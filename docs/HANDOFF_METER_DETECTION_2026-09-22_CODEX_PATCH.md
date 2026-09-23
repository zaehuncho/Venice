# simple_meter_reader.py: Codex fail-closed patch integrated (2026-09-22)

For Astra. Your file changed; this is what changed and why.

- **Source:** Codex's meter-detection red team (`%TEMP%\vv-meter-attack-20260922\evidence\transaction\`).
  Diff `DIFF_FILE.patch`, rollback `ROLLBACK.sh`. It was applied on the owner's word after Claude's
  independent review.
- **Baseline:** the reader sha256 was `64103D10…425507` (unchanged since 09-21 14:38), so the patch
  applied cleanly. The result is byte-identical to Codex's `MODIFIED_FILE.zip` (`31255cb3f554d96c…`).
- **What changed:**
  1. **Locator exception.** Before: the previous box was reused and published. Now the patch restores
     `_det_seen_dts`, calls `_det_reset_lock_state()`, clears the warm box and structure proof, and
     revokes `_gameplay_lock_authorized`. It publishes `rejection_reason=detector_locator_exception`
     without calling `read()`.
  2. **Fill exception.** Before: a `pass` fell through to the colour fallback. Now the patch clears
     the proof, restores the census latch, resets the lock, and publishes `detector_fill_exception`.
  3. **Counters.** Adds `locator_exception`, `fill_exception` and `health_exception`, shown as
     `detfault=` in DETECTOR HEALTH. The health log itself is wrapped in try/except.
- **Tests:** `tests/test_meter_detector_exception_fail_closed.py` (13) passes, and all reader tests
  pass (832 passed, 2 skipped).
- **Watch live:** `detfault=` must stay 0 during shots. Before this patch the exceptions were silent,
  so their real rate is unknown. A non-zero count during shots is a bug to chase. With the patch,
  such a shot is dropped where it used to be guessed.
- **Not fixed (Codex's launch block still stands):**
  - The Left Fade anchor-misplacement hypothesis is unconfirmed.
  - The one-frame fill-error geometry gate was not shipped.
  - Controlled online Arrow2 trials are still owed.
- **The sidecar must be rebuilt before any packaging.**
