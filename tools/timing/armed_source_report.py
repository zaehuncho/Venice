"""Attribute each shot outcome to the predictor that ARMED it, and join a hand count.

    python tools/timing/armed_source_report.py "<orion_native.log>" [options]

WHY. There is no trustworthy in-app timing verdict (the frozen self-grader
false-LATEs at the dead top; the banner oracle is retired), so the only valid
ground truth is the game's own TIMING banner counted by hand. This tool makes
that hand count JOINABLE: for every released shot it reports WHICH predictor's
decision armed the fired token (phase / sampler / registration+sampler_far /
...) and how uncertain that decision was, so a 50-shot batch splits per source
and the "does the arming source dominate the scatter?" hypothesis becomes
falsifiable instead of argued.

WHAT IT READS (append-only key=value lines; unknown fields are ignored):
  * "Outcome identity:"  -- per-shot outcome. NEW logs (2026-08-08+) carry
    armed_source= / armed_sigma_ms= / armed_fill_pct= / command_eta_ms= /
    armed_schedule_token= stamped from the ACTUAL armed token
    ([ORION_ARMED_SOURCE], AutomationEngine::emitShotOutcome). The verdict= on
    this line is the FROZEN SELF-GRADER's and is NOT timing truth
    (grader_truth=0 / absent) -- it is reported only as "grader says", never
    as a miss rate.
  * "TIP RESERVATION: disposition=reservation_promoted" -- the first arm of
    each shot's token. On OLD logs (no armed_* fields) outcomes are attributed
    through this line by (physical_epoch, shot_attempt); that attribution can
    be wrong when the token was torn down and re-armed from a different source
    after promotion (the promotion line logs only the FIRST arm), and is
    labeled "via promotion join" in the output.
  * "TIP DEADLINE DECISION: disposition=rejected_missed" -- shots that never
    released (aborts). Counted separately so a hand count of RELEASED shots
    aligns, but reported per source: an unschedulable/missed shot is part of
    the consistency budget too.
  * "Tip timing set to" / "Shot lead set to" -- config-drag markers, printed
    so a window can be chosen where the configuration was sane.

SESSIONS. The log concatenates app runs; shot_attempt / schedule_token restart
at 1. A new session starts whenever the promotion schedule_token or the
outcome release_seq goes backwards. Filter with --session N (1-based) and/or
--window HH:MM:SS-HH:MM:SS (matched against the log's own UTC timestamps).

HAND-COUNT INPUT (--hand / --hand-file). Type the banner verdicts IN SHOT
ORDER for the released shots of the selected window, easiest form wins:

    --hand GGLGEG            one letter per shot: G=green/good, E=early,
                             L=late, X=skip (a shot you did not count)
    --hand "G G L G E G"     same, separated by spaces/commas/newlines
    --hand "good good late green early skip"   full words work too

Run --list first: it prints the released shots in order (time, source, sigma)
so you can align your count, then re-run with --hand. If your count length
does not match the window's released-shot count the tool says so and shows
the alignment table instead of guessing.

CONTAMINATED-LEAD FILTER (--max-lead-ms). The 2026-08-08 session dragged
Shot Lead to 300-320 ms, past the entire tip runway: nearly every attempt
died as reservation_disposition=unschedulable_lead (lateness_ms ==
lead_ms - tip_eta_ms exactly), and the shots that DID release in that regime
released only because their meters happened to be slow. Rows from such a
regime say nothing about predictor consistency. --max-lead-ms N discards
released shots whose governing lead exceeds N (the joined promotion's
lead_ms when attributed, else the last "Shot lead set to" config mark in
force; a shot with no known lead is kept) and prints exactly how many rows
were discarded, per session.

CAVEATS (do not read past them):
  * per-source miss rates come ONLY from --hand; without it the tool prints
    sigma structure and the grader's untrusted verdicts, nothing more.
  * a promoted-then-aborted shot shows in rejected_missed, not in the hand
    join -- the banner never appeared for it.
  * n per source is small in any 50-shot batch; the tool prints a 95% CI
    (Wilson) next to every rate so a 3/7 does not masquerade as 43%.
"""
import argparse
import math
import re
import statistics
import sys

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})\.(\d{3})Z\s+(.*)$")
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_+]*)=([^\s]+)")

PROMOTED = "TIP RESERVATION: disposition=reservation_promoted"
OUTCOME = "Outcome identity:"
MISSED = "TIP DEADLINE DECISION: disposition=rejected_missed"
TIP_SET = "Tip timing set to"
LEAD_SET = "Shot lead set to"

GOOD_WORDS = {"g", "good", "green", "excellent", "ok"}
EARLY_WORDS = {"e", "early"}
LATE_WORDS = {"l", "late"}
SKIP_WORDS = {"x", "s", "skip", "-", "?"}


