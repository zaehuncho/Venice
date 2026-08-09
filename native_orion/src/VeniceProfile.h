#pragma once

// [VENICE_PROFILE 2026-08-08] Settings backup / restore as a customer-portable file.
//
// WHY THIS EXISTS. Shot Lead, Tip Timing, Meter Delay and the press-anchored learned
// constants are hard-won, banner-graded values. A reinstall (or a second rig) used to
// mean hand-editing settings.json — which the settings signature then flags — or
// re-tuning from scratch. This module defines ONE file format, venice-profile.json,
// that carries exactly the customer-owned timing surface and nothing else.
//
// DESIGN RULES (each is a test contract in tests/VeniceProfileTests.cpp +
// tests/test_venice_profile.py):
//   * ALLOWLIST, NEVER A DUMP. Export writes only the keys in this file; import reads
//     only those same keys. License keys, entitlement caches, session/bridge tokens,
//     machine identity (console IP, chiaki path, capture index) and machine-measured
//     latency posteriors are structurally unreachable — they are never touched, so no
//     future field addition can leak them by accident.
//   * IMPORT CLAMPS, NEVER TRUSTS. Every numeric is clamped into the same band the
//     AppConfig load path enforces (a profile file is exactly as untrusted as a
//     hand-edited settings.json), and every clamp/reject is reported to the caller for
//     the session log. A profile can therefore never install a value the settings
//     loader would have refused.
//   * IMPORT IS NON-DESTRUCTIVE for "not configured" markers: an absent/zero Shot Lead
//     or an absent Tip Timing never wipes a configured target value.
//   * EXPORT IS DETERMINISTIC (no timestamps inside the payload), so
//     export(import(export(x))) == export(x) is directly assertable.
//
// Header-only (like the policy headers) so OrionCommon consumers and the headless
// test targets share ONE implementation with no export-macro churn.

#include "AppConfig.h"

#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QString>
#include <QtCore/QStringList>

#include <algorithm>
#include <cmath>

