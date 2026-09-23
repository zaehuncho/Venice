#include "OrionAppController.h"
#include "ActivityLogClipboard.h"
#include "ReleasePathPolicy.h"
#include "LeadCalibrationPolicy.h"
#include "OrionPaths.h"
#include "GuiFreezeWatchdogPolicy.h"
#include "PacketBridgeAuthority.h"
#include "PacketBridgeServiceNames.h"  // [task #64] VeniceNetSvc/NexusVisionSvc resolution

#include "AutomationAccessPolicy.h"
#include "ControllerRoutingPolicy.h"
#include "FireEpochClock.h"
#include "MeterOverlayPolicy.h"
#include "PreciseFirePolicy.h"
#include "RawInputDeviceIdentityCache.h"
#include "SidecarLaunchPolicy.h"
#include "ShotIntentPolicy.h"
#include "UpdateGatePolicy.h"
#include "UsbPadPowerPolicy.h"
#include "ShotReleasePolicy.h"
#include "VeniceProfile.h"
#include "VideoInputDeviceEnumeration.h"

#include <QtCore/QDateTime>
#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QMutexLocker>
#include <QtCore/QProcess>
#include <QtCore/QRegularExpression>
#include <QtCore/QSaveFile>
#include <QtCore/QStandardPaths>
#include <QtCore/QTemporaryFile>
#include <QtCore/private/qzipwriter_p.h>
#include "Diagnostics.h"
#include "SidecarWatchdog.h"  // isCaptureCardSource / sidecarRestartDelayMs for the capture preview handoff
#include "WinMmButtonMapping.h"  // Sony-order dwButtons/dwPOV mapping for the WinMM fallback route
#include <chrono>
#include <QtCore/QScopedValueRollback>
#include <QtCore/QTextStream>
#include <QtCore/QTimer>
#include <QtCore/QUrl>
#include <QtCore/QUrlQuery>
#include <QtGui/QClipboard>
#include <QtGui/QGuiApplication>
#include <QtGui/QColor>
#include <QtGui/QDesktopServices>
#include <QtGui/QPainter>
#include <QtGui/QWindow>
#include <QtNetwork/QHostAddress>
#include <QtNetwork/QTcpSocket>
#include <QtNetwork/QNetworkInformation>   // [CL2-P8-002 2026-09-23] network-return heartbeat

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <iterator>
#include <limits>
#include <mutex>
#include <tuple>
#include <thread>
#include <vector>

#if ORION_WITH_OPENCV
#include <opencv2/imgproc.hpp>
#endif

#ifdef Q_OS_WIN
#include <Windows.h>
#include <Xinput.h>
#include <mmsystem.h>
#include <hidsdi.h>
#include <iphlpapi.h>
#include <ws2tcpip.h>
#include <tlhelp32.h>     // process snapshot: "is a stream client still alive?" without spawning tasklist
#include <shellapi.h>     // [ORION_UPDATE_ELEVATE] ShellExecuteExW "runas" for the updater handoff
#include "PreciseWaitTimer.h"  // [ORION_PRECISE_WAIT] hi-res fire wakeup + timer-res throttle opt-out
#endif

namespace orion {

namespace {

static constexpr quint16 PacketBridgePort = 47291;

RemotePlaySession::BandwidthMode bandwidthModeFromString(const QString& value)
{
    const QString lower = value.trimmed().toLower();
    if (lower == QLatin1String("quality")) return RemotePlaySession::BandwidthMode::Quality;
    if (lower == QLatin1String("lowbandwidth") || lower == QLatin1String("low") || lower == QLatin1String("low_bandwidth"))
        return RemotePlaySession::BandwidthMode::LowBandwidth;
    if (lower == QLatin1String("ultralow") || lower == QLatin1String("ultra_low") || lower == QLatin1String("ultra"))
        return RemotePlaySession::BandwidthMode::UltraLow;
    if (lower == QLatin1String("experimental120") || lower == QLatin1String("probe120"))
        return RemotePlaySession::BandwidthMode::Experimental120;
    if (lower == QLatin1String("experimental240") || lower == QLatin1String("probe240"))
        return RemotePlaySession::BandwidthMode::Experimental240;
    return RemotePlaySession::BandwidthMode::Balanced;
}

#ifdef Q_OS_WIN
// [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] Who actually produced the desktop UI
// message we are looking at right now.
//
// GetCurrentInputMessageSource() (user32, Windows 8+) reports the origin of the
// message the calling thread is currently processing. A pad mapper -- Steam
// Input's desktop configuration, DS4Windows, DualSenseX -- is a user-mode process
// synthesizing mouse/keyboard input with SendInput(), which the OS tags
// IMO_INJECTED. The owner's own mouse and keyboard are IMO_HARDWARE. That is the
// exact discriminator the 2026-09-11 timing guard lacked, and unlike a time
// window it cannot lose a race with the 4 ms input poll.
//
// Resolved dynamically so the build does not depend on the SDK's WINVER, and
// fails to Unknown (never to Injected) so an unavailable API can only ever fall
// back to the timing guard.
orion::DesktopUiInputOrigin currentDesktopUiInputOrigin()
{
    struct OrionInputMessageSource {
        DWORD deviceType;
        DWORD originId;
    };
    using GetSourceFn = BOOL(WINAPI*)(OrionInputMessageSource*);
    static const GetSourceFn getSource = []() -> GetSourceFn {
        if (HMODULE user32 = GetModuleHandleW(L"user32.dll")) {
            return reinterpret_cast<GetSourceFn>(
                reinterpret_cast<void*>(
                    GetProcAddress(user32, "GetCurrentInputMessageSource")));
        }
        return nullptr;
    }();
    if (!getSource) {
        return orion::DesktopUiInputOrigin::Unknown;
    }
    OrionInputMessageSource source{};
    if (!getSource(&source)) {
        return orion::DesktopUiInputOrigin::Unknown;
    }
    constexpr DWORD kOriginUnavailable = 0;  // IMO_UNAVAILABLE
    constexpr DWORD kOriginHardware = 1;     // IMO_HARDWARE
    constexpr DWORD kOriginInjected = 2;     // IMO_INJECTED
    constexpr DWORD kOriginSystem = 4;       // IMO_SYSTEM
    switch (source.originId) {
    case kOriginInjected:
        return orion::DesktopUiInputOrigin::Injected;
    case kOriginHardware:
        return orion::DesktopUiInputOrigin::Hardware;
    // IMO_SYSTEM is the OS synthesizing a message on its own behalf (menu
    // tracking, a window move). It is deliberately NOT treated as injected:
    // fail open to the timing guard rather than eat a legitimate system message.
    case kOriginSystem:
    case kOriginUnavailable:
    default:
        return orion::DesktopUiInputOrigin::Unknown;
    }
}

// --- GRACEFUL CHIAKI DISCONNECT -------------------------------------------------------------
//
// Every chiaki termination used to be a TerminateProcess (a blanket `taskkill /F /T` sweep), so
// chiaki_session_stop() never ran, no Takion/ctrl disconnect was ever sent, and the PS5 simply
// saw the transport vanish — which it reports as "LAN cable disconnected". The sidecar now closes
// the session cleanly on its shutdown command, so the native side must let that finish and keep
// the force-kill purely as a last-resort safety net for a client that genuinely refuses to exit.
//
// How long the disconnect path waits for a gracefully-shutting-down stream client to exit before
// resorting to the /F sweep. remotePlay_.stop() has already waited out kSidecarGracefulShutdownMs
// by the time this runs, so this is only the tail margin for the client's own exit.
constexpr int kTeardownChiakiExitPollMs = 750;

// True when any Remote Play stream-client image is still running. Uses a toolhelp snapshot
// (microseconds, no child process) rather than tasklist.exe so the poll itself can never become
// another GUI-thread stall on the disconnect path.
bool anyChiakiClientRunning()
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        return true;   // can't tell -> assume alive so the force-kill safety net still runs
    }
    PROCESSENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    bool found = false;
    if (Process32FirstW(snap, &entry)) {
        do {
            const QString name = QString::fromWCharArray(entry.szExeFile);
            if (name.compare(QLatin1String("OrionStream.exe"), Qt::CaseInsensitive) == 0
                || name.compare(QLatin1String("chiaki.exe"), Qt::CaseInsensitive) == 0
                || name.compare(QLatin1String("chiaki-ng.exe"), Qt::CaseInsensitive) == 0
                || name.compare(QLatin1String("chiaki4deck.exe"), Qt::CaseInsensitive) == 0) {
                found = true;
            }
        } while (!found && Process32NextW(snap, &entry));
    }
    CloseHandle(snap);
    return found;
}

// Poll for up to timeoutMs for the stream clients to exit on their own (the graceful path).
// Returns true only if one is STILL alive at the deadline — i.e. a force-kill is warranted.
// Returns as soon as the last one is gone, so a clean disconnect pays ~0ms here.
bool chiakiClientsAliveAfterGrace(int timeoutMs)
{
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeoutMs);
    for (;;) {
        if (!anyChiakiClientRunning()) {
            return false;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

int normalizeAxis(SHORT value, SHORT deadzone)
{
    if (std::abs(static_cast<int>(value)) < deadzone) {
        return 0;
    }
    const double scaled = static_cast<double>(value) / 32767.0 * 127.0;
    return std::clamp(static_cast<int>(std::round(scaled)), -127, 127);
}

int normalizeJoyAxis(DWORD value, DWORD minValue, DWORD maxValue)
{
    if (maxValue <= minValue) {
        minValue = 0;
        maxValue = 65535;
    }
    const double t = (static_cast<double>(value) - static_cast<double>(minValue))
                     / (static_cast<double>(maxValue) - static_cast<double>(minValue));
    const double centered = (t * 2.0) - 1.0;
    const int scaled = std::clamp(static_cast<int>(std::round(centered * 127.0)), -127, 127);
    return std::abs(scaled) < 8 ? 0 : scaled;
}

int normalizeHidAxis(quint8 value)
{
    const int centered = static_cast<int>(value) - 128;
    if (std::abs(centered) < 8) {
        return 0;
    }
    return std::clamp(centered, -127, 127);
}

uint16_t xinputDpadBitsFromNibble(quint8 value)
{
    switch (value & 0x0F) {
    case 0: return XINPUT_GAMEPAD_DPAD_UP;
    case 1: return XINPUT_GAMEPAD_DPAD_UP | XINPUT_GAMEPAD_DPAD_RIGHT;
    case 2: return XINPUT_GAMEPAD_DPAD_RIGHT;
    case 3: return XINPUT_GAMEPAD_DPAD_RIGHT | XINPUT_GAMEPAD_DPAD_DOWN;
    case 4: return XINPUT_GAMEPAD_DPAD_DOWN;
    case 5: return XINPUT_GAMEPAD_DPAD_DOWN | XINPUT_GAMEPAD_DPAD_LEFT;
    case 6: return XINPUT_GAMEPAD_DPAD_LEFT;
    case 7: return XINPUT_GAMEPAD_DPAD_LEFT | XINPUT_GAMEPAD_DPAD_UP;
    default: return 0;
    }
}

int dpadNibbleFromXinputButtons(uint16_t buttons)
{
    const bool up = (buttons & XINPUT_GAMEPAD_DPAD_UP) != 0;
    const bool down = (buttons & XINPUT_GAMEPAD_DPAD_DOWN) != 0;
    const bool left = (buttons & XINPUT_GAMEPAD_DPAD_LEFT) != 0;
    const bool right = (buttons & XINPUT_GAMEPAD_DPAD_RIGHT) != 0;
    if (up && right) return 1;
    if (right && down) return 3;
    if (down && left) return 5;
    if (left && up) return 7;
    if (up) return 0;
    if (right) return 2;
    if (down) return 4;
    if (left) return 6;
    return 8;
}

QString rawDevicePath(HANDLE device)
{
    UINT chars = 0;
    if (GetRawInputDeviceInfoW(device, RIDI_DEVICENAME, nullptr, &chars) != 0 || chars == 0) {
        return {};
    }
    std::vector<wchar_t> buffer(chars + 1, L'\0');
    if (GetRawInputDeviceInfoW(device, RIDI_DEVICENAME, buffer.data(), &chars) == UINT(-1)) {
        return {};
    }
    return QString::fromWCharArray(buffer.data()).toUpper();
}

QString classifyRawInputControllerPath(const QString& path)
{
    if (!path.contains(QStringLiteral("VID_054C"))) {
        return {};
    }
    if (path.contains(QStringLiteral("PID_0CE6")) ||
        path.contains(QStringLiteral("PID_0DF2")) ||
        path.contains(QStringLiteral("PID_0E5F"))) {
        return QStringLiteral("DualSense");
    }
    if (path.contains(QStringLiteral("PID_05C4")) ||
        path.contains(QStringLiteral("PID_09CC"))) {
        return QStringLiteral("DualShock 4");
    }
    return QStringLiteral("PlayStation HID pad");
}

QString classifyRawInputController(HANDLE device)
{
    return classifyRawInputControllerPath(rawDevicePath(device));
}

bool rawInputPathLooksVirtual(const QString& path);

bool findRawInputController(QHash<quintptr, QString>* cache, QString* label, QString* devicePath = nullptr)
{
    UINT count = 0;
    if (GetRawInputDeviceList(nullptr, &count, sizeof(RAWINPUTDEVICELIST)) != 0 || count == 0) {
        return false;
    }
    std::vector<RAWINPUTDEVICELIST> devices(count);
    if (GetRawInputDeviceList(devices.data(), &count, sizeof(RAWINPUTDEVICELIST)) == UINT(-1)) {
        return false;
    }

    QString bestKind;
    QString bestPath;
    int bestScore = -1;
    for (const auto& dev : devices) {
        if (dev.dwType != RIM_TYPEHID) {
            continue;
        }
        RID_DEVICE_INFO info {};
        info.cbSize = sizeof(info);
        UINT infoSize = sizeof(info);
        if (GetRawInputDeviceInfoW(dev.hDevice, RIDI_DEVICEINFO, &info, &infoSize) == UINT(-1)) {
            continue;
        }
        if (info.dwType != RIM_TYPEHID || info.hid.usUsagePage != 0x01 ||
            (info.hid.usUsage != 0x04 && info.hid.usUsage != 0x05)) {
            continue;
        }

        const auto key = reinterpret_cast<quintptr>(dev.hDevice);
        QString kind = cache ? cache->value(key) : QString();
        if (kind.isEmpty()) {
            kind = classifyRawInputController(dev.hDevice);
            if (cache && !kind.isEmpty()) {
                cache->insert(key, kind);
            }
        }
        if (!kind.isEmpty()) {
            const QString path = rawDevicePath(dev.hDevice);
            int score = 10;
            if (kind.contains(QStringLiteral("DualSense"), Qt::CaseInsensitive)) {
                score += 80;
            } else if (kind.contains(QStringLiteral("DualShock"), Qt::CaseInsensitive)) {
                score += 50;
            }
            if (!rawInputPathLooksVirtual(path)) {
                score += 30;
            }
            if (score > bestScore) {
                bestScore = score;
                bestKind = kind;
                bestPath = path;
            }
        }
    }
    if (bestScore < 0) {
        return false;
    }
    if (label) {
        *label = bestKind;
    }
    if (devicePath) {
        *devicePath = bestPath;
    }
    return true;
}

bool sendSonyLightbar(const QString& devicePath, const QString& kind, const QColor& color, QString* status)
{
    if (devicePath.isEmpty()) {
        if (status) {
            *status = QStringLiteral("LED unavailable for this device path");
        }
        return false;
    }

    HANDLE handle = CreateFileW(
        reinterpret_cast<LPCWSTR>(devicePath.utf16()),
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        if (status) {
            *status = QStringLiteral("LED unavailable for this device path");
        }
        return false;
    }

    QByteArray report;
    if (kind.contains(QStringLiteral("DualShock"), Qt::CaseInsensitive)) {
        // DS4 USB output report 0x05. Byte 1 enables rumble/lightbar update;
        // bytes 6-8 are RGB.
        report = QByteArray(32, char(0));
        report[0] = char(0x05);
        report[1] = '\xFF';
        report[6] = char(color.red());
        report[7] = char(color.green());
        report[8] = char(color.blue());
    } else if (kind.contains(QStringLiteral("DualSense"), Qt::CaseInsensitive)) {
        // DualSense USB output report 0x02: a 1-byte report ID followed by the
        // 47-byte common output struct.
        //
        // The offsets below are counted from the START OF THE BUFFER, i.e. they
        // are the struct offset plus one for the report ID. Getting that +1
        // wrong is silent — HidD_SetOutputReport still returns TRUE, the pad
        // just ignores you — so they are named rather than inlined:
        //
        //   [0]  report id (0x02)
        //   [1]  valid_flag0   (struct +0)
        //   [2]  valid_flag1   (struct +1)
        //   [42] lightbar_setup(struct +41)
        //   [43] led_brightness(struct +42)
        //   [44] player_leds   (struct +43)
        //   [45] lightbar_red  (struct +44)
        //   [46] lightbar_green(struct +45)
        //   [47] lightbar_blue (struct +46)
        //
        // valid_flag1 bit 2 is LIGHTBAR_CONTROL_ENABLE. Without it the RGB bytes
        // are parsed but discarded, which is the difference between a colour
        // picker and a colour picker that appears to work. Every other flag is
        // left clear on purpose: this write must change the lightbar and NOTHING
        // else — no rumble, no haptics select, no player-indicator takeover, no
        // mic LED — because the pad it is writing to is the same pad the timing
        // path is reading from.
        constexpr int kLightbarRed = 45;
        constexpr int kLightbarGreen = 46;
        constexpr int kLightbarBlue = 47;
        constexpr char kValidFlag1LightbarControl = char(0x04);
        report = QByteArray(48, char(0));
        report[0] = char(0x02);
        report[1] = char(0x00);
        report[2] = kValidFlag1LightbarControl;
        report[kLightbarRed] = char(color.red());
        report[kLightbarGreen] = char(color.green());
        report[kLightbarBlue] = char(color.blue());
    } else {
        CloseHandle(handle);
        if (status) {
            *status = QStringLiteral("LED unavailable for this controller");
        }
        return false;
    }

    const BOOLEAN ok = HidD_SetOutputReport(
        handle,
        report.data(),
        static_cast<ULONG>(report.size()));
    CloseHandle(handle);
    if (!ok) {
        if (status) {
            *status = QStringLiteral("LED unavailable for this device path");
        }
        return false;
    }
    if (status) {
        *status = QStringLiteral("LED set to %1").arg(color.name(QColor::HexRgb).toUpper());
    }
    return true;
}

bool decodeSonyReport(const QByteArray& report, const QString& kind, ControllerState* state)
{
    if (!state || report.size() < 10) {
        return false;
    }
    const auto at = [&report](int index) -> quint8 {
        return index >= 0 && index < report.size() ? static_cast<quint8>(report.at(index)) : 0;
    };

    const quint8 reportId = at(0);
    int axis = 1;
    int face = 5;
    int shoulder = 6;
    int special = 7;
    int l2 = 8;
    int r2 = 9;

    if (kind.contains(QStringLiteral("DualSense"), Qt::CaseInsensitive)) {
        if (reportId == 0x31 && report.size() >= 11) {
            axis = 2; face = 9; shoulder = 10; special = 11; l2 = 6; r2 = 7;
        } else {
            axis = 1; face = 8; shoulder = 9; special = 10; l2 = 5; r2 = 6;
        }
    } else if (kind.contains(QStringLiteral("DualShock"), Qt::CaseInsensitive)) {
        if (reportId == 0x11 && report.size() >= 12) {
            axis = 3; face = 7; shoulder = 8; special = 9; l2 = 10; r2 = 11;
        } else {
            axis = 1; face = 5; shoulder = 6; special = 7; l2 = 8; r2 = 9;
        }
    } else if (report.size() >= 11 && (at(8) & 0x0F) <= 8) {
        axis = 1; face = 8; shoulder = 9; special = 10; l2 = 5; r2 = 6;
    }

    if (axis + 3 >= report.size() || face >= report.size() || shoulder >= report.size()) {
        return false;
    }

    ControllerState out;
    out.leftStickX = normalizeHidAxis(at(axis));
    out.leftStickY = normalizeHidAxis(at(axis + 1));
    out.rightStickX = normalizeHidAxis(at(axis + 2));
    out.rightStickY = normalizeHidAxis(at(axis + 3));

    const quint8 faceBits = at(face);
    const quint8 shoulderBits = at(shoulder);
    const quint8 specialBits = at(special);
    const quint8 dpad = faceBits & 0x0F;
    out.buttons = 0;
    out.dpad = dpad <= 8 ? static_cast<int>(dpad) : 8;
    out.buttons |= xinputDpadBitsFromNibble(dpad);
    if (faceBits & 0x10) out.buttons |= XINPUT_GAMEPAD_X;
    if (faceBits & 0x20) out.buttons |= XINPUT_GAMEPAD_A;
    if (faceBits & 0x40) out.buttons |= XINPUT_GAMEPAD_B;
    if (faceBits & 0x80) out.buttons |= XINPUT_GAMEPAD_Y;
    if (shoulderBits & 0x01) out.buttons |= XINPUT_GAMEPAD_LEFT_SHOULDER;
    if (shoulderBits & 0x02) out.buttons |= XINPUT_GAMEPAD_RIGHT_SHOULDER;
    if (shoulderBits & 0x10) out.buttons |= XINPUT_GAMEPAD_BACK;
    if (shoulderBits & 0x20) out.buttons |= XINPUT_GAMEPAD_START;
    if (shoulderBits & 0x40) out.buttons |= XINPUT_GAMEPAD_LEFT_THUMB;
    if (shoulderBits & 0x80) out.buttons |= XINPUT_GAMEPAD_RIGHT_THUMB;
    if (specialBits & 0x01) out.buttons |= XINPUT_GAMEPAD_GUIDE;
    out.touchpad = (specialBits & 0x02) != 0;
    out.l2 = std::max<int>(at(l2), (shoulderBits & 0x04) ? 255 : 0);
    out.r2 = std::max<int>(at(r2), (shoulderBits & 0x08) ? 255 : 0);
    *state = out;
    return true;
}

// Build the internal ControllerState from one WinMM joystick sample. Split out of the
// discovery scan so the per-tick known-id fast path (readWinMmControllerById) shares the
// exact same mapping.
ControllerState winMmSampleFrom(const JOYINFOEX& joy, const WinMmAxisRanges& ranges)
{
    ControllerState sample;
    sample.leftStickX = normalizeJoyAxis(joy.dwXpos, ranges.xMin, ranges.xMax);
    // Internal ControllerState Y is +down / up-negative (HID/DS4 convention — see
    // ControllerState::rightStickUp and the RawInput/XInput readers). WinMM dwYpos/dwRpos
    // surface the raw HID Y axes, whose MINIMUM is stick-up, so normalizeJoyAxis already
    // yields up = -127. The old negation here (copied from the XInput reader, whose sThumbLY
    // genuinely is +up) INVERTED up/down end-to-end whenever this fallback was the live route
    // (live 2026-07-06 session: route "WinMM Microsoft PC-joystick driver" -> Y-flipped pad).
    sample.leftStickY = normalizeJoyAxis(joy.dwYpos, ranges.yMin, ranges.yMax);
    sample.rightStickX = normalizeJoyAxis(joy.dwZpos, ranges.zMin, ranges.zMax);
    sample.rightStickY = normalizeJoyAxis(joy.dwRpos, ranges.rMin, ranges.rMax);

    // Buttons + POV hat: Sony HID-usage order (btn1=Square btn2=Cross btn3=Circle
    // btn4=Triangle ...), NOT the XBOX A/B/X/Y order the old inline mapping assumed —
    // that scrambled the face buttons whenever this fallback was the live route
    // (live 2026-07 session: Circle acted as Square, etc.). See WinMmButtonMapping.h.
    applyWinMmButtonsToSample(joy.dwButtons, joy.dwPOV, sample);
    return sample;
}

// Human-readable label of the MAPPED buttons in a WinMM sample, for the raw-mask
// diagnostic log line (lets a live press-test verify the assumed Sony button order).
QString winMmMappedButtonsLabel(const ControllerState& s)
{
    QStringList parts;
    if (s.square()) parts << QStringLiteral("Square");
    if (s.cross()) parts << QStringLiteral("Cross");
    if (s.circle()) parts << QStringLiteral("Circle");
    if (s.triangle()) parts << QStringLiteral("Triangle");
    if (s.l1()) parts << QStringLiteral("L1");
    if (s.r1()) parts << QStringLiteral("R1");
    if (s.l2 > 0) parts << QStringLiteral("L2");
    if (s.r2 > 0) parts << QStringLiteral("R2");
    if (s.create()) parts << QStringLiteral("Create");
    if (s.options()) parts << QStringLiteral("Options");
    if ((s.buttons & XINPUT_GAMEPAD_LEFT_THUMB) != 0) parts << QStringLiteral("L3");
    if ((s.buttons & XINPUT_GAMEPAD_RIGHT_THUMB) != 0) parts << QStringLiteral("R3");
    if (s.ps()) parts << QStringLiteral("PS");
    if (s.touchpad) parts << QStringLiteral("Touchpad");
    return parts.isEmpty() ? QStringLiteral("(none)") : parts.join(QLatin1Char('+'));
}

// Per-tick fast path for the LIVE WinMM fallback route: re-read ONLY the known joystick id
// (one joyGetPosEx — no device enumeration, no joyGetDevCapsW/registry). Discovery of a new
// device stays in readWinMmController below, throttled to ~1/s.
bool readWinMmControllerById(UINT joyId, const WinMmAxisRanges& ranges, ControllerState* state,
                             DWORD* rawButtonsOut = nullptr)
{
    if (!state) {
        return false;
    }
    JOYINFOEX joy = {};
    joy.dwSize = sizeof(JOYINFOEX);
    joy.dwFlags = JOY_RETURNALL;
    if (joyGetPosEx(joyId, &joy) != JOYERR_NOERROR) {
        return false;
    }
    *state = winMmSampleFrom(joy, ranges);
    if (rawButtonsOut) {
        *rawButtonsOut = joy.dwButtons;
    }
    return true;
}

bool readWinMmController(ControllerState* state, UINT* joyId, QString* label = nullptr,
                         bool* virtualDevice = nullptr, WinMmAxisRanges* rangesOut = nullptr,
                         DWORD* rawButtonsOut = nullptr, bool* sonyVidOut = nullptr)
{
    if (!state) {
        return false;
    }

    JOYINFOEX joy = {};
    JOYCAPS caps = {};
    const UINT maxDevices = joyGetNumDevs();
    for (UINT id = 0; id < maxDevices; ++id) {
        joy = {};
        joy.dwSize = sizeof(JOYINFOEX);
        joy.dwFlags = JOY_RETURNALL;
        if (joyGetPosEx(id, &joy) != JOYERR_NOERROR) {
            continue;
        }

        if (joyGetDevCapsW(id, &caps, sizeof(caps)) != JOYERR_NOERROR) {
            caps = {};
        }
        const QString name = QString::fromWCharArray(caps.szPname).trimmed();
        const QString upperName = name.toUpper();
        const bool looksVirtual = upperName.contains(QStringLiteral("VIGEM"))
            || upperName.contains(QStringLiteral("VIRTUAL"))
            || upperName.contains(QStringLiteral("XBOX 360"));

        WinMmAxisRanges ranges;
        ranges.xMin = caps.wXmin; ranges.xMax = caps.wXmax;
        ranges.yMin = caps.wYmin; ranges.yMax = caps.wYmax;
        ranges.zMin = caps.wZmin; ranges.zMax = caps.wZmax;
        ranges.rMin = caps.wRmin; ranges.rMax = caps.wRmax;

        *state = winMmSampleFrom(joy, ranges);
        if (rawButtonsOut) {
            *rawButtonsOut = joy.dwButtons;
        }
        if (joyId) {
            *joyId = id;
        }
        if (label) {
            *label = name.isEmpty() ? QStringLiteral("WinMM USB pad") : name;
        }
        if (virtualDevice) {
            *virtualDevice = looksVirtual;
        }
        if (rangesOut) {
            *rangesOut = ranges;
        }
        if (sonyVidOut) {
            // JOYCAPS carries the underlying HID VID: a Sony pad served by the legacy
            // WinMM route means RawInput SHOULD have owned it — used by the route-decision
            // diagnostic to call out a stale HidHide cloak (pad absent from RawInput
            // enumeration while WinMM still reads it).
            *sonyVidOut = caps.wMid == 0x054C;
        }
        return true;
    }

    return false;
}

ControllerState xinputStateToController(const XINPUT_STATE& nativeState)
{
    ControllerState physical;
    physical.buttons = nativeState.Gamepad.wButtons;
    physical.dpad = dpadNibbleFromXinputButtons(physical.buttons);
    physical.l2 = nativeState.Gamepad.bLeftTrigger;
    physical.r2 = nativeState.Gamepad.bRightTrigger;
    physical.leftStickX = normalizeAxis(nativeState.Gamepad.sThumbLX, XINPUT_GAMEPAD_LEFT_THUMB_DEADZONE);
    physical.leftStickY = -normalizeAxis(nativeState.Gamepad.sThumbLY, XINPUT_GAMEPAD_LEFT_THUMB_DEADZONE);
    physical.rightStickX = normalizeAxis(nativeState.Gamepad.sThumbRX, XINPUT_GAMEPAD_RIGHT_THUMB_DEADZONE);
    physical.rightStickY = -normalizeAxis(nativeState.Gamepad.sThumbRY, XINPUT_GAMEPAD_RIGHT_THUMB_DEADZONE);
    return physical;
}

bool rawInputPathLooksVirtual(const QString& path)
{
    return path.contains(QStringLiteral("VIGEM"), Qt::CaseInsensitive)
        || path.contains(QStringLiteral("IG_"), Qt::CaseInsensitive)
        || path.contains(QStringLiteral("ROOT\\HIDCLASS"), Qt::CaseInsensitive);
}

HWND findChiakiWindow()
{
    struct FindState {
        HWND hwnd = nullptr;
        qint64 bestArea = 0;
        DWORD ownPid = 0;
    } state;
    state.ownPid = GetCurrentProcessId();

    EnumWindows([](HWND hwnd, LPARAM lParam) -> BOOL {
        auto* s = reinterpret_cast<FindState*>(lParam);

        // Exclude Orion's own windows by PROCESS ID, never by title — the stream
        // client is "OrionStream.exe" titled "Orion Stream", and a title filter on
        // "orion" silently skipped it (the stream then floated on the desktop).
        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        if (!pid || pid == s->ownPid) {
            return TRUE;
        }

        // Skip windows we already reparented (WS_CHILD) so a stale handle never
        // beats a freshly recreated top-level stream window.
        const LONG_PTR style = GetWindowLongPtrW(hwnd, GWL_STYLE);
        if (style & WS_CHILD) {
            return TRUE;
        }

        wchar_t title[512] = {};
        GetWindowTextW(hwnd, title, 511);
        const QString lower = QString::fromWCharArray(title).trimmed().toLower();
        const bool titleMatch = lower.contains(QStringLiteral("chiaki"))
            || lower.contains(QStringLiteral("orion stream"));
        bool match = false;

        {
            HANDLE proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
            if (!proc) {
                return TRUE;
            }
            wchar_t imagePath[MAX_PATH * 4] = {};
            DWORD size = static_cast<DWORD>(sizeof(imagePath) / sizeof(imagePath[0]));
            const BOOL ok = QueryFullProcessImageNameW(proc, 0, imagePath, &size);
            CloseHandle(proc);
            if (!ok || size == 0) {
                return TRUE;
            }
            const QString exe = QFileInfo(QString::fromWCharArray(imagePath, static_cast<int>(size))).fileName().toLower();
            match = exe == QLatin1String("orionstream.exe")
                || exe == QLatin1String("chiaki.exe")
                || exe == QLatin1String("chiaki-ng.exe")
                || exe == QLatin1String("chiaki4deck.exe");
        }
        if (!match) {
            return TRUE;
        }

        // Prefer visible windows, but accept a not-yet-shown stream window (it can
        // exist for a beat before the first ShowWindow) so the embed lands on the
        // very first watchdog tick instead of after a desktop flash.
        RECT rect {};
        GetWindowRect(hwnd, &rect);

        // A hidden window qualifies ONLY when its TITLE matched. Qt processes own
        // several hidden, untitled helper windows; matching one of those by exe
        // name alone embeds an invisible helper while the real stream window
        // floats (live-seen: hwnd embedded with title=""). An exe-only match must
        // therefore be visible and stream-window sized.
        if (!titleMatch) {
            if (!IsWindowVisible(hwnd)
                || (rect.right - rect.left) < 200 || (rect.bottom - rect.top) < 120) {
                return TRUE;
            }
        }
        qint64 area = qMax<qint64>(1, static_cast<qint64>(rect.right - rect.left)) * qMax<qint64>(1, static_cast<qint64>(rect.bottom - rect.top));
        if (IsWindowVisible(hwnd)) {
            area *= 1000; // any visible candidate outranks every hidden one
        }
        if (area > s->bestArea) {
            s->bestArea = area;
            s->hwnd = hwnd;
        }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&state));

    return state.hwnd;
}

void optimizeChiakiWindowProcess(HWND hwnd)
{
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (!pid) {
        return;
    }
    HANDLE proc = OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!proc) {
        return;
    }
    SetPriorityClass(proc, HIGH_PRIORITY_CLASS);
    SetProcessPriorityBoost(proc, TRUE);
    CloseHandle(proc);
}

HWND mainWindowHandle()
{
    const auto windows = QGuiApplication::topLevelWindows();
    for (auto* window : windows) {
        if (window && window->isVisible()) {
            return reinterpret_cast<HWND>(window->winId());
        }
    }
    return nullptr;
}

// Device-pixel-ratio of the Orion window. QML hands us logical embed coordinates;
// SetWindowPos needs physical pixels, so we scale by this on high-DPI displays.
qreal mainWindowDevicePixelRatio()
{
    const auto windows = QGuiApplication::topLevelWindows();
    for (auto* window : windows) {
        if (window && window->isVisible()) {
            return window->devicePixelRatio();
        }
    }
    return 1.0;
}
#endif

} // namespace

#ifdef Q_OS_WIN
// Reads controller HID reports on a DEDICATED thread (its own message-only window and
// message pump), not the Qt GUI thread. WM_INPUT arrives at the device rate (~250/s for
// a USB DualSense) while the GUI thread under decoder-frame load services its message
// queue at only ~30/s -- the OS queue then backs up without bound and the "read" replays
// seconds-old stick states in slow motion (the live hook+frame input lag: a flick railed
// for 13s then released over ~1s). A bare pump on its own thread always drains at the
// device rate, so snapshot() is the freshest report regardless of UI load.
class OrionRawInputWorker {
public:
    struct Snapshot {
        ControllerState state{};
        QString label;
        QString devicePath;
        quintptr deviceHandle = 0;
        qint64 lastReportMs = 0;
    };
    enum class Status { Starting, Registered, Failed };

    explicit OrionRawInputWorker(OrionAppController* owner)
        : owner_(owner), thread_([this] { run(); })
    {
    }

    ~OrionRawInputWorker()
    {
        // The pump thread has a message queue (it created a window), so WM_QUIT lands
        // even between window messages. If the thread died before storing its id the
        // join below still returns (run() exited).
        if (const DWORD tid = threadId_.load())
            PostThreadMessageW(tid, WM_QUIT, 0, 0);
        if (thread_.joinable())
            thread_.join();
    }

    Status status() const { return status_.load(); }

    Snapshot snapshot() const
    {
        const auto waitStarted = std::chrono::steady_clock::now();
        Snapshot result;
        quint64 waitUs = 0;
        {
            QMutexLocker lock(&mutex_);
            waitUs = static_cast<quint64>(std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - waitStarted).count());
            result = snap_;
        }
        quint64 observed = maxSnapshotLockWaitUs_.load(std::memory_order_relaxed);
        while (observed < waitUs
               && !maxSnapshotLockWaitUs_.compare_exchange_weak(
                   observed, waitUs, std::memory_order_relaxed)) {
        }
        return result;
    }

    quint64 takeMaxSnapshotLockWaitUs() noexcept
    {
        return maxSnapshotLockWaitUs_.exchange(0, std::memory_order_relaxed);
    }

private:
    static LRESULT CALLBACK wndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
    {
        if (msg == WM_NCCREATE) {
            const auto* cs = reinterpret_cast<const CREATESTRUCTW*>(lp);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(cs->lpCreateParams));
        } else if (auto* self = reinterpret_cast<OrionRawInputWorker*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA))) {
            if (msg == WM_INPUT)
                self->handleInput(reinterpret_cast<HRAWINPUT>(lp));
            else if (msg == WM_INPUT_DEVICE_CHANGE)
                self->handleDeviceChange(wp, lp);
        }
        // WM_INPUT requires DefWindowProc for system-side cleanup, and it is harmless
        // for everything else this window receives.
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    void run()
    {
        threadId_.store(GetCurrentThreadId());
        WNDCLASSW wc{};
        wc.lpfnWndProc = &OrionRawInputWorker::wndProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = L"OrionRawInputSink";
        RegisterClassW(&wc); // already-registered is fine (re-create after teardown)
        HWND hwnd = CreateWindowExW(0, wc.lpszClassName, L"", 0, 0, 0, 0, 0,
                                    HWND_MESSAGE, nullptr, wc.hInstance, this);
        if (!hwnd) {
            status_.store(Status::Failed);
            return;
        }

        RAWINPUTDEVICE devices[2] = {};
        devices[0].usUsagePage = 0x01; // Generic Desktop
        devices[0].usUsage = 0x05;     // Game Pad
        devices[0].dwFlags = RIDEV_INPUTSINK | RIDEV_DEVNOTIFY;
        devices[0].hwndTarget = hwnd;
        devices[1].usUsagePage = 0x01; // Generic Desktop
        devices[1].usUsage = 0x04;     // Joystick
        devices[1].dwFlags = RIDEV_INPUTSINK | RIDEV_DEVNOTIFY;
        devices[1].hwndTarget = hwnd;
        if (RegisterRawInputDevices(devices, 2, sizeof(RAWINPUTDEVICE)) != TRUE) {
            status_.store(Status::Failed);
            DestroyWindow(hwnd);
            return;
        }
        status_.store(Status::Registered);

        MSG msg;
        while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        DestroyWindow(hwnd);
    }

    void handleInput(HRAWINPUT input)
    {
        UINT size = 0;
        if (GetRawInputData(input, RID_INPUT, nullptr, &size, sizeof(RAWINPUTHEADER)) != 0 || size == 0)
            return;
        QByteArray buffer(int(size), Qt::Uninitialized);
        if (GetRawInputData(input, RID_INPUT, buffer.data(), &size, sizeof(RAWINPUTHEADER)) == UINT(-1))
            return;
        const auto* raw = reinterpret_cast<const RAWINPUT*>(buffer.constData());
        if (!raw || raw->header.dwType != RIM_TYPEHID)
            return;

        const auto key = reinterpret_cast<quintptr>(raw->header.hDevice);
        const qint64 reportMs = QDateTime::currentMSecsSinceEpoch();
        const RawInputDeviceIdentity identity = identities_.resolve(
            key,
            reportMs,
            [device = raw->header.hDevice] { return rawDevicePath(device); },
            [](const QString& path) { return classifyRawInputControllerPath(path); });
        if (identity.kind.isEmpty())
            return;

        const auto& hid = raw->data.hid;
        const auto* data = reinterpret_cast<const char*>(hid.bRawData);
        for (DWORD i = 0; i < hid.dwCount; ++i) {
            const QByteArray report(data + (i * hid.dwSizeHid), int(hid.dwSizeHid));
            ControllerState decoded;
            if (decodeSonyReport(report, identity.kind, &decoded)) {
                QMutexLocker lock(&mutex_);
                snap_.state = decoded;
                snap_.label = identity.kind;
                snap_.devicePath = identity.path;
                snap_.deviceHandle = key;
                snap_.lastReportMs = reportMs;
            }
        }
    }

    void handleDeviceChange(WPARAM changeKind, LPARAM deviceHandle)
    {
        const auto handleVal = static_cast<quintptr>(deviceHandle);
        // Prefer the path resolved while the device was alive. Windows may no
        // longer answer RIDI_DEVICENAME by the time GIDC_REMOVAL is delivered.
        QString path = identities_.cachedPath(handleVal);
        if (path.isEmpty()) {
            path = rawDevicePath(reinterpret_cast<HANDLE>(deviceHandle));
        }
        bool removedActivePhysical = false;
        if (changeKind == GIDC_REMOVAL && !rawInputPathLooksVirtual(path)) {
            // Stop serving the removed pad's last state as "fresh" (mirrors the old
            // GUI-thread teardown; the GUI slot below still handles selection/logs).
            QMutexLocker lock(&mutex_);
            removedActivePhysical = rawInputRemovalMatchesActivePhysical(
                snap_.deviceHandle != 0 && snap_.deviceHandle == handleVal,
                !path.isEmpty(), !snap_.devicePath.isEmpty(),
                !path.isEmpty() && !snap_.devicePath.isEmpty()
                    && path.compare(snap_.devicePath, Qt::CaseInsensitive) == 0);
            if (removedActivePhysical) {
                snap_.state = ControllerState{};
                snap_.label.clear();
                snap_.devicePath.clear();
                snap_.deviceHandle = 0;
                snap_.lastReportMs = 0;
            }
        }
        identities_.clear(); // device set changed -> re-classify on the next report
        const auto kindVal = static_cast<quintptr>(changeKind);
        QMetaObject::invokeMethod(owner_, [o = owner_, handleVal, kindVal, path, removedActivePhysical] {
            o->handleRawInputDeviceChange(handleVal, kindVal, path, removedActivePhysical);
        }, Qt::QueuedConnection);
    }

    OrionAppController* owner_ = nullptr;
    mutable QMutex mutex_;
    Snapshot snap_;
    RawInputDeviceIdentityCache identities_; // worker-thread-only handle cache
    mutable std::atomic<quint64> maxSnapshotLockWaitUs_{0};
    std::atomic<Status> status_{Status::Starting};
    std::atomic<DWORD> threadId_{0};
    std::thread thread_; // last member: starts after everything above is initialized
};
#endif // Q_OS_WIN

// Sub-tick precise release fire thread. The engine decides on its 4ms tick; when a release
// deadline lands within the scheduler horizon it is committed here, and this thread submits
// the RELEASE output (ViGEm + input-hook pipe) at the exact deadline — coarse-sleeping to
// ~1.2ms out, then spinning the remainder — instead of letting the next tick fire it 0-4ms
// late on the grid. The write is serialized against the GUI tick's submit through
// OrionAppController::submitMutex_; the GUI tick additionally re-checks firedUnconsumed()
// under that mutex so it can never re-press the held state on top of a release this thread
// already wrote (the pump-fake race). Lock order everywhere: submitMutex_ before m_.
enum class PreciseFireArmResult {
    Armed,
    EngineDisarmed,
    RouteRejected,
    WindowRejected,
    MailboxBusy,
};

enum class PreciseFireRetargetResult {
    Retargeted,
    EngineDisarmed,
    InvalidToken,
    WrongToken,
    NotWaiting,
    OutcomePending,
    RouteRejected,
    WindowRejected,
    EngineRejected,
};

class OrionPreciseFireThread {
public:
    explicit OrionPreciseFireThread(OrionAppController* owner)
        : owner_(owner)
#ifdef Q_OS_WIN
        , hiresWait_(initHiresWait())
#endif
        , thread_([this] { run(); }) {}

    ~OrionPreciseFireThread()
    {
        {
            std::lock_guard<std::mutex> lock(m_);
            quit_ = true;
            armed_ = false;
        }
        cv_.notify_all();
#ifdef Q_OS_WIN
        if (hiresWait_) {
            SetEvent(wakeEvent_);
        }
#endif
        thread_.join();
#ifdef Q_OS_WIN
        if (wakeEvent_) {
            CloseHandle(wakeEvent_);
            wakeEvent_ = nullptr;
        }
#endif
    }

    // [ORION_PRECISE_WAIT] True when the worker's coarse leg waits on the high-resolution
    // waitable timer instead of the condvar (ORION_PRECISE_WAIT_HIRES=1 and creation succeeded).
    [[nodiscard]] bool hiresWaitActive() const
    {
#ifdef Q_OS_WIN
        return hiresWait_;
#else
        return false;
#endif
    }

    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] The style travels WITH the packet, sampled once at
    // arm time exactly as the mode and the route generation are. The final submit fence
    // re-validates the packet against it, so a settings write between arm and fire can never make
    // an already-armed release look malformed and drop the shot.
    PreciseFireArmResult arm(quint64 token, double absoluteDeadlineMs,
                             double absoluteAuthorityExpiryMs,
                             ShotMode releaseMode,
                             const ControllerState& releaseOutput,
                             quint64 routeGeneration,
                             LatencyControllerRoute route,
                             TempoReleaseStyle releaseStyle)
    {
        // Pair arm with disarmCurrent() under the same outer submit fence. A
        // watchdog first clears AutomationEngine::armed_; an in-flight GUI arm
        // that loses this race then observes the cleared gate and cannot publish a
        // copied deadline after the watchdog has neutraled the pad.
        QMutexLocker submitLock(&owner_->submitMutex_);
        std::lock_guard<std::mutex> lock(m_);
        if (!owner_->automation_.armed()) {
            return PreciseFireArmResult::EngineDisarmed;
        }
        const PreciseFireRouteBinding binding{routeGeneration, route};
        const LatencyControllerRoute liveRoute = PreciseFirePolicy::liveControllerRoute(
            owner_->remotePlay_.state() == RemotePlayState::Running,
            owner_->orionInput_.enabled(), owner_->orionInput_.connected(),
            owner_->controller_.isConnected(), owner_->controller_.isDs4Backend());
        if (!owner_->automation_.scheduledFireRouteBindingMatches(routeGeneration, route)
            || !PreciseFirePolicy::routeBindingMatches(
                binding, liveRoute, routeGeneration, route)) {
            return PreciseFireArmResult::RouteRejected;
        }
        if (armed_ || fired_ || failed_) {
            return PreciseFireArmResult::MailboxBusy;
        }
        const auto armedNow = std::chrono::steady_clock::now();
        // Re-read the engine clock only after both handoff locks are held. Passing
        // absolute engine deadlines prevents GUI/lock delay from extending either
        // the fire target or its finite evidence lease. In particular, a finite
        // expiry that crossed zero during the handoff remains expired instead of
        // becoming the worker's negative "unbounded" sentinel.
        const PreciseFireArmWindow window = PreciseFirePolicy::evaluate(
            absoluteDeadlineMs, absoluteAuthorityExpiryMs,
            owner_->automation_.engineNowMs());
        if (!window.allowed) {
            return PreciseFireArmResult::WindowRejected;
        }
        const auto target = armedNow
            + std::chrono::microseconds(static_cast<long long>(window.delayMs * 1000.0));
        const auto authorityExpiry = !window.finiteAuthority
            ? std::chrono::steady_clock::time_point::max()
            : armedNow + std::chrono::microseconds(static_cast<long long>(
                  window.authorityRemainingMs * 1000.0));
        token_ = token;
        target_ = target;
        authorityExpiry_ = authorityExpiry;
        releaseMode_ = releaseMode;
        releaseStyle_ = releaseStyle;
        releaseOutput_ = releaseOutput;
        routeGeneration_ = routeGeneration;
        route_ = route;
        ++targetRevision_;
        armed_ = true;
        aborted_ = false;   // [CONCURRENCY N2] fresh arm clears any stale abort
        owner_->lastArmedFireToken_.store(token, std::memory_order_release);
        cv_.notify_all();
#ifdef Q_OS_WIN
        // [ORION_PRECISE_WAIT] The hires timed wait blocks on wakeEvent_/timer, not on cv_,
        // so every state change that notifies the condvar must set the event too. The event is
        // auto-reset and only this worker waits on it; a signal landing while the worker is not
        // waiting stays latched until its next wait (one spurious wake, predicate re-checked).
        if (hiresWait_) {
            SetEvent(wakeEvent_);
        }
#endif
        return PreciseFireArmResult::Armed;
    }

    PreciseFireRetargetResult retarget(
        const PhaseAnchorRefinementProposal& proposal)
    {
        // This is a true replacement transaction, not the ordinary
        // invalidateUnconfirmedVisionSchedule()->scheduleFire() sequence. The old worker target
        // stays live until every mailbox, route, time-window and engine-identity gate passes.
        // Holding submitMutex_ also prevents the worker from crossing its physical-submit
        // boundary while the engine deadline and worker target are changed together.
        QMutexLocker submitLock(&owner_->submitMutex_);
        std::lock_guard<std::mutex> lock(m_);
        if (!owner_->automation_.armed()) {
            return PreciseFireRetargetResult::EngineDisarmed;
        }
        switch (PreciseFirePolicy::evaluateRetargetMailbox(
                    proposal.scheduleToken, token_, armed_, fired_, failed_)) {
        case PreciseFireRetargetGate::InvalidToken:
            return PreciseFireRetargetResult::InvalidToken;
        case PreciseFireRetargetGate::WrongToken:
            return PreciseFireRetargetResult::WrongToken;
        case PreciseFireRetargetGate::NotWaiting:
            return PreciseFireRetargetResult::NotWaiting;
        case PreciseFireRetargetGate::OutcomePending:
            return PreciseFireRetargetResult::OutcomePending;
        case PreciseFireRetargetGate::Allowed:
            break;
        }
        const PreciseFireRouteBinding binding{
            proposal.routeGeneration, proposal.route};
        const LatencyControllerRoute liveRoute = PreciseFirePolicy::liveControllerRoute(
            owner_->remotePlay_.state() == RemotePlayState::Running,
            owner_->orionInput_.enabled(), owner_->orionInput_.connected(),
            owner_->controller_.isConnected(), owner_->controller_.isDs4Backend());
        if (!owner_->automation_.scheduledFireRouteBindingMatches(
                proposal.routeGeneration, proposal.route)
            || !PreciseFirePolicy::routeBindingMatches(
                binding, liveRoute, proposal.routeGeneration, proposal.route)) {
            return PreciseFireRetargetResult::RouteRejected;
        }
        const auto retargetNow = std::chrono::steady_clock::now();
        const PreciseFireArmWindow window = PreciseFirePolicy::evaluate(
            proposal.refinedDeadlineMs, proposal.refinedAuthorityExpiryMs,
            owner_->automation_.engineNowMs());
        if (!window.allowed) {
            return PreciseFireRetargetResult::WindowRejected;
        }
        const auto refinedTarget = retargetNow
            + std::chrono::microseconds(
                static_cast<long long>(window.delayMs * 1000.0));
        const auto refinedAuthorityExpiry = !window.finiteAuthority
            ? std::chrono::steady_clock::time_point::max()
            : retargetNow + std::chrono::microseconds(static_cast<long long>(
                  window.authorityRemainingMs * 1000.0));
        // commitPhaseAnchorRefinement rechecks the proposal/token/shot identity and BOTH old/new
        // deadline runways at the instant of this call. If it refuses, none of the worker fields
        // below have changed and the c30 (or original c20) target still fires normally.
        if (!owner_->automation_.commitPhaseAnchorRefinement(
                proposal.id, proposal.scheduleToken, proposal.refinedDeadlineMs)) {
            return PreciseFireRetargetResult::EngineRejected;
        }
        target_ = refinedTarget;
        authorityExpiry_ = refinedAuthorityExpiry;
        ++targetRevision_;
        cv_.notify_all();
#ifdef Q_OS_WIN
        if (hiresWait_) {
            SetEvent(wakeEvent_);
        }
#endif
        return PreciseFireRetargetResult::Retargeted;
    }

    // [ORION_BLIND_WAITER 2026-09-15 owner] Replace the RELEASE PACKET of an already-armed token
    // without touching its deadline, its lease or its revision.
    //
    // WHY IT EXISTS: the NO METER blind token is now armed AT THE PRESS so a stalled GUI thread
    // cannot lose the release (see AutomationEngine::armBlindPreciseFire). arm() copies the pad
    // packet once, so without this the packet the worker presses would be up to a whole hold
    // (~950 ms) old — the exact hazard the old "don't copy the whole controller packet into the
    // worker hundreds of milliseconds early" comment named. With it the packet is at most ONE
    // GUI tick old: strictly fresher than the 24 ms the pre-arm horizon used to allow.
    //
    // SAFETY: token-scoped and armed-only, so it cannot touch another shot's token and becomes a
    // no-op the instant the fire loop claims the shot (the claim sets armed_ = false and copies
    // releaseOutput_ under this same mutex before unlocking for its final spin — there is no
    // window in which a refresh can tear the packet the worker is about to submit).
    // targetRevision_ is deliberately NOT bumped: a bump makes the waiting worker abandon this
    // target, which at the deadline would DROP the fire. Timing is untouched here by construction.
    bool refreshReleaseOutput(quint64 token, ShotMode releaseMode,
                              const ControllerState& releaseOutput,
                              TempoReleaseStyle releaseStyle)
    {
        std::lock_guard<std::mutex> lock(m_);
        if (!armed_ || token == 0 || token_ != token) {
            return false;
        }
        releaseMode_ = releaseMode;
        // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] The refreshed packet was generated under THIS
        // style, so the fence's copy must move with it or the two would disagree.
        releaseStyle_ = releaseStyle;
        releaseOutput_ = releaseOutput;
        return true;
    }

    void disarm(quint64 token)
    {
        // Linearize invalidation against the final pad submit. Lock order is the
        // controller-wide contract: submitMutex_ before m_. If the fire thread
        // already owns submitMutex_, its physical edge wins; otherwise aborted_
        // is visible before it can submit.
        QMutexLocker submitLock(&owner_->submitMutex_);
        std::lock_guard<std::mutex> lock(m_);
        // [CONCURRENCY N2] Record the abort intent whenever the token still matches — even if the
        // fire loop has ALREADY claimed the shot (armed_==false during the unlocked spin). The
        // pre-spin path still aborts via armed_=false (the loop's !armed_ guard); the post-spin
        // re-check reads aborted_ so a meter-vanish / shutdown disarm arriving mid-spin is honored
        // instead of pressing anyway ~1.2ms later.
        if (token_ == token) {
            armed_ = false;
            aborted_ = true;
        }
    }

    void disarmCurrent()
    {
        // Token-independent safety fence for watchdog/disconnect/device-loss
        // paths. Those paths must not depend on the GUI-owned token mirror: the
        // GUI may be frozen between arm() and its bookkeeping store.
        QMutexLocker submitLock(&owner_->submitMutex_);
        std::lock_guard<std::mutex> lock(m_);
        armed_ = false;
        aborted_ = true;
    }

    // [ORION_ROLLING_LEASE] Hand the engine's ROLLING meter-authority lease to an already-armed
    // token.
    //
    // arm() copies the lease exactly once (authorityExpiry_, :1044) and nothing ever updated it
    // again, while the engine's own copy keeps rolling forward as genuine frames arrive
    // (AutomationEngine::refreshScheduledFireAuthorityLease). The GUI tick only reaches arm()
    // when schedToken != armedToken, so for the entire life of an armed token the worker ran on
    // a lease frozen at arm time. One dropped frame near the deadline then expired that stale
    // copy and the worker declined to submit -- silently, through a bare `continue`.
    //
    // Measured, 2026-08-04 batch: 4 of the 5 live_tip_deadline_missed aborts were preceded by a
    // fed-frame gap of 27-32 ms inside the 200 ms before their deadline, against a session median
    // of 16.7 ms -- the 98th-99th percentile of that session's own gap distribution. One missing
    // frame, and a lease the engine had ALREADY renewed was never handed over.
    //
    // Safety, in the order that matters:
    //   * MONOTONIC     -- only ever extends, so it can never create a new decline and can never
    //                      be used to retire a token early.
    //   * TOKEN-SCOPED  -- a non-matching token is a no-op, so a lease cannot leak across shots.
    //   * NEVER REVIVES -- requires the ENGINE's lease to be unexpired at the instant of the call,
    //                      so evidence that has genuinely gone stale stays stale.
    //   * NEVER UNBOUNDS-- a negative expiry is the engine's "no finite authority" sentinel;
    //                      refusing it keeps a finite lease finite.
    //
    // The fire deadline is NOT touched here. This changes WHETHER a proven-future token may
    // submit, never WHEN it fires, so no overtime command can be manufactured on this path.
    bool refreshAuthority(quint64 token, double absoluteAuthorityExpiryMs, double engineNowMs)
    {
        std::lock_guard<std::mutex> lock(m_);
        const auto nowTp = std::chrono::steady_clock::now();
        // Special-case the unbounded sentinel rather than subtracting from time_point::max(),
        // which would overflow. +infinity then loses the monotonic comparison by construction.
        const double currentRemainingMs =
            authorityExpiry_ == std::chrono::steady_clock::time_point::max()
                ? std::numeric_limits<double>::infinity()
                : std::chrono::duration<double, std::milli>(authorityExpiry_ - nowTp).count();
        const PreciseFireAuthorityRefresh decision =
            PreciseFirePolicy::evaluateAuthorityRefresh(
                absoluteAuthorityExpiryMs, engineNowMs, currentRemainingMs,
                armed_, token != 0 && token_ == token);
        if (!decision.extend) {
            return false;
        }
        authorityExpiry_ = nowTp + std::chrono::microseconds(
            static_cast<long long>(decision.remainingMs * 1000.0));
        return true;
    }

    // Diagnostic drain for the authority decline that used to be a bare `continue`. Deliberately
    // NOT routed through failed_/takeFailure(): that mailbox drives ControllerFault and schedule
    // revocation, and this path is not a controller fault. Reporting only.
    bool takeAuthorityDecline(quint64* token, double* overdueMs, quint64* total)
    {
        const quint64 seen = authorityDeclines_.load(std::memory_order_acquire);
        if (seen == authorityDeclinesReported_) {
            return false;
        }
        authorityDeclinesReported_ = seen;
        *total = seen;
        *token = lastAuthorityDeclineToken_.load(std::memory_order_relaxed);
        *overdueMs = static_cast<double>(
            lastAuthorityDeclineOverdueUs_.load(std::memory_order_relaxed)) / 1000.0;
        return true;
    }

    // One-shot: hands the completed fire and its exact locally confirmed route to the GUI tick,
    // which preserves that metadata while confirming the token into the engine before process().
    bool takeFired(quint64* token, double* actualMs, PreciseFireDeliveryStage* stage,
                   uint32_t* transportSeq, PreciseFireDeliverySnapshot* snapshot,
                   quint64 expectedToken = 0)
    {
        std::lock_guard<std::mutex> lock(m_);
        if (!fired_ || (expectedToken != 0 && firedToken_ != expectedToken)) {
            return false;
        }
        fired_ = false;
        *token = firedToken_;
        *actualMs = firedActualMs_;
        *stage = firedDeliveryStage_;
        *transportSeq = firedTransportSeq_;
        *snapshot = firedSnapshot_;
        firedDeliveryStage_ = PreciseFireDeliveryStage::None;
        firedTransportSeq_ = 0;
        firedSnapshot_ = {};
        return true;
    }

    // One-shot failure mailbox. The worker never mutates QObject/controller
    // lifecycle state from its TIME_CRITICAL thread; the GUI poll consumes this
    // before AutomationEngine::process(), revokes the schedule, and surfaces a
    // ControllerFault. It is deliberately distinct from fired_, so a failed
    // token can never reach confirmScheduledFire or grading.
    bool takeFailure(quint64* token, QString* detail, quint64 expectedToken = 0)
    {
        std::lock_guard<std::mutex> lock(m_);
        if (!failed_ || (expectedToken != 0 && failedToken_ != expectedToken)) {
            return false;
        }
        failed_ = false;
        *token = failedToken_;
        *detail = failedDetail_;
        failedDetail_.clear();
        return true;
    }

    // True from exact local route acceptance until takeFired() consumes it. Checked by the GUI
    // tick under submitMutex_ right before its own submit.
    [[nodiscard]] bool firedUnconsumed() const
    {
        std::lock_guard<std::mutex> lock(m_);
        return fired_;
    }

private:
    void run();
    void runLoop();
    std::atomic<quint64> exceptions_{0};     // fire-thread exceptions survived (see run())

    OrionAppController* owner_ = nullptr;
    mutable std::mutex m_;
    std::condition_variable cv_;
    bool quit_ = false;
    bool armed_ = false;
    // [CONCURRENCY N2] Set by disarm(token) even AFTER the fire loop has claimed the shot
    // (armed_ already false), so a last-moment abort that races the ~1.2ms unlocked fire spin is
    // still honored: the loop re-checks this under m_ after the spin, before submit. Cleared on
    // every fresh arm().
    bool aborted_ = false;
    bool fired_ = false;
    bool failed_ = false;
    quint64 token_ = 0;
    quint64 targetRevision_ = 0;
    quint64 firedToken_ = 0;
    quint64 failedToken_ = 0;
    double firedActualMs_ = -1.0;
    PreciseFireDeliveryStage firedDeliveryStage_ = PreciseFireDeliveryStage::None;
    uint32_t firedTransportSeq_ = 0;
    PreciseFireDeliverySnapshot firedSnapshot_{};
    QString failedDetail_;
    std::chrono::steady_clock::time_point target_{};
    std::chrono::steady_clock::time_point authorityExpiry_ =
        std::chrono::steady_clock::time_point::max();
    // [ORION_ROLLING_LEASE] Observability for the authority decline in run(). That branch was a
    // bare `continue` -- no log, no mailbox, no counter -- which is why a token dying there was
    // invisible in every batch log and got misattributed to the predictor for a whole session.
    // Atomics because the fire loop writes them outside m_ on its TIME_CRITICAL thread.
    std::atomic<quint64> authorityDeclines_{0};
    std::atomic<quint64> lastAuthorityDeclineToken_{0};
    std::atomic<long long> lastAuthorityDeclineOverdueUs_{0};
    quint64 authorityDeclinesReported_ = 0;   // GUI-thread only
    ShotMode releaseMode_ = ShotMode::ButtonShot;
    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] The style the armed packet was built under; the
    // final submit fence validates against it. Flick is the shipped default.
    TempoReleaseStyle releaseStyle_ = TempoReleaseStyle::Flick;
    ControllerState releaseOutput_;
    quint64 routeGeneration_ = 0;
    LatencyControllerRoute route_ = LatencyControllerRoute::None;
#ifdef Q_OS_WIN
    // [ORION_PRECISE_WAIT] Default-on high-resolution wait (ORION_PRECISE_WAIT_HIRES=0 opts
    // out). The
    // condvar timed wait quantizes to the process timer resolution, and Windows 11 ignores this
    // process's timeBeginPeriod(1) while its window is occluded/minimized — measured as a
    // 4-15.6ms uniform submit-lateness tail on 15% of precise releases and as the
    // subtick_fire_at_past token-kill population (overdue 8.2-21.5ms, n=47). The hires waitable
    // timer fires at its due time regardless of the timer-resolution policy. Declared BEFORE
    // thread_ so setup completes before run() can observe it.
    orion::PreciseWaitTimer hiresTimer_;
    HANDLE wakeEvent_ = nullptr;
    bool hiresWait_ = false;

    bool initHiresWait()
    {
        // [ORION_METER_DELAY 2026-08-07] Default ON. Opt out with ORION_PRECISE_WAIT_HIRES=0.
        if (qEnvironmentVariableIsSet("ORION_PRECISE_WAIT_HIRES")
            && qEnvironmentVariableIntValue("ORION_PRECISE_WAIT_HIRES") == 0) {
            return false;
        }
        if (!hiresTimer_.create()) {
            return false;
        }
        wakeEvent_ = CreateEventW(nullptr, FALSE, FALSE, nullptr); // auto-reset
        return wakeEvent_ != nullptr;
    }
#endif
    std::thread thread_; // last member: starts after everything above is initialized
};

void OrionPreciseFireThread::run()
{
    // [2026-09-11] Two shutdown crash dumps (00:21 and 11:46) show std::bad_alloc thrown from Qt
    // on THIS thread while the application was tearing down -- an uncaught exception on a
    // std::thread is std::terminate, i.e. the whole process aborts (0xc0000409 FAST_FAIL_FATAL_APP_EXIT).
    // A fire-thread exception must never take the process with it: note it, fail the mailbox
    // closed (the engine sees a failed token and re-arms or aborts the shot through its normal
    // paths) and keep serving. Qt is deliberately not used here -- it may be the thing failing.
    for (;;) {
        try {
            runLoop();
            return;
        } catch (const std::exception& e) {
            std::fprintf(stderr, "[orion] precise-fire thread exception: %s\n", e.what());
        } catch (...) {
            std::fprintf(stderr, "[orion] precise-fire thread exception (unknown)\n");
        }
        std::lock_guard<std::mutex> guard(m_);
        exceptions_.fetch_add(1, std::memory_order_relaxed);
        if (quit_) {
            return;
        }
        armed_ = false;
        failed_ = true;
    }
}

void OrionPreciseFireThread::runLoop()
{
#ifdef Q_OS_WIN
    // top priority for the final spin.
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
    // [ORION_TIMER_RES_BATTERY 2026-08-07] timeBeginPeriod(1) is process-wide and
    // costs real battery when held permanently on DC power. Only assert it while a
    // fire is actually armed; drop it on the way back to idle. Boundary re-check
    // for armed_ before wait/release paired to keep the counter balanced across
    // spurious wake-ups.
    bool tbpAsserted = false;
    auto assertTbp = [&]() {
        if (!tbpAsserted) { timeBeginPeriod(1); tbpAsserted = true; }
    };
    auto releaseTbp = [&]() {
        if (tbpAsserted) { timeEndPeriod(1); tbpAsserted = false; }
    };
#endif
    std::unique_lock<std::mutex> lock(m_);
    for (;;) {
#ifdef Q_OS_WIN
        // [ORION_TIMER_RES_BATTERY 2026-08-07] Release 1ms timer granularity
        // while parked on the condvar. cv_.wait releases the mutex, so this
        // thread is genuinely idle here - no reason to keep the process-wide
        // scheduler tick at 1ms and burn battery. assertTbp() below picks it
        // back up before we do any timed wait.
        releaseTbp();
#endif
        cv_.wait(lock, [this] { return quit_ || armed_; });
        if (quit_) {
            break;
        }
#ifdef Q_OS_WIN
        // Armed: take 1ms scheduler granularity for the coarse-wait leg. The
        // hi-res leg is timer-res-independent, but the condvar fallback path
        // below IS timer-res sensitive.
        assertTbp();
#endif
        const quint64 token = token_;
        const quint64 targetRevision = targetRevision_;
        const auto target = target_;
        constexpr auto kSpinLead = std::chrono::microseconds(1200);
#ifdef Q_OS_WIN
        if (hiresWait_) {
            // [ORION_PRECISE_WAIT] Same predicate loop as the condvar leg below, but the timed
            // wait rides the high-resolution waitable timer, immune to the process timer
            // resolution (which Windows 11 silently coarsens to 15.625ms while the window is
            // occluded — the measured 4-15.6ms submit-lateness tail). State changes wake it via
            // wakeEvent_ (set alongside every cv_ notify); any wake reason just re-evaluates
            // the predicate under m_, so semantics are identical to a condvar spurious wake.
            // A Failed wait (timer machinery broke) falls back to one condvar wait so the leg
            // can never busy-loop; the fire semantics downstream are untouched either way.
            while (!quit_ && armed_ && token_ == token
                   && targetRevision_ == targetRevision
                   && std::chrono::steady_clock::now() + kSpinLead < target) {
                lock.unlock();
                const auto waitResult = hiresTimer_.waitUntil(target - kSpinLead, wakeEvent_);
                lock.lock();
                if (waitResult == orion::PreciseWaitTimer::WaitResult::Failed
                    && !quit_ && armed_ && token_ == token
                    && targetRevision_ == targetRevision
                    && std::chrono::steady_clock::now() + kSpinLead < target) {
                    cv_.wait_until(lock, target - kSpinLead);
                }
            }
        } else
#endif
        {
            while (!quit_ && armed_ && token_ == token
                   && targetRevision_ == targetRevision
                   && std::chrono::steady_clock::now() + kSpinLead < target) {
                cv_.wait_until(lock, target - kSpinLead);
            }
        }
        if (quit_) {
            break;
        }
        if (!armed_ || token_ != token || targetRevision_ != targetRevision) {
            continue;   // disarmed, re-armed, or transactionally retargeted while waiting
        }
        // Claim the fire atomically so a late disarm can't race the write.
        armed_ = false;
        const ShotMode mode = releaseMode_;
        const TempoReleaseStyle releaseStyle = releaseStyle_;
        const ControllerState out = releaseOutput_;
        const quint64 routeGeneration = routeGeneration_;
        const LatencyControllerRoute route = route_;
        lock.unlock();
        // Spin the last stretch for sub-ms precision (bounded: <= ~1.2ms of CPU).
        while (std::chrono::steady_clock::now() < target) {
#ifdef Q_OS_WIN
            YieldProcessor();
#else
            std::this_thread::yield();
#endif
        }
        // [CONCURRENCY N2] The claim (armed_=false above) and the ~1.2ms spin ran UNLOCKED. Re-verify
        // under m_ that the fire was not superseded during the spin BEFORE pressing the pad: a
        // last-moment disarm(token) (meter vanished -> clearScheduledFire), a re-arm to a NEW token,
        // or quit_ at shutdown must all abort. The claim set armed_=false, so a plain armed_ check
        // can't see the disarm — aborted_ records that intent explicitly. The spin itself is
        // unchanged, so sub-ms precision is preserved. lock is re-held here and carried to the next
        // wait() on continue.
        {
            QMutexLocker submitLock(&owner_->submitMutex_);
            // Re-check under BOTH locks, with submitMutex_ acquired first. This
            // closes the old gap between the abort check and controller_.submit().
            lock.lock();
            // [ORION_ROLLING_LEASE] Split the authority lapse out of the composite guard so it is
            // OBSERVABLE. The behaviour is identical -- still a `continue`, still fail-closed, the
            // pad is not pressed -- but a token dying because its evidence lease expired now leaves
            // a trace. Previously this was indistinguishable from a disarm or a token swap, and a
            // whole batch of aborts was attributed to the tip predictor as a result.
            const auto submitNowTp = std::chrono::steady_clock::now();
            const bool authorityLapsed = submitNowTp > authorityExpiry_;
            if (quit_ || aborted_ || token_ != token
                || !owner_->automation_.armed() || authorityLapsed) {
                if (authorityLapsed && !quit_ && !aborted_ && token_ == token) {
                    lastAuthorityDeclineToken_.store(token, std::memory_order_relaxed);
                    lastAuthorityDeclineOverdueUs_.store(
                        std::chrono::duration_cast<std::chrono::microseconds>(
                            submitNowTp - authorityExpiry_).count(),
                        std::memory_order_relaxed);
                    authorityDeclines_.fetch_add(1, std::memory_order_release);
                }
                continue;
            }
            const PreciseFireRouteBinding binding{routeGeneration, route};
            const LatencyControllerRoute liveRoute = PreciseFirePolicy::liveControllerRoute(
                owner_->remotePlay_.state() == RemotePlayState::Running,
                owner_->orionInput_.enabled(), owner_->orionInput_.connected(),
                owner_->controller_.isConnected(), owner_->controller_.isDs4Backend());
            if (!owner_->automation_.scheduledFireRouteBindingMatches(
                    routeGeneration, route)
                || !PreciseFirePolicy::routeBindingMatches(
                    binding, liveRoute, routeGeneration, route)) {
                // The deadline was calculated for another route. Revoke before
                // touching either release output, neutral the already-owned
                // representations, and let the GUI recovery gate mint a fresh
                // route generation after physical input returns neutral.
                owner_->automation_.setArmed(false);
                lock.unlock();
                ControllerState neutral;
                neutral.lightbarSet = false;
                QString ignored;
                owner_->controller_.submit(neutral, &ignored);
                const OrionInputPacket last = owner_->orionInput_.lastSent();
                if (directInputRouteCurrentlyOwned(
                        owner_->orionInput_.connected(), owner_->orionInput_.haveSent(),
                        owner_->orionInput_.haveSent() && last.own != 0)) {
                    (void)owner_->orionInput_.sendDetailed(neutral, true, true);
                }
                lock.lock();
                failed_ = true;
                failedToken_ = token;
                failedDetail_ = QStringLiteral(
                    "route_binding_changed generation=%1 bound=%2 live=%3")
                    .arg(routeGeneration)
                    .arg(static_cast<int>(route))
                    .arg(static_cast<int>(liveRoute));
                continue;
            }
            // The mailbox is immutable once armed, but release correctness must
            // be proved at the last possible boundary.  A stale held Button
            // packet (the live seq=2 failure shape) or a packet generated for a
            // different stick mode is a failed release, not a route write.  Keep
            // m_ held while publishing the failure mailbox; neither OrionInput
            // nor ViGEm has been touched and fired_ remains false.
            if (!isValidShotReleaseOutput(out, mode, releaseStyle)) {
                failed_ = true;
                failedToken_ = token;
                failedDetail_ = QStringLiteral(
                    "malformed_release_output mode=%1 square=%2 rs=(%3,%4)")
                    .arg(static_cast<int>(mode))
                    .arg(out.square() ? 1 : 0)
                    .arg(out.rightStickX)
                    .arg(out.rightStickY);
                continue;
            }
            lock.unlock();
            QString error;
            InputRouteWriteResult pipeWrite = InputRouteWriteResult::Failed;
            ControllerState virtualOut;
            virtualOut.lightbarSet = false;
            bool virtualSubmitOk = false;
            OrionInputTransactionTiming pipeTiming;
            PreciseFireDispatchTiming dispatchTiming;
            if (route == LatencyControllerRoute::Pipe) {
                // This is the scheduler timestamp: take it immediately before
                // entering the active route. sendDetailed() includes the
                // bounded local-delivery ACK wait, which is telemetry and must
                // never become release-command time.
                dispatchTiming = timePreciseFireDispatch(
                    [this] { return owner_->automation_.engineNowMs(); },
                    [&] {
                        pipeWrite = owner_->orionInput_.sendDetailed(
                            out, true, true, &pipeTiming);
                    });
                // Keep the inactive ViGEm representation neutral, but keep its
                // submit cost outside both active-route timing samples.
                virtualSubmitOk = owner_->controller_.submit(virtualOut, &error);
            } else {
                virtualOut = out;
                dispatchTiming = timePreciseFireDispatch(
                    [this] { return owner_->automation_.engineNowMs(); },
                    [&] {
                        virtualSubmitOk = owner_->controller_.submit(virtualOut, &error);
                    });
            }
            const PreciseFireDeliveryDecision delivery =
                PreciseFirePolicy::evaluateBoundDelivery(
                    pipeWrite, virtualSubmitOk, route);
            // Stamp the precise release write so the GUI tick coalesces (skips its duplicate
            // hook write within ~3ms) — one hook packet per release, no takion-stalling burst.
            if (delivery.pipeAccepted) {
                owner_->lastFireHookWriteUs_.store(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now().time_since_epoch()).count(),
                    std::memory_order_relaxed);
            }
            const double fireEpochMs = delivery.confirmScheduledFire
                ? captureFireEpochMs(dispatchTiming.commandIssuedMs, [this] {
                      return owner_->automation_.engineNowMs();
                  })
                : -1.0;
            lock.lock();
            if (delivery.confirmScheduledFire) {
                PreciseFireDeliverySnapshot snapshot;
                snapshot.output = delivery.pipeAccepted ? out : virtualOut;
                snapshot.outputValid = true;
                snapshot.routeGeneration = routeGeneration;
                snapshot.route = route;
                snapshot.commandIssuedMs = dispatchTiming.commandIssuedMs;
                snapshot.activeRouteCompleteMs = dispatchTiming.activeRouteCompleteMs;
                snapshot.activeRouteDurationMs = dispatchTiming.activeRouteDurationMs;
                snapshot.fireEpochMs = fireEpochMs;
                if (delivery.pipeAccepted && owner_->orionInput_.haveSent()) {
                    snapshot.pipePacket = owner_->orionInput_.lastSent();
                    snapshot.pipePacketValid = true;
                    if (pipeTiming.matches(snapshot.pipePacket.seq)) {
                        snapshot.pipeTiming = pipeTiming;
                    }
                }
                fired_ = true;
                firedToken_ = token;
                // AutomationEngine's scheduledFireDeltaMs, release marker and
                // learning anchors all consume this value. It is intentionally
                // the pre-dispatch command instant, never ACK completion.
                firedActualMs_ = dispatchTiming.commandIssuedMs;
                firedDeliveryStage_ = delivery.stage;
                firedTransportSeq_ = snapshot.pipePacketValid
                    ? snapshot.pipePacket.seq : 0;
                firedSnapshot_ = snapshot;
            } else {
                failed_ = true;
                failedToken_ = token;
                QString pipeStatus;
                switch (pipeWrite) {
                case InputRouteWriteResult::LocalUdpAccepted:
                    pipeStatus = QStringLiteral("local_udp_accepted");
                    break;
                case InputRouteWriteResult::OwnershipReleased:
                    pipeStatus = QStringLiteral("ownership_released");
                    break;
                case InputRouteWriteResult::Written:
                    pipeStatus = QStringLiteral("written_unacked_not_required");
                    break;
                case InputRouteWriteResult::WrittenUnconfirmed:
                    pipeStatus = QStringLiteral("written_ack_missing");
                    break;
                case InputRouteWriteResult::Unchanged:
                    pipeStatus = QStringLiteral("unchanged");
                    break;
                case InputRouteWriteResult::Failed:
                    pipeStatus = QStringLiteral("failed_before_write");
                    break;
                }
                failedDetail_ = QStringLiteral("route_generation=%1 bound_route=%2 pipe=%3 vigem_ok=%4%5")
                    .arg(routeGeneration)
                    .arg(static_cast<int>(route))
                    .arg(pipeStatus)
                    .arg(virtualSubmitOk ? 1 : 0)
                    .arg(error.isEmpty() ? QString()
                                         : QStringLiteral(" error=%1").arg(error.left(96)));
            }
        }
    }
#ifdef Q_OS_WIN
    // [ORION_TIMER_RES_BATTERY 2026-08-07] Guard the paired call: if we exit the
    // loop while parked we already released; if we exit mid-work we still need
    // to release. releaseTbp() is idempotent.
    releaseTbp();
#endif
}

RemoteFrameProvider::RemoteFrameProvider()
    : QQuickImageProvider(QQuickImageProvider::Image)
{
    QImage fallback(1280, 720, QImage::Format_RGB32);
    fallback.fill(QColor(6, 9, 14));
    snapshots_.reset(0, std::move(fallback));
}

namespace {
qint64 previewSteadyNowNs()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}
}

QImage RemoteFrameProvider::requestImage(const QString& id, QSize* size, const QSize& requestedSize)
{
    QMutexLocker locker(&mutex_);
    // Timestamp after serialization. Async image-provider requests can arrive on
    // worker threads; sampling before the lock allows a later-arriving thread to
    // publish its timestamp first and then have an older timestamp rewind the
    // cadence epoch. The provider service order is the final handoff we gate.
    const qint64 nowNs = previewSteadyNowNs();
    if (lastRequestSteadyNs_ > 0 && nowNs > lastRequestSteadyNs_) {
        maxRequestGapNs_ = std::max(maxRequestGapNs_, nowNs - lastRequestSteadyNs_);
    }
    lastRequestSteadyNs_ = nowNs;
    ++windowImageRequests_;
    bool exact = false;
    QImage image = snapshots_.lookupProviderId(id, &exact);
    if (!exact) {
        ++windowSnapshotMisses_;
    }
    locker.unlock();

    if (size) {
        *size = image.size();
    }
    if (requestedSize.isValid()) {
        return image.scaled(requestedSize, Qt::KeepAspectRatio, Qt::SmoothTransformation);
    }
    return image;
}

void RemoteFrameProvider::setFrame(int serial, const QImage& frame)
{
    // QImage is copy-on-write: share the buffer (atomic refcount bump) instead of a deep copy on every
    // frame. The producer (handleRemoteFrame) and consumer (requestImage) only read it, and any write
    // would detach automatically, so this is safe under mutex_ and removes a per-frame deep copy.
    const qint64 nowNs = previewSteadyNowNs();
    QMutexLocker locker(&mutex_);
    // Do not charge a stopped/idle session's wall time to the first request of
    // the next stream. A live source cadence cannot legitimately leave this
    // setter idle for half a second; source/transport telemetry owns that gap.
    if (lastSetSteadyNs_ > 0 && nowNs - lastSetSteadyNs_ > 500'000'000LL) {
        lastRequestSteadyNs_ = 0;
    }
    lastSetSteadyNs_ = nowNs;
    snapshots_.publish(serial, frame);
    ++windowFramesSet_;
}

void RemoteFrameProvider::resetFrames(int serial, const QImage& fallback)
{
    QMutexLocker locker(&mutex_);
    snapshots_.reset(serial, fallback);
    lastSetSteadyNs_ = previewSteadyNowNs();
    ++windowFramesSet_;
}

RemoteFrameProviderStats RemoteFrameProvider::takeWindowStats()
{
    QMutexLocker locker(&mutex_);
    RemoteFrameProviderStats result;
    result.framesSet = windowFramesSet_;
    result.imageRequests = windowImageRequests_;
    result.snapshotMisses = windowSnapshotMisses_;
    result.maxRequestGapMs = static_cast<double>(maxRequestGapNs_) / 1'000'000.0;
    windowFramesSet_ = 0;
    windowImageRequests_ = 0;
    windowSnapshotMisses_ = 0;
    maxRequestGapNs_ = 0;
    return result;
}

void RemoteFrameProvider::resetStats()
{
    QMutexLocker locker(&mutex_);
    windowFramesSet_ = 0;
    windowImageRequests_ = 0;
    windowSnapshotMisses_ = 0;
    lastSetSteadyNs_ = 0;
    lastRequestSteadyNs_ = 0;
    maxRequestGapNs_ = 0;
}

OrionAppController::OrionAppController(QString rootDir, QObject* parent)
    : QObject(parent),
      rootDir_(std::move(rootDir)),
      iconSource_(QUrl::fromLocalFile(rootDir_ + QStringLiteral("/assets/orion.png")).toString()),
      // [ORION_DATA_DIR] Logs are the ONLY artifact support has when a customer machine
      // misbehaves, and in a production install rootDir_ is the Program Files install dir, which
      // an unelevated account cannot write -- so the diagnostic log silently did not exist for
      // exactly the users who most needed it. Assets on the line above stay with the install.
      // Dev builds resolve orionDataDir() to rootDir_, so logs/ stays where the tooling expects.
      appLogSink_(orionDataDir(rootDir_) + QStringLiteral("/logs/orion_native.log")),
      config_(rootDir_, this),
      security_(rootDir_, this),
      periodicSecurityEvaluator_(rootDir_)
{
#ifdef Q_OS_WIN
    if (auto* app = QCoreApplication::instance()) {
        app->installNativeEventFilter(this);
    }
#endif

    qRegisterMetaType<DetectionResult>();
    qRegisterMetaType<TelemetrySnapshot>();
    qRegisterMetaType<ShotContext>();
    qRegisterMetaType<LicenseResult>();
    qRegisterMetaType<UpdateManifest>();
    qRegisterMetaType<RemotePlayState>();

    // The production controller owns a real, route-dependent output transport.
    // Every autonomous/calibration release must therefore carry an exact route
    // proof even when it fires immediately rather than through the worker.
    automation_.setControllerRouteBindingRequired(true);

    // [RT-MED-09 2026-09-23] Settings save + sign is a crash-safe transaction (see
    // SecurityManager::beginSettingsWrite). Order at startup:
    //   1. Finish or roll back a save that a crash/reboot interrupted, BEFORE load() reads it,
    //      so the customer comes back on the exact old pair or the exact new pair.
    //   2. If the pair verifies, load() inside a transaction: a settings_version migration
    //      rewrites settings.json from VERIFIED content, and used to leave it unsigned (a lock
    //      after every update that bumped kSettingsVersion). It is re-signed here.
    //   3. One-time bootstrap only while this profile never held a verified pair (no signature
    //      AND no signed-once marker). Deleting settings.json.sig later no longer gets whatever
    //      is on disk signed at the next start; the customer uses Repair settings instead.
    {
        QString recoveryDetail;
        const auto recovery = security_.recoverInterruptedSettingsWrite(&recoveryDetail);
        if (recovery == SecurityManager::SettingsWriteRecovery::RolledBack) {
            appendLog(QStringLiteral("Venice restored your last saved settings after an interrupted save."));
        }
        if (recovery != SecurityManager::SettingsWriteRecovery::NoJournal) {
            appendLog(QStringLiteral("Settings engine detail: %1").arg(recoveryDetail));
        }
    }
    const bool settingsPairValidBeforeLoad = security_.verifySettingsSignature();
    if (settingsPairValidBeforeLoad) {
        QString gateDetail;
        (void)security_.beginSettingsWrite(&gateDetail);
        if (!gateDetail.isEmpty()) {
            appendLog(QStringLiteral("Settings engine detail: %1").arg(gateDetail));
        }
    }
    config_.load();
    if (settingsPairValidBeforeLoad) {
        const bool rewrittenByLoad = !security_.verifySettingsSignature();
        QString commitErr;
        if (!security_.commitSettingsWrite(&commitErr)) {
            appendLog(QStringLiteral("Settings engine detail: re-sign after load failed: %1").arg(commitErr));
        } else if (rewrittenByLoad) {
            appendLog(QStringLiteral("Settings engine detail: settings upgraded for this version and re-signed"));
        }
    } else if (security_.settingsBootstrapAllowed()) {
        // First-ever launch on this profile. In a production build an unsigned settings.json
        // that is already on disk is NOT signed as-is: bootstrap writes defaults (the only
        // content this start can vouch for) and keeps the unsigned file aside for support.
        AppConfigData bootData = config_.data();
        if (security_.releaseManifestRequired()
            && QFile::exists(orionDataDir(rootDir_) + QStringLiteral("/settings.json"))) {
            (void)security_.preserveRejectedSettings(nullptr);
            AppConfig defaults(rootDir_);
            bootData = defaults.data();
        }
        QString bootErr;
        if (config_.save(bootData, &bootErr) && security_.commitSettingsWrite(&bootErr)) {
            appendLog(QStringLiteral("First-run: signed default settings"));
        } else {
            appendLog(QStringLiteral("First-run: failed to seed settings signature: %1")
                          .arg(bootErr));
        }
    }

    remotePlay_.setRootDir(rootDir_);
    // Autonomous tip-vision is the PRODUCTION DEFAULT and settings.autonomousVision defaults OFF, so the
    // ORION_AUTONOMOUS_VISION env force MUST be set before this first syncBackendConfig() (unlike
    // ORION_FREEZE_CAL, whose settings default is already ON). Same =0 opt-out semantics as the
    // envDefaultOn block below, which re-affirms + logs it. Keeps a stale settings.json from dropping the
    // launch build back to the per-type path.
    {
        const QString v = qEnvironmentVariable("ORION_AUTONOMOUS_VISION").trimmed().toLower();
        if (v == QLatin1String("0") || v == QLatin1String("false")
            || v == QLatin1String("no") || v == QLatin1String("off")) {
            qunsetenv("ORION_AUTONOMOUS_VISION");
        } else {
            qputenv("ORION_AUTONOMOUS_VISION", "1");
        }
    }
    syncBackendConfig();
    // Dev guard for the update gate: a launch from a CMake build tree must never
    // auto-apply an update (the updater would overwrite a source checkout).
    // Detected by CMakeCache.txt in the exe dir or up to three parents
    // (build/Release/OrionNative.exe -> build/CMakeCache.txt).
    // Production builds compile this out entirely: a planted CMakeCache.txt
    // must not let a user dodge a mandatory update.
#ifndef ORION_PRODUCTION_BUILD
    {
        QDir probe(QCoreApplication::applicationDirPath());
        for (int depth = 0; depth < 4 && !devBuild_; ++depth) {
            devBuild_ = QFileInfo::exists(probe.filePath(QStringLiteral("CMakeCache.txt")));
            if (!probe.cdUp()) {
                break;
            }
        }
    }
#endif
    // PRODUCTION DEFAULT: the pre-encryption input hook AND the decoded-frame
    // export are ON (the bundled OrionStream build ships both pipes; this is the
    // live-confirmed lowest-latency config). ORION_INPUT_HOOK=0 disables the input
    // hook; ORION_FRAME_PIPE=0 disables decoded export only in development or an
    // explicit capture-card launch. Production no-card Remote Play overrides that
    // developer flag below and fails closed on the pipe. ViGEm is still submitted
    // every tick as the live input fallback either way.
    const auto envDefaultOn = [](const char* name) {
        const QString v = qEnvironmentVariable(name).trimmed().toLower();
        if (v == QLatin1String("0") || v == QLatin1String("false")
            || v == QLatin1String("no") || v == QLatin1String("off")) {
            qunsetenv(name);
            return false;
        }
        qputenv(name, "1");
        return true;
    };
    // Input hook DEFAULT ON (opt out with ORION_INPUT_HOOK=0). The pre-encryption injection USED to
    // stall chiaki's takion/feedback thread during a shot -> stream lag; that root cause is now fixed
    // reader-side (HIGHEST-priority reader thread + a latest-wins _nowait slot that shares no lock with
    // takion and never force-wakes the send) and the hook is live-confirmed, so it is the default
    // low-latency path. envDefaultOn ALSO qputenv's "1" so the child chiaki opens its input pipe
    // (remote_play_client.py keys on the env). ViGEm stays the always-submitted fallback, so a missing
    // pipe / stock chiaki degrades cleanly to ViGEm.
    if (envDefaultOn("ORION_INPUT_HOOK")) {
        orionInput_.setEnabled(!isXboxRemotePlay(config_.data()));
        appendLog(QStringLiteral("Input hook ENABLED (pre-encryption pipe; ViGEm fallback active)"));
    } else {
        appendLog(QStringLiteral("Input hook disabled by ORION_INPUT_HOOK=0 (ViGEm only)"));
    }
    squareOutputWatchdogEnabled_ =
        qEnvironmentVariableIntValue("ORION_SQUARE_OUTPUT_WATCHDOG") == 1;
    appendLog(QStringLiteral("SQUARE OUTPUT WATCHDOG: enabled=%1 timeout_ms=1500 "
                             "copies=2 abort_drain_owned=1 knob=ORION_SQUARE_OUTPUT_WATCHDOG default=0")
                  .arg(squareOutputWatchdogEnabled_ ? 1 : 0));
    const bool captureCardVideoSource =
        config_.data().videoSource.compare(QLatin1String("capture_card"),
                                          Qt::CaseInsensitive) == 0;
#ifdef ORION_PRODUCTION_BUILD
    constexpr bool productionBuild = true;
#else
    constexpr bool productionBuild = false;
#endif
    if (shouldRequireRemotePlayFramePipe(productionBuild, captureCardVideoSource)) {
        // Production no-card automation has exactly one authoritative video
        // source. A stale developer ORION_FRAME_PIPE=0 cannot restore the
        // occlusion-prone GDI/window path.
        qputenv("ORION_FRAME_PIPE", "1");
        appendLog(QStringLiteral("Decoded-frame pipe REQUIRED (production Remote Play; window fallback disabled)"));
    } else if (envDefaultOn("ORION_FRAME_PIPE")) {
        appendLog(QStringLiteral("Decoded-frame pipe ENABLED (OrionStream frame export feeds the detector)"));
    } else {
        appendLog(QStringLiteral("Decoded-frame pipe disabled by ORION_FRAME_PIPE=0 (window capture)"));
    }
    // Grader REMOVED: force the calibration freeze on. The post-release meter self-grade mis-grades
    // early/late at the dead-top (peaks ~100% then recedes for BOTH), so the engine times OPEN-LOOP
    // off the live meter instead. ORION_FREEZE_CAL=0 re-enables learning (dev only).
    if (envDefaultOn("ORION_FREEZE_CAL")) {
        appendLog(QStringLiteral("Calibration grader FROZEN (open-loop; meter self-grade removed)"));
    } else {
        appendLog(QStringLiteral("Calibration grader UNFROZEN by ORION_FREEZE_CAL=0 (dev self-grade learning)"));
    }
    // Session statistics use the independent colour-window grader. It observes the native
    // release marker and never calls learnFromOutcome, so enabling it does not unfreeze or
    // perturb production timing. ORION_GREEN_SELF_GRADE=0 is a diagnostic opt-out.
    if (envDefaultOn("ORION_GREEN_SELF_GRADE")) {
        appendLog(QStringLiteral("Color-window session grader ENABLED (telemetry only; timing learner unchanged)"));
    } else {
        appendLog(QStringLiteral("Color-window session grader disabled by ORION_GREEN_SELF_GRADE=0"));
    }
    // PRODUCTION DEFAULT: autonomous tip-vision timing — target the meter TIP, fire near the tip, and
    // ride ONE self-measured global lead (no user calibration, no hardcoded timing). Forced on like
    // ORION_FREEZE_CAL so a stale settings.json can't drop back to the per-type path; the engine's
    // applyConfig keys on the env being SET (envDefaultOn qputenv's "1"; =0 qunsets it -> per-type).
    if (envDefaultOn("ORION_AUTONOMOUS_VISION")) {
        appendLog(QStringLiteral("Autonomous tip-vision timing ENABLED (tip target + self-measured global lead)"));
    } else {
        appendLog(QStringLiteral("Autonomous tip-vision DISABLED by ORION_AUTONOMOUS_VISION=0 (per-type fallback path)"));
    }
    qmlRenderMode_ = envDefaultOn("ORION_QML_RENDER");
    if (qmlRenderMode_) {
        appendLog(QStringLiteral("QML frame render ENABLED (decoder-pipe preview shown in QML; chiaki window parked off-screen — no in-panel embed, overlay draws on top)"));
    } else {
        appendLog(QStringLiteral("QML frame render disabled by ORION_QML_RENDER=0 (legacy in-panel HWND embed)"));
    }
    // Preview-only smoothness policy: keep the image-provider pull and texture
    // preparation off the GUI thread by default.  The old synchronous default
    // made a full-resolution image:// refresh compete with stdout dispatch,
    // controller polling, and QML layout every source frame.  This never changes
    // detector or automation pixels; ORION_PREVIEW_ASYNC=0 remains a diagnostic
    // escape hatch for a Qt/driver-specific renderer problem.
    previewAsync_ = envDefaultOn("ORION_PREVIEW_ASYNC");
    if (previewAsync_) {
        appendLog(QStringLiteral("Preview async ENABLED (worker-thread image-provider pull; ORION_PREVIEW_ASYNC=0 disables)"));
    } else {
        appendLog(QStringLiteral("Preview async disabled by ORION_PREVIEW_ASYNC=0 (diagnostic synchronous renderer)"));
    }
#ifndef ORION_PRODUCTION_BUILD
    // Keep the developer signing hook out of the production image entirely.
    // A runtime `localDevAllowed() == false` check is not sufficient containment:
    // it still leaves an environment-controlled hook and marker in the binary.
    if (qEnvironmentVariableIsSet("ORION_AUTO_SIGN_SETTINGS") && localDevAllowed()) {
        QString signatureError;
        security_.writeSettingsSignature(&signatureError);
    }
#endif
    // [ORION_USER_LOG] (fix 5b, default OFF) SEPARATE human-readable log -> logs/orion_user.log
    // (local timestamps, plain-language lines). Strictly additive: with the env unset nothing is
    // buffered or written and logs/orion_native.log stays byte-identical.
    userLog_.setEnabled(UserFacingLog::envEnabled());
    if (userLog_.enabled()) {
        appendLog(QStringLiteral("User log ENABLED (ORION_USER_LOG): plain-language events -> logs/orion_user.log"));
        userLog_.append(QStringLiteral("Venice started. This log shows the important events in plain language."));
    }

    // Debug-only controller isolation harness. When ORION_FORCE_VIRTUAL_NEUTRAL is
    // truthy, pollPhysicalController submits a neutral pad to ViGEm every tick (see
    // forceVirtualNeutral_). Not for normal play; compiled out of production.
#ifndef ORION_PRODUCTION_BUILD
    {
        const QString v = qEnvironmentVariable("ORION_FORCE_VIRTUAL_NEUTRAL").trimmed().toLower();
        forceVirtualNeutral_ = (v == QLatin1String("1") || v == QLatin1String("true")
                                || v == QLatin1String("yes") || v == QLatin1String("on"));
    }
#endif

    // [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] Two knobs, read once at startup so
    // the native event filter stays a pure read on the hot message path:
    //   ORION_CONTROLLER_UI_PASSTHROUGH=1        -> disable the isolation entirely
    //     (deliberate controller-driven UI, or the owner's way back to the
    //     pre-2026-09-19 behaviour if a mapper turns out to report as hardware).
    //   ORION_CONTROLLER_UI_INJECTED_ISOLATION=0 -> keep the timing guards but stop
    //     trusting GetCurrentInputMessageSource's injected verdict.
    controllerUiPassthrough_ =
        qEnvironmentVariableIntValue("ORION_CONTROLLER_UI_PASSTHROUGH") == 1;
    controllerUiInjectedIsolation_ =
        !qEnvironmentVariableIsSet("ORION_CONTROLLER_UI_INJECTED_ISOLATION")
        || qEnvironmentVariableIntValue("ORION_CONTROLLER_UI_INJECTED_ISOLATION") != 0;
    if (controllerUiPassthrough_) {
        appendLog(QStringLiteral(
            "Controller UI isolation DISABLED (ORION_CONTROLLER_UI_PASSTHROUGH=1): "
            "mapped pad input may activate Venice controls during a session."));
    }

    serverState_ = licenseClient_.serverUrl().host();
    sessionStartMs_ = 0;  // session clock starts when live capture goes Running (see stateChanged)

    // CRIT-1 lease-gated fire (docs/SECURITY_REDTEAM.md). Shipping builds force
    // this on at compile time; development builds may opt in with
    // ORION_LEASE_GATED_FIRE while exercising offline fixtures.
    leaseGate_.setEnabled(LeaseGate::enabledFromEnvironment());
    if (leaseGate_.enabled()) {
        appendLog(QStringLiteral("Lease-gated fire ENABLED (ORION_LEASE_GATED_FIRE): automation requires a live server lease (max staleness %1 min).")
                      .arg(leaseGate_.maxStalenessMs() / 60000));
    }

#ifdef Q_OS_WIN
    // [ORION_PRECISE_WAIT] Default-on OS-timing guard for the fire path (evidence in
    // PreciseWaitTimer.h). ORION_TIMER_RES_GUARD=0 disables the guard; otherwise the process
    // opts out of Windows 11 timer-resolution throttling so the fire thread's timeBeginPeriod(1) stays honored
    // while the window is occluded/minimized. Wake-time-only: no submit is created, moved later,
    // or retried by this flag.
    // [ORION_METER_DELAY 2026-08-07] Default ON. Opt out with ORION_TIMER_RES_GUARD=0.
    // [ORION_TIMER_RES_BATTERY 2026-08-07] Skip the guard on battery power unless a
    // Remote Play session is already running. Holding timer resolution disabled on
    // DC costs 15-25% battery for a benefit (shot precision under occlusion) that
    // doesn't apply until a session actually runs. Env-var opt-out unchanged.
    if (!(qEnvironmentVariableIsSet("ORION_TIMER_RES_GUARD")
          && qEnvironmentVariableIntValue("ORION_TIMER_RES_GUARD") == 0)) {
        SYSTEM_POWER_STATUS pw{};
        // ACLineStatus: 0 = offline (battery), 1 = online (AC), 255 = unknown.
        // On failure or "unknown", treat as AC so we don't accidentally strand a
        // desktop-with-UPS user with coarse timer resolution.
        const bool onAc = GetSystemPowerStatus(&pw)
            ? (pw.ACLineStatus == 1 || pw.ACLineStatus == 255)
            : true;
        const bool sessionRunning =
            remotePlay_.state() == RemotePlayState::Running;
        if (onAc || sessionRunning) {
            const bool timerGuardOk = orion::disableTimerResolutionThrottling();
            timerResolutionThrottleOptOutApplied_ = timerGuardOk;
            appendLog(QStringLiteral(
                "Timer-resolution throttle opt-out (ORION_TIMER_RES_GUARD): %1 "
                "(kernel timer resolution now %2 ms)")
                          .arg(timerGuardOk ? QStringLiteral("applied") : QStringLiteral("FAILED"))
                          .arg(orion::currentTimerResolutionMs(), 0, 'f', 3));
        } else {
            appendLog(QStringLiteral(
                "Timer-resolution throttle opt-out DEFERRED (on battery, no live "
                "Remote Play session). Plug in AC or start Remote Play to activate."));
        }
    }
#endif
    // Sub-tick release scheduler: the precise fire thread is always running (parked on its
    // condition variable when idle); the engine arms it per shot via scheduledFireDeadlineMs.
    fireThread_ = new OrionPreciseFireThread(this);
    if (fireThread_->hiresWaitActive()) {
        appendLog(QStringLiteral(
            "Precise-fire hires wait ENABLED (ORION_PRECISE_WAIT_HIRES): fire wakeups ride a "
            "high-resolution waitable timer (immune to timer-resolution coarsening)."));
    }
    connect(&automation_, &AutomationEngine::visionScheduleInvalidating, this,
            [this](quint64 token) { fencePreciseFireToken(token, false); },
            Qt::DirectConnection);
    connect(&automation_, &AutomationEngine::scheduledFireFallbackInvalidating, this,
            [this](quint64 token) { fencePreciseFireToken(token, true); },
            Qt::DirectConnection);

    const auto recordSecurityEvent = [this](const QString& event, const QString& detail) {
        lastSecurityAuditEvent_ = QStringLiteral("%1: %2").arg(event, detail);
        appendLog(QStringLiteral("Security: %1 - %2").arg(event, detail.left(160)));
        emit statusChanged();
    };
    connect(&security_, &SecurityManager::securityEvent, this, recordSecurityEvent);
    connect(&periodicSecurityEvaluator_, &PeriodicSecurityEvaluator::securityEvent,
            this, recordSecurityEvent);
    connect(&periodicSecurityEvaluator_, &PeriodicSecurityEvaluator::evaluationFinished,
            this, [this](quint64 generation, const SecurityStatus& status) {
        if (!securityEvaluationFence_.accepts(generation)) {
            return;
        }
        applySecurityStatus(status);
    });
    connect(&periodicSecurityEvaluator_, &PeriodicSecurityEvaluator::evaluationFailed,
            this, [this](quint64 generation, const QString& detail) {
        if (!securityEvaluationFence_.accepts(generation)) {
            return;
        }
        SecurityStatus locked = failClosedSecurityEvaluationStatus(detail);
        locked.entitlementState = entitlementState_;
        locked.integrityState = integrityState_;
        applySecurityStatus(locked);
        appendLog(QStringLiteral("Security evaluator failure (automation locked): %1")
                      .arg(detail.left(160)));
    });

    connect(&licenseClient_, &LicenseClient::activationFinished, this, [this](const LicenseResult& result) {
        authBusy_ = false;
        if (result.ok && authLicenseKey_.startsWith(QStringLiteral("PAIR-"))) {
            static const QRegularExpression canonicalKey(
                QStringLiteral("^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$"));
            if (!canonicalKey.match(result.canonicalLicenseKey).hasMatch()) {
                if (authenticated_) {
                    disconnectRemotePlay(true);
                }
                authenticated_ = false;
                leaseGate_.recordHeartbeatKill();
                authToken_.clear();
                authTokenId_.clear();
                authTokenExpires_ = 0;
                licenseHeartbeatTimer_.stop();
                licenseState_ = QStringLiteral("Locked");
                authMessage_ = QStringLiteral("Discord sign-in did not return a valid code. Get a fresh one-time code from zaeorion.com/connect and try again.");
                updateSecurityStatus();
                emit authChanged();
                emit statusChanged();
                return;
            }
            authLicenseKey_ = result.canonicalLicenseKey;
        }
        if (result.ok) {
            authenticated_ = true;
            currentPage_ = QStringLiteral("remotePlay");
            licenseState_ = result.plan.isEmpty() ? QStringLiteral("Verified") : result.plan;
            // Always the curated line: the server's success text is not customer copy and
            // must not render unclassified in AuthGate (Astra, bug sweep). Keep it in the log.
            if (!result.message.isEmpty())
                appendLog(QStringLiteral("License server message: %1").arg(result.message));
            authMessage_ = QStringLiteral("Verified. Opening Venice.");
            appendLog(QStringLiteral("License verified for %1").arg(result.user.isEmpty() ? QStringLiteral("current device") : result.user));
            // Profile page data rides the activation verdict (and every heartbeat
            // after it). Absent on the current live Lambda -> no-op.
            applyLicenseProfile(result.profile);
            // CRIT-1: keep the server session token client-side (it was discarded
            // before — only a token_present bool survived) and seed the fire
            // lease. The /api/license/check heartbeat is the authoritative lease
            // from here on; activation only opens the first heartbeat window.
            authToken_ = result.token;
            authTokenId_ = result.tokenId;
            authTokenExpires_ = result.tokenExpiresEpochS;
            leaseGate_.recordActivation(result.tokenExpiresEpochS);
            if (leaseGate_.enabled()) {
                appendLog(QStringLiteral("Fire lease seeded by activation: %1").arg(leaseGate_.stateText()));
            }
            QJsonObject entitlement;
            entitlement.insert(QStringLiteral("user"), result.user);
            entitlement.insert(QStringLiteral("plan"), licenseState_);
            entitlement.insert(QStringLiteral("token_present"), !result.token.isEmpty());
            entitlement.insert(QStringLiteral("token_id"), authTokenId_);
            entitlement.insert(QStringLiteral("token_expires"), double(authTokenExpires_));
            entitlement.insert(QStringLiteral("client_version"), appVersion());
            entitlement.insert(QStringLiteral("license_state"), licenseState_);
            QString cacheError;
            if (security_.cacheLocalEntitlement(entitlement, &cacheError)) {
                entitlementState_ = security_.entitlementState();
            } else {
                entitlementState_ = QStringLiteral("Entitlement cache failed: %1").arg(cacheError);
                appendLog(entitlementState_);
            }
            emit navigationChanged();
            // [CL2-P8-002 2026-09-23] Fresh session: clean retry ladder, normal cadence.
            applyHeartbeatAction(heartbeatCoordinator_.onSessionStarted());
            // Pre-warm the capture-card preview the instant auth succeeds, so the sidecar +
            // Elgato open (~2-4s) overlaps the remaining gate screens and the Live Capture
            // card is already live when RemotePlayPage mounts — instead of a black panel for
            // a few seconds. Fully guarded inside (capture-card source only, no-op if a
            // session/preview is already up); RemotePlayPage's own onCompleted call stays as
            // the idempotent fallback.
            startCapturePreview();
        } else {
            // An activation failure is never allowed to leave authority from an
            // earlier attempt resident in memory.
            if (authenticated_) {
                disconnectRemotePlay(true);
            }
            authenticated_ = false;
            leaseGate_.recordHeartbeatKill();
            authToken_.clear();
            authTokenId_.clear();
            authTokenExpires_ = 0;
            licenseState_ = QStringLiteral("Locked");
            // Contract §5: /api/activate answers with the same CODES as the heartbeat;
            // frozen / blacklisted / version_blocked get their own copy here too.
            authMessage_ = licenseErrorUserText(result, QStringLiteral("License verification failed."));
            // [RT-MED-10 / CL3-F8-008 2026-09-23] The service pause (global kill switch) is
            // not a network fault; activation sends it as "service_disabled: <reason>".
            if (result.error.startsWith(QLatin1String("service_disabled"))
                || result.message.startsWith(QLatin1String("service_disabled"))) {
                authMessage_ = QStringLiteral("Venice is paused by the service right now. Nothing is wrong with your PC or internet.");
            }
            appendLog(QStringLiteral("License activation failed: %1").arg(authMessage_));
        }
        // License authority/cache mutation is a synchronous checkpoint. This
        // also invalidates any periodic result that began before the mutation.
        updateSecurityStatus();
        refreshLeaseNotice();   // [CL2-P8-002 2026-09-23]
        emit authChanged();
        emit statusChanged();
    });

    connect(&licenseClient_, &LicenseClient::validationFinished, this, [this](const LicenseResult& result) {
        // MOTD rides the heartbeat (contract §5). Only a STRUCTURED server response
        // (ok, or ok:false with an error code) may set or clear it — a transport
        // failure leaves error empty and must keep the last notice on screen.
        if (result.ok || !result.error.isEmpty()) {
            applyServerMotd(result.motd);
        }
        if (result.ok) {
            if (!result.plan.isEmpty()) {
                licenseState_ = result.plan;
            }
            // Keep the Profile page's days-left / reset allowance fresh without a
            // second round trip. Tolerant: no `profile` in the body -> no change.
            applyLicenseProfile(result.profile);
            // CRIT-1: a successful heartbeat is the authoritative fire lease.
            // Once the backend signs the lease (07-15) the embedded verify key
            // makes a valid Ed25519 signature MANDATORY for a refresh; until
            // then expiry + server verdict + monotonic max-staleness gate fire.
            const QByteArray leaseKey = LeaseGate::leaseVerifyPublicKey();
            if (!leaseKey.isEmpty()
                && !LeaseGate::verifyLeaseSignature(leaseKey, authLicenseKey_, security_.machineId(),
                                                    result.leaseExpiresAtEpochS, result.leaseSig)) {
                appendLog(QStringLiteral("Lease heartbeat: lease signature INVALID — lease not refreshed."));
                // [CL2-P8-002 2026-09-23] Treated as a failed refresh: retry on the
                // backoff ladder. The lease is NOT touched and still fails closed.
                applyHeartbeatAction(heartbeatCoordinator_.onResult(HeartbeatOutcome::Failure));
                refreshLeaseNotice();
                return;   // stale lease will fail closed via max-staleness
            }
            leaseGate_.recordHeartbeatOk(result.leaseExpiresAtEpochS);
            // [CL2-P8-002 2026-09-23] Recovered: back to the normal cadence and clear
            // the "Reconnecting" notice now instead of on the next notice tick.
            // Engineering log only (not the customer Activity ring).
            if (heartbeatCoordinator_.consecutiveFailures() > 0) {
                appendLog(heartbeatRecoveredLogLine(heartbeatCoordinator_.consecutiveFailures(),
                                                    leaseGate_.stateText()));
            }
            applyHeartbeatAction(heartbeatCoordinator_.onResult(
                HeartbeatOutcome::Ok, leaseGate_.fireAllowed()));
            refreshLeaseNotice();
            return;
        }
        // Fail-soft: only an EXPLICIT server kill code disables a running session.
        // A transport failure leaves result.error empty -> keep running (a network
        // blip must never lock out a paying user mid-session). (With the lease
        // gate enabled, a sustained outage still fails closed on max-staleness.)
        const QString& e = result.error;
        // Contract §5 revoke-fast codes: service_disabled | revoked | expired |
        // device_mismatch | invalid_key | inactive | frozen | blacklisted |
        // version_blocked (isLicenseKillCode, LicenseClient.h). Exact CODE match —
        // prose or an unknown string is never a kill.
        const bool kill = isLicenseKillCode(e);
        if (!kill) {
            // [CL2-P8-002 2026-09-23] Transport failure (e empty) or a non-kill code
            // (rate_limited, server error): retry at 15 s / 30 s / 60 s, then the
            // normal 5-min cadence, instead of waiting a full 5 min. rate_limited
            // jumps straight to the 60 s rung. Retries only REQUEST a refresh; the
            // lease is untouched and still fails closed on expiry/max-staleness.
            // [round 2] If an event queued a re-run behind this request, the
            // coordinator sends it now instead (the stale failure is not counted).
            const HeartbeatAction next = heartbeatCoordinator_.onResult(
                e == QLatin1String("rate_limited") ? HeartbeatOutcome::RateLimited
                                                   : HeartbeatOutcome::Failure);
            // Engineering log only: a Wi-Fi blip must not spam the customer feed.
            appendLog(heartbeatRetryLogLine(e, next.sendNow ? 0 : heartbeatCoordinator_.nextDelayMs(),
                                            leaseGate_.stateText()));
            applyHeartbeatAction(next);
            refreshLeaseNotice();
            return;
        }
        appendLog(QStringLiteral("License heartbeat: session disabled (%1)").arg(e));
        applyHeartbeatAction(heartbeatCoordinator_.onResult(HeartbeatOutcome::Kill));   // [CL2-P8-002] stops the timer
        licenseHeartbeatTimer_.stop();
        disconnectRemotePlay(true);   // stop the bot/capture immediately
        authenticated_ = false;
        leaseGate_.recordHeartbeatKill();   // CRIT-1 revoke-fast: drop the fire lease NOW
        authToken_.clear();
        authTokenId_.clear();
        authTokenExpires_ = 0;
        licenseState_ = QStringLiteral("Locked");
        // frozen / blacklisted / version_blocked (+ min_client_version) get their own
        // copy; the other codes show the server's `message` prose as before.
        authMessage_ = licenseErrorUserText(
            result, QStringLiteral("Venice has been disabled. Check the Discord for updates."));
        if (e == QLatin1String("service_disabled")) {
            // [RT-MED-10 / CL3-F8-008 2026-09-23] Mid-session service pause: one plain line;
            // the server's reason stays in the engineering line below.
            authMessage_ = QStringLiteral("Venice is paused by the service right now. Nothing is wrong with your PC or internet.");
        }
        // [CL2-P8-006/008 2026-09-23] authMessage_ may now be mapped customer copy;
        // keep the raw server code in the engineering log for support (the
        // "session disabled" line above is the customer event).
        appendLog(QStringLiteral("Lease heartbeat: server code %1 shown to the customer as: %2").arg(e, authMessage_));
        refreshLeaseNotice();   // [CL2-P8-002 2026-09-23] signed out -> no lease notice
        emit authChanged();
        emit navigationChanged();
        emit statusChanged();
    });
    licenseHeartbeatTimer_.setInterval(LicenseHeartbeatBackoff::kNormalIntervalMs);   // 5-min server re-check
    connect(&licenseHeartbeatTimer_, &QTimer::timeout, this, [this]() {
        if (authenticated_ && !authLicenseKey_.isEmpty()) {
            licenseClient_.validate(authLicenseKey_, security_.machineId());
        }
    });
    // [CL2-P8-002 2026-09-23] Event-driven recovery + visible lease state.
    //  * Network return: QNetworkInformation (Windows Network List Manager
    //    backend, shipped in networkinformation/) -> Online triggers an
    //    immediate heartbeat. Missing backend = logged, no-op (the retry ladder
    //    and the resume hook in nativeEventFilter still recover).
    //  * leaseNoticeTimer_ re-evaluates the lease every 5 s because staleness /
    //    expiry is time-driven: nothing emits when the lease quietly lapses.
    heartbeatMonotonic_.start();
    if (QNetworkInformation::loadBackendByFeatures(QNetworkInformation::Feature::Reachability)) {
        if (auto* netInfo = QNetworkInformation::instance()) {
            connect(netInfo, &QNetworkInformation::reachabilityChanged, this,
                    [this](QNetworkInformation::Reachability reachability) {
                if (reachability == QNetworkInformation::Reachability::Online) {
                    requestImmediateLicenseHeartbeat(QStringLiteral("network back online"));
                }
            });
        }
    } else {
        appendLog(QStringLiteral("Lease heartbeat: network-reachability backend unavailable; "
                                 "recovery relies on the retry ladder and resume events."));
    }
    leaseNoticeTimer_.setInterval(5'000);
    connect(&leaseNoticeTimer_, &QTimer::timeout, this, [this]() { refreshLeaseNotice(); });
    leaseNoticeTimer_.start();
    // MOTD `until` expiry: re-evaluate the stored notice when its deadline passes so
    // the banner hides between heartbeats instead of lingering up to 5 minutes.
    motdExpiryTimer_.setSingleShot(true);
    connect(&motdExpiryTimer_, &QTimer::timeout, this, [this]() { applyServerMotd(motd_); });

    connect(&licenseClient_, &LicenseClient::versionCheckFinished, this, [this](const VersionResult& result) {
        if (!result.ok) {
            updateState_ = QStringLiteral("API offline");
            appendLog(QStringLiteral("Backend version check unavailable: %1")
                          .arg(result.message.isEmpty() ? QStringLiteral("unknown error") : result.message));
            emit statusChanged();
            return;
        }

        // Contract §5: /api/version carries the MOTD as well as the heartbeat.
        applyServerMotd(result.motd);

        const int versionCompare = compareSemanticVersions(appVersion(), result.version);
        updateState_ = versionCompare < 0
            ? QStringLiteral("Update %1").arg(result.version)
            : QStringLiteral("Current %1").arg(appVersion());
        appendLog(QStringLiteral("Backend version: api=%1 env=%2 server=%3 local=%4 state=%5")
                      .arg(result.api,
                           result.env.isEmpty() ? QStringLiteral("unknown") : result.env,
                           result.version,
                           appVersion(),
                           updateState_));
        emit statusChanged();
    });

    connect(&licenseClient_, &LicenseClient::updateCheckFinished, this, [this](const UpdateManifest& manifest) {
        if (!manifest.ok) {
            // /api/update is unreachable or not yet deployed. Fail soft: never
            // block the app, clear any update flags, and fall back to the backend
            // health check so the Patch pill still reflects connectivity.
            appendLog(QStringLiteral("Update endpoint unavailable: %1")
                          .arg(manifest.message.isEmpty() ? QStringLiteral("unknown error") : manifest.message));
            latestVersion_.clear();
            updateNotes_.clear();
            updateAvailable_ = false;
            updateMandatory_ = false;
            updateBlocked_ = false;
            // Fail-soft gate rule: offline / unreachable NEVER bricks the app —
            // proceed on the current version (the pill shows connectivity).
            resolveUpdateGate(UpdateGateAction::Proceed);
            licenseClient_.checkVersion();
            emit statusChanged();
            return;
        }

        latestVersion_ = manifest.version;
        updateNotes_ = manifest.notes;
        updateAvailable_ = false;
        updateMandatory_ = false;
        updateBlocked_ = false;

        if (!manifest.channel.isEmpty() && manifest.channel != updateChannel()) {
            appendLog(QStringLiteral("Update manifest channel mismatch: requested=%1 served=%2")
                          .arg(updateChannel(), manifest.channel));
        }

        const UpdateDecision decision = evaluateUpdate(appVersion(), manifest);
        switch (decision) {
        case UpdateDecision::UpToDate:
        case UpdateDecision::DowngradeBlocked:
            updateState_ = QStringLiteral("Current %1").arg(appVersion());
            break;
        case UpdateDecision::UpdateAvailable:
            updateState_ = QStringLiteral("Update %1").arg(manifest.version);
            updateAvailable_ = true;
            break;
        case UpdateDecision::MandatoryUpdate:
            updateState_ = QStringLiteral("Update required %1").arg(manifest.version);
            updateAvailable_ = true;
            updateMandatory_ = true;
            break;
        case UpdateDecision::ClientBlocked:
            // current < minimum_supported_version: this is the only state allowed
            // to hard-block (per spec). The UI surfaces a required-update gate.
            updateState_ = QStringLiteral("Update required");
            updateAvailable_ = true;
            updateMandatory_ = true;
            updateBlocked_ = true;
            break;
        case UpdateDecision::InvalidManifest:
            resolveUpdateGate(UpdateGateAction::Proceed);
            licenseClient_.checkVersion();
            emit statusChanged();
            return;
        }

        resolveUpdateGate(evaluateUpdateGate(decision, devBuild_, updaterPresent()));

        appendLog(QStringLiteral("Update check: latest=%1 local=%2 channel=%3 state=%4 mandatory=%5 blocked=%6 gate=%7")
                      .arg(manifest.version, appVersion(), updateChannel(), updateState_,
                           updateMandatory_ ? QStringLiteral("yes") : QStringLiteral("no"),
                           updateBlocked_ ? QStringLiteral("yes") : QStringLiteral("no"),
                           updateGatePhase_));
        emit statusChanged();
        // Silent updater: apply now if available and the session is idle.
        maybeAutoApplyUpdate();
    });

    QTimer::singleShot(250, this, [this]() {
        updateState_ = QStringLiteral("Checking");
        emit statusChanged();
        licenseClient_.checkUpdate(appVersion(), updateChannel());
    });
    // Gate watchdog: the request carries a 15s transfer timeout, so the finished
    // handler always resolves the gate eventually — but if anything keeps the
    // check from completing, never strand the user on the "checking" splash.
    QTimer::singleShot(20'000, this, [this]() {
        if (!updateGateResolved_) {
            appendLog(QStringLiteral("Update gate: check did not resolve in time; proceeding on current version."));
            resolveUpdateGate(UpdateGateAction::Proceed);
            emit statusChanged();
        }
    });

    // Silent updater: re-check periodically so a published build rolls out
    // without a relaunch. The finished handler auto-applies when the session is
    // idle (deferred while streaming), so the user is always brought current.
    updateRecheckTimer_.setInterval(30 * 60 * 1000); // 30 min
    connect(&updateRecheckTimer_, &QTimer::timeout, this, [this]() {
        if (devBuild_) {
            return; // a build tree never auto-updates
        }
        licenseClient_.checkUpdate(appVersion(), updateChannel());
    });
    updateRecheckTimer_.start();

    connect(&remotePlay_, &RemotePlaySession::stateChanged, this, [this](RemotePlayState state, const QString& status) {
        hookDigitalRestSinceMs_ = -1;
        hookReleaseRepairDueMs_ = -1;
        hookReleaseRepairHavePreviousOutput_ = false;
        const QString prevRemoteState = remoteState_;   // [ORION_USER_LOG] dedupe user lines
        remoteState_ = remotePlayTeardownActive_ ? QStringLiteral("Disconnecting") : stateText(state);
        // [COPY-FIX 2026-09-23 NEW-A5] The Live page, the input-dead overlay and the
        // Activity feed show customer copy; the raw engine status is logged below.
        const QString customerStatus = ui_notifications::customerRemoteStatus(status);
        remoteStatus_ = remotePlayTeardownActive_ ? QStringLiteral("Disconnecting…") : customerStatus;
        remoteRunning_ = state == RemotePlayState::Running || state == RemotePlayState::Connecting;
        // The pre-Connect capture preview only exists while Disconnected. Once the real session
        // takes the panel over (Connecting/Running) drop the preview flag — this is also what makes
        // the Connect handoff seamless: RemotePlaySession leaves capturePreviewActive set through
        // the paced device hand-off and this Connecting transition clears it once streamLive covers
        // the panel, so the last preview frame never flashes to the idle placeholder.
        if ((state == RemotePlayState::Connecting || state == RemotePlayState::Running)
                && capturePreviewActive_) {
            capturePreviewActive_ = false;
        }
        if (shouldReleaseInputRouteOnRemoteState(state)) {
            // Error is terminal for input authority but not necessarily for
            // capture. A failed warm promotion restores the live HDMI preview
            // in RemotePlaySession; release only Chiaki/ViGEm ownership here.
            releaseFailedRemoteInputRoute();
        }
        if (state == RemotePlayState::Connecting) {
            setCaptureSourceHealth(QStringLiteral("waiting_for_first_frame"));
        } else if (state == RemotePlayState::Disconnected || state == RemotePlayState::Error) {
            setCaptureSourceHealth(QStringLiteral("capture_source_lost"));
            sessionStartMs_ = 0;  // session clock stops + resets when the stream ends
            // [ORION_BANNER_VERDICT_LIVE 2026-09-14] The banner tally is a LIVE reading; it
            // must not survive the stream that produced it. resetBannerTallyForSession also
            // drops the de-dupe watermark, because the sidecar's verdict seq restarts at 1.
            resetBannerTallyForSession();
        }
        // A fresh stream session = fresh capture geometry/latency — silently drop every
        // shot type to ACQUIRE (clocks stay as warm starts; an unchanged setup re-locks in
        // calLockAfterGreens shots).
        static bool wasRunning = false;
        const bool nowRunning = state == RemotePlayState::Running;
        if (!nowRunning && latencyCalibrationActive_) {
            // A controlled marker is meaningful only on the live console route. Never leave
            // calibration armed across disconnect/reconnect where an unrelated shot could be
            // mislabeled as a controlled early sample.
            latencyCalibrationStatusOverride_ = QStringLiteral(
                "Cancelled because Remote Play stopped. Existing samples were kept.");
            automation_.setLatencyCalibrationMode(false);
            appendLog(QStringLiteral("Timing latency calibration cancelled: Remote Play stopped."));
        }
        if (nowRunning && !wasRunning) {
            sessionStartMs_ = QDateTime::currentMSecsSinceEpoch();  // session time starts at live capture
            // [ORION_BANNER_VERDICT_LIVE 2026-09-14] Fresh stream = fresh tally (and a fresh
            // sidecar verdict counter, which restarts at seq 1).
            resetBannerTallyForSession();
#ifdef Q_OS_WIN
            // Startup intentionally defers the process timer-throttling opt-out
            // on battery. Complete that deferred transition as soon as a live
            // session actually needs precision timing. An explicit opt-out
            // remains authoritative, and a startup failure is retried here.
            const bool timerGuardDisabled =
                qEnvironmentVariableIsSet("ORION_TIMER_RES_GUARD")
                && qEnvironmentVariableIntValue("ORION_TIMER_RES_GUARD") == 0;
            if (!timerGuardDisabled && !timerResolutionThrottleOptOutApplied_) {
                timerResolutionThrottleOptOutApplied_ =
                    orion::disableTimerResolutionThrottling();
                appendLog(QStringLiteral(
                    "Timer-resolution throttle deferred apply on stream start: %1 "
                    "(kernel timer resolution now %2 ms)")
                              .arg(timerResolutionThrottleOptOutApplied_
                                       ? QStringLiteral("applied")
                                       : QStringLiteral("FAILED"))
                              .arg(orion::currentTimerResolutionMs(), 0, 'f', 3));
            }
#endif
            automation_.recalibrateAllShotTypes();
            appendLog(QStringLiteral("Calibration: all shot types -> Acquire (stream start)"));
        }
        wasRunning = nowRunning;
        // Input authority follows the REAL session state, not remoteRunning_ (which is also true
        // during Connecting so the preview remains visible). Preview/Connecting/Error/Disconnected
        // must all revoke any copied precise-fire deadline; Running re-arms only if ViGEm is live.
        const bool routeReadyNow = automationRouteReady(nowRunning, controller_.isConnected());
        syncEngineArmed();
        if (!routeReadyNow) {
            // setArmed(false) intentionally stays lock-free and does not mutate ShotContext. Reset
            // NOW on this GUI-thread route transition: if the physical pad vanished simultaneously,
            // pollPhysicalController() returns before process() and otherwise leaves a stale Holding
            // context that a later reconnect could revive.
            automation_.reset();
            shot_ = automation_.context();
            shotState_ = holdStateText(shot_.state);
            observeBotOwnership(shot_);
        }
        appendLog(QStringLiteral("Remote Play: %1 - %2").arg(remoteState_, customerStatus));
        if (customerStatus != status) {
            // Engineering log only (ui_notifications rule 0 hides "engine detail:").
            appendLog(QStringLiteral("Remote Play engine detail: %1").arg(status));
        }
        // [ORION_USER_LOG] (5b) plain-language connection lines, on state CHANGES only.
        if (userLog_.enabled() && remoteState_ != prevRemoteState) {
            if (state == RemotePlayState::Running) {
                userLog_.append(QStringLiteral("Connected — the console stream is live."));
            } else if (state == RemotePlayState::Connecting) {
                userLog_.append(QStringLiteral("Connecting to your console..."));
            } else if (state == RemotePlayState::Error) {
                userLog_.append(QStringLiteral("Connection problem: %1").arg(customerStatus));
            } else {
                userLog_.append(QStringLiteral("Disconnected from the console."));
            }
        }
        // [ORION_INPUT_DEAD_UX] A healthy Running session closes the failure episode: the
        // bounded retry budget belongs to an episode, never to the session. Any other state
        // simply re-evaluates the overlay from the same shared predicate.
        if (state == RemotePlayState::Running) {
            inputRetryPlanner_.reset();
            inputRetryPendingAttempt_ = 0;
            inputRetryGaveUp_ = false;
        }
        refreshInputDeliveryState();
        emit statusChanged();
        // A stream just ended: apply any update that was deferred while live.
        if (!nowRunning && (state == RemotePlayState::Disconnected || state == RemotePlayState::Error)) {
            maybeAutoApplyUpdate();
        }
    });
    // [ORION_INPUT_DEAD_UX 2026-08-30] Terminal input-session failures, classified by
    // RemotePlaySession (promotion verdict / native deadline / lost command / identity block).
    // Drives the bounded auto-retry so a failed promote no longer strands the player on live
    // HDMI with dead input and NO retry (measured 08-28: 2m18s of presses into a Disconnected
    // session after one failed promote).
    connect(&remotePlay_, &RemotePlaySession::inputSessionFailure,
            this, &OrionAppController::handleInputSessionFailure);
    connect(&remotePlay_, &RemotePlaySession::inputRecoveryStarted,
            this, [this]() {
        // Input-only recovery keeps the current stream/capture generation alive, so it must not
        // flow through Running -> Connecting -> Running (which recalibrates every shot type).
        // Revoke only controller/fire authority until a forced neutral write proves the fresh
        // direct pipe. Failure/timeout still transitions RemotePlaySession to Error below.
        inputRouteAwaitingRecovery_ = true;
        preciseFireRecoveryNeutralFrames_ = 0;
        automation_.setArmed(false);
        automation_.reset();
        // [2026-09-22 RED TEAM CL-007] This was the only reset() site that did not neutralise owned
        // output; a bot-held Square could survive the recovery with no release and no drain.
        neutralizeOwnedInput();
        shot_ = automation_.context();
        shotState_ = holdStateText(shot_.state);
        observeBotOwnership(shot_);
        syncEngineArmed();
        {
            // syncEngineArmed has drained the precise worker. Retire the old
            // handle/snapshot before a replacement input child can be spawned;
            // cleanup and idle probes must not inherit its ownership proof.
            QMutexLocker submitLock(&submitMutex_);
            orionInput_.resetConnection();
            directPipeOwnsInput_ = false;
        }
        appendLog(QStringLiteral(
            "Input-only recovery started: controller/fire authority revoked; existing "
            "sidecar, capture, detector, and learned timing retained."));
        refreshInputDeliveryState();
        emit statusChanged();
    });
    connect(&remotePlay_, &RemotePlaySession::sidecarProcessGenerationStarted,
            this, [this]() {
        // The sidecar's latency scope epoch is process-local. A watchdog restart
        // may legitimately return from epoch 42 to epoch 2; retain the native
        // attestation generation fence but establish a fresh scope-epoch baseline
        // before this process can emit its first acknowledgement or telemetry.
        latencyCacheRouteAttestation_.beginSidecarProcessGeneration();
        automation_.beginSidecarProcessGeneration();
        appendLog(QStringLiteral(
            "New sidecar process generation: timing scope baseline reset; "
            "fresh neutral route proof required"));
    });

    connect(&remotePlay_, &RemotePlaySession::previewActiveChanged, this, [this](bool active) {
        if (capturePreviewActive_ == active) {
            return;
        }
        capturePreviewActive_ = active;
        // [ORION_INPUT_DEAD_UX] Preview restore after a failed promotion is exactly the moment
        // video goes live over a dead session — re-evaluate the overlay on the same event.
        refreshInputDeliveryState();
        emit statusChanged();
    });

    connect(&remotePlay_, &RemotePlaySession::frameReady, this, &OrionAppController::handleRemoteFrame);

    // Sidecar capability probe (No-Meter/pose stack present?). The sidecar resolves the imports
    // and weight files WITHOUT importing torch and reports once at startup; re-emit statusChanged
    // so the QML banner bindings (noMeterAvailable / noMeterUnavailableReason) refresh.
    connect(&remotePlay_, &RemotePlaySession::sidecarCapabilitiesChanged, this,
            [this](bool available, const QString& reason) {
        if (!available && config_.data().noMeterEnabled) {
            appendLog(QStringLiteral("WARNING: No-Meter mode is ENABLED but cannot run on this "
                                     "install (%1). Shots will not be timed — turn it off.")
                          .arg(reason.isEmpty() ? QStringLiteral("missing dependencies") : reason));
        }
        emit statusChanged();
    });
    connect(&remotePlay_, &RemotePlaySession::sidecarStatsChanged, this, [this]() {
        // The sidecar owns capture and network telemetry. The native C++
        // detector/automation path owns shot state so the virtual controller
        // uses the same values the UI displays. This signal is emitted at frame
        // cadence, but all work here is presentation-only: sample and format it
        // at human cadence instead of rebuilding strings on every frame.
        const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
        if (remotePlay_.state() == RemotePlayState::Running
            && telemetryStatusThrottle_.take(nowMs)) {
            if (!config_.data().meterEnabled) {
                shotState_ = remotePlay_.shotState();
                shot_.fillPct = remotePlay_.shotFillPct();
                shot_.confidence = remotePlay_.shotConfidence();
            }
            if (!remotePlay_.algorithmText().isEmpty()) {
                visionPipeline_ = remotePlay_.algorithmText();
            }
            emit statusChanged();
        }
    });

    connect(&remotePlay_, &RemotePlaySession::latencyRouteAttestationAck, this,
            [this](bool accepted, const QString& deliveryRoute, quint64 generation,
                   quint64 scopeEpoch, const QString& scopeDigest,
                   const QString& reason) {
        const LatencyControllerRoute route = deliveryRoute == QLatin1String("pipe")
            ? LatencyControllerRoute::Pipe
            : (deliveryRoute == QLatin1String("vigem_ds4")
                ? LatencyControllerRoute::VigemDs4
                : (deliveryRoute == QLatin1String("vigem_xusb")
                    ? LatencyControllerRoute::VigemXusb
                    : LatencyControllerRoute::None));
        if (!latencyCacheRouteAttestation_.pending()
            || generation != latencyCacheRouteAttestation_.generation()
            || route != latencyCacheRouteAttestation_.route()
            || !automation_.controllerDeliveryRouteAttestationExpected(generation, route)) {
            appendLog(QStringLiteral(
                "Ignored stale/mismatched timing route acknowledgement: route=%1 generation=%2")
                          .arg(deliveryRoute).arg(generation));
            return;
        }
        if (!accepted) {
            appendLog(QStringLiteral(
                "Timing route acknowledgement rejected by sidecar: route=%1 generation=%2 reason=%3")
                          .arg(deliveryRoute).arg(generation)
                          .arg(reason.isEmpty() ? QStringLiteral("unspecified") : reason));
            return;
        }
        const quint64 priorScopeEpoch =
            latencyCacheRouteAttestation_.observedScopeEpoch();
        if (latencyCacheRouteAttestation_.observeScopeEpoch(scopeEpoch)) {
            automation_.setControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
            appendLog(QStringLiteral(
                "Timing route acknowledgement crossed a sidecar scope transition: "
                "old_epoch=%1 new_epoch=%2; proof revoked")
                          .arg(priorScopeEpoch).arg(scopeEpoch));
            return;
        }
        if (latencyCacheRouteAttestation_.completeExactEcho(
                generation, route, scopeEpoch)) {
            appendLog(QStringLiteral(
                "Timing route acknowledged directly by sidecar: route=%1 generation=%2 "
                "scope_epoch=%3 scope=%4")
                          .arg(deliveryRoute).arg(generation).arg(scopeEpoch)
                          .arg(scopeDigest.left(12)));
        }
    });

    connect(&remotePlay_, &RemotePlaySession::sidecarDetectionReady, this, [this](const DetectionResult& result) {
        // The full-res sidecar detector is the SINGLE source of truth for both
        // shot timing and the meter overlay state. (The preview detector in
        // handleRemoteFrame only positions the box.)
        const bool timestampedFrame = std::isfinite(result.captureTsMs)
            && result.captureTsMs > 0.0;
        const bool uniqueCaptureProof = timestampedFrame
            ? result.captureTsMs > lastCaptureProofTsMs_ + 0.001
            : (result.frameNumber >= 0
               && result.frameNumber != lastCaptureProofFrameNumber_);
        if (uniqueCaptureProof) {
            if (timestampedFrame) {
                lastCaptureProofTsMs_ = result.captureTsMs;
            }
            lastCaptureProofFrameNumber_ = result.frameNumber;
        }
        const double recoveryMaxAgeMs = std::min(
            automation_.config().staleFrameMaxMs,
            automation_.config().strictReleaseMaxSourceAgeMs);
        const bool freshCaptureProof = uniqueCaptureProof
            && !result.staleFrame && !result.ghostFrame
            && std::isfinite(result.frameAgeMs) && result.frameAgeMs >= 0.0
            && result.frameAgeMs <= recoveryMaxAgeMs;
        if (captureAwaitingFreshFrame_ && freshCaptureProof
            && !guiFreezeTripped_.load(std::memory_order_relaxed)) {
            captureAwaitingFreshFrame_ = false;
            syncEngineArmed();
            appendLog(QStringLiteral("Capture recovered on a fresh processed frame; shot timing re-armed."));
        }
        // Scope changes can happen entirely inside the sidecar while the native
        // controller route remains Pipe. Revoke the old proof before this frame
        // reaches AutomationEngine; the next eligible neutral tick mints exactly
        // one replacement generation bound to the new estimator epoch.
        const quint64 priorLatencyScopeEpoch =
            latencyCacheRouteAttestation_.observedScopeEpoch();
        if (latencyCacheRouteAttestation_.observeScopeEpoch(
                result.measuredLatencyScopeEpoch)) {
            automation_.setControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
            appendLog(QStringLiteral(
                "Timing scope changed in-place: old_epoch=%1 new_epoch=%2; "
                "old controller proof revoked")
                          .arg(priorLatencyScopeEpoch)
                          .arg(result.measuredLatencyScopeEpoch));
        }
        const bool overlayIntegrityAllowed = automationSecurityAllowed();
        if (overlayIntegrityAllowed) {
            automation_.updateDetection(result);
            // Rung-30/35 consensus may have produced a better anchor on this exact fresh
            // sample. Commit it together with the already-copied precise target (or before the
            // first worker arm) so reevaluation never has to tear down the fallback token.
            applyPendingPhaseAnchorRefinement();
            // Sub-tick decision-latency win: a fresh meter sample can move the scheduled fire deadline
            // EARLIER right now instead of waiting up to a full 4ms inputPollTimer_ tick. Reschedule-only
            // (never fires in-tick / never advances the tracking-history/tempo cadence).
            automation_.reevaluateScheduleOnFreshSample();
        }
        shot_ = automation_.context();
        shotState_ = holdStateText(shot_.state);

        const qint64 nowMeterMs = QDateTime::currentMSecsSinceEpoch();
        const bool genuineOverlayDetection = isGenuineMeterOverlayDetection(
            result, automation_.config().staleFrameMaxMs);
        const bool overlaySourceHealthy = overlayIntegrityAllowed
            && !captureAwaitingFreshFrame_
            && !guiFreezeTripped_.load(std::memory_order_relaxed)
            && !result.staleFrame && !result.ghostFrame
            && std::isfinite(result.frameAgeMs) && result.frameAgeMs >= 0.0
            && result.frameAgeMs <= recoveryMaxAgeMs;
        const bool genuineOverlayFrame = genuineOverlayDetection
            && overlaySourceHealthy && uniqueCaptureProof;
        const bool shotVisuallyActive = isShooting();
        const bool priorOverlayVisualRecent = meterOverlayVisualRecent(
            overlaySourceHealthy, lastMeterOverlayVisualSeenMs_, nowMeterMs,
            shotVisuallyActive);
        const bool genuinePresentationFrame = genuineOverlayFrame
            && isLiveMeterOverlayVisualEvidence(
                result, automation_.config().staleFrameMaxMs,
                shotVisuallyActive)
            && meterOverlayMayAcquireOrContinue(
                result, shot_.physicalShotEpoch, priorOverlayVisualRecent,
                meterBoxCapture_);
        const QSize overlaySampleCaptureSize(
            result.bboxFrameWidth, result.bboxFrameHeight);
        if (overlaySampleCaptureSize.isValid()
            && meterBoxCaptureSize_.isValid()
            && overlaySampleCaptureSize != meterBoxCaptureSize_) {
            // Resolution is part of the source identity. A held bbox from the
            // prior coordinate namespace cannot inherit its shot lease.
            meterOverlayContinuityLease_.reset();
        }
        const MeterOverlayContinuityObservation continuityObservation =
            meterOverlayContinuityLease_.observe(
                result, automation_.config().staleFrameMaxMs, nowMeterMs,
                isShooting(), shot_.physicalShotEpoch, overlaySourceHealthy,
                uniqueCaptureProof);
        const bool trustedOverlayPosition = continuityObservation
            == MeterOverlayContinuityObservation::TrustedPosition;
        const bool overlayPositionDetection = genuinePresentationFrame
            || trustedOverlayPosition;
        if (genuineOverlayFrame) {
            lastRealMeterSeenMs_ = nowMeterMs;
        }
        if (genuinePresentationFrame) {
            lastMeterOverlayVisualSeenMs_ = nowMeterMs;
        }
        const qint64 visibilityFreshnessMs = isShooting()
            ? kMeterConfirmShotMs_ : kMeterConfirmFreshMs_;
        const bool rawMeterVisible = userMeterVisibleFromRawEvidence(
            genuineOverlayFrame, lastRealMeterSeenMs_, nowMeterMs,
            visibilityFreshnessMs);
        const UserMeterVisibilityNotice visibilityNotice = userMeterVisibilityNotice(
            userMeterVisible_, rawMeterVisible);
        userMeterVisible_ = rawMeterVisible;
        // Meter-blind safety net. `rawMeterVisible` (genuine detection, or one within the
        // freshness window) is the "the configured colour IS visible" evidence; the physical
        // shot epoch is the CV-independent proof that a shot happened at all. Counting armed
        // epochs that produce neither is what catches a wrong bar colour -- no detector-health
        // field is consulted, because a colour-blind detector reports perfect health.
        // [RT-MED-04 / CL3-F8-009 2026-09-23] The engine's ACTIVE press epoch (owned shot or the
        // still-pending METER press), not shot_.physicalShotEpoch, which only beginShot assigns.
        observeMeterBlindness(automation_.activePhysicalPressEpoch(), rawMeterVisible);
        if (userLog_.enabled()) {
            if (visibilityNotice == UserMeterVisibilityNotice::Detected) {
                userLog_.append(QStringLiteral("Shot meter detected on screen."));
            } else if (visibilityNotice == UserMeterVisibilityNotice::NoLongerVisible) {
                userLog_.append(QStringLiteral("Shot meter no longer visible."));
            }
        }

        // Detection-presence transition log: proves real sidecar samples are
        // reaching the engine (presence=accepted) rather than the engine starving
        // (no_sample_ever). Logged only on a change so it doesn't spam.
        if (shot_.detectionPresence != lastLoggedPresence_) {
            const QString prevPresence = lastLoggedPresence_;   // [ORION_USER_LOG]
            lastLoggedPresence_ = shot_.detectionPresence;
            // A missing/duplicate sample can alternate on every detector
            // payload.  Logging each diagnostic transition at 60 Hz filled the
            // GUI log queue during the exact shot interval whose preview must
            // stay smooth.  Keep the first loss and every accepted boundary,
            // but collapse noise-to-noise oscillation; the per-frame detector
            // census remains available in release diagnostics.
            const bool suppressDiagnosticOscillation =
                ui_notifications::isNoisyDetectionPresence(prevPresence)
                && ui_notifications::isNoisyDetectionPresence(
                    shot_.detectionPresence);
            // NB: QString::arg does NOT collapse %% -> % (that's printf) — a doubled %% here
            // rendered every line as "fill 55.7%%".
            if (!suppressDiagnosticOscillation) {
                appendLog(QStringLiteral("Detection presence: %1 (fill %2% conf %3 green %4/%5 age %6ms src=%7)")
                          .arg(shot_.detectionPresence)
                          .arg(result.fillPct, 0, 'f', 1)
                          .arg(result.confidence, 0, 'f', 2)
                          .arg(result.greenStartPct, 0, 'f', 0)
                          .arg(result.greenEndPct, 0, 'f', 0)
                          .arg(result.frameAgeMs, 0, 'f', 0)
                          .arg(result.detectorSource));
            }
        }

        meterRuntimeState_ = result.detected ? QStringLiteral("Meter") : QStringLiteral("Searching");
        if (!result.profileName.isEmpty() || !result.style.isEmpty()) {
            meterProfile_ = result.profileName.isEmpty() ? result.style : result.profileName;
        }
        meterRejectionReason_ = result.detected
            ? QStringLiteral("-")
            : (result.rejectionReason.isEmpty() ? QStringLiteral("no meter lock") : result.rejectionReason);
        // UI truth boundary: timing may coast or extrapolate internally through a
        // bounded occlusion, but a live HUD value is published only when this exact
        // payload is a genuine fresh raw detector frame. A memory/stale/no-meter
        // payload is explicitly unavailable rather than displaying a plausible lie.
        const bool metricAccepted = genuineOverlayFrame
            && std::isfinite(result.fillPct) && result.fillPct >= 0.0 && result.fillPct <= 100.0
            && std::isfinite(result.confidence) && result.confidence >= 0.0 && result.confidence <= 1.0
            && std::isfinite(result.velocityPctS);
        if (metricAccepted) {
            meterMetricsCurrent_ = true;
            measuredMeterAtMs_ = nowMeterMs;
            ++measuredMeterSerial_;
            if (measuredMeterSerial_ == 0) {
                ++measuredMeterSerial_;
            }
            measuredMeterFillPct_ = result.fillPct;
            measuredMeterConfidence_ = result.confidence;
            measuredMeterVelocityPctS_ = result.velocityPctS;
            // A new detector frame invalidates the prior frame's crossing join.
            // The next 4ms input tick runs processHolding on this exact sample and
            // publishes an active-target-matched ETA via refreshMeterTargetEtaSnapshot().
            measuredEtaSerial_ = 0;
            measuredEtaArmToken_ = 0;
            measuredEtaTargetPct_ = -1.0;
            measuredEtaAtObservationMs_ = -1.0;
            meterFillLine_ = QStringLiteral("%1%").arg(measuredMeterFillPct_, 0, 'f', 1);
            // The engine target is the actual target used by both Square and
            // right-stick timing. Do not fall back to a decorative/default target
            // before a bot-owned shot exists.
            meterTargetLine_ = shot_.armToken != 0 && std::isfinite(shot_.targetPct)
                    && shot_.targetPct > 0.0
                ? QStringLiteral("%1%").arg(shot_.targetPct, 0, 'f', 1)
                : QStringLiteral("--");
            armMeterMetricsExpiry();
            if (meterMetricsCadence_.takeSample(nowMeterMs)) {
                emit meterMetricsChanged();
            }
        } else {
            clearMeterMetrics();
        }
        if (result.detected) {
            lastMeterSeenMs_ = nowMeterMs;
            if (overlayPositionDetection) {
                // Timing remains genuine-raw-only. The display may additionally
                // follow a reader-authorized position-only bbox during the bounded,
                // current-shot continuity lease established above.
                if (result.width > 0 && result.height > 0) {
                    QSize captureSize(result.bboxFrameWidth, result.bboxFrameHeight);
                    if (!captureSize.isValid()) {
                        captureSize = QSize(
                            remotePlay_.captureWidth(), remotePlay_.captureHeight());
                    }
                    // A bbox and its coordinate dimensions are one immutable
                    // detector sample. Resolution/source changes start a new
                    // join epoch so an older differently-scaled box can never
                    // be extrapolated into the new frame namespace.
                    if (captureSize.isValid()
                        && meterBoxCaptureSize_.isValid()
                        && meterBoxCaptureSize_ != captureSize) {
                        meterBoxRing_.clear();
                    }
                    if (captureSize.isValid()) {
                        meterBoxCaptureSize_ = captureSize;
                    }
                    meterBoxCapture_ = QRect(result.x, result.y, result.width, result.height);
                    meterBoxRing_.record(result.frameNumber, meterBoxCapture_);
                    // Preview decode/presentation can publish frame N before
                    // telemetry for N reaches this callback. Amend only still-
                    // unacknowledged overlay metadata; the image identity and
                    // all AutomationEngine inputs remain untouched.
                    static_cast<void>(
                        remoteFrameOverlaySnapshots_.backfillUnacknowledged(
                            qmlPreviewLastAcknowledgedSerial_,
                            captureSize,
                            MeterBoxRing::kJoinWindow,
                            [this](int sourceFrameNumber,
                                   QRect& outCaptureBox,
                                   int& matchedDetectionFrameNumber) {
                                return meterBoxRing_.lookup(
                                    sourceFrameNumber,
                                    outCaptureBox,
                                    &matchedDetectionFrameNumber);
                            }));
                    // The exact observation can also arrive just AFTER Ready.
                    // Correct the visible provisional bridge only while this
                    // same serial/source/shot/coordinate space is still front.
                    // Do not change future computed boxes or any timing input.
                    if (genuinePresentationFrame && meterConfirmed_ && meterBox_.isValid()
                        && remoteFrameOverlaySnapshots_.backfillPresentedExact(
                            qmlPreviewLastAcknowledgedSerial_, captureSize,
                            result.frameNumber, meterBoxCapture_, shot_.armToken)) {
                        const auto presented = remoteFrameOverlaySnapshots_.lookup(
                            qmlPreviewLastAcknowledgedSerial_);
                        if (presented.has_value()) {
                            const QRect exactBox = resolveLateMeterOverlayBox(
                                presented->joinedCaptureBox, presented->captureSize,
                                presented->frameSize, presented->shotToken,
                                presented->sourceFrameNumber);
                            if (exactBox.isValid()
                                && (meterBox_ != exactBox || !meterRejectedBox_.isNull())) {
                                meterBox_ = exactBox;
                                meterRejectedBox_ = {};
                                emit meterBoxChanged();
                            }
                        }
                    }
                }
            }
            lastResult_ = QStringLiteral("%1 %2 fill %3%")
                              .arg(meterProfile_, result.colorName)
                              .arg(result.fillPct, 0, 'f', 1);
        }
        const bool hardOverlayFailure = !overlaySourceHealthy
            && (result.staleFrame || result.ghostFrame
                || !overlayIntegrityAllowed || captureAwaitingFreshFrame_
                || guiFreezeTripped_.load(std::memory_order_relaxed)
                || !std::isfinite(result.frameAgeMs)
                || result.frameAgeMs < 0.0
                || result.frameAgeMs > recoveryMaxAgeMs);
        const bool overlayVisualExpired = !meterOverlayVisualRecent(
            overlaySourceHealthy, lastMeterOverlayVisualSeenMs_, nowMeterMs,
            shotVisuallyActive);
        if (hardOverlayFailure
            || (!genuinePresentationFrame
                && !meterOverlayContinuityLease_.active()
                && overlayVisualExpired)) {
            // The presentation clock is stricter than generic raw-detection
            // presence: empty post-shot echoes and unstructured idle candidates
            // cannot renew it. In-shot misses retain the measured 350ms bridge;
            // after the shot it contracts to 200ms so an empty-court outline
            // cannot outlive the useful meter by another third of a second.
            // Detector payloads continue at capture cadence while no meter is
            // present. Clearing and emitting on every one of those frames woke
            // QML at up to 60 Hz and could make the preview miss its deadline.
            // The first loss edge owns the clear; already-empty samples are inert.
            const bool hadOverlayState = meterConfirmed_
                || !meterBox_.isNull() || !meterRejectedBox_.isNull()
                || !meterOverlayComputedBox_.isNull()
                || !meterOverlayComputedRejectedBox_.isNull()
                || !meterBoxCapture_.isNull() || meterOverlayTracker_.valid()
                || !meterBoxRing_.empty();
            if (hadOverlayState) {
                // Preserve older immutable joins already being uploaded, but
                // make this detector-loss frame authoritative for every newer
                // unpresented snapshot so a prior provisional join cannot
                // resurrect the box after the clear boundary.
                static_cast<void>(
                    remoteFrameOverlaySnapshots_.clearUnacknowledgedFromSourceFrame(
                        qmlPreviewLastAcknowledgedSerial_, result.frameNumber));
                meterOverlayTracker_.reset();
                meterBox_ = {};
                meterRejectedBox_ = {};
                meterOverlayComputedBox_ = {};
                meterOverlayComputedRejectedBox_ = {};
                // Detector loss clears the box for future frames, not the immutable
                // frame/overlay joins already being uploaded by QML. Clearing this
                // bounded history mid-stream made otherwise valid Image.Ready
                // completions miss their exact snapshot (~10/s while no meter was
                // visible), which looked like preview stutter and stale frames.
                // Lifecycle teardown/reset remains responsible for clearing history.
                meterConfirmed_ = false;
                meterBoxCapture_ = {};
                meterBoxRing_.clear();
                emit meterBoxChanged();
            }
            lastMeterOverlayVisualSeenMs_ = 0;
        }
        if (!remotePlay_.algorithmText().isEmpty()) {
            visionPipeline_ = remotePlay_.algorithmText();
        }
        notifyTelemetryStatusAtHumanCadence(nowMeterMs);
    });

    // Persist the per-type learned offset so calibration survives a restart (it was
    // previously loaded from learning.json but never saved back).
    connect(&automation_, &AutomationEngine::learningUpdated, this,
            [this](const QMap<QString, double>& learned) {
        LearningData data = config_.learning();
        data.shotTypeLearnedOffsetMs = learned;
        config_.saveLearning(data);
    });
    // [ORION_PHASE_COLD_START] Persist the learned animation constant so the cold start is paid
    // once per jumpshot instead of once per launch.
    connect(&automation_, &AutomationEngine::phaseConstantUpdated, this,
            [this](double learnedPhysicalMs) {
        LearningData data = config_.learning();
        data.learnedPhasePhysicalMs = learnedPhysicalMs;
        config_.saveLearning(data);
        // [ORION_USER_TIP] The Tip Timing card displays THIS persisted value (in the
        // effective frame), so the learner's own publish cadence is the card's refresh
        // cadence — once per accepted landing while the window is full, nothing per tick.
        emit tipTimingChanged();
    });
    // [ORION_AIM_FREEZE 2026-08-08] Persist the instrument's OWN full-window median SEPARATELY
    // from the aim slot. While tip_phase_aim_frozen holds, phaseConstantUpdated above
    // deliberately re-persists the manual value (the Tip Timing card's persistence contract),
    // which used to discard the session's own measurement at the last instruction — so the
    // measured_phase_physical_ms key stayed empty and the restore-time manual-vs-measured
    // divergence warning died at every restart. Nothing on the decision path consumes this.
    // [ORION_AIM_AUTOUNLOCK 2026-08-13] The engine has established, over ten consecutive FULL
    // learner windows, that the locked aim disagrees with this rig's own measured animation by
    // more than kTipTimingDivergenceWarnMs. Hand the value back to the learner through exactly
    // the path the card's Reset button uses, so the manual value survives as the learner's prior
    // and there is no mid-session snap. The engine cannot do this itself: settings are the
    // controller's to write, and routing it here keeps one owner for the lock.
    connect(&automation_, &AutomationEngine::tipTimingAutoUnlockRequested, this,
            [this](double frozenPhysicalMs, double measuredPhysicalMs) {
        Q_UNUSED(frozenPhysicalMs);
        Q_UNUSED(measuredPhysicalMs);
        if (!config_.data().tipPhaseAimFrozen && !config_.data().tipTimingUserSet) {
            return;   // already unlocked (user got there first) -- nothing to do, stay quiet
        }
        // The engine has already emitted the plain-language TIP TIMING AUTO-UNLOCKED line through
        // engineDiagnostic, which is what reaches the activity log and the user log; adding a
        // second sentence here would double-report one event. resetTipTiming() emits
        // tipTimingChanged(), so the card drops its "Locked" pill on the same beat.
        resetTipTiming();
    });
    connect(&automation_, &AutomationEngine::phaseMeasuredMedianUpdated, this,
            [this](double measuredPhysicalMs) {
        persistMeasuredPhaseMedian(config_, measuredPhysicalMs);
        // The Tip Timing card shows this measurement beside a diverging manual value
        // (tipTimingMeasuredMs), so re-notify on the same cadence as the aim slot above.
        emit tipTimingChanged();
    });
    // [ORION_SESSION_LEAD_PROBE 2026-09-01] persist the (anchor->freeze median, lead) reference
    // pair the engine captured for the current Shot Lead; see AppConfigData::sessionLeadProbeEnabled.
    connect(&automation_, &AutomationEngine::leadReferenceCaptured, this,
            [this](double physicalMs, double leadMs) {
        LearningData data = config_.learning();
        data.leadReferencePhysicalMs = physicalMs;
        data.leadReferenceLeadMs = leadMs;
        config_.saveLearning(data);
    });
    // [ORION_LEAD_CONFLICT 2026-08-08] The two engine diagnoses that name the lost-session trap
    // (Shot Lead unschedulable against Tip Timing; user lead >3*sd from the validated
    // posterior) -> persistent ShotLeadCard banner state. Banner VISIBILITY is computed
    // reactively in QML from the live pair (actuationLeadMs, shotLeadMaxUsableMs), so it clears
    // the instant the user fixes either control; these connects carry only the engine-side
    // facts the banners quote (session miss count, posterior stats).
    connect(&automation_, &AutomationEngine::shotLeadConflictDiagnosed, this,
            [this](double leadMs, double maxUsableLeadMs, int missesThisSession) {
        Q_UNUSED(leadMs);
        Q_UNUSED(maxUsableLeadMs);
        shotLeadConflictMisses_ = missesThisSession;
        emit leadDiagnosticsChanged();
    });
    connect(&automation_, &AutomationEngine::leadAuthorityDisagreementDiagnosed, this,
            [this](double leadMs, double authorityMs, double authoritySdMs, int authorityN) {
        Q_UNUSED(leadMs);
        leadAuthorityMs_ = authorityMs;
        leadAuthoritySdMs_ = authoritySdMs;
        leadAuthoritySamples_ = authorityN;
        emit leadDiagnosticsChanged();
    });
    // Persist the per-shot-type feedforward animation clock so it survives a restart.
    connect(&automation_, &AutomationEngine::feedforwardUpdated, this,
            [this](const QMap<QString, double>& feedforward) {
        LearningData data = config_.learning();
        data.shotTypeFeedforwardMs = feedforward;
        config_.saveLearning(data);
    });
    // Persist the per-shot-type meter-appear->release clock (the meter_appear anchor).
    connect(&automation_, &AutomationEngine::meterClockUpdated, this,
            [this](const QMap<QString, double>& meterClock) {
        LearningData data = config_.learning();
        data.shotTypeMeterToReleaseMs = meterClock;
        config_.saveLearning(data);
    });
    // Persist the per-shot-type calibration phase so a dialed-in (LOCKED) type restarts locked.
    connect(&automation_, &AutomationEngine::calPhaseUpdated, this,
            [this](const QMap<QString, int>& calPhase) {
        LearningData data = config_.learning();
        data.shotTypeCalPhase = calPhase;
        config_.saveLearning(data);
    });
    // [ORION_PRESS_ANCHOR] Persist the press->real-tip calibration (per-type windowed
    // median/MAD/weight) on each accepted PRESS-TIP OBSERVATION, so the Aug 9-17 collection
    // accumulates across restarts. settings.json (not learning.json) on purpose: these are
    // the documented press_anchored_* knobs the owner reads/arms when the data is in.
    connect(&automation_, &AutomationEngine::pressAnchoredCalibrationUpdated, this,
            [this](const QMap<QString, double>& tipMs, const QMap<QString, double>& sigmaMs,
                   const QMap<QString, double>& weight) {
        AppConfigData data = config_.data();
        data.pressAnchoredTipMs = tipMs;
        data.pressAnchoredTipSigmaMs = sigmaMs;
        data.pressAnchoredTipN = weight;
        saveConfigSilently(data);
    });
    // Persist the network-offset baseline captured when a type LOCKED (delta-compensation
    // reference) and the per-type velocity prior, alongside the clocks they calibrate.
    connect(&automation_, &AutomationEngine::rttBaselineUpdated, this,
            [this](const QMap<QString, double>& baselines) {
        LearningData data = config_.learning();
        data.shotTypeRttBaselineMs = baselines;
        config_.saveLearning(data);
    });
    connect(&automation_, &AutomationEngine::velocityPriorUpdated, this,
            [this](const QMap<QString, double>& priors) {
        LearningData data = config_.learning();
        data.shotTypeVelocityPriorPctMs = priors;
        config_.saveLearning(data);
    });
    // [ORION_NO_METER_V2 2026-09-14] The per-shot-type press->release hold the VISION path
    // measured. It is what lets the blind release law prefer this owner's own Δ over the
    // shipped table once both the type and Standstill carry >= 8 of his holds.
    connect(&automation_, &AutomationEngine::noMeterHoldLearned, this,
            [this](const QMap<QString, NoMeterHoldRecord>& holds) {
        LearningData data = config_.learning();
        data.noMeterHoldByType = holds;
        config_.saveLearning(data);
    });
    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] The banner closed loop's per-type trim. Persisted
    // to learning.json (the engine decays it 50 % at the next start -- contexts change between
    // sessions) and published to the caption under the Shot Lead slider in the same handler, so
    // the number on screen is always the number the scheduler is spending.
    connect(&automation_, &AutomationEngine::bannerLeadTrimUpdated, this,
            [this](const QMap<QString, double>& trims) {
        LearningData data = config_.learning();
        data.bannerLeadTrimByType = trims;
        config_.saveLearning(data);
        // [ORION_BANNER_TRIM_TEMPO 2026-09-16] The card shows the REFERENCE sub-bucket --
        // Standstill at a normal tempo, the shot the owner tunes the slider on. The bare
        // "Standstill" key is the fallback for a snapshot taken with ORION_BANNER_TRIM_TEMPO=0,
        // so one caption serves both keying modes.
        const double shown = trims.contains(QStringLiteral("Standstill/normal"))
            ? trims.value(QStringLiteral("Standstill/normal"), 0.0)
            : trims.value(QStringLiteral("Standstill"), 0.0);
        if (!qFuzzyCompare(1.0 + shown, 1.0 + bannerLeadTrimMs_)) {
            bannerLeadTrimMs_ = shown;
            emit bannerLeadTrimChanged();
        }
    });
    // [ORION_LEAD_AUTO_SEED 2026-09-15 owner] The plug-and-play Shot Lead's caption. NOTHING is
    // persisted here on purpose: the seed is derived state (this rig's measured latency + the
    // shipped aim margin, or the placeholder until that latency exists), and writing it anywhere
    // near actuation_lead_ms is precisely what the feature promises not to do.
    connect(&automation_, &AutomationEngine::leadAutoSeedUpdated, this,
            [this](double leadMs, double measuredMs, const QString& kind) {
        const bool active = !kind.isEmpty();
        if (active == leadAutoSeedActive_ && kind == leadAutoSeedKind_
            && qFuzzyCompare(1.0 + leadMs, 1.0 + leadAutoSeedMs_)
            && qFuzzyCompare(1.0 + measuredMs, 1.0 + leadAutoSeedMeasuredMs_)) {
            return;
        }
        leadAutoSeedActive_ = active;
        leadAutoSeedKind_ = kind;
        leadAutoSeedMs_ = active ? leadMs : 0.0;
        leadAutoSeedMeasuredMs_ = active ? measuredMs : 0.0;
        emit leadAutoSeedChanged();
    });
    // Persist the hybrid global phase-clock self-learned globals (autonomous_vision path) so they
    // survive a restart. Emitted only from clean, vision-timed, non-fallback releases.
    connect(&automation_, &AutomationEngine::globalTimingLearned, this,
            [this](double appearToTip, double holdToRelease, double latency, double riseVel) {
        LearningData data = config_.learning();
        data.globalAppearToTipMs = appearToTip;
        data.globalHoldToReleaseMs = holdToRelease;
        data.learnedLatencyMs = latency;
        data.globalRiseVelocityPctMs = riseVel;
        data.shotTypeLatencyMs = automation_.config().shotTypeLatencyMs;
        config_.saveLearning(data);
    });
    // [ORION_USER_LEAD] This install's OWN end-to-end lead, measured from where releases landed.
    //
    // SEEDING RULE, and the two halves matter equally:
    //   * it fills the Shot Lead only when the control has never been configured — not user-set
    //     AND still at 0. Once seeded (or once the user moves it) this handler is inert forever,
    //     so the value under the slider can never move while the user is reading the banner and
    //     drawing conclusions from it. That is the whole "your value wins" contract.
    //   * the readout updates every time regardless, so the card can always show what this rig
    //     measures next to what the user chose.
    //
    // QueuedConnection deliberately: the engine emits this from inside evaluatePostReleaseMeter,
    // and saveConfigSilently calls straight back into AutomationEngine::applyConfig. Deferring to
    // the event loop keeps that re-entry out of the middle of a post-release grade.
    connect(&automation_, &AutomationEngine::actuationLeadMeasured, this,
            [this](double medianLeadMs, int samples) {
        if (!std::isfinite(medianLeadMs) || medianLeadMs <= 0.0) {
            return;
        }
        actuationLeadMeasuredMs_ = medianLeadMs;
        actuationLeadMeasuredSamples_ = samples;
        emit actuationLeadMeasurementChanged();

        auto data = config_.data();
        if (!actuationLeadAcceptsMeasuredSeed(data)) {
            return;   // user's value, or an already-installed seed: never silently overwritten
        }
        const double seeded = qBound(AppConfigData::kActuationLeadMinMs, medianLeadMs,
                                     AppConfigData::kActuationLeadMaxMs);
        data.actuationLeadMs = seeded;
        data.actuationLeadUserSet = false;   // measured, not chosen — the user can still be shown so
        // [ORION_LEAD_BY_SOURCE 2026-09-14] the seed was measured on THIS video route; it must
        // not follow the user to the other one.
        mirrorActuationLeadIntoSourceStash(data);
        if (!saveConfigSilently(data)) {
            return;
        }
        appendLog(QStringLiteral(
                      "Shot lead measured on your setup: %1 ms (from %2 shots). "
                      "Adjust it if the game's TIMING banner says EARLY or LATE.")
                      .arg(seeded, 0, 'f', 0)
                      .arg(samples));
    }, Qt::QueuedConnection);
    // Detector-only release-window diagnostic. AutomationEngine has already enforced the native
    // release-id join, non-proxy marker, confidence floor, metric sanity, and dedupe. This is not
    // a game result, so it is logged without touching session accuracy or any learner.
    connect(&automation_, &AutomationEngine::releaseWindowDiagnostic, this,
            [this](int releaseSeq, const QString& shotType, int label,
                   double fillAtRelease, double greenStartPct) {
        const QString position = label == 1 ? QStringLiteral("IN_WINDOW")
            : (label == 0 ? QStringLiteral("COMMAND_EARLY") : QStringLiteral("COMMAND_OVER"));
        appendLog(QStringLiteral("Release-window diagnostic (not game outcome): "
                                 "seq=%1 position=%2 fill=%3 window_start=%4 shot=%5")
                      .arg(releaseSeq).arg(position)
                      .arg(fillAtRelease, 0, 'f', 1)
                      .arg(greenStartPct, 0, 'f', 1)
                      .arg(shotType));
    });
    // Telemetry: one machine-parseable line per attributed outcome (tokens space-free;
    // shot=<type> last since it can contain spaces).
    connect(&automation_, &AutomationEngine::shotOutcomeLearned, this,
            [this](int seq, const QString& shotType, const QString& verdict,
                   double errorMs, double /*learnedOffsetMs*/, double /*feedforwardMs*/,
                   int learnCount, const QString& calPhase, int calGreens, int calMisses,
                   double meterClockMs, const QString& anchor) {
        // The frozen post-release meter grader produces a well-known constant
        // LATE-66 artifact at the dead top. Keep that signal available for
        // detector forensics, but never name it a shot outcome or let it look
        // like gameplay truth.
        const bool graderAuthoritative = !automation_.config().calibrationFrozen
            || automation_.config().calibrationMode;
        // [ORION_LEAD_CONFLICT 2026-08-08] leadMs/lead_source REPLACED learnedOffset=/ffClockMs=
        // here. Those two printed config_.learnedLatencyMs / globalHoldToReleaseMs on the
        // shipped autonomous path, and learnedLatencyMs has no reachable writer in any shipped
        // configuration (globalTimingLearned needs clean vision-timed grades; the grader is
        // frozen) — so both fields were permanently inert seed echoes, and they misled two
        // separate investigations into concluding the learner was broken. The active lead and
        // where it came from (user|seed|authority) is what a reader of this line actually
        // needs next to a verdict.
        const ActiveLeadTelemetry lead = activeLeadForTelemetry();
        const QString payload = QStringLiteral("seq=%1 verdict=%2 errorMs=%3 leadMs=%4 "
                                               "lead_source=%5 meterClockMs=%6 anchor=%7 phase=%8 greens=%9 "
                                               "misses=%10 learnCount=%11 shot=%12")
                      .arg(seq).arg(verdict)
                      .arg(errorMs, 0, 'f', 1).arg(lead.leadMs, 0, 'f', 1)
                      .arg(lead.source).arg(meterClockMs, 0, 'f', 0)
                      .arg(anchor).arg(calPhase).arg(calGreens)
                      .arg(calMisses).arg(learnCount)
                      .arg(shotType);
        appendLog((graderAuthoritative
                       ? QStringLiteral("Shot outcome: ")
                       : QStringLiteral("Self-grade diagnostic (not timing truth): "))
                  + payload);
        // Session tally + per-type last verdict feed the Meter tab's Auto-Calibration
        // and Live Shot cards. FROZEN self-grade is excluded (2026-07-05): with the grader
        // frozen (open-loop ship config) the post-release meter self-grade is known-degenerate
        // at the dead-top — it emits a constant deflate-LATE (meterRecedeLatePct 12 x
        // meterMsPerPct 5.5 = the live "18/20 shots LATE 66.0ms"), which is not a timing
        // measurement. The raw telemetry line above stays for diagnostics; only the UI tally
        // is gated. An authoritative injected verdict (calibrationMode) still tallies.
        if (graderAuthoritative) {
            recordSessionGrade(seq, shotType, verdict, errorMs,
                               automation_.config().calibrationMode
                                   ? QStringLiteral("calibration_oracle")
                                   : QStringLiteral("legacy_dev_grader"));
        }
        // Shoot-to-train: each graded shot is one calibration sample. When the target is hit,
        // lock in the learned colour/green hue. [Track B / B3] Once the sidecar's
        // ColorCalibrator has spoken (calibrate_meter_status events), IT owns the shot count
        // (committed shots, not graded shots) and this legacy increment is skipped.
        if (meterCalibrating_ && !sidecarCalAuthoritative_) {
            ++meterCalShots_;
            if (meterCalShots_ >= meterCalTarget_) {
                finishMeterCalibration();
            } else {
                meterCalStatus_ = QStringLiteral("Learning your meter… %1 / %2 shots.")
                                      .arg(meterCalShots_).arg(meterCalTarget_);
            }
        }
        emit statusChanged();
    });

    // Reader detector health -> the Meter Detection card's status line. Presentation only:
    // the object never reaches the engine, the telemetry snapshot, or any timing path.
    connect(&remotePlay_, &RemotePlaySession::detectorHealthReady, this,
            &OrionAppController::observeDetectorHealth);

    // [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] One graded shot off the GAME'S OWN
    // feedback banner -> the rolling 10-shot tally the owner tunes against, plus one plain
    // Activity line. Presentation only: it never reaches the engine, the telemetry snapshot
    // or any timing path.
    // [ORION_ONSET_FF 2026-09-21] A court change drops the onset feedforward's reference.
    connect(&remotePlay_, &RemotePlaySession::courtChanged, this,
            [this](const QString& courtIp) {
                Q_UNUSED(courtIp);   // never log the court address
                automation_.noteContextChanged(QStringLiteral("court_changed"));
            });
    connect(&remotePlay_, &RemotePlaySession::bannerVerdict, this,
            &OrionAppController::observeBannerVerdict);
    // [ORION_RELEASE_ORACLE_TRIM 2026-09-15] The banner-free input to the same trim.
    connect(&remotePlay_, &RemotePlaySession::releaseOracle, this,
            &OrionAppController::observeReleaseOracle);
    // [ORION_SHOT_RANGE 2026-09-17] The press's THREE/MID reading -> the engine's live shot.
    connect(&remotePlay_, &RemotePlaySession::shotRange, this,
            &OrionAppController::observeShotRange);

    // [Track B / B3] Sidecar ColorCalibrator lifecycle -> the meterCalibration* properties +
    // the MeterConfigPanel badge. The sidecar emits per committed shot and per state
    // transition; "locked" while a user window is open means the bands baked + persisted.
    connect(&remotePlay_, &RemotePlaySession::meterCalibrationStatusReady, this,
            [this](int shotsDone, int shotsNeeded, const QString& state,
                   const QString& learnedDate, bool calibrating) {
        sidecarCalAuthoritative_ = true;
        if (!state.isEmpty()) {
            meterCalState_ = (state == QLatin1String("seed")) ? QStringLiteral("factory") : state;
        }
        meterCalLearnedDate_ = learnedDate;
        meterCalShots_ = shotsDone;
        if (shotsNeeded > 0) {
            meterCalTarget_ = shotsNeeded;
        }
        if (meterCalibrating_) {
            if (state == QLatin1String("locked")) {
                // The sidecar baked + persisted (FROZEN) — the user flow is complete; no
                // 'finish' command needed (it would be a no-op: the window already closed).
                meterCalibrating_ = false;
                meterCalStatus_ = QStringLiteral("Calibrated ✓ — colour bands learned from %1 shots.")
                                      .arg(shotsDone);
                appendLog(QStringLiteral("Meter calibration complete: bands frozen after %1 committed shots.")
                              .arg(shotsDone));
            } else if (!calibrating) {
                // Sidecar window closed without a bake (10-shot window exhausted / cancel):
                // the snapshot was restored sidecar-side.
                meterCalibrating_ = false;
                meterCalStatus_ = QStringLiteral("Calibration window closed — previous bands kept.");
                appendLog(QStringLiteral("Meter calibration window closed without enough clean shots."));
            } else {
                meterCalStatus_ = QStringLiteral("Learning your meter… %1 / %2 shots.")
                                      .arg(shotsDone).arg(shotsNeeded);
            }
        }
        emit statusChanged();
    });

    connect(&remotePlay_, &RemotePlaySession::audioToggleBusyChanged, this, [this](bool) {
        emit statusChanged();
    });

    connect(&remotePlay_, &RemotePlaySession::telemetryReady, this, [this](const TelemetrySnapshot& rpTelemetry) {
        // The sidecar's active sampler is the sole RTT/court/tick authority. The
        // native bridge contributes packet counters and cadence diagnostics only;
        // arbitrary UDP direction gaps are not request/response RTT samples. Start
        // from the sidecar snapshot so every false/zero validity field revokes stale
        // timing immediately, then copy only non-authoritative bridge diagnostics.
        const TelemetrySnapshot observed = telemetry_;
        telemetry_ = rpTelemetry;
        if (networkBridge_.connected()) {
            // Preserve passive identity only as a distinct display field. RTT,
            // tick, offset, and courtIp authority all come from rpTelemetry.
            telemetry_.diagnosticCourtIp = observed.diagnosticCourtIp;
            if (telemetry_.consoleIp.isEmpty()) {
                telemetry_.consoleIp = observed.consoleIp;
            }
            telemetry_.inboundPackets = observed.inboundPackets;
            telemetry_.outboundPackets = observed.outboundPackets;
            telemetry_.consecutivePackets = observed.consecutivePackets;
            telemetry_.inboundConsecutive = observed.inboundConsecutive;
            telemetry_.outboundConsecutive = observed.outboundConsecutive;
            telemetry_.inboundIntervalMs = observed.inboundIntervalMs;
            telemetry_.outboundIntervalMs = observed.outboundIntervalMs;
            if (telemetry_.packetIntervalMs <= 0.0) {
                telemetry_.packetIntervalMs = observed.packetIntervalMs;
            }
            telemetry_.playingGame = telemetry_.playingGame || observed.playingGame;
        } else if (telemetry_.inboundPackets == 0 && telemetry_.outboundPackets == 0) {
            // RemotePlaySession may publish placeholder counters between sidecar
            // telemetry frames. Do not erase them merely because the RTT object
            // carries no packet-counter fields.
            telemetry_.inboundPackets = observed.inboundPackets;
            telemetry_.outboundPackets = observed.outboundPackets;
        }
        // (The HUD-only route-sample mirror that lived here was removed with
        // the COURT/JITTER HUD rows, 2026-08-06 — see the meterHud* comment.)
        // [ORION_METER_DELAY 2026-08-07] Court-IP session gate. telemetryCourtIp()
        // is the app-wide merge (verified sidecar courtIp when rttTargetVerified,
        // else the bridge's diagnostic classification), so both discovery paths
        // drive the one gate; the controller ignores no-op updates.
        meterDelay_.setCourtIpKnown(!telemetryCourtIp().isEmpty());
        automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
        notifyTelemetryPropertiesAtHumanCadence(
            QDateTime::currentMSecsSinceEpoch());
    });

    // No-meter mode: route pose landmarks (push/release) from the Python sidecar to the engine.
    // The engine schedules the release using its own clock + noMeterBaseOffsetMs - noMeterDecodeCompMs.
    // frame_seq is for staleness rejection only (see plan: no cross-process wall-clock).
    connect(&remotePlay_, &RemotePlaySession::poseLandmarkReady, this,
            [this](const QString& kind, int frameSeq, double confidence, quint64 armToken) {
        if (config_.data().noMeterEnabled) {
            automation_.updatePoseLandmark(kind, frameSeq, confidence, armToken);
        }
    });

    // No-meter skeleton overlay: store the locked player's keypoints + box for the QML draw,
    // plus the camera-anchor points (anchor / lock_center / indicator, full-frame px;
    // indicator is empty when the under-player marker is not detected this frame).
    connect(&remotePlay_, &RemotePlaySession::poseOverlayReady, this,
            [this](const QVariantList& keypoints, const QVariantList& box,
                   const QVariantList& anchor, const QVariantList& lockCenter,
                   const QVariantList& indicator) {
        poseKeypoints_ = keypoints;
        poseBox_ = box;
        poseAnchor_ = anchor;
        poseLockCenter_ = lockCenter;
        poseIndicator_ = indicator;
        emit poseOverlayChanged();
    });

    // One physical shot creates one sidecar arm epoch in both meter and lab pose
    // modes. The token prevents a queued landmark from an earlier shot being
    // accepted after the next shot has armed. Meter mode still uses the command to
    // open SimpleMeterReader's bounded hardware shot gate.
    connect(&automation_, &AutomationEngine::shotArmed, this,
            [this](orion::ShotMode, const QString&, quint64 armToken) {
        if (!config_.data().inputTimedEnabled) remotePlay_.armPose(armToken);
    });

    // [ORION_SHOT_GATE_TYPE 2026-09-15] The shot gate's TYPE/CLOSE channel, all three relayed
    // straight through: the engine is on the real-time path and never talks to the sidecar
    // itself. Unconditional (no inputTimedEnabled gate like armPose above): the shot gate serves
    // the METER reader on every path, and the blind 200 ms type grace exists on both the NO METER
    // and the METER BACKSTOP clocks. Each is fenced to one emission per physical epoch inside the
    // engine, so these connections add no per-tick or per-frame traffic -- at most two sidecar
    // lines per shot (an optional re-type, and exactly one close).
    connect(&automation_, &AutomationEngine::shotGateShotType, this,
            [this](quint64 physicalShotEpoch, const QString& shotType, bool rhythm) {
        remotePlay_.armMeterGate(QStringLiteral("type_upgrade"), physicalShotEpoch,
                                 shotType, rhythm);
    });
    connect(&automation_, &AutomationEngine::shotGateRelease, this,
            [this](quint64 physicalShotEpoch, double releaseWallMsEpoch) {
        remotePlay_.sendShotGateRelease(physicalShotEpoch, releaseWallMsEpoch);
    });
    connect(&automation_, &AutomationEngine::shotGateDisarm, this,
            [this](quint64 physicalShotEpoch, const QString& reason) {
        remotePlay_.sendShotGateDisarm(physicalShotEpoch, reason);
    });
    // [RT-MED-04 / CL3-F4-007 2026-09-23] A completed METER press the meter never answered: the
    // CV-independent input to the meter-blind warning / DETECTION UNAVAILABLE latch.
    connect(&automation_, &AutomationEngine::meterPressUnanswered, this,
            [this](quint64 physicalShotEpoch, double holdMs) {
        observeMeterPressUnanswered(physicalShotEpoch, holdMs);
    });

    // Stage each release's timing intent, but do not teach the sidecar yet.  The existing seq-paired
    // submit block resolves this only after the active local controller route accepts the release.
    // A failed/mismatched delivery is consumed without a marker, so the latency oracle fails closed.
    connect(&automation_, &AutomationEngine::releaseMarker, this,
             [this](int seq, double wallMs, bool latencyCalibration,
                    double validationTargetPct, double validationTolerancePct,
                    quint64 physicalShotEpoch, quint64 shotAttempt) {
                 if (automation_.context().inputTimedShot) return; // no meter-learner labels
                 releaseMarkerDeliveryGate_.stage(
                     seq, wallMs, latencyCalibration,
                     validationTargetPct, validationTolerancePct,
                     physicalShotEpoch, shotAttempt);
             });

    // Controlled timing-latency calibration is independent from meter-colour training. Mirror
    // the engine's truth-only status into a low-fanout QML surface; QML never infers readiness
    // from a displayed number: native independently requires either a distinct planned-stop L2
    // validation or a fully converged posterior, plus a fresh estimator/source epoch.
    connect(&automation_, &AutomationEngine::latencyCalibrationStatusChanged, this,
            [this](bool active, bool ready, int acceptedSamples,
                   double measuredLeadMs, double posteriorSdMs) {
        latencyCalibrationActive_ = active;
        latencyCalibrationReady_ = ready;
        latencyCalibrationSamples_ = std::max(0, acceptedSamples);
        latencyCalibrationMeasuredLeadMs_ =
            std::isfinite(measuredLeadMs) && measuredLeadMs > 0.0 ? measuredLeadMs : 0.0;
        latencyCalibrationSdMs_ =
            std::isfinite(posteriorSdMs) && posteriorSdMs > 0.0 ? posteriorSdMs : 0.0;

        if (!latencyCalibrationStatusOverride_.isEmpty()) {
            latencyCalibrationStatus_ = latencyCalibrationStatusOverride_;
            latencyCalibrationStatusOverride_.clear();
        } else if (ready) {
            latencyCalibrationStatus_ = QStringLiteral(
                "Ready - measured lead %1 ms (SD %2 ms).")
                .arg(latencyCalibrationMeasuredLeadMs_, 0, 'f', 1)
                .arg(latencyCalibrationSdMs_, 0, 'f', 1);
        } else if (active && latencyCalibrationSamples_ >= latencyCalibrationTarget()) {
            latencyCalibrationStatus_ = QStringLiteral(
                "Diagnostic validation measured - checking route precision.");
        } else if (active) {
            latencyCalibrationStatus_ = latencyCalibrationSamples_ == 1
                ? QStringLiteral(
                    "Diagnostic stage one measured; waiting for a distinct validation sample.")
                : QStringLiteral(
                    "Diagnostic timing probe is waiting for a trustworthy rising meter.");
        } else if (latencyCalibrationSamples_ > 0) {
            latencyCalibrationStatus_ = QStringLiteral(
                "Timing is learning passively from confirmed local-route releases.");
        } else {
            latencyCalibrationStatus_ = QStringLiteral(
                "Verifying automatic timing authority for this controller and video route.");
        }
        emit latencyCalibrationChanged();
        // A sidecar/source epoch can lose authority without changing Remote Play's Running state.
        // Surface passive adaptation, but never turn a normal gameplay press into a probe.
        refreshPassiveLatencyAdaptationStatus(automation_.armed());
    });

    // RC-3/C3: surface a suppressed blind fire loudly (mirrors the P0.1 not-submitted fault log) so a
    // blind-fire session is visible in the log instead of silently mistiming on a dead frame.
    connect(&automation_, &AutomationEngine::blindFireSuppressed, this, [this](const QString& reason) {
        appendLog(reason);
    });

    // B2c/C5: engine diagnostics (currently the IDLE arm-gate reason transitions) -> the session
    // log, so "the bot stopped arming after a shot" is a grep for IDLE-GATE rather than a guess.
    // [ORION_LEAD_CONFLICT 2026-08-08] Routed through presentEngineDiagnosticLine so the frozen
    // grader's "Outcome identity: ... verdict=LATE grader_truth=0" artifact line can never
    // reach the Activity log leading with a bare verdict word again — reading exactly that word
    // as a real grade is what started the lost session. Every other line passes byte-identical.
    connect(&automation_, &AutomationEngine::engineDiagnostic, this, [this](const QString& line) {
        appendLog(presentEngineDiagnosticLine(line));
    });

    // [ORION_FUSED_FIRE] fused-posterior diagnostics (FusedShadow / FusedAnchorLearn lines) ->
    // the session log, so the shadow A/B is greppable alongside the Release lines.
    connect(&automation_, &AutomationEngine::fusedDiagnostic, this, [this](const QString& line) {
        appendLog(line);
    });

    // [ORION_PROBE] Relay each warmup pump-fake press marker to the sidecar's latency estimator,
    // AND arm the reader's hardware shot-gate for it.
    //
    // WHY THE ARM IS REQUIRED. simple_meter_reader.detect() is a hard no-op unless _shot_armed_hw
    // is set: it returns "gameplay_ineligible" without examining a single pixel. Only a PHYSICAL
    // controller edge sets that flag -- the sidecar's CV self-arm cannot, because it needs
    // result.detected, which needs read(), which needs the gate. The claim that the CV self-arm
    // covered probes was simply false, and it meant the probe feature could never have worked on
    // any machine: 32 probes on 2026-08-05 produced a VISIBLE on-screen meter every time and
    // rise_samples=0 every time, because nothing was looking.
    //
    // Bump the SHARED counter rather than forging a constant epoch: the sidecar only fires
    // notify_physical_shot_start on incoming_epoch > current_epoch, so a reused number would
    // silently suppress the reader reset on the next REAL shot.
    //
    // Deliberately NOT calling automation_.setPhysicalShotEpoch(): leaving the engine's epoch
    // behind the reader's stamp means the ownership test (result.gameplayStructureEpoch ==
    // shot_.physicalShotEpoch) can never match a probe detection. So the probe's meter reaches the
    // estimator and the telemetry, but can never seed timing or arm a shot -- which is what the
    // old comment claimed was already true.
    //
    // Ordering is safe: the arm goes out at press time, ~400ms before the meter renders. Timeout
    // is safe too: probes emit no release marker, so _settle_shot_gate never truncates a probe
    // window to the 3s post-release clamp that silently killed the manual-arm workaround.
    // The ORIGINAL marker relay, restored verbatim. Do NOT fold this into the arming lambda
    // below: this is a signal->slot connect whose delivery mode Qt chooses from the two objects'
    // thread affinity (queued when RemotePlaySession lives on another thread). Rewriting it as a
    // lambda that calls sendProbeMarker() inline silently converted that queued delivery into a
    // direct cross-thread call, the marker stopped reaching the estimator, and a probe run then
    // produced NO pending probe at all -- no label, and no expiry either, so it failed silently
    // in a way that looked like the reader was still broken. Measured 2026-08-05: the reader was
    // in fact acquiring probe meters correctly at the time (READER ACQUIRE epoch=7 and epoch=12).
    connect(&automation_, &AutomationEngine::probeMarker,
            &remotePlay_, &RemotePlaySession::sendProbeMarker);

    // Arming rides as a SEPARATE connection on the same signal. Qt delivers to every connection,
    // so the two are independent and neither can break the other.
    connect(&automation_, &AutomationEngine::probeMarker, this, [this](int, double, double) {
        // Same streamActive definition the physical arm site uses (remoteRunning_ OR an embedded
        // Chiaki surface). A narrower guard here would silently drop the arm in the embedded case
        // and reproduce the exact "meter on screen, nothing looking at it" failure.
        // Same authority as the physical arm site (see orion::meterGateArmAllowed):
        // a live capture preview is a live detection feed even when the Chiaki
        // input session is Error/absent.
        if (!orion::meterGateArmAllowed(
                remoteRunning_,
                chiakiEmbedStatus_ == QLatin1String("Embedded"),
                capturePreviewActive_,
                automationSecurityAllowed(),
                1u)) {
            return;
        }
        ++physicalShotEpochCounter_;
        if (physicalShotEpochCounter_ == 0) {
            ++physicalShotEpochCounter_;   // zero is the invalid wire sentinel
        }
        remotePlay_.armMeterGate(QStringLiteral("probe_edge"), physicalShotEpochCounter_);
    });

    // [ORION_FUSED_FIRE] persist the v4 learning fields: posthoc-taught appear->tip anchor
    // clocks + the one-shot lead re-baseline latch (mirrors the meterClockUpdated wiring).
    connect(&automation_, &AutomationEngine::fusedLearningUpdated, this,
            [this](const QMap<QString, double>& appearToTip, bool leadRebaselined) {
        LearningData data = config_.learning();
        data.shotTypeAppearToTipMs = appearToTip;
        data.leadRebaselined = leadRebaselined;
        // A re-baseline also shifted the global clocks — capture them in the same write.
        data.globalAppearToTipMs = automation_.config().globalAppearToTipMs;
        data.globalHoldToReleaseMs = automation_.config().globalHoldToReleaseMs;
        config_.saveLearning(data);
    });

    connect(&remotePlay_, &RemotePlaySession::setupMessage, this, [this](const QString& message) {
        backendMessage_ = message;
        // [CL2-P3-005 2026-09-23] Plain-language capture notices are customer events: also
        // write them to the optional orion_user.log sink (appendCustomerEvent calls appendLog).
        if (message.startsWith(QLatin1String("Capture: "))) {
            appendCustomerEvent(message);
        } else {
            appendLog(message);
        }
        emit statusChanged();
    });

    connect(&remotePlay_, &RemotePlaySession::helperResult, this, [this](const QString& operation, const QJsonObject& result) {
        const bool ok = result.value(QStringLiteral("ok")).toBool(false);
        if (operation == QLatin1String("discover") && ok) {
            // Two payload shapes are supported:
            //   * Legacy helper: { devices: [ { ip, name, ... }, ... ] }
            //   * New chiaki UDP discovery: { ip, name, type, found: [ ... ] }
            QString detectedIp = result.value(QStringLiteral("ip")).toString();
            QString detectedName = result.value(QStringLiteral("name")).toString();
            QString detectedType = result.value(QStringLiteral("type")).toString();
            if (detectedIp.isEmpty()) {
                const auto devices = result.value(QStringLiteral("devices")).toArray();
                if (!devices.isEmpty()) {
                    const auto first = devices.first().toObject();
                    detectedIp = first.value(QStringLiteral("ip")).toString();
                    detectedName = first.value(QStringLiteral("name")).toString();
                }
            }
            if (detectedIp.isEmpty()) {
                const auto found = result.value(QStringLiteral("found")).toArray();
                if (!found.isEmpty()) {
                    const auto first = found.first().toObject();
                    detectedIp = first.value(QStringLiteral("ip")).toString();
                    detectedName = first.value(QStringLiteral("name")).toString();
                    detectedType = first.value(QStringLiteral("type")).toString();
                }
            }
            // Prefer the chiaki-registered nickname when it matched a known MAC — this
            // is what tells us we picked the user's actual console rather than just
            // some other device on the LAN that happened to answer the broadcast.
            const QString matchedNick = result.value(QStringLiteral("matched_nickname")).toString();
            const bool matched = result.value(QStringLiteral("matched")).toBool(false) || !matchedNick.isEmpty();
            const auto allFound = result.value(QStringLiteral("found")).toArray();
            const int registeredCount = result.value(QStringLiteral("registered_count")).toInt(0);

            // Log every responder so the user can audit which device was selected and
            // why (especially useful when the LAN has more than one PlayStation).
            if (allFound.size() > 1) {
                appendLog(QStringLiteral("Discovery found %1 responders, %2 registered host(s) on file:")
                              .arg(allFound.size()).arg(registeredCount));
                for (const auto& v : allFound) {
                    const auto e = v.toObject();
                    appendLog(QStringLiteral("  - %1  %2  %3  state=%4  score=%5%6")
                                  .arg(e.value(QStringLiteral("ip")).toString(),
                                       e.value(QStringLiteral("type")).toString(),
                                       e.value(QStringLiteral("name")).toString(),
                                       e.value(QStringLiteral("state")).toString())
                                  .arg(e.value(QStringLiteral("score")).toInt())
                                  .arg(e.value(QStringLiteral("matched_nickname")).toString().isEmpty()
                                           ? QString()
                                           : QStringLiteral(" [registered:") + e.value(QStringLiteral("matched_nickname")).toString() + QStringLiteral("]")));
                }
            }

            if (!detectedIp.isEmpty()) {
                auto data = config_.data();
                const QString displayLabel = matched && !matchedNick.isEmpty()
                    ? matchedNick
                    : (detectedName.isEmpty()
                           ? (detectedType.isEmpty() ? QStringLiteral("PlayStation") : detectedType)
                           : detectedName);
                if (data.remotePlayConsoleIp != detectedIp) {
                    data.remotePlayConsoleIp = detectedIp;
                    const QString tag = matched ? QStringLiteral(" (registered console)") : QString();
                    persistConfig(data, QStringLiteral("Detected %1 at %2%3").arg(displayLabel, detectedIp, tag));
                } else {
                    const QString tag = matched ? QStringLiteral(" (registered)") : QString();
                    appendLog(QStringLiteral("Discovery confirmed %1 at %2%3 — no change.")
                                  .arg(displayLabel, detectedIp, tag));
                }
            } else {
                appendLog(QStringLiteral("No PS5/PS4 consoles responded to discovery on the local network."));
            }
        } else if (operation == QLatin1String("oauth-url") && ok) {
            backendMessage_ = QStringLiteral("Legacy OAuth flow is disabled. Use Chiaki registration.");
        } else if (operation == QLatin1String("add-profile") && ok) {
            const auto user = result.value(QStringLiteral("user")).toString();
            if (!user.isEmpty()) {
                auto data = config_.data();
                data.remotePlayProfile = user;
                persistConfig(data, QStringLiteral("Saved legacy profile %1").arg(user));
            }
        } else if (operation == QLatin1String("profiles") && ok) {
            appendLog(QStringLiteral("Legacy profile scan returned %1 item(s).").arg(result.value(QStringLiteral("count")).toInt()));
        } else if (operation == QLatin1String("register") && ok) {
            appendLog(QStringLiteral("Legacy pairing helper returned success for %1").arg(result.value(QStringLiteral("user")).toString()));
        } else if (operation == QLatin1String("check") && ok) {
            backendMessage_ = QStringLiteral("Chiaki ready: %1")
                                  .arg(result.value(QStringLiteral("path")).toString());
        }

        if (!ok) {
            backendMessage_ = formatHelperError(operation, result);
            if (operation == QLatin1String("test-session")) {
                remoteState_ = QStringLiteral("Error");
                remoteStatus_ = backendMessage_;
                remoteRunning_ = false;
            }
            appendLog(QStringLiteral("%1 failed: %2").arg(operation, backendMessage_));
        }
        emit statusChanged();
        emit settingsChanged();
    });

    connect(&automation_, &AutomationEngine::shotStateChanged, this, [this](const ShotContext& context) {
        shot_ = context;
        shotState_ = holdStateText(context.state);
        observeBotOwnership(context);
        // [ORION_METER_DELAY 2026-08-07] Late, NON-CAUSAL hint: it can extend an
        // engagement the raw physical edge already started (and, under the
        // OffenseDefense bypass policy since 2026-08-08, start one — see
        // MeterDelayController::setShotCycleActive). GreenWindow sits between
        // Holding and Releasing with the button still held, so it counts too.
        setMeterDelayShotCycle(context.state == HoldState::Holding
                               || context.state == HoldState::GreenWindow
                               || context.state == HoldState::Releasing);
        emit statusChanged();
    });
    connect(&automation_, &AutomationEngine::shotAborted, this, [this](const QString& reason) {
        appendLog(QStringLiteral("Shot automation aborted: %1").arg(reason));
        // [ORION_USER_LOG] (5b) plain-language abort line (translate the common token).
        // [ORION_ACTIVITY_FEED 2026-09-14] These go through appendCustomerEvent now,
        // so the same prose reaches the Activity feed whether or not the optional
        // orion_user.log file sink is enabled. The raw enum line above is
        // unchanged (tooling greps it) and is deny-listed out of the feed.
        {
            if (reason == QLatin1String("ls_cancel")) {
                appendCustomerEvent(QStringLiteral(
                    "Shot canceled — you moved the stick to change the play."));
            } else if (reason == QLatin1String("live_tip_deadline_missed")
                       && shotLeadConflictActiveNow()) {
                // [ORION_LEAD_CONFLICT 2026-08-08] While the Shot Lead x Tip Timing pair is
                // structurally unschedulable, this abort is not a transient miss — it is the
                // certain outcome of the two settings, and printing the raw enum here is how a
                // whole session died shot by shot with nothing naming the cause. Say the cause
                // and the fix. The conflict test runs against the CURRENT pair, so a genuine
                // transient miss on a schedulable pair keeps the raw reason below.
                // [COPY-FIX 2026-09-23 CW-9/EA-27/NEW-A7] On the Shot Lead card's own 1-100
                // scale, no "schedule"/"deadline" jargon; the ms values stay in the raw
                // "Shot automation aborted:" engineering line above.
                appendCustomerEvent(QStringLiteral(
                    "Shot not taken — Shot Lead %1 is too high for your jumpshot, so Venice "
                    "can't release in time. Lower it to %2 or less (or press Reset on Tip Timing).")
                                        .arg(ui_notifications::shotLeadSliderValue(actuationLeadMs()))
                                        .arg(ui_notifications::shotLeadSliderValue(shotLeadMaxUsableMs())));
            } else {
                // [COPY-FIX 2026-09-23 NEW-A7] Never print the raw reason enum to customers.
                appendCustomerEvent(ui_notifications::customerShotNotTakenText(reason));
            }
        }
    });
    connect(&automation_, &AutomationEngine::releaseIssued, this, [this](const ShotContext& context) {
        // Release does not invalidate detector evidence. A genuine receding meter
        // remains the bot's live eyes, so the normal freshness/identity policy below
        // exclusively owns when the box disappears.
        emit statusChanged();
        // Rich release telemetry: the bare "fill %" line couldn't distinguish a
        // green-window timed release from a max-hold dump at 100%. These fields
        // prove which path fired (plan/reason), what it aimed at (target vs green
        // window), and how fresh the meter was (frame age) on every shot.
        appendLog(QStringLiteral("Release issued: fill %1% target %2% (green %3-%4) "
                                 "plan=%5 reason=%6 code=%7 presence=%8 src=%9 "
                                 "conf=%10 age=%11ms offset=%12ms seq=%14 shot=%13")
                      .arg(context.fillPct, 0, 'f', 1)
                      .arg(context.targetPct, 0, 'f', 1)
                      .arg(context.greenStartPct, 0, 'f', 1)
                      .arg(context.greenEndPct, 0, 'f', 1)
                      .arg(context.releasePlan, context.releaseReason,
                           context.releaseReasonCode, context.detectionPresence,
                           context.detectorSource)
                      .arg(context.confidence, 0, 'f', 2)
                      .arg(context.frameAgeMs, 0, 'f', 0)
                      .arg(context.networkOffsetMs, 0, 'f', 1)
                      .arg(context.shotType)
                      .arg(context.releaseSeq));
        // Stage the plain-language success text, but do not emit it yet.
        // releaseIssued proves timing intent only; the exact transport result is
        // resolved by the seq-paired Release submit block below.
        userReleaseTracker_.stage(context.releaseSeq, context.fillPct, context.targetPct,
                                  context.shotType, rttVerified(), rttMs());
        // SEPARATE seq-paired line (the "Release issued:" line above is byte-for-byte
        // unchanged — correlate_releases.py parses it; never edit a parsed line, add one).
        // Per-release path attribution so a Go-To release is mechanically traceable to its
        // block: greenConfirmed=1/targetMode=green_tip => block 1 (green) rode the tip;
        // greenConfirmed=0/targetMode=meter_full => block 2 (velocity) fired at the floor.
        // greenConfirmFill/Ms expose the green-confirmation race. All values are single
        // space-free tokens; shot=<type> is LAST (it may contain spaces, like the issued line).
        //
        // [ORION_ATTRIBUTION_RISE] READ THIS BEFORE CONCLUDING ANYTHING FROM expectedRise /
        // withinReach ON A PRE-2026-08-31 LOG. Both fields were written ONLY by processHolding's
        // legacy vision-crossing block, and processHolding delegates every autonomous-vision shot
        // to processAutonomousLiveMeterHolding and returns before reaching that block — so on a
        // shipped build they read expectedRise=0.0 withinReach=0 on 100% of fires REGARDLESS of
        // timing quality. That is a stamping gap, not a dormant lever: the live path's aim is the
        // tip (targetPct 100) with the rate compensation applied in the TIME domain as
        // tipAbs - lead, and withinReach was never an aim term in either path — it is a
        // reachability gate. Both paths now stamp expectedRise = velocity x lead (unclamped), so
        // 100 - expectedRise is the fill the command should have been issued at to land on the
        // tip; compare it with the `fill` on the Release issued line above.
        appendLog(QStringLiteral("Release attribution: seq=%1 greenConfirmed=%2 targetMode=%3 "
                                 "greenWidth=%4 greenConfirmFill=%5 greenConfirmMs=%6 vel=%7 "
                                 "crossingEta=%8 expectedRise=%9 withinReach=%10 shot=%11")
                      .arg(context.releaseSeq)
                      .arg(context.greenConfirmedAtRelease ? 1 : 0)
                      .arg(context.targetModeAtRelease)
                      .arg(context.greenWidthAtReleasePct, 0, 'f', 1)
                      .arg(context.greenConfirmFillPct, 0, 'f', 1)
                      .arg(context.greenConfirmMs, 0, 'f', 0)
                      .arg(context.releaseVelocityPctMs, 0, 'f', 4)
                      .arg(context.releaseCrossingEtaMs, 0, 'f', 0)
                      .arg(context.expectedRiseAtReleasePct, 0, 'f', 1)
                      .arg(context.withinReachAtRelease ? 1 : 0)
                      .arg(context.shotType));
        // NEW seq-paired line (add-only, key=value — never edit a parsed line): meter-appear
        // ANCHOR timing, the dial instrument for the re-anchored clock. anchorAppearMs =
        // firstMeterSeen - holdStart (the fade WIND-UP; -1 if the meter never genuinely appeared).
        // appearToRelMs = release - firstMeterSeen (the ACTUAL appear->release segment = the clock
        // that fired; the green Standstill released at ~appear+100ms, the late fades at +200-280ms).
        // holdToRelMs = release - holdStart. fillAtRel / peakFill bracket the release on the rise
        // (peak >> fillAtRel = released well before the VISUAL peak, expected because the view lags
        // the true meter; a peak that then bounced back is the LATE signature). plannedClockMs = the
        // feedforward clock value used (-1 on a vision release). shot=<type> LAST (may contain spaces).
        const double anchorAppearMs = (context.meterSeenThisShot && context.firstMeterSeenMs >= 0.0
                                       && context.holdStartMs >= 0.0)
            ? context.firstMeterSeenMs - context.holdStartMs : -1.0;
        const double appearToRelMs = (context.meterSeenThisShot && context.firstMeterSeenMs >= 0.0)
            ? context.releaseTriggerMs - context.firstMeterSeenMs : -1.0;
        const double holdToRelMs = context.holdStartMs >= 0.0
            ? context.releaseTriggerMs - context.holdStartMs : -1.0;
        // [2026-09-11] every per-shot deadline displacement rides on this line so a dataset
        // built from it can never miss one (the 09-09 press-latency trim moved 19 shots by up to
        // 30 ms and the verdict dataset was built without a column for it).
        // [ORION_VISION_HOLD_BAND 2026-09-15] APPEND-ONLY (key=value parsers are unaffected; every
        // field above keeps its position, including pressTrimMs, which has sat after shot=<type>
        // since 2026-09-11). hold_band names whether
        // the vision path's release INSTANT was clamped onto the press-anchored hold band for
        // this shot, and which edge: late (the prediction sat past press + law + band and was
        // pulled back), early (it sat before press + law − band and was held until it), or none.
        // A value other than none is also the marker-suppression and learner-fence record: that
        // release taught neither the latency estimator, nor no_meter_hold_by_type, nor the phase
        // constant, so a dataset built from this line can separate "the aim moved" from "the
        // band moved it" without joining another line.
        appendLog(QStringLiteral("Release timing: seq=%1 code=%2 anchorAppearMs=%3 appearToRelMs=%4 "
                                 "holdToRelMs=%5 plannedClockMs=%6 fillAtRel=%7 peakFill=%8 shot=%9 "
                                 "pressTrimMs=%10 hold_band=%11")
                      .arg(context.releaseSeq)
                      .arg(context.releaseReasonCode)
                      .arg(anchorAppearMs, 0, 'f', 0)
                      .arg(appearToRelMs, 0, 'f', 0)
                      .arg(holdToRelMs, 0, 'f', 0)
                      .arg(context.plannedFlickMs, 0, 'f', 0)
                      .arg(context.fillPct, 0, 'f', 1)
                      .arg(context.peakFillPct, 0, 'f', 1)
                      .arg(context.shotType)
                      .arg(context.pressLatencyTrimMs, 0, 'f', 1)
                      .arg(context.holdBandKind.isEmpty() ? QStringLiteral("none")
                                                          : context.holdBandKind));
        // NEW seq-paired line (add-only): SHADOW-MODE autonomous-model decision
        // (autonomous_vision_shadow). COMPUTED but it did NOT control this release. The
        // global-velocity phase-aligned model: shadowAppearToRelMs = when it WOULD have fired,
        // relative to meter-appear (-1 = its deadline never came due this shot). deltaMs =
        // actualAppearToRel - shadow (>0 = autonomous would have fired EARLIER than the live path).
        // Cross-reference with the post-release verdict (Shot outcome:) to A/B the model BEFORE it is
        // ever allowed to take control. Only emitted when shadow mode is enabled (keeps logs clean).
        if (config_.data().autonomousVisionShadow) {
            const double shadowAppearToRelMs = (context.shadowFireMs >= 0.0 && context.firstMeterSeenMs >= 0.0)
                ? context.shadowFireMs - context.firstMeterSeenMs : -1.0;
            const double shadowDeltaMs = (shadowAppearToRelMs >= 0.0 && appearToRelMs >= 0.0)
                ? appearToRelMs - shadowAppearToRelMs : 0.0;
            appendLog(QStringLiteral("Shadow timing: seq=%1 shadowAppearToRelMs=%2 deltaMs=%3 "
                                     "shadowFill=%4 vg=%5 latencyMs=%6 shot=%7")
                          .arg(context.releaseSeq)
                          .arg(shadowAppearToRelMs, 0, 'f', 0)
                          .arg(shadowDeltaMs, 0, 'f', 0)
                          .arg(context.shadowFireFillPct, 0, 'f', 1)
                          .arg(context.shadowVgPctMs, 0, 'f', 3)
                          .arg(context.effectiveLatencyMs, 0, 'f', 0)
                          .arg(context.shotType));
        }
        // NEW seq-paired line (existing lines are parsed — never edit one, add one): sub-tick
        // scheduler + network sample-and-hold attribution. scheduled=1 -> the precise fire
        // thread submitted this release; deltaMs = actual fire - scheduled deadline (target
        // sub-ms). wifi/jitter = the auto wifi-mode state latched at shot start; clockVisionDiv
        // = feedforward-clock vs vision-crossing disagreement at release.
        // [ORION_TIP_FRAME_NATIVE 2026-09-17] APPEND-ONLY (key=value parsers are unaffected; every
        // field above keeps its name and position). The two fire instants this shot's last
        // SUCCESSFUL vision arm held, in absolute engine ms: aligned_ms is what was actually armed
        // (the frame-centred target less the lead) and unaligned_ms is what the bare tip alone
        // would have armed (tip - lead). aligned_ms - unaligned_ms IS the frame-centre offset the
        // console received, so a graded batch can price frame-native firing against the banner
        // without reconstructing the tip -- which is exactly what docs/POLL_PHASE_TRACKER.md §10
        // had to do offline, and why the offset's disappearance went unnoticed for two weeks.
        // Both are -1 on a shot that never reached a vision arm (blind, feedforward, pose).
        appendLog(QStringLiteral("Scheduled fire: seq=%1 scheduled=%2 deltaMs=%3 wifi=%4 "
                                 "jitterMs=%5 heldOffsetMs=%6 clockVisionDivMs=%7 "
                                 "aligned_ms=%8 unaligned_ms=%9")
                      .arg(context.releaseSeq)
                      .arg(context.firedByScheduler ? 1 : 0)
                      .arg(context.scheduledFireDeltaMs, 0, 'f', 2)
                      .arg(context.wifiMode ? 1 : 0)
                      .arg(context.networkJitterMs, 0, 'f', 1)
                      .arg(context.networkOffsetMs, 0, 'f', 1)
                      .arg(context.clockVisionDivergenceMs, 0, 'f', 1)
                      .arg(context.fireAlignedFireAtMs, 0, 'f', 3)
                      .arg(context.fireUnalignedFireAtMs, 0, 'f', 3));
        // NEW seq-paired line (add-only, key=value): per-shot detection-sample census.
        // Attributes a detector-authority abort to the gate that starved it:
        // fresh=0 + staleMem high = sidecar fed only held/echoed fills (detector or
        // feed-gate side); samples=0 = no payloads at all; confLow/staleFrame high =
        // native gates rejecting. firstFreshMs/firstMeterMs are ms after hold start
        // (-1 = never); minFreshFill = lowest fresh-accept fill this shot (-1 = none).
        appendLog(QStringLiteral("Release detsummary: seq=%1 samples=%2 fresh=%3 staleMem=%4 "
                                 "confLow=%5 staleFrame=%6 nodet=%7 firstFreshMs=%8 "
                                 "firstMeterMs=%9 minFreshFill=%10 shot=%11")
                      .arg(context.releaseSeq)
                      .arg(context.detSamplesTotal)
                      .arg(context.detFreshAccepts)
                      .arg(context.detStaleOrMemory)
                      .arg(context.detConfLow)
                      .arg(context.detStaleFrameDrop)
                      .arg(context.detNotDetected)
                      .arg(context.firstFreshAcceptMs >= 0.0
                               ? context.firstFreshAcceptMs - context.holdStartMs : -1.0, 0, 'f', 0)
                      .arg(context.firstMeterSeenMs >= 0.0
                               ? context.firstMeterSeenMs - context.holdStartMs : -1.0, 0, 'f', 0)
                      .arg(context.minFreshFillPct > 100.0 ? -1.0 : context.minFreshFillPct, 0, 'f', 1)
                      .arg(context.shotType));
        // NEW seq-paired line (add-only, key=value): the BLIND-FIRE verdict. blindFire=1
        // means the release fired on the feedforward animation clock (code=feedforward_target)
        // — vision could not time it. staleMs = ms since the last FRESH accept at the instant
        // of release (-1 = vision never went fresh this shot); fresh=N is the fresh-accept
        // count; lastFresh=1 if the very last sample was a clean accept. One grep
        // ("Release freshness") answers "did this shot fire blind, and how stale was the meter
        // when it did" without joining the issued + detsummary lines. shot=<type> is LAST.
        const double staleMs = context.lastFreshAcceptMs >= 0.0
                ? context.releaseTriggerMs - context.lastFreshAcceptMs : -1.0;
        appendLog(QStringLiteral("Release freshness: seq=%1 blindFire=%2 code=%3 lastFresh=%4 "
                                 "fresh=%5 staleMs=%6 memTrusted=%7 shot=%8")
                      .arg(context.releaseSeq)
                      .arg(context.releaseReasonCode == QStringLiteral("feedforward_target") ? 1 : 0)
                      .arg(context.releaseReasonCode)
                      .arg(context.lastSampleFreshAccept ? 1 : 0)
                      .arg(context.detFreshAccepts)
                      .arg(staleMs, 0, 'f', 0)
                      .arg(context.detMemoryTrusted)
                      .arg(context.shotType));
        // NEW seq-paired line (add-only, key=value): vision-vs-clock honesty at the release
        // instant. visionFreshAtRel=1 = the last sample was a GENUINE fresh accept still
        // inside the freshness window when the trigger pulled (blindFire=1 + visionFreshAtRel=1
        // = the clock fired while healthy vision was in hand). reachHeldMs = how long a DUE
        // feedforward clock was held by the reachability gate before release (0 = never held;
        // large = the reach-floor opening, not the clock, decided the timing). tipGateDeferMs
        // = how long the tip gate deferred the due clock toward the predicted crossing (0 =
        // no defer). anchorValid=1 = the meter-appear anchor VALIDATED (episode gates) this
        // shot. shot=<type> is LAST (it may contain spaces).
        appendLog(QStringLiteral("Release vision: seq=%1 visionFreshAtRel=%2 reachHeldMs=%3 "
                                 "tipGateDeferMs=%4 anchorValid=%5 shot=%6")
                      .arg(context.releaseSeq)
                      .arg(context.visionFreshAtRelease ? 1 : 0)
                      .arg(context.reachHoldStartMs >= 0.0
                               ? context.releaseTriggerMs - context.reachHoldStartMs : 0.0, 0, 'f', 0)
                      .arg(context.tipGateDeferMs, 0, 'f', 0)
                      .arg(context.anchorValidMs >= 0.0 ? 1 : 0)
                      .arg(context.shotType));
        // NEW seq-paired line (add-only, key=value): Tempo / mode-keyed calibration attribution.
        // mode=TempoSquare|ButtonShot|GoToStick; bucket=the mode-keyed timing bucket the dial+clock
        // came from (spaces->_); path=releaseReasonCode (feedforward_target=open-loop clock, else a
        // vision path); plannedFlickMs=the chosen open-loop deadline relative to the anchor (-1 if a
        // vision release). Pair with "Release issued" (same seq) for the release FILL% — a mid-meter
        // fill on a TempoSquare line is the "flick landed mid-meter" signature. shot=<type> LAST.
        const QString modeName = context.mode == ShotMode::TempoSquare ? QStringLiteral("TempoSquare")
            : context.mode == ShotMode::GoToStick ? QStringLiteral("GoToStick")
            : context.mode == ShotMode::TempoStick ? QStringLiteral("TempoStick")
            : QStringLiteral("ButtonShot");
        // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] APPEND-ONLY (key=value parsers are unaffected;
        // every field above keeps its position, shot=<type> included -- it may contain spaces, so
        // release_style is appended AFTER it exactly as pressTrimMs is on the Release timing
        // line). release_style names which release EDGE this shot actually emitted: flick (the
        // opposing full-scale deflection) or letgo (the stick driven to neutral at the same
        // instant). It is the setting in force at the release, so a returned log can separate
        // "the timing was wrong" from "the game did not read the edge".
        appendLog(QStringLiteral("Release tempo: seq=%1 mode=%2 bucket=%3 path=%4 plannedFlickMs=%5 "
                                 "flickDir=%6 shot=%7 release_style=%8")
                      .arg(context.releaseSeq)
                      .arg(modeName)
                      .arg(QString(context.bucketKey).replace(QLatin1Char(' '), QLatin1Char('_')))
                      .arg(context.releaseReasonCode)
                      .arg(context.plannedFlickMs, 0, 'f', 0)
                      // Both Tempo representations use one invariant gather-down /
                      // flick-up packet for every shot type. "-" means this mode has
                      // no generated tempo flick.
                      // [ORION_TEMPO_RELEASE_STYLE] flickDir is deliberately LEFT ALONE -- it is a
                      // parsed field and this change is append-only. release_style at the end of
                      // the line is what says whether that "up" was a flick or a let-go.
                      .arg(context.mode == ShotMode::TempoSquare
                                   || context.mode == ShotMode::TempoStick
                               ? QStringLiteral("up")
                               : QStringLiteral("-"))
                      .arg(context.shotType)
                      // The ENGINE's effective style, not the file's: the env override
                      // (ORION_TEMPO_RELEASE_STYLE) is applied in applyConfig, and the log must
                      // report the edge that was actually emitted.
                      .arg(QString::fromLatin1(tempoReleaseStyleToken(
                          tempoReleaseStyleFromString(
                              automation_.config().tempoReleaseStyle)))));
        // Pair engine telemetry with the exact output-route proof. A precise fire already carries
        // its sequence-tagged local ACK; a non-precise release is classified from this GUI tick's
        // active route below. Never infer precise delivery from a later de-duplicated pipe write.
        pendingSubmitSeq_ = context.releaseSeq;
        pendingSubmitPhysicalShotEpoch_ = context.physicalShotEpoch;
        pendingSubmitShotAttempt_ = context.armToken;
        pendingSubmitScheduleToken_ = context.releaseScheduleToken;
        pendingSubmitRouteGeneration_ = context.releaseScheduleRouteGeneration;
        pendingSubmitRoute_ = context.releaseScheduleRoute;
        pendingSubmitDeliveryStage_ = confirmedPreciseFireStage_;
        pendingSubmitFireToken_ = confirmedPreciseFireToken_;
        pendingSubmitTransportSeq_ = confirmedPreciseFireTransportSeq_;
        pendingSubmitSnapshot_ = confirmedPreciseFireSnapshot_;
        // [ORION_TIP_FRAME_NATIVE 2026-09-17] The shot's own grid, its last vision arm's target
        // rule, and the lead that arm spent — taken HERE because the engine resets the shot (and
        // with it framePhase) well before the post-submit line below is written.
        pendingSubmitFrameGrid_ = ReleaseFrameGridSnapshot{};
        pendingSubmitFrameGrid_.grid = context.framePhase.estimate();
        pendingSubmitFrameGrid_.fireTargetMode = context.fireTargetMode;
        pendingSubmitFrameGrid_.leadMs = context.effectiveLatencyMs;
        pendingSubmitFrameGrid_.alignedFireAtMs = context.fireAlignedFireAtMs;
        pendingSubmitFrameGrid_.unalignedFireAtMs = context.fireUnalignedFireAtMs;
        confirmedPreciseFireStage_ = PreciseFireDeliveryStage::None;
        confirmedPreciseFireToken_ = 0;
        confirmedPreciseFireTransportSeq_ = 0;
        confirmedPreciseFireSnapshot_ = {};
    });
    // MUST remain queued: connectVirtualController()/teardown emit this signal while their caller
    // holds submitMutex_. syncEngineArmed()->disarmPreciseFire() takes that same fence, so a direct
    // callback would self-deadlock. Every disconnect path revokes/resets before mutating the target;
    // this queued beat synchronizes the public route state once the mutation lock has been released.
    connect(&controller_, &VirtualController::statusChanged, this, [this](bool connected, const QString& message) {
        // Only log here. The user-facing controllerStatus_ string is owned by
        // pollPhysicalController() so it always reflects whether a real
        // physical pad is plugged in (which is what the user actually cares about).
        appendLog(QStringLiteral("Virtual pad: %1").arg(message.left(120)));
        // The virtual target is a hard release-authority boundary. A removal must revoke a
        // deadline already copied to the fire worker immediately; a successful reconnect may
        // re-arm only when Remote Play is genuinely Running and all other safety gates pass.
        syncEngineArmed();
        if (!connected) {
            // This queued callback runs on the controller's GUI thread. Reset here instead of relying
            // on the next physical poll, which may never reach AutomationEngine::process when the
            // physical pad disappeared in the same PnP transition.
            automation_.reset();
            // The direct pipe can still own a held button when ViGEm goes away.
            // Normal submit polling is gated on the virtual target, so teardown
            // must clear that state even when the optional timeout watchdog is off.
            neutralizeOwnedInput();
            shot_ = automation_.context();
            shotState_ = holdStateText(shot_.state);
            observeBotOwnership(shot_);
            // [ORION_METER_DELAY 2026-08-07] AutomationEngine::reset() emits no
            // shotStateChanged, so clear the shot-cycle hint here or it can go
            // stale-true until the controller's stuck-state watchdog fires.
            setMeterDelayShotCycle(false);
            // The virtual pad was unplugged — refresh status immediately.
            emit statusChanged();
        }
    }, Qt::QueuedConnection);


    connect(&networkBridge_, &NetworkBridge::telemetryUpdated, this, [this](const TelemetrySnapshot& snap) {
        // Packet observations intentionally carry no RTT/court/tick authority.
        // Preserve the sidecar's sampled authority independently of diagnostic
        // bridge flow state. Only RemotePlaySession may revoke that authority.
        const TelemetrySnapshot previous = telemetry_;
        const bool preserveCourtRtt = previous.rttTargetVerified;
        telemetry_ = snap;
        // The bridge's classifier deliberately publishes only diagnosticCourtIp and leaves
        // courtIp EMPTY, so an empty value here is absence of evidence -- never evidence that the
        // court was lost. Gating its preservation on rttTargetVerified therefore let a bridge beat
        // REVOKE the court identity whenever RTT was merely unverified, contradicting this block's
        // own contract that only RemotePlaySession may revoke it.
        //
        // 2026-08-03 measured consequence: courtIp alternated empty (bridge beat) / set (sidecar
        // beat). Kept because it is a genuine telemetry-merge bug independent of that actuator:
        // a source that structurally never carries the field must not be able to erase it.
        if (telemetry_.courtIp.isEmpty()) {
            telemetry_.courtIp = previous.courtIp;
        }
        // Same contract for playingGame. applyPassiveSnapshot() writes it from the bridge's OWN
        // packet classifier (NetworkBridge.cpp:675) and zeroes it on reset (:620), so a quiet
        // moment in the passive flow would otherwise revoke "in a game" and close the same
        // meter-delay gate. The bridge may ASSERT the flow it can see; only RemotePlaySession --
        // which owns the actual session state and reasserts it on every sidecar beat -- may
        // revoke it. Without this the intercept still thrashed after the courtIp fix.
        if (!telemetry_.playingGame && previous.playingGame) {
            telemetry_.playingGame = true;
        }
        if (preserveCourtRtt) {
            telemetry_.courtIp = previous.courtIp;
            telemetry_.rttMs = previous.rttMs;
            telemetry_.rttTargetVerified = true;
            telemetry_.jitterMs = previous.jitterMs;
            telemetry_.offsetMs = previous.offsetMs;
            telemetry_.syncAdjustMs = previous.syncAdjustMs;
            telemetry_.syncConfidence = previous.syncConfidence;
            telemetry_.syncSource = previous.syncSource;
            telemetry_.syncActive = previous.syncActive;
            telemetry_.tickerLatencyMs = previous.tickerLatencyMs;
            telemetry_.tickPhaseVerified = previous.tickPhaseVerified;
            telemetry_.tickPhaseConfidence = previous.tickPhaseConfidence;
            telemetry_.tickPhaseObservedEpochMs = previous.tickPhaseObservedEpochMs;
        }
        // [ORION_METER_DELAY 2026-08-07] Same court-IP gate as the sidecar
        // telemetry beat: the diagnosticCourtIp merge above may have discovered
        // (or lost) the court identity.
        meterDelay_.setCourtIpKnown(!telemetryCourtIp().isEmpty());
        // [ORION_TEMPO_BRIDGE_LIVE task #36] A bridge telemetry beat is the literal
        // "health telemetry present within N ms" leg of the remap gate: it proves the
        // packet-bridge backend process is alive. It cannot open the gate on its own —
        // the intercept-applying echo leg is still required.
        automation_.noteTempoRemapBridgeHealthBeat();
        automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
        notifyTelemetryPropertiesAtHumanCadence(
            QDateTime::currentMSecsSinceEpoch());
    });
    connect(&networkBridge_, &NetworkBridge::playerCountChanged, this, [this](int count) {
        playerCount_ = count;
        emit telemetryChanged();
    });
    connect(&networkBridge_, &NetworkBridge::packetObserved, this,
            [this](const QString& srcIp, const QString& dstIp, int srcPort, int dstPort, int size, double ts) {
#ifdef ORION_PRODUCTION_BUILD
        // The current loopback bearer token authenticates Orion to the listener,
        // but not the listener/service to Orion. Keep these packets available to
        // NetworkBridge diagnostics while compiling their RTT/court/tick feed out
        // of production until privileged service identity is independently proven.
        constexpr bool explicitDevelopmentExperiment = false;
#else
        // Development-only escape hatch for controlled packet-timing experiments.
        // This environment lookup and its name do not compile into production.
        static const bool explicitDevelopmentExperiment =
            qEnvironmentVariableIntValue("ORION_DEV_ALLOW_UNVERIFIED_BRIDGE_TIMING") == 1;
#endif
        (void)packet_bridge_authority::forwardToTimingIfAuthorized(
            packet_bridge_authority::ServiceIdentityTrust::Unverified,
            explicitDevelopmentExperiment,
            [this, &srcIp, &dstIp, srcPort, dstPort, size, ts]() {
                remotePlay_.observePacket(srcIp, dstIp, srcPort, dstPort, size, ts);
            });
    });
    connect(&networkBridge_, &NetworkBridge::connectionChanged, this, [this](bool ok, const QString& msg) {
        // [ORION_ACTIVITY_FEED 2026-09-14] ROOT CAUSE of 470 identical
        // "Network bridge: connected - Bridge: [WinError 5] Access is denied."
        // lines in one hour: NetworkBridge::handleMessage maps EVERY `error`
        // event from the service to connectionChanged(connected_.load(), ...).
        // While the loopback link is authenticated but WinDivert cannot open
        // without elevation, the service answers `error` on every verb, so a
        // STATE-UNCHANGED signal fired every few seconds and each one logged.
        // connectionChanged is a state signal, so log state CHANGES only: the
        // same (ok, message) pair never logs twice in a row. Everything below
        // (court-IP revocation, bridge restart, the QML notifies) still runs on
        // every signal — only the log line is deduplicated.
        const QString bridgeState = QStringLiteral("%1 - %2")
                                        .arg(ok ? QStringLiteral("connected")
                                                : QStringLiteral("disconnected"),
                                             msg);
        if (bridgeState != lastNetworkBridgeStateLine_) {
            lastNetworkBridgeStateLine_ = bridgeState;
            appendLog(QStringLiteral("Network bridge: %1").arg(bridgeState));
        }
        if (!ok) {
            // The bridge owns display-only diagnostics, so disconnect revokes
            // only that identity and cannot mutate sidecar timing authority.
            telemetry_.diagnosticCourtIp.clear();
            // [ORION_METER_DELAY 2026-08-07] Diagnostic identity revoked without
            // a telemetry beat — re-evaluate the court-IP session gate.
            meterDelay_.setCourtIpKnown(!telemetryCourtIp().isEmpty());
            // [VENICENET WAVE 2B] The applied-delay echo is owned by the VeniceNet
            // DLL snapshot now, not this (diagnostics-only) bridge connection, so a
            // NetworkBridge drop no longer resets it — the DLL tracks the actuation
            // service independently.
            ensurePacketBridgeRunning();
        }
        // packetCaptureActive is a telemetry-notified Q_PROPERTY. Without this
        // signal QML could remain stuck on "starting" after the authenticated
        // bridge had already become active.
        emit telemetryChanged();
        emit statusChanged();
        // [ORION_METER_DELAY_ARM_STATE] meterDelayStatusText reads the bridge link
        // state (connected/waiting), so a connect/disconnect must refresh the card.
        emit meterDelayStatusTextChanged();
    });

    networkBridge_.setConsoleIp(config_.data().remotePlayConsoleIp);
    // [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in flag is deleted:
    // everything network-side ships ON, so the loopback client and the WinDivert
    // bridge come up whenever the network feature OR Meter Delay is enabled — both
    // default ON (see packetBridgeLinkConfigured). Unverified bridge packets are
    // still never timing authority in production.
    if (packetBridgeLinkConfigured(config_.data().networkEnabled,
                                   config_.data().meterDelayEnabled)) {
        ensurePacketBridgeRunning();
        networkBridge_.start();
    } else {
        appendLog(QStringLiteral("Packet bridge: off (optional diagnostics; unverified packet data is never production timing authority)."));
    }

    refreshTimer_.setInterval(1000);
    connect(&refreshTimer_, &QTimer::timeout, this, &OrionAppController::refreshSecurity);
    refreshTimer_.start();

    // Batched log flush: keeps the synchronous disk write + logsChanged emit off the per-call
    // (per-tick/per-frame) path. appendLog() only appends in memory; this drains it every 200ms.
    logFlushTimer_.setTimerType(Qt::CoarseTimer);
    logFlushTimer_.setInterval(200);
    connect(&logFlushTimer_, &QTimer::timeout, this, &OrionAppController::flushPendingLogs);
    logFlushTimer_.start();

    // HUD freshness must expire independently of decoder/detector callbacks. If
    // the frame pipe stalls, no frameChanged signal arrives to make QML re-read
    // meterMetricsValid(), so a dedicated single-shot timer owns the 120ms edge.
    meterMetricsExpiryTimer_.setSingleShot(true);
    meterMetricsExpiryTimer_.setTimerType(Qt::PreciseTimer);
    connect(&meterMetricsExpiryTimer_, &QTimer::timeout, this, [this]() {
        const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
        if (!live_hud::meterMetricFresh(meterMetricsCurrent_, measuredMeterAtMs_,
                                        nowMs, kMeterConfirmFreshMs_)) {
            clearMeterMetrics();
            return;
        }
        // A precise timer can still wake a millisecond early. Rearm for only the
        // remaining interval so the sample cannot become permanently "fresh".
        const qint64 ageMs = nowMs - measuredMeterAtMs_;
        meterMetricsExpiryTimer_.start(
            static_cast<int>(std::max<qint64>(1, kMeterConfirmFreshMs_ - ageMs)));
    });

    inputPollTimer_.setTimerType(Qt::PreciseTimer);
    inputPollTimer_.setInterval(4);
    connect(&inputPollTimer_, &QTimer::timeout, this, &OrionAppController::pollPhysicalController);
    // AutomationEngine defaults armed for standalone/unit use. The application starts without a
    // Remote Play route, so establish the production authority gate BEFORE the first 4ms input poll.
    syncEngineArmed();
    inputPollTimer_.start();
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] One cheap registry walk so the
    // "Fix controller USB power" offer and the truthful absent-pad advice are
    // ready from the first frame (also re-run on every Connect refusal).
    refreshControllerUsbPowerScan();

    lightbarEffectTimer_.setTimerType(Qt::CoarseTimer);
    lightbarEffectTimer_.setInterval(180);
    connect(&lightbarEffectTimer_, &QTimer::timeout, this, &OrionAppController::updateLightbarEffect);
    controllerLedStatus_ = QStringLiteral("LED waiting for controller");
    // Kick once so a persisted lightbar_enabled=true starts the effect ticker at
    // launch instead of waiting for the first settings change.
    applyControllerLightbar(true);

    // Detection-overlay hue cycle. Created here but NOT started: it starts only
    // once RGB mode is on and a preview is being rendered (see
    // refreshMeterOverlayEffectTimer), so it is inert for every user who leaves
    // the overlay on a fixed colour.
    overlayEffectTimer_.setTimerType(Qt::CoarseTimer);
    overlayEffectTimer_.setInterval(250);
    connect(&overlayEffectTimer_, &QTimer::timeout,
            this, &OrionAppController::updateMeterOverlayEffect);
    {
        // Seed the cycle at the user's own colour so a session that launches
        // with RGB already persisted starts from where the box was, instead of
        // snapping to red on the first tick.
        const QColor overlayBase(config_.data().meterOverlayColor);
        if (overlayBase.isValid() && overlayBase.hue() >= 0) {
            overlayRgbHueDeg_ = static_cast<double>(overlayBase.hue());
        }
    }

    // Stream-window containment watchdog. During an explicit spawn/restart grace
    // it hunts at frame cadence so a new top-level window is hidden immediately.
    // Outside that bounded grace, a passive capture-card preview has no Chiaki
    // window to find; the policy slows the OS-wide scan to human cadence.
    chiakiEmbedWatchdog_.setTimerType(Qt::PreciseTimer);
    // ~60 Hz while a stream is live: once embedded/contained the fast path is a cheap
    // GetParent no-op, so the only real work is the brief window after a spawn/recreation,
    // where polling this fast recaptures the new top-level within ~1 frame (was ~6 at
    // 100 ms) and the off-screen-first re-embed keeps the grab invisible. The faster
    // tick is what makes the containment AUTHORITATIVE — a freshly-spawned stream window
    // is adopted off-screen before it can flash on the desktop. Self-stops when idle.
    chiakiEmbedWatchdog_.setInterval(kStreamWindowWatchdogSteadyMs);
    connect(&chiakiEmbedWatchdog_, &QTimer::timeout, this, [this]() {
#ifdef Q_OS_WIN
        const qint64 watchdogNowMs = QDateTime::currentMSecsSinceEpoch();
        const int watchdogInterval = streamWindowWatchdogIntervalMs(
            watchdogNowMs, embedWatchdogGraceUntilMs_);
        if (chiakiEmbedWatchdog_.interval() != watchdogInterval) {
            chiakiEmbedWatchdog_.setInterval(watchdogInterval);
        }
        const bool captureCardMode =
            config_.data().videoSource.compare(QLatin1String("capture_card"), Qt::CaseInsensitive) == 0;
        // In capture-card mode chiaki runs INPUT-ONLY and its embed status is
        // "Capture card (stream window hidden)" — NOT "Embedded". The old stop
        // condition (!remoteRunning_ && != "Embedded" && past grace) fired as
        // soon as the grace expired, leaving chiaki windows un-adopted on the
        // desktop. Keep the watchdog alive in capture-card mode as long as the
        // session is live (remoteRunning_ or the sidecar preview is active).
        if (!captureCardMode && !remoteRunning_ && chiakiEmbedStatus_ != QLatin1String("Embedded")
            && watchdogNowMs > embedWatchdogGraceUntilMs_) {
            chiakiEmbedWatchdog_.stop();
            return;
        }
        if (captureCardMode && !remoteRunning_ && !capturePreviewActive_
            && watchdogNowMs > embedWatchdogGraceUntilMs_) {
            chiakiEmbedWatchdog_.stop();
            return;
        }
        if (!chiakiEmbedVisible_) {
            // Capture-card mode: even when the live capture tab is not visible, we must still
            // adopt+hide the chiaki window so it never floats on the desktop. The old early-return
            // here let a restarted chiaki spawn outside the launcher when the user was on another tab.
            if (config_.data().videoSource.compare(QLatin1String("capture_card"), Qt::CaseInsensitive) != 0) {
                return; // page hidden — QML re-enables on tab return (non-capture-card only)
            }
            // Fall through in capture card mode to keep the window adopted/hidden.
        }
        HWND chiaki = reinterpret_cast<HWND>(chiakiWindowHandle_);
        // Capture-card mode: keep the stream window HIDDEN (adopt-as-hidden, Edit A). If it is already
        // our hidden child there's nothing to do; otherwise hunt+adopt it. Return BEFORE the embed /
        // Vulkan->OpenGL fallback logic below — that path is meaningless with no visible embed (this is
        // why no separate `!captureCard` guard on the fallback is needed).
        if (config_.data().videoSource.compare(QLatin1String("capture_card"), Qt::CaseInsensitive) == 0) {
            if (!(chiaki && IsWindow(chiaki) && GetParent(chiaki) == mainWindowHandle())) {
                setChiakiEmbedVisible(true);
            }
            return;
        }
        if (chiakiEmbedStatus_ == QLatin1String("Embedded") && chiaki && IsWindow(chiaki)
            && GetParent(chiaki) == mainWindowHandle()) {
            return;
        }
        setChiakiEmbedVisible(true);

        // Vulkan embed fallback (opt-in, OFF by default).
        //
        // The original auto-fallback flipped the persisted backend to OpenGL if the
        // stream wasn't embedded ~8 s after the sidecar started. On a COLD first
        // connect the stream window has not appeared yet at +8 s (PS5 wake, pairing,
        // software-decode 1080p init), so this fired every run, nuked Vulkan before
        // it ever had a chance, and the disconnect→reconnect churn exited the sidecar
        // into safe mode. Worse, on this hardware the OpenGL decoder readback yields a
        // mangled near-black feed (V~22/S~245, ~62% black pixels) — measured live —
        // so "falling back" actively blinds the bot and shows garbage in the panel.
        // The Vulkan zero-copy path is the known-good feed (the pre-present readback
        // fix targets it), so we keep retrying the Vulkan embed for the whole session
        // instead. Gate the fallback behind an explicit escape hatch AND require that
        // the stream window was actually FOUND but won't reparent (the genuine
        // libplacebo non-reparenting case), never the benign "still launching" case.
        const bool allowOpenGlFallback = qEnvironmentVariableIsSet("ORION_ALLOW_OPENGL_FALLBACK");
        const bool streamWindowFound = chiakiWindowHandle_ != 0
            && IsWindow(reinterpret_cast<HWND>(chiakiWindowHandle_));
        if (allowOpenGlFallback
            && streamWindowFound
            && chiakiEmbedStatus_ != QLatin1String("Embedded")
            && remoteRunning_
            && !vulkanEmbedFallbackTried_
            && config_.data().streamRenderBackend.compare(QLatin1String("opengl"), Qt::CaseInsensitive) != 0
            && QDateTime::currentMSecsSinceEpoch() > embedWatchdogGraceUntilMs_ - 12000) {
            vulkanEmbedFallbackTried_ = true;
            auto data = config_.data();
            data.streamRenderBackend = QStringLiteral("opengl");
            saveConfigSilently(data);
            appendLog(QStringLiteral("Vulkan stream window found but did not embed — ORION_ALLOW_OPENGL_FALLBACK set, switching to OpenGL (persisted) and restarting the stream."));
            disconnectRemotePlay(true);  // synchronous: teardown must finish before reconnect
            connectRemotePlay();
        }
#else
        chiakiEmbedWatchdog_.stop();
#endif
    });

    // [ORION_METER_DELAY 2026-08-07] Don't do a synchronous security evaluation on
    // startup — it can add hundreds of ms to launcher-to-window time. Fail closed
    // until the periodic evaluator returns.
    applySecurityStatus(failClosedSecurityEvaluationStatus(
        QStringLiteral("Initial security evaluation in progress")));
    periodicSecurityEvaluator_.request(securityEvaluationFence_.current());
    updateRuntimeStatus();

    // [ORION_METER_DELAY 2026-08-07] Wire the meter-delay actuator to the
    // service via NetworkBridge and prime it from persisted config. Session gate
    // transitions (playing/stream-ended) are driven from the RemotePlaySession
    // stateChanged lambda established above; the connect below is fire-and-forget
    // and runs on the NetworkBridge worker thread via queueCommand.
    {
        applyMeterDelayRuntimeConfig();
        // [ORION_METER_DELAY_BYPASS 2026-08-08] Do NOT seed offense from
        // !defenseModeActive_: Defense Mode is dormant (physical trigger
        // disabled), so that seed was a permanent, evidence-free offense=true —
        // it made the OffenseDefense policy identical to AlwaysOn and the
        // "Bypass on defense" toggle a no-op (measured live: Locked at 250 ms
        // for 5.5 min of mixed play). No possession detector exists; engagement
        // under OffenseDefense now keys on the live shot inputs this controller
        // already feeds (setPhysicalSquareHeld / setShotCycleActive). setOffense
        // stays wired for a future real possession signal only.
        meterDelay_.setOffense(false);
        // [VENICENET WAVE 2B] Seed the DLL with the current possession + console
        // scope; the actuation lifecycle (enable/target) follows from the
        // controller's intercept/delay signals wired above.
        veniceNet_.setOffense(false);
        veniceNet_.setConsoleIp(config_.data().remotePlayConsoleIp);
    }
    // [VENICENET WAVE 2B] Route the actuator's outputs to the VeniceNet DLL
    // (the IPC client to the packet-bridge service) instead of NetworkBridge.
    // delayCommanded is the keepalive-bearing set_target (the DLL re-asserts it
    // to the service on every call, feeding the service watchdog);
    // interceptStart/Stop map to venicenet_set_enabled, which drives the
    // start/stop_meter_intercept lifecycle. NetworkBridge no longer carries any
    // meter-delay verb — it stays for passive diagnostics only.
    connect(&meterDelay_, &orion::MeterDelayController::delayCommanded,
            &veniceNet_, &orion::VeniceNetClient::setTargetDelayMs);
    connect(&meterDelay_, &orion::MeterDelayController::interceptStartRequested,
            &veniceNet_, [this]() { veniceNet_.setEnabled(true); });
    connect(&meterDelay_, &orion::MeterDelayController::interceptStopRequested,
            &veniceNet_, [this]() { veniceNet_.setEnabled(false); });
    // [ORION_METER_DELAY 2026-08-07] A settled/unsettled flip must re-poll
    // syncEngineArmed() so the ramp gate above takes/releases the engine.
    connect(&meterDelay_, &orion::MeterDelayController::delayConditionChanged, this,
            [this](quint64, double, bool) { syncEngineArmed(); });
    // [ORION_METER_DELAY_OBSERVABILITY 2026-08-08] stateTextChanged previously had
    // ZERO consumers — no QML surface and no log line — so a log census could not
    // distinguish "never armed" from "armed and holding". Two consumers now:
    //  1. the Meter Delay card's meterDelayStatusText property (refreshed here);
    //  2. a transition-deduped log line. publish() emits on every applied-value
    //     change (each 50 ms ramp tick), so the log key deliberately excludes the
    //     applied value — only session/shot/reason/target transitions are written.
    connect(&meterDelay_, &orion::MeterDelayController::stateTextChanged, this,
            [this]() {
        const QString state = QStringLiteral("%1|%2|target=%3ms|%4")
            .arg(meterDelay_.sessionStateString(), meterDelay_.shotStateString(),
                 QString::number(meterDelay_.targetDelayMs(), 'f', 0),
                 meterDelay_.reasonText());
        if (state != lastMeterDelayStateLogged_) {
            lastMeterDelayStateLogged_ = state;
            appendLog(QStringLiteral("Meter delay state: %1 (applied %2 ms)")
                          .arg(state)
                          .arg(meterDelay_.currentDelayMs(), 0, 'f', 0));
        }
        emit meterDelayStatusTextChanged();
    });
    // Settled-condition changes are rare (a few per possession at worst) and are the
    // exact record a timing census needs: which delay condition each shot ran under.
    connect(&meterDelay_, &orion::MeterDelayController::delayConditionChanged, this,
            [this](quint64 epoch, double keyMs, bool settled) {
        appendLog(QStringLiteral("Meter delay condition: settled=%1 key=%2ms epoch=%3")
                      .arg(settled ? 1 : 0)
                      .arg(keyMs, 0, 'f', 0)
                      .arg(epoch));
    });
    // [ORION_METER_DELAY_LEAD_STARVATION 2026-08-08] Keep the engine's view of the APPLIED
    // delay current. stateTextChanged fires on every applied-value change (each 50ms ramp
    // tick) as well as every state transition, so the engine tracks partial ramps too — a
    // half-ramped 120ms delay starves the visible runway just as a settled 250ms one does.
    // This is a diagnosis/refusal input only (see setMeterDelayCondition's contract): it can
    // suppress an arm and label a conflict, never time a release. Same thread as process().
    connect(&meterDelay_, &orion::MeterDelayController::stateTextChanged, this,
            [this]() {
        automation_.setMeterDelayCondition(meterDelay_.currentDelayMs(),
                                           meterDelay_.conditionSettled());
        // [ORION_TEMPO_BRIDGE_LIVE task #36] Same cadence: the remap gate must track
        // Locked/applied transitions (including the ramp-to-0 defense bypass).
        pushTempoRemapBridgeState();
    });
    connect(&meterDelay_, &orion::MeterDelayController::delayConditionChanged, this,
            [this](quint64, double, bool settled) {
        automation_.setMeterDelayCondition(meterDelay_.currentDelayMs(), settled);
        pushTempoRemapBridgeState();
    });
    // [ORION_METER_DELAY_ARM_STATE 2026-08-08] The service's OWN report of its arm
    // state (hello `features` / the meter_delay_disarmed refusal). NetworkBridge
    // transition-dedupes the signal — the disarmed refusal repeats once per keepalive
    // verb — so this appendLog fires once per transition, giving a log census the
    // line that finally separates "never armed" from "armed and working" (the 2.2 MB
    // production log could not tell those apart).
    connect(&networkBridge_, &orion::NetworkBridge::meterDelayServiceStateChanged, this,
            [this](orion::NetworkBridge::MeterDelayServiceState state,
                   const QString& cause) {
        const char* label = "unknown";
        switch (state) {
        case orion::NetworkBridge::MeterDelayServiceState::Armed:
            label = "ARMED";
            break;
        case orion::NetworkBridge::MeterDelayServiceState::Disarmed:
            label = "DISARMED";
            break;
        case orion::NetworkBridge::MeterDelayServiceState::Unknown:
            break;
        }
        appendLog(QStringLiteral("Meter delay service state: %1 (%2)")
                      .arg(QLatin1String(label), cause));
        emit meterDelayStatusTextChanged();
    });
    // [VENICENET WAVE 2B] The service's own applied-delay echo + arm state now
    // arrive through the VeniceNet DLL snapshot (the actuation path), marshaled
    // onto this thread by VeniceNetClient::snapshotChanged. This replaces the
    // NetworkBridge JSON-over-pipe echo parsing: NetworkBridge no longer carries
    // meter-delay verbs, so its echo would be silent. The applied value changes
    // on every keepalive during a ramp, so the log line is emitted on the
    // active/driver-state TRANSITION only; the status line + availability refresh
    // on every snapshot.
    connect(&veniceNet_, &orion::VeniceNetClient::snapshotChanged, this, [this]() {
        const orion::VeniceNetClient::Snapshot snap = veniceNet_.snapshot();
        const bool active = snap.armed;
        const double appliedMs = snap.armed ? snap.appliedDelayMs : -1.0;
        const bool activeChanged = active != meterDelayEchoActive_;
        meterDelayEchoActive_ = active;
        meterDelayEchoAppliedMs_ = appliedMs;
        if (activeChanged) {
            appendLog(QStringLiteral(
                          "Meter delay service echo: intercept %1 (applying %2 ms) [VeniceNet]")
                          .arg(active ? QStringLiteral("ACTIVE") : QStringLiteral("inactive"))
                          .arg(appliedMs >= 0.0 ? QString::number(appliedMs, 'f', 0)
                                                : QStringLiteral("-")));
        }
        // [ORION_TEMPO_BRIDGE_LIVE task #36] The echo members feed the remap gate's
        // intercept-applying leg; an armed echo is also a backend health beat. A dead
        // service resets the snapshot (armed=false) through this same path, so the gate
        // closes without any latch — idempotent across a service restart.
        pushTempoRemapBridgeState();
        if (snap.armed) {
            automation_.noteTempoRemapBridgeHealthBeat();
        }
        emit meterDelayStatusTextChanged();
        emit meterDelayBackendAvailabilityChanged();
    });
    // [ORION_METER_DELAY_AVAILABILITY 2026-08-08] Once per launch, say out loud when
    // the headline toggle is ON but nothing on this machine can actuate it. The 2.2 MB
    // production log from the failing session contained zero meter-delay lines; this
    // is the line that makes that failure mode self-identifying.
    if (config_.data().meterDelayEnabled && !meterDelayBackendAvailable()) {
        appendLog(QStringLiteral(
            "Meter delay is ON in settings but no delay backend exists on this install "
            "(no VeniceNetSvc/NexusVisionSvc service, no nexus_svc.py debug bridge): the "
            "delay cannot engage. The Meter Delay card shows the same warning."));
    }
    // [ORION_TEMPO_BRIDGE_LIVE 2026-08-08 task #36] Wire the engine's tempo-remap bridge
    // gate BEFORE the first 4 ms input poll (same ordering contract as syncEngineArmed).
    // Until the delay engine locks AND the service's own echo confirms the intercept is
    // applying, a Square press under Tempo remap stays a plain pass-through instead of
    // being eaten by a remap nothing behind the bridge can complete.
    pushTempoRemapBridgeState();
    connect(&remotePlay_, &RemotePlaySession::stateChanged, this,
            [this](RemotePlayState state, const QString&) {
        if (state == RemotePlayState::Running) {
            meterDelay_.setPlayingGame(true);
        } else if (state == RemotePlayState::Disconnected
                   || state == RemotePlayState::Error) {
            meterDelay_.notifyStreamEnded();
        }
    });

#ifndef ORION_PRODUCTION_BUILD
    // Local auto-unlock and environment-key injection are developer launcher
    // conveniences. Compile the complete hooks out of production rather than
    // relying on a later runtime predicate to make them unreachable.
    if (qEnvironmentVariableIsSet("ORION_AUTO_UNLOCK_LOCAL") && localDevAllowed()) {
        authenticated_ = true;
        licenseState_ = QStringLiteral("Local Dev");
        authMessage_ = QStringLiteral("Local native dev license accepted. Opening Venice.");
        currentPage_ = QStringLiteral("remotePlay");
        appendLog(QStringLiteral("Local UI smoke-test unlock accepted."));
        // Pre-warm the capture preview so the Live Capture card is live at launch instead of
        // a black panel. This auto-unlock path AUTHENTICATES DIRECTLY and skips the license
        // activationFinished handler where the other pre-warm lives, so it needs its own call.
        // Deferred to the event loop so remotePlay_ is fully wired before it spawns the sidecar;
        // startCapturePreview() is idempotent/guarded (capture-card source, no-op if already up).
        QTimer::singleShot(0, this, [this]() { startCapturePreview(); });
    }

    // Dev convenience: auto-submit a license key from the ORION_LICENSE_KEY env var so a
    // dev/test key — kept in a gitignored *.local.ps1, NEVER in source — does not have to be
    // re-typed every launch. Runs the SAME authenticate() path: an NVDEV- key unlocks offline,
    // any other key (e.g. a server test key) goes through normal server activation. Only the key
    // suffix is logged (no secret in the log).
    if (!authenticated_ && localDevAllowed()
            && qEnvironmentVariableIsSet("ORION_LICENSE_KEY")) {
        const QString envKey = qEnvironmentVariable("ORION_LICENSE_KEY").trimmed();
        if (!envKey.isEmpty()) {
            appendLog(QStringLiteral("Auto-authenticating from ORION_LICENSE_KEY env (key_suffix=%1).")
                          .arg(envKey.right(4)));
            authenticate(envKey);
        }
    }
#endif

    // --- Watchdogs -------------------------------------------------------
    // Sidecar crash while streaming: restart it once; a repeat inside the
    // 5-minute window escalates to safe mode. Any trip disarms + neutrals first.
    connect(&remotePlay_, &RemotePlaySession::sidecarStopFinished, this, [this]() {
        if (remotePlayTeardownActive_) finishRemotePlayTeardown();
    });
    connect(&remotePlay_, &RemotePlaySession::sidecarExited, this, [this](bool whileStreaming) {
        // Frame numbers restart with the sidecar. Drop the entire old join
        // namespace before any restart path can paint a low-number new frame.
        meterBoxRing_.clear();
        meterBoxCapture_ = {};
        meterOverlayTracker_.reset();
        meterOverlayContinuityLease_.reset();
        meterBox_ = {};
        meterRejectedBox_ = {};
        meterOverlayComputedBox_ = {};
        meterOverlayComputedRejectedBox_ = {};
        remoteFrameOverlaySnapshots_.clear();
        meterConfirmed_ = false;
        meterBoxCaptureSize_ = {};
        lastRealMeterSeenMs_ = 0;
        lastMeterOverlayVisualSeenMs_ = 0;
        userMeterVisible_ = false;
        // The health line describes the reader that just died; a restarted sidecar
        // re-publishes within ~2 s, and until then the card must not quote a ghost.
        if (!detectorHealthLine_.isEmpty() || !detectorProvider_.isEmpty()) {
            detectorHealthLine_.clear();
            detectorProvider_.clear();
            emit detectorHealthChanged();
        }
        botOwnershipArmToken_ = 0;
        botOwnershipStartedMs_ = -1.0;
        botOwnershipEndedMs_ = -1.0;
        clearMeterMetrics(true);
        emit meterBoxChanged();
        // Never leave the last live pixels onscreen after a sidecar crash or a
        // stopped preview. Preserve the deliberate warm-preview-to-connect
        // handoff, where the existing frame is intentionally held until the
        // replacement sidecar starts.
        if (whileStreaming || remoteState_ != QLatin1String("Connecting")) {
            clearRemotePreviewFrame();
        }
        // RemotePlaySession emits its terminal Disconnected state synchronously before this
        // signal, so remoteRunning_ is already false even for a genuine crash. Use the immutable
        // process-generation verdict instead: it is false for user disconnect and intentional
        // watchdog restart. The independent teardown/shutdown intent guards are defense-in-depth.
        if (!shouldRecoverUnexpectedSidecarExit(
                whileStreaming,
                remotePlayTeardownActive_,
                applicationShutdownActive(applicationShutdownPhase_))) {
            return;
        }
        // A copied precise-fire deadline survives independently of the GUI
        // tick. Revoke it and neutral both routes synchronously on every real
        // sidecar loss, including startup-grace and auto-reconnect branches.
        captureAwaitingFreshFrame_ = true;
        automation_.setArmed(false);
        disarmPreciseFire();
        automation_.reset();
        neutralizeOwnedInput();
        // C4 (2026-07-25): safe mode used to return HERE, which is what made the latch permanent.
        // Safe mode's auto-recovery (onWatchdogTick) waits for a clean re-stream — but the thing
        // that had to come back was the sidecar that just exited, and nothing was left to restart
        // it, so the health run never accumulated and the bot stayed dead until a manual click.
        // Give the recovery something to observe: a BOUNDED restart, automation still disarmed
        // (safe mode never re-arms here — only exitSafeMode does, after its full stability window).
        // Past the cap a genuine crash loop stops respawning, exactly as before.
        if (safeModeActive_) {
            if (safeModeSidecarRestarts_ < kSafeModeSidecarRestartMax_) {
                ++safeModeSidecarRestarts_;
                appendLog(QStringLiteral("SAFE MODE: sidecar exited — restarting it (%1/%2) so the "
                                         "stream can re-heal; automation stays disarmed until the "
                                         "safe-mode stability window passes.")
                              .arg(safeModeSidecarRestarts_)
                              .arg(kSafeModeSidecarRestartMax_));
                restartSidecarWithWindowContainment();
                syncEngineArmed();   // re-asserts DISARMED while safeModeActive_
            } else {
                appendLog(QStringLiteral("SAFE MODE: sidecar exited again after %1 restarts — "
                                         "not restarting (crash loop). Manual reset required.")
                              .arg(safeModeSidecarRestarts_));
                // [COPY-FIX 2026-09-23 NEW-A6] The line above is engineering-only now
                // (ui_notifications rule 0 hides "sidecar"); the customer gets the action.
                appendLog(QStringLiteral("Safe mode: video detection keeps stopping. Click SAFE MODE "
                                         "at the top, then Exit safe mode, or restart Venice."));
            }
            return;
        }
        // Startup grace: an exit during chiaki's warm-up window is a hiccup, not a crash.
        // Restart the sidecar quietly (still neutral/disarm for safety) WITHOUT counting it
        // toward the safe-mode escalation, so connecting "just works" without the user having
        // to reconnect by hand. Capped so a genuinely broken stream still escalates.
        if (QDateTime::currentMSecsSinceEpoch() < streamStartupGraceUntilMs_
                && streamStartupRestarts_ < 4) {
            ++streamStartupRestarts_;
            appendLog(QStringLiteral("Stream warm-up hiccup (sidecar exited during startup, attempt %1/4) "
                                     "— restarting quietly, no safe mode.").arg(streamStartupRestarts_));
            restartSidecarWithWindowContainment();
            syncEngineArmed();
            return;
        }
        if (config_.data().autoReconnect) {
            // Full reconnect on a mid-stream drop instead of the default sidecar restart.
            const quint64 reconnectGeneration = ++remotePlayLifecycleGeneration_;
            appendLog(QStringLiteral("Auto-reconnect: stream dropped mid-session — reconnecting."));
            QTimer::singleShot(1200, this, [this, reconnectGeneration]() {
                if (!delayedAutoReconnectStillCurrent(
                        reconnectGeneration,
                        remotePlayLifecycleGeneration_,
                        remotePlay_.state(),
                        config_.data().autoReconnect,
                        safeModeActive_,
                        applicationShutdownActive(applicationShutdownPhase_))) {
                    return;
                }
                disconnectRemotePlay(true);  // synchronous: teardown must finish before reconnect
                connectRemotePlay();
            });
            return;
        }
        // [COPY-FIX 2026-09-23] The reason is shown in the SAFE MODE dialog and the
        // "Watchdog trip:" Activity line, so it is customer copy.
        tripWatchdog(QStringLiteral("Video detection stopped mid-stream"));
        if (!safeModeActive_) {
            appendLog(QStringLiteral("Watchdog: restarting detection sidecar."));
            restartSidecarWithWindowContainment();
            syncEngineArmed();
        } else if (safeModeSidecarRestarts_ < kSafeModeSidecarRestartMax_) {
            // C4: THIS exit is what just latched safe mode, so the safe-mode branch above did not
            // run. Without a restart here the sidecar stays dead, the frame feed never comes back,
            // and the bounded auto-recovery has nothing healthy to observe — the exact permanent
            // stall C4 is about. Restart once (bounded, still disarmed).
            ++safeModeSidecarRestarts_;
            appendLog(QStringLiteral("SAFE MODE just latched: restarting the detection sidecar "
                                     "(%1/%2) so the stream can re-heal; automation stays disarmed.")
                          .arg(safeModeSidecarRestarts_)
                          .arg(kSafeModeSidecarRestartMax_));
            restartSidecarWithWindowContainment();
            syncEngineArmed();
        }
    });
    watchdogTimer_.setTimerType(Qt::CoarseTimer);
    watchdogTimer_.setInterval(2000);
    connect(&watchdogTimer_, &QTimer::timeout, this, &OrionAppController::onWatchdogTick);
    watchdogTimer_.start();
    // GUI-freeze watchdog: a worker thread monitors this heartbeat. A frozen
    // render/main thread can't protect the user, so the worker neutral-submits
    // and disarms directly; the GUI escalates to safe mode when it thaws.
    // [RT-MED-10 2026-09-23] Steady clock + suspend awareness (GuiFreezeWatchdogPolicy.h):
    // sleep/resume and wall-clock steps no longer read as a GUI freeze.
    guiHeartbeatMs_.store(gui_freeze::monotonicMs(), std::memory_order_relaxed);
    guiFreezeThread_ = std::thread([this]() {
        gui_freeze::LoopState loopState;
        int awakeSuspendFlagPolls = 0;
        while (!watchdogThreadStop_.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(gui_freeze::kPollIntervalMs));
            // A PBT_APMSUSPEND with no resume message (aborted suspend) must not park the
            // watchdog forever: 60 s of polls on time while "suspended" clears the flag.
            if (systemSuspended_.load(std::memory_order_relaxed)) {
                if (++awakeSuspendFlagPolls > 120) {
                    guiHeartbeatMs_.store(gui_freeze::monotonicMs(), std::memory_order_relaxed);
                    systemSuspended_.store(false, std::memory_order_relaxed);
                    awakeSuspendFlagPolls = 0;
                }
            } else {
                awakeSuspendFlagPolls = 0;
            }
            const qint64 watchdogNowMs = gui_freeze::monotonicMs();
            const gui_freeze::Verdict verdict = gui_freeze::evaluate(
                loopState, watchdogNowMs, guiHeartbeatMs_.load(std::memory_order_relaxed),
                guiFreezeSuppressUntilMs_.load(std::memory_order_relaxed),
                systemSuspended_.load(std::memory_order_relaxed));
            if (verdict == gui_freeze::Verdict::Suspended) {
                // The whole process was paused (sleep/hibernate/VM pause), not the GUI thread.
                // Give the GUI a fresh 6 s from now instead of reading the pause as a freeze.
                guiHeartbeatMs_.store(watchdogNowMs, std::memory_order_relaxed);
                continue;
            }
            // [SAFE MODE] Stand down inside a declared, bounded blocking section (the disconnect
            // teardown — see guiFreezeSuppressUntilMs_). Checked BEFORE the exchange so a normal
            // disconnect doesn't even latch guiFreezeTripped_, which is what onWatchdogTick
            // escalates into the manual-recovery safe-mode latch. Nothing to protect there anyway:
            // disconnectRemotePlay() disarms the fire thread and the teardown neutrals + unplugs
            // the pad itself.
            if (verdict == gui_freeze::Verdict::Trip && !guiFreezeTripped_.exchange(true)) {
                // Cross-thread by design: armed_ is a single bool gate and the
                // virtual-pad submit is serialized by submitMutex_ (same pattern
                // as the precise fire thread).
                automation_.setArmed(false);
                disarmPreciseFire();
                neutralizeOwnedInput();
            }
        }
    });

#ifdef Q_OS_WIN
    QTimer::singleShot(0, this, &OrionAppController::registerRawInputController);
#endif
}

OrionAppController::~OrionAppController()
{
    // [UAF] Stop and JOIN every thread that dereferences this controller's state BEFORE the
    // teardown that frees it. This used to run last: disconnectRemotePlay(true) unplugs the ViGEm
    // target (controller_.disconnectController()) and resets automation_, and killChiakiProcesses()
    // then spends seconds inside blocking taskkills — while the precise fire thread was still
    // live and dereferencing owner_->controller_ / owner_->orionInput_ / owner_->automation_ on
    // its own thread. A release armed just before shutdown could therefore submit into a freed
    // pad target (use-after-free) anywhere in that multi-second window. The GUI-freeze watchdog
    // thread has the same exposure (it neutral-submits + disarms cross-thread), so it is stopped
    // and joined here too. After this point no other thread touches our members.
    watchdogThreadStop_.store(true, std::memory_order_relaxed);
    if (guiFreezeThread_.joinable()) {
        guiFreezeThread_.join();
    }
    delete fireThread_;     // joins the precise fire thread
    fireThread_ = nullptr;

    // Command the injected inbound delay to 0 while networkBridge_ is still
    // fully alive (members are destroyed only after this body completes). An
    // This is normally complete from root-window closing/aboutToQuit. Keep
    // destruction as the final fail-safe for startup and non-window exits.
    prepareForApplicationExit();

    logsDirty_ = false;     // disk-only flush, don't emit during teardown
    flushPendingLogs();     // persist any queued log lines before shutdown
    appLogSink_.stopAndDrain();
    // Kill any chiaki/OrionStream processes so no stale processes remain after the
    // X button closes the app. The destructor is the LAST chance — window.close()
    // triggers QApplication::exec() to return, then this runs. Without it chiaki
    // keeps running orphaned on the desktop.
#ifdef Q_OS_WIN
    delete rawInputWorker_; // joins the input pump thread
    rawInputWorker_ = nullptr;
    if (auto* app = QCoreApplication::instance()) {
        app->removeNativeEventFilter(this);
    }
#endif
}

// ── MOTD (docs/ADMIN_PANEL_V2_CONTRACT.md §5) ─────────────────────────────────
// The banner shows iff there is text, `until` has not passed, and the user has not
// dismissed THIS text. Dismissal is process memory only — never written to settings.
bool OrionAppController::motdVisible() const
{
    return !motdDismissed_ && motd_.activeAt(QDateTime::currentSecsSinceEpoch());
}

void OrionAppController::dismissMotd()
{
    if (motd_.isEmpty() || motdDismissed_) {
        return;
    }
    motdDismissed_ = true;
    appendLog(QStringLiteral("MOTD dismissed for this session."));
    emit motdChanged();
}

// ── [CL2-P8-002 2026-09-23] Licence heartbeat recovery ───────────────────────
// None of these touch leaseGate_: they only REQUEST a /api/license/check. The
// lease is refreshed exclusively by a verified ok answer in validationFinished,
// so fail-closed behaviour is exactly as before.
// [round 2] Executes a coordinator decision. Kill/stop is always honoured; a
// (re)start or send only while signed in and not exiting.
void OrionAppController::applyHeartbeatAction(const HeartbeatAction& action)
{
    if (action.stopTimer) {
        licenseHeartbeatTimer_.stop();
    }
    if (applicationShutdownActive(applicationShutdownPhase_) || !authenticated_
        || authLicenseKey_.isEmpty()) {
        return;   // signed out / killed / exiting: never re-arm the heartbeat
    }
    if (action.restartTimerMs >= 0) {
        licenseHeartbeatTimer_.start(action.restartTimerMs);
    }
    if (action.sendNow) {
        licenseClient_.validate(authLicenseKey_, security_.machineId());
    }
}

void OrionAppController::requestImmediateLicenseHeartbeat(const QString& reason)
{
    if (applicationShutdownActive(applicationShutdownPhase_) || !authenticated_
        || authLicenseKey_.isEmpty()) {
        return;
    }
    const qint64 nowMs = heartbeatMonotonic_.isValid() ? heartbeatMonotonic_.elapsed() : 0;
    const bool inFlight = licenseClient_.validationInFlight();
    const bool wasPending = heartbeatCoordinator_.rerunPending();
    // Debounced inside (Windows fires several resume/online events per wake). A
    // wake / network return restarts the short retry ladder; if a request is in
    // flight the coordinator remembers ONE re-run for when it completes
    // (LicenseClient::validate would otherwise silently no-op).
    const HeartbeatAction action = heartbeatCoordinator_.onImmediateRequest(nowMs, inFlight);
    const bool queued = !wasPending && heartbeatCoordinator_.rerunPending();
    if (!action.sendNow && !queued) {
        return;   // debounced
    }
    appendLog(heartbeatImmediateLogLine(reason, queued, leaseGate_.stateText()));   // engineering log only
    applyHeartbeatAction(action);
}

void OrionAppController::refreshLeaseNotice()
{
    bool signedInForLease = authenticated_;
#ifndef ORION_PRODUCTION_BUILD
    // The dev-only Local Dev session bypasses the lease (AutomationAccessPolicy);
    // do not show a lease notice for it.
    if (licenseState_ == QLatin1String("Local Dev")) {
        signedInForLease = false;
    }
#endif
    const LeaseNoticeKind kind = leaseNoticeKind(leaseGate_.enabled(), signedInForLease,
                                                 leaseGate_.fireAllowed(),
                                                 heartbeatCoordinator_.clockOffAtReceipt());
    if (kind == leaseNoticeKind_) {
        return;
    }
    const LeaseNoticeKind previous = leaseNoticeKind_;
    leaseNoticeKind_ = kind;
    leaseNotice_ = leaseNoticeText(kind);
    if (kind == LeaseNoticeKind::None) {
        if (authenticated_) {   // a sign-out also clears the notice; that is not a restore
            appendLog(leaseRestoredLogLine());   // customer Activity ring: one line per episode
        }
    } else {
        // Customer Activity ring (licence allow-list): the plain banner copy, once
        // per episode. The lease state goes to the engineering retry lines.
        appendLog(leaseNotice_);
    }
    emit leaseNoticeChanged();
    emit statusChanged();
    // The lease just lapsed while only the normal 5-min tick is pending (e.g. a
    // missed resume event): ask once now. Skipped while the retry ladder is
    // already running so a failure never doubles up. Debounced; a no-op while a
    // request is in flight.
    if (previous == LeaseNoticeKind::None && kind == LeaseNoticeKind::Reconnecting
        && heartbeatCoordinator_.consecutiveFailures() == 0) {
        requestImmediateLicenseHeartbeat(QStringLiteral("lease lapsed"));
    }
}

void OrionAppController::applyServerMotd(const LicenseMotd& motd)
{
    const qint64 now = QDateTime::currentSecsSinceEpoch();
    // An absent, blank or already-expired notice clears the slot (contract: hidden
    // when absent or until < now). A changed text re-arms the banner even if the
    // previous one was dismissed; the same text keeps the user's dismissal.
    const LicenseMotd next = motd.activeAt(now) ? motd : LicenseMotd{};
    const bool textChanged = next.text != motd_.text;
    const bool changed = textChanged || next.level != motd_.level || next.untilEpochS != motd_.untilEpochS;
    if (textChanged) {
        motdDismissed_ = false;
        if (!next.text.isEmpty()) {
            appendLog(QStringLiteral("MOTD (%1%2): %3")
                          .arg(next.level,
                               next.untilEpochS > 0
                                   ? QStringLiteral(", until %1").arg(
                                         QDateTime::fromSecsSinceEpoch(next.untilEpochS).toString(Qt::ISODate))
                                   : QString(),
                               next.text.left(200)));
        } else if (!motd_.text.isEmpty()) {
            appendLog(QStringLiteral("MOTD cleared."));
        }
    }
    motd_ = next;

    // Hide on the dot when `until` passes rather than waiting for the next 5-min
    // heartbeat. Capped at 24 h per arm; the timeout re-applies motd_ and re-arms.
    motdExpiryTimer_.stop();
    if (motd_.untilEpochS > now) {
        const qint64 waitMs = std::min<qint64>((motd_.untilEpochS - now) * 1000 + 250, qint64(24) * 60 * 60 * 1000);
        motdExpiryTimer_.start(static_cast<int>(waitMs));
    }

    if (changed) {
        emit motdChanged();
    }
}

int OrionAppController::profileDaysLeft() const noexcept
{
    return licenseDaysLeft(profile_.known, profile_.expiryEpochS,
                           QDateTime::currentSecsSinceEpoch());
}

void OrionAppController::applyLicenseProfile(const LicenseProfile& profile)
{
    // TOLERANT: the current live backend sends no `profile`. A response without
    // one must never blank a page the previous response already populated, so an
    // unknown block is simply ignored.
    if (!profile.known) {
        return;
    }
    const bool changed =
        profile.discordUserId != profile_.discordUserId
        || profile.discordUsername != profile_.discordUsername
        || profile.plan != profile_.plan
        || profile.expiryEpochS != profile_.expiryEpochS
        || profile.activatedAtEpochS != profile_.activatedAtEpochS
        || profile.hwidResetsUsed != profile_.hwidResetsUsed
        || profile.hwidResetsFreeTotal != profile_.hwidResetsFreeTotal
        || profile.hwidResetsFreeRemaining != profile_.hwidResetsFreeRemaining
        || profile.hwidPaidCredits != profile_.hwidPaidCredits
        || !profile_.known;
    profile_ = profile;
    if (changed) {
        emit profileChanged();
        // The "Time left" row on Setup/Live reads timeLeft_, which the 1 s runtime
        // poll rewrites from licenseState_ alone. Refresh it here so the hero
        // number on Profile and that row can never tell the user two stories.
        emit statusChanged();
    }
}

void OrionAppController::requestApplicationShutdown()
{
    if (!markApplicationShutdownRequested(applicationShutdownPhase_)) {
        return;
    }

    appendLog(QStringLiteral("Application shutdown requested."));

    // Fail closed before the close event hides the window. Do not acquire the
    // submit fence or write a pipe packet in this callback: native WM_CLOSE must
    // return immediately even if a driver/pipe is wedged. setArmed(false) is the
    // lock-free fire-authority boundary; the queued teardown performs the fully
    // serialized disarm, neutral, unplug, and child-process cleanup.
    inputPollTimer_.stop();
    chiakiEmbedWatchdog_.stop();
    automation_.setArmed(false);

    QTimer::singleShot(0, this, [this]() {
        prepareForApplicationExit();
        if (auto* app = QCoreApplication::instance()) {
            app->quit();
        }
    });
}

void OrionAppController::prepareForApplicationExit()
{
    if (!beginApplicationShutdownTeardown(applicationShutdownPhase_)) {
        return;
    }

    appendLog(QStringLiteral("Application shutdown teardown started."));
    // [2026-09-11] The precise-fire worker is joined FIRST: nothing below may race a worker that
    // still reads automation_ / the input client / the log sink while they are being reset.
    delete fireThread_;
    fireThread_ = nullptr;

    // Stop every GUI producer that could re-arm automation, reopen capture, or
    // queue a sidecar recovery while teardown is in progress.
    refreshTimer_.stop();
    inputPollTimer_.stop();
    lightbarEffectTimer_.stop();
    chiakiEmbedWatchdog_.stop();
    updateRecheckTimer_.stop();
    watchdogTimer_.stop();
    licenseHeartbeatTimer_.stop();
    leaseNoticeTimer_.stop();   // [CL2-P8-002 2026-09-23]
    logFlushTimer_.stop();

    automation_.setArmed(false);
    disarmPreciseFire();
    automation_.reset();
    neutralizeOwnedInput();

    // [VENICENET WAVE 2B] Release the inbound delay + intercept now, while the DLL
    // client is still alive, so the console is handed back at 0 ms before exit —
    // the DLL's own venicenet_shutdown() (member destruction) is the fail-safe.
    veniceNet_.setEnabled(false);

    // A synchronous exit upgrades an already-queued Disconnect. The stale
    // callback sees cleared ownership flags and becomes a no-op.
    disconnectRemotePlay(true);
    killChiakiProcesses();
    flushPendingLogs();
    appLogSink_.drain();
    completeApplicationShutdownTeardown(applicationShutdownPhase_);
}

void OrionAppController::killChiakiProcesses()
{
    if (chiakiCleanupDone_) return;
    chiakiCleanupDone_ = true;
    remotePlay_.stop();
    remotePlay_.waitForStopped();
}

void OrionAppController::setFrameProvider(RemoteFrameProvider* provider)
{
    frameProvider_ = provider;
}

bool OrionAppController::nativeEventFilter(const QByteArray& eventType, void* message, qintptr* result)
{
#ifdef Q_OS_WIN
    // [CL2-P8-002 2026-09-23] Resume from sleep/hibernate: the fire lease has
    // usually lapsed while suspended, so re-check the licence now instead of on
    // the next 5-min tick. Queued (never re-enter from inside a window proc) and
    // debounced in requestImmediateLicenseHeartbeat (Windows sends both resume
    // codes, to every top-level window). Never consumed: falls through below.
    if (eventType == "windows_generic_MSG" && message) {
        const auto* powerMsg = static_cast<const MSG*>(message);
        if (powerMsg->message == WM_POWERBROADCAST && powerMsg->wParam == PBT_APMSUSPEND) {
            // [RT-MED-10 2026-09-23] Going to sleep: park the GUI-freeze watchdog and make the
            // pad safe NOW (the queued GUI work may not run before the system suspends). Same
            // cross-thread-safe calls the freeze worker itself uses.
            systemSuspended_.store(true, std::memory_order_relaxed);
            automation_.setArmed(false);
            disarmPreciseFire();
            neutralizeOwnedInput();
        }
        if (powerMsg->message == WM_POWERBROADCAST
            && (powerMsg->wParam == PBT_APMRESUMEAUTOMATIC || powerMsg->wParam == PBT_APMRESUMESUSPEND)) {
            // Re-seed the heartbeat BEFORE un-parking the watchdog.
            guiHeartbeatMs_.store(gui_freeze::monotonicMs(), std::memory_order_relaxed);
            const bool wasSuspended = systemSuspended_.exchange(false, std::memory_order_relaxed);
            QMetaObject::invokeMethod(this, [this, wasSuspended]() {
                if (wasSuspended && remoteRunning_) {
                    appendLog(QStringLiteral("Venice resumed from sleep. If the picture or your controller doesn't come back, press Disconnect, then Connect."));
                }
                syncEngineArmed();
                requestImmediateLicenseHeartbeat(QStringLiteral("resumed from sleep"));
            }, Qt::QueuedConnection);
        }
    }
    // Steam Input / Windows GameInput can translate a desktop-visible XUSB pad's
    // A/D-pad/trigger input into keyboard or mouse navigation for the foreground
    // app. During a live PS5 session those events belong to the console, never to
    // Orion's focused QML controls. RawInput still feeds the console route; this
    // only consumes the mapped desktop message when it lands inside the short
    // window opened by real controller activity.
    if (eventType == "windows_generic_MSG" && message) {
        const auto* msg = static_cast<const MSG*>(message);
        DesktopUiInputKind kind = DesktopUiInputKind::Other;
        switch (msg->message) {
        case WM_KEYDOWN:
        case WM_KEYUP:
        case WM_SYSKEYDOWN:
        case WM_SYSKEYUP:
            switch (msg->wParam) {
            case VK_RETURN:
            case VK_SPACE:
            case VK_TAB:
            case VK_ESCAPE:
            case VK_LEFT:
            case VK_RIGHT:
            case VK_UP:
            case VK_DOWN:
                kind = DesktopUiInputKind::NavigationKey;
                break;
            default:
                break;
            }
            break;
        case WM_LBUTTONDOWN:
        case WM_LBUTTONUP:
        case WM_LBUTTONDBLCLK:
        case WM_RBUTTONDOWN:
        case WM_RBUTTONUP:
        case WM_RBUTTONDBLCLK:
        case WM_MBUTTONDOWN:
        case WM_MBUTTONUP:
        case WM_MBUTTONDBLCLK:
        case WM_XBUTTONDOWN:
        case WM_XBUTTONUP:
        case WM_XBUTTONDBLCLK:
            kind = DesktopUiInputKind::MouseButton;
            break;
        // [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] The owner-visible half of the
        // defect: a stick mapped to the pointer walks the cursor over Venice and
        // lights up every hover state it crosses, and the other stick maps to the
        // wheel (which in QML changes whatever Slider/ComboBox is under the
        // cursor). Neither message was classified before, so neither could ever
        // be suppressed no matter how wide the guard was.
        case WM_MOUSEMOVE:
        case WM_NCMOUSEMOVE:
        case WM_MOUSEHOVER:
            kind = DesktopUiInputKind::PointerMotion;
            break;
        case WM_MOUSEWHEEL:
        case WM_MOUSEHWHEEL:
            kind = DesktopUiInputKind::WheelScroll;
            break;
        default:
            break;
        }
        const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
        const DesktopUiInputOrigin origin = desktopUiInputKindDrivesUi(kind)
            ? currentDesktopUiInputOrigin()
            : DesktopUiInputOrigin::Unknown;
        if (shouldIsolateDesktopUiInput(
                remoteRunning_, controllerUiPassthrough_, controllerUiInjectedIsolation_,
                origin, nowMs, controllerUiGuardUntilMs_,
                controllerUiPointerGuardUntilMs_, kind)) {
            if (!controllerUiSuppressionLogged_) {
                controllerUiSuppressionLogged_ = true;
                appendLog(QStringLiteral("Controller UI isolation active: PS5 gameplay input cannot activate Venice controls."));
            }
            if (result) {
                *result = 0;
            }
            return true;
        }
    }
#else
    Q_UNUSED(eventType);
    Q_UNUSED(message);
    Q_UNUSED(result);
#endif
    // Controller raw input no longer flows through the GUI-thread message pump: WM_INPUT /
    // WM_INPUT_DEVICE_CHANGE target OrionRawInputWorker's message-only window on its own
    // thread. (Under decoder-frame load the GUI pump drained ~30 WM_INPUT/s vs the
    // DualSense's ~250/s, so the OS queue backed up and the read replayed stale sticks --
    // the hook+frame input lag.) Device changes arrive via handleRawInputDeviceChange.
    return false;
}

void OrionAppController::handleRawInputDeviceChange(
    quintptr deviceHandle, quintptr changeKind, const QString& devicePath,
    bool removedActivePhysical)
{
#ifdef Q_OS_WIN
    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    if (deviceHandle == lastControllerDeviceChangeHandle_
        && changeKind == lastControllerDeviceChangeKind_
        && nowMs - lastControllerDeviceChangeMs_ < 250) {
        return;
    }
    lastControllerDeviceChangeHandle_ = deviceHandle;
    lastControllerDeviceChangeKind_ = changeKind;
    lastControllerDeviceChangeMs_ = nowMs;
    for (int index = 0; index < 4; ++index) {
        xinputSlotCapabilityKnown_[index] = false;
        xinputSlotNoNavigation_[index] = false;
    }
    rawInputDeviceKinds_.clear();
    lastRawInputPresenceCheckMs_ = 0;
    const bool changedVirtual = rawInputPathLooksVirtual(devicePath);
    if (changeKind == GIDC_REMOVAL) {
        if (changedVirtual) {
            appendLog(QStringLiteral("Virtual controller device removed - physical route unchanged."));
        } else {
            const bool matchesActivePhysical = rawInputRemovalMatchesActivePhysical(
                removedActivePhysical,
                !devicePath.isEmpty(), !rawInputDevicePath_.isEmpty(),
                !devicePath.isEmpty() && !rawInputDevicePath_.isEmpty()
                    && devicePath.compare(rawInputDevicePath_, Qt::CaseInsensitive) == 0);
            if (matchesActivePhysical) {
                rawInputPresent_ = false;
                rawInputState_ = ControllerState{};
                lastRawInputMs_ = 0;
                rawInputLabel_.clear();
                rawInputDevicePath_.clear();
                activePhysicalDevicePath_.clear();
                activePhysicalDeviceKind_.clear();
                lastLightbarSentColor_ = QColor();
                lastLightbarSentDevicePath_.clear();
                lastLightbarSentDeviceKind_.clear();
                lightbarRefreshPending_ = false;
                lightbarEffectTimer_.stop();
                controllerSelector_.reset();
                physicalMissingSinceMs_ = nowMs;
                // [ORION_PAD_SILENT_HOLD 2026-09-11] The device is gone from the bus:
                // nothing to nudge, and the poll's onset block is skipped (since != 0),
                // so pin the unplug grace here instead of inheriting the last episode's.
                physicalMissingNudged_ = true;
                physicalMissingTeardownGraceMs_ = silentPadTeardownGraceMs(
                    /*enumerated=*/false, automation_.armed() || ownSeq_ >= 0);
                controllerLedStatus_ = QStringLiteral("LED waiting for controller");
                appendLog(QStringLiteral("Physical controller removed - waiting for debounce before virtual teardown."));
                disarmPreciseFire();
                automation_.reset();
                neutralizeOwnedInput();
                // [ViGEm RACE] VirtualController has no internal lock — serialization is entirely
                // caller-side via submitMutex_. This handler is device-change driven, so unlike the
                // 4ms controller tick it can land at ANY instant, including mid-release while the
                // precise fire thread is inside controller_.submit(). An unlocked neutral submit
                // there interleaves with the release report and can corrupt or erase the press.
            } else {
                appendLog(QStringLiteral("Non-selected controller device removed - physical route unchanged."));
            }
        }
        emit statusChanged();
    } else if (changeKind == GIDC_ARRIVAL) {
        appendLog(QStringLiteral("Controller device change detected - refreshing devices."));
        if (!changedVirtual) {
            // Re-open once after a real device arrival even when Windows reuses
            // the same HID path. The selected route is resolved by the next 4 ms
            // poll; clearing the success cache prevents that refresh being
            // mistaken for an unchanged recurring Solid write.
            lastLightbarSentColor_ = QColor();
            lastLightbarSentDevicePath_.clear();
            lastLightbarSentDeviceKind_.clear();
            requestControllerLightbarRefresh();
        }
    }
#else
    Q_UNUSED(deviceHandle);
    Q_UNUSED(changeKind);
    Q_UNUSED(devicePath);
    Q_UNUSED(removedActivePhysical);
#endif
}

QString OrionAppController::logText() const
{
    return logs_.join(QLatin1Char('\n'));
}

QString OrionAppController::activityText() const
{
    return customerLogs_.join(QLatin1Char('\n'));
}

void OrionAppController::appendCustomerEvent(const QString& plainText)
{
    if (plainText.trimmed().isEmpty()) {
        return;
    }
    // The optional plain-language FILE sink keeps its own (untimestamped-by-us)
    // copy; appendLog owns the rings and the diagnostic stream.
    userLog_.append(plainText);
    appendLog(plainText);
}

void OrionAppController::authenticate(const QString& key)
{
    const auto normalized = key.trimmed().toUpper();
    if (authBusy_) {
        return;
    }
    static const QRegularExpression publicDiscordId(QStringLiteral("^\\d{16,22}$"));
    if (publicDiscordId.match(normalized).hasMatch()) {
        authMessage_ = QStringLiteral("A Discord ID is public. Select Connect Discord to verify your account.");
        emit authChanged();
        return;
    }
    if (!security_.validateLicenseKeyFormat(normalized)) {
        authMessage_ = QStringLiteral("Invalid key format.");
        emit authChanged();
        return;
    }
#ifndef ORION_PRODUCTION_BUILD
    if (normalized.startsWith(QStringLiteral("NVDEV-")) && localDevAllowed()) {
        authenticated_ = true;
        authBusy_ = false;
        currentPage_ = QStringLiteral("remotePlay");
        licenseState_ = QStringLiteral("Local Dev");
        authMessage_ = QStringLiteral("Local native dev license accepted. Opening Venice.");
        appendLog(QStringLiteral("Local native dev license accepted."));
        QJsonObject entitlement;
        entitlement.insert(QStringLiteral("user"), QStringLiteral("local-dev"));
        entitlement.insert(QStringLiteral("plan"), QStringLiteral("Local Dev"));
        entitlement.insert(QStringLiteral("client_version"), appVersion());
        entitlement.insert(QStringLiteral("license_state"), licenseState_);
        QString cacheError;
        if (security_.cacheLocalEntitlement(entitlement, &cacheError)) {
            entitlementState_ = security_.entitlementState();
        } else {
            entitlementState_ = QStringLiteral("Entitlement cache failed: %1").arg(cacheError);
        }
        updateSecurityStatus();
        emit authChanged();
        emit navigationChanged();
        emit statusChanged();
        return;
    }
#endif

    authBusy_ = true;
    authMessage_ = QStringLiteral("Checking your code…");
    licenseState_ = QStringLiteral("Checking");
    emit authChanged();
    emit statusChanged();
    const QString machineId = security_.machineId();
    if (machineId.trimmed().isEmpty()) {
        authBusy_ = false;
        authMessage_ = QStringLiteral("Machine fingerprint unavailable. Restart Venice or check Windows identity services.");
        licenseState_ = QStringLiteral("Locked");
        appendLog(QStringLiteral("License activation blocked: machine_id unavailable."));
        emit authChanged();
        emit statusChanged();
        return;
    }
    appendLog(QStringLiteral("License activation request: key_suffix=%1 machine_id_len=%2 machine_id_suffix=%3 server=%4")
        .arg(normalized.right(4))
        .arg(machineId.size())
        .arg(machineId.right(8))
        .arg(licenseClient_.serverUrl().host()));
    authLicenseKey_ = normalized;
    licenseClient_.activate(normalized, machineId);
}

void OrionAppController::checkBackend()
{
    backendMessage_ = QStringLiteral("Checking Venice service and Remote Play backend...");
    updateState_ = QStringLiteral("Checking");
    appendLog(backendMessage_);
    emit statusChanged();
    licenseClient_.checkUpdate(appVersion(), updateChannel());
    remotePlay_.checkBackend();
}

QString OrionAppController::updateChannel() const noexcept
{
    return config_.data().updateChannel;
}

QString OrionAppController::accentColor() const noexcept
{
    return config_.data().customAccent;
}

void OrionAppController::setAccentColor(const QString& color)
{
    // Accept only #RRGGBB so a malformed settings value can't break QML bindings.
    static const QRegularExpression hexColor(QStringLiteral("^#[0-9A-Fa-f]{6}$"));
    const QString normalized = color.trimmed().toUpper();
    if (!hexColor.match(normalized).hasMatch()) {
        appendLog(QStringLiteral("Ignoring invalid accent color '%1'.").arg(color));
        return;
    }
    auto data = config_.data();
    if (data.customAccent.compare(normalized, Qt::CaseInsensitive) == 0) {
        return;
    }
    data.customAccent = normalized;
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setUpdateChannel(const QString& channel)
{
    const QString normalized = channel.trimmed().toLower();
    if (normalized != QLatin1String("dev") && normalized != QLatin1String("internal")
        && normalized != QLatin1String("beta") && normalized != QLatin1String("stable")) {
        appendLog(QStringLiteral("Ignoring unknown update channel '%1'.").arg(channel));
        return;
    }
    auto data = config_.data();
    if (data.updateChannel == normalized) {
        return;
    }
    data.updateChannel = normalized;
    saveConfigSilently(data);
    appendLog(QStringLiteral("Update channel -> %1; re-checking for updates.").arg(normalized));
    emit settingsChanged();
    licenseClient_.checkUpdate(appVersion(), normalized);
}

void OrionAppController::resolveUpdateGate(UpdateGateAction action)
{
    if (updateGateResolved_) {
        return; // later checks only refresh the pill; never re-block a live session
    }
    updateGateResolved_ = true;
    // Dev-only escape hatch so automated smokes/stress runs can reach the shell
    // (a dev checkout always trails the served version). No effect in production.
    if (devBuild_ && qEnvironmentVariableIsSet("ORION_SKIP_UPDATE_GATE")) {
        updateGatePhase_ = QStringLiteral("clear");
        appendLog(QStringLiteral("Update gate: skipped (ORION_SKIP_UPDATE_GATE, dev build)."));
        emit updateGateChanged();
        return;
    }
    switch (action) {
    case UpdateGateAction::Proceed:
        updateGatePhase_ = QStringLiteral("clear");
        break;
    case UpdateGateAction::OfferUpdate:
        updateGatePhase_ = QStringLiteral("offer");
        break;
    case UpdateGateAction::ForceUpdate:
        updateGatePhase_ = QStringLiteral("force");
        break;
    }
    if (updateGatePhase_ != QLatin1String("clear")) {
        if (devBuild_) {
            // Dev checkout: never auto-apply (the updater would overwrite a source
            // checkout) and don't block — proceed straight to the app.
            appendLog(QStringLiteral("Update gate: dev build — auto-update skipped, proceeding."));
            updateGatePhase_ = QStringLiteral("clear");
        } else if (updaterPresent() && !updateAlreadyAttemptedThisVersion()) {
            // Aggressive auto-update: apply immediately, no manual input. The gate page
            // shows progress while OrionUpdater runs; startUpdate() relaunches + quits.
            appendLog(QStringLiteral("Update gate: auto-applying update (%1) — no manual input.").arg(updateState_));
            noteUpdateAttempt();
            startUpdate();
        } else if (updaterPresent()) {
            // [ORION_UPDATE_NO_LOCKOUT] We ALREADY tried to update to this exact version and we
            // are still running the old one, so the update demonstrably failed. Trying again
            // cannot succeed for a different reason, and on a ForceUpdate gate it is a permanent
            // brick: force -> startUpdate() -> quit (300 ms) -> relaunch -> force -> ... with
            // continueWithoutUpdate() refusing to release the user (see :4135). The updater needs
            // to write the install dir, which for a default {autopf}\Orion install is Program
            // Files, and it is launched unelevated -- so for every non-admin customer the first
            // mandatory update ends the product permanently, with the updater's own log written
            // to that same unwritable directory and therefore lost.
            //
            // A failed update must never be worse than no update. Degrade to running the version
            // we have, loudly. The user keeps a working product and the failure is now visible.
            updateGatePhase_ = QStringLiteral("clear");
            appendLog(QStringLiteral(
                "Update gate: update to %1 was already attempted and did not apply — continuing "
                "on %2 rather than blocking. If this repeats, the installer directory is likely "
                "not writable by this account; reinstall to a per-user location or run the "
                "update as administrator.")
                          .arg(latestVersion_.isEmpty() ? QStringLiteral("latest") : latestVersion_,
                               appVersion()));
        } else {
            // Update available but the updater is missing — can't apply; don't block.
            appendLog(QStringLiteral("Update gate: update available but OrionUpdater missing — proceeding."));
            updateGatePhase_ = QStringLiteral("clear");
        }
    }
    emit updateGateChanged();
}

void OrionAppController::continueWithoutUpdate()
{
    if (updateGatePhase_ == QLatin1String("clear")) {
        return;
    }
    if (updateGatePhase_ == QLatin1String("force") && !devBuild_) {
        appendLog(QStringLiteral("Update gate: continue refused (mandatory update)."));
        return;
    }
    appendLog(QStringLiteral("Update gate: continuing on current version %1.").arg(appVersion()));
    updateGatePhase_ = QStringLiteral("clear");
    emit updateGateChanged();
    emit statusChanged();
}

bool OrionAppController::updaterPresent() const
{
    return QFileInfo::exists(QCoreApplication::applicationDirPath() + QStringLiteral("/OrionUpdater.exe"));
}

void OrionAppController::maybeAutoApplyUpdate()
{
    // [ORION_UPDATE_NO_LOCKOUT 2026-08-08] The complete automatic-apply
    // decision lives in silentAutoApplyAllowed() (UpdateGatePolicy.h) so it is
    // unit-testable. The alreadyAttempted term is the fix for the live
    // 18-relaunches-in-two-minutes loop (deployed log 2026-08-07 05:11-05:13Z):
    // the startup gate refused to re-attempt a failed 1.0.1 update, but this
    // silent path re-ran the doomed startUpdate() handoff on every relaunch.
    // The automatic path gets ONE attempt per target version; the explicit
    // user-clicked startUpdate() button intentionally bypasses this so a human
    // can retry after fixing the environment (e.g. running elevated).
    const bool streaming = remoteRunning_ || chiakiEmbedStatus_ == QLatin1String("Embedded");
    const bool alreadyAttempted = updateAlreadyAttemptedThisVersion();
    if (!silentAutoApplyAllowed(updateAvailable_, devBuild_, updaterPresent(),
                                streaming, alreadyAttempted)) {
        if (updateAvailable_ && !devBuild_ && updaterPresent()) {
            if (streaming) {
                // Never yank the user out of a live stream/game — defer until idle.
                appendLog(QStringLiteral("Silent update %1 ready; deferring until the stream ends.")
                              .arg(latestVersion_.isEmpty() ? QStringLiteral("latest") : latestVersion_));
            } else if (alreadyAttempted
                       && silentUpdateSuppressedLoggedVersion_ != latestVersion_) {
                // Once per target version, say WHY the update is not retrying
                // itself. Conservative surfacing choice: the loud remediation
                // guidance already comes from the startup gate's degrade line;
                // this line just keeps the silent path's decision visible.
                silentUpdateSuppressedLoggedVersion_ = latestVersion_;
                appendLog(QStringLiteral(
                              "Silent update: %1 already attempted and did not apply — not retrying "
                              "automatically. Use the update button to retry manually.")
                              .arg(latestVersion_.isEmpty() ? QStringLiteral("latest") : latestVersion_));
            }
        }
        return;
    }
    appendLog(QStringLiteral("Silent update: applying %1 now (idle session).")
                  .arg(latestVersion_.isEmpty() ? QStringLiteral("latest") : latestVersion_));
    // Record the attempt BEFORE the handoff (mirrors the startup gate at
    // :4477): if the updater fails and the app relaunches, both gates now
    // agree this version had its one automatic chance.
    noteUpdateAttempt();
    updateState_ = QStringLiteral("Updating");
    emit statusChanged();
    startUpdate();
}

bool OrionAppController::updateAlreadyAttemptedThisVersion() const
{
    // [ORION_UPDATE_NO_LOCKOUT] Persisted in the per-user data dir (NOT the install dir, which is
    // exactly the directory we may be unable to write). Keyed by target version so a genuinely
    // new release always gets one clean attempt.
    const QString target = latestVersion_.trimmed();
    if (target.isEmpty()) {
        return false;
    }
    QFile f(orionDataDir(rootDir_) + QStringLiteral("/update_attempt.txt"));
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return false;
    }
    return QString::fromUtf8(f.readAll()).trimmed() == target;
}

void OrionAppController::noteUpdateAttempt() const
{
    const QString target = latestVersion_.trimmed();
    if (target.isEmpty()) {
        return;
    }
    const QString dir = orionDataDir(rootDir_);
    QDir().mkpath(dir);
    QFile f(dir + QStringLiteral("/update_attempt.txt"));
    if (f.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) {
        f.write(target.toUtf8());
    }
}

void OrionAppController::clearUpdateAttempt() const
{
    // Called once we are demonstrably RUNNING a version that is not behind: the previous
    // attempt either succeeded or is irrelevant, so the next release starts with a clean slate.
    QFile::remove(orionDataDir(rootDir_) + QStringLiteral("/update_attempt.txt"));
}

void OrionAppController::startUpdate()
{
    if (!updateAvailable_ && !updateBlocked_) {
        appendLog(QStringLiteral("Update requested but none is available; ignoring."));
        return;
    }
    if (devBuild_) {
        // Dev guard: the updater would replace files in a source build tree.
        appendLog(QStringLiteral("Update refused: running from a build tree. Use the packaged install to update."));
        return;
    }
    const QString installDir = QCoreApplication::applicationDirPath();
    const QString updaterPath = installDir + QStringLiteral("/OrionUpdater.exe");
    if (!QFileInfo::exists(updaterPath)) {
        updateState_ = QStringLiteral("Updater missing");
        appendLog(QStringLiteral("Cannot update: OrionUpdater.exe is not next to OrionNative.exe."));
        emit statusChanged();
        return;
    }

    const QStringList args = {
        QStringLiteral("--manifest-url"), licenseClient_.updateManifestUrl(updateChannel()).toString(),
        QStringLiteral("--install-dir"),  installDir,
        QStringLiteral("--launcher-pid"), QString::number(QCoreApplication::applicationPid()),
        QStringLiteral("--current-version"), appVersion(),
        QStringLiteral("--relaunch"),     QStringLiteral("OrionNative.exe"),
    };
    appendLog(QStringLiteral("Handing off to OrionUpdater (%1 -> %2); Venice will now exit.")
                  .arg(appVersion(), latestVersion_.isEmpty() ? QStringLiteral("latest") : latestVersion_));
    // [ORION_UPDATE_ELEVATE] The updater REPLACES files in the install directory. A default
    // {autopf}\Orion install puts that in Program Files, which a standard user cannot write, and
    // neither OrionNative nor OrionUpdater carries a UAC manifest -- so a plain startDetached
    // produced an updater that could not copy anything, wrote its own diagnostic to that same
    // unwritable directory, and failed invisibly.
    //
    // Ask for elevation when a real create/delete probe cannot write both the install
    // and its parent (the verified helper/backup live beside the install). Qt's
    // QFileInfo::isWritable() does not consult NTFS ACLs by default on Windows.
    // If UAC is declined, keep this launcher running and the old install untouched.
    bool launched = false;
    const auto canCreateAndRemove = [](const QString& directory) {
        QTemporaryFile probe(QDir(directory).filePath(QStringLiteral(".venice-update-write-XXXXXX")));
        return probe.open() && probe.remove();
    };
    const bool installDirWritable = canCreateAndRemove(installDir)
        && canCreateAndRemove(QFileInfo(installDir).dir().absolutePath());
#ifdef Q_OS_WIN
    if (!installDirWritable) {
        appendLog(QStringLiteral(
            "Update: install directory is not writable by this account; requesting elevation "
            "for OrionUpdater."));
        const QString argString = [&args] {
            QStringList quoted;
            for (const QString& a : args) {
                quoted << (a.contains(QLatin1Char(' ')) ? QStringLiteral("\"%1\"").arg(a) : a);
            }
            return quoted.join(QLatin1Char(' '));
        }();
        SHELLEXECUTEINFOW info{};
        info.cbSize = sizeof(info);
        info.fMask = SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI;
        info.lpVerb = L"runas";
        const std::wstring exeW = QDir::toNativeSeparators(updaterPath).toStdWString();
        const std::wstring argW = argString.toStdWString();
        const std::wstring dirW = QDir::toNativeSeparators(installDir).toStdWString();
        info.lpFile = exeW.c_str();
        info.lpParameters = argW.c_str();
        info.lpDirectory = dirW.c_str();
        info.nShow = SW_SHOWNORMAL;
        launched = ShellExecuteExW(&info) != FALSE;
        if (!launched) {
            appendLog(QStringLiteral(
                "Update: elevation was declined or unavailable; Venice is keeping the current "
                "version running and no update files were changed."));
            updateState_ = QStringLiteral("Administrator approval required");
            emit statusChanged();
            return;
        }
    }
#else
    Q_UNUSED(installDirWritable);
#endif
    if (!launched && !QProcess::startDetached(updaterPath, args, installDir)) {
        updateState_ = QStringLiteral("Updater failed to start");
        appendLog(QStringLiteral("Failed to launch OrionUpdater.exe."));
        emit statusChanged();
        return;
    }
    // Exit so the updater can replace files while we are not holding them open.
    QTimer::singleShot(300, qApp, &QCoreApplication::quit);
}

void OrionAppController::toggleDefenseMode()
{
    defenseModeActive_ = !defenseModeActive_;
    // [ORION_METER_DELAY_BYPASS 2026-08-08] "Defense Mode off" is NOT evidence of
    // offense — asserting offense=true here (the pre-fix code) would pin the
    // OffenseDefense delay permanently ON the moment this dormant toggle is ever
    // used, recreating the "bypass on defense does nothing" bug. Engagement keys
    // on live shot inputs inside MeterDelayController; this path only ever
    // reports the DEFENSE direction (false), which is also the startup seed.
    meterDelay_.setOffense(false);
    veniceNet_.setOffense(false); // [VENICENET WAVE 2B]
    // Single clean gate: the engine refuses to arm (and resets any mid-flight
    // shot) while disarmed, so a held Square passes through untouched.
    syncEngineArmed();
    appendLog(defenseModeActive_
                  ? QStringLiteral("Defense Mode ON: shot automation disarmed; inputs pass through.")
                  : QStringLiteral("Defense Mode OFF: shot automation re-armed."));
    applyControllerLightbar(true); // defense color while active (when lightbar enabled)
    emit statusChanged();
}

void OrionAppController::syncEngineArmed()
{
    const bool routeReady = automationRouteReady(
        directInputWriteAllowed(remotePlay_.state(), remotePlay_.inputRecoveryPending()),
        controller_.isConnected());
    // [ORION_METER_DELAY_ROUTE_AUTHORITY 2026-08-08] Route/security authority and
    // the meter-delay ramp gate are evaluated separately so a delay-only disarm
    // can keep the controller-route attestation (see engineArmDecision() in
    // ControllerRoutingPolicy.h for the measured 72 s
    // waiting_for_latency_calibration bench this fixes).
    const bool routeAuthorityOk = routeReady
        && automationSecurityAllowed()
        && !applicationShutdownActive(applicationShutdownPhase_)
        && !defenseModeActive_ && !safeModeActive_
        && (!config_.data().inputTimedEnabled || !inputTimedPaused_)
        && (config_.data().inputTimedEnabled || !captureAwaitingFreshFrame_)
        && !inputRouteAwaitingRecovery_
        && !preciseFireDeliveryFault_
        && !shotIntentEdgeTracker_.transportRecoveryActive();
    // [ORION_METER_DELAY_FIRST_SHOT 2026-08-07] Use readyForArm() instead of
    // conditionSettled(). conditionSettled() reports false during the entire
    // ~2.5 s initial ramp to the first target on every session (100 ms/s slew
    // vs 250 ms target). That gate killed every shot in the initial ramp
    // window on every session. readyForArm() accepts the initial monotonic
    // climb (safe: delay strictly ascending, never oscillates) while still
    // refusing Backoff-recovery re-ramps (Fix 2 snaps those). See
    // MeterDelayController.h:readyForArm() for the contract; original callsite
    // used to gate on conditionSettled() to protect frozen learned_latency_ms.
    const bool meterDelayGateOk =
        config_.data().inputTimedEnabled || !meterDelay_.enabled() || meterDelay_.readyForArm();
    const EngineArmDecision decision =
        engineArmDecision(routeAuthorityOk, meterDelayGateOk);
    const bool shouldArm = decision.arm;
    automation_.setArmed(shouldArm);
    refreshPassiveLatencyAdaptationStatus(shouldArm);
    if (shouldArm && meterDelayRampDisarmLogged_) {
        meterDelayRampDisarmLogged_ = false;
        appendLog(QStringLiteral(
            "Meter delay ramp settled: engine re-armed on the preserved "
            "controller-route attestation (no fresh route proof needed)."));
    }
    if (!shouldArm) {
        if (decision.revokeRouteAttestation) {
            latencyCacheRouteAttestation_.revoke();
            automation_.setControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
        } else if (!meterDelayRampDisarmLogged_) {
            // Delay-gate-only disarm: the outbound controller route is untouched
            // by the inbound video delay, so the attestation survives and
            // authority returns the instant the ramp settles. Transition-deduped
            // (a ramp re-invokes syncEngineArmed every publish tick).
            meterDelayRampDisarmLogged_ = true;
            appendLog(QStringLiteral(
                "Meter delay ramp disarm: precise fire disarmed for the ramp; "
                "controller-route attestation preserved (delay gate only, "
                "route authority intact)."));
        }
        disarmPreciseFire();
        // A release signal can be emitted just before a route transition reaches this callback.
        // Do not let that now-undeliverable release survive as a pending submit/grade and later
        // masquerade as a real shot when the preview or a replacement route becomes active.
        if (pendingSubmitSeq_ >= 0) {
            automation_.cancelPostReleaseGrade(pendingSubmitSeq_);
            const QString notice = userReleaseTracker_.fail(
                pendingSubmitSeq_, UserReleaseFailureReason::ControllerRouteRevoked);
            appendCustomerEvent(notice);
            pendingSubmitSeq_ = -1;
        }
        releaseMarkerDeliveryGate_.reset();
        pendingSubmitPhysicalShotEpoch_ = 0;
        pendingSubmitShotAttempt_ = 0;
        pendingSubmitScheduleToken_ = 0;
        pendingSubmitRouteGeneration_ = 0;
        pendingSubmitRoute_ = LatencyControllerRoute::None;
        pendingSubmitDeliveryStage_ = PreciseFireDeliveryStage::None;
        pendingSubmitFireToken_ = 0;
        pendingSubmitTransportSeq_ = 0;
        pendingSubmitSnapshot_ = {};
        pendingSubmitFrameGrid_ = {};
        confirmedPreciseFireStage_ = PreciseFireDeliveryStage::None;
        confirmedPreciseFireToken_ = 0;
        confirmedPreciseFireTransportSeq_ = 0;
        confirmedPreciseFireSnapshot_ = {};
    }
}

void OrionAppController::refreshPassiveLatencyAdaptationStatus(bool routeReady)
{
    const RemapConfig timingConfig = automation_.config();
    const bool autonomousLiveMeter = timingConfig.autonomousVision
        && !timingConfig.noMeterEnabled && !timingConfig.inputTimedEnabled;
    if (!autonomousLiveMeter || automation_.autonomousLiveMeterReady()
        || automation_.latencyCalibrationMode()) {
        return;
    }
    const QString nextStatus = routeReady
        ? QStringLiteral(
            "Verifying automatic timing authority; automation stays inactive until the route is proven.")
        : QStringLiteral("Waiting for a live controller and video route.");
    if (latencyCalibrationStatus_ != nextStatus) {
        latencyCalibrationStatus_ = nextStatus;
        emit latencyCalibrationChanged();
    }
}

void OrionAppController::fencePreciseFireToken(quint64 token, bool fallbackTakeover)
{
    if (!fireThread_ || token == 0) {
        return;
    }

    // Direct connection on the controller thread. disarm() serializes with the
    // final submit; if submit already won, consume+confirm it before the engine
    // decides whether the token may be cleared.
    fireThread_->disarm(token);
    quint64 firedToken = 0;
    double firedActualMs = -1.0;
    PreciseFireDeliveryStage firedStage = PreciseFireDeliveryStage::None;
    uint32_t firedTransportSeq = 0;
    PreciseFireDeliverySnapshot firedSnapshot;
    const bool submitted = fireThread_->takeFired(
        &firedToken, &firedActualMs, &firedStage, &firedTransportSeq,
        &firedSnapshot, token);
    if (submitted) {
        confirmPreciseFire(
            firedToken, firedActualMs, firedStage, firedTransportSeq, firedSnapshot);
    } else {
        // A failure can complete between the GUI mailbox poll and this direct
        // invalidation fence. Consume only this token and latch the route fault
        // now; leaving it queued would permit a replacement schedule to arm and
        // fire before the next poll. WrittenUnconfirmed remains fail-closed
        // because bytes may already be queued in Chiaki.
        quint64 failedToken = 0;
        QString failedDetail;
        if (fireThread_->takeFailure(&failedToken, &failedDetail, token)) {
            automation_.rejectScheduledFire(token);
            preciseFireDeliveryFault_ = true;
            preciseFireRecoveryNeutralFrames_ = 0;
            automation_.setArmed(false);
            const QString userNotice = UserFacingReleaseTracker::failureNotice(
                UserReleaseFailureReason::PreciseWriteFailed);
            appendCustomerEvent(userNotice);
            setControllerLifecycle(
                ControllerLifecycleState::ControllerFault,
                QStringLiteral("Precise release NOT submitted; controller route recovery required"));
            appendLog(QStringLiteral(
                "Precise release NOT_SUBMITTED during %1 fence: token=%2 %3; "
                "automation disarmed pending route recovery.")
                          .arg(fallbackTakeover
                                   ? QStringLiteral("grace-takeover")
                                   : QStringLiteral("authority"))
                          .arg(failedToken)
                          .arg(failedDetail.left(160)));
        }
    }

    quint64 expected = token;
    lastArmedFireToken_.compare_exchange_strong(
        expected, 0, std::memory_order_acq_rel);
}

void OrionAppController::applyPendingPhaseAnchorRefinement()
{
    const PhaseAnchorRefinementProposal proposal =
        automation_.pendingPhaseAnchorRefinement();
    if (!proposal.valid()) {
        return;
    }
    auto reject = [this, &proposal](const QString& reason) {
        automation_.rejectPhaseAnchorRefinement(proposal.id, reason);
        appendLog(QStringLiteral(
            "PRECISE FIRE RETARGET: disposition=kept_fallback stage=%1 proposal=%2 "
            "token=%3 reason=%4")
                      .arg(proposal.stagePct)
                      .arg(proposal.id)
                      .arg(proposal.scheduleToken)
                      .arg(reason.left(64)));
    };
    if (!fireThread_ || proposal.scheduleToken != automation_.scheduledFireToken()) {
        reject(QStringLiteral("worker_or_engine_token_missing"));
        return;
    }
    const quint64 armedToken =
        lastArmedFireToken_.load(std::memory_order_acquire);
    if (armedToken == 0) {
        // The engine token exists but the controller has not copied it into the worker yet.
        // Committing now is safe: the ordinary arm block will see only the refined deadline.
        if (automation_.commitPhaseAnchorRefinement(
                proposal.id, proposal.scheduleToken, proposal.refinedDeadlineMs)) {
            appendLog(QStringLiteral(
                "PRECISE FIRE RETARGET: disposition=committed_before_worker stage=%1 "
                "proposal=%2 token=%3")
                          .arg(proposal.stagePct)
                          .arg(proposal.id)
                          .arg(proposal.scheduleToken));
        } else {
            reject(QStringLiteral("engine_commit_refused"));
        }
        return;
    }
    if (armedToken != proposal.scheduleToken) {
        reject(QStringLiteral("wrong_worker_token"));
        return;
    }

    const PreciseFireRetargetResult result = fireThread_->retarget(proposal);
    if (result == PreciseFireRetargetResult::Retargeted) {
        appendLog(QStringLiteral(
            "PRECISE FIRE RETARGET: disposition=retargeted stage=%1 proposal=%2 "
            "token=%3 eta_ms=%4")
                      .arg(proposal.stagePct)
                      .arg(proposal.id)
                      .arg(proposal.scheduleToken)
                      .arg(proposal.refinedDeadlineMs - automation_.engineNowMs(),
                           0, 'f', 3));
        return;
    }
    QString reason;
    switch (result) {
    case PreciseFireRetargetResult::Retargeted:
        return;
    case PreciseFireRetargetResult::EngineDisarmed:
        reason = QStringLiteral("engine_disarmed"); break;
    case PreciseFireRetargetResult::InvalidToken:
        reason = QStringLiteral("invalid_token"); break;
    case PreciseFireRetargetResult::WrongToken:
        reason = QStringLiteral("wrong_token"); break;
    case PreciseFireRetargetResult::NotWaiting:
        reason = QStringLiteral("worker_claimed"); break;
    case PreciseFireRetargetResult::OutcomePending:
        reason = QStringLiteral("outcome_pending"); break;
    case PreciseFireRetargetResult::RouteRejected:
        reason = QStringLiteral("route_rejected"); break;
    case PreciseFireRetargetResult::WindowRejected:
        reason = QStringLiteral("window_rejected"); break;
    case PreciseFireRetargetResult::EngineRejected:
        reason = QStringLiteral("engine_rejected"); break;
    }
    reject(reason);
}

void OrionAppController::confirmPreciseFire(
    quint64 token, double actualMs, PreciseFireDeliveryStage stage, uint32_t transportSeq,
    const PreciseFireDeliverySnapshot& snapshot)
{
    if (token == 0 || token != automation_.scheduledFireToken()
        || stage == PreciseFireDeliveryStage::None
        || !automation_.scheduledFireRouteBindingMatches(
            snapshot.routeGeneration, snapshot.route)
        || (stage == PreciseFireDeliveryStage::LocalUdpAccepted
            && snapshot.route != LatencyControllerRoute::Pipe)
        || (stage == PreciseFireDeliveryStage::ActiveVigemSubmit
            && snapshot.route != LatencyControllerRoute::VigemDs4
            && snapshot.route != LatencyControllerRoute::VigemXusb)) {
        confirmedPreciseFireToken_ = 0;
        confirmedPreciseFireStage_ = PreciseFireDeliveryStage::None;
        confirmedPreciseFireTransportSeq_ = 0;
        confirmedPreciseFireSnapshot_ = {};
        return;
    }
    // Preserve the exact worker result until AutomationEngine emits releaseIssued. A later GUI-tick
    // write is a different transaction and must never overwrite the route proof for this token.
    confirmedPreciseFireToken_ = token;
    confirmedPreciseFireStage_ = stage;
    confirmedPreciseFireTransportSeq_ = transportSeq;
    confirmedPreciseFireSnapshot_ = snapshot;
    automation_.confirmScheduledFire(token, actualMs);
}

void OrionAppController::disarmPreciseFire(bool confirmSubmitted)
{
    // Token-independent by design: the GUI-freeze worker can arrive while the
    // GUI thread is between FireThread::arm() and its token-mirror store. Both
    // arm and disarmCurrent share submitMutex_ -> m_, and arm re-checks the
    // atomic engine gate under that fence.
    if (!fireThread_) {
        lastArmedFireToken_.store(0, std::memory_order_release);
        return;
    }
    fireThread_->disarmCurrent();
    lastArmedFireToken_.store(0, std::memory_order_release);

    quint64 firedToken = 0;
    double firedActualMs = -1.0;
    PreciseFireDeliveryStage firedStage = PreciseFireDeliveryStage::None;
    uint32_t firedTransportSeq = 0;
    PreciseFireDeliverySnapshot firedSnapshot;
    if (fireThread_->takeFired(
            &firedToken, &firedActualMs, &firedStage, &firedTransportSeq,
            &firedSnapshot)
        && confirmSubmitted) {
        confirmPreciseFire(
            firedToken, firedActualMs, firedStage, firedTransportSeq, firedSnapshot);
    }
    // Teardown/watchdog owns the user-visible fault in these paths. Drain a
    // worker failure so it cannot survive disconnect and poison a later session.
    quint64 failedToken = 0;
    QString failedDetail;
    fireThread_->takeFailure(&failedToken, &failedDetail);
}

bool OrionAppController::releaseStaleSquareOutputLocked(const QString& reason)
{
    if (!squareOutputWatchdogEnabled_) return false;
    const auto result = orionInput_.releaseSquareForWatchdog();
    if (result.attempted == 0) return false;
    squareOutputWatchdog_.noteReleaseAttempt();
    appendLog(QStringLiteral("%1: reason=%2 copies_attempted=%3 copies_accepted=%4 "
                             "local_route_ack=%5 console_ack=0")
                  .arg(result.accepted > 0 ? QStringLiteral("SQUARE WATCHDOG RELEASED")
                                           : QStringLiteral("SQUARE WATCHDOG RELEASE FAILED"), reason)
                  .arg(result.attempted).arg(result.accepted)
                  .arg(result.accepted == 2 ? 1 : 0));
    if (result.accepted != 2) {
        preciseFireDeliveryFault_ = true;
        preciseFireRecoveryNeutralFrames_ = 0;
        automation_.setArmed(false);
    }
    return true;
}

void OrionAppController::neutralizeOwnedInput()
{
    QMutexLocker submitLock(&submitMutex_);
    // Callers already disarmed/revoked ownership. The optional watchdog adds
    // its bounded Square-up copies; it never gates mandatory neutral cleanup.
    releaseStaleSquareOutputLocked(QStringLiteral("owned_input_teardown"));
    ControllerState neutral;
    neutral.lightbarSet = false;
    QString ignored;
    controller_.submit(neutral, &ignored);
    const bool directRouteOwned = directInputRouteCurrentlyOwned(
        orionInput_.connected(), orionInput_.haveSent(),
        orionInput_.haveSent() && orionInput_.lastSent().own != 0);
    if (directRouteOwned) {
        const bool previousSquare = (orionInput_.lastSent().buttons & (1u << 2)) != 0;
        // Cleanup is a delivery transaction, not an ordinary latest-wins
        // update. Even an identical cached neutral needs an exact local ACK.
        // The established-owned-route gate above forbids reconnecting or
        // seeding a new route merely because a virtual target disappeared.
        const auto result = orionInput_.sendDetailed(neutral, true, true);
        const bool accepted = result == InputRouteWriteResult::LocalUdpAccepted;
        if (accepted) {
            lastFireHookWriteUs_.store(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()).count(),
                std::memory_order_relaxed);
        }
        appendLog(QStringLiteral(
            "Owned input cleanup: previous_square=%1 requested_square=0 "
            "virtual_connected=%2 local_route_ack=%3 console_ack=0")
                      .arg(previousSquare ? 1 : 0)
                      .arg(controller_.isConnected() ? 1 : 0)
                      .arg(accepted ? 1 : 0));
    }
}

void OrionAppController::releaseFailedRemoteInputRoute()
{
    // The stateChanged callback runs on the GUI thread. Revoke every copied
    // deadline before touching either output route, then serialize the direct
    // pipe release and ViGEm destruction against the precise-fire worker.
    automation_.setArmed(false);
    disarmPreciseFire();
    automation_.reset();

    bool virtualPadUnplugged = false;
    bool hadDirectOwnership = false;
    bool directOwnershipReleased = false;
    {
        QMutexLocker submitLock(&submitMutex_);
        ControllerState neutral;
        neutral.lightbarSet = false;

        // own=false is materially different from a neutral owned packet: it
        // tells patched Chiaki that Orion no longer owns console input while a
        // capture-only sidecar remains alive. Never create/seed a fresh pipe
        // from this Error path: before Running, Chiaki's feedback sender is not
        // initialized and MUST_DELIVER must remain unsent.
        hadDirectOwnership = directInputRouteCurrentlyOwned(
            orionInput_.connected(), orionInput_.haveSent(),
            orionInput_.haveSent() && orionInput_.lastSent().own != 0);
        if (hadDirectOwnership) {
            releaseStaleSquareOutputLocked(QStringLiteral("remote_input_route_failed"));
            // No reconnect/seed from a terminal Error if watchdog ACK failed.
            directOwnershipReleased = orionInput_.connected()
                && orionInput_.send(neutral, false);
        }
        // Error terminates this input-process generation even when the
        // capture-only sidecar stays alive. A retry must establish and seed a
        // fresh pipe instead of inheriting a locally non-null stale handle.
        orionInput_.resetConnection();
        directPipeOwnsInput_ = false;

        if (controller_.isConnected()) {
            const int retiringSlot = controller_.xinputUserIndex() >= 0
                ? controller_.xinputUserIndex()
                : inferredVirtualXinputSlot_;
            if (retiringSlot >= 0) {
                retiredVirtualXinputSlot_ = retiringSlot;
                retiredVirtualXinputSlotUntilMs_ = QDateTime::currentMSecsSinceEpoch()
                    + kRetiredVirtualSlotQuarantineMs;
            }
            controller_.submit(neutral);
            controller_.disconnectController();
            inferredVirtualXinputSlot_ = -1;
            virtualPadUnplugged = true;
        }
    }

    shotIntentEdgeTracker_.reset();
    previousControllerUiState_ = ControllerState{};
    squareUpAuditTracker_.reset();
    hookDigitalRestSinceMs_ = -1;
    hookReleaseRepairDueMs_ = -1;
    hookReleaseRepairHavePreviousOutput_ = false;
    inputRouteAwaitingRecovery_ = false;
    preciseFireDeliveryFault_ = false;
    preciseFireRecoveryNeutralFrames_ = 0;
    hookDownHeartbeats_ = 0;
    hookRecoveryAttempts_ = 0;
    hookFullRestartEscalated_ = false;

    const ControllerLifecycleState fallbackLifecycle = physicalPadLive_
        ? ControllerLifecycleState::PhysicalLive
        : (rawInputPresent_ ? ControllerLifecycleState::PhysicalPresentNoLive
                            : ControllerLifecycleState::NoPhysical);
    const QString fallbackStatus = physicalPadLive_
        ? QStringLiteral("Physical controller live")
        : (rawInputPresent_ ? QStringLiteral("Physical detected, waiting for input report")
                            : QStringLiteral("Disconnected"));
    setControllerLifecycle(fallbackLifecycle, fallbackStatus);
    const QString directStatus = !hadDirectOwnership
        ? QStringLiteral("no established direct input ownership")
        : (directOwnershipReleased
            ? QStringLiteral("direct input ownership released")
            : QStringLiteral("direct input ownership release NOT confirmed; route remains fail-closed"));
    appendLog(virtualPadUnplugged
        ? QStringLiteral("Remote Play error: %1 and virtual pad unplugged; capture preview preserved.").arg(directStatus)
        : QStringLiteral("Remote Play error: %1; capture preview preserved.").arg(directStatus));
}

void OrionAppController::tripWatchdog(const QString& reason)
{
    // Safety FIRST, recovery second: neutral the virtual pad and disarm before
    // anything else — automation must never stay armed across a crash.
    captureAwaitingFreshFrame_ = true;
    automation_.setArmed(false);
    disarmPreciseFire();
    automation_.reset();
    neutralizeOwnedInput();
    appendLog(QStringLiteral("Watchdog trip: %1").arg(reason));

    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    if (watchdogWindowStartMs_ == 0 || now - watchdogWindowStartMs_ > 5 * 60 * 1000) {
        watchdogWindowStartMs_ = now;
        watchdogRecoveryCount_ = 0;
    }
    ++watchdogRecoveryCount_;
    appendLog(QStringLiteral("Watchdog: trip %1/%2 in 5min window (reason: %3)")
                  .arg(watchdogRecoveryCount_)
                  .arg(3)
                  .arg(reason));
    // C1: was >= 2. Two trips in 5 minutes is exactly what ONE transient blip produces when a
    // recovery restart's own cold start re-trips a watchdog (live: 3 sidecar restarts in 3 min
    // from an input-hook blip; each pairing risked the permanent latch). Three trips inside the
    // window is a genuine repeating failure; two is a recoverable hiccup + its echo. Safe mode
    // itself now also AUTO-RECOVERS after a clean re-stream (see onWatchdogTick), so the latch
    // can no longer permanently kill a session over a transient — while a real crash loop
    // (trips keep coming / the stream never re-heals) still stops exactly as before.
    if (watchdogRecoveryCount_ >= 3) {
        enterSafeMode(reason);
    }
    emit statusChanged();
}

void OrionAppController::enterSafeMode(const QString& reason)
{
    if (safeModeActive_) {
        return;
    }
    safeModeActive_ = true;
    safeModeReason_ = reason;
    // C1: arm the bounded auto-recovery policy — a fresh latch starts a fresh stability run
    // (configure is idempotent and never resets the per-session recovery budget).
    safeModeRecovery_.configure(kSafeModeStabilityWindowMs_, kSafeModeAutoRecoverMax_);
    safeModeRecovery_.onEnterSafeMode();
    syncEngineArmed();
    // [CL3-F8-003 2026-09-23] Same promise as the SAFE MODE dialog (TopStatusBar.qml). The
    // "SAFE MODE:" prefix stays: tools/diagnostics/session_report.py keys on it.
    appendLog(QStringLiteral("SAFE MODE: %1. Shots are paused. Venice usually turns them back on by itself "
                             "once the stream has been steady for %2 seconds, or click SAFE MODE "
                             "at the top, then Exit safe mode.")
                  .arg(reason)
                  .arg(kSafeModeStabilityWindowMs_ / 1000));
    appendLog(QStringLiteral("Safe mode engine detail: auto-recover budget %1/session")
                  .arg(kSafeModeAutoRecoverMax_));
    emit statusChanged();
}

void OrionAppController::exitSafeMode()
{
    if (!safeModeActive_) {
        return;
    }
    safeModeActive_ = false;
    safeModeReason_.clear();
    watchdogRecoveryCount_ = 0;
    watchdogWindowStartMs_ = 0;
    guiFreezeTripped_.store(false, std::memory_order_relaxed);
    syncEngineArmed();
    appendLog(QStringLiteral("Safe mode exited; automation re-armed."));
    emit statusChanged();
}

void OrionAppController::onWatchdogTick()
{
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    guiHeartbeatMs_.store(gui_freeze::monotonicMs(), std::memory_order_relaxed);

    // [ORION_INPUT_DEAD_UX] Periodic re-evaluation of the dead-input overlay. The direct pipe
    // can drop without a dedicated NOTIFY, and the press latch expires on wall clock; this 2 s
    // beat keeps the on-screen verdict honest between event-driven refreshes. Cheap (a few
    // comparisons; emits only on change).
    refreshInputDeliveryState();

    // The freeze worker tripped while the GUI was stuck — escalate now that we
    // are demonstrably alive again (the trip already neutraled + disarmed).
    if (guiFreezeTripped_.load(std::memory_order_relaxed) && !safeModeActive_) {
        enterSafeMode(QStringLiteral("Venice's window stopped responding for a few seconds"));
        return;
    }
    if (safeModeActive_) {
        // C1 AUTO-RECOVER: safe mode must protect against a crash loop, not permanently kill a
        // session over a transient (the "bot stops firing entirely" latch). While latched, feed
        // the bounded recovery policy one health observation per 2s tick: the stream must be
        // RUNNING with a fresh frame feed, CONTINUOUSLY for the stability window (any unhealthy
        // tick resets the run — a crash-looping/dead stream never accumulates it), and at most
        // kSafeModeAutoRecoverMax_ auto-exits per session. exitSafeMode() re-arms + clears the
        // trip counters exactly as the manual button does; the manual path stays untouched.
        // Note: the sidecar's in-process self-heal / an HDMI renegotiation completing is what
        // re-heals the feed here — safe mode never spawns restarts itself, so a genuinely
        // crashing sidecar stays down and stays latched (manual reset only).
        const bool streamHealthy = remoteRunning_
            && remotePlay_.state() == RemotePlayState::Running
            && remotePlay_.frameAgeMs() >= 0.0
            && remotePlay_.frameAgeMs() < kSafeModeHealthyFrameAgeMs_;
        if (safeModeRecovery_.observe(streamHealthy, now)) {
            appendLog(QStringLiteral("SAFE MODE auto-recovery: stream healthy for %1s after '%2' — "
                                     "re-arming automation (auto-recovery %3/%4 this session; a "
                                     "repeated failure will still require a manual reset).")
                          .arg(kSafeModeStabilityWindowMs_ / 1000)
                          .arg(safeModeReason_)
                          .arg(safeModeRecovery_.autoRecoveries())
                          .arg(kSafeModeAutoRecoverMax_));
            exitSafeMode();
        }
        return;
    }

    // Capture-transport stall: detector freshness can safely disarm automation
    // without destroying a healthy stream. Process recovery is reserved for
    // raw delivery/backend failure. One restart per occurrence; repeats escalate
    // via tripWatchdog's window.
    // RC-1: in capture-card mode Chiaki is INPUT-ONLY — the video feed is the capture card, not the
    // Chiaki stream window. So the lifecycle watchdog keys on raw capture transport/backend health,
    // never on the Chiaki embed window: an un-embedded / relaunched Chiaki window is normal in cc-mode
    // and must not restart the sidecar or spin up the embed-window hunter (that respawned a floating
    // chiaki + the 15-21s freeze loop). remoteRunning_ already covers the cc-mode session liveness.
    const bool captureCardMode =
        config_.data().videoSource.compare(QLatin1String("capture_card"), Qt::CaseInsensitive) == 0;
    const bool streamLive = remoteRunning_
        || (!captureCardMode && chiakiEmbedStatus_ == QLatin1String("Embedded"));
    const double frameAge = remotePlay_.frameAgeMs();
    const double pixelAge = remotePlay_.pixelAgeMs();
    const double transportAge = remotePlay_.transportAgeMs();
    const bool backendFrozen = remotePlay_.backendFrozen();
    const bool transportFailed = streamTransportNeedsRestart(transportAge, backendFrozen);
    // Keep lifecycle recovery separate from detector authority. frameAge/pixelAge
    // still fail detection closed, but a static court transition or loading screen
    // is not a dead process while raw frames continue arriving. Restart only after
    // raw transport delivery has stopped for the bounded threshold or the backend
    // explicitly reports its own terminal frozen state.
    if (streamLive && transportFailed && !config_.data().inputTimedEnabled) {
        // Occlusion-aware: MINIMIZING the Orion window stalls the Vulkan present, which freezes
        // the decoded-frame feed. That's expected + fully recoverable (the present resumes the
        // instant the window is restored) — NOT a sidecar fault. Escalating it to a restart +
        // SAFE MODE forced a manual recovery just because the user stepped away. Hold instead:
        // the stall already disarms the engine, and the feed resumes when Orion returns to the
        // foreground. Only a stall while Orion is VISIBLE is a real fault worth restarting.
        bool orionMinimized = false;
#ifdef Q_OS_WIN
        const HWND mainHwnd = mainWindowHandle();
        orionMinimized = mainHwnd && IsIconic(mainHwnd);
#endif
        if (orionMinimized) {
            if (!occlusionPauseLogged_) {
                // [RT-LOW-01 2026-09-23] Customer copy says Venice, never the internal codename.
                appendLog(QStringLiteral("Feed paused: Venice is minimized, so shots are paused. "
                                         "They resume when you bring the Venice window back."));
                occlusionPauseLogged_ = true;
            }
            frameStallSinceMs_ = 0;   // don't accumulate toward a restart/safe-mode while minimized
        } else if (lastWatchdogRestartMs_ != 0 && now - lastWatchdogRestartMs_ < 40'000) {
            // Post-restart COLD-START GRACE: a fresh chiaki takes ~11-20s (Vulkan init) before
            // frames flow, and the capture card needs a re-open on top. Counting that startup
            // gap as a NEW stall is how restart #1 manufactured trip #2 (-> safe mode). Don't
            // accumulate until the grace passes; a genuinely dead feed still trips ~48s later.
            frameStallSinceMs_ = 0;
        } else if (frameStallSinceMs_ == 0) {
            frameStallSinceMs_ = now;
        } else if (now - frameStallSinceMs_ >= 1500
                   && now - lastWatchdogRestartMs_ > 30'000) {
            lastWatchdogRestartMs_ = now;
            frameStallSinceMs_ = 0;
            // [CL3-F8-001 2026-09-23] The trip reason is customer copy (Activity feed and the
            // SAFE MODE dialog's "Reason:"); the numbers go to an engineering line (rule 0).
            appendLog(QStringLiteral("Watchdog engine detail: capture transport failed (transport age %1 ms, backend frozen=%2; detector frame age %3 ms, pixel age %4 ms)")
                          .arg(transportAge, 0, 'f', 0)
                          .arg(backendFrozen ? 1 : 0)
                          .arg(frameAge, 0, 'f', 0)
                          .arg(pixelAge, 0, 'f', 0));
            tripWatchdog(QStringLiteral("The capture card stopped sending video"));
            if (!safeModeActive_) {
                appendLog(QStringLiteral("Watchdog: restarting detection sidecar (capture transport failure)."));
                restartSidecarWithWindowContainment();
                // The wrapper keeps the 16ms containment hunter armed through
                // the entire cold start in every source mode; capture-card mode
                // still launches OrionStream for controller input.
                syncEngineArmed();
            }
        }
    } else {
        frameStallSinceMs_ = 0;
        occlusionPauseLogged_ = false;
    }

    // Connect-time decoder-export stall: the session is up but live video never started
    // flowing. chiaki's present can stall for tens of seconds right after connect, which
    // starves the decoded-frame export; the orchestrator then falls back to GDI capture of
    // the off-screen-parked chiaki window, which yields STATIC BLACK frames. Those frames
    // still "arrive", so frameAge stays low and the stall check above never fires — the user
    // used to fix it by manually disconnecting/reconnecting a few times. Detect it precisely:
    // we are in decoder mode, the capture tier never became "decoder", and no unique (live)
    // frames are flowing well past warm-up. A healthy (even slow) warm-up flips the tier to
    // "decoder" the instant decoded frames arrive, so this never interrupts a real warm-up.
    // Auto-restart the stream once (what the user did by hand), bounded by the shared
    // startup-restart cap + spacing so it can't loop.
    // Publish the live capture tier so the engine can re-verify the VIDEO half of a packaged
    // factory latency prior. Capture-card and decoder differ by an entire encode/decode stage, so
    // a decoder prior applied to a capture-card session is a silently wrong actuation lead. The
    // setter is a no-op when unchanged and treats an empty/unknown tier as "not attested", so a
    // pre-warmup session keeps the previous controller-half-only behaviour rather than failing.
    automation_.setLatencyVideoRoute(remotePlay_.captureTier());

    const bool usingDecoderSource =
        config_.data().videoSource.compare(QLatin1String("capture_card"), Qt::CaseInsensitive) != 0;
    const bool decoderStalled = usingDecoderSource
        && remotePlay_.captureTier() != QLatin1String("decoder")
        && remotePlay_.uniqueFrameFps() <= 0;
    // 35s (was 15s): chiaki's own cold startup is ~20s+ before frames can flow — a CONSISTENT
    // ~11s Vulkan device init (pl_vulkan_create; measured across every session) BEFORE it even
    // creates the frame-export pipe, then the PS5 session handshake. The old 15s timeout fired
    // mid-startup and killed chiaki before it could connect -> relaunch loop + a stray parked
    // window (the user's "stall then a chiaki window appears"). The orchestrator now pre-attaches
    // the pipe reader so the session starts ~as soon as chiaki is up, but keep generous margin so
    // a slow-but-healthy startup is never killed; the 4-attempt cap still recovers a genuine fail.
    // FIX D: never run the decoder-stall restart when the sidecar already reported a TERMINAL
    // error (e.g. a genuine launch failure) — restarting just loops 4x and respawns an
    // un-embeddable (floating) chiaki window. Surface the error instead of looping. (With the
    // orchestrator's HWND-decouple, a not-yet-visible window no longer errors here.)
    if (streamLive && !config_.data().inputTimedEnabled
            && remotePlay_.state() != RemotePlayState::Error
            && now < streamStartupGraceUntilMs_
            && now - streamConnectMs_ > 35000
            && decoderStalled
            && streamStartupRestarts_ < 4
            && now - lastWatchdogRestartMs_ > 30000) {
        ++streamStartupRestarts_;
        lastWatchdogRestartMs_ = now;
        appendLog(QStringLiteral("Watchdog: live video not flowing %1s after connect "
                                 "(decoder export stalled, capture=%2) — auto-restarting the stream "
                                 "(attempt %3/4) so you don't have to reconnect by hand.")
                      .arg((now - streamConnectMs_) / 1000)
                      .arg(remotePlay_.captureResolution())
                      .arg(streamStartupRestarts_));
        restartSidecarWithWindowContainment();
        syncEngineArmed();
    }
}

void OrionAppController::setDefenseTriggerButton(const QString& value)
{
    // Defense Mode is toggled by D-pad Up only — there is no UI dropdown. Pin the
    // value so any caller (or a stale settings.json) can only resolve to dpad_up.
    Q_UNUSED(value);
    auto data = config_.data();
    if (data.defenseTriggerButton == QLatin1String("dpad_up")) {
        return;
    }
    data.defenseTriggerButton = QStringLiteral("dpad_up");
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setDefenseStickAssist(bool value)
{
    auto data = config_.data();
    if (data.defenseStickAssist == value) {
        return;
    }
    data.defenseStickAssist = value;
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setDefenseStickAssistStrength(double value)
{
    auto data = config_.data();
    const double clamped = qBound(0.0, value, 1.0);
    if (qFuzzyCompare(data.defenseStickAssistStrength, clamped)) {
        return;
    }
    data.defenseStickAssistStrength = clamped;
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setDefenseL2HoldAssist(bool value)
{
    auto data = config_.data();
    if (data.defenseL2HoldAssist == value) {
        return;
    }
    data.defenseL2HoldAssist = value;
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setDefenseSprintAssist(bool value)
{
    auto data = config_.data();
    if (data.defenseSprintAssist == value) {
        return;
    }
    data.defenseSprintAssist = value;
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setDefenseLightbarColor(const QString& value)
{
    static const QRegularExpression hexColor(QStringLiteral("^#[0-9A-Fa-f]{6}$"));
    const QString normalized = value.trimmed().toUpper();
    if (!hexColor.match(normalized).hasMatch()) {
        return;
    }
    auto data = config_.data();
    if (data.defenseLightbarColor.compare(normalized, Qt::CaseInsensitive) == 0) {
        return;
    }
    data.defenseLightbarColor = normalized;
    saveConfigSilently(data);
    if (defenseModeActive_) {
        applyControllerLightbar(true);
    }
    emit settingsChanged();
}

QString OrionAppController::exportDiagnostics()
{
    // Everything written here is support-safe: every text chunk passes
    // redactDiagnosticsText (license keys, tokens, emails, machine ids).
    QMap<QString, QString> files;

    QString versions;
    versions += QStringLiteral("client=%1\n").arg(displayVersion());
    versions += QStringLiteral("raw_version=%1\n").arg(appVersion());
    versions += QStringLiteral("channel=%1\n").arg(updateChannel());
    versions += QStringLiteral("dev_build=%1\n").arg(devBuild_ ? 1 : 0);
#ifdef ORION_PRODUCTION_BUILD
    versions += QStringLiteral("production_build=1\n");
#else
    versions += QStringLiteral("production_build=0\n");
#endif
    versions += QStringLiteral("updater_present=%1\n").arg(updaterPresent() ? 1 : 0);
    versions += QStringLiteral("update_state=%1\n").arg(updateState_);
    versions += QStringLiteral("active_profile=%1\n").arg(config_.data().activeProfile);
    versions += QStringLiteral("chiaki_path=%1\n").arg(config_.data().chiakiPath);
    versions += QStringLiteral("safe_mode=%1 reason=%2\n").arg(safeModeActive_ ? 1 : 0).arg(safeModeReason_);
    files.insert(QStringLiteral("versions.txt"), versions);

    QString license;
    license += QStringLiteral("license_state=%1\n").arg(licenseState_);
    license += QStringLiteral("entitlement=%1\n").arg(entitlementState_);
    license += QStringLiteral("security=%1 detail=%2\n").arg(securityState_, securityDetail_);
    license += QStringLiteral("integrity=%1\n").arg(integrityState_);
    license += QStringLiteral("time_left=%1\n").arg(timeLeft());
    files.insert(QStringLiteral("license.txt"), license);

    QString controller;
    controller += QStringLiteral("status=%1\n").arg(controllerStatus_);
    controller += QStringLiteral("source=%1\n").arg(controllerInputSource_);
    controller += QStringLiteral("device=%1\n").arg(controllerDeviceDetail_);
    controller += QStringLiteral("stable_device=%1\n").arg(controllerStableDevice_);
    controller += QStringLiteral("health=%1\n").arg(controllerHealth());
    controller += QStringLiteral("defense_mode=%1\n").arg(defenseModeActive_ ? 1 : 0);
    files.insert(QStringLiteral("controller.txt"), controller);

    QString capture;
    capture += QStringLiteral("capture_source=%1\n").arg(captureSourceHealth_);
    capture += QStringLiteral("unique_fps=%1\n").arg(remotePlay_.uniqueFrameFps());
    capture += QStringLiteral("capture_loop_fps=%1\n").arg(remotePlay_.captureLoopFps());
    capture += QStringLiteral("duplicate_pct=%1\n").arg(remotePlay_.duplicateFramePct(), 0, 'f', 1);
    capture += QStringLiteral("frame_age_ms=%1\n").arg(remotePlay_.frameAgeMs(), 0, 'f', 1);
    capture += QStringLiteral("embed=%1\n").arg(chiakiEmbedStatus_);
    files.insert(QStringLiteral("capture.txt"), capture);

    QString network;
    network += QStringLiteral("rtt_ms=%1\n").arg(rttMs(), 0, 'f', 1);
    network += QStringLiteral("rtt_raw_ms=%1\n").arg(telemetry_.rttMs, 0, 'f', 1);
    network += QStringLiteral("rtt_verified=%1\n").arg(rttVerified() ? 1 : 0);
    network += QStringLiteral("jitter_ms=%1\n").arg(jitterMs(), 0, 'f', 1);
    network += QStringLiteral("applied_offset_ms=%1\n").arg(effectiveSyncAdjustMs(), 0, 'f', 1);
    network += QStringLiteral("sync_source=%1 confidence=%2\n").arg(syncSource()).arg(syncConfidence(), 0, 'f', 2);
    network += QStringLiteral("wifi_mode=%1\n").arg(wifiModeActive() ? 1 : 0);
    network += QStringLiteral("tick_phase_locked=%1\n").arg(tickPhaseVerified() ? 1 : 0);
    network += QStringLiteral("packet_capture_enabled=%1 active=%2\n")
                   .arg(packetCaptureEnabled() ? 1 : 0)
                   .arg(packetCaptureActive() ? 1 : 0);
    network += QStringLiteral("packets_in=%1 out=%2\n").arg(inboundPackets()).arg(outboundPackets());
    // The UNMASKED court endpoint lives here and only here. The Network card
    // shows the masked form because it is permanently on screen; this bundle is
    // produced by an explicit user action, so it carries the full value support
    // actually needs.
    network += QStringLiteral("court_ip=%1 verified=%2\n")
                   .arg(telemetryCourtIp().isEmpty() ? QStringLiteral("-") : telemetryCourtIp())
                   .arg(courtIpVerified() ? 1 : 0);
    files.insert(QStringLiteral("network.txt"), network);

    files.insert(QStringLiteral("calibration.json"), calibrationSnapshot());

    // Last 50 shot-telemetry rows from the in-memory log ring.
    QStringList shotLines;
    for (const QString& line : logs_) {
        if (line.contains(QLatin1String("Release telemetry"))
            || line.contains(QLatin1String("Shot outcome:"))
            || line.contains(QLatin1String("Self-grade diagnostic"))
            || line.contains(QLatin1String("Scheduled fire:"))
            || line.contains(QLatin1String("Release submit:"))) {
            shotLines << line;
        }
    }
    while (shotLines.size() > 50) {
        shotLines.removeFirst();
    }
    files.insert(QStringLiteral("shots.txt"), shotLines.join(QLatin1Char('\n')));
    files.insert(QStringLiteral("recent_log.txt"), logs_.join(QLatin1Char('\n')));

    // Tail of the persistent app log (~last 800 lines).
    {
        QFile logFile(orionDataDir(rootDir_) + QStringLiteral("/logs/orion_native.log"));
        if (logFile.open(QIODevice::ReadOnly | QIODevice::Text)) {
            const qint64 maxBytes = 256 * 1024;
            if (logFile.size() > maxBytes) {
                logFile.seek(logFile.size() - maxBytes);
            }
            files.insert(QStringLiteral("app_log_tail.txt"), QString::fromUtf8(logFile.readAll()));
        }
    }
    {
        QFile updaterLog(QCoreApplication::applicationDirPath() + QStringLiteral("/orion_updater.log"));
        if (updaterLog.open(QIODevice::ReadOnly | QIODevice::Text)) {
            files.insert(QStringLiteral("updater_log.txt"), QString::fromUtf8(updaterLog.readAll()));
        }
    }

    // Crash dumps: list every dump as metadata AND bundle the newest few raw
    // (a minidump is binary process memory, so it can't be text-redacted — the
    // user is told that in the bundle's README and chooses whether to send it).
    QFileInfoList bundledDumps;
    {
        QString dumps;
        QFileInfoList allDumps;
        const QStringList dumpDirs = {rootDir_, QCoreApplication::applicationDirPath(),
                                      orionDataDir(rootDir_) + QStringLiteral("/logs")};
        for (const QString& dir : dumpDirs) {
            const QFileInfoList found = QDir(dir).entryInfoList({QStringLiteral("*.dmp")}, QDir::Files, QDir::Time);
            for (const QFileInfo& fi : found) {
                allDumps.append(fi);
                dumps += QStringLiteral("%1  %2 bytes  %3\n")
                             .arg(fi.fileName())
                             .arg(fi.size())
                             .arg(fi.lastModified().toUTC().toString(Qt::ISODate));
            }
        }
        std::sort(allDumps.begin(), allDumps.end(), [](const QFileInfo& a, const QFileInfo& b) {
            return a.lastModified() > b.lastModified();
        });
        constexpr qint64 kMaxDumpBytes = 32 * 1024 * 1024;
        for (const QFileInfo& fi : allDumps) {
            if (bundledDumps.size() >= 3 || fi.size() > kMaxDumpBytes) {
                continue;
            }
            bundledDumps.append(fi);
        }
        files.insert(QStringLiteral("crash_dumps.txt"),
                     dumps.isEmpty() ? QStringLiteral("none\n") : dumps);
        files.insert(QStringLiteral("README.txt"),
                     QStringLiteral("Venice diagnostics bundle.\n"
                                    "All text files are redacted (license keys, tokens, emails stripped).\n"
                                    "crash_dumps/ contains raw minidumps (binary memory snapshots that\n"
                                    "cannot be redacted) — include them only if support asks for them.\n"));
    }

    const QString desktop = QStandardPaths::writableLocation(QStandardPaths::DesktopLocation);
    const QString zipPath = desktop + QStringLiteral("/venice_diagnostics_")
                            + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd_HHmmss"))
                            + QStringLiteral(".zip");
    QZipWriter zip(zipPath);
    if (zip.status() != QZipWriter::NoError) {
        appendLog(QStringLiteral("Diagnostics export failed: cannot create %1").arg(zipPath));
        return QString();
    }
    for (auto it = files.constBegin(); it != files.constEnd(); ++it) {
        zip.addFile(it.key(), redactDiagnosticsText(it.value()).toUtf8());
    }
    for (const QFileInfo& fi : bundledDumps) {
        QFile dump(fi.absoluteFilePath());
        if (dump.open(QIODevice::ReadOnly)) {
            zip.addFile(QStringLiteral("crash_dumps/") + fi.fileName(), dump.readAll());
        }
    }
    zip.close();
    appendLog(QStringLiteral("Diagnostics exported (redacted) -> %1").arg(QDir::toNativeSeparators(zipPath)));
    return QDir::toNativeSeparators(zipPath);
}

void OrionAppController::openLogsFolder()
{
    const QString logsDir = orionDataDir(rootDir_) + QStringLiteral("/logs");
    QDir().mkpath(logsDir);
    QDesktopServices::openUrl(QUrl::fromLocalFile(logsDir));
}

// [VENICE_PROFILE 2026-08-08] The one profile entry point (see the header note: ONE
// slot + ONE signal by design). All policy — the export allowlist, the import clamps,
// never-export-license — lives in VeniceProfile.h and is covered by
// OrionVeniceProfileTests; this method contributes only file I/O and the persist
// ordering.
void OrionAppController::runVeniceProfileAction(const QString& action, const QUrl& fileUrl)
{
    const QString verb = action.trimmed().toLower();
    QString path = fileUrl.isLocalFile() ? fileUrl.toLocalFile()
                                         : fileUrl.toString(QUrl::PreferLocalFile);
    path = path.trimmed();
    if (path.isEmpty()) {
        emit veniceProfileActionCompleted(false, QStringLiteral("No file selected."));
        return;
    }

    if (verb == QLatin1String("export")) {
        if (!path.endsWith(QLatin1String(".json"), Qt::CaseInsensitive)) {
            path += QLatin1String(".json");
        }
        const QJsonObject profile = orion::veniceProfileExport(config_.data(), config_.learning());
        QSaveFile out(path);
        if (!out.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            const QString msg =
                QStringLiteral("Profile export failed: %1").arg(out.errorString());
            appendLog(msg);
            emit veniceProfileActionCompleted(false, msg);
            return;
        }
        out.write(QJsonDocument(profile).toJson(QJsonDocument::Indented));
        if (!out.commit()) {
            const QString msg =
                QStringLiteral("Profile export failed: %1").arg(out.errorString());
            appendLog(msg);
            emit veniceProfileActionCompleted(false, msg);
            return;
        }
        const QString shown = QDir::toNativeSeparators(path);
        appendLog(QStringLiteral("Venice profile exported -> %1").arg(shown));
        emit veniceProfileActionCompleted(
            true, QStringLiteral("Profile saved to %1").arg(shown));
        return;
    }

    if (verb == QLatin1String("import")) {
        QFile in(path);
        // A profile is a few KB; anything huge is not a profile. Bound the read so a
        // mistargeted pick (a video, a log) can never balloon memory here.
        constexpr qint64 kMaxProfileBytes = 1 * 1024 * 1024;
        if (!in.open(QIODevice::ReadOnly) || in.size() > kMaxProfileBytes) {
            const QString msg = QStringLiteral("Profile import failed: %1")
                                    .arg(in.isOpen() ? QStringLiteral("file too large")
                                                     : in.errorString());
            appendLog(msg);
            emit veniceProfileActionCompleted(false, msg);
            return;
        }
        QJsonParseError parseError{};
        const QJsonDocument doc = QJsonDocument::fromJson(in.readAll(), &parseError);
        if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
            const QString msg = QStringLiteral("Profile import failed: not valid JSON (%1)")
                                    .arg(parseError.errorString());
            appendLog(msg);
            emit veniceProfileActionCompleted(false, msg);
            return;
        }

        auto data = config_.data();
        auto learning = config_.learning();
        const auto result = orion::veniceProfileImport(doc.object(), data, learning);
        if (!result.ok) {
            appendLog(QStringLiteral("Profile import refused: %1").arg(result.error));
            emit veniceProfileActionCompleted(false, result.error);
            return;
        }
        for (const QString& note : result.notes) {
            appendLog(QStringLiteral("Profile import: %1").arg(note));
        }
        // ORDER MATTERS (same contract as setTipTimingMs): persist the learning slot
        // FIRST — the settings save below is what re-runs applyConfig
        // (syncBackendConfig), and applyConfig must restore the NEW tip prior (and
        // re-prime the meter-delay actuator) in the same pass.
        QString learningError;
        if (!config_.saveLearning(learning, &learningError)) {
            const QString msg = QStringLiteral("Profile import failed: learning save (%1) - "
                                               "no settings were changed")
                                    .arg(learningError);
            appendLog(msg);
            emit veniceProfileActionCompleted(false, msg);
            return;
        }
        if (!saveConfigSilently(data)) {
            emit veniceProfileActionCompleted(
                false, QStringLiteral("Profile import failed: settings save - see the log."));
            return;
        }
        const QString summary =
            QStringLiteral("Profile imported: %1 value(s) applied%2")
                .arg(result.applied.size())
                .arg(result.notes.isEmpty()
                         ? QString()
                         : QStringLiteral(", %1 adjusted/skipped (see log)")
                               .arg(result.notes.size()));
        appendLog(QStringLiteral("Venice profile imported <- %1 (%2)")
                      .arg(QDir::toNativeSeparators(path),
                           result.applied.join(QStringLiteral(", "))));
        emit veniceProfileActionCompleted(true, summary);
        return;
    }

    emit veniceProfileActionCompleted(
        false, QStringLiteral("Unknown profile action '%1'.").arg(action));
}

QString OrionAppController::profilesSnapshot() const
{
    const QJsonObject profiles = config_.profiles();
    QJsonArray list;
    // Default is always offered, even before the first explicit save.
    if (!profiles.contains(QStringLiteral("Default"))) {
        QJsonObject def;
        def.insert(QStringLiteral("name"), QStringLiteral("Default"));
        list.append(def);
    }
    for (auto it = profiles.constBegin(); it != profiles.constEnd(); ++it) {
        QJsonObject o = it.value().toObject();
        o.insert(QStringLiteral("name"), it.key());
        list.append(o);
    }
    QJsonObject root;
    root.insert(QStringLiteral("active"), config_.data().activeProfile);
    root.insert(QStringLiteral("profiles"), list);
    return QString::fromUtf8(QJsonDocument(root).toJson(QJsonDocument::Compact));
}

void OrionAppController::saveProfile(const QString& name, const QString& notes)
{
    const QString trimmed = name.trimmed().left(48);
    if (trimmed.isEmpty()) {
        appendLog(QStringLiteral("Profile save ignored: empty name."));
        return;
    }
    auto data = config_.data();
    QJsonObject profiles = config_.profiles();
    QJsonObject p;
    p.insert(QStringLiteral("notes"), notes.trimmed().left(200));
    p.insert(QStringLiteral("meter_style"), data.meterStyle);
    p.insert(QStringLiteral("meter_color"), data.meterColor);
    p.insert(QStringLiteral("bandwidth_mode"), data.streamBandwidthMode);
    p.insert(QStringLiteral("rtt_baseline_ms"), rttMs());
    p.insert(QStringLiteral("saved_at"), QDateTime::currentDateTimeUtc().toString(Qt::ISODate));
    profiles.insert(trimmed, p);
    config_.setProfiles(profiles);
    data.activeProfile = trimmed;
    saveConfigSilently(data);
    appendLog(QStringLiteral("Profile '%1' saved (meter %2/%3, %4).")
                  .arg(trimmed, data.meterStyle, data.meterColor, data.streamBandwidthMode));
    emit settingsChanged();
}

void OrionAppController::switchProfile(const QString& name)
{
    const QString trimmed = name.trimmed().left(48);
    if (trimmed.isEmpty() || trimmed == config_.data().activeProfile) {
        return;
    }
    const QJsonObject profiles = config_.profiles();
    const bool isDefault = trimmed.compare(QStringLiteral("Default"), Qt::CaseInsensitive) == 0;
    if (!isDefault && !profiles.contains(trimmed)) {
        appendLog(QStringLiteral("Profile '%1' does not exist.").arg(trimmed));
        return;
    }
    // Persist the outgoing profile's learning to ITS file before the path flips.
    config_.saveLearning(config_.learning());

    auto data = config_.data();
    data.activeProfile = trimmed;
    // Apply the profile's captured setup (meter style/color drive the detector;
    // bandwidth drives the stream preset).
    if (!isDefault) {
        const QJsonObject p = profiles.value(trimmed).toObject();
        const QString style = p.value(QStringLiteral("meter_style")).toString();
        const QString color = p.value(QStringLiteral("meter_color")).toString();
        const QString bandwidth = p.value(QStringLiteral("bandwidth_mode")).toString();
        if (!style.isEmpty()) {
            // [ORION_PILL_REMOVED 2026-09-21] A profile is a third ingress for meter_style
            // beside settings.json load and setMeterStyle (Astra found this bypass). Apply
            // the same acceptance rule: anything not on the list -- including a stored
            // "Pill" -- becomes Arrow2 instead of reaching applyPillYoloRoute.
            const QString normalized = normalizedMeterStyle(style);
            const bool accepted = normalized == style.trimmed().left(48);
            data.meterStyle = normalized;
            if (!accepted) {
                appendLog(QStringLiteral("Profile '%1': meter style '%2' is not available; using Arrow2.")
                              .arg(trimmed, style.trimmed().left(48)));
            }
        }
        if (!color.isEmpty()) {
            data.meterColor = color;
        }
        if (!bandwidth.isEmpty()) {
            data.streamBandwidthMode = bandwidth;
        }
    }
    saveConfigSilently(data);
    // Load the incoming profile's learning namespace and push it into the engine —
    // its per-type clocks/offsets warm-start exactly where that build left off.
    config_.reloadLearning();
    if (!config_.learningLoadNote().isEmpty()) {
        appendLog(QStringLiteral("Learning data: %1").arg(config_.learningLoadNote()));
    }
    syncBackendConfig();
    appendLog(QStringLiteral("Profile -> '%1' (learning namespace %2).")
                  .arg(trimmed, QFileInfo(config_.learningPath()).fileName()));
    emit settingsChanged();
    emit statusChanged();
}

void OrionAppController::deleteProfile(const QString& name)
{
    const QString trimmed = name.trimmed().left(48);
    if (trimmed.isEmpty() || trimmed.compare(QStringLiteral("Default"), Qt::CaseInsensitive) == 0) {
        return; // Default is not deletable
    }
    if (trimmed == config_.data().activeProfile) {
        switchProfile(QStringLiteral("Default"));
    }
    QJsonObject profiles = config_.profiles();
    if (!profiles.contains(trimmed)) {
        return;
    }
    profiles.remove(trimmed);
    config_.setProfiles(profiles);
    saveConfigSilently(config_.data());
    // The learning file is derived data; remove it with the profile.
    const QString slug = AppConfig::profileLearningSlug(trimmed);
    if (!slug.isEmpty()) {
        QFile::remove(rootDir_ + QStringLiteral("/learning.") + slug + QStringLiteral(".json"));
    }
    appendLog(QStringLiteral("Profile '%1' deleted.").arg(trimmed));
    emit settingsChanged();
}

void OrionAppController::applyDefenseAssists(ControllerState& output) const
{
    const auto& cfg = config_.data();
    if (cfg.defenseStickAssist) {
        // Faster lateral first-step: exponent < 1 boosts small deflections so the
        // defender reacts sooner; full deflection is unchanged.
        const double strength = qBound(0.0, cfg.defenseStickAssistStrength, 1.0);
        const double exponent = 1.0 - 0.45 * strength;
        const auto shape = [exponent](int v) {
            const double n = qBound(-1.0, v / 127.0, 1.0);
            const double shaped = std::copysign(std::pow(std::abs(n), exponent), n);
            return static_cast<int>(std::lround(shaped * 127.0));
        };
        output.leftStickX = shape(output.leftStickX);
        output.leftStickY = shape(output.leftStickY);
    }
    if (cfg.defenseL2HoldAssist && output.l2 > 24) {
        output.l2 = 255; // commit an L2 tap to a full intent hold
    }
    if (cfg.defenseSprintAssist && output.r2 > 24) {
        output.r2 = 255; // commit to full sprint while pressed
    }
}

void OrionAppController::detectPs5()
{
    if (isXboxRemotePlay(config_.data()))
        return;
    backendMessage_ = QStringLiteral("Console discovery is Chiaki-managed; checking passive telemetry state...");
    appendLog(backendMessage_);
    emit statusChanged();
    remotePlay_.discoverPs5();
}

void OrionAppController::openChiaki()
{
    if (isXboxRemotePlay(config_.data())) {
        openXboxRemotePlay();
        return;
    }
    saveRemoteSettings();

    // If no console IP is configured, run a quick UDP discovery sweep first so the
    // user doesn't have to press "Detect" manually before launching Chiaki.
    if (config_.data().remotePlayConsoleIp.trimmed().isEmpty()) {
        appendLog(QStringLiteral("No console IP set — running quick PS5 discovery before launching Chiaki..."));
        remotePlay_.discoverPs5();
    }

#ifdef Q_OS_WIN
    if (!findChiakiWindow()) {
        QProcess::execute(QStringLiteral("taskkill.exe"), {QStringLiteral("/IM"), QStringLiteral("OrionStream.exe"), QStringLiteral("/F"), QStringLiteral("/T")});
        QProcess::execute(QStringLiteral("taskkill.exe"), {QStringLiteral("/IM"), QStringLiteral("chiaki.exe"), QStringLiteral("/F"), QStringLiteral("/T")});
        QProcess::execute(QStringLiteral("taskkill.exe"), {QStringLiteral("/IM"), QStringLiteral("chiaki-ng.exe"), QStringLiteral("/F"), QStringLiteral("/T")});
        QProcess::execute(QStringLiteral("taskkill.exe"), {QStringLiteral("/IM"), QStringLiteral("chiaki4deck.exe"), QStringLiteral("/F"), QStringLiteral("/T")});
    }
#endif
    remotePlay_.applyConfig(config_.data());
    // Push the user's bandwidth preset into chiaki's settings store before launch so
    // resolution / fps / codec / bitrate / audio-buffer all take effect immediately.
    remotePlay_.applyBandwidthMode(bandwidthModeFromString(config_.data().streamBandwidthMode));
    remotePlay_.setAudioMode(config_.data().streamAudioMode);
    remotePlay_.openChiakiClient();
    // Don't auto-embed for the manual Open Chiaki path — leave Chiaki visible as a
    // separate window so the user can verify the stream / dismiss any dialogs before
    // pressing Connect (which is where embed/capture is supposed to take over).
}

void OrionAppController::updateChiakiEmbedRect(int x, int y, int width, int height)
{
    // QML render mode: the decoder-pipe preview is painted by QML, so the chiaki
    // window must NOT cover the panel (a WS_CHILD HWND always renders over QML —
    // the overlay airspace problem). Park it off-screen as a small visible child:
    // it keeps decoding/exporting (decode-decoupled, occlusion-safe) and feeding the
    // pipe, never floats as a top-level (no taskbar), can't grab the mouse, and the
    // panel stays clear for the QML Image + overlay. Ignore the QML panel rect.
    QRect next = QRect(x, y, width, height).normalized();
    if (qmlRenderMode_) {
        next = QRect(-4000, -4000, 320, 180);
    }
    if (next == chiakiEmbedRect_ && chiakiEmbedStatus_ == QLatin1String("Embedded")) {
        return;
    }
    chiakiEmbedRect_ = next;
    if (chiakiEmbedVisible_) {
        setChiakiEmbedVisible(true);
    }
}

void OrionAppController::restartSidecarWithWindowContainment()
{
    // [ORION_DISCONNECT_AUDIT 2026-09-19] F8, defence in depth. All six call sites are
    // independently gated on "the session is still live" today, so this is not a live
    // bug -- but this function is precisely the one that re-arms chiakiEmbedVisible_
    // and restarts chiakiEmbedWatchdog_ that disconnectRemotePlay() just stopped, and
    // then respawns the sidecar. A restart racing a teardown is the shape of "I pressed
    // Disconnect and it came back". The teardown flag is the authority for "the user is
    // stopping"; honour it here too instead of trusting six callers to keep doing so.
    if (!sidecarRestartAllowedDuringLifecycle(
            applicationShutdownActive(applicationShutdownPhase_),
            remotePlayTeardownActive_)) {
        if (remotePlayTeardownActive_) {
            appendLog(QStringLiteral(
                "Sidecar restart refused: a Remote Play teardown is in progress."));
        }
        return;
    }
    // A restarted detector begins a new frame/timestamp namespace. Invalidate
    // the last HUD sample now; do not wait for the exit callback or a new frame.
    clearMeterMetrics(true);
#ifdef Q_OS_WIN
    if (!isXboxRemotePlay(config_.data())) {
        const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
        embedWatchdogGraceUntilMs_ = extendStreamWindowContainmentGrace(
            embedWatchdogGraceUntilMs_, nowMs);
        chiakiEmbedWatchdog_.setInterval(kStreamWindowWatchdogFastMs);
        // PS5 capture-card mode still launches OrionStream for INPUT. Xbox does
        // not: never start its window-containment watchdog for Microsoft's app.
        chiakiEmbedVisible_ = true;
        setChiakiEmbedVisible(true);
        if (!chiakiEmbedWatchdog_.isActive()) {
            chiakiEmbedWatchdog_.start();
        }
    }
#endif
    {
        // A contained restart replaces the OrionStream pipe server. Invalidate
        // the client generation under the same fence as precise-fire writes so
        // the replacement must accept a fresh seed packet before it owns input.
        QMutexLocker submitLock(&submitMutex_);
        orionInput_.resetConnection();
        directPipeOwnsInput_ = false;
    }
    remotePlay_.restartSidecar();
}

void OrionAppController::setChiakiEmbedVisible(bool visible)
{
    if (isXboxRemotePlay(config_.data()))
        return; // Never reparent/hide Microsoft's client or a browser window.
    chiakiEmbedVisible_ = visible;
    const auto setEmbedStatus = [this](const QString& value) {
        if (chiakiEmbedStatus_ != value) {
            chiakiEmbedStatus_ = value;
            emit statusChanged();
        }
    };

#ifdef Q_OS_WIN
    if (!visible) {
        HWND chiaki = reinterpret_cast<HWND>(chiakiWindowHandle_);
        if (chiaki && IsWindow(chiaki)) {
            ShowWindowAsync(chiaki, SW_HIDE);
        }
        // Forget the applied geometry so the next show re-issues SetWindowPos (with
        // SWP_SHOWWINDOW) instead of the idempotent fast path skipping it as "unchanged".
        lastEmbedAppliedRect_ = QRect();
        setEmbedStatus(QStringLiteral("Hidden"));
        return;
    }

    // CAPTURE-CARD mode: the HDMI card is the video source (rendered in QML from the sidecar preview),
    // so the chiaki window serves NO on-screen purpose — not even the decoder-pipe present. ADOPT it as
    // an OFF-SCREEN, HIDDEN WS_CHILD of the launcher: structurally contained (cannot float as a top-level
    // / no taskbar entry) AND never painted -> no window ever appears at the top of the screen. chiaki
    // still runs + connects for INPUT (this touches only the WINDOW, never the session). Placed BEFORE
    // the qmlRenderMode_ branch so it is authoritative regardless of render mode.
    const bool captureCardMode =
        config_.data().videoSource.compare(QLatin1String("capture_card"), Qt::CaseInsensitive) == 0;
    if (captureCardMode || qmlRenderMode_) {
        // Video is already delivered by HDMI/SHM. Parenting a foreign HWND attaches
        // input queues and couples shutdown/DPI behavior; it is not a video input.
        // Only a verified stream executable is eligible, never a title-only match.
        const HWND candidate = findChiakiWindow();
        if (candidate && IsWindow(candidate)) {
            const bool changed = chiakiWindowHandle_ != reinterpret_cast<quintptr>(candidate);
            chiakiWindowHandle_ = reinterpret_cast<quintptr>(candidate);
            if (captureCardMode) {
                ShowWindowAsync(candidate, SW_HIDE);
            } else if (changed || lastEmbedAppliedRect_ != QRect(-4000, -4000, 320, 180)) {
                SetWindowPos(candidate, nullptr, -4000, -4000, 320, 180,
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_ASYNCWINDOWPOS);
                lastEmbedAppliedRect_ = QRect(-4000, -4000, 320, 180);
            }
            if (changed) {
                ++embedRegrabCount_;
                appendLog(QStringLiteral("Stream window contained asynchronously (no cross-process parenting)."));
            }
        }
        setEmbedStatus(captureCardMode ? QStringLiteral("Capture card (stream window hidden)")
                                     : QStringLiteral("QML render (stream window off-screen)"));
        return;
    }

    const bool wasEmbedded = chiakiEmbedStatus_ == QLatin1String("Embedded");
    bool embedEvicted = false;

    HWND chiaki = reinterpret_cast<HWND>(chiakiWindowHandle_);

    // Fast path: already embedded and the child is still ours — just keep the
    // geometry in sync. Skips the EnumWindows/OpenProcess sweep, which lets the
    // QML 250 ms sync timer and the native watchdog poll aggressively for free.
    if (chiaki && IsWindow(chiaki) && GetParent(chiaki) == mainWindowHandle()
        && chiakiEmbedRect_.width() >= 80 && chiakiEmbedRect_.height() >= 80) {
        // Validate the child really is the stream window before trusting it. An
        // untitled hidden helper window that slipped past the finder would
        // otherwise be locked in as "Embedded" forever while the real stream
        // window floats on the desktop (live-seen). GetWindowTextW on a foreign
        // window reads the cached caption — cheap enough for every tick.
        wchar_t childTitle[256] = {};
        GetWindowTextW(chiaki, childTitle, 255);
        const QString childLower = QString::fromWCharArray(childTitle).trimmed().toLower();
        const bool childLooksReal = childLower.contains(QStringLiteral("chiaki"))
            || childLower.contains(QStringLiteral("orion stream"));
        if (childLooksReal) {
            const qreal fastDpr = mainWindowDevicePixelRatio();
            const QRect target(
                qRound(chiakiEmbedRect_.x() * fastDpr),
                qRound(chiakiEmbedRect_.y() * fastDpr),
                qMax(80, qRound(chiakiEmbedRect_.width() * fastDpr)),
                qMax(80, qRound(chiakiEmbedRect_.height() * fastDpr)));
            // Idempotent: when neither the geometry nor the DPR has changed since the
            // last placement, do NOT re-issue SetWindowPos. The watchdog + QML timer
            // poll this ~10x/sec; re-asserting position/Z-order/show on the Vulkan
            // child every idle tick is what makes the panel flicker and flash black.
            if (target == lastEmbedAppliedRect_ && qFuzzyCompare(fastDpr, lastEmbedAppliedDpr_)) {
                setEmbedStatus(QStringLiteral("Embedded"));
                return;
            }
            // Genuine move/resize: place it, but leave Z-order alone (a child window
            // already renders over the QML surface in its rect — re-asserting HWND_TOP
            // every move is a needless source of churn).
            SetWindowPos(
                chiaki,
                nullptr,
                target.x(), target.y(), target.width(), target.height(),
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            lastEmbedAppliedRect_ = target;
            lastEmbedAppliedDpr_ = fastDpr;
            setEmbedStatus(QStringLiteral("Embedded"));
            return;
        }
        // Bogus child: give it back its original style/parent, hide it, and fall
        // through to a fresh search for the real stream window.
        appendLog(QStringLiteral("Embedded child failed validation (title=\"%1\") - evicting and re-searching for the stream window.")
                      .arg(QString::fromWCharArray(childTitle)));
        if (chiakiOriginalStyle_ != 0) {
            SetWindowLongPtrW(chiaki, GWL_STYLE, chiakiOriginalStyle_);
        }
        if (chiakiOriginalExStyle_ != 0) {
            SetWindowLongPtrW(chiaki, GWL_EXSTYLE, chiakiOriginalExStyle_);
        }
        SetParent(chiaki, reinterpret_cast<HWND>(chiakiOriginalParent_));
        ShowWindow(chiaki, SW_HIDE);
        chiaki = nullptr;
        chiakiWindowHandle_ = 0;
        chiakiOriginalParent_ = 0;
        chiakiOriginalStyle_ = 0;
        chiakiOriginalExStyle_ = 0;
        lastEmbedAppliedRect_ = QRect();
        embedEvicted = true;
    }

    HWND candidate = findChiakiWindow();
    if (candidate && candidate != chiaki) {
        if (chiaki && IsWindow(chiaki) && GetParent(chiaki) == mainWindowHandle()) {
            ShowWindow(chiaki, SW_HIDE);
        }
        chiaki = candidate;
        chiakiWindowHandle_ = reinterpret_cast<quintptr>(chiaki);
        chiakiOriginalParent_ = 0;
        chiakiOriginalStyle_ = 0;
        chiakiOriginalExStyle_ = 0;
        lastEmbedAppliedRect_ = QRect();
    } else if (!chiaki || !IsWindow(chiaki)) {
        chiaki = candidate;
        chiakiWindowHandle_ = reinterpret_cast<quintptr>(chiaki);
        chiakiOriginalParent_ = 0;
        chiakiOriginalStyle_ = 0;
        chiakiOriginalExStyle_ = 0;
        lastEmbedAppliedRect_ = QRect();
    }

    if (!chiaki || !IsWindow(chiaki)) {
        setEmbedStatus(visible ? QStringLiteral("Chiaki window not found") : QStringLiteral("Hidden"));
        return;
    }

    if (!visible || chiakiEmbedRect_.width() < 80 || chiakiEmbedRect_.height() < 80) {
        ShowWindow(chiaki, SW_HIDE);
        lastEmbedAppliedRect_ = QRect();
        setEmbedStatus(QStringLiteral("Hidden"));
        return;
    }

    HWND parent = mainWindowHandle();
    if (!parent || !IsWindow(parent)) {
        setEmbedStatus(QStringLiteral("Venice window not ready"));
        return;
    }

    if (GetParent(chiaki) != parent) {
        optimizeChiakiWindowProcess(chiaki);
        // The freshly-found stream window is a visible, framed top-level for the
        // instant before we adopt it (Chiaki shows it itself). Shove it off-screen
        // FIRST — before restyle + reparent — so it never paints on the desktop
        // during the grab. This is what removes the visible "jump out" flash when a
        // recreated stream window is recaptured mid-session.
        SetWindowPos(chiaki, nullptr, -32000, -32000, 0, 0,
                     SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
        chiakiOriginalParent_ = reinterpret_cast<quintptr>(GetParent(chiaki));
        chiakiOriginalStyle_ = GetWindowLongPtrW(chiaki, GWL_STYLE);
        chiakiOriginalExStyle_ = GetWindowLongPtrW(chiaki, GWL_EXSTYLE);
        LONG_PTR style = chiakiOriginalStyle_;
        style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU);
        style |= (WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN);
        LONG_PTR exStyle = chiakiOriginalExStyle_;
        exStyle &= ~(WS_EX_APPWINDOW | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE | WS_EX_DLGMODALFRAME);
        exStyle |= WS_EX_TOOLWINDOW;
        SetWindowLongPtrW(chiaki, GWL_STYLE, style);
        SetWindowLongPtrW(chiaki, GWL_EXSTYLE, exStyle);
        // Reparent BEFORE making the window visible so it never flashes as a
        // framed top-level window on the desktop/taskbar.
        SetParent(chiaki, parent);
        if (GetParent(chiaki) != parent) {
            setEmbedStatus(QStringLiteral("Embed failed"));
            return;
        }
        ShowWindow(chiaki, SW_RESTORE);
        wchar_t embeddedTitle[256] = {};
        GetWindowTextW(chiaki, embeddedTitle, 255);
        appendLog(QStringLiteral("Stream window embedded: hwnd=0x%1 title=\"%2\"")
                      .arg(QString::number(reinterpret_cast<quintptr>(chiaki), 16),
                           QString::fromWCharArray(embeddedTitle)));
        // Machine-parseable re-grab counter so a batch's orion_native.log shows how
        // often the stream window was recaptured (pop-out events). Add-only line; no
        // spaces inside values. reason: firstshow | recreated (stream churn) | evicted
        // (a bogus child was replaced by the real window).
        ++embedRegrabCount_;
        const QString regrabReason = embedEvicted
            ? QStringLiteral("evicted")
            : (wasEmbedded ? QStringLiteral("recreated") : QStringLiteral("firstshow"));
        QString regrabTitle = QString::fromWCharArray(embeddedTitle).trimmed();
        regrabTitle.replace(QLatin1Char(' '), QLatin1Char('_'));
        if (regrabTitle.isEmpty()) {
            regrabTitle = QStringLiteral("none");
        }
        appendLog(QStringLiteral("Embed regrab: seq=%1 reason=%2 title=%3")
                      .arg(embedRegrabCount_)
                      .arg(regrabReason, regrabTitle));
    }

    // QML supplies logical coordinates; scale to physical pixels for SetWindowPos
    // so the embedded stream lands exactly over the capture panel on high-DPI.
    const qreal dpr = mainWindowDevicePixelRatio();
    const QRect placed(
        qRound(chiakiEmbedRect_.x() * dpr),
        qRound(chiakiEmbedRect_.y() * dpr),
        qMax(80, qRound(chiakiEmbedRect_.width() * dpr)),
        qMax(80, qRound(chiakiEmbedRect_.height() * dpr)));
    SetWindowPos(
        chiaki,
        HWND_TOP,
        placed.x(), placed.y(), placed.width(), placed.height(),
        SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW);
    ShowWindow(chiaki, SW_SHOW);
    RedrawWindow(chiaki, nullptr, nullptr, RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN);
    // Record what we placed so the idempotent fast path skips the no-op re-issues
    // that the ~10x/sec pollers would otherwise make.
    lastEmbedAppliedRect_ = placed;
    lastEmbedAppliedDpr_ = dpr;
    setEmbedStatus(QStringLiteral("Embedded"));
#else
    Q_UNUSED(visible);
    setEmbedStatus(QStringLiteral("Unsupported"));
#endif
}

void OrionAppController::openPsnLogin()
{
    backendMessage_ = QStringLiteral("OAuth login is no longer handled by Venice. Use Remote Play registration instead.");
    appendLog(backendMessage_);
    emit statusChanged();
}

void OrionAppController::saveProfileFromRedirect()
{
    backendMessage_ = QStringLiteral("Remote Play profiles are managed by Chiaki now. Open Chiaki to register the console.");
    appendLog(backendMessage_);
    emit statusChanged();
}

void OrionAppController::refreshProfiles()
{
    backendMessage_ = QStringLiteral("The Remote Play client owns console profiles. Venice consumes its stream and automation telemetry.");
    appendLog(backendMessage_);
    emit statusChanged();
}

void OrionAppController::registerPin()
{
    backendMessage_ = QStringLiteral("PIN registration is handled in the Remote Play client. Register the console, then connect from Venice.");
    appendLog(backendMessage_);
    emit statusChanged();
}

void OrionAppController::startCapturePreview()
{
    // Live capture-card preview at launch: show the Elgato HDMI feed in the dashboard BEFORE the
    // user presses Connect, WITHOUT starting Chiaki / Remote Play or the input hook. Only the
    // capture-card source supports this (the decoder pipe needs Chiaki). Heavily guarded so it is a
    // safe no-op when a session is already up, during teardown, in safe mode, or before auth — and
    // so a redundant call (dashboard reload + post-disconnect resume) never double-opens the card.
    if (applicationShutdownActive(applicationShutdownPhase_)
        || !authenticated_ || safeModeActive_) {
        return;
    }
    if (!isCaptureCardSource(config_.data())) {
        return;   // decoder-pipe source: no pre-connect preview is possible (needs Chiaki)
    }
    const RemotePlayState st = remotePlay_.state();
    if (st == RemotePlayState::Connecting || st == RemotePlayState::Running) {
        return;
    }
    if (remoteState_ == QLatin1String("Disconnecting") || capturePreviewActive_) {
        return;
    }
    appendLog(QStringLiteral("Live capture preview: opening capture card (no Chiaki, no input)."));

    // Clear any stale frame from a previous session so the panel doesn't show
    // a frozen frame while the new sidecar opens the capture card.
    clearRemotePreviewFrame();

    remotePlay_.startCapturePreview();
}

void OrionAppController::connectRemotePlay()
{
    if (remotePlayTeardownActive_) {
        appendLog(QStringLiteral("Connect waiting: previous session cleanup is still finishing."));
        return;
    }
    if (applicationShutdownActive(applicationShutdownPhase_)) {
        return;
    }
    // Re-entrancy guard: ignore repeat clicks while a session is already coming up or live.
    // This is the primary defence against spawning a second Remote Play / OrionStream instance
    // (the QML button is also disabled during the transition, but the engine must not rely on it).
    const RemotePlayState liveState = remotePlay_.state();
    if (liveState == RemotePlayState::Connecting || liveState == RemotePlayState::Running) {
        appendLog(QStringLiteral("Connect ignored — a stream session is already %1.")
                      .arg(stateText(liveState)));
        return;
    }

    // [ORION_CONNECT_LATENCY 2026-09-19] The click itself, stamped. Until now the
    // first line any connect produced was "Chiaki Remote Play settings saved." from
    // inside this handler, so "click -> handler entry" was unmeasurable and every
    // earlier stage had to be inferred by differencing unrelated lines. This is the
    // t=0 every later "Connect stage:" line is relative to.
    remotePlay_.beginConnectStopwatch();
    appendLog(QStringLiteral("Connect pressed."));

    // [ORION_INPUT_DEAD_UX] From this accepted Connect until a user Disconnect, the player has
    // ASKED for a session: a dead input state under live video is now a stranding, not a browse.
    // A fresh MANUAL Connect also grants a fresh bounded retry budget; the retry machinery's own
    // reconnects (inputRetryInFlightReconnect_) must not — that would unbound the loop.
    inputSessionIntentActive_ = true;
    if (!inputRetryInFlightReconnect_) {
        inputRetryPlanner_.reset();
        inputRetryPendingAttempt_ = 0;
        inputRetryGaveUp_ = false;
    }

    // Invalidate any crash-recovery timer before this accepted Connect can
    // spend time in discovery/controller setup. That timer must not wake later
    // and disconnect the session this call is about to create.
    ++remotePlayLifecycleGeneration_;

    {
        // A user-started session is a new input-process generation even when a
        // capture-card preview sidecar is promoted in place. Retire any stale
        // client handle before creating the virtual route or asking Chiaki to
        // start, so the first Running tick must seed the current pipe server.
        QMutexLocker submitLock(&submitMutex_);
        orionInput_.resetConnection();
        directPipeOwnsInput_ = false;
    }

    botOwnershipArmToken_ = 0;
    botOwnershipStartedMs_ = -1.0;
    botOwnershipEndedMs_ = -1.0;
    clearMeterMetrics(true);

    controllerUiGuardUntilMs_ = 0;
    controllerUiPointerGuardUntilMs_ = 0;
    controllerUiSuppressionLogged_ = false;
    directPipeOwnsInput_ = false;
    shotIntentEdgeTracker_.reset();
    previousControllerUiState_ = ControllerState{};
    squareUpAuditTracker_.reset();
    hookDigitalRestSinceMs_ = -1;
    hookReleaseRepairDueMs_ = -1;
    hookReleaseRepairHavePreviousOutput_ = false;
    hookDownHeartbeats_ = 0;
    hookRecoveryAttempts_ = 0;
    hookFullRestartEscalated_ = false;
    inputRouteAwaitingRecovery_ = false;
    preciseFireDeliveryFault_ = false;
    preciseFireRecoveryNeutralFrames_ = 0;
    syncEngineArmed();

    updateSecurityStatus();
    if (!automationSecurityAllowed()) {
        const QString reason = (leaseGate_.enabled() && !leaseGate_.fireAllowed())
            ? QStringLiteral("%1 (server lease required)").arg(leaseGate_.stateText())
            : securityLockReason_;
        backendMessage_ = QStringLiteral("Security lock active: %1").arg(reason);
        // [CL3-F8-006 2026-09-23] Customer line first; the raw lock reason is engineering-only
        // (rule 0 "engine detail:"), so lease text never reaches the feed through here.
        if (settingsRepairAvailable_) {
            appendLog(ui_notifications::settingsRepairCustomerText());
        } else if (leaseGate_.enabled() && !leaseGate_.fireAllowed()) {
            appendLog(QStringLiteral("Remote Play: reconnecting to Venice servers — shots are paused until your subscription is confirmed. Usually a few seconds."));
        } else {
            appendLog(QStringLiteral("Remote Play: Venice's safety check didn't pass, so Connect is blocked (code ST-03). Restart Venice; if it repeats, reinstall from #downloads."));
        }
        appendLog(QStringLiteral("Security engine detail: Remote Play blocked by security lock: %1").arg(reason));
        emit statusChanged();
        return;
    }

    saveRemoteSettings();

    // Auto-detect the console IP if it's not configured. discoverPs5() is synchronous
    // (~2 s) and persists the result back into config_ via the helperResult handler,
    // so by the time we read the IP below it will reflect whatever was found.
    if (!isXboxRemotePlay(config_.data()) && config_.data().remotePlayConsoleIp.trimmed().isEmpty()) {
        appendLog(QStringLiteral("No console IP set — discovering PS5 before connecting..."));
        remotePlay_.discoverPs5();
        remotePlay_.applyConfig(config_.data());
    }

    // Probe controller state right now so a freshly plugged pad is detected
    // before we decide whether to create the virtual XUSB target.
#ifdef Q_OS_WIN
    pollPhysicalController();
    // [ORION_METER_DELAY 2026-08-07] Allow ViGEm target creation when the pad is
    // enumerated but idle (rawInputPresent_ == true). The old gate blocked
    // Connect on any pad that hadn't sent an input report yet, forcing users to
    // press a button on the pad before Remote Play would come up.
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-07] Tightened: require an ACTUAL recent
    // HID input report (hasRecentRawInput()), not mere enumeration
    // (rawInputPresent_). A dead-but-enumerated pad used to slip past this
    // predicate and hang Remote Play at "connected, but no inputs are landing"
    // -- the classic symptom users mistook for a bot bug. rawInputPresent_ still
    // drives the "press any button" hint text below (correct: pad enumerated but
    // silent), we just no longer LET Connect proceed on that state.
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Same admission predicate, but an
    // ENUMERATED-and-silent pad now gets one wake probe + re-check before the
    // refusal (padConnectGateAction in ControllerRoutingPolicy.h has the full
    // rationale + live-log evidence). The wake never admits by itself — the
    // gate still only passes once a real HID report has landed — so the
    // dead-pad hole above stays closed. The NOT-enumerated refusal keeps its
    // message but now tells the user the one recovery that worked live
    // (re-seat the cable: after system idle the pad can drop off USB entirely,
    // invisible to RawInput AND WinMM, until a re-plug re-arrives it).
    PadConnectGateAction padGate = padConnectGateAction(
        physicalPadLive_, hasRecentRawInput(), rawInputPresent_);
    if (padGate == PadConnectGateAction::WakeProbeThenRecheck
        && wakePhysicalPadAndConfirmReport()) {
        padGate = PadConnectGateAction::Proceed;
    }
    if (padGate != PadConnectGateAction::Proceed) {
        const bool enumerated = padGate == PadConnectGateAction::WakeProbeThenRecheck;
        // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] The absent-pad refusal must
        // tell the truth the registry knows: "never seen" and "was here and
        // dropped off USB" are different situations with different remedies
        // (rig-verified: the drop-off is EnhancedPowerManagementEnabled=1
        // wedging the pad's firmware after idle; only a re-plug revives it,
        // and the consented one-time fix prevents the recurrence).
        refreshControllerUsbPowerScan();
        QString status;
        if (enumerated) {
            status = QStringLiteral("Controller found but not reporting yet — press any button on it, then press Connect.");
        } else {
            switch (absentPadAdvice(sonyUsbHistoryKnown_, controllerUsbPowerFixAvailable_)) {
            case AbsentPadAdvice::DroppedOffBusFixAvailable:
                status = QStringLiteral(
                    "Your controller has been connected to this PC before but is not on the USB bus "
                    "right now. Unplug and re-plug its USB cable, then press Connect. To stop it "
                    "dropping off after the PC sits idle, use the one-time \"Fix controller USB "
                    "power\" button.");
                break;
            case AbsentPadAdvice::DroppedOffBus:
                status = QStringLiteral(
                    "Your controller has been connected to this PC before but is not on the USB bus "
                    "right now. Unplug and re-plug its USB cable (or power-cycle the pad), then "
                    "press Connect.");
                break;
            case AbsentPadAdvice::NeverSeen:
                status = QStringLiteral(
                    "No controller detected. Plug your DualSense into the PC (USB or Bluetooth) "
                    "before starting Remote Play.");
                break;
            }
        }
        setControllerLifecycle(
            enumerated ? ControllerLifecycleState::PhysicalPresentNoLive : ControllerLifecycleState::NoPhysical,
            status);
        backendMessage_ = status;
        // Self-diagnosing refusal: presence + report age answer "which of the
        // two refusable states was this" without a debugger on the next report.
        appendLog(QStringLiteral(
                      "Remote Play blocked: %1 (rawPresent=%2 lastReportAgeMs=%3)")
                      .arg(status)
                      .arg(rawInputPresent_ ? 1 : 0)
                      .arg(lastRawInputMs_ > 0
                               ? QString::number(QDateTime::currentMSecsSinceEpoch() - lastRawInputMs_)
                               : QStringLiteral("-")));
        emit statusChanged();
        return;
    }
#endif

    if (!controller_.isConnected()) {
        connectVirtualController();
        if (!controller_.isConnected()) {
            backendMessage_ = QStringLiteral("Virtual controller is required before Remote Play starts.");
            appendLog(backendMessage_);
            emit statusChanged();
            return;
        }
    }
    appendLog(QStringLiteral("Controller route: %1 -> %2 -> %3")
                  .arg(controllerDeviceDetail_.isEmpty() ? controllerInputSource_ : controllerDeviceDetail_,
                       controller_.backendName(), isXboxRemotePlay(config_.data())
                           ? QStringLiteral("Xbox app (external)") : QStringLiteral("Chiaki")));

    // Make sure the bandwidth preset is applied right before the stream starts so
    // chiaki's internal session reads the optimised resolution / codec / buffer sizes.
    remotePlay_.applyBandwidthMode(bandwidthModeFromString(config_.data().streamBandwidthMode));
    remotePlay_.setAudioMode(config_.data().streamAudioMode);
    setCaptureSourceHealth(QStringLiteral("waiting_for_first_frame"));
    // [ORION_CONNECT_LATENCY 2026-09-19] ORDER, not content. setChiakiEmbedVisible()
    // used to run HERE, in front of start(), and it is the single most expensive
    // thing on the native half of the connect: measured 152 ms median (min 146,
    // n=11, logs 09-18/09-19) between "Streaming preset: ..." and "Capture-card
    // input window contained ...". It is Win32 window surgery on ANOTHER process's
    // window — findChiakiWindow() sweeps up to 8 times and SetParent() blocks on
    // the stream client's message loop — and NOTHING in it is an input to the
    // console handshake. Meanwhile start() only has to write the promotion command
    // to the live sidecar's stdin (measured 70 ms to "Stream promotion started"),
    // after which the sidecar spends ~1073 ms on standby claim + handshake.
    //
    // Asking the console first and containing the window second overlaps that
    // ~175 ms of local work with the ~1073 ms already in flight. Nothing regresses
    // on containment: chiakiEmbedWatchdog_ starts immediately below and re-runs the
    // exact same containment every tick for the whole session (35 s of fast ticks),
    // and the deferred call still happens on this same event-loop turn, before any
    // sidecar reply can be processed.
    remotePlay_.start();
    setChiakiEmbedVisible(true);

    // Embed the Chiaki stream the instant its window appears so it never lingers
    // as a separate top-level window — and keep watching for the entire session
    // (the old one-shot 6 s poll missed windows that appeared after its deadline
    // and never recovered a stream-process restart).
    const qint64 connectNowMs = QDateTime::currentMSecsSinceEpoch();
    embedWatchdogGraceUntilMs_ = connectNowMs + 35000;  // chiaki cold start can surface the window at ~28-30s; embed it even then so it never floats
    chiakiEmbedWatchdog_.setInterval(kStreamWindowWatchdogFastMs);
    // Startup grace: chiaki commonly emits ~20-30 s of black/stalled frames while it
    // establishes (first keyframe / surface warm-up). A sidecar exit inside this window is a
    // warm-up hiccup, not a real crash — auto-restart quietly so the user never has to manually
    // reconnect and never gets dumped into safe mode just for connecting.
    // NOTE: these two windows were briefly shortened to 15s/30s on the premise that chiaki is
    // pre-launched during preview. That premise is FALSE — sidecarShouldAutoLaunchClient() does
    // NOT launch chiaki in preview mode, so chiaki still cold-starts at Connect. A 15s embed grace
    // expires BEFORE the window appears, leaving the chiaki window floating un-adopted on the
    // desktop; a 30s startup grace is shorter than the 35s connect-stall timeout, so a genuine
    // warm-up exit gets misread as a crash.
    streamStartupGraceUntilMs_ = connectNowMs + 75000;  // > the 35s connect-stall timeout + 30s spacing so a genuine-fail retry still fits
    streamStartupRestarts_ = 0;
    streamConnectMs_ = connectNowMs;
    // Batch-tuning breadcrumb: record the active shot-tuning knobs once per connect so a live
    // batch log self-documents what was in effect when analysing greens/misses (key=value, no
    // spaces in values, per the telemetry log-shape convention).
    appendLog(QStringLiteral("ShotTuning meterColor=%1 autoColor=%2 earlyLateMs=%3 noDip=%4 noDipLeadMs=%5 remap=%6")
                  .arg(config_.data().meterColor)
                  .arg(config_.data().autoMeterColor ? 1 : 0)
                  .arg(config_.data().earlyLateOffsetMs, 0, 'f', 0)
                  .arg(config_.data().noDipEnabled ? 1 : 0)
                  .arg(config_.data().noDipLeadMs, 0, 'f', 0)
                  .arg(config_.data().tempoRemapType));
    if (!isXboxRemotePlay(config_.data()))
        chiakiEmbedWatchdog_.start();
}

// ─── [ORION_INPUT_DEAD_UX 2026-08-30] input-session auto-retry + dead-input overlay ────────────
// Closes the owner-unacceptable trap measured across 08-28..08-30: 31 of 185 Square-edge epochs
// landed while the input session was Disconnected (worst window 2m18s, 24 presses) — the HDMI
// video kept playing, the fail-closed write gate correctly refused every press, and NOTHING told
// the player or retried. The write gate itself is untouched; this only retries through the same
// user-facing Connect path and says the truth on screen. See InputSessionRetryPolicy.h for the
// bounded state machine and the no-double-spawn / no-fake-readiness argument.

namespace {
// Kill switch for the RETRY only. The overlay is deliberately not killable: visibility of dead
// input must never be optional.
[[nodiscard]] bool inputSessionAutoRetryEnabled()
{
    static const bool enabled =
        qgetenv("ORION_INPUT_SESSION_AUTORETRY") != QByteArrayLiteral("0");
    return enabled;
}
} // namespace

void OrionAppController::refreshInputDeliveryState()
{
    const RemotePlayState sessionState = remotePlay_.state();
    // "Video looks alive": either the pre/post-failure capture-card preview is feeding the
    // panel, or a session (Connecting/Running) owns it. Without live pixels there is no
    // illusion to break — the page's ordinary Disconnected/Error surface owns that case.
    const bool videoAlive = capturePreviewActive_ || remoteRunning_;
    const bool pipeEnabled = orionInput_.enabled();
    const bool pipeConnected = orionInput_.connected();
    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    // Running-edge debounce (OVERLAY only — the per-press log line stays instantaneous):
    // the pipe seeds a beat after Running, so require the down state to persist for the
    // grace, or to be proven by an actual undeliverable press inside it.
    const bool runningPipeDown = sessionState == RemotePlayState::Running
        && pressUndeliverable(sessionState, pipeEnabled, pipeConnected);
    if (runningPipeDown) {
        if (runningPipeDownSinceMs_ == 0) {
            runningPipeDownSinceMs_ = nowMs;
        }
    } else {
        runningPipeDownSinceMs_ = 0;
    }
    const bool suppressRunningEdgeFlash = runningPipeDown
        && !runningPipeDownConfirmed(nowMs, runningPipeDownSinceMs_,
                                     lastUndeliverablePressMs_);
    const InputDeadSeverity severity = suppressRunningEdgeFlash
        ? InputDeadSeverity::None
        : inputDeadOverlaySeverity(
              sessionState, pipeEnabled, pipeConnected,
              videoAlive, inputSessionIntentActive_,
              nowMs, lastUndeliverablePressMs_);

    QString headline;
    QString detail;
    if (severity == InputDeadSeverity::Notice) {
        // Connecting: deliberately-neutral routes, verdict pending. Honest, calm.
        // [COPY-FIX 2026-09-23 CW-13/EA-21] Sentence case, plain words.
        headline = QStringLiteral("Connecting — buttons aren't reaching your PS5 yet");
        // remoteStatus_ is already customer copy (ui_notifications::customerRemoteStatus).
        detail = remoteStatus_;
    } else if (severity == InputDeadSeverity::Critical) {
        headline = QStringLiteral("Controller input isn't reaching your PS5");
        if (sessionState == RemotePlayState::Running) {
            // Running with the direct pipe down: the heartbeat watchdog owns this recovery
            // (input-only repair, then one contained sidecar restart).
            // [COPY-FIX 2026-09-23 EA-22] The cause is the Remote Play link, not the USB pad.
            detail = QStringLiteral(
                "Connection to the PS5 dropped — reconnecting now. Presses won't reach the "
                "game until it's back.");
        } else if (!inputSessionIntentActive_) {
            // Press latch outside a session: the player pressed a shot button into the
            // passive preview — they believe they are connected.
            // [COPY-FIX 2026-09-23 EA-23] The button is "Connect" (RemotePlayPage.qml).
            detail = QStringLiteral(
                "This is the HDMI preview only — Venice isn't connected yet. Press Connect.");
        } else if (inputRetryPendingAttempt_ > 0) {
            detail = QStringLiteral("Reconnecting automatically — attempt %1 of %2.")
                         .arg(inputRetryPendingAttempt_)
                         .arg(InputSessionRetryPlanner::kMaxAttempts);
        } else if (inputRetryPlanner_.identityBlocked()) {
            // [COPY-FIX 2026-09-23 NEW-A18] remoteStatus_ is customer copy; the raw engine
            // status is already in the log ("Remote Play engine detail:").
            detail = remoteStatus_;
        } else if (inputRetryGaveUp_) {
            // [COPY-FIX 2026-09-23 NEW-A5] No raw "(%1)" suffix; the detail is in the log.
            detail = QStringLiteral(
                "Venice couldn't reconnect your controller (code RP-07). Press Connect.");
        } else {
            detail = QStringLiteral("Press Connect to restore the session.");
        }
    }

    if (severity == inputDeadSeverity_
        && headline == inputDeadHeadline_
        && detail == inputDeadDetail_) {
        return;
    }
    const bool criticalEdge = (severity == InputDeadSeverity::Critical)
        != (inputDeadSeverity_ == InputDeadSeverity::Critical);
    inputDeadSeverity_ = severity;
    inputDeadHeadline_ = headline;
    inputDeadDetail_ = detail;
    emit inputDeliveryStateChanged();
    if (criticalEdge) {
        // Out-of-app affordance for a player not looking at the app: while the dead state is
        // terminal, the physical pad's lightbar goes warning-red through the SAME opt-in write
        // path Defense Mode uses (see applyControllerLightbar — inert when the user's lightbar
        // feature is off, guarded against writes mid-shot). No audio device dependency exists in
        // this codebase and none is added.
        applyControllerLightbar(true);
    }
}

void OrionAppController::handleInputSessionFailure(int failureClass, bool wakeObserved)
{
    const auto cls = static_cast<InputSessionFailureClass>(failureClass);
    inputRetryPendingAttempt_ = 0;
    if (!inputSessionIntentActive_
        || applicationShutdownActive(applicationShutdownPhase_)
        || remotePlayTeardownActive_
        || safeModeActive_) {
        // No player intent (or a state that owns its own recovery/teardown): report only.
        refreshInputDeliveryState();
        return;
    }
    if (!inputSessionAutoRetryEnabled()) {
        if (!inputRetryGaveUp_) {
            inputRetryGaveUp_ = true;
            appendLog(QStringLiteral(
                "Input-session AUTO-RETRY disabled (ORION_INPUT_SESSION_AUTORETRY=0) — the "
                "overlay will direct the player to press Connect."));
        }
        refreshInputDeliveryState();
        return;
    }
    const InputSessionRetryDecision decision =
        inputRetryPlanner_.onFailure(cls, wakeObserved);
    if (!decision.retry) {
        if (!inputRetryGaveUp_) {
            inputRetryGaveUp_ = true;
            appendLog(QStringLiteral(
                "Input-session AUTO-RETRY exhausted after %1 attempts (class=%2) — giving up "
                "cleanly; the overlay now says to press Connect.")
                          .arg(inputRetryPlanner_.attemptsUsed())
                          .arg(failureClass));
            if (userLog_.enabled()) {
                userLog_.append(QStringLiteral(
                    "Automatic reconnect could not restore the console session — press "
                    "Connect when you are ready."));
            }
        }
        refreshInputDeliveryState();
        return;
    }
    scheduleInputSessionRetry(decision, cls);
}

void OrionAppController::scheduleInputSessionRetry(
    const InputSessionRetryDecision& decision, InputSessionFailureClass cls)
{
    inputRetryPendingAttempt_ = decision.attempt;
    inputRetryGaveUp_ = false;
    const quint64 generation = remotePlayLifecycleGeneration_;
    const bool cold = decision.coldRestart;
    const int attempt = decision.attempt;
    appendLog(QStringLiteral(
        "Input-session AUTO-RETRY scheduled: attempt=%1/%2 delay=%3ms cold=%4 class=%5 — a "
        "failed input session must never sit silent under live video.")
                  .arg(attempt)
                  .arg(InputSessionRetryPlanner::kMaxAttempts)
                  .arg(decision.delayMs)
                  .arg(cold ? 1 : 0)
                  .arg(static_cast<int>(cls)));
    if (userLog_.enabled()) {
        userLog_.append(QStringLiteral(
            "Connection problem — reconnecting automatically (attempt %1 of %2).")
                            .arg(attempt)
                            .arg(InputSessionRetryPlanner::kMaxAttempts));
    }
    QTimer::singleShot(static_cast<int>(decision.delayMs), this,
                       [this, generation, cold, attempt]() {
        fireScheduledInputSessionRetry(generation, cold, attempt);
    });
    refreshInputDeliveryState();
}

void OrionAppController::fireScheduledInputSessionRetry(
    quint64 generation, bool coldRestart, int attempt)
{
    if (inputRetryPendingAttempt_ != attempt) {
        return;   // superseded by a manual action or a newer schedule
    }
    inputRetryPendingAttempt_ = 0;
    if (generation != remotePlayLifecycleGeneration_
        || !inputSessionIntentActive_
        || safeModeActive_
        || remotePlayTeardownActive_
        || applicationShutdownActive(applicationShutdownPhase_)) {
        // A manual Connect/Disconnect advanced the lifecycle, intent was withdrawn, or a
        // state that owns its own recovery latched. Stand down silently.
        refreshInputDeliveryState();
        return;
    }
    const RemotePlayState liveState = remotePlay_.state();
    if (liveState == RemotePlayState::Connecting || liveState == RemotePlayState::Running) {
        return;   // something else already brought a session up — never race it
    }
    appendLog(QStringLiteral("Input-session AUTO-RETRY firing: attempt=%1/%2 cold=%3")
                  .arg(attempt)
                  .arg(InputSessionRetryPlanner::kMaxAttempts)
                  .arg(coldRestart ? 1 : 0));
    inputRetryInFlightReconnect_ = true;
    if (coldRestart) {
        // No-verdict failure classes only: the sidecar may be wedged inside the failed
        // promotion, and a warm start_stream would QUEUE behind that wedge in its serialized
        // stdin loop — executing later, it could raise an input client the native already
        // condemned (an orphan holding the pipe). Synchronous teardown kills the sidecar
        // process (its job object reaps children) so the reconnect starts from nothing.
        disconnectRemotePlay(true);
    }
    connectRemotePlay();
    inputRetryInFlightReconnect_ = false;
    const RemotePlayState afterState = remotePlay_.state();
    if (afterState != RemotePlayState::Connecting && afterState != RemotePlayState::Running) {
        // connectRemotePlay()/start() refused locally (pad gate, security lock, missing
        // IP/exe). Count it against the SAME bounded budget so a refusal can never spin.
        appendLog(QStringLiteral(
            "Input-session AUTO-RETRY attempt %1 refused before a session started (%2).")
                      .arg(attempt)
                      .arg(backendMessage_.isEmpty() ? remoteStatus_ : backendMessage_));
        handleInputSessionFailure(
            static_cast<int>(InputSessionFailureClass::LocalRefusal), false);
        return;
    }
    refreshInputDeliveryState();
}

void OrionAppController::disconnectRemotePlay(bool synchronous)
{
    // Exactly one caller owns process/pad destruction. A synchronous shutdown
    // may upgrade a user-click teardown still queued on the event loop; that
    // callback later observes the cleared flags and becomes a no-op.
    if (remotePlayTeardownActive_) {
        if (synchronous) {
            remotePlayTeardownSynchronous_ = true;
            remotePlayTeardownDeferred_ = false;
            finishRemotePlayTeardown();
        }
        return;
    }
    // A manual or internal teardown supersedes every delayed reconnect that
    // was scheduled for the prior session generation.
    ++remotePlayLifecycleGeneration_;
    // [ORION_INPUT_DEAD_UX] A user/system teardown withdraws session intent and cancels the
    // retry episode; the retry machinery's own cold-restart teardown keeps both (it is about
    // to reconnect this exact intent).
    if (!inputRetryInFlightReconnect_) {
        inputSessionIntentActive_ = false;
        inputRetryPlanner_.reset();
        inputRetryPendingAttempt_ = 0;
        inputRetryGaveUp_ = false;
        // The press latch exists to catch a player who believes they are connected;
        // a deliberate Disconnect IS the player acting on the truth.
        lastUndeliverablePressMs_ = 0;
        refreshInputDeliveryState();
    }
    remotePlayTeardownActive_ = true;
    remotePlayTeardownDeferred_ = !synchronous;
    remotePlayTeardownStopRequested_ = false;
    remotePlayTeardownSynchronous_ = synchronous;

    chiakiEmbedWatchdog_.stop();
    remoteRunning_ = false;
    shotIntentEdgeTracker_.reset();
    previousControllerUiState_ = ControllerState{};
    squareUpAuditTracker_.reset();
    hookDigitalRestSinceMs_ = -1;
    hookReleaseRepairDueMs_ = -1;
    hookReleaseRepairHavePreviousOutput_ = false;
    hookDownHeartbeats_ = 0;
    hookRecoveryAttempts_ = 0;
    hookFullRestartEscalated_ = false;
    inputRouteAwaitingRecovery_ = false;
    preciseFireDeliveryFault_ = false;
    preciseFireRecoveryNeutralFrames_ = 0;
    controllerUiGuardUntilMs_ = 0;
    controllerUiPointerGuardUntilMs_ = 0;
    controllerUiSuppressionLogged_ = false;
    directPipeOwnsInput_ = false;
    remoteState_ = QStringLiteral("Disconnecting");
    remoteStatus_ = QStringLiteral("Disconnecting…");
    chiakiEmbedStatus_ = QStringLiteral("Hidden");
    setCaptureSourceHealth(QStringLiteral("capture_source_lost"));
    backendMessage_ = QStringLiteral("Disconnecting…");
    // [ViGEm RACE] Disarm the precise fire thread BEFORE anything tears the pad down.
    // automation_.reset() below clears the ENGINE's scheduled fire, but the fire thread has
    // already latched its own copy of the deadline + release output, and the only thing that
    // disarms it is the schedDeadlineMs<0 reconciliation on a later GUI tick in
    // pollPhysicalController() — which cannot run, because it is blocked behind this same
    // synchronous teardown. Without this, a release armed a few ms before the click submits into
    // a pad that finishRemotePlayTeardown() is concurrently neutraling and unplugging.
    // [ORION_DISCONNECT_AUDIT 2026-09-19] F4: this teardown was the ONLY one that did
    // not revoke the engine's armed gate. disarmPreciseFire() drains the worker's
    // mailbox and cancels the armed token; it does NOT clear automation_'s armed_ bool,
    // and there is no syncEngineArmed() anywhere below. Every sibling teardown does
    // both -- releaseFailedRemoteInputRoute, tripWatchdog, the sidecarExited handler
    // and prepareForApplicationExit all call automation_.setArmed(false) first.
    //
    // It matters on the DEFERRED path (the user's own Disconnect click, synchronous ==
    // false): remotePlay_.stop() is one singleShot(0) away, so for one event-loop turn
    // the 4 ms input poll still runs against state_ == Running, orionInput_ still
    // connected and ViGEm still plugged -- directInputWriteAllowed() is satisfied and a
    // Square held at the instant of the click can START A FRESH OWNED SHOT after the
    // user asked to disconnect. Bounded to one tick, but it is exactly the "pad still
    // owned after teardown began" class. Revoke the gate first, like everyone else.
    automation_.setArmed(false);
    disarmPreciseFire();
    automation_.reset();
    // Neutral both output routes now, before graceful sidecar shutdown can
    // spend seconds draining. The virtual target is unplugged below.
    neutralizeOwnedInput();
    unplugRemoteController(); // release local input immediately, before asynchronous process cleanup
    if (pendingSubmitSeq_ >= 0) {
        automation_.cancelPostReleaseGrade(pendingSubmitSeq_);
        const QString notice = userReleaseTracker_.fail(
            pendingSubmitSeq_, UserReleaseFailureReason::RemotePlayDisconnected);
        appendCustomerEvent(notice);
        pendingSubmitSeq_ = -1;
    }
    releaseMarkerDeliveryGate_.reset();
    pendingSubmitPhysicalShotEpoch_ = 0;
    pendingSubmitShotAttempt_ = 0;
    pendingSubmitScheduleToken_ = 0;
    pendingSubmitRouteGeneration_ = 0;
    pendingSubmitRoute_ = LatencyControllerRoute::None;
    pendingSubmitDeliveryStage_ = PreciseFireDeliveryStage::None;
    pendingSubmitFireToken_ = 0;
    pendingSubmitTransportSeq_ = 0;
    pendingSubmitSnapshot_ = {};
    pendingSubmitFrameGrid_ = {};
    confirmedPreciseFireStage_ = PreciseFireDeliveryStage::None;
    confirmedPreciseFireToken_ = 0;
    confirmedPreciseFireTransportSeq_ = 0;
    confirmedPreciseFireSnapshot_ = {};
    meterBoxRing_.clear();
    meterBoxCapture_ = {};
    meterOverlayTracker_.reset();
    meterOverlayContinuityLease_.reset();
    meterBox_ = {};
    meterRejectedBox_ = {};
    meterOverlayComputedBox_ = {};
    meterOverlayComputedRejectedBox_ = {};
    remoteFrameOverlaySnapshots_.clear();
    meterConfirmed_ = false;
    meterBoxCaptureSize_ = {};
    lastRealMeterSeenMs_ = 0;
    lastMeterOverlayVisualSeenMs_ = 0;
    userMeterVisible_ = false;
    botOwnershipArmToken_ = 0;
    botOwnershipStartedMs_ = -1.0;
    botOwnershipEndedMs_ = -1.0;
    clearMeterMetrics(true);
    emit meterBoxChanged();
    shot_ = automation_.context();
    shotState_ = holdStateText(shot_.state);
    // The 4 ms poll that normally refreshes the HUD is stopping with the stream,
    // so drop the snapshot here. Otherwise the last live shot's numbers would
    // stay readable on a dead session.
    refreshLiveMeterTelemetry();
    meterRuntimeState_ = QStringLiteral("Searching");
    lastResult_ = QStringLiteral("-");
    outputState_ = QStringLiteral("No Output");
    emit statusChanged();

    // Clear any cached preview frame before process teardown. This makes the
    // capture panel visually stop immediately even if Windows takes a moment to
    // destroy Chiaki's audio/video resources.
    clearRemotePreviewFrame();

    setChiakiEmbedVisible(false);

    // Local input is already released. The user path retires the owned process via
    // exit signals and a deadline timer; only explicit internal callers may wait.
    if (synchronous) {
        finishRemotePlayTeardown();
    } else {
        QTimer::singleShot(0, this, [this]() {
            if (!remotePlayTeardownActive_ || !remotePlayTeardownDeferred_) {
                return;
            }
            remotePlayTeardownDeferred_ = false;
            finishRemotePlayTeardown();
        });
    }
}

void OrionAppController::unplugRemoteController()
{
    // CRITICAL: tear down the virtual pad on disconnect. While it remains plugged,
    // Windows GameInput / Xbox app keeps reading its stick state — if the physical
    // pad has any drift the cursor wanders on the desktop and keyboard shortcuts
    // get spammed when the user tries to alt-tab away. Submitting a neutral state
    // first guarantees no last-frame ghost input survives.
    //
    // [ViGEm RACE] Held under submitMutex_: VirtualController has no internal lock, and
    // disconnectController() FREES the ViGEm target. Doing this unlocked could pull the pad out
    // from under the precise fire thread while it is inside controller_.submit() (use-after-free),
    // or interleave the neutral with a release report. The fire thread was additionally disarmed
    // in disconnectRemotePlay() before this teardown began, so nothing should be in flight — this
    // lock closes the remaining window.
    bool virtualPadUnplugged = false;
    {
        // Lock scope kept tight: only the pad mutations. The status/log work below emits
        // statusChanged() into QML, which must not run while the fire thread is blocked.
        QMutexLocker submitLock(&submitMutex_);
        // The stopped sidecar owned this pipe generation. Clear both the OS
        // handle and de-dup snapshot before a later Connect can reuse them.
        orionInput_.resetConnection();
        if (controller_.isConnected()) {
            const int retiringSlot = controller_.xinputUserIndex() >= 0
                ? controller_.xinputUserIndex()
                : inferredVirtualXinputSlot_;
            if (retiringSlot >= 0) {
                retiredVirtualXinputSlot_ = retiringSlot;
                retiredVirtualXinputSlotUntilMs_ = QDateTime::currentMSecsSinceEpoch()
                    + kRetiredVirtualSlotQuarantineMs;
            }
            ControllerState neutral;
            controller_.submit(neutral);
            controller_.disconnectController();
            inferredVirtualXinputSlot_ = -1;
            virtualPadUnplugged = true;
        }
    }
    if (virtualPadUnplugged) {
        setControllerLifecycle(
            physicalPadLive_ ? ControllerLifecycleState::PhysicalLive : ControllerLifecycleState::NoPhysical,
            physicalPadLive_ ? QStringLiteral("Physical controller live") : QStringLiteral("Disconnected"));
        appendLog(QStringLiteral("Virtual pad unplugged on disconnect (prevents desktop input hijack)."));
    }

}

void OrionAppController::finishRemotePlayTeardown()
{
    if (!remotePlayTeardownActive_) {
        return;
    }
    if (!remotePlayTeardownStopRequested_) {
        remotePlayTeardownStopRequested_ = true;
        remotePlay_.stop();
    }
    if (remotePlayTeardownSynchronous_) {
        // Retained only for application exit and explicit internal recovery callers.
        guiFreezeSuppressUntilMs_.store(gui_freeze::monotonicMs() + 20000,
                                        std::memory_order_relaxed);
        remotePlay_.waitForStopped();
        guiHeartbeatMs_.store(gui_freeze::monotonicMs(), std::memory_order_relaxed);
        guiFreezeSuppressUntilMs_.store(0, std::memory_order_relaxed);
        if (!remotePlayTeardownActive_) return; // completion may have finalized us
    }
    if (remotePlay_.stopping()) return;
    // The retired sidecar's job owns its children. No global process enumeration,
    // taskkill, sleeps or process waits are permitted in the user-button path.
    remoteState_ = QStringLiteral("Disconnected");
    remoteStatus_ = QStringLiteral("Disconnected");
    backendMessage_ = QStringLiteral("Remote Play disconnected.");
    emit statusChanged();

    // Resume the live capture-card preview so the dashboard shows the HDMI feed again after a
    // disconnect (same as before the first Connect). Deferred by the capture-card release beat so
    // the full sidecar's Elgato handle frees first (no double-open); startCapturePreview() re-guards
    // against a quick reconnect, safe mode, or a non-capture-card source.
    if (isCaptureCardSource(config_.data())
        && shouldResumeRuntimeAfterDisconnect(applicationShutdownPhase_)) {
        // Keep the full 3000ms: SidecarWatchdog.h picked it from the 2500-4000ms Elgato handle
        // RELEASE window measured in live forensics, and stopSidecar()'s graceful wait cannot be
        // counted against it — that wait returns as soon as the process exits, so a fast exit
        // (~300ms) would leave only ~1.8s before the reopen and the device would still be held.
        // Reopening early is exactly what produces "device IN USE / no live device".
        const auto generation = remotePlayLifecycleGeneration_;
        QTimer::singleShot(kSidecarRestartDelayMsCaptureCard, this, [this, generation]() {
            if (generation == remotePlayLifecycleGeneration_
                    && !remotePlayTeardownActive_ && !remoteRunning_
                    && !applicationShutdownActive(applicationShutdownPhase_)) startCapturePreview();
        });
    }

    // Normal user teardown has remained event-driven; resume with a current heartbeat.
    guiHeartbeatMs_.store(gui_freeze::monotonicMs(), std::memory_order_relaxed);
    guiFreezeSuppressUntilMs_.store(0, std::memory_order_relaxed);
    remotePlayTeardownDeferred_ = false;
    remotePlayTeardownActive_ = false;
}

void OrionAppController::runLatencyProbes()
{
    // [ORION_PROBE] warmup pump-fake probe run. Preconditions: streaming (the presses must
    // reach the console) and no shot in flight (the engine refuses otherwise). The venue
    // check is the USER's (the button lives in warmup flows): probes are visible pump fakes.
    if (!remoteRunning()) {
        appendLog(QStringLiteral("LatencyProbes: not streaming — start Remote Play first"));
        return;
    }
    // startLatencyProbes() returns SILENTLY when the engine is mid-shot or a run is already
    // in flight. Silence on a user-pressed button reads as a broken button, and the user then
    // presses it again mid-run -- which is also a no-op, compounding the confusion. Say why.
    if (automation_.latencyProbesActive()) {
        appendLog(QStringLiteral("LatencyProbes: a run is already in flight — let it finish"));
        return;
    }
    if (!automation_.armed()) {
        appendLog(QStringLiteral(
            "LatencyProbes: controller route is disarmed — enable Bot + Controller first"));
        return;
    }
    // Configurable via settings `latency_probe_count` (default 16 = the previous hardcode; see
    // AppConfigData::latencyProbeCount for the convergence arithmetic — a 16-run straddles the
    // 3.3ms readiness gate on the measured probe-raw robust_sd of 10.6-13.9ms, which is why the
    // owner needed TWO manual runs on 2026-08-06; 24 converges in one run with margin). The
    // historical floor still holds: the tick-phase fit needs >=6 CLOSED probes
    // (latency_estimator _record_probe_pair returns early below 6), and only counts above 16
    // slide the fixed-size 16-entry _probe_pairs/_probe_raw_run windows.
    automation_.startLatencyProbes(automation_.config().latencyProbeCount);
}

QString OrionAppController::latencyProbeSummary() const
{
    // [ORION_PROBE] The point of this readout is to make "never measured" VISIBLE. Both
    // consumers of a probe run degrade silently to a factory prior, so a user (or I) can
    // otherwise spend a session tuning against a number nothing ever measured.
    if (automation_.latencyProbesActive()) {
        return QStringLiteral("Running — hold still, 8 pump fakes over ~20s.");
    }

    const int n = automation_.measuredLatencyN();
    const double sd = automation_.measuredLatencySdMs();
    const double conf = automation_.tickPhaseConf();

    QString out;
    if (n <= 0 || sd <= 0.0) {
        // A probe run does NOT clear this branch, and saying only "NOT MEASURED" after a run the
        // user just watched succeed reads as a failed run. The estimator drops every probe LABEL
        // while probe_spawn_offset_ms is 0 (latency_estimator.py _close_probe -> probe_uncalibrated)
        // because press->appear still contains the game's own spawn constant. The SPREAD survives
        // that -- a constant offset cannot move it -- so the jitter is real and logged, just not
        // here. Point at it rather than let a good run look like a bad one.
        out = QStringLiteral(
            "Actuation latency: mean NOT MEASURED (needs the D_spawn constant) — "
            "fused rule on a flat 12.0ms prior.\n"
            "   Jitter IS measured: grep the sidecar log for \"probe raw spread\".\n");
    } else {
        out = QStringLiteral("Actuation latency: %1 ms   jitter (sd): %2 ms   n=%3\n")
                  .arg(automation_.measuredLatencyMs(), 0, 'f', 1)
                  .arg(sd, 0, 'f', 2)
                  .arg(n);
    }

    if (conf <= 0.0) {
        out += QStringLiteral(
            "Console tick phase: NOT MEASURED — snap disabled, sigma_tick stays 4.8 ms.");
    } else {
        // The snap's whole value is the 4.8 -> ~1.8 collapse, so quote the delta, not the raw
        // sd: "conf 0.82" tells the user nothing about whether it is worth anything.
        out += QStringLiteral("Console tick phase: locked (conf %1, sd %2 ms) — snap active, "
                              "sigma_tick 4.8 -> %3 ms.")
                   .arg(conf, 0, 'f', 2)
                   .arg(automation_.tickPhaseSdMs(), 0, 'f', 2)
                   .arg(std::max(0.5, automation_.tickPhaseSdMs()), 0, 'f', 1);
    }
    return out;
}

void OrionAppController::startLatencyCalibration()
{
    // remoteRunning_ also covers Connecting for preview continuity. Calibration requires the
    // exact Running state so every controlled marker has a live console/input/capture route.
    if (remotePlay_.state() != RemotePlayState::Running) {
        latencyCalibrationStatus_ = QStringLiteral(
            "Press Connect and wait for Remote Play to reach Running first.");
        appendLog(QStringLiteral(
            "Timing latency calibration not started: Remote Play is not Running."));
        emit latencyCalibrationChanged();
        return;
    }
    // Running video alone is insufficient: safe/defense mode, a missing virtual
    // controller, a recovering direct-input pipe, or a delivery fault all leave
    // the engine deliberately disarmed. Do not advertise an active calibration
    // that cannot own or submit the bounded controlled measurement/validation releases.
    if (!automation_.armed()) {
        latencyCalibrationStatus_ = QStringLiteral(
            "Bot controller route is not ready. Re-enable Bot + Controller, then retry.");
        appendLog(QStringLiteral(
            "Timing latency calibration not started: automation route is disarmed."));
        emit latencyCalibrationChanged();
        return;
    }
    if (latencyCalibrationReady_) {
        latencyCalibrationStatus_ = QStringLiteral(
            "Ready - timing latency is already measured for this live capture path.");
        emit latencyCalibrationChanged();
        return;
    }

    automation_.setLatencyCalibrationMode(true);
    appendLog(QStringLiteral(
        "Explicit timing setup started: two controlled stages, not gameplay. L1 measures the "
        "route, L2 validates a safe non-cap stop, ambiguous evidence retries its current stage, "
        "and later delivered tip shots refine the estimate automatically."));
}

void OrionAppController::cancelLatencyCalibration()
{
    // A diagnostic cancel is authoritative even if a queued status update already made the
    // internal state look inactive. Production has no customer-facing setup prompt.
    const bool wasActive = latencyCalibrationActive_
        || automation_.latencyCalibrationMode();
    // The engine owns release authority; revoke it before consulting the queued UI mirror.
    // Otherwise a stale `latencyCalibrationActive_ == false` can turn Cancel into a no-op.
    automation_.setLatencyCalibrationMode(false);
    if (!wasActive) {
        return;
    }
    latencyCalibrationStatusOverride_ = latencyCalibrationSamples_ > 0
        ? QStringLiteral("Cancelled - existing measured samples were kept.")
        : QStringLiteral("Cancelled.");
    appendLog(QStringLiteral("Timing latency calibration cancelled."));
}

void OrionAppController::startMeterCalibration()
{
    if (remotePlay_.state() != RemotePlayState::Running) {
        meterCalStatus_ = QStringLiteral("Start the stream first, then calibrate while you shoot.");
        emit statusChanged();
        return;
    }
    meterCalibrating_ = true;
    meterCalShots_ = 0;
    meterCalStatus_ = QStringLiteral("Watching for your meter — take %1 shots.").arg(meterCalTarget_);

    // No boxing: let the detector auto-pick the meter colour from candidates while the user
    // shoots, and open a sidecar calibration window so it accumulates the green-chevron hue.
    auto data = config_.data();
    data.autoMeterColor = true;
    saveConfigSilently(data);
    remotePlay_.calibrateMeter(QStringLiteral("start"), meterCalTarget_);

    appendLog(QStringLiteral("Meter calibration started — shoot %1 (auto-learn colour + green hue, no boxing).")
                  .arg(meterCalTarget_));
    emit statusChanged();
}

void OrionAppController::cancelMeterCalibration()
{
    if (!meterCalibrating_) {
        return;
    }
    meterCalibrating_ = false;
    meterCalStatus_.clear();
    remotePlay_.calibrateMeter(QStringLiteral("cancel"), 0);
    appendLog(QStringLiteral("Meter calibration cancelled."));
    emit statusChanged();
}

void OrionAppController::finishMeterCalibration()
{
    meterCalibrating_ = false;
    remotePlay_.calibrateMeter(QStringLiteral("finish"), meterCalShots_);
    meterCalStatus_ = QStringLiteral("Calibrated ✓ — meter colour: %1, green hue learned from %2 shots.")
                          .arg(config_.data().meterColor)
                          .arg(meterCalShots_);
    appendLog(QStringLiteral("Meter calibration complete after %1 shots.").arg(meterCalShots_));
    emit statusChanged();
}

void OrionAppController::recordManualShotResult(bool made)
{
    if (manualShotTally_.record(made)) {
        appendLog(QStringLiteral("Manual shot tally: action=%1 makes=%2 misses=%3 total=%4 source=user")
                      .arg(made ? QStringLiteral("make") : QStringLiteral("miss"))
                      .arg(manualShotMakes()).arg(manualShotMisses()).arg(manualShotTotal()));
        emit manualShotTallyChanged();
    }
}

void OrionAppController::undoManualShotResult()
{
    if (manualShotTally_.undo()) {
        appendLog(QStringLiteral("Manual shot tally: action=undo makes=%1 misses=%2 total=%3 source=user")
                      .arg(manualShotMakes()).arg(manualShotMisses()).arg(manualShotTotal()));
        emit manualShotTallyChanged();
    }
}

void OrionAppController::resetManualShotResults()
{
    if (manualShotTally_.reset()) {
        appendLog(QStringLiteral("Manual shot tally: action=reset makes=0 misses=0 total=0 source=user"));
        emit manualShotTallyChanged();
    }
}

// ═══════════════════════════════════════════════════════════════════════════════════════
// [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] The live shot-verdict tally.
//
// The owner reads NBA 2K27's own shot-feedback banner while he moves ONE slider and could
// not hold the score in his head: "can't tell if I found my value or not because sometimes
// it's green". The sidecar now grades that banner with the SAME validated reader the
// offline grader uses (tools/timing/panel_grade.py) and emits one verdict per banner
// appearance; these three functions are the whole native side of the feature.
//
// PRESENTATION ONLY, three times over: the verdict is never handed to AutomationEngine, it
// never enters the telemetry snapshot, and it never influences a timing decision. It is a
// scoreboard for a human.
// ═══════════════════════════════════════════════════════════════════════════════════════
void OrionAppController::observeBannerVerdict(const QString& timing, const QString& timingColor,
                                              const QString& coverage, double ncc,
                                              qint64 frameEpochMs, int seq, int attributed,
                                              qint64 releaseSeq, double releaseDelayMs,
                                              bool hasCoverage)
{
    // The cell COLOUR and the match score are the sidecar's own evidence for the word; the
    // tally classifies on the WORD (see ShotVerdictTally::bucketFor - the panel paints a
    // coverage cell green too, so a colour rule would count open misses as makes). They stay
    // in the signal for the log line RemotePlaySession already writes.
    Q_UNUSED(timingColor);
    Q_UNUSED(ncc);
    Q_UNUSED(releaseDelayMs);
    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] THE ONE PLACE A BANNER REACHES THE ENGINE.
    // Everything else about this function is unchanged and still presentation-only; this single
    // call is the bounded closed loop (see AutomationEngine::observeBannerVerdict and
    // BannerLeadTrim.h). Three fences, all of them here rather than downstream:
    //   * attributed != 1        -> a panel with no BOT release behind it (a replay screen, a
    //                               shot the owner took by hand) never moves the lead.
    //   * releaseSeq <= 0        -> no release id to match on; the engine could not tell which
    //                               shot TYPE graded, so the bucket would be a guess.
    //   * the engine's own ring  -> the epoch must belong to a release THIS engine made.
    // Coverage travels with the verdict: only explicit OPEN/WIDE OPEN panels
    // may calibrate the open-shot correction. Every panel still reaches the tally.
    // [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] ...plus the panel's LAYOUT, because a panel with
    // NO coverage cell (the 2-cell TIMING | DISTANCE layout: drills, and any no-defender
    // context) has no defender to shrink the window and calibrates as open. The engine owns that
    // rule and its kill switch; this is only the wire.
    // Run BEFORE the de-dupe below on purpose: the two windows are independent (the tally
    // de-dupes on the verdict counter, the trim on the release id), and the engine has its own
    // one-verdict-per-release fence.
    if (attributed == 1 && releaseSeq > 0) {
        automation_.observeBannerVerdict(static_cast<quint64>(releaseSeq), timing, coverage,
                                         hasCoverage);
    }
    if (!bannerTally_.record(timing, coverage, frameEpochMs, seq)) {
        return;   // blank word, or a seq this window already holds
    }
    // The customer Activity line. Plain, one per shot, exactly what the banner said - no
    // engineering detail, so it reads as a shot log and not as telemetry.
    appendCustomerEvent(coverage.trimmed().isEmpty()
                            ? QStringLiteral("Shot: %1").arg(bannerTally_.lastTiming())
                            : QStringLiteral("Shot: %1 · %2")
                                  .arg(bannerTally_.lastTiming(), bannerTally_.lastCoverage()));
    emit bannerTallyChanged();
}

// [ORION_RELEASE_ORACLE_TRIM 2026-09-15 owner] The banner-free half of the closed loop.
//
// Unlike the tally above this is NOT presentation: the oracle exists only to move the trim, and
// there is nothing on screen that shows it. Two fences here, matching observeBannerVerdict's:
//   * releaseSeq <= 0 -> the sidecar could not attribute the measurement, so the engine could
//                        not tell which shot TYPE it graded and the bucket would be a guess.
//   * the engine's own ring -> AutomationEngine::observeReleaseOracle refuses an epoch this
//                        engine did not release, and parks the rest behind the banner's window.
// There is no `attributed` field to check: the sidecar emits an oracle only for a release it
// already bound, and `release_seq` IS that binding.
void OrionAppController::observeReleaseOracle(qint64 releaseSeq, double gapPx,
                                              const QString& proxy)
{
    if (releaseSeq <= 0) {
        return;
    }
    automation_.observeReleaseOracle(static_cast<quint64>(releaseSeq), gapPx, proxy);
}

void OrionAppController::observeShotRange(qint64 releaseSeq, const QString& range, double conf)
{
    if (releaseSeq <= 0) {
        return;
    }
    automation_.noteShotRange(static_cast<quint64>(releaseSeq), range, conf);
}

void OrionAppController::resetBannerTally()
{
    if (bannerTally_.reset()) {
        emit bannerTallyChanged();
    }
}

void OrionAppController::resetBannerTallyForSession()
{
    if (bannerTally_.resetSession()) {
        emit bannerTallyChanged();
    }
}

void OrionAppController::recordSessionGrade(int releaseSeq, const QString& shotType,
                                            const QString& verdict, double errorMs,
                                            const QString& source)
{
    // Release ids are monotonic for the whole engine lifetime (including resets). Reject
    // duplicates and delayed labels rather than letting recovery/replay inflate the rate.
    if (releaseSeq <= 0 || releaseSeq <= lastSessionGradedReleaseSeq_) {
        return;
    }
    const bool green = verdict.compare(QStringLiteral("EXCELLENT"), Qt::CaseInsensitive) == 0
        || verdict.compare(QStringLiteral("GREEN"), Qt::CaseInsensitive) == 0;
    const bool recognized = green
        || verdict.compare(QStringLiteral("EARLY"), Qt::CaseInsensitive) == 0
        || verdict.compare(QStringLiteral("LATE"), Qt::CaseInsensitive) == 0
        || verdict.compare(QStringLiteral("OVER"), Qt::CaseInsensitive) == 0;
    if (!recognized) {
        appendLog(QStringLiteral("Session grade skipped: seq=%1 unknown_verdict=%2 source=%3")
                      .arg(releaseSeq).arg(verdict, source));
        return;
    }

    lastSessionGradedReleaseSeq_ = releaseSeq;
    ++sessionVerdicts_;
    if (green) {
        ++sessionGreens_;
    }
    const QString displayVerdict = green ? QStringLiteral("EXCELLENT") : verdict.toUpper();
    lastVerdictByType_.insert(shotType, displayVerdict);
    appendLog(QStringLiteral("Session grade: seq=%1 verdict=%2 greens=%3 misses=%4 graded=%5 "
                             "source=%6 shot=%7")
                  .arg(releaseSeq).arg(displayVerdict)
                  .arg(sessionGreens_).arg(sessionVerdicts_ - sessionGreens_)
                  .arg(sessionVerdicts_).arg(source, shotType));

    if (userLog_.enabled()) {
        if (green) {
            userLog_.append(QStringLiteral("Shot timing: on target (green) â€” %1.").arg(shotType));
        } else if (std::isfinite(errorMs)) {
            const QString direction = displayVerdict == QLatin1String("EARLY")
                ? QStringLiteral("early") : QStringLiteral("late");
            userLog_.append(QStringLiteral("Shot timing: a little %1 (about %2 ms) â€” %3.")
                                .arg(direction).arg(qAbs(errorMs), 0, 'f', 0).arg(shotType));
        } else {
            userLog_.append(QStringLiteral("Shot timing: %1 of the detected green window â€” %2.")
                                .arg(displayVerdict == QLatin1String("EARLY")
                                         ? QStringLiteral("early") : QStringLiteral("over"),
                                     shotType));
        }
    }
    emit statusChanged();
}

void OrionAppController::applyPreset(const QString& name)
{
    auto data = config_.data();
    const QString n = name.trimmed().toLower();
    if (n == QLatin1String("playstation") || n == QLatin1String("ps5")) {
        data.streamBandwidthMode = QStringLiteral("Balanced");   // 720p60, the live-proven default
        data.streamRenderBackend = QStringLiteral("vulkan");
        data.streamAudioEnabled = false;
        data.streamAudioMode = QStringLiteral("Off");
        data.controllerLightbarEnabled = true;
    } else if (n == QLatin1String("quality")) {
        data.streamBandwidthMode = QStringLiteral("Quality");    // 1080p60
        data.streamRenderBackend = QStringLiteral("vulkan");
    } else if (n == QLatin1String("performance")) {
        data.streamBandwidthMode = QStringLiteral("LowBandwidth"); // 540p, lowest latency
        data.streamRenderBackend = QStringLiteral("vulkan");
    } else {
        appendLog(QStringLiteral("Unknown preset: %1").arg(name));
        return;
    }
    persistConfig(data, QStringLiteral("Preset applied: %1").arg(name));
}

void OrionAppController::saveRemoteSettings()
{
    auto data = config_.data();
    data.remotePlayConsoleIp = data.remotePlayConsoleIp.trimmed();
    data.remotePlayClientMode = QStringLiteral("chiaki");
    data.remotePlayWindowTitle = data.remotePlayWindowTitle.trimmed().isEmpty() ? QStringLiteral("Chiaki") : data.remotePlayWindowTitle.trimmed();
    data.chiakiPath = data.chiakiPath.trimmed();
    persistConfig(data, QStringLiteral("Chiaki Remote Play settings saved."));
}

void OrionAppController::saveTimingSettings()
{
    auto data = config_.data();
    data.latencyCompensationMs = qBound(0.0, data.latencyCompensationMs, 160.0);
    data.releaseThresholdPct = qBound(50.0, data.releaseThresholdPct, 100.0);
    data.tempoWaitMs = qBound(0.0, data.tempoWaitMs, 250.0);
    data.tempoFallbackTimeoutMs = qBound(200.0, data.tempoFallbackTimeoutMs, 1500.0);
    data.tempoFlickHoldMs = qBound(16.0, data.tempoFlickHoldMs, 250.0);
    data.tempoMinStickHoldMs = qBound(0.0, data.tempoMinStickHoldMs, 500.0);
    data.minimumHoldMs = qBound(0.0, data.minimumHoldMs, data.maximumHoldMs);
    data.maximumHoldMs = qBound(qMax(50.0, data.minimumHoldMs), data.maximumHoldMs, 5000.0);
    data.noMeterBaseOffsetMs = qBound(-200.0, data.noMeterBaseOffsetMs, 200.0);  // widened 2026-08-08 — see AppConfig.cpp for rationale
    data.noMeterDecodeCompMs = qBound(0.0, data.noMeterDecodeCompMs, 40.0);
    data.stableFrames = qBound(1, data.stableFrames, 10);
    persistConfig(data, QStringLiteral("Timing settings saved and applied."));
}

void OrionAppController::refreshSecurity()
{
    // The full integrity/host scan hashes release files and enumerates processes/
    // drivers. Keep the exact 1 Hz cadence, but run it on the persistent security
    // worker so video presentation and the 4 ms input loop cannot be stalled.
    // The current verdict remains authoritative until this generation completes.
    periodicSecurityEvaluator_.request(securityEvaluationFence_.current());
    updateRuntimeStatus();
}

void OrionAppController::verifyReleaseIntegrityNow()
{
    QString detail;
    const bool ok = security_.verifyReleaseIntegrity(&detail);
    integrityState_ = detail;
    appendLog(QStringLiteral("Release integrity: %1 - %2").arg(ok ? QStringLiteral("OK") : QStringLiteral("FAILED"), detail));
    updateSecurityStatus();
}

void OrionAppController::clearInvalidLocalEntitlement()
{
    QString error;
    if (security_.clearLocalEntitlement(&error)) {
        entitlementState_ = security_.entitlementState();
        appendLog(QStringLiteral("Local entitlement cache cleared."));
    } else {
        entitlementState_ = QStringLiteral("Entitlement clear failed: %1").arg(error);
        appendLog(entitlementState_);
    }
    updateSecurityStatus();
}

void OrionAppController::signSettings()
{
#ifdef ORION_PRODUCTION_BUILD
    // [RT-MED-09 2026-09-23] Never an unconditional re-sign in a customer build: that would
    // launder a hand-edited settings.json. Customers get the scoped repairSettings() instead.
    appendLog(QStringLiteral("Settings engine detail: signSettings is not available in this build"));
#else
    QString error;
    if (security_.writeSettingsSignature(&error)) {
        appendLog(QStringLiteral("Settings signature refreshed."));
    } else {
        appendLog(QStringLiteral("Settings signature failed: %1").arg(error));
    }
#endif
    updateSecurityStatus();
}

bool OrionAppController::meterAnchorTrained() const
{
    return config_.data().meterTemplateAnchor.value(QStringLiteral("enabled")).toBool(false);
}

QString OrionAppController::trainMeterAnchor(double anchorXFrac, double anchorYFrac,
                                            double anchorWFrac, double anchorHFrac,
                                            double meterXFrac, double meterYFrac,
                                            double meterWFrac, double meterHFrac)
{
    const QImage img = lastRemoteFrame_;
    if (img.isNull() || img.width() < 16 || img.height() < 16) {
        return QStringLiteral("No live frame to train from — start the stream first.");
    }
    const int fw = img.width();
    const int fh = img.height();
    const auto frac = [](double v) { return std::clamp(v, 0.0, 1.0); };

    QRect aRect(int(frac(anchorXFrac) * fw), int(frac(anchorYFrac) * fh),
                std::max(6, int(anchorWFrac * fw)), std::max(6, int(anchorHFrac * fh)));
    aRect = aRect.intersected(QRect(0, 0, fw, fh));
    if (aRect.width() < 6 || aRect.height() < 6) {
        return QStringLiteral("Anchor selection too small — draw a box over a clear HUD landmark.");
    }

    QRect mRect(int(frac(meterXFrac) * fw), int(frac(meterYFrac) * fh),
                std::max(4, int(meterWFrac * fw)), std::max(8, int(meterHFrac * fh)));
    mRect = mRect.intersected(QRect(0, 0, fw, fh));
    if (mRect.width() < 4 || mRect.height() < 8) {
        return QStringLiteral("Meter selection too small — draw a box over the shot meter.");
    }

    const QString relPath = QStringLiteral("calibration/meter_anchor.png");
    const QString absPath = orionDataDir(rootDir_) + QStringLiteral("/") + relPath;
    QDir().mkpath(orionDataDir(rootDir_) + QStringLiteral("/calibration"));
    const QImage glyph = img.copy(aRect);
    if (glyph.isNull() || !glyph.save(absPath, "PNG")) {
        return QStringLiteral("Failed to save the anchor template to %1.").arg(absPath);
    }

    QJsonObject anchor;
    anchor.insert(QStringLiteral("enabled"), true);
    anchor.insert(QStringLiteral("template_path"), relPath);
    QJsonObject band;
    constexpr double margin = 0.15;
    band.insert(QStringLiteral("x0"), std::clamp(anchorXFrac - margin, 0.0, 1.0));
    band.insert(QStringLiteral("y0"), std::clamp(anchorYFrac - margin, 0.0, 1.0));
    band.insert(QStringLiteral("x1"), std::clamp(anchorXFrac + anchorWFrac + margin, 0.0, 1.0));
    band.insert(QStringLiteral("y1"), std::clamp(anchorYFrac + anchorHFrac + margin, 0.0, 1.0));
    anchor.insert(QStringLiteral("search_band"), band);
    QJsonObject off;
    off.insert(QStringLiteral("dx"), mRect.x() - aRect.x());
    off.insert(QStringLiteral("dy"), mRect.y() - aRect.y());
    off.insert(QStringLiteral("w"), mRect.width());
    off.insert(QStringLiteral("h"), mRect.height());
    anchor.insert(QStringLiteral("meter_offset"), off);
    anchor.insert(QStringLiteral("match_confidence_min"), 0.6);
    QJsonArray ref;
    ref.append(fw);
    ref.append(fh);
    anchor.insert(QStringLiteral("ref_wh"), ref);

    AppConfigData data = config_.data();
    data.meterTemplateAnchor = anchor;
    persistConfig(data, QStringLiteral("Meter anchor trained (glyph %1x%2 px, meter offset %3,%4). Reconnect the stream to apply.")
                            .arg(aRect.width()).arg(aRect.height())
                            .arg(mRect.x() - aRect.x()).arg(mRect.y() - aRect.y()));
    emit settingsChanged();
    return QString();
}

void OrionAppController::clearMeterAnchor()
{
    AppConfigData data = config_.data();
    data.meterTemplateAnchor = QJsonObject();
    persistConfig(data, QStringLiteral("Meter anchor training cleared — using plain colour detection."));
    emit settingsChanged();
}

void OrionAppController::connectVirtualController()
{
    if (virtualConnectInProgress_) {
        return;
    }
    QScopedValueRollback<bool> connectGuard(virtualConnectInProgress_, true);
    pollPhysicalController();
    // [ORION_METER_DELAY 2026-08-07] Same relaxation as the outer Connect gate:
    // an enumerated-but-idle pad is enough to spin up the virtual target.
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-07] Tightened to hasRecentRawInput()
    // for the same reason as the outer gate (see comment above): an enumerated
    // pad that never emitted a HID report cannot drive the virtual target and
    // stalls the entire pipeline until the user gives up.
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Same admission predicate as the
    // outer Connect gate (padConnectGateAction), WITHOUT the wake probe: this
    // path is also reached from the 4 ms poll tick's auto retry, where a
    // blocking wake would stall the GUI input loop. The user-clicked Connect
    // path has already run the wake before it calls into here, so a pad the
    // wake recovered passes both gates on the same click.
    if (padConnectGateAction(physicalPadLive_, hasRecentRawInput(), rawInputPresent_)
        != PadConnectGateAction::Proceed) {
        setControllerLifecycle(
            rawInputPresent_ ? ControllerLifecycleState::PhysicalPresentNoLive : ControllerLifecycleState::NoPhysical,
            rawInputPresent_
                ? QStringLiteral("Physical detected, waiting for input report")
                : QStringLiteral("Plug controller into PC - no live reports"));
        appendLog(QStringLiteral("Virtual controller start blocked: no live physical controller."));
        return;
    }
    setControllerLifecycle(ControllerLifecycleState::VirtualStarting, QStringLiteral("Virtual controller starting"));
#ifdef Q_OS_WIN
    bool slotsBefore[XUSER_MAX_COUNT] = {};
    for (DWORD i = 0; i < XUSER_MAX_COUNT; ++i) {
        XINPUT_STATE st = {};
        slotsBefore[i] = XInputGetState(i, &st) == ERROR_SUCCESS;
    }
#endif

    QString error;
    // Virtual pad target is ALWAYS the X360 pad (chiaki maps it to PS5 — the proven, only
    // supported path). The DS4 emulation target was unreliable (the bot failed to take over
    // shots), so it is no longer selectable; we force XUSB regardless of any stale config.
    const bool preferXusb = true;
    // Serialize with the precise-fire thread. VirtualController has NO internal lock — every
    // other submit site (fire thread, poll tick, watchdog, freeze thread, startup) takes
    // submitMutex_, but this reconnect path did not. A user hitting Connect Controller while a
    // scheduled release sits inside its ~6ms fire horizon would re-target the ViGEm pad and push
    // a neutral report concurrently with the fire thread's release write — corrupting or dropping
    // the release on exactly the shot in flight. The lock also covers connectController itself,
    // since that is what swaps the target the other thread is writing to.
    QMutexLocker submitLock(&submitMutex_);
    if (!controller_.connectController(&error, preferXusb)) {
        setControllerLifecycle(ControllerLifecycleState::ControllerFault, QStringLiteral("Virtual controller failed: %1").arg(error));
        appendLog(QStringLiteral("Virtual controller failed: %1").arg(error));
        return;
    }
    ControllerState neutral;
    if (!controller_.submit(neutral, &error)) {
        setControllerLifecycle(ControllerLifecycleState::ControllerFault, QStringLiteral("Virtual submit failed: %1").arg(error));
        appendLog(controllerStatus_);
    } else {
        setControllerLifecycle(ControllerLifecycleState::VirtualReady, QStringLiteral("Virtual controller ready"));
        appendLog(QStringLiteral("Controller: %1").arg(controllerStatus_));
    }

#ifdef Q_OS_WIN
    // After connecting the virtual pad, probe every XInput slot so we can tell the
    // user exactly which slot the virtual pad landed on AND which slots already
    // contain a physical pad. chiaki picks the lowest-numbered SDL controller index
    // by default, so the user needs to know which one to select if both are present.
    QStringList slotInfo;
    const int knownVirtualSlot = controller_.xinputUserIndex();
    int virtualSlot = -1;
    int physicalSlot = -1;
    if (knownVirtualSlot >= 0) {
        virtualSlot = knownVirtualSlot;
    }

    // If the ViGEm user-index export is unavailable, infer virtual slot by looking
    // for the XInput slot that appeared immediately after target creation.
    if (virtualSlot < 0) {
        for (DWORD i = 0; i < XUSER_MAX_COUNT; ++i) {
            XINPUT_STATE st = {};
            if (XInputGetState(i, &st) == ERROR_SUCCESS && !slotsBefore[i]) {
                virtualSlot = static_cast<int>(i);
                break;
            }
        }
    }

    for (DWORD i = 0; i < XUSER_MAX_COUNT; ++i) {
        XINPUT_STATE st = {};
        XINPUT_CAPABILITIES caps = {};
        if (XInputGetState(i, &st) == ERROR_SUCCESS) {
            DWORD capRes = XInputGetCapabilities(i, XINPUT_FLAG_GAMEPAD, &caps);
            const bool isVigemByCaps = (capRes == ERROR_SUCCESS) && (caps.Flags & XINPUT_CAPS_NO_NAVIGATION);
            const bool isVirtual = (virtualSlot >= 0 && int(i) == virtualSlot)
                || (virtualSlot < 0 && isVigemByCaps);
            if (virtualSlot < 0 && isVirtual) virtualSlot = int(i);
            else if (physicalSlot < 0 && !isVirtual) physicalSlot = int(i);
            slotInfo << QStringLiteral("slot %1=%2")
                            .arg(int(i))
                            .arg(isVirtual ? QStringLiteral("virtual") : QStringLiteral("physical"));
        }
    }

    inferredVirtualXinputSlot_ = virtualSlot;
    if (!slotInfo.isEmpty()) {
        appendLog(QStringLiteral("XInput pads: %1").arg(slotInfo.join(QStringLiteral(", "))));
    }
    if (virtualSlot >= 0 && knownVirtualSlot < 0) {
        appendLog(QStringLiteral("Inferred virtual XInput slot: %1 (ViGEm user index export unavailable)")
                      .arg(virtualSlot + 1));
    }
    if (virtualSlot >= 0 && physicalSlot >= 0 && physicalSlot < virtualSlot) {
        appendLog(QStringLiteral("Note: chiaki may pick the physical pad on slot %1 "
                                 "instead of the virtual on slot %2. In chiaki's settings, "
                                 "select the controller named after ViGEm/XUSB so the bot's "
                                 "automated inputs reach the PS5.")
                      .arg(physicalSlot).arg(virtualSlot));
    }
#endif

    emit statusChanged();
}

void OrionAppController::refreshControllerDevices()
{
#ifdef Q_OS_WIN
    controllerSelector_.reset();
    rawInputDeviceKinds_.clear();
    rawInputPresent_ = false;
    rawInputLabel_.clear();
    rawInputDevicePath_.clear();
    lastRawInputPresenceCheckMs_ = 0;
    appendLog(QStringLiteral("Controller devices refreshed."));
    pollPhysicalController();
#else
    appendLog(QStringLiteral("Controller refresh is only available on Windows."));
#endif
}

void OrionAppController::refreshCaptureDevices()
{
    if (captureRefreshInFlight_) return;
    captureRefreshInFlight_ = true;
    struct InventoryResult {
        VideoInputDeviceInventory inventory;
        std::atomic<bool> ready{false};
    };
    auto result = std::make_shared<InventoryResult>();
    // Worker owns only its result; it never dereferences a destroyed controller.
    std::thread([result]() {
        try { result->inventory = enumerateVideoInputDevices(); } catch (...) {}
        result->ready.store(true, std::memory_order_release);
    }).detach();
    auto* poll = new QTimer(this);
    poll->setInterval(25);
    connect(poll, &QTimer::timeout, this, [this, result, poll]() {
        if (!result->ready.load(std::memory_order_acquire)) return;
        poll->stop();
        poll->deleteLater();
        captureRefreshInFlight_ = false;
        const auto& devices = result->inventory.friendlyNames;
        if (captureDeviceList_ != devices || captureDeviceIds_ != result->inventory.stableIds) {
            captureDeviceList_ = devices;
            captureDeviceIds_ = result->inventory.stableIds;
            emit captureDevicesChanged();
        }
        appendLog(QStringLiteral("Capture devices: %1")
                      .arg(devices.isEmpty() ? QStringLiteral("none found") : devices.join(QStringLiteral(", "))));
    });
    poll->start();
}

void OrionAppController::applyLightbarNow()
{
    applyControllerLightbar(true);
    appendLog(QStringLiteral("Lightbar apply requested: %1").arg(controllerLedStatus_));
}

void OrionAppController::openWindowsGameControllersPanel()
{
#ifdef Q_OS_WIN
    QProcess::startDetached(QStringLiteral("control.exe"), {QStringLiteral("joy.cpl")});
#else
    appendLog(QStringLiteral("Windows game controller panel is unavailable on this platform."));
#endif
}

void OrionAppController::clearLogs()
{
    logs_.clear();
    emit logsChanged();
}

void OrionAppController::setCurrentPage(const QString& page)
{
    if (currentPage_ == page) {
        return;
    }
    currentPage_ = page;
    emit navigationChanged();
}

void OrionAppController::setConsoleIp(const QString& value)
{
    auto data = config_.data();
    if (data.remotePlayConsoleIp == value) {
        return;
    }
    data.remotePlayConsoleIp = value;
    networkBridge_.setConsoleIp(value);
    saveConfigSilently(data);
}

void OrionAppController::setPacketCaptureEnabled(bool value)
{
    // [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in flag is deleted;
    // this property now drives the network feature itself (default ON).
    auto data = config_.data();
    if (data.networkEnabled == value) {
        return;
    }
    data.networkEnabled = value;
    if (!saveConfigSilently(data)) {
        return;
    }

    if (value) {
        appendLog(QStringLiteral(
            "Network features enabled by user (diagnostic only; never timing authority)."));
        ensurePacketBridgeRunning();
        networkBridge_.start();
    } else if (packetBridgeLinkConfigured(data.networkEnabled, data.meterDelayEnabled)) {
        // [ORION_METER_DELAY_LINK 2026-08-08] The network opt-out must not tear
        // down the link Meter Delay is actively using: the bridge is the delay's
        // actuator, and stopping it here would zero the delay mid-session while the
        // card still said "Active". The court discovery the delay's session gate
        // rides stays up with the link.
        appendLog(QStringLiteral(
            "Network features disabled by user; packet bridge stays up (Meter Delay owns the link)."));
    } else {
        appendLog(QStringLiteral("Network features disabled by user."));
        networkBridge_.stop();
        telemetry_.diagnosticCourtIp.clear();
        // [ORION_METER_DELAY 2026-08-07] Diagnostic identity revoked without a
        // telemetry beat — re-evaluate the court-IP session gate.
        meterDelay_.setCourtIpKnown(!telemetryCourtIp().isEmpty());
    }
    emit telemetryChanged();
}

void OrionAppController::setRemotePlayConsole(const QString& value)
{
    const QString v = (value.trimmed().toLower() == QLatin1String("xbox"))
        ? QStringLiteral("Xbox") : QStringLiteral("PS5");
    auto data = config_.data();
    if (data.remotePlayConsole == v) {
        return;
    }
    if (remotePlay_.state() == RemotePlayState::Running
            || remotePlay_.state() == RemotePlayState::Connecting) {
        appendLog(QStringLiteral("Disconnect before changing the console route."));
        emit settingsChanged();
        return;
    }
    remotePlay_.stop(); // Retire any warm HDMI preview, not the launcher.
    chiakiEmbedWatchdog_.stop();
    switchRemotePlayConsole(data, v);
    if (!saveConfigSilently(data)) {
        emit settingsChanged();
        return;
    }
    // The X360 ViGEm pad already works for both (chiaki maps it to PS5; the Xbox app
    // reads XInput directly). PS5 = chiaki Remote Play + decoder pipe; Xbox = capture
    // the explicitly selected Xbox app window via WGC.
    appendLog(v == QLatin1String("Xbox")
        ? QStringLiteral("Console: Xbox beta — select Microsoft's Remote Play window in Setup. WGC + X360 route; Xbox lead is separate from PS5.")
        : QStringLiteral("Console: PS5 — chiaki Remote Play + DualSense mapping (default path)."));
}

void OrionAppController::openXboxRemotePlay()
{
    if (!QDesktopServices::openUrl(QUrl(QStringLiteral("xbox://"))))
        appendLog(QStringLiteral("Open the Xbox Windows app and start Remote Play, then refresh the window list in Setup."));
}

void OrionAppController::setXboxRemotePlayWindowTitle(const QString& value)
{
    if (remotePlay_.state() == RemotePlayState::Running
            || remotePlay_.state() == RemotePlayState::Connecting) {
        emit settingsChanged();
        return;
    }
    auto data = config_.data();
    data.xboxRemotePlayWindowTitle = value.trimmed().left(512);
    if (data.xboxRemotePlayWindowTitle != config_.data().xboxRemotePlayWindowTitle)
        saveConfigSilently(data);
}

void OrionAppController::setXboxUntestedAcknowledged(bool value)
{
    auto data = config_.data();
    if (data.xboxUntestedAcknowledged == value) {
        emit settingsChanged();
        return;
    }
    // Withdrawal policy (Astra, release review): the acknowledgement cannot be
    // withdrawn while an Xbox session is Connecting/Running -- same rule as the
    // console route itself (setRemotePlayConsole). Disconnect first; the gate
    // then refuses the next start. Granting it mid-session is harmless.
    if (!value && isXboxRemotePlay(data)
            && (remotePlay_.state() == RemotePlayState::Running
                || remotePlay_.state() == RemotePlayState::Connecting)) {
        appendLog(QStringLiteral("Disconnect before withdrawing the Xbox untested-in-beta acknowledgement."));
        emit settingsChanged();
        return;
    }
    data.xboxUntestedAcknowledged = value;
    saveConfigSilently(data);
    appendLog(value ? QStringLiteral("Xbox: untested-in-beta notice acknowledged.")
                    : QStringLiteral("Xbox: untested-in-beta acknowledgement withdrawn."));
}

QStringList OrionAppController::xboxRemotePlayWindows() const
{
    QStringList titles;
#ifdef Q_OS_WIN
    EnumWindows([](HWND hwnd, LPARAM context) -> BOOL {
        if (!IsWindowVisible(hwnd) || IsIconic(hwnd))
            return TRUE;
        wchar_t title[1024] = {};
        GetWindowTextW(hwnd, title, 1024);
        const QString text = QString::fromWCharArray(title);
        if (!text.contains(QStringLiteral("Xbox"), Qt::CaseInsensitive))
            return TRUE;
        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        HANDLE proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        if (!proc)
            return TRUE;
        wchar_t image[32768] = {};
        DWORD size = 32768;
        const BOOL ok = QueryFullProcessImageNameW(proc, 0, image, &size);
        CloseHandle(proc);
        const QString exe = ok ? QFileInfo(QString::fromWCharArray(image, int(size))).fileName().toLower() : QString();
        const QStringList allowed = {QStringLiteral("msedge.exe"), QStringLiteral("chrome.exe"),
            QStringLiteral("firefox.exe"), QStringLiteral("xboxpcapp.exe"), QStringLiteral("xboxapp.exe"),
            QStringLiteral("xboxgame streaming.exe"), QStringLiteral("applicationframehost.exe")};
        if (allowed.contains(exe))
            reinterpret_cast<QStringList*>(context)->append(text);
        return TRUE;
    }, reinterpret_cast<LPARAM>(&titles));
#endif
    titles.sort(Qt::CaseInsensitive);
    titles.removeDuplicates();
    return titles;
}

void OrionAppController::setChiakiPath(const QString& value)
{
    auto data = config_.data();
    const auto cleaned = value.trimmed().left(512);
    if (data.chiakiPath == cleaned) {
        return;
    }
    data.chiakiPath = cleaned;
    data.remotePlayClientMode = QStringLiteral("chiaki");
    saveConfigSilently(data);
}

void OrionAppController::setProfileUser(const QString& value)
{
    auto data = config_.data();
    if (data.remotePlayProfile == value) {
        return;
    }
    data.remotePlayProfile = value;
    saveConfigSilently(data);
}

void OrionAppController::setPin(const QString& value)
{
    auto cleaned = value;
    cleaned.remove(QRegularExpression(QStringLiteral("[^0-9]")));
    cleaned = cleaned.left(8);
    if (pin_ == cleaned) {
        return;
    }
    pin_ = cleaned;
    emit settingsChanged();
}

void OrionAppController::setRedirectUrl(const QString& value)
{
    if (redirectUrl_ == value) {
        return;
    }
    redirectUrl_ = value.left(4096);
    emit settingsChanged();
}

void OrionAppController::setTimingLatencyMs(double value)
{
    auto data = config_.data();
    data.latencyCompensationMs = qBound(0.0, value, 160.0);
    saveConfigSilently(data);
}

void OrionAppController::setReleaseThresholdPct(double value)
{
    auto data = config_.data();
    data.releaseThresholdPct = qBound(50.0, value, 100.0);
    saveConfigSilently(data);
}

void OrionAppController::setEarlyLateOffsetMs(double value)
{
    auto data = config_.data();
    data.earlyLateOffsetMs = qBound(-100.0, value, 100.0);
    saveConfigSilently(data);
}

void OrionAppController::setTempoInputSource(const QString& value)
{
    // This property is the explicit opt-in boundary for raw stick ownership.
    // Unknown values remain fail-safe (Square), while `both` exposes the normal
    // Square Button/Tempo path plus separately qualified TempoStick and Go-To.
    const QString normalized = normalizedRemotePlayInputSource(value);
    auto data = config_.data();
    if (data.remotePlayInputSource == normalized) {
        return;
    }
    data.remotePlayInputSource = normalized;
    saveConfigSilently(data);
}

void OrionAppController::setTempoRemapType(const QString& value)
{
    // Legacy setter retained for older callers. The shipped Tempo control uses
    // setTempoInputPath to update this saved preference and the active source atomically.
    const QString lower = value.trimmed().toLower();
    const QString norm = (lower == QLatin1String("stick")) ? QStringLiteral("stick")
        : (lower == QLatin1String("both")) ? QStringLiteral("both")
        : QStringLiteral("button");
    auto data = config_.data();
    if (data.tempoRemapType == norm) {
        return;
    }
    data.tempoRemapType = norm;
    saveConfigSilently(data);
}

QString OrionAppController::tempoRemapTypeForShot(const QString& shotType) const
{
    // Legacy diagnostic helper retained for compatibility with older QML. It is
    // not a runtime actuation authority and is not exposed by the shipped UI.
    if (shotType.contains(QLatin1String("Go-To"), Qt::CaseInsensitive)) {
        return QStringLiteral("stick");
    }
    const QString t = config_.data().tempoRemapType;
    if (t == QLatin1String("both")) {
        // "Both" = auto per shot type: step-back / fade shots want the stick gather->flick;
        // everything else the button. (In Skele mode the release simply follows whichever
        // trigger you hold, so "Both" is the natural follow-the-trigger default there.)
        return (shotType.contains(QLatin1String("Fade"), Qt::CaseInsensitive)
                || shotType.contains(QLatin1String("Step"), Qt::CaseInsensitive))
            ? QStringLiteral("stick") : QStringLiteral("button");
    }
    return t;
}

void OrionAppController::setNoDipLeadMs(double value)
{
    const double v = qBound(-150.0, value, 150.0);
    auto data = config_.data();
    if (data.noDipLeadMs == v) {
        return;
    }
    data.noDipLeadMs = v;
    saveConfigSilently(data);
}

// [ORION_USER_LEAD] ------------------------------------------------------------------------
// Shot Lead: the user's own value, and this install's measured one.
//
// Every write through here is a USER act, so it latches actuationLeadUserSet. That latch is the
// entire guarantee behind "your value is never silently overwritten": the measured seed below
// refuses to touch a control that has ever been moved.
QString OrionAppController::leadCalibrationHint() const
{
    if (!leadCalActive_) {
        return QString();
    }
    if (leadCal_.locked) {
        // [COPY-FIX 2026-09-23 CW-6/EA-30] Speak the Shot Lead card's 1..100 scale, not ms.
        return QStringLiteral(
            "Locked at Shot Lead %1 after %2 shots. Saved — you should not need to touch this "
            "again unless you change capture hardware.")
            .arg(ui_notifications::shotLeadSliderValue(leadCal_.leadMs)).arg(leadCal_.shots);
    }
    if (leadCal_.shots == 0) {
        return QStringLiteral(
            "Take a shot, then tap what the game's TIMING banner said. Skip any shot where you "
            "did not see the banner.");
    }
    return QStringLiteral("Shot %1 — Shot Lead %2 so far. Keep going until it locks.")
        .arg(leadCal_.shots).arg(ui_notifications::shotLeadSliderValue(leadCal_.leadMs));
}

void OrionAppController::beginLeadCalibration()
{
    // [ORION_LEAD_CALIBRATION] Refuse when an env override owns the lead. ORION_LEAD_FLOOR_MS or
    // ORION_LEAD_BIAS_MS set config_.leadOverrideFromEnv, and the engine then SKIPS the user-lead
    // branch entirely -- so a calibration would run, feel like it worked, write a value, and be
    // silently discarded. That exact trap already cost this project a session of sweeps through a
    // dead slider; refusing loudly is the only honest behaviour.
    if (automation_.config().leadOverrideFromEnv) {
        // Engineering line (rule 0 hides ORION_ names) + one customer line.
        appendLog(QStringLiteral(
            "Lead calibration refused: an environment override owns the lead "
            "(ORION_LEAD_FLOOR_MS / ORION_LEAD_BIAS_MS). Clear it and relaunch, or the calibrated "
            "value would be silently ignored."));
        appendLog(QStringLiteral("Calibration isn't available right now — restart Venice."));
        return;
    }
    // Start from the user's existing lead if they have one, else 300 ms. 300 is not this rig's
    // number smuggled in as a default -- it is the centre of the plausible range for the setup
    // this product targets (Remote Play through a capture card, where the card alone is ~160 ms).
    // Starting mid-range means the bisection brackets from either side in roughly equal steps; a
    // start at an extreme wastes shots walking in one direction before it can reverse.
    // [ORION_LEAD_CALIBRATION 2026-09-21] ...and refuse when no shot can be taken: the spec's
    // "no capture -> no calibration". A flow that cannot fire is a flow that cannot be graded.
    if (!remoteRunning_) {
        appendLog(QStringLiteral(
            "Lead calibration needs a running session: start Remote Play (or the capture card) "
            "and take open shots in shoot-around."));
        return;
    }
    if (leadCalActive_) {
        return;   // already running; the screen shows its state
    }
    constexpr double kCalibrationStartMs = 300.0;
    // [2026-09-23 owner] On an Auto install start from the value Venice is ACTUALLY flying (the
    // measured auto seed) when it has one - the best first guess this rig can offer.
    const bool autoLead = actuationLeadMs() <= 0.0;
    const double startMs = !autoLead ? actuationLeadMs()
        : (leadAutoSeedActive_ && leadAutoSeedMs_ > 0.0 ? leadAutoSeedMs_ : kCalibrationStartMs);
    leadCal_ = LeadCalibrationPolicy::begin(startMs);
    // [RT-MED-01 / CL3-F4-009 2026-09-23] Snapshot the WHOLE tuple, not just the number: an Auto
    // install is (0, user_set=false) and must come back as exactly that on Cancel.
    leadCalStartLead_ = captureActuationLeadProvenance(config_.data());
    leadCalWroteLead_ = false;
    if (autoLead) {
        // [2026-09-23 owner: "yes, save the value it locks on"] Every verdict must grade the lead
        // under test. On Auto the engine kept flying its seed while the bisection believed it was
        // at 300, so the first taps graded the wrong number and a lock at the start value
        // persisted nothing. Apply the start value now; Cancel still restores Auto exactly.
        setActuationLeadMs(leadCal_.leadMs);
        leadCalWroteLead_ = true;
    }
    leadCalActive_ = true;
    appendLog(QStringLiteral("Lead calibration started at %1 ms.").arg(qRound(leadCal_.leadMs)));
    emit leadCalibrationChanged();
}

void OrionAppController::cancelLeadCalibration()
{
    if (!leadCalActive_) {
        return;
    }
    leadCalActive_ = false;
    // Every accepted step was persisted as it happened, so "cancel" after the lock is simply
    // "done", and before it the lead sits wherever the last graded shot left it.
    if (leadCal_.locked) {
        appendLog(QStringLiteral("Lead calibration finished: %1 ms saved after %2 shots.")
                      .arg(qRound(leadCal_.leadMs)).arg(leadCal_.shots));
    } else if (leadCalWroteLead_) {
        // [2026-09-22 GM-006 / CX-007] Cancel means cancel: every graded step persisted as it
        // happened, so restore the lead the customer walked in with.
        // [RT-MED-01 / CL3-F4-009 2026-09-23] ...EXACTLY: lead, user_set and the route's stash
        // entry, written verbatim. Not setActuationLeadMs(), which clamps 0 up to 150 and latches
        // user_set=true -- that turned an Auto install into a pinned, ~120 ms-short user lead.
        const QString restored = describeActuationLeadProvenance(leadCalStartLead_);
        auto data = config_.data();
        const ActuationLeadRestore outcome = restoreActuationLeadProvenance(data, leadCalStartLead_);
        if (outcome == ActuationLeadRestore::RouteChanged) {
            appendLog(QStringLiteral(
                "Lead calibration cancelled after %1 shots; the video route changed during "
                "calibration, so the current route's lead was left as it is (%2 ms).")
                          .arg(leadCal_.shots).arg(qRound(actuationLeadMs())));
        } else if (outcome == ActuationLeadRestore::Restored && !saveConfigSilently(data)) {
            appendLog(QStringLiteral(
                "Lead calibration cancelled after %1 shots, but the previous lead (%2) could not "
                "be saved.")
                          .arg(leadCal_.shots).arg(restored));
        } else {
            appendLog(QStringLiteral(
                "Lead calibration cancelled after %1 shots; restored the lead to %2.")
                          .arg(leadCal_.shots).arg(restored));
            if (outcome == ActuationLeadRestore::Restored) {
                // The banners counted during calibration belong to the abandoned values.
                resetBannerTally();
            }
        }
    } else {
        appendLog(QStringLiteral("Lead calibration cancelled; the lead is unchanged."));
    }
    leadCalWroteLead_ = false;
    emit leadCalibrationChanged();
}

void OrionAppController::reportLeadCalibrationVerdict(const QString& verdict)
{
    if (!leadCalActive_ || leadCal_.locked) {
        return;
    }
    const QString v = verdict.trimmed().toLower();
    LeadVerdict parsed = LeadVerdict::Skip;
    if (v == QLatin1String("early")) {
        parsed = LeadVerdict::Early;
    } else if (v == QLatin1String("late")) {
        parsed = LeadVerdict::Late;
    } else if (v == QLatin1String("good")) {
        parsed = LeadVerdict::Good;
    } else if (v != QLatin1String("skip")) {
        return;   // unknown verdict moves nothing rather than guessing
    }

    const double before = leadCal_.leadMs;
    leadCal_ = LeadCalibrationPolicy::apply(leadCal_, parsed);
    if (leadCal_.leadMs != before) {
        // Persist every step, not only the lock: a user who closes the app mid-calibration keeps
        // the progress they made rather than silently reverting to where they started.
        setActuationLeadMs(leadCal_.leadMs);
        leadCalWroteLead_ = true;   // [RT-MED-01] Cancel now has something to undo
    }
    appendLog(QStringLiteral(
        "Lead calibration: verdict=%1 lead %2 -> %3 ms step=%4 shots=%5 locked=%6")
                  .arg(v).arg(qRound(before)).arg(qRound(leadCal_.leadMs))
                  .arg(leadCal_.stepMs, 0, 'f', 1).arg(leadCal_.shots)
                  .arg(leadCal_.locked ? 1 : 0));
    emit leadCalibrationChanged();
}

void OrionAppController::setActuationLeadMs(double value)
{
    if (!std::isfinite(value)) {
        return;
    }
    const double v = qBound(AppConfigData::kActuationLeadMinMs, value,
                            AppConfigData::kActuationLeadMaxMs);
    auto data = config_.data();
    if (data.actuationLeadMs == v && data.actuationLeadUserSet) {
        return;
    }
    data.actuationLeadMs = v;
    data.actuationLeadUserSet = true;
    // [ORION_LEAD_BY_SOURCE 2026-09-14] the value belongs to the route it was tuned on.
    mirrorActuationLeadIntoSourceStash(data);
    if (!saveConfigSilently(data)) {
        return;
    }
    appendLog(QStringLiteral("Shot lead set to %1 ms (your value; measurement will not change it)")
                  .arg(v, 0, 'f', 0));
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14] The tally describes ONE value. The moment the
    // owner commits a new one, the banners already counted belong to the old setting - a
    // window that straddles the move is exactly the confusion the card exists to remove.
    resetBannerTally();
}

double OrionAppController::meterDelayLeadOffsetMaxMs() const noexcept
{
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] The largest offset THIS rig can still schedule:
    // the visible-evidence ceiling (maxSchedulableTipLeadMs, mirrored here through the same
    // tip-timing accessor the ShotLeadUsableMaxIndicator uses) minus the delay-0 Shot Lead.
    // Clipped to the setting's own band so the UI never offers a value the setter would reject.
    // <= 0 means the delay-0 lead already sits at the ceiling: no delayed condition is reachable
    // through an in-video anchor until Shot Lead comes down or Tip Timing goes up.
    const double baseLeadMs = config_.data().actuationLeadMs;
    if (!std::isfinite(baseLeadMs) || baseLeadMs <= 0.0) {
        return AppConfigData::kMeterDelayLeadOffsetMaxMs;
    }
    const double headroomMs = shotLeadMaxUsableMs() - baseLeadMs;
    if (!std::isfinite(headroomMs) || headroomMs <= 0.0) {
        return 0.0;
    }
    return std::min(headroomMs, AppConfigData::kMeterDelayLeadOffsetMaxMs);
}

void OrionAppController::setMeterDelayLeadOffsetMs(double value)
{
    if (!std::isfinite(value)) {
        return;
    }
    const double v = qBound(AppConfigData::kMeterDelayLeadOffsetMinMs, value,
                            AppConfigData::kMeterDelayLeadOffsetMaxMs);
    auto data = config_.data();
    if (data.meterDelayLeadOffsetMs == v) {
        return;
    }
    data.meterDelayLeadOffsetMs = v;
    if (!saveConfigSilently(data)) {
        return;
    }
    const double ceilingMs = meterDelayLeadOffsetMaxMs();
    // Warn, never clamp (see the header note). The number the operator needs is the ceiling,
    // and they need it at the moment they cross it, not shot by shot afterwards.
    const QString warn = (v > ceilingMs)
        ? QStringLiteral(" — ABOVE this rig's schedulable max (+%1 ms): live tip shots will "
                         "abort while the delay is applied.")
              .arg(ceilingMs, 0, 'f', 0)
        : QString();
    appendLog(QStringLiteral(
                  "Meter Delay Lead Offset set to %1 ms (applied only while the meter delay is "
                  "engaged; delay-off timing is unchanged)%2")
                  .arg(v, 0, 'f', 0)
                  .arg(warn));
}

void OrionAppController::nudgeActuationLeadMs(double deltaMs)
{
    if (!std::isfinite(deltaMs)) {
        return;
    }
    const auto& data = config_.data();
    // Nudging an unconfigured control starts from whatever the user is actually being shown: the
    // measured lead if this install has one, otherwise the midpoint of the range. It must never
    // start from a hard-coded "known good" number — that would ship one rig's latency to everyone.
    const double base = data.actuationLeadMs > 0.0
        ? data.actuationLeadMs
        : (actuationLeadMeasuredMs_ > 0.0
               ? actuationLeadMeasuredMs_
               : 0.5 * (AppConfigData::kActuationLeadMinMs + AppConfigData::kActuationLeadMaxMs));
    setActuationLeadMs(base + deltaMs);
}

void OrionAppController::resetActuationLead()
{
    auto data = config_.data();
    if (data.actuationLeadMs <= 0.0 && !data.actuationLeadUserSet) {
        return;
    }
    data.actuationLeadMs = 0.0;
    data.actuationLeadUserSet = false;
    // [ORION_LEAD_BY_SOURCE 2026-09-14] Reset clears THIS route only: the stash entry is
    // removed (absent == not configured), and the other route keeps the lead it was tuned to.
    mirrorActuationLeadIntoSourceStash(data);
    if (!saveConfigSilently(data)) {
        return;
    }
    appendLog(QStringLiteral("Shot lead reset — it will be measured from your next shots again"));
}

// ===== [ORION_USER_TIP] Tip Timing ==========================================================
//
// FRAME DISCIPLINE, because getting it wrong silently re-aims the bot (pickup prompt §9.5):
// every value crossing this boundary is converted through the two shared helpers in
// AppConfig.h and NOWHERE else. The UI frame is the EFFECTIVE aim (what the decision path
// consumes); the storage frame is the learner's canonical base-30 PHYSICAL prior
// (learning.json learned_phase_physical_ms). `activeConstant` below is the LIVE engine
// constant (already carries the base-20 shift when that regime is on); `defaultSeed` is the
// COMPILED RemapConfig default — see the helper comment for why mixing those up
// double-counts the 58.3ms shift.

namespace {
// The compiled tip-phase defaults the conversions are anchored to. A function-local static
// const instead of constructing RemapConfig per call — the struct carries QMaps.
const RemapConfig& tipTimingRemapDefaults()
{
    static const RemapConfig defaults;
    return defaults;
}
}   // namespace

double OrionAppController::tipTimingMs() const
{
    const double activeConstant = automation_.config().tipPhaseConstantMs;
    const double canonical = config_.learning().learnedPhasePhysicalMs;
    if (canonical > 0.0) {
        return orion::tipTimingEffectiveFromCanonical(
            canonical, activeConstant, tipTimingRemapDefaults().tipPhaseSeedPhysicalMs);
    }
    // No prior at all (fresh install / fresh profile): the engine consumes exactly the
    // shipped constant, and displaying anything else would quote an aim the bot is not on.
    return activeConstant;
}

double OrionAppController::tipTimingMinMs() const
{
    // The learner's own plausibility band, expressed in the effective frame. Canonical band
    // edges are the COMPILED defaults (240/430): the live band already carries the base-20
    // shift, and effective = canonical + activeConstant - defaultSeed absorbs that same
    // shift through activeConstant — using the live band here would double-count it.
    const auto& defaults = tipTimingRemapDefaults();
    return orion::tipTimingEffectiveFromCanonical(
        defaults.tipPhaseLearnMinMs, automation_.config().tipPhaseConstantMs,
        defaults.tipPhaseSeedPhysicalMs);
}

double OrionAppController::tipTimingMaxMs() const
{
    const auto& defaults = tipTimingRemapDefaults();
    return orion::tipTimingEffectiveFromCanonical(
        defaults.tipPhaseLearnMaxMs, automation_.config().tipPhaseConstantMs,
        defaults.tipPhaseSeedPhysicalMs);
}

double shotLeadMaxUsableForTipTimingMs(double tipTimingEffectiveMs) noexcept
{
    // See the header note: documented UI mirror of AutomationEngine::maxSchedulableTipLeadMs()
    // (constant - kTipLeadScheduleMarginMs). 30ms covers the measured decision latency range
    // (frame_age 8.5-17.4ms + 2-9ms fill advance, 2026-08-08 batch); keep in lockstep with
    // kTipLeadScheduleMarginMs in AutomationEngine.cpp.
    constexpr double kTipLeadScheduleMarginUiMs = 30.0;
    return qMax(0.0, tipTimingEffectiveMs - kTipLeadScheduleMarginUiMs);
}

double OrionAppController::shotLeadMaxUsableMs() const
{
    return shotLeadMaxUsableForTipTimingMs(tipTimingMs());
}

double OrionAppController::meterDelayAppliedNowMs() const noexcept
{
    // [ORION_LEAD_CONFLICT_UI task #48] The actuator's applied value, not the persisted
    // slider target: the ShotLeadUsableMaxIndicator must reflect the delay the ENGINE is
    // judged against (setMeterDelayCondition pushes this same value), including partial
    // ramps and the defense-bypass ramp to 0.
    return meterDelay_.currentDelayMs();
}

QString OrionAppController::shotLeadUsableMaxWarning(double leadMs, double usableMaxMs,
                                                     double appliedDelayMs) const
{
    return orion::shotLeadUsableMaxWarningLine(leadMs, usableMaxMs, appliedDelayMs);
}

bool OrionAppController::shotLeadConflictActiveNow() const
{
    // Mirror of the engine's config-time conflict gate (AutomationEngine::applyConfig,
    // [ORION_LEAD_CONFLICT]) over the public RemapConfig snapshot, minus userActuationLeadSet:
    // a SEEDED in-band lead above the usable max is exactly as unschedulable as a typed one,
    // and the live-miss diagnostic in the engine does not require the latch either.
    const auto& rc = automation_.config();
    if (!rc.tipPhaseEnabled || rc.leadOverrideFromEnv) {
        return false;
    }
    if (!std::isfinite(rc.userActuationLeadMs)
        || rc.userActuationLeadMs < rc.actuationLeadMinMs
        || rc.userActuationLeadMs > rc.actuationLeadMaxMs) {
        return false;
    }
    return rc.userActuationLeadMs > shotLeadMaxUsableMs();
}

OrionAppController::ActiveLeadTelemetry OrionAppController::activeLeadForTelemetry() const
{
    // In-band classification identical to AutomationEngine::actuationLeadSourceLabel(): a valid
    // in-band value replaces the authority, and the env sweep out-ranks everything (labelled
    // authority there too). The engine's exact consumed value for the authority branch is
    // private to it; this layer reports the last authority-published median instead, and -1
    // when the authority has never published to the app layer.
    const auto& rc = automation_.config();
    const bool inBand = !rc.leadOverrideFromEnv
        && std::isfinite(rc.userActuationLeadMs)
        && rc.userActuationLeadMs >= rc.actuationLeadMinMs
        && rc.userActuationLeadMs <= rc.actuationLeadMaxMs;
    if (inBand) {
        return {rc.userActuationLeadMs,
                rc.userActuationLeadSet ? QStringLiteral("user") : QStringLiteral("seed")};
    }
    return {actuationLeadMeasuredMs_ > 0.0 ? actuationLeadMeasuredMs_ : -1.0,
            QStringLiteral("authority")};
}

double OrionAppController::tipTimingMeasuredMs() const
{
    // Same frame arithmetic as tipTimingMs() above, applied to the instrument's own persisted
    // measurement (measured_phase_physical_ms; canonical base-30 physical). -1 = none yet.
    const double canonical = config_.learning().measuredPhasePhysicalMs;
    if (!(canonical > 0.0)) {
        return -1.0;
    }
    return orion::tipTimingEffectiveFromCanonical(
        canonical, automation_.config().tipPhaseConstantMs,
        tipTimingRemapDefaults().tipPhaseSeedPhysicalMs);
}

QString presentEngineDiagnosticLine(const QString& line)
{
    // See the header note. Only the frozen self-grader's artifact line is rewritten; the
    // startsWith + grader_truth=0 guard keeps every other diagnostic byte-identical.
    if (!line.startsWith(QLatin1String("Outcome identity: "))
        || !line.contains(QLatin1String(" grader_truth=0"))) {
        return line;
    }
    static const QRegularExpression verdictToken(
        QStringLiteral("\\bverdict=([A-Za-z0-9_]+)"));
    const QRegularExpressionMatch m = verdictToken.match(line);
    if (!m.hasMatch()) {
        return line;
    }
    QString out = line;
    out.replace(m.capturedStart(), m.capturedLength(),
                QStringLiteral("verdict=ungraded-artifact(%1)").arg(m.captured(1)));
    return out;
}

void persistMeasuredPhaseMedian(AppConfig& config, double measuredPhysicalMs)
{
    LearningData data = config.learning();
    data.measuredPhasePhysicalMs = measuredPhysicalMs;
    config.saveLearning(data);
}

void OrionAppController::setTipTimingMs(double effectiveMs)
{
    if (!std::isfinite(effectiveMs)) {
        return;   // refuse garbage outright — no value is installed at all
    }
    // Clamp INTO the learner's plausibility band rather than refusing near-misses: the band
    // edge is the nearest aim this control is allowed to install (same policy as the Shot
    // Lead's qBound). A fat-fingered 4355 therefore lands on the band edge, never on a
    // guaranteed-miss aim; the engine-side restore consumes only what this wrote, so no
    // out-of-band value can enter through this path.
    const double lo = tipTimingMinMs();
    const double hi = tipTimingMaxMs();
    const double v = qBound(lo, effectiveMs, hi);
    const auto& defaults = tipTimingRemapDefaults();
    const double canonical = orion::tipTimingCanonicalFromEffective(
        v, automation_.config().tipPhaseConstantMs, defaults.tipPhaseSeedPhysicalMs);

    // ORDER MATTERS: persist the learning slot FIRST, then the settings flags — the settings
    // save is what re-runs applyConfig (syncBackendConfig), and applyConfig must restore the
    // NEW prior and latch it as the frozen aim in the same pass.
    LearningData learning = config_.learning();
    learning.learnedPhasePhysicalMs = canonical;
    config_.saveLearning(learning);

    auto data = config_.data();
    data.tipPhaseAimFrozen = true;    // the manual value outranks the learner while locked
    data.tipTimingUserSet = true;
    if (!saveConfigSilently(data)) {
        emit tipTimingChanged();      // still re-sync the card with whatever actually holds
        return;
    }
    appendLog(QStringLiteral(
                  "Tip timing set to %1 ms (your value; locked — the learner will not move it)")
                  .arg(v, 0, 'f', 1));
    emit tipTimingChanged();
}

void OrionAppController::setTipTimingLocked(bool locked)
{
    auto data = config_.data();
    if (data.tipPhaseAimFrozen == locked) {
        return;
    }
    data.tipPhaseAimFrozen = locked;
    if (!locked) {
        // Unlocking IS handing the aim back to the learner, so the "your setting" claim
        // must drop with it — an unlocked value is the learner's starting point, not a
        // user guarantee.
        data.tipTimingUserSet = false;
    }
    if (!saveConfigSilently(data)) {
        emit tipTimingChanged();
        return;
    }
    appendLog(locked
                  // [ORION_TIP_RESTORED 2026-09-11] "for this session" was wrong and it mattered:
                  // tip_phase_aim_frozen is persisted, so the lock survives restarts. The owner
                  // needs the permanence stated, because a drifting aim is what it exists to stop.
                  ? QStringLiteral("Tip timing locked at %1 ms (persists across restarts; "
                                   "the learner keeps measuring but no longer moves the aim)")
                        .arg(tipTimingMs(), 0, 'f', 1)
                  : QStringLiteral("Tip timing unlocked — Venice's learner is adjusting it again"));
    emit tipTimingChanged();
}

void OrionAppController::nudgeTipTimingMs(double deltaMs)
{
    if (!std::isfinite(deltaMs)) {
        return;
    }
    // Nudging starts from what the user is being shown — the current effective aim, learned
    // or manual — never from a hard-coded constant (that would quote the shipped constant as
    // the aim, the exact mistake §9.5 exists to prevent).
    setTipTimingMs(tipTimingMs() + deltaMs);
}

void OrionAppController::resetTipTiming()
{
    auto data = config_.data();
    if (!data.tipPhaseAimFrozen && !data.tipTimingUserSet) {
        return;
    }
    data.tipPhaseAimFrozen = false;
    data.tipTimingUserSet = false;
    if (!saveConfigSilently(data)) {
        emit tipTimingChanged();
        return;
    }
    // DELIBERATE SEMANTIC: reset restores learner CONTROL, not some earlier learned number.
    // The manual value remains the learner's prior and the learner walks away from it as
    // real landings arrive — a smooth handover instead of an instant re-aim to a value
    // nobody has graded recently.
    appendLog(QStringLiteral(
        "Tip timing handed back to Venice's learner — it keeps adapting from the current value"));
    emit tipTimingChanged();
}

void OrionAppController::setTempoWaitMs(double value)
{
    auto data = config_.data();
    data.tempoWaitMs = qBound(0.0, value, 250.0);
    saveConfigSilently(data);
}

void OrionAppController::setRhythmFlickDelayMs(double value)
{
    // [ORION_RHYTHM_FLICK_DELAY 2026-09-14] Mirrors setActuationLeadMs: reject non-finite,
    // clamp into the persisted band, no-op on an unchanged value (QML re-binds this on every
    // settingsChanged, and an unguarded write would re-sign settings.json on every repaint),
    // then persist. saveConfigSilently() -> syncBackendConfig() -> automation_.applyConfig()
    // is what pushes the new trim into the live engine, exactly as the Shot Lead setter does.
    if (!std::isfinite(value)) {
        return;
    }
    const double v = qBound(AppConfigData::kRhythmFlickDelayMinMs, value,
                            AppConfigData::kRhythmFlickDelayMaxMs);
    auto data = config_.data();
    if (data.rhythmFlickDelayMs == v) {
        return;
    }
    data.rhythmFlickDelayMs = v;
    if (!saveConfigSilently(data)) {
        return;
    }
    appendLog(QStringLiteral(
                  "Rhythm flick timing set to %1 ms (%2; applies only while Rhythm is on)")
                  .arg(v, 0, 'f', 0)
                  .arg(v > 0.0 ? QStringLiteral("flick fires LATER")
                               : (v < 0.0 ? QStringLiteral("flick fires EARLIER")
                                          : QStringLiteral("no trim"))));
}

void OrionAppController::setTempoFallbackMs(double value)
{
    auto data = config_.data();
    data.tempoFallbackTimeoutMs = qBound(200.0, value, 1500.0);
    saveConfigSilently(data);
}

void OrionAppController::setTempoFlickHoldMs(double value)
{
    auto data = config_.data();
    data.tempoFlickHoldMs = qBound(16.0, value, 250.0);
    saveConfigSilently(data);
}

// [ORION_TEMPO_RELEASE_STYLE 2026-09-15 owner] Ignore-unknown, never guess: a value that is
// neither "flick" nor "letgo" leaves the current style in force rather than writing a style the
// engine would have to interpret. Same rule the file loader and the env override apply.
void OrionAppController::setTempoReleaseStyle(const QString& value)
{
    const QString normalized = value.trimmed().toLower();
    if (normalized != QLatin1String("flick") && normalized != QLatin1String("letgo")) {
        return;
    }
    auto data = config_.data();
    if (data.tempoReleaseStyle == normalized) {
        return;
    }
    data.tempoReleaseStyle = normalized;
    saveConfigSilently(data);
}

void OrionAppController::setTempoMinStickHoldMs(double value)
{
    auto data = config_.data();
    data.tempoMinStickHoldMs = qBound(0.0, value, 500.0);
    saveConfigSilently(data);
}

void OrionAppController::setRttSyncMode(const QString& value)
{
    const QString normalized = value.trimmed().compare(QStringLiteral("manual"), Qt::CaseInsensitive) == 0
                                   ? QStringLiteral("Manual")
                                   : QStringLiteral("Auto");
    auto data = config_.data();
    if (data.rttSyncMode == normalized) {
        return;
    }
    data.rttSyncMode = normalized;
    saveConfigSilently(data);
    automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
    emit telemetryChanged();
}

void OrionAppController::setStreamBandwidthMode(const QString& value)
{
    const QString trimmed = value.trimmed();
    const QString lower = trimmed.toLower();
    QString normalized;
    RemotePlaySession::BandwidthMode enumVal = RemotePlaySession::BandwidthMode::Balanced;
    if (lower == QLatin1String("quality")) {
        normalized = QStringLiteral("Quality");
        enumVal = RemotePlaySession::BandwidthMode::Quality;
    } else if (lower == QLatin1String("lowbandwidth") || lower == QLatin1String("low") || lower == QLatin1String("low_bandwidth")) {
        normalized = QStringLiteral("LowBandwidth");
        enumVal = RemotePlaySession::BandwidthMode::LowBandwidth;
    } else if (lower == QLatin1String("ultralow") || lower == QLatin1String("ultra_low") || lower == QLatin1String("ultra")) {
        normalized = QStringLiteral("UltraLow");
        enumVal = RemotePlaySession::BandwidthMode::UltraLow;
    } else if (lower == QLatin1String("experimental120") || lower == QLatin1String("probe120")) {
        normalized = QStringLiteral("Experimental120");
        enumVal = RemotePlaySession::BandwidthMode::Experimental120;
    } else if (lower == QLatin1String("experimental240") || lower == QLatin1String("probe240")) {
        normalized = QStringLiteral("Experimental240");
        enumVal = RemotePlaySession::BandwidthMode::Experimental240;
    } else {
        normalized = QStringLiteral("Balanced");
        enumVal = RemotePlaySession::BandwidthMode::Balanced;
    }

    auto data = config_.data();
    if (data.streamBandwidthMode == normalized) {
        // Still push to chiaki settings — covers first-run when chiaki may not have
        // the optimised values yet even though our config already does.
        remotePlay_.applyBandwidthMode(enumVal);
        return;
    }
    data.streamBandwidthMode = normalized;
    saveConfigSilently(data);
    remotePlay_.applyBandwidthMode(enumVal);
    emit settingsChanged();
}

void OrionAppController::setStreamAudioEnabled(bool value)
{
    auto data = config_.data();
    if (data.streamAudioEnabled == value) {
        return;
    }
    data.streamAudioEnabled = value;
    if (value && data.streamAudioMode == QLatin1String("Off")) {
        data.streamAudioMode = QStringLiteral("Standard");
    } else if (!value) {
        data.streamAudioMode = QStringLiteral("Off");
    }
    saveConfigSilently(data);
    remotePlay_.setAudioMode(data.streamAudioMode);
    appendLog(value ? QStringLiteral("Chiaki audio unmuted.") : QStringLiteral("Chiaki audio muted."));
    emit settingsChanged();
}

void OrionAppController::setStreamAudioMode(const QString& value)
{
    QString normalized = value.trimmed();
    const QString lower = normalized.toLower();
    if (lower == QLatin1String("stabilized")) {
        normalized = QStringLiteral("Stabilized");
    } else if (lower == QLatin1String("standard") || lower == QLatin1String("on")) {
        normalized = QStringLiteral("Standard");
    } else {
        normalized = QStringLiteral("Off");
    }
    auto data = config_.data();
    const bool enabled = normalized != QLatin1String("Off");
    if (data.streamAudioMode == normalized && data.streamAudioEnabled == enabled) {
        return;
    }
    data.streamAudioMode = normalized;
    data.streamAudioEnabled = enabled;
    saveConfigSilently(data);
    remotePlay_.applyConfig(data);
    remotePlay_.setAudioMode(normalized);
    appendLog(enabled
        ? QStringLiteral("Chiaki audio mode set to %1.").arg(normalized)
        : QStringLiteral("Chiaki audio muted."));
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarEnabled(bool value)
{
    auto data = config_.data();
    if (data.controllerLightbarEnabled == value) {
        applyControllerLightbar(true);
        return;
    }
    data.controllerLightbarEnabled = value;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarColor(const QString& value)
{
    const QString normalized = QColor(value).isValid()
        ? QColor(value).name(QColor::HexRgb).toUpper()
        : QStringLiteral("#7C3AED");
    auto data = config_.data();
    if (data.controllerLightbarColor == normalized) {
        applyControllerLightbar(true);
        return;
    }
    data.controllerLightbarColor = normalized;
    data.controllerLightbarPrimaryColor = normalized;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarMode(const QString& value)
{
    const QString lower = value.trimmed().toLower();
    QString normalized;
    if (lower == QLatin1String("pulse")) normalized = QStringLiteral("Pulse");
    else if (lower == QLatin1String("strobe")) normalized = QStringLiteral("Strobe");
    else if (lower == QLatin1String("rainbow")) normalized = QStringLiteral("Rainbow");
    else normalized = QStringLiteral("Solid");
    auto data = config_.data();
    if (data.controllerLightbarMode == normalized) {
        applyControllerLightbar(true);
        return;
    }
    data.controllerLightbarMode = normalized;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarPrimaryColor(const QString& value)
{
    const QString normalized = QColor(value).isValid()
        ? QColor(value).name(QColor::HexRgb).toUpper()
        : QStringLiteral("#7C3AED");
    auto data = config_.data();
    if (data.controllerLightbarPrimaryColor == normalized) {
        applyControllerLightbar(true);
        return;
    }
    data.controllerLightbarPrimaryColor = normalized;
    data.controllerLightbarColor = normalized;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarSecondaryColor(const QString& value)
{
    const QString normalized = QColor(value).isValid()
        ? QColor(value).name(QColor::HexRgb).toUpper()
        : QStringLiteral("#4F8CFF");
    auto data = config_.data();
    if (data.controllerLightbarSecondaryColor == normalized) {
        applyControllerLightbar(true);
        return;
    }
    data.controllerLightbarSecondaryColor = normalized;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarBrightness(double value)
{
    auto data = config_.data();
    const double normalized = qBound(0.05, value, 1.0);
    if (std::abs(data.controllerLightbarBrightness - normalized) < 0.005) {
        return;
    }
    data.controllerLightbarBrightness = normalized;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

void OrionAppController::setControllerLightbarEffectSpeed(double value)
{
    auto data = config_.data();
    const double normalized = qBound(0.2, value, 4.0);
    if (std::abs(data.controllerLightbarEffectSpeed - normalized) < 0.005) {
        return;
    }
    data.controllerLightbarEffectSpeed = normalized;
    saveConfigSilently(data);
    applyControllerLightbar(true);
    emit settingsChanged();
}

// ---------------------------------------------------------------------------
// Detection overlay appearance. Presentation only: these three setters write a
// config field, persist it, and emit settingsChanged. None of them touches the
// detector, the automation engine, the input route, or any clock. The overlay
// they style is painted on top of a frame that has already been decoded and
// already been decided upon.
// ---------------------------------------------------------------------------

void OrionAppController::setMeterOverlayColor(const QString& value)
{
    // Fall back to the default blue rather than to "invalid": QML would
    // render an invalid colour as transparent black, i.e. it would silently
    // delete the lock box.
    const QColor parsed(value);
    const QString normalized = parsed.isValid()
        ? parsed.name(QColor::HexRgb).toUpper()
        : QString::fromLatin1(AppConfigData::kMeterOverlayDefaultColor);
    auto data = config_.data();
    if (data.meterOverlayColor == normalized) {
        return;
    }
    data.meterOverlayColor = normalized;
    saveConfigSilently(data);
    emit settingsChanged();
    // Only observable when the cycle is off; while RGB is on the draw colour is
    // owned by the cycle and this is just the colour it will snap back to.
    if (!data.meterOverlayRgb) {
        emit meterOverlayDrawColorChanged();
    }
}

void OrionAppController::setMeterOverlayStyle(const QString& value)
{
    const QString lower = value.trimmed().toLower();
    QString normalized;
    if (lower == QLatin1String("brackets")) normalized = QStringLiteral("Brackets");
    else if (lower == QLatin1String("hairline")) normalized = QStringLiteral("Hairline");
    else if (lower == QLatin1String("clean")) normalized = QStringLiteral("Clean");
    else normalized = QStringLiteral("Solid");
    auto data = config_.data();
    if (data.meterOverlayStyle == normalized) {
        return;
    }
    data.meterOverlayStyle = normalized;
    saveConfigSilently(data);
    emit settingsChanged();
}

void OrionAppController::setMeterOverlayRgb(bool value)
{
    auto data = config_.data();
    if (data.meterOverlayRgb == value) {
        return;
    }
    data.meterOverlayRgb = value;
    saveConfigSilently(data);
    // Start the hue cycle from the user's own colour instead of from red, so
    // enabling RGB reads as "my colour started moving" rather than as a jump.
    if (value) {
        const QColor base(data.meterOverlayColor);
        overlayRgbHueDeg_ = base.isValid() && base.hue() >= 0
            ? static_cast<double>(base.hue())
            : 0.0;
    }
    refreshMeterOverlayEffectTimer();
    emit settingsChanged();
    emit meterOverlayDrawColorChanged();
}

void OrionAppController::refreshMeterOverlayEffectTimer()
{
    // Two conditions, both necessary: the user asked for the cycle, and there is
    // a preview being rendered for it to appear on. Without the second the timer
    // would keep waking the GUI thread while the app sits on a settings page.
    const bool want = config_.data().meterOverlayRgb && overlayPreviewRenderActive_;
    if (want == overlayEffectTimer_.isActive()) {
        return;
    }
    if (want) {
        overlayEffectTimer_.start();
        return;
    }
    overlayEffectTimer_.stop();
    if (!overlayRgbColor_.isEmpty()) {
        // Release the cycle's hold on the draw colour so the box returns to the
        // configured colour instead of freezing on whatever hue it stopped at.
        overlayRgbColor_.clear();
        emit meterOverlayDrawColorChanged();
    }
}

void OrionAppController::updateMeterOverlayEffect()
{
    // 10 degrees per 250 ms tick = one full rotation every 9 seconds. Slow
    // enough to read as a colour rather than as a flicker, and slow enough that
    // it can never be mistaken for a state change on the lock box.
    overlayRgbHueDeg_ = std::fmod(overlayRgbHueDeg_ + 10.0, 360.0);
    // Saturation is held below full so the stroke keeps some white in it: a
    // fully-saturated yellow or cyan is close to unreadable against a floodlit
    // court, which is the background this overlay actually lives on.
    const QString next = QColor::fromHsv(static_cast<int>(overlayRgbHueDeg_), 205, 255)
                             .name(QColor::HexRgb)
                             .toUpper();
    if (next == overlayRgbColor_) {
        return;
    }
    overlayRgbColor_ = next;
    emit meterOverlayDrawColorChanged();
}

void OrionAppController::setManualSyncAdjustMs(double value)
{
    auto data = config_.data();
    const double clamped = std::max(-100.0, std::min(250.0, value));
    if (std::abs(data.manualSyncAdjustMs - clamped) < 0.05) {
        return;
    }
    data.manualSyncAdjustMs = clamped;
    saveConfigSilently(data);
    automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
    emit telemetryChanged();
}

void OrionAppController::setManualOffsetMs(double value)
{
    auto data = config_.data();
    const double clamped = std::max(-100.0, std::min(250.0, value));
    if (std::abs(data.manualOffsetMs - clamped) < 0.05) {
        return;
    }
    data.manualOffsetMs = clamped;
    saveConfigSilently(data);
    automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
    emit telemetryChanged();
}

void OrionAppController::setMeterStyle(const QString& value)
{
    auto data = config_.data();
    const auto cleaned = value.trimmed().left(48);
    // [ORION_PILL_REMOVED 2026-09-21 owner] One shared rule (normalizedMeterStyle):
    // a value the rule would rewrite is not an accepted pick, so nothing in the UI
    // or a settings round trip can select a withdrawn or unknown style.
    if (cleaned.isEmpty() || normalizedMeterStyle(cleaned) != cleaned) {
        return;
    }
    if (data.meterStyle == cleaned) {
        return;
    }
    data.meterStyle = cleaned;
    saveConfigSilently(data);
    // A different meter style is a different animation/window geometry — every type's
    // calibration must re-converge (silent, clocks kept as warm starts).
    automation_.recalibrateAllShotTypes();
    appendLog(QStringLiteral("Calibration: all shot types -> Acquire (meter style changed)"));
}

void OrionAppController::setMeterProposer(const QString& value)
{
    // Normalise exactly like the settings loader so QML can hand over labels/case
    // variants and the persisted value is always one of the two the sidecar knows.
    const QString normalized = normalizedMeterProposer(value);
    auto data = config_.data();
    if (data.meterProposer == normalized) {
        return;
    }
    data.meterProposer = normalized;
    saveConfigSilently(data);
    emit settingsChanged();
    // The locator singleton reads ORION_METER_PROPOSER once per sidecar process, so the
    // running sidecar keeps its current proposer; the next launch picks this one up.
    appendLog(QStringLiteral("Meter Detection: proposer -> %1 (applies at the next sidecar launch)")
                  .arg(normalized == QLatin1String("yolo") ? QStringLiteral("YOLO")
                                                            : QStringLiteral("Pure CV")));
}

void OrionAppController::setActiveShotType(const QString& value)
{
    auto data = config_.data();
    const auto cleaned = value.trimmed().left(32);
    if (cleaned.isEmpty() || data.activeShotType == cleaned) {
        return;
    }
    data.activeShotType = cleaned;
    saveConfigSilently(data);
}

QString OrionAppController::shotBucketKey(const QString& type) const
{
    // Button and Square-triggered Tempo now share one timing authority. Output
    // shaping must never create a second calibration namespace.
    return type.trimmed();
}

double OrionAppController::shotTypeOffset(const QString& type) const
{
    return config_.data().shotTypeOffsets.value(shotBucketKey(type), 0.0);
}

void OrionAppController::setShotTypeOffset(const QString& type, double value)
{
    const auto key = shotBucketKey(type);
    if (key.isEmpty()) {
        return;
    }
    auto data = config_.data();
    const double clamped = qBound(-250.0, value, 250.0);
    if (qFuzzyCompare(data.shotTypeOffsets.value(key, 0.0) + 1.0, clamped + 1.0)) {
        return;
    }
    data.shotTypeOffsets.insert(key, clamped);
    saveConfigSilently(data);
}

QString OrionAppController::shotTypeMode(const QString& type) const
{
    const QString ov = config_.data().shotTypeModeOverride.value(type.trimmed()).trimmed().toLower();
    return ov.isEmpty() ? QStringLiteral("auto") : ov;
}

void OrionAppController::setShotTypeMode(const QString& type, const QString& mode)
{
    const QString key = type.trimmed();
    QString m = mode.trimmed().toLower();
    if (key.isEmpty() || (m != QLatin1String("auto") && m != QLatin1String("normal") && m != QLatin1String("tempo"))) {
        return;
    }
    auto data = config_.data();
    if (m == QLatin1String("auto")) {
        data.shotTypeModeOverride.remove(key);
    } else {
        data.shotTypeModeOverride.insert(key, m);
    }
    saveConfigSilently(data);
    emit statusChanged();
}

bool OrionAppController::calibrationMode() const
{
    return config_.data().calibrationMode;
}

void OrionAppController::setCalibrationMode(bool enabled)
{
    auto data = config_.data();
    if (data.calibrationMode == enabled) {
        return;
    }
    data.calibrationMode = enabled;
    saveConfigSilently(data);
    appendLog(enabled ? QStringLiteral("Calibration mode ON (feedback-text oracle dials offsets)")
                      : QStringLiteral("Calibration mode OFF"));
    emit statusChanged();
}

void OrionAppController::recalibrateShotType(const QString& type)
{
    if (type.trimmed().isEmpty()) {
        return;
    }
    const QString key = shotBucketKey(type);   // route to the active (normal/tempo) bucket
    automation_.recalibrateShotType(key);   // phase->Acquire + clears streaks; persists via calPhaseUpdated
    appendLog(QStringLiteral("Calibration: %1 reset to Acquire (recalibrating)").arg(key));
    emit statusChanged();
}

void OrionAppController::lockShotType(const QString& type)
{
    if (type.trimmed().isEmpty()) {
        return;
    }
    const QString key = shotBucketKey(type);   // route to the active (normal/tempo) bucket
    automation_.lockShotType(key);          // phase->Lock; persists via calPhaseUpdated
    appendLog(QStringLiteral("Calibration: %1 LOCKED").arg(key));
    emit statusChanged();
}

QString OrionAppController::calibrationSnapshot() const
{
    const RemapConfig cfg = automation_.config();
    const QMap<QString, int> greens = automation_.calGreensByType();
    static const QStringList types = {QStringLiteral("Standstill"), QStringLiteral("Left Fade"),
                                      QStringLiteral("Right Fade"), QStringLiteral("Go-To")};
    QJsonObject root;
    for (const QString& t : types) {
        const QString bk = shotBucketKey(t);   // reflect the active (normal/tempo) bucket
        const int phase = cfg.shotTypeCalPhase.value(bk, 0);
        QJsonObject o;
        o.insert(QStringLiteral("locked"), phase == 1);
        o.insert(QStringLiteral("phase"), phase == 1 ? QStringLiteral("Locked") : QStringLiteral("Acquiring"));
        o.insert(QStringLiteral("clock"), cfg.shotTypeFeedforwardMs.value(bk, 0.0));
        o.insert(QStringLiteral("greens"), greens.value(bk, 0));
        o.insert(QStringLiteral("verdict"), lastVerdictByType_.value(t, QStringLiteral("-")));
        root.insert(t, o);
    }
    root.insert(QStringLiteral("grader"), automation_.activeGraderName());
    return QString::fromUtf8(QJsonDocument(root).toJson(QJsonDocument::Compact));
}

void OrionAppController::setMeterColor(const QString& value)
{
    auto data = config_.data();
    const auto cleaned = value.trimmed().left(48);
    const QString lower = cleaned.toLower();
    // Red and Purple ONLY. White/Yellow were accepted here but the reader has no band for
    // either: they wrote a config the detector could not act on, so the user got a renamed HUD
    // and zero detections. This whitelist must stay in lockstep with meter_bar_colors.SUPPORTED
    // on the Python side -- a colour accepted here but unknown there falls back to Red, which is
    // safe but silently ignores the user's choice.
    if (lower != QLatin1String("red") && lower != QLatin1String("purple")) {
        return;
    }
    if (cleaned.isEmpty() || data.meterColor == cleaned) {
        return;
    }
    data.meterColor = cleaned;
    saveConfigSilently(data);
    // A different meter color changes the detector's read — recalibrate every type.
    automation_.recalibrateAllShotTypes();
    appendLog(QStringLiteral("Calibration: all shot types -> Acquire (meter color changed)"));
}

void OrionAppController::setMeterEnabled(bool value)
{
    // The production bot cannot operate without meter authority. Keep the property for
    // settings compatibility, but refuse attempts to disable its only visual authority.
    value = true;
    auto data = config_.data();
    if (data.meterEnabled == value && !(value && data.noMeterEnabled)) {
        return;
    }
    data.meterEnabled = value;
    if (value) {
        // Mutual exclusion with No Meter.
        data.noMeterEnabled = false;
    }
    saveConfigSilently(data);
}

void OrionAppController::applyMeterDelayRuntimeConfig()
{
    // [ORION_METER_DELAY] The ONE mapping from persisted settings to the live actuator,
    // shared by the constructor priming and the QML setters below. Values only —
    // possession (setOffense) and session gates are runtime state fed elsewhere.
    const auto& cfg = config_.data();
    meterDelay_.setEnabled(cfg.meterDelayEnabled);
    meterDelay_.setManualDelayMs(static_cast<double>(cfg.meterDelayMs));
    // [ORION_DEFENSE_FLAG 2026-08-08] OffenseDefense now means "the D-pad Up
    // manual defense flag gates the delay" (bypass while defending); AlwaysOn
    // means the hotkey is a no-op and the delay holds all session. The persisted
    // toggle is the Option B master switch between the two.
    meterDelay_.setEngagePolicy(cfg.meterDelayBypassOnDefense
        ? orion::MeterDelayController::EngagePolicy::OffenseDefense
        : orion::MeterDelayController::EngagePolicy::AlwaysOn);
    // [VENICENET WAVE 2B] Mirror the engage policy into the DLL actuation client.
    // Enable + target are NOT pushed here: they flow from the controller's own
    // state machine (interceptStart/Stop + delayCommanded) so the DLL's intercept
    // lifecycle tracks the shot-aware engage gates, not the raw persisted toggle.
    veniceNet_.setEngagePolicy(cfg.meterDelayBypassOnDefense
        ? static_cast<int>(VENICENET_POLICY_OFFENSE_DEFENSE)
        : static_cast<int>(VENICENET_POLICY_ALWAYS_ON));
}

void OrionAppController::pushTempoRemapBridgeState()
{
    // [ORION_TEMPO_BRIDGE_LIVE 2026-08-08 task #36] The ONE computation of the engine's
    // tempo-remap bridge gate inputs, shared by the constructor wiring and every backend
    // transition signal:
    //   delayEngineEngaged — the delay engine is commanding (shot state Locked, or a
    //     non-zero ramp in flight). Open-loop by construction, so it is necessary but
    //     never sufficient;
    //   interceptApplying  — the service's OWN echo (VeniceNet snapshot) confirms the
    //     intercept is armed and holding a non-zero delay. This is the leg a sniff-only
    //     debug bridge can never satisfy — the filed #36 symptom.
    // Health-beat freshness is stamped separately (telemetry beats / armed snapshots) and
    // judged inside the engine at press time.
    const bool delayEngineEngaged =
        meterDelay_.shotState() == orion::MeterDelayController::ShotState::Locked
        || meterDelay_.currentDelayMs() > 0.0;
    const bool interceptApplying = meterDelayEchoActive_ && meterDelayEchoAppliedMs_ > 0.0;
    automation_.setTempoRemapBridgeState(delayEngineEngaged, interceptApplying,
                                         meterDelay_.shotStateString());
}

void OrionAppController::setMeterDelayShotCycle(bool active)
{
    // [ORION_DEFENSE_FLAG 2026-08-08] The shot cycle is DECOUPLED from the
    // engagement decision (MeterDelayController::setShotCycleActive is recorded
    // diagnostic state only; the D-pad Up manual defense flag is the sole
    // OffenseDefense gate). Its edges are therefore no longer decision points
    // and the per-shot "Meter delay policy" line moved to the hotkey handler,
    // where the decision actually happens — logging it here would claim a shot
    // influence the actuator no longer has.
    meterDelayShotCycleLastPushed_ = active;
    meterDelay_.setShotCycleActive(active);
}

void OrionAppController::setMeterDelayEnabled(bool value)
{
    auto data = config_.data();
    if (data.meterDelayEnabled == value) {
        return;
    }
    data.meterDelayEnabled = value;
    saveConfigSilently(data);
    applyMeterDelayRuntimeConfig();
    appendLog(QStringLiteral("Meter delay %1 (user setting).")
                  .arg(value ? QStringLiteral("enabled") : QStringLiteral("disabled")));
    // [ORION_METER_DELAY_LINK 2026-08-08] The toggle brings its own prerequisites up:
    // the delay is actuated through the packet bridge, and before this call the
    // bridge only ran when the (UI-less since 2026-08-06) packet-capture opt-in was
    // on — enabling Meter Delay reported success and did nothing. Both calls are
    // idempotent: ensurePacketBridgeRunning() no-ops when the bridge is reachable or
    // no backend exists on this install, NetworkBridge::start() no-ops when the
    // worker already runs. Disabling deliberately does NOT stop the bridge — the
    // controller ramps the delay to 0 and releases the intercept via its own state
    // machine (setEnabled(false) -> enterIdle -> stop_meter_intercept), and the
    // passive-diagnostics consumers keep the link.
    if (value) {
        ensurePacketBridgeRunning();
        networkBridge_.start();
    }
    // [ORION_METER_DELAY_AVAILABILITY] Never let the toggle pretend: enabling it on an
    // install with no packet bridge must say so at the moment of the click, in the log
    // AND (via meterDelayStatusText / the card banner) in the UI.
    if (value && !meterDelayBackendAvailable()) {
        appendLog(QStringLiteral(
            "Meter delay enabled, but no delay backend exists on this install "
            "(no VeniceNetSvc/NexusVisionSvc service, no nexus_svc.py debug bridge): "
            "the delay cannot engage."));
    }
    emit meterDelayStatusTextChanged();
}

void OrionAppController::setMeterDelayMs(int value)
{
    // Same band the load path enforces (AppConfig.cpp meter_delay_ms cleanInt), so a
    // stale QML binding or scripted write cannot push the actuator outside the
    // 100-600 ms band ([ORION_METER_DELAY_RANGE 2026-08-08]).
    const int clamped = qBound(AppConfigData::kMeterDelayMinMs, value,
                               AppConfigData::kMeterDelayMaxMs);
    auto data = config_.data();
    if (data.meterDelayMs == clamped) {
        return;
    }
    data.meterDelayMs = clamped;
    saveConfigSilently(data);
    applyMeterDelayRuntimeConfig();
    appendLog(QStringLiteral("Meter delay set to %1 ms (user setting).").arg(clamped));
}

void OrionAppController::setMeterDelayBypassOnDefense(bool value)
{
    auto data = config_.data();
    if (data.meterDelayBypassOnDefense == value) {
        return;
    }
    data.meterDelayBypassOnDefense = value;
    saveConfigSilently(data);
    // [ORION_DEFENSE_FLAG 2026-08-08] Option B master switch: ON = the D-pad Up
    // hotkey toggles the runtime defense flag (delay bypassed while defending);
    // OFF = the hotkey is inert and the delay is always applied. The policy
    // round-trip below (applyMeterDelayRuntimeConfig -> setEngagePolicy) clears
    // any live defense flag, so flipping this switch always lands in
    // offense/apply mode — never on a stale bypass.
    applyMeterDelayRuntimeConfig();
    appendLog(QStringLiteral("Meter delay defense bypass (D-pad Up) %1 (user setting).")
                  .arg(value ? QStringLiteral("enabled") : QStringLiteral("disabled")));
}

void OrionAppController::setAutoTune(bool value)
{
    // Auto = learning ON (clock self-tunes from the meter grade); Manual = frozen.
    auto data = config_.data();
    const bool freeze = !value;
    if (data.freezeCalibration == freeze) {
        return;
    }
    data.freezeCalibration = freeze;
    saveConfigSilently(data);
}

void OrionAppController::markStreamSetupComplete()
{
    auto data = config_.data();
    if (data.streamSetupComplete) {
        return;
    }
    // [2026-09-21 beta] The contract half of the Setup gate: never record setup as
    // complete without a usable video route, whatever the UI showed (Astra, bug sweep).
    if (isXboxRemotePlay(data)) {
        if (!data.xboxUntestedAcknowledged || data.xboxRemotePlayWindowTitle.trimmed().isEmpty()) {
            appendLog(QStringLiteral("Setup: acknowledge the Xbox notice and select the Remote Play window before continuing."));
            emit settingsChanged();
            return;
        }
    } else if (isCaptureCardSource(data) && !captureCardSelected()) {
        appendLog(QStringLiteral("Setup: select a capture device with a stable identity, or switch the video source to PS5 Remote Play."));
        emit settingsChanged();
        return;
    }
    data.streamSetupComplete = true;
    saveConfigSilently(data);
}

void OrionAppController::markPreflightComplete()
{
    auto data = config_.data();
    if (data.preflightComplete) {
        return;
    }
    data.preflightComplete = true;
    saveConfigSilently(data);
    appendLog(QStringLiteral("First-run preflight check completed."));
}

QString OrionAppController::licenseKeyMasked() const
{
    if (authLicenseKey_.isEmpty()) {
        return {};
    }
    return QStringLiteral("••••-••••-••••-%1")
        .arg(authLicenseKey_.right(4));
}

QString OrionAppController::machineIdMasked() const
{
    const QString id = security_.machineId();
    if (id.isEmpty()) {
        return {};
    }
    return QStringLiteral("••••••%1").arg(id.right(6));
}

void OrionAppController::copyProfileDiscordId()
{
    if (profile_.discordUserId.isEmpty()) {
        appendLog(QStringLiteral(
            "Copy Discord ID requested but the licence server has not reported one."));
        return;
    }
    if (auto* clipboard = QGuiApplication::clipboard()) {
        clipboard->setText(profile_.discordUserId);
        appendLog(QStringLiteral("Discord ID copied to clipboard."));
    }
}

void OrionAppController::copyLicenseKey()
{
    if (authLicenseKey_.isEmpty()) {
        appendLog(QStringLiteral("Copy key requested but no license key is loaded."));
        return;
    }
    if (auto* clipboard = QGuiApplication::clipboard()) {
        clipboard->setText(authLicenseKey_);
        appendLog(QStringLiteral("License key copied to clipboard (key_suffix=%1).")
                      .arg(authLicenseKey_.right(4)));
    }
}

void OrionAppController::copyActivityLog()
{
    auto* clipboard = QGuiApplication::clipboard();
    if (!clipboard) {
        return;
    }
    // Land the pending 200 ms batch on disk first so the copy includes the
    // seconds right before the click. drain() blocks only until the worker
    // finishes the tiny queued write; with a healthy disk that is
    // microseconds, and a wedged disk surfaces via the fallback below anyway.
    flushPendingLogs();
    // Prefer the on-disk engineer stream over the UI ring: the ring keeps
    // only the last kActivityRingMaxLines (1000) human events, which can
    // still be too little to show both the setup and the failure being
    // reported, and it excludes the periodic machine telemetry.
    // readLogTailForSharing() bounds the tail (256 KiB / 1500 lines, line-
    // aligned) and runs the same sharing redaction pass.
    const ActivityLogClipboardCopyResult copy = copyActivityLogToClipboard(
        clipboard, appLogSink_,
        orionDataDir(rootDir_) + QStringLiteral("/logs/orion_native.log"), logs_);
    if (copy.diskIncomplete) {
        appendLog(QStringLiteral("Partial activity log copied from the session ring; disk logging is incomplete."));
        return;
    }
    if (!copy.diskTail.isEmpty()) {
        appendLog(QStringLiteral("Activity log copied to clipboard "
                                 "(%1 lines from the disk log tail).")
                      .arg(copy.diskTail.count(QLatin1Char('\n')) + 1));
        return;
    }
    // Disk log absent/locked/empty (fresh install, exotic redirection): fall
    // back to the in-memory ring so the button never silently does nothing.
    appendLog(QStringLiteral("Activity log copied to clipboard "
                             "(%1 lines from the session ring; disk log unavailable).")
                  .arg(logs_.size()));
}

void OrionAppController::clearPendingActivationKey()
{
    if (pendingActivationKey_.isEmpty()) {
        return;
    }
    pendingActivationKey_.clear();
    emit pendingActivationKeyChanged();
}

void OrionAppController::applyActivationDeepLink(const QString& uri)
{
    // orion://activate?key=XXXX-... (QUrl parses "activate" as the host). Only the
    // activate action is recognized; anything else is logged and dropped.
    const QUrl url(uri.trimmed());
    if (url.scheme().compare(QLatin1String("orion"), Qt::CaseInsensitive) != 0) {
        return;
    }
    const QString action = url.host().isEmpty()
        ? url.path().remove(QLatin1Char('/')).toLower()
        : url.host().toLower();
    if (action != QLatin1String("activate")) {
        appendLog(QStringLiteral("Deep link ignored (unknown action: %1).").arg(action.left(32)));
        return;
    }
    const QString key = QUrlQuery(url).queryItemValue(QStringLiteral("key")).trimmed().toUpper();
    if (!security_.validateLicenseKeyFormat(key)) {
        appendLog(QStringLiteral("Activation deep link rejected: malformed key."));
        return;
    }
    if (authenticated_) {
        appendLog(QStringLiteral("Activation deep link ignored — already activated (key_suffix=%1).")
                      .arg(key.right(4)));
        return;
    }
    pendingActivationKey_ = key;
    appendLog(QStringLiteral("Activation key received via orion:// deep link (key_suffix=%1).")
                  .arg(key.right(4)));
    emit pendingActivationKeyChanged();
}

void OrionAppController::acceptLegalAgreement()
{
    auto data = config_.data();
    if (data.legalAcceptedVersion >= AppConfigData::kCurrentLegalVersion) {
        return;
    }
    data.legalAcceptedVersion = AppConfigData::kCurrentLegalVersion;
    saveConfigSilently(data);
    appendLog(QStringLiteral("Legal agreement + rules accepted (terms v%1).")
                  .arg(AppConfigData::kCurrentLegalVersion));
}

void OrionAppController::setVideoSource(const QString& value)
{
    if (isXboxRemotePlay(config_.data()))
        return; // Xbox always uses its explicit WGC target; preserve the PS5 source.
    const QString norm = actuationLeadSourceKey(value);
    auto data = config_.data();
    if (data.videoSource == norm) {
        return;
    }
    const RemotePlayState state = remotePlay_.state();
    if (!videoSourceChangeAllowed(state == RemotePlayState::Connecting,
                                  state == RemotePlayState::Running,
                                  remotePlayTeardownActive_ || remotePlay_.stopping()
                                      || remoteState_ == QLatin1String("Disconnecting"))) {
        appendLog(QStringLiteral("Video source change refused while Remote Play is active; disconnect first."));
        emit settingsChanged(); // restore the selector after a rejected UI edit
        return;
    }
    // A preview is logically Disconnected but still owns a live card and emits
    // frames. Retire it before committing another source and its timing lead.
    if (capturePreviewActive_ || remotePlay_.sidecarPid() != 0) {
        remotePlay_.stop(QStringLiteral("video_source_change"));
        clearRemotePreviewFrame();
    }
    // [ORION_LEAD_BY_SOURCE 2026-09-14] The route IS most of the lead (capture exposure+encode
    // vs network+decoder), so the Shot Lead travels with it: stash the live pair under the route
    // being left, restore the route being entered. A route that was never configured restores
    // "not configured" (0 / false) — the pre-existing behaviour, which is what the card prompts
    // on and what lets the measured seed run once there is authority — rather than silently
    // inheriting a number measured on the other pipe.
    const auto leadSwitch = switchActuationLeadVideoSource(data, norm);
    if (!saveConfigSilently(data)) {
        return;
    }
    // saveConfigSilently -> syncBackendConfig -> automation_.applyConfig, so the engine's
    // userActuationLeadMs follows this switch immediately, exactly as it does for
    // setActuationLeadMs. The engine's lead semantics are unchanged; only which number it gets.
    appendLog(QStringLiteral("Shot Lead: video source %1 -> %2, lead %3 -> %4")
                  .arg(leadSwitch.fromSource, leadSwitch.toSource,
                       actuationLeadDescription(leadSwitch.previousLeadMs,
                                                leadSwitch.previousUserSet),
                       actuationLeadDescription(leadSwitch.restoredLeadMs,
                                                leadSwitch.restoredUserSet)));
}

void OrionAppController::setCaptureCardIndex(int value)
{
    const RemotePlayState state = remotePlay_.state();
    if (!videoSourceChangeAllowed(state == RemotePlayState::Connecting,
                                   state == RemotePlayState::Running,
                                   remotePlayTeardownActive_ || remotePlay_.stopping()
                                       || remoteState_ == QLatin1String("Disconnecting"))) {
        appendLog(QStringLiteral("Capture device change refused while Remote Play is active; disconnect first."));
        emit settingsChanged();
        return;
    }
    value = std::clamp(value, 0, 16);
    if (value >= captureDeviceIds_.size() || captureDeviceIds_[value].isEmpty()) {
        appendLog(QStringLiteral("Capture device selection needs a fresh stable device identity; refresh the device list."));
        emit captureDevicesChanged();
        return;
    }
    auto data = config_.data();
    const QString chosenId = captureDeviceIds_[value];
    if (data.captureCardIndex == value && data.captureCardDeviceId == chosenId) {
        return;
    }
    // A disconnected warm preview still owns the previous card. Retire it and
    // invalidate its last image before making the new selection visible. Do not
    // reopen here: the old handle has an asynchronous release beat, and the next
    // explicit Preview or Connect will launch with this new stable identity.
    if (capturePreviewActive_ || remotePlay_.sidecarPid() != 0) {
        remotePlay_.stop(QStringLiteral("capture_device_change"));
        clearRemotePreviewFrame();
    }
    data.captureCardIndex = value;
    data.captureCardDeviceId = chosenId;
    if (saveConfigSilently(data))
        emit captureDevicesChanged();
}

void OrionAppController::setCaptureCardFps(int value)
{
    // [ORION_CAPTURE_FPS 2026-09-14] SNAP, do not clamp — see snappedCaptureCardFps. Persisting
    // the snapped value keeps stored == requested == what the card is actually asked for, so the
    // health line's requested-vs-negotiated comparison is meaningful.
    const int snapped = snappedCaptureCardFps(value);
    auto data = config_.data();
    if (data.captureCardFps == snapped) {
        return;
    }
    data.captureCardFps = snapped;
    if (!saveConfigSilently(data)) {
        return;
    }
    appendLog(QStringLiteral(
                  "Capture card refresh rate set to %1 fps (applies the next time you connect)")
                  .arg(snapped));
    if (snapped > 60) {
        // [ORION_CAPTURE_FPS_60_ONLY 2026-09-14 owner] The UI cannot reach this any more — it is
        // a settings.json / env value only. The Elgato HD60 X ACCEPTS a 1080p120 request and
        // delivers 60, and the sidecar then grades every shot's cadence against a frame interval
        // the card never produced: the owner's random earlies and lates ("i turned it off and i
        // went perfect from the field"). Say so at the moment it is set, not after a session.
        appendLog(QStringLiteral(
            "Capture card refresh rate %1 fps: most capture cards ACCEPT this and still deliver "
            "60 — check the Capture health line's uniqfps before trusting it. 60 Hz is the "
            "meter's native cadence.")
                      .arg(snapped));
    }
}

void OrionAppController::setHardwareDecode(bool value)
{
    auto data = config_.data();
    if (data.hardwareDecode == value) {
        return;
    }
    data.hardwareDecode = value;
    saveConfigSilently(data);
}

void OrionAppController::setControllerType(const QString& value)
{
    // X360 is the only supported virtual pad now (DS4 emulation was buggy). Pin it
    // regardless of the requested value so no stale "DS4" can ever be re-selected.
    Q_UNUSED(value);
    auto data = config_.data();
    if (data.controllerType == QLatin1String("X360")) {
        return;
    }
    data.controllerType = QStringLiteral("X360");
    saveConfigSilently(data);
}

void OrionAppController::setAutoReconnect(bool value)
{
    auto data = config_.data();
    if (data.autoReconnect == value) {
        return;
    }
    data.autoReconnect = value;
    saveConfigSilently(data);
}

void OrionAppController::setAutoMeterColor(bool value)
{
    auto data = config_.data();
    if (data.autoMeterColor == value) {
        return;
    }
    data.autoMeterColor = value;
    saveConfigSilently(data);
}

void OrionAppController::setDetectionConfidencePercent(int value)
{
    value = std::clamp(value, 0, 100);
    auto data = config_.data();
    if (data.detectionConfidencePercent == value) {
        return;
    }
    data.detectionConfidencePercent = value;
    saveConfigSilently(data);
}

QString OrionAppController::tempoInputPath() const
{
    return selectedTempoInputPath(config_.data());
}

void OrionAppController::setTempoInputPath(const QString& value)
{
    auto data = config_.data();
    if (!selectTempoInputPath(data, value) || sameTempoPathSettings(data, config_.data())) return;
    // Revoke the old route before publishing a different trigger selection.
    automation_.setArmed(false);
    disarmPreciseFire();
    neutralizeOwnedInput();
    saveConfigSilently(data);
    syncEngineArmed();
}

void OrionAppController::setTempoEnabled(bool value)
{
    auto data = config_.data();
    setTempoPathEnabled(data, value);
    if (sameTempoPathSettings(data, config_.data())) return;
    automation_.setArmed(false);
    disarmPreciseFire();
    neutralizeOwnedInput();
    saveConfigSilently(data);
    syncEngineArmed();
}

void OrionAppController::setNoDipEnabled(bool value)
{
    auto data = config_.data();
    if (data.noDipEnabled == value) {
        return;
    }
    data.noDipEnabled = value;
    saveConfigSilently(data);
}

void OrionAppController::setInputTimedEnabled(bool value)
{
    // [ORION_NO_METER_SHELVED 2026-09-15 owner] "Shelve the no meter path, we'll beef that up for
    // a later update." The refusal is back, and it is deliberately asymmetric: turning the mode
    // OFF is always allowed (an install that somehow arrived on the blind path can always get
    // back to the meter), turning it ON is refused while AppConfig::inputTimedAllowed() is false.
    // The UI that used to call this is unmounted, so in the shipped app the only callers left are
    // a deep link, a stale QML binding, or a test -- and the first two must not be able to put a
    // customer on a blind release. Tests lift the fence with setInputTimedAllowedForTesting().
    if (value && !AppConfig::inputTimedAllowed()) {
        appendLog(QStringLiteral(
            "NO METER: refused — the mode is shelved in this build (meter path only)."));
        return;
    }
    auto data = config_.data();
    if (data.inputTimedEnabled == value) return;
    // [ORION_MODE_EXCLUSIVITY 2026-09-14 owner] "switching between the two should cut off the
    // other". This IS the disarm the switch owes the console: setArmed(false) synchronously
    // fences the controller worker's release token and disarmPreciseFire() drops any copied
    // deadline, BEFORE the new mode's config reaches the engine. The engine then clears both
    // paths' transient release state on the config edge
    // (AutomationEngine::clearTransientReleaseStateForModeSwitch) and hands the console one
    // neutral pass-through tick, so no held virtual Square or tempo gather crosses the switch.
    automation_.setArmed(false);
    disarmPreciseFire();
    data.inputTimedEnabled = value;
    if (saveConfigSilently(data)) {
        // [2026-09-14 owner] was `inputTimedPaused_ = !value`, which is why a saved NO METER
        // selection "starts paused when the app launches" (the member also defaulted to true).
        // The Pause control is gone from the card, so nothing may set this except an explicit
        // setInputTimedPaused: selecting NO METER must mean NO METER is running.
        inputTimedPaused_ = false;
        emit settingsChanged();
    }
    syncEngineArmed();
}

void OrionAppController::setInputTimedPaused(bool value)
{
    if (inputTimedPaused_ == value) return;
    inputTimedPaused_ = value;
    // Uses the same synchronous revocation fence as route/auth disarming.
    syncEngineArmed();
    emit settingsChanged();
}

void OrionAppController::setInputTimedDelayMs(double value)
{
    if (!std::isfinite(value)) return;
    auto data = config_.data();
    value = std::clamp(value, 100.0, 2500.0);
    if (data.inputTimedDelayMs == value) return;
    if (data.inputTimedEnabled) {
        automation_.setArmed(false);
        disarmPreciseFire();
    }
    data.inputTimedDelayMs = value;
    saveConfigSilently(data);
    syncEngineArmed();
}

void OrionAppController::setInputTimedLeadMs(double value)
{
    if (!std::isfinite(value)) return;
    auto data = config_.data();
    value = std::clamp(value, 150.0, 400.0);
    if (data.inputTimedLeadMs == value) return;
    if (data.inputTimedEnabled) {
        automation_.setArmed(false);
        disarmPreciseFire();
    }
    data.inputTimedLeadMs = value;
    saveConfigSilently(data);
    syncEngineArmed();
}

void OrionAppController::setNoMeterHoldMs(double value)
{
    // [ORION_NO_METER_V2 2026-09-14 owner] THE blind-release control. Same shape as
    // setRhythmFlickDelayMs / setActuationLeadMs: reject non-finite, clamp into the persisted
    // band (QML re-binds on every settingsChanged, so an unguarded write would re-sign
    // settings.json on every repaint), then persist — saveConfigSilently() ->
    // syncBackendConfig() -> automation_.applyConfig() is what pushes it into the live engine.
    // The clamp is also a SAFETY property here: 500 ms is the bottom of the band precisely
    // because a shorter hold walks toward the pump-fake commit threshold.
    if (!std::isfinite(value)) {
        return;
    }
    const double v = qBound(AppConfigData::kNoMeterHoldMinMs, value,
                            AppConfigData::kNoMeterHoldMaxMs);
    auto data = config_.data();
    if (data.noMeterHoldMs == v) {
        return;
    }
    // A live NO METER arm must not reinterpret its copied deadline under a new hold.
    if (data.inputTimedEnabled) {
        automation_.setArmed(false);
        disarmPreciseFire();
    }
    data.noMeterHoldMs = v;
    if (saveConfigSilently(data)) {
        appendLog(QStringLiteral("No Meter release timing set to %1 ms").arg(v, 0, 'f', 0));
        // [ORION_BANNER_VERDICT_LIVE 2026-09-14] Restart the 10-shot count with the value.
        resetBannerTally();
    }
    syncEngineArmed();
}

void OrionAppController::setNoMeterFadeTrimMs(double value)
{
    // [ORION_NO_METER_FADE_TRIM 2026-09-14 owner] "fades need work". Same shape as
    // setNoMeterHoldMs: reject non-finite, clamp into the persisted band (QML re-binds on every
    // settingsChanged, so an unguarded write would re-sign settings.json on every repaint), then
    // persist. Unlike the hold this cannot reach the pump-fake floor on its own — the floor in
    // blindReleaseHold() still catches the sum — but the clamp keeps file, UI and env agreeing on
    // one band.
    if (!std::isfinite(value)) {
        return;
    }
    const double v = qBound(AppConfigData::kNoMeterFadeTrimMinMs, value,
                            AppConfigData::kNoMeterFadeTrimMaxMs);
    auto data = config_.data();
    if (data.noMeterFadeTrimMs == v) {
        return;
    }
    // A live NO METER arm must not reinterpret its copied deadline under a new trim.
    if (data.inputTimedEnabled) {
        automation_.setArmed(false);
        disarmPreciseFire();
    }
    data.noMeterFadeTrimMs = v;
    if (saveConfigSilently(data)) {
        appendLog(QStringLiteral("No Meter fade trim set to %1 ms").arg(v, 0, 'f', 0));
        // [ORION_BANNER_VERDICT_LIVE 2026-09-14] Restart the 10-shot count with the value.
        resetBannerTally();
    }
    syncEngineArmed();
}

void OrionAppController::setNoMeterVisionAssist(bool value)
{
    // [ORION_NO_METER_VISION_ASSIST 2026-09-14 owner] The pure-blind vs hybrid A/B switch. Same
    // shape as setNoMeterFadeTrimMs: no-op on an unchanged write (QML re-binds on every
    // settingsChanged, so an unguarded write would re-sign settings.json on every repaint), tear
    // a live NO METER arm down before the rules change under it, then persist —
    // saveConfigSilently() -> syncBackendConfig() -> automation_.applyConfig() is what pushes it
    // into the live engine.
    auto data = config_.data();
    if (data.noMeterVisionAssist == value) {
        return;
    }
    if (data.inputTimedEnabled) {
        automation_.setArmed(false);
        disarmPreciseFire();
    }
    data.noMeterVisionAssist = value;
    if (saveConfigSilently(data)) {
        appendLog(value
                      ? QStringLiteral("No Meter: vision owns the release when it sees the meter")
                      : QStringLiteral("No Meter: blind hold only (vision assist off)"));
        // [ORION_BANNER_VERDICT_LIVE 2026-09-14] Restart the 10-shot count with the new rule.
        resetBannerTally();
    }
    syncEngineArmed();
}

void OrionAppController::setInputTimedRhythmEnabled(bool value)
{
    auto data = config_.data();
    if (data.inputTimedRhythmEnabled == value) return;
    if (data.inputTimedEnabled) {
        automation_.setArmed(false);
        disarmPreciseFire();
    }
    data.inputTimedRhythmEnabled = value;
    saveConfigSilently(data);
    syncEngineArmed();
}

void OrionAppController::setNoMeterEnabled(bool value)
{
    // Pose-only timing is not a production authority source. A stale QML binding or
    // hand-edited config must not re-enable it at runtime.
    value = false;
    auto data = config_.data();
    if (data.noMeterEnabled == value && !(value && data.meterEnabled)) {
        return;
    }
    data.noMeterEnabled = value;
    if (value) {
        // Mutual exclusion with Meter.
        data.meterEnabled = false;
    }
    saveConfigSilently(data);
}

void OrionAppController::setNoMeterReleasePoint(const QString& value)
{
    const QString raw = value.trimmed().toLower();
    QString normalized = QStringLiteral("Push");
    if (raw.contains(QStringLiteral("jump"))) {
        normalized = QStringLiteral("Jump");
    } else if (raw.contains(QStringLiteral("set"))) {
        normalized = QStringLiteral("Set Point");
    } else if (raw.contains(QStringLiteral("release"))) {
        normalized = QStringLiteral("Release");
    }
    auto data = config_.data();
    if (data.noMeterReleasePoint == normalized) {
        return;
    }
    data.noMeterReleasePoint = normalized;
    saveConfigSilently(data);
}

void OrionAppController::setNoMeterBaseOffsetMs(double value)
{
    auto data = config_.data();
    data.noMeterBaseOffsetMs = qBound(-200.0, value, 200.0);  // widened 2026-08-08 — see AppConfig.cpp for rationale
    saveConfigSilently(data);
}

void OrionAppController::setNoMeterDecodeCompMs(double value)
{
    auto data = config_.data();
    data.noMeterDecodeCompMs = qBound(0.0, value, 40.0);
    saveConfigSilently(data);
}

void OrionAppController::setNoMeterConfidenceGate(double value)
{
    auto data = config_.data();
    data.noMeterConfidenceGate = qBound(0.50, value, 0.95);
    saveConfigSilently(data);
}

void OrionAppController::setNoMeterPushReleaseWindowMs(double value)
{
    auto data = config_.data();
    data.noMeterPushReleaseWindowMs = qBound(100.0, value, 400.0);
    saveConfigSilently(data);
}

void OrionAppController::setNoMeterHandedness(const QString& value)
{
    const QString normalized = value.trimmed().toLower().startsWith(QLatin1Char('l'))
        ? QStringLiteral("Left") : QStringLiteral("Right");
    auto data = config_.data();
    if (data.noMeterHandedness == normalized) {
        return;
    }
    data.noMeterHandedness = normalized;
    saveConfigSilently(data);
}

void OrionAppController::setShowSkeleton(bool value)
{
    auto data = config_.data();
    if (data.showSkeleton == value) {
        return;
    }
    data.showSkeleton = value;
    saveConfigSilently(data);
}

void OrionAppController::setShowLiveMeterMetrics(bool value)
{
    auto data = config_.data();
    if (data.showLiveMeterMetrics == value) {
        return;
    }
    data.showLiveMeterMetrics = value;
    saveConfigSilently(data);
}

void OrionAppController::setMinHoldMs(double value)
{
    auto data = config_.data();
    data.minimumHoldMs = qBound(0.0, value, data.maximumHoldMs);
    saveConfigSilently(data);
}

void OrionAppController::setMaxHoldMs(double value)
{
    auto data = config_.data();
    data.maximumHoldMs = qBound(qMax(50.0, data.minimumHoldMs), value, 5000.0);
    saveConfigSilently(data);
}

void OrionAppController::setStableFrames(int value)
{
    auto data = config_.data();
    data.stableFrames = qBound(1, value, 10);
    saveConfigSilently(data);
}

bool OrionAppController::localDevAllowed() const
{
#ifdef ORION_PRODUCTION_BUILD
    // Production: never. On-disk markers and env vars are attacker-controlled.
    return false;
#else
    return qEnvironmentVariableIsSet("ORION_LOCAL_UI_TEST")
        || QFileInfo::exists(rootDir_ + QStringLiteral("/nexus-server/nexus_server.db"))
        || QFileInfo::exists(rootDir_ + QStringLiteral("/native_orion/CMakeLists.txt"));
#endif
}

bool OrionAppController::automationSecurityAllowed() const noexcept
{
#ifdef ORION_PRODUCTION_BUILD
    constexpr bool localDevBypass = false;
#else
    // licenseState_ reaches Local Dev only after authenticate() validates the
    // source-tree marker. Do not re-read security_policy.json and stat marker
    // files on every 4 ms input tick. The last complete security verdict remains
    // authoritative, and any lock (including evaluator failure) wins.
    const bool localDevBypass = AutomationAccessPolicy::localDevBypassEligible(
        licenseState_ == QLatin1String("Local Dev"),
        securityLockActive_, securityReleaseManifestRequired_);
#endif
    // Both layers are required: launcher authentication establishes the user
    // session; the short signed lease keeps that session revocable. Production
    // compiles the lease gate on, so neither an unset nor a falsy environment
    // variable can turn this into an offline unlock.
    return AutomationAccessPolicy::allowed(
        authenticated_, localDevBypass, securityLockActive_,
        leaseGate_.enabled(), leaseGate_.fireAllowed());
}

QString OrionAppController::stateText(RemotePlayState state) const
{
    switch (state) {
    case RemotePlayState::Disconnected:
        return QStringLiteral("Disconnected");
    case RemotePlayState::Connecting:
        return QStringLiteral("Connecting");
    case RemotePlayState::Running:
        return QStringLiteral("Running");
    case RemotePlayState::Error:
        return QStringLiteral("Error");
    }
    return QStringLiteral("Unknown");
}

QString OrionAppController::holdStateText(HoldState state) const
{
    switch (state) {
    case HoldState::Idle:
        return QStringLiteral("Idle");
    case HoldState::Armed:
        return QStringLiteral("Armed");
    case HoldState::Holding:
        return QStringLiteral("Holding");
    case HoldState::GreenWindow:
        return QStringLiteral("Green Window");
    case HoldState::Releasing:
        return QStringLiteral("Releasing");
    case HoldState::PumpFake:
        return QStringLiteral("Pump Fake");
    case HoldState::Cooldown:
        return QStringLiteral("Cooldown");
    }
    return QStringLiteral("Unknown");
}

QString OrionAppController::holdStateToken(HoldState state)
{
    switch (state) {
    case HoldState::Idle:
        return QStringLiteral("Idle");
    case HoldState::Armed:
        return QStringLiteral("Armed");
    case HoldState::Holding:
        return QStringLiteral("Holding");
    case HoldState::GreenWindow:
        return QStringLiteral("GreenWindow");
    case HoldState::Releasing:
        return QStringLiteral("Releasing");
    case HoldState::PumpFake:
        return QStringLiteral("PumpFake");
    case HoldState::Cooldown:
        return QStringLiteral("Cooldown");
    }
    return QStringLiteral("Unknown");
}

QString OrionAppController::shotModeToken(ShotMode mode)
{
    switch (mode) {
    case ShotMode::TempoSquare:
        return QStringLiteral("TempoSquare");
    case ShotMode::TempoStick:
        return QStringLiteral("TempoStick");
    case ShotMode::GoToStick:
        return QStringLiteral("GoToStick");
    case ShotMode::ButtonShot:
        return QStringLiteral("ButtonShot");
    }
    return QStringLiteral("Unknown");
}

QString OrionAppController::telemetryToken(const QString& raw)
{
    const QString s = raw.simplified();
    if (s.isEmpty()) {
        return QStringLiteral("unknown");
    }
    QString out = s;
    out.replace(QChar(' '), QChar('_'));
    return out;
}

void OrionAppController::clearMeterMetrics(bool forceNotify)
{
    const bool changed = meterMetricsCurrent_ || measuredMeterAtMs_ != 0
        || measuredMeterFillPct_ >= 0.0 || measuredMeterConfidence_ >= 0.0
        || measuredEtaArmToken_ != 0 || measuredEtaAtObservationMs_ >= 0.0
        || meterFillLine_ != QLatin1String("--")
        || meterTargetLine_ != QLatin1String("--");
    meterMetricsExpiryTimer_.stop();
    meterMetricsCurrent_ = false;
    measuredMeterAtMs_ = 0;
    measuredMeterFillPct_ = -1.0;
    measuredMeterConfidence_ = -1.0;
    measuredMeterVelocityPctS_ = 0.0;
    measuredEtaSerial_ = 0;
    measuredEtaArmToken_ = 0;
    measuredEtaTargetPct_ = -1.0;
    measuredEtaAtObservationMs_ = -1.0;
    meterFillLine_ = QStringLiteral("--");
    meterTargetLine_ = QStringLiteral("--");
    if (changed || forceNotify) {
        emit meterMetricsChanged();
    }
}

void OrionAppController::armMeterMetricsExpiry()
{
    if (!meterMetricsCurrent_ || measuredMeterAtMs_ <= 0) {
        meterMetricsExpiryTimer_.stop();
        return;
    }
    meterMetricsExpiryTimer_.start(static_cast<int>(kMeterConfirmFreshMs_));
}

void OrionAppController::refreshMeterTargetEtaSnapshot()
{
    if (!meterMetricsValid() || measuredEtaSerial_ == measuredMeterSerial_
        || shot_.armToken == 0 || shot_.armToken != botOwnershipArmToken_
        || !shot_.lastSampleGenuineAccept || shot_.lastDetectionMs <= 0.0
        || !std::isfinite(shot_.targetPct) || shot_.targetPct <= 0.0
        || !std::isfinite(shot_.releaseCrossingEtaMs)
        || shot_.releaseCrossingEtaMs < 0.0) {
        return;
    }

    // processHolding records releaseCrossingEtaMs as crossing-now for the exact
    // active target used on this input tick. Convert it back to the detector
    // observation instant once, then the getter subtracts the wall-clock metric
    // age. No green-centre or registration ETA is accepted as a substitute.
    const double engineNowMs = automation_.engineNowMs();
    const double engineMetricAgeMs = engineNowMs - shot_.lastDetectionMs;
    if (!std::isfinite(engineMetricAgeMs) || engineMetricAgeMs < 0.0
        || engineMetricAgeMs >= static_cast<double>(kMeterConfirmFreshMs_)) {
        return;
    }
    measuredEtaSerial_ = measuredMeterSerial_;
    measuredEtaArmToken_ = shot_.armToken;
    measuredEtaTargetPct_ = shot_.targetPct;
    measuredEtaAtObservationMs_ = shot_.releaseCrossingEtaMs + engineMetricAgeMs;
    meterTargetLine_ = QStringLiteral("%1%").arg(measuredEtaTargetPct_, 0, 'f', 1);
    if (meterMetricsCadence_.takeEta(QDateTime::currentMSecsSinceEpoch())) {
        emit meterMetricsChanged();
    }
}

namespace {

// Mirrors OrionAppController::HudLine::kAbsentKey (a private nested type these
// free helpers cannot name). Both are INT_MIN; a quantised value can never
// collide with it because hudQuantise clamps to +/-1e9.
constexpr int kHudAbsentKey = std::numeric_limits<int>::min();

// Quantise a value to the digits a HUD row renders, so an unchanged readout
// never reformats. Absent/non-finite collapses to the hidden-row key.
[[nodiscard]] int hudQuantise(double value, double scale) noexcept
{
    if (!std::isfinite(value)) {
        return kHudAbsentKey;
    }
    const double scaled = std::round(value * scale);
    // Clamp instead of overflowing int: an absurd value still renders, it just
    // stops distinguishing neighbours it could never be read apart from anyway.
    constexpr double kLimit = 1.0e9;
    return static_cast<int>(std::clamp(scaled, -kLimit, kLimit));
}

} // namespace

void OrionAppController::refreshLiveMeterTelemetry()
{
    // There is no visibility toggle: the overlay is part of the product, not a
    // diagnostic. Its cost is bounded instead — three values, published at 30 Hz,
    // and nothing at all is computed on a tick with no meter and no shot.
    //
    // PERSISTENCE: losing the shot demotes the values to the "--" placeholder;
    // it never empties them. The overlay is a fixed on-screen instrument whose
    // needles fall back to rest, not a panel that appears and vanishes.
    //
    // TWO GATES (2026-08-04 "the box shows no live values"). The rows are not
    // all provable at the same moment, so they no longer share one gate:
    //
    //   `measured` — the most recent detector payload was a genuine fresh raw
    //     frame. meterMetricsValid() is the established truth boundary for that,
    //     and it is the SAME freshness window the page uses to draw the lock box
    //     (kMeterConfirmFreshMs_). FILL is a camera reading, so this is all it
    //     needs; reusing meterMetricsValid() still keeps the engine's internally
    //     coasted estimate from ever being presented as a measurement.
    //
    //   `live` — measured AND the bot owns a shot. TIP and FIRE come from
    //     processHolding's ShotContext and simply do not exist outside an owned
    //     shot, so they stay at "--" rather than being synthesised from fill and
    //     velocity (that would be a second, non-authoritative predictor sitting
    //     next to the meter claiming to be the decision the app actually made).
    //
    // Measured live: the bot owns ~180 ms of a ~2500 ms meter (2026-08-04 batch,
    // Holding->Cooldown 12:34:12.820-.997 against a meter visible 12:34:12-15),
    // so gating FILL on ownership left all three rows reading "--" for ~93% of
    // the time the box was on screen. That is the reported defect, and the fix
    // belongs here rather than in the page's visibility gate: the box is correct
    // to follow the meter, the values were wrong to follow the shot.
    //
    // Everything below reads the ShotContext copied earlier in THIS input tick,
    // so all rows share one instant.
    const bool owned = shot_.armToken != 0
        && shot_.state != HoldState::Idle && shot_.state != HoldState::Cooldown;
    const bool measured = meterMetricsValid();
    const bool live = owned && measured;

    // Steady no-meter, no-shot state: every row is already at the placeholder
    // (HudLine starts there at construction), so there is nothing left to do on
    // those ticks. This early return is the whole reason the overlay costs
    // nothing while there is no meter on screen.
    const bool fillPublished = hudFill_.key != HudLine::kAbsentKey;
    // TIP/FIRE are included in this "is anything still on screen?" test because the
    // readability hold below can leave them showing digits after the meter itself is
    // gone. Without them here, the early return would fire on the very ticks that
    // are supposed to retire those digits and they would stay frozen on screen until
    // the next meter appeared — the stale-sticker failure this overlay was rebuilt
    // to eliminate.
    const bool shotRowsPublished = hudTip_.key != HudLine::kAbsentKey
        || hudFire_.key != HudLine::kAbsentKey;
    if (!measured && !liveMeter_.valid && !fillPublished && !shotRowsPublished) {
        return;
    }

    // A LOSS is a rare edge, not a cadence event: it is published immediately
    // (never through the throttle) so the overlay cannot linger on digits that
    // are no longer being proven — neither the last shot's deadline nor the last
    // frame's fill. The cadence is deliberately left untouched so the NEXT
    // shot's first row is not held back behind a stale interval.
    //
    // Rows demote to "--", NOT to an empty string. An empty string used to
    // collapse the row, which collapsed the box, which is exactly why the
    // readout looked like it flashed up for one shot and then disappeared.
    const bool losing = (liveMeter_.valid && !live) || (fillPublished && !measured);
    if (!losing && !meterHudCadence_.take(QDateTime::currentMSecsSinceEpoch())) {
        return;
    }

    LiveMeterTelemetry next;
    next.valid = live;

    // --- fill % (genuine raw detector measurement) -----------------------
    // Detector-gated only. This is the row that is on for the meter's whole
    // life, and it is also the ground truth the other two are judged against.
    if (measured) {
        next.fillPct = measuredMeterFillPct_;
    }

    const double engineNowMs = automation_.engineNowMs();

    if (live) {
        // --- tip ETA: the canonical decision, stamped this tick --------------
        // processAutonomousLiveMeterHolding writes releaseCrossingEtaMs as
        // (tipAbsMs - now) on the same tick that produced this ShotContext, so no
        // second age correction is owed here and none is invented.
        if (std::isfinite(shot_.releaseCrossingEtaMs) && shot_.releaseCrossingEtaMs >= 0.0) {
            next.tipEtaMs = shot_.releaseCrossingEtaMs;
        }

        // --- command deadline: absolute fireAtMs, so always current ----------
        // Negative is kept, not clamped: "the deadline is already 14 ms in the past"
        // is the single most important thing this overlay can say.
        if (std::isfinite(shot_.tipPredictionDeadlineMs) && shot_.tipPredictionDeadlineMs > 0.0) {
            next.commandEtaMs = shot_.tipPredictionDeadlineMs - engineNowMs;
            next.commandEtaValid = true;
        }
    }

    // --- FIRE prefers the ARMED schedule (2026-08-06 owner request) ----------
    // While a precise-fire token is actually armed, the scheduler's own
    // absolute deadline is the countdown the app will act on, so it supersedes
    // the tick-stamped prediction above — including on the rare tick where the
    // detector sample goes stale mid-arm, so an armed countdown is never
    // demoted to "--" while something is genuinely armed. Pure display reads
    // of two existing engine accessors — the exact pair shotEtaMs() already
    // reads on this thread; nothing new is computed on the engine side.
    const double schedDeadlineMs = automation_.scheduledFireDeadlineMs();
    if (automation_.scheduledFireToken() != 0
        && std::isfinite(schedDeadlineMs) && schedDeadlineMs > 0.0) {
        next.commandEtaMs = schedDeadlineMs - engineNowMs;
        next.commandEtaValid = true;
        next.commandScheduled = true;
    }

    // The remaining LiveMeterTelemetry fields (rise, lead, sigma, predictor
    // source, schedule, reservation) are intentionally left at their sentinels.
    // The overlay is three rows and always on, so sampling values it will not
    // draw would be pure GUI-thread cost on every shot for every user. The
    // struct keeps the fields so re-adding a row stays a one-line change.

    // Tone changes are publishable events in their own right. `meterHudLive`
    // and `meterHudCommandLate` are read by QML for opacity and for the FIRE
    // row's colour, and neither is implied by a digit change: a shot can end
    // with the fill row's rendered percent unchanged, which would otherwise
    // leave the box lit and FIRE green over a shot that is already over.
    const bool toneChanged = liveMeter_.valid != next.valid
        || (liveMeter_.fillPct >= 0.0) != (next.fillPct >= 0.0)
        || (liveMeter_.commandEtaValid && liveMeter_.commandEtaMs < 0.0)
            != (next.commandEtaValid && next.commandEtaMs < 0.0);
    liveMeter_ = next;

    bool changed = toneChanged;

    // VALUE ONLY. The FILL/TIP/FIRE captions are constant Text items in QML, so
    // they are never re-published, never re-allocated and never re-laid-out.
    //
    // Fill is quantised to WHOLE percent (scale 1.0, was 0.1%): a tenth of a
    // percent is unreadable on a live overlay and it forced a string rebuild on
    // essentially every 30 Hz tick. Whole percent cuts those rebuilds ~10x.
    //
    // The `measured` branch is load-bearing: the unavailable sentinel is -1.0,
    // which quantises to a perfectly valid key (-1), so testing the value alone
    // would publish "FILL -1%" the moment the detector went quiet.
    const int fillKey = measured
        ? hudQuantise(liveMeter_.fillPct, 1.0) : HudLine::kAbsentKey;
    if (hudFill_.stale(fillKey)) {
        changed = true;
        hudFill_.text = fillKey == HudLine::kAbsentKey
            ? QStringLiteral("--")
            : QStringLiteral("%1%").arg(liveMeter_.fillPct, 0, 'f', 0);
    }

    // READABILITY HOLD (2026-08-06). A shot is owned for ~180 ms of a ~2500 ms
    // meter, so TIP/FIRE were painted and erased faster than they could be read —
    // reported as "no live tip/fire values" on days of purely bot-driven shots.
    // While a shot is owned the rows update every tick exactly as before; when it
    // ends they KEEP THAT SHOT'S DIGITS for a beat instead of blanking, then
    // demote normally. Nothing is extrapolated: these are the engine's own
    // published numbers for the shot that just happened, held still long enough
    // to be legible. A countdown is deliberately NOT continued past the shot —
    // that would be this file inventing a prediction the engine never made.
    constexpr qint64 kHudShotHoldMs = 1500;
    const qint64 nowWallMs = QDateTime::currentMSecsSinceEpoch();
    const bool tipFresh = liveMeter_.tipEtaMs >= 0.0;
    const bool fireFresh = liveMeter_.commandEtaValid;
    if (tipFresh || fireFresh) {
        hudShotHoldUntilMs_ = nowWallMs + kHudShotHoldMs;
    }
    const bool holding = nowWallMs < hudShotHoldUntilMs_;

    // Holding reuses the row's CURRENT key, so stale() reports "not stale" and the
    // existing text survives untouched — the hold cannot invent a key of its own.
    const int tipKey = tipFresh ? hudQuantise(liveMeter_.tipEtaMs, 1.0)
                       : holding ? hudTip_.key
                                 : HudLine::kAbsentKey;
    if (hudTip_.stale(tipKey)) {
        changed = true;
        hudTip_.text = tipKey == HudLine::kAbsentKey
            ? QStringLiteral("--")
            : QStringLiteral("%1ms").arg(liveMeter_.tipEtaMs, 0, 'f', 0);
    }

    const int fireKey = fireFresh ? hudQuantise(liveMeter_.commandEtaMs, 1.0)
                        : holding ? hudFire_.key
                                  : HudLine::kAbsentKey;
    if (hudFire_.stale(fireKey)) {
        changed = true;
        hudFire_.text = fireKey == HudLine::kAbsentKey
            ? QStringLiteral("--")
            : QStringLiteral("%1ms").arg(liveMeter_.commandEtaMs, 0, 'f', 0);
    }

    // COURT/JITTER publishing removed 2026-08-06 (owner: three values only —
    // FILL/TIP/FIRE, the bot's own decision chain — "and make it actually LIVE
    // and not static"). The two network rows were the static feel: COURT was a
    // string that never changed for this topology's whole life and JITTER
    // moved a tenth of a ms at a time, so 2 of 5 rows were permanently frozen
    // next to a FILL that streams. The three surviving rows are already as
    // live as the truth allows — FILL re-publishes with every fresh detector
    // frame at the 30 Hz cadence, TIP is re-stamped (tipAbs - now) on every
    // engine tick while the bot owns the shot, and FIRE above is recomputed
    // against engineNowMs() on every pass, so an armed token reads as a real
    // countdown rather than a value latched at arm time.

    if (changed) {
        emit meterHudChanged();
    }
}

void OrionAppController::observeDetectorHealth(const QJsonObject& health)
{
    // One compact line: "<proposer> · <infer> ms · <state> · locks N · <refused|drops> N".
    // Every field is optional so an older sidecar (or a reader without the snapshot)
    // degrades to fewer segments rather than to a blank card.
    const QString provider = health.value(QStringLiteral("provider")).toString().trimmed();
    QString label;
    if (provider.isEmpty() || provider == QLatin1String("none")) {
        label = QStringLiteral("No detector");
    } else if (provider == QLatin1String("cv-contour")) {
        label = QStringLiteral("Pure CV");
    } else {
        // ONNX Runtime provider ids: DmlExecutionProvider / CUDAExecutionProvider /
        // CPUExecutionProvider / ... -> "YOLO · DirectML" etc.
        QString backend = provider;
        backend.remove(QStringLiteral("ExecutionProvider"));
        if (backend.compare(QLatin1String("Dml"), Qt::CaseInsensitive) == 0) {
            backend = QStringLiteral("DirectML");
        }
        label = backend.isEmpty() ? QStringLiteral("YOLO")
                                  : QStringLiteral("YOLO · %1").arg(backend);
    }
    QStringList parts{label};
    if (health.contains(QStringLiteral("infer_ms"))) {
        parts.append(QStringLiteral("%1 ms")
                         .arg(health.value(QStringLiteral("infer_ms")).toDouble(), 0, 'f', 1));
    }
    const QString state = health.value(QStringLiteral("state")).toString().trimmed();
    if (!state.isEmpty() && state != QLatin1String("-")) {
        parts.append(state);
    }
    if (health.contains(QStringLiteral("locks"))) {
        parts.append(QStringLiteral("locks %1").arg(health.value(QStringLiteral("locks")).toInt()));
    }
    if (provider == QLatin1String("cv-contour")) {
        // The CV locator's own gate refusals (no green tip / no white outline) are the
        // number that says whether it is seeing THIS meter; YOLO has no equivalent, so
        // it shows the reader's drop count instead.
        const int refused = health.value(QStringLiteral("cv_no_tip")).toInt()
            + health.value(QStringLiteral("cv_no_outline")).toInt();
        parts.append(QStringLiteral("refused %1").arg(refused));
    } else if (health.contains(QStringLiteral("drops"))) {
        parts.append(QStringLiteral("drops %1").arg(health.value(QStringLiteral("drops")).toInt()));
    }
    const QString line = parts.join(QStringLiteral(" · "));
    if (line == detectorHealthLine_ && provider == detectorProvider_) {
        return;
    }
    detectorHealthLine_ = line;
    detectorProvider_ = provider;
    emit detectorHealthChanged();
}

void OrionAppController::observeMeterBlindness(quint64 physicalShotEpoch,
                                               bool genuineRawDetection)
{
    const bool was = meterBlindWarning_;

    // [ORION_MODE_EXCLUSIVITY 2026-09-14 owner] NO METER releases on a timer and looks at
    // nothing. "No meter was detected for this shot" is the DESIGN there, not a fault, so the
    // advisor is silent for the whole mode and its streak is reset — otherwise the first press
    // back on the meter path inherits a streak earned while the detector was not being asked a
    // question, and the customer feed shows a detector warning for a mode with no detector.
    if (config_.data().inputTimedEnabled) {
        meterBlindLatch_.reset();
        meterBlindWarning_ = false;
        if (detectionUnavailable_) {
            detectionUnavailable_ = false;
            automation_.setDetectionUnavailable(false);
        }
        if (was) {
            emit meterBlindChanged();
        }
        return;
    }

    // [RT-MED-04 / CL3-F8-009 2026-09-23] A genuine raw detection is evidence about the SHOT meter
    // only while a press is in flight (physicalShotEpoch = the engine's active press epoch, owned
    // or still pending). An idle sighting between shots -- a false lock, a replay, a HUD element --
    // used to reset the streak here and could starve the trip forever; it is now ignored. The
    // STREAK is fed by observeMeterPressUnanswered(), not by frames: in METER mode a press the
    // detector never sees never begins a shot, so no frame-side epoch ever moved (CL3-F4-007).
    if (genuineRawDetection) {
        meterBlindLatch_.observeRawDetection(physicalShotEpoch);
        // While latched the customer keeps seeing why the bot is not timing shots: one in-press
        // blip clears the STREAK but not the warning ([CL2-P9-001 r2, Codex]).
        meterBlindWarning_ = meterBlindLatch_.warning();
        if (was != meterBlindWarning_) {
            appendLog(QStringLiteral("Meter detection recovered."));
            emit meterBlindChanged();
        }
    }
}

void OrionAppController::observeMeterPressUnanswered(quint64 physicalShotEpoch, double holdMs)
{
    if (config_.data().inputTimedEnabled) {
        return;   // NO METER: the detector was never asked (see observeMeterBlindness)
    }
    applyMeterBlindLatchEvent(meterBlindLatch_.observeUnansweredPress(physicalShotEpoch, holdMs));
}

void OrionAppController::applyMeterBlindLatchEvent(MeterBlindnessLatch::Event event)
{
    const bool was = meterBlindWarning_;
    if (event == MeterBlindnessLatch::Event::Tripped && !detectionUnavailable_) {
        detectionUnavailable_ = true;
        automation_.setDetectionUnavailable(true);
        // [RT-MED-04 2026-09-23] Reworded: in METER mode the blind backstop is already held off
        // by greenWindowPriority, so "blind timer shots are OFF" described nothing that changed.
        appendLog(QStringLiteral(
            "DETECTION UNAVAILABLE: %1 shot presses in a row got no meter - Venice is not timing "
            "shots until %2 shots with a visible meter prove detection is back.")
                      .arg(meterBlindLatch_.streak())
                      .arg(MeterBlindnessLatch::kRecoveryOwnedShots));
    } else if (event == MeterBlindnessLatch::Event::Recovered && detectionUnavailable_) {
        detectionUnavailable_ = false;
        automation_.setDetectionUnavailable(false);
        appendLog(QStringLiteral("DETECTION RESTORED: %1 shots with a visible meter.")
                      .arg(MeterBlindnessLatch::kRecoveryOwnedShots));
    }
    // [CL2-P9-001 r2] The customer warning follows the LATCH, not just the streak.
    meterBlindWarning_ = meterBlindLatch_.warning();
    if (was != meterBlindWarning_) {
        // [2026-09-14 owner] The prefix was "NO METER:", which is now the name of a whole timing
        // MODE and the engine's own blind-release log tag — one grep, two unrelated meanings.
        appendLog(meterBlindWarning_
                      ? QStringLiteral("METER BLIND: %1 shots with no meter detected — %2")
                            .arg(meterBlindLatch_.streak())
                            .arg(meterBlindHint())
                      : QStringLiteral("Meter detection recovered."));
        emit meterBlindChanged();
    }
}

void OrionAppController::observeBotOwnership(const ShotContext& context)
{
    const bool takeoverState = context.state == HoldState::Armed
        || context.state == HoldState::Holding
        || context.state == HoldState::GreenWindow;
    // [CL2-P9-001 2026-09-23] Count distinct OWNED meter shots; two of them prove the meter is
    // readable again. [RT-MED-04 / CL3-F4-007 2026-09-23] "Owned" now means VISION-owned: the shot
    // genuinely saw and accepted its meter (meterSeenThisShot). A bare Holding state is not proof --
    // a Go-To push enters Holding with release_reason=await_meter and no meter at all, and two of
    // those used to clear the latch.
    if (!context.inputTimedShot && context.meterSeenThisShot
        && (context.state == HoldState::Holding || context.state == HoldState::GreenWindow
            || context.state == HoldState::Releasing)
        && context.physicalShotEpoch != 0) {
        applyMeterBlindLatchEvent(meterBlindLatch_.observeVisionOwnedShot(context.physicalShotEpoch));
    }
    bool changed = false;

    if (context.armToken == 0) {
        changed = botOwnershipArmToken_ != 0 || botOwnershipStartedMs_ >= 0.0
            || botOwnershipEndedMs_ >= 0.0;
        botOwnershipArmToken_ = 0;
        botOwnershipStartedMs_ = -1.0;
        botOwnershipEndedMs_ = -1.0;
    } else if (context.armToken != botOwnershipArmToken_) {
        // The synchronous shotStateChanged connection observes beginShot's first
        // committed owned state before process() returns its forced output. This
        // is the real native takeover edge, after physical intent debounce.
        if (takeoverState) {
            botOwnershipArmToken_ = context.armToken;
            botOwnershipStartedMs_ = automation_.engineNowMs();
            botOwnershipEndedMs_ = -1.0;
            changed = true;
        }
    } else if (context.releaseTriggerMs > 0.0
               && botOwnershipEndedMs_ != context.releaseTriggerMs) {
        // Local release-submit boundary only. There is no per-shot console ACK,
        // so the HUD must never imply end-to-end delivery confirmation here.
        botOwnershipEndedMs_ = context.releaseTriggerMs;
        changed = true;
    }

    if (changed) {
        emit meterMetricsChanged();
    }
}

bool OrionAppController::meterMetricsValid() const noexcept
{
    return live_hud::meterMetricFresh(
        meterMetricsCurrent_, measuredMeterAtMs_,
        QDateTime::currentMSecsSinceEpoch(), kMeterConfirmFreshMs_);
}

bool OrionAppController::tickPhaseVerified() const noexcept
{
    if (!telemetry_.rttTargetVerified || !telemetry_.tickPhaseVerified
        || telemetry_.tickPhaseConfidence < 0.55
        || telemetry_.tickPhaseObservedEpochMs <= 0) {
        return false;
    }
    const qint64 ageMs = QDateTime::currentMSecsSinceEpoch()
        - telemetry_.tickPhaseObservedEpochMs;
    return ageMs >= 0 && ageMs <= 250;
}

double OrionAppController::tickerLatencyMs() const noexcept
{
    return tickPhaseVerified() ? telemetry_.tickerLatencyMs : 0.0;
}

double OrionAppController::shotHoldMs() const noexcept
{
    return live_hud::botOwnedDurationMs(
        shot_.armToken, botOwnershipArmToken_, botOwnershipStartedMs_,
        botOwnershipEndedMs_, automation_.engineNowMs());
}

double OrionAppController::shotEtaMs() const noexcept
{
    const bool commandPending = shot_.state == HoldState::Armed
        || shot_.state == HoldState::Holding
        || shot_.state == HoldState::GreenWindow;
    const double deadlineMs = automation_.scheduledFireDeadlineMs();
    if (!meterMetricsValid() || !commandPending
        || automation_.scheduledFireToken() == 0
        || !std::isfinite(deadlineMs) || deadlineMs < 0.0) {
        return -1.0;
    }
    return std::max(0.0, deadlineMs - automation_.engineNowMs());
}

double OrionAppController::shotEtaToTargetMs() const noexcept
{
    return live_hud::matchingActiveTargetEtaMs(
        meterMetricsCurrent_, measuredMeterAtMs_,
        QDateTime::currentMSecsSinceEpoch(), kMeterConfirmFreshMs_,
        shot_.armToken, measuredEtaArmToken_, shot_.targetPct,
        measuredEtaTargetPct_, measuredEtaAtObservationMs_);
}

void OrionAppController::logShotStateTransition(const ControllerState& physical,
                                                const ControllerState& output)
{
    if (shot_.state == prevShotState_) {
        return;
    }
    // A physical Square press that produces NO "Idle -> Armed" here (no later
    // "Release issued") is a pass-through / missed arm — the bot never took
    // ownership. A full Idle->Armed->Holding->Releasing chain with the paired
    // "Release ownership: out_cleared_all=1" is a clean bot-owned shot.
    appendLog(QStringLiteral("Shot state: %1 -> %2 mode=%3 seq=%4 phys_sq=%5 out_sq=%6 "
                             "src=%7 kind=%8")
                  .arg(holdStateToken(prevShotState_))
                  .arg(holdStateToken(shot_.state))
                  .arg(shotModeToken(shot_.mode))
                  .arg(shot_.releaseSeq)
                  .arg(physical.square() ? 1 : 0)
                  .arg(output.square() ? 1 : 0)
                  .arg(telemetryToken(controllerInputSource_))
                  .arg(telemetryToken(activePhysicalDeviceKind_)));
    prevShotState_ = shot_.state;
}

void OrionAppController::updateReleaseOwnershipTrace(const ControllerState& physical,
                                                     const ControllerState& output,
                                                     bool submitOk, qint64 nowMs)
{
    const bool inWindow = (shot_.state == HoldState::Releasing
                           || shot_.state == HoldState::Cooldown);
    if (!inWindow) {
        // Left the release window (cooldown completed -> Idle). Emit the summary now;
        // this is the normal flush path and fires the tick the engine returns to Idle.
        if (ownSeq_ >= 0) {
            flushReleaseOwnershipTrace();
        }
        return;
    }

    const int seq = shot_.releaseSeq;
    const bool physSq = physical.square();
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] Subtract a stick-click steal from the ownership trace.
    // out_cleared_all=1 below is asserted to prove Orion held the output Square at 0 for the whole
    // pulse+cooldown, and the comment at flushReleaseOwnershipTrace calls any 0 there "a real
    // engine override bug". Square passthrough deliberately puts X on the output when R3 is held,
    // so a steal during a live shot's Releasing window would fabricate precisely that signature and
    // send us hunting an engine bug that does not exist. Read the engine's own injection latch
    // rather than re-deriving the gate here, so the two cannot disagree.
    const bool outSq = output.square() && !automation_.squarePassthroughInjectedLastTick();
    const QString backend = directPipeOwnsInput_
        ? QStringLiteral("PIPE")
        : (controller_.isDs4Backend() ? QStringLiteral("DS4")
                                      : QStringLiteral("XUSB"));

    if (ownSeq_ != seq) {
        // A new release window started. Flush any stale prior window first (defensive;
        // the out-of-window path normally already did), then initialise accumulators.
        if (ownSeq_ >= 0) {
            flushReleaseOwnershipTrace();
        }
        ownSeq_ = seq;
        ownStartMs_ = nowMs;
        ownLastTms_ = 0;
        ownTicks_ = 0;
        ownPhysHeldAll_ = true;
        ownOutClearedAll_ = true;
        ownMaxOutSq_ = false;
        ownPhysReleaseMs_ = -1;
        ownPrevState_ = shot_.state;
        ownLogFirst_ = true;
        ownBackend_ = backend;
        ownSrc_ = telemetryToken(controllerInputSource_);
        ownKind_ = telemetryToken(activePhysicalDeviceKind_);
    }

    const qint64 tMs = nowMs - ownStartMs_;
    ownLastTms_ = tMs;
    ++ownTicks_;
    if (!physSq) {
        ownPhysHeldAll_ = false;
        if (ownPhysReleaseMs_ < 0) {
            ownPhysReleaseMs_ = tMs;
        }
    }
    if (outSq) {
        ownOutClearedAll_ = false;
        ownMaxOutSq_ = true;
    }

    // Log the first tick, any phys/out Square change, and the Releasing->Cooldown
    // transition. The window is ~250ms so this is a handful of lines per shot.
    const bool stateChanged = shot_.state != ownPrevState_;
    const bool sqChanged = (physSq != ownPrevPhysSq_) || (outSq != ownPrevOutSq_);
    if (ownLogFirst_ || stateChanged || sqChanged) {
        // [ORION_OUTPUT_DIVERGENCE 2026-09-14 owner] l2/r2 are the OUTPUT trigger values. The
        // "it seemed like it was holding L2 for me" report could not be judged from this log at
        // all, because no line anywhere carried a trigger value.
        appendLog(QStringLiteral("Release tick: seq=%1 state=%2 t_ms=%3 phys_sq=%4 "
                                 "out_sq=%5 ok=%6 backend=%7 rs=(%8,%9) l2=%10 r2=%11 "
                                 "src=%12 kind=%13")
                      .arg(seq)
                      .arg(holdStateToken(shot_.state))
                      .arg(tMs)
                      .arg(physSq ? 1 : 0)
                      .arg(outSq ? 1 : 0)
                      .arg(submitOk ? 1 : 0)
                      .arg(backend)
                      .arg(output.rightStickX)
                      .arg(output.rightStickY)
                      .arg(static_cast<int>(output.l2))
                      .arg(static_cast<int>(output.r2))
                      .arg(ownSrc_)
                      .arg(ownKind_));
        ownLogFirst_ = false;
    }
    ownPrevPhysSq_ = physSq;
    ownPrevOutSq_ = outSq;
    ownPrevState_ = shot_.state;
}

// [ORION_OUTPUT_DIVERGENCE 2026-09-14 owner] ---------------------------------------------------
//
// THE REPORT: "buttons sometimes are weird, it seemed like it was holding L2 for me". Tonight's
// log cannot judge it — zero input-hook failures, no pad-silence lines, and not one trigger value
// on any output line — so the only honest answer is to start recording the thing itself.
//
// WHAT IT WATCHES: every button the engine has no business changing, plus both analog triggers.
// Square is EXCLUDED because the release logic legitimately suppresses and latches it (the whole
// remap/overlap-latch design is "output Square != physical Square"), so including it would bury
// the signal under normal operation. Everything else diverging means the output path invented or
// swallowed an input the player did not ask for — which is exactly the claim.
//
// COST: a handful of integer compares per tick, one QString only on an edge. Rate-limited to one
// START line per field per second so a stuck trigger cannot flood the ring buffer.
//
// BEHAVIOURAL CHANGE: none. Nothing here reads back into the output.
void OrionAppController::observeOutputDivergence(const ControllerState& physical,
                                                 const ControllerState& output)
{
    const qint64 nowMs = static_cast<qint64>(automation_.engineNowMs());
    // Square is deliberately absent; so are the sticks (the remap owns the right stick by design).
    struct FieldSample {
        const char* name;
        int out;
        int phys;
    };
    const auto btn = [](const ControllerState& s, uint16_t mask) {
        return (s.buttons & mask) != 0 ? 1 : 0;
    };
    const FieldSample samples[] = {
        {"l2", static_cast<int>(output.l2), static_cast<int>(physical.l2)},
        {"r2", static_cast<int>(output.r2), static_cast<int>(physical.r2)},
        {"cross", btn(output, XINPUT_GAMEPAD_A), btn(physical, XINPUT_GAMEPAD_A)},
        {"circle", btn(output, XINPUT_GAMEPAD_B), btn(physical, XINPUT_GAMEPAD_B)},
        {"triangle", btn(output, XINPUT_GAMEPAD_Y), btn(physical, XINPUT_GAMEPAD_Y)},
        {"l1", btn(output, XINPUT_GAMEPAD_LEFT_SHOULDER),
         btn(physical, XINPUT_GAMEPAD_LEFT_SHOULDER)},
        {"r1", btn(output, XINPUT_GAMEPAD_RIGHT_SHOULDER),
         btn(physical, XINPUT_GAMEPAD_RIGHT_SHOULDER)},
        {"l3", btn(output, XINPUT_GAMEPAD_LEFT_THUMB),
         btn(physical, XINPUT_GAMEPAD_LEFT_THUMB)},
        {"r3", btn(output, XINPUT_GAMEPAD_RIGHT_THUMB),
         btn(physical, XINPUT_GAMEPAD_RIGHT_THUMB)},
    };
    // [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16 owner] r2 is a WATCHED field, and the sprint
    // release makes it diverge on purpose for the whole of a sprinting Square press. Attribute it
    // rather than hide it -- this audit exists because of "it seemed like it was holding L2 for
    // me", and a policy that silently suppressed its own evidence would be the worse bug. Live
    // mining whitelists sprint_released=1 and every remaining r2 line IS a defect. Read once so
    // both the OPEN and END lines of one window agree.
    const int sprintShaped = automation_.sprintReleaseActive() ? 1 : 0;
    constexpr int kFieldCount = static_cast<int>(std::size(samples));
    static_assert(kFieldCount
                      <= static_cast<int>(std::tuple_size<decltype(outDivergeSinceMs_)>::value),
                  "outDivergeSinceMs_/outDivergeLoggedMs_ must cover every watched field");

    for (int i = 0; i < kFieldCount; ++i) {
        const FieldSample& f = samples[i];
        if (f.out == f.phys) {
            if (outDivergeSinceMs_[i] >= 0 && outDivergeReported_[i]) {
                appendLog(QStringLiteral(
                    "OUTPUT DIVERGENCE END: field=%1 held_ms=%2 state=%3 reason=%4 "
                    "sprint_released=%5")
                              .arg(QLatin1String(f.name))
                              .arg(nowMs - outDivergeSinceMs_[i])
                              .arg(holdStateToken(shot_.state))
                              .arg(shot_.releaseReason.isEmpty() ? QStringLiteral("-")
                                                                 : shot_.releaseReason)
                              .arg(std::strcmp(f.name, "r2") == 0 ? sprintShaped : 0));
            }
            outDivergeSinceMs_[i] = -1;
            outDivergeReported_[i] = false;
            continue;
        }
        if (outDivergeSinceMs_[i] < 0) {
            outDivergeSinceMs_[i] = nowMs;
            continue;
        }
        const qint64 heldMs = nowMs - outDivergeSinceMs_[i];
        // 40 ms = ~2-3 controller polls. Shorter than that is the ordinary one-frame skew between
        // the packet the engine read and the packet this tick is comparing against.
        if (heldMs < kOutDivergeMinMs_ || outDivergeReported_[i]) {
            continue;
        }
        if (outDivergeLoggedMs_[i] >= 0 && nowMs - outDivergeLoggedMs_[i] < 1000) {
            continue;   // 1/s per field
        }
        outDivergeLoggedMs_[i] = nowMs;
        outDivergeReported_[i] = true;
        ++outDivergeEvents_;
        appendLog(QStringLiteral(
            "OUTPUT DIVERGENCE: field=%1 out=%2 phys=%3 held_ms=%4 state=%5 reason=%6 "
            "sprint_released=%7")
                      .arg(QLatin1String(f.name))
                      .arg(f.out)
                      .arg(f.phys)
                      .arg(heldMs)
                      .arg(holdStateToken(shot_.state))
                      .arg(shot_.releaseReason.isEmpty() ? QStringLiteral("-")
                                                         : shot_.releaseReason)
                      .arg(std::strcmp(f.name, "r2") == 0 ? sprintShaped : 0));
    }
}

void OrionAppController::flushReleaseOwnershipTrace()
{
    if (ownSeq_ < 0) {
        return;
    }
    // out_cleared_all=1 proves Orion held output Square at 0 for the WHOLE pulse+cooldown
    // (any 0 here is a real engine override bug). phys_held_all / phys_release_t_ms say
    // whether the USER kept holding — if the bot cleared output cleanly while the user
    // still held and the shot still didn't fire in-game, the leak is downstream (Chiaki).
    appendLog(QStringLiteral("Release ownership: seq=%1 phys_held_all=%2 out_cleared_all=%3 "
                             "max_out_sq=%4 phys_release_t_ms=%5 ticks=%6 dur_ms=%7 "
                             "backend=%8 src=%9 kind=%10 out_diverge_events=%11")
                  .arg(ownSeq_)
                  .arg(ownPhysHeldAll_ ? 1 : 0)
                  .arg(ownOutClearedAll_ ? 1 : 0)
                  .arg(ownMaxOutSq_ ? 1 : 0)
                  .arg(ownPhysReleaseMs_)
                  .arg(ownTicks_)
                  .arg(ownLastTms_)
                  .arg(ownBackend_)
                  .arg(ownSrc_)
                  .arg(ownKind_)
                  .arg(outDivergeEvents_));
    ownSeq_ = -1;
}

QString OrionAppController::formatHelperError(const QString& operation, const QJsonObject& result) const
{
    const auto error = result.value(QStringLiteral("error")).toString(QStringLiteral("PS5 helper command failed."));
    if (error == QLatin1String("no_saved_profile")) {
        const auto path = result.value(QStringLiteral("profile_path")).toString();
        return QStringLiteral("No legacy profile is saved for '%1'. Use Chiaki registration instead.")
            .arg(result.value(QStringLiteral("user")).toString(config_.data().remotePlayProfile))
            + (path.isEmpty() ? QString() : QStringLiteral(" Profile store: %1").arg(path));
    }
    if (error == QLatin1String("user_not_registered_with_console")) {
        return QStringLiteral("The saved legacy profile is not paired with this console. Use Chiaki registration instead.");
    }
    if (error == QLatin1String("host_unreachable")) {
        return QStringLiteral("PS5 is not reachable at %1. Verify the console IP, wake state, and LAN connection.")
            .arg(result.value(QStringLiteral("host")).toString(config_.data().remotePlayConsoleIp));
    }
    if (error == QLatin1String("pin_must_be_8_digits")) {
        return QStringLiteral("PIN must be exactly 8 digits.");
    }
    if (error == QLatin1String("register_failed_check_pin_console")) {
        return QStringLiteral("Pairing failed. Check that the PS5 pairing screen is open and the PIN has not expired.");
    }
    if (error == QLatin1String("session_create_failed")) {
        return QStringLiteral("Legacy session create failed for %1. Use the Chiaki backend instead.")
            .arg(result.value(QStringLiteral("user")).toString(config_.data().remotePlayProfile));
    }
    if (operation == QLatin1String("test-session")) {
        return QStringLiteral("PS5 session test failed: %1").arg(error);
    }
    return error;
}

QString OrionAppController::rectSummary(const QRect& rect) const
{
    if (!rect.isValid() || rect.width() <= 0 || rect.height() <= 0) {
        return QStringLiteral("-");
    }
    return QStringLiteral("%1,%2 %3x%4")
        .arg(rect.x())
        .arg(rect.y())
        .arg(rect.width())
        .arg(rect.height());
}

void OrionAppController::clearRemotePreviewFrame()
{
    if (!frameProvider_) {
        return;
    }
    QImage blank(1280, 720, QImage::Format_RGB32);
    blank.fill(QColor(6, 9, 14));
    const int nextSerial = frameSerial_ == std::numeric_limits<int>::max()
        ? 1 : frameSerial_ + 1;
    // A lifecycle clear is also the provider's identity boundary: no delayed
    // async request from the old stream may resolve against retained pixels.
    frameProvider_->resetFrames(nextSerial, blank);
    meterOverlayTracker_.reset();
    meterOverlayContinuityLease_.reset();
    lastMeterOverlayVisualSeenMs_ = 0;
    meterOverlayComputedBox_ = {};
    meterOverlayComputedRejectedBox_ = {};
    remoteFrameOverlaySnapshots_.reset(RemoteFrameOverlaySnapshot{
        nextSerial, {}, {}, false, blank.size()});
    frameSerial_ = nextSerial;
    emit frameChanged();
    frameProvider_->resetStats();
    qmlPreviewStatsAtMs_ = 0;
    qmlPreviewReadyAcksTotal_ = 0;
    qmlPreviewReadyAcksWindow_ = 0;
    qmlPreviewStaleAcksWindow_ = 0;
    qmlPreviewLastAcknowledgedSerial_ = -1;
}

void OrionAppController::acknowledgeRemoteFramePresented(int serial)
{
    const RemoteFrameAckDisposition disposition = classifyRemoteFrameAck(
        serial, frameSerial_, qmlPreviewLastAcknowledgedSerial_);
    if (disposition == RemoteFrameAckDisposition::StaleOrFuture) {
        ++qmlPreviewStaleAcksWindow_;
        return;
    }
    if (disposition == RemoteFrameAckDisposition::Duplicate) {
        return;
    }
    const auto snapshot = remoteFrameOverlaySnapshots_.lookup(serial);
    if (!snapshot.has_value()) {
        ++qmlPreviewStaleAcksWindow_;
        return;
    }
    qmlPreviewLastAcknowledgedSerial_ = serial;
    ++qmlPreviewReadyAcksTotal_;
    ++qmlPreviewReadyAcksWindow_;

    QRect presentedMeterBox = snapshot->meterBox;
    if (!presentedMeterBox.isValid()
        && snapshot->meterConfirmed
        && snapshot->joinedCaptureBox.isValid()) {
        presentedMeterBox = resolveLateMeterOverlayBox(
            snapshot->joinedCaptureBox,
            snapshot->captureSize,
            snapshot->frameSize,
            snapshot->shotToken,
            snapshot->sourceFrameNumber);
    }

    const bool boxChanged = meterBox_ != presentedMeterBox
        || meterRejectedBox_ != snapshot->rejectedBox
        || meterConfirmed_ != snapshot->meterConfirmed;
    const bool sizeChanged = liveFrameWidth_ != snapshot->frameSize.width()
        || liveFrameHeight_ != snapshot->frameSize.height();

    meterBox_ = presentedMeterBox;
    meterRejectedBox_ = snapshot->rejectedBox;
    meterConfirmed_ = snapshot->meterConfirmed;
    liveFrameWidth_ = snapshot->frameSize.width();
    liveFrameHeight_ = snapshot->frameSize.height();

    if (boxChanged) {
        emit meterBoxChanged();
    }
    if (sizeChanged) {
        // liveFrameWidth/Height are frameChanged properties. The source serial
        // is unchanged, so this re-evaluation cannot start a second image load.
        emit frameChanged();
    }
}

void OrionAppController::setPreviewRenderClockActive(bool active)
{
    remotePlay_.setPreviewRenderClockActive(active);
    // Same signal already tells us whether anything is being drawn over the
    // video. Reusing it means the cosmetic hue cycle cannot keep ticking behind
    // a page that has no preview on it, and costs no new plumbing.
    if (overlayPreviewRenderActive_ != active) {
        overlayPreviewRenderActive_ = active;
        refreshMeterOverlayEffectTimer();
    }
}

void OrionAppController::advanceRemotePreviewPresentation()
{
    remotePlay_.presentNextPreviewFrameOnRenderTick();
}

void OrionAppController::handleRemoteFrame(const QImage& frame, int frameNumber)
{
    const bool captureHealthChanged = captureSourceHealth_ != QLatin1String("frame_feed_active");
    if (captureHealthChanged) {
        captureSourceHealth_ = QStringLiteral("frame_feed_active");
    }
    const int nextFrameSerial = frameSerial_ == std::numeric_limits<int>::max()
        ? 1 : frameSerial_ + 1;
    if (frameProvider_) {
        // Publish the immutable image under its final URL identity before any
        // QML-visible serial or matching overlay state advances. On the
        // practically-unreachable integer wrap, clear the old key namespace.
        if (nextFrameSerial <= frameSerial_) {
            frameProvider_->resetFrames(nextFrameSerial, frame);
        } else {
            frameProvider_->setFrame(nextFrameSerial, frame);
        }
    }
    lastRemoteFrame_ = frame;

    // Overlay box from the AUTHORITATIVE sidecar bbox (cached in meterBoxCapture_, capture-frame px),
    // mapped into THIS preview image's px. This REPLACES the old per-frame GUI-thread preview detector
    // (convertToFormat + cvtColor + detector_.detect() — a full OpenCV pass on EVERY streamed frame,
    // the single biggest GUI-thread frame-drop source). The full-res sidecar is the one source of truth
    // for timing AND the overlay; nothing here touches the engine/fill/rejection state. Coords are the
    // preview-image px the QML overlay expects (it scales them to the displayed image via drawScale).
    {
        const qint64 nowOverlayMs = QDateTime::currentMSecsSinceEpoch();
        // OVERLAY CONFIRMATION GATE. Draw the lock only while the authoritative detector has a
        // recent genuine lock. This display-only lease is deliberately longer than timing/metric
        // freshness: the measured detector p90 was 167ms (max 298ms), so reusing the 120ms timing
        // TTL made a continuously visible meter blink. Echoes/stale samples cannot renew it and
        // AutomationEngine never reads it. This is driven by the detector, NOT by isShooting (which is true the
        // instant Square is held ~135ms, regardless of whether a meter exists). That is exactly
        // why holding Square idle used to paint a lock on a stale prior-shot bbox or a transient
        // distractor: the old gate keyed off the button + a 300ms sticky on lastMeterSeenMs_
        // (which also advances on echoes). Keying off lastRealMeterSeenMs_ kills the idle false-lock.
        const bool overlayRuntimeHealthy = automationSecurityAllowed()
            && captureSourceHealth_ == QLatin1String("frame_feed_active")
            && !captureAwaitingFreshFrame_
            && !guiFreezeTripped_.load(std::memory_order_relaxed);
        const bool rawMeterRecent = meterOverlayVisualRecent(
            overlayRuntimeHealthy, lastMeterOverlayVisualSeenMs_, nowOverlayMs,
            isShooting());
        const bool continuityMeterRecent = meterOverlayContinuityLease_.maintain(
            nowOverlayMs, isShooting(), shot_.physicalShotEpoch,
            overlayRuntimeHealthy);
        const bool meterRecent = rawMeterRecent || continuityMeterRecent;
        const QSize reportedCaptureSize(
            remotePlay_.captureWidth(), remotePlay_.captureHeight());
        // Prefer the bbox's own immutable coordinate dimensions. Both capture-card
        // and Remote Play detector frames currently normalize to 1280x720, while
        // this fallback preserves compatibility with an older sidecar that omits
        // bbox_wh. Never clear a live box merely because unrelated telemetry moved
        // to the next resolution before this joined preview frame was presented.
        const QSize captureSize = meterBoxCaptureSize_.isValid()
            ? meterBoxCaptureSize_ : reportedCaptureSize;
        // FRAME-ID JOIN: pick the bbox that was detected on THIS preview frame (frameNumber), not the
        // latest one. This is the drift/flicker fix: the box now composites on its own frame. When the
        // preview carries no decoder id (frameNumber < 0, e.g. GDI tiers / placeholder), fall back to
        // the latest bbox (meterBoxCapture_) — the pre-join behaviour — so those paths aren't regressed.
        // If a real decoder id has NO recorded box (join miss), draw NOTHING rather than smear a stale
        // position: a missing box for one paint reads far better than a box in the wrong place.
        // Do NOT "fall back to meterBoxCapture_" here — meterBoxCapture_ is the LATEST bbox, which by
        // definition belongs to a DIFFERENT (newer) frame than the one being painted. Substituting it
        // re-creates exactly the drift/flicker the frame-id join was built to remove. The blink is
        // instead handled below by HOLDING the last correctly-joined box while the meter is still
        // recently-real, which keeps the box on screen without ever drawing a wrong-frame position.
        QRect joinedCaptureBox;
        int joinedDetectionFrameNumber = -1;
        bool haveJoinedBox = false;
        if (frameNumber >= 0) {
            haveJoinedBox = meterBoxRing_.lookup(
                frameNumber, joinedCaptureBox, &joinedDetectionFrameNumber);
        } else if (!meterBoxCapture_.isNull()) {
            joinedCaptureBox = meterBoxCapture_;
            haveJoinedBox = true;
        }
        const bool usableJoinedBox = haveJoinedBox && !joinedCaptureBox.isNull()
            && captureSize.isValid() && frame.width() > 0 && frame.height() > 0;
        const MeterOverlayBoxAction boxAction =
            meterOverlayBoxAction(meterRecent, usableJoinedBox);
        if (boxAction == MeterOverlayBoxAction::UpdateFromJoinedBox) {
            const QRect rawMappedBox = mapCaptureBoxAspectFit(
                joinedCaptureBox, captureSize, frame.size());
            const QRect newBox = meterOverlayTracker_.update(
                rawMappedBox, shot_.armToken, frame.size(), frameNumber);
            // Re-mapped every preview frame, but EMIT only when it actually moved — the capture bbox
            // changes per detection (~30/s), not per preview frame, so this kills the per-frame signal.
            // meterBoxChanged (not statusChanged) repaints only the box, not the 98-property fan-out.
            if (newBox != meterOverlayComputedBox_) {
                meterOverlayComputedBox_ = newBox;
                meterOverlayComputedRejectedBox_ = {};
            }
        } else if (boxAction == MeterOverlayBoxAction::HoldLastJoinedBox) {
            // Metadata for this preview frame has not joined yet.  Keep the
            // last box that *did* belong to its presented pixels; the bounded
            // detector-freshness/continuity lease above is the only authority
            // for this hold.  Do not update tracker state from a newer box.
        } else if (boxAction == MeterOverlayBoxAction::Clear) {
            // FOOLPROOF BOX (2026-07-22): only clear the box when the meter is GENUINELY gone
            // (no real detection within the confirm window).
            // Freshness expired or the detector/capture authority failed.  A
            // mere join miss is handled by HoldLastJoinedBox above; reaching
            // this branch is therefore a genuine visibility boundary.
            meterOverlayTracker_.reset();
            if (!meterOverlayComputedBox_.isNull()) {
                meterOverlayComputedBox_ = {};
                meterOverlayComputedRejectedBox_ = {};
            }
        }

        // The async image request and every pixel-coupled overlay input share
        // one serial. Public QML properties remain on the prior acknowledged
        // snapshot while retainWhileLoading keeps that prior texture visible.
        remoteFrameOverlaySnapshots_.publish(RemoteFrameOverlaySnapshot{
            nextFrameSerial,
            meterOverlayComputedBox_,
            meterOverlayComputedRejectedBox_,
            meterRecent,
            frame.size(),
            captureSize,
            frameNumber,
            boxAction == MeterOverlayBoxAction::UpdateFromJoinedBox
                ? joinedCaptureBox : QRect{},
            boxAction == MeterOverlayBoxAction::UpdateFromJoinedBox
                ? joinedDetectionFrameNumber : -1,
            shot_.armToken});
    }

    if (captureHealthChanged) {
        emit statusChanged();
    }
    // Expose the keyed source URL only after its image and all matching overlay
    // properties are committed. QML may load asynchronously, but a request for
    // this serial can now resolve only this frame, never the latest one.
    frameSerial_ = nextFrameSerial;
    emit frameChanged();

    // Measure the final image-provider handoff, not merely SHM receipt. This is
    // the stage that exposed the old synchronous-QML stutter: a healthy producer
    // and SHM reader could still feed only 53-57 visible refreshes per second.
    if (frameProvider_) {
        const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
        if (qmlPreviewStatsAtMs_ == 0) {
            frameProvider_->resetStats();
            qmlPreviewReadyAcksWindow_ = 0;
            qmlPreviewStaleAcksWindow_ = 0;
            qmlPreviewStatsAtMs_ = nowMs;
        // [ORION_ACTIVITY_FEED 2026-09-14 owner "overall polish"] Same treatment as
        // the three RemotePlaySession gauges: 438 of the census hour's lines came
        // from this one 5 s beat. Steady state is a minute; a window that saw a
        // stale presentation ack (the symptom this line exists to expose) keeps the
        // original 5 s cadence. The line stays disk-only either way — it is
        // deny-listed out of the customer Activity ring.
        } else if (nowMs - qmlPreviewStatsAtMs_
                   >= (qmlPreviewStaleAcksWindow_ > 0
                           ? kQmlPreviewStatsIntervalMs_
                           : kQmlPreviewStatsHealthyIntervalMs_)) {
            const double seconds = std::max(
                0.001, static_cast<double>(nowMs - qmlPreviewStatsAtMs_) / 1000.0);
            const RemoteFrameProviderStats stats = frameProvider_->takeWindowStats();
            // 60 Hz presentation budget. This is observational only: it never
            // gates source frames, detector delivery, or automation authority.
            const double presenterDeadlineLateMaxMs = std::max(
                0.0, stats.maxRequestGapMs - (1000.0 / 60.0));
            appendLog(QStringLiteral(
                "qml_preview_pipeline: set_fps=%1 request_fps=%2 request_gap_max_ms=%3 "
                "sets=%4 requests=%5 async=%6 snapshot_miss=%7 ready_ack_fps=%8 "
                "ready_ack_total=%9 stale_ack=%10 present_deadline_late_max_ms=%11")
                          .arg(stats.framesSet / seconds, 0, 'f', 1)
                          .arg(stats.imageRequests / seconds, 0, 'f', 1)
                          .arg(stats.maxRequestGapMs, 0, 'f', 1)
                          .arg(stats.framesSet)
                          .arg(stats.imageRequests)
                          .arg(previewAsync_ ? 1 : 0)
                          .arg(stats.snapshotMisses)
                          .arg(qmlPreviewReadyAcksWindow_ / seconds, 0, 'f', 1)
                          .arg(qmlPreviewReadyAcksTotal_)
                          .arg(qmlPreviewStaleAcksWindow_)
                          .arg(presenterDeadlineLateMaxMs, 0, 'f', 1));
            qmlPreviewReadyAcksWindow_ = 0;
            qmlPreviewStaleAcksWindow_ = 0;
            qmlPreviewStatsAtMs_ = nowMs;
        }
    }
}

void OrionAppController::registerRawInputController()
{
#ifdef Q_OS_WIN
    if (!rawInputWorker_) {
        rawInputWorker_ = new OrionRawInputWorker(this);
    }
    if (rawInputRegistered_) {
        return;
    }
    switch (rawInputWorker_->status()) {
    case OrionRawInputWorker::Status::Registered:
        rawInputRegistered_ = true;
        appendLog(QStringLiteral("Raw Input controller capture armed (dedicated input thread)."));
        break;
    case OrionRawInputWorker::Status::Failed:
        rawInputRegistered_ = true; // stop polling; XInput/WinMM selection fallbacks remain
        appendLog(QStringLiteral("Raw Input controller capture failed; falling back to XInput/WinMM."));
        break;
    case OrionRawInputWorker::Status::Starting:
        break; // worker thread still arming -- re-check on the next tick
    }
#endif
}

void OrionAppController::pollPhysicalController()
{
    {
        // [CL2-P4-001 / CL2-P5-001] Track the worst gap between input ticks (reported and reset by
        // the input hook heartbeat as `Input tick health`).
        const auto tickNow = std::chrono::steady_clock::now();
        if (inputTickLastAt_.time_since_epoch().count() != 0) {
            const double gapMs = std::chrono::duration<double, std::milli>(tickNow - inputTickLastAt_).count();
            inputTickGapMaxMs_ = std::max(inputTickGapMaxMs_, gapMs);
        }
        inputTickLastAt_ = tickNow;
    }
#ifdef Q_OS_WIN
    registerRawInputController();
    if (rawInputWorker_) {
        // Mirror the dedicated input thread's freshest snapshot into the GUI-thread
        // members the selector reads (the GUI-thread WM_INPUT handler used to write
        // these directly before the read moved off-thread).
        const auto snap = rawInputWorker_->snapshot();
        if (snap.lastReportMs > 0) {
            // [ORION_SPECIAL_BUTTON_TRACE 2026-09-11] Owner report: Options / touchpad "don't
            // work". Log the EDGES of the non-shot buttons (once per press and release) so the
            // next session shows whether the pad delivered them (decode) and which route
            // carried them. Edge-only, so it stays quiet during play.
            {
                const auto specialMask = [](const ControllerState& st) -> unsigned {
                    return (st.options() ? 1u : 0u) | (st.create() ? 2u : 0u)
                        | (st.ps() ? 4u : 0u) | (st.touchpad ? 8u : 0u);
                };
                const unsigned before = specialMask(rawInputState_);
                const unsigned after = specialMask(snap.state);
                if (before != after) {
                    appendLog(QStringLiteral(
                                  "Special button edge: options=%1 create=%2 ps=%3 touchpad=%4 "
                                  "route=%5 hook=%6")
                                  .arg((after & 1u) ? 1 : 0)
                                  .arg((after & 2u) ? 1 : 0)
                                  .arg((after & 4u) ? 1 : 0)
                                  .arg((after & 8u) ? 1 : 0)
                                  .arg(directPipeOwnsInput_ ? QStringLiteral("pipe")
                                                            : QStringLiteral("xusb"))
                                  .arg(orionInput_.enabled() ? 1 : 0));
                }
            }
            rawInputState_ = snap.state;
            lastRawInputMs_ = snap.lastReportMs;
            if (!snap.label.isEmpty()) {
                rawInputLabel_ = snap.label;
            }
            if (!snap.devicePath.isEmpty()) {
                rawInputDevicePath_ = snap.devicePath;
            }
            // Only a FRESH report asserts presence (a stale snapshot must not undo the
            // removal/enumeration logic that clears rawInputPresent_).
            if (QDateTime::currentMSecsSinceEpoch() - snap.lastReportMs <= 750) {
                rawInputPresent_ = true;
            }
        }
    }
    {
        const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
        const qint64 rawReportAgeMs = lastRawInputMs_ > 0
            ? nowMs - lastRawInputMs_
            : std::numeric_limits<qint64>::max();
        controllerLastInputAgeMs_ = lastRawInputMs_ > 0 ? static_cast<double>(rawReportAgeMs) : -1.0;

        // WM_INPUT is already decoded by the dedicated worker. Once that worker
        // has a fresh report and stable device identity, a full GetRawInputDeviceList
        // + HID path/classification sweep every 500 ms adds GUI-thread work but no
        // new information. Device-change events reset the sentinel below; a stale
        // or incomplete snapshot retains the bounded 500 ms recovery scan.
        const bool rawSnapshotFresh = rawInputPresent_ && lastRawInputMs_ > 0
            && rawReportAgeMs >= 0 && rawReportAgeMs <= 750;
        const bool rawIdentityKnown = !rawInputDevicePath_.isEmpty()
            && !rawInputLabel_.isEmpty();
        const bool rawPresenceRefreshRequested = lastRawInputPresenceCheckMs_ == 0;
        const bool rawPresenceStaleOrMissing = !rawSnapshotFresh || !rawIdentityKnown;
        if (rawPresenceRefreshRequested
            || (rawPresenceStaleOrMissing
                && nowMs - lastRawInputPresenceCheckMs_ >= 500)) {
            lastRawInputPresenceCheckMs_ = nowMs;
            QString label;
            QString path;
            rawInputPresent_ = findRawInputController(&rawInputDeviceKinds_, &label, &path);
            if (rawInputPresent_ && !label.isEmpty()) {
                rawInputLabel_ = label;
            }
            if (rawInputPresent_ && !path.isEmpty()) {
                rawInputDevicePath_ = path;
            }
        }

        QVector<ControllerDeviceCandidate> candidates;
        const bool rawInputLive = rawInputPresent_ && lastRawInputMs_ > 0 && rawReportAgeMs <= 750;
        if (rawInputPresent_) {
            ControllerDeviceCandidate raw;
            raw.key = QStringLiteral("raw:%1").arg(rawInputDevicePath_);
            raw.source = QStringLiteral("RawInput");
            raw.label = rawInputLabel_.isEmpty() ? QStringLiteral("PlayStation HID pad") : rawInputLabel_;
            raw.detail = rawInputDevicePath_.isEmpty() ? raw.label : QStringLiteral("%1 / %2").arg(raw.label, rawInputDevicePath_);
            raw.state = rawInputState_;
            raw.live = rawInputLive;
            raw.physicalSony = rawInputDevicePath_.contains(QStringLiteral("VID_054C"), Qt::CaseInsensitive);
            raw.virtualDevice = rawInputPathLooksVirtual(rawInputDevicePath_);
            raw.liveReportAgeMs = rawInputLive ? rawReportAgeMs : -1;
            candidates.push_back(raw);
        }

        const int activeVirtualXinputSlot = controller_.xinputUserIndex() >= 0
            ? controller_.xinputUserIndex()
            : inferredVirtualXinputSlot_;
        bool xinputPhysicalLive = false;
        const qint64 nowXinputMs = QDateTime::currentMSecsSinceEpoch();
        if (retiredVirtualXinputSlot_ >= 0
            && nowXinputMs > retiredVirtualXinputSlotUntilMs_) {
            retiredVirtualXinputSlot_ = -1;
            retiredVirtualXinputSlotUntilMs_ = 0;
        }
        for (DWORD index = 0; index < XUSER_MAX_COUNT; ++index) {
            // Skip a slot that read EMPTY within the last ~1s — XInputGetState on a disconnected
            // slot stalls, and polling 4 empty slots at 250Hz blows the GUI frame budget.
            if (nowXinputMs < xinputSlotNextProbeMs_[index]) {
                continue;
            }
            XINPUT_STATE nativeState = {};
            if (XInputGetState(index, &nativeState) != ERROR_SUCCESS) {
                xinputSlotNextProbeMs_[index] = nowXinputMs + 1000;  // empty -> don't re-probe for 1s
                xinputSlotCapabilityKnown_[index] = false;
                xinputSlotNoNavigation_[index] = false;
                continue;
            }
            xinputSlotNextProbeMs_[index] = 0;  // connected -> poll every tick (live state)
            if (!xinputSlotCapabilityKnown_[index]) {
                XINPUT_CAPABILITIES caps = {};
                const bool capabilityRead =
                    XInputGetCapabilities(index, XINPUT_FLAG_GAMEPAD, &caps)
                    == ERROR_SUCCESS;
                xinputSlotCapabilityKnown_[index] = true;
                if (capabilityRead) {
                    xinputSlotNoNavigation_[index] =
                        (caps.Flags & XINPUT_CAPS_NO_NAVIGATION) != 0;
                }
            }
            const bool noNavigationCapability =
                xinputSlotNoNavigation_[index];
            ControllerDeviceCandidate xi;
            xi.key = QStringLiteral("xinput:%1").arg(index);
            xi.source = QStringLiteral("XInput");
            xi.label = QStringLiteral("XInput pad");
            xi.detail = noNavigationCapability
                ? QStringLiteral("XInput slot %1 (virtual-compatible)").arg(index + 1)
                : QStringLiteral("XInput slot %1").arg(index + 1);
            xi.state = xinputStateToController(nativeState);
            xi.live = true;
            // Windows can keep the just-unplugged ViGEm slot readable for a few
            // polls after disconnectController(). Identity used to be cleared
            // immediately, so that stale XUSB target was selected as the
            // "physical" pad and a second target was created. Quarantine the
            // retired slot across the PnP removal window and also honor the
            // capability signature ViGEm exposes on this build.
            xi.virtualDevice = isVirtualXinputCandidate(
                static_cast<int>(index), activeVirtualXinputSlot,
                retiredVirtualXinputSlot_, nowXinputMs,
                retiredVirtualXinputSlotUntilMs_, noNavigationCapability);
            if (!xi.virtualDevice) {
                xinputPhysicalLive = true;  // a real Xbox pad -> WinMM fallback not needed
            }
            xi.liveReportAgeMs = 0;
            candidates.push_back(xi);
        }

        // WinMM/dinput joystick polling is the sole source of a recurring c0000374
        // heap corruption: joyGetPosEx -> joyOpen -> dinput!DIHid_BuildHidList ->
        // RtlReAllocateHeap corrupts the shared process heap when the device set is
        // churning (unplug/replug, HidHide cloak transitions). It is also a pure
        // last-resort fallback (the selector ranks WinMM below RawInput/XInput), so
        // only touch it when nothing better is live, never within the device-change
        // cooldown, and at most ~1/sec. The poll and the WM_INPUT_DEVICE_CHANGE filter
        // run on the same GUI thread, so the cooldown fully avoids enumerating while
        // devices change. Cache the last sample so a DI-only pad doesn't flicker out
        // of the selector between throttled polls.
        constexpr qint64 kWinMmDeviceChangeCooldownMs = 2000;
        constexpr qint64 kWinMmPollIntervalMs = 1000;
        constexpr qint64 kWinMmCacheTtlMs = 1500;
        const bool winmmHigherPriorityLive = rawInputLive || xinputPhysicalLive;
        if (isXboxRemotePlay(config_.data()))
            lastWinMmSeenMs_ = 0;
        // Once any modern controller is seen, latch WinMM off for the whole session (fixes the
        // unplug/replug c0000374: an unplug momentarily drops rawInputLive, and WinMM enumerating in
        // that churn window corrupts the heap).
        // Latch on PRESENT (enumerated), not only LIVE (recent input): an idle-but-connected
        // DualSense — e.g. while the user sits on the auth / stream-setup screen without touching
        // it — never goes "live", so latching on rawInputLive alone left WinMM polling every 1s
        // and eventually hitting the dinput c0000374 heap corruption. rawInputPresent_ means a
        // modern HID pad is enumerated, so the legacy WinMM/dinput fallback is never needed.
        if (winmmHigherPriorityLive || rawInputPresent_) {
            modernControllerEverSeen_ = true;
        }
        const bool winmmDeviceChurnCooldown = lastControllerDeviceChangeMs_ > 0
            && nowMs - lastControllerDeviceChangeMs_ < kWinMmDeviceChangeCooldownMs;
        DWORD winmmFreshRawButtons = 0;
        bool winmmFreshRawValid = false;
        if (!isXboxRemotePlay(config_.data()) && !modernControllerEverSeen_ && !winmmHigherPriorityLive && !winmmDeviceChurnCooldown
            && nowMs - lastWinMmPollMs_ >= kWinMmPollIntervalMs) {
            lastWinMmPollMs_ = nowMs;
            ControllerState winmmState;
            UINT joyId = 0;
            QString winmmLabel;
            bool winmmVirtual = false;
            bool winmmSonyVid = false;
            if (readWinMmController(&winmmState, &joyId, &winmmLabel, &winmmVirtual, &lastWinMmRanges_,
                                    &winmmFreshRawButtons, &winmmSonyVid)) {
                lastWinMmState_ = winmmState;
                lastWinMmJoyId_ = joyId;
                lastWinMmLabel_ = winmmLabel;
                lastWinMmVirtual_ = winmmVirtual;
                lastWinMmSonyVid_ = winmmSonyVid;
                lastWinMmSeenMs_ = nowMs;
                winmmFreshRawValid = true;
            }
        }
        // FAST PATH — WinMM is the live route (no RawInput / physical-XInput candidate): re-read
        // JUST the known joystick id EVERY 4ms tick. Serving ticks from the 1/s discovery cache
        // shipped ~1Hz, up-to-1.5s-stale input to the console whenever this fallback carried a
        // session (live 2026-07-06: route "WinMM Microsoft PC-joystick driver" = delayed pad).
        // joyGetPosEx on an id that answered within the TTL does no device enumeration — the
        // c0000374 heap corruption came from ENUMERATING during device churn, which the churn
        // cooldown still gates here (and full discovery above stays at 1/s). Same gates as
        // discovery (incl. the modern-pad latch) so the candidate's LIFECYCLE is unchanged —
        // only its freshness improves. A failed read stops refreshing lastWinMmSeenMs_, so the
        // candidate ages out via the TTL and the 1/s discovery resumes.
        if (!isXboxRemotePlay(config_.data()) && !modernControllerEverSeen_ && !winmmHigherPriorityLive && !winmmDeviceChurnCooldown
            && lastWinMmSeenMs_ > 0 && nowMs > lastWinMmSeenMs_
            && nowMs - lastWinMmSeenMs_ <= kWinMmCacheTtlMs) {
            ControllerState freshWinMm;
            if (readWinMmControllerById(static_cast<UINT>(lastWinMmJoyId_), lastWinMmRanges_, &freshWinMm,
                                        &winmmFreshRawButtons)) {
                lastWinMmState_ = freshWinMm;
                lastWinMmSeenMs_ = nowMs;
                winmmFreshRawValid = true;
            }
        }
        // DIAGNOSTIC (WinMM fallback route only): applyWinMmButtonsToSample assumes the Sony
        // HID-usage button order (btn1=Square ... btn14=Touchpad), which can in principle vary
        // by pad revision/driver. Log the RAW dwButtons mask on change alongside the mapped
        // names so a 10-second live press-test of each button verifies (or corrects) the order
        // straight from logs/orion_native.log. Budgeted so a long WinMM session can't bloat
        // the log.
        if (winmmFreshRawValid && winmmFreshRawButtons != lastWinMmRawButtons_) {
            if (winMmRawButtonLogBudget_ > 0) {
                --winMmRawButtonLogBudget_;
                appendLog(QStringLiteral("WinMM raw dwButtons=0x%1 -> mapped %2")
                              .arg(winmmFreshRawButtons, 4, 16, QLatin1Char('0'))
                              .arg(winMmMappedButtonsLabel(lastWinMmState_)));
                if (winMmRawButtonLogBudget_ == 0) {
                    appendLog(QStringLiteral(
                        "WinMM raw button log budget exhausted; further changes unlogged."));
                }
            }
            lastWinMmRawButtons_ = winmmFreshRawButtons;
        }
        if (!winmmHigherPriorityLive && lastWinMmSeenMs_ > 0
            && nowMs - lastWinMmSeenMs_ <= kWinMmCacheTtlMs) {
            ControllerDeviceCandidate wm;
            wm.key = QStringLiteral("winmm:%1").arg(lastWinMmJoyId_);
            wm.source = QStringLiteral("WinMM");
            wm.label = lastWinMmLabel_.isEmpty() ? QStringLiteral("WinMM USB pad") : lastWinMmLabel_;
            wm.detail = QStringLiteral("%1 / WinMM joystick #%2").arg(wm.label).arg(lastWinMmJoyId_ + 1);
            wm.state = lastWinMmState_;
            wm.live = true;
            wm.virtualDevice = lastWinMmVirtual_;
            wm.liveReportAgeMs = nowMs - lastWinMmSeenMs_;
            candidates.push_back(wm);
        }

        const auto selected = controllerSelector_.select(candidates, nowMs);
        controllerHealth_ = selected.health;
        controllerIsPhysicalSony_ = selected.active && selected.device.physicalSony;
        controllerStableDevice_ = selected.active ? selected.device.detail : QStringLiteral("-");

        if (!selected.active) {
            if (squareOutputWatchdogEnabled_)
                squareOutputWatchdog_.observePhysical(false, false);
            hookDigitalRestSinceMs_ = -1;
            hookReleaseRepairDueMs_ = -1;
            hookReleaseRepairHavePreviousOutput_ = false;
            previousControllerUiState_ = ControllerState{};
            squareUpAuditTracker_.reset();
            physicalPadLive_ = false;
            // [PRESSED OVERLAY 2026-08-08] Route lost: clear the local badge so
            // a pad unplugged mid-hold cannot leave "PRESSED" lit.
            syncPressedOverlay(false);
            // Route lost: the raw Square sample is gone. Without this a Square
            // held at unplug would stay latched forever — shouldDisengage()'s
            activePhysicalDevicePath_.clear();
            activePhysicalDeviceKind_.clear();
            if (physicalPadConnected_) {
                if (physicalMissingSinceMs_ == 0) {
                    physicalMissingSinceMs_ = nowMs;
                    physicalMissingGraceWarned_ = false;
                    // [ORION_METER_DELAY 2026-08-07] Give a longer grace when
                    // nothing is in flight — a Bluetooth pad going to sleep
                    // shouldn't rip the virtual target down.
                    // [ORION_PAD_TEARDOWN_GRACE 2026-08-07] Idle grace 60000 -> 8000
                    // ms; a minute of silent grace let users debug a "dead" bot
                    // for a full minute before the log said anything. 8 s still
                    // covers ordinary BT reconnects.
                    // [ORION_PAD_SILENT_HOLD 2026-09-11] Enumerated-but-silent pads are
                    // HELD (neutral) for 30 s and nudged once; only a pad that is gone
                    // from the bus keeps the 2.5 s / 8 s teardown. See
                    // silentPadTeardownGraceMs() for the live evidence.
                    physicalMissingNudged_ = false;
                    physicalMissingTeardownGraceMs_ = silentPadTeardownGraceMs(
                        rawInputPresent_, automation_.armed() || ownSeq_ >= 0);
                    // Transport absence is not a physical UP/neutral sample.
                    // Fence both reader wake and controller output until the
                    // reappeared selected device proves a real neutral state.
                    shotIntentEdgeTracker_.beginTransportRecovery();
                    // [ORION_METER_DELAY 2026-08-07] Transport absence is not a
                    // physical release for FIRE purposes, but the delay actuator's
                    // watchdog exempts a held Square, so a vanished pad must not
                    // leave the engage feed latched true. Drop both shot inputs;
                    // this carries no release/fire semantics.
                    meterDelay_.setPhysicalSquareHeld(false);
                    setMeterDelayShotCycle(false);
                    automation_.setArmed(false);
                    disarmPreciseFire();
                    latencyCacheRouteAttestation_.revoke();
                    automation_.reset();
                    neutralizeOwnedInput();
                    // [ViGEm RACE] VirtualController carries no internal lock — every mutation has
                    // to be serialized caller-side via submitMutex_ or it interleaves with the
                    // precise fire thread's release submit and can corrupt or erase the press.
                }
                if (nowMs - physicalMissingSinceMs_ < physicalMissingTeardownGraceMs_) {
                    // [ORION_PAD_SILENT_HOLD 2026-09-11] Enumerated + silent: nudge the
                    // HID collection once (open + input-report poll) and say so. The
                    // virtual pad was already submitted neutral at onset, so nothing
                    // is stuck while we wait for the report stream to come back.
                    if (!physicalMissingNudged_ && rawInputPresent_
                        && nowMs - physicalMissingSinceMs_ >= 400) {
                        physicalMissingNudged_ = true;
                        bool nudgeOpened = false;
                        bool nudgeAnswered = false;
                        nudgePhysicalPadOnce(&nudgeOpened, &nudgeAnswered);
                        appendLog(QStringLiteral(
                                      "Physical controller enumerated but silent for %1 ms - "
                                      "nudged the HID collection (open=%2 report_poll=%3); "
                                      "virtual pad held neutral for up to %4 ms.")
                                      .arg(nowMs - physicalMissingSinceMs_)
                                      .arg(nudgeOpened ? 1 : 0)
                                      .arg(nudgeAnswered ? 1 : 0)
                                      .arg(physicalMissingTeardownGraceMs_));
                    }
                    // [ORION_PAD_TEARDOWN_GRACE 2026-08-07] Emit ONE user-visible
                    // warning once the grace has been running >3 s, so the user
                    // isn't debugging a "dead bot" in silence. Latched so the
                    // 50ms poll doesn't churn the log.
                    if (!physicalMissingGraceWarned_
                        && nowMs - physicalMissingSinceMs_ >= 3000) {
                        physicalMissingGraceWarned_ = true;
                        const qint64 remainingMs =
                            physicalMissingTeardownGraceMs_
                            - (nowMs - physicalMissingSinceMs_);
                        appendLog(QStringLiteral(
                                      "Physical controller silent for >3s - "
                                      "virtual target will be torn down in %1 ms "
                                      "unless the pad reappears (check USB/BT).")
                                      .arg(std::max<qint64>(0, remainingMs)));
                    }
                    notifyControllerStatusAtHumanCadence(nowMs);
                    return;
                }
                physicalPadConnected_ = false;
                // [ViGEm RACE] Same caller-side serialization, and here it is a use-after-free
                // rather than a dropped report: disconnectController() FREES the ViGEm target, so
                // doing it unlocked can pull the pad out from under a fire thread that is already
                // inside controller_.submit().
                {
                    QMutexLocker submitLock(&submitMutex_);
                    if (controller_.isConnected()) {
                        const int retiringSlot = controller_.xinputUserIndex() >= 0
                            ? controller_.xinputUserIndex()
                            : inferredVirtualXinputSlot_;
                        if (retiringSlot >= 0) {
                            retiredVirtualXinputSlot_ = retiringSlot;
                            retiredVirtualXinputSlotUntilMs_ = nowMs
                                + kRetiredVirtualSlotQuarantineMs;
                        }
                        ControllerState neutral;
                        controller_.submit(neutral);
                        controller_.disconnectController();
                        inferredVirtualXinputSlot_ = -1;
                    }
                }
                appendLog(QStringLiteral("Physical pad unavailable - virtual pad torn down to prevent stuck input."));
                // Pad torn down mid-release — flush the ownership summary so it's not lost.
                if (ownSeq_ >= 0) {
                    flushReleaseOwnershipTrace();
                }
            }
            const QString status = selected.physicalSonyVisible
                ? QStringLiteral("Physical detected, waiting for input report")
                : (selected.virtualVisible
                       ? QStringLiteral("Only virtual controller visible")
                       : QStringLiteral("Plug controller into PC - no live reports"));
            controllerInputSource_ = rawInputPresent_ ? QStringLiteral("RawInput device present") : QStringLiteral("None");
            controllerDeviceDetail_ = rawInputPresent_
                ? QStringLiteral("%1 detected; waiting for live input report").arg(rawInputLabel_.isEmpty() ? QStringLiteral("PlayStation HID pad") : rawInputLabel_)
                : QStringLiteral("No controller device");
            setControllerLifecycle(
                selected.physicalSonyVisible ? ControllerLifecycleState::PhysicalPresentNoLive : ControllerLifecycleState::NoPhysical,
                status);
            notifyControllerStatusAtHumanCadence(nowMs);
            return;
        }

        physicalPadConnected_ = true;
        physicalPadLive_ = true;
        lastPhysicalSeenMs_ = nowMs;
        physicalMissingSinceMs_ = 0;
        physicalMissingGraceWarned_ = false;
        controllerInputSource_ = selected.device.source;
        controllerDeviceDetail_ = selected.device.detail;
        const QString selectedPhysicalPath = selected.device.source == QLatin1String("RawInput")
            ? rawInputDevicePath_
            : QString();
        const QString selectedPhysicalKind = selected.device.label;
        const bool lightbarRouteChanged = activePhysicalDevicePath_ != selectedPhysicalPath
            || activePhysicalDeviceKind_ != selectedPhysicalKind;
        activePhysicalDevicePath_ = selectedPhysicalPath;
        activePhysicalDeviceKind_ = selectedPhysicalKind;
        if (lightbarRouteChanged) {
            requestControllerLightbarRefresh();
        }

        // Windows/Steam Input may translate controller face buttons and D-pad
        // presses into keyboard or mouse messages for the focused Orion window.
        // Tag a short interval after genuine physical-pad activity so the
        // native event filter can consume only those mapped UI messages while
        // leaving ordinary keyboard/mouse input untouched.
        const ControllerState& physicalState = selected.device.state;
        const SquareUpAuditPhase squareUpAuditPhase =
            squareUpAuditTracker_.observe(physicalState.square());
        const bool squareUpDeliveryAudit = squareUpAuditPhase != SquareUpAuditPhase::None;
        const bool controllerPressEdge = controllerUiActivityPressEdge(
            physicalState.buttons, physicalState.dpad, physicalState.l2, physicalState.r2,
            previousControllerUiState_.buttons, previousControllerUiState_.dpad,
            previousControllerUiState_.l2, previousControllerUiState_.r2);
        previousControllerUiState_ = physicalState;
        if (controllerPressEdge) {
            controllerUiGuardUntilMs_ = nowMs + kControllerUiGuardMs;
        }
        // [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] Level-triggered pointer guard.
        // A mapped pointer is driven by a HELD stick, not by an edge, so this
        // deliberately re-arms on every poll the stick is deflected instead of
        // only on the transition. The 4 ms poll keeps it continuously open while
        // the owner is actually playing, and it closes on its own
        // kControllerUiPointerGuardMs after the stick recenters.
        if (controllerUiPointerActivity(
                physicalState.leftStickX, physicalState.leftStickY,
                physicalState.rightStickX, physicalState.rightStickY)) {
            controllerUiPointerGuardUntilMs_ = nowMs + kControllerUiPointerGuardMs;
        }

        const bool streamActive = remoteRunning_ || chiakiEmbedStatus_ == QLatin1String("Embedded");

        // Wake the reader on the FIRST physical shot-input frame. AutomationEngine
        // intentionally waits through a hold debounce before it owns the control, but
        // using that later transition as the detector's wake-up loses the meter's early
        // rise. This edge carries no fire authority; beginShot still emits the scoped,
        // tokenized pose_arm used by the release engine. Both RS-up (Go-To) and RS-down
        // shot gestures qualify, while diagonal/horizontal dribbles do not.
        const auto inputCfg = automation_.config();
        const bool transportRecoveryWasActive =
            shotIntentEdgeTracker_.transportRecoveryActive();
        const ShotIntentEdges intent = shotIntentEdgeTracker_.update(
            physicalState,
            inputCfg.stickUpThreshold, inputCfg.stickDownThreshold,
            inputCfg.gotoLateralMaxRatio);
        // Feed the raw physical Square HELD state to the meter delay actuator.
        // This is the CAUSAL gate: it fires at t=0, ~300 ms before the meter
        // renders, so the delay ramp completes before the read window opens.
        meterDelay_.setPhysicalSquareHeld(physicalState.square());
        // [PRESSED OVERLAY 2026-08-08] Same raw sample, same tick: local
        // "PRESSED" badge acknowledgment. Display-only, no authority.
        syncPressedOverlay(physicalState.square());
        if (transportRecoveryWasActive
            && !shotIntentEdgeTracker_.transportRecoveryActive()) {
            appendLog(QStringLiteral(
                "Physical controller route recovered after three selected-device "
                "neutral reports; a new shot press is now required."));
            syncEngineArmed();
        }
        // [ORION_RHYTHM_STICK_PULL 2026-09-15] One spelling, defined beside the edges themselves
        // (orion::shotIntentSourceLabel) so the epoch line and the shot-gate arm can never
        // disagree about what an RS pull-down is called.
        const QString intentSource = QString::fromLatin1(shotIntentSourceLabel(intent));
        quint64 physicalShotEpoch = 0;
        double physicalPressWallMsEpoch = 0.0;
        if (intent.any()) {
            if (intent.stickUp || intent.stickDown) {
                // Edge-only shot gestures (Go-To RS up/down) hold no button, so
                // the Square-held feed above never sees them. Latch the meter
                // delay engagement for minEngagedMs from this raw edge.
                meterDelay_.notifyPhysicalShotEdge();
            }
            ++physicalShotEpochCounter_;
            if (physicalShotEpochCounter_ == 0) {
                ++physicalShotEpochCounter_; // zero is the invalid wire sentinel
            }
            physicalShotEpoch = physicalShotEpochCounter_;
            // Observe the edge before logging/IPC work. This stamp is diagnostics
            // only; engine monotonic clocks and release authority remain unchanged.
            physicalPressWallMsEpoch = double(QDateTime::currentMSecsSinceEpoch());
            // This assignment must precede AutomationEngine::process() below. A detector
            // completion from the prior shot may arrive at any point in this GUI tick.
            // intent.square certifies a debounced physical Square DOWN edge (three clean
            // UP polls required first) and powers the engine's stale-latch canary.
            automation_.setPhysicalShotEpoch(physicalShotEpoch, intent.square);
            // Exactly one bounded forensic record per physical edge. This proves the
            // originating control state without adding per-poll log pressure or granting
            // any timing/fire authority to diagnostics.
            // [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16 owner] APPEND-ONLY: the existing fields
            // keep their exact spelling and order (offline tooling joins on this line's
            // timestamp), and one fact about this press is added at the end. It is the engine's
            // OWN predicate, asked on the same physical sample in the same tick, so the line can
            // never claim a shaping process() did not perform -- or miss one it did.
            appendLog(QStringLiteral(
                "Physical shot epoch: epoch=%1 intent=%2 route=%3 raw_button_mask=0x%4 "
                "ls=(%5,%6) rs=(%7,%8) l2=%9 r2=%10 sprint_released=%11")
                          .arg(physicalShotEpoch)
                          .arg(intentSource)
                          .arg(selected.device.source)
                          .arg(static_cast<unsigned int>(physicalState.buttons),
                               4, 16, QLatin1Char('0'))
                          .arg(physicalState.leftStickX)
                          .arg(physicalState.leftStickY)
                          .arg(physicalState.rightStickX)
                          .arg(physicalState.rightStickY)
                          .arg(static_cast<unsigned int>(physicalState.l2))
                          .arg(static_cast<unsigned int>(physicalState.r2))
                          .arg(automation_.sprintReleaseWouldEngage(physicalState) ? 1 : 0));
            if (intent.square
                && shotIntentEdgeTracker_.lastSquareEdgeUsedDeliveredRelease()) {
                appendLog(QStringLiteral(
                    "RAPID SQUARE REARM: epoch=%1 proof=delivered_release_plus_two_up_polls "
                    "action=fresh_press_forwarded")
                              .arg(physicalShotEpoch));
            }
            // [ORION_PRESS_DELIVERY_AUDIT 2026-08-30] Owner invariant: a physical press must
            // always be able to reach the console. When it structurally CANNOT — no Running
            // input session (in capture-card mode the HDMI video keeps playing, so the game
            // looks alive from the chair while every input is dead), or the direct pipe is
            // down while its route is authoritative — say so AT THE PRESS. Previously this
            // failure was only diagnosable as an absence (an epoch line followed by nothing):
            // 31 of 185 square-edge epochs across 08-28..08-30 landed in exactly such windows
            // (e.g. 08-28 21:48:30-21:50:48, session Disconnected, 24 undeliverable presses).
            // One line per epoch; epochs are already debounced edges, so this cannot flood.
            const RemotePlayState pressSessionState = remotePlay_.state();
            const bool pipeEnabledAtPress = orionInput_.enabled();
            const bool pipeConnectedAtPress = orionInput_.connected();
            // pressUndeliverable() is THE shared predicate: this forensic line, the on-screen
            // input-dead overlay (refreshInputDeliveryState), and the press latch below all
            // evaluate the same function, so the log and the screen can never disagree.
            if (pressUndeliverable(pressSessionState, pipeEnabledAtPress,
                                   pipeConnectedAtPress)) {
                appendLog(QStringLiteral(
                    "PRESS UNDELIVERABLE: epoch=%1 intent=%2 session_state=%3 pipe_enabled=%4 "
                    "pipe_connected=%5 vpad=%6 - no live input route; this press cannot reach "
                    "the console")
                              .arg(physicalShotEpoch)
                              .arg(intentSource)
                              .arg(static_cast<int>(pressSessionState))
                              .arg(pipeEnabledAtPress ? 1 : 0)
                              .arg(pipeConnectedAtPress ? 1 : 0)
                              .arg(controller_.isConnected() ? 1 : 0));
                // [ORION_INPUT_DEAD_UX] Same tick, same predicate: latch the overlay on (a
                // press into dead input proves the player believes they are connected, even
                // outside session intent), and — when the bounded retry budget is already
                // exhausted — let the press earn ONE extra throttled reconnect attempt: a
                // Square press is the strongest possible "I expect to be connected" signal.
                lastUndeliverablePressMs_ = QDateTime::currentMSecsSinceEpoch();
                if (inputSessionIntentActive_ && !safeModeActive_
                    && !remotePlayTeardownActive_
                    && !applicationShutdownActive(applicationShutdownPhase_)
                    && inputSessionAutoRetryEnabled()
                    && inputRetryPendingAttempt_ == 0) {
                    const InputSessionRetryDecision pressDecision =
                        inputRetryPlanner_.onUndeliverablePress(lastUndeliverablePressMs_);
                    if (pressDecision.retry) {
                        appendLog(QStringLiteral(
                            "Undeliverable press with exhausted retry budget — granting one "
                            "press-triggered reconnect attempt (throttled to one per %1 s).")
                                      .arg(InputSessionRetryPlanner::kPressRetryThrottleMs
                                           / 1000));
                        scheduleInputSessionRetry(pressDecision,
                                                  inputRetryPlanner_.lastClass());
                    }
                }
                refreshInputDeliveryState();
            }
        }
        // Edge-only transaction identity. ShotIntentEdgeTracker emits `square`
        // once per debounced physical DOWN, so every non-zero epoch below can
        // produce exactly one delivery record without adding held-button log or
        // transport pressure.
        const bool squareDownDeliveryAudit = intent.square && physicalShotEpoch != 0;
        // Arm on ANY live detection feed, not just a live Chiaki input session:
        // in capture-card mode the meter comes from HDMI, so a failed/absent
        // Remote Play session must never bench the reader. See
        // orion::meterGateArmAllowed() for the incident this encodes.
        const bool armAllowed = orion::meterGateArmAllowed(
            remoteRunning_,
            chiakiEmbedStatus_ == QLatin1String("Embedded"),
            capturePreviewActive_,
            automationSecurityAllowed(),
            static_cast<unsigned int>(physicalShotEpoch));
        if (armAllowed) {
            // [ORION_SHOT_GATE_TYPE 2026-09-15] The type travels WITH the arm. This poll is the
            // one that carried the physical edge, and AutomationEngine::process() below latches
            // pendingSquareShotType_ from this identical ControllerState, so asking the engine's
            // own classifier here yields exactly the label the engine goes on to use -- one
            // function, one config, no second implementation to drift. Without it the sidecar's
            // meter-onset expectation is the union of every shot type (~1.05 s); with it, ~0.5 s.
            remotePlay_.armMeterGate(
                intentSource, physicalShotEpoch,
                automation_.classifyPhysicalShotType(physicalState, intent.square),
                automation_.rhythmReleaseConfigured(), physicalPressWallMsEpoch);
        } else if (physicalShotEpoch != 0) {
            // NAME THE REFUSAL. This failure was previously diagnosable only by
            // the ABSENCE of a "shot_gate_arm send" line next to a "Physical
            // shot epoch" line -- i.e. by noticing something that was not there.
            // Throttled so a long unarmed stretch cannot flood the ring.
            static qint64 lastArmRefusalLogMs = 0;
            const qint64 nowRefusalMs = QDateTime::currentMSecsSinceEpoch();
            if (nowRefusalMs - lastArmRefusalLogMs >= 5000) {
                lastArmRefusalLogMs = nowRefusalMs;
                appendLog(QStringLiteral(
                    "Meter-gate arm SUPPRESSED (remote=%1 embedded=%2 preview=%3 security=%4) "
                    "- the reader will not see this shot")
                        .arg(remoteRunning_ ? 1 : 0)
                        .arg(chiakiEmbedStatus_ == QLatin1String("Embedded") ? 1 : 0)
                        .arg(capturePreviewActive_ ? 1 : 0)
                        .arg(automationSecurityAllowed() ? 1 : 0));
            }
        }

        // [DPAD-UP DEFENSE HOTKEY 2026-08-08] Owner-requested physical toggle for
        // the RUNTIME manual defense flag (MeterDelayController::
        // setDefenseModeManualActive), observed on the SAME selected-device report
        // the shot-intent tracker just consumed. It previously flipped the
        // PERSISTED meterDelayBypassOnDefense setting, which both rewrote the
        // engage policy mid-session and survived restarts — a customer could come
        // back to a silently-bypassed delay. The flag is non-persistent (resets to
        // offense/apply on every session and launch), touches no settings.json
        // key, and never revokes route attestation (the ramp it triggers disarms
        // as delay-gate-only via readyForArm(), task #44's split). Passive by
        // construction: the report is neither consumed nor rewritten, so D-pad Up
        // still mirrors to the console for menu navigation, and this branch holds
        // no shot/fire authority. Gated on a CONNECTED session (Running, or an
        // embedded chiaki window) — never Connecting, idle, or the launcher — and
        // the gate suppresses emission only, so a press held from the launcher
        // cannot fire retroactively at connect. Additionally gated on the "Defense
        // bypass" master switch and on Meter Delay itself being enabled: with
        // either off the hotkey is deliberately inert (Option B semantics — see
        // MeterConfigPanel.qml).
        const bool dpadUpHeld =
            (physicalState.buttons & XINPUT_GAMEPAD_DPAD_UP) != 0;
        const bool bypassHotkeySessionConnected =
            remotePlay_.state() == RemotePlayState::Running
            || chiakiEmbedStatus_ == QLatin1String("Embedded");
        if (meterDelayBypassHotkeyTracker_.update(
                dpadUpHeld, bypassHotkeySessionConnected)
            && config_.data().meterDelayEnabled
            && config_.data().meterDelayBypassOnDefense) {
            const bool defenseOn = !meterDelay_.defenseModeManualActive();
            meterDelay_.setDefenseModeManualActive(defenseOn);
            appendLog(QStringLiteral("Meter delay defense mode: %1 (D-pad Up) => %2")
                          .arg(defenseOn ? QStringLiteral("ON") : QStringLiteral("OFF"),
                               defenseOn ? QStringLiteral("bypassing")
                                         : QStringLiteral("applying")));
            // The engagement-decision record. The manual flag is now the single
            // runtime gate, so its edges ARE the decision points (the old
            // per-shot-cycle line logged a signal that no longer participates).
            appendLog(QStringLiteral("Meter delay policy: bypass=%1 => %2")
                          .arg(defenseOn ? 1 : 0)
                          .arg(defenseOn ? QStringLiteral("bypassing")
                                         : QStringLiteral("applying")));
        }

        if (!controller_.isConnected() && streamActive && !virtualConnectInProgress_
            && nowMs - lastVirtualConnectAttemptMs_ >= 1500) {
            lastVirtualConnectAttemptMs_ = nowMs;
            appendLog(QStringLiteral("Physical controller present but virtual pad is disconnected - retrying connect."));
            connectVirtualController();
        }

        const QString route = QStringLiteral("%1 %2 -> %3 -> Chiaki")
                                  .arg(selected.device.source,
                                       selected.device.label,
                                       controller_.isConnected() ? controller_.backendName() : QStringLiteral("virtual not connected"));
        if (lastControllerRoute_ != route) {
            lastControllerRoute_ = route;
            appendLog(QStringLiteral("Controller route: %1").arg(route));
            // WHY this route won (one key=value line; a 5-second relaunch confirms the
            // RawInput pick from the log alone). rawPresent = Sony HID pad in RawInput
            // enumeration; rawLive = fresh WM_INPUT report <=750ms old.
            appendLog(QStringLiteral("Route decision: selected=%1 rawPresent=%2 rawLive=%3 rawAgeMs=%4 "
                                     "xinputPhysLive=%5 winmmSonyVid=%6")
                          .arg(selected.device.source)
                          .arg(rawInputPresent_ ? 1 : 0)
                          .arg(rawInputLive ? 1 : 0)
                          .arg(lastRawInputMs_ > 0 ? QString::number(rawReportAgeMs) : QStringLiteral("-"))
                          .arg(xinputPhysicalLive ? 1 : 0)
                          .arg(lastWinMmSonyVid_ ? 1 : 0));
            // Stale-HidHide signature (root cause of the 2026-07-06 WinMM regression): the
            // pad ARRIVED while the HidHide cloak was ON, so Windows never registered its
            // HID gamepad collection with RawInput — turning the cloak off does NOT
            // retroactively register it. WinMM still reads the pad, so the selector can
            // only fall back. Re-plugging (or pnputil /restart-device on the HID node)
            // re-arrives the device and RawInput takes over automatically.
            if (selected.device.source == QLatin1String("WinMM") && lastWinMmSonyVid_
                && !rawInputPresent_) {
                appendLog(QStringLiteral("WARNING: Sony pad visible to WinMM but ABSENT from RawInput "
                                         "enumeration (stale HidHide cloak: pad arrived while cloaked). "
                                         "Re-plug the DualSense or restart its HID device; RawInput is "
                                         "preferred automatically once visible."));
            }
        }

        const bool hasVirtual = controller_.isConnected();
        if (shotIntentEdgeTracker_.transportRecoveryActive()) {
            setControllerLifecycle(
                ControllerLifecycleState::ControllerFault,
                QStringLiteral("Controller input recovering; release Square and center the right stick"));
        } else if (inputRouteAwaitingRecovery_ || preciseFireDeliveryFault_) {
            setControllerLifecycle(
                ControllerLifecycleState::ControllerFault,
                QStringLiteral("Controller output route recovering; automation is disarmed"));
        } else if (hasVirtual && streamActive) {
            setControllerLifecycle(ControllerLifecycleState::StreamingMirror, QStringLiteral("Streaming physical -> virtual"));
        } else if (hasVirtual) {
            setControllerLifecycle(ControllerLifecycleState::VirtualReady, QStringLiteral("Virtual controller ready"));
        } else {
            setControllerLifecycle(ControllerLifecycleState::PhysicalLive, QStringLiteral("Physical controller live"));
        }

        // Reconcile availability before AutomationEngine::process() can mint a
        // deadline. The post-submit reconciliation below remains the stronger
        // proof, but it is too late to stop this tick from copying an old-route
        // token into the precise worker.
        const LatencyControllerRoute liveRouteBeforeProcess =
            PreciseFirePolicy::liveControllerRoute(
                directInputWriteAllowed(remotePlay_.state(), remotePlay_.inputRecoveryPending()),
                orionInput_.enabled(), orionInput_.connected(),
                controller_.isConnected(), controller_.isDs4Backend());
        if (latencyCacheRouteAttestation_.active()
            && !latencyCacheRouteAttestation_.matchesActiveRoute(
                liveRouteBeforeProcess)) {
            const quint64 revokedGeneration =
                latencyCacheRouteAttestation_.generation();
            const LatencyControllerRoute revokedRoute =
                latencyCacheRouteAttestation_.route();
            latencyCacheRouteAttestation_.revoke();
            automation_.setControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
            appendLog(QStringLiteral(
                "Controller route changed before automation process: generation=%1 "
                "attested=%2 live=%3; scheduled authority revoked.")
                          .arg(revokedGeneration)
                          .arg(static_cast<int>(revokedRoute))
                          .arg(static_cast<int>(liveRouteBeforeProcess)));
        }

        // Defense Mode is deferred to a future update — its physical D-pad trigger is
        // disabled here so it can never disarm shot automation. toggleDefenseMode() and
        // applyDefenseAssists() are kept dormant (not deleted) for when it ships again.

        // Sub-tick scheduler, step 1: consume the worker outcome BEFORE process(). A failed
        // physical write is never confirmed into the engine: revoke the token, cancel any
        // pending grade, and fail closed until a real route proves recovery.
        bool failClosedNeutralThisTick =
            shotIntentEdgeTracker_.transportRecoveryActive();
        if (fireThread_) {
            quint64 failedToken = 0;
            QString failedDetail;
            if (fireThread_->takeFailure(&failedToken, &failedDetail)) {
                quint64 expected = failedToken;
                lastArmedFireToken_.compare_exchange_strong(
                    expected, 0, std::memory_order_acq_rel);
                preciseFireDeliveryFault_ = true;
                preciseFireRecoveryNeutralFrames_ = 0;
                automation_.setArmed(false);
                QString userNotice;
                if (pendingSubmitSeq_ >= 0) {
                    automation_.cancelPostReleaseGrade(pendingSubmitSeq_);
                    userNotice = userReleaseTracker_.fail(
                        pendingSubmitSeq_, UserReleaseFailureReason::PreciseWriteFailed);
                    pendingSubmitSeq_ = -1;
                } else {
                    userNotice = UserFacingReleaseTracker::failureNotice(
                        UserReleaseFailureReason::PreciseWriteFailed);
                }
                releaseMarkerDeliveryGate_.reset();
                pendingSubmitPhysicalShotEpoch_ = 0;
                pendingSubmitShotAttempt_ = 0;
                pendingSubmitScheduleToken_ = 0;
                pendingSubmitRouteGeneration_ = 0;
                pendingSubmitRoute_ = LatencyControllerRoute::None;
                appendCustomerEvent(userNotice);
                pendingSubmitDeliveryStage_ = PreciseFireDeliveryStage::None;
                pendingSubmitFireToken_ = 0;
                pendingSubmitTransportSeq_ = 0;
                pendingSubmitSnapshot_ = {};
                pendingSubmitFrameGrid_ = {};
                confirmedPreciseFireStage_ = PreciseFireDeliveryStage::None;
                confirmedPreciseFireToken_ = 0;
                confirmedPreciseFireTransportSeq_ = 0;
                confirmedPreciseFireSnapshot_ = {};
                automation_.reset();
                shot_ = automation_.context();
                shotState_ = holdStateText(shot_.state);
                observeBotOwnership(shot_);
                outputState_ = QStringLiteral("Release not submitted");
                failClosedNeutralThisTick = true;
                setControllerLifecycle(
                    ControllerLifecycleState::ControllerFault,
                    QStringLiteral("Precise release NOT submitted; controller route recovery required"));
                appendLog(QStringLiteral("Precise release NOT_SUBMITTED: token=%1 %2; "
                                         "automation disarmed and grade cancelled.")
                              .arg(failedToken)
                              .arg(failedDetail.left(180)));
            } else {
                quint64 firedToken = 0;
                double firedActualMs = -1.0;
                PreciseFireDeliveryStage firedStage = PreciseFireDeliveryStage::None;
                uint32_t firedTransportSeq = 0;
                PreciseFireDeliverySnapshot firedSnapshot;
                if (fireThread_->takeFired(
                        &firedToken, &firedActualMs, &firedStage, &firedTransportSeq,
                        &firedSnapshot)) {
                    confirmPreciseFire(
                        firedToken, firedActualMs, firedStage, firedTransportSeq,
                        firedSnapshot);
                }
            }
        }

        const RemapConfig recoveryCfg = automation_.config();
        const bool physicalShotInputsNeutral = routeRecoveryShotInputsNeutral(
            selected.device.state,
            recoveryCfg.stickUpThreshold,
            recoveryCfg.stickDownThreshold);
        const bool routeRecoveryGateActive = preciseFireDeliveryFault_
            || inputRouteAwaitingRecovery_;
        if (routeRecoveryGateActive && physicalShotInputsNeutral) {
            preciseFireRecoveryNeutralFrames_ = std::min(
                preciseFireRecoveryNeutralFrames_ + 1, 3);
        } else {
            preciseFireRecoveryNeutralFrames_ = 0;
        }
        const bool routeRecoveryProbe = routeRecoveryGateActive
            && preciseFireRecoveryNeutralFrames_ >= 3;

        ControllerState output = selected.device.state;
        bool tempoMovementCommitRequired = false;
        quint64 tempoMovementCommitGeneration = 0;
        if (automationSecurityAllowed()) {
            output = automation_.process(selected.device.state);
            tempoMovementCommitRequired = automation_.tempoMovementCommitPending();
            tempoMovementCommitGeneration = automation_.tempoMovementCommitGeneration();
        } else {
            // Auth/lease loss must revoke the worker's copied deadline before
            // resetting only the engine-side schedule.
            disarmPreciseFire();
            automation_.reset();
            shot_ = automation_.context();
            shotState_ = holdStateText(shot_.state);
            observeBotOwnership(shot_);
            outputState_ = QStringLiteral("Security Locked");
            failClosedNeutralThisTick = true;
        }
        if (defenseModeActive_ && !automation_.tempoMovementOwned()) {
            applyDefenseAssists(output);
        }
        output.lightbarSet = false;

        // Reconcile the controller-owned handshake before deciding whether this neutral tick may
        // create a proof. AutomationEngine revokes its expected token on estimator/source/session
        // resets; observing that mismatch here prevents any delayed old-source echo from completing.
        // Handshake expiry must share the engine's steady/monotonic timebase. The surrounding
        // controller bookkeeping uses wall time, which can jump when Windows synchronizes its clock.
        const qint64 latencyAttestationNowMs =
            static_cast<qint64>(automation_.engineNowMs());
        const bool latencyAttestationRuntimeReady =
            remotePlay_.state() == RemotePlayState::Running
            && controller_.isConnected() && automation_.armed();
        if (latencyCacheRouteAttestation_.active()
            && (!latencyAttestationRuntimeReady
                || !automation_.controllerDeliveryRouteAttestationExpected(
                    latencyCacheRouteAttestation_.generation(),
                    latencyCacheRouteAttestation_.route()))) {
            latencyCacheRouteAttestation_.revoke();
            automation_.setControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
        }
        if (latencyCacheRouteAttestation_.pending()
            && automation_.restoredLatencyAttestationEchoMatches(
                latencyCacheRouteAttestation_.generation(),
                latencyCacheRouteAttestation_.route(),
                latencyCacheRouteAttestation_.scopeEpoch())) {
            const quint64 echoedGeneration = latencyCacheRouteAttestation_.generation();
            const LatencyControllerRoute echoedRoute = latencyCacheRouteAttestation_.route();
            const quint64 echoedScopeEpoch = latencyCacheRouteAttestation_.scopeEpoch();
            if (latencyCacheRouteAttestation_.completeExactEcho(
                    echoedGeneration, echoedRoute, echoedScopeEpoch)) {
                const QString echoedDeliveryRoute = echoedRoute == LatencyControllerRoute::Pipe
                    ? QStringLiteral("pipe")
                    : (echoedRoute == LatencyControllerRoute::VigemDs4
                        ? QStringLiteral("vigem_ds4")
                        : QStringLiteral("vigem_xusb"));
                appendLog(QStringLiteral(
                    "Timing cache route confirmed by exact sidecar echo: route=%1 "
                    "generation=%2 scope_epoch=%3")
                              .arg(echoedDeliveryRoute)
                              .arg(echoedGeneration)
                              .arg(echoedScopeEpoch));
            }
        }
        const quint64 expiringAttestationGeneration =
            latencyCacheRouteAttestation_.generation();
        const LatencyControllerRoute expiringAttestationRoute =
            latencyCacheRouteAttestation_.route();
        if (latencyCacheRouteAttestation_.retryPendingIfTimedOut(
                latencyAttestationNowMs)) {
            const QString retryDeliveryRoute = expiringAttestationRoute
                    == LatencyControllerRoute::Pipe
                ? QStringLiteral("pipe")
                : (expiringAttestationRoute == LatencyControllerRoute::VigemDs4
                    ? QStringLiteral("vigem_ds4") : QStringLiteral("vigem_xusb"));
            remotePlay_.attestLatencyControllerRoute(
                retryDeliveryRoute, expiringAttestationGeneration);
            appendLog(QStringLiteral(
                "Timing cache route proof receipt timed out; retrying the same proof: "
                "route=%1 generation=%2 retry=%3")
                          .arg(retryDeliveryRoute).arg(expiringAttestationGeneration)
                          .arg(latencyCacheRouteAttestation_.retryCount()));
        }
        const bool latencyRouteAttestationProbe =
            latencyCacheRouteAttestation_.needsNeutralProof()
            && pendingSubmitSeq_ < 0
            && shot_.state == HoldState::Idle
            && PreciseFirePolicy::latencyRouteAttestationEligible(
                remotePlay_.state() == RemotePlayState::Running,
                controller_.isConnected(), automation_.armed(),
                !preciseFireDeliveryFault_ && !inputRouteAwaitingRecovery_,
                selected.device.state, output);
        if (failClosedNeutralThisTick) {
            output = ControllerState{};
            output.lightbarSet = false;
        }
        // Sub-tick scheduler, step 2: the engine committed a release deadline inside the
        // horizon — arm the fire thread with the exact release output (this tick's held
        // state with the shot button cleared). If the engine cleared its deadline (fired
        // in-tick / aborted), disarm any stale arm.
        if (fireThread_) {
            const double schedDeadlineMs = automation_.scheduledFireDeadlineMs();
            const quint64 schedToken = automation_.scheduledFireToken();
            const double schedAuthorityExpiryMs = automation_.scheduledFireAuthorityExpiryMs();
            const quint64 armedToken = lastArmedFireToken_.load(std::memory_order_acquire);
            // [ORION_ROLLING_LEASE] The arm branch below only runs for a NEW token, so an armed
            // token never saw another lease update: it ran to its deadline on the copy taken at
            // arm time while the engine's own lease kept rolling forward with each genuine frame.
            // Push the current engine lease across on every tick the token stays armed. The call
            // only ever EXTENDS and only for this exact token, so it cannot retire a token early
            // and cannot leak a lease across shots; the deadline is untouched either way.
            if (schedDeadlineMs >= 0.0 && schedToken == armedToken && schedToken != 0) {
                fireThread_->refreshAuthority(
                    schedToken, schedAuthorityExpiryMs, automation_.engineNowMs());
                // [ORION_BLIND_WAITER 2026-09-15] The NO METER blind token is armed at the press
                // and can therefore wait a whole hold. Keep the packet it will press current with
                // this tick's owned output (which also carries a mid-hold shot-type upgrade's
                // release edge), so early arming buys tick-independence without paying for it in
                // a stale pad snapshot. Meter/pose/vision tokens are untouched: they arm inside
                // their own horizon and are re-armed, not refreshed, when anything changes.
                if (automation_.scheduledFireIsBlindInputTimed()) {
                    ControllerState refreshOutput = output;
                    const ShotContext refreshContext = automation_.context();
                    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] The refreshed packet and the fence's
                    // copy of the style are written from the same read, so they cannot diverge.
                    const TempoReleaseStyle refreshStyle =
                        tempoReleaseStyleFromString(automation_.config().tempoReleaseStyle);
                    // The fade argument stays at its historical default here: these controller
                    // call sites have never passed the arm-time fade latch, and changing that
                    // under a "release style" patch would silently reverse every fade's flick
                    // direction. Style only.
                    applyShotReleaseEdge(refreshOutput, refreshContext.mode,
                                         refreshContext.shotType, false, refreshStyle);
                    refreshOutput.lightbarSet = false;
                    fireThread_->refreshReleaseOutput(schedToken, refreshContext.mode,
                                                      refreshOutput, refreshStyle);
                }
            }
            {
                quint64 declineToken = 0;
                quint64 declineTotal = 0;
                double declineOverdueMs = 0.0;
                if (fireThread_->takeAuthorityDecline(
                        &declineToken, &declineOverdueMs, &declineTotal)) {
                    appendLog(QStringLiteral(
                        "PRECISE FIRE AUTHORITY DECLINE: token=%1 overdue_ms=%2 total=%3 "
                        "(evidence lease expired at the submit instant; pad not pressed)")
                                  .arg(declineToken)
                                  .arg(declineOverdueMs, 0, 'f', 3)
                                  .arg(declineTotal));
                }
            }
            if (schedDeadlineMs >= 0.0 && schedToken != armedToken) {
                ControllerState fireOutput = output;
                const ShotContext fireContext = automation_.context();
                // Style only: the fade argument keeps its historical default (see the refresh
                // site above) so "flick" is byte-identical to the pre-2026-09-15 build.
                applyShotReleaseEdge(
                    fireOutput, fireContext.mode, fireContext.shotType, false,
                    tempoReleaseStyleFromString(
                        automation_.config().tempoReleaseStyle));
                fireOutput.lightbarSet = false;
                // [ORION_TICK_LOCK] Phase-align the absolute fire deadline so the release LANDS mid-
                // console-tick (avoids the ±8.3ms 60Hz quantization edges), using the surfaced next-tick
                // eta telemetry (tickerLatencyMs). Default OFF -> the raw scheduled deadline is armed
                // unchanged. The fire path already uses this absolute-deadline sub-tick scheduler, so the
                // nudge composes with it (bounded to ±half a tick; applied once per token at arm time).
                const RemapConfig fireCfg = automation_.config();
                const double engineNowMs = automation_.engineNowMs();
                double armDeadlineMs = schedDeadlineMs;
                bool tickNudged = false;
                const double verifiedTickEtaMs = tickerLatencyMs();
                if (!fireCfg.inputTimedEnabled && fireCfg.tickLockEnabled && tickPhaseVerified()
                    && verifiedTickEtaMs > 0.0) {
                    const double nudged = AutomationEngine::tickAlignedFireDeadlineMs(
                        schedDeadlineMs, engineNowMs, verifiedTickEtaMs,
                        fireCfg.consoleTickIntervalMs);
                    if (nudged != armDeadlineMs) {
                        armDeadlineMs = nudged;
                        tickNudged = true;
                    }
                }
                const PreciseFireArmResult armResult = fireThread_->arm(
                    schedToken, armDeadlineMs, schedAuthorityExpiryMs,
                    fireContext.mode,
                    fireOutput,
                    automation_.scheduledFireRouteGeneration(),
                    automation_.scheduledFireRoute(),
                    tempoReleaseStyleFromString(fireCfg.tempoReleaseStyle));
                if (armResult == PreciseFireArmResult::EngineDisarmed) {
                    // A watchdog/defense disarm won while this GUI tick was in
                    // flight. Revoke the engine token and preserve its neutral.
                    lastArmedFireToken_.store(0, std::memory_order_release);
                    automation_.cancelScheduledFire(schedToken);
                    output = ControllerState{};
                    output.lightbarSet = false;
                } else if (armResult == PreciseFireArmResult::RouteRejected) {
                    // Route availability changed after process() created the
                    // token. Do not let the new route inherit its deadline.
                    preciseFireDeliveryFault_ = true;
                    preciseFireRecoveryNeutralFrames_ = 0;
                    automation_.setArmed(false);
                    disarmPreciseFire();
                    automation_.cancelScheduledFire(schedToken);
                    output = ControllerState{};
                    output.lightbarSet = false;
                    appendLog(QStringLiteral(
                        "Precise-fire arm rejected: controller route/generation changed; "
                        "automation disarmed for neutral route re-attestation."));
                } else if (armResult == PreciseFireArmResult::WindowRejected) {
                    // Lock handoff or tick phase consumed the finite evidence
                    // lease. Fence the untracked token but keep the owned HOLD
                    // packet; a full neutral here would be an ungraded release.
                    lastArmedFireToken_.store(0, std::memory_order_release);
                    automation_.cancelScheduledFire(schedToken);
                    appendLog(QStringLiteral(
                        "Precise-fire arm skipped: token=%1 deadline/authority window expired; "
                        "owned output preserved for the next authoritative decision.")
                                  .arg(schedToken));
                } else if (armResult == PreciseFireArmResult::MailboxBusy) {
                    // A one-slot worker outcome or older arm must never be
                    // overwritten by a newer token. Fail closed and recover the
                    // route after the physical shot controls return neutral.
                    preciseFireDeliveryFault_ = true;
                    preciseFireRecoveryNeutralFrames_ = 0;
                    automation_.setArmed(false);
                    disarmPreciseFire();
                    automation_.cancelScheduledFire(schedToken);
                    output = ControllerState{};
                    output.lightbarSet = false;
                    appendLog(QStringLiteral(
                        "Precise-fire arm blocked: worker mailbox/arm still occupied; "
                        "automation disarmed for route recovery."));
                }
                // [CONCURRENCY N1] The tick-lock nudge is applied HERE at arm time, so the engine
                // still holds the un-nudged schedFireDeadlineMs_. When the nudge pulls the fire
                // EARLIER, the fire thread presses before that stored deadline; a fresh meter sample
                // landing in the gap before the next GUI-tick confirm slips past the engine's grace
                // + earlier-only reschedule guards (which key off the un-nudged, later deadline) and
                // re-arms the shot -> a SECOND physical press. Reconcile the engine's stored deadline
                // with what was actually armed so those guards reflect the true fire instant. Only
                // when the nudge moved the deadline -> byte-identical when tick-lock is OFF.
                if (armResult == PreciseFireArmResult::Armed && tickNudged) {
                    automation_.noteArmedFireDeadlineMs(schedToken, armDeadlineMs);
                }
                // NEW add-only line (existing lines are parsed — never edit one, add one):
                // arm-time observability for the fire path. eta_ms = how far the armed deadline
                // sits ahead of the engine clock AT ARM TIME. Lets offline mining split a late
                // submit (Scheduled fire: deltaMs) into arm-lateness (token born/handed over
                // with eta already tiny — GUI-tick scheduling) vs wakeup-lateness (timer
                // quantization in the worker; see PreciseWaitTimer.h). One line per arm.
                if (armResult == PreciseFireArmResult::Armed) {
                    appendLog(QStringLiteral(
                        "PRECISE FIRE ARM: token=%1 eta_ms=%2 nudged=%3 hires=%4")
                                  .arg(schedToken)
                                  .arg(armDeadlineMs - automation_.engineNowMs(), 0, 'f', 3)
                                  .arg(tickNudged ? 1 : 0)
                                  .arg(fireThread_->hiresWaitActive() ? 1 : 0));
                }
            } else if (schedDeadlineMs < 0.0 && armedToken != 0) {
                fireThread_->disarm(armedToken);
                quint64 expected = armedToken;
                lastArmedFireToken_.compare_exchange_strong(
                    expected, 0, std::memory_order_acq_rel);
            }
        }
        if (forceVirtualNeutral_) {
            // Isolation test: force a neutral submit while still reading/selecting the
            // physical pad. Overrides both the physical mirror and automation output so
            // a held physical Square only reaches the game if Chiaki reads the DualSense
            // directly (a routing leak), not via Orion's virtual mirror.
            output = ControllerState{};
            output.lightbarSet = false;
            if (!forceVirtualNeutralLogged_) {
                forceVirtualNeutralLogged_ = true;
                appendLog(QStringLiteral("Controller isolation test: forcing virtual neutral output"));
            }
        }
        shot_ = automation_.context();
        shotState_ = holdStateText(shot_.state);
        observeBotOwnership(shot_);
        refreshMeterTargetEtaSnapshot();
        // Same tick, immediately after the ShotContext copy above, so every HUD
        // row shares one instant. Internally throttled to 30 Hz and short-circuits
        // to a single bool read when the overlay is off.
        refreshLiveMeterTelemetry();
        // Ownership timeline: log every shot-state transition (catches a physical press
        // that never armed = pass-through). Runs whether or not a virtual pad is present.
        logShotStateTransition(selected.device.state, output);
        // [ORION_OUTPUT_DIVERGENCE 2026-09-14 owner] "buttons sometimes are weird, it seemed like
        // it was holding L2 for me". Evidence only — this is the LAST point where the engine's
        // final output and the pad's own packet are both in hand.
        observeOutputDivergence(selected.device.state, output);
        // Freeze the branch decision for this epoch. A connection transition
        // between two independent isConnected() reads must not emit both the
        // disconnected and submitted identities (or neither).
        const bool virtualControllerConnectedAtSubmit = controller_.isConnected();
        const qint64 idleInputNowMs = static_cast<qint64>(automation_.engineNowMs());
        const bool hookDigitalRest = virtualControllerConnectedAtSubmit
            && remotePlay_.state() == RemotePlayState::Running
            && orionInput_.enabled() && orionInput_.connected()
            && shot_.state == HoldState::Idle
            && pendingSubmitSeq_ < 0
            && !routeRecoveryGateActive && !failClosedNeutralThisTick
            && !tempoMovementCommitRequired && !forceVirtualNeutral_
            && automation_.scheduledFireDeadlineMs() < 0.0
            && lastArmedFireToken_.load(std::memory_order_acquire) == 0
            && PreciseFirePolicy::controllerDigitalControlsAtRest(physicalState)
            && PreciseFirePolicy::controllerDigitalControlsAtRest(output);
        if (!hookDigitalRest) {
            hookDigitalRestSinceMs_ = -1;
        } else if (hookDigitalRestSinceMs_ < 0
                   || idleInputNowMs < hookDigitalRestSinceMs_) {
            hookDigitalRestSinceMs_ = idleInputNowMs;
        }
        if (squareUpDeliveryAudit && !virtualControllerConnectedAtSubmit) {
            appendLog(QStringLiteral(
                "Square-up route audit: latest_physical_epoch=%1 phase=%2 physical_square=0 "
                "requested_square=%3 pipe_snapshot_square=-1 pipe_snapshot_seq=0 "
                "snapshot_local_ack=0 current_delivery_stage=not_confirmed console_ack=0 "
                "engine_state=%4 engine_reason=%5 reason=virtual_disconnected")
                          .arg(physicalShotEpochCounter_)
                          .arg(squareUpAuditPhase == SquareUpAuditPhase::RawUp
                                   ? QStringLiteral("raw_up") : QStringLiteral("debounced_up"))
                          .arg(output.square() ? 1 : 0)
                          .arg(holdStateToken(shot_.state))
                          .arg(shot_.releaseReason));
        }
        if (squareDownDeliveryAudit && !virtualControllerConnectedAtSubmit) {
            // No submit API was available, so neither `output` nor any stale
            // OrionInputClient snapshot is evidence of what reached a route.
            appendLog(QStringLiteral(
                "Square-down delivery identity: physical_epoch=%1 shot_attempt=%2 "
                "square_bit=-1 delivery_source_seq=0 delivery_stage=not_confirmed "
                "local_route_ack=0 console_ack=0 packet_snapshot=unavailable "
                "ack_wait_us=0 reason=virtual_disconnected delivered_r2=-1 "
                "sprint_released=%3")
                          .arg(physicalShotEpoch)
                          .arg(shot_.armToken)
                          .arg(automation_.sprintReleaseActive() ? 1 : 0));
        }
        if (virtualControllerConnectedAtSubmit) {
            QString error;
            bool submitOk = false;
            bool virtualSubmitOk = false;
            bool hookOwnsInput = false;
            InputRouteWriteResult hookWrite = InputRouteWriteResult::Failed;
            PreciseFireDeliveryDecision activeRouteDelivery;
            OrionInputPacket squareDownPipePacket{};
            OrionInputTransactionTiming squareDownPipeTiming{};
            ControllerState squareDownVirtualOutput{};
            bool squareDownPipePacketValid = false;
            bool squareDownVirtualOutputValid = false;
            int squareUpPipeSnapshotBit = -1;
            uint32_t squareUpPipeSnapshotSeq = 0;
            bool squareUpSnapshotLocalAck = false;
            bool precisionRouteRejected = false;
            bool preciseReleaseAlreadyDelivered = false;
            bool routeBoundRelease = false;
            bool outputDigitalReleaseEdge = false;
            {
                // Sub-tick scheduler, step 3: serialize against the fire thread. If it
                // already wrote the RELEASED state for this shot and the engine hasn't
                // caught up yet (fire landed after this tick's process()), convert this
                // tick's held output to released — re-pressing on top of the fire is
                // exactly the pump-fake artifact.
                QMutexLocker submitLock(&submitMutex_);
                if (!automation_.armed() && !defenseModeActive_) {
                    output = ControllerState{};
                    output.lightbarSet = false;
                }
                if (fireThread_ && fireThread_->firedUnconsumed()) {
                    applyShotReleaseEdge(
                        output, shot_.mode, shot_.shotType, false,
                        tempoReleaseStyleFromString(
                            automation_.config().tempoReleaseStyle));
                }
                bool squareWatchdogRouteFailed = false;
                if (squareOutputWatchdogEnabled_) {
                    squareOutputWatchdog_.observePhysical(true, physicalState.square());
                    const OrionInputPacket confirmed = orionInput_.lastSent();
                    const bool confirmedSquareHeld = orionInput_.connected()
                        && orionInput_.haveSent() && confirmed.own != 0
                        && (confirmed.buttons & (1u << 2)) != 0;
                    const qint64 watchdogNowMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now().time_since_epoch()).count();
                    squareOutputWatchdog_.observeOutput(confirmedSquareHeld, watchdogNowMs);
                    if (squareOutputWatchdog_.releaseDue(
                            watchdogNowMs, automation_.squareOutputWatchdogOwnershipActive())) {
                        const bool attempted = releaseStaleSquareOutputLocked(
                            QStringLiteral("unowned_output_timeout"));
                        squareWatchdogRouteFailed = attempted && !orionInput_.connected();
                    }
                    if (squareOutputWatchdog_.suppressSquare())
                        output.buttons &= ~XINPUT_GAMEPAD_X;
                }
                outputDigitalReleaseEdge = hookReleaseRepairHavePreviousOutput_
                    && PreciseFirePolicy::controllerDigitalReleaseEdge(
                        hookReleaseRepairPreviousOutput_, output);
                hookReleaseRepairPreviousOutput_ = output;
                hookReleaseRepairHavePreviousOutput_ = true;

                // The native input pipe is the authoritative PS5 route when connected. ViGEm is
                // used only after a failure known to precede pipe acceptance; an ambiguous pending
                // write keeps desktop output neutral while OrionStream tears the session down.
                // While the pipe is healthy, keep the desktop-visible XUSB
                // target neutral so Steam Input/GameInput cannot translate PS5
                // face buttons into clicks or navigation inside Orion.
                const qint64 nowHookUs = std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()).count();
                const RemotePlayState liveInputState = remotePlay_.state();
                const bool directInputReady = directInputWriteAllowed(
                    liveInputState, remotePlay_.inputRecoveryPending())
                    && !squareWatchdogRouteFailed;
                preciseReleaseAlreadyDelivered =
                    pendingSubmitSeq_ >= 0
                    && PreciseFirePolicy::hasConfirmedPreciseDelivery(
                        pendingSubmitDeliveryStage_, pendingSubmitFireToken_);
                // Route binding is independent of precise scheduling. Immediate
                // autonomous releases carry token=0 but still depend on the exact
                // controller route whose latency was used by the timing decision.
                routeBoundRelease = pendingSubmitSeq_ >= 0
                    && pendingSubmitRouteGeneration_ != 0
                    && pendingSubmitRoute_ != LatencyControllerRoute::None;
                const PreciseFireRouteBinding precisionBinding{
                    pendingSubmitRouteGeneration_, pendingSubmitRoute_};
                const LatencyControllerRoute liveSubmitRoute =
                    PreciseFirePolicy::liveControllerRoute(
                        directInputReady,
                        orionInput_.enabled(), orionInput_.connected(),
                        controller_.isConnected(), controller_.isDs4Backend());
                const bool precisionBindingMatches = routeBoundRelease
                    && automation_.controllerDeliveryRouteAttestationExpected(
                        precisionBinding.generation, precisionBinding.route)
                    && PreciseFirePolicy::routeBindingMatches(
                        precisionBinding, liveSubmitRoute,
                        precisionBinding.generation, precisionBinding.route);
                if (routeBoundRelease && !precisionBindingMatches) {
                    // A worker-confirmed release may be mirrored only as neutral
                    // after a route switch; an unconfirmed grace fallback is
                    // rejected outright. Neither may replay its edge elsewhere.
                    precisionRouteRejected = true;
                    preciseFireDeliveryFault_ = true;
                    preciseFireRecoveryNeutralFrames_ = 0;
                    automation_.setArmed(false);
                    output = ControllerState{};
                    output.lightbarSet = false;
                    const OrionInputPacket priorPipePacket = orionInput_.lastSent();
                    if (directInputRouteCurrentlyOwned(
                            orionInput_.connected(), orionInput_.haveSent(),
                            orionInput_.haveSent() && priorPipePacket.own != 0)) {
                        releaseStaleSquareOutputLocked(QStringLiteral("controller_route_changed"));
                        if (orionInput_.connected()) // no fresh route after an ambiguous watchdog ACK
                            (void)orionInput_.sendDetailed(output, true, true);
                    }
                    if (squareDownDeliveryAudit) {
                        squareDownVirtualOutput = output;
                        squareDownVirtualOutputValid = true;
                    }
                    virtualSubmitOk = controller_.submit(output, &error);
                    hookOwnsInput = liveSubmitRoute == LatencyControllerRoute::Pipe;
                    submitOk = preciseReleaseAlreadyDelivered;
                }
                const bool exactDeliveryAckRequired =
                    PreciseFirePolicy::requiresExactDeliveryAck(
                        pendingSubmitSeq_ >= 0,
                        preciseReleaseAlreadyDelivered,
                        routeRecoveryProbe,
                        squareDownDeliveryAudit)
                    || latencyRouteAttestationProbe
                    || tempoMovementCommitRequired;
                const bool suppressDeliveredPreciseDuplicate =
                    preciseReleaseAlreadyDelivered && !routeRecoveryProbe;
                if (!precisionRouteRejected && routeBoundRelease
                    && !preciseReleaseAlreadyDelivered) {
                    // Grace-takeover remains on the route captured by the token.
                    // A failed Pipe ACK gets a neutral ViGEm submit, never a
                    // cross-route release fallback.
                    ControllerState virtualOutput;
                    virtualOutput.lightbarSet = false;
                    if (precisionBinding.route == LatencyControllerRoute::Pipe) {
                        hookWrite = orionInput_.sendDetailed(output, true, true);
                        hookOwnsInput = true;
                    } else {
                        virtualOutput = output;
                    }
                    if (squareDownDeliveryAudit) {
                        squareDownVirtualOutput = virtualOutput;
                        squareDownVirtualOutputValid = true;
                    }
                    virtualSubmitOk = controller_.submit(virtualOutput, &error);
                    activeRouteDelivery = PreciseFirePolicy::evaluateBoundDelivery(
                        hookWrite, virtualSubmitOk, precisionBinding.route);
                    submitOk = activeRouteDelivery.confirmScheduledFire;
                } else if (!precisionRouteRejected
                    && PreciseFirePolicy::shouldAttemptDirectWrite(
                        directInputReady,
                        exactDeliveryAckRequired,
                        suppressDeliveredPreciseDuplicate,
                        nowHookUs - lastFireHookWriteUs_.load(std::memory_order_relaxed),
                        kHookCoalesceUs_)) {
                    // Recovery and immediate (non-worker) releases bypass de-dup
                    // and request an exact local ACK. A worker-confirmed release
                    // stays on its proven pipe/ViGEm route for this tick instead
                    // of being duplicated or switched by the GUI mirror.
                    hookWrite = orionInput_.sendDetailed(
                        output, true, exactDeliveryAckRequired);
                }
                if (!precisionRouteRejected
                    && !(routeBoundRelease && !preciseReleaseAlreadyDelivered)) {
                    hookOwnsInput = shouldNeutralizeDesktopVirtualPad(
                        directInputReady, orionInput_.enabled(), orionInput_.connected())
                        || inputRouteMayStillOwnInput(hookWrite);
                    ControllerState virtualOutput = output;
                    // During Connecting neither route may forward gameplay input:
                    // Chiaki's pipe exists before its feedback sender, and exposing
                    // the ViGEm mirror can also leak controller buttons into desktop
                    // UI. Retained Running during recovery is not input readiness.
                    if (!directInputReady || hookOwnsInput) {
                        virtualOutput = ControllerState{};
                        virtualOutput.lightbarSet = false;
                    }
                    if (squareDownDeliveryAudit) {
                        squareDownVirtualOutput = virtualOutput;
                        squareDownVirtualOutputValid = true;
                    }
                    virtualSubmitOk = controller_.submit(virtualOutput, &error);
                    // Neutralizing the desktop pad while input is not ready is
                    // cleanup, not proof that a replacement console route works.
                    activeRouteDelivery = directInputReady
                        ? PreciseFirePolicy::evaluateDelivery(
                            hookWrite, hookOwnsInput, virtualSubmitOk)
                        : PreciseFireDeliveryDecision{};
                }
                if (squareDownDeliveryAudit && activeRouteDelivery.pipeAccepted
                    && orionInput_.haveSent()) {
                    // Snapshot while submitMutex_ still excludes the precise-fire
                    // worker. A later lastSent() read can observe a release from a
                    // different transaction and must never be joined to this epoch.
                    const OrionInputPacket accepted = orionInput_.lastSent();
                    const uint32_t localUdpStage = static_cast<uint32_t>(
                        static_cast<uint8_t>(OrionInputAckStage::LocalUdpAccepted));
                    if (accepted.seq != 0
                        && accepted.seq == orionInput_.lastAckExpectedSeq()
                        && accepted.seq == orionInput_.lastAckSourceSeq()
                        && orionInput_.lastAckStage() == localUdpStage
                        && (accepted.reserved & OrionInputMustDeliver) != 0) {
                        squareDownPipePacket = accepted;
                        squareDownPipePacketValid = true;
                        const OrionInputTransactionTiming acceptedTiming =
                            orionInput_.lastTransactionTiming();
                        if (acceptedTiming.matches(accepted.seq)) {
                            squareDownPipeTiming = acceptedTiming;
                        }
                    }
                }
                if (squareUpDeliveryAudit && orionInput_.connected()
                    && orionInput_.haveSent()) {
                    // Diagnostic-only snapshot under submitMutex_: no extra write,
                    // ACK wait, dedup bypass, epoch, or ownership decision. This may
                    // be a previously accepted no-change state, so its provenance is
                    // labelled separately from THIS tick's delivery stage below.
                    const OrionInputPacket snapshot = orionInput_.lastSent();
                    if (snapshot.own != 0 && snapshot.seq != 0) {
                        squareUpPipeSnapshotBit = (snapshot.buttons & (1u << 2)) ? 1 : 0;
                        squareUpPipeSnapshotSeq = snapshot.seq;
                        squareUpSnapshotLocalAck =
                            snapshot.seq == orionInput_.lastAckExpectedSeq()
                            && snapshot.seq == orionInput_.lastAckSourceSeq()
                            && orionInput_.lastAckStage() == static_cast<uint32_t>(
                                OrionInputAckStage::LocalUdpAccepted)
                            && (snapshot.reserved & OrionInputMustDeliver) != 0;
                    }
                }
                if (tempoMovementCommitRequired) {
                    // Linearize the LS-only movement commit at the same local
                    // delivery boundary used by precise fire. A forced-neutral
                    // diagnostic tick cannot acknowledge a packet it replaced.
                    automation_.confirmTempoMovementCommit(
                        tempoMovementCommitGeneration,
                        activeRouteDelivery.confirmScheduledFire
                            && !forceVirtualNeutral_ && !failClosedNeutralThisTick);
                }
                // Ordinary mirror semantics: a healthy pipe may legitimately
                // de-dup a steady state. Written-only is reserved for precise
                // deadline confirmation and explicit recovery proof.
                if (!precisionRouteRejected
                    && !(routeBoundRelease && !preciseReleaseAlreadyDelivered)) {
                    submitOk = preciseReleaseAlreadyDelivered
                        || hookOwnsInput || virtualSubmitOk;
                }
                if (outputDigitalReleaseEdge) {
                    const OrionInputPacket confirmed = orionInput_.lastSent();
                    if (directInputRouteCurrentlyOwned(
                            orionInput_.connected(), orionInput_.haveSent(),
                            orionInput_.haveSent() && confirmed.own != 0)) {
                        hookReleaseRepairDueMs_ = idleInputNowMs + kHookReleaseRepairDelayMs_;
                    }
                }
            }

            // [ORION_INPUT_RELEASE_REPAIR 2026-09-21] LocalUdpAccepted is deliberately not a
            // console acknowledgement. Re-check the established client-to-OrionStream route
            // shortly after a release instead of waiting for the old all-axis-neutral 1 Hz
            // probe. The sender's separate redundant-history path protects console button-ups;
            // an equal-state MustDeliver does not itself replay button history. Re-presses are safe:
            // reassertLastState() always duplicates the CURRENT confirmed state, never a cached
            // release payload. Pending precision work postpones the repair rather than racing it.
            if (hookReleaseRepairDueMs_ >= 0
                && idleInputNowMs >= hookReleaseRepairDueMs_
                && pendingSubmitSeq_ < 0
                && automation_.scheduledFireDeadlineMs() < 0.0
                && lastArmedFireToken_.load(std::memory_order_acquire) == 0
                && directInputWriteAllowed(
                    remotePlay_.state(), remotePlay_.inputRecoveryPending())) {
                InputRouteWriteResult repairResult = InputRouteWriteResult::Failed;
                bool repairAttempted = false;
                {
                    QMutexLocker submitLock(&submitMutex_);
                    if (!fireThread_ || !fireThread_->firedUnconsumed()) {
                        repairAttempted = true;
                        repairResult = orionInput_.reassertLastState();
                    }
                }
                if (repairAttempted
                    && repairResult == InputRouteWriteResult::LocalUdpAccepted) {
                    hookReleaseRepairDueMs_ = -1;
                } else if (repairAttempted
                           && repairResult == InputRouteWriteResult::Unchanged) {
                    // The route no longer owns a confirmed packet; there is no direct state to
                    // repair. A later owned seed starts a new release-tracking generation.
                    hookReleaseRepairDueMs_ = -1;
                } else if (repairAttempted
                           && (repairResult == InputRouteWriteResult::WrittenUnconfirmed
                               || repairResult == InputRouteWriteResult::Failed)) {
                    hookReleaseRepairDueMs_ = -1;
                    appendLog(QStringLiteral(
                        "Input release-repair duplicate FAILED (result=%1): direct pipe closed; "
                        "ordinary recovery must re-seed the current controller state.")
                                  .arg(static_cast<int>(repairResult)));
                }
            }

            if (squareUpDeliveryAudit) {
                appendLog(QStringLiteral(
                    "Square-up route audit: latest_physical_epoch=%1 phase=%2 physical_square=0 "
                    "requested_square=%3 pipe_snapshot_square=%4 pipe_snapshot_seq=%5 "
                    "snapshot_local_ack=%6 current_delivery_stage=%7 console_ack=0 "
                    "engine_state=%8 engine_reason=%9")
                              .arg(physicalShotEpochCounter_)
                              .arg(squareUpAuditPhase == SquareUpAuditPhase::RawUp
                                       ? QStringLiteral("raw_up") : QStringLiteral("debounced_up"))
                              .arg(output.square() ? 1 : 0)
                              .arg(squareUpPipeSnapshotBit)
                              .arg(squareUpPipeSnapshotSeq)
                              .arg(squareUpSnapshotLocalAck ? 1 : 0)
                              .arg(QString::fromLatin1(
                                  preciseFireDeliveryStageField(activeRouteDelivery.stage)))
                              .arg(holdStateToken(shot_.state))
                              .arg(shot_.releaseReason));
            }
            if (squareDownDeliveryAudit) {
                int deliveredSquareBit = -1;
                // [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16 owner] The DELIVERED sprint trigger,
                // read off the very packet this line already proves the Square bit from -- so the
                // identity line reflects the SHAPED packet, not the engine's intent. -1 keeps its
                // existing meaning here: no packet was in hand to read.
                int deliveredR2 = -1;
                uint32_t deliverySourceSeq = 0;
                QString packetSnapshot = QStringLiteral("unavailable");
                if (activeRouteDelivery.stage
                        == PreciseFireDeliveryStage::LocalUdpAccepted) {
                    packetSnapshot = QStringLiteral("pipe_snapshot_mismatch");
                    if (squareDownPipePacketValid) {
                        constexpr uint32_t kPsSquareBit = 1u << 2;
                        deliveredSquareBit =
                            (squareDownPipePacket.buttons & kPsSquareBit) != 0 ? 1 : 0;
                        deliveredR2 = static_cast<int>(squareDownPipePacket.r2_state);
                        deliverySourceSeq = squareDownPipePacket.seq;
                        packetSnapshot = QStringLiteral("direct_pipe_seq_join");
                    }
                } else if (activeRouteDelivery.stage
                               == PreciseFireDeliveryStage::ActiveVigemSubmit) {
                    packetSnapshot = QStringLiteral("vigem_snapshot_missing");
                    if (squareDownVirtualOutputValid) {
                        deliveredSquareBit = squareDownVirtualOutput.square() ? 1 : 0;
                        deliveredR2 = static_cast<int>(squareDownVirtualOutput.r2);
                        packetSnapshot = QStringLiteral("vigem_submit");
                    }
                }
                const QString deliveryStageField = QString::fromLatin1(
                    preciseFireDeliveryStageField(activeRouteDelivery.stage));
                const bool pipeTimingMatches = squareDownPipePacketValid
                    && squareDownPipeTiming.matches(squareDownPipePacket.seq);
                const uint64_t ackWaitUs = pipeTimingMatches
                    ? squareDownPipeTiming.ackWaitUs : 0;
                appendLog(QStringLiteral(
                    "Square-down delivery identity: physical_epoch=%1 shot_attempt=%2 "
                    "square_bit=%3 delivery_source_seq=%4 delivery_stage=%5 "
                    "local_route_ack=%6 console_ack=0 packet_snapshot=%7 "
                    "ack_wait_us=%8 ack_wait_us_valid=%9 timing_source_seq=%10 "
                    "delivered_r2=%11 sprint_released=%12")
                              .arg(physicalShotEpoch)
                              .arg(shot_.armToken)
                              .arg(deliveredSquareBit)
                              .arg(static_cast<qulonglong>(deliverySourceSeq))
                              .arg(deliveryStageField)
                              .arg(activeRouteDelivery.confirmScheduledFire ? 1 : 0)
                              .arg(packetSnapshot)
                              .arg(static_cast<qulonglong>(ackWaitUs))
                              .arg(pipeTimingMatches
                                       && squareDownPipeTiming.ackSampleValid ? 1 : 0)
                              .arg(pipeTimingMatches
                                       ? squareDownPipeTiming.sourceSeq : 0)
                              .arg(deliveredR2)
                              .arg(automation_.sprintReleaseActive() ? 1 : 0));
            }

            // [ORION_PRESS_DELIVERY_AUDIT 2026-08-30] Post-submit invariant audit: the pad's
            // Square is physically HELD but the gameplay state this tick handed to the console
            // route carries Square UP. Every such tick is either a deliberate policy (bot-owned
            // Tempo representation, post-fire anti-pump-fake drain/cooldown, the
            // waiting_for_button_release latch) or exactly the reported bug ("holding Square
            // and the bot won't shoot"). Attribute it either way — the gate that suppressed the
            // press is named, so live mining can whitelist the deliberate reasons and any
            // remaining line IS the defect. Edge-deduped on the attributed reason (one line per
            // suppression window, never per tick); nothing here touches output or timing.
            {
                const bool physSquareHeldNow = selected.device.state.square();
                const bool submittedSquare = (output.buttons & XINPUT_GAMEPAD_X) != 0;
                QString suppressReason;
                if (physSquareHeldNow && !submittedSquare && !defenseModeActive_) {
                    if (forceVirtualNeutral_) {
                        suppressReason = QStringLiteral("isolation_test");
                    } else if (precisionRouteRejected) {
                        suppressReason = QStringLiteral("precision_route_rejected");
                    } else if (failClosedNeutralThisTick) {
                        suppressReason = QStringLiteral("fail_closed_neutral");
                    } else if (!automation_.armed()) {
                        suppressReason = QStringLiteral("engine_disarmed");
                    } else {
                        suppressReason = QStringLiteral("engine:%1/%2")
                            .arg(holdStateText(shot_.state), shot_.releaseReason);
                    }
                }
                if (suppressReason != lastSquareSuppressionLogged_) {
                    lastSquareSuppressionLogged_ = suppressReason;
                    if (!suppressReason.isEmpty()) {
                        appendLog(QStringLiteral(
                            "SQUARE SUPPRESSED: gate=%1 session_running=%2 pipe_connected=%3 "
                            "(physical Square held; console being told Square-up)")
                                      .arg(suppressReason)
                                      .arg(remotePlay_.state() == RemotePlayState::Running
                                               ? 1 : 0)
                                      .arg(orionInput_.connected() ? 1 : 0));
                    }
                }
            }

            if (precisionRouteRejected) {
                appendLog(QStringLiteral(
                    "Precision release route rejected: token=%1 generation=%2 bound=%3; "
                    "release was not replayed on the live route.")
                              .arg(pendingSubmitScheduleToken_)
                              .arg(pendingSubmitRouteGeneration_)
                              .arg(static_cast<int>(pendingSubmitRoute_)));
            }

            // Keep steady-state Pipe ownership across an intentional coalesced
            // mirror write, but use the exact accepted transaction for releases
            // and proofs. A worker-confirmed release carries its own immutable
            // stage; the GUI's neutral catch-up must not erase that attestation.
            const bool exactRouteTransaction = pendingSubmitSeq_ >= 0
                || latencyRouteAttestationProbe || routeRecoveryProbe
                || tempoMovementCommitRequired || squareDownDeliveryAudit;
            const PreciseFireDeliveryStage selectedRouteStage =
                !directInputWriteAllowed(remotePlay_.state(), remotePlay_.inputRecoveryPending())
                ? PreciseFireDeliveryStage::None
                : preciseReleaseAlreadyDelivered
                ? pendingSubmitDeliveryStage_
                : (exactRouteTransaction
                    ? activeRouteDelivery.stage
                    : (hookOwnsInput
                        ? PreciseFireDeliveryStage::LocalUdpAccepted
                        : (virtualSubmitOk
                            ? PreciseFireDeliveryStage::ActiveVigemSubmit
                            : PreciseFireDeliveryStage::None)));
            const LatencyControllerRoute selectedLatencyRoute =
                selectedRouteStage == PreciseFireDeliveryStage::LocalUdpAccepted
                ? LatencyControllerRoute::Pipe
                : (selectedRouteStage == PreciseFireDeliveryStage::ActiveVigemSubmit
                    ? (controller_.isDs4Backend()
                        ? LatencyControllerRoute::VigemDs4
                        : LatencyControllerRoute::VigemXusb)
                    : LatencyControllerRoute::None);
            if (latencyCacheRouteAttestation_.active()
                && !latencyCacheRouteAttestation_.matchesActiveRoute(
                    selectedLatencyRoute)) {
                latencyCacheRouteAttestation_.revoke();
                automation_.setControllerDeliveryRouteAttestation(
                    0, LatencyControllerRoute::None);
            }
            if (latencyRouteAttestationProbe
                && activeRouteDelivery.confirmScheduledFire) {
                const QString deliveryRoute = activeRouteDelivery.stage
                        == PreciseFireDeliveryStage::LocalUdpAccepted
                    ? QStringLiteral("pipe")
                    : (controller_.isDs4Backend()
                        ? QStringLiteral("vigem_ds4")
                        : QStringLiteral("vigem_xusb"));
                const LatencyControllerRoute latencyRoute = activeRouteDelivery.stage
                        == PreciseFireDeliveryStage::LocalUdpAccepted
                    ? LatencyControllerRoute::Pipe
                    : (controller_.isDs4Backend()
                        ? LatencyControllerRoute::VigemDs4
                        : LatencyControllerRoute::VigemXusb);
                if (latencyCacheRouteAttestation_.beginPending(
                        latencyRoute, latencyAttestationNowMs)) {
                    const quint64 generation =
                        latencyCacheRouteAttestation_.generation();
                    automation_.setControllerDeliveryRouteAttestation(
                        generation, latencyRoute);
                    remotePlay_.attestLatencyControllerRoute(
                        deliveryRoute, generation);
                    appendLog(QStringLiteral(
                        "Timing cache route proof issued by neutral local delivery: "
                        "backend=%1 generation=%2; awaiting exact sidecar echo")
                                  .arg(deliveryRoute)
                                  .arg(generation));
                }
            }

            bool recoveryGateCleared = false;
            if (preciseFireDeliveryFault_
                && PreciseFirePolicy::routeRecoveryReady(
                    activeRouteDelivery.confirmScheduledFire,
                    physicalShotInputsNeutral,
                    preciseFireRecoveryNeutralFrames_)) {
                preciseFireDeliveryFault_ = false;
                recoveryGateCleared = true;
                appendLog(activeRouteDelivery.pipeAccepted
                    ? QStringLiteral("Precise-fire route recovered: direct pipe accepted a forced neutral proof after physical release.")
                    : QStringLiteral("Precise-fire route recovered: active ViGEm fallback accepted neutral after physical release."));
            }
            if (inputRouteAwaitingRecovery_
                && PreciseFirePolicy::routeRecoveryReady(
                    activeRouteDelivery.pipeAccepted,
                    physicalShotInputsNeutral,
                    preciseFireRecoveryNeutralFrames_)) {
                inputRouteAwaitingRecovery_ = false;
                hookDownHeartbeats_ = 0;
                hookRecoveryAttempts_ = 0;
                hookFullRestartEscalated_ = false;
                recoveryGateCleared = true;
                appendLog(QStringLiteral("Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared."));
            }
            if (recoveryGateCleared) {
                if (!preciseFireDeliveryFault_ && !inputRouteAwaitingRecovery_) {
                    preciseFireRecoveryNeutralFrames_ = 0;
                }
                syncEngineArmed();
            }
            if (hookOwnsInput != directPipeOwnsInput_) {
                directPipeOwnsInput_ = hookOwnsInput;
                appendLog(hookOwnsInput
                    ? QStringLiteral("Controller route isolation: direct Chiaki pipe owns PS5 input; desktop XUSB held neutral.")
                    : QStringLiteral("Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input."));
            }
            if (!virtualSubmitOk && !hookOwnsInput) {
                setControllerLifecycle(ControllerLifecycleState::ControllerFault,
                                       QStringLiteral("Virtual submit failed: %1").arg(error));
                appendLog(controllerStatus_);
            }
            // Hook liveness HEARTBEAT (~1/s while enabled): the only other place writeCount/failures
            // surface is the per-release "Release submit:" line, so a mid-session silent fallback to
            // ViGEm (pipe drop / reader stall) would be invisible until the next shot. This makes the
            // pre-encryption path verifiable live (pair with chiaki's ~1/s inject line end-to-end).
            if (orionInput_.enabled()) {
                const qint64 nowHbMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()).count();
                if (nowHbMs - lastHookHeartbeatMs_ >= 1000) {
                    lastHookHeartbeatMs_ = nowHbMs;
                    const quint64 rawSnapshotLockWaitMaxUs = rawInputWorker_
                        ? rawInputWorker_->takeMaxSnapshotLockWaitUs() : 0;
                    const OrionInputTransactionTiming hookTiming =
                        orionInput_.lastTransactionTiming();
                    appendLog(QStringLiteral("Input hook heartbeat: connected=%1 writes=%2 failures=%3 "
                                             "ack_failures=%4 last_us=%5 max_us=%6 ack_winerr=%7 "
                                             "ack_stage=%8 ack_error=%9 ack_seq=%10 expected_seq=%11 "
                                             "ack_bytes=%12 ack_wait_us=%13 "
                                             "raw_snapshot_lock_wait_max_us=%14 clock_sample_failures=%15 "
                                             "timing_source_seq=%16 write_attempted=%17 "
                                             "write_sample_valid=%18 ack_attempted=%19 "
                                             "ack_sample_valid=%20")
                                  .arg(orionInput_.connected() ? 1 : 0)
                                  .arg(orionInput_.writeCount())
                                  .arg(orionInput_.writeFailures())
                                  .arg(orionInput_.ackFailures())
                                  .arg(hookTiming.writeUs)
                                  .arg(orionInput_.maxWriteUs())
                                  .arg(orionInput_.lastAckWinError())
                                  .arg(orionInput_.lastAckStage())
                                  .arg(orionInput_.lastAckProtocolError())
                                  .arg(orionInput_.lastAckSourceSeq())
                                  .arg(orionInput_.lastAckExpectedSeq())
                                  .arg(orionInput_.lastAckReadBytes())
                                  .arg(hookTiming.ackWaitUs)
                                  .arg(rawSnapshotLockWaitMaxUs)
                                  .arg(orionInput_.clockSampleFailures())
                                  .arg(hookTiming.sourceSeq)
                                  .arg(hookTiming.writeAttempted ? 1 : 0)
                                  .arg(hookTiming.writeSampleValid ? 1 : 0)
                                  .arg(hookTiming.ackAttempted ? 1 : 0)
                                  .arg(hookTiming.ackSampleValid ? 1 : 0));
                    // [CL2-P4-001] Separate line: the heartbeat above is parser-pinned.
                    appendLog(QStringLiteral("Input tick health: gap_max_ms=%1 owned_keepalives=%2 abandons=%3 "
                                             "split_trigger_releases=%4")
                                  .arg(inputTickGapMaxMs_, 0, 'f', 1)
                                  .arg(orionInput_.ownedKeepalives())
                                  .arg(orionInput_.abandonsSent())
                                  .arg(orionInput_.splitTriggerReleases()));
                    inputTickGapMaxMs_ = 0.0;
                    // The direct-input pipe and capture transport have independent lifecycles.
                    // Try the smallest repair three times (Chiaki input child only), then wait one
                    // final grace and allow ONE contained full-sidecar restart. The escalation
                    // latch survives that restart's transient non-Running state; video freshness
                    // cannot re-arm automation until a real pipe write proves the route recovered.
                    const bool hookConnected = orionInput_.connected();
                    if (remotePlay_.state() == RemotePlayState::Running && !hookConnected) {
                        ++hookDownHeartbeats_;
                        const InputLinkRecoveryAction recoveryAction = inputLinkRecoveryAction(
                            hookDownHeartbeats_, hookRecoveryAttempts_,
                            hookFullRestartEscalated_, remotePlay_.inputRecoveryPending());
                        if (recoveryAction == InputLinkRecoveryAction::RecoverInputOnly
                            || recoveryAction == InputLinkRecoveryAction::RestartSidecarContained) {
                            const double frameAge = remotePlay_.frameAgeMs();
                            const double pixelAge = remotePlay_.pixelAgeMs();
                            const double transportAge = remotePlay_.transportAgeMs();
                            const bool backendFrozen = remotePlay_.backendFrozen();
                            const bool captureFailed = streamTransportNeedsRestart(
                                transportAge, backendFrozen);
                            const bool fullRestartRequired = captureFailed
                                || recoveryAction
                                    == InputLinkRecoveryAction::RestartSidecarContained;
                            if (fullRestartRequired) {
                                hookFullRestartEscalated_ = true;
                                inputRouteAwaitingRecovery_ = true;
                                lastWatchdogRestartMs_ = QDateTime::currentMSecsSinceEpoch();

                                const QString faultReason = captureFailed
                                    ? QStringLiteral("The controller link and the capture card both stopped")
                                    : QStringLiteral("The controller link kept dropping");
                                tripWatchdog(faultReason);
                                setControllerLifecycle(
                                    ControllerLifecycleState::ControllerFault,
                                    QStringLiteral("Direct controller route unavailable — one contained restart is running; automation is disarmed."));
                                appendLog(QStringLiteral("Input route escalation: hook down %1s, attempts %2/%3 "
                                                         "(transport age %4 ms, backend frozen=%5; detector "
                                                         "frame age %6 ms, pixel age %7 ms) — contained full "
                                                         "sidecar restart 1/1; automation stays fail-closed "
                                                         "until the direct pipe reconnects.")
                                              .arg(hookDownHeartbeats_)
                                              .arg(hookRecoveryAttempts_)
                                              .arg(kInputHookRecoveryMaxAttempts)
                                              .arg(transportAge, 0, 'f', 0)
                                              .arg(backendFrozen ? 1 : 0)
                                              .arg(frameAge, 0, 'f', 0)
                                              .arg(pixelAge, 0, 'f', 0));
                                restartSidecarWithWindowContainment();
                                syncEngineArmed();
                            } else {
                                ++hookRecoveryAttempts_;
                                const bool requested = remotePlay_.recoverInputLink();
                                appendLog(QStringLiteral("Input hook down %1s but capture transport healthy "
                                                         "(transport age %2 ms, backend frozen=%3; detector "
                                                         "frame age %4 ms, pixel age %5 ms) — input-only "
                                                         "Chiaki recovery attempt %6/%7 %8; capture and "
                                                         "detector remain live.")
                                              .arg(hookDownHeartbeats_)
                                              .arg(transportAge, 0, 'f', 0)
                                              .arg(backendFrozen ? 1 : 0)
                                              .arg(frameAge, 0, 'f', 0)
                                              .arg(pixelAge, 0, 'f', 0)
                                              .arg(hookRecoveryAttempts_)
                                              .arg(kInputHookRecoveryMaxAttempts)
                                              .arg(requested ? QStringLiteral("requested")
                                                             : QStringLiteral("NOT delivered")));
                            }
                        }
                    } else if (hookConnected) {
                        if (!inputRouteAwaitingRecovery_) {
                            hookDownHeartbeats_ = 0;
                            hookRecoveryAttempts_ = 0;
                            hookFullRestartEscalated_ = false;
                        }
                    } else {
                        hookDownHeartbeats_ = 0;
                        if (!hookFullRestartEscalated_) {
                            hookRecoveryAttempts_ = 0;
                        }
                    }
                    // [ORION_INPUT_IDLE_REASSERT 2026-08-30] 1 Hz liveness proof of the
                    // ESTABLISHED direct route while the pad is idle. The de-dup design means
                    // an idle hold sends nothing, so a silently wedged pipe reader (or any
                    // de-dup snapshot divergence that escaped the fail-closed rules) would
                    // otherwise be discovered by the PLAYER'S NEXT PRESS — the worst possible
                    // instant. Re-asserting the last confirmed state as a MustDeliver
                    // transaction is console-idempotent; its ACK either proves the route
                    // (microseconds when healthy) or closes the pipe NOW so the heartbeat
                    // recovery machinery can re-seed before a later press. Engine Idle
                    // alone is NOT physical idle: it includes initial Square debounce,
                    // pass-through holds and movement preparation. Require both the
                    // physical report and generated output digital controls to remain at rest for
                    // 250 ms, with no shot/recovery work pending. This avoids adding a
                    // second synchronous ACK on a press or its immediate release tick.
                    // A press arriving during the bounded ACK may still wait for it;
                    // this gate removes the observed-input overlap, not that transport
                    // failure ceiling. An ACK already confirmed this tick needs no probe.
                    // Kill switch: ORION_INPUT_IDLE_REASSERT=0.
                    static const bool idleReassertEnabled =
                        qgetenv("ORION_INPUT_IDLE_REASSERT") != QByteArrayLiteral("0");
                    if (idleReassertEnabled && hookConnected
                        && directInputWriteAllowed(
                            remotePlay_.state(), remotePlay_.inputRecoveryPending())
                        && hookDigitalRest && hookDigitalRestSinceMs_ >= 0
                        && idleInputNowMs - hookDigitalRestSinceMs_ >= 250
                        && !activeRouteDelivery.pipeAccepted) {
                        const InputRouteWriteResult reassertResult =
                            orionInput_.reassertLastState();
                        if (reassertResult == InputRouteWriteResult::WrittenUnconfirmed
                            || reassertResult == InputRouteWriteResult::Failed) {
                            appendLog(QStringLiteral(
                                "Input route reassert FAILED (result=%1): direct pipe closed "
                                "at idle; recovery will re-seed the route before the next "
                                "press instead of discovering the wedge on it.")
                                          .arg(static_cast<int>(reassertResult)));
                        }
                    }
                }
            }
            // Pair Release issued with the exact locally accepted route; the stable delivery_stage
            // is deliberately local proof, never a console/game acknowledgement.
            /* Resolve timing issuance against the ACTUAL submitted state: submit result,
            // backend, whether Square is cleared (square_bit must be 0 on a release), and
            // the RS for Go-To. Only this seq-paired confirmation unlocks the user-facing
            // success claim; Release issued by itself remains timing intent. */
            if (pendingSubmitSeq_ >= 0) {
                const OrionInputPacket hookPacket = orionInput_.lastSent();
                const OrionInputTransactionTiming latestPipeTiming =
                    orionInput_.lastTransactionTiming();
                PreciseFireDeliveryStage deliveryStage = pendingSubmitDeliveryStage_;
                uint32_t deliverySourceSeq = pendingSubmitTransportSeq_;
                if (deliveryStage == PreciseFireDeliveryStage::None) {
                    deliveryStage = activeRouteDelivery.stage;
                    deliverySourceSeq = activeRouteDelivery.pipeAccepted
                        && orionInput_.haveSent() ? hookPacket.seq : 0;
                }
                const bool deliveryConfirmed = deliveryStage != PreciseFireDeliveryStage::None;
                const bool deliverySucceeded = deliveryConfirmed && submitOk;
                const bool preciseMarkerTimestamp =
                    pendingSubmitDeliveryStage_ != PreciseFireDeliveryStage::None
                    && pendingSubmitFireToken_ != 0;
                const auto deliveredMarker = releaseMarkerDeliveryGate_.resolve(
                    pendingSubmitSeq_, deliverySucceeded,
                    // Sub-ms epoch: QDateTime::currentMSecsSinceEpoch() floors to whole ms and
                    // was quantizing the non-precise marker path; the offline lattice tests
                    // need release stamps at the same sub-ms resolution the frame stamps carry.
                    std::chrono::duration<double, std::milli>(
                        std::chrono::system_clock::now().time_since_epoch())
                        .count(),
                    preciseMarkerTimestamp);
                if (deliveredMarker) {
                    remotePlay_.sendReleaseMarker(
                        deliveredMarker->seq, deliveredMarker->wallMsEpoch,
                        deliveredMarker->latencyCalibration,
                        deliveredMarker->validationTargetPct,
                        deliveredMarker->validationTolerancePct,
                        deliveredMarker->physicalShotEpoch,
                        deliveredMarker->shotAttempt);
                } else if (deliverySucceeded) {
                    appendLog(QStringLiteral(
                        "Release marker dropped: missing or mismatched delivery intent for seq=%1")
                                  .arg(pendingSubmitSeq_));
                }
                const QString deliveryStageField = QString::fromLatin1(
                    preciseFireDeliveryStageField(deliveryStage));
                const QString deliveryBackend = deliveryStage == PreciseFireDeliveryStage::LocalUdpAccepted
                    ? QStringLiteral("PIPE")
                    : deliveryStage == PreciseFireDeliveryStage::ActiveVigemSubmit
                        ? (controller_.isDs4Backend() ? QStringLiteral("DS4")
                                                      : QStringLiteral("XUSB"))
                        : QStringLiteral("NONE");
                // Report the transaction that actually won local delivery, never
                // the GUI thread's later catch-up `output`.  Precise fires carry an
                // immutable controller/packet snapshot through the worker mailbox;
                // ordinary tick fires still use an exact transport-sequence join.
                // If neither proof matches, emit an explicit unknown sentinel.
                int deliveredSquareBit = -1;
                int deliveredRightX = 0;
                int deliveredRightY = 0;
                QString deliveredPacketSource = QStringLiteral("unavailable");
                const bool preciseDelivery = pendingSubmitDeliveryStage_
                        != PreciseFireDeliveryStage::None
                    && pendingSubmitFireToken_ != 0;
                const double fireEpochMs = preciseDelivery
                    ? pendingSubmitSnapshot_.fireEpochMs
                    : (deliveredMarker ? deliveredMarker->wallMsEpoch : -1.0);
                const QString fireEpochField = fireEpochLogField(fireEpochMs);
                const OrionInputPacket* deliveredPipePacket = nullptr;
                if (deliveryStage == PreciseFireDeliveryStage::LocalUdpAccepted) {
                    if (preciseDelivery && pendingSubmitSnapshot_.pipePacketValid
                        && deliverySourceSeq != 0
                        && pendingSubmitSnapshot_.pipePacket.seq == deliverySourceSeq) {
                        deliveredPipePacket = &pendingSubmitSnapshot_.pipePacket;
                        deliveredPacketSource = QStringLiteral("precise_pipe_mailbox");
                    } else if (!preciseDelivery && deliverySourceSeq != 0
                               && orionInput_.haveSent()
                               && hookPacket.seq == deliverySourceSeq) {
                        deliveredPipePacket = &hookPacket;
                        deliveredPacketSource = QStringLiteral("direct_pipe_seq_join");
                    }
                }
                const OrionInputTransactionTiming deliveredPipeTiming = preciseDelivery
                    ? pendingSubmitSnapshot_.pipeTiming : latestPipeTiming;
                const bool deliveredPipeTimingMatches = deliveredPipePacket
                    && deliveredPipeTiming.matches(deliverySourceSeq);
                if (deliveredPipePacket) {
                    constexpr uint32_t kPsSquareBit = 1u << 2;
                    constexpr int kHookAxisScale = 258;
                    deliveredSquareBit = (deliveredPipePacket->buttons & kPsSquareBit) != 0
                        ? 1 : 0;
                    deliveredRightX = static_cast<int>(deliveredPipePacket->right_x)
                        / kHookAxisScale;
                    deliveredRightY = static_cast<int>(deliveredPipePacket->right_y)
                        / kHookAxisScale;
                } else if (deliveryStage == PreciseFireDeliveryStage::ActiveVigemSubmit) {
                    const ControllerState* deliveredVigemOutput = nullptr;
                    if (preciseDelivery && pendingSubmitSnapshot_.outputValid) {
                        deliveredVigemOutput = &pendingSubmitSnapshot_.output;
                        deliveredPacketSource = QStringLiteral("precise_vigem_mailbox");
                    } else if (!preciseDelivery) {
                        deliveredVigemOutput = &output;
                        deliveredPacketSource = QStringLiteral("vigem_submit");
                    }
                    if (deliveredVigemOutput) {
                        deliveredSquareBit = deliveredVigemOutput->square() ? 1 : 0;
                        deliveredRightX = deliveredVigemOutput->rightStickX;
                        deliveredRightY = deliveredVigemOutput->rightStickY;
                    }
                }
                // The release packet is now proven locally accepted and its
                // Square bit is observably UP.  Arm the narrow rapid-repress
                // recovery only for modes that owned physical Square.  A failed,
                // unknown, or malformed delivery never relaxes the normal
                // three-report input lifetime.
                if (deliverySucceeded && deliveredSquareBit == 0
                    && (shot_.mode == ShotMode::ButtonShot
                        || shot_.mode == ShotMode::TempoSquare)) {
                    shotIntentEdgeTracker_.noteOwnedSquareReleaseDeliveredForEpoch(
                        shot_.physicalShotEpoch, physicalShotEpochCounter_);
                }
                // [ORION_TIP_FRAME_NATIVE 2026-09-17] WHERE ON THE CONSOLE'S FRAME GRID DID THIS
                // COMMAND ACTUALLY LAND. docs/POLL_PHASE_TRACKER.md §10 asked for exactly this
                // line: "emit the grid phase and the realised distance to the intended centre AT
                // THE FIRE, not only at anchor dating", because the schedule could be shown to
                // carry the frame-centre offset while the fire did not.
                //
                // THE LEAD IS ADDED ON PURPOSE. The grid lives on the capture-aligned clock and
                // the command becomes VISIBLE to the console `lead` ms after it is issued, so the
                // instant to place on the grid is command_issued + lead -- which is precisely the
                // target the arm centred. Reading command_issued alone would measure the lead's
                // own residue mod 16.7 ms and say nothing about centring.
                //
                // fire_grid_phase_ms is in [0, period) and fire_centre_delta_ms is the signed
                // distance from the frame's centre (bounded by half a frame, negative = the
                // release landed before the centre). Sentinels -1.000 / -99.000 mean "not
                // measurable here": no locked grid for this shot, or no precise-fire timestamp.
                const QString fireTargetField = pendingSubmitFrameGrid_.fireTargetMode.isEmpty()
                    ? QStringLiteral("none")
                    : pendingSubmitFrameGrid_.fireTargetMode;
                const double frameGridCommandIssuedMs =
                    preciseDelivery ? pendingSubmitSnapshot_.commandIssuedMs : -1.0;
                // Both halves must be real or the answer is a sentinel, never a fabricated zero:
                // no precise-fire timestamp means there is no release instant to place, and a
                // lead of 0 would put the raw issue instant on a grid it does not live on.
                const orion::game_frame_phase::ReleaseGridPhase releasePhase =
                    (frameGridCommandIssuedMs > 0.0 && pendingSubmitFrameGrid_.leadMs > 0.0)
                        ? orion::game_frame_phase::releasePhaseOnGrid(
                              pendingSubmitFrameGrid_.grid, frameGridCommandIssuedMs,
                              pendingSubmitFrameGrid_.leadMs)
                        : orion::game_frame_phase::ReleaseGridPhase{};
                const double fireGridPhaseMs = releasePhase.phaseMs;
                const double fireCentreDeltaMs = releasePhase.centreDeltaMs;
                appendLog(QStringLiteral("Release submit: seq=%1 ok=%2 backend=%3 "
                                         "square_bit=%4 rs=(%5,%6) hook_write_us=%7 "
                                         "hook_max_us=%8 hook_writes=%9 hook_failures=%10 "
                                         "hook_packet_seq=%11 hook_flags=0x%12 "
                                         "delivery_stage=%13 delivery_source_seq=%14 "
                                         "delivery_token=%15 packet_snapshot=%16 "
                                         "console_ack=0 tick_submit_ok=%17 "
                                         "hook_ack_wait_us=%18 clock_sample_failures=%19 "
                                         "hook_timing_source_seq=%20 hook_write_attempted=%21 "
                                         "hook_write_us_valid=%22 hook_ack_attempted=%23 "
                                         // APPEND-ONLY: every field above keeps its name and its
                                         // order; the free-form ERR= suffix stays last because its
                                         // value may contain spaces.
                                         "hook_ack_wait_us_valid=%24 fire_grid_phase_ms=%25 "
                                         "fire_centre_delta_ms=%26 fire_target=%27 %28%29")
                              .arg(pendingSubmitSeq_)
                              .arg(deliveryConfirmed ? 1 : 0)
                              .arg(deliveryBackend)
                              .arg(deliveredSquareBit)
                              .arg(deliveredRightX)
                              .arg(deliveredRightY)
                              .arg(deliveredPipeTimingMatches
                                       ? deliveredPipeTiming.writeUs : 0)
                              .arg(orionInput_.maxWriteUs())
                              .arg(orionInput_.writeCount())
                              .arg(orionInput_.writeFailures())
                              .arg(orionInput_.haveSent() ? hookPacket.seq : 0)
                              .arg(QString::number(orionInput_.haveSent()
                                                      ? hookPacket.reserved : 0,
                                                  16))
                              .arg(deliveryStageField)
                              .arg(deliverySourceSeq)
                              .arg(pendingSubmitFireToken_)
                              .arg(deliveredPacketSource)
                              .arg(submitOk ? 1 : 0)
                              .arg(deliveredPipeTimingMatches
                                       ? deliveredPipeTiming.ackWaitUs : 0)
                              .arg(orionInput_.clockSampleFailures())
                              .arg(deliveredPipeTimingMatches
                                       ? deliveredPipeTiming.sourceSeq : 0)
                              .arg(deliveredPipeTimingMatches
                                       && deliveredPipeTiming.writeAttempted ? 1 : 0)
                              .arg(deliveredPipeTimingMatches
                                       && deliveredPipeTiming.writeSampleValid ? 1 : 0)
                              .arg(deliveredPipeTimingMatches
                                       && deliveredPipeTiming.ackAttempted ? 1 : 0)
                              .arg(deliveredPipeTimingMatches
                                       && deliveredPipeTiming.ackSampleValid ? 1 : 0)
                              // [ORION_TIP_FRAME_NATIVE 2026-09-17] Numbered BELOW the ERR suffix
                              // on purpose: QString::arg fills the lowest remaining marker, so a
                              // new marker placed after a free-form value would be exposed to
                              // whatever that value happens to contain.
                              .arg(fireGridPhaseMs, 0, 'f', 3)
                              .arg(fireCentreDeltaMs, 0, 'f', 3)
                              .arg(fireTargetField)
                              .arg(fireEpochField)
                              .arg(deliverySucceeded ? QString()
                                    : QStringLiteral(" ERR=%1").arg(
                                          deliveryConfirmed
                                              ? error.left(80)
                                              : QStringLiteral("local_delivery_not_confirmed"))));
                if (preciseDelivery) {
                    // Add-only parser-safe transaction telemetry. The command
                    // timestamp is the scheduler/marker authority; completion
                    // and the pipe ACK wait are diagnostic-only and can vary
                    // without moving scheduledFireDeltaMs or learned lead.
                    const double pipeAckCompleteMs =
                        pendingSubmitSnapshot_.route == LatencyControllerRoute::Pipe
                            ? pendingSubmitSnapshot_.activeRouteCompleteMs : -1.0;
                    appendLog(QStringLiteral(
                        "Precise dispatch timing: seq=%1 token=%2 delivery_stage=%3 "
                        "command_issued_ms=%4 active_route_complete_ms=%5 "
                        "active_route_wait_ms=%6 pipe_ack_complete_ms=%7 "
                        "pipe_timing_source_seq=%8 pipe_write_us=%9 "
                        "pipe_write_us_valid=%10 pipe_ack_attempted=%11 "
                        "pipe_ack_wait_us=%12 pipe_ack_wait_us_valid=%13")
                                  .arg(pendingSubmitSeq_)
                                  .arg(pendingSubmitFireToken_)
                                  .arg(deliveryStageField)
                                  .arg(pendingSubmitSnapshot_.commandIssuedMs, 0, 'f', 3)
                                  .arg(pendingSubmitSnapshot_.activeRouteCompleteMs, 0, 'f', 3)
                                  .arg(pendingSubmitSnapshot_.activeRouteDurationMs, 0, 'f', 3)
                                  .arg(pipeAckCompleteMs, 0, 'f', 3)
                                  .arg(pendingSubmitSnapshot_.pipeTiming.sourceSeq)
                                  .arg(pendingSubmitSnapshot_.pipeTiming.writeUs)
                                  .arg(pendingSubmitSnapshot_.pipeTiming.writeSampleValid ? 1 : 0)
                                  .arg(pendingSubmitSnapshot_.pipeTiming.ackAttempted ? 1 : 0)
                                  .arg(pendingSubmitSnapshot_.pipeTiming.ackWaitUs)
                                  .arg(pendingSubmitSnapshot_.pipeTiming.ackSampleValid ? 1 : 0));
                }
                appendLog(QStringLiteral(
                    "Release delivery identity: physical_epoch=%1 shot_attempt=%2 "
                    "release_seq=%3 schedule_token=%4 delivery_token=%5 "
                    "delivery_stage=%6 local_route_ack=%7 console_ack=0")
                              .arg(pendingSubmitPhysicalShotEpoch_)
                              .arg(pendingSubmitShotAttempt_)
                              .arg(pendingSubmitSeq_)
                              .arg(pendingSubmitScheduleToken_)
                              .arg(pendingSubmitFireToken_)
                              .arg(deliveryStageField)
                              .arg(deliverySucceeded ? 1 : 0));
                const QString userNotice = deliverySucceeded
                    ? userReleaseTracker_.confirm(pendingSubmitSeq_)
                    : userReleaseTracker_.fail(
                        pendingSubmitSeq_, UserReleaseFailureReason::TransportNotConfirmed);
                appendCustomerEvent(userNotice);
                if (!deliverySucceeded) {
                    automation_.cancelPostReleaseGrade(pendingSubmitSeq_);
                    preciseFireDeliveryFault_ = true;
                    preciseFireRecoveryNeutralFrames_ = 0;
                    automation_.setArmed(false);
                    setControllerLifecycle(
                        ControllerLifecycleState::ControllerFault,
                        QStringLiteral("Release delivery was not locally confirmed; grade cancelled"));
                }
                pendingSubmitSeq_ = -1;
                pendingSubmitPhysicalShotEpoch_ = 0;
                pendingSubmitShotAttempt_ = 0;
                pendingSubmitScheduleToken_ = 0;
                pendingSubmitRouteGeneration_ = 0;
                pendingSubmitRoute_ = LatencyControllerRoute::None;
                pendingSubmitDeliveryStage_ = PreciseFireDeliveryStage::None;
                pendingSubmitFireToken_ = 0;
                pendingSubmitTransportSeq_ = 0;
                pendingSubmitSnapshot_ = {};
                pendingSubmitFrameGrid_ = {};
            }
            // Per-tick ownership trace through Releasing/Cooldown + the summary flush.
            updateReleaseOwnershipTrace(selected.device.state, output, submitOk, nowMs);
        } else if (pendingSubmitSeq_ >= 0) {
            // P0.1: a release fired but NO virtual pad is connected (preview / virtual-disconnected): the
            // cleared-Square state never reaches the console. This is exactly the "telemetry says released,
            // game saw nothing" failure. Previously this only wrote the debug log and the overlay went on
            // to GRADE a shot that never fired. Now: (1) surface it LOUDLY on the user-facing status channel
            // (the same ControllerFault banner a failed submit raises), and (2) cancel the phantom grade so
            // the post-release meter (the human's shot, not the bot's) can't poison the learner/overlay.
            appendLog(QStringLiteral("Release submit: seq=%1 ok=0 backend=NONE "
                                     "delivery_stage=not_confirmed delivery_source_seq=0 "
                                     "delivery_token=%2 console_ack=0 tick_submit_ok=0 "
                                     "fire_epoch_ms=-1.000 reason=virtual_disconnected")
                          .arg(pendingSubmitSeq_)
                          .arg(pendingSubmitFireToken_));
            appendLog(QStringLiteral(
                "Release delivery identity: physical_epoch=%1 shot_attempt=%2 "
                "release_seq=%3 schedule_token=%4 delivery_token=%5 "
                "delivery_stage=not_confirmed local_route_ack=0 console_ack=0")
                          .arg(pendingSubmitPhysicalShotEpoch_)
                          .arg(pendingSubmitShotAttempt_)
                          .arg(pendingSubmitSeq_)
                          .arg(pendingSubmitScheduleToken_)
                          .arg(pendingSubmitFireToken_));
            automation_.cancelPostReleaseGrade(pendingSubmitSeq_);
            const QString notice = userReleaseTracker_.fail(
                pendingSubmitSeq_, UserReleaseFailureReason::VirtualControllerDisconnected);
            appendCustomerEvent(notice);
            setControllerLifecycle(ControllerLifecycleState::ControllerFault,
                                   QStringLiteral("Release NOT submitted — virtual pad disconnected; the shot "
                                                  "did not reach the console"));
            releaseMarkerDeliveryGate_.reset();
            pendingSubmitSeq_ = -1;
            pendingSubmitPhysicalShotEpoch_ = 0;
            pendingSubmitShotAttempt_ = 0;
            pendingSubmitScheduleToken_ = 0;
            pendingSubmitRouteGeneration_ = 0;
            pendingSubmitRoute_ = LatencyControllerRoute::None;
            pendingSubmitDeliveryStage_ = PreciseFireDeliveryStage::None;
            pendingSubmitFireToken_ = 0;
            pendingSubmitTransportSeq_ = 0;
            pendingSubmitSnapshot_ = {};
            pendingSubmitFrameGrid_ = {};
        }
        // Lost the virtual pad mid-release — don't lose the ownership summary (can't keep
        // tracing without a submit, so flush whatever we accumulated).
        if (!controller_.isConnected() && ownSeq_ >= 0) {
            flushReleaseOwnershipTrace();
        }
        return;
    }
#endif
}

void OrionAppController::updateLightbarEffect()
{
    applyControllerLightbar(false);
}

void OrionAppController::requestControllerLightbarRefresh()
{
    // A single pending bit coalesces reconnect/settings churn into one HID write.
    // If a shot is active, applyControllerLightbar leaves it pending and the
    // existing coarse timer performs exactly one deferred attempt once safe.
    lightbarRefreshPending_ = true;
    if (config_.data().controllerLightbarEnabled
        && !lightbarEffectTimer_.isActive()) {
        lightbarEffectTimer_.start();
    }
}

void OrionAppController::setCaptureSourceHealth(const QString& value)
{
    if (captureSourceHealth_ == value) {
        return;
    }
    captureSourceHealth_ = value;
    if (value != QLatin1String("frame_feed_active")) {
        // A source-generation/liveness boundary invalidates every prior-shot
        // continuity proof immediately. A new stream must earn a new structural
        // lock before any held position can be presented.
        meterOverlayContinuityLease_.reset();
        lastRealMeterSeenMs_ = 0;
        lastMeterOverlayVisualSeenMs_ = 0;
    }
    emit statusChanged();
}

void OrionAppController::notifyTelemetryStatusAtHumanCadence(qint64 nowMs)
{
    if (telemetryStatusThrottle_.take(nowMs)) {
        emit statusChanged();
    }
}

void OrionAppController::notifyTelemetryPropertiesAtHumanCadence(qint64 nowMs)
{
    if (telemetryPropertyThrottle_.take(nowMs)) {
        emit telemetryChanged();
    }
}

void OrionAppController::notifyControllerStatusAtHumanCadence(qint64 nowMs)
{
    if (controllerStatusThrottle_.take(nowMs)) {
        emit statusChanged();
    }
}

void OrionAppController::setControllerLifecycle(ControllerLifecycleState state, const QString& status)
{
    const bool stateChanged = controllerLifecycleState_ != state;
    const bool controllerStatusChanged = controllerStatus_ != status;
    controllerLifecycleState_ = state;
    controllerStatus_ = status;
    if (stateChanged || controllerStatusChanged) {
        appendLog(QStringLiteral("Controller state: %1").arg(status));
        controllerStatusThrottle_.markImmediate(
            QDateTime::currentMSecsSinceEpoch());
        emit statusChanged();
    }
}

void OrionAppController::setControllerLedStatus(const QString& status)
{
    if (controllerLedStatus_ == status) {
        return;
    }
    controllerLedStatus_ = status;
    emit statusChanged();
}

void OrionAppController::applyControllerLightbar(bool force)
{
#ifdef Q_OS_WIN
    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    const QString mode = config_.data().controllerLightbarMode;
    const bool animated = mode == QLatin1String("Pulse")
        || mode == QLatin1String("Strobe")
        || mode == QLatin1String("Rainbow");

    if (force) {
        lightbarRefreshPending_ = true;
    }
    if (!config_.data().controllerLightbarEnabled) {
        lightbarRefreshPending_ = false;
        lastLightbarSentColor_ = QColor();
        lastLightbarSentDevicePath_.clear();
        lastLightbarSentDeviceKind_.clear();
        lightbarEffectTimer_.stop();
        setControllerLedStatus(QStringLiteral("LED disabled"));
        return;
    }

    // Animated modes retain their 180 ms effect ticker. Solid mode is strictly
    // event-driven: the ticker exists only while one settings/reconnect refresh
    // is pending, then stops after that attempt.
    const bool wantTimer = animated || lightbarRefreshPending_;
    if (wantTimer && !lightbarEffectTimer_.isActive()) {
        lightbarEffectTimer_.start();
    } else if (!wantTimer && lightbarEffectTimer_.isActive()) {
        lightbarEffectTimer_.stop();
    }
    if (!force && !animated && !lightbarRefreshPending_) {
        return;
    }
    if (!force && animated && nowMs - lastLightbarApplyMs_ < 160) {
        return;
    }

    // PRECISION GUARD: never touch the HID output endpoint while a shot is being
    // timed/released — an output report on the same device the RawInput worker is
    // reading must not be able to perturb the timing path mid-shot. The effect
    // ticker retries right after the shot returns to Idle/Cooldown.
    const HoldState shotState = automation_.context().state;
    if (shotState != HoldState::Idle && shotState != HoldState::Cooldown) {
        lightbarRefreshPending_ = true;
        return;
    }

    // [ORION_INPUT_DEAD_UX] Terminal dead-input override outranks everything: a session the
    // player asked for is down while video plays, so the pad in their hands is the one surface
    // guaranteed to be seen. Same opt-in write path as every other lightbar update (inert when
    // the user's lightbar feature is off — the enabled gate above already returned).
    const bool inputDeadWarning = inputDeadSeverity_ == InputDeadSeverity::Critical
        && inputSessionIntentActive_;
    // Defense Mode overrides the base color (mode pill + pad agree at a glance).
    QColor color(inputDeadWarning
                     ? QColor(QStringLiteral("#EF4444"))
                     : QColor(defenseModeActive_
                                  ? config_.data().defenseLightbarColor
                                  : config_.data().controllerLightbarPrimaryColor));
    const QColor secondary(config_.data().controllerLightbarSecondaryColor);
    if (!color.isValid()) {
        lightbarRefreshPending_ = false;
        lightbarEffectTimer_.stop();
        setControllerLedStatus(QStringLiteral("LED color invalid"));
        return;
    }
    const double speed = qBound(0.2, config_.data().controllerLightbarEffectSpeed, 4.0);
    const double brightness = qBound(0.05, config_.data().controllerLightbarBrightness, 1.0);
    const double t = static_cast<double>(nowMs % 600000) / 1000.0 * speed;
    if (mode == QLatin1String("Pulse") && secondary.isValid()) {
        const double phase = 0.5 + 0.5 * std::sin(t * 3.14159265358979323846);
        color.setRedF(color.redF() * (1.0 - phase) + secondary.redF() * phase);
        color.setGreenF(color.greenF() * (1.0 - phase) + secondary.greenF() * phase);
        color.setBlueF(color.blueF() * (1.0 - phase) + secondary.blueF() * phase);
    } else if (mode == QLatin1String("Strobe")) {
        const bool on = (static_cast<int>(t * 5.0) % 2) == 0;
        if (!on) {
            color = QColor(0, 0, 0);
        }
    } else if (mode == QLatin1String("Rainbow")) {
        const double hue = std::fmod(t * 0.15, 1.0);
        color = QColor::fromHsvF(static_cast<float>(hue), 0.95f, 1.0f);
    }
    color.setRed(std::clamp(static_cast<int>(std::round(color.red() * brightness)), 0, 255));
    color.setGreen(std::clamp(static_cast<int>(std::round(color.green() * brightness)), 0, 255));
    color.setBlue(std::clamp(static_cast<int>(std::round(color.blue() * brightness)), 0, 255));

    if (activePhysicalDevicePath_.isEmpty()) {
        lightbarRefreshPending_ = false;
        lightbarEffectTimer_.stop();
        setControllerLedStatus(QStringLiteral("LED unavailable for this device path"));
        return;
    }
    if (!physicalPadConnected_ || lastPhysicalSeenMs_ <= 0 || nowMs - lastPhysicalSeenMs_ > 1500) {
        lightbarRefreshPending_ = false;
        lightbarEffectTimer_.stop();
        setControllerLedStatus(QStringLiteral("LED waiting for controller"));
        activePhysicalDevicePath_.clear();
        activePhysicalDeviceKind_.clear();
        return;
    }

    // Bluetooth pads need the CRC32-framed 0x11/0x31 output reports, which this
    // path doesn't build — be honest instead of silently failing every write.
    const QString lowerPath = activePhysicalDevicePath_.toLower();
    if (lowerPath.contains(QStringLiteral("bthenum")) || lowerPath.contains(QStringLiteral("{00001124-"))) {
        lightbarRefreshPending_ = false;
        lightbarEffectTimer_.stop();
        setControllerLedStatus(
            QStringLiteral("Lightbar requires a USB connection (Bluetooth pad detected)"));
        return;
    }

    // A recurring timer must never reopen the HID device to resend an identical
    // Solid value. Explicit force=true calls still write immediately, which keeps
    // settings changes and manual Apply semantics intact.
    const bool sameSolidWrite = !animated
        && lastLightbarSentColor_.isValid()
        && lastLightbarSentColor_ == color
        && lastLightbarSentDevicePath_ == activePhysicalDevicePath_
        && lastLightbarSentDeviceKind_ == activePhysicalDeviceKind_;
    if (!force && sameSolidWrite) {
        lightbarRefreshPending_ = false;
        lightbarEffectTimer_.stop();
        return;
    }

    QString status;
    lastLightbarApplyMs_ = nowMs;
    const bool sent = sendSonyLightbar(
        activePhysicalDevicePath_, activePhysicalDeviceKind_, color, &status);
    if (sent) {
        lastLightbarSentColor_ = color;
        lastLightbarSentDevicePath_ = activePhysicalDevicePath_;
        lastLightbarSentDeviceKind_ = activePhysicalDeviceKind_;
    }
    lightbarRefreshPending_ = false;
    if (!animated) {
        lightbarEffectTimer_.stop();
    }
    setControllerLedStatus(status);
#else
    Q_UNUSED(force);
    setControllerLedStatus(QStringLiteral("LED unavailable on this platform"));
#endif
}

bool OrionAppController::hasRecentRawInput() const noexcept
{
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-07] lastRawInputMs_ is set only when the
    // rawInputWorker snapshot carries a real HID report (OrionAppController.cpp
    // :9381) and is cleared to 0 on device removal (:4183 / :6913). Enumeration
    // (findRawInputController -> rawInputPresent_ = true at :9419) sets rawInputPresent_.
    // Fall back to rawInputPresent_ when the controller is enumerated so that an
    // idle pad resting on the desk satisfies the gate without forcing the user
    // to wiggle the sticks.
    if (lastRawInputMs_ <= 0) return rawInputPresent_;
    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    return (nowMs - lastRawInputMs_) < 3000 || rawInputPresent_;
}

#ifdef Q_OS_WIN
namespace {
// [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Walk HKLM\SYSTEM\CurrentControlSet\
// Enum\USB for Sony pad device keys (VID_054C + the PID set the RawInput
// classifier accepts) and report (a) whether ANY instance has ever enumerated
// (registry history survives the pad dropping off the bus and app restarts)
// and (b) which instances' Device Parameters still carry
// EnhancedPowerManagementEnabled=1 — the rig-verified cause of the pad
// wedging off USB after idle (see UsbPadPowerPolicy.h). Read-only; the write
// happens only in the user-consented applyControllerUsbPowerFix().
struct SonyUsbPowerScanResult {
    bool historyKnown = false;
    QStringList epmEnabledParamKeys; // relative to HKLM
};

bool sonyPadUsbDeviceKeyName(const QString& keyName)
{
    if (!keyName.startsWith(QStringLiteral("VID_054C&PID_"), Qt::CaseInsensitive)) {
        return false;
    }
    static const char* kPids[] = {"0CE6", "0DF2", "0E5F", "05C4", "09CC"};
    for (const char* pid : kPids) {
        if (keyName.mid(13, 4).compare(QLatin1String(pid), Qt::CaseInsensitive) == 0) {
            return true;
        }
    }
    return false;
}

SonyUsbPowerScanResult scanSonyUsbPadPowerState()
{
    SonyUsbPowerScanResult result;
    HKEY usbKey = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE,
                      L"SYSTEM\\CurrentControlSet\\Enum\\USB", 0,
                      KEY_READ, &usbKey) != ERROR_SUCCESS) {
        return result;
    }
    for (DWORD i = 0;; ++i) {
        wchar_t deviceName[256];
        DWORD deviceLen = 256;
        if (RegEnumKeyExW(usbKey, i, deviceName, &deviceLen,
                          nullptr, nullptr, nullptr, nullptr) != ERROR_SUCCESS) {
            break;
        }
        const QString device = QString::fromWCharArray(deviceName);
        if (!sonyPadUsbDeviceKeyName(device)) {
            continue;
        }
        HKEY deviceKey = nullptr;
        if (RegOpenKeyExW(usbKey, deviceName, 0, KEY_READ, &deviceKey) != ERROR_SUCCESS) {
            continue;
        }
        for (DWORD j = 0;; ++j) {
            wchar_t instanceName[256];
            DWORD instanceLen = 256;
            if (RegEnumKeyExW(deviceKey, j, instanceName, &instanceLen,
                              nullptr, nullptr, nullptr, nullptr) != ERROR_SUCCESS) {
                break;
            }
            result.historyKnown = true;
            const QString paramsPath =
                QStringLiteral("SYSTEM\\CurrentControlSet\\Enum\\USB\\%1\\%2\\Device Parameters")
                    .arg(device, QString::fromWCharArray(instanceName));
            HKEY paramsKey = nullptr;
            if (RegOpenKeyExW(HKEY_LOCAL_MACHINE,
                              reinterpret_cast<LPCWSTR>(paramsPath.utf16()), 0,
                              KEY_READ, &paramsKey) != ERROR_SUCCESS) {
                continue;
            }
            DWORD value = 0;
            DWORD size = sizeof(value);
            DWORD type = 0;
            if (RegQueryValueExW(paramsKey, L"EnhancedPowerManagementEnabled", nullptr,
                                 &type, reinterpret_cast<LPBYTE>(&value), &size)
                    == ERROR_SUCCESS
                && type == REG_DWORD && value != 0) {
                result.epmEnabledParamKeys.append(paramsPath);
            }
            RegCloseKey(paramsKey);
        }
        RegCloseKey(deviceKey);
    }
    RegCloseKey(usbKey);
    return result;
}
} // namespace
#endif // Q_OS_WIN

void OrionAppController::refreshControllerUsbPowerScan()
{
#ifdef Q_OS_WIN
    const SonyUsbPowerScanResult scan = scanSonyUsbPadPowerState();
    sonyUsbHistoryKnown_ = scan.historyKnown;
    sonyUsbEpmEnabledParamKeys_ = scan.epmEnabledParamKeys;
    controllerUsbPowerFixAvailable_ = !scan.epmEnabledParamKeys.isEmpty();
#endif
}

void OrionAppController::applyControllerUsbPowerFix()
{
#ifdef Q_OS_WIN
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Explicit user consent is the
    // button press that got us here — nothing calls this automatically. Write
    // EnhancedPowerManagementEnabled=0 on every known Sony pad instance so
    // hidusb stops idling the pad into the firmware-wedging suspend. Scoped
    // strictly to Sony pad Enum keys; no global power setting is touched.
    refreshControllerUsbPowerScan();
    if (sonyUsbEpmEnabledParamKeys_.isEmpty()) {
        backendMessage_ = QStringLiteral(
            "Controller USB power fix: nothing to change — no pad entry has the risky setting.");
        appendLog(backendMessage_);
        emit statusChanged();
        return;
    }
    int fixed = 0;
    int failed = 0;
    for (const QString& paramsPath : std::as_const(sonyUsbEpmEnabledParamKeys_)) {
        HKEY paramsKey = nullptr;
        if (RegOpenKeyExW(HKEY_LOCAL_MACHINE,
                          reinterpret_cast<LPCWSTR>(paramsPath.utf16()), 0,
                          KEY_SET_VALUE, &paramsKey) != ERROR_SUCCESS) {
            ++failed;
            appendLog(QStringLiteral("Controller USB power fix: open failed for %1").arg(paramsPath));
            continue;
        }
        const DWORD zero = 0;
        if (RegSetValueExW(paramsKey, L"EnhancedPowerManagementEnabled", 0, REG_DWORD,
                           reinterpret_cast<const BYTE*>(&zero), sizeof(zero))
                == ERROR_SUCCESS) {
            ++fixed;
        } else {
            ++failed;
            appendLog(QStringLiteral("Controller USB power fix: write failed for %1").arg(paramsPath));
        }
        RegCloseKey(paramsKey);
    }
    if (failed > 0 && fixed == 0) {
        backendMessage_ = QStringLiteral(
            "Controller USB power fix FAILED (%1 entr%2). Start Venice through its launcher "
            "(administrator) and try again.")
                              .arg(failed)
                              .arg(failed == 1 ? QStringLiteral("y") : QStringLiteral("ies"));
    } else {
        backendMessage_ = QStringLiteral(
            "Controller USB power fix applied to %1 pad entr%2%3. Unplug and re-plug the "
            "controller once to finish. If you later use a NEW USB port, the fix may be "
            "offered again for that port — that is expected.")
                              .arg(fixed)
                              .arg(fixed == 1 ? QStringLiteral("y") : QStringLiteral("ies"))
                              .arg(failed > 0
                                       ? QStringLiteral(" (%1 failed — see log)").arg(failed)
                                       : QString());
    }
    appendLog(backendMessage_);
    refreshControllerUsbPowerScan();
    emit statusChanged();
#endif
}

bool OrionAppController::nudgePhysicalPadOnce(bool* opened, bool* answered)
{
    bool probeOpened = false;
    bool probeAnswered = false;
#ifdef Q_OS_WIN
    const QString path = rawInputDevicePath_;
    if (!path.isEmpty()) {
        HANDLE handle = CreateFileW(
            reinterpret_cast<LPCWSTR>(path.utf16()),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (handle == INVALID_HANDLE_VALUE) {
            // Metadata-only open still runs the PnP open path (and the wake)
            // when a privileged client holds read/write access.
            handle = CreateFileW(
                reinterpret_cast<LPCWSTR>(path.utf16()),
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        }
        if (handle != INVALID_HANDLE_VALUE) {
            probeOpened = true;
            // DualSense/DS4 report id 0x01 (USB and BT-simple). A failure here
            // is expected for BT-enhanced mode and is NOT a refusal: only the
            // confirm loop below decides.
            QByteArray report(256, char(0));
            report[0] = char(0x01);
            probeAnswered = HidD_GetInputReport(
                                handle, report.data(),
                                static_cast<ULONG>(report.size())) == TRUE;
            CloseHandle(handle);
        }
    }
#endif
    if (opened) {
        *opened = probeOpened;
    }
    if (answered) {
        *answered = probeAnswered;
    }
    return probeOpened;
}

bool OrionAppController::wakePhysicalPadAndConfirmReport()
{
#ifdef Q_OS_WIN
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Enumerated-but-silent recovery.
    // A healthy Sony pad streams input reports continuously while enumerated
    // (IMU noise makes every report unique; Raw Input posts WM_INPUT per
    // report, with no state-change de-duplication), so "enumerated + silent"
    // is not an idle pad — it is a pad whose transport went quiet, most
    // commonly USB selective suspend after system idle. Opening the HID
    // collection forces a D0 resume; the synchronous input-report poll nudges
    // firmware that idles its stream while suspended. Both are best-effort
    // DIAGNOSTICS ONLY: admission below still requires the dedicated input
    // thread to decode a REAL report (the exact predicate the 2026-08-07 gate
    // introduced), so a genuinely dead pad is refused exactly as before.
    if (!rawInputWorker_) {
        return false;
    }
    const QString path = rawInputDevicePath_;
    bool probeOpened = false;
    bool probeAnswered = false;
    nudgePhysicalPadOnce(&probeOpened, &probeAnswered);
    // Confirm: re-run the standard poll/mirror until a real report lands. This
    // reuses the production predicates (selector liveness + hasRecentRawInput),
    // so on success every downstream consumer (physicalPadLive_, route logs,
    // the connectVirtualController gate) already agrees. 600 ms covers USB
    // resume latency without a noticeable stall on a click that would
    // otherwise FAIL outright. No reentrancy hazard: the auto retry inside
    // pollPhysicalController (connectVirtualController) is gated on an active
    // stream, which cannot be the case while the Connect gate is still deciding.
    const qint64 waitStartMs = QDateTime::currentMSecsSinceEpoch();
    while (QDateTime::currentMSecsSinceEpoch() - waitStartMs < 600) {
        pollPhysicalController();
        if (physicalPadLive_ || hasRecentRawInput()) {
            appendLog(QStringLiteral(
                          "Controller wake probe: silent pad recovered in %1 ms "
                          "(open=%2 report_poll=%3).")
                          .arg(QDateTime::currentMSecsSinceEpoch() - waitStartMs)
                          .arg(probeOpened ? 1 : 0)
                          .arg(probeAnswered ? 1 : 0));
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    appendLog(QStringLiteral(
                  "Controller wake probe: pad still silent after %1 ms "
                  "(open=%2 report_poll=%3 path_known=%4).")
                  .arg(QDateTime::currentMSecsSinceEpoch() - waitStartMs)
                  .arg(probeOpened ? 1 : 0)
                  .arg(probeAnswered ? 1 : 0)
                  .arg(path.isEmpty() ? 0 : 1));
    return false;
#else
    return false;
#endif
}

void OrionAppController::appendLog(const QString& message)
{
    const QString simplified = message.simplified();
    const bool periodicDiagnostic =
        ui_notifications::isPeriodicMachineDiagnostic(simplified);
    // `logs_` is the user-facing Activity/Debug model, not the audit store. If
    // periodic pipeline telemetry enters this ring it silently ages every
    // useful connection/shot event out even though QML later filters those
    // machine lines. Keep the human ring human-sized (kActivityRingMaxLines,
    // 1000 as of 2026-08-08 so a whole batch stays skimmable in-app); the
    // complete diagnostic stream is still queued to disk below.
    const QString stamped = QStringLiteral("%1  %2")
                                .arg(QDateTime::currentDateTime().toString(QStringLiteral("HH:mm:ss")),
                                     simplified);
    if (!periodicDiagnostic) {
        logs_.append(stamped);
        while (logs_.size() > ui_notifications::kActivityRingMaxLines) {
            logs_.removeFirst();
        }
        logsDirty_ = true;
    }
    // [ORION_ACTIVITY_FEED 2026-09-14 owner] THE customer/engineering split, in
    // one place: ui_notifications::shouldEnterActivityRing(). Engineering
    // telemetry keeps its disk line below and its place in the raw `logs_` ring
    // the Debug page reads — it simply never reaches the Activity feed, which is
    // why 1000 entries there is now hours of real events instead of the twelve
    // minutes of counter lines the 2026-09-14 census measured.
    if (ui_notifications::shouldEnterActivityRing(simplified)) {
        customerLogs_.append(stamped);
        while (customerLogs_.size() > ui_notifications::kActivityRingMaxLines) {
            customerLogs_.removeFirst();
        }
        logsDirty_ = true;
    }
    // Queue the disk line (timestamped now, so order/timestamps stay accurate) and mark the UI dirty;
    // flushPendingLogs() hands the batch to the disk worker and coalesces logsChanged on a 200 ms beat.
    // No filesystem operation runs on this GUI path.
    pendingLogDiskLines_.append(QStringLiteral("%1  %2")
                                    .arg(QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs), simplified));
    // Periodic machine telemetry deliberately reaches only the ordered disk
    // sink. It neither mutates nor wakes the user-facing QML model.
}

void OrionAppController::flushPendingLogs()
{
    if (!pendingLogDiskLines_.isEmpty()) {
        QStringList diskBatch;
        pendingLogDiskLines_.swap(diskBatch);
        if (!appLogSink_.enqueue(diskBatch)) {
            // Only possible after shutdown has stopped the sink. Preserve the
            // original FIFO order rather than silently dropping audit lines.
            diskBatch.append(pendingLogDiskLines_);
            pendingLogDiskLines_ = std::move(diskBatch);
        }
    }
    // [ORION_USER_LOG] (5b) drain the user-facing sink on the same beat. Inert (no file, no
    // write) unless the flag is on AND lines are queued — the diagnostic stream above is
    // untouched either way.
    userLog_.flushTo(orionDataDir(rootDir_) + QStringLiteral("/logs"));
    if (logsDirty_) {
        logsDirty_ = false;
        emit logsChanged();
    }
}

// [RT-MED-09 2026-09-23] Gate for every customer settings save. Refused = the pair on disk is
// already missing/invalid outside the one-time bootstrap (a damaged or hand-edited file). A
// production save must not sign that content by writing a slider change on top of it: the
// in-memory config was LOADED from the rejected file. Repair settings is the only way out.
bool OrionAppController::beginSignedSettingsSave()
{
    QString detail;
    const auto gate = security_.beginSettingsWrite(&detail);
    if (gate == SecurityManager::SettingsWriteGate::Refused) {
        if (!settingsSaveRefusedLogged_) {
            settingsSaveRefusedLogged_ = true;
            appendLog(ui_notifications::settingsRepairCustomerText());
        }
        appendLog(QStringLiteral("Settings engine detail: %1").arg(detail));
        return false;
    }
    if (!detail.isEmpty()) {
        appendLog(QStringLiteral("Settings engine detail: %1").arg(detail));
    }
    return true;
}

// [RT-MED-09 2026-09-23] Customer "Repair settings". Narrow on purpose:
//  - only while the settings signature is missing/invalid (never a general re-sign button);
//  - never signs the rejected file: it is copied aside (settings.rejected.json) for support and
//    replaced by factory defaults, which are then saved + signed through the normal commit;
//  - learning.json (the measured timing) is a separate file and is not touched.
void OrionAppController::repairSettings()
{
    if (security_.verifySettingsSignature()) {
        settingsRepairAvailable_ = false;
        emit statusChanged();
        return;
    }
    if (remoteRunning_) {
        appendLog(QStringLiteral("Disconnect first, then press Repair settings."));
        return;
    }
    QString keptAt;
    (void)security_.preserveRejectedSettings(&keptAt);
    AppConfig defaults(rootDir_);
    QString error;
    if (!config_.save(defaults.data(), &error) || !security_.commitSettingsWrite(&error)) {
        appendLog(QStringLiteral("Repair settings didn't finish (code ST-02). Restart Venice and press Repair settings again; if it repeats, open a ticket in the Venice Discord."));
        appendLog(QStringLiteral("Settings engine detail: repair failed: %1").arg(error));
        updateSecurityStatus();
        return;
    }
    settingsSaveRefusedLogged_ = false;
    appendLog(QStringLiteral("Settings repaired: Venice is back on its default settings. Your shot timing history was kept. Check Shot Lead and your meter style, then press Connect."));
    if (!keptAt.isEmpty()) {
        appendLog(QStringLiteral("Settings engine detail: rejected settings kept at %1").arg(keptAt));
    }
    syncBackendConfig();
    updateSecurityStatus();
    emit settingsChanged();
}

bool OrionAppController::saveConfigSilently(const AppConfigData& data)
{
    if (!beginSignedSettingsSave()) {
        return false;
    }
    QString error;
    if (!config_.save(data, &error)) {
        appendLog(QStringLiteral("Settings save failed: %1").arg(error));
        return false;
    }
    QString signatureError;
    if (!security_.commitSettingsWrite(&signatureError)) {
        appendLog(QStringLiteral("Settings signature failed: %1").arg(signatureError));
    }

    syncBackendConfig();
    // [ORION_METER_DELAY 2026-08-07] Settings were just re-signed above, so an
    // async periodic re-evaluation is enough here. This removes hundreds of ms
    // of blocking work from every settings save.
    (void)securityEvaluationFence_.invalidate();
    periodicSecurityEvaluator_.request(securityEvaluationFence_.current());
    emit settingsChanged();
    return true;
}

void OrionAppController::persistConfig(const AppConfigData& data, const QString& successMessage)
{
    if (!beginSignedSettingsSave()) {
        return;
    }
    QString error;
    if (!config_.save(data, &error)) {
        appendLog(QStringLiteral("Settings save failed: %1").arg(error));
        return;
    }
    QString signatureError;
    if (!security_.commitSettingsWrite(&signatureError)) {
        appendLog(QStringLiteral("Settings signature failed: %1").arg(signatureError));
    }
    syncBackendConfig();
    updateSecurityStatus();
    appendLog(successMessage);
    emit settingsChanged();
}

void OrionAppController::syncBackendConfig()
{
    const bool enableInputPipe = !isXboxRemotePlay(config_.data())
        && qEnvironmentVariable("ORION_INPUT_HOOK") == QLatin1String("1");
    if (orionInput_.enabled() != enableInputPipe) {
        disarmPreciseFire();
        QMutexLocker submitLock(&submitMutex_);
        orionInput_.resetConnection();
        orionInput_.setEnabled(enableInputPipe);
        directPipeOwnsInput_ = false;
    }
    auto engineConfig = config_.data();
    if (isXboxRemotePlay(engineConfig))
        engineConfig.meterBlindBackstop = false; // External windows require real meter evidence.
    automation_.applyConfig(engineConfig, config_.learning());
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] Re-read the loop's published state from the ENGINE
    // rather than from the settings object: applyConfig above is also where the trim is RESET on
    // a committed Shot Lead change and where it is restored (at half strength) at app start, so
    // this is the one place that can keep the caption honest on every one of those paths.
    {
        const bool armed = automation_.bannerLeadTrimEnabled();
        const double shown =
            automation_.bannerLeadTrimMsForType(QStringLiteral("Standstill"));
        if (armed != bannerLeadTrimEnabled_
            || !qFuzzyCompare(1.0 + shown, 1.0 + bannerLeadTrimMs_)) {
            bannerLeadTrimEnabled_ = armed;
            bannerLeadTrimMs_ = shown;
            emit bannerLeadTrimChanged();
        }
    }
    // [ORION_LEAD_AUTO_SEED 2026-09-15] Same reason, same place: applyConfig above is where the
    // seed is re-evaluated against the freshly committed Shot Lead pair, so this is the one
    // handler that can keep the caption honest on the reset-to-Auto and the first-run paths
    // (both of which change the seed without any shot having been fired).
    {
        const bool active = automation_.leadAutoSeedActive();
        const double seedMs = automation_.leadAutoSeedMs();
        const double measuredMs = automation_.leadAutoSeedMeasuredMs();
        const QString kind = automation_.leadAutoSeedKind();
        if (active != leadAutoSeedActive_ || kind != leadAutoSeedKind_
            || !qFuzzyCompare(1.0 + seedMs, 1.0 + leadAutoSeedMs_)
            || !qFuzzyCompare(1.0 + measuredMs, 1.0 + leadAutoSeedMeasuredMs_)) {
            leadAutoSeedActive_ = active;
            leadAutoSeedKind_ = kind;
            leadAutoSeedMs_ = seedMs;
            leadAutoSeedMeasuredMs_ = measuredMs;
            emit leadAutoSeedChanged();
        }
    }
    detector_.applyConfig(config_.data());
    remotePlay_.applyConfig(config_.data());
    // [ORION_METER_DELAY 2026-08-08] Every non-QML save path funnels through here
    // (profile switch, resets, imported settings). Without this line those paths
    // changed the persisted meter-delay fields but the live actuator kept the
    // pre-switch values until restart — only the three QML setters re-primed it.
    applyMeterDelayRuntimeConfig();
    // T3 add-only: declare the release-timing REGIME every config apply, so a batch's log
    // self-identifies which clock path was live (the batch-2 confusion was autonomous_vision
    // silently overriding a hold_start setting). key=value, add-only.
    {
        const auto& timingConfig = config_.data();
        // [ORION_RHYTHM_FLICK_DELAY 2026-09-14] APPEND-ONLY: rhythm_delay_ms is the flick trim
        // (positive = flick fires later), carried here so a batch log says what the trim was for
        // the whole session without having to read settings.json back.
        // [ORION_NO_METER_V2 2026-09-14] hold_ms is H_ref, the blind law's whole user input;
        // delay_ms / lead_ms are retired from the math and are no longer carried here.
        appendLog(QStringLiteral(
            "Input timer: enabled=%1 hold_ms=%2 rhythm=%3 rhythm_delay_ms=%4")
            .arg(timingConfig.inputTimedEnabled ? 1 : 0)
            .arg(timingConfig.noMeterHoldMs, 0, 'f', 1)
            .arg(timingConfig.inputTimedRhythmEnabled ? 1 : 0)
            .arg(timingConfig.rhythmFlickDelayMs, 0, 'f', 1));
        appendLog(QStringLiteral("Timing mode: autonomous=%1 shadow=%2 anchor=%3 tipGate=%4")
                      .arg(timingConfig.autonomousVision ? 1 : 0)
                      .arg(timingConfig.autonomousVisionShadow ? 1 : 0)
                      .arg(timingConfig.feedforwardAnchor)
                      .arg(timingConfig.tipGateEnabled ? 1 : 0));
    }
    telemetry_.consoleIp = config_.data().remotePlayConsoleIp;
    networkBridge_.setConsoleIp(config_.data().remotePlayConsoleIp);
    // [VENICENET WAVE 2B] The DLL scopes its intercept filter to the console IP
    // (set_filter); keep it in lockstep with the diagnostics bridge on every
    // config apply (profile switch, import, reset).
    veniceNet_.setConsoleIp(config_.data().remotePlayConsoleIp);
    const auto& data = config_.data();
    automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
    settingsSnapshot_ = QStringLiteral(
                            "Timing Mode: %1\n"
                            "Tempo Remap: %2\n"
                            "Input Source: %3\n"
                            "Meter Enabled: %4\n"
                            "Meter Style: %5\n"
                            "Meter Color: %6\n"
                            "Release Threshold: %7%\n"
                            "Early/Late Offset: %8 ms\n"
                            "Latency Compensation: %9 ms\n"
                            "Minimum Hold: %10 ms\n"
                            "Maximum Hold: %11 ms\n"
                            "Stable Frames: %12\n"
                            "Fast No Dip: %13\n"
                            "Hold Strategy: predictive\n"
                            "RTT Sync Mode: %14\n"
                            "Network Enabled: %15\n"
                            "Chiaki Console: %16\n"
                            "Chiaki Path: %17\n"
                            "Internal Fallback: %18\n"
                            "Fallback Target: %19%\n"
                            "Decode Comp: %20 ms")
                            .arg(QStringLiteral("Square Hold"),
                                 QStringLiteral("false"),
                                 QStringLiteral("square"),
                                 data.meterEnabled ? QStringLiteral("true") : QStringLiteral("false"),
                                 data.meterStyle,
                                 data.meterColor)
                            .arg(data.releaseThresholdPct, 0, 'f', 1)
                            .arg(data.earlyLateOffsetMs, 0, 'f', 1)
                            .arg(data.latencyCompensationMs, 0, 'f', 1)
                            .arg(data.minimumHoldMs, 0, 'f', 0)
                            .arg(data.maximumHoldMs, 0, 'f', 0)
                            .arg(data.stableFrames)
                            .arg(data.noDipEnabled ? QStringLiteral("true") : QStringLiteral("false"))
                            .arg(data.rttSyncMode,
                                 data.networkEnabled ? QStringLiteral("true") : QStringLiteral("false"),
                                 data.remotePlayConsoleIp.isEmpty() ? QStringLiteral("-") : data.remotePlayConsoleIp,
                                 data.chiakiPath.isEmpty() ? QStringLiteral("-") : data.chiakiPath,
                                 QStringLiteral("bot-owned"),
                                 QString::number(data.releaseThresholdPct, 'f', 1))
                            .arg(data.noMeterDecodeCompMs, 0, 'f', 1);
    emit telemetryChanged();
    emit settingsChanged();
    // [ORION_USER_TIP] Every path through here has just re-applied config to the engine, and
    // the tip-timing effective frame depends on live engine constants (the base-20 regime can
    // have flipped) as well as on the settings flags saved a moment ago — so the card is
    // re-notified from the one place that sees all of those at once.
    emit tipTimingChanged();
}

void OrionAppController::applySecurityStatus(const SecurityStatus& status)
{
    securityDetail_ = status.message;
    securityReleaseManifestRequired_ = status.releaseManifestRequired;
    entitlementState_ = status.entitlementState;
    integrityState_ = status.integrityState;
    lastSecurityAuditEvent_ = status.lastSecurityAuditEvent.isEmpty() ? lastSecurityAuditEvent_ : status.lastSecurityAuditEvent;
    securityLockActive_ = status.securityLockActive;
    securityLockReason_ = status.securityLockReason.isEmpty() ? QStringLiteral("-") : status.securityLockReason;
    // [RT-MED-09] The repair banner is offered only when the settings signature is the lock.
    settingsRepairAvailable_ = status.securityLockActive && !status.settingsSignatureValid
        && status.securityLockReason == QLatin1String("Settings signature missing or invalid");
#ifndef ORION_PRODUCTION_BUILD
    if (securityLockActive_ && licenseState_ == QLatin1String("Local Dev") && localDevAllowed()
        && status.evaluationComplete && !status.releaseManifestRequired) {
        securityLockActive_ = false;
        securityLockReason_ = QStringLiteral("Local dev bypass");
    }
#endif

    if (securityLockActive_) {
        securityState_ = QStringLiteral("Locked");
    } else if (status.debuggerObserved) {
        securityState_ = QStringLiteral("Debugger");
    } else if (!status.vigemBusInstalled) {
        securityState_ = QStringLiteral("ViGEm missing");
    } else if (!status.settingsSignatureValid) {
        securityState_ = QStringLiteral("Settings unsigned");
    } else if (!status.releaseManifestValid && status.releaseManifestRequired) {
        securityState_ = QStringLiteral("Integrity failed");
    } else {
        securityState_ = QStringLiteral("Ready");
    }
    // Applying a newly locked verdict must immediately revoke any precise-fire
    // deadline already copied to the worker, not merely wait for the next input
    // poll to notice the lock.
    syncEngineArmed();
    emit statusChanged();
}

void OrionAppController::updateSecurityStatus()
{
    // Synchronous checkpoints remain fail-closed and authoritative. Invalidating
    // first fences out a periodic result that started before the mutation/action.
    (void)securityEvaluationFence_.invalidate();
    applySecurityStatus(security_.evaluate());
}

void OrionAppController::updateRuntimeStatus()
{
    const QJsonObject data = readStatusJson();
    // The one-second status poll is a compatibility source, not a reason to
    // invalidate every QML status binding. Snapshot the fields it owns and
    // publish only the notifier whose value actually changed.
    const QString previousSessionTime = sessionTime_;
    const QString previousStatusAge = statusAge_;
    const bool previousCoreActive = coreActive_;
    const QString previousScriptState = scriptState_;
    const QString previousScriptDetail = scriptDetail_;
    const QString previousLicenseDetail = licenseDetail_;
    const QString previousTimeLeft = timeLeft_;
    const QString previousTimeLeftDetail = timeLeftDetail_;
    const QString previousMeterRuntimeState = meterRuntimeState_;
    const QString previousHoldSource = holdSource_;
    const QString previousOutputState = outputState_;
    const QString previousLastResult = lastResult_;
    const QString previousBlockReason = blockReason_;
    const QString previousNetSyncReason = netSyncReason_;
    const QString previousVisionPipeline = visionPipeline_;
    const QString previousConsoleIp = telemetry_.consoleIp;
    const int previousInboundPackets = telemetry_.inboundPackets;
    const int previousOutboundPackets = telemetry_.outboundPackets;
    const int previousConsecutivePackets = telemetry_.consecutivePackets;
    const double previousPacketIntervalMs = telemetry_.packetIntervalMs;
    const bool previousPlayingGame = telemetry_.playingGame;
    const int previousPlayerCount = playerCount_;
    const int previousLegitPlayerCount = legitPlayerCount_;
    const int previousCheaterCount = cheaterCount_;
    const auto numericValue = [&data](const QStringList& keys, double fallback) {
        for (const auto& key : keys) {
            const auto value = data.value(key);
            if (value.isDouble()) {
                return value.toDouble(fallback);
            }
            if (value.isString()) {
                bool ok = false;
                const double parsed = value.toString().trimmed().toDouble(&ok);
                if (ok) {
                    return parsed;
                }
            }
        }
        return fallback;
    };
    const auto textValue = [&data](const QStringList& keys, const QString& fallback) {
        for (const auto& key : keys) {
            const auto value = data.value(key);
            if (value.isString()) {
                const auto text = value.toString().trimmed();
                if (!text.isEmpty()) {
                    return text;
                }
            }
        }
        return fallback;
    };
    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    const qint64 elapsed = sessionStartMs_ > 0 ? qMax<qint64>(0, (nowMs - sessionStartMs_) / 1000) : 0;
    sessionTime_ = QStringLiteral("%1:%2:%3")
                       .arg(elapsed / 3600, 2, 10, QLatin1Char('0'))
                       .arg((elapsed % 3600) / 60, 2, 10, QLatin1Char('0'))
                       .arg(elapsed % 60, 2, 10, QLatin1Char('0'));

    double age = -1.0;
    const auto tsValue = data.value(QStringLiteral("status_unix_ts"));
    if (!tsValue.isUndefined()) {
        const double nowS = nowMs / 1000.0;
        age = qMax(0.0, nowS - tsValue.toDouble(nowS));
    }
    const bool feedFresh = age >= 0.0 && age < 2.5;
    const bool cvAlive = data.value(QStringLiteral("cv_script_active")).toBool(false);
    coreActive_ = feedFresh && (cvAlive || !data.isEmpty());
    scriptState_ = coreActive_ ? QStringLiteral("Active") : QStringLiteral("Inactive");
    const auto fps = data.value(QStringLiteral("fps"));
    scriptDetail_ = coreActive_
                        ? QStringLiteral("Capture fresh - %1 FPS").arg(fps.isUndefined() ? QStringLiteral("-") : QString::number(fps.toDouble(), 'f', 1))
                        : QStringLiteral("Waiting for live capture heartbeat");

    licenseDetail_ = licenseState_ == QLatin1String("Local Dev") ? QStringLiteral("Local source mode") : QStringLiteral("Current launcher entitlement");
    if (licenseState_ == QLatin1String("Local Dev")) {
        timeLeft_ = QStringLiteral("Dev");
        timeLeftDetail_ = QStringLiteral("No entitlement granted");
    } else {
        // Single source of truth with the Profile page's hero number: both derive
        // from licenseDaysLeft(), so "Time left" can never contradict "Days left".
        const int days = profileDaysLeft();
        if (days == -2) {
            timeLeft_ = QStringLiteral("Lifetime");
            timeLeftDetail_ = QStringLiteral("No expiry");
        } else if (days < 0) {
            timeLeft_ = QStringLiteral("-");
            timeLeftDetail_ = QStringLiteral("No expiry loaded");
        } else {
            timeLeft_ = days == 1 ? QStringLiteral("1 day")
                                  : QStringLiteral("%1 days").arg(days);
            timeLeftDetail_ = QStringLiteral("Expires %1").arg(
                QDateTime::fromSecsSinceEpoch(profile_.expiryEpochS)
                    .toLocalTime()
                    .toString(QStringLiteral("d MMM yyyy")));
        }
    }

    const bool meterSeen = data.value(QStringLiteral("meter_detected")).toBool(false)
        || data.value(QStringLiteral("hold_meter_detected")).toBool(false)
        || data.value(QStringLiteral("cv_timing_detected")).toBool(false);
    meterRuntimeState_ = meterSeen ? QStringLiteral("Meter") : QStringLiteral("Searching");
    holdSource_ = tempoInputLabel(textOrFallback(data, {QStringLiteral("tempo_input_mode"), QStringLiteral("hold_square_input_source")}, QStringLiteral("square")));

    const bool pulseOn = data.value(QStringLiteral("cv_green_pulse_sent")).toBool(false)
        || data.value(QStringLiteral("gcv_tx_cv_green_pulse")).toBool(false);
    const int gcvMagic = data.value(QStringLiteral("gcv_tx_magic")).toInt(0);
    const int gcvMode = data.value(QStringLiteral("gcv_tx_hold_mode")).toInt(0);
    outputState_ = gcvMagic == 127 ? QStringLiteral("OK / mode %1").arg(gcvMode) : QStringLiteral("No Output");
    lastResult_ = textOrFallback(data, {QStringLiteral("last_result"), QStringLiteral("autogreen_state"), QStringLiteral("meter_state")},
                                 pulseOn ? QStringLiteral("Pulse sent") : (coreActive_ ? QStringLiteral("Waiting") : QStringLiteral("-")));
    if (lastResult_.size() > 26) {
        lastResult_ = lastResult_.left(25) + QStringLiteral("...");
    }

    blockReason_ = textOrFallback(data, {QStringLiteral("autogreen_block_reason"), QStringLiteral("hold_release_block_reason")},
                                  coreActive_ ? QStringLiteral("None") : QStringLiteral("-"));
    if (blockReason_.size() > 26) {
        blockReason_ = blockReason_.left(25) + QStringLiteral("...");
    }
    statusAge_ = age >= 0.0 ? QStringLiteral("%1s").arg(age, 0, 'f', 1) : QStringLiteral("-");
    const bool syncReady = data.value(QStringLiteral("orion_sync_ready")).toBool(false);
    netSyncReason_ = textOrFallback(data, {QStringLiteral("orion_sync_reason")}, syncReady ? QStringLiteral("Ready") : QStringLiteral("Waiting"));
    netSyncReason_.replace(QLatin1Char('_'), QLatin1Char(' '));
    if (!netSyncReason_.isEmpty()) {
        netSyncReason_[0] = netSyncReason_[0].toUpper();
    }

    if (!data.isEmpty() && !networkBridge_.connected()) {
        telemetry_.consoleIp = textValue({QStringLiteral("network_console_ip"), QStringLiteral("console_ip")},
                                         config_.data().remotePlayConsoleIp);
        telemetry_.inboundPackets = qMax(0, static_cast<int>(numericValue({
            QStringLiteral("orion_sync_inbound_total"),
            QStringLiteral("network_inbound_total"),
            QStringLiteral("inbound_packets")
        }, telemetry_.inboundPackets)));
        telemetry_.outboundPackets = qMax(0, static_cast<int>(numericValue({
            QStringLiteral("orion_sync_outbound_total"),
            QStringLiteral("network_outbound_total"),
            QStringLiteral("outbound_packets")
        }, telemetry_.outboundPackets)));
        telemetry_.consecutivePackets = qMax(0, static_cast<int>(numericValue({
            QStringLiteral("network_consecutive_packets"),
            QStringLiteral("orion_sync_consecutive"),
            QStringLiteral("consecutive_packets")
        }, telemetry_.consecutivePackets)));
        telemetry_.packetIntervalMs = qMax(0.0, numericValue({
            QStringLiteral("network_packet_interval_ms"),
            QStringLiteral("orion_sync_packet_interval_ms"),
            QStringLiteral("packet_interval_ms")
        }, telemetry_.packetIntervalMs));
        telemetry_.playingGame = data.value(QStringLiteral("playing_game")).toBool(
            data.value(QStringLiteral("game_detected")).toBool(telemetry_.playingGame));

        playerCount_ = qMax(0, static_cast<int>(numericValue({
            QStringLiteral("player_count"),
            QStringLiteral("players_detected")
        }, playerCount_)));
        legitPlayerCount_ = qMax(0, static_cast<int>(numericValue({
            QStringLiteral("legit_player_count"),
            QStringLiteral("legit_players")
        }, legitPlayerCount_)));
        cheaterCount_ = qMax(0, static_cast<int>(numericValue({
            QStringLiteral("cheater_count"),
            QStringLiteral("cheaters_detected")
        }, cheaterCount_)));

        automation_.updateNetworkQuality(networkAutomationOffset(), networkAutomationJitter());
    }

    const double fill = data.value(QStringLiteral("fill_percent")).toDouble(data.value(QStringLiteral("fill_pct")).toDouble(shot_.fillPct));
    const double conf = data.value(QStringLiteral("confidence")).toDouble(shot_.confidence);
    const auto statusLine = textOrFallback(data, {QStringLiteral("status_line")}, QStringLiteral("-"));
    const auto target = textOrFallback(data, {QStringLiteral("autogreen_target_pct"), QStringLiteral("target_release_pct")}, QStringLiteral("-"));
    const auto targetSource = textOrFallback(data, {QStringLiteral("autogreen_target_source"), QStringLiteral("target_release_source")}, QStringLiteral("-"));
    visionPipeline_ = QStringLiteral(
                          "Fill: %1% | Velocity: %2%/s | Release: %3 | Confidence: %4% | FPS: %5 | Latency: %6 ms\n"
                          "Style: %7 | Color: %8\n"
                          "Status: %9\n"
                          "Tempo: %10 | CV Pulse: %11 | Output: %12 | Target: %13% %14")
                          .arg(fill, 0, 'f', 1)
                          .arg(data.value(QStringLiteral("velocity")).toDouble(0.0), 0, 'f', 1)
                          .arg((data.value(QStringLiteral("release_ready")).toBool(false) || data.value(QStringLiteral("green_window")).toBool(false)) ? QStringLiteral("YES") : QStringLiteral("No"))
                          .arg(conf * 100.0, 0, 'f', 1)
                          .arg(fps.isUndefined() ? QStringLiteral("-") : QString::number(fps.toDouble(), 'f', 1))
                          .arg(data.value(QStringLiteral("latency_ms")).toDouble(config_.data().latencyCompensationMs), 0, 'f', 1)
                          .arg(textOrFallback(data, {QStringLiteral("meter_style")}, config_.data().meterStyle),
                               textOrFallback(data, {QStringLiteral("meter_color_name")}, config_.data().meterColor),
                               statusLine,
                               textOrFallback(data, {QStringLiteral("tempo_state")}, QStringLiteral("-")),
                               pulseOn ? QStringLiteral("true") : QStringLiteral("false"),
                               outputState_,
                               target,
                               targetSource);

    if (sessionTime_ != previousSessionTime) {
        emit sessionTimeChanged();
    }
    if (statusAge_ != previousStatusAge) {
        emit statusAgeChanged();
    }

    const bool runtimeTelemetryChanged = telemetry_.consoleIp != previousConsoleIp
        || telemetry_.inboundPackets != previousInboundPackets
        || telemetry_.outboundPackets != previousOutboundPackets
        || telemetry_.consecutivePackets != previousConsecutivePackets
        || telemetry_.packetIntervalMs != previousPacketIntervalMs
        || telemetry_.playingGame != previousPlayingGame
        || playerCount_ != previousPlayerCount
        || legitPlayerCount_ != previousLegitPlayerCount
        || cheaterCount_ != previousCheaterCount;
    if (runtimeTelemetryChanged) {
        emit telemetryChanged();
    }

    const bool runtimeStatusChanged = coreActive_ != previousCoreActive
        || scriptState_ != previousScriptState
        || scriptDetail_ != previousScriptDetail
        || licenseDetail_ != previousLicenseDetail
        || timeLeft_ != previousTimeLeft
        || timeLeftDetail_ != previousTimeLeftDetail
        || meterRuntimeState_ != previousMeterRuntimeState
        || holdSource_ != previousHoldSource
        || outputState_ != previousOutputState
        || lastResult_ != previousLastResult
        || blockReason_ != previousBlockReason
        || netSyncReason_ != previousNetSyncReason
        || visionPipeline_ != previousVisionPipeline;
    if (runtimeStatusChanged) {
        emit statusChanged();
    }
}

bool OrionAppController::packetBridgeReachable(int timeoutMs) const
{
    QTcpSocket socket;
    socket.connectToHost(QStringLiteral("127.0.0.1"), PacketBridgePort);
    const bool ok = socket.waitForConnected(timeoutMs);
    if (ok) {
        socket.disconnectFromHost();
    }
    return ok;
}

void OrionAppController::ensurePacketBridgeRunning()
{
    const auto& data = config_.data();
    // [VENICENET WAVE 2B] Ensure the meter-delay ACTUATION client (VeniceNet.dll)
    // is loaded + initialised. It is a thin IPC client to the packet-bridge
    // service; loading it is independent of whether the service is up yet (the
    // DLL retries the connection with backoff and reports NOT_INSTALLED until it
    // succeeds). LoadLibrary refuses gracefully on a stale install missing the
    // DLL, and the ABI-version handshake refuses a mismatched engine — both leave
    // veniceNet_.available()==false rather than crashing. The service-bootstrap
    // below still runs so the DLL has something to connect to during the
    // migration window (nexus_svc.py / NexusVisionSvc / VeniceNetSvc).
    if (veniceNet_.ensureLoaded()) {
        emit meterDelayBackendAvailabilityChanged();
        emit meterDelayStatusTextChanged();
    }
    // [ORION_METER_DELAY_LINK 2026-08-08] Same predicate as the constructor start and
    // meterDelayStatusText's bridgeLinkConfigured: network feature OR Meter Delay.
    if (!packetBridgeLinkConfigured(data.networkEnabled, data.meterDelayEnabled)) {
        appendLog(QStringLiteral(
            "Packet bridge disabled by settings (network features off, meter delay off)."));
        return;
    }

    if (packetBridgeReachable()) {
        packetBridgeStartPending_ = false;
        appendLog(QStringLiteral("Packet bridge reachable on 127.0.0.1:%1.").arg(PacketBridgePort));
        return;
    }

    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    if (packetBridgeStartPending_ || (nowMs - lastPacketBridgeStartAttemptMs_) < 15000) {
        return;
    }

    // [ORION_PACKET_BRIDGE_INSTALL_GUARD 2026-08-07, updated 2026-08-08] A CURRENT
    // customer install ships the compiled bridge (packet_bridge\NexusVisionSvc.exe,
    // required by tools/package_orion_release.py) and the installer registers it as
    // the demand-start NexusVisionSvc service (installer/orion.iss
    // RegisterPacketBridgeService), so on such installs the probe below finds the
    // service and this guard passes. The guard still matters for the installs where
    // no backend exists — pre-bridge packages, a partial upgrade, or an sc-create
    // failure the installer reported and continued past. There, every 15s tick would
    // run sc.exe/QProcess::startDetached against a service that cannot exist, and
    // every attempt can briefly flash a console window because sc.exe is a Windows
    // console executable spawned detached from a GUI-subsystem parent. Short-circuit
    // when we can prove nothing can succeed, so those machines don't get a cmd flash
    // every 15 seconds.
    //
    // The check runs ONCE per launch and caches: `packetBridgeAvailabilityChecked_`.
    // A machine with the service registered OR the debug script present takes the
    // original path unchanged; only the provably-backendless case is silenced.
    //
    // [ORION_METER_DELAY_AVAILABILITY 2026-08-08] The probe itself moved to
    // probePacketBridgeAvailability() so the Meter Delay card's
    // meterDelayBackendAvailable property reads the SAME once-per-launch answer
    // instead of re-deriving (or worse, lying). This call site keeps the side
    // effects: the one-shot log line and the availability-changed notify.
    if (!packetBridgeAvailabilityChecked_) {
        (void)probePacketBridgeAvailability();
        emit meterDelayBackendAvailabilityChanged();
    }
    if (!packetBridgeAvailable_) {
        if (!packetBridgeUnavailableLogged_) {
            packetBridgeUnavailableLogged_ = true;
            appendLog(QStringLiteral(
                "Packet bridge: not available on this install (no VeniceNetSvc or "
                "NexusVisionSvc service registered, no nexus_svc.py). Auto-start "
                "suppressed to avoid console-window spawns. Current installers register "
                "the service; if Meter Delay is wanted here, re-run the Venice installer "
                "as administrator."));
        }
        return;
    }
    packetBridgeStartPending_ = true;
    lastPacketBridgeStartAttemptMs_ = nowMs;

    // [ORION_PACKET_BRIDGE_NAMES 2026-08-08 task #64] Resolve WHICH registered name to
    // demand-start: VeniceNetSvc (wave-3 installs) first, NexusVisionSvc (legacy) second.
    // Wave 3 exposed the old hardcode: on a customer install registered as VeniceNetSvc
    // the app never started the service and Meter Delay reported unavailable.
    const QString bridgeServiceName = installedPacketBridgeServiceName();
    const bool serviceInstalled = !bridgeServiceName.isEmpty();
    appendLog(serviceInstalled
                  ? QStringLiteral(
                        "Packet bridge service found: %1. Bridge not reachable; demand-starting it.")
                        .arg(bridgeServiceName)
                  : QStringLiteral(
                        "Packet bridge service not installed: tried [%1]. Starting local sniff-only debug mode.")
                        .arg(orion::packetBridgeServiceNameCandidates()
                                 .join(QStringLiteral(", "))));
#ifdef Q_OS_WIN
    if (serviceInstalled) {
        // [ORION_METER_DELAY 2026-08-07] Suppress the cmd flash. sc.exe is a
        // console subsystem exe; startDetached from a GUI parent briefly popped
        // a black cmd window every 15s of retries. CREATE_NO_WINDOW hides it,
        // and we clear CREATE_NEW_CONSOLE so Qt's default flag doesn't put it
        // back.
        QProcess sc;
        sc.setProgram(QStringLiteral("sc.exe"));
        sc.setArguments({QStringLiteral("start"), bridgeServiceName});
        sc.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments* args) {
            args->flags |= 0x08000000;   // CREATE_NO_WINDOW
            args->flags &= ~0x00000010U; // clear CREATE_NEW_CONSOLE
        });
        sc.startDetached();
    }
#endif

    if (serviceInstalled) {
        QTimer::singleShot(12000, this, [this, bridgeServiceName]() {
            if (packetBridgeReachable(300)) {
                packetBridgeStartPending_ = false;
                appendLog(QStringLiteral("Packet bridge service is online."));
                return;
            }

            appendLog(QStringLiteral("%1 did not open the packet bridge; falling back to local sniff-only debug mode.")
                          .arg(bridgeServiceName));
            if (!startPacketBridgeDebug()) {
                packetBridgeStartPending_ = false;
                appendLog(QStringLiteral("Packet bridge fallback failed. Run install_nexus_service.bat as Administrator or start: .venv311\\Scripts\\python.exe nexus_svc.py debug"));
                return;
            }
            QTimer::singleShot(2600, this, [this]() {
                packetBridgeStartPending_ = false;
                if (packetBridgeReachable(300)) {
                    appendLog(QStringLiteral("Packet bridge debug fallback is online."));
                } else {
                    appendLog(QStringLiteral("Packet bridge still not reachable. WinDivert may need Administrator rights or driver installation."));
                }
            });
        });
        return;
    }

    QTimer::singleShot(500, this, [this]() {
        if (packetBridgeReachable(250)) {
            packetBridgeStartPending_ = false;
            appendLog(QStringLiteral("Packet bridge service is online."));
            return;
        }

        if (!startPacketBridgeDebug()) {
            packetBridgeStartPending_ = false;
            appendLog(QStringLiteral("Packet bridge auto-start failed. Run install_nexus_service.bat as Administrator or start: .venv311\\Scripts\\python.exe nexus_svc.py debug"));
            return;
        }

        appendLog(QStringLiteral("Started local sniff-only packet bridge debug process."));
        QTimer::singleShot(2600, this, [this]() {
            packetBridgeStartPending_ = false;
            if (packetBridgeReachable(300)) {
                appendLog(QStringLiteral("Packet bridge debug process is online."));
            } else {
                appendLog(QStringLiteral("Packet bridge still not reachable. WinDivert may need Administrator rights or driver installation."));
            }
        });
    });
}

QString OrionAppController::installedPacketBridgeServiceName() const
{
#ifdef Q_OS_WIN
    // [ORION_PACKET_BRIDGE_NAMES task #64] First registered candidate wins:
    // sc.exe query <name> exits 0 only for a registered service. The candidate
    // list is venicenet::candidateServiceNames() — VeniceNetSvc (current) before
    // NexusVisionSvc (legacy; customers on old installs MUST keep working).
    return orion::resolveInstalledPacketBridgeServiceName(
        orion::packetBridgeServiceNameCandidates(), [](const QString& name) {
            QProcess query;
            query.start(QStringLiteral("sc.exe"), {QStringLiteral("query"), name});
            if (!query.waitForFinished(1500)) {
                query.kill();
                query.waitForFinished(500);
                return false;
            }
            return query.exitStatus() == QProcess::NormalExit && query.exitCode() == 0;
        });
#else
    return QString();
#endif
}

bool OrionAppController::packetBridgeServiceInstalled() const
{
    return !installedPacketBridgeServiceName().isEmpty();
}

bool OrionAppController::packetBridgeBackendPresent(const QString& rootDir,
                                                    const QString& appDir,
                                                    bool serviceInstalled)
{
    // [ORION_METER_DELAY_AVAILABILITY] Pure decision core, mirrored from the install
    // guard in ensurePacketBridgeRunning(): a bridge backend exists when the elevated
    // NexusVisionSvc is registered OR the nexus_svc.py debug script is present in
    // either launch root (startPacketBridgeDebug() looks in exactly these two
    // places). Static + argument-driven so MeterDelaySettingsPropertyTests can pin
    // both verdicts without constructing a controller.
    if (serviceInstalled) {
        return true;
    }
    return QFileInfo::exists(rootDir + QStringLiteral("/nexus_svc.py"))
        || QFileInfo::exists(appDir + QStringLiteral("/nexus_svc.py"));
}

bool OrionAppController::probePacketBridgeAvailability() const
{
    if (!packetBridgeAvailabilityChecked_) {
        packetBridgeAvailabilityChecked_ = true;
        packetBridgeAvailable_ = packetBridgeBackendPresent(
            rootDir_, QCoreApplication::applicationDirPath(),
            packetBridgeServiceInstalled());
    }
    return packetBridgeAvailable_;
}

bool OrionAppController::meterDelayBackendAvailable() const
{
    return probePacketBridgeAvailability();
}

QString OrionAppController::meterDelayStatusText() const
{
    // [ORION_METER_DELAY_AVAILABILITY] Customer-honest state line for the Meter Delay
    // card. Gathers the live facts and delegates to the static decision core so the
    // precedence (backend absent > off > service link/arm state > actuator ramp) is
    // pinned by MeterDelaySettingsPropertyTests without constructing a controller.
    const auto& cfg = config_.data();
    MeterDelayStatusInputs in;
    in.backendAvailable = meterDelayBackendAvailable();
    in.enabled = cfg.meterDelayEnabled;
    // [VENICENET WAVE 2B] The service arm state + connection now come from the
    // VeniceNet DLL snapshot (the actuation path), mapped onto the honesty enum
    // the static status-line core consumes: HANDLE_OPEN/READY => Armed+connected;
    // NOT_STARTED => Disarmed+connected (connected but intercept not armed);
    // NOT_INSTALLED/ERROR => Unknown+not-connected.
    const int ds = veniceNet_.available() ? veniceNet_.driverState()
                                          : VENICENET_DRIVER_NOT_INSTALLED;
    switch (ds) {
    case VENICENET_DRIVER_HANDLE_OPEN:
    case VENICENET_DRIVER_READY:
        in.serviceState = NetworkBridge::MeterDelayServiceState::Armed;
        in.bridgeConnected = true;
        break;
    case VENICENET_DRIVER_NOT_STARTED:
        in.serviceState = NetworkBridge::MeterDelayServiceState::Disarmed;
        in.bridgeConnected = true;
        break;
    default: // NOT_INSTALLED / ERROR
        in.serviceState = NetworkBridge::MeterDelayServiceState::Unknown;
        in.bridgeConnected = false;
        break;
    }
    in.bridgeLinkConfigured = packetBridgeLinkConfigured(
        cfg.networkEnabled, cfg.meterDelayEnabled);
    in.session = meterDelay_.sessionState();
    in.shot = meterDelay_.shotState();
    in.appliedMs = meterDelay_.currentDelayMs();
    in.targetMs = meterDelay_.targetDelayMs();
    in.reason = meterDelay_.reasonText();
    in.serviceAppliedMs = meterDelayEchoAppliedMs_;
    return meterDelayStatusLine(in);
}

QString OrionAppController::meterDelayStatusLine(const MeterDelayStatusInputs& in)
{
    // The first branch is the ship-blocker fix: a customer install has no packet
    // bridge, so the card must SAY the feature cannot engage instead of letting the
    // toggle pretend.
    if (!in.backendAvailable) {
        return QStringLiteral(
            "Not available on this install — the network-delay service is missing, "
            "so this setting currently has no effect.");
    }
    if (!in.enabled) {
        return QStringLiteral("Off.");
    }
    // [ORION_METER_DELAY_ARM_STATE 2026-08-08] The bridge link and the service's own
    // arm state outrank the actuator's ramp state below: the controller keeps ramping
    // its book-keeping even when the service refuses every verb (nexus_svc ships the
    // intercept DISARMED), and reporting "Active — holding 250 ms" off that
    // book-keeping is exactly the lie this surface exists to prevent.
    switch (in.serviceState) {
    case NetworkBridge::MeterDelayServiceState::Disarmed:
        return QStringLiteral(
            "Delay service is connected but DISARMED — no delay is being applied. "
            "Arming is an operator action on the bridge (--arm-meter-delay or "
            "ORION_METER_DELAY_ARMED=1 before it starts).");
    case NetworkBridge::MeterDelayServiceState::Unknown:
        if (!in.bridgeConnected) {
            if (!in.bridgeLinkConfigured) {
                return QStringLiteral(
                    "Delay service link is off (network features are disabled), so no "
                    "delay is being applied.");
            }
            return QStringLiteral(
                "Waiting for the delay service connection — no delay is being "
                "applied yet.");
        }
        return QStringLiteral(
            "Connected to an older delay service that does not report arming — "
            "cannot confirm any delay is actually applied.");
    case NetworkBridge::MeterDelayServiceState::Armed:
        break;
    }
    switch (in.session) {
    case orion::MeterDelayController::SessionState::Idle:
        return QStringLiteral("Waiting for a live game session (%1).").arg(in.reason);
    case orion::MeterDelayController::SessionState::Probing:
        return QStringLiteral("Checking connection quality…");
    case orion::MeterDelayController::SessionState::Ready:
    case orion::MeterDelayController::SessionState::Backoff:
        break;
    }
    switch (in.shot) {
    case orion::MeterDelayController::ShotState::Locked:
        // [ORION_METER_DELAY_ECHO] When the service has echoed what it is actually
        // applying, quote the service's number — the one the controller cannot fake.
        if (in.serviceAppliedMs >= 0.0) {
            return QStringLiteral("Active — holding %1 ms (service reports %2 ms applied).")
                .arg(in.appliedMs, 0, 'f', 0)
                .arg(in.serviceAppliedMs, 0, 'f', 0);
        }
        return QStringLiteral("Active — holding %1 ms.").arg(in.appliedMs, 0, 'f', 0);
    case orion::MeterDelayController::ShotState::Engaging:
        return QStringLiteral("Engaging — %1 of %2 ms.")
            .arg(in.appliedMs, 0, 'f', 0).arg(in.targetMs, 0, 'f', 0);
    case orion::MeterDelayController::ShotState::Disengaging:
        return QStringLiteral("Releasing — %1 ms.").arg(in.appliedMs, 0, 'f', 0);
    case orion::MeterDelayController::ShotState::Standby:
        break;
    }
    return QStringLiteral("Armed — waiting to engage (%1).").arg(in.reason);
}

bool OrionAppController::startPacketBridgeDebug()
{
    const QStringList scripts = {
        rootDir_ + QStringLiteral("/nexus_svc.py"),
        QCoreApplication::applicationDirPath() + QStringLiteral("/nexus_svc.py"),
    };

    QString script;
    for (const auto& candidate : scripts) {
        if (QFileInfo::exists(candidate)) {
            script = QDir::toNativeSeparators(candidate);
            break;
        }
    }
    if (script.isEmpty()) {
        return false;
    }

    const QStringList pythonCandidates = {
        rootDir_ + QStringLiteral("/.venv311/Scripts/python.exe"),
        rootDir_ + QStringLiteral("/.venv/Scripts/python.exe"),
        QCoreApplication::applicationDirPath() + QStringLiteral("/.venv311/Scripts/python.exe"),
        QCoreApplication::applicationDirPath() + QStringLiteral("/.venv/Scripts/python.exe"),
    };

    QString python;
    for (const auto& candidate : pythonCandidates) {
        if (QFileInfo::exists(candidate)) {
            python = QDir::toNativeSeparators(candidate);
            break;
        }
    }
    if (python.isEmpty()) {
        python = QStringLiteral("python.exe");
    }

#ifdef Q_OS_WIN
    // [ORION_METER_DELAY 2026-08-07] Same no-flash treatment as sc.exe above —
    // python.exe is a console-subsystem executable and would otherwise pop a
    // black cmd window every debug fallback.
    QProcess pyProc;
    pyProc.setProgram(python);
    pyProc.setArguments({script, QStringLiteral("debug")});
    pyProc.setWorkingDirectory(QFileInfo(script).absolutePath());
    pyProc.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments* args) {
        args->flags |= 0x08000000;   // CREATE_NO_WINDOW
        args->flags &= ~0x00000010U; // clear CREATE_NEW_CONSOLE
    });
    return pyProc.startDetached();
#else
    return QProcess::startDetached(
        python,
        {script, QStringLiteral("debug")},
        QFileInfo(script).absolutePath());
#endif
}

QJsonObject OrionAppController::readStatusJson() const
{
    QFile file(orionDataDir(rootDir_) + QStringLiteral("/status.json"));
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    const auto doc = QJsonDocument::fromJson(file.read(512 * 1024));
    return doc.isObject() ? doc.object() : QJsonObject{};
}

QString OrionAppController::tempoInputLabel(const QString& value) const
{
    const auto normalized = value.trimmed().toLower();
    if (normalized.contains(QStringLiteral("stick"))) {
        return QStringLiteral("Stick");
    }
    if (normalized.contains(QStringLiteral("both"))) {
        return QStringLiteral("Square + Stick");
    }
    if (normalized.contains(QStringLiteral("square"))) {
        return QStringLiteral("Square");
    }
    return value.isEmpty() ? QStringLiteral("Square") : value;
}

QString OrionAppController::textOrFallback(const QJsonObject& obj, const QStringList& keys, const QString& fallback) const
{
    for (const auto& key : keys) {
        const auto value = obj.value(key);
        if (value.isString()) {
            const auto text = value.toString().trimmed();
            if (!text.isEmpty()) {
                return text;
            }
        } else if (value.isDouble()) {
            return QString::number(value.toDouble(), 'f', 1);
        } else if (value.isBool()) {
            return value.toBool() ? QStringLiteral("true") : QStringLiteral("false");
        }
    }
    return fallback;
}

QString OrionAppController::telemetryCourtIpMasked() const
{
    const QString raw = telemetryCourtIp();
    if (raw.isEmpty()) {
        return QString{};
    }
    // Host-identity redaction only. Everything the user or support needs to act
    // on (address family, provider block, rough region) survives; the one octet
    // that identifies the individual server is dropped, because this string sits
    // permanently on a window people screenshot and stream. The full value is
    // still in the exported diagnostics bundle (network.txt).
    const QHostAddress address(raw);
    if (address.protocol() == QAbstractSocket::IPv4Protocol) {
        const int lastDot = raw.lastIndexOf(QLatin1Char('.'));
        return lastDot > 0 ? raw.left(lastDot) + QStringLiteral(".***") : raw;
    }
    if (address.protocol() == QAbstractSocket::IPv6Protocol) {
        // Keep the routing prefix (first two groups) and drop the interface half.
        const QStringList groups = address.toString().split(QLatin1Char(':'));
        if (groups.size() >= 2) {
            return groups.at(0) + QLatin1Char(':') + groups.at(1)
                + QStringLiteral(":***");
        }
    }
    // Unparseable: never echo an unrecognised string back to the overlay.
    return QStringLiteral("***");
}

double OrionAppController::networkAutomationOffset() const noexcept
{
    const auto& data = config_.data();
    if (data.rttSyncMode == QLatin1String("Manual")) {
        return data.manualSyncAdjustMs != 0.0 ? data.manualSyncAdjustMs : data.manualOffsetMs;
    }
    if (!telemetry_.rttTargetVerified) {
        return 0.0;
    }
    if (telemetry_.syncAdjustMs > 0.0) {
        return telemetry_.syncAdjustMs;
    }
    if (telemetry_.offsetMs > 0.0) {
        return telemetry_.offsetMs;
    }
    if (telemetry_.rttMs > 0.0) {
        return std::clamp((telemetry_.rttMs * 0.5) + (telemetry_.jitterMs * 1.5), 0.0, 80.0);
    }
    return 0.0;
}

} // namespace orion
