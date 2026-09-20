// [ORION_ACTIVITY_FEED / PROFILE 2026-09-14 owner] Two pure policies that decide
// what the CUSTOMER sees, pinned here because neither has a compile-time guard:
//
//   1. ui_notifications::shouldEnterActivityRing() — THE one place the Activity
//      feed is separated from engineering telemetry (OrionAppController::appendLog
//      is its only production caller). The census that motivated it is quoted in
//      UiNotificationPolicy.h; this suite pins the exact templates it measured, so
//      a future edit cannot quietly let 12,000 lines an hour back into the ring —
//      or, just as bad, quietly swallow a human event.
//
//   2. orion::parseLicenseProfile() — the backend `profile` block the Profile page
//      renders. The CURRENT live Lambda does not send one, so the tolerant path
//      (absent/garbled -> zeros, never a fabricated expiry) is the load-bearing case.
#include "LicenseClient.h"
#include "UiNotificationPolicy.h"

#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtTest/QTest>

using namespace orion;

class ActivityFeedPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void censusTelemetryTemplatesNeverReachTheCustomerFeed()
    {
        // Verbatim shapes from the 2026-09-14 census of one hour of
        // logs/orion_native.log, with their measured line counts.
        const QStringList telemetry = {
            QStringLiteral("Sidecar: 2026-09-14 10:00:00 ERROR simple_reader: DETECTOR HEALTH "
                           "frames=900 locks=12 misses=3"),                       // 1105
            QStringLiteral("Input hook heartbeat: connected=1 writes=4880 failures=0"),   // 589
            QStringLiteral("Network bridge: connected - Bridge: [WinError 5] Access is denied."), // 470
            QStringLiteral("Sidecar: preview_stats: fps=60.0 dropped=0"),                 // 445
            QStringLiteral("SHM preview frame read: count=300 frame=41 1280x720 "
                           "source_to_read_ms=1.2"),                              // 445
            QStringLiteral("Telemetry stage split: n=60 emit->receipt p50=2.10ms "
                           "p90=4.40ms max=9.90ms"),                              // 439
            QStringLiteral("qml_preview_pipeline: set_fps=60.0 request_fps=60.0 "
                           "request_gap_max_ms=18.2"),                            // 438
            QStringLiteral("preview_pipeline: transport=shm present_fps=60.0 total=3600"), // 438
            QStringLiteral("Capture health: tier=capture_card fps=60 age_ms=8"),          // 359
            QStringLiteral("IDLE-GATE: reason=no_meter sqLatch=0 rsUpLatch=0"),           // 288
            QStringLiteral("Release submit: seq=41 ok=1 backend=PIPE stage=committed"),   // 88
            QStringLiteral("Self-grade diagnostic (not timing truth): verdict=late "
                           "fill=101.0 seq=41"),                                  // 72
        };
        for (const QString& line : telemetry) {
            QVERIFY2(!ui_notifications::shouldEnterActivityRing(line), qPrintable(line));
        }
    }

    void humanEventsAlwaysReachTheCustomerFeed()
    {
        const QStringList human = {
            // Connection state changes.
            QStringLiteral("Remote Play: Running - Stream is live"),
            QStringLiteral("Auto-reconnect: stream dropped mid-session - reconnecting."),
            QStringLiteral("Disconnect: stream client still alive after graceful shutdown"),
            // Controller route / state.
            QStringLiteral("Controller route: RawInput -> ViGEm -> Chiaki"),
            QStringLiteral("Controller state: Physical Sony pad ready"),
            QStringLiteral("Virtual pad unplugged on disconnect (prevents desktop input hijack)."),
            // Shots, in the plain language the customer feed now carries.
            QStringLiteral("Release command accepted by the local controller route - meter at "
                           "96% (aiming for 100%), Standstill shot."),
            QStringLiteral("Shot release was NOT delivered - the virtual controller "
                           "disconnected; automation stopped for safety."),
            QStringLiteral("Shot not taken (live_tip_deadline_missed)."),
            QStringLiteral("Shot canceled - you moved the stick to change the play."),
            // Capture / console / safety problems.
            QStringLiteral("Capture devices: Elgato HD60 X"),
            QStringLiteral("No PS5/PS4 consoles responded to discovery on the local network."),
            QStringLiteral("SAFE MODE: watchdog tripped - automation disarmed."),
            QStringLiteral("Watchdog: restarting detection sidecar."),
            // Licence / MOTD / first run.
            QStringLiteral("License verified for current device"),
            QStringLiteral("License heartbeat: session disabled (revoked)"),
            QStringLiteral("MOTD (warn, until 2026-09-15T00:00:00): Maintenance tonight"),
            QStringLiteral("First-run preflight check completed."),
            QStringLiteral("Meter calibration complete after 5 shots."),
        };
        for (const QString& line : human) {
            QVERIFY2(ui_notifications::shouldEnterActivityRing(line), qPrintable(line));
        }
    }

    void ruleOrderIsAllowThenDenyThenSidecarThenCounterShapeThenKeep()
    {
        // 1. The allow-list wins even over engineering-shaped detail: a licence
        //    line carrying three key=value pairs is still a customer event.
        QVERIFY(ui_notifications::shouldEnterActivityRing(QStringLiteral(
            "License activation request: key_suffix=ABCD machine_id_len=64 attempt=1")));
        // 2. Deny-list beats the default keep.
        QVERIFY(!ui_notifications::shouldEnterActivityRing(
            QStringLiteral("Release attribution: seq=41 greenConfirmed=1 targetMode=green_tip")));
        // 3. Raw sidecar stdout is a machine stream even without a known template.
        QVERIFY(!ui_notifications::shouldEnterActivityRing(
            QStringLiteral("Sidecar: 2026-09-14 10:00:00 INFO some_new_module: hello")));
        // 4. The counter-line heuristic catches the NEXT template nobody denied:
        //    three or more key=value pairs.
        QVERIFY(!ui_notifications::shouldEnterActivityRing(
            QStringLiteral("Brand new telemetry: alpha=1 beta=2 gamma=3")));
        QVERIFY(ui_notifications::shouldEnterActivityRing(
            QStringLiteral("Brand new telemetry: alpha=1 beta=2")));
        QVERIFY(ui_notifications::looksLikeCounterLine(
            QStringLiteral("a=1 b=2 c=3")));
        QVERIFY(!ui_notifications::looksLikeCounterLine(
            QStringLiteral("Shot Lead: video source Capture -> Remote, lead 275 -> 285")));
        // 5. Fail OPEN. An unknown, prose-shaped line is a customer event until
        //    someone proves otherwise — silently swallowing a new failure notice
        //    is worse than one extra row.
        QVERIFY(ui_notifications::shouldEnterActivityRing(
            QStringLiteral("Something new and human happened")));
        // Blank input never occupies a ring slot.
        QVERIFY(!ui_notifications::shouldEnterActivityRing(QStringLiteral("   ")));
    }

    void rawDiagnosticRingRuleIsUnchangedForTheDebugPage()
    {
        // The Debug page reads the RAW ring, which is still gated only by the
        // per-frame periodic filter. Widening the customer rule must not have
        // widened this one.
        QVERIFY(ui_notifications::isPeriodicMachineDiagnostic(
            QStringLiteral("Capture health: tier=capture_card")));
        QVERIFY(!ui_notifications::isPeriodicMachineDiagnostic(
            QStringLiteral("Release submit: seq=41 ok=1 backend=PIPE")));
        QVERIFY(!ui_notifications::isPeriodicMachineDiagnostic(
            QStringLiteral("Network bridge: connected - Bridge: [WinError 5] Access is denied.")));
    }

    // ── Profile block ────────────────────────────────────────────────────────

    void profileIsAbsentOnTheCurrentLiveBackend()
    {
        // The deployed Lambda answers without `profile`. That must parse to an
        // UNKNOWN block — never to "0 days left", which would read as expired.
        const LicenseResult activate = parseActivateResponse(
            QByteArray(R"({"ok":true,"token":"t","tid":"i","expires":123})"));
        QVERIFY(activate.ok);
        QVERIFY(!activate.profile.known);
        QCOMPARE(activate.profile.expiryEpochS, qint64(0));
        QCOMPARE(activate.profile.hwidResetsFreeTotal, 0);

        const LicenseResult check = parseLicenseCheckResponse(
            QByteArray(R"({"ok":true,"plan":"month","expiry":1800000000})"));
        QVERIFY(check.ok);
        QVERIFY(!check.profile.known);

        // A non-object `profile` is the same as none.
        QVERIFY(!parseLicenseProfile(QJsonValue(QStringLiteral("nope"))).known);
        QVERIFY(!parseLicenseProfile(QJsonValue()).known);
    }

    void profileParsesTheFullBlockFromBothEndpoints()
    {
        const QByteArray payload = QByteArray(R"({
            "ok": true, "token": "t", "tid": "i", "expires": 123,
            "profile": {
                "discord_user_id": "424242424242424242",
                "discord_username": "isaiah",
                "plan": "month",
                "expiry": 1800000000,
                "activated_at": 1700000000,
                "hwid_resets": {"used": 1, "free_total": 3,
                                "free_remaining": 2, "paid_credits": 4}
            }})");
        for (const LicenseResult& result :
             {parseActivateResponse(payload), parseLicenseCheckResponse(payload)}) {
            const LicenseProfile& p = result.profile;
            QVERIFY(p.known);
            QCOMPARE(p.discordUserId, QStringLiteral("424242424242424242"));
            QCOMPARE(p.discordUsername, QStringLiteral("isaiah"));
            QCOMPARE(p.plan, QStringLiteral("month"));
            QCOMPARE(p.expiryEpochS, qint64(1800000000));
            QCOMPARE(p.activatedAtEpochS, qint64(1700000000));
            QCOMPARE(p.hwidResetsUsed, 1);
            QCOMPARE(p.hwidResetsFreeTotal, 3);
            QCOMPARE(p.hwidResetsFreeRemaining, 2);
            QCOMPARE(p.hwidPaidCredits, 4);
            QVERIFY(!p.lifetime());
        }
    }

    void profileTreatsZeroExpiryAsLifetimeNotExpired()
    {
        const LicenseResult result = parseLicenseCheckResponse(QByteArray(
            R"({"ok":true,"profile":{"plan":"lifetime","expiry":0}})"));
        QVERIFY(result.profile.known);
        QVERIFY(result.profile.lifetime());
        QCOMPARE(result.profile.plan, QStringLiteral("lifetime"));
    }

    void profileToleratesStringNumbersAndClampsNonsense()
    {
        const LicenseProfile p = parseLicenseProfile(
            QJsonDocument::fromJson(QByteArray(R"({
                "expiry": "1800000000", "activated_at": "-5",
                "hwid_resets": {"used": 5, "free_total": 3,
                                "free_remaining": 9, "paid_credits": "2"}
            })")).object());
        QVERIFY(p.known);
        QCOMPARE(p.expiryEpochS, qint64(1800000000));   // numeric string accepted
        QCOMPARE(p.activatedAtEpochS, qint64(0));       // negative clamped
        QCOMPARE(p.hwidPaidCredits, 2);
        // free_remaining can never exceed free_total - used, whatever the server said.
        QCOMPARE(p.hwidResetsFreeRemaining, 0);
    }
};

QTEST_MAIN(ActivityFeedPolicyTests)
#include "ActivityFeedPolicyTests.moc"
