#include "SharedMemoryFrameReader.h"
#include "SharedMemoryFramePump.h"
#include "RemotePlaySession.h"

#include <QtCore/QDir>
#include <QtCore/QElapsedTimer>
#include <QtCore/QFileInfo>
#include <QtCore/QProcess>
#include <QtCore/QProcessEnvironment>
#include <QtCore/QThread>
#include <QtCore/QUuid>
#include <QtGui/QColor>
#include <QtTest/QTest>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

using namespace orion;

namespace orion {

class RemotePlaySessionTestAccess final {
public:
    static void armShmEpoch(RemotePlaySession& session, quint64 epoch,
                            const QString& readyEvent)
    {
        session.shmSourceEpoch_ = epoch;
        session.shmTransportNames_.mappingName = QStringLiteral("OrionPreviewFrame_test");
        session.shmTransportNames_.mutexName = QStringLiteral("OrionPreviewMutex_test");
        session.shmTransportNames_.readyEventName = readyEvent;
        session.shmSourceActive_ = true;
        session.shmReaderOpen_ = true;
        session.shmFallbackRequested_ = false;
        session.jpegFallbackFirstFrameNumber_ = 0;
        session.shmFramesRead_ = 7;
        session.lastPreviewPipelineStatsMs_ = 1234;
    }

    static void handoff(RemotePlaySession& session, const QJsonObject& message)
    {
        session.handlePreviewTransportHandoff(message);
    }

    static void deliverShmBatch(RemotePlaySession& session,
                                const SharedMemoryFramePumpBatch& batch)
    {
        session.handleShmPumpBatch(batch);
    }

    static void submitJpeg(RemotePlaySession& session, int frameNumber)
    {
        session.submitPreviewPayload(QByteArrayLiteral("eA=="), frameNumber);
    }

    static quint64 decoderSubmitted(const RemotePlaySession& session)
    {
        return session.frameDecoder_->stats().submitted;
    }

    static bool shmActive(const RemotePlaySession& session)
    {
        return session.shmSourceActive_;
    }

    static bool jpegFallback(const RemotePlaySession& session)
    {
        return session.shmFallbackRequested_;
    }

    static int firstJpegFrame(const RemotePlaySession& session)
    {
        return session.jpegFallbackFirstFrameNumber_;
    }

    static quint64 shmFramesRead(const RemotePlaySession& session)
    {
        return session.shmFramesRead_;
    }

    static qint64 pipelineBaseline(const RemotePlaySession& session)
    {
        return session.lastPreviewPipelineStatsMs_;
    }

    static double shmSourceToReadAgeMs(const RemotePlaySession& session)
    {
        return session.shmSourceToReadAgeMs_;
    }

    static double shmReadToDispatchAgeMs(const RemotePlaySession& session)
    {
        return session.shmReadToDispatchAgeMs_;
    }

    static double shmSourceToDispatchAgeMs(const RemotePlaySession& session)
    {
        return session.shmSourceToDispatchAgeMs_;
    }

    static quint64 shmPresentationTimestampRejects(const RemotePlaySession& session)
    {
        return session.shmPresentationTimestampRejects_;
    }
};

} // namespace orion

namespace {

class ControlledFrameSource final : public SharedMemoryFrameSource {
public:
    void configureTransport(const SharedMemoryTransportNames& names) override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        eventMode_ = names.eventNotificationsEnabled();
        eventReady_ = false;
        interrupted_ = false;
    }

