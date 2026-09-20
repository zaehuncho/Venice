"""Admin Panel V2 — server-side contract tests (docs/ADMIN_PANEL_V2_CONTRACT.md §8).

Covers: the capability matrix per role, the staff lifecycle (create -> enroll ->
login -> act), the HWID reset state machine (free x3 -> staff credit -> deduct
1 day -> trial_no_deduct / insufficient / locked / force, no-expiry -> paid_only),
an audit row carrying the
actor on every mutation, the version gate, MOTD, owner TOTP on/off, the owner IP
allowlist, blacklist, the fraud flag, the metrics shape, the revoke-fast codes,
the /api/bot/deliver actor requirement and the /api/bot/killswitch write removal.
"""
import json
import time
import pytest
from conftest import (invoke, put_license, put_staff, staff_login, no_dm,
                      make_staff_nonce_ts, make_nonce_ts, audit_rows,
                      OWNER_H, BOT_H, TEST_ADMIN_SECRET)


def _mutate(lf, action, headers, **body):
    body.setdefault("reason", "test")
    return invoke(lf, "POST", "/api/admin/license", body={"action": action, **body},
                  headers=headers)


def _staff_mutate(lf, action, headers, **body):
    body.setdefault("reason", "test")
    return invoke(lf, "POST", "/api/staff/license", body={"action": action, **body},
                  headers=headers)


# ══════════════════════════════════════════════════════════════════════════════
# §1 capability matrix
# ══════════════════════════════════════════════════════════════════════════════
class TestCapabilityMatrix:
    """The matrix is enforced in ONE place (require_capability). These assert the
    table itself, then spot-check that the routes actually consult it."""

    @pytest.mark.parametrize("cap,owner,admin,support", [
        ("license.lookup",             True,  True,  True),
        ("license.create",             True,  True,  False),
        ("license.reset_machine",      True,  True,  True),
        ("license.reset_machine.force", True, False, False),
        ("license.extend",             True,  True,  False),
        ("license.revoke",             True,  True,  False),
        ("license.unrevoke",           True,  True,  False),
        ("license.freeze",             True,  True,  False),
        ("license.unfreeze",           True,  True,  False),
        ("license.set_plan",           True,  True,  False),
        ("license.transfer",           True,  True,  False),
        ("license.set_reset_policy",   True,  False, False),
        ("license.blacklist",          True,  False, False),
        ("staff.manage",               True,  False, False),
        ("config.write",               True,  False, False),
        ("audit.read_all",             True,  False, False),
        ("audit.read_own",             True,  True,  True),
        ("metrics",                    True,  False, False),
    ])
    def test_matrix(self, lf, cap, owner, admin, support):
        for role, expected in (("owner", owner), ("admin", admin), ("support", support)):
            got = lf.has_capability({"role": role}, cap)
            assert got is expected, f"{role} x {cap}: expected {expected}, got {got}"

    def test_legacy_staff_role_degrades_to_support(self, lf):
        # Rows written by the pre-V2 enroll path carry role="staff". They must map
        # to the LEAST privilege, never to admin.
        assert lf.normalize_role("staff") == "support"
        assert lf.has_capability({"role": "staff"}, "license.lookup") is True
        assert lf.has_capability({"role": "staff"}, "license.create") is False

    def test_support_cannot_create_admin_can(self, lf):
        sup = staff_login(lf, "sup1", role="support")
        s, b, _ = _staff_mutate(lf, "create", sup, plan="month", days=30)
        assert s == 403 and b["error"] == "forbidden"

        adm = staff_login(lf, "adm1", role="admin")
        s, b, _ = _staff_mutate(lf, "create", adm, plan="month", days=30)
        assert s == 201 and b["ok"] is True
        assert b["keys"][0]["license_key"]

    def test_support_cannot_revoke(self, lf):
        put_license(lf, "ORION-CAP1-BBBB-CCCC")
        sup = staff_login(lf, "sup2", role="support")
        s, b, _ = _staff_mutate(lf, "revoke", sup, key="ORION-CAP1-BBBB-CCCC")
        assert s == 403 and b["error"] == "forbidden"

    def test_admin_cannot_blacklist_or_manage_staff(self, lf):
        adm = staff_login(lf, "adm2", role="admin")
        s, b, _ = _staff_mutate(lf, "blacklist", adm, machine_id="M-EVIL")
        assert s == 403 and b["error"] == "forbidden"
        # /api/admin/* with a non-owner staff token is refused at the router gate.
        s, b, _ = invoke(lf, "POST", "/api/admin/staff",
                         body={"action": "list"}, headers=adm)
        assert s == 403 and b["error"] == "forbidden"

    def test_owner_role_staff_token_reaches_admin_routes(self, lf):
        """§1: the owner's DAILY identity is a staff row with role=owner, machine
        bound like any other staff — not the break-glass secret."""
        own = staff_login(lf, "own1", role="owner")
        s, b, _ = invoke(lf, "GET", "/api/admin/metrics", headers=own)
        assert s == 200 and b["ok"] is True

    def test_support_machine_id_is_masked(self, lf):
        put_license(lf, "ORION-MASK-BBBB-CCCC", machine_id="MACHINE-SECRET-123456")
        sup = staff_login(lf, "sup3", role="support")
        s, b, _ = _staff_mutate(lf, "lookup", sup, key="ORION-MASK-BBBB-CCCC")
        assert s == 200
        assert b["license"]["machine_id"] == "...123456"
        assert b["license"]["license_key"] is None
        adm = staff_login(lf, "adm3", role="admin")
        s, b, _ = _staff_mutate(lf, "lookup", adm, key="ORION-MASK-BBBB-CCCC")
        assert b["license"]["machine_id"] == "MACHINE-SECRET-123456"


