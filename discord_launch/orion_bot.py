"""
Orion License Bot — always-on gateway bot (discord.py).
Thin layer over the Orion Lambda: it owns the Discord side (slash commands,
DMs, roles, the online presence); the Lambda owns all license logic + DynamoDB.

Commands: /claim_trial  /hwid_reset  /status  /setup  /faq  /deliver (admin)
The launcher killswitch is NOT a bot command — it's owner-console-only (OrionOwner.exe / orion-admin CLI).
Delivery on purchase is handled separately by the Lambda's SellHub webhook.

ENV VARS (set on your host — do NOT hardcode secrets):
  DISCORD_BOT_TOKEN   bot token (Dev Portal -> Bot)
  ORION_BOT_SECRET    must equal SSM /orion/bot_service_secret
  ORION_API_BASE      https://v348t5hg3i.execute-api.us-east-1.amazonaws.com   (direct origin; bypasses Cloudflare)
  ORION_GUILD_ID      your server id
  CUSTOMER_ROLE_ID    role added on purchase / not used by trial
  LIFETIME_ROLE_ID    role added for Lifetime
  TRIAL_ROLE_ID       role added on trial claim
  ORION_LOGO_URL      (optional) embed thumbnail
  ORION_ADMIN_LOG     (optional) channel name for admin alerts (default: staff-chat)

/status extras (the license lookup rides the EXISTING /api/staff/license endpoint,
which takes a staff bearer token — NOT the bot secret. Enroll a dedicated staff
identity for the bot ONCE via /api/staff/enroll, then set):
  ORION_STAFF_ID          (optional) staff_id the bot logs in as for lookups.
                          Unset => /status degrades politely to "open a ticket".
  ORION_STAFF_MACHINE_ID  (optional) machine_id used at that enrollment
                          (default "orion-bot-host" — pick one and keep it).
  ORION_EDGE_AUTH         REQUIRED (was "optional"). X-Edge-Auth header value;
                          must equal SSM /orion/edge_auth_secret.
                          Corrected 2026-08-04: /api/bot/* is NO LONGER exempt
                          from the lambda's edge-auth check, and the check is no
                          longer gated on EDGE_AUTH_ENFORCE — the HIGH-3/HIGH-4
                          hardening made require_edge_auth() unconditional for
                          every route and fail-closed. Without this set, EVERY
                          command (/claim_trial /hwid_reset /deliver) gets a 403
                          before reaching the bot-secret gate.

Run:  pip install -U "discord.py>=2.3" aiohttp   then   python orion_bot.py
"""
import os
import time
import secrets as pysecrets
import discord
from discord import app_commands
import aiohttp

BOT_TOKEN        = os.environ["DISCORD_BOT_TOKEN"]
BOT_SECRET       = os.environ["ORION_BOT_SECRET"]
API_BASE         = os.environ.get("ORION_API_BASE", "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com").rstrip("/")
GUILD_ID         = int(os.environ["ORION_GUILD_ID"])
CUSTOMER_ROLE_ID = int(os.environ.get("CUSTOMER_ROLE_ID", "0"))
LIFETIME_ROLE_ID = int(os.environ.get("LIFETIME_ROLE_ID", "0"))
TRIAL_ROLE_ID    = int(os.environ.get("TRIAL_ROLE_ID", "0"))
LOGO_URL         = os.environ.get("ORION_LOGO_URL", "")
ADMIN_LOG        = os.environ.get("ORION_ADMIN_LOG", "staff-chat")
STAFF_ID         = os.environ.get("ORION_STAFF_ID", "")
STAFF_MACHINE_ID = os.environ.get("ORION_STAFF_MACHINE_ID", "orion-bot-host")
EDGE_AUTH        = os.environ.get("ORION_EDGE_AUTH", "")
EMBED_COLOR      = 0x2563EB

intents = discord.Intents.default()          # no privileged intents needed
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)
GUILD   = discord.Object(id=GUILD_ID)