    bool open() override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ++openCalls_;
        open_ = openSucceeds_;
        lastError_ = open_ ? QString() : QStringLiteral("controlled open failure");
        return open_;
    }

    void close() override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        open_ = false;
        ++closeCalls_;
        cv_.notify_all();
    }

    [[nodiscard]] bool isOpen() const noexcept override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return open_;
    }

    SharedMemoryFrameWaitResult waitForFrameReady(int timeoutMs) override
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!eventMode_) {
            return SharedMemoryFrameWaitResult::Unsupported;
        }
        ++waitEntered_;
        cv_.notify_all();
        const bool woke = cv_.wait_for(
            lock, std::chrono::milliseconds(timeoutMs), [this]() {
                return interrupted_ || eventReady_;
            });
        if (!woke) {
            return SharedMemoryFrameWaitResult::Timeout;
        }
        if (interrupted_) {
            interrupted_ = false;
            return SharedMemoryFrameWaitResult::Interrupted;
        }
        eventReady_ = false; // model a Windows auto-reset event
        return SharedMemoryFrameWaitResult::Ready;
    }

    void interruptWait() noexcept override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        interrupted_ = true;
        cv_.notify_all();
    }

    bool probeGeneration(std::uint64_t& writeCount) override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ++probeCalls_;
        writeCount = committedGeneration_;
        lastError_.clear();
        return writeCount > 0;
    }

    QImage readFrame(int& frameNumber) override
    {
        int marker = 0;
        std::chrono::milliseconds readDelay{0};
        {
            std::unique_lock<std::mutex> lock(mutex_);
            ++readCalls_;
            lastReadThread_ = std::this_thread::get_id();
            ++readEntered_;
            cv_.notify_all();
            cv_.wait(lock, [this]() { return !blockReads_; });
            if (ordinaryNoFrame_) {
                lastError_.clear();
                frameNumber = 0;
                return {};
            }
            if (hardReadFault_) {
                lastError_ = QStringLiteral("controlled read fault");
                frameNumber = 0;
                return {};
            }
            lastError_.clear();
            marker = ++nextFrame_;
            committedGeneration_ = static_cast<std::uint64_t>(marker);
            const auto sourceNow = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch()).count();
            lastReadTimestampNs_ = sourceNow > 0
                ? static_cast<std::uint64_t>(sourceNow) : 0;
            readDelay = readDelay_;
        }
        if (readDelay.count() > 0) {
            std::this_thread::sleep_for(readDelay);
        }
        frameNumber = marker;
        QImage image(4, 4, QImage::Format_BGR888);
        image.fill(QColor(marker & 0xff, 0, 0));
        return image;
    }

    [[nodiscard]] std::uint64_t lastReadGeneration() const noexcept override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return committedGeneration_;
    }

    [[nodiscard]] std::uint64_t lastReadTimestampNs() const noexcept override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return lastReadTimestampNs_;
    }

    [[nodiscard]] QString lastError() const override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return lastError_;
    }

    void setBlockReads(bool block)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        blockReads_ = block;
        if (!block) {
            cv_.notify_all();
        }
    }

    void setOrdinaryNoFrame(bool ordinary)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ordinaryNoFrame_ = ordinary;
    }

    void setReadDelay(std::chrono::milliseconds delay)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        readDelay_ = delay;
    }

    void signalFrameReady()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        eventReady_ = true;
        cv_.notify_all();
    }

    [[nodiscard]] bool waitForEventWaitEntered(
        int target, std::chrono::milliseconds timeout = std::chrono::milliseconds(1000))
    {
        std::unique_lock<std::mutex> lock(mutex_);
        return cv_.wait_for(lock, timeout, [this, target]() {
            return waitEntered_ >= target;
        });
    }

    [[nodiscard]] bool waitForReadEntered(
        int target, std::chrono::milliseconds timeout = std::chrono::milliseconds(1000))
    {
        std::unique_lock<std::mutex> lock(mutex_);
        return cv_.wait_for(lock, timeout, [this, target]() {
            return readEntered_ >= target;
        });
    }

    [[nodiscard]] bool waitForCloseCalls(
        int target, std::chrono::milliseconds timeout = std::chrono::milliseconds(1000))
    {
        std::unique_lock<std::mutex> lock(mutex_);
        return cv_.wait_for(lock, timeout, [this, target]() {
            return closeCalls_ >= target;
        });
    }

    [[nodiscard]] std::thread::id lastReadThread() const
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return lastReadThread_;
    }

private:
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    bool openSucceeds_ = true;
    bool open_ = false;
    bool blockReads_ = false;
    bool ordinaryNoFrame_ = false;
    bool hardReadFault_ = false;
    bool eventMode_ = false;
    bool eventReady_ = false;
    bool interrupted_ = false;
    std::chrono::milliseconds readDelay_{0};
    QString lastError_;
    int openCalls_ = 0;
    int closeCalls_ = 0;
    int readCalls_ = 0;
    int readEntered_ = 0;
    int waitEntered_ = 0;
    int probeCalls_ = 0;
    int nextFrame_ = 0;
    std::uint64_t committedGeneration_ = 0;
    std::uint64_t lastReadTimestampNs_ = 0;
    std::thread::id lastReadThread_;
};

} // namespace

