# Orion — Chiaki Fork Inject/Release-Path Optimization Plan

_Generated 2026-07-18. Source: 10-probe fable research fan-out (`wf_f65c6f36-6a8`) run **read-only against the shipping fork** `C:/Users/Administrator/Desktop/chiaki-ng-src` (branch `orion`) — NOT the stale `vendor/chiaki-orion` copies. Every claim below was verified against real functions/line numbers in that fork. All patches are surgical; the RTT-composition consumer (P9) lands in the NexusVision main repo._

---

## Context — why this exists

The fork is the **input/release leg** for the auto-green bot on the user's own PS5 over Remote Play. The bot decides "release NOW" → `orioninputbridge.cpp` injects synthetic controller state → `feedbacksender.c` builds the feedback packet → `gkcrypt.c` encrypts → UDP → PS5. Every millisecond **and every jitter-millisecond** on that inject→wire path widens the shot-timing error, and a **dropped/late release packet = a bricked shot**. Target: **per-shot-type release-time σ < the game's green-window half-width**; reliability of the critical release matters as much as mean latency.

The research found the fork's OrionStream low-latency layer (`1ea25990`), input coalesce (`25d6766f`), and the fixed silent send-drop (`9b8ab2b4`) already in place — so this plan is the **next layer**, and it centers on one verified structural regression the anti-stall work left behind.

### Do-not-break (every patch respects this)
No changes to PS5 pairing/handshake, Senkusha/MTU negotiation, or GKCrypt key derivation. **Zero wire-format changes** in the recommended set — the only wire-visible deltas are duplicate/extra feedback datagrams in shapes the console already receives (absolute-state events, 4-event replay window). Controller state must never desync.

---

## The headline verified findings (grounded)

1. **The release waits on an 8ms self-poll — quantized to ~15.6ms.** To kill an old per-write wake cascade (stick bursts stalling video send), the fork removed the sender wake for **all** injects (`feedbacksender.c:120-136`, no-signal `nowait` setter). The critical release now sits in `orion_pending_state` until the sender's next timed poll (`:328-342`), a **0–8ms uniform delay** — and because **nothing in the repo calls `timeBeginPeriod`** (verified), that wait is quantized by the default **15.6ms Windows tick**. Real pickup delay is uniform up to ~16ms: **mean ~4–8ms, σ ~2.3–4.6ms, every shot.** This is the single largest controllable client-side term.
2. **A confirmed live reliability bug:** `takion.c:767` **discards** `chiaki_takion_send_raw`'s return value (err left `SUCCESS` from the gmac at `:763`) → the shipped F2 peek-don't-pop guard (`9b8ab2b4`) is **dead code against socket failure.** One-line fix.
3. **`sendto` runs *inside* the crypto mutex** for the feedback path (`takion.c:748-770`), while the control path already unlocks first (`:594-601`) — so the release's wire send can stall behind congestion-path `gkcrypt_local_mutex` contention.
4. **The console's own RTT-to-PS5 is measured and thrown away:** `q.rtt` is `CHIAKI_LOGV`'d and discarded at `streamconnection.c:693-703`. Meanwhile `rtt_sync_engine.py` pings the **court server (wrong hop)** blind and injects its jitter-margin as lead — 2–6ms of noise straight into release σ.

---

## Leverage ranking (merge-aware)

| Rank | Item | Lev | Gain | Verified anchor / why |
|---|---|---|---|---|
| 1 | **P4** edge-wake + `timeBeginPeriod(1)` + thread priority | 9 | ~5ms mean **+σ** | Deletes the 0–~16ms slot-pickup lottery for the release edge; only item cutting both mean AND σ; lost-wake degrades to exactly today's poll |
| 2 | **P2** propagate swallowed `send()` err + new-seq history echo | 8 | reliability | `takion.c:767` one-liner revives the F2 guard; echo turns 1-lost-datagram=brick into recovery |
| 3 | **P7** Takion DATA-ACK RTT + per-packet wire timestamps → bot | 7 | observability | Only probe measuring the hop the release actually traverses (µs ack-RTT, Karn-filtered); feeds P9 |
| 4 | **P10** critical-release wire replication (immediate byte dupe) | 7 | reliability | Cheapest dupe (pure UDP duplication, no crypto lock); **merge into P2** |
| 5 | **P1** edge-triggered wake + one-shot echo | 7 | ~4ms | Correct diagnosis; it's **P4 minus** the timer-resolution + priority fixes → **fold part A into P4**, part B into P2 |
| 6 | **P8** move `sendto` out of `gkcrypt_local_mutex` + bounded WOULDBLOCK retry | 6 | ~0.5ms tail | Removes a real congestion-coupled release tail; subsumes P3's mutex-shortening half |
| 7 | **P5** per-release QPC inject→encrypt→wire telemetry | 6 | observability | Ground truth validating P4/P8; **share P7's pipe** (send-side half) |
| 8 | **P9** compose takion RTT-to-PS5, demote court-ping to delta-bias | 6 | ~2ms | Fixes wrong-hop modeling error; `q.rtt` discarded at `streamconnection.c:693` |
| 9 | **P3** GMAC ctx caching + heap-free keystream | 4 | ~0.1ms | Verified real per-packet EVP alloc+keyschedule, but win is µs-tail; **gate on measured need** |
| 10 | **P6** frame-export pooled readback | 3 | ~1ms preview | Removes a 3.1MB copy on the takion thread (`orionframeexport.cpp:135`) but serves the **secondary** preview path |

