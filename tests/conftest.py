"""Shared test helpers. The FL-template tests skip when FL Studio is absent."""

from __future__ import annotations

import os
from pathlib import Path


def fl_app() -> Path:
    """Newest installed FL Studio app bundle (macOS), or a nonexistent path -
    template-based tests skip via `.is_file()` checks when FL is not installed."""
    override = os.environ.get("FL_APP")
    if override:
        return Path(override)
    installed = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
    return installed[0] if installed else Path("/Applications/FL Studio.app")
