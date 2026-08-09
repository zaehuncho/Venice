#pragma once

#include "OrionExports.h"
#include "OrionTypes.h"

#include <QtCore/QObject>
#include <QtCore/QString>

#include <atomic>

#ifdef Q_OS_WIN
#include <Windows.h>
#endif

namespace orion {

class ORION_REMOTEPLAY_API VirtualController final : public QObject {
    Q_OBJECT
private:
    enum class TargetKind {
        None,
        Ds4,
        Xusb
    };

public:
    explicit VirtualController(QObject* parent = nullptr);
    ~VirtualController() override;

    bool connectController(QString* error = nullptr, bool preferXusb = true);
    void disconnectController();
    bool submit(const ControllerState& state, QString* error = nullptr);

    [[nodiscard]] bool isConnected() const noexcept { return connected_; }
    [[nodiscard]] QString backendName() const noexcept { return backendName_; }
    [[nodiscard]] QString lastError() const noexcept { return lastError_; }
    [[nodiscard]] int xinputUserIndex() const noexcept { return xinputUserIndex_; }
    [[nodiscard]] bool isDs4Backend() const noexcept { return targetKind_ == TargetKind::Ds4; }

signals:
    void statusChanged(bool connected, QString message);

private:
#ifdef Q_OS_WIN
    using PVIGEM_CLIENT = void*;
    using PVIGEM_TARGET = void*;
    using VIGEM_ERROR = unsigned int;

    struct XusbReport {
        unsigned short wButtons = 0;
        unsigned char bLeftTrigger = 0;
        unsigned char bRightTrigger = 0;
        short sThumbLX = 0;
        short sThumbLY = 0;
        short sThumbRX = 0;
        short sThumbRY = 0;
    };

    struct Ds4Report {
        unsigned short wButtons = 0;
        unsigned char bSpecial = 0;
        unsigned char bThumbLX = 128;
        unsigned char bThumbLY = 128;
        unsigned char bThumbRX = 128;
        unsigned char bThumbRY = 128;
        unsigned char bTriggerL = 0;
        unsigned char bTriggerR = 0;
    };

    using vigem_alloc_t = PVIGEM_CLIENT(__cdecl*)();
    using vigem_free_t = void(__cdecl*)(PVIGEM_CLIENT);
    using vigem_connect_t = VIGEM_ERROR(__cdecl*)(PVIGEM_CLIENT);
    using vigem_disconnect_t = void(__cdecl*)(PVIGEM_CLIENT);
    using vigem_target_ds4_alloc_t = PVIGEM_TARGET(__cdecl*)();
    using vigem_target_x360_alloc_t = PVIGEM_TARGET(__cdecl*)();
    using vigem_target_free_t = void(__cdecl*)(PVIGEM_TARGET);
    using vigem_target_add_t = VIGEM_ERROR(__cdecl*)(PVIGEM_CLIENT, PVIGEM_TARGET);
    using vigem_target_remove_t = void(__cdecl*)(PVIGEM_CLIENT, PVIGEM_TARGET);
    using vigem_target_ds4_update_t = VIGEM_ERROR(__cdecl*)(PVIGEM_CLIENT, PVIGEM_TARGET, Ds4Report);
    using vigem_target_x360_update_t = VIGEM_ERROR(__cdecl*)(PVIGEM_CLIENT, PVIGEM_TARGET, XusbReport);
    using vigem_target_x360_get_user_index_t = VIGEM_ERROR(__cdecl*)(PVIGEM_CLIENT, PVIGEM_TARGET, unsigned long*);

    bool loadVigem(QString* error);
    void unloadVigem();
    [[nodiscard]] XusbReport toXusb(const ControllerState& state) const;
    [[nodiscard]] Ds4Report toDs4(const ControllerState& state) const;

    HMODULE vigemDll_ = nullptr;
    PVIGEM_CLIENT client_ = nullptr;
    PVIGEM_TARGET target_ = nullptr;
    vigem_alloc_t vigem_alloc_ = nullptr;
    vigem_free_t vigem_free_ = nullptr;
    vigem_connect_t vigem_connect_ = nullptr;
    vigem_disconnect_t vigem_disconnect_ = nullptr;
    vigem_target_ds4_alloc_t vigem_target_ds4_alloc_ = nullptr;
    vigem_target_x360_alloc_t vigem_target_x360_alloc_ = nullptr;
    vigem_target_free_t vigem_target_free_ = nullptr;
    vigem_target_add_t vigem_target_add_ = nullptr;
    vigem_target_remove_t vigem_target_remove_ = nullptr;
    vigem_target_ds4_update_t vigem_target_ds4_update_ = nullptr;
    vigem_target_x360_update_t vigem_target_x360_update_ = nullptr;
    vigem_target_x360_get_user_index_t vigem_target_x360_get_user_index_ = nullptr;
#endif

    std::atomic<bool> connected_{false};
    std::atomic<bool> disconnecting_{false};
    TargetKind targetKind_ = TargetKind::None;
    QString backendName_ = QStringLiteral("ViGEm/XUSB");
    QString lastError_;
    int xinputUserIndex_ = -1;
};

} // namespace orion
