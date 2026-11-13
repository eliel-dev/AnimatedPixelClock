import struct
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "twin-wsl2"))

import flash_update
import http_bridge
import improv_packet


class ImprovPacketTests(unittest.TestCase):
    def test_packet_contains_open_wifi_credentials_and_checksum(self):
        packet = bytes.fromhex(improv_packet.packet_hex("PixelClock-TWIN", ""))
        self.assertEqual(packet[:6], b"IMPROV")
        self.assertEqual(packet[6:8], bytes((1, 3)))
        self.assertEqual(packet[-1], sum(packet[:-1]) & 0xFF)
        self.assertIn(b"PixelClock-TWIN", packet)

    def test_rejects_credentials_that_twin_cannot_represent(self):
        with self.assertRaisesRegex(ValueError, "commas"):
            improv_packet.packet_hex("Pixel,Clock", "")


class FlashUpdateTests(unittest.TestCase):
    def test_updates_boot_and_app_without_changing_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flash_path = root / "flash.bin"
            flash_path.write_bytes(b"\xa5" * flash_update.FLASH_SIZE)

            bootloader = root / "bootloader.bin"
            bootloader.write_bytes(b"boot")
            partitions = root / "partitions.bin"
            app0 = bytearray(32)
            app0[:4] = b"\xaa\x50\x00\x10"
            struct.pack_into("<II", app0, 4, flash_update.APP0_OFFSET, 0x640000)
            partitions.write_bytes(app0)
            boot_app0 = root / "boot_app0.bin"
            boot_app0.write_bytes(b"ota")
            firmware = root / "firmware.bin"
            firmware.write_bytes(b"app-image")

            flash_update.update_flash(SimpleNamespace(
                flash=flash_path,
                bootloader=bootloader,
                partitions=partitions,
                boot_app0=boot_app0,
                firmware=firmware,
            ))

            result = flash_path.read_bytes()
            self.assertEqual(result[:4], b"boot")
            self.assertEqual(result[flash_update.PARTITIONS_OFFSET:
                                    flash_update.PARTITIONS_OFFSET + 32], bytes(app0))
            self.assertEqual(result[flash_update.OTA_DATA_OFFSET:
                                    flash_update.OTA_DATA_OFFSET + 3], b"ota")
            self.assertEqual(result[flash_update.APP0_OFFSET:
                                    flash_update.APP0_OFFSET + 9], b"app-image")
            self.assertEqual(result[0x9000], 0xA5)
            self.assertEqual(result[0xC90000], 0xA5)


class HttpBridgeTests(unittest.TestCase):
    def test_relays_request_and_response_and_propagates_half_close(self):
        client, bridge_client = socket.socketpair()
        bridge_upstream, upstream = socket.socketpair()
        client.settimeout(1)
        upstream.settimeout(1)
        relay = threading.Thread(
            target=http_bridge.BridgeHandler._relay,
            args=(bridge_client, bridge_upstream),
        )
        relay.start()
        try:
            client.sendall(b"GET /api/status HTTP/1.1\r\n\r\n")
            self.assertEqual(upstream.recv(128), b"GET /api/status HTTP/1.1\r\n\r\n")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(upstream.recv(1), b"")
            upstream.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            self.assertEqual(client.recv(128), b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            upstream.shutdown(socket.SHUT_WR)
            relay.join(timeout=1)
            self.assertFalse(relay.is_alive())
        finally:
            for connection in (client, bridge_client, bridge_upstream, upstream):
                connection.close()


if __name__ == "__main__":
    unittest.main()
