# Inbound Meter Delay — Implementation Plan (v3)

*v2 rewritten with shot-aware dynamic delay. All values empirically validated 2026-08-01.*

## What We Proved

Live testing with `tools/delay_test.py` against PS5 IP `192.168.137.100`:

| Test | Result |
|------|--------|
| **Inbound delay** (server → PS5) | Meter decouples from shot animation. Effect starts at ~150ms. |
| **Outbound delay** (PS5 → server) | No visible effect, even at 150ms. Not implemented. |
| **Sweet spot** | 165ms inbound — meter clearly delayed, gameplay smooth. |
| **Ceiling** | ~250ms — above this, gameplay feels laggy. |
| **Side effects at 165ms** | None. No teleporting, no desync, no rubber-banding. |

**Conclusion:** Client-side effect. Delaying inbound game packets causes the
PS5's latency compensation to decouple the meter from the shot animation.

## Shot-Aware Delay — The Key Innovation

**Problem with always-on delay:** At 165ms constant inbound delay, the meter
effect works perfectly, but ALL gameplay feedback is delayed — movement feels
sluggish because server acknowledgments arrive 165ms late.

**Solution:** Only inject delay when a shot is happening. The bot already knows
exactly when a shot is being armed (HoldState transitions in AutomationEngine).

```
Normal gameplay (running, dribbling, passing):
    delay = 0ms → full responsiveness, zero input lag

Shot detected (bot arms / meter about to appear):
    delay ramps to 165ms → meter decouples, bot gets clean read

Shot completes (release fired / meter gone):
    delay ramps back to 0ms → responsiveness returns
```

**Result:** Responsive movement 95% of the time. Delayed meter only during the
~2 second shot window when the bot needs it. Best of both worlds.

### Ramp Timing

The ramp must complete before the meter appears. Timeline of a typical shot:

```
t=0ms     Bot detects shot initiation (HoldState → Armed)
t=0ms     Controller starts ramping: 0 → 165ms
t=100ms   Delay at ~55ms  (fast ramp: 55ms/tick for shot-start)
t=200ms   Delay at ~110ms
t=300ms   Delay at ~165ms ✓ target reached
t=300-500ms  Meter appears on screen (animation wind-up takes ~300-500ms)
          → Delay is already at 165ms when meter shows. Perfect.

Shot release fires...

t+0ms     Controller starts ramping down: 165 → 0ms
t+100ms   Delay at ~130ms  (slower ramp down: 35ms/tick)
t+200ms   Delay at ~95ms
t+300ms   Delay at ~60ms
t+400ms   Delay at ~25ms
t+500ms   Delay at 0ms ✓ full responsiveness restored
```

**Fast ramp UP (55ms/tick):** We need to hit 165ms before the meter appears
(~300ms after shot initiation). 3 ticks × 55ms = 165ms. Tight but proven safe
since the PS5 client handles RTT spikes gracefully (tested: instant jump to
150ms caused no visible glitch — just a brief hitch absorbed by the client's
internal smoothing).

**Slow ramp DOWN (35ms/tick):** Less aggressive on the way down to avoid the
server seeing an abrupt RTT drop. 5 ticks from 165 → 0. Gameplay responsiveness
returns over ~500ms — imperceptible to the user.

## Operating Parameters

