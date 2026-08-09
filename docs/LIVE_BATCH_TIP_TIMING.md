# Live batch — autonomous tip timing (release gate)

Purpose: prove the tip-timing fixes on real hardware, and in the same session capture the
measurements that decide two still-open questions (is the predictor sigma honest, and does a learned
forecaster beat the current sampler). Offline tests are **not** release approval.

Baseline being replaced — session `2026-08-03T02:48:45Z..02:49:27Z`, capture-card + PIPE:
13 physical shot epochs, 10 evidence-backed ownerships, **2 releases, 8 `live_tip_deadline_missed`
aborts**. Every abort logged `lead_kind=factory lead_ms=241.400`; the estimator had already computed
196.0 and the engine never used it.

---

## 0. Pre-flight — just run the script

```powershell
powershell -ExecutionPolicy Bypass -File scripts\preflight_tip_batch.ps1
```

Read-only, ~2 s, exits non-zero if anything blocks. It checks every trap that has silently burned a
batch on this rig before: OBS holding the Elgato (`0xC00D3704`, opens-but-no-frames means *busy*, not
a wrong index), a VM stealing the `192.168.137.1` ICS address, a stale `OrionNative.exe`, a
pre-fix `241.4` prior or one that no longer matches its generator, `ORION_DETCSV` not being set, the
`-Framedump` clobber, whether every diagnostic emitter the post-batch greps rely on actually exists,
and disk headroom for the PNG dump.

Two things it does **not** cover:

- **No training running** — `nvidia-smi` under 10%. Training owns the GPU and muddies timing.
- **One launch only** — rate limits and clean telemetry both want a single connect.

> Not batch-blocking, but do it before you ship: `build/sidecar/autogreen_sidecar.dist/models/latency_factory_prior.json`
> still holds the pre-fix `241.4 ×4`. The dev launcher runs the sidecar from source and resolves the
> repo copy, so **the batch is unaffected** — but a release built from the existing `build/` tree
> would re-ship the bug. Rebuild with `scripts\build_orion_sidecar.ps1` and confirm the bundled JSON
> reads `sha256-45f3e2548dbe`. Likewise commit the staged five-file prior set together
> (`git diff --cached --name-only`) or the suite breaks for anyone who pulls.

---

## 1. Launch

Both routes get the same instrumentation. Run **capture card first** (it is the only route with any
measured latency evidence), then Remote Play.

```powershell
# measurement flags for this batch
$env:ORION_FILL_KALMAN = "1"      # kalmanTipMs / kalmanVel telemetry (already loads on the shipped path)
$env:ORION_FRAMEDUMP_INTERVAL = "0.04"
$env:ORION_FRAMEDUMP_MAX = "20000"

.\run_orion.local.ps1 -Framedump -Detdiag
```

- `-Detdiag` → per-frame DETDIAG log lines **and** `logs\diagnostics\detframes.csv`.
  **These are two independent gates.** DETDIAG lines are gated by `ORION_DETDIAG`; the CSV is gated
  separately by `ORION_DETCSV` (default `0`, deliberately off for shipping — it costs ~60 synchronous
  flushes/second on the capture thread). `-Detdiag` now sets both; before this fix it set only the
  first, so the usual liveness check passed while the CSV stayed stale. That is precisely how the
  08-03 batch was lost: `detframes.csv` was last written 2026-07-24 and every offline residual join
  had no data.
  **Verify BOTH before shooting:** grep the log for `DETDIAG`, *and* confirm
  `logs\diagnostics\detframes.csv` is actually growing. A passing DETDIAG grep alone does not prove
  the CSV is being written.
- `-Framedump` → raw PNGs to `logs\diagnostics\framedump`. Last real dump was 2026-07-24.
- `ORION_FILL_FORECAST=1` is already set in the launcher **but the forecaster does not load** — its
  init sits inside the retired legacy CV block that `ORION_SIMPLE_READER=1` bypasses, so it fails
  silently (`CV detector built` = 0 occurrences). Do not expect `predTipMs`. Getting forecaster data
  needs its load moved next to the Kalman's first; that is a code change, not a flag.

Switch routes by `videoSource` (capture_card ↔ decoder) and re-run the whole matrix.

---

## 2. The batch — aim 25–35 shots per route

The brief requires every mode and both controller routes represented, because the canonical timing
authority is supposed to be unified across them:

- 8–10 **Standstill** (the bread-and-butter timing check)
- 4–6 **No-dip**
- 6–8 **Fades** (left + right)
- 4–6 **Moving / off-dribble**
- 3–4 **Go-To** (its abort/trust gate must still hold — it never blind-fires)
- 3–4 **Tempo Square**
- 3–4 **Tempo Stick**

Cover **both controller routes** where available (PIPE and ViGEm). `decoder-*` and `*-vigem` have
never been measured even once — their priors are pure declared ignorance, so the first real labels on
those routes matter more than anything else in this batch.

