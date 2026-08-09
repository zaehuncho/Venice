"""FROZEN-METER ORACLE v2 -> a live, self-measured release-path latency (ms).

The engine historically modeled ~13ms of release latency while the real capture->detect->
release->animation-stop->encode->decode loop is ~40-100ms. This estimator MEASURES it live
instead of hardcoding it, using the shot meter itself as ground truth:

  * FROZEN-METER ORACLE: after a release the shot meter stops rising and FREEZES at some value
    F_stop. Because our view of the meter is delayed, the OBSERVED fill keeps rising after the
    release command and only reaches F_stop L milliseconds later. Inverting the observed rise
    fill(t) to the instant f(t*) == F_stop and subtracting the release-command timestamp yields
    a supervised per-shot release-path latency label (the release ts and the fill samples are on
    the SAME wall clock — capture-epoch ms since A0 — so the subtraction is exact).

v2 upgrades (design: async-whistling-pelican plan, A2):

  * RISE-vs-DEFLATE DISAMBIGUATION: a freeze is label-eligible ONLY when the view approached
    F_stop from BELOW (game froze mid-rise). A rise->cap->deflate->freeze (a LATE release) used
    to fall into the last-below-sample fallback and emit a garbage label; it is now classified
    (`freeze_kind == "deflate"`) and routed AWAY from the label stream (grading input, Phase 2).

  * PLATEAU-CENSOR FIX (the measured dominant error): the old `f < F_stop - 0.05` censor let
    noisy FROZEN-plateau samples (sigma ~0.35-0.6pp) leak into the inversion tail as zero-slope
    late points, dragging every label ~+35ms LATE (Part-0 M5 simulation on 39 real rises:
    bias +28..+38ms, sigma ~25ms). Widening the censor margin to 1.0pp (~3 sigma of plateau
    jitter) collapses the bias to ~+1ms and sigma to ~8ms. The margin-fixed LINEAR tail fit is
    the label path — the template inversion measured strictly WORSE on the same close events
    (prior-dominated truncated fits) and is retained only for grading (Phase 2), not labels.
    Slope-fallback labels (flat tail, no fittable rise) are DROPPED, not guessed.

  * OPTIONAL L_FIXED DECOMPOSITION + CONJUGATE POSTERIOR: when a caller has an RTT measurement
    from the same physical command/effect loop, each label can be decomposed
        label_fixed = L_total - rtt_ms(shot) - TICK_WAIT_EXPECT
    (rtt measured per shot when provided; console-tick wait = its 8.3ms expectation until a
    tick-phase lock exists) and folded into a conjugate-normal posterior on L_fixed
    (prior mu0=60, sd0=12) with a per-label sigma from the local fill slope, a MAD outlier gate,
    and a two-same-side-rejections regime-change escape. Convergence: sd <= 3.3ms in <=4-5 shots.
    Consumption reconstitutes L_total = l_fixed + rtt_now + tick_wait — portable across Wi-Fi
    wobble. The rolling median of raw totals is retained as telemetry/back-compat (`value_ms`
    reports the posterior reconstitution once labels exist).

Phase-2 (A2(c)): the tick-staggered warmup probe run additionally pins the ABSOLUTE console
input-tick phase — per-probe (press_phase, raw) pairs fit the sawtooth
L_k = C + ((phi_edge - phi_k) mod 16.667) over a 64-point phi_edge scan; exposed as
tick_phase_ms / tick_phase_conf / tick_phase_sd_ms (conf halves per 10 minutes since the run —
session-scoped, self-retiring). Probes ONLY: a passive fill-step PLL cannot separate tick drift
from latency drift, so no passive phase path exists by design.

numpy-only in the hot path (the template fit reuses tip_registration_infer's scipy core when
available; the estimator degrades to the linear inversion without it). Never raises into the CV
loop.
"""
from __future__ import annotations

import logging
import os
import ctypes
import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("latency_estimator")

# Phase tags written to detframes.csv (mtr_phase column) so the oracle join + a live batch can
# separate the shot-meter freeze (the oracle signal) from a later post-shot feedback-UI plateau.
PHASE_NONE = "none"
PHASE_RISE = "rise"
PHASE_FROZEN = "frozen"     # flat + attributable to a recent release == the F_stop freeze
PHASE_PLATEAU = "plateau"   # flat but NOT release-attributable == post-shot feedback UI / idle hold

# Console input-sampling wait expectation (game samples input at 60Hz -> uniform 0..16.7ms).
# Subtracted from every label as a CONSTANT until a tick-phase lock provides the exact value;
# its per-shot scatter lives in the label sigma (TICK_WAIT_SD) instead.
TICK_WAIT_EXPECT_MS = 8.3
TICK_WAIT_SD_MS = 4.8

# Console input-sampling tick period (60Hz).
TICK_PERIOD_MS = 1000.0 / 60.0

# The first clean controlled release establishes telemetry only. A distinct second release must
# use L1 to predict an in-green non-cap stop, then visibly land within its slope/SD-derived target
# residual before provisional authority exists. Ordinary observations may refine that validated
# route but can never create provisional authority by themselves. Persisted authority remains
# stricter: only a fully converged, RTT-decomposed posterior is written and it must be paired with
# a current verified RTT after every process start.
PROVISIONAL_MAX_SD_MS = 6.0
CONVERGED_MAX_SD_MS = 3.3
CONVERGED_MIN_LABELS = 6
# [ORION_REOPEN_SOFT] Regime-reopen soft landing (env ORION_LATENCY_REGIME_REOPEN_SOFT, default
# OFF -> behaviour is bit-identical to before the flag existed). MEASURED 2026-08-06 (physical
# epochs 50/51 of the 46/50 counted batch): two same-side tail outliers (seq 47 total=329.1
# rejected, seq 48 total=272.9 force-accepted) tripped the regime escape in _ingest_label,
# inflated the posterior sd 2.3 -> 5.8 past CONVERGED_MAX_SD_MS, ready_for_native dropped, and
# the native gate refused to arm (IDLE-GATE: reason=waiting_for_latency_calibration) for the rest
# of the session -- the recovery path benched the bot and the owner's own held press carried both
# shots ~1.7s LATE. With the flag ON, the escape still reopens the LEARNING posterior exactly as
# before, but the last converged authority (mu/var/n snapshotted at the instant of the escape)
# stays PUBLISHED through value_ms/l_fixed_*/ready_for_native while the reopened posterior
# re-converges: a reopen affects learning, never the ready gate. The retained authority is
# dropped (fail closed, back to honest warming) when any of:
#   * accepted evidence contradicts the RETAINED posterior too -- a second escape whose label
#     lies outside the retained 4-sigma/25ms radius (the world genuinely changed, e.g. the
#     125.7 -> ~200 shift of 2026-08-06T10:38Z), rather than snapping back to it (the
#     transient-outlier shape of the 329.1/272.9 pair);
#   * _REOPEN_GUARD_MAX_LABELS accepted labels fail to re-converge the live posterior;
#   * the guard outlives _REOPEN_GUARD_MAX_AGE_MS on the frame wall clock;
#   * any posterior reset/revocation (also structural: the guard self-deactivates if n
#     regresses below its arm-time count, so a reset path that forgets the explicit drop
#     still cannot leave a stale retained authority published).
# The guard can only ever arm at the instant a CONVERGED (n >= CONVERGED_MIN_LABELS,
# sd <= CONVERGED_MAX_SD_MS, route-attested) posterior takes the escape, so genuinely absent or
# still-warming authority can never be retained. The distinction this encodes: "a good prior
# that just got re-opened for learning" is publishable; "no idea what the latency is" never is.
_REOPEN_GUARD_MAX_LABELS = 8
_REOPEN_GUARD_MAX_AGE_MS = 120000.0
# A route cache is only a shortcut around repeating an already-completed controlled L1/L2
# calibration.  It never creates authority from ordinary observations, so the persistence floor is
# the same two distinct labels required by that protocol rather than the legacy six-shot batch.
PERSISTED_MIN_LABELS = 2
# [ORION_PROBE_PERSIST] A probe RUN is "complete" once this many closed probes produced accepted
# labels (the run needs a calibrated probe_spawn_offset_ms to label at all). Three matches the
# operational floor measured 2026-08-05: three pump-fake presses with ball possession close three
# probes, and three sigma-8 labels pull the posterior sd under the 6ms provisional ceiling.
_PROBE_VALIDATED_MIN_LABELS = 3
PERSISTED_MAX_AGE_S = 72.0 * 60.0 * 60.0
# 3 (2026-08-04): the label domain changed from 'extrapolated up-crossing of F_stop' to
# 'observed freeze onset' (~70ms longer). A cached mu from the old domain would contradict
# every fresh label and self-revoke through the contradiction gate, so cut it cleanly.
_CACHE_VERSION = 3
_CACHE_MAGIC = b"ORION-LATENCY-V2\0"
_ONLINE_PROCESS_VAR_MS2 = 1.0

# Prior-blended actuation authority.
#
# A factory profile is a WEAK route seed, not a measurement of this machine on this night. Freezing
# the published actuation lead at that seed while the posterior silently learns the truth is what
# produced the 2026-08-03 session: the estimator computed 196.0ms and the engine actuated on 241.4ms
# for all 13 shots, so every scheduled command deadline landed in the past.
#
# The published factory-kind authority is therefore the posterior, but bounded: an accepted label may
# only pull the actuation lead a limited distance away from the seed, and that allowance widens with
# corroboration. One observation can move the lead meaningfully; it can never own it. The underlying
# posterior (`value_ms`) is left untouched so telemetry, cache persistence and the validated-authority
# promotion path all keep seeing the honest Bayesian estimate.
#
#   k(n) = SCALE * (1 - exp(-n / TAU))   ->  k(1)=1.18  k(2)=1.90  k(3)=2.33  k(6)=2.85
#   allowance_ms = k(n) * factory_sd_ms
_AUTHORITY_MOVE_K_SCALE = 3.0
_AUTHORITY_MOVE_K_TAU = 2.0
_FACTORY_PRIOR_SCHEMA = "orion.latency_factory_prior.v1"
_FACTORY_PRIOR_PATH = Path(__file__).resolve().parent / "models" / "latency_factory_prior.json"

# A controlled, non-cap L2 that misses its planned stop is still causal evidence, but it is not a
# successful validation. Permit one bounded re-anchor per cold-start epoch so the next distinct L2
# can be planned from the observed route instead of repeating the same miss forever. The recovery
# never increments N, never latches provisional authority, and never reduces posterior uncertainty.
# Cap-saturated, deflating, noisy, or unrelated motion remains completely ineligible.
_CONTROLLED_RECOVERY_MAX_COUNT = 1
_CONTROLLED_RECOVERY_MAX_RESIDUAL_PCT = 15.0
_CONTROLLED_RECOVERY_MAX_CORRECTION_MS = 60.0


@dataclass(frozen=True)
class LatencyTelemetrySnapshot:
    """One immutable, lock-coherent view of every wire-visible estimator field.

    The CV thread closes oracle opportunities while the sidecar stdin thread can
    concurrently open a new release.  Publishing individual properties allows a
    telemetry payload to combine different estimator generations (for example,
    the new sample count with the old posterior deviation).  Only this snapshot
    is safe to serialize across those threads.
    """

    # Shadow/observed posterior.  This value is allowed to move after every
    # accepted label and is never, by itself, permission to actuate.
    value_ms: float
    confidence: float
    bootstrapped: bool
    # Explicit actuation contract.  ``factory`` always publishes the immutable
    # packaged route prior; ``validated`` publishes the causally validated
    # posterior; ``none`` is fail-closed.  Consumers must never infer authority
    # from value_ms/N/SD independently.
    authority_kind: str
    authority_value_ms: float
    authority_sd_ms: float
    factory_prior_active: bool
    factory_prior_source: str
    factory_prior_version: str
    factory_prior_sd_ms: float
    n_labels: int
    l_fixed_ms: float
    l_fixed_sd_ms: float
    ready_for_native: bool
    controlled_anchor_available: bool
    provisional_ready: bool
    restored_from_cache: bool
    restored_route_attested: bool
    label_starved: bool
    last_status: str
    last_rejection: str
    label_method: str
    rtt_regime: str
    last_probe_raw_ms: float
    tick_phase_ms: float
    tick_phase_conf: float
    tick_phase_sd_ms: float
    release_seq: int
    pending_release_seq: int
    pending_release: bool
    phase: str
    freeze_kind: str
    # How many of ``n_labels`` are protocol-unvalidated corroboration observations. Purely
    # diagnostic: it explains a moving posterior that is deliberately still ``factory`` authority.
    corroboration_labels: int = 0
    # [ORION_PROBE] online D_spawn observable; -1 = not derivable yet. Telemetry only.
    probe_spawn_estimate_ms: float = -1.0
    # [ORION_PROBE_PERSIST]/[ORION_AUTO_PROBE_ONBOARD] flag-gated (default OFF -> False/0).
    # probe_recommended is the fresh-install onboarding hint; probe_prior_restored marks a
    # factory seed that came from this rig's own persisted probe run; probe_labels counts
    # probe-derived labels in the posterior. All telemetry-only.
    probe_recommended: bool = False
    probe_prior_restored: bool = False
    probe_labels: int = 0
    # [ORION_REOPEN_SOFT] True while a retained pre-reopen converged authority is being
    # published (flag-gated, default OFF -> always False). Telemetry/diagnostics only: native
    # gates on the numeric authority tuple, never on this bit.
    reopen_guard_active: bool = False


def _env_flag(name: str, default: bool = False) -> bool:
    """Boolean env flag: unset -> default; '0'/'false'/'no'/'off' -> False; else True."""
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in ("0", "false", "no", "off")


def _estimator_locked(method):
    """Serialize an estimator operation on its re-entrant state lock."""

    @wraps(method)
    def _locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return _locked


def _scope_digest(route_scope: str) -> str:
    return hashlib.sha256(str(route_scope).encode("utf-8", "strict")).hexdigest()


def _load_factory_prior(route_scope: str):
    """Load a versioned weak total-latency prior for zero-setup first-shot ownership.

    The model is release-manifested data, not an actuation constant hidden in code. A profile must
    match the exact non-empty route scope; malformed/missing data returns no value. Native repeats
    the model/source/version bounds and additionally requires the current direct route token before
    it can use this weak prior.
    """
    scope = str(route_scope or "").strip().lower()
    if not scope:
        return None
    try:
        raw = _FACTORY_PRIOR_PATH.read_bytes()
        if not raw or len(raw) > 64 * 1024:
            return None
        payload = json.loads(raw.decode("utf-8", "strict"))
        if (not isinstance(payload, dict)
                or payload.get("schema") != _FACTORY_PRIOR_SCHEMA):
            return None
        model_id = str(payload.get("model_id", "") or "").strip()
        model_version = str(payload.get("model_version", "") or "").strip()
        profiles = payload.get("profiles")
        if (not model_id or len(model_id) > 64 or not model_version
                or len(model_version) > 32 or not isinstance(profiles, list)):
            return None
        matches = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            terms = profile.get("scope_contains")
            if (not isinstance(terms, list) or not terms
                    or any(not isinstance(term, str) or not term.strip()
                           for term in terms)):
                continue
            lowered = [term.strip().lower() for term in terms]
            if not all(term in scope for term in lowered):
                continue
            mean_ms = float(profile.get("mean_ms", 0.0) or 0.0)
            sd_ms = float(profile.get("sd_ms", 0.0) or 0.0)
            name = str(profile.get("name", "") or "").strip()
            if (not name or len(name) > 48 or not np.isfinite(mean_ms)
                    or not 15.0 <= mean_ms <= 500.0 or not np.isfinite(sd_ms)
                    or not 6.0 <= sd_ms <= 100.0):
                continue
            matches.append((len(lowered), mean_ms, sd_ms, name))
        if not matches:
            return None
        _, mean_ms, sd_ms, name = max(matches, key=lambda row: row[0])
        return {
            "mean_ms": mean_ms,
            "sd_ms": sd_ms,
            "source": f"{model_id}:{name}",
            "version": model_version,
        }
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _default_cache_path(route_scope: str) -> Optional[Path]:
    """Return a per-route user cache path, or None when safe reuse cannot be scoped.

    An empty/unknown route deliberately disables reuse: decoder and capture-card fixed latency
    are not interchangeable.  Production is Windows-only, where the payload is DPAPI protected.
    """
    scope = str(route_scope or "").strip()
    if not scope or os.name != "nt":
        return None
    override = str(os.environ.get("ORION_LATENCY_CACHE_PATH", "") or "").strip()
    if override.lower() in ("0", "off", "false", "none", "disabled"):
        return None
    if override:
        return Path(override)
    base = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if not base:
        return None
    return Path(base) / "Orion" / "timing" / (
        "latency-" + _scope_digest(scope)[:24] + ".bin")


