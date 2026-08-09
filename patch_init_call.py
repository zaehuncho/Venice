"""Patch the init function call in DllMain to succeed"""
import struct
import shutil

SRC_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch.dll"
DST_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch_v2.dll"

shutil.copy(SRC_PATH, DST_PATH)

with open(DST_PATH, 'r+b') as f:
    data = bytearray(f.read())

    # Entry point at 0x3364F0
    # Original bytes: 48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 49 8B F8 8B DA 48 8B F1 83 FA 01 75 05 E8 D3 08 00 00
    #
    # At offset +0x17 (0x3364F0 + 0x17 = 0x336507):
    #   83 FA 01 = cmp edx, 1  (check if reason == DLL_PROCESS_ATTACH)
    #   75 05    = jne +5 (skip init call if not attach)
    #   E8 D3 08 00 00 = call 0x3379E4 (relative call, this is the init function)
    #
    # The init function might be failing. Let me:
    # 1. Hook into Helios running and see what happens
    # 2. Or find what exactly the init function does

    entry_offset = 0x3364F0

    # The call instruction is at offset +0x1C from entry (0x3364F0 + 0x1C = 0x33650C)
    # E8 D3 08 00 00 at offset 0x33650C
    # Target = 0x33650C + 5 + 0x8D3 = 0x3379E4 (entry RVA)

    call_offset = entry_offset + 0x1C  # 0x33650C
    print(f"[*] Call instruction at file offset: 0x{call_offset:X}")
    print(f"[*] Bytes at call: {data[call_offset:call_offset+5].hex()}")

    # The init function is at RVA 0x3379E4
    # File offset = 0x3379E4 - 0x1000 + 0x400 = 0x336BE4
    init_file_offset = 0x3379E4 - 0x1000 + 0x400
    print(f"[*] Init function at file offset: 0x{init_file_offset:X}")
    print(f"[*] Init function bytes: {data[init_file_offset:init_file_offset+32].hex()}")

    # Let me look at the init function more closely
    # We need to understand what it does and why it fails

    # Alternative: Instead of patching the call, let's patch the init function itself
    # to return 1 (success) immediately
    #
    # Or we can NOP the call and set eax=1:
    # At call_offset, replace:
    #   E8 xx xx xx xx  (5 bytes call)
    # With:
    #   B8 01 00 00 00  (mov eax, 1)
    #
    # This makes the init "succeed" without actually running

    print("\n[*] Patching init call to return 1...")
    patch = bytes([0xB8, 0x01, 0x00, 0x00, 0x00])  # mov eax, 1
    for i, b in enumerate(patch):
        data[call_offset + i] = b

    print(f"[*] Patched bytes: {data[call_offset:call_offset+5].hex()}")

    f.seek(0)
    f.write(data)

print(f"\n[+] Saved to: {DST_PATH}")
