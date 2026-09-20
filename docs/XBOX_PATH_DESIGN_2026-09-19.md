# Implementation update — 2026-09-19

Status: "Xbox beta" external-client WGC attachment implemented in the final-integration pass.
This section supersedes the historical estimates and stub assertions below; the remaining
original design is retained as analysis, not current implementation instructions.

- Setup offers an explicit Xbox Windows app window picker, refresh, and Open Xbox app.
- Microsoft owns login and console transport. Venice does not launch Chiaki, claim console
  acknowledgements, kill the Xbox app, or fall back to a desktop/other window.
- WGC requires fresh private pixels; closure, minimization, a title/PID change, restart,
  slow conversion, and geometry changes retire the relevant frame authority.
- Native X360 ViGEm remains the output. Physical-pad isolation (HidHide where needed) must
  be configured and verified in the Xbox app; this pass does not install/configure a driver.
- Xbox's lead stash is separate (`xbox_wgc`); selecting Xbox preserves the PS5 video source
  and lead. Xbox meter mode disables the blind fallback; actual meter evidence is required.
- Xbox has not been console-tested. No claim of decoder-pipe equivalence, measured Xbox
  latency, or PS5-level accuracy. Test controller identity, resize/close, and open-shot timing.

Official platform reference: https://www.xbox.com/en-US/consoles/remote-play

---

# An Xbox path for Xbox users — design, costing and recommendation

**Date:** 2026-09-19 · **Status:** design + audit. Nothing implemented; every code change
below is a snippet, not a patch applied to the tree.
**Ask (owner, 2026-09-19):** "an xbox path for xbox users."
**Architecture (owner-confirmed):** ride Microsoft's own Xbox Remote Play app. No capture
card, no Chiaki, no protocol reverse-engineering.


> **Line anchors:** taken against `HEAD e104e88` plus the 2026-09-19 working tree.
> `native_orion/src/OrionAppController.cpp` is under concurrent edit by other agents and its
> numbers drift by tens of lines between reads — every anchor below is therefore paired with
> the symbol or the quoted text, and **the text is authoritative, not the number.**
> `tests/test_xbox_path_hazards.py` pins the load-bearing ones by content so this document
> fails loudly rather than rotting quietly.

---

## 0. The recommendation, first

**Build it. Ship it as BETA / best-effort, behind its own toggle, after the PS5 path
ships — and do not promise Xbox users PS5 consistency until the timing bench says we can.**

The transport question is *solved* by the confirmed architecture. Microsoft's app is the
transport; it enforces same-network and Remote Play-enabled; we never touch their
protocol. What remains is not a transport problem at all:

> **The ship gate is TIMING FEASIBILITY.** The green-window fire is tuned against an
> Elgato HD60 X loop measured at **206–244 ms (median 213.7 ms, n=277)** delivering a
> **60.0 fps, 16.667 ms** cadence that the frame-phase estimator locks onto with
> **sd 0.05 ms median**. Xbox Remote Play inserts its own encoder, network and jitter
> buffer, and window capture inserts a compositor hop, *in front of* that estimator.
> Nothing in this repo has ever measured either.

So the plan below is: **measure first, on real Remote Play frames, with a bench harness,
before writing the product feature.** If the cadence survives, Xbox ships as a proper
mode. If it does not, Xbox ships as "best-effort — timing is less consistent than PS5",
which is still a real product for a customer who otherwise has nothing, but it must be
labelled honestly at the point of sale.

**Sequencing that does not risk the PS5 path:**

| phase | what | when |
|---|---|---|
| **1** | Xbox *controller* support on the existing PS5 rig | 3–5 days · can ship immediately |
| **2** | Timing feasibility bench on real Xbox Remote Play frames | 1 week of measurement · **the gate** |
| **3** | Xbox mode: WGC capture of the Remote Play window + ViGEm X360 + HidHide | 3–5 weeks · its own release |

---

## 1. The architecture

```
   Xbox Series/One console
        │  (Microsoft's Remote Play protocol — we never touch it)
        ▼
   ┌──────────────────────────────────────────────┐
   │  Microsoft Xbox Remote Play app (PC)         │   ← user signs in HERE
   │  window titles: "Remote Play", "Xbox Game    │
   │  Streaming", "Xbox Remote Play",             │
   │  "Xbox Console Companion"                    │
   └──────────────────────────────────────────────┘
        │ window pixels                    ▲ XInput
        │ (Windows.Graphics.Capture)       │ (ViGEm X360 virtual pad)
        ▼                                  │
   ┌──────────────────────────────────────────────┐
   │  Venice                                      │
   │   detector / meter reader  → timing engine   │
   │   physical pad (XInput, HidHide-cloaked)     │
   │   merge: user drives movement, bot fires     │
   └──────────────────────────────────────────────┘
```

Three swaps against the PS5 rig, and nothing else:

