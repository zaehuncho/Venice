// Venice storefront. Progressive enhancement only: every link on the page still works without this file.
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const DISCORD_INVITE = 'https://discord.gg/yTuekgdgEN';
const SIGN_IN_URL = `/auth/discord?return_to=${encodeURIComponent('/#pricing')}`;

// ---------- Entrance reveal ----------
// Progressive: [data-reveal] elements are only hidden once this runs, and only when motion is
// allowed and IntersectionObserver exists. Without JS, or under reduced motion, the page is fully
// visible with nothing to reveal — so a below-the-fold section can never get stuck hidden.
if (!reducedMotion && 'IntersectionObserver' in window) {
  const revealables = document.querySelectorAll('[data-reveal]');
  if (revealables.length) {
    document.documentElement.classList.add('reveal-ready');
    const revealer = new IntersectionObserver((entries, observer) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add('is-in');
        observer.unobserve(entry.target);
      }
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealables.forEach((node) => revealer.observe(node));
  }
}

// ---------- Star texture ----------
// A faint sky that drifts upward, BLINKS and flickers, capped at 30 fps with the pixel ratio at 1.25
// and paused whenever the tab is hidden. Under reduced motion or Save-Data it is painted once and
// left completely still — no drift, no blink, no parallax, no constellation, no per-frame work.
//
// [2026-09-21 owner: "blinking and flickering stars"] Two motions, not one. Every star carries a
// slow twinkle AND a fast, small flicker on top, so the sky shimmers instead of breathing. One in
// six is a BLINKER: the same wave cubed, which holds it dim for most of its cycle and snaps it
// bright for a moment — that reads as a blink, where a plain sine only reads as a pulse.
//
// With a fine pointer the nearest few stars to the cursor chain into a small constellation that
// forms and dissolves as the pointer moves. It is deliberately dim and short-range: the hero and
// the pricing card stay the brightest things on the page.
const canvas = document.getElementById('starfield');
if (canvas instanceof HTMLCanvasElement) {
  const context = canvas.getContext('2d', { alpha: false });
  const saveData = navigator.connection?.saveData === true;
  const still = reducedMotion || saveData;
  const parallax = matchMedia('(pointer: fine)').matches && !still;
  const pointer = { x: 0, y: 0, targetX: 0, targetY: 0 };
  // Cursor in CSS pixels, plus an eased presence so the constellation fades in and out instead of
  // snapping when the pointer enters or leaves the window.
  const cursor = { x: 0, y: 0, here: 0, target: 0 };
  const LINK_RADIUS = 170;   // px: how far from the cursor a star can be and still join
  const LINK_MAX = 6;        // at most this many points in one constellation
  const LINK_SPAN = 150;     // px: longest line drawn between two joined stars
  const near = [];           // scratch list, reused every frame (no per-frame allocation)
  let width = 0;
  let height = 0;
  let count = 0;
  let frame = 0;
  let previous = 0;

  // Deterministic sky in normalised coordinates, so a resize rescales the same stars instead of reshuffling them.
  let seed = 20260919;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
  const stars = Array.from({ length: 190 }, (_, index) => ({
    x: random(),
    y: random(),
    z: 0.25 + random() * 0.75,
    size: 0.42 + random() * 0.58,
    alpha: 0.22 + random() * 0.38,
    blue: index % 5 === 0,
    rise: 0.006 + random() * 0.015,   // normalised units per second, upward (scaled by depth)
    twPhase: random() * Math.PI * 2,  // twinkle offset so they don't pulse in unison
    twRate: 0.55 + random() * 0.95,   // twinkle speed
    flPhase: random() * Math.PI * 2,  // flicker offset (fast, small, independent of the twinkle)
    flRate: 3.6 + random() * 4.2,     // flicker speed — several times the twinkle
    blink: index % 6 === 3,           // a sixth of the sky blinks instead of pulsing
    tw: 1,                            // current brightness multiplier (1 when still)
  }));

  // One source of truth for where a star is on screen, so the links land exactly on the dots.
  const screenX = (star) => star.x * width + pointer.x * 8 * star.z;
  const screenY = (star) => star.y * height + pointer.y * 6 * star.z;

  // Collect the stars around the cursor, nearest first, and chain them: each step hops to the
  // closest point not yet used. That reads as a constellation rather than a fan of spokes.
  const linkStars = () => {
    near.length = 0;
    if (cursor.here < 0.01) return;
    for (let index = 0; index < count; index += 1) {
      const star = stars[index];
      const sx = screenX(star);
      const sy = screenY(star);
      const d2 = (sx - cursor.x) ** 2 + (sy - cursor.y) ** 2;
      if (d2 <= LINK_RADIUS * LINK_RADIUS) near.push({ star, sx, sy, d2 });
    }
    near.sort((a, b) => a.d2 - b.d2);
    near.length = Math.min(near.length, LINK_MAX);
    if (near.length < 2) return;

    context.lineWidth = 1;
    context.strokeStyle = '#6fb4ff';
    for (let i = 0; i < near.length - 1; i += 1) {
      // Nearest remaining neighbour to the current point becomes the next link in the chain.
      let best = i + 1;
      let bestD2 = Infinity;
      for (let j = i + 1; j < near.length; j += 1) {
        const d2 = (near[j].sx - near[i].sx) ** 2 + (near[j].sy - near[i].sy) ** 2;
        if (d2 < bestD2) { bestD2 = d2; best = j; }
      }
      if (best !== i + 1) { const swap = near[i + 1]; near[i + 1] = near[best]; near[best] = swap; }
      const span = Math.sqrt(bestD2);
      if (span > LINK_SPAN) break;   // a lone far star ends the chain instead of stretching to it
      // Fade with how far the pair sits from the cursor and how long the line is.
      const reach = Math.sqrt(near[i].d2) / LINK_RADIUS;
      context.globalAlpha = cursor.here * 0.30 * (1 - reach) * (1 - span / LINK_SPAN);
      context.beginPath();
      context.moveTo(near[i].sx, near[i].sy);
      context.lineTo(near[i + 1].sx, near[i + 1].sy);
      context.stroke();
    }
    // Lift the joined points so the constellation has visible nodes, not just lines.
    context.fillStyle = '#9fd0ff';
    for (let i = 0; i < near.length; i += 1) {
      const reach = Math.sqrt(near[i].d2) / LINK_RADIUS;
      context.globalAlpha = cursor.here * 0.55 * (1 - reach);
      context.beginPath();
      context.arc(near[i].sx, near[i].sy, Math.max(0.9, near[i].star.size * near[i].star.z * 1.8), 0, Math.PI * 2);
      context.fill();
    }
  };

  const paint = () => {
    if (!context) return;
    context.fillStyle = '#06080c';
    context.fillRect(0, 0, width, height);
    for (let index = 0; index < count; index += 1) {
      const star = stars[index];
      context.globalAlpha = star.alpha * star.tw;
      context.fillStyle = star.blue ? '#6fb4ff' : '#e6f1ff';
      context.beginPath();
      context.arc(screenX(star), screenY(star), Math.max(0.4, star.size * star.z), 0, Math.PI * 2);
      context.fill();
    }
    if (parallax) linkStars();
    context.globalAlpha = 1;
  };

  // Advance the drift and twinkle by dt seconds. Nearer stars (higher z) drift a little faster.
  const advance = (dt) => {
    for (let index = 0; index < count; index += 1) {
      const star = stars[index];
      star.y -= star.rise * star.z * dt;
      if (star.y < -0.02) { star.y += 1.04; star.x = (star.x + 0.37) % 1; }
      star.twPhase += star.twRate * dt;
      star.flPhase += star.flRate * dt;
      // 0..1 twinkle, cubed for blinkers so they sit dim and flash rather than breathe.
      const wave = 0.5 + 0.5 * Math.sin(star.twPhase);
      const shaped = star.blink ? wave * wave * wave : wave;
      // The flicker is deliberately small: it should shimmer, never strobe.
      const flicker = 1 + 0.16 * Math.sin(star.flPhase);
      star.tw = (star.blink ? 0.05 + 1.25 * shaped : 0.40 + 0.75 * shaped) * flicker;
    }
  };

  const draw = (now = 0) => {
    frame = 0;
    if (now - previous < 33) {   // ~30 fps ceiling
      frame = requestAnimationFrame(draw);
      return;
    }
    const dt = previous ? Math.min((now - previous) / 1000, 0.05) : 0;
    previous = now;
    if (parallax) {
      pointer.x += (pointer.targetX - pointer.x) * 0.08;
      pointer.y += (pointer.targetY - pointer.y) * 0.08;
      cursor.here += (cursor.target - cursor.here) * 0.12;
    }
    if (!still) advance(dt);
    paint();
    const settling = Math.abs(pointer.targetX - pointer.x) > 0.002 || Math.abs(pointer.targetY - pointer.y) > 0.002
      || Math.abs(cursor.target - cursor.here) > 0.01;
    // Keep animating while the sky drifts, or while a pointer nudge settles.
    if ((!still || settling) && !document.hidden) frame = requestAnimationFrame(draw);
  };
  const wake = () => {
    if (!frame && !document.hidden) frame = requestAnimationFrame(draw);
  };

  const resize = () => {
    const scale = Math.min(devicePixelRatio || 1, 1.25);
    width = innerWidth;
    height = innerHeight;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context?.setTransform(scale, 0, 0, scale, 0, 0);
    count = saveData ? 40 : Math.min(stars.length, Math.max(64, Math.round((width * height) / 8200)));
    paint();
  };

  let resizeFrame = 0;
  addEventListener('resize', () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(resize);
  }, { passive: true });
  if (parallax) {
    addEventListener('pointermove', (event) => {
      pointer.targetX = event.clientX / width - 0.5;
      pointer.targetY = event.clientY / height - 0.5;
      cursor.x = event.clientX;
      cursor.y = event.clientY;
      cursor.target = 1;
      wake();
    }, { passive: true });
    // Pointer gone (left the window, or switched to touch): let the constellation dissolve.
    addEventListener('pointerout', (event) => {
      if (event.relatedTarget) return;
      cursor.target = 0;
      wake();
    }, { passive: true });
    addEventListener('blur', () => { cursor.target = 0; wake(); }, { passive: true });
  }
  document.addEventListener('visibilitychange', () => {
    cancelAnimationFrame(frame);
    frame = 0;
    previous = 0;
    if (!document.hidden && !still) wake();
  });
  // First paint rides the first animation frame, so the page's own first layout is not forced into
  // this script's task; the drift loop then starts (unless the sky is meant to stay still).
  requestAnimationFrame(() => { resize(); if (!still) wake(); });
}

