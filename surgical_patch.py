"""Surgical CvPython.dll patch - find the specific auth check that hides Start button"""
import struct
import shutil
import os
import re

SRC = r"C:\Users\aaron\Desktop\HeliosII\versions\1.0.9.0\plugins\CvPython.dll"
BAK = SRC + ".surgical.bak"

print(f"[*] Analyzing: {SRC}")

with open(SRC, 'rb') as f:
    data = bytearray(f.read())

# Parse PE
pe_offset = struct.unpack('<I', data[0x3C:0x40])[0]
num_sections = struct.unpack('<H', data[pe_offset+6:pe_offset+8])[0]
opt_hdr_size = struct.unpack('<H', data[pe_offset+0x14:pe_offset+0x16])[0]
sections_offset = pe_offset + 0x18 + opt_hdr_size

text_raw = None
text_size = None
rdata_raw = None
rdata_size = None

for i in range(num_sections):
    sec_off = sections_offset + i * 40
    name = data[sec_off:sec_off+8].rstrip(b'\x00').decode('ascii', errors='ignore')
    raw_size = struct.unpack('<I', data[sec_off+16:sec_off+20])[0]
    raw_ptr = struct.unpack('<I', data[sec_off+20:sec_off+24])[0]
    if name == '.text':
        text_raw = raw_ptr
        text_size = raw_size
    elif name == '.rdata':
        rdata_raw = raw_ptr
        rdata_size = raw_size

print(f"[*] .text: 0x{text_raw:X} - 0x{text_raw+text_size:X}")
print(f"[*] .rdata: 0x{rdata_raw:X} - 0x{rdata_raw+rdata_size:X}")

# Look for strings related to Start button visibility
# Qt widgets use setVisible(bool), setEnabled(bool)
# The string "Start" might be near the UI code

rdata = data[rdata_raw:rdata_raw+rdata_size]

# Find all occurrences of relevant strings
strings_of_interest = [
    b'Start', b'start',
    b'setEnabled', b'setVisible',
    b'isLoggedIn', b'IsLoggedIn', b'loggedIn',
    b'session', b'Session',
    b'Auth', b'auth',
]

print("\n[*] Searching for UI-related strings...")
found_strings = []
for s in strings_of_interest:
    offset = 0
    while True:
        idx = data.find(s, offset)
        if idx == -1:
            break
        # Get surrounding context
        start = max(0, idx - 20)
        end = min(len(data), idx + len(s) + 20)
        context = data[start:end]
        found_strings.append((idx, s, context))
        offset = idx + 1

for idx, s, ctx in found_strings[:30]:
    # Clean context for display
    ctx_clean = bytes(b if 32 <= b < 127 else ord('.') for b in ctx)
    print(f"  0x{idx:05X}: {s.decode():20s} | {ctx_clean.decode()}")

# Look for the pattern: call to auth check, followed by test, followed by conditional
# that controls setEnabled/setVisible

# The key insight: after calling an "isLoggedIn" type function,
# there will be test eax, eax followed by a jump
# The jump target will contain setEnabled(false) or setVisible(false)

# Let's find all call instructions followed by test eax, eax
print("\n[*] Scanning for call; test eax,eax; jz patterns...")

text_data = data[text_raw:text_raw+text_size]
pattern = b'\xE8.....\x85\xC0\x74'  # call rel32; test eax,eax; jz rel8
# Or: call rel32; test eax,eax; 0F 84 (jz rel32)
pattern2 = b'\xE8.....\x85\xC0\x0F\x84'

candidates = []
offset = 0
while offset < len(text_data) - 20:
    # Check for pattern 1 (jz rel8)
    if (text_data[offset] == 0xE8 and
        offset + 8 < len(text_data) and
        text_data[offset+5] == 0x85 and
        text_data[offset+6] == 0xC0 and
        text_data[offset+7] == 0x74):

        file_off = text_raw + offset
        candidates.append((file_off, 'jz8', offset + 7, 2))  # patch at offset+7, 2 bytes

    # Check for pattern 2 (jz rel32)
    if (text_data[offset] == 0xE8 and
        offset + 11 < len(text_data) and
        text_data[offset+5] == 0x85 and
        text_data[offset+6] == 0xC0 and
        text_data[offset+7] == 0x0F and
        text_data[offset+8] == 0x84):

        file_off = text_raw + offset
        candidates.append((file_off, 'jz32', offset + 7, 6))  # patch at offset+7, 6 bytes

    offset += 1

print(f"[*] Found {len(candidates)} call;test;jz patterns")

# Now we need to identify which ones are auth-related
# Strategy: Look for patterns near string references to auth words

# Find references to "Session" or "Auth" strings
auth_string_refs = []
for idx, s, ctx in found_strings:
    if b'Session' in s or b'Auth' in s or b'Token' in s:
        auth_string_refs.append(idx)

print(f"[*] Auth string references: {len(auth_string_refs)}")

# For now, let's patch the most likely candidates
# These are call;test;jz patterns that appear to be auth checks

# Pick candidates that are within reasonable distance of auth strings
# This is heuristic but better than patching everything

print("\n[*] Filtering candidates near auth strings...")
nearby_candidates = []
for file_off, jtype, patch_rel, patch_len in candidates:
    for auth_ref in auth_string_refs:
        if abs(file_off - auth_ref) < 0x2000:  # Within 8KB
            nearby_candidates.append((file_off, jtype, patch_rel, patch_len, auth_ref))
            break

print(f"[*] {len(nearby_candidates)} candidates near auth strings")

# Show top candidates
for file_off, jtype, patch_rel, patch_len, auth_ref in nearby_candidates[:20]:
    print(f"    0x{file_off:05X} ({jtype}) - near auth string at 0x{auth_ref:05X}")

# Backup
if not os.path.exists(BAK):
    shutil.copy(SRC, BAK)
    print(f"\n[+] Backup: {BAK}")

# Apply surgical patches only to nearby candidates
print(f"\n[*] Applying {len(nearby_candidates)} surgical patches...")
for file_off, jtype, patch_rel, patch_len, _ in nearby_candidates:
    abs_patch = text_raw + patch_rel
    if jtype == 'jz8':
        # NOP the jz rel8 (2 bytes: 74 xx -> 90 90)
        data[abs_patch] = 0x90
        data[abs_patch + 1] = 0x90
    else:
        # NOP the jz rel32 (6 bytes: 0F 84 xx xx xx xx -> 90 90 90 90 90 90)
        for i in range(6):
            data[abs_patch + i] = 0x90

with open(SRC, 'wb') as f:
    f.write(data)

print(f"[+] Patched: {SRC}")
print(f"[*] Applied {len(nearby_candidates)} patches")
