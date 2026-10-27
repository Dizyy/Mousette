"""Entry point of the single-file CouchPad.exe (made by build.py).

Does what start.bat does for the source version: offer to run as administrator and
install updates, then start the server. The source version runs `python -m server`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from server.__main__ import main
from server.input_backend import is_elevated
from server.update import update_exe

ADMIN_QUESTION_S = 10


def offer_admin(argv: list[str]) -> None:
    """Windows drops input sent by a normal program into admin windows (Task Manager,
    installers), so ask once whether to restart with admin rights. No answer means no."""
    if sys.platform != "win32" or is_elevated() or os.environ.get("COUCHPAD_UPDATED"):
        return
    import ctypes
    import msvcrt

    print("Run as administrator so CouchPad can control Task Manager and other admin windows? "
          f"[y/N] ({ADMIN_QUESTION_S} s) ", end="", flush=True)
    answer = ""
    deadline = time.monotonic() + ADMIN_QUESTION_S
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            answer = msvcrt.getwch()
            break
        time.sleep(0.05)
    print(answer or "n")
    if answer.lower() != "y":
        return
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                 subprocess.list2cmdline(argv), None, 1)
    if result > 32:  # started; this copy is done
        sys.exit(0)
    print("Could not start as administrator; continuing without admin rights.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if getattr(sys, "frozen", False):
        offer_admin(args)
        update_exe(args)
    sys.exit(main())
