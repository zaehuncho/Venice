# Orion — RTT / Network-Sync Investigation & Fix Brief (max depth)

**You are investigating and then fixing the RTT / network-sync subsystem of Orion**, a real-time NBA-2K auto-green bot. Orion watches the shot meter over an HDMI capture card and injects the release through a forked Chiaki Remote Play session to a PS5. The "sync" subsystem is supposed to measure the network latency between the player and the **game server ("the court")** and shift the release fire time so the on-screen result lands on the green window despite network delay — i.e. it should be a **real, measurable timing advantage**, and the **Network UI must show true-live values** (live court IP, real jitter, real RTT, the offset actually applied).

Right now it does neither reliably. Your job: **map the true current state with concrete evidence, root-cause why it's dead/wrong, then produce a flag-gated fix plan that (1) makes sync a real advantage and (2) makes the Network panel display true-live values.** Repo: `C:\Users\aaron\Desktop\NexusVision`. The Chiaki fork is the **sibling** dir `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`) — NOT the in-repo vendor copy.

---

## ⚠️ CRITICAL REFRAME — there are TWO hops, and the one that matters is NOT the one the code chases

The bot is a **control loop with delay**: its eyes (capture card) and hands (input) are on the PC, but the game runs on the PS5. So two distinct latencies exist, and prior work conflated them:

1. **Remote Play loop — PC↔PS5 (PRIMARY, the one that matters).** Orion sees the meter late (capture card ~30–35ms + Remote Play video decode, PS5→PC) and its release input travels PC→PS5 before it lands. This round-trip is the network term of the bot's release timing. **The fork already measures exactly this** — senkusha / `q.rtt` in the chiaki Remote Play client are the **PC↔PS5** RTT — **and throws it away.** This is the real prize.

2. **Court-server loop — PS5↔2K server (SECONDARY / DIAGNOSTIC).** What `rtt_sync_engine`'s `CourtIPDetector` + ICMP ping chase. In 2K the shot green is computed on the *shooting console* (client-authoritative, server-reconciled), so this hop likely does **not** move where the green lands — it's a "which server am I on / how good is my connection" **diagnostic**, not the timing driver. (Unconfirmed: if 2K greens turned out server-timed, this hop would matter more — see **Q0** below, which makes the bot settle this empirically.)

**Consequences for this whole brief, override any conflicting wording below:**
- The **authoritative RTT to plumb and consume is the fork's PC↔PS5 Remote Play RTT** (senkusha/`q.rtt`), NOT the PS5→court hop. Anywhere older text says "PS5→court stream RTT" for `q.rtt`, read it as **PC↔PS5 Remote Play RTT**.
- The **ICMP court ping is demoted to a diagnostic/display signal** (Network panel: "which 2K server, how far"), never the primary timing input.
- The oracle (measured-lead) **already absorbs the average loop delay empirically** — so live RTT's job is the **fast per-shot correction + jitter robustness**, layered on top of the oracle as a delta, NOT a replacement. Reconcile so you never double-count the network term.

---

## Your operating contract (non-negotiable)

1. **Prove every claim.** Each finding needs `file:line` + a real artifact: a log excerpt (`logs/orion_native.log`, `logs/orion_user.log`), a settings value, a test result, or a before/after number. No assertion without evidence. If something can only be settled on a live rig, say so explicitly and say what to capture.
2. **Re-verify the "already-traced" findings below before trusting them.** They came from a prior read-only pass and are believed correct, but you must confirm each against current code (line numbers drift). Treat them as a head-start map, not gospel.
3. **Separate MAP from FIX.** First deliver the map + root-cause (what is, and why). Then the fix plan. Do not start rewriting before the map is done and the gating question (Q1) is answered.
4. **Flag-gated, default-OFF, reversible.** Every behavioral change hides behind a new env flag or settings key, default-OFF, byte-identical when off. Nothing you touch may change shipped behavior until proven. Do **not** flip a default or commit — a human reviews and a live batch confirms first.
5. **Never weaken the guaranteed-release invariant.** The bot must ALWAYS release a held shot. No sync change may introduce a path where a bad/missing RTT value stalls or cancels a release. A dead sync must fail *soft* to the current clock, never block the fire.
6. **Privacy/permission gate is real.** Court detection uses passive packet sniffing (WinDivert). It is opt-in by design (`network_packet_capture_opt_in`). Do not silently force it on; treat re-enabling it as a product decision to surface, with the trade-offs.

