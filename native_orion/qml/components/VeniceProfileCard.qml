import QtCore
import QtQuick
import QtQuick.Layouts
import OrionNative
import QtQuick.Dialogs

// [VENICE_PROFILE 2026-08-08] Backup / restore of the customer timing surface as a
// portable venice-profile.json (Shot Lead, Tip Timing, Meter Delay, per-shot-type
// learned press constants). Lives on the DEBUG page on purpose: it is a
// once-per-reinstall tool, not a session control, and the Meter Delay card is
// already dense. Everything routes through the single controller entry point
// orion.runVeniceProfileAction("export"|"import", url); the allowlist and the
// import clamps live in C++ (VeniceProfile.h), never here.
Card {
    id: root
    title: "Venice Profile"
    subtitle: "Back up or restore your tuned timing as venice-profile.json"

    property string lastResult: ""
    property bool lastOk: true

    Connections {
        target: orion
        function onVeniceProfileActionCompleted(ok, summary) {
            root.lastOk = ok
            root.lastResult = summary
        }
    }

    FileDialog {
        id: exportDialog
        title: "Export Venice profile"
        fileMode: FileDialog.SaveFile
        nameFilters: ["Venice profile (*.json)"]
        defaultSuffix: "json"
        currentFolder: StandardPaths.writableLocation(StandardPaths.DocumentsLocation)
        selectedFile: currentFolder + "/venice-profile.json"
        onAccepted: orion.runVeniceProfileAction("export", selectedFile)
    }

    FileDialog {
        id: importDialog
        title: "Import Venice profile"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Venice profile (*.json)", "All files (*)"]
        currentFolder: StandardPaths.writableLocation(StandardPaths.DocumentsLocation)
        onAccepted: orion.runVeniceProfileAction("import", selectedFile)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        Text {
            Layout.fillWidth: true
            text: "Saves Shot Lead, Tip Timing, Meter Delay and the learned per-shot press "
                  + "constants to a file you can restore after a reinstall or on a second PC. "
                  + "No license or account data is included; imported values are range-checked."
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontCaption
            wrapMode: Text.Wrap
        }

        Item { Layout.fillHeight: true }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            PrimaryButton {
                text: "Export Profile"
                Layout.fillWidth: true
                Layout.preferredHeight: Theme.controlHeight
                onClicked: exportDialog.open()
            }
            PrimaryButton {
                text: "Import Profile"
                Layout.fillWidth: true
                Layout.preferredHeight: Theme.controlHeight
                onClicked: importDialog.open()
            }
        }

        Text {
            Layout.fillWidth: true
            visible: root.lastResult.length > 0
            text: root.lastResult
            color: root.lastOk ? Theme.success : Theme.danger
            font.family: Theme.fontMono
            font.pixelSize: Theme.fontCaption
            wrapMode: Text.Wrap
            elide: Text.ElideNone
        }
    }
}
