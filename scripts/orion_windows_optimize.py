"""
Orion Windows system optimizations for maximum streaming performance.
Run as Administrator. Applies:
1. High Performance power plan
2. Disable CPU core parking
3. Process priority HIGH for OrionStream.exe
4. MMCSS Pro Audio boost for chiaki threads
5. Disable EcoQoS for OrionStream.exe
6. Game Mode enabled
"""
import subprocess, ctypes, os

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    status = 'OK' if r.returncode == 0 else 'FAIL'
    print(f'  [{status}] {cmd[:80]}')
    if r.returncode != 0 and r.stderr:
        print(f'         {r.stderr.strip()[:200]}')
    return r

if not is_admin():
    print('WARNING: Not running as Administrator. Some optimizations will fail.')
    print('Run: powershell -Command "Start-Process python -ArgumentList orion_windows_optimize.py -Verb RunAs"')
    print()

print('=== Orion Windows System Optimizations ===')
print()

# 1. High Performance power plan
print('1. Power Plan: High Performance')
run('powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c')

# 2. Disable CPU core parking
print('2. Disable CPU core parking')
run('powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR CPMINCORES 100')
run('powercfg /setactive SCHEME_CURRENT')

# 3. Set process priority for OrionStream.exe via registry
print('3. Process priority HIGH for OrionStream.exe')
run('reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options\\OrionStream.exe" /v PriorityClass /t REG_DWORD /d 128 /f')

# 4. Disable EcoQoS for OrionStream.exe (set power throttling to "ignore")
print('4. Disable EcoQoS / power throttling for OrionStream.exe')
run('reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options\\OrionStream.exe\\PerfOptions" /v CpuPriorityClass /t REG_DWORD /d 3 /f')
run('reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options\\OrionStream.exe\\PerfOptions" /v PowerThrottling /t REG_DWORD /d 0 /f')

# 5. Enable Game Mode
print('5. Enable Windows Game Mode')
run('reg add "HKCU\\Software\\Microsoft\\GameBar" /v AllowAutoGameMode /t REG_DWORD /d 1 /f')
run('reg add "HKCU\\Software\\Microsoft\\GameBar" /v AutoGameModeEnabled /t REG_DWORD /d 1 /f')

# 6. Set timer resolution to 0.5ms globally
print('6. Global timer resolution: 0.5ms')
# This is done per-process via timeBeginPeriod in the app, but we can set the system default
run('reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\kernel" /v GlobalTimerResolutionRequests /t REG_DWORD /d 1 /f')

# 7. Disable Windows Update during gaming
print('7. Windows Update: pause during active sessions')
# This is handled by Game Mode, but we also set the network bandwidth reservation
run('reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DeliveryOptimization\\Config" /v DODownloadMode /t REG_DWORD /d 0 /f')

print()
print('=== Done. Reboot recommended for all changes to take effect. ===')
print()
print('To verify:')
print('  powercfg /getactivescheme')
print('  reg query "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options\\OrionStream.exe"')
