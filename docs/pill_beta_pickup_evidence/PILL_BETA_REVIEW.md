# Pill beta handoff review — 2026-09-22 local / 2026-09-23 UTC

**Decision: needs changes for release approval; keep the owner's Pill (beta) state pending the planned live measurement.** The pre-registered 60% threshold was met exactly on only 20 open standstills, not the planned approximately 50. This is not a measured parity result against Arrow2.

## Reproduced live facts

- `D:\NexusVision\shot_records\session_20260922_202346.jsonl` has 34 records, epochs 102–135: Standstill 12 EXCELLENT / 2 EARLY / 5 LATE / 1 no-banner (20 total); Right Fade 6 / 1 / 0 / 2 (9); Left Fade 0 / 2 / 0 / 1 (3); Go-To 2 unanswered. All 34 `pickup` and `framedump` fields are null.
- `logs/orion_native.log` records `METER STYLE: Pill -> proposer=yolo` at 01:23:46.239Z and a CUDA detector load. Between that line and the next Pill route line at 01:44:17.437Z there are zero `PICKUP:` lines and zero ruler `cap_ok`/latch diagnostics. The previous CV route did emit `PICKUP:`. The missing Pill pickup was structural: the reader called `open_pickup_record` only on a proposer that provided it, while YOLO did not.
- C: free space was 3,832,020,992 bytes at review, below the handoff's 4.65 GB framedump floor. The session's framedump directory is absent. D: first-write guard failure is reported in the handoff but was not re-tested here. Thus this session does **not** measure ruler latch, carry between presses, `cap_ok=0`, fallback frequency, or fill-noise on actual Pill frames. `green_end=96` on a release line is not a substitute for per-frame `cap_ok`.
- The `_measure_fill_in_box` Pill branch matches commit `0eca7d2` exactly in a block-for-block comparison, and `pill_fill_ruler.py` is clean at SHA-256 `A716BFED1A46128E159DE6EA9333BB3D62E9191942076C11FFBF71596ED1D966`. The **shared** reader is not byte-identical to 09-19: green-cap choice, static-zone quarantine, and detector-fault handling changed; these can affect Pill indirectly. The live result cannot be attributed to the unchanged ruler alone.

## Pickup repair in this review

`simple_meter_reader.py` now opens an epoch-keyed pickup record on the Pill YOLO base, flushes exactly one `PICKUP:` miss/hit line per closed press, and lets the orchestrator's existing epoch join place that record in `shot_records`. It populates `first_sight_fill` and `first_sight_ms_after_press` on the first non-rejected, positive Pill fill measured on the frame clock; stale/pre-press, other-epoch, and post-release frames are ignored. It works with `ORION_PLAYER_ANCHOR=0` and `ORION_CV_TIPLESS_ARMED=0`; CV's existing pickup path is unchanged. A press closed before its first frame still produces a miss line.

**Metric distinction:** Pill's new `first_sight_*` is the first **pixel-valid reader fill**, not the raw first YOLO box proposal or a fully accepted shot. It can lag the proposal and can precede a later ownership/static-zone veto. It must not be interpreted as detector-only latency or directly compared with CV's first accepted locator sight. A `-1.0` miss means no pixel-valid fill before close, not necessarily no YOLO box. This patch changes forensics, not shot acceptance, ruler state, or fire timing.

## Remaining decision gates

1. Obtain the planned approximately 50 open standstills and approximately 10 fades with a Pill framedump after C: has room or D: meets its first-write guard. Check release/misfire census as well as banner outcomes; do not promote the narrow 12/20 threshold pass to Arrow2 consistency. Left Fade 0/3 is a signal to stratify, not a rate estimate.
2. In that session, reconcile physical shot epochs with `PICKUP:` epochs and JSONL pickup objects. Separate `-1` (no valid fill) from positive first-valid-fill latency; use detector health and frame evidence to distinguish late YOLO proposals from failed fill reads.
3. Replay or log per-frame `_dbg_pill_ruler` by epoch: `latched`, `span`, `hist_n`, `n_out`, `n_relatch`, `cap_ok`, and box-vs-landmark fill. Confirm carry across physical presses and count box-ruler fallbacks. Re-estimate fill noise on those **live** frames before changing scale/latch constants or Shot Lead. The older offline 19.3% live-box `cap_ok=0` figure remains a hypothesis for this run, not an observation.
4. Keep Pill labeled beta until these gates pass. The source-level pickup repair is tested; no app build, launch, or live session was performed here.

Reproduction, hashes, rollback, and test results: `VERIFICATION.txt` in this directory.
