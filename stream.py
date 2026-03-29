#!/usr/bin/env python3
"""
Teslong TD300 Boroscope Viewer
Connects via iAP2, starts camera, streams MJPEG to browser at http://localhost:8080
"""
import struct, time, sys, os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# When running as PyInstaller bundle under sudo, macOS SIP strips DYLD paths.
# Explicitly load bundled libusb so pyusb can find it.
if getattr(sys, 'frozen', False):
    _libusb = os.path.join(sys._MEIPASS, 'libusb-1.0.dylib')
    if os.path.exists(_libusb):
        import ctypes
        ctypes.cdll.LoadLibrary(_libusb)

import usb.core, usb.util

VID, PID = 0x3301, 0x2003
MK = b'\xFF\x55\x02\x00\xEE\x10'
LS = 0xFF5A; SYN, ACK = 0x80, 0x40; CSS = 0x4040; CSID = 10

latest_frame = None
frame_lock = threading.Lock()
frame_count = 0

def gc(d): return (-sum(d) & 0xFF) & 0xFF
def lp(pl, c, sq, ak, sid=0):
    n = (len(pl) + 10) if pl else 9
    h = struct.pack('>HHBBBB', LS, n, c, sq, ak, sid)
    h += bytes([gc(h)])
    return (h + pl + bytes([gc(pl)])) if pl else h
def cm(mid, p=b''): return struct.pack('>HHH', CSS, 6 + len(p), mid) + p


def iap2_handshake(dev):
    """Complete iAP2 handshake: detect → negotiate → auth → identify"""
    tx, rx = 0, 0

    def rd(ep=0x81, to=2000):
        try: return bytes(dev.read(ep, 65536, timeout=to))
        except Exception: return None
    def wr(d, ep=0x01):
        try: dev.write(ep, d, timeout=2000); return True
        except Exception: return False

    # Detect
    for _ in range(5): rd(to=200)
    wr(MK)
    syn = None
    for _ in range(10):
        d = rd(to=1500)
        if not d: wr(MK); continue
        idx = d.find(b'\xff\x5a')
        if idx >= 0: syn = d[idx:]; break
    if not syn: return None, None
    rx = syn[5]

    # Negotiate
    s = struct.pack('>BBHHHBB', 1, 1, 4096, 3000, 1500, 4, 1) + bytes([CSID, 0, 1])
    wr(lp(s, SYN | ACK, tx, rx))
    for _ in range(10):
        d = rd(to=2000)
        if not d: continue
        if d[0] == 0xFF and d[1] == 0x5A:
            if (d[4] & ACK) and not (d[4] & SYN): break
            elif d[4] & SYN: rx = d[5]; wr(lp(s, SYN | ACK, tx, rx))
    else: return None, None

    # Auth (skip challenge)
    tx = (tx + 1) & 0xFF; wr(lp(cm(0xAA00), ACK, tx, rx, CSID))
    for _ in range(15):
        d = rd(to=3000)
        if not d: continue
        if len(d) >= 9 and d[0] == 0xFF and d[1] == 0x5A and d[7] == CSID:
            nn = struct.unpack('>H', d[2:4])[0]
            if nn > 9:
                rx = d[5]; wr(lp(None, ACK, tx, rx))
                pl = d[9:nn-1]
                if len(pl) >= 6 and struct.unpack('>HHH', pl[:6])[2] == 0xAA01: break
    time.sleep(0.1)
    tx = (tx + 1) & 0xFF; wr(lp(cm(0xAA05), ACK, tx, rx, CSID))
    time.sleep(0.3); [rd(to=300) for _ in range(5)]

    # Identify
    tx = (tx + 1) & 0xFF; wr(lp(cm(0x1D00), ACK, tx, rx, CSID))
    for _ in range(20):
        d = rd(to=3000)
        if not d: continue
        if len(d) >= 9 and d[0] == 0xFF and d[1] == 0x5A and d[7] == CSID:
            nn = struct.unpack('>H', d[2:4])[0]
            if nn > 9:
                rx = d[5]; wr(lp(None, ACK, tx, rx))
                pl = d[9:nn-1]
                if len(pl) >= 6 and struct.unpack('>HHH', pl[:6])[2] == 0x1D01:
                    tx = (tx + 1) & 0xFF; wr(lp(cm(0x1D02), ACK, tx, rx, CSID))
                    break
    else: return None, None

    time.sleep(0.3); [rd(to=300) for _ in range(5)]
    return tx, rx