**Deliberately include ~3 stress shots**: one right after a cutscene, one with a defender close, one
near the scorebug side of the screen.

---

## 3. Acceptance criteria (all must hold)

1. Every valid owned physical shot either produces **exactly one timely release marker** or a
   truthful explicit fail-closed reason. No third outcome.
2. **No unsolicited shots** — zero releases without a physical shot epoch.
3. **No writes after a missed deadline.** Any `rejected_missed` must show 0 markers.
4. **No menu/loading false locks**, and no high-fill carryover relock.
5. Tip-position evidence measured from **genuine meter frames**, not the self-grade (which is
   explicitly "not timing truth" and reports `errorMs=0.0`).
6. Standstill, no-dip, fades, Go-To, Tempo Square and Tempo Stick all represented.
7. Both controller routes represented where available.

---

## 4. Post-batch — capture before any relaunch

```powershell
$ts = Get-Date -Format yyyyMMdd_HHmm
Copy-Item logs\diagnostics\detframes.csv logs\diagnostics\detframes_TIP_$ts.csv
Copy-Item logs\orion_native.log            logs\orion_native_TIP_$ts.log
```

### What to grep — the new diagnostics

```powershell
# 1. Did the lead ever leave the seed?  (08-03: it never did — all 8 aborts read 241.400)
Select-String -Path logs\orion_native.log -Pattern "lead_kind=\w+ lead_ms=[\d.]+" |
  ForEach-Object { $_.Matches.Value } | Group-Object | Sort-Object Count -Desc

# 2. Reservation lifecycle per shot
Select-String -Path logs\orion_native.log -Pattern "TIP RESERVATION:"

# 3. Every miss, ATTRIBUTED
Select-String -Path logs\orion_native.log -Pattern "reservation_disposition=\w+" |
  ForEach-Object { $_.Matches.Value } | Group-Object
```

`reservation_disposition` is the key readout, and each value points somewhere different:

| value | meaning | where to look |
|---|---|---|
| `unschedulable_lead` | deadline was already past the first time it could be computed | the **route prior** is wrong for this geometry |
| `deadline_missed_before_validity` | deadline was future, but the estimate never became authoritative in time | the **predictor / sigma gate** |
| `authority_lost_before_submit` | was valid, then a lease broke | route / detector / controller transition |
| `no_reservation` | no plan was ever held | ownership or estimate never formed |

### Watch these live

- **The first accepted label of each session moves the lead.** One observation may pull it up to
  ±33.9 ms and snaps the published authority SD from 28.7 to 6.0. That is the bounded-blend design
  working, but if greens run consistently ~2 frames late immediately after the first label, suspect a
  mislabelled freeze — the walk-back takes several labels because the now-tightened posterior will
  reject the first clean corrective one.
- **`measured_latency_n` climbing while `lead_kind=factory`.** It is published as *validation-capable*
  evidence, so corroboration labels must not advance it. If it ever reaches 6 with kind still
  `factory`, the engine loses lead authority and stops shooting — that is a regression, not a rig
  fault.
- **`reservation_disposition` on every miss.** It routes you to the right subsystem (see the table
  above). Cross-check it against the raw fields on the same line — `lead_kind`/`lead_ms`/`lead_sd_ms`,
  `tip_eta_ms`, `reservation_first_tip_eta_ms` — rather than trusting the label alone.

### Measurements that decide the open questions

- **Is the sigma heuristic honest?** Join each `TIP RESERVATION` / `TIP DEADLINE DECISION`
  prediction against the actual tip frame from `detframes.csv`. Compare the real residual at a
  ~200 ms horizon against the modelled `predictor_sigma_ms` (currently `6.0 + 0.20*horizonMs`,
  producing 43.6–56.6). If real residuals are much smaller, the validity gate is needlessly blocking
  shots and no model is needed — only honest sigma.
- **Does the Kalman beat the sampler?** `kalmanTipMs` / `kalmanVel` are already emitted. Grade both
  against the actual tip. The Kalman also yields a principled covariance, which is exactly what
  `combinedSigma <= 65` spends.
- **Per-route lead.** Any accepted label on `decoder-*` or `*-vigem` is the highest-value timing
  measurement currently available — those three profiles have `evidence_n=0`.
- **Arm-path check.** Confirm from the new instrumentation whether the reader is armed at the
  physical edge (t=0) or only at ownership (~t+430 ms). Unarmed acquisition costs ~1.2 frames of
  meter and is a wiring question, not a detection one.

---

## 5. Known non-goals for this batch

- **Inbound meter delay is shelved.** `MeterDelayController` and the `nexus_svc` intercept are built
  and unit-tested but deliberately **inert** — nothing wires them. Do not enable them; they are not
  part of this gate.
- Do **not** claim 100% tip accuracy or production readiness from offline results. This batch is the
  evidence, not a formality.
