import QtQuick
import QtQuick.Layouts
import "../pages"

Item {
    id: root

    // First-run onboarding is a coach-mark TOUR overlaid on the real launcher
    // (FirstRunTour.qml) instead of a standalone Setup Guide / Preflight tab.
    //
    // [ORION_UI_BUBBLES 2026-09-15 owner: "quick start should just be shown when the
    // customer first launches the UI"] This auto-open is now the ONLY way the tour
    // appears — the sidebar's "Quick Start" button that re-opened it is gone. Two
    // things made it re-show after the customer had already seen it:
    //
    //   1. "Seen" was only persisted when the user reached Finish/Skip/Esc
    //      (FirstRunTour.finish() -> orion.markPreflightComplete()). A customer who
    //      closed the app, or was force-quit, mid-tour left the flag false, so it
    //      opened again on EVERY later launch. It is now marked the moment it
    //      auto-opens: first launch means first launch, however it ends.
    //   2. This handler runs on every AppShell CREATION, and Main.qml's gate Loader
    //      destroys and rebuilds AppShell whenever updateGatePhase / authenticated /
    //      legalAccepted / streamSetupComplete changes — so a mid-session licence
    //      re-check that blipped a gate re-opened the tour over a live game. Marking
    //      it seen up-front closes that too: the very next creation reads the flag
    //      as true. (markPreflightComplete only updates the in-memory config on a
    //      SUCCESSFUL write, so a launcher that cannot write its settings at all can
    //      still see it again — that install has louder problems.)
    //
    // A future Setup control can re-open it with firstRunTour.start(); nothing does
    // today, and TourRegistry anchors stay registered across the app either way.
    Component.onCompleted: {
        if (!orion.preflightComplete) {
            orion.markPreflightComplete()
            firstRunTour.start()
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 16

        Sidebar {
            Layout.preferredWidth: 214
            Layout.fillHeight: true
            // [ORION_UI_BUBBLES 2026-09-15] The rail raises no tour signal any more:
            // its "Quick Start" footer button is gone and its nav delegate is a pure
            // page router. The tour is first-launch onboarding only (above).
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
                //
                // [OVERVIEW REMOVED 2026-09-14 owner] "general" is no longer a page.
                // [PROFILE REMOVED 2026-09-15 owner] neither is "profile" — the
                // licence/account truth moved to the Sidebar's footer strip.
                // Both are deliberately NOT listed here, so a settings.json that
                // still carries current_page="general" or "profile" falls through to
                // Remote Play rather than loading a null component into a blank panel.
                readonly property bool isRemotePage:
                    orion.currentPage !== "dashboard"
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

    Component { id: dashboardPage; DashboardPage {} }
    Component { id: patchNotesPage; PatchNotesPage {} }
    Component { id: debugPage; DebugPage {} }
}
