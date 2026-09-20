# External review prompt: is the residual shot-timing jitter closed on the software/network side?

Paste everything below this line into another model. It contains the measured facts, what has been
ruled out, and the exact questions. Ask for mechanisms plus a concrete test for each, not opinions.

---

You are reviewing a timing system and I want you to try to prove me wrong.

**System.** A PC watches a console game (NBA 2K27 on PS5) through a capture card at 60 fps and
times a button release for an on-screen shot meter. The meter is a vertical bar that fills from
0% to 100% in roughly 550 ms; the release must land in a green window of about 3 to 4 percentage
points just below the 100% tip (a perfect release is a guaranteed make; late is a guaranteed miss).
The PC sends the release over PlayStation Remote Play, using a patched client that writes the
controller packet directly onto the wire (PC-side fire-to-wire 0.45 ms, measured). The console and
PC are on a point-to-point wired gigabit link. The PC reads the bar with sub-pixel precision
(0.5 percentage points of noise), dates the 20% crossing by interpolation between 60 fps frames,
and fires at a fixed, learned interval after that crossing minus a fixed lead (about 293 ms).

**What is measured.**
1. The meter fills on a fixed convex curve: local rate 0.158 %/ms at 10 to 20% rising to 0.245 %/ms
   at 80 to 90%. The time from the 20% crossing to the 90% crossing is 334.5 ms with a
   shot-to-shot robust spread (rMAD) of 8.9 ms; from 30% it is 277.7 ms with rMAD 6.9 ms; from 40%
   225.9 ms with rMAD 5.8 ms. So the animation's own duration varies about 2 to 3% shot to shot.
2. Where the release lands, measured from pixels independent of the reader's ruler, has a
   per-session median that is stable at about 97.4% (range 96.1 to 97.9 over eight sessions) and a
   per-shot rMAD of about 1.2 to 1.5 percentage points, i.e. about 6 to 7 ms of time.
3. The landing sequence is memoryless: lag-1 autocorrelation is +0.07 (n=35 and n=40 sessions).
   Late and early shots do not come in runs, apart from one 6-shot early episode in one session.
4. No periodic structure: folding the true fire timestamp against the console's own video frame
   clock (obtained from the Remote Play stream) shows no 60 Hz, 30 Hz, 120 Hz or 250 Hz input-poll
   sawtooth (n=130, permutation p between 0.29 and 0.97 at every period tested), and the frozen
   fill values sit on a continuous 1-pixel grid, not on a render-frame comb.
5. The meter's per-shot speed measured early (20 to 30% interval) does not predict the remaining
   time well enough to correct with: a leave-one-out regression makes the residual worse (9.6 to
   22 ms rMAD) because the early interval is dominated by acquisition artifacts; only gross slow
   meters (30% slower) are detectable and are now handled.
6. Network jitter: 200 round trips, 0.03 ms robust spread, zero loss. The client sends the release
   as its own packet with no rate limiting behind periodic packets (audited in source).
7. Detection is not the limiter: when the meter is acquired in time the anchor's dating noise is
   below 0.1 percentage points.

**What is already fixed or explained** (do not spend time here): a registration template trained on
the previous game's faster meter that predicted the tip 100 to 180 ms early; a straight-line
extrapolator that over-estimates runway on a convex meter and vetoed the correct estimate; stale
previous-shot meters being re-read; a session-to-session drift that turned out to be the reader's
ruler, not the console.

**The question.** After the fixes above, the remaining per-shot error is about 6 to 7 ms rMAD with
no memory and no periodic structure, and the measured animation-duration variability alone
accounts for most of it. I have concluded that nothing observable from the PC or the network can
predict or cancel the remaining error, and that the only further lever would be hardware on the
controller path (not available now) or game-side settings that lengthen the meter in time.

Please answer, for each item, with a concrete mechanism and a concrete test I could run:
1. Is there any observable on the PC or the wire, before the release is sent, that could
   predict where the remaining ±6 ms will land on a given shot? Consider the Remote Play stream
   (video timestamps, audio, feedback acknowledgements), the console's network behaviour, and
   anything about the game's rendering that leaks into the captured video.
2. Is there any way to make the console register the release at a more deterministic instant
   (packet timing, packet contents, sending duplicates at chosen offsets, exploiting how the
   Remote Play client on the console applies controller state)?
3. Is my inference that the residual is mostly animation-duration variability sound, given the
   numbers in item 1 of the measurements? What would falsify it?
4. Is there a statistical estimator that could use the 60 fps fill samples from 20% up to the
   firing instant (about 100 ms of data, 6 samples, 0.5 percentage points of noise) to estimate
   this shot's duration scale better than the fixed curve, without the noise making it worse?
5. Anything I have not listed.

Be specific and skeptical. If the honest answer is "no, this is closed from the software side",
say so and explain which measurement would have to be different for you to answer otherwise.
