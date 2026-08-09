import QtQuick
import OrionNative

// Static, render-cheap ambient depth for Venice. No Canvas, timers, particles,
// or animation: the 60 FPS preview keeps the GUI/render budget.
Item {
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: "#050B15" }
            GradientStop { position: 0.55; color: "#02060C" }
            GradientStop { position: 1.0; color: "#010307" }
        }
    }

    Rectangle {
        width: parent.width * 0.54
        height: parent.height * 1.45
        x: parent.width * 0.66
        y: -parent.height * 0.28
        rotation: 17
        color: "#160E3156"
        border.color: "#182D5B94"
        border.width: 1
    }

    Rectangle {
        width: parent.width * 0.32
        height: parent.height * 1.25
        x: parent.width * 0.09
        y: parent.height * 0.27
        rotation: -24
        color: "#0D12335A"
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "#14020A14" }
            GradientStop { position: 0.48; color: "#00000000" }
            GradientStop { position: 1.0; color: "#18030A13" }
        }
    }
}
