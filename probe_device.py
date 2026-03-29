#!/usr/bin/env python3
"""
Teslong TD300 USB Protocol Probe
Attempts to communicate with the device by taking over from accessoryd
and probing the command protocol.
"""

import usb.core
import usb.util
import struct
import time
import sys
import os

VENDOR_ID = 0x3301
PRODUCT_ID = 0x2003

# iAP2 constants
IAP2_SYNC_BYTE1 = 0xFF
IAP2_SYNC_BYTE2 = 0x55
IAP2_SOP_BYTE2 = 0x5A  # Start of Packet (link control)

def find_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("[ERROR] TD300 not found!")
        sys.exit(1)
    print(f"[OK] Found: {dev.product} by {dev.manufacturer}")
    return dev


def detach_all_drivers(dev):
    """Detach kernel drivers from all interfaces"""
    for intf_num in [0, 1]:
        try:
            if dev.is_kernel_driver_active(intf_num):
                dev.detach_kernel_driver(intf_num)
                print(f"[OK] Detached kernel driver from interface {intf_num}")
            else:
                print(f"[OK] No kernel driver on interface {intf_num}")
        except Exception as e:
            print(f"[WARN] Interface {intf_num} detach: {e}")


def claim_interfaces(dev):
    """Claim all interfaces"""
    try:
        dev.set_configuration()
    except:
        pass
    for intf_num in [0, 1]:
        try:
            usb.util.claim_interface(dev, intf_num)
            print(f"[OK] Claimed interface {intf_num}")
        except Exception as e:
            print(f"[WARN] Claim interface {intf_num}: {e}")


def try_iap2_link_sync(dev):
    """Try to establish iAP2 link synchronization on Interface 0"""
    print("\n=== Probing iAP2 Link Layer (Interface 0, EP 0x01/0x81) ===")

    # iAP2 Link Synchronization payload
    # The sync packet has a specific format
    link_sync = bytes([
        0xFF, 0x55,  # SYN bytes
        0x02,        # Link version
        0x00,        # Max outstanding packets
        0x00, 0x00,  # Max packet length (negotiated)
        0x00, 0x00,  # Retransmit timeout
        0x00, 0x00,  # Cumulative ACK timeout
        0x00,        # Max retransmissions
        0x00,        # Max cumulative ACKs
        0x00, 0x00, 0x00, 0x00,  # Session IDs (accessory -> device)
    ])

    # Try reading first - device might have pending data
    print("[PROBE] Reading pending data from EP 0x81...")
    for i in range(3):
        try:
            data = dev.read(0x81, 4096, timeout=1000)
            print(f"  Read {len(data)} bytes: {data[:64].tobytes().hex()}")
        except usb.core.USBTimeoutError:
            print(f"  Attempt {i+1}: timeout (no data)")
            break
        except Exception as e:
            print(f"  Attempt {i+1}: {e}")
            break

    # Try writing iAP2 sync bytes
    print("[PROBE] Sending iAP2 link sync...")
    try:
        written = dev.write(0x01, link_sync, timeout=2000)
        print(f"  Wrote {written} bytes")

        time.sleep(0.1)
        try:
            data = dev.read(0x81, 4096, timeout=2000)
            print(f"  Response: {len(data)} bytes")
            print(f"  Hex: {data[:64].tobytes().hex()}")
            return True
        except usb.core.USBTimeoutError:
            print("  No response")
        except Exception as e:
            print(f"  Read error: {e}")
    except Exception as e:
        print(f"  Write error: {e}")

    # Try a simple iAP2 SYN ACK pattern
    print("[PROBE] Sending 0xFF 0x55 0xAA...")
    for payload in [
        bytes([0xFF, 0x55, 0xAA]),
        bytes([0xFF, 0x5A, 0x00, 0x01]),
        bytes([0xFF, 0x55, 0x02]),
    ]:
        try:
            dev.write(0x01, payload, timeout=1000)
            time.sleep(0.05)
            data = dev.read(0x81, 4096, timeout=1000)
            print(f"  Sent {payload.hex()} -> Got {len(data)} bytes: {data[:32].tobytes().hex()}")
            return True
        except:
            pass

    return False


