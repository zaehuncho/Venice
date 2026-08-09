// ───────────────────────────────────────────────────────────────────────────
//  VeniceNetClient.cpp — dynamic loader + Qt marshaling for VeniceNet.dll
// ───────────────────────────────────────────────────────────────────────────

#include "VeniceNetClient.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDir>

#include <windows.h>

namespace orion {

VeniceNetClient::VeniceNetClient(QObject* parent)
    : QObject(parent)
{
}

VeniceNetClient::~VeniceNetClient()
{
    // Contract order: unregister the callback FIRST (so no callback fires into a
    // half-destroyed object), then shutdown (which drains the DLL I/O thread),
    // then FreeLibrary.
    if (registerCb_ != nullptr) {
        registerCb_(nullptr, nullptr);
    }
    if (shutdown_ != nullptr) {
        shutdown_();
    }
    available_.store(false);
    if (module_ != nullptr) {
        ::FreeLibrary(static_cast<HMODULE>(module_));
        module_ = nullptr;
    }
}

bool VeniceNetClient::ensureLoaded()
{
    if (available_.load()) {
        return true;
    }
    if (module_ == nullptr) {
        // App dir first (CMake stages VeniceNet.dll next to OrionNative.exe),
        // then bare name (PATH / default search).
        const QString appDll = QDir::toNativeSeparators(
            QCoreApplication::applicationDirPath() + QStringLiteral("/VeniceNet.dll"));
        module_ = ::LoadLibraryW(reinterpret_cast<const wchar_t*>(appDll.utf16()));
        if (module_ == nullptr) {
            module_ = ::LoadLibraryW(L"VeniceNet.dll");
        }
        if (module_ == nullptr) {
            return false; // stale install missing the DLL — degrade gracefully
        }
    }

    const HMODULE mod = static_cast<HMODULE>(module_);
    const auto resolve = [mod](const char* name) -> void* {
        return reinterpret_cast<void*>(::GetProcAddress(mod, name));
    };

    abiVersion_   = reinterpret_cast<AbiVersionFn>(resolve("venicenet_abi_version"));
    init_         = reinterpret_cast<InitFn>(resolve("venicenet_init"));
    shutdown_     = reinterpret_cast<ShutdownFn>(resolve("venicenet_shutdown"));
    setEnabled_   = reinterpret_cast<SetEnabledFn>(resolve("venicenet_set_enabled"));
    setTarget_    = reinterpret_cast<SetTargetFn>(resolve("venicenet_set_target_delay_ms"));
    setPolicy_    = reinterpret_cast<SetPolicyFn>(resolve("venicenet_set_engage_policy"));
    setOffense_   = reinterpret_cast<SetOffenseFn>(resolve("venicenet_set_offense"));
    setLive_      = reinterpret_cast<SetLiveFn>(resolve("venicenet_set_live"));
    setConsoleIp_ = reinterpret_cast<SetConsoleIpFn>(resolve("venicenet_set_console_ip"));
    shotEdge_     = reinterpret_cast<ShotEdgeFn>(resolve("venicenet_notify_shot_edge"));
    registerCb_   = reinterpret_cast<RegisterCbFn>(resolve("venicenet_register_state_callback"));

    if (abiVersion_ == nullptr || init_ == nullptr || shutdown_ == nullptr
        || registerCb_ == nullptr || setEnabled_ == nullptr || setTarget_ == nullptr) {
        return false; // incomplete surface — refuse to use it
    }

    // ABI handshake: refuse a DLL whose ABI differs from the header we built
    // against (the client must not drive a mismatched engine).
    if (abiVersion_() != VENICENET_ABI_VERSION) {
        return false;
    }

    if (init_() != VENICENET_OK) {
        return false;
    }
    // Registering primes the callback once synchronously, seeding last_.
    registerCb_(&VeniceNetClient::stateCallbackThunk, this);
    available_.store(true);
    return true;
}

void VeniceNetClient::stateCallbackThunk(const VeniceNetSnapshot* snap, void* user)
{
    if (snap == nullptr || user == nullptr) {
        return;
    }
    static_cast<VeniceNetClient*>(user)->onSnapshot(*snap);
}

void VeniceNetClient::onSnapshot(const VeniceNetSnapshot& snap)
{
    // Runs on the DLL's I/O thread (or the caller thread during priming). Copy
    // out under the lock; NEVER call back into venicenet_* here (contract).
    {
        QMutexLocker lock(&mutex_);
        last_.driverState = snap.driver_state;
        last_.appliedDelayMs = snap.applied_delay_ms;
        last_.targetDelayMs = snap.target_delay_ms;
        last_.armed = snap.armed != 0;
        last_.enabled = snap.enabled != 0;
        last_.courtIpDetected = snap.court_ip_detected != 0;
        last_.errorCode = snap.error_code;
        last_.courtIp = QString::fromUtf8(snap.court_ip);
        last_.errorText = QString::fromUtf8(snap.error_text);
    }
    driverState_.store(snap.driver_state);
    armed_.store(snap.armed != 0);
    // Only quote the applied delay while the intercept is actually holding; a
    // stale value must not leak once the service reports inactive.
    appliedMs_.store(snap.armed != 0 ? snap.applied_delay_ms : -1.0);
    // Queued to the app thread (this object lives there; emit is from the worker).
    emit snapshotChanged();
}

VeniceNetClient::Snapshot VeniceNetClient::snapshot() const
{
    QMutexLocker lock(&mutex_);
    return last_;
}

void VeniceNetClient::setEnabled(bool enabled)
{
    if (available_.load() && setEnabled_ != nullptr) {
        setEnabled_(enabled);
    }
}

void VeniceNetClient::setTargetDelayMs(double ms)
{
    if (available_.load() && setTarget_ != nullptr) {
        setTarget_(ms);
    }
}

void VeniceNetClient::setEngagePolicy(int policy)
{
    if (available_.load() && setPolicy_ != nullptr) {
        setPolicy_(static_cast<VeniceNetEngagePolicy>(policy));
    }
}

void VeniceNetClient::setOffense(bool offense)
{
    if (available_.load() && setOffense_ != nullptr) {
        setOffense_(offense);
    }
}

void VeniceNetClient::setLive(bool liveBall)
{
    if (available_.load() && setLive_ != nullptr) {
        setLive_(liveBall);
    }
}

void VeniceNetClient::setConsoleIp(const QString& ipv4)
{
    if (available_.load() && setConsoleIp_ != nullptr) {
        const QByteArray utf8 = ipv4.toUtf8();
        setConsoleIp_(ipv4.isEmpty() ? nullptr : utf8.constData());
    }
}

void VeniceNetClient::notifyShotEdge()
{
    if (available_.load() && shotEdge_ != nullptr) {
        shotEdge_();
    }
}

} // namespace orion
