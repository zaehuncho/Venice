# Review Result — adversarial review of today's four commits

**Date:** 2026-08-11 · **Branch:** `fix/timing-input-and-remoteplay-blockers` (HEAD `b29371c`)
**Scope:** `b29371c` (0.0-epoch guard), `d2ce0fe` (IPC + QProcess leaks), `85f0e20` (#47 ship blocker), `e1f1e2b` (grading, default-OFF). Read-only; nothing modified or launched. Brief §2/§3 observed. Each commit reviewed adversarially by its own agent from the real `git show` diff, told not to trust "committed."

---

## Verdict up front

| Commit | Verdict |
|---|---|
| **`85f0e20` (#47)** | **Your prior was right — there IS a third bug the tests don't cover.** [MEDIUM] A geometry-only re-invalidation after poison+recovery leaves a **scoped, un-fenced** estimator that can persist a contaminated posterior into the trusted on-disk scope. Q1/Q2/Q3 otherwise clean or fail-closed. |
| **`d2ce0fe` (leaks)** | **Substantially correct.** No double-free, memory ordering right, reaper joins before erase. Q5 moot (terminate), Q6/Q7 safe. Three tracker-level notes (incomplete DoS closure, 2× thread misnomer, test doesn't exercise the fix). |
| **`e1f1e2b` (grading)** | **Your load-bearing claim is FALSE — bluntly.** The fill-tolerance tail is a *rate* gate (~0.8 pp/frame), not a settle discriminator; a slow rise threads it. LOW today (default-OFF **and** compile-time-only), but a hard blocker on any future decision to flip the default. |
| **`b29371c` (epoch guard)** | **Correct. NOT A BUG.** Predicate right, no `float(None)` path, strictly removes a worse failure mode. |

Two of the four are clean-to-ship; two carry a real hole — one latent-durable (#47), one gated-off (grading). Neither blocks the EA build as it stands, but the #47 one is worth closing because its damage is **persistent** (on-disk), and the grading one must gate any future flip.

---

## `85f0e20` — #47 route revocation retryable

### [MEDIUM · CONFIRMED-as-gap, PLAUSIBLE-as-exploit] THE THIRD BUG — geometry-only re-invalidation keeps a scoped, un-fenced estimator that can poison the persisted scope
`remote_play_orchestrator.py:3205-3222` (mismatch branch) × `:3145-3147` (recovery re-scope) × `:5031-5033` (training feed)

**WHAT.** #47's whole point is that recovery re-scopes the estimator to the *configured* route with `restore_cache=False` — which re-opens a live `_cache_path` (pre-#47, a poisoned process was unscoped for life, so persistence was impossible). The idempotence guard that decides whether to re-fence on a later invalidation is `if newly_revoked or route_changed or mode_changed:` (`:3211`). A `matches==False` caused by **geometry alone** — the frame's ndarray shape disagrees with its own stamped mode, while `api=='DSHOW'`, `index==expected`, and the stamped mode still read configured — makes all three conditions False *once the session is already poisoned+recovered*. So the guard flips `_capture_route_currently_invalid=True` but **skips `_replace_latency_authority`**. The estimator keeps the configured scope, is never fenced, and `update()` (gated only on `estimator is not None`, **not** on the invalid flag) keeps feeding it wrong-route pixels.

**TRIGGER.** Poison via an MSMF blip → recover to configured DSHOW (estimator now scoped) → a torn/reopen frame (or a stamp lagging the buffer) whose shape disagrees with its stamped mode while api/index/mode still read configured.

**IMPACT.** Live telemetry stays fail-closed (consumers see `invalid` ⇒ generation 0), and the in-memory contamination is discarded on the next recovery — so the *only* durable harm, but a real one: if a controlled+validated label forms during that window, `_persist_cache()` (`latency_estimator.py:2297`) writes a **contaminated posterior into the configured route's DPAPI cache**, which a **future process restores at startup**. That is a persistent, cross-session poisoning of the trusted scope.

**Why the tests miss it.** The new `test_remote_play_frame_pipe.py` cases exercise an **API-based** (MSMF) mismatch, where the *first* mismatch is `newly_revoked`/`route_changed` and DOES reset to the unscoped estimator (the tests assert `scope == ""`). The **scoped-retained, geometry-only** case is untested.

**CONFIDENCE.** PLAUSIBLE (full path traced; no live repro). Low probability in practice (bad-geometry frames rarely yield an accepted controlled label), but the guard is genuinely **incomplete**, not benign. **Is the committed change wrong?** — the guard is *incomplete*: `_capture_route_currently_invalid=True` should also force the estimator unscoped/fenced, or `update()`/persist should honor the invalid flag. Fix direction (report-only): gate `update()`/persist on `not _capture_route_currently_invalid`, OR add a geometry-delta term to the idempotence condition so a geometry-only re-invalidation resets authority.

### Answers
- **Q1 (stale retryable flag): essentially CLEAN.** Sole writer is the guard (init `:852`, clear `:3140`, set `:3206`), under `_capture_latency_route_lock`. Every consumer also gates on `not _capture_warm_cache_verified`, which only a real stamped frame through the guard can set True; the guard runs per-frame before pixels enter detection and again per telemetry emission. Exceptions between mismatch-detect and flag-set fail **closed** (flags set before the reset). One theoretical edge: a raise from `_latency_route_scope()` at `:3146` leaves the session stuck at generation 0 — fail-closed, LOW. Second-instance N/A (one orchestrator per sidecar).
- **Q2 (persistence safety): the intended re-key is SAFE; the one real hole is the finding above.** Poisoning permanently forces `restore_cache=False` in depth (`_rekey :2006`, `_replace :2912-2913`); `_capture_warm_cache_revoked` is set-only, never cleared → the poisoned posterior can never be *reloaded* in-process. For an api/index/mode flip, the first flipped frame swaps the estimator to unscoped `''` **before** those pixels reach detection (`:3838` runs ahead of `:3846`) → no persist. Only the geometry-only window (Finding) escapes. Residual pre-existing trust: the frame stamp must faithfully reflect the actual capture API — if the backend lies, bad pixels train the scoped estimator regardless.
- **Q3 (thread safety): correctly ordered, no torn-read emission (PLAUSIBLE).** Mismatch publishes `invalid=True`/`verified=False` before `_replace_latency_authority` takes `_latency_route_lock` and clears the attestation generation. Consumers combine `invalid OR not verified`, so partial visibility fails **closed** both ways. `attest_controller_latency_route` doesn't take the guard lock, but `_replace` subsequently wipes the stored generation under `_latency_route_lock` → no non-zero generation survives on a bad route.
- **Q4 (idempotence guard): it MISSES one transition** — the geometry-only re-invalidation above. Every other invalid transition moves the `(api,index,mode)` tuple (`route_changed`) or is the first mismatch (`newly_revoked`), so it resets. The geometry-only case is the gap.

---

## `d2ce0fe` — IPC client-thread leak + sidecar QProcess leak

**Substantially correct — the reviewer actively tried to break it and couldn't.**

- **Q5 (lambda throw → flag never set → leak reintroduced?): MOOT.** `handleClient` can throw unguarded (notably `std::thread writer(...)` throwing `system_error` under the very resource pressure the cap addresses), but an exception escaping the thread's top-level callable hits **`std::terminate`** — the service dies, so no live process ever holds an un-reaped-because-flag-unset entry. The pre-fix code had the identical property; the lambda changes nothing here. (Tracker note: throw→terminate is a latent crash-DoS, pre-existing, out of scope.)
- **Q6 (cap): sound.** (a) `closesocket`/`join` under `clientThreadsMutex_` is a syscall-under-lock inefficiency, not a deadlock — the reaper only joins already-returned handlers (which hold no other lock), and `accept` is outside the lock. (b) 32 is comfortably above legitimate concurrency — the only client is the single-socket in-process VeniceNet DLL; a legit burst can't reach 32. (c) Nothing leaks on a cap-refusal — the size check precedes all allocation; only the accepted `conn` exists and it's `closesocket`'d.
- **Q7 (QProcess `deleteLater` double-delete/race): SAFE.** `sidecarProcess_` is nulled before `deleteLater()`. Even the genuinely-double-scheduled path — `stopSidecar` → `waitForFinished` emits `finished` synchronously on Windows → the lambda calls `deleteLater()`, then `stopSidecar` calls it again — is safe: neither deferred-delete event is processed until control returns to the event loop, the first deletes the object, and `~QObject` purges the still-pending second. `FailedToStart` doesn't emit `finished`, so no double there.

### Findings (tracker-level, not blockers)
- **[MEDIUM] Incomplete DoS closure.** The cap bounds the leak but threads spawn *before* auth, so an unprivileged process can hold all 32 slots pre-auth and lock out the real app (self-healing ~1 s after it disconnects). Much smaller than the pre-fix unbounded LocalSystem growth — a genuine improvement, but not a full close.
- **[LOW] Cap under-counts by 2×.** Each handler spawns an uncounted writer thread → 32 clients ≈ 64 threads/HANDLEs. Still bounded; the constant just understates the ceiling.
- **[LOW] The added test doesn't exercise the fix.** `testFinishedThreadStaysJoinableUntilJoined` pins only the language premise (a finished `std::thread` stays `joinable()`); it never touches `acceptLoop`, the reaper, the completion flag, or the cap. The fix ships with **zero direct test coverage** (the commit message concedes this).

**Clean:** completion flag is a real `shared_ptr<atomic<bool>>` with acquire/release pairing (no data race); reaper joins before erase; cap-check↔spawn atomic under one lock; `stop()` joins all threads outside the lock after `acceptThread_.join()`.

---

## `e1f1e2b` — grading (default-OFF)

### [LOW live / HIGH-if-armed · CONFIRMED] The "fill-tolerance tail is the real settle discriminator" claim is BROKEN
`native_orion/src/AutomationEngine.cpp:12834-12866`

The tail is not a "fill frozen" test — it's **band-membership vs the last fill**, which makes it a **fill-*rate* gate at ~0.8 pp/frame** (`runFillTol/(runNeed-1) = 4.0/5`). It checks no per-frame delta, monotonicity, variance, or total drift. Concrete counterexample, traced through float values: a marker translating at a constant 20 px/frame with fill rising a constant 0.7 pp/frame (`90.0 → 94.9` over 8 frames) passes the motion walk (`isSmooth=true`, `usedMotion=true`) **and** threads the tail as a 6-frame "settled" run — while it is still climbing the entire time. The same hole absorbs a decelerating overshoot bounce (up to 4 pp, anchored to the last fill), in-band oscillation (`94,98,94,98,94,96`), and a plateau-then-resume.

**Why it matters despite default-OFF:** the false settle doesn't early-release the current shot (both consumers run on post-release samples), but it poisons the **learner** — the recorded `settled_fill` for the counterexample is ~93 vs a true ~99, biased ~6 pp low, which is indistinguishable from a genuine EARLY landing and drifts *future*-shot timing. On a real 2K meter the decelerating settle knee passes through this sub-0.8 pp/frame band every shot.

**Severity is capped two ways:** the flag is default-OFF, **and** it (like the whole `meterSettle*` family) is **compile-time-only** — no AppConfig load/UI/env, so a customer cannot arm it on a shipped build (this is the established pattern for the subsystem, matching your "flip = recompile" note; it is *not* an unreachable-flag bug per brief §4). So field blast radius today is zero. But the hole is real in the code and is a **hard blocker on any future decision to flip the default.**

**Tests are the blind spot:** the 7 new cases only reject a *steep* rise (5 pp/frame collapses the tail in one step); none exercises the `0 < rate ≤ 0.8 pp/frame` band where mid-flight actually threads. The suite proves the strong gates (travel, accel, frame-count) and the easy rejection — never the discriminator at the boundary it has to defend.

**Clean:** default-OFF byte-identity, `usedMotion` isolation, the travel/accel/frame-count gates, and no new epoch/lifetime/thread hazard all verified.

---

## `b29371c` — 0.0-epoch guard — NOT A BUG (correct)

`_detect_ts = (_frame_wall_ms / 1000.0) if _frame_wall_ms > 0.0 else None` — the fail-closed sentinel is exactly `0.0`, correctly rejected; NaN and negatives also route to `None` (the fail-safe fallback). No `float(None)` path (`read()` resolves `None → perf_counter()` before any `float(ts)`). It strictly **removes** the worse old failure modes (the all-`0.0` window pinning velocity to 0, and the `0.0`-vs-`1.7e9` slope blow-up). Steady state carries a real epoch → shipping path byte-identical, as claimed.

---

## Bottom line
You caught two self-inflicted bugs in #47 via failing tests; there is a **third one the tests don't reach** — the geometry-only re-invalidation that keeps a scoped estimator alive and can poison the on-disk cache across sessions. It's the only durable-harm finding in the set. The grading claim is genuinely broken but locked behind a default-OFF, recompile-only flag, so it's a note-to-self for whenever you consider arming it. The leak fixes and the epoch guard are good to ship.

*Full per-commit evidence trails: `.audit_raw/REVIEW_85f0e20_47.md`, `REVIEW_d2ce0fe_leaks.md`, `REVIEW_e1f1e2b_grading.md`.*
