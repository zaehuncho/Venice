#pragma once

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
//  MeterDelayController â€” shot-aware inbound network delay actuator
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
//
//  WHAT THIS IS
//  ------------
//  Injecting ~165 ms of delay on the inbound (server -> PS5) game UDP flow makes
//  the console's latency compensation decouple the shot meter from the shot
//  animation, which gives the vision reader a cleaner meter to read.  Running
//  that delay all the time makes ordinary movement feel sluggish, so this class
//  ramps the delay in only while a shot is happening and ramps it back out
//  afterwards.
//
//  It is a pure ACTUATOR.  It commands a delay value; it never produces, adjusts
//  or forwards a timing measurement.  See "SAFETY" below.
//
//
//  â•â•â• WHY THE GATE IS THE RAW PHYSICAL SQUARE EDGE, NOT THE BOT'S HOLD STATE â•â•â•
//
//  docs/ADAPTIVE_DELAY_PLAN.md (:227-239) wires the shot gate to
//  `AutomationEngine::holdStateChanged` / `holdState()`.  That is wrong twice
//  over and following it would wreck tip timing:
//
//   1. Those symbols do not exist.  The real signal is
//      `AutomationEngine::shotStateChanged(orion::ShotContext)`, read through
//      `automation_.context().state` (AutomationEngine.h:1797, :872).
//
//   2. More importantly the bot's hold state is CAUSALLY TOO LATE.  Measured on
//      the 2026-08-03T02:48Z live capture-card session:
//
//          physical square edge .............. t = 0      ms
//          meter first renders ............... t = ~300   ms
//          shot state Idle -> Holding ........ t = +428   ms
//
//      Starting the ramp at t=428 ms would be raising the injected delay DURING
//      the meter's rise.  The delay is exactly what sets the apparent meter
//      velocity, so a ramp that overlaps the read window changes meter velocity
//      mid-flight and destroys the tip prediction the release engine depends on.
//
//  Therefore the engage gate is the RAW physical shot edge, taken from the 4 ms
//  Qt::PreciseTimer input poll (OrionAppController.cpp:3170-3176) at the point
//  where the "Physical shot epoch:" record is emitted
//  (OrionAppController.cpp:8332, :8364-8394).  From t=0 the ramp reaches target
//  within ~100-200 ms, i.e. comfortably before the meter renders at ~300 ms, so
//  the applied delay is CONSTANT across the entire meter read window.
//
//  Public API is therefore built on `setPhysicalSquareHeld(bool)` and
//  `notifyPhysicalShotEdge()`.  The bot's shot state may additionally be fed in
//  through `setShotCycleActive(bool)`, but only ever as a LATE, NON-CAUSAL hint
//  that keeps the delay engaged longer.  It can never start or shorten a ramp.
//
//
//  â•â•â• WHY THIS EXPOSES A QUERYABLE DELAY CONDITION â•â•â•
//
//  docs/ADAPTIVE_DELAY_PLAN.md (:843-847) claims the controller "doesn't modify
//  any automation timing".  That is not safe as written.  The marker-backed
//  latency estimator measures the METER'S OBSERVED RESPONSE to a release; if
//  inbound packets are being held for 165 ms then the observed response is
//  produced under a different transport condition than when the delay is off.
//  The effective actuation lead can legitimately differ between delay-on and
//  delay-off, so latency posteriors learned in one condition must not be applied
//  blind in the other.
//
//  This class does NOT implement that keying.  It exposes the minimum the timing
//  side needs to do it itself:
//
//      currentDelayMs()      - the delay value currently commanded
//      conditionSettled()    - false while a ramp is in motion (measurements
//                              taken while unsettled are untrustworthy)
//      conditionKeyMs()      - the settled delay quantised to
//                              kConditionQuantumMs; this is the LATENCY ROUTE
//                              KEY (0 = delay off, 165 = delay on, ...)
//      conditionEpoch()      - monotonically increasing; bumps exactly when the
//                              settled condition key changes
//      delayConditionChanged() - emitted on every settled-condition change
//
//
//  â•â•â• SAFETY â•â•â•
//
//  PacketBridgeAuthority.h compiles the packet bridge's timing feed out of
//  production builds (ORION_PRODUCTION_BUILD -> mayFeedTiming() == false) because
//  the bridge is ServiceIdentityTrust::Unverified.  This controller respects that
//  intent absolutely:
//
//    * It emits commands OUTWARD only (delayCommanded / interceptStart /
//      interceptStop).  Nothing it produces is a clock, an offset, a latency or a
//      phase.
//    * `updateNetworkHealth()` is a health/backoff input only.  It can lower the
//      delay target; it can never become a timing measurement.
//    * The condition signals above are deliberately declarative facts about this
//      actuator's own commanded output.  They carry no bridge-sourced value.
//
//  The delay is forced to 0 on: disable, session Idle (game end / court IP lost),
//  `notifyStreamEnded()`, `shutdown()` and destruction.
//
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

