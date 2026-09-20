"""No guild-wide privilege in Nereus channel overwrite selection."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "nereus_deploy", ROOT / ".codex_artifacts/nereus-announcements-20260916/deploy_nereus.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def test_zero_permission_install_uses_member_overwrite():
    roles = [{"id": deploy.GUILD, "permissions": "0"}]
    assert deploy.overwrite_target("123456789012345678", {"roles": []}, roles) == (
        "123456789012345678", 1)


def test_managed_role_install_uses_role_overwrite():
    roles = [{"id": "222", "permissions": "0", "tags": {"bot_id": "123456789012345678"}}]
    assert deploy.overwrite_target("123456789012345678", {"roles": ["222"]}, roles) == (
        "222", 0)


def test_elevated_everyone_or_unexpected_role_fails_closed():
    with pytest.raises(RuntimeError, match="elevated"):
        deploy.overwrite_target("123456789012345678", {"roles": []}, [
            {"id": deploy.GUILD, "permissions": str(1 << 3)}])
    with pytest.raises(RuntimeError, match="Unexpected"):
        deploy.overwrite_target("123456789012345678", {"roles": ["333"]}, [
            {"id": deploy.GUILD, "permissions": "0"},
            {"id": "333", "permissions": "0"}])
