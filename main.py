"""AI Job Intelligence & Discovery System - entry point.

Usage:  python main.py            (interactive)
        python main.py --help     (all options)
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # pragma: no cover - checked before heavy imports
    sys.stderr.write("Python 3.11 or newer is required.\n")
    sys.exit(1)

if sys.platform == "win32":  # make Unicode output safe on legacy Windows consoles
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from src.cli.application import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