#include "OrionExports.h"

#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QTimer>
#include <QtCore/QtGlobal>

#include <deque>
#include <functional>

namespace orion {

class ORION_AUTOMATION_API MeterDelayController final : public QObject {
    Q_OBJECT
public:
    // Session layer: baseline / network health across the whole game session.
    enum class SessionState { Idle, Probing, Ready, Backoff };
    Q_ENUM(SessionState)

    // Shot layer: per-shot ramp. Only runs while session is Ready or Backoff.
    enum class ShotState { Standby, Engaging, Locked, Disengaging };
    Q_ENUM(ShotState)

    // ── Engagement policy ───────────────────────────────────────────────────
    //
    // WHEN the delay is engaged, which is a completely separate question from
    // how fast it ramps. The original design had exactly one policy (per-shot,
    // gated on the physical Square edge) and it is the one that got the feature
    // deleted. Two independent reasons, and only the first is about stutter:
    //
    //  1. STUTTER. A ramp perturbs the console's inbound packet rate by
    //     1/(1+D'). Per-shot engagement places that perturbation ~300ms before
    //     the meter renders -- the worst possible moment. See nexus_svc.py's
    //     slew notes for the arithmetic and tools/meter_delay_bench.py for the
    //     measurement.
    //
    //  2. LEAD POISONING, which is arguably worse. `learned_latency_ms` is a
    //     SINGLE SCALAR and the grader is frozen, so that seed ships
    //     permanently. If the delay is 0ms on some shots and 165ms on others --
    //     or a partial 90ms because a ramp did not finish -- the lead is wrong
    //     by up to 165ms on those shots. A delay that varies shot-to-shot does
    //     not merely risk a stutter, it corrupts the one number the bot fires
    //     on. conditionKeyMs()/conditionEpoch() exist so the timing side CAN key
    //     posteriors per delay-state, but nothing does that yet.
    //
    // Both problems vanish if the delay is CONSTANT across every shot. That is
    // why AlwaysOn is the default: engage once, hold, and the delay stops being
    // a special case and becomes ordinary latency that the learned lead absorbs
    // for free. The cost is input lag while on defence, which is a feel
    // question, not a correctness one.
    enum class EngagePolicy {
        // Engage as soon as the session arms; never disengage until the session
        // goes Idle. Zero mid-play transitions. DEFAULT.
        AlwaysOn,
        // Wait for a dead ball (free throw, inbound, after a made basket) so the
        // single ramp lands where nothing is moving, then hold like AlwaysOn.
        // Strictly better than OffenseDefense if possession detection is shaky.
        DeadBall,
        // Engage on offence, disengage on defence. Buys back defensive
        // responsiveness at the cost of transitions per possession AND a
        // delay that differs between shots taken on a settled vs unsettled
        // ramp -- callers MUST refuse to fire while !conditionSettled()
        // (readyForArm() carves out the monotonic per-shot climb, same argument
        // as the initial-ramp bypass).
        //
        // [ORION_DEFENSE_FLAG 2026-08-08] Offence/defence is now MANUAL. The
        // shot-cycle-driven engagement (the 2026-08-08 morning fix) was
        // unreliable in play and the 100 ms/s slew made the per-shot ramp lag
        // real possession changes anyway, so the owner asked for direct
        // control: the D-pad Up hotkey toggles defenseModeManualActive_, and
        // that flag is the SOLE gate under this policy. flag false (default) =
        // offense = delay applied; flag true = defense = delay bypassed
        // (ramps to 0). squareHeld_ / edge latch / shotCycleActive_ no longer
        // participate in the engagement decision at all. offense_ is retained
        // for a future real possession detector but nothing feeds it today.
        OffenseDefense,
        // The original per-shot gate. Retained for parity and experiment ONLY.
        // At the shipping slew it CANNOT reach target inside the ~300ms
        // pre-meter budget; policyCanSettleBeforeMeter() reports false and the
        // reason text says so. It does not silently half-work.
        ShotGated,
    };
    Q_ENUM(EngagePolicy)

