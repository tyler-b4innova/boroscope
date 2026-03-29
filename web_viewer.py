#!/usr/bin/env python3
"""
Web-based MJPEG viewer for Teslong TD300 boroscope.

Serves a live video stream over HTTP so any browser on the local network
can display the feed.  Frames are pushed into a shared queue by the
capture layer (td300_viewer or similar) via push_frame().

    Usage from another module:
        import web_viewer
        web_viewer.push_frame(jpeg_bytes)   # call per frame
        web_viewer.start(port=8080)         # non-blocking

    Standalone test mode:
        python web_viewer.py                # synthetic pattern at ~10 fps
"""

import io
import struct
import threading
import time
import zlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty

# ---------------------------------------------------------------------------
# Shared frame queue -- fed by the capture pipeline, consumed by HTTP clients
# ---------------------------------------------------------------------------

_frame_queue: Queue = Queue(maxsize=4)
_latest_frame: bytes = b""
_latest_lock = threading.Lock()

# Metadata surfaced in the UI
_meta_lock = threading.Lock()
_meta = {
    "width": 0,
    "height": 0,
    "fps": 0.0,
    "recording": False,
}


def push_frame(jpeg_bytes: bytes, width: int = 0, height: int = 0) -> None:
    """Push a JPEG frame into the viewer pipeline.

    Called by the capture layer for every decoded frame.  Drops the oldest
    frame when the queue is full so the stream never blocks the producer.
    """
    global _latest_frame

    with _latest_lock:
        _latest_frame = jpeg_bytes

    # Non-blocking put -- drop stale frame if the queue is full
    if _frame_queue.full():
        try:
            _frame_queue.get_nowait()
        except Empty:
            pass
    try:
        _frame_queue.put_nowait(jpeg_bytes)
    except Exception:
        pass

    if width and height:
        set_meta(width=width, height=height)


def set_meta(**kwargs) -> None:
    """Update stream metadata (resolution, fps, recording state)."""
    with _meta_lock:
        _meta.update(kwargs)


def get_meta() -> dict:
    with _meta_lock:
        return dict(_meta)


# ---------------------------------------------------------------------------
# HTML / CSS / JS  -- single-page viewer
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TD300 Boroscope</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: #0a0a0a;
    color: #d4d4d4;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    user-select: none;
  }

  /* --- video area --- */
  .viewport {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    overflow: hidden;
    background: #000;
  }

  .viewport img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    display: block;
  }

  /* --- bottom bar --- */
  .bar {
    height: 48px;
    background: #141414;
    border-top: 1px solid #222;
    display: flex;
    align-items: center;
    padding: 0 16px;
    gap: 16px;
    flex-shrink: 0;
  }

  .bar .group { display: flex; align-items: center; gap: 8px; }
  .bar .spacer { flex: 1; }

  .badge {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 3px 8px;
    border-radius: 4px;
    background: #1e1e1e;
    color: #888;
    white-space: nowrap;
  }
  .badge.res { color: #6cb4ee; background: #162230; }

  /* recording dot */
  .rec-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: #444;
    transition: background 0.2s;
  }
  .rec-dot.active {
    background: #e33;
    box-shadow: 0 0 6px #e33;
    animation: pulse 1.2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }

  .btn {
    background: #1e1e1e;
    color: #ccc;
    border: 1px solid #333;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .btn:hover { background: #2a2a2a; border-color: #555; color: #fff; }
  .btn:active { background: #333; }

  /* --- connection overlay --- */
  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0,0,0,0.7);
    z-index: 10;
    transition: opacity 0.3s;
  }
  .overlay.hidden { opacity: 0; pointer-events: none; }
  .overlay span {
    color: #666;
    font-size: 14px;
    letter-spacing: 0.05em;
  }

  /* flash effect on screenshot */
  .flash {
    position: absolute; inset: 0;
    background: #fff;
    opacity: 0;
    pointer-events: none;
    z-index: 20;
  }
  .flash.fire { animation: flashAnim 0.25s ease-out; }
  @keyframes flashAnim {
    0%   { opacity: 0.6; }
    100% { opacity: 0; }
  }