async def api_post(path, payload, headers):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(API_BASE + path, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                try:
                    return r.status, await r.json()
                except Exception:
                    return r.status, {}
    except Exception as e:
        print("api_post error:", e)
        return 0, {}


async def lambda_post(path, payload):
    # Both gates apply: X-Edge-Auth at the lambda's router, X-Orion-Bot-Secret
    # inside each /api/bot/* handler. Sending only the latter 403s at the router.
    headers = {"X-Orion-Bot-Secret": BOT_SECRET, "Content-Type": "application/json"}
    if EDGE_AUTH:
        headers["X-Edge-Auth"] = EDGE_AUTH
    return await api_post(path, payload, headers)


# ── staff session (for /status license lookups) ───────────────────────────────
# /api/staff/license wants a staff bearer token, not the bot secret — see the
# docstring. Token is cached and re-fetched once on 401/403 (expiry/rotation).

_staff_session = {"token": "", "expires": 0}


def _staff_base_headers():
    h = {"Content-Type": "application/json"}
    if EDGE_AUTH:
        h["X-Edge-Auth"] = EDGE_AUTH
    return h


async def staff_login():
    if not STAFF_ID:
        return False
    payload = {"staff_id": STAFF_ID, "machine_id": STAFF_MACHINE_ID,
               "nonce": pysecrets.token_hex(16), "timestamp": int(time.time())}
    st, data = await api_post("/api/staff/login", payload, _staff_base_headers())
    if st == 200 and data.get("ok") and data.get("token"):
        _staff_session["token"] = data["token"]
        _staff_session["expires"] = int(data.get("expires", 0))
        return True
    print(f"[STAFF] login failed status={st} body={data} — is ORION_STAFF_ID enrolled "
          f"with machine_id={STAFF_MACHINE_ID!r}? (and ORION_EDGE_AUTH set if enforced)")
    return False


async def staff_post(path, payload):
    """POST as the bot's staff identity. Returns (status, data);
    status 0 = backend unreachable, -1 = staff auth not configured / login failed."""
    if not STAFF_ID:
        return -1, {}
    if not _staff_session["token"] or _staff_session["expires"] - 60 <= time.time():
        if not await staff_login():
            return -1, {}
    headers = _staff_base_headers()
    headers["Authorization"] = "Bearer " + _staff_session["token"]
    headers["X-Machine-Id"] = STAFF_MACHINE_ID
    st, data = await api_post(path, payload, headers)
    if st in (401, 403):                       # expired/rotated token — one re-login + retry
        _staff_session["token"] = ""
        if not await staff_login():
            return -1, {}
        headers["Authorization"] = "Bearer " + _staff_session["token"]
        st, data = await api_post(path, payload, headers)
    return st, data


async def backend_selfcheck():
    """Probe the backend ONCE at startup and print a loud, actionable diagnosis. The bot's
    commands die silently otherwise. As of the 2026-07-03 reconciliation, the deployed
    orion-activate Lambda DOES have the /api/bot/* routes (trial/hwid-reset/killswitch/deliver/
    provision) wired to require_bot(), and SSM /orion/bot_service_secret is the shared secret
    both this bot and the sellhub webhook use — confirmed by reading the live deployed bundle.
    This probe still runs every startup as a live sanity check (env drift, a rotated secret, a
    future route removal), not because the wiring is in doubt anymore.
    Uses killswitch 'status' (a pure read) as the probe — no side effects."""
    st, data = await lambda_post("/api/bot/killswitch", {"action": "status", "by": "selfcheck"})
    if st == 0:
        print("[SELFCHECK] FAIL: cannot reach the backend at all — check ORION_API_BASE / network.")
    elif st == 404:
        print("[SELFCHECK] FAIL: backend returned 404 for /api/bot/killswitch — route missing or "
              "API Gateway misconfigured. This should not happen on the reconciled deploy; check "
              "for a bad redeploy or stage mismatch.")
    elif st in (401, 403):
        print("[SELFCHECK] FAIL: backend rejected the bot secret (or edge-auth blocks the direct "
              "origin for bot routes). Verify ORION_BOT_SECRET == SSM /orion/bot_service_secret "
              "and that the lambda exempts /api/bot/* from the edge-auth header check.")
    elif data.get("ok"):
        print(f"[SELFCHECK] OK: backend bot routes live (killswitch status={data.get('enabled')}).")
    else:
        print(f"[SELFCHECK] WARN: unexpected reply status={st} body={data} — verify manually.")


def key_embed(title, key, tier, extra=""):
    e = discord.Embed(
        title=title,
        description=(f"**Tier:** {tier}\n\n**Key**\n```\n{key}\n```\n{extra}"
                     "Activate it in the Orion launcher. Keep it private — sharing or reselling voids it."),
        color=EMBED_COLOR)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Orion • Precision Shot-Timing")
    return e


# ── FAQ content (source of truth: docs/gtm/faq.md — keep in sync) ─────────────

FAQ_TOPICS = {
    "capture_card": {
        "label": "Capture card not detected",
        "emoji": "🎥",
        "title": "🎥 My capture card isn't detected",
        "description": (
            "**Orion can't see your capture card? Work down this list — it's almost always #1–#3.**\n\n"
            "**1. Close everything else that uses the card.** OBS, Discord video, the card's own viewer app, "
            "browser tabs with camera access — capture cards allow **one** app at a time. Close them all, then restart Orion.\n\n"
            "**2. Check the physical chain.** Card plugged into a **USB 3.0+ port** (blue, or the fastest port you have — "
            "avoid unpowered hubs)? HDMI from the **console's output** into the card's **IN** port? "
            "Console actually powered on and outputting a picture?\n\n"
            "**3. HDCP is the silent killer.** The PS5 encrypts HDMI output by default and your card will show **black or nothing**. "
            "On the PS5: **Settings → System → HDMI → disable \"Enable HDCP.\"** This is the single most common \"not detected\" cause.\n\n"
            "**4. Driver check.** Open Windows **Device Manager** — does the card show up (usually under \"Cameras\" or "
            "\"Sound, video and game controllers\") without a warning icon? If not, install the manufacturer's driver and replug.\n\n"
            "**5. Still nothing?** Some cards only expose certain resolutions — set the console output to **1080p** and try again.\n\n"
            "If it still won't show, open a ticket with: **card model, Windows version, console, and a screenshot of Device Manager**. "
            "Known-good cards are listed in the requirements — if you haven't bought a card yet, check that list first."
        ),
    },
    "remote_play": {
        "label": "Remote Play won't pair / laggy",
        "emoji": "🎮",
        "title": "🎮 Remote Play won't pair / the stream is laggy",
        "description": (
            "**Pairing problems:**\n"
            "1. On the PS5: **Settings → System → Remote Play → ON**, and **Settings → System → Power Saving → "
            "Features Available in Rest Mode → \"Stay Connected to the Internet\" + \"Enable Turning On PS5 from Network\"** both ON.\n"
            "2. Get the pairing PIN from **Settings → System → Remote Play → Link Device** — it expires quickly, "
            "so have Chiaki open and ready.\n"
            "3. PC and PS5 must be on the **same network** for first pairing. Enter the console's IP exactly as shown "
            "under Settings → Network → Connection Status.\n"
            "4. Pairing loops or times out? Reboot the PS5 fully (not rest mode) and generate a fresh PIN.\n\n"
            "**Lag / stutter / rubber-banding:**\n"
            "• **Wire everything.** Ethernet to the PS5 and Ethernet to the PC is the setup Orion is built for. "
            "Wi-Fi on either end adds jitter that no software can time through. If you must use Wi-Fi: 5 GHz / Wi-Fi 6, "
            "same room as the router, nothing else streaming.\n"
            "• Drop the Remote Play stream quality one notch — a stable 720p stream times better than a stuttering 1080p one.\n"
            "• Close downloads, cloud sync, and anyone else's Netflix on the network.\n\n"
            "**Be straight with yourself here:** Orion reads your stream and is exactly as fast as that stream. "
            "A clean wired link = clean greens. A congested Wi-Fi link = late reads, and no setting can fix physics. "
            "This is also why we say it **before** you buy."
        ),
    },
    "no_greens": {
        "label": "No greens / overlay not reading",
        "emoji": "🟩",
        "title": "🟩 No greens / the overlay isn't reading the meter",
        "description": (
            "**Orion runs but shots come out early, late, or the overlay never locks onto the meter? Check these in order:**\n\n"
            "**1. Is the stream actually visible in Orion?** Open the Live Capture panel — you should see your gameplay. "
            "If it's black, that's the real problem: reconnect (Disconnect → Connect Chiaki), make sure Chiaki shows the game "
            "and not its setup screen, and keep the Orion window visible (don't minimize it).\n\n"
            "**2. Hold, don't tap.** Orion releases for you — **hold** Square (tempo), right-stick-down (stick tempo), "
            "or right-stick-up (go-to) and let go of nothing. If you release manually, you're fighting the tool.\n\n"
            "**3. Controller in the right place?** It must be plugged into the **PC**. If your pad is paired to the PS5 directly, "
            "Orion can see the meter but can't act on it.\n\n"
            "**4. Meter visible in-game?** Shot meter ON in 2K's settings, and use a meter style/size Orion has calibrated "
            "against (defaults work best). A hidden or exotic meter gives the vision system nothing to read.\n\n"
            "**5. Let calibration finish.** The first 10–15 shots in practice teach Orion your jumper. "
            "Judging it off shot #2 in a Rec game is judging it before it's calibrated.\n\n"
            "**6. Stream quality dips = read quality dips.** If lag spikes coincide with the misses, it's the network — "
            "run `/faq` → Remote Play.\n\n"
            "Still off after all six? Open a ticket with a **short clip or screenshot of the overlay during a shot** — "
            "that one image usually tells us the fix immediately."
        ),
    },
    "new_pc": {
        "label": "New PC / move my license",
        "emoji": "💻",
        "title": "💻 I got a new PC (moving your license / HWID reset)",
        "description": (
            "Your license is **single-user and locked to one PC** — that's what keeps keys from being shared "
            "and your purchase from being resold. Moving to a new machine is self-service:\n\n"
            "1. In the Discord server, run **`/hwid_reset`**.\n"
            "2. The bot unbinds your key from the old machine.\n"
            "3. On the new PC: install Orion, paste your **same key**, activate. Done.\n\n"
            "**Limits (anti-sharing, not anti-you):**\n"
            "• Once per **24 hours**.\n"
            "• Up to **3 resets per 30 days**.\n\n"
            "Rebuilt Windows, swapped major hardware, or RMA'd the machine? Same command — a reinstall on the same box "
            "sometimes reads as a \"new\" machine and one reset fixes it.\n\n"
            "Legitimately need more than the limit (multiple hardware failures, stolen laptop)? Open a ticket with a short "
            "explanation — staff can reset it manually. What we **don't** do is resets that let two people share one key; "
            "that pattern gets the key revoked."
        ),
    },
    "missing_key": {
        "label": "Paid but never got my key",
        "emoji": "🔑",
        "title": "🔑 I paid but never got my key",
        "description": (
            "Nine times out of ten this is the **Discord ID field** at checkout — it needs your numeric User ID, "
            "not your username.\n\n"
            "1. Make sure you've **joined this server** and your **DMs are open** (Privacy Settings → Direct Messages ON), "
            "then check your DMs again — delivery can take a minute.\n"
            "2. Still nothing? Open a ticket in **#create-ticket** with the **email you used at checkout** — we'll verify "
            "the purchase and hand you your key directly. You will not be left paying for nothing.\n\n"
            "*(Buying in future? Instructions for copying your numeric Discord ID are in #how-to-buy — "
            "Settings → Advanced → Developer Mode ON, then right-click your name → Copy User ID.)*"
        ),
    },
    "quick_answers": {
        "label": "Quick answers (trial, tiers, expiry…)",
        "emoji": "❓",
        "title": "❓ Quick answers",
        "description": (
            "• **Is there a free trial?** Yes — `/claim_trial` in the server. Full features, no card. "
            "One per Discord account and per PC.\n"
            "• **Does every tier have all features?** Yes. Tiers differ only in duration.\n"
            "• **Can I use it on two PCs at once?** No — single-user, one machine. Use `/hwid_reset` to move it.\n"
            "• **When does my license expire?** Run `/status` with your key — instant, private answer. "
            "(Your key is in your original delivery DM.)\n"
            "• **What does Orion actually do to my game?** Nothing. It watches your screen and times a controller input — "
            "it never touches game files, game memory, or your console. See the \"What Orion is\" post in #faq."
        ),
    },
}


def faq_embed(topic: str) -> discord.Embed:
    t = FAQ_TOPICS[topic]
    e = discord.Embed(title=t["title"], description=t["description"], color=EMBED_COLOR)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Orion • Precision Shot-Timing — still stuck? Open a ticket in #create-ticket")
    return e


def status_embed(lic: dict, now: int = None) -> discord.Embed:
    """Render the SAFE license fields returned by /api/staff/license action=lookup
    (suffix/status/revoked/plan/expiry/activations/machine_suffix — never the full key)."""
    now = int(time.time()) if now is None else now
    expiry   = int(lic.get("expiry") or 0)
    revoked  = bool(lic.get("revoked")) or lic.get("status") == "revoked"
    lifetime = expiry == 0
    expired  = (not lifetime) and expiry <= now
    active   = lic.get("status") == "active" and not revoked and not expired

    if revoked:
        state = "❌ Revoked — open a ticket in #create-ticket if you think this is a mistake"
    elif expired:
        state = "⌛ Expired — grab a new tier in the store to keep going"
    elif active:
        state = "✅ Active"
    else:
        state = f"❌ Inactive ({lic.get('status') or 'unknown'})"

    if lifetime and not revoked:
        expires_txt = "Never — Lifetime ⭐"
    else:
        expires_txt = f"<t:{expiry}:F> (<t:{expiry}:R>)"

    suffix = lic.get("machine_suffix")
    machine_txt = f"🔒 Bound to your PC (…{suffix})" if suffix else "Not activated on a PC yet"

    e = discord.Embed(title=f"🔑 Your Orion License (…{lic.get('license_key_suffix', '????')})",
                      color=EMBED_COLOR)
    e.add_field(name="Status",  value=state, inline=False)
    e.add_field(name="Plan",    value=(lic.get("plan") or "?").capitalize(), inline=True)
    e.add_field(name="Expires", value=expires_txt, inline=True)
    e.add_field(name="Machine", value=machine_txt, inline=False)
    e.add_field(name="Activations", value=str(lic.get("activations", 0)), inline=True)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Orion • Precision Shot-Timing — new PC? /hwid_reset moves it yourself")
    return e


class SetupView(discord.ui.View):
    """/setup — one button per killer failure; each posts the matching FAQ fix, ephemeral."""
    TOPICS = ("capture_card", "remote_play", "no_greens")

    def __init__(self):
        super().__init__(timeout=600)
        for key in self.TOPICS:
            t = FAQ_TOPICS[key]
            self.add_item(self._make_button(key, t["label"], t["emoji"]))

    @staticmethod
    def _make_button(key, label, emoji):
        btn = discord.ui.Button(label=label, emoji=emoji, custom_id=f"setup:{key}",
                                style=discord.ButtonStyle.primary)

        async def _cb(interaction: discord.Interaction):
            await interaction.response.send_message(embed=faq_embed(key), ephemeral=True)

        btn.callback = _cb
        return btn


@client.event
async def on_ready():
    try:
        synced = await tree.sync(guild=GUILD)
        print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    except Exception as e:
        print("command sync error:", e)
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="🎯 Orion"))
    await backend_selfcheck()
    print(f"{client.user} is online")


