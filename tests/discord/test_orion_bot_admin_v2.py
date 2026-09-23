"""Gateway-bot half of Admin Panel V2 (docs/ADMIN_PANEL_V2_CONTRACT.md §3/§4/§5/§7).

Offline: the bot module is imported from its path with dummy env; `client.run` is
__main__-guarded so nothing connects. Every /api/* call goes through `lambda_post`
/ `staff_post`, which these tests replace with recorders — so the assertions are
about PAYLOAD SHAPE (what the backend agent has to accept) and about the reply the
customer/staff member actually sees.

What is pinned here:
  §3  the HWID self-service state machine: free / paid / deduct, and every refusal
      code (cooldown, locked, payment_required, insufficient_time, paid_only).
      payment_required is the ONLY one that offers the confirm button, and the
      button re-calls with mode="deduct".
  §4  every bot route carries actor_discord_id = the invoking user; /deliver and
      /keygen carry a reason and are NOT gated on the Discord ADMINISTRATOR bit.
  §5  the bot never writes the killswitch.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "unit-dummy-token")
os.environ.setdefault("ORION_BOT_SECRET", "unit-dummy-bot-secret")
os.environ.setdefault("ORION_GUILD_ID", "123456789012345678")

_MODULE_PATH = Path(__file__).resolve().parents[2] / "discord_launch" / "orion_bot.py"
_spec = importlib.util.spec_from_file_location("orion_bot_adminv2", _MODULE_PATH)
bot = importlib.util.module_from_spec(_spec)
sys.modules["orion_bot_adminv2"] = bot
_spec.loader.exec_module(bot)

ACTOR = 111111111111111111
TARGET = 222222222222222222


def _run(coro):
    return asyncio.run(coro)


# ── fake Discord objects ──────────────────────────────────────────────────────

class FakeRole:
    def __init__(self, rid):
        self.id = rid


class FakeUser:
    def __init__(self, uid, roles=()):
        self.id = uid
        self.roles = [FakeRole(r) for r in roles]
        self.mention = f"<@{uid}>"
        self.dms = []

    async def send(self, *a, **kw):
        self.dms.append((a, kw))


class FakeResponse:
    def __init__(self, sink):
        self.sink = sink
        self.deferred = False

    async def defer(self, **kw):
        self.deferred = True

    async def send_message(self, content=None, **kw):
        self.sink.append({"where": "response", "content": content, **kw})

    async def edit_message(self, content=None, **kw):
        self.sink.append({"where": "edit", "content": content, **kw})


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append({"where": "followup", "content": content, **kw})


class FakeInteraction:
    def __init__(self, uid=ACTOR, roles=()):
        self.sent = []
        self.user = FakeUser(uid, roles)
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup(self.sent)
        self.guild = None          # role grants are best-effort / try-excepted

    @property
    def last(self):
        return self.sent[-1]

    @property
    def text(self):
        return str(self.sent[-1].get("content") or "")

    async def edit_original_response(self, **kw):
        self.sent.append({"where": "edit_original", **kw})


class Recorder:
    """Stands in for lambda_post / staff_post: records (path, payload), replies
    from a per-path script (last reply repeats)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    async def __call__(self, path, payload):
        self.calls.append((path, payload))
        replies = self.script.get(path) or [(200, {"ok": True})]
        return replies.pop(0) if len(replies) > 1 else replies[0]

    def payload(self, path):
        return next(p for (q, p) in self.calls if q == path)

    def paths(self):
        return [p for (p, _) in self.calls]


def cmd(name):
    c = bot.tree.get_command(name, guild=bot.GUILD)
    assert c is not None, f"/{name} is not registered"
    return c


# ── surface ───────────────────────────────────────────────────────────────────

def test_every_contract_command_is_registered():
    names = {c.name for c in bot.tree.get_commands(guild=bot.GUILD)}
    assert {"purchase", "claim_trial", "hwid_reset", "status", "setup", "faq",
            "deliver", "keygen", "lookup"} <= names


