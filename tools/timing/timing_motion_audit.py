"""Offline P1 attribution; explicit identities, causal joins, no outcome grading.

python tools/timing/timing_motion_audit.py --out .codex_artifacts/timing-motion
No runtime/environment/settings changes. Missing randomized offsets are not zero
sensitivity; box correlations are associations, not causal camera measurements.
"""
from __future__ import annotations
import argparse
import bisect
import csv
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics as st

KV = re.compile(r"(?<!\w)(\w+)=([^\s]+)")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_log(lines):
    rows, epochs, releases, draws, applied = [], {}, {}, {}, {}
    stats = Counter()
    session, previous_epoch, previous_press = 0, None, None
    issued = {}
    for lineno, line in enumerate(lines, 1):
        if 'Sidecar tail:' in line:
            continue
        try:
            t = datetime.fromisoformat(line[:24].replace('Z', '+00:00')).timestamp()*1000
        except ValueError:
            continue
        d = dict(KV.findall(line))
        if 'Physical shot epoch:' in line:
            ep = d.get('epoch')
            if ep is None:
                continue
            if ep == previous_epoch and t == previous_press:
                stats['duplicate_presses'] += 1
                continue
            if previous_press is None or t-previous_press > 600000 or int(ep) <= int(previous_epoch):
                session += 1
                epochs, releases, draws, applied, issued = {}, {}, {}, {}, {}
            previous_epoch, previous_press = ep, t
            row = dict(session=session, epoch=int(ep), press_ms=t, press_line=lineno,
                       intent=d.get('intent'), release_ms=None, release_seq=None,
                       peak_fill=None, green_start=None, green_end=None, shot_type=None,
                       offset_draw_ms=None, offset_applied_ms=None, abort_reasons=[],
                       stretch_events=[], arm_ms=None, lead_ms=None)
            rows.append(row)
            epochs[ep] = row
        elif 'DEV FIRE OFFSET DRAW:' in line:
            draws[d.get('shot_attempt')] = (t, number(d.get('offset_ms')))
            stats['offset_draw_lines'] += 1
        elif 'DEV FIRE OFFSET APPLIED:' in line:
            applied[d.get('shot_attempt')] = (t, number(d.get('applied_ms')))
        elif 'Release issued:' in line:
            issued[d.get('seq')] = t
        elif 'Release delivery identity:' in line:
            row = epochs.get(d.get('physical_epoch'))
            if row is None or t < row['press_ms'] or row['release_seq'] is not None:
                stats['unjoined_deliveries'] += 1
                continue
            seq = d.get('release_seq')
            command = issued.get(seq, t)
            if not row['press_ms'] <= command <= t:
                command = t
            row.update(release_ms=command, release_seq=seq, release_line=lineno,
                       shot_attempt=d.get('shot_attempt'))
            for source, key in ((draws, 'offset_draw_ms'), (applied, 'offset_applied_ms')):
                draw_t, value = source.get(d.get('shot_attempt'), (0, None))
                if row['press_ms'] <= draw_t <= t:
                    row[key] = value
            releases[seq] = row
        elif 'Release landing:' in line:
            row = releases.get(d.get('seq'))
            if row is None or not 0 <= t-row['release_ms'] <= 6000 or row['peak_fill'] is not None:
                stats['unjoined_landings'] += 1
                continue
            row.update(peak_fill=number(d.get('peak_fill')), green_start=number(d.get('green_start')),
                       green_end=number(d.get('green_end')), fill_at_rel=number(d.get('fill_at_rel')),
                       landing_ms=t, landing_line=lineno, graded=d.get('graded'))
            match = re.search(r' shot=(.*?)\s*$', line)
            row['shot_type'] = match.group(1) if match else 'unknown'
        elif 'TIP PHASE RATE STRETCH:' in line:
            row = epochs.get(d.get('physical_epoch'))
            stats['stretch_lines'] += 1
            if row and row['release_ms'] is None and t >= row['press_ms']:
                row['stretch_events'].append(dict(line=lineno, wall_ms=t, **d))
            else:
                stats['unjoined_stretches'] += 1
        elif 'TIP RESERVATION: disposition=reservation_promoted' in line:
            row = epochs.get(d.get('physical_epoch'))
            if row and row['release_ms'] is None and t >= row['press_ms']:
                row.update(arm_ms=t, lead_ms=number(d.get('lead_ms')), arm_fill=number(d.get('fill_pct')))
        elif 'SHOT NOT OWNED:' in line or 'Shot abort identity:' in line:
            stats['abort_lines'] += 1
            row = epochs.get(d.get('physical_epoch'))
            if row and t >= row['press_ms']:
                reason = d.get('reason', 'unknown')
                if reason not in row['abort_reasons']:
                    row['abort_reasons'].append(reason)
            else:
                stats['unjoined_aborts'] += 1
    return rows, dict(stats)


