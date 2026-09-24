import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import worker, {
  checkoutConfigured,
  constantTimeEqual,
  parseStripeSignature,
  redirectTarget,
  safeReturnTo,
  signValue,
  verifyStripeSignature,
  verifyValue,
} from '../src/worker.js';

const unavailableEnv = {
  CHECKOUT_PROVIDER: 'stripe',
  ASSETS: { fetch: () => new Response('asset', { status: 200 }) },
};

const stripeEnv = {
  ...unavailableEnv,
  CHECKOUT_PROVIDER: 'stripe',
  STRIPE_MODE: 'test',
  STRIPE_PUBLISHABLE_KEY: 'pk_test_public',
  STRIPE_PRICE_ID: 'price_test',
  STRIPE_SECRET_KEY: 'sk_test_fixture_only',
  STRIPE_WEBHOOK_SECRET: 'webhook',
  DISCORD_CLIENT_ID: '1549481446782017668',
  DISCORD_CLIENT_SECRET: 'discord-secret',
  DISCORD_INVITE_URL: 'https://discord.gg/yTuekgdgEN',
  SESSION_SECRET: 'session-secret-with-sufficient-length',
  ORION_API_BASE: 'https://api.example',
  ORION_BOT_SECRET: 'bot-secret',
  PAIR_ISSUER_SECRET: 'pair-secret',
  ORION_EDGE_AUTH: 'edge-secret',
};
const liveStripeEnv = {
  ...stripeEnv,
  STRIPE_MODE: 'live',
  STRIPE_PUBLISHABLE_KEY: 'pk_live_fixture_only',
  STRIPE_SECRET_KEY: 'rk_live_fixture_only',
};

async function signedStripeEvent(event, env = liveStripeEnv) {
  const body = JSON.stringify(event);
  const t = Math.floor(Date.now() / 1000);
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(env.STRIPE_WEBHOOK_SECRET),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const bytes = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`${t}.${body}`));
  return new Request('https://zaeorion.com/api/stripe/webhook', {
    method: 'POST', body, headers: { 'Stripe-Signature': `t=${t},v1=${Buffer.from(bytes).toString('hex')}` },
  });
}

const subscription = {
  id: 'sub_test1', livemode: true, status: 'active', metadata: { discord_user_id: '123456789012345678' },
  items: { data: [{ price: { id: stripeEnv.STRIPE_PRICE_ID }, quantity: 1 }], has_more: false },
};

test('signed paid monthly checkout provisions only the verified Stripe price and Discord ID', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, body: init?.body && JSON.parse(init.body) });
    if (path === '/v1/subscriptions/sub_test1') return Response.json(subscription);
    if (path === '/api/bot/provision') return Response.json({ ok: true }, { status: 201 });
    throw Error(`unexpected path ${path}`);
  };
  try {
    const event = { id: 'evt_checkout', livemode: true, type: 'checkout.session.completed', data: { object: {
      id: 'cs_test1', livemode: true, mode: 'subscription', payment_status: 'paid', amount_total: 2500,
      subscription: 'sub_test1', metadata: { discord_user_id: '123456789012345678' },
    } } };
    const response = await worker.fetch(await signedStripeEvent(event), liveStripeEnv);
    assert.equal(response.status, 200);
    assert.deepEqual(calls.map((call) => call.path), ['/v1/subscriptions/sub_test1', '/api/bot/provision']);
    assert.deepEqual(calls[1].body, { order_id: 'stripe:checkout:cs_test1',
      discord_user_id: '123456789012345678', plan: 'month', days: 30, renew: false,
      notify: true, subscription_id: 'sub_test1', stripe_event_id: 'evt_checkout' });
  } finally { globalThis.fetch = originalFetch; }
});

test('signed recurring invoice resolves the current parent subscription field', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, body: init?.body && JSON.parse(init.body) });
    if (path === '/v1/subscriptions/sub_test1') return Response.json(subscription);
    if (path === '/api/bot/provision') return Response.json({ ok: true });
    throw Error(`unexpected path ${path}`);
  };
  try {
    const event = { id: 'evt_renew', livemode: true, type: 'invoice.paid', data: { object: {
      id: 'in_test2', object: 'invoice', livemode: true, billing_reason: 'subscription_cycle', status: 'paid', amount_paid: 2500,
      parent: { subscription_details: { subscription: 'sub_test1',
        metadata: { discord_user_id: '123456789012345678' } } },
    } } };
    const response = await worker.fetch(await signedStripeEvent(event), liveStripeEnv);
    assert.equal(response.status, 200);
    assert.deepEqual(calls.map((call) => call.path), ['/v1/subscriptions/sub_test1', '/api/bot/provision']);
    assert.equal(calls[1].body.renew, true);
    assert.equal(calls[1].body.discord_user_id, '123456789012345678');
    assert.equal(calls[1].body.subscription_id, 'sub_test1');
  } finally { globalThis.fetch = originalFetch; }
});

test('paid one-off checkout or wrong Stripe price cannot mint membership', async () => {
  const originalFetch = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async (input) => {
    const path = new URL(input).pathname;
    paths.push(path);
    if (path === '/v1/subscriptions/sub_test1') return Response.json({ ...subscription,
      items: { data: [{ price: { id: 'price_unrelated' }, quantity: 1 }], has_more: false } });
    throw Error(`unexpected path ${path}`);
  };
  try {
    const base = { id: 'cs_test_bad', livemode: true, payment_status: 'paid', amount_total: 2500,
      subscription: 'sub_test1', metadata: { discord_user_id: '123456789012345678' } };
    for (const object of [{ ...base, mode: 'payment' }, { ...base, mode: 'subscription' }]) {
      const response = await worker.fetch(await signedStripeEvent({
        id: 'evt_bad', livemode: true, type: 'checkout.session.completed', data: { object },
      }), liveStripeEnv);
      assert.equal(response.status, 500);
    }
    assert.deepEqual(paths, ['/v1/subscriptions/sub_test1']);
  } finally { globalThis.fetch = originalFetch; }
});

test('subscription cancellation targets only the Discord-linked Venice plan', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, body: JSON.parse(init.body) });
    if (path === '/api/bot/chargeback') return Response.json({ ok: true });
    throw Error(`unexpected path ${path}`);
  };
  try {
    const event = { id: 'evt_cancel', livemode: true, type: 'customer.subscription.deleted',
      data: { object: { id: subscription.id, livemode: true, status: 'canceled',
        metadata: subscription.metadata } } };
    const response = await worker.fetch(await signedStripeEvent(event), liveStripeEnv);
    assert.equal(response.status, 200);
    assert.equal(calls[0].path, '/api/bot/chargeback');
    assert.equal(calls[0].body.discord_user_id, '123456789012345678');
    assert.equal(calls[0].body.subscription_id, 'sub_test1');
    assert.equal(calls.length, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test('test-mode and mixed-mode Stripe webhooks never alter production entitlements', async () => {
  const originalFetch = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async (input) => { paths.push(new URL(input).pathname); throw Error('unexpected outbound request'); };
  try {
    for (const type of ['checkout.session.completed', 'invoice.paid', 'customer.subscription.deleted']) {
      const object = {
        id: type === 'customer.subscription.deleted' ? 'sub_test1' : 'cs_test1',
        livemode: false, mode: 'subscription', payment_status: 'paid', amount_total: 2500,
        subscription: 'sub_test1', metadata: { discord_user_id: '123456789012345678' },
      };
      for (const [env, eventLive, objectLive] of [
        [stripeEnv, false, false],
        [liveStripeEnv, false, false],
        [liveStripeEnv, true, false],
        [stripeEnv, true, true],
      ]) {
        const event = { id: 'evt_non_live', livemode: eventLive, type,
          data: { object: { ...object, livemode: objectLive } } };
        const response = await worker.fetch(await signedStripeEvent(event, env), env);
        assert.equal(response.status, 200);
      }
    }
    assert.deepEqual(paths, []);
  } finally { globalThis.fetch = originalFetch; }
});

test('live event cannot provision a test-mode subscription', async () => {
  const originalFetch = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async (input) => {
    const path = new URL(input).pathname;
    paths.push(path);
    if (path === '/v1/subscriptions/sub_test1') return Response.json({ ...subscription, livemode: false });
    throw Error(`unexpected path ${path}`);
  };
  try {
    const event = { id: 'evt_mixed_subscription', livemode: true, type: 'checkout.session.completed',
      data: { object: { id: 'cs_test1', livemode: true, mode: 'subscription',
        payment_status: 'paid', amount_total: 2500, subscription: 'sub_test1',
        metadata: { discord_user_id: '123456789012345678' } } } };
    const response = await worker.fetch(await signedStripeEvent(event), liveStripeEnv);
    assert.equal(response.status, 500);
    assert.deepEqual(paths, ['/v1/subscriptions/sub_test1']);
  } finally { globalThis.fetch = originalFetch; }
});

test('redirect targets are limited to HTTPS allowlisted hosts', () => {
  const hosts = new Set(['discord.gg', 'discord.com']);
  assert.equal(redirectTarget('https://discord.gg/venice', hosts), 'https://discord.gg/venice');
  assert.equal(redirectTarget('http://discord.gg/venice', hosts), null);
  assert.equal(redirectTarget('https://evil.example/?next=discord.gg', hosts), null);
});

test('return destinations cannot escape the Venice origin', () => {
  assert.equal(safeReturnTo('/pricing?source=discord#plan'), '/pricing?source=discord#plan');
  assert.equal(safeReturnTo('//evil.example/path'), '/#pricing');
  assert.equal(safeReturnTo('/\\evil.example'), '/#pricing');
  assert.equal(safeReturnTo('https://evil.example'), '/#pricing');
});

test('signed sessions verify and reject tampering or expiry', async () => {
  const secret = 'unit-test-secret-with-sufficient-length';
  const signed = await signValue({ discordId: '123456789012345678', exp: Math.floor(Date.now() / 1000) + 60 }, secret);
  assert.equal((await verifyValue(signed, secret)).discordId, '123456789012345678');
  const [payload, signature] = signed.split('.');
  const tampered = `${payload}.${signature[0] === 'A' ? 'B' : 'A'}${signature.slice(1)}`;
  assert.equal(await verifyValue(tampered, secret), null);
  const expired = await signValue({ exp: 1 }, secret);
  assert.equal(await verifyValue(expired, secret), null);
});

test('Stripe webhook signatures enforce HMAC and timestamp tolerance', async () => {
  const body = JSON.stringify({ id: 'evt_test', type: 'invoice.paid' });
  const secret = 'whsec_unit_test_only';
  const timestamp = 1_800_000_000;
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const raw = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`${timestamp}.${body}`));
  const signature = Buffer.from(raw).toString('hex');
  const header = `t=${timestamp},v1=${signature}`;
  assert.deepEqual(parseStripeSignature(header), { t: String(timestamp), v1: [signature] });
  assert.equal(await verifyStripeSignature(body, header, secret, timestamp), true);
  assert.equal(await verifyStripeSignature(`${body}x`, header, secret, timestamp), false);
  assert.equal(await verifyStripeSignature(body, header, secret, timestamp + 301), false);
});