def _dpapi_transform(data: bytes, entropy: bytes, *, protect: bool) -> Optional[bytes]:
    """Protect/unprotect one cache blob with the current Windows user via DPAPI.

    This detects corruption and blocks cross-user reuse; it is not an authority boundary against
    malicious code already running as the same Windows user. Native freshness/epoch/SD gates and
    the current verified RTT remain the release-safety boundary. There is no plaintext fallback.
    """
    if os.name != "nt" or not data:
        return None
    try:
        from ctypes import wintypes

        class _DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        def _blob(raw: bytes):
            buf = ctypes.create_string_buffer(raw)
            return (_DATA_BLOB(len(raw), ctypes.cast(
                buf, ctypes.POINTER(ctypes.c_ubyte))), buf)

        source, source_buf = _blob(bytes(data))
        entropy_blob, entropy_buf = _blob(bytes(entropy))
        output = _DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if protect:
            fn = crypt32.CryptProtectData
            fn.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p,
                           ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(_DATA_BLOB)]
            ok = fn(ctypes.byref(source), "Orion live timing calibration",
                    ctypes.byref(entropy_blob), None, None, flags,
                    ctypes.byref(output))
        else:
            fn = crypt32.CryptUnprotectData
            fn.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
                           ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(_DATA_BLOB)]
            ok = fn(ctypes.byref(source), None, ctypes.byref(entropy_blob),
                    None, None, flags, ctypes.byref(output))
        # Keep ctypes buffers live until the OS call returns.
        _ = (source_buf, entropy_buf)
        if not ok or not output.pbData or output.cbData <= 0:
            return None
        try:
            return ctypes.string_at(output.pbData, int(output.cbData))
        finally:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
    except Exception:
        return None


def fit_tick_phase(pairs, period_ms: float = TICK_PERIOD_MS, grid: int = 64):
    """[Phase-2 A2(c)] Fit the console input-tick EDGE phase from tick-staggered probe raws.

    pairs: [(press_wall_ms, raw_ms)] — the warmup probe run staggers presses k*2.083ms across
    the 16.7ms tick, so the raw press->appear latencies trace the sawtooth
        raw_k = C + ((phi_edge - phi_k) mod P),   phi_k = press_k mod P
    (a press landing mid-tick WAITS for the next input-sample edge). Scan phi_edge over `grid`
    points in [0, P), solve C in closed form per candidate (mean residual), keep the RMS-minimal
    edge. Returns (phi_edge_ms, rms_ms, c_ms) or (None, -1.0, 0.0) when unfittable.

    IDENTIFIABILITY (plan [fix]): absolute phase comes ONLY from probes — a passive fill-step
    PLL cannot separate console-tick drift from latency drift, so no passive phase path exists.
    """
    try:
        P = float(period_ms)
        if not pairs or len(pairs) < 4 or P <= 0.0:
            return None, -1.0, 0.0
        press = np.asarray([p for p, _ in pairs], float)
        raw = np.asarray([r for _, r in pairs], float)
        if not (np.isfinite(press).all() and np.isfinite(raw).all()):
            return None, -1.0, 0.0
        phases = np.mod(press, P)
        best_phi, best_rms, best_c = None, np.inf, 0.0
        for j in range(int(grid)):
            phi = P * j / float(grid)
            wait = np.mod(phi - phases, P)
            resid = raw - wait
            c = float(resid.mean())
            rms = float(np.sqrt(np.mean((resid - c) ** 2)))
            if rms < best_rms:
                best_phi, best_rms, best_c = phi, rms, c
        if best_phi is None:
            return None, -1.0, 0.0
        return float(best_phi), float(best_rms), float(best_c)
    except Exception:
        return None, -1.0, 0.0