# ══════════════════════════════════════════════════════════════════════════════
# §2 staff lifecycle
# ══════════════════════════════════════════════════════════════════════════════
class TestStaffLifecycle:
    def test_create_enroll_login_act(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "create", "discord_user_id": "555", "role": "admin",
            "caps": {"keys_per_day": 3}, "reason": "new hire"})
        assert s == 201 and b["ok"] is True
        staff_id, enroll_key = b["staff_id"], b["enroll_key"]
        assert b["caps"]["keys_per_day"] == 3

        # The enroll key is shown ONCE — only the salted hash is stored.
        row = lf.staff_table().get_item(Key={"staff_id": staff_id})["Item"]
        assert enroll_key not in str(row)
        assert row["enroll_key_hash"] and row["enroll_salt"]

        # Login before enrolment is refused.
        nt = make_staff_nonce_ts()
        s, b2, _ = invoke(lf, "POST", "/api/staff/login", body={
            "staff_id": staff_id, "machine_id": "M-NEW", **nt})
        assert s == 403 and b2["error"] == "enrollment_pending"

        # A wrong key does not enroll.
        nt = make_staff_nonce_ts()
        s, b2, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": "not-the-key", "staff_id": staff_id,
            "machine_id": "M-NEW", **nt})
        assert s == 403 and b2["error"] == "invalid_enroll_key"

        # The real one does, and carries the assigned role.
        nt = make_staff_nonce_ts()
        s, b2, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": enroll_key, "staff_id": staff_id,
            "machine_id": "M-NEW", **nt})
        assert s == 200 and b2["role"] == "admin"
        h = {"authorization": f"Bearer {b2['token']}", "x-machine-id": "M-NEW"}

        # ...and can act within its role.
        s, b3, _ = _staff_mutate(lf, "create", h, plan="week", days=7)
        assert s == 201

        # The one-time key is spent: replaying it now hits staff_id_taken.
        nt = make_staff_nonce_ts()
        s, b4, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": enroll_key, "staff_id": staff_id,
            "machine_id": "M-OTHER", **nt})
        assert s == 409 and b4["error"] == "staff_id_taken"

    def test_caps_bound_a_staff_member(self, lf):
        h = staff_login(lf, "capped", role="admin", caps={"keys_per_day": 2,
                                                          "resets_per_day": 1,
                                                          "extend_max_days": 5})
        s, b, _ = _staff_mutate(lf, "create", h, plan="month", count=2)
        assert s == 201 and len(b["keys"]) == 2
        s, b, _ = _staff_mutate(lf, "create", h, plan="month", count=1)
        assert s == 429 and b["error"] == "cap_exceeded"
        assert b["limit"] == 2

    def test_extend_cap(self, lf):
        put_license(lf, "ORION-EXT1-BBBB-CCCC")
        h = staff_login(lf, "extguy", role="admin", caps={"extend_max_days": 5})
        s, b, _ = _staff_mutate(lf, "extend", h, key="ORION-EXT1-BBBB-CCCC", days=30)
        assert s == 429 and b["error"] == "cap_exceeded"
        s, b, _ = _staff_mutate(lf, "extend", h, key="ORION-EXT1-BBBB-CCCC", days=5)
        assert s == 200

    def test_disable_revokes_live_tokens(self, lf):
        h = staff_login(lf, "gone", role="admin")
        s, _, _ = invoke(lf, "GET", "/api/staff/whoami", headers=h)
        assert s == 200
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "disable", "staff_id": "gone", "reason": "left the team"})
        assert s == 200 and b["disabled"] is True
        s, b, _ = invoke(lf, "GET", "/api/staff/whoami", headers=h)
        assert s == 403          # token deleted, not merely flagged

    def test_set_role_and_set_caps_and_reset_machine(self, lf):
        h = staff_login(lf, "mover", role="support")
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "set_role", "staff_id": "mover", "role": "admin",
            "reason": "promotion"})
        assert s == 200 and b["role"] == "admin"
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "set_caps", "staff_id": "mover", "caps": {"resets_per_day": 42},
            "reason": "trusted"})
        assert s == 200 and b["caps"]["resets_per_day"] == 42
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "reset_machine", "staff_id": "mover", "reason": "new laptop"})
        assert s == 200 and b["tokens_revoked"] >= 1
        assert lf.staff_table().get_item(Key={"staff_id": "mover"})["Item"]["machine_id"] == ""

    def test_reissue_enrollment(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "create", "discord_user_id": "777", "role": "support",
            "reason": "hire"})
        staff_id = b["staff_id"]
        first = b["enroll_key"]
        s, b2, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "reissue_enrollment", "staff_id": staff_id, "reason": "lost it"})
        assert s == 200 and b2["enroll_key"] != first
        nt = make_staff_nonce_ts()
        s, b3, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": first, "staff_id": staff_id, "machine_id": "M-X", **nt})
        assert s == 403 and b3["error"] == "invalid_enroll_key"
        nt = make_staff_nonce_ts()
        s, b4, _ = invoke(lf, "POST", "/api/staff/enroll", body={
            "enroll_key": b2["enroll_key"], "staff_id": staff_id,
            "machine_id": "M-X", **nt})
        assert s == 200

    def test_invalid_role_rejected(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/staff", headers=OWNER_H, body={
            "action": "create", "role": "superuser", "reason": "nice try"})
        assert s == 400 and b["error"] == "invalid_role"

    def test_staff_can_read_only_its_own_audit(self, lf):
        put_license(lf, "ORION-OWNAUD-BBBB-CCCC")
        a = staff_login(lf, "auda", role="admin")
        b_h = staff_login(lf, "audb", role="admin")
        _staff_mutate(lf, "lookup", a, key="ORION-OWNAUD-BBBB-CCCC")
        _staff_mutate(lf, "lookup", b_h, key="ORION-OWNAUD-BBBB-CCCC")
        s, body, _ = invoke(lf, "GET", "/api/staff/audit", headers=a)
        assert s == 200 and body["ok"] is True
        assert body["audit"], "own rows should be visible"
        assert {r["actor_id"] for r in body["audit"]} == {"auda"}


