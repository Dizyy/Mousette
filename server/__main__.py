"""Entry point: `python -m server [--host IP] [--port N] [--dry-run]`."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import secrets
import socket
import sys

import uvicorn

from .app import create_app, is_lan_address
from .input_backend import DryRunBackend, PynputBackend, is_elevated

DEFAULT_PORT = 8765


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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
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

    pin = f"{secrets.randbelow(1_000_000):06d}"
    backend = DryRunBackend() if args.dry_run else PynputBackend()
    app = create_app(backend, pin, address=f"{host}:{args.port}")

    bar = "=" * 46
    print(bar)
    print("  PhoneMouse is running" + ("  (DRY RUN - no real input)" if args.dry_run else ""))
    print(f"  Open on iPhone/iPad:  http://{host}:{args.port}")
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

    uvicorn.run(app, host=host, port=args.port, log_level="warning", ws_max_size=64 * 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
