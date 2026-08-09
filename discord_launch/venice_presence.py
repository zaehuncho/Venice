"""Venice — presence-only gateway client.

WHY THIS EXISTS
The Cloudflare Worker (orion_worker.js) serves every slash command over HTTP
interactions. That path is effectively 100% available, but it cannot ever show
the green "Online" dot: presence is a property of a live gateway (WebSocket)
connection, and HTTP interactions have none. This process holds that connection
and does nothing else.

WHY IT CANNOT FIGHT THE WORKER
Once an Interactions Endpoint URL is set on the app, Discord delivers
INTERACTION_CREATE over HTTP *only* — it stops sending them down the gateway.
So this client cannot receive a command even if it wanted to. It registers no
commands and adds no handlers. That is the whole point: the earlier warning in
BOT_DEPLOY_GUIDE.md about the gateway bot "fighting" the Worker applies to
orion_bot.py, which registers a command tree; it does not apply here.

Deliberately NOT included: any license logic, any DM sending, any role grants.
Those live in the Lambda (see handle_bot_trial / the Gumroad webhook), so this
process needs no secrets beyond the bot token and can be restarted at any time
without touching a customer.

Env:
  DISCORD_BOT_TOKEN   required — same token as SSM /orion/discord_bot_token
  VENICE_STATUS       optional activity text (default: "/purchase")

Run:  pip install -U "discord.py>=2.3"   then   python venice_presence.py
"""
import logging
import os
import sys

import discord

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
STATUS_TEXT = os.environ.get("VENICE_STATUS", "/purchase").strip() or "/purchase"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,          # container/systemd log capture
)
log = logging.getLogger("venice.presence")

if not TOKEN:
    log.error("DISCORD_BOT_TOKEN is unset — nothing to connect with.")
    raise SystemExit(2)

# No privileged intents. This client reads nothing and sends nothing; it only
# needs a session. Requesting more would fail the app's intent checks for no gain.
intents = discord.Intents.none()
client = discord.Client(
    intents=intents,
    activity=discord.Game(name=STATUS_TEXT),
    status=discord.Status.online,
)


@client.event
async def on_ready():
    log.info("online as %s (%s) — presence held, %d guild(s)",
             client.user, client.user.id, len(client.guilds))


@client.event
async def on_resumed():
    # Gateway resumes are normal and frequent; log them so a restart loop is
    # distinguishable from healthy reconnection when reading host logs.
    log.info("gateway session resumed")


@client.event
async def on_disconnect():
    log.warning("gateway disconnected — discord.py will reconnect")


if __name__ == "__main__":
    # reconnect=True is discord.py's default and handles the ordinary cases
    # (resume, 1000-class closes, network blips) with exponential backoff. A
    # raise past it means the token is bad or the app was deleted — let the
    # process exit non-zero so the host's restart policy escalates visibly
    # instead of us swallowing it into a silent retry loop.
    try:
        client.run(TOKEN, reconnect=True, log_handler=None)
    except discord.LoginFailure:
        log.error("login failed — DISCORD_BOT_TOKEN is invalid or was reset")
        raise SystemExit(3)
