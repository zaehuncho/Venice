#include "DeepLinkTargetPolicy.h"

#ifdef Q_OS_WIN

#include <QtCore/QDir>

#include <limits>
#include <string>

namespace orion::deep_link_target_policy {
namespace {

constexpr DWORD kProcessImagePathCapacity = 32'768;

QString stripExtendedPathPrefix(QString path)
{
    // GetFinalPathNameByHandleW normally returns a DOS path prefixed with
    // "\\?\". Normalize it before the ordinal comparison so an equivalent
    // extended/non-extended spelling cannot create a false mismatch.
    if (path.startsWith(QStringLiteral("\\\\?\\UNC\\"), Qt::CaseInsensitive)) {
        path = QStringLiteral("\\\\") + path.mid(8);
    } else if (path.startsWith(QStringLiteral("\\\\?\\"), Qt::CaseInsensitive)) {
        path.remove(0, 4);
    }
    return QDir::cleanPath(QDir::fromNativeSeparators(path));
}

QString canonicalExecutablePath(const QString& path)
{
    if (path.trimmed().isEmpty()) {
        return {};
    }

    const std::wstring nativePath = QDir::toNativeSeparators(path).toStdWString();
    HANDLE file = CreateFileW(
        nativePath.c_str(),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return {};
    }

    const DWORD flags = FILE_NAME_NORMALIZED | VOLUME_NAME_DOS;
    const DWORD required = GetFinalPathNameByHandleW(file, nullptr, 0, flags);
    if (required == 0) {
        CloseHandle(file);
        return {};
    }

    std::wstring buffer(static_cast<size_t>(required), L'\0');
    const DWORD written = GetFinalPathNameByHandleW(
        file, buffer.data(), static_cast<DWORD>(buffer.size()), flags);
    CloseHandle(file);
    if (written == 0 || static_cast<size_t>(written) >= buffer.size()) {
        return {};
    }

    return stripExtendedPathPrefix(
        QString::fromWCharArray(buffer.data(), static_cast<int>(written)));
}

QString processImagePath(DWORD processId)
{
    if (processId == 0) {
        return {};
    }

    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, processId);
    if (!process) {
        return {};
    }

    std::wstring buffer(kProcessImagePathCapacity, L'\0');
    DWORD length = static_cast<DWORD>(buffer.size());
    const BOOL queried = QueryFullProcessImageNameW(process, 0, buffer.data(), &length);
    CloseHandle(process);
    if (!queried || length == 0 || static_cast<size_t>(length) >= buffer.size()) {
        return {};
    }
    return QString::fromWCharArray(buffer.data(), static_cast<int>(length));
}

} // namespace

bool canonicalExecutablePathsEqual(const QString& left, const QString& right)
{
    const QString canonicalLeft = canonicalExecutablePath(left);
    const QString canonicalRight = canonicalExecutablePath(right);
    if (canonicalLeft.isEmpty() || canonicalRight.isEmpty()
        || canonicalLeft.size() > std::numeric_limits<int>::max()
        || canonicalRight.size() > std::numeric_limits<int>::max()) {
        return false;
    }

    return CompareStringOrdinal(
               reinterpret_cast<LPCWCH>(canonicalLeft.utf16()),
               static_cast<int>(canonicalLeft.size()),
               reinterpret_cast<LPCWCH>(canonicalRight.utf16()),
               static_cast<int>(canonicalRight.size()),
               TRUE)
        == CSTR_EQUAL;
}

bool processImageMatchesExecutable(
    DWORD processId,
    const QString& expectedExecutablePath)
{
    const QString imagePath = processImagePath(processId);
    return !imagePath.isEmpty()
        && canonicalExecutablePathsEqual(imagePath, expectedExecutablePath);
}

bool windowOwnedByExecutable(
    HWND window,
    const QString& expectedExecutablePath,
    DWORD* verifiedProcessId)
{
    if (!window || !IsWindow(window)) {
        return false;
    }

    DWORD processId = 0;
    if (GetWindowThreadProcessId(window, &processId) == 0 || processId == 0
        || !processImageMatchesExecutable(processId, expectedExecutablePath)) {
        return false;
    }

    // Close the HWND-reuse window between process-image resolution and return.
    DWORD postCheckProcessId = 0;
    if (!IsWindow(window)
        || GetWindowThreadProcessId(window, &postCheckProcessId) == 0
        || postCheckProcessId != processId) {
        return false;
    }

    if (verifiedProcessId) {
        *verifiedProcessId = processId;
    }
    return true;
}

} // namespace orion::deep_link_target_policy

#endif // Q_OS_WIN
