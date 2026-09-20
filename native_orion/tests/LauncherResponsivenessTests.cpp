#include "AsyncProcessRetirer.h"
#include "ReleasePathPolicy.h"
#include "NativeCrashDiagnostics.h"
#include <QtCore/QTemporaryDir>
#include "RemotePlaySession.h"
#include <QtCore/QCoreApplication>
#include <QtCore/QElapsedTimer>
#include <QtCore/QFile>
#include <QtCore/QThread>
#include <QtTest/QSignalSpy>
#include <QtTest/QTest>
#include <cstdio>

namespace orion {
class RemotePlaySessionTestAccess final {
public:
    static void install(RemotePlaySession& s, QProcess* process) { s.sidecarProcess_ = process; }
    static void queueRestart(RemotePlaySession& s) {
        s.sidecarRestartPending_ = true;
        s.scheduleSidecarStart(1);
    }
    static void armReadiness(RemotePlaySession& s) {
        s.state_ = RemotePlayState::Running;
        s.streamPromotePending_ = true;
        s.streamPromoteAcked_ = true;
        s.inputRecoveryPending_ = true;
        s.rejectLateSidecarStarted_ = false;
        s.rejectLateInputRecoveryReady_ = false;
    }
    static bool readinessRetired(const RemotePlaySession& s) {
        return !s.streamPromotePending_ && !s.streamPromoteAcked_ && !s.inputRecoveryPending_
            && s.rejectLateSidecarStarted_ && s.rejectLateInputRecoveryReady_
            && s.streamPromoteGeneration_ > 0 && s.inputRecoveryGeneration_ > 0
            && s.state_ != RemotePlayState::Running;
    }
    static bool restartIntent(const RemotePlaySession& s) { return s.intentionalSidecarRestart_; }
    static bool hasProcess(const RemotePlaySession& s) { return s.sidecarProcess_ != nullptr; }
};
}
using namespace orion;

class LauncherResponsivenessTests : public QObject {
    Q_OBJECT
    QProcess* fixture(const QString& mode, QObject* owner = nullptr) {
        auto* p = new QProcess(owner);
#ifdef Q_OS_WIN
        p->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments* a) {
            a->flags |= 0x08000000; // no console window for test children
        });
#endif
        p->start(QCoreApplication::applicationFilePath(), {mode});
        return p;
    }
