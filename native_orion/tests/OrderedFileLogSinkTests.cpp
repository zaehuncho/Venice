#include "OrderedFileLogSink.h"

#include <QtCore/QDir>
#include <QtCore/QElapsedTimer>
#include <QtCore/QFile>
#include <QtCore/QTemporaryDir>
#include <QtTest/QTest>

using namespace orion;

namespace {

QByteArray lineEnding()
{
#ifdef Q_OS_WIN
    return QByteArray("\r\n");
#else
    return QByteArray("\n");
#endif
}

QByteArray readAll(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    return file.readAll();
}

void writeFile(const QString& path, const QByteArray& bytes)
{
    QFile file(path);
    QVERIFY2(file.open(QIODevice::WriteOnly | QIODevice::Truncate), qPrintable(file.errorString()));
    QCOMPARE(file.write(bytes), bytes.size());
    QVERIFY(file.flush());
}

} // namespace

class OrderedFileLogSinkTests final : public QObject {
    Q_OBJECT

private slots:
    void preservesBatchAndLineOrderWithinByteBound();
    void rotatesBeforeFirstPendingBatch();
    void stopDrainsAndRejectsLaterWrites();
    void failingStorageNeverBlocksProducerOrShutdown();
};

void OrderedFileLogSinkTests::preservesBatchAndLineOrderWithinByteBound()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("logs/orion_native.log"));

    OrderedFileLogSink::Options options;
    options.maxFileBytes = 1024 * 1024;
    options.rotationCheckBatches = 300;
    options.maxOutstandingBytes = 17; // deterministically forces cross-line UTF-8 chunking
    OrderedFileLogSink sink(path, options);

    QVERIFY(sink.enqueue({QStringLiteral("first"), QStringLiteral("méter tip")}));
    QVERIFY(sink.enqueue({QStringLiteral("third"), QStringLiteral("fourth line is longer")}));
    sink.drain();

    QByteArray expected;
    expected += QByteArray("first") + lineEnding();
    expected += QByteArray("m\xC3\xA9ter tip") + lineEnding();
    expected += QByteArray("third") + lineEnding();
    expected += QByteArray("fourth line is longer") + lineEnding();
    QCOMPARE(readAll(path), expected);
    const OrderedFileLogSink::Stats stats = sink.stats();
    QCOMPARE(stats.acceptedBatches, quint64(2));
    QCOMPARE(stats.acceptedLines, quint64(4));
    QCOMPARE(stats.writtenBatches, quint64(2));
    QCOMPARE(stats.writtenLines, quint64(4));
    QCOMPARE(stats.outstandingBytes, qsizetype(0));
    QVERIFY(stats.workerStarted);
    QVERIFY(stats.maxObservedOutstandingBytes <= options.maxOutstandingBytes);
}

void OrderedFileLogSinkTests::rotatesBeforeFirstPendingBatch()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString logsDir = dir.filePath(QStringLiteral("logs"));
    QVERIFY(QDir().mkpath(logsDir));
    const QString path = logsDir + QStringLiteral("/orion_native.log");
    writeFile(path, QByteArray("old-content"));
    writeFile(path + QStringLiteral(".1"), QByteArray("stale-one"));
    writeFile(path + QStringLiteral(".2"), QByteArray("stale-two"));

    OrderedFileLogSink::Options options;
    options.maxFileBytes = 3;
    options.rotationCheckBatches = 1;
    OrderedFileLogSink sink(path, options);
    QVERIFY(sink.enqueue({QStringLiteral("new")}));
    sink.drain();

    QCOMPARE(readAll(path), QByteArray("new") + lineEnding());
    QCOMPARE(readAll(path + QStringLiteral(".1")), QByteArray("old-content"));
    QVERIFY(!QFile::exists(path + QStringLiteral(".2")));
}

void OrderedFileLogSinkTests::stopDrainsAndRejectsLaterWrites()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("logs/orion_native.log"));

    OrderedFileLogSink sink(path);
    for (int i = 0; i < 200; ++i) {
        QVERIFY(sink.enqueue({QStringLiteral("line-%1").arg(i)}));
    }
    sink.stopAndDrain();
    sink.stopAndDrain(); // shutdown is intentionally idempotent

    QByteArray expected;
    for (int i = 0; i < 200; ++i) {
        expected.append(QStringLiteral("line-%1").arg(i).toUtf8());
        expected.append(lineEnding());
    }
    QCOMPARE(readAll(path), expected);
    QVERIFY(!sink.enqueue({QStringLiteral("too-late")}));
    const OrderedFileLogSink::Stats stats = sink.stats();
    QCOMPARE(stats.acceptedLines, quint64(200));
    QCOMPARE(stats.writtenLines, quint64(200));
    QVERIFY(stats.workerStopped);
}

// [M-02 / CX-015 2026-09-22] A log path that can never be opened (here: a directory sits where
// the file should be) must not block the producer - the GUI/input thread - or hang shutdown.
void OrderedFileLogSinkTests::failingStorageNeverBlocksProducerOrShutdown()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("logs/orion_native.log"));
    QVERIFY(QDir().mkpath(path));   // the "file" is a directory: every open fails

    OrderedFileLogSink::Options options;
    options.maxOutstandingBytes = 64;
    options.retryDelayMs = 5;
    options.admissionWaitMs = 250;
    options.shutdownGiveUpMs = 100;
    OrderedFileLogSink sink(path, options);

    QElapsedTimer timer;
    timer.start();
    for (int i = 0; i < 200; ++i) {
        QVERIFY(sink.enqueue({QStringLiteral("diagnostic line %1 with some padding").arg(i)}));
    }
    // At most one bounded healthy-storage wait before the fault is flagged; after that, drops
    // are immediate.
    QVERIFY2(timer.elapsed() < 2000, qPrintable(QString::number(timer.elapsed())));
    const OrderedFileLogSink::Stats faulted = sink.stats();
    QVERIFY(faulted.storageFault);
    QVERIFY(faulted.storageFaultEpisodes >= 1);
    QVERIFY(faulted.droppedBatches > 0);
    QVERIFY(faulted.outstandingBytes <= options.maxOutstandingBytes);

    timer.restart();
    sink.stopAndDrain();
    QVERIFY2(timer.elapsed() < 1500, qPrintable(QString::number(timer.elapsed())));
    QVERIFY(sink.stats().workerStopped);
    QVERIFY(!sink.enqueue({QStringLiteral("after stop")}));
}

QTEST_APPLESS_MAIN(OrderedFileLogSinkTests)

#include "OrderedFileLogSinkTests.moc"
