#include "VideoInputDeviceEnumeration.h"

#include <QtCore/QCryptographicHash>

#ifdef Q_OS_WIN
#include <Windows.h>
#include <dshow.h>
#endif

namespace orion {

QString stableVideoInputDeviceId(const QString& monikerDisplayName)
{
    const QString normalized = monikerDisplayName.trimmed().toCaseFolded().normalized(
        QString::NormalizationForm_C);
    if (normalized.isEmpty()) {
        return {};
    }

    // Domain-separate the digest so a future identity source can evolve without
    // accidentally comparing equal to this DirectShow-moniker scheme.
    QByteArray material("orion-dshow-moniker-v1", 22);
    material.append('\0');
    material.append(normalized.toUtf8());
    const QByteArray digest = QCryptographicHash::hash(
        material, QCryptographicHash::Sha256).toHex();
    return QStringLiteral("dshow-moniker-sha256-v1:%1")
        .arg(QString::fromLatin1(digest));
}

void appendVideoInputDevice(VideoInputDeviceInventory& inventory,
                            const QString& friendlyName,
                            const QString& monikerDisplayName)
{
    const QString cleanedName = friendlyName.trimmed();
    inventory.friendlyNames.append(
        cleanedName.isEmpty() ? QStringLiteral("Unknown device") : cleanedName);
    inventory.stableIds.append(stableVideoInputDeviceId(monikerDisplayName));
}

bool insertVideoInputDeviceEnvironment(QProcessEnvironment& environment,
                                       const VideoInputDeviceInventory& inventory)
{
    const QString namesKey = QStringLiteral("ORION_VIDEO_DEVICE_NAMES");
    const QString idsKey = QStringLiteral("ORION_VIDEO_DEVICE_IDS");
    environment.remove(namesKey);
    environment.remove(idsKey);
    if (inventory.isEmpty() || !inventory.isAligned()) {
        return false;
    }
    environment.insert(namesKey, inventory.friendlyNames.join(QLatin1Char('|')));
    if (inventory.hasAnyStableId()) {
        environment.insert(idsKey, inventory.stableIds.join(QLatin1Char('|')));
    }
    return true;
}

VideoInputDeviceInventory enumerateVideoInputDevices()
{
    VideoInputDeviceInventory inventory;
#ifdef Q_OS_WIN
    const HRESULT coInit = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    const bool comAvailable = SUCCEEDED(coInit) || coInit == RPC_E_CHANGED_MODE;
    if (!comAvailable) {
        return inventory;
    }

    IBindCtx* bindContext = nullptr;
    (void)CreateBindCtx(0, &bindContext);

    ICreateDevEnum* deviceEnumerator = nullptr;
    if (SUCCEEDED(CoCreateInstance(CLSID_SystemDeviceEnum, nullptr, CLSCTX_INPROC_SERVER,
                                   IID_ICreateDevEnum,
                                   reinterpret_cast<void**>(&deviceEnumerator)))
        && deviceEnumerator) {
        IEnumMoniker* monikerEnumerator = nullptr;
        if (deviceEnumerator->CreateClassEnumerator(
                CLSID_VideoInputDeviceCategory, &monikerEnumerator, 0) == S_OK
            && monikerEnumerator) {
            IMoniker* moniker = nullptr;
            while (monikerEnumerator->Next(1, &moniker, nullptr) == S_OK) {
                QString friendlyName;
                QString displayName;

                LPOLESTR rawDisplayName = nullptr;
                if (SUCCEEDED(moniker->GetDisplayName(
                        bindContext, nullptr, &rawDisplayName))
                    && rawDisplayName) {
                    displayName = QString::fromWCharArray(rawDisplayName);
                }
                if (rawDisplayName) {
                    CoTaskMemFree(rawDisplayName);
                }

                IPropertyBag* propertyBag = nullptr;
                if (SUCCEEDED(moniker->BindToStorage(
                        nullptr, nullptr, IID_IPropertyBag,
                        reinterpret_cast<void**>(&propertyBag)))
                    && propertyBag) {
                    VARIANT value;
                    VariantInit(&value);
                    if (SUCCEEDED(propertyBag->Read(
                            L"FriendlyName", &value, nullptr))
                        && value.vt == VT_BSTR && value.bstrVal) {
                        friendlyName = QString::fromWCharArray(value.bstrVal);
                    }
                    VariantClear(&value);
                    propertyBag->Release();
                }

                // Append outside the property-bag branch. DirectShow already
                // consumed this index even if the optional name read failed.
                appendVideoInputDevice(inventory, friendlyName, displayName);
                moniker->Release();
            }
            monikerEnumerator->Release();
        }
        deviceEnumerator->Release();
    }

    if (bindContext) {
        bindContext->Release();
    }
    if (SUCCEEDED(coInit)) {
        CoUninitialize();
    }
#endif
    return inventory;
}

} // namespace orion
