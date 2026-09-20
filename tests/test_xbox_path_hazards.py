"""Xbox-path hazards — pinned so docs/XBOX_PATH_DESIGN_2026-09-19.md cannot rot.

[XBOX PATH 2026-09-19] The confirmed Xbox architecture rides Microsoft's own Xbox Remote
Play app: WGC-capture its window, inject the shot through a ViGEm X360 virtual pad, HidHide
the physical pad so the app sees one merged controller. The design document costs that work
against what the tree looks like TODAY.

These tests assert the *hazards* that design depends on, by content rather than by line
number. Each one failing means an Xbox-relevant assumption changed, and the right response
is to re-read the design doc -- not to edit the test until it passes.

None of these assert desired behaviour. They assert "the thing the design says is true is
still true", so a refactor that quietly removes a Sony-only gate, or quietly 'fixes' the
DualSense-ordered WinMM table without a family fence, is surfaced at the moment it lands.

Blocked-file note: every file inspected here is READ ONLY for this test. Nothing is patched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "native_orion" / "src"

DESIGN_DOC = "docs/XBOX_PATH_DESIGN_2026-09-19.md"


def _read(relative: str) -> str:
    path = REPO_ROOT / relative
    if not path.exists():
        pytest.skip(f"{relative} not present in this checkout")
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Input side
# --------------------------------------------------------------------------- #

def test_rawinput_identity_is_still_gated_to_the_sony_vid() -> None:
    """The primary pad reader drops every non-VID_054C report before decode.

    This is hazard #1 in the design: an Xbox pad is invisible to RawInput, so XInput
    (selector priority 170) is the Xbox reader and WinMM (120) is the trap below it.
    If this gate ever disappears, the Xbox reader story changes and section 5.2 of
    """ + DESIGN_DOC + " is wrong."
    text = _read("native_orion/src/OrionAppController.cpp")
    assert 'if (!path.contains(QStringLiteral("VID_054C")))' in text, (
        "the RawInput Sony-VID gate moved or changed shape; re-read " + DESIGN_DOC
    )
    assert 'raw.physicalSony = rawInputDevicePath_.contains(QStringLiteral("VID_054C")' in text, (
        "physicalSony is no longer derived from the Sony VID; the selector's +300 tier "
        "(ControllerDeviceSelector.cpp:22-24) may now admit an Xbox pad -- re-read " + DESIGN_DOC
    )


def test_device_selector_still_ranks_sony_rawinput_above_xinput() -> None:
    """Sony+RawInput outranks XInput, which outranks WinMM.

    The Xbox plan needs an equal top tier for a live Xbox pad on XInput; until that
    exists, an Xbox pad can fall through to the DualSense-ordered WinMM table.
    """
    text = _read("native_orion/src/ControllerDeviceSelector.cpp")
    sony = re.search(r"physicalSony.*?\n.*?(\d{3})", text, re.S)
    assert "300" in text and "170" in text and "120" in text, (
        "the selector priority tiers changed; section 5.1 of " + DESIGN_DOC + " is stale"
    )
    assert sony is not None
    assert "physicalXbox" not in text, (
        "an Xbox identity tier now exists -- Phase 1 of " + DESIGN_DOC + " has landed; "
        "update the doc and turn this into a positive assertion"
    )


def test_winmm_button_table_is_still_dualsense_ordered() -> None:
    """btn1 maps to XINPUT_GAMEPAD_X (= square()), which on an Xbox pad is the A button.

    This is the single most dangerous Xbox hazard: under WinMM, pressing A on an Xbox
    controller arms the shot path. The design's recommendation is to FENCE WinMM off for
    the Xbox family rather than re-order the table, because the table is correct for the
    pad it was written for.
    """
    text = _read("native_orion/src/WinMmButtonMapping.h")
    assert "if (dwButtons & 0x0001u) sample.buttons |= XINPUT_GAMEPAD_X;" in text, (
        "the WinMM button table changed. If it was re-ordered for Xbox, confirm a "
        "pad-family fence landed with it -- otherwise DualSense users now shoot on the "
        "wrong button. See section 5.2 of " + DESIGN_DOC
    )
    # Triggers are read as digital BUTTON bits here, not axes -- on Xbox those bits are
    # View and Menu, so a menu press reads as a fully pulled trigger.
    assert "sample.l2 = (dwButtons & 0x0040u) ? 255 : 0;" in text
    assert "sample.r2 = (dwButtons & 0x0080u) ? 255 : 0;" in text


def test_winmm_axes_still_assume_the_dualsense_directinput_layout() -> None:
    """dwZpos -> rightStickX, dwRpos -> rightStickY, dwUpos never read.

    On an Xbox pad Z is the COMBINED LT/RT axis, so a trigger pull becomes a phantom
    right-stick deflection and every stick-based shot gesture misreads.
    """
    text = _read("native_orion/src/OrionAppController.cpp")
    assert "normalizeJoyAxis(joy.dwZpos" in text, "WinMM Z-axis assignment moved"
    assert "normalizeJoyAxis(joy.dwRpos" in text, "WinMM R-axis assignment moved"
    assert "dwUpos" not in text, (
        "dwUpos is now read -- the WinMM axis layout changed; section 5.2 of "
        + DESIGN_DOC + " is stale"
    )


def test_sdl_ignore_list_is_still_sony_only() -> None:
    """The Chiaki child is told to ignore Sony pads only.

    On a PS5 session with an Xbox pad attached, the fork's SDL would open that pad
    directly and race the Orion input route. Phase 1 adds Microsoft's VID (0x045e).
    """
    text = _read("native_orion/src/RemotePlaySession.cpp")
    assert "SDL_GAMECONTROLLER_IGNORE_DEVICES" in text
    assert "0x054c/0x0ce6" in text, "the SDL ignore-list changed shape"
    assert "0x045e" not in text, (
        "Microsoft VIDs are now in the SDL ignore-list -- Phase 1 of " + DESIGN_DOC
        + " has landed; update the doc and make this a positive assertion"
    )


# --------------------------------------------------------------------------- #
# Output / capture side
# --------------------------------------------------------------------------- #

def test_vigem_x360_is_already_the_preferred_virtual_target() -> None:
    """The Xbox output route needs no new backend: X360/XUSB is already the default."""
    header = _read("native_orion/src/VirtualController.h")
    assert "preferXusb = true" in header, (
        "ViGEm no longer prefers the X360 target; the Xbox output costing in "
        + DESIGN_DOC + " assumed it does"
    )
    source = _read("native_orion/src/VirtualController.cpp")
    assert "vigem_target_x360_alloc" in source
    assert "vigem_target_x360_update" in source


def test_vigem_xusb_route_enumerant_exists() -> None:
    """LatencyControllerRoute::VigemXusb is already wired end to end."""
    types = _read("native_orion/src/OrionTypes.h")
    assert "VigemXusb" in types, (
        "the ViGEm XUSB route enumerant vanished; the '0 days of plumbing' claim in "
        + DESIGN_DOC + " no longer holds"
    )
    policy = _read("native_orion/src/PreciseFirePolicy.h")
    assert "VigemXusb" in policy


def test_xbox_external_route_is_separate_from_ps5() -> None:
    controller = _read("native_orion/src/OrionAppController.cpp")
    assert "switchRemotePlayConsole(data, v)" in controller
    assert "!isXboxRemotePlay(config_.data())" in controller
    session = _read("native_orion/src/RemotePlaySession.cpp")
    assert 'xbox ? QStringLiteral("external") : QStringLiteral("chiaki")' in session
    assert 'xbox ? QStringLiteral("wgc") : config_.videoSource' in session
    assert '!xbox && sidecarShouldAutoLaunchClient(previewMode_)' in session
    assert '"Xbox beta"' in _read("docs/XBOX_PATH_DESIGN_2026-09-19.md")


def test_wgc_backend_can_target_a_window_by_hwnd_or_title() -> None:
    """The Xbox capture path is WGC against Microsoft's Remote Play window.

    GDI PrintWindow/BitBlt -- what the Aim++ prototype uses -- commonly returns black on
    a hardware-accelerated window. The design mandates WGC; this pins that the backend
    we intend to reuse still takes a window target.
    """
    text = _read("wgc_backend.py")
    assert "class WGCCaptureBackend" in text
    assert "window_hwnd" in text and "window_name" in text, (
        "WGCCaptureBackend no longer accepts a window target; section 3 of "
        + DESIGN_DOC + " is stale"
    )
    assert "from windows_capture import WindowsCapture" in text, (
        "the WGC backend no longer uses the windows-capture package"
    )


def test_window_locator_does_not_yet_know_the_xbox_titles() -> None:
    """The locator matches Chiaki/OrionStream only. Phase 3 adds the Xbox titles.

    Kept as a negative assertion deliberately: when it flips, the Xbox window locator has
    landed and the doc's Phase 3 estimate should be updated rather than re-derived.
    """
    text = _read("remote_play_client.py")
    assert "_SAFE_TITLE_MARKERS" in text
    assert '"chiaki"' in text
    lowered = text.lower()
    for marker in ("xbox remote play", "xbox game streaming", "xbox console companion"):
        assert marker not in lowered, (
            f"the locator now knows {marker!r} -- Phase 3 of {DESIGN_DOC} has started; "
            "update the doc and convert this to a positive assertion"
        )


# --------------------------------------------------------------------------- #
# Reusable assets the plan depends on
# --------------------------------------------------------------------------- #

def test_hidhide_and_the_merge_hub_reference_still_exist() -> None:
    """HidHide is the one genuinely new C++ component; its port source must be here."""
    text = _read("virtual_controller.py")
    assert "class HidHideClient" in text, (
        "the HidHide reference implementation is gone -- it is the port source for the "
        "Xbox input merge (section 4 of " + DESIGN_DOC + ")"
    )
    assert "\\\\\\\\.\\\\HidHide" in text or "HidHide" in text
    assert "class ControllerIOHub" in text
    assert "class PhysicalControllerReader" in text
    assert "_VIGEM_TARGET_TYPE_XBOX360" in text


def test_the_design_document_is_present() -> None:
    """These tests reference it by name in every failure message."""
    doc = REPO_ROOT / DESIGN_DOC
    assert doc.exists(), f"{DESIGN_DOC} is missing; these hazard pins have no referent"
    body = doc.read_text(encoding="utf-8", errors="replace")
    for required in ("Xbox Remote Play", "Windows.Graphics.Capture", "HidHide", "ViGEm"):
        assert required in body


def test_xbox_start_does_not_reap_capture_card_processes() -> None:
    text = _read("native_orion/src/RemotePlaySession.cpp")
    start = text.split("void RemotePlaySession::startSidecar()", 1)[1].split(
        "void RemotePlaySession::scheduleSidecarStart", 1)[0]
    # Stronger than the former platform guard: no global process sweep for either route.
    assert "QProcess::execute" not in start
    assert "WINDOWTITLE eq ActiveMovie Window" not in start
    assert "AssignProcessToJobObject" in text
