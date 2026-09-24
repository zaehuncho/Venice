"""P-H (2026-09-23 final red team): remember the sign-in.

The production build never remembered a sign-in: the canonical licence key lived only in
OrionAppController::authLicenseKey_, so every launch showed AuthGate and a keyless (PAIR-)
customer needed a new one-time code each time. The canonical key is now stored DPAPI-protected
in the per-user data dir and re-submitted to the server at launch; a definitive refusal deletes
it, a transient failure keeps it, and Sign out deletes it.

Native behaviour (store/load/clear, corrupt file, refusal vs transient policy, no key text in
logs) is pinned by the QtTest OrionRememberedSignInTests (native_orion/tests/
RememberedSignInTests.cpp). These are the offline source / wiring / copy contracts.
See docs/redteam/2026-09-23-final/patches/P-H_remember_signin.md.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def activation_handler(cpp: str) -> str:
    return function_body(cpp, "connect(&licenseClient_, &LicenseClient::activationFinished")


def validation_handler(cpp: str) -> str:
    return function_body(cpp, "connect(&licenseClient_, &LicenseClient::validationFinished")


# ---- storage (SecurityManager) ----------------------------------------------------------------

def test_security_manager_exposes_the_remembered_signin_store() -> None:
    header = source("native_orion/src/SecurityManager.h")
    for api in ("storeRememberedLicenseKey", "loadRememberedLicenseKey",
                "clearRememberedLicenseKey", "hasRememberedLicenseKey",
                "enum class RememberedKeyLoad", "isRememberableLicenseKey"):
        assert api in header, api
    # The path stays private: callers cannot read or write the file around the API.
    private = header[header.index("private:\n    [[nodiscard]] QByteArray settingsDigest()"):]
    assert "rememberedLicenseKeyPath()" in private


def test_store_is_dpapi_only_canonical_only_and_not_build_bound() -> None:
    impl = source("native_orion/src/SecurityManager.cpp")
    store = function_body(impl, "bool SecurityManager::storeRememberedLicenseKey(")
    assert "isRememberableLicenseKey(canonicalKey)" in store
    assert "dpapiProtect(plain, rememberedSignInEntropy()" in store
    # No plaintext fallback: a failed DPAPI call refuses to store.
    assert "if (!dpapiOk || blob.isEmpty())" in store
    assert "return false;" in store.split("if (!dpapiOk || blob.isEmpty())", 1)[1][:400]
    # Bound to machine + Windows user; deliberately NOT to the build hash (an update must not
    # sign the customer out).
    assert '"machine_id"' in store and '"user"' in store
    assert "appBuildHash" not in store and "build_sha256" not in store
    assert "atomicWriteBytes(path" in store
    entropy = function_body(impl, "QByteArray SecurityManager::rememberedSignInEntropy()")
    assert 'QByteArrayLiteral("venice-remembered-signin-v1|") + machineId().toUtf8()' in entropy
    path = function_body(impl, "QString SecurityManager::rememberedLicenseKeyPath()")
    assert 'orionDataDir(rootDir_) + QStringLiteral("/.vault/venice_signin.dat")' in path
    helper = function_body(impl, "QByteArray dpapiProtect(")
    assert "CryptProtectData(" in helper and "CRYPTPROTECT_UI_FORBIDDEN" in helper
    assert "CRYPTPROTECT_LOCAL_MACHINE" not in impl   # CURRENT_USER scope only


def test_canonical_key_shape_rejects_pair_codes() -> None:
    impl = source("native_orion/src/SecurityManager.cpp")
    shape = function_body(impl, "bool isRememberableLicenseKey(const QString& key)")
    assert r'"^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$"' in shape
    # No trimming / uppercasing that could turn some other string into a stored key.
    assert "trimmed()" not in shape and "toUpper()" not in shape


def test_load_deletes_every_bad_record_and_returns_the_key_only_after_all_checks() -> None:
    impl = source("native_orion/src/SecurityManager.cpp")
    load = function_body(impl, "QString SecurityManager::loadRememberedLicenseKey(")
    reject = load[load.index("const auto reject"):load.index("const QByteArray raw")]
    assert "QFile::remove(path)" in reject
    assert "RememberedKeyLoad::Cleared" in reject
    for check in ("kRememberedFileMaxBytes", "kRememberedOuterSchema", "dpapiUnprotect(",
                  "kRememberedPayloadSchema", "binding.value(QStringLiteral(\"machine_id\"))",
                  "currentUserBinding()", "isRememberableLicenseKey(key)"):
        assert check in load, check
    # The key is returned exactly once, after the last check.
    assert load.count("return key;") == 1
    assert load.index("return key;") > load.index("isRememberableLicenseKey(key)")
    # The detail string carries the suffix only.
    assert ".arg(key.right(4))" in load
    assert ".arg(key)" not in load


def test_entitlement_cache_still_uses_its_own_entropy() -> None:
    impl = source("native_orion/src/SecurityManager.cpp")
    for fn in ("QByteArray SecurityManager::protectBytes(", "QByteArray SecurityManager::unprotectBytes("):
        body = function_body(impl, fn)
        assert 'QByteArrayLiteral("orion-entitlement-v1|")' in body
    assert impl.count("CryptProtectData(") == 1     # one call site shape (dpapiProtect)
    assert impl.count("CryptUnprotectData(") == 1


# ---- policy (LicenseClient / LicenseHeartbeatPolicy) ----------------------------------------

def test_refusal_classifier_covers_both_endpoints_and_keeps_owner_wide_gates() -> None:
    impl = source("native_orion/src/LicenseClient.cpp")
    body = function_body(impl, "bool isRememberedSignInRefusalCode(const QString& code)")
    for code in ("invalid_key", "revoked", "license_revoked", "expired", "license_expired",
                 "inactive", "license_invalid_status", "frozen", "blacklisted", "device_mismatch",
                 "device_limit_reached", "subscription_required", "discord_signin_required",
                 "trial_used"):
        assert f'"{code}"' in body, code
    for keep in ("service_disabled", "version_blocked", "rate_limited", "internal_error",
                 "entitlement_unavailable", "kill_state_unavailable", "config_unavailable",
                 "timestamp_expired"):
        assert f'"{keep}"' not in body, keep
    # Exact code match, never a substring / prose match.
    assert "code == QLatin1String(refusal)" in body
    assert "contains(" not in body and "startsWith(" not in body


def test_backend_activation_codes_are_all_classified() -> None:
    """Every 403 the activation endpoint can answer is either a listed refusal or a
    documented keep, so a new backend code is a conscious decision, not a silent keep."""
    lam = source("backend/lambda_function.py")
    start = lam.index("def handle_activate(")
    end = lam.index("\ndef ", start + 10)
    codes = set(re.findall(r'err\("([a-z_]+)", 403', lam[start:end]))
    assert codes, "handle_activate no longer answers 403 codes?"
    impl = source("native_orion/src/LicenseClient.cpp")
    refusals = function_body(impl, "bool isRememberedSignInRefusalCode(const QString& code)")
    documented_keep = {"version_blocked", "replay_detected", "invalid_timestamp"}
    for code in codes:
        assert f'"{code}"' in refusals or code in documented_keep, code


def test_retry_decision_walks_the_heartbeat_ladder() -> None:
    policy = source("native_orion/src/LicenseHeartbeatPolicy.h")
    fn = function_body(policy, "rememberedSignInAfterFailure(")
    assert "backoff.reset();" in fn and "d.clearStoredKey = true;" in fn
    assert "backoff.recordFailure(rateLimited);" in fn
    assert "d.retryDelayMs = backoff.nextDelayMs();" in fn


# ---- controller wiring ---------------------------------------------------------------------

def test_activation_success_stores_only_after_the_server_verdict_and_pair_exchange() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    handler = activation_handler(cpp)
    ok_branch = handler[handler.index("        if (result.ok) {\n            authenticated_ = true;"):
                        handler.index("        } else {\n            // An activation failure")]
    assert "security_.storeRememberedLicenseKey(authLicenseKey_, &rememberError)" in ok_branch
    # The PAIR- code is replaced by the canonical key BEFORE anything can be stored.
    assert handler.index("authLicenseKey_ = result.canonicalLicenseKey;") < \
        handler.index("storeRememberedLicenseKey")
    # A sign-in that cannot be remembered forgets any OLDER remembered key.
    assert 'forgetRememberedSignIn(QStringLiteral("this sign-in could not be remembered"))' in ok_branch
    assert "key_suffix=%1" in ok_branch and ".arg(authLicenseKey_.right(4))" in ok_branch


def test_activation_failure_clears_only_on_a_refusal_of_the_remembered_attempt() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    handler = activation_handler(cpp)
    assert "const bool rememberedAttempt = std::exchange(rememberedSignInAttempt_, false);" in handler
    failure = handler[handler.index("        } else {\n            // An activation failure"):]
    block = failure[failure.index("if (rememberedAttempt) {"):]
    assert "isRememberedSignInRefusalCode(result.error)" in block
    assert "forgetRememberedSignIn(" in block
    assert "rememberedSignInRetryTimer_.start(next.retryDelayMs);" in block
    # The failure branch never grants authority.
    assert "authenticated_ = true" not in failure


def test_heartbeat_kill_deletes_the_store_only_for_key_level_verdicts() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    handler = validation_handler(cpp)
    kill = handler[handler.index('appendLog(QStringLiteral("License heartbeat: session disabled (%1)").arg(e));'):]
    assert "if (isRememberedSignInRefusalCode(e)) {" in kill
    assert "forgetRememberedSignIn(" in kill


def test_startup_resume_runs_after_the_dev_hooks_and_outside_them() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    env_hook = cpp.index('qEnvironmentVariableIsSet("ORION_LICENSE_KEY")')
    dev_end = cpp.index("#endif", env_hook)
    launch = cpp.index('beginRememberedSignIn(QStringLiteral("launch"));')
    assert launch > dev_end, "remembered sign-in must run after (and outside) the dev-only hooks"
    guard = cpp[cpp.rindex("if (", 0, launch):launch]
    assert "!authenticated_ && !authBusy_" in guard


def test_resume_goes_through_authenticate_and_never_unlocks_locally() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    begin = function_body(cpp, "void OrionAppController::beginRememberedSignIn(const QString& trigger)")
    assert "security_.loadRememberedLicenseKey(&outcome, &detail)" in begin
    assert "authenticate(key);" in begin
    assert "authenticated_ = true" not in begin
    assert "leaseGate_.record" not in begin
    assert "currentPage_" not in begin
    assert "if (authenticated_ || authBusy_ ||" in begin
    assert 'authMessage_ = QStringLiteral("Signing you in…");' in begin
    assert "rememberedSignInAttempt_ = false;" in begin
    assert ".arg(trigger, key.right(4))" in begin
    resume = function_body(cpp, "void OrionAppController::resumeRememberedSignIn()")
    assert "beginRememberedSignIn(" in resume


def test_sign_out_tears_down_the_session_and_forgets_the_pc() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    body = function_body(cpp, "void OrionAppController::signOut()")
    for step in ("if (authBusy_) {", "HeartbeatOutcome::Kill", "licenseHeartbeatTimer_.stop();",
                 "disconnectRemotePlay(true);", "authenticated_ = false;",
                 "leaseGate_.recordHeartbeatKill();", "authToken_.clear();",
                 "authLicenseKey_.clear();", "forgetRememberedSignIn(",
                 "security_.clearLocalEntitlement(", "updateSecurityStatus();",
                 "emit authChanged();"):
        assert step in body, step
    forget = function_body(cpp, "void OrionAppController::forgetRememberedSignIn(const QString& reason)")
    assert "security_.clearRememberedLicenseKey(&error)" in forget
    assert "rememberedSignInRetryTimer_.stop();" in forget
    header = source("native_orion/src/OrionAppController.h")
    assert "Q_INVOKABLE void signOut();" in header
    assert "Q_INVOKABLE void resumeRememberedSignIn();" in header
    assert "Q_PROPERTY(bool rememberedSignInAvailable READ rememberedSignInAvailable NOTIFY authChanged)" in header


def test_no_log_line_prints_a_full_key() -> None:
    cpp = source("native_orion/src/OrionAppController.cpp")
    assert ".arg(authLicenseKey_)" not in cpp
    for fn in ("void OrionAppController::beginRememberedSignIn(",
               "void OrionAppController::forgetRememberedSignIn(",
               "void OrionAppController::signOut()"):
        body = function_body(cpp, fn)
        assert ".arg(key)" not in body
        assert ".arg(authLicenseKey_" not in body
    impl = source("native_orion/src/SecurityManager.cpp")
    for fn in ("bool SecurityManager::storeRememberedLicenseKey(",
               "bool SecurityManager::clearRememberedLicenseKey("):
        body = function_body(impl, fn)
        assert ".arg(canonicalKey" not in body and "qDebug" not in body and "qWarning" not in body


# ---- QML ----------------------------------------------------------------------------------

def test_account_flyout_has_a_sign_out() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    block = sidebar[sidebar.index('objectName: "licenseFlyoutSignOut"') - 200:]
    block = block[:1600]
    assert "orion.signOut()" in block
    assert 'text: "Sign out"' in block
    assert "licenseFlyout.close()" in block
    assert "enabled: orion.authBusy !== true" in block
    # Customer copy: no internal names, no key wording.
    assert "Orion" not in block and "licence key" not in sidebar.lower()


def test_authgate_offers_unlock_with_the_remembered_signin() -> None:
    gate = source("native_orion/qml/pages/AuthGate.qml")
    assert "readonly property bool remembered: orion.rememberedSignInAvailable === true" in gate
    assert "readonly property bool ready: (keyField.text.trim().length > 0 || resumeReady) && !orion.authBusy" in gate
    unlock = gate[gate.index("function tryUnlock()"):gate.index("function consumePendingKey()")]
    assert "orion.resumeRememberedSignIn()" in unlock
    assert "orion.authenticate(k.trim())" in unlock
    row = gate[gate.index('objectName: "authRememberedRow"'):][:1400]
    assert 'text: "Venice remembers this PC. Press Unlock to sign in again."' in row
    assert "orion.signOut()" in row
    assert "Orion" not in row


HINT_CASES = [
    ("Signing you in…", ""),
    ("Signed out. To sign in again, get a one-time code from zaeorion.com/connect and press Unlock.", ""),
    # Unchanged neighbours (the new rule must not swallow them).
    ("This sign-in link expired or was used. Connect Discord again.",
     "That one-time code is invalid, expired, or already used. Open Connect Discord again for a fresh code."),
    ("Connect your Discord account at zaeorion.com/connect, then unlock with the one-time code.",
     "Sign in with the Discord account that owns your trial or subscription, then use a fresh one-time code."),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_authgate_hint_for_the_new_messages(tmp_path: Path) -> None:
    gate = source("native_orion/qml/pages/AuthGate.qml")
    fn = re.search(r"// HINT-FN-BEGIN\n(.*?)\n\s*// HINT-FN-END", gate, re.S).group(1)
    script = tmp_path / "hint.js"
    script.write_text(fn + "\nconst cases = " + json.dumps([m for m, _ in HINT_CASES]) + ";\n"
                      + "process.stdout.write(JSON.stringify(cases.map(hintFor)));\n",
                      encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    for (message, expected), actual in zip(HINT_CASES, json.loads(out)):
        assert actual == expected, f"{message!r} -> {actual!r}, want {expected!r}"


# ---- build / verifier registration ------------------------------------------------------------

def test_native_test_target_is_registered_everywhere() -> None:
    cmake = source("native_orion/CMakeLists.txt")
    assert "add_executable(OrionRememberedSignInTests\n        tests/RememberedSignInTests.cpp" in cmake
    assert "orion_add_qtest(OrionRememberedSignInTests)" in cmake
    verify = source("scripts/verify_orion.ps1")
    build_lines = [line for line in verify.splitlines() if "--target OrionNative " in line]
    assert len(build_lines) == 2
    for line in build_lines:
        assert " OrionRememberedSignInTests " in line
    tests = source("native_orion/tests/RememberedSignInTests.cpp")
    # File tests never run against the real per-user data dir.
    assert "#if defined(ORION_PRODUCTION_BUILD)" in tests and "QSKIP(" in tests
    assert "QTemporaryDir dir_;" in tests
