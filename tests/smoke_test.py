"""End-to-end protocol test.

Starts the server in --dry-run mode on 127.0.0.1 (so your real mouse and
keyboard are never touched), then sends every protocol message type over a
WebSocket and checks the server accepted and executed each one.

Run:  .venv\\Scripts\\python.exe tests\\smoke_test.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import segno  # noqa: E402
from nacl.secret import SecretBox  # noqa: E402
from server.__main__ import connect_url  # noqa: E402
from server.app import PinGuard, create_app  # noqa: E402
from server.input_backend import DryRunBackend  # noqa: E402
from server.encryption import load_key  # noqa: E402
from server.qr import qr_lines  # noqa: E402
HOST = "127.0.0.1"

# (message, substring expected in the server's dry-run log)
INPUT_MESSAGES = [
    ({"type": "move", "dx": 12, "dy": -7}, "DRY-RUN move dx=12 dy=-7"),
    ({"type": "move", "dx": 0.4, "dy": 0.4}, None),                  # sub-pixel: buffered
    ({"type": "move", "dx": 0.7, "dy": 0.7}, "DRY-RUN move dx=1 dy=1"),  # remainder flushed
    ({"type": "click", "button": "left"}, "DRY-RUN click button=left count=1"),
    ({"type": "click", "button": "right"}, "DRY-RUN click button=right count=1"),
    ({"type": "click", "button": "middle"}, "DRY-RUN click button=middle count=1"),
    ({"type": "double_click"}, "DRY-RUN click button=left count=2"),
    ({"type": "mouse_down", "button": "left"}, "DRY-RUN mouse_down button=left"),
    ({"type": "mouse_up", "button": "left"}, "DRY-RUN mouse_up button=left"),
    ({"type": "scroll", "dx": 0, "dy": -1.5}, "DRY-RUN scroll dx=0.000 dy=-1.500"),
    ({"type": "zoom", "steps": 0.75}, "DRY-RUN zoom steps=0.750"),
    ({"type": "type_text", "text": "Héllo! café naïve ñ ÉÜ 😀👍"}, "DRY-RUN type_text text='Héllo! café naïve ñ ÉÜ 😀👍'"),
    *[({"type": "key", "name": n}, f"DRY-RUN hotkey keys={n}")
      for n in ("enter", "backspace", "tab", "esc", "up", "down", "left", "right", "delete", "home", "end")],
    ({"type": "hotkey", "keys": ["win", "s"]}, "DRY-RUN hotkey keys=win+s"),
    ({"type": "hotkey", "keys": ["alt", "tab"]}, "DRY-RUN hotkey keys=alt+tab"),
    ({"type": "hotkey", "keys": ["win", "d"]}, "DRY-RUN hotkey keys=win+d"),
    # Trackpad gestures: Task view, app switcher, virtual desktops, notifications.
    ({"type": "hotkey", "keys": ["win", "tab"]}, "DRY-RUN hotkey keys=win+tab"),
    ({"type": "hotkey", "keys": ["ctrl", "alt", "tab"]}, "DRY-RUN hotkey keys=ctrl+alt+tab"),
    ({"type": "hotkey", "keys": ["ctrl", "win", "right"]}, "DRY-RUN hotkey keys=ctrl+win+right"),
    ({"type": "hotkey", "keys": ["win", "n"]}, "DRY-RUN hotkey keys=win+n"),
    *[({"type": "key", "name": n}, f"DRY-RUN hotkey keys={n}")
      for n in ("volume_down", "volume_mute", "volume_up", "media_previous", "media_play_pause", "media_next")],
    ({"type": "hotkey", "keys": ["ctrl", "c"]}, "DRY-RUN hotkey keys=ctrl+c"),
    ({"type": "hotkey", "keys": ["ctrl", "v"]}, "DRY-RUN hotkey keys=ctrl+v"),
    ({"type": "hotkey", "keys": ["ctrl", "shift", "Esc"]}, "DRY-RUN hotkey keys=ctrl+shift+esc"),
]

# PC actions are confirmed with action_ok. Dry run only logs: nothing is shut down.
ACTION_MESSAGES = [
    ("task_manager", "DRY-RUN task_manager"),
    ("shutdown", "DRY-RUN shutdown delay=5s"),
    ("cancel_shutdown", "DRY-RUN cancel_shutdown"),
]

BAD_MESSAGES = [
    ("not json", "bad_json"),
    (json.dumps({"type": "move", "dx": 1, "dy": 1}), "bad_version"),
    (json.dumps({"v": 2, "type": "move", "dx": 1, "dy": 1}), "bad_version"),
    (json.dumps({"v": 1, "type": "teleport"}), "unknown_type"),
    (json.dumps({"v": 1, "type": "click", "button": "thumb"}), "bad_field"),
    (json.dumps({"v": 1, "type": "move", "dx": "far", "dy": 0}), "bad_field"),
    (json.dumps({"v": 1, "type": "zoom", "steps": 500}), "bad_field"),
    (json.dumps({"v": 1, "type": "key", "name": "launch_rockets"}), "bad_field"),
    (json.dumps({"v": 1, "type": "type_text", "text": "x" * 5000}), "bad_field"),
    (json.dumps({"v": 1, "type": "paste_text", "text": ""}), "bad_field"),
    (json.dumps({"v": 1, "type": "Shutdown"}), "unknown_type"),
    (json.dumps({"v": 1, "type": "restart"}), "unknown_type"),
    (json.dumps({"v": 1, "type": "ctrl_alt_del"}), "unknown_type"),
    (json.dumps({"type": "shutdown"}), "bad_version"),
]

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, port: int):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        # A known key in a temporary file, so the test never touches the real one.
        self._key_dir = tempfile.TemporaryDirectory()
        self.key = os.urandom(32)
        key_file = Path(self._key_dir.name) / "key"
        key_file.write_bytes(self.key)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "server", "--host", HOST, "--port", str(port), "--dry-run",
             "--key-file", str(key_file)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        self.lines: list[str] = []
        self.pin: str | None = None
        self._ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
            m = re.search(r"PIN:\s+(\d{6})", line)
            if m:
                self.pin = m.group(1)
                self._ready.set()

    def wait_ready(self, port: int) -> None:
        if not self._ready.wait(15):
            raise RuntimeError("server did not print a PIN:\n" + "\n".join(self.lines))
        for _ in range(50):
            try:
                with socket.create_connection((HOST, port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start listening")

    async def wait_for_log(self, needle: str, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.log_count(needle) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def log_count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)
        self._key_dir.cleanup()


class EncryptedClient:
    """The phone's side of an encrypted connection, written from PROTOCOL.md (not server code)."""

    def __init__(self, ws, key: bytes):
        self.ws, self.key = ws, key
        self.box = None
        self.sent = self.received = 0

    @staticmethod
    def nonce(direction: int, counter: int) -> bytes:
        return bytes([direction]) + counter.to_bytes(8, "little") + bytes(15)

    async def handshake(self) -> None:
        client_nonce = os.urandom(16)
        await send(self.ws, {"type": "enc_hello", "nonce": base64.urlsafe_b64encode(client_nonce).decode().rstrip("=")})
        challenge = await recv_json(self.ws)
        server_nonce = base64.urlsafe_b64decode(challenge["nonce"] + "==")
        session_key = hashlib.sha512(b"Mousette session v1" + self.key + server_nonce + client_nonce).digest()[:32]
        self.box = SecretBox(session_key)

    def frame(self, msg: dict) -> bytes:
        text = json.dumps({"v": 1, **msg}, ensure_ascii=False).encode()
        frame = self.box.encrypt(text, self.nonce(0, self.sent)).ciphertext
        self.sent += 1
        return frame

    async def send(self, msg: dict) -> bytes:
        frame = self.frame(msg)
        await self.ws.send(frame)
        return frame

    async def recv(self) -> dict:
        data = await asyncio.wait_for(self.ws.recv(), 3)
        assert isinstance(data, bytes), f"expected an encrypted frame, got {data!r}"
        msg = json.loads(self.box.decrypt(data, self.nonce(1, self.received)))
        self.received += 1
        return msg