test('constant-time helper rejects unequal values', () => {
  assert.equal(constantTimeEqual('abc', 'abc'), true);
  assert.equal(constantTimeEqual('abc', 'abd'), false);
  assert.equal(constantTimeEqual('abc', 'ab'), false);
});

test('checkout remains fail-closed until all production secrets exist', () => {
  assert.equal(checkoutConfigured(unavailableEnv), false);
  assert.equal(checkoutConfigured({
    CHECKOUT_PROVIDER: 'stripe', STRIPE_MODE: 'test', STRIPE_PUBLISHABLE_KEY: 'pk_test_public', STRIPE_PRICE_ID: 'price_test',
    STRIPE_SECRET_KEY: 'sk_test_fixture_only', STRIPE_WEBHOOK_SECRET: 'webhook', DISCORD_CLIENT_ID: 'client',
    DISCORD_CLIENT_SECRET: 'discord-secret', SESSION_SECRET: 'session-secret', ORION_API_BASE: 'https://api.example',
    ORION_BOT_SECRET: 'bot-secret', ORION_EDGE_AUTH: 'edge-secret',
  }), true);
});

test('live checkout rejects mixed Stripe modes and fails closed', async () => {
  assert.equal(checkoutConfigured(liveStripeEnv), true);
  assert.equal(checkoutConfigured({ ...liveStripeEnv, STRIPE_SECRET_KEY: 'sk_live_fixture_only' }), true);
  const mixed = { ...liveStripeEnv, STRIPE_SECRET_KEY: stripeEnv.STRIPE_SECRET_KEY };
  assert.equal(checkoutConfigured(mixed), false);
  assert.equal(checkoutConfigured({ ...stripeEnv, STRIPE_MODE: 'live' }), false);
  const config = await worker.fetch(new Request('https://zaeorion.com/api/checkout/config'), mixed);
  assert.deepEqual(await config.json(), {
    provider: 'unavailable', configured: false, publishableKey: '', authenticated: false, username: '',
    trial: true, inGuild: false, membershipUnknown: false, joinUrl: 'https://discord.gg/yTuekgdgEN',
    avatarUrl: '',
  });
  const buy = await worker.fetch(new Request('https://zaeorion.com/buy'), mixed);
  assert.equal(buy.status, 503);
  assert.equal(buy.headers.get('location'), null);
  const session = await worker.fetch(new Request('https://zaeorion.com/api/checkout/session', { method: 'POST' }), mixed);
  assert.equal(session.status, 503);
});