```
DEFAULT_DELAY      = 165ms    // tested sweet spot
ADAPTIVE_MIN       = 150ms    // floor — below this the effect isn't visible
ADAPTIVE_MAX       = 170ms    // ceiling for auto-adjustment in active state
MANUAL_MIN         = 100ms    // user override floor
MANUAL_MAX         = 250ms    // user override ceiling
RAMP_UP_RATE       = 55ms/tick  // fast ramp for shot start (~300ms to target)
RAMP_DOWN_RATE     = 35ms/tick  // slower ramp after shot (~500ms to zero)
TICK_INTERVAL      = 100ms    // controller tick rate
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  nexus_svc.py (SYSTEM service)                               │
│                                                              │
│  WinDivert dual-handle setup:                                │
│    Handle 1: SNIFF (unchanged) — telemetry broadcast         │
│    Handle 2: ★ INTERCEPT inbound game UDP → delay → reinject │
│                                                              │
│  ★ InboundDelayBuffer: holds server→PS5 packets              │
│  ★ Accepts set_meter_delay command                           │
│  ★ Intercept handle stays open during game, delay value      │
│    changes dynamically (0 ↔ 165ms) per shot state            │
└──────────────┬───────────────────────────────────────────────┘
               │ TCP 127.0.0.1:47291
               ▼
┌──────────────────────────────────────────────────────────────┐
│  NetworkBridge.cpp                                           │
│                                                              │
│  ★ Reads delay telemetry from packet events                  │
│  ★ Sends set_meter_delay commands to nexus_svc               │
└──────────────┬───────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────┐
│  ★ MeterDelayController.cpp (new)                            │
│                                                              │
│  Dual-layer state machine:                                   │
│    Session layer: IDLE → PROBING → READY → BACKOFF           │
│    Shot layer:    STANDBY ↔ ENGAGING ↔ DISENGAGING           │
│                                                              │
│  Session layer manages baseline + network health             │
│  Shot layer manages per-shot ramp up/down                    │
│                                                              │
│  ★ Receives shot state from AutomationEngine                 │
└──────────────┬───────────────────────────────────────────────┘
               │ signals
               ▼
┌──────────────────────────────────────────────────────────────┐
│  OrionAppController                                          │
│                                                              │
│  ★ Wires AutomationEngine shot state → MeterDelayController  │
│  ★ Q_PROPERTYs for QML                                       │
└──────────────┬───────────────────────────────────────────────┘
               │ bindings
               ▼
┌──────────────────────────────────────────────────────────────┐
│  DashboardPage.qml — Network tab                             │
│                                                              │
│  ★ "Meter Delay" card: session state, shot state, delay      │
└──────────────────────────────────────────────────────────────┘
```

---

## Controller State Machine — Dual Layer

The controller has two independent layers: a **session layer** that manages
the baseline and network health across the game, and a **shot layer** that
manages the per-shot ramp up/down.

### Session Layer

```
         ┌─────────┐
         │  IDLE   │◄──── game end / disabled / no court IP
         └────┬────┘
              │ enabled && playingGame && courtIp
              ▼
         ┌─────────┐
         │ PROBING │  Measure baseline jitter/RTT (~2s, ≥8 samples)
         │         │  Delay = 0 (no shots should fire during probe)
         └────┬────┘
              │ baseline locked
              ▼
         ┌─────────┐
         │  READY  │  Delay system armed. Shot layer takes over.
         │         │  Micro-adjusts target within 150-170ms band.
         └────┬────┘  Monitors network health continuously.
              │
              │ jitter spike / packet loss
              ▼
         ┌─────────┐
         │ BACKOFF │  Reduce target by 20ms. Cooldown 2s.
         │         │  Shot layer still runs but with lower target.
         └────┬────┘
              │ stable 2s
              └──► READY (restore target)
```

| State | Behavior |
|-------|----------|
| **IDLE** | Feature off. Delay = 0. Intercept handle closed. |
| **PROBING** | Collect baseline. No delay. ~2 seconds. |
| **READY** | System armed. Shot layer active. Target = 165ms (auto) or manual. |
| **BACKOFF** | Network degraded. Target reduced. Shot layer still runs at lower target. |

### Shot Layer (only active when session = READY or BACKOFF)

```
         ┌──────────┐
         │ STANDBY  │◄──── shot completes + ramp-down done
         │ delay=0  │
         └────┬─────┘
              │ shotActive = true (bot arms shot)
              ▼
         ┌───────────┐
         │ ENGAGING  │  Ramp UP at 55ms/tick → target
         │ 0 → 165ms │  ~300ms to full delay
         └────┬──────┘
              │ delay >= target
              ▼
         ┌───────────┐
         │  LOCKED   │  Hold at target. Meter is decoupled.
         │ delay=165 │  Bot reads meter with clean signal.
         └────┬──────┘
              │ shotActive = false (release fired)
              ▼
         ┌──────────────┐
         │ DISENGAGING  │  Ramp DOWN at 35ms/tick → 0
         │ 165 → 0ms    │  ~500ms to full responsiveness
         └──────────────┘
              │ delay <= 0
              └──► STANDBY
```