def try_interface1_with_alt(dev):
    """Try Interface 1 with alt setting 1"""
    print("\n=== Probing Data Channel (Interface 1, EP 0x02/0x82) ===")

    try:
        dev.set_interface_altsetting(interface=1, alternate_setting=1)
        print("[OK] Set Interface 1 Alt Setting 1")
    except Exception as e:
        print(f"[WARN] Set alt setting: {e}")
        return

    # Read first
    print("[PROBE] Reading from EP 0x82...")
    try:
        data = dev.read(0x82, 16384, timeout=2000)
        print(f"  Read {len(data)} bytes!")
        print(f"  First 64 bytes: {data[:64].tobytes().hex()}")
        return True
    except usb.core.USBTimeoutError:
        print("  Timeout - no data")
    except Exception as e:
        print(f"  Error: {e}")

    # Try CBW-style commands (512-byte command blocks, like from the g512CBW variable)
    print("[PROBE] Trying CBW-style commands on EP 0x02...")

    # Based on the ZMFICamera strings: begincmd, getdevinfo, openCamera, getpic
    # CBW format is typically: signature + tag + data_transfer_length + flags + LUN + command_length + command
    # But this is a proprietary protocol, not standard SCSI

    # Try simple command patterns
    commands = {
        "getdevinfo": bytes([0x01, 0x00, 0x00, 0x00]),
        "getdevflag": bytes([0x02, 0x00, 0x00, 0x00]),
        "openCamera": bytes([0x03, 0x00, 0x00, 0x00]),
        "start_stream": bytes([0x10, 0x00, 0x00, 0x00]),
        "zero_cmd": bytes([0x00, 0x00, 0x00, 0x00]),
    }

    for name, cmd in commands.items():
        # Pad to 512 bytes like g512CBW
        padded = cmd + bytes(512 - len(cmd))
        try:
            written = dev.write(0x02, padded, timeout=1000)
            print(f"  {name}: wrote {written} bytes")
            time.sleep(0.1)
            try:
                data = dev.read(0x82, 16384, timeout=1500)
                print(f"    Response: {len(data)} bytes")
                print(f"    Hex: {data[:64].tobytes().hex()}")
                return True
            except usb.core.USBTimeoutError:
                print(f"    No response")
            except Exception as e:
                print(f"    Read error: {e}")
        except Exception as e:
            print(f"  {name}: write error: {e}")

    return False


def try_control_transfers(dev):
    """Try USB control transfers - some cameras respond to standard USB requests"""
    print("\n=== Probing USB Control Transfers ===")

    # Try to get additional descriptors
    control_requests = [
        # (bmRequestType, bRequest, wValue, wIndex, length, description)
        (0x80, 0x06, 0x0F00, 0x00, 256, "BOS descriptor"),
        (0x80, 0x06, 0x0200, 0x00, 512, "Full config descriptor"),
        (0xC0, 0x01, 0x0000, 0x00, 512, "Vendor GET 0x01"),
        (0xC0, 0x51, 0x0000, 0x00, 512, "Vendor GET 0x51"),
        (0xC0, 0x00, 0x0000, 0x00, 64, "Vendor GET 0x00"),
        (0xC1, 0x01, 0x0000, 0x00, 512, "Vendor Intf GET 0x01"),
        (0xC1, 0x01, 0x0000, 0x01, 512, "Vendor Intf1 GET 0x01"),
    ]

    for bmReq, bReq, wVal, wIdx, length, desc in control_requests:
        try:
            data = dev.ctrl_transfer(bmReq, bReq, wVal, wIdx, length, timeout=1000)
            if len(data) > 0:
                print(f"  {desc}: {len(data)} bytes")
                hex_str = bytes(data[:min(64, len(data))]).hex(' ')
                print(f"    {hex_str}")
        except usb.core.USBError as e:
            if "pipe" not in str(e).lower() and "stall" not in str(e).lower():
                print(f"  {desc}: {e}")


