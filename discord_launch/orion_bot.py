"""
Orion License Bot — always-on gateway bot (discord.py).
Thin layer over the Orion Lambda: it owns the Discord side (slash commands,
DMs, roles, the online presence); the Lambda owns all license logic + DynamoDB.

Commands: /purchase  /claim_trial  /hwid_reset  /status  /setup  /faq
          /deliver /keygen /lookup  (staff — the SERVER resolves the invoker's
          Discord id against orion-staff; docs/ADMIN_PANEL_V2_CONTRACT.md §1/§4)
The launcher killswitch is NOT a bot command — it's owner-console-only (OrionOwner.exe / orion-admin CLI).
/api/bot/killswitch is status-only (contract §5); the startup selfcheck reads it, nothing writes it.
Delivery on purchase is handled by the website checkout webhook and backend.

Every /api/bot/* call forwards `actor_discord_id` = the invoking user (contract §4).

ENV VARS (set on your host — do NOT hardcode secrets):
  DISCORD_BOT_TOKEN   bot token (Dev Portal -> Bot)
  ORION_BOT_SECRET    must equal SSM /orion/bot_service_secret
  ORION_API_BASE      https://v348t5hg3i.execute-api.us-east-1.amazonaws.com   (direct origin; bypasses Cloudflare)
  ORION_GUILD_ID      your server id
  CUSTOMER_ROLE_ID    role added on purchase / not used by trial
  LIFETIME_ROLE_ID    LEGACY/optional — only applied when staff deliver a lifetime comp
  TRIAL_ROLE_ID       role added on trial claim
  ORION_LOGO_URL      (optional) embed thumbnail
  ORION_ADMIN_LOG     (optional) channel name for admin alerts (default: staff-chat)
  STAFF_ROLE_IDS      (optional) comma-separated Discord role ids allowed to /lookup OTHER
                      users. Client-side gate only — /api/bot/status has no server-side
                      staff check yet. /deliver and /keygen are gated by the SERVER.

/lookup(key) extras (staff-only lookups use the EXISTING /api/staff/license
endpoint; /status uses the self-only /api/bot/status route by Discord ID):
  ORION_STAFF_ID          (optional) staff_id the bot logs in as for lookups.
                          Unset => staff key lookup degrades politely.
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
STORE_URL        = "https://zaeorion.com/#pricing"
STAFF_ROLE_IDS   = {int(x) for x in os.environ.get("STAFF_ROLE_IDS", "").split(",") if x.strip().isdigit()}
EMBED_COLOR      = 0x2563EB
MAX_REASON       = 200          # contract: every mutation carries reason ≤ 200 chars
MAX_KEYGEN_COUNT = 25           # contract §2: count ≤ 25 per call

# Server routes this bot depends on (all POST). Keep in sync with the report.
ROUTE_STATUS     = "/api/bot/status"       # {discord_id, actor_discord_id}
ROUTE_HWID_RESET = "/api/bot/hwid-reset"   # {discord_id, actor_discord_id, mode?}      contract §3
ROUTE_TRIAL      = "/api/bot/trial"        # {discord_id, actor_discord_id}
ROUTE_DELIVER    = "/api/bot/deliver"      # {discord_id?, plan, days?, count?, reason, actor_discord_id}  §4
ROUTE_KILLSWITCH = "/api/bot/killswitch"   # {action: "status"} ONLY (contract §5 — writes removed)
# Staff-token route (NOT the bot secret): the bot logs in as its own enrolled staff
# identity, so `actor_discord_id` here is context for the audit row, not the auth
# factor. {action: "lookup", license_key|key, reason, actor_discord_id}  contract §2.
ROUTE_STAFF_LIC  = "/api/staff/license"

# Codes the server uses to refuse a staff action. NOTE the router's edge-auth
# gate answers the SAME `403 {"error":"forbidden"}`, so the refusal message
# covers both (ask the backend for a distinct code, e.g. not_staff, if needed).
STAFF_REFUSAL_ERRORS = {"forbidden", "not_staff", "staff_required", "capability_denied", "actor_required"}

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


# ── staff session (for staff-only /lookup key lookups) ─────────────────────────
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
    Uses killswitch 'status' (a pure read) as the probe — no side effects. This is the
    ONLY killswitch call in the bot: contract §5 removed the route's write actions and
    the owner flips global kill from OrionOwner.exe / orion-admin."""
    st, data = await lambda_post(ROUTE_KILLSWITCH, killswitch_status_payload())
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


def killswitch_status_payload():
    return {"action": "status", "by": "selfcheck"}


