#!/usr/bin/env python3
"""Print an Improv-Serial Wi-Fi provisioning packet as hex."""
import sys


def packet_hex(ssid: str, password: str) -> str:
    ssid_bytes = ssid.encode("utf-8")
    password_bytes = password.encode("utf-8")
    if not 1 <= len(ssid_bytes) <= 32 or len(password_bytes) > 64:
        raise ValueError("Wi-Fi SSID must be 1-32 bytes and password at most 64 bytes")
    if b"," in ssid_bytes or b"," in password_bytes:
        raise ValueError("TWIN's --wifi setting cannot contain commas")

    rpc = bytes((len(ssid_bytes),)) + ssid_bytes
    rpc += bytes((len(password_bytes),)) + password_bytes
    if len(rpc) > 253:
        raise ValueError("Wi-Fi credentials exceed the Improv-Serial packet limit")
    data = bytes((0x01, len(rpc))) + rpc
    frame = b"IMPROV" + bytes((0x01, 0x03, len(data))) + data
    return (frame + bytes((sum(frame) & 0xFF,))).hex()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: improv_packet.py SSID PASSWORD")
    try:
        print(packet_hex(sys.argv[1], sys.argv[2]))
    except ValueError as error:
        raise SystemExit(str(error))
