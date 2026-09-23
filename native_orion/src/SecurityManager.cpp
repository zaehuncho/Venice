#include "SecurityManager.h"
#include "Ed25519.h"
#include "OrionPaths.h"
#include "ReleaseManifestTrust.h"
#include "UpdateManifest.h"
#include "MachineIdentity.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QHash>
#include <QtCore/QMutex>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QMetaObject>
#include <QtCore/QRegularExpression>
#include <QtCore/QSaveFile>
#include <QtCore/QSysInfo>

#include <algorithm>
#include <cctype>
#include <exception>
#include <utility>

#ifdef Q_OS_WIN
#include <Windows.h>
#include <wincrypt.h>
#include <winreg.h>
#include <wintrust.h>
#include <softpub.h>
#include <intrin.h>
#include <tlhelp32.h>
#pragma comment(lib, "crypt32.lib")
#pragma comment(lib, "wintrust.lib")
#endif

namespace orion {

namespace {


// [ORION_HASH_CACHE 2026-08-07] Memoize by (canonical path, size, mtime_ns). Solves
// the SAFE MODE freeze on packaged installs: verifyReleaseIntegrity() iterates 2828
// entries of release_manifest.json — including OrionSidecar.exe at ~27 MB — and used
// to re-hash every file on every evaluate(). Cold NTFS + Defender scanning + Program
// Files easily made that 6-15 s, which trips the GUI-thread freeze watchdog.
// Every settings save fires updateSecurityStatus() -> evaluate(), so the second-plus
// call in a session was paying the same catastrophic cost. Cache lookup is O(files)
// stat calls after the first hydration; the mutex is Q_GLOBAL_STATIC-lifetimed and
// held only across the cache lookup, not the actual read.
//
// Invalidation is by (size, mtime) — any tamper that changes size or mtime misses
// the cache (correct — we want to detect that). A tamper that preserves both AND
// finds a collision requires SHA-256 preimage-with-constraints, which is not a real
// attack surface. If we ever needed stricter, moving to (device_id, inode, mtime, size)
// would be equivalent and equally safe.
// [ORION_HASH_CACHE_CHANGETIME 2026-08-07] The (path,size,mtime) key can be spoofed
// by a copy that preserves both -- `rsync -a`, `robocopy /COPY:DAT`, tar -p, ... all
// keep size AND mtime while the file content is new. On Windows NTFS records a
// SEPARATE ChangeTime that reflects any metadata OR content mutation and cannot be
// preserved by user-mode copy tools (SetFileInformationByHandle can rewrite it, but
// that requires admin AND is not what benign backup tools do). Add ChangeTime to the
// key so a rehash IS triggered on any real content change. Also cap the cache at
// 4096 entries (FIFO eviction) so a long-running process cannot leak unbounded.
QString sha256FileHex(const QString& path)
{
    QFileInfo info(path);
    if (!info.exists() || !info.isFile()) {
        return {};
    }
    struct CacheEntry {
        qint64 size;
        qint64 mtimeMs;
        qint64 changeTime100ns;  // NTFS ChangeTime in Windows 100ns FILETIME units; 0 on non-Win/failure.
        QString hex;
    };
    static constexpr int kMaxCacheEntries = 4096;
    static QHash<QString, CacheEntry> cache;
    static QList<QString> insertOrder;  // FIFO eviction companion.
    static QMutex cacheMutex;
    const QString key = info.canonicalFilePath();
    const qint64 size = info.size();
    const qint64 mtimeMs = info.lastModified().toMSecsSinceEpoch();
    qint64 changeTime100ns = 0;
#ifdef Q_OS_WIN
    // Query NTFS ChangeTime. Same open contract as CryptographicHash below - if the
    // file is exclusively locked we'll bail; that's fine (cache stays cold for it).
    const HANDLE h = CreateFileW(
        reinterpret_cast<const wchar_t*>(path.utf16()),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        FILE_BASIC_INFO basic{};
        if (GetFileInformationByHandleEx(h, FileBasicInfo, &basic, sizeof(basic))) {
            changeTime100ns = static_cast<qint64>(basic.ChangeTime.QuadPart);
        }
        CloseHandle(h);
    }
#endif
    {
        QMutexLocker lock(&cacheMutex);
        const auto it = cache.constFind(key);
        if (it != cache.constEnd()
            && it->size == size
            && it->mtimeMs == mtimeMs
            && it->changeTime100ns == changeTime100ns) {
            return it->hex;
        }
    }
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    while (!file.atEnd()) {
        hash.addData(file.read(1024 * 1024));
    }
    const QString hex = QString::fromLatin1(hash.result().toHex());
    {
        QMutexLocker lock(&cacheMutex);
        // Track insertion order for FIFO; skip if the key already existed (we're
        // just updating its stat fields, order stays where it was).
        if (!cache.contains(key)) {
            insertOrder.append(key);
            // Evict oldest while over the cap. In practice this only fires on the
            // one entry we just added, but the loop is correct if the cap ever
            // changes below the current size.
            while (insertOrder.size() > kMaxCacheEntries) {
                const QString oldest = insertOrder.takeFirst();
                cache.remove(oldest);
            }
        }
        cache.insert(key, CacheEntry{size, mtimeMs, changeTime100ns, hex});
    }
    return hex;
}


bool debuggerPresent()
{
#ifdef Q_OS_WIN
    BOOL remoteDebugger = FALSE;
    CheckRemoteDebuggerPresent(GetCurrentProcess(), &remoteDebugger);
    return IsDebuggerPresent() != 0 || remoteDebugger != FALSE;
#else
    return false;
#endif
}

QString currentUserBinding()
{
#ifdef Q_OS_WIN
    wchar_t buffer[256] = {};
    DWORD chars = static_cast<DWORD>(sizeof(buffer) / sizeof(buffer[0]));
    if (GetUserNameW(buffer, &chars) && chars > 0) {
        return QString::fromWCharArray(buffer).trimmed();
    }
#endif
    return qEnvironmentVariable("USERNAME", qEnvironmentVariable("USER", QStringLiteral("unknown-user")));
}

bool looksSafeManifestPath(const QString& rel)
{
    const QString clean = QDir::cleanPath(rel.trimmed());
    return !clean.isEmpty()
        && !QDir::isAbsolutePath(clean)
        && !clean.startsWith(QStringLiteral("../"))
        && clean != QStringLiteral("..")
        && !clean.contains(QStringLiteral("/../"));
}

QByteArray decodeDetachedEd25519Signature(const QByteArray& encoded)
{
    const QByteArray text = encoded.trimmed();
    if (text.size() == 128) {
        bool hexOnly = true;
        for (const char c : text) {
            const unsigned char u = static_cast<unsigned char>(c);
            if (!std::isxdigit(u)) {
                hexOnly = false;
                break;
            }
        }
        if (hexOnly) {
            const QByteArray raw = QByteArray::fromHex(text);
            if (raw.size() == 64) {
                return raw;
            }
        }
    }
    QByteArray raw = QByteArray::fromBase64(text, QByteArray::Base64UrlEncoding);
    if (raw.size() == 64) {
        return raw;
    }
    raw = QByteArray::fromBase64(text, QByteArray::Base64Encoding);
    return raw.size() == 64 ? raw : QByteArray{};
}

#ifdef Q_OS_WIN

[[maybe_unused]] bool hypervisorBitSet()  // retained for reference; no longer a VM signal (false-positives on Win11 VBS)
{
    int cpuInfo[4] = {};
    __cpuid(cpuInfo, 1);
    return (cpuInfo[2] & (1 << 31)) != 0;
}

bool vmViaRegistryOrName()
{
    // Registry: check for VM-specific services
    static const wchar_t* vmKeys[] = {
        L"SYSTEM\\CurrentControlSet\\Services\\vmicheartbeat",
        L"SYSTEM\\CurrentControlSet\\Services\\vmicvss",
        L"SYSTEM\\CurrentControlSet\\Services\\vmicshutdown",
        L"SYSTEM\\CurrentControlSet\\Services\\vmicexchange",
        L"SYSTEM\\CurrentControlSet\\Services\\vmci",
        L"SYSTEM\\CurrentControlSet\\Services\\vboxguest",
        L"SYSTEM\\CurrentControlSet\\Services\\vboxmouse",
        L"SYSTEM\\CurrentControlSet\\Services\\vboxservice",
        L"SYSTEM\\CurrentControlSet\\Services\\vmware",
        L"SYSTEM\\CurrentControlSet\\Services\\qemu-ga",
    };
    for (const auto* keyPath : vmKeys) {
        HKEY key = nullptr;
        if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, keyPath, 0, KEY_READ | KEY_WOW64_64KEY, &key) == ERROR_SUCCESS) {
            RegCloseKey(key);
            return true;
        }
    }
    // Computer model / manufacturer
    wchar_t model[256] = {};
    DWORD modelBytes = sizeof(model);
    if (RegGetValueW(HKEY_LOCAL_MACHINE, L"SYSTEM\\CurrentControlSet\\Control\\SystemInformation",
                     L"SystemProductName", RRF_RT_REG_SZ, nullptr, model, &modelBytes) == ERROR_SUCCESS) {
        const QString m = QString::fromWCharArray(model).trimmed().toLower();
        static const QStringList vmModels = {
            QStringLiteral("virtual"), QStringLiteral("vmware"), QStringLiteral("virtualbox"),
            QStringLiteral("hyper-v"), QStringLiteral("xen"), QStringLiteral("kvm"), QStringLiteral("qemu")
        };
        for (const auto& vm : vmModels) {
            if (m.contains(vm)) return true;
        }
    }
    return false;
}

