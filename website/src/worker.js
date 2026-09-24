const SESSION_COOKIE = "venice_session";
const OAUTH_COOKIE = "venice_oauth";
// [2026-09-22 RED TEAM GMC-001] Set by /checkout/complete once the Worker has confirmed (with the
// Stripe call it already makes) that this Discord account's checkout is complete. It only changes
// the MESSAGE /connect shows while the webhook is still provisioning; it never grants access.
const PAID_COOKIE = "venice_paid";
const PAID_COOKIE_SECONDS = 1800;
const ACTIVATING_RETRY_SECONDS = 5;
const ACTIVATING_MAX_ATTEMPTS = 24; // 24 x 5 s = 2 minutes
const MAX_BODY_BYTES = 1_000_000;
const STRIPE_TOLERANCE_SECONDS = 300;
// Pinned to the live webhook endpoint's API version so objects fetched here
// (subscriptions, checkout sessions) have the same shape as the events verified.
const STRIPE_API_VERSION = "2026-08-26.dahlia";
const DISCORD_ID_PATTERN = /^\d{16,22}$/u;
const ORION_GUILD_TIMEOUT_MS = 5000;
const SITE_ORIGIN = "https://zaeorion.com";
const JOIN_REQUIRED_MESSAGE = "Join the Venice Discord server first — Venice confirms your access and sends your setup steps there by DM.";
const MEMBERSHIP_UNAVAILABLE_MESSAGE = "We couldn't confirm your Discord membership. Please try again in a minute.";
// Site copy for each /api/trial outcome. The backend's own `message` is worded
// for Discord (slash commands, bold markdown) and is never shown on the site.
const TRIAL_OUTCOMES = {
  issued: { status: 200, message: "Your 7-day trial is active. Check your Discord DMs to connect Venice." },
  already_claimed: { status: 409, message: "You've already used your free trial. Subscribe to keep going." },
  dm_failed: {
    status: 422,
    message: "We couldn't DM you. In the Venice server, open Privacy Settings and allow Direct Messages, then try again.",
  },
  rate_limited: { status: 429, message: "Too many attempts. Wait a few minutes and try again." },
  unavailable: {
    status: 503,
    message: "We couldn't start your trial right now. Please try again later, or open a ticket in the Venice Discord.",
  },
};

