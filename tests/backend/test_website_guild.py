"""Website (zaeorion.com Worker) routes added 2026-09-19:

  POST /api/bot/guild-join    {discord_id, access_token}  -> auto-join at sign-in
  POST /api/bot/guild-member  {discord_id, purpose?}      -> live purchase gate

plus the machine-readable `code` on /api/bot/trial replies and its per-account
claim throttle. Both guild routes accept ONLY the website Worker's own secret.
"""
import io
import json
import time
import urllib.error

import pytest

from conftest import (invoke, audit_rows, no_dm, TEST_BOT_SECRET, TEST_WORKER_SECRET,
                      TEST_DISCORD_BOT, TEST_GUILD_ID)

WORKER_H = {"x-orion-bot-secret": TEST_WORKER_SECRET}
BOT_H = {"x-orion-bot-secret": TEST_BOT_SECRET}
DISCORD_ID = "123456789012345678"
ACCESS_TOKEN = "oauthAccessTokenFixture7Q9xZ2kLm4"


def _stub_discord(lf, monkeypatch, status, code=None):
    """Replace the Discord transport; record every (method, id, payload)."""
    calls = []

    def fake(method, discord_user_id, payload=None):
        calls.append((method, discord_user_id, payload))
        return status, code

    monkeypatch.setattr(lf, "_discord_guild_member_call", fake)
    return calls


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


def _stub_urlopen(monkeypatch, lf, *, status=None, error_status=None, error_body=b""):
    """Drive the REAL Discord helper with a fake HTTP transport."""
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        if error_status is not None:
            raise urllib.error.HTTPError(request.full_url, error_status, "error", {},
                                         io.BytesIO(error_body))
        return _FakeResponse(status)

    monkeypatch.setattr(lf.urllib.request, "urlopen", fake_urlopen)
    return seen


def _freeze_clock(lf, monkeypatch):
    """Fixed-window rate limits: pin the clock so a test never straddles a window."""
    frozen = int(time.time())
    monkeypatch.setattr(lf, "now_ts", lambda: frozen)


def _all_output(capsys, lf):
    captured = capsys.readouterr()
    return captured.out + captured.err + json.dumps(
        lf.audit_table().scan().get("Items", []), default=str)