    // â”€â”€ Operating parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Fallback injected delay for auto mode when no verified RTT estimate is
    // available (the user-selected, doc-validated sweet spot).
    static constexpr double kDefaultDelayMs   = 165.0;

    // The total inbound lag we want (network + injected). This is the constant
    // that makes the meter behave identically regardless of connection quality.
    static constexpr double kDesiredTotalInboundMs = 200.0;

    // Adaptive auto-mode clamps. The injected delay never goes below floor
    // (meter stops decoupling) or above ceiling (gameplay feels sluggish).
    static constexpr double kAdaptiveFloorMs  = 120.0;
    static constexpr double kAdaptiveCeilingMs = 160.0;
    static constexpr double kManualMinMs      = 100.0;
    // [ORION_METER_DELAY_RANGE 2026-08-08] Raised 300 → 600 (owner request): the
    // customer slider now spans 100-600 ms so the empirical physics ceiling can be
    // probed live. Raised IN LOCKSTEP with AppConfigData::kMeterDelay{Min,Max}Ms,
    // the MeterConfigPanel slider, VENICENET_MAX_DELAY_MS (venicenet/VeniceNet.h),
    // the service's MeterDelayIntercept kHardMaxMs and nexus_svc.py's
    // _METER_DELAY_HARD_MAX_MS. NOTE an already-running service still enforces the
    // cap it was started with until it is restarted. The slew cap (kSlewMsPerSec,
    // static_assert-pinned below) is deliberately UNCHANGED — it guards stutter,
    // not the target; a 0 → 600 ramp simply takes ~6 s.
    static constexpr double kManualMaxMs      = 600.0;
    static constexpr double kHardMaxMs        = 600.0;  // never command above this

    static constexpr int    kTickMs           = 50;

    // ── Slew ────────────────────────────────────────────────────────────────
    // These were 55.0 up / 20.0 down per 50ms tick, i.e. D' = +1.10 / -0.40,
    // i.e. the console saw 48% then 167% of its normal server-update rate. That
    // is the measured cause of the stutter that got this feature deleted
    // (968b127f). Both directions are now 5.0 per 50ms tick == 100 ms/s ==
    // D' = 0.10 == >=90.9% of normal rate, matching _METER_MAX_SLEW_MS_PER_S in
    // nexus_svc.py exactly.
    //
    // This is defence in depth, not the primary guarantee: the SERVICE enforces
    // its own cap regardless of what this class commands, so a bug here cannot
    // reintroduce the stutter. Keeping the controller polite as well means the
    // commanded and applied values track each other closely, which is what makes
    // conditionSettled() meaningful.
    static constexpr double kRampUpPerTickMs   = 5.0;   // ~1.65 s 0 -> 165
    static constexpr double kRampDownPerTickMs = 5.0;   // ~1.65 s 165 -> 0

    static constexpr double kSlewMsPerSec =
        (kRampUpPerTickMs * 1000.0) / static_cast<double>(kTickMs);
    // nexus_svc.py _METER_MAX_SLEW_MS_PER_S. Kept in sync by static_assert so a
    // unilateral change on this side fails the build instead of being silently
    // clamped by the service and reading as a mysterious slow ramp.
    static constexpr double kServiceSlewCapMsPerSec = 100.0;
    static_assert(kSlewMsPerSec <= kServiceSlewCapMsPerSec,
                  "controller slew exceeds the nexus_svc cap; the service will "
                  "clamp it and commanded/applied will diverge");

    // Measured on the 2026-08-03T02:48Z capture-card session: the meter first
    // renders ~300ms after the physical Square edge. A per-shot policy has only
    // this long to settle, and at kSlewMsPerSec it needs ~1650ms.
    static constexpr qint64 kPreMeterBudgetMs = 300;

