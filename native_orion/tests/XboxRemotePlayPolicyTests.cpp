#include "AppConfig.h"
#include "RemotePlaySession.h"
#include "SidecarWatchdog.h"
#include <QtCore/QJsonDocument>
#include <QtCore/QTemporaryDir>
#include <QtTest/QTest>

namespace orion {
class RemotePlaySessionTestAccess final {
public:
    static QJsonObject config(const RemotePlaySession& session)
    { return QJsonDocument::fromJson(session.buildSidecarConfig()).object(); }
};
}
using namespace orion;

class XboxRemotePlayPolicyTests : public QObject {
    Q_OBJECT
private slots:
    void xboxWindowAndBothLeadStashesRoundTrip()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        AppConfig cfg(dir.path());
        AppConfigData data;
        data.videoSource = QStringLiteral("capture_card");
        data.actuationLeadMs = 274.0;
        data.actuationLeadUserSet = true;
        switchRemotePlayConsole(data, QStringLiteral("Xbox"));
        data.actuationLeadMs = 231.0;
        data.actuationLeadUserSet = true;
        data.xboxRemotePlayWindowTitle = QStringLiteral("Xbox - Remote Play");
        QVERIFY(cfg.save(data));
        AppConfig loaded(dir.path());
        QVERIFY(loaded.load());
        data = loaded.data();
        QCOMPARE(data.xboxRemotePlayWindowTitle, QStringLiteral("Xbox - Remote Play"));
        QCOMPARE(data.actuationLeadMs, 231.0);
        switchRemotePlayConsole(data, QStringLiteral("PS5"));
        QCOMPARE(data.actuationLeadMs, 274.0);
    }
    void consoleSwitchPreservesPs5SourceAndLead()
    {
        AppConfigData data;
        data.videoSource = QStringLiteral("capture_card");
        data.actuationLeadMs = 274.0;
        data.actuationLeadUserSet = true;
        switchRemotePlayConsole(data, QStringLiteral("Xbox"));
        QCOMPARE(data.videoSource, QStringLiteral("capture_card"));
        QVERIFY(!isCaptureCardSource(data));
        QCOMPARE(data.actuationLeadMs, 0.0);
        QVERIFY(!data.actuationLeadUserSet);
        data.actuationLeadMs = 231.0;
        data.actuationLeadUserSet = true;
        switchRemotePlayConsole(data, QStringLiteral("PS5"));
        QVERIFY(isCaptureCardSource(data));
        QCOMPARE(data.actuationLeadMs, 274.0);
        QVERIFY(data.actuationLeadUserSet);
        switchRemotePlayConsole(data, QStringLiteral("Xbox"));
        QCOMPARE(data.actuationLeadMs, 231.0);
    }
    void sameConsoleDoesNotResetTuning()
    {
        AppConfigData data;
        data.actuationLeadMs = 274.0;
        switchRemotePlayConsole(data, QStringLiteral("PS5"));
        QCOMPARE(data.actuationLeadMs, 274.0);
    }
    void xboxConfigNeverOwnsChiakiOrPs5Identity()
    {
        AppConfigData data;
        data.remotePlayConsole = QStringLiteral("Xbox");
        data.xboxRemotePlayWindowTitle = QStringLiteral("Xbox - Remote Play");
        data.remotePlayConsoleIp = QStringLiteral("192.0.2.10");
        data.videoSource = QStringLiteral("capture_card");
        RemotePlaySession session;
        session.applyConfig(data);
        const auto obj = RemotePlaySessionTestAccess::config(session);
        QCOMPARE(obj["platform"].toString(), QStringLiteral("xbox"));
        QCOMPARE(obj["client_mode"].toString(), QStringLiteral("external"));
        QCOMPARE(obj["frame_source"].toString(), QStringLiteral("wgc"));
        QCOMPARE(obj["window_title"].toString(), data.xboxRemotePlayWindowTitle);
        QVERIFY(!obj["auto_launch_client"].toBool(true));
        QVERIFY(!obj["close_client_on_disconnect"].toBool(true));
        QVERIFY(obj["console_ip"].toString().isEmpty());
        QVERIFY(obj["console_identity"].toString().isEmpty());
        QVERIFY(obj["chiaki_path"].toString().isEmpty());
        QVERIFY(!session.recoverInputLink());
    }
    void ps5ConfigKeepsExistingSource()
    {
        for (const auto& source : {QStringLiteral("capture_card"), QStringLiteral("decoder")}) {
            AppConfigData data;
            data.videoSource = source;
            RemotePlaySession session;
            session.applyConfig(data);
            const auto obj = RemotePlaySessionTestAccess::config(session);
            QCOMPARE(obj["platform"].toString(), QStringLiteral("ps5"));
            QCOMPARE(obj["client_mode"].toString(), QStringLiteral("chiaki"));
            QCOMPARE(obj["frame_source"].toString(), source);
            QVERIFY(obj["auto_launch_client"].toBool());
            QVERIFY(obj["close_client_on_disconnect"].toBool());
        }
    }
    void xboxRequiresExplicitWindowBeforeStarting()
    {
        AppConfigData data;
        data.remotePlayConsole = QStringLiteral("Xbox");
        RemotePlaySession session;
        session.applyConfig(data);
        session.start();
        QCOMPARE(session.state(), RemotePlayState::Error);
    }
};
QTEST_GUILESS_MAIN(XboxRemotePlayPolicyTests)
#include "XboxRemotePlayPolicyTests.moc"