# ══════════════════════════════════════════════════════════════════════════════
# §3 HWID reset state machine
# ══════════════════════════════════════════════════════════════════════════════
class TestResetPolicy:
    def _setup(self, lf, discord_id="42", cooldown=0, key="ORION-RST1-BBBB-CCCC",
               penalty_days=1, **lic):
        # Owner rule 2026-09-15: 3 free, then ONE day off the subscription, and no
        # price_url anywhere - customers cannot buy a reset.
        lf.config_set("reset_policy_defaults", {
            "free_resets": 3, "penalty_days": penalty_days, "cooldown_s": cooldown,
            "self_service": True})
        extra = dict(lic.pop("extra", {}))
        extra.setdefault("discord_user_id", discord_id)
        put_license(lf, key, machine_id="MACHINE-OLD", extra=extra, **lic)
        return key

    def _reset(self, lf, discord_id="42", **body):
        return invoke(lf, "POST", "/api/bot/hwid-reset",
                      body={"discord_id": discord_id, **body}, headers=BOT_H)

    def test_free_then_paid_then_deduct(self, lf):
        key = self._setup(lf)
        # 1-3: the three free resets the owner promised.
        for i in range(3):
            s, b, _ = self._reset(lf)
            assert s == 200 and b["ok"] is True, b
            assert b["mode"] == "free"
            assert b["free_remaining"] == 2 - i
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert int(row["hwid_resets_used"]) == 3
        assert row["machine_id"] == ""

        # 4: out of free resets, no credit, no confirmation -> confirm the deduct.
        s, b, _ = self._reset(lf)
        assert s == 200 and b["ok"] is False
        assert b["error"] == "payment_required"       # wire-compat name for "confirm"
        assert b["confirm_required"] is True
        assert b["penalty_days"] == 1 and b["deduct_days"] == 1
        assert "price_url" not in b, "customers can no longer buy a reset"
        assert "buy" not in b["message"].lower()

        # A staff-granted credit takes precedence over deducting time.
        lf.licenses_table().update_item(
            Key={"license_key": key},
            UpdateExpression="SET hwid_paid_credits = :c",
            ExpressionAttributeValues={":c": 1})
        s, b, _ = self._reset(lf)
        assert b["ok"] is True and b["mode"] == "paid"
        assert b["hwid_paid_credits"] == 0
        assert b["hwid_resets_used"] == 3 and b["hwid_free_resets"] == 3
        assert b["lifetime"] is False and b["expiry"] > int(time.time())

        # Then the deduct path, which needs an explicit confirmation.
        before = int(lf.licenses_table().get_item(
            Key={"license_key": key})["Item"]["expiry"])
        s, b, _ = self._reset(lf, mode="deduct")
        assert b["ok"] is True and b["mode"] == "deduct"
        after = int(lf.licenses_table().get_item(
            Key={"license_key": key})["Item"]["expiry"])
        assert before - after == 1 * 86400, "one day per reset, not three"
        assert "1 day was deducted" in b["message"]
        assert b["deduct_days"] == 1

    def test_default_deduction_is_one_day(self, lf):
        """The owner's number, read straight off the module defaults - a revert to
        3 fails here even though every config row in this suite is explicit."""
        assert lf.RESET_PENALTY_DAYS == 1
        assert lf.RESET_FREE_DEFAULT == 3
        assert lf.CONFIG_DEFAULTS["reset_policy_defaults"]["penalty_days"] == 1
        assert "price_url" not in lf.CONFIG_DEFAULTS["reset_policy_defaults"]

    def test_per_key_penalty_days_override_is_honoured(self, lf):
        """license.set_reset_policy has always WRITTEN hwid_penalty_days; until now
        nothing read it, so a per-key override silently did nothing."""
        key = self._setup(lf, extra={"discord_user_id": "42", "hwid_resets_used": 3,
                                     "hwid_penalty_days": 5})
        before = int(lf.licenses_table().get_item(Key={"license_key": key})["Item"]["expiry"])
        s, b, _ = self._reset(lf, mode="deduct")
        assert b["ok"] is True and b["deduct_days"] == 5
        after = int(lf.licenses_table().get_item(Key={"license_key": key})["Item"]["expiry"])
        assert before - after == 5 * 86400

    def test_deduct_refused_when_not_enough_time(self, lf):
        self._setup(lf, expiry=int(time.time()) + 3600,
                    extra={"discord_user_id": "42", "hwid_resets_used": 3})
        s, b, _ = self._reset(lf, mode="deduct")
        assert b["ok"] is False and b["error"] == "insufficient_time"
        assert b["penalty_days"] == 1
        assert "price_url" not in b

    def test_trial_cannot_deduct_and_is_told_why(self, lf):
        """A trial has no subscription to take a day from, and apply_hwid_reset
        floors the expiry at `now` - so without the guard a trial would get an
        unlimited supply of 4th resets. Both the confirm step and the confirmed
        call must refuse, and neither may unbind the machine."""
        self._setup(lf, discord_id="77", key="ORION-TRIA-BBBB-CCCC", plan="trial",
                    expiry=int(time.time()) + 3 * 86400,
                    extra={"discord_user_id": "77", "hwid_resets_used": 3})
        for body in ({}, {"mode": "deduct"}):
            s, b, _ = self._reset(lf, discord_id="77", **body)
            assert s == 200 and b["ok"] is False
            assert b["error"] == "trial_no_deduct", b
            assert "trial" in b["message"].lower()
        row = lf.licenses_table().get_item(
            Key={"license_key": "ORION-TRIA-BBBB-CCCC"})["Item"]
        assert row["machine_id"] == "MACHINE-OLD", "a refused reset must not unbind"

    def test_trial_still_gets_its_three_free_resets(self, lf):
        self._setup(lf, discord_id="78", key="ORION-TRI2-BBBB-CCCC", plan="trial",
                    expiry=int(time.time()) + 3 * 86400,
                    extra={"discord_user_id": "78"})
        for i in range(3):
            s, b, _ = self._reset(lf, discord_id="78")
            assert b["ok"] is True and b["mode"] == "free", b
            assert b["free_remaining"] == 2 - i

    def test_key_with_no_expiry_cannot_deduct(self, lf):
        """The owner's rule needs a clock to take days from; a staff lifetime comp
        has none, so it is paid_only - and the reply no longer sells anything."""
        lf.config_set("reset_policy_defaults", {"free_resets": 0, "penalty_days": 1,
                                                "cooldown_s": 0, "self_service": True})
        put_license(lf, "ORION-LIFE-BBBB-CCCC", expiry=0, plan="lifetime",
                    extra={"discord_user_id": "99", "hwid_resets_used": 0,
                           "hwid_free_resets": 0})
        s, b, _ = self._reset(lf, discord_id="99", mode="deduct")
        assert s == 200 and b["ok"] is False
        assert b["error"] == "paid_only"
        assert "price_url" not in b
        assert "ticket" in b["message"].lower()

    def test_cooldown(self, lf):
        self._setup(lf, cooldown=86400)
        s, b, _ = self._reset(lf)
        assert b["ok"] is True
        s, b, _ = self._reset(lf)
        assert b["ok"] is False and b["error"] == "cooldown"
        assert b["retry_at"] > int(time.time())

    def test_locked(self, lf):
        self._setup(lf, extra={"discord_user_id": "42", "hwid_reset_locked": True})
        s, b, _ = self._reset(lf)
        assert b["ok"] is False and b["error"] == "locked"

    def test_self_service_can_be_switched_off(self, lf):
        self._setup(lf)
        lf.config_set("reset_policy_defaults", {"self_service": False})
        s, b, _ = self._reset(lf)
        assert b["ok"] is False and b["error"] == "self_service_disabled"

    def test_owner_force_bypasses_lock_and_cooldown(self, lf):
        key = self._setup(lf, cooldown=86400,
                          extra={"discord_user_id": "42", "hwid_reset_locked": True,
                                 "last_reset_at": int(time.time())})
        # Staff (no force capability) is refused by the policy...
        adm = staff_login(lf, "adm9", role="admin")
        s, b, _ = _staff_mutate(lf, "reset_machine", adm, key=key)
        assert s == 409 and b["error"] == "locked"
        # ...and an admin cannot escalate by asking for force.
        s, b, _ = _staff_mutate(lf, "reset_machine", adm, key=key, force=True)
        assert s == 403 and b["error"] == "forbidden"
        # The owner can.
        s, b, _ = _mutate(lf, "reset_machine", OWNER_H, key=key, force=True)
        assert s == 200 and b["mode"] == "force"

    def test_staff_reset_does_not_burn_the_customer_free_count(self, lf):
        key = self._setup(lf)
        sup = staff_login(lf, "supR", role="support")
        s, b, _ = _staff_mutate(lf, "reset_machine", sup, key=key)
        assert s == 200 and b["mode"] == "staff"
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert int(row.get("hwid_resets_used", 0)) == 0
        assert len(row["reset_history"]) == 1
        assert row["reset_history"][0]["by"] == "staff:supR"

    def test_reset_history_records_actor_and_machine(self, lf):
        key = self._setup(lf)
        self._reset(lf)
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        h = row["reset_history"][-1]
        assert h["mode"] == "free"
        assert h["by"] == "customer:42"
        assert h["machine_before_suffix"] == "NE-OLD"

    def test_staff_resets_are_capped_per_day(self, lf):
        key = self._setup(lf)
        put_license(lf, "ORION-RST2-BBBB-CCCC", machine_id="M2")
        sup = staff_login(lf, "supC", role="support", caps={"resets_per_day": 1})
        s, _, _ = _staff_mutate(lf, "reset_machine", sup, key=key)
        assert s == 200
        s, b, _ = _staff_mutate(lf, "reset_machine", sup, key="ORION-RST2-BBBB-CCCC")
        assert s == 429 and b["error"] == "cap_exceeded"

    def test_lookup_mode_reports_the_index(self, lf):
        self._setup(lf)
        s, b, _ = self._reset(lf)
        assert b["lookup_mode"] == "gsi"    # conftest creates discord_user_id-index

    def test_owner_sets_a_per_key_reset_policy(self, lf):
        key = self._setup(lf)
        s, b, _ = _mutate(lf, "set_reset_policy", OWNER_H, key=key,
                          free_resets=0, locked=True, paid_credits=2)
        assert s == 200
        assert b["reset_policy"]["free_resets"] == 0
        assert b["reset_policy"]["locked"] is True
        assert b["reset_policy"]["paid_credits"] == 2
        # Only the owner may.
        adm = staff_login(lf, "admP", role="admin")
        s, b, _ = _staff_mutate(lf, "set_reset_policy", adm, key=key, free_resets=9)
        assert s == 403

    def test_gumroad_reset_credit_grant(self, lf):
        key = self._setup(lf)
        s, b, _ = invoke(lf, "POST", "/api/bot/reset-credit", headers=BOT_H,
                         body={"discord_user_id": "42", "order_id": "GR-1"})
        assert s == 200 and b["hwid_paid_credits"] == 1
        # Spend-once: a replayed webhook does not grant a second credit.
        s, b, _ = invoke(lf, "POST", "/api/bot/reset-credit", headers=BOT_H,
                         body={"discord_user_id": "42", "order_id": "GR-1"})
        assert b.get("duplicate_order") is True
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert int(row["hwid_paid_credits"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# §4 audit
# ══════════════════════════════════════════════════════════════════════════════
class TestAudit:
    def test_every_mutation_records_an_actor(self, lf):
        put_license(lf, "ORION-AUD1-BBBB-CCCC", machine_id="M-A")
        adm = staff_login(lf, "audit_adm", role="admin")
        mutations = [
            ("revoke",   adm,      {"key": "ORION-AUD1-BBBB-CCCC"}, "staff", "audit_adm"),
            ("unrevoke", adm,      {"key": "ORION-AUD1-BBBB-CCCC"}, "staff", "audit_adm"),
            ("extend",   OWNER_H,  {"key": "ORION-AUD1-BBBB-CCCC", "days": 5}, "owner", "break-glass"),
            ("freeze",   OWNER_H,  {"key": "ORION-AUD1-BBBB-CCCC"}, "owner", "break-glass"),
            ("unfreeze", OWNER_H,  {"key": "ORION-AUD1-BBBB-CCCC"}, "owner", "break-glass"),
            ("set_plan", OWNER_H,  {"key": "ORION-AUD1-BBBB-CCCC", "plan": "month"}, "owner", "break-glass"),
            ("transfer", OWNER_H,  {"key": "ORION-AUD1-BBBB-CCCC", "email": "x@y.z"}, "owner", "break-glass"),
        ]
        for action, headers, body, exp_type, exp_id in mutations:
            fn = _mutate if headers is OWNER_H else _staff_mutate
            s, b, _ = fn(lf, action, headers, reason=f"because {action}", **body)
            assert s == 200, (action, b)
            rows = audit_rows(lf, f"license.{action}")
            assert rows, f"no audit row for license.{action}"
            r = rows[0]
            assert r["actor_type"] == exp_type
            assert r["actor_id"] == exp_id
            assert r["reason"] == f"because {action}"
            assert r["target"] == "CCCC"
            assert r["target_type"] == "license"
            assert r["result"] == "ok"
            assert r["day"] and int(r["ts"]) > 0
            assert r["audit_id"] == r["event_id"]     # ULID in both

    def test_mutations_require_a_reason(self, lf):
        put_license(lf, "ORION-AUD2-BBBB-CCCC")
        s, b, _ = invoke(lf, "POST", "/api/admin/license", headers=OWNER_H, body={
            "action": "revoke", "key": "ORION-AUD2-BBBB-CCCC"})
        assert s == 400 and b["error"] == "reason_required"

    def test_capability_denials_are_audited(self, lf):
        put_license(lf, "ORION-AUD3-BBBB-CCCC")
        sup = staff_login(lf, "denied", role="support")
        _staff_mutate(lf, "revoke", sup, key="ORION-AUD3-BBBB-CCCC")
        rows = [r for r in audit_rows(lf) if r.get("result") == "forbidden"]
        assert rows and rows[0]["actor_id"] == "denied"

    def test_owner_audit_is_paged_and_filterable(self, lf):
        put_license(lf, "ORION-AUD4-BBBB-CCCC")
        for i in range(4):
            _mutate(lf, "lookup", OWNER_H, key="ORION-AUD4-BBBB-CCCC",
                    reason=f"look {i}")
        s, b, _ = invoke(lf, "GET", "/api/admin/audit", headers=OWNER_H,
                         qs={"action": "license.lookup", "limit": "2"})
        assert s == 200 and b["ok"] is True
        assert len(b["audit"]) == 2
        assert b["index"] == "day-ts-index"
        assert b["cursor"]
        s2, b2, _ = invoke(lf, "GET", "/api/admin/audit", headers=OWNER_H,
                           qs={"action": "license.lookup", "limit": "2",
                               "cursor": b["cursor"]})
        assert s2 == 200
        first = {r["audit_id"] for r in b["audit"]}
        second = {r["audit_id"] for r in b2["audit"]}
        assert not (first & second), "cursor returned overlapping rows"

    def test_staff_cannot_read_the_full_audit(self, lf):
        adm = staff_login(lf, "nosy", role="admin")
        s, b, _ = invoke(lf, "GET", "/api/admin/audit", headers=adm)
        assert s == 403

    def test_legacy_staff_audit_table_still_written(self, lf):
        """The shipped OrionOwner build reads orion-staff-audit; §4 unifies the
        writer but must not blind that view."""
        put_staff(lf, "legacy1", machine_id="")
        nt = make_staff_nonce_ts()
        invoke(lf, "POST", "/api/staff/login",
               body={"staff_id": "legacy1", "machine_id": "M-L", **nt})
        rows = lf.staff_audit_table().scan().get("Items", [])
        assert any(r["action"] == "staff_login" for r in rows)
        s, b, _ = invoke(lf, "GET", "/api/admin/staff/audit", headers=OWNER_H)
        assert s == 200 and b["audit"]


# ══════════════════════════════════════════════════════════════════════════════
# §5 kill switch, version gate, MOTD, owner hardening
# ══════════════════════════════════════════════════════════════════════════════
class TestVersionGate:
    def test_below_minimum_is_blocked(self, lf):
        lf.config_set("min_client_version", "1.2.0")
        put_license(lf, "ORION-VER1-BBBB-CCCC", machine_id="M-V")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-VER1-BBBB-CCCC", "machine_id": "M-V",
            "client_version": "1.1.9"})
        assert s == 403
        assert b["error"] == "version_blocked"
        assert b["min_client_version"] == "1.2.0"
        assert b["message"]

    def test_at_or_above_minimum_passes(self, lf):
        lf.config_set("min_client_version", "1.2.0")
        put_license(lf, "ORION-VER2-BBBB-CCCC", machine_id="M-V")
        for v in ("1.2.0", "1.2.1", "2.0.0"):
            s, b, _ = invoke(lf, "POST", "/api/license/check", body={
                "license_key": "ORION-VER2-BBBB-CCCC", "machine_id": "M-V",
                "client_version": v})
            assert s == 200, (v, b)

    def test_explicitly_blocked_version(self, lf):
        lf.config_set("blocked_versions", ["1.3.3"])
        put_license(lf, "ORION-VER3-BBBB-CCCC", machine_id="M-V")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-VER3-BBBB-CCCC", "machine_id": "M-V",
            "client_version": "1.3.3"})
        assert s == 403 and b["error"] == "version_blocked"

    def test_activate_is_gated_too(self, lf):
        lf.config_set("min_client_version", "2.0.0")
        put_license(lf, "ORION-VER4-BBBB-CCCC", machine_id="")
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-VER4-BBBB-CCCC", "machine_id": "M-V",
            "client_version": "1.0.0", **nt})
        assert s == 403 and b["error"] == "version_blocked"

    def test_a_client_that_sends_no_version_is_not_bricked(self, lf):
        """Legacy launchers predate client_version. Turning the gate on must not
        silently kill every one of them — that is what global_kill is for."""
        lf.config_set("min_client_version", "9.9.9")
        put_license(lf, "ORION-VER5-BBBB-CCCC", machine_id="M-V")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-VER5-BBBB-CCCC", "machine_id": "M-V"})
        assert s == 200

    def test_version_endpoint_reports_the_gate(self, lf):
        lf.config_set("min_client_version", "1.5.0")
        s, b, _ = invoke(lf, "GET", "/api/version", qs={"client_version": "1.0.0"})
        assert s == 200
        assert b["error"] == "version_blocked"
        assert b["min_client_version"] == "1.5.0"