    // â”€â”€ Dead-man keepalive â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // nexus_svc arms a starvation watchdog (_METER_WATCHDOG_TIMEOUT_S = 500 ms):
    // a non-zero delay that is not re-asserted by set_meter_delay inside that
    // window is forced back to 0.  A shot sits at a CONSTANT delay for the whole
    // kDefaultMinEngagedMs = 900 ms dwell, so emitting delayCommanded only on
    // change starved the watchdog and zeroed the delay ~500 ms into every shot -
    // exactly the mid-meter-read ramp this class exists to prevent.  A non-zero
    // commanded value is therefore re-emitted at least this often.  Must stay
    // comfortably below the service timeout so a couple of missed 50 ms ticks
    // (or a coalesced command) cannot trip it.
    static constexpr qint64 kKeepaliveMs      = 150;

    static constexpr double kBackoffStepMs    = 20.0;
    static constexpr double kBackoffFloorMs   = 100.0;
    static constexpr int    kMinProbes        = 8;
    static constexpr qint64 kMinProbeMs       = 2000;
    // If health telemetry never arrives the session must still arm, otherwise an
    // unwired telemetry source silently disables the whole feature. Arming
    // without a baseline simply means backoff cannot trigger.
    static constexpr qint64 kProbeTimeoutMs   = 5000;
    static constexpr qint64 kBackoffRecoverMs = 2000;
    static constexpr double kJitterMult       = 3.0;
    static constexpr double kRttMult          = 2.5;

    // A shot must stay engaged long enough to cover the whole meter read.
    // Asymmetric risk: dwelling too long only costs a little input lag, while
    // dwelling too short ramps the delay DOWN during the read and corrupts the
    // tip prediction.  Default generously long.
    static constexpr qint64 kDefaultMinEngagedMs   = 900;
    static constexpr qint64 kDefaultDisengageHoldoffMs = 250;
    static constexpr qint64 kMaxEngagedMs          = 6000;  // stuck-state watchdog

    // Settled-condition quantisation. Auto-mode micro-adjustment drifts the
    // target by fractions of a millisecond; without quantisation the condition
    // epoch would churn on noise.
    static constexpr double kConditionQuantumMs = 5.0;

    explicit MeterDelayController(QObject* parent = nullptr);
    ~MeterDelayController() override;

    // â”€â”€ Configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Compile-time production fence, mirroring PacketBridgeAuthority.h.
    //
    // This controller drives an ELEVATED service to hold the console's inbound
    // game packets. No settings key, environment variable or QML binding may
    // turn that on in a shipping build, so setEnabled(true) is refused outright
    // when fenced. nexus_svc independently ships the intercept DISARMED, so
    // BOTH ends have to be deliberately opened before a single packet is held.
    [[nodiscard]] static constexpr bool productionFenced() noexcept
    {
        // Shipping feature since 2026-08-07.
        return false;
    }

    void setEnabled(bool enabled);
    [[nodiscard]] bool enabled() const noexcept { return enabled_; }

    // 0 => automatic (kDesiredTotalInboundMs minus the smoothed one-way
    // latency, clamped to [kAdaptiveFloorMs, kAdaptiveCeilingMs]; falls back
    // to kDefaultDelayMs when no verified RTT is available).
    // Any other value is clamped into [kManualMinMs, kManualMaxMs].
    void setManualDelayMs(double ms);
    [[nodiscard]] double manualDelayMs() const noexcept { return manual_; }
    [[nodiscard]] bool manualMode() const noexcept { return manual_ > 0.0; }

    void setMinEngagedMs(qint64 ms);
    [[nodiscard]] qint64 minEngagedMs() const noexcept { return minEngagedMs_; }
    void setDisengageHoldoffMs(qint64 ms);
    [[nodiscard]] qint64 disengageHoldoffMs() const noexcept { return disengageHoldoffMs_; }

