#pragma once

// [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] USB power policy for the physical Sony pad.
//
// Rig-verified mechanism behind "Connect says plug in controller while it IS
// plugged in" (deployed orion_native.log sessions 2026-08-08 03:14 and 05:36,
// plus live registry/event-log inspection of the rig on 2026-08-08):
//
//   * The DualSense's USB HID interface node ships with
//     `EnhancedPowerManagementEnabled = 1` (imported from the device's own
//     MS OS extended-property descriptor at first install). With it set,
//     hidusb idles the pad into runtime suspend after inactivity. DualSense /
//     DualShock firmware is notorious for wedging in that state: the pad
//     drops off the USB bus entirely and does NOT recover on host resume or
//     even a full reboot — only a physical re-plug (VBUS cycle) revives it.
//     (Same mechanism and same fix as DS4Windows' "Disable Enhanced Power
//     Management" troubleshooting step.)
//   * The rig showed the drop during NORMAL uptime with no sleep transition
//     at all (Kernel-Power log: no standby between the pad working at
//     22:28 local and being gone at 00:33), so the global "USB selective
//     suspend setting" — already Disabled on the rig — does not prevent it.
//   * The Enum registry keeps one instance key per port the pad was ever
//     plugged into (six phantom instances on the rig): the value is
//     PER-INSTANCE, so the fix must be applied to every known Sony pad
//     instance, and a NEW port used later re-imports the bad default — the
//     offer must therefore be re-detected, not remembered as "done".
//
// Policy split (pure, unit-testable): given what the registry history and the
// power-parameter scan said, choose the truthful advice for the
// "no pad visible to RawInput/WinMM" Connect refusal. The fix itself is a
// USER-CONSENTED, explicit action (a button) — never applied silently.

namespace orion {

enum class AbsentPadAdvice {
    // No Sony pad has ever enumerated on this machine: the plain
    // "plug your DualSense in" guidance is the truth.
    NeverSeen,
    // A Sony pad has been on this PC's USB before but is not on the bus now:
    // tell the user the one recovery that works (re-seat the cable) instead
    // of gaslighting them with "plug in controller".
    DroppedOffBus,
    // Same as DroppedOffBus, and at least one known pad instance still has
    // EnhancedPowerManagementEnabled=1 — offer the one-time consented fix so
    // the drop stops recurring.
    DroppedOffBusFixAvailable,
};

[[nodiscard]] inline constexpr AbsentPadAdvice absentPadAdvice(
    bool sonyUsbHistoryKnown,
    bool usbPowerFixApplicable) noexcept
{
    if (!sonyUsbHistoryKnown) {
        // Without history the fix cannot apply (the EPM value lives under the
        // per-instance history keys); never claim a pad "was here" without
        // registry evidence.
        return AbsentPadAdvice::NeverSeen;
    }
    return usbPowerFixApplicable ? AbsentPadAdvice::DroppedOffBusFixAvailable
                                 : AbsentPadAdvice::DroppedOffBus;
}

} // namespace orion
