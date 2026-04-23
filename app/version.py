"""Runtime version string surfaced into the UI footer.

Computed once at module import by running `git describe --tags --always
--dirty` against the repo containing this file. The deploy flow is
`git checkout <tag>`, so a clean production deploy produces a plain tag
name (`v0.6.0`). An operator who committed locally without pushing or
deployed from an ahead-of-tag commit sees it as `v0.6.0-3-gabc1234` --
drift is visible rather than silently masked.

Fallbacks:
  - `.git` not present (tarball install, rare for ephemera's flow): "unknown"
  - `git` binary not on PATH: "unknown"
  - Any subprocess failure / timeout: "unknown"

Zero per-request cost -- the string is cached in a module constant at
import, read directly from `template_context()` on every page render.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _compute_version() -> str:
    """Shell out to `git describe` once. Exceptions swallowed into the
    "unknown" sentinel so a misconfigured environment degrades to a
    readable footer rather than a 500."""
    repo_root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"
    return result.stdout.strip() or "unknown"


VERSION: str = _compute_version()
