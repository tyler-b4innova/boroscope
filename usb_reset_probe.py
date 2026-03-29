#!/usr/bin/env python3
"""
Teslong TD300 - USB Reset + Capture Initial Handshake
Resets the USB device and captures the initial exchange to understand
the exact protocol the device expects.
"""

import usb.core
import usb.util
import struct
import time
import sys
import ctypes

VENDOR_ID = 0x3301
PRODUCT_ID = 0x2003


def iap_checksum(data):
    return (0x100 - (sum(data) & 0xFF)) & 0xFF


def find_and_setup():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("[ERROR] Device not found!")
        sys.exit(1)
    print(f"[OK] Found: {dev.product}")

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
    return dev


def read_ep(dev, ep, timeout=500):
    try:
        data = dev.read(ep, 16384, timeout=timeout)
        return bytes(data)
    except:
        return None


def fast_capture(dev, duration=3):
    """Capture all data from both endpoints for a duration"""
    print(f"\n[CAPTURE] Fast capture for {duration}s...")
    start = time.time()
    packets = []
    while time.time() - start < duration:
        for ep in [0x81, 0x82]:
            d = read_ep(dev, ep, timeout=100)
            if d:
                t = time.time() - start
                packets.append((t, ep, d))
    return packets


def display_packets(packets, label=""):
    if label:
        print(f"\n[{label}] {len(packets)} packets:")
    seen = {}
    for t, ep, d in packets:
        key = (ep, d)
        if key not in seen:
            seen[key] = 0
        seen[key] += 1
    for (ep, d), count in seen.items():
        suffix = f" x{count}" if count > 1 else ""
        print(f"  EP 0x{ep:02x}: {d.hex()}{suffix}")


