#include "BrokerInstallTrust.h"
#include "NativeCrashDiagnostics.h"
#include "OrionAppController.h"
#include "SidecarReaderProfile.h"

#include <QtCore/QAbstractNativeEventFilter>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QDebug>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSettings>
#include <QtCore/QStandardPaths>
#include <QtCore/QTextStream>
#include <QtCore/QTimer>
#include <QtGui/QGuiApplication>
#include <QtGui/QIcon>
#include <QtGui/QWindow>
#include <QtQml/QQmlApplicationEngine>
#include <QtQml/QQmlContext>
#include <QtQml/QQmlError>
#include <QtQuick/QQuickWindow>
#include <QtQuickControls2/QQuickStyle>
#include <QtCore/QEventLoop>
#include <QtWidgets/QApplication>

#ifdef Q_OS_WIN
#include "DeepLinkTargetPolicy.h"

#include <Windows.h>
#include <dwmapi.h>
#include <windowsx.h>
#include <wincrypt.h>
#include <wintrust.h>
#include <softpub.h>

#pragma comment(lib, "wintrust.lib")
#pragma comment(lib, "crypt32.lib")

namespace {
// Windows 11 corner-round preference enum (lives in dwmapi.h on newer SDKs but we
// declare it manually so the build works on older Windows SDKs too).
#ifndef DWMWA_WINDOW_CORNER_PREFERENCE
#define DWMWA_WINDOW_CORNER_PREFERENCE 33
#endif
constexpr int kDwmWindowCornerRound = 2;       // DWMWCP_ROUND
constexpr int kDwmWindowCornerRoundSmall = 3;  // DWMWCP_ROUNDSMALL

// Native event filter that strips Windows' non-client frame so the QML title
// bar paints flush against the window edge. Without this, WS_THICKFRAME makes
// Windows reserve ~8 px on every edge, which shows up as an empty black strip
// above our custom title bar.
class FramelessNativeFilter : public QAbstractNativeEventFilter
{
public:
    void setShutdownTarget(HWND rootWindow, orion::OrionAppController* controller) noexcept
    {
        shutdownRootWindow_ = rootWindow;
        shutdownController_ = controller;
    }

