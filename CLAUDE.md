# Boroscope — Teslong TD300 macOS Viewer

## What this is
A Python-based viewer for the Teslong TD300 articulating inspection camera on macOS. The TD300 is an MFi (Made for iPhone) device that communicates via Apple's iAP2 protocol over USB. Since there's no macOS driver, this project implements the full iAP2 protocol stack and camera command protocol from scratch using pyusb.

## Architecture
- `stream.py` — The main viewer. Handles iAP2 handshake, camera initialization, and streams MJPEG to browser at http://localhost:8080
- `PROTOCOL.md` — Full reverse-engineered protocol specification (from ZMFICamera.framework binary analysis)

## Running
```
sudo venv/bin/python3 stream.py
```
- Requires sudo for USB reset to work on macOS
- Opens http://localhost:8080 with live video feed
- If device is unresponsive, physically unplug and replug it, then re-run

## USB Device
- VID: 0x3301, PID: 0x2003
- Interface 0: iAP2 control (EP 0x01 OUT, EP 0x81 IN)
- Interface 1: Camera data (EP 0x02 OUT, EP 0x82 IN)

## Protocol Stack
1. **iAP2 Link Layer**: Detect (marker exchange) → Negotiate (SYN+ACK) → Auth (certificate, skip challenge) → Identify (accept device)
2. **Camera Protocol**: BB AA framed packets on Interface 1. CID 0x05=GetInfo, 0x06=OpenStream, 0x07/0x0A=VideoData, 0x0B=SwitchCamera
3. **Video Format**: JPEG frames in CID 0x07/0x0A packets. 12-byte header (5 outer + 7 inner), then JPEG fragment. Reassemble using FFD8 start / FFD9 end markers.

## Key Gotchas
- macOS accessoryd daemon competes for the device — USB reset breaks its hold
- Do NOT kill accessoryd or use launchctl — it makes things worse
- If device gets stuck (ignores iAP2 markers), physical replug is the only fix
- Interface 1 alt=1 must be set AFTER auth+identify (per MFi spec)
- Camera uses EA Native Transport — data flows directly on EP 0x02/0x82, not multiplexed through iAP2 link layer
