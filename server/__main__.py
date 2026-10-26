"""Entry point: `python -m server [--host IP] [--port N] [--dry-run]`."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import secrets
import socket
import sys

import uvicorn

from . import __version__
from .app import create_app, is_lan_address
from .input_backend import DryRunBackend, PynputBackend, is_elevated
from .mdns import MdnsResponder

# Port 80 lets the phone open the address without ":port"; 8765 if something else has 80.
PREFERRED_PORTS = (80, 8765)


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Without this, Windows lets us share a port another program listens on.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def url(host: str, port: int) -> str:
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def lan_ip_candidates() -> list[str]:
    """This PC's private IPv4 addresses, most likely home network first.

    VPN adapters (typically on 10.x.x.x) often own the default
    route, so addresses are ranked rather than taken from the routing table:
    home routers almost always hand out 192.168.x.x, then 172.16-31.x, then 10.x.
    """
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # Connecting a UDP socket sends no packets; it only makes the OS pick a route.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
        except OSError:
            pass

    def usable(ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        return addr.is_private and not addr.is_loopback and not addr.is_link_local

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12"):
            return 1
        return 2

    return sorted((ip for ip in ips if usable(ip)), key=lambda ip: (rank(ip), ip))


def main() -> int:
    parser = argparse.ArgumentParser(prog="phonemouse", description="PhoneMouse PC server")
    parser.add_argument("--host", help="address to bind (default: this PC's LAN IP)")
    parser.add_argument("--port", type=int,
                        help="port to listen on (default: 80, or 8765 if 80 is taken)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log input events instead of moving the real mouse/keyboard")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    candidates = [] if args.host else lan_ip_candidates()
    host = args.host or (candidates[0] if candidates else None)
    if host is None:
        print("Could not find a local network address. Is this PC connected to Wi-Fi/Ethernet?")
        print("You can pass one explicitly, e.g.  start.bat --host 192.168.x.x")
        return 1
    if not is_lan_address(host):
        print(f"Refusing to bind to {host}: PhoneMouse only listens on private/LAN addresses.")
        return 1

    port = args.port or next((p for p in PREFERRED_PORTS if port_is_free(host, p)), None)
    if port is None:
        print(f"Ports {' and '.join(map(str, PREFERRED_PORTS))} are both in use. "
              "Choose another, e.g.  start.bat --port 9000")
        return 1

    # The .local name only makes sense on a real network, not for local tests.
    mdns = None if ipaddress.ip_address(host).is_loopback else MdnsResponder(host)
    hostname = mdns.start() if mdns else None

    pin = f"{secrets.randbelow(1_000_000):06d}"
    backend = DryRunBackend() if args.dry_run else PynputBackend()
    names = [hostname, host] if hostname else [host]
    app = create_app(backend, pin, addresses=[f"{name}:{port}" for name in names])

    bar = "=" * 46
    print(bar)
    print(f"  PhoneMouse {__version__} is running" + ("  (DRY RUN - no real input)" if args.dry_run else ""))
    print(f"  Open on iPhone/iPad:  {url(names[0], port)}")
    if hostname:
        print(f"  Or:                   {url(host, port)}")
    print(f"  PIN:                  {pin}")
    if len(candidates) > 1:
        print(f"  Other addresses:      {', '.join(candidates[1:])}")
        print("  (if the iPhone/iPad can't connect, try  start.bat --host <address>)")
    print("  Press Ctrl+C to stop.")
    print(bar, flush=True)
    if not is_elevated():
        logging.warning("Not running as administrator: Windows will ignore the phone while an admin "
                        "window (Task Manager, On-Screen Keyboard, installers) is in front. "
                        "Restart with start.bat and answer Y to fix this.")

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", ws_max_size=64 * 1024)
    finally:
        if mdns:
            mdns.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