# ── /api/bot/guild-join ───────────────────────────────────────────────────────
class TestGuildJoin:
    def test_only_the_website_worker_may_join_users(self, lf, monkeypatch):
        calls = _stub_discord(lf, monkeypatch, 201)
        body = {"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN}
        for headers in ({}, BOT_H, {"x-orion-bot-secret": "wrong"}):
            status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join", body=body, headers=headers)
            assert status == 403 and resp["error"] == "forbidden"
        assert calls == []
        denied = audit_rows(lf, "discord.guild_join")
        assert [r["result"] for r in denied] == ["forbidden_consumer"]
        assert denied[0]["actor_id"] == "/orion/bot_service_secret"
        # Edge auth still applies in front of the Worker secret.
        status, _, _ = invoke(lf, "POST", "/api/bot/guild-join", body=body, headers=WORKER_H, edge=False)
        assert status == 403 and calls == []

    @pytest.mark.parametrize("discord_status,code,http,result", [
        (201, None, 200, "joined"),
        (204, None, 200, "already_member"),
        (403, 50013, 502, "failed"),     # bot lacks Create Instant Invite
        (400, 30001, 502, "failed"),     # user is in too many servers
        (401, 50025, 502, "failed"),     # invalid OAuth2 access token
        (429, None, 502, "failed"),
        (0, None, 502, "failed"),        # transport failure / unset config
    ])
    def test_discord_status_mapping(self, lf, monkeypatch, discord_status, code, http, result):
        calls = _stub_discord(lf, monkeypatch, discord_status, code)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN},
                                 headers=WORKER_H)
        assert status == http
        assert resp["result"] == result
        if http == 200:
            assert resp == {"ok": True, "result": result}
        else:
            assert resp["ok"] is False and resp["error"] == "guild_join_failed"
            assert resp["discord_status"] == discord_status and resp["discord_code"] == code
        assert calls == [("PUT", DISCORD_ID, {"access_token": ACCESS_TOKEN})]
        [row] = audit_rows(lf, "discord.guild_join")
        assert row["result"] == result and row["target"] == DISCORD_ID
        assert ACCESS_TOKEN not in json.dumps(row, default=str)

    @pytest.mark.parametrize("discord_id", [
        "", "123", "12345678901234567a", "1" * 23, " 12345678901234567 x", "１２３４５６７８９０１２３４５６７８",
        "-12345678901234567",
    ])
    def test_rejects_malformed_discord_ids(self, lf, monkeypatch, discord_id):
        calls = _stub_discord(lf, monkeypatch, 201)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": discord_id, "access_token": ACCESS_TOKEN},
                                 headers=WORKER_H)
        assert status == 400 and resp["error"] == "invalid_discord_id"
        assert calls == []

    @pytest.mark.parametrize("token", ["", "short", "has space in the token value", "a" * 513,
                                       12345678901234567890, None, ["x" * 20], "tok\nen" * 4])
    def test_rejects_malformed_access_tokens(self, lf, monkeypatch, token):
        calls = _stub_discord(lf, monkeypatch, 201)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": DISCORD_ID, "access_token": token},
                                 headers=WORKER_H)
        assert status == 400 and resp["error"] == "invalid_access_token"
        assert calls == []

    def test_body_is_size_capped_and_must_be_an_object(self, lf, monkeypatch):
        calls = _stub_discord(lf, monkeypatch, 201)
        big = json.dumps({"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN, "pad": "x" * 5000})
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join", raw_body=big, headers=WORKER_H)
        assert status == 413 and resp["error"] == "payload_too_large"
        for raw in ("not json", "[1, 2]", '"string"'):
            status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join", raw_body=raw, headers=WORKER_H)
            assert status == 400 and resp["error"] == "invalid_json"
        assert calls == []

    def test_blacklisted_accounts_are_not_added(self, lf, monkeypatch):
        calls = _stub_discord(lf, monkeypatch, 201)
        lf.licenses_table().put_item(Item={
            "license_key": "BLACKLIST#discord#" + DISCORD_ID, "status": "blacklist",
            "revoked": True, "created_at": int(time.time())})
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN},
                                 headers=WORKER_H)
        assert status == 403 and resp["error"] == "not_allowed" and resp["result"] == "failed"
        assert calls == []

    def test_joins_are_rate_limited_per_account(self, lf, monkeypatch):
        _freeze_clock(lf, monkeypatch)
        calls = _stub_discord(lf, monkeypatch, 204)
        body = {"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN}
        results = [invoke(lf, "POST", "/api/bot/guild-join", body=body, headers=WORKER_H)[0]
                   for _ in range(lf.GUILD_JOIN_RATE_MAX + 1)]
        assert results[:-1] == [200] * lf.GUILD_JOIN_RATE_MAX
        assert results[-1] == 429
        assert len(calls) == lf.GUILD_JOIN_RATE_MAX

    def test_real_discord_request_and_the_token_is_never_logged(self, lf, monkeypatch, capsys):
        seen = _stub_urlopen(monkeypatch, lf, status=201)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN},
                                 headers=WORKER_H)
        assert status == 200 and resp == {"ok": True, "result": "joined"}
        [request] = seen
        assert request.get_method() == "PUT"
        assert request.full_url == f"https://discord.com/api/v10/guilds/{TEST_GUILD_ID}/members/{DISCORD_ID}"
        assert request.get_header("Authorization") == "Bot " + TEST_DISCORD_BOT
        assert request.get_header("Content-type") == "application/json"
        assert json.loads(request.data) == {"access_token": ACCESS_TOKEN}
        assert ACCESS_TOKEN not in _all_output(capsys, lf)

        # Worst case: Discord echoes the token in an error body. It still never
        # reaches stdout, the audit table or the reply.
        _stub_urlopen(monkeypatch, lf, error_status=401,
                      error_body=json.dumps({"message": f"bad token {ACCESS_TOKEN}",
                                             "code": 50025}).encode())
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN},
                                 headers=WORKER_H)
        assert status == 502 and resp["discord_status"] == 401 and resp["discord_code"] == 50025
        assert ACCESS_TOKEN not in json.dumps(resp)
        assert ACCESS_TOKEN not in _all_output(capsys, lf)

        # 204 already a member through the real transport.
        _stub_urlopen(monkeypatch, lf, status=204)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-join",
                                 body={"discord_id": DISCORD_ID, "access_token": ACCESS_TOKEN},
                                 headers=WORKER_H)
        assert status == 200 and resp["result"] == "already_member"
        assert ACCESS_TOKEN not in _all_output(capsys, lf)


