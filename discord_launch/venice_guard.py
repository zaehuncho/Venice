"""Venice Guard — verification, anti-abuse, anti-nuke, and private tickets.

This bot intentionally has no Administrator permission and has no access to
Venice licence keys or the Triton purchase backend.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import io
import os
import random
import re
import unicodedata
from urllib.parse import urlsplit

import discord
from discord import app_commands


TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["VENICE_GUILD_ID"])
VERIFIED_ROLE_ID = int(os.environ["VERIFIED_ROLE_ID"])
STAFF_ROLE_ID = int(os.environ["STAFF_ROLE_ID"])
VERIFY_CHANNEL_ID = int(os.environ["VERIFY_CHANNEL_ID"])
CREATE_TICKET_CHANNEL_ID = int(os.environ["CREATE_TICKET_CHANNEL_ID"])
TICKET_CATEGORY_ID = int(os.environ["TICKET_CATEGORY_ID"])
MOD_LOG_CHANNEL_ID = int(os.environ["MOD_LOG_CHANNEL_ID"])
TICKET_LOG_CHANNEL_ID = int(os.environ["TICKET_LOG_CHANNEL_ID"])
TRUSTED_ACTOR_IDS = {
    int(value) for value in os.environ.get("TRUSTED_ACTOR_IDS", "").split(",")
    if value.strip().isdigit()
}
APPROVED_BOT_IDS = {
    int(value) for value in os.environ.get("APPROVED_BOT_IDS", "").split(",")
    if value.strip().isdigit()
}

BLUE = 0x2563EB
DANGER = 0xEF4444
GUILD = discord.Object(id=GUILD_ID)
MIN_ACCOUNT_AGE_DAYS = int(os.environ.get("MIN_ACCOUNT_AGE_DAYS", "7"))
JOIN_THRESHOLD = int(os.environ.get("JOIN_THRESHOLD", "8"))
JOIN_WINDOW_SECONDS = int(os.environ.get("JOIN_WINDOW_SECONDS", "20"))
SPAM_THRESHOLD = int(os.environ.get("SPAM_THRESHOLD", "6"))
SPAM_WINDOW_SECONDS = int(os.environ.get("SPAM_WINDOW_SECONDS", "8"))

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+",
    re.I,
)
SUSPICIOUS_HOST_FRAGMENTS = {
    "grabify", "iplogger", "ip-grabber", "discord-gift", "discordnitro",
    "steamcomrnunity", "dlscord", "disc0rd", "token-grabber",
}
SHORTENER_HOSTS = {"bit.ly", "tinyurl.com", "cutt.ly", "rb.gy", "t.co"}


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.moderation = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

message_windows: dict[int, collections.deque[float]] = collections.defaultdict(collections.deque)
duplicate_windows: dict[int, collections.deque[tuple[float, str]]] = collections.defaultdict(collections.deque)
join_window: collections.deque[float] = collections.deque()
action_windows: dict[int, collections.deque[float]] = collections.defaultdict(collections.deque)
security_state = {"raid_mode": False, "raid_until": None, "last_incident": None}
ticket_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
moderation_strikes: dict[int, collections.deque[float]] = collections.defaultdict(collections.deque)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def is_staff(member: discord.Member) -> bool:
    return (member.guild.owner_id == member.id
            or any(role.id == STAFF_ROLE_ID for role in member.roles)
            or member.guild_permissions.manage_messages)


def is_trusted(actor_id: int) -> bool:
    return actor_id in TRUSTED_ACTOR_IDS or actor_id == getattr(client.user, "id", 0)


def blue_embed(title: str, description: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=BLUE, timestamp=utcnow())
    embed.set_footer(text="Venice Guard • Security & Support")
    return embed


async def channel_by_id(channel_id: int):
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException:
            return None
    return channel


async def log_security(title: str, description: str, *, color: int = DANGER):
    channel = await channel_by_id(MOD_LOG_CHANNEL_ID)
    if channel:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=utcnow())
        embed.set_footer(text="Venice Guard • Immutable Discord audit context")
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    security_state["last_incident"] = f"{title}: {description}"[:500]


def normalized_content(content: str) -> str:
    """Fold common invite/link obfuscation without changing displayed messages."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKC", content)
        if unicodedata.category(ch) != "Cf"
    )


