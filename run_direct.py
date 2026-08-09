"""Run 2k_Vision script directly, simulating Helios environment"""
import os
import sys
import ctypes
import numpy as np

# Set working directory to script location
SCRIPT_DIR = r"C:\Users\aaron\Desktop\HeliosII\scripts\_2k_Vision"
os.chdir(SCRIPT_DIR)

# The original script uses ch.dll which needs auth
# We have the patched version at ch.dll (45 patches applied)
# Let's try loading it

DLL_PATH = os.path.join(SCRIPT_DIR, "bin", "ch.dll")
print(f"[*] DLL: {DLL_PATH}")
print(f"[*] Exists: {os.path.exists(DLL_PATH)}")
print(f"[*] Size: {os.path.getsize(DLL_PATH)} bytes")

# Add bin to PATH for dependencies
os.environ["PATH"] = os.path.join(SCRIPT_DIR, "bin") + ";" + os.environ.get("PATH", "")

# Try to load the DLL
print("\n[*] Attempting to load DLL...")
try:
    # Use CDLL first to test basic loading
    dll = ctypes.CDLL(DLL_PATH)
    print("[+] CDLL load succeeded!")

    # Check exports
    exports = ['r', 'p', 'g', 'x', 'CheckAuth', 'IsLicensed']
    for name in exports:
        try:
            func = getattr(dll, name)
            print(f"  [+] {name}: found")
        except:
            print(f"  [-] {name}: not found")

except OSError as e:
    print(f"[-] Load failed: {e}")

    # Try to understand the failure
    print("\n[*] Checking DLL dependencies...")
    import subprocess
    result = subprocess.run(
        ["dumpbin", "/dependents", DLL_PATH],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print("  (dumpbin not available)")

    # Check if Python DLL is accessible
    python_dll = r"C:\Users\aaron\AppData\Local\Programs\Python\Python311\python311.dll"
    print(f"\n[*] Python DLL: {os.path.exists(python_dll)}")
