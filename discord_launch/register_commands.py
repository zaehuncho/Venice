"""Register the Worker's slash commands with Discord (guild-scoped, instant).

The Cloudflare Worker (orion_worker.js) only HANDLES interactions; Discord
learns the command names/options from a one-time registration. The live Lambda
has no /api/discord/register-commands route (LAMBDA_RECONCILIATION.md), so this
script PUTs discord_commands.json straight to Discord's REST API.

Re-run whenever discord_commands.json changes (new options such as /deliver
`reason` are NOT picked up otherwise). Bulk-overwrite semantics: commands not in
the JSON are removed from the guild. Do NOT run this while the gateway bot
(orion_bot.py) is the deployed front-end — it syncs its own tree on boot and the
two would fight.

`default_member_permissions: "0"` on /deliver and /keygen is Discord-side
VISIBILITY, not authorization: it hides the command from ordinary members until
the owner grants the Staff role access in Server Settings → Integrations →
<app> → Command Permissions. AFTER registering, do that, or staff who are not
Discord administrators will not see the commands. The real gate is the license
server's orion-staff check on `actor_discord_id` (contract §1/§4); /lookup is
deliberately visible to everyone because it answers "your own license" with no
options.

Env (never hard-code):
  DISCORD_BOT_TOKEN   bot token (same as SSM /orion/discord_bot_token)
  DISCORD_APP_ID      application id
  ORION_GUILD_ID      server id

Usage:  python register_commands.py [--dry-run]
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COMMANDS_PATH = os.path.join(HERE, "discord_commands.json")
API = "https://discord.com/api/v10"


def load_commands(path=COMMANDS_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cmds = json.load(f)
    names = [c["name"] for c in cmds]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate command names in {path}: {names}")
    for c in cmds:
        required_seen_optional = False
        for o in c.get("options", []):
            # Discord rejects a required option after an optional one.
            if not o.get("required"):
                required_seen_optional = True
            elif required_seen_optional:
                raise ValueError(f"{c['name']}: required option {o['name']!r} follows an optional one")
    return cmds


def register(cmds, token, app_id, guild_id):
    url = f"{API}/applications/{app_id}/guilds/{guild_id}/commands"
    req = urllib.request.Request(url, data=json.dumps(cmds).encode(), method="PUT")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main(argv):
    dry = "--dry-run" in argv
    cmds = load_commands()
    print(f"{len(cmds)} command(s): " + ", ".join(c["name"] for c in cmds))
    if dry:
        print(json.dumps(cmds, indent=2))
        return 0
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    app_id = os.environ.get("DISCORD_APP_ID", "").strip()
    guild_id = os.environ.get("ORION_GUILD_ID", "").strip()
    missing = [n for n, v in (("DISCORD_BOT_TOKEN", token), ("DISCORD_APP_ID", app_id), ("ORION_GUILD_ID", guild_id)) if not v]
    if missing:
        print("missing env: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        out = register(cmds, token, app_id, guild_id)
    except urllib.error.HTTPError as e:
        print(f"Discord HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        return 1
    print("registered: " + ", ".join(c.get("name", "?") for c in out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
