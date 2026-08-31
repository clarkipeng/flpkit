"""Signature detection: the ONLY place flpkit infers a size or a stride.

FL grows record layouts era by era without version-tagging them, so every
era-variable size here is DETECTED from an in-data signature, with a
hardcoded FL-2026-measured table as the warned fallback (detect-don't-assume).
"""

from __future__ import annotations

import functools
import logging
import struct
from collections.abc import Mapping

log = logging.getLogger("flpkit")

# Events whose size breaks the classic range rule. 172 is a ONE-byte event:
# verified by full-stream consistency on 16 FL-2026-authored files (every one
# consumes exactly, with ChannelID.New counts matching the FLhd header) and on
# the two shipped templates that contain it - under the classic 4-byte read,
# "Basic with limiter" loses a channel AND its tempo event to the desync.
# pyflp shares the 4-byte bug (it cannot parse fresh FL 2026 saves at all),
# so this table is where flpkit deliberately diverges from the oracle.
# It is the FALLBACK: every public function takes ``event_size_overrides`` so
# a capability profile's offline-detected table wins over this hardcoded one;
# falling back here is logged once per process.
EVENT_SIZE_OVERRIDES_FALLBACK: Mapping[int, int] = {172: 1}


@functools.cache
def _log_size_override_fallback() -> None:
    log.info(
        "no event_size_overrides provided; falling back to the built-in "
        "FL-2026-measured table %r (a capability profile can supply the "
        "detected table for other FL versions)",
        EVENT_SIZE_OVERRIDES_FALLBACK,
    )


def resolve_size_overrides(overrides: Mapping[int, int] | None) -> Mapping[int, int]:
    if overrides is not None:
        return overrides
    _log_size_override_fallback()
    return EVENT_SIZE_OVERRIDES_FALLBACK


# Playlist item records: the 16-byte decoded head is constant across eras -
# position u32, pattern_base u16 (ALWAYS 20480 - the per-record signature),
# item_index u16 (pattern iid+20480, else channel iid), length u32,
# track_rvidx u16 (reversed: track 1 = 499 in the 500-track space), group u16.
# The RECORD SIZE grows era by era: 32 (classic), 60 (FL 21), 80 (observed in
# 17 of FL's bundled demos), 88 (FL 2026, decoded exactly against a project
# whose playlist visibly held 4 clips). Rather than hardcode that list, the
# stride is DETECTED: the smallest 4-byte-aligned size >= 32 that divides the
# blob and puts pattern_base == 20480 (and a sane track) at EVERY record head.
# All 49 corpus files with playlists resolve unambiguously under this rule.
PLAYLIST_STRIDE_MIN = 32
PLAYLIST_STRIDE_MAX = 256
PLAYLIST_TRACK_SPACE = 500  # track number = PLAYLIST_TRACK_SPACE - track_rvidx
PATTERN_INDEX_BASE = 20480  # item_index >= this means a pattern clip


def _stride_fits(blob: bytes, stride: int) -> bool:
    """True when every stride-aligned record head carries the pattern_base
    signature (20480) and a track index inside the 500-track space."""
    for at in range(0, len(blob), stride):
        (base,) = struct.unpack_from("<H", blob, at + 4)
        (rvidx,) = struct.unpack_from("<H", blob, at + 12)
        if base != PATTERN_INDEX_BASE or not 0 <= PLAYLIST_TRACK_SPACE - rvidx <= PLAYLIST_TRACK_SPACE:
            return False
    return True


def playlist_stride(blob: bytes) -> int | None:
    """The detected record size of a playlist blob, None when nothing fits."""
    return next(
        (
            s
            for s in range(PLAYLIST_STRIDE_MIN, PLAYLIST_STRIDE_MAX + 1, 4)
            if len(blob) % s == 0 and _stride_fits(blob, s)
        ),
        None,
    )
