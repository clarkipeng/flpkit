"""The ONE read+write engine over the FLP event stream.

A .flp element type (notes, playlist, automation, levels, tempo) implements
the ``Format`` protocol; :func:`read` and :func:`patch` are the only paths
that touch the file. All the messy splice and chunk-length math lives here,
once. Writes are raw byte surgery - patch exactly the bytes that express the
change, never reserialize the whole file (FL rejects whole-file
reserialization by third-party writers; verified live 2026-08-27) - and every
write verifies itself by re-reading the saved file.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .detect import resolve_size_overrides

Mode = Literal["merge", "replace"]


class FlpError(ValueError):
    """Malformed .flp input or a write whose readback did not match."""


# -- stream structure facts (verified live; see the parent project's e2e suite)
EVENT_CHANNEL_NEW = 64  # u16 channel IID; starts that channel's event block
EVENT_MIXER_FLAGS = 236  # InsertID.Flags; first one means the mixer section began
EVENT_CHANNEL_TYPE = 21  # 1 byte; 5 marks an automation channel
EVENT_CHANNEL_LEVELS = 219  # 24B: pan i32, volume u32, pitch i32, filter 12B
EVENT_CHANNEL_AUTOMATION = 234  # the channel's automation points blob
EVENT_NAME_LEGACY = 192  # ChannelID._Name
EVENT_NAME_INTERNAL = 201  # PluginID.InternalName (e.g. the generator name)
EVENT_NAME_USER = 203  # PluginID.Name (the user-visible rename)
EVENT_VOL_WORD = 72  # legacy per-channel volume (pre-Levels files)
EVENT_PAN_WORD = 73  # legacy per-channel pan
EVENT_VOL_BYTE = 2  # older still
EVENT_PAN_BYTE = 3

# Events that belong to the currently open rack channel.
CHANNEL_SCOPED = frozenset((
    EVENT_CHANNEL_TYPE, EVENT_CHANNEL_LEVELS, EVENT_CHANNEL_AUTOMATION,
    EVENT_NAME_LEGACY, EVENT_NAME_INTERNAL, EVENT_NAME_USER,
    EVENT_VOL_WORD, EVENT_PAN_WORD, EVENT_VOL_BYTE, EVENT_PAN_BYTE,
))


def is_channel_scoped(event_id: int) -> bool:
    return event_id in CHANNEL_SCOPED


@dataclass(frozen=True)
class Target:
    """Which element instance a Format addresses; unused fields stay None."""

    pattern: int | None = None
    channel: int | None = None
    arrangement: int | None = None


@dataclass(frozen=True)
class SpliceSite:
    """Everything one locating walk found about WHERE an element lives.

    ``scoped``/``rest`` split the payload by the target: :func:`read` decodes
    ``scoped``; a replace write keeps ``rest`` and drops ``scoped`` (a merge
    keeps everything). Formats whose whole payload IS the target scope leave
    them at their defaults.
    """

    head: int  # stream offset where the element's event begins (or will)
    end: int  # just past the existing event; == head when the element is absent
    payload: bytes | None  # the existing element's payload; None = no event yet
    scoped: bytes | None = None  # target-scoped subset of payload; None = all of it
    rest: bytes = b""  # payload outside the scope, in file order
    prelude: bytes = b""  # event bytes spliced ahead when creating the element


class Format(Protocol):
    """The contract a .flp element type implements. Reading an implementation
    tells you how that element is found, packed, unpacked, and checked -
    nothing hidden in the engine. An instance is call-scoped: ``locate`` runs
    first and may capture in-file templates (a flags word, a record template,
    a header/trailer) that ``encode`` then carries (detect-don't-assume)."""

    name: str  # "notes", "playlist", "automation", "levels", "tempo"
    event_id: int  # the FLP event that carries it

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """WHERE this element lives (e.g. notes: the pattern's blob)."""
        ...

    def encode(self, data: Sequence[Any], ppq: int) -> bytes:
        """Tool units -> the element's file bytes (validating the input)."""
        ...

    def decode(self, blob: bytes, ppq: int) -> list[Any]:
        """File bytes -> tool units; byte-faithful (encode of the result
        reproduces ``blob`` exactly), so kept items survive any rewrite."""
        ...

    def verify(self, sent: Sequence[Any], readback: Sequence[Any]) -> None:
        """Raise FlpError unless the saved file decodes to exactly ``sent``."""
        ...


def read(
    path: Path,
    fmt: Format,
    target: Target,
    *,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[Any]:
    """locate -> decode. The only element read path."""
    _header, ppq, stream = _open(path.read_bytes(), event_size_overrides)
    site = fmt.locate(stream, target)
    return fmt.decode(_scope(site), ppq)


def patch(
    path: Path,
    fmt: Format,
    target: Target,
    data: Sequence[Any],
    mode: Mode,
    *,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[Any]:
    """locate -> encode -> splice + fix chunk lengths -> re-read -> fmt.verify.

    The only write path. The new payload is ``encode`` over the kept items
    (all of them on merge, only the out-of-scope ``rest`` on replace) plus
    ``data``; the readback IS the contract - the saved file must decode to
    exactly what was spliced, or FlpError. Returns the target-scoped readback.
    """
    raw = bytearray(path.read_bytes())
    header, ppq, stream = _open(bytes(raw), event_size_overrides)
    base = 8 + len(header) + 8  # the FLdt payload's file offset

    site = fmt.locate(stream, target)
    kept_blob = site.payload if mode == "merge" else site.rest
    kept = fmt.decode(kept_blob, ppq) if kept_blob else []
    payload = fmt.encode([*kept, *data], ppq)

    event = site.prelude + frame_event(fmt.event_id, payload)
    raw[base + site.head : base + site.end] = event
    _bump_fldt_length(raw, len(event) - (site.end - site.head))
    path.write_bytes(bytes(raw))

    _, _, saved_stream = _open(path.read_bytes(), event_size_overrides)
    saved = fmt.locate(saved_stream, target)
    fmt.verify(fmt.decode(payload, ppq), fmt.decode(saved.payload or b"", ppq))
    return fmt.decode(_scope(saved), ppq)


def verify_identical(sent: Sequence[Any], readback: Sequence[Any], context: str) -> None:
    """The one readback contract: the saved element decodes to exactly what
    was sent - same items, same order. Raises FlpError naming the first drift."""
    if list(readback) == list(sent):
        return
    drift = next(
        (f"item {i}: sent {s!r}, read back {r!r}" for i, (s, r) in enumerate(zip(sent, readback)) if s != r),
        f"sent {len(sent)} items, read back {len(readback)}",
    )
    raise FlpError(f"readback of {context} does not match the write: {drift}")


def _scope(site: SpliceSite) -> bytes:
    return site.scoped if site.scoped is not None else (site.payload or b"")


def _open(data: bytes, overrides: Mapping[int, int] | None) -> tuple[bytes, int, Stream]:
    header, raw = chunks(data)
    return header, int.from_bytes(header[4:6], "little"), Stream(raw, overrides)


# -- the wire format ----------------------------------------------------------


def chunks(data: bytes) -> tuple[bytes, memoryview]:
    """Split FLhd header / FLdt event stream; raises FlpError on a non-FLP file.

    Layout: b"FLhd" + u32 len(6) + (u16 format, u16 nChannels, u16 ppq),
    then b"FLdt" + u32 len + the event stream.
    """
    if data[:4] != b"FLhd":
        raise FlpError("not an FLP file: no FLhd magic at offset 0")
    if len(data) < 8:
        raise FlpError(f"truncated FLhd chunk: file is only {len(data)} bytes")
    hd_len = int.from_bytes(data[4:8], "little")
    if hd_len < 6 or 8 + hd_len > len(data):
        raise FlpError(f"bad FLhd length {hd_len} at offset 4")
    header = data[8 : 8 + hd_len]
    # pyflp's FileFormat set: None_/Project/Score/Automation/ChannelState/
    # PluginState/GeneratorState/FXState/InsertState/Patcher.
    file_format = int.from_bytes(header[0:2], "little", signed=True)
    if file_format not in (-1, 0, 0x10, 24, 0x20, 0x30, 0x31, 0x32, 0x40, 0x50):
        raise FlpError(f"unknown FLP format {file_format} at offset 8")
    dt_off = 8 + hd_len
    if data[dt_off : dt_off + 4] != b"FLdt":
        raise FlpError(f"no FLdt magic at offset {dt_off}")
    if dt_off + 8 > len(data):
        raise FlpError(f"truncated FLdt chunk header at offset {dt_off}")
    dt_len = int.from_bytes(data[dt_off + 4 : dt_off + 8], "little")
    start = dt_off + 8
    if start + dt_len > len(data):
        raise FlpError(
            f"FLdt length {dt_len} at offset {dt_off + 4} overruns the file ({len(data)} bytes)"
        )
    return header, memoryview(data)[start : start + dt_len]


class Stream:
    """The FLdt event stream: slices like a memoryview, iterates as events.

    Event encoding: id < 64 -> 1 data byte, 64-127 -> 2, 128-191 -> 4,
    192+ -> varint length + payload, except the measured size-override table
    (resolved by detect.py; a capability profile's detected table wins over
    the built-in fallback). Iteration yields (event_id, payload_offset,
    payload_size), offsets relative to the stream; raises FlpError on
    truncation.
    """

    def __init__(self, raw: memoryview | bytes, size_overrides: Mapping[int, int] | None = None):
        self.raw = raw if isinstance(raw, memoryview) else memoryview(raw)
        self.size_overrides = resolve_size_overrides(size_overrides)

    def __getitem__(self, key):
        return self.raw[key]

    def __len__(self) -> int:
        return len(self.raw)

    def __iter__(self) -> Iterator[tuple[int, int, int]]:
        stream, overrides, end = self.raw, self.size_overrides, len(self.raw)
        pos = 0
        while pos < end:
            head = pos
            event_id = stream[pos]
            pos += 1
            if event_id in overrides:
                size = overrides[event_id]
            elif event_id < 192:
                size = 1 if event_id < 64 else 2 if event_id < 128 else 4
            else:
                size = 0
                shift = 0
                while True:
                    if pos >= end:
                        raise FlpError(f"truncated varint for event {event_id} at stream offset {head}")
                    byte = stream[pos]
                    pos += 1
                    size |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
            if pos + size > end:
                raise FlpError(
                    f"event {event_id} payload of {size} bytes at stream offset {head} "
                    f"overruns the stream ({end} bytes)"
                )
            yield event_id, pos, size
            pos += size

    def channel_events(self) -> Iterator[tuple[int | None, int, int, int, int]]:
        """Iterate events with rack-channel attribution: yields (channel,
        event_id, head, off, size), ``channel`` None outside the channel
        section. The rule (corpus-verified on all 164 bundled projects):
        CHANNEL 0 IS IMPLICIT - a channel-scoped event with no channel open
        yet opens channel 0 (stock 'Basic with limiter' does this; found
        live); New(64)=N opens/switches to channel N; the first mixer event
        ends attribution (slot plugin names are not channel names)."""
        current: int | None = None
        mixer_seen = False
        prev_end = 0
        for event_id, off, size in self:
            head = prev_end
            prev_end = off + size
            channel: int | None = None
            if event_id == EVENT_CHANNEL_NEW and size == 2 and not mixer_seen:
                current = channel = int.from_bytes(self.raw[off : off + size], "little")
            elif event_id == EVENT_MIXER_FLAGS:
                mixer_seen = True
                current = None
            elif is_channel_scoped(event_id) and not mixer_seen:
                current = current if current is not None else 0
                channel = current
            yield channel, event_id, head, off, size


def frame_event(event_id: int, payload: bytes) -> bytes:
    """Frame one event: fixed-size payload below id 192, else a varint length
    (7 bits per byte, high bit = continue) then the payload."""
    if event_id < 192:
        return bytes([event_id]) + payload
    length = bytearray()
    n = len(payload)
    while True:
        length.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            break
    return bytes([event_id]) + bytes(length) + payload


def _bump_fldt_length(data: bytearray, delta: int) -> None:
    hd_len = int.from_bytes(data[4:8], "little")
    dt_len_off = 8 + hd_len + 4
    old_len = int.from_bytes(data[dt_len_off : dt_len_off + 4], "little")
    data[dt_len_off : dt_len_off + 4] = (old_len + delta).to_bytes(4, "little")