@tree.command(name="claim_trial", description="Claim your free 3-day Venice trial", guild=GUILD)
async def claim_trial(interaction: discord.Interaction):
    # Contract changed 2026-08-04: /api/bot/trial now DMs the key and grants the
    # Trial role server-side, and deliberately no longer returns license_key —
    # the key must not transit the Cloudflare Worker that serves interactions.
    # This command is now a thin relay of the backend's message, and it rolls the
    # claim back itself on a DM bounce, so there's nothing to do here but report.
    await interaction.response.defer(ephemeral=True)
    _, data = await lambda_post("/api/bot/trial", {"discord_id": interaction.user.id})
    prefix = "✅ " if data.get("ok") else "❌ "
    await interaction.followup.send(
        prefix + data.get("message", "Could not create a trial right now."), ephemeral=True)


@tree.command(name="hwid_reset", description="Unbind your Orion license so you can activate on a new PC", guild=GUILD)
async def hwid_reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, data = await lambda_post("/api/bot/hwid-reset", {"discord_id": interaction.user.id})
    await interaction.followup.send(("✅ " if data.get("ok") else "❌ ") + data.get("message", "Error."), ephemeral=True)


@tree.command(name="status", description="Check your Orion license — active, plan, expiry, machine binding", guild=GUILD)
@app_commands.describe(key="Your license key from your delivery DM (XXXX-XXXX-XXXX-XXXX)")
async def status(interaction: discord.Interaction, key: str):
    # Everything here is ephemeral — the key never appears in the channel.
    await interaction.response.defer(ephemeral=True)
    norm = key.strip().upper()
    if not norm:
        await interaction.followup.send("❌ Paste your license key — it's in your delivery DM.", ephemeral=True)
        return
    st, data = await staff_post("/api/staff/license", {"action": "lookup", "license_key": norm})
    # DESIGN TODO (backend, deliberately NOT done here — lambda_function.py is frozen
    # mid-flight): a discord_id-keyed read-only /api/bot/status would drop the
    # "paste your key" step entirely (handle_bot_hwid_reset already resolves
    # discord_id → license). Until then /status proxies the staff lookup by key.
    if st == -1:
        await interaction.followup.send(
            "❌ Self-service lookup isn't configured yet — open a ticket in **#create-ticket** "
            "and we'll check your license for you.", ephemeral=True)
        return
    if st == 0:
        await interaction.followup.send("❌ Backend unreachable — try again in a minute or open a ticket.", ephemeral=True)
        return
    if st == 404 or data.get("error") == "invalid_key":
        await interaction.followup.send(
            "❌ That key isn't in our system. Check for typos — keys never contain `O`, `I`, `0`, or `1`. "
            "Paid but never got a key? Run `/faq` → *Paid but never got my key*.", ephemeral=True)
        return
    lic = data.get("license") if data.get("ok") else None
    if not lic:
        await interaction.followup.send(f"❌ {data.get('error', 'Lookup failed — try again or open a ticket.')}", ephemeral=True)
        return
    await interaction.followup.send(embed=status_embed(lic), ephemeral=True)