test('Stripe Checkout uses the current embedded page mode', async () => {
  const source = await readFile(new URL('../src/worker.js', import.meta.url), 'utf8');
  assert.match(source, /ui_mode:\s*["']embedded_page["']/);
  assert.doesNotMatch(source, /ui_mode:\s*["']embedded["']/);
});

test('unconfigured Worker exposes no checkout secrets and fails closed', async () => {
  const config = await worker.fetch(new Request('https://zaeorion.com/api/checkout/config'), unavailableEnv);
  assert.equal(config.status, 200);
  assert.deepEqual(await config.json(), {
    provider: 'unavailable', configured: false, publishableKey: '', authenticated: false, username: '',
    trial: false, inGuild: false, membershipUnknown: false, joinUrl: '',
    avatarUrl: '',
  });
  const buy = await worker.fetch(new Request('https://zaeorion.com/buy'), unavailableEnv);
  assert.equal(buy.status, 503);
  assert.equal(buy.headers.get('location'), null);
});

test('Discord OAuth starts with a signed secure state cookie', async () => {
  const response = await worker.fetch(
    new Request('https://zaeorion.com/auth/discord?return_to=%2F%23pricing'),
    stripeEnv,
  );
  assert.equal(response.status, 302);
  const target = new URL(response.headers.get('location'));
  assert.equal(target.origin, 'https://discord.com');
  assert.equal(target.pathname, '/oauth2/authorize');
  assert.equal(target.searchParams.get('client_id'), stripeEnv.DISCORD_CLIENT_ID);
  assert.equal(target.searchParams.get('redirect_uri'), 'https://zaeorion.com/auth/discord/callback');
  assert.match(response.headers.get('set-cookie'), /^venice_oauth=.*; Path=\/; HttpOnly; Secure; SameSite=Lax;/);
});

test('/discord signs in through OAuth and shows only the verified Discord profile', async () => {
  const unsigned = await worker.fetch(new Request('https://zaeorion.com/discord?discord_id=999999999999999999'), stripeEnv);
  assert.equal(unsigned.status, 302);
  assert.equal(new URL(unsigned.headers.get('location')).pathname, '/auth/discord');
  assert.equal(new URL(unsigned.headers.get('location')).searchParams.get('return_to'), '/discord');
  assert.doesNotMatch(unsigned.headers.get('location'), /discord\.gg/u);

  const begin = await worker.fetch(new Request('https://zaeorion.com/auth/discord?return_to=%2Fdiscord'), stripeEnv);
  const state = new URL(begin.headers.get('location')).searchParams.get('state');
  const oauthCookie = begin.headers.get('set-cookie').split(';', 1)[0];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const path = new URL(input).pathname;
    if (path === '/api/v10/oauth2/token') return Response.json({ access_token: 'oauth-fixture' });
    if (path === '/api/v10/users/@me') return Response.json({
      id: '123456789012345678', global_name: '<Venice User>', username: 'venice-user',
      avatar: '0123456789abcdef0123456789abcdef',
    });
    if (path === '/api/bot/guild-join') return Response.json({ ok: true, result: 'already_member' });
    throw Error(`unexpected path ${path}`);
  };
  let callback;
  try {
    callback = await worker.fetch(new Request(`https://zaeorion.com/auth/discord/callback?state=${state}&code=oauth-code`, {
      headers: { Cookie: oauthCookie },
    }), stripeEnv);
  } finally { globalThis.fetch = originalFetch; }
  assert.equal(callback.status, 302);
  assert.equal(callback.headers.get('location'), 'https://zaeorion.com/discord');
  const sessionCookie = callback.headers.get('set-cookie').split(';', 1)[0];
  const profile = await worker.fetch(new Request('https://zaeorion.com/discord?discord_id=999999999999999999', {
    headers: { Cookie: sessionCookie },
  }), stripeEnv);
  assert.equal(profile.status, 200);
  assert.equal(profile.headers.get('cache-control'), 'private, no-store');
  assert.equal(profile.headers.get('referrer-policy'), 'no-referrer');
  const html = await profile.text();
  assert.match(html, /Discord connected\./u);
  assert.match(html, /&lt;Venice User&gt;/u);
  assert.match(html, /Discord ID 123456789012345678/u);
  assert.match(html, /cdn\.discordapp\.com\/avatars\/123456789012345678/u);
  assert.match(html, /Get your one-time code/u);
  assert.doesNotMatch(html, /999999999999999999|discord\.gg|claim_trial|Connect launcher/u);

  globalThis.fetch = async (input) => {
    if (new URL(input).pathname === '/api/bot/guild-member') return Response.json({ ok: true, member: true });
    throw Error('unexpected outbound request');
  };
  let checkout;
  try {
    checkout = await worker.fetch(new Request('https://zaeorion.com/api/checkout/config', {
      headers: { Cookie: sessionCookie },
    }), stripeEnv);
  } finally { globalThis.fetch = originalFetch; }
  const checkoutConfig = await checkout.json();
  assert.equal(checkoutConfig.authenticated, true);
  assert.equal(checkoutConfig.inGuild, true);
  const logout = await worker.fetch(new Request('https://zaeorion.com/logout', {
    method: 'POST', headers: { Cookie: sessionCookie },
  }), stripeEnv);
  assert.equal(logout.status, 303);
  assert.equal(logout.headers.get('location'), 'https://zaeorion.com/discord');
  assert.match(logout.headers.get('set-cookie'), /Max-Age=0/u);
});

test('/discord never falls back to a server invite', async () => {
  const unavailable = await worker.fetch(new Request('https://zaeorion.com/discord'), unavailableEnv);
  assert.equal(unavailable.status, 503);
  assert.doesNotMatch(await unavailable.text(), /discord\.gg/u);
});

test('launcher connection requires a signed Discord session and never trusts a copied ID', async () => {
  const unauthenticated = await worker.fetch(new Request('https://zaeorion.com/connect?discord_id=999999999999999999'), stripeEnv);
  assert.equal(unauthenticated.status, 302);
  assert.equal(new URL(unauthenticated.headers.get('location')).pathname, '/auth/discord');

  const originalFetch = globalThis.fetch;
  const calls = [];
  const pairCode = `PAIR-${'A'.repeat(32)}`;
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, body: JSON.parse(init.body), headers: init.headers });
    if (path === '/api/bot/pair-status') return Response.json({ ok: true, ready: true });
    return Response.json({ ok: true, pair_code: pairCode, expires: Math.floor(Date.now() / 1000) + 300 });
  };
  try {
    const signed = await signValue({ discordId: '123456789012345678', username: '<Venice>',
      exp: Math.floor(Date.now() / 1000) + 60 }, stripeEnv.SESSION_SECRET);
    const response = await worker.fetch(new Request('https://zaeorion.com/connect?discord_id=999999999999999999',
      { headers: { Cookie: `venice_session=${signed}` } }), stripeEnv);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'private, no-store');
    assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
    assert.deepEqual(calls.map(({ path, body }) => ({ path, body })), [
      { path: '/api/bot/pair-status', body: { discord_id: '123456789012345678' } },
      { path: '/api/bot/pair-issue', body: { discord_id: '123456789012345678' } },
    ]);
    assert.equal(calls[0].headers['X-Orion-Pair-Secret'], stripeEnv.PAIR_ISSUER_SECRET);
    assert.equal(calls[0].headers['X-Orion-Bot-Secret'], undefined);
    const html = await response.text();
    assert.match(html, /&lt;Venice&gt;/u);
    assert.doesNotMatch(html, /999999999999999999/u);
    assert.match(html, new RegExp(`orion://activate\\?key=${pairCode}`));
  } finally { globalThis.fetch = originalFetch; }
});

test('missing pair issuer secret disables connection without disabling checkout sign-in', async () => {
  const { PAIR_ISSUER_SECRET, ...withoutPairSecret } = stripeEnv;
  const connection = await worker.fetch(new Request('https://zaeorion.com/connect'), withoutPairSecret);
  assert.equal(connection.status, 503);
  assert.equal(checkoutConfigured(withoutPairSecret), true);
  const oauth = await worker.fetch(new Request('https://zaeorion.com/auth/discord'), withoutPairSecret);
  assert.equal(oauth.status, 302);
});

test('launcher connection fails closed when entitlement is absent', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ error: 'subscription_required' }, { status: 403 });
  try {
    const signed = await signValue({ discordId: '123456789012345678', exp: Math.floor(Date.now() / 1000) + 60 }, stripeEnv.SESSION_SECRET);
    const response = await worker.fetch(new Request('https://zaeorion.com/connect',
      { headers: { Cookie: `venice_session=${signed}` } }), stripeEnv);
    assert.equal(response.status, 403);
    assert.equal(response.headers.get('cache-control'), 'private, no-store');
    assert.doesNotMatch(await response.text(), /orion:\/\/activate/u);
  } finally { globalThis.fetch = originalFetch; }
});

test('health and static asset responses carry security headers', async () => {
  const health = await worker.fetch(new Request('https://zaeorion.com/health'), unavailableEnv);
  assert.equal(health.status, 200);
  assert.equal(health.headers.get('x-content-type-options'), 'nosniff');
  assert.match(health.headers.get('content-security-policy'), /frame-src https:\/\/js\.stripe\.com/);
  const asset = await worker.fetch(new Request('https://zaeorion.com/'), unavailableEnv);
  assert.equal(await asset.text(), 'asset');
  assert.equal(asset.headers.get('x-frame-options'), 'DENY');
});

// ── Website trial, server-membership gate and login auto-join (2026-09-19) ──

const MEMBER_ID = '123456789012345678';
const OTHER_ID = '999999999999999999';
const ACCESS_TOKEN = 'oauthAccessTokenFixture7Q9xZ2kLm4';
const INVITE = stripeEnv.DISCORD_INVITE_URL;
const TRIAL_NEUTRAL = "We couldn't start your trial right now. Please try again later, or open a ticket in the Venice Discord.";

async function sessionCookieFor(discordId = MEMBER_ID, env = stripeEnv) {
  const signed = await signValue({ discordId, username: 'Venice', exp: Math.floor(Date.now() / 1000) + 600 },
    env.SESSION_SECRET);
  return `venice_session=${signed}`;
}

function parseBody(body) {
  if (typeof body === 'string') { try { return JSON.parse(body); } catch { return body; } }
  if (body instanceof URLSearchParams) return Object.fromEntries(body);
  return body;
}

// Replaces fetch for one test; every outbound call is recorded. The handler
// returns a Response or throws (a network failure).
function installFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const target = new URL(input);
    const body = parseBody(init.body);
    calls.push({ host: target.host, path: target.pathname, body, headers: init.headers || {} });
    return handler(target.pathname, body, init);
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

function captureConsole() {
  const lines = [];
  const originals = {};
  for (const level of ['log', 'info', 'warn', 'error', 'debug']) {
    originals[level] = console[level];
    console[level] = (...args) => { lines.push(args.map((arg) => (typeof arg === 'string' ? arg : JSON.stringify(arg))).join(' ')); };
  }
  return { lines, restore: () => Object.assign(console, originals) };
}

async function withMocks(handler, run) {
  const fetchMock = installFetch(handler);
  const logs = captureConsole();
  try {
    return await run(fetchMock.calls, logs.lines);
  } finally {
    logs.restore();
    fetchMock.restore();
  }
}

function trialRequest({ cookie, origin = 'https://zaeorion.com', contentType = 'application/json',
  body = '{}', url = 'https://zaeorion.com/api/trial' } = {}) {
  const headers = {};
  if (cookie) headers.Cookie = cookie;
  if (origin !== null) headers.Origin = origin;
  if (contentType !== null) headers['Content-Type'] = contentType;
  return new Request(url, { method: 'POST', headers, body });
}

const memberOk = () => Response.json({ ok: true, member: true });
const trialIssued = () => Response.json({ ok: true, code: 'issued', plan: 'trial', expiry: 1,
  message: '✅ Your 7-day trial is active. Check your DMs to connect Discord.' });

