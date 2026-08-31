"""Notes: a pattern's PatternID.Notes blob of 24-byte packed note records.

A pattern's blob holds EVERY channel's notes, so the target scope is one
channel within one pattern: replacing is scoped to the target channel
(measured: naive whole-blob replacement of one note on channel 0 wiped 51
notes on channel 3 of the same pattern).
"""

from __future__ import annotations

import logging
import struct
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from flpkit.codec import (
    EVENT_CHANNEL_NEW,
    FlpError,
    SpliceSite,
    Stream,
    Target,
    frame_event,
    is_channel_scoped,
    verify_identical,
)

log = logging.getLogger("flpkit")

EVENT_PATTERN_NEW = 65  # u16 pattern number
EVENT_PATTERN_NOTES = 224  # varint-length blob of NOTE_STRUCT records
EVENT_CUR_GROUP_ID = 146  # ProjectID.CurGroupId; the anchor for a new pattern block

# 24-byte packed note record inside a PatternID.Notes blob (matches what FL
# writes): position u32 (ticks), flags u16, rack_channel u16, length u32
# (ticks), key u16 (MIDI), group u16, then fine_pitch, u1, release,
# midi_channel, pan, velocity, mod_x, mod_y - one byte each.
NOTE_STRUCT = struct.Struct("<IHHIHHBBBBBBBB")
NOTE_SIZE = 24

# Neutral per-note values FL writes for an ordinary note; wrong defaults show
# up as notes that play but behave oddly (silent, hard-panned, pitch-shifted).
FINE_PITCH_CENTER = 120
RELEASE_DEFAULT = 64
NOTE_PAN_CENTER = 64
MOD_DEFAULT = 128
# Every note FL itself writes carries bit 0x4000. Surveyed across the 132
# bundled FL 2026 projects that contain notes: 23,526 records with
# flags=0x4000 and 909 with 0x4008 - and NOT ONE with flags=0. New notes
# template their flags word from the target file's OWN notes when it has any
# (detect-don't-assume); this corpus constant is the fallback.
NOTE_FLAGS_DEFAULT = 0x4000


class NoteLike(Protocol):
    """What a caller-supplied note needs, in tool units: ``key`` is MIDI
    0-127, ``start``/``length`` are beats, ``velocity``/``pan`` are 0..1
    (pan 0.5 = centre). Any object with these attributes works."""

    key: int
    start: float
    length: float
    velocity: float
    pan: float


@dataclass(frozen=True)
class NoteSpec:
    """A ready-made NoteLike for callers without their own note model."""

    key: int
    start: float
    length: float
    velocity: float = 0.8
    pan: float = 0.5


@dataclass(frozen=True)
class Note(NoteSpec):
    """One decoded note record, full-fidelity: the NoteLike tool units plus
    every raw field the tool surface does not model, so a decoded note
    re-encodes byte-identically (kept notes survive any rewrite untouched)."""

    channel: int = 0
    flags: int = NOTE_FLAGS_DEFAULT
    group: int = 0
    fine_pitch: int = FINE_PITCH_CENTER
    u1: int = 0
    release: int = RELEASE_DEFAULT
    midi_channel: int = 0
    mod_x: int = MOD_DEFAULT
    mod_y: int = MOD_DEFAULT


