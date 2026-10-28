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


def version_file(path: Path) -> None:
    """Windows file properties (Details tab): product name and version. Code signing needs them,
    and virus scanners trust an .exe with them a little more."""
    sys.path.insert(0, str(ROOT))
    from server import __version__

    nums = tuple(int(n) for n in __version__.split(".")) + (0,) * (4 - len(__version__.split(".")))
    strings = {
        "CompanyName": "Mousette",
        "FileDescription": "Mousette",
        "FileVersion": __version__,
        "InternalName": "Mousette",
        "LegalCopyright": "Mousette contributors, AGPL-3.0",
        "OriginalFilename": "Mousette.exe",
        "ProductName": "Mousette",
        "ProductVersion": __version__,
    }
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [{', '.join(f'StringStruct({k!r}, {v!r})' for k, v in strings.items())}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ]
)
""", encoding="utf-8")


def main() -> int:
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                    "-r", str(ROOT / "requirements.txt"), "pyinstaller",
                    # The Windows .exe lives in the tray (server/tray.py).
                    "pillow", *(["pystray"] if sys.platform == "win32" else [])], check=True)
    BUILD.mkdir(exist_ok=True)
    icon = BUILD / "mousette.ico"
    png_to_ico(ROOT / "client" / "icons" / "icon-512.png", icon)
    version = BUILD / "version.txt"
    version_file(version)
    sep = ";" if sys.platform == "win32" else ":"
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "Mousette", "--icon", str(icon),
        # Windows: no console window; pystray picks its backend at runtime.
        *(["--windowed", "--collect-submodules", "pystray", "--version-file", str(version)]
          if sys.platform == "win32" else []),
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
