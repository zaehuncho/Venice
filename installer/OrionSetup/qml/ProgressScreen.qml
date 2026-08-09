import QtQuick

// Screen 2 — PROGRESS (the hero). Download rendered as a shot meter with mono
// telemetry and a per-component checklist.
Item {
    id: root

    property real pct: 0
    property real mbNow: 0
    property real speed: 0        // bytes/sec, <0 or 0 => installing/estimating
    property int  etaSec: -1
    property string phaseText: "Downloading"
    property string statusText: "Getting Orion ready…"
    property string subText: "Preparing download"
    property bool perfect: false

    // per-component state, filled from installer.componentState(...)
    property var itemStates: []
    // per-component SHA-256-verified flag, filled from installer.componentVerified(...)
    property var itemVerified: []

    function resetStates() {
        var arr = [];
        var ver = [];
        for (var i = 0; i < installer.components.length; ++i) { arr.push("pending"); ver.push(false); }
        itemStates = arr;
        itemVerified = ver;
    }
    function setItemState(i, s) {
        var arr = itemStates.slice();
        if (i >= 0 && i < arr.length) { arr[i] = s; itemStates = arr; }
    }
    function setItemVerified(i) {
        var arr = itemVerified.slice();
        if (i >= 0 && i < arr.length) { arr[i] = true; itemVerified = arr; }
    }
    // True only if every downloadable component (archives + drivers, i.e. not the
    // "step" row) was genuinely SHA-256 verified. Honest input to the Finish line.
    function allVerified() {
        for (var i = 0; i < installer.components.length; ++i) {
            if (installer.components[i].kind === "step") continue;
            if (!itemVerified[i]) return false;
        }
        return true;
    }

    function fmtSpeed() {
        if (root.pct >= 99.5) return "installing";
        if (root.speed <= 1) return "— MB/s";
        return (root.speed / (1000 * 1000)).toFixed(1) + " MB/s";
    }
    function fmtEta() {
        if (root.pct >= 100) return "done";
        if (root.etaSec < 0 || root.speed <= 1) return "estimating…";
        var m = Math.floor(root.etaSec / 60);
        var s = root.etaSec % 60;
        return "~" + (m < 10 ? "0" + m : m) + ":" + (s < 10 ? "0" + s : s) + " left";
    }

    Column {
        anchors.fill: parent
        anchors.leftMargin: 40
        anchors.rightMargin: 40
        anchors.topMargin: 34
        anchors.bottomMargin: 26
        spacing: 0

        Text {
            text: root.phaseText
            color: Theme.green
            font.family: Theme.mono
            font.pixelSize: 11
            font.letterSpacing: 2.4
            font.capitalization: Font.AllUppercase
            bottomPadding: 14
        }
        Text {
            text: root.statusText
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 14
            font.weight: Font.Medium
            bottomPadding: 4
        }
        Text {
            text: root.subText
            color: Theme.muted
            font.family: Theme.sans
            font.pixelSize: 13
        }

        Item { width: 1; height: 12 }

        // the shot meter
        ShotMeter {
            id: meter
            width: parent.width
            value: root.pct
            perfect: root.perfect
        }

        Item { width: 1; height: 11 }

        // mono telemetry row: MB now / total · speed · eta ······ pct (right)
        Item {
            width: parent.width
            height: 16
            Row {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: 14
                Text {
                    textFormat: Text.StyledText
                    text: "<span style='color:#E9F2EC'>" + Math.round(root.mbNow)
                          + "</span> / " + Math.round(installer.totalMb) + " MB"
                    color: Theme.muted
                    font.family: Theme.mono
                    font.pixelSize: 12
                }
                Text {
                    text: root.fmtSpeed()
                    color: Theme.muted
                    font.family: Theme.mono
                    font.pixelSize: 12
                }
                Text {
                    text: root.fmtEta()
                    color: Theme.muted
                    font.family: Theme.mono
                    font.pixelSize: 12
                }
            }
            Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: Math.floor(root.pct) + "%"
                color: Theme.green
                font.family: Theme.mono
                font.pixelSize: 13
            }
        }

        Item { width: 1; height: 20 }

        // component checklist
        Column {
            width: parent.width
            spacing: 2
            Repeater {
                model: installer.components
                ComponentRow {
                    width: parent.width
                    label: modelData.name
                    size: modelData.sizeText
                    last: index === installer.components.length - 1
                    state: (index < root.itemStates.length)
                           ? root.itemStates[index] : "pending"
                    verified: (index < root.itemVerified.length)
                              ? root.itemVerified[index] : false
                }
            }
        }
    }
}
