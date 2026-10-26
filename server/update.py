"""Automatic updater: `python -m server.update`, run by start.bat before the server starts.

Asks GitHub for the latest PhoneMouse release. If it is newer than this copy, downloads
it and replaces PhoneMouse's files in place (.venv and other local files are kept).
Uses only the standard library, so it works before the requirements are installed.

Exit codes: 0 = nothing to do (or the check failed; PhoneMouse then starts as it is),
3 = updated, so start.bat restarts itself with the new files.
"""

from __future__ import annotations

import io
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

REPO = "Dizyy/PhoneMouse"
# Redirects to ".../releases/tag/<newest tag>". Unlike api.github.com, which allows only
# 60 requests an hour per internet connection (and answers 403 after that), it has no such limit.
LATEST_URL = f"https://github.com/{REPO}/releases/latest"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.zip"
ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 5
EXIT_UPDATED = 3
# Never touched by an update: the Python environment and caches made on this PC.
KEEP = {".venv", ".git", "__pycache__"}


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(map(int, match.groups())) if match else None


def latest_tag() -> str:
    request = urllib.request.Request(LATEST_URL, method="HEAD",
                                     headers={"User-Agent": f"PhoneMouse/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        final_url = response.geturl()
    match = re.search(r"/releases/tag/([^/?#]+)$", final_url)
    if not match:
        raise ValueError(f"unexpected answer from GitHub: {final_url}")
    return urllib.parse.unquote(match.group(1))


def download(tag: str) -> bytes:
    request = urllib.request.Request(ZIP_URL.format(tag=tag),
                                     headers={"User-Agent": f"PhoneMouse/{__version__}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def kept(rel: Path) -> bool:
    return any(part in KEEP for part in rel.parts)


def install(archive: bytes, root: Path) -> None:
    """Replace PhoneMouse's files under `root` with the ones in the GitHub zip."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp).resolve()
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for name in zf.namelist():
                target = (tmp_path / name).resolve()
                if not target.is_relative_to(tmp_path):
                    raise ValueError(f"unsafe path in the download: {name}")
            zf.extractall(tmp_path)
        # GitHub puts everything in one folder named like "PhoneMouse-1.2.0".
        (top,) = [p for p in tmp_path.iterdir()]
        if not (top / "server" / "__main__.py").is_file():
            raise ValueError("the download doesn't look like PhoneMouse")
        new_files = {p.relative_to(top) for p in top.rglob("*") if p.is_file()}

        for rel in sorted(new_files):
            if kept(rel):
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(top / rel, dest)

        # Remove files the new version no longer has, but only inside the folders it ships
        # (server, client, ...), never files the user put next to PhoneMouse.
        shipped_dirs = {rel.parts[0] for rel in new_files if len(rel.parts) > 1}
        for folder in shipped_dirs - KEEP:
            for old in list((root / folder).rglob("*")):
                rel = old.relative_to(root)
                if old.is_file() and not kept(rel) and rel not in new_files:
                    old.unlink()


def main() -> int:
    if "--no-update" in sys.argv[1:]:
        return 0
    if (ROOT / ".git").exists():
        # A git checkout (developer copy): don't overwrite it, `git pull` updates it.
        return 0
    current = parse_version(__version__)
    try:
        tag = latest_tag()
    except Exception as exc:  # offline, GitHub down, rate limit: just start as we are
        print(f"Could not check for updates ({exc}). Starting PhoneMouse {__version__}.")
        return 0
    latest = parse_version(tag)
    if latest is None or current is None or latest <= current:
        return 0

    print(f"Updating PhoneMouse {__version__} to {tag.lstrip('v')} ...")
    try:
        install(download(tag), ROOT)
    except Exception as exc:
        print(f"Update failed ({exc}). Starting PhoneMouse {__version__}.")
        print(f"You can download the new version from https://github.com/{REPO}/releases")
        return 0
    print(f"Updated to {tag.lstrip('v')}.")
    return EXIT_UPDATED


if __name__ == "__main__":
    sys.exit(main())
