import QtQuick
import QtQuick.Layouts
import "../pages"

Item {
    id: root

    // First-run onboarding is now a coach-mark TOUR overlaid on the real launcher
    // (FirstRunTour.qml) instead of a standalone Setup Guide / Preflight tab. On the
    // first launch (until orion.preflightComplete — reused as the "onboarding seen"
    // flag) it auto-opens; the sidebar "Setup Guide" entry re-opens it any time.
    Component.onCompleted: {
        if (!orion.preflightComplete)
            firstRunTour.start()
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 16

        Sidebar {
            Layout.preferredWidth: 214
            Layout.fillHeight: true
            // "Setup Guide" is no longer a page — it launches the guided tour.
            onTourRequested: firstRunTour.start()
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 14

            // Wrapper Item so the Loader can be faded/translated on page change
            // without messing with the parent layout.
            Item {
                id: pageArea
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: false

                // Remote Play is the DEFAULT page (currentPage defaults to
                // "remotePlay") and is anything that isn't one of the other known
                // pages — matches the original Loader fallback so an unset/unknown
                // page still lands on Remote Play instead of a blank panel.
                readonly property bool isRemotePage:
                    orion.currentPage !== "general"
                    && orion.currentPage !== "dashboard"
                    && orion.currentPage !== "patchNotes"
                    && orion.currentPage !== "debug"

                // Remote Play is kept ALIVE across tab switches (never destroyed),
                // so its embedded Chiaki window + capture pipeline is never torn
                // down. Previously the shared Loader destroyed RemotePlayPage when
                // you left the tab, whose onDestruction hid the Chiaki window ->
                // window-capture went black -> the bot/stream "stopped" and didn't
                // reliably recover. Its internal 250ms timer re-syncs the embed and
                // hides it while this page isn't the current one.
                RemotePlayPage {
                    anchors.fill: parent
                    visible: pageArea.isRemotePage
                }

                Loader {
                    id: pageLoader
                    anchors.fill: parent
                    visible: !pageArea.isRemotePage
                    active: !pageArea.isRemotePage
                    opacity: 1.0
                    Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
                    transform: Translate { id: pageTranslate; x: 0
                        Behavior on x { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                    }
                    sourceComponent: {
                        if (orion.currentPage === "general")
                            return generalPage
                        if (orion.currentPage === "dashboard")
                            return dashboardPage
                        if (orion.currentPage === "patchNotes")
                            return patchNotesPage
                        if (orion.currentPage === "debug")
                            return debugPage
                        return null
                    }
                    // Brief fade+slide-in when the page swaps.
                    onSourceComponentChanged: {
                        opacity = 0.0
                        pageTranslate.x = 14
                        fadeInTimer.restart()
                    }
                    Timer {
                        id: fadeInTimer
                        interval: 30
                        repeat: false
                        onTriggered: {
                            pageLoader.opacity = 1.0
                            pageTranslate.x = 0
                        }
                    }
                }
            }
        }
    }

    // First-run guided tour — overlays the whole shell (sidebar + page area) on top.
    // Safe Mode existed but was never mounted. Overlay it only during the rare
    // fault state so normal capture keeps every pixel of working area.
    TopStatusBar {
        visible: orion.safeModeActive
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 16
        height: 46
        z: 3500
    }

    FirstRunTour {
        id: firstRunTour
        anchors.fill: parent
    }

    Component { id: generalPage; GeneralPage {} }
    Component { id: dashboardPage; DashboardPage {} }
    Component { id: patchNotesPage; PatchNotesPage {} }
    Component { id: debugPage; DebugPage {} }
}