# ── /api/bot/guild-member ─────────────────────────────────────────────────────
class TestGuildMember:
    def test_only_the_website_worker_may_check_membership(self, lf, monkeypatch):
        calls = _stub_discord(lf, monkeypatch, 200)
        for headers in ({}, BOT_H):
            status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                     body={"discord_id": DISCORD_ID}, headers=headers)
            assert status == 403 and resp["error"] == "forbidden"
        assert calls == []
        assert [r["result"] for r in audit_rows(lf, "discord.membership_check")] == ["forbidden_consumer"]

    @pytest.mark.parametrize("discord_status,code,http,member", [
        (200, None, 200, True),
        (404, 10007, 200, False),    # Unknown Member
        (404, 10013, 200, False),    # Unknown User
        (404, None, 200, False),
        (404, 10004, 502, None),     # Unknown Guild = misconfiguration, not "join"
        (403, 50001, 502, None),
        (401, 0, 502, None),
        (429, None, 502, None),
        (500, None, 502, None),
        (0, None, 502, None),
    ])
    def test_membership_mapping(self, lf, monkeypatch, discord_status, code, http, member):
        calls = _stub_discord(lf, monkeypatch, discord_status, code)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                 body={"discord_id": DISCORD_ID, "purpose": "checkout"},
                                 headers=WORKER_H)
        assert status == http
        if member is None:
            assert resp["ok"] is False and resp["error"] == "membership_unavailable"
            assert "member" not in resp
        else:
            assert resp == {"ok": True, "member": member}
        assert calls == [("GET", DISCORD_ID, None)]

    def test_denials_are_audited_only_for_purchase_checks(self, lf, monkeypatch):
        _stub_discord(lf, monkeypatch, 404, 10007)
        for purpose in ("config", "checkout", "trial", "something-else"):
            status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                     body={"discord_id": DISCORD_ID, "purpose": purpose},
                                     headers=WORKER_H)
            assert status == 200 and resp["member"] is False
        rows = audit_rows(lf, "discord.membership_check")
        assert sorted(r["details"]["purpose"] for r in rows) == ["checkout", "trial"]
        assert all(r["result"] == "not_member" and r["target"] == DISCORD_ID for r in rows)
        _stub_discord(lf, monkeypatch, 500)
        invoke(lf, "POST", "/api/bot/guild-member",
               body={"discord_id": DISCORD_ID, "purpose": "trial"}, headers=WORKER_H)
        results = sorted(r["result"] for r in audit_rows(lf, "discord.membership_check"))
        assert results == ["not_member", "not_member", "unavailable"]

    @pytest.mark.parametrize("discord_id", ["", "123", "abcdefghijklmnopq", "1" * 23])
    def test_rejects_malformed_discord_ids(self, lf, monkeypatch, discord_id):
        calls = _stub_discord(lf, monkeypatch, 200)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                 body={"discord_id": discord_id}, headers=WORKER_H)
        assert status == 400 and resp["error"] == "invalid_discord_id"
        assert calls == []

    def test_real_discord_request_uses_a_single_member_get(self, lf, monkeypatch):
        seen = _stub_urlopen(monkeypatch, lf, status=200)
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                 body={"discord_id": DISCORD_ID}, headers=WORKER_H)
        assert status == 200 and resp["member"] is True
        [request] = seen
        assert request.get_method() == "GET" and request.data is None
        assert request.full_url == f"https://discord.com/api/v10/guilds/{TEST_GUILD_ID}/members/{DISCORD_ID}"
        _stub_urlopen(monkeypatch, lf, error_status=404,
                      error_body=b'{"message": "Unknown Member", "code": 10007}')
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                 body={"discord_id": DISCORD_ID}, headers=WORKER_H)
        assert status == 200 and resp["member"] is False
        _stub_urlopen(monkeypatch, lf, error_status=404,
                      error_body=b'{"message": "Unknown Guild", "code": 10004}')
        status, resp, _ = invoke(lf, "POST", "/api/bot/guild-member",
                                 body={"discord_id": DISCORD_ID}, headers=WORKER_H)
        assert status == 502 and resp["discord_code"] == 10004

    def test_membership_checks_are_rate_limited(self, lf, monkeypatch):
        _freeze_clock(lf, monkeypatch)
        _stub_discord(lf, monkeypatch, 200)
        statuses = [invoke(lf, "POST", "/api/bot/guild-member", body={"discord_id": DISCORD_ID},
                           headers=WORKER_H)[0] for _ in range(lf.GUILD_MEMBER_RATE_MAX + 1)]
        assert statuses[-1] == 429 and set(statuses[:-1]) == {200}

    def test_routes_are_post_only(self, lf):
        for path in ("/api/bot/guild-join", "/api/bot/guild-member"):
            status, resp, _ = invoke(lf, "GET", path, headers=WORKER_H)
            assert status == 404