def parse_kv(text):
    return {k: v for k, v in KV_RE.findall(text)}


def fnum(d, key, default=None):
    try:
        return float(d[key])
    except (KeyError, ValueError):
        return default


LEAD_SET_RE = re.compile(r"Shot lead set to (\d+(?:\.\d+)?) ms")


def load(path):
    """One pass over the log. Returns (events, config_marks). Binary-safe.

    Every event is stamped with cfg_lead = the last "Shot lead set to" value
    seen so far (None before the first mark), so an UNATTRIBUTED release can
    still be regime-classified by the configuration in force when it fired.
    """
    events = []
    config_marks = []
    cfg_lead = None
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").rstrip()
            m = TS_RE.match(line)
            if not m:
                continue
            date, hms, ms, body = m.groups()
            ts = "%sT%s.%s" % (date, hms, ms)
            if body.startswith(PROMOTED):
                d = parse_kv(body)
                events.append(dict(
                    kind="promoted", ts=ts, hms=hms,
                    source=d.get("source", "?"),
                    sigma=fnum(d, "predictor_sigma_ms"),
                    eta=fnum(d, "command_eta_ms"),
                    fill=fnum(d, "fill_pct"),
                    lead=fnum(d, "lead_ms"),
                    epoch=int(fnum(d, "physical_epoch", -1)),
                    attempt=int(fnum(d, "shot_attempt", -1)),
                    token=int(fnum(d, "schedule_token", -1)),
                    cfg_lead=cfg_lead))
            elif body.startswith(OUTCOME):
                d = parse_kv(body)
                events.append(dict(
                    kind="outcome", ts=ts, hms=hms,
                    verdict=d.get("verdict", "?"),
                    grader_truth=d.get("grader_truth"),  # None on pre-field logs
                    epoch=int(fnum(d, "physical_epoch", -1)),
                    attempt=int(fnum(d, "shot_attempt", -1)),
                    seq=int(fnum(d, "release_seq", -1)),
                    armed_source=d.get("armed_source"),  # None on pre-field logs
                    armed_sigma=fnum(d, "armed_sigma_ms"),
                    armed_fill=fnum(d, "armed_fill_pct"),
                    armed_eta=fnum(d, "command_eta_ms"),
                    armed_token=int(fnum(d, "armed_schedule_token", 0)),
                    cfg_lead=cfg_lead))
            elif body.startswith(MISSED):
                d = parse_kv(body)
                events.append(dict(
                    kind="missed", ts=ts, hms=hms,
                    source=d.get("source", "?"),
                    sigma=fnum(d, "predictor_sigma_ms"),
                    lead=fnum(d, "lead_ms"),
                    reservation=d.get("reservation_disposition", "?"),
                    cfg_lead=cfg_lead))
            elif TIP_SET in body or LEAD_SET in body:
                lm = LEAD_SET_RE.search(body)
                if lm:
                    cfg_lead = float(lm.group(1))
                config_marks.append((ts, hms, body.split("(")[0].strip()))
    return events, config_marks


def split_sessions(events):
    """New session when the promotion token or outcome release_seq regresses."""
    sessions = [[]]
    last_token = -1
    last_seq = -1
    for ev in events:
        regress = ((ev["kind"] == "promoted" and 0 < ev["token"] <= last_token
                    and ev["token"] < last_token)
                   or (ev["kind"] == "outcome" and 0 < ev["seq"] <= last_seq
                       and ev["seq"] < last_seq))
        if regress and sessions[-1]:
            sessions.append([])
            last_token = -1
            last_seq = -1
        if ev["kind"] == "promoted" and ev["token"] > 0:
            last_token = ev["token"]
        if ev["kind"] == "outcome" and ev["seq"] > 0:
            last_seq = ev["seq"]
        sessions[-1].append(ev)
    return sessions


def in_window(ev, window):
    if window is None:
        return True
    lo, hi = window
    return lo <= ev["hms"] <= hi


