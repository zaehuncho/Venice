#include "MachineIdentity.h"

#include <QtCore/QByteArray>
#include <QtCore/QCryptographicHash>
#include <QtCore/QSysInfo>

#ifdef Q_OS_WIN
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#include <winreg.h>
#endif

namespace orion {

namespace {

// Byte-for-byte the derivation that used to live in SecurityManager.cpp's
// anonymous namespace. Kept identical so the digest never changes across the
// refactor: HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid, WOW64-64 view,
// trimmed, UTF-8. Empty on any failure (same as before).
QByteArray registryValueMachineGuid()
{
#ifdef Q_OS_WIN
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, L"SOFTWARE\\Microsoft\\Cryptography", 0, KEY_READ | KEY_WOW64_64KEY, &key) != ERROR_SUCCESS) {
        return {};
    }
    wchar_t buffer[256] = {};
    DWORD bytes = sizeof(buffer);
    const auto status = RegQueryValueExW(key, L"MachineGuid", nullptr, nullptr, reinterpret_cast<LPBYTE>(buffer), &bytes);
    RegCloseKey(key);
    if (status != ERROR_SUCCESS || bytes == 0) {
        return {};
    }
    return QString::fromWCharArray(buffer).trimmed().toUtf8();
#else
    return {};
#endif
}

QString hexSha256(const QByteArray& data)
{
    return QString::fromLatin1(QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex());
}

} // namespace

QString deriveMachineId()
{
    QByteArray material;
    material += QSysInfo::machineHostName().toUtf8();
    material += '|';
    material += QSysInfo::machineUniqueId();
    material += '|';
    material += registryValueMachineGuid();
    return hexSha256(material).left(64);
}

} // namespace orion
