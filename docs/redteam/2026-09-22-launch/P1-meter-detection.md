# P1 — Meter detection: reliability review (Claude, internal wave 2026-09-22)

Scope: `simple_meter_reader.py` (Astra's file; reported only), `meter_locator_cv.py`, `player_anchor.py` (the locator's window table), `native_orion/backend/autogreen_sidecar.py`, and the path from reader to native ownership.
Read-only. No source, settings or git changes. Nothing was built or launched.

**Evidence base**
- `logs/orion_native.log.1` covers 2026-09-21T18:56Z to 2026-09-22T22:31Z: 572 physical presses and 1,187 DETECTOR HEALTH lines. `logs/orion_native.log` holds the tail.
- `D:\NexusVision\shot_records\session_202609{19..22}_*.jsonl` holds 1,793 records (1,608 on schema 2).
- Parsing scripts are in the agent scratchpad only. No repo writes.

**Already known; not re-reported:**
- The 09-21 report `docs/redteam/2026-09-21/C-meter-detection-sidecar.md`: F3 (No-Meter epoch join), F6 (CV self-arm), F8 (`native_ownership=unconfirmed` literal), F9 (300-char relay trim) and F10.
- The 09-22 Codex fail-closed patch handoff.

F9 matters here: it is the reason finding 001 cannot be closed from logs alone.

---

### [CL2-P1-001] high — Go-To (stick) meters render about 2 s after the stick edge, but the reader and locator press windows assume under 0.7 to 1.6 s; the 09-22 epoch-10 shot was lost this way

- **Lane:** P1

- **Failure path:**
  1. **Measured timing of a 2K27 stick Go-To.** Every Go-To that was released in the log shows the same pattern:
     - locator first sight at 1,933 / 1,950 / 1,950 ms after `stick_up_edge`;
     - native onset (`ONSET FF onset_ms`) at 1,923 / 1,987 / 1,992 ms;
     - `PRESS-TIP OBSERVATION press_to_tip_ms` at 2,114 / 2,190 / 2,213 / 2,244 / 2,257 ms.
     - The median record onset for stick Go-To is 850 ms (n=52). 60 % of those onsets fall outside the Go-To expectation window.
  2. **The per-press windows that decide what the locator and reader accept were tuned on Square and 2K26 timing:**
     - `player_anchor.py:346-372` gives `goto` 320 ms, so the expectation window is **220–720 ms**. Standstill is 150 ms, so its window is 50–550 ms, against a live median of 501 ms. The table predates the 2.1× slower 2K27 meter. `tests/test_player_anchor.py:168-173` pins these constants, so the fixture passes while the live data contradicts it.
       - Outside that window, `meter_locator_cv.py:1165` (`_expectation_ok`) refuses sub-floor candidates.
       - `meter_locator_cv.py:193` also closes the tipless path.
     - The ghost-press guard lasts **1.6 s** (`simple_meter_reader.py:2022`, `ORION_READER_GHOST_PRESS_WINDOW_S`). That always expires before a Go-To meter exists. Of the three bridged Go-Tos:
       - epoch 42 ended `window_end`;
       - 09-22 epoch 10 ended `window_end`;
       - 09-22 epoch 18 stood down falsely on `real_onset_low` at +296 ms, while its real meter came at +1,956 ms.
     - The locator's arm window is **2.5 s** (`meter_locator_cv.py:653`, `ORION_ANCHOR_ARM_S`). Its docstring (`:637`) claims "a meter's whole life is under 1.5 s", which is false for Go-To: the tip is already at 2.1–2.26 s. After 2.5 s the locator treats the press as unarmed. Anchor, relaxed floors and tipless all switch off in the middle of a late Go-To's rise.
  3. **Real case: session_20260922_164912 epoch 10, `logs/orion_native.log.1:28181-28203`.**
     - **Press:** `stick_up_edge` at 21:50:03.183Z, 2.25 s after the previous release. The leftover 96.2 % lock was correctly dropped (`STALE LOCK DROPPED AT PRESS`). Its identity was retired 60 ms later (`GHOST IDENTITY RETIRED … full_nofinds=2`).
     - **The meter was real.** It rendered roughly 1.7–2.2 s after the press. The player released it himself and the game graded it LATE: `BANNER VERDICT UNATTRIBUTED: timing=LATE … (no bot release behind this…` at 21:50:06.801Z.
     - **The locator barely saw it.** Between 21:50:04.686 and 21:50:06.684 the DETECTOR HEALTH `found=` counter moved 1089 → 1097, i.e. **8 finds in 120 calls**. During the previous Square shot it moved 795 → 883, i.e. 88 finds.
     - **The reader first accepted a sample at 77.4 % fill, 2,320 ms after the press** (record `onset_ms=2319.56 onset_fill=77.36 onset_structure_verified=false`). That is above the engine's 40 % first-fill bound, so it could never be owned.
     - **Result:** `STRUCTURE NECROPSY epoch=10 (next_press): never latched … qual_det=14 qual_green=0 max_fresh_up=2 authorized_frames=0` and `outcome=unanswered closed=timeout`.
  4. **Epoch 11 has the same signature without any stale lock** (`:28198-28286`): locator first sight at 90 % fill, 2,300 ms after the press; record onset 2,317 ms at 78.3 %; unanswered. That rules out the stale-drop and ghost path as the sole cause.
     - **Anchor counters in epoch 11:** during the window, `anchor_conf_hi` rose by 121 while `anchor_patch_hit` rose by 8 and `refused_outside` stayed 0. So neither the patch nor the band scan found the rising column.
     - **Unexplained coincidence:** both epochs 10 and 11 reach first acceptance at about 2,317–2,320 ms at 77–78 %.

