from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_all_lambda_discord_requests_send_a_discord_user_agent():
    """Discord's edge rejects urllib's default Python signature with HTTP 403.

    Pin a Discord-compliant User-Agent on every direct REST request from both
    Lambdas so role grants and private key delivery work from AWS.
    """
    webhook = (ROOT / "discord_launch/gumroad_webhook/lambda_function.py").read_text(encoding="utf-8")
    backend = (ROOT / "backend/lambda_function.py").read_text(encoding="utf-8")
    assert webhook.count('add_header("User-Agent", DISCORD_USER_AGENT)') == 3
    # DM channel open, DM send, role add, role remove, guild member PUT/GET (website gate).
    assert backend.count('add_header("User-Agent", "DiscordBot (https://zaeorion.com, 1.0)")') == 5
    assert "role_id = ssm_get(role_ssm, decrypt=True)" in backend
    assert "guild_id = ssm_get(DISCORD_GUILD_ID_SSM, decrypt=True)" in backend
