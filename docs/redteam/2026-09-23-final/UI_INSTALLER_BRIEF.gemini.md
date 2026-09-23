# Launcher UI & Custom Installer Design Brief (Gemini)
**Target:** Venice (NBA 2K27 Jump-Shot Timing Tool for PS5)  
**Date:** 2026-09-23 (Final Launch Review)  
**Goal:** Define the visual and UX overhaul to make the Launcher UI look substantially more premium and align the Inno Setup installer with Venice's brand aesthetic.

---

## 1. Brand Identity & Visual Language

### 1.1 The Website Baseline (`website/public/index.html` & `styles.css`)
The website establishes Venice's visual identity:
- **Atmosphere:** Deep, luxurious obsidian black. It does not feel like flat software; it feels like an elite performance instrument.
- **Background:** Base `#06080c` with two soft, breathing radial glow sources:
  - High center: Electric Sapphire (`rgba(38, 112, 255, 0.22)`)
  - Top right: Soft Ice Glow (`rgba(90, 179, 251, 0.11)`)
  - Overlay: Micro-texture fractal noise at 5% opacity for a tactile, matte-glass feel.
- **Surfaces & Elevation:**
  - Base canvas: `#06080c`
  - Cards & Panels: `#0b0f16` (subtle elevation, not high-contrast blue)
  - Insets & Fields: `#05070a`
- **Borders & Dividers:**
  - Ultra-delicate translucent white: `rgba(255, 255, 255, 0.09)`
  - Elevated/Hover borders: `rgba(255, 255, 255, 0.16)`
- **Color Palette:**
  - Primary Accent: `#1c67dd` (electric cobalt) / hover `#2470e4`
  - Light Accent: `#6fb4ff` / `#9fd0ff` (ice sapphire highlights)
  - Success / Green: `#8be3bb` (soft mint/sage, not harsh neon)
  - Warning / Amber: `#f59e0b` / dim `#332712`
  - Danger / Red: `#ffc2c2` / dim `#351a1e`
  - Text Primary: `#f2f5f9` (crisp ice white)
  - Text Secondary: `#a7b2bf` (refined cool grey)
  - Text Muted: `#8d99a7`
- **Typography:**
  - Custom geometric typeface: **Geist** (`font-weight: 400, 600, 700`) with tight tracking (`letter-spacing: -0.02em`).
  - Monospace: Clean code font (`Cascadia Mono` / `SF Mono`).
- **Shapes:** Soft rounded geometry (`12px` card radius, `8px` controls, `20px` pill badges).

---

### 1.2 Where the Launcher Currently Drifts (`Theme.qml`)

| Visual Element | Website Brand (Target) | Current Launcher (`Theme.qml`) | Brand Drift & Problem |
| :--- | :--- | :--- | :--- |
| **Surface Hue** | Neutral Obsidian (`#06080c`, `#0b0f16`) | Heavily Tinted Navy (`#020509`, `#081221`, `#0C1A2D`) | Launcher feels like a 2010s blue gaming client rather than modern obsidian luxury. |
| **Borders** | Translucent Frosted White (`rgba(255,255,255,0.09)`) | Opaque Saturated Cyan/Blue (`#12263F`, `#21466F`) | Heavy blue outlines look boxed-in, rigid, and dated. |
| **Backdrop** | Ambient radial glow + matte grain | `VeniceBackdrop.qml`: Animated twinkling starfield | Retro sci-fi starfield clashes with the website’s minimalist, high-end athletic tech aesthetic. |
| **Typography** | Geist (`letter-spacing: -0.02em`) | Standard Windows `Segoe UI Variable` | Generic system look; lacks the branded personality of the site. |
| **Card Sizing** | Balanced spacing, generous breathing room | High vertical density, crowded right-rail, small fixed heights | Cards feel packed together; text frequently truncates or requires nested scrollbars. |

---

## 2. Top 10 Launcher UI Improvements (Ranked by Customer Impact)

### 1. Live Page Right-Rail Consolidation & Card De-Cluttering
- **Files:** `native_orion/qml/pages/RemotePlayPage.qml`, `native_orion/qml/components/ShotLeadCard.qml`, `TipTimingCard.qml`, `MeterConfigPanel.qml`
- **The Problem:** The right-hand column on `RemotePlayPage.qml` stacks 5 vertical cards:
  1. *Meter Detection* (Style/Color/Detector)
  2. *Shot Lead* (Slider + Conflict banner + Auto-Trim + Auto-Seed + Guided Calibration)
  3. *Tip Timing* (Aim adjustments + Divergence banner)
  4. *Rhythm Timing*
  5. *Live Activity Log*
  
  On standard 1080p monitors at 125% Windows scaling (effective 864px height), this column overflows by over 600 pixels. Customers are forced to scroll up and down constantly while playing just to see if their shot was detected or to adjust calibration.