| State | Behavior |
|-------|----------|
| **STANDBY** | Delay = 0. Full gameplay responsiveness. Waiting for shot. |
| **ENGAGING** | Ramping up at 55ms/tick. Bot just initiated a shot. |
| **LOCKED** | Holding at target (165ms). Meter is decoupled. Bot reads and fires. |
| **DISENGAGING** | Ramping down at 35ms/tick. Shot complete, restoring responsiveness. |

### How Shot State Flows In

The `AutomationEngine` already tracks `HoldState`:
- `Idle` / `Warmup` → no shot → STANDBY
- `Armed` / `Holding` → shot in progress → ENGAGING/LOCKED
- Release fired → DISENGAGING

The `MeterDelayController` receives a simple `setShotActive(bool)` signal:
```cpp
// In OrionAppController wiring:
connect(&automation_, &AutomationEngine::holdStateChanged, this, [this]() {
    bool shooting = (automation_.holdState() == HoldState::Armed
                  || automation_.holdState() == HoldState::Holding);
    meterDelay_->setShotActive(shooting);
});
```

### Timing Diagram — Full Shot Cycle

```
Time    Event                        Delay    Shot Layer    Session
─────   ──────────────────────────   ─────    ──────────    ───────
 0ms    Running around, dribbling     0ms     STANDBY       READY
        (full responsiveness)

 ---    Player initiates shot         0ms     STANDBY       READY
 0ms    Bot detects → Armed           0ms     →ENGAGING     READY
100ms   Tick: delay += 55             55ms    ENGAGING      READY
200ms   Tick: delay += 55            110ms    ENGAGING      READY
300ms   Tick: delay += 55            165ms    →LOCKED       READY

350ms   Meter appears on screen      165ms    LOCKED        READY
        (meter is decoupled ✓)

600ms   Bot detects green window     165ms    LOCKED        READY
650ms   Bot fires release            165ms    →DISENGAGING  READY
750ms   Tick: delay -= 35            130ms    DISENGAGING   READY
850ms   Tick: delay -= 35             95ms    DISENGAGING   READY
950ms   Tick: delay -= 35             60ms    DISENGAGING   READY
1050ms  Tick: delay -= 35             25ms    DISENGAGING   READY
1150ms  Tick: delay -= 25              0ms    →STANDBY      READY

        Back to full responsiveness
```

---

## File-by-File Implementation

### 1. `nexus_svc.py` — Inbound Packet Interception

**Current:** Single WinDivert handle, `NETWORK_FORWARD` layer, `SNIFF` flag.

**New:** Two handles running simultaneously:
- **Handle 1 (unchanged):** SNIFF for telemetry broadcast
- **Handle 2 (new):** INTERCEPT for inbound game UDP

**Key difference from v2:** The intercept handle stays open for the entire game
session. The delay value changes dynamically (0 ↔ 165ms) as the shot layer
engages and disengages. Opening/closing the handle per shot would be too slow
and could drop packets.

