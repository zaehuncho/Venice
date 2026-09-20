import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

Rectangle {
    id: root
    color: Theme.bgSidebar
    radius: Theme.radiusCard
    border.color: Theme.borderSoft
    border.width: 1

    // Three customer tabs; Debug is appended only in a debug build.
    //
    // [SIDEBAR BRAND BLOCK REMOVED 2026-09-14 owner] The 44px logo tile plus the
    // wordmark/tagline lockup are gone: the window title and the gates already
    // carry the brand, and inside the app that block was a fixed 54px of chrome
    // above the only controls that matter. The Theme tokens it read stay defined
    // — the gates still render them.
    //
    // [OVERVIEW TAB REMOVED 2026-09-14 owner] The "general" page duplicated what
    // Setup and Live already show; its Account card moved to the bottom of Setup
    // and GeneralPage.qml was deleted. Nothing may route to "general" any more.
    //
    // [PROFILE TAB REMOVED 2026-09-15 owner: "remove the profile tab, license info
    // and days left should be displayed on the bottom left corner or at the top of
    // the page somewhere"] A whole tab for one number was a tab you visited once.
    // The licence state and the days left now live permanently in the footer strip
    // below, and everything else the page carried (masked key, Discord ID, PC
    // resets, activated date, machine, version) is one click away in its flyout.
    // pages/ProfilePage.qml is deleted; nothing may route to "profile" any more.
    //
    // [ORION_UI_BUBBLES 2026-09-15 owner: "remove the session start bubble and the
    // quick start bubble, quick start should just be shown when the customer first
    // launches the UI"] Two footer bubbles are gone from this rail:
    //   * "Quick Start" — the button that re-opened the guided tour. The tour is
    //     first-launch onboarding, not a permanent control, so it is now reached
    //     ONLY by the auto-open in AppShell.qml (gated on orion.preflightComplete).
    //     The nav model can no longer carry an action:"tour" entry either — the
    //     delegate is a pure page router now. TourRegistry anchors stay registered
    //     on every nav row, because the first-launch tour still spotlights them.
    //   * "Session: Offline/Live" — the StatusPill that sat at the very bottom.
    //     The Live page already carries the session state three ways (the Connect /
    //     Connected CTA, the blinking LIVE badge and the FPS pill on the capture),
    //     so the rail's copy only restated it, permanently, on every page.
    // The third footer item, the licence strip, stays and was redesigned below.
    readonly property var pages: {
        var list = [
            { key: "remotePlay", label: "Live", icon: "play" },
            { key: "dashboard", label: "Setup", icon: "grid" },
            { key: "patchNotes", label: "Updates", icon: "notes" }
        ]
        if (orion.debugUiEnabled)
            list.push({ key: "debug", label: "Debug", icon: "gear" })
        return list
    }

    // ── Licence strip truth ──────────────────────────────────────────────────
    // Everything here is server truth republished by OrionAppController. The
    // CURRENT live Lambda sends no `profile` block, so orion.profileKnown is the
    // single flag that says whether the server has actually spoken: when it has
    // not, the strip reads an honest "Not reported" rather than a fabricated 0
    // (a customer reads "0" as "expired today").
    readonly property bool licenseLifetime:
        orion.profileLifetime === true
        || Number(orion.profileDaysLeft) === -2
        || String(orion.profilePlan || "").toLowerCase() === "lifetime"
        || String(orion.licenseState || "").toLowerCase() === "lifetime"
    readonly property int licenseDaysLeft: Number(orion.profileDaysLeft)
    // The activation response puts the PLAN in licenseState when the key has one
    // ("1 Month"); the four generic words below are states, never plans.
    readonly property string licensePlanLabel: {
        var plan = String(orion.profilePlan || "").trim()
        if (plan.length === 0) {
            var state = String(orion.licenseState || "").trim()
            if (state.length === 0 || state === "Verified" || state === "Locked"
                || state === "Checking" || state === "Local Dev")
                return ""
            plan = state
        }
        return plan.charAt(0).toUpperCase() + plan.slice(1)
    }
    readonly property string licenseStateText: {
        var state = String(orion.licenseState || "").trim()
        var plan = root.licensePlanLabel
        if (root.licenseLifetime)
            return "Lifetime"
        if (state.length === 0 || state === "Locked")
            return "Not activated"
        if (state === "Checking")
            return "Checking"
        if (plan.toLowerCase() === "trial")
            return "Trial"
        if (state === "Local Dev")
            return plan.length > 0 ? "Local Dev · " + plan : "Local Dev"
        return plan.length > 0 ? "Active · " + plan : "Active"
    }
    // licenseDaysLeft rounds UP and clamps at 0, so 0 means the expiry has already
    // passed and 1 means "less than a day left" — hence Expired / Expires today.
    readonly property string licenseDaysText: {
        if (root.licenseLifetime)
            return "Lifetime"
        var days = root.licenseDaysLeft
        if (days === 0)
            return "Expired"
        if (days === 1)
            return "Expires today"
        if (days > 1)
            return days + " days left"
        // days === -1: unknown. Fall back to the controller's own phrasing, which
        // is "-" when it has no expiry loaded either.
        var fallback = String(orion.timeLeft || "").trim()
        if (fallback.length === 0 || fallback === "-" || fallback === "—")
            return "Not reported"
        if (fallback === "Lifetime")
            return "Lifetime"
        if (fallback === "Dev")
            return "Developer build"
        return fallback + " left"
    }
    // Subtle warning tone in the last three days — and only when the server has
    // actually reported an expiry, so an unknown licence never looks alarming.
    readonly property bool licenseExpiringSoon:
        !root.licenseLifetime && root.licenseDaysLeft >= 0 && root.licenseDaysLeft <= 3
    readonly property int licenseFreeRemaining: Number(orion.profileHwidResetsFreeRemaining)
    readonly property int licenseFreeTotal: Number(orion.profileHwidResetsFreeTotal)
    readonly property int licensePaidCredits: Number(orion.profileHwidPaidCredits)
    readonly property string licenseResetsText: {
        if (orion.profileKnown !== true)
            return "Not reported"
        var text = root.licenseFreeRemaining + " of " + root.licenseFreeTotal + " free remaining"
        if (root.licensePaidCredits > 0)
            text += " · " + (root.licensePaidCredits === 1
                                  ? "1 credit" : root.licensePaidCredits + " credits")
        return text
    }
    readonly property string licenseActivatedText: {
        var epoch = Number(orion.profileActivatedEpochS)
        if (!(epoch > 0))
            return "Not recorded"
        return new Date(epoch * 1000).toLocaleDateString(Qt.locale())
    }

    // [ORION_UI_BUBBLES 2026-09-15] Flyout header vocabulary. The chip carries the
    // STATE as one short word; the plan ("1 Month") rides beside it and the days
    // left is the headline, so the first line of the card reads the way the owner
    // asked for it: "Active · 23 days left" split across a chip and a headline
    // instead of one run-on string that elides at sidebar width.
    readonly property string licenseChipText: {
        var text = root.licenseStateText
        var sep = text.indexOf(" · ")
        return sep > 0 ? text.substring(0, sep) : text
    }
    readonly property string licenseTone: {
        if (root.licenseChipText === "Not activated" || root.licenseDaysText === "Expired")
            return "danger"
        if (root.licenseExpiringSoon)
            return "warning"
        if (root.licenseChipText === "Checking")
            return "neutral"
        return "success"
    }
    readonly property color licenseToneColor: root.licenseTone === "danger" ? Theme.danger
                                              : root.licenseTone === "warning" ? Theme.warning
                                              : root.licenseTone === "neutral" ? Theme.textMuted
                                              : Theme.success
    readonly property color licenseToneFill: root.licenseTone === "danger" ? Theme.dangerDim
                                             : root.licenseTone === "warning" ? Theme.warningDim
                                             : root.licenseTone === "neutral" ? Theme.bgField
                                             : Theme.successDim

    // Pill-shaped copy affordance, as the Profile page's Account card had it.
    // Declared BEFORE DetailRow, which instantiates it: an inline component may
    // only reference inline components declared above it in the same file.
    component CopyChip: Button {
        id: chip
        property bool copied: false
        implicitHeight: 22
        implicitWidth: 52
        hoverEnabled: true
        // A QML handler REPLACES the base's, so any override of onClicked would
        // drop the reset; the timer is armed from the property change instead.
        onCopiedChanged: if (chip.copied) copyReset.restart()
        Timer {
            id: copyReset
            interval: 1600
            onTriggered: chip.copied = false
        }
        contentItem: Text {
            text: chip.copied ? "Copied" : "Copy"
            color: chip.copied ? Theme.success : Theme.textSecondary
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontMicro
            font.weight: Font.DemiBold
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }
        background: Rectangle {
            radius: Theme.radiusChip
            color: chip.hovered ? Theme.bgCardHover : Theme.bgField
            border.color: chip.copied ? Theme.successBorder
                          : chip.hovered ? Theme.borderStrong : Theme.borderSoft
            border.width: 1
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
            Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
        }
    }

    // [ORION_UI_BUBBLES 2026-09-15] One label/value row of the flyout's two-column
    // grid, with an optional inline Copy affordance. Fixed label column + a
    // right-aligned value keeps every row on the same two rails at any string
    // length; the value elides rather than pushing the card wider than the rail.
    component DetailRow: RowLayout {
        id: detailRow
        property string label: ""
        property string value: ""
        property string valueObjectName: ""
        property string copyObjectName: ""
        property color valueColor: Theme.textPrimary
        property bool mono: false
        property bool copyable: false
        // Elide the HEAD for identifiers (a masked key/machine id is recognised by
        // its tail) and the TAIL for prose (names, dates, build strings).
        property int valueElide: Text.ElideRight
        signal copyRequested()
        Layout.fillWidth: true
        spacing: 8

        Text {
            text: detailRow.label
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontCaption
            Layout.preferredWidth: 62
            Layout.alignment: Qt.AlignVCenter
            elide: Text.ElideRight
        }
        Text {
            objectName: detailRow.valueObjectName
            text: detailRow.value
            color: detailRow.valueColor
            font.family: detailRow.mono ? Theme.fontMono : Theme.fontUi
            font.pixelSize: Theme.fontCaption
            font.weight: Font.DemiBold
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            horizontalAlignment: Text.AlignRight
            elide: detailRow.valueElide
        }
        CopyChip {
            objectName: detailRow.copyObjectName
            visible: detailRow.copyable
            Layout.alignment: Qt.AlignVCenter
            onClicked: {
                detailRow.copyRequested()
                copied = true
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 8

        // Clean top: a modest breathing gap where the brand lockup used to sit,
        // so the first nav row does not collide with the window chrome.
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 10
        }

        Repeater {
            model: root.pages
            delegate: Button {
                id: nav
                required property var modelData
                readonly property bool active: orion.currentPage === modelData.key
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                text: modelData.label
                hoverEnabled: true
                // [ORION_UI_BUBBLES 2026-09-15] Pure page router: the action:"tour"
                // branch that raised the guided tour from this rail is gone with the
                // Quick Start footer button.
                onClicked: orion.currentPage = modelData.key

                // Anchor for the first-run tour spotlight (e.g. "nav:remotePlay").
                Component.onCompleted: TourRegistry.register("nav:" + modelData.key, nav)
                Component.onDestruction: TourRegistry.unregister("nav:" + modelData.key, nav)

                contentItem: RowLayout {
                    spacing: 10

                    // Active-page accent indicator.
                    Rectangle {
                        Layout.preferredWidth: 3
                        Layout.preferredHeight: 18
                        Layout.leftMargin: 2
                        radius: 1.5
                        color: nav.active ? Theme.accent : "transparent"
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }
                    NavIcon {
                        icon: nav.modelData.icon
                        color: nav.active ? Theme.accentBorder : Theme.textFaint
                        Layout.preferredWidth: 18
                        Layout.preferredHeight: 18
                        Layout.alignment: Qt.AlignVCenter
                    }
                    Text {
                        text: nav.text
                        color: nav.active ? Theme.textPrimary : nav.hovered ? Theme.textSecondary : Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontBody
                        font.weight: nav.active ? Font.DemiBold : Font.Normal
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: nav.active ? Theme.accentSoft : nav.hovered ? Theme.bgCard : "transparent"
                    border.color: nav.active ? Theme.borderStrong : "transparent"
                    border.width: 1
                    Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
                }
            }
        }

        Item { Layout.fillHeight: true }

        // ── Licence strip ────────────────────────────────────────────────────
        // [PROFILE TAB -> FOOTER STRIP 2026-09-15 owner] Bottom-left, same width as
        // the nav rows: the two lines a customer actually wants at a glance (what
        // they are on, how long it lasts). Clicking it opens the flyout with the
        // support details the Profile page carried.
        // [ORION_UI_BUBBLES 2026-09-15 owner] It is the ONLY thing in the footer
        // now — the Quick Start button above it and the Session pill below it are
        // both gone — so it carries the whole bottom edge: 40px, a chevron that
        // says it opens, and a hover state on the whole strip.
        Button {
            id: licenseStrip
            objectName: "licenseStrip"
            Layout.fillWidth: true
            Layout.preferredHeight: 40
            hoverEnabled: true
            leftPadding: 10
            rightPadding: 8
            topPadding: 3
            bottomPadding: 3
            onClicked: licenseFlyout.visible ? licenseFlyout.close() : licenseFlyout.open()

            // Pointer affordance without stealing clicks from the Button itself.
            HoverHandler {
                cursorShape: Qt.PointingHandCursor
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            }

            contentItem: RowLayout {
                spacing: 6

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 1

                    Text {
                        objectName: "licenseStripState"
                        Layout.fillWidth: true
                        text: root.licenseStateText
                        color: licenseStrip.hovered || licenseFlyout.visible
                               ? Theme.textPrimary : Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmall
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }
                    Text {
                        objectName: "licenseStripDays"
                        Layout.fillWidth: true
                        text: root.licenseDaysText
                        color: root.licenseExpiringSoon ? Theme.warning : Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontCaption
                        elide: Text.ElideRight
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }
                }

                // Open/close affordance, so the strip never reads as a dead label.
                // The glyph flips rather than rotating: a rotated Text overdraws
                // its layout cell, and the two chevrons are the same metrics.
                Text {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: 10
                    horizontalAlignment: Text.AlignHCenter
                    text: licenseFlyout.visible ? "‹" : "›"
                    color: licenseStrip.hovered || licenseFlyout.visible
                           ? Theme.accentBorder : Theme.textFaint
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontTitle
                    font.weight: Font.DemiBold
                    Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                }
            }

            background: Rectangle {
                radius: Theme.radiusControl
                color: root.licenseExpiringSoon ? Theme.warningDim
                       : licenseStrip.hovered || licenseFlyout.visible ? Theme.bgCard : "transparent"
                border.color: root.licenseExpiringSoon ? Theme.warningBorder
                              : licenseStrip.hovered || licenseFlyout.visible
                                ? Theme.borderStrong : Theme.borderSoft
                border.width: 1
                Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
            }

            // The details that used to fill pages/ProfilePage.qml. Anchored to
            // the strip and opened to the RIGHT of the rail, bottom-aligned, so
            // it never covers the nav and never runs off the bottom edge.
            //
            // [ORION_UI_BUBBLES 2026-09-15 owner: "rework the profile bubble to make
            // it look neater and better"] Rebuilt as a proper card in the house
            // style (Card.qml): drop halo + top light catch under a modal surface,
            // a header block whose chip carries the licence STATE and whose
            // headline is the days left, one hairline, then a two-column key/value
            // grid on a fixed 8px rhythm. Every colour is a Theme token, every
            // value elides instead of widening the card, Esc closes it (focus:true
            // — without active focus CloseOnEscape never fires) and a press outside
            // the strip closes it.
            Popup {
                id: licenseFlyout
                objectName: "licenseFlyout"
                x: licenseStrip.width + 10
                y: licenseStrip.height - height
                width: 296
                padding: 14
                modal: false
                focus: true
                closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent

                background: Rectangle {
                    radius: Theme.radiusCard
                    color: Theme.modalSurface
                    border.color: Theme.modalBorder
                    border.width: 1

                    // Soft elevation: one darker halo below the card, the same
                    // no-effects-module idiom Card.qml uses. A negative z paints
                    // it behind its own parent.
                    Rectangle {
                        z: -1
                        anchors.fill: parent
                        anchors.topMargin: 2
                        anchors.leftMargin: 1
                        anchors.rightMargin: -1
                        anchors.bottomMargin: -2
                        radius: parent.radius + 1
                        color: Theme.shadowHalo
                        opacity: 0.55
                    }
                    // 1px light catch along the top edge.
                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.leftMargin: parent.radius
                        anchors.rightMargin: parent.radius
                        anchors.topMargin: 1
                        height: 1
                        color: Theme.hairlineLight
                    }
                }
                enter: Transition {
                    NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: Theme.motionFast; easing.type: Easing.OutCubic }
                }
                exit: Transition {
                    NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 90; easing.type: Easing.InCubic }
                }

                contentItem: ColumnLayout {
                    spacing: 8

                    // ---- header: state chip + plan, days left as the headline ----
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Rectangle {
                            Layout.preferredHeight: 20
                            Layout.preferredWidth: Math.min(chipRow.implicitWidth + 16,
                                                            licenseFlyout.availableWidth - 70)
                            radius: Theme.radiusChip
                            color: root.licenseToneFill
                            border.color: root.licenseToneColor
                            border.width: 1
                            Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                            Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }

                            Row {
                                id: chipRow
                                anchors.centerIn: parent
                                spacing: 5
                                Rectangle {
                                    width: 6
                                    height: 6
                                    radius: 3
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: root.licenseToneColor
                                    Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                                }
                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: root.licenseChipText.toUpperCase()
                                    color: root.licenseToneColor
                                    font.family: Theme.fontUi
                                    font.pixelSize: Theme.fontMicro
                                    font.weight: Font.Bold
                                    font.letterSpacing: 0.8
                                }
                            }
                        }

                        Item { Layout.fillWidth: true }

                        Text {
                            visible: root.licensePlanLabel.length > 0
                                     && root.licensePlanLabel !== root.licenseChipText
                            text: root.licensePlanLabel
                            color: Theme.textMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontCaption
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                            Layout.maximumWidth: 96
                            Layout.alignment: Qt.AlignVCenter
                        }
                    }

                    Text {
                        text: root.licenseDaysText
                        color: root.licenseExpiringSoon ? Theme.warning : Theme.textPrimary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontTitle
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }

                    // The controller's own phrasing of the expiry (an exact date
                    // when it has one) sits under the headline rather than
                    // competing with it.
                    Text {
                        readonly property string detail: String(orion.timeLeftDetail || "").trim()
                        visible: detail.length > 0 && detail !== root.licenseDaysText
                        text: detail
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontCaption
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.topMargin: 2
                        Layout.preferredHeight: 1
                        color: Theme.hairline
                    }

                    // ---- two-column key/value grid ----
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6

                        DetailRow {
                            visible: String(orion.licenseKeyMasked || "").length > 0
                            label: "Key"
                            value: String(orion.licenseKeyMasked || "")
                            mono: true
                            valueElide: Text.ElideLeft
                            copyable: true
                            copyObjectName: "licenseFlyoutCopyKey"
                            onCopyRequested: orion.copyLicenseKey()
                        }
                        DetailRow {
                            objectName: "licenseFlyoutDiscordRow"
                            visible: String(orion.profileDiscordId || "").length > 0
                            label: "Discord"
                            valueObjectName: "licenseFlyoutDiscordId"
                            value: String(orion.profileDiscordId || "")
                            mono: true
                            valueElide: Text.ElideLeft
                            copyable: true
                            copyObjectName: "licenseFlyoutCopyDiscordId"
                            onCopyRequested: orion.copyProfileDiscordId()
                        }
                        DetailRow {
                            visible: String(orion.profileDiscordName || "").length > 0
                            label: "Name"
                            value: String(orion.profileDiscordName || "")
                        }
                        DetailRow {
                            objectName: "licenseFlyoutResetsRow"
                            label: "PC resets"
                            value: root.licenseResetsText
                            valueColor: orion.profileKnown !== true ? Theme.textFaint
                                        : root.licenseFreeRemaining > 0 || root.licensePaidCredits > 0
                                          ? Theme.textPrimary : Theme.warning
                        }
                        DetailRow {
                            label: "Activated"
                            value: root.licenseActivatedText
                            valueColor: Number(orion.profileActivatedEpochS) > 0
                                        ? Theme.textPrimary : Theme.textFaint
                        }
                        DetailRow {
                            label: "Machine"
                            mono: true
                            valueElide: Text.ElideLeft
                            value: String(orion.machineIdMasked || "").length > 0
                                   ? String(orion.machineIdMasked) : "Not bound"
                            valueColor: String(orion.machineIdMasked || "").length > 0
                                        ? Theme.textPrimary : Theme.textFaint
                        }
                        DetailRow {
                            // Not 'Version': the nav rail's old Version STATUS ROW is
                            // gone for good, and the contract test pins its absence.
                            label: "Build"
                            value: Theme.productName + " " + orion.displayVersion
                                   + " · " + String(orion.updateChannel || "stable")
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.topMargin: 2
                        Layout.preferredHeight: 1
                        color: Theme.hairline
                    }

                    // The owner's HWID rule, stated once, in plain language. The
                    // reset itself is run from Discord — this only reports the
                    // allowance, exactly as the Profile page did.
                    Text {
                        Layout.fillWidth: true
                        // QML layouts do not do height-for-width, so a wrapping
                        // Text reserves ONE line and then overflows. Binding the
                        // preferred height to the post-wrap contentHeight settles
                        // it in a second pass (the popup width is fixed, so this
                        // cannot loop).
                        Layout.preferredHeight: contentHeight
                        text: "Each licence key includes "
                              + (orion.profileKnown === true ? root.licenseFreeTotal : 3)
                              + " free PC resets. After that, buy a reset credit or trade "
                              + "days off your subscription. Run the reset from the Venice Discord."
                        color: Theme.textFaint
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontMicro
                        lineHeight: 1.25
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }
}
