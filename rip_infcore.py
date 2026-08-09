"""Aggressive InferenceCore.dll patcher - bypass model auth"""
import struct
import shutil
import os

SRC = r"C:\Users\aaron\Desktop\HeliosII\versions\1.0.9.0\lib\InferenceCore.dll"
BAK = SRC + ".bak"

print(f"[*] Target: {SRC}")
print(f"[*] Size: {os.path.getsize(SRC)} bytes")

if not os.path.exists(BAK):
    shutil.copy(SRC, BAK)
    print(f"[+] Backup: {BAK}")

with open(SRC, 'rb') as f:
    data = bytearray(f.read())

print(f"[*] Loaded {len(data)} bytes")

# Parse PE
pe_offset = struct.unpack('<I', data[0x3C:0x40])[0]
num_sections = struct.unpack('<H', data[pe_offset+6:pe_offset+8])[0]
opt_hdr_size = struct.unpack('<H', data[pe_offset+0x14:pe_offset+0x16])[0]
sections_offset = pe_offset + 0x18 + opt_hdr_size

text_raw = None
text_size = None
for i in range(num_sections):
    sec_off = sections_offset + i * 40
    name = data[sec_off:sec_off+8].rstrip(b'\x00').decode('ascii', errors='ignore')
    raw_size = struct.unpack('<I', data[sec_off+16:sec_off+20])[0]
    raw_ptr = struct.unpack('<I', data[sec_off+20:sec_off+24])[0]
    if name == '.text':
        text_raw = raw_ptr
        text_size = raw_size
        print(f"[*] .text section: offset=0x{raw_ptr:X} size=0x{raw_size:X}")
        break

text_data = data[text_raw:text_raw+text_size]

# Look for model/session related strings
print("\n[*] Scanning for model/auth strings...")
strings_to_find = [
    b'session', b'Session', b'HELIOS_SESSION',
    b'model', b'Model', b'load_model', b'load_Model',
    b'decrypt', b'Decrypt', b'encrypt', b'Encrypt',
    b'auth', b'Auth', b'authenticate',
    b'license', b'License', b'licensed',
    b'key', b'Key', b'aes', b'AES',
    b'token', b'Token',
    b'infcore_set_session', b'infcore_load_model',
]

for s in strings_to_find:
    offset = 0
    while True:
        idx = data.find(s, offset)
        if idx == -1:
            break
        print(f"    '{s.decode()}' at 0x{idx:X}")
        offset = idx + 1
        if offset - data.find(s) > 1000:  # Only show first few
            break

patches_applied = 0

# Aggressive patching - NOP all jz (don't take failure paths)
print("\n[*] Patching jz patterns...")
jz_patterns = [
    (b'\x85\xC0\x0F\x84', 2, 4),
    (b'\x85\xC0\x74', 2, 1),
    (b'\x84\xC0\x0F\x84', 2, 4),
    (b'\x84\xC0\x74', 2, 1),
]

for pattern, jmp_offset, jmp_len in jz_patterns:
    offset = 0
    count = 0
    while True:
        idx = text_data.find(pattern, offset)
        if idx == -1:
            break
        file_offset = text_raw + idx
        if jmp_len == 1:
            data[file_offset + jmp_offset] = 0x90
            data[file_offset + jmp_offset + 1] = 0x90
        else:
            for i in range(6):
                data[file_offset + jmp_offset + i] = 0x90
        count += 1
        patches_applied += 1
        offset = idx + 1
    if count > 0:
        print(f"    Pattern {pattern[:4].hex()}: {count}")

# Patch jnz to always jump
print("\n[*] Patching jnz patterns...")
jnz_patterns = [
    (b'\x85\xC0\x0F\x85', 2, 4),
    (b'\x85\xC0\x75', 2, 1),
    (b'\x84\xC0\x0F\x85', 2, 4),
    (b'\x84\xC0\x75', 2, 1),
]

for pattern, jmp_offset, jmp_len in jnz_patterns:
    offset = 0
    count = 0
    while True:
        idx = text_data.find(pattern, offset)
        if idx == -1:
            break
        file_offset = text_raw + idx
        if jmp_len == 1:
            data[file_offset + jmp_offset] = 0xEB
        else:
            data[file_offset + jmp_offset] = 0x90
            data[file_offset + jmp_offset + 1] = 0xE9
        count += 1
        patches_applied += 1
        offset = idx + 1
    if count > 0:
        print(f"    Pattern {pattern[:4].hex()}: {count}")

print(f"\n[*] Total patches: {patches_applied}")

with open(SRC, 'wb') as f:
    f.write(data)

print(f"[+] Patched: {SRC}")
