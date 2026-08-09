# Red-team: Chiaki-ng fork input / decode / takion path

**Scope:** read-only adversarial audit of the forked Chiaki-ng Remote-Play client that carries the
NBA-2K auto-green bot's release timing and decoded-frame export. A bug here = a live miss/misfire for
a paying customer.

**Source of truth.** The DEPLOYED patched source is NOT inside the repo tree. It lives in the sibling
directory `C:\Users\Administrator\Desktop\chiaki-ng-src` (this is what
`tools/chiaki/build_orion_chiaki.ps1` builds; the in-repo `vendor/chiaki-orion` is the *legacy*
FPS-probe fork per its own `ORION_PATCH_NOTES.md` and did **not** produce the deployed binary). All
file:line references below are in `chiaki-ng-src` unless noted. This was verified against the build
script's required-source list (`orionframeexport.cpp`, `orioninputbridge.cpp`, `ffmpegdecoder.c`).

**Deploy env checked:** `remote_play_client.py:325` sets `CHIAKI_ORION_FRAME_FPS=60`;
`native_orion/src/RemotePlaySession.cpp:1676` sets `120`. So the 30 fps *default* frame-export cap is
NOT active in production (see Negative N1).

---

## Data-flow recap (what carries the shot release)

1. Native fire thread (TIME_CRITICAL) computes the tip instant, writes an `OrionInputPacket` to the
   `\\.\pipe\orion_input` named pipe (`native_orion/src/OrionInputClient.cpp`).
2. Fork's `OrionInputBridge::run()` (HIGHEST prio) blocks in `ReadFile`, coalesces same-button dupes,
   and calls `chiaki_session_set_controller_state_nowait()` → drops the state into the feedback
   sender's **1-deep latest-wins slot** `orion_pending_state` (no wake, no takion lock).
3. Feedback-sender thread polls that slot every ~8 ms, applies it through the history-record path, and
   sends the button edge as a takion feedback-*history* packet (immediate socket write once called).
4. Decoded frames are tapped in `ffmpegdecoder.c`'s `frame_decoded_cb` and shipped to the bot over
   `\\.\pipe\orion_frames` by `OrionFrameExport`.

The release therefore crosses **two** quantizers (bridge coalesce, then the 8 ms feedback poll) and
**one** collapsing 1-deep slot before it reaches the wire. #8 and #9 live in that chain.

---

## Confirmed known issues (NOT re-reported as new)

### #8 — CONFIRMED. 0–8 ms feedback-sender poll quantizes the sub-tick fire.
The takion send itself is immediate (`takion.c:742-770` `takion_send_feedback_packet` → direct
`send()` at `takion.c:517-519`, only serialized by `gkcrypt_local_mutex`). The quantization is in
`feedbacksender.c`: the sender thread waits on `chiaki_cond_timedwait_pred(...)`
(`feedbacksender.c:339`) and, once it has ever seen an Orion inject, clamps its wake to
`FEEDBACK_STATE_TIMEOUT_MIN_MS = 8` (`feedbacksender.c:8, 336-337`). The `_nowait` slot is filled
without signaling (`feedbacksender.c:120-136`), so a release dropped into the slot waits up to a full
8 ms for the next poll before it is sent. The native side's microsecond-precise QPC fire is thus
smeared by up to ~8 ms of uniform jitter — half a frame at 60 fps. **Confirmed real.**

### #9 — CONFIRMED, with an added twist. The 1-deep slot defeats the bridge's edge-preservation.
`orion_pending_state` holds exactly one state (`feedbacksender.c:132-133`). The bridge writes press
then release as two *separate* `_nowait` calls microseconds apart (`orioninputbridge.cpp:184` in a
loop). If the feedback thread does not wake between them (both land inside one 8 ms poll window), the
second write overwrites the first and the drain (`feedbacksender.c:357-373`) sees only the final
state. When net press→release = idle→idle, the equals-guard at `feedbacksender.c:370` skips
`apply_state_locked` entirely → **no history event, the tap never reaches the PS5.**