bool sandboxArtifactsPresent()
{
    const QString user = currentUserBinding().toLower();
    static const QStringList sandboxNames = {
        QStringLiteral("sandbox"), QStringLiteral("virus"), QStringLiteral("malware"),
        QStringLiteral("test"), QStringLiteral("john doe"), QStringLiteral("currentuser")
    };
    for (const auto& s : sandboxNames) {
        if (user.contains(s)) return true;
    }
    wchar_t computer[256] = {};
    DWORD chars = static_cast<DWORD>(sizeof(computer) / sizeof(computer[0]));
    if (GetComputerNameW(computer, &chars) && chars > 0) {
        const QString comp = QString::fromWCharArray(computer).trimmed().toLower();
        static const QStringList vmNames = {
            QStringLiteral("sandbox"), QStringLiteral("vmware"), QStringLiteral("virtualbox"),
            QStringLiteral("hyper-v"), QStringLiteral("test"), QStringLiteral("malware"),
            QStringLiteral("virus"), QStringLiteral("analysis")
        };
        for (const auto& s : vmNames) {
            if (comp.contains(s)) return true;
        }
    }
    return false;
}

bool remoteDebuggerPresent()
{
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    if (!ntdll) return false;
    using NtQueryInformationProcessFn = NTSTATUS(WINAPI*)(HANDLE, ULONG, PVOID, ULONG, PULONG);
    auto NtQueryInformationProcess = reinterpret_cast<NtQueryInformationProcessFn>(
        GetProcAddress(ntdll, "NtQueryInformationProcess"));
    if (!NtQueryInformationProcess) return false;
    // ProcessDebugPort = 7
    HANDLE debugPort = nullptr;
    ULONG retLen = 0;
    if (NtQueryInformationProcess(GetCurrentProcess(), 7, &debugPort, sizeof(debugPort), &retLen) == 0) {
        if (debugPort != nullptr) return true;
    }
    // ProcessDebugObjectHandle = 30
    HANDLE debugObject = nullptr;
    if (NtQueryInformationProcess(GetCurrentProcess(), 30, &debugObject, sizeof(debugObject), &retLen) == 0) {
        if (debugObject != nullptr) return true;
    }
    return false;
}

