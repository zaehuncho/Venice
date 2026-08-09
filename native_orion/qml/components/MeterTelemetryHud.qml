import QtQuick
import OrionNative

// Live meter-side telemetry readout, drawn beside the detected meter's lock box.
//
// VISIBILITY — TIED TO THE METER, NOT TO A SHOT (2026-08-04, user: "remove the
// fill tip fire on the screen it should only display on screen when the meter
// shows"). The page gates this on the SAME condition that draws the lock box
// (orion.meterConfirmed + a real frame-joined bbox), so it appears and vanishes
// with the detection box and never floats over an empty court with "--" in it.
//
// That is deliberately NOT the older gate. This readout was once gated on a
// live bot-OWNED shot, which existed for only a few hundred ms and produced the
// "shows briefly then disappears" report; it was then made unconditionally
// persistent, which produced the opposite complaint (a parked "FILL -- TIP --
// FIRE -186" strip with no meter anywhere on screen). Meter present is the
// correct middle: it is on for as long as there is something to read.
//
//   FILL  the genuine raw detector fill %. Ground truth for the eye: if this is
//         frozen or nonsense, every number beside it is fiction. It is a CAMERA
//         reading, so it is published for as long as the detector has a fresh
//         genuine sample — i.e. for as long as this box is on screen.
//   TIP   ms until the predicted tip crossing. The decision the app exists for.
//   FIRE  ms until the command deadline (tip - actuation lead), and while a
//         precise-fire token is armed, the armed schedule itself — recomputed
//         against the live clock on every publish, never latched at arm time.
//         Goes RED the instant it is negative, because a deadline already in
//         the past is unrecoverable, not merely tight. TIP minus FIRE is the
//         lead, so the actuation latency is readable without a fourth field.
//
// EXACTLY THREE ROWS (2026-08-06, owner: "it only needs 3 values not 6 only 3
// values that the bot uses and make it actually LIVE and not static"). COURT
// and JITTER — added earlier the same day — are REMOVED again: they are
// network diagnostics, not inputs to the timing decision, and they were most
// of the static feel. COURT was a string that never changed ("LOCAL" for this
// topology's whole life) and JITTER a sub-ms figure that moved a tenth at a
// time; two permanently-frozen rows made the whole plate read as a static
// sticker even while FILL underneath was streaming. The three rows that
// remain are the bot's own decision chain, and each one genuinely moves:
// FILL streams with the detector (~0.18pp/ms during a rise, re-published at
// 30 Hz), TIP counts down per tick while the bot owns the shot, and FIRE is
// a live `deadline - now` countdown while a token is armed. TIP/FIRE reading
// "--" OUTSIDE a bot-owned shot is truth, not staleness — the engine has no
// prediction to show there, and synthesising one here would put a second,
// non-authoritative countdown next to the meter (see below).
//
// LAYOUT — A BOX, not a strip (2026-08-04, user: "it should be in a BOX not a
// horizontal or vertical box a litteral box like i showed before", with their
// reference: a small accent-bordered panel sitting immediately right of the
// meter, caption and value on one line, stacked). Caption/value lines in
// a fixed caption gutter, so all values start on one vertical rule and
// the panel reads as a plate rather than a ticker. Three lines as of
// 2026-08-06 (COURT/JITTER added, then removed, on owner request).
//
// It is the SMALLEST of the three shapes this has had, and that matters because
// size has been the complaint twice:
//   3-row block (old) ~104 x 63 = 6552 px^2
//   1-line strip      ~168 x 23 = 3864 px^2
//   this box          ~ 76 x 55 = 4180 px^2, and 55 px of height against the
//                     strip's 23 — but 8 px SHORTER and 27% narrower than the
//                     3-row block the height complaint was actually about.
// The savings come from the caption gutter (captions are 8 px and share a
// column instead of each pushing its own value right) and from 1 px row
// spacing, not from shrinking the digits: the value glyphs stay at 11 px
// because a number is re-read every shot while a caption is recognised by
// position after the first look.
//
// TRUTH: every value is a pre-formatted string published by OrionAppController
// from one batched LiveMeterTelemetry snapshot taken on a single 4 ms input tick.
// There is no QML-side timer, no client-side arithmetic and no fallback value.
// "--" is native's own statement that it could not prove that number; QML never
// invents one, and never substitutes the last good reading.
//
// TIP and FIRE are therefore blank outside a bot-owned shot, and that is
// correct rather than a defect: they are engine predictions stamped by
// processHolding, and the alternative — deriving them here from fill and
// velocity — would put a second, non-authoritative countdown next to the meter
// claiming to be the decision the app actually made. FILL carries the box in
// the meantime (2026-08-04: the bot owns ~180 ms of a ~2500 ms meter, which is
// why gating all three rows on ownership read as "the HUD has no values").
//
// PERFORMANCE: this sits next to a 60 fps video preview, so it is built to cost
// almost nothing.
//   * The field set is FIXED — six Text items created once. No Repeater, no
//     model, no delegate churn, and no item ever changes visibility.
//   * The three CAPTION Texts are compile-time constants: they are laid out once
//     at creation and are never touched again. Only the three value Texts update.
//   * Every value binding targets `orion.meterHud*`, which is notified at most
//     30 Hz by meterHudChanged() and never by the per-tick input or detector
//     signals.
//   * The strings are already formatted in C++ and are only rebuilt when the
//     rendered digits change (fill is quantised to whole percent), so a steady
//     value assigns an identical QString and QQuickText skips relayout entirely.
//   * The whole item is `visible: false` between meters, so none of the above
//     is even rendered while there is nothing to read.
// Keep it that way: no JS number formatting, no per-frame object allocation, no
// animations or Behaviors on any property.
//
// PLACEMENT — beside the lock, not near it. The box is positioned by the page
// against the lock box's OUTERMOST DRAWN edge with a 6px gap and a shared top
// edge, so the two are read as a single mark. It prefers the right of the meter,
// flips to the left when it would overhang, and is clamped into the preview on
// both axes, because the meter can legitimately sit in any corner of the frame.
//
// LEGIBILITY: the court is white. The scrim alone is not enough at the moment a
// bright surface slides under the box, so every Text also carries a 1 px dark
// outline. Both are static properties, not per-frame work. The border and
// captions are the lock colour (accentColor, default Theme.meterLock — the
// bright blue as of 2026-08-06), so the box always reads as the second half of
// the lock mark under any user colour choice.
Rectangle {
    id: root

    // Tone-only inputs, resolved in C++ so QML never re-derives them. Neither
    // is a visibility gate — the page owns visibility, and it owns it on the
    // meter, not on the freshness of a timing prediction.
    //
    // TWO TONES, because the rows are not all provable at once (see the native
    // side's refreshLiveMeterTelemetry):
    //   `measured` — FILL is a current camera reading. True for the meter's
    //                whole life, so it is what takes the box out of its dimmed
    //                resting state: a live number is never shown at the opacity
    //                that means "nothing here is being proven".
    //   `live`     — the bot owns a shot, so TIP/FIRE are current predictions
    //                too. It is the only thing allowed to colour FIRE, because
    //                green on a row reading "--" would claim a healthy deadline
    //                that does not exist.
    property bool measured: false
    property bool live: false
    property bool commandLate: false
    // The lock's current stroke colour, passed in by the page rather than read
    // from Theme, so the box follows the user's chosen overlay colour (and the
    // RGB cycle) without this component knowing that either feature exists. It
    // changes at most a few times a second and drives exactly four properties.
    property color accentColor: Theme.meterLock

    // A floor, not a fixed width. Without it the panel would shrink to a stub
    // whenever all three values were placeholders and then jump wider the
    // instant a shot started, which reads as the overlay resizing rather than
    // filling in. 76 is the width of the widest realistic line ("FIRE -148ms"),
    // so in practice the box holds one size for a whole session. It is a
    // Math.max over an implicit size — no timer, no measurement.
    implicitWidth: Math.max(stack.implicitWidth + 10, 76)
    implicitHeight: stack.implicitHeight + 8
    // Square corners and the lock's own violet border, so the box reads as the
    // second half of the lock mark rather than as a floating UI card that
    // happens to be nearby. This is the reference's treatment exactly.
    radius: 0
    color: Theme.overlayScrim
    border.color: root.accentColor
    border.width: 1
    // Resting state is legible but recessive: a meter is on screen but NOTHING
    // in the box is currently being proven. It is an instrument at rest, not an
    // alert. Keyed on `measured` rather than `live` because FILL is live for the
    // meter's whole life while the shot is ~180 ms of it — dimming on `live`
    // would show a real, changing percentage at the opacity that means "no
    // reading". One property, changed at most at the 30 Hz cadence.
    opacity: (root.measured || root.live) ? 1.0 : 0.62

    // One caption/value line. Declared inline so the box stays one file; it owns
    // no state and adds no binding beyond the two it is given.
    //
    // NOTE: an inline component cannot reach the enclosing file's ids, so the
    // gutter metrics live here rather than on `root`. They are readonly
    // constants — three copies of two numbers, evaluated once at creation.
    component Field: Item {
        id: field
        property string caption: ""
        property string value: "--"
        property color valueColor: Theme.textPrimary
        property color captionColor: Theme.meterLock

        // Fixed caption gutter. This is what makes the panel a plate instead of
        // ragged rows: the captions differ in width, so without a shared
        // column every value would start at a different x and the eye would
        // have to re-find the digits on every line. 22 px holds the longest
        // caption of the 4-glyph set (FILL/FIRE) with its tracking — back
        // from the 32 px the 6-glyph "JITTER" briefly forced on 2026-08-06;
        // the reclaimed 10 px is exactly the decluttering the owner asked for.
        readonly property real captionColumn: 22
        readonly property real captionGap: 4

        // Derived from the VALUE only. The caption is inside the fixed gutter,
        // so a caption can never widen a line and the box's width depends on
        // exactly one thing: the longest number currently on screen.
        implicitWidth: captionColumn + captionGap + valueText.implicitWidth
        implicitHeight: valueText.implicitHeight

        // Captions carry the lock colour for two reasons. It ties the
        // box to the frame the way the reference does, and it replaces a faint
        // blue-grey that simply vanished on a bright court: at 8px there is not
        // enough ink for a low-contrast hue to survive a white baseline. The
        // accent also keeps the captions unmistakably NOT numbers, so the eye
        // lands on the white digits first. Monospace to match the values, so
        // caption and value sit on one rhythm instead of two.
        Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: field.captionColumn
            text: field.caption
            color: field.captionColor
            style: Text.Outline
            styleColor: Theme.overlayScrim
            font.family: Theme.fontMono
            font.pixelSize: 8
            font.weight: Font.DemiBold
            font.letterSpacing: 0.3
        }
        Text {
            id: valueText
            anchors.left: parent.left
            anchors.leftMargin: field.captionColumn + field.captionGap
            anchors.verticalCenter: parent.verticalCenter
            text: field.value
            color: field.valueColor
            style: Text.Outline
            styleColor: Theme.overlayScrim
            font.family: Theme.fontMono
            font.pixelSize: 11
            font.weight: Font.Bold
        }
    }

    // Anchored to the top-left rather than filled or centred, so the Column's
    // implicit size is derived purely from its children and never from the
    // Rectangle it also sizes. That keeps the size relationship strictly
    // one-directional (column -> box) — the padding below matches implicitWidth/
    // implicitHeight above, so the stack is exactly centred without the box's
    // own geometry feeding back into it.
    Column {
        id: stack
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.leftMargin: 5
        anchors.topMargin: 4
        // 1px, not a design-system gap. Three baselines this close still read as
        // separate lines because each one starts with a violet caption, and the
        // height saved is height that would otherwise cover the court.
        spacing: 1

        // --- the eye ------------------------------------------------------
        // The only row gated on the DETECTOR rather than on a shot, so it is
        // the one that is alive for as long as the box is on screen.
        Field {
            objectName: "meterHudFillRow"
            captionColor: root.accentColor
            caption: "FILL"
            value: orion.meterHudFillLine
            valueColor: root.measured ? Theme.textSecondary : Theme.textMuted
        }

        // --- the prediction ------------------------------------------------
        Field {
            objectName: "meterHudTipRow"
            captionColor: root.accentColor
            caption: "TIP"
            value: orion.meterHudTipLine
            // Muted with the placeholder: an unproven row must not be presented
            // in the same ink as a measured one.
            valueColor: root.live ? Theme.textPrimary : Theme.textMuted
        }

        // --- the actuation deadline (the only field that carries an alarm) ---
        Field {
            objectName: "meterHudFireRow"
            captionColor: root.accentColor
            caption: "FIRE"
            value: orion.meterHudFireLine
            // Muted while parked: green would claim a healthy deadline that is
            // not currently being measured. An ARMED countdown publishes
            // through the same line with `live` true, so it is never muted
            // while something is genuinely scheduled.
            valueColor: !root.live ? Theme.textMuted
                        : root.commandLate ? Theme.danger : Theme.success
        }

        // COURT/JITTER rows removed 2026-08-06 (owner: three values only, the
        // ones the bot uses). See the header comment for why they were also
        // the static feel. The RTT/court machinery underneath is untouched.
    }
}
