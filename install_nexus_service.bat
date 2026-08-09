@echo off
:: ===========================================================================
:: install_nexus_service.bat  -  Nexus Vision packet-bridge (RTT capture) setup
::
:: Installs the elevated WinDivert capture service (NexusVisionSvc) so Orion can
:: measure the REAL PS5<->court round-trip (RTT / jitter / offset) instead of the
:: degraded "sniff-only" fallback.  Passive measurement only.
::
:: >>> RIGHT-CLICK THIS FILE -> "Run as administrator" <<<
:: ===========================================================================
setlocal enableextensions
cd /d "%~dp0"

:: --- must be elevated (service install + driver load need admin) ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo ERROR: not elevated.  Right-click this file and choose "Run as administrator".
  pause & exit /b 1
)

set "PY=.venv311\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: %PY% not found.  Expected the nexus_svc Python 3.11 venv at .venv311
  pause & exit /b 1
)

echo [1/4] Installing capture dependencies into .venv311 ...
"%PY%" -m pip install --upgrade --quiet pywin32 pydivert
if %errorlevel% neq 0 ( echo ERROR: dependency install failed. & pause & exit /b 1 )

:: pywin32 needs a one-time post-install to register its service host DLLs
"%PY%" "%~dp0.venv311\Scripts\pywin32_postinstall.py" -install >nul 2>&1

echo [2/4] Registering NexusVisionSvc (clean re-register, DEMAND-START) ...
:: remove any stale registration first (a prior install left a dead host-exe path,
:: which is why the service couldn't start); then register fresh. 2026-07-02: DEMAND
:: start (was auto) — packet capture is now opt-in in Orion (it captures nothing on a
:: non-routing PC, and the WinDivert kernel-driver load at boot is the one plausible
:: "LAN cable disconnected" source). Orion sc-starts it only when the user enables
:: Network packet capture.
"%PY%" nexus_svc.py remove >nul 2>&1
"%PY%" nexus_svc.py --startup manual install

echo [3/4] Service registered (demand-start; Orion starts it only when packet capture is enabled).

echo [4/4] Status:
sc query NexusVisionSvc | findstr /C:"STATE"
echo.
echo If STATE shows 4 RUNNING, the packet bridge is active. Launch Orion and the
echo Network panel will show the real court RTT (no "sniff-only debug mode" in the log).
echo To remove later:  "%PY%" nexus_svc.py remove   (as administrator)
pause