# ── staff-action helpers (contract §1/§4) ─────────────────────────────────────

def has_staff_role(interaction) -> bool:
    """Client-side gate for /lookup of OTHER users only (read-only route, no
    server-side actor check yet). Mutations (/deliver, /keygen) are gated by
    the server's orion-staff resolution, never by this."""
    if not STAFF_ROLE_IDS:
        return False
    roles = getattr(getattr(interaction, "user", None), "roles", None) or []
    return any(getattr(r, "id", None) in STAFF_ROLE_IDS for r in roles)


def is_staff_refusal(status: int, data: dict) -> bool:
    return (not data.get("ok")) and (status == 403 or data.get("error") in STAFF_REFUSAL_ERRORS)


def staff_refusal_text(status: int, data: dict) -> str:
    msg = "❌ Refused: this action is limited to staff (admin+) on the license server's staff list."
    if status == 403:
        msg += (" If you ARE staff, the bot's ORION_EDGE_AUTH / ORION_BOT_SECRET may be "
                "misconfigured — tell the owner.")
    detail = data.get("message")
    if detail and data.get("error") != "forbidden":
        msg += f"\n({detail})"
    return msg


def valid_reason(raw) -> tuple:
    """-> (reason, error_text). Every mutation needs a reason ≤ 200 chars (contract)."""
    reason = str(raw or "").strip()
    if not reason:
        return "", "❌ A `reason` is required (it goes in the audit log)."
    if len(reason) > MAX_REASON:
        return "", f"❌ `reason` must be ≤ {MAX_REASON} characters."
    return reason, ""


def deliver_payload(actor_id, target_id, plan: str, reason: str, days: int = 0) -> dict:
    p = {"discord_id": target_id, "plan": str(plan or "").lower(), "reason": reason,
         "actor_discord_id": actor_id}
    if days and int(days) > 0:
        p["days"] = int(days)
    return p


def keygen_payload(actor_id, plan: str, reason: str, count: int = 1, days: int = 0, note: str = "") -> dict:
    # ASSUMED payload extension: `count` on /api/bot/deliver (the contract defines
    # count on /api/staff/license create, which needs a bearer token). If the
    # server ignores it, exactly one key comes back and the reply says so.
    p = {"plan": str(plan or "").lower(), "count": int(count), "reason": reason,
         "actor_discord_id": actor_id}
    if days and int(days) > 0:
        p["days"] = int(days)
    if note:
        p["note"] = str(note)[:MAX_REASON]
    return p


def staff_lookup_payload(actor_id, license_key: str, reason: str = "") -> dict:
    """Body for /api/staff/license action=lookup.

    Sends the key under BOTH names on purpose: the live lambda reads
    `license_key`, the contract (§2) spells it `key`. Same dual-send trick as
    discord_id/actor_discord_id — drop the loser once the backend lands."""
    key = str(license_key or "").strip().upper()
    return {
        "action": "lookup",
        "license_key": key,
        "key": key,
        "reason": reason or f"discord lookup by {actor_id}",
        "actor_discord_id": actor_id,
    }


def keys_from(data: dict) -> list:
    for k in ("keys", "license_keys"):
        if isinstance(data.get(k), list):
            return [str(x) for x in data[k]]
    return [str(data["license_key"])] if data.get("license_key") else []


def _fmt_expiry(expiry, lifetime=False) -> str:
    if lifetime or expiry == 0:
        return "never — Lifetime"
    if not expiry:
        return "unknown"
    return f"<t:{int(expiry)}:D> (<t:{int(expiry)}:R>)"


# ── /hwid_reset (contract §3) ─────────────────────────────────────────────────

def hwid_reset_payload(user_id, mode: str = "") -> dict:
    # `discord_id` is what today's Lambda reads; `actor_discord_id` is the
    # contract's name. Send both so the command works across the rollout.
    p = {"discord_id": user_id, "actor_discord_id": user_id}
    if mode:
        p["mode"] = mode
    return p


def _free_resets_remaining(d: dict):
    for k in ("resets_remaining", "free_resets_remaining", "hwid_free_remaining"):
        if isinstance(d.get(k), int):
            return d[k]
    if isinstance(d.get("hwid_free_resets"), int) and isinstance(d.get("hwid_resets_used"), int):
        return max(0, d["hwid_free_resets"] - d["hwid_resets_used"])
    return None


_MODE_LABEL = {"free": "free reset", "paid": "staff credit used", "deduct": "1 day deducted"}