- **The Redesign:**
  - **Single Unified "Shot Timing" Card:** Merge *Shot Lead* and *Tip Timing* into one cohesive card. Keep Shot Lead front and center; tuck Tip Timing into an expandable "Jumpshot Aim (Advanced)" drawer.
  - **Move Activity Log to Bottom Drawer:** The raw activity log should live in a collapsible bottom tray or dedicated Diagnostics modal, freeing up the entire right sidebar for gameplay controls.

---

### 2. Live Feed Empty State & Staging Checklist
- **Files:** `native_orion/qml/pages/RemotePlayPage.qml:411-432`
- **The Problem:** When Venice is opened before connecting, the video area is an empty dark rectangle with centered text:
  > *"No stream"*  
  > *"Press Connect to start"*  
  It provides zero feedback on whether the capture card is detected, whether the controller is plugged in, or whether the PS5 is reachable.
- **The Redesign:**
  - Transform the placeholder into a **Pre-Flight Hardware Staging Area**:
    ```
    +--------------------------------------------------------+
    |                    READY TO CONNECT                    |
    |                                                        |
    |   [✓] Elgato HD60 X (1080p60 HDMI Ready)              |
    |   [✓] DualSense Controller (USB Connected)             |
    |   [✓] PS5 Console (1920x1080 · Ready to link)          |
    |                                                        |
    |               [  CONNECT TO PS5  ]                     |
    +--------------------------------------------------------+
    ```
  - If any item is missing (e.g. controller not plugged in, or capture card held by OBS), show a warning badge with a direct one-click fix right in the box.

---

### 3. Top Banner Arbitration (End Stacking & Overlap)
- **Files:** `native_orion/qml/pages/RemotePlayPage.qml:545-760`
- **The Problem:** Three different status banners compete for the exact same top-center position (`anchors.top: parent.top, anchors.topMargin: 16`):
  1. `motdBanner` (Server announcements / maintenance)
  2. `leaseBanner` (`SHOTS PAUSED` — license lease reconnecting)
  3. `inputDeadBanner` (`CONTROLLER INPUT IS NOT REACHING THE CONSOLE`)
  
  When multiple conditions trigger, they either awkwardly overlap or flash between states, obstructing the top of the NBA 2K scorebug.
- **The Redesign:**
  - Establish a strict single-slot **Notification Toast Queue**:
    - Highest Priority: `inputDead` (Critical safety fault)
    - Medium Priority: `leaseBanner` (Automation paused)
    - Low Priority: `motdBanner` (Informational notice)
  - Render as a sleek, non-intrusive top-center floating pill that smoothly transitions between states without jumping layout.

---

### 4. Guided Lead Calibration as the Hero Flow
- **Files:** `native_orion/qml/components/ShotLeadCard.qml:298-410`
- **The Problem:** For a brand-new customer, finding their Shot Lead is the #1 setup task. Currently, the card displays a confusing 1..100 slider at the top, while the guided calibration flow (*"Calibrate my lead"*) is buried in a small sub-panel at the bottom.
- **The Redesign:**
  - When an install has no calibrated lead (`!orion.actuationLeadUserSet`), **the Guided Calibration UI should be the hero state of the card**:
    ```
    +-------------------------------------------------------+
    | SHOT LEAD CALIBRATION                                 |
    | Take 10 open shots in 2K Shoot-Around to tune Venice  |
    |                                                       |
    |                [ START CALIBRATION ]                  |
    +-------------------------------------------------------+
    ```
  - Once calibrated, the card collapses into its compact locked state showing the calibrated value with an inline `[Fine-Tune Slider]` toggle.

---

### 5. Demote or Drawer-Collapse "Tip Timing"
- **Files:** `native_orion/qml/components/TipTimingCard.qml`
- **The Problem:** Venice autonomous machine learning automatically adapts to jumpshot animation timing (`recordPhaseConstantSample`). Presenting both *Shot Lead* and *Tip Timing* as primary cards causes users to tweak both knobs in opposite directions, hopelessly de-calibrating their shots.
- **The Redesign:**
  - Demote *Tip Timing* into an expandable "Advanced Timing" section.
  - In normal operation, display a simple badge on the Shot Lead card:
    `"Jumpshot Timing: Auto-Learned (310 ms) [Unlock]"`