def try_crossover_probe(dev):
    """Try sending commands on Interface 0 to activate Interface 1"""
    print("\n=== Cross-Interface Probe ===")
    print("[PROBE] Sending camera commands on iAP EP 0x01, reading data on EP 0x82...")

    # Ensure Interface 1 is on alt setting 1
    try:
        dev.set_interface_altsetting(interface=1, alternate_setting=1)
    except:
        pass

    # The ZMFICamera framework sends commands via EASession (which wraps Interface 0/1)
    # But it seems like the commands go over the EAP session on Interface 1
    # Let's try sending the "openCamera" command format on Interface 0
    # and see if data appears on Interface 1

    # From the binary: begincmd builds a command header, sends it,
    # then data flows, then endcmd closes it

    # Try various known iAP2 message IDs for starting ExternalAccessory session
    # iAP2 message: 2-byte length + 2-byte message ID + payload
    # Message 0xEA00 = Start ExternalAccessory Protocol Session
    iap2_start_eap = struct.pack('>HH',
        8,       # length (header + payload)
        0xEA00,  # Start EAP Session message ID
    ) + struct.pack('>HH',
        0x0000,  # Protocol ID
        0x0001,  # Session ID
    )

    # Wrap in iAP2 link packet
    # Link packet: FF 55 + 2-byte length + control + seq + ack + session + payload + checksum
    seq = 0
    ack = 0
    session_id = 0
    control = 0x40  # ACK flag
    payload = iap2_start_eap
    pkt_len = 9 + len(payload)  # header(6) + payload + checksum(1)

    link_header = struct.pack('>BBHBBBB',
        0xFF, 0x55,
        pkt_len,
        control,
        seq,
        ack,
        session_id,
    )

    # Simple XOR checksum
    full_pkt = link_header + payload
    checksum = 0
    for b in full_pkt[2:]:  # skip sync bytes
        checksum ^= b
    full_pkt += bytes([checksum])

    print(f"[PROBE] Sending iAP2 EAP start on EP 0x01: {full_pkt.hex()}")
    try:
        dev.write(0x01, full_pkt, timeout=2000)
        time.sleep(0.2)

        # Read from both endpoints
        for ep in [0x81, 0x82]:
            try:
                data = dev.read(ep, 16384, timeout=1000)
                print(f"  EP 0x{ep:02x}: {len(data)} bytes -> {data[:64].tobytes().hex()}")
            except usb.core.USBTimeoutError:
                print(f"  EP 0x{ep:02x}: timeout")
            except Exception as e:
                print(f"  EP 0x{ep:02x}: {e}")
    except Exception as e:
        print(f"  Write error: {e}")


def main():
    print("=" * 60)
    print("  Teslong TD300 USB Protocol Probe")
    print("=" * 60)

    if os.geteuid() != 0:
        print("[WARN] Not running as root - some operations may fail")
        print("[HINT] Run with: sudo /tmp/boroscope-venv/bin/python3 probe_device.py")

    dev = find_device()

    # Detach system drivers
    detach_all_drivers(dev)

    # Claim interfaces
    claim_interfaces(dev)

    # Probe control transfers first (non-destructive)
    try_control_transfers(dev)

    # Probe iAP2 link on Interface 0
    try_iap2_link_sync(dev)

    # Probe data channel on Interface 1
    try_interface1_with_alt(dev)

    # Try cross-interface activation
    try_crossover_probe(dev)

    # Cleanup
    print("\n=== Cleanup ===")
    for intf in [0, 1]:
        try:
            usb.util.release_interface(dev, intf)
            print(f"[OK] Released interface {intf}")
        except:
            pass

    print("\n[INFO] Probe complete. You may need to replug the device for accessoryd to reclaim it.")


if __name__ == "__main__":
    main()