def _deduct_days(d: dict) -> int:
    """`penalty_days` is the wire name, `deduct_days` the contract's. Accept both
    so this renders correctly against an old or a new Lambda."""
    for k in ("deduct_days", "penalty_days"):
        try:
            n = int(d.get(k) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            return n
    return 1


def _day_word(n: int) -> str:
    return "day" if n == 1 else "days"


def render_hwid_reset(d: dict) -> tuple:
    """-> (text, offer_deduct: bool). The caller attaches HwidDeductView when
    offer_deduct is True (server said payment_required — contract §3 step 5).

    Owner rule 2026-09-15: three free resets per key, then ONE day off the
    subscription per reset. Customers can no longer buy a reset, so no reply
    here renders a store link."""
    if d.get("ok"):
        mode = str(d.get("mode") or "free")
        lines = [f"✅ Reset done (**{_MODE_LABEL.get(mode, mode)}**). Activate on your new PC with the same key."]
        rem = _free_resets_remaining(d)
        if rem is not None:
            lines.append(f"Free resets remaining: **{rem}**")
        if isinstance(d.get("hwid_paid_credits"), int) and d["hwid_paid_credits"] > 0:
            lines.append(f"Staff reset credits: **{d['hwid_paid_credits']}**")
        if mode == "deduct":
            days = _deduct_days(d)
            lines.append(f"**{days} {_day_word(days)}** {'was' if days == 1 else 'were'} "
                         f"deducted from your subscription.")
        if "expiry" in d or d.get("lifetime"):
            lines.append(f"Expires: {_fmt_expiry(d.get('expiry'), bool(d.get('lifetime')))}")
        elif d.get("message"):
            lines.append(str(d["message"]))
        return "\n".join(lines), False
    err = d.get("error")
    if err == "cooldown":
        ra = d.get("retry_at")
        when = f"<t:{int(ra)}:R> (<t:{int(ra)}:f>)" if ra else "later"
        return f"⏳ You reset recently. Try again {when}, or open a ticket if it's urgent.", False
    if err == "locked":
        return ("🔒 Self-service resets are locked on your license. Open a ticket in **#create-ticket** "
                "and staff will sort it out."), False
    if err == "payment_required":
        days = _deduct_days(d)
        return ("⚠️ You've used all 3 free PC resets on this key.\n"
                f"The next reset takes **{days} {_day_word(days)} off your subscription**.\n"
                "_Nothing happens until you press a button below._"), True
    if err == "trial_no_deduct":
        return ("❌ You've used all 3 free PC resets on your trial key. A trial has no subscription "
                "to take a day from — subscribe to keep resetting, or open a ticket."), False
    if err == "insufficient_time":
        days = _deduct_days(d)
        return (f"❌ Less than {days} {_day_word(days)} left on your subscription, so there's nothing "
                "to deduct. Renew it, or open a ticket."), False
    if err == "paid_only":
        return ("❌ This key has no expiry to take days from — open a ticket and staff will "
                "reset it for you."), False
    return str(d.get("message") or "❌ Couldn't reset right now — try again or open a ticket."), False


class HwidDeductView(discord.ui.View):
    """Confirm step for the `deduct` mode: one danger button re-calls
    /api/bot/hwid-reset with mode="deduct", plus Cancel. No "buy a reset" link —
    customers cannot buy one any more (owner rule 2026-09-15). Bound to the
    invoking user."""

    def __init__(self, user_id: int, penalty_days: int = 1):
        super().__init__(timeout=300)
        self.user_id = user_id
        penalty_days = max(1, int(penalty_days or 1))
        self.penalty_days = penalty_days
        d = f"{penalty_days} {_day_word(penalty_days)}"
        self.deduct = discord.ui.Button(label=f"Deduct {d} and reset",
                                        style=discord.ButtonStyle.danger, custom_id="hwid_deduct")
        self.deduct.callback = self._deduct
        self.add_item(self.deduct)
        self.cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary,
                                        custom_id="hwid_cancel")
        self.cancel.callback = self._cancel
        self.add_item(self.cancel)

    def _mine(self, interaction) -> bool:
        return getattr(getattr(interaction, "user", None), "id", None) == self.user_id

    async def _deduct(self, interaction: discord.Interaction):
        if not self._mine(interaction):
            await interaction.response.send_message("That button isn't yours.", ephemeral=True)
            return
        await interaction.response.defer()          # ack the click; the reset may take >3 s
        _, data = await lambda_post(ROUTE_HWID_RESET, hwid_reset_payload(self.user_id, mode="deduct"))
        text, _ = render_hwid_reset(data)
        self.stop()
        await interaction.edit_original_response(content=text, view=None)

    async def _cancel(self, interaction: discord.Interaction):
        if not self._mine(interaction):
            await interaction.response.send_message("That button isn't yours.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing was deducted.", view=None)


