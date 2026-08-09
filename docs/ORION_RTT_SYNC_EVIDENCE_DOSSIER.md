# Orion — RTT / Network-Sync Evidence Dossier + Waved Fix Plan

_Produced 2026-07-22 in response to `docs/ORION_RTT_SYNC_INVESTIGATION_PROMPT.md`. A 6-agent read-only fan-out over both repos (`NexusVision` + the sibling Chiaki fork `chiaki-ng-src`@`orion`), followed by a lead adversarial-verify pass re-reading every load-bearing `file:line` + log/settings artifact. Evidence standard: every claim = exact `file:line` + a real artifact (log line, settings value, `sc query`, filesystem). Live-only items are labelled. **MAP + ROOT CAUSE first; the fix plan is flag-gated, default-OFF, reversible; nothing applied; no default flipped.**_

Repos: `C:\Users\aaron\Desktop\NexusVision` · fork `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`).

---

## 0. The Q1 GATE answer (resolve-first, per the brief)

**VERDICT: APPLIED-BUT-NEGLIGIBLE-WRONG-HOP.** The sync offset is **not** hard-zeroed on the primary shot type today — but it is a ~6.5 ms LAN-hop nudge, ~10 % of the real timing error, and it becomes zeroed the instant the measured-lead lever is switched on.

- **`useMeasuredLead = FALSE`** — `AutomationEngine.cpp:2338-2339` ANDs four terms; two are false: `measuredLeadEnabled` (`settings.json:60 "measured_lead": false`, `AppConfig.h:182` default false, `ORION_MEASURED_LEAD` unset on the rig) and `leadRebaselined` (`learning.json:8 "lead_rebaselined": false`; only writer `rebaselineLeadClocks():3869` is itself measured-lead-gated → never runs; **0** `LeadRebaseline` lines in the 41 MB log). So at `:2358` the ternary keeps `std::abs(shot_.networkOffsetMs)` — **applied, not zeroed.**
- **Per shot type** (from `Release issued: … offset=` + `Scheduled fire: … heldOffsetMs=`):
  - **Standstill (primary; `settings.json:3 active_shot_type`) + Go-To: APPLIED ≈ 6.2-6.6 ms.** Non-fade routes through `effectiveLatency` (`ffOffset`, `:2838-2839`) into the feedforward deadline. Live artifact: `seq=42 heldOffsetMs=6.6`, `seq=43 6.3`, `seq=44 6.2`, all `code=feedforward_target shot=Standstill`.
  - **Fades: get nothing** — `:2838` routes fades to `shotTypeOffsetMs(bucketKey)`, not `effectiveLatency`, so `networkOffsetMs` never reaches the fade deadline.
  - **tempo: N/A live** (`tempo.enabled=false`).
- **The applied ~6.5 ms is not a court RTT.** It decomposes (`rtt_sync_engine.py:637-638`) as `decode_comp (~3.5-7.5 ms) + tick_phase_advance (2.5) + court_bias (0) + predicted_half (~0.4 ms LAN)`. The real PS5→court round-trip (tens of ms) is **absent**.
- **The zeroing trap (the crux of Q1):** the moment `measured_lead` is ever turned on, `useMeasuredLead→TRUE` → `:2358` (and scheduled mirror `:4453`) **zero `abs(networkOffsetMs)`** to avoid double-counting the oracle's round-trip. **So a correct court-RTT must NOT re-enter through `networkOffsetMs` if measured-lead is ever used — it must prime the measured-lead oracle (`measuredLatencyMs_`, `:2363/:2656`).** Today the trap is latent (measured-lead off), so `networkOffsetMs` is the working door.

**Consequence for the whole effort:** sync today is a real-but-negligible lever. It is worth fixing, but **it is NOT where the make-rate is lost.** The very shots the 6.6 ms offset was applied to (seq=42/43/44) are the same shots the detection dossier proved fired blind at 56 % fill, `verdict=LATE +66 ms` — a **detection** failure. RTT sync is a ~6.5 ms→~25 ms correction sitting on top of a ~66 ms detection error. Prioritise accordingly (see §5).

