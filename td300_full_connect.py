#!/usr/bin/env python3
"""
Teslong TD300 Full iAP2 Connection — Detect → Negotiate → Auth → Identify → EA Session → Video
"""
import usb.core, usb.util, struct, os, time, sys

VENDOR_ID, PRODUCT_ID = 0x3301, 0x2003
IAP2_MARKER = b'\xFF\x55\x02\x00\xEE\x10'
LINK_START = 0xFF5A
CONTROL_SYN, CONTROL_ACK = 0x80, 0x40
CSM_START = 0x4040
CTRL_SID = 10  # Control session ID


def gen_cs(data):
    return (-sum(data) & 0xFF) & 0xFF


def check_cs(data):
    return sum(data) & 0xFF == 0


def link_header(length, control, seq, ack, session_id):
    h = struct.pack(">HHBBBB", LINK_START, length, control, seq, ack, session_id)
    return h + bytes([gen_cs(h)])


def link_packet(payload, control, seq, ack, session_id=0):
    pkt_len = (len(payload) + 10) if payload else 9
    hdr = link_header(pkt_len, control, seq, ack, session_id)
    if payload:
        return hdr + payload + bytes([gen_cs(payload)])
    return hdr


def csm_msg(msg_id, params=b''):
    return struct.pack(">HHH", CSM_START, 6 + len(params), msg_id) + params


def csm_param(pid, data):
    return struct.pack(">HH", 4 + len(data), pid) + data


def parse_link(data):
    if len(data) < 9 or data[0] != 0xFF or data[1] != 0x5A:
        return None
    if not check_cs(data[:9]):
        return None
    length = struct.unpack(">H", data[2:4])[0]
    return {
        'ctrl': data[4], 'seq': data[5], 'ack': data[6],
        'sid': data[7], 'length': length,
        'payload': data[9:length - 1] if length > 9 and len(data) >= length else None,
        'raw': data
    }


def parse_csm(data):
    if not data or len(data) < 6:
        return None
    s, l, mid = struct.unpack(">HHH", data[:6])
    if s != CSM_START:
        return None
    return {'id': mid, 'params': data[6:l] if l > 6 else b'', 'len': l}


