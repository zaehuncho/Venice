"""P-E (2026-09-23 final red team): customer states, copy and patch-day docs.

RT-MED-09  settings save + sign is one crash-safe transaction; scoped Repair settings; no
           customer-build Sign Settings; one-time bootstrap marker.
RT-MED-10  sleep/resume never reads as a GUI freeze; the service pause is not an internet error.
RT-LOW-01  no "Orion" in customer text.
RT-LOW-05  every channel agrees on the owner decisions (2026-09-23): Remote Play and capture card
           fully supported, Xbox EXPERIMENTAL (shown, labelled), installer unsigned for the beta,
           free trial starts on the website home page.
CL3-F8-*   raw transport numbers, safe-mode contradiction, RP-10 "Remote is already in use",
           TLS/clock hint, bot copy (keyless, Connect, Arrow2).
RT-HIGH-04 patch-day runbook, support macros and status templates exist and quote real strings.

Native behaviour (the transaction, the watchdog decision) is pinned by the QtTests
SettingsSignatureTransactionTests / GuiFreezeWatchdogPolicyTests; these are the offline
source/copy contracts.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def function_body(text: str, signature: str) -> str:
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace:i + 1]
    raise AssertionError(f"unbalanced body for {signature}")


# ---- RT-MED-09: settings save + sign ---------------------------------------------------------

def test_security_manager_exposes_the_settings_transaction() -> None:
    header = source("native_orion/src/SecurityManager.h")
    for api in ("beginSettingsWrite", "commitSettingsWrite", "recoverInterruptedSettingsWrite",
                "settingsBootstrapAllowed", "markSettingsSignedOnce", "preserveRejectedSettings"):
        assert api in header, api
    impl = source("native_orion/src/SecurityManager.cpp")
    begin = function_body(impl, "SecurityManager::beginSettingsWrite(QString* detail) const")
    # Snapshot first, journal LAST.
    assert begin.index("settingsPrevPath()") < begin.index("settingsPrevSigPath()") \
        < begin.index("atomicWriteBytes(settingsJournalPath()")
    assert "SettingsWriteGate::Refused" in begin
    recover = function_body(impl, "SecurityManager::recoverInterruptedSettingsWrite(QString* detail)")
    # Rollback only to a snapshot that proves itself against the journal.
    assert "prevSig == journal && prevDigest == prevSig" in recover
    assert "writeSettingsSignature" not in recover, "recovery must never sign"
    # The unreachable first-run branch (RT-LOW-07) is gone, and evaluate() signs nothing in a
    # production build.
    evaluate = function_body(impl, "SecurityStatus SecurityManager::evaluate()")
    assert "settings_signature_first_run_bootstrap" not in evaluate
    assert "if (!status.settingsSignatureValid && !status.releaseManifestRequired)" in evaluate


def test_every_controller_settings_save_goes_through_the_transaction() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    for sig in ("bool OrionAppController::saveConfigSilently(const AppConfigData& data)",
                "void OrionAppController::persistConfig(const AppConfigData& data, const QString& successMessage)"):
        body = function_body(cpp, sig)
        assert body.index("beginSignedSettingsSave()") < body.index("config_.save(")
        assert "security_.commitSettingsWrite(" in body
        assert "writeSettingsSignature" not in body, sig
    gate = function_body(cpp, "bool OrionAppController::beginSignedSettingsSave()")
    assert "SettingsWriteGate::Refused" in gate and "return false;" in gate


def test_startup_recovers_before_load_and_bootstrap_is_one_time() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    recover = cpp.index("security_.recoverInterruptedSettingsWrite(")
    load = cpp.index("    config_.load();\n", recover)
    assert recover < load
    block = cpp[recover:cpp.index("remotePlay_.setRootDir(rootDir_);", recover)]
    assert "security_.settingsBootstrapAllowed()" in block
    # The old unconditional "no sig file -> sign whatever is loaded" branch is gone.
    assert "if (!security_.hasSettingsSignature()) {" not in cpp
    # A production bootstrap never signs an unsigned file that is already on disk.
    assert "preserveRejectedSettings" in block and "AppConfig defaults(rootDir_);" in block


def test_repair_is_scoped_and_sign_settings_is_dev_only() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    repair = function_body(cpp, "void OrionAppController::repairSettings()")
    assert repair.index("security_.verifySettingsSignature()") < repair.index("config_.save(")
    assert "preserveRejectedSettings" in repair
    assert "AppConfig defaults(rootDir_);" in repair
    assert "config_.save(defaults.data()" in repair
    assert "config_.save(config_.data()" not in repair, "repair must not sign loaded content"
    sign = function_body(cpp, "void OrionAppController::signSettings()")
    guard = sign.index("#ifdef ORION_PRODUCTION_BUILD")
    assert guard < sign.index("#else") < sign.index("security_.writeSettingsSignature") < sign.index("#endif")
    header = source("native_orion/src/OrionAppController.h")
    assert "Q_PROPERTY(bool settingsRepairAvailable READ settingsRepairAvailable NOTIFY statusChanged)" in header
    assert "Q_INVOKABLE void repairSettings();" in header
    debug = source("native_orion/qml/pages/DebugPage.qml")
    assert 'text: "Sign Settings"; visible: orion.debugUiEnabled === true;' in debug
    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    assert 'objectName: "settingsRepairButton"' in live
    assert "onClicked: orion.repairSettings()" in live
    assert "readonly property bool settingsRepair: orion.settingsRepairAvailable === true" in live
    policy = source("native_orion/src/UiNotificationPolicy.h")
    assert "Venice's settings didn't save cleanly (code ST-01), so shots are off." in policy
    assert "Venice's settings didn't save cleanly (code ST-01), so shots are off." in live


def test_raw_security_lock_reason_never_reaches_the_customer_feed() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    assert 'appendLog(QStringLiteral("Remote Play blocked by security lock: %1")' not in cpp
    assert "Security engine detail: Remote Play blocked by security lock: %1" in cpp


# ---- RT-MED-10: sleep / resume and the service pause -----------------------------------------

def test_gui_freeze_watchdog_uses_a_steady_clock_and_parks_on_suspend() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    for line in cpp.splitlines():
        if "guiHeartbeatMs_.store(" in line or "guiFreezeSuppressUntilMs_.store(" in line:
            assert "currentMSecsSinceEpoch" not in line, line
    assert "gui_freeze::evaluate(" in cpp
    assert "PBT_APMSUSPEND" in cpp and "systemSuspended_.store(true" in cpp
    assert '"UI thread froze for over 6 seconds"' not in cpp
    policy = source("native_orion/src/GuiFreezeWatchdogPolicy.h")
    assert "std::chrono::steady_clock" in policy
    assert "kLoopGapSuspendMs" in policy


def _run_gate_js(messages: list[str], tmp_dir: Path) -> list[list[str]]:
    gate = source("native_orion/qml/pages/AuthGate.qml")
    fn = re.search(r"// HINT-FN-BEGIN\n(.*?)\n\s*// HINT-FN-END", gate, re.S).group(1)
    script = (fn + "\nconst cases = " + json.dumps(messages) + ";\n"
              "process.stdout.write(JSON.stringify(cases.map(m => [displayMessage(m), hintFor(m)])));\n")
    tmp = tmp_dir / "pe_gate_probe.js"
    tmp.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_service_pause_is_not_an_internet_problem_and_tls_names_the_clock(tmp_path: Path) -> None:
    paused, heartbeat, tls, pinned, timeout = _run_gate_js([
        "service_disabled: Emergency maintenance",
        "Venice is paused by the service right now. Nothing is wrong with your PC or internet.",
        "TLS verification failed.",
        "Pinned server certificate did not match.",
        "License request timed out. Check your connection and try again.",
    ], tmp_path)
    assert paused[0] == "Venice is paused by the service right now. Nothing is wrong with your PC or internet."
    assert "#announcements" in paused[1] and "internet connection" not in paused[1]
    assert "#announcements" in heartbeat[1]
    assert tls[0] == "Couldn't make a secure connection to Venice's servers."
    assert "date and time" in tls[1] and "date and time" in pinned[1]
    assert "internet connection" in timeout[1] and "date and time" not in timeout[1]


def test_controller_maps_the_service_pause_on_activation_and_heartbeat() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    assert cpp.count('"Venice is paused by the service right now. Nothing is wrong with your PC or internet."') >= 2


# ---- CL3-F8 lows: raw numbers, safe-mode agreement, RP-10 ------------------------------------

def test_watchdog_reasons_are_customer_copy_and_numbers_are_engineering_only() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    assert 'tripWatchdog(QStringLiteral("Capture transport failed (' not in cpp
    assert 'tripWatchdog(QStringLiteral("The capture card stopped sending video"));' in cpp
    assert "Watchdog engine detail: capture transport failed (transport age" in cpp
    for raw in ("Direct controller input route and capture transport failed",
                "Direct controller input route remained unavailable after three input-only recoveries"):
        assert raw not in cpp
    # The Activity line and the SAFE MODE dialog make the same promise.
    assert "Auto-recovers after %2s of a" not in cpp
    dialog = source("native_orion/qml/components/TopStatusBar.qml")
    assert "will stay off until you exit safe mode" not in dialog
    assert "steady for 30 seconds" in dialog
    assert "once the stream has been steady for %2 seconds" in cpp


def test_remote_play_in_use_maps_to_rp10_before_the_catch_all() -> None:
    policy = source("native_orion/src/UiNotificationPolicy.h")
    body = function_body(policy, "inline QString customerRemoteStatus(const QString& raw)")
    rp10 = body.index("code RP-10")
    assert rp10 < body.index("Production package is incomplete") < body.index("code RP-09")
    assert '"80108b10"' in body and '"already in use"' in body


# ---- RT-LOW-01: no internal codename in customer text ----------------------------------------

def test_no_orion_codename_in_customer_strings() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    assert "Orion is minimized" not in cpp
    assert "bring Orion to the foreground" not in cpp
    assert "Feed paused: Venice is minimized" in cpp
    assert '"Orion diagnostics bundle.' not in cpp and '"/orion_diagnostics_"' not in cpp
    for rel in ("native_orion/qml/pages/RemotePlayPage.qml", "native_orion/qml/pages/AuthGate.qml",
                "native_orion/qml/components/TopStatusBar.qml",
                "native_orion/qml/components/StreamSetupForm.qml"):
        text = source(rel)
        for literal in re.findall(r'"([^"\n]*)"', text):
            assert not re.search(r"\bOrion\b", literal), (rel, literal)


# ---- RT-LOW-05: every channel agrees on the owner decisions -----------------------------------

EMBEDS = sorted((ROOT / "discord_launch" / "launch_embeds").glob("*.json"))


def _embed_customer_text(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("_meta", None)
    return json.dumps(data, ensure_ascii=False)


def _import_bot():
    os.environ.setdefault("DISCORD_BOT_TOKEN", "unit-dummy-token")
    os.environ.setdefault("ORION_BOT_SECRET", "unit-dummy-bot-secret")
    os.environ.setdefault("ORION_GUILD_ID", "123456789012345678")
    if "orion_bot_module" in sys.modules:
        return sys.modules["orion_bot_module"]
    path = ROOT / "discord_launch" / "orion_bot.py"
    spec = importlib.util.spec_from_file_location("orion_bot_module", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orion_bot_module"] = mod
    spec.loader.exec_module(mod)
    return mod


STALE_SCOPE = (
    "Remote Play-only",
    "is the supported setup",
    "Xbox is not supported",
    "there is no Xbox version",
    "signed installer",
    "/claim_trial",
    "Claim the trial in Discord",
)


def test_website_embeds_and_app_agree_on_the_owner_decisions() -> None:
    texts = {
        "index.html": source("website/public/index.html"),
        "refunds.html": source("website/public/refunds.html"),
        "StreamSetupForm.qml": source("native_orion/qml/components/StreamSetupForm.qml"),
    }
    for path in EMBEDS:
        texts[path.name] = _embed_customer_text(path)
    worker = source("website/src/worker.js")
    texts["worker ENTITLEMENT_MESSAGE"] = re.search(r"const ENTITLEMENT_MESSAGE = '([^']*)'", worker).group(1)
    for name, text in texts.items():
        for stale in STALE_SCOPE:
            assert stale not in text, (name, stale)
    # Xbox: mentioned => labelled experimental.
    for name, text in texts.items():
        if "Xbox" in text:
            assert re.search(r"Xbox (?:is |Remote Play is )?(?:listed as )?(?:EXPERIMENTAL|experimental)", text), name
    # Remote Play is presented as fully supported wherever support scope is stated.
    assert "fully supported" in texts["index.html"]
    assert "fully supported" in texts["welcome_terms_patch.json"]
    # The trial starts on the website.
    assert "Start the free trial on the Venice home page" in texts["worker ENTITLEMENT_MESSAGE"]
    assert "home page" in texts["pricing.json"] and "home page" in texts["purchase_command.json"]


def test_downloads_embed_allows_the_unsigned_beta_installer() -> None:
    meta = json.loads(source("discord_launch/launch_embeds/downloads.json"))["_meta"]
    assert "Do not post a link to an unsigned build" not in meta["blocked_by"]
    assert "OWNER DECISION" in meta["signing_status"] and "UNSIGNED" in meta["signing_status"]
    field = _embed_customer_text(ROOT / "discord_launch" / "launch_embeds" / "downloads.json")
    assert "More info" in field and "Run anyway" in field and "Get-FileHash" in field


FORBIDDEN_BOT_WORDS = re.compile(r"\bkeys?\b|chiaki|defaults work best|licen[cs]e", re.I)


def test_bot_customer_replies_are_keyless_and_name_the_real_buttons() -> None:
    bot = _import_bot()
    payloads = [
        {"ok": True, "mode": "free", "free_resets_remaining": 2},
        {"ok": False, "error": "locked"},
        {"ok": False, "error": "payment_required", "deduct_days": 1},
        {"ok": False, "error": "trial_no_deduct"},
        {"ok": False, "error": "paid_only"},
    ]
    for payload in payloads:
        text, _ = bot.render_hwid_reset(payload)
        assert not FORBIDDEN_BOT_WORDS.search(text), (payload, text)
    for topic, spec in bot.FAQ_TOPICS.items():
        for field in ("label", "title", "description"):
            assert not FORBIDDEN_BOT_WORDS.search(spec[field]), (topic, field)
    no_greens = bot.FAQ_TOPICS["no_greens"]["description"]
    assert "Arrow2" in no_greens and "**Connect**" in no_greens
    quick = bot.FAQ_TOPICS["quick_answers"]["description"]
    assert "zaeorion.com" in quick and "home page" in quick
    remote = bot.FAQ_TOPICS["remote_play"]["description"]
    assert "RP-10" in remote and "fully supported" in remote
    bot_src = source("discord_launch/orion_bot.py")
    start = bot_src.index("async def claim_trial(interaction: discord.Interaction):")
    claim = bot_src[start:bot_src.index("\n@tree.command", start)]
    assert "zaeorion.com" in claim and "lambda_post" not in claim


# ---- RT-HIGH-04 (docs) + 30 Hz preview-only ---------------------------------------------------

def test_patch_day_runbook_macros_and_templates_exist_and_quote_real_strings() -> None:
    runbook = source("docs/support/PATCH_DAY_RUNBOOK.md")
    for step in ("Detect", "Confirm", "Communicate", "Kill", "Ship", "Verify", "Recover"):
        assert step in runbook, step
    assert "tools/package_orion_release.py" in runbook
    assert "verify_release_integrity.py" in runbook
    assert "orion-admin killswitch engage" in runbook and "--motd-text" in runbook
    for script in ("tools/verify_release_integrity.py", "tools/package_orion_release.py",
                   "tools/admin/orion_admin.py", "scripts/verify_orion.ps1",
                   "scripts/build_orion_sidecar.ps1", "tools/timing/panel_grade.py",
                   "tools/diagnostics/replay_framedump.py", "run_orion.local.ps1"):
        assert (ROOT / script).exists(), script  # every command the runbook names is real
    assert "register_commands.py" in runbook  # the never-run warning
    macros = source("docs/support/SUPPORT_MACROS.md")
    cpp = source("native_orion/src/OrionAppController.cpp")
    policy = source("native_orion/src/UiNotificationPolicy.h")
    quoted = [
        ("Venice's settings didn't save cleanly (code ST-01), so shots are off.", policy),
        ("Another device is using Remote Play on this PS5 (code RP-10).", policy),
        ("Venice is paused by the service right now. Nothing is wrong with your PC or internet.", cpp),
        ("The capture card stopped sending video", cpp),
        ("Feed paused: Venice is minimized, so shots are paused.", cpp),
    ]
    for text, where in quoted:
        assert text in where, text
        assert text in macros, text
    templates = json.loads(source("discord_launch/launch_embeds/patch_day_status_templates.json"))
    assert set(templates["templates"]) == {"patch_detected", "service_paused", "fix_shipped", "all_clear"}
    for name, tpl in templates["templates"].items():
        for embed in tpl["message"]["embeds"]:
            assert len(embed["description"]) <= 4096, name


def test_30hz_capture_is_labelled_preview_only() -> None:
    form = source("native_orion/qml/components/StreamSetupForm.qml")
    assert 'model: ["30 Hz (preview only)", "60 Hz (recommended)"]' in form
    assert 'objectName: "captureFpsCombo"' in form
    assert "Shot timing needs 60 Hz." in form
