import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

ScrollView {
    id: root
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    ScrollBar.vertical: ScrollBar {
        id: pageScrollBar
        policy: ScrollBar.AsNeeded
        width: 6
        contentItem: Rectangle { radius: 3; color: pageScrollBar.pressed ? Theme.scrollbarThumbActive : Theme.scrollbarThumb }
        background: Rectangle { color: "transparent" }
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4
                Text {
                    text: "Updates"
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontDisplay
                    font.weight: Font.DemiBold
                }
                Text {
                    text: "Release notes delivered by the update service"
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmall
                }
            }
            StatusPill { statusText: orion.displayVersion; tone: "success"; Layout.alignment: Qt.AlignVCenter }
        }

        Card {
            title: orion.updateNotes.length > 0
                   ? "Latest release" + (orion.latestVersion.length > 0 ? " — " + orion.latestVersion : "")
                   : "You're up to date"
            subtitle: orion.updateNotes.length > 0 ? "Verified server release notes" : Theme.productName + " " + orion.displayVersion
            tone: orion.updateAvailable ? "accent" : "neutral"
            Layout.fillWidth: true
            // No height cap: the Text is not clipped, so a long notes body used to
            // spill past the card border at 420px. The page already scrolls, so the
            // card simply grows to fit.
            Layout.preferredHeight: orion.updateNotes.length > 0
                                    ? releaseNotes.implicitHeight + 90
                                    : 132

            Text {
                id: releaseNotes
                anchors.fill: parent
                text: orion.updateNotes.length > 0
                      ? orion.updateNotes
                      : "No newer release notes are available. Venice checks signed updates automatically at launch; live timing and the meter profile stay beside Live Capture."
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontBody
                lineHeight: 1.35
                wrapMode: Text.WordWrap
            }
        }

        Item { Layout.fillHeight: true }
    }
}
