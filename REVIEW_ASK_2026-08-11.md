# Review Ask — adversarial review of today's four commits

Not another repo sweep. **Review my own work**, on this branch:

```
b29371c  detector: 0.0-epoch velocity guard
d2ce0fe  IPC client-thread + HANDLE leak, sidecar QProcess leak
85f0e20  #47 capture route revocation retryable (SHIP BLOCKER)
e1f1e2b  grading: smoothly-translating settled marker (default OFF)
```

**Why this and not more codebase:** while implementing `85f0e20` I introduced two real bugs and
only caught them because existing tests failed — (1) I let a poisoned route acquire a scope, which
made revocation launderable via `_rekey_latency_route`, and (2) I removed an early-out that was
silently providing per-frame idempotence, so a persistently bad route re-replaced the latency
authority on every frame. Two self-inflicted bugs in one change to the timing-authority path is a
strong prior that there is a third one the tests don't cover.

Read `BUG_AUDIT_BRIEF.md` §2 (safety) and §3 (verification traps) first — they still apply.

## Specific questions, highest value first

### 85f0e20 (#47) — the ship blocker
1. **Is there any path where `_capture_route_currently_invalid` is left stale?** It is set in the
   mismatch branch and cleared in the `matches` branch of `_guard_capture_latency_route`. Is that
   guard the *only* writer, and can it be bypassed — early return, exception, a caller that skips
   it entirely (`frame_data=None` paths), or a second orchestrator instance?
2. **Recovery re-keys to a real scope with `restore_cache=False`, which means the estimator gets a
   cache path and can PERSIST.** Is that safe? Specifically: can a posterior learned during a
   window where the route silently flipped again get written into the configured route's scope
   before the next guard call notices? What actually triggers persistence — teardown, or a
   completed controlled posterior?
3. **Thread safety.** The two flags are plain bools read/written from the capture thread and read
   from attestation paths under `_latency_route_lock`. The mismatch branch deliberately publishes
   before taking the lock. Is the new second flag correctly ordered with respect to that, or did I
   create a window where suppression is visible but the authority reset is not?
4. Does the idempotence guard (`newly_revoked or route_changed or mode_changed`) miss a transition
   that genuinely needs an authority reset?

### d2ce0fe — leaks
5. `handleClient` is now wrapped in a lambda that sets a completion flag. **If `handleClient`
   throws, the flag is never set** and that entry is never reaped. Does it throw? (`std::terminate`
   would fire anyway, so this may be moot — confirm.)
6. The new `kMaxLiveClients = 32` cap closes the socket and `continue`s. Is closing inside the
   `clientThreadsMutex_` lock a problem, and is 32 above any legitimate concurrent-client count?
7. `proc->deleteLater()` at the end of the `QProcess::finished` handler: is there any path where
   the same QProcess is already deleted or scheduled — particularly the deliberate-teardown
   siblings racing an unsolicited exit?

### e1f1e2b — grading (default OFF, so lower stakes)
8. With `meterSettleAllowSmoothMotion` **ON**, does the motion path admit anything the
   fill-tolerance tail should reject? I claim the tail is the real settle-vs-mid-flight
   discriminator. Try to break that claim.

## Rules
- Do NOT fix anything. Report only.
- Separate CONFIRMED (proved it) from PLAUSIBLE (read it).
- **If you think one of these changes is wrong, say so bluntly.** I have been wrong twice today
  already — I misread the −57 ms cluster as a timing offset when it was a `Δfill × 5.5` artifact,
  and I miscounted 4 aborts as 76 by substring-matching `aborted=0`. Do not assume the committed
  version is correct because it is committed.
- Explicitly list what you checked and found clean.
