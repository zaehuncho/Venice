# P7 Local IPC and services: summary (2026-09-22)

The agent's full write-up was stopped by a safety classifier and discarded. This is the defensive fix list from its summary. It was a read-only review plus read-only `sc qc` / `sc sdshow` / `icacls` / `Get-Acl` queries; nothing was edited or started.

**Verdict: needs changes.**

| ID | Severity | Area | Required fix |
|---|---|---|---|
| CL2-P7-001 | high (probable) | VeniceNetSvc (runs as SYSTEM) writes its token under `C:\ProgramData\NexusVision`, a folder a standard user can create or own | The service creates that folder itself with an explicit protected DACL (SYSTEM + Administrators full, Users read only where needed). It refuses to write into a folder it does not own, or one that is a junction or reparse point. The installer sets the same DACL. |
| CL2-P7-002 | medium | The frame pipe `orion_frames` has a fixed name. A producer that fails the identity check still delivers frames to detection. | Fail closed: an unverified producer's frames are not used. Consider a per-session random pipe name passed to the child. |
| CL2-P7-003 | medium | The input pipe `orion_input` has a fixed name and no first-instance flag, and the ends do not verify each other's process. Its DACL is already correct. | Add `FILE_FLAG_FIRST_PIPE_INSTANCE`. Verify the peer's process ID and image (`GetNamedPipeClientProcessId` / `GetNamedPipeServerProcessId`) against the expected child/parent. |
| CL2-P7-004 | medium | Every interactive user can read the service token | Restrict the token file to SYSTEM and the installing user, or move to a per-user authenticated channel. |
| CL2-P7-005 | low | The service's TCP port is not bound exclusively; the standby pipe accepts any host; `taskkill.exe` / `sc.exe` are started without full paths | Use `SO_EXCLUSIVEADDRUSE` and bind to loopback; restrict the standby pipe to local clients (`PIPE_REJECT_REMOTE_CLIENTS`); use full `%SystemRoot%\System32\` paths. |

**Checked and fine:** the install folder inherits Program Files permissions (Users can read and run only); the service binary path is quoted; the preview shared-memory names are random per session; the sidecar's stdin is reachable only by its parent.

**Verification:** re-run the read-only ACL queries after the fix. Unit-test the refusal paths (untrusted folder, a second pipe instance, the wrong peer process). Check the VM install sets the ProgramData DACL.
