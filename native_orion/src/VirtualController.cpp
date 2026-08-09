#include "VirtualController.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDir>

#include <algorithm>

namespace orion {

namespace {

constexpr unsigned short XUSB_GAMEPAD_DPAD_UP = 0x0001;
constexpr unsigned short XUSB_GAMEPAD_DPAD_DOWN = 0x0002;
constexpr unsigned short XUSB_GAMEPAD_DPAD_LEFT = 0x0004;
constexpr unsigned short XUSB_GAMEPAD_DPAD_RIGHT = 0x0008;
constexpr unsigned short XUSB_GAMEPAD_START = 0x0010;
constexpr unsigned short XUSB_GAMEPAD_BACK = 0x0020;
constexpr unsigned short XUSB_GAMEPAD_LEFT_THUMB = 0x0040;
constexpr unsigned short XUSB_GAMEPAD_RIGHT_THUMB = 0x0080;
constexpr unsigned short XUSB_GAMEPAD_LEFT_SHOULDER = 0x0100;
constexpr unsigned short XUSB_GAMEPAD_RIGHT_SHOULDER = 0x0200;
constexpr unsigned short XUSB_GAMEPAD_GUIDE = 0x0400;
constexpr unsigned short XUSB_GAMEPAD_A = 0x1000;
constexpr unsigned short XUSB_GAMEPAD_B = 0x2000;
constexpr unsigned short XUSB_GAMEPAD_X = 0x4000;
constexpr unsigned short XUSB_GAMEPAD_Y = 0x8000;
constexpr unsigned int VIGEM_ERROR_NONE = 0x20000000;

short axisToShort(int value)
{
    const int clipped = std::clamp(value, -127, 127);
    return static_cast<short>(std::clamp(clipped * 258, -32768, 32767));
}

unsigned char triggerToByte(int value)
{
    return static_cast<unsigned char>(std::clamp(value, 0, 255));
}

unsigned char axisToByte(int value)
{
    return static_cast<unsigned char>(std::clamp(value + 128, 0, 255));
}

unsigned short dpadNibbleFromState(const ControllerState& state)
{
    if (state.dpad >= 0 && state.dpad <= 8) {
        return static_cast<unsigned short>(state.dpad);
    }

    const bool up = (state.buttons & XUSB_GAMEPAD_DPAD_UP) != 0;
    const bool down = (state.buttons & XUSB_GAMEPAD_DPAD_DOWN) != 0;
    const bool left = (state.buttons & XUSB_GAMEPAD_DPAD_LEFT) != 0;
    const bool right = (state.buttons & XUSB_GAMEPAD_DPAD_RIGHT) != 0;
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

#ifdef Q_OS_WIN
template <typename T>
T resolve(HMODULE module, const char* name)
{
    return reinterpret_cast<T>(GetProcAddress(module, name));
}
#endif

} // namespace

VirtualController::VirtualController(QObject* parent)
    : QObject(parent)
{
}

VirtualController::~VirtualController()
{
    disconnectController();
}

bool VirtualController::connectController(QString* error, bool preferXusb)
{
#ifndef Q_OS_WIN
    lastError_ = QStringLiteral("Virtual controller is only implemented for Windows.");
    if (error) {
        *error = lastError_;
    }
    emit statusChanged(false, lastError_);
    return false;
#else
    if (connected_) {
        return true;
    }
    disconnecting_ = false;
    xinputUserIndex_ = -1;
    if (!loadVigem(error)) {
        return false;
    }

    client_ = vigem_alloc_();
    if (!client_) {
        lastError_ = QStringLiteral("vigem_alloc returned null.");
        if (error) {
            *error = lastError_;
        }
        emit statusChanged(false, lastError_);
        return false;
    }

    auto err = vigem_connect_(client_);
    if (err != VIGEM_ERROR_NONE) {
        lastError_ = QStringLiteral("vigem_connect failed: 0x%1").arg(err, 8, 16, QLatin1Char('0'));
        if (error) {
            *error = lastError_;
        }
        vigem_free_(client_);
        client_ = nullptr;
        emit statusChanged(false, lastError_);
        return false;
    }

    targetKind_ = TargetKind::None;
    if (preferXusb && vigem_target_x360_alloc_) {
        target_ = vigem_target_x360_alloc_();
        targetKind_ = target_ ? TargetKind::Xusb : TargetKind::None;
    }
    if (!target_ && vigem_target_ds4_alloc_ && vigem_target_ds4_update_) {
        target_ = vigem_target_ds4_alloc_();
        targetKind_ = target_ ? TargetKind::Ds4 : TargetKind::None;
    }
    if (!target_ && vigem_target_x360_alloc_) {
        target_ = vigem_target_x360_alloc_();
        targetKind_ = target_ ? TargetKind::Xusb : TargetKind::None;
    }
    if (!target_) {
        lastError_ = QStringLiteral("vigem target allocation failed (DS4 and XUSB unavailable).");
        if (error) {
            *error = lastError_;
        }
        vigem_disconnect_(client_);
        vigem_free_(client_);
        client_ = nullptr;
        emit statusChanged(false, lastError_);
        return false;
    }

    err = vigem_target_add_(client_, target_);
    if (err != VIGEM_ERROR_NONE) {
        lastError_ = QStringLiteral("vigem_target_add failed: 0x%1").arg(err, 8, 16, QLatin1Char('0'));
        if (error) {
            *error = lastError_;
        }
        vigem_target_free_(target_);
        target_ = nullptr;
        vigem_disconnect_(client_);
        vigem_free_(client_);
        client_ = nullptr;
        emit statusChanged(false, lastError_);
        return false;
    }

    connected_ = true;
    backendName_ = targetKind_ == TargetKind::Ds4 ? QStringLiteral("ViGEm/DS4") : QStringLiteral("ViGEm/XUSB");
    if (targetKind_ == TargetKind::Xusb && vigem_target_x360_get_user_index_) {
        unsigned long userIndex = 0;
        const auto userErr = vigem_target_x360_get_user_index_(client_, target_, &userIndex);
        if (userErr == VIGEM_ERROR_NONE) {
            xinputUserIndex_ = static_cast<int>(userIndex);
        }
    }
    lastError_.clear();
    if (targetKind_ == TargetKind::Ds4) {
        emit statusChanged(true, QStringLiteral("Virtual DS4 controller connected."));
    } else if (xinputUserIndex_ >= 0) {
        emit statusChanged(true, QStringLiteral("Virtual XUSB controller connected (slot %1).").arg(xinputUserIndex_ + 1));
    } else {
        emit statusChanged(true, QStringLiteral("Virtual XUSB controller connected."));
    }
    return true;
#endif
}

void VirtualController::disconnectController()
{
    // Atomic re-entry guard. A plain-bool check-then-set raced between the input/ViGEm-callback
    // thread and the destructor: two callers both read false, both passed, and both freed
    // target_/client_ -> heap-corrupting double free (the c0000374 / bad_alloc crash dumps).
    // compare_exchange admits exactly one caller into the teardown.
    bool expected = false;
    if (!disconnecting_.compare_exchange_strong(expected, true)) {
        return;
    }
#ifdef Q_OS_WIN
    if (target_ && client_ && vigem_target_remove_) {
        vigem_target_remove_(client_, target_);
    }
    if (target_ && vigem_target_free_) {
        vigem_target_free_(target_);
    }
    target_ = nullptr;
    targetKind_ = TargetKind::None;
    if (client_ && vigem_disconnect_) {
        vigem_disconnect_(client_);
    }
    if (client_ && vigem_free_) {
        vigem_free_(client_);
    }
    client_ = nullptr;
#endif
    if (connected_) {
        connected_ = false;
        xinputUserIndex_ = -1;
        emit statusChanged(false, QStringLiteral("Virtual controller disconnected."));
    }
    disconnecting_ = false;
}

bool VirtualController::submit(const ControllerState& state, QString* error)
{
#ifndef Q_OS_WIN
    Q_UNUSED(state);
    lastError_ = QStringLiteral("Virtual controller is only implemented for Windows.");
    if (error) {
        *error = lastError_;
    }
    return false;
#else
    if (!connected_ || !client_ || !target_) {
        lastError_ = QStringLiteral("Virtual controller is not connected.");
        if (error) {
            *error = lastError_;
        }
        return false;
    }
    VIGEM_ERROR err = VIGEM_ERROR_NONE;
    if (targetKind_ == TargetKind::Ds4) {
        const auto report = toDs4(state);
        err = vigem_target_ds4_update_(client_, target_, report);
    } else {
        const auto report = toXusb(state);
        err = vigem_target_x360_update_(client_, target_, report);
    }
    if (err != VIGEM_ERROR_NONE) {
        lastError_ = QStringLiteral("%1 update failed: 0x%2")
                         .arg(backendName_)
                         .arg(err, 8, 16, QLatin1Char('0'));
        if (error) {
            *error = lastError_;
        }
        return false;
    }
    return true;
#endif
}

#ifdef Q_OS_WIN

bool VirtualController::loadVigem(QString* error)
{
    if (vigemDll_) {
        return true;
    }

    const auto appDir = QCoreApplication::applicationDirPath();
    const QStringList candidates = {
        appDir + QStringLiteral("/ViGEmClient.dll"),
        appDir + QStringLiteral("/vendor/vigem/ViGEmClient.dll"),
        appDir + QStringLiteral("/vendor/vigem/ViGEmClient64.dll"),
        QStringLiteral("ViGEmClient.dll")
    };

    for (const auto& path : candidates) {
        vigemDll_ = LoadLibraryW(reinterpret_cast<LPCWSTR>(path.utf16()));
        if (vigemDll_) {
            break;
        }
    }
    if (!vigemDll_) {
        lastError_ = QStringLiteral("ViGEmClient.dll not found. Install ViGEmBus or place the client DLL next to OrionNative.exe.");
        if (error) {
            *error = lastError_;
        }
        emit statusChanged(false, lastError_);
        return false;
    }

    vigem_alloc_ = resolve<vigem_alloc_t>(vigemDll_, "vigem_alloc");
    vigem_free_ = resolve<vigem_free_t>(vigemDll_, "vigem_free");
    vigem_connect_ = resolve<vigem_connect_t>(vigemDll_, "vigem_connect");
    vigem_disconnect_ = resolve<vigem_disconnect_t>(vigemDll_, "vigem_disconnect");
    vigem_target_ds4_alloc_ = resolve<vigem_target_ds4_alloc_t>(vigemDll_, "vigem_target_ds4_alloc");
    vigem_target_x360_alloc_ = resolve<vigem_target_x360_alloc_t>(vigemDll_, "vigem_target_x360_alloc");
    vigem_target_free_ = resolve<vigem_target_free_t>(vigemDll_, "vigem_target_free");
    vigem_target_add_ = resolve<vigem_target_add_t>(vigemDll_, "vigem_target_add");
    vigem_target_remove_ = resolve<vigem_target_remove_t>(vigemDll_, "vigem_target_remove");
    vigem_target_ds4_update_ = resolve<vigem_target_ds4_update_t>(vigemDll_, "vigem_target_ds4_update");
    vigem_target_x360_update_ = resolve<vigem_target_x360_update_t>(vigemDll_, "vigem_target_x360_update");
    vigem_target_x360_get_user_index_ =
        resolve<vigem_target_x360_get_user_index_t>(vigemDll_, "vigem_target_x360_get_user_index");

    if (!vigem_alloc_ || !vigem_free_ || !vigem_connect_ || !vigem_disconnect_
        || !vigem_target_free_ || !vigem_target_add_ || !vigem_target_remove_
        || ((!vigem_target_ds4_alloc_ || !vigem_target_ds4_update_)
            && (!vigem_target_x360_alloc_ || !vigem_target_x360_update_))) {
        lastError_ = QStringLiteral("ViGEmClient.dll is missing required exports.");
        if (error) {
            *error = lastError_;
        }
        unloadVigem();
        emit statusChanged(false, lastError_);
        return false;
    }

    return true;
}

void VirtualController::unloadVigem()
{
    if (vigemDll_) {
        FreeLibrary(vigemDll_);
    }
    vigemDll_ = nullptr;
    vigem_alloc_ = nullptr;
    vigem_free_ = nullptr;
    vigem_connect_ = nullptr;
    vigem_disconnect_ = nullptr;
    vigem_target_ds4_alloc_ = nullptr;
    vigem_target_x360_alloc_ = nullptr;
    vigem_target_free_ = nullptr;
    vigem_target_add_ = nullptr;
    vigem_target_remove_ = nullptr;
    vigem_target_ds4_update_ = nullptr;
    vigem_target_x360_update_ = nullptr;
    vigem_target_x360_get_user_index_ = nullptr;
}

VirtualController::XusbReport VirtualController::toXusb(const ControllerState& state) const
{
    XusbReport report;
    report.wButtons = state.buttons;
    report.bLeftTrigger = triggerToByte(state.l2);
    report.bRightTrigger = triggerToByte(state.r2);
    report.sThumbLX = axisToShort(state.leftStickX);
    report.sThumbLY = axisToShort(-state.leftStickY);
    report.sThumbRX = axisToShort(state.rightStickX);
    report.sThumbRY = axisToShort(-state.rightStickY);
    return report;
}

VirtualController::Ds4Report VirtualController::toDs4(const ControllerState& state) const
{
    Ds4Report report;
    unsigned short buttons = dpadNibbleFromState(state);
    if (state.buttons & XUSB_GAMEPAD_X) buttons |= 1 << 4;  // Square
    if (state.buttons & XUSB_GAMEPAD_A) buttons |= 1 << 5;  // Cross
    if (state.buttons & XUSB_GAMEPAD_B) buttons |= 1 << 6;  // Circle
    if (state.buttons & XUSB_GAMEPAD_Y) buttons |= 1 << 7;  // Triangle
    if (state.buttons & XUSB_GAMEPAD_LEFT_SHOULDER) buttons |= 1 << 8;
    if (state.buttons & XUSB_GAMEPAD_RIGHT_SHOULDER) buttons |= 1 << 9;
    if (state.l2 > 24) buttons |= 1 << 10;
    if (state.r2 > 24) buttons |= 1 << 11;
    if (state.buttons & XUSB_GAMEPAD_BACK) buttons |= 1 << 12;   // Create / Share
    if (state.buttons & XUSB_GAMEPAD_START) buttons |= 1 << 13;  // Options
    if (state.buttons & XUSB_GAMEPAD_LEFT_THUMB) buttons |= 1 << 14;
    if (state.buttons & XUSB_GAMEPAD_RIGHT_THUMB) buttons |= 1 << 15;

    unsigned char special = 0;
    if (state.buttons & XUSB_GAMEPAD_GUIDE) special |= 1 << 0;
    if (state.touchpad) special |= 1 << 1;

    report.wButtons = buttons;
    report.bSpecial = special;
    report.bThumbLX = axisToByte(state.leftStickX);
    report.bThumbLY = axisToByte(state.leftStickY);
    report.bThumbRX = axisToByte(state.rightStickX);
    report.bThumbRY = axisToByte(state.rightStickY);
    report.bTriggerL = triggerToByte(state.l2);
    report.bTriggerR = triggerToByte(state.r2);
    return report;
}

#endif

} // namespace orion
