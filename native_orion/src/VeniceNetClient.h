#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  VeniceNetClient — Qt wrapper that loads VeniceNet.dll and drives it
// ───────────────────────────────────────────────────────────────────────────
//
//  WAVE 2B: the meter-delay actuation path no longer goes through NetworkBridge's
//  hand-rolled pipe client. OrionAppController owns one of these; it LoadLibrary's
//  VeniceNet.dll at first use, checks the ABI version, calls venicenet_init(), and
//  registers a state callback. The DLL invokes that callback on its own I/O
//  thread; this wrapper copies the snapshot out and re-emits it as a Qt signal so
//  the app consumes state on its own thread (the DLL's "no venicenet_* re-entry
//  from the callback" rule is honoured because the wrapper never calls back in).
//
//  The DLL is resolved dynamically (GetProcAddress), never linked against its
//  import lib, so a stale install missing VeniceNet.dll degrades to
//  available()==false rather than a load-time failure.
// ───────────────────────────────────────────────────────────────────────────

#include "VeniceNet.h"

#include <QtCore/QMutex>
#include <QtCore/QObject>
#include <QtCore/QString>

#include <atomic>

namespace orion {

class VeniceNetClient final : public QObject {
    Q_OBJECT
public:
    explicit VeniceNetClient(QObject* parent = nullptr);
    ~VeniceNetClient() override;

    // Loads VeniceNet.dll (app dir first, then PATH), verifies the ABI version,
    // calls venicenet_init() and registers the state callback. Idempotent; a
    // repeat call after success is a no-op. Returns true once initialised.
    bool ensureLoaded();
    [[nodiscard]] bool available() const noexcept { return available_.load(); }

    // Cheap lock-free reads for hot getters (Q_PROPERTY evaluation).
    [[nodiscard]] int driverState() const noexcept { return driverState_.load(); }
    [[nodiscard]] bool armed() const noexcept { return armed_.load(); }
    // Service-reported applied delay, or < 0 when unknown/not connected.
    [[nodiscard]] double appliedDelayMs() const noexcept { return appliedMs_.load(); }

    // A coherent copy of the last snapshot (strings included).
    struct Snapshot {
        int driverState = VENICENET_DRIVER_NOT_INSTALLED;
        double appliedDelayMs = -1.0;
        double targetDelayMs = 0.0;
        bool armed = false;
        bool enabled = false;
        bool courtIpDetected = false;
        int errorCode = VENICENET_OK;
        QString courtIp;
        QString errorText;
    };
    [[nodiscard]] Snapshot snapshot() const;

    // ── Configuration setters (no-op when the DLL is not loaded) ──
    void setEnabled(bool enabled);
    void setTargetDelayMs(double ms);
    void setEngagePolicy(int policy);   // VeniceNetEngagePolicy value
    void setOffense(bool offense);
    void setLive(bool liveBall);
    void setConsoleIp(const QString& ipv4);
    void notifyShotEdge();

signals:
    // Emitted (queued from the DLL's I/O thread) on every observable state
    // change and once on registration priming.
    void snapshotChanged();

private:
    static void stateCallbackThunk(const VeniceNetSnapshot* snap, void* user);
    void onSnapshot(const VeniceNetSnapshot& snap);

    // Resolved DLL exports (function pointers; never a static import).
    using AbiVersionFn   = uint32_t (*)();
    using InitFn         = VeniceNetStatus (*)();
    using ShutdownFn     = void (*)();
    using SetEnabledFn   = VeniceNetStatus (*)(bool);
    using SetTargetFn    = VeniceNetStatus (*)(double);
    using SetPolicyFn    = VeniceNetStatus (*)(VeniceNetEngagePolicy);
    using SetOffenseFn   = VeniceNetStatus (*)(bool);
    using SetLiveFn      = VeniceNetStatus (*)(bool);
    using SetConsoleIpFn = VeniceNetStatus (*)(const char*);
    using ShotEdgeFn     = VeniceNetStatus (*)();
    using RegisterCbFn   = VeniceNetStatus (*)(VeniceNetStateCallback, void*);

    void* module_ = nullptr;              // HMODULE
    std::atomic<bool> available_{false};
    std::atomic<int> driverState_{VENICENET_DRIVER_NOT_INSTALLED};
    std::atomic<bool> armed_{false};
    std::atomic<double> appliedMs_{-1.0};

    AbiVersionFn   abiVersion_   = nullptr;
    InitFn         init_         = nullptr;
    ShutdownFn     shutdown_     = nullptr;
    SetEnabledFn   setEnabled_   = nullptr;
    SetTargetFn    setTarget_    = nullptr;
    SetPolicyFn    setPolicy_    = nullptr;
    SetOffenseFn   setOffense_   = nullptr;
    SetLiveFn      setLive_      = nullptr;
    SetConsoleIpFn setConsoleIp_ = nullptr;
    ShotEdgeFn     shotEdge_     = nullptr;
    RegisterCbFn   registerCb_   = nullptr;

    mutable QMutex mutex_;
    Snapshot last_;
};

} // namespace orion