class TestMotd:
    def test_motd_served_while_live(self, lf):
        until = int(time.time()) + 3600
        lf.config_set("motd", {"text": "Servers back at 9pm", "level": "maint",
                               "until": until})
        s, b, _ = invoke(lf, "GET", "/api/version")
        assert b["motd"] == {"text": "Servers back at 9pm", "level": "maint",
                             "until": until}
        put_license(lf, "ORION-MOTD-BBBB-CCCC", machine_id="M-M")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-MOTD-BBBB-CCCC", "machine_id": "M-M"})
        assert s == 200 and b["motd"]["level"] == "maint"

    def test_expired_motd_is_dropped(self, lf):
        lf.config_set("motd", {"text": "old news", "level": "info",
                               "until": int(time.time()) - 5})
        s, b, _ = invoke(lf, "GET", "/api/version")
        assert "motd" not in b

    def test_unknown_level_falls_back_to_info(self, lf):
        lf.config_set("motd", {"text": "hi", "level": "PANIC", "until": 0})
        s, b, _ = invoke(lf, "GET", "/api/version")
        assert b["motd"]["level"] == "info"


class TestOwnerTotp:
    def test_enroll_confirm_then_required(self, lf):
        # OFF by default — every admin call works without a code.
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 200 and b["owner_totp_required"] is False

        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H,
                         body={"action": "totp_enroll", "reason": "hardening"})
        assert s == 200 and b["otpauth_uri"].startswith("otpauth://totp/")
        secret = b["secret"]

        # A wrong code does not turn the requirement on.
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H,
                         body={"action": "totp_confirm", "code": "000000",
                               "reason": "hardening"})
        assert s == 403 and b["error"] == "invalid_totp"

        code = lf.totp_now(secret)
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H,
                         body={"action": "totp_confirm", "code": code,
                               "reason": "hardening"})
        assert s == 200 and b["owner_totp_required"] is True

        # Now the secret alone is not enough.
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 403 and b["error"] == "totp_required"
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={**OWNER_H, "x-orion-admin-totp": "123456"})
        assert s == 403 and b["error"] == "invalid_totp"
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={**OWNER_H, "x-orion-admin-totp": lf.totp_now(secret)})
        assert s == 200

        # And it can be switched back off.
        s, b, _ = invoke(lf, "POST", "/api/admin/config",
                         headers={**OWNER_H, "x-orion-admin-totp": lf.totp_now(secret)},
                         body={"action": "totp_disable", "reason": "travelling"})
        assert s == 200 and b["owner_totp_required"] is False
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 200

    def test_totp_accepts_one_step_of_drift_but_not_two(self, lf):
        secret = lf.gen_totp_secret()
        now = time.time()
        assert lf.totp_verify(secret, lf.totp_at(secret, int(now // 30)), at=now)
        assert lf.totp_verify(secret, lf.totp_at(secret, int(now // 30) - 1), at=now)
        assert lf.totp_verify(secret, lf.totp_at(secret, int(now // 30) + 1), at=now)
        assert not lf.totp_verify(secret, lf.totp_at(secret, int(now // 30) + 2), at=now)

    def test_rfc6238_reference_vector(self, lf):
        """RFC 6238 appendix B, SHA-1 seed "12345678901234567890" at T=59s."""
        import base64 as _b64
        secret = _b64.b32encode(b"12345678901234567890").decode()
        assert lf.totp_at(secret, 59 // 30) == "287082"

    def test_requirement_on_with_no_secret_fails_closed(self, lf):
        import boto3
        lf.config_set("owner_totp_required", True)
        try:
            boto3.client("ssm", region_name="us-east-1").delete_parameter(
                Name="/orion/owner_totp_secret")
        except Exception:
            pass
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={**OWNER_H, "x-orion-admin-totp": "123456"})
        assert s == 403 and b["error"] == "totp_not_provisioned"


class TestIpAllowlist:
    def test_allowlist_gates_the_admin_surface(self, lf):
        lf.config_set("owner_ip_allowlist", ["203.0.113.0/24", "198.51.100.7"])
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={**OWNER_H, "x-forwarded-for": "203.0.113.9"})
        assert s == 200
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={**OWNER_H, "x-forwarded-for": "198.51.100.7"})
        assert s == 200
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={**OWNER_H, "x-forwarded-for": "192.0.2.5"})
        assert s == 403 and b["error"] == "ip_not_allowed"
        # A non-empty allowlist with no resolvable client IP fails CLOSED.
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 403 and b["error"] == "ip_not_allowed"

    def test_denials_are_audited(self, lf):
        lf.config_set("owner_ip_allowlist", ["203.0.113.0/24"])
        invoke(lf, "GET", "/api/admin/whoami",
               headers={**OWNER_H, "x-forwarded-for": "192.0.2.5"})
        rows = audit_rows(lf, "owner.ip_denied")
        assert rows and rows[0]["result"] == "ip_not_allowed"

    def test_empty_allowlist_allows_everything(self, lf):
        s, b, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 200


class TestRevokeFastCodes:
    """§5: the launcher switches on CODES. Prose lives in `message`."""

    def _check(self, lf, key, machine="M-R", **body):
        return invoke(lf, "POST", "/api/license/check",
                      body={"license_key": key, "machine_id": machine, **body})

    def test_every_kill_code(self, lf):
        cases = []
        put_license(lf, "ORION-C1-BBBB-CCCC", machine_id="M-R", revoked=True)
        cases.append(("ORION-C1-BBBB-CCCC", "revoked"))
        put_license(lf, "ORION-C2-BBBB-CCCC", machine_id="M-R",
                    expiry=int(time.time()) - 10)
        cases.append(("ORION-C2-BBBB-CCCC", "expired"))
        put_license(lf, "ORION-C3-BBBB-CCCC", machine_id="OTHER")
        cases.append(("ORION-C3-BBBB-CCCC", "device_mismatch"))
        cases.append(("ORION-NOPE-NOPE-NOPE", "invalid_key"))
        put_license(lf, "ORION-C4-BBBB-CCCC", machine_id="M-R", status="suspended")
        cases.append(("ORION-C4-BBBB-CCCC", "inactive"))
        put_license(lf, "ORION-C5-BBBB-CCCC", machine_id="M-R", status="frozen")
        cases.append(("ORION-C5-BBBB-CCCC", "frozen"))
        for key, code in cases:
            s, b, _ = self._check(lf, key)
            assert s == 403, (key, b)
            assert b["error"] == code, (key, b)
            assert b.get("message"), f"{code} carries no human message"
            assert " " not in b["error"], "error must stay a machine-readable code"

    def test_service_disabled_code(self, lf):
        put_license(lf, "ORION-C6-BBBB-CCCC", machine_id="M-R")
        lf.config_table().put_item(Item={"config_key": "global_kill",
                                         "enabled": True, "reason": "maintenance"})
        s, b, _ = self._check(lf, "ORION-C6-BBBB-CCCC")
        assert s == 503 and b["error"] == "service_disabled"
        assert b["message"] == "maintenance"

    def test_blacklisted_code(self, lf):
        put_license(lf, "ORION-C7-BBBB-CCCC", machine_id="M-BL")
        lf.licenses_table().put_item(Item={
            "license_key": "BLACKLIST#machine#M-BL", "status": "blacklist",
            "revoked": True, "created_at": int(time.time())})
        s, b, _ = self._check(lf, "ORION-C7-BBBB-CCCC", machine="M-BL")
        assert s == 403 and b["error"] == "blacklisted"

    def test_heartbeat_stamps_last_check_and_version(self, lf):
        put_license(lf, "ORION-C8-BBBB-CCCC", machine_id="M-R")
        s, b, _ = self._check(lf, "ORION-C8-BBBB-CCCC", client_version="1.4.2")
        assert s == 200
        row = lf.licenses_table().get_item(Key={"license_key": "ORION-C8-BBBB-CCCC"})["Item"]
        assert int(row["last_check_at"]) > 0
        assert row["client_version"] == "1.4.2"


class TestKillswitchWriteRemoval:
    def test_status_still_reads(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/bot/killswitch",
                         body={"action": "status"}, headers=BOT_H)
        assert s == 200 and b["ok"] is True and b["enabled"] is False

    @pytest.mark.parametrize("action", ["on", "enable", "engage", "off", "disable", "disengage"])
    def test_writes_are_refused(self, lf, action):
        s, b, _ = invoke(lf, "POST", "/api/bot/killswitch",
                         body={"action": action, "by": "someone"}, headers=BOT_H)
        assert s == 403 and b["error"] == "write_removed"
        # ...and the config is untouched.
        assert lf.get_global_kill()[0] is False

    def test_attempt_is_audited(self, lf):
        invoke(lf, "POST", "/api/bot/killswitch",
               body={"action": "on", "by": "leaked-secret"}, headers=BOT_H)
        rows = audit_rows(lf, "config.killswitch_write_denied")
        assert rows and rows[0]["result"] == "write_removed"

    def test_owner_still_owns_the_global_kill(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H, body={
            "action": "set", "global_kill": {"enabled": True, "reason": "incident"},
            "reason": "incident response"})
        assert s == 200
        assert lf.get_global_kill() == (True, "incident")


# ══════════════════════════════════════════════════════════════════════════════
# §2 blacklist + §6 fraud + metrics + config
# ══════════════════════════════════════════════════════════════════════════════
class TestBlacklist:
    def test_machine_blacklist_blocks_activate_and_check(self, lf):
        put_license(lf, "ORION-BL1-BBBB-CCCC", machine_id="")
        s, b, _ = _mutate(lf, "blacklist", OWNER_H, key="ORION-BL1-BBBB-CCCC",
                          machine_id="M-BAD", reason="chargeback farm")
        assert s == 200 and b["entries"] == ["BLACKLIST#machine#M-BAD"]
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-BL1-BBBB-CCCC", "machine_id": "M-BAD", **nt})
        assert s == 403 and b["error"] == "blacklisted"

    def test_discord_blacklist_blocks_activate(self, lf):
        put_license(lf, "ORION-BL2-BBBB-CCCC", machine_id="",
                    extra={"discord_user_id": "1313"})
        _mutate(lf, "blacklist", OWNER_H, key="ORION-BL2-BBBB-CCCC",
                discord_user_id="1313", reason="abuse")
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-BL2-BBBB-CCCC", "machine_id": "M-OK", **nt})
        assert s == 403 and b["error"] == "blacklisted"

    def test_unblacklist_restores_access(self, lf):
        put_license(lf, "ORION-BL3-BBBB-CCCC", machine_id="")
        _mutate(lf, "blacklist", OWNER_H, key="ORION-BL3-BBBB-CCCC",
                machine_id="M-OOPS", reason="mistake")
        s, b, _ = _mutate(lf, "unblacklist", OWNER_H, key="ORION-BL3-BBBB-CCCC",
                          machine_id="M-OOPS", reason="mistake, reverting")
        assert s == 200 and b["blacklisted"] is False
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-BL3-BBBB-CCCC", "machine_id": "M-OOPS", **nt})
        assert s == 200

    def test_only_the_owner_may_blacklist(self, lf):
        adm = staff_login(lf, "admBL", role="admin")
        put_license(lf, "ORION-BL4-BBBB-CCCC")
        s, b, _ = _staff_mutate(lf, "blacklist", adm, key="ORION-BL4-BBBB-CCCC",
                                machine_id="M-X")
        assert s == 403 and b["error"] == "forbidden"

    def test_blacklist_is_audited(self, lf):
        put_license(lf, "ORION-BL5-BBBB-CCCC")
        _mutate(lf, "blacklist", OWNER_H, key="ORION-BL5-BBBB-CCCC",
                machine_id="M-AUD", reason="fraud ring")
        rows = audit_rows(lf, "blacklist.add")
        assert rows and rows[0]["reason"] == "fraud ring"
        assert rows[0]["actor_type"] == "owner"


class TestFraudSignals:
    def test_too_many_resets_flags_but_does_not_kill(self, lf):
        now = int(time.time())
        hist = [{"ts": now - i * 3600, "by": "customer:1", "mode": "free",
                 "machine_before_suffix": f"MM{i:04d}"} for i in range(5)]
        put_license(lf, "ORION-FR1-BBBB-CCCC", machine_id="",
                    extra={"reset_history": hist})
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-FR1-BBBB-CCCC", "machine_id": "M-NEW", **nt})
        assert s == 200, "a fraud signal must FLAG, never auto-kill"
        row = lf.licenses_table().get_item(Key={"license_key": "ORION-FR1-BBBB-CCCC"})["Item"]
        assert row["flags"]["suspect"] is True
        assert int(row["flags"]["suspect_signals"]["resets_30d"]) == 5
        assert audit_rows(lf, "fraud.flag")

    def test_too_many_machines_flags(self, lf):
        now = int(time.time())
        mh = [{"suffix": f"AAA{i:03d}", "ts": now - i * 100} for i in range(4)]
        put_license(lf, "ORION-FR2-BBBB-CCCC", machine_id="",
                    extra={"machine_history": mh})
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-FR2-BBBB-CCCC", "machine_id": "M-FIFTH", **nt})
        assert s == 200
        row = lf.licenses_table().get_item(Key={"license_key": "ORION-FR2-BBBB-CCCC"})["Item"]
        assert row["flags"]["suspect"] is True

    def test_a_clean_key_is_not_flagged(self, lf):
        put_license(lf, "ORION-FR3-BBBB-CCCC", machine_id="")
        nt = make_nonce_ts()
        invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-FR3-BBBB-CCCC", "machine_id": "M-ONE", **nt})
        row = lf.licenses_table().get_item(Key={"license_key": "ORION-FR3-BBBB-CCCC"})["Item"]
        assert not row.get("flags", {}).get("suspect")
        assert len(row["machine_history"]) == 1

    def test_thresholds_are_configurable(self, lf):
        lf.config_set("fraud_thresholds", {"machines_30d": 99, "resets_30d": 99})
        now = int(time.time())
        hist = [{"ts": now - i, "by": "customer:1", "mode": "free",
                 "machine_before_suffix": "x"} for i in range(8)]
        put_license(lf, "ORION-FR4-BBBB-CCCC", machine_id="",
                    extra={"reset_history": hist})
        nt = make_nonce_ts()
        invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-FR4-BBBB-CCCC", "machine_id": "M-N", **nt})
        row = lf.licenses_table().get_item(Key={"license_key": "ORION-FR4-BBBB-CCCC"})["Item"]
        assert not row.get("flags", {}).get("suspect")

    def test_chargeback_revokes_and_audits(self, lf):
        put_license(lf, "ORION-CB1-BBBB-CCCC", extra={"discord_user_id": "2222"})
        s, b, _ = invoke(lf, "POST", "/api/bot/chargeback", headers=BOT_H, body={
            "kind": "chargebacked", "discord_user_id": "2222", "reason": "gumroad dispute"})
        assert s == 200 and b["revoked"] is True
        row = lf.licenses_table().get_item(Key={"license_key": "ORION-CB1-BBBB-CCCC"})["Item"]
        assert row["revoked"] is True and row["status"] == "revoked"
        rows = audit_rows(lf, "webhook.chargeback")
        assert rows and rows[0]["actor_type"] == "webhook"


class TestMetrics:
    def test_shape(self, lf):
        now = int(time.time())
        put_license(lf, "ORION-MT1-BBBB-CCCC", plan="month", machine_id="M-1",
                    extra={"last_check_at": now - 10, "client_version": "1.4.2",
                           "last_activated": now - 100})
        put_license(lf, "ORION-MT2-BBBB-CCCC", plan="lifetime", revoked=True,
                    status="revoked")
        put_license(lf, "ORION-MT3-BBBB-CCCC", plan="month", status="frozen")
        put_license(lf, "ORION-MT4-BBBB-CCCC", plan="week", expiry=now - 5)
        s, b, _ = invoke(lf, "GET", "/api/admin/metrics", headers=OWNER_H)
        assert s == 200 and b["ok"] is True
        for k in ("licenses", "trials", "activations", "online_now", "resets",
                  "versions", "staff", "fraud_flagged"):
            assert k in b, f"metrics missing {k}"
        assert set(b["licenses"]) >= {"active", "frozen", "revoked", "expired", "by_plan"}
        assert set(b["trials"]) >= {"active", "claimed_7d", "converted_30d"}
        assert set(b["activations"]) >= {"24h", "7d"}
        assert set(b["resets"]) >= {"24h", "7d", "paid_7d", "deduct_7d"}
        assert set(b["staff"]) >= {"active", "actions_24h"}
        assert b["licenses"]["active"] == 1
        assert b["licenses"]["revoked"] == 1
        assert b["licenses"]["frozen"] == 1
        assert b["licenses"]["expired"] == 1
        assert b["licenses"]["by_plan"]["month"] == 2
        assert b["online_now"] == 1
        assert b["versions"]["1.4.2"] == 1
        assert b["activations"]["24h"] == 1

    def test_marker_rows_are_not_counted_as_licenses(self, lf):
        lf.licenses_table().put_item(Item={"license_key": "ORDER#x", "status": "order_claim"})
        lf.licenses_table().put_item(Item={"license_key": "BLACKLIST#machine#y",
                                           "status": "blacklist"})
        lf.licenses_table().put_item(Item={"license_key": "TRIALMACHINE#z",
                                           "status": "trial_machine_claim"})
        s, b, _ = invoke(lf, "GET", "/api/admin/metrics", headers=OWNER_H)
        assert b["licenses"]["active"] == 0
        assert b["licenses"]["by_plan"] == {}

    def test_metrics_are_owner_only(self, lf):
        adm = staff_login(lf, "admM", role="admin")
        s, b, _ = invoke(lf, "GET", "/api/admin/metrics", headers=adm)
        assert s == 403

    def test_metrics_are_rate_limited(self, lf):
        codes = [invoke(lf, "GET", "/api/admin/metrics", headers=OWNER_H)[0]
                 for _ in range(8)]
        assert codes.count(429) >= 1, "metrics must be rate-limited (6/min)"


class TestConfig:
    def test_defaults_are_served_before_anything_is_written(self, lf):
        s, b, _ = invoke(lf, "GET", "/api/admin/config", headers=OWNER_H)
        assert s == 200
        c = b["config"]
        assert c["reset_policy_defaults"]["free_resets"] == 3
        assert c["reset_policy_defaults"]["penalty_days"] == 1     # owner rule: 1 day
        assert c["reset_policy_defaults"]["cooldown_s"] == 86400
        assert c["reset_policy_defaults"]["self_service"] is True
        assert "price_url" not in c["reset_policy_defaults"]
        assert c["fraud_thresholds"] == {"machines_30d": 3, "resets_30d": 4}
        assert c["owner_totp_required"] is False
        assert c["owner_ip_allowlist"] == []
        assert c["blocked_versions"] == []
        assert c["global_kill"]["enabled"] is False
        assert "license.revoke" in c["alerts"]["events"]

    def test_set_and_read_back(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H, body={
            "action": "set", "min_client_version": "1.9.0",
            "reason": "dropping old builds"})
        assert s == 200 and b["applied"]["min_client_version"] == "1.9.0"
        s, b, _ = invoke(lf, "GET", "/api/admin/config", headers=OWNER_H)
        assert b["config"]["min_client_version"] == "1.9.0"
        rows = audit_rows(lf, "config.set")
        assert rows and rows[0]["target"] == "min_client_version"

    def test_unknown_key_rejected(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H, body={
            "action": "set", "config": {"rm_rf": True}, "reason": "nope"})
        assert s == 400 and b["error"] == "unknown_config_key"

    def test_config_is_owner_only(self, lf):
        adm = staff_login(lf, "admC", role="admin")
        s, b, _ = invoke(lf, "GET", "/api/admin/config", headers=adm)
        assert s == 403

    def test_rotate_admin_secret(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/config", headers=OWNER_H, body={
            "action": "rotate_admin_secret", "reason": "quarterly rotation"})
        assert s == 200 and b["admin_secret"] and b["admin_secret"] != TEST_ADMIN_SECRET
        # The old secret stops working immediately; the new one works.
        s, _, _ = invoke(lf, "GET", "/api/admin/whoami", headers=OWNER_H)
        assert s == 403
        s, _, _ = invoke(lf, "GET", "/api/admin/whoami",
                         headers={"x-orion-admin-secret": b["admin_secret"]})
        assert s == 200
        assert audit_rows(lf, "config.rotate_admin_secret")


# ══════════════════════════════════════════════════════════════════════════════
# §2/§4 remaining route behaviour
# ══════════════════════════════════════════════════════════════════════════════
class TestDeliverActorRequirement:
    def test_missing_actor_is_refused(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H,
                         body={"plan": "month", "discord_id": "5"})
        assert s == 400 and b["error"] == "actor_required"

    def test_non_staff_actor_is_refused(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H,
                         body={"plan": "month", "discord_id": "5",
                               "actor_discord_id": "999999"})
        assert s == 403 and b["error"] == "not_staff"

    def test_support_actor_cannot_mint(self, lf):
        put_staff(lf, "sup_d", role="support", discord_user_id="8001")
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H,
                         body={"plan": "month", "discord_id": "5",
                               "actor_discord_id": "8001"})
        assert s == 403 and b["error"] == "forbidden"

    def test_disabled_staff_actor_is_refused(self, lf):
        put_staff(lf, "adm_off", role="admin", discord_user_id="8002", disabled=True)
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H,
                         body={"plan": "month", "discord_id": "5",
                               "actor_discord_id": "8002"})
        assert s == 403 and b["error"] == "staff_disabled"

    def test_admin_actor_mints_and_is_audited(self, lf):
        put_staff(lf, "adm_d", role="admin", discord_user_id="8003")
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H,
                         body={"plan": "month", "discord_id": "5",
                               "actor_discord_id": "8003", "reason": "goodwill"})
        assert s == 200 and b["license_key"]
        rows = [r for r in audit_rows(lf, "license.create")
                if (r.get("details") or {}).get("route") == "bot_deliver"]
        assert rows and rows[0]["actor_id"] == "adm_d"
        assert rows[0]["reason"] == "goodwill"


class TestBotSurfaceContract:
    """Fields and codes the Discord bot / Cloudflare worker / launcher read back.
    These are the cross-workstream handshake — breaking them breaks a front-end."""

    def _seed(self, lf, discord_id="4242", key="ORION-BOT1-BBBB-CCCC", **lic):
        lf.config_set("reset_policy_defaults", {
            "free_resets": 3, "penalty_days": 1, "cooldown_s": 0,
            "self_service": True})
        extra = dict(lic.pop("extra", {}))
        extra.setdefault("discord_user_id", discord_id)
        put_license(lf, key, machine_id="MACHINE-BOTOLD", extra=extra, **lic)
        return key

    def test_hwid_reset_success_fields(self, lf):
        self._seed(lf)
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-reset", headers=BOT_H,
                         body={"discord_id": "4242", "actor_discord_id": "4242"})
        assert s == 200 and b["ok"] is True
        for field in ("mode", "hwid_free_resets", "hwid_resets_used",
                      "hwid_paid_credits", "penalty_days", "deduct_days",
                      "expiry", "lifetime"):
            assert field in b, f"hwid-reset response missing {field}"
        assert b["mode"] in ("free", "paid", "deduct")

    def test_hwid_reset_refusal_codes(self, lf):
        self._seed(lf, extra={"hwid_resets_used": 3})
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-reset", headers=BOT_H,
                         body={"discord_id": "4242"})
        assert b["error"] == "payment_required"       # kept for front-end compat
        assert b["confirm_required"] is True
        assert b["penalty_days"] == 1 and b["deduct_days"] == 1

    def test_no_reply_ever_links_a_store(self, lf):
        """Owner rule 2026-09-15: there is no reset to buy. A stale price_url left
        in the config row must NOT resurface on the wire."""
        self._seed(lf, extra={"hwid_resets_used": 3})
        lf.config_set("reset_policy_defaults", {"free_resets": 3, "penalty_days": 1,
                                                "cooldown_s": 0, "self_service": True,
                                                "price_url": "https://old.example/x"})
        for body in ({"discord_id": "4242"}, {"discord_id": "4242", "mode": "deduct"}):
            s, b, _ = invoke(lf, "POST", "/api/bot/hwid-reset", headers=BOT_H, body=body)
            assert "price_url" not in b, b
            assert "old.example" not in json.dumps(b)

    def test_cross_account_reset_requires_staff(self, lf):
        self._seed(lf)
        # Another customer cannot reset someone else's key.
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-reset", headers=BOT_H,
                         body={"discord_id": "4242", "actor_discord_id": "9999"})
        assert s == 403 and b["error"] == "not_staff", "must be distinct from `forbidden`"
        # A staff member can (support+ is enough for a reset).
        put_staff(lf, "sup_x", role="support", discord_user_id="9999")
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-reset", headers=BOT_H,
                         body={"discord_id": "4242", "actor_discord_id": "9999"})
        assert s == 200 and b["ok"] is True
        rows = audit_rows(lf, "license.reset_machine")
        assert rows[0]["actor_type"] == "staff" and rows[0]["actor_id"] == "sup_x"

    def test_cross_account_status_requires_staff(self, lf):
        self._seed(lf)
        s, b, _ = invoke(lf, "POST", "/api/bot/status", headers=BOT_H,
                         body={"discord_id": "4242", "actor_discord_id": "9999"})
        assert s == 403 and b["error"] == "not_staff"
        # Self-lookup stays open.
        s, b, _ = invoke(lf, "POST", "/api/bot/status", headers=BOT_H,
                         body={"discord_id": "4242", "actor_discord_id": "4242"})
        assert s == 200 and b["has_license"] is True
        # And so does the legacy call with no actor at all.
        s, b, _ = invoke(lf, "POST", "/api/bot/status", headers=BOT_H,
                         body={"discord_id": "4242"})
        assert s == 200

    def test_disabled_staff_cannot_act_cross_account(self, lf):
        self._seed(lf)
        put_staff(lf, "sup_off", role="support", discord_user_id="8888", disabled=True)
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-reset", headers=BOT_H,
                         body={"discord_id": "4242", "actor_discord_id": "8888"})
        assert s == 403 and b["error"] == "not_staff"

    def test_hwid_credit_route(self, lf):
        key = self._seed(lf)
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-credit", headers=BOT_H,
                         body={"discord_user_id": "4242", "order_id": "HC-1"})
        assert s == 200 and b["hwid_paid_credits"] == 1
        # Spend-once on order_id.
        s, b, _ = invoke(lf, "POST", "/api/bot/hwid-credit", headers=BOT_H,
                         body={"discord_user_id": "4242", "order_id": "HC-1"})
        assert b.get("duplicate_order") is True
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert int(row["hwid_paid_credits"]) == 1
        rows = audit_rows(lf, "license.grant_reset_credit")
        assert rows and rows[0]["actor_type"] == "webhook"

    def test_deliver_bulk_count(self, lf):
        put_staff(lf, "adm_bulk", role="admin", discord_user_id="7100",
                  caps={"keys_per_day": 10, "resets_per_day": 5, "extend_max_days": 30})
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H, body={
            "plan": "week", "count": 3, "actor_discord_id": "7100",
            "reason": "giveaway", "note": "stream drop"})
        assert s == 200
        assert len(b["keys"]) == 3 and b["count"] == 3
        assert b["license_key"] == b["keys"][0]["license_key"]
        row = lf.licenses_table().get_item(
            Key={"license_key": b["keys"][0]["license_key"]})["Item"]
        assert row["note"] == "stream drop"
        # count==1 keeps the single-key shape the bot already renders.
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H, body={
            "plan": "week", "actor_discord_id": "7100", "reason": "one-off"})
        assert b["license_key"] and b["count"] == 1

    def test_deliver_count_is_bounded_and_capped(self, lf):
        put_staff(lf, "adm_cap", role="admin", discord_user_id="7200",
                  caps={"keys_per_day": 2, "resets_per_day": 5, "extend_max_days": 30})
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H, body={
            "plan": "week", "count": 26, "actor_discord_id": "7200", "reason": "x"})
        assert s == 400 and b["error"] == "invalid_count"
        s, b, _ = invoke(lf, "POST", "/api/bot/deliver", headers=BOT_H, body={
            "plan": "week", "count": 3, "actor_discord_id": "7200", "reason": "x"})
        assert s == 429 and b["error"] == "cap_exceeded"

    def test_alert_target_endpoint(self, lf):
        lf.config_set("alerts", {"owner_discord_user_id": "5150",
                                 "events": ["license.revoke"]})
        s, b, _ = invoke(lf, "GET", "/api/bot/alert-target", headers=BOT_H)
        assert s == 200 and b["owner_discord_user_id"] == "5150"
        assert b["events"] == ["license.revoke"]
        s, b, _ = invoke(lf, "GET", "/api/bot/alert-target")
        assert s == 403

    def test_motd_until_is_unix_seconds(self, lf):
        until = int(time.time()) + 600
        lf.config_set("motd", {"text": "hi", "level": "warn", "until": until})
        s, b, _ = invoke(lf, "GET", "/api/version")
        assert isinstance(b["motd"]["until"], int)
        assert b["motd"]["until"] == until
        # A string in config is still served as a number.
        lf.config_set("motd", {"text": "hi", "level": "warn", "until": str(until)})
        s, b, _ = invoke(lf, "GET", "/api/version")
        assert isinstance(b["motd"]["until"], int)

    def test_min_client_version_returned_on_both_gates(self, lf):
        lf.config_set("min_client_version", "3.0.0")
        put_license(lf, "ORION-MCV-BBBB-CCCC", machine_id="")
        nt = make_nonce_ts()
        s, b, _ = invoke(lf, "POST", "/api/activate", body={
            "license_key": "ORION-MCV-BBBB-CCCC", "machine_id": "M-V",
            "client_version": "1.0.0", **nt})
        assert s == 403 and b["min_client_version"] == "3.0.0"
        put_license(lf, "ORION-MCV2-BBBB-CCCC", machine_id="M-V")
        s, b, _ = invoke(lf, "POST", "/api/license/check", body={
            "license_key": "ORION-MCV2-BBBB-CCCC", "machine_id": "M-V",
            "client_version": "1.0.0"})
        assert s == 403 and b["min_client_version"] == "3.0.0"

    @pytest.mark.parametrize("route,body", [
        ("admin_license", {"action": "revoke", "key": "ORION-RV-BBBB-CCCC",
                           "reason": "refund"}),
        ("admin_kill", {"target_type": "license_key", "target_id": "ORION-RV-BBBB-CCCC",
                        "reason": "refund"}),
        ("chargeback", {"kind": "refunded", "key": "ORION-RV-BBBB-CCCC",
                        "reason": "refund"}),
    ])
    def test_every_revoke_path_sets_both_status_and_flag(self, lf, route, body):
        """The webhook workstream found refunds slipping through on /api/activate,
        which reads the BOOLEAN while /api/license/check reads both. Every revoke
        path must write status="revoked" AND revoked=True."""
        key = "ORION-RV-BBBB-CCCC"
        put_license(lf, key, machine_id="")
        if route == "admin_license":
            s, _, _ = invoke(lf, "POST", "/api/admin/license", headers=OWNER_H,
                             body={**body})
        elif route == "admin_kill":
            s, _, _ = invoke(lf, "POST", "/api/admin/kill", headers=OWNER_H, body=body)
        else:
            s, _, _ = invoke(lf, "POST", "/api/bot/chargeback", headers=BOT_H, body=body)
        assert s == 200
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert row["status"] == "revoked", route
        assert row["revoked"] is True, route
        # Both gates now agree.
        nt = make_nonce_ts()
        sa, ba, _ = invoke(lf, "POST", "/api/activate",
                           body={"license_key": key, "machine_id": "M-R", **nt})
        assert sa == 403 and ba["error"] == "license_revoked"
        sc, bc, _ = invoke(lf, "POST", "/api/license/check",
                           body={"license_key": key, "machine_id": "M-R"})
        assert sc == 403 and bc["error"] in ("revoked", "device_mismatch")


