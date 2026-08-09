Place `chiaki-ng.exe` or `chiaki.exe` and its required runtime files in this
folder to ship Orion with a bundled PS Remote Play backend.

Recommended install flow:

```powershell
.\tools\install_remote_play_backend.ps1 -ChiakiExe C:\Path\To\chiaki-ng.exe
.\tools\verify_remote_play_backend.py
pyinstaller --noconfirm Orion.spec
```

The Orion launcher also detects Sony PS Remote Play and Xbox app installations
at runtime, but those clients are not copied here by default.