---

## Part 1 — The Map (verified, per area)

### A. Python engine (`rtt_sync_engine.py`, 817 ln)
- `CourtIPDetector` (`:167`): pps-scored public-IP lock (`score = pps*log2(pkts)`, ×2 for hard-coded "2K ranges" the code calls guesses, `:168/:215/:232`). **Live-wired but starved** — its only packet feed is `RTTSyncEngine.observe_packet` (`:537`) ← sidecar stdin (`autogreen_sidecar.py:944-953`) ← `RemotePlaySession::observePacket` (`:896`) ← `NetworkBridge::packetObserved` (`OrionAppController.cpp:2270`) — the disabled bridge. **Cannot lock without the bridge.**
- `KalmanFilter1D` (`:57`): live scalar filter (`q/r` from settings), but its input is the LAN ping.
- `TickSynchronizer` (`:347`): circular-mean phase lock; **dormant** (`observe_tick` needs relayed packets).
- `CourtProfileDB` (`:293`): per-court persistence; **effectively dead** — `update_profile` is court_ip-gated (`:735-736`), court_ip never set → `court_profiles.json = {}` (2 bytes).
- **Wrong hop:** `RTTSampler.measure_rtt_ms` (`:119-161`) pings PC→gateway via ICMP/TCP; the release rides PS5→court. Seed: `remote_play_orchestrator.py:1087-1088 set_ping_target(gateway_ip)`. Artifact: all `RTT tick:` lines end `court=-`, `raw=0.5-1.0ms jitter=0.3ms`.
- **Dead code (F):** `pre_shot_warmup:521` and `get_effective_offset_ms:672` = **0 callers**; `align_release:676` only reachable in the standalone `virtual_controller=True` path (native runs `False`).
- **Snapshot→native:** `autogreen_sidecar.py:835-859` builds `payload["rtt"]{…}` → stdout → `RemotePlaySession.cpp:2077-2098` → `telemetry_.syncAdjustMs = predicted>0 ? predicted : effective` (`:2090`).
- **Tests:** `test_rtt_sync_engine.py` covers the detector in isolation + decode-comp; **untested:** the ping loop (all tests `ping_enabled=False`), `CourtProfileDB`, tick align, the native↔sidecar relay.

### B. Native bridge + estimator (`NetworkBridge.cpp` 470 ln, `RttEstimator.h` 81 ln, `nexus_svc.py` 566 ln)
- `NetworkBridge` is a **TCP client** to the WinDivert service `nexus_svc.py` at `127.0.0.1:47291` (`:20/:71`). Court lock rule = **first public game-port peer** (`:361-368`, ports `isLikelyGamePort:212-220`), not pps-scored. Emits `courtIpDetected` (`:366`), `packetObserved` (`:460`).
- `RttEstimator.h`: RFC3550 interarrival jitter (`:66`) + EMA RTT (`α=0.15`, `:67`); `syncAdjustMs = rttMs*0.5 + min(8, jitter*1.2)` (`:75-78`). **Real inputs today: none** — only fed by `NetworkBridge.cpp:414` (bridge off) → sits at reset (0).
- `nexus_svc.py`: needs **pydivert + WinDivert64.sys**, a `NETWORK_FORWARD` sniff (sees only *routed* traffic), a SYSTEM service (pywin32), token ACL. **Not installed/running:** `sc query NexusVisionSvc` → **1060 "does not exist"**, token file absent, `nexus_svc.log` ends `2026-07-02 11:12:21 stopped`.

