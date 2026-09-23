# Fire-to-wire: how much delay and jitter the PC adds to a release (2026-09-22)

## Verdict

**The PC side is not a meaningful source of timing variance. Rule it out.**

- Time from the scheduled deadline to the release datagram on the wire, over 425 of 425 shots:
  **median 0.22 ms, sd 0.09 ms, p95 0.43 ms, p99 0.55 ms, max 1.02 ms.** Every shot is well under
  the 3 ms sd and 10 ms tail thresholds, by more than 30x.
- The scheduler is effectively exact. Actual command time minus the scheduled deadline is
  0.000 ms at the median, 0.018 ms at p99 and 0.14 ms at worst (`Scheduled fire: deltaMs`).
- The fire-to-wire time does not depend on the randomised dev offset, the onset feed-forward,
  the frame-grid phase or the time of the session.
- The fire-to-wire time does not relate to the banner verdict once the randomised offset is
  controlled for. The partial Spearman correlation between the ordinal verdict and fire-to-wire,
  given dev_ms, is 0.05 (p = 0.50). The only nominal differences between verdicts are 0.03 ms.
  That is about 500x smaller than one video frame (16.7 ms) and cannot move a banner.
- The press edge reaches the wire in median 1.9 ms (sd 0.49 ms, max 3.8 ms). Most of that spread
  comes from `press_ts_ms` being truncated to whole milliseconds. The press delay is flat across
  verdicts (Kruskal p = 0.59).
- The echo is never shed and the twin is never lost. All 425 releases show the triplet: primary,
  twin at +3.2 ms (median) and echo at +40.6 ms (median).

The timing variance lives downstream of the PC: in the network, the console's input poll, the
game's animation and meter timing, and the prediction of the deadline itself. Nothing between
"the scheduler decides" and "the datagram leaves the NIC" is worth engineering time.

## Data

| Source | Use |
|---|---|
| `D:\NexusVision\netcap\20260922_220018\packets.csv` | 586,981 packets, 22:00:19Z to 22:32:32Z, wall ms from tshark `frame.time_epoch` |
| `shot_records\session_20260922_165506.jsonl` | 223 releases inside the capture (dev offset uniform from -25 to +25 ms) |
| `shot_records\session_20260922_172143.jsonl` | 202 releases inside the capture (offset 0 or +10) |
| `logs\orion_native.log.1` (+ `.log` for epoch 202) | `Release submit`, `Precise dispatch timing`, `Scheduled fire`, `DEV FIRE OFFSET APPLIED`, `Release onsetff`, `Square-down delivery identity` |

No fork (OrionStream) log with per-packet send times was needed. The pipe ACK in the native log
brackets the send from above (see "Clock offset").

## Method

### 1. Which datagram is the release

The PC-to-PS5 takion packets on port 9296 fall into these frame lengths. Each frame length
includes 42 bytes of Ethernet, IP and UDP headers.

| frame.len | payload | what | source |
|---|---|---|---|
| 82 | 40 = 0xc + 0x1c | FEEDBACK_STATE (type 6), periodic, at least every 50 ms | `takion.c:791-810`, `feedback.h:36` |
| 57 | 15 = 0xf | congestion packet | `takion.h:273` |
| 71 | 29 = 1 + 0x10 + 0xc | takion DATA_ACK | `takion.c:725` |
| **69** | 27 = 0xc + 5 x 3 | **FEEDBACK_HISTORY (type 1)**: 4 retained events plus 1 new, each a 3-byte button event | `takion.c:845-860`, `feedback.c:87-160`, `feedbacksender.c:11,695-696` |
| 70 / 66 | 28 / 24 | history with a 2-byte event, or only 4 events | same |

A shot's release is the first 69/70/66 datagram at or after `release_ms`. It is confirmed by the
fork's signature pattern: a twin about 3 ms later (`CHIAKI_ORION_INPUT_RELEASE_REDUNDANT_DELAY_MS`
3) and an echo about 40 ms later (`..._RELEASE_ECHO_DELAY_MS` 40, `orioninput.h:24,28`).

**Match confidence.** All 425 of 425 shots matched. Session 165506 matched 223 of 223 and session
172143 matched 202 of 202. Epoch 202 was matched by hand because its log lines rotated into
`orion_native.log`; its fire-to-wire is 0.18 ms.

- In 397 shots the triplet is exactly primary, twin, echo, with nothing else in between.
- 28 shots have one extra history packet in the window: a passthrough button event between the
  twin and the echo. The primary and twin are unaffected.
- Two shots (165506 epochs 19 and 96) have an odd twin spacing (0.1 and 1.2 ms), so they are
  medium confidence. Their fire-to-wire values of 0.21 and 0.22 ms are typical anyway.
