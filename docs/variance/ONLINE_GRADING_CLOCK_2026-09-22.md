# Online grading clock — what the 2K server actually times (2026-09-22)

Corpus: `D:\NexusVision\shot_records\*.jsonl`, banner-graded Standstills, `onset_source=detect_loop`,
session-centred (median onset / hold per session, >=12 shots), onset glitches (|dev|>200 ms) and
non-meter releases (|meter_rel dev|>60 ms) dropped. Online n=816 / 27 sessions (09-17..21);
the 09-20/21 subset n=471 / 14 sessions agrees. Offline `session_20260920_170027` n=39 (2 misses,
uninformative).

## The observation that forces the question
| | onset sd | hold sd | meter-relative sd | EXCELLENT |
|---|---|---|---|---|
| offline 170027 | 73 | **72** | 4 | **95 %** |
| online 09-20/21 | 30-95 | 30-92 | 5-10 | 58-73 % |
Offline, hold time (press -> release) swings 72 ms sd and nothing is lost: the game grades its own
visible meter. Online, the bot is just as tight against the meter and loses 25-35 points.

## Ordered-probit fit: grade = hold - (1-beta)*onset + noise
- **beta = 0.45 (90 % CI 0.27-0.69, session bootstrap); 0.53 [0.29, 0.97] on 09-20/21 only.**
  Roughly HALF of the online grade follows the PRESS clock, not the visible meter. Reading: the meter
  the client draws is itself delayed/jittered relative to what the server starts timing.
- latent sigma 44 ms; EXCELLENT band [-55, +41] ms around the median shot (width ~96 ms, centre -7 ms).
- variance split: onset-driven **19 ms sd**; **unexplained 40 ms sd** (absent offline).

## ~~What each lever is worth~~ SUPERSEDED — this table used the double-subtracted residual; see RECONCILED LADDER at the end

### (superseded) What each lever is worth (model simulation, in-sample — must be confirmed live)
| policy | EXC | LATE | EARLY |
|---|---|---|---|
| no feedforward | 70.6 | 21.8 | 7.5 |
| shipped FF (gain .2, clamp 10, one-sided) | 72.7 | 19.3 | 8.0 |
| FF gain .45, clamp 40, two-sided | 75.8 | 16.2 | 7.9 |
| no FF, fire 7 ms earlier | 72.3 | 17.4 | 10.2 |
| FF .45/40 + 7 ms earlier | **76.8** | 12.2 | 11.0 |
| the unexplained 40 ms cut to 30 / 20 / 10 ms sd | **89 / 98 / 100** | | |

## ~~Conclusions~~ SUPERSEDED in part — see CORRECTIONS and RECONCILED LADDER at the end

### (superseded) Conclusions
1. The shipped onset feedforward is the right lever at ~half its identified strength. Candidate
   config: `onset_ff_gain` 0.45, `onset_ff_clamp_ms` 40, `onset_ff_one_sided` false; plus -7 ms.
   Prize ~+4-6 EXC points. Must be A/B'd live (gain 0.2 vs 0.45, randomised per shot) — this
   estimate is in-sample and model-based.
2. "Aim at the tip" was derived offline at 5-9 ms noise; at 44 ms online noise the aim sits ~7 ms
   late of the band centre. Small, but the direction is now measured, not assumed.
3. **The real prize is the unexplained 40 ms sd.** It does not exist offline, so it is online
   specific: uplink jitter of the release vs the press, server-tick phase of press and release, or
   server processing. Court RTT is NOT recorded on the capture-card rig (`network.court_ready=0` on
   every stamped record). On ICS the PS5's game traffic is routed THROUGH this PC, so it is
   measurable per shot. Instrument before building anything.

## CORRECTIONS after the second-eyes review (`SECOND_EYES_REVIEW.md`, verdict: weakened) — read before using any number above

1. **beta is a robust ASSOCIATION, not an identified mechanism.** Across nine refits it spans
   0.33-0.67 (random-intercept 0.40, per-session cut-points 0.33-0.35, tighter filters 0.67);
   leave-one-session-out 0.38-0.49; LR vs beta=0 p~1e-22. "Half the grade follows the press clock"
   is a hypothesis this supports, not a demonstrated fact.