def main():
    print("=" * 60)
    print("  Teslong TD300 - USB Reset + Protocol Discovery")
    print("=" * 60)

    dev = find_and_setup()

    # Step 1: Capture baseline
    print("\n--- Step 1: Baseline Capture ---")
    pkts = fast_capture(dev, 2)
    display_packets(pkts, "Baseline")

    # Step 2: USB device reset
    print("\n--- Step 2: USB Reset ---")
    try:
        dev.reset()
        print("[OK] USB reset sent")
        time.sleep(1)
    except Exception as e:
        print(f"[WARN] Reset: {e}")

    # Re-find and setup after reset
    time.sleep(2)
    dev = find_and_setup()

    # Step 3: Capture post-reset (device may send different init sequence)
    print("\n--- Step 3: Post-Reset Capture ---")
    pkts = fast_capture(dev, 3)
    display_packets(pkts, "Post-Reset")

    # Step 4: Try rapid-fire detection sequences
    print("\n--- Step 4: Rapid-fire detect sequences ---")

    # The device sends ff550200ee10 which is iAP1: Lingo=0, Cmd=0xEE
    # In the iPod Authentication Protocol, Cmd 0xEE is "iPodNotification"
    # or a request for the host to identify itself
    #
    # Common iAP1 host responses to accessory identification:
    # - RetAccessoryAuthenticationInfo (Lingo 0, Cmd 0x14)
    # - RetFIDTokenValues (Lingo 0, Cmd 0x3A)
    # - iPodAck (Lingo 0, Cmd 0x02)

    # Let's try the most likely sequence:
    # The device might be doing iAP2 negotiation:
    # ff 55 02 = "I want to switch to iAP2"
    # followed by 00 EE 10 = extra negotiation data
    #
    # BUT wait - what if this is actually:
    # ff 55 = START
    # 02 = length (2 bytes follow)
    # 00 = ... some protocol identifier
    # EE = ... some protocol identifier
    # 10 = checksum
    #
    # And the "00 EE" is actually a request ID, not lingo+cmd
    #
    # What if we need to respond with a matching "00 EE" + data?

    # Try sending a response with the same 00 EE pattern
    test_packets = [
        # Respond with matching pattern + data
        ("Match 00 EE + zeros", b'\xff\x55\x06\x00\xee\x00\x00\x00\x00'),
        ("Match 00 EE + 0x01", b'\xff\x55\x04\x00\xee\x01\x00'),
        # Maybe it's "00 EE" = protocol type, device wants response "00 EF"
        ("Response 00 EF", b'\xff\x55\x02\x00\xef'),
        # Maybe cmd 0xEE expects specific ACK
        ("Specific ACK EE", b'\xff\x55\x04\x00\x02\x00\xee'),
    ]

    # Fix checksums
    fixed_packets = []
    for label, pkt in test_packets:
        # Recalculate checksum: covers everything after FF 55 (len + data)
        body = pkt[2:-1] if len(pkt) > 3 else pkt[2:]
        cs = iap_checksum(body)
        fixed = pkt[:2] + body + bytes([cs])
        fixed_packets.append((label, fixed))

    for label, pkt in fixed_packets:
        # Drain
        for _ in range(5):
            read_ep(dev, 0x81, 100)

        print(f"\n[SEND] {label}: {pkt.hex()}")
        try:
            dev.write(0x01, pkt, timeout=1000)
        except Exception as e:
            print(f"  Write error: {e}")
            continue

        time.sleep(0.2)
        baseline = b'\xff\x55\x02\x00\xee\x10'
        for i in range(5):
            d = read_ep(dev, 0x81, 300)
            if d and d != baseline:
                print(f"  *** NEW: {d.hex()}")
            elif d:
                pass  # Same beacon
            else:
                break

    # Step 5: Try the approach that worked before - long packets that got different responses
    print("\n--- Step 5: Long packet probe (previously got different response) ---")

    # Previously: sent ff550011400000000008ea0000000001b2
    #             got  ff5500114000
    # The response was the first 6 bytes of our sent packet echoed back!
    # Let's try more variations to confirm this echo behavior

    test_long = [
        ("AA BB CC DD", bytes([0xFF, 0x55, 0xAA, 0xBB, 0xCC, 0xDD, 0x00, 0x00, 0x00, 0x00])),
        ("11 22 33 44", bytes([0xFF, 0x55, 0x11, 0x22, 0x33, 0x44, 0x00, 0x00, 0x00, 0x00])),
        ("Simple 01", bytes([0xFF, 0x55, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])),
        ("No prefix", bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])),
        ("Just 0xEE", bytes([0xEE, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])),
    ]

    for label, pkt in test_long:
        for _ in range(5):
            read_ep(dev, 0x81, 100)

        print(f"\n[SEND] {label}: {pkt.hex()}")
        try:
            dev.write(0x01, pkt, timeout=1000)
        except Exception as e:
            print(f"  Write error: {e}")
            continue

        time.sleep(0.15)
        baseline = b'\xff\x55\x02\x00\xee\x10'
        responses = []
        for i in range(5):
            d = read_ep(dev, 0x81, 300)
            if d:
                responses.append(d)
                if d != baseline:
                    print(f"  *** RESPONSE: {d.hex()}")
        if all(r == baseline for r in responses):
            print(f"  Same beacon x{len(responses)}")

    # Step 6: Try sending packets on EP 0x82 -> 0x02 (Interface 1) while
    # monitoring EP 0x81 for state changes
    print("\n--- Step 6: Interface 1 OUT while monitoring Interface 0 IN ---")
    for _ in range(5):
        read_ep(dev, 0x81, 100)

    test_if1 = bytes(512)  # 512 zeros
    print(f"[SEND] 512 zeros on EP 0x02")
    try:
        dev.write(0x02, test_if1, timeout=2000)
        time.sleep(0.2)
        # Check both endpoints
        for ep in [0x81, 0x82]:
            d = read_ep(dev, ep, 500)
            if d:
                print(f"  EP 0x{ep:02x}: {len(d)} bytes: {d[:32].hex()}")
            else:
                print(f"  EP 0x{ep:02x}: no data")
    except Exception as e:
        print(f"  Write error: {e}")

    # Cleanup
    for intf in [0, 1]:
        try:
            usb.util.release_interface(dev, intf)
        except:
            pass
    print("\n[DONE]")


if __name__ == "__main__":
    main()
