; Orion — Inno Setup installer
; -----------------------------------------------------------------------------
; The delivery mechanism for the Orion NBA-2K auto-green bot. This is an INSTALLER,
; not a standalone exe, because the product loads kernel-mode drivers (ViGEmBus,
; HidHide, WinDivert) that cannot be baked into an exe — see docs/IP_PROTECTION_PLAN.md
; Phase E. It lays down the SIGNED package produced by tools/package_orion_release.py
; and runs the driver installs as elevated steps.
;
; BUILD: iscc.exe installer\orion.iss   (Inno Setup 6+)
; SIGN:  the output installer .exe must itself be EV-signed (see [Setup] SignTool) so
;        SmartScreen grants reputation — a raw/unsigned installer trips Defender.
;
; ==== PLACEHOLDERS to fill during the packaging execution (search "TODO:") ====
;  - MyAppVersion         : injected from the release manifest
;  - SourcePackageDir     : the signed release/orion-package/ output dir
;  - ViGEmBus / HidHide installer filenames under redist\ (vendor-signed MSIs the user supplies)
;  - VC++ redist filename (if the runtime isn't statically linked)
;  - SignTool config (EV cert / HSM) — configured in the Inno IDE or via /Ssigntool=
; =============================================================================

; Customer-facing product name: drives the setup wizard title, the Start menu
; group, Add/Remove Programs, and DefaultDirName ({autopf}\Venice).
; MyAppExeName deliberately does NOT follow the rebrand — the updater and the
; deep-link owner check both resolve OrionNative.exe by name.
; AppId is likewise frozen: it is the upgrade identity, not a display string.
#define MyAppName "Venice"
#define MyAppPublisher "Venice"
#define MyAppExeName "OrionNative.exe"
; Version is injected by installer\build_installer.ps1 from the packaged
; release_manifest.json (iscc /DMyAppVersion=...). The dev fallback exists only so the
; Inno IDE can open this script; a REAL build must never ship it -- enforced below.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
; The SIGNED package dir emitted by tools/package_orion_release.py
; (iscc /DSourcePackageDir=... to override).
#ifndef SourcePackageDir
  #define SourcePackageDir "..\release\orion-package"
#endif

; SERVER-SHARD launch chain. When the packed package carries the unpacked activation
; broker (OrionActivate.exe), the customer-facing launch target, the Start-menu/desktop
; shortcuts, and the orion:// protocol handler must all point at the BROKER, not the
; packed inner OrionNative.exe: on a shard build OrionNative.exe is the Lethe bootstrap
; and cannot run until the broker has written the DPAPI session (docs/ACTIVATION_BROKER
; _2026-09-19.md). MyAppExeName stays OrionNative.exe (the updater + deep-link owner
; check resolve it by name); LaunchExeName is the separate, correct launch entry point.
; The signed release_manifest.json/.sig ship beside the broker (they are part of the
; package) so the broker's --register self-trust gate is satisfied.
#if FileExists(SourcePackageDir + "\OrionActivate.exe")
  #define LaunchExeName "OrionActivate.exe"
  #define ServerShardBuild
#else
  #define LaunchExeName "OrionNative.exe"
#endif

; ==== Compile-time ship gates (fail loud at iscc time, not on a customer machine) ====
; A template-stage version must never reach an artifact. Define AllowDevVersion for a
; local IDE smoke-compile only.
#if MyAppVersion == "0.0.0-dev"
  #ifndef AllowDevVersion
    #error MyAppVersion is 0.0.0-dev - run installer\build_installer.ps1 (or pass /DMyAppVersion=x.y.z). Define /DAllowDevVersion for a local smoke-compile only.
  #endif
#endif
; The source dir must be a real packager output, not an arbitrary folder: the release
; manifest is what SecurityCore verifies at runtime, so a package without it would
; install an app that locks automation on first launch.
#if !FileExists(SourcePackageDir + "\release_manifest.json")
  #error SourcePackageDir does not contain release_manifest.json - point at the output of tools/package_orion_release.py
#endif
; A server-shard package (broker present) is incomplete without the packed inner
; payload and the signed manifest the broker's self-trust gate verifies.
#ifdef ServerShardBuild
  #if !FileExists(SourcePackageDir + "\OrionNative.packed.exe")
    #error Server-shard package has OrionActivate.exe but no OrionNative.packed.exe - re-run pack_lethe_release.py --server-shard
  #endif
  #if !FileExists(SourcePackageDir + "\release_manifest.sig")
    #error Server-shard package must ship a signed release_manifest.sig beside the broker (its --register self-trust anchor)
  #endif
#endif

[Setup]
AppId={{B7E2F1A0-4C3D-4E9A-9F21-ORION0000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Fixed, predictable install dir so the OrionUpdater auto-update path is stable.
DisableProgramGroupPage=yes
; Kernel-driver installs require elevation.
PrivilegesRequired=admin
; The only per-user paths this installer touches are the uninstall cleanup of
; {localappdata}\NexusVision (nexus_bridge.token + dirifempty) and the DataDir read in
; [Code]; the elevated-vs-logged-in profile mismatch is the KNOWN, ACCEPTED Inno caveat
; documented at the DataDir helper below. Acknowledged here so the packing gate's compile
; log stays warning-free and a NEW warning cannot hide behind this known one.
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Customer-visible installer FILENAME — matches the app it installs (Venice).
; The .iss filename itself stays orion.iss (build-tree identity, not shipped).
OutputBaseFilename=VeniceSetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; [2026-09-21 owner: "nice looking installer"] Branded wizard art in the website palette:
; the V mark on the dark canvas as the side image and the small header image. Generated
; by tools (Pillow) into installer\assets\; guarded so a clone without the bitmaps still
; compiles with Inno's stock art rather than failing the build.
#if FileExists(SourcePath + "assets\venice-wizard.bmp")
  WizardImageFile={#SourcePath}assets\venice-wizard.bmp
#endif
#if FileExists(SourcePath + "assets\venice-wizard-small.bmp")
  WizardSmallImageFile={#SourcePath}assets\venice-wizard-small.bmp
#endif
; Venice branding on the installer itself. Icon comes from the same assets\orion.ico
; that OrionNative.exe embeds (blue V), so shortcut + installer + running app all match.
; Kept optional-with-fallback because the .ico is gitignored on some clones.
#if FileExists(SourcePath + "..\assets\orion.ico")
  SetupIconFile={#SourcePath}..\assets\orion.ico
#endif
; EV code-signing of THIS installer exe (SmartScreen reputation; an unsigned installer
; trips Defender). Enabled by defining SignToolName: installer\build_installer.ps1
; passes /DSignToolName=signtool together with the matching /Ssigntool="..." command,
; or configure a "signtool" sign tool in the Inno IDE (Tools > Configure Sign Tools).
; The owner must supply the EV cert/HSM; see installer\README.md and docs/CODE_SIGNING.md.
#ifdef SignToolName
SignTool={#SignToolName}
SignedUninstaller=yes
#endif
UninstallDisplayIcon={app}\{#LaunchExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; Product copy on the two pages every customer reads. Everything else keeps Inno's defaults.
WelcomeLabel1=Welcome to Venice
WelcomeLabel2=Setup will install Venice — jump-shot timing for NBA 2K27 on PS5 — on this PC.%n%nBefore you continue, close any capture software (OBS, your capture card's own viewer) so Venice can use the card, and keep your controller plugged into this PC over USB.%n%nVenice is in beta: PS5 with a capture card is the supported setup.
FinishedHeadingLabel=Venice is installed
FinishedLabel=Setup has finished installing Venice on this PC.%n%nOpen Venice, sign in with Discord, and the first-run tour will walk you through connecting your PS5. Your account and settings live on your Discord login, so there is no licence key to keep.
ClickFinish=Click Finish to leave Setup.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; --- The signed application package (native exe + 6 DLLs + Qt runtime + OrionStream.exe
;     + OrionSidecar.exe and its Nuitka dep dir + assets + the shipped model subset). ---
; recursesubdirs pulls the OrionSidecar.exe dependency directory too. The packager's
; crown-jewel .py name-gate guarantees no readable reader source is present here.
; Excludes: admin/staff tooling is present in the strict release package so the
; release-manifest hash gate covers every binary the DEVELOPER can run, but customers
; never see those tools. Excluding them here keeps the shipping install surface to what
; the customer actually uses. AppId + release_manifest.sig are unaffected.
;   OrionOwner.exe / OrionStaff.exe : internal admin/staff GUIs
;   OrionUpdater.exe / OrionSidecar.exe / OrionNative.exe : SHIP (customer-facing)
Source: "{#SourcePackageDir}\*"; DestDir: "{app}"; Excludes: "OrionOwner.exe,OrionStaff.exe"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

; --- Redistributable driver installers (nefarius, vendor-signed .exe bundles; downloaded to
;     redist\ at build time — see installer\README.md). Kept out of {app}; extracted to a temp
;     dir, run in [Run], not shipped permanently. Pin the versions you validated. ---
Source: "redist\ViGEmBus_1.22.0_x64_x86_arm64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsViGEmBus
Source: "redist\HidHide_1.5.230_x64.exe";           DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsHidHide
; VC++ 2015-2022 x64 runtime. REQUIRED: the Orion binaries are built /MD (dynamic CRT)
; and windeployqt is invoked without --compiler-runtime, so the package carries no
; msvcp140/vcruntime140 -- on a clean machine OrionNative.exe would fail to launch with
; a missing-DLL error before any Orion code runs. Skipped when the runtime is present.
Source: "redist\vc_redist.x64.exe";    DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsVCRedist

[Icons]
; LaunchExeName == OrionActivate.exe for a server-shard build (the broker is the launch
; entry point), else OrionNative.exe. See the LaunchExeName define above.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#LaunchExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#LaunchExeName}"; Tasks: desktopicon

[Run]
; --- Driver installs (ViGEmBus, HidHide) MOVED to [Code] (InstallDriverChecked, run from
;     CurStepChanged/ssPostInstall) on 2026-09-21: a [Run] entry cannot inspect the exit
;     code, so a failed or reboot-pending driver install let Setup finish and Venice then
;     reported only "virtual controller required" at Connect (Astra, bug sweep). Both are
;     still idempotent (skip-if-present) and still run silently before first launch. ---
; VC++ runtime BEFORE first launch (see the [Files] note: the package is /MD with no
; app-local CRT, so without this a clean machine cannot start OrionNative.exe at all).
; NOTE: installed from [Code] (CurStepChanged/ssPostInstall) instead of here so the
; redist exit code is actually inspected. A bare [Run] entry cannot check the result,
; and on a Windows SKU missing Windows Update the redist can fail silently -> msvcp140.dll
; won't load -> OrionNative dies at launch with no diagnostic. See InstallVCRedistChecked.

; --- Optional: launch Orion at the end (the broker on a server-shard build). ---
; [COPY-FIX 2026-09-23] Hidden while a driver/runtime restart is pending (RestartPending in [Code]).
Filename: "{app}\{#LaunchExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent; Check: not RestartPending

[UninstallRun]
; Leave the shared drivers installed on uninstall (other software may use ViGEmBus/HidHide).
; If a clean removal is wanted, add msiexec /x steps here guarded by an "also remove drivers?" task.
; The packet-bridge service (VeniceNetSvc, plus any legacy NexusVisionSvc leftover) is
; removed from [Code] CurUninstallStepChanged via RemoveBridgeService, which waits for
; the asynchronous sc stop to reach STOPPED before deleting — a declarative entry here
; cannot do that and can leave the service marked delete-pending until reboot.

[InstallDelete]
; WAVE 3: purge the retired Nuitka packet-bridge bundle before the new payload lands.
; Old installs carried a full Python runtime under packet_bridge\ (NexusVisionSvc.exe,
; python312.dll, pydivert\, ...). Inno upgrades never remove files absent from the new
; package, so without this the stale ~60 MB bundle — including a second, orphaned
; WinDivert pair — would survive next to VeniceNetSvc.exe forever. The legacy service
; registration itself is stopped in CurStepChanged(ssInstall) below (so nothing here is
; file-locked) and deleted in RegisterPacketBridgeService before VeniceNetSvc is created.
Type: filesandordirs; Name: "{app}\packet_bridge"

[UninstallDelete]
; Force-remove the entire install directory on uninstall, INCLUDING files that Inno's
; per-file tracking doesn't own (log files written to {app} at runtime, cached models
; recomputed after install, sidecar temp files, anything the app itself created). Without
; this the uninstaller leaves an empty-ish {app} behind because it only removes what it
; installed, and a reinstall then hits "DeleteFile failed" on the leftovers. This also
; purges the packet_bridge\ sub-tree (VeniceNetSvc.exe + WinDivert64.dll/.sys) — its
; service registration is removed FIRST in CurUninstallStepChanged(usUninstall), and the
; service's own teardown unloads the WinDivert kernel driver before it reports STOPPED,
; so nothing here is file-locked by a loaded driver on the normal path.
Type: filesandordirs; Name: "{app}"
; The packet-bridge bearer token. The SCM service publishes it to
; %ProgramData%\NexusVision\nexus_bridge.token (LocalSystem mode); an unelevated debug
; bridge publishes to %LOCALAPPDATA%\NexusVision\ instead (IpcServer.h publishToken).
; Both are Venice-private secrets with no value once the service is deleted — remove
; them, then fold the parent NexusVision dir away only when nothing else remains
; (dirifempty: the "Orion Native" data dir survives unless the user chose removal in
; MaybeRemoveUserData, and a sibling product's files always keep the dir alive).
Type: files; Name: "{commonappdata}\NexusVision\nexus_bridge.token"
Type: dirifempty; Name: "{commonappdata}\NexusVision"
Type: files; Name: "{localappdata}\NexusVision\nexus_bridge.token"
Type: dirifempty; Name: "{localappdata}\NexusVision"
; The user's mutable state — %LOCALAPPDATA%\NexusVision\Orion Native\ (settings.json,
; learning.json + per-profile variants, logs\, calibration) — is deliberately NOT listed
; here. Surviving a reinstall is the right default; removal is an explicit interactive
; CHOICE handled by MaybeRemoveUserData in CurUninstallStepChanged (never in silent mode).

[Code]
{ Skip a driver install if it is already present. ViGEmBus/HidHide register a service; probing
  the service key is a cheap, reliable presence check that avoids reinstalling on every run. }

function ServiceInstalled(const ServiceName: string): Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\' + ServiceName);
end;

function NeedsViGEmBus: Boolean;
begin
  { ViGEmBus service key is "ViGEmBus". Install only if absent. }
  Result := not ServiceInstalled('ViGEmBus');
end;

function NeedsHidHide: Boolean;
begin
  { HidHide's kernel service is "HidHide". Install only if absent. }
  Result := not ServiceInstalled('HidHide');
end;

function NeedsVCRedist: Boolean;
var
  Installed: Cardinal;
begin
  { VC++ 2015-2022 x64 runtime presence, as recorded by the redist itself. Missing
    key or Installed<>1 -> run the redist. (64-bit view: the app is x64.) }
  Result := True;
  if RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                        'Installed', Installed) then
    Result := Installed <> 1;
end;

{ Kill any Venice processes that would lock files during install/uninstall.
  taskkill returns 128 when no matching process exists — that's success for our
  purposes, so we always report True. /F is required because OrionNative may be
  in a non-responsive state (e.g. after the packer-crash class of failures).
  Runs SILENTLY (SW_HIDE) so the user never sees flashing consoles. }
{ Is any Venice image running? (tasklist filter; exit code is 0 either way, so parse the
  output file for the image name.) }
function VeniceImageRunning(const Image: string): Boolean;
var
  ResultCode: Integer;
  Lines: TArrayOfString;
  TmpFile: string;
  I: Integer;
begin
  Result := False;
  TmpFile := ExpandConstant('{tmp}\venice_tasklist.txt');
  if Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq ' + Image + '" /NH > "' + TmpFile + '"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and LoadStringsFromFile(TmpFile, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos(Lowercase(Image), Lowercase(Lines[I])) > 0 then
      begin
        Result := True;
        Break;
      end;
  DeleteFile(TmpFile);
end;

{ Close Venice before files are replaced. [2026-09-21 beta] This used to be a silent
  `taskkill /F` on every image, which threw away a customer's last unsaved change and
  killed any unrelated process that happened to share the name (Astra, bug sweep).
  Now: ask first, request a graceful close (WM_CLOSE; the app's normal shutdown path
  writes settings atomically), wait up to ~8 s, and only then force the stragglers.
  Returns False if the customer chose to cancel. }
function CloseVeniceProcesses(const Prompt: Boolean): Boolean;
var
  ResultCode: Integer;
  Names: array[0..5] of string;
  I, Waited: Integer;
  AnyRunning: Boolean;
begin
  Result := True;
  Names[0] := 'OrionNative.exe';
  Names[1] := 'OrionSidecar.exe';
  Names[2] := 'OrionUpdater.exe';
  Names[3] := 'OrionOwner.exe';    { leftover from an older install }
  Names[4] := 'OrionStaff.exe';    { same }
  Names[5] := 'OrionActivate.exe'; { server-shard activation broker }

  AnyRunning := False;
  for I := 0 to High(Names) do
    if VeniceImageRunning(Names[I]) then AnyRunning := True;
  if not AnyRunning then
    Exit;

  if Prompt then
    if MsgBox('Venice is currently running and needs to close before Setup can continue.'
              + #13#10 + #13#10 + 'Click OK to close Venice now, or Cancel to finish what '
              + 'you were doing first.', mbConfirmation, MB_OKCANCEL) <> IDOK then
    begin
      Result := False;
      Exit;
    end;

  { Graceful first: without /F, taskkill posts WM_CLOSE to the app's windows. }
  for I := 0 to High(Names) do
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM ' + Names[I],
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while (Waited < 8000) and VeniceImageRunning('OrionNative.exe') do
  begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  { Anything still alive (a hung instance, the console-less sidecar) is forced. }
  for I := 0 to High(Names) do
    if VeniceImageRunning(Names[I]) then
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + Names[I],
           '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  { One quick beat so the OS releases the handles before Inno starts copying. }
  Sleep(400);
end;

function InitializeSetup: Boolean;
begin
  { Close any running Venice from a previous install before we start copying files.
    Fixes the "DeleteFile failed; code 5 / Access is denied" prompt on Qt6Core.dll
    and libcrypto-3-x64.dll — those get locked by a running instance and Inno cannot
    replace them, so the user was forced to Skip (which leaves version-mismatched
    files behind and breaks the install). No dialog if nothing is running. }
  Result := CloseVeniceProcesses(True);
end;

function InitializeUninstall: Boolean;
begin
  { Same reason on the way out: without this, the uninstaller can't delete Qt6Core /
    libcrypto and reports partial-uninstall, then a fresh install hits the same
    DeleteFile failure. }
  Result := CloseVeniceProcesses(True);
end;

// Install the VC++ 2015-2022 x64 runtime with a real exit-code check. Run from the
// Inno Code section rather than a declarative Run entry because a Run entry cannot
// inspect the result: on a Windows SKU without Windows Update the redist can fail
// silently, after which OrionNative.exe cannot load msvcp140.dll and the customer
// sees nothing happen after "Install complete." Codes per Microsoft: 0 = success,
// 1638 = a newer/equal runtime already present, 3010 = success but reboot required;
// anything else is a genuine failure the user must be told about. The file is the
// same {tmp}\vc_redist.x64.exe extracted by the Files section (deleteafterinstall
// removes it only at the very end of setup, so it is present at ssPostInstall).
{ [2026-09-21 release gate] FAIL CLOSED. Meter Delay is a headline feature and the CRT is
  a hard prerequisite; a setup that "succeeds" without either is the 2026-08-08 hazard
  (fresh installs with Meter Delay dead and nobody told). From ssPostInstall a raised
  exception marks Setup FAILED (non-zero exit, Finished page not reached). NOTE (Astra,
  release review): this reports failure but is NOT transactional -- copied files and
  registrations remain; the transactional service rollback + PrepareToInstall preflight
  is the tracked follow-up. Declared before its first caller (Pascal Script requires it). }
procedure FailInstall(const Msg: string);
begin
  Log('FATAL: ' + Msg);
  MsgBox(Msg + #13#10 + #13#10 + 'Setup cannot complete. Re-run this installer as '
         + 'administrator, or open a ticket with this message.', mbCriticalError, MB_OK);
  RaiseException(Msg);
end;

{ Same, but the customer sees PRODUCT copy plus a short support code, while the technical
  detail (exit codes, sc/registry specifics) goes to the Setup log only (Astra, bug sweep:
  "sc create exit code" is not a customer message). }
procedure FailInstallDetailed(const Msg, Code, Detail: string);
begin
  Log('FATAL [' + Code + ']: ' + Msg + ' -- ' + Detail);
  MsgBox(Msg + ' (code ' + Code + ')' + #13#10 + #13#10
         + 'Setup cannot complete. Re-run this installer as administrator, or open a '
         + 'ticket in the Venice Discord and quote the code above.', mbCriticalError, MB_OK);
  RaiseException(Msg + ' [' + Code + '] ' + Detail);
end;

{ [COPY-FIX 2026-09-23 CW reboot] A 3010 from the VC++ redist is a pending restart too;
  NeedRestart() below turns either flag into Inno's own "Restart now" Finished page. }
var
  VCRedistRestartNeeded: Boolean;

procedure InstallVCRedistChecked;
var
  ResultCode: Integer;
begin
  if not NeedsVCRedist then
    Exit;
  if Exec(ExpandConstant('{tmp}\vc_redist.x64.exe'), '/install /quiet /norestart',
          '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
  begin
    if (ResultCode <> 0) and (ResultCode <> 1638) and (ResultCode <> 3010) then
      { [2026-09-21 release gate] fail CLOSED: without the CRT OrionNative.exe cannot start
        at all, so a "completed" setup here is a broken install with no diagnostic. }
      FailInstallDetailed('A Windows component Venice needs (the Visual C++ runtime) could not be '
                          + 'installed. Restart your PC and run Setup again.',
                          'VCRT-01', 'vc_redist exit code ' + IntToStr(ResultCode))
    else if ResultCode = 3010 then
    begin
      VCRedistRestartNeeded := True;
      MsgBox('The Microsoft Visual C++ runtime was installed, but Windows needs a '
             + 'restart to finish. Please restart your PC before opening Venice.',
             mbInformation, MB_OK);
    end;
  end
  else
    FailInstallDetailed('A Windows component Venice needs (the Visual C++ runtime) could not be '
                        + 'started. Restart your PC and run Setup again.',
                        'VCRT-02', 'vc_redist launch error ' + IntToStr(ResultCode));
end;

{ [2026-09-21] Vendor driver installs with a REAL result check (was a fire-and-forget [Run]
  entry). Exit 0 = installed, 1638 = already present, 3010 = installed but Windows needs a
  restart; anything else fails Setup. After the installer returns, the driver's SCM key is
  re-checked so a "successful" run that registered nothing is still caught. A pending
  restart is remembered and reported once on the Finished page. }
var
  DriverRestartNeeded: Boolean;

procedure InstallDriverChecked(const Friendly, ExeName, ServiceName: string);
var
  ResultCode: Integer;
  ExePath: string;
begin
  if ServiceInstalled(ServiceName) then
    Exit;
  ExePath := ExpandConstant('{tmp}\' + ExeName);
  if not FileExists(ExePath) then
    FailInstall('The ' + Friendly + ' driver installer is missing from this Setup package.');
  if not Exec(ExePath, '/quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    FailInstallDetailed('The ' + Friendly + ' driver could not be started. Restart your PC and '
                        + 'run Setup again.', 'DRV-01', ExeName + ' launch error ' + IntToStr(ResultCode));
  if (ResultCode <> 0) and (ResultCode <> 1638) and (ResultCode <> 3010) then
    FailInstallDetailed('The ' + Friendly + ' driver could not be installed. Venice needs it to '
                        + 'send controller input to your console. Restart your PC and run Setup again.',
                        'DRV-02', ExeName + ' exit code ' + IntToStr(ResultCode));
  if ResultCode = 3010 then
    DriverRestartNeeded := True
  else if not ServiceInstalled(ServiceName) then
    FailInstallDetailed('The ' + Friendly + ' driver installed but Windows has not registered it '
                        + 'yet. Restart your PC and run Setup again.',
                        'DRV-03', ServiceName + ' service key absent after ' + ExeName);
end;

{ Run sc.exe hidden and return whether it launched; ResultCode carries sc's own exit code. }
function RunSc(const Params: string; var ResultCode: Integer): Boolean;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), Params, '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode);
end;

{ Wait until the SCM reports a service STOPPED (or not registered at all). sc.exe stop
  only REQUESTS the stop; deleting a still-running service marks it "for deletion" and
  the following sc create fails with 1072. cmd /c "sc query X | findstr STOPPED" gives
  exit code 0 only when the state line is present — same poll owner_venice_setup.ps1
  uses, ~10 s worst case. }
function WaitServiceStopped(const ServiceName: string): Boolean;
var
  I, ResultCode: Integer;
begin
  { [2026-09-21] Returns whether the service is stopped or absent. Matches the NUMERIC
    SCM state (`STATE : 1`) instead of the word STOPPED, which is localized on non-English
    Windows and made the old poll time out and race the delete (Astra, bug sweep). }
  Result := False;
  for I := 0 to 39 do
  begin
    if not ServiceInstalled(ServiceName) then
    begin
      Result := True;
      Exit;
    end;
    if Exec(ExpandConstant('{cmd}'),
            '/c sc query ' + ServiceName + ' | findstr /R /C:"STATE *: *1 "',
            '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
    begin
      Result := True;
      Exit;
    end;
    Sleep(500);
  end;
end;

{ Stop + delete one packet-bridge service registration, waiting out the asynchronous
  stop. Failure is non-fatal (the service may already be stopped/absent). }
procedure RemoveBridgeService(const ServiceName: string);
var
  ResultCode: Integer;
begin
  if not ServiceInstalled(ServiceName) then
    Exit;
  RunSc('stop ' + ServiceName, ResultCode);
  if not WaitServiceStopped(ServiceName) then
    { Deleting a still-running service marks it delete-pending and the re-create then
      fails with 1072 -- a broken upgrade. Stop here instead, with something to act on. }
    FailInstallDetailed('An earlier Venice service on this PC did not stop in time. Restart your '
                        + 'PC and run Setup again.', 'VNSVC-04', ServiceName + ' not STOPPED within 20 s');
  RunSc('delete ' + ServiceName, ResultCode);
  Sleep(600);  { let the SCM finish the delete before anything recreates a bridge }
end;

{ Register the WinDivert packet-bridge service (VeniceNetSvc) with a real exit-code
  check, mirroring InstallVCRedistChecked. This is the elevated host that makes the
  inbound Meter Delay actually apply on an installed build: it runs as LocalSystem so it
  can open a WinDivert handle (which loads the WinDivert64.sys kernel driver on demand),
  while the unelevated Venice app drives it over 127.0.0.1.

  WAVE 3 (2026-08-08): the host is the C++ VeniceNetSvc.exe (wave 2A) registered under
  the customer-facing name VeniceNetSvc. The LEGACY NexusVisionSvc (Nuitka nexus_svc.py
  from older installs) binds the same exclusive TCP 47291, so on upgrade it is stopped
  and deleted FIRST — exactly one bridge service may exist at a time
  (scripts/owner_venice_setup.ps1 enforces the same mutual exclusion on the dev rig).

  DEMAND-START (not auto): the app sc-starts it only when the delay is needed; the kernel
  driver is never loaded at boot. --arm-meter-delay is baked into binPath (arms a bare
  developer launch of the exe), but in SCM service mode the C++ service reads the arm
  switch from its ENVIRONMENT — venicenet_service/main.cpp runService reads
  ORION_METER_DELAY_ARMED via ServiceArgs.h — so the per-service Environment registry
  value written below is the actual service-mode arm. A service registered without
  either still reports DISARMED honestly. A permissive-but-scoped DACL grants
  Interactive Users START/STOP so the non-admin app can start it (driving it is still
  bearer-token gated). }
procedure RegisterPacketBridgeService;
var
  ExePath, BinPath, Sddl, EnvKey: string;
  ArmRead: string;
  ResultCode: Integer;
begin
  ExePath := ExpandConstant('{app}\packet_bridge\VeniceNetSvc.exe');
  if not FileExists(ExePath) then
  begin
    { build_installer.ps1 already refuses a package without the bridge; reaching this
      branch means the pipeline was bypassed. Do not ship a silent no-Meter-Delay install. }
    FailInstall('The Venice network-delay service (packet_bridge\VeniceNetSvc.exe) is missing '
                + 'from this installer package.');
  end;

  { ROLLBACK COMPATIBILITY: an upgrade from a pre-Venice install must remove the legacy
    service BEFORE the new one is created — both bind exclusive TCP 47291. Then a clean
    re-register of VeniceNetSvc itself so an upgrade never leaves a stale binPath. }
  RemoveBridgeService('NexusVisionSvc');
  RemoveBridgeService('VeniceNetSvc');

  { binPath = "<exe>" --arm-meter-delay  (inner quotes escaped for sc/CommandLineToArgvW). }
  BinPath := '"\"' + ExePath + '\" --arm-meter-delay"';
  if not RunSc('create VeniceNetSvc binPath= ' + BinPath
               + ' start= demand type= own obj= LocalSystem DisplayName= "Venice Packet Service"',
               ResultCode) or (ResultCode <> 0) then
  begin
    FailInstallDetailed('Venice''s network service could not be set up on this PC.',
                        'VNSVC-01', 'sc create exit code ' + IntToStr(ResultCode));
  end;

  RunSc('description VeniceNetSvc "Privileged WinDivert packet bridge for Venice. Lets '
        + 'the app apply the inbound meter delay without running as Administrator."', ResultCode);

  { SERVICE-MODE ARM: the C++ service reads ORION_METER_DELAY_ARMED from its per-service
    Environment (REG_MULTI_SZ under the service key) when launched by the SCM — the
    binPath flag alone does NOT arm service mode (venicenet_service/main.cpp runService).
    Written after sc create so the key exists; read back so a silent write failure can
    never ship a service that starts DISARMED without the operator being told. }
  EnvKey := 'SYSTEM\CurrentControlSet\Services\VeniceNetSvc';
  if not RegWriteMultiStringValue(HKLM, EnvKey, 'Environment',
                                  'ORION_METER_DELAY_ARMED=1')
     or not RegQueryMultiStringValue(HKLM, EnvKey, 'Environment', ArmRead)
     or (Pos('ORION_METER_DELAY_ARMED=1', ArmRead) = 0) then
    { A service that starts DISARMED is the silent-broken state; never complete setup on it. }
    FailInstallDetailed('Venice''s network service was installed but could not be configured.',
                        'VNSVC-02', 'ORION_METER_DELAY_ARMED env write/readback failed under ' + EnvKey);

  { Grant Interactive Users SERVICE_START(RP)/STOP(WP)/QUERY so the unelevated app can
    start the demand-start service; SYSTEM/Admins keep full control. Same SDDL as
    scripts/owner_venice_setup.ps1 — keep the two in lockstep. }
  Sddl := 'D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)'
        + '(A;;CCLCSWRPWPLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)';
  if not RunSc('sdset VeniceNetSvc ' + Sddl, ResultCode) or (ResultCode <> 0) then
    { Without the DACL the unelevated app cannot start the service: Meter Delay is dead for
      every customer who does not run Venice as administrator, i.e. all of them. }
    FailInstallDetailed('Venice''s network service was installed but could not be given the '
                        + 'permissions it needs.', 'VNSVC-03', 'sc sdset exit code ' + IntToStr(ResultCode));
end;

(* SERVER-SHARD only: register the activation broker as the orion:// protocol handler and
  as the customer launch target. On a shard build the customer clicks
  orion://activate?code=PAIR-... from the website; that MUST reach the broker
  (OrionActivate.exe), which performs /api/activate and writes the DPAPI session the
  bootstrap needs. The broker also self-registers orion:// on every run and accepts
  --register as an idempotent install hook; it requires the signed
  release_manifest.json/.sig (shipped in {app}) to satisfy its self-trust gate. The
  packed inner OrionNative.exe deliberately does NOT re-point orion:// when a sibling
  broker exists (main.cpp runtime guard), so this registration is stable across launches. *)
procedure RegisterActivationBroker;
var
  BrokerPath: string;
  ResultCode: Integer;
begin
  BrokerPath := ExpandConstant('{app}\OrionActivate.exe');
  if not FileExists(BrokerPath) then
    Exit;  { non-shard build: the app registers orion:// itself at runtime }
  if not Exec(BrokerPath, '--register', ExpandConstant('{app}'), SW_HIDE,
              ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    { --register verifies the signed manifest beside the broker before writing HKCU
      orion://. On a SHARD build this is FATAL, not advisory: a nonzero exit means the
      broker's own self-trust gate refused (tampered/incomplete package) or orion://
      could not be registered - and the customer's only activation path is clicking
      orion://activate?code=... on the website. Failing the install here is the
      fail-closed outcome; reporting "installed successfully" and leaving a dead
      protocol handler is not. }
    Log('OrionActivate.exe --register returned ' + IntToStr(ResultCode));
    RaiseException('Activation broker registration failed (OrionActivate.exe --register'
      + ' exit ' + IntToStr(ResultCode) + ').'#13#10
      + 'This is a server-shard build: the broker must verify the signed release manifest'
      + ' and register the orion:// handler before Venice can activate.'#13#10
      + 'Re-download the installer; if it happens again, contact support.');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  // ssInstall fires just before [InstallDelete]/[Files] processing. Stop (not delete)
  // any running bridge service NOW so the [InstallDelete] purge of the stale
  // packet_bridge\ Nuitka bundle and the file copy over VeniceNetSvc.exe never hit a
  // file lock held by a running SYSTEM service. Deletion/re-registration happens later
  // in RegisterPacketBridgeService; if the user aborts mid-install, a stopped-but-
  // still-registered service is the recoverable state.
  if CurStep = ssInstall then
  begin
    if ServiceInstalled('NexusVisionSvc') then
    begin
      RunSc('stop NexusVisionSvc', ResultCode);
      WaitServiceStopped('NexusVisionSvc');
    end;
    if ServiceInstalled('VeniceNetSvc') then
    begin
      RunSc('stop VeniceNetSvc', ResultCode);
      WaitServiceStopped('VeniceNetSvc');
    end;
  end;
  // ssPostInstall runs after files are copied (so {tmp}\vc_redist.x64.exe exists) and
  // before the Finished page where the optional app auto-launch lives, guaranteeing the
  // runtime is in place before Venice can be started.
  if CurStep = ssPostInstall then
  begin
    InstallVCRedistChecked;
    InstallDriverChecked('ViGEmBus virtual controller', 'ViGEmBus_1.22.0_x64_x86_arm64.exe', 'ViGEmBus');
    InstallDriverChecked('HidHide controller', 'HidHide_1.5.230_x64.exe', 'HidHide');
    RegisterPacketBridgeService;
    RegisterActivationBroker;
    if DriverRestartNeeded then
      MsgBox('A controller driver was installed and Windows needs a restart to finish. '
             + 'Please restart your PC before opening Venice.', mbInformation, MB_OK);
  end;
end;

{ [COPY-FIX 2026-09-23 CW reboot] Inno calls NeedRestart after installing: True makes the
  Finished page offer "Yes, restart the computer now" instead of letting the customer
  launch Venice on a driver/runtime that is not live yet. RestartPending also gates the
  postinstall "Launch Venice" [Run] entry (Check:), belt and braces. }
function RestartPending: Boolean;
begin
  Result := DriverRestartNeeded or VCRedistRestartNeeded;
end;

function NeedRestart(): Boolean;
begin
  Result := RestartPending;
end;

(* Best-effort stop of the WinDivert kernel driver on uninstall. NORMAL path: the bridge
   service unloads the driver itself during runService teardown
   (venicenet_service/main.cpp::unloadWinDivertDriver) BEFORE the SCM reports it STOPPED,
   so by the time RemoveBridgeService's stop-poll returns the driver is already gone.
   This procedure covers the abnormal residue only: a bridge that crashed before its
   teardown, or a stop that timed out, leaves WinDivert64.sys loaded — which (a) keeps a
   kernel driver resident after uninstall and (b) file-locks {app}\packet_bridge\
   WinDivert64.sys so the [UninstallDelete] "{app}" purge leaves debris behind.
   STOP ONLY, never "sc delete WinDivert": the registration is (re)created on demand by
   whatever opens a WinDivert handle next, and another product's WinDivert simply refuses
   the stop while its handles are open — this fails soft by design.
   NOTE the paren-star comment form: braces like {app} inside a brace-comment would END
   the comment at the first close-brace and turn the rest into "code". *)
procedure StopWinDivertDriver;
var
  ResultCode: Integer;
begin
  if not ServiceInstalled('WinDivert') then
    Exit;
  RunSc('stop WinDivert', ResultCode);
  WaitServiceStopped('WinDivert');
end;

(* Optional user-data removal, asked ONCE per uninstall. Venice's mutable state lives in
   %LOCALAPPDATA%\NexusVision\Orion Native\ (OrionPaths.h orionDataDir: Qt
   AppLocalDataLocation for org "NexusVision", app "Orion Native") — settings.json,
   learning.json + per-profile variants, logs\, calibration caches. Surviving a
   reinstall is the right default, so the uninstaller ASKS instead of assuming; the
   default button is No (keep). A SILENT uninstall always keeps the data — an unattended
   run must never destroy learned calibration without a human choosing it.
   Known Inno caveat, accepted: {localappdata} resolves under the profile of the user the
   elevated uninstaller runs as. When a standard user uninstalls via a separate admin
   account's credentials, that admin profile is inspected instead and the standard user's
   data simply survives (the Yes path removes nothing of theirs). *)
procedure MaybeRemoveUserData;
var
  DataDir: string;
begin
  DataDir := ExpandConstant('{localappdata}\NexusVision\Orion Native');
  if not DirExists(DataDir) then
    Exit;
  if UninstallSilent then
    Exit;
  if MsgBox('Remove Venice data?' + #13#10 + #13#10
            + 'Settings and learned shot calibration will be lost. Choose No to keep '
            + 'them for a future reinstall.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    if not DelTree(DataDir, True, True, True) then
      Log('Venice data dir could not be fully removed: ' + DataDir);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // Remove the packet-bridge service registration (private to Venice; safe to delete on
  // uninstall) BEFORE files are removed, waiting out the asynchronous stop so no handle
  // is open on WinDivert (the service unloads the WinDivert driver on its own shutdown,
  // before it reports STOPPED) and the exe is not file-locked. The legacy NexusVisionSvc
  // name is covered too, for machines where an old install's registration outlived its
  // files. ORDER IS THE ROLLBACK GUARANTEE: services are deleted first and files only
  // after, so a failed/aborted uninstall can leave files-without-service (harmless,
  // re-runnable) but never a registered service pointing at a deleted exe.
  if CurUninstallStep = usUninstall then
  begin
    RemoveBridgeService('VeniceNetSvc');
    RemoveBridgeService('NexusVisionSvc');
    { Residue check: sc delete on a service stuck in a pending state marks it
      delete-pending until reboot. That registration is inert (start would fail on the
      soon-removed exe) and the SCM completes the delete on the next boot; log it so a
      support bundle can explain a transient Services.msc entry. }
    if ServiceInstalled('VeniceNetSvc') or ServiceInstalled('NexusVisionSvc') then
      Log('Bridge service still registered after removal - delete-pending until reboot.');
    StopWinDivertDriver;
    MaybeRemoveUserData;
  end;
end;

// -------------------------------------------------------------------------------
// Offline read-back artifact (NOTE: still inside [Code], so Pascal-style comments):
// with /DEmitPreprocessed (passed by build_installer.ps1) ISPP saves the fully-
// translated script — every #define expanded, exactly what iscc compiled — to
// Output\orion.preprocessed.iss. build_installer.ps1 greps it for the VeniceNetSvc
// registration strings so a rebrand regression fails the build. Placed at the very
// end of the file so the whole translation is captured.
#ifdef EmitPreprocessed
  #expr SaveToFile(AddBackslash(SourcePath) + "Output\orion.preprocessed.iss")
#endif
