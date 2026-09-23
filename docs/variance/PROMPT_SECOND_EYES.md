# Second eyes on Venice's online timing variance (2026-09-22)

You are reviewing a finding, not starting from zero. Try to BREAK it first; only then look for
what it missed. Read-only: no source edits, no builds, no launching the app.

## Context
Venice (repo `C:\Users\aaron\Desktop\NexusVision`) times NBA 2K27 jump-shot releases on PS5.
Offline the bot hits ~95 % EXCELLENT; online 58-73 %. Read, in order:
1. `docs/variance/ONLINE_GRADING_CLOCK_2026-09-22.md` - the finding.
2. Shot records: `D:\NexusVision\shot_records\*.jsonl` (fields: press_ts_ms, onset_ms = press ->
   meter first seen, release_after_press_ms = hold, banner.timing = EARLY/EXCELLENT/LATE,
   onset_source; `session_20260920_170027` is OFFLINE, the rest online).
3. Tools: `tools/timing/net_probe.py`, `tools/timing/net_join.py`, and the ONSET FF code in
   `native_orion/src/OnsetFeedforward.h` + the scheduleFire choke point in `AutomationEngine.cpp`
   (grep "ONSET FF").

## The claim to attack
Online, grade = hold - (1-beta)*onset + noise with **beta ~= 0.45 (90 % CI 0.27-0.69)**, fitted by
ordered probit on 816 session-centred online standstills. Meaning ~half the online grade follows
the PRESS clock, not the visible meter. Latent sigma ~44 ms; onset-driven 19 ms sd; an
**unexplained 40 ms sd that does not exist offline**. Implied levers: onset feedforward gain 0.45 /
clamp 40 / two-sided + fire ~7 ms earlier (~71 -> ~77 % EXC, in-sample); and the jackpot = whatever
the 40 ms is (network, server tick, server processing) - unmeasured until the next capture.

## Questions (answer with numbers, not opinions)
1. Is beta real or an artefact? Candidate artefacts: session-centring, the outlier filters
   (|onset dev|>200, |meter_rel dev|>60), onset measurement error (60 Hz quantisation, detection lag),
   the bot's own feedback loops (banner trim, onset FF already live on 09-21 sessions), the probit
   link / equal-variance assumption, ordinal cut-points shared across sessions. Refit with
   alternatives (logit, per-session cut-points, a random-effects version, onset from a different
   source) and report how beta moves.
2. Is the "unexplained 40 ms" really online-only, or partly misfit? The offline session has 2 misses
   in 39 - say what an offline comparison can and cannot support.
3. What else in the records predicts the grade beyond hold and onset (rhythm, tempo, coverage,
   distance, time within session, previous verdict, early meter shape)? Report out-of-sample, with
   multiplicity control. Prior work found most covariates dead - do not re-report those as new.
4. Given the net_probe design (passive ICS capture, headers only, ICMP to court + gateway), what is
   it blind to, and what cheap measurement would identify the 40 ms fastest?
5. Anything that makes mid-80s % EXCELLENT online reachable or provably unreachable.

Write `docs/variance/SECOND_EYES_REVIEW.md`: verdict on beta (holds / weakened / refuted), each
answer with its evidence, and a ranked list of next measurements.
