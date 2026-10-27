"""Entry point of the single-file Mousette.exe (made by build.py).

On Windows it runs without a console: a tray icon and a window with the QR code, PIN and
status (server/tray.py), which also installs updates. Elsewhere it installs updates, then
runs the console version. The source version runs `python -m server`.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    args = sys.argv[1:]
    if sys.platform == "win32":
        from server.tray import run
        sys.exit(run(args))
    from server.__main__ import main
    from server.update import update_exe
    if getattr(sys, "frozen", False):
        update_exe(args)
    sys.exit(main())
