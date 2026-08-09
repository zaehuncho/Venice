#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  PacketBridgeServiceNames.h — which Windows service name(s) the packet
//  bridge may be registered under, and how the app resolves the one to use.
// ───────────────────────────────────────────────────────────────────────────
//
//  [ORION_PACKET_BRIDGE_NAMES 2026-08-08 task #64] Installer wave 3 registers
//  the bridge as VeniceNetSvc while every earlier customer install registered
//  NexusVisionSvc. OrionAppController's sc.exe start/query previously
//  hardcoded the legacy name, so on a wave-3 install the app never
//  demand-started the service and Meter Delay reported unavailable.
//
//  The single source of truth for the candidate list is
//  venicenet::candidateServiceNames() (venicenet/venicenet_service_route.cpp,
//  compiled into the consumers of this header): VeniceNetSvc first (current),
//  NexusVisionSvc second (legacy — customers on old installs MUST keep
//  working). This header only adapts it to Qt types and adds the pure
//  first-installed-wins resolution the sc.exe path uses, kept separable from
//  QProcess so scServiceStartTriesBothNames can pin the policy with a mocked
//  probe.
// ───────────────────────────────────────────────────────────────────────────

#include "venicenet_service_route.h"

#include <QtCore/QString>
#include <QtCore/QStringList>

#include <functional>

namespace orion {

// Candidate registered service names, most-current first, adapted from
// venicenet::candidateServiceNames() (the ratified migration-window list).
[[nodiscard]] inline QStringList packetBridgeServiceNameCandidates()
{
    QStringList out;
    for (const std::string& name : venicenet::candidateServiceNames()) {
        out.append(QString::fromStdString(name));
    }
    return out;
}

// Pure resolution policy: the first candidate the probe reports as a
// registered service wins. Returns an empty QString when none is installed.
// The production probe runs `sc.exe query <name>`; tests inject their own.
[[nodiscard]] inline QString resolveInstalledPacketBridgeServiceName(
    const QStringList& candidates,
    const std::function<bool(const QString&)>& installedProbe)
{
    if (!installedProbe) {
        return QString();
    }
    for (const QString& name : candidates) {
        if (installedProbe(name)) {
            return name;
        }
    }
    return QString();
}

} // namespace orion