Added twist worth flagging: the bridge's coalesce loop (`orioninputbridge.cpp:130-153`) was
specifically rewritten to STOP at a button/trigger edge so a press+release is *not* collapsed at the
bridge. That fix is **necessary but insufficient** — it is silently undone by the 1-deep slot
downstream. The same slot also yields a **stuck-button** variant: sequence press→release→press within
one poll window collapses to a single applied press (release overwritten), and since the final state
equals the just-applied press the release is never emitted → BOX stays held → held jump/shot =
misfire. Both are the #9 mechanism; the fix must widen the slot to an edge-preserving queue (mirror the
bridge's own logic) OR have the bridge signal on a button edge.

---

## NEW findings (ranked)

### F1 — HIGH / HARD. Reference-frame bitmap saturates and disables missing-reference recovery.
`lib/src/videoreceiver.c:10-44` (+ harm site `:272-298`), header `videoreceiver.h:36`.

The fork replaced the `int32_t reference_frames[16]` MRU with an added `uint16_t
reference_frames_bitmap` "Orion optimization". `add_ref_frame` sets `bitmap |= 1u << (key & 0xF)`
(`:15`) and never clears a bit; the only reset is at init (`:64`). `have_ref_frame` checks the bitmap
**first** and short-circuits `return true` on a set bit (`:36-38`), only falling back to the
authoritative linear scan when the bit is clear.

Because frame indices increment by 1, all 16 residues `(key & 0xF)` are seen within the first ~16
frames, after which the bitmap is permanently `0xFFFF` and `have_ref_frame()` returns `true` for
**every** frame index for the rest of the session. The structure is used inverted: a bitmap is only
sound as a fast *reject* (bit clear ⇒ definitely absent); here a set bit is a fast *accept* with no
verification, so it produces false positives.

Harm: in `chiaki_video_receiver_flush_frame`, the missing-reference handler is gated by
`if(slice.reference_frame != 0xff && !have_ref_frame(...))` (`:275`). With `have_ref_frame` always
true, the entire block `:275-298` is skipped: (a) no remap of a P-frame to an available older
reference, (b) no `frames_lost` increment, (c) `succ` stays true so the P-frame is pushed to the
decoder (`:302`) with a reference that was actually lost. Instead of a clean drop, the decoder
produces corruption that propagates until the next IDR.

Failure scenario at the tip: ordinary remote-play packet loss drops an earlier frame; the next
P-frame references it; the fork decodes it anyway → corrupted luma/blocks around the shot meter → the
CV reader either misreads the meter (wrong tip → misfire) or its geometry gate rejects several frames
(blind through the green window → missed shot). This runs for the whole game after frame ~16.

Confidence: HARD. Bits are provably never cleared; the always-true return provably skips the recovery
block; corruption-on-missing-reference is standard decoder behavior. Conditional on packet loss with a
reference gap, but that is common on Wi-Fi remote play.

Fix: delete the bitmap fast-accept and use the linear MRU as the authoritative check, OR make the
bitmap a fast-*reject* only (bit clear ⇒ return false; bit set ⇒ fall through to the linear scan) AND
clear the evicted frame's bit in `add_ref_frame` when the MRU drops an entry — but eviction can't clear
a bit safely because `& 0xF` aliases 16-apart indices, so simplest correct fix is to drop the bitmap.

### F2 — MED / HARD mechanism. History (button/release) send failure is silently dropped, no resend.
`lib/src/feedbacksender.c:184-189` (call site `:408-409`).

`feedback_sender_send_history_packet` calls `chiaki_takion_send_feedback_history(...)` (`:188`) and
**ignores the return value** — contrast `feedback_sender_send_state` (`:179-181`) which at least logs
on error. The history packet has already been popped from the ring (`feedbacksender.c:393-402`,
`history_packet_len--`) before the send, so a failed send is not requeued. The 4-event resend window
(`FEEDBACK_HISTORY_RESEND_EVENT_COUNT`, `:226-227`) only re-covers the event if a *subsequent* state
change generates another history packet. When the release is the terminal input of a shot (nothing
pressed after), no further history packet is formatted → the failed release is never resent →
**stuck BOX / lost release.**

