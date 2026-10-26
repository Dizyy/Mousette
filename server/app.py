"""FastAPI app: WebSocket endpoint at /ws and the static web client at /."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import protocol
from .input_backend import InputBackend

log = logging.getLogger("phonemouse")

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"

AUTH_TIMEOUT_S = 10
MAX_FAILED_PINS = 5
# Also counted across all addresses, so changing IP address doesn't allow more guesses.
MAX_FAILED_PINS_TOTAL = 10
LOCKOUT_S = 60

# WebSocket close codes (4000-4999 are reserved for applications).
CLOSE_AUTH_REQUIRED = 4001
CLOSE_AUTH_FAILED = 4003
CLOSE_LOCKED_OUT = 4029
CLOSE_NOT_LAN = 4030
CLOSE_BAD_ORIGIN = 4031

# Windows' registry sometimes maps .js to text/plain, which browsers refuse to run.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def is_lan_address(host: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local


class PinGuard:
    """Checks the PIN and locks out guessing: per address, and for everyone after
    too many wrong PINs in total (so switching IP addresses doesn't help)."""

    def __init__(self, pin: str):
        self.pin = pin
        self._failures: dict[str, tuple[int, float]] = {}
        self._recent: list[float] = []  # times of wrong PINs from any address

    def locked_for(self, host: str) -> float:
        now = time.monotonic()
        self._recent = [t for t in self._recent if t > now - LOCKOUT_S]
        remaining = 0.0
        if len(self._recent) >= MAX_FAILED_PINS_TOTAL:
            remaining = self._recent[0] + LOCKOUT_S - now
        count, since = self._failures.get(host, (0, 0.0))
        if count >= MAX_FAILED_PINS:
            if since + LOCKOUT_S - now <= 0:
                self._failures.pop(host, None)
            else:
                remaining = max(remaining, since + LOCKOUT_S - now)
        return remaining

    def check(self, host: str, pin: str) -> bool:
        if hmac.compare_digest(pin.encode(), self.pin.encode()):
            self._failures.pop(host, None)
            return True
        count, _ = self._failures.get(host, (0, 0.0))
        self._failures[host] = (count + 1, time.monotonic())
        self._recent.append(time.monotonic())
        return False


def create_app(backend: InputBackend, pin: str, address: str | None = None) -> FastAPI:
    """`address` is the "ip:port" the phone opens. When given, requests naming any other
    host are refused, which stops DNS-rebinding tricks from websites."""
    app = FastAPI(title="PhoneMouse", docs_url=None, redoc_url=None, openapi_url=None)
    guard = PinGuard(pin)
    # One worker keeps input events strictly ordered and off the event loop.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="input")

    def wrong_host(host_header: str | None) -> bool:
        # Browsers leave out ":80", so accept that form too.
        return address is not None and host_header not in (address, address.removesuffix(":80"))

    @app.middleware("http")
    async def lan_only(request: Request, call_next):
        if not is_lan_address(request.client.host if request.client else None):
            return PlainTextResponse("PhoneMouse only accepts local network clients.", status_code=403)
        host_header = request.headers.get("host")
        if wrong_host(host_header):
            return PlainTextResponse(f"Open PhoneMouse at http://{address}", status_code=403)
        response = await call_next(request)
        # Make Safari check for a newer client on every load (a cheap 304 when nothing changed),
        # otherwise it keeps showing an old page after an update.
        response.headers["Cache-Control"] = "no-cache"
        # The page may only load its own files and talk to this server, and can't be framed.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            f"connect-src 'self' ws://{host_header}; manifest-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def send(ws: WebSocket, msg: dict) -> None:
        await ws.send_json(msg)

    async def authenticate(ws: WebSocket, host: str) -> bool:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            await send(ws, protocol.error("auth_required", "send an auth message first"))
            await ws.close(CLOSE_AUTH_REQUIRED)
            return False

        try:
            type_, fields = protocol.parse(raw)
        except protocol.ProtocolError as exc:
            await send(ws, protocol.error(exc.code, exc.message))
            await ws.close(CLOSE_AUTH_REQUIRED)
            return False

        if type_ != "auth":
            await send(ws, protocol.error("auth_required", f"'{type_}' rejected: not authenticated"))
            await ws.close(CLOSE_AUTH_REQUIRED)
            return False

        locked = guard.locked_for(host)
        if locked:
            await send(ws, protocol.make("auth_failed", reason="locked_out", retry_after=round(locked)))
            await ws.close(CLOSE_LOCKED_OUT)
            return False

        if not guard.check(host, fields["pin"]):
            log.warning("Wrong PIN from %s", host)
            await send(ws, protocol.make("auth_failed", reason="wrong_pin"))
            await ws.close(CLOSE_AUTH_FAILED)
            return False

        await send(ws, protocol.make("auth_ok", protocol=protocol.PROTOCOL_VERSION))
        return True

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        host = ws.client.host if ws.client else None
        if not is_lan_address(host):
            await ws.close(CLOSE_NOT_LAN)
            return
        # Browsers always send Origin. Only PhoneMouse's own page may connect, so other
        # websites open in a browser on this network can't try PINs in the background.
        host_header = ws.headers.get("host")
        origin = ws.headers.get("origin")
        if wrong_host(host_header) or (origin is not None and origin != f"http://{host_header}"):
            log.warning("Refused connection from %s (origin %s, host %s)", host, origin, host_header)
            await ws.close(CLOSE_BAD_ORIGIN)
            return
        await ws.accept()
        try:
            if not await authenticate(ws, host):
                return
            log.info("Client connected: %s", host)
            loop = asyncio.get_running_loop()
            while True:
                raw = await ws.receive_text()
                try:
                    type_, fields = protocol.parse(raw)
                except protocol.ProtocolError as exc:
                    await send(ws, protocol.error(exc.code, exc.message))
                    continue
                if type_ == "ping":
                    await send(ws, protocol.make("pong", id=fields["id"]))
                elif type_ == "auth":
                    await send(ws, protocol.make("auth_ok", protocol=protocol.PROTOCOL_VERSION))
                else:
                    try:
                        await loop.run_in_executor(executor, backend.execute, type_, fields)
                    except Exception as exc:  # keep the connection alive on injection errors
                        log.exception("Input injection failed for %s", type_)
                        await send(ws, protocol.error("input_failed", str(exc), ref=type_))
                    else:
                        if type_ in protocol.ACTION_TYPES:
                            log.info("PC action from %s: %s", host, type_)
                            await send(ws, protocol.make("action_ok", ref=type_))
        except WebSocketDisconnect:
            pass
        finally:
            log.info("Client disconnected: %s", host)
            await asyncio.get_running_loop().run_in_executor(executor, backend.release_all)

    app.mount("/", StaticFiles(directory=CLIENT_DIR, html=True), name="client")
    return app