```python
class _InboundDelayBuffer:
    """Holds inbound game packets (server → PS5) and re-injects after delay."""

    def __init__(self):
        self._delay_ms = 0.0
        self._buffer = []              # (release_time, packet)
        self._lock = threading.Lock()
        self._handle = None
        self._running = False
        self._stop = threading.Event()

    def start(self, console_ip: str, court_ip: str):
        """Open intercept handle. Stays open for the game session."""
        if self._running:
            return
        filt = (
            f"ip and udp and "
            f"ip.DstAddr == {console_ip} and "
            f"ip.SrcAddr == {court_ip} and "
            f"udp.SrcPort >= 30000 and udp.SrcPort <= 30020"
        )
        self._handle = pydivert.WinDivert(
            filt, layer=pydivert.Layer.NETWORK_FORWARD
        )
        self._handle.open()
        self._running = True
        self._stop.clear()
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def stop(self):
        """Flush all and close. Called on game end."""
        self._stop.set()
        self._flush_all()
        if self._handle:
            try:
                self._handle.close()
            except Exception:
                pass
        self._handle = None
        self._running = False

    def set_delay(self, delay_ms: float):
        """Called frequently as shot layer ramps up/down."""
        self._delay_ms = max(0.0, min(300.0, delay_ms))
        if self._delay_ms == 0.0:
            self._flush_all()

    @property
    def active(self):
        return self._running

    @property
    def current_delay(self):
        return self._delay_ms

    @property
    def buffer_depth(self):
        with self._lock:
            return len(self._buffer)

    def _capture_loop(self):
        while not self._stop.is_set():
            try:
                pkt = self._handle.recv()
            except Exception:
                if self._stop.is_set():
                    break
                continue
            delay_s = self._delay_ms / 1000.0
            if delay_s <= 0:
                self._handle.send(pkt)
                continue
            release_at = time.perf_counter() + delay_s
            with self._lock:
                self._buffer.append((release_at, pkt))

    def _flush_loop(self):
        while not self._stop.is_set():
            now = time.perf_counter()
            to_send = []
            with self._lock:
                idx = 0
                for i, (t, _) in enumerate(self._buffer):
                    if t <= now:
                        idx = i + 1
                    else:
                        break
                if idx > 0:
                    to_send = [p for _, p in self._buffer[:idx]]
                    self._buffer = self._buffer[idx:]
            for p in to_send:
                try:
                    self._handle.send(p)
                except Exception:
                    pass
            time.sleep(0.0005)  # 0.5ms flush resolution

    def _flush_all(self):
        with self._lock:
            remaining = [p for _, p in self._buffer]
            self._buffer.clear()
        for p in remaining:
            try:
                self._handle.send(p)
            except Exception:
                pass
```

**New commands:**
```json
{"cmd": "set_meter_delay", "delay_ms": 165.0}
{"cmd": "start_meter_intercept"}
{"cmd": "stop_meter_intercept"}
```

- `start_meter_intercept`: Opens the intercept handle when game starts.
  Requires `console_ip` and `court_ip` to be set.
- `set_meter_delay`: Changes the delay value. Called rapidly during ramp.
  Handle must already be open.
- `stop_meter_intercept`: Flushes buffer, closes handle. Called on game end.

**New telemetry fields:**
```json
{
  "event": "packet",
  ...existing...,
  "meter_delay_active": true,
  "meter_delay_ms": 165.0,
  "meter_buffer_depth": 2
}
```

### 2. `MeterDelayController.h` — New Header