### C. The fork (`chiaki-ng-src`, branch `orion`) — the correct hop
- **`q.rtt` is discarded** (triple-confirmed): `streamconnection.c:695-704` — `q` is a local; the only touch of `q.rtt` is the log at `:696-703` (`"…rtt=%.4f, loss=%lld"`); the handler then stores **only** `measured_bitrate` (`:704`); `q` goes out of scope. Even the log is dead — `CHIAKI_LOGV` = `((void)0)` under `NDEBUG` (`log.h:49-50`) and VERBOSE-masked in debug (`main.cpp:300`). `streamconnection.h:80` has **no rtt field** (last member `double measured_bitrate`).
- Senkusha RTT is one-shot (`senkusha.c:385 rtt_us`, stored once `session.c:649`, never re-measured).
- **No export exists** — `rg 'q.rtt|netstat|ORION_QRTT|compose_takion'` in the fork returns only the two log refs; absent in Orion. `compose_takion`/`ORION_QRTT` are doc-only.

### D. The UI (`DashboardPage.qml` 511 ln, `GeneralPage.qml`, `RemotePlayPage.qml`) — verified live-vs-fake
| Field | QML | Q_PROPERTY writer | State | Artifact |
|---|---|---|---|---|
| Court IP | `Dashboard:358`/`General:128` | `telemetryCourtIp` ← `RemotePlaySession.cpp:2096` (bridge path `:2241-2264` dead) | **Frozen "—"** | last lock `2026-07-02T09:37:18`; ticks `court=-` |
| "RRT" (typo) | `Dashboard:376`/`General:129` | `rttMs` ← `:2087` (`rtt.filtered_ms`) | **Live, LAN hop** | `filtered=0.6-0.7ms` |
| Jitter | `Dashboard:378` (warn ≥4) | `jitterMs` ← `:2088` | **Live, near-zero** | `jitter=0.3ms` never ≥4 |
| Applied offset | `Dashboard:382` | `effectiveSyncAdjustMs` ← `syncAdjustMs :2090` | **Live, wrong-source** | mode Auto → sidecar LAN offset |
| Ticker Latency | `Dashboard:356` | `tickerLatencyMs` ← `:2092` (`next_tick_eta_ms`) | **Live, mislabeled** | it's a tick ETA, not a latency |
| Inbound/Outbound | `Dashboard:318-339` | `in/outboundPackets` (bridge only) | **Frozen 0** | `Packet bridge: off` ×52 |
- **`syncSource` (`.h:181`) + `syncConfidence` (`.h:182`) exist but are referenced by ZERO QML** (grep-confirmed) — the cheapest honest fix.
- "RRT" typo at `DashboardPage.qml:376`. `RemotePlayPage.qml:44-47` filters `court ip|network bridge|packet bridge` lines OUT of its log view.

### E. Consumption / merge (`OrionAppController.cpp`, `AutomationEngine.cpp`)
- Merge (`OrionAppController.cpp:1757-1814`) pivots on `networkBridge_.connected()` (`:1772`). **Bridge never connects** (opt-in off) → the `else` at `:1809-1810 telemetry_ = rpTelemetry` (**sidecar wholesale**) is **always** taken; the bridge-preferred/`sidecarCourtLocked` block is doubly dead.
- `networkAutomationOffset()` (`:7112-7128`) returns `telemetry_.syncAdjustMs` → `updateNetworkQuality` (`AutomationEngine.cpp:1566-1577`) → `updateNetworkOffset` → `pendingNetworkOffsetMs_ = clamp(offset,-100,100)` (`:1556`) → `shot_.networkOffsetMs` — **this is Q1's offset.** Artifact: `Scheduled fire: … heldOffsetMs=6.6` (`:2119-2126`).
- **F dead ingest:** the `orion_sync_*` status-file keys (`:6816-6888`) have **zero writers** repo-wide (grep-confirmed) — dead code.

