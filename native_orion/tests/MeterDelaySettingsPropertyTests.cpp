// ─────────────────────────────────────────────────────────────────────────────
//  MeterDelaySettingsPropertyTests — QML property contract for the Meter Delay card
// ─────────────────────────────────────────────────────────────────────────────
//
// Regression coverage for the shipped "meter delay toggle doesn't work" bug:
// MeterConfigPanel.qml binds orion.meterDelayEnabled / orion.meterDelayMs /
// orion.meterDelayBypassOnDefense, but OrionAppController declared no Q_PROPERTY
// for any of those names. QML resolves a missing property to `undefined`, so the
// toggle rendered unchecked regardless of the persisted value and every
// assignment failed at runtime ("Cannot assign to non-existent property") — the
// subsystem was untestable from the UI. Nothing in the build fails when a
// Q_PROPERTY vanishes (QML property resolution is runtime-only), so this suite
// pins the contract at the meta-object level.
//
// WHY THIS SUITE NEVER CONSTRUCTS AN OrionAppController: the constructor spawns
// the packet-bridge local process when the network defaults are on
// (OrionAppController.cpp, ensurePacketBridgeRunning()/networkBridge_.start())
// and the destructor force-kills every chiaki/OrionStream image on the machine
// (killChiakiProcesses()). Both are unacceptable side effects for a unit test on
// a rig that may be running a live session. The QML contract under test —
// property exists, correct type, readable, writable, notifies settingsChanged —
// is fully decided by the moc-generated staticMetaObject, which needs no
// instance. The disk round-trip of the same three settings is asserted against
// AppConfig directly (the single source of truth the accessors read and write).

#include "AppConfig.h"
#include "OrionAppController.h"

#include <QtCore/QFile>
#include <QtCore/QMetaObject>
#include <QtCore/QMetaProperty>
#include <QtCore/QTemporaryDir>
#include <QtTest/QSignalSpy>
#include <QtTest/QTest>

using orion::AppConfig;
using orion::AppConfigData;
using orion::OrionAppController;

class MeterDelaySettingsPropertyTests final : public QObject {
    Q_OBJECT

private slots:
    void meterDelayPropertiesExistForQml();
    void meterDelaySettingsRoundTripThroughAppConfig();
    void meterDelayMsIsClampedOnLoad();
    void meterDelayAvailabilityContractForQml();
    void meterDelayBackendPresenceProbe();
    void meterDelayStatusLineIsHonestAcrossArmStates();
    // [ORION_METER_DELAY_LINK 2026-08-08] Enabling Meter Delay must BY ITSELF
    // configure the packet-bridge link; the diagnostics capture opt-in (which lost
    // its UI control 2026-08-06) must never be able to veto a headline feature.
    void meterDelayAloneConfiguresTheBridgeLink();
    // [ORION_METER_DELAY_ECHO 2026-08-08] The service's own applied-delay echo:
    // NetworkBridge must parse the state broadcast / delay-verb ack snapshot,
    // reject malformed values, dedupe, and treat any echo as proof of Armed.
    void serviceEchoParsesDedupesAndConfirmsArmed();
    // [ORION_LEAD_CONFLICT 2026-08-08] Lives here because this is the one suite that compiles
    // the real OrionAppController TU (see the file header); same vanishing-property class of
    // bug as the meter-delay toggle, same protection.
    void shotLeadConflictSurfaceContractForQml();
    // [METER DETECTION CARD 2026-09-10] MeterConfigPanel.qml binds orion.meterProposer
    // (Detector combo) and orion.detectorHealthLine / orion.detectorProvider (health
    // line) exactly like the meter-delay properties above — same vanishing-property
    // protection, plus the settings.json round-trip + normalisation for meter_proposer.
    void meterDetectionPropertiesExistForQml();
    void meterProposerRoundTripThroughAppConfig();
    void shotLeadMaxUsableMirrorsEngineMargin();
    void outcomeIdentityArtifactVerdictIsDemoted();
    void measuredPhaseMedianPersistsCanonically();
    // [PROFILE BLOCK 2026-09-14, moved to the licence strip 2026-09-15 owner]
    // components/Sidebar.qml binds twelve orion.profile* properties plus
    // machineIdMasked. Same vanishing-property class of bug as the meter-delay
    // toggle: QML resolves them at RUNTIME, so a renamed or dropped Q_PROPERTY
    // ships a silently blank licence strip in the sidebar footer.
    void profilePropertiesExistForQml();
    void daysLeftIsUnknownLifetimeOrWholeDaysRoundedUp();
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] components/ShotVerdictTally.qml binds ten
    // orion.banner* properties and calls orion.resetBannerTally(), from inside BOTH
    // NoMeterCard and ShotLeadCard. Same vanishing-property class of bug as the meter-delay
    // toggle, and worse here: the tally would render "no shots yet" forever while the
    // sidecar happily graded every banner, which is indistinguishable from "the reader is
    // off" and would send the owner back to counting by eye.
    void bannerVerdictTallyPropertiesExistForQml();
};

