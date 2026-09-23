# Red team — merged lanes (Claude CL / Codex CX / Gemini GM) — 2026-09-22

**Single P1–P9 external-team handoff:** `RED_TEAM_REPORT.codex.P1-P9.consolidated.md`.
It deduplicates and severity-ranks the current P1–P5 rerun and P6–P9 addendum; use that
report for current P-lane decisions. The original CL/CX/GM rows below preserve the initial
patch-wave history and do not themselves constitute current release approval.

One row per ROOT CAUSE. Severity = the agreed scale; where teams disagreed the higher is kept and
noted. "Coordinator check" = Claude re-read the cited lines (read-only). Owner column is the patch
lane; Codex/Gemini verify, never patch. Status: open until the owner says patch.

| # | ids | sev | root cause (one line) | coordinator check | lane |
|---|---|---|---|---|---|
| 1 | CL-001 CL-002 CX-004 GM-003 | CRITICAL | fork: 25 ms delivery miss → `chiaki_session_stop`; +40 ms echo head-of-line blocks the FIFO; shipped binary lacks the expedite | confirmed at the lines + fork log | Claude (input lane) |
| 2 | CL-003 GM-004 | CRITICAL (GM) / HIGH (CL) → CRITICAL | trigger releases have no wire redundancy when a press coincides and no corrector anywhere; recovery neutrality ignores L2/R2 | confirmed | Claude |
| 3 | CL-004 | HIGH | launcher release-repair probe at +24 ms lands inside the echo window; re-asserts unconfirmed `last_` | confirmed | Claude |
| 4 | CL-005 | HIGH | sender thread exits silently on flush/identity error | confirmed | Claude |
| 5 | CL-006 | HIGH | watchdog recovers on MISSING evidence; ladder can't cover the PS5 re-handshake | confirmed (`SidecarWatchdog.h:161-167`) | Claude (native) + Astra (sidecar) |
| 6 | CL-007 | HIGH | `inputRecoveryStarted` resets without neutralising owned output | confirmed | Claude |
| 7 | CL-008 | HIGH | No-Meter / Input-Timed switches the epoch join off | not re-read | OWNER: ship as a gate? |
| 8 | CL-009 CX-009 | HIGH (CL) / LOW (CX) → HIGH | affinity failure silent; E-cores ignored | not re-read | Astra |
| 9 | GM-002 CX-001 | CATASTROPHIC | `learning.json` unreadable/zero-byte → defaults silently, next save overwrites; no last-good | confirmed (`AppConfig.cpp:79-87`, `:235-242`) | Claude (persistence) |
| 10 | CX-002 | CATASTROPHIC (policy) | Stripe refund/dispute never revokes; worker handles only checkout/invoice/subscription events | confirmed (`worker.js:631-685`, no `charge.refunded`/`dispute`) | OWNER policy + Claude (worker/backend) |
| 11 | CX-013 | CRITICAL | kill-switch read error → `return False` = permission to continue | confirmed (`lambda_function.py:459-465`) | Claude (backend) |
| 12 | CX-014 | CRITICAL | audit write failure swallowed after the mutation committed | confirmed (`:441-445`) | Claude (backend); OWNER: outbox vs pre-write |
| 13 | CX-015 | CRITICAL | log sink: unbounded capacity wait can freeze GUI/input relay and shutdown | not re-read | Claude (launcher) |
| 14 | GM-001 | CATASTROPHIC (GM) → **LOW** | "entitlement cache not enforced": `entitlementOk` is computed and discarded, BUT in `ORION_PRODUCTION_BUILD` the lease gate is unconditionally on (`LeaseGate.cpp:33-38`), `automationSecurityAllowed()` refuses fire without a server lease (`:7441`), and both non-server `authenticated_ = true` sites are `#ifndef ORION_PRODUCTION_BUILD` (`:4734`, `:5368`). The residual is dead evaluation + misleading status, not a bypass; the debugger step is the generic memory-patch class. | re-read all four sites | Claude (hygiene); OWNER may overrule |
| 15 | GM-005 | CRITICAL (GM) → **MEDIUM** | minted key shown ephemerally to the minting staff when the customer's DMs are closed (`orion_bot.py:822-823`) — that is initial generation, visible to one staff member; policy call | confirmed | OWNER policy |
| 16 | GM-006 CX-007 CX-020 | HIGH | guided lead calibration: Cancel keeps partial changes; "initial candidate saved" claimed before applied | plausible (my code from 09-21) | Claude |
| 17 | CX-008 | HIGH | legacy learned-timing fields bypass validation | not re-read | Claude (AppConfig) |
| 18 | CX-016 | HIGH | calendar-month payment → fixed 30-day access | not re-read | OWNER policy + Claude (backend) |
| 19 | CX-017 | HIGH | interrupted order claims can stay pending forever | not re-read | Claude (backend) |
| 20 | CX-018 GM-012 | HIGH | optional DLL fallbacks outside the pre-load trust gate; no `SetDefaultDllDirectories` | not re-read | Claude (launcher); Codex verifies (S15) |
| 21 | CX-019 GM-018 | HIGH / LOW | diagnostics/logs expose console + network identifiers | not re-read | Claude (logging) |
| 22 | GM-007 GM-016 | HIGH / MEDIUM | VeniceNet token-file race; thread handle accumulation under churn | not re-read | Claude (service, deferred feature) |
| 23 | GM-008 | HIGH | UAC elevation profile mismatch leaves a standard user without valid install state | not re-read | Claude (installer) |
| 24 | GM-009 CX-003 CX-021 | MEDIUM | price/trial copy drift in the Worker profile template and one Discord response ("three-day trial") | plausible | Claude (worker) — quick |
| 25 | GM-010 | HIGH | dark-boot DirectShow failure → MSMF fallback → warm timing revoked (known trap) | known incident | Claude (capture) |
| 26 | GM-011 | HIGH | DualSense EPM USB drop (known; `fix_dualsense_usb_power.ps1`) | known incident | Claude (installer/docs) |
| 27 | CL-010 CL-011 CL-012 CL-018 CL-019 | MEDIUM | trigger travel MustDeliver; fire packet freezes pad; route binding InputTimed-only; pipe not recreated; transport log not relayed | confirmed (see CL report) | Claude (input lane) |
| 28 | CL-013 CL-014 CL-015 | MEDIUM | meter authority reset on route fault; shot-record corpus on D:\; CV self-arm churn | — | Astra; OWNER on CL-014 |
| 29 | CL-016 CL-017 | MEDIUM | debug flag reaches gkcrypt; AV-clock path unvalidated + hot-thread I/O | — | Astra |
| 30 | GM-013 GM-014 GM-015 GM-017 CX-006 CX-022 | MEDIUM | prior only capture-card; IP-drift probes; metrics full scan; console diag loss non-elevated; checksum bootstrap; dismissed secret in raw view | — | Claude / Astra (GM-013) |
| 31 | CL-020–025 CX-012 GM-019 | LOW | MMCSS, per-line flush, flick expedite, FF buckets, clock/handles, log literals, updater race interval, Inno manifest warning | — | Claude |