```cpp
#pragma once
#include <QObject>
#include <QTimer>
#include <QElapsedTimer>
#include <deque>

namespace orion {

class MeterDelayController : public QObject {
    Q_OBJECT
public:
    // Session layer
    enum class SessionState { Idle, Probing, Ready, Backoff };
    Q_ENUM(SessionState)

    // Shot layer
    enum class ShotState { Standby, Engaging, Locked, Disengaging };
    Q_ENUM(ShotState)

    explicit MeterDelayController(QObject* parent = nullptr);

    // Configuration
    void setEnabled(bool enabled);
    bool enabled() const;
    void setManualDelayMs(double ms);
    double manualDelayMs() const;
    bool isManualMode() const;

    // External state inputs
    void setPlayingGame(bool playing);
    void setCourtIpKnown(bool known);
    void setShotActive(bool active);   // from AutomationEngine

    // Network telemetry input
    void updateTelemetry(double rttMs, double jitterMs,
                         bool rttVerified, bool packetLoss);

    // Read state
    SessionState sessionState() const;
    ShotState shotState() const;
    double currentDelayMs() const;
    double targetDelayMs() const;
    QString sessionStateString() const;
    QString shotStateString() const;
    QString reasonText() const;

signals:
    void sessionStateChanged(SessionState s);
    void shotStateChanged(ShotState s);
    void delayChanged(double delayMs);     // → NetworkBridge → nexus_svc
    void interceptStart();                  // → open intercept handle
    void interceptStop();                   // → close intercept handle
    void telemetryChanged();                // → QML bindings

private slots:
    void onTick();

private:
    void transitionSession(SessionState s, const QString& reason);
    void transitionShot(ShotState s);
    void tickSession();
    void tickShot();
    void checkBackoff();
    void computeTarget();

    // ── Operating parameters (empirically determined 2026-08-01) ──
    static constexpr double kDefaultDelay    = 165.0;
    static constexpr double kAdaptiveMin     = 150.0;
    static constexpr double kAdaptiveMax     = 170.0;
    static constexpr double kManualMin       = 100.0;
    static constexpr double kManualMax       = 250.0;
    static constexpr double kRampUpRate      = 55.0;   // ms/tick — reach 165 in ~300ms
    static constexpr double kRampDownRate    = 35.0;   // ms/tick — reach 0 in ~500ms
    static constexpr double kBackoffStep     = 20.0;
    static constexpr int    kTickMs          = 100;
    static constexpr int    kMinProbes       = 8;
    static constexpr double kMinProbeS       = 2.0;
    static constexpr double kJitterMult      = 3.0;
    static constexpr double kRttMult         = 2.5;
    static constexpr int    kCooldownTicks   = 20;     // 2s

    // ── Session state ──
    bool enabled_ = false;
    bool playing_ = false;
    bool courtKnown_ = false;
    SessionState session_ = SessionState::Idle;
    QString reason_;

    QTimer tick_;
    QElapsedTimer probeTimer_;

    struct Sample { double rtt; double jitter; };
    std::deque<Sample> probes_;
    double baseJitter_ = 0.0;
    double baseRtt_ = 0.0;

    double target_ = kDefaultDelay;
    double manual_ = 0.0;

    int cooldown_ = 0;
    int stableTicks_ = 0;

    // ── Shot state ──
    ShotState shot_ = ShotState::Standby;
    bool shotActive_ = false;      // raw input from AutomationEngine
    double current_ = 0.0;         // actual delay value being sent

    // ── Latest telemetry ──
    double lastRtt_ = 0.0;
    double lastJitter_ = 0.0;
    bool lastLoss_ = false;
};

} // namespace orion
```

### 3. `MeterDelayController.cpp` — Implementation

**Tick logic (runs every 100ms):**

```
void MeterDelayController::onTick() {
    tickSession();  // manage baseline, network health, target
    tickShot();     // manage per-shot ramp up/down
}

// ── Session layer ──

tickSession():
    IDLE:
        if (enabled && playing && courtKnown):
            emit interceptStart()
            → PROBING

    PROBING:
        if (!playing || !courtKnown) → IDLE
        collect sample
        if (samples >= 8 && elapsed >= 2s):
            compute baseline
            computeTarget()
            → READY

    READY:
        if (!playing || !courtKnown):
            emit interceptStop()
            → IDLE
        checkBackoff()
        // Micro-adjust target within 150-170 (auto mode only)
        if (!isManualMode()):
            if (lastJitter < baseJitter * 1.5 && target < kAdaptiveMax):
                target += 0.5
            elif (lastJitter > baseJitter * 2.0 && target > kAdaptiveMin):
                target -= 1.0

    BACKOFF:
        if (!playing) → IDLE
        target -= kBackoffStep
        if (target < kManualMin) target = kAdaptiveMin
        cooldown--
        if (cooldown <= 0 && stableTicks >= kCooldownTicks):
            computeTarget()  // restore original target
            → READY

// ── Shot layer (only runs when session = READY or BACKOFF) ──

tickShot():
    if (session != READY && session != BACKOFF):
        if (current > 0):
            current = 0
            emit delayChanged(0)
        shot = STANDBY
        return

    STANDBY:
        // current stays at 0
        if (shotActive):
            → ENGAGING

    ENGAGING:
        current += min(kRampUpRate, target - current)
        emit delayChanged(current)
        if (current >= target):
            → LOCKED

    LOCKED:
        // Hold at target
        if (!shotActive):
            → DISENGAGING

    DISENGAGING:
        current -= min(kRampDownRate, current)
        emit delayChanged(current)
        if (current <= 0):
            current = 0
            emit delayChanged(0)
            → STANDBY
```