# ── /api/bot/trial codes + throttle (consumed by the website) ────────────────
class TestTrialCodes:
    def test_every_trial_outcome_carries_a_code(self, lf, monkeypatch):
        sent = no_dm(lf, monkeypatch)
        body = {"discord_id": DISCORD_ID, "actor_discord_id": DISCORD_ID}
        status, first, _ = invoke(lf, "POST", "/api/bot/trial", body=body, headers=WORKER_H)
        assert status == 200 and first["ok"] is True and first["code"] == "issued"
        assert "license_key" not in first
        fields = {f["name"]: f["value"] for f in sent[0][1]["fields"]}
        assert "no charge" in fields["When it ends"]
        assert "https://zaeorion.com" in fields["When it ends"]
        assert lf.BILLING_PORTAL_URL not in json.dumps(sent[0][1])
        status, again, _ = invoke(lf, "POST", "/api/bot/trial", body=body, headers=WORKER_H)
        assert status == 200 and again["ok"] is False and again["code"] == "already_claimed"
        assert again["message"] == "You've already claimed your free trial."

        no_dm(lf, monkeypatch, fail=True)
        status, bounced, _ = invoke(lf, "POST", "/api/bot/trial",
                                    body={"discord_id": "223456789012345678"}, headers=WORKER_H)
        assert status == 200 and bounced["ok"] is False and bounced["code"] == "dm_failed"

        lf.licenses_table().put_item(Item={
            "license_key": "BLACKLIST#discord#323456789012345678", "status": "blacklist",
            "revoked": True, "created_at": int(time.time())})
        status, blocked, _ = invoke(lf, "POST", "/api/bot/trial",
                                    body={"discord_id": "323456789012345678"}, headers=WORKER_H)
        assert status == 403 and blocked["error"] == "blacklisted" and blocked["code"] == "blacklisted"

    def test_trial_claims_are_throttled_per_account(self, lf, monkeypatch):
        _freeze_clock(lf, monkeypatch)
        no_dm(lf, monkeypatch, fail=True)     # closed DMs: every attempt rolls back
        body = {"discord_id": DISCORD_ID}
        replies = [invoke(lf, "POST", "/api/bot/trial", body=body, headers=BOT_H)
                   for _ in range(lf.TRIAL_CLAIM_RATE_MAX + 1)]
        assert [r[1]["code"] for r in replies[:-1]] == ["dm_failed"] * lf.TRIAL_CLAIM_RATE_MAX
        status, limited, _ = replies[-1]
        assert status == 429 and limited["code"] == "rate_limited" and limited["message"]
        # Another account is unaffected.
        no_dm(lf, monkeypatch)
        status, other, _ = invoke(lf, "POST", "/api/bot/trial",
                                  body={"discord_id": "423456789012345678"}, headers=BOT_H)
        assert status == 200 and other["code"] == "issued"


def test_paid_subscription_dm_points_at_the_site(lf, monkeypatch):
    sent = no_dm(lf, monkeypatch)
    key = "ABCD-EFGH-JKLM-NPQR"
    lf.licenses_table().put_item(Item={"license_key": key, "discord_user_id": DISCORD_ID,
                                       "status": "active"})
    lf.licenses_table().put_item(Item={"license_key": "ORDER#stripe:checkout:cs_x",
                                       "status": "order_claim"})
    assert lf._notify_stripe_provision(DISCORD_ID, key, "stripe:checkout:cs_x") is True
    fields = {f["name"]: f["value"] for f in sent[0][1]["fields"]}
    assert fields["Manage or cancel"] == lf.BILLING_PORTAL_URL
    assert lf.BILLING_PORTAL_URL == "https://billing.stripe.com/p/login/5kQ7sL0Ec4Ya5MfcFsgQE00"
    lf.licenses_table().put_item(Item={"license_key": "ORDER#stripe:invoice:in_x",
                                       "status": "order_claim"})
    assert lf._notify_stripe_provision(DISCORD_ID, key, "stripe:invoice:in_x", renewed=True) is True
    renewed = {f["name"]: f["value"] for f in sent[1][1]["fields"]}
    assert renewed["Manage or cancel"] == lf.BILLING_PORTAL_URL