void MeterDelaySettingsPropertyTests::profilePropertiesExistForQml()
{
    const QMetaObject& meta = OrionAppController::staticMetaObject;

    const struct {
        const char* name;
        QMetaType::Type type;
    } expectations[] = {
        {"profileKnown", QMetaType::Bool},
        {"profileDiscordId", QMetaType::QString},
        {"profileDiscordName", QMetaType::QString},
        {"profilePlan", QMetaType::QString},
        {"profileExpiryEpochS", QMetaType::Double},
        {"profileDaysLeft", QMetaType::Int},
        {"profileLifetime", QMetaType::Bool},
        {"profileActivatedEpochS", QMetaType::Double},
        {"profileHwidResetsUsed", QMetaType::Int},
        {"profileHwidResetsFreeTotal", QMetaType::Int},
        {"profileHwidResetsFreeRemaining", QMetaType::Int},
        {"profileHwidPaidCredits", QMetaType::Int},
    };

    for (const auto& expected : expectations) {
        const int index = meta.indexOfProperty(expected.name);
        QVERIFY2(index >= 0,
                 qPrintable(QStringLiteral(
                     "Q_PROPERTY \"%1\" is missing from OrionAppController - "
                     "Sidebar.qml binds orion.%1, so without it the licence strip "
                     "silently renders a blank row.")
                     .arg(QLatin1String(expected.name))));
        const QMetaProperty property = meta.property(index);
        QCOMPARE(property.typeId(), static_cast<int>(expected.type));
        QVERIFY(property.isReadable());
        QVERIFY2(property.hasNotifySignal(),
                 qPrintable(QStringLiteral(
                     "%1 must notify or the page never refreshes")
                     .arg(QLatin1String(expected.name))));
        // Its OWN low-fanout notifier: the Profile page must not re-evaluate on
        // every broad status tick, and a heartbeat must refresh it without one.
        QCOMPARE(QByteArray(property.notifySignal().name()),
                 QByteArrayLiteral("profileChanged"));
    }

    // Support identity + the two clipboard actions the page offers.
    QVERIFY(meta.indexOfProperty("machineIdMasked") >= 0);
    QVERIFY(meta.indexOfProperty("licenseKeyMasked") >= 0);
    QVERIFY(meta.indexOfMethod("copyProfileDiscordId()") >= 0);
    QVERIFY(meta.indexOfMethod("copyLicenseKey()") >= 0);
    // The customer Activity feed the Live page and LogViewer bind.
    const int activity = meta.indexOfProperty("activityText");
    QVERIFY(activity >= 0);
    QCOMPARE(meta.property(activity).typeId(), static_cast<int>(QMetaType::QString));
    QCOMPARE(QByteArray(meta.property(activity).notifySignal().name()),
             QByteArrayLiteral("logsChanged"));
}

void MeterDelaySettingsPropertyTests::daysLeftIsUnknownLifetimeOrWholeDaysRoundedUp()
{
    constexpr qint64 kDay = 86400;
    constexpr qint64 now = 1800000000;
    // No server profile yet (the CURRENT live Lambda): unknown, never "0 days",
    // which a customer would read as expired.
    QCOMPARE(OrionAppController::licenseDaysLeft(false, now + 10 * kDay, now), -1);
    // expiry 0 with a known profile is LIFETIME, not "expired in 1970".
    QCOMPARE(OrionAppController::licenseDaysLeft(true, 0, now), -2);
    // Whole days, rounded UP: four hours left is still a day you can play.
    QCOMPARE(OrionAppController::licenseDaysLeft(true, now + 4 * 3600, now), 1);
    QCOMPARE(OrionAppController::licenseDaysLeft(true, now + kDay, now), 1);
    QCOMPARE(OrionAppController::licenseDaysLeft(true, now + kDay + 1, now), 2);
    QCOMPARE(OrionAppController::licenseDaysLeft(true, now + 30 * kDay, now), 30);
    // Past expiry floors at zero rather than going negative.
    QCOMPARE(OrionAppController::licenseDaysLeft(true, now - kDay, now), 0);
    QCOMPARE(OrionAppController::licenseDaysLeft(true, now, now), 0);
}
void MeterDelaySettingsPropertyTests::meterDelayPropertiesExistForQml()
{
    const QMetaObject& meta = OrionAppController::staticMetaObject;

    const struct {
        const char* name;
        QMetaType::Type type;
    } expectations[] = {
        {"inputTimedEnabled", QMetaType::Bool},
        {"inputTimedPaused", QMetaType::Bool},
        {"inputTimedDelayMs", QMetaType::Double},
        {"inputTimedRhythmEnabled", QMetaType::Bool},
        {"meterDelayEnabled", QMetaType::Bool},
        {"meterDelayMs", QMetaType::Int},
        {"meterDelayBypassOnDefense", QMetaType::Bool},
        // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] MeterConfigPanel.qml binds
        // orion.meterDelayLeadOffsetMs (the delayed-condition Shot Lead offset) exactly the way
        // it binds the three above, so it needs the same protection: without the Q_PROPERTY the
        // field silently stops committing and the delayed condition quietly reverts to firing
        // with the delay-0 lead — which is the bug this whole surface exists to end.
        {"meterDelayLeadOffsetMs", QMetaType::Double},
    };

    for (const auto& expected : expectations) {
        const int index = meta.indexOfProperty(expected.name);
        QVERIFY2(index >= 0,
                 qPrintable(QStringLiteral(
                     "Q_PROPERTY \"%1\" is missing from OrionAppController — "
                     "MeterConfigPanel.qml binds orion.%1, so without it the Meter "
                     "Delay card silently stops working (the shipped toggle bug).")
                     .arg(QLatin1String(expected.name))));

        const QMetaProperty property = meta.property(index);
        QCOMPARE(property.typeId(), static_cast<int>(expected.type));
        QVERIFY2(property.isReadable(),
                 qPrintable(QStringLiteral("%1 must be readable (checked/value binding)")
                     .arg(QLatin1String(expected.name))));
        QVERIFY2(property.isWritable(),
                 qPrintable(QStringLiteral("%1 must be writable (onToggled/onMoved assign it)")
                     .arg(QLatin1String(expected.name))));
        QVERIFY2(property.hasNotifySignal(),
                 qPrintable(QStringLiteral("%1 must notify or the card never refreshes")
                     .arg(QLatin1String(expected.name))));
        // The whole settings surface shares the broad settingsChanged notifier; a
        // property on a different signal would refresh on a different cadence than
        // the card expects.
        QCOMPARE(QByteArray(property.notifySignal().name()),
                 QByteArrayLiteral("settingsChanged"));
    }
}