---

### 6. Clean Meter Style Display (Remove Fake Dropdown)
- **Files:** `native_orion/qml/components/MeterConfigPanel.qml:70-105`
- **The Problem:** `meterStyleCombo` displays a dropdown box with a single option (`Arrow2`) with `enabled: false` and `opacity: 0.55` (because Pill was withdrawn). It looks like a disabled or broken control.
- **The Redesign:**
  - Replace the fake dropdown with a crisp, branded status badge:
    ```
    [ Style: Arrow2 ]   [ Color: Cyan ▾ ]
    ```
  - Style stays locked with a subtle info tooltip: *"Venice is optimized for NBA 2K27's Arrow2 meter."*

---

### 7. Modern Obsidian Theme Palette (`Theme.qml`)
- **Files:** `native_orion/qml/Theme.qml`, `native_orion/qml/components/VeniceBackdrop.qml`
- **The Problem:** The current color tokens use high-saturation navy blues and cyan accents.
- **The Redesign:**
  - Align `Theme.qml` with the website's exact color tokens:
    ```qml
    readonly property color bgShell:      "#06080C"  // Deep neutral obsidian
    readonly property color bgCard:       "#0B0F16"  // Subtle card elevation
    readonly property color bgInset:      "#05070A"  // Deep inset
    readonly property color borderSoft:   Qt.rgba(1, 1, 1, 0.08)  // Translucent frosted line
    readonly property color borderStrong: Qt.rgba(1, 1, 1, 0.16)
    readonly property color brandAccent:  "#1C67DD"  // Electric Cobalt
    readonly property color accentGlow:   "#5AB3FB"  // Ice highlight
    ```
  - Replace `VeniceBackdrop.qml`'s retro starfield with a subtle, hardware-accelerated radial blue bloom matching the website header.

---

### 8. Sidebar Visual Rhythm & Account Pill
- **Files:** `native_orion/qml/components/Sidebar.qml`
- **The Problem:** The sidebar mixes navigation icons (Live, Setup, Updates), Discord account status, and profile badges in an uneven vertical stack with awkward spacing.
- **The Redesign:**
  - Group into 3 clean vertical zones:
    1. **Top:** Minimal Venice V logo + Wordmark.
    2. **Center:** Clean nav tabs with glowing active pill indicators (Live, Setup, Patch Notes).
    3. **Bottom:** Compact user profile card showing Discord avatar, username, and subscription status (`Active · Beta`).

---

### 9. Unified Top Status Bar
- **Files:** `native_orion/qml/components/TopStatusBar.qml`, `native_orion/qml/pages/RemotePlayPage.qml:440-526`
- **The Problem:** Telemetry is scattered: Top bar shows API connection; video preview overlays FPS, LIVE badge, and PRESSED indicator in three different corners.
- **The Redesign:**
  - Create a single, polished **Status Cluster** pinned to the top-right of the preview:
    ```
    [ ● LIVE · 60 FPS · DUALSENSE READY ]
    ```
  - Single glance gives complete reassurance that video, controller, and bot are operational.

---

### 10. Typography & Font Hierarchy Overhaul
- **Files:** `native_orion/qml/Theme.qml:120-130`
- **The Problem:** Standard Segoe UI Variable renders thin and clinical on dark backgrounds without custom font metrics.
- **The Redesign:**
  - Bundle **Geist** (`Geist-Regular.woff2`, `Geist-SemiBold.woff2`) directly into Qt resources (`qrc:/fonts/`).
  - Set `font.family: "Geist"` as the primary UI font across all titles, buttons, and labels.
  - Implement tighter letter tracking on headers (`letterSpacing: -0.5`) to match the website’s editorial tech aesthetic.

---

## 3. Custom Installer Design Brief (`installer/orion.iss`)

### 3.1 Overview & Architecture Options
The installer is the customer’s very first touchpoint with Venice. A generic light-gray Win32 wizard feels like legacy enterprise software and damages brand trust.

#### Implementation Architecture:
1. **Targeting Inno Setup's Built-in Modern Wizard (`WizardStyle=modern`):**
   - Inno Setup natively supports custom sidebar art (`WizardImageFile`, 164×314 px) and header art (`WizardSmallImageFile`, 55×55 px).
   - Custom branding is achieved by designing pixel-perfect, dark-canvas bitmap assets in the exact `#06080c` obsidian palette, creating a cohesive visual frame around the wizard pages.
