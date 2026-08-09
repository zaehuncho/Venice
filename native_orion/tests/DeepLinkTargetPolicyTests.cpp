#include "DeepLinkTargetPolicy.h"

#include <QtCore/QDir>
#include <QtCore/QFileInfo>
#include <QtCore/QUuid>
#include <QtTest/QTest>

#include <Windows.h>

#include <string>

using namespace orion::deep_link_target_policy;

namespace {

QString currentExecutablePath()
{
    std::wstring buffer(32'768, L'\0');
    const DWORD length = GetModuleFileNameW(
        nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (length == 0 || static_cast<size_t>(length) >= buffer.size()) {
        return {};
    }
    return QString::fromWCharArray(buffer.data(), static_cast<int>(length));
}

QString differentExistingExecutablePath()
{
    const QString windowsDir = qEnvironmentVariable("SystemRoot", QStringLiteral("C:/Windows"));
    return QDir(windowsDir).filePath(QStringLiteral("System32/cmd.exe"));
}

class TestWindow final {
public:
    TestWindow()
    {
        className_ = QStringLiteral("OrionDeepLinkTargetPolicy_%1")
                         .arg(QUuid::createUuid().toString(QUuid::Id128));
        WNDCLASSW windowClass{};
        windowClass.lpfnWndProc = DefWindowProcW;
        windowClass.hInstance = GetModuleHandleW(nullptr);
        classNameStorage_ = className_.toStdWString();
        windowClass.lpszClassName = classNameStorage_.c_str();
        atom_ = RegisterClassW(&windowClass);
        if (atom_ != 0) {
            window_ = CreateWindowExW(
                0,
                classNameStorage_.c_str(),
                L"Orion",
                WS_OVERLAPPED,
                0, 0, 100, 100,
                nullptr, nullptr,
                GetModuleHandleW(nullptr),
                nullptr);
        }
    }

    ~TestWindow()
    {
        if (window_) {
            DestroyWindow(window_);
        }
        if (atom_ != 0) {
            UnregisterClassW(classNameStorage_.c_str(), GetModuleHandleW(nullptr));
        }
    }

    [[nodiscard]] HWND handle() const noexcept { return window_; }

private:
    QString className_;
    std::wstring classNameStorage_;
    ATOM atom_ = 0;
    HWND window_ = nullptr;
};

} // namespace

class DeepLinkTargetPolicyTests final : public QObject {
    Q_OBJECT

private slots:
    void currentProcessMatchesExactExecutable()
    {
        QVERIFY(processImageMatchesExecutable(GetCurrentProcessId(), currentExecutablePath()));
    }

    void canonicalComparisonUsesWindowsCaseInsensitiveSemantics()
    {
        const QString exact = currentExecutablePath();
        QString alternateCase = exact;
        for (qsizetype i = 0; i < alternateCase.size(); ++i) {
            const QChar ch = alternateCase.at(i);
            if (ch.isLetter()) {
                alternateCase[i] = ch.isUpper() ? ch.toLower() : ch.toUpper();
            }
        }
        QVERIFY(canonicalExecutablePathsEqual(exact, alternateCase));
    }

    void currentProcessRejectsDifferentExecutable()
    {
        const QString different = differentExistingExecutablePath();
        QVERIFY2(QFileInfo::exists(different), qPrintable(different));
        QVERIFY(!processImageMatchesExecutable(GetCurrentProcessId(), different));
    }

    void unresolvedExecutablePathFailsClosed()
    {
        const QString missing = QDir::temp().filePath(
            QStringLiteral("orion-missing-%1.exe")
                .arg(QUuid::createUuid().toString(QUuid::Id128)));
        QVERIFY(!QFileInfo::exists(missing));
        QVERIFY(!canonicalExecutablePathsEqual(currentExecutablePath(), missing));
        QVERIFY(!processImageMatchesExecutable(GetCurrentProcessId(), missing));
    }

    void windowMustBelongToExpectedExecutable()
    {
        TestWindow window;
        QVERIFY(window.handle());

        DWORD verifiedProcessId = 0;
        QVERIFY(windowOwnedByExecutable(
            window.handle(), currentExecutablePath(), &verifiedProcessId));
        QCOMPARE(verifiedProcessId, GetCurrentProcessId());

        verifiedProcessId = 0xFFFFFFFFu;
        QVERIFY(!windowOwnedByExecutable(
            window.handle(), differentExistingExecutablePath(), &verifiedProcessId));
        QCOMPARE(verifiedProcessId, 0xFFFFFFFFu);
    }

    void invalidWindowFailsClosed()
    {
        QVERIFY(!windowOwnedByExecutable(nullptr, currentExecutablePath(), nullptr));
    }
};

QTEST_APPLESS_MAIN(DeepLinkTargetPolicyTests)

#include "DeepLinkTargetPolicyTests.moc"
