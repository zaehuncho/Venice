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
#include "ActivityLogClipboard.h"
#include "OrderedFileLogSink.h"

#include <QtCore/QDir>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QTemporaryDir>
#include <QtGui/QClipboard>
#include <QtGui/QGuiApplication>
#include <QtTest/QTest>

using namespace orion;

class ActivityFeedPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void actualClipboardFallsBackOnFaultedDiskDrain()
    {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString blockedLogPath = temp.filePath(QStringLiteral("logs/orion_native.log"));
        QVERIFY(QDir().mkpath(blockedLogPath)); // opening a directory as a log always faults
        OrderedFileLogSink::Options options;
        options.retryDelayMs = 5;
        options.shutdownGiveUpMs = 50;
        OrderedFileLogSink sink(blockedLogPath, options);
        QVERIFY(sink.enqueue({QStringLiteral("pending diagnostic event")}));

        QClipboard* clipboard = QGuiApplication::clipboard();
        QVERIFY(clipboard);
        struct ClipboardRestore final {
            QClipboard* clipboard;
            QString original;
            ~ClipboardRestore() { clipboard->setText(original); }
        } restore{clipboard, clipboard->text()};
        clipboard->setText(QStringLiteral("stale clipboard sentinel"));
        const QStringList recentRing = {
            QStringLiteral("recent failure key=ABCD-EFGH-IJKL-MNOP")
        };
        const ActivityLogClipboardCopyResult result = copyActivityLogToClipboard(
            clipboard, sink, blockedLogPath, recentRing);
        const QString copied = clipboard->text();
        sink.stopAndDrain();

        QVERIFY(!result.drained);
        QVERIFY(result.diskIncomplete);
        QVERIFY(result.storageFault);
        QVERIFY(copied.contains(QStringLiteral("Disk log incomplete")));
        QVERIFY(copied.contains(QStringLiteral("recent failure")));
        QVERIFY(!copied.contains(QStringLiteral("ABCD-EFGH-IJKL-MNOP")));
        QVERIFY(!copied.contains(QStringLiteral("stale clipboard sentinel")));
    }

    void partialDiskLogCopiesRecentRedactedRing()
    {
        const QString oldDisk = QStringLiteral("old disk event");
        const QStringList ring = {QStringLiteral("new error key=ABCD-EFGH-IJKL-MNOP")};
        const QString partial = ui_notifications::activityLogCopyForSharing(
            oldDisk, ring, false, true, 0);
        QVERIFY(partial.contains(QStringLiteral("Disk log incomplete")));
        QVERIFY(partial.contains(QStringLiteral("new error")));
        QVERIFY(!partial.contains(QStringLiteral("ABCD-EFGH-IJKL-MNOP")));
        QVERIFY(ui_notifications::activityLogCopyForSharing(oldDisk, ring, true, false, 0)
                    == oldDisk);
        QVERIFY(ui_notifications::activityLogCopyForSharing(oldDisk, ring, true, false, 1)
                    .contains(QStringLiteral("new error")));
    }

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
            // [COPY-FIX 2026-09-23] The watchdog trip reason is customer copy now; the
            // raw "restarting detection sidecar" line is engineering-only (rule 0 below).
            QStringLiteral("Watchdog trip: Video detection stopped mid-stream"),
            QStringLiteral("Safe mode: video detection keeps stopping. Click SAFE MODE at the "
                           "top, then Exit safe mode, or restart Venice."),
            QStringLiteral("Remote Play: Error - Venice couldn't reconnect your controller "
                           "(code RP-07). Press Connect."),
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

    // [COPY-FIX 2026-09-23 NEW-A6] Rule 0: lease / sidecar / ORION_ lines stay in the
    // disk log but never reach the customer feed, even when they also carry an
    // allow-listed word ("watchdog", "safe mode", "stream warm-up hiccup", "controller").
    void internalOnlyLinesNeverReachTheCustomerFeed()
    {
        const QStringList internal = {
            QStringLiteral("Lease-gated fire ENABLED (ORION_LEASE_GATED_FIRE): automation "
                           "requires a live server lease (max staleness 15 min)."),
            QStringLiteral("Fire lease seeded by activation: Lease valid (900 s left)"),
            QStringLiteral("Stream warm-up hiccup (sidecar exited during startup, attempt 1/4) "
                           "- restarting quietly, no safe mode."),
            QStringLiteral("Watchdog: restarting detection sidecar."),
            QStringLiteral("Watchdog: restarting detection sidecar (capture transport failure)."),
            QStringLiteral("SAFE MODE: sidecar exited - restarting it (1/3) so the stream can re-heal; "
                           "automation stays disarmed until the safe-mode stability window passes."),
            QStringLiteral("SAFE MODE: sidecar exited again after 3 restarts - not restarting "
                           "(crash loop). Manual reset required."),
            QStringLiteral("SAFE MODE just latched: restarting the detection sidecar (1/3) so the "
                           "stream can re-heal; automation stays disarmed."),
            QStringLiteral("Input-only recovery started: controller/fire authority revoked; existing "
                           "sidecar, capture, detector, and learned timing retained."),
            QStringLiteral("User log ENABLED (ORION_USER_LOG): plain-language events -> logs/orion_user.log"),
            QStringLiteral("Remote Play engine detail: Failed to launch autogreen sidecar: boom"),
        };
        for (const QString& line : internal) {
            QVERIFY2(ui_notifications::isInternalOnlyLine(line), qPrintable(line));
            QVERIFY2(!ui_notifications::shouldEnterActivityRing(line), qPrintable(line));
        }
        // "lease" alone is NOT a marker: every shot line says "release".
        QVERIFY(!ui_notifications::isInternalOnlyLine(QStringLiteral(
            "Release command accepted by the local controller route - meter at 96%.")));
        QVERIFY(ui_notifications::shouldEnterActivityRing(QStringLiteral(
            "Release command accepted by the local controller route - meter at 96%.")));
        QVERIFY(ui_notifications::shouldEnterActivityRing(QStringLiteral(
            "Capture card refresh rate set to 60 fps (applies the next time you connect)")));
    }

    // [COPY-FIX 2026-09-23 NEW-A5 / EA-25] Raw RemotePlaySession statuses map to coded,
    // plain customer copy; plain statuses pass through untouched.
    void remoteStatusMapsEngineErrorsToCodedCustomerCopy()
    {
        using ui_notifications::customerRemoteStatus;
        const struct { const char* raw; const char* code; } coded[] = {
            {"Production package is incomplete: required meter detector model not found", "RP-05"},
            {"Failed to launch autogreen sidecar: The system cannot find the file specified.", "RP-06"},
            {"Autogreen sidecar CRASHED (code 3)", "RP-06"},
            {"Live capture preview stopped: sidecar died (code 1)", "RP-06"},
            {"Stream start did not confirm - no input link to the console. Disconnect and try again.", "RP-08"},
            {"Remote Play input session was not proven ready; automation remains disabled.", "RP-08"},
            {"Chiaki input recovery blocked: trusted client image unavailable", "RP-07"},
            {"Chiaki input recovery command could not reach the sidecar.", "RP-07"},
            {"Chiaki input recovery did not prove a current console session; automation remains disabled.", "RP-07"},
            {"Recovered input child did not prove a fresh console session.", "RP-07"},
            {"Chiaki input recovery failed.", "RP-07"},
            {"Autogreen error", "RP-09"},
            {"Some future sidecar wording nobody mapped", "RP-09"},
        };
        for (const auto& c : coded) {
            const QString out = customerRemoteStatus(QString::fromLatin1(c.raw));
            QVERIFY2(out.contains(QLatin1String(c.code)), qPrintable(out));
            for (const char* jargon : {"sidecar", "Chiaki", "autogreen", "input child", "ORION_"}) {
                QVERIFY2(!out.contains(QLatin1String(jargon), Qt::CaseInsensitive), qPrintable(out));
            }
        }
        QCOMPARE(customerRemoteStatus(QStringLiteral("Console IP is required before starting Chiaki")),
                 QStringLiteral("Enter your PS5's IP address in Setup, then press Connect."));
        QCOMPARE(customerRemoteStatus(QStringLiteral("Starting Chiaki autogreen sidecar")),
                 QStringLiteral("Starting\u2026"));
        QCOMPARE(customerRemoteStatus(QStringLiteral("Autogreen running - meter detection active")),
                 QStringLiteral("Connected \u2014 meter detection active"));
        // Plain statuses, including the existing coded copy, pass through unchanged.
        for (const QString& plain : {
                 QStringLiteral("Disconnected"),
                 QStringLiteral("Waking the console from rest mode..."),
                 QStringLiteral("Live capture preview"),
                 QStringLiteral("Part of Venice is missing from this install (code RP-01). "
                                "Reinstall Venice from the latest download.")}) {
            QCOMPARE(customerRemoteStatus(plain), plain);
        }
    }

    // [P-E 2026-09-23 CL3-F8-001/003/004/006, RT-LOW-01, RT-MED-09/10] Customer lines the P-E
    // patch writes reach the feed; their engineering twins never do; RP-10 wins over RP-09.
    void peCustomerStateLinesAndTheirEngineeringTwins()
    {
        using ui_notifications::customerRemoteStatus;
        using ui_notifications::shouldEnterActivityRing;
        const QString rp10 = QStringLiteral("Another device is using Remote Play on this PS5 (code RP-10). "
                                            "Close Remote Play there, then press Connect.");
        for (const QString& raw : {
                 QStringLiteral("RP_IN_USE: Remote is already in use (0x80108b10) - Chiaki session quit"),
                 QStringLiteral("Chiaki session quit: 80108b10"),
                 QStringLiteral("Remote is already in use")}) {
            QCOMPARE(customerRemoteStatus(raw), rp10);
        }
        const QStringList shown = {
            ui_notifications::settingsRepairCustomerText(),
            QStringLiteral("Watchdog trip: The capture card stopped sending video"),
            QStringLiteral("SAFE MODE: The capture card stopped sending video. Shots are paused. Venice usually "
                           "turns them back on by itself once the stream has been steady for 30 seconds, or "
                           "click SAFE MODE at the top, then Exit safe mode."),
            QStringLiteral("Feed paused: Venice is minimized, so shots are paused. They resume when you bring "
                           "the Venice window back."),
            QStringLiteral("Venice resumed from sleep. If the picture or your controller doesn't come back, "
                           "press Disconnect, then Connect."),
            QStringLiteral("Settings repaired: Venice is back on its default settings. Your shot timing history "
                           "was kept. Check Shot Lead and your meter style, then press Connect."),
            QStringLiteral("Remote Play: ") + rp10,
        };
        for (const QString& line : shown) {
            QVERIFY2(shouldEnterActivityRing(line), qPrintable(line));
            QVERIFY2(!line.contains(QLatin1String("Orion")), qPrintable(line));
            QVERIFY2(!line.contains(QLatin1String("frozen=")) && !line.contains(QLatin1String(" ms)")),
                     qPrintable(line));
        }
        const QStringList hidden = {
            QStringLiteral("Watchdog engine detail: capture transport failed (transport age 8123 ms, backend "
                           "frozen=1; detector frame age 8120 ms, pixel age 8120 ms)"),
            QStringLiteral("Security engine detail: Remote Play blocked by security lock: Settings signature "
                           "missing or invalid"),
            QStringLiteral("Settings engine detail: interrupted settings write rolled back to the last signed settings"),
            QStringLiteral("Safe mode engine detail: auto-recover budget 2/session"),
        };
        for (const QString& line : hidden) {
            QVERIFY2(!shouldEnterActivityRing(line), qPrintable(line));
        }
    }

    // [COPY-FIX 2026-09-23 NEW-A7] "Shot not taken (%1)." printed the raw abort enum.
    void shotNotTakenNeverPrintsTheRawReasonEnum()
    {
        using ui_notifications::customerShotNotTakenText;
        const QStringList reasons = {
            QStringLiteral("live_tip_deadline_missed"), QStringLiteral("input_timer_late_abort"),
            QStringLiteral("controller_route_changed_abort"), QStringLiteral("precise_fire_delivery_failed"),
            QStringLiteral("meter_structure_unverified_abort"), QStringLiteral("ownership_proof_incomplete"),
            QStringLiteral("no_pose_landmark_abort"), QStringLiteral("automation_disarmed_abort"),
            QStringLiteral("measured_lead_unready_abort"), QStringLiteral("input_timer_manual_cancel"),
            QStringLiteral("something_brand_new"),
        };
        for (const QString& reason : reasons) {
            const QString text = customerShotNotTakenText(reason);
            QVERIFY2(!text.contains(QLatin1Char('_')), qPrintable(text));
            QVERIFY2(!text.contains(reason), qPrintable(text));
            QVERIFY2(ui_notifications::shouldEnterActivityRing(text), qPrintable(text));
        }
        QCOMPARE(customerShotNotTakenText(QStringLiteral("live_tip_deadline_missed")),
                 QStringLiteral("Shot not taken \u2014 Venice couldn't release in time."));
        QCOMPARE(customerShotNotTakenText(QStringLiteral("something_brand_new")),
                 QStringLiteral("Shot not taken."));
        // The raw enum line stays in the disk log only, even when its reason carries
        // the allow-listed word "controller".
        QVERIFY(!ui_notifications::shouldEnterActivityRing(QStringLiteral(
            "Shot automation aborted: controller_route_changed_abort")));
        // Shot Lead numbers are on the card's 1..100 scale (ShotLeadCard.qml valueFromMs).
        QCOMPARE(ui_notifications::shotLeadSliderValue(150.0), 1);
        QCOMPARE(ui_notifications::shotLeadSliderValue(400.0), 100);
        QCOMPARE(ui_notifications::shotLeadSliderValue(275.0), 51);
        QCOMPARE(ui_notifications::shotLeadSliderValue(20.0), 1);
        QCOMPARE(ui_notifications::shotLeadSliderValue(900.0), 100);
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
