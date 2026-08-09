# State of the Bot — fast-pickup handoff

*As of 2026-07-18, branch `feat/detection-template-anchor-qml-render` (nothing pushed to a
remote). Reconstructed from the commit log, tonight's live batch, and the docs cited inline.
Replaces the 2026-07-09 version. Companions: `docs/ARCHITECTURE.md`, `docs/HOW_IT_WORKS.md`,
`docs/ROADMAP_TO_JULY15.md`.*

---

## 1. CURRENT STATE — what ships today

- **Reader:** the shipped compiled bundle runs the **pure-CV `SimpleMeterReader`** (Python
  sidecar, `simple_meter_reader.py`) — torch/YOLO stripped, no Kalman, ~0.6 ms/frame, no ML
  model, zero décor false-locks. The **dev rig** (`run_orion`, `ORION_METER_LOCATOR=1`) runs the
  legacy MeterDetector + YOLO chain — **a different reader**; all live testing must use the
  shipped one (`docs/DETECTION_PLAN.md`, commit `a38bc25d`).
- **Bundle:** trimmed **~1.35 GB → ~500 MB** by rejecting debug DLLs + whitelisting only shipped
  models (`af25d3bf`, `docs/PACKAGE_FOOTPRINT.md`).
- **Installer:** bespoke Qt6/QML downloader **OrionSetup** matching the approved mockup
  (`60c2b620`), security-hardened — HTTPS-only, strict SHA-256, Authenticode verify, honest
  signed-state UI (`ca9b06c8`). First-run **coach-mark tour** replaced the Setup/Preflight tabs
  (`9d206e8f`).
- **Detection fix flags — ALL DEFAULT-OFF, byte-identical to HEAD when off** (this is the key
  status, and it contradicts any "flags now on" framing — they are implemented but **not yet
  live**):
  | Flag | Env | What it does | Default | Status |
  |---|---|---|---|---|
  | N1/N3 ANCHOR | `ORION_READER_ANCHOR` | floor/notch-anchored box velocity + median width (jitter σ 2.77→0.95, kills ~130px coast walk-off) | **OFF** | **Default-on candidate after ONE live A/B** |
  | N4 RISE_PROBATION | `ORION_READER_RISE_PROBATION` | preventive drop of a cold non-rising acquire (~5f) | **OFF** | Live-gated (needs hw-arm signal) |
  | N5 PCTL_FILL | `ORION_READER_PCTL_FILL` | occlusion-tolerant percentile fill read | **OFF** | Live-gated (no offline fill oracle) |
  | SHOT_COAST | `ORION_READER_SHOT_COAST` | keeps the lock alive through the arm-occlusion after the hw hold-trigger disarms → vision-timed release | **OFF** | **Unvalidated live** (offline: box survived 5/21→21/21 occlusion frames) |
- The ceiling-stack timing flags (H4/H5/H6/bandit/self-grade) also remain **default-OFF behind
  `AppConfig.h`** — `docs/LIVE_CEILING_VALIDATION.md` is the flip runbook.

---

## 2. THIS SESSION's work (commit-grouped)

**Detection fix set (the live-blind-fire investigation):**
- `a38bc25d` docs — synthesis + triage: confirmed the shipped reader is `SimpleMeterReader`,
  reconciled 9 external-AI strategies against the code.
- `121bef0c` — verified `SimpleMeterReader` fix set: N1/N3 ANCHOR (gate-safe), N4/N5 live-gated,
  N2 (full-track tip box) **reverted** (regressed peak-fill 93→77), reactive fake-lock breaker
  flipped default-off. All flags default-off, proven byte-identical.
- `998e0dd0` — **SHOT_COAST**: fixes the mid-shot blind fire (root cause: the no-disappear coast
  is gated on `armed`, but the hardware hold-trigger releases at the shot → `armed` flips false
  exactly when the arm occludes the meter → lock drops → blind release). A rising-red read latches
  a self-decaying 24f grace that protects the coast after disarm. **Written in direct response to
  tonight's batch** (`session 2026-07-18T04:2x`, `samples=0 fill 0.0%`).

**Patch-A churn fix:**
- `ccab3875` — stop the sidecar-restart churn. Patch A (`9f0ad728`) restarted the sidecar
  whenever the input hook was down 5s, but the live rig reads `connected=0` permanently (releases
  work via XUSB/ViGEm fallback) → re-fired every ~5s, spawning a new "Orion Stream" window each
  cycle (~7 restarts/session). Now **gated on the video feed also being stalled** → no churn when
  video is healthy.

**Live-path stall hardening:**
- `9f0ad728` — bounded input-link recovery watchdog + bounded capture enumeration.

**Server security (07-15 licensing hardening + red-team V2):**
- `293bbe56` — offline signing, key idempotency, edge-auth fail-closed (56 tests).
- `b4c3b01e` — ACTUAL 07-15 licensing hardening: 12 fixes (86 tests).
- `ce2bb84c` — revert-catching NEW-3/NEW-2 + orphan-retry tests (91 tests).
- `dabcdc52` — embed lease public key (NEW-1 client) + wire nefarius drivers.
- `69a230f7` — turnkey server-provisioning script (non-secret infra).
- `140f41a8` — Gumroad→SellHub migration: activate + webhook lambdas.
- *(uncommitted in working tree: `backend/lambda_function.py` + webhooks + backend tests — the
  red-team V2 fixes; see `docs/SERVER_DEPLOY_CHECKLIST.md`.)*

