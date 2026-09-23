"""rc1 RT-CRIT-01 client half (P-F): owner TOTP step-up in the admin clients.

The backend (backend/lambda_function.py owner_step_up / handle_staff_login) now
needs a FRESH single-use owner TOTP code for owner-sensitive actions and for an
owner-role staff sign-in while owner TOTP is required. These tests pin the client
side:

* tools/admin/orion_admin.py (behavioural, FakeTransport, offline):
  - X-Orion-Admin-TOTP is sent on an owner-role bearer request EXACTLY when the
    action is owner-sensitive, never on ordinary requests or staff routes;
  - the code is asked for per request, used once, never saved or printed;
  - body ``totp_code`` is sent at staff sign-in only with ``--owner``;
  - replayed/wrong codes surface as one plain message;
  - break-glass (admin secret) requests are unchanged.
* native_orion AdminToolController + qml/admin (source contract; the native
  build is done centrally): the same rules in C++/QML.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = ROOT / "tools" / "admin" / "orion_admin.py"
_spec = importlib.util.spec_from_file_location("orion_admin_stepup", _MODULE_PATH)
admin = importlib.util.module_from_spec(_spec)
sys.modules["orion_admin_stepup"] = admin
_spec.loader.exec_module(admin)

CODE = "482913"
TOTP_HEADER = "X-Orion-Admin-TOTP"
PLAIN = "That code was already used or is wrong — wait for the next code."


class FakeTransport(admin.Transport):
    def __init__(self, responses=None, default=None):
        self.responses = responses or []
        self.default = default if default is not None else admin.ApiResult(True, 200, {"ok": True})
        self.calls = []

    def request(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(json.dumps(body)) if body is not None else None})
        for matcher, result in self.responses:
            if matcher(method, url, body):
                return result
        return self.default

    def path(self, index):
        return self.calls[index]["url"].split("?", 1)[0].replace(admin.DEFAULT_BASE_URL, "")


class Prompter:
    def __init__(self, answer=CODE):
        self.answer = answer
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answer


BEARER_OWNER = {"admin_secret": "", "staff_token": "tok-owner", "role": "owner",
                "discord_user_id": "77", "staff_id": "staff_owner"}
BREAK_GLASS = {"admin_secret": "owner-secret", "role": "owner"}


def run_cli(argv, config, transport=None, prompter=None, confirm=True):
    transport = transport or FakeTransport()
    client = admin.OrionAdminClient(admin.AdminConfig(**config), transport, code_prompter=prompter)
    lines = []
    rc = admin.run(admin.build_parser().parse_args(argv), client, lambda s: lines.append(str(s)),
                   lambda _p: confirm)
    return rc, lines, transport


def staff_list_with(role):
    listing = {"ok": True, "staff": [{"staff_id": "staff_x", "role": role}]}
    return FakeTransport([(lambda m, u, b: m == "GET" and u.endswith("/api/admin/staff"),
                           admin.ApiResult(True, 200, listing))])


def assert_code_not_leaked(lines, transport):
    assert all(CODE not in ln for ln in lines)
    for call in transport.calls:
        assert CODE not in json.dumps(call["body"] or {})


# --------------------------------------------------------------------------- #
# Policy mirrors the backend
# --------------------------------------------------------------------------- #

def _backend_step_up_keys():
    src = (ROOT / "backend" / "lambda_function.py").read_text(encoding="utf-8")
    m = re.search(r"^STEP_UP_CONFIG_KEYS\s*=\s*\(([^)]*)\)", src, re.M)
    assert m, "backend STEP_UP_CONFIG_KEYS not found"
    return tuple(re.findall(r'"([^"]+)"', m.group(1)))


def test_cli_step_up_keys_match_backend():
    assert tuple(admin.STEP_UP_CONFIG_KEYS) == _backend_step_up_keys()


def test_native_step_up_keys_match_backend():
    header = (ROOT / "native_orion" / "src" / "AdminToolController.h").read_text(encoding="utf-8")
    body = header.split("inline bool configPatchNeedsStepUp", 1)[1].split("inline bool staffCreateNeedsStepUp", 1)[0]
    for key in _backend_step_up_keys():
        assert f'QLatin1String("{key}")' in body


@pytest.mark.parametrize("body,expected", [
    ({"action": "totp_enroll"}, True),
    ({"action": "totp_confirm", "code": "1"}, True),
    ({"action": "totp_disable"}, True),
    ({"action": "rotate_admin_secret"}, True),
    ({"owner_totp_required": False, "reason": "r"}, True),
    ({"owner_ip_allowlist": [], "reason": "r"}, True),
    ({"alerts": {}, "reason": "r"}, True),
    ({"global_kill": False, "reason": "r"}, True),
    ({"global_kill": {"enabled": False}, "reason": "r"}, True),
    ({"global_kill": True, "reason": "r"}, False),          # engaging stays one step
    ({"motd": {"text": "x"}, "reason": "r"}, False),
    ({"min_client_version": "1.0.0", "reason": "r"}, False),
])
def test_config_body_needs_step_up(body, expected):
    assert admin.config_body_needs_step_up(body) is expected


@pytest.mark.parametrize("body,target,expected", [
    ({"action": "create", "role": "owner"}, None, True),
    ({"action": "create", "role": "admin"}, None, False),
    ({"action": "disable", "staff_id": "s"}, "owner", True),
    ({"action": "reissue_enrollment", "staff_id": "s"}, "owner", True),
    ({"action": "disable", "staff_id": "s"}, "support", False),
    ({"action": "set_role", "staff_id": "s", "role": "owner"}, "support", True),
    ({"action": "set_role", "staff_id": "s", "role": "admin"}, "support", False),
    ({"action": "disable", "staff_id": "s"}, None, True),     # unknown row: may be owner
])
def test_staff_body_needs_step_up(body, target, expected):
    assert admin.staff_body_needs_step_up(body, target) is expected


# --------------------------------------------------------------------------- #
# Owner-role bearer: the header is sent exactly when needed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("argv", [
    ["license", "lookup", "--key", "ORION-AAAA-BBBB-CCCC"],
    ["metrics"],
    ["server-config", "show"],
    ["server-config", "set", "--motd-text", "hello", "--reason", "notice"],
    ["--yes", "killswitch", "engage", "--reason", "incident"],
    ["audit"],
    ["staff", "list"],
    ["staff", "create", "--discord", "5", "--role", "support", "--reason", "hire"],
])
def test_bearer_ordinary_requests_never_send_totp_or_prompt(argv):
    prompter = Prompter()
    rc, _lines, t = run_cli(argv, BEARER_OWNER, prompter=prompter)
    assert rc == 0
    assert prompter.prompts == []
    assert t.calls
    for call in t.calls:
        assert TOTP_HEADER not in call["headers"]
        assert "totp_code" not in (call["body"] or {})
        assert "step_up_code" not in (call["body"] or {})


@pytest.mark.parametrize("argv", [
    ["server-config", "totp-enroll", "--reason", "re-enrol"],
    ["server-config", "totp-confirm", "--code", "111222", "--reason", "confirm"],
    ["--yes", "server-config", "rotate-secret", "--reason", "rotation"],
    ["server-config", "set", "--owner-totp-required", "false", "--reason", "off"],
    ["server-config", "set", "--ip-allowlist", "203.0.113.0/24", "--reason", "lock"],
    ["server-config", "set", "--alert-owner", "999", "--reason", "alerts"],
    ["killswitch", "release", "--reason", "over"],
    ["staff", "create", "--discord", "5", "--role", "owner", "--reason", "co-owner"],
])
def test_bearer_sensitive_config_and_owner_create_send_fresh_code_once(argv):
    prompter = Prompter()
    rotated = admin.ApiResult(True, 200, {"ok": True, "admin_secret": "new-secret"})
    rc, lines, t = run_cli(argv, BEARER_OWNER, transport=FakeTransport(default=rotated), prompter=prompter)
    assert rc == 0
    assert len(prompter.prompts) == 1
    posts = [c for c in t.calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["headers"][TOTP_HEADER] == CODE
    assert posts[0]["headers"]["Authorization"] == "Bearer tok-owner"
    assert "X-Orion-Admin-Secret" not in posts[0]["headers"]
    assert_code_not_leaked(lines, t)


def test_bearer_action_on_owner_row_looks_up_role_then_sends_code():
    prompter = Prompter()
    t = staff_list_with("owner")
    rc, lines, t = run_cli(["--yes", "staff", "reset-machine", "--staff-id", "staff_x", "--reason", "rebind"],
                           BEARER_OWNER, transport=t, prompter=prompter)
    assert rc == 0
    assert [c["method"] for c in t.calls] == ["GET", "POST"]
    assert TOTP_HEADER not in t.calls[0]["headers"]           # the lookup never carries it
    assert t.calls[1]["headers"][TOTP_HEADER] == CODE
    assert len(prompter.prompts) == 1
    assert_code_not_leaked(lines, t)


def test_bearer_action_on_support_row_sends_no_code():
    prompter = Prompter()
    rc, _lines, t = run_cli(["--yes", "staff", "disable", "--staff-id", "staff_x", "--reason", "left"],
                            BEARER_OWNER, transport=staff_list_with("support"), prompter=prompter)
    assert rc == 0
    assert prompter.prompts == []
    assert all(TOTP_HEADER not in c["headers"] for c in t.calls)


def test_bearer_promotion_to_owner_sends_code():
    prompter = Prompter()
    rc, _lines, t = run_cli(["staff", "set-role", "--staff-id", "staff_x", "--role", "owner", "--reason", "promote"],
                            BEARER_OWNER, transport=staff_list_with("support"), prompter=prompter)
    assert rc == 0
    assert t.calls[-1]["headers"][TOTP_HEADER] == CODE


def test_each_sensitive_request_asks_again_code_is_not_reused():
    prompter = Prompter()
    transport = FakeTransport()
    client = admin.OrionAdminClient(admin.AdminConfig(**BEARER_OWNER), transport, code_prompter=prompter)
    client.server_config_update({"action": "totp_disable", "reason": "a"})
    client.server_config_update({"global_kill": False, "reason": "b"})
    assert len(prompter.prompts) == 2
    assert client.config.admin_totp == ""                     # never cached on the config


def test_env_code_is_used_for_step_up_only():
    cfg = dict(BEARER_OWNER, admin_totp=CODE)                 # ORION_ADMIN_TOTP for this command
    transport = FakeTransport()
    client = admin.OrionAdminClient(admin.AdminConfig(**cfg), transport, code_prompter=None)
    client.metrics()
    client.server_config_update({"action": "rotate_admin_secret", "reason": "r"})
    assert TOTP_HEADER not in transport.calls[0]["headers"]
    assert transport.calls[1]["headers"][TOTP_HEADER] == CODE


def test_bearer_without_code_source_refuses_before_sending():
    rc, lines, t = run_cli(["server-config", "totp-enroll", "--reason", "x"], BEARER_OWNER, prompter=None)
    assert rc == 1
    assert t.calls == []
    assert any("ORION_ADMIN_TOTP" in ln for ln in lines)


def test_malformed_code_refuses_before_sending():
    rc, lines, t = run_cli(["server-config", "totp-enroll", "--reason", "x"], BEARER_OWNER,
                           prompter=Prompter("12ab"))
    assert rc == 1
    assert t.calls == []
    assert any("6-digit" in ln for ln in lines)


@pytest.mark.parametrize("error", ["totp_replayed", "invalid_totp"])
def test_replayed_or_wrong_code_is_one_plain_message(error):
    denied = admin.ApiResult(False, 403, {"ok": False, "error": error, "message": "server text", "step_up": True})
    rc, lines, t = run_cli(["killswitch", "release", "--reason", "over"], BEARER_OWNER,
                           transport=FakeTransport(default=denied), prompter=Prompter())
    assert rc == 1
    assert lines == [f"error: {PLAIN}"]
    assert_code_not_leaked(lines, t)


@pytest.mark.parametrize("error", ["step_up_required", "step_up_unavailable", "config_unavailable",
                                   "audit_unavailable", "totp_not_provisioned"])
def test_other_step_up_errors_are_plain(error):
    denied = admin.ApiResult(False, 403, {"ok": False, "error": error})
    rc, lines, _t = run_cli(["killswitch", "release", "--reason", "over"], BEARER_OWNER,
                            transport=FakeTransport(default=denied), prompter=Prompter())
    assert rc == 1
    assert lines == [f"error: {admin.STEP_UP_MESSAGES[error]}"]


# --------------------------------------------------------------------------- #
# Break-glass is unchanged; staff (non-owner) roles unaffected
# --------------------------------------------------------------------------- #

def test_break_glass_headers_unchanged_and_never_prompts():
    prompter = Prompter()
    cfg = dict(BREAK_GLASS, admin_totp="135790")
    rc, _lines, t = run_cli(["server-config", "totp-enroll", "--reason", "x"], cfg, prompter=prompter)
    assert rc == 0
    assert prompter.prompts == []
    headers = t.calls[0]["headers"]
    assert headers["X-Orion-Admin-Secret"] == "owner-secret"
    assert headers[TOTP_HEADER] == "135790"
    assert admin.build_auth_headers(admin.AdminConfig(**cfg)) == headers


def test_break_glass_without_totp_sends_no_totp_header():
    rc, _lines, t = run_cli(["killswitch", "release", "--reason", "over"], BREAK_GLASS, prompter=Prompter())
    assert rc == 0
    assert TOTP_HEADER not in t.calls[0]["headers"]


def test_support_staff_never_sends_totp():
    support = {"admin_secret": "", "staff_token": "tok", "role": "support", "discord_user_id": "42",
               "staff_id": "staff_abc"}
    prompter = Prompter()
    rc, _lines, t = run_cli(["license", "lookup", "--key", "ORION-AAAA-BBBB-CCCC"], support, prompter=prompter)
    assert rc == 0
    assert prompter.prompts == []
    assert t.path(0) == "/api/staff/license"
    assert TOTP_HEADER not in t.calls[0]["headers"]


def test_build_auth_headers_step_up_code_only_when_given():
    cfg = admin.AdminConfig(**BEARER_OWNER)
    assert TOTP_HEADER not in admin.build_auth_headers(cfg)
    assert admin.build_auth_headers(cfg, step_up_code=CODE)[TOTP_HEADER] == CODE


# --------------------------------------------------------------------------- #
# Staff sign-in
# --------------------------------------------------------------------------- #

LOGGED_IN = {"ok": True, "token": "tok-9", "staff": {"staff_id": "staff_owner", "role": "owner"}}


def test_staff_login_without_owner_flag_sends_no_code(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "admin_config.json")
    prompter = Prompter()
    rc, _lines, t = run_cli(["staff-login", "--staff-id", "staff_1"], {}, prompter=prompter,
                            transport=FakeTransport(default=admin.ApiResult(True, 200, LOGGED_IN)))
    assert rc == 0
    assert prompter.prompts == []
    assert "totp_code" not in t.calls[0]["body"]
    assert TOTP_HEADER not in t.calls[0]["headers"]


def test_staff_login_owner_flag_sends_body_code_once_and_never_saves_it(tmp_path, monkeypatch):
    path = tmp_path / "admin_config.json"
    monkeypatch.setattr(admin, "CONFIG_PATH", path)
    prompter = Prompter()
    rc, lines, t = run_cli(["staff-login", "--staff-id", "staff_owner", "--owner"], {}, prompter=prompter,
                           transport=FakeTransport(default=admin.ApiResult(True, 200, LOGGED_IN)))
    assert rc == 0
    assert len(prompter.prompts) == 1
    assert t.calls[0]["body"]["totp_code"] == CODE
    assert TOTP_HEADER not in t.calls[0]["headers"]
    assert CODE not in path.read_text(encoding="utf-8")
    assert all(CODE not in ln for ln in lines)


def test_staff_login_owner_account_without_flag_gets_a_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "admin_config.json")
    denied = admin.ApiResult(False, 403, {"ok": False, "error": "totp_required"})
    rc, lines, _t = run_cli(["staff-login", "--staff-id", "staff_owner"], {},
                            transport=FakeTransport(default=denied))
    assert rc == 1
    assert "--owner" in lines[-1]


def test_staff_login_replayed_code_is_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "admin_config.json")
    denied = admin.ApiResult(False, 403, {"ok": False, "error": "totp_replayed"})
    rc, lines, _t = run_cli(["staff-login", "--staff-id", "staff_owner", "--owner"], {},
                            transport=FakeTransport(default=denied), prompter=Prompter())
    assert rc == 1
    assert lines == [f"error: {PLAIN}"]


# --------------------------------------------------------------------------- #
# Native AdminToolController + qml/admin (source contract)
# --------------------------------------------------------------------------- #

NATIVE_H = (ROOT / "native_orion" / "src" / "AdminToolController.h").read_text(encoding="utf-8")
NATIVE_CPP = (ROOT / "native_orion" / "src" / "AdminToolController.cpp").read_text(encoding="utf-8")
QML_MAIN = (ROOT / "native_orion" / "qml" / "admin" / "AdminMain.qml").read_text(encoding="utf-8")
QML_LOGIN = (ROOT / "native_orion" / "qml" / "admin" / "AdminLoginPane.qml").read_text(encoding="utf-8")


def _function(src, signature):
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(signature)


def test_native_apply_auth_sends_totp_to_bearer_only_with_a_step_up_code():
    body = _function(NATIVE_CPP, "void AdminToolController::applyAuth(")
    bearer = body.split("} else if (!staffToken_.isEmpty()) {", 1)[1].split("break;", 1)[0]
    assert 'if (!stepUpCode.isEmpty()) {' in bearer
    assert 'request.setRawHeader("X-Orion-Admin-TOTP", stepUpCode.toUtf8());' in bearer
    staff_branch = body.split("case AuthKind::Staff:", 1)[1]
    assert "X-Orion-Admin-TOTP" not in staff_branch
    # break-glass unchanged: session code unless a fresh step-up code replaces it
    assert "stepUpCode.isEmpty() ? ownerTotp_ : stepUpCode" in body


@pytest.mark.parametrize("method,tag,predicate", [
    ("updateConfig(", "config_update", "configPatchNeedsStepUp(patch)"),
    ("totpEnroll(", "totp_enroll", 'configActionNeedsStepUp(QStringLiteral("totp_enroll"))'),
    ("totpConfirm(", "totp_confirm", 'configActionNeedsStepUp(QStringLiteral("totp_confirm"))'),
    ("rotateAdminSecret(", "rotate_secret", 'configActionNeedsStepUp(QStringLiteral("rotate_admin_secret"))'),
    ("createStaff(", "staff_create", "staffCreateNeedsStepUp(normalizedRole)"),
    ("staffAction(", "staff_action", "staffActionNeedsStepUp(act, staffRoleFor(id), extra)"),
])
def test_native_sensitive_actions_route_through_step_up(method, tag, predicate):
    body = _function(NATIVE_CPP, "void AdminToolController::" + method)
    assert f'sendOwnerAction(QStringLiteral("{tag}")' in body
    assert predicate in body
    assert "sendRequest(" not in body


def test_native_code_is_never_logged_or_kept():
    for call in re.findall(r"appendAdminDiagnostic\([^;]*;", NATIVE_CPP, re.S):
        for secret in ("digits", "stepUpCode", "ownerTotp_", "ownerCode", "code)", "totp_code"):
            assert secret not in call, call
    submit = _function(NATIVE_CPP, "void AdminToolController::submitStepUp(")
    assert 'context.insert(QStringLiteral("step_up"), true)' in submit
    assert "digits.clear();" in submit
    assert "stepUp_.body" not in submit or "digits" not in submit.split("stepUp_.body", 1)[1][:40]
    # the parked call holds the request, never the code
    struct = NATIVE_H.split("struct StepUpCall", 1)[1].split("};", 1)[0]
    assert "code" not in struct.lower()
    send = _function(NATIVE_CPP, "void AdminToolController::sendRequest(")
    assert 'pending.body.remove(QStringLiteral("totp_code"));' in send


def test_native_staff_login_sends_totp_code_only_when_typed():
    body = _function(NATIVE_CPP, "void AdminToolController::staffLogin(")
    assert 'if (!code.isEmpty()) {\n        body.insert(QStringLiteral("totp_code"), code);' in body
    assert "codeWellFormed(code)" in body
    assert "X-Orion-Admin-TOTP" not in body


def test_native_plain_message_for_replayed_or_wrong():
    assert 'That code was already used or is wrong \\u2014 wait for the next code.' in NATIVE_H
    plain = NATIVE_H.split("inline QString plainMessage", 1)[1].split("} // namespace admin_step_up", 1)[0]
    assert 'QLatin1String("totp_replayed")' in plain and 'QLatin1String("invalid_totp")' in plain
    handle = _function(NATIVE_CPP, "void AdminToolController::handleResponse(")
    assert "admin_step_up::plainMessage(code)" in handle
    assert "armStepUp(" in handle      # a wrong/replayed code re-opens the prompt


def test_native_session_end_drops_parked_action():
    reset = _function(NATIVE_CPP, "void AdminToolController::resetSessionData(")
    assert "clearStepUp();" in reset


def test_qml_step_up_popup_clears_the_code_on_every_path():
    popup = QML_MAIN.split("Popup {\n        id: stepUpPopup", 1)[1]
    assert "admin.submitStepUp(code)" in popup
    submit = popup.split("function submit()", 1)[1].split("}", 1)[0]
    assert submit.index('stepUpCode.text = ""') < submit.index("admin.submitStepUp(code)")
    assert 'onOpened: {\n            stepUpCode.text = ""' in popup
    assert 'onClosed: {\n            stepUpCode.text = ""' in popup
    assert "admin.cancelStepUp()" in popup
    assert "admin.stepUpPending" in popup


def test_qml_staff_login_passes_and_clears_owner_code():
    fn = QML_LOGIN.split("function submitStaff()", 1)[1].split("function submitOwner()", 1)[0]
    assert fn.index('ownerCodeField.text = ""') < fn.index("admin.staffLogin(staffIdField.text, code)")
    assert "admin.staffLogin(staffIdField.text)" not in QML_LOGIN
    # break-glass owner form unchanged
    assert "admin.ownerLogin(secretField.text, totpField.text)" in QML_LOGIN