def attribute(session):
    """Attach an armed source to every outcome; count promotions and misses.

    Every shot gains src/sigma/fill/eta/how plus lead = the joined promotion's
    lead_ms when one exists, else the config lead in force at the release
    (None when neither is known). A stamped armed_source of "none" is the
    engine's explicit unattributed sentinel, NOT a source.
    """
    promos = {}
    for ev in session:
        if ev["kind"] == "promoted":
            promos[(ev["epoch"], ev["attempt"])] = ev
    shots = []
    for ev in session:
        if ev["kind"] != "outcome":
            continue
        shot = dict(ev)
        p = promos.get((ev["epoch"], ev["attempt"]))
        shot["lead"] = p["lead"] if p is not None else ev.get("cfg_lead")
        stamped = ev.get("armed_source")
        if stamped and stamped != "none":  # authoritative, from the fired token
            shot["src"] = stamped
            shot["sigma"] = ev["armed_sigma"]
            shot["fill"] = ev["armed_fill"]
            shot["eta"] = ev["armed_eta"]
            shot["how"] = "stamped"
        elif p is not None and not stamped:
            # Pre-armed_source log only. A stamped "none" is NOT joined through the
            # promotion line: the engine said this release did not fire from an
            # attributed token, and the FIRST arm the promotion logged may not be
            # what released (that ambiguity is exactly why the stamp exists).
            shot["src"] = p["source"]
            shot["sigma"] = p["sigma"]
            shot["fill"] = p["fill"]
            shot["eta"] = p["eta"]
            shot["how"] = "via promotion join"
        else:
            shot["src"] = "none"
            shot["sigma"] = None
            shot["fill"] = None
            shot["eta"] = None
            shot["how"] = "unattributed"
        shots.append(shot)
    return shots, promos


def parse_hand(text):
    tokens = []
    for chunk in re.split(r"[\s,;]+", text.strip()):
        if not chunk:
            continue
        low = chunk.lower()
        if low in GOOD_WORDS | EARLY_WORDS | LATE_WORDS | SKIP_WORDS:
            tokens.append(low)
        elif all(c in "gelxs-?" for c in low):
            tokens.extend(low)  # compact "gglge" form
        else:
            raise SystemExit("hand-count token not understood: %r "
                             "(use G/E/L/X or good/early/late/skip)" % chunk)
    out = []
    for t in tokens:
        if t in GOOD_WORDS:
            out.append("GOOD")
        elif t in EARLY_WORDS:
            out.append("EARLY")
        elif t in LATE_WORDS:
            out.append("LATE")
        else:
            out.append("SKIP")
    return out


def wilson(k, n):
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = k / n
    den = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, center - half), min(1.0, center + half))


def pct(x):
    return "%.0f%%" % (100.0 * x)


def q(vals, frac):
    if not vals:
        return float("nan")
    v = sorted(vals)
    idx = min(len(v) - 1, max(0, int(round(frac * (len(v) - 1)))))
    return v[idx]


