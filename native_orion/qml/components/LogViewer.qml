import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Functional activity log: severity color-coding, filter tabs (All / Shots / Issues), a follow-tail
// toggle, live line count, and copy. `text` is the whole log as one newline-joined string (set by the
// native side); we split + filter + colorize per line in QML.
Card {
    id: root
    property string text: ""
    title: "Activity Log"
    subtitle: "Launcher, Remote Play, detection + security events"

    property string filterMode: "all"          // all | shots | issues
    property bool follow: true

    readonly property var allLines: root.text.length > 0 ? root.text.split("\n") : []

    function lineMatches(line) {
        if (filterMode === "all")
            return true;
        var l = line.toLowerCase();
        if (filterMode === "shots")
            return l.indexOf("release") !== -1 || l.indexOf("shot") !== -1
                || l.indexOf("detection presence") !== -1 || l.indexOf("green") !== -1;
        // issues
        return l.indexOf("error") !== -1 || l.indexOf("warning") !== -1 || l.indexOf("fail") !== -1
            || l.indexOf("abort") !== -1 || l.indexOf("degrad") !== -1 || l.indexOf("stall") !== -1;
    }

    readonly property var shownLines: {
        var out = [];
        for (var i = 0; i < allLines.length; ++i) {
            var ln = allLines[i];
            if (ln.length > 0 && lineMatches(ln))
                out.push(ln);
        }
        return out;
    }

    function lineColor(line) {
        var l = line.toLowerCase();
        if (l.indexOf("error") !== -1 || l.indexOf("fail") !== -1 || l.indexOf("abort") !== -1
            || l.indexOf("not_submitted") !== -1)
            return "#FF6B6B";                                   // red — failures
        if (l.indexOf("warning") !== -1 || l.indexOf("degrad") !== -1 || l.indexOf("stall") !== -1)
            return "#FFC857";                                   // amber — warnings
        if (l.indexOf("release issued") !== -1 || l.indexOf("release submit") !== -1)
            return "#5BE39B";                                   // green — a landed shot
        if (l.indexOf("detection presence") !== -1 || l.indexOf("capture") !== -1
            || l.indexOf("fillforecast") !== -1 || l.indexOf("fillkalman") !== -1)
            return "#6FD3E0";                                   // cyan — detection / telemetry
        return "#AEB6C2";                                       // grey — normal
    }


    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        // ---- toolbar: filter tabs + count + follow + copy ----
        RowLayout {
            Layout.fillWidth: true
            spacing: 6

            Repeater {
                model: [ { k: "all", t: "All" }, { k: "shots", t: "Shots" }, { k: "issues", t: "Issues" } ]
                delegate: Rectangle {
                    Layout.preferredHeight: 26
                    Layout.preferredWidth: tabLabel.implicitWidth + 22
                    radius: 6
                    color: root.filterMode === modelData.k ? "#1E2A3D" : "transparent"
                    border.width: 1
                    border.color: root.filterMode === modelData.k ? "#4F8CFF" : "#263241"
                    Text {
                        id: tabLabel
                        anchors.centerIn: parent
                        text: modelData.t
                        color: root.filterMode === modelData.k ? "#FFFFFF" : "#9AA6B4"
                        font.pixelSize: 12
                    }
                    MouseArea { anchors.fill: parent; onClicked: root.filterMode = modelData.k }
                }
            }

            Item { Layout.fillWidth: true }

            Text {
                text: root.shownLines.length + " line" + (root.shownLines.length === 1 ? "" : "s")
                color: "#6B7684"; font.pixelSize: 11
            }

            Rectangle {
                Layout.preferredHeight: 26; Layout.preferredWidth: followLabel.implicitWidth + 22
                radius: 6
                color: root.follow ? "#12321F" : "transparent"
                border.width: 1; border.color: root.follow ? "#5BE39B" : "#263241"
                Text {
                    id: followLabel; anchors.centerIn: parent
                    text: root.follow ? "▼ Following" : "⏸ Paused"
                    color: root.follow ? "#5BE39B" : "#9AA6B4"; font.pixelSize: 12
                }
                MouseArea { anchors.fill: parent; onClicked: root.follow = !root.follow }
            }

            // "Copy all" puts the bounded tail of the on-disk engineer log on
            // the clipboard (all severities, ignoring the filter tabs — far
            // more history than the rows shown here) via the native
            // controller, which also runs the sharing redaction pass — the
            // paste target is typically a support ticket or an AI chat.
            Rectangle {
                Layout.preferredHeight: 26
                Layout.preferredWidth: copyAllLabel.implicitWidth + 22
                radius: 6
                color: copyAllArea.copied ? "#12321F" : "transparent"
                border.width: 1
                border.color: copyAllArea.copied ? "#5BE39B" : "#263241"
                Text {
                    id: copyAllLabel
                    anchors.centerIn: parent
                    text: copyAllArea.copied ? "Copied" : "Copy all"
                    color: copyAllArea.copied ? "#5BE39B" : "#9AA6B4"
                    font.pixelSize: 12
                }
                MouseArea {
                    id: copyAllArea
                    property bool copied: false
                    anchors.fill: parent
                    onClicked: {
                        orion.copyActivityLog()
                        copied = true
                        copyAllReset.restart()
                    }
                    Timer {
                        id: copyAllReset
                        interval: 1600
                        onTriggered: copyAllArea.copied = false
                    }
                }
            }

            // Escape hatch when the problem is older than the copy's tail
            // bound: open the logs folder and attach orion_native.log itself.
            Rectangle {
                Layout.preferredHeight: 26
                Layout.preferredWidth: openFolderLabel.implicitWidth + 22
                radius: 6
                color: "transparent"
                border.width: 1
                border.color: "#263241"
                Text {
                    id: openFolderLabel
                    anchors.centerIn: parent
                    text: "Open folder"
                    color: "#9AA6B4"
                    font.pixelSize: 12
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: orion.openLogsFolder()
                }
            }
        }

        // ---- the colorized, filtered, follow-tailing line list ----
        // ListView has no `background` property, so the bordered panel is a wrapping Rectangle.
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: "#0B0F14"
            radius: 10
            border.color: "#263241"
            border.width: 1

            ListView {
                id: lv
                anchors.fill: parent
                anchors.margins: 6
                clip: true
                model: root.shownLines
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                // Read-only TextEdit instead of Text so individual lines are
                // selectable in place (drag to select, Ctrl+C). Kept per-line
                // inside the ListView because a flat TextArea would lose the
                // severity colour-coding; "Copy all" above is the primary way
                // to extract the whole log. TextEdit has no elide, so long
                // lines clip at the right edge instead of showing "…".
                delegate: TextEdit {
                    width: lv.width - 16
                    x: 8
                    clip: true
                    text: modelData
                    readOnly: true
                    selectByMouse: true
                    color: root.lineColor(modelData)
                    selectionColor: "#2D7DFF"
                    selectedTextColor: "#FFFFFF"
                    font.family: "Cascadia Mono"
                    font.pixelSize: 12
                    textFormat: TextEdit.PlainText
                    wrapMode: TextEdit.NoWrap
                }
                onCountChanged: if (root.follow) Qt.callLater(positionViewAtEnd)
            }
        }
    }
}
