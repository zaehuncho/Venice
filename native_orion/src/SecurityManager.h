#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>
#include <QtCore/QJsonObject>
#include <QtCore/QMetaType>
#include <QtCore/QObject>
#include <QtCore/QQueue>
#include <QtCore/QString>
#include <QtCore/QThread>
#include <QtCore/QTimer>

#include <atomic>
#include <functional>

namespace orion {

struct SecurityStatus {
    // False only when the evaluator itself failed/timed out. This is distinct
    // from a completed dev-policy verdict and can never be bypassed locally.
    bool evaluationComplete = true;
    QString machineId;
    bool debuggerObserved = false;
    bool vmObserved = false;
    bool sandboxObserved = false;
    bool analysisToolObserved = false;
    bool timingAnomalyObserved = false;
    bool settingsSignatureValid = false;
    bool executableIntegrityKnown = false;
    bool releaseManifestRequired = false;
    bool releaseManifestValid = false;
    // True when the running exe carries an Authenticode signature that FAILED validation (tampered/
    // untrusted) — NOT merely unsigned. Only locks when enforceAuthenticode() opts in (see .cpp).
    bool authenticodeTampered = false;
    bool securityLockActive = false;
    bool vigemBusInstalled = false;
    bool hidhideInstalled = false;
    QString securityLockReason;
    QString entitlementState = QStringLiteral("No cached entitlement");
    QString integrityState = QStringLiteral("Unchecked");
    QString lastSecurityAuditEvent;
    QString message;
};

class ORION_SECURITY_API SecurityManager final : public QObject {
    Q_OBJECT
public:
    explicit SecurityManager(QString rootDir, QObject* parent = nullptr);

    [[nodiscard]] QString machineId() const;
    [[nodiscard]] SecurityStatus evaluate();
    [[nodiscard]] bool validateLicenseKeyFormat(const QString& key) const;
    [[nodiscard]] bool verifySettingsSignature() const;
    [[nodiscard]] bool verifyReleaseIntegrity(QString* detail = nullptr) const;
    [[nodiscard]] bool vmOrSandboxObserved() const;
    [[nodiscard]] bool analysisToolRunning() const;
    [[nodiscard]] bool timingAnomalyDetected() const;
    [[nodiscard]] bool vigemBusInstalled() const;
    [[nodiscard]] bool hidhideInstalled() const;
    [[nodiscard]] bool releaseManifestRequired() const;
    [[nodiscard]] QString appBuildHash() const;
    [[nodiscard]] bool authenticodeValid() const;
    // True ONLY when the exe is signed but the signature is invalid (tampered/untrusted); false for
    // an unsigned or unparseable binary (so an unsigned legitimate release is never mistaken for a
    // tamper). Narrower than !authenticodeValid().
    [[nodiscard]] bool authenticodeSignedButInvalid() const;
    // Whether a signed-but-invalid exe should hard-lock. Opt-in (security policy enforce_authenticode)
    // and only in release policy; default OFF so signing can be rolled out without bricking a launch.
    [[nodiscard]] bool enforceAuthenticode() const;
    [[nodiscard]] QString entitlementState() const;
    bool writeSettingsSignature(QString* error = nullptr) const;
    bool cacheLocalEntitlement(const QJsonObject& entitlement, QString* error = nullptr) const;
    [[nodiscard]] QJsonObject loadLocalEntitlement(bool* ok = nullptr, QString* state = nullptr) const;
    bool clearLocalEntitlement(QString* error = nullptr) const;

    // [ORION_FIRST_RUN_SIG_BOOTSTRAP] Narrow public accessor for the OrionAppController
    // first-run bootstrap. Only the presence of the signature file is exposed -- callers
    // still cannot see settingsPath / settingsSigPath / the digest bytes.
    [[nodiscard]] bool hasSettingsSignature() const;

signals:
    void securityEvent(QString event, QString detail);

private:
    [[nodiscard]] QByteArray settingsDigest() const;
    [[nodiscard]] QString settingsPath() const;
    [[nodiscard]] QString settingsSigPath() const;
    [[nodiscard]] QString localEntitlementPath() const;
    [[nodiscard]] QString releaseManifestPath() const;
    [[nodiscard]] QString releaseManifestSignaturePath(const QString& manifestPath) const;
    [[nodiscard]] QString securityPolicyPath() const;
    [[nodiscard]] QJsonObject readJsonObject(const QString& path, qsizetype maxBytes = 1024 * 1024) const;
    [[nodiscard]] QByteArray readFileLimited(const QString& path, qsizetype maxBytes) const;
    [[nodiscard]] QByteArray protectBytes(const QByteArray& plain, bool* protectedByDpapi = nullptr) const;
    [[nodiscard]] QByteArray unprotectBytes(const QByteArray& blob, bool dpapiProtected, bool* ok = nullptr) const;
    [[nodiscard]] QJsonObject entitlementBinding() const;
    void auditSecurityEvent(const QString& event, const QString& detail);

