You are Gemini, helping prepare Venice (a paid NBA 2K27 shot-timing tool) for its beta launch. These are **quick, bounded writing and review tasks**. Repo: `C:\Users\aaron\Desktop\NexusVision`. Read-only except the files named below. Never use ProjectReplay.

Before you finish each task, re-read your output against the source files it cites and fix anything that doesn't match.

## Task 1 — customer support macros

Write `docs/support/SUPPORT_MACROS.md`: short, copy-paste Discord replies for the 15 most likely launch-week tickets. Base them on your own report `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.gemini.customer.md` and on the real in-app text (quote it exactly).

Cover at least:
- paid but no code yet;
- the pairing code expired;
- SmartScreen / antivirus warnings;
- capture card not detected or wrong device;
- 30 fps / dropped-frame capture;
- black screen (HDCP);
- "shots paused" / licence reconnecting;
- PC clock wrong;
- new PC / HWID reset;
- "meter detection unavailable";
- online late streaks (point to timing_expectations);
- Remote Play "already in use";
- how to send logs (no keys or personal data);
- refunds (policy from `website/public/index.html` / the terms);
- the minimum PC and capture spec.

## Task 2 — copy consistency

List every place the customer sees:
- price;
- trial length;
- supported meter styles (**Arrow2 only**; Pill is withdrawn);
- the Shot Lead advice.

Sources: `website/public/index.html`, `website/src/worker.js`, `discord_launch/launch_embeds/*.json`, `discord_launch/orion_bot.py`, `native_orion/qml/**/*.qml`.

The correct values are **$19.99/month, a 7-day trial, Arrow2 only**, and "don't move Shot Lead after one or two lates". Output a table of file:line, current text and the corrected text, in `docs/support/COPY_FIXES.md`. Do not edit the source files.

## Task 3 — launch-day checklist

Write `docs/support/LAUNCH_DAY_CHECKLIST.md`: a one-page, timed checklist (T-24 h, T-1 h, launch, +1 h, +24 h) built from your runbooks (Part C of your customer report). For each step, give who does it (owner/Claude/Codex) and how to verify it.

## Rules

- **Do not** build, deploy, launch Venice, commit, or edit any source file.
- **Do not** contact Stripe, Discord, PSN, 2K or any live service.
- No secrets.
