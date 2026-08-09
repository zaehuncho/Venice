#include "MeterDelayController.h"

#include <QtCore/QStringList>

#include <algorithm>
#include <chrono>
#include <cmath>

namespace orion {
namespace {

constexpr double kEps = 1e-6;

qint64 steadyNowMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

} // namespace

MeterDelayController::MeterDelayController(QObject* parent)
    : QObject(parent)
{
    tickTimer_.setTimerType(Qt::PreciseTimer);
    tickTimer_.setInterval(kTickMs);
    connect(&tickTimer_, &QTimer::timeout, this, &MeterDelayController::tick);
}

MeterDelayController::~MeterDelayController()
{
    // Commanding 0 here is a hard requirement: if the process dies with a
    // non-zero delay still armed, the elevated service keeps holding inbound
    // packets with nobody left to lower it.
    shutdown();
}

qint64 MeterDelayController::nowMs() const
{
    return clock_ ? clock_() : steadyNowMs();
}

// ─── Configuration ──────────────────────────────────────────────────────────

void MeterDelayController::setEnabled(bool enabled)
{
    // [ORION_METER_DELAY_UNFENCE 2026-08-07] Shipping feature. Production fence removed.
    // The remaining safety story is unchanged: (1) nexus_svc still ships DISARMED
    // (_METER_ARMED, arm via ORION_METER_DELAY_ARMED=1); (2) enterIdle() commands 0 on
    // every teardown; (3) the service enforces its own slew (kSlewMsPerSec) and hard-max
    // (_METER_DELAY_HARD_MAX_MS = 300); (4) the OrionAppController release predicate
    // gates on conditionSettled() so no shot fires while the delay is ramping.
    if (enabled_ == enabled || shuttingDown_) return;
    enabled_ = enabled;
    if (!enabled_) {
        // Forced zero on disable.
        enterIdle(QStringLiteral("Disabled"));
    }
    updateTimer();
    evaluateSessionGates();
    publish();
}

void MeterDelayController::setManualDelayMs(double ms)
{
    double next = 0.0;
    if (std::isfinite(ms) && ms > 0.0) {
        next = std::clamp(ms, kManualMinMs, kManualMaxMs);
    }
    if (std::abs(next - manual_) <= kEps) return;
    manual_ = next;
    computeTarget();
    publish();
}

void MeterDelayController::setMinEngagedMs(qint64 ms)
{
    minEngagedMs_ = std::clamp<qint64>(ms, 0, kMaxEngagedMs);
}

void MeterDelayController::setDisengageHoldoffMs(qint64 ms)
{
    disengageHoldoffMs_ = std::clamp<qint64>(ms, 0, kMaxEngagedMs);
}

// ─── Session inputs ─────────────────────────────────────────────────────────

void MeterDelayController::setPlayingGame(bool playing)
{
    if (playing_ == playing) return;
    playing_ = playing;
    evaluateSessionGates();
}

void MeterDelayController::setCourtIpKnown(bool known)
{
    if (courtKnown_ == known) return;
    courtKnown_ = known;
    evaluateSessionGates();
}

void MeterDelayController::setServiceSupported(bool supported)
{
    if (serviceSupported_ == supported) return;
    serviceSupported_ = supported;
    evaluateSessionGates();
}

void MeterDelayController::notifyStreamEnded()
{
    playing_ = false;
    courtKnown_ = false;
    squareHeld_ = false;
    shotCycleActive_ = false;
    edgeLatchExpiryMs_ = 0;
    enterIdle(QStringLiteral("Stream ended"));
    publish();
}

void MeterDelayController::shutdown()
{
    if (shuttingDown_) return;
    shuttingDown_ = true;
    tickTimer_.stop();
    squareHeld_ = false;
    shotCycleActive_ = false;
    defenseModeManualActive_ = false;
    edgeLatchExpiryMs_ = 0;
    current_ = 0.0;
    shot_ = ShotState::Standby;
    session_ = SessionState::Idle;
    reason_ = QStringLiteral("Shut down");
    if (interceptRequested_) {
        interceptRequested_ = false;
        emit interceptStopRequested();
    }
    publish();
}

// ─── Shot inputs ────────────────────────────────────────────────────────────

void MeterDelayController::setPhysicalSquareHeld(bool held)
{
    if (squareHeld_ == held) return;
    squareHeld_ = held;
    const qint64 now = nowMs();
    if (held) {
        engageNow(now);
    } else {
        // Start the disengage hold-off clock from the moment engagement stopped
        // being requested.
        lastEngageRequestMs_ = now;
        publish();
    }
}

void MeterDelayController::notifyPhysicalShotEdge()
{
    engageNow(nowMs());
}

void MeterDelayController::setEngagePolicy(EngagePolicy policy)
{
    if (policy_ == policy) return;
    policy_ = policy;
    // Changing policy must never strand a delay that the old policy was holding
    // and the new one would not. Re-derive from a clean shot layer.
    deadBallLatched_ = false;
    // [ORION_DEFENSE_FLAG 2026-08-08] A policy round-trip (e.g. the "Defense
    // bypass" master switch off -> on) must not resurrect a stale defense latch:
    // re-entering OffenseDefense with the flag still true would silently bypass
    // the delay with nothing on screen saying so. Clean slate, offense mode.
    defenseModeManualActive_ = false;
    edgeLatchExpiryMs_ = 0;
    lastEngageRequestMs_ = nowMs();
    publish();
}

QString MeterDelayController::readyReason(const QString& base) const
{
    QString text = base + QStringLiteral(" [") + policyName(policy_) + QStringLiteral("]");
    if (!policyCanSettleBeforeMeter()) {
        // Say it out loud rather than half-working. A partially-ramped delay is
        // both a stutter and a corrupted lead, and the whole reason this feature
        // was deleted is that neither was visible until a batch had been lost.
        const double neededMs =
            ((manualMode() ? manual_ : kDefaultDelayMs) / kSlewMsPerSec) * 1000.0;
        text += QStringLiteral(
                    " WARNING: this policy cannot settle before the meter "
                    "renders (needs %1 ms, has %2 ms) - every shot reads a "
                    "different partial delay. Gate firing on conditionSettled().")
                    .arg(QString::number(neededMs, 'f', 0),
                         QString::number(kPreMeterBudgetMs));
    }
    return text;
}

bool MeterDelayController::policyCanSettleBeforeMeter() const noexcept
{
    if (policy_ != EngagePolicy::ShotGated) {
        // Every other policy engages long before a shot begins, so the delay is
        // already constant by the time the meter renders.
        return true;
    }
    // ShotGated starts its ramp at the physical Square edge and the meter
    // renders kPreMeterBudgetMs later. Can the slew cover the target in time?
    const double target = manualMode() ? manual_ : kDefaultDelayMs;
    const double neededMs = (target / kSlewMsPerSec) * 1000.0;
    return neededMs <= static_cast<double>(kPreMeterBudgetMs);
}

QString MeterDelayController::policyName(EngagePolicy policy)
{
    switch (policy) {
    case EngagePolicy::AlwaysOn:       return QStringLiteral("AlwaysOn");
    case EngagePolicy::DeadBall:       return QStringLiteral("DeadBall");
    case EngagePolicy::OffenseDefense: return QStringLiteral("OffenseDefense");
    case EngagePolicy::ShotGated:      return QStringLiteral("ShotGated");
    }
    return QStringLiteral("AlwaysOn");
}

void MeterDelayController::setBallLive(bool live)
{
    if (ballLive_ == live) return;
    ballLive_ = live;
    if (!live && policy_ == EngagePolicy::DeadBall) {
        // The one moment in the session when a ramp is free: nothing is moving,
        // so even a visible packet-rate perturbation costs nothing.
        deadBallLatched_ = true;
        lastEngageRequestMs_ = nowMs();
    }
}

void MeterDelayController::setOffense(bool offense)
{
    if (offense_ == offense) return;
    offense_ = offense;
    lastEngageRequestMs_ = nowMs();
}

void MeterDelayController::setDefenseModeManualActive(bool active)
{
    // [ORION_DEFENSE_FLAG 2026-08-08] The D-pad Up hotkey's runtime flag. Under
    // OffenseDefense this IS the engagement decision (engageStartWanted /
    // shouldDisengage read it directly), so the next tick — at most kTickMs away
    // — starts the ramp in the commanded direction. No dwell, no holdoff: an
    // explicit user command outranks the per-shot protection windows, and
    // fail-closed is preserved independently (the ramp publishes settled=false,
    // readyForArm() refuses, and syncEngineArmed() disarms as delay-gate-only).
    if (defenseModeManualActive_ == active) return;
    defenseModeManualActive_ = active;
    lastEngageRequestMs_ = nowMs();
    publish();
}

void MeterDelayController::setShotCycleActive(bool active)
{
    // [ORION_DEFENSE_FLAG 2026-08-08] Recorded diagnostic state ONLY. The hint
    // used to extend engagements (and, for one day, start them under the bypass
    // policy); the shot-cycle detector proved unreliable and the manual defense
    // flag replaced it as the sole OffenseDefense gate. Deliberately does not
    // touch lastEngageRequestMs_ either — the hint must have ZERO influence on
    // engagement timing (pinned by shotCycleNoLongerDrivesBypass).
    if (shotCycleActive_ == active) return;
    shotCycleActive_ = active;
}

void MeterDelayController::engageNow(qint64 now)
{
    edgeLatchExpiryMs_ = std::max(edgeLatchExpiryMs_, now + minEngagedMs_);
    lastEngageRequestMs_ = now;
    // [ORION_DEFENSE_FLAG 2026-08-08] The manual defense flag is the SOLE gate
    // under OffenseDefense: a physical Square edge while the user is in defense
    // mode (e.g. contesting with the shot button) must not start even a one-step
    // blip of the ramp. Without this guard the edge would apply one synchronous
    // +kRampUpPerTickMs step before the next tick's disengage caught it.
    if (policy_ == EngagePolicy::OffenseDefense && defenseModeManualActive_) {
        publish();
        return;
    }
    if (!shotLayerActive() || shot_ == ShotState::Locked) {
        publish();
        return;
    }
    if (shot_ != ShotState::Engaging) {
        beginEngagement(now);
    }
    // Apply the first ramp step synchronously with the physical edge instead of
    // waiting up to one 50 ms tick. The entire point of gating on the raw edge is
    // causality; spending a tick before the first step gives that back.
    current_ = std::min(target_, current_ + kRampUpPerTickMs);
    lastRampMs_ = now;
    if (current_ >= target_ - kEps) {
        current_ = target_;
        transitionShot(ShotState::Locked);
    }
    publish();
}

void MeterDelayController::beginEngagement(qint64 now)
{
    // Refresh the target from the latest smoothed RTT before each shot so the
    // whole read runs on the freshest adaptive value.
    // Ready only: a Backoff session runs on enterBackoff()'s deliberately
    // reduced target, which this refresh must not restore mid-degradation.
    if (session_ == SessionState::Ready) {
        computeTarget();
    }
    engagedAtMs_ = now;
    transitionShot(ShotState::Engaging);
}

// ─── Network health ─────────────────────────────────────────────────────────

void MeterDelayController::updateNetworkHealth(double rttMs, double jitterMs,
                                               bool rttVerified, bool packetLoss)
{
    lastRtt_ = std::isfinite(rttMs) ? std::max(0.0, rttMs) : 0.0;
    lastJitter_ = std::isfinite(jitterMs) ? std::max(0.0, jitterMs) : 0.0;
    lastRttVerified_ = rttVerified;
    lastLoss_ = packetLoss;

    // Maintain a smoothed one-way latency estimate for adaptive targeting.
    // Only verified RTT is trustworthy enough to drive an actuator.
    if (rttVerified && std::isfinite(rttMs) && rttMs > 0.0) {
        const double oneWay = rttMs / 2.0;
        if (!haveRttEstimate_) {
            smoothedOneWayMs_ = oneWay;
            haveRttEstimate_ = true;
        } else {
            // EMA with alpha=0.15: responsive enough to track real changes,
            // smooth enough to ignore individual spikes.
            constexpr double kAlpha = 0.15;
            smoothedOneWayMs_ = kAlpha * oneWay + (1.0 - kAlpha) * smoothedOneWayMs_;
        }
    }

    if (session_ == SessionState::Probing) {
        probes_.push_back(Sample{lastRtt_, lastJitter_});
        while (probes_.size() > 64) probes_.pop_front();
    }
}

// ─── State machine ──────────────────────────────────────────────────────────

bool MeterDelayController::sessionGatesOpen() const noexcept
{
    return enabled_ && playing_ && courtKnown_ && serviceSupported_ && !shuttingDown_;
}

bool MeterDelayController::shotLayerActive() const noexcept
{
    return session_ == SessionState::Ready || session_ == SessionState::Backoff;
}

void MeterDelayController::evaluateSessionGates()
{
    if (shuttingDown_) return;
    if (!sessionGatesOpen()) {
        if (session_ != SessionState::Idle || current_ != 0.0
            || shot_ != ShotState::Standby || interceptRequested_) {
            QString why = QStringLiteral("Idle");
            if (!enabled_) why = QStringLiteral("Disabled");
            else if (!playing_) why = QStringLiteral("No game in progress");
            else if (!courtKnown_) why = QStringLiteral("Court IP unknown");
            else if (!serviceSupported_) why = QStringLiteral("Service too old (needs v3+)");
            enterIdle(why);
            publish();
        }
    }
}

void MeterDelayController::enterIdle(const QString& reason)
{
    if (interceptRequested_) {
        interceptRequested_ = false;
        emit interceptStopRequested();
    }
    probes_.clear();
    baselineValid_ = false;
    baseJitter_ = 0.0;
    baseRtt_ = 0.0;
    smoothedOneWayMs_ = 0.0;
    haveRttEstimate_ = false;
    current_ = 0.0;
    lastRampMs_ = 0;
    edgeLatchExpiryMs_ = 0;
    // The dead-ball latch is session-scoped: a new session must wait for its own
    // dead ball rather than inheriting a stale engagement from the last one.
    deadBallLatched_ = false;
    // [ORION_DEFENSE_FLAG 2026-08-08] The manual defense flag is session-scoped
    // and NON-PERSISTENT by design: every session starts in offense mode (delay
    // applied). A customer must never discover, one restart later, that the
    // delay has been "silently off" because a defense toggle survived teardown.
    defenseModeManualActive_ = false;
    // [ORION_METER_DELAY_FIRST_SHOT 2026-08-07] The initial-ramp arm bypass is
    // session-scoped: a new session must re-earn its "initial climb" bypass.
    initialRampReachedTarget_ = false;
    transitionShot(ShotState::Standby);
    transitionSession(SessionState::Idle, reason);
}

void MeterDelayController::enterBackoff(qint64 now)
{
    const double base = manualMode()
        ? std::clamp(manual_, kManualMinMs, kManualMaxMs)
        : target_;
    target_ = std::max(kBackoffFloorMs, base - kBackoffStepMs);
    stableSinceMs_ = now;
    transitionSession(SessionState::Backoff,
                      QStringLiteral("Network degraded - delay target reduced"));
}

void MeterDelayController::computeTarget()
{
    if (manualMode()) {
        target_ = std::clamp(manual_, kManualMinMs, kManualMaxMs);
        return;
    }
    // Adaptive auto mode: hold the TOTAL inbound latency (network + injected)
    // constant at kDesiredTotalInboundMs so the meter behaves identically
    // regardless of connection quality.
    if (haveRttEstimate_ && smoothedOneWayMs_ > 0.0) {
        target_ = std::clamp(kDesiredTotalInboundMs - smoothedOneWayMs_,
                             kAdaptiveFloorMs, kAdaptiveCeilingMs);
    } else {
        target_ = kDefaultDelayMs;
    }
}

void MeterDelayController::microAdjustTarget()
{
    // Auto mode only, and only between shots. Drifting the target while a shot
    // is engaged would change meter velocity mid-read, which is precisely the
    // failure mode this controller exists to avoid. The RTT EMA already smooths
    // out noise, so re-deriving the target is all the adjustment needed.
    if (manualMode() || shot_ != ShotState::Standby) return;
    computeTarget();
}

bool MeterDelayController::healthDegraded() const noexcept
{
    if (!baselineValid_) return false;
    if (lastLoss_) return true;
    if (baseJitter_ > 0.0 && lastJitter_ > baseJitter_ * kJitterMult) return true;
    // An unverified RTT is a display-only number; it must not drive an actuator
    // decision on its own.
    if (baseRtt_ > 0.0 && lastRttVerified_ && lastRtt_ > baseRtt_ * kRttMult) return true;
    return false;
}

bool MeterDelayController::engageStartWanted(qint64 now) const noexcept
{
    switch (policy_) {
    case EngagePolicy::AlwaysOn:
        // The session gates already required enabled && playing && courtKnown &&
        // serviceSupported. Once armed there is nothing further to wait for, and
        // never disengaging is the entire point: zero mid-play transitions.
        return true;
    case EngagePolicy::DeadBall:
        return deadBallLatched_;
    case EngagePolicy::OffenseDefense:
        // [ORION_DEFENSE_FLAG 2026-08-08] The manual defense flag is the SOLE
        // gate. History, because this is the third wiring in two days:
        //   v1: offense_ only — but the app seeded it permanently true, so the
        //       policy was AlwaysOn with a different name (measured live:
        //       Locked at 250 ms for 5.5 min of mixed play).
        //   v2 (2026-08-08 morning): offense_ || squareHeld_ || shotCycle ||
        //       edge latch — "works a little", but setShotCycleActive proved
        //       unreliable and the 100 ms/s slew lagged every real possession
        //       change the cycle did catch.
        //   v3 (this): the owner toggles defense himself on D-pad Up. flag
        //       false (default) = offense = apply; flag true = defense =
        //       bypass. Nothing else participates: not the shot cycle, not the
        //       physical Square, not the edge latch, not offense_ (no
        //       possession detector exists to feed it).
        return !defenseModeManualActive_;
    case EngagePolicy::ShotGated:
        return squareHeld_ || now < edgeLatchExpiryMs_;
    }
    return false;
}

bool MeterDelayController::policyHoldsIndefinitely() const noexcept
{
    // AlwaysOn and DeadBall deliberately have no disengage condition short of
    // the session going Idle. Their whole value is that the applied delay is
    // CONSTANT across every shot, which is what keeps the single scalar
    // learned_latency_ms honest.
    return policy_ == EngagePolicy::AlwaysOn || policy_ == EngagePolicy::DeadBall;
}

bool MeterDelayController::shouldDisengage(qint64 now) const noexcept
{
    // [ORION_DEFENSE_FLAG 2026-08-08] An explicit defense toggle disengages NOW.
    // The minEngagedMs dwell and disengage holdoff exist to protect an in-flight
    // meter read from a ramp the BOT's own activity would otherwise trigger; a
    // deliberate user command is a different animal — delaying it just leaves the
    // customer lagged on defense for up to another ~1.15 s. Fail-closed is
    // unaffected: the ramp-down publishes settled=false and readyForArm()
    // refuses from its first tick, so no armed token survives the transition.
    if (policy_ == EngagePolicy::OffenseDefense && defenseModeManualActive_) {
        return true;
    }
    if (policyHoldsIndefinitely()) {
        // No disengage path at all. NOTE this deliberately also bypasses the
        // kMaxEngagedMs stuck-state watchdog below: for these policies a
        // permanently held delay is the INTENDED behaviour, not a stuck state.
        // Safety is not weakened -- enterIdle() still forces zero on disable,
        // game end, court-IP loss, stream end, shutdown and destruction, and
        // nexus_svc's own dead-man switch and starvation watchdog are
        // independent of anything decided here.
        return false;
    }
    // Watchdog: a stuck bot hint or a stuck latch must never pin the delay on
    // forever. A genuinely held physical button is real live input and is
    // exempt.
    //
    // [ORION_METER_DELAY_WATCHDOG_D1 2026-08-08] The policy's own live demand
    // (engageStartWanted) is exempt too. engagedAtMs_ is only reset by
    // beginEngagement(), which never runs while Locked, so under OffenseDefense
    // (the shipped default: meterDelayBypassOnDefense=true) 6 s of continuous
    // offense used to trip this line: Locked -> Disengaging for exactly one
    // tick, publish() emitted settled=false, and OrionAppController's
    // delayConditionChanged -> syncEngineArmed() wiring revoked any armed
    // precise-fire token -- a ~1% duty cycle (one 50 ms flip per 6 s) that
    // silently killed 1-3 shots per 50-shot batch as
    // authority_lost_before_submit, while current_ never left target.
    //
    // The watchdog's real prey is unbounded STALE inputs, and those still trip
    // it: the ShotGated edge latch self-expires at most minEngagedMs_
    // (<= kMaxEngagedMs) after the last real edge, and AlwaysOn / DeadBall never
    // reach this line (policyHoldsIndefinitely). [ORION_DEFENSE_FLAG 2026-08-08]
    // The shot-cycle hint no longer participates in engagement at all, so the
    // "stuck hint" hazard class is gone outright. Under OffenseDefense the
    // manual defense flag was already handled at the top of this function, and
    // engageStartWanted is simply !flag — holding while the user stays on
    // offense is the INTENDED behaviour, exactly like AlwaysOn, and enterIdle()
    // still forces zero on session end regardless. This condition strictly
    // REMOVES disengage events; it can never add one.
    if (!squareHeld_ && !engageStartWanted(now)
        && now - engagedAtMs_ >= kMaxEngagedMs) return true;
    if (engageStartWanted(now)) return false;
    if (now - engagedAtMs_ < minEngagedMs_) return false;
    return now - lastEngageRequestMs_ >= disengageHoldoffMs_;
}

bool MeterDelayController::rampDue(qint64 now) const noexcept
{
    // Half a tick of slack: keeps the ramp rate honest when a physical edge has
    // just applied a step microseconds before a scheduled tick.
    return now - lastRampMs_ >= (kTickMs / 2);
}

void MeterDelayController::rampUp(qint64 now)
{
    if (!rampDue(now)) return;
    current_ = std::min(target_, current_ + kRampUpPerTickMs);
    lastRampMs_ = now;
    if (current_ >= target_ - kEps) {
        current_ = target_;
        if (shot_ != ShotState::Locked) transitionShot(ShotState::Locked);
    }
}

void MeterDelayController::rampDown(qint64 now)
{
    if (!rampDue(now)) return;
    current_ = std::max(0.0, current_ - kRampDownPerTickMs);
    lastRampMs_ = now;
    if (current_ <= kEps) {
        current_ = 0.0;
        transitionShot(ShotState::Standby);
    }
}

void MeterDelayController::tick()
{
    if (shuttingDown_) return;
    const qint64 now = nowMs();
    tickSession(now);
    tickShot(now);
    publish();
}

void MeterDelayController::tickSession(qint64 now)
{
    if (!sessionGatesOpen()) {
        if (session_ != SessionState::Idle) {
            enterIdle(enabled_ ? QStringLiteral("Session gates closed")
                               : QStringLiteral("Disabled"));
        }
        return;
    }

    switch (session_) {
    case SessionState::Idle: {
        interceptRequested_ = true;
        emit interceptStartRequested();
        probes_.clear();
        probeStartMs_ = now;
        computeTarget();
        transitionSession(SessionState::Probing,
                          QStringLiteral("Measuring network baseline"));
        break;
    }
    case SessionState::Probing: {
        const qint64 elapsed = now - probeStartMs_;
        const bool haveSamples =
            static_cast<int>(probes_.size()) >= kMinProbes && elapsed >= kMinProbeMs;
        if (haveSamples) {
            double jitterSum = 0.0;
            double rttSum = 0.0;
            for (const auto& s : probes_) {
                jitterSum += s.jitter;
                rttSum += s.rtt;
            }
            const double n = static_cast<double>(probes_.size());
            baseJitter_ = jitterSum / n;
            baseRtt_ = rttSum / n;
            baselineValid_ = true;
            // Seed the adaptive estimate from the baseline so computeTarget()
            // has an RTT by the time the session becomes Ready. Probe samples
            // are unverified, so updateNetworkHealth() may not have seeded it.
            if (!haveRttEstimate_ && baseRtt_ > 0.0) {
                smoothedOneWayMs_ = baseRtt_ / 2.0;
                haveRttEstimate_ = true;
            }
            computeTarget();
            stableSinceMs_ = now;
            transitionSession(SessionState::Ready, readyReason(
                QStringLiteral("Armed - baseline locked")));
        } else if (elapsed >= kProbeTimeoutMs) {
            baselineValid_ = false;
            computeTarget();
            stableSinceMs_ = now;
            transitionSession(SessionState::Ready, readyReason(
                QStringLiteral("Armed - no health telemetry (backoff disabled)")));
        }
        break;
    }
    case SessionState::Ready: {
        if (healthDegraded()) {
            enterBackoff(now);
            break;
        }
        microAdjustTarget();
        break;
    }
    case SessionState::Backoff: {
        if (healthDegraded()) {
            stableSinceMs_ = now;
        } else if (now - stableSinceMs_ >= kBackoffRecoverMs) {
            computeTarget();
            // [ORION_METER_DELAY_RECOVER_SNAP 2026-08-07] Snap current_=target_ on
            // the Backoff -> Ready RECOVER edge, mirroring the Backoff-DOWN snap
            // in tickShot()'s Locked case (lines 618-620 in the original). Without
            // this the Locked-case rampUp() below climbs over ~4 ticks (~200 ms),
            // and publish() reports nowSettled=false the whole way -- that in turn
            // fires delayConditionChanged which OrionAppController wires to
            // syncEngineArmed() (OrionAppController.cpp:3746-3749), disarming the
            // engine mid-play. Direction is upward and target moved DOWN during
            // backoff; snapping back up is not a mid-shot perturbation (the meter
            // was reading against the reduced Backoff target already) - it just
            // shortcuts the re-ramp so the engine stays armed.
            current_ = target_;
            lastRampMs_ = now;
            transitionSession(SessionState::Ready,
                              QStringLiteral("Network recovered - delay target restored"));
        }
        break;
    }
    }
}

void MeterDelayController::tickShot(qint64 now)
{
    if (!shotLayerActive()) {
        if (current_ != 0.0) {
            current_ = 0.0;
            lastRampMs_ = now;
        }
        if (shot_ != ShotState::Standby) transitionShot(ShotState::Standby);
        return;
    }

    if (engageStartWanted(now)) {
        lastEngageRequestMs_ = now;
    }

    switch (shot_) {
    case ShotState::Standby:
        current_ = 0.0;
        if (engageStartWanted(now)) {
            beginEngagement(now);
            rampUp(now);
        }
        break;

    case ShotState::Engaging:
        if (shouldDisengage(now)) {
            transitionShot(ShotState::Disengaging);
            break;
        }
        rampUp(now);
        break;

    case ShotState::Locked:
        // Backoff tracking: a reduced target takes effect immediately, even
        // mid-shot. Safety outranks read stability.
        if (current_ > target_ + kEps) {
            current_ = target_;
            lastRampMs_ = now;
        } else if (current_ < target_ - kEps) {
            rampUp(now);
        }
        if (shouldDisengage(now)) {
            transitionShot(ShotState::Disengaging);
        }
        break;

    case ShotState::Disengaging:
        if (engageStartWanted(now)) {
            // Rapid succession: resume from wherever the ramp currently is
            // rather than restarting from zero.
            beginEngagement(now);
            rampUp(now);
            break;
        }
        rampDown(now);
        break;
    }
}

// ─── Transitions / publication ──────────────────────────────────────────────

void MeterDelayController::transitionSession(SessionState next, const QString& reason)
{
    reason_ = reason;
    if (session_ == next) return;
    session_ = next;
    emit sessionStateChanged(session_);
}

void MeterDelayController::transitionShot(ShotState next)
{
    if (shot_ == next) return;
    shot_ = next;
    // [ORION_METER_DELAY_FIRST_SHOT 2026-08-07] Latch first Locked so readyForArm()
    // can tell "initial monotonic climb" from "recovery re-ramp" (see .h).
    if (shot_ == ShotState::Locked) {
        initialRampReachedTarget_ = true;
    }
    emit shotStateChanged(shot_);
}

bool MeterDelayController::readyForArm() const noexcept
{
    // Disabled or shutting down: outer callers already collapse !enabled() to true
    // (delay off means no gate). Return true here defensively.
    if (!enabled_ || shuttingDown_) return true;
    // Genuinely settled at 0 (Standby) or at target (Locked) always qualifies -
    // this is the strictly stronger condition and matches conditionSettled().
    if (settled_) return true;
    // Session not yet arming the shot layer: the shot layer produces no ramp; the
    // "engine dead-window" doesn't apply. Report NOT ready so the outer gate stays
    // conservative (matches conditionSettled()'s behaviour in these states).
    if (!shotLayerActive()) return false;
    // [ORION_METER_DELAY_WATCHDOG_D1 2026-08-08] Belt-and-suspenders for the
    // watchdog phantom flip (root cause fixed in shouldDisengage()): if the
    // applied delay is AT target and the policy still actively wants the
    // engagement, a momentary Disengaging label is pure bookkeeping -- tickShot()
    // is guaranteed to re-engage on the very next tick without current_ ever
    // moving, so disarming the engine here would revoke a healthy in-flight
    // shot for nothing. All three conditions are required:
    //   * at-target: the transport condition genuinely has not changed;
    //   * engageStartWanted: distinguishes the phantom flip from the FIRST tick
    //     of a legitimate disengage (which is also still at-target). Without
    //     this term a genuine ramp-down would sail through, and because
    //     syncEngineArmed() is only re-polled on delayConditionChanged edges
    //     (none fire mid-ramp; settled_ is already false) the engine would stay
    //     armed for the entire ~1.65 s ramp -- a fail-closed regression.
    //   * Locked/Disengaging: never widens the Engaging-ramp or Standby paths.
    if ((shot_ == ShotState::Locked || shot_ == ShotState::Disengaging)
        && std::abs(current_ - target_) <= 0.5
        && engageStartWanted(nowMs())) {
        return true;
    }
    // [ORION_DEFENSE_FLAG 2026-08-08] Re-engagement climb under OffenseDefense:
    // every defense -> offense flip of the manual flag starts a fresh Engaging
    // climb back to target (formerly this happened per shot under the shot-cycle
    // wiring). That climb is exactly as monotonic as the initial ramp accepted
    // below — rampDown() never runs in the Engaging state, the manual target is
    // fixed for the whole climb, and a Backoff target cut mid-climb only SNAPS
    // current_ down to the (lower) target and Locks. Refusing here would open a
    // multi-second dead window after every defensive possession the moment a
    // session has Locked once (initialRampReachedTarget_ latches). The
    // engageStartWanted() term distinguishes this from a ramp the policy no
    // longer wants (fail-closed unchanged for genuine disengages), and
    // Locked-state re-ramps (mid-lock target changes) still refuse — pinned by
    // midLockTargetChangeIsADelayGateDisarmOnly.
    if (policy_ == EngagePolicy::OffenseDefense
        && shot_ == ShotState::Engaging
        && current_ + kEps < target_
        && engageStartWanted(nowMs())) {
        return true;
    }
    // Initial monotonic ramp to first target since session start. The applied delay
    // is < target but climbing at kSlewMsPerSec == 100 ms/s, so a fired shot reads a
    // partial delay that is stable-ish and NEVER decreases mid-flight. Arming here
    // recovers the ~2.5 s dead window at the cost of some read-window drift on the
    // first shot only. Once we've hit Locked once (initialRampReachedTarget_), the
    // "initial" property is spent; a later Engaging state is a recovery re-ramp
    // that must gate on Fix 2's snap, not this bypass.
    if (initialRampReachedTarget_) return false;
    // Must be actively climbing UPWARD toward target. Guards against pathological
    // states where current_ > target_ transiently (e.g., during backoff clamp).
    return current_ + kEps < target_ && shot_ == ShotState::Engaging;
}

void MeterDelayController::updateTimer()
{
    if (manualTick_ || shuttingDown_ || !enabled_) {
        tickTimer_.stop();
        return;
    }
    if (!tickTimer_.isActive()) tickTimer_.start();
}

void MeterDelayController::setManualTickMode(bool manual)
{
    manualTick_ = manual;
    updateTimer();
}

void MeterDelayController::setMonotonicClockForTesting(std::function<qint64()> clock)
{
    clock_ = std::move(clock);
}

QString MeterDelayController::sessionStateString() const
{
    switch (session_) {
    case SessionState::Idle:    return QStringLiteral("Idle");
    case SessionState::Probing: return QStringLiteral("Probing");
    case SessionState::Ready:   return QStringLiteral("Ready");
    case SessionState::Backoff: return QStringLiteral("Backoff");
    }
    return QStringLiteral("Idle");
}

QString MeterDelayController::shotStateString() const
{
    switch (shot_) {
    case ShotState::Standby:     return QStringLiteral("Standby");
    case ShotState::Engaging:    return QStringLiteral("Engaging");
    case ShotState::Locked:      return QStringLiteral("Locked");
    case ShotState::Disengaging: return QStringLiteral("Disengaging");
    }
    return QStringLiteral("Standby");
}

void MeterDelayController::publish()
{
    // Hard output clamp. Nothing above kHardMaxMs ever reaches the service, and
    // a disabled / shutting-down controller can only ever command zero.
    double commanded = std::clamp(current_, 0.0, kHardMaxMs);
    if (!enabled_ || shuttingDown_) commanded = 0.0;
    current_ = commanded;

    bool visibleChange = false;

    // A non-zero delay must be RE-ASSERTED, not merely set once: nexus_svc
    // treats >500 ms of set_meter_delay silence as a dead controller and forces
    // the delay to 0. During a Locked shot the value is constant for the whole
    // dwell, so change-only emission starved that watchdog and the service
    // zeroed the delay mid-meter-read. Keepalives are emitted on the same
    // signal; NetworkBridge coalesces them, so the cost is one JSON line per
    // 150 ms while a shot is engaged and nothing at all while idle.
    const bool valueChanged = std::abs(commanded - lastCommanded_) > kEps;
    const qint64 emitNow = nowMs();
    const bool keepaliveDue =
        commanded > kEps && !shuttingDown_
        && (!haveCommandEmit_ || emitNow - lastCommandEmitMs_ >= kKeepaliveMs);

    if (valueChanged || keepaliveDue) {
        lastCommanded_ = commanded;
        lastCommandEmitMs_ = emitNow;
        haveCommandEmit_ = true;
        // Only a real change is user-visible; a keepalive must not churn the UI.
        if (valueChanged) visibleChange = true;
        emit delayCommanded(commanded);
    }

    // ── Settled delay condition ──
    const bool nowSettled =
        (shot_ == ShotState::Standby && commanded <= kEps)
        || (shot_ == ShotState::Locked && std::abs(commanded - target_) <= 0.5);
    const double quantised =
        std::round(commanded / kConditionQuantumMs) * kConditionQuantumMs;
    const bool keyChanged = nowSettled && std::abs(quantised - conditionKey_) > kEps;

    if (nowSettled != settled_ || keyChanged) {
        settled_ = nowSettled;
        if (nowSettled) {
            settledDelay_ = commanded;
            conditionKey_ = quantised;
        }
        if (keyChanged) {
            ++conditionEpoch_;
        }
        visibleChange = true;
        emit delayConditionChanged(conditionEpoch_, conditionKey_, settled_);
    }

    const QString summary = QStringLiteral("%1|%2|%3|%4|%5")
                                .arg(sessionStateString(), shotStateString(),
                                     QString::number(commanded, 'f', 1),
                                     QString::number(target_, 'f', 1),
                                     reason_);
    if (summary != lastPublishedSummary_) {
        lastPublishedSummary_ = summary;
        visibleChange = true;
    }

    if (visibleChange) {
        emit stateTextChanged();
    }
}

} // namespace orion