// ---------- Purchase gate ----------
// States: out (not signed in) → join (signed in, not in the Venice server) → in (can start trial / subscribe) → done.
// Every state is already in the markup; this only switches which one is visible, so the gate never changes height.
const planActions = document.querySelector('[data-plan-actions]');
const planError = document.querySelector('[data-plan-error]');
const planErrorText = document.querySelector('[data-plan-error-text]');
const planRetry = document.querySelector('[data-plan-retry]');
const trialButton = document.querySelector('[data-trial-claim]');
const trialLabel = document.querySelector('[data-trial-label]');
const trialNote = document.querySelector('[data-trial-note]');
const membershipNote = document.querySelector('[data-membership-note]');
const trialMessage = document.querySelector('[data-trial-message]');
const joinRefresh = document.querySelector('[data-join-refresh]');
const joinRefreshLabel = document.querySelector('[data-join-refresh-label]');
const doneState = document.querySelector('[data-plan-done]');
let siteConfig = null;
let retryAction = null;

const setPlanState = (state) => {
  if (planActions) planActions.dataset.state = state;
};

// ---------- Header account control ----------
// Both states live in one grid cell, so showing the signed-in account never reflows the header.
const accountControl = document.querySelector('[data-account]');
const accountLink = document.querySelector('[data-account-link]');
const accountAvatar = document.querySelector('[data-account-avatar]');
const accountInitial = document.querySelector('[data-account-initial]');
accountAvatar?.addEventListener('error', () => {
  // Never leave a broken image in the header: fall back to the initial.
  accountAvatar.hidden = true;
  if (accountInitial) accountInitial.hidden = false;
});

