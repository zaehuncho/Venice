@echo off
setlocal enableextensions enabledelayedexpansion
title Orion Setup
color 0B
cd /d "%~dp0"

:: ---------------------------------------------------------------------------
:: Orion Setup - one-time console registration helper.
:: Kept OUT of the launcher on purpose: the launcher stays clean and you only
:: need this the first time. The stream client is opened in setup/lobby mode
:: (no auto-stream); actual streaming happens from Orion's Start Stream button.
:: ---------------------------------------------------------------------------

:: Locate the Orion Stream client across dev + packaged layouts.
set "STREAM_EXE="
for %%P in (
  "native_orion\deploy\chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe"
  "native_orion\deploy\chiaki-ng\chiaki-ng-Win\chiaki.exe"
  "chiaki-ng-orion\chiaki-ng-Win\OrionStream.exe"
  "OrionStream.exe"
) do (
  if not defined STREAM_EXE if exist "%%~P" set "STREAM_EXE=%%~P"
)

:menu
cls
echo.
echo     ============================================================
echo                         O R I O N   S E T U P
echo     ============================================================
echo.
echo       First-time setup only. You won't need this again.
echo.
echo       [1]   Register your PS5      ^(open the stream client^)
echo       [2]   Exit
echo.
echo     ------------------------------------------------------------
set "choice="
set /p "choice=     Choose an option (1-2): "

if "%choice%"=="1" goto register
if "%choice%"=="2" goto end
goto menu

:register
echo.
if not defined STREAM_EXE (
  echo     [!] Orion Stream client was not found under native_orion\deploy.
  echo         Make sure Orion is fully installed, then try again.
  echo.
  pause
  goto menu
)
echo     Opening the Orion Stream client in setup mode...
echo     In the client: add and register your PS5 using the 8-digit PIN,
echo     then close it and come back here.
echo.
start "" "%STREAM_EXE%"
pause
goto menu

:end
echo.
echo     Setup complete. Launch Orion, set your console IP, and press
echo     Start Stream. Your settings save automatically.
echo.
timeout /t 2 /nobreak >nul
endlocal
exit /b 0