## Sequence

1. **Blocker set (rows 1–6, 27)** — Claude, fork first, ONE rebuild + ONE OrionStream redeploy, native rebuild, owner's chord ×20 + sprint-release ×20.
2. **Row 9** (learning last-good) — Claude, same native build.
3. **Rows 11, 12, 10** — backend/worker, one Lambda + Worker deploy on the owner's word (row 10 needs the refund/dispute policy first).
4. Astra: rows 8, 28, 29, and the sidecar half of 5.
5. Owner decisions before their rows start: 7, 10, 12 (approach), 14 (accept the downgrade?), 15, 18.
6. Codex + Gemini: re-verify the patched tree against their own ids; then Codex external.

**2026-09-22 ~01:50 — Claude lane PATCHED** (rows 1–7, 9, 10, 11, 12, 16, 24 and the CL-010 launcher half);
see `RED_TEAM_REPORT.claude.md` § PATCH APPLIED for files, tests and what is deployed. Owner: "you patch,
then I'll have Codex run the final." Deployed so far: OrionStream only.

## P6–P9 expansion — 2026-09-22 final read-only pass

Full ranked findings, file/line evidence, test results, fixes, and verification criteria:
`RED_TEAM_REPORT.codex.P6-P9.md`. This expansion is **blocked** overall. The old rows above
describe the initial patch wave; their “PATCHED” note is not a release approval. The current
`release/orion-package` is older than the audited native, updater, and input-fork builds.

| lane | new finding IDs | current decision | next owner/patch focus |
|---|---|---|---|
| P6 timing engine/learners | P6-01, P6-02, P6-03 | needs changes; learning recovery blocks | exact Auto/zero calibration rollback, production experiment gating, semantic last-good learning-file tests; do not ship the refuted global +10 ms shift |
| P7 local IPC/services | P7-01, P7-02, P7-03 | blocked | same-user input-pipe peer authentication, per-user elevated-service authority, clean-install ACL inspection; no admin-shell escalation has been demonstrated |
| P8 real-world robustness | P8-01 through P8-04 | blocked pending bounded recovery and rig matrix | hibernate/sleep, Wi-Fi/rest, clock jumps, dual instance/official client, two-PC licence, Windows update |
| P9 game-patch resilience | P9-01, P9-02 | blocked | fleet lock-rate alert, immediate explicit detection-unavailable state, signed emergency update drill from a standard-user Program Files install |
| release identity | P0-01 | blocked | frozen dual-tree integration, new signed package, StrictSecurity and clean-VM installation/rollback proof |

## P1–P5 current-code rerun — 2026-09-22

The new ranked, source-hash-anchored read-only audit is `RED_TEAM_REPORT.codex.P1-P5.rerun.md`.
Its **current** verdict is **blocked**: P1 input, P4 session/fork/package, and P5 recovery are
blocked; P2 timing/learners and P3 meter/sidecar need changes. It records 30/30 native tests,
147/147 fork unit tests, and 1,772 passing selected Python tests, plus an isolated current-fork
mixed-trigger probe. These are offline/dev results, not a passing production deployment.

The original rows above describe the initial wave and are not authoritative current status where
patched code differs. Current remaining source paths include mixed-trigger release redundancy,
same-user input-pipe peer authentication, semantic/range validation and transactional backup for
learning data, sidecar total-telemetry-silence health, unbounded repeated soft delivery faults,
exceptional sender exits, and permanent log-I/O backpressure. The on-disk release package is
older than the reviewed launcher/fork binaries. See the rerun report for severity ordered lowest
to highest, affected files, impact, reproduction, required fix, and verification for each item.