@tree.command(name="setup", description="Troubleshoot the common setup failures — pick your symptom", guild=GUILD)
async def setup(interaction: discord.Interaction):
    e = discord.Embed(
        title="🛠 Orion Setup Troubleshooter",
        description=("Pick the symptom below and I'll post the fix.\n\n"
                     "🎥 **Capture card not detected** — Orion can't see your card / black screen\n"
                     "🎮 **Remote Play won't pair / laggy** — pairing loops, PIN timeouts, stutter\n"
                     "🟩 **No greens / overlay not reading** — Orion runs but shots come out early or late\n\n"
                     "Something else? `/faq` covers licenses, new PCs, and delivery — "
                     "or open a ticket in **#create-ticket**."),
        color=EMBED_COLOR)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Orion • Precision Shot-Timing")
    await interaction.response.send_message(embed=e, view=SetupView(), ephemeral=True)


@tree.command(name="faq", description="Get an answer from the Orion FAQ", guild=GUILD)
@app_commands.describe(topic="What do you need help with?",
                       public="Post it in the channel so others can see it (default: only you)")
@app_commands.choices(topic=[
    app_commands.Choice(name=f'{t["emoji"]} {t["label"]}', value=key)
    for key, t in FAQ_TOPICS.items()
])
async def faq(interaction: discord.Interaction, topic: str, public: bool = False):
    await interaction.response.send_message(embed=faq_embed(topic), ephemeral=not public)


