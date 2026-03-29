#!/usr/bin/env python3
"""
Teslong TD300 iAP2 Host - connects to the boroscope and streams video.
Based on the wiomoc/iap2 protocol reference.
"""

import usb.core
import usb.util
import struct
import time
import sys
import os
import threading

VENDOR_ID = 0x3301
PRODUCT_ID = 0x2003

# iAP2 constants
IAP2_MARKER = b'\xFF\x55\x02\x00\xEE\x10'
LINK_START = 0xFF5A
CONTROL_SYN = 0x80
CONTROL_ACK = 0x40
CONTROL_EAK = 0x20
CONTROL_RST = 0x10
CSM_START = 0x4040

# iAP2 Control Session Message IDs
MSG_REQUEST_AUTH_CERT = 0xAA00
MSG_AUTH_CERTIFICATE = 0xAA01
MSG_REQUEST_AUTH_CHALLENGE = 0xAA02
MSG_AUTH_RESPONSE = 0xAA03
MSG_AUTH_FAILED = 0xAA04
MSG_AUTH_SUCCEEDED = 0xAA05
MSG_START_IDENTIFICATION = 0x1D00
MSG_IDENTIFICATION_INFO = 0x1D01
MSG_IDENTIFICATION_ACCEPTED = 0x1D02
MSG_IDENTIFICATION_REJECTED = 0x1D03
MSG_START_EAP_SESSION = 0xEA00
MSG_STOP_EAP_SESSION = 0xEA01
MSG_STATUS_EAP_SESSION = 0xEA03

CONTROL_SESSION_ID = 10
EA_SESSION_ID = 11


def gen_checksum(data):
    """iAP2 checksum: negate the byte-wise sum"""
    s = 0
    for b in data:
        s = (s + b) & 0xFF
    return (-s) & 0xFF


def check_checksum(data):
    s = 0
    for b in data:
        s = (s + b) & 0xFF
    return s == 0


def build_link_header(length, control, seq, ack, session_id):
    """Build a 9-byte iAP2 link packet header (FF 5A format)"""
    header = struct.pack(">HHBBBB", LINK_START, length, control, seq, ack, session_id)
    return header + bytes([gen_checksum(header)])


def build_syn_payload(max_outgoing=30, max_len=65535, retransmit_timeout=4000,
                       ack_timeout=500, max_retransmissions=4, max_ack=3,
                       sessions=None):
    """Build LinkSynchronizationPayload"""
    if sessions is None:
        sessions = [
            (CONTROL_SESSION_ID, 0, 1),  # Control session (type=0, version=1)
            (EA_SESSION_ID, 2, 1),         # EA session (type=2, version=1)
        ]
    payload = struct.pack(">BBHHHBB",
        0x01,  # version
        max_outgoing,
        max_len,
        retransmit_timeout,
        ack_timeout,
        max_retransmissions,
        max_ack,
    )
    for sid, stype, sver in sessions:
        payload += bytes([sid, stype, sver])
    return payload


def build_csm(msg_id, params=b''):
    """Build a Control Session Message"""
    length = 6 + len(params)
    return struct.pack(">HHH", CSM_START, length, msg_id) + params


def build_csm_param(param_id, data):
    """Build a single CSM parameter (TLV)"""
    return struct.pack(">HH", 4 + len(data), param_id) + data


