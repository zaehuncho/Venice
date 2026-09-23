/**
 * Venice License Bot — Cloudflare Worker (Discord HTTP interactions).
 *
 * Verifies Discord's Ed25519 signature, then proxies each slash command to the
 * Lambda's /api/bot/* endpoints. The LAMBDA does all license logic + DMs +
 * roles + staff-role resolution. This Worker holds NO license logic and NO
 * staff list: it forwards the invoking user's id as `actor_discord_id` on
 * every call and the server decides (docs/ADMIN_PANEL_V2_CONTRACT.md §1, §4).
 * The launcher killswitch is NOT reachable here — it's owner-console-only
 * (OrionOwner.exe / orion-admin CLI). /api/bot/killswitch is status-only.
 *
 * Branding note: the product is customer-facing "Venice" as of 2026-08-04. The
 * orion-* Gumroad slugs, SSM paths and AWS resource names are INTERNAL join keys
 * — renaming them breaks the live purchase→provision path. Don't "align" them.
 *
 * Worker SECRETS  (wrangler secret put ...):
 *   DISCORD_PUBLIC_KEY   app public key, hex (Dev Portal -> General Information)
 *   DISCORD_APP_ID       application id (for the deferred follow-up)
 *   ORION_BOT_SECRET     MUST equal SSM /orion/bot_service_secret
 *   ORION_EDGE_AUTH      MUST equal SSM /orion/edge_auth_secret
 * Worker VARS  (wrangler.toml [vars]):
 *   ORION_API_BASE            direct execute-api origin (bypasses Cloudflare bot-fight)
 *   STORE_URL                 the website /purchase sends everyone to. Owner rule
 *                             2026-09-15: one product line (free 7-day trial, then
 *                             $19.99/month recurring), so /purchase shows NO tiers and
 *                             NO prices — one embed, one button, one destination.
 *   GUMROAD_BASE              (legacy/fallback) storefront base, e.g.
 *                             https://<seller>.gumroad.com/l — used only when
 *                             STORE_URL is unset, so a deploy that predates the
 *                             STORE_URL var still shows a working button.
 *   STAFF_ROLE_IDS            (optional) comma-separated Discord role ids. Client-side
 *                             gate for /lookup of OTHER users only — /api/bot/status has
 *                             no server-side staff check yet (see report). /deliver and
 *                             /keygen are gated by the SERVER (orion-staff role), never here.
 *
 * WHY ORION_EDGE_AUTH exists: backend/lambda_function.py's router calls
 * require_edge_auth() on EVERY route — the HIGH-3/HIGH-4 hardening explicitly
 * removed the old /api/bot/* exemption ("the webhooks/worker present the edge
 * secret on their calls"). Both gates apply: edge secret at the router, bot
 * secret inside each handler.
 *
 * KNOWN AMBIGUITY: the router's edge-auth refusal and a handler's staff-role
 * refusal are BOTH `HTTP 403 {"ok":false,"error":"forbidden"}`. Staff commands
 * therefore render one message covering both cases. Ask the backend for a
 * distinct code (e.g. `not_staff`) if that ever bites.
 *
 * Command registration: this Worker only HANDLES interactions. The command
 * definitions (names, options, choices) live in discord_commands.json and are
 * pushed with register_commands.py — the live Lambda has no register-commands
 * route. Re-run it whenever the JSON changes (new options are not auto-synced).
 *
 * NOT MIRRORED from orion_bot.py: /status /setup /faq live only in the gateway
 * bot. /purchase here covers the entitlement-display half of /status using the
 * bot-secret-gated /api/bot/status, which returns no license key.
 *
 * KEYS ON THIS PATH: customer routes deliberately never return a key here
 * (/api/bot/trial DMs it server-side; /api/bot/status has none). /deliver and
 * /keygen are the exception — /api/bot/deliver mints and RETURNS the key, and
 * this Worker holds no bot token so it cannot DM. The key is therefore rendered
 * into the staff member's EPHEMERAL reply. Owner call: if a key must never
 * transit the Worker at all, run /deliver and /keygen from the gateway bot
 * (orion_bot.py DMs the recipient directly) and drop them from
 * discord_commands.json.
 */

