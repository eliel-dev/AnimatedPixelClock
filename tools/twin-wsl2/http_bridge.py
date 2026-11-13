#!/usr/bin/env python3
"""Forward the Companion's fixed HTTP port to TWIN's loopback web forward."""
import logging
import os
import selectors
import socket
import socketserver

LISTEN = ("127.0.0.1", 80)
TARGET = ("127.0.0.1", 8080)


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        try:
            with socket.create_connection(TARGET, timeout=5) as upstream:
                upstream.settimeout(None)
                self._relay(client, upstream)
        except OSError as error:
            logging.warning("HTTP bridge to %s:%d failed: %s", *TARGET, error)

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(client, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, client)
            while selector.get_map():
                for event, _ in selector.select():
                    source = event.fileobj
                    destination = event.data
                    data = source.recv(65536)
                    if data:
                        destination.sendall(data)
                        continue
                    selector.unregister(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass


class BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Port 80 is privileged here; run this helper with sudo.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        with BridgeServer(LISTEN, BridgeHandler) as server:
            logging.info("Forwarding http://127.0.0.1:80 to http://127.0.0.1:8080 (loopback only)")
            server.serve_forever()
    except OSError as error:
        raise SystemExit(f"Cannot bind {LISTEN[0]}:{LISTEN[1]}: {error}")


if __name__ == "__main__":
    main()