- **Affected:** `player_anchor.py:346-372`, `meter_locator_cv.py:637-655` and `:1142-1181`, `simple_meter_reader.py:2022`, and the Go-To ownership path behind them (native `pendingMeterOwnershipCandidate`, whose cap of ≥ 6 s is fine).

- **Impact:** Go-To shots are silently unowned. The press is passed through and the player's own timing decides the shot. Epoch 10 was a banner-proven real shot lost LATE.
  - **Occurrence:** 1 of 6 banner-proven stick Go-Tos in the log (09-21 epochs 11/12/13/42 and 09-22 epoch 18 were owned). Across 09-19..09-22 records, `stick_up_edge` Go-To has 17 released vs 44 unanswered. Not every unanswered stick edge was a shot.
  - Records show 5 more stick Go-Tos first seen at ≥ 77 % fill (135347 ep98/127, 143409 ep24, 164912 ep10/11).
  - Square presses show the same late-drawn class at a lower rate: 8 of 1,562 disarmed `ownership_proof_incomplete` / `stamp_missing`, first sight at 54–90 % fill, 684–1,284 ms after the press.
  - Fails closed: no wrong input is sent.

- **Reproduction (high level):**
  1. In a Go-To session, press and hold right-stick-up about 2.2 s after a previous released shot, and repeat without a leftover meter.
  2. Look for `STRUCTURE NECROPSY … (next_press)` and a `found=` delta under 10 per 2 s while the meter is on screen.
  3. Look for an `unanswered` record whose `onset_fill` is above 40.

- **Required fix:**
  1. Make the per-type press windows 2K27-measured and data-driven. Go-To onset is about 1,950 ms: use a window of at least ~1,400–2,700 ms, an anchor arm of at least 3.2 s for Go-To, and a ghost-press window per shot type (≥ 2.6 s for Go-To). Standstill onset is about 500 ms, not 150. Re-derive the table from `ONSET FF onset_ms` per bucket.
  2. Enable an automatic framedump for any hardware epoch that closes with `STRUCTURE NECROPSY`, or with `outcome=unanswered` and `onset_fill > 40`. The scan census that would explain where the rise went (`scan_full_hit/scan_full…`) sits past the 300-char relay trim (known F9). Epochs 10 and 11 cannot be root-caused without frames.
  3. Update `tests/test_player_anchor.py` to pin the measured windows, not the 2K26 ones.

- **Verification test:**
  1. Replay the dumped frames of an epoch-10-style press. The locator must propose the column below 30 % fill, and the reader must latch structure before 40 %.
  2. Unit test: `onset_window_ms("Go-To")` must contain [1,900, 2,300]. The locator's `_press_state` must stay armed at 2.8 s for Go-To.
  3. Live: 20 stick Go-Tos, including 5 taken 2–3 s after a previous shot, must produce 0 `unanswered` records with a banner.

- **Confidence:** the loss is **confirmed** (banner-proven). The window mismatch is **confirmed** (code plus measured timing). That the windows *caused* epochs 10 and 11 is only **probable**: epoch 11 shows the locator finding nothing in patch or band even while armed. See the reproduction above.

---

### [CL2-P1-002] medium — Shot-record forensics are poisoned on bridged presses: the leftover meter is logged as this press's first sight and onset

- **Lane:** P1

- **Failure path:**
  - **Locator pickup record.** `meter_locator_cv.py:98-109` (`_note_pickup`) records the first accepted sight after an armed press. On a bridged press that first sight is the previous shot's meter at dt 0 or a few frames later. Nothing excludes sightings made before the reader's stale/ghost guard stands down.
  - **Orchestrator onset hook.** `remote_play_orchestrator.py:2439-2456` (`_shot_record_frame_hook`) records `onset_ms` at the first result with `detected and gameplay_sample_epoch == epoch`. The reader stamps the new epoch on leftover frames it publishes before eviction, so the leftover wins.