void MeterDelaySettingsPropertyTests::meterDelaySettingsRoundTripThroughAppConfig()
{
#ifdef ORION_PRODUCTION_BUILD
    // AppConfig deliberately redirects mutable state to the per-user data
    // directory in production and ignores the supplied checkout root. This is
    // a development-path persistence fixture; running it in production would
    // read and overwrite the tester's live customer settings.
    QSKIP("settings path redirects to the per-user data dir in production builds");
#endif
    QTemporaryDir dir;
    QVERIFY(dir.isValid());

    AppConfig first(dir.path());
    first.load();  // no settings.json yet -> seeds defaults, writes nothing

    // Shipped defaults (a change here is a product decision, not a wiring fix).
    QCOMPARE(first.data().meterDelayEnabled, true);
    QCOMPARE(first.data().meterDelayMs, 250);
    QCOMPARE(first.data().meterDelayBypassOnDefense, true);
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Default 0 == the pre-keying engine at every
    // delay value. A change here would silently re-time every existing install's delayed shots.
    QCOMPARE(first.data().meterDelayLeadOffsetMs, 0.0);

    // Non-default values for all four settings must survive save -> fresh load.
    AppConfigData data = first.data();
    data.meterDelayEnabled = false;
    data.meterDelayMs = 280;
    data.meterDelayBypassOnDefense = false;
    data.meterDelayLeadOffsetMs = 65.0;
    QString error;
    QVERIFY2(first.save(data, &error), qPrintable(error));

    AppConfig second(dir.path());
    QVERIFY(second.load());
    QCOMPARE(second.data().meterDelayEnabled, false);
    QCOMPARE(second.data().meterDelayMs, 280);
    QCOMPARE(second.data().meterDelayBypassOnDefense, false);
    QCOMPARE(second.data().meterDelayLeadOffsetMs, 65.0);

    // Out-of-band (hand-edited settings.json) clamps into the signed band on load, exactly like
    // meterDelayMs — a typo must never install an offset no other gate would accept.
    AppConfigData wild = second.data();
    wild.meterDelayLeadOffsetMs = 5000.0;
    QVERIFY2(second.save(wild, &error), qPrintable(error));
    AppConfig third(dir.path());
    QVERIFY(third.load());
    QCOMPARE(third.data().meterDelayLeadOffsetMs,
             AppConfigData::kMeterDelayLeadOffsetMaxMs);
}

void MeterDelaySettingsPropertyTests::meterDetectionPropertiesExistForQml()
{
    const QMetaObject& meta = OrionAppController::staticMetaObject;

    const struct {
        const char* name;
        bool writable;
        const char* notify;
    } expectations[] = {
        // The user's choice: persisted, so it rides the broad settings notifier.
        {"meterProposer", true, "settingsChanged"},
        // Sidecar-fed presentation strings: read-only, own low-fanout notifier.
        {"detectorHealthLine", false, "detectorHealthChanged"},
        {"detectorProvider", false, "detectorHealthChanged"},
    };

    for (const auto& expected : expectations) {
        const int index = meta.indexOfProperty(expected.name);
        QVERIFY2(index >= 0,
                 qPrintable(QStringLiteral(
                     "Q_PROPERTY \"%1\" is missing from OrionAppController — the Meter "
                     "Detection card binds orion.%1 and would silently go dead.")
                     .arg(QLatin1String(expected.name))));
        const QMetaProperty property = meta.property(index);
        QCOMPARE(property.typeId(), static_cast<int>(QMetaType::QString));
        QVERIFY(property.isReadable());
        QCOMPARE(property.isWritable(), expected.writable);
        QVERIFY2(property.hasNotifySignal(),
                 qPrintable(QStringLiteral("%1 must notify or the card never refreshes")
                     .arg(QLatin1String(expected.name))));
        QCOMPARE(QByteArray(property.notifySignal().name()), QByteArray(expected.notify));
    }
}

void MeterDelaySettingsPropertyTests::meterProposerRoundTripThroughAppConfig()
{
    // The normaliser is the single source of truth for settings.json, the controller
    // setter and the sidecar env (ORION_METER_PROPOSER): only "cv" | "yolo" ever escape it.
    QCOMPARE(orion::normalizedMeterProposer(QStringLiteral("cv")), QStringLiteral("cv"));
    QCOMPARE(orion::normalizedMeterProposer(QStringLiteral("  YOLO ")), QStringLiteral("yolo"));
    QCOMPARE(orion::normalizedMeterProposer(QStringLiteral("onnx")), QStringLiteral("yolo"));
    QCOMPARE(orion::normalizedMeterProposer(QStringLiteral("tensorrt")), QStringLiteral("cv"));
    QCOMPARE(orion::normalizedMeterProposer(QString{}), QStringLiteral("cv"));

#ifdef ORION_PRODUCTION_BUILD
    QSKIP("settings path redirects to the per-user data dir in production builds");
#endif
    QTemporaryDir dir;
    QVERIFY(dir.isValid());

    AppConfig first(dir.path());
    first.load();  // no settings.json yet -> seeds defaults, writes nothing
    // Shipped default: the pure-CV proposer (a change here is a product decision).
    QCOMPARE(first.data().meterProposer, QStringLiteral("cv"));

    AppConfigData data = first.data();
    data.meterProposer = QStringLiteral("yolo");
    QString error;
    QVERIFY2(first.save(data, &error), qPrintable(error));

    AppConfig second(dir.path());
    QVERIFY(second.load());
    QCOMPARE(second.data().meterProposer, QStringLiteral("yolo"));

    // An unknown value (hand-edited settings.json, stale profile) converges on the
    // default on the way through — the sidecar must never see a proposer name that
    // get_locator() does not understand.
    AppConfigData wild = second.data();
    wild.meterProposer = QStringLiteral("tensorrt");
    QVERIFY2(second.save(wild, &error), qPrintable(error));
    AppConfig third(dir.path());
    QVERIFY(third.load());
    QCOMPARE(third.data().meterProposer, QStringLiteral("cv"));
}

