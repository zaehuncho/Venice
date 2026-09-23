"""Customer copy fixes from docs/redteam/2026-09-23-final/GEMINI_VERIFIED.claude.md.

String-level contracts for QML / C++ / installer / Discord-embed copy (the native
behaviour of the C++ policies is pinned in native_orion/tests/ActivityFeedPolicyTests.cpp
and LicenseHeartbeatPolicyTests.cpp). The AuthGate hint picker is plain JavaScript, so it is
executed under node against every message the app and the server can produce.
See docs/redteam/2026-09-23-final/COPY_FIXES_APPLIED.claude.md.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ---- 1. AuthGate default line --------------------------------------------------------------

def test_authgate_default_line_is_the_one_time_code_instruction() -> None:
    header = source("native_orion/src/OrionAppController.h")
    assert ('QString authMessage_ = QStringLiteral("Paste the one-time code from '
            'zaeorion.com/connect, then press Unlock.");') in header
    assert "Enter a license key" not in header
    controller = source("native_orion/src/OrionAppController.cpp")
    assert 'QStringLiteral("Checking license...")' not in controller
    assert 'QStringLiteral("License verified. Opening Venice.")' not in controller
    assert "did not return a valid license" not in controller
    gate = source("native_orion/qml/pages/AuthGate.qml")
    assert '"Verifying license…"' not in gate
    assert "license server" not in gate


# ---- 2. AuthGate hint picker ------------------------------------------------------------------

HINT_UPDATE = "Restart Venice to update, or reinstall from #downloads."
HINT_TICKET = "Open a ticket in the Venice Discord."
HINT_FINGERPRINT = "Restart Venice; if it repeats, open a ticket."
HINT_RATE = "Too many attempts in a row — wait a moment, then try the connection once."
HINT_HWID = ("This account is linked to another PC. Run /hwid_reset in Discord, then connect "
             "this PC again. If that was not you, open a ticket.")
HINT_SIGNIN = ("Sign in with the Discord account that owns your trial or subscription, then use "
               "a fresh one-time code.")
HINT_CODE = ("That one-time code is invalid, expired, or already used. Open Connect Discord "
             "again for a fresh code.")
HINT_ENDED = ("Your trial or subscription has ended. Run /status in Discord or renew on the "
              "Venice website.")
HINT_NETWORK = ("Couldn't reach Venice's servers. Check your internet connection and try again "
                "in a moment.")

# (message as shown in authMessage, expected hint). Messages are the literal strings from
# OrionAppController.cpp, LicenseClient.cpp (licenseErrorUserText + transport errors) and
# backend/lambda_function.py handle_activate, plus the bare codes the parser falls back to.
HINT_CASES = [
    # The four mis-hints the verified review found.
    ("A Discord ID is public. Select Connect Discord to verify your account.", ""),
    ("This device is blocked.", HINT_TICKET),
    ("Update required — this version is no longer allowed (minimum version 1.2.0).", HINT_UPDATE),
    ("Machine fingerprint unavailable. Restart Venice or check Windows identity services.",
     HINT_FINGERPRINT),
    ("Discord sign-in did not return a valid code. Get a fresh one-time code from "
     "zaeorion.com/connect and try again.", HINT_CODE),
    # Server prose and bare codes.
    ("This account is blocked.", HINT_TICKET),
    ("This build is no longer supported — update required.", HINT_UPDATE),
    ("version_blocked", HINT_UPDATE),
    ("This licence is linked to a different PC. Use /hwid_reset in Discord or open a ticket.",
     HINT_HWID),
    ("This sign-in link expired or was used. Connect Discord again.", HINT_CODE),
    ("Invalid key format.", HINT_CODE),
    ("invalid_key", HINT_CODE),
    ("replay_detected", HINT_CODE),
    ("An active Venice subscription tied to your Discord account is required. Run /purchase "
     "or open a ticket.", HINT_SIGNIN),
    ("Connect your Discord account at zaeorion.com/connect, then unlock with the one-time code.",
     HINT_SIGNIN),
    ("license_expired", HINT_ENDED),
    ("trial_used", HINT_ENDED),
    ("license_revoked", HINT_ENDED),
    ("Activation is rate limited locally. Wait a moment and retry.", HINT_RATE),
    ("rate_limited", HINT_RATE),
    ("License request timed out. Check your connection and try again.", HINT_NETWORK),
    ("TLS verification failed.", HINT_NETWORK),
    ("Pinned server certificate did not match.", HINT_NETWORK),
    ("Connection refused", HINT_NETWORK),
    ("Host api.zaeorion.com not found", HINT_NETWORK),
    ("Account access could not be checked just now. Retry shortly.", HINT_NETWORK),
    # Messages that already carry the whole next step: no second line.
    ("Your PC clock is off. Turn on 'Set time automatically' in Windows Date & Time settings, "
     "then restart Venice.", ""),
    ("Your subscription is paused — contact support.", ""),
    ("This licence is frozen — the clock is paused. Open a ticket to resume.", ""),
    ("", ""),
]


def _hint_function() -> str:
    gate = source("native_orion/qml/pages/AuthGate.qml")
    match = re.search(r"// HINT-FN-BEGIN\n(.*?)\n\s*// HINT-FN-END", gate, re.S)
    assert match, "AuthGate.qml lost its HINT-FN-BEGIN/END markers"
    return match.group(1)


def test_authgate_binds_the_hint_to_the_tested_function() -> None:
    gate = source("native_orion/qml/pages/AuthGate.qml")
    assert "readonly property string errorHint: hintFor(orion.authMessage)" in gate
    # The old loose patterns that produced the wrong next steps are gone.
    assert "/machine|device|another|bound|hwid/" not in gate
    assert "/discord_signin|required/" not in gate
    assert "offline|unreach|tls|certificate/.test(m))\n            return \"Couldn't reach the license" not in gate


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_authgate_hint_picks_the_right_next_step_for_every_message(tmp_path: Path) -> None:
    script = tmp_path / "hint.js"
    script.write_text(
        _hint_function()
        + "\nconst cases = " + json.dumps([m for m, _ in HINT_CASES]) + ";\n"
        + "process.stdout.write(JSON.stringify(cases.map(hintFor)));\n",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    got = json.loads(out)
    for (message, expected), actual in zip(HINT_CASES, got):
        assert actual == expected, f"{message!r} -> {actual!r}, want {expected!r}"


# ---- 4. First-run tour meter step -------------------------------------------------------------

def test_tour_meter_step_says_arrow2_white_is_preset() -> None:
    tour = source("native_orion/qml/components/FirstRunTour.qml")
    assert "Pick the Style and Color" not in tour
    assert ('body: "Arrow2 (White) is preset here — set your in-game shot meter to Arrow2 '
            '(White). Detection adapts') in tour


# ---- 5 / 6 / 14. Live page raw errors, overlay button, headlines ----------------------------

def test_live_page_shows_customer_status_not_raw_engine_errors() -> None:
    controller = source("native_orion/src/OrionAppController.cpp")
    assert "ui_notifications::customerRemoteStatus(status)" in controller
    assert 'QStringLiteral("Remote Play engine detail: %1").arg(status)' in controller
    assert 'appendLog(QStringLiteral("Remote Play: %1 - %2").arg(remoteState_, customerStatus));' in controller
    # No raw status suffixes in the input-dead detail.
    assert "press Connect. (%1)" not in controller
    assert "restore the session. (%1)" not in controller
    assert '"Reconnect blocked: %1"' not in controller
    assert ('"Venice couldn\'t reconnect your controller (code RP-07). Press Connect."'
            in controller)
    policy = source("native_orion/src/UiNotificationPolicy.h")
    for code in ("RP-05", "RP-06", "RP-07", "RP-08", "RP-09"):
        assert f"(code {code})" in policy, code
    session = source("native_orion/src/RemotePlaySession.cpp")
    # The raw sidecar message no longer becomes the session status.
    assert ('setState(RemotePlayState::Error,\n                     '
            'QStringLiteral("Chiaki input recovery failed."));') in session.replace("\r\n", "\n")


def test_input_dead_overlay_names_the_real_button_in_sentence_case() -> None:
    controller = source("native_orion/src/OrionAppController.cpp")
    assert "Enable \"\n                \"Bot + Controller" not in controller
    assert "Press Enable" not in controller
    assert ('"This is the HDMI preview only — Venice isn\'t connected yet. Press Connect."'
            in controller)
    assert 'QStringLiteral("Controller input isn\'t reaching your PS5")' in controller
    assert 'QStringLiteral("Connecting — buttons aren\'t reaching your PS5 yet")' in controller
    assert "CONTROLLER INPUT IS NOT REACHING THE CONSOLE" not in controller
    assert "CONNECTING — BUTTONS NOT REACHING THE CONSOLE YET" not in controller
    assert "The direct input link dropped" not in controller


# ---- 7. Keyless wording ----------------------------------------------------------------------

def test_legal_gate_and_account_flyout_are_keyless() -> None:
    legal = source("native_orion/qml/pages/LegalGate.qml")
    assert "share your license key" not in legal
    assert "or share your account access or one-time codes." in legal
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    assert "licence key" not in sidebar.lower()
    assert 'text: "Your account includes "' in sidebar
    # The stored internal key row is dev-only (customers sign in with one-time codes).
    key_row = sidebar[sidebar.index('label: "Key"') - 600:sidebar.index('label: "Key"')]
    assert "visible: orion.debugUiEnabled === true" in key_row
    assert 'String(orion.licenseKeyMasked || "").length > 0' in key_row


# ---- 8. SmartScreen / download verification --------------------------------------------------

def test_downloads_embed_and_install_notice_agree_on_the_unsigned_installer() -> None:
    notice = source("installer/INSTALL_NOTICE.txt")
    embed = json.loads(source("discord_launch/launch_embeds/downloads.json"))
    field = embed["message"]["embeds"][0]["fields"][0]["value"]
    for text in (notice, field):
        assert "isn't code-signed yet" in text
        assert "More info" in text
        assert "Run anyway" in text
        assert "Get-FileHash" in text
        assert "SHA-256" in text
        assert "administrator" in text
    assert "stop and open a ticket" not in field
    assert "signed installer" not in field
    # No promise the notice cannot keep.
    assert "you'll never see" not in notice
    assert len(field) <= 1024   # Discord embed field limit


# ---- 9. Activity feed rule 0 --------------------------------------------------------------------

def test_activity_feed_hides_internal_lines_before_the_allow_list() -> None:
    policy = source("native_orion/src/UiNotificationPolicy.h")
    ring = policy[policy.index("inline bool shouldEnterActivityRing"):]
    assert ring.index("isInternalOnlyLine(line)") < ring.index("isCustomerActivityTemplate(line)")
    internal = policy[policy.index("inline bool isInternalOnlyLine"):policy.index("inline QString customerRemoteStatus")]
    for marker in ('"lease-gated fire"', '"fire lease"', '"sidecar"', '"orion_"',
                   '"engine detail:"', '"shot automation aborted:"'):
        assert marker in internal, marker
    # A bare "lease" marker would hide every "Release ..." shot line.
    assert 'QStringLiteral("lease"),' not in internal
    controller = source("native_orion/src/OrionAppController.cpp")
    assert 'tripWatchdog(QStringLiteral("Video detection stopped mid-stream"));' in controller
    assert "Safe mode: video detection keeps stopping." in controller
    assert "(applies the next time you connect)" in controller


# ---- 10. Rows 12-21 of the verified list -------------------------------------------------------

def test_shot_not_taken_and_calibration_copy_speak_the_slider_scale() -> None:
    controller = source("native_orion/src/OrionAppController.cpp")
    assert 'QStringLiteral("Shot not taken (%1).")' not in controller
    assert "ui_notifications::customerShotNotTakenText(reason)" in controller
    assert "more than Tip Timing %2 ms can" not in controller
    assert "is too high for your jumpshot, so Venice" in controller
    assert '"Locked at %1 ms after' not in controller
    assert "adjusting by %3 ms" not in controller
    assert "Locked at Shot Lead %1 after %2 shots." in controller
    assert '"Shot %1 — Shot Lead %2 so far. Keep going until it locks."' in controller
    assert "Calibration isn't available right now — restart Venice." in controller
    policy = source("native_orion/src/UiNotificationPolicy.h")
    # Mirrors ShotLeadCard.qml valueFromMs (150..400 ms <-> 1..100).
    assert "constexpr double kLo = 150.0;" in policy
    assert "constexpr double kHi = 400.0;" in policy
    card = source("native_orion/qml/components/ShotLeadCard.qml")
    assert "readonly property real msLo: 150" in card
    assert "readonly property real msHi: 400" in card


def test_installer_offers_restart_instead_of_launch_when_one_is_pending() -> None:
    iss = source("installer/orion.iss")
    assert "function NeedRestart(): Boolean;" in iss
    assert "Result := DriverRestartNeeded or VCRedistRestartNeeded;" in iss
    assert "VCRedistRestartNeeded := True;" in iss
    assert "Flags: nowait postinstall skipifsilent; Check: not RestartPending" in iss


def test_remaining_customer_surfaces_drop_jargon() -> None:
    panel = source("native_orion/qml/components/MeterConfigPanel.qml")
    health = panel[panel.index('objectName: "detectorHealthLine"'):][:200]
    assert "visible: orion.debugUiEnabled === true" in health
    update = source("native_orion/qml/pages/UpdateGatePage.qml")
    assert "Ed25519" not in update
    assert 'text: "Signed, verified updates"' in update
    heartbeat = source("native_orion/src/LicenseHeartbeatPolicy.h")
    assert "until your licence is confirmed" not in heartbeat
    assert ("Reconnecting to Venice servers — shots paused until your subscription is "
            "confirmed. Usually a few seconds.") in heartbeat
    assert 'QStringLiteral("Subscription confirmed: shots re-enabled.")' in heartbeat
    guide = source("discord_launch/launch_embeds/setup_guide.json")
    json.loads(guide)
    assert "Shot meter **on**, style **Arrow2** (White), in 2K's settings." in guide
    form = source("native_orion/qml/components/StreamSetupForm.qml")
    assert 'model: ["PS5", "Xbox"]' not in form
    assert ('model: (orion.debugUiEnabled === true || orion.remotePlayConsole === "Xbox")\n'
            '                   ? ["PS5", "Xbox"] : ["PS5"]') in form
    assert "WGC video" not in form
    assert "HidHide" not in form


# ---- 11. Lead env overrides are dev-only -------------------------------------------------------

def test_lead_floor_and_bias_env_overrides_are_compiled_out_of_production() -> None:
    engine = source("native_orion/src/AutomationEngine.cpp").replace("\r\n", "\n")
    start = engine.index("    bool leadOverrideFromEnv = false;\n")
    end = engine.index("    config_.leadOverrideFromEnv = leadOverrideFromEnv;", start)
    block = engine[start:end]
    guard = block.index("#ifndef ORION_PRODUCTION_BUILD")
    close = block.index("#endif")
    assert guard < block.index('qEnvironmentVariableIsSet("ORION_LEAD_FLOOR_MS")') < close
    assert guard < block.index('qEnvironmentVariableIsSet("ORION_LEAD_BIAS_MS")') < close
    # Exactly one env read of each, and both inside the guard.
    assert engine.count('qEnvironmentVariable("ORION_LEAD_FLOOR_MS")') == 1
    assert engine.count('qEnvironmentVariable("ORION_LEAD_BIAS_MS")') == 1
