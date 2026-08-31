"""Playlist: an arrangement's PlaylistID.Items blob of fixed-stride records.

The 16-byte record head is constant across eras (see detect.py, which infers
the era's record size from the pattern_base==20480 signature); the rest of a
record is era-opaque, so every record is built from a byte TEMPLATE - the
first existing record in the target blob - and a playlist with no existing
record has no honest template (FlpError, never invented tail bytes).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from flpkit.codec import FlpError, SpliceSite, Stream, Target, verify_identical
from flpkit.detect import PATTERN_INDEX_BASE, PLAYLIST_TRACK_SPACE, playlist_stride

EVENT_ARRANGEMENT_NEW = 99  # u16 arrangement index; starts an arrangement block
EVENT_PLAYLIST = 233  # blob of playlist-item records
RECORD_HEAD = struct.Struct("<IHHIHH")  # position, pattern_base, item_index, length, track_rvidx, group

# Bytes 24-31 are the clip's cut window in TICKS (u32 start, u32 end) - NOT
# the f32 offsets pyflp documents. Proven live: FL 2026 derives the visible
# clip length from this window (a zeroed window collapsed the clip to length 1
# and FL rewrote the head length field to match on its next save). An uncut
# clip spans 0..length; earlier eras stamp the f32 -1.0 bit pattern in BOTH
# fields as their uncut sentinel (corpus: FL-bundled demo/legacy projects,
# which FL 2026 itself opens and rewrites). The layout is only trusted where
# the file ITSELF exhibits a verified shape - the full span or the legacy
# sentinel; anything else (including a genuine cut) still refuses.
_WINDOW_AT = 24
_LEGACY_UNCUT = 0xBF800000  # f32 -1.0 bits, read as our u32 model


class ClipLike(Protocol):
    """What a caller-supplied clip needs, in tool units: a pattern number
    placed on a 1-based playlist track, ``start``/``length`` in beats."""

    pattern: int
    track: int
    start: float
    length: float


@dataclass(frozen=True)
class ClipSpec:
    """A ready-made ClipLike for callers without their own clip model."""

    pattern: int
    track: int
    start: float
    length: float


@dataclass(frozen=True)
class Clip:
    """One decoded record: the head in tool units plus the era-opaque tail
    (record bytes 16..stride, cut window included) carried raw, so a decoded
    clip re-encodes byte-identically."""

    track: int
    start: float  # beats
    length: float  # beats
    pattern: int | None = None  # set for pattern clips
    channel: int | None = None  # set for audio/automation channel clips
    group: int = 0
    tail: bytes = b""


class PlaylistFormat:
    name = "playlist"
    event_id = EVENT_PLAYLIST

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """The target arrangement's playlist event (arrangement events switch
        the current index; 0 before any). Captures the write template - the
        first existing record - and checks every record's cut window spans
        0..length, the only layout this writer trusts."""
        arrangement = target.arrangement or 0
        current = 0
        prev_end = 0
        existing = None
        for event_id, off, size in stream:
            head = prev_end
            prev_end = off + size
            if event_id == EVENT_ARRANGEMENT_NEW and size == 2:
                current = int.from_bytes(stream[off : off + size], "little")
            elif event_id == EVENT_PLAYLIST and current == arrangement and existing is None:
                existing = (head, off, size)

        self._template: bytes | None = None
        self._refusal = (
            f"arrangement {arrangement} has no playlist event; a write needs "
            "one existing clip as a byte template"
        )
        if existing is None:
            return SpliceSite(len(stream), len(stream), None)
        head, off, size = existing
        blob = bytes(stream[off : off + size])
        stride = playlist_stride(blob)
        if stride is None or not blob:
            self._refusal = (
                f"arrangement {arrangement}: playlist blob of {len(blob)} bytes "
                "fits no known record size; no honest template"
            )
            return SpliceSite(head, off + size, blob)
        for at in range(0, len(blob), stride):
            (length,) = struct.unpack_from("<I", blob, at + 8)
            window = struct.unpack_from("<II", blob, at + _WINDOW_AT)
            if window != (0, length) and window != (_LEGACY_UNCUT, _LEGACY_UNCUT):
                self._refusal = (
                    f"arrangement {arrangement}: record at byte {at} has cut window "
                    f"{window[0]}..{window[1]} for length {length}; refusing to write "
                    "the cut window into a file whose own records do not span "
                    "0..length (unverified layout)"
                )
                return SpliceSite(head, off + size, blob)
        self._template = blob[:stride]
        self._refusal = None
        return SpliceSite(head, off + size, blob)

    def encode(self, data: Sequence[ClipLike], ppq: int) -> bytes:
        """Tool-unit clips -> position-sorted records (all 54 corpus
        arrangements are stored that way). New records inherit the template's
        era tail with the uncut window packed in; decoded clips carry their
        own tail verbatim."""
        refusal = getattr(self, "_refusal", None)  # set by locate on the write path
        if refusal:
            raise FlpError(refusal)
        records = []
        for clip in data:
            if not 1 <= clip.track <= PLAYLIST_TRACK_SPACE:
                raise ValueError(f"track {clip.track} outside 1..{PLAYLIST_TRACK_SPACE}")
            # Exactly as quantized, no floor - decoded records must round-trip
            # their raw length byte-identically (same rule as notes).
            length = round(clip.length * ppq)
            tail = getattr(clip, "tail", b"")
            if not tail:
                template = getattr(self, "_template", None)
                if template is None:
                    raise FlpError("a new clip needs the located byte template; write through codec.patch")
                tail = bytearray(template[RECORD_HEAD.size :])
                struct.pack_into("<II", tail, _WINDOW_AT - RECORD_HEAD.size, 0, length)
            pattern = getattr(clip, "pattern", None)
            item_index = PATTERN_INDEX_BASE + pattern if pattern is not None else clip.channel
            records.append(
                RECORD_HEAD.pack(
                    round(clip.start * ppq), PATTERN_INDEX_BASE, item_index, length,
                    PLAYLIST_TRACK_SPACE - clip.track, getattr(clip, "group", 0),
                )
                + bytes(tail)
            )
        records.sort(key=lambda r: struct.unpack_from("<I", r)[0])
        return b"".join(records)

    def decode(self, blob: bytes, ppq: int) -> list[Clip]:
        """Records -> tool-unit clips at the detected stride. Raises FlpError
        when no stride fits (a new FL era) - the project reader surfaces that
        as a per-event warning, never a crash."""
        if not blob:
            return []
        stride = playlist_stride(blob)
        if stride is None:
            raise FlpError(f"playlist blob of {len(blob)} bytes fits no record size (new era?)")
        clips = []
        for at in range(0, len(blob), stride):
            position, _base, item_index, length, track_rvidx, group = RECORD_HEAD.unpack_from(blob, at)
            is_pattern = item_index >= PATTERN_INDEX_BASE
            clips.append(
                Clip(
                    track=PLAYLIST_TRACK_SPACE - track_rvidx,
                    start=position / ppq,
                    length=length / ppq,
                    pattern=item_index - PATTERN_INDEX_BASE if is_pattern else None,
                    channel=None if is_pattern else item_index,
                    group=group,
                    tail=blob[at + RECORD_HEAD.size : at + stride],
                )
            )
        return clips

    def verify(self, sent: Sequence[Clip], readback: Sequence[Clip]) -> None:
        verify_identical(sent, readback, "the arrangement's playlist")
