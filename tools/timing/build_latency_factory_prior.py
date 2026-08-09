#!/usr/bin/env python3
"""Regenerate ``models/latency_factory_prior.json`` from accepted latency labels in a log.

WHY THIS EXISTS
---------------
The shipped ``models/latency_factory_prior.json`` was, until this script landed, an untracked
hand-edit with no source of truth: a single number (241.4 ms) was typed into all four route
profiles, and the four ``sd_ms`` values (24/32/36/42) had no traceable derivation at all. The
artifact was nevertheless packaged into the release sidecar bundle
(``tools/sidecar_bundle_manifest.py``, ``scripts/build_orion_sidecar.ps1``), so a shipped build
embedded an actuation constant that existed nowhere in git.

This builder makes the artifact reproducible. Everything it emits is derived from labels that
are actually present in an Orion log, plus two explicitly-declared ignorance terms for the parts
of the route matrix that have never been measured even once.

THE EVIDENCE PROBLEM
--------------------
Every accepted latency label Orion has ever produced comes from ONE route: capture-card video
with the pre-encryption PIPE controller path. The ``decoder-*`` video half and the ``*-vigem``
controller half have never been measured. A prior that reports the same mean for all four routes
with only cosmetic sd differences misrepresents that as four independent measurements.

So this builder:
  * fits the measured route from the real labels, recency-weighted (the labels contain a genuine
    regime shift, so the newest evidence must dominate without discarding the older regime);
  * propagates that fit to the unmeasured routes UNCHANGED in mean, because there is no evidence
    to justify shifting it -- except for the one physically-motivated ViGEm delta;
  * widens the sd of every unmeasured route by an explicit, declared ignorance variance, so the
    file encodes "we do not know this" instead of inventing precision.

Run:
    ./.venv/Scripts/python.exe tools/timing/build_latency_factory_prior.py --check
    ./.venv/Scripts/python.exe tools/timing/build_latency_factory_prior.py --write
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Sequence

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "models" / "latency_factory_prior.json"
DEFAULT_LOGS = (ROOT / "logs" / "orion_native.log", ROOT / "logs" / "orion_native.log.1")

# --- Contract constants. These are consumed by name/prefix and MUST NOT be renamed. -----------
# ``latency_estimator._load_factory_prior`` matches ``schema`` exactly and selects a profile by
# ``scope_contains``; ``AutomationEngine.cpp::factoryLatencyPriorMatchesRoute`` parses the
# published source string "<model_id>:<name>" by the "venice-e2e-route-prior:" prefix and the
# "-pipe" / "-vigem" suffix. Changing any of these silently disables the prior at runtime.
SCHEMA = "orion.latency_factory_prior.v1"
MODEL_ID = "venice-e2e-route-prior"

# Hard bounds enforced independently by three consumers. Verified in-tree:
#   latency_estimator.py            -- 15.0 <= mean_ms <= 500.0, 6.0 <= sd_ms <= 100.0
#   tools/sidecar_bundle_manifest.py-- same bounds, at bundle-build time
#   native_orion/src/AutomationEngine.cpp -- 6.0 <= authority sd <= 100.0
MEAN_MIN_MS, MEAN_MAX_MS = 15.0, 500.0
SD_MIN_MS, SD_MAX_MS = 6.0, 100.0

# One console input-poll period. Mirrors latency_estimator.TICK_WAIT_EXPECT_MS (8.3).
TICK_WAIT_EXPECT_MS = 8.3
# One 60Hz video frame period.
FRAME_PERIOD_60_MS = 1000.0 / 60.0

# --- Declared ignorance for the never-measured halves of the route matrix. --------------------
# Both are modelled as a symmetric uniform distribution over a differential we have no data on.
# A uniform on [-a, +a] contributes sd = a / sqrt(3). Centred on zero because we do not even know
# the SIGN of the differential -- only its plausible magnitude.
#
# VIDEO: capture-card HDMI capture vs. network decode. These pipelines differ by whole frame
# periods, but which is slower depends on encoder queue depth and card buffering. +/- 2 frame
# periods is the honest span.
VIDEO_IGNORANCE_HALF_WIDTH_MS = 2.0 * FRAME_PERIOD_60_MS
# CONTROLLER: an emulated ViGEm pad traverses the Windows HID/USB stack before reaching the same
# transport the PIPE path writes to directly. The point estimate for the delta is exactly one
# poll period (below); the uncertainty around it is several more, since the route has never been
# measured on any machine.
CONTROLLER_IGNORANCE_HALF_WIDTH_MS = 4.0 * TICK_WAIT_EXPECT_MS

# Recency half-life for weighting labels. The observed evidence spans a regime shift roughly 11h
# wide; a 12h half-life lets the newer regime dominate without throwing the older one away.
DEFAULT_HALF_LIFE_HOURS = 12.0

_OBS_RE = re.compile(
    r"^(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s.*?"
    r"latency observation:\s(?P<body>.*)$"
)
_FIELD_RE = re.compile(r"(?P<key>[a-z_]+)=(?P<value>[^\s]+)")
# The controller DELIVERY route, as proposed/acknowledged/rejected between native and sidecar.
# This is the route the latency label is actually attributable to. (Note: "Controller route:
# RawInput ... -> ViGEm/XUSB -> Chiaki" lines describe local RawInput mirroring and are NOT the
# delivery route -- conflating them would wrongly label PIPE evidence as ViGEm evidence.)
_ROUTE_RE = re.compile(r"Timing route [a-z ]*?(?:by sidecar|receipt[^:]*)?[^:]*: route=(?P<route>\w+)")
# Earlier sessions predate the explicit route-proof handshake. There the delivery route is stated
# by the input hook itself: once the pre-encryption pipe hook is installed, controller input
# reaches the console through it and ViGEm is only a standby ("fallback active").
_PIPE_HOOK_RE = re.compile(r"Input hook ENABLED \(pre-encryption pipe")
_HOOK_OFF_RE = re.compile(r"Input hook (?:DISABLED|REMOVED|detached)", re.IGNORECASE)
_VIDEO_TIER_RE = re.compile(r"Capture health: tier=(?P<tier>\w+)")
# The estimator's own digest of the full route scope string. One distinct value across a log is
# conclusive proof that every label in it came from a single route.
_SCOPE_RE = re.compile(r"\bscope=(?P<scope>[0-9a-f]{6,})")


class Label(NamedTuple):
    when: datetime
    total_ms: float
    video_tier: str
    controller_route: str


def _uniform_sd(half_width_ms: float) -> float:
    """sd of a symmetric uniform distribution on [-half_width, +half_width]."""
    return half_width_ms / math.sqrt(3.0)


def parse_log(path: Path) -> tuple[list[Label], set[str]]:
    """Extract accepted latency labels, each attributed to the route in force AT THAT MOMENT.

    Attribution is by last-seen-before-the-label rather than whole-log, because a log routinely
    contains short excursions to another tier that no label falls inside. Whole-log attribution
    would either discard usable evidence or, worse, mislabel it.
    """
    labels: list[Label] = []
    scopes: set[str] = set()
    tier = "unknown"
    route = "unknown"
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            found_route = _ROUTE_RE.search(line)
            if found_route:
                route = found_route.group("route").lower()
            elif _PIPE_HOOK_RE.search(line):
                route = "pipe"
            elif _HOOK_OFF_RE.search(line):
                # The pipe hook came down; ViGEm is now carrying input. Any label after this
                # point belongs to a different route and must not be pooled with pipe evidence.
                route = "vigem"
            found_tier = _VIDEO_TIER_RE.search(line)
            if found_tier:
                tier = found_tier.group("tier").lower()
            found_scope = _SCOPE_RE.search(line)
            if found_scope:
                scopes.add(found_scope.group("scope"))
            match = _OBS_RE.match(line.rstrip("\n"))
            if not match:
                continue
            fields = {m.group("key"): m.group("value")
                      for m in _FIELD_RE.finditer(match.group("body"))}
            if fields.get("accepted") != "1":
                continue
            try:
                total_ms = float(fields["total_ms"])
            except (KeyError, ValueError):
                continue
            when = datetime.strptime(
                match.group("iso"), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            labels.append(Label(when, total_ms, tier, route))
    return labels, scopes


def collect(paths: Sequence[Path]) -> tuple[list[Label], set[str]]:
    labels: list[Label] = []
    scopes: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        found, found_scopes = parse_log(path)
        labels.extend(found)
        scopes |= found_scopes
    labels.sort(key=lambda row: row.when)
    return labels, scopes


def split_regimes(labels: Sequence[Label]) -> list[list[Label]]:
    """Split the label series at its single largest temporal gap.

    A reproducible stand-in for hand-labelling "daytime" vs "night" sessions. Only used to prove
    the published sd actually straddles the regimes present in the evidence.
    """
    if len(labels) < 3:
        return [list(labels)]
    gaps = [(labels[i + 1].when - labels[i].when).total_seconds()
            for i in range(len(labels) - 1)]
    cut = max(range(len(gaps)), key=lambda i: gaps[i])
    return [list(labels[: cut + 1]), list(labels[cut + 1:])]


def fit_measured_route(labels: Sequence[Label], half_life_hours: float) -> dict:
    """Recency-weighted point estimate + honest predictive sd for the ONE measured route."""
    if not labels:
        raise SystemExit("no accepted latency labels found; cannot build a prior from nothing")
    newest = labels[-1].when
    weights = []
    for label in labels:
        age_hours = (newest - label.when).total_seconds() / 3600.0
        weights.append(0.5 ** (age_hours / half_life_hours))

    sum_w = sum(weights)
    sum_w2 = sum(w * w for w in weights)
    mean = sum(w * l.total_ms for w, l in zip(weights, labels)) / sum_w

    # Reliability-weights unbiased variance. Falls back to the biased form if the effective
    # denominator degenerates (single dominant label).
    denominator = sum_w - sum_w2 / sum_w
    ss = sum(w * (l.total_ms - mean) ** 2 for w, l in zip(weights, labels))
    variance = ss / denominator if denominator > 1e-9 else ss / sum_w
    n_eff = (sum_w * sum_w) / sum_w2  # Kish effective sample size

    # Posterior-predictive width: spread of the observations PLUS the uncertainty in the mean
    # itself. With ~7 effective samples the latter is not negligible and must not be hidden.
    predictive_sd = math.sqrt(variance * (1.0 + 1.0 / n_eff))

    # Floor: the published sd must reach the mean of every regime present in the evidence, or it
    # would claim a session drawn entirely from one regime is an outlier.
    regimes = split_regimes(labels)
    regime_means = [sum(l.total_ms for l in group) / len(group) for group in regimes if group]
    straddle = max((abs(rm - mean) for rm in regime_means), default=0.0)

    return {
        "mean_ms": mean,
        "sd_ms": max(predictive_sd, straddle),
        "predictive_sd_ms": predictive_sd,
        "straddle_floor_ms": straddle,
        "n": len(labels),
        "n_eff": n_eff,
        "regime_means": regime_means,
        "window": (labels[0].when, labels[-1].when),
    }


def build_payload(fit: dict, half_life_hours: float, log_names: Sequence[str],
                  evidence_route: str) -> dict:
    base_mean = fit["mean_ms"]
    base_sd = fit["sd_ms"]
    video_ignorance = _uniform_sd(VIDEO_IGNORANCE_HALF_WIDTH_MS)
    controller_ignorance = _uniform_sd(CONTROLLER_IGNORANCE_HALF_WIDTH_MS)

    def widen(*extra_sds: float) -> float:
        return math.sqrt(base_sd ** 2 + sum(sd ** 2 for sd in extra_sds))

    window_lo, window_hi = fit["window"]
    measured_window = f"{window_lo:%Y-%m-%dT%H:%M:%SZ}/{window_hi:%Y-%m-%dT%H:%M:%SZ}"
    unmeasured = "never measured"

    profiles = [
        {
            "name": "capture-card-pipe",
            "scope_contains": ["controller=pipe", "capture_card"],
            "mean_ms": round(base_mean, 1),
            "sd_ms": round(base_sd, 1),
            "evidence_n": fit["n"],
            "evidence_route": evidence_route,
            "evidence_window_utc": measured_window,
            "derivation": (
                "recency-weighted mean of all accepted labels (half-life "
                f"{half_life_hours:g}h); sd = max(posterior-predictive, regime-straddle floor)"
            ),
        },
        {
            "name": "decoder-pipe",
            "scope_contains": ["controller=pipe", "decoder"],
            "mean_ms": round(base_mean, 1),
            "sd_ms": round(widen(video_ignorance), 1),
            "evidence_n": 0,
            "evidence_route": unmeasured,
            "evidence_window_utc": "",
            "derivation": (
                "controller half shares capture-card-pipe evidence; video half never measured -- "
                f"mean unchanged, sd widened by an independent uniform +/-"
                f"{VIDEO_IGNORANCE_HALF_WIDTH_MS:.1f}ms (2 frame periods @60Hz) ignorance term"
            ),
        },
        {
            "name": "capture-card-vigem",
            "scope_contains": ["capture_card"],
            "mean_ms": round(base_mean + TICK_WAIT_EXPECT_MS, 1),
            "sd_ms": round(widen(controller_ignorance), 1),
            "evidence_n": 0,
            "evidence_route": unmeasured,
            "evidence_window_utc": "",
            "derivation": (
                "video half shares capture-card-pipe evidence; controller half never measured -- "
                f"mean shifted by exactly one console input-poll period ({TICK_WAIT_EXPECT_MS}ms), "
                f"the only physically-motivated ViGEm delta; sd widened by an independent uniform "
                f"+/-{CONTROLLER_IGNORANCE_HALF_WIDTH_MS:.1f}ms (4 poll periods) ignorance term"
            ),
        },
        {
            "name": "decoder-vigem",
            "scope_contains": ["decoder"],
            "mean_ms": round(base_mean + TICK_WAIT_EXPECT_MS, 1),
            "sd_ms": round(widen(video_ignorance, controller_ignorance), 1),
            "evidence_n": 0,
            "evidence_route": unmeasured,
            "evidence_window_utc": "",
            "derivation": (
                "neither half ever measured -- one poll period of ViGEm shift on the "
                "capture-card-pipe mean, sd widened by BOTH independent ignorance terms"
            ),
        },
    ]

    payload = {
        "schema": SCHEMA,
        "model_id": MODEL_ID,
        "model_version": "",  # filled below, content-derived
        "generated_by": "tools/timing/build_latency_factory_prior.py",
        "generated_from": {
            "logs": list(log_names),
            "accepted_labels": fit["n"],
            "evidence_route": evidence_route,
            "evidence_window_utc": measured_window,
            "recency_half_life_hours": half_life_hours,
            "effective_sample_size": round(fit["n_eff"], 3),
            "regime_means_ms": [round(value, 2) for value in fit["regime_means"]],
            "posterior_predictive_sd_ms": round(fit["predictive_sd_ms"], 3),
            "regime_straddle_floor_ms": round(fit["straddle_floor_ms"], 3),
            "note": (
                "ONE route has ever been measured. Only capture-card-pipe carries evidence_n>0; "
                "the other three profiles are that same evidence propagated with explicit "
                "ignorance terms. They are NOT four independent measurements."
            ),
        },
        "profiles": profiles,
    }
    payload["model_version"] = model_content_version(payload)
    return payload


def model_content_version(payload: dict) -> str:
    """Tamper-evident content version over the numerics the runtime actually consumes."""
    import hashlib

    material = json.dumps(
        [payload["schema"], payload["model_id"],
         [[p["name"], p["scope_contains"], p["mean_ms"], p["sd_ms"]]
          for p in payload["profiles"]]],
        sort_keys=True, separators=(",", ":"),
    )
    return "sha256-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def validate(payload: dict) -> None:
    """Re-assert every bound the three runtime consumers enforce, before we write anything."""
    if payload["schema"] != SCHEMA or payload["model_id"] != MODEL_ID:
        raise SystemExit("schema/model_id drifted from the runtime contract")
    if not payload["model_version"] or len(payload["model_version"]) > 32:
        raise SystemExit("model_version must be non-empty and <= 32 chars")
    if payload["model_version"] != model_content_version(payload):
        raise SystemExit("model_version does not match content: hand-edit detected")
    expected = {
        "capture-card-pipe": ("controller=pipe", "capture_card"),
        "decoder-pipe": ("controller=pipe", "decoder"),
        "capture-card-vigem": ("capture_card",),
        "decoder-vigem": ("decoder",),
    }
    seen = {}
    for profile in payload["profiles"]:
        name = profile["name"]
        if name not in expected:
            raise SystemExit(f"unknown profile name {name!r}: native parses these by suffix")
        if tuple(profile["scope_contains"]) != expected[name]:
            raise SystemExit(f"profile {name!r} scope_contains drifted from the runtime contract")
        if not MEAN_MIN_MS <= profile["mean_ms"] <= MEAN_MAX_MS:
            raise SystemExit(f"profile {name!r} mean_ms out of consumer bounds")
        if not SD_MIN_MS <= profile["sd_ms"] <= SD_MAX_MS:
            raise SystemExit(f"profile {name!r} sd_ms out of consumer bounds")
        seen[name] = profile
    if set(seen) != set(expected):
        raise SystemExit("profile set must cover all four routes")
    # The measured route must be the tightest; every unmeasured route must admit more ignorance.
    base = seen["capture-card-pipe"]["sd_ms"]
    for name in ("decoder-pipe", "capture-card-vigem", "decoder-vigem"):
        if seen[name]["sd_ms"] <= base:
            raise SystemExit(f"{name} must be wider than the one route we actually measured")
    if seen["decoder-vigem"]["sd_ms"] < max(
            seen["decoder-pipe"]["sd_ms"], seen["capture-card-vigem"]["sd_ms"]):
        raise SystemExit("decoder-vigem is the least-known route and must carry the widest sd")


def render(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", action="append", type=Path, default=None,
                        help="log file to mine (repeatable; defaults to logs/orion_native.log*)")
    parser.add_argument("--half-life-hours", type=float, default=DEFAULT_HALF_LIFE_HOURS)
    parser.add_argument("--write", action="store_true", help="write models/latency_factory_prior.json")
    parser.add_argument("--check", action="store_true",
                        help="regenerate in memory and diff against the committed file")
    args = parser.parse_args(argv)

    logs = list(args.log) if args.log else [p for p in DEFAULT_LOGS if p.exists()]
    if not logs:
        raise SystemExit("no log files found; pass --log")

    labels, scopes = collect(logs)
    if not labels:
        raise SystemExit("no accepted latency labels found in the supplied logs")
    video = sorted({label.video_tier for label in labels})
    controller = sorted({label.controller_route for label in labels})
    if len(controller) != 1 or len(video) != 1:
        raise SystemExit(
            "the accepted labels span multiple routes (controller=%s video=%s); a single-route "
            "prior cannot be fit from them -- split the logs by route first"
            % (controller, video))
    evidence_route = f"{video[0]}+{controller[0]}"
    if len(scopes) > 1:
        raise SystemExit(
            f"the logs contain multiple estimator route scopes {sorted(scopes)}; refusing to "
            "pool labels that the estimator itself considers non-interchangeable")

    fit = fit_measured_route(labels, args.half_life_hours)
    payload = build_payload(fit, args.half_life_hours, [p.name for p in logs], evidence_route)
    validate(payload)
    text = render(payload)

    print(f"accepted labels : {fit['n']}  route={evidence_route}  "
          f"scope_digest={sorted(scopes) or ['-']}")
    for label in labels:
        print(f"  {label.when:%Y-%m-%dT%H:%M:%SZ}  total_ms={label.total_ms:<6} "
              f"video={label.video_tier} controller={label.controller_route}")
    for index, group in enumerate(split_regimes(labels), 1):
        values = [l.total_ms for l in group]
        mean = sum(values) / len(values)
        print(f"  regime {index}: n={len(values)} mean={mean:.2f} values={values}")
    print(f"effective n     : {fit['n_eff']:.3f} (half-life {args.half_life_hours:g}h)")
    print(f"predictive sd   : {fit['predictive_sd_ms']:.3f}  "
          f"straddle floor  : {fit['straddle_floor_ms']:.3f}")
    print(f"model_version   : {payload['model_version']}")
    for profile in payload["profiles"]:
        print(f"  {profile['name']:<20} mean={profile['mean_ms']:>6.1f} "
              f"sd={profile['sd_ms']:>5.1f}  evidence_n={profile['evidence_n']}")

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("\nCHECK FAILED: committed file differs from a fresh regeneration", file=sys.stderr)
            return 1
        print("\ncheck ok: committed file is byte-identical to a fresh regeneration")
        return 0

    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        temporary = OUT.with_suffix(".json.tmp")
        # Explicit LF: the artifact is hashed and diffed across platforms, so it must not pick up
        # CRLF from a Windows build host.
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(OUT)
        print(f"\nwrote {OUT}")
        return 0

    print("\n(dry run -- pass --write to publish, --check to verify)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
