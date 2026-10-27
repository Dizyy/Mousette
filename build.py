"""Builds the single-file download: dist/CouchPad.exe on Windows (dist/CouchPad elsewhere).

Run with the project's Python:  .venv\\Scripts\\python.exe build.py
Attach the result to the GitHub release; the .exe updater downloads it from there.
PyInstaller is only needed for building, so it is not in requirements.txt.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def png_to_ico(png: Path, ico: Path) -> None:
    """Windows icons may hold a PNG as-is, so no image library is needed."""
    data = png.read_bytes()
    width, height = struct.unpack(">II", data[16:24])
    entry = struct.pack("<BBBBHHII", width % 256, height % 256, 0, 0, 1, 32, len(data), 6 + 16)
    ico.write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + data)


def main() -> int:
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                    "-r", str(ROOT / "requirements.txt"), "pyinstaller"], check=True)
    BUILD.mkdir(exist_ok=True)
    icon = BUILD / "couchpad.ico"
    png_to_ico(ROOT / "client" / "icons" / "icon-192.png", icon)
    sep = ";" if sys.platform == "win32" else ":"
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "CouchPad", "--icon", str(icon),
        "--add-data", f"{ROOT / 'client'}{sep}client",
        # Both pick their backends at runtime, which PyInstaller can't see on its own.
        "--collect-submodules", "uvicorn", "--collect-submodules", "pynput",
        # PyNaCl loads libsodium through cffi's compiled backend, also unseen.
        "--hidden-import", "_cffi_backend", "--collect-binaries", "nacl",
        "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD), "--specpath", str(BUILD),
        str(ROOT / "couchpad.py"),
    ], check=True)
    print(f"Built {ROOT / 'dist' / ('CouchPad.exe' if sys.platform == 'win32' else 'CouchPad')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
