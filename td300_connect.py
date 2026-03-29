#!/usr/bin/env python3
"""TD300 iAP2 Host — proper ACK management for max_outgoing=1 device"""
import usb.core, usb.util, struct, os, time, sys

VID, PID = 0x3301, 0x2003
MARKER = b'\xFF\x55\x02\x00\xEE\x10'
LS = 0xFF5A
SYN, ACK = 0x80, 0x40
CSM_S = 0x4040
CS_ID = 10  # control session link ID

def cs(d): return (-sum(d)&0xFF)&0xFF
def lp(pl, c, sq, ak, sid=0):
    n = (len(pl)+10) if pl else 9
    h = struct.pack('>HHBBBB', LS, n, c, sq, ak, sid)
    h += bytes([cs(h)])
    return (h+pl+bytes([cs(pl)])) if pl else h
def cm(mid, p=b''): return struct.pack('>HHH', CSM_S, 6+len(p), mid)+p
def cp(pid, d): return struct.pack('>HH', 4+len(d), pid)+d
def pl(d):
    """Parse link packet, return (ctrl, seq, ack, sid, payload) or None"""
    if len(d)<9 or d[0]!=0xFF or d[1]!=0x5A: return None
    n = struct.unpack('>H',d[2:4])[0]
    return (d[4], d[5], d[6], d[7], d[9:n-1] if n>9 and len(d)>=n else None)

