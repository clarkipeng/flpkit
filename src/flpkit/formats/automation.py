"""Automation: a type-5 channel's points blob (event 234).

Blob layout (pyflp-verified field map, corpus-verified sizes): 17B header,
point count u32 at 17, then 24B points - x_delta f64 (BEATS from the previous
point; corpus positions land on clean beat fractions), value f64 (0..1),
tension f32, 4B tension-linked tail. After the points FL writes a trailer
(112 bytes in 1098 of 1100 corpus blobs, 136 in 2). Writes are correct by
construction, not by decoding: the header and the era trailer are carried
verbatim from the channel's own blob, only the count and the point records
between them are rebuilt. A channel that is not kind 5 or has no blob is an
error, not an invitation to fabricate: creating a NEW clip needs live
minimal-pair evidence this writer deliberately does not guess at.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from flpkit.codec import (
    EVENT_CHANNEL_AUTOMATION,
    EVENT_CHANNEL_TYPE,
    FlpError,
    SpliceSite,
    Stream,
    Target,
    verify_identical,
)

log = logging.getLogger("flpkit")

CHANNEL_TYPE_AUTOMATION = 5  # corpus: all 1100 automation blobs sit in type-5 channels
_COUNT_AT = 17
_POINTS_AT = 21
_POINT_SIZE = 24
_TAIL_AT = 20  # tail offset inside one 24-byte point record
# The tail FL writes on a plain point (corpus: 1,093 of 9,676 points carry
# exactly this, typically a clip's first point; the rest vary per point, which
# is why decoded points carry their own tail bytes for byte-faithful rewrites).
AUTOMATION_TAIL_DEFAULT = bytes(4)
# FL itself stores values a hair outside 0..1 (corpus max 1.0000403), so input
# validation allows the same slack rather than refusing FL's own data.
_VALUE_SLACK = 0.001


class AutomationPointLike(Protocol):
    """What a caller-supplied point needs: ``position`` is beats from clip
    start (absolute; the writer converts back to FL's stored x-deltas),
    ``value`` is normalized 0..1, ``tension`` -1..1. A point may also carry a
    4-byte ``tail`` attribute (FL's opaque per-point tail); points without one
    get the plain-point default."""

    position: float
    value: float
    tension: float


@dataclass(frozen=True)
class AutomationPointSpec:
    """A ready-made AutomationPointLike for callers without their own model."""

    position: float
    value: float
    tension: float = 0.0


@dataclass(frozen=True)
class FlpAutomationPoint:
    """One decoded point, relative to the clip start. ``tail`` is FL's opaque
    4-byte per-point tail, carried raw so a decoded clip is readback-identical;
    it is byte-identical for FL-authored blobs (corpus-tested)."""

    position: float  # beats from clip start
    value: float  # normalized 0..1
    tension: float
    tail: bytes = AUTOMATION_TAIL_DEFAULT


class AutomationFormat:
    name = "automation"
    event_id = EVENT_CHANNEL_AUTOMATION
    frame = None

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """The target channel's automation blob, refusing dishonest ground:
        an unknown channel (IndexError), a non-type-5 channel, a channel with
        no blob, or a blob whose count overruns it. Captures the header and
        the era trailer for encode to carry verbatim."""
        seen: set[int] = set()
        kind = None
        found = None
        for channel, event_id, head, off, size in stream.channel_events():
            if channel is None:
                continue
            seen.add(channel)
            if channel != target.channel:
                continue
            if event_id == EVENT_CHANNEL_TYPE and size == 1:
                kind = stream[off]
            elif event_id == EVENT_CHANNEL_AUTOMATION and found is None:
                found = (head, off, size)
        if target.channel not in seen:
            raise IndexError(f"no channel {target.channel}; project has {len(seen)}")
        if kind != CHANNEL_TYPE_AUTOMATION:
            raise FlpError(
                f"channel {target.channel} is kind {kind}, not an automation channel "
                f"(kind {CHANNEL_TYPE_AUTOMATION}); refusing to write points into it"
            )
        if found is None:
            raise FlpError(
                f"channel {target.channel} has no automation blob (event "
                f"{EVENT_CHANNEL_AUTOMATION}); only existing clips are patched - "
                "creating one needs undecoded header/trailer/link bytes"
            )
        head, off, size = found
        blob = bytes(stream[off : off + size])
        if len(blob) < _POINTS_AT:
            raise FlpError(
                f"channel {target.channel} automation blob of {len(blob)} bytes is "
                "truncated; no honest header to carry"
            )
        (count,) = struct.unpack_from("<I", blob, _COUNT_AT)
        points_end = _POINTS_AT + count * _POINT_SIZE
        if points_end > len(blob):
            raise FlpError(
                f"channel {target.channel} automation blob claims {count} points but "
                f"holds {len(blob)} bytes; refusing to guess where the trailer starts"
            )
        self._header = blob[:_COUNT_AT]
        self._trailer = blob[points_end:]
        return SpliceSite(head, off + size, blob)

    def encode(self, data: Sequence[AutomationPointLike], ppq: int) -> bytes:
        """Points -> header (verbatim) + count + 24B records + trailer
        (verbatim). Points are sorted by position first; FL stores forward
        x-deltas (zero deltas, i.e. vertical steps, are fine)."""
        ordered = sorted(data, key=lambda p: p.position)
        for p in ordered:
            if not -_VALUE_SLACK <= p.value <= 1 + _VALUE_SLACK:
                raise ValueError(f"point value {p.value} out of range (normalized 0..1)")
            if not -1.0 <= p.tension <= 1.0:
                raise ValueError(f"point tension {p.tension} out of range (-1..1)")
            tail = getattr(p, "tail", None)
            if tail is not None and len(tail) != 4:
                raise ValueError(f"point tail must be 4 bytes, got {len(tail)}")
        if ordered and ordered[0].position < 0:
            raise ValueError(f"point position {ordered[0].position} is negative")
        body = bytearray(self._header)
        body += struct.pack("<I", len(ordered))
        prev = 0.0
        for p in ordered:
            tail = getattr(p, "tail", None)
            body += struct.pack("<ddf", p.position - prev, p.value, p.tension)
            body += bytes(tail) if tail is not None else AUTOMATION_TAIL_DEFAULT
            prev = p.position
        return bytes(body) + self._trailer

    def decode(self, blob: bytes, ppq: int) -> list[FlpAutomationPoint]:
        """Points with cumulative-sum x_delta -> absolute positions. A
        malformed blob decodes to no points, with a warning - the project
        reader's never-crash contract (the write path refuses these in
        locate, before any byte moves)."""
        if len(blob) < _POINTS_AT:
            log.warning("automation blob of %d bytes is truncated", len(blob))
            return []
        (count,) = struct.unpack_from("<I", blob, _COUNT_AT)
        if _POINTS_AT + count * _POINT_SIZE > len(blob):
            log.warning("automation blob claims %d points but holds %d bytes", count, len(blob))
            return []
        points = []
        position = 0.0
        for i in range(count):
            at = _POINTS_AT + i * _POINT_SIZE
            delta, value, tension = struct.unpack_from("<ddf", blob, at)
            position += delta
            points.append(
                FlpAutomationPoint(
                    position=position, value=value, tension=tension,
                    tail=bytes(blob[at + _TAIL_AT : at + _POINT_SIZE]),
                )
            )
        return points

    def verify(self, sent: Sequence[FlpAutomationPoint], readback: Sequence[FlpAutomationPoint]) -> None:
        verify_identical(sent, readback, "the channel's automation points")
