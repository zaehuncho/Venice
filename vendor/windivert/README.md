Place a matched WinDivert DLL/SYS pair here only when overriding the pydivert-bundled binaries.

Expected files:
- WinDivert64.dll or WinDivert.dll
- WinDivert64.sys

The launcher prefers the binaries bundled by the installed pydivert package. This folder is a local override for release builds or testing with an explicitly verified WinDivert build.

Current test pair copied from `.venv311/Lib/site-packages/pydivert/windivert_dll`:
- WinDivert64.dll SHA-256: C1E060EE19444A259B2162F8AF0F3FE8C4428A1C6F694DCE20DE194AC8D7D9A2
- WinDivert64.sys SHA-256: 8DA085332782708D8767BCACE5327A6EC7283C17CFB85E40B03CD2323A90DDC2
