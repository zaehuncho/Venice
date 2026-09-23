"""Cloudflare-Worker half of the owner's 2026-09-15 pricing rule.

Two commands changed shape and this pins both:

  /purchase     ONE ephemeral embed + ONE link button to the website. No tiers, no
                prices, no {{PRICE_*}} templates — the price lives on the site so
                changing it never needs a Worker deploy.
  /hwid_reset   three free resets, then ONE day off the subscription. Nothing in
                any reply may sell a reset any more.

The Worker is ESM with no JS test runner in this repo, so each case shells out to
`node tests/discord/worker_probe.mjs`, which imports the REAL module with a fake
`env` and a stubbed `fetch` and prints the reply as JSON. Skips (does not fail) if
node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "worker_probe.mjs")
WORKER = os.path.abspath(os.path.join(HERE, "..", "..", "discord_launch", "orion_worker.js"))

STORE = "https://venice.example"
ENV = {"STORE_URL": STORE, "ORION_API_BASE": "https://api.invalid",
       "ORION_BOT_SECRET": "s", "ORION_EDGE_AUTH": "e"}


def probe(**payload):
    # bytes, not text=True: Windows would decode node's UTF-8 (emoji, en dashes)
    # as cp1252 and blow up inside the reader thread.
    r = subprocess.run([NODE, PROBE, json.dumps(payload)],
                       capture_output=True, timeout=60)
    assert r.returncode == 0, f"probe failed: {r.stderr.decode('utf-8', 'replace')[-2000:]}"
    return json.loads(r.stdout.decode("utf-8"))


def purchase(env=None, lambda_reply=None):
    return probe(probe="purchase", command="purchase", env=env if env is not None else ENV,
                 lambdaReply=lambda_reply or {"ok": True, "has_license": False})


def buttons(reply):
    return [c for row in reply.get("components") or [] for c in row.get("components", [])]


# ── /purchase ─────────────────────────────────────────────────────────────────

class TestPurchase:
    def test_one_embed_and_exactly_one_link_button(self):
        r = purchase()
        assert len(r["embeds"]) == 1
        btns = buttons(r)
        assert len(btns) == 1, btns
        assert btns[0]["style"] == 5 and btns[0]["url"] == STORE
        assert btns[0]["label"] == "Subscribe on the website"

    def test_no_tier_names_and_no_prices_anywhere(self):
        blob = json.dumps(purchase()).lower()
        for banned in ("7 days", "1 month", "lifetime", "{{price", "$", "tier"):
            assert banned not in blob, f"/purchase still shows {banned!r}"

    def test_it_points_at_the_free_trial(self):
        assert "7-day trial" in json.dumps(purchase())

    def test_store_url_wins_over_the_legacy_gumroad_base(self):
        r = purchase(env=dict(ENV, GUMROAD_BASE="https://seller.gumroad.com/l"))
        assert buttons(r)[0]["url"] == STORE

    def test_gumroad_base_is_the_fallback_when_store_url_is_unset(self):
        """A Worker deployed before STORE_URL existed must still show a button."""
        env = {k: v for k, v in ENV.items() if k != "STORE_URL"}
        r = purchase(env=dict(env, GUMROAD_BASE="https://seller.gumroad.com/l"))
        assert buttons(r)[0]["url"] == "https://seller.gumroad.com/l"

    def test_an_unconfigured_store_warns_instead_of_sending_a_broken_button(self):
        """Discord rejects the whole message on a malformed URL, which would leave
        the user staring at 'thinking…' forever."""
        env = {k: v for k, v in ENV.items() if k != "STORE_URL"}
        r = purchase(env=dict(env, GUMROAD_BASE="https://<seller>.gumroad.com/l"))
        assert buttons(r) == []
        assert "STORE_URL" in json.dumps(r["embeds"][0])

    def test_an_existing_licence_is_still_shown(self):
        r = purchase(lambda_reply={"ok": True, "has_license": True, "active": True,
                                   "plan": "month", "expiry": 1800000000, "bound": True})
        field = r["embeds"][0]["fields"][0]
        assert "already have a license" in field["name"]
        assert "month" in field["value"] and "hwid_reset" in field["value"]
        assert buttons(r), "the subscribe button stays available"

    def test_a_failed_lookup_still_shows_the_button(self):
        r = purchase(lambda_reply={"ok": False, "error": "boom"})
        assert buttons(r)
        assert "still works" in json.dumps(r["embeds"][0])


# ── /hwid_reset ───────────────────────────────────────────────────────────────

class TestHwidReset:
    def render(self, data):
        return probe(probe="hwidReset", env=ENV, data=data, uid="1000")

    def test_free_reset_counts_down(self):
        r = self.render({"ok": True, "mode": "free", "hwid_free_resets": 3,
                         "hwid_resets_used": 1})
        assert "free reset" in r["content"]
        assert "**2**" in r["content"]
        assert r["components"] == []

    def test_deduct_says_one_day_and_shows_the_new_expiry(self):
        r = self.render({"ok": True, "mode": "deduct", "deduct_days": 1,
                         "expiry": 1800000000})
        assert "1 day deducted" in r["content"]
        assert "**1 day** was deducted" in r["content"]
        assert "<t:1800000000:D>" in r["content"]

    def test_confirm_step_offers_deduct_and_cancel_only(self):
        r = self.render({"ok": False, "error": "payment_required", "deduct_days": 1,
                         "price_url": "https://old.example/l/reset"})
        labels = [b["label"] for b in buttons(r)]
        assert labels == ["Deduct 1 day and reset", "Cancel"]
        assert all(b.get("url") is None for b in buttons(r))
        assert "used all 3 free PC resets" in r["content"]
        assert "1 day off your subscription" in r["content"]

    def test_the_confirm_button_is_bound_to_the_invoker(self):
        r = self.render({"ok": False, "error": "payment_required", "deduct_days": 1})
        assert buttons(r)[0]["custom_id"] == "hwid_deduct:1000"

    def test_a_trial_is_refused_with_a_reason_not_a_button(self):
        r = self.render({"ok": False, "error": "trial_no_deduct"})
        assert r["components"] == []
        assert "trial" in r["content"].lower()
        assert "subscribe" in r["content"].lower()

    @pytest.mark.parametrize("data", [
        {"ok": False, "error": "payment_required", "deduct_days": 1},
        {"ok": False, "error": "trial_no_deduct"},
        {"ok": False, "error": "insufficient_time", "deduct_days": 1},
        {"ok": False, "error": "paid_only"},
        {"ok": False, "error": "cooldown", "retry_at": 1800000000},
        {"ok": False, "error": "locked"},
    ])
    def test_no_reply_ever_sells_a_reset(self, data):
        """Even when an older Lambda in front of this Worker still sends a
        price_url, nothing may render a store link."""
        r = self.render(dict(data, price_url="https://old.example/l/reset"))
        blob = json.dumps(r).lower()
        assert "buy a reset" not in blob
        assert "old.example" not in blob

    def test_penalty_days_from_an_older_lambda_still_renders(self):
        r = self.render({"ok": False, "error": "payment_required", "penalty_days": 2})
        assert "2 days off your subscription" in r["content"]
        assert buttons(r)[0]["label"] == "Deduct 2 days and reset"

    def test_deduct_days_defaults_to_one_when_the_server_sends_neither(self):
        assert probe(probe="constants", env=ENV, data={})["deductDays"] == 1


# ── module surface ────────────────────────────────────────────────────────────

class TestWorkerSurface:
    def test_the_tier_table_is_gone_from_the_source(self):
        src = open(WORKER, encoding="utf-8").read()
        assert "{{PRICE_" not in src, "price templates must not survive"
        assert "const TIERS" not in src
        assert "GUMROAD_HWID_RESET_SLUG" not in src, \
            "the paid-reset slug is retired; /hwid_reset links no store"

    def test_exports_and_command_list(self):
        c = probe(probe="constants", env=ENV)
        assert "TIERS" not in c["exports"] and "buyButtons" not in c["exports"]
        assert {"storeUrl", "deductDays", "cmdPurchase"} <= set(c["exports"])
        assert c["commandNames"] == ["purchase", "claim_trial", "hwid_reset",
                                     "deliver", "keygen", "lookup"]
