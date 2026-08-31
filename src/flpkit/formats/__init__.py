"""DATA-like Format implementations: each module states ONE .flp element -
how it is found (locate), packed (encode), unpacked (decode), and checked
(verify). Reading one module IS that feature; the engine is codec.py."""

from .automation import AutomationFormat
from .levels import LevelsFormat
from .notes import NotesFormat
from .playlist import PlaylistFormat
from .tempo import TempoFormat

# Every Format this library ships, for generic iteration (e.g. round-trip
# suites). Classes, not instances: a Format instance is call-scoped (its
# locate captures in-file templates), so construct one per use.
ALL = (NotesFormat, PlaylistFormat, AutomationFormat, LevelsFormat, TempoFormat)

__all__ = ["ALL", "AutomationFormat", "LevelsFormat", "NotesFormat", "PlaylistFormat", "TempoFormat"]
