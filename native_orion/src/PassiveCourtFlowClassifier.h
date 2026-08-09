#pragma once

#include <QtCore/QHash>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QVector>
#include <QtCore/QtGlobal>

#include <algorithm>
#include <cmath>

namespace orion {

// A deliberately narrow, passive classifier for the Network diagnostics page.
//
// This type has no socket, probe, RTT, or automation dependency. It consumes
// observations stamped with the caller's local monotonic clock and exposes only
// flow identity/cadence. A result from this class must never be treated as timing
// authority; it merely says that sustained traffic looks like a court flow.
class PassiveCourtFlowClassifier final {
public:
    enum class Transport {
        Udp,
        Tcp,
        Other,
    };

    static constexpr int kCourtPortMin = 30000;
    static constexpr int kCourtPortMax = 30099;
    static constexpr int kMinimumPackets = 12;
    static constexpr int kMinimumPacketsPerDirection = 3;
    static constexpr qint64 kMinimumObservationSpanMs = 400;
    static constexpr double kMinimumPacketsPerSecond = 10.0;
    static constexpr qint64 kObservationWindowMs = 1500;
    static constexpr qint64 kStaleAfterMs = 2000;
    static constexpr int kMaxTrackedEndpoints = 16;
    static constexpr int kMaxSamplesPerEndpoint = 512;

    struct Snapshot {
        bool hasCandidate = false;
        bool qualified = false;
        QString endpointIp;
        int endpointPort = 0;
        quint64 inboundPackets = 0;
        quint64 outboundPackets = 0;
        int recentPackets = 0;
        int inboundRun = 0;
        int outboundRun = 0;
        double packetsPerSecond = 0.0;
        double packetIntervalMs = 0.0;
        double inboundIntervalMs = 0.0;
        double outboundIntervalMs = 0.0;
        qint64 ageMs = 0;
        int qualifiedEndpointCount = 0;
    };

    // Returns true only when the observation belongs to a syntactically valid
    // private-LAN <-> public-IPv4 UDP flow whose PUBLIC port is 30000-30099.
    // Protocol validation remains at the caller so this helper cannot silently
    // reinterpret a TCP event as a UDP court candidate.
    bool observe(const QString& srcIp,
                 const QString& dstIp,
                 int srcPort,
                 int dstPort,
                 Transport transport,
                 qint64 observedAtMonotonicMs)
    {
        if (transport != Transport::Udp
            || observedAtMonotonicMs < 0 || srcPort <= 0 || srcPort > 65535
            || dstPort <= 0 || dstPort > 65535) {
            return false;
        }
        if (lastClockMs_ >= 0 && observedAtMonotonicMs < lastClockMs_) {
            // A monotonic-clock rollback means the caller violated the API
            // contract. Drop all accumulated evidence instead of making stale
            // traffic appear fresh.
            reset();
            return false;
        }
        lastClockMs_ = observedAtMonotonicMs;
        prune(observedAtMonotonicMs);

        const Ipv4Kind srcKind = ipv4Kind(srcIp);
        const Ipv4Kind dstKind = ipv4Kind(dstIp);
        const bool outbound = srcKind == Ipv4Kind::PrivateLan
            && dstKind == Ipv4Kind::Public;
        const bool inbound = srcKind == Ipv4Kind::Public
            && dstKind == Ipv4Kind::PrivateLan;
        if (!outbound && !inbound) {
            return false;
        }

        const QString publicIp = outbound ? dstIp : srcIp;
        const int publicPort = outbound ? dstPort : srcPort;
        if (publicPort < kCourtPortMin || publicPort > kCourtPortMax) {
            return false;
        }

        const QString key = endpointKey(publicIp, publicPort);
        auto it = flows_.find(key);
        if (it == flows_.end()) {
            if (flows_.size() >= kMaxTrackedEndpoints) {
                // Bound memory and fail closed under a flood of distinct peers.
                return false;
            }
            Flow flow;
            flow.ip = publicIp;
            flow.port = publicPort;
            flow.lastObservedMs = observedAtMonotonicMs;
            it = flows_.insert(key, flow);
        }

        Flow& flow = it.value();
        flow.lastObservedMs = observedAtMonotonicMs;
        if (outbound) {
            ++flow.outboundTotal;
        } else {
            ++flow.inboundTotal;
        }
        flow.samples.push_back({observedAtMonotonicMs, inbound});
        if (flow.samples.size() > kMaxSamplesPerEndpoint) {
            flow.samples.remove(0, flow.samples.size() - kMaxSamplesPerEndpoint);
        }
        trimWindow(flow, observedAtMonotonicMs);
        return true;
    }

