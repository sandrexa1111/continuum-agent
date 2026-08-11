#!/usr/bin/env python3
"""Verify that every relative Markdown link points at a file that exists.

Documentation that links to files which are not there is worse than
documentation with no links: it signals the docs were never checked. The spec
cross-references heavily, so this runs in CI.

This began life as a shell pipeline in the workflow and failed on its first run
for a reason worth recording: under ``set -o pipefail``, a Markdown file
containing *no* relative links makes ``grep`` exit non-zero, which failed the
job even though nothing was broken. Exit codes from filters inside pipelines are
a poor fit for "count the problems" logic, so it moved here.

Usage:

    python scripts/check_links.py [root]

Exits 0 when every link resolves, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# [text](target) -- skipping images (![...]) is unnecessary here since a missing
# image is also a broken link worth reporting.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}
EXTERNAL = re.compile(r"^(https?:|mailto:|#)")


def check(root: Path) -> list[str]:
    problems: list[str] = []

    for path in sorted(root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")

        for match in LINK.finditer(text):
            target = match.group(1)
            if EXTERNAL.match(target):
                continue

            # Strip any anchor; we verify the file exists, not the heading.
            file_part = target.split("#", 1)[0]
            if not file_part:
                continue

            resolved = (path.parent / file_part).resolve()
            if not resolved.exists():
                line = text[: match.start()].count("\n") + 1
                problems.append(
                    f"{path.relative_to(root).as_posix()}:{line}: broken link -> {target}"
                )

    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    problems = check(root)

    for problem in problems:
        # GitHub Actions renders this annotation format inline on the PR.
        print(f"::error::{problem}" if _in_actions() else problem, file=sys.stderr)

    count = len(list(root.rglob("*.md")))
    if problems:
        print(f"\n{len(problems)} broken link(s) across {count} markdown file(s)")
        return 1
    print(f"all relative links resolve ({count} markdown files checked)")
    return 0


def _in_actions() -> bool:
    import os

    return os.environ.get("GITHUB_ACTIONS") == "true"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
