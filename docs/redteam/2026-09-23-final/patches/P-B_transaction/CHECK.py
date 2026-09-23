from pathlib import Path
import sys
s = Path(sys.argv[1]).read_text(encoding='utf-8')
fixed = (
    'DisableDirPage=yes' in s
    and 'function PrepareToInstall(var NeedsRestart: Boolean): string;' in s
    and 'Type: dirifempty; Name: "{app}"' in s
    and 'Type: filesandordirs; Name: "{app}"' not in s
    and "Names[6] := 'OrionStream.exe'" in s
)
print('FIXED_INSTALL_BRANCH=' + ('PASS' if fixed else 'FAIL'))
print('DARK_STYLE_PRESERVED=' + ('PASS' if 'WizardStyle=modern dark' in s else 'FAIL'))
raise SystemExit(0 if fixed else 1)