test('/api/trial claims the trial for the signed-in, in-server Discord account', async () => {
  const cookie = await sessionCookieFor();
  await withMocks((path) => {
    if (path === '/api/bot/guild-member') return memberOk();
    if (path === '/api/bot/trial') return trialIssued();
    throw Error(`unexpected path ${path}`);
  }, async (calls, logs) => {
    const response = await worker.fetch(trialRequest({ cookie }), stripeEnv);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await response.json(), { ok: true, code: 'issued',
      message: 'Your 7-day trial is active. Check your Discord DMs to connect Venice.' });
    assert.deepEqual(calls.map(({ path, body }) => ({ path, body })), [
      { path: '/api/bot/guild-member', body: { discord_id: MEMBER_ID, purpose: 'trial' } },
      { path: '/api/bot/trial', body: { discord_id: MEMBER_ID, actor_discord_id: MEMBER_ID } },
    ]);
    for (const call of calls) {
      assert.equal(call.host, 'api.example');
      assert.equal(call.headers['X-Orion-Bot-Secret'], stripeEnv.ORION_BOT_SECRET);
      assert.equal(call.headers['X-Edge-Auth'], stripeEnv.ORION_EDGE_AUTH);
    }
    const line = logs.find((entry) => entry.includes('"site_trial"'));
    assert.ok(line, 'structured site_trial log line');
    assert.deepEqual(JSON.parse(line), { event: 'site_trial', result: 'issued', backendStatus: 200,
      backendCode: 'issued', discord: '...5678' });
    assert.ok(logs.every((entry) => !entry.includes(MEMBER_ID)), 'full Discord ID never logged');
  });
});

test('/api/trial requires a signed Discord session', async () => {
  await withMocks(() => { throw Error('no backend call expected'); }, async (calls) => {
    for (const cookie of [undefined, 'venice_session=forged.value']) {
      const response = await worker.fetch(trialRequest({ cookie }), stripeEnv);
      assert.equal(response.status, 401);
      const body = await response.json();
      assert.equal(body.ok, false);
      assert.equal(body.code, 'auth_required');
      assert.equal(body.authUrl, '/auth/discord');
      assert.ok(body.error);
    }
    assert.equal(calls.length, 0);
  });
});

test('/api/trial rejects cross-site origins and non-JSON requests before touching the backend', async () => {
  const cookie = await sessionCookieFor();
  await withMocks(() => { throw Error('no backend call expected'); }, async (calls) => {
    for (const origin of ['https://evil.example', 'https://zaeorion.com.evil.example', 'null', '', null]) {
      const response = await worker.fetch(trialRequest({ cookie, origin }), stripeEnv);
      assert.equal(response.status, 403, `origin ${origin}`);
      assert.equal((await response.json()).code, 'forbidden_origin');
    }
    for (const contentType of ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data', null]) {
      const response = await worker.fetch(trialRequest({ cookie, contentType }), stripeEnv);
      assert.equal(response.status, 415, `content-type ${contentType}`);
      assert.equal((await response.json()).code, 'unsupported_media_type');
    }
    const get = await worker.fetch(new Request('https://zaeorion.com/api/trial', { headers: { Cookie: cookie } }), stripeEnv);
    assert.equal(get.status, 405);
    assert.equal(get.headers.get('allow'), 'POST');
    assert.equal(calls.length, 0);
  });
  // The request's own origin is accepted (local `wrangler dev`, preview URLs).
  await withMocks((path) => {
    if (path === '/api/bot/guild-member') return memberOk();
    if (path === '/api/bot/trial') return trialIssued();
    throw Error(`unexpected path ${path}`);
  }, async () => {
    const local = await worker.fetch(trialRequest({ cookie, origin: 'http://localhost:8787',
      url: 'http://localhost:8787/api/trial', contentType: 'application/json; charset=utf-8' }), stripeEnv);
    assert.equal(local.status, 200);
  });
});

test('/api/trial ignores any Discord ID in the body or query string', async () => {
  const cookie = await sessionCookieFor();
  await withMocks((path) => {
    if (path === '/api/bot/guild-member') return memberOk();
    if (path === '/api/bot/trial') return trialIssued();
    throw Error(`unexpected path ${path}`);
  }, async (calls) => {
    const response = await worker.fetch(trialRequest({ cookie,
      url: `https://zaeorion.com/api/trial?discord_id=${OTHER_ID}`,
      body: JSON.stringify({ discord_id: OTHER_ID, actor_discord_id: OTHER_ID, discordId: OTHER_ID }) }), stripeEnv);
    assert.equal(response.status, 200);
    assert.equal(calls.length, 2);
    assert.doesNotMatch(JSON.stringify(calls), new RegExp(OTHER_ID));
    assert.equal(calls[1].body.discord_id, MEMBER_ID);
  });
});

test('/api/trial maps every backend result to site copy, never the Discord wording', async () => {
  const cookie = await sessionCookieFor();
  const cases = [
    { name: 'issued', reply: trialIssued, status: 200, code: 'issued', ok: true,
      message: 'Your 7-day trial is active. Check your Discord DMs to connect Venice.' },
    { name: 'already claimed', status: 409, code: 'already_claimed',
      reply: () => Response.json({ ok: false, code: 'already_claimed', message: "You've already claimed your free trial." }),
      message: "You've already used your free trial. Subscribe to keep going." },
    { name: 'already claimed (backend without codes)', status: 409, code: 'already_claimed',
      reply: () => Response.json({ ok: false, message: "You've already claimed your free trial." }),
      message: "You've already used your free trial. Subscribe to keep going." },
    { name: 'DM bounced', status: 422, code: 'dm_failed',
      reply: () => Response.json({ ok: false, code: 'dm_failed',
        message: "I couldn't DM you. Turn on **Privacy Settings → Direct Messages** for this server, then run `/claim_trial` again." }),
      message: "We couldn't DM you. In the Venice server, open Privacy Settings and allow Direct Messages, then try again." },
    { name: 'DM bounced (backend without codes)', status: 422, code: 'dm_failed',
      reply: () => Response.json({ ok: false,
        message: "I couldn't DM you. Turn on **Privacy Settings → Direct Messages** for this server, then run `/claim_trial` again." }),
      message: "We couldn't DM you. In the Venice server, open Privacy Settings and allow Direct Messages, then try again." },
    { name: 'rate limited', status: 429, code: 'rate_limited',
      reply: () => Response.json({ ok: false, error: 'rate_limited', code: 'rate_limited' }, { status: 429 }),
      message: 'Too many attempts. Wait a few minutes and try again.' },
    { name: 'blacklisted', status: 503, code: 'unavailable',
      reply: () => Response.json({ ok: false, error: 'blacklisted', code: 'blacklisted', message: 'This account is blocked.' }, { status: 403 }),
      message: TRIAL_NEUTRAL, backendCode: 'blacklisted' },
    { name: 'backend auth failure', status: 503, code: 'unavailable',
      reply: () => Response.json({ ok: false, error: 'forbidden' }, { status: 403 }), message: TRIAL_NEUTRAL },
    { name: 'backend outage', status: 503, code: 'unavailable',
      reply: () => new Response('Internal Server Error', { status: 500 }), message: TRIAL_NEUTRAL },
    { name: 'network failure', status: 503, code: 'unavailable',
      reply: () => { throw new TypeError('fetch failed'); }, message: TRIAL_NEUTRAL },
  ];
  for (const scenario of cases) {
    await withMocks((path) => {
      if (path === '/api/bot/guild-member') return memberOk();
      if (path === '/api/bot/trial') return scenario.reply();
      throw Error(`unexpected path ${path}`);
    }, async (calls, logs) => {
      const response = await worker.fetch(trialRequest({ cookie }), stripeEnv);
      assert.equal(response.status, scenario.status, scenario.name);
      const body = await response.json();
      assert.deepEqual(body, { ok: scenario.ok === true, code: scenario.code, message: scenario.message }, scenario.name);
      assert.doesNotMatch(body.message, /claim_trial|\*\*|blocked/u, scenario.name);
      const line = JSON.parse(logs.find((entry) => entry.includes('"site_trial"')));
      assert.equal(line.result, scenario.code, scenario.name);
      assert.equal(line.discord, '...5678');
      if (scenario.backendCode) assert.equal(line.backendCode, scenario.backendCode);
    });
  }
});


test('non-members get join_required on trial and checkout with no trial claim and no Stripe call', async () => {
  const cookie = await sessionCookieFor();
  await withMocks((path) => {
    if (path === '/api/bot/guild-member') return Response.json({ ok: true, member: false });
    throw Error(`unexpected path ${path}`);
  }, async (calls) => {
    const trial = await worker.fetch(trialRequest({ cookie }), stripeEnv);
    assert.equal(trial.status, 403);
    assert.deepEqual(await trial.json(), { ok: false, code: 'join_required', error: 'join_required',
      message: 'Join the Venice Discord server first — Venice confirms your access and sends your setup steps there by DM.', joinUrl: INVITE });

    const checkout = await worker.fetch(new Request('https://zaeorion.com/api/checkout/session', {
      method: 'POST', headers: { Cookie: cookie } }), liveStripeEnv);
    assert.equal(checkout.status, 403);
    assert.deepEqual(await checkout.json(), { error: 'join_required',
      message: 'Join the Venice Discord server first — Venice confirms your access and sends your setup steps there by DM.', joinUrl: INVITE });

    assert.deepEqual(calls.map(({ path, body }) => ({ path, body })), [
      { path: '/api/bot/guild-member', body: { discord_id: MEMBER_ID, purpose: 'trial' } },
      { path: '/api/bot/guild-member', body: { discord_id: MEMBER_ID, purpose: 'checkout' } },
    ]);
    assert.ok(calls.every((call) => call.host === 'api.example'), 'no Stripe or Discord call');
  });
});