    Snapshot snapshot(qint64 nowMonotonicMs)
    {
        if (nowMonotonicMs < 0) {
            return {};
        }
        if (lastClockMs_ >= 0 && nowMonotonicMs < lastClockMs_) {
            reset();
            return {};
        }
        lastClockMs_ = nowMonotonicMs;
        prune(nowMonotonicMs);

        QString strongestQualified;
        QString strongestCandidate;
        int strongestQualifiedPackets = -1;
        int strongestCandidatePackets = -1;
        double strongestQualifiedRate = -1.0;
        double strongestCandidateRate = -1.0;
        int qualifiedCount = 0;

        for (auto it = flows_.begin(); it != flows_.end(); ++it) {
            trimWindow(it.value(), nowMonotonicMs);
            const int packets = it.value().samples.size();
            const double rate = packetRate(it.value());
            const bool qualified = isQualified(it.value(), nowMonotonicMs, rate);
            if (qualified) {
                ++qualifiedCount;
                if (betterCandidate(it.key(), packets, rate,
                                    strongestQualified, strongestQualifiedPackets,
                                    strongestQualifiedRate)) {
                    strongestQualified = it.key();
                    strongestQualifiedPackets = packets;
                    strongestQualifiedRate = rate;
                }
            }
            if (betterCandidate(it.key(), packets, rate,
                                strongestCandidate, strongestCandidatePackets,
                                strongestCandidateRate)) {
                strongestCandidate = it.key();
                strongestCandidatePackets = packets;
                strongestCandidateRate = rate;
            }
        }

        // Retain a still-qualified endpoint to avoid display flicker when two
        // legitimate peers briefly trade the highest packet count.
        if (!activeKey_.isEmpty()) {
            const auto active = flows_.constFind(activeKey_);
            if (active != flows_.constEnd()
                && isQualified(active.value(), nowMonotonicMs,
                               packetRate(active.value()))) {
                strongestQualified = activeKey_;
            }
        }
        if (!strongestQualified.isEmpty()) {
            activeKey_ = strongestQualified;
        } else {
            activeKey_.clear();
        }

        const QString selectedKey = !strongestQualified.isEmpty()
            ? strongestQualified : strongestCandidate;
        if (selectedKey.isEmpty()) {
            return {};
        }

        const auto selectedIt = flows_.constFind(selectedKey);
        if (selectedIt == flows_.constEnd()) {
            return {};
        }
        const Flow& selected = selectedIt.value();
        Snapshot result;
        result.hasCandidate = true;
        result.qualified = !strongestQualified.isEmpty();
        if (result.qualified) {
            result.endpointIp = selected.ip;
            result.endpointPort = selected.port;
        }
        result.inboundPackets = selected.inboundTotal;
        result.outboundPackets = selected.outboundTotal;
        result.recentPackets = selected.samples.size();
        result.packetsPerSecond = packetRate(selected);
        result.packetIntervalMs = meanInterval(selected.samples, -1);
        result.inboundIntervalMs = meanInterval(selected.samples, 1);
        result.outboundIntervalMs = meanInterval(selected.samples, 0);
        result.ageMs = std::max<qint64>(0, nowMonotonicMs - selected.lastObservedMs);
        result.qualifiedEndpointCount = qualifiedCount;
        directionalRuns(selected.samples, result.inboundRun, result.outboundRun);
        return result;
    }

    void reset()
    {
        flows_.clear();
        activeKey_.clear();
        lastClockMs_ = -1;
    }

private:
    enum class Ipv4Kind {
        Invalid,
        PrivateLan,
        Public,
        Other,
    };

    struct Sample {
        qint64 observedMs = 0;
        bool inbound = false;
    };

    struct Flow {
        QString ip;
        int port = 0;
        quint64 inboundTotal = 0;
        quint64 outboundTotal = 0;
        qint64 lastObservedMs = 0;
        QVector<Sample> samples;
    };

    static QString endpointKey(const QString& ip, int port)
    {
        return QStringLiteral("%1:%2").arg(ip).arg(port);
    }

