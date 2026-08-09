"""Test the patched ch_patched.dll"""
import os
import sys
import ctypes
import numpy as np

# Path to the patched DLL
DLL_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch_patched.dll"
SCRIPT_DIR = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision"

print(f"[*] Loading DLL: {DLL_PATH}")

if not os.path.exists(DLL_PATH):
    print(f"[-] DLL not found!")
    sys.exit(1)

os.chdir(SCRIPT_DIR)

try:
    dll = ctypes.CDLL(DLL_PATH)
    print("[+] DLL loaded successfully!")

    # Check what exports are available
    print("\n[*] Checking exports...")

    # Standard exports
    exports = ['r', 'p', 'g', 'x', 'CheckAuth', 'IsLicensed']
    for name in exports:
        try:
            func = getattr(dll, name)
            print(f"  [+] {name}: {func}")
        except AttributeError:
            print(f"  [-] {name}: not found")

except Exception as e:
    print(f"[-] Error loading DLL: {e}")
    import traceback
    traceback.print_exc()