void MeterDelaySettingsPropertyTests::meterDelayMsIsClampedOnLoad()
{
#ifdef ORION_PRODUCTION_BUILD
    QSKIP("settings path redirects to the per-user data dir in production builds");
#endif
    QTemporaryDir dir;
    QVERIFY(dir.isValid());

    AppConfig writer(dir.path());
    writer.load();
    AppConfigData data = writer.data();
    // Out-of-band value (e.g. hand-edited settings.json). The load path must clamp
    // into the shared band rather than hand the actuator an arbitrary delay.
    data.meterDelayMs = 10000;
    QString error;
    QVERIFY2(writer.save(data, &error), qPrintable(error));

    AppConfig reader(dir.path());
    reader.load();
    QCOMPARE(reader.data().meterDelayMs, AppConfigData::kMeterDelayMaxMs);
    // [ORION_METER_DELAY_RANGE 2026-08-08] Band widened 200-300 -> 100-600 (a
    // product decision by the owner: probe the empirical physics ceiling). This
    // is the ONLY edit ever made to this pinned suite for that change; the
    // in-band round-trip regression lives in AutomationEngineTests::
    // meterDelayMsRoundTripsAcrossTheWidenedBand.
    QCOMPARE(AppConfigData::kMeterDelayMinMs, 100);
    QCOMPARE(AppConfigData::kMeterDelayMaxMs, 600);
}

// [ORION_METER_DELAY_AVAILABILITY 2026-08-08] Regression pin for the ship-blocker:
// the delay backend (NexusVisionSvc / nexus_svc.py / WinDivert) can be absent — an
// older install, a partial package, or a failed installer service registration; the
// current installer does ship+register NexusVisionSvc, but the card MUST surface
// backend availability instead of letting the toggle silently do nothing wherever
// that registration is missing. MeterConfigPanel.qml binds
// orion.meterDelayBackendAvailable (the unavailable banner) and
// orion.meterDelayStatusText (the live actuator state line); as with the three
// settings properties above, nothing in the build fails if these Q_PROPERTYs vanish,
// so this pins them at the meta-object level.
void MeterDelaySettingsPropertyTests::meterDelayAvailabilityContractForQml()
{
    const QMetaObject& meta = OrionAppController::staticMetaObject;

    const struct {
        const char* name;
        QMetaType::Type type;
        const char* notify;
    } expectations[] = {
        {"meterDelayBackendAvailable", QMetaType::Bool,
         "meterDelayBackendAvailabilityChanged"},
        {"meterDelayStatusText", QMetaType::QString, "meterDelayStatusTextChanged"},
        // [ORION_METER_DELAY_ARM_STATE 2026-08-08] The service's own arm report. The
        // bridge backend can be PRESENT and CONNECTED while the intercept is disarmed
        // (nexus_svc ships disarmed; arming is out of band), and without this
        // property the card displayed "Armed" against a service applying nothing.
        {"meterDelayServiceDisarmed", QMetaType::Bool, "meterDelayStatusTextChanged"},
    };

    for (const auto& expected : expectations) {
        const int index = meta.indexOfProperty(expected.name);
        QVERIFY2(index >= 0,
                 qPrintable(QStringLiteral(
                     "Q_PROPERTY \"%1\" is missing from OrionAppController — without it "
                     "the Meter Delay card cannot tell the customer the delay backend "
                     "is absent, and the toggle silently lies (the meter-delay "
                     "ship-blocker).")
                     .arg(QLatin1String(expected.name))));

        const QMetaProperty property = meta.property(index);
        QCOMPARE(property.typeId(), static_cast<int>(expected.type));
        QVERIFY2(property.isReadable(),
                 qPrintable(QStringLiteral("%1 must be readable")
                     .arg(QLatin1String(expected.name))));
        QVERIFY2(!property.isWritable(),
                 qPrintable(QStringLiteral(
                     "%1 is a runtime fact about this install; QML must not be able "
                     "to assign it").arg(QLatin1String(expected.name))));
        QVERIFY2(property.hasNotifySignal(),
                 qPrintable(QStringLiteral("%1 must notify or the card never refreshes")
                     .arg(QLatin1String(expected.name))));
        QCOMPARE(QByteArray(property.notifySignal().name()),
                 QByteArray(expected.notify));
    }
}

