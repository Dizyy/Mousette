"""Draws a QR code with text characters, so it shows in the PhoneMouse window."""

from __future__ import annotations

import segno

BORDER = 4  # light modules around the code; scanners need this quiet zone

# Two module rows per text line: (top is light, bottom is light) -> character.
# Light modules are drawn as blocks, so the code reads dark-on-light in a dark console.
_CHARS = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


def qr_lines(text: str) -> list[str]:
    matrix = segno.make(text, error="m", boost_error=False).matrix
    size = len(matrix) + 2 * BORDER
    light = [[True] * size for _ in range(BORDER)]
    light += [[True] * BORDER + [not m for m in row] + [True] * BORDER for row in matrix]
    light += [[True] * size for _ in range(BORDER)]
    if len(light) % 2:
        light.append([True] * size)
    return ["".join(_CHARS[pair] for pair in zip(top, bottom))
            for top, bottom in zip(light[::2], light[1::2])]
