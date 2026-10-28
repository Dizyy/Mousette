"""The Deck: the phone's own buttons, kept on the PC so every phone gets the same ones and a
cleared browser loses nothing. Stored as JSON next to the encryption key."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("mousette.deck")


class DeckStore:
    """`path` None keeps the buttons in memory only (tests)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._buttons: list[dict] | None = None
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._buttons = data["buttons"] if isinstance(data.get("buttons"), list) else None
            except (OSError, ValueError, AttributeError, KeyError):
                log.warning("Could not read %s; the phone shows the default buttons", path)

    @property
    def buttons(self) -> list[dict] | None:
        """The saved buttons, or None when the phone should show its default ones."""
        return self._buttons

    def save(self, buttons: list[dict]) -> None:
        """`buttons` were checked by protocol.py. Written to a temp file first, so a crash
        never leaves half a file."""
        self._buttons = buttons
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"buttons": buttons}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
