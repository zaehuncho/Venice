"""Patch DllMain to return TRUE immediately"""
import struct
import shutil

SRC_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch.dll"
DST_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch_patched.dll"

# Backup and copy
shutil.copy(SRC_PATH, DST_PATH)

with open(DST_PATH, 'r+b') as f:
    data = bytearray(f.read())

    # Entry point is at file offset 0x3364F0
    entry_offset = 0x3364F0

    print(f"[*] Original entry point bytes:")
    print(' '.join(f'{b:02X}' for b in data[entry_offset:entry_offset+32]))

    # We want to patch to:
    # mov eax, 1  ; return TRUE
    # ret
    #
    # In x64: B8 01 00 00 00 C3
    #
    # But we need to handle the ret properly for __stdcall
    # Actually for x64, return is just: mov eax, 1; ret
    # The original uses standard x64 calling convention

    # Simpler: just make it return 1 immediately
    # B8 01 00 00 00  mov eax, 1
    # C3              ret

    patch = bytes([
        0xB8, 0x01, 0x00, 0x00, 0x00,  # mov eax, 1
        0xC3,                           # ret
    ])

    # Apply patch
    for i, b in enumerate(patch):
        data[entry_offset + i] = b

    print(f"[*] Patched entry point bytes:")
    print(' '.join(f'{b:02X}' for b in data[entry_offset:entry_offset+32]))

    f.seek(0)
    f.write(data)

print(f"\n[+] Patched DLL saved to: {DST_PATH}")
print("[*] Now testing...")