    // Engagement policy. Defaults to AlwaysOn — see the enum's notes.
    void setEngagePolicy(EngagePolicy policy);
    [[nodiscard]] EngagePolicy engagePolicy() const noexcept { return policy_; }
    // False for ShotGated at the shipping slew: the ramp cannot reach target
    // inside kPreMeterBudgetMs, so every shot would be read at a different,
    // partially-ramped delay. Callers that select such a policy are choosing to
    // fire on an unsettled condition and must gate on conditionSettled().
    [[nodiscard]] bool policyCanSettleBeforeMeter() const noexcept;
    [[nodiscard]] static QString policyName(EngagePolicy policy);

    // â”€â”€ Session inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    void setPlayingGame(bool playing);
    void setCourtIpKnown(bool known);
    // NetworkBridge hello version >= 3. Defaults to true so an unwired build
    // still functions; NetworkBridge independently drops unsupported commands.
    void setServiceSupported(bool supported);
    void notifyStreamEnded();

    // â”€â”€ Shot inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // THE causal gate: raw physical Square, polled every 4 ms. Engaging happens
    // synchronously on the rising edge (not on the next 50 ms tick).
    void setPhysicalSquareHeld(bool held);
    // Edge-only equivalent for shot gestures with no held button (Go-To right
    // stick up/down). Latches engagement for minEngagedMs().
    void notifyPhysicalShotEdge();
    // Bot shot-cycle hint from AutomationEngine's ShotContext.
    // [ORION_DEFENSE_FLAG 2026-08-08] DECOUPLED from the engagement decision
    // entirely: it can no longer start, extend or shorten an engagement under
    // ANY policy (the shot-cycle detector proved unreliable as a bypass driver,
    // and the manual defense flag replaced it). Retained as recorded diagnostic
    // state only — pinned by shotCycleNoLongerDrivesBypass.
    void setShotCycleActive(bool active);

    // ── Policy inputs ───────────────────────────────────────────────────────
    // Live/dead ball. DeadBall policy latches its single engagement the first
    // time this goes false, so the one ramp in the whole session happens while
    // nothing on court is moving. Ignored by every other policy.
    void setBallLive(bool live);
    [[nodiscard]] bool ballLive() const noexcept { return ballLive_; }
    // Possession. [ORION_DEFENSE_FLAG 2026-08-08] No longer consulted by any
    // policy (the manual defense flag is OffenseDefense's sole gate). Kept as
    // recorded state for a future real possession detector.
    void setOffense(bool offense);
    [[nodiscard]] bool offense() const noexcept { return offense_; }

    // [ORION_DEFENSE_FLAG 2026-08-08] Manual defense mode — the D-pad Up
    // hotkey's RUNTIME flag and the SOLE bypass gate under OffenseDefense.
    // true  = user is on defense: the delay is bypassed (ramps to 0 so the
    //         customer is not reactive-lagged while defending);
    // false = user is on offense: the delay is applied (ramps to target).
    // Deliberately NON-PERSISTENT — never a settings key. Every launch and
    // every session starts at false so the delay can never be "silently off"
    // after a restart. Reset to false by enterIdle() (disconnect, game end,
    // court-IP loss, disable, shutdown) and by any engage-policy change (a
    // stale defense latch must not survive a policy round-trip).
    void setDefenseModeManualActive(bool active);
    [[nodiscard]] bool defenseModeManualActive() const noexcept
    {
        return defenseModeManualActive_;
    }

    // â”€â”€ Network health (backoff only â€” NEVER timing) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    void updateNetworkHealth(double rttMs, double jitterMs, bool rttVerified,
                             bool packetLoss);

    // â”€â”€ Read state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    [[nodiscard]] SessionState sessionState() const noexcept { return session_; }
    [[nodiscard]] ShotState shotState() const noexcept { return shot_; }
    [[nodiscard]] double currentDelayMs() const noexcept { return current_; }
    [[nodiscard]] double targetDelayMs() const noexcept { return target_; }
    [[nodiscard]] QString sessionStateString() const;
    [[nodiscard]] QString shotStateString() const;
    [[nodiscard]] QString reasonText() const { return reason_; }
    [[nodiscard]] bool interceptActive() const noexcept { return interceptRequested_; }

