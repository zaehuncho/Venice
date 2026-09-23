#pragma once

#include <QtCore/QString>

// [SERVER-SHARD D3 — machine_id parity, 2026-09-19]
// ONE translation unit (MachineIdentity.cpp) is compiled into BOTH SecurityCore
// (SecurityManager::machineId) and the OrionActivate broker so the machine_id
// that /api/activate binds is byte-identical to the id the in-app client would
// send. Do NOT hand-reimplement this anywhere else: a second derivation is the
// stub-vs-SecurityManager drift design D3 forbids. If this ever drifts, every
// clean install fails machine binding (fails CLOSED — no security hole — but the
// app never starts). OrionMachineIdParityTests pins the invariant.
namespace orion {

// 64 lowercase hex characters. Hashes QSysInfo::machineHostName() +
// QSysInfo::machineUniqueId() + HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid
// with SHA-256, joined by '|'. Not exported: each binary compiles its own copy
// from this shared source, which is exactly what the parity test verifies.
QString deriveMachineId();

} // namespace orion