2. **In-Wizard Dark Theming (Pascal Script `[Code]`):**
   - Inno Setup’s standard window surface is light. By hooking `InitializeWizard` and calling Windows `SetWindowTheme` (or utilizing an Inno dark-mode VCL style like `ISSkin` / `VclStylesInno`), the wizard window, edit controls, and buttons can render in full dark mode.
3. **Future Evolution (Post-Beta):**
   - A frameless web-based bootstrapper (WebView2 or lightweight Qt Quick installer) that delivers a 100% custom fluid animated setup experience.

---

### 3.2 Screen-by-Screen Installer Experience

```
+-----------------------------------------------------------------------------+
|  [ V ]  VENICE SETUP                                            [ _ ] [ X ] |
+-----------------------------------------------------------------------------+
|                  |                                                          |
|                  |   Welcome to Venice                                      |
|                  |   Jump-shot timing for NBA 2K27 on PS5                   |
|                  |                                                          |
|    [ BRANDED     |   Pre-flight checklist before installing:                |
|      SIDEBAR     |                                                          |
|      GRAPHIC:    |   [✓] DualSense or PS5 controller plugged into PC over USB |
|      Obsidian    |   [✓] 60 Hz capture card or Remote Play on same network   |
|      Canvas +    |   [✓] Close OBS Studio or other capture applications     |
|      Glowing     |                                                          |
|      Sapphire    |   Notice: Windows SmartScreen may show an initial        |
|      Venice V    |   unrecognized app alert. Click "More info" then         |
|      Mark ]      |   "Run anyway" to proceed with the beta build.           |
|                  |                                                          |
|                  |                                                          |
|                  |                                  [  Next >  ] [ Cancel ] |
+-----------------------------------------------------------------------------+
```

#### Screen 1: Welcome Page (`WelcomeLabel`)
- **Visuals:**
  - Sidebar: Full-bleed vertical banner (`venice-wizard.bmp`) rendered in obsidian `#06080c` with the glowing sapphire Venice V mark and soft grain.
  - Main Panel: Clean dark typography with crisp white header.
- **Copy:**
  - Title: *"Welcome to Venice"*
  - Subtitle: *"Jump-shot timing for NBA 2K27 on PS5"*
  - Hardware Checklist:
    - *"Keep your controller plugged into this PC over USB"*
    - *"Ensure your PS5 is on the same local network"*
    - *"Close OBS or other capture software before starting"*
  - SmartScreen Callout:
    - *"Windows SmartScreen Notice: Click **More info** then **Run anyway** if prompted."*

#### Screen 2: Legal Agreement & Safety Notice
- **Visuals:** High-contrast, clean scroll box containing terms.
- **Copy:**
  - Clear section headers: *Disclaimer of Affiliation* (not affiliated with 2K/Take-Two/Sony), *Personal Use Rules*, *Digital Access Terms*.
  - Radio button selection: `(o) I accept the agreement` / `( ) I do not accept the agreement`.

#### Screen 3: Destination Location (`DefaultDirName`)
- **Default:** `{autopf}\Venice` (`C:\Program Files\Venice`).
- **Visuals:** Clean path picker with folder icon.
- **Callout Note:**
  - *"Recommended: Keep the default installation path to ensure secure system driver permissions."*
- **Options:**
  - `[X] Create a desktop shortcut` (Checked by default).

#### Screen 4: Installation Progress
- **Visuals:**
  - Custom branded progress bar in Electric Cobalt (`#1C67DD`).
  - Active sub-task status label updating in real time:
    - *"Extracting Venice binaries…"*
    - *"Installing ViGEmBus virtual controller driver…"*
    - *"Installing HidHide filter driver…"*
    - *"Configuring Venice packet service…"*
- **Fail-Safe UX:**
  - If a driver requires a reboot, avoid throwing raw error codes (`DRV-02`). Remember the state and display a polite explanation on the final screen.

#### Screen 5: Finish Page (`FinishedLabel`)
- **Visuals:**
  - Large glowing checkmark icon in soft mint green (`#8BE3BB`).
  - Header: *"Venice is installed and ready"*
- **Copy:**
  - *"Setup has finished installing Venice on your PC."*
  - Next Steps:
    1. *"Launch Venice below."*
    2. *"Sign in with Discord at **zaeorion.com/connect** to retrieve your one-time unlock code."*
    3. *"Follow the in-app guide to pair your PS5 and calibrate your shot lead."*
- **Checkboxes:**
  - `[X] Launch Venice now` (Checked by default).
  - Button: `[ Finish ]` in primary blue styling.

---
*Design brief complete. Written to `docs/redteam/2026-09-23-final/UI_INSTALLER_BRIEF.gemini.md`.*