// Behavior of the availability decision itself (the exact predicate the QML property
// reads through probePacketBridgeAvailability). With no backend anywhere the UI state
// must report unavailable; with the service registered OR the debug script present in
// either launch root it must report available. Static seam — never constructs a
// controller (see the file header for why).
void MeterDelaySettingsPropertyTests::meterDelayBackendPresenceProbe()
{
    QTemporaryDir rootDir;
    QTemporaryDir appDir;
    QVERIFY(rootDir.isValid());
    QVERIFY(appDir.isValid());

    // Customer install: no service, no script anywhere -> unavailable. This is the
    // verdict that drives the card's warning banner.
    QVERIFY2(!OrionAppController::packetBridgeBackendPresent(
                 rootDir.path(), appDir.path(), /*serviceInstalled=*/false),
             "no NexusVisionSvc and no nexus_svc.py must report the backend absent");

    // Operator ran install_nexus_service.bat: the registered service alone is enough.
    QVERIFY(OrionAppController::packetBridgeBackendPresent(
        rootDir.path(), appDir.path(), /*serviceInstalled=*/true));

    // Dev rig: the debug script in the launch root (startPacketBridgeDebug()'s first
    // candidate) is enough without the service.
    {
        QFile script(rootDir.path() + QStringLiteral("/nexus_svc.py"));
        QVERIFY(script.open(QIODevice::WriteOnly));
        script.write("# packet bridge debug script (test fixture)\n");
        script.close();
    }
    QVERIFY(OrionAppController::packetBridgeBackendPresent(
        rootDir.path(), appDir.path(), /*serviceInstalled=*/false));
    QVERIFY(QFile::remove(rootDir.path() + QStringLiteral("/nexus_svc.py")));

    // ...and the second candidate location (beside the executable) also counts.
    {
        QFile script(appDir.path() + QStringLiteral("/nexus_svc.py"));
        QVERIFY(script.open(QIODevice::WriteOnly));
        script.write("# packet bridge debug script (test fixture)\n");
        script.close();
    }
    QVERIFY(OrionAppController::packetBridgeBackendPresent(
        rootDir.path(), appDir.path(), /*serviceInstalled=*/false));
}

// [ORION_METER_DELAY_ARM_STATE 2026-08-08] The card's status line must distinguish at
// least: backend absent / bridge connected but the service DISARMED / armed and
// applying. Before this, the actuator's ramp book-keeping alone decided the text, so
// a dev rig with nexus_svc running disarmed showed "Active — holding 250 ms" while
// the service refused every verb and held nothing. Static seam — never constructs a
// controller (see the file header for why).
void MeterDelaySettingsPropertyTests::meterDelayStatusLineIsHonestAcrossArmStates()
{
    using State = orion::NetworkBridge::MeterDelayServiceState;
    using Session = orion::MeterDelayController::SessionState;
    using Shot = orion::MeterDelayController::ShotState;

    // One fixed actuator picture for every case: session Ready, shot Locked at the
    // shipped 250 ms default. Only the service-side facts vary, so any difference in
    // the produced text is attributable to the arm state alone.
    OrionAppController::MeterDelayStatusInputs in;
    in.enabled = true;
    in.bridgeConnected = true;
    in.bridgeLinkConfigured = true;
    in.session = Session::Ready;
    in.shot = Shot::Locked;
    in.appliedMs = 250.0;
    in.targetMs = 250.0;
    in.reason = QStringLiteral("Holding");

    // 1. Backend absent (customer install): the existing ship-blocker verdict wins.
    in.backendAvailable = false;
    in.serviceState = State::Armed;
    const QString absent = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(absent.contains(QStringLiteral("Not available on this install")),
             qPrintable(absent));

    // 2. Bridge connected but the service reports DISARMED: the identical Locked@250
    // actuator state must NOT read as active.
    in.backendAvailable = true;
    in.serviceState = State::Disarmed;
    const QString disarmed = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(disarmed.contains(QStringLiteral("DISARMED")), qPrintable(disarmed));
    QVERIFY2(disarmed.contains(QStringLiteral("no delay is being applied")),
             "the disarmed line must state the consequence, not just the state");
    QVERIFY2(!disarmed.contains(QStringLiteral("Active")),
             "a disarmed service must never be reported as applying the delay");

    // 3. Armed and applying: the ramp state is finally allowed to speak.
    in.serviceState = State::Armed;
    const QString armed = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(armed.contains(QStringLiteral("Active — holding 250 ms")),
             qPrintable(armed));

    // The three verdicts must be pairwise distinct or the card cannot distinguish
    // the states at all.
    QVERIFY(absent != disarmed);
    QVERIFY(absent != armed);
    QVERIFY(disarmed != armed);

    // [ORION_METER_DELAY_ECHO] When the service has echoed what it is applying, the
    // Locked line quotes the service's own number beside the controller's.
    in.serviceAppliedMs = 204.0;
    const QString echoed = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(echoed.contains(QStringLiteral("Active — holding 250 ms")),
             qPrintable(echoed));
    QVERIFY2(echoed.contains(QStringLiteral("service reports 204 ms applied")),
             qPrintable(echoed));
    in.serviceAppliedMs = -1.0;

    // Supporting honesty states around the same actuator picture:
    // bridge not connected yet (service present, link on) must not read as active...
    in.serviceState = State::Unknown;
    in.bridgeConnected = false;
    const QString waiting = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(waiting.contains(QStringLiteral("no delay is being applied")),
             qPrintable(waiting));
    QVERIFY(!waiting.contains(QStringLiteral("Active")));

    // ...and a link that is configured OFF must say so instead of waiting forever.
    in.bridgeLinkConfigured = false;
    const QString linkOff = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(linkOff.contains(QStringLiteral("network features are disabled")),
             qPrintable(linkOff));

    // An older service that cannot report arming: "cannot confirm", never "Active".
    in.bridgeConnected = true;
    in.bridgeLinkConfigured = true;
    const QString older = OrionAppController::meterDelayStatusLine(in);
    QVERIFY2(older.contains(QStringLiteral("cannot confirm")), qPrintable(older));
    QVERIFY(!older.contains(QStringLiteral("Active")));

    // The user's own OFF still outranks everything downstream of it.
    in.enabled = false;
    in.serviceState = State::Disarmed;
    QCOMPARE(OrionAppController::meterDelayStatusLine(in), QStringLiteral("Off."));
}