---

## Three merge groups (build these, not ten patches)

**A — LATENCY (P4 ⊇ P1-part-A):** identical edge-detection mechanism. One patch: in the `nowait` setter compare `buttons/l2_state/r2_state` against a producer-side `orion_pushed_prev` shadow; on an **edge** (button/trigger only — sticks stay silent, preserving the anti-stall design) call `chiaki_cond_signal`; extend `state_cond_check` with `orion_pending` (nested `orion_pending_mutex` under `state_mutex`, same order already used at `:361`). Add `timeBeginPeriod(1)` + power-throttling opt-out + `THREAD_PRIORITY_HIGHEST` on the feedback thread. Prefer P1's dedicated `orion_pushed_prev` shadow (self-evidently correct vs reusing the retained slot).

**B — RELIABILITY (P2 + P10 + P1-part-B):** one patch: (a) **P2's `takion.c:767` error-propagation one-liner — non-negotiable, ship first;** (b) P10's **immediate byte-identical dupe #1** for the critical-masked release (zero protocol novelty, no lock, no `key_pos` advance); (c) P2's **delayed echo via NEW-seq history re-flush** (~3–16ms later) — new seq_nums sidestep the unknowable seq16-dedupe firmware question and are byte-equivalent to stock trailing-4-event behavior. Cancel-on-newer-flush kept. Mask-gated: mask=0 is byte-for-byte stock.

**C — OBSERVABILITY (P5 + P7):** one telemetry pipe (one GUI exporter cloned from `OrionFrameExport`, one cb hook in `takion`): P5's staged inject/lock/precrypt/postsend records + P7's `first_send_us` ack-RTT records (add the `uint64` to the sendbuffer packet struct, Karn `tries==0` filter) as two record types on one ring. **P9 consumes it** on the NexusVision side; P7's ack-RTT is the cross-check P9 needs for `q.rtt`.

_P4 vs P8: no conflict — P4 fixes pickup cadence (mean+σ), P8 fixes the send-path tail under contention. They compose._

---

## Protocol / desync risk watch (ranked by threat)

1. **P3 (deferred) — highest:** a subtly wrong cached GMAC tag → PS5 **silently drops every feedback packet** = total input brick, no error. Guard: current-key-index-only fast path, dual-compute assert for the first 100 packets, any EVP failure invalidates cache → byte-identical legacy fallback (both OpenSSL + mbedtls arms). **Only build if telemetry shows a surviving crypto tail.**
2. **P10 delayed byte-identical echo:** reuses seq16 with unknown firmware dedupe semantics → a replay racing a newer press could flicker a button. Guard: **use P2's new-seq re-flush for the delayed copy** (removes the question); keep only the immediate back-to-back dupe (LAN reorder within a pair is vanishingly rare, events are absolute-state); mask-gated.
3. **P4/P1 edge wake:** risk is not protocol but **regression of the shot-correlated video stall** the no-wake design fixed. Guard: structural (only ≤2 edges/shot signal; each wake does work the poll would do ≤8ms later → aggregate `gkcrypt_local_mutex` traffic unchanged) + explicit A/B gate: **frame inter-arrival p99 during 10-press bursts within +5% of baseline.**
4. **P8:** unlocking before `sendto` decouples wire order from `key_pos` order — but this idiom already exists on the control path (`:594-601`) and each packet carries its own `key_pos`. Audit error paths for **exactly-once unlock** (the label restructure is where a double-unlock would hide).
5. **P2/P1-B echoes:** new-seq replay of absolute-state events is exactly what the stock 4-event carryover does every session → residual risk ≈ zero.

**Pairing / Senkusha / GKCrypt derivation are untouched by every recommended patch.**

---

## Highest-value bet: **P4 (with P1's edge-detection folded in)**

It attacks the largest verified, controllable term in the release-σ budget — the 8ms self-poll quantized to 15.6ms — removing **milliseconds of both mean AND σ**, per shot, every shot. It's also the **safest** big patch: lost-wake degrades to exactly today's poll (strictly dominating), the anti-stall invariant is preserved by edge-only signaling, and **zero wire bytes change.** A/B is cheap and decisive.

---

## Implementation sequence (surgical order)

