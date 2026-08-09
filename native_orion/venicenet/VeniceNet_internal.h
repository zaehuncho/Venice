#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  VeniceNet_internal.h — internal C++ state behind the VeniceNet C ABI
// ───────────────────────────────────────────────────────────────────────────
//
//  NEVER include this from the app or any other consumer: everything here is
//  implementation detail of VeniceNet.dll and may change without an ABI bump.
//  The only contract is VeniceNet.h + docs/VENICENET_API.md.
//
//  WAVE 2B: `Core` is a thin IPC CLIENT. The privileged WinDivert engine lives
//  in a separate SYSTEM service (VeniceNetSvc.exe / NexusVisionSvc.exe). Core
//  owns:
//    * the configuration mirror (client-owned intent),
//    * a background I/O thread that maintains a loopback-TCP connection to the
//      bridge service, authenticates with the per-session bearer token, and
//      translates the ratified ABI verbs to/from the existing nexus_svc wire
//      protocol (newline-delimited JSON),
//    * the live snapshot facts derived from the service's broadcasts + the pipe
//      connection state,
//    * the single state callback.
//
//  Locking model:
//    mutex_      guards the config mirror + the live snapshot facts + callback.
//                Snapshots are composed under it and COPIED OUT before any
//                callback runs, so the callback never runs with an internal lock
//                held — that is what makes the "no venicenet_* re-entry from the
//                callback" rule a documentation contract, not deadlock roulette.
//    sendMutex_  guards the socket handle + outbound sends. A caller-thread
//                setter takes mutex_ (mirror), releases it, then sends under
//                sendMutex_ — the two are never nested, so there is no lock-order
//                inversion against the I/O thread.
// ───────────────────────────────────────────────────────────────────────────

#include "VeniceNet.h"

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>

namespace venicenet {

class Core {
public:
    // Process-wide singleton (the ABI is stateful-global by design: one DLL,
    // one bridge-connection ownership domain per process).
    static Core& instance();

    VeniceNetStatus init();
    void shutdown();

    VeniceNetStatus setEnabled(bool enabled);
    VeniceNetStatus setTargetDelayMs(double targetMs);
    VeniceNetStatus setEngagePolicy(VeniceNetEngagePolicy policy);
    VeniceNetStatus setOffense(bool offense);
    VeniceNetStatus setLive(bool liveBall);
    VeniceNetStatus setConsoleIp(const char* consoleIpv4);
    VeniceNetStatus notifyShotEdge();

    VeniceNetStatus snapshot(VeniceNetSnapshot* out) const;
    VeniceNetStatus registerStateCallback(VeniceNetStateCallback callback,
                                          void* userData);

private:
    Core() = default;
    ~Core();
    Core(const Core&) = delete;
    Core& operator=(const Core&) = delete;

    // ── snapshot / callback ──
    void fillSnapshotLocked(VeniceNetSnapshot& out) const;
    // Composes the snapshot under the lock, releases it, and invokes the
    // callback ONLY if the snapshot changed since the last invocation. Callers
    // must NOT hold mutex_.
    void notifyStateChangedIfChanged();
    // Unconditional prime (registration contract). Callers must NOT hold mutex_.
    void primeCallback();

    // ── I/O thread ──
    void ioThreadMain();
    // One connection lifecycle: connect, auth, serve until the socket drops.
    void runOneConnection();
    // Send a full line (already terminated with '\n') on the current socket.
    // Takes sendMutex_. Returns false if there is no connected socket or the
    // send failed.
    bool sendRaw(const std::string& line);
    // Replay the config mirror onto a freshly authenticated connection so a
    // transient disconnect never silently drops the enabled/target/console state.
    void replayConfigOnConnect();
    // Parse + act on one service line (I/O thread only).
    void handleServiceLine(const std::string& line);
    // Marks the connection down and clears the socket. I/O thread only.
    void markDisconnected(int32_t driverState, int32_t errorCode,
                          const std::string& errorText);

    // Wire-command emitters (no-op unless connected+authenticated). Each also
    // reflects the intent in the mirror via its caller.
    void wireSetConsoleIp(const std::string& ip);
    void wireSetEnabled(bool enabled);
    void wireSetTargetDelay(double ms);
    void wireSetEngagePolicy(VeniceNetEngagePolicy policy);
    void wireSetOffense(bool offense);
    void wireSetLive(bool liveBall);
    void wireNotifyShotEdge();

    mutable std::mutex mutex_;
    bool initialized_ = false;

    // ── Configuration mirror (client-owned intent) ──
    bool enabled_ = false;
    double targetDelayMs_ = 0.0;
    VeniceNetEngagePolicy policy_ = VENICENET_POLICY_ALWAYS_ON;
    bool offense_ = false;
    bool liveBall_ = true;
    std::string consoleIp_;

    // ── Live snapshot facts (fed by the I/O thread from service broadcasts) ──
    int32_t driverState_ = VENICENET_DRIVER_NOT_INSTALLED;
    double appliedDelayMs_ = 0.0;
    double latencyP50Ms_ = -1.0;
    double latencyP95Ms_ = -1.0;
    uint32_t bufferDepth_ = 0;
    bool armed_ = false;
    bool courtIpDetected_ = false;
    std::string courtIp_;
    int32_t errorCode_ = VENICENET_OK;
    std::string errorText_;

    // ── Callback registration + change-detection ──
    VeniceNetStateCallback callback_ = nullptr;
    void* callbackUserData_ = nullptr;
    VeniceNetSnapshot lastNotified_{};
    bool haveLastNotified_ = false;

    // ── Transport ──
    std::thread ioThread_;
    std::atomic<bool> ioRunning_{false};
    std::atomic<bool> connected_{false};   // authenticated + serving
    bool wsaReady_ = false;
    std::mutex sendMutex_;
    uintptr_t socket_ = ~static_cast<uintptr_t>(0); // INVALID_SOCKET
};

} // namespace venicenet