class TD300Connection:
    def __init__(self, dev):
        self.dev = dev
        self.tx_seq = 0
        self.rx_seq = 0
        self.state = 'detect'
        self.device_lsp = None
        self.running = True
        self.ea_data = bytearray()
        self.video_frames = []

    def read(self, ep=0x81, timeout=2000):
        try:
            data = self.dev.read(ep, 65536, timeout=timeout)
            return bytes(data)
        except usb.core.USBTimeoutError:
            return None
        except Exception as e:
            print(f"  [READ ERR] {e}")
            return None

    def write(self, data, ep=0x01, timeout=2000):
        try:
            return self.dev.write(ep, data, timeout=timeout)
        except Exception as e:
            print(f"  [WRITE ERR] {e}")
            return 0

    def send_link_packet(self, payload=None, control=0, seq=None, ack=None, session_id=0):
        if seq is None:
            seq = self.tx_seq
        if ack is None:
            ack = self.rx_seq
        if payload:
            length = len(payload) + 10  # header(9) + payload + payload_checksum(1)
        else:
            length = 9
        header = build_link_header(length, control, seq, ack, session_id)
        if payload:
            pkt = header + payload + bytes([gen_checksum(payload)])
        else:
            pkt = header
        self.write(pkt)
        return pkt

    def send_csm(self, msg_id, params=b''):
        csm = build_csm(msg_id, params)
        self.tx_seq = (self.tx_seq + 1) & 0xFF
        pkt = self.send_link_packet(csm, control=CONTROL_ACK, session_id=CONTROL_SESSION_ID)
        return pkt

    def parse_link_packet(self, data):
        """Parse a link-layer packet starting with FF 5A"""
        if len(data) < 9:
            return None
        start = struct.unpack(">H", data[0:2])[0]
        if start != LINK_START:
            return None
        if not check_checksum(data[:9]):
            return None
        length = struct.unpack(">H", data[2:4])[0]
        control = data[4]
        seq = data[5]
        ack = data[6]
        session_id = data[7]
        payload = None
        if length > 9 and len(data) >= length:
            payload = data[9:length - 1]  # exclude payload checksum
        return {
            'control': control,
            'seq': seq,
            'ack': ack,
            'session_id': session_id,
            'payload': payload,
            'length': length,
        }

    def parse_csm(self, data):
        """Parse a Control Session Message from payload"""
        if len(data) < 6:
            return None
        start, length, msg_id = struct.unpack(">HHH", data[:6])
        if start != CSM_START:
            return None
        params = data[6:length] if length > 6 else b''
        return {'msg_id': msg_id, 'params': params, 'length': length}

    def connect(self):
        print("=== Phase 1: iAP2 Detect ===")
        # Drain any pending data
        while self.read(timeout=200):
            pass

        # Send IAP2 marker and immediately start reading
        print(f"[SEND] IAP2 marker: {IAP2_MARKER.hex()}")
        self.write(IAP2_MARKER)

        # Keep sending marker and reading responses
        marker_sent = 1
        marker_received = False
        for attempt in range(30):
            data = self.read(timeout=500)
            if data is None:
                if marker_sent < 5:
                    self.write(IAP2_MARKER)
                    marker_sent += 1
                continue

            if data == IAP2_MARKER:
                if not marker_received:
                    print(f"[RECV] IAP2 marker from device (attempt {attempt})")
                    marker_received = True
                    # Send one more marker to confirm, then start negotiate
                    self.write(IAP2_MARKER)
                continue

            # Got something different!
            print(f"[RECV] New data ({len(data)} bytes): {data[:32].hex()}")

            # Check if it's a link packet (FF 5A)
            if len(data) >= 2 and data[0] == 0xFF and data[1] == 0x5A:
                print("[OK] Got iAP2 link packet! Moving to negotiate phase.")
                self.state = 'negotiate'
                return self.negotiate(data)

        print("[FAIL] Could not establish link after 30 attempts")
        return False

    def negotiate(self, initial_packet=None):
        print("\n=== Phase 2: Link Negotiation ===")

        # Process initial packet if we already have one
        packets_to_process = []
        if initial_packet:
            packets_to_process.append(initial_packet)

        syn_received = False
        our_syn_sent = False
        ack_received = False

        for attempt in range(30):
            if not packets_to_process:
                data = self.read(timeout=1000)
                if data is None:
                    continue
                packets_to_process.append(data)

            while packets_to_process:
                data = packets_to_process.pop(0)

                if data == IAP2_MARKER:
                    self.write(IAP2_MARKER)
                    continue

                pkt = self.parse_link_packet(data)
                if pkt is None:
                    print(f"[WARN] Unparseable: {data[:20].hex()}")
                    continue

                ctrl = pkt['control']

                if ctrl & CONTROL_SYN:
                    syn_data = pkt['payload']
                    if syn_data and len(syn_data) >= 10:
                        ver, max_out, max_len, retrans, ack_to, max_retrans, max_ack = \
                            struct.unpack(">BBHHHBB", syn_data[:10])
                        sessions_data = syn_data[10:]
                        if not syn_received:
                            print(f"[SYN] Device: ver={ver} max_out={max_out} "
                                  f"max_len={max_len} retrans={retrans}ms ack_to={ack_to}ms "
                                  f"max_retrans={max_retrans} max_ack={max_ack}")
                            sessions = []
                            for i in range(0, len(sessions_data), 3):
                                if i + 3 <= len(sessions_data):
                                    sid, stype, sver = sessions_data[i], sessions_data[i+1], sessions_data[i+2]
                                    sessions.append((sid, stype, sver))
                                    print(f"  Session: id={sid} type={stype} ver={sver}")
                            self.device_lsp = {
                                'max_outgoing': max_out, 'max_len': max_len,
                                'retransmit_timeout': retrans, 'ack_timeout': ack_to,
                                'max_retransmissions': max_retrans, 'max_ack': max_ack,
                            }
                        self.rx_seq = pkt['seq']
                        syn_received = True

                    # Respond with SYN+ACK: our params + acknowledging device's SYN
                    # Match device's conservative params
                    our_syn = build_syn_payload(
                        max_outgoing=1,    # match device
                        max_len=4096,      # match device
                        retransmit_timeout=3000,
                        ack_timeout=1500,
                        max_retransmissions=4,
                        max_ack=1,
                        sessions=[(CONTROL_SESSION_ID, 0, 1)],  # only control, EA comes later
                    )
                    # Send combined SYN+ACK
                    self.send_link_packet(our_syn, control=CONTROL_SYN | CONTROL_ACK,
                                          seq=self.tx_seq, ack=self.rx_seq)
                    if not our_syn_sent:
                        print(f"[SEND] SYN+ACK (matching device params)")
                        our_syn_sent = True

                if ctrl & CONTROL_ACK:
                    print(f"[RECV] ACK seq={pkt['seq']} ack={pkt['ack']}")
                    ack_received = True

                if syn_received and ack_received:
                    print("[OK] Link established!")
                    self.state = 'normal'
                    return self.authenticate()

                if ctrl & CONTROL_RST:
                    print("[RECV] RST - device reset the link!")
                    return False

        print("[FAIL] Negotiation failed")
        return False

    def authenticate(self):
        print("\n=== Phase 3: Authentication ===")

        # Request device's MFi certificate
        print("[SEND] RequestAuthenticationCertificate")
        self.send_csm(MSG_REQUEST_AUTH_CERT)

        # Read certificate response
        for attempt in range(10):
            data = self.read(timeout=2000)
            if data is None:
                continue

            pkt = self.parse_link_packet(data)
            if pkt is None or pkt['payload'] is None:
                continue

            # ACK the packet
            self.rx_seq = pkt['seq']
            self.send_link_packet(control=CONTROL_ACK, seq=self.tx_seq)

            if pkt['session_id'] == CONTROL_SESSION_ID:
                csm = self.parse_csm(pkt['payload'])
                if csm is None:
                    continue
                print(f"[RECV] CSM msg_id=0x{csm['msg_id']:04X} params={len(csm['params'])} bytes")

                if csm['msg_id'] == MSG_AUTH_CERTIFICATE:
                    cert_data = csm['params']
                    print(f"[AUTH] Got certificate ({len(cert_data)} bytes)")

                    # Send a challenge (20 random bytes)
                    challenge = os.urandom(20)
                    challenge_param = build_csm_param(0, challenge)
                    print("[SEND] RequestAuthenticationChallengeResponse")
                    self.send_csm(MSG_REQUEST_AUTH_CHALLENGE, challenge_param)

                elif csm['msg_id'] == MSG_AUTH_RESPONSE:
                    print(f"[AUTH] Got challenge response ({len(csm['params'])} bytes)")
                    # We don't actually verify — just accept
                    print("[SEND] AuthenticationSucceeded")
                    self.send_csm(MSG_AUTH_SUCCEEDED)
                    return self.identify()

                elif csm['msg_id'] == MSG_AUTH_FAILED:
                    print("[AUTH] Authentication failed!")
                    return False

        print("[FAIL] Authentication timeout")
        return False

    def identify(self):
        print("\n=== Phase 4: Identification ===")

        # Send StartIdentification
        print("[SEND] StartIdentification")
        self.send_csm(MSG_START_IDENTIFICATION)

        # Read identification info
        for attempt in range(10):
            data = self.read(timeout=3000)
            if data is None:
                continue

            pkt = self.parse_link_packet(data)
            if pkt is None or pkt['payload'] is None:
                continue

            self.rx_seq = pkt['seq']
            self.send_link_packet(control=CONTROL_ACK, seq=self.tx_seq)

            if pkt['session_id'] == CONTROL_SESSION_ID:
                csm = self.parse_csm(pkt['payload'])
                if csm is None:
                    continue
                print(f"[RECV] CSM msg_id=0x{csm['msg_id']:04X} params={len(csm['params'])} bytes")

                if csm['msg_id'] == MSG_IDENTIFICATION_INFO:
                    # Parse some params for display
                    self.parse_identification(csm['params'])
                    # Accept
                    print("[SEND] IdentificationAccepted")
                    self.send_csm(MSG_IDENTIFICATION_ACCEPTED)
                    return self.start_ea_session()

        print("[FAIL] Identification timeout")
        return False

    def parse_identification(self, params):
        """Parse IdentificationInformation params for display"""
        offset = 0
        while offset < len(params) - 4:
            param_len, param_id = struct.unpack(">HH", params[offset:offset+4])
            param_data = params[offset+4:offset+param_len]
            if param_id == 0 and param_data:  # name
                print(f"  Name: {param_data.rstrip(b'\\x00').decode('utf-8', errors='replace')}")
            elif param_id == 1 and param_data:  # model
                print(f"  Model: {param_data.rstrip(b'\\x00').decode('utf-8', errors='replace')}")
            elif param_id == 2 and param_data:  # manufacturer
                print(f"  Manufacturer: {param_data.rstrip(b'\\x00').decode('utf-8', errors='replace')}")
            elif param_id == 10:  # EA protocol (group param)
                print(f"  EA Protocol ({len(param_data)} bytes)")
            offset += param_len
            if param_len == 0:
                break

    def start_ea_session(self):
        print("\n=== Phase 5: Start EA Session ===")

        # Build StartExternalAccessoryProtocolSession
        # protocol_id = 1 (the first EA protocol the device advertised)
        # session_id = a unique ID for this session
        params = build_csm_param(0, struct.pack(">B", 1))  # protocol_id = 1
        params += build_csm_param(1, struct.pack(">H", 1))  # session_id = 1

        print("[SEND] StartExternalAccessoryProtocolSession (protocol_id=1, session_id=1)")
        self.send_csm(MSG_START_EAP_SESSION, params)

        # Now read data from both control and EA sessions
        print("\n=== Phase 6: Reading Video Data ===")
        output_file = "/tmp/td300_video.bin"
        total_bytes = 0

        with open(output_file, "wb") as f:
            for attempt in range(200):  # Read for a while
                # Read from EP 0x81 (iAP2 link layer data)
                data = self.read(timeout=1000)
                if data is None:
                    continue

                pkt = self.parse_link_packet(data)
                if pkt is None:
                    # Might be raw data or marker
                    if data == IAP2_MARKER:
                        continue
                    print(f"[RAW] {len(data)} bytes: {data[:32].hex()}")
                    continue

                self.rx_seq = pkt['seq']
                # ACK every few packets
                if attempt % 3 == 0:
                    self.send_link_packet(control=CONTROL_ACK, seq=self.tx_seq)

                payload = pkt['payload']
                if payload is None:
                    continue

                if pkt['session_id'] == EA_SESSION_ID:
                    # EA session data — this is video!
                    # First 2 bytes are the stream ID
                    if len(payload) > 2:
                        stream_id = struct.unpack(">H", payload[:2])[0]
                        video_data = payload[2:]
                        total_bytes += len(video_data)
                        f.write(video_data)
                        if total_bytes % 65536 < len(video_data):
                            print(f"[VIDEO] {total_bytes} bytes received (stream {stream_id})")

                        # Check for JPEG markers
                        if b'\xff\xd8' in video_data[:4]:
                            print(f"[JPEG] Frame start detected at {total_bytes - len(video_data)}")

                elif pkt['session_id'] == CONTROL_SESSION_ID:
                    csm = self.parse_csm(payload)
                    if csm:
                        print(f"[CSM] msg_id=0x{csm['msg_id']:04X}")
                        if csm['msg_id'] == MSG_STATUS_EAP_SESSION:
                            print("[EAP] Session status update")

                # Also check EP 0x82 (Interface 1 direct data)
                ep82_data = self.read(ep=0x82, timeout=100)
                if ep82_data:
                    total_bytes += len(ep82_data)
                    f.write(ep82_data)
                    print(f"[EP82] {len(ep82_data)} bytes")

        print(f"\n[DONE] Total video data: {total_bytes} bytes saved to {output_file}")
        return total_bytes > 0