### F. Dormant machinery (Q6)
| Feature | Wired? | Fires? | Correct-RTT unlocks? | Verdict |
|---|---|---|---|---|
| tick-snap / `align_release` (`AutomationEngine.cpp:2642-2678`; `rtt_sync_engine.py:676`) | native: fused-block only; python: standalone mode only | **No** (`fused_fire=false`, shadow-only; 372 `FusedShadow` lines, 0 live) | No — orthogonal to RTT | **KEEP + FIX-to-wire** (σ_tick 4.8→1.8 ms) |
| jitter-adaptive gating (`:3364-3367/:3373/:4524`) | **Yes, live** | **No** (LAN jitter 0.3 ≪ 4.0 trigger) | **YES** (real WAN jitter) | **KEEP** — unlocked by court lock |
| `CourtProfileDB` | Yes | **No** (`{}` empty, `court_bias=0`) | **YES** | **KEEP** — downstream of court lock |
| decode-comp (7.5 ms) | **Yes, live** (`rtt_sync_engine.py:610`) | **Yes** — the dominant real term in the ~6.5 ms | No — independent, already works | **KEEP as-is** |

---

## Part 2 — Root cause

1. **Court detection is dead → the sidecar has no court to measure.** The opt-in gate `OrionAppController.cpp:2286-2291` requires `networkEnabled && passiveSniffingEnabled`; `passiveSniffingEnabled` defaults false (`AppConfig.h:253`) = `network_packet_capture_opt_in` (`settings.json:92 false`). The **only** court-lock writer (`:2260`) traces to the **only** `courtIpDetected` emitter (`NetworkBridge.cpp:366`), reachable only if the bridge starts — it doesn't. Compounded: the service isn't even installed (`sc` 1060) and the WinDivert `NETWORK_FORWARD` sniff needs **ICS topology** (PS5 routed through the PC) to see any packets. Artifact: last lock `2026-07-02T09:37:18Z`, `Packet bridge: off` ×52 through today.
2. **The hop is wrong by design.** The Python engine measures PC→gateway ICMP (`rtt_sync_engine.py:119-161`); the release travels PS5→court. The fork measures the *correct* PS5→court RTT (`q.rtt`) and throws it away (`streamconnection.c:704`).
3. **The UI reads wrong because the merge is starved.** Bridge off → merge takes the sidecar-wholesale branch (`:1810`) → every Network field is fed the LAN-hop sidecar snapshot; Court IP + packet cards are frozen because their only source (the bridge) is off; `syncSource` (which would expose the wrong hop) is bound by no QML.

---

## Part 3 — Waved fix plan (ordered by dependency; flag-gated, default-OFF, reversible)

**Sequencing rule (from the brief):** the Q1 reconciliation (§0) and the Q3 STEP-1 **log-only** probe come *before* any consumer change. Nothing consumes `q.rtt` for timing until its units/cadence are proven.

### Wave 0 — Honest UI (offline-provable now, zero behavioral change)
| Item | Edit site | Fix | Proof |
|---|---|---|---|
| "RRT"→"RTT" | `DashboardPage.qml:376` (string) | typo | visual |
| Surface hop/source | new `StatRow` after `:382` binding `orion.syncSource`; refine labels `RemotePlaySession.cpp:2094` to name the hop (`"Court"` vs `"LAN gateway"`) | wrong-hop never silent again | property already populated (`court=-` path) |
| Court IP honest states | `Dashboard:358`/`General:128` + new `Q_PROPERTY bool packetCaptureActive` (getter = `networkEnabled && passiveSniffingEnabled && bridge.connected()`) | locked-IP / "detecting…" / "packet capture off" instead of frozen "—" | settings + bridge state |
| Packet cards | wrap `Dashboard:306-342` in `visible: packetCaptureActive` + off-note | live counts or honest "off" instead of frozen 0 | `Packet bridge: off` |
| Relabel Ticker | `Dashboard:356` string → "Next Tick ETA" | it *is* an ETA, not a latency | `:2092` source |
| Remove dead ingest | `OrionAppController.cpp:6816-6888` | delete the writer-less `orion_sync_*` block | 0 writers (grep) |
- All display-only; underlying capture stays opt-in gated. Revert = restore strings/bindings.