class TestLicenseCreateFix:
    def test_owner_provision_key_needs_discord_subscription(self, lf):
        """A manual admin key is not itself a paid Discord entitlement."""
        s, b, _ = invoke(lf, "POST", "/api/admin/provision", headers=OWNER_H,
                         body={"plan": "month", "days": 30, "reason": "manual sale"})
        assert s == 201
        key = b["license_key"]
        assert b["plan"] == "month" and b["expiry"] > int(time.time())
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert row["plan"] == "month" and int(row["expiry"]) > 0
        nt = make_nonce_ts()
        s, b2, _ = invoke(lf, "POST", "/api/activate",
                          body={"license_key": key, "machine_id": "M-P", **nt})
        assert s == 403 and b2["error"] == "subscription_required"

    def test_manual_lifetime_plan_gets_expiry_zero_but_no_entitlement(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/provision", headers=OWNER_H,
                         body={"plan": "lifetime", "reason": "lifetime sale"})
        assert b["expiry"] == 0
        nt = make_nonce_ts()
        s, b2, _ = invoke(lf, "POST", "/api/activate",
                          body={"license_key": b["license_key"],
                                "machine_id": "M-L", **nt})
        assert s == 403 and b2["error"] == "subscription_required"

    def test_bulk_create_is_bounded(self, lf):
        s, b, _ = invoke(lf, "POST", "/api/admin/license", headers=OWNER_H, body={
            "action": "create", "plan": "week", "count": 26, "reason": "bulk"})
        assert s == 400 and b["error"] == "invalid_count"
        s, b, _ = invoke(lf, "POST", "/api/admin/license", headers=OWNER_H, body={
            "action": "create", "plan": "week", "count": 3, "reason": "bulk"})
        assert s == 201 and len(b["keys"]) == 3