**Installer + packaging + trims:**
- `60c2b620` OrionSetup installer · `a642db80` contrast + disk-size polish · `ca9b06c8` installer
  security hardening · `af25d3bf` footprint trim 1.35GB→500MB · `2603620e` opt-in compiled-sidecar
  spawn + packager bundling · `9d206e8f` coach-mark tour.

**Red-team docs added this arc:** `REDTEAM_CONCURRENCY.md`, `REDTEAM_FINAL_LIVEPATH.md`,
`REDTEAM_SERVER_V2/V3.md`, `REDTEAM_CHIAKI_FORK.md`, `IP_PROTECTION_PLAN.md`.

---

## 3. WHAT NEEDS THE USER (blocks shipping / can't be done offline)

1. **AWS server deploy** — follow `docs/SERVER_DEPLOY_CHECKLIST.md`. The red-team V2 fixes are
   **uncommitted** and several **fail CLOSED** — they break the money path if infra is missing:
   - SSM params: `/orion/lease_signing_key`, `/orion/edge_auth_secret`, `/orion/webhook_bot_secret`,
     `/orion/worker_bot_secret`; ensure `/orion/ed25519_public_key` is a plain **String**.
   - DynamoDB tables: `orion-ratelimit`, `orion-update-manifest`.
   - Edge auth now enforced on `/api/bot/*` — webhook Lambdas + Cloudflare worker must inject
     `X-Edge-Auth`.
2. **Lease keypair → SSM** — generate the Ed25519 pair on a secure box (one-liner in the
   checklist §2). Private PEM → SSM `/orion/lease_signing_key` (never commit). Public `PUB_B64` →
   embed in client `LeaseGate::leaseVerifyPublicKey()`. Until that's non-empty, CRIT-1 is **not
   enforced client-side**.
3. **EV code-signing cert** — for the OrionSetup installer + the Nuitka single-exe
   (`IP_PROTECTION_PLAN.md`). Currently the "signed" state is honest-but-unsigned.
4. **Driver MSIs** — the nefarius ViGEm/HidHide drivers are wired (`dabcdc52`); the signed MSIs
   need to be dropped in for the installer to bundle.
5. **The live-validation grading loop** — see §4. The bot cannot be tuned to its real make-rate
   without live batches with real shots.

---

## 4. TONIGHT'S BATCH GRADE + OPEN QUESTIONS / NEXT

### Batch grade (2026-07-17 `session_20260717_231912`, 04:19–04:27Z)
The post-fix relaunch `session_20260718_001259` produced **no shots** (empty framedump, empty
detframes — user relaunched at 00:12, confirmed ~1 min uptime, then stopped). The gradeable batch
is `231912`, which ran the **DEFAULT build (all new flags OFF)** — so it is the **"before"** that
motivated SHOT_COAST, not an "after".

- **Releases: 8 fires, 100% `blindFire=1` — ZERO vision-timed.** `code=feedforward_target`
  throughout. `fresh=0` on the Left-Fade (seq 7) and the relaunch Go-To → totally blind. **Goal
  (samples>0 / vision-timed) NOT met** — expected, since the fix (SHOT_COAST) shipped default-off
  *after* this session.
- **Within-shot disappearances:** the blind fires with `fresh=0` ARE the manifestation. Raw
  det→undet transitions in-file were low (0/1/1/2), but detection coverage was thin. SHOT_COAST is
  the untested remedy.
- **Box width jitter (N1/N3): excellent even flags-off** — w σ 0.7–1.3px, range 30–36px. No width
  drift. (x/y spread is cross-shot meter repositioning, not within-lock jitter.)
- **False-locks: none evident** — rejections were overwhelmingly `roi_not_found` (conservative);
  no spurious décor locks.
- **anchor_found = 0 / 1428 detected frames** — consistent with ANCHOR default-off.
- **Churn: 3 sidecar restarts in ~3 min** (04:20:58 / 04:21:36 / 04:23:15), all from "input hook
  down 5s" — the exact Patch-A churn. Ran **pre-churn-fix build**. The post-fix relaunch had **0
  restarts** (but near-zero streaming, so unconfirmed under load).
- Prior batch `192510`: framedump exists (5098 frames) but **no detframes CSV survived** — a
  CSV-level before/after isn't possible.

**Net:** tonight validated the *problem* (blind fires + churn), not the fixes. The fixes exist,
are default-off, and need a real shot batch on the current HEAD to prove.

### Open questions / next
1. **Detection live A/B verdict is PENDING.** Run a real shot batch with `ORION_READER_ANCHOR=1`
   (+ SHOT_COAST) vs off. If N1/N3 hold and SHOT_COAST converts blind→vision-timed fires
   (`blindFire=0`, `samples>0`), **flip N1/N3 + SHOT_COAST default-on**. This is the top gate.
2. **Confirm the churn fix under load** — a full streaming session should now show ~0 sidecar
   restarts / one stable window.
3. **Overnight red-team + trim results** already landed — reference `docs/REDTEAM_FINAL_LIVEPATH.md`,
   `docs/REDTEAM_SERVER_V2/V3.md`, `docs/PACKAGE_FOOTPRINT.md`.
4. **Ceiling-stack flip** — after detection is clean, run `docs/LIVE_CEILING_VALIDATION.md` (a
   clean detection batch is its entry gate) to flip H4/H5/H6/plateau-aim on with evidence.

---

### Fast commands
- Grade a detection batch: `docs/LIVE_BATCH_RUNBOOK.md` (blind-fire %, lock rate, phantom locks).
- Regression gate (must stay green before any commit): `python tools/regression/run_gates.py`.
- Release timing evidence: `grep "Release freshness" logs/orion_native.log` → `blindFire=0` +
  `samples>0` is the win.
</content>
</invoke>