const showAccount = (config) => {
  if (!accountControl) return;
  if (!config?.authenticated) {
    accountControl.dataset.state = 'out';
    return;
  }
  const name = config.username || 'Discord account';
  accountLink?.setAttribute('aria-label', `Discord account: ${name}`);
  if (accountInitial) accountInitial.textContent = (name.trim()[0] || 'V').toUpperCase();
  let avatar = '';
  try {
    const parsed = new URL(String(config.avatarUrl || ''), location.origin);
    if (parsed.protocol === 'https:' && parsed.hostname === 'cdn.discordapp.com') avatar = parsed.toString();
  } catch { /* fall back to the initial */ }
  if (accountAvatar && avatar) {
    accountAvatar.src = avatar;
    accountAvatar.hidden = false;
    if (accountInitial) accountInitial.hidden = true;
  } else if (accountAvatar) {
    accountAvatar.hidden = true;
    if (accountInitial) accountInitial.hidden = false;
  }
  accountControl.dataset.state = 'in';
};

const hidePlanMessage = () => {
  if (planError) planError.hidden = true;
  if (planRetry) planRetry.hidden = true;
  retryAction = null;
};
const showPlanMessage = (message, { retry = null, info = false } = {}) => {
  if (!planError || !planErrorText) return;
  planErrorText.textContent = message;
  planError.classList.toggle('is-info', info);
  planError.hidden = false;
  retryAction = retry;
  if (planRetry) planRetry.hidden = !retry;
};
planRetry?.addEventListener('click', () => {
  const action = retryAction;
  hidePlanMessage();
  action?.();
});

