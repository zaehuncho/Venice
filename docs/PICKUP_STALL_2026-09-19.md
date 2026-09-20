# The pickup stall — root cause and fix (2026-09-19)

Follow-up to `docs/STANDSTILL_LATES_2026-09-19.md` §3.1 (class **b late-pickup**: 9 of 96
Sept-18 OPEN standstills, 6 LATE = 66.7 %, projected −5.2 of 18 LATEs with no observed EARLY
cost). That report named the symptom — the ownership-proof chain restarts 4–7 times with
`break_geometry` 3–6, the first *accepted* proof frame lands at 34–40 % fill instead of ~17 %,
`command_eta_ms` goes negative and the engine fires `live_tip_fired_late`. This document is the
cause, the fix, and what is and is not proved.

---

## 1. Root cause: the shape gate was judging the OVERLAY, not the detector

`AutomationEngine::recordPendingMeterOwnershipSample`'s geometry-continuity test (`geometryMatches`,
IoU ≥ 0.10, ≤ 1.50× per dimension, ≤ 1.25× aspect) ran on `DetectionResult::x/y/width/height`.
That rectangle is **not** the detector's proposal. With `ORION_READER_BOX_TIGHT=2` — which
`run_orion.local.ps1:575` sets on every live run — `simple_meter_reader._tight_display_box`
re-shapes the outgoing rectangle at the `detect()` production boundary so the overlay hugs the
meter's housing, and its final clamp lets each edge sit up to
`ORION_READER_BOX_TIGHT_SIDE_REACH` / `_TOP_REACH` / `_BOT_REACH` = **18 px** outside the served
box. The hug is re-derived from *this frame's* colour-path pixels, so it appears and disappears
between consecutive frames of a completely stationary meter.

The reader's own comment claims the transform is "DISPLAY-ONLY". It is not: the drawn rectangle
is the **only** box on the wire (`payload["bbox"]` → `sidecarResult.x/y/width/height`), and the
engine uses it for the overlay, for `meter_x/meter_y` continuity **and** for the false-lock
shape gate.

### 1.1 The arithmetic, from the live log alone

`logs/orion_native.log`, 2026-09-18T23:00:53Z, epoch 72 (WIDE OPEN standstill, **LATE**, oracle
gap 8 px):

```
23:00:53.478Z  BOX LATCHED: epoch=72 generation=153 box=[831,318,26,110]
23:00:53.599Z  Ownership proof census: ... first_fill=36.700 restarts=4 break_geometry=3
23:00:53.601Z  Ownership geometry break: old_box=813,318,44,118 new_box=830,319,26,118
               iou=0.583 width_scale=0.591 aspect_scale=0.591
23:00:53.601Z  TIP RESERVATION ... fill_pct=43.68 command_eta_ms=-25.015
23:00:53.601Z  TIP DEADLINE DECISION: disposition=fired_late lateness_ms=25.015
23:00:53.604Z  PICKUP: epoch=72 first_sight_fill=18.0 first_sight_ms_after_press=500.6
```

`831 − 18 = 813` and `26 + 18 = 44`. The right edge is invariant (857 vs 856). The "44 px box"
is the latched 26 px box plus exactly one side-reach on the left — a presentation artefact, not
a detection. The same signature on three of the other four logged breaks:

| epoch | old_box | new_box | Δleft | right edge |
|---|---|---|---|---|
| 58 | 773,300,**29**,121 | 756,306,**44**,127 | −17 | 802 → 800 |
| 65 | 690,323,**29**,119 | 673,319,**44**,119 | −17 | 719 → 717 |
| 72 | 813,318,**44**,118 | 830,319,**26**,118 | +17 | 857 → 856 |
| 33 | 1070,267,**36**,123 | 1077,267,**29**,124 | +7 | 1106 → 1106 |

The reader had *seen the meter at 18.0 % fill, 500.6 ms after the press*. The engine would not
accept a proof sample for another 122 ms because the drawn rectangle flapped.

### 1.2 The detector's rectangle never did this

