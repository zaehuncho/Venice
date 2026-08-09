"""Aggressive CvPython.dll patcher - enable Start button without auth"""
import struct
import shutil
import os

# Target the active version
SRC = r"C:\Users\aaron\Desktop\HeliosII\versions\1.0.9.0\plugins\CvPython.dll"
BAK = SRC + ".bak"
DST = SRC  # Patch in place

print(f"[*] Target: {SRC}")
print(f"[*] Size: {os.path.getsize(SRC)} bytes")

# Backup first
if not os.path.exists(BAK):
    shutil.copy(SRC, BAK)
    print(f"[+] Backup: {BAK}")

with open(SRC, 'rb') as f:
    data = bytearray(f.read())

print(f"[*] Loaded {len(data)} bytes")

# Parse PE to find .text section
pe_offset = struct.unpack('<I', data[0x3C:0x40])[0]
num_sections = struct.unpack('<H', data[pe_offset+6:pe_offset+8])[0]
opt_hdr_size = struct.unpack('<H', data[pe_offset+0x14:pe_offset+0x16])[0]
sections_offset = pe_offset + 0x18 + opt_hdr_size

text_raw = None
text_size = None
for i in range(num_sections):
    sec_off = sections_offset + i * 40
    name = data[sec_off:sec_off+8].rstrip(b'\x00').decode('ascii', errors='ignore')
    virt_addr = struct.unpack('<I', data[sec_off+12:sec_off+16])[0]
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

# Search patterns that indicate auth checks
# Common patterns:
# 1. test eax, eax; jz/jnz (check return value)
# 2. cmp byte/dword [xxx], 0; jz/jnz
# 3. call to auth function followed by test/jmp

patches_applied = 0

# Pattern 1: test eax, eax; jnz (0x85 0xC0 0x0F 0x85) - make jnz always fall through
# Pattern 2: test eax, eax; jne (0x85 0xC0 0x75) - make jne always fall through
# Pattern 3: test al, al; jnz/jne
# Pattern 4: cmp eax, 0; jnz/jne

patterns = [
    # (pattern, patch_offset, patch_bytes, description)
    (b'\x85\xC0\x0F\x85', 2, b'\x90\xE9', "test eax,eax; jnz -> jmp"),
    (b'\x85\xC0\x75', 2, b'\xEB', "test eax,eax; jne -> jmp"),
    (b'\x84\xC0\x0F\x85', 2, b'\x90\xE9', "test al,al; jnz -> jmp"),
    (b'\x84\xC0\x75', 2, b'\xEB', "test al,al; jne -> jmp"),
    (b'\x83\xF8\x00\x0F\x85', 3, b'\x90\xE9', "cmp eax,0; jnz -> jmp"),
    (b'\x83\xF8\x00\x75', 3, b'\xEB', "cmp eax,0; jne -> jmp"),
]

# Also look for specific strings that might indicate auth
auth_strings = [b'session', b'Session', b'auth', b'Auth', b'login', b'Login', b'license', b'License', b'token', b'Token']

print("\n[*] Scanning for auth-related strings...")
for s in auth_strings:
    offset = 0
    while True:
        idx = data.find(s, offset)
        if idx == -1:
            break
        print(f"    Found '{s.decode()}' at 0x{idx:X}")
        offset = idx + 1

# Look for "isLoggedIn" or similar
logged_in_patterns = [b'isLoggedIn', b'IsLoggedIn', b'logged', b'Logged', b'isAuth', b'IsAuth']
for s in logged_in_patterns:
    idx = data.find(s)
    if idx != -1:
        print(f"[!] Found '{s.decode()}' at 0x{idx:X}")

# Now let's find the actual auth check
# The Start button is hidden when not logged in
# Look for patterns near UI-related code

# Pattern: A comparison followed by conditional that hides/shows UI
# In Qt, this might be: call isVisible/setVisible, or isEnabled/setEnabled

print("\n[*] Scanning for conditional jumps after potential auth checks...")

# Find all test eax,eax; jz patterns (auth returns 0 = fail)
# and convert jz to unconditional jump (always succeed)

jz_patterns = [
    (b'\x85\xC0\x0F\x84', 2, 4, "test eax,eax; jz rel32"),  # jz with 32-bit offset
    (b'\x85\xC0\x74', 2, 1, "test eax,eax; jz rel8"),      # jz with 8-bit offset
    (b'\x84\xC0\x0F\x84', 2, 4, "test al,al; jz rel32"),
    (b'\x84\xC0\x74', 2, 1, "test al,al; jz rel8"),
]

# For jz patterns, we want to NOP them out (don't take the jump = auth succeeded)
print("\n[*] Patching jz (jump-if-zero) patterns to NOP...")
for pattern, jmp_offset, jmp_len, desc in jz_patterns:
    offset = 0
    count = 0
    while True:
        idx = text_data.find(pattern, offset)
        if idx == -1:
            break

        file_offset = text_raw + idx

        # NOP the conditional jump
        if jmp_len == 1:
            # jz rel8: 74 xx -> 90 90 (2 bytes)
            data[file_offset + jmp_offset] = 0x90
            data[file_offset + jmp_offset + 1] = 0x90
        else:
            # jz rel32: 0F 84 xx xx xx xx -> 90 90 90 90 90 90 (6 bytes)
            for i in range(6):
                data[file_offset + jmp_offset + i] = 0x90

        count += 1
        patches_applied += 1
        offset = idx + 1

    if count > 0:
        print(f"    {desc}: {count} patches")

# Also patch jnz patterns to always jump (invert the logic)
print("\n[*] Patching jnz (jump-if-not-zero) patterns to always jump...")
jnz_patterns = [
    (b'\x85\xC0\x0F\x85', 2, 4, "test eax,eax; jnz rel32"),
    (b'\x85\xC0\x75', 2, 1, "test eax,eax; jnz rel8"),
    (b'\x84\xC0\x0F\x85', 2, 4, "test al,al; jnz rel32"),
    (b'\x84\xC0\x75', 2, 1, "test al,al; jnz rel8"),
]

for pattern, jmp_offset, jmp_len, desc in jnz_patterns:
    offset = 0
    count = 0
    while True:
        idx = text_data.find(pattern, offset)
        if idx == -1:
            break

        file_offset = text_raw + idx

        # Convert conditional jump to unconditional
        if jmp_len == 1:
            # jnz rel8 (75 xx) -> jmp rel8 (EB xx)
            data[file_offset + jmp_offset] = 0xEB
        else:
            # jnz rel32 (0F 85 xx xx xx xx) -> jmp rel32 (90 E9 xx xx xx xx)
            data[file_offset + jmp_offset] = 0x90
            data[file_offset + jmp_offset + 1] = 0xE9

        count += 1
        patches_applied += 1
        offset = idx + 1

    if count > 0:
        print(f"    {desc}: {count} patches")

print(f"\n[*] Total patches applied: {patches_applied}")

# Write patched DLL
with open(DST, 'wb') as f:
    f.write(data)

print(f"[+] Patched DLL saved to: {DST}")
print("\n[!] RESTART HELIOS to apply patches")