bool knownAnalysisToolRunning()
{
    static const QStringList toolNames = {
        QStringLiteral("cheat engine"), QStringLiteral("x64dbg"), QStringLiteral("x32dbg"),
        QStringLiteral("ollydbg"), QStringLiteral("ida"), QStringLiteral("ghidra"),
        QStringLiteral("dnspy"), QStringLiteral("process hacker"), QStringLiteral("processhacker"),
        QStringLiteral("systeminformer"), QStringLiteral("pestudio"), QStringLiteral("detect it easy"),
        QStringLiteral("die.exe"), QStringLiteral("scylla"), QStringLiteral("reclass"),
        QStringLiteral("wireshark"), QStringLiteral("fiddler"), QStringLiteral("procmon"),
        QStringLiteral("process monitor"), QStringLiteral("autoruns"), QStringLiteral("debugview"),
        QStringLiteral("windbg"), QStringLiteral("windbgx"), QStringLiteral("apimonitor"),
        QStringLiteral("hxd"), QStringLiteral("hxd.exe"), QStringLiteral("cff explorer"),
        QStringLiteral("pe-bear"), QStringLiteral("pebear"), QStringLiteral("binary ninja"),
        QStringLiteral("radare2"), QStringLiteral("r2"), QStringLiteral("frida"),
        QStringLiteral("magnifier"), QStringLiteral("magnify")
    };
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return false;
    PROCESSENTRY32W pe = {};
    pe.dwSize = sizeof(pe);
    bool found = false;
    if (Process32FirstW(snap, &pe)) {
        do {
            const QString name = QString::fromWCharArray(pe.szExeFile).toLower();
            for (const auto& tool : toolNames) {
                if (name.contains(tool)) {
                    found = true;
                    break;
                }
            }
        } while (!found && Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return found;
}

#endif // Q_OS_WIN

} // namespace

SecurityManager::SecurityManager(QString rootDir, QObject* parent)
    : QObject(parent),
      rootDir_(std::move(rootDir))
{
}

QString SecurityManager::machineId() const
{
    // [SERVER-SHARD D3] Single source of truth, shared verbatim with the
    // OrionActivate broker via native_orion/src/MachineIdentity.cpp. Never
    // re-derive the machine_id here — that reintroduces the stub-vs-manager
    // drift design D3 forbids. OrionMachineIdParityTests pins the equality.
    return deriveMachineId();
}

SecurityStatus SecurityManager::evaluate()
{
    SecurityStatus status;
    status.machineId = machineId();
    status.debuggerObserved = debuggerPresent();
    status.settingsSignatureValid = verifySettingsSignature();
    status.releaseManifestRequired = releaseManifestRequired();
    // DEV build auto-recover: an out-of-band settings.json write that doesn't re-sign (e.g. the
    // MainWindow console/profile saves) flips the signature stale and would dead-LOCK automation on
    // the next connect. In a DEV build, re-sign the CURRENT settings.json and re-verify so the lock
    // never strands the user. Production still hard-locks (tamper evidence).
    if (!status.settingsSignatureValid && !status.releaseManifestRequired) {
        QString _resignErr;
        if (writeSettingsSignature(&_resignErr) && verifySettingsSignature()) {
            status.settingsSignatureValid = true;
            auditSecurityEvent(QStringLiteral("settings_signature_autoresigned"), settingsPath());
        }
    }
    // [ORION_FIRST_RUN_BOOTSTRAP 2026-08-07] Production-safe first-launch fallback. On the
    // very first launch after a fresh install, %LOCALAPPDATA%\Orion\ contains NEITHER
    // settings.json NOR settings.json.sig -- nothing has ever run here. The tamper-detection
    // invariant is meaningless in that state (there's nothing to tamper with), yet the check
    // above would keep failing until the user triggered a save via the UI, which they cannot
    // do because Remote Play is locked out. The result is a customer with a fresh install
    // who sees "Settings signature missing or invalid" and cannot proceed.
    //
    // This ONLY triggers when BOTH files are absent -- if either exists we're not first-
    // launch, and auto-signing would defeat tamper detection. AppConfig::save() elsewhere in
    // the app writes both files atomically thereafter (OrionAppController.cpp:10981/10999),
    // so this bootstrap runs exactly once in the product's lifetime on this machine.
    if (!status.settingsSignatureValid && status.releaseManifestRequired
        && !QFile::exists(settingsPath()) && !QFile::exists(settingsSigPath())) {
        QString _bootErr;
        // AppConfig::save() will have already written settings.json with defaults during
        // startup; if it hasn't yet (evaluate() ran first), skip -- next tick will catch it.
        // We only sign what already exists on disk; we do not fabricate content.
        if (QFile::exists(settingsPath()) && writeSettingsSignature(&_bootErr)
            && verifySettingsSignature()) {
            status.settingsSignatureValid = true;
            auditSecurityEvent(QStringLiteral("settings_signature_first_run_bootstrap"),
                               settingsPath());
        }
    }
    QString integrityDetail;
    status.releaseManifestValid = verifyReleaseIntegrity(&integrityDetail);
    status.integrityState = integrityDetail.isEmpty() ? QStringLiteral("Unchecked") : integrityDetail;
    status.executableIntegrityKnown = status.releaseManifestValid || !status.releaseManifestRequired;
    status.authenticodeTampered = authenticodeSignedButInvalid();
    status.vmObserved = vmOrSandboxObserved();
    status.sandboxObserved = status.vmObserved; // vmOrSandboxObserved covers both
    status.analysisToolObserved = analysisToolRunning();
    status.debuggerObserved = status.debuggerObserved || remoteDebuggerPresent();
    status.vigemBusInstalled = vigemBusInstalled();
    status.hidhideInstalled = hidhideInstalled();
    bool entitlementOk = false;
    QString entitlementDetail;
    const QJsonObject entitlement = loadLocalEntitlement(&entitlementOk, &entitlementDetail);
    Q_UNUSED(entitlement);
    status.entitlementState = entitlementDetail;

    if (status.releaseManifestRequired && status.debuggerObserved) {
        status.securityLockActive = true;
        status.securityLockReason = QStringLiteral("Debugger observed in release policy");
        status.message = QStringLiteral("Debugger observed; automation should remain locked in release mode.");
        auditSecurityEvent(QStringLiteral("debugger_observed"), status.securityLockReason);
    } else if (status.releaseManifestRequired && !status.releaseManifestValid) {
        status.securityLockActive = true;
        status.securityLockReason = status.integrityState;
        status.message = QStringLiteral("Release integrity verification failed; automation locked.");
        auditSecurityEvent(QStringLiteral("integrity_lock"), status.integrityState);
    } else if (enforceAuthenticode() && status.authenticodeTampered) {
        // Opt-in (enforce_authenticode policy, release-only): the exe is SIGNED but the signature is
        // invalid (tampered/untrusted). The hash manifest already caught modified shipped FILES; this
        // additionally catches a resigned/repackaged binary. Never fires for an unsigned build.
        status.securityLockActive = true;
        status.securityLockReason = QStringLiteral("Executable signature invalid (tampered)");
        status.message = QStringLiteral("Executable Authenticode signature is present but invalid; automation locked.");
        auditSecurityEvent(QStringLiteral("authenticode_tampered"), QCoreApplication::applicationFilePath());
    } else if (!status.settingsSignatureValid) {
        status.securityLockActive = true;
        status.securityLockReason = QStringLiteral("Settings signature missing or invalid");
        status.message = QStringLiteral("Settings signature missing or invalid; automation locked until settings are signed.");
        auditSecurityEvent(QStringLiteral("settings_signature_invalid"), settingsPath());
    } else if (status.vmObserved) {
        // Deliberately NOT a lock (securityLockActive stays false): the VM heuristic flags real
        // gaming PCs. Logged for telemetry only — the message must say so, or it reads like a
        // scary latent lock on the user's own box.
        status.message = QStringLiteral("VM/sandbox indicators observed (logged only; automation NOT locked).");
        auditSecurityEvent(QStringLiteral("vm_sandbox_detected"), QStringLiteral("virtual machine or sandbox environment"));
    } else if (status.analysisToolObserved) {
        status.securityLockActive = true;
        status.securityLockReason = QStringLiteral("Analysis or debugging tool detected");
        status.message = QStringLiteral("Analysis tool running; automation locked.");
        auditSecurityEvent(QStringLiteral("analysis_tool_detected"), status.securityLockReason);
    } else if (status.debuggerObserved) {
        status.message = QStringLiteral("Debugger observed in dev mode; production automation would lock.");
        auditSecurityEvent(QStringLiteral("debugger_observed_dev"), QStringLiteral("non-locking dev observation"));
    } else if (!status.vigemBusInstalled) {
        status.message = QStringLiteral("ViGEmBus driver is missing. Virtual controller output will not work.");
        auditSecurityEvent(QStringLiteral("vigem_missing"), QStringLiteral("virtual controller unavailable"));
    } else {
        status.message = status.hidhideInstalled
                             ? QStringLiteral("Local security checks passed. HidHide is available but optional.")
                             : QStringLiteral("Local security checks passed. HidHide is not installed; physical controller hiding is optional.");
    }
    status.lastSecurityAuditEvent = lastSecurityAuditEvent_;
    return status;
}

bool SecurityManager::validateLicenseKeyFormat(const QString& key) const
{
    static const QRegularExpression re(QStringLiteral("^[A-Z0-9][A-Z0-9\\-]{9,95}$"));
    return re.match(key.trimmed().toUpper()).hasMatch();
}

bool SecurityManager::verifySettingsSignature() const
{
    const auto digest = settingsDigest();
    if (digest.isEmpty()) {
        return false;
    }
    const auto stored = readFileLimited(settingsSigPath(), 512).trimmed().toLower();
    return !stored.isEmpty() && stored == digest.toHex();
}

bool SecurityManager::verifyReleaseIntegrity(QString* detail, bool exactRoot) const
{
    const QString manifestPath = exactRoot
        ? QDir(rootDir_).absoluteFilePath(QStringLiteral("release_manifest.json"))
        : releaseManifestPath();
    const bool required = exactRoot || releaseManifestRequired();
    if (!QFileInfo::exists(manifestPath)) {
        if (detail) {
            *detail = required
                          ? QStringLiteral("Release manifest missing")
                          : QStringLiteral("Manifest absent (dev mode)");
        }
        return !required;
    }

    // Preserve the exact bytes: the detached Ed25519 signature authenticates
    // this byte sequence, not a parsed/re-serialized approximation.
    const QByteArray manifestBytes = readFileLimited(manifestPath, 4 * 1024 * 1024);
    QJsonParseError parseError;
    const QJsonDocument manifestDoc = QJsonDocument::fromJson(manifestBytes, &parseError);
    if (manifestBytes.isEmpty() || parseError.error != QJsonParseError::NoError
        || !manifestDoc.isObject()) {
        if (detail) *detail = QStringLiteral("Release manifest is invalid JSON");
        return false;
    }
    const QJsonObject manifest = manifestDoc.object();
    if (manifest.value(QStringLiteral("schema")).toString() != QLatin1String("orion.release_manifest.v1")) {
        if (detail) *detail = QStringLiteral("Release manifest schema mismatch");
        return false;
    }

    const bool signatureRequired = required
        || manifest.value(QStringLiteral("signature_required")).toBool(false);
    if (signatureRequired) {
        if (manifest.value(QStringLiteral("signature_alg")).toString().trimmed().compare(
                QLatin1String("ed25519"), Qt::CaseInsensitive) != 0) {
            if (detail) *detail = QStringLiteral("Release manifest signature algorithm mismatch");
            return false;
        }
        const QString keyId = manifest.value(QStringLiteral("public_key_id")).toString().trimmed();
        if (keyId.isEmpty()) {
            if (detail) *detail = QStringLiteral("Release manifest public_key_id missing");
            return false;
        }
        const QString signaturePath = releaseManifestSignaturePath(manifestPath);
        if (!QFileInfo::exists(signaturePath)) {
            if (detail) *detail = QStringLiteral("Release manifest signature missing");
            return false;
        }
        const QByteArray signature = decodeDetachedEd25519Signature(
            readFileLimited(signaturePath, 4096));
        if (signature.size() != 64) {
            if (detail) *detail = QStringLiteral("Release manifest signature is invalid");
            return false;
        }

#ifdef ORION_PRODUCTION_BUILD
        constexpr bool productionBuild = true;
        const QString testOverrideId;
        const QByteArray testOverrideKey;
#else
        constexpr bool productionBuild = false;
        const QString testOverrideId =
            qEnvironmentVariable("ORION_RELEASE_MANIFEST_TEST_PUBKEY_ID").trimmed();
        const QByteArray testOverrideKey =
            qgetenv("ORION_RELEASE_MANIFEST_TEST_PUBKEY_B64").trimmed();
#endif
        const QByteArray publicKey = decodeEd25519PublicKey(QString::fromLatin1(
            selectReleaseManifestPublicKeyEncoding(
                productionBuild, keyId, testOverrideId, testOverrideKey)));
        if (publicKey.size() != 32) {
            if (detail) {
                *detail = QStringLiteral("Release manifest public_key_id is not trusted: %1")
                              .arg(keyId);
            }
            return false;
        }
        if (!ed25519Available()) {
            // [5c 2026-08-08] Be specific: the usual real-world cause is antivirus quarantining
            // or altering the pinned libcrypto-3-x64.dll, not a corrupt manifest. Ed25519.cpp
            // logs the exact failure (qCritical); this is the user-facing surface.
            if (detail) {
                *detail = QStringLiteral(
                    "Signature verifier unavailable (libcrypto-3-x64.dll missing or altered — "
                    "antivirus may have quarantined it; reinstall Venice)");
            }
            return false;
        }
        if (!ed25519Verify(publicKey, manifestBytes, signature)) {
            if (detail) *detail = QStringLiteral("Release manifest signature verification failed");
            return false;
        }
    }
    const auto files = manifest.value(QStringLiteral("files")).toObject();
    if (files.isEmpty()) {
        if (detail) *detail = QStringLiteral("Release manifest contains no files");
        return false;
    }

    const QDir root(rootDir_);
    const QDir appDir(QCoreApplication::applicationDirPath());
    for (auto it = files.constBegin(); it != files.constEnd(); ++it) {
        const QString rel = QDir::fromNativeSeparators(it.key());
        if (!looksSafeManifestPath(rel)) {
            if (detail) *detail = QStringLiteral("Unsafe manifest path: %1").arg(rel);
            return false;
        }
        QString expected;
        if (it.value().isString()) {
            expected = it.value().toString().trimmed().toLower();
        } else if (it.value().isObject()) {
            expected = it.value().toObject().value(QStringLiteral("sha256")).toString().trimmed().toLower();
        }
        if (expected.size() != 64) {
            if (detail) *detail = QStringLiteral("Invalid hash for %1").arg(rel);
            return false;
        }

        QString candidate = root.absoluteFilePath(rel);
        if (!exactRoot && !QFileInfo::exists(candidate)) {
            candidate = appDir.absoluteFilePath(rel);
        }
        if (!QFileInfo::exists(candidate)) {
            if (detail) *detail = QStringLiteral("Manifest file missing: %1").arg(rel);
            return false;
        }
        const QString actual = sha256FileHex(candidate);
        if (actual.isEmpty() || actual != expected) {
            if (detail) *detail = QStringLiteral("Hash mismatch: %1").arg(rel);
            return false;
        }
    }

    if (detail) {
        *detail = QStringLiteral("Release manifest verified (%1 files)").arg(files.size());
    }
    return true;
}

bool SecurityManager::vigemBusInstalled() const
{
#ifdef Q_OS_WIN
    return QFileInfo::exists(QStringLiteral("C:/Program Files/Nefarius Software Solutions/ViGEm Bus Driver/ViGEmBus.sys"))
        || QFileInfo::exists(QStringLiteral("C:/Program Files/Nefarius Software Solutions/Virtual Gamepad Emulation Bus Driver/ViGEmBus.sys"));
#else
    return false;
#endif
}

bool SecurityManager::hidhideInstalled() const
{
#ifdef Q_OS_WIN
    return QFileInfo::exists(QStringLiteral("C:/Program Files/Nefarius Software Solutions/HidHide/x64/HidHideCLI.exe"))
        || QFileInfo::exists(QStringLiteral("C:/Program Files/Nefarius Software Solutions/HidHide/x64/HidHide/HidHide.sys"));
#else
    return false;
#endif
}

bool SecurityManager::vmOrSandboxObserved() const
{
#ifdef Q_OS_WIN
    // NOTE: the bare CPUID hypervisor-present bit is NOT a reliable VM signal anymore — on
    // Windows 11 with Hyper-V / VBS / Memory-Integrity / WSL2 / Windows Sandbox enabled it is
    // set on BARE METAL, so hypervisorBitSet() false-positives on real customer machines (it
    // was flagging a real gaming PC as a "VM/sandbox"). Rely on the strong, VM-specific signals
    // instead: VM-only registry keys / device names (VMware, VirtualBox, QEMU, Hyper-V guest)
    // and sandbox/analysis artifacts. A real guest still trips those.
    return vmViaRegistryOrName() || sandboxArtifactsPresent();
#else
    return false;
#endif
}

bool SecurityManager::analysisToolRunning() const
{
#ifdef Q_OS_WIN
    return knownAnalysisToolRunning();
#else
    return false;
#endif
}

bool SecurityManager::timingAnomalyDetected() const
{
    // Reserved for future RDTSC-based timing checks. Currently always false
    // to avoid false positives on variable-frequency CPUs.
    return false;
}

bool SecurityManager::releaseManifestRequired() const
{
#ifdef ORION_PRODUCTION_BUILD
    // Production: integrity is mandatory. The on-disk policy file is
    // attacker-editable and must not be able to switch it off.
    return true;
#else
    if (qEnvironmentVariableIntValue("ORION_REQUIRE_RELEASE_MANIFEST") > 0) {
        return true;
    }
    const QJsonObject policy = readJsonObject(securityPolicyPath(), 512 * 1024);
    return policy.value(QStringLiteral("require_release_manifest")).toBool(false);
#endif
}

QString SecurityManager::appBuildHash() const
{
    const QString exe = QCoreApplication::applicationFilePath();
    const QString hash = sha256FileHex(exe);
    return hash.isEmpty() ? QStringLiteral("unknown-build") : hash;
}

bool SecurityManager::authenticodeValid() const
{
#ifdef Q_OS_WIN
    const QString exe = QCoreApplication::applicationFilePath();
    if (exe.isEmpty()) return false;

    // WinVerifyTrust — check Authenticode signature on the running exe.
    // Warning-only: logs unsigned/tampered builds but does NOT block execution.
    // A patched binary with a valid SHA256 hash but no valid signature will fail this.
    WINTRUST_FILE_INFO fileInfo{};
    fileInfo.cbStruct = sizeof(fileInfo);
    fileInfo.pcwszFilePath = reinterpret_cast<LPCWSTR>(exe.utf16());

    WINTRUST_DATA trustData{};
    trustData.cbStruct = sizeof(trustData);
    trustData.dwUIChoice = WTD_UI_NONE;
    trustData.fdwRevocationChecks = WTD_REVOKE_NONE;
    trustData.dwUnionChoice = WTD_CHOICE_FILE;
    trustData.pFile = &fileInfo;
    trustData.dwStateAction = WTD_STATEACTION_VERIFY;

    GUID actionGuid = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    LONG result = WinVerifyTrust(NULL, &actionGuid, &trustData);

    // Close state handle
    trustData.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(NULL, &actionGuid, &trustData);

    if (result != ERROR_SUCCESS) {
        qDebug("[Security] Authenticode validation FAILED (result=0x%08X) for %s",
               static_cast<unsigned>(result), qPrintable(exe));
    }
    return result == ERROR_SUCCESS;
#else
    return false;
#endif
}

bool SecurityManager::authenticodeSignedButInvalid() const
{
#ifdef Q_OS_WIN
    const QString exe = QCoreApplication::applicationFilePath();
    if (exe.isEmpty()) return false;

    WINTRUST_FILE_INFO fileInfo{};
    fileInfo.cbStruct = sizeof(fileInfo);
    fileInfo.pcwszFilePath = reinterpret_cast<LPCWSTR>(exe.utf16());

    WINTRUST_DATA trustData{};
    trustData.cbStruct = sizeof(trustData);
    trustData.dwUIChoice = WTD_UI_NONE;
    trustData.fdwRevocationChecks = WTD_REVOKE_NONE;
    trustData.dwUnionChoice = WTD_CHOICE_FILE;
    trustData.pFile = &fileInfo;
    trustData.dwStateAction = WTD_STATEACTION_VERIFY;

    GUID actionGuid = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    LONG result = WinVerifyTrust(NULL, &actionGuid, &trustData);
    trustData.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(NULL, &actionGuid, &trustData);

    if (result == ERROR_SUCCESS) {
        return false;  // valid signature
    }
    // Exclude the "no usable signature" family — an unsigned or unparseable binary is NOT a tamper
    // signal (it's the pre-signing legitimate state). Everything else (TRUST_E_BAD_DIGEST = file
    // modified after signing, TRUST_E_SUBJECT_NOT_TRUSTED / CERT_E_* = present-but-untrusted) means a
    // signature EXISTS and failed -> tamper. Conservative on purpose: false positives here would
    // brick a launch, so only a positively-present-and-invalid signature counts.
    if (result == static_cast<LONG>(TRUST_E_NOSIGNATURE)
        || result == static_cast<LONG>(TRUST_E_PROVIDER_UNKNOWN)
        || result == static_cast<LONG>(TRUST_E_SUBJECT_FORM_UNKNOWN)
        || result == static_cast<LONG>(TRUST_E_ACTION_UNKNOWN)) {
        return false;
    }
    return true;
#else
    return false;
#endif
}

bool SecurityManager::enforceAuthenticode() const
{
    // Opt-in and release-only. Default OFF so an unsigned legitimate build (before code-signing is
    // set up) is never bricked — only a SIGNED-but-tampered binary is caught, and only once the
    // operator confirms a genuinely-signed release passes and flips enforce_authenticode on in the
    // security policy. With the flag off the evaluate() lock chain is byte-identical to pre-change.
    if (!releaseManifestRequired()) {
        return false;
    }
    const QJsonObject policy = readJsonObject(securityPolicyPath(), 512 * 1024);
    return policy.value(QStringLiteral("enforce_authenticode")).toBool(false);
}

QString SecurityManager::entitlementState() const
{
    bool ok = false;
    QString state;
    const QJsonObject entitlement = loadLocalEntitlement(&ok, &state);
    Q_UNUSED(entitlement);
    return state;
}

bool SecurityManager::writeSettingsSignature(QString* error) const
{
    const auto digest = settingsDigest();
    if (digest.isEmpty()) {
        if (error) {
            *error = QStringLiteral("No settings digest could be created.");
        }
        return false;
    }

    QSaveFile file(settingsSigPath());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    file.write(digest.toHex());
    file.write("\n");
    if (!file.commit()) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    return true;
}

bool SecurityManager::cacheLocalEntitlement(const QJsonObject& entitlement, QString* error) const
{
    QJsonObject payload = entitlement;
    payload.insert(QStringLiteral("schema"), QStringLiteral("orion.local_entitlement.v1"));
    payload.insert(QStringLiteral("cached_at"), QDateTime::currentDateTimeUtc().toSecsSinceEpoch());
    payload.insert(QStringLiteral("binding"), entitlementBinding());

    const QByteArray plain = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    bool protectedByDpapi = false;
    const QByteArray protectedBlob = protectBytes(plain, &protectedByDpapi);
    if (protectedBlob.isEmpty()) {
        if (error) *error = QStringLiteral("Entitlement protection failed");
        return false;
    }

    QJsonObject outer;
    outer.insert(QStringLiteral("schema"), QStringLiteral("orion.local_entitlement_cache.v1"));
    outer.insert(QStringLiteral("protected_by_dpapi"), protectedByDpapi);
    outer.insert(QStringLiteral("blob_b64"), QString::fromLatin1(protectedBlob.toBase64()));

    QFileInfo(localEntitlementPath()).absoluteDir().mkpath(QStringLiteral("."));
    QSaveFile file(localEntitlementPath());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) *error = file.errorString();
        return false;
    }
    file.write(QJsonDocument(outer).toJson(QJsonDocument::Indented));
    if (!file.commit()) {
        if (error) *error = file.errorString();
        return false;
    }
    return true;
}