namespace orion {

inline constexpr int kVeniceProfileVersion = 1;
inline constexpr const char* kVeniceProfileKind = "venice-profile";

// Tip Timing travels in the CANONICAL frame (learning.json learned_phase_physical_ms,
// base-30 physical ms) — the regime-invariant slot the Tip Timing card itself persists
// into — so a profile moved between base-20/base-30 installs can never double-count the
// +58.3 shift (AppConfig.h tipTimingCanonicalFromEffective). Same plausibility band the
// learning loader enforces (AppConfig.cpp loadLearningObject).
inline constexpr double kVeniceProfileTipTimingMinMs = 200.0;
inline constexpr double kVeniceProfileTipTimingMaxMs = 500.0;

// Bounds shared with AppConfig::loadSettingsObject — keep in lockstep with the
// clamps there (they are the single source of truth for what settings.json accepts).
inline constexpr double kVeniceProfileNoDipLeadBandMs = 150.0;   // +/- band
inline constexpr double kVeniceProfileShotTypeOffsetBandMs = 250.0; // +/- band
inline constexpr double kVeniceProfilePressTipMinMs = 150.0;
inline constexpr double kVeniceProfilePressTipMaxMs = 2500.0;
inline constexpr double kVeniceProfilePressSigmaMinMs = 1.0;
inline constexpr double kVeniceProfilePressSigmaMaxMs = 500.0;
inline constexpr double kVeniceProfilePressNMax = 100.0;
// Hostile-file bounds: a map bigger than this is not a shot-type table.
inline constexpr int kVeniceProfileMaxMapEntries = 32;
inline constexpr int kVeniceProfileMaxMapKeyLen = 32;

namespace detail {
inline QJsonObject mapToJson(const QMap<QString, double>& map)
{
    QJsonObject obj;
    for (auto it = map.constBegin(); it != map.constEnd(); ++it) {
        obj.insert(it.key(), it.value());
    }
    return obj;
}
} // namespace detail

// The exported payload. Deterministic; safe to diff, safe to publish (contains only
// the customer's own tuning values). Runtime-only state (e.g. the live defense-mode
// flag) and machine-specific measurements are deliberately absent.
[[nodiscard]] inline QJsonObject veniceProfileExport(const AppConfigData& data,
                                                     const LearningData& learning)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("_comment"),
               QStringLiteral(
                   "Venice timing profile: Shot Lead, Tip Timing, Meter Delay and the "
                   "per-shot-type learned press constants. Import from Venice's Debug "
                   "page (Venice Profile card). Out-of-range values are clamped on "
                   "import. Contains only your own tuning values - no account, "
                   "activation, or secret data."));
    obj.insert(QStringLiteral("kind"), QLatin1String(kVeniceProfileKind));
    obj.insert(QStringLiteral("profile_version"), kVeniceProfileVersion);

    // === Shot Lead (the ONE user-facing lead control) ===
    obj.insert(QStringLiteral("actuation_lead_ms"), data.actuationLeadMs);
    obj.insert(QStringLiteral("actuation_lead_user_set"), data.actuationLeadUserSet);

    // === Tip Timing (canonical base-30 physical; absent = never set/measured) ===
    if (learning.learnedPhasePhysicalMs > 0.0) {
        obj.insert(QStringLiteral("tip_timing_ms"), learning.learnedPhasePhysicalMs);
    }
    obj.insert(QStringLiteral("tip_timing_user_set"), data.tipTimingUserSet);
    obj.insert(QStringLiteral("tip_phase_aim_frozen"), data.tipPhaseAimFrozen);

    // === Meter Delay actuator config ===
    obj.insert(QStringLiteral("meter_delay_enabled"), data.meterDelayEnabled);
    obj.insert(QStringLiteral("meter_delay_ms"), data.meterDelayMs);
    obj.insert(QStringLiteral("meter_delay_bypass_on_defense"),
               data.meterDelayBypassOnDefense);

    // === Other customer-tuned timing knobs ===
    obj.insert(QStringLiteral("no_dip_lead_ms"), data.noDipLeadMs);
    obj.insert(QStringLiteral("press_anchored_predictor_enabled"),
               data.pressAnchoredPredictorEnabled);
    if (!data.pressAnchoredTipMs.isEmpty()) {
        obj.insert(QStringLiteral("press_anchored_tip_ms"),
                   detail::mapToJson(data.pressAnchoredTipMs));
    }
    if (!data.pressAnchoredTipSigmaMs.isEmpty()) {
        obj.insert(QStringLiteral("press_anchored_tip_sigma_ms"),
                   detail::mapToJson(data.pressAnchoredTipSigmaMs));
    }
    if (!data.pressAnchoredTipN.isEmpty()) {
        obj.insert(QStringLiteral("press_anchored_tip_n"),
                   detail::mapToJson(data.pressAnchoredTipN));
    }
    if (!data.shotTypeOffsets.isEmpty()) {
        obj.insert(QStringLiteral("shot_type_offsets"),
                   detail::mapToJson(data.shotTypeOffsets));
    }
    return obj;
}

struct VeniceProfileImportResult {
    bool ok = false;        // the file was a recognizable venice-profile
    QString error;          // set when !ok
    QStringList applied;    // keys actually installed
    QStringList notes;      // human-readable clamp/reject reports (log each)
};