    bool nativeEventFilter(const QByteArray& eventType, void* message, qintptr* result) override
    {
        if (eventType != "windows_generic_MSG") {
            return false;
        }
        MSG* msg = static_cast<MSG*>(message);
        if (!msg) {
            return false;
        }

        // Observe WM_CLOSE for the exact launcher HWND before Qt consumes it.
        // A frameless QQuickWindow can otherwise disappear while a hidden
        // popup/tool window keeps QApplication's event loop alive.
        if (msg->message == WM_CLOSE
            && shutdownController_
            && shutdownRootWindow_
            && msg->hwnd == shutdownRootWindow_) {
            shutdownController_->requestApplicationShutdown();
        }

        if (msg->message == WM_NCCALCSIZE && msg->wParam == TRUE) {
            // Returning 0 with wParam=TRUE means "client area = entire window".
            // No non-client area is reserved → the QML chrome paints edge-to-edge.
            *result = 0;
            return true;
        }

        if (msg->message == WM_NCHITTEST) {
            // Provide resize hit-test for the 6 px outer ring so users can still
            // drag the window edges. The QML title bar handles dragging via
            // startSystemMove(), so we only need to expose resize handles here.
            const int x = GET_X_LPARAM(msg->lParam);
            const int y = GET_Y_LPARAM(msg->lParam);
            RECT wr{};
            GetWindowRect(msg->hwnd, &wr);
            const int border = 6;
            const bool left   = x >= wr.left  && x < wr.left  + border;
            const bool right  = x <  wr.right && x >= wr.right - border;
            const bool top    = y >= wr.top   && y < wr.top   + border;
            const bool bottom = y <  wr.bottom && y >= wr.bottom - border;

            if (top && left)        { *result = HTTOPLEFT;     return true; }
            if (top && right)       { *result = HTTOPRIGHT;    return true; }
            if (bottom && left)     { *result = HTBOTTOMLEFT;  return true; }
            if (bottom && right)    { *result = HTBOTTOMRIGHT; return true; }
            if (left)               { *result = HTLEFT;        return true; }
            if (right)              { *result = HTRIGHT;       return true; }
            if (top)                { *result = HTTOP;         return true; }
            if (bottom)             { *result = HTBOTTOM;      return true; }
            // Everything inside the window is client; QML handles the title-bar drag.
            return false;
        }

        return false;
    }

private:
    HWND shutdownRootWindow_ = nullptr;
    orion::OrionAppController* shutdownController_ = nullptr;
};

// ---- orion:// deep-link plumbing (zero-typing activation) -------------------
// The Discord delivery flow links buyers to orion://activate?key=... so the key
// lands in the launcher's activation field without any typing/paste errors.

// Magic tag on the WM_COPYDATA payload so we never interpret someone else's
// broadcast as a deep link ("ORDL" — Orion Deep Link).
constexpr ULONG_PTR kOrionDeepLinkCopyDataId = 0x4F52444C;

// [Codex finding #2 — 2026-09-20 round 2] The broker-trust logic (Authenticode
// signer gate + SIGNED release_manifest.json coverage gate) now lives in ONE
// shared translation unit, native_orion/src/BrokerInstallTrust.cpp, so the packed
// app (here) and the broker (OrionActivate.cpp) reach the same verdict for the
// same file and reuse the SAME pinned Ed25519 key + verifier as the updater. The
// expected Authenticode signer constants (kExpectedBrokerSignerSubstr /
// kExpectedBrokerSignerThumbprint — OWNER must set the real CN/SHA-1 from
// Desktop\VeniceSigning) also live there. See BrokerInstallTrust.h.
// Whether a sibling OrionActivate.exe is a GENUINE server-shard broker — see
// orion::genuineBrokerInstallPresent (BrokerInstallTrust.{h,cpp}). Resolves the
// production-build flag and (dev-only) release-manifest test-key override exactly
// as SecurityManager::verifyReleaseIntegrity does, so the two guards agree.
bool siblingBrokerIsGenuine()
{
    const QString appDir = QCoreApplication::applicationDirPath();
    const QString brokerExe = appDir + QStringLiteral("/OrionActivate.exe");
#ifdef ORION_PRODUCTION_BUILD
    constexpr bool productionBuild = true;
    const QString testId;
    const QByteArray testKey;
#else
    constexpr bool productionBuild = false;
    const QString testId = qEnvironmentVariable("ORION_RELEASE_MANIFEST_TEST_PUBKEY_ID").trimmed();
    const QByteArray testKey = qgetenv("ORION_RELEASE_MANIFEST_TEST_PUBKEY_B64").trimmed();
#endif
    return orion::genuineBrokerInstallPresent(brokerExe, appDir, productionBuild,
                                              testId, testKey, nullptr);
}

// Register the orion:// URL protocol for the CURRENT USER (HKCU — no elevation
// needed) pointing at this executable. Idempotent: rewrites the same values on
// every launch so the handler always tracks the installed/updated exe path.
void registerOrionProtocolHandler()
{
    // Server-shard installs ship the signed activation broker (OrionActivate.exe)
    // beside this payload, and the broker OWNS the orion:// handler: it runs
    // /api/license/redeem and writes the DPAPI shard session that the packed
    // bootstrap must read before this app can even start. If we re-registered
    // orion:// to point at ourselves (as a normal install does on every launch),
    // the next activation — an hwid reset or a move to a new PC — would bypass the
    // broker, no session would be written, and the packed build could never fetch
    // its shard. So when a GENUINE broker is present, leave the handler to it.
    //
    // [Codex finding #2] Defer ONLY when the sibling OrionActivate.exe is verified
    // (Authenticode-signed by Venice OR byte-covered by a SIGNED release_manifest.json,
    // whose detached Ed25519 signature is checked against the pinned production key)
    // — never on the mere existence of a file with that name. NOTE: because the
    // broker is not code-signed yet, in practice this defers via the SIGNED manifest
    // gate today, and additionally validates the Authenticode signature once signing
    // exists. Logic + pinned key are shared with the broker via BrokerInstallTrust.
    if (siblingBrokerIsGenuine()) {
        return;
    }
    const QString exe = QDir::toNativeSeparators(QCoreApplication::applicationFilePath());
    QSettings cls(QStringLiteral("HKEY_CURRENT_USER\\Software\\Classes\\orion"), QSettings::NativeFormat);
    cls.setValue(QStringLiteral("Default"), QStringLiteral("URL:Orion Protocol"));
    cls.setValue(QStringLiteral("URL Protocol"), QString());
    cls.setValue(QStringLiteral("DefaultIcon/Default"), QStringLiteral("\"%1\",0").arg(exe));
    cls.setValue(QStringLiteral("shell/open/command/Default"),
                 QStringLiteral("\"%1\" \"%2\"").arg(exe, QStringLiteral("%1")));
}

// Forward a deep-link URI to an already-running Orion instance over WM_COPYDATA.
// Returns true when a window was found and the message was delivered.
bool forwardDeepLinkToRunningInstance(const QString& uri)
{
    // Matched pair with `title: "Venice"` in qml/Main.qml — see the long note there.
    // If you change one, change the other: a mismatch does not fail loudly, it just
    // never finds the running instance and silently launches a duplicate.
    HWND target = FindWindowW(nullptr, L"Venice");
    if (!target || !IsWindow(target)) {
        return false;
    }

    DWORD verifiedProcessId = 0;
    if (!orion::deep_link_target_policy::windowOwnedByExecutable(
            target,
            QCoreApplication::applicationFilePath(),
            &verifiedProcessId)) {
        return false;
    }

    const std::wstring payload = uri.toStdWString();
    COPYDATASTRUCT cds{};
    cds.dwData = kOrionDeepLinkCopyDataId;
    cds.cbData = static_cast<DWORD>((payload.size() + 1) * sizeof(wchar_t));
    cds.lpData = const_cast<wchar_t*>(payload.c_str());

    // Re-bind the HWND immediately before sending the secret-bearing URI. If
    // the original process exited and Windows reused the handle, fail closed.
    DWORD finalProcessId = 0;
    if (!IsWindow(target)
        || GetWindowThreadProcessId(target, &finalProcessId) == 0
        || finalProcessId != verifiedProcessId) {
        return false;
    }
    SendMessageW(target, WM_COPYDATA, 0, reinterpret_cast<LPARAM>(&cds));
    // Bring the running launcher forward so the pre-filled AuthGate is visible.
    if (IsIconic(target)) {
        ShowWindow(target, SW_RESTORE);
    }
    SetForegroundWindow(target);
    return true;
}

// [CL2-P8-001 / GM2-005 / CX P8-03 2026-09-22] A second launcher must never run beside a live
// one: its Connect kills every OrionStream on the machine (the first instance's live stream
// included) and both share the input pipe, settings and logs. Bring the verified running
// window forward instead. Returns true when one was found.
bool activateRunningInstance()
{
    HWND target = FindWindowW(nullptr, L"Venice");
    if (!target || !IsWindow(target)) {
        return false;
    }
    DWORD verifiedProcessId = 0;
    if (!orion::deep_link_target_policy::windowOwnedByExecutable(
            target, QCoreApplication::applicationFilePath(), &verifiedProcessId)) {
        return false;
    }
    if (IsIconic(target)) {
        ShowWindow(target, SW_RESTORE);
    }
    SetForegroundWindow(target);
    return true;
}

// Receives WM_COPYDATA deep-link forwards from a second launcher instance and
// hands the URI to the controller on the GUI thread.
class DeepLinkNativeFilter : public QAbstractNativeEventFilter
{
public:
    explicit DeepLinkNativeFilter(orion::OrionAppController* controller)
        : controller_(controller) {}

