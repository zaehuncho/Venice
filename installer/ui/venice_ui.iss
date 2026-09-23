; Venice one-screen installer look (option A, owner 2026-09-23: "the iss looks janky").
; -----------------------------------------------------------------------------------------
; Included by installer\orion.iss (and the scratch preview). It only restyles pages; every
; install step, check and security rule stays in orion.iss. Define VeniceUiAssets (the
; installer\assets folder) before including. Needs in [Setup]: WizardStyle=modern dark
; hidebevels, WizardBackColor=#06080c, DisableWelcomePage=no (Inno 6 skips Welcome by default and the
; whole one-screen design lives there), DisableReadyPage=yes, DisableDirPage=yes; and in [Messages]
; SetupWindowTitle=Venice Setup.
;
; Welcome  -> big glowing V + VENICE, one line, a 3-item checklist, where it installs, [Install]
; Progress -> smaller glowing mark above the status text and progress bar
; Finished -> glowing mark, "Venice is ready", the next steps, the Launch checkbox, [Done]
; The hero frames come from tools\installer\make_wizard_art.py (flat #06080c edges so they
; melt into WizardBackColor). Everything here is cosmetic: any failure leaves Inno's own page.

[Files]
Source: "{#VeniceUiAssets}\hero\venice-hero-*.bmp"; Flags: dontcopy

[Code]
const
  VeniceHeroFrames = 16;
  VeniceHeroMs = 190;
  VeniceHeroW = 440;
  VeniceHeroH = 190;
  VeniceTextColor = $00F9F5F2;      // #f2f5f9 (TColor is $00BBGGRR)
  VeniceSecondColor = $00BFB2A7;    // #a7b2bf
  VeniceMutedColor = $00A7998D;     // #8d99a7

var
  VeniceWelcomeHero, VeniceInstallHero, VeniceFinishHero: TBitmapImage;
  VeniceFinishTitle: TLabel;
  VeniceTimer: LongWord;
  VeniceFrame: Integer;
  VeniceFramesReady: Boolean;
  { [2026-09-23] Every frame decoded ONCE at start-up; a tick only assigns an in-memory bitmap
    (no disk read + decode per tick, which made the glow stutter). }
  VeniceFrameBitmaps: array of TBitmap;

function VeniceSetTimer(hWnd: LongWord; nIDEvent, uElapse: LongWord; lpTimerFunc: LongWord): LongWord;
  external 'SetTimer@user32.dll stdcall';
function VeniceKillTimer(hWnd: LongWord; uIDEvent: LongWord): BOOL;
  external 'KillTimer@user32.dll stdcall';

function VeniceHeroFile(I: Integer): String;
begin
  Result := ExpandConstant('{tmp}\venice-hero-') + Format('%.2d', [I]) + '.bmp';
end;

function VeniceMakeHero(Parent: TWinControl; W, Top: Integer): TBitmapImage;
begin
  Result := TBitmapImage.Create(WizardForm);
  Result.Parent := Parent;
  Result.Stretch := True;
  Result.BackColor := $000C0806;
  Result.Width := W;
  Result.Height := W * VeniceHeroH div VeniceHeroW;
  Result.Left := (Parent.ClientWidth - W) div 2;
  Result.Top := Top;
  if VeniceFramesReady then
  try
    Result.Bitmap.LoadFromFile(VeniceHeroFile(0));
  except
  end;
end;

function VeniceLabel(Parent: TWinControl; const S: String; Top, Height, Size: Integer;
  Color: TColor; Bold: Boolean): TLabel;
begin
  Result := TLabel.Create(WizardForm);
  Result.Parent := Parent;
  Result.AutoSize := False;
  Result.WordWrap := True;
  Result.Transparent := True;
  Result.Alignment := taCenter;
  Result.Left := ScaleX(20);
  Result.Width := Parent.ClientWidth - ScaleX(40);
  Result.Top := Top;
  Result.Height := Height;
  Result.Font.Name := 'Segoe UI';
  Result.Font.Size := Size;
  Result.Font.Color := Color;
  if Bold then
    Result.Font.Style := [fsBold];
  Result.Caption := S;
end;

procedure VeniceTick(Wnd, Msg, IdEvent, Time: LongWord);
var
  Target: TBitmapImage;
begin
  VeniceFrame := (VeniceFrame + 1) mod VeniceHeroFrames;
  { [2026-09-23 owner: "glitchy but functional"] The progress page repaints constantly, and
    swapping the hero under it flickered, so the progress mark stays still. Only the calm
    welcome and finished screens breathe. }
  case WizardForm.CurPageID of
    wpWelcome: Target := VeniceWelcomeHero;
    wpFinished: Target := VeniceFinishHero;
  else
    Target := nil;
  end;
  if (Target <> nil) and (VeniceFrame < GetArrayLength(VeniceFrameBitmaps)) then
  try
    Target.Bitmap.Assign(VeniceFrameBitmaps[VeniceFrame]);
  except
  end;