def test_json_registration_matches_the_gateway_bots_tree():
    """discord_commands.json registers the WORKER's commands; orion_bot.py syncs
    its own tree. The two front-ends must present the same surface, or a user who
    learns `/keygen plan: reason:` on one gets "unknown option" on the other."""
    import json
    root = _MODULE_PATH.parent
    spec = json.loads((root / "discord_commands.json").read_text(encoding="utf-8"))
    by_name = {c["name"]: c for c in spec}

    worker_only = set()
    bot_only = {"status", "setup", "faq"}   # need a bot token / cached staff session
    tree_names = {c.name for c in bot.tree.get_commands(guild=bot.GUILD)}
    assert set(by_name) - worker_only == tree_names - bot_only

    for name in set(by_name) - worker_only:
        cmd_obj = cmd(name)
        json_opts = [(o["name"], bool(o.get("required"))) for o in by_name[name].get("options", [])]
        tree_opts = [(p.name, p.required) for p in cmd_obj.parameters]
        assert json_opts == tree_opts, f"/{name} options drifted between the two front-ends"
        # staff commands are hidden by default in BOTH front-ends (visibility only)
        hidden_json = by_name[name].get("default_member_permissions") == "0"
        hidden_tree = getattr(cmd_obj, "default_permissions", None) is not None
        assert hidden_json == hidden_tree, f"/{name} default visibility differs"


def test_purchase_is_ephemeral_single_plan_and_uses_store_url():
    i = FakeInteraction()
    _run(cmd("purchase").callback(i))
    reply = i.last
    assert reply["ephemeral"] is True
    embed = reply["embed"]
    # [2026-09-21 owner] $19.99/mo, final. Pinned on purpose
    # so an accidental price drift in the bot copy fails here.
    assert embed.title == "🛒 Venice — $19.99 / month"
    assert "Discord ID" in embed.description
    assert "#create-ticket" in embed.description
    assert reply["view"].children[0].url == "https://zaeorion.com/#pricing"


def test_routes_are_the_documented_ones():
    assert bot.ROUTE_STATUS == "/api/bot/status"
    assert bot.ROUTE_HWID_RESET == "/api/bot/hwid-reset"
    assert bot.ROUTE_TRIAL == "/api/bot/trial"
    assert bot.ROUTE_DELIVER == "/api/bot/deliver"
    assert bot.ROUTE_STAFF_LIC == "/api/staff/license"


