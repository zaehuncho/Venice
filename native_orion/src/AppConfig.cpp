#include "AppConfig.h"
#include "BannerLeadTrim.h"
#include "OrionPaths.h"
#include "RemotePlayExecutablePolicy.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDateTime>
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
#include <cstring>
#include <iterator>
#include <limits>

namespace orion {

namespace {
// [ORION_NO_METER_SHELVED 2026-09-15 owner] "Shelve the no meter path, we'll beef that up for a
// later update." This is the fence that makes that true of an INSTALL rather than only of the UI:
// load() and save() both force input_timed_enabled false while it is false, so a hand-edited
// settings.json, a file restored from a backup, or a settings.json written by yesterday's build
// with the mode selected all come up on the meter path. The UI half (RemotePlayPage's mode switch
// and the two NO METER cards) is unmounted, and the controller's setter refuses -- three
// independent doors, because a half-shelved mode that only the UI hides is exactly the state that
// ships a blind release to a customer who never chose one.
//
// NOTHING IS COMPILED OUT. The whole blind engine -- NO METER v2's hold law, the vision-assist
// hybrid, the console-frame quantiser, the per-type hold learner -- is still built, still tested,
// and still USED: the meter path's own blind backstop answers an unclaimed press through the same
// code. setInputTimedAllowedForTesting() lifts the fence so those tests drive the real path.
bool g_inputTimedAllowed = false;   // see AppConfig::inputTimedAllowed()

// [ORION_SPRINT_RELEASE_FENCED 2026-09-17 owner] The SECOND fence, built for the same reason and
// on the same shape. sprint_release_on_square shipped ON for one evening, was refuted live on
// 2026-09-16 23:04 (every press carrying R2 went dead, 100 %), and its compiled default was
// flipped to false the same night -- but a default is only the answer for a file that does not
// already have one. The owner's settings.json still carried the `true` the earlier session had
// persisted, the file won over the default, and 2026-09-17's acceptance session lost EVERY fade
// (9 firings, 0 fades answered) to a behaviour that had already been proven harmful.
//
// So the default is no longer the mechanism: load() and save() both force the key false while
// this is false, which means a persisted true, a restored backup and a hand edit all come up on
// the pad's own R2. The key itself keeps round-tripping (it is still read, still written, still
// in `introducedKeys`) so an existing settings.json stays valid and the day the mechanism is
// re-run it finds its own key where it left it.
//
// NOTHING IS COMPILED OUT. The shaping, its state machine and all six of its tests are still
// built and still tested; ORION_SPRINT_RELEASE_ON_SQUARE=1 (exact "1") is the ONLY door, and it
// is a dev A/B door -- the engine's own env override already honours it, so setting it lifts the
// fence on BOTH halves at once and the A and the B stay the same build.
bool g_sprintReleaseAllowed = false;   // see AppConfig::sprintReleaseAllowed()
}

bool AppConfig::inputTimedAllowed() noexcept { return g_inputTimedAllowed; }
void AppConfig::setInputTimedAllowedForTesting(bool allowed) noexcept { g_inputTimedAllowed = allowed; }

bool AppConfig::sprintReleaseAllowed()
{
    if (g_sprintReleaseAllowed) {
        return true;
    }
    // Ignore-don't-guess, exactly like the engine's own override: an EXACT "1" (trimmed) lifts the
    // fence and every other value -- "true", "yes", "0", a typo, an empty string -- leaves it up.
    return qEnvironmentVariable("ORION_SPRINT_RELEASE_ON_SQUARE").trimmed() == QLatin1String("1");
}

void AppConfig::setSprintReleaseAllowedForTesting(bool allowed) noexcept
{
    g_sprintReleaseAllowed = allowed;
}

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

// [2026-09-22 RED TEAM GM-002 / CX-001] readObject() cannot tell "no file" from "a file that is
// zero bytes, truncated or not JSON". For learning data that difference is the whole bug: a
// corrupt file loaded as {} became defaults, and the next save overwrote the customer's tuned
// profile with those defaults, silently. This variant reports which case it is.
enum class ObjectReadStatus { Missing, Invalid, Ok };

ObjectReadStatus readObjectStatus(const QString& path, QJsonObject* out)
{
    QFile file(path);
    if (!file.exists()) {
        return ObjectReadStatus::Missing;
    }
    if (!file.open(QIODevice::ReadOnly)) {
        return ObjectReadStatus::Invalid;
    }
    const QByteArray raw = file.readAll();
    if (raw.trimmed().isEmpty()) {
        return ObjectReadStatus::Invalid;
    }
    const auto doc = QJsonDocument::fromJson(raw);
    if (!doc.isObject()) {
        return ObjectReadStatus::Invalid;
    }
    if (out) {
        *out = doc.object();
    }
    return ObjectReadStatus::Ok;
}

// [RT-MED-03 / CL3-F4-006 2026-09-23] SEMANTIC bands for learning.json. The syntactic guard
// (readObjectStatus) only proves the file is a JSON object; a finite but absurd value --
// "bias_pct": -60, a negative clock, 1e300 -- used to load and then rotate into .bak as
// "last good". Every numeric field now has a plausibility band, taken from the band the
// engine's own learner or clamp enforces (widened a little so no value a shipped writer can
// produce is refused). 0 stays legal wherever the engine reads 0 as "unarmed / not learned".
// ONE table feeds both the per-field loader (an out-of-band value keeps the default) and the
// whole-file validator (any violation quarantines the file and prefers the last-good copy).
struct LearningBand {
    const char* key;
    double lo;
    double hi;
};

// Scalars at the top level of learning.json.
constexpr LearningBand kLearningScalarBands[] = {
    {"version", 0.0, 1000.0},
    {"ema_fill_per_frame", 0.0, 100.0},        // pct per frame; no shipped writer (0)
    {"ema_green_ratio", 0.0, 1.0},             // a ratio
    {"bias_pct", -10.0, 10.0},                 // aim target = clampPct(100 + bias); no writer (0)
    {"global_appear_to_tip_ms", 0.0, 3000.0},  // learner band (80, 2000) + a 200 ms rebaseline
    {"global_hold_to_release_ms", 0.0, 6000.0},// learner band (150, 5000) + rebaseline
    {"learned_latency_ms", -130.0, 130.0},     // clamp +-globalLatencyClampMs (100; was 130)
    {"probe_spawn_offset_ms", 0.0, 2000.0},    // press -> meter-appear game constant (178.2)
    {"global_rise_velocity_pct_ms", 0.0, 2.0}, // learner band (0.05, 1.0); default 0.226
};

// Per-shot-type maps ({"<type>": number}).
constexpr LearningBand kLearningMapBands[] = {
    {"shot_type_learned_offset_ms", -120.0, 120.0},     // learnFromOutcome clamp +-120
    {"shot_type_feedforward_ms", 0.0, 6000.0},          // seed band (150, 5000) + rebaseline
    {"shot_type_meter_to_release_ms", 0.0, 6000.0},     // seed band (80, 5000) + rebaseline
    {"shot_type_appear_to_tip_ms", 0.0, 3000.0},        // posthoc label band (150, 3000)
    {"shot_type_latency_ms", -130.0, 130.0},            // clamp +-shotTypeLatencyClampMs
    {"shot_type_velocity_prior_pct_ms", 0.0, 2.0},      // learner band (0.001, 2.0)
    {"shot_type_rtt_baseline_ms", -100.0, 100.0},       // pendingNetworkOffsetMs_ clamp +-100
    {"shot_type_cal_phase", 0.0, 2.0},                  // 0 = Acquire, 1 = Lock
};

constexpr double kLearningPhaseMinMs = 200.0;   // learned/measured phase + lead reference band
constexpr double kLearningPhaseMaxMs = 500.0;
constexpr double kNoMeterHoldMinMs = 200.0;     // no_meter_hold_by_type median band
constexpr double kNoMeterHoldMaxMs = 4000.0;
constexpr double kPerLevelNudgeAbsMax = 20.0;

bool learningNumberInBand(const QJsonValue& v, double lo, double hi)
{
    if (!v.isDouble()) {
        return false;
    }
    const double d = v.toDouble();
    return std::isfinite(d) && d >= lo && d <= hi;
}

double perLevelNudge(const QJsonObject& perLevel, const QString& key)
{
    const auto level = perLevel.value(key).toObject();
    // [RT-MED-03] An out-of-band nudge degrades to 0 (no nudge), never to an arbitrary aim.
    const QJsonValue v = level.value(QStringLiteral("nudge_pct"));
    return learningNumberInBand(v, -kPerLevelNudgeAbsMax, kPerLevelNudgeAbsMax) ? v.toDouble()
                                                                                : 0.0;
}

QString defaultChiakiPath(const QString& rootDir)
{
#ifdef ORION_PRODUCTION_BUILD
    Q_UNUSED(rootDir);
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
    // Repair the legacy pose-only mode flags independently of inputTimedEnabled:
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

QString AppConfig::learningBackupPath() const
{
    return learningPath() + QStringLiteral(".bak");
}

QStringList AppConfig::learningSemanticViolations(const QJsonObject& obj)
{
    // [RT-MED-03 / CL3-F4-006 2026-09-23] Every field that is PRESENT must be a finite number
    // inside its band (see kLearningScalarBands / kLearningMapBands). Absent fields are fine:
    // an older writer, or a value that was never learned. The result names each violation as
    // "key" or "key[type]" so the quarantine note says what was wrong without printing values.
    QStringList violations;
    for (const LearningBand& band : kLearningScalarBands) {
        const QString key = QString::fromLatin1(band.key);
        if (obj.contains(key) && !learningNumberInBand(obj.value(key), band.lo, band.hi)) {
            violations.append(key);
        }
    }
    for (const LearningBand& band : kLearningMapBands) {
        const QString key = QString::fromLatin1(band.key);
        if (!obj.contains(key)) {
            continue;
        }
        const QJsonValue mapValue = obj.value(key);
        if (!mapValue.isObject()) {
            violations.append(key);
            continue;
        }
        const QJsonObject map = mapValue.toObject();
        for (auto it = map.constBegin(); it != map.constEnd(); ++it) {
            if (!learningNumberInBand(it.value(), band.lo, band.hi)) {
                violations.append(QStringLiteral("%1[%2]").arg(key, it.key().left(48)));
            }
        }
    }
    if (obj.contains(QStringLiteral("lead_rebaselined"))
        && !obj.value(QStringLiteral("lead_rebaselined")).isBool()) {
        violations.append(QStringLiteral("lead_rebaselined"));
    }
    if (obj.contains(QStringLiteral("per_level"))) {
        const QJsonValue perLevelValue = obj.value(QStringLiteral("per_level"));
        if (!perLevelValue.isObject()) {
            violations.append(QStringLiteral("per_level"));
        } else {
            const QJsonObject perLevel = perLevelValue.toObject();
            for (auto it = perLevel.constBegin(); it != perLevel.constEnd(); ++it) {
                const QJsonObject level = it.value().toObject();
                if (!it.value().isObject()
                    || (level.contains(QStringLiteral("nudge_pct"))
                        && !learningNumberInBand(level.value(QStringLiteral("nudge_pct")),
                                                 -kPerLevelNudgeAbsMax, kPerLevelNudgeAbsMax))) {
                    violations.append(QStringLiteral("per_level[%1]").arg(it.key().left(48)));
                }
            }
        }
    }
    for (const char* phaseKey : {"learned_phase_physical_ms", "measured_phase_physical_ms"}) {
        const QString key = QString::fromLatin1(phaseKey);
        if (obj.contains(key)
            && !learningNumberInBand(obj.value(key), kLearningPhaseMinMs, kLearningPhaseMaxMs)) {
            violations.append(key);
        }
    }
    {
        // The writer emits the lead-reference pair together or not at all.
        const bool hasPhys = obj.contains(QStringLiteral("lead_reference_physical_ms"));
        const bool hasLead = obj.contains(QStringLiteral("lead_reference_lead_ms"));
        if (hasPhys != hasLead
            || (hasPhys
                && (!learningNumberInBand(obj.value(QStringLiteral("lead_reference_physical_ms")),
                                          kLearningPhaseMinMs, kLearningPhaseMaxMs)
                    || !learningNumberInBand(obj.value(QStringLiteral("lead_reference_lead_ms")),
                                             1e-9, 1000.0)))) {
            violations.append(QStringLiteral("lead_reference"));
        }
    }
    if (obj.contains(QStringLiteral("no_meter_hold_by_type"))) {
        const QJsonValue holdValue = obj.value(QStringLiteral("no_meter_hold_by_type"));
        if (!holdValue.isObject()) {
            violations.append(QStringLiteral("no_meter_hold_by_type"));
        } else {
            const QJsonObject holds = holdValue.toObject();
            for (auto it = holds.constBegin(); it != holds.constEnd(); ++it) {
                const QJsonObject rec = it.value().toObject();
                if (!it.value().isObject()
                    || !learningNumberInBand(rec.value(QStringLiteral("median_ms")),
                                             kNoMeterHoldMinMs, kNoMeterHoldMaxMs)
                    || !learningNumberInBand(rec.value(QStringLiteral("n")), 1.0, 1.0e6)) {
                    violations.append(
                        QStringLiteral("no_meter_hold_by_type[%1]").arg(it.key().left(48)));
                }
            }
        }
    }
    if (obj.contains(QStringLiteral("banner_lead_trim_by_type"))) {
        const QJsonValue trimValue = obj.value(QStringLiteral("banner_lead_trim_by_type"));
        if (!trimValue.isObject()) {
            violations.append(QStringLiteral("banner_lead_trim_by_type"));
        } else {
            const QJsonObject trims = trimValue.toObject();
            for (auto it = trims.constBegin(); it != trims.constEnd(); ++it) {
                if (!learningNumberInBand(it.value(), -BannerLeadTrim::kPersistCeilingMs,
                                          BannerLeadTrim::kPersistCeilingMs)) {
                    violations.append(
                        QStringLiteral("banner_lead_trim_by_type[%1]").arg(it.key().left(48)));
                }
            }
        }
    }
    return violations;
}

void AppConfig::reloadLearning()
{
    learning_ = LearningData{};
    learningLoadNote_.clear();
    QJsonObject learningJson;
    const ObjectReadStatus status = readObjectStatus(learningPath(), &learningJson);
    if (status == ObjectReadStatus::Ok) {
        const QStringList violations = learningSemanticViolations(learningJson);
        if (violations.isEmpty()) {
            if (!learningJson.isEmpty()) {
                loadLearningObject(learningJson);
            }
            return;
        }
        // [RT-MED-03 / CL3-F4-006 2026-09-23] Valid JSON, absurd values. Same fail-closed shape
        // as the unreadable case below: keep the evidence, prefer the last-good copy -- but only
        // a last-good copy that ALSO passes the semantic check. Without one, the in-band fields
        // of the file still load (the loader drops every out-of-band value to its default), so
        // one bad key cannot cost the customer every other thing this install has learned.
        const QString quarantine = learningPath() + QStringLiteral(".invalid-")
            + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
        QFile::rename(learningPath(), quarantine);
        QJsonObject backup;
        if (readObjectStatus(learningBackupPath(), &backup) == ObjectReadStatus::Ok
            && !backup.isEmpty() && learningSemanticViolations(backup).isEmpty()) {
            loadLearningObject(backup);
            QFile::copy(learningBackupPath(), learningPath());
            learningLoadNote_ = QStringLiteral(
                "learning.json had out-of-range values (%1); restored the previous good copy from "
                "learning.json.bak (the rejected file was kept as %2).")
                .arg(violations.mid(0, 8).join(QStringLiteral(", ")),
                     QFileInfo(quarantine).fileName());
        } else {
            loadLearningObject(learningJson);
            learningLoadNote_ = QStringLiteral(
                "learning.json had out-of-range values (%1) and no valid backup; those values were "
                "reset to defaults and the rest was kept (the rejected file was kept as %2).")
                .arg(violations.mid(0, 8).join(QStringLiteral(", ")),
                     QFileInfo(quarantine).fileName());
        }
        qWarning().noquote() << learningLoadNote_;
        return;
    }
    if (status == ObjectReadStatus::Missing) {
        return;   // a true first run: defaults are correct
    }
    // The file exists but cannot be read. Keep the evidence, try the last-good copy, and never
    // let a later save overwrite the corrupt original as if it had been an empty first run.
    const QString quarantine = learningPath() + QStringLiteral(".corrupt-")
        + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    QFile::rename(learningPath(), quarantine);
    QJsonObject backup;
    // [RT-MED-03] A backup that is readable but out of band is no "last good" either.
    if (readObjectStatus(learningBackupPath(), &backup) == ObjectReadStatus::Ok && !backup.isEmpty()
        && learningSemanticViolations(backup).isEmpty()) {
        loadLearningObject(backup);
        QFile::copy(learningBackupPath(), learningPath());
        learningLoadNote_ = QStringLiteral(
            "learning.json was unreadable; restored the previous good copy from learning.json.bak "
            "(the damaged file was kept as %1).").arg(QFileInfo(quarantine).fileName());
    } else {
        learningLoadNote_ = QStringLiteral(
            "learning.json was unreadable and no backup existed; learned timing starts from defaults "
            "(the damaged file was kept as %1).").arg(QFileInfo(quarantine).fileName());
    }
    qWarning().noquote() << learningLoadNote_;
}

const QList<AppConfig::SettingsMigration>& AppConfig::settingsMigrations()
{
    // THE ALLOWLIST. One entry = one key = one deliberate, evidenced decision.
    // Keep entries in ascending targetVersion order; never renumber or edit a
    // shipped entry (idempotency across installs depends on the history being
    // append-only).
    //
    // v1 exists to install the settings_version stamp itself; its one candidate
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
    // [ORION_FREEZE_RIDES_RELEASE 2026-09-01] v2 is a fail-closed correction,
    // not a timing retune.  Older builds saved the then-default true value into
    // settings.json even when the user never selected it.  The instrument is now
    // proven to measure the release landing rather than the animation tip, so an
    // automatic unlock can move an already-early aim farther in the wrong
    // direction.  There was no *_user_set companion in the old schema; false is
    // therefore the safe upgrade value.  A user may explicitly opt back in after
    // the v2 stamp, and current-version files are never migrated again.
    static const QList<SettingsMigration> registry = {
        {2,
         QStringLiteral("tip_timing_auto_unlock"),
         QJsonValue(true),
         QJsonValue(false),
         QString{},
         QStringLiteral("anchor-to-freeze measures the release landing; legacy auto-unlock can move the aim in the wrong direction")},
    };
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

    // [2026-09-22 GM-002 / CX-001] one loader: reloadLearning() distinguishes a missing file from
    // a damaged one, restores the last-good copy and records what it did (learningLoadNote()).
    reloadLearning();

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
    // [ORION_NO_METER_V2 2026-09-14] Retired from the timing math; still normalised and written
    // so an existing file round-trips unchanged (see AppConfigData).
    data.inputTimedDelayMs = std::isfinite(data.inputTimedDelayMs)
        ? std::clamp(data.inputTimedDelayMs, 100.0, 2500.0) : 500.0;
    data.inputTimedLeadMs = std::isfinite(data.inputTimedLeadMs)
        ? std::clamp(data.inputTimedLeadMs, 150.0, 400.0) : 272.0;
    // [ORION_NO_METER_V2 2026-09-14 owner] THE blind hold. Clamped to the slider's own band on
    // the way out too, so no write path can persist a hold the UI could not have produced — the
    // 500 ms floor is the pump-fake guard's first line.
    data.noMeterHoldMs = std::isfinite(data.noMeterHoldMs)
        ? std::clamp(data.noMeterHoldMs, AppConfigData::kNoMeterHoldMinMs,
                     AppConfigData::kNoMeterHoldMaxMs)
        : 650.0;
    // [ORION_NO_METER_FADE_TRIM 2026-09-14 owner] Same policy as the hold: clamp on the way out
    // so no write path persists a trim the slider could not have produced.
    data.noMeterFadeTrimMs = std::isfinite(data.noMeterFadeTrimMs)
        ? std::clamp(data.noMeterFadeTrimMs, AppConfigData::kNoMeterFadeTrimMinMs,
                     AppConfigData::kNoMeterFadeTrimMaxMs)
        : 0.0;
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] Same policy as the hold: the grid is
    // clamped on the way out too, so no write path can persist a frame period the engine would
    // then have to re-clamp (a zero or a NaN here would divide the whole blind law by nothing).
    data.consoleFrameMs = clampedConsoleFrameMs(data.consoleFrameMs);
    // [ORION_LATE_FIRE_TOLERANCE 2026-09-14] Same policy: no write path may persist a tolerance
    // outside the band, because the ceiling is what keeps a "late" release a shot.
    data.lateFireToleranceMs = std::isfinite(data.lateFireToleranceMs)
        ? std::clamp(data.lateFireToleranceMs, AppConfigData::kLateFireToleranceMinMs,
                     AppConfigData::kLateFireToleranceMaxMs)
        : 24.0;
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] Same policy: no write path may persist a step or a
    // clamp outside the band, because the clamp is what keeps the banner loop a trim rather than
    // a second, unaccountable lead control.
    data.bannerTrimStepMs = std::isfinite(data.bannerTrimStepMs)
        ? std::clamp(data.bannerTrimStepMs, AppConfigData::kBannerTrimStepMinMs,
                     AppConfigData::kBannerTrimStepMaxMs)
        : 3.0;
    data.bannerTrimMaxMs = std::isfinite(data.bannerTrimMaxMs)
        ? std::clamp(data.bannerTrimMaxMs, AppConfigData::kBannerTrimMaxMinMs,
                     AppConfigData::kBannerTrimMaxMaxMs)
        : 15.0;
    // [ORION_BANNER_TRIM_HOLD 2026-09-16] Same policy again: no write path may persist a hold
    // outside the band, because a hold under four would let one good run retire a trim the
    // verdicts had just earned -- which is the behaviour this setting exists to end.
    data.bannerTrimHoldShots = std::clamp(data.bannerTrimHoldShots,
                                          AppConfigData::kBannerTrimHoldShotsMin,
                                          AppConfigData::kBannerTrimHoldShotsMax);
    // [ORION_BANNER_TRIM_BIAS 2026-09-19] Same policy again, for the same reason: the two limits
    // are what keep the integrator a bounded second opinion rather than a per-verdict stepper,
    // so no write path may persist a window or a vote margin outside its band.
    data.bannerTrimBiasWindow = std::clamp(data.bannerTrimBiasWindow,
                                           AppConfigData::kBannerTrimBiasWindowMin,
                                           AppConfigData::kBannerTrimBiasWindowMax);
    data.bannerTrimBiasVotes = std::clamp(data.bannerTrimBiasVotes,
                                          AppConfigData::kBannerTrimBiasVotesMin,
                                          AppConfigData::kBannerTrimBiasVotesMax);
    // [ORION_ONSET_FF 2026-09-21] Same policy: no write path may persist a gain, clamp, window
    // or sample floor outside its band, and a non-finite magnitude falls back to the compiled
    // default rather than to something a clamp would invent.
    data.onsetFeedforwardGain = std::isfinite(data.onsetFeedforwardGain)
        ? std::clamp(data.onsetFeedforwardGain, AppConfigData::kOnsetFeedforwardGainMin,
                     AppConfigData::kOnsetFeedforwardGainMax)
        : 0.2;
    data.onsetFeedforwardClampMs = std::isfinite(data.onsetFeedforwardClampMs)
        ? std::clamp(data.onsetFeedforwardClampMs, AppConfigData::kOnsetFeedforwardClampMinMs,
                     AppConfigData::kOnsetFeedforwardClampMaxMs)
        : 10.0;
    data.onsetFeedforwardWindow = std::clamp(data.onsetFeedforwardWindow,
                                             AppConfigData::kOnsetFeedforwardWindowMin,
                                             AppConfigData::kOnsetFeedforwardWindowMax);
    data.onsetFeedforwardMinSamples = std::clamp(data.onsetFeedforwardMinSamples,
                                                 AppConfigData::kOnsetFeedforwardMinSamplesMin,
                                                 AppConfigData::kOnsetFeedforwardMinSamplesMax);
    // [ORION_LEAD_OFFSET_BY_TYPE 2026-09-16] Same policy, and for the same reason the banner
    // trim's clamp exists: no write path may persist a per-type offset outside the band, because
    // the band is what keeps this a per-type correction rather than a second Shot Lead.
    data.leadOffsetLeftFadeMs = clampedLeadOffsetMs(data.leadOffsetLeftFadeMs, -6.0);
    data.leadOffsetRightFadeMs = clampedLeadOffsetMs(data.leadOffsetRightFadeMs, 8.0);
    data.leadOffsetStandstillMs = clampedLeadOffsetMs(data.leadOffsetStandstillMs, 0.0);
    data.leadOffsetOtherMs = clampedLeadOffsetMs(data.leadOffsetOtherMs, 0.0);
    // [ORION_LEAD_OFFSET_FADE_MID 2026-09-17] Same policy, same band as the other four.
    data.leadOffsetFadeMidMs = clampedLeadOffsetMs(data.leadOffsetFadeMidMs, 6.0);
    // [ORION_LEAD_AUTO_SEED 2026-09-15] Same policy: no write path may persist an aim margin or a
    // placeholder outside its band. The margin's ceiling is what keeps it a margin rather than a
    // second Shot Lead, and the placeholder's band is the slider's own, so the auto seed is
    // always a value the control could have produced.
    data.aimMarginMs = std::isfinite(data.aimMarginMs)
        ? std::clamp(data.aimMarginMs, AppConfigData::kAimMarginMinMs,
                     AppConfigData::kAimMarginMaxMs)
        : 69.0;
    data.leadFactoryPlaceholderMs = std::isfinite(data.leadFactoryPlaceholderMs)
        ? std::clamp(data.leadFactoryPlaceholderMs,
                     AppConfigData::kLeadFactoryPlaceholderMinMs,
                     AppConfigData::kLeadFactoryPlaceholderMaxMs)
        : 269.0;
    // [ORION_METER_BACKSTOP_GRACE 2026-09-15] Same policy: no write path may persist a grace
    // outside the band, because the ceiling is what keeps the backstop inside the shot it answers.
    data.meterBackstopGraceMs = std::isfinite(data.meterBackstopGraceMs)
        ? std::clamp(data.meterBackstopGraceMs, AppConfigData::kMeterBackstopGraceMinMs,
                     AppConfigData::kMeterBackstopGraceMaxMs)
        : 100.0;
    // [ORION_METER_BACKSTOP_GRACE_FADE 2026-09-15] Same policy, its own band.
    data.meterBackstopGraceFadeMs = std::isfinite(data.meterBackstopGraceFadeMs)
        ? std::clamp(data.meterBackstopGraceFadeMs, AppConfigData::kMeterBackstopGraceFadeMinMs,
                     AppConfigData::kMeterBackstopGraceFadeMaxMs)
        : 220.0;
    // [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-16] Same policy, its own band: no write path may
    // persist a probe outside it, because the ceiling is what keeps the question ("has ANYTHING
    // been seen yet?") from being asked before a normal meter could have been drawn.
    data.meterBackstopNeverSeenProbeMs = std::isfinite(data.meterBackstopNeverSeenProbeMs)
        ? std::clamp(data.meterBackstopNeverSeenProbeMs,
                     AppConfigData::kMeterBackstopNeverSeenProbeMinMs,
                     AppConfigData::kMeterBackstopNeverSeenProbeMaxMs)
        : 0.0;   // [2026-09-17] the compiled default, which is now the collapse OFF
    // [ORION_METER_BACKSTOP_NEVER_SEEN_FADE 2026-09-16] Same policy, its own band.
    data.meterBackstopNeverSeenProbeFadeMs =
        std::isfinite(data.meterBackstopNeverSeenProbeFadeMs)
        ? std::clamp(data.meterBackstopNeverSeenProbeFadeMs,
                     AppConfigData::kMeterBackstopNeverSeenProbeFadeMinMs,
                     AppConfigData::kMeterBackstopNeverSeenProbeFadeMaxMs)
        : 0.0;
    // [ORION_VISION_HOLD_BAND 2026-09-15] Same policy again, per band: no write path may persist
    // a band outside it, because the ceiling is what keeps the band from binding on a real hold
    // and the floor IS the kill switch.
    data.visionHoldBandMs = std::isfinite(data.visionHoldBandMs)
        ? std::clamp(data.visionHoldBandMs, AppConfigData::kVisionHoldBandMinMs,
                     AppConfigData::kVisionHoldBandMaxMs)
        : 40.0;
    data.visionHoldBandFadeMs = std::isfinite(data.visionHoldBandFadeMs)
        ? std::clamp(data.visionHoldBandFadeMs, AppConfigData::kVisionHoldBandFadeMinMs,
                     AppConfigData::kVisionHoldBandFadeMaxMs)
        : 60.0;
    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] An unrecognised style is replaced by the shipped
    // one on the way out, so a hand-edited file can never persist a value the engine would have
    // to guess at. "flick" and "letgo" are the whole vocabulary.
    if (data.tempoReleaseStyle != QLatin1String("flick")
        && data.tempoReleaseStyle != QLatin1String("letgo")) {
        data.tempoReleaseStyle = QStringLiteral("flick");
    }
    // [ORION_SPRINT_RELEASE_FENCED 2026-09-17 owner] The write half of the fence. No route may
    // persist a true while the feature is fenced, so the file on disk agrees with the install
    // rather than carrying a selection nothing will honour -- and, crucially, so the ONE write
    // the app makes on any other setting also scrubs the true the refuted session left behind.
    // The key keeps being written (false), so the file round-trips and stays valid.
    if (!sprintReleaseAllowed()) {
        data.sprintReleaseOnSquare = false;
    }
    // [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16] Same clamp-on-every-route policy the timing
    // knobs use: no write path may persist a sprint cut outside the band, because the floor is
    // what keeps an ordinary walk from being reshaped and the ceiling is the saturated trigger
    // the owner's dead presses actually reported.
    data.sprintReleaseR2Threshold = std::clamp(data.sprintReleaseR2Threshold,
                                               AppConfigData::kSprintReleaseR2ThresholdMin,
                                               AppConfigData::kSprintReleaseR2ThresholdMax);
    // [R2_HOLD_DEFAULT_INERT 2026-09-21] [R2_HOLD_PERSISTED_FENCE] The write half of the
    // pass-through fence. A prior build may have persisted the old implicit 50 ms value; merely
    // changing the struct default would let that stale file keep inventing R2 holds forever.
    // Scrub the hidden setting to zero. A diagnostic A/B remains available only through the
    // explicit ORION_SQUARE_PRESS_R2_HOLD_MS process environment override.
    data.squarePressR2HoldMs = 0.0;
    data.meterEnabled = true;
    data.noMeterEnabled = false;
    normalizeRemotePlayBackend(data, rootDir_);
    // [ORION_LEAD_BY_SOURCE 2026-09-14] THE chokepoint for the per-route stash invariant: every
    // path that writes the live Shot Lead (setActuationLeadMs, nudge, lead-calibration steps,
    // the measured seed, reset, a Venice profile import) ends up here, so mirroring once here
    // means no write path can forget. The individual setters mirror explicitly as well; this is
    // the guarantee that survives someone adding a seventh write path.
    mirrorActuationLeadIntoSourceStash(data);
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
    // Normalised on SAVE as well as on load: the generic save API must not be able to
    // persist a raw withdrawn style even if a caller hands it unsanitised data (Astra).
    obj.insert(QStringLiteral("meter_style"), normalizedMeterStyle(data.meterStyle));
    obj.insert(QStringLiteral("meter_proposer"), normalizedMeterProposer(data.meterProposer));
    // [ORION_PILL_YOLO_ROUTE 2026-09-17] Written on every save so the key exists in
    // an installed settings.json and an owner can take the route down by hand.
    obj.insert(QStringLiteral("pill_yolo_route"), data.pillYoloRoute);
    obj.insert(QStringLiteral("remote_play_client_mode"), QStringLiteral("chiaki"));
    obj.insert(QStringLiteral("remote_play_console"), data.remotePlayConsole);
    if (!data.xboxRemotePlayWindowTitle.isEmpty())
        obj.insert(QStringLiteral("xbox_remote_play_window_title"), data.xboxRemotePlayWindowTitle);
    obj.insert(QStringLiteral("xbox_untested_ack"), data.xboxUntestedAcknowledged);
    obj.insert(QStringLiteral("stream_setup_complete"), data.streamSetupComplete);
    obj.insert(QStringLiteral("preflight_complete"), data.preflightComplete);
    obj.insert(QStringLiteral("legal_accepted_version"), data.legalAcceptedVersion);
    obj.insert(QStringLiteral("video_source"), data.videoSource);
    obj.insert(QStringLiteral("capture_card_index"), data.captureCardIndex);
    obj.insert(QStringLiteral("capture_card_device_id"), data.captureCardDeviceId);
    // [ORION_CAPTURE_FPS 2026-09-14] Persisted already-snapped, so the file never carries a rate
    // the card was not actually asked for.
    obj.insert(QStringLiteral("capture_card_fps"), snappedCaptureCardFps(data.captureCardFps));
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
    obj.insert(QStringLiteral("tip_phase_anchor_consensus"), data.tipPhaseAnchorConsensus);
    obj.insert(QStringLiteral("tip_phase_first_sight_anchor"), data.tipPhaseFirstSightAnchor);
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
    // [ORION_TIP_FRAME_NATIVE 2026-09-15] fire on the console frame grid (default true).
    obj.insert(QStringLiteral("tip_frame_native"), data.tipFrameNative);
    obj.insert(QStringLiteral("tip_phase_aim_frozen"), data.tipPhaseAimFrozen);
    obj.insert(QStringLiteral("tip_timing_auto_unlock"), data.tipTimingAutoUnlockEnabled);
    obj.insert(QStringLiteral("session_lead_probe"), data.sessionLeadProbeEnabled);
    obj.insert(QStringLiteral("tip_source_steal_guard"), data.tipSourceStealGuardEnabled);
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
    obj.insert(QStringLiteral("meter_settle_allow_smooth_motion"), data.meterSettleAllowSmoothMotion);
    obj.insert(QStringLiteral("square_passthrough_button"), data.squarePassthroughButton);
    // [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16 owner] see AppConfigData::sprintReleaseOnSquare.
    // [ORION_SPRINT_RELEASE_FENCED 2026-09-17] `data` was already forced false above while the
    // feature is fenced, so this writes false without knowing why.
    obj.insert(QStringLiteral("sprint_release_on_square"), data.sprintReleaseOnSquare);
    obj.insert(QStringLiteral("sprint_release_r2_threshold"), data.sprintReleaseR2Threshold);
    // [ORION_SQUARE_PRESS_R2_HOLD 2026-09-16 owner] see AppConfigData::squarePressR2HoldMs
    obj.insert(QStringLiteral("square_press_r2_hold_ms"), data.squarePressR2HoldMs);
    obj.insert(QStringLiteral("no_dip_enabled"), data.noDipEnabled);
    obj.insert(QStringLiteral("no_dip_lead_ms"), data.noDipLeadMs);
    // [ORION_USER_LEAD] the user-facing Shot Lead + whether the USER (not the measurement) set it.
    obj.insert(QStringLiteral("actuation_lead_ms"), data.actuationLeadMs);
    obj.insert(QStringLiteral("actuation_lead_user_set"), data.actuationLeadUserSet);
    // [ORION_LEAD_BY_SOURCE 2026-09-14] ONE key holds both routes' leads:
    //   "actuation_lead_by_source": {"capture_card": {"lead_ms": 274, "user_set": true}, ...}
    // An absent route = never configured there (see AppConfigData::actuationLeadBySourceMs).
    // actuation_lead_ms / actuation_lead_user_set remain the canonical LIVE pair the engine and
    // the Venice profile read; this object is only the memory for the route not in use.
    {
        QJsonObject leadBySource;
        for (auto it = data.actuationLeadBySourceMs.constBegin();
             it != data.actuationLeadBySourceMs.constEnd(); ++it) {
            QJsonObject entry;
            entry.insert(QStringLiteral("lead_ms"), it.value());
            entry.insert(QStringLiteral("user_set"),
                         data.actuationLeadUserSetBySource.value(it.key(), false));
            leadBySource.insert(it.key(), entry);
        }
        obj.insert(QStringLiteral("actuation_lead_by_source"), leadBySource);
    }
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
    // [ORION_NO_METER_SHELVED 2026-09-15] Written false while the mode is shelved, so the file on
    // disk agrees with the install rather than carrying a selection nothing will honour. The key
    // itself keeps being written: an existing file must still round-trip, and the day the mode
    // comes back it must find its own key where it left it.
    obj.insert(QStringLiteral("input_timed_enabled"),
               data.inputTimedEnabled && inputTimedAllowed());
    obj.insert(QStringLiteral("input_timed_delay_ms"), data.inputTimedDelayMs);
    obj.insert(QStringLiteral("input_timed_lead_ms"), data.inputTimedLeadMs);
    obj.insert(QStringLiteral("input_timed_rhythm_enabled"), data.inputTimedRhythmEnabled);
    // [ORION_NO_METER_V2 2026-09-14] the blind-release reference hold (ms).
    obj.insert(QStringLiteral("no_meter_hold_ms"), data.noMeterHoldMs);
    // [ORION_NO_METER_FADE_TRIM 2026-09-14] the fade-only trim (ms), added to both fade deltas.
    obj.insert(QStringLiteral("no_meter_fade_trim_ms"), data.noMeterFadeTrimMs);
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14] the console's input-sampling period (ms) and the
    // switch that snaps every blind hold onto it. Persisted already-clamped.
    obj.insert(QStringLiteral("console_frame_ms"), clampedConsoleFrameMs(data.consoleFrameMs));
    obj.insert(QStringLiteral("no_meter_frame_quantize"), data.noMeterFrameQuantize);
    // [ORION_NO_METER_VISION_ASSIST 2026-09-14] let VISION own a NO METER shot whenever it can
    // see the meter; the blind hold stays as the deadline.
    obj.insert(QStringLiteral("no_meter_vision_assist"), data.noMeterVisionAssist);
    // [ORION_LATE_FIRE_TOLERANCE 2026-09-14] how late the vision path may still fire (ms).
    obj.insert(QStringLiteral("late_fire_tolerance_ms"), data.lateFireToleranceMs);
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] the closed loop from the game's own TIMING banner back
    // onto the Shot Lead: the switch, the per-verdict step and the clamp. The trim itself lives
    // in learning.json (banner_lead_trim_by_type) -- it is evidence, not a setting.
    obj.insert(QStringLiteral("banner_lead_trim"), data.bannerLeadTrim);
    obj.insert(QStringLiteral("banner_trim_step_ms"), data.bannerTrimStepMs);
    obj.insert(QStringLiteral("banner_trim_max_ms"), data.bannerTrimMaxMs);
    // [ORION_BANNER_TRIM_HOLD 2026-09-16] How many consecutive EXCELLENT/GREEN verdicts hold the
    // trim before it idles back toward the slider. Persisted already-clamped.
    obj.insert(QStringLiteral("banner_trim_hold_shots"),
               std::clamp(data.bannerTrimHoldShots, AppConfigData::kBannerTrimHoldShotsMin,
                          AppConfigData::kBannerTrimHoldShotsMax));
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16] Whether the trim is keyed by (shot type, tempo) or by
    // shot type alone. A plain switch, like banner_lead_trim itself.
    obj.insert(QStringLiteral("banner_trim_tempo_buckets"), data.bannerTrimTempoBuckets);
    // [ORION_BANNER_TRIM_RANGE 2026-09-17] Whether FADES also key on the shot's range.
    obj.insert(QStringLiteral("banner_trim_range_buckets"), data.bannerTrimRangeBuckets);
    // [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] Whether a panel with NO coverage cell (the 2-cell
    // TIMING | DISTANCE layout: no defender context) calibrates the trim as an open shot does.
    obj.insert(QStringLiteral("banner_trim_absent_coverage_open"),
               data.bannerTrimAbsentCoverageOpen);
    // [ORION_BANNER_TRIM_BIAS 2026-09-19] The net-vote integrator's window and margin. Persisted
    // already-clamped, exactly like banner_trim_hold_shots.
    obj.insert(QStringLiteral("banner_trim_bias_window"),
               std::clamp(data.bannerTrimBiasWindow, AppConfigData::kBannerTrimBiasWindowMin,
                          AppConfigData::kBannerTrimBiasWindowMax));
    obj.insert(QStringLiteral("banner_trim_bias_votes"),
               std::clamp(data.bannerTrimBiasVotes, AppConfigData::kBannerTrimBiasVotesMin,
                          AppConfigData::kBannerTrimBiasVotesMax));
    // [ORION_ONSET_FF 2026-09-21] Persisted already-clamped, like the trim limits above.
    obj.insert(QStringLiteral("onset_ff_gain"),
               std::clamp(data.onsetFeedforwardGain, AppConfigData::kOnsetFeedforwardGainMin,
                          AppConfigData::kOnsetFeedforwardGainMax));
    obj.insert(QStringLiteral("onset_ff_clamp_ms"),
               std::clamp(data.onsetFeedforwardClampMs,
                          AppConfigData::kOnsetFeedforwardClampMinMs,
                          AppConfigData::kOnsetFeedforwardClampMaxMs));
    obj.insert(QStringLiteral("onset_ff_window"),
               std::clamp(data.onsetFeedforwardWindow, AppConfigData::kOnsetFeedforwardWindowMin,
                          AppConfigData::kOnsetFeedforwardWindowMax));
    obj.insert(QStringLiteral("onset_ff_min_samples"),
               std::clamp(data.onsetFeedforwardMinSamples,
                          AppConfigData::kOnsetFeedforwardMinSamplesMin,
                          AppConfigData::kOnsetFeedforwardMinSamplesMax));
    obj.insert(QStringLiteral("onset_ff_one_sided"), data.onsetFeedforwardOneSided);
    // [ORION_LEAD_OFFSET_BY_TYPE 2026-09-16] The fixed per-shot-type addition to the Shot Lead,
    // in ms. Persisted already-clamped, exactly like console_frame_ms: the band is what keeps a
    // per-type correction from becoming a second lead control.
    obj.insert(QStringLiteral("lead_offset_left_fade_ms"),
               clampedLeadOffsetMs(data.leadOffsetLeftFadeMs, -6.0));
    // [ORION_LEFT_FADE_LATER 2026-09-22] Revision 2 = the value above is post-migration, so a +8 the
    // owner sets deliberately later is never rewritten again.
    obj.insert(QStringLiteral("lead_offset_left_fade_rev"), 2);
    obj.insert(QStringLiteral("lead_offset_right_fade_ms"),
               clampedLeadOffsetMs(data.leadOffsetRightFadeMs, 8.0));
    obj.insert(QStringLiteral("lead_offset_standstill_ms"),
               clampedLeadOffsetMs(data.leadOffsetStandstillMs, 0.0));
    obj.insert(QStringLiteral("lead_offset_other_ms"),
               clampedLeadOffsetMs(data.leadOffsetOtherMs, 0.0));
    // [ORION_LEAD_OFFSET_FADE_MID 2026-09-17] The mid-range fade's own offset.
    obj.insert(QStringLiteral("lead_offset_fade_mid_ms"),
               clampedLeadOffsetMs(data.leadOffsetFadeMidMs, 6.0));
    // [ORION_LEAD_AUTO_SEED 2026-09-15] the plug-and-play Shot Lead: the switch, the game-side
    // aim margin added to this rig's measured latency, and the placeholder used until that
    // latency is authoritative. actuation_lead_ms is NEVER written by any of them.
    obj.insert(QStringLiteral("lead_auto_seed"), data.leadAutoSeed);
    obj.insert(QStringLiteral("aim_margin_ms"), data.aimMarginMs);
    obj.insert(QStringLiteral("lead_factory_placeholder_ms"), data.leadFactoryPlaceholderMs);
    // [ORION_OWNED_METER_NEVER_ABORTS 2026-09-14] an owned, lead-validated shot fires at ANY
    // lateness rather than aborting; the tolerance above becomes the beyond_tolerance label.
    obj.insert(QStringLiteral("owned_meter_never_aborts"), data.ownedMeterNeverAborts);
    // [ORION_OWNERSHIP_PROOF_LENIENCY 2026-09-14] near the deadline, accept a same-candidate
    // ownership episode whose proof a geometry break restarted.
    obj.insert(QStringLiteral("ownership_proof_leniency"), data.ownershipProofLeniency);
    // [ORION_METER_BLIND_BACKSTOP 2026-09-14] the meter path's blind backstop deadline.
    obj.insert(QStringLiteral("meter_blind_backstop"), data.meterBlindBackstop);
    // [ORION_METER_BACKSTOP_GRACE 2026-09-15] how long that deadline waits past the law (ms).
    obj.insert(QStringLiteral("meter_backstop_grace_ms"), data.meterBackstopGraceMs);
    // [ORION_METER_BACKSTOP_GRACE_FADE 2026-09-15] the fade's own, larger grace.
    obj.insert(QStringLiteral("meter_backstop_grace_fade_ms"), data.meterBackstopGraceFadeMs);
    // [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-16] how long before the law the backstop asks
    // whether ANY candidate has been seen for this press (0 = off).
    obj.insert(QStringLiteral("meter_backstop_never_seen_probe_ms"),
               data.meterBackstopNeverSeenProbeMs);
    // [ORION_METER_BACKSTOP_NEVER_SEEN_FADE 2026-09-16] the fade's own probe (0 = fades excluded).
    obj.insert(QStringLiteral("meter_backstop_never_seen_probe_fade_ms"),
               data.meterBackstopNeverSeenProbeFadeMs);
    // [ORION_VISION_HOLD_BAND 2026-09-15] how far a VISION release may sit from the press-anchored
    // hold law before it is clamped back onto the band (ms; 0 = off), and the fade's own band.
    obj.insert(QStringLiteral("vision_hold_band_ms"), data.visionHoldBandMs);
    obj.insert(QStringLiteral("vision_hold_band_fade_ms"), data.visionHoldBandFadeMs);
    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] "flick" (opposing full-scale flick) or "letgo"
    // (drive the stick to neutral at the same instant).
    obj.insert(QStringLiteral("tempo_release_style"), data.tempoReleaseStyle);
    // [ORION_PRESS_ANCHORED_FALLBACK 2026-09-14 — RETIRED] written only so an existing file
    // round-trips; nothing reads it.
    obj.insert(QStringLiteral("press_anchored_fallback_enabled"), data.pressAnchoredFallbackEnabled);
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
    // [ORION_RHYTHM_FLICK_DELAY 2026-09-14] Written in BOTH layouts the other tempo keys use
    // (flat + the nested "tempo" object) so a file written by either generation round-trips.
    obj.insert(QStringLiteral("rhythm_flick_delay_ms"), data.rhythmFlickDelayMs);
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
    tempo.insert(QStringLiteral("rhythm_flick_delay_ms"), data.rhythmFlickDelayMs);
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
    // [ORION_SESSION_LEAD_PROBE] engine units (no base shift), plus the lead it belongs to.
    if (data.leadReferencePhysicalMs > 0.0 && data.leadReferenceLeadMs > 0.0) {
        obj.insert(QStringLiteral("lead_reference_physical_ms"), data.leadReferencePhysicalMs);
        obj.insert(QStringLiteral("lead_reference_lead_ms"), data.leadReferenceLeadMs);
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
    // [ORION_NO_METER_V2 2026-09-14] Per-shot-type press->release hold measured on the vision
    // path. {median_ms, n} per type: the blind law needs the count as much as the value, because
    // it only prefers a learned Δ once BOTH the type and Standstill carry real evidence.
    {
        QJsonObject nh;
        for (auto it = data.noMeterHoldByType.constBegin();
             it != data.noMeterHoldByType.constEnd(); ++it) {
            QJsonObject rec;
            rec.insert(QStringLiteral("median_ms"), it.value().medianMs);
            rec.insert(QStringLiteral("n"), it.value().n);
            nh.insert(it.key(), rec);
        }
        obj.insert(QStringLiteral("no_meter_hold_by_type"), nh);
    }
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] The banner loop's per-shot-type additive trim, in ms.
    // A plain number per bucket: unlike the hold there is no count to weigh, because the trim IS
    // the accumulated evidence and it decays 50 % at the next start regardless of how it got
    // there. Out-of-band entries are dropped on the way OUT as well as on the way in, so no
    // write path can persist a trim the loader would then have to refuse.
    {
        QJsonObject bt;
        for (auto it = data.bannerLeadTrimByType.constBegin();
             it != data.bannerLeadTrimByType.constEnd(); ++it) {
            const double value = it.value();
            if (!std::isfinite(value)
                || std::abs(value) > BannerLeadTrim::kPersistCeilingMs) {
                continue;
            }
            bt.insert(it.key(), value);
        }
        obj.insert(QStringLiteral("banner_lead_trim_by_type"), bt);
    }
    // [2026-09-22 GM-002 / CX-001] Keep a last-good generation: only a VALID current file is
    // promoted to .bak, so a corrupt file can never overwrite a good backup.
    // [RT-MED-03 / CL3-F4-006 2026-09-23] ...and "valid" now means SEMANTICALLY valid too: a
    // syntactically fine file carrying out-of-band values must never displace the last-good copy.
    {
        QJsonObject current;
        if (readObjectStatus(learningPath(), &current) == ObjectReadStatus::Ok
            && learningSemanticViolations(current).isEmpty()) {
            QFile::remove(learningBackupPath());
            QFile::copy(learningPath(), learningBackupPath());
        }
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
        // Unknown style/colour -> the current certified shipped defaults. A
        // valid explicit value in settings.json still wins.
        // [ORION_PILL_REMOVED 2026-09-21 owner] "pill" is no longer accepted: a
        // persisted Pill (beta) install loads as Arrow2, exactly as a 2K26 style
        // does. The Pill -> yolo launch route stays compiled but unreachable.
        // The rule itself lives in normalizedMeterStyle() (AppConfig.h) -- shared
        // with save(), setMeterStyle() and switchProfile().
        {
            // [RT-LOW-06 2026-09-23] Unknown/legacy styles migrate to the offered set with a log
            // line; save() then persists the canonical value.
            const QString storedStyle = data_.meterStyle.trimmed().left(48);
            data_.meterStyle = normalizedMeterStyle(storedStyle);
            if (!storedStyle.isEmpty() && storedStyle != data_.meterStyle) {
                qInfo().noquote()
                    << QStringLiteral("METER STYLE MIGRATED: stored meter_style '%1' is not offered; "
                                      "using %2.")
                           .arg(storedStyle, data_.meterStyle);
            }
        }
        const QString color = data_.meterColor.trimmed().toLower();
        if (color != QLatin1String("purple")
            && color != QLatin1String("white")
            && color != QLatin1String("yellow")
            && color != QLatin1String("red")) {
            data_.meterColor = QStringLiteral("White");
        }
    }
    // Unknown/absent proposer -> "cv" (the certified default); "yolo" is the only
    // other accepted value. Normalised here so settings.json can never hand the
    // sidecar an ORION_METER_PROPOSER value get_locator() does not understand.
    data_.meterProposer = normalizedMeterProposer(
        cleanText(obj, "meter_proposer", data_.meterProposer, 16));
    // [ORION_PILL_YOLO_ROUTE 2026-09-17] Missing key -> the compiled default (true),
    // which is what makes a Pill install that predates this build launch on the only
    // proposer that can see its meter. See AppConfigData::pillYoloRoute.
    data_.pillYoloRoute = cleanBool(obj, "pill_yolo_route", data_.pillYoloRoute);
    data_.remotePlayClientMode = cleanText(obj, "remote_play_client_mode", data_.remotePlayClientMode, 48);
    {
        const QString console = cleanText(obj, "remote_play_console", data_.remotePlayConsole, 16).trimmed().toLower();
        data_.remotePlayConsole = (console == QLatin1String("xbox")) ? QStringLiteral("Xbox") : QStringLiteral("PS5");
        data_.xboxRemotePlayWindowTitle = cleanText(obj, "xbox_remote_play_window_title", QString(), 512).trimmed();
        data_.xboxUntestedAcknowledged = cleanBool(obj, "xbox_untested_ack", data_.xboxUntestedAcknowledged);
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
    data_.captureCardDeviceId = cleanText(
        obj, "capture_card_device_id", QString(), 96).trimmed().toLower();
    // [ORION_CAPTURE_FPS 2026-09-14] A missing or non-numeric key keeps the compiled default
    // (60 — exactly what every build before this one hard-coded, so an existing install is
    // unchanged); any number present is SNAPPED to {30, 60, 120}. cleanInt's band is deliberately
    // far wider than the allowed set — it only rejects absurd values before the snap, because a
    // clamp to a band EDGE is a different (and wrong) answer than "the nearest supported mode".
    data_.captureCardFps = snappedCaptureCardFps(
        cleanInt(obj, "capture_card_fps", data_.captureCardFps, 0, 1000));
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
        //
        // History (the pin is what makes a restyle a one-constant change —
        // every persisted value converges on the compiled default next launch):
        //   pre-2026-08-06  #CC44FF  reference violet
        //   2026-08-06      #FF2BD6  magenta
        //   2026-09-14      magenta -> blue #1E90FF, owner: blue, visible on
        //                   light and dark.
        data_.meterOverlayColor =
            QString::fromLatin1(AppConfigData::kMeterOverlayDefaultColor);
        data_.meterOverlayRgb = false;
        const QString rawStyle =
            cleanText(obj, "meter_overlay_style", data_.meterOverlayStyle, 24);
        const QString lowerStyle = rawStyle.toLower();
        if (lowerStyle == QLatin1String("brackets")) data_.meterOverlayStyle = QStringLiteral("Brackets");
        else if (lowerStyle == QLatin1String("hairline")) data_.meterOverlayStyle = QStringLiteral("Hairline");
        else if (lowerStyle == QLatin1String("clean")) data_.meterOverlayStyle = QStringLiteral("Clean");
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
    data_.tipPhaseAnchorConsensus = cleanBool(
        obj, "tip_phase_anchor_consensus", data_.tipPhaseAnchorConsensus);
    // [ORION_TIP_PHASE_FIRST_SIGHT] anchor the phase model on the first accepted sample when
    // every witnessable ladder rung is already history (default ON).
    data_.tipPhaseFirstSightAnchor = cleanBool(
        obj, "tip_phase_first_sight_anchor", data_.tipPhaseFirstSightAnchor);
    // [ORION_TYPE_TRIM] per-shot-type tip-phase trim (default OFF). Values clamped to
    // single-digit ms so a stale settings entry can never move an aim by more than one
    // green-window sliver; keys are exact classifyShotType labels.
    data_.tipPhaseTypeTrimEnabled = cleanBool(obj, "tip_phase_type_trim_enabled", data_.tipPhaseTypeTrimEnabled);
    // A settings file that carries the map is AUTHORITATIVE for it: the built-in defaults are
    // replaced, not merged. Merging let a default (Right Fade -6) ride silently under a file
    // that only named Left Fade -- live on 2026-09-01 for a whole batch nobody had configured.
    if (obj.contains(QStringLiteral("tip_phase_type_trim"))
        && obj.value(QStringLiteral("tip_phase_type_trim")).isObject()) {
        const auto trim = obj.value(QStringLiteral("tip_phase_type_trim")).toObject();
        data_.tipPhaseTypeTrimMs.clear();
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
    data_.tipFrameNative = cleanBool(obj, "tip_frame_native", data_.tipFrameNative);
    // [ORION_AIM_FREEZE] hold the learned aim still for the session (default OFF).
    data_.tipPhaseAimFrozen = cleanBool(obj, "tip_phase_aim_frozen", data_.tipPhaseAimFrozen);
    // [ORION_AIM_AUTOUNLOCK] Hand a refuted lock back to the learner (opt-in; default OFF).
    data_.tipTimingAutoUnlockEnabled = cleanBool(obj, "tip_timing_auto_unlock",
                                                 data_.tipTimingAutoUnlockEnabled);
    data_.sessionLeadProbeEnabled = cleanBool(obj, "session_lead_probe",
                                              data_.sessionLeadProbeEnabled);
    // [ORION_SOURCE_STEAL_GUARD] worse-sigma decisions may not evict an armed token (default ON).
    data_.tipSourceStealGuardEnabled = cleanBool(obj, "tip_source_steal_guard",
                                                 data_.tipSourceStealGuardEnabled);
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
    data_.meterSettleAllowSmoothMotion =
        cleanBool(obj, "meter_settle_allow_smooth_motion", data_.meterSettleAllowSmoothMotion);
    data_.squarePassthroughButton =
        cleanText(obj, "square_passthrough_button", data_.squarePassthroughButton, 8);
    // [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16 owner] A plain boolean read the same
    // fail-to-the-setting way square_passthrough_enabled is: a non-boolean value leaves the
    // compiled default standing rather than silently changing the press. The threshold is
    // CLAMPED into its band rather than rejected -- an out-of-band value is a typo, not a
    // request to reshape every walk (64) or to disarm the feature (a value above 255 is
    // unreachable by a trigger).
    // [ORION_SPRINT_RELEASE_FENCED 2026-09-17 owner] The read half of the fence, and the half
    // that actually cost the owner a session: the compiled default lost to a persisted true, so
    // the persisted value is now READ (it still has to round-trip) and then ANDed away. Logged
    // once per load when it actually fenced something, because "my fades stopped answering" must
    // be one grep away from its cause and not a silent disagreement between file and build.
    const bool persistedSprintRelease =
        cleanBool(obj, "sprint_release_on_square", data_.sprintReleaseOnSquare);
    const bool sprintReleaseIsAllowed = sprintReleaseAllowed();
    data_.sprintReleaseOnSquare = persistedSprintRelease && sprintReleaseIsAllowed;
    if (persistedSprintRelease && !sprintReleaseIsAllowed) {
        qWarning().noquote()
            << QStringLiteral("SPRINT RELEASE: persisted true ignored (feature fenced 2026-09-17;"
                              " env ORION_SPRINT_RELEASE_ON_SQUARE=1 to re-enable for testing)");
    }
    data_.sprintReleaseR2Threshold =
        cleanInt(obj, "sprint_release_r2_threshold", 200,
                 AppConfigData::kSprintReleaseR2ThresholdMin,
                 AppConfigData::kSprintReleaseR2ThresholdMax);
    // [R2_HOLD_PERSISTED_FENCE 2026-09-21] The read half. Preserve visibility of a stale value in
    // the warning, but never let a hidden settings file override the new faithful-pass-through
    // default. AutomationEngine's explicit environment knob is the only remaining A/B door.
    const double persistedSquarePressR2HoldMs =
        cleanDouble(obj, "square_press_r2_hold_ms", 0.0,
                    AppConfigData::kSquarePressR2HoldMinMs,
                    AppConfigData::kSquarePressR2HoldMaxMs);
    data_.squarePressR2HoldMs = 0.0;
    if (persistedSquarePressR2HoldMs > 0.0) {
        qWarning().noquote()
            << QStringLiteral("R2 HOLD: persisted %1 ms ignored (pass-through fence 2026-09-21;"
                              " env ORION_SQUARE_PRESS_R2_HOLD_MS to re-enable for testing)")
                   .arg(persistedSquarePressR2HoldMs, 0, 'f', 1);
    }
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
    // [ORION_LEAD_BY_SOURCE 2026-09-14] The per-route stash. Same band/clean rules as
    // actuation_lead_ms above (cleanActuationLeadMs IS that rule, factored out). Only the two
    // canonical route keys are accepted verbatim; anything else in the object is ignored rather
    // than folded onto a route, so a hand-edited file cannot make two keys fight over "decoder".
    // An entry that resolves to not-configured is dropped, keeping "absent == not configured".
    //
    // MIGRATION + INVARIANT in one call: mirroring the live pair afterwards seeds the CURRENT
    // route from actuation_lead_ms when the file predates this key (an existing install keeps
    // firing at exactly the lead it had), and re-asserts the invariant when a hand-edited stash
    // disagrees with the live pair — the live pair is the authority, because that is the number
    // the engine and every earlier build actually consume. Live values are never changed here.
    {
        data_.actuationLeadBySourceMs.clear();
        data_.actuationLeadUserSetBySource.clear();
        const QJsonObject leadBySource =
            obj.value(QStringLiteral("actuation_lead_by_source")).toObject();
        for (auto it = leadBySource.constBegin(); it != leadBySource.constEnd(); ++it) {
            if (actuationLeadSourceKey(it.key()) != it.key() && it.key() != QLatin1String("xbox_wgc")) {
                continue;   // not one of the two canonical route keys
            }
            const QJsonObject entry = it.value().toObject();
            const QJsonValue leadValue = entry.value(QStringLiteral("lead_ms"));
            if (!leadValue.isDouble()) {
                continue;
            }
            const double lead = cleanActuationLeadMs(leadValue.toDouble(0.0));
            const QJsonValue userValue = entry.value(QStringLiteral("user_set"));
            const bool userSet = userValue.isBool() && userValue.toBool(false);
            if (!actuationLeadIsConfigured(lead, userSet)) {
                continue;
            }
            data_.actuationLeadBySourceMs.insert(it.key(), lead);
            data_.actuationLeadUserSetBySource.insert(it.key(), userSet);
        }
        mirrorActuationLeadIntoSourceStash(data_);
    }
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
    // [ORION_NO_METER_SHELVED 2026-09-15] Same fence on the way in: a true on disk (from an older
    // build, a restored backup, or a hand edit) loads as false while the mode is shelved.
    data_.inputTimedEnabled =
        inputTimedAllowed() && cleanBool(obj, "input_timed_enabled", false);
    // [ORION_NO_METER_V2 2026-09-14] Tolerated, not consumed: an existing file keeps its values
    // and loads without complaint, but no timing math reads either one any more.
    data_.inputTimedDelayMs = cleanDouble(obj, "input_timed_delay_ms", 500.0, 100.0, 2500.0);
    data_.inputTimedLeadMs = cleanDouble(obj, "input_timed_lead_ms", 272.0, 150.0, 400.0);
    data_.inputTimedRhythmEnabled = cleanBool(obj, "input_timed_rhythm_enabled", false);
    // [ORION_NO_METER_V2 2026-09-14] The blind hold. Out-of-band / hand-edited values are clamped
    // INTO the slider's band rather than rejected: the floor is a safety property, so the answer
    // to "someone wrote 200" is 500, never 200.
    data_.noMeterHoldMs = cleanDouble(obj, "no_meter_hold_ms", 650.0,
                                      AppConfigData::kNoMeterHoldMinMs,
                                      AppConfigData::kNoMeterHoldMaxMs);
    // [ORION_NO_METER_FADE_TRIM 2026-09-14] The fade-only trim. Clamped into the slider's band
    // for the same reason as the hold.
    data_.noMeterFadeTrimMs = cleanDouble(obj, "no_meter_fade_trim_ms", 0.0,
                                          AppConfigData::kNoMeterFadeTrimMinMs,
                                          AppConfigData::kNoMeterFadeTrimMaxMs);
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] The console's frame period. A missing or
    // non-numeric key keeps the compiled default (the exact 1000/60), and a hand-edited value is
    // clamped into 8..40 ms rather than rejected — the band is a guard against a broken number,
    // not a menu of modes, and dividing the blind law by a zero or a NaN is the failure it
    // exists to make impossible.
    data_.consoleFrameMs = clampedConsoleFrameMs(
        cleanDouble(obj, "console_frame_ms", kConsoleFrameMsDefault,
                    AppConfigData::kConsoleFrameMinMs, AppConfigData::kConsoleFrameMaxMs));
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] Default TRUE. An existing settings.json
    // without the key adopts the snap, which IS the behaviour change the owner asked for
    // ("641 felt inconsistent, 650 is basically perfect"); false restores the unquantized law.
    data_.noMeterFrameQuantize = cleanBool(obj, "no_meter_frame_quantize",
                                           data_.noMeterFrameQuantize);
    // [ORION_NO_METER_VISION_ASSIST 2026-09-14 owner] Default TRUE. An existing settings.json
    // without the key adopts the hybrid, which is the behaviour change the owner asked for; the
    // key exists so pure-blind stays one toggle away for the A/B.
    data_.noMeterVisionAssist = cleanBool(obj, "no_meter_vision_assist",
                                          data_.noMeterVisionAssist);
    // [ORION_LATE_FIRE_TOLERANCE 2026-09-14 owner] Clamped into the band rather than rejected:
    // 0 restores the abort-on-any-miss behaviour exactly, and the ceiling is a safety property
    // (past ~40 ms a "late" release is not a shot any more, it is a giveaway).
    data_.lateFireToleranceMs = cleanDouble(obj, "late_fire_tolerance_ms", 24.0,
                                            AppConfigData::kLateFireToleranceMinMs,
                                            AppConfigData::kLateFireToleranceMaxMs);
    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] Default TRUE. An existing settings.json without
    // the key adopts the loop, which is the behaviour change the owner asked for; the key exists
    // so a pure open-loop Shot Lead stays one toggle away. Step and clamp are CLAMPED into their
    // bands rather than rejected, exactly like late_fire_tolerance_ms: a 0 step is a valid
    // request ("record the verdicts, move nothing") and the ceilings are safety properties.
    data_.bannerLeadTrim = cleanBool(obj, "banner_lead_trim", data_.bannerLeadTrim);
    data_.bannerTrimStepMs = cleanDouble(obj, "banner_trim_step_ms", 3.0,
                                         AppConfigData::kBannerTrimStepMinMs,
                                         AppConfigData::kBannerTrimStepMaxMs);
    data_.bannerTrimMaxMs = cleanDouble(obj, "banner_trim_max_ms", 15.0,
                                        AppConfigData::kBannerTrimMaxMinMs,
                                        AppConfigData::kBannerTrimMaxMaxMs);
    // [ORION_BANNER_TRIM_HOLD 2026-09-16 owner] The EXCELLENT/GREEN hold, CLAMPED into its band
    // like its two neighbours: an out-of-band value is a typo, not a request to disarm the hold.
    data_.bannerTrimHoldShots = cleanInt(obj, "banner_trim_hold_shots", 12,
                                         AppConfigData::kBannerTrimHoldShotsMin,
                                         AppConfigData::kBannerTrimHoldShotsMax);
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16 owner] The tempo sub-buckets. A plain boolean, read the
    // same fail-to-the-setting way banner_lead_trim is: a non-boolean value leaves the compiled
    // default (TRUE) standing rather than silently disarming the split.
    data_.bannerTrimTempoBuckets = cleanBool(obj, "banner_trim_tempo_buckets",
                                             data_.bannerTrimTempoBuckets);
    // [ORION_BANNER_TRIM_RANGE 2026-09-17 owner] The fade range sub-buckets. A plain boolean,
    // read the same way: an existing settings.json without the key adopts TRUE, which changes
    // nothing on its own -- a fade only gains a third key segment once the sidecar can actually
    // read the range, and an unknown range keys exactly as the 2026-09-16 build did.
    data_.bannerTrimRangeBuckets = cleanBool(obj, "banner_trim_range_buckets",
                                             data_.bannerTrimRangeBuckets);
    // [ORION_BANNER_COVERAGE_ABSENT 2026-09-19 owner] A panel with no coverage CELL calibrates.
    // A plain boolean, read the fail-to-the-setting way its neighbours are: an existing
    // settings.json without the key adopts TRUE, which is the behaviour change the owner asked
    // for -- the 2-cell drill panel was 98 of 281 graded releases on 2026-09-18 and every one of
    // them was excluded from the loop as "coverage unknown".
    data_.bannerTrimAbsentCoverageOpen = cleanBool(obj, "banner_trim_absent_coverage_open",
                                                   data_.bannerTrimAbsentCoverageOpen);
    // [ORION_BANNER_TRIM_BIAS 2026-09-19 owner] The integrator's window and vote margin, CLAMPED
    // into their bands like banner_trim_hold_shots: an out-of-band value is a typo, not a request
    // to disarm the bound. 0 votes IS a legal request (the documented kill switch) and is inside
    // the band, so it survives the clamp.
    data_.bannerTrimBiasWindow = cleanInt(obj, "banner_trim_bias_window", 12,
                                          AppConfigData::kBannerTrimBiasWindowMin,
                                          AppConfigData::kBannerTrimBiasWindowMax);
    data_.bannerTrimBiasVotes = cleanInt(obj, "banner_trim_bias_votes", 0,
                                         AppConfigData::kBannerTrimBiasVotesMin,
                                         AppConfigData::kBannerTrimBiasVotesMax);
    // [ORION_ONSET_FF 2026-09-21 owner] The feedforward's limits, CLAMPED into their bands rather
    // than rejected, exactly like banner_trim_step_ms: 0 gain is a valid request ("keep the
    // reference, move nothing") and the ceilings are safety properties. A settings.json written
    // before this key existed simply keeps the compiled defaults.
    data_.onsetFeedforwardGain = cleanDouble(obj, "onset_ff_gain", data_.onsetFeedforwardGain,
                                             AppConfigData::kOnsetFeedforwardGainMin,
                                             AppConfigData::kOnsetFeedforwardGainMax);
    data_.onsetFeedforwardClampMs = cleanDouble(obj, "onset_ff_clamp_ms",
                                                data_.onsetFeedforwardClampMs,
                                                AppConfigData::kOnsetFeedforwardClampMinMs,
                                                AppConfigData::kOnsetFeedforwardClampMaxMs);
    data_.onsetFeedforwardWindow = cleanInt(obj, "onset_ff_window", data_.onsetFeedforwardWindow,
                                            AppConfigData::kOnsetFeedforwardWindowMin,
                                            AppConfigData::kOnsetFeedforwardWindowMax);
    data_.onsetFeedforwardMinSamples = cleanInt(obj, "onset_ff_min_samples",
                                                data_.onsetFeedforwardMinSamples,
                                                AppConfigData::kOnsetFeedforwardMinSamplesMin,
                                                AppConfigData::kOnsetFeedforwardMinSamplesMax);
    data_.onsetFeedforwardOneSided = cleanBool(obj, "onset_ff_one_sided",
                                               data_.onsetFeedforwardOneSided);
    // [ORION_LEAD_OFFSET_BY_TYPE 2026-09-16 owner] The fixed per-shot-type addition to the Shot
    // Lead. CLAMPED into the band rather than rejected, exactly like banner_trim_step_ms: 0 is a
    // valid request on every bucket (and zeroing all four restores the 2026-09-16 baseline lead
    // byte-for-byte) while the +-40 ceiling is a safety property. An existing settings.json
    // without the keys adopts the 8/8/0/0 defaults, which IS the behaviour change the owner asked
    // for -- fades fire 8 ms earlier, standstills are untouched.
    data_.leadOffsetLeftFadeMs = cleanDouble(obj, "lead_offset_left_fade_ms", -6.0,
                                             AppConfigData::kLeadOffsetByTypeMinMs,
                                             AppConfigData::kLeadOffsetByTypeMaxMs);
    // [ORION_LEFT_FADE_LATER 2026-09-22 owner] One-time migration: every install before this change
    // persisted the old +8 default. A pre-revision-2 file that still holds exactly +8 adopts the new
    // default; any other value (a deliberate owner choice) is kept. The next save writes rev 2.
    if (obj.value(QStringLiteral("lead_offset_left_fade_rev")).toInt(1) < 2
        && std::abs(data_.leadOffsetLeftFadeMs - 8.0) < 1e-9) {
        data_.leadOffsetLeftFadeMs = -6.0;
    }
    data_.leadOffsetRightFadeMs = cleanDouble(obj, "lead_offset_right_fade_ms", 8.0,
                                              AppConfigData::kLeadOffsetByTypeMinMs,
                                              AppConfigData::kLeadOffsetByTypeMaxMs);
    data_.leadOffsetStandstillMs = cleanDouble(obj, "lead_offset_standstill_ms", 0.0,
                                               AppConfigData::kLeadOffsetByTypeMinMs,
                                               AppConfigData::kLeadOffsetByTypeMaxMs);
    data_.leadOffsetOtherMs = cleanDouble(obj, "lead_offset_other_ms", 0.0,
                                          AppConfigData::kLeadOffsetByTypeMinMs,
                                          AppConfigData::kLeadOffsetByTypeMaxMs);
    // [ORION_LEAD_OFFSET_FADE_MID 2026-09-17 owner] Used INSTEAD of the two fade offsets when
    // the live press's range reads `mid`. An existing file without the key adopts 6.0, which is
    // only reachable once a range actually arrives.
    data_.leadOffsetFadeMidMs = cleanDouble(obj, "lead_offset_fade_mid_ms", 6.0,
                                            AppConfigData::kLeadOffsetByTypeMinMs,
                                            AppConfigData::kLeadOffsetByTypeMaxMs);
    // [ORION_LEAD_AUTO_SEED 2026-09-15 owner] Default TRUE. An existing settings.json without the
    // key adopts the seed, which is the behaviour change the owner asked for -- and which can
    // only reach an install whose Shot Lead is still "never configured", because a configured
    // lead (user-set OR already seeded into actuation_lead_ms) out-ranks it everywhere. The
    // margin and the placeholder are CLAMPED into their bands rather than rejected, exactly like
    // banner_trim_step_ms: 0 margin restores today's pure authority lead and the ceilings are
    // safety properties.
    data_.leadAutoSeed = cleanBool(obj, "lead_auto_seed", data_.leadAutoSeed);
    data_.aimMarginMs = cleanDouble(obj, "aim_margin_ms", 69.0,
                                    AppConfigData::kAimMarginMinMs,
                                    AppConfigData::kAimMarginMaxMs);
    data_.leadFactoryPlaceholderMs =
        cleanDouble(obj, "lead_factory_placeholder_ms", 269.0,
                    AppConfigData::kLeadFactoryPlaceholderMinMs,
                    AppConfigData::kLeadFactoryPlaceholderMaxMs);
    // [ORION_OWNED_METER_NEVER_ABORTS 2026-09-14 owner] default TRUE; false restores the bounded
    // tolerance above exactly as it shipped.
    data_.ownedMeterNeverAborts = cleanBool(obj, "owned_meter_never_aborts",
                                            data_.ownedMeterNeverAborts);
    // [ORION_OWNERSHIP_PROOF_LENIENCY 2026-09-14 owner] default TRUE; false restores today's
    // ownership_proof_incomplete refusal exactly.
    data_.ownershipProofLeniency = cleanBool(obj, "ownership_proof_leniency",
                                             data_.ownershipProofLeniency);
    // [ORION_METER_BLIND_BACKSTOP 2026-09-14 owner] default TRUE. An existing settings.json
    // without the key adopts the backstop, which is the behaviour change the owner asked for
    // ("aborts on wide-open shots" is a ship blocker); false restores today's
    // press_unanswered_no_meter abort exactly.
    data_.meterBlindBackstop = cleanBool(obj, "meter_blind_backstop", data_.meterBlindBackstop);
    // [ORION_METER_BACKSTOP_GRACE 2026-09-15 owner] Clamped into the band rather than rejected,
    // exactly like the late-fire tolerance: 0 restores the 2026-09-14 deadline (press + the law)
    // byte-for-byte, and the ceiling is a safety property (a grace past ~400 ms would outlive the
    // shot animation the backstop exists to answer). An existing settings.json without the key
    // adopts the 100 ms default, which IS the behaviour change the owner asked for.
    // [ORION_METER_BACKSTOP_GRACE_FADE 2026-09-15 owner] Clamped into its band rather than
    // rejected, exactly like the Standstill grace: 0 restores the single-grace behaviour (a fade
    // then uses meter_backstop_grace_ms) and the ceiling is a safety property.
    data_.meterBackstopGraceFadeMs = cleanDouble(obj, "meter_backstop_grace_fade_ms", 220.0,
                                                 AppConfigData::kMeterBackstopGraceFadeMinMs,
                                                 AppConfigData::kMeterBackstopGraceFadeMaxMs);
    data_.meterBackstopGraceMs = cleanDouble(obj, "meter_backstop_grace_ms", 100.0,
                                             AppConfigData::kMeterBackstopGraceMinMs,
                                             AppConfigData::kMeterBackstopGraceMaxMs);
    // [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-16 owner] Clamped into its band rather than
    // rejected, exactly like the two graces: 0 restores the 2026-09-15 deadline byte-for-byte and
    // the ceiling is a safety property.
    // [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-17 owner] The default is now 0 -- THE COLLAPSE IS
    // OFF. The 2026-09-17 press-window dump showed it firing blind 19-39 ms before a LATE
    // GATHER's own meter existed (track drawn at press+614/+617, collapsed deadline +653), and a
    // blind fire on a shot that was about to have a meter costs more than the ~100 ms of feel it
    // saves on a press that was never going to get one. See AppConfigData for the full trade; an
    // existing settings.json that carries the key keeps whatever the owner set.
    data_.meterBackstopNeverSeenProbeMs = cleanDouble(
        obj, "meter_backstop_never_seen_probe_ms", 0.0,
        AppConfigData::kMeterBackstopNeverSeenProbeMinMs,
        AppConfigData::kMeterBackstopNeverSeenProbeMaxMs);
    // [ORION_METER_BACKSTOP_NEVER_SEEN_FADE 2026-09-16 owner] Clamped into its own band, and its
    // default is 0: an existing settings.json without the key EXCLUDES fades from the collapse,
    // which is the measured behaviour (a slow fade's meter is first seen 1000-1051 ms into the
    // press, after its own law). 0 here means excluded, NOT "fall back to the Standstill probe".
    data_.meterBackstopNeverSeenProbeFadeMs = cleanDouble(
        obj, "meter_backstop_never_seen_probe_fade_ms", 0.0,
        AppConfigData::kMeterBackstopNeverSeenProbeFadeMinMs,
        AppConfigData::kMeterBackstopNeverSeenProbeFadeMaxMs);
    // [ORION_VISION_HOLD_BAND 2026-09-15 owner] Clamped into the band rather than rejected,
    // exactly like the two backstop graces: 0 is the kill switch (vision fires wherever it
    // predicted, byte-for-byte the 2026-09-15 build) and the ceiling is a safety property (a band
    // past ~200 ms cannot bind on a real hold, so it would be an OFF switch wearing an ON label).
    // [SHIP CONFIG 2026-09-17] The compiled default is now 0 (band OFF) -- see AppConfigData::
    // visionHoldBandMs for the 09-16 21:00 refutation. An existing settings.json without the key
    // therefore adopts 0/0 and fires exactly where vision predicted, and a file that DOES carry
    // the key keeps whatever the owner last set. The two fallbacks below must stay in lockstep
    // with the header's defaults or a fresh install and a key-less file would disagree.
    data_.visionHoldBandMs = cleanDouble(obj, "vision_hold_band_ms", 0.0,
                                         AppConfigData::kVisionHoldBandMinMs,
                                         AppConfigData::kVisionHoldBandMaxMs);
    data_.visionHoldBandFadeMs = cleanDouble(obj, "vision_hold_band_fade_ms", 0.0,
                                             AppConfigData::kVisionHoldBandFadeMinMs,
                                             AppConfigData::kVisionHoldBandFadeMaxMs);
    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15 owner] Ignore-unknown, never guess: a value that is
    // neither "flick" nor "letgo" leaves the shipped flick in force. cleanText gives us the raw
    // string; the vocabulary check is here so file, setting and env all share one rule.
    {
        const QString style = cleanText(obj, "tempo_release_style", data_.tempoReleaseStyle, 16)
                                  .trimmed().toLower();
        if (style == QLatin1String("flick") || style == QLatin1String("letgo")) {
            data_.tempoReleaseStyle = style;
        }
    }
    // [ORION_PRESS_ANCHORED_FALLBACK 2026-09-14 — RETIRED] Tolerated on load so an existing file
    // is neither rejected nor silently rewritten; no code path reads the value.
    data_.pressAnchoredFallbackEnabled = cleanBool(
        obj, "press_anchored_fallback_enabled", data_.pressAnchoredFallbackEnabled);
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
    // [ORION_RHYTHM_FLICK_DELAY 2026-09-14] Signed trim, so the band is symmetric about 0 and 0
    // is the inert default. Clamped rather than ignored: unlike the Shot Lead (where an
    // out-of-band value must degrade to "not configured" so it cannot become an invented lead),
    // this can only ever shrink an EXISTING lead, so clamping is the safe, predictable behaviour.
    data_.rhythmFlickDelayMs = cleanDouble(obj, "rhythm_flick_delay_ms", data_.rhythmFlickDelayMs,
                                           AppConfigData::kRhythmFlickDelayMinMs,
                                           AppConfigData::kRhythmFlickDelayMaxMs);
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
        data_.rhythmFlickDelayMs = cleanDouble(tempo, "rhythm_flick_delay_ms",
                                               data_.rhythmFlickDelayMs,
                                               AppConfigData::kRhythmFlickDelayMinMs,
                                               AppConfigData::kRhythmFlickDelayMaxMs);
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
    // [RT-MED-03 / CL3-F4-006 2026-09-23] Every numeric field goes through its semantic band
    // (kLearningScalarBands / kLearningMapBands): an out-of-band or non-numeric value keeps the
    // default instead of installing a number no learner could have produced.
    const auto bandFor = [](const LearningBand* first, const LearningBand* last,
                            const char* key) -> const LearningBand* {
        for (const LearningBand* b = first; b != last; ++b) {
            if (std::strcmp(b->key, key) == 0) {
                return b;
            }
        }
        return nullptr;
    };
    const auto scalar = [&](const char* key, double fallback) -> double {
        const LearningBand* band = bandFor(std::begin(kLearningScalarBands),
                                           std::end(kLearningScalarBands), key);
        const QJsonValue v = obj.value(QString::fromLatin1(key));
        return (band != nullptr && learningNumberInBand(v, band->lo, band->hi)) ? v.toDouble()
                                                                                 : fallback;
    };
    const auto mapInto = [&](const char* key, QMap<QString, double>& out) {
        const LearningBand* band = bandFor(std::begin(kLearningMapBands),
                                           std::end(kLearningMapBands), key);
        const QJsonObject map = obj.value(QString::fromLatin1(key)).toObject();
        for (auto it = map.constBegin(); it != map.constEnd(); ++it) {
            if (band != nullptr && learningNumberInBand(it.value(), band->lo, band->hi)) {
                out.insert(it.key(), it.value().toDouble());
            }
        }
    };
    learning_.version = static_cast<int>(scalar("version", learning_.version));
    learning_.emaFillPerFrame = scalar("ema_fill_per_frame", learning_.emaFillPerFrame);
    learning_.emaGreenRatio = scalar("ema_green_ratio", learning_.emaGreenRatio);
    learning_.biasPct = scalar("bias_pct", learning_.biasPct);

    const auto perLevel = obj.value(QStringLiteral("per_level")).toObject();
    learning_.openNudgePct = perLevelNudge(perLevel, QStringLiteral("OPEN"));
    learning_.lightNudgePct = perLevelNudge(perLevel, QStringLiteral("LIGHT"));
    learning_.moderateNudgePct = perLevelNudge(perLevel, QStringLiteral("MODERATE"));
    learning_.heavyNudgePct = perLevelNudge(perLevel, QStringLiteral("HEAVY"));
    learning_.smotheredNudgePct = perLevelNudge(perLevel, QStringLiteral("SMOTHERED"));

    mapInto("shot_type_learned_offset_ms", learning_.shotTypeLearnedOffsetMs);
    mapInto("shot_type_feedforward_ms", learning_.shotTypeFeedforwardMs);
    mapInto("shot_type_meter_to_release_ms", learning_.shotTypeMeterToReleaseMs);
    mapInto("shot_type_appear_to_tip_ms", learning_.shotTypeAppearToTipMs);
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
    // [ORION_SESSION_LEAD_PROBE] same plausibility band; an out-of-band pair reads as never
    // captured and the engine recaptures.
    {
        const double v = obj.value(QStringLiteral("lead_reference_physical_ms")).toDouble(-1.0);
        const double l = obj.value(QStringLiteral("lead_reference_lead_ms")).toDouble(-1.0);
        const bool ok = v >= 200.0 && v <= 500.0 && l > 0.0 && l <= 1000.0;
        learning_.leadReferencePhysicalMs = ok ? v : -1.0;
        learning_.leadReferenceLeadMs = ok ? l : -1.0;
    }
    {
        QMap<QString, double> calPhase;
        mapInto("shot_type_cal_phase", calPhase);
        for (auto it = calPhase.constBegin(); it != calPhase.constEnd(); ++it) {
            learning_.shotTypeCalPhase.insert(it.key(), static_cast<int>(it.value()));
        }
    }
    mapInto("shot_type_rtt_baseline_ms", learning_.shotTypeRttBaselineMs);
    mapInto("shot_type_velocity_prior_pct_ms", learning_.shotTypeVelocityPriorPctMs);
    // Hybrid global phase-clock self-learned globals (autonomous_vision path).
    learning_.globalAppearToTipMs = scalar("global_appear_to_tip_ms", learning_.globalAppearToTipMs);
    learning_.globalHoldToReleaseMs = scalar("global_hold_to_release_ms", learning_.globalHoldToReleaseMs);
    learning_.learnedLatencyMs = scalar("learned_latency_ms", learning_.learnedLatencyMs);
    learning_.probeSpawnOffsetMs = scalar("probe_spawn_offset_ms", learning_.probeSpawnOffsetMs);
    learning_.globalRiseVelocityPctMs = scalar("global_rise_velocity_pct_ms", learning_.globalRiseVelocityPctMs);
    // Per-shot-type latency residual (autonomous vision)
    mapInto("shot_type_latency_ms", learning_.shotTypeLatencyMs);
    // [ORION_NO_METER_V2 2026-09-14] Per-type vision-path hold. A record whose median is outside
    // the plausible hold envelope, or whose count is non-positive, is dropped rather than
    // installed: it feeds a BLIND release, so a corrupt entry must degrade to the shipped table,
    // never to an arbitrary hold.
    const auto noMeterHold = obj.value(QStringLiteral("no_meter_hold_by_type")).toObject();
    for (auto it = noMeterHold.constBegin(); it != noMeterHold.constEnd(); ++it) {
        const QJsonObject rec = it.value().toObject();
        NoMeterHoldRecord parsed;
        parsed.medianMs = rec.value(QStringLiteral("median_ms")).toDouble(-1.0);
        parsed.n = rec.value(QStringLiteral("n")).toInt(0);
        if (parsed.n > 0 && parsed.medianMs >= 200.0 && parsed.medianMs <= 4000.0) {
            learning_.noMeterHoldByType.insert(it.key(), parsed);
        }
    }
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] Same schema guard, same reason: this value is ADDED to
    // the owner's Shot Lead, so a corrupt entry must degrade to "no trim" rather than to an
    // offset no setting could have produced. The 50 % start decay and the clamp to the LIVE
    // banner_trim_max_ms both happen in the engine (BannerLeadTrim::restoreDecayed) -- the file
    // layer only refuses what is not a number in the persistable band.
    const auto bannerTrim = obj.value(QStringLiteral("banner_lead_trim_by_type")).toObject();
    for (auto it = bannerTrim.constBegin(); it != bannerTrim.constEnd(); ++it) {
        const double value = it.value().toDouble(std::numeric_limits<double>::quiet_NaN());
        if (std::isfinite(value) && std::abs(value) <= BannerLeadTrim::kPersistCeilingMs) {
            learning_.bannerLeadTrimByType.insert(it.key(), value);
        }
    }
}

} // namespace orion
