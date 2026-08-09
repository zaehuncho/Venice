"""
ch.dll Auth Bypass Patcher

Patches ch.dll to:
1. Make all auth functions return success
2. Skip network checks in r() init
3. Enable offline operation

Based on RE analysis:
- CheckAuth @ 0x1E390: B0 01 C3 (mov al, 1; ret) - ALREADY STUBBED
- GetAuthStatus @ 0x1E3E0: B8 01 00 00 00 C3 (mov eax, 1; ret) - ALREADY STUBBED
- IsLicensed @ 0x1E3F0: B0 01 C3 - ALREADY STUBBED
- ValidateLicense @ 0x1E310: B8 01 00 00 00 C3 - ALREADY STUBBED
- VerifySession @ 0x1E3A0: B0 01 C3 - ALREADY STUBBED

The r() function at 0x1E410 calls auth internally - we need to patch that.
"""

import os
import sys
import struct
import shutil
from datetime import datetime

def rva_to_offset(rva):
    """Convert RVA to file offset for ch.dll"""
    # .text: VA=0x1000, RawPtr=0x400
    if 0x1000 <= rva < 0x398000:
        return rva - 0x1000 + 0x400
    # .rdata: VA=0x398000, RawPtr=0x396800
    if 0x398000 <= rva < 0x4A5000:
        return rva - 0x398000 + 0x396800
    # .data: VA=0x4A5000, RawPtr=0x4A3200
    if rva >= 0x4A5000:
        return rva - 0x4A5000 + 0x4A3200
    return None