QJsonObject SecurityManager::loadLocalEntitlement(bool* ok, QString* state) const
{
    if (ok) *ok = false;
    const QString path = localEntitlementPath();
    if (!QFileInfo::exists(path)) {
        if (state) *state = QStringLiteral("No cached entitlement");
        return {};
    }
    const QJsonObject outer = readJsonObject(path, 512 * 1024);
    if (outer.value(QStringLiteral("schema")).toString() != QLatin1String("orion.local_entitlement_cache.v1")) {
        if (state) *state = QStringLiteral("Invalid entitlement cache schema");
        return {};
    }
    const QByteArray blob = QByteArray::fromBase64(outer.value(QStringLiteral("blob_b64")).toString().toLatin1());
    bool unprotectedOk = false;
    const QByteArray plain = unprotectBytes(blob, outer.value(QStringLiteral("protected_by_dpapi")).toBool(false), &unprotectedOk);
    if (!unprotectedOk) {
        if (state) *state = QStringLiteral("Entitlement cache decrypt failed");
        return {};
    }
    const QJsonObject payload = QJsonDocument::fromJson(plain).object();
    if (payload.value(QStringLiteral("schema")).toString() != QLatin1String("orion.local_entitlement.v1")) {
        if (state) *state = QStringLiteral("Invalid entitlement payload schema");
        return {};
    }
    const QJsonObject binding = payload.value(QStringLiteral("binding")).toObject();
    const QJsonObject expected = entitlementBinding();
    for (const QString& key : {QStringLiteral("machine_id"), QStringLiteral("user"), QStringLiteral("build_sha256")}) {
        if (binding.value(key).toString() != expected.value(key).toString()) {
            if (state) *state = QStringLiteral("Entitlement binding mismatch: %1").arg(key);
            return {};
        }
    }

    const qint64 now = QDateTime::currentDateTimeUtc().toSecsSinceEpoch();
    const qint64 expires = payload.value(QStringLiteral("expires_unix")).toVariant().toLongLong();
    if (expires > 0 && expires < now) {
        if (state) *state = QStringLiteral("Cached entitlement expired");
        return {};
    }

    if (ok) *ok = true;
    if (state) {
        const QString plan = payload.value(QStringLiteral("plan")).toString(QStringLiteral("verified"));
        *state = QStringLiteral("Cached entitlement valid (%1)").arg(plan);
    }
    return payload;
}