// [ORION_METER_DELAY_LINK 2026-08-08, VENICENET WAVE 1] The predicate shared by the
// constructor start, ensurePacketBridgeRunning() and meterDelayStatusText(). The
// passive-sniffing opt-in flag is DELETED (owner decision: everything network-side
// ships on by default), so the predicate is now networkEnabled || meterDelayEnabled
// — and Meter Delay alone still configures the link, keeping the headline feature
// self-sufficient (the trap the 3-arg predicate originally closed).
void MeterDelaySettingsPropertyTests::meterDelayAloneConfiguresTheBridgeLink()
{
    // Meter Delay ON configures the link BY ITSELF, whatever the network flag says.
    QVERIFY(OrionAppController::packetBridgeLinkConfigured(
        /*networkEnabled=*/false, /*meterDelay=*/true));
    QVERIFY(OrionAppController::packetBridgeLinkConfigured(true, true));

    // The network feature alone also brings the link up (capture diagnostics ride
    // it now that the opt-in sub-flag is gone).
    QVERIFY(OrionAppController::packetBridgeLinkConfigured(true, false));

    // Nothing wants the bridge -> the link stays down.
    QVERIFY(!OrionAppController::packetBridgeLinkConfigured(false, false));
}

// [ORION_METER_DELAY_ECHO 2026-08-08] The display-only echo restored with the
// relaunched feature (its handler was deleted in 968b127f and only the first two
// comment lines survived in handleLine). The service's snapshot rides both the
// {"event":"meter_delay"} state broadcast and every delay-verb ack; either one is
// proof the intercept exists service-side, i.e. the service is ARMED.
void MeterDelaySettingsPropertyTests::serviceEchoParsesDedupesAndConfirmsArmed()
{
    orion::NetworkBridge bridge;  // no start(): handleLine is fed directly
    QSignalSpy echoSpy(&bridge, &orion::NetworkBridge::meterDelayServiceEcho);
    QSignalSpy stateSpy(&bridge, &orion::NetworkBridge::meterDelayServiceStateChanged);
    QVERIFY(echoSpy.isValid());
    QVERIFY(stateSpy.isValid());

    // Pre-auth lines must not drive the card (nexus_svc only broadcasts to authed
    // clients; anything arriving earlier is not a trustworthy actuator state).
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"meter_delay\",\"meter_delay_active\":true,"
        "\"meter_delay_ms\":100.0,\"meter_buffer_depth\":3}"));
    QCOMPARE(echoSpy.count(), 0);
    QCOMPARE(bridge.meterDelayServiceState(),
             orion::NetworkBridge::MeterDelayServiceState::Unknown);

    bridge.ingestLineForTesting(QByteArrayLiteral("{\"event\":\"ack\",\"cmd\":\"auth\"}"));

    // State broadcast -> echo emitted AND the arm state is confirmed Armed.
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"meter_delay\",\"state\":\"started\",\"reason\":\"client_request\","
        "\"meter_delay_active\":true,\"meter_delay_ms\":204.5,\"meter_buffer_depth\":7}"));
    QCOMPARE(echoSpy.count(), 1);
    QCOMPARE(echoSpy.last().at(0).toBool(), true);
    QCOMPARE(echoSpy.last().at(1).toDouble(), 204.5);
    QCOMPARE(echoSpy.last().at(2).toInt(), 7);
    QCOMPARE(bridge.meterDelayServiceState(),
             orion::NetworkBridge::MeterDelayServiceState::Armed);
    QVERIFY(stateSpy.count() >= 1);

    // An identical snapshot on a delay-verb ack is deduped (the keepalive stream
    // re-acks every ~150 ms; the GUI thread must not be woken for no change).
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"ack\",\"cmd\":\"set_meter_delay\",\"meter_delay_active\":true,"
        "\"meter_delay_ms\":204.5,\"meter_buffer_depth\":7}"));
    QCOMPARE(echoSpy.count(), 1);

    // A changed applied value on the ack stream IS re-emitted.
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"ack\",\"cmd\":\"set_meter_delay\",\"meter_delay_active\":true,"
        "\"meter_delay_ms\":209.5,\"meter_buffer_depth\":6}"));
    QCOMPARE(echoSpy.count(), 2);
    QCOMPARE(echoSpy.last().at(1).toDouble(), 209.5);

    // Malformed snapshots (out-of-envelope delay, negative depth, wrong types)
    // never reach the card.
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"meter_delay\",\"meter_delay_active\":true,"
        "\"meter_delay_ms\":5000.0,\"meter_buffer_depth\":1}"));
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"meter_delay\",\"meter_delay_active\":true,"
        "\"meter_delay_ms\":100.0,\"meter_buffer_depth\":-2}"));
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"meter_delay\",\"meter_delay_active\":\"yes\","
        "\"meter_delay_ms\":100.0,\"meter_buffer_depth\":1}"));
    QCOMPARE(echoSpy.count(), 2);

    // An ack with no snapshot (older service) is not an error and not an echo.
    bridge.ingestLineForTesting(QByteArrayLiteral(
        "{\"event\":\"ack\",\"cmd\":\"stop_meter_intercept\"}"));
    QCOMPARE(echoSpy.count(), 2);
}

