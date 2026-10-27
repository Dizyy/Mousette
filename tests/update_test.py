"""Test of the automatic updater's file replacement, for the source folder and the .exe. No network needed.

Run:  .venv\\Scripts\\python.exe tests\\update_test.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server import update  # noqa: E402
from server.update import install, parse_version  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        failures.append(label)


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def main() -> int:
    print("Version numbers")
    check(parse_version("v1.2.0") == (1, 2, 0), "v1.2.0 parses")
    check(parse_version("1.10.0") > parse_version("1.9.9"), "1.10.0 is newer than 1.9.9")
    check(parse_version("v1.2.0-beta") is None, "pre-release tags are ignored")

    print("Installing an update")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = {
            "start.bat": "old", "server/__main__.py": "old", "server/removed.py": "old",
            "server/__pycache__/x.pyc": "cache", ".venv/Scripts/python.exe": "venv",
            "my notes.txt": "mine",
        }
        for name, text in old.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        install(make_zip({
            "CouchPad-1.2.0/start.bat": "new", "CouchPad-1.2.0/server/__main__.py": "new",
            "CouchPad-1.2.0/server/added.py": "new", "CouchPad-1.2.0/.venv/evil": "x",
        }), root)
        read = lambda name: (root / name).read_text() if (root / name).exists() else None  # noqa: E731
        check(read("start.bat") == "new" and read("server/__main__.py") == "new", "files replaced")
        check(read("server/added.py") == "new", "new files added")
        check(read("server/removed.py") is None, "files the new version dropped are removed")
        check(read(".venv/Scripts/python.exe") == "venv" and read(".venv/evil") is None, ".venv untouched")
        check(read("my notes.txt") == "mine", "the user's own files are kept")

    print("Bad downloads are refused")
    for label, files in [("path outside the folder", {"../evil.txt": "x"}),
                         ("not CouchPad", {"Other-1.0/readme.md": "x"})]:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "start.bat").write_text("old")
            try:
                install(make_zip(files), Path(tmp))
                refused = False
            except ValueError:
                refused = True
            check(refused and (Path(tmp) / "start.bat").read_text() == "old", f"{label}: refused")

    print("Updating the single-file .exe")
    for label, download, updated in [("new release", b"MZ" + b"n" * 1_000_000, True),
                                     ("broken download", b"<html>not found</html>", False)]:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "CouchPad.exe"
            exe.write_bytes(b"MZ old")
            (Path(tmp) / "CouchPad.exe.old").write_bytes(b"MZ older")
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = download
            env = {k: v for k, v in os.environ.items() if k != "COUCHPAD_UPDATED"}
            with mock.patch.object(sys, "executable", str(exe)),                  mock.patch.dict(os.environ, env, clear=True),                  mock.patch.object(update, "latest_tag", return_value="v99.0.0"),                  mock.patch.object(update.urllib.request, "urlopen", return_value=response),                  mock.patch.object(update.subprocess, "Popen") as popen:
                try:
                    update.update_exe(["--port", "9000"])
                    exited = False
                except SystemExit:
                    exited = True
            if updated:
                check(exe.read_bytes() == download and exited, f"{label}: new .exe in place, old one restarts it")
                check(popen.call_args.args[0] == [str(exe), "--port", "9000"], f"{label}: restarted with the same options")
                check((Path(tmp) / "CouchPad.exe.old").read_bytes() == b"MZ old", f"{label}: running copy kept as .old")
            else:
                check(exe.read_bytes() == b"MZ old" and not exited and not popen.called, f"{label}: refused, old .exe kept")
                check(not (Path(tmp) / "CouchPad.exe.new").exists(), f"{label}: nothing left behind")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