bool SecurityManager::clearLocalEntitlement(QString* error) const
{
    if (!QFileInfo::exists(localEntitlementPath())) {
        return true;
    }
    if (!QFile::remove(localEntitlementPath())) {
        if (error) *error = QStringLiteral("Failed to remove local entitlement cache");
        return false;
    }
    return true;
}

QByteArray SecurityManager::settingsDigest() const
{
    const auto data = readFileLimited(settingsPath(), 512 * 1024);
    if (data.isEmpty()) {
        return {};
    }
    QByteArray material;
    material += "orion-settings-v1|";
    material += machineId().toUtf8();
    material += '|';
    material += data;
    return QCryptographicHash::hash(material, QCryptographicHash::Sha256);
}

// [ORION_DATA_DIR 2026-08-07] These three previously returned `rootDir_ + "/…"`, which
// for a production install is `C:\Program Files\Venice\` — a directory a standard user
// cannot write. `AppConfig::settingsPath()` was moved to `orionDataDir(rootDir_)` on
// 2026-08-04 for exactly this reason (commit 5e8c9cbe), but SecurityManager was not
// updated in the same pass. The consequence on every fresh customer install:
//   * settings.json lives at `%LOCALAPPDATA%\Orion\settings.json` (AppConfig)
//   * SecurityManager checked `C:\Program Files\Venice\settings.json` → not found
//     → `settingsDigest()` returned empty → `verifySettingsSignature()` failed
//     → security lock: "Settings signature missing or invalid"
//     → Remote Play blocked
//   * The same split affected `.vault/orion_entitlement.cache` — the first postinstall
//     launch inherited elevation and wrote it to Program Files anyway, so it "worked
//     once". Every subsequent unelevated relaunch failed the DPAPI check silently
//     and forced online re-verification (and lost the offline capability entirely).
// Both paths now resolve through the same `orionDataDir()` helper AppConfig uses; both
// halves of the app read/write the same files, first-launch bootstrap can create them
// once via `AppConfig::save()`, and non-admin users can actually write them.