- Unrelated history traffic runs at 2.85 packets per second. The chance that an unrelated packet
  falls in the first millisecond after a release is therefore about 0.3%. The triplet check
  removes that risk.
- No primary release datagram is missing.

### 2. Which "intended time"

Three candidates exist. Only one is precise enough.

- **`release_ms`** (shot record, and the `shot_gate_release` log line). This is the engine's
  `schedFireActualMs_`, which equals `commandIssuedMs`: the monotonic instant just before the
  pipe write (`OrionAppController.cpp:1720`). It is mapped to wall time as
  `system_clock::now() - (engineNow - commandIssued)` (`AutomationEngine.cpp:13183-13190`,
  `epochNowMsF` at `:156`), which gives sub-microsecond resolution. **This is the anchor used.**
- **The scheduled deadline.** `Scheduled fire: deltaMs` = `commandIssued - deadline`
  (`AutomationEngine.cpp:9343`). Its median is 0.000 ms and its maximum 0.14 ms. So "deadline to
  wire" = "command to wire" + deltaMs, and the two are indistinguishable. The precise-fire thread
  waits on a high-resolution timer until 1.2 ms before the target, then busy-spins the rest
  (`OrionAppController.cpp:1569,1616`). `commandIssued` is stamped after `submitMutex_` is taken
  (`:1632`), so contention on that mutex is already inside deltaMs, and it is negligible.
  (`aligned_ms`/`unaligned_ms` on the same log line are the last vision arm's plan, not the final
  deadline. Do not use them for this purpose.)
- **`fire_epoch_ms`** (`Release submit`). It is anchored with `QDateTime::currentMSecsSinceEpoch()`,
  which is truncated to whole milliseconds (`FireEpochClock.h:88-97`). Against the wire it reads
  1.27 ms at the median with **sd 0.49 ms and a range of 2.8 ms**. All of that is clock-read
  noise: the same shots measured from `release_ms` have sd 0.09 ms. **Do not use `fire_epoch_ms`
  for sub-millisecond work.** The same caveat applies to `press_ts_ms`, which is an integer.

## Results

### Fire-to-wire = first release datagram - release_ms (ms)

| set | n | median | mean | sd | p5 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|
| 165506 (dev offset from -25 to +25) | 223 | 0.215 | 0.246 | 0.081 | 0.176 | 0.430 | 0.529 | 0.604 |
| 172143 (dev offset 0 / +10) | 202 | 0.222 | 0.258 | 0.101 | 0.178 | 0.431 | 0.665 | 1.023 |
| all | 425 | 0.221 | 0.252 | 0.091 | 0.177 | 0.431 | 0.552 | 1.023 |

**Shape of the distribution.** It is unimodal and right-skewed, with a floor of about 0.16 ms.
In 0.05 ms bins starting at 0.15 ms, the counts are 135, 141, 63, 33, 27, 10, 6, 4, 1, 1, 2.

- There is no 1 ms, 4 ms or 8 ms quantisation. The takion sender's 8 ms minimum period and 50 ms
  timeout do not apply, because the Orion queue wakes the sender immediately.
- The single value over 0.7 ms (1.02 ms, 172143 epoch 131) is the one release that went through
  the GUI-tick path (`packet_snapshot=direct_pipe_seq_join`) instead of the precise mailbox.

**What moves it.** It tracks the pipe round trip (Spearman rho 0.96 with `active_route_wait_ms`)
and the pipe write time (rho 0.62). The whole local path slows or speeds together: pipe write,
then the fork bridge read, then enqueue, then sender wake, then gkcrypt, then `sendto`. There is
no evidence of a queue.

It shows no relation to:
- the dev offset (rho 0.06, p = 0.27)
- the onset feed-forward (rho -0.02)
- the frame-grid phase (rho 0.01)
- time in session (+0.001 ms per minute, about 0.03 ms over 30 minutes)

The two 172143 arms are the same within 0.04 ms: offset 0 has median 0.232 ms and offset +10
has median 0.215 ms.

### Stage split, from the native log (median / p95 / max)

- deadline to command: 0.000 / 0.000 / 0.14 ms (scheduler plus submit mutex)
- command to datagram on the wire: 0.22 / 0.43 / 1.02 ms (pipe, fork bridge, sender, `sendto`)
- wire to pipe ACK received by the native: 0.085 / 0.17 / 0.28 ms, after the wire in 423 of 423
  shots (one shot has no ACK field)

### Twin and echo spacing (from the primary)

- The twin comes at a median of +3.19 ms (p5 2.49, p95 3.98).
- The echo comes at a median of +40.55 ms (p5 39.3, p95 41.5). The p5 is slightly low because of
  the 28 extra-packet cases.
- The echo is present in 425 of 425 shots, so `RELEASE_ECHO_MAX_QUEUE` (2) never shed one here.
- Neither the twin nor the echo can make the console see the release earlier than the primary.
  They matter only when a datagram is lost.