const securityHeaders = {
  "Content-Security-Policy": "default-src 'self'; img-src 'self' data: https://cdn.discordapp.com; style-src 'self' 'unsafe-inline'; script-src 'self' https://js.stripe.com; connect-src 'self' https://api.stripe.com https://r.stripe.com; frame-src https://js.stripe.com https://hooks.stripe.com; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; upgrade-insecure-requests",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(self \"https://js.stripe.com\")",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
};

function withHeaders(response) {
  const next = new Response(response.body, response);
  for (const [name, value] of Object.entries(securityHeaders)) next.headers.set(name, value);
  if (next.headers.get("content-type")?.includes("text/html") && !next.headers.has("Cache-Control")) {
    next.headers.set("Cache-Control", "public, max-age=0, s-maxage=3600");
  }
  return next;
}

function json(data, status = 200) {
  return withHeaders(Response.json(data, {
    status,
    headers: { "Cache-Control": "no-store", "Content-Type": "application/json; charset=utf-8" },
  }));
}

function redirectTarget(raw, allowedHosts) {
  try {
    const target = new URL(String(raw || ""));
    if (target.protocol !== "https:" || !allowedHosts.has(target.hostname)) return null;
    return target.toString();
  } catch {
    return null;
  }
}

function safeReturnTo(raw) {
  const value = String(raw || "/#pricing");
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/#pricing";
  try {
    const parsed = new URL(value, "https://local.invalid");
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/#pricing";
  }
}

function parseCookies(request) {
  const result = {};
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const index = part.indexOf("=");
    if (index <= 0) continue;
    result[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return result;
}

function base64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function fromBase64url(value) {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const binary = atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "="));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function hmacKey(secret) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function signValue(payload, secret) {
  const encoded = base64url(new TextEncoder().encode(JSON.stringify(payload)));
  const signature = await crypto.subtle.sign("HMAC", await hmacKey(secret), new TextEncoder().encode(encoded));
  return `${encoded}.${base64url(new Uint8Array(signature))}`;
}

async function verifyValue(value, secret) {
  try {
    const [encoded, signature, extra] = String(value || "").split(".");
    if (!encoded || !signature || extra) return null;
    const valid = await crypto.subtle.verify(
      "HMAC",
      await hmacKey(secret),
      fromBase64url(signature),
      new TextEncoder().encode(encoded),
    );
    if (!valid) return null;
    const payload = JSON.parse(new TextDecoder().decode(fromBase64url(encoded)));
    if (!payload.exp || payload.exp < Math.floor(Date.now() / 1000)) return null;
    return payload;
  } catch {
    return null;
  }
}

function cookie(name, value, maxAge) {
  return `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

function clearCookie(name) {
  return `${name}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;
}

function oauthConfigured(env) {
  return Boolean(env.DISCORD_CLIENT_ID)
    && Boolean(env.DISCORD_CLIENT_SECRET)
    && Boolean(env.SESSION_SECRET);
}

function pairConfigured(env) {
  return oauthConfigured(env) && Boolean(env.ORION_API_BASE)
    && Boolean(env.PAIR_ISSUER_SECRET) && Boolean(env.ORION_EDGE_AUTH);
}

function checkoutConfigured(env) {
  const mode = env.STRIPE_MODE;
  return env.CHECKOUT_PROVIDER === "stripe"
    && (mode === "test" || mode === "live")
    && String(env.STRIPE_PUBLISHABLE_KEY || "").startsWith(`pk_${mode}_`)
    && (String(env.STRIPE_SECRET_KEY || "").startsWith(`sk_${mode}_`)
      || String(env.STRIPE_SECRET_KEY || "").startsWith(`rk_${mode}_`))
    && Boolean(env.STRIPE_PRICE_ID)
    && Boolean(env.STRIPE_WEBHOOK_SECRET)
    && oauthConfigured(env)
    && Boolean(env.ORION_API_BASE)
    && Boolean(env.ORION_BOT_SECRET)
    && Boolean(env.ORION_EDGE_AUTH);
}

function orionBotConfigured(env) {
  return Boolean(env.ORION_API_BASE) && Boolean(env.ORION_BOT_SECRET) && Boolean(env.ORION_EDGE_AUTH);
}

function trialConfigured(env) {
  return oauthConfigured(env) && orionBotConfigured(env);
}

function discordInviteUrl(env) {
  return redirectTarget(env.DISCORD_INVITE_URL, new Set(["discord.gg", "discord.com"])) || "";
}

// Logs carry only the last four digits of a Discord ID.
function redactId(value) {
  const id = String(value || "");
  return id ? `...${id.slice(-4)}` : "";
}

function safeCode(value) {
  const code = String(value || "");
  return /^[a-z_]{1,40}$/u.test(code) ? code : "";
}

async function readJson(response) {
  try {
    const data = await response.json();
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

async function currentSession(request, env) {
  if (!env.SESSION_SECRET) return null;
  const session = await verifyValue(parseCookies(request)[SESSION_COOKIE], env.SESSION_SECRET);
  // A paid-checkout hint shares the signing key; it must never stand in for a session.
  return session && session.paid === undefined ? session : null;
}

function canonicalOrigin(url) {
  return url.hostname === "www.zaeorion.com" ? "https://zaeorion.com" : url.origin;
}

async function beginDiscordAuth(request, env, url) {
  if (!oauthConfigured(env)) return json({ error: "Discord sign-in is not configured." }, 503);
  const state = base64url(crypto.getRandomValues(new Uint8Array(24)));
  const statePayload = await signValue({
    state,
    returnTo: safeReturnTo(url.searchParams.get("return_to")),
    exp: Math.floor(Date.now() / 1000) + 600,
  }, env.SESSION_SECRET);
  const callback = `${canonicalOrigin(url)}/auth/discord/callback`;
  const authorize = new URL("https://discord.com/oauth2/authorize");
  authorize.search = new URLSearchParams({
    client_id: env.DISCORD_CLIENT_ID,
    response_type: "code",
    redirect_uri: callback,
    scope: "identify guilds.join",
    state,
    prompt: "consent",
  }).toString();
  const response = new Response(null, { status: 302, headers: { Location: authorize.toString() } });
  response.headers.append("Set-Cookie", cookie(OAUTH_COOKIE, statePayload, 600));
  response.headers.set("Cache-Control", "no-store");
  return withHeaders(response);
}

async function finishDiscordAuth(request, env, url) {
  if (!oauthConfigured(env)) return json({ error: "Discord sign-in is not configured." }, 503);
  const oauth = await verifyValue(parseCookies(request)[OAUTH_COOKIE], env.SESSION_SECRET);
  const state = url.searchParams.get("state") || "";
  const code = url.searchParams.get("code") || "";
  if (!oauth || !code || oauth.state !== state) return json({ error: "Discord sign-in expired or was invalid." }, 400);

  const redirectUri = `${canonicalOrigin(url)}/auth/discord/callback`;
  const tokenResponse = await fetch("https://discord.com/api/v10/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: env.DISCORD_CLIENT_ID,
      client_secret: env.DISCORD_CLIENT_SECRET,
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri,
    }),
  });
  if (!tokenResponse.ok) return json({ error: "Discord sign-in could not be completed." }, 502);
  const token = await tokenResponse.json();
  const userResponse = await fetch("https://discord.com/api/v10/users/@me", {
    headers: { Authorization: `Bearer ${token.access_token}` },
  });
  if (!userResponse.ok) return json({ error: "Discord identity could not be read." }, 502);
  const user = await userResponse.json();
  if (!/^\d{16,22}$/u.test(String(user.id || ""))) return json({ error: "Discord returned an invalid identity." }, 502);

  // Best effort: a failed join never fails the sign-in. The access token goes to
  // the backend once and is not kept in the session cookie or any log line.
  const guildJoin = await joinVeniceGuild(env, String(user.id), token.access_token, token.scope);
  const session = await signValue({
    discordId: String(user.id),
    username: String(user.global_name || user.username || "Discord user").slice(0, 80),
    avatar: /^(?:a_)?[a-f0-9]{32}$/u.test(String(user.avatar || "")) ? String(user.avatar) : "",
    inGuild: guildJoin !== "failed",
    guildJoin,
    exp: Math.floor(Date.now() / 1000) + 3600,
  }, env.SESSION_SECRET);
  const response = new Response(null, {
    status: 302,
    headers: { Location: `${canonicalOrigin(url)}${safeReturnTo(oauth.returnTo)}` },
  });
  response.headers.append("Set-Cookie", cookie(SESSION_COOKIE, session, 3600));
  response.headers.append("Set-Cookie", clearCookie(OAUTH_COOKIE));
  response.headers.set("Cache-Control", "no-store");
  return withHeaders(response);
}

