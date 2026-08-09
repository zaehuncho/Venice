import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "components"
import "pages"

ApplicationWindow {
    id: window

    // Venice uses one stable customer-facing cobalt accent. The legacy persisted
    // accent remains available to internal/admin surfaces.
    Component.onCompleted: Theme.accent = Theme.brandAccent
    // Sized so the full-bleed Remote Play capture card still fits the stream's
    // native 1280x720 — the GDI-capture floor; below it the shot meter gets too
    // small to detect reliably. (The decoded-frame pipe, the production default,
    // is resolution-independent.) While streaming the page hides its banner +
    // setup column so the embed reaches >=1280x720 (see RemotePlayPage.qml).
    readonly property int designWidth: 1400
    readonly property int designHeight: 820

    // Screen-fit scale. The layout is designed at a fixed 1400x820 LOGICAL px,
    // but common customer displays have a smaller logical desktop (1920x1080 at
    // 150% scaling -> 1280x720; 1366x768 panels; 4K laptops at 300%). A fixed
    // min=max window there spills off-screen with no resize path — unusable.
    // When (and only when) the current screen is too small, shrink the window
    // and uniformly scale the design-space shell into it. On any screen that
    // fits, fitScale is exactly 1.0 and nothing changes. The 56/8 px allowances
    // cover the taskbar/work-area at any DPI; the 0.5 floor keeps the window
    // sane if Screen briefly reports 0 during monitor hot-plug.
    //
    // Interactions audited 2026-08-08:
    //   * Chiaki embed + FirstRunTour map geometry via mapToItem(), which
    //     resolves through item transforms — both stay aligned under the scale.
    //   * Controls-layer popups (combo dropdowns, tooltips) render in the window
    //     overlay, outside this transform, so they appear unscaled — cosmetic,
    //     and only in this small-screen rescue mode.
    //   * Legacy GDI-capture floor: at fitScale<1 on a 100%-DPI screen the
    //     embed's physical size can drop below 1280x720; on such screens the
    //     old fixed window did not fit at all, and the production decoded-frame
    //     pipe is resolution-independent either way.
    // Moving the window to a different-sized screen re-evaluates Screen.* and
    // resnaps the window (back) to the correct size for that display.
    readonly property real fitScale: Math.max(0.5, Math.min(1.0,
        (Screen.width - 8) / designWidth,
        (Screen.height - 56) / designHeight))

    width: Math.round(designWidth * fitScale)
    height: Math.round(designHeight * fitScale)
    minimumWidth: Math.round(designWidth * fitScale)
    maximumWidth: Math.round(designWidth * fitScale)
    minimumHeight: Math.round(designHeight * fitScale)
    maximumHeight: Math.round(designHeight * fitScale)
    visible: true
    // CUSTOMER-VISIBLE: this is the taskbar / Alt-Tab / Task Manager label, so it
    // carries the product name.
    //
    // It is ALSO the handle orion:// activation forwarding uses to find an already
    // running instance — main.cpp does FindWindowW(nullptr, L"Venice"). The two are
    // a matched pair and MUST be changed together; a mismatch silently breaks deep
    // links (forwarding just fails to find the window and a second instance starts).
    //
    // This was frozen at "Orion" while the rebrand was customer-chrome-only. It is
    // unfrozen now because the taskbar was still reading "Orion" and nothing has
    // ever been published, so there is no older build looking for the old title.
    // Safety does not rest on the title being secret: forwardDeepLinkToRunningInstance
    // verifies the window is owned by THIS executable (windowOwnedByExecutable) and
    // re-binds the HWND before sending the secret-bearing URI, so an unrelated window
    // that happens to be titled "Venice" is rejected on process identity.
    title: "Venice"
    // C++ also wires QWindow::closing and aboutToQuit; shutdown is one-shot so
    // all three paths can overlap safely.
    onClosing: orion.requestApplicationShutdown()
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontBody

    Rectangle {
        id: shell
        // Fixed design-space size, uniformly scaled into the (possibly screen-
        // fit shrunk) window. The scale factors are derived from the ACTUAL
        // window size rather than fitScale so the rounded window dimensions are
        // filled exactly. Identity (scale 1.0) whenever the screen fits 1400x820.
        width: window.designWidth
        height: window.designHeight
        transform: Scale {
            xScale: window.width / window.designWidth
            yScale: window.height / window.designHeight
        }
        radius: 16
        clip: true
        color: Theme.bgShell
        border.color: Theme.borderSoft
        border.width: 1

        VeniceBackdrop {
            anchors.fill: parent
            opacity: 0.82
        }

        Rectangle {
            id: titleBar
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 38
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0.0; color: Theme.bgSidebar }
                GradientStop { position: 1.0; color: Theme.bgShell }
            }

            // 1 px hairline at the bottom of the title bar so it visually
            // separates from the page content below.
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: Theme.hairline
            }

            // Customer-facing Venice brand mark on the far left.
            Row {
                id: brandRow
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                spacing: 9

                Image {
                    width: 20
                    height: 20
                    anchors.verticalCenter: parent.verticalCenter
                    source: orion ? orion.iconSource : ""
                    sourceSize.width: 64
                    sourceSize.height: 64
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                }
                Text {
                    text: Theme.productName
                    color: Theme.textPrimary
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            // Drag area between the brand row and the window buttons. Excludes
            // the buttons themselves so they remain clickable.
            MouseArea {
                anchors.left: brandRow.right
                anchors.right: titleButtons.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                onPressed: window.startSystemMove()
                cursorShape: Qt.SizeAllCursor
            }

            Row {
                id: titleButtons
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 96

                Button {
                    id: minBtn
                    width: 48
                    height: titleBar.height
                    onClicked: window.showMinimized()
                    contentItem: Text {
                        text: "\u2013"
                        color: minBtn.hovered ? Theme.textPrimary : Theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        font.pixelSize: 16
                        Behavior on color { ColorAnimation { duration: 110 } }
                    }
                    background: Rectangle {
                        color: minBtn.hovered ? Theme.bgCard : "transparent"
                        Behavior on color { ColorAnimation { duration: 110 } }
                    }
                }

                Button {
                    id: closeBtn
                    width: 48
                    height: titleBar.height
                    onClicked: window.close()
                    contentItem: Text {
                        text: "\u2715"
                        color: closeBtn.hovered ? "#FFFFFF" : Theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        Behavior on color { ColorAnimation { duration: 110 } }
                    }
                    background: Rectangle {
                        color: closeBtn.hovered ? Theme.danger : "transparent"
                        Behavior on color { ColorAnimation { duration: 110 } }
                    }
                }
            }
        }

        Loader {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: titleBar.bottom
            anchors.bottom: parent.bottom
            // The update gate mounts BEFORE AuthGate: an outdated client updates
            // (or is hard-blocked) before it can be used. Fail-soft: offline or a
            // slow check clears the gate and proceeds on the current version.
            // After auth, a first-run Stream Setup screen shows once (until
            // streamSetupComplete), then the app shell.
            // After auth: legal agreement + rules (once), then the first-run Stream Setup
            // screen (until streamSetupComplete), then the app shell.
            sourceComponent: orion.updateGatePhase !== "clear" ? updateGatePage
                             : !orion.authenticated ? authPage
                             : !orion.legalAccepted ? legalGatePage
                             : !orion.streamSetupComplete ? streamSetupGatePage
                             : shellPage
        }
    }

    Component {
        id: updateGatePage
        UpdateGatePage {}
    }

    Component {
        id: authPage
        AuthGate {}
    }

    Component {
        id: legalGatePage
        LegalGate {}
    }

    Component {
        id: streamSetupGatePage
        StreamSetupGate {}
    }

    Component {
        id: shellPage
        AppShell {}
    }
}