def camera_thread(dev):
    """Read video from the camera and update latest_frame"""
    global latest_frame, frame_count

    # Activate data interface
    dev.set_interface_altsetting(interface=1, alternate_setting=1)
    time.sleep(0.3)

    def rd82(to=100):
        try: return bytes(dev.read(0x82, 65536, timeout=to))
        except Exception: return None
    def wr02(d):
        try: dev.write(0x02, d, timeout=2000)
        except Exception: pass

    # Get device info (CID 0x05) — parse for resolution capabilities
    wr02(b'\xBB\xAA\x05\x00\x00')
    time.sleep(0.3)
    info = rd82(500)
    if info and len(info) >= 5:
        payload = info[5:]
        if len(payload) >= 0x30:
            vendor = payload[0x00:0x10].rstrip(b'\x00').decode('utf-8', errors='replace')
            product = payload[0x10:0x20].rstrip(b'\x00').decode('utf-8', errors='replace')
            version = payload[0x20:0x30].rstrip(b'\x00').decode('utf-8', errors='replace')
            print(f"[CAMERA] {vendor} {product} fw={version}")
        # Dump full payload for analysis
        print(f"[CAMERA] DevInfo ({len(payload)}b): {payload.hex()}")

    # Note: NTC100 firmware only supports res_cur=8 (1280x720).
    # CID 0x0B switch command returns status=5 (error) for all other values.

    # Open stream (CID 0x06)
    wr02(b'\xBB\xAA\x06\x00\x00')
    time.sleep(0.2)
    rd82(500)  # read ack

    print("[CAMERA] Stream opened, reading frames...")

    # Read loop: parse BB AA / AA BB packets, extract JPEG from CID 0x07/0x0A
    rx_buf = bytearray()
    jpeg_buf = bytearray()

    while True:
        try:
            d = rd82(200)
            if not d:
                continue

            rx_buf.extend(d)

            # Parse packets from buffer
            while len(rx_buf) >= 5:
                # Find sync word (BB AA or AA BB)
                sync_pos = -1
                for i in range(len(rx_buf) - 1):
                    if (rx_buf[i] == 0xBB and rx_buf[i+1] == 0xAA) or \
                       (rx_buf[i] == 0xAA and rx_buf[i+1] == 0xBB):
                        sync_pos = i
                        break

                if sync_pos < 0:
                    rx_buf.clear()
                    break
                if sync_pos > 0:
                    rx_buf = rx_buf[sync_pos:]

                if len(rx_buf) < 5:
                    break

                cid = rx_buf[2]
                payload_len = struct.unpack('<H', rx_buf[3:5])[0]
                total_len = 5 + payload_len

                if total_len > 65536:
                    rx_buf = rx_buf[2:]  # skip bad sync
                    continue

                if len(rx_buf) < total_len:
                    break  # need more data

                # Extract complete packet
                pkt = bytes(rx_buf[:total_len])
                rx_buf = rx_buf[total_len:]

                # CID 0x07 or 0x0A = video frame data
                if cid in (0x07, 0x0A) and payload_len > 7:
                    # Skip 7-byte inner header (seq, reserved, flags, angle)
                    jpeg_data = pkt[12:]  # offset 5 (outer) + 7 (inner)

                    # Check for JPEG start marker = new frame
                    if len(jpeg_data) >= 2 and jpeg_data[0] == 0xFF and jpeg_data[1] == 0xD8:
                        # Flush previous frame if complete
                        if jpeg_buf:
                            end = len(jpeg_buf)
                            while end > 2 and jpeg_buf[end-1] == 0:
                                end -= 1
                            if end > 1000:
                                frame = bytes(jpeg_buf[:end])
                                with frame_lock:
                                    latest_frame = frame
                                    frame_count += 1
                                if frame_count <= 5 or frame_count % 30 == 0:
                                    print(f"[CAMERA] Frame #{frame_count} ({len(frame)} bytes)")
                        jpeg_buf = bytearray(jpeg_data)
                    else:
                        jpeg_buf.extend(jpeg_data)

        except Exception as e:
            print(f"[CAMERA] Error: {e}")
            break


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            last_id = 0
            try:
                while True:
                    with frame_lock:
                        f = latest_frame
                        fid = frame_count
                    if f and fid != last_id:
                        last_id = fid
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n')
                        self.wfile.write(f'Content-Length: {len(f)}\r\n'.encode())
                        self.wfile.write(b'\r\n')
                        self.wfile.write(f)
                        self.wfile.write(b'\r\n')
                        self.wfile.flush()
                    else:
                        time.sleep(0.03)
            except: pass
        elif self.path == '/snapshot':
            with frame_lock:
                f = latest_frame
            if f:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Disposition', 'attachment; filename=td300_snapshot.jpg')
                self.send_header('Content-Length', str(len(f)))
                self.end_headers()
                self.wfile.write(f)
            else:
                self.send_response(503)
                self.end_headers()
        elif self.path == '/quit':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Shutting down...')
            threading.Thread(target=lambda: os._exit(0), daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TD300 Boroscope</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;color:#eee;font-family:system-ui;display:flex;flex-direction:column;height:100vh}
.bar{display:flex;align-items:center;justify-content:space-between;padding:8px 16px;background:#1a1a1a;border-bottom:1px solid #333}
.bar h1{font-size:16px;font-weight:500;color:#8cf}
.bar .controls{display:flex;gap:8px}
.bar button{background:#333;color:#eee;border:1px solid #555;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px}
.bar button:hover{background:#444}
.bar .status{font-size:12px;color:#888}
.view{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#000}
.view img{max-width:100%;max-height:100%;object-fit:contain}
</style></head><body>
<div class="bar">
  <h1>TD300 Boroscope</h1>
  <div class="controls">
    <span class="status" id="status">Connecting...</span>
    <button onclick="snapshot()">Screenshot</button>
    <button onclick="if(confirm('Stop viewer?'))fetch('/quit')" style="color:#f88">Quit</button>
  </div>
</div>
<div class="view">
  <img id="feed" src="/stream" onerror="err()" onload="ok()">
</div>
<script>
let fc=0;
function ok(){fc++;document.getElementById('status').textContent='Live ('+fc+' frames)'}
function err(){document.getElementById('status').textContent='Disconnected';setTimeout(()=>{document.getElementById('feed').src='/stream?t='+Date.now()},2000)}
function snapshot(){window.open('/snapshot','_blank')}
</script></body></html>"""


camera_alive = threading.Event()


def connect_and_stream():
    """Connect to TD300, handshake, start camera. Returns when camera thread dies."""
    global latest_frame, frame_count

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if not dev:
        print("[*] Waiting for TD300...")
        while not dev:
            time.sleep(1)
            dev = usb.core.find(idVendor=VID, idProduct=PID)
    print(f"Found: {dev.product}")

    print("[*] USB reset...")
    dev.reset()
    time.sleep(2)
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if not dev:
        time.sleep(1)
        dev = usb.core.find(idVendor=VID, idProduct=PID)
    if not dev:
        print("[!] Device lost after reset")
        return

    for i in [0, 1]:
        try:
            if dev.is_kernel_driver_active(i): dev.detach_kernel_driver(i)
        except Exception: pass
    try: dev.set_configuration()
    except Exception: pass
    for i in [0, 1]:
        try: usb.util.claim_interface(dev, i)
        except Exception: pass

    print("[*] iAP2 handshake...")
    tx, rx = iap2_handshake(dev)
    if tx is None:
        print("[!] Handshake failed")
        return
    print("[OK] Handshake complete")

    camera_alive.set()
    t = threading.Thread(target=camera_thread, args=(dev,), daemon=True)
    t.start()
    t.join()  # Block until camera thread dies
    camera_alive.clear()
    print("[!] Camera disconnected")

    for i in [0, 1]:
        try: usb.util.release_interface(dev, i)
        except Exception: pass


def main():
    print("=" * 50)
    print("  Teslong TD300 Boroscope Viewer")
    print("=" * 50)

    # Start web server in background
    port = 8080
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(('0.0.0.0', port), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[*] Open http://localhost:{port} in your browser")

    # Connect loop — reconnects on disconnect
    try:
        while True:
            connect_and_stream()
            print("[*] Reconnecting in 3s...")
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nStopping...")
        server.shutdown()


if __name__ == '__main__':
    main()