// Applies a profile onto (data, learning) IN PLACE, clamping everything. On !ok the
// outputs are untouched. Never reads any key outside the export allowlist above.
[[nodiscard]] inline VeniceProfileImportResult veniceProfileImport(
    const QJsonObject& profile, AppConfigData& data, LearningData& learning)
{
    VeniceProfileImportResult result;
    if (profile.value(QLatin1String("kind")).toString() != QLatin1String(kVeniceProfileKind)) {
        result.error = QStringLiteral(
            "Not a Venice profile (missing kind=venice-profile) - nothing was changed.");
        return result;
    }
    const QJsonValue versionValue = profile.value(QLatin1String("profile_version"));
    if (!versionValue.isDouble() || versionValue.toInt() < 1) {
        result.error =
            QStringLiteral("Venice profile has no valid profile_version - nothing was changed.");
        return result;
    }
    if (versionValue.toInt() > kVeniceProfileVersion) {
        result.notes << QStringLiteral(
                            "profile_version %1 is newer than this build understands (%2); "
                            "unknown fields were ignored")
                            .arg(versionValue.toInt())
                            .arg(kVeniceProfileVersion);
    }

    // Work on copies so a mid-parse failure can never half-apply.
    AppConfigData next = data;
    LearningData nextLearning = learning;

    const auto takeBool = [&](const char* key, bool& target) {
        const QJsonValue v = profile.value(QLatin1String(key));
        if (v.isUndefined()) {
            return;
        }
        if (!v.isBool()) {
            result.notes << QStringLiteral("%1: not a boolean - rejected").arg(QLatin1String(key));
            return;
        }
        target = v.toBool();
        result.applied << QLatin1String(key);
    };

    // Clamp-into-band numeric (the load-path policy: honour the nearest allowed value,
    // report the clamp). Returns true when a value was installed.
    const auto takeClamped = [&](const char* key, double lo, double hi,
                                 const auto& install) -> bool {
        const QJsonValue v = profile.value(QLatin1String(key));
        if (v.isUndefined()) {
            return false;
        }
        if (!v.isDouble() || !std::isfinite(v.toDouble())) {
            result.notes << QStringLiteral("%1: not a finite number - rejected")
                                .arg(QLatin1String(key));
            return false;
        }
        const double raw = v.toDouble();
        const double clamped = std::clamp(raw, lo, hi);
        if (clamped != raw) {
            result.notes << QStringLiteral("%1: %2 out of range [%3, %4] - clamped to %5")
                                .arg(QLatin1String(key))
                                .arg(raw)
                                .arg(lo)
                                .arg(hi)
                                .arg(clamped);
        }
        install(clamped);
        result.applied << QLatin1String(key);
        return true;
    };

    const auto takeClampedMap = [&](const char* key, double lo, double hi,
                                    QMap<QString, double>& target) {
        const QJsonValue v = profile.value(QLatin1String(key));
        if (v.isUndefined()) {
            return;
        }
        if (!v.isObject()) {
            result.notes << QStringLiteral("%1: not an object - rejected").arg(QLatin1String(key));
            return;
        }
        const QJsonObject map = v.toObject();
        if (map.size() > kVeniceProfileMaxMapEntries) {
            result.notes << QStringLiteral("%1: %2 entries exceeds the %3-entry bound - rejected")
                                .arg(QLatin1String(key))
                                .arg(map.size())
                                .arg(kVeniceProfileMaxMapEntries);
            return;
        }
        bool any = false;
        for (auto it = map.constBegin(); it != map.constEnd(); ++it) {
            if (it.key().trimmed().isEmpty() || it.key().size() > kVeniceProfileMaxMapKeyLen) {
                result.notes << QStringLiteral("%1[%2]: invalid shot-type key - rejected")
                                    .arg(QLatin1String(key), it.key().left(48));
                continue;
            }
            if (!it.value().isDouble() || !std::isfinite(it.value().toDouble())) {
                result.notes << QStringLiteral("%1[%2]: not a finite number - rejected")
                                    .arg(QLatin1String(key), it.key());
                continue;
            }
            const double raw = it.value().toDouble();
            const double clamped = std::clamp(raw, lo, hi);
            if (clamped != raw) {
                result.notes << QStringLiteral("%1[%2]: %3 out of range [%4, %5] - clamped to %6")
                                    .arg(QLatin1String(key), it.key())
                                    .arg(raw)
                                    .arg(lo)
                                    .arg(hi)
                                    .arg(clamped);
            }
            target.insert(it.key(), clamped);
            any = true;
        }
        if (any) {
            result.applied << QLatin1String(key);
        }
    };

    // === Shot Lead. Accepted set is {0} u [min, max] (AppConfig load policy): a value
    // in (0, min) is clamped UP to min, above max down to max. A zero/absent lead means
    // "source install never configured one" and must NOT wipe a configured target lead.
    {
        const QJsonValue v = profile.value(QLatin1String("actuation_lead_ms"));
        if (!v.isUndefined()) {
            if (!v.isDouble() || !std::isfinite(v.toDouble())) {
                result.notes << QStringLiteral("actuation_lead_ms: not a finite number - rejected");
            } else if (v.toDouble() <= 0.0) {
                result.notes << QStringLiteral(
                    "actuation_lead_ms: 0 (not configured in the exported install) - skipped");
            } else {
                double lead = v.toDouble();
                if (lead < AppConfigData::kActuationLeadMinMs) {
                    result.notes << QStringLiteral(
                                        "actuation_lead_ms: %1 below %2 - clamped up")
                                        .arg(lead)
                                        .arg(AppConfigData::kActuationLeadMinMs);
                    lead = AppConfigData::kActuationLeadMinMs;
                } else if (lead > AppConfigData::kActuationLeadMaxMs) {
                    result.notes << QStringLiteral(
                                        "actuation_lead_ms: %1 above %2 - clamped down")
                                        .arg(lead)
                                        .arg(AppConfigData::kActuationLeadMaxMs);
                    lead = AppConfigData::kActuationLeadMaxMs;
                }
                next.actuationLeadMs = lead;
                // The restored lead must keep the "your value wins" promise on the new
                // install: default to user-set unless the profile says otherwise.
                next.actuationLeadUserSet =
                    profile.value(QLatin1String("actuation_lead_user_set")).toBool(true);
                result.applied << QStringLiteral("actuation_lead_ms");
            }
        }
    }

    // === Tip Timing (canonical). Clamp INTO the learner's plausibility band — the
    // same "nearest allowed aim" policy the Tip Timing card's own setter applies.
    takeClamped("tip_timing_ms", kVeniceProfileTipTimingMinMs, kVeniceProfileTipTimingMaxMs,
                [&](double v) { nextLearning.learnedPhasePhysicalMs = v; });
    takeBool("tip_timing_user_set", next.tipTimingUserSet);
    takeBool("tip_phase_aim_frozen", next.tipPhaseAimFrozen);

    // === Meter Delay ===
    takeBool("meter_delay_enabled", next.meterDelayEnabled);
    takeClamped("meter_delay_ms", static_cast<double>(AppConfigData::kMeterDelayMinMs),
                static_cast<double>(AppConfigData::kMeterDelayMaxMs),
                [&](double v) { next.meterDelayMs = static_cast<int>(std::lround(v)); });
    takeBool("meter_delay_bypass_on_defense", next.meterDelayBypassOnDefense);

    // === Other customer-tuned timing knobs ===
    takeClamped("no_dip_lead_ms", -kVeniceProfileNoDipLeadBandMs, kVeniceProfileNoDipLeadBandMs,
                [&](double v) { next.noDipLeadMs = v; });
    takeBool("press_anchored_predictor_enabled", next.pressAnchoredPredictorEnabled);
    takeClampedMap("press_anchored_tip_ms", kVeniceProfilePressTipMinMs,
                   kVeniceProfilePressTipMaxMs, next.pressAnchoredTipMs);
    takeClampedMap("press_anchored_tip_sigma_ms", kVeniceProfilePressSigmaMinMs,
                   kVeniceProfilePressSigmaMaxMs, next.pressAnchoredTipSigmaMs);
    takeClampedMap("press_anchored_tip_n", 0.0, kVeniceProfilePressNMax,
                   next.pressAnchoredTipN);
    takeClampedMap("shot_type_offsets", -kVeniceProfileShotTypeOffsetBandMs,
                   kVeniceProfileShotTypeOffsetBandMs, next.shotTypeOffsets);

    data = next;
    learning = nextLearning;
    result.ok = true;
    return result;
}

} // namespace orion