async function stripeRequest(env, path, options = {}) {
  const response = await fetch(`https://api.stripe.com${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.STRIPE_SECRET_KEY}`,
      "Stripe-Version": STRIPE_API_VERSION,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(`Stripe request failed (${response.status}): ${payload?.error?.type || "unknown"}`);
  return payload;
}

function betaCouponId(env) {
  const id = String(env.STRIPE_BETA_COUPON || "").trim();
  return /^[A-Za-z0-9_-]{1,64}$/u.test(id) ? id : "";
}

async function createCheckoutSession(request, env, url) {
  if (!checkoutConfigured(env)) return json({ error: "Checkout is not configured." }, 503);
  const session = await currentSession(request, env);
  if (!session?.discordId) return json({ error: "Connect Discord before checkout.", authUrl: "/auth/discord" }, 401);
  // Never take money we cannot deliver: access is confirmed by bot DM, which needs
  // a shared server. The check is live (the session's login-time flag can be stale)
  // and fails closed.
  const membership = await discordMembership(env, session.discordId, "checkout");
  if (membership !== "member") return membershipRefusal(env, membership);
  const returnUrl = `${canonicalOrigin(url)}/checkout/complete?session_id={CHECKOUT_SESSION_ID}`;
  const body = new URLSearchParams({
    mode: "subscription",
    ui_mode: "embedded_page",
    "line_items[0][price]": env.STRIPE_PRICE_ID,
    "line_items[0][quantity]": "1",
    // Cards only (covers Apple Pay / Google Pay). Delayed-settlement methods
    // complete Checkout as "unpaid" and would never be provisioned.
    "payment_method_types[0]": "card",
    client_reference_id: session.discordId,
    return_url: returnUrl,
    "metadata[discord_user_id]": session.discordId,
    "subscription_data[metadata][discord_user_id]": session.discordId,
    "subscription_data[metadata][source]": "zaeorion.com",
  });
  // Beta pricing (owner 2026-09-24): 25% off the $19.99 price for the first 3
  // months, via a Stripe coupon applied here so customers never type a code.
  // Unset STRIPE_BETA_COUPON to end the sale; existing discounts run out on
  // their own. A malformed id is ignored rather than sent to Stripe.
  const betaCoupon = betaCouponId(env);
  if (betaCoupon) body.set("discounts[0][coupon]", betaCoupon);
  try {
    const checkout = await stripeRequest(env, "/v1/checkout/sessions", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Idempotency-Key": `venice-${session.discordId}-${crypto.randomUUID()}`,
      },
      body,
    });
    if (!checkout.client_secret) return json({ error: "Stripe did not return a checkout session." }, 502);
    return json({ clientSecret: checkout.client_secret });
  } catch (error) {
    console.error(JSON.stringify({ event: "checkout_session_failed", message: String(error?.message || error) }));
    return json({ error: "Secure checkout is temporarily unavailable." }, 502);
  }
}

function parseStripeSignature(header) {
  const values = { t: "", v1: [] };
  for (const part of String(header || "").split(",")) {
    const [key, value] = part.split("=", 2);
    if (key === "t") values.t = value;
    if (key === "v1" && value) values.v1.push(value.toLowerCase());
  }
  return values;
}

function hex(bytes) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left, right) {
  if (left.length !== right.length) return false;
  let result = 0;
  for (let index = 0; index < left.length; index += 1) result |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return result === 0;
}

async function verifyStripeSignature(body, header, secret, now = Math.floor(Date.now() / 1000)) {
  const { t, v1 } = parseStripeSignature(header);
  const timestamp = Number(t);
  if (!Number.isInteger(timestamp) || Math.abs(now - timestamp) > STRIPE_TOLERANCE_SECONDS || v1.length === 0) return false;
  const signature = await crypto.subtle.sign(
    "HMAC",
    await hmacKey(secret),
    new TextEncoder().encode(`${t}.${body}`),
  );
  const expected = hex(new Uint8Array(signature));
  return v1.some((candidate) => constantTimeEqual(candidate, expected));
}

// Raw backend call: returns the Response whatever its status. Callers that must
// read a refusal body (trial, membership) use this; the rest use callOrion.
async function orionRequest(env, path, payload, { pairIssuer = false, timeoutMs = 0 } = {}) {
  const base = new URL(env.ORION_API_BASE);
  if (base.protocol !== "https:") throw new Error("ORION_API_BASE must use HTTPS");
  return fetch(new URL(path, base), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [pairIssuer ? "X-Orion-Pair-Secret" : "X-Orion-Bot-Secret"]:
        pairIssuer ? env.PAIR_ISSUER_SECRET : env.ORION_BOT_SECRET,
      "X-Edge-Auth": env.ORION_EDGE_AUTH,
    },
    body: JSON.stringify(payload),
    ...(timeoutMs ? { signal: AbortSignal.timeout(timeoutMs) } : {}),
  });
}

async function callOrion(env, path, payload, pairIssuer = false) {
  const response = await orionRequest(env, path, payload, { pairIssuer });
  if (!response.ok) throw new Error(`Orion backend returned ${response.status}`);
  return response;
}

// Live server-membership check: "member" | "not_member" | "unknown". One
// backend call, no retries; every caller treats "unknown" as not proven.
async function discordMembership(env, discordId, purpose) {
  if (!orionBotConfigured(env) || !DISCORD_ID_PATTERN.test(String(discordId || ""))) return "unknown";
  try {
    const response = await orionRequest(env, "/api/bot/guild-member", { discord_id: discordId, purpose },
      { timeoutMs: ORION_GUILD_TIMEOUT_MS });
    const data = await readJson(response);
    if (response.ok && data.ok === true && typeof data.member === "boolean") {
      return data.member ? "member" : "not_member";
    }
    console.warn(JSON.stringify({ event: "guild_member_check_failed", purpose, status: response.status,
      backendCode: safeCode(data.error), discord: redactId(discordId) }));
  } catch (error) {
    console.warn(JSON.stringify({ event: "guild_member_check_failed", purpose,
      error: String(error?.name || "Error"), discord: redactId(discordId) }));
  }
  return "unknown";
}

function membershipRefusal(env, membership, extra = {}) {
  if (membership === "not_member") {
    return json({ ...extra, ...(extra.ok === false ? { code: "join_required" } : {}),
      error: "join_required", message: JOIN_REQUIRED_MESSAGE, joinUrl: discordInviteUrl(env) }, 403);
  }
  return json({ ...extra, ...(extra.ok === false ? { code: "membership_unavailable" } : {}),
    error: "membership_unavailable", message: MEMBERSHIP_UNAVAILABLE_MESSAGE }, 503);
}