class TD300:
    def __init__(self):
        self.dev = None
        self.tx_seq = 0
        self.rx_seq = 0

    def rd(self, ep=0x81, timeout=2000):
        try:
            return bytes(self.dev.read(ep, 65536, timeout=timeout))
        except:
            return None

    def wr(self, data, ep=0x01):
        try:
            self.dev.write(ep, data, timeout=2000)
        except Exception as e:
            print(f"  [WR ERR] {e}")

    def send_link(self, payload, ctrl, sid=0):
        pkt = link_packet(payload, ctrl, self.tx_seq, self.rx_seq, sid)
        self.wr(pkt)
        return pkt

    def send_csm(self, msg_id, params=b''):
        self.tx_seq = (self.tx_seq + 1) & 0xFF
        msg = csm_msg(msg_id, params)
        return self.send_link(msg, CONTROL_ACK, CTRL_SID)

    def ack(self, rx_seq=None):
        if rx_seq is not None:
            self.rx_seq = rx_seq
        return self.send_link(None, CONTROL_ACK)

    def read_link_packets(self, timeout=2000, max_reads=5):
        """Read and parse link packets, handling concatenated data"""
        packets = []
        for _ in range(max_reads):
            data = self.rd(timeout=timeout)
            if data is None:
                break
            # Handle multiple link packets in one USB read
            offset = 0
            while offset < len(data):
                if offset + 9 > len(data):
                    break
                if data[offset] == 0xFF and data[offset + 1] == 0x5A:
                    length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
                    if offset + length <= len(data):
                        pkt = parse_link(data[offset:offset + length])
                        if pkt:
                            packets.append(pkt)
                        offset += length
                    else:
                        break
                else:
                    offset += 1
            if packets:
                break
        return packets

    def connect(self):
        # Find device
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if not self.dev:
            print("[ERROR] TD300 not found!")
            return False
        print(f"[OK] {self.dev.product}")

        # Claim
        for i in [0, 1]:
            try:
                if self.dev.is_kernel_driver_active(i):
                    self.dev.detach_kernel_driver(i)
            except: pass
        try: self.dev.set_configuration()
        except: pass
        for i in [0, 1]:
            try: usb.util.claim_interface(self.dev, i)
            except: pass
        try: self.dev.set_interface_altsetting(interface=1, alternate_setting=1)
        except: pass

        # Phase 1: Detect
        print("\n--- Detect ---")
        for _ in range(5):
            self.rd(timeout=200)  # drain
        self.wr(IAP2_MARKER)

        syn_data = None
        for _ in range(10):
            d = self.rd(timeout=1500)
            if d is None:
                self.wr(IAP2_MARKER)
                continue
            # Find SYN packet
            idx = 0
            while idx < len(d):
                if idx + 2 <= len(d) and d[idx] == 0xFF and d[idx + 1] == 0x5A:
                    length = struct.unpack(">H", d[idx + 2:idx + 4])[0] if idx + 4 <= len(d) else 0
                    if idx + length <= len(d):
                        syn_data = d[idx:idx + length]
                        break
                idx += 1
            if syn_data:
                break
        if not syn_data:
            print("[FAIL] No SYN received")
            return False

        # Phase 2: Negotiate
        print("--- Negotiate ---")
        pkt = parse_link(syn_data)
        if not pkt or not (pkt['ctrl'] & CONTROL_SYN):
            print("[FAIL] Invalid SYN")
            return False

        payload = pkt['payload']
        if payload and len(payload) >= 10:
            ver, mo, ml, rt, at, mr, ma = struct.unpack(">BBHHHBB", payload[:10])
            print(f"  Device: max_out={mo} max_len={ml} retrans={rt}ms ack_to={at}ms")

        self.rx_seq = pkt['seq']

        # Send SYN+ACK matching device params
        our_syn = struct.pack(">BBHHHBB", 0x01, 1, 4096, 3000, 1500, 4, 1)
        our_syn += bytes([CTRL_SID, 0, 1])  # control session
        self.send_link(our_syn, CONTROL_SYN | CONTROL_ACK)

        # Wait for ACK
        got_ack = False
        for _ in range(10):
            d = self.rd(timeout=2000)
            if d is None:
                continue
            p = parse_link(d)
            if p and (p['ctrl'] & CONTROL_ACK) and not (p['ctrl'] & CONTROL_SYN):
                got_ack = True
                break
            elif p and (p['ctrl'] & CONTROL_SYN):
                # Resend SYN+ACK
                self.rx_seq = p['seq']
                self.send_link(our_syn, CONTROL_SYN | CONTROL_ACK)

        if not got_ack:
            print("[FAIL] No ACK for SYN")
            return False
        print("  Link established!")

        # Phase 3: Auth
        print("--- Auth ---")
        self.send_csm(0xAA00)  # RequestAuthenticationCertificate
        print("  Sent RequestAuthCert")

        cert = None
        for _ in range(10):
            d = self.rd(timeout=3000)
            if d is None:
                continue
            p = parse_link(d)
            if not p:
                continue
            # ACK data packets
            if p['payload'] and p['sid'] == CTRL_SID:
                self.rx_seq = p['seq']
                self.ack()
                csm = parse_csm(p['payload'])
                if csm:
                    if csm['id'] == 0xAA01:  # AuthenticationCertificate
                        cert = csm['params']
                        print(f"  Got MFi certificate ({len(cert)} bytes)")
                        break
            elif p['ctrl'] & CONTROL_ACK:
                pass  # Just an ACK for our message

        if not cert:
            print("[FAIL] No certificate")
            return False

        # Send challenge
        challenge = os.urandom(20)
        self.send_csm(0xAA02, csm_param(0, challenge))  # RequestAuthChallengeResponse
        print("  Sent challenge")

        # Get response
        for _ in range(10):
            d = self.rd(timeout=3000)
            if d is None:
                continue
            p = parse_link(d)
            if not p or not p['payload']:
                continue
            if p['sid'] == CTRL_SID:
                self.rx_seq = p['seq']
                self.ack()
                csm = parse_csm(p['payload'])
                if csm and csm['id'] == 0xAA03:  # AuthenticationResponse
                    print(f"  Got auth response ({len(csm['params'])} bytes)")
                    # Accept without verifying
                    self.send_csm(0xAA05)  # AuthenticationSucceeded
                    print("  Sent AuthSucceeded")
                    break

        # Phase 4: Identification
        print("--- Identify ---")
        time.sleep(0.3)

        # The device might send StartIdentification (0x1D00) to us, or we send it
        # As HOST, we send StartIdentification
        self.send_csm(0x1D00)
        print("  Sent StartIdentification")

        for _ in range(15):
            d = self.rd(timeout=3000)
            if d is None:
                continue
            p = parse_link(d)
            if not p:
                continue
            if p['payload'] and p['sid'] == CTRL_SID:
                self.rx_seq = p['seq']
                self.ack()
                csm = parse_csm(p['payload'])
                if csm:
                    print(f"  CSM 0x{csm['id']:04X} ({len(csm['params'])} bytes)")
                    if csm['id'] == 0x1D01:  # IdentificationInformation
                        print("  Got IdentificationInformation!")
                        # Parse name (param 0)
                        params = csm['params']
                        off = 0
                        while off + 4 <= len(params):
                            plen, pid = struct.unpack(">HH", params[off:off+4])
                            if plen == 0:
                                break
                            pdata = params[off+4:off+plen]
                            if pid == 0:
                                print(f"    Name: {pdata.rstrip(b'\\x00').decode('utf-8', errors='replace')}")
                            elif pid == 2:
                                print(f"    Manufacturer: {pdata.rstrip(b'\\x00').decode('utf-8', errors='replace')}")
                            elif pid == 10:
                                print(f"    EA Protocol group ({len(pdata)} bytes)")
                            off += plen

                        # Accept
                        self.send_csm(0x1D02)  # IdentificationAccepted
                        print("  Sent IdentificationAccepted")
                        break
                    elif csm['id'] == 0x1D00:  # Device wants US to send identification?
                        print("  Device sent StartIdentification to us (unexpected for host)")
            elif p['ctrl'] & CONTROL_ACK:
                pass

        # Phase 5: Start EA Session
        print("--- EA Session ---")
        time.sleep(0.3)
        ea_params = csm_param(0, struct.pack(">B", 1))  # protocol_id = 1
        ea_params += csm_param(1, struct.pack(">H", 1))  # session_id = 1
        self.send_csm(0xEA00, ea_params)  # StartExternalAccessoryProtocolSession
        print("  Sent StartEAPSession (protocol_id=1, session_id=1)")

        # Phase 6: Read video data
        print("\n--- Video Stream ---")
        output = "/tmp/td300_video.bin"
        total = 0
        ea_sid = 11  # EA session ID on link layer

        with open(output, "wb") as f:
            for i in range(500):
                d = self.rd(timeout=2000)
                if d is None:
                    continue

                # Handle multiple packets
                offset = 0
                while offset < len(d):
                    if offset + 9 > len(d):
                        break
                    if d[offset] == 0xFF and d[offset + 1] == 0x5A:
                        length = struct.unpack(">H", d[offset + 2:offset + 4])[0]
                        end = offset + length
                        if end > len(d):
                            break
                        p = parse_link(d[offset:end])
                        offset = end
                        if not p:
                            continue

                        # ACK periodically
                        if p['payload']:
                            self.rx_seq = p['seq']
                            if i % 2 == 0:
                                self.ack()

                        if p['sid'] == ea_sid and p['payload'] and len(p['payload']) > 2:
                            # EA data: first 2 bytes = stream ID, rest = video
                            video = p['payload'][2:]
                            total += len(video)
                            f.write(video)
                            if b'\xff\xd8' in video[:4]:
                                print(f"  [JPEG] Frame @ {total} bytes")
                            if total % 65536 < len(video):
                                print(f"  {total // 1024} KB received")
                        elif p['sid'] == CTRL_SID and p['payload']:
                            csm = parse_csm(p['payload'])
                            if csm:
                                print(f"  [CSM] 0x{csm['id']:04X}")
                    else:
                        offset += 1

        print(f"\n[DONE] {total} bytes saved to {output}")
        return total > 0

    def cleanup(self):
        if self.dev:
            for i in [0, 1]:
                try: usb.util.release_interface(self.dev, i)
                except: pass


if __name__ == "__main__":
    print("=" * 50)
    print("  Teslong TD300 iAP2 Video Stream")
    print("=" * 50)

    td = TD300()
    try:
        td.connect()
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        td.cleanup()
        print("\nReplug device to restore normal operation.")
