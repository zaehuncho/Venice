"""Test the patched ch.dll directly"""
import os
import sys
import ctypes
import numpy as np

# Path to the patched DLL
DLL_PATH = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision\bin\ch.dll"
SCRIPT_DIR = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision"

print(f"[*] Loading DLL: {DLL_PATH}")
print(f"[*] Script dir: {SCRIPT_DIR}")

if not os.path.exists(DLL_PATH):
    print(f"[-] DLL not found!")
    sys.exit(1)

# Change to script directory (DLL expects this)
os.chdir(SCRIPT_DIR)

try:
    # Load the DLL
    dll = ctypes.PyDLL(DLL_PATH)
    print("[+] DLL loaded successfully")

    # Setup function signatures
    dll.r.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
    dll.r.restype = ctypes.c_int

    dll.p.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    dll.p.restype = ctypes.c_int

    dll.g.argtypes = []
    dll.g.restype = ctypes.POINTER(ctypes.c_ubyte)

    dll.x.argtypes = []
    dll.x.restype = None

    # Try to check exports
    print("\n[*] Checking exports...")
    try:
        if hasattr(dll, 'CheckAuth'):
            dll.CheckAuth.argtypes = []
            dll.CheckAuth.restype = ctypes.c_int
            result = dll.CheckAuth()
            print(f"[+] CheckAuth() = {result}")
    except Exception as e:
        print(f"[-] CheckAuth error: {e}")

    try:
        if hasattr(dll, 'IsLicensed'):
            dll.IsLicensed.argtypes = []
            dll.IsLicensed.restype = ctypes.c_int
            result = dll.IsLicensed()
            print(f"[+] IsLicensed() = {result}")
    except Exception as e:
        print(f"[-] IsLicensed error: {e}")

    # Initialize with 1920x1080 resolution
    print("\n[*] Initializing DLL with r(1920, 1080, script_dir)...")
    script_dir_bytes = SCRIPT_DIR.encode('utf-8')

    rc = dll.r(1920, 1080, script_dir_bytes)
    print(f"[*] r() returned: {rc}")

    if rc == 0:
        print("[+] SUCCESS! DLL initialized without auth!")

        # Try processing a dummy frame
        print("\n[*] Testing process() with dummy frame...")
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

        sz = dll.p(frame.ctypes.data_as(ctypes.c_void_p), 1920, 1080)
        print(f"[+] p() returned size: {sz}")

        if sz > 0:
            data = ctypes.string_at(dll.g(), sz)
            print(f"[+] Got {len(data)} bytes of output data")

        # Cleanup
        dll.x()
        print("[+] Cleanup done")
    else:
        print(f"[-] Init failed with code {rc}")

except Exception as e:
    print(f"[-] Error: {e}")
    import traceback
    traceback.print_exc()
