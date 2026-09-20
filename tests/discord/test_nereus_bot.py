"""Offline security and publishing contracts for the Nereus announcement bot."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[2]
for key, value in {
    "DISCORD_BOT_TOKEN": "fixture-token",
    "VENICE_GUILD_ID": "100",
    "ADMIN_ROLE_ID": "101",
    "ANNOUNCEMENTS_CHANNEL_ID": "103",
    "AUDIT_LOG_CHANNEL_ID": "104",
}.items():
    os.environ[key] = value

spec = importlib.util.spec_from_file_location("nereus", ROOT / "discord_launch" / "nereus_bot.py")
nereus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nereus)


def test_copy_is_bounded_and_links_are_fixed():
    assert nereus.clean_copy("  Maintenance  ", " Scheduled work tonight. ") == (
        "Maintenance", "Scheduled work tonight.")
    with pytest.raises(ValueError):
        nereus.clean_copy("Update", "Visit https://example.com for details")
    with pytest.raises(ValueError):
        nereus.clean_copy("Update", "Visit https://exa\u200bmple.com for details")
    assert nereus.SITE_URL == "https://zaeorion.com/"


def test_only_owner_or_admin_can_publish():
    guild = SimpleNamespace(owner_id=1)
    assert nereus.is_publisher(SimpleNamespace(id=1, guild=guild, roles=[]))
    assert nereus.is_publisher(SimpleNamespace(id=2, guild=guild,
                                                roles=[SimpleNamespace(id=101)]))
    assert not nereus.is_publisher(SimpleNamespace(id=3, guild=guild,
                                                    roles=[SimpleNamespace(id=105)]))


def test_command_has_privileged_default_and_no_member_intents():
    command = next(c for c in nereus.tree.get_commands(guild=nereus.GUILD) if c.name == "announce")
    assert command.default_permissions.manage_guild
    assert not nereus.intents.members and not nereus.intents.message_content
    source = (ROOT / "discord_launch" / "nereus_bot.py").read_text(encoding="utf-8")
    assert "permissions.administrator" not in source
    assert "allowed_mentions=discord.AllowedMentions.none()" in source


def test_audit_precedes_public_post(monkeypatch):
    order = []
    record = SimpleNamespace(content="audit", edit=AsyncMock())

    async def audit_send(*args, **kwargs):
        order.append("audit")
        return record

    async def public_send(*args, **kwargs):
        order.append("public")
        return SimpleNamespace(id=900, jump_url="https://discord.com/channels/100/103/900")

    public = SimpleNamespace(guild=SimpleNamespace(id=100), id=103, send=public_send)
    audit = SimpleNamespace(guild=SimpleNamespace(id=100), id=104, send=audit_send)

    async def fetch(channel_id):
        return public if channel_id == 103 else audit

    monkeypatch.setattr(nereus, "fetch_text_channel", fetch)
    posted = asyncio.run(nereus.publish(SimpleNamespace(id=1), "Update", "New feature is ready.", "update"))
    assert posted.id == 900
    assert order == ["audit", "public"]
    assert record.edit.await_count == 1


def test_no_public_post_if_audit_fails(monkeypatch):
    public_send = AsyncMock()
    public = SimpleNamespace(guild=SimpleNamespace(id=100), id=103, send=public_send)
    audit = SimpleNamespace(guild=SimpleNamespace(id=100), id=104,
                            send=AsyncMock(side_effect=RuntimeError("audit offline")))

    async def fetch(channel_id):
        return public if channel_id == 103 else audit

    monkeypatch.setattr(nereus, "fetch_text_channel", fetch)
    with pytest.raises(RuntimeError, match="audit offline"):
        asyncio.run(nereus.publish(SimpleNamespace(id=1), "Update", "New feature is ready.", "update"))
    public_send.assert_not_awaited()
