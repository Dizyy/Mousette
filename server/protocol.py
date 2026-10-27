"""Mousette wire protocol, version 1.

Pure validation logic with no dependency on FastAPI, pynput or the web client.
See PROTOCOL.md for the human-readable specification.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

PROTOCOL_VERSION = 1

BUTTONS = ("left", "right", "middle")

# Keys accepted by the "key" message.
NAMED_KEYS = (
    "enter", "backspace", "tab", "esc", "delete", "home", "end",
    "up", "down", "left", "right",
    "space", "page_up", "page_down",
    "volume_down", "volume_mute", "volume_up",
    "media_previous", "media_play_pause", "media_next",
)

# Extra names accepted inside "hotkey" combinations.
MODIFIER_KEYS = ("ctrl", "alt", "shift", "win")
FUNCTION_KEYS = tuple(f"f{i}" for i in range(1, 13))
HOTKEY_NAMES = NAMED_KEYS + MODIFIER_KEYS + FUNCTION_KEYS

MAX_MOVE = 4000          # pixels per message
MAX_SCROLL = 100         # wheel notches per message
MAX_TEXT_LENGTH = 2000   # characters per type_text message
MAX_PASTE_LENGTH = 100_000  # characters per paste_text message
MAX_HOTKEY_KEYS = 5
MAX_PIN_LENGTH = 32
MAX_TOKEN_LENGTH = 64
ENC_NONCE_BYTES = 16


class ProtocolError(Exception):
    """A message was rejected. `code` is a stable, machine-readable string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def make(type_: str, **fields: Any) -> dict[str, Any]:
    """Build an outgoing (server -> client) message."""
    return {"v": PROTOCOL_VERSION, "type": type_, **fields}


def error(code: str, message: str, ref: str | None = None) -> dict[str, Any]:
    msg = make("error", code=code, message=message)
    if ref is not None:
        msg["ref"] = ref
    return msg


def _number(msg: dict, field: str, limit: float) -> float:
    value = msg.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolError("bad_field", f"'{field}' must be a finite number")
    if abs(value) > limit:
        raise ProtocolError("bad_field", f"'{field}' must be between -{limit} and {limit}")
    return float(value)


def _button(msg: dict) -> str:
    button = msg.get("button", "left")
    if button not in BUTTONS:
        raise ProtocolError("bad_field", f"'button' must be one of {', '.join(BUTTONS)}")
    return button


def _hotkey_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ProtocolError("bad_field", "hotkey keys must be non-empty strings")
    lowered = name.lower()
    if lowered in HOTKEY_NAMES:
        return lowered
    if len(name) == 1 and name.isprintable():
        return lowered
    raise ProtocolError("bad_field", f"unknown key name '{name}'")


def _validate_move(msg: dict) -> dict:
    return {"dx": _number(msg, "dx", MAX_MOVE), "dy": _number(msg, "dy", MAX_MOVE)}


def _validate_scroll(msg: dict) -> dict:
    return {"dx": _number(msg, "dx", MAX_SCROLL), "dy": _number(msg, "dy", MAX_SCROLL)}


def _validate_button(msg: dict) -> dict:
    return {"button": _button(msg)}


def _validate_double_click(msg: dict) -> dict:
    return {"button": _button(msg)}


def _validate_type_text(msg: dict) -> dict:
    text = msg.get("text")
    if not isinstance(text, str):
        raise ProtocolError("bad_field", "'text' must be a string")
    if len(text) > MAX_TEXT_LENGTH:
        raise ProtocolError("bad_field", f"'text' is longer than {MAX_TEXT_LENGTH} characters")
    return {"text": text}


def _validate_paste_text(msg: dict) -> dict:
    text = msg.get("text")
    if not isinstance(text, str) or not text:
        raise ProtocolError("bad_field", "'text' must be a non-empty string")
    if len(text) > MAX_PASTE_LENGTH:
        raise ProtocolError("bad_field", f"'text' is longer than {MAX_PASTE_LENGTH} characters")
    return {"text": text}


def _validate_key(msg: dict) -> dict:
    name = msg.get("name")
    if not isinstance(name, str) or name.lower() not in NAMED_KEYS:
        raise ProtocolError("bad_field", f"'name' must be one of {', '.join(NAMED_KEYS)}")
    return {"name": name.lower()}


def _validate_hotkey(msg: dict) -> dict:
    keys = msg.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_HOTKEY_KEYS:
        raise ProtocolError("bad_field", f"'keys' must be a list of 1-{MAX_HOTKEY_KEYS} key names")
    return {"keys": [_hotkey_name(k) for k in keys]}


def _validate_no_fields(msg: dict) -> dict:
    return {}


def _validate_auth(msg: dict) -> dict:
    # Either the PIN, or the token from an earlier auth_ok (lets the phone log in again
    # after iOS reloaded the page, without asking for the PIN).
    if "token" in msg and "pin" not in msg:
        token = msg["token"]
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_LENGTH:
            raise ProtocolError("bad_field", "'token' must be a string")
        return {"token": token}
    pin = msg.get("pin")
    if not isinstance(pin, str) or not 0 < len(pin) <= MAX_PIN_LENGTH:
        raise ProtocolError("bad_field", "'pin' must be a string")
    return {"pin": pin}


def _validate_enc_hello(msg: dict) -> dict:
    # Starts an encrypted connection; "nonce" is the client's random value (base64url, 16 bytes).
    nonce = msg.get("nonce")
    try:
        raw = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4)) if isinstance(nonce, str) else b""
    except ValueError:
        raw = b""
    if len(raw) != ENC_NONCE_BYTES:
        raise ProtocolError("bad_field", f"'nonce' must be {ENC_NONCE_BYTES} random bytes in base64url")
    return {"nonce": raw}


def _validate_ping(msg: dict) -> dict:
    # "id" is optional and echoed back verbatim in the pong.
    if "id" in msg and not isinstance(msg["id"], (int, float, str)):
        raise ProtocolError("bad_field", "'id' must be a number or string")
    return {"id": msg.get("id")}


_VALIDATORS = {
    "auth": _validate_auth,
    "enc_hello": _validate_enc_hello,
    "ping": _validate_ping,
    "move": _validate_move,
    "click": _validate_button,
    "double_click": _validate_double_click,
    "mouse_down": _validate_button,
    "mouse_up": _validate_button,
    "scroll": _validate_scroll,
    "type_text": _validate_type_text,
    "paste_text": _validate_paste_text,
    "key": _validate_key,
    "hotkey": _validate_hotkey,
    "task_manager": _validate_no_fields,
    "shutdown": _validate_no_fields,
    "cancel_shutdown": _validate_no_fields,
}

MESSAGE_TYPES = tuple(_VALIDATORS)
INPUT_TYPES = tuple(t for t in MESSAGE_TYPES if t not in ("auth", "enc_hello", "ping"))
# Actions the server confirms with "action_ok", so the client can show feedback.
ACTION_TYPES = ("task_manager", "shutdown", "cancel_shutdown", "paste_text")


def parse(raw: str) -> tuple[str, dict]:
    """Parse and validate one incoming text frame.

    Returns (type, normalized_fields). Raises ProtocolError on any problem.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        raise ProtocolError("bad_json", "message is not valid JSON") from None
    if not isinstance(msg, dict):
        raise ProtocolError("bad_json", "message must be a JSON object")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("bad_version", f"'v' must be {PROTOCOL_VERSION}")
    type_ = msg.get("type")
    validator = _VALIDATORS.get(type_) if isinstance(type_, str) else None
    if validator is None:
        raise ProtocolError("unknown_type", f"unknown message type {type_!r}")
    return type_, validator(msg)