    QString rootDir_;
    QString lastSecurityAuditEvent_;
};

// A monotonic authority epoch for asynchronous security work. Synchronous
// checkpoints (startup, connect, settings/license mutation, explicit integrity
// actions) invalidate every result that was started against older state.
class ORION_SECURITY_API SecurityEvaluationGenerationFence final {
public:
    [[nodiscard]] quint64 current() const noexcept { return generation_; }
    [[nodiscard]] quint64 invalidate() noexcept;
    [[nodiscard]] bool accepts(quint64 generation) const noexcept {
        return generation != 0 && generation == generation_;
    }

private:
    quint64 generation_ = 1;
};

// Converts an evaluator/worker failure into an explicitly locked verdict. The
// controller may carry forward display-only entitlement/integrity text, but it
// must never carry forward fire authority after the evaluator fails.
[[nodiscard]] ORION_SECURITY_API SecurityStatus
failClosedSecurityEvaluationStatus(const QString& detail);

// Runs the periodic, full security evaluation on one persistent worker thread.
// There is one active scan plus at most one replaceable pending generation;
// duplicate 1 Hz requests are coalesced instead of creating a stale backlog.
// The caller decides whether the generation-tagged result is authoritative.
class ORION_SECURITY_API PeriodicSecurityEvaluator final : public QObject {
    Q_OBJECT
public:
    using EvaluateFunction = std::function<SecurityStatus()>;
    // [ORION_SEC_WATCHDOG_TIMEOUT 2026-08-07] Raised 5000 -> 30000 ms. The 5 s
    // budget cost real launches: a cold NTFS + Defender scan of the 2828-file
    // manifest can legitimately take 6-15 s on first launch, and every one of
    // those launches fell through to the fail-closed watchdog. 30 s is still an
    // upper bound on a HONEST scan (Fix 6 keeps the cache viable across runs);
    // anything longer means the worker is genuinely wedged and we should reset.
    static constexpr int kDefaultEvaluationTimeoutMs = 30000;

    explicit PeriodicSecurityEvaluator(QString rootDir, QObject* parent = nullptr);
    // Deterministic test seam. Production always uses the rootDir constructor,
    // which owns one persistent worker-side SecurityManager.
    explicit PeriodicSecurityEvaluator(EvaluateFunction evaluator, QObject* parent = nullptr);
    PeriodicSecurityEvaluator(EvaluateFunction evaluator, int evaluationTimeoutMs,
                              QObject* parent = nullptr);
    ~PeriodicSecurityEvaluator() override;

    bool request(quint64 generation);
    [[nodiscard]] bool evaluationInFlight() const noexcept { return evaluationInFlight_; }
    [[nodiscard]] int pendingRequestCount() const noexcept { return pendingGenerations_.size(); }

signals:
    void evaluationStarted(quint64 generation);
    void evaluationFinished(quint64 generation, orion::SecurityStatus status);
    void evaluationFailed(quint64 generation, QString detail);
    void securityEvent(QString event, QString detail);

private:
    void initializeWorker(const QString& rootDir);
    void dispatchNext();
    void completeSuccess(quint64 generation, SecurityStatus status);
    void completeFailure(quint64 generation, QString detail);

    QThread workerThread_;
    QTimer evaluationWatchdog_;
    QObject* workerContext_ = nullptr;
    SecurityManager* workerSecurity_ = nullptr;
    EvaluateFunction evaluator_;
    QQueue<quint64> pendingGenerations_;
    quint64 activeGeneration_ = 0;
    bool evaluationInFlight_ = false;
    bool watchdogFailureEmitted_ = false;
    int evaluationTimeoutMs_ = kDefaultEvaluationTimeoutMs;
    std::atomic_bool stopping_{false};
};

} // namespace orion

Q_DECLARE_METATYPE(orion::SecurityStatus)
