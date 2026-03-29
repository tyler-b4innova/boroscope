#!/usr/bin/env python3
"""
Teslong TD300 - iAP2 Link Handshake and EAP Session
Establishes an iAP2 link with the device and opens the video stream.
"""

import usb.core
import usb.util
import struct
import time
import sys
import threading

VENDOR_ID = 0x3301
PRODUCT_ID = 0x2003

# iAP2 Link Layer Constants
LINK_SYN = bytes([0xFF, 0x55])
LINK_SOP = bytes([0xFF, 0x5A])  # Start of (data) Packet

# iAP2 Link Control byte flags
CTRL_SYN = 0x80
CTRL_ACK = 0x40
CTRL_EAK = 0x20
CTRL_RST = 0x10
CTRL_SUS = 0x08


def checksum(data):
    """Simple XOR checksum over all bytes"""
    cs = 0
    for b in data:
        cs = (cs + b) & 0xFF
    return (~cs + 1) & 0xFF


def build_link_sync_response(max_outstanding=3, max_packet_len=4096,
                              retransmit_timeout=1000, cum_ack_timeout=50,
                              max_retransmit=30, max_cum_ack=3):
    """Build iAP2 link synchronization packet (host response)"""
    payload = struct.pack('>BBHHHHBB',
        0x01,                # version (accept version 1? or use 2)
        max_outstanding,
        max_packet_len,
        retransmit_timeout,
        cum_ack_timeout,
        max_retransmit,
        max_cum_ack,
    )
    # No session IDs for now
    payload += bytes([0])  # session count = 0

    # Build full sync packet
    pkt = LINK_SYN + payload
    cs = checksum(payload)
    pkt += bytes([cs])
    return pkt


def build_link_data_packet(session_id, seq, ack, payload, control=0):
    """Build an iAP2 link-layer data packet"""
    # Format: FF 5A | Length (2) | Control | Seq | Ack | SessionID | Payload | Checksum
    pkt_content = struct.pack('>BBBB', control, seq, ack, session_id) + payload
    length = len(pkt_content) + 3  # +2 for length field, +1 for checksum
    header = LINK_SOP + struct.pack('>H', length)
    full = header + pkt_content
    cs = checksum(pkt_content)
    full += bytes([cs])
    return full


def build_iap2_message(msg_id, params=b''):
    """Build an iAP2 session-layer message"""
    # iAP2 message: StartOfMessage(2) + MessageLength(2) + MessageID(2) + Parameters
    som = 0x4040  # Start of message marker
    msg_len = 6 + len(params)  # 2(SOM) + 2(len) + 2(msgID) + params
    return struct.pack('>HHH', som, msg_len, msg_id) + params


def parse_link_packet(data):
    """Parse an iAP2 link-layer packet"""
    if len(data) < 2:
        return None

    if data[0] == 0xFF and data[1] == 0x55:
        # Link sync/control packet
        return {
            'type': 'sync',
            'raw': data.hex(),
            'payload': data[2:].hex() if len(data) > 2 else ''
        }
    elif data[0] == 0xFF and data[1] == 0x5A:
        # Data packet
        if len(data) >= 6:
            length = struct.unpack('>H', data[2:4])[0]
            control = data[4]
            seq = data[5]
            ack = data[6] if len(data) > 6 else 0
            session = data[7] if len(data) > 7 else 0
            payload = data[8:-1] if len(data) > 9 else b''
            return {
                'type': 'data',
                'length': length,
                'control': control,
                'seq': seq,
                'ack': ack,
                'session': session,
                'payload': payload,
                'raw': data.hex()
            }
    return {'type': 'unknown', 'raw': data.hex()}