async def recv_json(ws, timeout=3.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def expect_closed(ws) -> int | None:
    try:
        await asyncio.wait_for(ws.recv(), 3)
    except websockets.ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else None
    return None


def send(ws, msg: dict):
    return ws.send(json.dumps({"v": 1, **msg}, ensure_ascii=False))


async def run(server: Server, port: int) -> None:
    url = f"ws://{HOST}:{port}/ws"

    print("Unauthenticated PC actions are refused")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "shutdown"})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply.get("code") == "auth_required" and code == 4001,
              f"shutdown before auth is rejected and socket closed (code {code})")
    check(server.log_count("DRY-RUN shutdown") == 0, "unauthenticated shutdown was not executed")

    print("Static client")
    for path, ctype in [("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css"),
                        ("/manifest.webmanifest", "manifest+json"), ("/icons/icon-180.png", "image/png")]:
        with urllib.request.urlopen(f"http://{HOST}:{port}{path}") as r:
            check(r.status == 200 and ctype in r.headers["content-type"]
                  and r.headers.get("cache-control") == "no-cache",
                  f"GET {path} -> {r.status} {r.headers['content-type']}, {r.headers.get('cache-control')}")
    with urllib.request.urlopen(f"http://{HOST}:{port}/") as r:
        csp = r.headers.get("content-security-policy", "")
        check("frame-ancestors 'none'" in csp and "script-src 'self'" in csp
              and r.headers.get("x-content-type-options") == "nosniff",
              "security headers set (CSP, nosniff)")

    print("Other websites and host names are refused")
    try:
        async with websockets.connect(url, origin="http://evil.example") as ws:
            await recv_json(ws)
        refused = False
    except Exception as exc:  # handshake rejected (HTTP 403) or closed with 4031
        refused = "403" in str(exc) or "4031" in str(exc)
    check(refused, "WebSocket from another website's page is refused")
    async with websockets.connect(url, origin=f"http://{HOST}:{port}") as ws:
        await send(ws, {"type": "ping"})
        check((await recv_json(ws)).get("code") == "auth_required", "WebSocket from Mousette's own page is accepted")
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/", headers={"Host": "evil.example"}))
        status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    check(status == 403, f"page requested under another host name is refused ({status})")

    # A link from another website must not log in with a PIN in its "#": the page is sent
    # to /?from=link, where the client ignores it. Camera, typed address and reload log in.
    def page(path: str, fetch_site: str | None, referer: str | None = None) -> tuple[int, str | None]:
        conn = http.client.HTTPConnection(HOST, port, timeout=5)
        headers = {"Sec-Fetch-Site": fetch_site} if fetch_site else {}
        if referer:
            headers["Referer"] = referer
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status, r.getheader("location")
    for path, site, expected in [("/", "cross-site", (303, "/?from=link")),
                                 ("/index.html", "cross-site", (303, "/?from=link")),
                                 ("/", "same-site", (303, "/?from=link")),
                                 ("/?from=link", "cross-site", (200, None)),
                                 ("/", "none", (200, None)),
                                 ("/", "same-origin", (200, None)),
                                 ("/", None, (200, None))]:
        check(page(path, site) == expected, f"GET {path} from {site or 'unknown'} -> {page(path, site)}")
    # The page moving itself from the IP address to the .local name keeps its PIN; a link
    # from any other website still doesn't.
    own = f"http://{HOST}:{port}/"
    check(page("/", "cross-site", own) == (200, None), f"GET / from our own address ({own}) -> keeps the PIN")
    check(page("/", "cross-site", "http://evil.example/") == (303, "/?from=link"), "GET / from evil.example -> /?from=link")
    conn = http.client.HTTPConnection(HOST, port, timeout=5)
    conn.request("GET", "/name")
    r = conn.getresponse()
    check(r.status == 200 and json.loads(r.read()) == {"name": None},
          "GET /name -> no .local name on a loopback test server")
    conn.close()

    print("Wrong PINs are limited across all addresses")
    guard = PinGuard("123456")
    for i in range(10):
        guard.check(f"192.168.0.{i + 10}", "000000")  # 10 addresses, one guess each
    check(guard.locked_for("192.168.0.99") > 0, "an 11th address is locked out too")

    print("Authentication")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "move", "dx": 5, "dy": 5})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply.get("code") == "auth_required" and code == 4001,
              f"input before auth is rejected and socket closed (code {code})")
    check(server.log_count("DRY-RUN move dx=5 dy=5") == 0, "unauthenticated move was not executed")

    wrong = "000000" if server.pin != "000000" else "111111"
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": wrong})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply == {"v": 1, "type": "auth_failed", "reason": "wrong_pin"} and code == 4003,
              f"wrong PIN -> auth_failed, closed (code {code})")

    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": server.pin})
        reply = await recv_json(ws)
        token = reply.pop("token", None)
        check(reply == {"v": 1, "type": "auth_ok", "protocol": 1, "direct": f"{HOST}:{port}"}
              and isinstance(token, str) and len(token) == 32,
              "correct PIN -> auth_ok with a token and the IP address")

    print("Token login (phone reloaded the page after being locked)")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "token": token})
        reply = await recv_json(ws)
        check(reply == {"v": 1, "type": "auth_ok", "protocol": 1, "direct": f"{HOST}:{port}"},
              f"saved token -> {reply}")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "token": "0" * 32})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply == {"v": 1, "type": "auth_failed", "reason": "token_expired"} and code == 4003,
              f"unknown token -> token_expired, closed (code {code})")

    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": server.pin})
        await recv_json(ws)

        print("Ping / pong")
        await send(ws, {"type": "ping", "id": 42})
        reply = await recv_json(ws)
        check(reply == {"v": 1, "type": "pong", "id": 42}, f"ping -> {reply}")

        print("Input messages")
        for msg, _ in INPUT_MESSAGES:
            await send(ws, msg)
        # The server answers in order, so a pong after the batch means every message
        # before it was processed; any error reply would arrive before the pong.
        await send(ws, {"type": "ping", "id": "batch"})
        replies = []
        while True:
            reply = await recv_json(ws)
            replies.append(reply)
            if reply.get("type") == "pong":
                break
        errors = [r for r in replies if r.get("type") == "error"]
        check(not errors, f"{len(INPUT_MESSAGES)} input messages, no error replies {errors or ''}")
        # The pong can arrive before the server's last log lines are read from its output.
        await server.wait_for_log(INPUT_MESSAGES[-1][1])
        for msg, needle in INPUT_MESSAGES:
            if needle:
                check(server.log_count(needle) >= 1, f"{msg['type']:<12} executed: {needle[8:]}")

        print("PC actions")
        for type_, needle in ACTION_MESSAGES:
            await send(ws, {"type": type_})
            reply = await recv_json(ws)
            check(reply == {"v": 1, "type": "action_ok", "ref": type_}, f"{type_} -> {reply}")
            check(server.log_count(needle) == 1, f"{type_:<15} executed: {needle[8:]}")

        print("Paste text from the phone")
        paste = "Line 1 é ü 😀\nLine 2"
        await send(ws, {"type": "paste_text", "text": paste})
        reply = await recv_json(ws)
        check(reply == {"v": 1, "type": "action_ok", "ref": "paste_text"}, f"paste_text -> {reply}")
        await server.wait_for_log("DRY-RUN paste_text")
        check(server.log_count(f"DRY-RUN paste_text text={paste!r}") == 1, "paste_text      executed with the exact text")

        print("Encrypted connection (key from the QR code)")
        async with websockets.connect(url) as enc_ws:
            enc = EncryptedClient(enc_ws, server.key)
            await enc.handshake()
            await enc.send({"type": "auth", "pin": server.pin})
            reply = await enc.recv()
            check(reply.get("type") == "auth_ok" and "token" in reply, "encrypted PIN login -> encrypted auth_ok")
            secret = "my password 🙂"
            paste_frame = await enc.send({"type": "paste_text", "text": secret})
            check((await enc.recv()) == {"v": 1, "type": "action_ok", "ref": "paste_text"}, "encrypted paste_text -> action_ok")
            await server.wait_for_log("DRY-RUN paste_text text='my password")
            check(server.log_count(f"DRY-RUN paste_text text={secret!r}") == 1, "encrypted paste_text executed once")
            check(secret.encode() not in paste_frame and b"paste_text" not in paste_frame, "the frame on the wire doesn't show the text")
            await enc_ws.send(paste_frame)  # someone on the network replays a recorded frame
            code = await expect_closed(enc_ws)
            check(code == 4032 and server.log_count(f"DRY-RUN paste_text text={secret!r}") == 1,
                  f"replayed frame -> closed (code {code}), not executed again")

        async with websockets.connect(url) as enc_ws:
            enc = EncryptedClient(enc_ws, os.urandom(32))
            await enc.handshake()
            await enc.send({"type": "auth", "pin": server.pin})
            code = await expect_closed(enc_ws)
            check(code == 4032, f"wrong key -> closed (code {code}) without logging in")

        async with websockets.connect(url) as enc_ws:
            enc = EncryptedClient(enc_ws, server.key)
            await enc.handshake()
            await send(enc_ws, {"type": "auth", "pin": server.pin})
            code = await expect_closed(enc_ws)
            check(code == 4032, f"unencrypted message after the handshake -> closed (code {code})")

        async with websockets.connect(url) as enc_ws:
            await send(enc_ws, {"type": "enc_hello", "nonce": "short"})
            reply = await recv_json(enc_ws)
            check(reply.get("code") == "bad_field", "enc_hello with a bad nonce -> bad_field")

        print("Invalid messages get an error reply, connection stays open")
        for raw, expected_code in BAD_MESSAGES:
            await ws.send(raw)
            reply = await recv_json(ws)
            check(reply.get("type") == "error" and reply.get("code") == expected_code,
                  f"{raw[:50]!r} -> {reply.get('code')}")
        await send(ws, {"type": "ping", "id": 7})
        check((await recv_json(ws)).get("type") == "pong", "still connected after bad messages")

        print("Held button released on disconnect")
        await send(ws, {"type": "mouse_down", "button": "right"})
        await send(ws, {"type": "ping"})
        await recv_json(ws)
    await asyncio.sleep(0.5)
    check(server.log_count("DRY-RUN mouse_up button=right") == 1, "mouse_up right sent after disconnect")

    print("PIN lockout")
    for _ in range(5):
        async with websockets.connect(url) as ws:
            await send(ws, {"type": "auth", "pin": wrong})
            await recv_json(ws)
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": server.pin})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply.get("reason") == "locked_out" and code == 4029,
              f"correct PIN refused after 5 wrong guesses (code {code})")