def entitlement_embed(d: dict, target_id) -> discord.Embed:
    """Render a /api/bot/status reply (entitlement by Discord id; never a key)."""
    e = discord.Embed(title="🔎 License lookup", color=EMBED_COLOR)
    e.add_field(name="User", value=f"<@{target_id}> (`{target_id}`)", inline=False)
    if not d.get("has_license"):
        e.add_field(name="License", value="none linked to this Discord account", inline=False)
        return e
    e.add_field(name="Status", value="✅ Active" if d.get("active") else "⌛ Expired / inactive", inline=True)
    e.add_field(name="Plan", value=str(d.get("plan") or "unknown"), inline=True)
    e.add_field(name="Expires", value=_fmt_expiry(d.get("expiry"), bool(d.get("lifetime"))), inline=False)
    e.add_field(name="Machine", value="🔒 bound to a PC" if d.get("bound") else "not activated yet", inline=True)
    if d.get("lookup_mode"):
        e.set_footer(text=f"lookup_mode: {d['lookup_mode']}")
    return e


def key_embed(title, key, tier, extra=""):
    e = discord.Embed(
        title=title,
        description=(f"**Tier:** {tier}\n\n**Key**\n```\n{key}\n```\n{extra}"
                      "Activate it in the Venice launcher. Keep it private — sharing or reselling voids it."),
        color=EMBED_COLOR)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Venice • Precision Shot-Timing")
    return e


# ── FAQ content (source of truth: docs/gtm/faq.md — keep in sync) ─────────────

FAQ_TOPICS = {
    "capture_card": {
        "label": "Capture card not detected",
        "emoji": "🎥",
        "title": "🎥 My capture card isn't detected",
        "description": (
            "**Venice can't see your capture card? Work down this list — it's almost always #1–#3.**\n\n"
            "**1. Close everything else that uses the card.** OBS, Discord video, the card's own viewer app, "
            "browser tabs with camera access — capture cards allow **one** app at a time. Close them all, then restart Venice.\n\n"
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
            "• **Wire everything.** Ethernet to the PS5 and Ethernet to the PC is the setup Venice is built for. "
            "Wi-Fi on either end adds jitter that no software can time through. If you must use Wi-Fi: 5 GHz / Wi-Fi 6, "
            "same room as the router, nothing else streaming.\n"
            "• Drop the Remote Play stream quality one notch — a stable 720p stream times better than a stuttering 1080p one.\n"
            "• Close downloads, cloud sync, and anyone else's Netflix on the network.\n\n"
            "**Be straight with yourself here:** Venice reads your stream and is exactly as fast as that stream. "
            "A clean wired link = clean greens. A congested Wi-Fi link = late reads, and no setting can fix physics. "
            "This is also why we say it **before** you buy."
        ),
    },
    "no_greens": {
        "label": "No greens / overlay not reading",
        "emoji": "🟩",
        "title": "🟩 No greens / the overlay isn't reading the meter",
        "description": (
            "**Venice runs but shots come out early, late, or the overlay never locks onto the meter? Check these in order:**\n\n"
            "**1. Is the stream actually visible in Venice?** Open the Live Capture panel — you should see your gameplay. "
            "If it's black, that's the real problem: reconnect (Disconnect → Connect Chiaki), make sure Chiaki shows the game "
            "and not its setup screen, and keep the Venice window visible (don't minimize it).\n\n"
            "**2. Hold, don't tap.** Venice releases for you — **hold** Square (tempo), right-stick-down (stick tempo), "
            "or right-stick-up (go-to) and let go of nothing. If you release manually, you're fighting the tool.\n\n"
            "**3. Controller in the right place?** It must be plugged into the **PC**. If your pad is paired to the PS5 directly, "
            "Venice can see the meter but can't act on it.\n\n"
            "**4. Meter visible in-game?** Shot meter ON in 2K's settings, and use a meter style/size Venice has calibrated "
            "against (defaults work best). A hidden or exotic meter gives the vision system nothing to read.\n\n"
            "**5. Let calibration finish.** The first 10–15 shots in practice teach Venice your jumper. "
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
            "2. The bot unbinds your membership from the old machine.\n"
            "3. On the new PC: install Venice, open https://zaeorion.com/connect, sign in to the same Discord account, and use the one-time code.\n\n"
            "**Limits (anti-sharing, not anti-you):**\n"
            "• A cooldown applies between self-service resets.\n"
            "• The first **3 PC resets are free**. After that, staff can grant a reset credit or you can confirm a one-day subscription deduction when eligible.\n\n"
            "Rebuilt Windows, swapped major hardware, or RMA'd the machine? Same command — a reinstall on the same box "
            "sometimes reads as a \"new\" machine and one reset fixes it.\n\n"
            "Legitimately need more than that (multiple hardware failures, stolen laptop)? Open a ticket with a short "
            "explanation — staff can reset it manually. What we **don't** do is resets that let two people share one key; "
            "that pattern gets the key revoked."
        ),
    },
    "missing_key": {
        "label": "Paid but access is missing",
        "emoji": "🔑",
        "title": "🔑 I paid but cannot connect",
        "description": (
            "The Venice website links your purchase to the Discord account you connect at checkout.\n\n"
            "1. Confirm you connected the **same Discord account** you use here.\n"
            "2. Run `/status`, then open https://zaeorion.com/connect on the PC running Venice.\n"
            "3. Check your DMs for the subscription confirmation; delivery may take a minute.\n"
            "4. Still missing? Open a private ticket in **#create-ticket**. Include the checkout email and approximate time, "
            "but never post payment details or a one-time code in a public channel."
        ),
    },
    "quick_answers": {
        "label": "Quick answers (trial, pricing, expiry…)",
        "emoji": "❓",
        "title": "❓ Quick answers",
        "description": (
            "• **Is there a free trial?** Yes — `/claim_trial` in the server. Full features, no card. "
            "One per Discord account and per PC. Then open https://zaeorion.com/connect to sign in; "
            "your public Discord ID is not an activation code.\n"
            "• **What does it cost?** A free 7-day trial, then one **$25/month** recurring membership.\n"
            "• **Can I use it on two PCs at once?** No — single-user, one machine. Use `/hwid_reset` to move it.\n"
            "• **When does my license expire?** Run `/status` — it checks your linked Discord account privately.\n"
            "• **What does Venice actually do to my game?** Nothing. It watches your screen and times a controller input — "
            "it never touches game files, game memory, or your console. See the \"What Venice is\" post in #faq."
        ),
    },
}