const setJoinUrl = (url) => {
  let target = DISCORD_INVITE;
  try {
    const parsed = new URL(String(url || ''), location.origin);
    if (parsed.protocol === 'https:' && ['discord.gg', 'discord.com', 'www.discord.com'].includes(parsed.hostname)) target = parsed.toString();
  } catch { /* keep the invite */ }
  document.querySelectorAll('[data-join-link]').forEach((link) => link.setAttribute('href', target));
};
const showJoin = (url) => {
  setJoinUrl(url);
  setPlanState('join');
};
const isJoinRequired = (payload) => payload?.error === 'join_required' || payload?.code === 'join_required';
const needsJoin = (config) => Boolean(config?.authenticated) && config.inGuild === false && config.membershipUnknown !== true;
const scrollToGate = () => document.getElementById('pricing')?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });

const applyConfig = (config) => {
  siteConfig = config;
  showAccount(config);
  if (!config || !planActions || planActions.dataset.state === 'done') return;
  if (!config.authenticated) {
    setPlanState('out');
    return;
  }
  const name = config.username || 'your Discord account';
  document.querySelectorAll('[data-account-name]').forEach((node) => { node.textContent = name; });
  if (trialNote) trialNote.hidden = config.trial === true;
  if (membershipNote) membershipNote.hidden = !(config.membershipUnknown === true && config.trial === true);
  setJoinUrl(config.joinUrl);
  if (needsJoin(config)) {
    showJoin(config.joinUrl);
    return;
  }
  setPlanState('in');
};