| layer | PS5 today | Xbox |
|---|---|---|
| video in | Elgato HD60 X capture card | **WGC capture of the Remote Play window** |
| transport | our patched Chiaki fork (`OrionStream`) | **Microsoft's app** |
| command out | 24-byte packet over `\\.\pipe\orion_input` (`OrionInputClient.cpp:374-441`) | **ViGEm X360 virtual pad** (`VirtualController.cpp:105-169`) |

Everything above the write — the 4 ms poll (`OrionAppController.cpp`, `inputPollTimer_` at `Qt::PreciseTimer`), the
timing decision (`AutomationEngine::scheduleFire`, `AutomationEngine.cpp:16955` (approx.)), the
sub-tick fire worker (`OrionAppController.cpp:1445-1650`), the whole detector and grading
stack — is console-agnostic and reused unchanged.

**The route enumerant already exists.** `LatencyControllerRoute::VigemXusb`
(`OrionTypes.h:34-39`) is already handled by `PreciseFirePolicy` at `:92-104`, `:163-176`
and `:343-363`, with live submit sites at `OrionAppController.cpp:1678`, `:13288`,
`:13319`, `:13355`. `preferXusb = true` is the default (`VirtualController.h:30`) and
`controllerType = "X360"` is the shipped value (`AppConfig.h:261-263`).

**Do not budget `remotePlayConsole = "Xbox"` as partial work.** `AppConfig.h:218-222`
declares it and `OrionAppController::setRemotePlayConsole` (`OrionAppController.cpp:8949-8964`)
*saves the string and writes a log line telling the user to set `frame_source=wgc` by
hand*. The key is referenced nowhere else in `native_orion/src`. It is a stub.

---

## 2. PRIORITY 1 — timing feasibility (this is the ship gate)

### 2.1 What the engine currently depends on

The release is not fired on a timer; it is fired against a reconstructed **frame phase**.
Two instruments carry that, and both are load-bearing:

```
FRAME PHASE: edges=2 phase_ms=13.08 sd=0.04 lock=0 period_ms=16.667 skips=0 source=phase …
CAPTURE PHASE: deadline_eta_ms=81.32 cycle_phase_ms=16.59 coherence=0.975 n=90 lock=0 target_ms=2.00 …
```

Measured baseline on the Elgato rig, `logs/orion_native.log` (2026-09-18T18:29Z → 09-19T02:48Z):

| quantity | baseline | why it matters |
|---|---|---|
| `FRAME PHASE sd` | **median 0.05 ms**, p90 0.10, max 2.74 (n=157) | the estimator's confidence in the 16.667 ms grid |
| `FRAME PHASE skips` | non-zero on **4 of 157** samples | dropped/duplicated console frames |
| `CAPTURE PHASE coherence` | median **0.803** (p10 0.070) | how repeatable the capture-to-detect phase is |
| `raw_fps` | median **60.0** | delivery rate |
| `raw_gap_max_ms` | median 21.8, p90 28.4, **max 87.3** | worst inter-frame gap in a health window |
| `raw_late` | median 0, max 20 | frames arriving after their slot |
| end-to-end loop | **213.7 ms median** (206–244, n=277) | the lead the engine subtracts |
| fire → wire | **0.376 ms median**, max 1.51 (n=267) | the output leg; already negligible |

The output leg is *not* the risk. **The input leg is.** The whole product is a prediction
of when the meter's tip will occur, made from frames whose arrival phase the engine
believes it knows to a twentieth of a millisecond.

### 2.2 What Xbox Remote Play + WGC changes, and what is unknown

| new stage | effect | measured anywhere? |
|---|---|---|
| Xbox console encoder | adds latency; may be variable-rate | **no** |
| network + Microsoft's **jitter buffer** | adds latency, and *deliberately re-times frames* | **no** |
| the app's renderer / present cadence | may not be 60.000 Hz; may present on the compositor's vsync | **no** |
| WGC frame pool → our ring | one compositor hop; frames arrive on a WGC worker thread (`wgc_backend.py:94`) | **no** |
| dynamic bitrate / resolution scaling | the detector's box geometry and fill ruler assume a stable raster | **no** |

The dangerous one is the **jitter buffer**. A capture card hands us frames whose spacing
*is* the console's output spacing — that is why `period_ms=16.667` locks. A streaming
client is free to hold a frame back and release two close together to smooth playback.
If it does, `FRAME PHASE` will not lock, `CAPTURE PHASE coherence` will collapse, and the
engine's fire-at-frame-centre target (`fire_target=frame_centre`, visible on every
`TIP RESERVATION` line) becomes meaningless — it would be aiming at a grid that is not
there.

The second dangerous one is **compression**. The meter reader is a colour reader operating
on thin structures, and this repo already has the scar: *"bt601/709 hue bug killed red;
green tip doesn't survive re-encode"* (memory: *Compressed reader shipped*). Remote Play
video is a second encode on top of the console's own. `compressed_meter_reader.py` exists
precisely for that class of source and is the right starting point — but it was tuned for
the Chiaki decoder's output, not Microsoft's.