@tree.command(name="deliver", description="ADMIN: mint a license and DM it to a user", guild=GUILD)
@app_commands.describe(user="recipient", plan="license tier", days="override length in days (optional)")
@app_commands.choices(plan=[
    app_commands.Choice(name="day", value="day"),
    app_commands.Choice(name="week", value="week"),
    app_commands.Choice(name="month", value="month"),
    app_commands.Choice(name="lifetime", value="lifetime"),
])
async def deliver(interaction: discord.Interaction, user: discord.User, plan: str, days: int = 0):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    payload = {"discord_id": user.id, "plan": plan.lower()}
    if days > 0:
        payload["days"] = days
    _, data = await lambda_post("/api/bot/deliver", payload)
    if not data.get("ok"):
        await interaction.followup.send(f"❌ {data.get('message', 'error')}", ephemeral=True)
        return
    key = data.get("license_key")
    if not key:
        await interaction.followup.send("❌ Backend returned ok but no key — check the lambda (contract drift).",
                                        ephemeral=True)
        return
    try:
        await user.send(embed=key_embed("🔑 Your Orion License", key, plan.capitalize()))
    except discord.Forbidden:
        await interaction.followup.send(f"⚠️ Minted `{key}` but couldn't DM {user.mention} (DMs closed) — deliver manually.", ephemeral=True)
        return
    try:
        member = interaction.guild.get_member(user.id) or await interaction.guild.fetch_member(user.id)
        if CUSTOMER_ROLE_ID:
            await member.add_roles(discord.Object(id=CUSTOMER_ROLE_ID))
        if plan.lower() == "lifetime" and LIFETIME_ROLE_ID:
            await member.add_roles(discord.Object(id=LIFETIME_ROLE_ID))
    except Exception:
        pass
    await interaction.followup.send(f"✅ Delivered **{plan}** to {user.mention}.", ephemeral=True)


if __name__ == "__main__":
    client.run(BOT_TOKEN)
