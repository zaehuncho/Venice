"""Patch isUserAuthenticated to always return true"""
import struct
import shutil
import os

SRC = r"C:\Users\aaron\Desktop\HeliosII\versions\1.0.9.0\plugins\CvPython.dll"

with open(SRC, 'rb') as f:
    data = bytearray(f.read())

# The key is to find where isUserAuthenticated is called
# It's a virtual function call through PluginHost

# From the strings, we see:
# ?isUserAuthenticated@PluginHost@Helios@@... at 0x24AFB
# ?getAuthSessionToken@PluginHost@Helios@@... at 0x24ABA

# These are imported functions. Let's look at the import table
# to find their addresses and then find call sites

# Parse PE headers
pe_offset = struct.unpack('<I', data[0x3C:0x40])[0]

# Get .text section
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
        break

print(f"[*] .text: 0x{text_raw:X} - 0x{text_raw+text_size:X}")

# The pattern we're looking for is likely:
# 1. A call to a getter (getPluginHost or similar)
# 2. Then a call through vtable to isUserAuthenticated
# 3. Then test eax, eax; jz/jne

# Alternative approach: Find all test eax, eax patterns and
# look for ones that are followed by enabling/disabling widgets

# Let's look for the pattern near "Start" and "Restart" strings
# The UI code for the Control section would reference these

# Find where "Start" string is used
start_str_offset = data.find(b'Start\x00\x00\x00\x00\x00\x00\x00Restart')
print(f"[*] 'Start...Restart' at 0x{start_str_offset:X}")

# This is at 0x1D7C4 based on earlier scan
# The code that uses this is likely in .text section
# Look for LEA instructions that reference this address

# In x64, LEA rax, [rip+offset] is 48 8D 05 xx xx xx xx
# The offset is relative to the instruction after LEA

text_data = data[text_raw:text_raw+text_size]

# Simpler approach: Just find all test eax,eax patterns and
# look for clusters that might be auth checks

# Let's patch at the function level instead
# Find the startProcessing function mentioned in the strings:
# ?startProcessing@CVPythonPlugin

start_processing_ref = data.find(b'startProcessing@CVPythonP')
print(f"[*] startProcessing string at 0x{start_processing_ref:X}")

# The actual approach: patch isUserAuthenticated in Helios.exe
# since CvPython.dll imports it from there

print("\n[*] Checking Helios.exe for isUserAuthenticated export...")
helios_path = r"C:\Users\aaron\Desktop\HeliosII\Helios.exe"
with open(helios_path, 'rb') as f:
    helios_data = bytearray(f.read())

# Find the function
auth_func = helios_data.find(b'isUserAuthenticated')
print(f"[*] isUserAuthenticated string in Helios.exe: 0x{auth_func:X}" if auth_func != -1 else "[*] Not found directly")

# Let's try a different approach: hook at runtime using Frida
# But first, let me look for the actual check pattern in CvPython.dll

# The CVPython plugin checks auth before showing buttons
# Look for patterns like:
# mov rcx, [something]  ; get PluginHost
# call [rax+offset]     ; call isUserAuthenticated virtual
# test eax, eax
# jz skip_enable

# Common pattern for virtual call: call [rax+offset] = FF 50 xx or FF 90 xx xx xx xx

print("\n[*] Searching for virtual call + test + jz patterns...")

# Pattern: call [rax+xx]; test eax,eax; jz
# FF 50 xx 85 C0 74 or FF 50 xx 85 C0 0F 84

candidates = []
for i in range(len(text_data) - 20):
    # Virtual call through rax
    if text_data[i] == 0xFF and text_data[i+1] == 0x50:
        # Check for test eax,eax; jz after
        if i + 5 < len(text_data):
            if text_data[i+3] == 0x85 and text_data[i+4] == 0xC0:
                if text_data[i+5] == 0x74:  # jz rel8
                    candidates.append((text_raw + i, 'vcall_jz8', i + 5, 2))
                elif text_data[i+5] == 0x0F and i+6 < len(text_data) and text_data[i+6] == 0x84:
                    candidates.append((text_raw + i, 'vcall_jz32', i + 5, 6))

    # Virtual call through rax with larger offset
    if text_data[i] == 0xFF and text_data[i+1] == 0x90:
        if i + 9 < len(text_data):
            if text_data[i+6] == 0x85 and text_data[i+7] == 0xC0:
                if text_data[i+8] == 0x74:
                    candidates.append((text_raw + i, 'vcall32_jz8', i + 8, 2))
                elif text_data[i+8] == 0x0F and text_data[i+9] == 0x84:
                    candidates.append((text_raw + i, 'vcall32_jz32', i + 8, 6))

print(f"[*] Found {len(candidates)} virtual call + test + jz patterns")

for off, ptype, patch_off, patch_len in candidates:
    print(f"    0x{off:05X} ({ptype})")

# Patch all of them - these are likely auth checks
print(f"\n[*] Patching {len(candidates)} patterns...")

shutil.copy(SRC, SRC + ".vcall.bak")

for _, ptype, patch_rel, patch_len in candidates:
    abs_off = text_raw + patch_rel
    for j in range(patch_len):
        data[abs_off + j] = 0x90  # NOP

with open(SRC, 'wb') as f:
    f.write(data)

print(f"[+] Patched {len(candidates)} virtual call auth checks")