// Adds the signed-in user to the Venice server (OAuth scope guilds.join) so the
// bot can DM them (trial / subscription confirmation). Returns "joined" | "already_member" | "failed".
async function joinVeniceGuild(env, discordId, accessToken, grantedScope) {
  if (!orionBotConfigured(env) || typeof accessToken !== "string" || !accessToken) return "failed";
  if (typeof grantedScope === "string" && !grantedScope.split(/\s+/u).includes("guilds.join")) {
    console.warn(JSON.stringify({ event: "guild_join", result: "failed", reason: "scope_not_granted",
      discord: redactId(discordId) }));
    return "failed";
  }
  try {
    const response = await orionRequest(env, "/api/bot/guild-join",
      { discord_id: discordId, access_token: accessToken }, { timeoutMs: ORION_GUILD_TIMEOUT_MS });
    const data = await readJson(response);
    const result = response.ok && ["joined", "already_member"].includes(data.result) ? data.result : "failed";
    console.log(JSON.stringify({ event: "guild_join", result, status: response.status,
      backendCode: safeCode(data.error), discord: redactId(discordId) }));
    return result;
  } catch (error) {
    console.warn(JSON.stringify({ event: "guild_join", result: "failed", error: String(error?.name || "Error"),
      discord: redactId(discordId) }));
    return "failed";
  }
}

// Maps the backend's /api/bot/trial reply to a site outcome. `code` is the
// contract; the message tests keep a Worker deployed ahead of the backend working.
function trialOutcome(status, data) {
  if (status === 200 && data.ok === true) return "issued";
  const code = safeCode(data.code || data.error);
  const message = String(data.message || "");
  if (code === "already_claimed" || (status === 200 && /already claimed/iu.test(message))) return "already_claimed";
  if (code === "dm_failed" || (status === 200 && /couldn.t DM you/iu.test(message))) return "dm_failed";
  if (status === 429 || code === "rate_limited") return "rate_limited";
  return "unavailable"; // blacklisted, auth, outage, anything unexpected: one neutral error
}

function allowedOrigins(url) {
  return new Set([SITE_ORIGIN, "https://www.zaeorion.com", canonicalOrigin(url), url.origin]);
}

async function claimTrial(request, env, url) {
  if (!trialConfigured(env)) {
    return json({ ok: false, code: "unavailable",
      message: "The free trial can't be started right now. Please try again in a few minutes, or open a ticket in the Venice Discord." }, 503);
  }
  // Belt and braces on top of SameSite=Lax: same-origin JSON requests only.
  if (!allowedOrigins(url).has(request.headers.get("Origin") || "")) {
    return json({ ok: false, code: "forbidden_origin", error: "forbidden_origin",
      message: "This request must come from zaeorion.com." }, 403);
  }
  const contentType = (request.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    return json({ ok: false, code: "unsupported_media_type", error: "unsupported_media_type",
      message: "Send this request as JSON." }, 415);
  }
  // The Discord ID comes only from the signed session; the body is never read.
  const session = await currentSession(request, env);
  if (!DISCORD_ID_PATTERN.test(String(session?.discordId || ""))) {
    return json({ ok: false, code: "auth_required", error: "Sign in with Discord to start your free trial.",
      message: "Sign in with Discord to start your free trial.", authUrl: "/auth/discord" }, 401);
  }
  const discordId = String(session.discordId);
  const membership = await discordMembership(env, discordId, "trial");
  if (membership !== "member") return membershipRefusal(env, membership, { ok: false });

  let status = 0;
  let data = {};
  try {
    const response = await orionRequest(env, "/api/bot/trial", { discord_id: discordId, actor_discord_id: discordId });
    status = response.status;
    data = await readJson(response);
  } catch (error) {
    data = { error: String(error?.name || "error").toLowerCase() };
  }
  const outcome = trialOutcome(status, data);
  console.log(JSON.stringify({ event: "site_trial", result: outcome, backendStatus: status,
    backendCode: safeCode(data.code || data.error), discord: redactId(discordId) }));
  const { status: httpStatus, message } = TRIAL_OUTCOMES[outcome];
  return json({ ok: outcome === "issued", code: outcome, message }, httpStatus);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/gu, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

// The signed-in Discord user's avatar URL, or "" when they have no custom avatar.
// Single source of truth for both the account page and /api/checkout/config so the
// hash validation can never drift between them. Only Discord's own hash format is
// accepted, so nothing user-controlled reaches the URL.
function avatarUrlFor(session) {
  const id = String(session?.discordId || "");
  const hash = String(session?.avatar || "");
  if (!/^\d{16,22}$/u.test(id) || !/^(?:a_)?[a-f0-9]{32}$/u.test(hash)) return "";
  return `https://cdn.discordapp.com/avatars/${id}/${hash}.png?size=64`;
}

function discordAccountPage(session, purchaseComplete = false) {
  const name = escapeHtml(session.username || 'Discord account');
  const id = escapeHtml(session.discordId);
  const avatar = /^(?:a_)?[a-f0-9]{32}$/u.test(String(session.avatar || ''))
    ? `<img src="https://cdn.discordapp.com/avatars/${id}/${session.avatar}.png?size=128" width="72" height="72" alt="">`
    : `<span aria-hidden="true">${name.slice(0, 1)}</span>`;
  const notice = purchaseComplete
    ? '<p class="account-notice" role="status">Checkout complete. The Venice bot will DM your subscription confirmation in Discord as soon as activation finishes.</p>'
    : '';
  // [COPY-FIX 2026-09-23 CW-4] After checkout the one-time code is the ONLY next step:
  // no Subscribe button (they just paid) and no trial block.
  const actions = purchaseComplete
    ? '<div class="account-actions"><a class="button primary" href="/connect">Get your one-time code</a></div>'
    : '<div class="account-actions"><a class="button primary" href="/buy">Subscribe · $14.99/month beta</a><a class="button secondary" href="/connect">Get your one-time code</a></div><div class="account-trial"><strong>Starting the 7-day trial?</strong><p>Start it on the Venice home page with this same account — 7 days free, no card needed. Then choose Get your one-time code and paste that code into Venice on your PC. Need help? Open a ticket in the server.</p></div>';
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#05070b"><title>Your Discord account · Venice</title><link rel="icon" href="/favicon.ico"><link rel="stylesheet" href="/styles.css"></head><body><main class="shell account-page" id="main"><a class="brand" href="/"><span class="brand-mark"><img src="/orion.png" width="28" height="28" alt=""></span><span>VENICE</span></a><section class="account-panel" aria-labelledby="account-title"><p class="kicker">VENICE ACCOUNT</p><h1 id="account-title">Discord connected.</h1>${notice}<div class="account-identity"><div class="account-avatar">${avatar}</div><div><strong>${name}</strong><span>Discord ID ${id}</span></div></div><p>This is the Discord profile linked to this browser. Venice uses this verified account for checkout and launcher access; your Discord ID by itself is not a sign-in code.</p>${actions}<form action="/logout" method="post"><button class="account-switch" type="submit">Use a different Discord account</button></form></section></main></body></html>`;
  const response = withHeaders(new Response(html, {
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'private, no-store', 'Vary': 'Cookie' },
  }));
  response.headers.set('Referrer-Policy', 'no-referrer');
  return response;
}

