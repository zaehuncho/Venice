#include "AppConfig.h"
#include "FrameDecoder.h"
#include "RemotePlaySession.h"
#include "SidecarWatchdog.h"

#include <QtCore/QBuffer>
#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QElapsedTimer>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>
#include <QtCore/QThread>
#include <QtGui/QGuiApplication>
#include <QtTest/QSignalSpy>
#include <QtTest/QTest>

#include <cstdio>

using namespace orion;

namespace orion {
class RemotePlaySessionTestAccess final {
public:
    static void resetPreview(RemotePlaySession& session, bool accept)
    {
        session.resetPreviewPresentation(accept);
    }
    static void publishJpeg(RemotePlaySession& session, const QByteArray& jpeg, int frame)
    {
        session.submitPreviewPayload(jpeg, frame);
    }
    static quint64 decoded(const RemotePlaySession& session)
    {
        return session.frameDecoder_->decodedCount();
    }
    static int queuedFrames(const RemotePlaySession& session)
    {
        return static_cast<int>(session.previewPresentationBuffer_.depth());
    }
    static void renderClock(RemotePlaySession& session)
    {
        session.previewPresentationRenderClockActive_ = true;
    }
    static void inventoryPrepared(RemotePlaySession& session)
    {
        session.captureInventoryPrepared_ = true;
    }
    static CaptureSidecarLaunchIdentity identity(const RemotePlaySession& session)
    {
        return session.activeSidecarLaunchIdentity_;
    }
    static bool promotionPending(const RemotePlaySession& session)
    {
        return session.streamPromotePending_;
    }
    static void setState(RemotePlaySession& session, RemotePlayState state)
    {
        session.setState(state, QStringLiteral("route fixture"));
    }
    static bool requestFrame(RemotePlaySession& session)
    {
        return session.sendSidecarCommand({{QStringLiteral("cmd"), QStringLiteral("emit_frame")}});
    }
};
}

namespace {

QByteArray makeJpeg(const QColor& color)
{
    QImage image(12, 12, QImage::Format_RGB32);
    image.fill(color);
    QByteArray bytes;
    QBuffer buffer(&bytes);
    buffer.open(QIODevice::WriteOnly);
    if (!image.save(&buffer, "JPEG", 90)) return {};
    return bytes.toBase64();
}

// The test binary is copied to OrionSidecar.exe in its isolated test directory.
// This branch is a protocol-only producer: it never opens a card or console.
int runFakeSidecar(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::addLibraryPath(
        QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("../../Release")));
    const int index = qEnvironmentVariableIntValue("ORION_CAPTURE_CARD_INDEX");
    const int frameNumber = 101 + index;
    const QByteArray jpeg = makeJpeg(index == 0 ? QColor(Qt::red) : QColor(Qt::blue));
    if (jpeg.isEmpty()) return 3;
    // Match autogreen_sidecar's fixed wire order. QJsonObject sorts keys and
    // would move frame_number before jpeg_b64; the hot-path scanner deliberately
    // reads frame_number after the payload.
    const QByteArray line = QByteArrayLiteral("{\"event\":\"frame\",\"jpeg_b64\":\"")
        + jpeg + QByteArrayLiteral("\",\"frame_number\":")
        + QByteArray::number(frameNumber) + QByteArrayLiteral("}\n");
    std::fwrite(line.constData(), 1, static_cast<size_t>(line.size()), stdout);
    std::fflush(stdout);
    char command[4096]{};
    while (std::fgets(command, sizeof(command), stdin)) {
        if (QByteArray(command).contains("\"shutdown\"")) return 0;
        if (QByteArray(command).contains("\"emit_frame\"")) {
            std::fwrite(line.constData(), 1, static_cast<size_t>(line.size()), stdout);
            std::fflush(stdout);
        }
    }
    return 0;
}

