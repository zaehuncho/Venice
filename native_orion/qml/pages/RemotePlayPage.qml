import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

Item {
    id: root
    readonly property color panel: Theme.bgSidebar
    readonly property color card: Theme.bgCard
    readonly property color field: Theme.bgField
    readonly property color border: Theme.borderSoft
    readonly property color text: Theme.textPrimary
    readonly property color muted: Theme.textMuted
    readonly property color accent: Theme.accent
    readonly property color accentSoft: Theme.accentBorder
    readonly property bool streamLive: orion.remoteRunning || orion.chiakiEmbedStatus === "Embedded"
    // Live capture-card preview (HDMI feed shown BEFORE Connect). Kept separate from streamLive so
    // the panel shows the card while the Start button stays enabled and the session is Disconnected.
    readonly property bool previewActive: orion.capturePreviewActive
    // Transition states drive the connect/disconnect buttons so a click gives instant
    // feedback and can't be fired twice (which would spawn a duplicate stream).
    readonly property bool connecting: orion.remoteState === "Connecting"
    readonly property bool disconnecting: orion.remoteState === "Disconnecting"
    // A session that ended in a reported failure. The detection sidecar may still be alive
    // (a failed stream promotion never kills it), so the teardown control has to stay reachable.
    readonly property bool errored: orion.remoteState === "Error"
    // Shoot-to-train: no manual boxing. Calibration state (active / shots / target / status)
    // lives in the controller (orion.meterCalibration*) and drives the overlay below.

    // ===== FIX 2: "No Meter" (skele) availability gate ==========================================
    // No-Meter timing needs `pose_timing` -> ultralytics/torch plus three models/*.pt weight files.
    // The shipped packager excludes torch and its models whitelist is intentionally EMPTY, and the
    // orchestrator swallows the resulting init failure with a bare `except` — so turning the mode on
    // produced a bot that silently did nothing at all, with no way for the customer to tell. The
    // sidecar now probes those dependencies at startup ({"event":"capabilities"}) and
    // RemotePlaySession stores the verdict.
    //
    // INTEGRATOR NOTE: this binds to `orion.noMeterAvailable` / `orion.noMeterUnavailableReason`,
    // which must be exposed on OrionAppController and fed from
    // RemotePlaySession::sidecarCapabilitiesChanged(bool, QString) (see RemotePlaySession.h).
    // Until those properties exist the expressions evaluate to `undefined`, which is deliberately
    // coerced to "assume available" — so on a build that has not wired them this page behaves
    // EXACTLY as before: nothing is hidden and no warning appears.
    readonly property bool noMeterSupported: orion.noMeterAvailable === undefined
                                             ? true : (orion.noMeterAvailable === true)
    readonly property string noMeterReason: orion.noMeterUnavailableReason === undefined
                                            ? "" : String(orion.noMeterUnavailableReason)
    // The dangerous state: the mode is switched ON but cannot possibly run.
    readonly property bool noMeterBroken: orion.noMeterEnabled && !root.noMeterSupported

    // [ORION_NO_METER_SHELVED 2026-09-15] `noMeterMode` is gone with the switch that set it.
    // orion.inputTimedEnabled is forced false on every load and save, so a binding on it would
    // read as a live choice the customer does not have. The three orion.noMeterEnabled bindings
    // that remain below (the skeleton overlay and the two unavailable-sidecar warnings) are
    // harmless: they are false on every shipped install for exactly the same reason.

    // ===== [ORION_INPUT_DEAD_UX 2026-08-30] dead-input overlay bindings =========================
    // In capture-card mode the HDMI video keeps playing whatever the Chiaki INPUT session does,
    // so a failed promotion / dropped session leaves the game looking alive while every button is
    // dead. The controller computes the verdict from the SAME predicate that writes the per-press
    // "PRESS UNDELIVERABLE" log line (one source of truth); this page only renders it. Undefined
    // coercion mirrors the noMeterSupported pattern so an unwired build renders nothing new.
    readonly property bool inputDead: orion.inputDeadOverlayActive === true
    // [CL2-P8-002 2026-09-23] Non-empty while the licence lease blocks shots.
    readonly property bool leaseBlocked: String(orion.leaseNotice || "").length > 0
    readonly property bool inputDeadCritical: orion.inputDeadCritical === true
    readonly property string inputDeadHeadline: orion.inputDeadHeadline === undefined
                                                ? "" : String(orion.inputDeadHeadline)
    readonly property string inputDeadDetail: orion.inputDeadDetail === undefined
                                              ? "" : String(orion.inputDeadDetail)

    // Debounced status text: the orchestrator's remoteStatus can churn rapidly
    // (connect/capture-health transitions). Only surface a value once it's held
    // ~350ms so the status line doesn't flicker.
    // Initialized imperatively in Component.onCompleted so these are snapshots,
    // not live bindings that bypass the debounce on every broad NOTIFY.
    property string stableStatus: ""
    property string pendingStatus: ""
    // Expanded by default (2026-08-08): the owner skims this feed during a
    // batch, and a collapsed 42px strip hid it. The +/- toggle still allows
    // collapsing when the live feed should dominate.
    property bool activityExpanded: true

    function considerRemoteStatus(nextStatus) {
        var candidate = String(nextStatus)
        // statusChanged is a broad NOTIFY signal. Frame/controller telemetry may
        // legitimately trigger it while remoteStatus itself is unchanged; those
        // notifications must not perpetually restart the debounce window.
        if (candidate === root.pendingStatus)
            return
        root.pendingStatus = candidate
        if (candidate === root.stableStatus) {
            statusDebounce.stop()
            return
        }
        statusDebounce.restart()
    }
    Connections {
        target: orion
        function onStatusChanged() { root.considerRemoteStatus(orion.remoteStatus) }
        function onFrameChanged() { root.queuePreviewSerial(orion.frameSerial) }
        // Keep the activity pane incremental. Binding a TextArea to the entire
        // diagnostic ring rebuilt a ~32 KB QTextDocument whenever the
        // 5-second preview telemetry arrived. That work runs on the same GUI
        // thread as the SHM notification/presentation timers and showed up as a
        // 32-70 ms video gap even though the capture producer stayed at 60 FPS.
        // [ORION_PREVIEW_LOG_THROTTLE 2026-09-21] While the Qt Quick render clock
        // drives the live preview, the 200 ms controller cadence re-ran the whole
        // 1000-line filter + O(n^2) overlap scan on the GUI thread and starved
        // FrameAnimation (present_gap_max_ms 261 ms median, worsening over a
        // session). Coalesce to captureLogThrottle while live; sync immediately
        // when the preview is not rendering.
        function onLogsChanged() {
            if (previewRenderClock.running) {
                root.captureLogDirty = true
                if (!captureLogThrottle.running)
                    captureLogThrottle.start()
                return
            }
            root.syncCaptureLogModel()
        }
    }
    Timer {
        id: statusDebounce
        interval: 350
        onTriggered: root.stableStatus = root.pendingStatus
    }
    // Live-preview activity sync: at most one model sync per interval while the
    // render clock is running. Shot-abort / watchdog lines still land within
    // one interval; the video no longer competes with a per-200 ms rebuild.
    property bool captureLogDirty: false
    // The exact ring text last synced into captureLogModel; syncCaptureLogModel()
    // returns early when orion.activityText is identical to it.
    property string captureLogSyncedText: ""
    Timer {
        id: captureLogThrottle
        interval: 1500
        repeat: false
        onTriggered: {
            if (root.captureLogDirty) {
                root.captureLogDirty = false
                root.syncCaptureLogModel()
            }
        }
    }

    // Filter the global activity log down to live-capture / Remote Play events for the
    // in-card log (drops license / security / update / network-bridge / court-IP noise).
    // 2026-08-08: raised 36 -> 1000 to match the controller-side Activity ring
    // (kActivityRingMaxLines) so the owner can scroll a whole batch in-app.
    // Cheap because syncCaptureLogModel() is incremental (one remove + one
    // append in steady state) and ListView only lays out visible delegates.
    readonly property int captureLogLimit: 1000

    function filterLogLines(t) {
        if (!t || t.length === 0)
            return []
        var noise = /license|security:|vm[_ ]sandbox|machine_id|update (gate|check)|network bridge|packet bridge|nexusvisionsvc|auto-authenticating|court ip|activation request/i
        // These lines remain complete in logs/orion_native.log and in the
        // diagnostics bundle. They are machine telemetry, not user activity;
        // continuously laying them out in the live page competes with video.
        var periodic = /qml_preview_pipeline|preview_pipeline:|preview_stats:|capture health:|shm preview frame read:|detection presence:/i
        // Raw sidecar/stdout and per-frame rejection diagnostics are valuable in
        // logs/orion_native.log, but they are not user events. Besides looking
        // unfinished, publishing them here can wake and relayout the live page
        // while a shot is in flight. Human-facing failures still arrive through
        // Remote Play state, watchdog, safe-mode, and shot-abort messages.
        var machine = /sidecar:|detector frame rejected:|release (attribution|timing|detsummary|freshness|vision|tempo|window diagnostic)|shadow timing:|scheduled fire:/i
        var lines = t.split("\n")
        var keep = []
        for (var i = 0; i < lines.length; ++i) {
            if (lines[i].length === 0 || noise.test(lines[i])
                    || periodic.test(lines[i]) || machine.test(lines[i]))
                continue
            keep.push(lines[i])
        }
        if (keep.length > root.captureLogLimit)
            keep = keep.slice(keep.length - root.captureLogLimit)
        return keep
    }

    // Preserve the longest suffix/prefix overlap so the steady-state update is
    // one remove + one append, not destruction/recreation of the whole log.
    // ListView then lays out only its visible delegates.
    function syncCaptureLogModel() {
        // [ORION_ACTIVITY_FEED 2026-09-14 owner] Source the CUSTOMER ring
        // (orion.activityText), not the raw one: the native rule
        // (ui_notifications::shouldEnterActivityRing) has already removed the
        // engineering telemetry that used to age every real event out of a
        // 1000-line ring within minutes. The QML filter below stays as the
        // Live-page-specific pass (licence/security/court-IP belong on Profile
        // and Debug, not over the video).
        // Steady-state exit on the SOURCE STRING, not on count+last-line: at the
        // 1000-line cap a duplicate trailing line can rotate older entries while
        // both count and last line stay identical (Astra, release review). An
        // exact compare of the ring text is ~32 KB of memcmp per tick -- nothing
        // next to a delegate relayout -- and it can never miss a change.
        var text = orion.activityText
        if (text === root.captureLogSyncedText)
            return
        root.captureLogSyncedText = text
        // Filter the CUSTOMER ring (orion.activityText) -- tests/test_venice_ui_contract.py
        // pins this exact expression as the activity-feed source contract.
        var next = root.filterLogLines(orion.activityText)
        var current = []
        for (var i = 0; i < captureLogModel.count; ++i)
            current.push(captureLogModel.get(i).line)

        var overlap = Math.min(current.length, next.length)
        for (; overlap > 0; --overlap) {
            var matches = true
            for (var j = 0; j < overlap; ++j) {
                if (current[current.length - overlap + j] !== next[j]) {
                    matches = false
                    break
                }
            }
            if (matches)
                break
        }

        var removeCount = current.length - overlap
        if (removeCount > 0)
            captureLogModel.remove(0, removeCount)
        for (var k = overlap; k < next.length; ++k)
            captureLogModel.append({ "line": next[k] })
    }

    ListModel {
        id: captureLogModel
        objectName: "captureLogModel"
    }
    readonly property int captureLogLines: captureLogModel.count

    function activityTone(line) {
        if (/failed|error|abort|safe mode|unavailable|disconnected/i.test(line))
            return Theme.danger
        if (/warning|reconnect|recover|calibrat/i.test(line))
            return Theme.warning
        if (/release issued|enabled|connected|ready|complete/i.test(line))
            return Theme.success
        return Theme.accentBorder
    }

    // Double-buffer the async image provider. Binding one Image directly to a
    // 60 Hz serial cancels an in-flight texture whenever the next frame arrives;
    // the provider can fetch at 60 FPS while only 35-50 textures ever reach
    // Ready. Here the visible slot is never mutated. The other slot loads one
    // immutable serial, swaps only at Ready, then immediately starts the newest
    // pending serial. Detector pixels remain on the unbuffered sidecar path.
    property int previewFrontSlot: -1
    property int previewFrontSerial: -1
    property int previewLoadingSlot: -1
    property int previewPendingSerial: -1

    function queuePreviewSerial(serial) {
        if (serial < 0 || serial <= root.previewFrontSerial)
            return
        root.previewPendingSerial = Math.max(root.previewPendingSerial, serial)
    }

    function pumpPreviewSerial() {
        if (root.previewLoadingSlot >= 0 || root.previewPendingSerial < 0)
            return
        var slot = root.previewFrontSlot === 0 ? 1 : 0
        var image = slot === 0 ? previewImageA : previewImageB
        var serial = root.previewPendingSerial
        root.previewPendingSerial = -1
        root.previewLoadingSlot = slot
        image.requestedSerial = serial
        image.source = "image://remote/live/" + serial
    }

    function previewLoaderStatusChanged(image, slot) {
        if (slot !== root.previewLoadingSlot || image.requestedSerial < 0)
            return
        if (image.status === Image.Ready) {
            var serial = image.requestedSerial
            if (serial > root.previewFrontSerial) {
                root.previewFrontSlot = slot
                root.previewFrontSerial = serial
                orion.acknowledgeRemoteFramePresented(serial)
            }
            root.previewLoadingSlot = -1
        } else if (image.status === Image.Error) {
            root.previewLoadingSlot = -1
            image.source = ""
        }
    }

    // Drive the display-only jitter buffer from Qt Quick's actual render clock.
    // One tick can dequeue at most one paired image/frame id and start at most
    // one immutable provider request. Hidden pages explicitly hand ownership
    // back to the native precise-timer fallback. Detector/timing frames bypass
    // this presentation path entirely.
    FrameAnimation {
        id: previewRenderClock
        running: root.visible && orion.qmlRenderMode
                 && (root.streamLive || root.previewActive)
        onRunningChanged: {
            orion.setPreviewRenderClockActive(running)
            // Leaving live preview: flush any activity sync coalesced while rendering.
            if (!running && root.captureLogDirty) {
                captureLogThrottle.stop()
                root.captureLogDirty = false
                root.syncCaptureLogModel()
            }
        }
        onTriggered: {
            orion.advanceRemotePreviewPresentation()
            root.pumpPreviewSerial()
        }
        Component.onCompleted: orion.setPreviewRenderClockActive(running)
        Component.onDestruction: orion.setPreviewRenderClockActive(false)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 14

            Card {
                title: "Live Capture"
                // FIX 2: a broken No-Meter mode outranks the connection status here — the stream can
                // be perfectly healthy while the bot cannot fire a single shot.
                subtitle: root.noMeterBroken
                          ? "No-Meter mode unavailable - the bot will not fire"
                          : root.stableStatus
                Layout.fillWidth: true
                Layout.fillHeight: true

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10

                    Rectangle {
                        id: captureHost
                        objectName: "captureHost"
                        // A clean 16:9 framed "screen" pinned to the TOP of the card so there's no
                        // tall letterbox void above it. Fills the available width; height follows 16:9
                        // (capped so it can't overflow the card on very wide windows).
                        readonly property real screenAspect: 16.0 / 9.0
                        Layout.fillWidth: true
                        Layout.preferredHeight: width / screenAspect
                        Layout.maximumHeight: 620
                        Layout.alignment: Qt.AlignTop
                        radius: 12
                        color: "#000000"
                        border.color: root.border
                        border.width: 1
                        clip: true

                        // First-run tour spotlight anchor (live capture panel).
                        Component.onCompleted: TourRegistry.register("rp:capture", captureHost)
                        Component.onDestruction: TourRegistry.unregister("rp:capture", captureHost)

                        onWidthChanged: root.syncChiakiEmbed()
                        onHeightChanged: root.syncChiakiEmbed()
                        onXChanged: root.syncChiakiEmbed()
                        onYChanged: root.syncChiakiEmbed()

                        // QML render mode (default): these two slots are the live
                        // decoder-pipe/capture-card video. The front texture stays
                        // immutable while its sibling asynchronously loads the next
                        // frame, eliminating source-cancellation flicker and judder.
                        Image {
                            id: previewImageA
                            objectName: "previewImageA"
                            property int requestedSerial: -1
                            anchors.fill: parent
                            anchors.margins: 8
                            source: ""
                            z: root.previewFrontSlot === 0 ? 2 : 1
                            opacity: root.previewFrontSlot === 0 ? 1 : 0
                            onStatusChanged: root.previewLoaderStatusChanged(previewImageA, 0)
                            visible: orion.qmlRenderMode ? (root.streamLive || root.previewActive)
                                                         : (orion.chiakiEmbedStatus !== "Embedded")
                            fillMode: Image.PreserveAspectFit
                            smooth: true
                            cache: false
                            asynchronous: orion.previewAsync
                            retainWhileLoading: orion.previewAsync
                        }

                        Image {
                            id: previewImageB
                            objectName: "previewImageB"
                            property int requestedSerial: -1
                            anchors.fill: parent
                            anchors.margins: 8
                            source: ""
                            z: root.previewFrontSlot === 1 ? 2 : 1
                            opacity: root.previewFrontSlot === 1 ? 1 : 0
                            onStatusChanged: root.previewLoaderStatusChanged(previewImageB, 1)
                            visible: orion.qmlRenderMode ? (root.streamLive || root.previewActive)
                                                         : (orion.chiakiEmbedStatus !== "Embedded")
                            fillMode: Image.PreserveAspectFit
                            smooth: true
                            cache: false
                            asynchronous: orion.previewAsync
                            retainWhileLoading: orion.previewAsync
                        }

                        // Idle placeholder — clean text, no logo glyph. Hidden once the pre-Connect
                        // capture-card preview is feeding frames (previewActive) as well as when live.
                        ColumnLayout {
                            anchors.centerIn: parent
                            spacing: 8
                            visible: !root.streamLive && !root.previewActive

                            Text {
                                Layout.alignment: Qt.AlignHCenter
                                text: "No stream"
                                color: Theme.textSecondary
                                font.family: Theme.fontUi
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                            }
                            Text {
                                Layout.alignment: Qt.AlignHCenter
                                text: "Press Connect to start"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                            }
                        }

                        // [PREVIEW-ONLY BADGE REMOVED 2026-09-14 owner] The amber
                        // passive-preview chip over the pre-connect capture is gone (it
                        // read PREVIEW·ONLY, previewOnlyLabel). The Connect button beside
                        // it already says the session is not up, so the badge only ever
                        // restated its neighbour.

                        // LIVE badge — pinned to the capture's top-left while streaming.
                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.margins: 16
                            width: liveBadgeRow.implicitWidth + 18
                            height: 24
                            radius: 12
                            color: "#CC05080D"
                            border.color: Theme.success
                            border.width: 1
                            visible: root.streamLive
                            z: 10

                            Row {
                                id: liveBadgeRow
                                anchors.centerIn: parent
                                spacing: 6
                                Rectangle {
                                    width: 7; height: 7; radius: 3.5
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: Theme.success
                                    SequentialAnimation on opacity {
                                        running: root.streamLive; loops: Animation.Infinite
                                        NumberAnimation { to: 0.35; duration: 900; easing.type: Easing.InOutQuad }
                                        NumberAnimation { to: 1.0; duration: 900; easing.type: Easing.InOutQuad }
                                    }
                                }
                                Text {
                                    text: "LIVE"
                                    color: "#FFFFFF"
                                    font.family: Theme.fontUi
                                    font.pixelSize: 10
                                    font.weight: Font.Bold
                                    font.letterSpacing: 1.2
                                }
                            }
                        }

                        // [PRESSED OVERLAY 2026-08-08] Local acknowledgment that the physical
                        // Square press was written by the input hook — the on-screen meter
                        // reacts V + meter_delay ms later. Bottom-centre of the preview so it
                        // sits where the human is already looking without covering the meter;
                        // reposition by moving these anchors, opacity via shownOpacity.
                        PressedBadge {
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.bottom: parent.bottom
                            anchors.bottomMargin: 14
                            z: 10
                            pressed: orion.physicalSquarePressed === true
                        }

                        // True decoded-frame rate (unique fps — the REAL feed rate, not the
                        // display/preview). Colour-coded so 60fps is obvious at a glance.
                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.leftMargin: 16
                            anchors.topMargin: 48
                            width: fpsRow.implicitWidth + 16
                            height: 22
                            radius: 11
                            color: "#CC05080D"
                            border.color: orion.uniqueFrameFps >= 55 ? Theme.success
                                          : (orion.uniqueFrameFps >= 40 ? "#E0B341" : "#E06A6A")
                            border.width: 1
                            visible: root.streamLive
                            z: 10
                            Row {
                                id: fpsRow
                                anchors.centerIn: parent
                                spacing: 5
                                Text {
                                    text: orion.uniqueFrameFps
                                    color: orion.uniqueFrameFps >= 55 ? Theme.success
                                           : (orion.uniqueFrameFps >= 40 ? "#E0B341" : "#E06A6A")
                                    font.family: Theme.fontMono; font.pixelSize: 12; font.weight: Font.Bold
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    text: "FPS"
                                    color: Theme.textMuted
                                    font.family: Theme.fontUi; font.pixelSize: 9; font.weight: Font.DemiBold
                                    font.letterSpacing: 0.8
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }
                        }

                        // ===== TIMING WARM-UP BANNER: REMOVED (2026-09-12, owner) ================
                        // "TIMING WARMING UP — SHOTS STAY MANUAL" sat top-centre over the preview
                        // while orion.latencyCalibrationReady was false. With the user lead
                        // authoritative from the first press (user_lead_satisfies_authority) the
                        // state is momentary and the banner read as a fault. UI removal only: the
                        // readiness state, the Setup page's Timing pill and the
                        // waiting_for_latency_calibration log line are untouched. The dead-input
                        // overlay below keeps the top-centre slot to itself now.

                        // ===== [ORION_MOTD 2026-09-14] SERVER NOTICE (MOTD) BANNER ==============
                        // docs/ADMIN_PANEL_V2_CONTRACT.md §5: the owner's message of the day
                        // rides the /api/license/check heartbeat (and /api/version). It takes
                        // the top-centre slot the timing warm-up banner used and yields to the
                        // dead-input banner below (input not reaching the console outranks a
                        // notice). Level colours: info=accent, warn=warning, maint=danger.
                        // Dismiss is in-memory only (orion.dismissMotd()); a changed text
                        // re-shows it and the controller hides it once `until` passes.
                        Rectangle {
                            id: motdBanner
                            objectName: "motdBanner"
                            readonly property string level: orion.motdLevel || "info"
                            readonly property color tone: level === "maint" ? Theme.danger
                                                        : (level === "warn" ? Theme.warning : Theme.accent)
                            readonly property string tag: level === "maint" ? "MAINTENANCE"
                                                        : (level === "warn" ? "WARNING" : "NOTICE")
                            // Wrap the text before the banner would overflow the preview.
                            readonly property real textMaxWidth: Math.max(80,
                                parent.width - 32 - 28 - motdTagPill.width - motdClose.width - 2 * motdRow.spacing)
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: 16
                            width: motdRow.implicitWidth + 28
                            height: motdRow.implicitHeight + 16
                            radius: 12
                            color: "#F005080D"
                            border.color: tone
                            border.width: level === "maint" ? 2 : 1
                            // [CL2-P8-002 2026-09-23] The lease notice below outranks a notice.
                            visible: orion.motdVisible === true && !root.inputDead && !root.leaseBlocked
                            z: 12

                            // Unwrapped width of the notice, measured outside the wrapping Text so
                            // the width binding cannot loop on its own implicitWidth.
                            TextMetrics {
                                id: motdMetrics
                                font: motdText.font
                                text: orion.motdText
                            }

                            Row {
                                id: motdRow
                                anchors.centerIn: parent
                                spacing: 10

                                Rectangle {
                                    id: motdTagPill
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: motdTagText.implicitWidth + 12
                                    height: 18
                                    radius: 9
                                    color: Qt.rgba(motdBanner.tone.r, motdBanner.tone.g, motdBanner.tone.b, 0.18)
                                    border.color: motdBanner.tone
                                    border.width: 1
                                    Text {
                                        id: motdTagText
                                        anchors.centerIn: parent
                                        text: motdBanner.tag
                                        color: motdBanner.tone
                                        font.family: Theme.fontUi; font.pixelSize: 9; font.weight: Font.Bold
                                        font.letterSpacing: 0.8
                                    }
                                }

                                Text {
                                    id: motdText
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: orion.motdText
                                    color: Theme.textPrimary
                                    font.family: Theme.fontUi; font.pixelSize: 12; font.weight: Font.DemiBold
                                    wrapMode: Text.WordWrap
                                    width: Math.min(motdMetrics.advanceWidth + 2, motdBanner.textMaxWidth)
                                }

                                Rectangle {
                                    id: motdClose
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 20; height: 20; radius: 10
                                    color: motdCloseArea.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : "transparent"
                                    Text {
                                        anchors.centerIn: parent
                                        text: "×"
                                        color: motdCloseArea.containsMouse ? Theme.textPrimary : Theme.textSecondary
                                        font.family: Theme.fontUi; font.pixelSize: 14; font.weight: Font.Bold
                                    }
                                    MouseArea {
                                        id: motdCloseArea
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: orion.dismissMotd()
                                    }
                                }
                            }
                        }

                        // ===== [CL2-P8-002 2026-09-23] LICENCE LEASE BANNER ======================
                        // Shots are paused because the server lease lapsed (offline, after
                        // sleep, server unreachable, or the PC clock is off). Before this the
                        // bot silently stopped firing while everything looked Ready. Same
                        // top-centre slot as the MOTD (which it hides); yields to the
                        // dead-input banner. Not dismissible: it clears itself when the next
                        // heartbeat restores the lease. Text comes from the controller.
                        Rectangle {
                            id: leaseBanner
                            objectName: "leaseBanner"
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: 16
                            width: Math.min(leaseBannerRow.implicitWidth + 28, parent.width - 32)
                            height: leaseBannerRow.implicitHeight + 16
                            radius: 12
                            color: "#F005080D"
                            border.color: Theme.warning
                            border.width: 1
                            visible: root.leaseBlocked && !root.inputDead
                            z: 12

                            Row {
                                id: leaseBannerRow
                                anchors.centerIn: parent
                                spacing: 10

                                Rectangle {
                                    id: leaseTagPill
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: leaseTagText.implicitWidth + 12
                                    height: 18
                                    radius: 9
                                    color: Qt.rgba(Theme.warning.r, Theme.warning.g, Theme.warning.b, 0.18)
                                    border.color: Theme.warning
                                    border.width: 1
                                    Text {
                                        id: leaseTagText
                                        anchors.centerIn: parent
                                        text: "SHOTS PAUSED"
                                        color: Theme.warning
                                        font.family: Theme.fontUi; font.pixelSize: 9; font.weight: Font.Bold
                                        font.letterSpacing: 0.8
                                    }
                                }

                                Text {
                                    id: leaseBannerText
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: orion.leaseNotice || ""
                                    color: Theme.textPrimary
                                    font.family: Theme.fontUi; font.pixelSize: 12; font.weight: Font.DemiBold
                                    wrapMode: Text.WordWrap
                                    width: Math.min(leaseMetrics.advanceWidth + 2,
                                                    Math.max(80, leaseBanner.parent.width - 32 - 28
                                                                 - leaseTagPill.width - leaseBannerRow.spacing))
                                }
                            }

                            TextMetrics {
                                id: leaseMetrics
                                font: leaseBannerText.font
                                text: orion.leaseNotice || ""
                            }
                        }

                        // ===== [ORION_INPUT_DEAD_UX 2026-08-30] DEAD-INPUT OVERLAY ==============
                        // The one trap this page must never allow: the game looks perfectly
                        // alive (capture-card HDMI keeps flowing) while every button press is
                        // dead. Driven entirely by controller verdicts computed from the same
                        // pressUndeliverable() predicate as the "PRESS UNDELIVERABLE" log line.
                        //
                        // Two pieces, neither of which covers gameplay or fire-critical UI:
                        //   1. a pulsing border around the whole preview (border only — zero
                        //      pixels of video obscured; peripheral motion is what makes it
                        //      unmissable while the player watches the GAME, not the app);
                        //   2. a top-center banner (same slot/family as the timing warm-up
                        //      banner, which yields while this is up) stating plainly that
                        //      input is NOT reaching the console and what is happening
                        //      (retrying / press Connect / console waking / recovering).
                        // The meter overlay, PRESSED badge (bottom-center) and corner chips all
                        // keep their surfaces.
                        Rectangle {
                            id: inputDeadFrame
                            anchors.fill: parent
                            anchors.margins: 4
                            color: "transparent"
                            radius: 10
                            border.width: 3
                            border.color: root.inputDeadCritical ? Theme.danger : Theme.warning
                            visible: root.inputDead
                            z: 11
                            // Pulse only in the terminal dead state; steady while merely
                            // Connecting. The pulsing flag snaps opacity back when the pulse
                            // ends mid-cycle so the frame can never park half-faded.
                            readonly property bool pulsing: root.inputDead && root.inputDeadCritical
                            onPulsingChanged: if (!pulsing) opacity = 1.0
                            SequentialAnimation on opacity {
                                running: inputDeadFrame.pulsing
                                loops: Animation.Infinite
                                NumberAnimation { to: 0.25; duration: 450; easing.type: Easing.InOutQuad }
                                NumberAnimation { to: 1.0; duration: 450; easing.type: Easing.InOutQuad }
                            }
                        }

                        Rectangle {
                            objectName: "inputDeadBanner"
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: 16
                            width: Math.min(parent.width - 32, inputDeadCol.implicitWidth + 32)
                            height: inputDeadCol.implicitHeight + 18
                            radius: 12
                            color: "#F005080D"
                            border.color: root.inputDeadCritical ? Theme.danger : Theme.warning
                            border.width: root.inputDeadCritical ? 2 : 1
                            visible: root.inputDead
                            z: 12

                            Column {
                                id: inputDeadCol
                                anchors.centerIn: parent
                                width: parent.width - 32
                                spacing: 4
                                Row {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    spacing: 7
                                    Rectangle {
                                        width: 9; height: 9; radius: 4.5
                                        anchors.verticalCenter: parent.verticalCenter
                                        color: root.inputDeadCritical ? Theme.danger : Theme.warning
                                        SequentialAnimation on opacity {
                                            running: root.inputDead
                                            loops: Animation.Infinite
                                            NumberAnimation { to: 0.3; duration: 500 }
                                            NumberAnimation { to: 1.0; duration: 500 }
                                        }
                                    }
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: root.inputDeadHeadline
                                        color: root.inputDeadCritical ? "#FF9B9B" : "#F3C969"
                                        font.family: Theme.fontUi
                                        font.pixelSize: 12
                                        font.weight: Font.Bold
                                        font.letterSpacing: 1.0
                                    }
                                }
                                Text {
                                    width: parent.width
                                    horizontalAlignment: Text.AlignHCenter
                                    text: root.inputDeadDetail
                                    color: Theme.textSecondary
                                    font.family: Theme.fontUi
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }

                        // Active shot type — small chip, top-right (clean, minimal).
                        Rectangle {
                            id: shotTypeChip
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 16
                            width: shotTypeLabel.implicitWidth + 18
                            // First-run tour spotlight anchor (shot-type indicator).
                            Component.onCompleted: TourRegistry.register("rp:shotType", shotTypeChip)
                            Component.onDestruction: TourRegistry.unregister("rp:shotType", shotTypeChip)
                            height: 24
                            radius: 12
                            color: "#CC05080D"
                            border.color: Theme.borderSoft
                            border.width: 1
                            visible: root.streamLive && orion.activeBotShotType.length > 0
                            z: 10
                            Text {
                                id: shotTypeLabel
                                anchors.centerIn: parent
                                text: orion.activeBotShotType.toUpperCase()
                                color: Theme.textSecondary
                                font.family: Theme.fontUi
                                font.pixelSize: 10
                                font.weight: Font.Bold
                                font.letterSpacing: 1.0
                            }
                        }

                        // ===== FIX 2: No-Meter unavailable warning =========================
                        // Unmissable, and shown whether or not a stream is up (before Connect is
                        // exactly when the user can still fix it). Replaces the old silent failure:
                        // toggle on -> pose init throws -> bare `except` eats it -> a bot that
                        // simply never shoots, with a UI that looks completely normal.
                        Rectangle {
                            id: noMeterWarning
                            visible: root.noMeterBroken
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: 16
                            // Fixed-cap width (same idiom as the calibration card below): the
                            // height follows the wrapped text. Deriving the width from the
                            // layout's implicitWidth instead would couple width<->height.
                            width: Math.min(parent.width - 40, 460)
                            height: noMeterWarnCol.implicitHeight + 20
                            radius: 10
                            color: Theme.dangerDim
                            border.color: Theme.danger
                            border.width: 1
                            z: 20

                            ColumnLayout {
                                id: noMeterWarnCol
                                anchors.fill: parent
                                anchors.margins: 10
                                spacing: 4

                                Text {
                                    Layout.fillWidth: true
                                    text: "NO-METER MODE UNAVAILABLE"
                                    color: Theme.danger
                                    font.family: Theme.fontUi
                                    font.pixelSize: 11
                                    font.weight: Font.Black
                                    font.letterSpacing: 1.2
                                    horizontalAlignment: Text.AlignHCenter
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "This build does not ship the pose detection files, so no shot "
                                          + "will be timed or released. Switch back to meter detection."
                                    color: Theme.textPrimary
                                    font.family: Theme.fontUi
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                    horizontalAlignment: Text.AlignHCenter
                                }
                                Text {
                                    Layout.fillWidth: true
                                    visible: root.noMeterReason.length > 0
                                    text: root.noMeterReason
                                    color: Theme.textMuted
                                    font.family: Theme.fontMono
                                    font.pixelSize: 10
                                    wrapMode: Text.WordWrap
                                    horizontalAlignment: Text.AlignHCenter
                                }
                            }
                        }

                        // Lock box: one clean box around the detected meter —
                        // no debug search/reject boxes, fill/target lines, or algorithm text.
                        Item {
                            id: meterDebugLayer
                            objectName: "meterDebugLayer"
                            anchors.fill: parent
                            anchors.margins: 8
                            // Preview images occupy sibling z levels 1 and 2. The detector is the
                            // bot's primary visual truth, so keep its complete box above every
                            // non-modal capture HUD chip. A top-corner meter must never disappear
                            // behind LIVE/FPS/shot-type chrome.
                            z: 20
                            // CONFIRMED-METER GATE: show the lock ONLY while the detector has a FRESH,
                            // REAL meter lock (orion.meterConfirmed), NOT merely while Square is held
                            // (orion.isShooting was true ~135ms after the button regardless of whether a
                            // meter existed — so holding Square idle painted a lock on a stale/distractor
                            // bbox). Driven purely by the detector, so idle holds never show a box.
                            // Capture-card users see the preview before a console-input session is
                            // connected. `streamLive` is deliberately false in that passive mode,
                            // but detector telemetry and frame-ID joins are already authoritative.
                            // Hiding on `streamLive` alone made the bot work while its eyes appeared
                            // absent. The overlay is presentation-only, so enabling it for the live
                            // capture preview cannot alter detector pixels or release timing.
                            visible: (root.streamLive || root.previewActive)
                                     && !orion.noMeterEnabled
                                     && orion.meterConfirmed
                                     && orion.liveFrameWidth > 0
                                     && orion.liveFrameHeight > 0
                                     && orion.meterBoxWidth > 0
                                     && orion.meterBoxHeight > 0

                            // Map the exact frame-joined detector box onto the
                            // letterboxed preview with one uniform scale. Native
                            // does not smooth, stretch, or add a geometry guard.
                            readonly property real drawScale: Math.min(width / orion.liveFrameWidth, height / orion.liveFrameHeight)
                            readonly property real drawW: orion.liveFrameWidth * drawScale
                            readonly property real drawH: orion.liveFrameHeight * drawScale
                            readonly property real drawX: (width - drawW) * 0.5
                            readonly property real drawY: (height - drawH) * 0.5
                            readonly property real boxX: drawX + orion.meterBoxX * drawScale
                            readonly property real boxY: drawY + orion.meterBoxY * drawScale
                            // No screen-pixel floor: width and height retain the
                            // detector's aspect ratio even for a distant meter.
                            readonly property real boxW: orion.meterBoxWidth * drawScale
                            readonly property real boxH: orion.meterBoxHeight * drawScale
                            // Lock treatment: a vivid magenta outline;
                            // the hue itself is data — orion.meterOverlayDrawColor) that traces the
                            // meter's own silhouette — including its arrow caps — with no slack,
                            // no fill and no rounding. Square corners are deliberate: a radius
                            // pulls the stroke away from the cap tips, which is exactly where the
                            // player is reading the meter, and it makes a 20px-tall distant meter
                            // look like a lozenge rather than a bounding box.
                            //
                            // Colour and style are the user's choice, resolved entirely in
                            // C++. `meterOverlayDrawColor` is already the final stroke colour
                            // — the configured colour, or the current step of the RGB cycle —
                            // so QML never asks which mode is active and never runs colour
                            // maths. It is notified by its own low-fanout signal at most a few
                            // times a second, never per frame. Native guarantees a valid
                            // "#RRGGBB", so there is no fallback branch to evaluate here.
                            readonly property color lockColor: orion.meterOverlayDrawColor
                            readonly property string overlayStyle: orion.meterOverlayStyle
                            Item {
                                id: lockBox
                                objectName: "meterLockBox"
                                x: meterDebugLayer.boxX
                                y: meterDebugLayer.boxY
                                width: meterDebugLayer.boxW
                                height: meterDebugLayer.boxH
                                // Hairline is the whole point of that style: a single thin
                                // line instead of a confident stroke. One quiet outer keyline
                                // preserves contrast without turning the mark into three
                                // competing rectangles.
                                // Clean (2026-09-10, owner reference): ONE thin line a few pixels
                                // off the meter and nothing else -- no second keyline, no weight.
                                readonly property bool cleanStyle:
                                    meterDebugLayer.overlayStyle === "Clean"
                                readonly property real strokeW:
                                    (meterDebugLayer.overlayStyle === "Hairline" || lockBox.cleanStyle) ? 1 : 2
                                // A fixed one-screen-pixel gap is visually stable as the game
                                // camera zooms. Scaling this gap with the preview made the mark
                                // breathe even when the joined detector box did not.
                                readonly property real airGap: lockBox.cleanStyle ? 3 : 1
                                // Distance from the raw bbox edge out to the OUTERMOST drawn
                                // pixel. Published so anything that has to sit beside the lock
                                // measures its gap from the mark the user can actually see
                                // instead of from the invisible detector rectangle underneath.
                                readonly property real frameInset: airGap + strokeW + 1

                                // Every stroke lives wholly outside the raw bbox, so the lock can
                                // never paint over a meter pixel the detector is reading.
                                //
                                // Two rings, outside-in: a restrained dark keyline and the 2px
                                // lock colour. Both live wholly outside the raw bbox, leaving the
                                // meter pixels and its tip unobscured. Removing the redundant inner
                                // keyline makes the lock read as one clean mark instead of a stack.
                                //
                                // The two rings are the Solid and Hairline styles. In
                                // Brackets they are hidden rather than destroyed: toggling
                                // `visible` on two existing Rectangles is cheaper and more
                                // predictable than tearing down and rebuilding scene nodes,
                                // and the style is changed by hand from a settings page, not
                                // by anything on the shot path.
                                readonly property bool ringsVisible:
                                    meterDebugLayer.overlayStyle !== "Brackets"
                                Rectangle {
                                    objectName: "meterLockOuterFrame"
                                    visible: lockBox.ringsVisible && !lockBox.cleanStyle
                                    anchors.fill: parent
                                    anchors.margins: -lockBox.frameInset
                                    radius: 0
                                    color: "transparent"
                                    border.color: Theme.meterLockKeyline
                                    border.width: 1
                                    opacity: 0.68
                                }
                                Rectangle {
                                    objectName: "meterLockFrame"
                                    visible: lockBox.ringsVisible
                                    anchors.fill: parent
                                    anchors.margins: -(lockBox.airGap + lockBox.strokeW)
                                    radius: 0
                                    color: "transparent"
                                    border.color: meterDebugLayer.lockColor
                                    border.width: lockBox.strokeW
                                    opacity: 0.96
                                }

                                // BRACKETS style: four corner marks on the same rectangle the
                                // solid frame would occupy, so switching style never moves the
                                // lock. It exists to leave the meter's long edges completely
                                // unobstructed — on a small/distant meter a full outline can
                                // be a meaningful fraction of the thing you are trying to
                                // read. Behind a Loader so a user who never picks this style
                                // never instantiates any of it.
                                Loader {
                                    objectName: "meterLockBracketLoader"
                                    anchors.fill: parent
                                    // The bracket thickness ends exactly at the raw bbox edge;
                                    // no corner arm can paint over the meter or its tip.
                                    anchors.margins: -lockBox.frameInset
                                    active: meterDebugLayer.overlayStyle === "Brackets"
                                    sourceComponent: Component {
                                        Item {
                                            id: bracketLayer
                                            // Keyline + core + keyline, matching the rings.
                                            readonly property real thick: lockBox.strokeW + 2
                                            // Arms are a fraction of each axis and then capped,
                                            // so they read as corner marks on a big close meter
                                            // and do not merge into a full box on a small one.
                                            readonly property real armH:
                                                Math.max(2, Math.min(width * 0.5, 14))
                                            readonly property real armV:
                                                Math.max(2, Math.min(height * 0.25, 22))

                                            Repeater {
                                                model: 4
                                                delegate: Item {
                                                    id: corner
                                                    required property int index
                                                    anchors.fill: parent
                                                    readonly property bool atRight: index === 1 || index === 3
                                                    readonly property bool atBottom: index >= 2

                                                    Rectangle {
                                                        objectName: "meterLockBracketArmH"
                                                        x: corner.atRight ? corner.width - width : 0
                                                        y: corner.atBottom ? corner.height - height : 0
                                                        width: bracketLayer.armH
                                                        height: bracketLayer.thick
                                                        color: Theme.meterLockKeyline
                                                        opacity: 0.9
                                                        Rectangle {
                                                            anchors.fill: parent
                                                            anchors.margins: 1
                                                            color: meterDebugLayer.lockColor
                                                        }
                                                    }
                                                    Rectangle {
                                                        objectName: "meterLockBracketArmV"
                                                        x: corner.atRight ? corner.width - width : 0
                                                        y: corner.atBottom ? corner.height - height : 0
                                                        width: bracketLayer.thick
                                                        height: bracketLayer.armV
                                                        color: Theme.meterLockKeyline
                                                        opacity: 0.9
                                                        Rectangle {
                                                            anchors.fill: parent
                                                            anchors.margins: 1
                                                            color: meterDebugLayer.lockColor
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Three-value unboxed live readout. Deliberately a SIBLING of
                        // meterDebugLayer, not a child of it, so its own clamping is
                        // computed in the same coordinate space as the preview rather
                        // than inside a box that moves under it.
                        //
                        // METER-PRESENT GATE — the one thing this has now got right.
                        // It first lived inside the lock-box layer AND was gated on a
                        // live bot-owned shot, so it existed for the few hundred ms of
                        // an actual shot: the "shows briefly then disappears" report.
                        // The fix over-corrected into an always-on fixture, which left
                        // a "FILL -- TIP -- FIRE -186" panel parked over an empty court
                        // with no meter anywhere (2026-08-04 screenshot). It is now
                        // bound to `meterDebugLayer.visible` — the EXACT condition that
                        // draws the detection box (fresh orion.meterConfirmed lock with
                        // a real frame-joined bbox). Meter on screen, readout on screen;
                        // meter gone, readout gone. Not the narrow owned-shot window,
                        // which is what made it flicker.
                        //
                        // Binding to the sibling's `visible` rather than restating the
                        // five-clause condition is deliberate: there is then exactly one
                        // definition of "a meter is on screen", and the two marks cannot
                        // drift apart by a frame or by a later edit.
                        //
                        // Every value is native-published from real scheduler events.
                        // Nothing here computes, extrapolates or defaults a timing
                        // number: "--" is native saying it cannot prove that value.
                        //
                        // The gate above is visibility ONLY. It used to be the
                        // values' gate as well, by proxy: native published all
                        // three rows only during a bot-OWNED shot, which is ~180ms
                        // of a ~2500ms meter, so the box correctly appeared on the
                        // meter and then sat there reading "FILL -- TIP -- FIRE --"
                        // for the other ~93% of its life (2026-08-04 screenshots).
                        // The fix was on the native side and it was to POPULATE ON
                        // DETECTION, not to re-narrow this gate back to the shot:
                        // FILL is a camera reading and is now published for exactly
                        // as long as this lock is drawn. TIP/FIRE remain shot-scoped
                        // because they do not exist outside one.
                        MeterTelemetryHud {
                            id: meterTelemetryHud
                            objectName: "meterTelemetryHud"
                            z: 21
                            visible: meterDebugLayer.visible
                            // `measured` follows the DETECTOR (same freshness
                            // window that draws the box), `live` follows the
                            // owned shot. Keeping both is what lets the box show
                            // a real FILL for the whole time it is up while TIP
                            // and FIRE stay honestly blank between shots.
                            measured: orion.meterHudMeasured
                            live: orion.meterHudLive
                            commandLate: orion.meterHudCommandLate
                            // Same stroke colour as the lock, so the pair stays
                            // one mark under any user colour choice or the RGB
                            // cycle. Read from the layer's own resolved property
                            // rather than from orion, so there is exactly one
                            // place that decides what the overlay colour is.
                            accentColor: meterDebugLayer.lockColor

                            // Compact unboxed telemetry prefers the open space above
                            // the lock, like a lightweight instrument label. Near an
                            // edge it flips right, left, then below. Every branch is a
                            // direct binding to the same exact joined box: no Behavior,
                            // animation, low-pass or independent motion clock exists.
                            readonly property real gap: 4
                            // Drawn edges of the lock, in this item's coordinate space.
                            // These bindings re-evaluate on meterBoxChanged, exactly as
                            // the lock box itself does — no new signal, no new cadence.
                            // There is no "parked" branch any more: the item is only
                            // visible while the lock is drawn, so lockBox always has a
                            // real geometry whenever these are read.
                            readonly property real lockLeft:
                                meterDebugLayer.x + lockBox.x - lockBox.frameInset
                            readonly property real lockRight:
                                meterDebugLayer.x + lockBox.x + lockBox.width + lockBox.frameInset
                            readonly property real lockTop:
                                meterDebugLayer.y + lockBox.y - lockBox.frameInset
                            readonly property real lockBottom:
                                meterDebugLayer.y + lockBox.y + lockBox.height + lockBox.frameInset
                            readonly property real layerLeft: meterDebugLayer.x
                            readonly property real layerRight:
                                meterDebugLayer.x + meterDebugLayer.width
                            readonly property real layerTop: meterDebugLayer.y
                            readonly property real layerBottom:
                                meterDebugLayer.y + meterDebugLayer.height
                            readonly property bool fitsAbove:
                                lockTop - gap - height >= layerTop
                            readonly property bool fitsRight:
                                lockRight + gap + width <= layerRight
                            readonly property bool fitsLeft:
                                lockLeft - gap - width >= layerLeft
                            readonly property real centeredX: Math.max(
                                layerLeft, Math.min(layerRight - width,
                                    (lockLeft + lockRight - width) * 0.5))

                            x: fitsAbove ? centeredX
                               : fitsRight ? lockRight + gap
                               : fitsLeft ? lockLeft - gap - width
                               : centeredX
                            y: fitsAbove ? lockTop - gap - height
                               : (fitsRight || fitsLeft)
                                   ? Math.max(layerTop, Math.min(layerBottom - height,
                                                                 lockTop))
                                   : Math.max(layerTop, Math.min(layerBottom - height,
                                                                 lockBottom + gap))
                        }

                        // Optional truth-only shot telemetry. Keep it in a fixed capture corner
                        // instead of attaching chrome to the moving detector box: distant/go-to
                        // meters remain completely unobstructed. While the primary attached
                        // FILL/TIP/FIRE instrument is visible this secondary ETA/HOLD readout
                        // yields, avoiding two simultaneous telemetry clusters. It can take over
                        // after the meter lock leaves so a still-current HOLD result is not lost.
                        // Native returns negative values whenever current arm-token/freshness
                        // proof is absent, so no placeholder or decorative number is ever shown.
                        Column {
                            id: liveMeterMetrics
                            objectName: "liveMeterMetricsOverlay"
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            anchors.margins: 16
                            z: 20
                            spacing: 2
                            opacity: 0.82
                            readonly property real etaMs: orion.shotEtaToTargetMs
                            readonly property real holdMs: orion.shotHoldMs
                            readonly property bool etaAvailable: etaMs >= 0
                            readonly property bool holdAvailable: holdMs >= 0
                            visible: orion.showLiveMeterMetrics
                                     && (root.streamLive || root.previewActive)
                                     && !meterDebugLayer.visible
                                     && (etaAvailable || holdAvailable)

                            Text {
                                objectName: "meterEtaMetric"
                                visible: liveMeterMetrics.etaAvailable
                                text: visible ? "ETA  " + liveMeterMetrics.etaMs.toFixed(0) + " ms" : ""
                                color: "#DCEBFF"
                                style: Text.Outline
                                styleColor: "#D9050A12"
                                font.family: Theme.fontMono
                                font.pixelSize: 10
                                font.weight: Font.Bold
                            }
                            Text {
                                objectName: "meterHoldMetric"
                                // Local bot-ownership-to-submit duration; this does not claim
                                // console receipt and is hidden when the native value is absent.
                                visible: liveMeterMetrics.holdAvailable
                                text: visible ? "HOLD " + liveMeterMetrics.holdMs.toFixed(0) + " ms" : ""
                                color: "#AFCBEE"
                                style: Text.Outline
                                styleColor: "#D9050A12"
                                font.family: Theme.fontMono
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                            }
                        }

                        // Skeleton overlay (skele mode): the locked player's pose + lock box,
                        // streamed per-frame from the sidecar (orion.poseKeypoints / poseBox in
                        // full-frame px). Gated on Show Skeleton + Skele detection mode.
                        Canvas {
                            id: skeleCanvas
                            anchors.fill: parent
                            anchors.margins: 8
                            // FIX 2: also gated on the pose stack ACTUALLY being installed. Without
                            // it the sidecar can never emit a pose_overlay, so this canvas silently
                            // stayed blank and looked like "the skeleton just isn't showing" rather
                            // than "this entire mode cannot run". The gate keeps it off explicitly
                            // and the warning banner above says why. Inert on a build that has not
                            // wired orion.noMeterAvailable (noMeterSupported defaults to true).
                            visible: root.streamLive && orion.showSkeleton && orion.noMeterEnabled
                                     && root.noMeterSupported
                                     && orion.liveFrameWidth > 0 && orion.poseBox.length >= 4
                            renderStrategy: Canvas.Cooperative
                            z: 9

                            readonly property real dScale: Math.min(width / Math.max(1, orion.liveFrameWidth),
                                                                     height / Math.max(1, orion.liveFrameHeight))
                            readonly property real dX: (width - orion.liveFrameWidth * dScale) * 0.5
                            readonly property real dY: (height - orion.liveFrameHeight * dScale) * 0.5

                            Connections {
                                target: orion
                                function onPoseOverlayChanged() { skeleCanvas.requestPaint() }
                            }

                            onPaint: {
                                var ctx = getContext("2d")
                                ctx.clearRect(0, 0, width, height)
                                var kp = orion.poseKeypoints
                                var box = orion.poseBox
                                var anchor = orion.poseAnchor
                                var lockCenter = orion.poseLockCenter
                                var indicator = orion.poseIndicator
                                function sx(px) { return dX + px * dScale }
                                function sy(py) { return dY + py * dScale }
                                var line = "#FFFFFF"   // skeleton bones + box + tether: white
                                var dot = "#FF3B30"    // joints + user indicator: red
                                var lineShadow = "#0A0A0A"  // dark underlay so white reads on any background

                                if (box.length >= 4) {
                                    var bx = sx(box[0]), by = sy(box[1])
                                    var bw = (box[2] - box[0]) * dScale, bh = (box[3] - box[1]) * dScale
                                    ctx.strokeStyle = lineShadow; ctx.lineWidth = 3.5; ctx.strokeRect(bx, by, bw, bh)
                                    ctx.strokeStyle = line; ctx.lineWidth = 2; ctx.strokeRect(bx, by, bw, bh)
                                }

                                var edges = [[0,1],[0,2],[1,3],[2,4],[5,6],[5,7],[7,9],[6,8],[8,10],
                                             [5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16]]
                                // Skeleton bones: dark underlay first (so white reads on any
                                // background), then the white line on top.
                                for (var pass = 0; pass < 2; ++pass) {
                                    ctx.strokeStyle = pass === 0 ? lineShadow : line
                                    ctx.lineWidth = pass === 0 ? 4.5 : 2.5
                                    for (var e = 0; e < edges.length; ++e) {
                                        var a = kp[edges[e][0]], b = kp[edges[e][1]]
                                        if (!a || !b || a[2] < 0.3 || b[2] < 0.3) continue
                                        ctx.beginPath(); ctx.moveTo(sx(a[0]), sy(a[1])); ctx.lineTo(sx(b[0]), sy(b[1])); ctx.stroke()
                                    }
                                }
                                // Joints: red dot with a thin dark ring for contrast.
                                for (var k = 0; k < kp.length; ++k) {
                                    var p = kp[k]
                                    if (!p || p[2] < 0.3) continue
                                    ctx.beginPath(); ctx.arc(sx(p[0]), sy(p[1]), 3.5, 0, 6.2832)
                                    ctx.fillStyle = dot; ctx.fill()
                                    ctx.lineWidth = 1; ctx.strokeStyle = lineShadow; ctx.stroke()
                                }

                                // Camera anchor: green tether from the fixed
                                // bottom-center screen point to the locked player's box center.
                                if (anchor && anchor.length >= 2 && lockCenter && lockCenter.length >= 2) {
                                    ctx.strokeStyle = line
                                    ctx.lineWidth = 2
                                    ctx.beginPath()
                                    ctx.moveTo(sx(anchor[0]), sy(anchor[1]))
                                    ctx.lineTo(sx(lockCenter[0]), sy(lockCenter[1]))
                                    ctx.stroke()
                                }

                                // Under-player user-indicator marker (skipped on a null/empty
                                // indicator, i.e. not detected this frame).
                                if (indicator && indicator.length >= 2) {
                                    ctx.beginPath()
                                    ctx.arc(sx(indicator[0]), sy(indicator[1]), 5, 0, 6.2832)
                                    ctx.fillStyle = dot; ctx.fill()
                                    ctx.lineWidth = 1.5; ctx.strokeStyle = lineShadow; ctx.stroke()
                                }
                            }
                        }

                        // ====== Session read-out (on the video) ======
                        // A clean strip — session time + green make-rate + shots. No internals.
                        Rectangle {
                            id: meterReadout
                            visible: false
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            anchors.margins: 14
                            height: readoutText.implicitHeight + 16
                            radius: 10
                            color: Theme.overlayScrim
                            border.color: Theme.borderSoft
                            border.width: 1

                            Text {
                                id: readoutText
                                anchors.fill: parent
                                anchors.margins: 8
                                verticalAlignment: Text.AlignVCenter
                                text: "Session " + orion.sessionTime
                                      + "    ·    " + orion.sessionGreens + "/" + orion.sessionVerdicts + " green"
                                      + "    ·    " + orion.shotsReleased + " shots"
                                color: Theme.textSecondary
                                font.family: Theme.fontMono
                                font.pixelSize: 12
                                elide: Text.ElideRight
                            }
                        }

                        // ===== Shoot-to-train calibration overlay (no boxing) =====
                        // The user just takes their normal shots; the detector auto-learns the
                        // meter colour + green hue. Driven entirely by orion.meterCalibration*.
                        Item {
                            id: calibrateLayer
                            anchors.fill: parent
                            anchors.margins: 8
                            visible: false

                            Rectangle { anchors.fill: parent; color: "#AA060910" }

                            Rectangle {
                                anchors.centerIn: parent
                                width: Math.min(parent.width - 28, 460)
                                height: calCol.implicitHeight + 32
                                radius: 14
                                color: Theme.modalSurface
                                border.color: Theme.modalBorder
                                border.width: 1

                                ColumnLayout {
                                    id: calCol
                                    anchors.fill: parent
                                    anchors.margins: 18
                                    spacing: 14

                                    Text {
                                        Layout.fillWidth: true
                                        text: "CALIBRATING METER"
                                        color: Theme.textMuted
                                        font.family: Theme.fontUi
                                        font.pixelSize: 10
                                        font.weight: Font.DemiBold
                                        font.letterSpacing: 1.4
                                        horizontalAlignment: Text.AlignHCenter
                                    }

                                    RowLayout {
                                        Layout.alignment: Qt.AlignHCenter
                                        spacing: 6
                                        Text {
                                            text: orion.meterCalibrationShots
                                            color: Theme.accent
                                            font.family: Theme.fontUi
                                            font.pixelSize: 48
                                            font.weight: Font.Bold
                                        }
                                        Text {
                                            Layout.alignment: Qt.AlignBottom
                                            Layout.bottomMargin: 9
                                            text: "/ " + orion.meterCalibrationTarget + " shots"
                                            color: Theme.textMuted
                                            font.family: Theme.fontUi
                                            font.pixelSize: 16
                                        }
                                    }

                                    Row {
                                        Layout.alignment: Qt.AlignHCenter
                                        spacing: 6
                                        Repeater {
                                            model: orion.meterCalibrationTarget
                                            delegate: Rectangle {
                                                required property int index
                                                width: 11; height: 11; radius: 5.5
                                                color: index < orion.meterCalibrationShots ? Theme.success : Theme.borderStrong
                                                Behavior on color { ColorAnimation { duration: 160 } }
                                            }
                                        }
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: orion.meterCalibrationStatus.length > 0
                                              ? orion.meterCalibrationStatus
                                              : "Venice learns your meter automatically while you play."
                                        color: Theme.textSecondary
                                        font.family: Theme.fontUi
                                        font.pixelSize: 12
                                        wrapMode: Text.WordWrap
                                        horizontalAlignment: Text.AlignHCenter
                                    }

                                    DangerButton {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: "Cancel"
                                        implicitWidth: 120
                                        implicitHeight: 36
                                        onClicked: orion.cancelMeterCalibration()
                                    }
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 50
                        Layout.minimumHeight: 50
                        Layout.maximumHeight: 50
                        Layout.topMargin: 2
                        spacing: 12

                        PrimaryButton {
                            id: startStreamBtn
                            text: root.connecting ? "Connecting…"
                                  : (root.streamLive ? "Connected" : "Connect")
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.preferredWidth: 1
                            enabled: !root.streamLive && !root.connecting && !root.disconnecting
                            onClicked: orion.connectRemotePlay()
                            // First-run tour spotlight anchor (connect control).
                            Component.onCompleted: TourRegistry.register("rp:connect", startStreamBtn)
                            Component.onDestruction: TourRegistry.unregister("rp:connect", startStreamBtn)
                        }
                        DangerButton {
                            text: root.disconnecting ? "Disconnecting…" : "Disconnect"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.preferredWidth: 1
                            // `errored` is included so a session that FAILED (e.g. a stream
                            // promotion that could not launch Chiaki) is not a dead end: the
                            // detection sidecar can still be alive holding the capture card, and
                            // Disconnect is the only control that tears it down. Without it the
                            // user's only option was a Start that had nothing to fix.
                            enabled: (root.streamLive || root.connecting || root.errored)
                                     && !root.disconnecting
                            onClicked: orion.disconnectRemotePlay()
                        }

                        // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] One-time, user-consented USB
                        // power fix for the pad-drops-off-USB-after-idle wedge. Visible only
                        // while a known Sony pad entry still carries the risky Windows default
                        // (EnhancedPowerManagementEnabled=1); disappears once applied. The
                        // click IS the consent — the app never rewrites power settings silently.
                        // Primary discovery is the Setup Guide's "Controller Setup" step
                        // (FirstRunTour), which spotlights this button via the registry key
                        // below. Moved out of the main flow 2026-08-08: setup owns discovery,
                        // so the repeat-use copy is a compact secondary action beside
                        // Disconnect (its explanation lives in the hover tooltip), not a
                        // full-width banner button. A new USB port can re-surface it.
                        Button {
                            id: controllerUsbFixBtn
                            objectName: "controllerUsbFixButton"
                            visible: orion.controllerUsbPowerFixAvailable
                            text: "Fix Controller"
                            Layout.preferredWidth: 116
                            Layout.fillHeight: true
                            hoverEnabled: true
                            font.family: Theme.fontUi
                            font.pixelSize: 12
                            onClicked: orion.applyControllerUsbPowerFix()
                            contentItem: Text {
                                text: controllerUsbFixBtn.text
                                color: Theme.textSecondary
                                font: controllerUsbFixBtn.font
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: Theme.radiusControl
                                color: controllerUsbFixBtn.down ? Theme.bgInset : Theme.bgField
                                border.width: 1
                                border.color: controllerUsbFixBtn.hovered ? Theme.borderStrong
                                                                          : Theme.borderSoft
                            }
                            ToolTip {
                                id: controllerUsbFixTip
                                visible: controllerUsbFixBtn.hovered
                                delay: 420
                                timeout: 8000
                                y: -(implicitHeight + 8)
                                padding: 0
                                text: "One-time Windows USB power fix — stops the controller "
                                      + "dropping off USB after the PC sits idle."
                                contentItem: Text {
                                    text: controllerUsbFixTip.text
                                    color: Theme.textPrimary
                                    font.family: Theme.fontUi
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                    width: Math.min(300, implicitWidth)
                                    leftPadding: 11
                                    rightPadding: 11
                                    topPadding: 8
                                    bottomPadding: 9
                                }
                                background: Rectangle {
                                    radius: 8
                                    color: Theme.modalSurface
                                    border.color: Theme.borderStrong
                                    border.width: 1
                                }
                            }
                            // First-run tour spotlight anchor (Controller Setup step).
                            Component.onCompleted: TourRegistry.register("rp:controllerFix", controllerUsbFixBtn)
                            Component.onDestruction: TourRegistry.unregister("rp:controllerFix", controllerUsbFixBtn)
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        // Calibration result line ("Calibrated ✓ …") once the run finishes.
                        visible: false
                        text: orion.meterCalibrationStatus
                        color: "#8BD5A0"
                        font.family: "Cascadia Mono"
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }

                    // Activity feed — expanded by default (2026-08-08) so the owner
                    // can skim a batch without hunting for the "+"; the toggle below
                    // still collapses it when the live feed should dominate.
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: root.activityExpanded
                        Layout.preferredHeight: root.activityExpanded ? 160 : 42
                        Layout.topMargin: 8
                        radius: 12
                        color: "transparent"
                        border.color: Theme.borderSoft
                        border.width: 1

                        gradient: Gradient {
                            orientation: Gradient.Vertical
                            GradientStop { position: 0.0; color: Theme.bgInset }
                            GradientStop { position: 1.0; color: Theme.bgField }
                        }

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 8

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 7

                                Rectangle {
                                    Layout.preferredWidth: 7
                                    Layout.preferredHeight: 7
                                    radius: 4
                                    color: root.streamLive || root.previewActive
                                           ? Theme.success : Theme.textFaint
                                }
                                Text {
                                    text: "ACTIVITY"
                                    color: Theme.textMuted
                                    font.family: Theme.fontUi
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                    font.letterSpacing: 1.4
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: root.captureLogLines + (root.captureLogLines === 1 ? " event" : " events")
                                    color: Theme.textFaint
                                    font.family: Theme.fontMono
                                    font.pixelSize: 10
                                }
                                // One-click copy of the on-disk engineer log's
                                // bounded tail (native side; far more history than
                                // the filtered rows shown below) so the log can be
                                // pasted into a support ticket or AI chat instead
                                // of screenshotted. The MouseArea takes the
                                // exclusive grab on press, so a tap here does not
                                // also toggle the header's expand TapHandler.
                                Text {
                                    id: activityCopyLabel
                                    text: activityCopyArea.copied ? "Copied" : "Copy"
                                    color: activityCopyArea.copied ? Theme.success
                                           : (activityCopyArea.containsMouse ? Theme.accentHover : Theme.textMuted)
                                    font.family: Theme.fontUi
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                    font.underline: activityCopyArea.containsMouse && !activityCopyArea.copied
                                    MouseArea {
                                        id: activityCopyArea
                                        property bool copied: false
                                        anchors.fill: parent
                                        anchors.margins: -6
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: {
                                            orion.copyActivityLog()
                                            copied = true
                                            activityCopyReset.restart()
                                        }
                                        Timer {
                                            id: activityCopyReset
                                            interval: 1600
                                            onTriggered: activityCopyArea.copied = false
                                        }
                                    }
                                }
                                // Escape hatch for history older than the copy's
                                // tail bound: open the logs folder so the full
                                // orion_native.log can be attached to a report.
                                Text {
                                    text: "Logs"
                                    color: activityLogsArea.containsMouse ? Theme.accentHover : Theme.textMuted
                                    font.family: Theme.fontUi
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                    font.underline: activityLogsArea.containsMouse
                                    MouseArea {
                                        id: activityLogsArea
                                        anchors.fill: parent
                                        anchors.margins: -6
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: orion.openLogsFolder()
                                    }
                                }
                                Text {
                                    text: root.activityExpanded ? "−" : "+"
                                    color: Theme.textMuted
                                    font.family: Theme.fontUi
                                    font.pixelSize: 14
                                }
                                TapHandler { onTapped: root.activityExpanded = !root.activityExpanded }
                            }

                            Rectangle {
                                id: logPanel
                                visible: root.activityExpanded
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                color: "transparent"

                                ListView {
                                    id: activityList
                                    anchors.fill: parent
                                    clip: true
                                    model: captureLogModel
                                    boundsBehavior: Flickable.StopAtBounds
                                    spacing: 2
                                    ScrollBar.vertical: ScrollBar {
                                        id: logVerticalScrollBar
                                        policy: ScrollBar.AsNeeded
                                        width: 6
                                        contentItem: Rectangle { radius: 3; color: logVerticalScrollBar.pressed ? Theme.scrollbarThumbActive : Theme.scrollbarThumb }
                                        background: Rectangle { color: "transparent" }
                                    }

                                    delegate: Row {
                                        required property string line
                                        width: activityList.width - 10
                                        height: 20
                                        spacing: 8

                                        Rectangle {
                                            width: 5
                                            height: 5
                                            radius: 3
                                            anchors.verticalCenter: parent.verticalCenter
                                            color: root.activityTone(parent.line)
                                            opacity: 0.9
                                        }
                                        TextEdit {
                                            width: parent.width - 13
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: parent.line
                                            readOnly: true
                                            selectByMouse: true
                                            selectedTextColor: Theme.textPrimary
                                            selectionColor: Theme.accentMuted
                                            textFormat: TextEdit.PlainText
                                            color: Theme.textSecondary
                                            font.family: Theme.fontMono
                                            font.pixelSize: 10
                                        }
                                    }

                                    onCountChanged: Qt.callLater(positionViewAtEnd)
                                }

                                Text {
                                    anchors.centerIn: parent
                                    visible: captureLogModel.count === 0
                                    text: "No live-capture events yet."
                                    color: "#C6CEDA"
                                    font.family: Theme.fontMono
                                    font.pixelSize: 11
                                }
                            }
                        }
                    }
                }
            }

            // The meter/detection panel stays visible while streaming so it can be
            // tuned next to the live capture. The decoder-pipe frame feed is
            // resolution-independent, so the capture no longer needs the full width;
            // it's letterboxed to a neat 16:9 instead (see captureHost).
            //
            // 336, up from 308: the panel took on Detection Box and Shot Lead, and
            // Shot Lead's top row (value + state pill + Reset) needs ~284px of
            // content before it starts colliding. 28px is the smallest bump that
            // clears it. It costs the preview 28px of width, which the 16:9
            // letterbox absorbs without changing the capture's aspect or scale.
            Loader {
                Layout.preferredWidth: 336
                Layout.fillHeight: true
                sourceComponent: setupPanel
            }
        }
    }

    Component {
        id: setupPanel

        // The meter/detection section (moved here from the Dashboard) so it can be
        // tuned next to the live capture. Train Detection lives in this panel.
        ScrollView {
            id: meterScroll
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical: ScrollBar {
                id: meterVerticalScrollBar
                policy: ScrollBar.AsNeeded
                width: 6
                contentItem: Rectangle { radius: 3; color: meterVerticalScrollBar.pressed ? Theme.scrollbarThumbActive : Theme.scrollbarThumb }
                background: Rectangle { color: "transparent" }
            }

            // A ColumnLayout, not a Column: the two cards below are `Card`s that
            // size themselves through Layout.preferredHeight, which a plain
            // Column would silently ignore and collapse to zero height.
            ColumnLayout {
                width: meterScroll.availableWidth
                spacing: 12

                // [ORION_NO_METER_SHELVED 2026-09-15] The METER / NO METER switch, the
                // NoMeterCard and its RhythmCard are OUT of the customer's reach (owner: "shelve
                // the no meter path, we'll beef that up for a later update"). Nothing is deleted
                // and nothing is compiled out: the blind engine (NO METER v2, the hybrid, the
                // frame quantiser, the hold learner) is still built and still tested, and the
                // meter path's own blind backstop still uses it -- the mode simply has no way in
                // from the UI, and AppConfig forces input_timed_enabled false on load and save.
                // Re-shipping it means restoring this block and lifting that fence; the QML
                // contract test pins BOTH halves so neither can drift back on its own.

                MeterConfigPanel {
                    id: meterPanel
                    // [ORION_NO_METER_SHELVED 2026-09-15] Unconditional: there is one timing path
                    // on this page now, so a `visible` binding on the mode would be a switch that
                    // can never be thrown.
                    Layout.fillWidth: true
                    streamLive: root.streamLive
                    // First-run tour spotlight anchor (meter Style/Color).
                    Component.onCompleted: TourRegistry.register("rp:meter", meterPanel)
                    Component.onDestruction: TourRegistry.unregister("rp:meter", meterPanel)
                }

                // [ORION_NO_METER_SHELVED 2026-09-15] The NO METER side of the panel -- the
                // NoMeterCard hold slider and the input-timed variant of RhythmCard -- is unmounted
                // with the switch that selected it. NoMeterCard.qml and RhythmCard.qml both stay
                // in the tree (RhythmCard's meter variant is still mounted inside
                // MeterConfigPanel), so re-shipping the mode is a remount, not a rewrite.

                // ===== moved from the Setup page, 2026-08-04 =====================
                // User: "detection box and lead should be in the live tab under
                // network". Shot Lead lands directly beneath MeterConfigPanel.
                //
                // It belongs here because its feedback is on the screen to the
                // left: Shot Lead is tuned against the game's own TIMING banner,
                // which is only readable while the stream is up.
                //
                // "Detection Box" (Solid/Brackets/Hairline style picker) used to
                // sit here. REMOVED ENTIRELY 2026-08-06 (owner: "remove detection
                // box card completely") — the colour picker went earlier the same
                // day. The drawn lock overlay itself STAYS (bright blue factory
                // default, frame-id ring join intact); meterDebugLayer still reads
                // orion.meterOverlayStyle, whose persisted value simply has no UI
                // any more.
                // ================================================================

                // [LIVE TIMING PILL REMOVED 2026-09-14 owner] The Timing readiness pill
                // (objectName liveTimingStatus) is deleted. Its warm-up banner partner
                // went on 2026-09-12, so it pointed at nothing, and its not-ready wording
                // was internal vocabulary parked permanently beside the stream.
                // orion.latencyCalibrationReady stays engine-side and unread by QML.

                // [ORION_USER_LEAD] The one timing control a customer tunes.
                // Self-contained on purpose (see ShotLeadCard.qml).
                ShotLeadCard {
                    objectName: "shotLeadCard"
                    // [ORION_NO_METER_V2 2026-09-14] METER only. Shot Lead is how far AHEAD of a
                    // predicted tip the vision path fires; the blind path has no prediction and
                    // subtracts no lead, so leaving this on screen in NO METER would be a dial
                    // that changes nothing — the exact failure that wasted the owner's tuning
                    // session under the old law.
                    // [ORION_NO_METER_SHELVED 2026-09-15] With NO METER shelved the meter path is
                    // the only path, so the card is unconditional.
                    Layout.fillWidth: true
                }


                // [ORION_RHYTHM 2026-08-27] Retired by owner request (Tempo was off, so no stick
                // flick was generated and the control was inert).
                // [ORION_RHYTHM_RESTORED 2026-09-11] Back by owner request, and mounted inside
                // MeterConfigPanel directly ABOVE Meter Delay (owner placement), not here.

                // [ORION_TIP_FOLDED 2026-08-28] Tip Timing was RETIRED from the customer view
                // (owner request: combine timing into one control), on the premise that the engine
                // auto-aims the tip well enough to never need touching.
                //
                // [ORION_TIP_RESTORED 2026-09-11] Briefly unretired to stop the learner walking the
                // aim (measured 388.4 -> 369.5 ms DURING one 98-shot session). The FREEZE works and
                // STAYS ON -- settings.tip_phase_aim_frozen = true with learning.json
                // learned_phase_physical_ms = 287.7, verified holding at 361.7 ms across a full
                // n=20 window whose raw samples swung 326-393 ms.
                //
                // [ORION_TIP_REFOLDED 2026-09-11 owner] The CARD is retired again: with the aim
                // pinned, owner reports the control makes no timing difference, so it is one more
                // dial that does nothing. The freeze is config, not UI -- it needs no card. The
                // residual early/late spread is therefore NOT the aim constant; it is downstream,
                // and the card would only hide that. orion.tipTimingMs stays live and
                // engine-managed; TipTimingCard.qml stays in the tree, just not shown.
            }
        }
    }

    // In QML-render mode the chiaki window is parked off-screen by C++ (the panel
    // shows the decoder-pipe preview), so the 250ms embed-rect sync is pure waste —
    // only run it in legacy embed mode.
    Timer {
        interval: 250
        repeat: true
        running: !orion.qmlRenderMode
        triggeredOnStart: true
        onTriggered: root.syncChiakiEmbed()
    }

    function syncChiakiEmbed() {
        if (orion.qmlRenderMode)
            return
        // root.visible is false while another tab is current (the page is kept
        // alive but hidden) — hide the embedded Chiaki window so it doesn't float
        // over the other page. It re-shows automatically when this tab returns.
        if (!root.visible || !captureHost || !captureHost.visible || !root.streamLive) {
            orion.setChiakiEmbedVisible(false)
            return
        }
        // Embed the real Chiaki stream window directly over the capture panel so
        // the user sees the true console stream (never the desktop) with no
        // separate floating window. Map the inner video area (inside the 8px
        // margin) into Orion-window coordinates; C++ scales for device pixel ratio.
        var p = captureHost.mapToItem(null, 8, 8)
        var w = captureHost.width - 16
        var h = captureHost.height - 16
        if (w < 80 || h < 80) {
            orion.setChiakiEmbedVisible(false)
            return
        }
        orion.updateChiakiEmbedRect(p.x, p.y, w, h)
        orion.setChiakiEmbedVisible(true)
    }

    Component.onCompleted: {
        stableStatus = String(orion.remoteStatus)
        pendingStatus = stableStatus
        syncCaptureLogModel()
        syncChiakiEmbed()
        queuePreviewSerial(orion.frameSerial)
        // Open the capture card as soon as the dashboard mounts so the HDMI feed is visible BEFORE
        // the user presses Connect (no-op unless capture-card mode with no session up; see the
        // controller's startCapturePreview guards).
        orion.startCapturePreview()
    }

    Component.onDestruction: orion.setChiakiEmbedVisible(false)

    component SectionLabel: Text {
        color: root.accentSoft
        font.family: Theme.fontUi
        font.pixelSize: 10
        font.weight: Font.Black
        font.letterSpacing: 1.4
        font.capitalization: Font.AllUppercase
    }

    component StatusName: Text {
        Layout.fillWidth: true
        color: root.muted
        font.family: "Segoe UI Variable"
        font.pixelSize: 12
    }

    component StatusValue: Text {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignRight
        color: root.text
        font.family: "Segoe UI Variable"
        font.pixelSize: 12
        font.weight: Font.DemiBold
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
        horizontalAlignment: Text.AlignRight
    }

}
