"""Test initializing the patched ch_patched.dll"""
import os
import sys
import ctypes
import numpy as np

DLL_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch_v2.dll"
SCRIPT_DIR = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision"

print(f"[*] Loading DLL: {DLL_PATH}")
os.chdir(SCRIPT_DIR)

dll = ctypes.PyDLL(DLL_PATH)
print("[+] DLL loaded")

# Setup function signatures
dll.r.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
dll.r.restype = ctypes.c_int

dll.p.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
dll.p.restype = ctypes.c_int

dll.g.argtypes = []
dll.g.restype = ctypes.POINTER(ctypes.c_ubyte)

dll.x.argtypes = []
dll.x.restype = None

# Check auth exports
try:
    dll.CheckAuth.argtypes = []
    dll.CheckAuth.restype = ctypes.c_int
    auth = dll.CheckAuth()
    print(f"[*] CheckAuth() = {auth}")
except Exception as e:
    print(f"[-] CheckAuth error: {e}")

try:
    dll.IsLicensed.argtypes = []
    dll.IsLicensed.restype = ctypes.c_int
    lic = dll.IsLicensed()
    print(f"[*] IsLicensed() = {lic}")
except Exception as e:
    print(f"[-] IsLicensed error: {e}")

# Initialize
print(f"\n[*] Calling r(1920, 1080, '{SCRIPT_DIR}')...")
script_dir_bytes = SCRIPT_DIR.encode('utf-8')

try:
    rc = dll.r(1920, 1080, script_dir_bytes)
    print(f"[*] r() returned: {rc}")

    if rc == 0:
        print("[+] SUCCESS! DLL initialized!")

        # Test process
        print("\n[*] Testing p() with dummy frame...")
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

        sz = dll.p(frame.ctypes.data_as(ctypes.c_void_p), 1920, 1080)
        print(f"[*] p() returned size: {sz}")

        if sz > 0:
            ptr = dll.g()
            data = ctypes.string_at(ptr, sz)
            print(f"[+] Got {len(data)} bytes of output")
            # Print first few bytes
            print(f"[*] First 32 bytes: {data[:32].hex()}")
        else:
            print("[*] No output data (expected with no model)")

        # Cleanup
        print("\n[*] Calling x() for cleanup...")
        dll.x()
        print("[+] Cleanup done")
    else:
        print(f"[-] Init failed with code {rc}")

except Exception as e:
    print(f"[-] Exception: {e}")
    import traceback
    traceback.print_exc()

print("\n[*] Test complete")
