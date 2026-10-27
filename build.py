"""Builds the single-file download: dist/Mousette.exe on Windows (dist/Mousette elsewhere).

Run with the project's Python:  .venv\\Scripts\\python.exe build.py
Attach the result to the GitHub release; the .exe updater downloads it from there.
PyInstaller, Pillow (the .exe icon) and pystray (the Windows tray) are only needed for building,
so they are not in requirements.txt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def png_to_ico(png: Path, ico: Path) -> None:
    """Every size Windows asks for (taskbar, Explorer, title bar), so it never scales a big one down."""
    from PIL import Image

    Image.open(png).save(ico, sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


def main() -> int:
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                    "-r", str(ROOT / "requirements.txt"), "pyinstaller",
                    # The Windows .exe lives in the tray (server/tray.py).
                    "pillow", *(["pystray"] if sys.platform == "win32" else [])], check=True)
    BUILD.mkdir(exist_ok=True)
    icon = BUILD / "mousette.ico"
    png_to_ico(ROOT / "client" / "icons" / "icon-512.png", icon)
    sep = ";" if sys.platform == "win32" else ":"
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "Mousette", "--icon", str(icon),
        # Windows: no console window; pystray picks its backend at runtime.
        *(["--windowed", "--collect-submodules", "pystray"] if sys.platform == "win32" else []),
        "--add-data", f"{ROOT / 'client'}{sep}client",
        # Both pick their backends at runtime, which PyInstaller can't see on its own.
        "--collect-submodules", "uvicorn", "--collect-submodules", "pynput",
        # PyNaCl loads libsodium through cffi's compiled backend, also unseen.
        "--hidden-import", "_cffi_backend", "--collect-binaries", "nacl",
        "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD), "--specpath", str(BUILD),
        str(ROOT / "mousette.py"),
    ], check=True)
    print(f"Built {ROOT / 'dist' / ('Mousette.exe' if sys.platform == 'win32' else 'Mousette')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