    bool nativeEventFilter(const QByteArray& eventType, void* message, qintptr* result) override
    {
        if (eventType != "windows_generic_MSG") {
            return false;
        }
        MSG* msg = static_cast<MSG*>(message);
        if (!msg || msg->message != WM_COPYDATA) {
            return false;
        }
        const auto* cds = reinterpret_cast<const COPYDATASTRUCT*>(msg->lParam);
        if (!cds || cds->dwData != kOrionDeepLinkCopyDataId || !cds->lpData
            || cds->cbData < sizeof(wchar_t) || cds->cbData > 4096) {
            return false;
        }
        const QString uri = QString::fromWCharArray(
            static_cast<const wchar_t*>(cds->lpData),
            int(cds->cbData / sizeof(wchar_t))).trimmed();
        // fromWCharArray keeps the trailing NUL; trimmed() drops whitespace but not NULs.
        const qsizetype nul = uri.indexOf(QChar(u'\0'));
        const QString clean = nul >= 0 ? uri.left(nul) : uri;
        if (controller_ && !clean.isEmpty()) {
            controller_->applyActivationDeepLink(clean);
        }
        if (result) {
            *result = 1;
        }
        return true;
    }

private:
    orion::OrionAppController* controller_ = nullptr;
};

void applyNativeWindowChrome(HWND hwnd)
{
    if (!hwnd || !IsWindow(hwnd)) {
        return;
    }
    // Make sure WS_THICKFRAME + WS_MINIMIZEBOX are set so Windows uses the
    // standard minimize-to-taskbar genie animation. Qt's frameless flag strips
    // most of these so we restore them at the Win32 layer. Crucially we do NOT
    // add WS_CAPTION — that would draw the native title bar over the custom
    // QML title bar. The WM_NCCALCSIZE filter (above) reclaims the frame area
    // so the QML chrome paints edge-to-edge.
    LONG_PTR style = GetWindowLongPtrW(hwnd, GWL_STYLE);
    style |= WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX;
    style &= ~WS_CAPTION;
    SetWindowLongPtrW(hwnd, GWL_STYLE, style);

    // Tell DWM (Windows 11+) to round the actual HWND corners. On Windows 10 this
    // call simply returns S_OK with no effect, so it's safe.
    int pref = kDwmWindowCornerRound;
    DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &pref, sizeof(pref));