**Backoff in LOCKED state:** If network degrades while a shot is in progress,
reduce the delay immediately (don't wait for the shot to finish). The session
layer reduces the target, and the shot layer tracks the reduced target:

```cpp
// In LOCKED, if target was reduced by backoff:
if (current > target) {
    current = target;
    emit delayChanged(current);
}
```

### 4. `NetworkBridge.h / .cpp` — Modifications

```cpp
// New methods:
void sendSetMeterDelay(double delayMs);
void sendStartMeterIntercept();
void sendStopMeterIntercept();

void NetworkBridge::sendSetMeterDelay(double delayMs) {
    if (!connected_) return;
    QJsonObject cmd;
    cmd["cmd"] = "set_meter_delay";
    cmd["delay_ms"] = delayMs;
    sendCommand(cmd);
}

void NetworkBridge::sendStartMeterIntercept() {
    if (!connected_) return;
    QJsonObject cmd;
    cmd["cmd"] = "start_meter_intercept";
    sendCommand(cmd);
}

void NetworkBridge::sendStopMeterIntercept() {
    if (!connected_) return;
    QJsonObject cmd;
    cmd["cmd"] = "stop_meter_intercept";
    sendCommand(cmd);
}
```

Parse delay telemetry from packet events:
```cpp
if (obj.contains("meter_delay_active")) {
    snap.meterDelayActive = obj["meter_delay_active"].toBool();
    snap.meterDelayMs = obj["meter_delay_ms"].toDouble();
    snap.meterBufferDepth = obj["meter_buffer_depth"].toInt();
}
```

### 5. `OrionTypes.h`

```cpp
// Add to TelemetrySnapshot:
bool meterDelayActive = false;
double meterDelayMs = 0.0;
int meterBufferDepth = 0;
```

### 6. `OrionAppController.h / .cpp`

**New Q_PROPERTYs:**
```cpp
Q_PROPERTY(bool meterDelayEnabled READ meterDelayEnabled
           WRITE setMeterDelayEnabled NOTIFY settingsChanged)
Q_PROPERTY(double manualMeterDelayMs READ manualMeterDelayMs
           WRITE setManualMeterDelayMs NOTIFY settingsChanged)
Q_PROPERTY(QString meterDelaySessionState READ meterDelaySessionState
           NOTIFY meterDelayChanged)
Q_PROPERTY(QString meterDelayShotState READ meterDelayShotState
           NOTIFY meterDelayChanged)
Q_PROPERTY(double meterDelayCurrentMs READ meterDelayCurrentMs
           NOTIFY meterDelayChanged)
Q_PROPERTY(double meterDelayTargetMs READ meterDelayTargetMs
           NOTIFY meterDelayChanged)
Q_PROPERTY(QString meterDelayReason READ meterDelayReason
           NOTIFY meterDelayChanged)
```

**Wiring:**
```cpp
meterDelay_ = new MeterDelayController(this);

// Network telemetry → controller
connect(this, &OrionAppController::telemetryChanged, this, [this]() {
    meterDelay_->updateTelemetry(
        telemetry_.rttMs, telemetry_.jitterMs,
        telemetry_.rttTargetVerified, false);
});

// Shot state from AutomationEngine → controller
connect(&automation_, &AutomationEngine::holdStateChanged, this, [this]() {
    bool shooting = (automation_.holdState() == HoldState::Armed
                  || automation_.holdState() == HoldState::Holding);
    meterDelay_->setShotActive(shooting);
});

// Controller delay changes → nexus_svc
connect(meterDelay_, &MeterDelayController::delayChanged,
        &networkBridge_, &NetworkBridge::sendSetMeterDelay);

// Controller intercept lifecycle → nexus_svc
connect(meterDelay_, &MeterDelayController::interceptStart,
        &networkBridge_, &NetworkBridge::sendStartMeterIntercept);
connect(meterDelay_, &MeterDelayController::interceptStop,
        &networkBridge_, &NetworkBridge::sendStopMeterIntercept);

// Controller → QML
connect(meterDelay_, &MeterDelayController::telemetryChanged,
        this, &OrionAppController::meterDelayChanged);

// Game state → controller
connect(this, &OrionAppController::playingGameChanged, this, [this]() {
    meterDelay_->setPlayingGame(telemetry_.playingGame);
});
connect(this, &OrionAppController::telemetryChanged, this, [this]() {
    meterDelay_->setCourtIpKnown(!telemetry_.courtIp.isEmpty());
});
```

### 7. `AppConfig.h / .cpp`

```cpp
// AppConfigData:
bool meterDelayEnabled = true;          // ON by default
double manualMeterDelayMs = 0.0;        // 0 = auto (165ms)

// Save:
obj["meter_delay_enabled"] = data.meterDelayEnabled;
if (data.manualMeterDelayMs > 0)
    obj["meter_delay_manual_ms"] = data.manualMeterDelayMs;

// Load:
data.meterDelayEnabled = obj.value("meter_delay_enabled").toBool(true);
data.manualMeterDelayMs = std::clamp(
    obj.value("meter_delay_manual_ms").toDouble(0.0), 0.0, 250.0);
```

### 8. `DashboardPage.qml` — Meter Delay Card

```qml
NetworkCard {
    Layout.fillWidth: true
    title: "Meter Delay"
    subtitle: orion.meterDelaySessionState

    StatRow {
        label: "Session"
        value: orion.meterDelaySessionState
        valueColor: {
            switch(orion.meterDelaySessionState) {
                case "Ready":   return Theme.success
                case "Backoff": return Theme.warning
                case "Probing": return Theme.muted
                default:        return Theme.textSecondary
            }
        }
    }
    StatRow {
        label: "Shot"
        value: orion.meterDelayShotState
        visible: orion.meterDelaySessionState === "Ready"
                 || orion.meterDelaySessionState === "Backoff"
        valueColor: {
            switch(orion.meterDelayShotState) {
                case "Locked":      return Theme.success
                case "Engaging":    return Theme.info
                case "Disengaging": return Theme.muted
                default:            return Theme.textSecondary
            }
        }
    }
    StatRow {
        label: "Current Delay"
        value: orion.meterDelayCurrentMs.toFixed(0) + " ms"
        visible: orion.meterDelayCurrentMs > 0
    }
    StatRow {
        label: "Target"
        value: orion.meterDelayTargetMs.toFixed(0) + " ms"
    }
    StatRow {
        label: "Mode"
        value: orion.manualMeterDelayMs > 0
            ? "Manual (" + orion.manualMeterDelayMs.toFixed(0) + "ms)"
            : "Auto (150-170ms)"
    }
    StatRow {
        label: "Status"
        value: orion.meterDelayReason
        wrap: true
    }
}
```

### 9. `CMakeLists.txt`

```cmake
target_sources(AutomationCore PRIVATE
    src/MeterDelayController.h
    src/MeterDelayController.cpp
)
```

---

## Interaction with Existing Systems

### RTT Compensation — No Conflict
Bot's ICMP/TCP pings to court IP travel a different path than UDP game packets.
WinDivert intercept filter is narrow (udp.SrcPort 30000-30020). Bot's
`natural_rtt` stays accurate. The delay controller does NOT feed into the
`effective_offset_ms` calculation.

### Remote Play Stream — Not Touched
Intercept filter requires `ip.SrcAddr == {court_ip}`. Remote Play traffic
comes from PS5's LAN IP (192.168.137.100), not the court server. Source IP
filter prevents any Remote Play packet from being intercepted.

### AutomationEngine — Clean Integration
The only touch point is `holdStateChanged` → `setShotActive(bool)`. The
controller doesn't modify any automation timing. It operates on a completely
separate axis: it changes when the PS5 renders the meter, not when the bot
reads or fires.

### SNIFF Handle — Unchanged
Existing SNIFF handle for telemetry continues alongside the intercept handle.
Different filters and flags. WinDivert supports multiple simultaneous handles.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| **No court IP** | Session IDLE. No intercept handle opened. |
| **Court IP changes** | Stop intercept, restart with new IP, re-probe. |
| **nexus_svc restarts** | NetworkBridge reconnects. Controller re-sends state. |
| **Game ends** | Session → IDLE. Intercept handle closed. Delay = 0. |
| **Feature toggled off** | Same as game end. |
| **Rapid shot succession** | DISENGAGING interrupted by new shot → ENGAGING. Current delay doesn't reset to 0, ramps from wherever it is. Faster convergence. |
| **Shot canceled** | shotActive goes false during ENGAGING → DISENGAGING from partial ramp. Less total delay injected. |
| **Network degrades mid-shot** | Session BACKOFF reduces target. Shot LOCKED tracks reduced target. |
| **Manual override** | Fixed target. No micro-adjustment. Backoff still runs. |
| **Very fast shots** | If shot cycle < ramp time, delay may not reach full target. Still provides partial benefit. |

---

## Testing Plan

### Unit Tests — MeterDelayController

**Session layer:**

| Test | Setup | Verify |
|------|-------|--------|
| IDLE when disabled | `setEnabled(false)` | session=Idle, current=0 |
| IDLE without game | enabled, !playing | session=Idle |
| IDLE without court | enabled, playing, !courtKnown | session=Idle |
| Probes on start | enable+playing+court, feed 8 samples | PROBING→READY, interceptStart emitted |
| Probe min time | 8 samples in <2s | Stays PROBING |
| Micro-adjust up | READY, low jitter 10 ticks | target drifts toward 170 |
| Micro-adjust down | READY, moderate jitter | target drifts toward 150 |
| Jitter backoff | READY, jitter>3x baseline | BACKOFF, target reduced |
| Cooldown restore | BACKOFF, 20 stable ticks | →READY, target restored |
| Game end | READY, setPlayingGame(false) | IDLE, interceptStop emitted |

**Shot layer:**

| Test | Setup | Verify |
|------|-------|--------|
| Standby at 0 | session=READY, no shot | current=0, shot=Standby |
| Engage on shot | setShotActive(true) | shot→Engaging, delay ramps up |
| Ramp rate up | Engaging, tick | current increases ≤55ms/tick |
| Lock at target | current reaches target | shot→Locked, delay holds |
| Disengage on release | setShotActive(false) from Locked | shot→Disengaging |
| Ramp rate down | Disengaging, tick | current decreases ≤35ms/tick |
| Return to standby | current reaches 0 | shot→Standby |
| Rapid re-engage | Disengaging at 95ms, new shot | shot→Engaging, ramps from 95 |
| Cancel mid-ramp | Engaging at 60ms, shotActive=false | Disengaging from 60 |
| Backoff mid-lock | Locked at 165, target drops to 145 | current tracks to 145 |
| No shot in IDLE | setShotActive(true) while session=Idle | No delay change |

### Integration Tests — nexus_svc

| Test | Action | Verify |
|------|--------|--------|
| start_meter_intercept | Authenticated + IPs set | Handle opens, ack |
| set_meter_delay | delay_ms=165 | Telemetry shows active |
| Rapid delay changes | 0→55→110→165 in 300ms | Each value reflected |
| stop_meter_intercept | After active session | Buffer flushed, handle closed |
| Packet timing | Inject UDP at delay=165 | Re-injected at ±2ms |
| Flush on zero | Set 165, buffer fills, set 0 | Instant flush |

### Manual Verification

1. Enable meter delay, start Park game
2. Dashboard: Probing (~2s) → Ready. Shot state = Standby.
3. Take a shot — watch dashboard: Standby → Engaging → Locked → Disengaging → Standby
4. During Locked: meter should visibly lag behind animation
5. Between shots: movement should feel fully responsive (delay = 0)
6. Compare: 10 shots with delay ON vs 10 with delay OFF
7. Stress: rapid back-to-back shots — verify no stuck states
8. Kill WiFi → Backoff → reduced target → shots still work at lower delay
9. End game → clean IDLE