const DEFAULT_API = "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com";
const BRAND_COLOR = 2450411;         // #2563EB
const MAX_REASON = 200;              // contract: every mutation carries reason ≤ 200 chars
const MAX_KEYGEN_COUNT = 25;         // contract §2: count ≤ 25 per call

// There are no tiers any more (owner rule 2026-09-15): a free 7-day trial, then
// one recurring monthly subscription, and the price lives on the website — never
// in this file, so it can change without a Worker deploy. The old tier array and
// its unfilled price templates are gone with it. The webhook's PRODUCT_MAP keeps the
// legacy slugs (1day/7day/30day/120day/lifetime/beta) so keys already sold still
// resolve on refund pings; none of them are offered here.
const SUBSCRIBE_LABEL = "Subscribe on the website";

// Server routes this Worker depends on (all POST, bot secret + edge auth).
// Keep this list in sync with the report / backend cross-check.
const ROUTES = {
  status:    "/api/bot/status",      // {discord_id, actor_discord_id}
  hwidReset: "/api/bot/hwid-reset",  // {discord_id, actor_discord_id, mode?}   contract §3
  trial:     "/api/bot/trial",       // {discord_id, actor_discord_id}
  deliver:   "/api/bot/deliver",     // {discord_id?, plan, days?, count?, reason, actor_discord_id}  contract §4
};

// Error codes the server uses to refuse a staff action (contract §1/§4). The
// bare "forbidden" is shared with the edge-auth gate — see the header comment.
const STAFF_REFUSAL_CODES = new Set(["forbidden", "not_staff", "staff_required", "capability_denied", "actor_required"]);

const COMMAND_NAMES = ["purchase", "claim_trial", "hwid_reset", "deliver", "keygen", "lookup"];

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

// Real Ed25519 verification via Web Crypto. Tries the standard name then the
// legacy "NODE-ED25519" so it works across Worker runtime versions.
async function verifySignature(env, signature, timestamp, rawBody) {
  if (!signature || !timestamp) return false;
  const raw = hexToBytes(env.DISCORD_PUBLIC_KEY);
  const sig = hexToBytes(signature);
  const data = new TextEncoder().encode(timestamp + rawBody);
  for (const algo of ["Ed25519", "NODE-ED25519"]) {
    try {
      const params = algo === "Ed25519" ? { name: "Ed25519" } : { name: "NODE-ED25519", namedCurve: "NODE-ED25519" };
      const key = await crypto.subtle.importKey("raw", raw, params, false, ["verify"]);
      return await crypto.subtle.verify(params.name === "NODE-ED25519" ? { name: "NODE-ED25519" } : "Ed25519", key, sig, data);
    } catch (e) { /* try the next algorithm name */ }
  }
  return false;
}

/**
 * POST to the Lambda. Always resolves to an object with `ok` (bool) and, on
 * failure, `error` (code) + `message` (human) + `http_status`. Structured
 * fields the handler sent alongside an error (retry_at, penalty_days,
 * deduct_days…) are preserved whether it answered 200 or 4xx, so callers can
 * branch on `error` without caring about the HTTP status.
 */
async function callLambda(env, path, payload) {
  const base = env.ORION_API_BASE || DEFAULT_API;
  const headers = {
    "Content-Type": "application/json",
    "X-Orion-Bot-Secret": env.ORION_BOT_SECRET,
  };
  // Router-level gate; without it every /api/bot/* call returns 403.
  if (env.ORION_EDGE_AUTH) headers["X-Edge-Auth"] = env.ORION_EDGE_AUTH;
  try {
    const r = await fetch(base + path, { method: "POST", headers, body: JSON.stringify(payload) });
    let body;
    try { body = await r.json(); } catch (e) { body = {}; }
    if (body === null || typeof body !== "object") body = {};
    if (!r.ok) {
      console.log(`[lambda] ${path} -> HTTP ${r.status} ${JSON.stringify(body).slice(0, 200)}`);
      const out = Object.assign({}, body, { ok: false, http_status: r.status, error: body.error || `http_${r.status}` });
      if (!out.message) {
        // Distinguish a misconfiguration from a genuine backend fault, so a
        // missing/rotated edge secret doesn't masquerade as "backend down".
        out.message = r.status === 403
          ? "Authorization failed reaching the license service. Staff: check ORION_EDGE_AUTH / ORION_BOT_SECRET."
          : `License service error (${r.status}).`;
      }
      return out;
    }
    if (typeof body.ok !== "boolean") body.ok = true;
    body.http_status = r.status;
    return body;
  } catch (e) {
    console.log(`[lambda] ${path} -> fetch failed: ${e.message || e}`);
    return { ok: false, error: "unreachable", http_status: 0, message: "Backend unreachable — try again or open a ticket." };
  }
}