    // Force a non-client recalc so the new style takes effect immediately.
    SetWindowPos(hwnd, nullptr, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED);
}
} // namespace
#endif // Q_OS_WIN

int main(int argc, char* argv[])
{
    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("Orion Native"));
    QApplication::setOrganizationName(QStringLiteral("NexusVision"));
    QQuickStyle::setStyle(QStringLiteral("Basic"));

    // Resolve the native scheduler half of the same profile applied to the
    // sidecar later by RemotePlaySession. This must happen before controller
    // construction because AutomationEngine reads these values while applying
    // its initial configuration.
#ifdef ORION_PRODUCTION_BUILD
    constexpr bool preserveTimingOverrides = false;
#else
    constexpr bool preserveTimingOverrides = true;
#endif
    const QString nativeTimingProfile =
        orion::applyShippedNativeTimingProfile(preserveTimingOverrides);
    qInfo().noquote() << "Native shipped timing profile:" << nativeTimingProfile;

    // orion://activate?key=... deep link (zero-typing activation). The OS hands the
    // URI as a plain argv entry when the registered protocol handler launches us.
    QString deepLinkUri;
    const QStringList args = QCoreApplication::arguments();
    for (qsizetype i = 1; i < args.size(); ++i) {
        if (args.at(i).startsWith(QStringLiteral("orion://"), Qt::CaseInsensitive)) {
            deepLinkUri = args.at(i);
            break;
        }
    }

