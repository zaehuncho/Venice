"""Owner rule 2026-09-15 — the plan surface and the recurring monthly renewal.

The rule, verbatim: *"only the 3 day trial then 25 monthly after that, no lifetime
so it can keep recurring"*.

Two things have to be true for that to hold, and both are pinned here:

  1. `month` is the only plan sold. `lifetime` / `week` / `day` must still be
     ACCEPTED (keys already sold have to keep resolving, and staff still mint the
     occasional comp), so this file asserts "still works", not "is gone".
  2. The subscription actually RECURS. A Gumroad membership pings on every charge;
     the second charge must EXTEND the key the customer already has instead of
     minting a second one — otherwise their key dies after 30 days while they keep
     being billed, which is the one failure mode a subscription cannot have.
"""
import time

from conftest import (invoke, put_license, make_nonce_ts, TEST_BOT_SECRET,
                      TEST_WORKER_SECRET, TEST_PAIR_SECRET)

BOT_H = {"x-orion-bot-secret": TEST_BOT_SECRET}
WORKER_H = {"x-orion-bot-secret": TEST_WORKER_SECRET}


def _real_rows(lf):
    rows = lf.licenses_table().scan().get("Items", [])
    return [r for r in rows
            if not str(r["license_key"]).startswith(("ORDER#", "TRIAL#", "BLACKLIST#",
                                                     "TRIALMACHINE#", "RESETCREDIT-"))]


class TestPlanSurface:
    def test_month_is_the_only_sellable_plan(self, lf):
        assert lf.SELLABLE_PLAN == "month"
        assert lf.TIER_DAYS[lf.SELLABLE_PLAN] == 30
        # [2026-09-19 owner] 3 -> 7 days for launch: setup plus finding your Shot Lead eats
        # day one, and a 3-day trial can expire before the user gets a weekend session.
        # Pinned deliberately: the trial length is a pricing decision, not an implementation detail.
        assert lf.TRIAL_DAYS == 7

    def test_legacy_plans_are_still_accepted_just_not_sold(self, lf):
        """Removing them from TIER_DAYS would break every key already sold and
        every staff comp — the rule is "not advertised", not "not accepted"."""
        for plan in lf.LEGACY_PLANS:
            assert plan in lf.TIER_DAYS, f"{plan} keys already exist; keep resolving them"
        assert lf.SELLABLE_PLAN not in lf.LEGACY_PLANS

    def test_provision_defaults_to_month_when_no_plan_is_sent(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/bot/provision", headers=BOT_H,
                         body={"order_id": "ORD-DEFAULT", "discord_user_id": "500"})
        assert s == 201
        assert b["plan"] == "month"
        assert b["expiry"] > int(time.time()) + 29 * 86400

    def test_paid_provision_grants_customer_role(self, lf, monkeypatch):
        grants = []
        monkeypatch.setattr(lf, "_discord_add_role",
                            lambda user_id, role_ssm: grants.append((user_id, role_ssm)) or True)
        s, b, _ = invoke(lf, "POST", "/api/bot/provision", headers=BOT_H,
                         body={"order_id": "ORD-ROLE", "discord_user_id": "509"})
        assert s == 201 and b["role_granted"] is True
        assert grants == [("509", lf.DISCORD_CUSTOMER_ROLE_SSM)]

    def test_lifetime_still_mints_for_a_staff_comp(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/bot/provision", headers=BOT_H,
                         body={"plan": "lifetime", "days": None,
                               "order_id": "ORD-COMP", "discord_user_id": "501"})
        assert s == 201 and b["plan"] == "lifetime" and b["expiry"] == 0


