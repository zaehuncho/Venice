#pragma once

#include "AppConfig.h"

#include <QtCore/QByteArray>
#include <QtCore/QLatin1String>
#include <QtCore/QProcessEnvironment>
#include <QtCore/QString>
#include <QtCore/QStringList>

namespace orion {

// ============================================================================
//  SHIPPED READER PROFILE  (2026-08-04)
//
//  THE PROBLEM THIS SOLVES.  Timing-critical SimpleMeterReader/latency flags
//  are turned ON by run_orion.local.ps1, which
//  is gitignored (.gitignore:121 `*.local.ps1`) and appears in no packaging
//  list (installer/orion.iss [Files]; tools/package_orion_release.py). Every
//  live batch this project has ever graded — and therefore every timing number
//  the release lead was calibrated against — ran with those three ON. A
//  customer build inherits ZERO ORION_* variables, so it would run a detector
//  configuration that has never been played on, and whose fill differs from the
//  calibrated one by a strictly one-sided bias (measured below).
//
//  The correction belongs HERE rather than only in the Python defaults because
//  the native side is what actually spawns the sidecar, so pinning it here is
//  what makes dev and customer identical no matter which launcher (or none)
//  started the app. Flipping the Python defaults as well is complementary, not
//  redundant: it fixes the offline harnesses, which spawn no native parent.
//
//  MEASURED, production-wired A/B (require_gameplay_eligibility=True + a real
//  hw arm — NOT the SimpleMeterReader(W,H) shape the regression gates use, which
//  production never runs), 8,592 framedump frames, session_20260804_091119:
//
//    flag                 det flips   frames w/ fill change   mean delta   max
//    ORION_READER_ANCHOR       0        4    (0.17%)          +19.58 pp   +77.09
//    ORION_READER_PCTL_FILL    0      479   (20.52%)           +0.26 pp    +1.12
//    ORION_READER_TRACK_H_CAP  0       59    (2.53%)           +5.65 pp   +10.72
//    all three together        0      540   (23.14%)           +0.99 pp   +77.09
//
//  Two properties matter more than the magnitudes:
//    * DETECTION IS BYTE-IDENTICAL. Zero frames gain or lose a detection under
//      any of the three. None of them can cause a missed meter, so none of them
//      can turn a working install into a fail-closed one.
//    * THE BIAS IS ONE-SIDED. 539 of 540 changed frames move UP. These flags
//      remove UNDER-reads (an occlusion-truncated red extent; a fill denominator
//      poisoned by one over-tall track measurement). Shipping them OFF does not
//      add noise, it re-introduces a per-shot correlated, horizon-flat offset —
//      exactly the error class frame averaging cannot remove.
//
//  CONTRACT. Production always wins over an inherited process environment so
//  a customer cannot accidentally run an uncertified timing profile. Development
//  preserves explicit values, including "0", so controlled A/B runs remain easy.
//  Only silence means "apply the shipped profile" in a development build.
// ============================================================================

// The reader flags the shipped profile pins ON when the environment is silent.
// Additions here must clear the same bar: live-graded ON, and a production-wired
// A/B showing zero detection flips.
inline const char* const kShippedReaderProfileFlags[] = {
    // N1 anchors box velocity to the fill-invariant floor/notch instead of the
    // fill-column centre, which on a static rising meter gains a phantom upward
    // term and walks the coast/relocate window off the meter. N3 median-steadies
    // the emitted box width only. Fixed 176 of 2,334 detected frames' box
    // positions in the A/B, and one +77 pp fill misread caused by that walk-off.
    "ORION_READER_ANCHOR",
    // Occlusion-tolerant percentile read of the red extent (20th percentile of
    // per-column tops with run-length solidity) in place of the row-mean
    // crossing, which truncates low whenever the player's arm crosses the meter.
    "ORION_READER_PCTL_FILL",
    // Refuses an over-tall full-cap candidate into the fill DENOMINATOR history.
    // Without it one inflated measurement drags the 5-sample median up and every
    // fill for the rest of that lock is divided by a track ~10% too tall.
    "ORION_READER_TRACK_H_CAP",
    // These three remove per-shot ruler movement, acquisition-time ruler jitter,
    // and mid-shot fallback to a different base reference. All three were ON in
    // the live timing lane while their Python defaults remained OFF.
    "ORION_METER_SUBPIXEL_SESSION_RULER",
    "ORION_METER_SUBPIXEL_SESSION_PROVISIONAL",
    "ORION_METER_SUBPIXEL_BASE_HOLD",
    // The learned locator and armed-only priority acquire are part of the live
    // production detector. Without these, a clean install silently drops back
    // to the colour scan and first sight can arrive several frames too late.
    "ORION_METER_DETECTOR",
    "ORION_METER_DETECTOR_SYNC_ACQUIRE",
    // Preserve the last converged latency authority while a new regime earns
    // replacement evidence. The hard-reopen path caused multi-second holds.
    "ORION_LATENCY_REGIME_REOPEN_SOFT",
    // A short, structure-proven bridge carries a rising fill through a partial
    // top-strip occlusion. Production pins this so an inherited process value
    // cannot silently turn off the certified recovery path.
    "ORION_METER_PARTIAL_OCCLUSION",
};

struct ShippedReaderProfileValue {
    const char* name;
    const char* value;
};

// Numeric bounds are part of the certified behavior, not user tuning. The
// occlusion bridge is deliberately short and evidence-gated; widening these
// values would make stale detector evidence look current. The detector
// threshold is the held-out operating point (2,122/2,122 positives and zero
// false positives across 1,420 negatives).
inline const ShippedReaderProfileValue kShippedReaderProfileValues[] = {
    {"ORION_METER_PARTIAL_OCCLUSION_MAX_MS", "120"},
    {"ORION_METER_NEGATIVE_BRIDGE_MAX_MS", "45"},
    {"ORION_METER_PARTIAL_OCCLUSION_MIN_DIRECT", "3"},
    {"ORION_METER_PARTIAL_OCCLUSION_MIN_COLS", "2"},
    {"ORION_METER_DETECTOR_CONF", "0.35"},
    // DirectML is the package-complete accelerated provider certified by the
    // build smoke. CPU remains an explicit fallback after DML, never first.
    {"ORION_METER_PROVIDER_PRIORITY", "dml,cpu"},
    // 2k_Vision pure-CV landmark locator (replaces YOLO ONNX model).
};

// Native estimator flags calibrated with the same release profile. These must
// be resolved before OrionAppController constructs AutomationEngine; setting
// them only on the later child-process environment leaves the native scheduler
// on its uncalibrated defaults.
inline const char* const kShippedNativeTimingProfileFlags[] = {
    "ORION_HORIZON_DEBIAS",
    "ORION_RAMP_SHAPE",
    // [SHIP_PARITY R1 2026-09-23] A dated, horizon-plausible phase member can no longer be
    // withdrawn by the extrapolating sampler (+37.9 ms late bias). Live on the owner's rig
    // since 2026-09-11 (run_orion.local.ps1); the fix that removed the random EARLIES. The
    // compiled default (AutomationEngine.h tipPhaseSolo=false) stays so the engine's own
    // fixtures are untouched -- the same pattern as HORIZON_DEBIAS / RAMP_SHAPE above.
    "ORION_TIP_PHASE_SOLO",
};

// Native numeric pins, resolved exactly like the flags above (production force-pins,
// development preserves an explicit operator value).
struct ShippedNativeTimingProfileValue {
    const char* name;
    const char* value;
};
inline const ShippedNativeTimingProfileValue kShippedNativeTimingProfileValues[] = {
    // [SHIP_PARITY R2 2026-09-23] Curve-model rate stretch OFF. The compiled 0.6 latches on
    // the slow first segment and produced +57..+101 ms LATE fires (09-09: 5/5 of the severe
    // lates). The owner's rig has pinned 0 since 2026-09-09. The compiled default stays 0.6
    // only because 46 mechanism tests pin it (AutomationEngine.h curveRateStretchAlpha).
    {"ORION_CURVE_STRETCH_ALPHA", "0"},
};

inline constexpr const char kShippedTimingProfileId[] =
    "2k27-2026-09-23-v3";

// DELIBERATELY NOT IN THE PROFILE, with the reason, so this list is not
// re-litigated:
//   ORION_READER_FAKELOCK_BREAK — the launcher sets it, but it is a provable
//     no-op. Both call sites read `(self._fakelock_break or self._stalebreak)`
//     and `_stalebreak` is default-ON in simple_meter_reader.py, unset by the
//     launcher, so the OR is already satisfied in dev AND in a customer build.
//     Confirmed in the A/B: adding it to the other three changed nothing.
//   ORION_READER_ROBUST — never set by the launcher either, so it is OFF on the
//     dev rig too. It has no live hours in EITHER configuration; pinning it ON
//     would ship something genuinely untested, which is the exact failure this
//     header exists to prevent.
//   ORION_READER_TRACK_H_ROBUST_GATE — reads by no code at all (zero hits across
//     every .py/.cpp/.h/.qml in the tree). The launcher line and the A/B numbers
//     attached to it describe a mechanism that has never existed.
//   ORION_READER_BOX_PREDICT / ORION_READER_BOX_TIGHT — local experiments that
//     alter the emitted detector box. They remain outside the shipped profile
//     until a held-out production A/B proves no detection/ownership regression.
//   ORION_METER_DETECTOR_PHASED_ACQUIRE — deliberately OFF in the live launcher;
//     production keeps the full-frame armed-priority acquisition path.

// Apply the shipped reader profile to `env`. Development can preserve explicit
// values; production force-pins the certified values. Returns a compact,
// log-safe summary of the RESOLVED configuration
// — e.g. "ORION_READER_ANCHOR=1(profile) ORION_READER_PCTL_FILL=0(env) ..." —
// so a support log states exactly which reader config produced the session and
// a divergence like this one can never again be invisible.
[[nodiscard]] inline QString applyShippedReaderProfile(
    QProcessEnvironment& env, bool preserveExplicitOverrides = true)
{
    QStringList resolved;
    for (const char* const name : kShippedReaderProfileFlags) {
        const QString key = QString::fromLatin1(name);
        QString value;
        QLatin1String origin("profile");
        if (preserveExplicitOverrides && env.contains(key)) {
            value = env.value(key);
            origin = QLatin1String("env");
        } else {
            value = QStringLiteral("1");
            env.insert(key, value);
            if (!preserveExplicitOverrides) {
                origin = QLatin1String("production");
            }
        }
        resolved.append(key + QLatin1Char('=') + value + QLatin1Char('(') + origin
                        + QLatin1Char(')'));
    }
    for (const auto& setting : kShippedReaderProfileValues) {
        const QString key = QString::fromLatin1(setting.name);
        QString value;
        QLatin1String origin("profile");
        if (preserveExplicitOverrides && env.contains(key)) {
            value = env.value(key);
            origin = QLatin1String("env");
        } else {
            value = QString::fromLatin1(setting.value);
            env.insert(key, value);
            if (!preserveExplicitOverrides) {
                origin = QLatin1String("production");
            }
        }
        resolved.append(key + QLatin1Char('=') + value + QLatin1Char('(') + origin
                        + QLatin1Char(')'));
    }
    return resolved.join(QLatin1Char(' '));
}

// The meter box proposer is a USER SETTING (AppConfigData::meterProposer, the
// Live page's "Meter Detection" card), not a certified profile flag, so it is
// resolved separately from the lists above: the setting is the source of truth
// and the value is always normalised (normalizedMeterProposer) before it is
// exported. Same override contract as applyShippedReaderProfile: production
// ignores an inherited process value; development keeps an explicit one
// (run_orion.local.ps1 pins it for A/B batches). Returns the same
// "KEY=value(origin)" log form so the launch log names which proposer ran.
inline constexpr const char kMeterProposerEnvKey[] = "ORION_METER_PROPOSER";

[[nodiscard]] inline QString applyMeterProposerSetting(
    QProcessEnvironment& env, const QString& setting,
    bool preserveExplicitOverrides = true)
{
    const QString key = QString::fromLatin1(kMeterProposerEnvKey);
    QString value;
    QLatin1String origin("setting");
    if (preserveExplicitOverrides && env.contains(key)
        && !env.value(key).trimmed().isEmpty()) {
        value = normalizedMeterProposer(env.value(key));
        env.insert(key, value);
        origin = QLatin1String("env");
    } else {
        value = normalizedMeterProposer(setting);
        env.insert(key, value);
        if (!preserveExplicitOverrides) {
            origin = QLatin1String("production");
        }
    }
    return key + QLatin1Char('=') + value + QLatin1Char('(') + origin + QLatin1Char(')');
}

// ============================================================================
//  [ORION_PILL_YOLO_ROUTE 2026-09-17]  PILL -> YOLO PROPOSER ROUTE
//
//  MEASURED (docs/PILL_STYLE_STATUS.md, 661 labelled 2K27 park frames):
//    * the shipped CV contour locator proposes a box on 0 of 642 Pill frames.
//      The Pill's achromatic-bright fill column is 3 px @720p against gate 4's
//      col_w_min of 8, so the capsule dies at the width floor on every frame.
//      (The same locator reads the Arrow2 clip in that dataset 19/19.)
//    * the packaged models/orion_meter_detector.onnx IS the 08-30 Pill-trained
//      net (sha256 6af3199c...): 661/661, IoU p50 0.93, 0 false locks on 113
//      hard negatives, at both 1080p and 720p.
//    * the reader's rung-tolerant fill is geometry-gated, not style-gated, and
//      reads Pill correctly inside either box.
//
//  So `meter_style = Pill` with the shipped `meter_proposer = cv` is a BLIND
//  session: every press in the park fires with no meter, and (before this) the
//  style never reached the locator and nothing logged that the two disagreed.
//  The style now picks the proposer: Pill routes to the detector that can see
//  it, and ORION_METER_STYLE rides along so the Python side can key on the
//  style once the locator learns it (route B in the doc).
//
//  ARROW2/STRAIGHT ARE UNTOUCHED. applyPillYoloRoute() returns before it writes
//  anything unless the style normalises to "pill", so a non-Pill launch builds
//  a byte-identical environment to the one that shipped.
inline constexpr const char kMeterStyleEnvKey[] = "ORION_METER_STYLE";
inline constexpr const char kPillYoloRouteEnvKey[] = "ORION_PILL_YOLO_ROUTE";

// The style comparison used by the route. AppConfig has already whitelisted the
// value, so this only has to be case/whitespace tolerant.
[[nodiscard]] inline bool isPillMeterStyle(const QString& meterStyle)
{
    return meterStyle.trimmed().toLower() == QLatin1String("pill");
}

// The kill switch. The SETTING (`pill_yolo_route`, default true) is the owner's
// door; ORION_PILL_YOLO_ROUTE is the operator's. Ignore-don't-guess, the same
// idiom AppConfig::sprintReleaseAllowed() uses in the other direction: an EXACT
// "0" (trimmed) disables the route and every other value -- "false", "no", a
// typo, an empty string -- leaves it on, because a mistyped kill switch must not
// silently ship a blind proposer.
[[nodiscard]] inline bool pillYoloRouteEnabled(bool setting,
                                               const QProcessEnvironment& env)
{
    if (!setting) {
        return false;
    }
    return env.value(QString::fromLatin1(kPillYoloRouteEnvKey)).trimmed()
           != QLatin1String("0");
}

struct MeterStyleRouteResult
{
    // What ORION_METER_PROPOSER carries after the route ran ("cv" | "yolo").
    QString resolvedProposer;
    // One launch log line, or empty. Either the route line or the mismatch line;
    // never both, because the route is exactly what prevents the mismatch.
    QString log;
    // The Pill -> yolo override actually fired (and ORION_METER_STYLE was set).
    bool routed = false;
};

// Run AFTER applyMeterProposerSetting(), which has already put the user's
// setting into `env`. Non-Pill styles leave `env` exactly as that call left it.
[[nodiscard]] inline MeterStyleRouteResult applyPillYoloRoute(
    QProcessEnvironment& env, const QString& meterStyle, bool pillYoloRoute)
{
    const QString proposerKey = QString::fromLatin1(kMeterProposerEnvKey);
    MeterStyleRouteResult result;
    result.resolvedProposer = env.value(proposerKey);
    if (!isPillMeterStyle(meterStyle)) {
        return result;
    }
    if (pillYoloRouteEnabled(pillYoloRoute, env)) {
        // Unconditional: the persisted proposer, and an explicit dev override of
        // it, both lose to the style here. A "cv" that cannot see the meter is
        // not a preference worth honouring, and an A/B pin that silences the
        // only proposer that works would measure nothing.
        env.insert(proposerKey, QStringLiteral("yolo"));
        env.insert(QString::fromLatin1(kMeterStyleEnvKey), QStringLiteral("pill"));
        result.resolvedProposer = QStringLiteral("yolo");
        result.routed = true;
        result.log = QStringLiteral(
            "METER STYLE: Pill -> proposer=yolo (contour locator cannot see the Pill capsule; "
            "packaged detector is Pill-trained)");
        return result;
    }
    // Kill switch down: the environment is byte-identical to the pre-route build,
    // so the one thing left to do is refuse to be silent about a blind session.
    if (normalizedMeterProposer(result.resolvedProposer) == QLatin1String("cv")) {
        result.log = QStringLiteral(
            "METER STYLE MISMATCH: style=Pill proposer=cv -- the CV contour locator proposes no "
            "box on the Pill capsule (0/642 measured frames), so this session is blind. Clear "
            "ORION_PILL_YOLO_ROUTE=0 / set pill_yolo_route true, or set meter_proposer=yolo.");
    }
    return result;
}

// Resolve the native half of the timing profile into the current process.
// Production is authoritative; development preserves an explicit operator
// value for controlled A/B work. The profile id is always pinned so every log
// and child launch can identify the behavioral contract that produced it.
[[nodiscard]] inline QString applyShippedNativeTimingProfile(
    bool preserveExplicitOverrides = true)
{
    QStringList resolved;
    for (const char* const name : kShippedNativeTimingProfileFlags) {
        const QByteArray key(name);
        QByteArray value;
        QLatin1String origin("profile");
        if (preserveExplicitOverrides && qEnvironmentVariableIsSet(name)) {
            value = qgetenv(name);
            origin = QLatin1String("env");
        } else {
            value = QByteArrayLiteral("1");
            qputenv(name, value);
            if (!preserveExplicitOverrides) {
                origin = QLatin1String("production");
            }
        }
        resolved.append(QString::fromLatin1(key) + QLatin1Char('=')
                        + QString::fromLatin1(value) + QLatin1Char('(') + origin
                        + QLatin1Char(')'));
    }
    for (const auto& setting : kShippedNativeTimingProfileValues) {
        const QByteArray key(setting.name);
        QByteArray value;
        QLatin1String origin("profile");
        if (preserveExplicitOverrides && qEnvironmentVariableIsSet(setting.name)) {
            value = qgetenv(setting.name);
            origin = QLatin1String("env");
        } else {
            value = QByteArray(setting.value);
            qputenv(setting.name, value);
            if (!preserveExplicitOverrides) {
                origin = QLatin1String("production");
            }
        }
        resolved.append(QString::fromLatin1(key) + QLatin1Char('=')
                        + QString::fromLatin1(value) + QLatin1Char('(') + origin
                        + QLatin1Char(')'));
    }
    qputenv("ORION_TIMING_PROFILE_ID", QByteArray(kShippedTimingProfileId));
    resolved.prepend(QStringLiteral("id=")
                     + QString::fromLatin1(kShippedTimingProfileId));
    return resolved.join(QLatin1Char(' '));
}

} // namespace orion
