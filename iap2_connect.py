#!/usr/bin/env python3
"""
Teslong TD300 - iAP2 Link Establishment
Systematically tries different iAP2 detect/sync responses.
"""

import usb.core
import usb.util
import struct
import time
import sys

VENDOR_ID = 0x3301
PRODUCT_ID = 0x2003
SYN = b'\xFF\x55'
SOP = b'\xFF\x5A'


def iap_checksum(data):
    """iAP1-style checksum: 0x100 - (sum of bytes & 0xFF)"""
    return (0x100 - (sum(data) & 0xFF)) & 0xFF


def read_all(dev, ep=0x81, timeout=300, max_reads=20):
    """Read all available packets from an endpoint"""
    packets = []
    for _ in range(max_reads):
        try:
            data = dev.read(ep, 16384, timeout=timeout)
            packets.append(bytes(data))
        except:
            break
    return packets


def send_and_check(dev, label, payload, ep_out=0x01, ep_in=0x81,
                   read_timeout=800, pre_drain=True):
    """Send a packet and read the response, checking for state change"""
    # Drain pending
    if pre_drain:
        read_all(dev, ep_in, timeout=100)

    print(f"\n[SEND] {label}: {payload.hex()}")
    try:
        dev.write(ep_out, payload, timeout=2000)
    except Exception as e:
        print(f"  Write error: {e}")
        return []

    time.sleep(0.15)
    responses = read_all(dev, ep_in, timeout=read_timeout)

    baseline = b'\xff\x55\x02\x00\xee\x10'
    new_responses = [r for r in responses if r != baseline]

    if new_responses:
        print(f"  *** NEW RESPONSES ({len(new_responses)}):")
        for r in new_responses:
            print(f"    {r.hex()}")
    elif responses:
        print(f"  Same sync beacon x{len(responses)} (no change)")
    else:
        print(f"  No response")

    return responses