private slots:
    void gracefulStopKeepsEventLoopAlive() {
        int beats = 0, completions = 0, cleanups = 0, claims = 0;
        bool forced = true;
        QTimer heartbeat;
        heartbeat.setInterval(5);
        connect(&heartbeat, &QTimer::timeout, this, [&]() { ++beats; });
        heartbeat.start();
        QObject owner;
        QPointer<AsyncProcessRetirer> retire = new AsyncProcessRetirer(
            fixture("--fixture-graceful"), &owner, [&]() { ++cleanups; },
            [&](bool f) { forced = f; ++completions; },
            [&](QProcess* p) { if (p->processId() != 0) ++claims; });
        QElapsedTimer elapsed;
        elapsed.start();
        retire->start(2000);
        QVERIFY2(elapsed.elapsed() < 100, "stop request blocked GUI");
        QTRY_COMPARE_WITH_TIMEOUT(completions, 1, 4000);
        QVERIFY2(beats >= 10, "shutdown starved GUI heartbeat");
        QVERIFY(!forced);
        QCOMPARE(claims, 1);
        QCOMPARE(cleanups, 1);
        QTRY_VERIFY(retire.isNull());
        qInfo("graceful shutdown: GUI heartbeats=%d, callbacks=%d", beats, completions);
    }
    void deadlineKillsOnlyOwnedProcessAndCompletesOnce() {
        int completions = 0, cleanups = 0;
        bool forced = false;
        QPointer<QProcess> p = fixture("--fixture-hang");
        QObject owner;
        QPointer<AsyncProcessRetirer> retire = new AsyncProcessRetirer(
            p, &owner, [&]() { ++cleanups; }, [&](bool f) { forced = f; ++completions; });
        retire->start(100);
        QTRY_COMPARE_WITH_TIMEOUT(completions, 1, 3000);
        QVERIFY(forced);
        QCOMPARE(cleanups, 1);
        QTRY_VERIFY(retire.isNull());
        QTRY_VERIFY(p.isNull());
        QTest::qWait(150);
        QCOMPARE(completions, 1);
    }
    void failedStartAlsoCompletesOnce() {
        int completions = 0;
        auto* p = new QProcess;
        p->setProgram("Z:/missing/launcher-fixture-not-present.exe");
        p->start();
        QObject owner;
        QPointer<AsyncProcessRetirer> retire = new AsyncProcessRetirer(
            p, &owner, {}, [&](bool) { ++completions; });
        retire->start(500);
        QTRY_COMPARE_WITH_TIMEOUT(completions, 1, 2000);
        QTRY_VERIFY(retire.isNull());
    }
    void alreadyExitedCompletionIsDeferred() {
        int completions = 0;
        QObject owner;
        QPointer<AsyncProcessRetirer> retire = new AsyncProcessRetirer(
            new QProcess, &owner, {}, [&](bool) { ++completions; });
        retire->start(500);
        QCOMPARE(completions, 0);
        QTRY_COMPARE(completions, 1);
        QTRY_VERIFY(retire.isNull());
    }
    void realSessionStopIsNonBlockingAndRepeatable() {
        RemotePlaySession session;
        RemotePlaySessionTestAccess::install(session, fixture("--fixture-graceful", &session));
        QSignalSpy stopped(&session, &RemotePlaySession::sidecarStopFinished);
        QElapsedTimer elapsed;
        elapsed.start();
        session.stop();
        QVERIFY2(elapsed.elapsed() < 100, "RemotePlaySession::stop blocked GUI");
        QVERIFY(session.stopping());
        session.stop();
        QTRY_COMPARE_WITH_TIMEOUT(stopped.count(), 1, 3000);
        QVERIFY(!session.stopping());
        QVERIFY(!RemotePlaySessionTestAccess::hasProcess(session));
    }
    void newerStopCancelsDeferredRestart() {
        RemotePlaySession session;
        RemotePlaySessionTestAccess::install(session, fixture("--fixture-graceful", &session));
        QSignalSpy generations(&session, &RemotePlaySession::sidecarProcessGenerationStarted);
        session.stop();
        RemotePlaySessionTestAccess::queueRestart(session);
        session.stop();
        QTRY_VERIFY_WITH_TIMEOUT(!session.stopping(), 3000);
        QTest::qWait(100);
        QCOMPARE(generations.count(), 0);
        QVERIFY(!RemotePlaySessionTestAccess::hasProcess(session));
    }
    void restartRevokesReadinessBeforeAsynchronousCleanup() {
        RemotePlaySession session;
        RemotePlaySessionTestAccess::install(session, fixture("--fixture-graceful", &session));
        RemotePlaySessionTestAccess::armReadiness(session);
        session.restartSidecar();
        QVERIFY(RemotePlaySessionTestAccess::readinessRetired(session));
        QVERIFY(session.stopping());
        session.stop();
        QTRY_VERIFY_WITH_TIMEOUT(!session.stopping(), 3000);
        QVERIFY(!RemotePlaySessionTestAccess::hasProcess(session));
    }
    void restartingAnExitedSessionDoesNotSuppressTheNextCrash() {
        RemotePlaySession session;
        session.restartSidecar();
        QVERIFY(!RemotePlaySessionTestAccess::restartIntent(session));
        session.stop(); // cancel this fixture restart before it can launch anything
        QVERIFY(!RemotePlaySessionTestAccess::restartIntent(session));
    }
    void sessionDestructionDoesNotEmitIntoPartiallyDestroyedOwner() {
        int completions = 0;
        auto* session = new RemotePlaySession;
        RemotePlaySessionTestAccess::install(*session, fixture("--fixture-graceful", session));
        connect(session, &RemotePlaySession::sidecarStopFinished, this, [&]() { ++completions; });
        session->stop();
        delete session;
        QCOMPARE(completions, 0);
    }
    void unhandledFaultBreadcrumbIsMinimalAndDoesNotSwallowFault() {
#ifdef Q_OS_WIN
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QString path = dir.filePath("fault.log");
        native_crash_diagnostics::install(path);
        // No real process fault: exercise the fault recorder with a synthetic record.
        const auto previous = native_crash_diagnostics::previousFilter;
        native_crash_diagnostics::previousFilter = nullptr;
        EXCEPTION_RECORD record{};
        record.ExceptionCode = 0xc0000005;
        EXCEPTION_POINTERS info{&record, nullptr};
        const auto result = native_crash_diagnostics::recordUnhandled(&info);
        SetUnhandledExceptionFilter(previous);
        native_crash_diagnostics::previousFilter = previous;
        QCOMPARE(result, LONG(EXCEPTION_CONTINUE_SEARCH));
        QFile log(path);
        QVERIFY(log.open(QIODevice::ReadOnly));
        const auto bytes = log.readAll();
        QVERIFY(bytes.contains("exception=0xc0000005"));
        QVERIFY(bytes.size() < 256);
        QVERIFY(!bytes.contains("settings"));
#endif
    }
    void tempoTogglePreservesInputChoiceAndDoesNotRetuneLead() {
        AppConfigData data;
        data.actuationLeadMs = 274.0;
        data.rhythmFlickDelayMs = 12.0;
        data.tempoReleaseStyle = "let_go";
        QVERIFY(selectTempoInputPath(data, "Stick"));
        QCOMPARE(selectedTempoInputPath(data), QString("Stick"));
        QCOMPARE(data.remotePlayInputSource, QString("square"));
        QVERIFY(!data.tempoEnabled && !data.tempoRemapEnabled);
        setTempoPathEnabled(data, true);
        QCOMPARE(data.remotePlayInputSource, QString("stick"));
        QVERIFY(data.tempoEnabled && data.tempoRemapEnabled && data.tempoFlickEnabled);
        QCOMPARE(data.rhythmFlickDelayMs, 0.0);
        QCOMPARE(data.tempoReleaseStyle, QString("flick"));
        QVERIFY(selectTempoInputPath(data, "Button"));
        QCOMPARE(selectedTempoInputPath(data), QString("Button"));
        QCOMPARE(data.remotePlayInputSource, QString("square"));
        QVERIFY(data.tempoEnabled);
        QVERIFY(selectTempoInputPath(data, "Stick"));
        setTempoPathEnabled(data, false);
        QCOMPARE(selectedTempoInputPath(data), QString("Stick"));
        QCOMPARE(data.remotePlayInputSource, QString("square"));
        QVERIFY(!data.tempoEnabled && !data.tempoRemapEnabled && !data.tempoFlickEnabled);
        setTempoPathEnabled(data, true);
        QCOMPARE(data.remotePlayInputSource, QString("stick"));
        const auto beforeInvalid = data;
        QVERIFY(!selectTempoInputPath(data, "Tempo"));
        QVERIFY(!selectTempoInputPath(data, "unknown"));
        QVERIFY(sameTempoPathSettings(data, beforeInvalid));
        QCOMPARE(data.actuationLeadMs, 274.0);
    }
    void legacyActiveSourceIsDisplayedWithoutRewritingConfig() {
        AppConfigData data;
        data.tempoEnabled = true;
        data.remotePlayInputSource = "stick";
        data.tempoRemapType = "button";
        data.rhythmFlickDelayMs = 9.0;
        QCOMPARE(selectedTempoInputPath(data), QString("Stick"));
        QCOMPARE(data.tempoRemapType, QString("button"));
        QCOMPARE(data.rhythmFlickDelayMs, 9.0);
        setTempoPathEnabled(data, false);
        QCOMPARE(data.tempoRemapType, QString("stick"));
        setTempoPathEnabled(data, true);
        QCOMPARE(data.remotePlayInputSource, QString("stick"));
    }

};

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    if (app.arguments().contains("--fixture-hang")) { QThread::sleep(10); return 0; }
    if (app.arguments().contains("--fixture-graceful")) {
        QFile input;
        input.open(stdin, QIODevice::ReadOnly);
        const QByteArray line = input.readLine();
        if (!line.contains("shutdown")) return 3;
        QThread::msleep(250);
        return 0;
    }
    LauncherResponsivenessTests tests;
    return QTest::qExec(&tests, argc, argv);
}
#include "LauncherResponsivenessTests.moc"
