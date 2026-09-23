You are Gemini, doing a **customer's-eye final review** of Venice, a paid NBA 2K27 shot-timing tool launching as a $19.99/month beta with a 7-day trial. Repo: `C:\Users\aaron\Desktop\NexusVision`. **Read-only.** Write only the files named below. Never use ProjectReplay.

Run this after Codex's fixes and Claude's rebuild, when the owner says the package is ready. Before you finish each task, check your output against the source files it cites.

## Task 1: first-hour customer walkthrough (paper test)

Follow a brand-new customer from the website through to their first green:
1. `website/public/index.html` and `website/src/worker.js` (purchase → "activating…" → pairing code);
2. Discord `/connect` (`discord_launch/orion_bot.py`, `discord_launch/launch_embeds/*.json`);
3. the installer (`installer/orion.iss`, `installer/INSTALL_NOTICE.txt`);
4. first launch, licence pairing, capture card setup, and Remote Play connect (`native_orion/qml/**`);
5. **Calibrate my lead** (`ShotLeadCard.qml`), then the first game.

At every step, answer three questions:
- Would a customer know what to do next?
- What could confuse them?
- What would make them think "it's broken"?

Quote the exact on-screen text. Output: `docs/redteam/2026-09-23-final/CUSTOMER_WALKTHROUGH.gemini.md`, with a ranked list of the top 15 fixes (file:line, current text, suggested text).

## Task 2: error-message audit

List every customer-visible error or warning in `native_orion/qml/**`, `native_orion/src/UiNotificationPolicy.h`, `native_orion/src/LicenseClient.cpp` and `native_orion/src/OrionAppController.h`. For each one, say:
- **plain-language?**
- **tells the customer what to do?**
- **matches the support macros** in `docs/support/SUPPORT_MACROS.md`?

Flag any internal jargon that leaks into the UI (for example "sidecar", "lease", "epoch", "pipe", "Orion"). Output: `docs/redteam/2026-09-23-final/ERROR_COPY_AUDIT.gemini.md`.

## Task 3: launcher UI and installer design brief

The owner wants the launcher UI to look **substantially nicer**, and a custom installer that **looks like Venice**. Review the current look:
- `native_orion/qml/Theme.qml` (or wherever `Theme` is defined);
- `native_orion/qml/components/*.qml`, `native_orion/qml/pages/*.qml`;
- `installer/orion.iss`, `installer/assets/`, `website/public/index.html` (the website is the brand reference).

Write a **concrete design brief**, not code:
- A brand summary: the colours, type and mood the website uses. Where does the launcher drift from it?
- The **top 10 launcher UI improvements**, ranked by customer impact:
  - layout;
  - hierarchy;
  - spacing;
  - empty and loading states;
  - the live page;
  - status clarity;
  - cards that overflow or crowd.

  Each one names the file(s) involved.
- **The installer:** what a Venice-branded installer should look like, screen by screen:
  - welcome;
  - licence/notice;
  - install location;
  - progress;
  - finish.

  It is built with Inno Setup (`WizardStyle=modern`, `WizardImageFile`, `WizardSmallImageFile`). Say what fits in Inno Setup's limits, and what would need a custom-drawn wizard page or a separate bootstrapper.

Output: `docs/redteam/2026-09-23-final/UI_INSTALLER_BRIEF.gemini.md`.

## Rules

- Do **not** build, launch Venice, run installers, deploy, commit, or edit any source file.
- Do not contact Stripe, Discord, PSN, 2K or any live service.
- No secrets. Do not quote keys, tokens or personal data even if you find them. Report only where they are.