test('a membership-check outage fails closed on trial and checkout', async () => {
  const cookie = await sessionCookieFor();
  const outages = [
    () => Response.json({ ok: false, error: 'membership_unavailable', discord_status: 0 }, { status: 502 }),
    () => Response.json({ ok: false, error: 'forbidden' }, { status: 403 }),
    () => Response.json({ ok: true }),
    () => new Response('not json', { status: 200 }),
    () => { throw new TypeError('fetch failed'); },
  ];
  for (const outage of outages) {
    await withMocks((path) => {
      if (path === '/api/bot/guild-member') return outage();
      throw Error(`unexpected path ${path}`);
    }, async (calls) => {
      const trial = await worker.fetch(trialRequest({ cookie }), stripeEnv);
      assert.equal(trial.status, 503);
      assert.deepEqual(await trial.json(), { ok: false, code: 'membership_unavailable', error: 'membership_unavailable',
        message: "We couldn't confirm your Discord membership. Please try again in a minute." });
      const checkout = await worker.fetch(new Request('https://zaeorion.com/api/checkout/session', {
        method: 'POST', headers: { Cookie: cookie } }), liveStripeEnv);
      assert.equal(checkout.status, 503);
      assert.deepEqual(await checkout.json(), { error: 'membership_unavailable',
        message: "We couldn't confirm your Discord membership. Please try again in a minute." });
      assert.deepEqual(calls.map((call) => call.path), ['/api/bot/guild-member', '/api/bot/guild-member']);
    });
  }
});

test('members proceed to Stripe Checkout stamped with the session Discord ID', async () => {
  const cookie = await sessionCookieFor();
  await withMocks((path) => {
    if (path === '/api/bot/guild-member') return memberOk();
    if (path === '/v1/checkout/sessions') return Response.json({ id: 'cs_test_member', client_secret: 'cs_secret_fixture' });
    throw Error(`unexpected path ${path}`);
  }, async (calls) => {
    const response = await worker.fetch(new Request(`https://zaeorion.com/api/checkout/session?discord_id=${OTHER_ID}`, {
      method: 'POST', headers: { Cookie: cookie } }), liveStripeEnv);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { clientSecret: 'cs_secret_fixture' });
    assert.deepEqual(calls.map((call) => call.path), ['/api/bot/guild-member', '/v1/checkout/sessions']);
    assert.equal(calls[1].host, 'api.stripe.com');
    assert.equal(calls[1].body['metadata[discord_user_id]'], MEMBER_ID);
    assert.equal(calls[1].body.client_reference_id, MEMBER_ID);
    assert.equal(calls[1].headers['Stripe-Version'], '2026-08-26.dahlia');
    // Card only: delayed-settlement methods complete as "unpaid" and would never be provisioned.
    assert.equal(calls[1].body['payment_method_types[0]'], 'card');
    assert.deepEqual(Object.keys(calls[1].body).filter((key) => key.startsWith('payment_method_types')),
      ['payment_method_types[0]']);
    // Stripe Tax is not activated on the live account; re-enabling automatic_tax requires activating it in the live dashboard first.
    assert.ok(Object.keys(calls[1].body).every((key) => !key.startsWith('automatic_tax')));
    assert.doesNotMatch(JSON.stringify(calls), new RegExp(OTHER_ID));
  });
  // Signed-out checkout is still refused before any outbound call.
  await withMocks(() => { throw Error('no outbound call expected'); }, async (calls) => {
    const response = await worker.fetch(new Request('https://zaeorion.com/api/checkout/session', { method: 'POST' }), liveStripeEnv);
    assert.equal(response.status, 401);
    assert.equal(calls.length, 0);
  });
});

test('beta coupon is applied at checkout only when a well-formed coupon id is configured', async () => {
  const cookie = await sessionCookieFor();
  const cases = [
    [undefined, undefined],
    ['', undefined],
    ['venice-beta-3mo', 'venice-beta-3mo'],
    ['bad coupon&x=1', undefined],
  ];
  for (const [configured, expected] of cases) {
    const env = configured === undefined ? liveStripeEnv : { ...liveStripeEnv, STRIPE_BETA_COUPON: configured };
    await withMocks((path) => {
      if (path === '/api/bot/guild-member') return memberOk();
      if (path === '/v1/checkout/sessions') return Response.json({ id: 'cs_test_beta', client_secret: 'cs_secret_fixture' });
      throw Error(`unexpected path ${path}`);
    }, async (calls) => {
      const response = await worker.fetch(new Request('https://zaeorion.com/api/checkout/session', {
        method: 'POST', headers: { Cookie: cookie } }), env);
      assert.equal(response.status, 200);
      assert.equal(calls[1].body['discounts[0][coupon]'], expected);
      assert.equal(calls[1].body.allow_promotion_codes, undefined);
    });
  }
});

test('checkout config exposes the connected Discord avatar without leaking the id', async () => {
  // [2026-09-19] The header shows the connected account. The URL is built server-side from
  // the signed session so the page never receives the raw Discord id, and a forged or absent
  // avatar hash must degrade to "" rather than produce a URL.
  const hash = 'a'.repeat(32);
  const cases = [
    { avatar: hash, expect: `https://cdn.discordapp.com/avatars/${MEMBER_ID}/${hash}.png?size=64` },
    { avatar: `a_${hash}`, expect: `https://cdn.discordapp.com/avatars/${MEMBER_ID}/a_${hash}.png?size=64` },
    { avatar: '', expect: '' },
    { avatar: '../../evil', expect: '' },
    { avatar: 'ZZZZ' + 'a'.repeat(28), expect: '' },
  ];
  for (const { avatar, expect } of cases) {
    const signed = await signValue(
      { discordId: MEMBER_ID, username: 'Venice', avatar, exp: Math.floor(Date.now() / 1000) + 600 },
      stripeEnv.SESSION_SECRET);
    await withMocks((path) => {
      if (path === '/api/bot/guild-member') return Response.json({ ok: true, member: true });
      throw Error(`unexpected path ${path}`);
    }, async () => {
      const response = await worker.fetch(
        new Request('https://zaeorion.com/api/checkout/config', { headers: { Cookie: `venice_session=${signed}` } }),
        stripeEnv);
      const body = await response.json();
      assert.equal(body.avatarUrl, expect);
      assert.equal(body.username, 'Venice');
      // the raw id must never be handed to the page except inside the avatar URL itself
      const withoutAvatar = { ...body, avatarUrl: '' };
      assert.doesNotMatch(JSON.stringify(withoutAvatar), new RegExp(MEMBER_ID));
    });
  }
});

test('checkout config reports the trial and live server membership', async () => {
  const cookie = await sessionCookieFor();
  const config = (headers = {}) => worker.fetch(new Request('https://zaeorion.com/api/checkout/config', { headers }), stripeEnv);
  const scenarios = [
    { reply: () => Response.json({ ok: true, member: true }), inGuild: true, membershipUnknown: false },
    { reply: () => Response.json({ ok: true, member: false }), inGuild: false, membershipUnknown: false },
    { reply: () => Response.json({ ok: false, error: 'membership_unavailable' }, { status: 502 }), inGuild: false, membershipUnknown: true },
    { reply: () => { throw new TypeError('fetch failed'); }, inGuild: false, membershipUnknown: true },
  ];
  for (const scenario of scenarios) {
    await withMocks((path) => {
      if (path === '/api/bot/guild-member') return scenario.reply();
      throw Error(`unexpected path ${path}`);
    }, async (calls) => {
      const response = await config({ Cookie: cookie });
      const body = await response.json();
      assert.equal(response.headers.get('cache-control'), 'no-store');
      assert.equal(body.authenticated, true);
      assert.equal(body.trial, true);
      assert.equal(body.inGuild, scenario.inGuild);
      assert.equal(body.membershipUnknown, scenario.membershipUnknown);
      assert.equal(body.joinUrl, INVITE);
      assert.deepEqual(calls.map(({ path, body: sent }) => ({ path, body: sent })),
        [{ path: '/api/bot/guild-member', body: { discord_id: MEMBER_ID, purpose: 'config' } }]);
    });
  }
  await withMocks(() => { throw Error('signed-out config must not call the backend'); }, async (calls) => {
    const body = await (await config()).json();
    assert.equal(body.authenticated, false);
    assert.equal(body.inGuild, false);
    assert.equal(body.membershipUnknown, false);
    assert.equal(calls.length, 0);
  });
  const { ORION_BOT_SECRET, ...withoutBotSecret } = stripeEnv;
  const disabled = await worker.fetch(new Request('https://zaeorion.com/api/checkout/config'), withoutBotSecret);
  assert.equal((await disabled.json()).trial, false);
  const offline = await worker.fetch(trialRequest({ cookie }), withoutBotSecret);
  assert.equal(offline.status, 503);
});