### 2.3 The bench harness — what to measure and how

**Build this before the feature.** It is a measurement tool, not a product, and it can be
written entirely outside the blocked files.

`tools/timing/xbox_remote_play_bench.py` (new):

1. **Attach.** `WGCCaptureBackend(window_name=…)` (`wgc_backend.py:37-62`) against the
   located Remote Play HWND. Record `frame_number`, arrival timestamp and geometry for
   every frame for ≥ 10 minutes of real gameplay.
2. **Cadence.** From arrival timestamps produce the same statistics the engine produces:
   inter-frame gap histogram, implied `period_ms`, the phase residual and its sd, the
   count of skipped and duplicated frames. **Pass bar: a recoverable period within ±0.5 ms
   of 16.667 and a phase sd that lets `FRAME PHASE` reach `lock=1` at least as often as
   the Elgato baseline (57 of 157 samples).**
3. **Dropped/duplicated frames.** Hash each frame; count exact duplicates (the app
   re-presenting the same decoded frame) and gaps. **Pass bar: duplicate rate low enough
   that the reader's freshness gate — which already rejects stale frames — does not
   starve the ownership proof.**
4. **Added end-to-end latency.** The only honest way to get this without a console-side
   receipt is the method this repo already trusts: fire a known input through the ViGEm
   pad and measure from the command epoch (`FireEpochClock.h:58-66`,
   `timePreciseFireDispatch`) to the first captured frame in which the on-screen response
   appears. Run the *same* procedure on the PS5/Elgato rig the same day to get a paired
   comparison rather than an absolute. **Pass bar: report the delta; there is no fixed
   threshold, because the engine subtracts a learned lead — what it cannot survive is
   variance, not offset.**
5. **Variance is the real number.** Report the **sd** of that latency, not just the mean.
   The Elgato rig's learned lead has `lead_sd_ms=2.237` and `predictor_sigma_ms=14.037`
   on a typical `TIP RESERVATION` line. **Pass bar: Xbox's added sd must not push the
   combined sigma past the gate the engine already enforces at
   `AutomationEngine.cpp (the combined-sigma gate)`.** If it does, the engine will simply refuse to fire, and
   the product on Xbox becomes "it usually doesn't shoot" — the worst possible failure.
6. **Meter survivability.** Run `simple_meter_reader` / `compressed_meter_reader` offline
   over the captured frames using the existing replay framedump harness (memory: *Replay
   framedump harness*). **Pass bar: the green tip is detectable at the same rate as on
   decoder-pipe frames.**

**Honest framing for the result.** There are three outcomes and all three are shippable
positions, but they are different products:

* **Cadence locks, sd comparable** → Xbox ships as a normal mode.
* **Cadence locks, sd materially worse** → Xbox ships as **beta/best-effort**, with the
  lead widened and the sigma gate relaxed *only on the Xbox route*, and with the product
  page saying so.
* **Cadence does not lock** → Xbox cannot use frame-centre firing. The fallback is the
  blind hold law (the NO METER path), which grades far worse. That is not a mode worth
  selling; it is worth telling the customer we do not support Xbox yet.

### 2.4 The second, quieter risk: every constant is re-derived

Even in the best outcome, the Xbox rig starts with **`evidence_n = 0`**.
`models/latency_factory_prior.json` says it in its own words:

> "ONE route has ever been measured. Only `capture-card-pipe` carries `evidence_n>0`; the
> other three profiles are that same evidence propagated with explicit ignorance terms.
> They are **NOT** four independent measurements."

`capture-card-pipe`: mean 220.9 ms, sd 36.8, **evidence_n 87**.
`capture-card-vigem`: mean 229.2 ms, sd 41.5, **evidence_n 0** — and
`docs/LATENCY_FACTORY_PRIOR.md:95-112` is explicit that the +8.3 ms is *one assumed
console input-poll period*, not a measurement.

An Xbox build changes the console, the transport, the capture path **and** the output
route simultaneously. Budget the drills, not just the code: the press→tip priors, the hold
law, the meter geometry and the lead all have to be re-derived on the Xbox rig before a
single Xbox customer is sold.

---

## 3. Capture — WGC is the right API; PrintWindow is the trap

The reference prototype at `C:/Users/aaron/Desktop/Aim++/remote_play.py:122-126` and
`C:/Users/aaron/Desktop/Aim++/src/capture/window_capture.cpp:219-221` uses
`PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)` with a `BitBlt` fallback. **Take the
window-finding technique from it; do not take the capture method.**

**Why GDI is wrong here:**

* `PrintWindow`/`BitBlt` are GDI paths. Xbox Remote Play renders through a
  hardware-accelerated swap chain; GDI readback of such a window commonly returns a
  **black or stale frame**, and `PW_RENDERFULLCONTENT` is a best-effort hint, not a
  guarantee.
