#include "PassiveCourtFlowClassifier.h"

#include <QtTest/QTest>

using orion::PassiveCourtFlowClassifier;

namespace {

using Transport = PassiveCourtFlowClassifier::Transport;

void addBidirectionalFlow(PassiveCourtFlowClassifier& classifier,
                          const QString& privateIp,
                          const QString& publicIp,
                          int publicPort,
                          int packetCount,
                          qint64 startMs,
                          qint64 intervalMs)
{
    for (int index = 0; index < packetCount; ++index) {
        const qint64 observedMs = startMs + index * intervalMs;
        const bool inbound = (index % 2) != 0;
        const bool accepted = inbound
            ? classifier.observe(publicIp, privateIp, publicPort, 42000,
                                 Transport::Udp, observedMs)
            : classifier.observe(privateIp, publicIp, 42000, publicPort,
                                 Transport::Udp, observedMs);
        QVERIFY(accepted);
    }
}

} // namespace

class PassiveCourtFlowClassifierTests final : public QObject {
    Q_OBJECT

private slots:
    void acceptsOnlyUdpPublicCourtPortFlows()
    {
        PassiveCourtFlowClassifier classifier;

        QVERIFY(!classifier.observe(
            QStringLiteral("192.168.1.10"), QStringLiteral("8.8.8.8"),
            42000, 30000, Transport::Tcp, 1000));
        QVERIFY(!classifier.observe(
            QStringLiteral("192.168.1.10"), QStringLiteral("8.8.8.8"),
            30000, 443, Transport::Udp, 1010));
        QVERIFY(!classifier.observe(
            QStringLiteral("192.168.1.10"), QStringLiteral("203.0.113.7"),
            42000, 30000, Transport::Udp, 1020));
        QVERIFY(!classifier.observe(
            QStringLiteral("8.8.8.8"), QStringLiteral("1.1.1.1"),
            30000, 30001, Transport::Udp, 1030));
        QVERIFY(!classifier.observe(
            QStringLiteral("not-an-ip"), QStringLiteral("8.8.8.8"),
            42000, 30000, Transport::Udp, 1040));

        QVERIFY(classifier.observe(
            QStringLiteral("10.0.0.9"), QStringLiteral("8.8.8.8"),
            42000, 30000, Transport::Udp, 1050));
        QVERIFY(classifier.observe(
            QStringLiteral("1.1.1.1"), QStringLiteral("172.16.0.4"),
            30099, 42000, Transport::Udp, 1060));
    }

    void requiresSustainedBidirectionalEvidence()
    {
        PassiveCourtFlowClassifier classifier;
        const QString local = QStringLiteral("192.168.1.50");
        const QString remote = QStringLiteral("8.8.4.4");

        for (int index = 0; index < 20; ++index) {
            QVERIFY(classifier.observe(
                local, remote, 42000, 30042, Transport::Udp,
                1000 + index * 40));
        }
        auto snapshot = classifier.snapshot(1760);
        QVERIFY(snapshot.hasCandidate);
        QVERIFY(!snapshot.qualified);
        QVERIFY(snapshot.endpointIp.isEmpty());

        classifier.reset();
        addBidirectionalFlow(classifier, local, remote, 30042, 11, 2000, 40);
        snapshot = classifier.snapshot(2400);
        QVERIFY(!snapshot.qualified);

        QVERIFY(classifier.observe(
            remote, local, 30042, 42000, Transport::Udp, 2440));
        snapshot = classifier.snapshot(2440);
        QVERIFY(snapshot.qualified);
        QCOMPARE(snapshot.endpointIp, remote);
        QCOMPARE(snapshot.endpointPort, 30042);
        QCOMPARE(snapshot.inboundPackets, quint64(6));
        QCOMPARE(snapshot.outboundPackets, quint64(6));
        QVERIFY(snapshot.packetsPerSecond >=
                PassiveCourtFlowClassifier::kMinimumPacketsPerSecond);
    }

    void doesNotCombineDifferentEndpointsToReachThreshold()
    {
        PassiveCourtFlowClassifier classifier;
        const QString local = QStringLiteral("192.168.50.20");
        addBidirectionalFlow(
            classifier, local, QStringLiteral("8.8.8.8"), 30010, 6, 1000, 80);
        addBidirectionalFlow(
            classifier, local, QStringLiteral("1.1.1.1"), 30010, 6, 1480, 80);

        const auto snapshot = classifier.snapshot(1880);
        QVERIFY(snapshot.hasCandidate);
        QVERIFY(!snapshot.qualified);
        QVERIFY(snapshot.endpointIp.isEmpty());
    }

    void expiresAndClearsQualifiedEndpoint()
    {
        PassiveCourtFlowClassifier classifier;
        addBidirectionalFlow(
            classifier, QStringLiteral("192.168.1.25"),
            QStringLiteral("8.8.8.8"), 30099, 12, 1000, 40);

        auto snapshot = classifier.snapshot(1440);
        QVERIFY(snapshot.qualified);
        snapshot = classifier.snapshot(
            1440 + PassiveCourtFlowClassifier::kStaleAfterMs + 1);
        QVERIFY(!snapshot.hasCandidate);
        QVERIFY(!snapshot.qualified);
        QVERIFY(snapshot.endpointIp.isEmpty());
    }

    void clockRollbackFailsClosed()
    {
        PassiveCourtFlowClassifier classifier;
        addBidirectionalFlow(
            classifier, QStringLiteral("192.168.1.25"),
            QStringLiteral("8.8.8.8"), 30050, 12, 1000, 40);
        QVERIFY(classifier.snapshot(1440).qualified);

        QVERIFY(!classifier.observe(
            QStringLiteral("192.168.1.25"), QStringLiteral("8.8.8.8"),
            42000, 30050, Transport::Udp, 1200));
        QVERIFY(!classifier.snapshot(1440).hasCandidate);
    }
};

QTEST_APPLESS_MAIN(PassiveCourtFlowClassifierTests)

#include "PassiveCourtFlowClassifierTests.moc"
