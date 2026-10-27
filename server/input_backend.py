"""Turns validated protocol messages into OS input events.

`PynputBackend` drives the real mouse and keyboard. `DryRunBackend` only logs,
and is used by the automated test so it never touches your desktop.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("mousette.input")

SHUTDOWN_DELAY_S = 5
ERROR_NO_SHUTDOWN_IN_PROGRESS = 1116

# The phone sends Windows shortcuts; on macOS the same jobs have other keys
# ("win" is the Cmd key there).
MAC_HOTKEYS = {
    ("ctrl", "c"): ["win", "c"],
    ("ctrl", "v"): ["win", "v"],
    ("win", "s"): ["win", "space"],   # Spotlight
    ("alt", "tab"): ["win", "tab"],
    ("win", "d"): ["f11"],            # Show Desktop
}
# Tried in order on Linux; the first one installed opens.
LINUX_TASK_MANAGERS = ("gnome-system-monitor", "plasma-systemmonitor", "ksysguard",
                       "xfce4-taskmanager", "mate-system-monitor", "lxtask")


class InputBackend:
    """Dispatches (type, fields) from protocol.parse() to handler methods."""

    def __init__(self) -> None:
        self._held_buttons: set[str] = set()
        # Sub-pixel / sub-notch remainders so slow movements are not lost to rounding.
        self._move_rest = [0.0, 0.0]
        self._lock = threading.Lock()

    def execute(self, type_: str, fields: dict) -> None:
        with self._lock:
            getattr(self, f"_do_{type_}")(**fields)

    def release_all(self) -> None:
        """Release any buttons a disconnected client left pressed."""
        with self._lock:
            for button in list(self._held_buttons):
                self._do_mouse_up(button)

    def _do_move(self, dx: float, dy: float) -> None:
        self._move_rest[0] += dx
        self._move_rest[1] += dy
        ix, iy = int(self._move_rest[0]), int(self._move_rest[1])
        self._move_rest[0] -= ix
        self._move_rest[1] -= iy
        if ix or iy:
            self.move(ix, iy)

    def _do_click(self, button: str) -> None:
        self.click(button, 1)

    def _do_double_click(self, button: str) -> None:
        self.click(button, 2)

    def _do_mouse_down(self, button: str) -> None:
        self._held_buttons.add(button)
        self.press(button)

    def _do_mouse_up(self, button: str) -> None:
        self._held_buttons.discard(button)
        self.release(button)

    def _do_scroll(self, dx: float, dy: float) -> None:
        self.scroll(dx, dy)

    def _do_type_text(self, text: str) -> None:
        if text:
            self.type_text(text)

    def _do_paste_text(self, text: str) -> None:
        self.paste_text(text)

    def _do_key(self, name: str) -> None:
        self.hotkey([name])

    def _do_hotkey(self, keys: list[str]) -> None:
        self.hotkey(keys)

    def _do_task_manager(self) -> None:
        self.task_manager()

    def _do_shutdown(self) -> None:
        self.shutdown()

    def _do_cancel_shutdown(self) -> None:
        self.cancel_shutdown()

    # Implemented by subclasses.
    def move(self, dx: int, dy: int) -> None: raise NotImplementedError
    def click(self, button: str, count: int) -> None: raise NotImplementedError
    def press(self, button: str) -> None: raise NotImplementedError
    def release(self, button: str) -> None: raise NotImplementedError
    def scroll(self, dx: float, dy: float) -> None: raise NotImplementedError
    def type_text(self, text: str) -> None: raise NotImplementedError
    def paste_text(self, text: str) -> None: raise NotImplementedError
    def hotkey(self, keys: list[str]) -> None: raise NotImplementedError
    def task_manager(self) -> None: raise NotImplementedError
    def shutdown(self) -> None: raise NotImplementedError
    def cancel_shutdown(self) -> None: raise NotImplementedError


def is_elevated() -> bool:
    """True when running as administrator (always False off Windows)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class PynputBackend(InputBackend):
    def __init__(self) -> None:
        super().__init__()
        from pynput import keyboard, mouse

        self._shutdown_timer: threading.Timer | None = None
        self._mouse = mouse.Controller()
        self._keyboard = keyboard.Controller()
        self._buttons = {
            "left": mouse.Button.left,
            "right": mouse.Button.right,
            "middle": mouse.Button.middle,
        }
        k = keyboard.Key
        self._keys = {
            "enter": k.enter, "backspace": k.backspace, "tab": k.tab, "esc": k.esc,
            "delete": k.delete, "home": k.home, "end": k.end,
            "up": k.up, "down": k.down, "left": k.left, "right": k.right,
            "space": k.space, "page_up": k.page_up, "page_down": k.page_down,
            "ctrl": k.ctrl, "alt": k.alt, "shift": k.shift, "win": k.cmd,
            "volume_down": k.media_volume_down, "volume_mute": k.media_volume_mute,
            "volume_up": k.media_volume_up, "media_previous": k.media_previous,
            "media_play_pause": k.media_play_pause, "media_next": k.media_next,
            **{f"f{i}": getattr(k, f"f{i}") for i in range(1, 13)},
        }

    def move(self, dx: int, dy: int) -> None:
        self._mouse.move(dx, dy)

    def click(self, button: str, count: int) -> None:
        self._mouse.click(self._buttons[button], count)

    def press(self, button: str) -> None:
        self._mouse.press(self._buttons[button])

    def release(self, button: str) -> None:
        self._mouse.release(self._buttons[button])

    def scroll(self, dx: float, dy: float) -> None:
        # pynput multiplies by WHEEL_DELTA (120), so fractions give smooth scrolling.
        self._mouse.scroll(dx, dy)

    def type_text(self, text: str) -> None:
        # keyboard.type sends characters outside the active layout (accented letters, emoji)
        # as Unicode input events, splitting emoji into UTF-16 surrogate pairs.
        self._keyboard.type(text)

    def paste_text(self, text: str) -> None:
        # Clipboard + paste shortcut: instant for long text, and keeps line breaks and
        # formatting characters that typing would turn into Enter/Tab presses.
        from . import clipboard
        clipboard.set_text(text)
        time.sleep(0.05)  # let the target app see the new clipboard first
        self.hotkey(["ctrl", "v"])  # Cmd+V on macOS, see MAC_HOTKEYS

    def hotkey(self, keys: list[str]) -> None:
        if sys.platform == "darwin":
            keys = MAC_HOTKEYS.get(tuple(keys), keys)
        resolved = [self._keys.get(name, name) for name in keys]
        pressed = []
        try:
            for key in resolved:
                self._keyboard.press(key)
                pressed.append(key)
        finally:
            for key in reversed(pressed):
                self._keyboard.release(key)

    def task_manager(self) -> None:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Activity Monitor"])
            return
        if sys.platform != "win32":
            for app in LINUX_TASK_MANAGERS:
                if shutil.which(app):
                    subprocess.Popen([app])
                    return
            raise RuntimeError("No system monitor app found on this PC.")
        # Started by an admin Mousette, Task Manager inherits admin rights, so Windows
        # shows no UAC prompt (which the phone could not answer: it runs on the secure desktop).
        if not is_elevated():
            raise RuntimeError("Start Mousette as administrator first (answer Y when it starts). "
                               "Otherwise Windows asks for your PIN on the PC.")
        subprocess.Popen(["taskmgr.exe"])

    def shutdown(self) -> None:
        if sys.platform != "win32":
            # macOS and Linux have no seconds-based delay that works without root,
            # so the delay (and its cancel) happens here.
            if self._shutdown_timer is not None:
                self._shutdown_timer.cancel()
            self._shutdown_timer = threading.Timer(SHUTDOWN_DELAY_S, self._power_off)
            self._shutdown_timer.daemon = True
            self._shutdown_timer.start()
            return
        result = _run(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY_S)])
        if result.returncode != 0:
            raise RuntimeError(f"Shutdown failed: {(result.stderr or result.stdout).strip()}")

    def _power_off(self) -> None:
        self._shutdown_timer = None
        if sys.platform == "darwin":
            cmd = ["osascript", "-e", 'tell application "System Events" to shut down']
        else:
            cmd = ["systemctl", "poweroff"]
        result = _run(cmd)
        if result.returncode != 0:
            log.error("Shutdown failed: %s", (result.stderr or result.stdout).strip())

    def cancel_shutdown(self) -> None:
        if sys.platform != "win32":
            if self._shutdown_timer is None:
                raise RuntimeError("No shutdown is in progress.")
            self._shutdown_timer.cancel()
            self._shutdown_timer = None
            return
        result = _run(["shutdown", "/a"])
        if result.returncode == ERROR_NO_SHUTDOWN_IN_PROGRESS:
            raise RuntimeError("No shutdown is in progress.")
        if result.returncode != 0:
            raise RuntimeError(f"Cancel failed: {(result.stderr or result.stdout).strip()}")


class DryRunBackend(InputBackend):
    def move(self, dx, dy): log.info("DRY-RUN move dx=%d dy=%d", dx, dy)
    def click(self, button, count): log.info("DRY-RUN click button=%s count=%d", button, count)
    def press(self, button): log.info("DRY-RUN mouse_down button=%s", button)
    def release(self, button): log.info("DRY-RUN mouse_up button=%s", button)
    def scroll(self, dx, dy): log.info("DRY-RUN scroll dx=%.3f dy=%.3f", dx, dy)
    def type_text(self, text): log.info("DRY-RUN type_text text=%r", text)
    def paste_text(self, text): log.info("DRY-RUN paste_text text=%r", text)
    def hotkey(self, keys): log.info("DRY-RUN hotkey keys=%s", "+".join(keys))
    def task_manager(self): log.info("DRY-RUN task_manager")
    def shutdown(self): log.info("DRY-RUN shutdown delay=%ds", SHUTDOWN_DELAY_S)
    def cancel_shutdown(self): log.info("DRY-RUN cancel_shutdown")
