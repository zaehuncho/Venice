# Venice: external (black-box) red-team prompts

This is the outsider round. Run it after rc4 (or the final candidate) passes the owner's play test and StrictSecurity.

Each AI (Codex, Gemini, Claude) acts as an **outside attacker or customer with no source-code access**. They start from what a customer or cracker would have:
- the **installer**: `C:\Users\aaron\VeniceRC\rc4-20260923\VeniceSetup-1.0.0.exe`, or a copy of the installed folder;
- the **public website** (zaeorion.com);
- the **public Discord bot**;
- the **public API host** (`api.zaeorion.com`).

The owner supplies a **test account** created only for this round (a Discord account plus a trial or test licence), and revokes it afterwards.

## Shared rules (paste these with every prompt)

- **Scope.** Only the owner's systems:
  - the Venice installer, the installed app, the update endpoint and manifest;
  - `api.zaeorion.com`, the zaeorion.com website and Worker;
  - the Venice Discord bot.
- **Out of scope.** Never test or touch PSN, NBA 2K, Stripe itself, Discord itself, AWS or Cloudflare infrastructure, other customers, or unrelated software.
- **No source code.** Do not read `C:\Users\aaron\Desktop\NexusVision`, `chiaki-ng-src`, Lethe, the internal red-team reports or the patch docs. Work only from what an outsider can obtain.
- **Local testing.** Test only on a copy of the installed app, or in the owner's VM. Never touch the owner's real installation or settings.
- **Live endpoints: be gentle.**
  - Send at most a handful of requests per check, and at most 1 request per second per endpoint.
  - No load testing, fuzzing floods or credential stuffing.
  - Stop and tell the owner if anything answers 5xx repeatedly.
  - Only one AI tests live endpoints at a time; the owner schedules this.
- **Payments.** No real payments. Anything purchase-related uses the owner-provided test account or Stripe test mode only. Never enter real card data.
- **Nothing destructive or persistent.** Leave no files, accounts or backdoors behind. Do not try to break other users' data. Admin and owner routes are probed only for "is this exposed / does it refuse me", never used.
- **Secrets.** Do not print secrets. If you find one, report its location only.
- **Record.** Note the time (UTC) of every live request, so the owner can match it in the backend logs.

## Output (every prompt)

Write `docs/redteam/2026-09-23-final/EXTERNAL_REPORT.<ai>.md`. Start with a verdict: **approved / needs changes / blocked**.

For each finding, give:
- ID and severity (LOW, MEDIUM, HIGH or CRITICAL);
- what an outsider can do;
- the steps to reproduce, with timestamps;
- the impact;
- the suggested fix.

Also include a list of what you tried that correctly failed (negative controls), and a "not tested" list.

---

## Prompt: Codex (external security)

You are Codex acting as an **external attacker** against Venice, a paid NBA 2K27 shot-timing app (Windows desktop client, website, licence API, Discord bot). You have only what a customer or cracker has: the installer, the installed app, the public website, the public API host and the bot, plus the owner's test account. Follow the shared rules above exactly.

Try, as an outsider would, to:
1. Unlock or keep using Venice without a valid subscription:
   - an expired or revoked test licence;
   - replaying earlier responses;
   - changing the system clock;
   - blocking the network after sign-in;
   - editing local files under `%LOCALAPPDATA%\NexusVision` or the install folder;
   - swapping DLLs or executables in the installed copy.
2. Get the app to install an update that is not genuinely signed:
   - a local proxy or hosts-file redirect in the VM;
   - a tampered manifest or ZIP;
   - forcing a downgrade.
3. Abuse the licence API from outside:
   - pairing-code guessing (within the rate rules);
   - nonce and timestamp replay;
   - device-binding tricks with two VMs;
   - reaching admin or staff routes without credentials.
4. Abuse the website purchase flow and `/connect` in test mode:
   - getting a code without paying;
   - reusing a code;
   - cookie tampering.
5. Escalate privileges locally on a standard-user VM account through the installed service, folders or drivers.
6. Find leaks: secrets, keys, internal URLs or developer backdoors in shipped files, strings, logs or network traffic from the client.

---

## Prompt: Gemini (external customer and public surface)

You are Gemini acting as a **brand-new customer and an outside reviewer** of Venice. You have only the website, the Discord server/bot, the installer and the installed app (on the owner's VM), plus the owner's test account. Follow the shared rules above exactly.

1. Follow the full first-hour path as a customer:
   - the website, then the trial on the website;
   - Discord and `/connect`;
   - the one-time code;
   - the installer (including SmartScreen and every prompt);
   - first launch, pairing and capture setup;
   - **Calibrate my lead**, then the first game.

   Record every point of confusion, every message that sounds broken, and every place where the website, Discord and the app contradict each other (price, trial, Xbox experimental, Remote Play supported, unsigned installer).
2. Review the public surface for anything that should not be public:
   - internal names;
   - debug text;
   - staff commands visible to customers;
   - error messages that reveal internals.
3. Check the bot's public commands (`/faq`, `/setup`, `/hwid_reset`, `/claim_trial`, `/purchase`) for wrong or outdated guidance, and for anything a customer can do that they shouldn't.
4. Rate the installer and launcher first impression (look, speed, clarity) and list the top 10 fixes.

---

## Prompt: Claude (external reliability and misuse)

You are Claude acting as an **outside tester with only the installer, the installed app, the public website and the bot** (no source). Follow the shared rules above exactly.

1. Stress the installed app as a customer's PC would:
   - no capture card;
   - the wrong card or a webcam only;
   - OBS holding the card;
   - unplugging and replugging mid-session;
   - sleep and resume;
   - the network dropping mid-session;
   - a wrong clock;
   - two launches;
   - a reboot mid-session.

   For each case record what the customer sees and whether Venice ever shoots when it shouldn't.
2. Break the settings and data files the way real users do (crash mid-save, a copied settings file from another PC, a deleted learning file), then check the repair path works and never unlocks anything.
3. Update and uninstall like a customer (VM): update over an older install, uninstall with and without keeping data, reinstall. Check that no leftovers or services remain.
4. Check support readiness: does every error a customer can hit have a clear message, and a matching reply in `docs/support/SUPPORT_MACROS.md`? (Reading that support doc is allowed; it is public-facing copy.)