### Wave 1 — Q3 STEP-1: real-hop probe, LOG-ONLY (the crux, gates everything downstream)
- **Fork** (`streamconnection.c:703`, flag `env ORION_QRTT`, default unset): `getenv("ORION_QRTT")`-gated `fprintf(stderr, "ORION_QRTT rtt_ms=%.3f rtt_raw=%.6f seq=%u ts=%llu\n", q.rtt*1000.0, q.rtt, seq++, chiaki_time_now_monotonic_us()); fflush(stderr);` — emits BOTH raw and ×1000 so the unit is provable side-by-side (a wrong unit = 1000× error). Byte-identical when unset (the line is already compiled-out/masked).
- **Orion** (`chiaki_backend.py:679`, in `_stderr_reader_loop`): add an `ORION_QRTT` regex branch → log to `orion_native.log` next to the concurrent gateway-ping sample. **`rtt_sync.qrtt_authoritative` stays OFF — nothing consumes it.**
- **Proves:** (a) units (raw vs ms vs senkusha-µs vs live gateway-ms), (b) cadence (ts-deltas → true Hz), (c) the magnitude gap. **Expected: q.rtt ≈ 15-40 ms vs the gateway ping's 0.3-1 ms** (≥ the `learning.json:87` Standstill baseline 13.6 ms) — a 20-80× delta = the blind advantage.
- **LIVE-ONLY** (needs a fork build + a real game). Revert = unset `ORION_QRTT`.

### Wave 2 — Consume q.rtt (gated), reconciled with Q1
- **Feed** (`rtt_sync_engine.py` new `submit_qrtt_ms` → the Kalman choke `_process_rtt_sample:704-749`) as **authoritative**; demote ICMP to court-detect + fallback + slow bias (skip the raw-ping feed `:700-701` while q.rtt is fresh). Wire `backend.on_qrtt` at `remote_play_orchestrator.py:1171/:1262`; parse callback `chiaki_backend.py:492`. Flag `settings rtt_sync.qrtt_authoritative` (default False).
- **Q1 co-design (mandatory):** with `measured_lead` **OFF** (current default), q.rtt → `networkOffsetMs` gives a real advantage on Standstill/Go-To immediately. **If `measured_lead` is ever enabled**, q.rtt must instead prime the **oracle** (`measuredLatencyMs_`), because `:2358` zeroes `networkOffsetMs` then — do not let both doors add the same RTT. **Fades** take neither door (`:2838 → shotTypeOffsetMs`); a fade advantage needs the fade clock or a vision-path fire — flag as a separate sub-item.
- **Fail-soft:** missing/garbage/out-of-window q.rtt → fallback to today's value; the 30 s staleness reset keeps the estimate alive; **no release path awaits q.rtt.** Revert = key OFF (byte-identical).

### Wave 3 — Court detection revival (Q2; product decision + live)
- **Surface** a "Network packet capture" opt-in toggle that explains the **three** requirements: flip `network_packet_capture_opt_in`, one-time admin `install_nexus_service.bat` (installs `NexusVisionSvc`), and **ICS topology** (PS5 routed through the PC). Do **not** force it on silently.
- Once a court locks, **jitter-gating + CourtProfileDB light up as-is (no code change)**; Court IP + packet cards in the UI go live.
- **Note:** with Wave 2 delivering the correct hop via q.rtt, court detection is **no longer the timing path** — its remaining value is UI honesty (show the court IP), `CourtProfileDB` warm-start, and exposing real WAN jitter to the (already-correct) jitter gate. So Wave 3 is optional/secondary to Wave 2 for the make-rate.
- **LIVE-ONLY** to prove a re-lock (needs opt-in + service + ICS + a real game).

### Wave 4 — Additive (Q6)
- **Wire tick-snap into the live native fire path** (currently shadow-only, `fused_fire=false`) behind its own flag — orthogonal to RTT, needs its own A/B; potential σ_tick 4.8→1.8 ms. decode-comp already works (keep). jitter-gate + CourtProfileDB are unlocked by Wave 2/3, no gate-logic change.