`logs/diagnostics/detframes_*.csv` logs both rectangles per frame (`x,y,w,h` = drawn,
`det_x,det_y,det_w,det_h` = the detector's, added 2026-08-06 for exactly this reason).

* `detframes_20260917_142730.csv`, 2 465 detections: **`det_w` = 26 px on every single frame**
  (the CV landmark proposer builds `bw = round(ORION_CV_BOX_W × s)`, `meter_locator_cv.py:1343`),
  while the published `w` runs **26…44**.
* The per-edge surplus (`det_x − x`, `(x+w) − (det_x+det_w)`) histogram tops out at exactly
  **(0, 18)** and **(18, 0)** — the reach — and is 0–4 px on 97 % of frames.
* Across **20 887 consecutive detected frame pairs** in 7 live sessions
  (`detframes_20260917_130811/134104/140824/141836/142730`, `20260918_140934/152203`) the
  engine's own gate fails on the **drawn** rectangle **28 times** and on the **detector's
  twice** — a 14x difference on the same frames.

So the restarts are not a detector instability, not a BOX_LATCH gap, and not two proposers
disagreeing: the reader proposes one stable rectangle and paints a second, wobbling one over it,
and the engine was told only about the painted one.

### 1.3 The cost, measured over both engine logs (281 censused shots)

| | `break_geometry` ≥ 1 | `break_geometry` = 0 |
|---|---|---|
| shots | **24 (8.5 %)** | 257 |
| census `first_fill` p50 / p90 | **30.1 % / 37.6 %** | 17.6 % / 20.9 % |
| `first_fill` ≥ 32 % | **11 / 24 (45.8 %)** | 2 / 257 (0.8 %) |
| `restarts` p50 | **4.5** | 1.0 |
| `BOX LATCHED` → first `TIP RESERVATION` p50 / p90 / max | **120 / 134 / 157 ms** | 22 / 28 ms |
| that delay ≥ 100 ms | **10 / 17** | 2 / 131 |
| first reservation `fill_pct` p50 | **39.0 %** | 22.7 % |

(The 22 ms baseline is log-timestamp resolution plus sidecar relay; the frame-age-corrected
`l2r` in the forensic report reads ~3 ms for the same population. The **difference**, +98 ms, is
the quantity that matters and is unaffected by the offset.)

Break kinds over all 281 censused shots: **geometry 79**, gap 0, drop 2, anchor 3.

---

## 2. The fix

**Carry both rectangles and let the shape gate judge the one the proposer actually produced.**
Nothing is relaxed: the same IoU / dimension / aspect bounds run on every frame, on the detector's
rectangle. `max_proof_samples` is untouched, the geometry break is not disabled, and the
leniency path, the rise proof (`anchorRiseMinPct`), the first-sight bound
(`anchorMaxFirstFillPct`), structure verification and epoch identity are all unchanged.

Transport: the reader stamps the pre-transform rectangle on **every** published frame; the
orchestrator carries it on the same immutable per-frame snapshot as `bbox`; the sidecar emits it
as `det_bbox` inside the same `try` as `bbox`; native parses it into
`DetectionResult::detX..detHeight`. A frame that carries no detector rectangle — older sidecar,
or a build with the hug off, where the two rectangles are identical anyway — falls back to the
drawn rectangle on both sides of the comparison and is byte-identical to today.

### Files changed

| file | lines | what |
|---|---|---|
| `native_orion/src/OrionTypes.h` | 105–121 | `DetectionResult::detX/detY/detWidth/detHeight` (0 = absent) |
| `native_orion/src/RemotePlaySession.cpp` | 4897–4911 | parse `msg["det_bbox"]` (optional, positive w/h only) |
| `native_orion/src/AutomationEngine.h` | 386–403 | `Config::ownershipProofDetectorBox` (default `true`) |
| `native_orion/src/AutomationEngine.cpp` | 1780–1791 | env override `ORION_OWNERSHIP_PROOF_DETECTOR_BOX` |
| `native_orion/src/AutomationEngine.cpp` | 6348–6396 | `proofBox()` + `geometryMatches` reads it |
| `native_orion/src/AutomationEngine.cpp` | 6550–6558 | geometry-break census reports the compared rectangles |
| `native_orion/src/AutomationEngine.cpp` | 6635–6643, 6660–6667 | the episode's geometry reference is the proof box |
| `simple_meter_reader.py` | 14281–14294 | stamp `last_debug["det_box"]` on every frame, before the hug |
| `remote_play_orchestrator.py` | 448–452, 1597–1598, 4543–4554, 4620, 4644, 5868, 7794–7811 | `_last_meter_det_bbox` → `_ProcessedFrameSnapshot.det_bbox` → `'det_bbox'` |
| `native_orion/backend/autogreen_sidecar.py` | 780–781, 874–876, 900–901, 915, 958–959, 2723–2733 | `det_bbox` on the wire, cleared with `bbox` on an unhealthy frame |
| `native_orion/tests/AutomationEngineTests.cpp` | 554–557, 22094–22260 | 3 new cases |
| `tests/test_ownership_proof_detector_box.py` | new | 5 cases (flap shape + transport) |
| `tools/diagnostics/replay_with_presses.py` | 121–125, 252–262, 506–510 | harness repairs, see §5 |

### New knobs

* **`ORION_OWNERSHIP_PROOF_DETECTOR_BOX`** (native, default `1`). `0` restores the drawn-box
  ruler exactly as it shipped. Env-only — deliberately **no** `settings.json` key, so no
  production profile or installer surface changes.
* **`det_bbox`** — a new optional 4-int field on the sidecar telemetry payload. Additive; an
  engine that does not know it ignores it, a sidecar that does not send it costs nothing.
* No reader knob was added. `ORION_READER_BOX_TIGHT` and its reach knobs are pre-existing and
  unchanged; the hug still draws exactly as before.

---

## 3. Before / after

### 3.1 Engine unit test — the exact measured flap

`native_orion/tests/AutomationEngineTests.cpp::ownershipProofIgnoresTheDisplayHugFlap` feeds four
rising frames whose **detector** rectangle is a constant 26×120 and whose **drawn** rectangle
alternates 26 ↔ 44 with the right edge invariant — the epoch-72 signature, numerically.

| | drawn-box ruler (shipped) | detector-box ruler (this change) |
|---|---|---|
| `break_geometry` | ≥ 1 | **0** |
| `restarts` | > 1 | **1** |
| census `first_fill` | — (never promotes) | **10.0 %** (the first rising frame) |
| engine state after 4 frames | **`Idle` — never owned** | **`Holding`** |

The shipped ruler is not merely late on this input: with the flap alternating every frame the
proof never completes at all, which is the same failure mode as the 122 ms stall with a slower
flap. Verified by re-running the identical test with `ORION_OWNERSHIP_PROOF_DETECTOR_BOX=0`:

```
FAIL!  : ownershipProofIgnoresTheDisplayHugFlap() Compared values are not the same
   Actual   (engine.context().state): 0     (Idle)
   Expected (HoldState::Holding)    : 2
```

### 3.2 Refusal on genuine junk — unchanged

* `ownershipProofStillBreaksOnADetectorShapeChange`: the same drawn flap, but the **detector**
  rectangle stretches 26 → 60 px at an unchanged height (the "shares a centre with the meter yet
  stretches one dimension" false lock the gate exists for). The break still fires —
  `old_box=940,420,26,120 new_box=940,420,60,120` — and Square is **not** owned.
* `ownershipProofWithoutADetectorBoxKeepsTheDrawnBoxRuler`: with no `det_bbox` on the wire the
  flap still breaks the proof, i.e. the legacy path is untouched.
* `tools/diagnostics/pill_locator_replay.py --proposer cv --scale 0.6667 --armed --cls neg`:
  **113 hard-negative frames, 0 proposals (0.0 %)** — unchanged, as it must be: the locator and
  every reader gate are untouched, and the only reader edit writes a diagnostic dictionary key
  that nothing reads back.
* The 2026-09-12 shape gate, the outline gate, `STATIC_ZONE_QUARANTINE`, the rise proof and the
  first-sight bound are all upstream or orthogonal and were not modified.

### 3.3 Offline, on live reader output

Engine-replica ownership proof (first-sight bound 40 %, restart on the same geometry test, 3
unique frames rising ≥ 3.0 pp) over the 180 press epochs in the seven detframes sessions:

| ruler | geometry restarts | promoted | `first_fill` p50 / p90 / max |
|---|---|---|---|
| drawn box | **3** | 175 / 180 | 18.4 / 20.2 / 29.4 |
| detector box | **1** | 175 / 180 | 18.4 / 20.2 / 29.4 |

Stated plainly: **these particular sessions barely exhibit the pathology** (3 restarts in 180
presses), so this table shows the change is *inert where the flap does not happen* — it promotes
the same 175 presses at the same fills — and removes two thirds of the restarts that do happen.
It is not, and cannot be, a measurement of the 24 live shots that stalled: see §4.

---

## 4. What is NOT verified

* **No live A/B.** The −5.2-of-18 LATE conversion is the forensic report's projection, not a
  measurement of this build. It requires a session.
* **No frame dump covers a stalled shot.** The sessions that produced the 24 geometry-break
  shots (`session_20260918_174450`, `_224059`, `_201500`) have neither a framedump nor a
  detframes file. The "after" latch→reservation (target: back onto the 22 ms population) and
  first-accepted fill (target: back onto the 17.6 % population) are therefore projected from the
  mechanism plus the unit test, **not** replayed.
* **Why the hug flaps on some shots and not others is not explained.** `_tight_display_box`'s
  fresh branch depends on `_tight_src`, which read()'s colour path sets only on frames where it
  produced a source; the translate branch reuses the last shape. This change makes the engine
  immune to the flap rather than removing it, which is the right order — but if the drawn box is
  also wrong for the overlay on those frames, that is a separate, still-open reader question.
* **`meter_x/meter_y/meter_jump`** in `Release landing` still come from the drawn rectangle. They
  are a post-release settle diagnostic, not a timing input, so they were left alone; they are now
  the only remaining consumer of the hugged box inside the engine.
* **One native test fails in this working tree**:
  `bannerTrimBiasVotesMoveTheLeadAndNameTheirEvidence` (`bannerLeadTrimMsForType` reads 0,
  expected 3.0). It fails **identically with `ORION_OWNERSHIP_PROOF_DETECTOR_BOX=0`**, and no
  pre-existing test sets `detWidth`, so `proofBox()` returns the old values for every one of
  them — the failure is pre-existing in this (heavily dirty, multi-agent) tree and belongs to the
  banner-trim lane. Totals: **1 177 passed, 1 failed, 9 skipped**.

---

## 5. Two harness blind spots found on the way

`tools/diagnostics/replay_with_presses.py` could not have reproduced this bug, and neither can
any past replay run with it:

1. Its pinned `LIVE_ENV` never set `ORION_READER_BOX_TIGHT`, so the replay published the
   **detector** rectangle while the live product published the hugged one. Every "0 diff"
   box-stability replay was measuring a rectangle the product does not emit. Now pinned to `"2"`,
   matching `run_orion.local.ps1:575`.
2. `_png_for()` matched only the retired `f%05d_?_raw.png` naming, so every Sept-17/18
   press-window dump (`ep7_f000123_0_raw.jpg`) loaded as *"no decodable frames"*. Now accepts
   both namings.

Its per-frame record also carries `det_box` alongside `bbox` so the two rulers can be compared
offline from a replay, not only from a live `detframes` file.

**A full replay run was NOT completed in this session.** With both repairs in place the harness
loads `session_20260918_162245` (3 415 frames), but building its decoded-frame cache costs a
9.4 GB memmap on `D:` and the replay pass was still running after ~35 minutes, so it was stopped
and its scratch removed. The offline evidence in §1.2 / §3.3 therefore comes from the live
`detframes_*.csv` corpus — the shipped reader's actual per-frame output on real sessions, which
is stronger evidence than a re-run anyway — plus the engine unit tests in §3.1/§3.2. Anyone
re-running the replay should use `--windows-only` (press windows only) to keep the cache small.

---

## 6. Tests

```
native:  C:/Users/aaron/obld_p (throwaway VS2022 build, deleted afterwards)
         OrionNativeTests.exe                       1177 passed, 1 failed (pre-existing), 9 skipped
         + 3 new: ownershipProofIgnoresTheDisplayHugFlap
                  ownershipProofStillBreaksOnADetectorShapeChange
                  ownershipProofWithoutADetectorBoxKeepsTheDrawnBoxRuler
python:  tests/test_ownership_proof_detector_box.py (new, 5)
         tests/test_ship_defaults.py, test_event_driven_meter_emission.py,
         test_frame_integrity_pipeline.py, test_simple_reader_tight_box_clamp.py,
         test_simple_reader_detfill_latch.py, test_detframes_schema.py,
         test_detcsv_authority.py, test_idle_publish_gate.py,
         test_orchestrator_feed_gate.py, test_meter_update_idempotence.py   174 passed
         tests/test_simple_meter_reader.py, test_reader_fresh_onset_lock.py,
         test_reader_missing_measurement.py, test_meter_locator_cv.py       338 passed
offline: pill_locator_replay --cls neg   113 negative frames, 0 proposals
```

Nothing was committed, deployed, or written to `settings.json` / `learning.json`.
