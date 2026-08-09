"""Offline tests for the Discord bot's support-automation commands
(/status, /setup, /faq) added per docs/GO_TO_MARKET_DESIGN.md §3.

discord_launch/orion_bot.py reads its env at import and (now guarded behind
__main__) starts the gateway client — these tests set dummy env vars, import
the module from its path, and exercise the pure builders (status_embed,
faq_embed, SetupView) plus the staff-session HTTP logic with api_post stubbed.
No network, no Discord connection.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

# ── import the bot module with dummy env (never a real token) ────────────────
os.environ.setdefault("DISCORD_BOT_TOKEN", "unit-dummy-token")
os.environ.setdefault("ORION_BOT_SECRET", "unit-dummy-bot-secret")
os.environ.setdefault("ORION_GUILD_ID", "123456789012345678")

_MODULE_PATH = Path(__file__).resolve().parents[1] / "discord_launch" / "orion_bot.py"
_spec = importlib.util.spec_from_file_location("orion_bot_module", _MODULE_PATH)
bot = importlib.util.module_from_spec(_spec)
sys.modules["orion_bot_module"] = bot
_spec.loader.exec_module(bot)          # must NOT connect: client.run is __main__-guarded

NOW = int(time.time())


def _lic(**over):
    base = {
        "license_key_suffix": "K7QT",
        "status": "active",
        "revoked": False,
        "plan": "month",
        "expiry": NOW + 20 * 86400,
        "activations": 1,
        "machine_suffix": "9F3A",
    }
    base.update(over)
    return base


# ── command registration ─────────────────────────────────────────────────────

def test_support_commands_registered_alongside_existing():
    names = {c.name for c in bot.tree.get_commands(guild=bot.GUILD)}
    assert {"status", "setup", "faq"} <= names
    # the pre-existing surface must survive the additions
    assert {"claim_trial", "hwid_reset", "deliver"} <= names


def test_faq_choices_cover_every_topic():
    cmd = bot.tree.get_command("faq", guild=bot.GUILD)
    topic_param = next(p for p in cmd.parameters if p.name == "topic")
    assert {c.value for c in topic_param.choices} == set(bot.FAQ_TOPICS)


# ── status_embed (safe-field rendering) ───────────────────────────────────────

def test_status_embed_active_month():
    e = bot.status_embed(_lic(), now=NOW)
    fields = {f.name: f.value for f in e.fields}
    assert "✅ Active" in fields["Status"]
    assert fields["Plan"] == "Month"
    assert f"<t:{_lic()['expiry']}:F>" in fields["Expires"]
    assert "…9F3A" in fields["Machine"]
    assert e.colour.value == bot.EMBED_COLOR
    assert "…K7QT" in e.title


def test_status_embed_lifetime_never_expires():
    e = bot.status_embed(_lic(plan="lifetime", expiry=0), now=NOW)
    fields = {f.name: f.value for f in e.fields}
    assert "✅ Active" in fields["Status"]
    assert "Never" in fields["Expires"]


def test_status_embed_expired():
    e = bot.status_embed(_lic(expiry=NOW - 60), now=NOW)
    fields = {f.name: f.value for f in e.fields}
    assert "⌛ Expired" in fields["Status"]


def test_status_embed_revoked_beats_everything():
    # revoked flag OR status=="revoked", even with a future expiry
    for over in ({"revoked": True}, {"status": "revoked"}):
        e = bot.status_embed(_lic(**over), now=NOW)
        fields = {f.name: f.value for f in e.fields}
        assert "❌ Revoked" in fields["Status"]


def test_status_embed_unbound_machine():
    e = bot.status_embed(_lic(machine_suffix=None, activations=0), now=NOW)
    fields = {f.name: f.value for f in e.fields}
    assert "Not activated" in fields["Machine"]
    assert fields["Activations"] == "0"


def test_status_embed_never_contains_more_than_key_suffix():
    # the lookup contract returns ONLY the suffix; the embed must not fabricate more
    e = bot.status_embed(_lic(), now=NOW)
    blob = (e.title or "") + "".join(f.value for f in e.fields)
    assert "K7QT" in blob and "XXXX-K7QT" not in blob


# ── FAQ embeds ────────────────────────────────────────────────────────────────

def test_faq_embeds_all_topics_fit_discord_limits():
    for key in bot.FAQ_TOPICS:
        e = bot.faq_embed(key)
        assert e.title, key
        assert e.description and len(e.description) <= 4096, key
        assert e.colour.value == bot.EMBED_COLOR


def test_setup_view_has_one_button_per_killer_failure():
    view = bot.SetupView()
    buttons = [c for c in view.children if c.type.name == "button"]
    assert {b.custom_id for b in buttons} == {
        "setup:capture_card", "setup:remote_play", "setup:no_greens"}
    # every button topic must exist in the FAQ so the callback can't KeyError
    for b in buttons:
        assert b.custom_id.split(":", 1)[1] in bot.FAQ_TOPICS


# ── staff session (auth layer for /status) ────────────────────────────────────

class _ScriptedApi:
    """Stubs bot.api_post with a per-path script of (status, body) replies."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    async def __call__(self, path, payload, headers):
        self.calls.append((path, payload, dict(headers)))
        replies = self.script[path]
        return replies.pop(0) if len(replies) > 1 else replies[0]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def staff_env(monkeypatch):
    monkeypatch.setattr(bot, "STAFF_ID", "orion-bot")
    monkeypatch.setattr(bot, "STAFF_MACHINE_ID", "unit-machine")
    monkeypatch.setitem(bot._staff_session, "token", "")
    monkeypatch.setitem(bot._staff_session, "expires", 0)
    return monkeypatch