class TD:
    def __init__(self):
        self.dev = None
        self.tx = 0   # our sent seq
        self.rx = 0   # last received device seq
        self.dack = 0 # last ack device sent us (acknowledging our packets)

    def rd(self, to=2000):
        try: return bytes(self.dev.read(0x81, 65536, timeout=to))
        except: return None

    def wr(self, d):
        try: self.dev.write(0x01, d, timeout=2000); return True
        except Exception as e: print(f'  WR ERR: {e}'); return False

    def send_ack(self):
        """Send ACK-only packet (no data, no seq increment)"""
        self.wr(lp(None, ACK, self.tx, self.rx))

    def send_data(self, payload, sid=0):
        """Send data packet, increment seq, includes ACK piggybacked"""
        self.tx = (self.tx+1) & 0xFF
        return self.wr(lp(payload, ACK, self.tx, self.rx, sid))

    def send_csm(self, mid, params=b''):
        return self.send_data(cm(mid, params), CS_ID)

    def wait_for_ack(self, timeout=5000):
        """Wait until device ACKs our last sent packet"""
        start = time.time()
        while time.time()-start < timeout/1000:
            d = self.rd(to=1000)
            if not d: continue
            p = pl(d)
            if not p: continue
            ctrl, seq, ack, sid, payload = p
            if ctrl & ACK:
                self.dack = ack
                if ack == self.tx:
                    return True
            # Also process any data that comes with the ACK
            if payload and sid == CS_ID:
                return ('csm', d)  # Return the packet for processing
        return False

    def read_csm_response(self, expected_mid=None, timeout=5000):
        """Read and return a CSM response, handling ACKs along the way"""
        start = time.time()
        while time.time()-start < timeout/1000:
            d = self.rd(to=1000)
            if not d: continue
            p = pl(d)
            if not p: continue
            ctrl, seq, ack, sid, payload = p

            # Track device ACKs
            if ctrl & ACK:
                self.dack = ack

            # Handle data from control session
            if payload and sid == CS_ID:
                self.rx = seq
                self.send_ack()  # ACK the data

                if len(payload) >= 6:
                    s, l, mid = struct.unpack('>HHH', payload[:6])
                    if s == CSM_S:
                        params = payload[6:l] if l > 6 else b''
                        print(f'  <- CSM 0x{mid:04X} ({len(params)}b)')
                        if expected_mid is None or mid == expected_mid:
                            return (mid, params)

            # Handle data from EA session
            if payload and sid == 11:
                self.rx = seq
                if len(d) % 5 == 0: self.send_ack()
                return ('ea', payload)

        return None

    def connect(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if not self.dev:
            print('Device not found!'); return False
        print(f'[OK] {self.dev.product}')

        for i in [0,1]:
            try:
                if self.dev.is_kernel_driver_active(i): self.dev.detach_kernel_driver(i)
            except: pass
        try: self.dev.set_configuration()
        except: pass
        for i in [0,1]:
            try: usb.util.claim_interface(self.dev, i)
            except: pass
        try: self.dev.set_interface_altsetting(interface=1, alternate_setting=1)
        except: pass

        # --- DETECT ---
        print('\n[1] Detect')
        for _ in range(5): self.rd(200)
        self.wr(MARKER)

        syn_raw = None
        for i in range(10):
            d = self.rd(1500)
            if not d: self.wr(MARKER); continue
            idx = d.find(b'\xff\x5a')
            if idx >= 0:
                syn_raw = d[idx:]
                break
        if not syn_raw:
            print('  FAIL: No SYN'); return False
        print('  OK: Got SYN')

        # --- NEGOTIATE ---
        print('[2] Negotiate')
        length = struct.unpack('>H', syn_raw[2:4])[0]
        self.rx = syn_raw[5]
        our_syn = struct.pack('>BBHHHBB', 0x01, 1, 4096, 3000, 1500, 4, 1)
        our_syn += bytes([CS_ID, 0, 1])
        self.wr(lp(our_syn, SYN|ACK, self.tx, self.rx))

        for _ in range(10):
            d = self.rd(2000)
            if not d: continue
            p = pl(d)
            if not p: continue
            ctrl = p[0]
            if (ctrl&ACK) and not (ctrl&SYN):
                print('  OK: Link UP')
                break
            elif ctrl&SYN:
                self.rx = p[1]
                self.wr(lp(our_syn, SYN|ACK, self.tx, self.rx))
        else:
            print('  FAIL: negotiate'); return False

        # --- AUTH ---
        print('[3] Auth')
        self.send_csm(0xAA00)
        print('  Sent RequestAuthCert')

        # Wait for device to ACK our request, then read cert
        result = self.read_csm_response(0xAA01, timeout=10000)
        if not result:
            print('  FAIL: No cert'); return False
        mid, cert = result
        print(f'  Got MFi cert ({len(cert)}b)')

        # Wait a moment to ensure device is ready
        time.sleep(0.1)

        # Send challenge - WAIT for ACK of previous message first
        challenge = os.urandom(20)
        self.send_csm(0xAA02, cp(0, challenge))
        print('  Sent challenge')

        # Read auth response (device's MFi chip needs ~100ms)
        result = self.read_csm_response(0xAA03, timeout=10000)
        if not result:
            print('  FAIL: No auth response'); return False
        mid, response = result
        print(f'  Got auth response ({len(response)}b)')

        # Send AuthSucceeded
        time.sleep(0.1)
        self.send_csm(0xAA05)
        print('  Sent AuthSucceeded')

        # Wait for ACK
        time.sleep(0.3)
        # Drain any pending packets
        for _ in range(5):
            d = self.rd(300)
            if d:
                p = pl(d)
                if p and p[4] and p[3] == CS_ID:
                    self.rx = p[1]
                    self.send_ack()
                    if len(p[4]) >= 6:
                        s,l,mid = struct.unpack('>HHH', p[4][:6])
                        if s == CSM_S:
                            print(f'  <- CSM 0x{mid:04X}')

        # --- IDENTIFY ---
        print('[4] Identify')
        self.send_csm(0x1D00)
        print('  Sent StartIdentification')

        result = self.read_csm_response(0x1D01, timeout=10000)
        if not result:
            print('  FAIL: No identification'); return False
        mid, params = result

        # Parse identification
        off = 0
        while off+4 <= len(params):
            plen, pid = struct.unpack('>HH', params[off:off+4])
            if plen == 0: break
            pd = params[off+4:off+plen]
            if pid == 0: print(f'  Name: {pd.rstrip(b"\\x00").decode("utf-8", errors="replace")}')
            elif pid == 2: print(f'  Manufacturer: {pd.rstrip(b"\\x00").decode("utf-8", errors="replace")}')
            elif pid == 10: print(f'  EA Protocol ({len(pd)}b)')
            off += plen

        time.sleep(0.1)
        self.send_csm(0x1D02)
        print('  Sent IdentificationAccepted')
        time.sleep(0.3)

        # Drain any pending
        for _ in range(5):
            d = self.rd(300)
            if d:
                p = pl(d)
                if p: self.rx = p[1]; self.send_ack()

        # --- EA SESSION ---
        print('[5] EA Session')
        time.sleep(0.1)
        ea_p = cp(0, struct.pack('>B', 1)) + cp(1, struct.pack('>H', 1))
        self.send_csm(0xEA00, ea_p)
        print('  Sent StartEAPSession (proto=1, session=1)')

        # Check for EA session status
        time.sleep(0.5)
        for _ in range(5):
            d = self.rd(1000)
            if d:
                p = pl(d)
                if p:
                    ctrl, seq, ack, sid, payload = p
                    self.rx = seq
                    self.send_ack()
                    if payload and sid == CS_ID and len(payload) >= 6:
                        s,l,mid = struct.unpack('>HHH', payload[:6])
                        if s == CSM_S:
                            print(f'  <- CSM 0x{mid:04X}')
                    elif payload and sid == 11:
                        print(f'  EA data! {len(payload)}b')

        # --- VIDEO ---
        print('\n[6] VIDEO STREAM')
        total = 0
        frames = 0
        out = '/tmp/td300_video.bin'
        with open(out, 'wb') as f:
            for i in range(1000):
                d = self.rd(2000)
                if not d: continue

                off = 0
                while off < len(d):
                    if off+9 > len(d): break
                    if d[off] == 0xFF and d[off+1] == 0x5A:
                        n = struct.unpack('>H', d[off+2:off+4])[0]
                        end = off + n
                        if end > len(d): break

                        p = pl(d[off:end])
                        off = end
                        if not p: continue
                        ctrl, seq, ack, sid, payload = p

                        if payload:
                            self.rx = seq
                            if i % 2 == 0:
                                self.send_ack()

                        if sid == 11 and payload and len(payload) > 2:
                            video = payload[2:]
                            total += len(video)
                            f.write(video)
                            if video[:2] == b'\xff\xd8':
                                frames += 1
                                if frames <= 10 or frames % 10 == 0:
                                    print(f'  JPEG #{frames} ({len(video)}b) total={total//1024}KB')
                        elif sid == CS_ID and payload and len(payload) >= 6:
                            s,l,mid = struct.unpack('>HHH', payload[:6])
                            if s == CSM_S:
                                print(f'  [CSM] 0x{mid:04X}')
                    else:
                        off += 1

        print(f'\nTotal: {total} bytes, {frames} JPEG frames -> {out}')
        return total > 0

    def cleanup(self):
        if self.dev:
            for i in [0,1]:
                try: usb.util.release_interface(self.dev, i)
                except: pass

if __name__ == '__main__':
    print('='*50)
    print('  Teslong TD300 iAP2 Video Stream')
    print('='*50)
    td = TD()
    try:
        td.connect()
    except KeyboardInterrupt:
        print('\nStopped')
    except Exception as e:
        import traceback; traceback.print_exc()
    finally:
        td.cleanup()
        print('Replug device to restore normal operation.')