class TestMonthlyRenewal:
    """`renew: true` is what the Gumroad membership ping sends on charge 2..N."""

    def _provision(self, lf, order_id, discord_id="600", **extra):
        body = {"plan": "month", "days": 30, "order_id": order_id,
                "discord_user_id": discord_id}
        body.update(extra)
        headers = WORKER_H if order_id.startswith("stripe:") else BOT_H
        return invoke(lf, "POST", "/api/bot/provision", headers=headers, body=body)

    def test_renewal_extends_the_same_key_instead_of_minting_a_second(self, lf):
        s1, b1, _ = self._provision(lf, "ORD-M1")
        assert s1 == 201
        key, first_expiry = b1["license_key"], int(b1["expiry"])

        s2, b2, _ = self._provision(lf, "ORD-M2", renew=True)
        assert s2 == 200, b2
        assert b2["renewed"] is True
        assert b2["license_key"] == key, "a renewal must not hand out a new key"
        assert int(b2["expiry"]) == first_expiry + 30 * 86400

        assert len(_real_rows(lf)) == 1, "one subscriber, one license row"

    def test_renewal_rechecks_customer_role(self, lf, monkeypatch):
        grants = []
        monkeypatch.setattr(lf, "_discord_add_role",
                            lambda user_id, role_ssm: grants.append((user_id, role_ssm)) or True)
        self._provision(lf, "ORD-RG1", discord_id="610")
        s, b, _ = self._provision(lf, "ORD-RG2", discord_id="610", renew=True)
        assert s == 200 and b["role_granted"] is True
        assert grants == [("610", lf.DISCORD_CUSTOMER_ROLE_SSM),
                          ("610", lf.DISCORD_CUSTOMER_ROLE_SSM)]

    def test_a_lapsed_subscription_renews_from_today_not_from_the_old_expiry(self, lf):
        """Paying again after a gap must buy 30 days of ACCESS, not 30 days that
        were already spent sitting expired."""
        past = int(time.time()) - 10 * 86400
        put_license(lf, "ORION-LAPS-BBBB-CCCC", plan="month", expiry=past,
                    extra={"discord_user_id": "601"})
        s, b, _ = self._provision(lf, "ORD-LAPSED", discord_id="601", renew=True)
        assert s == 200 and b["renewed"] is True
        assert b["license_key"] == "ORION-LAPS-BBBB-CCCC"
        assert int(b["expiry"]) >= int(time.time()) + 29 * 86400

    def test_renewal_of_a_revoked_key_mints_fresh_rather_than_resurrecting_it(self, lf):
        """A chargeback revoked the key. A later subscription payment is a NEW
        relationship — never silently un-revoke."""
        put_license(lf, "ORION-REVK-BBBB-CCCC", plan="month", status="revoked",
                    revoked=True, extra={"discord_user_id": "602"})
        s, b, _ = self._provision(lf, "ORD-REVOKED", discord_id="602", renew=True)
        assert s == 201, b
        assert not b.get("renewed")
        assert b["license_key"] != "ORION-REVK-BBBB-CCCC"
        row = lf.licenses_table().get_item(
            Key={"license_key": "ORION-REVK-BBBB-CCCC"})["Item"]
        assert row["revoked"] is True and row["status"] == "revoked"

    def test_renewal_does_not_unfreeze_a_frozen_key(self, lf):
        """Freeze is an owner decision; a payment must not undo it behind the
        owner's back. The time is still added — the owner releases the freeze."""
        put_license(lf, "ORION-FRZN-BBBB-CCCC", plan="month", status="frozen",
                    expiry=int(time.time()) + 5 * 86400,
                    extra={"discord_user_id": "603"})
        s, b, _ = self._provision(lf, "ORD-FROZEN", discord_id="603", renew=True)
        assert s == 200 and b["renewed"] is True
        row = lf.licenses_table().get_item(
            Key={"license_key": "ORION-FRZN-BBBB-CCCC"})["Item"]
        assert row["status"] == "frozen", "a renewal must not silently unfreeze"

    def test_renewal_with_no_existing_key_falls_back_to_minting(self, lf):
        """Gumroad can fire a recurring charge for someone whose key was deleted.
        Minting beats 500-ing at a paying customer."""
        s, b, _ = self._provision(lf, "ORD-ORPHAN", discord_id="604", renew=True)
        assert s == 201 and not b.get("renewed")
        assert b["license_key"]

    def test_a_renewal_ping_is_still_spend_once_per_order(self, lf):
        self._provision(lf, "ORD-R1")
        s2, b2, _ = self._provision(lf, "ORD-R2", renew=True)
        first_expiry = int(b2["expiry"])
        s3, b3, _ = self._provision(lf, "ORD-R2", renew=True)      # replay
        assert s3 == 200 and b3.get("duplicate_order") is True
        row = lf.licenses_table().get_item(
            Key={"license_key": b2["license_key"]})["Item"]
        assert int(row["expiry"]) == first_expiry, "a replayed ping must not re-extend"

    def test_renewal_never_converts_a_newer_trial_into_the_paid_key(self, lf):
        paid = put_license(lf, "ORION-PAID-BBBB-CCCC", plan="month",
                           extra={"discord_user_id": "630", "created_at": 100})
        trial = put_license(lf, "ORION-TRIAL-BBBB-CCCC", plan="trial", source="trial",
                            order_id="", extra={"discord_user_id": "630", "created_at": 200})
        s, b, _ = self._provision(lf, "ORD-PAID-630", discord_id="630", renew=True)
        assert s == 200 and b["renewed"] is True
        assert b["license_key"] == paid["license_key"]
        unchanged = lf.licenses_table().get_item(Key={"license_key": trial["license_key"]})["Item"]
        assert unchanged["plan"] == "trial"

    def test_subscription_end_revoke_does_not_target_a_newer_trial(self, lf, monkeypatch):
        removed = []
        monkeypatch.setattr(lf, "_discord_remove_role",
                            lambda target, role: removed.append((target, role)) or True)
        paid = put_license(lf, "ORION-PAID-DDDD-EEEE", plan="month",
                           extra={"discord_user_id": "631", "created_at": 100})
        trial = put_license(lf, "ORION-TRIAL-DDDD-EEEE", plan="trial", source="trial",
                            order_id="", extra={"discord_user_id": "631", "created_at": 200})
        s, b, _ = invoke(lf, "POST", "/api/bot/chargeback", headers=BOT_H,
                         body={"discord_user_id": "631", "kind": "refunded",
                               "reason": "Stripe subscription ended"})
        assert s == 200 and b["revoked"] is True
        assert lf.licenses_table().get_item(Key={"license_key": paid["license_key"]})["Item"]["revoked"] is True
        assert lf.licenses_table().get_item(Key={"license_key": trial["license_key"]})["Item"]["revoked"] is False
        assert removed == [("631", lf.DISCORD_CUSTOMER_ROLE_SSM)]

    def test_stripe_checkout_dms_account_connection_once_without_private_key(self, lf, monkeypatch):
        sent = []
        monkeypatch.setattr(lf, "_discord_dm", lambda target, embed: sent.append((target, embed)))
        monkeypatch.setattr(lf, "_discord_add_role", lambda target, role: True)
        body = {"order_id": "stripe:checkout:cs_fixture", "discord_user_id": "632",
                "plan": "month", "notify": True, "subscription_id": "sub_fixture"}
        s, b, _ = invoke(lf, "POST", "/api/bot/provision", headers=WORKER_H, body=body)
        assert s == 201 and b["notified"] is True and "license_key" not in b
        assert len(sent) == 1 and sent[0][0] == "632"
        private_key = _real_rows(lf)[0]["license_key"]
        assert _real_rows(lf)[0]["oauth_pair_required"] is True
        assert sent[0][1]["title"] == "✅ Venice subscription active"
        assert "https://zaeorion.com/connect" in str(sent[0][1])
        assert "License Key" not in str(sent[0][1])
        assert private_key not in str(sent[0][1])
        s2, b2, _ = invoke(lf, "POST", "/api/bot/provision", headers=WORKER_H, body=body)
        assert s2 == 200 and b2["duplicate_order"] is True and "license_key" not in b2
        assert len(sent) == 1

    def test_stripe_paid_key_requires_discord_oauth_pairing(self, lf, monkeypatch):
        monkeypatch.setattr(lf, "_discord_dm", lambda target, embed: None)
        monkeypatch.setattr(lf, "_discord_add_role", lambda target, role: True)
        discord_id = "632456789012345678"
        s, b, _ = self._provision(lf, "stripe:checkout:cs_oauth", discord_id=discord_id,
                                  notify=True, subscription_id="sub_oauthfixture")
        assert s == 201 and b["notified"] is True
        private_key = _real_rows(lf)[0]["license_key"]
        direct, direct_body, _ = invoke(lf, "POST", "/api/license/redeem",
            body={"license_key": private_key, "machine_id": "PAID-PC", **make_nonce_ts()})
        assert direct == 403 and direct_body["error"] == "discord_signin_required"
        issued_status, issued, _ = invoke(lf, "POST", "/api/bot/pair-issue",
            headers={"x-orion-pair-secret": TEST_PAIR_SECRET},
            body={"discord_id": discord_id})
        assert issued_status == 200
        paired, activated, _ = invoke(lf, "POST", "/api/license/redeem",
            body={"license_key": issued["pair_code"], "machine_id": "PAID-PC", **make_nonce_ts()})
        assert paired == 200 and activated["canonical_license_key"] == private_key
        replay, replay_body, _ = invoke(lf, "POST", "/api/license/redeem",
            body={"license_key": issued["pair_code"], "machine_id": "PAID-PC", **make_nonce_ts()})
        assert replay == 403 and replay_body["error"] == "pair_invalid"

    def test_stripe_order_rejects_non_worker_secret_and_wrong_plan(self, lf):
        body = {"order_id": "stripe:checkout:cs_guard", "discord_user_id": "636",
                "plan": "month", "days": 30, "notify": True,
                "subscription_id": "sub_guard"}
        status, _, _ = invoke(lf, "POST", "/api/bot/provision", headers=BOT_H, body=body)
        assert status == 403
        for change in ({"plan": "lifetime"}, {"days": 36500}, {"notify": False},
                       {"order_id": "stripe:invoice:in_guard"}):
            invalid = {**body, **change}
            status, _, _ = invoke(lf, "POST", "/api/bot/provision",
                                  headers=WORKER_H, body=invalid)
            assert status == 400
        assert _real_rows(lf) == []

    def test_subscription_end_rejects_non_worker_secret(self, lf):
        status, _, _ = invoke(lf, "POST", "/api/bot/chargeback", headers=BOT_H,
                              body={"discord_user_id": "636", "subscription_id": "sub_guard",
                                    "kind": "refunded"})
        assert status == 403

    def test_stripe_dm_failure_retries_delivery_without_reminting(self, lf, monkeypatch):
        attempted = []
        monkeypatch.setattr(lf, "_discord_add_role", lambda target, role: True)
        def dm(target, embed):
            attempted.append(target)
            if len(attempted) == 1:
                raise RuntimeError("fixture DM bounce")
        monkeypatch.setattr(lf, "_discord_dm", dm)
        body = {"order_id": "stripe:checkout:cs_retry", "discord_user_id": "633",
                "plan": "month", "notify": True, "subscription_id": "sub_retry"}
        s1, b1, _ = invoke(lf, "POST", "/api/bot/provision", headers=WORKER_H, body=body)
        assert s1 == 503 and b1["error"] == "dm_pending"
        assert len(_real_rows(lf)) == 1
        s2, b2, _ = invoke(lf, "POST", "/api/bot/provision", headers=WORKER_H, body=body)
        assert s2 == 200 and b2["notified"] is True and b2["duplicate_order"] is True
        assert len(_real_rows(lf)) == 1 and attempted == ["633", "633"]

    def test_stripe_renewal_dm_never_repeats_key(self, lf, monkeypatch):
        sent = []
        monkeypatch.setattr(lf, "_discord_dm", lambda target, embed: sent.append(embed))
        monkeypatch.setattr(lf, "_discord_add_role", lambda target, role: True)
        self._provision(lf, "stripe:checkout:cs_initial", discord_id="634",
                        notify=True, subscription_id="sub_renewfixture")
        s, b, _ = self._provision(lf, "stripe:invoice:in_renew", discord_id="634",
                                  renew=True, notify=True, subscription_id="sub_renewfixture")
        assert s == 200 and b["renewed"] is True and b["notified"] is True
        assert "license_key" not in b and "License Key" not in str(sent[1])
        assert len(_real_rows(lf)) == 1

    def test_stripe_renewal_requires_oauth_on_existing_paid_row(self, lf, monkeypatch):
        monkeypatch.setattr(lf, "_discord_dm", lambda target, embed: None)
        monkeypatch.setattr(lf, "_discord_add_role", lambda target, role: True)
        key = "ORION-PAID-OAUTH-RENEW"
        put_license(lf, key, plan="month", extra={
            "discord_user_id": "634456789012345678", "source": "gumroad",
            "order_id": "stripe:checkout:cs_prior", "stripe_subscription_id": "sub_prior",
            "oauth_pair_required": False,
        })
        s, b, _ = self._provision(lf, "stripe:invoice:in_oauthrenew",
                                  discord_id="634456789012345678", renew=True,
                                  notify=True, subscription_id="sub_prior")
        assert s == 200 and b["renewed"] is True
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert row["oauth_pair_required"] is True

    def test_cancel_old_subscription_keeps_new_paid_key_and_customer_role(self, lf, monkeypatch):
        removed = []
        monkeypatch.setattr(lf, "_discord_remove_role",
                            lambda target, role: removed.append(target) or True)
        monkeypatch.setattr(lf, "_discord_dm", lambda target, embed: None)
        monkeypatch.setattr(lf, "_discord_add_role", lambda target, role: True)
        self._provision(lf, "stripe:checkout:cs_old", discord_id="635",
                        notify=True, subscription_id="sub_oldfixture")
        self._provision(lf, "stripe:checkout:cs_new", discord_id="635",
                        notify=True, subscription_id="sub_newfixture")
        rows = {row["stripe_subscription_id"]: row for row in _real_rows(lf)}
        s, b, _ = invoke(lf, "POST", "/api/bot/chargeback", headers=WORKER_H,
                         body={"discord_user_id": "635", "subscription_id": "sub_oldfixture",
                               "kind": "refunded", "reason": "old subscription ended"})
        assert s == 200 and b["revoked"] is True
        assert lf.licenses_table().get_item(Key={"license_key": rows["sub_oldfixture"]["license_key"]})["Item"]["revoked"] is True
        assert lf.licenses_table().get_item(Key={"license_key": rows["sub_newfixture"]["license_key"]})["Item"]["revoked"] is False
        assert removed == []

    def test_a_key_with_no_expiry_is_left_alone_by_a_renewal(self, lf):
        """A lifetime comp has nothing to extend; minting a second key beside it
        is the honest outcome, and the comp must not be given an expiry."""
        put_license(lf, "ORION-COMP-BBBB-CCCC", plan="lifetime", expiry=0,
                    extra={"discord_user_id": "605"})
        s, b, _ = self._provision(lf, "ORD-OVERCOMP", discord_id="605", renew=True)
        assert s == 201 and not b.get("renewed")
        row = lf.licenses_table().get_item(
            Key={"license_key": "ORION-COMP-BBBB-CCCC"})["Item"]
        assert int(row["expiry"]) == 0
