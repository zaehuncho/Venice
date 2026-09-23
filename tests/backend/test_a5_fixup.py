"""A5 regression contracts: lookup outage and persisted Stripe side effects."""

import hashlib
import time
import pytest

from .conftest import (TEST_BOT_SECRET, TEST_PAIR_SECRET, TEST_WORKER_SECRET,
                       invoke, make_nonce_ts, put_license)


PAIR_H = {"x-orion-pair-secret": TEST_PAIR_SECRET}
WORKER_H = {"x-orion-bot-secret": TEST_WORKER_SECRET}
DISCORD_ID = "723456789012345678"


class _FailedSecondaryLookup:
    """Direct key reads work, but both purchase lookup paths are unavailable."""

    def __init__(self, table):
        self.table = table

    def __getattr__(self, name):
        if name in ("query", "scan"):
            def fail(*args, **kwargs):
                raise RuntimeError("fixture secondary lookup outage")
            return fail
        return getattr(self.table, name)


def test_heartbeat_secondary_lookup_outage_is_retriable_without_new_lease(lf, monkeypatch):
    key = "ORION-A5-LOOKUP-OUTAGE"
    put_license(lf, key, machine_id="PAID-PC", extra={"discord_user_id": DISCORD_ID})
    table = lf.licenses_table()
    monkeypatch.setattr(lf, "licenses_table", lambda: _FailedSecondaryLookup(table))

    status, body, _ = invoke(lf, "POST", "/api/license/check", body={
        "license_key": key, "machine_id": "PAID-PC"})
    assert status == 503, body
    assert body["error"] == "entitlement_unavailable"
    assert "lease_expires_at" not in body and "lease_sig" not in body
    assert "subscription" not in str(body).lower()


def test_successful_empty_purchase_lookup_still_denies_entitlement(lf):
    key = "ORION-A5-NO-PURCHASE"
    put_license(lf, key, machine_id="PAID-PC", source="staff", order_id="",
                extra={"discord_user_id": DISCORD_ID})
    status, body, _ = invoke(lf, "POST", "/api/license/check", body={
        "license_key": key, "machine_id": "PAID-PC"})
    assert status == 403 and body["error"] == "subscription_required"


def test_pair_status_and_issue_distinguish_lookup_outage_from_no_purchase(lf, monkeypatch):
    put_license(lf, "ORION-A5-PAID-LOOKUP", machine_id="",
                extra={"discord_user_id": DISCORD_ID})
    table = lf.licenses_table()
    monkeypatch.setattr(lf, "licenses_table", lambda: _FailedSecondaryLookup(table))
    for path in ("/api/bot/pair-status", "/api/bot/pair-issue"):
        status, body, _ = invoke(lf, "POST", path,
                                 body={"discord_id": DISCORD_ID}, headers=PAIR_H)
        assert status == 503, (path, body)
        assert body["error"] == "entitlement_unavailable"
        assert "pair_code" not in body


def test_pair_redeem_lookup_outage_keeps_code_pending(lf, monkeypatch):
    put_license(lf, "ORION-A5-PAID-REDEEM", machine_id="",
                extra={"discord_user_id": DISCORD_ID, "oauth_pair_required": True})
    issued_status, issued, _ = invoke(lf, "POST", "/api/bot/pair-issue",
                                      body={"discord_id": DISCORD_ID}, headers=PAIR_H)
    assert issued_status == 200
    code = issued["pair_code"]
    marker = "PAIR#" + hashlib.sha256(code.encode("ascii")).hexdigest()
    table = lf.licenses_table()
    monkeypatch.setattr(lf, "licenses_table", lambda: _FailedSecondaryLookup(table))
    status, body, _ = invoke(lf, "POST", "/api/license/redeem", body={
        "license_key": code, "machine_id": "PAID-PC", **make_nonce_ts()})
    assert status == 503 and body["error"] == "entitlement_unavailable"
    assert table.get_item(Key={"license_key": marker})["Item"]["status"] == "pair_pending"


def test_pair_redeem_late_lookup_outage_restores_consumed_code(lf, monkeypatch):
    put_license(lf, "ORION-A5-PAID-LATE", machine_id="",
                extra={"discord_user_id": DISCORD_ID, "oauth_pair_required": True})
    issued_status, issued, _ = invoke(lf, "POST", "/api/bot/pair-issue",
                                      body={"discord_id": DISCORD_ID}, headers=PAIR_H)
    assert issued_status == 200
    code = issued["pair_code"]
    marker = "PAIR#" + hashlib.sha256(code.encode("ascii")).hexdigest()
    original = lf.find_licenses_by_discord
    lookups = [0]

    def intermittent(discord_id):
        lookups[0] += 1
        if lookups[0] >= 3:
            return [], "unavailable"
        return original(discord_id)

    monkeypatch.setattr(lf, "find_licenses_by_discord", intermittent)
    status, body, _ = invoke(lf, "POST", "/api/license/redeem", body={
        "license_key": code, "machine_id": "PAID-PC", **make_nonce_ts()})
    assert status == 503 and body["error"] == "entitlement_unavailable"
    assert lookups[0] >= 3
    assert lf.licenses_table().get_item(Key={"license_key": marker})["Item"]["status"] == "pair_pending"


