import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Tempo: one toggle; while it is on, one "Release with" picker (Button / Stick) and a one-line hint.
// [2026-09-23 owner] Redesigned as a simple toggle + dropdown: content-sized (no fixed heights that
// crushed or padded the rows), label/control rows aligned like the other cards in the panel.
Card {
    id: root
    property bool inputTimed: false
    readonly property bool tempoOn: inputTimed ? orion.inputTimedRhythmEnabled : orion.tempoEnabled
    readonly property string selectedPath: inputTimed ? "Button" : orion.tempoInputPath
    title: "Tempo"
    subtitle: "Release shots with a stick flick"
    Layout.preferredHeight: tempoCol.implicitHeight + 84

    function selectPath(index) {
        if (!root.tempoOn || root.inputTimed || (index !== 0 && index !== 1)) return
        orion.tempoInputPath = releasePath.model[index]
    }

    ColumnLayout {
        id: tempoCol
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Text {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                elide: Text.ElideRight
                text: root.tempoOn ? "On" : "Off"
                color: root.tempoOn ? Theme.textPrimary : Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
                font.weight: Font.DemiBold
            }
            DashboardToggle {
                id: tempoToggle
                objectName: "tempoToggle"
                checked: root.tempoOn
                onToggled: function(value) {
                    if (root.inputTimed) orion.inputTimedRhythmEnabled = value
                    else orion.tempoEnabled = value
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            visible: root.tempoOn
            Text {
                text: "Release with"
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
            }
            DashboardCombo {
                id: releasePath
                objectName: "releasePathSelector"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                visible: root.tempoOn
                // The shelved input-timed route is Square-only; do not expose a
                // trigger it cannot own, or change meter settings through that card.
                enabled: !root.inputTimed
                model: ["Button", "Stick"]
                value: root.selectedPath
                onActivated: function(index) {
                    root.selectPath(index)
                    value = Qt.binding(function() { return root.selectedPath })
                }
            }
        }

        Text {
            objectName: "tempoHint"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            visible: root.tempoOn
            text: root.selectedPath === "Stick" ? "Shoot with the right stick; Venice times the release."
                  : "Hold Square to shoot; Venice releases with a stick flick."
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
    }
}
