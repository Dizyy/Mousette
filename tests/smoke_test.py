"""End-to-end protocol test.

Starts the server in --dry-run mode on 127.0.0.1 (so your real mouse and
keyboard are never touched), then sends every protocol message type over a
WebSocket and checks the server accepted and executed each one.

Run:  .venv\\Scripts\\python.exe tests\\smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.app import PinGuard  # noqa: E402
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
    ({"type": "type_text", "text": "Héllo! café naïve ñ ÉÜ 😀👍"}, "DRY-RUN type_text text='Héllo! café naïve ñ ÉÜ 😀👍'"),
    *[({"type": "key", "name": n}, f"DRY-RUN hotkey keys={n}")
      for n in ("enter", "backspace", "tab", "esc", "up", "down", "left", "right", "delete", "home", "end")],
    ({"type": "hotkey", "keys": ["win", "s"]}, "DRY-RUN hotkey keys=win+s"),
    ({"type": "hotkey", "keys": ["alt", "tab"]}, "DRY-RUN hotkey keys=alt+tab"),
    ({"type": "hotkey", "keys": ["win", "d"]}, "DRY-RUN hotkey keys=win+d"),
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
    (json.dumps({"v": 1, "type": "key", "name": "launch_rockets"}), "bad_field"),
    (json.dumps({"v": 1, "type": "type_text", "text": "x" * 5000}), "bad_field"),
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
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "server", "--host", HOST, "--port", str(port), "--dry-run"],
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

    def log_count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)


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
        check((await recv_json(ws)).get("code") == "auth_required", "WebSocket from PhoneMouse's own page is accepted")
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/", headers={"Host": "evil.example"}))
        status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    check(status == 403, f"page requested under another host name is refused ({status})")

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
        check(reply == {"v": 1, "type": "auth_ok", "protocol": 1}, f"correct PIN -> {reply}")

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
        for msg, needle in INPUT_MESSAGES:
            if needle:
                check(server.log_count(needle) >= 1, f"{msg['type']:<12} executed: {needle[8:]}")

        print("PC actions")
        for type_, needle in ACTION_MESSAGES:
            await send(ws, {"type": type_})
            reply = await recv_json(ws)
            check(reply == {"v": 1, "type": "action_ok", "ref": type_}, f"{type_} -> {reply}")
            check(server.log_count(needle) == 1, f"{type_:<15} executed: {needle[8:]}")

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


def main() -> int:
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
