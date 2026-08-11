"""Console output that survives a Windows code page.

The default stdout encoding on a Windows console is still cp1252, which cannot
encode box-drawing characters. A CLI that raises ``UnicodeEncodeError`` while
printing its own tree view is broken on a platform we claim to support, so
output degrades instead:

* streams are switched to UTF-8 where the interpreter allows it;
* if that is not possible, glyphs fall back to ASCII equivalents.

Colour follows the ``NO_COLOR`` convention and is suppressed when stdout is not
a terminal, so piping output into a file or a test harness yields clean text.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import IO, Any

_UNICODE_GLYPHS = {
    "branch": "├── ",
    "last": "└── ",
    "pipe": "│   ",
    "blank": "    ",
    "rule": "─",
    "arrow": "→",
}

_ASCII_GLYPHS = {
    "branch": "|-- ",
    "last": "`-- ",
    "pipe": "|   ",
    "blank": "    ",
    "rule": "-",
    "arrow": "->",
}


def enable_utf8() -> None:
    """Try to switch stdout/stderr to UTF-8. Safe to call more than once."""
    for stream_name in ("stdout", "stderr"):
        stream: IO[Any] | None = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # Detached or already-wrapped stream; the glyph fallback covers it.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def supports_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "ascii").lower()
    return encoding.replace("-", "") in {"utf8", "utf16", "utf32"}


def glyphs() -> dict[str, str]:
    return _UNICODE_GLYPHS if supports_unicode() else _ASCII_GLYPHS


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if color_enabled() else text


def heading(text: str, width: int = 64) -> str:
    rule = glyphs()["rule"]
    padding = max(2, width - len(text) - 6)
    return bold(f"{rule * 2} {text} {rule * padding}")