</style>
</head>
<body>

<div class="viewport" id="viewport">
  <div class="overlay" id="overlay"><span>WAITING FOR STREAM</span></div>
  <div class="flash" id="flash"></div>
  <img id="stream" alt="">
</div>

<div class="bar">
  <div class="group">
    <div class="rec-dot" id="recDot"></div>
    <span class="badge" id="recLabel">IDLE</span>
  </div>
  <div class="spacer"></div>
  <span class="badge res" id="resBadge">--</span>
  <span class="badge" id="fpsBadge">-- fps</span>
  <button class="btn" id="btnScreenshot" title="Save current frame">Screenshot</button>
</div>

<script>
(function() {
  const img      = document.getElementById("stream");
  const overlay  = document.getElementById("overlay");
  const flash    = document.getElementById("flash");
  const recDot   = document.getElementById("recDot");
  const recLabel = document.getElementById("recLabel");
  const resBadge = document.getElementById("resBadge");
  const fpsBadge = document.getElementById("fpsBadge");
  const btnShot  = document.getElementById("btnScreenshot");

  /* -- start MJPEG stream -- */
  img.src = "/stream";
  img.onload = function() {
    overlay.classList.add("hidden");
  };
  img.onerror = function() {
    overlay.classList.remove("hidden");
    /* retry after a short delay */
    setTimeout(function() { img.src = "/stream?" + Date.now(); }, 2000);
  };

  /* -- poll metadata -- */
  function pollMeta() {
    fetch("/meta").then(r => r.json()).then(function(m) {
      if (m.width && m.height) {
        resBadge.textContent = m.width + " x " + m.height;
      }
      fpsBadge.textContent = m.fps.toFixed(1) + " fps";
      if (m.recording) {
        recDot.classList.add("active");
        recLabel.textContent = "REC";
        recLabel.style.color = "#e33";
      } else {
        recDot.classList.remove("active");
        recLabel.textContent = "IDLE";
        recLabel.style.color = "";
      }
    }).catch(function(){});
  }
  setInterval(pollMeta, 1000);
  pollMeta();

  /* -- screenshot -- */
  btnShot.addEventListener("click", function() {
    /* trigger flash */
    flash.classList.remove("fire");
    void flash.offsetWidth;          /* reflow to restart animation */
    flash.classList.add("fire");

    /* download the latest frame from a dedicated endpoint */
    var a = document.createElement("a");
    a.href = "/snapshot?dl=1&t=" + Date.now();
    a.download = "boroscope_" + new Date().toISOString().replace(/[:.]/g,"-") + ".jpg";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  });
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Routes: /  /stream  /snapshot  /meta"""

    # Silence per-request log lines (override to enable)
    def log_message(self, fmt, *args):
        pass

    # ---- routing ----

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            self._serve_html()
        elif path == "/stream":
            self._serve_stream()
        elif path == "/snapshot":
            self._serve_snapshot()
        elif path == "/meta":
            self._serve_meta()
        else:
            self.send_error(404)

    # ---- handlers ----

    def _serve_html(self):
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                try:
                    jpeg = _frame_queue.get(timeout=2.0)
                except Empty:
                    # No frame yet -- send an empty boundary to keep alive
                    continue

                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                    b"\r\n"
                )
                self.wfile.write(header)
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_snapshot(self):
        with _latest_lock:
            jpeg = _latest_frame

        if not jpeg:
            self.send_error(503, "No frame available")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Content-Disposition",
                         'attachment; filename="boroscope_snapshot.jpg"')
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(jpeg)

    def _serve_meta(self):
        import json
        body = json.dumps(get_meta()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class _ThreadedHTTPServer(HTTPServer):
    """HTTPServer that handles each request in a new daemon thread."""
    daemon_threads = True
    allow_reuse_address = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread,
                             args=(request, client_address),
                             daemon=True)
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_server: _ThreadedHTTPServer | None = None


def start(host: str = "0.0.0.0", port: int = 8080, blocking: bool = False):
    """Start the web viewer.

    Args:
        host:     Bind address (0.0.0.0 = all interfaces).
        port:     TCP port.
        blocking: If True, blocks the calling thread. Otherwise spawns a
                  daemon thread and returns immediately.
    """
    global _server
    _server = _ThreadedHTTPServer((host, port), _Handler)
    print(f"[viewer] http://localhost:{port}")

    if blocking:
        try:
            _server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _server.server_close()
    else:
        t = threading.Thread(target=_server.serve_forever, daemon=True)
        t.start()
        return _server


def stop():
    """Shut down a running server."""
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


# ---------------------------------------------------------------------------
# Test-pattern generator  (standalone mode)
# ---------------------------------------------------------------------------

def _build_minimal_jpeg(width: int, height: int, r: int, g: int, b: int,
                        text: str = "") -> bytes:
    """Build a valid JPEG entirely in pure Python (no Pillow).

    Generates a solid-colour image with an optional burnt-in text line
    rendered as a simple 5x7 bitmap font.  Enough for a test pattern.
    """
    # We construct an uncompressed baseline JPEG manually.  The trick is
    # to use restart markers and a trivial Huffman table so no real
    # compression is needed -- but the simplest legal approach is to build
    # raw RGB scanlines inside a JFIF wrapper using the "raw DCT" path.
    #
    # Actually the *simplest* correct approach: use zlib to deflate the
    # pixel data, but JPEG doesn't use zlib.  Instead we'll build a
    # minimal baseline JPEG by encoding 8x8 blocks of constant colour.
    #
    # For robustness we just build a BMP in memory and wrap it.  But BMP
    # isn't JPEG.  Let's do it properly -- build an actual JPEG bitstream.
    #
    # The most compact method: use a single-scan baseline JPEG with all-
    # same DCT coefficients per 8x8 block.  For a solid colour tile the
    # only non-zero coefficient is DC, so the Huffman stream is trivial.
    #
    # ... this is getting complex.  Since we *only* need this for the test
    # pattern and we explicitly have no extra deps, let's build a tiny
    # uncompressed JPEG using the "JPEG with restart interval = 1" trick:
    # after quantisation of a constant block the DC = value, AC = all 0.

    # ---- colour space: YCbCr ----
    y  = int( 0.299 * r + 0.587 * g + 0.114 * b)
    cb = int(-0.169 * r - 0.331 * g + 0.500 * b + 128)
    cr = int( 0.500 * r - 0.419 * g - 0.081 * b + 128)
    y  = max(0, min(255, y))
    cb = max(0, min(255, cb))
    cr = max(0, min(255, cr))

    # Round dimensions up to multiples of 16 (MCU = 16x16 for 4:2:0,
    # but we'll use 4:4:4 with 8x8 MCUs for simplicity)
    bw = (width  + 7) // 8
    bh = (height + 7) // 8
    real_w = bw * 8
    real_h = bh * 8

    # Build a character bitmap for overlay text
    text_pixels = set()  # set of (x, y) that should be white
    if text:
        _render_text_pixels(text, text_pixels, real_w, real_h)

    # We'll use the simplest possible JPEG structure.
    # Strategy: build each 8x8 block's DC value; AC = 0.
    # Then Huffman-encode the scan data.

    # -- Quantisation table: all 1s (no quantisation loss)
    qt = bytes([1] * 64)

    # -- Huffman tables --
    # DC table (class 0): we need to encode DC differences.
    # For a constant image every DC diff after the first is 0.
    # Category 0 = code 00 (2 bits).  For the first DC we need the
    # category that fits the value.
    #
    # We'll use standard JPEG Huffman tables (K.3 from the spec).

    # Standard DC luminance
    dc_lum_bits  = bytes([0,1,5,1,1,1,1,1,1,0,0,0,0,0,0,0])
    dc_lum_vals  = bytes([0,1,2,3,4,5,6,7,8,9,10,11])

    # Standard DC chrominance
    dc_chr_bits  = bytes([0,3,1,1,1,1,1,1,1,1,1,0,0,0,0,0])
    dc_chr_vals  = bytes([0,1,2,3,4,5,6,7,8,9,10,11])

    # Standard AC luminance
    ac_lum_bits  = bytes([0,2,1,3,3,2,4,3,5,5,4,4,0,0,1,0x7d])
    ac_lum_vals  = bytes([
        0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,
        0x13,0x51,0x61,0x07,0x22,0x71,0x14,0x32,0x81,0x91,0xa1,0x08,
        0x23,0x42,0xb1,0xc1,0x15,0x52,0xd1,0xf0,0x24,0x33,0x62,0x72,
        0x82,0x09,0x0a,0x16,0x17,0x18,0x19,0x1a,0x25,0x26,0x27,0x28,
        0x29,0x2a,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,
        0x46,0x47,0x48,0x49,0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,
        0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,0x6a,0x73,0x74,0x75,
        0x76,0x77,0x78,0x79,0x7a,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
        0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,
        0xa4,0xa5,0xa6,0xa7,0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,
        0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,0xc6,0xc7,0xc8,0xc9,
        0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,0xe1,0xe2,
        0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf1,0xf2,0xf3,0xf4,
        0xf5,0xf6,0xf7,0xf8,0xf9,0xfa,
    ])

    # Standard AC chrominance
    ac_chr_bits  = bytes([0,2,1,2,4,4,3,4,7,5,4,4,0,1,2,0x77])
    ac_chr_vals  = bytes([
        0x00,0x01,0x02,0x03,0x11,0x04,0x05,0x21,0x31,0x06,0x12,0x41,
        0x51,0x07,0x61,0x71,0x13,0x22,0x32,0x81,0x08,0x14,0x42,0x91,
        0xa1,0xb1,0xc1,0x09,0x23,0x33,0x52,0xf0,0x15,0x62,0x72,0xd1,
        0x0a,0x16,0x24,0x34,0xe1,0x25,0xf1,0x17,0x18,0x19,0x1a,0x26,
        0x27,0x28,0x29,0x2a,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,
        0x45,0x46,0x47,0x48,0x49,0x4a,0x53,0x54,0x55,0x56,0x57,0x58,
        0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,0x6a,0x73,0x74,
        0x75,0x76,0x77,0x78,0x79,0x7a,0x82,0x83,0x84,0x85,0x86,0x87,
        0x88,0x89,0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,
        0xa2,0xa3,0xa4,0xa5,0xa6,0xa7,0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,
        0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,0xc6,0xc7,
        0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,
        0xe2,0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf2,0xf3,0xf4,
        0xf5,0xf6,0xf7,0xf8,0xf9,0xfa,
    ])

    # Build Huffman encoder lookup from bits/vals
    def build_huff_enc(bits, vals):
        """Return {symbol: (code, length)} from JPEG DHT spec arrays."""
        table = {}
        code = 0
        vi = 0
        for length in range(1, 17):
            for _ in range(bits[length - 1]):
                table[vals[vi]] = (code, length)
                code += 1
                vi += 1
            code <<= 1
        return table

    dc_lum_enc = build_huff_enc(dc_lum_bits, dc_lum_vals)
    dc_chr_enc = build_huff_enc(dc_chr_bits, dc_chr_vals)
    ac_lum_enc = build_huff_enc(ac_lum_bits, ac_lum_vals)
    ac_chr_enc = build_huff_enc(ac_chr_bits, ac_chr_vals)

    # Bitstream writer
    class BitWriter:
        def __init__(self):
            self.data = bytearray()
            self.buf = 0
            self.bits = 0

        def write(self, code, length):
            self.buf = (self.buf << length) | code
            self.bits += length
            while self.bits >= 8:
                self.bits -= 8
                byte = (self.buf >> self.bits) & 0xFF
                self.data.append(byte)
                if byte == 0xFF:
                    self.data.append(0x00)  # byte-stuff

        def flush(self):
            if self.bits > 0:
                self.buf <<= (8 - self.bits)
                byte = (self.buf >> 0) & 0xFF
                self.data.append(byte)
                if byte == 0xFF:
                    self.data.append(0x00)
                self.bits = 0
                self.buf = 0

    def encode_dc(bw: BitWriter, diff: int, huff):
        if diff == 0:
            cat = 0
        else:
            cat = diff.bit_length() if diff > 0 else (-diff).bit_length()
        code, length = huff[cat]
        bw.write(code, length)
        if cat > 0:
            if diff < 0:
                diff = diff - 1  # one's complement for negatives
                diff &= (1 << cat) - 1
            bw.write(diff, cat)

    def encode_block_dc_only(bw: BitWriter, diff: int, dc_huff, ac_huff):
        """Encode an 8x8 block with only a DC coefficient (AC = all zero)."""
        encode_dc(bw, diff, dc_huff)
        # EOB for AC
        code, length = ac_huff[0x00]  # EOB symbol
        bw.write(code, length)

    # Quantised DC value for a constant block = round(8 * value / quant[0])
    # Since quant[0] = 1, DC = 8 * value.  But actually, for baseline JPEG
    # the DCT of a constant 8x8 block: DC coefficient = mean * 8.
    # After quantisation with Q=1: DC_q = round(mean * 8).
    # This fits in the range [0, 2040] for pixel values 0-255.
    dc_y  = y  * 8
    dc_cb = (cb - 128) * 8  # level-shifted
    dc_cr = (cr - 128) * 8

    # Actually, JPEG level-shifts by 128 before DCT.  So for pixel value p:
    # shifted = p - 128;  DCT DC = shifted * 8;  quantised = shifted * 8 / Q[0]
    dc_y = (y - 128) * 8

    # For text overlay we need per-block Y values.  We'll precompute a grid.
    # If a block has any text pixels, we make it bright (Y=220), otherwise
    # use the background colour Y value.

    bw_stream = BitWriter()
    prev_dc_y  = 0
    prev_dc_cb = 0
    prev_dc_cr = 0

    for by in range(bh):
        for bx in range(bw):
            # Check if this 8x8 block overlaps text
            has_text = False
            if text_pixels:
                for py in range(by * 8, by * 8 + 8):
                    for px in range(bx * 8, bx * 8 + 8):
                        if (px, py) in text_pixels:
                            has_text = True
                            break
                    if has_text:
                        break

            if has_text:
                block_dc_y = (220 - 128) * 8  # bright white-ish
            else:
                block_dc_y = dc_y

            diff = block_dc_y - prev_dc_y
            encode_block_dc_only(bw_stream, diff, dc_lum_enc, ac_lum_enc)
            prev_dc_y = block_dc_y

            # Cb
            diff = dc_cb - prev_dc_cb
            encode_block_dc_only(bw_stream, diff, dc_chr_enc, ac_chr_enc)
            prev_dc_cb = dc_cb

            # Cr
            diff = dc_cr - prev_dc_cr
            encode_block_dc_only(bw_stream, diff, dc_chr_enc, ac_chr_enc)
            prev_dc_cr = dc_cr

    bw_stream.flush()
    scan_data = bytes(bw_stream.data)

    # ---- Assemble JPEG ----
    out = bytearray()

    # SOI
    out += b'\xFF\xD8'

    # APP0 (JFIF)
    app0 = struct.pack(">H", 16) + b'JFIF\x00' + struct.pack(">BBHHHBB",
        1, 1,  # version
        0,     # units (0 = no units)
        1, 1,  # X/Y density
        0, 0,  # thumbnail
    )
    out += b'\xFF\xE0' + app0

    # DQT (quantisation table, id=0 -- used for all components)
    dqt_payload = struct.pack(">H", 67) + b'\x00' + qt
    out += b'\xFF\xDB' + dqt_payload

    # SOF0 (Start Of Frame, baseline, 4:4:4)
    sof = struct.pack(">HBHH", 11 + 3*3, 8, real_h, real_w)
    sof += b'\x03'  # 3 components
    sof += b'\x01\x11\x00'  # Y:  id=1, sampling 1x1, quant table 0
    sof += b'\x02\x11\x00'  # Cb: id=2, sampling 1x1, quant table 0
    sof += b'\x03\x11\x00'  # Cr: id=3, sampling 1x1, quant table 0
    out += b'\xFF\xC0' + sof

    # DHT (Huffman tables) -- 4 tables
    def dht_segment(cls, tid, bits, vals):
        payload = bytes([cls << 4 | tid]) + bits + vals
        return struct.pack(">H", 2 + len(payload)) + payload

    out += b'\xFF\xC4' + dht_segment(0, 0, dc_lum_bits, dc_lum_vals)
    out += b'\xFF\xC4' + dht_segment(0, 1, dc_chr_bits, dc_chr_vals)
    out += b'\xFF\xC4' + dht_segment(1, 0, ac_lum_bits, ac_lum_vals)
    out += b'\xFF\xC4' + dht_segment(1, 1, ac_chr_bits, ac_chr_vals)

    # SOS (Start Of Scan)
    sos = struct.pack(">HB", 12, 3)  # length=12, 3 components
    sos += b'\x01\x00'   # Y  -> DC table 0, AC table 0
    sos += b'\x02\x11'   # Cb -> DC table 1, AC table 1
    sos += b'\x03\x11'   # Cr -> DC table 1, AC table 1
    sos += b'\x00\x3F\x00'  # Ss=0, Se=63, Ah/Al=0
    out += b'\xFF\xDA' + sos

    # Scan data
    out += scan_data

    # EOI
    out += b'\xFF\xD9'

    return bytes(out)


# Tiny 5x7 bitmap font for digits, colon, space, and basic uppercase.
_FONT = {
    ' ': [0b00000]*7,
    '0': [0b01110,0b10001,0b10011,0b10101,0b11001,0b10001,0b01110],
    '1': [0b00100,0b01100,0b00100,0b00100,0b00100,0b00100,0b01110],
    '2': [0b01110,0b10001,0b00001,0b00110,0b01000,0b10000,0b11111],
    '3': [0b01110,0b10001,0b00001,0b00110,0b00001,0b10001,0b01110],
    '4': [0b00010,0b00110,0b01010,0b10010,0b11111,0b00010,0b00010],
    '5': [0b11111,0b10000,0b11110,0b00001,0b00001,0b10001,0b01110],
    '6': [0b00110,0b01000,0b10000,0b11110,0b10001,0b10001,0b01110],
    '7': [0b11111,0b00001,0b00010,0b00100,0b01000,0b01000,0b01000],
    '8': [0b01110,0b10001,0b10001,0b01110,0b10001,0b10001,0b01110],
    '9': [0b01110,0b10001,0b10001,0b01111,0b00001,0b00010,0b01100],
    ':': [0b00000,0b00100,0b00000,0b00000,0b00100,0b00000,0b00000],
    '.': [0b00000,0b00000,0b00000,0b00000,0b00000,0b00000,0b00100],
    '-': [0b00000,0b00000,0b00000,0b11111,0b00000,0b00000,0b00000],
    'T': [0b11111,0b00100,0b00100,0b00100,0b00100,0b00100,0b00100],
    'E': [0b11111,0b10000,0b10000,0b11110,0b10000,0b10000,0b11111],
    'S': [0b01110,0b10001,0b10000,0b01110,0b00001,0b10001,0b01110],
    'D': [0b11100,0b10010,0b10001,0b10001,0b10001,0b10010,0b11100],
    'P': [0b11110,0b10001,0b10001,0b11110,0b10000,0b10000,0b10000],
    'A': [0b01110,0b10001,0b10001,0b11111,0b10001,0b10001,0b10001],
    'R': [0b11110,0b10001,0b10001,0b11110,0b10100,0b10010,0b10001],
    'N': [0b10001,0b11001,0b10101,0b10011,0b10001,0b10001,0b10001],
    'F': [0b11111,0b10000,0b10000,0b11110,0b10000,0b10000,0b10000],
    'I': [0b01110,0b00100,0b00100,0b00100,0b00100,0b00100,0b01110],
    'X': [0b10001,0b01010,0b00100,0b00100,0b01010,0b10001,0b10001],
    'L': [0b10000,0b10000,0b10000,0b10000,0b10000,0b10000,0b11111],
    'O': [0b01110,0b10001,0b10001,0b10001,0b10001,0b10001,0b01110],
    'G': [0b01110,0b10001,0b10000,0b10111,0b10001,0b10001,0b01110],
    'V': [0b10001,0b10001,0b10001,0b10001,0b10001,0b01010,0b00100],
    'W': [0b10001,0b10001,0b10001,0b10101,0b10101,0b11011,0b10001],
    'H': [0b10001,0b10001,0b10001,0b11111,0b10001,0b10001,0b10001],
    'M': [0b10001,0b11011,0b10101,0b10101,0b10001,0b10001,0b10001],
}


def _render_text_pixels(text: str, pixel_set: set, img_w: int, img_h: int,
                        scale: int = 2, margin: int = 12):
    """Render text into pixel_set as {(x,y)} coordinates using the bitmap font."""
    cx = margin
    cy = margin
    for ch in text.upper():
        glyph = _FONT.get(ch)
        if glyph is None:
            cx += 6 * scale
            continue
        for row_i, row in enumerate(glyph):
            for col in range(5):
                if row & (1 << (4 - col)):
                    for sy in range(scale):
                        for sx in range(scale):
                            px = cx + col * scale + sx
                            py = cy + row_i * scale + sy
                            if 0 <= px < img_w and 0 <= py < img_h:
                                pixel_set.add((px, py))
        cx += 6 * scale


def _test_pattern_loop():
    """Generate coloured test frames at ~10 fps with a timestamp overlay."""
    import datetime

    WIDTH, HEIGHT = 640, 480
    set_meta(width=WIDTH, height=HEIGHT)

    colours = [
        (40, 80, 160),   # steel blue
        (50, 140, 80),   # forest green
        (160, 60, 60),   # brick red
        (140, 100, 40),  # bronze
        (80, 50, 130),   # purple
        (30, 120, 130),  # teal
    ]

    frame_count = 0
    t0 = time.monotonic()

    while True:
        now = datetime.datetime.now()
        ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 100000}"
        idx = (frame_count // 20) % len(colours)
        r, g, b = colours[idx]

        label = f"TD300 TEST  {ts}  F{frame_count}"
        jpeg = _build_minimal_jpeg(WIDTH, HEIGHT, r, g, b, text=label)

        push_frame(jpeg, width=WIDTH, height=HEIGHT)
        frame_count += 1

        # Update FPS estimate every 10 frames
        if frame_count % 10 == 0:
            elapsed = time.monotonic() - t0
            if elapsed > 0:
                set_meta(fps=round(frame_count / elapsed, 1))

        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Main -- test mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("TD300 Web Viewer -- test pattern mode")
    print("Open http://localhost:8080 in your browser")
    print("Press Ctrl+C to stop\n")

    start(port=8080, blocking=False)

    try:
        _test_pattern_loop()
    except KeyboardInterrupt:
        print("\nShutting down.")
        stop()