test('Discord sign-in asks for identify and guilds.join', async () => {
  const response = await worker.fetch(new Request('https://zaeorion.com/auth/discord'), stripeEnv);
  const target = new URL(response.headers.get('location'));
  assert.equal(target.searchParams.get('scope'), 'identify guilds.join');
  assert.equal(target.searchParams.get('response_type'), 'code');
});

async function discordLogin(env, backendReply, tokenExtra = {}) {
  const begin = await worker.fetch(new Request('https://zaeorion.com/auth/discord?return_to=%2F%23pricing'), env);
  const state = new URL(begin.headers.get('location')).searchParams.get('state');
  const oauthCookie = begin.headers.get('set-cookie').split(';', 1)[0];
  return withMocks((path) => {
    if (path === '/api/v10/oauth2/token') return Response.json({ access_token: ACCESS_TOKEN, token_type: 'Bearer',
      expires_in: 604800, refresh_token: 'refreshTokenFixture', scope: 'identify guilds.join', ...tokenExtra });
    if (path === '/api/v10/users/@me') return Response.json({ id: MEMBER_ID, username: 'venice-user' });
    if (path === '/api/bot/guild-join') return backendReply();
    throw Error(`unexpected path ${path}`);
  }, async (calls, logs) => {
    const response = await worker.fetch(new Request(
      `https://zaeorion.com/auth/discord/callback?state=${state}&code=oauth-code`, { headers: { Cookie: oauthCookie } }), env);
    const cookies = response.headers.getSetCookie();
    const sessionValue = cookies.find((value) => value.startsWith('venice_session=')).split(';', 1)[0].slice('venice_session='.length);
    const payload = JSON.parse(Buffer.from(sessionValue.split('.')[0], 'base64url').toString('utf8'));
    return { response, calls, logs, cookies, payload };
  });
}

test('sign-in joins the Venice server once and records the result in the session', async () => {
  for (const [reply, guildJoin, inGuild] of [
    [() => Response.json({ ok: true, result: 'joined' }, { status: 200 }), 'joined', true],
    [() => Response.json({ ok: true, result: 'already_member' }), 'already_member', true],
  ]) {
    const { response, calls, payload } = await discordLogin(stripeEnv, reply);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('location'), 'https://zaeorion.com/#pricing');
    assert.equal(payload.discordId, MEMBER_ID);
    assert.equal(payload.guildJoin, guildJoin);
    assert.equal(payload.inGuild, inGuild);
    const joins = calls.filter((call) => call.path === '/api/bot/guild-join');
    assert.equal(joins.length, 1);
    assert.deepEqual(joins[0].body, { discord_id: MEMBER_ID, access_token: ACCESS_TOKEN });
    assert.equal(joins[0].headers['X-Orion-Bot-Secret'], stripeEnv.ORION_BOT_SECRET);
  }
});

test('a failed server join never breaks the sign-in redirect', async () => {
  const failures = [
    () => Response.json({ ok: false, error: 'guild_join_failed', result: 'failed', discord_status: 403 }, { status: 502 }),
    () => Response.json({ ok: false, error: 'not_found' }, { status: 404 }),
    () => new Response('gateway timeout', { status: 504 }),
    () => { throw new TypeError('fetch failed'); },
    () => { throw new DOMException('The operation was aborted due to timeout', 'TimeoutError'); },
  ];
  for (const failure of failures) {
    const { response, payload, cookies } = await discordLogin(stripeEnv, failure);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('location'), 'https://zaeorion.com/#pricing');
    assert.ok(cookies.some((value) => value.startsWith('venice_session=')));
    assert.ok(cookies.some((value) => value.startsWith('venice_oauth=;') && /Max-Age=0/u.test(value)));
    assert.equal(payload.discordId, MEMBER_ID);
    assert.equal(payload.inGuild, false);
    assert.equal(payload.guildJoin, 'failed');
  }
  // A token granted without guilds.join skips the backend call entirely.
  const scoped = await discordLogin(stripeEnv, () => { throw Error('no join call expected'); }, { scope: 'identify' });
  assert.equal(scoped.response.status, 302);
  assert.equal(scoped.payload.guildJoin, 'failed');
  assert.ok(scoped.calls.every((call) => call.path !== '/api/bot/guild-join'));
  // Without the backend secret the login still works and simply reports not joined.
  const { ORION_BOT_SECRET, ...withoutBotSecret } = stripeEnv;
  const offline = await discordLogin(withoutBotSecret, () => { throw Error('no join call expected'); });
  assert.equal(offline.response.status, 302);
  assert.equal(offline.payload.inGuild, false);
});

test('the OAuth access token never reaches a log line or a cookie', async () => {
  const replies = [
    () => Response.json({ ok: true, result: 'joined' }),
    () => Response.json({ ok: false, error: 'guild_join_failed', result: 'failed' }, { status: 502 }),
    () => { throw new TypeError(`fetch failed ${ACCESS_TOKEN}`); },
  ];
  for (const reply of replies) {
    const { logs, cookies, payload, response } = await discordLogin(stripeEnv, reply);
    assert.ok(logs.some((entry) => entry.includes('"guild_join"')), 'join outcome is logged');
    for (const entry of logs) {
      assert.doesNotMatch(entry, new RegExp(ACCESS_TOKEN));
      assert.doesNotMatch(entry, new RegExp(MEMBER_ID));
    }
    for (const value of cookies) assert.doesNotMatch(value, new RegExp(ACCESS_TOKEN));
    assert.doesNotMatch(JSON.stringify(payload), /oauthAccessToken|refreshToken/u);
    assert.equal(response.headers.get('location'), 'https://zaeorion.com/#pricing');
  }
});

// ── Stripe API 2026-08-26.dahlia (live audit B3, 2026-09-19) ──

test('a dahlia-shaped renewal invoice with no `paid` field provisions the renewal', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, headers: init?.headers || {}, body: init?.body && JSON.parse(init.body) });
    if (path === '/v1/subscriptions/sub_test1') return Response.json(subscription);
    if (path === '/api/bot/provision') return Response.json({ ok: true });
    throw Error(`unexpected path ${path}`);
  };
  try {
    // Real basil+ shape: `status` replaces the removed boolean `paid`, and the
    // subscription lives under parent.subscription_details.
    const invoice = {
      id: 'in_dahlia1', object: 'invoice', livemode: true, status: 'paid', billing_reason: 'subscription_cycle',
      amount_due: 2500, amount_paid: 2500, amount_remaining: 0, currency: 'usd', metadata: {},
      parent: { type: 'subscription_details', quote_details: null, subscription_details: {
        subscription: 'sub_test1', metadata: { discord_user_id: '123456789012345678' } } },
      status_transitions: { paid_at: 1_800_000_000 },
    };
    assert.equal('paid' in invoice, false);
    const response = await worker.fetch(await signedStripeEvent({ id: 'evt_dahlia_renew', object: 'event',
      api_version: '2026-08-26.dahlia', livemode: true, type: 'invoice.paid', data: { object: invoice } }), liveStripeEnv);
    assert.equal(response.status, 200);
    assert.deepEqual(calls.map((call) => call.path), ['/v1/subscriptions/sub_test1', '/api/bot/provision']);
    assert.equal(calls[0].headers['Stripe-Version'], '2026-08-26.dahlia');
    assert.deepEqual(calls[1].body, { order_id: 'stripe:invoice:in_dahlia1', discord_user_id: '123456789012345678',
      plan: 'month', days: 30, renew: true, notify: true, subscription_id: 'sub_test1',
      stripe_event_id: 'evt_dahlia_renew' });
  } finally { globalThis.fetch = originalFetch; }
});

