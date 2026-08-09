pragma Singleton
import QtQuick

// Design tokens lifted verbatim from the approved mockup (dark, single theme).
QtObject {
    // surfaces
    readonly property color bg:        "#080C0A"
    readonly property color surface:   "#0F1613"
    readonly property color raised:    "#161F1A"
    readonly property color raised2:   "#1C271F"
    readonly property color line:      "#243029"
    readonly property color lineSoft:  "#19221D"

    // text
    readonly property color text:  "#EDF5EF"
    readonly property color muted: "#B4CABF"
    readonly property color dim:   "#8CA095"

    // greens
    readonly property color green:       "#33DE76"
    readonly property color greenBright: "#5BFF95"
    readonly property color perfect:     "#93FFAD"
    readonly property color greenDeep:   "#0E3D24"
    readonly property color glow:        Qt.rgba(51/255, 222/255, 118/255, 0.40)

    // state
    readonly property color amber: "#FFA23D"
    readonly property color red:   "#FF6060"

    // type
    readonly property string sans: "Segoe UI"
    readonly property string mono: "Cascadia Mono"

    readonly property int radius: 14

    // honour the OS reduced-motion preference where the platform reports it
    readonly property bool reduceMotion: false
}