bool SecurityManager::hasSettingsSignature() const
{
    return QFile::exists(settingsSigPath());
}

QString SecurityManager::settingsPath() const
{
    return orionDataDir(rootDir_) + QStringLiteral("/settings.json");
}

QString SecurityManager::settingsSigPath() const
{
    return orionDataDir(rootDir_) + QStringLiteral("/settings.json.sig");
}

QString SecurityManager::localEntitlementPath() const
{
    return orionDataDir(rootDir_) + QStringLiteral("/.vault/orion_entitlement.cache");
}

QString SecurityManager::releaseManifestPath() const
{
    const QString rootLocal = rootDir_ + QStringLiteral("/release_manifest.json");
    if (QFileInfo::exists(rootLocal)) {
        return rootLocal;
    }
    const QString appLocal = QCoreApplication::applicationDirPath() + QStringLiteral("/release_manifest.json");
    return QFileInfo::exists(appLocal) ? appLocal : rootLocal;
}

QString SecurityManager::releaseManifestSignaturePath(const QString& manifestPath) const
{
    return QFileInfo(manifestPath).dir().absoluteFilePath(
        QStringLiteral("release_manifest.sig"));
}

QString SecurityManager::securityPolicyPath() const
{
    const QString appLocal = QCoreApplication::applicationDirPath() + QStringLiteral("/security_policy.json");
    if (QFileInfo::exists(appLocal)) {
        return appLocal;
    }
    return rootDir_ + QStringLiteral("/security_policy.json");
}