class SharedMemoryFrameReaderTests final : public QObject {
    Q_OBJECT

private slots:
    void pythonWriterInteroperatesAcrossProcesses()
    {
        const QDir repoRoot(QStringLiteral(ORION_REPO_ROOT));
        QString python = qEnvironmentVariable("ORION_TEST_PYTHON");
        if (python.isEmpty()) {
            python = repoRoot.filePath(QStringLiteral(".venv/Scripts/python.exe"));
        }
        const QString writerScript = repoRoot.filePath(QStringLiteral("shm_frame_bridge.py"));
        QVERIFY2(QFileInfo::exists(python), qPrintable(QStringLiteral("Python missing: %1").arg(python)));
        QVERIFY2(QFileInfo::exists(writerScript), qPrintable(QStringLiteral("Writer missing: %1").arg(writerScript)));

        QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
        token.remove(QLatin1Char('-'));
        SharedMemoryTransportNames names;
        names.mappingName = QStringLiteral("OrionPreviewFrame_%1").arg(token);
        names.mutexName = QStringLiteral("OrionPreviewMutex_%1").arg(token);
        names.readyEventName = QStringLiteral("OrionPreviewReady_%1").arg(token);

        QProcess producer;
        producer.setProgram(python);
        producer.setArguments({QStringLiteral("-u"), writerScript,
                               QStringLiteral("--probe"),
                               QStringLiteral("--hold-ms"), QStringLiteral("3000")});
        producer.setProcessChannelMode(QProcess::MergedChannels);
        QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
        environment.insert(QStringLiteral("ORION_PREVIEW_SHM_MAPPING"), names.mappingName);
        environment.insert(QStringLiteral("ORION_PREVIEW_SHM_MUTEX"), names.mutexName);
        environment.insert(QStringLiteral("ORION_PREVIEW_SHM_READY_EVENT"), names.readyEventName);
        producer.setProcessEnvironment(environment);
        producer.start();
        QVERIFY2(producer.waitForStarted(3000), qPrintable(producer.errorString()));

        QByteArray output;
        QElapsedTimer readyTimer;
        readyTimer.start();
        while (!output.contains("SHM_PROBE_READY") && readyTimer.elapsed() < 5000) {
            producer.waitForReadyRead(100);
            output += producer.readAll();
            if (producer.state() == QProcess::NotRunning) break;
        }
        QVERIFY2(output.contains("SHM_PROBE_READY"), output.constData());

        SharedMemoryFrameReader reader;
        reader.configureTransport(names);
        QVERIFY(reader.waitForFrameReady(1000) == SharedMemoryFrameWaitResult::Ready);
        QElapsedTimer openTimer;
        openTimer.start();
        while (!reader.open() && openTimer.elapsed() < 1000) {
            QThread::msleep(5);
        }
        QVERIFY2(reader.isOpen(), qPrintable(reader.lastError()));

        int frameNumber = 0;
        QImage image;
        QElapsedTimer readTimer;
        readTimer.start();
        while (image.isNull() && readTimer.elapsed() < 1000) {
            image = reader.readFrame(frameNumber);
            if (image.isNull()) QThread::msleep(2);
        }
        QVERIFY2(!image.isNull(), qPrintable(reader.lastError()));
        QCOMPARE(image.size(), QSize(1280, 720));
        QCOMPARE(image.format(), QImage::Format_BGR888);
        QCOMPARE(frameNumber, 4242);
        const std::uint64_t sourceTimestampNs = reader.lastReadTimestampNs();
        QVERIFY(sourceTimestampNs > 0);
        const QColor pixel = image.pixelColor(640, 360);
        QCOMPARE(pixel.red(), 211);
        QCOMPARE(pixel.green(), 83);
        QCOMPARE(pixel.blue(), 17);

        // Latest-frame identity is consumed exactly once; a coalesced duplicate
        // notification must not re-present the same pixels.
        int duplicateNumber = -1;
        QVERIFY(reader.readFrame(duplicateNumber).isNull());
        QVERIFY(reader.lastError().isEmpty());
        QCOMPARE(reader.lastReadTimestampNs(), sourceTimestampNs);

#ifdef Q_OS_WIN
        // A producer-owned mutex timeout is ordinary latest-wins contention,
        // not a hard reader fault and never a reason to enter JPEG fallback.
        std::promise<DWORD> mutexAcquired;
        std::promise<void> releaseMutex;
        auto releaseFuture = releaseMutex.get_future();
        const std::wstring mutexObjectName = names.mutexName.toStdWString();
        std::thread holder([&mutexAcquired, &releaseFuture, &mutexObjectName]() {
            HANDLE mutex = OpenMutexW(
                SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, mutexObjectName.c_str());
            if (!mutex) {
                mutexAcquired.set_value(GetLastError());
                return;
            }
            const DWORD wait = WaitForSingleObject(mutex, 1000);
            mutexAcquired.set_value(wait);
            if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED) {
                releaseFuture.wait();
                ReleaseMutex(mutex);
            }
            CloseHandle(mutex);
        });
        const DWORD acquired = mutexAcquired.get_future().get();
        int contendedNumber = -1;
        QImage contendedImage;
        QString contendedError;
        if (acquired == WAIT_OBJECT_0 || acquired == WAIT_ABANDONED) {
            contendedImage = reader.readFrame(contendedNumber);
            contendedError = reader.lastError();
            releaseMutex.set_value();
        }
        holder.join();
        QCOMPARE(acquired, DWORD{WAIT_OBJECT_0});
        QVERIFY(contendedImage.isNull());
        QVERIFY2(contendedError.isEmpty(), qPrintable(contendedError));
#endif