def suspicious_link_reason(content: str) -> str | None:
    content = normalized_content(content)
    if INVITE_RE.search(content):
        return "Discord invite links are restricted to staff"
    for raw in URL_RE.findall(content):
        try:
            host = (urlsplit(raw.rstrip(".,!?)]}")).hostname or "").lower()
        except ValueError:
            return "malformed link"
        if host.startswith("xn--") or ".xn--" in host:
            return "punycode link"
        if host in SHORTENER_HOSTS:
            return "URL shortener"
        compact_host = host.replace("-", "")
        if any(fragment.replace("-", "") in compact_host for fragment in SUSPICIOUS_HOST_FRAGMENTS):
            return "known phishing pattern"
    return None


def record_spam(user_id: int, content: str, now: float) -> str | None:
    times = message_windows[user_id]
    times.append(now)
    while times and now - times[0] > SPAM_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= SPAM_THRESHOLD:
        return f"{len(times)} messages in {SPAM_WINDOW_SECONDS}s"

    normalized = " ".join(normalized_content(content).lower().split())[:300]
    if normalized:
        repeats = duplicate_windows[user_id]
        repeats.append((now, normalized))
        while repeats and now - repeats[0][0] > 30:
            repeats.popleft()
        if sum(1 for _, text in repeats if text == normalized) >= 4:
            return "repeated message spam"
    return None


def message_abuse_reason(message: discord.Message) -> str | None:
    if message.mention_everyone or len(message.mentions) + len(message.role_mentions) >= 5:
        return "mass mentions"
    if len(message.attachments) >= 5:
        return "attachment flood"
    if len(normalized_content(message.content)) > 3500:
        return "oversized message flood"
    return suspicious_link_reason(message.content)


async def moderate_message(message: discord.Message, reason: str):
    try:
        await message.delete()
    except discord.DiscordException:
        pass
    member = message.author
    now = asyncio.get_running_loop().time()
    strikes = moderation_strikes[member.id]
    strikes.append(now)
    while strikes and now - strikes[0] > 3600:
        strikes.popleft()
    minutes = 10 if len(strikes) == 1 else 60 if len(strikes) == 2 else 1440
    try:
        await member.timeout(dt.timedelta(minutes=minutes), reason=f"Venice Guard: {reason}")
    except discord.DiscordException:
        pass
    await log_security(
        "Automod action",
        f"**Member:** {member.mention} (`{member.id}`)\n"
        f"**Channel:** {message.channel.mention}\n**Reason:** {reason}\n"
        f"**Timeout:** {minutes} minutes (strike {len(strikes)} in one hour)",
    )


class VerifyModal(discord.ui.Modal, title="Venice Verification"):
    answer = discord.ui.TextInput(
        label="Answer the security question",
        placeholder="Enter the number only",
        min_length=1,
        max_length=4,
    )

    def __init__(self):
        super().__init__(timeout=180)
        left, right = random.randint(2, 12), random.randint(2, 12)
        self.expected = left + right
        self.answer.label = f"What is {left} + {right}?"

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild_id != GUILD_ID or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This gate only works inside Venice.", ephemeral=True)
            return
        member = interaction.user
        verified = interaction.guild.get_role(VERIFIED_ROLE_ID)
        if verified is None:
            await interaction.response.send_message("Verification is temporarily unavailable.", ephemeral=True)
            await log_security("Verification misconfiguration", "Verified role was not found.")
            return
        age = utcnow() - member.created_at
        if age < dt.timedelta(days=MIN_ACCOUNT_AGE_DAYS):
            await interaction.response.send_message(
                f"Your Discord account must be at least {MIN_ACCOUNT_AGE_DAYS} days old.", ephemeral=True)
            await log_security("Young account blocked", f"{member.mention} (`{member.id}`), age {age.days}d")
            return
        if str(self.answer.value).strip() != str(self.expected):
            await interaction.response.send_message("Incorrect answer. Press Verify and try again.", ephemeral=True)
            await log_security("Verification failed", f"{member.mention} (`{member.id}`) answered incorrectly.", color=0xF59E0B)
            return
        if verified not in member.roles:
            await member.add_roles(verified, reason="Passed Venice Guard verification")
        await interaction.response.send_message("Verified. Welcome to Venice.", ephemeral=True)
        await log_security("Member verified", f"{member.mention} (`{member.id}`) received {verified.mention}.", color=0x22C55E)