function userId(interaction) {
  return String((interaction.member?.user || interaction.user || {}).id || "");
}

function memberRoleIds(interaction) {
  return (interaction.member?.roles || []).map(String);
}

// Client-side gate used ONLY for /lookup of other users (read-only route with
// no server-side actor check yet). Not an auth boundary for mutations.
function hasStaffRole(env, interaction) {
  const ids = String(env.STAFF_ROLE_IDS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!ids.length) return false;
  const mine = new Set(memberRoleIds(interaction));
  return ids.some((id) => mine.has(id));
}

function isStaffRefusal(d) {
  return !d.ok && (STAFF_REFUSAL_CODES.has(d.error) || d.http_status === 403);
}

function staffRefusalMessage(d) {
  // Server-side staff resolution (orion-staff, role admin+) replaced the old
  // Discord ADMINISTRATOR-bit check. The message covers the edge-auth 403 too
  // because the two are indistinguishable on the wire (see header).
  const base = "❌ Refused: this action is limited to staff (admin+) on the license server's staff list.";
  const hint = d.http_status === 403
    ? " If you ARE staff, the bot's ORION_EDGE_AUTH / ORION_BOT_SECRET may be misconfigured — tell the owner."
    : "";
  return base + hint + (d.message && d.error !== "forbidden" ? `\n(${d.message})` : "");
}

function isHttpUrl(u) {
  try {
    const p = new URL(String(u));
    return (p.protocol === "https:" || p.protocol === "http:") && !/[<>{}]/.test(String(u));
  } catch (e) { return false; }
}

function validReason(raw) {
  const reason = String(raw ?? "").trim();
  if (!reason) return { error: "❌ A `reason` is required (it goes in the audit log)." };
  if (reason.length > MAX_REASON) return { error: `❌ \`reason\` must be ≤ ${MAX_REASON} characters.` };
  return { reason };
}

function fmtExpiry(status) {
  if (!status.has_license) return null;
  if (status.lifetime) return "never — Lifetime";
  if (!status.expiry) return "unknown";
  // Discord renders <t:unix:R> client-side in the reader's own timezone.
  return `<t:${status.expiry}:D> (<t:${status.expiry}:R>)`;
}

function fmtExpiryValue(expiry, lifetime) {
  if (lifetime || expiry === 0) return "never — Lifetime";
  if (!expiry) return "unknown";
  return `<t:${expiry}:D> (<t:${expiry}:R>)`;
}

// Link buttons (style 5) carry no custom_id and generate no follow-up
// interaction. Every URL is validated first: Discord rejects the whole message
// edit on a malformed URL (e.g. the "<seller>" placeholder), which would leave
// the user staring at "thinking…" forever.
// The single destination /purchase sends people to. STORE_URL is the website;
// GUMROAD_BASE is only a fallback so a Worker deployed before STORE_URL existed
// still produces a working button instead of none.
function storeUrl(env) {
  const primary = String(env.STORE_URL || "").trim().replace(/\/+$/, "");
  if (isHttpUrl(primary)) return primary;
  const fallback = String(env.GUMROAD_BASE || "").trim().replace(/\/+$/, "");
  return isHttpUrl(fallback) ? fallback : "";
}

function linkButton(label, url) {
  return isHttpUrl(url) ? { type: 2, style: 5, label, url: String(url) } : null;
}

