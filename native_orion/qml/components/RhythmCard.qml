import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Tempo is an opt-in toggle; its input selector appears only while enabled.
Card {
    id: root
    property bool inputTimed: false
    readonly property bool tempoOn: inputTimed ? orion.inputTimedRhythmEnabled : orion.tempoEnabled
    readonly property string selectedPath: inputTimed ? "Button" : orion.tempoInputPath
    title: "Tempo"
    subtitle: "One meter timing, button or stick input"
    Layout.preferredHeight: tempoOn ? 202 : 126

    function selectPath(index) {
        if (!root.tempoOn || root.inputTimed || (index !== 0 && index !== 1)) return
        orion.tempoInputPath = releasePath.model[index]
    }

    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: 10

        Item {
            width: parent.width
            height: tempoToggle.height
            Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: root.tempoOn ? "On" : "Off — standard button shots"
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
            }
            DashboardToggle {
                id: tempoToggle
                objectName: "tempoToggle"
                anchors.right: parent.right
                checked: root.tempoOn
                onToggled: function(value) {
                    if (root.inputTimed) orion.inputTimedRhythmEnabled = value
                    else orion.tempoEnabled = value
                }
            }
        }

        DashboardCombo {
            id: releasePath
            objectName: "releasePathSelector"
            width: parent.width
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
        Text {
            width: parent.width
            visible: root.tempoOn
            text: root.selectedPath === "Stick" ? "Hold the right stick; meter-timed tempo release"
                  : "Hold Square; release with a stick flick"
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 10
            wrapMode: Text.WordWrap
        }
    }
}