test('a renewal invoice whose status is not paid never provisions, whatever legacy fields say', async () => {
  const originalFetch = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async (input) => { paths.push(new URL(input).pathname); throw Error('unexpected outbound request'); };
  try {
    for (const status of ['open', 'draft', 'uncollectible', undefined]) {
      const invoice = { id: 'in_unpaid', object: 'invoice', livemode: true, status, paid: true,
        billing_reason: 'subscription_cycle', amount_paid: 2500,
        parent: { subscription_details: { subscription: 'sub_test1',
          metadata: { discord_user_id: '123456789012345678' } } } };
      const response = await worker.fetch(await signedStripeEvent({ id: 'evt_unpaid', livemode: true,
        type: 'invoice.paid', data: { object: invoice } }), liveStripeEnv);
      assert.equal(response.status, 500, `status ${status}`);
    }
    assert.deepEqual(paths, []);
  } finally { globalThis.fetch = originalFetch; }
});

// ── [2026-09-22 RED TEAM GMC-001] paid-but-not-yet-provisioned race ──

const ACTIVATING_TEXT = 'Payment received — activating your Venice subscription. This usually takes under a minute.';
const ACTIVATING_FALLBACK = 'Still waiting? Open a ticket in Discord with your receipt email — you will not be charged twice.';
// [P-E 2026-09-23 owner decision] The free trial starts on the website home page, not in Discord.
const ENTITLEMENT_TEXT = 'Your Discord account needs an active trial or subscription. Start the free trial on the Venice home page, then try again. Just paid? Activation can take up to a minute — refresh this page.';

async function paidCheckoutCookie(discordId = MEMBER_ID) {
  // Obtained exactly as a customer gets it: /checkout/complete after Stripe confirms the session.
  const { calls, restore } = installFetch((path) => {
    if (path === '/v1/checkout/sessions/cs_test_paid') {
      return Response.json({ id: 'cs_test_paid', status: 'complete', metadata: { discord_user_id: discordId } });
    }
    throw Error(`unexpected path ${path}`);
  });
  try {
    const response = await worker.fetch(new Request('https://zaeorion.com/checkout/complete?session_id=cs_test_paid',
      { headers: { Cookie: await sessionCookieFor(discordId) } }), stripeEnv);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('location'), 'https://zaeorion.com/discord?purchase=complete');
    assert.deepEqual(calls.map(({ path }) => path), ['/v1/checkout/sessions/cs_test_paid']);
    const setCookie = response.headers.get('set-cookie') || '';
    assert.match(setCookie, /^venice_paid=.*; HttpOnly; Secure; SameSite=Lax; Max-Age=1800$/u);
    return setCookie.split(';', 1)[0];
  } finally { restore(); }
}

async function connectWith(cookie, statusReply, path = '/connect', issueReply = () => {
  throw Error('pair-issue must not run before ready');
}) {
  const { calls, restore } = installFetch((requestPath) => {
    if (requestPath === '/api/bot/pair-status') return statusReply();
    if (requestPath === '/api/bot/pair-issue') return issueReply();
    throw Error(`unexpected path ${requestPath}`);
  });
  try {
    const response = await worker.fetch(new Request(`https://zaeorion.com${path}`, { headers: { Cookie: cookie } }), stripeEnv);
    return { response, html: await response.text(), calls };
  } finally { restore(); }
}

const notProvisioned = () => Response.json({ ok: true, ready: false });

test('GMC-001: paid but not yet provisioned shows the activating state with a bounded retry, never a code', async () => {
  const paid = await paidCheckoutCookie();
  const cookie = `${await sessionCookieFor()}; ${paid}`;
  const first = await connectWith(cookie, notProvisioned);
  assert.equal(first.response.status, 202);
  assert.equal(first.response.headers.get('cache-control'), 'private, no-store');
  assert.equal(first.calls.length, 1);
  assert.equal(first.calls[0].path, '/api/bot/pair-status');
  assert.ok(first.html.includes(ACTIVATING_TEXT));
  assert.match(first.html, /<meta http-equiv="refresh" content="5;url=\/connect\?activating=1">/u);
  assert.ok(!first.html.includes(ENTITLEMENT_TEXT));
  assert.doesNotMatch(first.html, /orion:\/\/activate|PAIR-/u);

  const later = await connectWith(cookie, notProvisioned, '/connect?activating=23');
  assert.match(later.html, /url=\/connect\?activating=24/u);

  // After 24 x 5 s the loop stops and the customer gets the ticket fallback.
  for (const path of ['/connect?activating=24', '/connect?activating=999']) {
    const done = await connectWith(cookie, notProvisioned, path);
    assert.equal(done.response.status, 202);
    assert.ok(done.html.includes(ACTIVATING_FALLBACK));
    assert.doesNotMatch(done.html, /http-equiv="refresh"|orion:\/\/activate/u);
  }
});

test('GMC-001: once provisioned the paid customer gets the code and the hint cookie is cleared', async () => {
  const paid = await paidCheckoutCookie();
  const pairCode = `PAIR-${'B'.repeat(32)}`;
  const { response, html } = await connectWith(`${await sessionCookieFor()}; ${paid}`,
    () => Response.json({ ok: true, ready: true }), '/connect?activating=3',
    () => Response.json({ ok: true, pair_code: pairCode }));
  assert.equal(response.status, 200);
  assert.ok(html.includes(`orion://activate?key=${pairCode}`));
  assert.ok(!html.includes(ACTIVATING_TEXT));
  assert.match(response.headers.get('set-cookie') || '', /^venice_paid=; .*Max-Age=0$/u);
});

test('GMC-001: never-paid, forged, or another account\'s paid hint still gets the subscription message', async () => {
  const session = await sessionCookieFor();
  const neverPaid = await connectWith(session, notProvisioned, '/connect?activating=1');
  assert.equal(neverPaid.response.status, 403);
  assert.ok(neverPaid.html.includes(ENTITLEMENT_TEXT));
  assert.ok(!neverPaid.html.includes(ACTIVATING_TEXT));

  const otherPaid = await paidCheckoutCookie(OTHER_ID);
  const forged = `venice_paid=${await signValue({ discordId: MEMBER_ID, paid: true,
    exp: Math.floor(Date.now() / 1000) + 600 }, 'not-the-session-secret')}`;
  for (const hint of [otherPaid, forged, 'venice_paid=garbage']) {
    const result = await connectWith(`${session}; ${hint}`, notProvisioned);
    assert.equal(result.response.status, 403);
    assert.ok(result.html.includes(ENTITLEMENT_TEXT));
    assert.ok(!result.html.includes(ACTIVATING_TEXT));
  }
  // The paid hint is never a session on its own.
  const paid = await paidCheckoutCookie();
  const alone = await worker.fetch(new Request('https://zaeorion.com/connect',
    { headers: { Cookie: paid.replace('venice_paid=', 'venice_session=') } }), stripeEnv);
  assert.equal(alone.status, 302);
  assert.equal(new URL(alone.headers.get('location')).pathname, '/auth/discord');
});

test('GMC-001: an incomplete checkout sets no paid hint', async () => {
  const { restore } = installFetch(() => Response.json({ id: 'cs_test_open', status: 'open',
    metadata: { discord_user_id: MEMBER_ID } }));
  try {
    const response = await worker.fetch(new Request('https://zaeorion.com/checkout/complete?session_id=cs_test_open',
      { headers: { Cookie: await sessionCookieFor() } }), stripeEnv);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('set-cookie'), null);
  } finally { restore(); }
});

test('AUD-A5-001: sixty-second provisioning polls status without spending pair-issue quota', async () => {
  const paid = await paidCheckoutCookie();
  const cookie = `${await sessionCookieFor()}; ${paid}`;
  let elapsedSeconds = 0;
  let issueAttempts = 0;
  let issued = 0;
  let statusPolls = 0;
  const pairCode = `PAIR-${'C'.repeat(32)}`;
  const { calls, restore } = installFetch((path) => {
    if (path === '/api/bot/pair-status') {
      statusPolls += 1;
      return Response.json({ ok: true, ready: elapsedSeconds >= 60 });
    }
    if (path === '/api/bot/pair-issue') {
      issueAttempts += 1;
      if (issueAttempts > 5) return Response.json({ error: 'rate_limited' }, { status: 429 });
      if (elapsedSeconds < 60) return Response.json({ error: 'subscription_required' }, { status: 403 });
      issued += 1;
      return Response.json({ ok: true, pair_code: pairCode });
    }
    throw Error(`unexpected path ${path}`);
  });
  try {
    for (let attempt = 0; attempt <= 12; attempt += 1) {
      elapsedSeconds = attempt * 5;
      const response = await worker.fetch(new Request(
        `https://zaeorion.com/connect?activating=${attempt}`, { headers: { Cookie: cookie } }), stripeEnv);
      const html = await response.text();
      if (elapsedSeconds < 60) {
        assert.equal(response.status, 202, `at ${elapsedSeconds} s`);
        assert.match(html, /http-equiv="refresh"/u);
      } else {
        assert.equal(response.status, 200);
        assert.match(html, /orion:\/\/activate/u);
      }
    }
    assert.equal(issued, 1);
    assert.equal(issueAttempts, 1);
    assert.equal(statusPolls, 13);
    assert.equal(calls.filter((call) => call.path === '/api/bot/pair-issue').length, 1);
  } finally { restore(); }
});

