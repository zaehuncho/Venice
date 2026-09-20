import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Functional activity log: severity color-coding, filter tabs (All / Shots / Issues), a follow-tail
// toggle, live line count, and copy. `text` is the whole log as one newline-joined string (set by the
// native side); we split + filter + colorize per line in QML.
Card {
    id: root
    // The RAW engineering ring (orion.logText) — everything the app logged.
    property string text: ""
    // [ORION_ACTIVITY_FEED 2026-09-14 owner] The CUSTOMER feed (orion.activityText):
    // the same stream with engineering telemetry removed by the single native rule,
    // ui_notifications::shouldEnterActivityRing. It is the DEFAULT view; the raw
    // ring is one tab away ("All"), the on-disk log is behind "Open folder", and
    // "Copy all" still copies the FULL on-disk tail so support gets everything.
    property string activityText: ""
    title: "Activity Log"
    subtitle: "Launcher, Remote Play, detection + security events"

    property string filterMode: "activity"     // activity | all | shots | issues
    property bool follow: true

    readonly property bool customerFeed: root.filterMode === "activity"
    readonly property string sourceText: root.customerFeed ? root.activityText : root.text
    readonly property var allLines: root.sourceText.length > 0 ? root.sourceText.split("\n") : []

    function lineMatches(line) {
        if (filterMode === "activity" || filterMode === "all")
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
            return Theme.logErr;                                   // red — failures
        if (l.indexOf("warning") !== -1 || l.indexOf("degrad") !== -1 || l.indexOf("stall") !== -1)
            return Theme.logWarn;                                   // amber — warnings
        if (l.indexOf("release issued") !== -1 || l.indexOf("release submit") !== -1)
            return Theme.logOk;                                   // green — a landed shot
        if (l.indexOf("detection presence") !== -1 || l.indexOf("capture") !== -1
            || l.indexOf("fillforecast") !== -1 || l.indexOf("fillkalman") !== -1)
            return Theme.logInfo;                                   // cyan — detection / telemetry
        return Theme.logText;                                       // grey — normal
    }


    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        // ---- toolbar: filter tabs + count + follow + copy ----
        RowLayout {
            Layout.fillWidth: true
            spacing: 6

            Repeater {
                // "Activity" is first and default: the customer feed. "All" is the
                // raw engineering ring, unchanged.
                model: [ { k: "activity", t: "Activity" }, { k: "all", t: "All" },
                         { k: "shots", t: "Shots" }, { k: "issues", t: "Issues" } ]
                delegate: Rectangle {
                    id: filterTab
                    readonly property bool active: root.filterMode === modelData.k
                    Layout.preferredHeight: 26
                    Layout.preferredWidth: tabLabel.implicitWidth + 22
                    radius: Theme.radiusChip
                    color: filterTab.active ? Theme.accentSoft : "transparent"
                    border.width: 1
                    border.color: filterTab.active ? Theme.accent : Theme.borderSoft
                    Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                    Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                    Text {
                        id: tabLabel
                        anchors.centerIn: parent
                        text: modelData.t
                        color: filterTab.active ? Theme.textOnAccent : Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmall
                        font.weight: filterTab.active ? Font.DemiBold : Font.Normal
                    }
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.filterMode = modelData.k }
                }
            }

            Item { Layout.fillWidth: true }

            Text {
                text: root.shownLines.length + " line" + (root.shownLines.length === 1 ? "" : "s")
                color: Theme.textFaint
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaption
            }

            Rectangle {
                Layout.preferredHeight: 26; Layout.preferredWidth: followLabel.implicitWidth + 22
                radius: Theme.radiusChip
                color: root.follow ? Theme.successDim : "transparent"
                border.width: 1; border.color: root.follow ? Theme.logOk : Theme.borderSoft
                Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                Text {
                    id: followLabel; anchors.centerIn: parent
                    // Plain words: the old ⏸ glyph has no Segoe UI Variable form and
                    // fell back to a mismatched emoji face next to the other chips.
                    text: root.follow ? "Following" : "Paused"
                    color: root.follow ? Theme.logOk : Theme.textSecondary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmall
                }
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.follow = !root.follow }
            }

            // "Copy all" puts the bounded tail of the on-disk engineer log on
            // the clipboard (all severities, ignoring the filter tabs — far
            // more history than the rows shown here) via the native
            // controller, which also runs the sharing redaction pass — the
            // paste target is typically a support ticket or an AI chat.
            Rectangle {
                Layout.preferredHeight: 26
                Layout.preferredWidth: copyAllLabel.implicitWidth + 22
                radius: Theme.radiusChip
                color: copyAllArea.copied ? Theme.successDim : "transparent"
                border.width: 1
                border.color: copyAllArea.copied ? Theme.logOk : Theme.borderSoft
                Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                Text {
                    id: copyAllLabel
                    anchors.centerIn: parent
                    text: copyAllArea.copied ? "Copied" : "Copy all"
                    color: copyAllArea.copied ? Theme.logOk : Theme.textSecondary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmall
                }
                MouseArea {
                    id: copyAllArea
                    property bool copied: false
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
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
                radius: Theme.radiusChip
                color: openFolderArea.containsMouse ? Theme.bgCardHover : "transparent"
                border.width: 1
                border.color: openFolderArea.containsMouse ? Theme.borderStrong : Theme.borderSoft
                Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                Text {
                    id: openFolderLabel
                    anchors.centerIn: parent
                    text: "Open folder"
                    color: Theme.textSecondary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmall
                }
                MouseArea {
                    id: openFolderArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: orion.openLogsFolder()
                }
            }
        }

        // ---- the colorized, filtered, follow-tailing line list ----
        // ListView has no `background` property, so the bordered panel is a wrapping Rectangle.
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: Theme.bgInset
            radius: Theme.radiusControl
            border.color: Theme.borderSoft
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
                    selectionColor: Theme.accent
                    selectedTextColor: Theme.textOnAccent
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.fontSmall
                    textFormat: TextEdit.PlainText
                    wrapMode: TextEdit.NoWrap
                }
                onCountChanged: if (root.follow) Qt.callLater(positionViewAtEnd)
            }
        }
    }
}