class TestFreezeClock:
    def test_freeze_stops_the_clock_and_unfreeze_gives_it_back(self, lf, monkeypatch):
        now = int(time.time())
        key = "ORION-FZ1-BBBB-CCCC"
        put_license(lf, key, machine_id="M-F", expiry=now + 10 * 86400)
        s, b, _ = _mutate(lf, "freeze", OWNER_H, key=key, reason="customer deployed")
        assert s == 200 and b["status"] == "frozen"
        # Heartbeat reports `frozen` while paused.
        s, hb, _ = invoke(lf, "POST", "/api/license/check",
                          body={"license_key": key, "machine_id": "M-F"})
        assert s == 403 and hb["error"] == "frozen"
        # Pretend three days went by while frozen.
        lf.licenses_table().update_item(
            Key={"license_key": key}, UpdateExpression="SET frozen_at = :f",
            ExpressionAttributeValues={":f": now - 3 * 86400})
        s, b, _ = _mutate(lf, "unfreeze", OWNER_H, key=key, reason="back")
        assert s == 200
        assert abs(b["expiry"] - (now + 13 * 86400)) <= 5
        s, hb, _ = invoke(lf, "POST", "/api/license/check",
                          body={"license_key": key, "machine_id": "M-F"})
        assert s == 200

    def test_double_freeze_refused(self, lf):
        key = "ORION-FZ2-BBBB-CCCC"
        put_license(lf, key, machine_id="M-F")
        _mutate(lf, "freeze", OWNER_H, key=key)
        s, b, _ = _mutate(lf, "freeze", OWNER_H, key=key)
        assert s == 409 and b["error"] == "already_frozen"


