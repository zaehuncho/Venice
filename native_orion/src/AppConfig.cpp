#include "AppConfig.h"
#include "OrionPaths.h"
#include "RemotePlayExecutablePolicy.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDebug>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonValue>
#include <QtCore/QRegularExpression>
#include <QtCore/QSaveFile>

#include <algorithm>
#include <cmath>

namespace orion {

namespace {

QJsonObject readObject(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    const auto doc = QJsonDocument::fromJson(file.readAll());
    return doc.isObject() ? doc.object() : QJsonObject{};
}

double perLevelNudge(const QJsonObject& perLevel, const QString& key)
{
    const auto level = perLevel.value(key).toObject();
    return level.value(QStringLiteral("nudge_pct")).toDouble(0.0);
}

QString defaultChiakiPath(const QString& rootDir)
{
#ifdef ORION_PRODUCTION_BUILD
    // Persist the intended install-relative location even when the package is
    // incomplete. Runtime resolution separately requires the file to exist and
    // fails closed; an old external absolute path must never survive as a
    // production fallback merely because that old file still exists.
    return remote_play_executable_policy::packagedOrionStreamPath(
        QCoreApplication::applicationDirPath());
#else
    const auto local = remote_play_executable_policy::select(
        QCoreApplication::applicationDirPath(), rootDir, QString(), false);
    if (!local.path.isEmpty()) {
        return local.path;
    }

    const QStringList candidates = {
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-orion/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-ng/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-ng/chiaki-ng-Win/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki/chiaki-ng.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/vendor/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/vendor/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Downloads/Chiaki/Chiaki/chiaki.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Downloads/Chiaki/chiaki.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/AppData/Local/chiaki-ng/chiaki-ng.exe")),
        QStringLiteral("C:/Program Files/chiaki-ng/chiaki-ng.exe"),
        QStringLiteral("C:/Program Files/Chiaki/chiaki.exe")
    };
    for (const auto& candidate : candidates) {
        if (QFileInfo::exists(candidate)) {
            return candidate;
        }
    }
    return {};
#endif
}

void normalizeRemotePlayBackend(AppConfigData& data, const QString& rootDir)
{
    const auto mode = data.remotePlayClientMode.trimmed().toLower();
    if (mode != QLatin1String("chiaki") && mode != QLatin1String("chiaki-ng")) {
        data.remotePlayClientMode = QStringLiteral("chiaki");
    } else {
        data.remotePlayClientMode = QStringLiteral("chiaki");
    }

    const auto title = data.remotePlayWindowTitle.trimmed().toLower();
    if (title.isEmpty() || title.contains(QStringLiteral("ps remote")) || title.contains(QStringLiteral("xbox"))) {
        data.remotePlayWindowTitle = QStringLiteral("Chiaki");
    }

    const auto detected = defaultChiakiPath(rootDir);
#ifdef ORION_PRODUCTION_BUILD
    // Always migrate the UI/config model to the bundled location. This is not
    // merely cosmetic: without this assignment an existing path into another
    // checkout remained valid forever and could be passed to a packaged
    // sidecar after upgrades.
    data.chiakiPath = detected;
#else
    // Development keeps explicit external experimentation only when there is
    // no packaged/repository-local client. A local OrionStream automatically
    // migrates stale cross-checkout settings to the active tree.
    const auto preferredLocal = remote_play_executable_policy::select(
        QCoreApplication::applicationDirPath(), rootDir, QString(), false);
    if (!preferredLocal.path.isEmpty()) {
        data.chiakiPath = preferredLocal.path;
    } else if (data.chiakiPath.trimmed().isEmpty() || !QFileInfo::exists(data.chiakiPath)) {
        if (!detected.isEmpty()) {
            data.chiakiPath = detected;
        }
    }
#endif

    // Square remains the safe default, while an explicit stick/both selection is
    // preserved. `both` is the production path that exposes the Square-triggered
    // Button/Tempo remap and the separately qualified TempoStick/GoTo gestures.
    data.remotePlayInputSource = normalizedRemotePlayInputSource(
        data.remotePlayInputSource);
}

} // namespace

AppConfig::AppConfig(QString rootDir, QObject* parent)
    : QObject(parent),
      rootDir_(std::move(rootDir))
{
    // Production is meter-authority-only: repair both legacy
    // `no_meter_enabled=true` and contradictory dual-true files at the trusted load
    // boundary. Pose-only timing remains compile-time-gated lab code in the engine;
    // it is never loadable from a shipped settings file.
    data_.meterEnabled = true;
    data_.noMeterEnabled = false;
    normalizeRemotePlayBackend(data_, rootDir_);
}

QString AppConfig::settingsPath() const
{
    // [ORION_DATA_DIR] Mutable state lives in the per-user data dir, NOT beside the executable.
    // See OrionPaths.h: in a production install the exe sits in Program Files, which a normal
    // user cannot write, so persisting settings there silently failed and the app came up
    // unconfigured on its second launch. Dev builds resolve this to rootDir_ unchanged.
    return orionDataDir(rootDir_) + QStringLiteral("/settings.json");
}

QString AppConfig::profileLearningSlug(const QString& profileName)
{
    QString slug;
    for (const QChar c : profileName.trimmed().toLower()) {
        if (c.isLetterOrNumber()) {
            slug.append(c);
        } else if (!slug.isEmpty() && !slug.endsWith(QLatin1Char('-'))) {
            slug.append(QLatin1Char('-'));
        }
    }
    while (slug.endsWith(QLatin1Char('-'))) {
        slug.chop(1);
    }
    return slug.left(48);
}

QString AppConfig::learningPath() const
{
    // [ORION_DATA_DIR] Learning is per-user MEASURED state (this machine's latency), so it
    // belongs with settings in the writable data dir rather than beside the executable.
    const QString dataDir = orionDataDir(rootDir_);
    const QString name = data_.activeProfile.trimmed();
    if (name.isEmpty() || name.compare(QStringLiteral("Default"), Qt::CaseInsensitive) == 0) {
        return dataDir + QStringLiteral("/learning.json");
    }
    const QString slug = profileLearningSlug(name);
    if (slug.isEmpty()) {
        return dataDir + QStringLiteral("/learning.json");
    }
    return dataDir + QStringLiteral("/learning.") + slug + QStringLiteral(".json");
}

void AppConfig::reloadLearning()
{
    learning_ = LearningData{};
    const auto learningJson = readObject(learningPath());
    if (!learningJson.isEmpty()) {
        loadLearningObject(learningJson);
    }
}

const QList<AppConfig::SettingsMigration>& AppConfig::settingsMigrations()
{
    // THE ALLOWLIST. One entry = one key = one deliberate, evidenced decision.
    // Keep entries in ascending targetVersion order; never renumber or edit a
    // shipped entry (idempotency across installs depends on the history being
    // append-only).
    //
    // CURRENTLY EMPTY, ON PURPOSE. v1 exists to install the settings_version
    // stamp itself; the infrastructure is the deliverable. The one candidate
    // rule was drafted, evidenced, and then PULLED the same day -- history:
    //
    // [PULLED 2026-08-08] v1 phase_veto_directional OFF -> ON.
    //   WHAT IT WOULD HAVE DONE: flip the one-directional live-meter veto ON for
    //   every pre-versioning install whose file still carried the old default
    //   false ({1, "phase_veto_directional", false, true, no companion}).
    //   THE EVIDENCE FOR IT (live, real meter, 2026-08-04..06 batches): the
    //   symmetric corroboration veto let the sampler -- running its own measured
    //   +61-92ms EARLY bias -- demote a healthy dated phase member; measured cost
    //   16 deadline-missed aborts + 3 counted earlies. See the
    //   [ORION_PHASE_VETO_DIRECTIONAL] block in AutomationEngine.cpp.
    //   WHY IT WAS PULLED: with the flag ON, 12 autonomous tip-decision pins in
    //   AutomationEngineTests fail under the then-current engine -- shots stuck
    //   at waiting_for_live_tip_deadline with deadline=-1, never releasing at
    //   fill 99.75, plus the tempo/goto LeavesButtonShotBitIdentical contracts.
    //   Attribution was established by an A/B/A rebuild on the flag alone (ON:
    //   the same 12 fail; OFF: all pass; ON again: the same 12 fail) while
    //   unrelated tests moved between builds. Two readings fit: (a) stale
    //   fixtures -- the synthetic meters rise to ~99% in ~270ms against a
    //   ~380-450ms physical phase constant, impossible on the real meter -- or
    //   (b) a genuine no-release interaction with the leased-deadline machinery.
    //   (b) is the identical no-fire class this flag exists to REMOVE, and the
    //   risk is asymmetric: OFF costs the bounded, measured 16+3; ON risks
    //   unbounded no-release. Coordinator decision 2026-08-08: pull the rule.
    //   RE-ENABLE CONDITION (both required, in order): the AutomationEngine
    //   owner adjudicates the 12 pins (fixture-vs-engine), THEN a counted live
    //   batch confirms flag-ON behaviour. Re-add the rule under a NEW version
    //   (kSettingsVersion+1), never back into v1 -- v1 has already been stamped
    //   onto installs as "migrates nothing".
    //
    // ALSO DELIBERATELY NOT MIGRATED (2026-08-08). Flags that change live
    // release timing ship only behind a counted live batch (project standing
    // rule), never silently via a migration:
    //   * ownership_proof_two_frame  -- ~14ms median saving measured offline, but
    //     never confirmed by a counted live batch, and it must not be paired with
    //     a 4.0pp anchor_rise_min_pct (that pairing saves 0ms).
    //   * tip_phase_anchor_base20    -- requires the coordinated ~58.3ms constant
    //     shift across four coupled terms; flipping the flag by itself via a
    //     migration would be actively wrong.
    //   * stop_dating_subframe, goto_tip_parity, stop_reopen_corroborate -- same
    //     class: no live validation yet.
    //   * user_lead_satisfies_authority -- default flipped ON in code 2026-08-07
    //     but entangled with an in-flight readiness fix; consequence-limited.
    //   * latency_probe_count (default 16 -> 24, 2026-08-07) -- numeric knob with
    //     no *_user_set companion; 16 on disk is indistinguishable from a user's
    //     choice, and 16 is slower convergence, not a correctness bug.
    // Any of these can be flipped by a FUTURE registry entry (new version) once
    // its counted batch lands; the mechanism existing is the point.
    static const QList<SettingsMigration> registry = {};
    return registry;
}

int AppConfig::settingsVersionOf(const QJsonObject& obj)
{
    const QJsonValue value = obj.value(QStringLiteral("settings_version"));
    if (!value.isDouble()) {
        return 0;   // absent or malformed (string/bool/null/object) -> pre-versioning
    }
    const double raw = value.toDouble();
    if (!std::isfinite(raw) || raw < 0.0) {
        return 0;   // corrupt stamp degrades to "run the allowlist again", never to a wipe
    }
    // Truncate, don't round: a fractional stamp never *reached* the next version,
    // and re-running a migration is safe (idempotent) while skipping one is not.
    return static_cast<int>(std::min(raw, 1000000.0));
}

QStringList AppConfig::applySettingsMigrations(QJsonObject& obj, int fromVersion,
                                               const QList<SettingsMigration>& rules)
{
    QStringList applied;
    for (const auto& rule : rules) {
        if (rule.targetVersion <= fromVersion || rule.targetVersion > kSettingsVersion) {
            continue;   // step already ran on an earlier launch / not shipped yet
        }
        if (!rule.userSetCompanion.isEmpty()) {
            const QJsonValue companion = obj.value(rule.userSetCompanion);
            // A user-set companion vetoes ABSOLUTELY -- including when the value
            // still equals the old default (an explicit choice OF the default is
            // still a choice). Present-but-malformed counts as user-set: when the
            // evidence is unreadable, the conservative reading wins.
            if (!companion.isUndefined() && (!companion.isBool() || companion.toBool(true))) {
                continue;
            }
        }
        const QJsonValue current = obj.value(rule.key);
        // Only a key that is absent or still sitting at the OLD default migrates.
        // Any other value -- hand-edited, A/B'd, or from another build lineage --
        // is evidence somebody moved it, and the allowlist never overrides that.
        if (!current.isUndefined() && current != rule.oldDefault) {
            continue;
        }
        obj.insert(rule.key, rule.newValue);
        applied << rule.key;
    }
    return applied;
}

bool AppConfig::load()
{
    const auto settings = readObject(settingsPath());
    bool settingsMigrated = false;
    if (!settings.isEmpty()) {
        QJsonObject effective = settings;
        const int onDiskVersion = settingsVersionOf(settings);
        if (onDiskVersion < kSettingsVersion) {
            // Pre-versioning (or older-versioned) file: run the explicit allowlist
            // on the raw JSON before parsing, then persist ONCE below so the stamp
            // advances and this never runs again for this install. Auditable: one
            // log line per key actually rewritten.
            const QStringList applied =
                applySettingsMigrations(effective, onDiskVersion, settingsMigrations());
            settingsMigrated = true;
            qInfo().noquote()
                << QStringLiteral("[AppConfig] settings_version %1 -> %2 (%3 key(s) migrated)")
                       .arg(onDiskVersion)
                       .arg(kSettingsVersion)
                       .arg(applied.size());
            for (const auto& rule : settingsMigrations()) {
                if (applied.contains(rule.key)) {
                    qInfo().noquote()
                        << QStringLiteral("[AppConfig] settings migration v%1 applied: %2 -- %3")
                               .arg(rule.targetVersion)
                               .arg(rule.key, rule.rationale);
                }
            }
        }
        loadSettingsObject(effective);
    }

    const auto learningJson = readObject(learningPath());
    if (!learningJson.isEmpty()) {
        loadLearningObject(learningJson);
    }

    // One-time migration: the global early_late_offset_ms knob is retired. Fold any persisted
    // value into the per-type learned offsets, then zero AND persist BOTH files in the same
    // pass -- if only learning.json were written, the next launch would fold it again.
    bool settingsPersisted = false;
    if (data_.earlyLateOffsetMs != 0.0) {
        const double fold = std::clamp(data_.earlyLateOffsetMs, -120.0, 120.0);
        for (auto it = data_.shotTypeOffsets.constBegin(); it != data_.shotTypeOffsets.constEnd(); ++it) {
            const double merged = learning_.shotTypeLearnedOffsetMs.value(it.key(), 0.0) + fold;
            learning_.shotTypeLearnedOffsetMs.insert(it.key(), std::clamp(merged, -120.0, 120.0));
        }
        data_.earlyLateOffsetMs = 0.0;
        saveLearning(learning_);
        save(data_);
        settingsPersisted = true;
    }

    // Persist the migrated settings + the advanced settings_version stamp exactly once
    // (save() stamps kSettingsVersion; the fold save above already carried it). A failed
    // save is non-fatal by design: the in-memory config is already migrated, and the
    // next launch simply retries -- a read-only or full disk must never block startup.
    if (settingsMigrated && !settingsPersisted) {
        save(data_);
    }

    emit configChanged(data_);
    return !settings.isEmpty();
}

bool AppConfig::save(const AppConfigData& requested, QString* error)
{
    AppConfigData data = requested;
    data.meterEnabled = true;
    data.noMeterEnabled = false;
    normalizeRemotePlayBackend(data, rootDir_);
    QJsonObject obj;
    // [ORION_SETTINGS_VERSION] Always stamp the CURRENT schema generation: this file is
    // being written by this build, so its defaults ARE this build's defaults. load()
    // compares this stamp against kSettingsVersion to run the settingsMigrations()
    // allowlist exactly once per install per version step (see AppConfig.h).
    obj.insert(QStringLiteral("settings_version"), kSettingsVersion);
    obj.insert(QStringLiteral("launcher_theme"), data.launcherTheme);
    obj.insert(QStringLiteral("launcher_custom_accent"), data.customAccent);
    obj.insert(QStringLiteral("update_channel"), data.updateChannel);
    obj.insert(QStringLiteral("defense_trigger_button"), data.defenseTriggerButton);
    obj.insert(QStringLiteral("defense_stick_assist"), data.defenseStickAssist);
    obj.insert(QStringLiteral("defense_stick_assist_strength"), data.defenseStickAssistStrength);
    obj.insert(QStringLiteral("defense_l2_hold_assist"), data.defenseL2HoldAssist);
    obj.insert(QStringLiteral("defense_sprint_assist"), data.defenseSprintAssist);
    obj.insert(QStringLiteral("defense_lightbar_color"), data.defenseLightbarColor);
    obj.insert(QStringLiteral("active_profile"), data.activeProfile);
    obj.insert(QStringLiteral("profiles"), profiles_);
    obj.insert(QStringLiteral("meter_color"), data.meterColor);
    obj.insert(QStringLiteral("meter_style"), data.meterStyle);
    obj.insert(QStringLiteral("remote_play_client_mode"), QStringLiteral("chiaki"));
    obj.insert(QStringLiteral("remote_play_console"), data.remotePlayConsole);
    obj.insert(QStringLiteral("stream_setup_complete"), data.streamSetupComplete);
    obj.insert(QStringLiteral("preflight_complete"), data.preflightComplete);
    obj.insert(QStringLiteral("legal_accepted_version"), data.legalAcceptedVersion);
    obj.insert(QStringLiteral("video_source"), data.videoSource);
    obj.insert(QStringLiteral("capture_card_index"), data.captureCardIndex);
    obj.insert(QStringLiteral("hardware_decode"), data.hardwareDecode);
    obj.insert(QStringLiteral("controller_type"), data.controllerType);
    obj.insert(QStringLiteral("auto_reconnect"), data.autoReconnect);
    obj.insert(QStringLiteral("remote_play_window_title"), data.remotePlayWindowTitle.trimmed().isEmpty() ? QStringLiteral("Chiaki") : data.remotePlayWindowTitle);
    obj.insert(QStringLiteral("chiaki_path"), data.chiakiPath);
    obj.insert(QStringLiteral("remote_play_console_ip"), data.remotePlayConsoleIp);
    obj.insert(QStringLiteral("remote_play_profile"), data.remotePlayProfile);
    obj.insert(QStringLiteral("remote_play_input_source"), data.remotePlayInputSource);
    obj.insert(QStringLiteral("stream_bandwidth_mode"), data.streamBandwidthMode);
    obj.insert(QStringLiteral("stream_render_backend"), data.streamRenderBackend);
    obj.insert(QStringLiteral("stream_audio_enabled"), data.streamAudioEnabled);
    obj.insert(QStringLiteral("stream_audio_mode"), data.streamAudioEnabled ? data.streamAudioMode : QStringLiteral("Off"));
    obj.insert(QStringLiteral("controller_lightbar_enabled"), data.controllerLightbarEnabled);
    obj.insert(QStringLiteral("controller_lightbar_color"), data.controllerLightbarColor);
    obj.insert(QStringLiteral("controller_lightbar_mode"), data.controllerLightbarMode);
    obj.insert(QStringLiteral("controller_lightbar_primary_color"), data.controllerLightbarPrimaryColor);
    obj.insert(QStringLiteral("controller_lightbar_secondary_color"), data.controllerLightbarSecondaryColor);
    obj.insert(QStringLiteral("controller_lightbar_brightness"), data.controllerLightbarBrightness);
    obj.insert(QStringLiteral("controller_lightbar_effect_speed"), data.controllerLightbarEffectSpeed);
    obj.insert(QStringLiteral("meter_overlay_color"), data.meterOverlayColor);
    obj.insert(QStringLiteral("meter_overlay_style"), data.meterOverlayStyle);
    obj.insert(QStringLiteral("meter_overlay_rgb"), data.meterOverlayRgb);
    obj.insert(QStringLiteral("orion_sync_mode"), data.rttSyncMode);
    obj.insert(QStringLiteral("manual_sync_adjust_ms"), data.manualSyncAdjustMs);
    obj.insert(QStringLiteral("manual_offset_ms"), data.manualOffsetMs);
    obj.insert(QStringLiteral("hold_square_input_source"), data.remotePlayInputSource);
    obj.insert(QStringLiteral("tempo_input_mode"), data.remotePlayInputSource);
    obj.insert(QStringLiteral("green_window_target_mode"), data.greenWindowTargetMode);
    obj.insert(QStringLiteral("feedforward_anchor"), data.feedforwardAnchor);
    obj.insert(QStringLiteral("anchor_max_first_fill_pct"), data.anchorMaxFirstFillPct);
    obj.insert(QStringLiteral("anchor_rise_min_pct"), data.anchorRiseMinPct);
    obj.insert(QStringLiteral("ownership_proof_two_frame"), data.ownershipProofTwoFrame);
    obj.insert(QStringLiteral("tip_phase_anchor_base20"), data.tipPhaseAnchorBase20);
    obj.insert(QStringLiteral("tip_phase_type_trim_enabled"), data.tipPhaseTypeTrimEnabled);
    {
        QJsonObject trim;
        for (auto it = data.tipPhaseTypeTrimMs.constBegin();
             it != data.tipPhaseTypeTrimMs.constEnd(); ++it) {
            trim.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("tip_phase_type_trim"), trim);
    }
    obj.insert(QStringLiteral("stop_reopen_corroborate"), data.stopReopenCorroborate);
    obj.insert(QStringLiteral("stop_dating_subframe"), data.stopDatingSubframe);
    obj.insert(QStringLiteral("phase_veto_directional"), data.phaseVetoDirectional);
    obj.insert(QStringLiteral("tip_phase_aim_frozen"), data.tipPhaseAimFrozen);
    obj.insert(QStringLiteral("tip_phase_rung_imminent_hold"), data.tipPhaseRungImminentHold);
    obj.insert(QStringLiteral("tempo_tip_parity"), data.tempoTipParity);
    obj.insert(QStringLiteral("goto_tip_parity"), data.gotoTipParity);
    obj.insert(QStringLiteral("user_lead_satisfies_authority"), data.userLeadSatisfiesAuthority);
    obj.insert(QStringLiteral("tempo_fade_mirror_gesture"), data.tempoFadeMirrorGesture);
    obj.insert(QStringLiteral("probe_cache_prior_authority"), data.probeCachePriorAuthority);
    obj.insert(QStringLiteral("latency_probe_count"), data.latencyProbeCount);
    obj.insert(QStringLiteral("tip_gate_enabled"), data.tipGateEnabled);
    obj.insert(QStringLiteral("tip_gate_cap_ms"), data.tipGateCapMs);
    obj.insert(QStringLiteral("memory_trust_enabled"), data.memoryTrustEnabled);
    obj.insert(QStringLiteral("autonomous_vision"), data.autonomousVision);
    obj.insert(QStringLiteral("autonomous_vision_shadow"), data.autonomousVisionShadow);
    // Tip-timing improvement flags (2026-07 W-series; default OFF = current behavior).
    obj.insert(QStringLiteral("measured_lead"), data.measuredLeadEnabled);
    obj.insert(QStringLiteral("tick_lock"), data.tickLockEnabled);
    obj.insert(QStringLiteral("reg_fusion"), data.regFusionEnabled);
    obj.insert(QStringLiteral("lead_learner_vision_gate"), data.leadLearnerVisionGate);
    obj.insert(QStringLiteral("fused_fire"), data.fusedFireEnabled);
    obj.insert(QStringLiteral("fused_shadow"), data.fusedShadowEnabled);
    obj.insert(QStringLiteral("grade_v2"), data.gradeV2Enabled);
    // Ceiling stack flags (2026-07 perfect-green build; default OFF = current behavior).
    obj.insert(QStringLiteral("plateau_aim"), data.plateauAimEnabled);
    obj.insert(QStringLiteral("template_arrival"), data.templateArrivalEnabled);
    obj.insert(QStringLiteral("press_t0"), data.pressT0Enabled);
    obj.insert(QStringLiteral("bandit_lead"), data.banditLeadEnabled);
    obj.insert(QStringLiteral("memory_trust_max_age_ms"), data.memoryTrustMaxAgeMs);
    obj.insert(QStringLiteral("memory_trust_fill_slack_pct"), data.memoryTrustFillSlackPct);
    obj.insert(QStringLiteral("memory_trust_max_fill_pct"), data.memoryTrustMaxFillPct);
    obj.insert(QStringLiteral("green_window_priority"), true);
    obj.insert(QStringLiteral("meter_enabled"), data.meterEnabled);
    // [ORION_METER_DELAY 2026-08-07] Persist meter-delay actuator config.
    obj.insert(QStringLiteral("meter_delay_enabled"), data.meterDelayEnabled);
    obj.insert(QStringLiteral("meter_delay_ms"), data.meterDelayMs);
    obj.insert(QStringLiteral("meter_delay_bypass_on_defense"), data.meterDelayBypassOnDefense);
    obj.insert(QStringLiteral("freeze_calibration"), data.freezeCalibration);
    obj.insert(QStringLiteral("banner_calibration"), data.bannerCalibration);
    obj.insert(QStringLiteral("tempo_mode_enabled"), data.tempoEnabled);
    obj.insert(QStringLiteral("tempo_remap_enabled"), data.tempoRemapEnabled);
    obj.insert(QStringLiteral("tempo_flick_enabled"), data.tempoFlickEnabled);
    obj.insert(QStringLiteral("tempo_remap_type"), data.tempoRemapType);
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] #88
    obj.insert(QStringLiteral("square_passthrough_enabled"), data.squarePassthroughEnabled);
    obj.insert(QStringLiteral("square_passthrough_button"), data.squarePassthroughButton);
    obj.insert(QStringLiteral("no_dip_enabled"), data.noDipEnabled);
    obj.insert(QStringLiteral("no_dip_lead_ms"), data.noDipLeadMs);
    // [ORION_USER_LEAD] the user-facing Shot Lead + whether the USER (not the measurement) set it.
    obj.insert(QStringLiteral("actuation_lead_ms"), data.actuationLeadMs);
    obj.insert(QStringLiteral("actuation_lead_user_set"), data.actuationLeadUserSet);
    // [ORION_METER_DELAY_LEAD_KEYING] see AppConfigData::meterDelayLeadOffsetMs.
    obj.insert(QStringLiteral("meter_delay_lead_offset_ms"), data.meterDelayLeadOffsetMs);
    // [ORION_USER_TIP] whether the USER explicitly chose the tip-timing value. The value
    // itself lives in learning.json (canonical base-30 physical, the learner's own slot);
    // this flag is what distinguishes "user typed this" from "learner found this".
    obj.insert(QStringLiteral("tip_timing_user_set"), data.tipTimingUserSet);
    // [ORION_PRESS_ANCHOR] Phase-B predictor arm (default OFF) + the per-type learned
    // press->real-tip calibration the Phase-A collector writes back on each accepted landing.
    obj.insert(QStringLiteral("press_anchored_predictor_enabled"),
               data.pressAnchoredPredictorEnabled);
    obj.insert(QStringLiteral("press_anchored_predictor_min_samples"),
               data.pressAnchoredPredictorMinSamples);
    {
        QJsonObject pa;
        for (auto it = data.pressAnchoredTipMs.constBegin();
             it != data.pressAnchoredTipMs.constEnd(); ++it) {
            pa.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("press_anchored_tip_ms"), pa);
    }
    {
        QJsonObject ps;
        for (auto it = data.pressAnchoredTipSigmaMs.constBegin();
             it != data.pressAnchoredTipSigmaMs.constEnd(); ++it) {
            ps.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("press_anchored_tip_sigma_ms"), ps);
    }
    {
        QJsonObject pn;
        for (auto it = data.pressAnchoredTipN.constBegin();
             it != data.pressAnchoredTipN.constEnd(); ++it) {
            pn.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("press_anchored_tip_n"), pn);
    }
    // [ORION_PROBE] 0 = use the engine default; any value in band overrides it.
    obj.insert(QStringLiteral("probe_press_ms"), data.probePressMs);
    obj.insert(QStringLiteral("probe_gap_ms"), data.probeGapMs);
    obj.insert(QStringLiteral("probe_call_lead_ms"), data.probeCallLeadMs);
    // [ORION_GREEN_CENTER] 0 = aim at the green window's late edge (previous behaviour).
    obj.insert(QStringLiteral("autonomous_green_center_frac"), data.autonomousGreenCenterFrac);
    obj.insert(QStringLiteral("autonomous_green_center_max_ms"), data.autonomousGreenCenterMaxMs);
    obj.insert(QStringLiteral("no_meter_enabled"), data.noMeterEnabled);
    obj.insert(QStringLiteral("no_meter_release_point"), data.noMeterReleasePoint);
    obj.insert(QStringLiteral("no_meter_base_offset_ms"), data.noMeterBaseOffsetMs);
    obj.insert(QStringLiteral("no_meter_decode_comp_ms"), data.noMeterDecodeCompMs);
    obj.insert(QStringLiteral("no_meter_confidence_gate"), data.noMeterConfidenceGate);
    obj.insert(QStringLiteral("no_meter_push_release_window_ms"), data.noMeterPushReleaseWindowMs);
    obj.insert(QStringLiteral("no_meter_handedness"), data.noMeterHandedness);
    obj.insert(QStringLiteral("active_shot_type"), data.activeShotType);
    {
        QJsonObject sto;
        for (auto it = data.shotTypeOffsets.constBegin(); it != data.shotTypeOffsets.constEnd(); ++it) {
            sto.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_offsets"), sto);
    }
    {
        QJsonObject mo;
        for (auto it = data.shotTypeModeOverride.constBegin(); it != data.shotTypeModeOverride.constEnd(); ++it) {
            mo.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_mode_override"), mo);
    }
    {
        QJsonObject fh;
        for (auto it = data.shotTypeFlickHoldMs.constBegin(); it != data.shotTypeFlickHoldMs.constEnd(); ++it) {
            fh.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_flick_hold_ms"), fh);
    }
    obj.insert(QStringLiteral("tempo_open_loop_primary"), data.tempoOpenLoopPrimary);
    obj.insert(QStringLiteral("ff_reachability_slack_pct"), data.ffReachabilitySlackPct);
    obj.insert(QStringLiteral("ls_cancel_threshold_pct"), data.lsCancelThresholdPct);
    obj.insert(QStringLiteral("ls_cancel_frames"), data.lsCancelFrames);
    obj.insert(QStringLiteral("calibration_mode"), data.calibrationMode);
    obj.insert(QStringLiteral("turbo_fade_enabled"), false);
    obj.insert(QStringLiteral("dunk_stick"), false);
    obj.insert(QStringLiteral("network_enabled"), data.networkEnabled);
    // [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in key is DELETED: it is
    // neither written here nor read on load. Everything
    // network-side rides network_enabled / meter_delay_enabled, both default ON.
    obj.insert(QStringLiteral("cuda_enabled"), data.cudaEnabled);
    obj.insert(QStringLiteral("latency_compensation_ms"), data.latencyCompensationMs);
    obj.insert(QStringLiteral("release_threshold"), data.releaseThresholdPct);
    obj.insert(QStringLiteral("green_threshold"), data.releaseThresholdPct);
    obj.insert(QStringLiteral("target_ms"), data.releaseThresholdPct);
    obj.insert(QStringLiteral("meter_timing"), data.releaseThresholdPct);
    obj.insert(QStringLiteral("fixed_hold_time_ms"), data.fixedHoldMs);
    obj.insert(QStringLiteral("early_late_offset_ms"), data.earlyLateOffsetMs);
    obj.insert(QStringLiteral("tempo_wait_ms"), data.tempoWaitMs);
    obj.insert(QStringLiteral("tempo_flick_hold_ms"), data.tempoFlickHoldMs);
    obj.insert(QStringLiteral("tempo_min_stick_hold_ms"), data.tempoMinStickHoldMs);
    obj.insert(QStringLiteral("tempo_fallback_timeout_ms"), data.tempoFallbackTimeoutMs);
    obj.insert(QStringLiteral("minimum_hold_time_ms"), data.minimumHoldMs);
    obj.insert(QStringLiteral("maximum_hold_time_ms"), data.maximumHoldMs);
    obj.insert(QStringLiteral("hold_stable_frames"), data.stableFrames);
    obj.insert(QStringLiteral("hold_release_strategy"), QStringLiteral("predictive"));
    obj.insert(QStringLiteral("vision_mode"), QStringLiteral("meter"));
    obj.insert(QStringLiteral("detection_confidence_percent"), data.detectionConfidencePercent);
    obj.insert(QStringLiteral("auto_meter_color"), data.autoMeterColor);
    obj.insert(QStringLiteral("show_live_meter_metrics"), data.showLiveMeterMetrics);

    // Trained template anchor + green (written verbatim; consumed by meter_detector.py).
    if (!data.meterTemplateAnchor.isEmpty()) {
        obj.insert(QStringLiteral("meter_template_anchor"), data.meterTemplateAnchor);
    }
    if (data.meterTrainedGreenHsvLow.size() == 3 && data.meterTrainedGreenHsvHigh.size() == 3) {
        obj.insert(QStringLiteral("meter_trained_green_hsv_low"), data.meterTrainedGreenHsvLow);
        obj.insert(QStringLiteral("meter_trained_green_hsv_high"), data.meterTrainedGreenHsvHigh);
    }

    QJsonObject latency;
    latency.insert(QStringLiteral("total_latency_ms"), data.latencyCompensationMs);
    obj.insert(QStringLiteral("latency"), latency);

    QJsonObject tempo;
    tempo.insert(QStringLiteral("enabled"), data.tempoEnabled);
    tempo.insert(QStringLiteral("remap_enabled"), data.tempoRemapEnabled);
    tempo.insert(QStringLiteral("flick_enabled"), data.tempoFlickEnabled);
    tempo.insert(QStringLiteral("input_source"), data.remotePlayInputSource);
    tempo.insert(QStringLiteral("input_mode"), data.remotePlayInputSource);
    tempo.insert(QStringLiteral("wait_ms"), data.tempoWaitMs);
    tempo.insert(QStringLiteral("flick_hold_ms"), data.tempoFlickHoldMs);
    tempo.insert(QStringLiteral("min_stick_hold_ms"), data.tempoMinStickHoldMs);
    tempo.insert(QStringLiteral("fallback_timeout_ms"), data.tempoFallbackTimeoutMs);
    obj.insert(QStringLiteral("tempo"), tempo);

    QJsonObject prediction;
    prediction.insert(QStringLiteral("green_window_start_pct"), data.releaseThresholdPct);
    prediction.insert(QStringLiteral("green_window_target_mode"), data.greenWindowTargetMode);
    obj.insert(QStringLiteral("prediction"), prediction);

    QJsonObject noMeter;
    noMeter.insert(QStringLiteral("enabled"), data.noMeterEnabled);
    noMeter.insert(QStringLiteral("release_point"), data.noMeterReleasePoint);
    noMeter.insert(QStringLiteral("base_offset_ms"), data.noMeterBaseOffsetMs);
    noMeter.insert(QStringLiteral("decode_comp_ms"), data.noMeterDecodeCompMs);
    noMeter.insert(QStringLiteral("confidence_gate"), data.noMeterConfidenceGate);
    noMeter.insert(QStringLiteral("push_release_window_ms"), data.noMeterPushReleaseWindowMs);
    noMeter.insert(QStringLiteral("handedness"), data.noMeterHandedness);
    obj.insert(QStringLiteral("no_meter"), noMeter);

    QJsonObject rttSync;
    rttSync.insert(QStringLiteral("ema_alpha"), 0.12);
    rttSync.insert(QStringLiteral("kalman_q"), 0.3);
    rttSync.insert(QStringLiteral("kalman_r"), 1.5);
    rttSync.insert(QStringLiteral("outlier_sigma"), 2.5);
    rttSync.insert(QStringLiteral("jitter_margin_ms"), 0.0);
    rttSync.insert(QStringLiteral("court_profiles"), true);
    rttSync.insert(QStringLiteral("tick_phase_lock"), true);
    rttSync.insert(QStringLiteral("decode_latency_aware"), true);
    rttSync.insert(QStringLiteral("decode_latency_ms"), data.noMeterDecodeCompMs);
    rttSync.insert(QStringLiteral("adaptive_jitter"), true);
    obj.insert(QStringLiteral("rtt_sync"), rttSync);

    QSaveFile file(settingsPath());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    file.write(QJsonDocument(obj).toJson(QJsonDocument::Indented));
    if (!file.commit()) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }

    data_ = data;
    emit configChanged(data_);
    return true;
}

bool AppConfig::saveLearning(const LearningData& data, QString* error)
{
    QJsonObject obj;
    // v3 marks the additive mode-keyed timing buckets ("<type>|tempo" keys live inside the existing
    // per-type maps — no schema change, so load stays backward-compatible with v2 files).
    obj.insert(QStringLiteral("version"), data.version >= 3 ? data.version : 3);
    obj.insert(QStringLiteral("ema_fill_per_frame"), data.emaFillPerFrame);
    obj.insert(QStringLiteral("ema_green_ratio"), data.emaGreenRatio);
    obj.insert(QStringLiteral("bias_pct"), data.biasPct);
    {
        QJsonObject perLevel;
        auto addNudge = [&perLevel](const QString& key, double value) {
            QJsonObject n;
            n.insert(QStringLiteral("nudge_pct"), value);
            perLevel.insert(key, n);
        };
        addNudge(QStringLiteral("OPEN"), data.openNudgePct);
        addNudge(QStringLiteral("LIGHT"), data.lightNudgePct);
        addNudge(QStringLiteral("MODERATE"), data.moderateNudgePct);
        addNudge(QStringLiteral("HEAVY"), data.heavyNudgePct);
        addNudge(QStringLiteral("SMOTHERED"), data.smotheredNudgePct);
        obj.insert(QStringLiteral("per_level"), perLevel);
    }
    {
        QJsonObject sto;
        for (auto it = data.shotTypeLearnedOffsetMs.constBegin(); it != data.shotTypeLearnedOffsetMs.constEnd(); ++it) {
            sto.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_learned_offset_ms"), sto);
    }
    {
        QJsonObject ff;
        for (auto it = data.shotTypeFeedforwardMs.constBegin(); it != data.shotTypeFeedforwardMs.constEnd(); ++it) {
            ff.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_feedforward_ms"), ff);
    }
    {
        QJsonObject mc;
        for (auto it = data.shotTypeMeterToReleaseMs.constBegin(); it != data.shotTypeMeterToReleaseMs.constEnd(); ++it) {
            mc.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_meter_to_release_ms"), mc);
    }
    {
        QJsonObject at;
        for (auto it = data.shotTypeAppearToTipMs.constBegin(); it != data.shotTypeAppearToTipMs.constEnd(); ++it) {
            at.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_appear_to_tip_ms"), at);
    }
    obj.insert(QStringLiteral("lead_rebaselined"), data.leadRebaselined);
    {
        QJsonObject ph;
        for (auto it = data.shotTypeCalPhase.constBegin(); it != data.shotTypeCalPhase.constEnd(); ++it) {
            ph.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_cal_phase"), ph);
    }
    // [ORION_PHASE_COLD_START] see LearningData::learnedPhasePhysicalMs
    if (data.learnedPhasePhysicalMs > 0.0) {
        obj.insert(QStringLiteral("learned_phase_physical_ms"), data.learnedPhasePhysicalMs);
    }
    // [ORION_AIM_FREEZE] see LearningData::measuredPhasePhysicalMs -- the instrument's own
    // full-window median, kept even while the Tip Timing card's manual value owns the slot above.
    if (data.measuredPhasePhysicalMs > 0.0) {
        obj.insert(QStringLiteral("measured_phase_physical_ms"), data.measuredPhasePhysicalMs);
    }
    {
        QJsonObject rb;
        for (auto it = data.shotTypeRttBaselineMs.constBegin(); it != data.shotTypeRttBaselineMs.constEnd(); ++it) {
            rb.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_rtt_baseline_ms"), rb);
    }
    {
        QJsonObject vp;
        for (auto it = data.shotTypeVelocityPriorPctMs.constBegin(); it != data.shotTypeVelocityPriorPctMs.constEnd(); ++it) {
            vp.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_velocity_prior_pct_ms"), vp);
    }
    // Hybrid global phase-clock self-learned globals (autonomous_vision path).
    obj.insert(QStringLiteral("global_appear_to_tip_ms"), data.globalAppearToTipMs);
    obj.insert(QStringLiteral("global_hold_to_release_ms"), data.globalHoldToReleaseMs);
    obj.insert(QStringLiteral("learned_latency_ms"), data.learnedLatencyMs);
    obj.insert(QStringLiteral("probe_spawn_offset_ms"), data.probeSpawnOffsetMs);
    obj.insert(QStringLiteral("global_rise_velocity_pct_ms"), data.globalRiseVelocityPctMs);
    // Per-shot-type latency residual (autonomous vision)
    {
        QJsonObject tl;
        for (auto it = data.shotTypeLatencyMs.constBegin(); it != data.shotTypeLatencyMs.constEnd(); ++it) {
            tl.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_latency_ms"), tl);
    }
    QSaveFile file(learningPath());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    file.write(QJsonDocument(obj).toJson(QJsonDocument::Indented));
    if (!file.commit()) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    learning_ = data;
    return true;
}

QString AppConfig::cleanText(const QJsonObject& obj, const char* key, QString fallback, qsizetype maxLen)
{
    const auto value = obj.value(QString::fromLatin1(key));
    if (!value.isString()) {
        return fallback;
    }
    auto text = value.toString().trimmed();
    text.remove(QRegularExpression(QStringLiteral("[\\x00-\\x1F]")));
    if (text.size() > maxLen) {
        text.truncate(maxLen);
    }
    return text.isEmpty() ? fallback : text;
}

double AppConfig::cleanDouble(const QJsonObject& obj, const char* key, double fallback, double lo, double hi)
{
    const auto value = obj.value(QString::fromLatin1(key));
    const double raw = value.isDouble() ? value.toDouble(fallback) : fallback;
    if (!std::isfinite(raw)) {
        return fallback;
    }
    return std::clamp(raw, lo, hi);
}

int AppConfig::cleanInt(const QJsonObject& obj, const char* key, int fallback, int lo, int hi)
{
    const auto value = obj.value(QString::fromLatin1(key));
    const int raw = value.isDouble() ? value.toInt(fallback) : fallback;
    return std::clamp(raw, lo, hi);
}

bool AppConfig::cleanBool(const QJsonObject& obj, const char* key, bool fallback)
{
    const auto value = obj.value(QString::fromLatin1(key));
    return value.isBool() ? value.toBool(fallback) : fallback;
}

void AppConfig::loadSettingsObject(const QJsonObject& obj)
{
    const bool hasCanonicalInputSource =
        obj.contains(QStringLiteral("remote_play_input_source"));
    data_.launcherTheme = cleanText(obj, "launcher_theme", data_.launcherTheme, 32);
    data_.customAccent = cleanText(obj, "launcher_custom_accent", data_.customAccent, 16);
    {
        const QString channel = cleanText(obj, "update_channel", data_.updateChannel, 16).trimmed().toLower();
        // Unknown ring -> the SHIPPED default ring (2026-07-25: beta -> stable, tracks
        // AppConfigData::updateChannel). An explicit valid ring in settings.json still wins.
        data_.updateChannel = (channel == QLatin1String("dev") || channel == QLatin1String("internal")
                               || channel == QLatin1String("beta") || channel == QLatin1String("stable"))
            ? channel
            : QStringLiteral("stable");
    }
    {
        const QString trigger = cleanText(obj, "defense_trigger_button", data_.defenseTriggerButton, 16).trimmed().toLower();
        data_.defenseTriggerButton = (trigger == QLatin1String("dpad_up") || trigger == QLatin1String("dpad_down")
                                      || trigger == QLatin1String("dpad_left") || trigger == QLatin1String("dpad_right"))
            ? trigger
            : QStringLiteral("dpad_up");
    }
    data_.defenseStickAssist = cleanBool(obj, "defense_stick_assist", data_.defenseStickAssist);
    data_.defenseStickAssistStrength = cleanDouble(obj, "defense_stick_assist_strength", data_.defenseStickAssistStrength, 0.0, 1.0);
    data_.defenseL2HoldAssist = cleanBool(obj, "defense_l2_hold_assist", data_.defenseL2HoldAssist);
    data_.defenseSprintAssist = cleanBool(obj, "defense_sprint_assist", data_.defenseSprintAssist);
    data_.defenseLightbarColor = cleanText(obj, "defense_lightbar_color", data_.defenseLightbarColor, 16);
    data_.activeProfile = cleanText(obj, "active_profile", data_.activeProfile, 48);
    if (obj.value(QStringLiteral("profiles")).isObject()) {
        profiles_ = obj.value(QStringLiteral("profiles")).toObject();
    }
    if (obj.value(QStringLiteral("meter_template_anchor")).isObject()) {
        data_.meterTemplateAnchor = obj.value(QStringLiteral("meter_template_anchor")).toObject();
    }
    if (obj.value(QStringLiteral("meter_trained_green_hsv_low")).isArray()
        && obj.value(QStringLiteral("meter_trained_green_hsv_high")).isArray()) {
        data_.meterTrainedGreenHsvLow = obj.value(QStringLiteral("meter_trained_green_hsv_low")).toArray();
        data_.meterTrainedGreenHsvHigh = obj.value(QStringLiteral("meter_trained_green_hsv_high")).toArray();
    }
    data_.meterColor = cleanText(obj, "meter_color", data_.meterColor, 48);
    data_.meterStyle = cleanText(obj, "meter_style", data_.meterStyle, 48);
    {
        // Unknown style/colour -> the SHIPPED defaults (2026-07-25 reconciliation: Arrow/Purple ->
        // Arrow2/Red, tracking AppConfigData). A valid explicit value in settings.json still wins.
        const QString style = data_.meterStyle.trimmed().toLower();
        if (style != QLatin1String("arrow")
            && style != QLatin1String("arrow2")
            && style != QLatin1String("dial")
            && style != QLatin1String("pill")
            && style != QLatin1String("straight")
            && style != QLatin1String("sword")) {
            data_.meterStyle = QStringLiteral("Arrow2");
        }
        const QString color = data_.meterColor.trimmed().toLower();
        if (color != QLatin1String("purple")
            && color != QLatin1String("white")
            && color != QLatin1String("yellow")
            && color != QLatin1String("red")) {
            data_.meterColor = QStringLiteral("Red");
        }
    }
    data_.remotePlayClientMode = cleanText(obj, "remote_play_client_mode", data_.remotePlayClientMode, 48);
    {
        const QString console = cleanText(obj, "remote_play_console", data_.remotePlayConsole, 16).trimmed().toLower();
        data_.remotePlayConsole = (console == QLatin1String("xbox")) ? QStringLiteral("Xbox") : QStringLiteral("PS5");
    }
    data_.streamSetupComplete = cleanBool(obj, "stream_setup_complete", data_.streamSetupComplete);
    data_.preflightComplete = cleanBool(obj, "preflight_complete", data_.preflightComplete);
    if (obj.contains(QStringLiteral("legal_accepted_version"))) {
        data_.legalAcceptedVersion = cleanInt(obj, "legal_accepted_version", data_.legalAcceptedVersion, 0, 100000);
    } else if (cleanBool(obj, "legal_accepted", false)) {
        // Migrate a pre-versioning acceptance: treat the legacy bool as acceptance of the
        // currently-shipped terms so existing users aren't re-prompted on upgrade.
        data_.legalAcceptedVersion = AppConfigData::kCurrentLegalVersion;
    }
    {
        const QString vs = cleanText(obj, "video_source", data_.videoSource, 24).trimmed().toLower();
        data_.videoSource = (vs == QLatin1String("capture_card") || vs == QLatin1String("capture card")
                             || vs == QLatin1String("capturecard"))
                                ? QStringLiteral("capture_card") : QStringLiteral("decoder");
    }
    data_.captureCardIndex = cleanInt(obj, "capture_card_index", data_.captureCardIndex, 0, 16);
    data_.hardwareDecode = cleanBool(obj, "hardware_decode", data_.hardwareDecode);
    // Controller type is pinned to X360 (the only supported virtual pad — DS4 emulation
    // was buggy and is no longer selectable). Any legacy "DS4" in settings normalises to X360.
    data_.controllerType = QStringLiteral("X360");
    data_.autoReconnect = cleanBool(obj, "auto_reconnect", data_.autoReconnect);
    data_.remotePlayWindowTitle = cleanText(obj, "remote_play_window_title", data_.remotePlayWindowTitle, 160);
    data_.chiakiPath = cleanText(obj, "chiaki_path", data_.chiakiPath, 512);
    data_.remotePlayConsoleIp = cleanText(obj, "remote_play_console_ip", data_.remotePlayConsoleIp, 64);
    data_.remotePlayProfile = cleanText(obj, "remote_play_profile", data_.remotePlayProfile, 64);
    if (hasCanonicalInputSource) {
        data_.remotePlayInputSource = normalizedRemotePlayInputSource(
            cleanText(obj, "remote_play_input_source", QStringLiteral("square"), 32));
    }
    {
        const QString rawMode = cleanText(obj, "stream_bandwidth_mode", data_.streamBandwidthMode, 24);
        const QString lower = rawMode.toLower();
        if (lower == QLatin1String("quality")) data_.streamBandwidthMode = QStringLiteral("Quality");
        else if (lower == QLatin1String("performance")) data_.streamBandwidthMode = QStringLiteral("Performance");
        else if (lower == QLatin1String("balanced")) data_.streamBandwidthMode = QStringLiteral("Balanced");
        else if (lower == QLatin1String("lowbandwidth") || lower == QLatin1String("low") || lower == QLatin1String("low_bandwidth"))
            data_.streamBandwidthMode = QStringLiteral("LowBandwidth");
        else if (lower == QLatin1String("ultralow") || lower == QLatin1String("ultra_low") || lower == QLatin1String("ultra"))
            data_.streamBandwidthMode = QStringLiteral("UltraLow");
        else if (lower == QLatin1String("experimental120") || lower == QLatin1String("probe120"))
            data_.streamBandwidthMode = QStringLiteral("Experimental120");
        else if (lower == QLatin1String("experimental240") || lower == QLatin1String("probe240"))
            data_.streamBandwidthMode = QStringLiteral("Experimental240");
        else data_.streamBandwidthMode = QStringLiteral("Performance");  // unknown -> shipped default
    }
    {
        const QString backend = cleanText(obj, "stream_render_backend", data_.streamRenderBackend, 16).toLower();
        data_.streamRenderBackend = backend == QLatin1String("opengl")
            ? QStringLiteral("opengl")
            : QStringLiteral("vulkan");
    }
    data_.streamAudioEnabled = cleanBool(obj, "stream_audio_enabled", data_.streamAudioEnabled);
    {
        const QString rawMode = cleanText(obj, "stream_audio_mode", data_.streamAudioEnabled ? data_.streamAudioMode : QStringLiteral("Off"), 24);
        const QString lower = rawMode.toLower();
        if (lower == QLatin1String("off") || !data_.streamAudioEnabled) {
            data_.streamAudioMode = QStringLiteral("Off");
            data_.streamAudioEnabled = false;
        } else if (lower == QLatin1String("stabilized") || lower == QLatin1String("stabilised")) {
            data_.streamAudioMode = QStringLiteral("Stabilized");
            data_.streamAudioEnabled = true;
        } else {
            data_.streamAudioMode = QStringLiteral("Standard");
            data_.streamAudioEnabled = true;
        }
    }
    data_.controllerLightbarEnabled = cleanBool(obj, "controller_lightbar_enabled", data_.controllerLightbarEnabled);
    {
        const QString color = cleanText(obj, "controller_lightbar_color", data_.controllerLightbarColor, 16);
        static const QRegularExpression hexColor(QStringLiteral("^#[0-9A-Fa-f]{6}$"));
        data_.controllerLightbarColor = hexColor.match(color).hasMatch()
            ? color.toUpper()
            : QStringLiteral("#7C3AED");
        const QString primary = cleanText(obj, "controller_lightbar_primary_color", data_.controllerLightbarColor, 16);
        data_.controllerLightbarPrimaryColor = hexColor.match(primary).hasMatch()
            ? primary.toUpper()
            : data_.controllerLightbarColor;
        const QString secondary = cleanText(obj, "controller_lightbar_secondary_color", data_.controllerLightbarSecondaryColor, 16);
        data_.controllerLightbarSecondaryColor = hexColor.match(secondary).hasMatch()
            ? secondary.toUpper()
            : QStringLiteral("#4F8CFF");
        const QString rawMode = cleanText(obj, "controller_lightbar_mode", data_.controllerLightbarMode, 24);
        const QString lowerMode = rawMode.toLower();
        if (lowerMode == QLatin1String("pulse")) data_.controllerLightbarMode = QStringLiteral("Pulse");
        else if (lowerMode == QLatin1String("strobe")) data_.controllerLightbarMode = QStringLiteral("Strobe");
        else if (lowerMode == QLatin1String("rainbow")) data_.controllerLightbarMode = QStringLiteral("Rainbow");
        else data_.controllerLightbarMode = QStringLiteral("Solid");
        data_.controllerLightbarBrightness = cleanDouble(obj, "controller_lightbar_brightness", data_.controllerLightbarBrightness, 0.05, 1.0);
        data_.controllerLightbarEffectSpeed = cleanDouble(obj, "controller_lightbar_effect_speed", data_.controllerLightbarEffectSpeed, 0.2, 4.0);
    }
    {
        // Detection-overlay appearance.
        //
        // COLOUR IS NOT CUSTOMISABLE ANY MORE (2026-08-06, owner: "remove the
        // custom detection box completely, have a bright blue meter detection
        // box as the default"). The swatch picker and the RGB-cycle toggle
        // were removed from the UI the same day, so a persisted
        // meter_overlay_color / meter_overlay_rgb — whether the pre-2026-08-06
        // factory violet (#CC44FF, kMeterOverlayLegacyDefaultColor), an old
        // explicit pick, or a stuck-on RGB cycle — has no UI path left that
        // could ever change it back. Pin BOTH to the factory default here so
        // every install converges on the bright blue on next launch; this
        // supersedes the narrower legacy-violet migration that used to live in
        // this block. Style survives as the one remaining knob: it is shape,
        // not colour.
        data_.meterOverlayColor =
            QString::fromLatin1(AppConfigData::kMeterOverlayDefaultColor);
        data_.meterOverlayRgb = false;
        const QString rawStyle =
            cleanText(obj, "meter_overlay_style", data_.meterOverlayStyle, 24);
        const QString lowerStyle = rawStyle.toLower();
        if (lowerStyle == QLatin1String("brackets")) data_.meterOverlayStyle = QStringLiteral("Brackets");
        else if (lowerStyle == QLatin1String("hairline")) data_.meterOverlayStyle = QStringLiteral("Hairline");
        else data_.meterOverlayStyle = QStringLiteral("Solid");
    }

    data_.rttSyncMode = cleanText(obj, "orion_sync_mode", data_.rttSyncMode, 16);
    if (data_.rttSyncMode.compare(QStringLiteral("manual"), Qt::CaseInsensitive) == 0) {
        data_.rttSyncMode = QStringLiteral("Manual");
    } else {
        data_.rttSyncMode = QStringLiteral("Auto");
    }
    data_.manualSyncAdjustMs = cleanDouble(obj, "manual_sync_adjust_ms", data_.manualSyncAdjustMs, -100.0, 250.0);
    data_.manualOffsetMs = cleanDouble(obj, "manual_offset_ms", data_.manualOffsetMs, -100.0, 250.0);
    if (!hasCanonicalInputSource) {
        data_.remotePlayInputSource = normalizedRemotePlayInputSource(
            cleanText(obj, "hold_square_input_source", data_.remotePlayInputSource, 32),
            data_.remotePlayInputSource);
        data_.remotePlayInputSource = normalizedRemotePlayInputSource(
            cleanText(obj, "tempo_input_mode", data_.remotePlayInputSource, 32),
            data_.remotePlayInputSource);
    }
    data_.greenWindowTargetMode = cleanText(obj, "green_window_target_mode", data_.greenWindowTargetMode, 24).toLower();
    if (data_.greenWindowTargetMode != QLatin1String("start")
        && data_.greenWindowTargetMode != QLatin1String("center")
        && data_.greenWindowTargetMode != QLatin1String("end")
        && data_.greenWindowTargetMode != QLatin1String("tip")) {
        data_.greenWindowTargetMode = QStringLiteral("tip");
    }
    data_.feedforwardAnchor = cleanText(obj, "feedforward_anchor", data_.feedforwardAnchor, 16).toLower();
    if (data_.feedforwardAnchor != QLatin1String("hold_start")
        && data_.feedforwardAnchor != QLatin1String("meter_appear")) {
        data_.feedforwardAnchor = QStringLiteral("meter_appear");
    }
    // T2 anchor-validity gates for the meter-appear clock (carryover rejection).
    data_.anchorMaxFirstFillPct = cleanDouble(obj, "anchor_max_first_fill_pct", data_.anchorMaxFirstFillPct, 10.0, 70.0);
    data_.anchorRiseMinPct = cleanDouble(obj, "anchor_rise_min_pct", data_.anchorRiseMinPct, 0.5, 15.0);
    // Candidate-A decision-budget flag: 2-frame strict ownership proof (default OFF).
    data_.ownershipProofTwoFrame = cleanBool(obj, "ownership_proof_two_frame", data_.ownershipProofTwoFrame);
    // Candidate-B decision-budget flag: base anchor 30 -> 20 constellation (default OFF).
    data_.tipPhaseAnchorBase20 = cleanBool(obj, "tip_phase_anchor_base20", data_.tipPhaseAnchorBase20);
    // [ORION_TYPE_TRIM] per-shot-type tip-phase trim (default OFF). Values clamped to
    // single-digit ms so a stale settings entry can never move an aim by more than one
    // green-window sliver; keys are exact classifyShotType labels.
    data_.tipPhaseTypeTrimEnabled = cleanBool(obj, "tip_phase_type_trim_enabled", data_.tipPhaseTypeTrimEnabled);
    {
        const auto trim = obj.value(QStringLiteral("tip_phase_type_trim")).toObject();
        for (auto it = trim.constBegin(); it != trim.constEnd(); ++it) {
            data_.tipPhaseTypeTrimMs.insert(it.key(), qBound(-9.0, it.value().toDouble(), 9.0));
        }
    }
    // [ORION_STOP_CORROBORATE] two-sample confirmation before a stop re-open (default OFF).
    data_.stopReopenCorroborate = cleanBool(obj, "stop_reopen_corroborate", data_.stopReopenCorroborate);
    // [ORION_STOP_SUBFRAME] sub-frame end-of-rise stop dating for the phase learner (default OFF).
    data_.stopDatingSubframe = cleanBool(obj, "stop_dating_subframe", data_.stopDatingSubframe);
    // [ORION_PHASE_VETO_DIRECTIONAL] one-directional live-meter veto (default OFF everywhere;
    // both the default flip and the v1 migration were withdrawn 2026-08-08 -- see
    // settingsMigrations() for the history and the re-enable condition).
    data_.phaseVetoDirectional = cleanBool(obj, "phase_veto_directional", data_.phaseVetoDirectional);
    // [ORION_AIM_FREEZE] hold the learned aim still for the session (default OFF).
    data_.tipPhaseAimFrozen = cleanBool(obj, "tip_phase_aim_frozen", data_.tipPhaseAimFrozen);
    // Rung-imminent hold flag: extend the imminent hold to ladder rungs (default OFF).
    data_.tipPhaseRungImminentHold = cleanBool(obj, "tip_phase_rung_imminent_hold", data_.tipPhaseRungImminentHold);
    // Tempo tip-parity flag: TempoStick joins the canonical ButtonShot tip pipeline (default OFF).
    data_.tempoTipParity = cleanBool(obj, "tempo_tip_parity", data_.tempoTipParity);
    // Go-To tip-parity flag: GoToStick joins the owned bounded-hold ladder (default OFF).
    data_.gotoTipParity = cleanBool(obj, "goto_tip_parity", data_.gotoTipParity);
    // [ORION_USER_LEAD_AUTHORITY] user-set lead may satisfy measured-lead readiness (default OFF).
    data_.userLeadSatisfiesAuthority = cleanBool(obj, "user_lead_satisfies_authority", data_.userLeadSatisfiesAuthority);
    data_.tempoFadeMirrorGesture = cleanBool(obj, "tempo_fade_mirror_gesture", data_.tempoFadeMirrorGesture);
    // [ORION_PROBE_CACHE_AUTHORITY] accept the probe-cache seed source in the factory
    // authority contract (default OFF; pairs with sidecar ORION_PROBE_PRIOR_AUTHORITY).
    data_.probeCachePriorAuthority = cleanBool(
        obj, "probe_cache_prior_authority", data_.probeCachePriorAuthority);
    // [ORION_PROBE_COUNT] warmup probe-run length (default 16 = today's behaviour).
    data_.latencyProbeCount = static_cast<int>(std::lround(cleanDouble(
        obj, "latency_probe_count", static_cast<double>(data_.latencyProbeCount), 4.0, 48.0)));
    // T4 tip gate: defer a due clock to the predicted tip when vision is healthy.
    if (obj.contains(QStringLiteral("tip_gate_enabled"))) {
        data_.tipGateEnabled = obj.value(QStringLiteral("tip_gate_enabled")).toBool(data_.tipGateEnabled);
    }
    data_.tipGateCapMs = cleanDouble(obj, "tip_gate_cap_ms", data_.tipGateCapMs, 0.0, 400.0);
    // EXPERIMENT (default OFF): native memory-trust A/B knob. A normal persisted setting
    // (unlike the tempo flags below, it is intentionally NOT force-reset at the end of load)
    // so it can be enabled for a test batch via the settings.json round-trip.
    data_.memoryTrustEnabled = cleanBool(obj, "memory_trust_enabled", data_.memoryTrustEnabled);
    // Hybrid global phase clock A/B knob (NOT force-reset at end of load, like memory_trust_enabled).
    data_.autonomousVision = cleanBool(obj, "autonomous_vision", data_.autonomousVision);
    data_.autonomousVisionShadow = cleanBool(obj, "autonomous_vision_shadow", data_.autonomousVisionShadow);
    // Tip-timing improvement flags (2026-07 W-series; NOT force-reset at end of load, like autonomous_vision).
    data_.measuredLeadEnabled = cleanBool(obj, "measured_lead", data_.measuredLeadEnabled);
    data_.tickLockEnabled = cleanBool(obj, "tick_lock", data_.tickLockEnabled);
    data_.regFusionEnabled = cleanBool(obj, "reg_fusion", data_.regFusionEnabled);
    data_.leadLearnerVisionGate = cleanBool(obj, "lead_learner_vision_gate", data_.leadLearnerVisionGate);
    data_.fusedFireEnabled = cleanBool(obj, "fused_fire", data_.fusedFireEnabled);
    data_.fusedShadowEnabled = cleanBool(obj, "fused_shadow", data_.fusedShadowEnabled);
    data_.gradeV2Enabled = cleanBool(obj, "grade_v2", data_.gradeV2Enabled);
    // Ceiling stack flags (2026-07 perfect-green build; default OFF = current behavior).
    data_.plateauAimEnabled = cleanBool(obj, "plateau_aim", data_.plateauAimEnabled);
    data_.templateArrivalEnabled = cleanBool(obj, "template_arrival", data_.templateArrivalEnabled);
    data_.pressT0Enabled = cleanBool(obj, "press_t0", data_.pressT0Enabled);
    data_.banditLeadEnabled = cleanBool(obj, "bandit_lead", data_.banditLeadEnabled);
    data_.memoryTrustMaxAgeMs = cleanDouble(obj, "memory_trust_max_age_ms", data_.memoryTrustMaxAgeMs, 0.0, 200.0);
    data_.memoryTrustFillSlackPct = cleanDouble(obj, "memory_trust_fill_slack_pct", data_.memoryTrustFillSlackPct, 0.0, 20.0);
    data_.memoryTrustMaxFillPct = cleanDouble(obj, "memory_trust_max_fill_pct", data_.memoryTrustMaxFillPct, 50.0, 100.0);
    data_.meterEnabled = cleanBool(obj, "meter_enabled", data_.meterEnabled);
    // [ORION_METER_DELAY 2026-08-07] Load meter-delay actuator config. Clamp
    // meter_delay_ms into [kMeterDelayMinMs, kMeterDelayMaxMs] (100-600 since
    // 2026-08-08) — matches nexus_svc's _METER_DELAY_HARD_MAX_MS and
    // MeterDelayController::kManualMinMs..kManualMaxMs.
    data_.meterDelayEnabled = cleanBool(obj, "meter_delay_enabled", data_.meterDelayEnabled);
    data_.meterDelayMs = cleanInt(obj, "meter_delay_ms", data_.meterDelayMs,
                                  AppConfigData::kMeterDelayMinMs, AppConfigData::kMeterDelayMaxMs);
    data_.meterDelayBypassOnDefense = cleanBool(obj, "meter_delay_bypass_on_defense", data_.meterDelayBypassOnDefense);
    data_.freezeCalibration = cleanBool(obj, "freeze_calibration", data_.freezeCalibration);
    data_.bannerCalibration = cleanBool(obj, "banner_calibration", data_.bannerCalibration);
    data_.tempoEnabled = cleanBool(obj, "tempo_mode_enabled", data_.tempoEnabled);
    data_.tempoRemapEnabled = cleanBool(obj, "tempo_remap_enabled", data_.tempoRemapEnabled);
    data_.tempoFlickEnabled = cleanBool(obj, "tempo_flick_enabled", data_.tempoFlickEnabled);
    data_.tempoRemapType = cleanText(obj, "tempo_remap_type", data_.tempoRemapType, 16);
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] #88. An unrecognised button string leaves the feature
    // inert rather than defaulting to a binding the user did not ask for; squarePassthroughBit()
    // maps anything outside {r3,l3} to 0.
    data_.squarePassthroughEnabled =
        cleanBool(obj, "square_passthrough_enabled", data_.squarePassthroughEnabled);
    data_.squarePassthroughButton =
        cleanText(obj, "square_passthrough_button", data_.squarePassthroughButton, 8);
    data_.noDipEnabled = cleanBool(obj, "no_dip_enabled", data_.noDipEnabled);
    data_.noDipLeadMs = cleanDouble(obj, "no_dip_lead_ms", data_.noDipLeadMs, -150.0, 150.0);
    // [ORION_USER_LEAD] accepted set is {0} u [150, 800]: 0 means "not configured yet" (the
    // engine then keeps its pre-existing authority path verbatim), anything else is a real lead.
    // A hand-edited in-between value is clamped UP into the band rather than silently discarded —
    // the user asked for a lead, so honour the nearest one this control is allowed to install.
    data_.actuationLeadMs = cleanDouble(obj, "actuation_lead_ms", data_.actuationLeadMs, 0.0,
                                        AppConfigData::kActuationLeadMaxMs);
    if (data_.actuationLeadMs > 0.0 && data_.actuationLeadMs < AppConfigData::kActuationLeadMinMs) {
        data_.actuationLeadMs = AppConfigData::kActuationLeadMinMs;
    }
    data_.actuationLeadUserSet = cleanBool(obj, "actuation_lead_user_set", data_.actuationLeadUserSet);
    // [ORION_METER_DELAY_LEAD_KEYING] Signed Shot Lead offset for the delayed condition. Clamped
    // into its own band by cleanDouble; 0 (the default) leaves the engine byte-identical.
    data_.meterDelayLeadOffsetMs =
        cleanDouble(obj, "meter_delay_lead_offset_ms", data_.meterDelayLeadOffsetMs,
                    AppConfigData::kMeterDelayLeadOffsetMinMs,
                    AppConfigData::kMeterDelayLeadOffsetMaxMs);
    // [ORION_USER_TIP] user-chosen tip-timing marker (presentation/reset state; the freeze
    // flag above is what actually holds the aim — see AppConfigData::tipTimingUserSet).
    data_.tipTimingUserSet = cleanBool(obj, "tip_timing_user_set", data_.tipTimingUserSet);
    // [ORION_PRESS_ANCHOR] Phase-B arm + per-type calibration written by the Phase-A collector.
    // Tip band [150, 2500]: the measured delay-0 press->tip census spans ~446 (No Dip) to
    // ~1047 (Left Fade); 2500 leaves headroom for slow Go-To wind-ups while rejecting a
    // mis-paired landing. Sigma band mirrors the decision-path 500ms sigma sanity ceiling.
    data_.pressAnchoredPredictorEnabled = cleanBool(
        obj, "press_anchored_predictor_enabled", data_.pressAnchoredPredictorEnabled);
    data_.pressAnchoredPredictorMinSamples = cleanInt(
        obj, "press_anchored_predictor_min_samples",
        data_.pressAnchoredPredictorMinSamples, 1, 1000);
    {
        const auto pa = obj.value(QStringLiteral("press_anchored_tip_ms")).toObject();
        for (auto it = pa.constBegin(); it != pa.constEnd(); ++it) {
            data_.pressAnchoredTipMs.insert(it.key(),
                                            qBound(150.0, it.value().toDouble(), 2500.0));
        }
    }
    {
        const auto ps = obj.value(QStringLiteral("press_anchored_tip_sigma_ms")).toObject();
        for (auto it = ps.constBegin(); it != ps.constEnd(); ++it) {
            data_.pressAnchoredTipSigmaMs.insert(it.key(),
                                                 qBound(1.0, it.value().toDouble(), 500.0));
        }
    }
    {
        const auto pn = obj.value(QStringLiteral("press_anchored_tip_n")).toObject();
        for (auto it = pn.constBegin(); it != pn.constEnd(); ++it) {
            data_.pressAnchoredTipN.insert(it.key(),
                                           qBound(0.0, it.value().toDouble(), 100.0));
        }
    }
    // [ORION_PROBE] Band ceiling 2000 is the estimator's own probe expiry (latency_estimator.py
    // _expire_pending_probe): a hold longer than that could not close even with a perfect meter.
    // Floor 0 keeps "absent/0 = engine default" meaningful.
    data_.probePressMs = cleanDouble(obj, "probe_press_ms", data_.probePressMs, 0.0, 2000.0);
    // Ceiling 30s: 8 probes at that gap is a 4-minute run, already past any reasonable session.
    data_.probeGapMs = cleanDouble(obj, "probe_gap_ms", data_.probeGapMs, 0.0, 30000.0);
    data_.probeCallLeadMs = cleanDouble(obj, "probe_call_lead_ms", data_.probeCallLeadMs,
                                        0.0, 10000.0);
    // [ORION_GREEN_CENTER] Band [0,1]: 0 aims at the window's late edge, 1 at its centre. Values
    // above 1 would aim past the centre toward the EARLY edge, which trades one cliff for the
    // other, so the band stops at the centre.
    data_.autonomousGreenCenterFrac = cleanDouble(obj, "autonomous_green_center_frac",
                                                  data_.autonomousGreenCenterFrac, 0.0, 1.0);
    data_.autonomousGreenCenterMaxMs = cleanDouble(obj, "autonomous_green_center_max_ms",
                                                   data_.autonomousGreenCenterMaxMs, 0.0, 80.0);
    data_.noMeterEnabled = cleanBool(obj, "no_meter_enabled", data_.noMeterEnabled);
    data_.noMeterReleasePoint = cleanText(obj, "no_meter_release_point", data_.noMeterReleasePoint, 32);
    // Range widened 2026-08-08 from [-40, 40] to [-200, 200]: the header default is 83.0
    // (pose-to-real-tip latency compensation for No-Meter Mode). The old ±40 clamp silently
    // pulled the default down to 40, invalidating any live calibration. 200 covers the
    // reasonable envelope for the compensation across customer-rig latency variation.
    data_.noMeterBaseOffsetMs = cleanDouble(obj, "no_meter_base_offset_ms", data_.noMeterBaseOffsetMs, -200.0, 200.0);
    data_.noMeterDecodeCompMs = cleanDouble(obj, "no_meter_decode_comp_ms", data_.noMeterDecodeCompMs, 0.0, 40.0);
    data_.noMeterConfidenceGate = cleanDouble(obj, "no_meter_confidence_gate", data_.noMeterConfidenceGate, 0.50, 0.95);
    data_.noMeterPushReleaseWindowMs = cleanDouble(obj, "no_meter_push_release_window_ms", data_.noMeterPushReleaseWindowMs, 100.0, 400.0);
    data_.noMeterHandedness = cleanText(obj, "no_meter_handedness", data_.noMeterHandedness, 16);
    data_.activeShotType = cleanText(obj, "active_shot_type", data_.activeShotType, 32);
    {
        const auto sto = obj.value(QStringLiteral("shot_type_offsets")).toObject();
        for (auto it = sto.constBegin(); it != sto.constEnd(); ++it) {
            data_.shotTypeOffsets.insert(it.key(), qBound(-250.0, it.value().toDouble(), 250.0));
        }
    }
    {
        const auto mo = obj.value(QStringLiteral("shot_type_mode_override")).toObject();
        for (auto it = mo.constBegin(); it != mo.constEnd(); ++it) {
            const QString v = it.value().toString().trimmed().toLower();
            if (v == QLatin1String("auto") || v == QLatin1String("normal") || v == QLatin1String("tempo")) {
                data_.shotTypeModeOverride.insert(it.key(), v);
            }
        }
        const auto fh = obj.value(QStringLiteral("shot_type_flick_hold_ms")).toObject();
        for (auto it = fh.constBegin(); it != fh.constEnd(); ++it) {
            data_.shotTypeFlickHoldMs.insert(it.key(), qBound(10.0, it.value().toDouble(), 500.0));
        }
    }
    data_.tempoOpenLoopPrimary = cleanBool(obj, "tempo_open_loop_primary", data_.tempoOpenLoopPrimary);
    data_.ffReachabilitySlackPct = cleanDouble(obj, "ff_reachability_slack_pct", data_.ffReachabilitySlackPct, 0.0, 60.0);
    data_.lsCancelThresholdPct = cleanDouble(obj, "ls_cancel_threshold_pct", data_.lsCancelThresholdPct, 10.0, 127.0);
    data_.lsCancelFrames = cleanInt(obj, "ls_cancel_frames", data_.lsCancelFrames, 1, 30);
    data_.calibrationMode = cleanBool(obj, "calibration_mode", data_.calibrationMode);
    data_.networkEnabled = cleanBool(obj, "network_enabled", data_.networkEnabled);
    // [VENICENET WAVE 1 2026-08-08] The legacy passive-sniffing capture keys (the
    // opt-in key and the older always-on key)
    // are deliberately NOT read: the opt-in flag is deleted and a persisted legacy
    // opt-out must not resurrect it. Network-side features ship ON.
    data_.cudaEnabled = cleanBool(obj, "cuda_enabled", data_.cudaEnabled);
    data_.latencyCompensationMs = cleanDouble(obj, "latency_compensation_ms", data_.latencyCompensationMs, 0.0, 250.0);
    data_.releaseThresholdPct = cleanDouble(obj, "release_threshold", data_.releaseThresholdPct, 50.0, 100.0);
    data_.fixedHoldMs = cleanDouble(obj, "fixed_hold_time_ms", data_.fixedHoldMs, 100.0, 2000.0);
    data_.earlyLateOffsetMs = cleanDouble(obj, "early_late_offset_ms", data_.earlyLateOffsetMs, -100.0, 100.0);
    data_.tempoWaitMs = cleanDouble(obj, "tempo_wait_ms", data_.tempoWaitMs, 0.0, 250.0);
    // [ORION_FLICK_FLOOR 2026-08-13] Lower bound is 50, not 10, because AutomationEngine applies
    // `std::max(flickHold, releasePulseMs)` and releasePulseMs is a fixed 50.0 (engine-only, never
    // configurable). Everything under 50 therefore ran as 50: this owner's stored 16 had been
    // silently executing as 50 for the whole life of the setting, and the slider's bottom 40 ms was
    // dead travel with no indication. Clamping here makes stored == displayed == effective, and
    // migrates an existing sub-floor value up on the next load.
    data_.tempoFlickHoldMs = cleanDouble(obj, "tempo_flick_hold_ms", data_.tempoFlickHoldMs, 50.0, 500.0);
    data_.tempoMinStickHoldMs = cleanDouble(obj, "tempo_min_stick_hold_ms", data_.tempoMinStickHoldMs, 0.0, 500.0);
    data_.tempoFallbackTimeoutMs = cleanDouble(obj, "tempo_fallback_timeout_ms", data_.tempoFallbackTimeoutMs, 100.0, 3000.0);
    data_.minimumHoldMs = cleanDouble(obj, "minimum_hold_time_ms", data_.minimumHoldMs, 0.0, 3000.0);
    data_.maximumHoldMs = cleanDouble(obj, "maximum_hold_time_ms", data_.maximumHoldMs, 50.0, 5000.0);
    data_.detectionConfidencePercent = cleanInt(obj, "detection_confidence_percent", data_.detectionConfidencePercent, 0, 100);
    data_.autoMeterColor = cleanBool(obj, "auto_meter_color", data_.autoMeterColor);
    data_.showLiveMeterMetrics = cleanBool(
        obj, "show_live_meter_metrics", data_.showLiveMeterMetrics);
    data_.stableFrames = cleanInt(obj, "hold_stable_frames", data_.stableFrames, 1, 10);

    const auto latency = obj.value(QStringLiteral("latency")).toObject();
    if (!latency.isEmpty()) {
        data_.latencyCompensationMs = cleanDouble(latency, "total_latency_ms", data_.latencyCompensationMs, 0.0, 250.0);
    }
    const auto tempo = obj.value(QStringLiteral("tempo")).toObject();
    if (!tempo.isEmpty()) {
        data_.tempoEnabled = cleanBool(tempo, "enabled", data_.tempoEnabled);
        data_.tempoRemapEnabled = cleanBool(tempo, "remap_enabled", data_.tempoRemapEnabled);
        data_.tempoFlickEnabled = cleanBool(tempo, "flick_enabled", data_.tempoFlickEnabled);
        if (!hasCanonicalInputSource) {
            data_.remotePlayInputSource = normalizedRemotePlayInputSource(
                cleanText(tempo, "input_source", data_.remotePlayInputSource, 32),
                data_.remotePlayInputSource);
            data_.remotePlayInputSource = normalizedRemotePlayInputSource(
                cleanText(tempo, "input_mode", data_.remotePlayInputSource, 32),
                data_.remotePlayInputSource);
        }
        data_.tempoWaitMs = cleanDouble(tempo, "wait_ms", data_.tempoWaitMs, 0.0, 250.0);
        // Same 50 ms engine floor as the flat tempo_flick_hold_ms key above.
        data_.tempoFlickHoldMs = cleanDouble(tempo, "flick_hold_ms", data_.tempoFlickHoldMs, 50.0, 500.0);
        data_.tempoMinStickHoldMs = cleanDouble(tempo, "min_stick_hold_ms", data_.tempoMinStickHoldMs, 0.0, 500.0);
        data_.tempoFallbackTimeoutMs = cleanDouble(tempo, "fallback_timeout_ms", data_.tempoFallbackTimeoutMs, 100.0, 3000.0);
    }
    const auto noMeter = obj.value(QStringLiteral("no_meter")).toObject();
    if (!noMeter.isEmpty()) {
        data_.noMeterEnabled = cleanBool(noMeter, "enabled", data_.noMeterEnabled);
        data_.noMeterReleasePoint = cleanText(noMeter, "release_point", data_.noMeterReleasePoint, 32);
        // Range widened 2026-08-08 — same reason as the root-key load above.
        data_.noMeterBaseOffsetMs = cleanDouble(noMeter, "base_offset_ms", data_.noMeterBaseOffsetMs, -200.0, 200.0);
        data_.noMeterDecodeCompMs = cleanDouble(noMeter, "decode_comp_ms", data_.noMeterDecodeCompMs, 0.0, 40.0);
        data_.noMeterConfidenceGate = cleanDouble(noMeter, "confidence_gate", data_.noMeterConfidenceGate, 0.50, 0.95);
        data_.noMeterPushReleaseWindowMs = cleanDouble(noMeter, "push_release_window_ms", data_.noMeterPushReleaseWindowMs, 100.0, 400.0);
        data_.noMeterHandedness = cleanText(noMeter, "handedness", data_.noMeterHandedness, 16);
    }
    const auto rp = data_.noMeterReleasePoint.trimmed().toLower();
    if (rp == QLatin1String("jump") || rp == QLatin1String("jump start")) {
        data_.noMeterReleasePoint = QStringLiteral("Jump");
    } else if (rp == QLatin1String("set") || rp == QLatin1String("set point") || rp == QLatin1String("set shot")) {
        data_.noMeterReleasePoint = QStringLiteral("Set Point");
    } else if (rp == QLatin1String("release") || rp == QLatin1String("release point")) {
        data_.noMeterReleasePoint = QStringLiteral("Release");
    } else {
        data_.noMeterReleasePoint = QStringLiteral("Push");
    }
    data_.noMeterHandedness =
        data_.noMeterHandedness.trimmed().toLower().startsWith(QLatin1Char('l'))
            ? QStringLiteral("Left") : QStringLiteral("Right");
    if (data_.minimumHoldMs > data_.maximumHoldMs) {
        data_.minimumHoldMs = 100.0;
        data_.maximumHoldMs = 1000.0;
    }

    // Repair both legacy pose-only settings and contradictory dual-true files
    // after every top-level and nested setting has been parsed.
    data_.meterEnabled = true;
    data_.noMeterEnabled = false;

    // Tempo remains a persisted meter-timed input style. Pose-only no-meter authority
    // is deliberately repaired off above and cannot survive reconnect in production.
    normalizeRemotePlayBackend(data_, rootDir_);
}

void AppConfig::loadLearningObject(const QJsonObject& obj)
{
    learning_.version = obj.value(QStringLiteral("version")).toInt(learning_.version);
    learning_.emaFillPerFrame = obj.value(QStringLiteral("ema_fill_per_frame")).toDouble(learning_.emaFillPerFrame);
    learning_.emaGreenRatio = obj.value(QStringLiteral("ema_green_ratio")).toDouble(learning_.emaGreenRatio);
    learning_.biasPct = obj.value(QStringLiteral("bias_pct")).toDouble(learning_.biasPct);

    const auto perLevel = obj.value(QStringLiteral("per_level")).toObject();
    learning_.openNudgePct = perLevelNudge(perLevel, QStringLiteral("OPEN"));
    learning_.lightNudgePct = perLevelNudge(perLevel, QStringLiteral("LIGHT"));
    learning_.moderateNudgePct = perLevelNudge(perLevel, QStringLiteral("MODERATE"));
    learning_.heavyNudgePct = perLevelNudge(perLevel, QStringLiteral("HEAVY"));
    learning_.smotheredNudgePct = perLevelNudge(perLevel, QStringLiteral("SMOTHERED"));

    const auto typeLearned = obj.value(QStringLiteral("shot_type_learned_offset_ms")).toObject();
    for (auto it = typeLearned.constBegin(); it != typeLearned.constEnd(); ++it) {
        learning_.shotTypeLearnedOffsetMs.insert(it.key(), it.value().toDouble());
    }

    const auto typeFeedforward = obj.value(QStringLiteral("shot_type_feedforward_ms")).toObject();
    for (auto it = typeFeedforward.constBegin(); it != typeFeedforward.constEnd(); ++it) {
        learning_.shotTypeFeedforwardMs.insert(it.key(), it.value().toDouble());
    }

    const auto typeMeterClock = obj.value(QStringLiteral("shot_type_meter_to_release_ms")).toObject();
    for (auto it = typeMeterClock.constBegin(); it != typeMeterClock.constEnd(); ++it) {
        learning_.shotTypeMeterToReleaseMs.insert(it.key(), it.value().toDouble());
    }

    const auto typeAppearTip = obj.value(QStringLiteral("shot_type_appear_to_tip_ms")).toObject();
    for (auto it = typeAppearTip.constBegin(); it != typeAppearTip.constEnd(); ++it) {
        learning_.shotTypeAppearToTipMs.insert(it.key(), it.value().toDouble());
    }
    learning_.leadRebaselined = obj.value(QStringLiteral("lead_rebaselined")).toBool(learning_.leadRebaselined);

    // [ORION_PHASE_COLD_START] Bounded by the same plausibility band the learner itself
    // applies, so a hand-edited or corrupted file cannot install an absurd animation length.
    {
        const double v = obj.value(QStringLiteral("learned_phase_physical_ms")).toDouble(-1.0);
        learning_.learnedPhasePhysicalMs = (v >= 200.0 && v <= 500.0) ? v : -1.0;
    }
    // [ORION_AIM_FREEZE] Same plausibility band as the aim slot above; a value outside it
    // degrades to "never measured" rather than installing an absurd animation length.
    {
        const double v = obj.value(QStringLiteral("measured_phase_physical_ms")).toDouble(-1.0);
        learning_.measuredPhasePhysicalMs = (v >= 200.0 && v <= 500.0) ? v : -1.0;
    }
    const auto typeCalPhase = obj.value(QStringLiteral("shot_type_cal_phase")).toObject();
    for (auto it = typeCalPhase.constBegin(); it != typeCalPhase.constEnd(); ++it) {
        learning_.shotTypeCalPhase.insert(it.key(), it.value().toInt());
    }

    const auto typeRttBaseline = obj.value(QStringLiteral("shot_type_rtt_baseline_ms")).toObject();
    for (auto it = typeRttBaseline.constBegin(); it != typeRttBaseline.constEnd(); ++it) {
        learning_.shotTypeRttBaselineMs.insert(it.key(), it.value().toDouble());
    }

    const auto typeVelocityPrior = obj.value(QStringLiteral("shot_type_velocity_prior_pct_ms")).toObject();
    for (auto it = typeVelocityPrior.constBegin(); it != typeVelocityPrior.constEnd(); ++it) {
        learning_.shotTypeVelocityPriorPctMs.insert(it.key(), it.value().toDouble());
    }
    // Hybrid global phase-clock self-learned globals (autonomous_vision path).
    learning_.globalAppearToTipMs = obj.value(QStringLiteral("global_appear_to_tip_ms")).toDouble(learning_.globalAppearToTipMs);
    learning_.globalHoldToReleaseMs = obj.value(QStringLiteral("global_hold_to_release_ms")).toDouble(learning_.globalHoldToReleaseMs);
    learning_.learnedLatencyMs = obj.value(QStringLiteral("learned_latency_ms")).toDouble(learning_.learnedLatencyMs);
    learning_.probeSpawnOffsetMs = obj.value(QStringLiteral("probe_spawn_offset_ms")).toDouble(learning_.probeSpawnOffsetMs);
    learning_.globalRiseVelocityPctMs = obj.value(QStringLiteral("global_rise_velocity_pct_ms")).toDouble(learning_.globalRiseVelocityPctMs);
    // Per-shot-type latency residual (autonomous vision)
    const auto typeLatency = obj.value(QStringLiteral("shot_type_latency_ms")).toObject();
    for (auto it = typeLatency.constBegin(); it != typeLatency.constEnd(); ++it) {
        learning_.shotTypeLatencyMs.insert(it.key(), it.value().toDouble());
    }
}

} // namespace orion