async function discordAccount(request, env, url) {
  if (!oauthConfigured(env)) return json({ error: 'Discord sign-in is not configured.' }, 503);
  const session = await currentSession(request, env);
  if (!/^\d{16,22}$/u.test(String(session?.discordId || ''))) {
    return withHeaders(Response.redirect(`${canonicalOrigin(url)}/auth/discord?return_to=%2Fdiscord`, 302));
  }
  return discordAccountPage(session, url.searchParams.get('purchase') === 'complete');
}

const ACTIVATING_MESSAGE = 'Payment received — activating your Venice subscription. This usually takes under a minute.';
const ACTIVATING_FALLBACK_MESSAGE = 'Still waiting? Open a ticket in Discord with your receipt email — you will not be charged twice.';

// `activating` = { attempt } while a paid checkout is still being provisioned, or
// { timedOut: true } once the bounded retry is spent. Retry is a meta refresh because the
// CSP allows no inline script; the attempt counter in the URL bounds it (nothing is trusted
// from it except the loop count).
function connectPage(session, code = '', error = '', activating = null) {
  const name = escapeHtml(session.username || 'Discord account');
  const id = escapeHtml(session.discordId);
  let action;
  let status = code ? 200 : 403;
  let refresh = '';
  if (code) {
    action = `<p class="kicker">ONE-TIME CONNECTION</p><h1>Connect Venice.</h1><p>Signed in as <strong>${name}</strong> (${id}). This code expires in five minutes and works once.</p><p><a class="button primary" href="orion://activate?key=${code}">Open Venice →</a></p><p>If the app does not open, enter <code>${code}</code> in its unlock screen.</p>`;
  } else if (activating?.timedOut) {
    status = 202;
    action = `<p class="kicker">PAYMENT RECEIVED</p><h1>Still activating.</h1><p role="status">${escapeHtml(ACTIVATING_FALLBACK_MESSAGE)}</p><p><a class="button primary" href="/connect">Check again</a> <a class="button secondary" href="/discord">View your Discord account →</a></p>`;
  } else if (activating) {
    status = 202;
    const next = `/connect?activating=${activating.attempt + 1}`;
    const retrySeconds = activating.retrySeconds || ACTIVATING_RETRY_SECONDS;
    if (retrySeconds <= ACTIVATING_RETRY_SECONDS) {
      refresh = `<meta http-equiv="refresh" content="${retrySeconds};url=${next}">`;
    }
    const message = activating.rateLimited
      ? `Too many connection-code requests. Try again in ${retrySeconds} seconds; your payment and account access are unchanged.`
      : ACTIVATING_MESSAGE;
    action = `<p class="kicker">PAYMENT RECEIVED</p><h1>Activating Venice.</h1><p role="status">${escapeHtml(message)}</p><p>${activating.rateLimited ? 'Your existing code remains valid until it expires.' : `This page checks again every ${ACTIVATING_RETRY_SECONDS} seconds and shows your one-time code as soon as it is ready.`}</p><p><a class="button secondary" href="${next}">Check now</a></p>`;
  } else {
    action = `<p class="kicker">ACCOUNT ACCESS</p><h1>Not ready to connect.</h1><p>${escapeHtml(error)}</p><p><a class="button secondary" href="/discord">View your Discord account →</a></p>`;
  }
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${refresh}<title>Connect Venice</title><link rel="icon" href="/favicon.ico"><link rel="stylesheet" href="/styles.css"></head><body><main class="shell" id="main"><a class="brand" href="/"><span class="brand-mark"><img src="/orion.png" width="28" height="28" alt=""></span><span>VENICE</span></a><section class="section">${action}</section></main></body></html>`;
  const response = withHeaders(new Response(html, {
    status,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'private, no-store', 'Vary': 'Cookie' },
  }));
  response.headers.set('Referrer-Policy', 'no-referrer');
  return response;
}

// True only when the signed paid-checkout cookie belongs to the signed-in Discord account.
async function recentPaidCheckout(request, env, session) {
  const paid = await verifyValue(parseCookies(request)[PAID_COOKIE], env.SESSION_SECRET);
  return Boolean(paid?.paid === true && paid.discordId && paid.discordId === session.discordId);
}

function activatingAttempt(url) {
  const raw = url.searchParams.get('activating') || '0';
  const attempt = /^\d{1,3}$/u.test(raw) ? Number(raw) : 0;
  return Math.min(attempt, ACTIVATING_MAX_ATTEMPTS);
}

async function connectLauncher(request, env, url) {
  if (!pairConfigured(env)) return json({ error: 'Discord connection is not configured.' }, 503);
  const session = await currentSession(request, env);
  if (!/^\d{16,22}$/u.test(String(session?.discordId || ''))) {
    return withHeaders(Response.redirect(`${canonicalOrigin(url)}/auth/discord?return_to=%2Fconnect`, 302));
  }
  // Poll entitlement/provisioning on a NON-MINTING endpoint. A pair-issue call
  // consumes its five-per-ten-minute quota and only happens once readiness is
  // confirmed. The signed paid cookie affects copy, never backend authority.
  // [COPY-FIX 2026-09-23 CW-1] A customer who paid in ANOTHER browser has no paid cookie and
  // lands here while the webhook is still provisioning; tell them to refresh.
  const ENTITLEMENT_MESSAGE = 'Your Discord account needs an active trial or subscription. Start the free trial on the Venice home page, then try again. Just paid? Activation can take up to a minute — refresh this page.';
  const OUTAGE_MESSAGE = "Venice's servers didn't respond just now. Nothing is wrong with your account — wait a minute and try again. If it keeps happening, open a ticket in the Venice Discord.";
  const paid = await recentPaidCheckout(request, env, session);
  const attempt = activatingAttempt(url);
  let statusResponse;
  try {
    statusResponse = await orionRequest(env, '/api/bot/pair-status',
      { discord_id: session.discordId }, { pairIssuer: true });
  } catch (error) {
    console.warn(JSON.stringify({ event: 'pair_status_unreachable', error: String(error?.name || 'Error'), discord: redactId(session.discordId) }));
    return connectPage(session, '', OUTAGE_MESSAGE);
  }
  if (statusResponse.status === 429 && paid) {
    const retry = await readJson(statusResponse);
    const retrySeconds = Math.max(1, Math.min(600, Number(retry.retry_after_s) || 60));
    return connectPage(session, '', '', attempt >= ACTIVATING_MAX_ATTEMPTS
      ? { timedOut: true } : { attempt, rateLimited: true, retrySeconds });
  }
  if (statusResponse.status >= 500 || statusResponse.status === 429) {
    console.warn(JSON.stringify({ event: 'pair_status_backend_error', status: statusResponse.status, discord: redactId(session.discordId) }));
    return connectPage(session, '', OUTAGE_MESSAGE);
  }
  if (!statusResponse.ok) {
    return connectPage(session, '', ENTITLEMENT_MESSAGE);
  }
  const status = await readJson(statusResponse);
  if (status.ready !== true) {
    if (!paid) return connectPage(session, '', ENTITLEMENT_MESSAGE);
    console.log(JSON.stringify({ event: 'pair_status_awaiting_provision', attempt, discord: redactId(session.discordId) }));
    return connectPage(session, '', '', attempt >= ACTIVATING_MAX_ATTEMPTS ? { timedOut: true } : { attempt });
  }
  let response;
  try {
    response = await orionRequest(env, '/api/bot/pair-issue',
      { discord_id: session.discordId }, { pairIssuer: true });
  } catch (error) {
    console.warn(JSON.stringify({ event: 'pair_issue_unreachable', error: String(error?.name || 'Error'), discord: redactId(session.discordId) }));
    return connectPage(session, '', OUTAGE_MESSAGE);
  }
  if (response.status === 429 && paid) {
    const retry = await readJson(response);
    const retrySeconds = Math.max(1, Math.min(600, Number(retry.retry_after_s) || 60));
    return connectPage(session, '', '', attempt >= ACTIVATING_MAX_ATTEMPTS
      ? { timedOut: true } : { attempt, rateLimited: true, retrySeconds });
  }
  if (response.status >= 500 || response.status === 429) {
    return connectPage(session, '', OUTAGE_MESSAGE);
  }
  if (!response.ok) {
    if (paid) return connectPage(session, '', '', attempt >= ACTIVATING_MAX_ATTEMPTS
      ? { timedOut: true } : { attempt });
    return connectPage(session, '', ENTITLEMENT_MESSAGE);
  }
  const data = await readJson(response);
  if (!/^PAIR-[A-HJ-NP-Z2-9]{32}$/u.test(String(data.pair_code || ''))) {
    console.warn(JSON.stringify({ event: 'pair_issue_bad_code', discord: redactId(session.discordId) }));
    return connectPage(session, '', OUTAGE_MESSAGE);
  }
  const issued = connectPage(session, data.pair_code);
  if (parseCookies(request)[PAID_COOKIE]) issued.headers.append('Set-Cookie', clearCookie(PAID_COOKIE));
  return issued;
}

function discordIdFrom(object) {
  const value = object?.metadata?.discord_user_id || object?.client_reference_id || "";
  return /^\d{16,22}$/u.test(String(value)) ? String(value) : "";
}

function subscriptionId(object) {
  const value = object?.parent?.subscription_details?.subscription || object?.subscription || object?.id;
  return typeof value === "string" ? value : value?.id || "";
}

function subscriptionMatchesPlan(env, subscription) {
  const items = subscription?.items;
  return subscription?.id?.startsWith("sub_")
    && Array.isArray(items?.data) && items.data.length === 1 && !items.has_more
    && items.data[0]?.price?.id === env.STRIPE_PRICE_ID
    && items.data[0]?.quantity === 1;
}

async function paidSubscriptionDiscordId(env, id, expectedId = "") {
  if (!/^sub_[A-Za-z0-9]+$/u.test(id)) throw new Error("Paid event has no subscription ID");
  const subscription = await stripeRequest(env, `/v1/subscriptions/${encodeURIComponent(id)}`);
  const discordId = discordIdFrom(subscription);
  if (subscription.livemode !== true || subscription.status !== "active" || !subscriptionMatchesPlan(env, subscription)
      || !discordId || (expectedId && discordId !== expectedId)) {
    throw new Error("Subscription identity, status, or price did not match Venice");
  }
  return discordId;
}

// [2026-09-23 rc1 RT-LOW-04 / CL3-F6-002] The verified Stripe event id rides every backend
// call as `stripe_event_id`; the backend keeps a processed-event marker so a replayed delivery
// is a 200 no-op (no second revoke audit / owner alert / DM). A 409 (same event still in flight)
// makes callOrion throw -> 500 -> Stripe retries later.
function stripeEventRef(event) {
  const id = String(event?.id || "");
  return /^evt_[A-Za-z0-9_]{1,250}$/u.test(id) ? { stripe_event_id: id } : {};
}

async function processStripeEvent(env, event) {
  const object = event?.data?.object || {};
  const eventRef = stripeEventRef(event);
  const liveKeys = /^(?:sk|rk)_live_/u.test(String(env.STRIPE_SECRET_KEY || ""))
    && String(env.STRIPE_PUBLISHABLE_KEY || "").startsWith("pk_live_");
  if (!liveKeys || event?.livemode !== true || object.livemode !== true) return "non_live_ignored";
  if (event.type === "checkout.session.completed" && object.payment_status === "paid") {
    if (object.mode !== "subscription" || !Number.isInteger(object.amount_total)
        || object.amount_total <= 0) throw new Error("Paid checkout is not a charged subscription");
    const discordId = discordIdFrom(object);
    if (!discordId) throw new Error("Paid checkout omitted discord_user_id");
    const paidSubscriptionId = subscriptionId(object);
    await paidSubscriptionDiscordId(env, paidSubscriptionId, discordId);
    await callOrion(env, "/api/bot/provision", {
      order_id: `stripe:checkout:${object.id}`,
      discord_user_id: discordId,
      plan: "month",
      days: 30,
      renew: false,
      notify: true,
      subscription_id: paidSubscriptionId,
      ...eventRef,
    });
    return "provisioned";
  }
  if (event.type === "invoice.paid") {
    if (object.billing_reason === "subscription_create") return "initial_invoice_ignored";
    // Invoices lost the boolean `paid` in API 2025-03-31.basil; `status` is the truth.
    if (object.billing_reason !== "subscription_cycle" || object.status !== "paid"
        || !Number.isInteger(object.amount_paid) || object.amount_paid <= 0) {
      throw new Error("Invoice is not a paid recurring subscription cycle");
    }
    const invoiceId = discordIdFrom(object)
      || discordIdFrom({ metadata: object.parent?.subscription_details?.metadata });
    const paidSubscriptionId = subscriptionId(object);
    const discordId = await paidSubscriptionDiscordId(env, paidSubscriptionId, invoiceId);
    await callOrion(env, "/api/bot/provision", {
      order_id: `stripe:invoice:${object.id}`,
      discord_user_id: discordId,
      plan: "month",
      days: 30,
      renew: true,
      notify: true,
      subscription_id: paidSubscriptionId,
      ...eventRef,
    });
    return "renewed";
  }
  if (event.type === "customer.subscription.deleted"
      || (event.type === "customer.subscription.updated" && ["canceled", "unpaid"].includes(object.status))) {
    const discordId = discordIdFrom(object);
    if (!discordId || !/^sub_[A-Za-z0-9]+$/u.test(String(object.id || ""))) {
      throw new Error("Ended subscription did not identify a Discord-linked subscription");
    }
    await callOrion(env, "/api/bot/chargeback", {
      discord_user_id: discordId,
      kind: "refunded",
      reason: `Stripe subscription ${String(object.status || "deleted").slice(0, 40)}`,
      subscription_id: object.id,
      ...eventRef,
    });
    return "revoked";
  }
  if (event.type === "invoice.payment_failed") return "payment_failure_recorded";
  // [2026-09-22 RED TEAM CX-002] A FULL refund or an opened dispute revokes the paid entitlement
  // even when the subscription itself is still active. Partial refunds are left to the owner (no
  // automatic action). Identity is resolved through the charge's invoice -> subscription, and the
  // backend's own chargeback route audits + alerts.
  if (event.type === "charge.refunded" || event.type === "charge.dispute.created") {
    const charge = event.type === "charge.refunded" ? object
      : await stripeRequest(env, `/v1/charges/${encodeURIComponent(String(object.charge || ""))}`);
    if (event.type === "charge.refunded") {
      const amount = Number(charge.amount || 0);
      const refunded = Number(charge.amount_refunded || 0);
      if (charge.refunded !== true && (amount <= 0 || refunded < amount)) return "partial_refund_recorded";
    }
    const invoiceId = String(charge.invoice || "");
    if (!/^in_[A-Za-z0-9]+$/u.test(invoiceId)) return "refund_without_invoice_recorded";
    const invoice = await stripeRequest(env, `/v1/invoices/${encodeURIComponent(invoiceId)}`);
    const subId = subscriptionId(invoice)
      || String(invoice.parent?.subscription_details?.subscription || invoice.subscription || "");
    if (!/^sub_[A-Za-z0-9]+$/u.test(subId)) return "refund_without_subscription_recorded";
    const subscription = await stripeRequest(env, `/v1/subscriptions/${encodeURIComponent(subId)}`);
    const discordId = discordIdFrom(subscription) || discordIdFrom(invoice);
    if (!discordId) throw new Error("Refund/dispute did not identify a Discord-linked subscription");
    await callOrion(env, "/api/bot/chargeback", {
      discord_user_id: discordId,
      kind: event.type === "charge.refunded" ? "refunded" : "disputed",
      reason: `Stripe ${event.type} ${String(charge.id || "").slice(0, 40)}`,
      subscription_id: subId,
      ...eventRef,
    });
    return event.type === "charge.refunded" ? "refund_revoked" : "dispute_revoked";
  }
  return "ignored";
}

async function stripeWebhook(request, env) {
  if (!checkoutConfigured(env)) return json({ error: "Checkout is not configured." }, 503);
  const declaredLength = Number(request.headers.get("Content-Length") || 0);
  if (declaredLength > MAX_BODY_BYTES) return json({ error: "Payload too large." }, 413);
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) return json({ error: "Payload too large." }, 413);
  const valid = await verifyStripeSignature(body, request.headers.get("Stripe-Signature"), env.STRIPE_WEBHOOK_SECRET);
  if (!valid) return json({ error: "Invalid Stripe signature." }, 400);
  let event;
  try { event = JSON.parse(body); } catch { return json({ error: "Invalid JSON." }, 400); }
  try {
    const result = await processStripeEvent(env, event);
    console.log(JSON.stringify({ event: "stripe_webhook", type: event.type, id: event.id, result }));
    return json({ received: true });
  } catch (error) {
    console.error(JSON.stringify({ event: "stripe_webhook_failed", type: event?.type, id: event?.id, message: String(error?.message || error) }));
    return json({ error: "Webhook processing failed." }, 500);
  }
}

async function checkoutComplete(request, env, url) {
  if (!checkoutConfigured(env)) return withHeaders(Response.redirect(`${canonicalOrigin(url)}/#pricing`, 302));
  const sessionId = url.searchParams.get("session_id") || "";
  if (!/^cs_[A-Za-z0-9_]+$/u.test(sessionId)) return json({ error: "Invalid checkout session." }, 400);
  try {
    const checkout = await stripeRequest(env, `/v1/checkout/sessions/${encodeURIComponent(sessionId)}`);
    const session = await currentSession(request, env);
    if (!session?.discordId || discordIdFrom(checkout) !== session.discordId) return json({ error: "Checkout session does not match this Discord account." }, 403);
    const complete = checkout.status === "complete";
    const destination = complete ? "/discord?purchase=complete" : "/#pricing";
    const response = new Response(null, { status: 302, headers: { Location: `${canonicalOrigin(url)}${destination}`, "Cache-Control": "no-store" } });
    if (complete) {
      // [GMC-001] Messaging hint only: /connect shows "activating" instead of "needs a
      // subscription" while the webhook provisions. Access still comes from the backend.
      const paid = await signValue({ discordId: session.discordId, paid: true,
        exp: Math.floor(Date.now() / 1000) + PAID_COOKIE_SECONDS }, env.SESSION_SECRET);
      response.headers.append("Set-Cookie", cookie(PAID_COOKIE, paid, PAID_COOKIE_SECONDS));
    }
    return withHeaders(response);
  } catch {
    return json({ error: "Checkout status could not be confirmed." }, 502);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = request.method.toUpperCase();

    if (url.hostname === "www.zaeorion.com") {
      return withHeaders(Response.redirect(`https://zaeorion.com${url.pathname}${url.search}${url.hash}`, 308));
    }
    if (url.pathname === "/health") {
      return json({ ok: true, service: "venice-site", checkout: checkoutConfigured(env) ? "stripe" : "unavailable" });
    }
    if (url.pathname === "/api/checkout/config" && method === "GET") {
      const session = await currentSession(request, env);
      const authenticated = Boolean(session?.discordId);
      // Live membership for signed-in users (one backend call, no retries);
      // fail-soft here because this only drives the UI, the purchase routes
      // re-check and fail closed.
      const membership = authenticated ? await discordMembership(env, session.discordId, "config") : "not_member";
      return json({
        provider: checkoutConfigured(env) ? "stripe" : "unavailable",
        configured: checkoutConfigured(env),
        publishableKey: checkoutConfigured(env) ? env.STRIPE_PUBLISHABLE_KEY : "",
        authenticated,
        username: session?.username || "",
        trial: trialConfigured(env),
        inGuild: membership === "member",
        membershipUnknown: authenticated && membership === "unknown",
        joinUrl: discordInviteUrl(env),
        // [2026-09-19 owner] Show the connected Discord account in the site header.
        // The URL is built HERE, from the signed session, so the page never receives the
        // raw Discord id and cannot be tricked into rendering someone else's avatar: the
        // hash is re-validated against Discord's own format before it is interpolated, and
        // an account with no custom avatar returns "" so the front end falls back to an
        // initial. cdn.discordapp.com is already the only remote host allowed by img-src.
        avatarUrl: avatarUrlFor(session),
      });
    }
    if (url.pathname === "/api/trial") {
      if (method === "POST") return claimTrial(request, env, url);
      const refused = json({ ok: false, code: "method_not_allowed", message: "Use POST." }, 405);
      refused.headers.set("Allow", "POST");
      return refused;
    }
    if (url.pathname === "/auth/discord" && method === "GET") return beginDiscordAuth(request, env, url);
    if (url.pathname === "/auth/discord/callback" && method === "GET") return finishDiscordAuth(request, env, url);
    if (url.pathname === "/discord" && method === "GET") return discordAccount(request, env, url);
    if (url.pathname === "/connect" && method === "GET") return connectLauncher(request, env, url);
    if (url.pathname === "/logout" && method === "POST") {
      const response = new Response(null, { status: 303, headers: { "Location": `${canonicalOrigin(url)}/discord`, "Set-Cookie": clearCookie(SESSION_COOKIE), "Cache-Control": "no-store" } });
      return withHeaders(response);
    }
    if (url.pathname === "/api/checkout/session" && method === "POST") return createCheckoutSession(request, env, url);
    if (url.pathname === "/api/stripe/webhook" && method === "POST") return stripeWebhook(request, env);
    if (url.pathname === "/checkout/complete" && method === "GET") return checkoutComplete(request, env, url);
    if (url.pathname === "/buy") {
      if (checkoutConfigured(env)) {
        const session = await currentSession(request, env);
        const destination = session?.discordId ? "/#pricing" : `/auth/discord?return_to=${encodeURIComponent("/#pricing")}`;
        return withHeaders(Response.redirect(`${canonicalOrigin(url)}${destination}`, 302));
      }
      return withHeaders(new Response("Checkout is being prepared.", { status: 503, headers: { "Cache-Control": "no-store" } }));
    }
    return withHeaders(await env.ASSETS.fetch(request));
  },
};

export {
  checkoutConfigured,
  constantTimeEqual,
  parseStripeSignature,
  redirectTarget,
  safeReturnTo,
  signValue,
  verifyStripeSignature,
  verifyValue,
};