class IAP2Link:
    def __init__(self, dev):
        self.dev = dev
        self.seq_tx = 0
        self.seq_rx = 0
        self.running = True
        self.received_packets = []

    def read_packet(self, ep=0x81, timeout=1000):
        """Read a single packet from the device"""
        try:
            data = self.dev.read(ep, 16384, timeout=timeout)
            return bytes(data)
        except usb.core.USBTimeoutError:
            return None
        except Exception as e:
            print(f"  [READ ERROR] {e}")
            return None

    def write_packet(self, data, ep=0x01, timeout=2000):
        """Write a packet to the device"""
        try:
            written = self.dev.write(ep, data, timeout=timeout)
            return written
        except Exception as e:
            print(f"  [WRITE ERROR] {e}")
            return 0

    def drain_packets(self, ep=0x81, count=10, timeout=500):
        """Read and display all pending packets"""
        packets = []
        for _ in range(count):
            pkt = self.read_packet(ep, timeout)
            if pkt is None:
                break
            packets.append(pkt)
            parsed = parse_link_packet(pkt)
            print(f"  <- [{parsed['type']}] {pkt.hex()}")
        return packets

    def establish_link(self):
        """Perform iAP2 link establishment"""
        print("\n=== Phase 1: Link Detect ===")

        # Drain any pending sync packets
        print("[READ] Draining pending packets...")
        self.drain_packets(timeout=300)

        # The device sends FF 55 02 00 EE 10 (link sync)
        # We need to respond with our own sync parameters

        # Try sending detect response first (FF 55 04)
        print("\n[SEND] Sending detect response (FF 55 04)...")
        self.write_packet(bytes([0xFF, 0x55, 0x04]))
        time.sleep(0.2)
        print("[READ] Response:")
        self.drain_packets(timeout=1000)

        print("\n=== Phase 2: Link Sync ===")

        # Send our sync parameters
        sync_pkt = build_link_sync_response(
            max_outstanding=3,
            max_packet_len=0xEE10,  # Match device's advertised size
            retransmit_timeout=2000,
            cum_ack_timeout=100,
            max_retransmit=30,
            max_cum_ack=3,
        )
        print(f"[SEND] Sync params: {sync_pkt.hex()}")
        self.write_packet(sync_pkt)
        time.sleep(0.3)
        print("[READ] Response:")
        pkts = self.drain_packets(timeout=2000)

        # Try alternate sync format - version 2
        print("\n[SEND] Trying version 2 sync...")
        sync_v2 = LINK_SYN + bytes([
            0x02,        # version 2
            0x03,        # max outstanding = 3
            0x10, 0x00,  # max packet length = 4096
            0x03, 0xE8,  # retransmit timeout = 1000ms
            0x00, 0x32,  # cum ack timeout = 50ms
            0x1E,        # max retransmissions = 30
            0x03,        # max cum acks = 3
            0x01,        # num session IDs = 1
            0x10,        # session ID = 16
        ])
        cs = checksum(sync_v2[2:])
        sync_v2 += bytes([cs])
        print(f"[SEND] {sync_v2.hex()}")
        self.write_packet(sync_v2)
        time.sleep(0.3)
        print("[READ] Response:")
        pkts = self.drain_packets(timeout=2000)

        # Try another format: maybe the device wants us to mirror its format
        print("\n[SEND] Mirroring device sync format...")
        mirror = bytes([0xFF, 0x55, 0x02, 0x00, 0xEE, 0x10])
        self.write_packet(mirror)
        time.sleep(0.3)
        print("[READ] Response:")
        pkts = self.drain_packets(timeout=2000)

        # Try iAP2 SOP (data packet) with ACK
        print("\n=== Phase 3: Try Data Packets ===")
        ack_pkt = build_link_data_packet(
            session_id=0, seq=0, ack=0,
            payload=b'', control=CTRL_ACK
        )
        print(f"[SEND] ACK packet: {ack_pkt.hex()}")
        self.write_packet(ack_pkt)
        time.sleep(0.2)
        print("[READ] Response:")
        self.drain_packets(timeout=1000)

        # Try sending a SYN+ACK data packet
        syn_ack = build_link_data_packet(
            session_id=0, seq=0, ack=0,
            payload=b'', control=CTRL_SYN | CTRL_ACK
        )
        print(f"\n[SEND] SYN+ACK: {syn_ack.hex()}")
        self.write_packet(syn_ack)
        time.sleep(0.2)
        print("[READ] Response:")
        self.drain_packets(timeout=1000)

    def try_eap_session(self):
        """Try to open an External Accessory Protocol session"""
        print("\n=== Phase 4: EAP Session ===")

        # iAP2 message IDs (from reverse engineering)
        # 0x1D00 = RequestAuthenticationCertificate
        # 0x1D02 = RequestAuthenticationChallengeResponse
        # 0xAA00 = IdentificationInformation
        # 0xAA02 = IdentificationAccepted/Rejected
        # 0xEA00 = StartExternalAccessoryProtocolSession
        # 0xEA02 = ExternalAccessoryProtocolSessionStarted

        # Build identification request
        # iAP2 messages use TLV (Tag-Length-Value) parameters
        # Parameter: ExternalAccessoryProtocolIdentifier
        #   Tag: 0x0000 (varies)
        #   SubParams: ProtocolName = "io.grus.exone"

        protocol_name = b"io.grus.exone"

        # Build StartEAPSession message
        # Parameter format: ParamLength(2) + ParamID(2) + ParamData
        proto_param = struct.pack('>HH', 4 + len(protocol_name), 0x0000) + protocol_name
        session_param = struct.pack('>HHH', 6, 0x0001, 0x0001)  # Session ID = 1

        eap_start_msg = build_iap2_message(0xEA00, proto_param + session_param)

        # Wrap in link packet
        eap_pkt = build_link_data_packet(
            session_id=0, seq=0, ack=0,
            payload=eap_start_msg, control=CTRL_ACK
        )

        print(f"[SEND] StartEAP: {eap_pkt.hex()}")
        self.write_packet(eap_pkt)
        time.sleep(0.5)
        print("[READ] Response:")
        self.drain_packets(timeout=2000)

        # Also try on Interface 1
        print("\n[CHECK] Interface 1 data channel:")
        for _ in range(3):
            pkt = self.read_packet(0x82, timeout=1000)
            if pkt:
                print(f"  <- Data: {len(pkt)} bytes: {pkt[:64].hex()}")
            else:
                print("  <- No data")
                break

    def continuous_read(self, duration=10):
        """Read continuously from both endpoints for a period"""
        print(f"\n=== Continuous Read ({duration}s) ===")
        start = time.time()
        total_81 = 0
        total_82 = 0

        while time.time() - start < duration:
            for ep in [0x81, 0x82]:
                try:
                    data = self.dev.read(ep, 16384, timeout=200)
                    if ep == 0x81:
                        total_81 += len(data)
                        if data[:2] != b'\xff\x55' or data != b'\xff\x55\x02\x00\xee\x10':
                            # New/different data!
                            print(f"  <- EP 0x{ep:02x}: {len(data)} bytes: {bytes(data)[:64].hex()}")
                    else:
                        total_82 += len(data)
                        print(f"  <- EP 0x{ep:02x}: {len(data)} bytes: {bytes(data)[:64].hex()}")
                except usb.core.USBTimeoutError:
                    pass
                except Exception as e:
                    pass

        print(f"\n[STATS] EP 0x81: {total_81} bytes | EP 0x82: {total_82} bytes")


def main():
    print("=" * 60)
    print("  Teslong TD300 - iAP2 Handshake")
    print("=" * 60)

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("[ERROR] Device not found!")
        sys.exit(1)
    print(f"[OK] Found: {dev.product}")

    # Take over interfaces
    for intf in [0, 1]:
        try:
            if dev.is_kernel_driver_active(intf):
                dev.detach_kernel_driver(intf)
        except:
            pass
    try:
        dev.set_configuration()
    except:
        pass
    for intf in [0, 1]:
        try:
            usb.util.claim_interface(dev, intf)
        except:
            pass

    # Activate Interface 1 data endpoints
    try:
        dev.set_interface_altsetting(interface=1, alternate_setting=1)
        print("[OK] Interface 1 Alt 1 activated")
    except:
        pass

    # Run link establishment
    link = IAP2Link(dev)
    link.establish_link()
    link.try_eap_session()
    link.continuous_read(duration=5)

    # Cleanup
    for intf in [0, 1]:
        try:
            usb.util.release_interface(dev, intf)
        except:
            pass

    print("\n[DONE] Replug device to restore accessoryd control.")


if __name__ == "__main__":
    main()
