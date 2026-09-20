pragma Singleton
import QtQuick

// Venice customer-facing visual system. Runtime and IPC identifiers remain Orion,
// but product copy and presentation are centralized here so they cannot drift.
QtObject {
    id: theme

    readonly property string productName: "Venice"
    readonly property string productMark: "VENICE"
    readonly property string productTagline: "Precision timing"

    // ---- brand accent ----------------------------------------------------
    readonly property color brandAccent: "#2D7DFF"
    readonly property color orionDefaultAccent: brandAccent
    property color accent: brandAccent
    readonly property color accentHover: Qt.lighter(accent, 1.18)
    readonly property color accentPressed: Qt.darker(accent, 1.25)
    readonly property color accentBorder: Qt.lighter(accent, 1.35)
    readonly property color accentSoft: Qt.rgba(accent.r, accent.g, accent.b, 0.16)
    readonly property color accentFaint: Qt.rgba(accent.r, accent.g, accent.b, 0.07)
    readonly property color accentGlow: Qt.rgba(accent.r, accent.g, accent.b, 0.32)

    // ---- surfaces: black canvas, dark-blue depth -------------------------
    readonly property color bgShell: "#020509"
    readonly property color bgSidebar: "#050B14"
    readonly property color bgCard: "#081221"
    readonly property color bgCardHover: "#0C1A2D"
    readonly property color bgField: "#02070E"
    readonly property color bgInset: "#050C16"
    readonly property color bgCardTop: "#091526"
    readonly property color bgCardBottom: "#050C16"

    // ---- borders ----
    readonly property color borderSoft: "#12263F"
    readonly property color borderStrong: "#21466F"
    readonly property color hairline: "#0E1D31"
    readonly property color hairlineLight: "#55224568"
    readonly property color focusRing: "#69A8FF"

    // ---- elevated/overlay surfaces (modals, popups, scrim, icon tiles) ----
    // Added so dialogs/scrollbars/icon tiles stop hand-coding one-off hexes.
    readonly property color modalSurface: "#070F1C"
    readonly property color modalBorder: "#1A3658"
    readonly property color overlayScrim: "#C0020509"
    readonly property color iconTileBg: "#06101D"
    readonly property color scrollbarThumb: "#244466"
    readonly property color scrollbarThumbActive: "#4C78A8"

    // ---- text ----
    readonly property color textPrimary: "#F4F8FC"
    readonly property color textSecondary: "#B9C6D7"
    readonly property color textMuted: "#74849A"
    readonly property color textFaint: "#4D6078"

    // High-contrast detector overlay tokens. Geometry and detector inputs never
    // depend on the UI theme.
    //
    // The lock colour is dodger blue per the 2026-09-14 owner directive
    // ("blue, visible"), replacing the 2026-08-31 magenta. The lock is never
    // drawn as a bare stroke: the dark keylines below bracket it on both sides
    // and carry the edge on a blown-out baseline without covering the meter.
    //
    // This constant is the QML-side fallback/companion default only. The
    // runtime stroke is `orion.meterOverlayDrawColor`, whose factory default
    // (AppConfigData::kMeterOverlayDefaultColor) must stay in lockstep with
    // this value.
    readonly property color meterLock: "#1E90FF"
    // Keyline that brackets the lock stroke on BOTH sides. A bright stroke
    // alone fails in exactly the two places it matters: it disappears into a
    // blown-out white highlight, and it goes muddy where it touches the red
    // bar. A dark hairline inside and outside guarantees an edge against any
    // backdrop without widening the stroke itself. Near-black with a navy bias
    // so it reads as part of the same mark and not as a separate grey box.
    readonly property color meterLockKeyline: "#06182F"

    // ---- status ----
    readonly property color success: "#22C55E"
    readonly property color successDim: "#143322"
    readonly property color successBorder: "#1E6A3B"
    readonly property color warning: "#F59E0B"
    readonly property color warningDim: "#332712"
    readonly property color warningBorder: "#74510D"
    readonly property color danger: "#EF4444"
    readonly property color dangerDim: "#351A1E"
    readonly property color dangerBorder: "#74303A"

    // Text drawn ON an accent/danger/success fill (buttons, selected chips, selection highlight).
    readonly property color textOnAccent: "#FFFFFF"

    // Log-viewer level colours: brighter than the status set so they read on mono text at 11-12px.
    readonly property color logErr: "#FF6B6B"
    readonly property color logWarn: "#FFC857"
    readonly property color logOk: "#5BE39B"
    readonly property color logInfo: "#6FD3E0"
    readonly property color logText: "#AEB6C2"

    // Drop halo drawn one pixel below cards and gate dialogs (soft elevation
    // without the effects module). Shared so every raised surface sits the same.
    readonly property color shadowHalo: "#06090E"

    // ---- shape ----
    readonly property int radiusCard: 14
    readonly property int radiusControl: 10
    // Small chips, tab pills, and compact inline buttons (copy/paste/filter).
    readonly property int radiusChip: 7
    readonly property int radiusPill: 15

    // Standard height for a row control: combos, text fields, and the buttons
    // that sit beside them. One value so a row of mixed controls stays level.
    readonly property int controlHeight: 38

    // ---- spacing scale ----
    readonly property int spaceXs: 4
    readonly property int spaceSm: 8
    readonly property int spaceMd: 12
    readonly property int spaceLg: 16
    readonly property int spaceXl: 24

    // ---- type ramp ----
    readonly property string fontUi: "Segoe UI Variable"
    readonly property string fontMono: "Cascadia Mono"
    readonly property int fontDisplay: 24   // page title
    readonly property int fontHeading: 20   // gate/dialog title, hero line
    readonly property int fontTitle: 15
    readonly property int fontBody: 13
    readonly property int fontSmall: 12     // card subtitle, secondary rows, hints
    readonly property int fontCaption: 11
    readonly property int fontMicro: 10     // uppercase eyebrow labels

    // ---- motion (deliberate, fast: ~150-250ms curves) ----
    readonly property int motionFast: 110
    readonly property int motionBase: 160
    readonly property int motionSlow: 250
}
