from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def fail(reason: str) -> int:
    print(f"VERIFY_FAIL={reason}")
    return 1


def main() -> int:
    if len(sys.argv) != 2:
        return fail("usage")
    index = Path(sys.argv[1]).resolve()
    root = index.parents[1]
    text = index.read_text(encoding="utf-8", errors="replace")
    required = {
        "venice_brand": "VENICE",
        "monthly_price": "Get Venice — $25/mo",
        "buy_route": 'href="/buy"',
        "discord_route": 'href="/discord"',
        "trial_copy": "Try it free for 7 days",
        "terms": 'href="/terms.html"',
        "privacy": 'href="/privacy.html"',
        "refunds": 'href="/refunds.html"',
        "canvas_starfield": 'id="starfield"',
        "favicon_svg": 'href="/favicon.svg?v=3"',
        "apple_icon": 'href="/apple-touch-icon.png?v=3"',
        "checkout_dialog": 'data-checkout-dialog',
        "discord_identity": "Discord-linked access",
    }
    for name, marker in required.items():
        if marker not in text:
            return fail(f"missing_{name}")
    if any(price in text for price in ("$7.99", "$19.99", "$49.99", "$199.99")):
        return fail("legacy_price_visible")
    if "Something powerful is on the way" in text or "ZaeOrion" in text:
        return fail("coming_soon_copy_present")
    if text.count("<section") > 5:
        return fail("site_not_simplified")
    if 'class="preview' in text or "Excellent release" in text or "timing-ring" in text:
        return fail("synthetic_product_preview_present")
    # v3 (2026-09-19, owner: "simpler, no slogan"): the page opens with the product name and one
    # factual line, and the gate states who takes the card and that you can cancel. No badge row.
    for marker in (
        "Jump-shot timing for NBA 2K27 on PS5", "Remote Play or capture card",
        "Secure checkout by Stripe", "Cancel anytime",
    ):
        if marker not in text:
            return fail("plain_storefront_copy_missing")
    if "text-gradient" in text or "hero-glow" in text or 'class="bento"' in text:
        return fail("decorative_hero_present")
    # Never borrow a competitor's credibility: no user counters, star ratings, review counts,
    # "trusted by" claims or live-purchase toasts. Only facts this product can stand behind.
    if re.search(
        r"\d[\d,]*\s*\+\s*(?:active\s+)?(?:users|members|reviews|customers)"
        r"|average rating|star rating|[0-9.]+\s*/\s*5 stars|★|trusted by|just purchased|recently purchased",
        text,
        flags=re.IGNORECASE,
    ):
        return fail("invented_social_proof")
    # Owner request: the connected Discord account is shown in the header (avatar + username),
    # with a POST sign-out, never a GET link.
    for marker in ("data-account-avatar", "data-account-initial", 'action="/logout" method="post"'):
        if marker not in text:
            return fail("header_account_control_missing")
    # v3: the home page inlines the shared stylesheet (one render-blocking request fewer). The inline
    # copy must stay byte-identical to public/styles.css, which the legal, 404 and Worker pages link.
    inline = re.search(r"<style>(.*?)</style>", text, flags=re.DOTALL)
    if not inline or inline.group(1).strip() != (root / "public/styles.css").read_text(encoding="utf-8").strip():
        return fail("inline_css_out_of_sync_with_styles_css")
    if 'href="/styles.css"' in text:
        return fail("styles_css_loaded_twice")
    for path in (
        "public/styles.css", "public/app.js", "public/orion.png", "public/favicon.ico",
        "public/favicon.svg", "public/apple-touch-icon.png",
        "public/terms.html", "public/privacy.html", "public/refunds.html", "src/worker.js",
        "wrangler.jsonc", "tests/worker.test.mjs",
    ):
        if not (root / path).is_file():
            return fail(f"missing_file_{path.replace('/', '_')}")

    app = (root / "public/app.js").read_text(encoding="utf-8")
    for marker in ("requestAnimationFrame(draw)", "prefers-reduced-motion", "visibilitychange",
                   "initEmbeddedCheckout", "/api/checkout/config", "avatarUrl", "cdn.discordapp.com"):
        if marker not in app:
            return fail("interactive_contract")
    if "30 fps" not in app or "1.25" not in app:
        return fail("starfield_not_performance_capped")
    if (root / "public/orion.png").stat().st_size > 80_000:
        return fail("logo_not_optimized")
    if re.search(r"sk_(live|test)_", app, flags=re.IGNORECASE):
        return fail("stripe_secret_in_client")

    worker = (root / "src/worker.js").read_text(encoding="utf-8")
    for marker in (
        "Content-Security-Policy", "Strict-Transport-Security", 'url.pathname === "/buy"',
        'url.pathname === "/discord"', "allowedHosts", '"/api/stripe/webhook"',
        '"/api/checkout/session"', "verifyStripeSignature", "X-Orion-Bot-Secret",
        "X-Edge-Auth", "crypto.subtle.verify", "HttpOnly; Secure; SameSite=Lax",
    ):
        if marker not in worker:
            return fail("worker_security_contract")
    if re.search(r"(?:sk|whsec)_(?:live|test)?_[A-Za-z0-9]", worker):
        return fail("hardcoded_secret")
    if 'return_to=%2Fdiscord' not in worker or 'Discord connected.' not in worker:
        return fail("discord_profile_page_missing")
    redirects = (root / "public/_redirects").read_text(encoding="utf-8")
    if re.search(r"^/discord\s", redirects, flags=re.MULTILINE):
        return fail("discord_invite_redirect_present")

    config = json.loads((root / "wrangler.jsonc").read_text(encoding="utf-8"))
    if "DISCORD_INVITE" in config["vars"] or "DISCORD_INVITE" in config["env"]["production"]["vars"]:
        return fail("discord_invite_config_present")
    if "BUY_URL" in config["vars"] or "BUY_URL" in config["env"]["production"]["vars"]:
        return fail("non_stripe_checkout_config_present")
    if "gumroad" in worker.lower() or "gumroad" in json.dumps(config).lower():
        return fail("non_stripe_checkout_code_present")
    if config["vars"]["CHECKOUT_PROVIDER"] != "stripe":
        return fail("stripe_provider_not_enabled")
    if not config["vars"]["STRIPE_PUBLISHABLE_KEY"].startswith("pk_test_"):
        return fail("stripe_test_publishable_key_missing")
    if config["vars"]["STRIPE_PRICE_ID"] != "price_1UGAOmGniZwGqtXLa9iDiEtk":
        return fail("stripe_price_not_pinned")
    if config["env"]["production"]["name"] != "venice-site-production":
        return fail("production_name")
    if config["env"]["production"]["vars"]["CHECKOUT_PROVIDER"] != "stripe":
        return fail("production_stripe_provider_not_enabled")
    if config["vars"].get("STRIPE_MODE") != "test":
        return fail("local_stripe_test_mode_missing")
    production = config["env"]["production"]["vars"]
    if production.get("STRIPE_MODE") != "live" or not production["STRIPE_PUBLISHABLE_KEY"].startswith("pk_live_"):
        return fail("production_live_key_missing")
    if production.get("STRIPE_PRICE_ID") != "price_1UGaaRGniZwGqtXLwQV7OLRY":
        return fail("production_live_price_missing")
    digest = hashlib.sha256(index.read_bytes()).hexdigest().upper()
    print(
        "VERIFY_OK=single_plan_25_month,plain_storefront,no_synthetic_preview,no_invented_social_proof,"
        "header_account_control,inline_css_in_sync,30fps_canvas,optimized_logo,"
        f"tab_favicon,stripe_live_production_checkout,discord_identity,security_headers,assets,legal,index_sha256:{digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
