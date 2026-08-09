# Orion Remote Play Setup

This flow is for PS5 through Chiaki.

## One-time setup

1. Install ViGEmBus so Orion can create the virtual XUSB controller.
2. Install or place Chiaki at the path shown in Orion's Remote Play page.
3. Register the PS5 in Chiaki once using the console PIN shown on the PS5 Remote Play pairing screen.
4. Set the PS5 console IP in Orion. A wired LAN or a stable 5 GHz/Wi-Fi 6 link is strongly preferred.
5. Plug the physical controller into the PC. Orion reads the physical pad and sends the virtual controller to Remote Play.

## Starting a session

1. Open Orion and unlock the launcher.
2. Go to Remote Play.
3. Confirm the console IP and Chiaki path.
4. Press Connect Chiaki.
5. Select the configured console in Chiaki if it is not already connected.
6. The Chiaki stream should embed into Orion's Live Capture panel and the preview should update.

## How Orion Uses Chiaki

Chiaki still owns the PS5 Remote Play protocol, PSN profile, registration, and actual stream session. Orion does not replace the Chiaki pairing UI yet. Orion launches or attaches to Chiaki, embeds its visible stream window into the launcher, captures that stream for CV, reads the physical controller from the PC, and sends the final virtual controller output through ViGEmBus.

The controller should be plugged into the PC, not the PS5, when testing automation. If the controller is connected to the PS5 directly, Orion cannot take over shot timing.

## In-game use

- Hold Square for a tempo shot. Do not release manually.
- Hold right stick down for stick tempo shots. Do not release manually.
- Hold right stick up for go-to shots. Do not release manually.
- Orion suppresses the physical shot input, drives the virtual right stick, and releases from the live CV/RTT timing engine.

## If Live Capture Is Black

1. Make sure Chiaki is showing the PS5 stream, not the setup screen.
2. Press Disconnect, then Connect Chiaki again.
3. Keep the Orion window visible. The fallback capture path reads the composited Chiaki window region.
4. Check Debug -> Session Log for capture or sidecar errors.
5. Verify ViGEmBus status with the Connect Pad button.

## Notes

- The network bridge is passive telemetry. It reads packet timing and court IP information; it does not modify packets.
- RTT Sync Auto uses the configured console IP until a court/server IP is detected.
- Dashboard setting changes are signed by Orion after saving so normal UI edits do not trip the settings-integrity warning.
