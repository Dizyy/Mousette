"""Mousette.exe without a console: a tray icon and a window with the QR code, PIN and status.

Windows only. The source version (start.bat, `python -m server`) keeps its console.
Try it from source with  `python -m server.tray --dry-run --no-update`.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox

import segno

from . import __version__, encryption
from .__main__ import StartupError, make_server, parse_args, prepare, setup_logging, url
from .app import CLIENT_DIR
from .input_backend import is_elevated

log = logging.getLogger("mousette.tray")

LOG_FILE = encryption.default_key_file().with_name("mousette.log")
LOG_MAX_BYTES = 1_000_000

# Comfort theme, as on the phone.
BG, SURFACE, BORDER = "#1a1916", "#24221e", "#36332d"
TEXT, MUTED, ACCENT, BAD = "#ece6da", "#a39a8a", "#9cc5a1", "#e3796a"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.restype = wintypes.HANDLE
ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF
MUTEX_NAME = "Local\\Mousette"
SHOW_EVENT_NAME = "Local\\MousetteShow"


def log_to_file() -> None:
    """A windowed .exe has no console: print() and logging go to the log file instead."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.unlink()
    except OSError:
        pass
    stream = open(LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream


def take_instance_lock(wait: bool) -> bool:
    """True when this is the only Mousette. Otherwise asks the running one to show its window.
    `wait`: this copy replaces one that is closing (update, restart as administrator)."""
    deadline = time.monotonic() + (15 if wait else 0)
    while True:
        handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return True  # kept open until the process ends
        _kernel32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    event = _kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    _kernel32.SetEvent(event)
    _kernel32.CloseHandle(event)
    return False


def restart_as_admin(argv: list[str]) -> bool:
    """Starts an elevated copy that takes over once this one quits. False if refused."""
    args = [a for a in argv if a != "--replace"] + ["--replace"]
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, args
    else:
        exe, params = sys.executable, ["-m", "server.tray", *args]
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(params),
                                                 os.getcwd(), 1)
    return result > 32


def status_text(clients: list[tuple[str, bool]]) -> tuple[str, str]:
    """(text, color) for the phones logged in right now."""
    if not clients:
        return "No phone connected", MUTED
    who = "Phone connected" if len(clients) == 1 else f"{len(clients)} phones connected"
    if all(encrypted for _, encrypted in clients):
        return f"{who} · encrypted", ACCENT
    return f"{who} · not encrypted: scan the QR code to encrypt", BAD