def main():
    print("=" * 60)
    print("  Teslong TD300 iAP2 Host Viewer")
    print("=" * 60)

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("[ERROR] TD300 not found! Plug in the device.")
        sys.exit(1)
    print(f"[OK] Found: {dev.product}")

    # USB reset to get clean state
    try:
        dev.reset()
        print("[OK] USB reset")
    except:
        pass
    time.sleep(2)

    # Re-find after reset
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("[ERROR] Device lost after reset!")
        sys.exit(1)

    # Claim interfaces
    for intf in [0, 1]:
        try:
            if dev.is_kernel_driver_active(intf):
                dev.detach_kernel_driver(intf)
                print(f"[OK] Detached driver from interface {intf}")
        except:
            pass
    try:
        dev.set_configuration()
    except:
        pass
    for intf in [0, 1]:
        try:
            usb.util.claim_interface(dev, intf)
            print(f"[OK] Claimed interface {intf}")
        except Exception as e:
            print(f"[WARN] Claim {intf}: {e}")
    try:
        dev.set_interface_altsetting(interface=1, alternate_setting=1)
        print("[OK] Interface 1 Alt 1 activated")
    except:
        pass

    # Connect
    conn = TD300Connection(dev)
    success = conn.connect()

    if not success:
        print("\n[INFO] Connection did not fully succeed. Check output above for progress.")

    # Cleanup
    for intf in [0, 1]:
        try:
            usb.util.release_interface(dev, intf)
        except:
            pass

    print("\n[INFO] Done. Replug device to restore normal operation.")


if __name__ == "__main__":
    main()