def check_qr() -> None:
    print("QR code")
    key = bytes(range(32))
    with_key = connect_url("mousette.local", 80, "123456", key)
    check(with_key == "http://mousette.local/#p=123456&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
          f"connect_url with the key -> {with_key}")
    with tempfile.TemporaryDirectory() as tmp:
        key_file = Path(tmp) / "sub" / "key"
        first = load_key(key_file)
        check(len(first) == 32 and load_key(key_file) == first, "encryption key is made once, then reused")
        check(load_key(key_file, new=True) != first, "--new-key replaces it")
    for args, expected in [(("mousette.local", 80, "123456"), "http://mousette.local/#p=123456"),
                           (("mousette.local", 8765, "123456"), "http://mousette.local:8765/#p=123456"),
                           (("192.168.1.23", 80, "123456"), "http://192.168.1.23/#p=123456")]:
        check(connect_url(*args) == expected, f"connect_url{args} -> {connect_url(*args)}")

    # Decode the drawn QR back into modules: each text line holds two module rows.
    url = "http://192.168.1.23/#p=123456&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    halves = {"█": (True, True), "▀": (True, False), "▄": (False, True), " ": (False, False)}  # True = light
    rows: list[list[bool]] = []
    for line in qr_lines(url):
        top, bottom = zip(*(halves[c] for c in line))
        rows += [list(top), list(bottom)]
    border = 4
    drawn = [[not light for light in row[border:-border]] for row in rows[border:border + len(rows) - 2 * border]]
    expected_matrix = [[bool(m) for m in row] for row in segno.make(url, error="l", boost_error=False).matrix]
    drawn = drawn[:len(expected_matrix)]
    check(drawn == expected_matrix, f"QR drawing matches the code for {url} ({len(expected_matrix)} modules)")