    // â”€â”€ Delay condition (see header notes) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    [[nodiscard]] bool conditionSettled() const noexcept { return settled_; }
    [[nodiscard]] double settledDelayMs() const noexcept { return settledDelay_; }
    [[nodiscard]] double conditionKeyMs() const noexcept { return conditionKey_; }
    [[nodiscard]] quint64 conditionEpoch() const noexcept { return conditionEpoch_; }

    // [ORION_METER_DELAY_FIRST_SHOT 2026-08-07] Arm gate for the OrionAppController
    // engine. conditionSettled() reports false during the entire ~2.5 s initial ramp
    // to the first target (kRampUpPerTickMs=5.0 * 1000/kTickMs = 100 ms/s vs a
    // ~250 ms manual target; see MeterDelayController.cpp:475-484 for rampUp() and
    // :701-752 for publish()'s nowSettled formula that only accepts Standby-at-0 or
    // Locked-at-target). Gating engine arm on that killed every shot fired during
    // that ramp on every session -- the "first-shot dead window" bug.
    //
    // readyForArm() returns true whenever the delay is being APPROACHED (not
    // disturbed) -- i.e., genuinely settled OR still in the FIRST monotonic ramp
    // toward the first target since the session became Ready. In the initial-ramp
    // window the applied delay is < target but strictly monotonically climbing, so
    // arming the engine is safe: the delay does not oscillate mid-shot. Subsequent
    // ramps (Backoff -> Recovery re-ramp, mid-play target changes) are NOT covered
    // here; Fix 2 (MeterDelayController.cpp:573-577) snaps those.
    //
    // [ORION_METER_DELAY_WATCHDOG_D1 2026-08-08] It also returns true while the
    // applied delay sits AT target and the engagement is still actively wanted by
    // the policy (engageStartWanted), regardless of the momentary shot-state
    // label. This absorbs the kMaxEngagedMs watchdog's one-tick
    // Locked -> Disengaging -> Locked bookkeeping flip under OffenseDefense
    // (root cause also fixed in shouldDisengage()), which used to revoke armed
    // precise-fire tokens ~once per 6 s of continuous offense. A GENUINE
    // disengage (policy no longer wants engagement) still reports false from its
    // very first tick -- fail-closed is unchanged.
    [[nodiscard]] bool readyForArm() const noexcept;

    // Commands 0 ms, releases the intercept and parks the state machine. Safe to
    // call repeatedly; called automatically from the destructor.
    //
    // OWNERS: prefer calling this explicitly during your own teardown. The
    // destructor still commands 0 as a last resort, but signals emitted from a
    // destructor reach receivers that may already be partway through their own
    // destruction.
    void shutdown();

    // â”€â”€ Test seam â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Replaces the internal 50 ms QTimer with explicit tick() calls and lets the
    // monotonic clock be driven by the test.
    void setManualTickMode(bool manual);
    void setMonotonicClockForTesting(std::function<qint64()> clock);

public slots:
    void tick();

signals:
    void sessionStateChanged(orion::MeterDelayController::SessionState state);
    void shotStateChanged(orion::MeterDelayController::ShotState state);
    // -> NetworkBridge::sendSetMeterDelay (fire-and-forget)
    void delayCommanded(double delayMs);
    void interceptStartRequested();
    void interceptStopRequested();
    // Settled-condition change. `settled` is false while a ramp is in motion.
    void delayConditionChanged(quint64 epoch, double conditionKeyMs, bool settled);
    // Anything user-visible changed (state strings, delay, target, reason).
    void stateTextChanged();

private:
    [[nodiscard]] qint64 nowMs() const;
    void evaluateSessionGates();
    void tickSession(qint64 now);
    void tickShot(qint64 now);
    void publish();