class NotesFormat:
    name = "notes"
    event_id = EVENT_PATTERN_NOTES

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """The target pattern's notes blob (only a 224 event while that
        pattern is current counts), split into the target channel's records
        and the rest. One walk also finds where a missing pattern block would
        splice - after the display-group anchor, where FL itself writes
        patterns (NOT stream offset 0: ahead of FLVersion, FL refuses the
        file) - and surveys the file's own note-flags template."""
        current = 0
        prev_end = 0
        pattern_end = existing = anchor_end = channel_section_head = None
        flags_seen: Counter[int] = Counter()
        for event_id, off, size in stream:
            head = prev_end
            prev_end = off + size
            if event_id == EVENT_PATTERN_NEW and size == 2:
                current = int.from_bytes(stream[off : off + size], "little")
                if current == target.pattern and pattern_end is None:
                    pattern_end = off + size
            elif event_id == EVENT_PATTERN_NOTES:
                if size % NOTE_SIZE == 0:
                    flags_seen.update(
                        int.from_bytes(stream[off + at + 4 : off + at + 6], "little")
                        for at in range(0, size, NOTE_SIZE)
                    )
                if current == target.pattern and existing is None:
                    existing = (head, off, size)
            elif event_id == EVENT_CUR_GROUP_ID and anchor_end is None:
                anchor_end = off + size
            elif (
                channel_section_head is None
                and anchor_end is None
                and (event_id == EVENT_CHANNEL_NEW or is_channel_scoped(event_id))
            ):
                channel_section_head = head
        self._flags = flags_seen.most_common(1)[0][0] if flags_seen else None
        self._channel = target.channel

        if existing is not None:
            head, off, size = existing
            blob = bytes(stream[off : off + size])
            mine, rest = bytearray(), bytearray()
            for at in range(0, len(blob) - NOTE_SIZE + 1, NOTE_SIZE):
                record = blob[at : at + NOTE_SIZE]
                in_scope = int.from_bytes(record[6:8], "little") == target.channel
                (mine if in_scope else rest).extend(record)
            scoped = bytes(mine) if target.channel is not None else None
            return SpliceSite(head, off + size, blob, scoped=scoped, rest=bytes(rest))
        if pattern_end is not None:
            return SpliceSite(pattern_end, pattern_end, None)
        # No such pattern yet - splice a new pattern block ahead of the event.
        prelude = frame_event(EVENT_PATTERN_NEW, int(target.pattern or 0).to_bytes(2, "little"))
        at = anchor_end if anchor_end is not None else channel_section_head
        at = at if at is not None else len(stream)
        return SpliceSite(at, at, None, prelude=prelude)

    def encode(self, data: Sequence[NoteLike], ppq: int) -> bytes:
        """Tool-unit notes -> packed records (ticks; velocity/pan re-scaled).
        Fields a plain NoteLike does not carry get FL's neutral defaults, the
        flags word from the file's own template (locate surveyed it; the
        corpus constant covers a file with no notes yet), and the rack channel
        from the write target."""
        flags = self._flags if self._flags is not None else NOTE_FLAGS_DEFAULT
        if self._flags is None and data:
            log.debug("no existing notes to template flags from; using the corpus default 0x%04x", flags)
        packed = bytearray()
        for note in data:
            packed += NOTE_STRUCT.pack(
                round(note.start * ppq),
                getattr(note, "flags", flags),
                getattr(note, "channel", self._channel) or 0,
                max(1, round(note.length * ppq)),  # length in ticks, never 0
                note.key,
                getattr(note, "group", 0),
                getattr(note, "fine_pitch", FINE_PITCH_CENTER),
                getattr(note, "u1", 0),
                getattr(note, "release", RELEASE_DEFAULT),
                getattr(note, "midi_channel", 0),
                min(255, max(0, round(note.pan * 128))),
                min(255, max(0, round(note.velocity * 127))),
                getattr(note, "mod_x", MOD_DEFAULT),
                getattr(note, "mod_y", MOD_DEFAULT),
            )
        return bytes(packed)

    def decode(self, blob: bytes, ppq: int) -> list[Note]:
        """Unpack records to full-fidelity notes in beats/0..1 tool units
        (exact: every field re-encodes to the same bytes). Raises FlpError on
        a truncated blob."""
        if len(blob) % NOTE_SIZE:
            raise FlpError(f"notes blob of {len(blob)} bytes is not a multiple of {NOTE_SIZE}")
        return [
            Note(
                key=key, start=position / ppq, length=length / ppq,
                velocity=velocity / 127, pan=pan / 128,
                channel=channel, flags=flags, group=group, fine_pitch=fine_pitch,
                u1=u1, release=release, midi_channel=midi_channel, mod_x=mod_x, mod_y=mod_y,
            )
            for (
                position, flags, channel, length, key, group,
                fine_pitch, u1, release, midi_channel, pan, velocity, mod_x, mod_y,
            ) in NOTE_STRUCT.iter_unpack(blob)
        ]

    def verify(self, sent: Sequence[Note], readback: Sequence[Note]) -> None:
        verify_identical(sent, readback, "the pattern's notes")