def test_killswitch_is_read_only_everywhere(monkeypatch):
    """Contract §5 BREAKING: the bot may READ killswitch status and nothing else.
    Revert caught: re-add an `action: "enable"/"disable"` call site and the
    payload assertion below fails."""
    assert bot.killswitch_status_payload() == {"action": "status", "by": "selfcheck"}
    rec = Recorder({bot.ROUTE_KILLSWITCH: [(200, {"ok": True, "enabled": False})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    _run(bot.backend_selfcheck())
    assert rec.paths() == [bot.ROUTE_KILLSWITCH]
    assert rec.payload(bot.ROUTE_KILLSWITCH)["action"] == "status"

    src = _MODULE_PATH.read_text(encoding="utf-8")
    for write_action in ('"action": "enable"', '"action": "disable"',
                         '"action": "kill"', '"action": "unkill"'):
        assert write_action not in src, f"killswitch write action {write_action} is back"


# ── §4 actor forwarding ───────────────────────────────────────────────────────

def test_every_bot_payload_builder_carries_the_actor():
    assert bot.hwid_reset_payload(ACTOR)["actor_discord_id"] == ACTOR
    assert bot.deliver_payload(ACTOR, TARGET, "month", "r")["actor_discord_id"] == ACTOR
    assert bot.keygen_payload(ACTOR, "month", "r")["actor_discord_id"] == ACTOR
    assert bot.staff_lookup_payload(ACTOR, "K")["actor_discord_id"] == ACTOR


def test_claim_trial_and_hwid_reset_forward_the_actor(monkeypatch):
    rec = Recorder({bot.ROUTE_TRIAL: [(200, {"ok": True, "message": "sent"})],
                    bot.ROUTE_HWID_RESET: [(200, {"ok": True, "mode": "free"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("claim_trial").callback(i))
    _run(cmd("hwid_reset").callback(FakeInteraction()))
    for path in (bot.ROUTE_TRIAL, bot.ROUTE_HWID_RESET):
        p = rec.payload(path)
        assert p["discord_id"] == ACTOR and p["actor_discord_id"] == ACTOR


def test_deliver_payload_shape():
    p = bot.deliver_payload(ACTOR, TARGET, "MONTH", "chargeback goodwill", days=45)
    assert p == {"discord_id": TARGET, "plan": "month", "reason": "chargeback goodwill",
                 "actor_discord_id": ACTOR, "days": 45}
    # days is omitted (not zero) when not overridden, so the server keeps its default
    assert "days" not in bot.deliver_payload(ACTOR, TARGET, "month", "r")


def test_keygen_payload_shape():
    p = bot.keygen_payload(ACTOR, "Lifetime", "reseller batch", count=5, days=0, note="n")
    assert p == {"plan": "lifetime", "count": 5, "reason": "reseller batch",
                 "actor_discord_id": ACTOR, "note": "n"}
    assert "discord_id" not in p, "/keygen mints UNASSIGNED keys — no recipient"


def test_staff_lookup_payload_sends_both_key_spellings():
    p = bot.staff_lookup_payload(ACTOR, " abcd-efgh ")
    assert p["action"] == "lookup"
    assert p["license_key"] == "ABCD-EFGH" == p["key"]
    assert p["reason"]


# ── §3 HWID reset state machine ───────────────────────────────────────────────

@pytest.mark.parametrize("data,expect,offer", [
    ({"ok": True, "mode": "free", "resets_remaining": 2}, ["free reset", "remaining: **2**"], False),
    ({"ok": True, "mode": "paid", "hwid_paid_credits": 1}, ["staff credit used", "credits: **1**"], False),
    ({"ok": True, "mode": "deduct", "deduct_days": 1, "expiry": 1800000000},
     ["1 day deducted", "**1 day** was deducted", "Expires:"], False),
    ({"ok": False, "error": "cooldown", "retry_at": 1800000000}, ["⏳", "<t:1800000000:R>"], False),
    ({"ok": False, "error": "locked"}, ["🔒", "#create-ticket"], False),
    ({"ok": False, "error": "payment_required", "deduct_days": 1},
     ["used all 3 free PC resets", "1 day off your subscription"], True),
    ({"ok": False, "error": "trial_no_deduct"}, ["trial", "subscribe"], False),
    ({"ok": False, "error": "insufficient_time", "deduct_days": 1},
     ["Less than 1 day left"], False),
    ({"ok": False, "error": "paid_only"}, ["no expiry to take days from"], False),
])
def test_render_hwid_reset_covers_every_contract_state(data, expect, offer):
    text, offer_deduct = bot.render_hwid_reset(data)
    for frag in expect:
        assert frag in text, f"{frag!r} missing from {text!r}"
    assert offer_deduct is offer


@pytest.mark.parametrize("data", [
    {"ok": False, "error": "payment_required", "deduct_days": 1},
    {"ok": False, "error": "trial_no_deduct"},
    {"ok": False, "error": "insufficient_time", "deduct_days": 1},
    {"ok": False, "error": "paid_only"},
    {"ok": True, "mode": "free", "resets_remaining": 0},
])
def test_no_hwid_reply_ever_sells_a_reset(data):
    """Owner rule 2026-09-15: three free resets, then a day off the subscription.
    There is nothing to buy, so no reply may pitch one - including when the server
    is an older Lambda that still sends a price_url."""
    text, _ = bot.render_hwid_reset(dict(data, price_url="https://old.example/l/reset"))
    low = text.lower()
    assert "buy a reset" not in low
    assert "old.example" not in low
    assert "credit" not in low or "staff" in low


def test_penalty_days_is_still_read_from_an_older_lambda():
    """`deduct_days` is the new name; a Lambda deployed before this change sends
    only `penalty_days`. Both must render."""
    text, offer = bot.render_hwid_reset({"ok": False, "error": "payment_required",
                                         "penalty_days": 2})
    assert "2 days off your subscription" in text and offer is True


def test_only_payment_required_offers_the_confirm_button(monkeypatch):
    rec = Recorder({bot.ROUTE_HWID_RESET: [
        (200, {"ok": False, "error": "payment_required", "deduct_days": 1,
               "price_url": "https://x.test/l/r"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("hwid_reset").callback(i))
    view = i.last.get("view")
    assert isinstance(view, bot.HwidDeductView)
    labels = [getattr(c, "label", None) for c in view.children]
    assert any("Deduct 1 day" in (l or "") for l in labels)
    assert "Cancel" in labels
    assert "Buy a reset" not in labels, "customers cannot buy a reset any more"
    assert all(getattr(c, "url", None) is None for c in view.children), \
        "no link button may survive on the confirm view"
    # the first call must NOT carry a mode — the server decides, the user confirms
    assert "mode" not in rec.payload(bot.ROUTE_HWID_RESET)


def test_confirm_button_recalls_with_mode_deduct(monkeypatch):
    rec = Recorder({bot.ROUTE_HWID_RESET: [
        (200, {"ok": True, "mode": "deduct", "deduct_days": 1, "expiry": 1800000000})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    view = bot.HwidDeductView(ACTOR, 1)
    i = FakeInteraction()
    _run(view._deduct(i))
    p = rec.payload(bot.ROUTE_HWID_RESET)
    assert p["mode"] == "deduct" and p["actor_discord_id"] == ACTOR
    assert "1 day deducted" in str(i.sent[-1].get("content"))
    assert i.sent[-1].get("view") is None, "the confirm buttons must be cleared"


def test_confirm_button_is_bound_to_the_invoker(monkeypatch):
    rec = Recorder({bot.ROUTE_HWID_RESET: [(200, {"ok": True, "mode": "deduct"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    view = bot.HwidDeductView(ACTOR, 3)
    stranger = FakeInteraction(uid=999)
    _run(view._deduct(stranger))
    assert "isn't yours" in stranger.text
    assert rec.calls == [], "a stranger's click must not reach the backend"


def test_cancel_deducts_nothing(monkeypatch):
    rec = Recorder({})
    monkeypatch.setattr(bot, "lambda_post", rec)
    view = bot.HwidDeductView(ACTOR, 3)
    i = FakeInteraction()
    _run(view._cancel(i))
    assert "Cancelled" in i.text and rec.calls == []


def test_the_buy_a_reset_plumbing_is_gone():
    """The env var, the URL builder and the link button all went with the rule
    change; a re-introduction should be a deliberate edit, not a silent revert."""
    assert not hasattr(bot, "HWID_RESET_BUY_URL")
    assert not hasattr(bot, "_buy_url")


def test_plan_pickers_offer_month_and_never_advertise_lifetime():
    """Owner rule 2026-09-15. /deliver sells month only; /keygen keeps lifetime so
    staff can mint a comp - but the backend must still ACCEPT the legacy values,
    which is pinned in tests/backend/test_plan_surface.py."""
    deliver_values = [c.value for c in bot.DELIVER_PLAN_CHOICES]
    keygen_values = [c.value for c in bot.KEYGEN_PLAN_CHOICES]
    assert deliver_values == ["month"]
    assert keygen_values[0] == "month", "month is the default/first choice"
    assert set(keygen_values) == {"month", "lifetime"}
    assert "week" not in keygen_values and "day" not in keygen_values
    comp = [c.name for c in bot.KEYGEN_PLAN_CHOICES if c.value == "lifetime"][0]
    assert "never sold" in comp, "lifetime must read as a staff comp, not a tier"


# ── §4 /deliver: no ADMINISTRATOR bit, server decides ─────────────────────────

def test_deliver_requires_a_reason(monkeypatch):
    rec = Recorder({})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("deliver").callback(i, FakeUser(TARGET), "month", "   "))
    assert "reason" in i.text and rec.calls == []
    long_reason = "x" * (bot.MAX_REASON + 1)
    j = FakeInteraction()
    _run(cmd("deliver").callback(j, FakeUser(TARGET), "month", long_reason))
    assert str(bot.MAX_REASON) in j.text and rec.calls == []


def test_deliver_forwards_actor_and_dms_the_key(monkeypatch):
    rec = Recorder({bot.ROUTE_DELIVER: [(200, {"ok": True, "license_key": "AAAA-BBBB", "plan": "month"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    target = FakeUser(TARGET)
    _run(cmd("deliver").callback(i, target, "month", "manual comp"))
    p = rec.payload(bot.ROUTE_DELIVER)
    assert p == {"discord_id": TARGET, "plan": "month", "reason": "manual comp",
                 "actor_discord_id": ACTOR}
    assert target.dms, "the recipient must be DM'd the key"
    assert "Delivered" in i.text


def test_deliver_renders_the_servers_forbidden_nicely(monkeypatch):
    rec = Recorder({bot.ROUTE_DELIVER: [(403, {"ok": False, "error": "forbidden"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("deliver").callback(i, FakeUser(TARGET), "month", "r"))
    assert "limited to staff" in i.text
    assert "ORION_EDGE_AUTH" in i.text, "403 is ambiguous with edge auth — say so"


def test_deliver_does_not_read_the_administrator_bit():
    """Contract §4 BREAKING. Revert caught: restore
    `interaction.user.guild_permissions.administrator` and this fails."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "guild_permissions.administrator" not in src


def test_deliver_reports_a_dm_bounce_instead_of_losing_the_key(monkeypatch):
    rec = Recorder({bot.ROUTE_DELIVER: [(200, {"ok": True, "license_key": "AAAA-BBBB"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    import discord as _d
    target = FakeUser(TARGET)

    async def boom(*a, **kw):
        raise _d.Forbidden(types.SimpleNamespace(status=403, reason="closed"), "DMs closed")

    target.send = boom
    i = FakeInteraction()
    _run(cmd("deliver").callback(i, target, "month", "r"))
    assert "AAAA-BBBB" in i.text and "couldn't DM" in i.text


# ── /keygen ───────────────────────────────────────────────────────────────────

def test_keygen_forwards_count_and_reason(monkeypatch):
    rec = Recorder({bot.ROUTE_DELIVER: [
        (200, {"ok": True, "keys": ["K1", "K2", "K3"], "plan": "week"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("keygen").callback(i, "week", "giveaway", count=3))
    p = rec.payload(bot.ROUTE_DELIVER)
    assert p["count"] == 3 and p["reason"] == "giveaway" and p["actor_discord_id"] == ACTOR
    assert "K1" in i.text and "K3" in i.text


def test_keygen_caps_count_at_the_contract_limit(monkeypatch):
    rec = Recorder({})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("keygen").callback(i, "week", "too many", count=bot.MAX_KEYGEN_COUNT + 1))
    assert str(bot.MAX_KEYGEN_COUNT) in i.text and rec.calls == []


def test_keygen_says_so_when_the_server_ignores_count(monkeypatch):
    """`count` is an ASSUMED extension of /api/bot/deliver — if the backend drops
    it, the staff member must be told rather than silently shorted."""
    rec = Recorder({bot.ROUTE_DELIVER: [(200, {"ok": True, "license_key": "ONLY-ONE"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("keygen").callback(i, "week", "batch", count=5))
    assert "asked for 5" in i.text and "ONLY-ONE" in i.text


def test_keygen_refusal_is_the_staff_message(monkeypatch):
    rec = Recorder({bot.ROUTE_DELIVER: [(403, {"ok": False, "error": "capability_denied",
                                               "message": "support cannot create keys"})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("keygen").callback(i, "week", "batch"))
    assert "limited to staff" in i.text and "support cannot create keys" in i.text


# ── /lookup ───────────────────────────────────────────────────────────────────

def test_lookup_self_uses_bot_status(monkeypatch):
    rec = Recorder({bot.ROUTE_STATUS: [
        (200, {"ok": True, "has_license": True, "plan": "month", "expiry": 1800000000,
               "active": True, "bound": True})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    i = FakeInteraction()
    _run(cmd("lookup").callback(i))
    p = rec.payload(bot.ROUTE_STATUS)
    assert p == {"discord_id": ACTOR, "actor_discord_id": ACTOR}
    embed = i.last["embed"]
    values = {f.name: f.value for f in embed.fields}
    assert values["Plan"] == "month" and "Active" in values["Status"]


def test_lookup_other_user_is_staff_gated(monkeypatch):
    rec = Recorder({bot.ROUTE_STATUS: [(200, {"ok": True, "has_license": False})]})
    monkeypatch.setattr(bot, "lambda_post", rec)
    monkeypatch.setattr(bot, "STAFF_ROLE_IDS", {777})
    i = FakeInteraction()                      # no staff role
    _run(cmd("lookup").callback(i, FakeUser(TARGET)))
    assert "staff-only" in i.text and rec.calls == []

    j = FakeInteraction(roles=(777,))          # staff role present
    _run(cmd("lookup").callback(j, FakeUser(TARGET)))
    assert rec.payload(bot.ROUTE_STATUS)["discord_id"] == TARGET
    assert rec.payload(bot.ROUTE_STATUS)["actor_discord_id"] == ACTOR


def test_lookup_by_key_uses_the_staff_route(monkeypatch):
    lam = Recorder({})
    staff = Recorder({bot.ROUTE_STAFF_LIC: [
        (200, {"ok": True, "license": {"license_key_suffix": "K7QT", "status": "active",
                                       "plan": "month", "expiry": 1800000000, "activations": 1}})]})
    monkeypatch.setattr(bot, "lambda_post", lam)
    monkeypatch.setattr(bot, "staff_post", staff)
    monkeypatch.setattr(bot, "STAFF_ROLE_IDS", {777})
    i = FakeInteraction(roles=(777,))
    _run(cmd("lookup").callback(i, None, "abcd-efgh"))
    p = staff.payload(bot.ROUTE_STAFF_LIC)
    assert p["action"] == "lookup" and p["license_key"] == "ABCD-EFGH"
    assert lam.calls == [], "a key lookup must never go out on the bot-secret route"
    assert "…K7QT" in i.last["embed"].title


def test_lookup_by_key_refuses_non_staff(monkeypatch):
    staff = Recorder({})
    monkeypatch.setattr(bot, "staff_post", staff)
    monkeypatch.setattr(bot, "STAFF_ROLE_IDS", {777})
    i = FakeInteraction()
    _run(cmd("lookup").callback(i, None, "ABCD"))
    assert "staff-only" in i.text and staff.calls == []


def test_lookup_by_key_degrades_when_no_staff_identity(monkeypatch):
    async def unconfigured(path, payload):
        return -1, {}

    monkeypatch.setattr(bot, "staff_post", unconfigured)
    monkeypatch.setattr(bot, "STAFF_ROLE_IDS", {777})
    i = FakeInteraction(roles=(777,))
    _run(cmd("lookup").callback(i, None, "ABCD"))
    assert "ORION_STAFF_ID" in i.text
