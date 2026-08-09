"""Venice — Discord guild audit / restructure tool.

Reads the whole guild (roles, categories, channels, permission overwrites) in a
handful of API calls, which the Discord *client* cannot give you: its sidebar is
virtualised, so a browser walk only ever sees what's on screen and reading an
overwrite means opening each channel's settings by hand.

Subcommands
  audit   read-only. Dumps guild.json and prints a human summary + a gap report
          against the blueprint. Changes nothing.
  plan    show the exact mutations `apply` would perform, in order.
  apply   perform them. Requires --yes. Channel DELETES additionally require
          --allow-delete, because deleting a Discord channel destroys its
          message history irreversibly.

Auth: bot token via VENICE_BOT_TOKEN (read from SSM /orion/discord_bot_token by
the caller — this file never touches AWS and never prints the token).

The bot needs Manage Roles + Manage Channels, and its own role must sit ABOVE
any role it edits or grants — Discord refuses role writes at or above the
actor's highest role, which is the single most common cause of a silent 403 here.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
TOKEN = os.environ.get("VENICE_BOT_TOKEN", "").strip()

# ── permission bits (only the ones this tool sets) ────────────────────────────
VIEW_CHANNEL       = 1 << 10
SEND_MESSAGES      = 1 << 11
ADD_REACTIONS      = 1 << 6
SPEAK              = 1 << 21
CONNECT            = 1 << 20
READ_HISTORY       = 1 << 16
USE_APP_COMMANDS   = 1 << 31
CREATE_INVITE      = 1 << 0
MENTION_EVERYONE   = 1 << 17
MANAGE_MESSAGES    = 1 << 13
ATTACH_FILES       = 1 << 15
EMBED_LINKS        = 1 << 14
SEND_IN_THREADS    = 1 << 38

OVERWRITE_ROLE = 0

CH_TEXT     = 0
CH_VOICE    = 2
CH_CATEGORY = 4

# ── the target structure (SERVER_BLUEPRINT.md, Venice rebrand) ────────────────
# gate:  public   = everyone can see, only staff/bots post
#        commands = as public, but everyone may use slash commands + buttons
#        verified = Verified role and above
#        customer = Customer role and above
#        staff    = Staff role and above
BLUEPRINT = [
    ("📋 INFORMATION", "public", [
        "welcome", "announcements", "rules", "terms-of-service",
        "faq", "status", "how-to-buy"]),
    ("🛒 STORE", "public", [
        ("pricing", "public"), ("purchase", "commands"),
        ("reviews", "public"), ("vouch-format", "public")]),
    ("🎫 SUPPORT", "public", [
        ("create-ticket", "commands"), ("support-info", "public")]),
    ("💬 COMMUNITY", "verified", [
        "general", "2k-discussion", "clips", "off-topic", "bot-commands"]),
    ("⭐ CUSTOMER", "customer", [
        "customer-lounge", "downloads", "setup-guide", "changelog",
        "priority-support"]),
    ("🔒 STAFF", "staff", [
        "staff-chat", "mod-log", "ticket-log", "sales-log"]),
]

ROLE_SPEC = [
    # name,           colour,   hoist, why
    ("👑 Owner",     0x2563EB, True,  "administrator — you only"),
    ("🛡️ Admin",     0x3B82F6, True,  "manage server/roles/channels"),
    ("🔧 Staff",     0x60A5FA, True,  "moderation + staff channels"),
    ("⭐ Lifetime",  0xFACC15, True,  "customer access + flex colour"),
    ("💎 Customer",  0x22C55E, True,  "granted on purchase"),
    ("🎁 Trial",     0x6366F1, False, "granted by /claim_trial"),
    ("✅ Verified",  0x94A3B8, False, "granted after verification"),
    ("🔇 Muted",     0x475569, False, "send denied everywhere"),
]


def api(method, path, body=None, _retries=5):
    """One Discord REST call, with 429 handling. Discord's rate limits are
    per-route and bursty during a restructure, so honour Retry-After rather than
    hammering — a 429 storm can get the token temporarily blocked."""
    if not TOKEN:
        sys.exit("VENICE_BOT_TOKEN is unset")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bot " + TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "VeniceGuildTool (internal, v1)")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        if e.code == 429 and _retries > 0:
            try:
                wait = float(json.loads(payload).get("retry_after", 1.0))
            except Exception:
                wait = 1.0
            time.sleep(wait + 0.25)
            return api(method, path, body, _retries - 1)
        raise RuntimeError(f"{method} {path} -> {e.code} {payload[:300]}")


def fetch(guild_id):
    return {
        "guild": api("GET", f"/guilds/{guild_id}"),
        "roles": api("GET", f"/guilds/{guild_id}/roles"),
        "channels": api("GET", f"/guilds/{guild_id}/channels"),
    }


def summarise(snap):
    g, roles, chans = snap["guild"], snap["roles"], snap["channels"]
    everyone = next((r for r in roles if r["name"] == "@everyone"), None)

    print(f"guild   : {g['name']}  ({g['id']})")
    print(f"owner   : {g.get('owner_id')}")
    print(f"2FA req : {'yes' if g.get('mfa_level') else 'NO — enable for moderation'}")
    print(f"verify  : level {g.get('verification_level')} (0=none 1=low 2=med 3=high 4=highest)")
    if everyone:
        p = int(everyone["permissions"])
        risky = [n for n, bit in (
            ("MENTION_EVERYONE", MENTION_EVERYONE), ("MANAGE_MESSAGES", MANAGE_MESSAGES),
            ("CREATE_INVITE", CREATE_INVITE)) if p & bit]
        print(f"@everyone risky perms: {', '.join(risky) if risky else 'none'}")

    print(f"\nroles ({len(roles)}) — highest first:")
    for r in sorted(roles, key=lambda r: -r["position"]):
        flags = []
        if r.get("managed"):
            flags.append("bot/integration")
        if r.get("hoist"):
            flags.append("hoisted")
        if int(r["permissions"]) & 0x8:
            flags.append("ADMINISTRATOR")
        print(f"  pos {r['position']:>3}  {r['name']:<28} {r['id']:<20} "
              f"{'· '.join(flags)}")

    cats = {c["id"]: c for c in chans if c["type"] == CH_CATEGORY}
    print(f"\nchannels ({len(chans)} total, {len(cats)} categories):")
    for cat in sorted(cats.values(), key=lambda c: c["position"]):
        kids = sorted([c for c in chans if c.get("parent_id") == cat["id"]],
                      key=lambda c: c["position"])
        print(f"\n  [{cat['position']:>2}] {cat['name']}   ({len(kids)} channels)")
        for ov in cat.get("permission_overwrites", []):
            if ov["type"] == OVERWRITE_ROLE:
                print(f"         overwrite role {ov['id']}: "
                      f"allow={ov['allow']} deny={ov['deny']}")
        for c in kids:
            kind = {CH_TEXT: "#", CH_VOICE: "🔊"}.get(c["type"], "?")
            own = " [own-perms]" if c.get("permission_overwrites") else ""
            print(f"         {kind}{c['name']}{own}")
    orphans = [c for c in chans if not c.get("parent_id") and c["type"] != CH_CATEGORY]
    if orphans:
        print(f"\n  (no category): {', '.join(c['name'] for c in orphans)}")


def gap_report(snap):
    have_roles = {r["name"].strip().lower() for r in snap["roles"]}
    have_chans = {c["name"].strip().lower() for c in snap["channels"]}

    print("\n" + "=" * 66)
    print("GAP REPORT vs blueprint")
    print("=" * 66)

    missing_roles = [n for n, _, _, _ in ROLE_SPEC
                     if n.strip().lower() not in have_roles
                     and n.split(" ", 1)[-1].lower() not in have_roles]
    print(f"\nroles missing ({len(missing_roles)}):")
    for n in missing_roles:
        print(f"  - {n}")

    want_chans, want_cats = [], []
    for cat, _gate, kids in BLUEPRINT:
        want_cats.append(cat)
        for k in kids:
            want_chans.append(k[0] if isinstance(k, tuple) else k)
    missing_c = [c for c in want_chans if c not in have_chans]
    print(f"\nchannels missing ({len(missing_c)}):")
    for c in missing_c:
        print(f"  - #{c}")

    extra_cats = [c["name"] for c in snap["channels"]
                  if c["type"] == CH_CATEGORY
                  and c["name"] not in want_cats]
    print(f"\ncategories not in blueprint ({len(extra_cats)}) "
          f"— consolidation candidates:")
    for c in extra_cats:
        print(f"  - {c}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["audit", "plan", "apply"])
    ap.add_argument("--guild", required=True)
    ap.add_argument("--out", default="guild.json")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--allow-delete", action="store_true")
    a = ap.parse_args()

    if a.cmd == "audit":
        snap = fetch(a.guild)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, ensure_ascii=False)
        summarise(snap)
        gap_report(snap)
        print(f"\nfull snapshot -> {a.out}")
    else:
        sys.exit(f"'{a.cmd}' not implemented yet — run audit first and review it")


if __name__ == "__main__":
    main()
