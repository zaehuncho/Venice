"""Fold release outcomes against the console and capture sampling grids.

The tool is read-only on its inputs. It joins successful ``Release submit``
records carrying ``fire_epoch_ms`` to their graded ``Release landing`` record,
projects AV-clock QPC samples onto the log wall clock, removes per-session and
per-shot-type landing bias, then fits both sinusoidal and discontinuous
sawtooth phase models.

Typical usage::

    python tools/timing/poll_phase_fold.py \
        --log logs/orion_native.log \
        --avclock logs/diagnostics/avclock_20260902_*.csv \
        --detframes logs/diagnostics/detframes_20260902_*.csv

On Windows, quote wildcard arguments; this script expands them itself. When no
AV-clock path is supplied it reads ``logs/diagnostics/avclock_*.csv``. Capture
comparison is optional and is enabled only when ``--detframes`` is supplied.

The permutation test shuffles residuals within session/shot-type strata. This
preserves each animation's outcome distribution while breaking only its phase
relationship. The sawtooth p-value includes the cliff-position grid search.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import glob
import math
import re
import statistics
import sys
import zlib
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np


DEFAULT_PERIOD_MS = 16.667
DEFAULT_PERMUTATIONS = 4000
_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+(?P<body>.*)$")
_KV = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s]+)")
_SHOT = re.compile(r"\bshot=(?P<shot>.+?)\s*$")


@dataclass(frozen=True)
class Release:
    seq: int
    fire_epoch_ms: float
    submit_log_ms: float
    landing_log_ms: float
    raw_peak_fill: float
    shot: str
    physical_epoch: int | None
    log_session: int


@dataclass(frozen=True)
class GridSeries:
    name: str
    path: Path
    wall_ms: np.ndarray
    frame_index: np.ndarray | None = None


@dataclass(frozen=True)
class FoldedRelease:
    release: Release
    session: str
    residual_pp: float
    console_phase_ms: float
    console_frame_index: int
    console_arrival_ms: float
    capture_phase_ms: float | None
    capture_arrival_ms: float | None


@dataclass(frozen=True)
class ModelFit:
    n: int
    sine_amplitude_pp: float
    sine_peak_ms: float
    sine_r2: float
    sine_p: float
    saw_height_pp: float
    saw_cliff_ms: float
    saw_r2: float
    saw_p: float


@dataclass(frozen=True)
class ParseStats:
    successful_submits: int = 0
    stamped_submits: int = 0
    graded_landings: int = 0
    joined: int = 0


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _fields(body: str) -> dict[str, str]:
    return {match.group("key"): match.group("value") for match in _KV.finditer(body)}


def _timestamp_ms(text: str) -> float:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000.0


def parse_releases(paths: Sequence[Path], max_landing_delay_ms: float = 10_000.0) -> tuple[list[Release], ParseStats]:
    """Join stamped successful submits to graded raw-peak landing rows."""
    pending: dict[int, list[dict[str, object]]] = defaultdict(list)
    releases: list[Release] = []
    current_epoch: int | None = None
    previous_epoch: int | None = None
    previous_epoch_ms: float | None = None
    log_session = 1
    stats = ParseStats()

    for path in paths:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                match = _TS.match(line)
                if not match:
                    continue
                body = match.group("body")
                event_ms = _timestamp_ms(match.group("ts"))

                if body.startswith("Physical shot epoch:"):
                    epoch = _integer(_fields(body).get("epoch"))
                    if epoch is not None:
                        if (previous_epoch is not None and epoch <= previous_epoch
                                and previous_epoch_ms is not None
                                and event_ms - previous_epoch_ms > 1000.0):
                            log_session += 1
                        current_epoch = epoch
                        previous_epoch = epoch
                        previous_epoch_ms = event_ms
                    continue

                if body.startswith("Release submit:"):
                    fields = _fields(body)
                    if fields.get("ok") != "1":
                        continue
                    stats = replace(stats, successful_submits=stats.successful_submits + 1)
                    seq = _integer(fields.get("seq"))
                    fire_epoch_ms = _finite(fields.get("fire_epoch_ms"))
                    if seq is None or fire_epoch_ms is None or fire_epoch_ms <= 0.0:
                        continue
                    stats = replace(stats, stamped_submits=stats.stamped_submits + 1)
                    pending[seq].append({
                        "seq": seq,
                        "fire_epoch_ms": fire_epoch_ms,
                        "submit_log_ms": event_ms,
                        "physical_epoch": current_epoch,
                        "log_session": log_session,
                    })
                    continue

                if body.startswith("Release delivery identity:"):
                    fields = _fields(body)
                    seq = _integer(fields.get("release_seq"))
                    epoch = _integer(fields.get("physical_epoch"))
                    if seq is not None and epoch is not None and pending.get(seq):
                        pending[seq][-1]["physical_epoch"] = epoch
                    continue

                if not body.startswith("Release landing:"):
                    continue
                fields = _fields(body)
                if fields.get("graded") != "1":
                    continue
                stats = replace(stats, graded_landings=stats.graded_landings + 1)
                seq = _integer(fields.get("seq"))
                raw_peak = _finite(fields.get("raw_peak_fill"))
                shot_match = _SHOT.search(body)
                if seq is None or raw_peak is None or not shot_match:
                    continue

                candidates = pending.get(seq, [])
                candidate_index = None
                for index in range(len(candidates) - 1, -1, -1):
                    delay = event_ms - float(candidates[index]["submit_log_ms"])
                    if 0.0 <= delay <= max_landing_delay_ms:
                        candidate_index = index
                        break
                if candidate_index is None:
                    continue
                submit = candidates.pop(candidate_index)
                releases.append(Release(
                    seq=seq,
                    fire_epoch_ms=float(submit["fire_epoch_ms"]),
                    submit_log_ms=float(submit["submit_log_ms"]),
                    landing_log_ms=event_ms,
                    raw_peak_fill=raw_peak,
                    shot=shot_match.group("shot").strip(),
                    physical_epoch=submit["physical_epoch"],
                    log_session=int(submit["log_session"]),
                ))
                stats = replace(stats, joined=stats.joined + 1)

    releases.sort(key=lambda release: release.fire_epoch_ms)
    return releases, stats


def _expanded_paths(patterns: Sequence[str], default_pattern: str | None = None) -> list[Path]:
    requested = list(patterns)
    if not requested and default_pattern:
        requested = [default_pattern]
    expanded: list[Path] = []
    for pattern in requested:
        matches = sorted(glob.glob(pattern)) if glob.has_magic(pattern) else [pattern]
        expanded.extend(Path(match).resolve() for match in matches if Path(match).is_file())
    return list(dict.fromkeys(expanded))


def load_avclock(path: Path) -> GridSeries:
    times: list[float] = []
    frames: list[int] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            qpc = _finite(row.get("arrival_qpc_us"))
            sync_qpc = _finite(row.get("sync_qpc_us"))
            sync_wall = _finite(row.get("sync_wall_ms"))
            wall = _finite(row.get("wall_ms"))
            frame = _integer(row.get("frame_index"))
            if qpc is not None and sync_qpc is not None and sync_wall is not None:
                wall = sync_wall + (qpc - sync_qpc) / 1000.0
            if wall is None or frame is None:
                continue
            times.append(wall)
            frames.append(frame & 0xFFFF)
    if not times:
        return GridSeries(path.stem, path, np.empty(0, dtype=float), np.empty(0, dtype=np.int64))
    order = np.argsort(np.asarray(times), kind="stable")
    return GridSeries(
        path.stem,
        path,
        np.asarray(times, dtype=float)[order],
        np.asarray(frames, dtype=np.int64)[order],
    )


def load_capture_grid(path: Path) -> GridSeries:
    times: list[float] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            wall = _finite(row.get("wall_ms"))
            if wall is not None:
                times.append(wall)
    values = np.unique(np.asarray(times, dtype=float)) if times else np.empty(0, dtype=float)
    return GridSeries(path.stem, path, values)


def _nearest_preceding(grids: Sequence[GridSeries], event_ms: float,
                       max_gap_ms: float) -> tuple[GridSeries, int, float] | None:
    best: tuple[GridSeries, int, float] | None = None
    best_age = math.inf
    for grid in grids:
        if grid.wall_ms.size == 0:
            continue
        index = bisect.bisect_right(grid.wall_ms, event_ms) - 1
        if index < 0:
            continue
        arrival = float(grid.wall_ms[index])
        age = event_ms - arrival
        if -1e-6 <= age <= max_gap_ms and age < best_age:
            best = grid, index, arrival
            best_age = age
    return best


def fold_releases(releases: Sequence[Release], console_grids: Sequence[GridSeries],
                  capture_grids: Sequence[GridSeries], period_ms: float,
                  max_gap_ms: float) -> tuple[list[FoldedRelease], int]:
    joined: list[tuple[Release, str, float, int, float, float | None, float | None]] = []
    missing_console = 0
    for release in releases:
        console = _nearest_preceding(console_grids, release.fire_epoch_ms, max_gap_ms)
        if console is None:
            missing_console += 1
            continue
        console_grid, console_index, console_arrival = console
        frame_index = int(console_grid.frame_index[console_index]) if console_grid.frame_index is not None else -1
        console_phase = (release.fire_epoch_ms - console_arrival) % period_ms

        capture_phase = None
        capture_arrival = None
        capture = _nearest_preceding(capture_grids, release.fire_epoch_ms, max_gap_ms)
        if capture is not None:
            _, _, capture_arrival = capture
            capture_phase = (release.fire_epoch_ms - capture_arrival) % period_ms
        joined.append((release, console_grid.name, console_phase, frame_index,
                       console_arrival, capture_phase, capture_arrival))

    medians: dict[tuple[str, str], float] = {}
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for release, session, *_ in joined:
        grouped[(session, release.shot)].append(release.raw_peak_fill)
    for key, values in grouped.items():
        medians[key] = statistics.median(values)

    folded = [
        FoldedRelease(
            release=release,
            session=session,
            residual_pp=release.raw_peak_fill - medians[(session, release.shot)],
            console_phase_ms=console_phase,
            console_frame_index=frame_index,
            console_arrival_ms=console_arrival,
            capture_phase_ms=capture_phase,
            capture_arrival_ms=capture_arrival,
        )
        for release, session, console_phase, frame_index, console_arrival,
        capture_phase, capture_arrival in joined
    ]
    return folded, missing_console


def _empty_fit(n: int) -> ModelFit:
    nan = float("nan")
    return ModelFit(n, nan, nan, nan, nan, nan, nan, nan, nan)


def fit_models(phases_ms: Sequence[float], residuals_pp: Sequence[float],
               strata: Sequence[str], period_ms: float = DEFAULT_PERIOD_MS,
               permutations: int = DEFAULT_PERMUTATIONS, seed: int = 4080,
               cliff_step_ms: float = 0.05, min_n: int = 8) -> ModelFit:
    """Fit sine and searched-cliff sawtooth models with stratified permutations."""
    x = np.asarray(phases_ms, dtype=float)
    y = np.asarray(residuals_pp, dtype=float)
    labels = np.asarray(strata, dtype=object)
    valid = np.isfinite(x) & np.isfinite(y)
    x, y, labels = x[valid] % period_ms, y[valid], labels[valid]
    n = int(x.size)
    if n < min_n:
        return _empty_fit(n)

    y_centered = y - y.mean()
    total_ss = float(y_centered @ y_centered)
    if total_ss <= np.finfo(float).eps:
        return ModelFit(n, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    omega = 2.0 * math.pi / period_ms
    sine_raw = np.column_stack((np.sin(omega * x), np.cos(omega * x)))
    sine_design = sine_raw - sine_raw.mean(axis=0)
    sine_gram_inv = np.linalg.pinv(sine_design.T @ sine_design)
    sine_cross = sine_design.T @ y_centered
    sine_coef = sine_gram_inv @ sine_cross
    sine_explained = float(sine_cross.T @ sine_gram_inv @ sine_cross)
    sine_r2 = max(0.0, min(1.0, sine_explained / total_ss))
    sine_amplitude = float(np.hypot(sine_coef[0], sine_coef[1]))
    sine_peak = (math.atan2(float(sine_coef[0]), float(sine_coef[1])) / omega) % period_ms

    cliffs = np.arange(0.0, period_ms, cliff_step_ms, dtype=float)
    saw_raw = ((x[:, None] - cliffs[None, :]) % period_ms) / period_ms - 0.5
    saw_design = saw_raw - saw_raw.mean(axis=0)
    saw_denom = np.einsum("ij,ij->j", saw_design, saw_design)
    usable = saw_denom > np.finfo(float).eps
    saw_cross = y_centered @ saw_design
    saw_explained_all = np.zeros_like(saw_denom)
    saw_explained_all[usable] = saw_cross[usable] ** 2 / saw_denom[usable]
    best = int(np.argmax(saw_explained_all))
    saw_height = float(saw_cross[best] / saw_denom[best]) if usable[best] else 0.0
    saw_explained = float(saw_explained_all[best])
    saw_r2 = max(0.0, min(1.0, saw_explained / total_ss))
    saw_cliff = float(cliffs[best])

    if permutations <= 0:
        return ModelFit(n, sine_amplitude, sine_peak, sine_r2, float("nan"),
                        saw_height, saw_cliff, saw_r2, float("nan"))

    rng = np.random.default_rng(seed)
    strata_indices = [np.flatnonzero(labels == label) for label in np.unique(labels)]
    sine_extreme = 0
    saw_extreme = 0
    completed = 0
    batch_size = min(256, permutations)
    tolerance = 1e-12
    while completed < permutations:
        count = min(batch_size, permutations - completed)
        shuffled = np.broadcast_to(y, (count, n)).copy()
        for indices in strata_indices:
            if indices.size < 2:
                continue
            order = np.argsort(rng.random((count, indices.size)), axis=1)
            shuffled[:, indices] = y[indices][order]
        shuffled -= shuffled.mean(axis=1, keepdims=True)

        sine_perm_cross = shuffled @ sine_design
        sine_perm_explained = np.einsum(
            "bi,ij,bj->b", sine_perm_cross, sine_gram_inv, sine_perm_cross)
        saw_perm_cross = shuffled @ saw_design
        saw_perm_explained = np.max(
            np.divide(saw_perm_cross ** 2, saw_denom,
                      out=np.zeros_like(saw_perm_cross), where=usable), axis=1)
        sine_extreme += int(np.count_nonzero(sine_perm_explained >= sine_explained - tolerance))
        saw_extreme += int(np.count_nonzero(saw_perm_explained >= saw_explained - tolerance))
        completed += count

    sine_p = (sine_extreme + 1.0) / (permutations + 1.0)
    saw_p = (saw_extreme + 1.0) / (permutations + 1.0)
    return ModelFit(n, sine_amplitude, sine_peak, sine_r2, sine_p,
                    saw_height, saw_cliff, saw_r2, saw_p)


def _circular_span(values: Iterable[float], period_ms: float) -> float:
    points = sorted(value % period_ms for value in values if math.isfinite(value))
    if len(points) <= 1:
        return 0.0
    gaps = [points[index + 1] - points[index] for index in range(len(points) - 1)]
    gaps.append(points[0] + period_ms - points[-1])
    return period_ms - max(gaps)


def verdict_line(pooled: ModelFit, session_fits: Sequence[ModelFit],
                 period_ms: float = DEFAULT_PERIOD_MS, alpha: float = 0.05,
                 max_cliff_span_ms: float = 3.0) -> str:
    valid_cliffs = [fit.saw_cliff_ms for fit in session_fits
                    if fit.n >= 8 and math.isfinite(fit.saw_cliff_ms)]
    agreement = _circular_span(valid_cliffs, period_ms)
    detected = (math.isfinite(pooled.saw_p) and pooled.saw_p <= alpha
                and agreement <= max_cliff_span_ms)
    state = "CONFIRMED" if detected else "NOT DETECTED"
    amplitude = abs(pooled.saw_height_pp)
    return (f"poll grid: {state}, amplitude {amplitude:.3f} pp, "
            f"cliff at {pooled.saw_cliff_ms:.3f} ms, pooled p={pooled.saw_p:.6f}, "
            f"sessions agree within {agreement:.3f} ms")


def _fit_subset(records: Sequence[FoldedRelease], phase_attr: str,
                period_ms: float, permutations: int, seed: int,
                pooled: bool) -> ModelFit:
    usable = [record for record in records
              if getattr(record, phase_attr) is not None and abs(record.residual_pp) <= 6.0]
    phases = [float(getattr(record, phase_attr)) for record in usable]
    residuals = [record.residual_pp for record in usable]
    strata = [f"{record.session}|{record.release.shot}" if pooled else record.release.shot
              for record in usable]
    return fit_models(phases, residuals, strata, period_ms, permutations, seed)


def _fmt_model(label: str, fit: ModelFit) -> str:
    return (f"{label:<8} n={fit.n:>3}  "
            f"sine amp={fit.sine_amplitude_pp:7.3f}pp peak={fit.sine_peak_ms:6.3f}ms "
            f"R2={fit.sine_r2:6.3f} p={fit.sine_p:8.6f}  "
            f"saw height={fit.saw_height_pp:+7.3f}pp cliff={fit.saw_cliff_ms:6.3f}ms "
            f"R2={fit.saw_r2:6.3f} p={fit.saw_p:8.6f}")


def avclock_quality(grid: GridSeries, window_seconds: float = 10.0) -> tuple[int, float, float]:
    """Return rows, fitted frame period, and scaled residual MAD near session center."""
    if grid.frame_index is None or grid.wall_ms.size < 3:
        return int(grid.wall_ms.size), float("nan"), float("nan")
    window_ms = window_seconds * 1000.0
    center = float(grid.wall_ms[grid.wall_ms.size // 2])
    start = max(float(grid.wall_ms[0]), center - window_ms / 2.0)
    end = min(float(grid.wall_ms[-1]), start + window_ms)
    start = max(float(grid.wall_ms[0]), end - window_ms)
    begin = int(np.searchsorted(grid.wall_ms, start, side="left"))
    finish = int(np.searchsorted(grid.wall_ms, end, side="right"))
    frames = grid.frame_index[begin:finish]
    wall_ms = grid.wall_ms[begin:finish]
    count = int(wall_ms.size)
    if count < 3:
        return count, float("nan"), float("nan")

    unwrapped = np.empty(count, dtype=float)
    unwrapped[0] = float(frames[0])
    for index in range(1, count):
        delta = (int(frames[index]) - int(frames[index - 1])) & 0xFFFF
        if delta > 0x7FFF:
            delta -= 0x10000
        unwrapped[index] = unwrapped[index - 1] + delta
    frame_offsets = unwrapped - unwrapped[0]
    relative_wall = wall_ms - wall_ms[0]
    design = np.column_stack((np.ones(count), frame_offsets))
    intercept, period = np.linalg.lstsq(design, relative_wall, rcond=None)[0]
    residual = relative_wall - (intercept + period * frame_offsets)
    center = float(np.median(residual))
    rmad = 1.4826 * float(np.median(np.abs(residual - center)))
    return count, float(period), rmad


def _stable_seed(base: int, label: str) -> int:
    return (base + zlib.crc32(label.encode("utf-8"))) & 0xFFFFFFFF


def run_analysis(log_paths: Sequence[Path], avclock_paths: Sequence[Path],
                 detframe_paths: Sequence[Path], period_ms: float,
                 permutations: int, seed: int, max_gap_ms: float,
                 alpha: float, max_cliff_span_ms: float) -> int:
    releases, parse_stats = parse_releases(log_paths)
    console_grids = [load_avclock(path) for path in avclock_paths]
    capture_grids = [load_capture_grid(path) for path in detframe_paths]
    folded, missing_console = fold_releases(
        releases, console_grids, capture_grids, period_ms, max_gap_ms)
    kept = [record for record in folded if abs(record.residual_pp) <= 6.0]

    print("AV CLOCK QUALITY (centered 10-second window)")
    for grid in console_grids:
        count, fitted_period, rmad = avclock_quality(grid)
        print(f"  {grid.path.name}: rows={grid.wall_ms.size} fit_n={count} "
              f"period={fitted_period:.6f}ms residual_rMAD={rmad:.6f}ms")
    print()
    print(f"JOIN: successful_submits={parse_stats.successful_submits} "
          f"stamped={parse_stats.stamped_submits} graded_landings={parse_stats.graded_landings} "
          f"joined={parse_stats.joined} console_matched={len(folded)} "
          f"missing_console={missing_console} discrete_failures_dropped={len(folded) - len(kept)}")

    sessions = sorted({record.session for record in kept})
    console_session_fits: list[ModelFit] = []
    print("\nPER SESSION")
    for session in sessions:
        records = [record for record in kept if record.session == session]
        console_fit = _fit_subset(
            records, "console_phase_ms", period_ms, permutations,
            _stable_seed(seed, f"console:{session}"), pooled=False)
        console_session_fits.append(console_fit)
        print(f"  [{session}]")
        print("    " + _fmt_model("console", console_fit))
        if capture_grids:
            capture_fit = _fit_subset(
                records, "capture_phase_ms", period_ms, permutations,
                _stable_seed(seed, f"capture:{session}"), pooled=False)
            print("    " + _fmt_model("capture", capture_fit))

    pooled_console = _fit_subset(
        kept, "console_phase_ms", period_ms, permutations,
        _stable_seed(seed, "console:pooled"), pooled=True)
    print("\nPOOLED")
    print("  " + _fmt_model("console", pooled_console))
    if capture_grids:
        pooled_capture = _fit_subset(
            kept, "capture_phase_ms", period_ms, permutations,
            _stable_seed(seed, "capture:pooled"), pooled=True)
        print("  " + _fmt_model("capture", pooled_capture))
    print()
    print(verdict_line(pooled_console, console_session_fits, period_ms,
                       alpha, max_cliff_span_ms))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", action="append", default=[], metavar="PATH",
                        help="Orion log path; repeat for rotated logs")
    parser.add_argument("--avclock", action="append", default=[], metavar="PATH_OR_GLOB",
                        help="AV-clock CSV or quoted glob; defaults to logs/diagnostics/avclock_*.csv")
    parser.add_argument("--detframes", action="append", default=[], metavar="PATH_OR_GLOB",
                        help="optional detframes CSV or quoted glob for capture-grid comparison")
    parser.add_argument("--period-ms", type=float, default=DEFAULT_PERIOD_MS)
    parser.add_argument("--permutations", type=int, default=DEFAULT_PERMUTATIONS)
    parser.add_argument("--seed", type=int, default=4080)
    parser.add_argument("--max-grid-gap-ms", type=float, default=100.0)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--max-cliff-span-ms", type=float, default=3.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.permutations < DEFAULT_PERMUTATIONS:
        parser.error(f"--permutations must be at least {DEFAULT_PERMUTATIONS}")
    if args.period_ms <= 0.0 or args.max_grid_gap_ms <= 0.0:
        parser.error("period and grid-gap values must be positive")

    root = Path(__file__).resolve().parents[2]
    log_paths = _expanded_paths(args.log or [str(root / "logs" / "orion_native.log")])
    avclock_paths = _expanded_paths(
        args.avclock, str(root / "logs" / "diagnostics" / "avclock_*.csv"))
    detframe_paths = _expanded_paths(args.detframes)
    if not log_paths:
        parser.error("no log files matched")
    if not avclock_paths:
        parser.error("no AV-clock CSV files matched")

    return run_analysis(log_paths, avclock_paths, detframe_paths,
                        args.period_ms, args.permutations, args.seed,
                        args.max_grid_gap_ms, args.alpha,
                        args.max_cliff_span_ms)


if __name__ == "__main__":
    sys.exit(main())