test('AUD-A5-001: two tabs can poll ninety seconds without minting before readiness', async () => {
  const paid = await paidCheckoutCookie();
  const cookie = `${await sessionCookieFor()}; ${paid}`;
  let elapsedSeconds = 0;
  let issueCalls = 0;
  const { calls, restore } = installFetch((path) => {
    if (path === '/api/bot/pair-status') {
      return Response.json({ ok: true, ready: elapsedSeconds >= 90 });
    }
    if (path === '/api/bot/pair-issue') {
      issueCalls += 1;
      return Response.json({ ok: true, pair_code: `PAIR-${'D'.repeat(32)}` });
    }
    throw Error(`unexpected path ${path}`);
  });
  try {
    for (let attempt = 0; attempt < 18; attempt += 1) {
      elapsedSeconds = attempt * 5;
      for (let tab = 0; tab < 2; tab += 1) {
        const response = await worker.fetch(new Request(
          `https://zaeorion.com/connect?activating=${attempt}`, { headers: { Cookie: cookie } }), stripeEnv);
        assert.equal(response.status, 202);
      }
    }
    assert.equal(issueCalls, 0);
    elapsedSeconds = 90;
    const ready = await worker.fetch(new Request('https://zaeorion.com/connect?activating=18',
      { headers: { Cookie: cookie } }), stripeEnv);
    assert.equal(ready.status, 200);
    assert.equal(issueCalls, 1);
    assert.equal(calls.filter((call) => call.path === '/api/bot/pair-status').length, 37);
  } finally { restore(); }
});

test('AUD-A5-001: status and issue quota 429 show an honest retry deadline, no code', async () => {
  const paid = await paidCheckoutCookie();
  const cookie = `${await sessionCookieFor()}; ${paid}`;
  for (const caseName of ['status', 'issue']) {
    const { calls, restore } = installFetch((path) => {
      if (path === '/api/bot/pair-status') {
        return caseName === 'status'
          ? Response.json({ error: 'rate_limited', retry_after_s: 73 }, { status: 429 })
          : Response.json({ ok: true, ready: true });
      }
      if (path === '/api/bot/pair-issue') {
        return Response.json({ error: 'rate_limited', retry_after_s: 73 }, { status: 429 });
      }
      throw Error(`unexpected path ${path}`);
    });
    try {
      const response = await worker.fetch(new Request('https://zaeorion.com/connect?activating=2',
        { headers: { Cookie: cookie } }), stripeEnv);
      const html = await response.text();
      assert.equal(response.status, 202);
      assert.match(html, /Try again in 73 seconds/u);
      assert.doesNotMatch(html, /http-equiv="refresh"|orion:\/\/activate/u);
      assert.equal(calls.filter((call) => call.path === '/api/bot/pair-issue').length,
        caseName === 'status' ? 0 : 1);
    } finally { restore(); }
  }
});

// [COPY-FIX 2026-09-23 CW-4] After checkout the one-time code is the only primary action.
test('CW-4: the account page after checkout offers only Get your one-time code', async () => {
  const cookie = await sessionCookieFor();
  const after = await worker.fetch(new Request('https://zaeorion.com/discord?purchase=complete',
    { headers: { Cookie: cookie } }), stripeEnv);
  assert.equal(after.status, 200);
  const html = await after.text();
  assert.match(html, /Checkout complete\./u);
  assert.match(html, /<a class="button primary" href="\/connect">Get your one-time code<\/a>/u);
  assert.equal((html.match(/class="button primary"/gu) || []).length, 1);
  assert.doesNotMatch(html, /href="\/buy"|Subscribe · \$14\.99\/month beta|Starting the 7-day trial\?|account-trial/u);

  // Without the purchase flag the page still sells: Subscribe primary, code secondary, trial block.
  const before = await worker.fetch(new Request('https://zaeorion.com/discord',
    { headers: { Cookie: cookie } }), stripeEnv);
  const plain = await before.text();
  assert.match(plain, /<a class="button primary" href="\/buy">Subscribe · \$14\.99\/month beta<\/a>/u);
  assert.match(plain, /<a class="button secondary" href="\/connect">Get your one-time code<\/a>/u);
  assert.match(plain, /Starting the 7-day trial\?/u);
  assert.doesNotMatch(plain, /Checkout complete/u);
});

// [COPY-FIX 2026-09-23 CW-1] Paid in another browser (no paid cookie): the not-ready page
// tells them activation can lag, instead of only "needs an active subscription".
test('CW-1: no paid cookie + not yet provisioned tells a just-paid customer to refresh', async () => {
  const { response, html } = await connectWith(await sessionCookieFor(), notProvisioned);
  assert.equal(response.status, 403);
  assert.ok(html.includes('Just paid? Activation can take up to a minute — refresh this page.'));
  assert.doesNotMatch(html, /orion:\/\/activate|PAIR-|http-equiv="refresh"/u);
});

// ── rc1 RT-LOW-04 / CL3-F6-002: Stripe event.id is forwarded for backend dedup ──
function refundFetch(calls, chargebackReplies) {
  return async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, body: init?.body ? JSON.parse(init.body) : undefined });
    if (path === '/v1/invoices/in_ref1') return Response.json({ id: 'in_ref1', subscription: 'sub_test1' });
    if (path === '/v1/subscriptions/sub_test1') return Response.json(subscription);
    if (path === '/api/bot/chargeback') {
      const [status, body] = chargebackReplies.shift();
      return Response.json(body, { status });
    }
    throw Error(`unexpected path ${path}`);
  };
}

const refundEvent = { id: 'evt_refund_replay1', livemode: true, type: 'charge.refunded', data: { object: {
  id: 'ch_ref1', livemode: true, refunded: true, amount: 1999, amount_refunded: 1999, invoice: 'in_ref1',
} } };

test('RT-LOW-04: a replayed Stripe event carries the same event id; the backend no-op is a 200', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = refundFetch(calls, [
    [200, { ok: true, revoked: true }],
    [200, { ok: true, duplicate_event: true }],
  ]);
  try {
    for (let i = 0; i < 2; i += 1) {
      const response = await worker.fetch(await signedStripeEvent(refundEvent), liveStripeEnv);
      assert.equal(response.status, 200);
    }
    const chargebacks = calls.filter((c) => c.path === '/api/bot/chargeback');
    assert.equal(chargebacks.length, 2);
    for (const c of chargebacks) assert.equal(c.body.stripe_event_id, 'evt_refund_replay1');
  } finally { globalThis.fetch = originalFetch; }
});

test('RT-LOW-04: an in-flight duplicate (409) is a 500 so Stripe retries later', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = refundFetch(calls, [[409, { ok: false, error: 'event_in_progress' }]]);
  try {
    const response = await worker.fetch(await signedStripeEvent(refundEvent), liveStripeEnv);
    assert.equal(response.status, 500);
  } finally { globalThis.fetch = originalFetch; }
});

test('RT-LOW-04: provision and cancellation forward the verified event id', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    calls.push({ path, body: init?.body ? JSON.parse(init.body) : undefined });
    if (path === '/v1/subscriptions/sub_test1') return Response.json(subscription);
    if (path === '/api/bot/provision') return Response.json({ ok: true }, { status: 201 });
    if (path === '/api/bot/chargeback') return Response.json({ ok: true });
    throw Error(`unexpected path ${path}`);
  };
  try {
    await worker.fetch(await signedStripeEvent({ id: 'evt_fwd_checkout', livemode: true,
      type: 'checkout.session.completed', data: { object: {
        id: 'cs_fwd1', livemode: true, mode: 'subscription', payment_status: 'paid', amount_total: 1999,
        subscription: 'sub_test1', metadata: { discord_user_id: '123456789012345678' } } } }), liveStripeEnv);
    await worker.fetch(await signedStripeEvent({ id: 'evt_fwd_cancel', livemode: true,
      type: 'customer.subscription.deleted', data: { object: { id: 'sub_test1', livemode: true,
        status: 'canceled', metadata: subscription.metadata } } }), liveStripeEnv);
    const provision = calls.find((c) => c.path === '/api/bot/provision');
    const cancel = calls.find((c) => c.path === '/api/bot/chargeback');
    assert.equal(provision.body.stripe_event_id, 'evt_fwd_checkout');
    assert.equal(cancel.body.stripe_event_id, 'evt_fwd_cancel');
  } finally { globalThis.fetch = originalFetch; }
});
