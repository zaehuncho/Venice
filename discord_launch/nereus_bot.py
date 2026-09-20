"""Nereus — least-privilege Venice announcements gateway.

Nereus has no license, payment, moderation, or member-management access.
It needs only View Channel, Send Messages, Embed Links, and Read Message
History in #announcements and View/Send (not Read History) in #audit-log.
Commands run in #announcements, with an ephemeral preview.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import unicodedata

import discord
from discord import app_commands


TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["VENICE_GUILD_ID"])
ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"])
ANNOUNCEMENTS_CHANNEL_ID = int(os.environ["ANNOUNCEMENTS_CHANNEL_ID"])
AUDIT_LOG_CHANNEL_ID = int(os.environ["AUDIT_LOG_CHANNEL_ID"])

GUILD = discord.Object(id=GUILD_ID)
SITE_URL = "https://zaeorion.com/"
BLUE = 0x2563EB
AMBER = 0xF59E0B
RED = 0xEF4444
COLORS = {"update": BLUE, "maintenance": AMBER, "incident": RED}
LINK_RE = re.compile(r"(?:https?://|www\.|discord\.gg/)", re.I)

intents = discord.Intents.none()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def is_publisher(member: discord.Member) -> bool:
    return member.id == member.guild.owner_id or any(
        role.id == ADMIN_ROLE_ID for role in member.roles
    )


def clean_copy(title: str, body: str) -> tuple[str, str]:
    """Reject user-supplied links; the fixed website button is the only CTA."""
    title = "".join(ch for ch in unicodedata.normalize("NFKC", title).strip()
                    if unicodedata.category(ch) != "Cf")
    body = "".join(ch for ch in unicodedata.normalize("NFKC", body).strip()
                   if unicodedata.category(ch) != "Cf")
    if not (3 <= len(title) <= 120 and 10 <= len(body) <= 1800):
        raise ValueError("Use a title of 3–120 characters and a message of 10–1800 characters.")
    if LINK_RE.search(title) or LINK_RE.search(body):
        raise ValueError("Announcements use the fixed official website button; remove links from the text.")
    return title, body


def announcement_embed(title: str, body: str, kind: str) -> discord.Embed:
    if kind not in COLORS:
        raise ValueError("Unknown announcement type.")
    embed = discord.Embed(title=title, description=body, color=COLORS[kind],
                          timestamp=dt.datetime.now(dt.timezone.utc))
    embed.set_footer(text=f"Venice • {kind.title()} • Official announcement")
    return embed


def fingerprint(title: str, body: str, kind: str) -> str:
    return hashlib.sha256(f"{kind}\n{title}\n{body}".encode("utf-8")).hexdigest()[:16]


def publication_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Venice website", style=discord.ButtonStyle.link,
                                    url=SITE_URL))
    return view


async def fetch_text_channel(channel_id: int) -> discord.TextChannel | None:
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def publish(actor: discord.Member, title: str, body: str, kind: str) -> discord.Message:
    """Write an audit request before public delivery; never publish unlogged."""
    channel = await fetch_text_channel(ANNOUNCEMENTS_CHANNEL_ID)
    audit = await fetch_text_channel(AUDIT_LOG_CHANNEL_ID)
    if channel is None or audit is None or channel.guild.id != GUILD_ID or audit.guild.id != GUILD_ID:
        raise RuntimeError("Announcement or audit channel is unavailable.")
    marker = fingerprint(title, body, kind)
    request = await audit.send(
        f"Nereus publish requested by <@{actor.id}> (`{actor.id}`); "
        f"type={kind}; title={title!r}; SHA256-prefix={marker}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )
    try:
        posted = await channel.send(embed=announcement_embed(title, body, kind),
                                    view=publication_view(),
                                    allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException:
        try:
            await request.edit(content=request.content + " PUBLICATION_FAILED")
        except discord.DiscordException:
            pass
        raise
    try:
        await request.edit(content=request.content + f" published={posted.id} channel={channel.id}")
    except discord.DiscordException:
        # The pre-publication audit record remains even when its final edit fails.
        pass
    return posted


class Preview(discord.ui.View):
    def __init__(self, actor_id: int, title: str, body: str, kind: str):
        super().__init__(timeout=300)
        self.actor_id = actor_id
        self.title = title
        self.body = body
        self.kind = kind
        self.finished = False

    @discord.ui.button(label="Publish announcement", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if (self.finished or interaction.guild_id != GUILD_ID
                or interaction.channel_id != ANNOUNCEMENTS_CHANNEL_ID
                or interaction.user.id != self.actor_id
                or not isinstance(interaction.user, discord.Member)
                or not is_publisher(interaction.user)):
            await interaction.response.send_message("This draft is not available to you.", ephemeral=True)
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        await interaction.response.defer(ephemeral=True)
        try:
            posted = await publish(interaction.user, self.title, self.body, self.kind)
        except (RuntimeError, discord.DiscordException) as exc:
            await interaction.followup.send(f"Publication failed: {type(exc).__name__}.", ephemeral=True)
            self.stop()
            return
        await interaction.followup.send(f"Published in <#{ANNOUNCEMENTS_CHANNEL_ID}>: {posted.jump_url}",
                                        ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This draft is not yours.", ephemeral=True)
            return
        self.finished = True
        await interaction.response.edit_message(content="Announcement cancelled.", embed=None, view=None)
        self.stop()


@client.event
async def on_ready():
    synced = await tree.sync(guild=GUILD)
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="Venice | announcements"))
    print(f"Nereus online as {client.user} ({client.user.id}); {len(synced)} commands synced", flush=True)


@tree.command(name="announce", description="ADMIN: preview and publish a Venice announcement", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(title="Announcement headline", body="Message text (no links)",
                       kind="Announcement type")
@app_commands.choices(kind=[
    app_commands.Choice(name="Update", value="update"),
    app_commands.Choice(name="Maintenance", value="maintenance"),
    app_commands.Choice(name="Incident", value="incident"),
])
async def announce(interaction: discord.Interaction, title: str, body: str, kind: str = "update"):
    if (interaction.guild_id != GUILD_ID or interaction.channel_id != ANNOUNCEMENTS_CHANNEL_ID
            or not isinstance(interaction.user, discord.Member)
            or not is_publisher(interaction.user)):
        await interaction.response.send_message("Use this command as Admin in #announcements.", ephemeral=True)
        return
    try:
        title, body = clean_copy(title, body)
        preview = announcement_embed(title, body, kind)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.send_message(
        "Preview only — nothing is public until you press Publish.",
        embed=preview, view=Preview(interaction.user.id, title, body, kind), ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN, reconnect=True, log_handler=None)