def patch_dll(input_path, output_path=None):
    """Patch ch.dll for offline operation"""

    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_cracked{ext}"

    print(f"[*] Reading: {input_path}")
    with open(input_path, 'rb') as f:
        data = bytearray(f.read())

    print(f"[*] Size: {len(data):,} bytes")

    patches_applied = 0

    # ========================================
    # PATCH 1: Auth functions (verify they're stubbed)
    # ========================================

    auth_funcs = {
        'CheckAuth': (0x1E390, b'\xB0\x01\xC3'),           # mov al, 1; ret
        'GetAuthStatus': (0x1E3E0, b'\xB8\x01\x00\x00\x00\xC3'),  # mov eax, 1; ret
        'IsLicensed': (0x1E3F0, b'\xB0\x01\xC3'),          # mov al, 1; ret
        'ValidateLicense': (0x1E310, b'\xB8\x01\x00\x00\x00\xC3'),
        'VerifySession': (0x1E3A0, b'\xB0\x01\xC3'),
    }

    print("\n[*] Checking auth functions...")
    for name, (rva, expected) in auth_funcs.items():
        offset = rva_to_offset(rva)
        actual = bytes(data[offset:offset+len(expected)])

        if actual == expected:
            print(f"    [OK] {name}: already stubbed")
        else:
            print(f"    [PATCH] {name}: {actual.hex()} -> {expected.hex()}")
            data[offset:offset+len(expected)] = expected
            patches_applied += 1

    # ========================================
    # PATCH 2: r() function auth call bypass
    # ========================================

    # At 0x1E430 there's: E8 9F AD FF FF 84 C0 0F 85 D4 02 00 00
    # This is: call auth_check; test al,al; jnz fail
    # We want to NOP the call and make test always succeed

    print("\n[*] Patching r() auth check...")

    # Location of auth call in r()
    r_auth_call_rva = 0x1E42D  # The E8 xx xx xx xx call
    r_auth_call_offset = rva_to_offset(r_auth_call_rva)

    # Original: E8 9F AD FF FF 84 C0 0F 85 D4 02 00 00
    # Patch to: B0 01 90 90 90 84 C0 90 90 90 90 90 90
    # (mov al,1; nops; test al,al; nops to skip the jnz)

    original_auth = bytes([0xE8, 0x9F, 0xAD, 0xFF, 0xFF, 0x84, 0xC0, 0x0F, 0x85])
    patched_auth =  bytes([0xB0, 0x01, 0x90, 0x90, 0x90, 0x84, 0xC0, 0x90, 0x90])

    # Find and patch
    try:
        idx = data.index(original_auth)
        print(f"    Found auth call at offset 0x{idx:X}")
        data[idx:idx+len(patched_auth)] = patched_auth
        # Also NOP the rest of the conditional jump
        data[idx+9:idx+13] = b'\x90\x90\x90\x90'
        patches_applied += 1
        print(f"    [PATCH] Auth call bypassed")
    except ValueError:
        print(f"    [SKIP] Auth call pattern not found (may already be patched)")

    # ========================================
    # PATCH 3: Skip network timeout/retry loops
    # ========================================

    # Look for WinHttp timeout patterns and reduce them
    print("\n[*] Patching network timeouts...")

    # Pattern: mov ecx, 0x1388 (5000ms timeout)
    timeout_patterns = [
        (bytes([0xB9, 0x88, 0x13, 0x00, 0x00]), bytes([0xB9, 0x01, 0x00, 0x00, 0x00])),  # 5000 -> 1ms
        (bytes([0xB9, 0xE8, 0x03, 0x00, 0x00]), bytes([0xB9, 0x01, 0x00, 0x00, 0x00])),  # 1000 -> 1ms
    ]

    for orig, patch in timeout_patterns:
        count = 0
        idx = 0
        while True:
            try:
                idx = data.index(orig, idx)
                data[idx:idx+len(patch)] = patch
                count += 1
                idx += len(patch)
            except ValueError:
                break
        if count:
            print(f"    [PATCH] Reduced {count} timeout(s)")
            patches_applied += count

    # ========================================
    # PATCH 4: Anti-debug bypass
    # ========================================

    print("\n[*] Checking for anti-debug...")

    # IsDebuggerPresent call pattern: FF 15 xx xx xx xx 85 C0 75
    # We want to make it always return 0

    # Just search for the CALL [IsDebuggerPresent] pattern and NOP/zero it
    # Pattern after call: 85 C0 75 xx (test eax,eax; jnz fail)
    # Patch to: 31 C0 90 90 (xor eax,eax; nop; nop)

    antidebug_pattern = bytes([0x85, 0xC0, 0x75])  # test eax,eax; jnz
    antidebug_patch = bytes([0x31, 0xC0, 0x90])    # xor eax,eax; nop

    idx = 0
    count = 0
    while True:
        try:
            idx = data.index(antidebug_pattern, idx)
            # Check if this looks like post-IsDebuggerPresent
            # (there's a CALL FF 15 just before)
            if idx > 6 and data[idx-6:idx-4] == b'\xFF\x15':
                data[idx:idx+3] = antidebug_patch
                count += 1
            idx += 3
        except ValueError:
            break

    if count:
        print(f"    [PATCH] Bypassed {count} anti-debug check(s)")
        patches_applied += count

    # ========================================
    # WRITE OUTPUT
    # ========================================

    print(f"\n[*] Writing: {output_path}")
    with open(output_path, 'wb') as f:
        f.write(data)

    print(f"\n[+] Done! Applied {patches_applied} patches")
    print(f"[+] Output: {output_path}")

    return output_path

def main():
    if len(sys.argv) < 2:
        # Default to the known ch.dll location
        input_path = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch.dll"
    else:
        input_path = sys.argv[1]

    if not os.path.exists(input_path):
        print(f"[-] Not found: {input_path}")
        sys.exit(1)

    # Backup
    backup = input_path + f".backup_{datetime.now().strftime('%H%M%S')}"
    print(f"[*] Backing up to: {backup}")
    shutil.copy2(input_path, backup)

    # Patch
    output = sys.argv[2] if len(sys.argv) > 2 else None
    patch_dll(input_path, output)

if __name__ == '__main__':
    main()
