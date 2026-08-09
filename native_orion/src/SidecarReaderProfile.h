#pragma once

#include <QtCore/QLatin1String>
#include <QtCore/QProcessEnvironment>
#include <QtCore/QString>
#include <QtCore/QStringList>

namespace orion {

// ============================================================================
//  SHIPPED READER PROFILE  (2026-08-04)
//
//  THE PROBLEM THIS SOLVES.  Three SimpleMeterReader flags are default-OFF in
//  simple_meter_reader.py and are turned ON only by run_orion.local.ps1, which
//  is gitignored (.gitignore:121 `*.local.ps1`) and appears in no packaging
//  list (installer/orion.iss [Files]; tools/package_orion_release.py). Every
//  live batch this project has ever graded — and therefore every timing number
//  the release lead was calibrated against — ran with those three ON. A
//  customer build inherits ZERO ORION_* variables, so it would run a detector
//  configuration that has never been played on, and whose fill differs from the
//  calibrated one by a strictly one-sided bias (measured below).
//
//  The correction belongs HERE rather than only in the Python defaults because
//  the native side is what actually spawns the sidecar, so pinning it here is
//  what makes dev and customer identical no matter which launcher (or none)
//  started the app. Flipping the Python defaults as well is complementary, not
//  redundant: it fixes the offline harnesses, which spawn no native parent.
//
//  MEASURED, production-wired A/B (require_gameplay_eligibility=True + a real
//  hw arm — NOT the SimpleMeterReader(W,H) shape the regression gates use, which
//  production never runs), 8,592 framedump frames, session_20260804_091119:
//
//    flag                 det flips   frames w/ fill change   mean delta   max
//    ORION_READER_ANCHOR       0        4    (0.17%)          +19.58 pp   +77.09
//    ORION_READER_PCTL_FILL    0      479   (20.52%)           +0.26 pp    +1.12
//    ORION_READER_TRACK_H_CAP  0       59    (2.53%)           +5.65 pp   +10.72
//    all three together        0      540   (23.14%)           +0.99 pp   +77.09
//
//  Two properties matter more than the magnitudes:
//    * DETECTION IS BYTE-IDENTICAL. Zero frames gain or lose a detection under
//      any of the three. None of them can cause a missed meter, so none of them
//      can turn a working install into a fail-closed one.
//    * THE BIAS IS ONE-SIDED. 539 of 540 changed frames move UP. These flags
//      remove UNDER-reads (an occlusion-truncated red extent; a fill denominator
//      poisoned by one over-tall track measurement). Shipping them OFF does not
//      add noise, it re-introduces a per-shot correlated, horizon-flat offset —
//      exactly the error class frame averaging cannot remove.
//
//  CONTRACT.  Never override an explicit operator value. A var already present
//  in the environment wins, including "0", so an A/B (or a support instruction
//  to disable one) still works. Only silence means "apply the shipped profile".
// ============================================================================

// The reader flags the shipped profile pins ON when the environment is silent.
// Additions here must clear the same bar: live-graded ON, and a production-wired
// A/B showing zero detection flips.
inline const char* const kShippedReaderProfileFlags[] = {
    // N1 anchors box velocity to the fill-invariant floor/notch instead of the
    // fill-column centre, which on a static rising meter gains a phantom upward
    // term and walks the coast/relocate window off the meter. N3 median-steadies
    // the emitted box width only. Fixed 176 of 2,334 detected frames' box
    // positions in the A/B, and one +77 pp fill misread caused by that walk-off.
    "ORION_READER_ANCHOR",
    // Occlusion-tolerant percentile read of the red extent (20th percentile of
    // per-column tops with run-length solidity) in place of the row-mean
    // crossing, which truncates low whenever the player's arm crosses the meter.
    "ORION_READER_PCTL_FILL",
    // Refuses an over-tall full-cap candidate into the fill DENOMINATOR history.
    // Without it one inflated measurement drags the 5-sample median up and every
    // fill for the rest of that lock is divided by a track ~10% too tall.
    "ORION_READER_TRACK_H_CAP",
};

// DELIBERATELY NOT IN THE PROFILE, with the reason, so this list is not
// re-litigated:
//   ORION_READER_FAKELOCK_BREAK — the launcher sets it, but it is a provable
//     no-op. Both call sites read `(self._fakelock_break or self._stalebreak)`
//     and `_stalebreak` is default-ON in simple_meter_reader.py, unset by the
//     launcher, so the OR is already satisfied in dev AND in a customer build.
//     Confirmed in the A/B: adding it to the other three changed nothing.
//   ORION_READER_ROBUST — never set by the launcher either, so it is OFF on the
//     dev rig too. It has no live hours in EITHER configuration; pinning it ON
//     would ship something genuinely untested, which is the exact failure this
//     header exists to prevent.
//   ORION_READER_TRACK_H_ROBUST_GATE — reads by no code at all (zero hits across
//     every .py/.cpp/.h/.qml in the tree). The launcher line and the A/B numbers
//     attached to it describe a mechanism that has never existed.

// Apply the shipped reader profile to `env`, leaving any explicitly-set value
// untouched. Returns a compact, log-safe summary of the RESOLVED configuration
// — e.g. "ORION_READER_ANCHOR=1(profile) ORION_READER_PCTL_FILL=0(env) ..." —
// so a support log states exactly which reader config produced the session and
// a divergence like this one can never again be invisible.
[[nodiscard]] inline QString applyShippedReaderProfile(QProcessEnvironment& env)
{
    QStringList resolved;
    for (const char* const name : kShippedReaderProfileFlags) {
        const QString key = QString::fromLatin1(name);
        QString value;
        QLatin1String origin("profile");
        if (env.contains(key)) {
            value = env.value(key);
            origin = QLatin1String("env");
        } else {
            value = QStringLiteral("1");
            env.insert(key, value);
        }
        resolved.append(key + QLatin1Char('=') + value + QLatin1Char('(') + origin
                        + QLatin1Char(')'));
    }
    return resolved.join(QLatin1Char(' '));
}

} // namespace orion