---

## System shape (two overlapping sync implementations + the fork)

There are **two** RTT engines feeding the same UI + timing, plus the fork that could feed the correct value:

- **Native C++ path (`native_orion/src/`):** `NetworkBridge.cpp/.h` (passive court-IP detection + packet-interval stats via a WinDivert forward-sniff service `nexus_svc.py`), `RttEstimator.h` (RFC3550 interarrival jitter + EMA RTT + `syncAdjustMs`). Feeds `OrionAppController` telemetry → QML.
- **Python path (`rtt_sync_engine.py`):** `CourtIPDetector` (pps-scored public-IP lock), `KalmanFilter1D` over ICMP/TCP ping, `TickSynchronizer` (tick phase lock), `CourtProfileDB` (per-court RTT persistence), decode-latency compensation. Runs in the autogreen sidecar; snapshots relayed to native via `RemotePlaySession.cpp`. Also drives the standalone virtual-controller path in `controller_remap.py`.
- **The Chiaki fork (`chiaki-ng-src`):** measures the *real* **PC↔PS5 Remote Play RTT** (`q.rtt` in `lib/src/streamconnection.c` CONNECTIONQUALITY, senkusha setup RTT in `lib/src/senkusha.c`) — the exact loop the bot's video-down + input-up traverse — and currently **discards** it. (This is the PRIMARY hop per the reframe above, NOT the court hop.)

`OrionAppController.cpp` merges the two engine feeds (native bridge preferred when court-locked; otherwise the sidecar value wholesale). The merged `telemetry_` drives both the Network UI and `AutomationEngine::updateNetworkQuality` → the release deadline.

---

## Already-traced findings (verify, then build on — do not re-derive from scratch)

**A. Court detection has been DEAD since 2026-07-02.**
- `logs/orion_native.log`: ~26,958 `"Court IP locked"` lines, **last at `2026-07-02T09:37:18Z`**; first `"Packet bridge: off (opt-in...)"` at `2026-07-02T16:36:20Z`, recurring through today.
- Cause chain: opt-in gate at `OrionAppController.cpp:2286-2297` + `network_packet_capture_opt_in=False` in `settings.json` + default-false `passiveSniffingEnabled` (`AppConfig.h:253`, mapped `AppConfig.cpp:301/:781`). Bridge off → no `courtIpDetected` → sidecar never gets a court IP relayed → its own detector never locks either.

**B. The sidecar engine has NEVER locked a court on its own.** Every `"RTT tick:"` line in the log ends `court=-`. Recent ticks read `raw=0.5-1.0ms filtered=0.6-0.7ms half=0.3-0.4ms jitter=0.3ms` — that is the **LAN gateway** (seeded at `remote_play_orchestrator.py:1087-1088`), not the court.

**C. Wrong hop by design.** The Python engine pings a **PC→2K-server** ICMP path (`rtt_sync_engine.py:119-161`) — a hop that (a) neither the video nor the input actually traverses, and (b) per the reframe is at best a diagnostic. Meanwhile the RTT the bot actually needs — the **PC↔PS5 Remote Play loop** — IS measured by the fork and thrown away: `streamconnection.c:693-707` only `CHIAKI_LOGV`s `q.rtt` ("rtt=%.4f") and stores nothing; `streamconnection.h:80` has no rtt field; senkusha RTT (`senkusha.c:292-386`) is a one-shot session-setup value stored once (`session.c:649`) and never re-measured. No `q.rtt`/netstats export to Orion exists (`compose_takion` / `ORION_QRTT` are doc-only, unimplemented). **The fix is to plumb this Remote Play RTT, not the court ping.**

**D. Sync DOES shift fire time — but with two big caveats.**
- Applied: sidecar `predicted_offset_ms` → `RemotePlaySession.cpp:2091` (`syncAdjustMs`) → `OrionAppController.cpp:1757-1815` merge → `networkAutomationOffset()` (`:7112-7128`) → `AutomationEngine::updateNetworkQuality` (`:1566-1577`) → latched at shot start (`:1892-1911`) → subtracted from the deadline (`AutomationEngine.cpp:2509-2520`, `const double deadline = predTip - effectiveLatency;`).
- **Caveat A (GATING):** on the measured-lead / fused-lead path the RTT offset is deliberately **zeroed** (`AutomationEngine.cpp:2338/:2352-2357/:2653-2660`) because the oracle round-trip already contains network RTT. **If measured-lead is the live default, improving RTT input may do NOTHING for the primary shot type — resolve this FIRST (see Q1).**
- **Caveat B:** the value applied is LAN half-RTT (~0.3-1ms) + ~7.5ms decode comp + 2.5ms tick advance — it moves the deadline but models the wrong hop.

