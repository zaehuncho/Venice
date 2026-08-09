/**
 * Venice License Bot — Cloudflare Worker (Discord HTTP interactions).
 *
 * Verifies Discord's Ed25519 signature, then proxies each slash command to the
 * Lambda's /api/bot/* endpoints. The LAMBDA does all license logic + DMs +
 * roles. This Worker holds NO license logic. The launcher killswitch is NOT
 * reachable here — it's owner-console-only (OrionOwner.exe / orion-admin CLI,
 * /api/admin/kill|unkill|status).
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
 *   ORION_API_BASE       direct execute-api origin (bypasses Cloudflare bot-fight)
 *   GUMROAD_BASE         storefront base, e.g. https://<seller>.gumroad.com/l
 *
 * WHY ORION_EDGE_AUTH exists: backend/lambda_function.py's router calls
 * require_edge_auth() on EVERY route — the HIGH-3/HIGH-4 hardening explicitly
 * removed the old /api/bot/* exemption ("the webhooks/worker present the edge
 * secret on their calls"). The Gumroad webhook was updated to match; this Worker
 * was not, so every command 403'd before reaching require_bot(). Both gates now
 * apply: edge secret at the router, bot secret inside each handler.
 *
 * NOT MIRRORED from orion_bot.py: /status /setup /faq live only in the gateway
 * bot — they need button components and, for /status, a cached staff-login
 * session. /purchase below covers the entitlement-display half of /status using
 * the new bot-secret-gated /api/bot/status, which returns no license key.
 */

const DEFAULT_API = "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com";
const BRAND_COLOR = 2450411;         // #2563EB
const MAX_ROW = 5;                   // Discord: 5 buttons per action row

// Sellable tiers, in display order. `slug` MUST match a PRODUCT_MAP key in
// discord_launch/gumroad_webhook/lambda_function.py — that string is how a
// Gumroad Ping resolves to a plan. `price` is display-only; keep it in sync with
// the real Gumroad price by hand (Gumroad's price API needs an access token, not
// worth a live call for a value that changes ~never).
// Only these two are sold (owner decision 2026-08-04). The other PRODUCT_MAP
// entries (1day/3day/14day/120day/lifetime/beta) stay in the webhook's map on
// purpose — previously-sold keys must still resolve to a plan on refund pings —
// they're just not offered here.
const TIERS = [
  { slug: "orion-7day",  label: "7 Days", price: "{{PRICE_7DAY}}" },
  { slug: "orion-30day", label: "1 Month", price: "{{PRICE_30DAY}}" },
];

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
    if (!r.ok) {
      // Distinguish the misconfiguration from a genuine backend fault, so a
      // missing/rotated edge secret doesn't masquerade as "backend down".
      console.log(`[lambda] ${path} -> HTTP ${r.status} ${JSON.stringify(body).slice(0, 200)}`);
      if (r.status === 403) {
        return { ok: false, message: "Authorization failed reaching the license service. Staff: check ORION_EDGE_AUTH / ORION_BOT_SECRET." };
      }
      return { ok: false, message: body.message || `License service error (${r.status}).` };
    }
    return body;
  } catch (e) {
    console.log(`[lambda] ${path} -> fetch failed: ${e.message || e}`);
    return { ok: false, message: "Backend unreachable — try again or open a ticket." };
  }
}

function isAdmin(interaction) {
  try { return (BigInt(interaction.member?.permissions || "0") & 8n) === 8n; }  // ADMINISTRATOR
  catch (e) { return false; }
}

function fmtExpiry(status) {
  if (!status.has_license) return null;
  if (status.lifetime) return "never — Lifetime";
  if (!status.expiry) return "unknown";
  // Discord renders <t:unix:R> client-side in the reader's own timezone.
  return `<t:${status.expiry}:D> (<t:${status.expiry}:R>)`;
}