struct ScopedCompiledSidecar {
    QByteArray old = qgetenv("ORION_COMPILED_SIDECAR");
    bool had = qEnvironmentVariableIsSet("ORION_COMPILED_SIDECAR");
    // [CL3-F2-001 / CL3-F3-004 2026-09-23] The production start gate (correctly) refuses to spawn a
    // sidecar unless models/orion_meter_detector.onnx sits beside the executable. The fake sidecar
    // never loads it, so the production-profile fixture stages a placeholder for the duration of
    // the test and removes only what it created. Dev builds resolve the model elsewhere.
    QString stagedModel;
    ScopedCompiledSidecar()
    {
        qputenv("ORION_COMPILED_SIDECAR", "1");
#ifdef ORION_PRODUCTION_BUILD
        const QString model = QDir(QCoreApplication::applicationDirPath())
                                  .filePath(QStringLiteral("models/orion_meter_detector.onnx"));
        if (!QFileInfo::exists(model)) {
            QDir().mkpath(QFileInfo(model).absolutePath());
            QFile placeholder(model);
            if (placeholder.open(QIODevice::WriteOnly)) {
                placeholder.write("route-transition-fixture-placeholder");
                placeholder.close();
                stagedModel = model;
            }
        }
#endif
    }
    ~ScopedCompiledSidecar()
    {
        had ? qputenv("ORION_COMPILED_SIDECAR", old) : qunsetenv("ORION_COMPILED_SIDECAR");
        if (!stagedModel.isEmpty()) {
            QFile::remove(stagedModel);
        }
    }
};

AppConfigData captureConfig(int index, const QString& id, const QString& fakeClient)
{
    AppConfigData config;
    config.videoSource = QStringLiteral("capture_card");
    config.captureCardIndex = index;
    config.captureCardDeviceId = id;
    config.captureCardFps = 60;
    config.remotePlayConsoleIp = QStringLiteral("127.0.0.1");
    config.chiakiPath = fakeClient;
    return config;
}

} // namespace

class RouteTransitionIntegrationTests final : public QObject {
    Q_OBJECT
private slots:
    void retiredDecoderImageCannotEnterNewProducerNamespace()
    {
        RemotePlaySession session;
        RemotePlaySessionTestAccess::resetPreview(session, true);
        RemotePlaySessionTestAccess::renderClock(session);
        const QByteArray jpeg = makeJpeg(QColor(Qt::red));
        QVERIFY(!jpeg.isEmpty());
        RemotePlaySessionTestAccess::publishJpeg(session, jpeg, 101);
        // Hold the owner event loop while the worker decodes A. Its queued delivery
        // has not run yet, so the A->B boundary is deterministic rather than a race.
        QElapsedTimer deadline;
        deadline.start();
        while (RemotePlaySessionTestAccess::decoded(session) == 0 && deadline.elapsed() < 2000)
            QThread::msleep(2);
        QCOMPARE(RemotePlaySessionTestAccess::decoded(session), quint64(1));
        RemotePlaySessionTestAccess::resetPreview(session, false);
        RemotePlaySessionTestAccess::resetPreview(session, true);
        RemotePlaySessionTestAccess::renderClock(session);
        QCoreApplication::processEvents();
        QCOMPARE(RemotePlaySessionTestAccess::queuedFrames(session), 0);
    }