class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", emoji="✅", style=discord.ButtonStyle.primary,
                       custom_id="venice_guard:verify")
    async def verify(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if security_state["raid_mode"]:
            until = security_state.get("raid_until")
            if until and utcnow() >= until:
                security_state["raid_mode"] = False
            else:
                await interaction.response.send_message(
                    "Verification is briefly paused while Venice Guard checks a join spike.", ephemeral=True)
                return
        await interaction.response.send_modal(VerifyModal())


async def find_open_ticket(guild: discord.Guild, member_id: int):
    marker = f"venice-ticket owner={member_id} "
    return next((channel for channel in guild.text_channels if (channel.topic or "").startswith(marker)), None)


def ticket_overwrites(guild: discord.Guild, member: discord.Member):
    staff = guild.get_role(STAFF_ROLE_ID)
    guard = guild.me
    allow = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True,
        attach_files=True, embed_links=True,
    )
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: allow,
    }
    if staff:
        overwrites[staff] = allow
    if guard:
        overwrites[guard] = allow
    return overwrites


async def open_ticket(interaction: discord.Interaction, kind: str):
    if interaction.guild_id != GUILD_ID or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Tickets only work inside Venice.", ephemeral=True)
        return
    async with ticket_locks[interaction.user.id]:
        existing = await find_open_ticket(interaction.guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
            return
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category is not configured.", ephemeral=True)
            await log_security("Ticket misconfiguration", "Configured ticket category was not found.")
            return
        safe_name = re.sub(r"[^a-z0-9-]", "-", interaction.user.name.lower()).strip("-")[:32] or "member"
        channel = await interaction.guild.create_text_channel(
            f"{kind}-{safe_name}",
            category=category,
            topic=f"venice-ticket owner={interaction.user.id} type={kind}",
            overwrites=ticket_overwrites(interaction.guild, interaction.user),
            reason=f"Venice Guard ticket: {kind}",
        )
        embed = blue_embed(
            f"{kind.replace('-', ' ').title()} Ticket",
            f"Welcome {interaction.user.mention}. Describe the issue and include screenshots where useful.\n\n"
            "Never paste a Venice licence key, password, payment card, or authentication code.",
        )
        staff = interaction.guild.get_role(STAFF_ROLE_ID)
        await channel.send(
            content=staff.mention if staff else None,
            embed=embed,
            view=TicketControls(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
        await log_security("Ticket opened", f"{interaction.user.mention} opened {channel.mention} ({kind}).", color=BLUE)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Support", emoji="🛠️", style=discord.ButtonStyle.primary,
                       custom_id="venice_guard:ticket:support")
    async def support(self, interaction, _button):
        await open_ticket(interaction, "support")

    @discord.ui.button(label="Purchase Help", emoji="🛒", style=discord.ButtonStyle.success,
                       custom_id="venice_guard:ticket:purchase")
    async def purchase(self, interaction, _button):
        await open_ticket(interaction, "purchase-help")

    @discord.ui.button(label="Report", emoji="⚠️", style=discord.ButtonStyle.danger,
                       custom_id="venice_guard:ticket:report")
    async def report(self, interaction, _button):
        await open_ticket(interaction, "report")


async def build_transcript(channel: discord.TextChannel) -> discord.File:
    lines = [f"Venice ticket transcript: #{channel.name}", f"Channel ID: {channel.id}", ""]
    async for message in channel.history(limit=1000, oldest_first=True):
        stamp = message.created_at.isoformat()
        content = message.clean_content.replace("\r", " ").replace("\n", " ")
        lines.append(f"[{stamp}] {message.author} ({message.author.id}): {content}")
        for attachment in message.attachments:
            lines.append(f"  attachment: {attachment.url}")
    data = "\n".join(lines).encode("utf-8", errors="replace")
    return discord.File(io.BytesIO(data), filename=f"{channel.name}-{channel.id}.txt")


async def close_ticket(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or not (channel.topic or "").startswith("venice-ticket "):
        await interaction.response.send_message("This is not a Venice Guard ticket.", ephemeral=True)
        return
    owner_match = re.search(r"owner=(\d+)", channel.topic or "")
    owner_id = int(owner_match.group(1)) if owner_match else 0
    if interaction.user.id != owner_id and not is_staff(interaction.user):
        await interaction.response.send_message("Only the ticket owner or Staff can close this ticket.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    transcript = await build_transcript(channel)
    log_channel = await channel_by_id(TICKET_LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("Ticket log channel is unavailable; the ticket was left open.", ephemeral=True)
        return
    embed = blue_embed(
        "Ticket closed",
        f"**Channel:** `{channel.name}` (`{channel.id}`)\n"
        f"**Owner:** <@{owner_id}> (`{owner_id}`)\n"
        f"**Closed by:** {interaction.user.mention} (`{interaction.user.id}`)",
    )
    await log_channel.send(embed=embed, file=transcript, allowed_mentions=discord.AllowedMentions.none())
    await interaction.followup.send("Transcript saved. Closing in 3 seconds.", ephemeral=True)
    await asyncio.sleep(3)
    await channel.delete(reason=f"Venice Guard ticket closed by {interaction.user.id}")


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="venice_guard:ticket:close")
    async def close(self, interaction, _button):
        await close_ticket(interaction)


@client.event
async def on_ready():
    TRUSTED_ACTOR_IDS.add(client.user.id)
    client.add_view(VerificationView())
    client.add_view(TicketPanelView())
    client.add_view(TicketControls())
    synced = await tree.sync(guild=GUILD)
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="Venice | safety + tickets"))
    print(f"Venice Guard online as {client.user} ({client.user.id}); {len(synced)} commands synced", flush=True)


@client.event
async def on_message(message: discord.Message):
    if message.guild is None or message.guild.id != GUILD_ID or message.author.bot:
        return
    if not isinstance(message.author, discord.Member) or is_staff(message.author):
        return
    reason = message_abuse_reason(message)
    if reason is None:
        reason = record_spam(message.author.id, message.content, asyncio.get_running_loop().time())
    if reason:
        await moderate_message(message, reason)


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if (before.content == after.content or after.guild is None or after.guild.id != GUILD_ID
            or after.author.bot or not isinstance(after.author, discord.Member)
            or is_staff(after.author)):
        return
    reason = message_abuse_reason(after)
    if reason:
        await moderate_message(after, f"edited message: {reason}")


@client.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return
    if member.bot and member.id not in APPROVED_BOT_IDS and not is_trusted(member.id):
        try:
            await member.kick(reason="Venice Guard: unapproved bot account")
            await log_security("Unapproved bot removed", f"Bot `{member.id}` joined without an allowlist entry.")
        except discord.DiscordException as exc:
            await log_security("Unapproved bot alert", f"Bot `{member.id}` could not be removed: `{type(exc).__name__}`")
        return
    now = asyncio.get_running_loop().time()
    join_window.append(now)
    while join_window and now - join_window[0] > JOIN_WINDOW_SECONDS:
        join_window.popleft()
    if len(join_window) >= JOIN_THRESHOLD:
        security_state["raid_mode"] = True
        security_state["raid_until"] = utcnow() + dt.timedelta(minutes=5)
        try:
            await member.timeout(dt.timedelta(minutes=10), reason="Venice Guard join-spike quarantine")
        except discord.DiscordException:
            pass
        await log_security(
            "Join spike detected",
            f"{len(join_window)} joins in {JOIN_WINDOW_SECONDS}s. Verification paused for 5 minutes; "
            f"newest member {member.mention} quarantined.",
        )


async def contain_actor(guild: discord.Guild, actor_id: int, action: str):
    if is_trusted(actor_id) or actor_id == guild.owner_id:
        return
    member = guild.get_member(actor_id)
    if member is None:
        await log_security("Anti-nuke alert", f"Actor `{actor_id}` exceeded the `{action}` threshold but is not a member.")
        return
    dangerous = []
    for role in member.roles:
        p = role.permissions
        if role < guild.me.top_role and (p.administrator or p.manage_guild or p.manage_roles or p.manage_channels or p.manage_webhooks or p.ban_members):
            dangerous.append(role)
    if dangerous:
        try:
            await member.remove_roles(*dangerous, reason=f"Venice Guard anti-nuke: {action}")
        except discord.DiscordException:
            pass
    try:
        await member.timeout(dt.timedelta(hours=1), reason=f"Venice Guard anti-nuke: {action}")
    except discord.DiscordException:
        pass
    await log_security(
        "Anti-nuke containment",
        f"**Actor:** {member.mention} (`{member.id}`)\n**Action burst:** {action}\n"
        f"**Removed manageable dangerous roles:** {', '.join(r.name for r in dangerous) or 'none'}\n"
        "**Timeout:** attempted for 1 hour",
    )


async def inspect_audit(guild: discord.Guild, action: discord.AuditLogAction, target_id: int | None,
                        label: str, threshold: int, channel_id: int | None = None):
    await asyncio.sleep(1.0)
    try:
        async for entry in guild.audit_logs(limit=6, action=action):
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            if channel_id is not None and getattr(getattr(entry, "extra", None), "channel", None) != channel_id:
                extra_channel = getattr(getattr(entry, "extra", None), "channel", None)
                if getattr(extra_channel, "id", None) != channel_id:
                    continue
            if (utcnow() - entry.created_at).total_seconds() > 15:
                return
            actor_id = entry.user.id
            if is_trusted(actor_id) or actor_id == guild.owner_id:
                return
            now = asyncio.get_running_loop().time()
            window = action_windows[actor_id]
            window.append(now)
            while window and now - window[0] > 15:
                window.popleft()
            if len(window) >= threshold:
                await contain_actor(guild, actor_id, label)
                window.clear()
            return
    except discord.DiscordException as exc:
        await log_security("Audit inspection failed", f"`{label}` target `{target_id}`: `{type(exc).__name__}`")


@client.event
async def on_guild_channel_create(channel):
    await inspect_audit(channel.guild, discord.AuditLogAction.channel_create, channel.id, "channel-create", 5)


@client.event
async def on_guild_channel_delete(channel):
    await inspect_audit(channel.guild, discord.AuditLogAction.channel_delete, channel.id, "channel-delete", 3)


@client.event
async def on_guild_role_create(role):
    await inspect_audit(role.guild, discord.AuditLogAction.role_create, role.id, "role-create", 5)


@client.event
async def on_guild_role_delete(role):
    await inspect_audit(role.guild, discord.AuditLogAction.role_delete, role.id, "role-delete", 3)


@client.event
async def on_guild_role_update(before, after):
    if before.permissions != after.permissions:
        elevated = after.permissions.value & ~before.permissions.value
        dangerous = discord.Permissions(
            administrator=True, manage_guild=True, manage_roles=True,
            manage_channels=True, manage_webhooks=True, ban_members=True,
        ).value
        threshold = 1 if elevated & dangerous else 3
        await inspect_audit(after.guild, discord.AuditLogAction.role_update, after.id, "role-permissions", threshold)


@client.event
async def on_guild_channel_update(before, after):
    if before.overwrites != after.overwrites:
        await inspect_audit(after.guild, discord.AuditLogAction.channel_update, after.id,
                            "channel-overwrites", 2)


@client.event
async def on_webhooks_update(channel):
    for action in (discord.AuditLogAction.webhook_create,
                   discord.AuditLogAction.webhook_update,
                   discord.AuditLogAction.webhook_delete):
        await inspect_audit(channel.guild, action, None, action.name, 1, channel.id)


@client.event
async def on_member_ban(guild, user):
    await inspect_audit(guild, discord.AuditLogAction.ban, user.id, "member-ban", 3)


@tree.command(name="guard_setup", description="OWNER: post Venice verification and ticket panels", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def guard_setup(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    verify_channel = await channel_by_id(VERIFY_CHANNEL_ID)
    ticket_channel = await channel_by_id(CREATE_TICKET_CHANNEL_ID)
    if not verify_channel or not ticket_channel:
        await interaction.followup.send("Configured panel channels were not found.", ephemeral=True)
        return
    verify_embed = blue_embed(
        "Verify for Venice",
        "Press **Verify**, solve the short security question, and receive the Verified role. "
        "New accounts and join spikes are reviewed automatically.",
    )
    ticket_embed = blue_embed(
        "Venice Support",
        "Choose **Support**, **Purchase Help**, or **Report**. Tickets are private to you and Staff; "
        "a transcript is stored in the private ticket log when closed.",
    )
    await verify_channel.send(embed=verify_embed, view=VerificationView())
    await ticket_channel.send(embed=ticket_embed, view=TicketPanelView())
    await interaction.followup.send("Venice Guard panels posted.", ephemeral=True)


@tree.command(name="security_status", description="Show Venice Guard status", guild=GUILD)
async def security_status(interaction: discord.Interaction):
    embed = blue_embed("Venice Guard Status")
    embed.add_field(name="Gateway", value="Online", inline=True)
    embed.add_field(name="Raid mode", value="Active" if security_state["raid_mode"] else "Normal", inline=True)
    embed.add_field(name="Trusted actors", value=str(len(TRUSTED_ACTOR_IDS)), inline=True)
    embed.add_field(name="Protections", value="Verification • anti-spam • invite/phishing filter • join-spike quarantine • anti-nuke audit", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="lockdown", description="STAFF: pause verification during an incident", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
async def lockdown(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 60] = 10):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    security_state["raid_mode"] = True
    security_state["raid_until"] = utcnow() + dt.timedelta(minutes=minutes)
    await interaction.response.send_message(f"Verification paused for {minutes} minutes.", ephemeral=True)
    await log_security("Manual lockdown", f"{interaction.user.mention} paused verification for {minutes} minutes.")


@tree.command(name="unlock", description="STAFF: return Venice Guard to normal mode", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
async def unlock(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    security_state["raid_mode"] = False
    security_state["raid_until"] = None
    await interaction.response.send_message("Verification restored.", ephemeral=True)
    await log_security("Lockdown cleared", f"{interaction.user.mention} restored verification.", color=0x22C55E)


@tree.command(name="guard_quarantine", description="STAFF: timeout a disruptive member with an audit reason", guild=GUILD)
@app_commands.default_permissions(moderate_members=True)
async def guard_quarantine(interaction: discord.Interaction, member: discord.Member,
                           minutes: app_commands.Range[int, 1, 1440], reason: str):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    reason = reason.strip()[:200]
    if not reason or member.id == interaction.guild.owner_id or is_staff(member) or member.bot:
        await interaction.response.send_message("A non-staff human member and an audit reason are required.", ephemeral=True)
        return
    try:
        await member.timeout(dt.timedelta(minutes=minutes), reason=f"Venice Guard by {interaction.user.id}: {reason}")
    except discord.DiscordException as exc:
        await interaction.response.send_message(f"Timeout failed: {type(exc).__name__}.", ephemeral=True)
        return
    await interaction.response.send_message(f"Quarantined {member.mention} for {minutes} minutes.", ephemeral=True)
    await log_security("Staff quarantine", f"{interaction.user.mention} timed out {member.mention} for {minutes}m. Reason: {reason}")


@tree.command(name="guard_release", description="STAFF: lift a Venice Guard timeout with an audit reason", guild=GUILD)
@app_commands.default_permissions(moderate_members=True)
async def guard_release(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    reason = reason.strip()[:200]
    if not reason or member.bot:
        await interaction.response.send_message("A human member and an audit reason are required.", ephemeral=True)
        return
    try:
        await member.timeout(None, reason=f"Venice Guard release by {interaction.user.id}: {reason}")
    except discord.DiscordException as exc:
        await interaction.response.send_message(f"Release failed: {type(exc).__name__}.", ephemeral=True)
        return
    await interaction.response.send_message(f"Timeout lifted for {member.mention}.", ephemeral=True)
    await log_security("Staff release", f"{interaction.user.mention} released {member.mention}. Reason: {reason}", color=BLUE)


@tree.command(name="guard_purge", description="STAFF: clear recent spam without deleting pinned messages", guild=GUILD)
@app_commands.default_permissions(manage_messages=True)
async def guard_purge(interaction: discord.Interaction, count: app_commands.Range[int, 1, 50], reason: str):
    if (not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user)
            or not isinstance(interaction.channel, discord.TextChannel)):
        await interaction.response.send_message("Staff text channels only.", ephemeral=True)
        return
    reason = reason.strip()[:200]
    if not reason:
        await interaction.response.send_message("An audit reason is required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=count, check=lambda m: not m.pinned)
    except discord.DiscordException as exc:
        await interaction.followup.send(f"Purge failed: {type(exc).__name__}.", ephemeral=True)
        return
    await interaction.followup.send(f"Removed {len(deleted)} recent unpinned messages.", ephemeral=True)
    await log_security("Staff spam purge", f"{interaction.user.mention} cleared {len(deleted)} messages in {interaction.channel.mention}. Reason: {reason}")


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