// ONE ephemeral embed + ONE link button (owner rule 2026-09-15). No tiers, no
// prices — the website is the only place a price is written down.
async function cmdPurchase(env, interaction) {
  const uid = userId(interaction);
  const status = await callLambda(env, ROUTES.status, { discord_id: uid, actor_discord_id: uid });

  const embed = {
    title: "Venice — Precision Shot-Timing",
    color: BRAND_COLOR,
    description:
      "Start with the **free 7-day trial** (`/claim_trial`), then subscribe on the " +
      "website to keep going. Your license key is **DM'd to you automatically** the " +
      "moment payment clears, and the 💎 Customer role is added.",
    footer: { text: "Keys arrive by DM — make sure your DMs are open." },
  };

  if (status.has_license) {
    embed.fields = [{
      name: status.active ? "✅ You already have a license" : "⚠️ Your license has expired",
      value:
        `Plan: **${status.plan || "unknown"}**\n` +
        `Expires: ${fmtExpiry(status)}\n` +
        `Bound to a PC: ${status.bound ? "yes — `/hwid_reset` to move it" : "no"}`,
    }];
  } else if (status.ok === false) {
    // Entitlement lookup failed; still show the button rather than dead-ending.
    embed.fields = [{ name: "Note", value: "Couldn't check your existing licence just now — the button below still works." }];
  }

  const btn = linkButton(SUBSCRIBE_LABEL, storeUrl(env));
  if (!btn) {
    embed.fields = [...(embed.fields || []), {
      name: "⚠️ Store link unavailable",
      value: "STORE_URL isn't configured on the bot. Staff: set it in wrangler.toml.",
    }];
    return { embeds: [embed], components: [] };
  }
  return { embeds: [embed], components: [{ type: 1, components: [btn] }] };
}

// ── /hwid_reset (contract §3) ────────────────────────────────────────────────

function hwidResetPayload(uid, mode) {
  // `discord_id` is what today's Lambda reads; `actor_discord_id` is the
  // contract's name. Send both so the command works across the rollout.
  const p = { discord_id: uid, actor_discord_id: uid };
  if (mode) p.mode = mode;
  return p;
}

function freeResetsRemaining(d) {
  for (const k of ["resets_remaining", "free_resets_remaining", "hwid_free_remaining"]) {
    if (typeof d[k] === "number") return d[k];
  }
  if (typeof d.hwid_free_resets === "number" && typeof d.hwid_resets_used === "number") {
    return Math.max(0, d.hwid_free_resets - d.hwid_resets_used);
  }
  return null;
}

const MODE_LABEL = { free: "free reset", paid: "staff credit used", deduct: "1 day deducted" };