    static Ipv4Kind ipv4Kind(const QString& ip)
    {
        const QStringList pieces = ip.split(QLatin1Char('.'), Qt::KeepEmptyParts);
        if (pieces.size() != 4) {
            return Ipv4Kind::Invalid;
        }

        quint32 value = 0;
        for (const QString& piece : pieces) {
            if (piece.isEmpty() || piece.size() > 3) {
                return Ipv4Kind::Invalid;
            }
            for (const QChar ch : piece) {
                if (!ch.isDigit()) {
                    return Ipv4Kind::Invalid;
                }
            }
            bool ok = false;
            const int octet = piece.toInt(&ok);
            if (!ok || octet < 0 || octet > 255) {
                return Ipv4Kind::Invalid;
            }
            value = (value << 8U) | static_cast<quint32>(octet);
        }

        const quint32 first = value >> 24U;
        const quint32 second = (value >> 16U) & 0xffU;
        if (first == 10U
            || (first == 172U && second >= 16U && second <= 31U)
            || (first == 192U && second == 168U)) {
            return Ipv4Kind::PrivateLan;
        }

        // Reject non-routable, shared, loopback, link-local, documentation,
        // benchmarking, multicast, and reserved ranges. They are neither a
        // private console address nor a public court endpoint.
        const bool nonPublic = first == 0U
            || first == 127U
            || (first == 100U && second >= 64U && second <= 127U)
            || (first == 169U && second == 254U)
            || (first == 192U && second == 0U && ((value >> 8U) & 0xffU) == 0U)
            || (first == 192U && second == 0U && ((value >> 8U) & 0xffU) == 2U)
            || (first == 192U && second == 88U && ((value >> 8U) & 0xffU) == 99U)
            || (first == 198U && (second == 18U || second == 19U))
            || (first == 198U && second == 51U && ((value >> 8U) & 0xffU) == 100U)
            || (first == 203U && second == 0U && ((value >> 8U) & 0xffU) == 113U)
            || first >= 224U;
        return nonPublic ? Ipv4Kind::Other : Ipv4Kind::Public;
    }

    static void trimWindow(Flow& flow, qint64 nowMs)
    {
        const qint64 cutoff = nowMs - kObservationWindowMs;
        int removeCount = 0;
        while (removeCount < flow.samples.size()
               && flow.samples.at(removeCount).observedMs < cutoff) {
            ++removeCount;
        }
        if (removeCount > 0) {
            flow.samples.remove(0, removeCount);
        }
    }

    void prune(qint64 nowMs)
    {
        for (auto it = flows_.begin(); it != flows_.end();) {
            if (nowMs - it.value().lastObservedMs > kStaleAfterMs) {
                if (it.key() == activeKey_) {
                    activeKey_.clear();
                }
                it = flows_.erase(it);
            } else {
                trimWindow(it.value(), nowMs);
                ++it;
            }
        }
    }

    static double packetRate(const Flow& flow)
    {
        if (flow.samples.size() < 2) {
            return 0.0;
        }
        const qint64 span = flow.samples.constLast().observedMs
            - flow.samples.constFirst().observedMs;
        if (span <= 0) {
            return 0.0;
        }
        return static_cast<double>(flow.samples.size() - 1) * 1000.0
            / static_cast<double>(span);
    }

    static bool isQualified(const Flow& flow, qint64 nowMs, double rate)
    {
        if (flow.samples.size() < kMinimumPackets
            || nowMs - flow.lastObservedMs > kStaleAfterMs) {
            return false;
        }
        const qint64 span = flow.samples.constLast().observedMs
            - flow.samples.constFirst().observedMs;
        int inbound = 0;
        for (const Sample& sample : flow.samples) {
            inbound += sample.inbound ? 1 : 0;
        }
        const int outbound = flow.samples.size() - inbound;
        return span >= kMinimumObservationSpanMs
            && rate >= kMinimumPacketsPerSecond
            && inbound >= kMinimumPacketsPerDirection
            && outbound >= kMinimumPacketsPerDirection;
    }

    static bool betterCandidate(const QString& key,
                                int packets,
                                double rate,
                                const QString& incumbentKey,
                                int incumbentPackets,
                                double incumbentRate)
    {
        if (packets != incumbentPackets) {
            return packets > incumbentPackets;
        }
        if (std::abs(rate - incumbentRate) > 0.0001) {
            return rate > incumbentRate;
        }
        return incumbentKey.isEmpty() || key < incumbentKey;
    }

    // direction: -1 = all, 0 = outbound, 1 = inbound.
    static double meanInterval(const QVector<Sample>& samples, int direction)
    {
        qint64 previous = -1;
        qint64 sum = 0;
        int intervals = 0;
        for (const Sample& sample : samples) {
            if (direction >= 0 && static_cast<int>(sample.inbound) != direction) {
                continue;
            }
            if (previous >= 0 && sample.observedMs > previous) {
                sum += sample.observedMs - previous;
                ++intervals;
            }
            previous = sample.observedMs;
        }
        return intervals > 0
            ? static_cast<double>(sum) / static_cast<double>(intervals)
            : 0.0;
    }

    static void directionalRuns(const QVector<Sample>& samples,
                                int& inboundRun,
                                int& outboundRun)
    {
        inboundRun = 0;
        outboundRun = 0;
        if (samples.isEmpty()) {
            return;
        }
        const bool tailInbound = samples.constLast().inbound;
        int run = 0;
        for (auto it = samples.crbegin(); it != samples.crend(); ++it) {
            if (it->inbound != tailInbound) {
                break;
            }
            ++run;
        }
        if (tailInbound) {
            inboundRun = run;
        } else {
            outboundRun = run;
        }
    }

    QHash<QString, Flow> flows_;
    QString activeKey_;
    qint64 lastClockMs_ = -1;
};

} // namespace orion