class LatencyEstimator:
    def __init__(
        self,
        boot_prior_ms: float = 0.0,
        window: int = 12,
        freeze_win: int = 4,
        freeze_eps_pct: float = 1.2,
        min_release_fill_pct: float = 20.0,
        post_release_window_ms: float = 900.0,
        boot_shots: int = 3,
        lo_ms: float = 15.0,
        hi_ms: float = 180.0,
        mu_prior_ms: float = 60.0,
        sd_prior_ms: float = 12.0,
        f_stop_max_pct: float = 95.0,
        route_scope: str = "",
        cache_path: Optional[str] = None,
        cache_max_age_s: float = PERSISTED_MAX_AGE_S,
        restore_cache: bool = True,
        factory_prior_source: str = "",
        factory_prior_version: str = "",
        factory_prior_sd_ms: float = 0.0,
        probe_persist_enabled: Optional[bool] = None,
        probe_onboard_enabled: Optional[bool] = None,
        probe_prior_authority: Optional[bool] = None,
        regime_reopen_soft: Optional[bool] = None,
    ) -> None:
        # Frame updates, native release/probe markers, route invalidation, and
        # telemetry serialization run on different sidecar threads.  RLock is
        # required because public operations deliberately compose private
        # mutation helpers while retaining one atomic state transition.
        self._lock = threading.RLock()
        # boot_prior_ms is the immutable packaged route authority until the observed posterior
        # independently earns validated authority.  Accepted labels may move the shadow value in
        # the meantime, but they never rewrite this provenance-bound mean in place.  A zero value
        # means no factory authority is available and native remains fail-closed.
        self._boot_prior_ms = float(boot_prior_ms)
        self._factory_prior_source = str(factory_prior_source or "").strip()[:112]
        self._factory_prior_version = str(factory_prior_version or "").strip()[:32]
        self._factory_prior_sd_ms = float(factory_prior_sd_ms)
        self._window = int(window)
        self._freeze_win = int(freeze_win)
        self._freeze_eps = float(freeze_eps_pct)
        self._min_release_fill = float(min_release_fill_pct)
        self._post_release_ms = float(post_release_window_ms)
        self._boot_shots = int(boot_shots)
        self._lo, self._hi = float(lo_ms), float(hi_ms)
        # Label-eligibility ceiling on F_stop: above this the crossing sits inside the flat cap.
        # HARD (M5: 95-99 stays ill-conditioned even margin-fixed — sd ~24ms). When the label
        # stream starves (a high green rate freezes above 95), the fix is warmup probes, not a
        # relaxed gate; `label_starved` is exposed so the UI can surface the probe prompt.
        self._f_stop_max = float(f_stop_max_pct)
        self._releases_since_label = 0

        # (wall_ms, fill_pct) of the CURRENT rise, reset when the meter leaves / a shot completes.
        self._rise: deque = deque(maxlen=256)
        self._rise_peak = 0.0
        # rolling measured per-shot TOTAL latencies (ms) — telemetry / robust cross-check.
        self._labels: deque = deque(maxlen=self._window)
        # pending release-command timestamp (wall ms) + per-shot measured RTT awaiting a freeze.
        self._pending_release_ms: Optional[float] = None
        self._pending_release_seq: int = -1
        self._pending_rtt_ms: float = 0.0
        self._pending_rtt_known: bool = False
        self._pending_release_calibration: bool = False
        # Present only on the second-stage controlled marker. Native predicts this stop from the
        # first causal label and sends it only after the matching controller edge is delivered.
        self._pending_validation_target_pct: Optional[float] = None
        self._pending_validation_tolerance_pct: Optional[float] = None
        self._last_release_ms: Optional[float] = None
        self._release_seq: int = 0
        # Native release ids are monotonic for the process and survive AutomationEngine::reset().
        # A duplicate or delayed marker must never supersede the live pending opportunity.
        self._last_native_release_seq: int = 0
        self._measured_ms: Optional[float] = None
        self._conf: float = 0.0
        self._phase: str = PHASE_NONE
        self._frozen_captured = False   # oracle already closed for the current freeze
        self._n_labels = 0
        # One posterior must never mix two measurement domains. The first accepted label locks
        # the estimator to either:
        #   * "decomposed": every label has verified RTT removed; unknown-RTT labels are rejected.
        #   * "total": RTT was unknown initially, so every later label remains end-to-end total.
        # In total mode, the first subsequently verified live RTT becomes a zero-jump delta
        # baseline; later RTT drift adjusts the published total without rewriting the posterior.
        self._rtt_regime: Optional[str] = None
        self._total_mode_rtt_baseline_ms: Optional[float] = None
        self._last_present_ms: Optional[float] = None

        # --- v2: conjugate-normal posterior on L_fixed -------------------------------------------
        self._prior_mu = float(mu_prior_ms)
        self._mu = self._prior_mu
        self._var = float(sd_prior_ms) ** 2
        self._prior_var = float(sd_prior_ms) ** 2
        self._fixed_accepted: deque = deque(maxlen=8)   # accepted label_fixed values (MAD gate)
        self._rtt_used: deque = deque(maxlen=8)         # rtt_ms subtracted per accepted label
        # Measured crossing->lock lag (ms) per accepted label. This is the per-install,
        # self-calibrated replacement for the machine-specific ORION_LEAD_FLOOR_MS constant:
        # native plans fill-domain STOP targets in the crossing domain but actuates on the full
        # lock-domain lead, so it needs the gap. Frozen-in-place installs measure ~0 naturally.
        self._lock_lag: deque = deque(maxlen=8)
        self._reject_streak = 0                          # consecutive same-side rejections
        self._reject_side = 0                            # -1 / +1
        self._last_label_method = ""       # "linear" | "probe" | "calibration" | "dropped"
        self._last_freeze_kind = ""                      # "rise" | "deflate" | "ambiguous"
        self._last_freeze_f_stop = -1.0
        self._last_freeze_peak = -1.0
        # Wire-visible observability only. These strings never participate in
        # estimator math; they explain why value_ms can legitimately remain 0.
        self._last_status = "awaiting_release"
        self._last_rejection = ""
        # A controlled marker is emitted only after native has proven a current genuine rising
        # meter and the local controller route has accepted the release. Latching that fact lets
        # later ordinary shots corroborate a slow route without allowing a cold passive/menu
        # label to widen the physical range or create provisional authority.
        self._has_controlled_anchor = False
        self._first_controlled_seq = -1
        self._controlled_validation_ready = False
        self._controlled_recovery_count = 0
        # How many of ``_n_labels`` are protocol-UNVALIDATED corroboration observations (clean
        # post-L1 controlled traces that native never named a validation target for). They refine
        # the posterior but are subtracted out of every count that can mint validated authority.
        self._corroboration_labels = 0

        # Route-scoped persisted posterior. The production oracle measures one end-to-end TOTAL
        # command-to-visible-effect value (the public-court RTT is unrelated), so only a successful
        # controlled L1/L2 total-domain posterior may be cached. DPAPI has no plaintext fallback.
        # Restore is telemetry-only until the orchestrator independently proves a fresh frame on the
        # exact scoped video route; native applies a second, independent controller-delivery proof.
        self._route_scope = str(route_scope or "").strip()
        self._cache_path = (Path(cache_path) if cache_path else
                            _default_cache_path(self._route_scope))
        self._cache_max_age_s = max(0.0, float(cache_max_age_s))
        self._restored_from_cache = False
        self._restored_route_attested = False

        # --- [ORION_PROBE] warmup pump-fake probe (press -> meter-appear) --------------------
        self._pending_probe_ms: Optional[float] = None
        self._pending_probe_rtt = 0.0
        self._pending_probe_rtt_known = False
        self._pending_probe_spawn = 0.0
        self._probe_rise: list = []
        self._last_probe_raw_ms = -1.0
        # Closed-probe raw press->meter-appear values of the current run, and how many probe
        # LABELS were ingested. The label count is the circularity guard for the online D_spawn
        # observable below: once probe labels are in the posterior, the posterior can no longer be
        # used as an independent reference for the constant those labels were computed with.
        self._probe_raw_run: list = []
        self._probe_labels = 0

        # --- [Phase-2 A2(c)] console input-tick phase from the tick-staggered probe run -------
        # Per-probe (press_wall_ms, raw_ms) pairs of the CURRENT run (a >60s press gap starts a
        # fresh run — phase is session-scoped and refit per run). With >= 6 closed probes the
        # sawtooth fit pins the absolute input-tick edge phase (epoch ms mod TICK_PERIOD_MS).
        # Confidence DECAYS with wall time since the fit (halved per 10 minutes) so a stale
        # phase self-retires instead of snapping fires onto a drifted grid.
        # --- [ORION_PROBE_PERSIST] probe-validated lead persistence + onboarding (default OFF) ---
        # Two independent flags, both env-driven so the launcher can arm them without a code
        # change, both overridable by tests via the constructor:
        #   ORION_PROBE_LEAD_PERSIST  - a completed probe run may persist the route-scoped cache,
        #                               and a probe-validated cache restores as a MEASURED PRIOR
        #                               SEED (factory-kind; never validated actuation authority).
        #   ORION_AUTO_PROBE_ONBOARD  - expose probe_recommended (telemetry only) so native can
        #                               auto-start the warmup probe run on a fresh install.
        self._probe_persist_enabled = (
            _env_flag("ORION_PROBE_LEAD_PERSIST", False)
            if probe_persist_enabled is None else bool(probe_persist_enabled))
        self._probe_onboard_enabled = (
            _env_flag("ORION_AUTO_PROBE_ONBOARD", False)
            if probe_onboard_enabled is None else bool(probe_onboard_enabled))
        # [ORION_PROBE_PRIOR_AUTHORITY] (default OFF) - a restored probe cache may REPLACE the
        # packaged factory tuple (boot/source/version/sd) so the native authority gate consumes
        # the probe-measured lead directly. MUST be armed together with the native settings flag
        # `probe_cache_prior_authority`: the replacement source string "probe_cache:self_measured"
        # fails AutomationEngine::factoryLatencyPriorMatchesRoute()'s venice-e2e-route-prior:
        # prefix unless that flag accepts it. Armed alone on the python side it would DISABLE
        # cold-start factory authority (the measured 2026-08-06 21:52/22:23 benched sessions:
        # every press refused waiting_for_latency_calibration until a manual probe run) - which
        # is exactly why the replacement is no longer the ORION_PROBE_LEAD_PERSIST default.
        self._probe_prior_authority = (
            _env_flag("ORION_PROBE_PRIOR_AUTHORITY", False)
            if probe_prior_authority is None else bool(probe_prior_authority))
        self._probe_prior_restored = False
        # [ORION_REOPEN_SOFT] see the constants block at the top of the file. Env-driven so the
        # launcher can arm it without a code change; constructor-overridable for tests.
        self._regime_reopen_soft = (
            _env_flag("ORION_LATENCY_REGIME_REOPEN_SOFT", False)
            if regime_reopen_soft is None else bool(regime_reopen_soft))
        # Retained pre-reopen authority: None, or a snapshot of the converged posterior taken at
        # the instant of a regime escape {mu, var, measured_ms, n, armed_wall_ms, labels_since}.
        # While active it is what value_ms/l_fixed_*/ready_for_native PUBLISH; the live learning
        # posterior (_mu/_var) is never touched by it and all ingestion math stays on the live
        # values.
        self._reopen_guard: Optional[dict] = None

        self._probe_pairs: list = []
        self._last_probe_press_ms: Optional[float] = None
        self._tick_phase_ms = -1.0
        self._tick_phase_rms_ms = -1.0
        self._tick_phase_sd_ms = -1.0
        self._tick_phase_conf_base = 0.0
        self._tick_phase_fit_wall_ms: Optional[float] = None
        self._last_wall_ms: Optional[float] = None

        # Template fitter for the inversion (loaded regardless of ORION_TIP_REG — inversion is
        # internal to the oracle). Missing template/scipy -> linear fallback only.
        self._reg = None
        try:
            from tip_registration_infer import RegistrationPredictor
            rp = RegistrationPredictor()
            self._reg = rp if rp.enabled else None
        except Exception:
            self._reg = None

        if bool(restore_cache):
            self._restore_cache()

    # ---- validated route-scoped warm start -----------------------------------------------------
    def _cache_entropy(self) -> bytes:
        return hashlib.sha256(
            b"Orion live timing cache v2 total\0"
            + self._route_scope.encode("utf-8", "strict")).digest()

    @_estimator_locked
    def _restore_cache(self) -> None:
        path = self._cache_path
        if (path is None or not self._route_scope or self._cache_max_age_s <= 0.0
                or os.name != "nt"):
            return
        try:
            blob = path.read_bytes()
            if len(blob) <= len(_CACHE_MAGIC) or len(blob) > 64 * 1024 \
                    or not blob.startswith(_CACHE_MAGIC):
                return
            clear = _dpapi_transform(blob[len(_CACHE_MAGIC):], self._cache_entropy(),
                                     protect=False)
            if not clear:
                return
            payload = json.loads(clear.decode("utf-8", "strict"))
            if not isinstance(payload, dict) or int(payload.get("version", 0)) != _CACHE_VERSION:
                return
            if str(payload.get("scope", "")) != _scope_digest(self._route_scope):
                return
            saved_at = float(payload.get("saved_at", 0.0))
            now = time.time()
            age_s = now - saved_at
            if (not np.isfinite(saved_at) or saved_at <= 0.0 or not np.isfinite(age_s)
                    or age_s < -300.0 or age_s > self._cache_max_age_s):
                return
            if str(payload.get("rtt_regime", "")) != "total":
                return
            # [ORION_PROBE_PERSIST] A probe-validated payload takes the weaker prior-seed path and
            # never the controlled-authority restore below (its controlled bits are False).
            if payload.get("probe_validated") is True:
                self._restore_probe_prior(payload)
                return
            if (payload.get("controlled_validation_ready") is not True
                    or payload.get("controlled_anchor") is not True):
                return

            n_labels = int(payload.get("n_labels", 0))
            mu = float(payload.get("mu_ms", float("nan")))
            var = float(payload.get("var_ms2", float("nan")))
            fixed = [float(v) for v in payload.get("fixed_accepted", [])]
            totals = [float(v) for v in payload.get("total_accepted", [])]
            if (n_labels < PERSISTED_MIN_LABELS or n_labels > 1_000_000
                    or not np.isfinite(mu) or mu < -100.0 or mu > 500.0
                    or not np.isfinite(var) or var <= 0.0
                    or var > PROVISIONAL_MAX_SD_MS ** 2
                    or len(fixed) < PERSISTED_MIN_LABELS or len(fixed) > 8
                    or len(totals) < PERSISTED_MIN_LABELS
                    or len(totals) > self._window
                    or not all(np.isfinite(v) and -100.0 <= v <= 500.0 for v in fixed)
                    or not all(np.isfinite(v) and self._lo <= v <= 500.0 for v in totals)
                    or abs(float(np.median(fixed)) - mu) > 20.0):
                return
            historical_total = mu + TICK_WAIT_EXPECT_MS
            if not np.isfinite(historical_total) or not (self._lo <= historical_total <= 500.0):
                return
            if abs(float(np.median(totals)) - historical_total) > 20.0:
                return

            self._mu = mu
            self._var = var
            self._fixed_accepted = deque(fixed, maxlen=8)
            self._rtt_used = deque([0.0] * min(len(fixed), 8), maxlen=8)
            self._labels = deque(totals, maxlen=self._window)
            self._n_labels = n_labels
            # Only a validated posterior is persistable, so nothing restored is corroboration.
            self._corroboration_labels = 0
            self._rtt_regime = "total"
            self._measured_ms = historical_total
            self._conf = float(np.clip(1.0 - np.sqrt(var) / 12.0, 0.0, 1.0))
            self._has_controlled_anchor = True
            self._first_controlled_seq = -1
            self._controlled_validation_ready = True
            self._restored_from_cache = True
            self._restored_route_attested = False
            self._last_label_method = "persisted_total"
            self._last_status = "restored_waiting_route_attestation"
            self._last_rejection = ""
            # WARNING so the restore survives the native relay's INFO throttle (a restore that
            # cannot be seen in orion_native.log cost a full diagnosis cycle on 2026-08-06).
            logger.warning("latency cache restored: n=%d sd=%.1fms (awaiting route attestation)",
                           n_labels, np.sqrt(var))
        except Exception:
            # Corrupt, stale, wrong-user, or wrong-route caches are equivalent to no evidence.
            return

    @_estimator_locked
    def _restore_probe_prior(self, payload: dict) -> None:
        """[ORION_PROBE_PERSIST] Restore a probe-validated posterior as a MEASURED ROUTE PRIOR.

        Deliberately weaker than the controlled-validation restore: the probe run measured this
        exact rig and route, but through the game-side D_spawn constant, so a cached probe lead
        re-enters as factory-KIND authority — a per-rig replacement for the packaged route model —
        never as validated actuation authority. Precedence consequences ("first 10 minutes on a
        new rig" contract):
          1. The user's own Shot Lead still REPLACES this outright in native
             (AutomationEngine::measuredLeadForActuationMs latches actuationLeadUserSet) — a
             probe cache can never overwrite an owner-tuned 300.
          2. It beats the packaged factory prior by overwriting the seed tuple (mean/sd/source),
             so install #2 session #2 cold-starts at ITS OWN measured lead, not 218.5.
          3. Fresh live labels refine it through the same bounded authority blend as any factory
             seed; a contradicted seed is simply out-blended — no revocation machinery needed.
          4. It can never mint validated authority, provisional_ready, or bypass the L1/L2
             protocol: the posterior restores with ZERO labels and no controlled anchor.
        Route safety matches the factory prior's own bar: the cache is scope-digest keyed and
        DPAPI-bound to this user, so a route change selects a different file and restores nothing.
        """
        if not self._probe_persist_enabled:
            return
        try:
            if (payload.get("controlled_validation_ready") is True
                    or payload.get("controlled_anchor") is True):
                return   # mixed provenance: a probe payload must not carry controlled claims
            n_labels = int(payload.get("n_labels", 0))
            probe_labels = int(payload.get("probe_labels", 0))
            mu = float(payload.get("mu_ms", float("nan")))
            var = float(payload.get("var_ms2", float("nan")))
            fixed = [float(v) for v in payload.get("fixed_accepted", [])]
            totals = [float(v) for v in payload.get("total_accepted", [])]
            if (n_labels < PERSISTED_MIN_LABELS or n_labels > 1_000_000
                    or probe_labels < _PROBE_VALIDATED_MIN_LABELS
                    or probe_labels > 1_000_000
                    or not np.isfinite(mu) or mu < -100.0 or mu > 500.0
                    or not np.isfinite(var) or var <= 0.0
                    or var > PROVISIONAL_MAX_SD_MS ** 2
                    or len(fixed) < PERSISTED_MIN_LABELS or len(fixed) > 8
                    or len(totals) < PERSISTED_MIN_LABELS
                    or len(totals) > self._window
                    or not all(np.isfinite(v) and -100.0 <= v <= 500.0 for v in fixed)
                    or not all(np.isfinite(v) and self._lo <= v <= 500.0 for v in totals)
                    or abs(float(np.median(fixed)) - mu) > 20.0):
                return
            historical_total = mu + TICK_WAIT_EXPECT_MS
            if (not np.isfinite(historical_total)
                    or not (self._lo <= historical_total <= 500.0)):
                return
            if abs(float(np.median(totals)) - historical_total) > 20.0:
                return
            # The seed sd floor of 6.0 keeps the tuple inside authority_kind's factory envelope
            # AND refuses to claim more precision than the provisional ceiling itself allows.
            seed_sd = float(np.clip(max(float(np.sqrt(var)), 6.0), 6.0, 100.0))
            # [ORION_PROBE_PRIOR_AUTHORITY] The factory-tuple REPLACEMENT is flag-gated OFF.
            #
            # MEASURED 2026-08-06 (sessions 21:49Z and 22:23Z, logs/orion_native.log): the
            # unconditional replacement below set factory_prior_source="probe_cache:self_measured",
            # which fails the native venice-e2e-route-prior: prefix check in
            # factoryLatencyPriorMatchesRoute(). Native then had NO factory authority at all, so
            # a restore that was supposed to warm the cold start instead BENCHED the bot: every
            # cold-session press was refused `waiting_for_latency_calibration` (8/8 and 7/7
            # presses in the two sessions) until the owner manually ran the warmup probes -
            # while sessions WITHOUT a cache file (same day, 13:55Z) cold-started fine on the
            # packaged prior. The replacement may run only when the native side is armed to
            # accept the replacement source (settings `probe_cache_prior_authority`).
            if self._probe_prior_authority:
                self._boot_prior_ms = float(historical_total)
                self._factory_prior_source = "probe_cache:self_measured"
                self._factory_prior_version = "cache-v%d" % _CACHE_VERSION
                self._factory_prior_sd_ms = seed_sd
            # Honest Bayesian warm start in BOTH modes: with zero labels the published authority
            # is bit-identical to the seed tuple in force (probe-measured when armed, otherwise
            # the untouched packaged prior), and the first fresh label meets a prior that already
            # encodes last session's probe run instead of the packaged average. The warm start
            # cannot mint authority: ready_for_native still demands CONVERGED_MIN_LABELS fresh
            # validation-capable labels, and authority_value_ms moves off the seed only with
            # accepted labels (allowance is 0 at n=0).
            self._prior_mu = float(mu)
            self._mu = float(mu)
            self._prior_var = float(seed_sd) ** 2
            self._var = float(seed_sd) ** 2
            self._probe_prior_restored = True
            self._last_label_method = "persisted_probe_prior"
            self._last_status = ("probe_prior_restored" if self._probe_prior_authority
                                 else "probe_prior_warmstart")
            self._last_rejection = ""
            # WARNING, not INFO: the native relay throttles and drops INFO (see the probe-close
            # note above). This line is the only positive evidence a restore ran; on 2026-08-06
            # its absence at INFO level made a completed restore indistinguishable from none.
            logger.warning(
                "probe-validated lead prior restored: total=%.1fms sd=%.1fms probe_labels=%d "
                "n=%d mode=%s (user-set lead still wins)",
                historical_total, seed_sd, probe_labels, n_labels,
                "authority_seed" if self._probe_prior_authority else "bayes_warmstart_only")
        except Exception:
            return

    @_estimator_locked
    def _persist_cache(self) -> None:
        path = self._cache_path
        if (path is None or not self._route_scope or os.name != "nt"
                or self._rtt_regime != "total"
                or self._n_labels < PERSISTED_MIN_LABELS
                or not np.isfinite(self._var) or self._var <= 0.0
                or self._var > PROVISIONAL_MAX_SD_MS ** 2
                or self._measured_ms is None
                or not np.isfinite(self._measured_ms)
                or not (self._lo <= self._measured_ms <= 500.0)
                or len(self._fixed_accepted) < PERSISTED_MIN_LABELS
                or len(self._labels) < PERSISTED_MIN_LABELS):
            return
        # Path 1 (unchanged contract): a controlled, causally VALIDATED posterior. The cache
        # restores as controlled+validated, so corroboration labels must not be what got it over
        # PERSISTED_MIN_LABELS.
        controlled_ok = bool(
            self._validation_capable_labels() >= PERSISTED_MIN_LABELS
            and self._has_controlled_anchor
            and self._controlled_validation_ready
            and self.provisional_ready)
        # Path 2 [ORION_PROBE_PERSIST], flag-gated default OFF: a COMPLETED probe run. Restores
        # only as a measured prior seed (see _restore_probe_prior), so the never-true controlled
        # gates above are deliberately not required here. Never let a probe payload displace a
        # restored (stronger-provenance) posterior.
        probe_ok = bool(
            not controlled_ok
            and self._probe_persist_enabled
            and not self._restored_from_cache
            and self._probe_labels >= _PROBE_VALIDATED_MIN_LABELS)
        if not controlled_ok and not probe_ok:
            return
        try:
            payload = {
                "version": _CACHE_VERSION,
                "scope": _scope_digest(self._route_scope),
                "saved_at": time.time(),
                "rtt_regime": "total",
                "controlled_anchor": bool(controlled_ok),
                "controlled_validation_ready": bool(controlled_ok),
                "n_labels": int(self._n_labels),
                "mu_ms": float(self._mu),
                "var_ms2": float(self._var),
                "fixed_accepted": [float(v) for v in self._fixed_accepted],
                "total_accepted": [float(v) for v in self._labels],
            }
            if probe_ok:
                payload["probe_validated"] = True
                payload["probe_labels"] = int(self._probe_labels)
            clear = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               allow_nan=False).encode("utf-8", "strict")
            protected = _dpapi_transform(clear, self._cache_entropy(), protect=True)
            if not protected:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
            with temporary.open("wb") as handle:
                handle.write(_CACHE_MAGIC + protected)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        except Exception as exc:
            logger.warning("latency cache save skipped: %s", exc)

    @_estimator_locked
    def set_restored_route_attested(self, attested: bool, reason: str = "") -> bool:
        """Gate a restored value on fresh video-route health without creating evidence.

        This changes no posterior/sample/calibration state. It is deliberately separate from
        release markers: a fresh neutral/loading frame can prove transport health but can never
        become a shot, a freeze observation, or an oracle label.
        """
        if not self._restored_from_cache:
            return False
        self._restored_route_attested = bool(attested)
        if self._restored_route_attested:
            self._last_status = "restored_route_attested_provisional"
            self._last_rejection = ""
        else:
            self._last_status = "restored_waiting_route_attestation"
            self._last_rejection = str(reason or "route_unattested")[:64]
        return self._restored_route_attested

    @_estimator_locked
    def revoke_restored_route(self, reason: str, *, invalidate_cache: bool = False) -> bool:
        """Immediately discard restored in-memory authority after a route contradiction.

        The monotonic native marker counters intentionally survive. A source/backend transition
        invalidates this process's warm value but normally leaves the exact-route cache available
        to a later clean process; contradictory causal evidence also deletes the cache.
        """
        if not self._restored_from_cache:
            return False
        # [ORION_REOPEN_SOFT] a route contradiction wipes the posterior below; a retained
        # reopen-guard snapshot of it must die with it (the structural n-regression check would
        # also catch this, but the explicit drop carries the reason into the log).
        self._drop_reopen_guard("restored_route_revoked")
        self._mu = float(self._prior_mu)
        self._var = float(self._prior_var)
        self._labels.clear()
        self._fixed_accepted.clear()
        self._rtt_used.clear()
        self._n_labels = 0
        self._corroboration_labels = 0
        self._measured_ms = None
        self._conf = 0.0
        self._pending_release_ms = None
        self._pending_rtt_ms = 0.0
        self._pending_rtt_known = False
        self._pending_release_calibration = False
        self._pending_validation_target_pct = None
        self._pending_validation_tolerance_pct = None
        self._rtt_regime = None
        self._total_mode_rtt_baseline_ms = None
        self._has_controlled_anchor = False
        self._first_controlled_seq = -1
        self._controlled_validation_ready = False
        self._controlled_recovery_count = 0
        self._reject_streak = 0
        self._reject_side = 0
        self._rise.clear()
        self._rise_peak = 0.0
        self._phase = PHASE_NONE
        self._restored_from_cache = False
        self._restored_route_attested = False
        self._last_label_method = ""
        self._last_status = "restored_revoked"
        self._last_rejection = str(reason or "route_transition")[:64]
        if invalidate_cache and self._cache_path is not None:
            try:
                if self._cache_path.is_file():
                    self._cache_path.unlink()
            except OSError as exc:
                logger.warning("latency cache invalidation failed: %s", exc)
        logger.warning("restored latency revoked: reason=%s cache_deleted=%d",
                       self._last_rejection, int(bool(invalidate_cache)))
        return True

    # ---- release-command markers ---------------------------------------------------------------
    def _log_release_observation(self, accepted: bool,
                                 lat_total: Optional[float] = None,
                                 sigma_meas: Optional[float] = None) -> None:
        """Emit one bounded, non-sensitive forensic line for a completed label opportunity.

        Release-marker logging happens before the visual consequence exists, so it can only show
        the *previous* posterior.  This close-time line proves whether ordinary gameplay is really
        refining the live estimate and, when it is not, names the strict rejection gate instead of
        leaving support to infer it from an unchanged sample count.
        """
        try:
            total_text = (f"{float(lat_total):.1f}"
                          if lat_total is not None and np.isfinite(float(lat_total)) else "-")
            sigma_text = (f"{float(sigma_meas):.1f}"
                          if sigma_meas is not None and np.isfinite(float(sigma_meas)) else "-")
            logger.info(
                "latency observation: seq=%d calibration=%d accepted=%d status=%s reject=%s "
                "total_ms=%s sigma_ms=%s fixed_ms=%.1f sd_ms=%.1f n=%d corr=%d freeze=%s "
                "controlled=%d validated=%d target_pct=%.1f tolerance_pct=%.1f "
                "f_stop=%.1f peak=%.1f reopen_guard=%d",
                int(self._pending_release_seq), int(bool(self._pending_release_calibration)),
                int(bool(accepted)), str(self._last_status or "unknown"),
                str(self._last_rejection or "-"), total_text, sigma_text,
                float(self._mu), float(np.sqrt(self._var)), int(self._n_labels),
                int(self._corroboration_labels),
                str(self._last_freeze_kind or "none"),
                int(bool(self._has_controlled_anchor)),
                int(bool(self._controlled_validation_ready)),
                (float(self._pending_validation_target_pct)
                 if self._pending_validation_target_pct is not None else -1.0),
                (float(self._pending_validation_tolerance_pct)
                 if self._pending_validation_tolerance_pct is not None else -1.0),
                float(self._last_freeze_f_stop),
                float(self._last_freeze_peak),
                # fixed_ms/sd_ms above stay the LIVE learning posterior; reopen_guard=1 says the
                # PUBLISHED authority is the retained pre-reopen tuple (see l_fixed_ms).
                int(bool(self._reopen_guard_publishable())))
        except Exception:
            pass

    @_estimator_locked
    def _expire_pending_release(self, wall_ms: float) -> None:
        """Retire a release that produced no trustworthy freeze inside the bounded window."""
        if self._pending_release_ms is None:
            return
        elapsed = float(wall_ms) - float(self._pending_release_ms)
        if not np.isfinite(elapsed) or elapsed <= self._post_release_ms:
            return
        self._last_status = "rejected_no_freeze"
        self._last_rejection = "no_freeze"
        self._last_freeze_kind = "none"
        self._reset_unvalidated_controlled_epoch("no_freeze")
        self._log_release_observation(False)
        self._pending_release_ms = None
        self._pending_release_calibration = False
        self._pending_validation_target_pct = None
        self._pending_validation_tolerance_pct = None
        self._frozen_captured = True

    @_estimator_locked
    def mark_release(self, wall_ms: float, seq: Optional[int] = None,
                     rtt_ms: Optional[float] = None,
                     calibration: bool = False,
                     validation_target_pct: Optional[float] = None,
                     validation_tolerance_pct: Optional[float] = None) -> int:
        """Record a release COMMAND on the fill wall clock. Returns the assigned release seq id.
        Called from the orchestrator's own release (self-measured) or a native release marker.
        Production passes rtt_ms=None because its verified public-court ping is not a measurement
        of the private PS5/Chiaki or HDMI command-to-visible-effect path.
        rtt_ms: the full network round-trip measured at release time (uplink press + downlink
        video are both in the oracle loop) — stripped from the label so the posterior tracks
        only the FIXED part of the latency. calibration=True identifies a controlled,
        frame-derived release. The first controlled marker establishes telemetry only. A later
        distinct marker may include validation_target_pct; authority remains disabled unless its
        observed stop agrees with that frame-derived target and its latency agrees with the first
        label. Controlled markers never relax the rise/non-cap gates."""
        try:
            wall_value = float(wall_ms)
            if not np.isfinite(wall_value) or wall_value <= 0.0:
                self._last_status = "rejected_invalid_marker"
                self._last_rejection = "invalid_marker"
                return 0

            native_seq = None if seq is None else int(seq)
            if native_seq is not None:
                if native_seq <= 0 or native_seq <= self._last_native_release_seq:
                    self._last_status = "rejected_replayed_marker"
                    self._last_rejection = "replayed_marker"
                    return 0

            # The marker may be backdated to the precise local submit timestamp, but it cannot
            # arrive after its entire causal observation window has already passed on the frame
            # clock. This also prevents a delayed menu/backlog command from opening authority.
            if (self._last_wall_ms is not None
                    and wall_value < float(self._last_wall_ms) - self._post_release_ms):
                self._last_status = "rejected_stale_marker"
                self._last_rejection = "stale_marker"
                return 0
            # A valid release may be the first event of a new shot after seconds with no meter
            # frames, so the previous frame timestamp is not a future-time reference. Compare
            # against this process's arrival wall clock instead (native and the sidecar share the
            # host wall clock). The post-release window is generous enough for submit/IPC jitter
            # while still rejecting timestamps that could manufacture a negative causal delay.
            arrival_wall_ms = time.time() * 1000.0
            if wall_value > arrival_wall_ms + self._post_release_ms:
                self._last_status = "rejected_future_marker"
                self._last_rejection = "future_marker"
                return 0

            # A new marker must never silently overwrite an unresolved opportunity. If that
            # opportunity was an unvalidated controlled L2, no frame arrived to run the usual
            # no-freeze expiry, so revoke its entire stale L1 epoch here. An already-planned L2
            # marker arriving in the same handoff is consumed and rejected; native must observe
            # the cold telemetry and emit a genuine no-target L1 on a later physical epoch.
            if self._pending_release_ms is not None:
                superseded_controlled_validation = bool(
                    self._pending_release_calibration
                    and self._pending_validation_target_pct is not None)
                self._last_status = "rejected_superseded"
                self._last_rejection = "no_freeze"
                self._last_freeze_kind = "none"
                if superseded_controlled_validation:
                    self._reset_unvalidated_controlled_epoch("superseded_no_freeze")
                self._log_release_observation(False)
                self._pending_release_ms = None
                self._pending_release_calibration = False
                self._pending_validation_target_pct = None
                self._pending_validation_tolerance_pct = None
                self._frozen_captured = True
                if superseded_controlled_validation and validation_target_pct is not None:
                    if native_seq is not None:
                        self._last_native_release_seq = native_seq
                    logger.info(
                        "latency validation marker rejected after epoch reset: seq=%d "
                        "reason=superseded_no_freeze",
                        int(native_seq or 0))
                    return 0

            validation_target = None
            validation_tolerance = None
            if validation_target_pct is not None:
                candidate = float(validation_target_pct)
                tolerance = (float(validation_tolerance_pct)
                             if validation_tolerance_pct is not None else float("nan"))
                if (not calibration or not np.isfinite(candidate)
                        or candidate < self._min_release_fill
                        or candidate > self._f_stop_max
                        or not np.isfinite(tolerance) or tolerance <= 0.0
                        or tolerance > 50.0
                        or not self._has_controlled_anchor):
                    self._last_status = "rejected_invalid_validation_marker"
                    self._last_rejection = "invalid_validation_marker"
                    return 0
                validation_target = candidate
                validation_tolerance = tolerance
            elif validation_tolerance_pct is not None:
                self._last_status = "rejected_invalid_validation_marker"
                self._last_rejection = "invalid_validation_marker"
                return 0

            self._release_seq += 1
            self._pending_release_ms = wall_value
            self._pending_release_seq = self._release_seq if seq is None else int(seq)
            rtt_value = float(rtt_ms) if rtt_ms is not None else 0.0
            self._pending_rtt_known = bool(
                rtt_ms is not None and np.isfinite(rtt_value) and rtt_value >= 0.0)
            self._pending_rtt_ms = max(0.0, rtt_value) if self._pending_rtt_known else 0.0
            self._pending_release_calibration = bool(calibration)
            self._pending_validation_target_pct = validation_target
            self._pending_validation_tolerance_pct = validation_tolerance
            self._last_release_ms = wall_value
            if native_seq is not None:
                self._last_native_release_seq = native_seq
            self._frozen_captured = False
            self._releases_since_label += 1
            self._last_status = "calibration_pending" if calibration else "release_pending"
            return self._pending_release_seq
        except Exception:
            return self._release_seq

    @_estimator_locked
    def mark_probe(self, wall_ms: float, seq: Optional[int] = None,
                   rtt_ms: Optional[float] = None,
                   spawn_offset_ms: Optional[float] = None) -> bool:
        """[ORION_PROBE] Record a warmup pump-fake PRESS. The meter-appear that follows closes
        it: the first rise samples are back-extrapolated through the registration template to
        the animation start t0 (= meter-appear), and
            label_total = (t0 - press) - spawn_offset_ms
        feeds the SAME L_fixed posterior as the passive oracle (sigma ~8ms: the spawn-offset
        uncertainty dominates). The probe measures the identical loop (input path + game +
        render + capture) PLUS the constant game-side spawn lead-in, which spawn_offset_ms
        strips (calibrated once offline). spawn 0/None = uncalibrated: only last_probe_raw_ms
        is recorded, for the offline D_spawn calibration itself. Warmup only (user decision)."""
        try:
            self._pending_probe_ms = float(wall_ms)
            rtt_value = float(rtt_ms) if rtt_ms is not None else 0.0
            self._pending_probe_rtt_known = bool(
                rtt_ms is not None and np.isfinite(rtt_value) and rtt_value >= 0.0)
            self._pending_probe_rtt = (
                max(0.0, rtt_value) if self._pending_probe_rtt_known else 0.0)
            self._pending_probe_spawn = float(spawn_offset_ms) if spawn_offset_ms else 0.0
            self._probe_rise = []
            self._probe_diag_n = 0   # fresh per-probe trace budget
            self._probe_diag_last_ms = None   # and a fresh decimation clock
            self._last_status = "probe_pending"
            # [Phase-2 A2(c)] a press far enough after the previous one starts a NEW probe run:
            # the tick phase is session-/run-scoped, so stale pairs must not blend into it.
            #
            # 60s -> 300s, measured 2026-08-05. The fit needs >=6 CLOSED probes, and closes are
            # scarce in any venue where the player must re-acquire the ball between shots: a run
            # typically lands 1-2. Accumulating across runs is therefore the ONLY way to reach 6,
            # and a 60s window meant the operator had to relaunch within 60s of the last PRESS or
            # silently lose every pair collected so far. 300s is still far shorter than the phase's
            # own validity (conf halves per 10 min), so nothing stale can reach the fit -- the
            # decay gate remains the real guard, and this only stops the operator being punished
            # for taking a breath between runs.
            if (self._last_probe_press_ms is not None
                    and abs(float(wall_ms) - self._last_probe_press_ms) > 300_000.0):
                self._probe_pairs = []
                self._probe_raw_run = []
            self._last_probe_press_ms = float(wall_ms)
            return True
        except Exception:
            return False

    @property
    @_estimator_locked
    def last_probe_raw_ms(self) -> float:
        """Most recent probe's raw press->appear (ms); -1 = none. With spawn_offset
        uncalibrated this is the calibration observable: D_spawn = median(raw) - L_oracle."""
        return self._last_probe_raw_ms

    @property
    @_estimator_locked
    def probe_labels(self) -> int:
        """[ORION_PROBE] Accepted labels that came from closed warmup probes."""
        return int(self._probe_labels)

    @property
    @_estimator_locked
    def probe_prior_restored(self) -> bool:
        """[ORION_PROBE_PERSIST] True when this epoch's factory seed came from a probe cache."""
        return bool(self._probe_prior_restored)

    @property
    @_estimator_locked
    def probe_recommended(self) -> bool:
        """[ORION_AUTO_PROBE_ONBOARD] Telemetry-only onboarding hint, flag-gated default OFF.

        True exactly when a warmup probe run is the missing instrument on this install: nothing
        persisted was restored (neither controlled authority nor a probe prior), and no label —
        passive or probe — exists yet. Native must AND this with its own conditions before
        auto-starting a run (user lead not set, engine idle, streaming, one run per session);
        this bit alone never actuates anything.
        """
        if not self._probe_onboard_enabled:
            return False
        if self._restored_from_cache or self._probe_prior_restored:
            return False
        if self._n_labels > 0 or self._probe_labels > 0:
            return False
        return True

    @property
    @_estimator_locked
    def release_seq(self) -> int:
        return self._release_seq

    @property
    @_estimator_locked
    def phase(self) -> str:
        return self._phase

    # ---- per-frame fill stream ------------------------------------------------------------------
    @_estimator_locked
    def update(self, wall_ms: float, fill_pct: float, present: bool, fed: bool = True) -> None:
        """Feed one detection frame. Detects the post-release freeze and closes the oracle."""
        try:
            wall_ms = float(wall_ms)
            fill = float(fill_pct)
            self._expire_pending_release(wall_ms)
            # [ORION_PROBE] Expire a probe whose meter never appeared -- BEFORE the not-present
            # early return below.
            #
            # This check used to live inside the present-frame branch, which made it unreachable
            # in the one situation it exists for. A probe that spawns no meter produces only
            # not-present frames, so update() returned at the guard below and the pending probe
            # sat there until the NEXT press overwrote it. Eight probes expired in total silence
            # on 2026-08-05 and the run looked identical to a successful one from the log.
            self._expire_pending_probe(wall_ms)
            self._log_probe_frame(wall_ms, fill, present, fed)
            # [Phase-2 A2(c)] latest wall clock seen — the tick-phase confidence decay reference.
            if np.isfinite(wall_ms):
                self._last_wall_ms = wall_ms
            # [ORION_REOPEN_SOFT] frame-driven expiry: a retained authority that outlives its
            # wall-clock bound without re-convergence is dropped here (fail closed) even when no
            # further labels ever arrive.
            self._expire_reopen_guard()
            if not present or not fed:
                # meter gone -> the shot ended; drop the rise buffer so the next shot fits clean.
                if self._phase != PHASE_NONE:
                    self._phase = PHASE_NONE
                # Only clear if it remains gone for >250ms.
                if self._last_present_ms is not None and (wall_ms - self._last_present_ms) > 250.0:
                    self._rise.clear()
                    self._rise_peak = 0.0
                    self._last_present_ms = None
                return

            # If we see a new present frame after a long gap, clear old history
            if self._last_present_ms is not None and (wall_ms - self._last_present_ms) > 250.0:
                self._rise.clear()
                self._rise_peak = 0.0

            self._last_present_ms = wall_ms
            self._rise.append((wall_ms, fill))
            self._rise_peak = max(self._rise_peak, fill)
            # [ORION_PROBE] collect the first rise samples after a probe press (low fill,
            # monotone) and close the probe once three are in; expire a probe whose meter
            # never appeared (aborted fake / detection miss).
            if self._pending_probe_ms is not None:
                dt = wall_ms - self._pending_probe_ms
                # Timeout is handled by _expire_pending_probe() at the top of update(), which runs
                # on every frame whether or not a meter is present. Do not re-check it here: a
                # second copy inside this branch is what hid the failure in the first place.
                # The lower bound is NOT cosmetic. _close_probe inverts each sample through the
                # template as g_inverse((fill - fmin)/A), and the global prior here is fmin=20.8.
                # Every sample at or below fmin clamps to u=0, so its back-extrapolated t0 collapses
                # to the sample's own timestamp. Three such samples are three frame times ~17ms
                # apart -> t0 spread ~13.6ms -> over the 6.0ms consistency cap -> dropped, silently.
                # A meter that has just appeared reads ~2/6/11%, i.e. ALWAYS below fmin, so the
                # first three samples were always the unusable ones and the probe could essentially
                # never close. Collect inside the invertible band instead: at ~0.185 pp/ms the
                # 21-45% window is ~130ms of rise, ~8 frames at 60fps -- ample for three samples.
                if dt > 0.0 and self._probe_fill_floor() < fill < 45.0 and (
                        not self._probe_rise or fill > self._probe_rise[-1][1] + 0.3):
                    self._probe_rise.append((wall_ms, fill))
                    if len(self._probe_rise) >= 3:
                        self._close_probe()
            self._classify(wall_ms, fill)
        except Exception:
            pass

    def _probe_fill_floor(self) -> float:
        """Lowest fill whose template inversion carries information, cached per estimator.

        Returns fmin + a small margin. Below fmin the inversion argument clamps to 0 and every
        sample maps to u=0 (see the collector). The margin keeps the first admitted sample off the
        exact clamp boundary, where g_inverse is flattest and the t0 estimate is least stable.
        Falls back to 0.0 with no template, preserving the previous unbounded behaviour rather
        than blocking collection outright.
        """
        cached = getattr(self, '_probe_fill_floor_v', None)
        if cached is not None:
            return float(cached)
        floor = 0.0
        try:
            if self._reg is not None:
                _T, _A, fmin = self._reg._prior_for(0.0)
                floor = max(0.0, float(fmin)) + 2.0
        except Exception:
            floor = 0.0
        self._probe_fill_floor_v = floor
        return floor

    @_estimator_locked
    def _close_probe(self) -> None:
        """Back-extrapolate the collected rise samples through the template (T, A, fmin held
        at the GLOBAL prior — 3 points cannot constrain 4 params) to the animation start t0,
        then ingest the spawn-stripped label into the L_fixed posterior."""
        press = self._pending_probe_ms
        rise = self._probe_rise
        self._pending_probe_ms = None
        self._probe_rise = []
        if press is None or self._reg is None:
            self._last_status = "probe_unavailable"
            self._last_rejection = "probe_unavailable"
            # Every exit below clears the pending probe ABOVE, so a silent one produces neither a
            # close nor an expiry -- indistinguishable in the log from "update() never ran". That
            # cost a full live batch on 2026-08-05: 16 markers, 0 closes, 0 expiries, and the frame
            # trace simply stopped at ~350ms with no reason given. Say why, every time.
            logger.warning("probe DROPPED: reason=probe_unavailable press=%s reg=%d",
                           "none" if press is None else "%.0f" % press,
                           int(self._reg is not None))
            return
        try:
            T, A, fmin = self._reg._prior_for(0.0)   # global prior (short elapsed -> global)
            A = max(float(A), 5.0)
            T = max(float(T), 40.0)
            t0s = []
            for (t, f) in rise[:3]:
                u = self._reg.g_inverse(max(0.0, float(f) - float(fmin)) / A)
                if u is None:
                    self._last_status = "probe_unfittable"
                    self._last_rejection = "probe_unfittable"
                    logger.warning(
                        "probe DROPPED: reason=probe_unfittable fill=%.2f fmin=%.2f A=%.2f "
                        "arg=%.4f (g_inverse out of domain) rise=%s",
                        float(f), float(fmin), float(A),
                        max(0.0, float(f) - float(fmin)) / A,
                        ",".join("%.0f@%.2f" % (t - press, fv) for (t, fv) in rise[:3]))
                    return
                t0s.append(float(t) - T * float(u))
            arr = np.asarray(t0s, float)
            if float(arr.std()) > 6.0:
                self._last_status = "probe_inconsistent"
                self._last_rejection = "probe_inconsistent"
                logger.warning(
                    "probe DROPPED: reason=probe_inconsistent t0_sd=%.1fms (cap 6.0) "
                    "t0s=%s rise=%s T=%.1f A=%.1f fmin=%.2f",
                    float(arr.std()),
                    ",".join("%.1f" % (v - press) for v in t0s),
                    ",".join("%.0f@%.2f" % (t - press, fv) for (t, fv) in rise[:3]),
                    float(T), float(A), float(fmin))
                return   # inconsistent back-extrapolation -> junk probe, drop it
            raw = float(arr.mean()) - press
            self._last_probe_raw_ms = raw
            # Greppable calibration line: tools/timing/calibrate_probe_spawn.py joins these
            # with the passive-oracle value from the same session to derive D_spawn.
            # WARNING, not INFO: the native relay throttles and drops INFO, so on 2026-08-05 a
            # probe run that produced no expiries was indistinguishable from one that produced
            # nothing at all -- the result existed only in memory and was lost on restart. A
            # user-triggered diagnostic must survive the transport that carries it.
            logger.warning("probe closed: raw=%.1fms spawn=%.1f rtt=%.1f",
                        raw, self._pending_probe_spawn, self._pending_probe_rtt)
            # [Phase-2 A2(c)] accumulate the (press_phase, raw) pair for the sawtooth tick-phase
            # fit. Raw needs NO spawn calibration (D_spawn is a constant — it lands in C, not in
            # the phase), so the phase converges even on an uncalibrated rig.
            self._record_probe_pair(press, raw)
            self._probe_raw_run.append(raw)
            if len(self._probe_raw_run) > 16:
                self._probe_raw_run = self._probe_raw_run[-16:]
            self._log_probe_spawn_estimate()
            if self._pending_probe_spawn <= 0.0:
                self._last_status = "probe_uncalibrated"
                self._last_rejection = "probe_uncalibrated"
                return   # uncalibrated: raw exposed for the offline D_spawn calibration only
            lat_total = raw - self._pending_probe_spawn
            if self._lo <= lat_total <= self._hi:
                self._last_label_method = "probe"
                if self._ingest_label(
                        lat_total, 8.0,
                        rtt_known=self._pending_probe_rtt_known,
                        rtt_ms=self._pending_probe_rtt):
                    self._probe_labels += 1
                    # [ORION_PROBE_PERSIST] _ingest_label persisted BEFORE this counter moved, so
                    # the run-completion write must be re-attempted with the count current. The
                    # call is idempotent and fully gated (flag OFF -> no-op).
                    self._persist_cache()
            else:
                self._last_status = "rejected_out_of_range"
                self._last_rejection = "out_of_range"
        except Exception as exc:
            # Was a bare `pass`. The pending probe is already cleared above, so an exception here
            # produced NO close, NO expiry and NO error -- the single most expensive failure shape
            # in this instrument. scipy is absent on the shipped venv, so anything in the template
            # inversion that reaches for it lands exactly here.
            self._last_status = "probe_exception"
            self._last_rejection = "probe_exception"
            logger.warning("probe DROPPED: reason=probe_exception %s: %s",
                           type(exc).__name__, str(exc)[:160])

    @property
    @_estimator_locked
    def probe_spawn_estimate_ms(self) -> float:
        """[ORION_PROBE] Online estimate of the game-side press->meter-appear constant D_spawn.

        ``raw = L_route + D_spawn`` and the passive oracle independently measures ``L_route``, so
        ``D_spawn = median(raw) - measured_total``. That constant is what today blocks the warmup
        probe run from producing labels at all (``probe_spawn_offset_ms`` ships as 0, so
        ``_close_probe`` drops every probe as ``probe_uncalibrated``), and deriving it currently
        means grinding a session log offline with tools/timing/calibrate_probe_spawn.py.

        STRICTLY TELEMETRY. It is never fed back into the posterior: doing so would compute a
        probe label from the posterior and then fold that label back in, manufacturing agreement
        and shrinking the variance on no new information. For the same reason it fails closed once
        any probe-derived label exists -- the reference would no longer be independent.

        -1.0 = not derivable yet.
        """
        try:
            if (self._probe_labels > 0 or len(self._probe_raw_run) < 3
                    or self._n_labels < 2 or self._measured_ms is None
                    or not np.isfinite(self._measured_ms) or self._measured_ms <= 0.0):
                return -1.0
            spawn = float(np.median(np.asarray(self._probe_raw_run, float))
                          - float(self._measured_ms))
            if not np.isfinite(spawn) or spawn <= 0.0 or spawn > 1000.0:
                return -1.0
            return spawn
        except (TypeError, ValueError, OverflowError):
            return -1.0

    def _log_probe_spawn_estimate(self) -> None:
        """Emit the greppable D_spawn readout so one live session yields the shipping constant."""
        try:
            self._log_probe_raw_spread()
            spawn = self.probe_spawn_estimate_ms
            if spawn <= 0.0:
                return
            logger.info("probe spawn estimate: d_spawn=%.1fms probes=%d n_labels=%d "
                        "measured_total_ms=%.1f (telemetry only)",
                        spawn, len(self._probe_raw_run), int(self._n_labels),
                        float(self._measured_ms))
        except Exception:
            pass

    def _log_probe_frame(self, wall_ms: float, fill: float, present: bool, fed: bool) -> None:
        """[ORION_PROBE] Bounded per-frame trace while a probe is pending.

        WHY THIS EXISTS. On 2026-08-05 a run produced 32 markers all ok=True, visibly correct
        shots (the operator watched them), and then NOTHING: no closes and no expiries. Both of
        those require update() to run, so their joint absence says update() never reached this
        estimator -- but nothing anywhere distinguished "update not called" from "called and every
        frame rejected". Five theories were proposed and none could be tested from the log.

        These lines separate the possibilities in one run:
          * no lines at all           -> update() is not being called (estimator not wired to the
                                         detector feed; check _latency_estimator_for_frame)
          * lines stop well before the 2000ms expiry -> the feed to THIS estimator was cut mid-probe
                                         (gate disarm / estimator swap). Diagnostic on its own,
                                         because a cut feed also silences _expire_pending_probe():
                                         no close AND no expiry, which reads exactly like "never
                                         wired" from the log.
          * present=0 or fed=0        -> the reader is not delivering RAW-ACCEPTED samples, so the
                                         collector below can never run
          * present=1 fed=1 fill>=45  -> frames arrive but sit above the collector's fill ceiling

        SAMPLING. The meter does not spawn until ~350ms after the press (READER ACQUIRE trails a
        probe arm by 383-403ms measured), and the probe expires at 2000ms. A flat 6-frame budget
        covered only the first ~90ms -- entirely inside the dead window before the meter can
        possibly exist -- so it reported present=0 for every probe and could not distinguish a cut
        feed from a meterless press. Dense for the first few frames (press-adjacent detail), then
        decimated to ~kGapMs so the budget reaches past the expiry.
        """
        kMax = 24
        kDenseFrames = 3
        kGapMs = 150.0
        try:
            if self._pending_probe_ms is None:
                return
            n = int(getattr(self, '_probe_diag_n', 0) or 0)
            if n >= kMax:
                return
            last = getattr(self, '_probe_diag_last_ms', None)
            if (n >= kDenseFrames and last is not None
                    and (wall_ms - float(last)) < kGapMs):
                return
            self._probe_diag_n = n + 1
            self._probe_diag_last_ms = wall_ms
            logger.warning("probe frame: dt=%.0fms present=%d fed=%d fill=%.1f rise_n=%d",
                           wall_ms - self._pending_probe_ms, int(bool(present)), int(bool(fed)),
                           float(fill), len(self._probe_rise))
        except Exception:
            pass

    def _expire_pending_probe(self, wall_ms: float) -> None:
        """[ORION_PROBE] Drop a probe whose meter never showed, and SAY SO.

        Called unconditionally from update(), so it fires on not-present frames -- the only frames
        a meterless probe ever generates. Silence here is worse than useless: a run of eight
        expired probes is indistinguishable in the log from eight successful ones, so the operator
        reads "run complete (8 presses)" and believes a measurement exists.

        The usual cause is a press that produced no shot meter at all: no ball in hand, a press too
        short to commit to a shot, or the console swallowing input behind a modal.
        """
        try:
            press = self._pending_probe_ms
            if press is None or not np.isfinite(wall_ms):
                return
            dt = wall_ms - press
            if dt <= 2000.0:
                return
            samples = len(self._probe_rise)
            self._pending_probe_ms = None
            self._probe_rise = []
            self._last_status = "probe_timeout"
            self._last_rejection = "probe_timeout"
            logger.warning(
                "probe EXPIRED: no meter within %.0fms of the press (rise_samples=%d, needed 3) "
                "— press produced no shot meter; check ball in hand / press length",
                dt, samples)
        except Exception:
            pass

    def _log_probe_raw_spread(self) -> None:
        """[ORION_PROBE] Emit the SPREAD of the raw press->appear run, unconditionally.

        WHY THIS IS SEPARATE FROM THE D_spawn READOUT. ``raw = L_route + D_spawn`` and D_spawn is
        a game-side CONSTANT, so ``sd(raw) == sd(L_route)`` -- the run measures actuation jitter
        exactly, and the uncalibrated offset shifts only the mean. But the D_spawn readout above
        returns early whenever the offset is not yet derivable (it needs >=2 passive-oracle labels
        from delivered shots), so on a cold warmup session -- the exact case the probe run was
        built for -- nothing was logged at all and the jitter went on the floor.

        This matters because the engine's shipped lead_sd is an n=8 factory prior that dominates
        its believed variance and has never been checked against a machine. Until this line
        existed there was no way to check it without an offline grind.

        Robust SD (1.4826 * MAD), not the sample SD: a single probe landing on a dropped frame is
        a fat outlier, and with n<=8 one such sample doubles a naive SD.
        """
        try:
            raw = np.asarray(self._probe_raw_run, dtype=float)
            raw = raw[np.isfinite(raw)]
            if raw.size < 3:
                return
            median = float(np.median(raw))
            rsd = 1.4826 * float(np.median(np.abs(raw - median)))
            # WARNING: this line IS the measurement the probe run exists to produce. INFO does not
            # survive the native relay.
            logger.warning("probe raw spread: n=%d median=%.1fms robust_sd=%.2fms "
                           "min=%.1f max=%.1f (jitter; offset-free)",
                        int(raw.size), median, rsd, float(raw.min()), float(raw.max()))
        except Exception:
            pass

    # ---- [Phase-2 A2(c)] console input-tick phase (probe-run sawtooth fit) -----------------------
    @_estimator_locked
    def _record_probe_pair(self, press_ms: float, raw_ms: float) -> None:
        """Record one closed probe's (press, raw) and refit the tick phase once >= 6 are in."""
        try:
            self._probe_pairs.append((float(press_ms), float(raw_ms)))
            if len(self._probe_pairs) > 16:
                self._probe_pairs = self._probe_pairs[-16:]
            if len(self._probe_pairs) < 6:
                return
            phi, rms, c = fit_tick_phase(self._probe_pairs)
            if phi is None or rms < 0.0:
                return
            n = len(self._probe_pairs)
            self._tick_phase_ms = float(phi)
            self._tick_phase_rms_ms = float(rms)
            # Phase-estimate sd from the fit residual; floored at the 64-point grid quantum so a
            # perfectly clean synthetic fit still reports an honest nonzero uncertainty.
            self._tick_phase_sd_ms = max(0.25, float(rms) / float(np.sqrt(max(1, n))))
            self._tick_phase_conf_base = float(np.clip(1.0 - rms / 6.0, 0.0, 1.0))
            self._tick_phase_fit_wall_ms = float(press_ms)
            # WARNING for the same reason as "probe closed": this is the half of a probe run that
            # works WITHOUT the D_spawn constant, so it is the primary product of the run and must
            # not be dropped by the INFO relay throttle.
            logger.warning("tick phase fit: phi=%.2fms rms=%.2f sd=%.2f conf=%.2f n=%d c=%.1f",
                        self._tick_phase_ms, rms, self._tick_phase_sd_ms,
                        self._tick_phase_conf_base, n, c)
        except Exception:
            pass

    @property
    @_estimator_locked
    def tick_phase_ms(self) -> float:
        """Input-tick EDGE phase (press-epoch ms modulo TICK_PERIOD_MS); -1 = no fit yet."""
        return float(self._tick_phase_ms)

    @property
    @_estimator_locked
    def tick_phase_sd_ms(self) -> float:
        """Phase-estimate sd (ms) from the sawtooth fit RMS; -1 = no fit yet."""
        return float(self._tick_phase_sd_ms)

    @property
    @_estimator_locked
    def tick_phase_conf(self) -> float:
        """Fit confidence clamp(1 - rms/6, 0, 1), DECAYED by wall time since the probe run
        (halved per 10 minutes): the phase is session-scoped and self-retires when stale —
        the engine's earlier-only snap disengages below conf 0.5."""
        try:
            if self._tick_phase_conf_base <= 0.0 or self._tick_phase_fit_wall_ms is None:
                return 0.0
            ref = self._last_wall_ms if self._last_wall_ms is not None else self._tick_phase_fit_wall_ms
            age_min = max(0.0, (float(ref) - self._tick_phase_fit_wall_ms) / 60_000.0)
            return float(self._tick_phase_conf_base * (0.5 ** (age_min / 10.0)))
        except Exception:
            return 0.0

    @_estimator_locked
    def _classify(self, wall_ms: float, fill: float) -> None:
        if len(self._rise) < 2:
            self._phase = PHASE_RISE
            return
        recent = list(self._rise)[-self._freeze_win:]
        fills = np.asarray([f for _, f in recent], float)
        spread = float(fills.max() - fills.min())
        rising = (self._rise[-1][1] - self._rise[-2][1]) > 0.15

        if spread <= self._freeze_eps and len(recent) >= min(self._freeze_win, 3):
            # flat -> a freeze. Is it attributable to a recent release?
            near_release = (
                self._last_release_ms is not None
                and 0.0 <= (wall_ms - self._last_release_ms) <= self._post_release_ms
            )
            if near_release and fill >= self._min_release_fill:
                self._phase = PHASE_FROZEN
                self._try_close_oracle(float(np.median(fills)))
            else:
                self._phase = PHASE_PLATEAU
        elif rising:
            self._phase = PHASE_RISE
        # else: ambiguous small change -> keep prior phase

    def _validation_residual_tolerance_pct(self, slope_lin: float) -> float:
        """Return the stricter marker/anchor-derived stop tolerance for the pending L2."""
        try:
            marker_tolerance_pct = float(self._pending_validation_tolerance_pct)
            anchor_sd_ms = float(np.sqrt(self._var))
            slope_sd_tolerance_pct = 3.0 * float(np.sqrt(
                0.4 ** 2 + (float(slope_lin) * anchor_sd_ms) ** 2))
            return float(min(marker_tolerance_pct, slope_sd_tolerance_pct))
        except (TypeError, ValueError, OverflowError):
            return float("nan")

    @_estimator_locked
    def _reset_unvalidated_controlled_epoch(self, reason: str) -> bool:
        """Atomically revoke a stale L1 after an unsafe controlled-L2 terminal.

        Near-cap, deflate, and no-freeze terminals contain no invertible latency evidence. Keeping
        their L1 would make every later physical epoch repeat the same unsafe L2 plan. Clearing only
        the controlled bit is also unsafe: repeated replacement L1 labels could eventually satisfy
        the ordinary N/SD convergence gate without a validation. Reset the complete unvalidated
        posterior instead, while deliberately retaining monotonic marker sequence counters.
        """
        if (not self._pending_release_calibration
                or self._pending_validation_target_pct is None
                or not self._has_controlled_anchor
                or self._controlled_validation_ready):
            return False

        seq = int(self._pending_release_seq)
        # [ORION_REOPEN_SOFT] this path resets the complete unvalidated posterior; any retained
        # reopen-guard snapshot of it must not survive the reset.
        self._drop_reopen_guard("controlled_epoch_reset")
        self._mu = float(self._prior_mu)
        self._var = float(self._prior_var)
        self._labels.clear()
        self._fixed_accepted.clear()
        self._rtt_used.clear()
        self._n_labels = 0
        self._corroboration_labels = 0
        self._measured_ms = None
        self._conf = 0.0
        self._pending_rtt_ms = 0.0
        self._pending_rtt_known = False
        self._rtt_regime = None
        self._total_mode_rtt_baseline_ms = None
        self._has_controlled_anchor = False
        self._first_controlled_seq = -1
        self._controlled_validation_ready = False
        self._controlled_recovery_count = 0
        self._reject_streak = 0
        self._reject_side = 0
        self._releases_since_label = 0
        self._rise.clear()
        self._rise_peak = 0.0
        self._phase = PHASE_NONE
        self._last_present_ms = None
        self._last_release_ms = None
        self._restored_from_cache = False
        self._restored_route_attested = False
        # A route cache predating this failed validation must not silently restore the revoked
        # anchor on restart. The target is one explicit route-scoped file; directories are never
        # removed and a deletion failure remains visible in diagnostics.
        if self._cache_path is not None:
            try:
                if self._cache_path.is_file():
                    self._cache_path.unlink()
            except OSError as exc:
                logger.warning("latency cache invalidation failed after epoch reset: %s", exc)
        logger.info(
            "latency calibration epoch reset: seq=%d reason=%s n=0 controlled=0 ready=0",
            seq, str(reason or "unsafe_validation_terminal")[:48])
        return True

    @_estimator_locked
    def _refine_controlled_anchor_from_miss(
            self, *, f_stop: float, validation_target: float,
            slope_lin: float, sigma_meas: float,
            observed_total_ms: float) -> bool:
        """Apply one non-authoritative, bounded correction from a clean controlled L2 miss.

        Only the ordinary command-to-visible-stop inversion of a monotone, non-cap rise is eligible.
        The correction moves the existing controlled anchor only: N/readiness histories are
        untouched and uncertainty can only stay level or widen. A later, distinct marker must still
        land inside its planned stop before provisional authority exists.
        """
        if (not self._pending_release_calibration
                or self._pending_validation_target_pct is None
                or not self._has_controlled_anchor
                or self._controlled_validation_ready
                # "L1 is the only validated-capable evidence" is the real precondition. Plain
                # `_n_labels != 1` would silently retire this one-shot recovery as soon as a
                # corroboration label landed, which is a side effect of the demotion, not intent.
                or self._validation_capable_labels() != 1
                or self._pending_release_seq == self._first_controlled_seq
                or not np.isfinite(f_stop) or f_stop > self._f_stop_max):
            return False

        if self._controlled_recovery_count >= _CONTROLLED_RECOVERY_MAX_COUNT:
            self._last_status = "rejected_validation_recovery_exhausted"
            self._last_rejection = "validation_stop_residual"
            return False

        residual_pct = float(f_stop) - float(validation_target)
        residual_tolerance_pct = self._validation_residual_tolerance_pct(slope_lin)
        if (not np.isfinite(residual_pct)
                or not np.isfinite(residual_tolerance_pct)
                or residual_tolerance_pct <= 0.0
                or abs(residual_pct) <= residual_tolerance_pct + 1e-6
                or abs(residual_pct) > _CONTROLLED_RECOVERY_MAX_RESIDUAL_PCT):
            self._last_status = "rejected_validation_recovery_gross_residual"
            self._last_rejection = "validation_stop_residual"
            return False

        if (not np.isfinite(slope_lin) or slope_lin <= 0.08
                or not np.isfinite(sigma_meas)
                or sigma_meas <= 0.0 or sigma_meas > PROVISIONAL_MAX_SD_MS):
            self._last_status = "rejected_validation_recovery_noisy"
            self._last_rejection = "validation_stop_residual"
            return False

        # Keep the posterior in its original measurement domain.  In decomposed mode an unknown
        # per-release RTT would silently mix total and RTT-stripped evidence, so it remains an
        # unconditional rejection just as it is on the accepted-label path.
        if self._rtt_regime == "decomposed":
            if not self._pending_rtt_known:
                self._last_status = "rejected_validation_recovery_rtt_unavailable"
                self._last_rejection = "rtt_unavailable"
                return False
            rtt_ms = float(self._pending_rtt_ms)
            expected_total_ms = self._mu + rtt_ms + TICK_WAIT_EXPECT_MS
        elif self._rtt_regime == "total":
            rtt_ms = 0.0
            expected_total_ms = float(self._measured_ms)
        else:
            self._last_status = "rejected_validation_recovery_unscoped"
            self._last_rejection = "validation_stop_residual"
            return False

        candidate_total_ms = float(observed_total_ms)
        innovation_ms = candidate_total_ms - expected_total_ms
        # The time-domain innovation and fill-domain miss must tell the same causal story. A
        # disagreement is usually a mixed shot/dropout, not route latency drift.
        predicted_residual_pct = float(slope_lin) * innovation_ms
        consistency_tolerance_pct = max(residual_tolerance_pct, 1.5)
        if (innovation_ms * residual_pct <= 0.0
                or abs(predicted_residual_pct - residual_pct)
                > consistency_tolerance_pct):
            self._last_status = "rejected_validation_recovery_inconsistent"
            self._last_rejection = "validation_stop_residual"
            return False

        correction_ms = candidate_total_ms - expected_total_ms
        outer_hi_ms = min(self._post_release_ms, 500.0)
        posterior_var = min(self._prior_var, self._var + _ONLINE_PROCESS_VAR_MS2)
        radius_ms = max(
            4.0 * float(np.sqrt(posterior_var + float(sigma_meas) ** 2)), 25.0)
        if (not np.isfinite(candidate_total_ms)
                or not np.isfinite(correction_ms)
                or candidate_total_ms < self._lo or candidate_total_ms > outer_hi_ms
                or abs(correction_ms) > _CONTROLLED_RECOVERY_MAX_CORRECTION_MS
                or abs(correction_ms) > radius_ms):
            self._last_status = "rejected_validation_recovery_out_of_range"
            self._last_rejection = "validation_stop_residual"
            return False

        candidate_fixed_ms = candidate_total_ms - rtt_ms - TICK_WAIT_EXPECT_MS
        if not np.isfinite(candidate_fixed_ms):
            self._last_status = "rejected_validation_recovery_out_of_range"
            self._last_rejection = "validation_stop_residual"
            return False

        before_total_ms = expected_total_ms
        self._mu = float(candidate_fixed_ms)
        # This was not an accepted label.  It must not manufacture confidence; retain the old
        # uncertainty or widen it to the miss's measured noise, whichever is more conservative.
        self._var = max(float(self._var), float(sigma_meas) ** 2)
        self._controlled_recovery_count += 1
        self._recompute()
        self._last_status = "calibration_anchor_refined_retry_required"
        self._last_rejection = "validation_stop_residual"
        logger.info(
            "latency calibration anchor refined: seq=%d old_total_ms=%.1f new_total_ms=%.1f "
            "correction_ms=%.1f residual_pct=%.1f n=%d ready=0",
            int(self._pending_release_seq), before_total_ms, candidate_total_ms,
            correction_ms, residual_pct, int(self._n_labels))
        return True

    # ---- oracle close: disambiguate, invert, decompose, update the posterior --------------------
    @_estimator_locked
    def _try_close_oracle(self, f_stop: float) -> None:
        """Invert the observed rise fill(t) to t* where f(t*)==F_stop; latency = t* - release ts."""
        if self._frozen_captured or self._pending_release_ms is None:
            return
        calibration = bool(self._pending_release_calibration)
        if calibration:
            # Preserve the route even when a later strict trajectory/range gate rejects it.
            self._last_label_method = "calibration"
        rise = list(self._rise)
        # rise samples strictly BELOW the freeze value carry the timing info. The censor margin
        # is 1.0pp (~3 sigma of measured frozen-plateau jitter): the old 0.05pp margin let noisy
        # plateau samples leak into the tail as zero-slope late points — the measured +35ms
        # oracle bias (Part-0 M5). One line, ~30ms of systematic error.
        below = [(t, f) for (t, f) in rise if f < f_stop - 1.0]
        if len(below) < 2:
            return
        t_arr = np.asarray([t for t, _ in below], float)
        f_arr = np.asarray([f for _, f in below], float)

        # --- v2 disambiguation: is this freeze approached from BELOW (rise-freeze, the oracle
        #     signal) or from ABOVE after a cap+deflate (a LATE release — settle value encodes
        #     overtime, not view latency)? ---
        tail_n = min(len(below), 3)
        tt3, ff3 = t_arr[-tail_n:], f_arr[-tail_n:]
        if tail_n >= 2 and (tt3[-1] - tt3[0]) > 1e-6:
            approach_slope = float((ff3[-1] - ff3[0]) / (tt3[-1] - tt3[0]))   # pct/ms
        else:
            approach_slope = 0.0
        peak = float(self._rise_peak)
        self._last_freeze_f_stop = float(f_stop)
        self._last_freeze_peak = peak

        deflate = peak >= 97.0 and (approach_slope < 0.0 or f_stop < peak - 1.5)
        # Slope gate 0.08 pct/ms (M5 recommended gate: pre-freeze tail slope >= 8pp/100ms —
        # below it the extrapolation to F_stop is ill-conditioned and the label is junk).
        # 2026-08-04: accept a SUB-CAP overshoot instead of demanding the meter freeze exactly at
        # its peak. The old `peak <= f_stop + 1.5` discarded every trace where the meter ran past
        # F_stop and settled back -- i.e. the ordinary post-release animation. The survivors were
        # only those whose overshoot happened to be under 1.5pp or was frame-undersampled, so the
        # estimator was labelling precisely the shots where its own crossing-vs-lock bias is
        # smallest and invisible. 8.0pp is meter-render geometry (observed overshoot ~6.5pp), not
        # a machine constant. The deflate gate above is untouched: a cap-and-recede (peak >= 97)
        # is still rejected, because there the settle encodes overtime rather than view latency.
        rise_ok = (peak < 97.0) and (peak - f_stop <= 8.0) \
            and (approach_slope > 0.08) and (f_stop <= self._f_stop_max)
        slope_lin = max(0.08, approach_slope)
        # Label/recovery noise from local fill noise through the slope + console-tick scatter +
        # timestamp jitter. Recovery still requires the ordinary non-cap rise gate below.
        sigma_meas = float(np.clip(
            np.sqrt((0.4 / slope_lin) ** 2 + TICK_WAIT_SD_MS ** 2 + 2.0 ** 2), 4.0, 20.0))

        if deflate:
            self._last_freeze_kind = "deflate"
            self._last_status = "rejected_deflate"
            self._last_rejection = "deflate"
            self._reset_unvalidated_controlled_epoch("deflate")
            self._log_release_observation(False)
            self._frozen_captured = True
            self._pending_release_ms = None
            self._pending_release_calibration = False
            self._pending_validation_target_pct = None
            self._pending_validation_tolerance_pct = None
            return
        if not rise_ok:
            self._last_freeze_kind = "ambiguous"
            if f_stop > self._f_stop_max:
                self._last_status = "rejected_near_cap"
                self._last_rejection = "near_cap"
                self._reset_unvalidated_controlled_epoch("near_cap")
            else:
                self._last_status = "rejected_ambiguous"
                self._last_rejection = "ambiguous"
            self._log_release_observation(False)
            self._frozen_captured = True
            self._pending_release_ms = None
            self._pending_release_calibration = False
            self._pending_validation_target_pct = None
            self._pending_validation_tolerance_pct = None
            return
        self._last_freeze_kind = "rise"

        # --- inversion: margin-fixed LINEAR tail fit (the measured-best label path; Part-0 M5:
        #     bias ~+1ms, sigma ~8ms under the gates above — the template inversion measured
        #     strictly worse on the same close events and is NOT used for labels). ---
        t_star: Optional[float] = None
        t_cross: Optional[float] = None
        tail = min(len(below), max(3, self._freeze_win))
        tt, ff = t_arr[-tail:], f_arr[-tail:]
        if ff[-1] > ff[0] + 0.2:
            try:
                a, b = np.polyfit(ff, tt, 1)
                t_cross = float(a * f_stop + b)
            except Exception:
                t_cross = None
        # --- 2026-08-04: label the LOCK, not the crossing ------------------------------------
        # `below` is censored to samples >1.0pp under F_stop, so it holds ONLY the fast rising
        # flank -- the ease-out, the overshoot and the snap-back are all excluded. The fit above
        # therefore extrapolates a constant-velocity flank across the censored deceleration zone
        # and lands on "when a straight continuation would first reach F_stop going up". The shot
        # does not lock there: it locks after ease-out + overshoot + recede, which is why the
        # label read ~70ms short and, because a shorter lead makes the meter freeze even higher,
        # walked further down every session.
        #
        # The freeze ONSET is directly observed: the first sample of the trailing settled run.
        # The sample before it is the last visibly moving one, so the lock became visible in that
        # bracket; take its midpoint and carry the bracket width as measurement sigma. No machine
        # constant is involved -- every quantity comes from this install's own frames.
        if t_cross is not None:
            onset_idx = len(rise)
            for i in range(len(rise) - 1, -1, -1):
                if abs(rise[i][1] - f_stop) > 1.0:   # same plateau-jitter band as the censor
                    break
                onset_idx = i
            if 0 < onset_idx < len(rise) and peak > f_stop + 1.0:
                t_pre = float(rise[onset_idx - 1][0])
                t_on = float(rise[onset_idx][0])
                t_star = 0.5 * (t_pre + t_on)
                sigma_meas = float(np.clip(np.sqrt(
                    ((t_on - t_pre) ** 2) / 12.0        # uniform over the bracket
                    + TICK_WAIT_SD_MS ** 2 + 2.0 ** 2), 4.0, 20.0))
            else:
                # Frozen in place: the meter stopped AT F_stop from below with no overshoot, so
                # the upward crossing IS the onset and both definitions coincide exactly.
                t_star = t_cross
        if t_star is None:
            # Flat/unfittable tail: DROP the label (the old last-rising-sample guess produced
            # exactly the junk labels M5's gate exists to exclude).
            self._last_label_method = "calibration" if calibration else "dropped"
            self._last_status = "rejected_unfittable"
            self._last_rejection = "unfittable"
            self._log_release_observation(False)
            self._frozen_captured = True
            self._pending_release_ms = None
            self._pending_release_calibration = False
            self._pending_validation_target_pct = None
            self._pending_validation_tolerance_pct = None
            return
        if not calibration:
            self._last_label_method = "linear"

        lat_total = t_star - float(self._pending_release_ms)
        validation_target = self._pending_validation_target_pct
        controlled_validation = bool(calibration and validation_target is not None)

        # After L1, an unconstrained broad calibration marker cannot VALIDATE anything: native
        # never named the current-rise, non-cap stop this release was planned to hit, so agreement
        # here could be accidental and the L1/L2 protocol stays deliberately unsatisfied. Throwing
        # the trace away entirely, however, is what left a whole live block stuck at n=1 while a
        # dozen clean 245-258ms rise-freezes went in the bin. Demote instead: the observation
        # becomes an ORDINARY corroboration label that refines the posterior (and therefore the
        # bounded factory-authority blend) while being excluded from every count that can mint
        # VALIDATED authority, and it sets neither the controlled-anchor nor the validation latch.
        corroboration_only = False
        if calibration and self._has_controlled_anchor and not controlled_validation:
            calibration = False
            corroboration_only = True
            self._last_label_method = "corroboration"
            self._last_status = "corroboration_pending"
            self._last_rejection = ""
            accepted = None
        elif controlled_validation and self._pending_release_seq == self._first_controlled_seq:
            self._last_status = "rejected_validation_not_distinct"
            self._last_rejection = "validation_not_distinct"
            self._log_release_observation(False, lat_total, sigma_meas)
            accepted = False
        else:
            accepted = None

        if accepted is None and controlled_validation:
            # The delivered marker carries the maximum residual that still remains inside this
            # shot's non-cap safety margin. Recompute a second independent ceiling from the
            # observed rise slope and L1 posterior SD, then take the stricter one. Thus the target
            # check is both frame-derived and uncertainty-derived; neither a permissive marker nor
            # a noisy anchor can validate a stop far from the planned point.
            residual_tolerance_pct = self._validation_residual_tolerance_pct(slope_lin)
            residual_pct = abs(float(f_stop) - float(validation_target))
            if (not np.isfinite(residual_tolerance_pct)
                    or residual_tolerance_pct <= 0.0
                    or residual_pct > residual_tolerance_pct + 1e-6):
                self._last_status = "rejected_validation_stop_residual"
                self._last_rejection = "validation_stop_residual"
                self._refine_controlled_anchor_from_miss(
                    f_stop=float(f_stop),
                    validation_target=float(validation_target),
                    slope_lin=slope_lin, sigma_meas=sigma_meas,
                    observed_total_ms=lat_total)
                self._log_release_observation(False, lat_total, sigma_meas)
                accepted = False

        # Cold ordinary labels retain the conservative route ceiling. The first controlled
        # non-cap label alone may use the broad, bounded 500ms envelope, but it is telemetry-only.
        # Every later label -- validation or ordinary gameplay -- must lie in the same
        # anchor-centered uncertainty envelope as the posterior outlier gate. This admits the
        # measured ~220ms corroboration around L1=235ms without allowing an unrelated 400ms event
        # merely because some controlled marker once existed.
        lo = self._lo
        hi = self._hi
        if calibration and not self._has_controlled_anchor:
            hi = min(self._post_release_ms, 500.0)
        elif self._has_controlled_anchor:
            posterior_var = min(self._prior_var, self._var + _ONLINE_PROCESS_VAR_MS2)
            n = len(self._fixed_accepted)
            if n < 3:
                radius_ms = max(
                    4.0 * float(np.sqrt(posterior_var + sigma_meas ** 2)), 25.0)
            else:
                arr = np.asarray(self._fixed_accepted, float)
                mad = float(np.median(np.abs(arr - np.median(arr))))
                radius_ms = max(3.0 * 1.4826 * mad, 12.0)
            current_rtt = (self._pending_rtt_ms
                           if self._rtt_regime == "decomposed"
                           and self._pending_rtt_known else 0.0)
            expected_total = self._mu + current_rtt + TICK_WAIT_EXPECT_MS
            lo = max(self._lo, expected_total - radius_ms)
            hi = min(self._post_release_ms, 500.0, expected_total + radius_ms)

        if accepted is None and lo <= lat_total <= hi:
            accepted = self._ingest_label(
                lat_total, sigma_meas, calibration=calibration,
                controlled_validation=controlled_validation,
                corroboration_only=corroboration_only)
            # Record the measured crossing->lock gap alongside the accepted label. Native uses it
            # to keep planning fill-domain stop targets in the CROSSING domain while actuating on
            # the lock-domain lead; without it every L2 validation would miss low by slope*lag.
            if accepted and t_cross is not None and t_star is not None:
                self._lock_lag.append(max(0.0, float(t_star) - float(t_cross)))
            self._log_release_observation(accepted, lat_total, sigma_meas)
        elif accepted is None:
            self._last_status = "rejected_out_of_range"
            self._last_rejection = "out_of_range"
            self._log_release_observation(False, lat_total, sigma_meas)
        self._frozen_captured = True
        self._pending_release_ms = None
        self._pending_release_calibration = False
        self._pending_validation_target_pct = None
        self._pending_validation_tolerance_pct = None

    @_estimator_locked
    def _ingest_label(self, lat_total: float, sigma_meas: float,
                      calibration: bool = False, *,
                      controlled_validation: bool = False,
                      corroboration_only: bool = False,
                      rtt_known: Optional[bool] = None,
                      rtt_ms: Optional[float] = None) -> bool:
        """Decompose the total label and fold it into the L_fixed posterior (gated)."""
        if controlled_validation and (not calibration or not self._has_controlled_anchor):
            self._last_status = "rejected_validation_without_anchor"
            self._last_rejection = "validation_without_anchor"
            return False
        if corroboration_only and (calibration or controlled_validation):
            # Structurally impossible from _try_close_oracle, but a corroboration label that also
            # claimed calibration/validation provenance would be exactly the back door this class
            # of label exists to avoid. Fail closed rather than trust the caller.
            self._last_status = "rejected_corroboration_provenance"
            self._last_rejection = "corroboration_provenance"
            return False
        incoming_rtt_known = (bool(self._pending_rtt_known)
                              if rtt_known is None else bool(rtt_known))
        candidate_regime = self._rtt_regime \
            or ("decomposed" if incoming_rtt_known else "total")
        if candidate_regime == "decomposed" and not incoming_rtt_known:
            # Mixing a total-only label into an RTT-stripped posterior can still converge by N/SD
            # while being biased by the missing RTT. Fail closed and wait for a verified marker.
            self._last_status = "rejected_rtt_unavailable"
            self._last_rejection = "rtt_unavailable"
            return False
        # Total mode remains in one coherent domain even if RTT becomes available later.
        incoming_rtt_ms = self._pending_rtt_ms if rtt_ms is None else rtt_ms
        rtt = float(incoming_rtt_ms) if candidate_regime == "decomposed" else 0.0
        label_fixed = lat_total - rtt - TICK_WAIT_EXPECT_MS

        # The static 60 ms prior is deliberately non-authoritative. A first controlled
        # calibration label initializes the posterior directly from measured evidence; without
        # this branch a clean high-latency route would be rejected or biased toward that prior.
        cold_calibration = bool(calibration and self._n_labels == 0)

        # A converged posterior must remain capable of following a real fixed-path drift. Without
        # bounded process noise its variance tends monotonically to zero, so later clean labels
        # have effectively no influence even after the outlier regime-change escape. One square
        # millisecond per accepted-shot opportunity keeps the steady-state SD below the 3.3 ms
        # authority ceiling while giving sustained measured evidence meaningful online weight.
        posterior_var = self._var
        if self._n_labels > 0 and not cold_calibration:
            posterior_var = min(self._prior_var,
                                self._var + _ONLINE_PROCESS_VAR_MS2)

        # A restored posterior is useful only while the first fresh causal evidence agrees with
        # it. Do not wait for the ordinary two-outlier route-change escape: one contradictory
        # label immediately removes in-memory authority and the persisted blob, then enters this
        # same observation into a genuinely cold epoch.
        dev = label_fixed - self._mu
        if self._restored_from_cache:
            contradiction_radius = max(
                3.0 * float(np.sqrt(posterior_var + float(sigma_meas) ** 2)), 12.0)
            if (not np.isfinite(label_fixed) or not np.isfinite(sigma_meas)
                    or sigma_meas <= 0.0 or abs(dev) > contradiction_radius):
                self.revoke_restored_route(
                    "contradictory_fresh_evidence", invalidate_cache=True)
                return self._ingest_label(
                    lat_total, sigma_meas, calibration=calibration,
                    controlled_validation=False,
                    corroboration_only=corroboration_only,
                    rtt_known=rtt_known, rtt_ms=rtt_ms)

        # --- outlier gate ---
        accept = True
        n = len(self._fixed_accepted)
        dev = label_fixed - self._mu
        if cold_calibration:
            accept = True
        elif n < 3:
            accept = abs(dev) <= max(
                4.0 * float(np.sqrt(posterior_var + sigma_meas ** 2)), 25.0)
        else:
            arr = np.asarray(self._fixed_accepted, float)
            mad = float(np.median(np.abs(arr - np.median(arr))))
            accept = abs(dev) <= max(3.0 * 1.4826 * mad, 12.0)
        if not accept:
            side = 1 if dev > 0 else -1
            if side == self._reject_side:
                self._reject_streak += 1
            else:
                self._reject_side, self._reject_streak = side, 1
            if self._reject_streak >= 2:
                # regime change (capture path / route changed): open the posterior back up and
                # accept this label rather than going deaf.
                #
                # [ORION_REOPEN_SOFT] Decide what happens to the PUBLISHED authority before the
                # learning posterior is touched. Flag OFF: none of this runs and the reopen
                # demotes ready_for_native exactly as before (measured 2026-08-06: that demotion
                # benched the bot for the last two shots of the 46/50 counted batch).
                if self._reopen_guard is not None:
                    # Second escape while retained. Same 4-sigma/25ms family as the n<3 cold
                    # outlier gate below: a forced label that AGREES with the RETAINED posterior
                    # (the transient-outlier shape -- e.g. the 329.1/272.9 pair snapping back to
                    # ~221.6) keeps it published, because the world is re-confirming the retained
                    # value; a forced label that contradicts the retained posterior too means the
                    # regime genuinely moved and the retention must fail closed.
                    g = self._reopen_guard
                    radius = max(4.0 * float(np.sqrt(float(g["var"])
                                                     + float(sigma_meas) ** 2)), 25.0)
                    if abs(label_fixed - float(g["mu"])) > radius:
                        self._drop_reopen_guard("regime_moved_beyond_retained")
                elif self._regime_reopen_soft and self._live_converged_ready():
                    # Only a CONVERGED, route-attested authority is worth retaining. A warming,
                    # cold, or restored-unattested posterior arms nothing and the native gate
                    # stays closed (fail closed) -- exactly today's behaviour.
                    self._arm_reopen_guard()
                posterior_var += 100.0
                self._reject_streak = 0
                self._reject_side = 0
                accept = True
        else:
            self._reject_streak = 0
            self._reject_side = 0
        if not accept:
            self._last_status = "rejected_outlier"
            self._last_rejection = "outlier"
            return False

        # --- conjugate-normal update ---
        if self._rtt_regime is None:
            # Lock only now: a rejected candidate must not choose the measurement domain for all
            # future labels in this estimator epoch.
            self._rtt_regime = candidate_regime
        var_m = float(sigma_meas) ** 2
        if cold_calibration:
            self._mu = float(label_fixed)
            self._var = var_m
            self._reject_streak = 0
            self._reject_side = 0
        else:
            new_var = 1.0 / (1.0 / posterior_var + 1.0 / var_m)
            self._mu = new_var * (self._mu / posterior_var + label_fixed / var_m)
            self._var = new_var
        self._fixed_accepted.append(label_fixed)
        self._rtt_used.append(rtt)
        self._releases_since_label = 0

        # legacy rolling-median telemetry (totals) + emitted point estimate
        self._labels.append(lat_total)
        self._n_labels += 1
        if corroboration_only:
            self._corroboration_labels += 1
        if calibration and not self._has_controlled_anchor:
            self._has_controlled_anchor = True
            self._first_controlled_seq = int(self._pending_release_seq)
        if controlled_validation:
            self._controlled_validation_ready = True
        # Keep restored provenance for this process even after corroborating labels. Native must
        # continue to require its independent neutral controller-delivery attestation; accepting a
        # visual consequence here must not silently bypass that route proof.
        self._recompute()
        # [ORION_REOPEN_SOFT] retained-authority bookkeeping on every accepted label: resolve as
        # soon as the live posterior has honestly re-converged, otherwise spend the relearn
        # budget (and drop, fail closed, when it runs out).
        if self._reopen_guard is not None:
            self._reopen_guard["labels_since"] = int(self._reopen_guard["labels_since"]) + 1
            if self._live_converged_ready():
                self._resolve_reopen_guard()
            else:
                self._expire_reopen_guard()
        if calibration:
            self._last_status = (
                "calibration_ready" if self.ready_for_native else "calibration_warming")
        elif corroboration_only:
            # Never "ready" on this path by construction (see _validation_capable_labels); keep the
            # status honest and greppable so support can tell a demoted controlled trace apart from
            # an ordinary passive gameplay label in the same observation line.
            self._last_status = (
                "ready" if self.ready_for_native else "corroboration_accepted")
        elif self.ready_for_native:
            # "ready_reopen_retained" = the gate is being held open by the RETAINED converged
            # authority while the reopened posterior relearns -- a re-opened good prior, never
            # absent authority. Plain "ready" = the live posterior itself is converged.
            self._last_status = ("ready_reopen_retained"
                                 if self._reopen_guard_publishable() else "ready")
        else:
            self._last_status = "warming"
        self._last_rejection = ""
        self._persist_cache()
        return True

    @_estimator_locked
    def _recompute(self) -> None:
        if not self._labels:
            return
        # Point estimate: posterior reconstitution (l_fixed + typical rtt + tick expectation).
        rtt_ref = float(np.median(self._rtt_used)) if self._rtt_used else 0.0
        self._measured_ms = float(self._mu + rtt_ref + TICK_WAIT_EXPECT_MS)
        # Confidence from the posterior sd: prior-only (sd 12) -> 0; converged (sd <= 3) -> >=0.75.
        sd = float(np.sqrt(self._var))
        self._conf = float(np.clip(1.0 - sd / 12.0, 0.0, 1.0))

    # ---- [ORION_REOPEN_SOFT] retained pre-reopen authority --------------------------------------
    @_estimator_locked
    def _live_converged_ready(self) -> bool:
        """ready_for_native's converged branch evaluated on the LIVE learning posterior only.

        Deliberately never consults the retained guard (which publishes through the public
        properties), so guard arming and guard resolution can never self-justify.
        """
        sd_ms = float(np.sqrt(self._var))
        route_ready = (not self._restored_from_cache
                       or self._restored_route_attested)
        return bool(route_ready
                    and self._measured_ms is not None and self._measured_ms > 0.0
                    and self._validation_capable_labels() >= CONVERGED_MIN_LABELS
                    and np.isfinite(sd_ms)
                    and 0.0 < sd_ms <= CONVERGED_MAX_SD_MS)

    @_estimator_locked
    def _reopen_guard_publishable(self) -> bool:
        """Pure check (no mutation): is the retained authority currently publishable?

        Side-effect free so a single telemetry_snapshot() -- which reads many properties under
        one lock hold -- can never observe a torn tuple (retained value with live sd, or vice
        versa). Expiry MUTATION happens in _expire_reopen_guard(), called from update() and the
        label-accept path.
        """
        g = self._reopen_guard
        if g is None or not self._regime_reopen_soft:
            return False
        # Structural fail-closed: any posterior wipe (reset/revocation) regresses _n_labels
        # below the count captured at arm time, deactivating the guard even if a reset path
        # forgot the explicit drop.
        if int(self._n_labels) < int(g["n"]):
            return False
        if int(g["labels_since"]) >= _REOPEN_GUARD_MAX_LABELS:
            return False
        armed = g.get("armed_wall_ms")
        if (armed is not None and self._last_wall_ms is not None
                and float(self._last_wall_ms) - float(armed) > _REOPEN_GUARD_MAX_AGE_MS):
            return False
        return True

    @_estimator_locked
    def _expire_reopen_guard(self) -> None:
        """Mutating twin of _reopen_guard_publishable(): log and drop a dead guard."""
        g = self._reopen_guard
        if g is None or self._reopen_guard_publishable():
            return
        if int(self._n_labels) < int(g["n"]):
            reason = "posterior_reset"
        elif int(g["labels_since"]) >= _REOPEN_GUARD_MAX_LABELS:
            reason = "relearn_budget_exhausted"
        else:
            reason = "stale_wall_age"
        self._drop_reopen_guard(reason)

    @_estimator_locked
    def _arm_reopen_guard(self) -> None:
        """Snapshot the (still-converged) posterior as the published authority for the reopen."""
        self._reopen_guard = {
            "mu": float(self._mu),
            "var": float(self._var),
            "measured_ms": float(self._measured_ms),
            "n": int(self._n_labels),
            "armed_wall_ms": (float(self._last_wall_ms)
                              if self._last_wall_ms is not None else None),
            "labels_since": 0,
        }
        logger.warning(
            "latency regime reopen SOFT: retained converged authority stays published "
            "(retained_fixed_ms=%.1f retained_sd_ms=%.2f n=%d) while the reopened posterior "
            "relearns -- this is a re-opened GOOD prior, not absent authority",
            float(self._mu), float(np.sqrt(self._var)), int(self._n_labels))

    @_estimator_locked
    def _drop_reopen_guard(self, reason: str) -> None:
        """Fail closed: stop publishing the retained authority; native benches until ready."""
        g = self._reopen_guard
        if g is None:
            return
        self._reopen_guard = None
        logger.warning(
            "latency reopen guard dropped: reason=%s retained_fixed_ms=%.1f live_fixed_ms=%.1f "
            "live_sd_ms=%.2f -- no trustworthy authority is published now; native holds fire "
            "until the posterior re-converges",
            str(reason or "unknown")[:48], float(g["mu"]), float(self._mu),
            float(np.sqrt(self._var)))

    @_estimator_locked
    def _resolve_reopen_guard(self) -> None:
        """The reopened posterior re-converged: hand publication back to the live values."""
        g = self._reopen_guard
        if g is None:
            return
        self._reopen_guard = None
        logger.info(
            "latency reopen guard resolved: live posterior re-converged "
            "(published_fixed_ms %.1f -> %.1f, sd_ms=%.2f, relearn_labels=%d)",
            float(g["mu"]), float(self._mu), float(np.sqrt(self._var)),
            int(g["labels_since"]))

    @property
    @_estimator_locked
    def reopen_guard_active(self) -> bool:
        """Telemetry-only: True while a retained pre-reopen authority is being published."""
        return self._reopen_guard_publishable()

    # ---- emitted estimate -----------------------------------------------------------------------
    @property
    @_estimator_locked
    def bootstrapped(self) -> bool:
        return self._measured_ms is None

    @property
    @_estimator_locked
    def lock_lag_ms(self) -> float:
        """Median measured gap (ms) between the meter's F_stop up-crossing and the visible lock.

        Labels are timestamped at the LOCK; native still predicts fill-domain stop targets in the
        CROSSING domain, so it must subtract this when planning a stop VALUE (not when actuating).
        -1.0 until a label has been accepted. Self-measured per install -- no machine constant.
        """
        if not self._lock_lag:
            return -1.0
        return float(np.median(np.asarray(self._lock_lag, float)))

    @property
    @_estimator_locked
    def factory_prior_active(self) -> bool:
        # This describes the current ACTUATION authority, not whether an
        # observed posterior exists.  Ordinary labels may refine ``value_ms``
        # in shadow while the factory mean/SD stay bit-for-bit fixed until that
        # posterior earns validated authority.
        return self.authority_kind == "factory"

    @property
    @_estimator_locked
    def authority_kind(self) -> str:
        if self.ready_for_native:
            return "validated"
        factory_available = bool(
            not self._restored_from_cache
            and self._boot_prior_ms > 0.0
            and self._factory_prior_source
            and self._factory_prior_version
            and np.isfinite(self._factory_prior_sd_ms)
            and 6.0 <= self._factory_prior_sd_ms <= 100.0)
        return "factory" if factory_available else "none"

    @_estimator_locked
    def _authority_move_allowance_ms(self) -> float:
        """How far an accepted-label posterior may pull the published factory-kind lead.

        Zero at n=0 (the seed is all we know), widening with corroboration. Scaled by the factory
        SD because that SD is the seed's own statement of how well it believes itself: a wide,
        honestly-uncertain profile yields ground quickly, a narrow one resists.
        """
        n = int(max(0, self._n_labels))
        if n <= 0:
            return 0.0
        sd = float(self._factory_prior_sd_ms)
        if not np.isfinite(sd) or sd <= 0.0:
            return 0.0
        k = _AUTHORITY_MOVE_K_SCALE * (1.0 - float(np.exp(-float(n) / _AUTHORITY_MOVE_K_TAU)))
        return float(k * sd)

    @property
    @_estimator_locked
    def authority_value_ms(self) -> float:
        kind = self.authority_kind
        if kind == "validated":
            return float(self.value_ms())
        if kind == "factory":
            boot = float(self._boot_prior_ms)
            # n==0: the posterior IS the seed, so this is bit-identical to the previous behaviour.
            if self._n_labels <= 0:
                return boot
            posterior = float(self.value_ms())
            if not np.isfinite(posterior) or posterior <= 0.0:
                return boot
            allowance = self._authority_move_allowance_ms()
            if allowance <= 0.0:
                return boot
            return float(boot + np.clip(posterior - boot, -allowance, allowance))
        return 0.0

    @property
    @_estimator_locked
    def authority_sd_ms(self) -> float:
        kind = self.authority_kind
        if kind == "validated":
            return float(self.l_fixed_sd_ms)
        if kind == "factory":
            seed_sd = float(self._factory_prior_sd_ms)
            if self._n_labels <= 0:
                return seed_sd
            # Corroborated leads are genuinely tighter than the seed, and the scheduler spends this
            # SD directly: it enters combinedSigmaMs, which gates whether a tip decision may be
            # submitted at all. Never widen past the seed, and never claim more precision than the
            # posterior actually has.
            post_sd = float(self.l_fixed_sd_ms)
            if not np.isfinite(post_sd) or post_sd <= 0.0:
                return seed_sd
            return float(min(seed_sd, max(post_sd, PROVISIONAL_MAX_SD_MS)))
        return 0.0

    @property
    @_estimator_locked
    def factory_prior_source(self) -> str:
        return self._factory_prior_source if self.factory_prior_active else ""

    @property
    @_estimator_locked
    def factory_prior_version(self) -> str:
        return self._factory_prior_version if self.factory_prior_active else ""

    @property
    @_estimator_locked
    def factory_prior_sd_ms(self) -> float:
        return float(self._factory_prior_sd_ms) if self.factory_prior_active else 0.0

    @property
    @_estimator_locked
    def n_labels(self) -> int:
        return self._n_labels

    @property
    @_estimator_locked
    def l_fixed_ms(self) -> float:
        """Posterior mean of the FIXED latency component (rtt + tick wait stripped).

        [ORION_REOPEN_SOFT] While a retained pre-reopen authority is active this publishes THAT
        converged mean; the live learning value stays visible in the observation log line.
        """
        if self._reopen_guard_publishable():
            return float(self._reopen_guard["mu"])
        return float(self._mu)

    @property
    @_estimator_locked
    def l_fixed_sd_ms(self) -> float:
        """Posterior sd of L_fixed. Prior sd until labels arrive; convergence target <= 3.3ms.

        [ORION_REOPEN_SOFT] While a retained pre-reopen authority is active this publishes the
        RETAINED sd (the last honestly-converged statement), keeping the published
        value/sd/ready tuple coherent for native's contract checks. Internal learning and
        protocol predicates read the live posterior directly.
        """
        if self._reopen_guard_publishable():
            return float(np.sqrt(float(self._reopen_guard["var"])))
        return float(np.sqrt(self._var))

    def _validation_capable_labels(self) -> int:
        """Accepted labels that may count toward VALIDATED actuation authority.

        Corroboration observations are excluded. Without this subtraction the demotion in
        ``_try_close_oracle`` would become a back door around the whole L1/L2 protocol: six clean
        post-L1 traces that native never named a validation target for would satisfy the purely
        passive N/SD convergence gate and mint validated authority no handshake ever authorised.
        They are still real evidence, so they keep moving ``_mu``/``_var`` -- and through the
        bounded factory blend, actuation -- they just cannot promote the authority KIND.
        """
        return int(max(0, int(self._n_labels) - int(self._corroboration_labels)))

    @property
    @_estimator_locked
    def corroboration_labels(self) -> int:
        """Count of protocol-unvalidated corroboration labels folded into the posterior."""
        return int(self._corroboration_labels)

    @property
    @_estimator_locked
    def converged(self) -> bool:
        # Live posterior sd on purpose (not the published l_fixed_sd_ms): learning-state
        # predicates must never be satisfied by a retained reopen-guard publication.
        return (self._validation_capable_labels() >= 3
                and float(np.sqrt(self._var)) <= CONVERGED_MAX_SD_MS)

    @property
    @_estimator_locked
    def provisional_ready(self) -> bool:
        """Guarded warm authority after a distinct causal validation release.

        This is deliberately a latch, not ``last_label_method == calibration``: later passive
        measurements refine the same posterior without dropping an otherwise valid warm start.
        L1 always remains telemetry-only. Native may actuate only after L2 both agrees with L1 and
        visibly stops inside the slope/SD-derived residual around its planned in-green target.
        """
        restored_ready = (not self._restored_from_cache
                          or self._restored_route_attested)
        # Live posterior sd on purpose (not the published l_fixed_sd_ms): a retained
        # reopen-guard publication must never mint provisional authority.
        live_sd_ms = float(np.sqrt(self._var))
        return bool(restored_ready
                    and self._has_controlled_anchor and self._controlled_validation_ready
                    and self._measured_ms is not None
                    and self._n_labels >= 2 and np.isfinite(self._measured_ms)
                    and self._lo <= self._measured_ms <= 500.0
                    and np.isfinite(live_sd_ms)
                    and 0.0 < live_sd_ms <= PROVISIONAL_MAX_SD_MS)

    @property
    @_estimator_locked
    def controlled_anchor_available(self) -> bool:
        """True when L1 exists for validation scheduling, never for tip actuation."""
        restored_ready = (not self._restored_from_cache
                          or self._restored_route_attested)
        live_sd_ms = float(np.sqrt(self._var))
        return bool(restored_ready
                    and self._has_controlled_anchor and self._measured_ms is not None
                    and self._n_labels >= 1 and np.isfinite(self._measured_ms)
                    and self._lo <= self._measured_ms <= 500.0
                    and np.isfinite(live_sd_ms)
                    and 0.0 < live_sd_ms <= 20.0)

    @property
    @_estimator_locked
    def freeze_kind(self) -> str:
        """Classification of the most recent release-attributed freeze: rise/deflate/ambiguous."""
        return self._last_freeze_kind

    @property
    @_estimator_locked
    def label_starved(self) -> bool:
        """True when >=8 releases produced no accepted label (e.g. every freeze lands inside
        green, above the F_stop<=95 gate). The remedy is a warmup probe run, not a wider gate."""
        return self._releases_since_label >= 8

    @property
    @_estimator_locked
    def ready_for_native(self) -> bool:
        """True for a causally validated controlled warm start or a fully converged posterior.

        Native still applies its own freshness, epoch and one-shot rebaseline
        checks. A restored posterior additionally stays non-authoritative until ``value_ms``
        receives a current verified RTT for this process.

        [ORION_REOPEN_SOFT] While a retained pre-reopen authority is active this stays True on
        the strength of THAT converged snapshot (which value_ms/l_fixed_* are then publishing):
        a regime reopen affects learning, never the ready gate. The guard can only exist if a
        converged, route-attested posterior took the escape, so this branch can never
        manufacture readiness out of absent or warming authority -- and it still sits behind
        route_ready, so a route revocation benches regardless.
        """
        # Live posterior sd on purpose: the published l_fixed_sd_ms rides the guard, and the
        # guard's own publishability must not feed back into this convergence check.
        sd_ms = float(np.sqrt(self._var))
        converged = bool(self._measured_ms is not None and self._measured_ms > 0.0
                         and self._validation_capable_labels() >= CONVERGED_MIN_LABELS
                         and np.isfinite(sd_ms)
                         and 0.0 < sd_ms <= CONVERGED_MAX_SD_MS)
        route_ready = (not self._restored_from_cache
                       or self._restored_route_attested)
        if route_ready and self._reopen_guard_publishable():
            return True
        return bool(route_ready and (converged or self.provisional_ready))

    @property
    @_estimator_locked
    def last_status(self) -> str:
        return str(self._last_status)

    @property
    @_estimator_locked
    def last_rejection(self) -> str:
        return str(self._last_rejection)

    @property
    @_estimator_locked
    def label_method(self) -> str:
        return self._last_label_method

    @property
    @_estimator_locked
    def rtt_regime(self) -> str:
        """Posterior domain: ``decomposed``, ``total``, or ``unlocked`` before label one."""
        return self._rtt_regime or "unlocked"

    @property
    @_estimator_locked
    def restored_from_cache(self) -> bool:
        return bool(self._restored_from_cache)

    @property
    @_estimator_locked
    def restored_route_attested(self) -> bool:
        return bool(self._restored_from_cache and self._restored_route_attested)

    def telemetry_snapshot(self) -> LatencyTelemetrySnapshot:
        """Return one immutable estimator generation for sidecar serialization.

        Do not replace this with independent property reads at the call site.  A
        release marker and a frame-driven close can otherwise be observed on
        opposite sides of the same JSON payload.
        """
        with self._lock:
            value_ms = float(self.value_ms())
            factory_prior_active = bool(self.factory_prior_active)
            return LatencyTelemetrySnapshot(
                value_ms=value_ms,
                confidence=float(self.confidence()),
                bootstrapped=bool(self.bootstrapped),
                authority_kind=str(self.authority_kind),
                authority_value_ms=float(self.authority_value_ms),
                authority_sd_ms=float(self.authority_sd_ms),
                factory_prior_active=factory_prior_active,
                factory_prior_source=(
                    str(self.factory_prior_source) if factory_prior_active else ""),
                factory_prior_version=(
                    str(self.factory_prior_version) if factory_prior_active else ""),
                factory_prior_sd_ms=(
                    float(self.factory_prior_sd_ms) if factory_prior_active else 0.0),
                n_labels=max(0, int(self.n_labels)),
                corroboration_labels=max(0, int(self.corroboration_labels)),
                l_fixed_ms=float(self.l_fixed_ms),
                l_fixed_sd_ms=float(self.l_fixed_sd_ms),
                ready_for_native=bool(self.ready_for_native),
                controlled_anchor_available=bool(self.controlled_anchor_available),
                provisional_ready=bool(self.provisional_ready),
                restored_from_cache=bool(self.restored_from_cache),
                restored_route_attested=bool(self.restored_route_attested),
                label_starved=bool(self.label_starved),
                last_status=str(self.last_status),
                last_rejection=str(self.last_rejection),
                label_method=str(self.label_method),
                rtt_regime=str(self.rtt_regime),
                last_probe_raw_ms=float(self.last_probe_raw_ms),
                probe_spawn_estimate_ms=float(self.probe_spawn_estimate_ms),
                probe_recommended=bool(self.probe_recommended),
                probe_prior_restored=bool(self.probe_prior_restored),
                probe_labels=int(self.probe_labels),
                tick_phase_ms=float(self.tick_phase_ms),
                tick_phase_conf=float(self.tick_phase_conf),
                tick_phase_sd_ms=float(self.tick_phase_sd_ms),
                release_seq=int(self.release_seq),
                pending_release_seq=int(self._pending_release_seq),
                pending_release=self._pending_release_ms is not None,
                phase=str(self.phase),
                freeze_kind=str(self.freeze_kind),
                reopen_guard_active=bool(self.reopen_guard_active),
            )

    @_estimator_locked
    def value_ms(self, rtt_ms: Optional[float] = None) -> float:
        """Measured release-path latency (ms), reconstituted for the live route.

        ``L_fixed`` is what the oracle learns.  Network RTT is deliberately removed from each
        accepted label so Wi-Fi wobble cannot contaminate that posterior.  When a current,
        independently verified filtered RTT is available, add *that* value back here instead of
        the median RTT from the calibration shots.  This is the continuous live-adjustment path:
        it changes the published total immediately without pretending that RTT drift is a new
        visual label.  Callers that cannot prove a current RTT retain the conservative historical
        reconstitution used before this optional argument was added.
        """
        # [ORION_REOPEN_SOFT] While a retained pre-reopen authority is active, publish THAT
        # converged reconstitution; the live learning posterior keeps updating underneath.
        guard_active = self._reopen_guard_publishable()
        published_measured = (float(self._reopen_guard["measured_ms"]) if guard_active
                              else self._measured_ms)
        published_mu = (float(self._reopen_guard["mu"]) if guard_active else self._mu)
        if published_measured is not None:
            # Route health is the outer authority gate.  In particular, supplying a fresh RTT must
            # not let an un-attested restored posterior escape through either reconstitution branch.
            if self._restored_from_cache and not self._restored_route_attested:
                self._last_status = "restored_waiting_route_attestation"
                return 0.0
            try:
                current_rtt = float(rtt_ms) if rtt_ms is not None else None
                if (current_rtt is not None and np.isfinite(current_rtt)
                        and current_rtt >= 0.0):
                    if self._rtt_regime == "decomposed":
                        return float(published_mu + current_rtt + TICK_WAIT_EXPECT_MS)
                    if self._rtt_regime == "total":
                        if self._total_mode_rtt_baseline_ms is None:
                            self._total_mode_rtt_baseline_ms = current_rtt
                            return float(published_measured)
                        return float(published_measured
                                     + current_rtt - self._total_mode_rtt_baseline_ms)
            except (TypeError, ValueError, OverflowError):
                pass
            return float(published_measured)
        return float(self._boot_prior_ms)

    @_estimator_locked
    def confidence(self) -> float:
        # [ORION_REOPEN_SOFT] Coherent with the published sd: while the retained authority is
        # active, confidence reflects the retained (converged) posterior it publishes.
        if self._reopen_guard_publishable():
            sd = float(np.sqrt(float(self._reopen_guard["var"])))
            return float(np.clip(1.0 - sd / 12.0, 0.0, 1.0))
        if self._measured_ms is None:
            # a boot prior is a weak guess; near-zero confidence but nonzero if a prior was given.
            return 0.15 if self._boot_prior_ms > 0.0 else 0.0
        return self._conf