A history send can fail on `gkcrypt_local_mutex` lock error or a socket `send()` error (e.g.
`EWOULDBLOCK` when the UDP send buffer is full under heavy inbound video during congestion — precisely
the moment a shot is being fired). No log, so it is invisible in field diagnostics.

Confidence: HARD that the return is ignored and not requeued; SOFT on frequency (needs a send failure
to coincide with a terminal release). Fix: check the return of `chiaki_takion_send_feedback_history`;
on failure, keep/re-push the history packet in the ring (do not decrement `history_packet_len` until a
successful send) and log at WARN.

### F3 — MED / SOFT. Export GPU readback runs under `decoder->mutex`, stalling render + next decode.
`lib/src/ffmpegdecoder.c:366-372`; `gui/src/orionframeexport.cpp:78-96`.

`frame_decoded_cb` is invoked while `decoder->mutex` is held (locked `ffmpegdecoder.c:276`, released
`:373`). For a hardware-decoded frame, `OrionFrameExport::exportFrame` performs
`av_hwframe_transfer_data` (GPU→CPU copy of a 1080p NV12 surface, ~1–3 ms) inside that callback
(`orionframeexport.cpp:84`). The renderer's `chiaki_ffmpeg_decoder_pull_frame` also takes
`decoder->mutex` (`ffmpegdecoder.c:385`), so during each export readback the present path and the next
`avcodec_receive_frame` block for the readback duration. At 60 fps export that is a ~1–3 ms stall on
most frames, adding decode/present jitter — and the whole point of the timing work is to remove jitter.
The readback must stay in the callback (deferring it races the zero-copy renderer's Vulkan layout
transitions, per the file's own comment), but it does not need `decoder->mutex`; it needs only to keep
`pending_frame` alive.

Confidence: SOFT (the stall is real and provable from the lock scope, but its live impact depends on
GPU readback latency and whether the render thread is actually contending each frame). Fix: hold a
short-lived `av_frame_ref` of `pending_frame`, release `decoder->mutex`, then do the readback on the
ref; or move the readback behind `cb_mutex` only.

### F4 — LOW / SOFT. 30 Hz keep-alive uses the SIGNALING setter with a possibly-stale state (double-tap race).
`gui/src/streamsession.cpp:1343-1355`.

While the bridge owns input, `SendFeedbackState` still re-asserts `orion_bridge->currentState()` every
33 ms via `chiaki_session_set_controller_state` — the **signaling** setter that takes
`feedback_sender_mutex` + `state_mutex` and wakes the sender (`session.c:339-346`,
`feedbacksender.c:103-115`). Two issues: (a) it partially re-introduces the takion wake the `_nowait`
path exists to avoid (bounded to 30 Hz, acknowledged as a "safety net"); (b) `currentState()` reads the
bridge's `state_`, updated per applied packet (`orioninputbridge.cpp:173-176`). If the keep-alive on
the Qt thread reads `state_` in the microsecond window after a press is applied but before the release
overwrites it, it injects an extra BOX press through the signaling path → an additional
history BOX-down/up pair → a spurious double tap (pump-fake-then-shoot type misfire).

Confidence: SOFT/LOW — the race window is microseconds against a 33 ms timer, so extremely rare, but
possible and non-deterministic. Fix: while owning, drive keep-alive through `_nowait` as well (never the
signaling setter), so the bridge's edge ordering is the single source of truth.

### F5 — LOW / SOFT. FPS-cap clock advances before a possibly-failing frame ref/readback.
`gui/src/orionframeexport.cpp:64-73` (`last_export_` set at `:73`, before the alloc/transfer at
`:75-96`). A frame that passes the cap gate but then fails `av_frame_alloc` or
`av_hwframe_transfer_data` still consumes the interval budget, so the next arriving frame may also be
gated. Self-corrects because frames keep arriving at decode rate; at worst it drops one extra frame on a
transient readback failure. Fix: set `last_export_` only after a successful enqueue.

### F6 — SOFT / MED (unverifiable here). Full reconnect resets synthetic PTS and export seq to 0.
`ffmpegdecoder.c:102, 335` (`synthetic_packet_pts` starts at 0, monotonic per decoder instance);
`orionframeexport.cpp:247` (`seq` starts at 0 per export instance). On a full session teardown/rebuild
(new `StreamSession` → new decoder + new `OrionFrameExport`), both the exported `hdr.pts` and `hdr.seq`
jump backward to 0. The header comment labels `pts` "diagnostics", but IF the bot's frame reader
dedupes/orders by `seq` or `pts` and assumes monotonicity, a reconnect would make it reject every new
frame as "old" until the counter climbs past the pre-reconnect high-water mark → blind window right
after a reconnect. Could not confirm the reader's behavior (bot CV side not in scope of these files).
Fix: have the reader treat a seq/pts reset as an explicit stream-restart, or stamp each export session
with a monotonic epoch id in the header.

---

## Stress-tested negatives (things I checked and believe are FINE)

- **N1 — 30 fps frame-export cap is not a live-timing risk in production.** The default is 30
  (`orionframeexport.cpp:23`), which would halve tip-timing temporal resolution (~33 ms blind windows).
  But the deploy launchers force 60 (`remote_play_client.py:325`) / 120
  (`RemotePlaySession.cpp:1676`). Confident, since both launch paths override the default. (If a future
  launcher drops the override, this becomes a real MED timing regression — flagging for defense in
  depth.)

- **N2 — No decoder PTS discontinuity from network resequencing on a *resumed* stream.** The decoder
  PTS is synthetic and monotonic (`ffmpegdecoder.c:329-335`), decoupled from the 16-bit network frame
  index. Takion's reorder-timeout skip (`takion.c:963, 1020-1037`) and crypt-available MAC re-drop
  (`takion.c:1129-1147`) drop *units*, and `videoreceiver` advances `synthetic_packet_pts` by
  `frames_lost` (`ffmpegdecoder.c:323-324`) to keep PTS monotone across gaps. So packet loss / reorder
  stalls do not produce a backward PTS jump. Confident. (The only PTS reset is a *full* decoder rebuild,
  which is F6, not a mid-stream event.)

- **N3 — No double hwframe readback.** `exportFrame` transfers a hw frame into a fresh CPU `ref`
  (`orionframeexport.cpp:84`); `av_frame_copy_props` does not copy `hw_frames_ctx`, so the writer
  thread's `if(src->hw_frames_ctx)` (`:193`) is false for that ref and does not transfer again.
  Confident from the FFmpeg contract that a transfer target is a CPU frame.

- **N4 — Bridge coalesce does not itself drop taps.** `orioninputbridge.cpp:142-153` peeks and only
  coalesces past packets whose button/L2/R2 state equals the current one, stopping at any edge. Verified
  by case analysis (press/release, release/press, ownership flips). The tap loss is downstream (#9),
  not here. Confident.

- **N5 — Slot mutex ordering is acyclic.** Producer (`_nowait`) takes only `orion_pending_mutex`
  (`feedbacksender.c:129`); consumer takes `state_mutex` then nests `orion_pending_mutex`
  (`:358-366`). No cycle; no deadlock. Confident.

---

## Recommended fix priority
1. **F1** (corrupt frames on loss) — delete the reference-frame bitmap fast-accept.
2. **#9** (lost tap / stuck button) — make the Orion slot edge-preserving (queue, or signal on button edge).
3. **F2** (silent release drop) — check + requeue history send, log on failure.
4. **#8** (8 ms smear) — phase-align or shorten the feedback poll for a pending Orion edge (e.g. let the
   bridge optionally signal `state_cond` on a button edge only, keeping stick spam on the poll path).
5. **F3, F4, F5, F6** — jitter / rare-race / reconnect hardening.