#ifdef Q_OS_WIN
    // Single-launcher guard for deep links: when Orion is already running, forward
    // the URI to that instance (WM_COPYDATA, handled by DeepLinkNativeFilter) and
    // exit instead of spawning a second launcher over a live session.
    HANDLE singletonMutex = CreateMutexW(nullptr, FALSE, L"Local\\OrionNativeLauncher");
    const bool alreadyRunning = (GetLastError() == ERROR_ALREADY_EXISTS);
    if (!deepLinkUri.isEmpty() && alreadyRunning && forwardDeepLinkToRunningInstance(deepLinkUri)) {
        if (singletonMutex) {
            CloseHandle(singletonMutex);
        }
        return 0;
    }
    // [M-08 r2 2026-09-23, Codex] The guard itself failing is not silent: say so and continue (a
    // missing mutex cannot tell us another copy is running, and refusing to start would lock the
    // customer out over a Windows resource error).
    if (!singletonMutex) {
        qWarning().noquote() << "Single-instance guard unavailable (CreateMutexW failed, error"
                             << GetLastError() << "); a second Venice window cannot be detected.";
    }
    // [CL2-P8-001] Every launch, not just deep links: one launcher per user session. The mutex
    // still exists while a previous instance is shutting down, so the message tells the user
    // what to do rather than silently doing nothing.
    if (alreadyRunning) {
        if (!activateRunningInstance()) {
            MessageBoxW(nullptr,
                        L"Venice is already running (or still closing).\n\n"
                        L"Switch to the open Venice window, or wait a few seconds and try again.",
                        L"Venice", MB_OK | MB_ICONINFORMATION);
        }
        if (singletonMutex) {
            CloseHandle(singletonMutex);
        }
        return 0;
    }

    // Keep the orion:// handler registered + pointed at the current exe (HKCU, no
    // elevation). Runs every launch so updates/moves never leave a stale handler.
    registerOrionProtocolHandler();

    // Install the native frame filter BEFORE the window is created so the very
    // first WM_NCCALCSIZE is handled correctly. This is what stops Windows
    // reserving an empty black strip above the custom QML title bar.
    static FramelessNativeFilter framelessFilter;
    app.installNativeEventFilter(&framelessFilter);
#endif

    const QString appDir = QCoreApplication::applicationDirPath();
    QString rootDir = appDir;
#ifndef ORION_PRODUCTION_BUILD
    const QString desktopNexus = QDir::homePath() + QStringLiteral("/Desktop/NexusVision");
    const QString appDirNative = QDir::fromNativeSeparators(appDir).toLower();
    const bool devBuildDir = appDirNative.contains(QStringLiteral("/native_orion/build/"))
        || appDirNative.contains(QStringLiteral("/native_orion\\build\\"));
    // Root-dir redirection is a DEV convenience (run the build-dir exe against the source-tree
    // settings/models). In production it is an integrity hole: an env var + a planted
    // ~/Desktop/NexusVision/settings.json would repoint the app at an attacker-controlled root,
    // sidestepping the install-dir the release manifest validates. Compiled out entirely in prod.
    const bool allowDevRootFallback = devBuildDir || qEnvironmentVariableIntValue("ORION_DEV_ROOT_FALLBACK") > 0;
    if (allowDevRootFallback && QFileInfo::exists(desktopNexus + QStringLiteral("/settings.json"))) {
        rootDir = desktopNexus;
    }
#endif
    const QString iconPath = rootDir + QStringLiteral("/assets/orion.ico");
    if (QFileInfo::exists(iconPath)) {
        QApplication::setWindowIcon(QIcon(iconPath));
    }

    // [2026-09-11] The controller is constructed BEFORE the QML engine so it is destroyed AFTER
    // it. Crash dump 15:19 (OrionNative.exe.23976.dmp, Release PDB): with the old order the
    // controller's ~QObject ran while the engine, its windows and the scene-graph render thread
    // were still alive, and removePostedEvents tore down a blocking meta-call event whose sender
    // thread had already gone -> QSemaphore::release on a dead semaphore -> 0xc0000409. QML holds
    // the controller only as a context property, so the engine must go first anyway.
    QDir().mkpath(rootDir + QStringLiteral("/logs"));
    orion::native_crash_diagnostics::install(rootDir + QStringLiteral("/logs/launcher_crash.log"));
    orion::OrionAppController controller(rootDir);
    QQmlApplicationEngine engine;
    auto* frameProvider = new orion::RemoteFrameProvider;
    engine.addImageProvider(QStringLiteral("remote"), frameProvider);

    controller.setFrameProvider(frameProvider);
    if (!deepLinkUri.isEmpty()) {
        // Launched via orion://activate?key=... — stage the key BEFORE the QML loads
        // so AuthGate's field is pre-filled on first paint.
        controller.applyActivationDeepLink(deepLinkUri);
    }
#ifdef Q_OS_WIN
    // Deep-link forwards from any later second instance (WM_COPYDATA).
    static DeepLinkNativeFilter deepLinkFilter(&controller);
    app.installNativeEventFilter(&deepLinkFilter);