// ---------- Checkout config (shared by the header, the gate and Stripe checkout) ----------
let configPromise;
const checkoutConfig = () => {
  configPromise ??= fetch('/api/checkout/config', { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
    .then((response) => response.ok ? response.json() : null)
    .catch(() => null);
  return configPromise;
};
const refreshConfig = () => {
  configPromise = undefined;
  return checkoutConfig().then((config) => {
    applyConfig(config);
    return config;
  });
};

// "I've joined — refresh": re-read membership without reloading the page.
let refreshing = false;
const recheckMembership = async ({ quiet = false } = {}) => {
  if (refreshing) return;
  refreshing = true;
  if (!quiet) {
    hidePlanMessage();
    joinRefresh?.setAttribute('aria-busy', 'true');
    if (joinRefreshLabel) joinRefreshLabel.textContent = 'Checking…';
  }
  try {
    const config = await refreshConfig();
    if (quiet) return;
    if (!config) showPlanMessage('Venice could not be reached. Check your connection and try again.', { retry: () => recheckMembership() });
    else if (needsJoin(config)) showPlanMessage('You’re not showing as a member yet. Join with the same Discord account, then refresh again.', { info: true });
  } finally {
    refreshing = false;
    joinRefresh?.removeAttribute('aria-busy');
    if (joinRefreshLabel) joinRefreshLabel.textContent = 'I’ve joined — refresh';
  }
};
joinRefresh?.addEventListener('click', () => recheckMembership());
// Coming back from the Discord tab re-checks membership once, quietly.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && planActions?.dataset.state === 'join') recheckMembership({ quiet: true });
});

// ---------- Free trial (POST /api/trial) ----------
const FINAL_TRIAL_CODES = new Set(['already_claimed', 'forbidden_origin', 'unsupported_media_type', 'method_not_allowed']);
let claiming = false;
const claimTrial = async () => {
  if (claiming) return;
  claiming = true;
  hidePlanMessage();
  trialButton?.setAttribute('aria-busy', 'true');
  if (trialLabel) trialLabel.textContent = 'Starting your trial…';
  try {
    const response = await fetch('/api/trial', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: '{}',
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 || payload.code === 'auth_required') {
      location.assign(SIGN_IN_URL);
      return;
    }
    if (isJoinRequired(payload)) {
      showJoin(payload.joinUrl);
      if (payload.message) showPlanMessage(payload.message, { info: true });
      return;
    }
    if (response.ok && payload.ok === true) {
      if (trialMessage && payload.message) trialMessage.textContent = payload.message;
      setPlanState('done');
      doneState?.focus({ preventScroll: true });
      return;
    }
    const message = payload.message || payload.error || 'Your trial could not be started. Please try again.';
    // A used trial is final; everything else (DMs closed, rate limit, outage) is worth retrying.
    if (FINAL_TRIAL_CODES.has(payload.code)) showPlanMessage(message, { info: payload.code === 'already_claimed' });
    else showPlanMessage(message, { retry: claimTrial });
  } catch {
    showPlanMessage('Venice could not be reached. Check your connection and try again.', { retry: claimTrial });
  } finally {
    claiming = false;
    trialButton?.removeAttribute('aria-busy');
    if (trialLabel) trialLabel.textContent = 'Start free 7-day trial';
  }
};
trialButton?.addEventListener('click', () => {
  if (!siteConfig?.authenticated) {
    location.assign(SIGN_IN_URL);
    return;
  }
  if (needsJoin(siteConfig)) {
    showJoin(siteConfig.joinUrl);
    return;
  }
  // Older backends without the website trial: the trial is claimed in Discord with /claim_trial.
  if (siteConfig.trial !== true) {
    window.open(DISCORD_INVITE, '_blank', 'noopener');
    return;
  }
  claimTrial();
});

// ---------- Embedded Stripe checkout (activates only after all server-side secrets exist) ----------
const checkoutDialog = document.querySelector('[data-checkout-dialog]');
const checkoutHost = document.getElementById('checkout-host');
const checkoutLoading = document.querySelector('[data-checkout-loading]');
const checkoutError = document.querySelector('[data-checkout-error]');
const checkoutRetryWrap = document.querySelector('[data-checkout-retry-wrap]');
const checkoutStatus = document.querySelector('[data-checkout-status]');
let stripePromise;
let embeddedCheckout;
let checkoutOpening = false;
let joinRequiredUrl = null;

const loadStripe = (publishableKey) => {
  if (window.Stripe) return Promise.resolve(window.Stripe(publishableKey));
  stripePromise ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://js.stripe.com/v3/';
    script.async = true;
    script.onload = () => resolve(window.Stripe(publishableKey));
    script.onerror = () => {
      stripePromise = undefined;
      reject(new Error('Secure checkout did not load.'));
    };
    document.head.append(script);
  });
  return stripePromise;
};

