"""Tiny mDNS responder: makes "couchpad.local" point at this PC.

iPhones and iPads look up .local names with multicast DNS (Bonjour). This answers
those lookups for one name with one IPv4 address. Standard library only: no
extra package, nothing for Windows' Smart App Control to block.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("couchpad.mdns")

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
TYPE_A = 1
TYPE_ANY = 255
CLASS_IN = 1
CACHE_FLUSH = 0x8000
TTL_S = 120
# Repeat the announcement this often. A phone waking from sleep may have forgotten the name,
# and its own lookups can be blocked (e.g. a firewall rule that only opens TCP ports).
REANNOUNCE_S = 30


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def _read_name(data: bytes, pos: int) -> tuple[str, int]:
    """Returns (name, position after it), following compression pointers."""
    labels, end, jumps = [], None, 0
    while True:
        length = data[pos]
        if length & 0xC0 == 0xC0:  # pointer to an earlier name
            if end is None:
                end = pos + 2
            pos = ((length & 0x3F) << 8) | data[pos + 1]
            jumps += 1
            if jumps > 20:
                raise ValueError("pointer loop")
            continue
        pos += 1
        if length == 0:
            break
        labels.append(data[pos:pos + length].decode("utf-8", "replace"))
        pos += length
    return ".".join(labels), end if end is not None else pos


def _parse(data: bytes) -> tuple[int, int, list[tuple[str, int, int]], list[tuple[str, int, bytes]]]:
    """Returns (id, flags, questions[(name, type, class)], answers[(name, type, rdata)])."""
    msg_id, flags, qd, an = struct.unpack_from("!HHHH", data)
    pos = 12
    questions, answers = [], []
    for _ in range(qd):
        name, pos = _read_name(data, pos)
        qtype, qclass = struct.unpack_from("!HH", data, pos)
        pos += 4
        questions.append((name.lower(), qtype, qclass))
    for _ in range(an):
        name, pos = _read_name(data, pos)
        rtype, _cls, _ttl, rdlen = struct.unpack_from("!HHIH", data, pos)
        pos += 10
        answers.append((name.lower(), rtype, data[pos:pos + rdlen]))
        pos += rdlen
    return msg_id, flags, questions, answers


class MdnsResponder:
    """Answers "<name>.local" with `ip` until stop() is called."""

    def __init__(self, ip: str, name: str = "couchpad"):
        self.ip = ip
        self._packed_ip = socket.inet_aton(ip)
        self._wanted = name
        self.hostname: str | None = None  # e.g. "couchpad.local" once claimed
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> str | None:
        """Claims a free name (couchpad, couchpad-2, ...) and starts answering.

        Returns the hostname, or None when mDNS isn't available on this network.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            # Windows' own DNS client also listens on 5353; share the port with it.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", MDNS_PORT))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(MDNS_GROUP) + self._packed_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, self._packed_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError as exc:
            log.warning("Could not start the .local name (%s); use the IP address instead.", exc)
            return None
        self._sock = sock

        for n in range(1, 10):
            candidate = f"{self._wanted}.local" if n == 1 else f"{self._wanted}-{n}.local"
            if not self._taken(candidate):
                self.hostname = candidate
                break
        if self.hostname is None:
            sock.close()
            return None

        threading.Thread(target=self._serve, name="mdns", daemon=True).start()
        for _ in range(2):  # announce, so phones that asked earlier update their cache
            self._send(self._answer(0), (MDNS_GROUP, MDNS_PORT))
            time.sleep(0.25)
        return self.hostname

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None and self.hostname is not None:
            try:  # goodbye: TTL 0 tells phones to forget the name
                self._send(self._answer(0, ttl=0), (MDNS_GROUP, MDNS_PORT))
            except OSError:
                pass
            self._sock.close()

    def _taken(self, hostname: str) -> bool:
        """Asks the network whether another device already uses `hostname`."""
        query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_name(hostname) + struct.pack("!HH", TYPE_A, CLASS_IN)
        self._sock.settimeout(0.25)
        deadline = time.monotonic() + 0.75
        self._send(query, (MDNS_GROUP, MDNS_PORT))
        while time.monotonic() < deadline:
            try:
                data, _ = self._sock.recvfrom(9000)
                _, flags, _, answers = _parse(data)
            except (OSError, ValueError, struct.error, IndexError):
                continue
            if flags & 0x8000 and any(n == hostname and t == TYPE_A and r != self._packed_ip
                                      for n, t, r in answers):
                return True
        return False

    def _answer(self, msg_id: int, ttl: int = TTL_S, question: bytes = b"") -> bytes:
        qd = 1 if question else 0
        header = struct.pack("!HHHHHH", msg_id, 0x8400, qd, 1, 0, 0)  # response, authoritative
        record = (_encode_name(self.hostname) + struct.pack("!HHIH", TYPE_A, CLASS_IN | CACHE_FLUSH, ttl, 4)
                  + self._packed_ip)
        return header + question + record

    def _send(self, packet: bytes, addr: tuple[str, int]) -> None:
        self._sock.sendto(packet, addr)

    def _serve(self) -> None:
        self._sock.settimeout(1.0)
        last_announce = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_announce >= REANNOUNCE_S:
                last_announce = time.monotonic()
                try:
                    self._send(self._answer(0), (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass
            try:
                data, src = self._sock.recvfrom(9000)
                msg_id, flags, questions, _ = _parse(data)
            except socket.timeout:
                continue
            except (OSError, ValueError, struct.error, IndexError):
                if self._stop.is_set():
                    return
                continue
            if flags & 0x8000:  # a response, not a question
                continue
            for name, qtype, qclass in questions:
                if name != self.hostname or qtype not in (TYPE_A, TYPE_ANY):
                    continue
                try:
                    if src[1] != MDNS_PORT:
                        # Simple resolvers ask from a random port: answer them directly,
                        # repeating their question and id.
                        question = _encode_name(self.hostname) + struct.pack("!HH", qtype, qclass & 0x7FFF)
                        self._send(self._answer(msg_id, question=question), src)
                    else:
                        self._send(self._answer(0), (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass
