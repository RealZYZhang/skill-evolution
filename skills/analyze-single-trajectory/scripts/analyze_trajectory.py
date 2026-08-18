#!/usr/bin/env python3
"""Run the approved semantic stage of the packaged single-trajectory workflow."""

from __future__ import annotations

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.trajectory_error_analysis import main


if __name__ == "__main__":
    raise SystemExit(main())
