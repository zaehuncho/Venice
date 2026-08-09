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
    // The lock colour is BRIGHT BLUE — the owner's chosen default look
    // (2026-08-06, replacing the original reference violet-magenta). It sits
    // ~210 degrees off the meter's saturated red, so the stroke can never be
    // confused with the bar it is tracing. On a floodlit white court a bright
    // blue alone is only ~2.6:1 against white, which is exactly why the lock
    // is never drawn as a bare stroke: the dark keylines below bracket it on
    // both sides and carry the edge on a blown-out baseline.
    //
    // This constant is the QML-side fallback/companion default only. The
    // runtime stroke is `orion.meterOverlayDrawColor`, whose factory default
    // (AppConfigData::kMeterOverlayDefaultColor) must stay in lockstep with
    // this value.
    readonly property color meterLock: "#00A8FF"
    // Keyline that brackets the lock stroke on BOTH sides. A bright stroke
    // alone fails in exactly the two places it matters: it disappears into a
    // blown-out white highlight, and it goes muddy where it touches the red
    // bar. A dark hairline inside and outside guarantees an edge against any
    // backdrop without widening the stroke itself. Near-black with a blue bias
    // so it reads as part of the same mark and not as a separate grey box.
    readonly property color meterLockKeyline: "#001626"

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

    // ---- shape ----
    readonly property int radiusCard: 14
    readonly property int radiusControl: 10
    readonly property int radiusPill: 15

    // ---- spacing scale ----
    readonly property int spaceXs: 4
    readonly property int spaceSm: 8
    readonly property int spaceMd: 12
    readonly property int spaceLg: 16
    readonly property int spaceXl: 24

    // ---- type ramp ----
    readonly property string fontUi: "Segoe UI Variable"
    readonly property string fontMono: "Cascadia Mono"
    readonly property int fontDisplay: 24
    readonly property int fontTitle: 15
    readonly property int fontBody: 13
    readonly property int fontCaption: 11

    // ---- motion (deliberate, fast: ~150-250ms curves) ----
    readonly property int motionFast: 110
    readonly property int motionBase: 160
    readonly property int motionSlow: 250
}