**E. Network UI: everything is "live but wrong-source" or frozen.** `native_orion/qml/pages/DashboardPage.qml` Network tab:
| Field | Binds to | State |
|---|---|---|
| Court IP (`:358`) | `orion.telemetryCourtIp` | **Frozen "—"** since 07-02 (never locks) |
| "RRT" (`:376`, label typo → RTT) | `orion.rttMs` | Live but **LAN gateway hop** (0.5-2ms) |
| Jitter (`:378`) | `orion.jitterMs` | Live but **near-zero LAN** (never hits the ≥4 warn colour) |
| Applied offset (`:382`) | `orion.effectiveSyncAdjustMs` | Genuinely fed to the engine, but composed from the wrong inputs |
| Ticker Latency (`:356`) | `orion.tickerLatencyMs` | Live but wrong-source (tick-ETA fallback math, no packets) |
| Inbound/Outbound packet cards (`~:300-340`) | `orion.inbound/outboundPackets` | **Frozen at 0** (bridge off) |
- `GeneralPage.qml:129-130` shows the same RTT/jitter pills. `RemotePlayPage.qml:43-47` actively filters court-IP lines OUT of its log view.

**F. Dead/loose ends:** `pre_shot_warmup` (`rtt_sync_engine.py:521`) and `get_effective_offset_ms` (`:672`) have no callers; `court_profiles.json` is `{}` (never persisted a court); legacy status-file ingest keys `orion_sync_*` (`OrionAppController.cpp:6817-6890`) have no identified writer; the "RRT" typo.

---

## Investigation targets — answer each with evidence

**Q0 — Which hop actually governs the green: PC↔PS5 Remote Play, or PS5↔court server?**

> **OPERATOR VERDICT (2026-07-22, from the player who runs the bot — treat as a strong prior, still confirm with the log-correlation in (d)):** Bot runs in **online MyCourt**. Greens are **server-adjudicated** online. BUT: **a truly perfectly-timed release greens regardless of connection, on- or offline** — so 2K **lag-compensates to the local release moment**; court RTT does **NOT shift where to release**. Online is *harder* only because jitter/delay make the meter harder to time (perception noise), not because the target moved. **"Visually green but miss" is common — especially on TEMPO** — which is the server **rejecting a *marginal* (green-edge) release** under jitter; a dead-center release has margin and survives.
>
> **Resulting model to build to:** (1) **PC↔PS5 Remote Play RTT → the release LEAD** (primary — where to fire). (2) **PS5↔court JITTER → a reconciliation-risk / safety-margin signal** — high jitter ⇒ aim green-**center**, not the tip, so a slightly-off release survives the server's call (tempo needs the most margin). Court RTT is NOT a timing-shift lever; court **jitter** is a margin lever. Plumb both, use them for these two different jobs.

This decides whether the court hop is a timing lever or just a diagnostic (see reframe). The operator verdict above answers the *direction*; still design experiments that CONFIRM it and quantify the margin lever, and specify exactly what to log/capture:
- **(a) Offline vs online A/B (decisive).** Same bot config in an OFFLINE mode (MyCareer/practice vs CPU — no court server in the loop, only Remote Play) vs an ONLINE mode (Rec/Park/MyCourt). Same ideal lead + make-rate → **console-timed** (court hop irrelevant). Systematically different lead online → **server-timed component**, and the shift size is the answer.
- **(b) Vary court RTT, hold Remote Play RTT fixed.** Same PC↔PS5 LAN, sessions on game servers of different ping. Regress the oracle's ideal lead vs court ping: flat → console-timed; sloped → server-timed (slope = compensation factor).
- **(c) Vary Remote Play RTT, hold court fixed.** Confirm the ideal lead shifts ~1:1 with the Remote Play RTT (expected — it's the dominant lever either way).
- **(d) FREE observational version (do this regardless).** Once STEP-1 (Q3) logs BOTH RTTs alongside each graded shot (`heldOffsetMs`, grader LATE/EARLY ms), regress landing-error against court-RTT and against Remote-Play-RTT across many shots. **Whichever RTT the residual timing error tracks is the hop that matters.** The bot becomes its own experiment.
- Output: a verdict (console-timed / server-timed / mixed with a factor) backed by the correlation, OR — if it needs live capture — the exact logging + session plan to settle it. This gates whether Q3 plumbs one RTT or both.

**Q1 — GATING: Does sync actually advantage the release TODAY, or is it zeroed?** Determine whether `measuredLeadEnabled` / the fused-lead path is the live default (check `learning.json`, `AppConfig`, and the runtime logs — does `useMeasuredLead` evaluate true on standstill/primary shots?). If the RTT offset is zeroed on the shots that matter, then the entire "make RTT correct" effort is moot until that's reconciled. **Resolve this before designing any fix.** Output: for each shot type (standstill, fade, tempo, go-to), is the network offset applied or zeroed, with the line that decides it. If zeroed, propose how a *correct* court-RTT should re-enter the lead (co-designed with the oracle so you don't double-count RTT).