2. **The "unexplained 40 ms" was my arithmetic error.** The fitted 44 ms is the residual AFTER onset is
   in the model; I subtracted onset's share a second time. No online-only component is identified.
3. **The 76.8 % projection was impossible under my own model.** Fixed band 94.4 ms, residual 43.5 ms:
   maximum EXCELLENT with perfect centring = 2*Phi(94.39/(2*43.54)) - 1 = **72.2 %**. The feedforward
   prize is therefore ~+3-5 points, not +6. Mid-80s requires the fixed-band model itself to be wrong
   (the band does vary per shot - see GREEN_WINDOW_WIDTH) or the residual to be reducible.
4. **A settings-only 0.45/40 is capped at 15 ms earlier** by the banner-trim bound on the ordinary path;
   only the A/B arm carries its own bound. Do not recommend it as a plain settings change.
5. **The probe was not headers-only** at snaplen 96 (~54 bytes of payload). Fixed: snaplen 42.
6. The identifying measurement is a **randomised per-shot release offset** (`-OffsetSweep`, uniform
   -25..+25 ms, the existing dev hook): it puts the banner grade on a real millisecond scale, which the
   ordinal fit alone cannot.

## RECONCILED LADDER (2026-09-22, after the second-eyes follow-up)

Recomputed with the reviewer's reproduced fit (beta 0.44, residual 43.54 ms CONDITIONAL on onset,
band [-54.61, +39.78] ms), sampling the observed onset deviations (n=816).
**SIMPLIFYING ASSUMPTION: meter-relative deviation fixed at zero (onset-only illustration).** Keeping the
observed meter-relative deviations instead gives no FF / shipped / arm 1 = **67.26 / 68.46 / 69.78 %**
(reviewer, deterministic integration). Neither version is a causal policy estimate: historically applied
corrections are incompletely observed. Honest range for arm 1 over shipped: **+1.3 to +2.7 points.**
Reviewer's exact deterministic values for the onset-only version: 66.46 / 68.22 / 70.90 / 71.47 / 72.16 %.

| policy | EXC |
|---|---|
| no feedforward | **66.7 %** (observed 67.2 % — the corrected model now matches reality) |
| shipped 0.2 / 10 / one-sided | 68.4 % |
| A/B arm 0.45 / 40 / two-sided | **71.0 %** |
| perfect onset cancel | 71.6 % |
| perfect cancel + centred (fixed-band ceiling) | 72.3 % |

- The reviewer's "+1.56 at most" was measured from the OLD 70.6 % baseline, itself produced by the
  broken residual. Against the corrected, reality-matching baseline the arm is +2.6 over shipped and
  +4.3 over no feedforward, and sits within ~1.3 points of the fixed-band ceiling. These are still
  in-sample model numbers; the pre-registered A/B decides.
- Counts: the newer (09-20/21) online subset is **471** shots / 14 sessions (my first fit and the
  reviewer's fresh count agree; the earlier "470" was the one-off).
- Offline SDs: the "73/72 ms" in the opening table are all 40 offline shots; the filtered 39 are
  onset 48.3 / hold 48.5 ms. The conclusion (large hold variation, 95 % EXC offline) is unchanged.
- Snaplen 42 is a byte limit, not a protocol-aware guarantee. `udp` also matches NON-INITIAL IPv4
  fragments (no UDP header, payload from byte 34); the filter now adds `(ip[6:2] & 0x1fff) = 0` to
  exclude them. The PS5 adapter is an Ethernet NPF device. Link type is recorded in the pcapng.
- Packet identity: "first PS5 packet after the release" is a PROXY, not a verified command-bearing
  packet; the pre-registration gates inference on local command timing and treats packet identity as
  needing independent validation.
- The dev offset is drawn per ARM TOKEN (a physical shot can re-arm). The analysis uses the offset the
  release actually FIRED with (the seq-paired release line), and gates all inference on measured
  first-stage fidelity - commanded offset vs the Remote Play datagram time in the capture - exactly as
  the pre-registration specifies.