def main():
    print("=" * 60)
    print("  Teslong TD300 - iAP2 Link Probe")
    print("=" * 60)

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("[ERROR] Device not found!")
        sys.exit(1)
    print(f"[OK] Found: {dev.product}")

    # Claim Interface 0 (detach accessoryd)
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
    try:
        dev.set_interface_altsetting(interface=1, alternate_setting=1)
    except:
        pass

    print("\n[INFO] Device baseline packet: ff550200ee10")
    print("[INFO] Parsed: FF55(SYN) + 02(payload_len=2) + 00 EE(data) + 10(checksum)")
    print("[INFO] Checksum verify: (02+00+EE)&FF=F0, 100-F0=10 ✓ (iAP1 format)")
    print()

    # Phase 1: Try iAP2 detect responses
    print("=" * 40)
    print("Phase 1: iAP2 Detect Responses")
    print("=" * 40)

    # iAP2 detect byte meanings:
    # From device: 0x02 = I support iAP2
    # From host: 0x05 = Let's use iAP2
    for detect_byte in [0x03, 0x04, 0x05, 0x06, 0x02]:
        pkt = SYN + bytes([detect_byte])
        send_and_check(dev, f"Detect 0x{detect_byte:02x}", pkt)

    # Phase 2: Try iAP1-format responses
    # If the device speaks iAP1, respond in iAP1 format
    print("\n" + "=" * 40)
    print("Phase 2: iAP1 Format Responses")
    print("=" * 40)

    # iAP1: FF 55 <len> <lingo> <cmd> [params] <checksum>
    # The device sent Lingo=0x00, Cmd=0xEE
    # Lingo 0x00 = General Lingo
    # Let's try common iAP1 responses

    # RequestIdentify (Lingo 0, Cmd 0x00)
    iap1_identify = bytes([0x00, 0x00])  # Lingo 0, Cmd 0x00
    pkt = SYN + bytes([len(iap1_identify)]) + iap1_identify + bytes([iap_checksum(bytes([len(iap1_identify)]) + iap1_identify)])
    send_and_check(dev, "iAP1 RequestIdentify", pkt)

    # ACK (Lingo 0, Cmd 0x02, Status=OK, CmdID=0xEE)
    iap1_ack = bytes([0x00, 0x02, 0x00, 0xEE])  # Lingo 0, ACK, status=0, for cmd 0xEE
    pkt = SYN + bytes([len(iap1_ack)]) + iap1_ack + bytes([iap_checksum(bytes([len(iap1_ack)]) + iap1_ack)])
    send_and_check(dev, "iAP1 ACK for 0xEE", pkt)

    # iPodAck for identify (Lingo 0, Cmd 0x02, status pending, cmd 0x01)
    iap1_ack2 = bytes([0x00, 0x02, 0x06, 0x01])  # ACK pending for StartIDPS
    pkt = SYN + bytes([len(iap1_ack2)]) + iap1_ack2 + bytes([iap_checksum(bytes([len(iap1_ack2)]) + iap1_ack2)])
    send_and_check(dev, "iAP1 ACK pending", pkt)

    # StartIDPS (Lingo 0, Cmd 0x38) - Start Identification Process
    iap1_startidps = bytes([0x00, 0x38])
    pkt = SYN + bytes([len(iap1_startidps)]) + iap1_startidps + bytes([iap_checksum(bytes([len(iap1_startidps)]) + iap1_startidps)])
    send_and_check(dev, "iAP1 StartIDPS", pkt)

    # Phase 3: Try iAP2 link sync with full parameters
    print("\n" + "=" * 40)
    print("Phase 3: iAP2 Full Sync Packets")
    print("=" * 40)

    # Full iAP2 sync payload format:
    # version(1) + maxOutstanding(1) + maxPktLen(2) + retransmitTimeout(2) +
    # cumAckTimeout(2) + maxRetransmit(1) + maxCumAck(1) + numSessions(1) + checksum(1)
    sync_payload = struct.pack('>BBHHHHBBB',
        0x02,     # version 2
        0x01,     # max outstanding = 1
        0x0400,   # max packet length = 1024
        0x03E8,   # retransmit timeout = 1000ms
        0x0032,   # cum ack timeout = 50ms
        0x1E,     # max retransmissions = 30
        0x03,     # max cum acks = 3
        0x00,     # num session IDs = 0
    )
    cs = iap_checksum(sync_payload)
    pkt = SYN + sync_payload + bytes([cs])
    send_and_check(dev, "iAP2 Full Sync v2", pkt)

    # Try matching the device's parameters more closely
    # Device sends 02 00 EE 10 - if 02=maxOutstanding, 00 EE=maxPktLen(238)
    sync_payload2 = struct.pack('>BBHHHHBBB',
        0x02,     # version 2
        0x02,     # max outstanding = 2 (matching device?)
        0x00EE,   # max packet length = 238 (matching device?)
        0x03E8,   # retransmit timeout
        0x0032,   # cum ack timeout
        0x1E,     # max retransmissions
        0x03,     # max cum acks
        0x00,     # num session IDs
    )
    cs = iap_checksum(sync_payload2)
    pkt = SYN + sync_payload2 + bytes([cs])
    send_and_check(dev, "iAP2 Sync matching device params", pkt)

    # Phase 4: Try sending on raw iAP2 data link (FF 5A format)
    print("\n" + "=" * 40)
    print("Phase 4: iAP2 Data Packets (FF 5A)")
    print("=" * 40)

    # Try a simple data packet: FF 5A + len + control + seq + ack + session + payload + checksum
    for ctrl_byte in [0x00, 0x40, 0x80, 0xC0]:
        pkt_payload = bytes([ctrl_byte, 0x00, 0x00, 0x00])  # control, seq, ack, session
        pkt_len = len(pkt_payload) + 3  # +2 for length, +1 for checksum
        pkt = SOP + struct.pack('>H', pkt_len) + pkt_payload + bytes([iap_checksum(pkt_payload)])
        send_and_check(dev, f"Data pkt ctrl=0x{ctrl_byte:02x}", pkt)

    # Phase 5: Try mirroring / echoing the device's packet back
    print("\n" + "=" * 40)
    print("Phase 5: Echo / Mirror")
    print("=" * 40)

    # Echo exact same packet
    send_and_check(dev, "Echo device pkt", b'\xff\x55\x02\x00\xee\x10')

    # Try with modified checksum/data
    for data_byte in [0x00, 0x01, 0x02, 0x0F, 0xFF]:
        pkt_data = bytes([0x00, data_byte])
        cs = iap_checksum(bytes([len(pkt_data)]) + pkt_data)
        pkt = SYN + bytes([len(pkt_data)]) + pkt_data + bytes([cs])
        send_and_check(dev, f"iAP1 Lingo0 Cmd=0x{data_byte:02x}", pkt)

    # Phase 6: Check Interface 1 after all attempts
    print("\n" + "=" * 40)
    print("Phase 6: Check Interface 1")
    print("=" * 40)
    pkts = read_all(dev, 0x82, timeout=500)
    if pkts:
        for p in pkts:
            print(f"  EP 0x82: {len(p)} bytes: {p[:64].hex()}")
    else:
        print("  No data on Interface 1")

    # Cleanup
    for intf in [0, 1]:
        try:
            usb.util.release_interface(dev, intf)
        except:
            pass

    print("\n[DONE] Replug device to restore accessoryd.")


if __name__ == "__main__":
    main()