- **Measured, 09-19..09-22:**
  - **586 of 1,587** pickup records (37 %) have `first_sight_ms_after_press=0.0` at 90–104 % fill; 281 of them are from 09-22 alone.
  - **97 of 583** bridged released records have `onset_ms < 300`, against 18 of 711 clean records. 21 have `onset_fill > 40`. **89** of those were tagged `tempo_estimate=quick`.
  - Go-To epochs 42 and 18 record onset at about 255 ms, while the engine's own onset was 1,992 and 1,923 ms.

- **Native is NOT affected.** All 536 `Ownership acquisition census` lines show `initial_high_samples=0 disposition=promoted`, and the engine's `ONSET FF onset_ms` values are correct.

- **Affected:** `shot_records.py:540-578` (`note_onset`), `remote_play_orchestrator.py:2425-2456`, `meter_locator_cv.py:98-109`. The consumers are `tools/timing/onset_ff_grade.py:105` (reads the record's `onset_ms`), `tools/diagnostics/anchor_acquire_study.py`, and every pickup or onset study run on rapid-drill sessions.

- **Impact:** the owner's timing decisions rest on the onset lever and the "pickup stall" class, both measured from these records. In rapid-drill sessions (56 % of presses are bridged: 319 of 572), roughly 1 in 6 bridged onsets is fabricated and more than a third of pickups are unusable. This can mis-set the onset feedforward gain and misclassify tempo in offline grading.

- **Reproduction (high level):** take any `session_20260922_165506.jsonl` record whose `pickup.first_sight_ms_after_press == 0`. Compare its `onset_ms` with the native `ONSET FF … physical_epoch=N onset_ms` for the same epoch.

- **Required fix:**
  1. Record the onset only from a sample that is structure-verified, or that has `fill ≤ 40` and an empty `rejection_reason`.
  2. Better: copy the engine's `ONSET FF onset_ms` into the record.
  3. In `_note_pickup`, skip sightings while the reader's press guard has not yet seen a low sighting (`_press_low_seen` is False).

- **Verification test:**
  1. Extend `tests/test_shot_records.py`: a leftover sample (fill 95, same epoch, unverified) followed by a real rise at 15 % must record `onset_fill ≈ 15`.
  2. On the next rapid drill, the median onset of bridged Standstills must be within 20 ms of the clean median, with no `first_sight_ms_after_press == 0` above 40 %.

- **Confidence:** confirmed.

---

### [CL2-P1-003] medium — Long range (30 ft and beyond) grades LATE at about 3× the mid-range rate; pickup is not the cause, and the range input the engine could use is dead

- **Lane:** P1 (detection-adjacent; the timing lane owns the aim)

- **Failure path.** Graded released shots by banner distance:
  - **22–26 ft (n = 829):** 18.6 % LATE, 69.8 % EXCELLENT.
  - **26–30 ft (n = 241):** 22.4 % LATE, 57.7 % EXCELLENT.
  - **≥ 30 ft (n = 26):** **50 % LATE, 31 % EXCELLENT.**
  - Detection is not the difference. The median first-sight fill is 14 % and the onset fill 18 % in every distance band, and the onset time is also unchanged (505 vs 503 ms).
  - The oracle shows the green window moves to the top at range: `green_bottom_pct` median 98.1 at ≥ 30 ft vs 95.3 nearer. The engine targets 100 % with no range input.
  - The range reader returned `range=unknown` on **1,608 of 1,608** schema-2 records (1,327 `uncalibrated`, 234 `no_samples`). The `range=` terms in `FADE WINDOW LEAD` and `BANNER TRIM` therefore never engage.

- **Affected:** the SHOT RANGE reader (`remote_play_orchestrator.py` `_shot_range_result`, the player-anchor "3" cells) and the engine aim. The fill reader is clean.

- **Impact:** every 30-ft-plus shot is roughly a coin flip. This is the population customers will notice on deep threes.

- **Reproduction (high level):** filter any 09-20..09-22 JSONL by `banner.distance_ft ≥ 30`.

- **Required fix:**
  1. Either calibrate or ship the range reader, or feed the per-shot measured `green_window_start_pct` (already on the reader result) into the aim.
  2. Until then, keep 30-ft-plus shots out of the headline make rate.
  3. The memory index already records "range classifier DEAD (09-19)". This finding quantifies the cost; it is not a new root cause.

- **Verification test:** 40 controlled 30–32 ft Standstills with the fix, compared on the same day with and without it. LATE must fall below 25 %.

- **Confidence:** the measurement is confirmed (n = 26; Fisher p < 0.001 against 22–26 ft). The cause is probable.

---

### [CL2-P1-004] low — The ghost guard stands down on a false "real onset low" before the real meter, and the reader latches box geometry on the wrong object

- **Lane:** P1

- **Failure path:** `simple_meter_reader.py:2022-2060` stands the guard down after two consecutive in-zone nonzero reads below the leftover's level band. A fading leftover, or a partial read, satisfies that rule.
  - 36 of 282 `real_onset_low` stand-downs (13 %) came more than 150 ms before the engine owned any meter.
  - 21 epochs show a `BOX LATCHED` under 0.35 s after the press, then a second latch later. For example, 09-22 epoch 18 latched generation 29 at +0.30 s and the real generation 30 at +1.92 s (`:28691-28760`).

- **Impact:** no measured verdict cost. Early stand-down gave 20.5 % LATE against 22.5 % for normal stand-down. It does remove the leftover-meter protection for the rest of the press, and it is the path by which Go-Tos (see 001) lose it.

- **Required fix:** require the escape reads to be rising (the second read greater than the first by at least 1 pp), not merely below the band.

- **Verification test:** an offline sequence of fading leftover reads (46 → 30 → 22 in zone) must not stand the guard down; a real rise (5 → 12 in zone) must.

- **Confidence:** confirmed for the behaviour; there is no evidence of shot loss.

---

## Checked and CLEAN (so nobody re-walks them)

- **Codex fail-closed patch (09-22).** DETECTOR HEALTH reads `detfault=0/0` on **1,187 of 1,187** lines, and there are 0 `CV processing error` lines.
  - A code read of `simple_meter_reader.py:13342-13384` and `:13689-13717` confirms two things. Locator and fill exceptions publish a rejected sample (`rejection_reason=detector_*_exception`) and revoke structure proof and authorization. An orchestrator-level exception goes to `_finish_processed_frame(False, 'detector_exception')` (`remote_play_orchestrator.py:8296-8303`).
  - One residual, speculative only: the lock reset is skipped when the epoch changes during the call, although a fault sample is still published.
- **Quick re-shot on Square: the stale-lock drop works.** It fired on 319 of 572 presses (56 %), every one within 2.5 s of the previous release.
  - Stale vs clean Standstill: median ownership age 552 vs 553 ms; LATE 19.8 % vs 18.6 %; median first fill 18.4 vs 17.6.
  - Presses within 2.5 s of the previous release: 369 of 370 released. The only failure was epoch 10 (a Go-To, finding 001).
  - The ghost guard ended at `release` or the real onset on 288 of 289 press summaries; `next_press` only on epoch 10.
  - No leftover under 40 % ever bridged a press (all 586 bridged first sights were at ≥ 50 %).
- **Leftover meters never reach native ownership.** The census shows `initial_high_samples=0` on all 536 lines.
- **Withholding layers are quiet:**
  - `press_fresh_withheld=0`, `idle_unpublished=0` and `reseed_refused=0` on all lines.
  - The static-zone quarantine withheld at most 15 reads per session and hit 5 press windows.
- **Unanswered `stick_down_edge` presses (157 across 09-19..09-22) are mostly not shots.** Only about 15 had any meter.
  - Native declines RS-down ownership unless stick input or rhythm is enabled (`AutomationEngine.cpp:5350-5366`), by design.
  - They are also not the Go-To path: `physicalGestureActiveForMode(GoToStick)` requires stick-up.
- **Go-To pending window in native:** at least 6 s (`AutomationEngine.h:291`, `.cpp:2096`). It is not the limiting factor in 001.

## Verdict

**needs changes**

It is not blocked because every failure found fails closed: the press is passed through and no wrong input is sent. But Go-To ownership is unreliable on 2K27 timing, and the forensics used to tune timing are contaminated.

**Top 5 fixes in priority order**
1. **CL2-P1-001:** re-derive the per-type press windows from live 2K27 onsets. Go-To about 1.4–2.7 s; anchor arm at least 3.2 s and ghost window at least 2.6 s for Go-To; Standstill about 500 ms. Update `tests/test_player_anchor.py`.
2. **CL2-P1-001:** auto-framedump every `STRUCTURE NECROPSY` or `unanswered` press with `onset_fill > 40`. Get the scan census out from behind the 300-char trim (known F9).
3. **CL2-P1-002:** take the record's `onset_ms` from the engine's `ONSET FF`, or require a structure-verified or ≤ 40 % sample. Stop `_note_pickup` from recording pre-guard leftover sightings. Re-run the onset-lever numbers on the cleaned data.
4. **CL2-P1-003:** give the aim a range signal: fix the range reader, or use the measured green-window start, for shots at 30 ft and beyond.
5. **CL2-P1-004:** require a rising pair for the ghost-zone escape.