class TestLookupHandles:
    def test_lookup_by_discord_email_and_machine(self, lf):
        put_license(lf, "ORION-LK1-BBBB-CCCC", machine_id="M-LK",
                    email="buyer@example.com",
                    extra={"discord_user_id": "31337"})
        for body, mode in (({"discord_user_id": "31337"}, "gsi"),
                           ({"email": "buyer@example.com"}, "scan"),
                           ({"machine_id": "M-LK"}, "scan"),
                           ({"key": "ORION-LK1-BBBB-CCCC"}, "key")):
            s, b, _ = _mutate(lf, "lookup", OWNER_H, **body)
            assert s == 200, (body, b)
            assert b["license"]["license_key_suffix"] == "CCCC"
            assert b["lookup_mode"] == mode

    def test_staff_lookup_accepts_key_or_license_key(self, lf):
        """The Discord bot sends BOTH field names; either must resolve, and the
        response must carry the keys the bot renders."""
        put_license(lf, "ORION-LK3-BBBB-CCCC", machine_id="MACHINE-BOT1",
                    plan="month", activations=1)
        sup = staff_login(lf, "supLK", role="support")
        for body in ({"license_key": "ORION-LK3-BBBB-CCCC"},
                     {"key": "ORION-LK3-BBBB-CCCC"},
                     {"key": "ORION-LK3-BBBB-CCCC",
                      "license_key": "ORION-LK3-BBBB-CCCC"}):
            s, b, _ = _staff_mutate(lf, "lookup", sup, **body)
            assert s == 200, (body, b)
            lic = b["license"]
            for field in ("license_key_suffix", "status", "revoked", "plan",
                          "expiry", "activations", "machine_suffix"):
                assert field in lic, f"lookup response missing {field}"
            assert lic["machine_suffix"] == "BOT1"

    def test_transfer_keeps_the_reset_counters_with_the_key(self, lf):
        key = "ORION-LK2-BBBB-CCCC"
        put_license(lf, key, machine_id="M-OLD",
                    extra={"discord_user_id": "1", "hwid_resets_used": 2})
        s, b, _ = _mutate(lf, "transfer", OWNER_H, key=key, discord_user_id="2",
                          reason="sold on")
        assert s == 200
        row = lf.licenses_table().get_item(Key={"license_key": key})["Item"]
        assert row["discord_user_id"] == "2"
        assert row["machine_id"] == ""
        assert int(row["hwid_resets_used"]) == 2, "counters travel WITH the key"