def test_staff_post_unconfigured_returns_sentinel(monkeypatch):
    monkeypatch.setattr(bot, "STAFF_ID", "")
    st, data = _run(bot.staff_post("/api/staff/license", {}))
    assert st == -1 and data == {}


def test_staff_post_logs_in_then_sends_bearer(staff_env):
    api = _ScriptedApi({
        "/api/staff/login": [(200, {"ok": True, "token": "tok-1", "expires": NOW + 3600})],
        "/api/staff/license": [(200, {"ok": True, "license": _lic()})],
    })
    staff_env.setattr(bot, "api_post", api)
    st, data = _run(bot.staff_post("/api/staff/license", {"action": "lookup", "license_key": "K"}))
    assert st == 200 and data["ok"]
    lookup_calls = [c for c in api.calls if c[0] == "/api/staff/license"]
    assert lookup_calls[0][2]["Authorization"] == "Bearer tok-1"
    assert lookup_calls[0][2]["X-Machine-Id"] == "unit-machine"
    # login payload carries the replay-window fields the lambda checks
    login_payload = next(c[1] for c in api.calls if c[0] == "/api/staff/login")
    assert {"staff_id", "machine_id", "nonce", "timestamp"} <= set(login_payload)


def test_staff_post_relogin_once_on_403(staff_env):
    api = _ScriptedApi({
        "/api/staff/login": [
            (200, {"ok": True, "token": "tok-old", "expires": NOW + 3600}),
            (200, {"ok": True, "token": "tok-new", "expires": NOW + 3600}),
        ],
        "/api/staff/license": [
            (403, {"ok": False, "error": "token_expired"}),
            (200, {"ok": True, "license": _lic()}),
        ],
    })
    staff_env.setattr(bot, "api_post", api)
    st, data = _run(bot.staff_post("/api/staff/license", {"action": "lookup", "license_key": "K"}))
    assert st == 200 and data["ok"]
    lookup_auth = [c[2]["Authorization"] for c in api.calls if c[0] == "/api/staff/license"]
    assert lookup_auth == ["Bearer tok-old", "Bearer tok-new"]


def test_staff_post_gives_up_when_relogin_fails(staff_env):
    api = _ScriptedApi({
        "/api/staff/login": [
            (200, {"ok": True, "token": "tok-old", "expires": NOW + 3600}),
            (403, {"ok": False, "error": "invalid_credentials"}),
        ],
        "/api/staff/license": [(403, {"ok": False, "error": "token_expired"})],
    })
    staff_env.setattr(bot, "api_post", api)
    st, _ = _run(bot.staff_post("/api/staff/license", {"action": "lookup", "license_key": "K"}))
    assert st == -1


def test_staff_headers_carry_edge_auth_when_configured(staff_env):
    staff_env.setattr(bot, "EDGE_AUTH", "edge-secret")
    api = _ScriptedApi({
        "/api/staff/login": [(200, {"ok": True, "token": "t", "expires": NOW + 3600})],
        "/api/staff/license": [(200, {"ok": True, "license": _lic()})],
    })
    staff_env.setattr(bot, "api_post", api)
    _run(bot.staff_post("/api/staff/license", {"action": "lookup", "license_key": "K"}))
    assert all(c[2].get("X-Edge-Auth") == "edge-secret" for c in api.calls)