---

## Part 4 — Guardrails checklist
- **Flag-gated default-OFF, byte-identical when off:** `ORION_QRTT` (fork, unset), `rtt_sync.qrtt_authoritative` (False), `network_packet_capture_opt_in` (False), the tick-snap flag, the new UI `packetCaptureActive` (display-only). ✔
- **Fail-soft — never blocks a release:** every RTT/court value missing → fallback to the current clock; no release path awaits q.rtt or a court lock; the guaranteed-release hard cap is untouched. ✔ (The one thing that must never change: nothing may gate the release on a network value.)
- **Detection gates green:** RTT work is sidecar/native/fork only — it must not touch `simple_meter_reader.py`; confirm `python tools/regression/run_gates.py --no-timing` stays **46/46** and reader byte-identity holds. ✔
- **Native tests green:** `OrionNativeTests` must pass (note from the detection investigation: the suite doesn't emit its Totals line in this environment — the timing invariants for `networkOffset` need that fixed or a live batch). ⚠ live/env-gated.
- **Opt-in surfaced, not forced:** re-enabling packet capture is a product decision with real trade-offs (kernel driver load, SYSTEM-priv sniffer, one-time admin, inert without ICS) — surface with consent, never silent-flip. ✔

---

## Part 5 — Honest gaps (live-only / unproven)
1. **q.rtt units (seconds vs ms)** — inferred from `%.4f` + double + senkusha contrast; **STEP-1's job**, not runtime-proven. Do not hardcode ×1000 first.
2. **q.rtt cadence** — console-driven (~1 Hz typical); measure via ts-deltas in STEP-1.
3. **Which fork channel is live** — `--ffmpeg-pipe-stdout`/stderr has no source match in the fork; the confirmed-live GUI IPC is the `'ORFR'` named pipe, so production likely needs an `'ORQR'` sideband record, not just stderr. Settle in STEP-1.
4. **Court re-lock** — needs opt-in + `NexusVisionSvc` installed + ICS topology + a real game; live-only. If WinDivert opens but no lock appears, that isolates the failure to non-ICS topology.
5. **Q5 "real advantage" — can only be shown live.** Definition: a correct sync shifts the fire **earlier by the true one-way court delay** so the *server-side* release lands in the green window. Measure: median `|errorMs|` and in-green rate (`verdict`, `blindFire=0`) before vs after, on matched Standstill sets, A/B `qrtt_authoritative` on/off. **A feature that can't be shown to move the make-rate is not an advantage** — this must be proven on a live batch, not asserted.
6. **The dominant timing error is NOT network.** seq=42/43/44 carried the 6.6 ms offset yet missed by 66 ms (`verdict=LATE`) because detection under-read (see `ORION_DETECTION_EVIDENCE_DOSSIER.md` D1/D4). **Fix detection first; RTT sync is a real but second-order lever** (~6.5 ms today → maybe ~25 ms with a correct court hop, vs a ~66 ms detection error).

_(Original order superseded by the Addendum v2 re-sequence below.)_

---

# Addendum v2 (2026-07-22) — Q0 hop-determination, the make/miss prerequisite, and Q6a (the reframe)

_The brief was revised around the operator verdict: **2K lag-compensates to the local release moment** (court RTT does NOT move the target), so online greens fail only because **marginal green-edge releases get reconciled-away under jitter, worst on tempo.** Model to build: **Remote-Play RTT → the release lead; court JITTER → a safety-margin signal.** This addendum answers the new Q0 + Q6a and re-sequences the plan; everything above (the map, root cause, Waves 0-2 mechanics) still holds._

## Q0 — Which hop governs the green
**VERDICT: MIXED — CONSOLE/LOCAL-timed release LEAD + court-JITTER margin factor.** The PC↔PS5 Remote-Play RTT governs *where* to fire; the PS5↔court hop is **not** a timing lever — court **jitter** is only a reconciliation/safety-margin signal. Confidence **MEDIUM** (three converging supports; the two decisive captures are absent today).

- **Orion encodes no adjudication/lag-comp — it's external netcode.** `rg 'server|court|adjudicat|lag.?comp|reconcil'` over the engine hits only court-*position* comments (`AutomationEngine.cpp:1010/:1742/:3249`); repo-wide `lag.?comp|adjudicat|reconcil` = **0**. The only PS5↔court awareness is the dead court-RTT *measurement* plumbing (`OrionAppController.cpp:1770-1779`). So the operator model is a behavioral prior about 2K's server; Orion implements none of it → it must be settled empirically.
- **The current log CANNOT settle it — and this is the pivotal finding:**
  1. **No server make/miss ground truth exists.** 0 make/miss/banner/result lines in 41 MB. The logged `verdict` is a **degenerate meter self-grade** (`OrionAppController.cpp:1669`, emitted *before* the authoritative gate `:1685`), quantized to spikes — 993 `EXCELLENT`=exactly 0.0, 813 `LATE`=exactly 66.0 = `meterRecedeLatePct(12.0)×meterMsPerPct(5.5)` (`AutomationEngine.h:511/:519/:1718`). The real on-screen timing banner is read **only** in `bannerCalibration` mode (`h:133-137`), which is **off** (`settings.json:11`).
  2. **No hop-varying RTT.** `court=-` in 100 % of ticks; no Remote-Play `q.rtt` yet. `|errorMs|` vs `jitterMs` r=−0.12, vs `heldOffsetMs` r=+0.04 (n=1906) — **null**, as expected for LAN near-constants.
- **Directional support (n=2703 outcomes):** self-graded EXCELLENT-rate Standstill 44 % (best) → harder types worse (Go-To 19 %, Back Fade 11.5 %); "green-by-fill yet self-graded miss" is real (58 %) and type-skewed (Go-To 79 %). **Caveat:** that miss population is two Orion subsystems (fill reader vs recede-grader) disagreeing, **not** the server rejecting a release — and the `LATE=66` mass is the same dead-top **detection** artifact the detection dossier found, not a network signal. **Tempo is untestable here** (`tempo_mode_enabled=false`, 0 tempo rows).
- **Experiments that settle it:** (a) **offline-vs-online dead-center A/B** (primary): flat green-rate across on/offline ⇒ console-timed; marginal-only online drop ⇒ court-jitter margin. (b) vary court RTT / hold RP: flat lead-vs-court-RTT ⇒ console-timed. (c) vary RP RTT / hold court: ~1:1 lead slope ⇒ RP loop owns the lead. (d) free regression once STEP-1 logs both RTTs **+ make/miss**.

## ⚠ The prerequisite the reframe exposes — a MAKE/MISS oracle (new Wave 0.5)
Q0 confirmation, Q5's "real advantage," **and** Q6a's A/B **all** require the server make/miss — which the bot **cannot currently see live**. Without it, none of the downstream network work can be *proven*. **Build it first.** Cheapest path: wire `bannerCalibration=true` (`AutomationEngine.h:133-137`, existing hook) or add a CV reader for the on-screen make/miss + timing banner. Log per shot: `make|miss`, timing-banner colour, and **release margin `|fillAtRel − greenCenterPct|`** (the marginal-vs-center axis Q0/Q6a turn on). This is the single highest-leverage enabler in the RTT track.

## Q6a — Jitter-adaptive tip→center aim lever (the one make-rate lever)
The engine **already carries the primitive** — it just pins the aim to the tip:
- Aim = tip today: `greenWindowTarget="tip"` (`AutomationEngine.h:79`), autonomous-vision targets `fill=100` with fire-floor `tipFireMinFillPct~88` (`:2320`), `targetMode="green_tip"` (`:2413`).
- But `greenCenterPct` is computed + carried per shot (`:1341/:1372/:1387`), and the fused path already has a time-domain tip-backoff `deltaAim` that **grows with landing uncertainty** `sigmaLand` (`:2648-2651`; `tFire = muTipMs − deltaAim − fusedLead`, `:2661`). So Q6a is a **modulation of an existing knob, not a new aim system.**
- **The lever (flag-gated, default-OFF):** `aimJitterBackoffMs = clamp(gain·max(0, networkJitterEmaMs_ − jitterAimFloorMs), 0, maxAimBackoffMs[bucket])`.
  - **Fused path:** `deltaAim += aimJitterBackoffMs` (`:2648-2651`).
  - **Feedforward / predictive / green_confirmed:** fire earlier by the same `aimJitterBackoffMs` (equivalently, reduce the effective target fill from tip toward `greenCenterPct`). Apply uniformly so it's not only the shadow-only fused path.
  - **Never-below-center clamp:** cap the backoff so predicted landing fill ≥ `greenCenterPct` — it moves tip→center, **never** toward `green_lo`. Still deep green.
  - **Tempo gets the largest `maxAimBackoffMs`** (`mode==TempoSquare` / `bucketKey "|tempo"`, `:1787/:1886`) — the operator's worst case.
- **Court-jitter is the fuel — this is where the court hop earns its keep:** feed `networkJitterEmaMs_` (`updateNetworkQuality`, `:1566-1571`) a *real* court jitter (RFC3550 from `RttEstimator.h:66` once the court is live, or the variance of the fork `q.rtt`), not the ~0.3 ms LAN it sees today.
- **A/B: LIVE-ONLY** (server reconciliation invisible offline) — make-rate on high-jitter sessions, tip vs jitter-center, tempo-focused, needs the Wave-0.5 oracle. **Offline gate:** jitter ≤ floor ⇒ backoff=0 ⇒ **byte-identical** ⇒ `run_gates` 46/46 + `OrionNativeTests` green.
- **Fail-soft:** default-OFF; jitter missing ⇒ aim=tip (identical); it fires **earlier**, never later ⇒ cannot touch the guaranteed-release cap; clamped inside green. Flag `ORION_JITTER_AIM` / `settings.jitter_adaptive_aim`.

## Re-sequenced plan (supersedes the original ordering)
| Wave | Item | Gated on | Provable |
|---|---|---|---|
| **0** | Honest UI (RRT→RTT, syncSource/hop, Court-IP states, packet cards, dead `orion_sync_*` cut) | — | offline now |
| **0.5** | **Make/miss oracle** (`bannerCalibration=true` or CV banner reader; log make/miss + release margin) | — | live (enables all proof) |
| **1** | Q3 STEP-1 **dual-RTT log-only** probe (RP `q.rtt` + court ping, both untouched, alongside each graded shot) | 0.5 for outcomes | live |
| **2** | Consume RP `q.rtt` as the **lead** delta (layered on the oracle; **not** `networkOffsetMs` if measured-lead is ever on — Q1 trap) | 1 (units) | live |
| **3** | **Q6a jitter→center aim** (the make-rate lever) | 0.5 (oracle) + court jitter (from 1/4) | live A/B |
| **4** | Court detection revival — **DIAGNOSTIC + jitter-feed only** (Q0: zero timing value; keep for UI, `CourtProfileDB`, and feeding Q6a's court jitter) | Q0 | live |
| **5** | Additive tick-snap (orthogonal to RTT) | — | its own A/B |

**Reaffirmed headline (now measured):** the dominant error is **detection, not network** — Q0's `LATE=66 ms` mass *is* the dead-top detection artifact (`meterRecedeLatePct×meterMsPerPct`), not a hop. RTT sync's honest value is **fast per-shot adaptation + jitter robustness**, and **Q6a is the single lever here that can actually move the make-rate** — via court **jitter** as a margin signal, and only once a make/miss oracle exists to prove it. Fix detection first (the other dossier's D1/D4); this track makes sync *correct, honest, and jitter-robust*.

_Ends v2._
