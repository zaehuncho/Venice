# Orion — Live capture→detect→fire Bug-Hunt (2026-07-09, adversarial)

Read-only review of the market-critical path (a bug here = a live miss/misfire for a paying user). Line numbers verified against the tree at/after ceiling commit `c9267fab`.

## 🔴 HIGH — likely live miss/misfire
**1. `holdStartFrameAgeMs` always 0 for ButtonShot/Go-To → learned hold clock biased ~1 frame EARLY.** `AutomationEngine.cpp` `beginShot` does `shot_ = ShotContext{}` (:1810) then stamps `shot_.holdStartFrameAgeMs = shot_.frameAgeMs` (:1840) — reading the just-zeroed field. `triggerRelease` (:3894) then computes a hold clock ~one frame-age (~10-33ms) short → `globalHoldToReleaseMs` learns short → every blind/feedforward fire on the hold clock fires ~one frame early. **FIX (native):** preserve the pre-reset frame age across the `ShotContext{}` reset (same pattern as `networkOffsetMs`). One-line save/restore. *[deferred — native, collides with the running security build]*

**2. RC-2b stall override never reaches native — the frame_count gate drops it.** Sidecar synthesizes a `meter_present:false` stall emission (`autogreen_sidecar.py:61-80`) with a `heartbeat` counter so native can accept it, but `RemotePlaySession.cpp:2101-2103` gates every emit on `sidecarFrameCount != lastSidecarDetectionFrameCount_`, and `frame_count` is frozen during a stall → every stall emission discarded → the meter-lost reset chain (the 293s-HOLD-bug fix) doesn't fire until the next unique frame. **FIX (native):** bypass the frame_count gate when `meter_present==false` (or dedupe on `heartbeat`). *[deferred — native]*

**3. Decoder-PTS discontinuity (Chiaki reconnect) → ~1-2s blind rejection + garbage velocity after every reconnect.** `remote_play_orchestrator.py:1716-1746` PTS→wall/epoch offsets are slow EMAs re-init only when exactly 0.0; on reconnect PTS restarts near 0 while offsets hold the old mapping → `frame_age_ms` reads huge → native `sidecarFresh` rejects ~70 frames + a sliding ts feeds detect/tip-reg/oracle/Kalman → first shots after reconnect run on corrupted timing. **FIX (Python):** detect the jump (`|(now−pts_s)−offset| > ~250ms`) → hard re-seed both offsets. *[PARALLEL-SAFE now]*

**4. Unlocked `sys.stdout.write` in the orchestrator can corrupt the JSONL IPC — on the no-meter fire path.** The sidecar locks stdout (`_emit_lock`, `autogreen_sidecar.py:40-54`) because the ~130KB preview line splits across writes; 3 orchestrator emitters write the SAME stdout with NO lock: `remote_play_orchestrator.py:714` `pose_landmark` (the no-meter RELEASE trigger), `:744` `pose_overlay` (every frame), `:2681` `calibrate_meter_status`. Interleave → both lines fail `QJsonDocument::fromJson` (silently skipped) → a lost meter sample at the tip = mistimed release. **FIX (Python):** route all three through the sidecar `_emit` / a shared lock. *[PARALLEL-SAFE now]*

## 🟡 MEDIUM
**5. CadenceLock mis-stamps up to 6 frames after a dropped card frame → inflates frame_age → over-lead → early fire.** `capture_card_backend.py:151-160`: a 1-3-frame drop advances the grid exactly ONE period regardless of periods skipped → subsequent frames read +16.7/+33ms phase error → up to `relock_after=6` frames stamped too old; engine adds `clamp(frameAgeMs,0,20)` to the lead → over-lead up to ~17-20ms. **FIX (Python):** quantize the phase error to the nearest integer grid multiple (`k=round(gap/dt_est)`) before the outlier test. *[PARALLEL-SAFE now]*

**6. [ORION_MEASURED_LEAD] double-counts network latency (flag-gated — pre-launch trap).** `AutomationEngine.cpp:2227-2246` (+ mirror :4257): `effectiveLatency = abs(networkOffsetMs) + … + measuredLatencyMs_`, but the oracle's measured latency ALREADY contains the round trip (the fused block says "never re-add networkOffsetMs", :2512) → when `measuredLeadEnabled` ships, fires ~half-RTT+jitter EARLY. Flag off today. **FIX before flip (native):** drop the `abs(networkOffsetMs)` term when `useMeasuredLead`. *[deferred — native]*

**7. `CaptureCardBackend.stop()` releases cv2 from another thread while the reader may block in `cap.read()`** (`capture_card_backend.py:598-610`) — MSMF/DSHOW release-during-read can access-violate → takes down the sidecar in the wedge-recovery path it exists for. Also `is_healthy()` doc says 30 bad reads, code breaks at 120 (:456). **FIX (Python):** abandon the wedged handle (let process-exit clean) or interlock read+release on the reader thread; fix the doc/threshold. *[PARALLEL-SAFE now]*

**8. Takion drain quantization (0-8ms, up to 50ms first input) undercuts the sub-tick fire.** `feedbacksender.c:320-341`: the Orion latest-wins slot drains on the sender's timed wake → the sub-ms `OrionPreciseFireThread` + the earlier-only tick-snap (`fusedTickSnapEpsilonMs=5.5ms`) are quantized by 0-8ms drain jitter → a "snapped" press can still slip a tick, defeating the claimed ±1.8ms σ. Not a regression. **FIX (fork/native):** model the 8ms drain in the tick-snap σ, or disable snap on the pipe path. *[deferred]* (Ties to the Day-1 "on-arrival input flush" lever — the real fix is draining on-arrival.)

**9. 1-deep Orion slot can swallow a sub-8ms press+release pair** (`orioninputbridge.cpp:130-153` stops coalescing at edges, but each edge overwrites the single slot before the ≤8ms drain). Latent — no current pulse <8ms (pump 42ms, flick 50ms). **FIX (fork):** min-pulse assert or 2-deep edge queue before tightening any pulse. *[deferred — latent]*

## LOW / notes
- `previewLag_` mixes decoder frame_number vs frame_count → meaningless (diagnostic only, `RemotePlaySession.cpp:1983`).
- Sidecar telemetry payload assembled from unsync attribute reads (`autogreen_sidecar.py:539-750`, `meter_present` read twice) → occasional one-frame mis-join. FIX: publish a single snapshot dict/frame.
- Dead-but-fed `TemporalSampler::updateKalman` with uncleared `kf_.P01` cross-covariance → latent poison if ever wired.
- Rejected sample instantly kills `meterFresh` → single-frame detector blips flap the vision-healthy checks (blind-suppress papers over it).
- Capture-card `frame_number` restarts at 1 after in-process backend re-open → briefly confuses MeterBoxRing join (cosmetic).

## Fix plan
- **PARALLEL-SAFE now (Python — fire while the native security build runs):** #3 (PTS re-seed), #4 (lock the 3 stdout writers), #5 (CadenceLock grid-quantize), #7 (capture stop safety + doc).
- **Deferred → native bug-fix pass after the security build commits:** #1 (holdStartFrameAge — HIGH), #2 (frame_count gate — HIGH), #6 (measured-lead double-count — before its flip).
- **Fork/latent:** #8, #9 → with the Day-1 on-arrival-flush lever.