// [ORION_LEAD_CONFLICT 2026-08-08] The Shot Lead x Tip Timing conflict surface: ShotLeadCard.qml
// binds orion.shotLeadMaxUsableMs / shotLeadConflictMisses / leadAuthority* and TipTimingCard.qml
// binds orion.tipTimingMeasuredMs. As with the meter-delay properties, nothing in the build fails
// when one vanishes — and these are the two warnings that would have saved the 2026-08-08 lost
// session, so they get the same meta-object pin.
void MeterDelaySettingsPropertyTests::shotLeadConflictSurfaceContractForQml()
{
    const QMetaObject& meta = OrionAppController::staticMetaObject;

    const struct {
        const char* name;
        QMetaType::Type type;
        const char* notify;
    } expectations[] = {
        {"shotLeadMaxUsableMs", QMetaType::Double, "tipTimingChanged"},
        {"shotLeadConflictMisses", QMetaType::Int, "leadDiagnosticsChanged"},
        {"leadAuthorityMs", QMetaType::Double, "leadDiagnosticsChanged"},
        {"leadAuthoritySdMs", QMetaType::Double, "leadDiagnosticsChanged"},
        {"leadAuthoritySamples", QMetaType::Int, "leadDiagnosticsChanged"},
        {"tipTimingMeasuredMs", QMetaType::Double, "tipTimingChanged"},
        // [ORION_BANNER_LEAD_TRIM 2026-09-15] The banner closed loop's caption binds these two.
        // Same argument as the pair above: nothing in the build fails when a Q_PROPERTY vanishes,
        // and without them the owner silently loses the only surface that says what the loop has
        // added on top of the value they set.
        {"bannerLeadTrimMs", QMetaType::Double, "bannerLeadTrimChanged"},
        {"bannerLeadTrimEnabled", QMetaType::Bool, "bannerLeadTrimChanged"},
        // [ORION_LEAD_AUTO_SEED 2026-09-15] The plug-and-play Shot Lead's caption binds these
        // five. Losing them is worse than losing a warning: an untuned install would fly a lead
        // nobody set and say nothing at all about where it came from.
        {"leadAutoSeedActive", QMetaType::Bool, "leadAutoSeedChanged"},
        {"leadAutoSeedMs", QMetaType::Double, "leadAutoSeedChanged"},
        {"leadAutoSeedKind", QMetaType::QString, "leadAutoSeedChanged"},
        {"leadAutoSeedMeasuredMs", QMetaType::Double, "leadAutoSeedChanged"},
        {"leadAutoSeedMarginMs", QMetaType::Double, "leadAutoSeedChanged"},
    };

    for (const auto& expected : expectations) {
        const int index = meta.indexOfProperty(expected.name);
        QVERIFY2(index >= 0,
                 qPrintable(QStringLiteral(
                     "Q_PROPERTY \"%1\" is missing from OrionAppController — the Shot Lead / "
                     "Tip Timing cards bind orion.%1, so without it the conflict warning that "
                     "cost the 2026-08-08 session goes silent again.")
                     .arg(QLatin1String(expected.name))));

        const QMetaProperty property = meta.property(index);
        QCOMPARE(property.typeId(), static_cast<int>(expected.type));
        QVERIFY2(property.isReadable(),
                 qPrintable(QStringLiteral("%1 must be readable")
                     .arg(QLatin1String(expected.name))));
        QVERIFY2(!property.isWritable(),
                 qPrintable(QStringLiteral(
                     "%1 is derived/engine-fed state; QML must not be able to assign it")
                     .arg(QLatin1String(expected.name))));
        QVERIFY2(property.hasNotifySignal(),
                 qPrintable(QStringLiteral("%1 must notify or the banner never refreshes")
                     .arg(QLatin1String(expected.name))));
        QCOMPARE(QByteArray(property.notifySignal().name()),
                 QByteArray(expected.notify));
    }
}

// The 30ms margin must mirror the engine's kTipLeadScheduleMarginMs (AutomationEngine.cpp).
// If the engine constant moves, this pin forces whoever moved it to revisit the UI mirror
// instead of letting the annotation drift from the abort behaviour.
void MeterDelaySettingsPropertyTests::shotLeadMaxUsableMirrorsEngineMargin()
{
    // The 2026-08-08 incident numbers: Tip Timing effective 314 -> max usable 284, so the
    // logged Shot Lead of 300/315/320 was unschedulable in every case.
    QCOMPARE(orion::shotLeadMaxUsableForTipTimingMs(314.0), 284.0);
    // Never negative, even for degenerate constants.
    QCOMPARE(orion::shotLeadMaxUsableForTipTimingMs(30.0), 0.0);
    QCOMPARE(orion::shotLeadMaxUsableForTipTimingMs(0.0), 0.0);
}

// [ORION_LEAD_CONFLICT 2026-08-08] The Activity-log rendering of the frozen grader's dead-top
// artifact. grader_truth=0 alone was not enough: a reader still sees "verdict=LATE" first and
// acts on the word — exactly the misread that started the lost session. The artifact line must
// lead with the qualifier, keep the forensic verdict word inside it, and keep grader_truth
// machine-parseable; every real grade and every unrelated line must pass byte-identical.
void MeterDelaySettingsPropertyTests::outcomeIdentityArtifactVerdictIsDemoted()
{
    const QString artifact = QStringLiteral(
        "Outcome identity: physical_epoch=17 shot_attempt=3 release_seq=3 verdict=LATE "
        "grader_truth=0");
    const QString rendered = orion::presentEngineDiagnosticLine(artifact);
    QVERIFY2(!rendered.contains(QStringLiteral("verdict=LATE")),
             qPrintable(QStringLiteral("bare verdict word survived: %1").arg(rendered)));
    QVERIFY2(rendered.contains(QStringLiteral("verdict=ungraded-artifact(LATE)")),
             qPrintable(rendered));
    QVERIFY2(rendered.contains(QStringLiteral(" grader_truth=0")),
             "the machine-parseable grader_truth field must survive the rewrite");

    // An authoritative grade (calibration oracle) passes byte-identical.
    const QString real = QStringLiteral(
        "Outcome identity: physical_epoch=34 shot_attempt=19 release_seq=17 verdict=EXCELLENT "
        "grader_truth=1");
    QCOMPARE(orion::presentEngineDiagnosticLine(real), real);

    // Unrelated diagnostics — even ones carrying a verdict token — pass byte-identical.
    const QString other = QStringLiteral(
        "TIP DEADLINE DECISION: disposition=rejected_missed lead_ms=320.000 verdict=LATE");
    QCOMPARE(orion::presentEngineDiagnosticLine(other), other);
}

