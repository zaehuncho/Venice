# Internal red team — evidence packet (2026-09-21 owner test session)

Owner report: (1) pressing TRIANGLE + CIRCLE together makes Venice 'disconnect'; (2) R2 'clamps down' (stays pressed) for no reason.
Both happened in the session logged in `logs/orion_native.log` (dev build, launched via run_orion.local.ps1), session 2026-09-22T03:05Z–03:27Z (22:05–22:27 local). Fork = C:/Users/aaron/Desktop/chiaki-ng-src (branch orion, working tree has Astra's UNCOMMITTED Takion/thread/time affinity edits + the 3rd release echo).

## What the log shows for (1) — the 'disconnect' is the CONTROLLER PIPE dropping and the route falling back to ViGEm, not the stream

```
22212:2026-09-22T03:11:12.550Z  Controller route changed before automation process: generation=2 attested=1 live=3; scheduled authority revoked.
22213:2026-09-22T03:11:12.551Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
22215:2026-09-22T03:11:12.552Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
22231:2026-09-22T03:11:14.827Z  Sidecar: 2026-09-21 22:11:14,823 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
22236:2026-09-22T03:11:17.045Z  Sidecar: 2026-09-21 22:11:17,037 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
22244:2026-09-22T03:11:19.645Z  Sidecar: 2026-09-21 22:11:19,642 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
22611:2026-09-22T03:13:39.964Z  Controller route changed before automation process: generation=7 attested=1 live=3; scheduled authority revoked.
22612:2026-09-22T03:13:39.964Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
22613:2026-09-22T03:13:39.964Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
22632:2026-09-22T03:13:44.529Z  Sidecar: 2026-09-21 22:13:44,525 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
22884:2026-09-22T03:14:34.317Z  Controller route changed before automation process: generation=9 attested=1 live=3; scheduled authority revoked.
22885:2026-09-22T03:14:34.317Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
22886:2026-09-22T03:14:34.317Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
22905:2026-09-22T03:14:38.859Z  Sidecar: 2026-09-21 22:14:38,856 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
22918:2026-09-22T03:14:43.657Z  Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared.
23290:2026-09-22T03:16:07.565Z  Controller route changed before automation process: generation=10 attested=1 live=3; scheduled authority revoked.
23291:2026-09-22T03:16:07.565Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
23292:2026-09-22T03:16:07.565Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
23312:2026-09-22T03:16:12.303Z  Sidecar: 2026-09-21 22:16:12,299 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
23365:2026-09-22T03:16:36.016Z  Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared.
24324:2026-09-22T03:21:51.215Z  Controller route changed before automation process: generation=11 attested=1 live=3; scheduled authority revoked.
24325:2026-09-22T03:21:51.215Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
24327:2026-09-22T03:21:51.216Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
24347:2026-09-22T03:21:55.505Z  Sidecar: 2026-09-21 22:21:55,501 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
24362:2026-09-22T03:22:00.280Z  Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared.
24473:2026-09-22T03:22:12.656Z  Controller route changed before automation process: generation=16 attested=1 live=3; scheduled authority revoked.
24474:2026-09-22T03:22:12.656Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
24475:2026-09-22T03:22:12.656Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
24492:2026-09-22T03:22:16.831Z  Sidecar: 2026-09-21 22:22:16,831 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
24506:2026-09-22T03:22:21.777Z  Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared.
24514:2026-09-22T03:22:22.817Z  Input release-repair duplicate FAILED (result=5): direct pipe closed; ordinary recovery must re-seed the current controller state.
24515:2026-09-22T03:22:22.818Z  Controller route changed before automation process: generation=17 attested=1 live=3; scheduled authority revoked.
24516:2026-09-22T03:22:22.818Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
24550:2026-09-22T03:22:31.810Z  Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared.
24568:2026-09-22T03:22:37.423Z  Input release-repair duplicate FAILED (result=5): direct pipe closed; ordinary recovery must re-seed the current controller state.
24569:2026-09-22T03:22:37.424Z  Controller route changed before automation process: generation=18 attested=1 live=3; scheduled authority revoked.
24571:2026-09-22T03:22:37.424Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
24592:2026-09-22T03:22:41.566Z  Sidecar: 2026-09-21 22:22:41,563 WARNING RemotePlayOrchestrator: Remote Play input session failed readiness: Remote Play input session to 192.168.137.81 did not become ready: the
24605:2026-09-22T03:22:46.328Z  Direct controller pipe recovered: forced write accepted after physical shot controls were neutral; fail-closed gate cleared.
24625:2026-09-22T03:22:52.662Z  Controller route changed before automation process: generation=23 attested=1 live=3; scheduled authority revoked.
```

Incident count: 10 pipe-closed events, 8 recoveries, 10 route changes.

## The heartbeat right before the FIRST incident (write burst, ack lag)

```
2026-09-22T03:11:10.066Z  Input hook heartbeat: connected=1 writes=8933 failures=0 ack_failures=0 last_us=14 max_us=347 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=8933 expected_seq=8933 ack_bytes=12 ack_wait_us=841 raw_snapshot_
2026-09-22T03:11:11.066Z  Input hook heartbeat: connected=1 writes=8934 failures=0 ack_failures=0 last_us=17 max_us=347 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=8934 expected_seq=8934 ack_bytes=12 ack_wait_us=556 raw_snapshot_
2026-09-22T03:11:12.068Z  Input hook heartbeat: connected=1 writes=8961 failures=0 ack_failures=0 last_us=9 max_us=347 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=8939 expected_seq=8939 ack_bytes=12 ack_wait_us=0 raw_snapshot_loc
2026-09-22T03:11:12.550Z  Controller route changed before automation process: generation=2 attested=1 live=3; scheduled authority revoked.
2026-09-22T03:11:12.551Z  Input release-repair duplicate FAILED (result=0): direct pipe closed; ordinary recovery must re-seed the current controller state.
2026-09-22T03:11:12.552Z  Timing cache route proof issued by neutral local delivery: backend=vigem_xusb generation=3; awaiting exact sidecar echo
2026-09-22T03:11:12.552Z  Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input.
2026-09-22T03:11:12.570Z  Timing route acknowledgement crossed a sidecar scope transition: old_epoch=3 new_epoch=4; proof revoked
2026-09-22T03:11:12.572Z  Timing cache route proof issued by neutral local delivery: backend=vigem_xusb generation=4; awaiting exact sidecar echo
```

## Heartbeats around EVERY incident (writes vs ack_seq — look for the gap)

```
2026-09-22T03:11:12.068Z  Input hook heartbeat: connected=1 writes=8961 failures=0 ack_failures=0 last_us=9 max_us=347 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=8939 expected_seq=8939 ack_bytes=12 
2026-09-22T03:13:39.269Z  Input hook heartbeat: connected=1 writes=12017 failures=0 ack_failures=1 last_us=7 max_us=347 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=11997 expected_seq=11997 ack_bytes=
2026-09-22T03:14:33.770Z  Input hook heartbeat: connected=1 writes=13028 failures=0 ack_failures=2 last_us=7 max_us=347 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=12936 expected_seq=12936 ack_bytes=
2026-09-22T03:22:11.892Z  Input hook heartbeat: connected=1 writes=30667 failures=0 ack_failures=5 last_us=7 max_us=400 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=30625 expected_seq=30625 ack_bytes=
2026-09-22T03:22:21.927Z  Input hook heartbeat: connected=1 writes=30715 failures=0 ack_failures=6 last_us=10 max_us=400 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=30715 expected_seq=30715 ack_bytes
2026-09-22T03:22:36.969Z  Input hook heartbeat: connected=1 writes=30868 failures=0 ack_failures=7 last_us=8 max_us=400 ack_winerr=0 ack_stage=2 ack_error=0 ack_seq=30868 expected_seq=30868 ack_bytes=
```

## What the log shows for (2) — R2

```
1024:2026-09-21T18:57:02.457Z  Physical shot epoch: epoch=151 intent=square_edge route=RawInput raw_button_mask=0x4000 ls=(112,-96) rs=(0,0) l2=0 r2=255 sprint_released=0
1027:2026-09-21T18:57:02.459Z  Square-down delivery identity: physical_epoch=151 shot_attempt=0 square_bit=1 delivery_source_seq=69941 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0
1061:2026-09-21T18:57:03.477Z  Release tick: seq=63 state=Releasing t_ms=0 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
1068:2026-09-21T18:57:03.526Z  Release tick: seq=63 state=Cooldown t_ms=50 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
1099:2026-09-21T18:57:06.294Z  Physical shot epoch: epoch=152 intent=square_edge route=RawInput raw_button_mask=0x4000 ls=(0,-127) rs=(0,0) l2=0 r2=255 sprint_released=0
1102:2026-09-21T18:57:06.295Z  Square-down delivery identity: physical_epoch=152 shot_attempt=0 square_bit=1 delivery_source_seq=70230 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0
1137:2026-09-21T18:57:07.211Z  Release tick: seq=64 state=Releasing t_ms=0 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
1143:2026-09-21T18:57:07.259Z  Release tick: seq=64 state=Cooldown t_ms=50 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
1175:2026-09-21T18:57:10.618Z  Physical shot epoch: epoch=153 intent=square_edge route=RawInput raw_button_mask=0x4000 ls=(28,-127) rs=(0,0) l2=0 r2=255 sprint_released=0
1178:2026-09-21T18:57:10.619Z  Square-down delivery identity: physical_epoch=153 shot_attempt=0 square_bit=1 delivery_source_seq=70558 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0
1213:2026-09-21T18:57:11.584Z  Release tick: seq=65 state=Releasing t_ms=0 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
1219:2026-09-21T18:57:11.630Z  Release tick: seq=65 state=Cooldown t_ms=47 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
1833:2026-09-21T20:51:57.216Z  PRESS ANALOG TRACE: epoch=1 r2=[255,255,0,0,0,0,0,0] ls=[0,0,0,0,0,0,0,0] rs=[0,0,0,0,0,0,0,0] r2_release_edge_ms=0.0
1982:2026-09-21T20:52:29.225Z  Physical shot epoch: epoch=3 intent=stick_down_edge route=RawInput raw_button_mask=0x0000 ls=(0,0) rs=(-26,79) l2=0 r2=255 sprint_released=0
2169:2026-09-21T20:52:45.632Z  Physical shot epoch: epoch=6 intent=square_edge route=RawInput raw_button_mask=0x4000 ls=(108,-103) rs=(0,0) l2=0 r2=255 sprint_released=0
2172:2026-09-21T20:52:45.633Z  Square-down delivery identity: physical_epoch=6 shot_attempt=0 square_bit=1 delivery_source_seq=1588 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0 pa
2208:2026-09-21T20:52:46.667Z  Release tick: seq=4 state=Releasing t_ms=0 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
2215:2026-09-21T20:52:46.715Z  Release tick: seq=4 state=Cooldown t_ms=50 phys_sq=1 out_sq=0 ok=1 backend=PIPE rs=(0,0) l2=0 r2=255 src=RawInput kind=DualSense
2249:2026-09-21T20:52:49.723Z  Physical shot epoch: epoch=7 intent=square_edge route=RawInput raw_button_mask=0x4000 ls=(28,-127) rs=(0,0) l2=0 r2=255 sprint_released=0
2252:2026-09-21T20:52:49.723Z  Square-down delivery identity: physical_epoch=7 shot_attempt=0 square_bit=1 delivery_source_seq=1750 delivery_stage=local_udp_accepted local_route_ack=1 console_ack=0 pa
```

Recovery is gated on 'physical shot controls were neutral' — if R2 is held at that moment the re-seed waits; if a stale snapshot with r2=255 is re-seeded, R2 stays down at the console until the next change.

## Code anchors (found, not yet analysed)

- native_orion/src/OrionAppController.cpp:13714 'Input release-repair duplicate FAILED … direct pipe closed'; :13931 'Direct controller pipe recovered: forced write accepted after physical shot controls were neutral'; :14101 'Input route reassert FAILED'; OrionAppController.h:2262 the independent fail-closed gate.
- 'Controller route changed before automation process: generation=N attested=… live=…; scheduled authority revoked' and 'Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input' — same file (grep them).
- 'Input hook heartbeat: connected= writes= failures= ack_failures= … ack_seq= expected_seq=' — the pipe ack protocol; find the writer + ack watchdog.
- Sidecar: remote_play_orchestrator.py:2824 'Remote Play input session failed readiness'.
- Fork bridge: chiaki-ng-src/gui/src/orioninputbridge.cpp — DisconnectNamedPipe at :65 and :347, 'owner_pipe_disconnected' fail_transport at :339, coalesce/drop logic ~:198-214, disconnect break at :175.
- Fork queue/merge: lib/src/orioninput.c, lib/src/feedbacksender.c (release redundancy: 3 ms twin + 40 ms echo for PURE BUTTON releases only — is_pure_button_release; triggers are analog fields l2/r2), lib/include/chiaki/orioninput.h.
- Fork buttons: CROSS 1<<0, MOON(circle) 1<<1, BOX(square) 1<<2, PYRAMID(triangle) 1<<3, OPTIONS 1<<12, SHARE 1<<13, PS 1<<15 (lib/include/chiaki/controller.h). Native raw_button_mask=0x4000 = square in the launcher's own mask space (see OrionInputProtocol.h / the RawInput mapping).
- No explicit button-combo/hotkey handling exists for triangle+circle in native or fork (grep'd: only the D-pad-Up meter-delay bypass hotkey).

## Ground rules for every agent

- READ-ONLY on source: do not edit, do not run cmake/msbuild (another agent, Astra, builds in native_orion/build), do not launch the apps, do not commit. Python-only pytest is fine: use `-p no:cacheprovider` and `--basetemp` under your own temp dir.
- Evidence over opinion: every finding cites file:line and, where possible, a log line number from logs/orion_native.log (grep -a; the file is 8 MB, never cat it whole).
- Do not print secrets (keys, tokens, SSM values). Do not target PSN/2K/third parties. This is the owner's own software.
- Report format per finding: SEVERITY (blocker/high/medium/low) · title · exploit/failure path · affected file:line · impact · reproduction · root-cause hypothesis with the evidence that supports AND the evidence that would refute it · proposed fix (specific, minimal) · verification test.
- Write your report to docs/redteam/2026-09-21/<your-surface>.md. Rank most severe first. If you find the root cause of bug (1) or (2), say so explicitly and rate your confidence.