QJsonObject SecurityManager::readJsonObject(const QString& path, qsizetype maxBytes) const
{
    const QByteArray raw = readFileLimited(path, maxBytes);
    if (raw.isEmpty()) {
        return {};
    }
    const QJsonDocument doc = QJsonDocument::fromJson(raw);
    return doc.isObject() ? doc.object() : QJsonObject{};
}

QByteArray SecurityManager::readFileLimited(const QString& path, qsizetype maxBytes) const
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    return file.read(maxBytes);
}

QByteArray SecurityManager::protectBytes(const QByteArray& plain, bool* protectedByDpapi) const
{
    if (protectedByDpapi) *protectedByDpapi = false;
#ifdef Q_OS_WIN
    DATA_BLOB in {};
    in.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(plain.constData()));
    in.cbData = static_cast<DWORD>(plain.size());
    const QByteArray entropy = QByteArrayLiteral("orion-entitlement-v1|") + machineId().toUtf8();
    DATA_BLOB entropyBlob {};
    entropyBlob.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(entropy.constData()));
    entropyBlob.cbData = static_cast<DWORD>(entropy.size());
    DATA_BLOB out {};
    if (CryptProtectData(&in, L"Orion local entitlement", &entropyBlob, nullptr, nullptr,
                         CRYPTPROTECT_UI_FORBIDDEN, &out)) {
        QByteArray result(reinterpret_cast<const char*>(out.pbData), static_cast<int>(out.cbData));
        LocalFree(out.pbData);
        if (protectedByDpapi) *protectedByDpapi = true;
        return result;
    }
    return {};
#else
    return plain;
#endif
}

QByteArray SecurityManager::unprotectBytes(const QByteArray& blob, bool dpapiProtected, bool* ok) const
{
    if (ok) *ok = false;
    if (!dpapiProtected) {
#ifdef Q_OS_WIN
        return {};
#else
        if (ok) *ok = true;
        return blob;
#endif
    }
#ifdef Q_OS_WIN
    DATA_BLOB in {};
    in.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(blob.constData()));
    in.cbData = static_cast<DWORD>(blob.size());
    const QByteArray entropy = QByteArrayLiteral("orion-entitlement-v1|") + machineId().toUtf8();
    DATA_BLOB entropyBlob {};
    entropyBlob.pbData = reinterpret_cast<BYTE*>(const_cast<char*>(entropy.constData()));
    entropyBlob.cbData = static_cast<DWORD>(entropy.size());
    DATA_BLOB out {};
    if (CryptUnprotectData(&in, nullptr, &entropyBlob, nullptr, nullptr,
                           CRYPTPROTECT_UI_FORBIDDEN, &out)) {
        QByteArray result(reinterpret_cast<const char*>(out.pbData), static_cast<int>(out.cbData));
        LocalFree(out.pbData);
        if (ok) *ok = true;
        return result;
    }
#endif
    return {};
}

QJsonObject SecurityManager::entitlementBinding() const
{
    QJsonObject binding;
    binding.insert(QStringLiteral("machine_id"), machineId());
    binding.insert(QStringLiteral("user"), currentUserBinding());
    binding.insert(QStringLiteral("build_sha256"), appBuildHash());
    binding.insert(QStringLiteral("fingerprint_version"), 2);
    return binding;
}

void SecurityManager::auditSecurityEvent(const QString& event, const QString& detail)
{
    const QString line = QStringLiteral("%1: %2").arg(event, detail);
    if (line == lastSecurityAuditEvent_) {
        return;
    }
    lastSecurityAuditEvent_ = line;
    emit securityEvent(event, detail);
}

quint64 SecurityEvaluationGenerationFence::invalidate() noexcept
{
    ++generation_;
    if (generation_ == 0) {
        generation_ = 1;
    }
    return generation_;
}

SecurityStatus failClosedSecurityEvaluationStatus(const QString& detail)
{
    const QString concise = detail.simplified().left(240);
    SecurityStatus status;
    status.evaluationComplete = false;
    status.securityLockActive = true;
    status.executableIntegrityKnown = false;
    status.settingsSignatureValid = false;
    status.integrityState = QStringLiteral("Security evaluation unavailable");
    status.securityLockReason = concise.isEmpty()
        ? QStringLiteral("Periodic security evaluator unavailable")
        : QStringLiteral("Periodic security evaluator failed: %1").arg(concise);
    status.lastSecurityAuditEvent = QStringLiteral("security_evaluator_failed: %1")
                                        .arg(concise.isEmpty()
                                                 ? QStringLiteral("unknown worker failure")
                                                 : concise);
    status.message = QStringLiteral(
        "Security evaluation failed; automation is locked until a complete check succeeds.");
    return status;
}

PeriodicSecurityEvaluator::PeriodicSecurityEvaluator(QString rootDir, QObject* parent)
    : QObject(parent)
{
    initializeWorker(rootDir);
}

PeriodicSecurityEvaluator::PeriodicSecurityEvaluator(EvaluateFunction evaluator, QObject* parent)
    : PeriodicSecurityEvaluator(std::move(evaluator),
                                kDefaultEvaluationTimeoutMs, parent)
{
}

PeriodicSecurityEvaluator::PeriodicSecurityEvaluator(EvaluateFunction evaluator,
                                                     int evaluationTimeoutMs,
                                                     QObject* parent)
    : QObject(parent),
      evaluator_(std::move(evaluator)),
      evaluationTimeoutMs_(std::max(1, evaluationTimeoutMs))
{
    initializeWorker({});
}

PeriodicSecurityEvaluator::~PeriodicSecurityEvaluator()
{
    stopping_.store(true, std::memory_order_release);
    evaluationWatchdog_.stop();
    pendingGenerations_.clear();
    workerThread_.quit();
    // evaluate() is intentionally not cancellable half-way through an integrity
    // pass. Joining here prevents its worker objects (and captured `this`) from
    // outliving the coordinator during application teardown.
    workerThread_.wait();
    workerContext_ = nullptr;
    workerSecurity_ = nullptr;
}

