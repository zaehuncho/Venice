// OrionSetup — a small standalone downloader/installer for Orion.
//
// Frameless dark Qt6 QML window that downloads Orion's components from the
// release host, verifies each, installs them + the controller drivers, and
// creates shortcuts. Requires administrator (see OrionSetup.manifest).
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QIcon>
#include <QQuickWindow>

#include "InstallerController.h"

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("OrionSetup"));
    app.setOrganizationName(QStringLiteral("Orion"));
    app.setApplicationDisplayName(QStringLiteral("Orion Installer"));
    app.setWindowIcon(QIcon(QStringLiteral(":/icon.ico")));

    InstallerController controller;

    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("installer"), &controller);

    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed,
                     &app, []() { QCoreApplication::exit(-1); },
                     Qt::QueuedConnection);

    engine.loadFromModule("OrionSetup", "Main");
    if (engine.rootObjects().isEmpty())
        return -1;

    return app.exec();
}