def test_status_polling_across_bucket_and_two_tabs_never_spends_issue_quota(lf, monkeypatch):
    bucket_base = int(time.time() // 600) * 600
    clock = [bucket_base + 540]
    monkeypatch.setattr(lf, "now_ts", lambda: clock[0])
    for sec in range(0, 65, 5):
        clock[0] = bucket_base + 540 + sec
        for tab in range(2):
            status, body, _ = invoke(lf, "POST", "/api/bot/pair-status",
                                     body={"discord_id": DISCORD_ID}, headers=PAIR_H)
            assert status == 200 and body["ready"] is False, (sec, tab, body)
            assert "pair_code" not in body
    assert all(not str(row["rl_key"]).startswith("pair_issue:")
               for row in lf.ratelimit_table().scan().get("Items", []))
    put_license(lf, "ORION-A5-LATE-PROVISION", machine_id="",
                extra={"discord_user_id": DISCORD_ID})
    clock[0] += 5
    status, body, _ = invoke(lf, "POST", "/api/bot/pair-status",
                             body={"discord_id": DISCORD_ID}, headers=PAIR_H)
    assert status == 200 and body["ready"] is True and "pair_code" not in body
    status, body, _ = invoke(lf, "POST", "/api/bot/pair-issue",
                             body={"discord_id": DISCORD_ID}, headers=PAIR_H)
    assert status == 200 and body["pair_code"].startswith("PAIR-")
    limits = [row for row in lf.ratelimit_table().scan().get("Items", [])
              if str(row["rl_key"]).startswith("pair_issue:")]
    assert len(limits) == 1 and int(limits[0]["hits"]) == 1
    assert sum(str(row["license_key"]).startswith("PAIR#")
               for row in lf.licenses_table().scan().get("Items", [])) == 1


def test_existing_issue_quota_does_not_prevent_non_minting_status(lf, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(lf, "now_ts", lambda: now)
    put_license(lf, "ORION-A5-QUOTA-PAID", machine_id="",
                extra={"discord_user_id": DISCORD_ID})
    bucket = now // 600
    lf.ratelimit_table().put_item(Item={
        "rl_key": f"pair_issue:{DISCORD_ID}:{bucket}", "hits": 5,
        "expires": (bucket + 2) * 600})
    status, body, _ = invoke(lf, "POST", "/api/bot/pair-status",
                             body={"discord_id": DISCORD_ID}, headers=PAIR_H)
    assert status == 200 and body["ready"] is True
    status, body, _ = invoke(lf, "POST", "/api/bot/pair-issue",
                             body={"discord_id": DISCORD_ID}, headers=PAIR_H)
    assert status == 429 and body["error"] == "rate_limited"
    assert 1 <= body["retry_after_s"] <= 600 and "pair_code" not in body


def test_limiter_infrastructure_outage_is_503_not_false_quota_deadline(lf, monkeypatch):
    put_license(lf, "ORION-A5-RATELIMIT-PAID", machine_id="",
                extra={"discord_user_id": DISCORD_ID})

    class FailedLimiter:
        def update_item(self, **kwargs):
            raise RuntimeError("fixture limiter table outage")

    monkeypatch.setattr(lf, "ratelimit_table", FailedLimiter)
    for path in ("/api/bot/pair-status", "/api/bot/pair-issue"):
        status, body, _ = invoke(lf, "POST", path,
                                 body={"discord_id": DISCORD_ID}, headers=PAIR_H)
        assert status == 503, (path, body)
        assert body["error"] == "rate_limit_unavailable"
        assert "retry_after_s" not in body and "pair_code" not in body


def test_reset_credit_lookup_outage_does_not_spend_paid_order(lf, monkeypatch):
    put_license(lf, "ORION-A5-RESET-CREDIT", machine_id="",
                extra={"discord_user_id": DISCORD_ID})
    table = lf.licenses_table()
    monkeypatch.setattr(lf, "licenses_table", lambda: _FailedSecondaryLookup(table))
    order = "a5-reset-credit-order"
    status, body, _ = invoke(lf, "POST", "/api/bot/reset-credit",
                             body={"discord_user_id": DISCORD_ID, "order_id": order},
                             headers={"x-orion-bot-secret": TEST_BOT_SECRET})
    assert status == 503 and body["error"] == "entitlement_unavailable"
    assert "Item" not in table.get_item(Key={"license_key": "ORDER#RESETCREDIT-" + order})


@pytest.mark.parametrize("fallback_scan", [False, True])
def test_discord_lookup_reads_all_pages_before_denial(lf, monkeypatch, fallback_scan):
    purchase = {"license_key": "ORION-A5-PAGED-PURCHASE", "discord_user_id": DISCORD_ID,
                "source": "gumroad", "order_id": "paid-order", "status": "active",
                "plan": "month", "expiry": int(time.time()) + 3600}

    class PagedTable:
        calls = 0

        def query(self, **kwargs):
            if fallback_scan:
                raise RuntimeError("fixture missing GSI")
            return self.page(kwargs)

        def scan(self, **kwargs):
            return self.page(kwargs)

        def page(self, kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"Items": [], "LastEvaluatedKey": {"license_key": "FIRST-PAGE"}}
            assert kwargs["ExclusiveStartKey"] == {"license_key": "FIRST-PAGE"}
            return {"Items": [purchase]}

    table = PagedTable()
    monkeypatch.setattr(lf, "licenses_table", lambda: table)
    rows, mode = lf.find_licenses_by_discord(DISCORD_ID)
    assert mode == ("scan" if fallback_scan else "gsi")
    assert rows == [purchase] and table.calls == 2


@pytest.mark.parametrize("fallback_scan", [False, True])
def test_partial_lookup_page_error_is_unavailable_not_authoritative_empty(lf, monkeypatch,
                                                                           fallback_scan):
    class FailedPage:
        calls = 0

        def query(self, **kwargs):
            if fallback_scan:
                raise RuntimeError("fixture missing GSI")
            return self.page(kwargs)

        def scan(self, **kwargs):
            return self.page(kwargs)

        def page(self, kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"Items": [], "LastEvaluatedKey": {"license_key": "FIRST-PAGE"}}
            raise RuntimeError("fixture later page outage")

    monkeypatch.setattr(lf, "licenses_table", lambda: FailedPage())
    rows, mode = lf.find_licenses_by_discord(DISCORD_ID)
    assert rows == [] and mode == "unavailable"


def _stripe_order(order_id):
    return {"order_id": order_id, "discord_user_id": DISCORD_ID,
            "plan": "month", "days": 30, "notify": True,
            "subscription_id": "sub_a5fixture"}


def _order_marker(lf, order_id):
    return lf.licenses_table().get_item(Key={"license_key": "ORDER#" + order_id})["Item"]


def _minted_rows(lf):
    return [row for row in lf.licenses_table().scan().get("Items", [])
            if row.get("source") == "gumroad" and row.get("status") == "active"]


def test_stripe_replay_repairs_role_without_remint_or_duplicate_dm(lf, monkeypatch):
    roles, dms = [], []
    monkeypatch.setattr(lf, "_discord_add_role",
                        lambda user, role: roles.append(user) or len(roles) > 1)
    monkeypatch.setattr(lf, "_discord_dm", lambda user, embed: dms.append(user))
    order = "stripe:checkout:cs_a5_role_retry"
    body = _stripe_order(order)
    first, first_body, _ = invoke(lf, "POST", "/api/bot/provision", body=body, headers=WORKER_H)
    assert first in (201, 503), first_body
    assert len(_minted_rows(lf)) == 1 and len(dms) == 1 and len(roles) == 1
    assert not _order_marker(lf, order).get("role_granted_at")
    first_expiry = int(_minted_rows(lf)[0]["expiry"])

    replay, replay_body, _ = invoke(lf, "POST", "/api/bot/provision", body=body, headers=WORKER_H)
    assert replay == 200, replay_body
    assert replay_body["duplicate_order"] is True
    assert len(_minted_rows(lf)) == 1 and int(_minted_rows(lf)[0]["expiry"]) == first_expiry
    assert roles == [DISCORD_ID, DISCORD_ID] and dms == [DISCORD_ID]
    assert int(_order_marker(lf, order)["role_granted_at"]) <= int(time.time())


def test_stripe_replay_does_not_repeat_successful_role_after_dm_failure(lf, monkeypatch):
    roles, dms = [], []
    monkeypatch.setattr(lf, "_discord_add_role", lambda user, role: roles.append(user) or True)

    def dm(user, embed):
        dms.append(user)
        if len(dms) == 1:
            raise RuntimeError("fixture DM outage")
    monkeypatch.setattr(lf, "_discord_dm", dm)
    order = "stripe:checkout:cs_a5_dm_retry"
    body = _stripe_order(order)
    first, first_body, _ = invoke(lf, "POST", "/api/bot/provision", body=body, headers=WORKER_H)
    assert first == 503 and first_body["error"] == "dm_pending"
    assert _order_marker(lf, order).get("role_granted_at")
    replay, replay_body, _ = invoke(lf, "POST", "/api/bot/provision", body=body, headers=WORKER_H)
    assert replay == 200 and replay_body["duplicate_order"] is True
    assert roles == [DISCORD_ID] and dms == [DISCORD_ID, DISCORD_ID]
    assert len(_minted_rows(lf)) == 1