def try_load(route_scope: str = "", cache_path: Optional[str] = None,
             restore_cache: bool = True) -> Optional["LatencyEstimator"]:
    """Return a LatencyEstimator iff ORION_MEASURE_LATENCY=1 (default on), else None.

    The packaged, versioned route model supplies a weak first-shot prior. An explicit
    ORION_LATENCY_BOOT_MS remains a developer diagnostic override but is intentionally not marked
    as factory authority, so native cannot actuate from it through the zero-setup path."""
    if os.environ.get("ORION_MEASURE_LATENCY", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    factory = None
    raw_override = os.environ.get("ORION_LATENCY_BOOT_MS")
    if raw_override is None:
        factory = _load_factory_prior(route_scope)
        boot = float(factory["mean_ms"]) if factory else 0.0
    else:
        try:
            boot = float(raw_override or 0.0)
        except Exception:
            boot = 0.0
    try:
        return LatencyEstimator(boot_prior_ms=boot,
                                hi_ms=(500.0 if factory else 180.0),
                                mu_prior_ms=(factory["mean_ms"] - TICK_WAIT_EXPECT_MS
                                             if factory else 60.0),
                                sd_prior_ms=(factory["sd_ms"] if factory else 12.0),
                                route_scope=str(route_scope or ""),
                                cache_path=cache_path,
                                restore_cache=bool(restore_cache),
                                factory_prior_source=(factory["source"] if factory else ""),
                                factory_prior_version=(factory["version"] if factory else ""),
                                factory_prior_sd_ms=(factory["sd_ms"] if factory else 0.0))
    except Exception:
        return None
