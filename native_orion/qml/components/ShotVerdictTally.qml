import QtQuick
import QtQuick.Layouts
import OrionNative

// [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] The live shot-verdict tally, as read off the
// GAME'S OWN shot-feedback banner by the sidecar and rolled over the last 10 shots by the
// controller (orion.bannerXxx, ShotVerdictTally.h).
//
// WHY IT EXISTS: "can't tell if I found my value or not because sometimes it's green". The
// owner was counting banners by eye while moving a slider, which is exactly the thing a
// human cannot do. This block is the answer, and the owner asked for MINIMAL text: the
// tally is DATA (a headline count + a 10-dot strip), and the suggestion is the ONLY
// sentence on it.
//
// Card-less on purpose - it is dropped into the bottom of a Card's own ColumnLayout
// (NoMeterCard and ShotLeadCard mount the same component with the same bindings), so it
// must add no chrome and no fixed height of its own.
ColumnLayout {
    id: root
    objectName: "shotVerdictTally"
    spacing: 4

    // Guarded reads: this component is also instantiated by the QML smoke test against a
    // property-map stub, and an undefined string would take .charAt() with it.
    readonly property int shots: orion.bannerCount10 ? orion.bannerCount10 : 0
    readonly property string pattern: orion.bannerPattern10 ? orion.bannerPattern10 : ""
    readonly property string advice: orion.bannerSuggestion ? orion.bannerSuggestion : ""
    readonly property string lastTiming: orion.bannerLastTiming ? orion.bannerLastTiming : ""
    readonly property string lastCoverage: orion.bannerLastCoverage ? orion.bannerLastCoverage : ""
    readonly property int contested: orion.bannerContested10 ? orion.bannerContested10 : 0
    // The suggestion's own first character carries its kind, so the styling never has to
    // re-derive the rule the controller already applied.
    readonly property bool advisesMove: root.advice.charAt(0) === "→"
                                        || root.advice.charAt(0) === "←"
    readonly property bool advisesKeep: root.advice.charAt(0) === "✓"

    // "last 10: 6 green · 3 early · 1 late", dropping every empty bucket; before the window
    // fills it says how many shots it actually has ("last 4 shots: 3 green · 1 late") so a
    // thin sample can never read as a full one.
    function headline() {
        var n = root.shots
        if (n <= 0)
            return "no shots yet"
        var parts = []
        if (orion.bannerGreen10 > 0) parts.push(orion.bannerGreen10 + " green")
        if (orion.bannerEarly10 > 0) parts.push(orion.bannerEarly10 + " early")
        if (orion.bannerLate10 > 0) parts.push(orion.bannerLate10 + " late")
        if (orion.bannerOther10 > 0) parts.push(orion.bannerOther10 + " other")
        var label = n >= 10 ? "last 10: " : ("last " + n + (n === 1 ? " shot: " : " shots: "))
        return label + parts.join(" · ")
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Text {
            objectName: "shotVerdictHeadline"
            text: root.headline()
            color: Theme.textPrimary
            font.family: Theme.fontMono
            font.pixelSize: 11
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            Layout.fillWidth: true
        }

        Text {
            objectName: "shotVerdictReset"
            visible: root.shots > 0
            text: "Reset"
            color: resetHover.hovered ? Theme.accentHover : Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 10
            font.underline: resetHover.hovered
            HoverHandler { id: resetHover; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: orion.resetBannerTally() }
        }
    }

    // Ten dots, OLDEST -> NEWEST. The shape of the run is the part a slider-mover reads at a
    // glance: three reds in a row means something a "3 late" count does not.
    Row {
        objectName: "shotVerdictDots"
        Layout.fillWidth: true
        spacing: 4

        Repeater {
            model: 10
            delegate: Rectangle {
                required property int index
                readonly property string slot: index < root.pattern.length
                                               ? root.pattern.charAt(index) : ""
                width: 8
                height: 8
                radius: 4
                color: slot === "g" ? Theme.success
                       : slot === "e" ? Theme.warning
                       : slot === "l" ? Theme.danger
                       : slot === "o" ? Theme.textMuted
                       : Theme.borderSoft
                opacity: slot === "o" ? 0.45 : 1.0
            }
        }
    }

    // THE sentence, and the only one. Accent when it points somewhere, success when the
    // value is found, muted while it is still collecting.
    Text {
        objectName: "shotVerdictSuggestion"
        Layout.fillWidth: true
        text: root.advice
        color: root.advisesKeep ? Theme.success
               : root.advisesMove ? Theme.accent
               : Theme.textMuted
        font.family: Theme.fontUi
        font.pixelSize: 11
        font.weight: root.advisesMove || root.advisesKeep ? Font.DemiBold : Font.Normal
        wrapMode: Text.WordWrap
    }

    // What the banner last said, verbatim, plus the one confound that can move the green
    // rate with the timing untouched: a run of contested shots.
    Text {
        objectName: "shotVerdictSubline"
        Layout.fillWidth: true
        visible: root.lastTiming.length > 0
        text: "last: " + root.lastTiming
              + (root.lastCoverage.length > 0 ? " · " + root.lastCoverage : "")
              + (root.contested >= 3
                 ? "   (" + root.contested + " of " + root.shots + " contested)" : "")
        color: Theme.textFaint
        font.family: Theme.fontMono
        font.pixelSize: 10
        elide: Text.ElideRight
    }
}
