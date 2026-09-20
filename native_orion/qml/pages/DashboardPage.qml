import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

Item {
    id: root

    readonly property bool accessReady: orion.licenseState !== "Locked"
                                        && orion.licenseState !== "Checking"
                                        && !orion.securityLockActive

    Loader {
        anchors.fill: parent
        sourceComponent: setupDash
    }

    // =====================================================================
    // SETUP -- concise production configuration on the existing meter key.
    // =====================================================================
    Component {
        id: setupDash

        ScrollView {
            id: setupScroll
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical: ScrollBar {
                id: setupScrollBar
                policy: ScrollBar.AsNeeded
                width: 6
                contentItem: Rectangle {
                    radius: 3
                    color: setupScrollBar.pressed
                           ? Theme.scrollbarThumbActive : Theme.scrollbarThumb
                }
                background: Rectangle { color: "transparent" }
            }

            ColumnLayout {
                width: setupScroll.availableWidth
                spacing: 12

                // [SETUP STATUS CARD REMOVED 2026-09-14 owner] The "Production Setup"
                // card — four status pills reading Console / Video / Reconnect /
                // Timing — is deleted: every pill restated a control sitting a few
                // pixels below it in the Connection card, and the Timing pill's twin
                // on the Live page went the same day. The engine property the Timing
                // pill read, orion.latencyCalibrationReady, is unchanged and simply
                // has no QML consumer now. (Both pill objectNames are asserted ABSENT
                // from this file by native_orion/tests, so do not name them here.)
                //
                // [ORION_USER_LEAD] Shot Lead used to sit here too. It moved to the LIVE
                // page's side panel (2026-08-04, user: "detection box and lead should be
                // in the live tab under network") because it is tuned against the game's
                // own TIMING banner: the card is useless anywhere the user cannot see
                // the shot land.

                Card {
                    title: "Connection"
                    subtitle: "Console, video source, stream quality, and audio"
                    Layout.fillWidth: true
                    // +80 = Card's title/subtitle header plus its 16px margins and
                    // 12px header gap; +70 left the form ~10px short and squeezed.
                    Layout.preferredHeight: streamForm.implicitHeight + 80

                    ColumnLayout {
                        anchors.fill: parent
                        StreamSetupForm {
                            id: streamForm
                            Layout.fillWidth: true
                        }
                    }
                }

                // [ACCOUNT CARD -> LICENCE STRIP 2026-09-14/15 owner] The Account
                // card that landed here when the Overview tab was deleted is gone: it
                // is account truth, not configuration. It became the Profile tab, and
                // then -- owner, 2026-09-15 -- the Sidebar footer licence strip, which
                // now owns the licence state, days left, the masked key + copy, the
                // HWID reset allowance, the machine binding and the build/channel line.
            }
        }
    }
}
