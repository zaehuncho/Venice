Claude's lanes for the final internal round (the owner starts it with: "run your final red-team lanes"). Run after Codex's FIXUP_REPORT and the rebuild, alongside Codex's re-gate and Gemini's review. Read-only against a **copy** of the release package; findings go to `docs/redteam/2026-09-23-final/RED_TEAM_REPORT.claude.final.md`, then Claude patches after all three reports are in.

Agents: Opus, read-only, one lane each. The same limits as Codex's prompt apply (no deploys, no commits, no direct OrionNative launch, no force-kill, no settings.json edits, reader files are Astra's lane, no secrets, no ProjectReplay).

| Lane | What it proves |
|---|---|
| F1 Package = source | Every packaged binary/sidecar file hash matches the tested build; manifest covers all runtime files; no lab/test/dev artifacts; ORION_PRODUCTION_BUILD gating (no dev env overrides, no ONSET_FF AB arms, dev offset refused) |
| F2 Input path regression | Packaged fork + launcher against the real pipe harness: release re-send, ABANDON bounded (post-A2-003), dead-man, stuck-button replay of every owner-reported combo (X after Triangle+Square, Square sprint), clean Disconnect on close |
| F3 Disconnect classification | Every stop/exit path in the exit map produces a parent-side AND child-side cause line; missing-line cases documented; "Remote is already in use" recovery path |
| F4 Timing no-regression | Engine fixtures: left fade −6, FF final floor (A1-002), oracle reset (A1-001), dev offset refusal, detection-unavailable latch; scoreboard on the owner's next test session vs baseline |
| F5 Capture + first run | Fresh-install state machine in a copied profile: no device, wrong device, index 0 generic, webcam, device reorder, 30 fps card, OBS holding the card; every state has a customer message and no timing authority |
| F6 Licence + purchase | Mocked end-to-end: purchase → webhook → role → /connect 202 polling → one code → pair → lease → outage (A5-002) → expiry → revoke/kill switch; heartbeat backoff and resume-from-sleep |
| F7 Update + install | Disposable install: fresh install, update, injected rollback, retired-file prune, uninstall keeps/removes the right data; SmartScreen/AV first-run copy |
| F8 UI states | Every page at 1280×720 and at 150% DPI: no overflow (the Shot Lead card class of bug), every error readable, nothing internal ("Orion", "sidecar", "lease") on screen |

Master output merges Codex + Gemini + Claude into `docs/redteam/2026-09-23-final/RED_TEAM_REPORT.md` with one patch list: **release blockers** first, then pre-launch polish, then post-launch.