#endif
    engine.rootContext()->setContextProperty(QStringLiteral("orion"), &controller);
    QStringList qmlWarnings;
    QObject::connect(&engine, &QQmlApplicationEngine::warnings, &app, [&qmlWarnings](const QList<QQmlError>& warnings) {
        for (const auto& warning : warnings) {
            qmlWarnings.append(warning.toString());
        }
    });
    const QUrl qmlEntry(QStringLiteral("qrc:/qt/qml/OrionNative/qml/Main.qml"));
    const QUrl legacyQmlEntry(QStringLiteral("qrc:/OrionNative/qml/Main.qml"));
    engine.load(qmlEntry);
    if (engine.rootObjects().isEmpty()) {
        engine.load(legacyQmlEntry);
    }
    if (engine.rootObjects().isEmpty()) {
        QFile log(rootDir + QStringLiteral("/native_orion/qml_runtime.log"));
        if (log.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) {
            QTextStream stream(&log);
            stream << "Tried " << qmlEntry.toString() << '\n';
            stream << "Tried " << legacyQmlEntry.toString() << '\n';
            for (const auto& warning : qmlWarnings) {
                stream << warning << '\n';
            }
        }
        return 1;
    }

    // A popup/tool window can keep Qt's event loop alive after the launcher
    // disappears, so do not rely on any single Qt close notification. The
    // closing signal is the normal path; visibleChanged(false) catches a native
    // WM_CLOSE that only hides the frameless root. Minimize keeps visible=true.
    for (QObject* root : engine.rootObjects()) {
        if (auto* window = qobject_cast<QQuickWindow*>(root)) {
            QObject::connect(window, &QQuickWindow::closing, &controller,
                             [&controller](QQuickCloseEvent*) {
                                 controller.requestApplicationShutdown();
                             });
            QObject::connect(window, &QWindow::visibleChanged, &controller,
                             [&controller](bool visible) {
                                 if (orion::shouldRequestShutdownForRootVisibility(visible)) {
                                     controller.requestApplicationShutdown();
                                 }
                             });
#ifdef Q_OS_WIN
            // Register the exact top-level HWND with the native close filter.
            // winId() is valid after engine.load() and makes the platform window
            // concrete if Qt deferred its creation.
            framelessFilter.setShutdownTarget(
                reinterpret_cast<HWND>(window->winId()), &controller);
#endif
        }
    }

    QObject::connect(&app, &QGuiApplication::lastWindowClosed, &controller,
                     [&controller]() { controller.requestApplicationShutdown(); });

#ifdef Q_OS_WIN
    // After the root QQuickWindow is exposed, grab its HWND and apply native
    // window chrome (rounded corners + minimize animation). Use a single-shot
    // timer because the HWND isn't available until the window is shown.
    QTimer::singleShot(0, &app, [&engine]() {
        for (QObject* root : engine.rootObjects()) {
            if (auto* w = qobject_cast<QWindow*>(root)) {
                if (auto hwnd = reinterpret_cast<HWND>(w->winId())) {
                    applyNativeWindowChrome(hwnd);
                }
            }
        }
    });
#endif

    // Updater handoff and future non-window quit paths run the same cleanup
    // synchronously before the event loop ends.
    QObject::connect(&app, &QCoreApplication::aboutToQuit, [&controller]() {
        controller.prepareForApplicationExit();
    });

    const int exitCode = QApplication::exec();
#ifdef Q_OS_WIN
    // The native filter has static lifetime; do not leave it pointing at the
    // stack-owned controller while local destructors unwind after exec().
    framelessFilter.setShutdownTarget(nullptr, nullptr);
#endif
    // Drain what is still queued for the GUI thread while every sender is alive: a blocking
    // cross-thread call left in the queue is satisfied here instead of being destroyed later
    // against a sender that no longer exists (see the controller/engine order note above).
    for (int pass = 0; pass < 3; ++pass) {
        QCoreApplication::sendPostedEvents();
        QCoreApplication::processEvents(QEventLoop::AllEvents, 50);
    }
    return exitCode;
}