* If the window is DRM/protected-content flagged (`SetWindowDisplayAffinity` with
  `WDA_EXCLUDEFROMCAPTURE` / `WDA_MONITOR`), **both** GDI and WGC return black — this is a
  hard feasibility item and **must be checked on day one of Phase 2**, before anything
  else is built.
* GDI capture blacks out when the window is occluded. WGC does not — the orchestrator's
  own comment says so (`remote_play_orchestrator.py:3709-3712`: *"Unlike GDI, WGC does not
  black out when the window is occluded"*).

**We already have the right backend.** `wgc_backend.py` wraps Windows.Graphics.Capture:
target precedence `window_hwnd` > `window_name` (title substring) > monitor
(`wgc_backend.py:19`, ctor `:40-55`), frames delivered on a WGC worker thread into a
2-deep ring (`:56`, `:94`), cursor and border suppressed by default (`:53-54`). It is
already selectable from the orchestrator via `frame_source='wgc'` or `ORION_WGC=1`
(`remote_play_orchestrator.py:3713-3721`), and it already targets a **located HWND** with
a title fallback (`:3721`).

**What changes for Xbox:** only the window locator's vocabulary.
`find_remote_play_window` (`remote_play_client.py:333-385`) scores candidates against
`_SAFE_TITLE_MARKERS = ("chiaki", "orion stream")` (`remote_play_client.py:28-31`) and a
`_is_chiaki_process_window` check. Xbox needs its own marker set and process check.

**`remote_play_client.py` is a BLOCKED file — snippet only, not applied:**

```diff
@@ remote_play_client.py:28  _SAFE_TITLE_MARKERS
 _SAFE_TITLE_MARKERS = (
     "chiaki",
     "orion stream",   # the renamed OrionStream window; must beat the "orion" reject
 )
+
+# [XBOX PATH 2026-09-19] Microsoft's own Remote Play client is the transport on the Xbox
+# route: we capture ITS window and let it relay our ViGEm pad to the console. These are the
+# titles it has shipped under. Kept as a SEPARATE tuple, and consulted only when the
+# configured console family is Xbox, so a PS5 session can never attach to an Xbox window
+# (or vice versa) — the 2026-08 "reverts to remote play" class of bug is exactly what
+# happens when one locator serves two sources.
+_XBOX_TITLE_MARKERS = (
+    "xbox remote play",
+    "xbox game streaming",
+    "xbox console companion",
+    "remote play",          # the bare title Microsoft's current client uses; LAST, because
+                            # it also matches "PS Remote Play" — gate it on the process name
+)
```

…with the accompanying change that `_is_probably_remote_play_title`
(`remote_play_client.py:283-295`) takes a family argument, and that the bare
`"remote play"` marker only scores when the owning process is Microsoft's. Without the
process check, an Xbox session could attach to a PS Remote Play window and silently read
the wrong game.

**Geometry.** The capture-card path gets a fixed `1920x1080@60/YUY2` raster
(`Capture health: cap_mode=1920x1080@60/YUY2/buf-1`). A window is whatever size the user
left it, and can be resized mid-session. The detector's box latch
(`ORION_BOX_LATCH`, live in the ship config) and the fill denominator are **pixel**
quantities — memory: *Fill denominator is the detector box* records that 1 px of box
wobble is a 0.93 % slope error worth 3.2 ms. `wgc_backend.py` already tracks
`self._last_geom` (`:62`); the Xbox mode must **refuse to run, or force a re-lock, on any
geometry change**, and should require the Remote Play window be left at a fixed size.
This is a real, specific regression risk that the capture card does not have.

---

## 4. Input merge — the user drives, the bot fires

### 4.1 The requirement

On the Xbox route there is no private pipe. Both the human's movement input and the bot's
shot press must reach Microsoft's app as **one** controller, or the app will see two pads
and the console will see a second player.

The mechanism is the one the Python stack already implements:

1. **Read the physical pad** — `PhysicalControllerReader` (`virtual_controller.py:381-536`):
   loads `xinput1_4` → `xinput1_3` → `xinput9_1_0` (`:412-414`), `XInputGetStateEx`
   ordinal 100 for the Guide button (`:417-425`), slot autodetect (`:433-445`), separate
   `bLeftTrigger`/`bRightTrigger` (`:497-500`, `:527-528`), sticks `>>8` with Y negated
   (`:523-526`).
2. **Hide it** — `HidHideClient` (`virtual_controller.py:297-373`) opens `\\.\HidHide`
   (`:305`) and cloaks the physical device (`:328`, `:343`, `:365`) so Microsoft's app
   enumerates only the virtual pad.
3. **Merge and submit** — `ControllerIOHub` (`virtual_controller.py:538-612`) owns all
   three: `start(hide_physical=True)` connects ViGEm, activates HidHide, sets
   `timeBeginPeriod(1)` and runs a 1 ms `ControllerIO` poll thread at
   `THREAD_PRIORITY_TIME_CRITICAL` (`:578-590`, `:612+`); `set_remap_function(fn)`
   (`:559-562`) is the seam where the bot's shot overrides the human's state;
   `inject_state()` (`:603-604`) submits.

**This is exactly the merge the Xbox route needs, and it already exists.** The hub reads
the human, applies a remap function, and submits a single merged pad. The bot's shot is a
remap: hold/release the X button on the merged state at the engine's chosen instant.

### 4.2 What must change

`ControllerIOHub` today is a self-contained 1 ms Python loop. The engine's fire lives in
C++ in a sub-tick worker with an authority lease, route binding and a re-validation fence
(`OrionAppController.cpp:1575-1650`). **Do not re-implement the fire in Python.** The C++
side already has the ViGEm submit path (`OrionAppController.cpp:1673-1680` under
`LatencyControllerRoute::VigemXusb`), so the correct split is:

* **C++ keeps the fire.** `VirtualController` is already the X360 backend
  (`VirtualController.cpp:105-169`, `toXusb` at `:342-353` — it copies `state.buttons`
  directly because the internal mask *is* XUSB).
* **HidHide is the only genuinely new C++ component.** Port `virtual_controller.py:297-373`
  (a `\\.\HidHide` device handle plus the cloak/uncloak ioctls). ~2–4 days including the
  "driver not installed" path, which must be a clear, actionable UI state — without
  cloaking, the user gets doubled input, which is immediately visible and looks like our
  bug.
* **The Python hub stays a reference**, not a shipped component. Note it is already forced
  off in production: `RemotePlaySession.cpp` (`obj.insert("virtual_controller", false)`) inserts `"virtual_controller": false`.

### 4.3 Focus is now load-bearing

A virtual pad reaches whichever application has focus. The capture-card rig never had to
care — the pipe delivered to the fork regardless. On the Xbox route, **if the user
alt-tabs to our window, the shot goes nowhere.** The Xbox mode needs:

* a foreground check on the located HWND before arming;
* a visible "Remote Play window is not focused — shots will not reach the console" state;
* and — since our own UI would steal focus — a deliberate "hands off" posture while a
  session is live.

This is a UX problem with a timing consequence and it is easy to underestimate.

---

## 5. Input side — reading an Xbox pad

### 5.1 Which API reads the pad today

| rank | API | call site | selector priority |
|---|---|---|---|
| **primary** | **RawInput** (usage page 0x01, usage 0x04/0x05) | worker `OrionAppController.cpp:841-1030` (`class OrionRawInputWorker`); registration `:926-939`; decode `:977` | `physicalSony && RawInput` → **+300** (`ControllerDeviceSelector.cpp:22-24`) |
| fallback 1 | **XInput** (`XInputGetState`) | poll `OrionAppController.cpp:12254-12267`; mapping `:681-693` | **+170** (`ControllerDeviceSelector.cpp:25-27`) |
| fallback 2 | **WinMM** (`joyGetPosEx`) | `OrionAppController.cpp:599-616`; mapping `:497-517` | **+120** (`ControllerDeviceSelector.cpp:28-30`) |
| n/a | HID direct I/O | lightbar write `:464-467`; USB wake probe `:14721` (approx.) | never an input source |

There is **no** DirectInput, **no** Windows.Gaming.Input, **no** `HidP_*` parsed-data
decode and **no** hidapi anywhere in the tree.

### 5.2 Does an Xbox pad work today? Partly — and the partial path is the dangerous one

* **RawInput: hard NO.** `OrionAppController.cpp:296` returns an empty identity for any
  device path without `VID_054C`; `:969` then drops the report. An Xbox pad is invisible
  to the primary reader.
* **XInput: YES, correctly.** Selected at priority 170, decoded properly — separate
  triggers, SDK deadzones, correct sign convention. This is the Xbox reader.
* **WinMM: YES, and WRONGLY.** If XInput is unavailable or slot-starved the selector falls
  to WinMM, whose table is documented as **DualSense HID usage order**
  (`WinMmButtonMapping.h:15-17`). On an Xbox pad:
  * **btn1 (A) → `XINPUT_GAMEPAD_X` = `square()`** (`WinMmButtonMapping.h:38`) —
    **pressing A arms the shot path.**
  * btn2 (B) → `cross()`, btn3 (X) → `circle()` (`:39-40`).
  * btn7 (View) → `l2 = 255`, btn8 (Menu) → `r2 = 255` (`:50-51`).
  * btn9/btn10 (LS/RS click) → Back/Start (`:44-45`); btn11–14 dead.
  * **Axes:** `dwZpos → rightStickX` (`OrionAppController.cpp:563`) — on Xbox, Z is the
    **combined LT/RT axis**, so pulling a trigger produces a phantom right-stick
    deflection that poisons the Go-To lateral rule (`ShotIntentPolicy.h:68-69`);
    `dwRpos → rightStickY` (`:564`) is really right-X; the real right-Y (`dwUpos`) is
    **never read**.

  A silently-wrong WinMM Xbox pad is worse than no support. **Fence it, don't fix it.**

### 5.3 Hard-coded DualSense / Sony inventory

**VID / PID literals**

| file:line | literal | breaks how |
|---|---|---|
| `OrionAppController.cpp:296` | `path.contains("VID_054C")` → `return {}` | **fatal** — kills the primary reader for any non-Sony pad |
| `OrionAppController.cpp:299-302` | `PID_0CE6` / `PID_0DF2` / `PID_0E5F` → `"DualSense"` | no Xbox kind exists |
| `OrionAppController.cpp:304-306` | `PID_05C4` / `PID_09CC` → `"DualShock 4"` | ditto |
| `OrionAppController.cpp:308` | fallback `"PlayStation HID pad"` | unreachable for Xbox (VID already gated) |
| `OrionAppController.cpp:358-361` | RawInput enumeration scoring: `"DualSense"` +80, `"DualShock"` +50 | non-Sony never reaches it |
| `OrionAppController.cpp:673` | `caps.wMid == 0x054C` (WinMM `JOYCAPS`) | diagnostic only (`:12692`) |
| `OrionAppController.cpp:12238` | `VID_054C` → `raw.physicalSony` | Xbox never gets the +300 RawInput tier |
| `OrionAppController.cpp:14551-14560` | `"VID_054C&PID_"` + PID allowlist `{0CE6,0DF2,0E5F,05C4,09CC}` | USB-power scan always "never seen" → `UsbPadPowerPolicy.h:49-61` → the "Plug your DualSense in" refusal (`:7574-7577`) |
| `RemotePlaySession.cpp:443`, `:2489` | `SDL_GAMECONTROLLER_IGNORE_DEVICES="0x054c/0x0ce6,…"` + `SDL_JOYSTICK_HIDAPI_PS5=0`/`PS4=0` | Sony-only ignore-list. **On the Xbox route this is moot (no fork), but it must not be left to ship with an Xbox pad on a PS5 session, where the fork would open the pad directly and race the Orion route.** |

**HID report offsets** — `decodeSonyReport`, `OrionAppController.cpp:481-547`

| branch | file:line | offsets |
|---|---|---|
| defaults | `:491-496` | `axis=1 face=5 shoulder=6 special=7 l2=8 r2=9` |
| DualSense BT report `0x31` | `:499-500` | `axis=2 face=9 shoulder=10 special=11 l2=6 r2=7` |
| DualSense USB | `:501-503` | `axis=1 face=8 shoulder=9 special=10 l2=5 r2=6` |
| DS4 BT report `0x11` | `:505-506` | `axis=3 face=7 shoulder=8 special=9 l2=10 r2=11` |
| DS4 other | `:507-509` | `axis=1 face=5 shoulder=6 special=7 l2=8 r2=9` |
| "unknown" heuristic — still DualSense-shaped | `:510-512` | `axis=1 face=8 shoulder=9 special=10 l2=5 r2=6` |
| sticks read as 4 consecutive **single bytes** | `:519-522` | Xbox HID sticks are **16-bit LE** → garbage |
| `normalizeHidAxis` assumes 8-bit centred at 128 | `:240-247` | invalid for Xbox HID |
| wake probe hard-codes DS report id `0x01` | `:14716-14720` (approx.) | best-effort only |

**Button bit masks** — `OrionAppController.cpp:524-544`: `faceBits & 0x10` Square,
`0x20` Cross, `0x40` Circle, `0x80` Triangle; `shoulderBits & 0x01/0x02` L1/R1,
`0x04/0x08` digital L2/R2, `0x10/0x20` Create/Options, `0x40/0x80` L3/R3;
`specialBits & 0x01/0x02` PS/Touchpad. All DualSense/DS4 byte layout.
D-pad hat nibble (`:527-530`, `:249-262`) is generic HID and would work as-is.

**WinMM index table** — `WinMmButtonMapping.h:38-51`; see §5.2. The header already records
a live regression from assuming Xbox order (`:21-26`).

**PS button names on the wire** — `OrionInputClient.cpp:12-29` (`PS_BOX = 1<<2` is the shot
button), `OrionInputClient.h:209` (`kPsBox`), `OrionInputClient.cpp:447` (`kSquareBit`),
`:41-60` (`mapButtons` XInput→PS), `:398-399` (touchpad bit 14);
`VirtualController.cpp:359-374` (DS4 report build); `virtual_controller.py:14-29`
(`DS4Button`). **On the Xbox route none of these are on the path** — the ViGEm X360
backend copies `state.buttons` straight through (`VirtualController.cpp:342-353`) because
the internal mask already *is* XUSB (`OrionTypes.h:41-55`, `square() == XINPUT_GAMEPAD_X`
at `:74`).

**Lightbar / touchpad** — `OrionAppController.cpp:410-455` (DS4 report `0x05`, DualSense
report `0x02`, RGB offsets 45/46/47); `:456-462` already returns "LED unavailable for this
controller", which is the correct Xbox outcome. `ControllerState::touchpad`
(`OrionTypes.h:66`) becomes permanently false.

**Sony-worded user-facing strings (C++, editable)** — `OrionAppController.cpp:7576`,
`:12854` and the selector/health strings below; `ControllerDeviceSelector.h:30`,
`ControllerDeviceSelector.cpp:68`, `:115-123`; `winMmMappedButtonsLabel` `:521-539`;
USB-power messages `:14516-14530`.

**QML (owned by another agent — listed for awareness only):**
`qml/pages/DashboardPage.qml:100` ("Color your DualSense…") and the lightbar card
`:122-233`; `qml/components/FirstRunTour.qml:500`; `qml/components/NoMeterCard.qml:6`;
`qml/components/PressedBadge.qml:5,7,20`; `qml/pages/RemotePlayPage.qml:426,435,776,778`;
`qml/components/MeterConfigPanel.qml:8,222`; `qml/pages/DebugPage.qml:297,300,331`.
(`qml/pages/LegalGate.qml:111` names Sony for legal reasons — leave it.)

### 5.4 What the mapping layer must add

1. **An `physicalXbox` identity** alongside `physicalSony`, so an Xbox pad on XInput can
   be ranked at the top tier when the configured family is Xbox
   (`ControllerDeviceSelector.cpp:14-32`, candidate construction
   `OrionAppController.cpp:12228-12407`).
2. **Fence WinMM off** whenever the family is Xbox (§5.2).
3. **De-Sony the connect gate and strings**: `rawInputPresent_` (`:12219`),
   `hasRecentRawInput()` (`:14521-14532`), `padConnectGateAction`
   (`ControllerRoutingPolicy.h:125-141`), the USB-power advice, the health text.
4. **Rename nothing internal.** `square()` stays `XINPUT_GAMEPAD_X`; the UI says
   "Shoot (X / Square)". Renaming the enum would break every timing-log grep in the repo.
5. **Optional, measure first:** a RawInput Xbox branch. Only worth it if XInput's polling
   jitter ever measures worse than RawInput event delivery — and the 4 ms app tick likely
   dominates either way.

---

## 6. UX — the sign-in correction

**"Sign in with Microsoft" inside our app is cosmetic and must not be built as auth.**

The real sign-in happens inside Microsoft's Remote Play client, which also enforces the
account, the same-network requirement and the console's "Remote Play enabled" setting. We
have no account relationship with Microsoft and should not ask the user for Microsoft
credentials — asking would be both useless and a trust liability.

What our Xbox tab actually needs:

| element | behaviour |
|---|---|
| **Step 1** | "Open the Xbox app and start Remote Play to your console." A button that launches the app if installed; a link if not. |
| **Step 2 — Attach** | Locate the window (§3) and show **Attached / Not found**, with the matched title and size. This is the only "connection" state we own. |
| **Step 3 — Live check** | Confirm frames are arriving (`wgc_backend.get_frame_nonblocking`, `wgc_backend.py:148`) and that they are **not black** (the DRM check). Show fps and the locked cadence. |
| **Step 4 — Controller** | ViGEm pad created; HidHide active; physical pad cloaked. One line each, with a clear remedy if the driver is missing. |
| **Persistent warning** | "Remote Play window is not focused — shots will not reach the console" (§4.3). |
| **Honest badge** | While Xbox mode is beta: a visible "Beta — timing is less consistent than the PS5 path" marker, not buried in a tooltip. |

The existing stream-setup flow is built around "we launch and embed the client". Xbox is
the opposite: **we attach to a client the user launched.** That is a genuinely different
page, not a variant of the current one.

---

## 7. Phased plan and costing

### Phase 1 — Xbox **controller** on the PS5 rig · 3–5 days · **ships first, low risk**

An Xbox pad works as the physical input while the output stays the proven Chiaki pipe.
Nothing about the shipped timing path changes. Converts the "I own a PS5, I play with an
Xbox pad" customer today.

Work: §5.4 items 1–4, plus adding Xbox VIDs (`0x045e`) to the SDL ignore-list
(`RemotePlaySession.cpp:443`, `:2489`) so the fork cannot open the pad behind our back,
plus hiding lightbar/touchpad UI.

**Acceptance:** an owner drill of ≥ 30 banner-graded shots on an Xbox pad against a
same-day DualSense baseline, with the `tests/test_square_path_press_census.py` invariants
green on the produced log.

**Risk to the PS5 path: low** — it is all device selection, already hysteresis-guarded
(`ControllerDeviceSelector.cpp:73-112`).

### Phase 2 — Timing feasibility bench · ~1 week of measurement · **the gate**

§2.3. Deliverable is a report, not a feature. **Day one is the black-frame check** — if
WGC cannot capture the Remote Play window at all, the whole route is dead and no further
work should be spent.

Also worth doing in this phase, free: **measure the ViGEm route on the existing PS5 rig.**
Chiaki already maps an X360 pad to PS5 (`AppConfig.h:261-263` says so), the code path
already exists, and it would retire `capture-card-vigem`'s `evidence_n: 0` on a rig where
pipe and ViGEm can be compared head to head. No new code.

### Phase 3 — Xbox mode · 3–5 weeks · **its own release**

| item | effort | note |
|---|---|---|
| ViGEm X360 output backend | **0 days** | `VirtualController.cpp:105-169`, already the default target |
| `LatencyControllerRoute::VigemXusb` plumbing | **0 days** | already wired: `OrionTypes.h:34-39`, `PreciseFirePolicy.h:92-104`, `:163-176`, `:343-363` |
| WGC capture backend | **0 days** | `wgc_backend.py` exists and is orchestrator-selectable |
| Xbox window locator (titles + process check) | 2–3 days | §3 snippet; **blocked file** |
| Make `remotePlayConsole` real (route + capture source) | 3–5 days | stop the stub at `OrionAppController.cpp:8949-8964` |
| **HidHide in C++** | 3–5 days | the one genuinely new component; port `virtual_controller.py:297-373` |
| Focus / foreground handling + "not focused" state | 2–3 days | §4.3 |
| Geometry-change refusal / re-lock | 2–3 days | §3; `wgc_backend.py:62` already tracks geometry |
| Xbox setup page (attach, not sign-in) | 3–5 days | §6; QML is owned by another agent |
| Re-derive the timing constants on the Xbox rig | **1–2 weeks of drills** | priors, hold law, lead, meter geometry |

### Timing consequences, stated plainly

* **Phase 1 costs nothing.** Same output route, same tick, different Windows input API.
* **Phase 2 costs drill time and produces the number everything else depends on.**
* **Phase 3 starts at `evidence_n = 0` on four axes at once** — console, transport,
  capture path, output route. Expect the first Xbox build to grade worse than the PS5
  build until its priors are relabelled.
* **No option recovers a console input receipt.** `console_ack=0` is hardcoded on every
  delivery line today (`OrionAppController.cpp:13963`, `:14050`) and stays hardcoded on
  Xbox — Microsoft's app gives us no more receipt than the fork does. The
  `docs/POLL_PHASE_TRACKER.md:72` gap ("PC → console input transport: not instrumented")
  is unchanged.

---

## 8. Appendix — assets that already exist and must not be rebuilt

| asset | path |
|---|---|
| WGC window capture (correct API, HWND/title targeting) | `wgc_backend.py:37-155`; orchestrator selection `remote_play_orchestrator.py:3713-3721` |
| ViGEm X360/DS4 backend (C++, default target X360) | `native_orion/src/VirtualController.{h,cpp}`; `VirtualController.cpp:105-169`, `toXusb` `:342-353` |
| ViGEm DLL (shipped, auto-copied) | `native_orion/vendor/vigem/ViGEmClient.dll`; CMake copy `native_orion/CMakeLists.txt:555-570`; repo-root copy is the Python stack's first-choice load (`virtual_controller.py:125-142`) |
| ViGEm client (Python, XBOX360 + DS4 target constants) | `virtual_controller.py:96-101`, `:106-163` |
| **HidHide client (the port source)** | `virtual_controller.py:297-373` |
| XInput physical reader (Python reference) | `virtual_controller.py:381-536` |
| XInput physical reader (C++, shipping) | `OrionAppController.cpp:681-693`, `:12254-12304` |
| Merge hub (read + remap + submit, 1 ms loop) | `virtual_controller.py:538-612` |
| Route enum + policy arms | `OrionTypes.h:34-39`; `PreciseFirePolicy.h:92-104`, `:163-176`, `:343-363` |
| Window locator (to be extended for Xbox) | `remote_play_client.py:28-31`, `:283-295`, `:333-385` |
| Timing instrument (route-agnostic) | `FireEpochClock.h:21-105`; stamps `OrionAppController.cpp:1664-1669`, `:1675-1679`, `:1692-1696` |
| Compressed-source meter reader (the starting point for re-encoded video) | `compressed_meter_reader.py` |

**Reference prototype (separate project — READ-ONLY, take the technique only):**
`C:/Users/aaron/Desktop/Aim++/remote_play.py` — `find_remote_play()` title list at `:82-87`
(the source of the Xbox titles above), `WindowCapture` at `:97-130`, `VirtualGamepad`
(ViGEm X360) at `:168-195`; `C:/Users/aaron/Desktop/Aim++/src/capture/window_capture.cpp:219-221`.
Its capture method (`PrintWindow(PW_RENDERFULLCONTENT)` with a `BitBlt` fallback) is the
part **not** to copy — see §3.

A sweep of the other Desktop siblings (`EXPLOITS/helios_bypass`,
`NEXUS SOURCE CODES/NexusVision-legacy-*`, `chiaki-ng-src`, `Aletheia`, `mcp-bridge`)
found **no ViGEm capability this repo does not already have** — the hits are copies of the
same `VirtualController` code or loose copies of the same DLL.