// [ORION_AIM_FREEZE 2026-08-08] The persistence half of the divergence warning: the measured
// median must round-trip through learning.json in the canonical base-30 physical frame, and
// the load path's [200,500] validation must reject out-of-band values instead of feeding the
// restore-time warning a wrong-frame number. Without the phaseMeasuredMedianUpdated connect
// (whose body this free function is) the key stays empty and the warning dies at restart.
void MeterDelaySettingsPropertyTests::measuredPhaseMedianPersistsCanonically()
{
#ifdef ORION_PRODUCTION_BUILD
    // This fixture intentionally writes settings.json and learning.json. Keep
    // production verification hermetic instead of touching the tester's real
    // per-user timing state.
    QSKIP("settings path redirects to the per-user data dir in production builds");
#endif
    QTemporaryDir dir;
    QVERIFY(dir.isValid());

    AppConfig writer(dir.path());
    writer.load();
    QCOMPARE(writer.learning().measuredPhasePhysicalMs, -1.0);   // fresh: never measured
    // Persist settings.json once so the readers below model a real install (load() reports
    // false while settings.json is absent, independent of learning.json).
    QString error;
    QVERIFY2(writer.save(writer.data(), &error), qPrintable(error));

    // The 2026-08-08 live median. Must survive a fresh process's load.
    orion::persistMeasuredPhaseMedian(writer, 310.2);
    AppConfig reader(dir.path());
    QVERIFY(reader.load());
    QCOMPARE(reader.learning().measuredPhasePhysicalMs, 310.2);

    // Wrong frame (e.g. an error-ms magnitude): saved, but the canonical-range validation on
    // load must return it to "never measured" rather than arming a bogus divergence warning.
    orion::persistMeasuredPhaseMedian(writer, 66.0);
    AppConfig reader2(dir.path());
    QVERIFY(reader2.load());
    QCOMPARE(reader2.learning().measuredPhasePhysicalMs, -1.0);
}

// [ORION_BANNER_VERDICT_LIVE 2026-09-14] The meta-object half of the live tally contract.
// The bucketing/suggestion RULES are pinned in ShotVerdictTallyTests.cpp against the pure
// policy; what can only be checked here is that the controller still publishes the names
// QML binds, on their own low-fanout notifier, with resetBannerTally() invokable.
void MeterDelaySettingsPropertyTests::bannerVerdictTallyPropertiesExistForQml()
{
    const QMetaObject& meta = OrionAppController::staticMetaObject;

    const struct {
        const char* name;
        QMetaType::Type type;
    } expectations[] = {
        {"bannerGreen10", QMetaType::Int},
        {"bannerEarly10", QMetaType::Int},
        {"bannerLate10", QMetaType::Int},
        {"bannerOther10", QMetaType::Int},
        {"bannerCount10", QMetaType::Int},
        {"bannerContested10", QMetaType::Int},
        {"bannerLastTiming", QMetaType::QString},
        {"bannerLastCoverage", QMetaType::QString},
        {"bannerPattern10", QMetaType::QString},
        {"bannerSuggestion", QMetaType::QString},
    };

    for (const auto& expected : expectations) {
        const int index = meta.indexOfProperty(expected.name);
        QVERIFY2(index >= 0,
                 qPrintable(QStringLiteral(
                     "Q_PROPERTY \"%1\" is missing from OrionAppController - "
                     "components/ShotVerdictTally.qml binds orion.%1 from inside both "
                     "NoMeterCard and ShotLeadCard, so without it the live shot tally "
                     "renders empty forever while the sidecar keeps grading banners.")
                     .arg(QLatin1String(expected.name))));
        const QMetaProperty property = meta.property(index);
        QCOMPARE(property.typeId(), static_cast<int>(expected.type));
        QVERIFY(property.isReadable());
        // Read-only by construction: the tally is EVIDENCE. QML must never be able to
        // write a count.
        QVERIFY2(!property.isWritable(),
                 qPrintable(QStringLiteral("%1 must stay read-only")
                                .arg(QLatin1String(expected.name))));
        QVERIFY2(property.hasNotifySignal(),
                 qPrintable(QStringLiteral("%1 must notify or the tally never refreshes")
                                .arg(QLatin1String(expected.name))));
        // Its OWN notifier, not statusChanged: one signal per graded shot must not drag
        // the ~98-property broad status fan-out through the GUI thread mid-session.
        QCOMPARE(QByteArray(property.notifySignal().name()),
                 QByteArrayLiteral("bannerTallyChanged"));
    }

    // The card's Reset link, and the automatic clear behind every committed slider change.
    QVERIFY2(meta.indexOfMethod("resetBannerTally()") >= 0,
             "resetBannerTally() must stay Q_INVOKABLE - ShotVerdictTally.qml calls it");
    const QMetaMethod reset = meta.method(meta.indexOfMethod("resetBannerTally()"));
    QCOMPARE(reset.methodType(), QMetaMethod::Method);

    // The three setters that must silently restart the count, so the tally can never
    // straddle a value change. Their presence is what the auto-reset hangs off.
    for (const char* setter : {"actuationLeadMs", "noMeterHoldMs", "noMeterFadeTrimMs"}) {
        const int index = meta.indexOfProperty(setter);
        QVERIFY2(index >= 0, setter);
        QVERIFY2(meta.property(index).isWritable(), setter);
    }
}

QTEST_GUILESS_MAIN(MeterDelaySettingsPropertyTests)
#include "MeterDelaySettingsPropertyTests.moc"