    [[nodiscard]] QString readyReason(const QString& base) const;
    void transitionSession(SessionState next, const QString& reason);
    void transitionShot(ShotState next);
    void enterIdle(const QString& reason);
    void enterBackoff(qint64 now);
    void computeTarget();
    void microAdjustTarget();
    [[nodiscard]] bool sessionGatesOpen() const noexcept;
    [[nodiscard]] bool shotLayerActive() const noexcept;
    // The engagement decision. Per policy: AlwaysOn — always; DeadBall — after
    // the first dead ball; OffenseDefense — SOLELY !defenseModeManualActive_
    // ([ORION_DEFENSE_FLAG 2026-08-08]); ShotGated — raw physical Square /
    // physical shot-gesture latch. The bot shot-cycle hint participates in NO
    // branch (engageHoldWanted, which added it, was deleted with the hint).
    [[nodiscard]] bool engageStartWanted(qint64 now) const noexcept;
    // True for AlwaysOn / DeadBall: engagement ends only when the session does.
    [[nodiscard]] bool policyHoldsIndefinitely() const noexcept;
    [[nodiscard]] bool shouldDisengage(qint64 now) const noexcept;
    [[nodiscard]] bool healthDegraded() const noexcept;
    [[nodiscard]] bool rampDue(qint64 now) const noexcept;
    void beginEngagement(qint64 now);
    void engageNow(qint64 now);
    void rampUp(qint64 now);
    void rampDown(qint64 now);
    void updateTimer();

    // â”€â”€ Configuration â”€â”€
    bool enabled_ = false;
    double manual_ = 0.0;
    qint64 minEngagedMs_ = kDefaultMinEngagedMs;
    qint64 disengageHoldoffMs_ = kDefaultDisengageHoldoffMs;

    // â”€â”€ Session gates â”€â”€
    bool playing_ = false;
    bool courtKnown_ = false;
    bool serviceSupported_ = true;

    SessionState session_ = SessionState::Idle;
    QString reason_ = QStringLiteral("Idle");
    bool interceptRequested_ = false;

    QTimer tickTimer_;
    bool manualTick_ = false;
    std::function<qint64()> clock_;

    struct Sample { double rtt; double jitter; };
    std::deque<Sample> probes_;
    qint64 probeStartMs_ = 0;
    double baseJitter_ = 0.0;
    double baseRtt_ = 0.0;
    bool baselineValid_ = false;

    // Smoothed one-way latency estimate (verified RTT / 2, EMA) driving the
    // adaptive auto-mode target.
    double smoothedOneWayMs_ = 0.0;
    bool haveRttEstimate_ = false;

    double target_ = kDefaultDelayMs;
    qint64 stableSinceMs_ = 0;

    // â”€â”€ Shot layer â”€â”€
    ShotState shot_ = ShotState::Standby;
    bool squareHeld_ = false;
    bool shotCycleActive_ = false;

    // Engagement policy state.
    EngagePolicy policy_ = EngagePolicy::AlwaysOn;
    bool ballLive_ = true;
    bool offense_ = false;
    // [ORION_DEFENSE_FLAG 2026-08-08] See setDefenseModeManualActive(). Runtime
    // only — never persisted; false = offense = delay applied.
    bool defenseModeManualActive_ = false;
    // DeadBall latches: once its single engagement has started it behaves like
    // AlwaysOn. Re-ramping at every subsequent dead ball would reintroduce the
    // per-possession transitions the policy exists to avoid.
    bool deadBallLatched_ = false;

    qint64 edgeLatchExpiryMs_ = 0;
    qint64 engagedAtMs_ = 0;
    qint64 lastEngageRequestMs_ = 0;
    qint64 lastRampMs_ = 0;
    double current_ = 0.0;

    // â”€â”€ Published/derived â”€â”€
    double lastCommanded_ = 0.0;
    qint64 lastCommandEmitMs_ = 0;
    bool haveCommandEmit_ = false;
    bool settled_ = true;
    double settledDelay_ = 0.0;
    double conditionKey_ = 0.0;
    quint64 conditionEpoch_ = 0;
    // [ORION_METER_DELAY_FIRST_SHOT 2026-08-07] Latched true on the first entry to
    // ShotState::Locked after enterIdle(). Cleared in enterIdle() (session teardown).
    // readyForArm() uses this to distinguish "initial monotonic climb to first
    // target" (safe to arm on) from "recovery re-ramp after target moved" (must
    // NOT arm; those cases oscillate).
    bool initialRampReachedTarget_ = false;
    QString lastPublishedSummary_;
    bool shuttingDown_ = false;

    // â”€â”€ Latest health sample â”€â”€
    double lastRtt_ = 0.0;
    double lastJitter_ = 0.0;
    bool lastRttVerified_ = false;
    bool lastLoss_ = false;
};

} // namespace orion