        producer.terminate();
        if (!producer.waitForFinished(2000)) {
            producer.kill();
            producer.waitForFinished(1000);
        }
    }

    void mismatchedReadyEventSurfacesBoundedNotificationFailureAcrossProcesses()
    {
        const QDir repoRoot(QStringLiteral(ORION_REPO_ROOT));
        QString python = qEnvironmentVariable("ORION_TEST_PYTHON");
        if (python.isEmpty()) {
            python = repoRoot.filePath(QStringLiteral(".venv/Scripts/python.exe"));
        }
        const QString writerScript = repoRoot.filePath(QStringLiteral("shm_frame_bridge.py"));
        QVERIFY2(QFileInfo::exists(python), qPrintable(QStringLiteral("Python missing: %1").arg(python)));

        QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
        token.remove(QLatin1Char('-'));
        SharedMemoryTransportNames producerNames;
        producerNames.mappingName = QStringLiteral("OrionPreviewFrame_%1").arg(token);
        producerNames.mutexName = QStringLiteral("OrionPreviewMutex_%1").arg(token);
        producerNames.readyEventName = QStringLiteral("OrionPreviewReady_producer_%1").arg(token);
        SharedMemoryTransportNames consumerNames = producerNames;
        consumerNames.readyEventName = QStringLiteral("OrionPreviewReady_consumer_%1").arg(token);

        QProcess producer;
        producer.setProgram(python);
        producer.setArguments({QStringLiteral("-u"), writerScript,
                               QStringLiteral("--probe-series"),
                               QStringLiteral("--frames"), QStringLiteral("180"),
                               QStringLiteral("--interval-ms"), QStringLiteral("8"),
                               QStringLiteral("--start-delay-ms"), QStringLiteral("750"),
                               QStringLiteral("--hold-ms"), QStringLiteral("3000")});
        producer.setProcessChannelMode(QProcess::MergedChannels);
        QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
        environment.insert(QStringLiteral("ORION_PREVIEW_SHM_MAPPING"), producerNames.mappingName);
        environment.insert(QStringLiteral("ORION_PREVIEW_SHM_MUTEX"), producerNames.mutexName);
        environment.insert(QStringLiteral("ORION_PREVIEW_SHM_READY_EVENT"), producerNames.readyEventName);
        producer.setProcessEnvironment(environment);
        producer.start();
        QVERIFY2(producer.waitForStarted(3000), qPrintable(producer.errorString()));

        QByteArray output;
        QElapsedTimer mappingTimer;
        mappingTimer.start();
        while (!output.contains("SHM_PROBE_MAPPING_READY") && mappingTimer.elapsed() < 5000) {
            producer.waitForReadyRead(100);
            output += producer.readAll();
            if (producer.state() == QProcess::NotRunning) break;
        }
        QVERIFY2(output.contains("SHM_PROBE_MAPPING_READY"), output.constData());

        SharedMemoryFramePump pump;
        SharedMemoryFramePumpBatch lostNotification;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (batch.notificationFailureRun > 0) {
                        lostNotification = batch;
                    }
                });
        QElapsedTimer recoveryTimer;
        recoveryTimer.start();
        pump.beginSourceEpoch(700, consumerNames);
        QTRY_VERIFY_WITH_TIMEOUT(lostNotification.notificationFailureRun > 0, 2500);
        QVERIFY2(recoveryTimer.elapsed() < 2000,
                 qPrintable(QStringLiteral("lost event detection took %1 ms")
                                .arg(recoveryTimer.elapsed())));
        QVERIFY(lostNotification.notificationError.contains(
            QStringLiteral("advanced without ready-event")));
        QVERIFY(lostNotification.eventWaitTimeouts >= 2);
        QVERIFY(lostNotification.eventGenerationProbes >= 1);
        QVERIFY(lostNotification.eventNotificationLosses >= 1);
        QCOMPARE(lostNotification.eventWaitFailures, quint64{0});
        const auto stats = pump.stats();
        QVERIFY(stats.eventWaitTimeouts >= lostNotification.eventWaitTimeouts);
        QVERIFY(stats.eventNotificationLosses >= 1);
        QCOMPARE(stats.framesRead, quint64{0});

        pump.retireSourceEpoch(700);
        producer.terminate();
        if (!producer.waitForFinished(2000)) {
            producer.kill();
            producer.waitForFinished(1000);
        }
    }

    void abandonedPartialPublishIsDroppedThenCleanWriterRecoversAcrossProcesses()
    {
        const QDir repoRoot(QStringLiteral(ORION_REPO_ROOT));
        QString python = qEnvironmentVariable("ORION_TEST_PYTHON");
        if (python.isEmpty()) {
            python = repoRoot.filePath(QStringLiteral(".venv/Scripts/python.exe"));
        }
        const QString writerScript = repoRoot.filePath(QStringLiteral("shm_frame_bridge.py"));
        QVERIFY2(QFileInfo::exists(python), qPrintable(QStringLiteral("Python missing: %1").arg(python)));

        QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
        token.remove(QLatin1Char('-'));
        SharedMemoryTransportNames names;
        names.mappingName = QStringLiteral("OrionPreviewFrame_%1").arg(token);
        names.mutexName = QStringLiteral("OrionPreviewMutex_%1").arg(token);
        names.readyEventName = QStringLiteral("OrionPreviewReady_%1").arg(token);

        const auto configureProducer = [&](QProcess& process, const QStringList& arguments) {
            process.setProgram(python);
            process.setArguments(arguments);
            process.setProcessChannelMode(QProcess::MergedChannels);
            QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
            environment.insert(QStringLiteral("ORION_PREVIEW_SHM_MAPPING"), names.mappingName);
            environment.insert(QStringLiteral("ORION_PREVIEW_SHM_MUTEX"), names.mutexName);
            environment.insert(QStringLiteral("ORION_PREVIEW_SHM_READY_EVENT"), names.readyEventName);
            process.setProcessEnvironment(environment);
        };

        QProcess abandonedProducer;
        configureProducer(
            abandonedProducer,
            {QStringLiteral("-u"), writerScript, QStringLiteral("--probe-abandon"),
             QStringLiteral("--hold-ms"), QStringLiteral("10000")});
        abandonedProducer.start();
        QVERIFY2(abandonedProducer.waitForStarted(3000),
                 qPrintable(abandonedProducer.errorString()));
        QByteArray output;
        QElapsedTimer abandonTimer;
        abandonTimer.start();
        while (!output.contains("SHM_PROBE_ABANDON_LOCKED")
               && abandonTimer.elapsed() < 5000) {
            abandonedProducer.waitForReadyRead(100);
            output += abandonedProducer.readAll();
            if (abandonedProducer.state() == QProcess::NotRunning) break;
        }
        QVERIFY2(output.contains("SHM_PROBE_ABANDON_LOCKED"), output.constData());

        SharedMemoryFrameReader reader;
        reader.configureTransport(names);
        QVERIFY2(reader.open(), qPrintable(reader.lastError()));
        // Consume the pending event from the old clean generation only after the
        // producer has invalidated the header and partially overwritten pixels.
        QCOMPARE(reader.waitForFrameReady(1000), SharedMemoryFrameWaitResult::Ready);
        abandonedProducer.kill();
        QVERIFY(abandonedProducer.waitForFinished(3000));

        int partialFrameNumber = -1;
        const QImage partial = reader.readFrame(partialFrameNumber);
        QVERIFY(partial.isNull());
        QCOMPARE(partialFrameNumber, 0);
        QVERIFY2(reader.lastError().contains(QStringLiteral("abandoned"), Qt::CaseInsensitive),
                 qPrintable(reader.lastError()));
        QCOMPARE(reader.lastReadGeneration(), std::uint64_t{0});

        QProcess cleanProducer;
        configureProducer(
            cleanProducer,
            {QStringLiteral("-u"), writerScript, QStringLiteral("--probe"),
             QStringLiteral("--hold-ms"), QStringLiteral("3000")});
        cleanProducer.start();
        QVERIFY2(cleanProducer.waitForStarted(3000), qPrintable(cleanProducer.errorString()));
        QByteArray cleanOutput;
        QElapsedTimer cleanTimer;
        cleanTimer.start();
        while (!cleanOutput.contains("SHM_PROBE_READY") && cleanTimer.elapsed() < 5000) {
            cleanProducer.waitForReadyRead(100);
            cleanOutput += cleanProducer.readAll();
            if (cleanProducer.state() == QProcess::NotRunning) break;
        }
        QVERIFY2(cleanOutput.contains("SHM_PROBE_READY"), cleanOutput.constData());
        QCOMPARE(reader.waitForFrameReady(1000), SharedMemoryFrameWaitResult::Ready);

        int recoveredFrameNumber = 0;
        QImage recovered;
        QElapsedTimer readTimer;
        readTimer.start();
        while (recovered.isNull() && readTimer.elapsed() < 1000) {
            recovered = reader.readFrame(recoveredFrameNumber);
            if (recovered.isNull()) QThread::msleep(2);
        }
        QVERIFY2(!recovered.isNull(), qPrintable(reader.lastError()));
        QCOMPARE(recoveredFrameNumber, 4242);
        const QColor pixel = recovered.pixelColor(640, 360);
        QCOMPARE(pixel.red(), 211);
        QCOMPARE(pixel.green(), 83);
        QCOMPARE(pixel.blue(), 17);

        cleanProducer.terminate();
        if (!cleanProducer.waitForFinished(2000)) {
            cleanProducer.kill();
            cleanProducer.waitForFinished(1000);
        }
    }

    void pumpReadsFromNamedEventWithoutStdoutNotification()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        SharedMemoryFramePump pump(std::move(source));
        SharedMemoryFramePumpBatch delivered;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) {
                        delivered = batch;
                    }
                });

        SharedMemoryTransportNames names;
        names.readyEventName = QStringLiteral("OrionPreviewReady_event_test");
        pump.beginSourceEpoch(60, names);
        QVERIFY(sourcePtr->waitForEventWaitEntered(1));
        sourcePtr->signalFrameReady();
        QTRY_VERIFY_WITH_TIMEOUT(!delivered.image.isNull(), 2000);

        QCOMPARE(delivered.sourceEpoch, quint64{60});
        QCOMPARE(delivered.mappedFrameNumber, 1);
        // No JSON frame_shm notice exists in this path; source identity comes
        // exclusively from the committed mapping header.
        QCOMPARE(delivered.eventFrameNumber, 0);
        const auto stats = pump.stats();
        QCOMPARE(stats.submitted, quint64{0});
        QCOMPARE(stats.eventSignals, quint64{1});
        QCOMPARE(stats.framesRead, quint64{1});
        QCOMPARE(stats.readFaults, quint64{0});
    }

    void pumpDoesNotFlagPausedProducerWhenGenerationDoesNotAdvance()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        SharedMemoryFramePump pump(std::move(source));
        SharedMemoryFramePumpBatch delivered;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) {
                        delivered = batch;
                    }
                });

        SharedMemoryTransportNames names;
        names.readyEventName = QStringLiteral("OrionPreviewReady_pause_test");
        pump.beginSourceEpoch(601, names);
        QVERIFY(sourcePtr->waitForEventWaitEntered(1));
        sourcePtr->signalFrameReady();
        QTRY_VERIFY_WITH_TIMEOUT(!delivered.image.isNull(), 2000);

        // Two 250 ms misses cause a header-only diagnostic probe. The mapping
        // still contains the already-consumed generation, so a genuine source
        // pause must remain healthy rather than manufacturing a fallback fault.
        QTRY_VERIFY_WITH_TIMEOUT(pump.stats().eventGenerationProbes >= 1, 1500);
        const auto stats = pump.stats();
        QVERIFY(stats.eventWaitTimeouts >= 2);
        QCOMPARE(stats.eventNotificationLosses, quint64{0});
        QCOMPARE(stats.readFaults, quint64{0});
    }

    void pumpRetireInterruptsBlockedNamedEventWait()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        SharedMemoryFramePump pump(std::move(source));

        SharedMemoryTransportNames names;
        names.readyEventName = QStringLiteral("OrionPreviewReady_interrupt_test");
        pump.beginSourceEpoch(61, names);
        QVERIFY(sourcePtr->waitForEventWaitEntered(1));

        QElapsedTimer retireTimer;
        retireTimer.start();
        pump.retireSourceEpoch(61);
        QVERIFY(sourcePtr->waitForCloseCalls(2)); // begin reset + retire reset
        QVERIFY2(retireTimer.elapsed() < 500,
                 qPrintable(QStringLiteral("event wait retire took %1 ms")
                                .arg(retireTimer.elapsed())));
        QCOMPARE(pump.stats().framesRead, quint64{0});
    }

    void pumpLegacyNoticeInterruptsNamedEventWaitWithoutPollingDelay()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        SharedMemoryFramePump pump(std::move(source));
        SharedMemoryFramePumpBatch delivered;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) {
                        delivered = batch;
                    }
                });

        SharedMemoryTransportNames names;
        names.readyEventName = QStringLiteral("OrionPreviewReady_fallback_test");
        pump.beginSourceEpoch(62, names);
        QVERIFY(sourcePtr->waitForEventWaitEntered(1));

        QElapsedTimer deliveryTimer;
        deliveryTimer.start();
        pump.submitFrameNotification(62, 707);
        QTRY_VERIFY_WITH_TIMEOUT(!delivered.image.isNull(), 2000);
        QVERIFY2(deliveryTimer.elapsed() < 200,
                 qPrintable(QStringLiteral("stdout fallback wake took %1 ms")
                                .arg(deliveryTimer.elapsed())));
        QCOMPARE(delivered.eventFrameNumber, 707);
        const auto stats = pump.stats();
        QCOMPARE(stats.submitted, quint64{1});
        QCOMPARE(stats.eventSignals, quint64{0});
        QCOMPARE(stats.framesRead, quint64{1});
    }

    void pumpReadsOffOwnerThreadAndDeliversOnOwnerThread()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        SharedMemoryFramePump pump(std::move(source));
        const std::thread::id ownerThread = std::this_thread::get_id();
        std::thread::id deliveryThread;
        SharedMemoryFramePumpBatch delivered;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    deliveryThread = std::this_thread::get_id();
                    if (!batch.image.isNull()) {
                        delivered = batch;
                    }
                });

        pump.beginSourceEpoch(1);
        pump.submitFrameNotification(1, 77);
        QTRY_VERIFY_WITH_TIMEOUT(!delivered.image.isNull(), 2000);

        QVERIFY(sourcePtr->lastReadThread() != ownerThread);
        QCOMPARE(deliveryThread, ownerThread);
        QCOMPARE(delivered.sourceEpoch, quint64{1});
        QCOMPARE(delivered.mappedFrameNumber, 1);
        const auto stats = pump.stats();
        QVERIFY(stats.workerObserved);
        QVERIFY(stats.workerDifferentFromOwner);
        QCOMPARE(stats.framesRead, quint64{1});
        QCOMPARE(stats.maxPendingDepth, std::size_t{1});
        QCOMPARE(stats.maxReadyDepth, std::size_t{1});
    }

    void pumpPreservesProducerTimestampAcrossReadAndCallbackDelay()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        sourcePtr->setReadDelay(std::chrono::milliseconds(35));
        SharedMemoryFramePump pump(std::move(source));
        SharedMemoryFramePumpBatch delivered;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) {
                        delivered = batch;
                    }
                });

        pump.beginSourceEpoch(2);
        pump.submitFrameNotification(2, 88);
        QTRY_VERIFY_WITH_TIMEOUT(!delivered.image.isNull(), 2000);

        QVERIFY(delivered.sourceTimestampNs > 0);
        QVERIFY(delivered.pumpReadCompletedTimestampNs
                >= delivered.sourceTimestampNs + quint64{25'000'000});
        QVERIFY(delivered.presentationDispatchTimestampNs
                >= delivered.pumpReadCompletedTimestampNs);
        const quint64 immutableSourceTimestampNs = delivered.sourceTimestampNs;
        QTest::qWait(20);
        QCOMPARE(delivered.sourceTimestampNs, immutableSourceTimestampNs);
    }

    void pumpCoalescesNotificationFloodToOnePendingRead()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        sourcePtr->setBlockReads(true);
        SharedMemoryFramePump pump(std::move(source));
        int deliveredBatches = 0;
        int deliveredImageBatches = 0;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    ++deliveredBatches;
                    if (!batch.image.isNull()) {
                        ++deliveredImageBatches;
                    }
                });

        pump.beginSourceEpoch(5);
        pump.submitFrameNotification(5, 1);
        QVERIFY(sourcePtr->waitForReadEntered(1));
        for (int frame = 2; frame <= 1000; ++frame) {
            pump.submitFrameNotification(5, frame);
        }
        auto stats = pump.stats();
        QCOMPARE(stats.submitted, quint64{1000});
        QCOMPARE(stats.notificationCoalesced, quint64{998});
        QCOMPARE(stats.maxPendingDepth, std::size_t{1});

        sourcePtr->setBlockReads(false);
        QTRY_COMPARE_WITH_TIMEOUT(pump.stats().framesRead, quint64{2}, 2000);
        QTest::qWait(30);
        stats = pump.stats();
        QCOMPARE(stats.readAttempts, quint64{2});
        QCOMPARE(stats.maxPendingDepth, std::size_t{1});
        QCOMPARE(stats.maxReadyDepth, std::size_t{1});
        QVERIFY(deliveredImageBatches <= 2);
        // One additional batch may report the reader-open transition before
        // either image; it is state, not a queued frame backlog.
        QVERIFY(deliveredBatches <= 3);
    }

    void pumpDropsStaleCompletionAcrossSourceEpoch()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        sourcePtr->setBlockReads(true);
        SharedMemoryFramePump pump(std::move(source));
        std::vector<quint64> deliveredEpochs;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) {
                        deliveredEpochs.push_back(batch.sourceEpoch);
                    }
                });

        pump.beginSourceEpoch(10);
        pump.submitFrameNotification(10, 10);
        QVERIFY(sourcePtr->waitForReadEntered(1));
        pump.retireSourceEpoch(10);
        pump.beginSourceEpoch(11);
        pump.submitFrameNotification(11, 11);
        sourcePtr->setBlockReads(false);

        QTRY_VERIFY_WITH_TIMEOUT(!deliveredEpochs.empty(), 2000);
        QCOMPARE(deliveredEpochs.size(), std::size_t{1});
        QCOMPARE(deliveredEpochs.front(), quint64{11});
        QVERIFY(pump.stats().staleCompletionDropped >= 1);
    }

    void pumpRetireClosesSourceAndRejectsLateNotification()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        SharedMemoryFramePump pump(std::move(source));

        pump.beginSourceEpoch(21);
        pump.submitFrameNotification(21, 1);
        QTRY_COMPARE_WITH_TIMEOUT(pump.stats().framesRead, quint64{1}, 2000);
        const quint64 submittedBeforeRetire = pump.stats().submitted;
        pump.retireSourceEpoch(21);
        QVERIFY(sourcePtr->waitForCloseCalls(2)); // begin reset + retire reset
        pump.submitFrameNotification(21, 2);
        QTest::qWait(20);
        QCOMPARE(pump.stats().submitted, submittedBeforeRetire);
        QCOMPARE(pump.stats().framesRead, quint64{1});
    }

    void pumpTreatsEmptyErrorNoFrameAsOrdinaryCoalescing()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        sourcePtr->setOrdinaryNoFrame(true);
        SharedMemoryFramePump pump(std::move(source));

        pump.beginSourceEpoch(31);
        pump.submitFrameNotification(31, 1);
        QVERIFY(sourcePtr->waitForReadEntered(1));
        QTRY_COMPARE_WITH_TIMEOUT(pump.stats().ordinaryNoFrame, quint64{1}, 2000);
        const auto stats = pump.stats();
        QCOMPARE(stats.readFaults, quint64{0});
        QCOMPARE(stats.framesRead, quint64{0});
    }

    void pumpDestructionIsBoundedWithReadInFlight()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        ControlledFrameSource* const sourcePtr = source.get();
        sourcePtr->setReadDelay(std::chrono::milliseconds(40));
        auto pump = std::make_unique<SharedMemoryFramePump>(std::move(source));

        pump->beginSourceEpoch(41);
        pump->submitFrameNotification(41, 1);
        QVERIFY(sourcePtr->waitForReadEntered(1));
        QCOMPARE(pump->stats().readAttempts, quint64{1});

        QElapsedTimer shutdownTimer;
        shutdownTimer.start();
        pump.reset();
        QVERIFY2(shutdownTimer.elapsed() < 500,
                 qPrintable(QStringLiteral("pump shutdown took %1 ms")
                                .arg(shutdownTimer.elapsed())));

        // Any already-posted affinity-thread wake-up must be removed with the
        // QObject; processing the loop after destruction must remain safe.
        QTest::qWait(10);
    }

    void pumpGuiStallDeliversOnlyNewestReadyFrame()
    {
        auto source = std::make_unique<ControlledFrameSource>();
        SharedMemoryFramePump pump(std::move(source));
        int imageBatches = 0;
        SharedMemoryFramePumpBatch newestBatch;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) {
                        ++imageBatches;
                        newestBatch = batch;
                    }
                });

        // Deliberately do not process Qt events while the worker completes
        // eight reads. This models a 40 ms GUI/render stall and proves that the
        // single posted wake-up owns one replaceable ready slot, not a FIFO.
        pump.beginSourceEpoch(51);
        constexpr int kFrames = 8;
        for (int frame = 1; frame <= kFrames; ++frame) {
            pump.submitFrameNotification(51, 1000 + frame);
            QElapsedTimer readTimer;
            readTimer.start();
            while (pump.stats().framesRead < static_cast<quint64>(frame)
                   && readTimer.elapsed() < 1000) {
                QThread::msleep(1);
            }
            QCOMPARE(pump.stats().framesRead, static_cast<quint64>(frame));
        }
        QThread::msleep(40);

        const auto stalledStats = pump.stats();
        QCOMPARE(stalledStats.deliveredBatches, quint64{0});
        QCOMPARE(stalledStats.maxReadyDepth, std::size_t{1});
        QCOMPARE(stalledStats.readyFrameReplaced,
                 static_cast<quint64>(kFrames - 1));

        QTRY_COMPARE_WITH_TIMEOUT(imageBatches, 1, 2000);
        QTest::qWait(20);
        QCOMPARE(imageBatches, 1);
        QCOMPARE(newestBatch.sourceEpoch, quint64{51});
        QCOMPARE(newestBatch.framesRead, quint64{kFrames});
        QCOMPARE(newestBatch.mappedFrameNumber, kFrames);
        QCOMPARE(newestBatch.eventFrameNumber, 1000 + kFrames);
        QCOMPARE(newestBatch.readyFrameReplaced,
                 static_cast<quint64>(kFrames - 1));
        QCOMPARE(newestBatch.deliveryScheduleFailures, quint64{0});
        QCOMPARE(pump.stats().deliveredBatches, quint64{1});
    }

    void remoteSessionAcceptsOnlyCurrentEpochJpegHandoff()
    {
        RemotePlaySession session;
        const QString currentEvent = QStringLiteral("OrionPreviewReady_currentepoch");
        RemotePlaySessionTestAccess::armShmEpoch(session, 91, currentEvent);

        const auto message = [](const QString& readyEvent, int firstJpegFrame) {
            return QJsonObject{
                {QStringLiteral("event"), QStringLiteral("preview_transport")},
                {QStringLiteral("protocol"), 1},
                {QStringLiteral("mode"), QStringLiteral("jpeg")},
                {QStringLiteral("reason"), QStringLiteral("shm_write_failures")},
                {QStringLiteral("shm_ready_event"), readyEvent},
                {QStringLiteral("first_jpeg_frame_number"), firstJpegFrame},
            };
        };

        // A delayed producer line from a previous sidecar generation cannot
        // retire the current mapping or alter its pipeline baseline.
        RemotePlaySessionTestAccess::handoff(
            session, message(QStringLiteral("OrionPreviewReady_staleepoch"), 40));
        QVERIFY(RemotePlaySessionTestAccess::shmActive(session));
        QVERIFY(!RemotePlaySessionTestAccess::jpegFallback(session));
        QCOMPARE(RemotePlaySessionTestAccess::pipelineBaseline(session), qint64{1234});

        // The exact current-epoch control record performs one in-place fence.
        RemotePlaySessionTestAccess::handoff(session, message(currentEvent, 42));
        QVERIFY(!RemotePlaySessionTestAccess::shmActive(session));
        QVERIFY(RemotePlaySessionTestAccess::jpegFallback(session));
        QCOMPARE(RemotePlaySessionTestAccess::firstJpegFrame(session), 42);
        QCOMPARE(RemotePlaySessionTestAccess::pipelineBaseline(session), qint64{0});

        const quint64 submittedBefore =
            RemotePlaySessionTestAccess::decoderSubmitted(session);
        RemotePlaySessionTestAccess::submitJpeg(session, 41);
        QCOMPARE(RemotePlaySessionTestAccess::decoderSubmitted(session), submittedBefore);
        RemotePlaySessionTestAccess::submitJpeg(session, 42);
        QCOMPARE(RemotePlaySessionTestAccess::decoderSubmitted(session), submittedBefore + 1);

        // A completion already queued from the retired epoch is ignored even
        // if it carries a valid image and a much newer cumulative read count.
        SharedMemoryFramePumpBatch late;
        late.sourceEpoch = 91;
        late.readerOpen = true;
        late.framesRead = 99;
        late.mappedFrameNumber = 41;
        late.image = QImage(8, 8, QImage::Format_RGB888);
        late.image.fill(Qt::red);
        RemotePlaySessionTestAccess::deliverShmBatch(session, late);
        QCOMPARE(RemotePlaySessionTestAccess::shmFramesRead(session), quint64{7});

        // Idempotent replay of the valid handoff cannot reopen/advance state.
        RemotePlaySessionTestAccess::handoff(session, message(currentEvent, 99));
        QCOMPARE(RemotePlaySessionTestAccess::firstJpegFrame(session), 42);
    }

    void remoteSessionBoundsShmPresentationClockDiagnostics()
    {
        RemotePlaySession session;
        RemotePlaySessionTestAccess::armShmEpoch(
            session, 92, QStringLiteral("OrionPreviewReady_clockdiag"));

        SharedMemoryFramePumpBatch valid;
        valid.sourceEpoch = 92;
        valid.readerOpen = true;
        valid.framesRead = 8;
        valid.mappedFrameNumber = 501;
        valid.sourceTimestampNs = 1'000'000'000ULL;
        valid.pumpReadCompletedTimestampNs = 1'025'000'000ULL;
        valid.presentationDispatchTimestampNs = 1'035'000'000ULL;
        valid.image = QImage(8, 8, QImage::Format_RGB888);
        valid.image.fill(Qt::green);
        RemotePlaySessionTestAccess::deliverShmBatch(session, valid);

        QCOMPARE(RemotePlaySessionTestAccess::shmFramesRead(session), quint64{8});
        QCOMPARE(RemotePlaySessionTestAccess::shmSourceToReadAgeMs(session), 25.0);
        QCOMPARE(RemotePlaySessionTestAccess::shmReadToDispatchAgeMs(session), 10.0);
        QCOMPARE(RemotePlaySessionTestAccess::shmSourceToDispatchAgeMs(session), 35.0);
        QCOMPARE(RemotePlaySessionTestAccess::shmPresentationTimestampRejects(session),
                 quint64{0});

        // A cross-domain/reversed triple is ignored for diagnostics only. The
        // current-epoch image and cumulative frame count still advance.
        SharedMemoryFramePumpBatch malformed = valid;
        malformed.framesRead = 9;
        malformed.mappedFrameNumber = 502;
        malformed.sourceTimestampNs = 2'000'000'000ULL;
        malformed.pumpReadCompletedTimestampNs = 1'900'000'000ULL;
        malformed.presentationDispatchTimestampNs = 2'100'000'000ULL;
        malformed.image.fill(Qt::blue);
        RemotePlaySessionTestAccess::deliverShmBatch(session, malformed);

        QCOMPARE(RemotePlaySessionTestAccess::shmFramesRead(session), quint64{9});
        QCOMPARE(RemotePlaySessionTestAccess::shmSourceToReadAgeMs(session), 25.0);
        QCOMPARE(RemotePlaySessionTestAccess::shmReadToDispatchAgeMs(session), 10.0);
        QCOMPARE(RemotePlaySessionTestAccess::shmSourceToDispatchAgeMs(session), 35.0);
        QCOMPARE(RemotePlaySessionTestAccess::shmPresentationTimestampRejects(session),
                 quint64{1});
    }
};

QTEST_MAIN(SharedMemoryFrameReaderTests)
#include "SharedMemoryFrameReaderTests.moc"