def regression(pairs):
    pairs = [(number(x), number(y)) for x, y in pairs]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    result = dict(n=len(pairs), r=None, slope=None, slope_95ci=None)
    if len(pairs) < 3:
        return result
    xs, ys = zip(*pairs)
    xm, ym = st.mean(xs), st.mean(ys)
    xx, yy = sum((x-xm)**2 for x in xs), sum((y-ym)**2 for y in ys)
    if not xx or not yy:
        return result
    xy = sum((x-xm)*(y-ym) for x, y in pairs)
    slope = xy/xx
    se = math.sqrt(sum((y-ym-slope*(x-xm))**2 for x, y in pairs)/(len(pairs)-2)/xx)
    result.update(r=xy/math.sqrt(xx*yy), slope=slope,
                  slope_95ci=[slope-1.96*se, slope+1.96*se])
    return result


def motion_features(frames, command_ms):
    empty = dict(motion_n=0)
    valid = []
    for f in frames:
        values = [number(f.get(k)) for k in ('wall_ms', 'fill_pct', 'x', 'y', 'w', 'h')]
        if any(v is None for v in values) or str(f.get('detected')) != '1' or str(f.get('fed', '1')) != '1':
            continue
        t, fill, x, y, w, h = values
        if t <= command_ms and w > 0 and h > 0:
            valid.append((t, fill, x+w/2, y+h/2, w, h, f))
    valid.sort(key=lambda x: x[0])
    anchor = None
    for a, b in zip(valid, valid[1:]):
        if a[1] < 20 <= b[1] and 0 < b[0]-a[0] <= 100:
            anchor = a[0] + (20-a[1])/(b[1]-a[1])*(b[0]-a[0])
            break
    if anchor is None:
        return empty
    use = [f for f in valid if anchor <= f[0] <= command_ms]
    if len(use) < 3 or command_ms-use[-1][0] > 100 or any(b[0]-a[0] > 100 for a,b in zip(use,use[1:])):
        return empty
    a, b = use[0], use[-1]
    duration = (b[0]-a[0])/1000
    if duration <= 0:
        return empty
    speed = sum(math.hypot(b[2]-a[2], b[3]-a[3]) for a,b in zip(use,use[1:]))/duration
    gens = [f[6].get('fill_estimator_generation') for f in use]
    modes = sorted({f[6]['fill_estimator_mode'] for f in use if f[6].get('fill_estimator_mode')})
    kinds = sorted({f[6]['ruler_kind'] for f in use if f[6].get('ruler_kind')})
    return dict(motion_n=len(use), anchor20_ms=anchor, anchor_to_fire_ms=command_ms-anchor,
                center_speed_px_s=speed, center_vx_px_s=(b[2]-a[2])/duration,
                center_vy_px_s=(b[3]-a[3])/duration, width_change_pct=100*(b[4]/a[4]-1),
                height_change_pct=100*(b[5]/a[5]-1),
                estimator_changes=sum(a != b for a,b in zip(gens,gens[1:])) if all(g is not None for g in gens) else None,
                estimator_modes=modes, ruler_kinds=kinds)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log', type=Path, default=Path('logs/orion_native.log'))
    ap.add_argument('--detdir', type=Path, default=Path('logs/diagnostics'))
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    raw = args.log.read_bytes()
    rows, stats = parse_log(raw.decode('utf-8', errors='replace').splitlines())
    hashes = {str(args.log): hashlib.sha256(raw).hexdigest()}
    frames = {}
    for path in sorted(args.detdir.glob('detframes*.csv')):
        data = path.read_bytes()
        hashes[str(path)] = hashlib.sha256(data).hexdigest()
        for f in csv.DictReader(data.decode('utf-8-sig', errors='replace').splitlines()):
            t = number(f.get('wall_ms'))
            if t is not None:
                # Exact repeated capture timestamps across rotated files count once.
                if t not in frames:
                    frames[t] = f
                elif frames[t] != f:
                    frames[t] = None  # conflicting instruments never silently overwrite
    times = sorted(t for t, f in frames.items() if f is not None)
    for row in rows:
        if row['release_ms'] is not None:
            start, end = row['press_ms'], row['release_ms']
            window = [frames[t] for t in times[bisect.bisect_left(times,start):bisect.bisect_right(times,end)]]
            window = [f for f in window if f.get('sample_shot_epoch', '0') in ('0', '', str(row['epoch']))]
            row.update(motion_features(window, end))
    landed = [r for r in rows if r['peak_fill'] is not None]
    strata = defaultdict(list)
    for row in landed:
        strata[(row['session'], row['shot_type'], row['lead_ms'])].append(row)
    for group in strata.values():
        center = st.median(r['peak_fill'] for r in group)
        for r in group:
            r['landing_deviation_pp'] = r['peak_fill']-center
    features = ['center_speed_px_s', 'center_vx_px_s', 'center_vy_px_s', 'width_change_pct',
                'height_change_pct', 'estimator_changes', 'anchor_to_fire_ms', 'fill_at_rel']
    correlations = {k: regression([(r.get(k), r['landing_deviation_pp']) for r in landed]) for k in features}
    per_session = []
    for session in sorted({r['session'] for r in rows}):
        group = [r for r in landed if r['session'] == session]
        peaks = [r['peak_fill'] for r in group]
        med = st.median(peaks) if peaks else None
        per_session.append(dict(session=session, n=len(group), peak_median=med,
            peak_rmad=1.4826*st.median(abs(p-med) for p in peaks) if peaks else None,
            first_press_ms=min(r['press_ms'] for r in rows if r['session']==session),
            correlations={k:regression([(r.get(k),r['landing_deviation_pp']) for r in group]) for k in features},
            stretched=[dict(epoch=r['epoch'],peak=r['peak_fill'],deviation=r['landing_deviation_pp'],events=r['stretch_events']) for r in group if r['stretch_events']]))
    sweeps = [r for r in landed if r['offset_draw_ms'] is not None and r['offset_applied_ms'] is not None]
    summary = dict(stats=stats, presses=len(rows), landings=len(landed),
        unique_abort_presses=sum(bool(r['abort_reasons']) for r in rows),
        motion_joined=sum(r.get('motion_n',0)>0 for r in rows),
        sweep_draw_regression=regression([(r['offset_draw_ms'],r['peak_fill']) for r in sweeps]),
        sweep_applied_regression=regression([(r['offset_applied_ms'],r['peak_fill']) for r in sweeps]),
        correlations=correlations, sessions=per_session,
        ruler_kind_observed=any(r.get('ruler_kinds') for r in rows),
        limitations=['Human banners are not in these inputs; no outcome success rate is inferred.',
            'Session boundaries use physical epoch resets or >600s press gaps; inspect line IDs.',
            'Anchor is observed 20% crossing, not an extrapolated acquisition or engine clock stamp.',
            'Release time is local issued/delivery time, not console receipt.',
            'Correlations are exploratory; session/type/lead median removes outcome offsets only.',
            'OLS intervals are approximate, not a randomized causal result.',
            'Missing sweep evidence does not establish quantization; no lead/curve trim selected.',
            'Ruler kind absent from CSV stays unknown; estimator mode is not a ruler kind.'])
    args.out.mkdir(parents=True, exist_ok=True)
    for name,payload in [('SUMMARY.json',summary),('SHOTS.json',rows),('INPUT_HASHES.json',hashes)]:
        path=args.out/name
        path.write_text(json.dumps(payload,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        json.loads(path.read_text(encoding='utf-8'))
    print(json.dumps({k:v for k,v in summary.items() if k not in ('sessions','limitations')},indent=2))
    print('TIMING MOTION AUDIT: completed; runtime unchanged; human outcomes ungraded')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