def check_clients() -> None:
    """The Mousette window learns which phones are logged in, and whether they're encrypted."""
    import uvicorn

    print("Connected phones (for the Mousette window)")
    events: list[list[tuple[str, bool]]] = []
    key = os.urandom(32)
    port = free_port()
    app = create_app(DryRunBackend(), "123456", key=key, on_clients=events.append)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    async def phones() -> None:
        url = f"ws://{HOST}:{port}/ws"
        async with websockets.connect(url) as ws:
            await send(ws, {"type": "auth", "pin": "123456"})
            await recv_json(ws)
            async with websockets.connect(url) as enc_ws:
                enc = EncryptedClient(enc_ws, key)
                await enc.handshake()
                await enc.send({"type": "auth", "pin": "123456"})
                await enc.recv()
        for _ in range(40):
            if len(events) >= 4:
                break
            await asyncio.sleep(0.05)

    asyncio.run(phones())
    server.should_exit = True
    thread.join(5)
    check(events == [[(HOST, False)], [(HOST, False), (HOST, True)], [(HOST, False)], []],
          f"phones logged in, then out: {events}")


def main() -> int:
    check_qr()
    check_clients()
    port = free_port()
    server = Server(port)
    try:
        server.wait_ready(port)
        print(f"Server up on {HOST}:{port} (dry run), PIN {server.pin}\n")
        asyncio.run(run(server, port))
    finally:
        server.stop()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        print("\n--- server log ---\n" + "\n".join(server.lines))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