    void warmPreviewASelectedBConnectReleasesAndColdStartsB()
    {
        ScopedCompiledSidecar compiled;
        QTemporaryDir fixture;
        QVERIFY(fixture.isValid());
        const QString fakeSidecar = QCoreApplication::applicationDirPath() + QStringLiteral("/OrionSidecar.exe");
        QVERIFY2(QFileInfo::exists(fakeSidecar), qPrintable(fakeSidecar));
        const QString fakeClient = fixture.filePath(QStringLiteral("fake-client.exe"));
        QFile client(fakeClient);
        QVERIFY(client.open(QIODevice::WriteOnly));
        QCOMPARE(client.write("fixture-client-image"), qint64(20));
        client.close();

        RemotePlaySession session;
        session.setRootDir(fixture.path());
        auto config = captureConfig(0, QStringLiteral("A"), fakeClient);
        session.applyConfig(config);
        QSignalSpy frames(&session, &RemotePlaySession::frameReady);
        RemotePlaySessionTestAccess::inventoryPrepared(session);
        session.startCapturePreview();
        QTRY_VERIFY_WITH_TIMEOUT(session.sidecarPid() > 0, 3000);
        const qint64 producerA = session.sidecarPid();
        const auto sawFrame = [&frames](int number) {
            for (const auto& record : frames)
                if (record.at(1).toInt() == number) return true;
            return false;
        };
        QTRY_VERIFY_WITH_TIMEOUT(sawFrame(101), 3000);
        frames.clear();

        config = captureConfig(1, QStringLiteral("B"), fakeClient);
        session.applyConfig(config);
        RemotePlaySessionTestAccess::inventoryPrepared(session);
        QElapsedTimer releaseBeat;
        releaseBeat.start();
        session.start();
#ifdef ORION_PRODUCTION_BUILD
        // [CL3-F2-001 2026-09-23] The customer build never launches a Remote Play client it cannot
        // verify as the packaged image (RP-01/RP-02); this fixture's fake client is exactly that.
        // Assert the refusal (fail closed, no producer) and leave the A->B transition itself to the
        // dev profile of this same test and the packaged hardware session.
        QCOMPARE(session.state(), RemotePlayState::Error);
        QVERIFY2(session.statusText().contains(QStringLiteral("(code RP-0")),
                 qPrintable(session.statusText()));
        // The refused Connect spawns no NEW producer; the only live sidecar is still warm-preview A
        // (this fixture drives the session directly - the controller's setCaptureCardIndex retires
        // A on selection, AUD-A2-002 - and the session stays in Error, so nothing is armed).
        QCOMPARE(session.sidecarPid(), producerA);
        session.stop(QStringLiteral("fixture_end"));
        QTRY_VERIFY_WITH_TIMEOUT(!session.stopping(), 3000);
        QSKIP("production refuses the fixture's unverified Remote Play client (asserted above)");
#endif
        QCOMPARE(session.state(), RemotePlayState::Connecting);
        QCOMPARE(session.sidecarPid(), qint64(0));
        QVERIFY(!RemotePlaySessionTestAccess::promotionPending(session));
        QTRY_VERIFY_WITH_TIMEOUT(session.sidecarPid() > 0, 7500);
        QVERIFY(session.sidecarPid() != producerA);
        QVERIFY2(releaseBeat.elapsed() >= sidecarRestartDelayMs(true) - 100,
                 "B producer launched before the capture-handle release beat");
        QCOMPARE(RemotePlaySessionTestAccess::identity(session).selectedDeviceId,
                 QStringLiteral("B"));
        QTRY_VERIFY_WITH_TIMEOUT(sawFrame(102), 3000);
        for (const auto& record : frames) {
            const int frameNumber = record.at(1).toInt();
            if (frameNumber > 0) QCOMPARE(frameNumber, 102);
        }
        session.stop(QStringLiteral("fixture_end"));
        QTRY_VERIFY_WITH_TIMEOUT(!session.stopping(), 3000);
    }