// Link buttons (style 5) carry no custom_id and generate no follow-up
// interaction, so the Worker stays stateless — no MESSAGE_COMPONENT handling.
function buyButtons(env) {
  const base = (env.GUMROAD_BASE || "").replace(/\/+$/, "");
  if (!base) return [];
  const rows = [];
  for (let i = 0; i < TIERS.length; i += MAX_ROW) {
    rows.push({
      type: 1,
      components: TIERS.slice(i, i + MAX_ROW).map((t) => ({
        type: 2, style: 5, label: t.label, url: `${base}/${t.slug}`,
      })),
    });
  }
  return rows;
}

async function cmdPurchase(env, interaction) {
  const uid = (interaction.member?.user || interaction.user || {}).id;
  const status = await callLambda(env, "/api/bot/status", { discord_id: uid });

  const lines = TIERS.map((t) => `**${t.label}** — ${t.price}`).join("\n");
  const embed = {
    title: "Venice — Precision Shot-Timing",
    color: BRAND_COLOR,
    description:
      "Pick a tier below. Checkout is handled by Gumroad; your license key is " +
      "**DM'd to you automatically** the moment payment clears, and the 💎 Customer " +
      "role is added.\n\n" + lines,
    footer: { text: "Keys arrive by DM — make sure your DMs are open." },
  };

  if (status.has_license) {
    const expiry = fmtExpiry(status);
    embed.fields = [{
      name: status.active ? "✅ You already have a license" : "⚠️ Your license has expired",
      value:
        `Plan: **${status.plan || "unknown"}**\n` +
        `Expires: ${expiry}\n` +
        `Bound to a PC: ${status.bound ? "yes — `/hwid_reset` to move it" : "no"}` +
        (status.active ? "\n\nBuying again **extends** nothing automatically — open a ticket to stack time." : ""),
    }];
  } else if (status.ok === false) {
    // Entitlement lookup failed; still show the tiers rather than dead-ending.
    embed.fields = [{ name: "Note", value: "Couldn't check your existing license just now — the buy links below still work." }];
  }

  const components = buyButtons(env);
  if (!components.length) {
    embed.fields = [...(embed.fields || []), {
      name: "⚠️ Store links unavailable",
      value: "GUMROAD_BASE isn't configured on the bot. Staff: set it in wrangler.toml.",
    }];
  }
  return { embeds: [embed], components };
}

async function runCommand(env, interaction) {
  const name = interaction.data?.name;
  const uid = (interaction.member?.user || interaction.user || {}).id;
  const opts = {};
  (interaction.data?.options || []).forEach((o) => { opts[o.name] = o.value; });

  if (name === "purchase") return await cmdPurchase(env, interaction);

  // Accept both spellings: the registered command is hwid_reset, but hyphens are
  // legal in Discord command names and users reach for /hwid-reset.
  if (name === "hwid_reset" || name === "hwid-reset") {
    const d = await callLambda(env, "/api/bot/hwid-reset", { discord_id: uid });
    return { content: d.message || (d.ok ? "Done." : "Couldn't reset right now.") };
  }
  if (name === "claim_trial") {
    const d = await callLambda(env, "/api/bot/trial", { discord_id: uid });
    return { content: d.message || (d.ok ? "Trial sent to your DMs!" : "Couldn't create a trial.") };
  }
  if (name === "deliver") {
    if (!isAdmin(interaction)) return { content: "❌ Admin only." };
    const d = await callLambda(env, "/api/bot/deliver", { discord_id: String(opts.user), plan: String(opts.plan || "").toLowerCase(), days: opts.days || 0 });
    return { content: d.message || (d.ok ? "Delivered." : "Couldn't deliver.") };
  }
  return { content: "Unknown command." };
}

async function followup(env, token, payload) {
  await fetch(`https://discord.com/api/v10/webhooks/${env.DISCORD_APP_ID}/${token}/messages/@original`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

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
    return new Response("ok");
  },
};