class TrayApp:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.args = parse_args(argv)
        self.events: queue.Queue = queue.Queue()  # work for the Tk thread, from other threads
        self.root = tk.Tk()
        self.root.withdraw()
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.icon = None
        self.quitting = False
        self.told_about_tray = False

    # ---------- start and stop ----------

    def run(self) -> int:
        try:
            self.setup = prepare(self.args, on_clients=lambda c: self.events.put(("clients", c)))
        except StartupError as exc:
            messagebox.showerror("Mousette", str(exc))
            return 1
        self.server = make_server(self.setup, log_config=None)
        self.server_thread = threading.Thread(target=self.server.run, name="server", daemon=True)
        self.server_thread.start()
        self.build_window()
        self.start_tray()
        threading.Thread(target=self.wait_for_show_requests, daemon=True).start()
        self.root.after(100, self.poll)
        self.show()
        self.root.mainloop()
        return 0

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        if self.icon:
            self.icon.stop()
        if self.server:
            self.server.should_exit = True
        if self.server_thread:
            self.server_thread.join(5)
        if self.setup.mdns:
            self.setup.mdns.stop()
        self.root.destroy()

    def restart_admin(self) -> None:
        if restart_as_admin(self.argv):
            self.quit()

    def poll(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "clients":
                self.show_clients(value)
            elif kind == "show":
                self.show()
            elif kind == "admin":
                self.restart_admin()
            elif kind == "log":
                sys.stdout.flush()
                os.startfile(LOG_FILE)  # opens in the default text editor
            elif kind == "quit":
                self.quit()
                return
        if self.server_thread and not self.server_thread.is_alive() and not self.quitting:
            messagebox.showerror("Mousette", "Mousette stopped unexpectedly. Details are in\n"
                                             f"{LOG_FILE}")
            self.quit()
            return
        self.root.after(100, self.poll)

    def wait_for_show_requests(self) -> None:
        """Starting Mousette.exe again shows this window instead (see take_instance_lock)."""
        event = _kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
        while _kernel32.WaitForSingleObject(event, INFINITE) == 0:
            self.events.put(("show", None))

    # ---------- tray icon ----------

    def start_tray(self) -> None:
        import pystray
        from PIL import Image

        def later(kind):
            return lambda icon, item: self.events.put((kind, None))

        menu = pystray.Menu(
            pystray.MenuItem("Show Mousette", later("show"), default=True),
            pystray.MenuItem("Restart as administrator", later("admin"), visible=not is_elevated()),
            pystray.MenuItem("Open log", later("log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Mousette", later("quit")),
        )
        image = Image.open(CLIENT_DIR / "icons" / "icon-192.png")
        self.icon = pystray.Icon("Mousette", image, "Mousette · No phone connected", menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    # ---------- window ----------

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()
        if not self.told_about_tray and self.icon:
            self.told_about_tray = True
            try:
                self.icon.notify("Mousette keeps running here. Click the icon to show it again.",
                                 "Mousette")
            except Exception:  # notifications may be turned off
                log.info("Could not show the tray notification", exc_info=True)

    def build_window(self) -> None:
        s = self.setup
        root = self.root
        root.title("Mousette")
        root.configure(bg=BG, padx=28, pady=22)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.iconphoto(True, tk.PhotoImage(file=str(CLIENT_DIR / "icons" / "icon-192.png")))
        try:  # dark title bar (Windows 10 20H1 and later)
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except (AttributeError, OSError):
            pass
        scale = max(1.0, root.winfo_fpixels("1i") / 96)

        def label(parent, text, size=11, color=TEXT, bold=False, **kw):
            font = ("Segoe UI", size, "bold" if bold else "normal")
            return tk.Label(parent, text=text, font=font, fg=color, bg=parent["bg"], **kw)

        title = f"Mousette {__version__}" + ("  (dry run: no real input)" if s.args.dry_run else "")
        label(root, title, 16, bold=True).pack(anchor="w")
        self.status = label(root, "", 11)
        self.status.pack(anchor="w", pady=(2, 14))
        self.show_clients([])

        card = tk.Frame(root, bg="#ffffff", padx=6, pady=6)
        card.pack()
        buffer = io.BytesIO()
        segno.make(s.qr_url, error="l", boost_error=False).save(
            buffer, kind="png", scale=round(6 * scale), border=3, dark="#000000", light="#ffffff")
        self.qr_image = tk.PhotoImage(data=base64.b64encode(buffer.getvalue()))
        tk.Label(card, image=self.qr_image, bg="#ffffff", bd=0).pack()
        label(root, "Scan with the phone camera (encrypted connection)", 10, MUTED).pack(pady=(8, 12))

        pin_row = tk.Frame(root, bg=BG)
        pin_row.pack(pady=(0, 14))
        label(pin_row, "PIN", 11, MUTED).pack(side="left", padx=(0, 12))
        label(pin_row, f"{s.pin[:3]} {s.pin[3:]}", 26, bold=True).pack(side="left")

        rows = [("Open on your phone", url(s.host, s.port))]
        if s.hostname:
            rows.append(("iPhone/iPad also", url(s.hostname, s.port)))
        if len(s.candidates) > 1:
            rows.append(("Other addresses", ", ".join(s.candidates[1:])))
        table = tk.Frame(root, bg=BG)
        table.pack(fill="x")
        for row, (name, value) in enumerate(rows):
            label(table, name, 10, MUTED).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=1)
            label(table, value, 10).grid(row=row, column=1, sticky="w", pady=1)

        if not is_elevated():
            warn = tk.Frame(root, bg=SURFACE, padx=12, pady=10, highlightthickness=1,
                            highlightbackground=BORDER)
            warn.pack(fill="x", pady=(14, 0))
            label(warn, "Not running as administrator: Windows ignores the phone while Task "
                        "Manager or another admin window is in front.", 10, MUTED,
                  wraplength=round(330 * scale), justify="left").pack(anchor="w")
            self.button(warn, "Restart as administrator", self.restart_admin).pack(anchor="w", pady=(8, 0))

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", pady=(16, 0))
        label(foot, "Closing this window keeps Mousette\nrunning in the tray, next to the clock.",
              9, MUTED, justify="left").pack(side="left")
        self.button(foot, "Quit Mousette", self.quit).pack(side="right")

    def button(self, parent, text, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 10), fg=TEXT,
                         bg=SURFACE, activebackground=BORDER, activeforeground=TEXT, relief="flat",
                         bd=0, padx=12, pady=5, cursor="hand2", highlightthickness=1,
                         highlightbackground=BORDER)

    def show_clients(self, clients: list[tuple[str, bool]]) -> None:
        text, color = status_text(clients)
        self.status.configure(text=f"●  {text}", fg=color)
        if self.icon:
            self.icon.title = f"Mousette · {text}"[:127]  # Windows caps tooltips at 128 characters


def run(argv: list[str]) -> int:
    log_to_file()
    setup_logging()
    args = parse_args(argv)
    if not take_instance_lock(wait=args.replace or bool(os.environ.get("MOUSETTE_UPDATED"))):
        return 0
    if getattr(sys, "frozen", False):
        from .update import update_exe
        update_exe(argv)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
    except (AttributeError, OSError):
        pass
    log.info("Mousette %s started (tray)", __version__)
    try:
        return TrayApp(argv).run()
    except Exception:
        log.exception("Mousette crashed")
        messagebox.showerror("Mousette", f"Mousette stopped because of an error. Details are in\n{LOG_FILE}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