    void activeShotSourceRefusalPreservesProducerAndLead()
    {
        // OrionAppController's ctor/dtor launch and kill machine-wide processes, so
        // the executable fixture exercises its shared policy against a live
        // protocol-only producer, real session states, and a modelled in-flight
        // shot, not the controller QObject itself. Cover BOTH switch directions.
        ScopedCompiledSidecar compiled;
        QTemporaryDir fixture;
        QVERIFY(fixture.isValid());
        const QString fakeClient = fixture.filePath(QStringLiteral("fake-client.exe"));
        QFile client(fakeClient);
        QVERIFY(client.open(QIODevice::WriteOnly));
        QCOMPARE(client.write("fixture-client-image"), qint64(20));
        client.close();
        for (const QString source : {QStringLiteral("capture_card"), QStringLiteral("decoder")}) {
            const QString destination = source == QLatin1String("capture_card")
                ? QStringLiteral("decoder") : QStringLiteral("capture_card");
            RemotePlaySession session;
            session.setRootDir(fixture.path());
            auto route = captureConfig(0, QStringLiteral("A"), fakeClient);
            route.videoSource = source;
            route.actuationLeadMs = source == QLatin1String("capture_card") ? 274.0 : 240.0;
            route.actuationLeadUserSet = true;
            mirrorActuationLeadIntoSourceStash(route);
            route.actuationLeadBySourceMs.insert(destination,
                source == QLatin1String("capture_card") ? 240.0 : 274.0);
            route.actuationLeadUserSetBySource.insert(destination, true);
            session.applyConfig(route);
            QSignalSpy frames(&session, &RemotePlaySession::frameReady);
            if (source == QLatin1String("capture_card")) {
                RemotePlaySessionTestAccess::inventoryPrepared(session);
                session.startCapturePreview();
            } else {
                session.start();
#ifdef ORION_PRODUCTION_BUILD
                // [CL3-F2-001] Decoder route needs the Remote Play client: refused in production
                // for the fixture's unverified image. Assert the fail-closed refusal instead.
                QCOMPARE(session.state(), RemotePlayState::Error);
                QVERIFY2(session.statusText().contains(QStringLiteral("(code RP-0")),
                         qPrintable(session.statusText()));
                QCOMPARE(session.sidecarPid(), qint64(0));
                continue;
#endif
            }
            QTRY_VERIFY_WITH_TIMEOUT(session.sidecarPid() > 0, 3000);
            const qint64 producer = session.sidecarPid();
            const auto sawProducerFrame = [&frames]() {
                for (const auto& record : frames)
                    if (record.at(1).toInt() == 101) return true;
                return false;
            };
            QTRY_VERIFY_WITH_TIMEOUT(sawProducerFrame(), 3000);
            bool shotInFlight = true;
            for (RemotePlayState state : {RemotePlayState::Connecting, RemotePlayState::Running}) {
                RemotePlaySessionTestAccess::setState(session, state);
                bool callbackRan = false;
                QTimer::singleShot(0, this, [&] {
                    if (videoSourceChangeAllowed(session.state() == RemotePlayState::Connecting,
                                                 session.state() == RemotePlayState::Running, false))
                        switchActuationLeadVideoSource(route, destination);
                    callbackRan = true;
                });
                QTRY_VERIFY_WITH_TIMEOUT(callbackRan, 1000);
                QVERIFY(shotInFlight);
                QCOMPARE(route.videoSource, source);
                QCOMPARE(route.actuationLeadMs,
                         source == QLatin1String("capture_card") ? 274.0 : 240.0);
                QCOMPARE(session.sidecarPid(), producer);
                frames.clear();
                QVERIFY(RemotePlaySessionTestAccess::requestFrame(session));
                QTRY_VERIFY_WITH_TIMEOUT(sawProducerFrame(), 3000);
            }
            QVERIFY(!videoSourceChangeAllowed(false, false, true));
            QCOMPARE(session.sidecarPid(), producer);
            shotInFlight = false;
            session.stop(QStringLiteral("fixture_end"));
            QTRY_VERIFY_WITH_TIMEOUT(!session.stopping(), 3000);
            QVERIFY(videoSourceChangeAllowed(false, false, false));
            switchActuationLeadVideoSource(route, destination);
            QCOMPARE(route.videoSource, destination);
            QCOMPARE(route.actuationLeadMs,
                     source == QLatin1String("capture_card") ? 240.0 : 274.0);
        }
    }
};

int main(int argc, char** argv)
{
    for (int i = 1; i < argc; ++i) {
        if (QByteArray(argv[i]) == "--config-json") return runFakeSidecar(argc, argv);
    }
    QGuiApplication app(argc, argv);
    QCoreApplication::addLibraryPath(
        QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("../../Release")));
    RouteTransitionIntegrationTests tests;
    return QTest::qExec(&tests, argc, argv);
}

#include "RouteTransitionIntegrationTests.moc"
