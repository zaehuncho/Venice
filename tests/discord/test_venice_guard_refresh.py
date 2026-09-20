"""Focused, offline contracts for the refreshed Discord moderation gateway."""
from __future__ import annotations

import asyncio
import importlib.util
import ast
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[2]
for key, value in {
    "DISCORD_BOT_TOKEN": "fixture-token",
    "VENICE_GUILD_ID": "1",
    "VERIFIED_ROLE_ID": "2",
    "STAFF_ROLE_ID": "3",
    "VERIFY_CHANNEL_ID": "4",
    "CREATE_TICKET_CHANNEL_ID": "5",
    "TICKET_CATEGORY_ID": "6",
    "MOD_LOG_CHANNEL_ID": "7",
    "TICKET_LOG_CHANNEL_ID": "8",
    "TRUSTED_ACTOR_IDS": "9,10",
    "APPROVED_BOT_IDS": "11",
}.items():
    os.environ[key] = value

spec = importlib.util.spec_from_file_location("venice_guard_refresh", ROOT / "discord_launch" / "venice_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_obfuscated_invite_and_phishing_filter():
    assert "invite" in guard.suspicious_link_reason("discord.\u200bgg/sample").lower()
    assert guard.suspicious_link_reason("https://bit.ly/example") == "URL shortener"
    assert guard.suspicious_link_reason("https://discord-gift.example/path") == "known phishing pattern"
    assert guard.suspicious_link_reason("https://zaeorion.com/#pricing") is None


def test_mass_mentions_attachments_and_repeat_window():
    base = dict(mention_everyone=False, mentions=[], role_mentions=[], attachments=[], content="hello")
    assert guard.message_abuse_reason(SimpleNamespace(**{**base, "mention_everyone": True})) == "mass mentions"
    assert guard.message_abuse_reason(SimpleNamespace(**{**base, "mentions": [1, 2, 3, 4, 5]})) == "mass mentions"
    assert guard.message_abuse_reason(SimpleNamespace(**{**base, "attachments": [1, 2, 3, 4, 5]})) == "attachment flood"
    guard.message_windows.clear()
    guard.duplicate_windows.clear()
    for n in range(3):
        assert guard.record_spam(42, "s\u200bame", float(n)) is None
    assert guard.record_spam(42, "same", 3.0) == "repeated message spam"


def test_moderation_commands_and_no_admin_permission_dependency():
    names = {command.name for command in guard.tree.get_commands(guild=guard.GUILD)}
    assert {"guard_setup", "security_status", "lockdown", "unlock",
            "guard_quarantine", "guard_release", "guard_purge"} <= names
    assert guard.intents.members and guard.intents.message_content and guard.intents.moderation
    source = (ROOT / "discord_launch" / "venice_guard.py").read_text(encoding="utf-8")
    assert "permissions.administrator" not in source


def test_triton_storefront_copy_is_current():
    source = (ROOT / "discord_launch" / "orion_bot.py").read_text(encoding="utf-8")
    assert 'STORE_URL        = "https://zaeorion.com/#pricing"' in source
    assert "The Venice website links your purchase" in source
    assert "3 PC resets are free" in source
    assert "Tiers differ only in duration" not in source


def test_approved_bot_join_is_separate_from_audit_trust(monkeypatch):
    assert 11 in guard.APPROVED_BOT_IDS
    assert not guard.is_trusted(11)
    guard.join_window.clear()
    monkeypatch.setattr(guard, "log_security", AsyncMock())
    allowed = SimpleNamespace(guild=SimpleNamespace(id=1), id=11, bot=True,
                              kick=AsyncMock())
    blocked = SimpleNamespace(guild=SimpleNamespace(id=1), id=12, bot=True,
                              kick=AsyncMock())
    asyncio.run(guard.on_member_join(allowed))
    allowed.kick.assert_not_awaited()
    asyncio.run(guard.on_member_join(blocked))
    blocked.kick.assert_awaited_once()


def test_status_prefers_discord_id_and_fail_closed_legacy_lookup():
    source = (ROOT / "discord_launch" / "orion_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    status = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "status")
    assert [argument.arg for argument in status.args.args] == ["interaction", "key"]
    body = ast.get_source_segment(source, status)
    assert "lambda_post(ROUTE_STATUS" in body
    assert '"discord_id": uid, "actor_discord_id": uid' in body
    assert "if st == 404" in body
    assert "str(license_row.get(\"discord_user_id\") or \"\") != str(uid)" in body
    assert "staff_post(ROUTE_STAFF_LIC" in body
    assert "No matching membership belongs to your Discord account" in body