def faq_embed(topic: str) -> discord.Embed:
    t = FAQ_TOPICS[topic]
    e = discord.Embed(title=t["title"], description=t["description"], color=EMBED_COLOR)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Venice • Precision Shot-Timing — still stuck? Open a ticket in #create-ticket")
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
        state = "⌛ Expired — renew your membership at zaeorion.com"
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

    e = discord.Embed(title=f"🔑 Your Venice License (…{lic.get('license_key_suffix', '????')})",
                      color=EMBED_COLOR)
    e.add_field(name="Status",  value=state, inline=False)
    e.add_field(name="Plan",    value=(lic.get("plan") or "?").capitalize(), inline=True)
    e.add_field(name="Expires", value=expires_txt, inline=True)
    e.add_field(name="Machine", value=machine_txt, inline=False)
    e.add_field(name="Activations", value=str(lic.get("activations", 0)), inline=True)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Venice • Precision Shot-Timing — new PC? /hwid_reset moves it yourself")
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


class PurchaseView(discord.ui.View):
    """One trusted path from Discord to the Venice storefront."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Open secure checkout",
            emoji="🛒",
            style=discord.ButtonStyle.link,
            url=STORE_URL,
        ))


@client.event
async def on_ready():
    try:
        synced = await tree.sync(guild=GUILD)
        print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    except Exception as e:
        print("command sync error:", e)
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.playing, name="Venice | Trial + membership"))
    await backend_selfcheck()
    print(f"{client.user} is online")


@tree.command(name="purchase", description="Buy Venice securely — $25 per month", guild=GUILD)
async def purchase(interaction: discord.Interaction):
    e = discord.Embed(
        title="🛒 Venice — $25 / month",
        description=(
            "One recurring monthly plan at **zaeorion.com**, paid by card through Stripe's secure checkout. "
            "Cancel anytime.\n\n"
            "Connect the **same Discord account** you use in this server on the website; your membership is tied to its Discord ID. After payment, Triton "
            "DMs a subscription confirmation and connection link, then grants Customer access. Your Discord ID alone cannot unlock the launcher.\n\n"
            "Questions or delivery problems? Open a ticket in **#create-ticket**."
        ),
        color=EMBED_COLOR,
    )
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Venice • Secure checkout • Connect with Discord")
    await interaction.response.send_message(embed=e, view=PurchaseView(), ephemeral=True)


@tree.command(name="claim_trial", description="Claim your free 7-day Venice trial", guild=GUILD)
async def claim_trial(interaction: discord.Interaction):
    # The backend DMs the Discord connection link and grants Trial server-side.
    # It never returns the private database key through a bot interaction.
    # This command is now a thin relay of the backend's message, and it rolls the
    # claim back itself on a DM bounce, so there's nothing to do here but report.
    await interaction.response.defer(ephemeral=True)
    uid = interaction.user.id
    _, data = await lambda_post(ROUTE_TRIAL, {"discord_id": uid, "actor_discord_id": uid})
    prefix = "✅ " if data.get("ok") else "❌ "
    await interaction.followup.send(
        prefix + data.get("message", "Could not create a trial right now."), ephemeral=True)


@tree.command(name="hwid_reset", description="Unbind your Venice license so you can activate on a new PC", guild=GUILD)
async def hwid_reset(interaction: discord.Interaction):
    # Contract §3: free (3) -> staff credit -> deduct 1 day (needs an explicit
    # confirm, the button below) with cooldown / locked / trial_no_deduct /
    # insufficient_time / paid_only.
    await interaction.response.defer(ephemeral=True)
    uid = interaction.user.id
    _, data = await lambda_post(ROUTE_HWID_RESET, hwid_reset_payload(uid))
    text, offer_deduct = render_hwid_reset(data)
    if offer_deduct:
        view = HwidDeductView(uid, _deduct_days(data))
        await interaction.followup.send(text, view=view, ephemeral=True)
    else:
        await interaction.followup.send(text, ephemeral=True)


@tree.command(name="status", description="Check the Venice membership linked to your Discord account", guild=GUILD)
async def status(interaction: discord.Interaction):
    # The invoking Discord account is the identity; customers never enter a key.
    await interaction.response.defer(ephemeral=True)
    uid = interaction.user.id
    st, data = await lambda_post(ROUTE_STATUS, {"discord_id": uid, "actor_discord_id": uid})
    if st == 404:
        await interaction.followup.send(
            "Membership status is temporarily unavailable. Try again shortly or open a private ticket.",
            ephemeral=True)
        return
    if st == 0:
        await interaction.followup.send("❌ Backend unreachable — try again in a minute or open a ticket.", ephemeral=True)
        return
    if not data.get("ok"):
        await interaction.followup.send("❌ Membership lookup failed. Open a ticket in **#create-ticket**.", ephemeral=True)
        return
    if not data.get("has_license"):
        await interaction.followup.send("No membership is linked to your Discord account yet. Try `/claim_trial` or visit the Venice website.", ephemeral=True)
        return
    e = discord.Embed(title="Your Venice membership", color=EMBED_COLOR)
    e.add_field(name="Status", value="Active" if data.get("active") else "Expired", inline=True)
    e.add_field(name="Plan", value=str(data.get("plan") or "Unknown").capitalize(), inline=True)
    e.add_field(name="Expires", value=_fmt_expiry(data.get("expiry"), bool(data.get("lifetime"))), inline=False)
    e.add_field(name="PC", value="Bound" if data.get("bound") else "Not bound", inline=True)
    resets = data.get("resets") or {}
    e.add_field(name="Free PC resets", value=str(resets.get("free_remaining", "Unknown")), inline=True)
    e.set_footer(text="Venice • Linked to your Discord ID • Need help? Open a ticket")
    await interaction.followup.send(embed=e, ephemeral=True)


@tree.command(name="setup", description="Troubleshoot the common setup failures — pick your symptom", guild=GUILD)
async def setup(interaction: discord.Interaction):
    e = discord.Embed(
        title="🛠 Venice Setup Troubleshooter",
        description=("Pick the symptom below and I'll post the fix.\n\n"
                     "🎥 **Capture card not detected** — Venice can't see your card / black screen\n"
                     "🎮 **Remote Play won't pair / laggy** — pairing loops, PIN timeouts, stutter\n"
                     "🟩 **No greens / overlay not reading** — Venice runs but shots come out early or late\n\n"
                     "Something else? `/faq` covers licenses, new PCs, and delivery — "
                     "or open a ticket in **#create-ticket**."),
        color=EMBED_COLOR)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="Venice • Precision Shot-Timing")
    await interaction.response.send_message(embed=e, view=SetupView(), ephemeral=True)


@tree.command(name="faq", description="Get an answer from the Venice FAQ", guild=GUILD)
@app_commands.describe(topic="What do you need help with?",
                       public="Post it in the channel so others can see it (default: only you)")
@app_commands.choices(topic=[
    app_commands.Choice(name=f'{t["emoji"]} {t["label"]}', value=key)
    for key, t in FAQ_TOPICS.items()
])
async def faq(interaction: discord.Interaction, topic: str, public: bool = False):
    await interaction.response.send_message(embed=faq_embed(topic), ephemeral=not public)


# Owner rule 2026-09-15: `month` is the only plan sold. /deliver therefore offers
# month alone (use `days` for a custom length); /keygen keeps `lifetime` so staff
# can still mint a comp — it is advertised nowhere. `day`/`week` are gone from both
# pickers; the backend still ACCEPTS them, so keys already sold are unaffected.
DELIVER_PLAN_CHOICES = [
    app_commands.Choice(name="month (30 days)", value="month"),
]
KEYGEN_PLAN_CHOICES = [
    app_commands.Choice(name="month (30 days)", value="month"),
    app_commands.Choice(name="lifetime (staff comp — never sold)", value="lifetime"),
]
PLAN_CHOICES = KEYGEN_PLAN_CHOICES          # back-compat alias


@tree.command(name="deliver", description="STAFF: mint a license and DM it to a user (audited)", guild=GUILD)
# Discord-side VISIBILITY only (mirrors discord_commands.json's
# default_member_permissions "0"): hides the command from ordinary members until
# the owner grants the Staff role access in Server Settings → Integrations. It is
# NOT the authorization gate — the server's orion-staff check is (contract §4).
@app_commands.default_permissions()
@app_commands.describe(user="recipient", plan="license plan (month is the only one sold)",
                       reason="why (goes in the audit log, max 200 chars)",
                       days="override length in days (optional)")
@app_commands.choices(plan=DELIVER_PLAN_CHOICES)
async def deliver(interaction: discord.Interaction, user: discord.User, plan: str, reason: str, days: int = 0):
    # No Discord ADMINISTRATOR-bit check any more (contract §4 BREAKING): the
    # server resolves `actor_discord_id` against orion-staff (role admin+) and
    # answers `forbidden` for anyone else — rendered below.
    reason, bad = valid_reason(reason)
    if bad:
        await interaction.response.send_message(bad, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    st, data = await lambda_post(ROUTE_DELIVER, deliver_payload(interaction.user.id, user.id, plan, reason, days))
    if not data.get("ok"):
        if is_staff_refusal(st, data):
            await interaction.followup.send(staff_refusal_text(st, data), ephemeral=True)
            return
        await interaction.followup.send(f"❌ {data.get('message') or data.get('error') or 'error'}", ephemeral=True)
        return
    keys = keys_from(data)
    key = keys[0] if keys else ""
    if not key:
        await interaction.followup.send("❌ Backend returned ok but no key — check the lambda (contract drift).",
                                        ephemeral=True)
        return
    try:
        await user.send(embed=key_embed("🔑 Your Venice License", key, plan.capitalize()))
    except discord.Forbidden:
        await interaction.followup.send(f"⚠️ Minted `{key}` but couldn't DM {user.mention} (DMs closed) — deliver manually.", ephemeral=True)
        return
    try:
        member = interaction.guild.get_member(user.id) or await interaction.guild.fetch_member(user.id)
        if CUSTOMER_ROLE_ID:
            await member.add_roles(discord.Object(id=CUSTOMER_ROLE_ID))
        # LEGACY: lifetime is not sold any more (owner rule 2026-09-15). The role
        # is still applied when staff hand out a comp, and is a no-op when the
        # owner leaves LIFETIME_ROLE_ID unset.
        if plan.lower() == "lifetime" and LIFETIME_ROLE_ID:
            await member.add_roles(discord.Object(id=LIFETIME_ROLE_ID))
    except Exception:
        pass
    await interaction.followup.send(f"✅ Delivered **{plan}** to {user.mention}.", ephemeral=True)


@tree.command(name="keygen", description="STAFF (admin+): mint unassigned license keys (audited)", guild=GUILD)
@app_commands.default_permissions()      # visibility only — see /deliver
@app_commands.describe(plan="license plan (month is the only one sold)",
                       reason="why (goes in the audit log, max 200 chars)",
                       count=f"how many keys (1-{MAX_KEYGEN_COUNT})",
                       days="override length in days (optional)",
                       note="optional note stored with the keys")
@app_commands.choices(plan=KEYGEN_PLAN_CHOICES)
async def keygen(interaction: discord.Interaction, plan: str, reason: str,
                 count: int = 1, days: int = 0, note: str = ""):
    """Unassigned keys (no recipient) — the staff member hands them out. Gated by
    the SERVER: `actor_discord_id` is resolved against orion-staff and
    license.create is admin+ (contract §1). No Discord permission bit is read."""
    reason, bad = valid_reason(reason)
    if bad:
        await interaction.response.send_message(bad, ephemeral=True)
        return
    count = max(1, int(count or 1))
    if count > MAX_KEYGEN_COUNT:
        await interaction.response.send_message(
            f"❌ `count` must be ≤ {MAX_KEYGEN_COUNT} (contract §2).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    st, data = await lambda_post(
        ROUTE_DELIVER, keygen_payload(interaction.user.id, plan, reason, count, days, note))
    if not data.get("ok"):
        if is_staff_refusal(st, data):
            await interaction.followup.send(staff_refusal_text(st, data), ephemeral=True)
            return
        await interaction.followup.send(
            "❌ " + str(data.get("message") or data.get("error") or "Could not mint."), ephemeral=True)
        return
    keys = keys_from(data)
    if not keys:
        await interaction.followup.send(
            "❌ Server returned ok but no key — contract drift; tell the owner.", ephemeral=True)
        return
    lines = [f"✅ Minted **{len(keys)}** × **{data.get('plan') or plan}** "
             f"key{'' if len(keys) == 1 else 's'} (reason: {reason})."]
    if len(keys) < count:
        lines.append(f"⚠️ You asked for {count}; the server returned {len(keys)} "
                     "(`count` not supported yet — run again for more).")
    if "expiry" in data:
        lines.append(f"Expires: {_fmt_expiry(data.get('expiry'), (data.get('plan') or plan) == 'lifetime')}")
    lines.append("```\n" + "\n".join(keys) + "\n```")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="lookup", description="Look up a license — your own, or (staff) another user's / a key", guild=GUILD)
@app_commands.describe(user="Discord user to look up (staff only)",
                       key="license key (staff only) — XXXX-XXXX-XXXX-XXXX")
async def lookup(interaction: discord.Interaction, user: discord.User = None, key: str = ""):
    """No server-side bot-lookup route exists (see the report's gap list), so:
      • by Discord user  -> /api/bot/status semantics (entitlement, never a key)
      • by license key   -> the EXISTING staff route /api/staff/license action=lookup,
                            which needs the bot's staff bearer token.
    Looking up anyone but yourself is gated client-side by STAFF_ROLE_IDS — the
    only gate available until the server checks an actor on these reads."""
    await interaction.response.defer(ephemeral=True)
    actor = interaction.user.id

    if key:
        if not has_staff_role(interaction):
            await interaction.followup.send(
                "❌ Key lookups are staff-only. Run `/status` for your own membership instead.", ephemeral=True)
            return
        st, data = await staff_post(ROUTE_STAFF_LIC, staff_lookup_payload(actor, key))
        if st == -1:
            await interaction.followup.send(
                "❌ Key lookup isn't configured — the bot has no staff identity "
                "(set ORION_STAFF_ID / ORION_STAFF_MACHINE_ID). Use OrionStaff.exe.", ephemeral=True)
            return
        if st == 0:
            await interaction.followup.send("❌ Backend unreachable — try again in a minute.", ephemeral=True)
            return
        if st == 404 or data.get("error") == "invalid_key":
            await interaction.followup.send("❌ That key isn't in our system (keys never contain `O`, `I`, `0`, `1`).", ephemeral=True)
            return
        if is_staff_refusal(st, data):
            await interaction.followup.send(staff_refusal_text(st, data), ephemeral=True)
            return
        lic = data.get("license") if data.get("ok") else None
        if not lic:
            await interaction.followup.send(
                f"❌ {data.get('message') or data.get('error') or 'Lookup failed.'}", ephemeral=True)
            return
        await interaction.followup.send(embed=status_embed(lic), ephemeral=True)
        return

    target = user.id if user is not None else actor
    if target != actor and not has_staff_role(interaction):
        await interaction.followup.send(
            "❌ Looking up other users is staff-only. Run `/lookup` with no options for your own license.",
            ephemeral=True)
        return
    st, data = await lambda_post(ROUTE_STATUS, {"discord_id": target, "actor_discord_id": actor})
    if not data.get("ok"):
        if is_staff_refusal(st, data):
            await interaction.followup.send(staff_refusal_text(st, data), ephemeral=True)
            return
        await interaction.followup.send(
            f"❌ {data.get('message') or data.get('error') or 'Lookup failed.'}", ephemeral=True)
        return
    await interaction.followup.send(embed=entitlement_embed(data, target), ephemeral=True)


if __name__ == "__main__":
    client.run(BOT_TOKEN)