**Q2 — Court detection: why dead, and what is the right revival (as a DIAGNOSTIC)?** Confirm the opt-in gate is the sole cause (A). Note the reframe: court detection is primarily a **diagnostic/display** feature (which 2K server, how far) unless Q0 proves greens are server-timed — so weigh its revival cost against that lower value. Determine: is re-enabling passive sniffing (`network_packet_capture_opt_in=True` / `passiveSniffingEnabled`) sufficient to lock a court again, and is it safe/desirable (WinDivert requires the `nexus_svc.py` service + elevation — verify what it needs)? Is the Python `CourtIPDetector` a viable court lock WITHOUT the bridge (it only gets packets relayed from the bridge today — `autogreen_sidecar.py:944-953`)? Recommend the revival path and prove it can lock a real court IP again (or say exactly what live capture is needed to prove it).

**Q3 — The real advantage = plumb the PC↔PS5 Remote Play RTT (fork `q.rtt`).** This is the crux. Design the plumbing to capture the fork's real **PC↔PS5 Remote Play** RTT (`streamconnection.c:693-707` CONNECTIONQUALITY `q.rtt`, and/or a periodic senkusha re-probe) and export it to Orion (a netstats pipe / stdout line the sidecar already reads), then feed it into the Kalman/estimator as the authoritative RTT **layered onto the measured-lead oracle as a fast per-shot delta (NOT a replacement — reconcile so the network term isn't double-counted; see Caveat A/Q1)**. The ICMP court ping is **demoted to a diagnostic/display signal**, not a timing input (unless Q0 proves greens are server-timed, in which case ALSO plumb the court RTT and co-design both). Map the exact fork edit sites, the export channel, and the Orion ingest point. **STEP-1 must be log-only** — export and log the Remote Play `q.rtt` (and the court ping) alongside each graded shot so it simultaneously (i) proves units/cadence/sanity and (ii) feeds Q0's correlation — before ANYTHING consumes it for timing. Quantify: how different is the real Remote Play `q.rtt` from the LAN-ping value the bot uses now (expected ~15–40ms vs ~0.3–1ms), and does it track the residual timing error?

**Q4 — Make the Network UI true-live.** For every field in table E, either make its binding reflect real live data or remove/relabel it honestly. Concretely: Court IP shows the live locked court (or an honest "detecting…"/"packet capture off" state, not a frozen "—"); RTT/Jitter show the correct-hop values from Q3; the packet cards reflect live counts or are hidden when the bridge is off; fix the "RRT" typo; surface the sync *source* (which engine/hop produced the number) so it's never silently wrong-hop again. Give a per-field before/after and the binding chain you changed.

**Q5 — Define & measure "real advantage."** State precisely what a correct sync does to the release, in loop terms: compensate the PC↔PS5 Remote Play delay (+ court delay only if Q0 says server-timed) so the press lands at the right moment on the PS5. Be honest about the value: the oracle already nails the *average* delay, so RTT sync's advantage is **fast per-shot adaptation + robustness**, not a big raw make-rate jump. Name the concrete wins and how to prove each: (i) **jitter/drift resilience** — landing-error variance across a fluctuating connection, live RTT ON vs OFF; (ii) **cold-start** — first-N-shot accuracy on a fresh session vs the stale-clock baseline; (iii) **jitter-gated safety** — fewer bricks on unstable links. A win that can't be shown in the numbers (offline where possible, live where required) is not a win — say so. And keep the headline honest: this is **second-order to detection** (the ~66ms under-read dominates the ~6–40ms network term).