### Relation to the banner verdict

Fire-to-wire by verdict, all shots (ms):

| verdict | n | median | mean | sd | p95 | max |
|---|---|---|---|---|---|---|
| EARLY | 54 | 0.212 | 0.226 | 0.053 | 0.344 | 0.366 |
| EXCELLENT | 255 | 0.226 | 0.263 | 0.103 | 0.443 | 1.023 |
| LATE | 72 | 0.213 | 0.242 | 0.081 | 0.435 | 0.482 |

- **Within session.** Kruskal-Wallis gives p = 0.052 for 165506 and p = 0.19 for 172143. The pooled
  test gives p = 0.018, which comes from EXCELLENT having a slightly heavier right tail.
- **Controlling for the randomised offset (165506, n = 190 graded).** The dev offset itself
  strongly predicts the verdict (Spearman 0.52, p = 1e-14). This confirms that the banner is
  sensitive to millisecond-scale displacement. The logit coefficients for dev_ms are +0.088 per ms
  for LATE and -0.101 per ms for EARLY. Fire-to-wire adds nothing for LATE (z = -0.02). The
  partial ordinal correlation, given dev_ms, is 0.05 (p = 0.50).
- **The EARLY logit.** It shows a nominal fire-to-wire coefficient (z = -3.6), because EARLY shots
  have a tighter fire-to-wire spread (sd 0.047 ms). The whole difference is about 0.03 ms of mean
  shift. Measured by the dev-offset coefficient, that shift is worth about 0.003 logit, so it
  cannot be causal. Treat it as chance across roughly 10 tests, or as a marker of CPU-load state.
  **The PC path explains none of the verdict variance.**

### Press edge to press datagram

- **Matching.** For each shot, the first history datagram after `press_ts_ms` matched in 423 of
  424 shots. One had no press datagram in the window, so it is unmatched. Epoch 202 was not
  checked for its press.
- **Delay.** Median 1.93 ms, sd 0.49, p5 1.18, p95 2.79, max 3.78, minimum 0.61 ms, and never
  negative.
- **Where the spread comes from.** `press_ts_ms` is truncated to whole milliseconds, which alone
  adds a uniform 0 to 1 ms (sd 0.29). Measured from `press_receipt_ts_ms`, the median is 1.20 ms
  and the sd 0.45 ms.
- **By verdict.** The medians are EARLY 1.96, EXCELLENT 1.93 and LATE 2.00 ms (Kruskal p = 0.59).
- **Why it is slower than the release.** The press is the player's physical Square relayed through
  RawInput and then the GUI-tick pipe write, not the precise mailbox. That makes it about 1.7 ms
  slower than the release, but just as flat. It sets the onset reference, and 0.5 ms of jitter
  there is irrelevant next to the 62-70 ms sd of the online onset.

## Clock offset: the adversarial check

The capture timestamp (npcap, reported by tshark `frame.time_epoch`) and `release_ms`
(`std::chrono::system_clock`, which is `GetSystemTimePreciseAsFileTime`) take the Windows wall
clock through different code paths.

**Bound from causality.** A release datagram must leave after `commandIssued` and before the fork
acknowledges the pipe write. The fork ACKs only after `sendto` has returned (`local_udp_accept`,
`feedbacksender.c:595-611`). So for each shot, the capture-minus-system offset c lies in
`[f2w - ackwait, f2w]`. Intersected over 423 shots, **c is in [-0.06, +0.16] ms**. The npcap and
system clocks therefore agree to within about 0.2 ms. The worst case, c = +0.16, would mean the
true fire-to-wire is about 0.06 ms at the median. Either way it is below 1.1 ms.

**Drift.** Across the 31 minutes it is +0.001 ms per minute, about 0.03 ms in total. That is
negligible.

**What survives any offset.** The sd, the percentile spreads, and every relation to the offset or
the verdict are differences between shots. A constant offset cancels out of all of them. Only the
absolute 0.22 ms median depends on c, and the causality bound fixes it within 0.2 ms.

**What does not survive.** Any analysis that anchors to `fire_epoch_ms` or `press_ts_ms` at sub-ms
resolution. Those fields carry up to about 1-2 ms of clock-read noise, as shown above.

## Caveats

- This measures the path up to the PC's NIC only. The ICS cable, the PS5's receive side, its input
  poll and the game are out of scope. The PS5 sends no ACK for feedback packets, so the capture
  cannot see the console's receipt.
- The PS5-to-PC stream timing was not examined here.
- One non-precise release (GUI-tick path) was included in the statistics. Excluding it moves the
  max to 0.69 ms and nothing else.
- The analysis scripts are scratch files, not committed. Per-shot rows were computed from the
  inputs listed above and can be regenerated.