// `penalty_days` is the wire name; `deduct_days` is the contract's. Accept both so
// this renders correctly whether the Lambda in front of it is old or new.
function deductDays(d) {
  const n = Number(d.deduct_days ?? d.penalty_days);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function dayWord(n) {
  return n === 1 ? "day" : "days";
}

/**
 * Turn a /api/bot/hwid-reset reply into an interaction message. `components`
 * is always present so a follow-up edit clears any earlier confirm buttons.
 *
 * Owner rule 2026-09-15: customers cannot buy a reset any more, so NOTHING here
 * renders a store link. Three free resets, then one day off the subscription.
 */
function renderHwidReset(env, d, uid) {
  if (d.ok) {
    const mode = String(d.mode || "free");
    const lines = [`✅ Reset done (**${MODE_LABEL[mode] || mode}**). Activate on your new PC with the same key.`];
    const remaining = freeResetsRemaining(d);
    if (remaining !== null) lines.push(`Free resets remaining: **${remaining}**`);
    if (typeof d.hwid_paid_credits === "number" && d.hwid_paid_credits > 0) {
      lines.push(`Staff reset credits: **${d.hwid_paid_credits}**`);
    }
    if (mode === "deduct") {
      const days = deductDays(d);
      lines.push(`**${days} ${dayWord(days)}** ${days === 1 ? "was" : "were"} deducted from your subscription.`);
    }
    if (d.expiry !== undefined || d.lifetime) lines.push(`Expires: ${fmtExpiryValue(d.expiry, d.lifetime)}`);
    if (!lines.some((l) => l.startsWith("Expires")) && d.message) lines.push(d.message);
    return { content: lines.join("\n"), components: [] };
  }
  switch (d.error) {
    case "cooldown": {
      const when = d.retry_at ? `<t:${d.retry_at}:R> (<t:${d.retry_at}:f>)` : "later";
      return { content: `⏳ You reset recently. Try again ${when}, or open a ticket if it's urgent.`, components: [] };
    }
    case "locked":
      return { content: "🔒 Self-service resets are locked on your license. Open a ticket in **#create-ticket** and staff will sort it out.", components: [] };
    case "payment_required": {
      const days = deductDays(d);
      const content =
        "⚠️ You've used all 3 free PC resets on this key.\n" +
        `The next reset takes **${days} ${dayWord(days)} off your subscription**.\n` +
        "_Nothing happens until you press a button below._";
      return { content, components: [{ type: 1, components: [
        { type: 2, style: 4, label: `Deduct ${days} ${dayWord(days)} and reset`, custom_id: `hwid_deduct:${uid}` },
        { type: 2, style: 2, label: "Cancel", custom_id: `hwid_cancel:${uid}` },
      ] }] };
    }
    case "trial_no_deduct":
      return { content: "❌ You've used all 3 free PC resets on your trial key. A trial has no subscription " +
                        "to take a day from — subscribe with `/purchase` to keep resetting, or open a ticket.",
               components: [] };
    case "insufficient_time": {
      const days = deductDays(d);
      return { content: `❌ Less than ${days} ${dayWord(days)} left on your subscription, so there's nothing to deduct. ` +
                        "Renew with `/purchase`, or open a ticket.", components: [] };
    }
    case "paid_only":
      return { content: "❌ This key has no expiry to take days from — open a ticket and staff will reset it for you.",
               components: [] };
    default:
      return { content: d.message || "❌ Couldn't reset right now — try again or open a ticket.", components: [] };
  }
}

async function cmdHwidReset(env, interaction, mode) {
  const uid = userId(interaction);
  const d = await callLambda(env, ROUTES.hwidReset, hwidResetPayload(uid, mode));
  return renderHwidReset(env, d, uid);
}

// ── /deliver + /keygen (contract §4: actor forwarded, server resolves the staff role) ──

function deliverPayload(actorId, targetId, plan, days, reason) {
  const p = { discord_id: String(targetId), plan: String(plan || "").toLowerCase(), reason, actor_discord_id: actorId };
  const n = Number(days);
  if (Number.isFinite(n) && n > 0) p.days = Math.floor(n);
  return p;
}

// ASSUMED payload extension: `count` on /api/bot/deliver (the contract defines
// count on /api/staff/license create, which needs a bearer token the bot does
// not hold). If the server ignores it, exactly one key comes back and the
// reply says so.
function keygenPayload(actorId, plan, days, count, reason, note) {
  const p = { plan: String(plan || "").toLowerCase(), count, reason, actor_discord_id: actorId };
  const n = Number(days);
  if (Number.isFinite(n) && n > 0) p.days = Math.floor(n);
  if (note) p.note = String(note).slice(0, MAX_REASON);
  return p;
}

function keysFrom(d) {
  if (Array.isArray(d.keys)) return d.keys.map(String);
  if (Array.isArray(d.license_keys)) return d.license_keys.map(String);
  return d.license_key ? [String(d.license_key)] : [];
}

async function cmdDeliver(env, interaction, opts) {
  const uid = userId(interaction);
  if (!opts.user) return { content: "❌ Pick a recipient (`user`)." };
  const v = validReason(opts.reason);
  if (v.error) return { content: v.error };
  const d = await callLambda(env, ROUTES.deliver, deliverPayload(uid, opts.user, opts.plan, opts.days, v.reason));
  if (!d.ok) {
    if (isStaffRefusal(d)) return { content: staffRefusalMessage(d) };
    return { content: `❌ ${d.message || d.error || "Couldn't deliver."}` };
  }
  const keys = keysFrom(d);
  const lines = [`✅ ${d.message || "Delivered."} Recipient: <@${opts.user}>, plan **${d.plan || opts.plan}**.`];
  if (d.expiry !== undefined) lines.push(`Expires: ${fmtExpiryValue(d.expiry, d.plan === "lifetime")}`);
  // The Lambda mints and returns the key; the gateway bot DMs it itself, but
  // this Worker has no bot token, so hand it to the staff member (ephemeral).
  if (keys.length) lines.push("Key (ephemeral — DM it to them if the server didn't):\n```\n" + keys.join("\n") + "\n```");
  return { content: lines.join("\n") };
}

async function cmdKeygen(env, interaction, opts) {
  const uid = userId(interaction);
  const v = validReason(opts.reason);
  if (v.error) return { content: v.error };
  let count = Math.floor(Number(opts.count) || 1);
  if (count < 1) count = 1;
  if (count > MAX_KEYGEN_COUNT) return { content: `❌ \`count\` must be ≤ ${MAX_KEYGEN_COUNT}.` };
  const d = await callLambda(env, ROUTES.deliver, keygenPayload(uid, opts.plan, opts.days, count, v.reason, opts.note));
  if (!d.ok) {
    if (isStaffRefusal(d)) return { content: staffRefusalMessage(d) };
    return { content: `❌ ${d.message || d.error || "Couldn't mint."}` };
  }
  const keys = keysFrom(d);
  if (!keys.length) return { content: "❌ Server returned ok but no key — contract drift; tell the owner." };
  const lines = [`✅ Minted **${keys.length}** × **${d.plan || opts.plan}** key${keys.length === 1 ? "" : "s"} (reason: ${v.reason}).`];
  if (keys.length < count) lines.push(`⚠️ You asked for ${count}; the server returned ${keys.length} (count not supported yet — run again for more).`);
  if (d.expiry !== undefined) lines.push(`Expires: ${fmtExpiryValue(d.expiry, (d.plan || opts.plan) === "lifetime")}`);
  lines.push("```\n" + keys.join("\n") + "\n```");
  return { content: lines.join("\n") };
}

// ── /lookup ──────────────────────────────────────────────────────────────────
// No /api/bot/lookup exists in the contract, so this runs on /api/bot/status
// semantics (entitlement by Discord id, no key). Key lookups need a staff
// bearer token this Worker doesn't hold — use OrionStaff.exe / the gateway bot.

function statusEmbed(d, targetId) {
  const embed = { title: "🔎 License lookup", color: BRAND_COLOR, fields: [{ name: "User", value: `<@${targetId}> (\`${targetId}\`)`, inline: false }] };
  if (!d.has_license) {
    embed.fields.push({ name: "License", value: "none linked to this Discord account", inline: false });
    return embed;
  }
  embed.fields.push(
    { name: "Status", value: d.active ? "✅ Active" : "⌛ Expired / inactive", inline: true },
    { name: "Plan", value: String(d.plan || "unknown"), inline: true },
    { name: "Expires", value: fmtExpiry(d) || "unknown", inline: false },
    { name: "Machine", value: d.bound ? "🔒 bound to a PC" : "not activated yet", inline: true },
  );
  if (d.lookup_mode) embed.footer = { text: `lookup_mode: ${d.lookup_mode}` };
  return embed;
}

async function cmdLookup(env, interaction, opts) {
  const uid = userId(interaction);
  if (opts.key) {
    return { content: "ℹ️ Key lookups aren't available through this bot (no bot-secret key-lookup route). " +
                      "Use OrionStaff.exe, or `/lookup user:@someone` here." };
  }
  const target = String(opts.user || uid);
  if (target !== uid && !hasStaffRole(env, interaction)) {
    return { content: "❌ Looking up other users is staff-only (STAFF_ROLE_IDS). You can run `/lookup` with no options for your own license." };
  }
  const d = await callLambda(env, ROUTES.status, { discord_id: target, actor_discord_id: uid });
  if (!d.ok) {
    if (isStaffRefusal(d)) return { content: staffRefusalMessage(d) };
    return { content: `❌ ${d.message || d.error || "Lookup failed."}` };
  }
  return { embeds: [statusEmbed(d, target)] };
}

// ── dispatch ─────────────────────────────────────────────────────────────────

async function runCommand(env, interaction) {
  const name = interaction.data?.name;
  const uid = userId(interaction);
  const opts = {};
  (interaction.data?.options || []).forEach((o) => { opts[o.name] = o.value; });

  if (name === "purchase") return await cmdPurchase(env, interaction);

  // Accept both spellings: the registered command is hwid_reset, but hyphens are
  // legal in Discord command names and users reach for /hwid-reset.
  if (name === "hwid_reset" || name === "hwid-reset") return await cmdHwidReset(env, interaction, undefined);
  if (name === "claim_trial") {
    const d = await callLambda(env, ROUTES.trial, { discord_id: uid, actor_discord_id: uid });
    return { content: d.message || (d.ok ? "Trial sent to your DMs!" : "Couldn't create a trial.") };
  }
  if (name === "deliver") return await cmdDeliver(env, interaction, opts);
  if (name === "keygen") return await cmdKeygen(env, interaction, opts);
  if (name === "lookup") return await cmdLookup(env, interaction, opts);
  return { content: "Unknown command." };
}

/**
 * MESSAGE_COMPONENT (type 3) — only the /hwid_reset confirm buttons exist.
 * Returns {response, work?}: `response` is sent inside Discord's 3 s window,
 * `work` (if any) runs afterwards and its result is PATCHed onto the message
 * the button lives on.
 */
function handleComponent(env, interaction) {
  const cid = String(interaction.data?.custom_id || "");
  const uid = userId(interaction);
  const [kind, owner] = cid.split(":");
  if (owner && owner !== uid) {
    return { response: { type: 4, data: { content: "That button isn't yours.", flags: 64 } } };
  }
  if (kind === "hwid_cancel") {
    return { response: { type: 7, data: { content: "Cancelled — nothing was deducted.", components: [] } } };
  }
  if (kind === "hwid_deduct") {
    return {
      response: { type: 6 },                                             // DEFERRED_UPDATE_MESSAGE
      work: async () => {
        const payload = await cmdHwidReset(env, interaction, "deduct");  // contract §3 step 5
        if (!payload.components) payload.components = [];
        return payload;
      },
    };
  }
  return { response: { type: 4, data: { content: "Unknown button.", flags: 64 } } };
}

async function followup(env, token, payload) {
  await fetch(`https://discord.com/api/v10/webhooks/${env.DISCORD_APP_ID}/${token}/messages/@original`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export {
  runCommand, handleComponent, callLambda, renderHwidReset, cmdPurchase, storeUrl,
  hasStaffRole, hwidResetPayload, deliverPayload, keygenPayload, validReason,
  isHttpUrl, deductDays, ROUTES, COMMAND_NAMES, SUBSCRIBE_LABEL,
};

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("Venice License Bot");
    const rawBody = await request.text();
    const valid = await verifySignature(
      env, request.headers.get("X-Signature-Ed25519"),
      request.headers.get("X-Signature-Timestamp"), rawBody);
    if (!valid) return new Response("invalid request signature", { status: 401 });

    const interaction = JSON.parse(rawBody);
    if (interaction.type === 1) return Response.json({ type: 1 });          // PING -> PONG
    if (interaction.type === 2) {                                           // slash command
      // Defer (ephemeral) inside Discord's 3s window, then do the work + edit the reply.
      ctx.waitUntil((async () => {
        let payload;
        try { payload = await runCommand(env, interaction); }
        catch (e) { payload = { content: "❌ Error: " + (e.message || e) }; }
        await followup(env, interaction.token, payload);
      })());
      return Response.json({ type: 5, data: { flags: 64 } });               // DEFERRED, ephemeral
    }
    if (interaction.type === 3) {                                           // button press
      const { response, work } = handleComponent(env, interaction);
      if (work) {
        ctx.waitUntil((async () => {
          let payload;
          try { payload = await work(); }
          catch (e) { payload = { content: "❌ Error: " + (e.message || e), components: [] }; }
          await followup(env, interaction.token, payload);
        })());
      }
      return Response.json(response);
    }
    return new Response("ok");
  },
};
