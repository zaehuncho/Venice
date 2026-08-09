#pragma once

#include "OrionTypes.h"

#include <cstdint>

namespace orion {

// WinMM (joyGetPosEx) -> internal ControllerState button mapping for the LAST-RESORT
// "Microsoft PC-joystick driver" fallback route (used only when the pad is not
// RawInput/HID-enumerated and no physical XInput pad is live).
//
// The generic joystick driver surfaces HID buttons in HID usage order. For a DualSense /
// DualShock the HID button order is (source of truth: the RawInput reader's faceBits
// decoding in decodeSonyReport, OrionAppController.cpp):
//   btn1=Square  btn2=Cross  btn3=Circle  btn4=Triangle  btn5=L1  btn6=R1  btn7=L2
//   btn8=R2  btn9=Create/Share  btn10=Options  btn11=L3  btn12=R3  btn13=PS  btn14=Touchpad
// Internally ControllerState uses XInput flag names for the PS buttons:
//   X=Square, A=Cross, B=Circle, Y=Triangle (see ControllerState::square()/cross()/...).
//
// REGRESSION HISTORY (live 2026-07 WinMM-fallback session): btn1..btn4 used to be mapped
// in XBOX face order (btn1->A, btn2->B, btn3->X, btn4->Y), which scrambled the face
// buttons end-to-end whenever this fallback carried the session: physical Square acted as
// Cross, Cross as Circle, and Circle as Square (and the mis-set Square bit also armed the
// autogreen square-hold logic). Buttons 5-12 were already in Sony order; only the face
// four were wrong.
//
// NOTE: WinMM button order can in principle vary by pad revision/driver. The poll loop
// logs the raw dwButtons mask on change (see pollPhysicalController), so a 10-second live
// press-test of each button confirms this table from logs/orion_native.log alone.
//
// dwPov is JOYINFOEX::dwPOV: hundredths of a degree clockwise from up (0=up, 9000=right,
// 18000=down, 27000=left). 0xFFFF means centered; some drivers return 0xFFFFFFFF, so any
// out-of-range value is treated as centered.
inline void applyWinMmButtonsToSample(uint32_t dwButtons, uint32_t dwPov, ControllerState& sample)
{
    sample.buttons = 0;
    if (dwButtons & 0x0001u) sample.buttons |= XINPUT_GAMEPAD_X;              // btn1  Square
    if (dwButtons & 0x0002u) sample.buttons |= XINPUT_GAMEPAD_A;              // btn2  Cross
    if (dwButtons & 0x0004u) sample.buttons |= XINPUT_GAMEPAD_B;              // btn3  Circle
    if (dwButtons & 0x0008u) sample.buttons |= XINPUT_GAMEPAD_Y;              // btn4  Triangle
    if (dwButtons & 0x0010u) sample.buttons |= XINPUT_GAMEPAD_LEFT_SHOULDER;  // btn5  L1
    if (dwButtons & 0x0020u) sample.buttons |= XINPUT_GAMEPAD_RIGHT_SHOULDER; // btn6  R1
    if (dwButtons & 0x0100u) sample.buttons |= XINPUT_GAMEPAD_BACK;           // btn9  Create/Share
    if (dwButtons & 0x0200u) sample.buttons |= XINPUT_GAMEPAD_START;          // btn10 Options
    if (dwButtons & 0x0400u) sample.buttons |= XINPUT_GAMEPAD_LEFT_THUMB;     // btn11 L3
    if (dwButtons & 0x0800u) sample.buttons |= XINPUT_GAMEPAD_RIGHT_THUMB;    // btn12 R3
    if (dwButtons & 0x1000u) sample.buttons |= XINPUT_GAMEPAD_GUIDE;          // btn13 PS
    sample.touchpad = (dwButtons & 0x2000u) != 0;                             // btn14 Touchpad
    sample.l2 = (dwButtons & 0x0040u) ? 255 : 0;                              // btn7  L2 (digital)
    sample.r2 = (dwButtons & 0x0080u) ? 255 : 0;                              // btn8  R2 (digital)

    sample.dpad = 8;  // centered
    if (dwPov < 36000u) {  // valid POV angles are 0..35999; 0xFFFF / 0xFFFFFFFF = centered
        if (dwPov >= 33750u || dwPov < 2250u) sample.dpad = 0;
        else if (dwPov < 6750u) sample.dpad = 1;
        else if (dwPov < 11250u) sample.dpad = 2;
        else if (dwPov < 15750u) sample.dpad = 3;
        else if (dwPov < 20250u) sample.dpad = 4;
        else if (dwPov < 24750u) sample.dpad = 5;
        else if (dwPov < 29250u) sample.dpad = 6;
        else sample.dpad = 7;
        static constexpr uint16_t kDpadBits[8] = {
            XINPUT_GAMEPAD_DPAD_UP,
            XINPUT_GAMEPAD_DPAD_UP | XINPUT_GAMEPAD_DPAD_RIGHT,
            XINPUT_GAMEPAD_DPAD_RIGHT,
            XINPUT_GAMEPAD_DPAD_RIGHT | XINPUT_GAMEPAD_DPAD_DOWN,
            XINPUT_GAMEPAD_DPAD_DOWN,
            XINPUT_GAMEPAD_DPAD_DOWN | XINPUT_GAMEPAD_DPAD_LEFT,
            XINPUT_GAMEPAD_DPAD_LEFT,
            XINPUT_GAMEPAD_DPAD_LEFT | XINPUT_GAMEPAD_DPAD_UP,
        };
        sample.buttons |= kDpadBits[sample.dpad];
    }
}

} // namespace orion
