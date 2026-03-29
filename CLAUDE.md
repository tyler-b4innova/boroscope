# Boroscope — Teslong TD300 macOS Viewer

## What this is
A Python-based viewer for the Teslong TD300 articulating inspection camera on macOS. The TD300 is an MFi (Made for iPhone) device that communicates via Apple's iAP2 protocol over USB. Since there's no macOS driver, this project implements the full iAP2 protocol stack and camera command protocol from scratch using pyusb.

## Running

### From source
```bash
python3 -m venv venv && source venv/bin/activate && pip install pyusb
sudo venv/bin/python3 stream.py
```

### From binary
```bash
sudo ./dist/boroscope
```

- Requires **sudo** for USB reset to break macOS accessoryd's hold on the device
- Opens http://localhost:8080 with live video feed and MJPEG stream
- Auto-reconnects if camera is disconnected
- Quit via Ctrl+C in terminal or red Quit button in browser UI
- If device is unresponsive after failed attempts, physically unplug and replug it

## Building

### Local (automatic on every commit via post-commit hook)
```bash
source venv/bin/activate
pip install pyinstaller
pyinstaller --onefile --name boroscope --add-binary "$(brew --prefix libusb)/lib/libusb-1.0.dylib:." stream.py
```

### CI/CD
GitHub Actions builds on every push to `main` and publishes to GitHub Releases. Uses macOS runner with Homebrew libusb.

## Architecture

### Files
- `stream.py` — Main viewer: iAP2 handshake, camera init, BB AA packet parser, MJPEG web server
- `PROTOCOL.md` — Full reverse-engineered protocol spec (from ZMFICamera.framework binary)
- `.githooks/post-commit` — Auto-builds binary on every commit
- `.github/workflows/build.yml` — CI/CD: build + release on push to main

### Exploration scripts (historical, from reverse engineering phase)
- `probe_device.py` — Initial USB device probing
- `iap2_handshake.py`, `iap2_connect.py` — Early handshake attempts
- `td300_connect.py`, `td300_full_connect.py` — Full protocol implementations
- `td300_viewer.py`, `web_viewer.py` — Early viewer attempts
- `usb_reset_probe.py` — USB reset experimentation
- `ea_runtime_inspect.swift`, `ea_session_hack.m`, `xpc_probe.swift` — macOS native API attempts (abandoned)
- `TeslongViewer/` — Early macOS app attempt (abandoned)

## USB Device
- **VID:** 0x3301, **PID:** 0x2003
- **Manufacturer:** Shenzhen Teslong Technology Co.
- **Firmware ID:** TESLONG NTC100, Ver.1.0.41
- **Interface 0:** iAP2 control (EP 0x01 OUT, EP 0x81 IN)
- **Interface 1:** Camera data (EP 0x02 OUT, EP 0x82 IN) — requires alt setting 1

## Protocol Stack

### 1. iAP2 Link Layer (Interface 0)
1. **Detect**: Send marker `FF 55 02 00 EE 10` on EP 0x01, receive SYN on EP 0x81
2. **Negotiate**: Send SYN+ACK (control=0xC0) with params: max_packet=4096, retransmit_timeout=3000, cumack_timeout=1500, max_retransmit=4, max_cumack=1, session_id=10
3. **Auth**: Send RequestCertificate (0xAA00), receive MFi cert (0xAA01), skip challenge, send AuthSucceeded (0xAA05)
4. **Identify**: Send StartIdentification (0x1D00), receive IdentificationInfo (0x1D01), send IdentificationAccepted (0x1D02)

### 2. Camera Protocol (Interface 1, EP 0x02/0x82)
After iAP2 handshake, set Interface 1 alt=1, then:
1. Send `BB AA 05 00 00` (GetAllInfo) → receive 512-byte device info response
2. Send `BB AA 06 00 00` (OpenStream) → receive 6-byte ack (status=0 = success)
3. Read continuous video from EP 0x82

### 3. Video Data Format
Video arrives as BB AA / AA BB framed packets with CID 0x07 or 0x0A:
```
[sync:2][cid:1][payload_len:LE16][inner_header:7][jpeg_fragment:N]
 BB AA   0x0A   varies            seq,flags,angle  JPEG data
```
- **Outer header**: 5 bytes (sync + CID + length)
- **Inner header**: 7 bytes (sequence number, reserved, button/sensor flags, 4-byte angle data)
- **JPEG data** starts at packet offset 12
- **Frame reassembly**: Accumulate fragments, new frame starts with FFD8, ends with FFD9
- **Frame size**: ~55-60KB per frame at 1280x720, ~10 fps

## Device Capabilities (NTC100 fw 1.0.41)

### DevInfo Structure (parsed from CID 0x05 response)
```
Offset  Field              Value
0x00    status             0x00 (+ 0x01 prefix)
0x02    vendor             "TESLONG" (16 bytes)
0x12    product            "NTC100" (16 bytes)
0x22    firmware           "Ver.1.0.41" (16 bytes)
0x32    device_id          01 13 60 b3 ...
0x3B    serial             5513514793029732104153325a3337
0x4A    separator          00
0x4B    cam_num            01 (1 camera)
0x4C    cam_cur            00 (camera index 0)
0x4D    res_cur            08 (resolution index 8 = 1280x720)
0x4E    res_list           00 08 (only index 8 available)
0x50+   padding            zeros
```

### Resolution
- **Video stream**: 1280x720 MJPEG only
- **CID 0x0B** (SwitchCamera/Resolution): Returns status=5 (error) for all resolution values other than 8
- The "1920x1080" in TD300 marketing likely refers to sensor capability or photo mode, not video stream
- DevInfo res_list contains only one entry (8), confirming firmware limitation

### Supported Commands
| Command | Bytes | Description | Status |
|---------|-------|-------------|--------|
| GetAllInfo | `BB AA 05 00 00` | Get device info (512b response) | Working |
| OpenStream | `BB AA 06 00 00` | Start video stream | Working |
| SwitchCamera | `BB AA 0B 03 00 [cam] [res:LE16]` | Change resolution | Rejected (status 5) |
| Sleep | `BB AA 08 00 00` | Put camera to sleep | Untested |

## Key Gotchas
- **sudo required**: USB reset (`dev.reset()`) needs root on macOS to break accessoryd's claim
- **Don't kill accessoryd**: Using `killall` or `launchctl bootout` makes things worse — accessoryd respawns and creates race conditions
- **Device state gets stuck**: After multiple failed handshake attempts, the device's iAP2 state machine gets stuck mid-session. Only a physical unplug/replug resets it.
- **Interface 1 alt=1 timing**: Must be set AFTER auth+identify completes (per MFi spec), not before
- **EA Native Transport**: Camera data flows directly on Interface 1 endpoints, NOT multiplexed through the iAP2 link layer sessions
- **PyInstaller + sudo**: macOS SIP strips DYLD_LIBRARY_PATH under sudo. Must preload bundled libusb via ctypes before importing pyusb.
- **Bare except clauses**: Must use `except Exception:` not bare `except:` — otherwise KeyboardInterrupt (Ctrl+C) gets swallowed in USB read/write loops

## Platform Support
- **macOS**: Fully working (requires sudo, Homebrew libusb)
- **Linux**: Should work — no accessoryd to fight, just needs udev rule for non-root USB access. Untested.
- **Windows**: Not supported (would need libusb-win32/WinUSB backend)
