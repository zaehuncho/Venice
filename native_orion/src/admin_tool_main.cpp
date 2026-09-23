#include "AdminToolController.h"
#include "SecurityManager.h"

#include <QtCore/QDir>
#include <QtCore/QFileInfo>
#include <QtCore/QFile>
#include <QtCore/QStringList>
#include <QtCore/QTextStream>
#include <QtGui/QIcon>
#include <QtQml/QQmlApplicationEngine>
#include <QtQml/QQmlContext>
#include <QtQuickControls2/QQuickStyle>
#include <QtWidgets/QApplication>
#include <QtWidgets/QMessageBox>

int main(int argc, char* argv[])
{
    QApplication app(argc, argv);
#if defined(ORION_OWNER_TOOL)
    QApplication::setApplicationName(QStringLiteral("Venice Owner"));
    constexpr orion::AdminToolController::Mode mode = orion::AdminToolController::Mode::Owner;
    const QUrl qmlEntry(QStringLiteral("qrc:/qt/qml/OrionOwner/qml/admin/AdminMain.qml"));
    const QUrl legacyEntry(QStringLiteral("qrc:/OrionOwner/qml/admin/AdminMain.qml"));
#else
    QApplication::setApplicationName(QStringLiteral("Venice Staff"));
    constexpr orion::AdminToolController::Mode mode = orion::AdminToolController::Mode::Staff;
    const QUrl qmlEntry(QStringLiteral("qrc:/qt/qml/OrionStaff/qml/admin/AdminMain.qml"));
    const QUrl legacyEntry(QStringLiteral("qrc:/OrionStaff/qml/admin/AdminMain.qml"));
#endif
    // Customer-facing identity is Venice (2026-09-21). Safe to rename: the consoles keep no
    // QSettings/QStandardPaths state under the organisation name - credentials live in memory.
    QApplication::setOrganizationName(QStringLiteral("Venice"));
    QQuickStyle::setStyle(QStringLiteral("Basic"));

    const QString appDir = QCoreApplication::applicationDirPath();
    QString rootDir = appDir;
    const QString desktopNexus = QDir::homePath() + QStringLiteral("/Desktop/NexusVision");
    const QString appDirNative = QDir::fromNativeSeparators(appDir).toLower();
    if (appDirNative.contains(QStringLiteral("/native_orion/build/"))
        && QFileInfo::exists(desktopNexus + QStringLiteral("/settings.json"))) {
        rootDir = desktopNexus;
    }
    const QString iconPath = rootDir + QStringLiteral("/assets/orion.ico");
    if (QFileInfo::exists(iconPath)) {
        QApplication::setWindowIcon(QIcon(iconPath));
    }
    const bool checkStartupSecurityOnly = QApplication::arguments().contains(QStringLiteral("--check-startup-security"));

#ifdef ORION_PRODUCTION_BUILD
    {
        orion::SecurityManager startupSecurity(rootDir);
        QString integrityDetail;
        if (!startupSecurity.verifyReleaseIntegrity(&integrityDetail)) {
            if (checkStartupSecurityOnly) {
                return 2;
            }
            QMessageBox::critical(
                nullptr,
                QApplication::applicationName(),
                QStringLiteral("Release integrity verification failed.\n\n%1\n\nThis privileged tool will now exit.")
                    .arg(integrityDetail.isEmpty() ? QStringLiteral("Unknown integrity failure") : integrityDetail));
            return 2;
        }
    }
#endif
    if (checkStartupSecurityOnly) {
        return 0;
    }

    QQmlApplicationEngine engine;
    orion::AdminToolController controller(rootDir, mode);
    engine.rootContext()->setContextProperty(QStringLiteral("admin"), &controller);
    QStringList qmlWarnings;
    QObject::connect(&engine, &QQmlApplicationEngine::warnings, &app, [&qmlWarnings](const QList<QQmlError>& warnings) {
        for (const auto& warning : warnings) {
            qmlWarnings.append(warning.toString());
        }
    });
    engine.load(qmlEntry);
    if (engine.rootObjects().isEmpty()) {
        engine.load(legacyEntry);
    }
    if (engine.rootObjects().isEmpty()) {
        QFile log(rootDir + QStringLiteral("/native_orion/admin_tool_qml_runtime.log"));
        if (log.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) {
            QTextStream stream(&log);
            stream << "Tried " << qmlEntry.toString() << '\n';
            stream << "Tried " << legacyEntry.toString() << '\n';
            for (const auto& warning : qmlWarnings) {
                stream << warning << '\n';
            }
        }
        return 1;
    }
    return QApplication::exec();
}
