"""Channel levels: ONE event (ChannelID.Levels, 219) with a fixed 24-byte
payload - pan i32 [0:4], volume u32 [4:8], pitch i32 [8:12], filter 12B -
verified by decoding FL 2026's own stock templates. FL stores fixed-point
ints; the tool surface is 0..1 volume and -1..1 pan so a model never has to
know that. A legacy project whose channel has no 219 event is refused
(FlpError) rather than written with guessed units.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

from flpkit.codec import EVENT_CHANNEL_LEVELS, FlpError, SpliceSite, Stream, Target, verify_identical

LEVEL_MAX = 12800
PAN_CENTRE = 6400
VOLUME_DEFAULT = 10000


@dataclass(frozen=True)
class Levels:
    """One channel's mix state in tool units, plus the opaque 12-byte filter
    tail carried raw so a rewrite is byte-identical."""

    volume: float  # 0..1
    pan: float  # -1..1, 0 = centre
    pitch_semitones: int
    tail: bytes = bytes(12)


class LevelsFormat:
    name = "levels"
    event_id = EVENT_CHANNEL_LEVELS

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """The target channel's Levels event (first 24-byte 219 attributed to
        it). An unknown channel is IndexError; a channel without one is a
        legacy-format refusal."""
        seen: set[int] = set()
        found = None
        for channel, event_id, head, off, size in stream.channel_events():
            if channel is None:
                continue
            seen.add(channel)
            if (
                event_id == EVENT_CHANNEL_LEVELS and size == 24
                and channel == target.channel and found is None
            ):
                found = (head, off, size)
        if target.channel not in seen:
            raise IndexError(f"no channel {target.channel}; project has {len(seen)}")
        if found is None:
            raise FlpError(
                f"channel {target.channel} has no Levels event (id 219); refusing to "
                "write a legacy-format project with guessed units"
            )
        head, off, size = found
        return SpliceSite(head, off + size, bytes(stream[off : off + size]))

    def encode(self, data: Sequence[Levels], ppq: int) -> bytes:
        """Exactly one Levels -> the 24-byte payload (tool units re-scaled to
        FL's fixed-point ints, filter tail verbatim)."""
        if len(data) != 1:
            raise FlpError(f"a channel has exactly one Levels, got {len(data)}")
        (levels,) = data
        return (
            struct.pack(
                "<iIi",
                round(PAN_CENTRE + levels.pan * PAN_CENTRE),
                round(levels.volume * LEVEL_MAX),
                levels.pitch_semitones,
            )
            + levels.tail
        )

    def decode(self, blob: bytes, ppq: int) -> list[Levels]:
        pan, volume, pitch = struct.unpack_from("<iIi", blob)
        return [
            Levels(
                volume=volume / LEVEL_MAX,
                pan=(pan - PAN_CENTRE) / PAN_CENTRE,
                pitch_semitones=pitch,
                tail=bytes(blob[12:]),
            )
        ]

    def verify(self, sent: Sequence[Levels], readback: Sequence[Levels]) -> None:
        verify_identical(sent, readback, "the channel's levels")
