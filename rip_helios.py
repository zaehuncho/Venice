"""Aggressive Helios.exe patcher - bypass all auth checks"""
import struct
import shutil
import os

SRC = r"C:\Users\aaron\Desktop\HeliosII\Helios.exe"
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

if not text_raw:
    print("[-] .text section not found")
    exit(1)

text_data = data[text_raw:text_raw+text_size]

# Look for auth strings
print("\n[*] Scanning for auth strings...")
auth_strings = [
    b'isLoggedIn', b'IsLoggedIn', b'loggedIn', b'LoggedIn',
    b'session', b'Session', b'HELIOS_SESSION',
    b'auth', b'Auth', b'authenticate', b'Authenticate',
    b'license', b'License', b'licensed', b'Licensed',
    b'token', b'Token',
    b'login', b'Login',
    b'setEnabled', b'setVisible', b'isEnabled', b'isVisible',
]

for s in auth_strings:
    idx = data.find(s)
    if idx != -1:
        print(f"    Found '{s.decode()}' at 0x{idx:X}")

patches_applied = 0

# Patch all test/jz patterns (make them NOT jump = auth passed)
print("\n[*] Patching jz patterns to NOP...")
jz_patterns = [
    (b'\x85\xC0\x0F\x84', 2, 4),  # test eax,eax; jz rel32
    (b'\x85\xC0\x74', 2, 1),      # test eax,eax; jz rel8
    (b'\x84\xC0\x0F\x84', 2, 4),  # test al,al; jz rel32
    (b'\x84\xC0\x74', 2, 1),      # test al,al; jz rel8
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
        print(f"    Pattern {pattern[:4].hex()}: {count} patches")

# Patch all test/jnz patterns (make them always jump)
print("\n[*] Patching jnz patterns to unconditional jump...")
jnz_patterns = [
    (b'\x85\xC0\x0F\x85', 2, 4),  # test eax,eax; jnz rel32
    (b'\x85\xC0\x75', 2, 1),      # test eax,eax; jnz rel8
    (b'\x84\xC0\x0F\x85', 2, 4),  # test al,al; jnz rel32
    (b'\x84\xC0\x75', 2, 1),      # test al,al; jnz rel8
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
        print(f"    Pattern {pattern[:4].hex()}: {count} patches")

print(f"\n[*] Total patches: {patches_applied}")

with open(SRC, 'wb') as f:
    f.write(data)

print(f"[+] Patched: {SRC}")