**Q6 — Additive: what should sync do that's built-but-dormant?** Assess the dormant machinery and recommend keep/fix/cut with evidence: `TickSynchronizer` phase-lock + `align_release` tick-snap (does snapping the release to the game's tick phase help?), jitter-adaptive release-confidence gating (`AutomationEngine.cpp:3364-3367/:4515-4516`), `CourtProfileDB` per-court RTT priming (warm-start a known court), decode-latency compensation. Anything genuinely additive that a correct RTT unlocks, propose it here.

**Q6a — THE jitter-margin lever (first-class, per the Q0 operator verdict).** Since online greens are server-adjudicated and *marginal* (green-edge) releases get reconciled-away under jitter ("visually green but miss," worst on tempo), design a flag-gated rule: **as court-side jitter rises, bias the aim point from the green TIP toward green CENTER** (widen the effective target inside the green window) so a slightly-off release still survives the server's call. A dead-center release already survives (operator: perfect timing greens regardless), so this only trades a sliver of tip-precision for reconciliation-safety, and only when jitter warrants it. Deliver: where the aim/target point is chosen in the engine, how jitter would modulate it, the tempo-specific case (largest margin), and how to A/B it (green-rate on high-jitter sessions, tip vs jitter-adaptive). This is the concrete "court hop earns its keep" outcome — court **jitter** as a safety signal, not court RTT as a timing shift.

---

## Deliverables

**Part 1 — The Map** (verified): per-area (Python engine, native bridge/estimator, fork, UI, consumption, telemetry) with file:line, plus the Q1 answer (applied-vs-zeroed per shot type) and the verified live-vs-fake UI table.

**Part 2 — Root cause**: why court is dead, why the hop is wrong, why the UI reads wrong — each with the deciding line + a log/settings artifact.

**Part 3 — Waved fix plan**: ordered, each item = flag/settings key · file:line edit sites (both repos where needed) · what it fixes · how it's proven (offline gate / log probe / live batch) · before→after metric · revert. Sequence by dependency: **Q3 STEP-1 log-only probe (logs both RTTs + outcomes) comes first — it feeds Q0's hop-determination AND the unit proof**; then Q1 reconciliation (oracle-delta design); then any consumer change. Court-detection revival (Q2) is gated on Q0 (only worth the cost if server-timed, else diagnostic-only). Note which items are offline-provable vs live-only.

**Part 4 — Guardrails checklist**: confirm each change is flag-gated default-OFF, fails soft (never blocks a release), keeps the detection gates green (`python tools/regression/run_gates.py`) and the native tests green (`OrionNativeTests`), and doesn't force packet capture on without surfacing the opt-in.

**Part 5 — Honest gaps**: what needs a live rig, what's unproven, what you couldn't determine.

---

## Evidence sources

- **Code:** `rtt_sync_engine.py`, `remote_play_orchestrator.py`, `native_orion/backend/autogreen_sidecar.py`, `native_orion/src/{NetworkBridge,RttEstimator,RemotePlaySession,OrionAppController,AutomationEngine,AppConfig}.{cpp,h}`, `native_orion/qml/pages/{DashboardPage,GeneralPage,RemotePlayPage}.qml`, `controller_remap.py`, `nexus_svc.py`. Fork: `chiaki-ng-src/lib/src/{streamconnection,senkusha,takion,session}.c`.
- **Logs:** `logs/orion_native.log` (grep `RTT tick:`, `Court IP locked`, `Packet bridge`, `RTT sync`), `logs/orion_user.log`.
- **Config:** `settings.json` (`network_*`, `rtt_sync`), `learning.json` (measured-lead, `shot_type_rtt_baseline_ms`), `court_profiles.json`.
- **Tests:** `tests/test_rtt_sync_engine.py` (note what's asserted vs untested — the ping loop, court-profile persistence, tick align, and the native↔sidecar relay are NOT covered).
- **Prior specs (doc-only, unimplemented — cross-check, don't trust as done):** `docs/ORION_MASTER_FIX_PLAN.md` (§5a P9 staged), `docs/ORION_CHIAKI_INJECT_PATH_PLAN.md`.

Deliver the map first. Do not change a default or commit. If the gating question (Q1) reveals the offset is zeroed on the primary shot type, STOP and report that before proposing downstream RTT work — it changes everything.