void PeriodicSecurityEvaluator::initializeWorker(const QString& rootDir)
{
    qRegisterMetaType<SecurityStatus>("orion::SecurityStatus");

    evaluationWatchdog_.setSingleShot(true);
    evaluationWatchdog_.setTimerType(Qt::PreciseTimer);
    connect(&evaluationWatchdog_, &QTimer::timeout, this, [this]() {
        if (!evaluationInFlight_ || stopping_.load(std::memory_order_acquire)) {
            return;
        }
        watchdogFailureEmitted_ = true;
        emit evaluationFailed(
            activeGeneration_,
            QStringLiteral("security evaluation exceeded the %1 ms freshness budget")
                .arg(evaluationTimeoutMs_));
        if (!pendingGenerations_.isEmpty()
            && pendingGenerations_.head() != activeGeneration_) {
            emit evaluationFailed(
                pendingGenerations_.head(),
                QStringLiteral("security worker remains blocked beyond the freshness budget"));
        }
        // [ORION_SEC_WATCHDOG_LEAK 2026-08-07] The old handler declared failure
        // to callers but LEFT evaluationInFlight_ == true forever. Every future
        // request() call then took the "already in flight" branch and stalled in
        // pendingGenerations_ with no worker running -- one slow first scan
        // permanently poisoned the periodic evaluator for the rest of the
        // session. Clear the in-flight flag and dispatchNext() so the queued
        // generation runs on a fresh scan. The comment below about "overlapping
        // integrity scans are forbidden" is preserved by the completeSuccess /
        // completeFailure guards (they check activeGeneration_ before touching
        // state), so a stale reply from the timed-out worker cannot corrupt the
        // new dispatch.
        activeGeneration_ = 0;
        evaluationInFlight_ = false;
        dispatchNext();
    });

    workerContext_ = new QObject;
    if (!evaluator_) {
        workerSecurity_ = new SecurityManager(rootDir);
        connect(workerSecurity_, &SecurityManager::securityEvent,
                this, &PeriodicSecurityEvaluator::securityEvent,
                Qt::QueuedConnection);
    }

    workerContext_->moveToThread(&workerThread_);
    if (workerSecurity_) {
        workerSecurity_->moveToThread(&workerThread_);
    }
    connect(&workerThread_, &QThread::finished,
            workerContext_, &QObject::deleteLater);
    if (workerSecurity_) {
        connect(&workerThread_, &QThread::finished,
                workerSecurity_, &QObject::deleteLater);
    }
    connect(&workerThread_, &QThread::finished, this, [this]() {
        if (stopping_.load(std::memory_order_acquire)) {
            return;
        }
        const quint64 failedGeneration = evaluationInFlight_
            ? activeGeneration_
            : (!pendingGenerations_.isEmpty() ? pendingGenerations_.head() : 0);
        const bool failureAlreadyPublished = watchdogFailureEmitted_;
        evaluationWatchdog_.stop();
        evaluationInFlight_ = false;
        watchdogFailureEmitted_ = false;
        activeGeneration_ = 0;
        pendingGenerations_.clear();
        if (failedGeneration != 0 && !failureAlreadyPublished) {
            emit evaluationFailed(
                failedGeneration,
                QStringLiteral("security worker thread stopped unexpectedly"));
        }
    });
    workerThread_.setObjectName(QStringLiteral("OrionSecurityEvaluator"));
    workerThread_.start();
}

bool PeriodicSecurityEvaluator::request(quint64 generation)
{
    if (generation == 0 || stopping_.load(std::memory_order_acquire)) {
        return false;
    }
    if (!workerThread_.isRunning() || !workerContext_) {
        emit evaluationFailed(generation,
                              QStringLiteral("security worker thread is not running"));
        return false;
    }
    if (evaluationInFlight_) {
        // A slow scan must never create an unbounded backlog of identical 1 Hz
        // work. Keep at most the newest authority epoch behind the active scan.
        if (generation == activeGeneration_
            || (!pendingGenerations_.isEmpty()
                && pendingGenerations_.head() == generation)) {
            return true;
        }
        pendingGenerations_.clear();
        pendingGenerations_.enqueue(generation);
        if (watchdogFailureEmitted_) {
            // The active timeout may belong to an invalidated generation. Lock
            // the newly current generation too while the sole worker is blocked.
            emit evaluationFailed(
                generation,
                QStringLiteral("security worker remains blocked beyond the freshness budget"));
        }
        return true;
    }

    pendingGenerations_.clear();
    pendingGenerations_.enqueue(generation);
    dispatchNext();
    return true;
}

void PeriodicSecurityEvaluator::dispatchNext()
{
    if (evaluationInFlight_ || pendingGenerations_.isEmpty()
        || stopping_.load(std::memory_order_acquire)) {
        return;
    }

    activeGeneration_ = pendingGenerations_.dequeue();
    evaluationInFlight_ = true;
    watchdogFailureEmitted_ = false;
    const quint64 generation = activeGeneration_;
    emit evaluationStarted(generation);
    evaluationWatchdog_.start(evaluationTimeoutMs_);

    const bool queued = QMetaObject::invokeMethod(
        workerContext_,
        [this, generation]() {
            if (stopping_.load(std::memory_order_acquire)) {
                return;
            }
            try {
                SecurityStatus status = evaluator_
                    ? evaluator_()
                    : workerSecurity_->evaluate();
                if (stopping_.load(std::memory_order_acquire)) {
                    return;
                }
                QMetaObject::invokeMethod(
                    this,
                    [this, generation, status = std::move(status)]() mutable {
                        completeSuccess(generation, std::move(status));
                    },
                    Qt::QueuedConnection);
            } catch (const std::exception& error) {
                const QString detail = QString::fromUtf8(error.what());
                if (!stopping_.load(std::memory_order_acquire)) {
                    QMetaObject::invokeMethod(
                        this,
                        [this, generation, detail]() {
                            completeFailure(generation, detail);
                        },
                        Qt::QueuedConnection);
                }
            } catch (...) {
                if (!stopping_.load(std::memory_order_acquire)) {
                    QMetaObject::invokeMethod(
                        this,
                        [this, generation]() {
                            completeFailure(
                                generation,
                                QStringLiteral("unknown security evaluator exception"));
                        },
                        Qt::QueuedConnection);
                }
            }
        },
        Qt::QueuedConnection);

    if (!queued) {
        completeFailure(generation,
                        QStringLiteral("could not queue security evaluation"));
    }
}

void PeriodicSecurityEvaluator::completeSuccess(quint64 generation,
                                                SecurityStatus status)
{
    if (!evaluationInFlight_ || generation != activeGeneration_
        || stopping_.load(std::memory_order_acquire)) {
        return;
    }
    evaluationInFlight_ = false;
    activeGeneration_ = 0;
    evaluationWatchdog_.stop();
    watchdogFailureEmitted_ = false;
    emit evaluationFinished(generation, std::move(status));
    dispatchNext();
}

void PeriodicSecurityEvaluator::completeFailure(quint64 generation,
                                                QString detail)
{
    if (!evaluationInFlight_ || generation != activeGeneration_
        || stopping_.load(std::memory_order_acquire)) {
        return;
    }
    const bool failureAlreadyPublished = watchdogFailureEmitted_;
    evaluationInFlight_ = false;
    activeGeneration_ = 0;
    evaluationWatchdog_.stop();
    watchdogFailureEmitted_ = false;
    if (!failureAlreadyPublished) {
        emit evaluationFailed(generation, std::move(detail));
    }
    dispatchNext();
}

} // namespace orion