- **Step 0 — ship today:** P2's `takion.c:767` one-liner (propagate `chiaki_takion_send_raw`'s return so F2 goes live). One line, zero risk.
- **Step 1 — telemetry first (P5+P7 merged):** build the send-side QPC-stage + ack-RTT pipe **before any big-patch A/B**, and record the **unpatched baseline distribution**. Include P9's one-line `q.rtt` INFO log in the same fork rebuild.
- **Step 2 — P4 ⊕ P1-A:** edge wake + `timeBeginPeriod(1)` + `THREAD_PRIORITY_HIGHEST`. A/B vs step-1 baseline; gate on the burst-stall regression test.
- **Step 3 — reliability (P2 echo + P10 immediate dupe, critical-mask gated):** forced-loss A/B at 5% with capture-card ground truth. After step 2 so echo constants tune against the tighter cadence.
- **Step 4 — P8:** `send_raw` outside `gkcrypt_local_mutex` in `takion_send_feedback_packet` **and** `chiaki_takion_send_mic_packet` + bounded WOULDBLOCK retry. Validate with step-1 telemetry under `iperf3` saturation.
- **Step 5 — P9 (NexusVision side):** takion-RTT composition, court-ping demoted to delta-bias / jitter-to-gate, behind a `compose_takion` flag; 200-shot A/B.
- **Step 6 — P3 (optional, telemetry-justified only):** GMAC ctx caching if step-1 histograms show crypto-tail spikes surviving step 4.
- **Step 7 — P6 (last, preview only):** frame-export pooling.

_Dependencies: telemetry before all A/Bs; P4 before echo tuning; P8 independent but measured by step 1; P9 needs step 1's pipe; P3/P6 gated on measured need._

---

## Per-item appendix (files · measurement · pass threshold)

- **P4** — `feedbacksender.c` `set_controller_state_nowait` (L120-136), `state_cond_check` (L307-312), `thread_func` wait clamp (L333-344); `feedbacksender.h` struct. **Measure:** QPC at edge-store in nowait setter → `send_raw` return; 200+ scripted press/release with video live. **PASS:** inject→send p50 < 1.0ms, p99 < 9.0ms, σ < 1.5ms; stick-spam wake count unchanged; burst frame-time p99 within +5%.
- **P2** — `takion.c` `takion_send_feedback_packet` (L742-772, bug L767); `feedbacksender.c` history echo. **Measure:** 5% forced loss + 10k clean injections. **PASS:** missed-release < 0.1% @5% loss (baseline ~5%); 0 desync in 10k @0% loss; inject→first-wire delta < 0.1ms.
- **P7** — `takionsendbuffer.c` packet struct (L19-26, add `first_send_us`); `takion.c` ack path; new GUI exporter. **Measure:** vs pcap RTT. **PASS:** median |rtt_us − pcap| < 1000µs over ≥50 samples; ≥0.8 samples/s; p99 added send cost < 5µs.
- **P10** — `takion.c` `takion_send_feedback_packet` (capture `wire_out` after gmac L763, dupe before L767). **PASS:** ≥99.99% delivery @5% first-send loss; added mean inject→first-wire ≤ 0.1ms; zero desync.
- **P5** — `takion.h` struct cb fields (~L183); `takion.c` `takion_send_feedback_packet`. **PASS:** ≥99.5% of releases produce a record with monotonic `inject≤lock≤precrypt≤postsend`; added takion-thread cost negligible.
- **P8** — `takion.c` `takion_send_feedback_packet` (lock L748, send L767, unlock) + `send_mic_packet`. **Measure:** `iperf3` uplink saturation + 5% loss. **PASS:** post-patch p99 of (mutex wait + send_raw) inside the fn well below baseline; zero double-unlock.
- **P9** — FORK `streamconnection.c` (L693-707, surface `q.rtt`); NexusVision `rtt_sync_engine.py` + `remote_play_orchestrator.py:~555`. **PASS:** composed network-lead std ≤ 1.5ms over 10-min Wi-Fi (legacy court-driven ≥ 3ms); per-shot-type release σ −≥20% over 200 shots.
- **P3** — `gkcrypt.c` `chiaki_gkcrypt_gmac` (L358-463), keystream malloc (L341). **PASS:** per-packet gmac p99 < 15µs / p99.9 < 40µs; mutex-wait p99 < 50µs; zero MAC-mismatch in a 30-min session.
- **P6** — `orionframeexport.cpp` `exportFrame` (L57-109), fixed out_buf L135. **PASS:** export latency p50 −≥25% / p99 −≥20% over 10-min 1080p hw-decode; no new copies on takion thread.

---

## Expected combined effect + honest ceiling

Steps 0–4 remove **~4–8ms mean and ~2–4ms σ** of client-side inject→wire delay, and take single-loss bricked shots from `p` to **~p²–p³**. What remains is **network RTT jitter** (addressed only statistically by step 5's composition) and **PS5-side input sampling** — both outside client-patch reach. That residual is the honest floor for the release leg; the σ-matrix (from the timing plan, `ORION_100PCT_TIMING_PLAN.md`) measures whether it now fits inside each shot type's green-window half-width.

_All patches land in the fork repo `chiaki-ng-src` (branch `orion`) except P9 (NexusVision main repo). Nothing in this plan has been applied — it is spec-only._