end;

procedure InitializeWizard;
var
  I, W, Y: Integer;
  Page: TWinControl;
begin
  VeniceFramesReady := False;
  try
    SetArrayLength(VeniceFrameBitmaps, VeniceHeroFrames);
    for I := 0 to VeniceHeroFrames - 1 do
    begin
      ExtractTemporaryFile('venice-hero-' + Format('%.2d', [I]) + '.bmp');
      VeniceFrameBitmaps[I] := TBitmap.Create;
      VeniceFrameBitmaps[I].LoadFromFile(VeniceHeroFile(I));
    end;
    VeniceFramesReady := True;
  except
  end;

  { No header strip on the inner pages: the progress page gets the whole window. }
  WizardForm.MainPanel.Visible := False;
  WizardForm.InnerNotebook.Top := ScaleY(12);
  WizardForm.InnerNotebook.Height := WizardForm.InnerPage.ClientHeight - ScaleY(24);

  { Welcome: one screen. }
  Page := WizardForm.WelcomePage;
  WizardForm.WizardBitmapImage.Visible := False;
  WizardForm.WelcomeLabel1.Visible := False;
  WizardForm.WelcomeLabel2.Visible := False;
  W := ScaleX(VeniceHeroW);
  if W > Page.ClientWidth - ScaleX(20) then
    W := Page.ClientWidth - ScaleX(20);
  VeniceWelcomeHero := VeniceMakeHero(Page, W, ScaleY(6));
  Y := VeniceWelcomeHero.Top + VeniceWelcomeHero.Height + ScaleY(2);
  VeniceLabel(Page, 'Jump-shot timing for NBA 2K27 on PS5', Y, ScaleY(24), 12, VeniceTextColor, False);
  Y := Y + ScaleY(34);
  VeniceLabel(Page,
    'Keep your controller plugged into this PC over USB' + #13#10 +
    'Close OBS or your capture card''s own viewer' + #13#10 +
    'Set your in-game shot meter to Arrow2 (White)',
    Y, ScaleY(64), 9, VeniceSecondColor, False);
  VeniceLabel(Page, 'Installs to ' + ExpandConstant('{autopf}') + '\Venice',
    Page.ClientHeight - ScaleY(26), ScaleY(18), 8, VeniceMutedColor, False);

  { Progress: smaller mark, then Inno's own status text and progress bar beneath it. }
  Page := WizardForm.InstallingPage;
  VeniceInstallHero := VeniceMakeHero(Page, W * 3 div 4, 0);
  Y := VeniceInstallHero.Top + VeniceInstallHero.Height + ScaleY(6);
  WizardForm.StatusLabel.Top := Y;
  WizardForm.FilenameLabel.Top := Y + ScaleY(20);
  WizardForm.ProgressGauge.Top := Y + ScaleY(44);

  { Finished: laid out in CurPageChanged, after Inno has filled in its text and Launch box. }
  WizardForm.WizardBitmapImage2.Visible := False;
  WizardForm.FinishedHeadingLabel.Visible := False;
  VeniceFinishHero := VeniceMakeHero(WizardForm.FinishedPage, W * 3 div 4, ScaleY(6));
  VeniceFinishTitle := VeniceLabel(WizardForm.FinishedPage, 'Venice is ready',
    VeniceFinishHero.Top + VeniceFinishHero.Height, ScaleY(28), 14, VeniceTextColor, True);

  if VeniceFramesReady then
  try
    VeniceTimer := VeniceSetTimer(0, 0, VeniceHeroMs, CreateCallback(@VeniceTick));
  except
    VeniceTimer := 0;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Y, X, Wd: Integer;
begin
  WizardForm.BackButton.Visible := False;
  if CurPageID = wpWelcome then
    WizardForm.NextButton.Caption := 'Install'
  else if CurPageID = wpFinished then
  begin
    X := ScaleX(56);
    Wd := WizardForm.FinishedPage.ClientWidth - 2 * X;
    Y := VeniceFinishTitle.Top + VeniceFinishTitle.Height + ScaleY(6);
    WizardForm.FinishedLabel.Left := X;
    WizardForm.FinishedLabel.Width := Wd;
    WizardForm.FinishedLabel.Top := Y;
    WizardForm.FinishedLabel.AdjustHeight;
    WizardForm.RunList.Left := X;
    WizardForm.RunList.Width := Wd;
    WizardForm.RunList.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(8);
    WizardForm.NextButton.Caption := 'Done';
  end;
end;

procedure DeinitializeSetup;
begin
  if VeniceTimer <> 0 then
    VeniceKillTimer(0, VeniceTimer);
end;