def report(shots, promos, misses, hand, list_only, discarded=0, max_lead=None):
    stamped = sum(1 for s in shots if s["how"] == "stamped")
    joined = sum(1 for s in shots if s["how"] == "via promotion join")
    print("released shots with an outcome line: %d   (%d stamped, %d via "
          "promotion join, %d unattributed)"
          % (len(shots), stamped, joined, len(shots) - stamped - joined))
    if max_lead is not None:
        print("CONTAMINATED-LEAD FILTER: discarded %d released shot(s) whose "
              "governing lead exceeded %.0f ms" % (discarded, max_lead))
    print("promotions seen: %d   rejected_missed aborts: %d"
          % (len(promos), len(misses)))
    if joined and not stamped:
        print("NOTE: pre-armed_source log -- attribution is the FIRST arm only; "
              "a re-armed token's true source is invisible here.")
    print()

    if list_only or hand:
        print("%-4s %-12s %-22s %8s %8s %8s  %-10s %s"
              % ("#", "time(UTC)", "armed_source", "sigma", "fill", "lead",
                 "grader*", "hand"))
        for i, s in enumerate(shots, 1):
            hv = hand[i - 1] if hand and i - 1 < len(hand) else ""
            print("%-4d %-12s %-22s %8s %8s %8s  %-10s %s"
                  % (i, s["hms"], s["src"],
                     "%.2f" % s["sigma"] if s["sigma"] is not None else "-",
                     "%.1f" % s["fill"] if s.get("fill") not in (None, -1.0) else "-",
                     "%.0f" % s["lead"] if s.get("lead") is not None else "-",
                     s["verdict"], hv))
        print("  (*grader = the frozen self-grader's verdict; NOT timing truth)")
        print()

    by_src = {}
    for s in shots:
        by_src.setdefault(s["src"], []).append(s)

    print("%-24s %4s %10s %10s %9s %11s" % ("armed_source", "n", "sigma_med",
                                            "sigma_p90", "fill_med",
                                            "grader_LATE"))
    for src in sorted(by_src, key=lambda k: -len(by_src[k])):
        rows = by_src[src]
        sig = [r["sigma"] for r in rows if r["sigma"] is not None]
        fil = [r["fill"] for r in rows if r.get("fill") not in (None, -1.0)]
        glate = sum(1 for r in rows if r["verdict"] == "LATE")
        print("%-24s %4d %10s %10s %9s %8d/%d"
              % (src, len(rows),
                 "%.2f" % statistics.median(sig) if sig else "-",
                 "%.2f" % q(sig, 0.9) if sig else "-",
                 "%.1f" % statistics.median(fil) if fil else "-",
                 glate, len(rows)))
    miss_by_src = {}
    for mv in misses:
        miss_by_src.setdefault(mv["source"], []).append(mv)
    if miss_by_src:
        print()
        print("shots that never released (rejected_missed), by DECIDING source "
              "at the miss:")
        for src in sorted(miss_by_src, key=lambda k: -len(miss_by_src[k])):
            rows = miss_by_src[src]
            kinds = {}
            for r in rows:
                kinds[r["reservation"]] = kinds.get(r["reservation"], 0) + 1
            print("  %-22s %3d   %s" % (src, len(rows),
                  " ".join("%s=%d" % kv for kv in sorted(kinds.items()))))

    if hand:
        print()
        if len(hand) != len(shots):
            print("HAND COUNT NOT JOINED: you typed %d verdicts but the window "
                  "has %d released shots. Use --list to align (X marks a shot "
                  "you did not count)." % (len(hand), len(shots)))
            return
        print("hand-counted miss rate per armed source (banner ground truth):")
        print("%-24s %4s %6s %6s %6s %10s %s"
              % ("armed_source", "n", "good", "early", "late", "miss_rate",
                 "95% CI"))
        for src in sorted(by_src, key=lambda k: -len(by_src[k])):
            idxs = [i for i, s in enumerate(shots) if s["src"] == src]
            verdicts = [hand[i] for i in idxs if hand[i] != "SKIP"]
            n = len(verdicts)
            good = sum(1 for v in verdicts if v == "GOOD")
            early = sum(1 for v in verdicts if v == "EARLY")
            late = sum(1 for v in verdicts if v == "LATE")
            miss = early + late
            lo, hi = wilson(miss, n)
            print("%-24s %4d %6d %6d %6d %10s %s"
                  % (src, n, good, early, late,
                     pct(miss / n) if n else "-",
                     "[%s, %s]" % (pct(lo), pct(hi)) if n else ""))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="path to orion_native.log")
    ap.add_argument("--session", type=int, default=None,
                    help="1-based session index (default: all, reported one by one)")
    ap.add_argument("--window", default=None,
                    help="HH:MM:SS-HH:MM:SS UTC filter inside the session")
    ap.add_argument("--hand", default=None,
                    help="hand-counted banner verdicts in shot order (see header)")
    ap.add_argument("--hand-file", default=None,
                    help="file containing the same")
    ap.add_argument("--list", action="store_true",
                    help="print the per-shot table for aligning a hand count")
    ap.add_argument("--marks", action="store_true",
                    help="print the config-drag markers (tip timing / shot lead)")
    ap.add_argument("--max-lead-ms", type=float, default=None,
                    help="discard released shots whose governing lead exceeds "
                         "this (unschedulable-lead regime; see header)")
    args = ap.parse_args()

    hand = None
    if args.hand_file:
        with open(args.hand_file, encoding="utf-8", errors="replace") as fh:
            hand = parse_hand(fh.read())
    elif args.hand:
        hand = parse_hand(args.hand)

    window = None
    if args.window:
        try:
            lo, hi = args.window.split("-")
            window = (lo.strip(), hi.strip())
        except ValueError:
            raise SystemExit("--window wants HH:MM:SS-HH:MM:SS")

    events, config_marks = load(args.log)
    if args.marks:
        print("config markers:")
        for ts, _hms, text in config_marks:
            print("  %s  %s" % (ts, text))
        print()

    sessions = split_sessions(events)
    if args.session and not (1 <= args.session <= len(sessions)):
        raise SystemExit("log has %d sessions" % len(sessions))
    picked = ([sessions[args.session - 1]] if args.session
              else [s for s in sessions if s])

    for i, session in enumerate(picked, 1):
        label = args.session if args.session else i
        times = [ev["ts"] for ev in session]
        print("=" * 78)
        print("session %d/%d   %s .. %s%s"
              % (label, len(sessions), times[0], times[-1],
                 ("   window %s-%s" % window) if window else ""))
        print("=" * 78)
        evs = [ev for ev in session if in_window(ev, window)]
        shots, promos = attribute(evs)
        misses = [ev for ev in evs if ev["kind"] == "missed"]
        discarded = 0
        if args.max_lead_ms is not None:
            kept = [s for s in shots
                    if s["lead"] is None or s["lead"] <= args.max_lead_ms]
            discarded = len(shots) - len(kept)
            shots = kept
        report(shots, promos, misses, hand, args.list,
               discarded=discarded, max_lead=args.max_lead_ms)
        print()


if __name__ == "__main__":
    main()
