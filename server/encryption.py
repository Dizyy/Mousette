"""End-to-end encryption between the phone and CouchPad (see PROTOCOL.md, "Encryption").

The PC keeps one random 32-byte key, saved so phones stay paired across restarts. The QR
code carries it after "#", so it reaches the phone through the camera and never travels over
the network. Each connection derives its own session key from it plus a random value from
each side, and numbers every message, so recorded traffic can neither be read nor replayed.

Messages are NaCl secretbox (XSalsa20-Poly1305): PyNaCl here, tweetnacl.js on the phone.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

KEY_BYTES = 32
NONCE_BYTES = 16          # random value each side contributes per connection
SESSION_LABEL = b"CouchPad session v1"
FROM_CLIENT, FROM_SERVER = 0, 1


class BadFrame(Exception):
    """A frame that doesn't decrypt: wrong key, tampered, replayed or out of order."""


def default_key_file() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
        return base / "CouchPad" / "key"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CouchPad" / "key"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "couchpad" / "key"


def load_key(path: Path, new: bool = False) -> bytes:
    """The saved key, or a fresh one (saved) when there is none or `new` is set."""
    if not new:
        try:
            key = path.read_bytes()
            if len(key) == KEY_BYTES:
                return key
        except OSError:
            pass
    key = os.urandom(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readable by this user only (on Windows the user's AppData folder already is).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _nonce(direction: int, counter: int) -> bytes:
    return bytes([direction]) + counter.to_bytes(8, "little") + bytes(SecretBox.NONCE_SIZE - 9)


class Session:
    """One connection's encryption. Every frame uses the next number in its direction, so a
    frame that is repeated, dropped or reordered fails to decrypt."""

    def __init__(self, key: bytes, server_nonce: bytes, client_nonce: bytes) -> None:
        session_key = hashlib.sha512(SESSION_LABEL + key + server_nonce + client_nonce).digest()[:32]
        self._box = SecretBox(session_key)
        self._received = 0
        self._sent = 0

    def decrypt(self, frame: bytes) -> str:
        try:
            plain = self._box.decrypt(frame, _nonce(FROM_CLIENT, self._received))
            text = plain.decode("utf-8")
        except (CryptoError, UnicodeDecodeError) as exc:
            raise BadFrame("frame doesn't decrypt") from exc
        self._received += 1
        return text

    def encrypt(self, text: str) -> bytes:
        frame = self._box.encrypt(text.encode("utf-8"), _nonce(FROM_SERVER, self._sent)).ciphertext
        self._sent += 1
        return frame