const showCheckoutError = (message, retry = false) => {
  if (checkoutLoading) checkoutLoading.hidden = true;
  if (checkoutError) { checkoutError.hidden = false; checkoutError.textContent = message; }
  if (checkoutRetryWrap) checkoutRetryWrap.hidden = !retry;
  if (checkoutStatus) checkoutStatus.textContent = 'Checkout needs attention';
};

const startCheckout = async () => {
  const config = siteConfig || await checkoutConfig();
  if (!(checkoutDialog instanceof HTMLDialogElement) || !checkoutHost || !config) return;
  if (checkoutOpening) return;
  const testMode = config.publishableKey.startsWith('pk_test_');
  checkoutOpening = true;
  joinRequiredUrl = null;
  embeddedCheckout?.destroy();
  embeddedCheckout = undefined;
  if (checkoutLoading) checkoutLoading.hidden = false;
  if (checkoutError) checkoutError.hidden = true;
  if (checkoutRetryWrap) checkoutRetryWrap.hidden = true;
  if (checkoutStatus) checkoutStatus.textContent = testMode ? 'Test mode · no subscription access' : 'Preparing secure checkout…';
  if (!checkoutDialog.open) checkoutDialog.showModal();
  try {
    const stripe = await loadStripe(config.publishableKey);
    const checkout = await stripe.initEmbeddedCheckout({
      fetchClientSecret: async () => {
        const response = await fetch('/api/checkout/session', { method: 'POST', headers: { Accept: 'application/json' } });
        const payload = await response.json().catch(() => ({}));
        if (isJoinRequired(payload)) {
          joinRequiredUrl = payload.joinUrl || DISCORD_INVITE;
          throw new Error(payload.message || 'Join the Venice Discord to continue.');
        }
        if (!response.ok || !payload.clientSecret) throw new Error(payload.message || payload.error || 'Checkout could not be created.');
        return payload.clientSecret;
      },
    });
    if (!checkoutDialog.open) {
      checkout.destroy();
      return;
    }
    embeddedCheckout = checkout;
    if (checkoutLoading) checkoutLoading.hidden = true;
    if (checkoutStatus) checkoutStatus.textContent = testMode
      ? `Test mode · ${config.username || 'Discord user'} · no subscription access`
      : `Connected as ${config.username || 'Discord user'}`;
    checkout.mount('#checkout-host');
  } catch (error) {
    if (joinRequiredUrl) {
      const message = error instanceof Error ? error.message : '';
      checkoutDialog.close();
      showJoin(joinRequiredUrl);
      if (message) showPlanMessage(message, { info: true });
      scrollToGate();
      return;
    }
    showCheckoutError(error instanceof Error ? error.message : 'Checkout could not be opened.', true);
  } finally {
    checkoutOpening = false;
  }
};

const openCheckout = async (event) => {
  const config = await checkoutConfig();
  if (!config?.configured || config.provider !== 'stripe') return;
  event.preventDefault();
  if (!config.authenticated) {
    location.assign(SIGN_IN_URL);
    return;
  }
  if (needsJoin(config)) {
    showJoin(config.joinUrl);
    scrollToGate();
    return;
  }
  if (checkoutDialog instanceof HTMLDialogElement && checkoutDialog.open) return;
  startCheckout();
};

document.querySelectorAll('[data-checkout]').forEach((button) => button.addEventListener('click', openCheckout));
document.querySelector('[data-checkout-retry]')?.addEventListener('click', () => startCheckout());
document.querySelector('[data-checkout-close]')?.addEventListener('click', () => checkoutDialog?.close());
checkoutDialog?.addEventListener('click', (event) => { if (event.target === checkoutDialog) checkoutDialog.close(); });
checkoutDialog?.addEventListener('close', () => {
  embeddedCheckout?.destroy();
  embeddedCheckout = undefined;
});

checkoutConfig().then(applyConfig);
