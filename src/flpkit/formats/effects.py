"""Captured mixer-effect references, spliced without interpreting plugin state."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from flpkit.codec import EVENT_MIXER_FLAGS, FlpError, SpliceSite, Stream, Target, verify_identical

_INTERNAL_NAME = 201
_WRAPPER = 212
_SLOT = 98
_RESOURCE_DIR = ("data", "plugins")


@dataclass(frozen=True)
class PluginReference:
    """One FL-authored opaque plugin tuple captured as data."""

    name: str
    chunk: bytes
    fl_build: str
    kind: str


class PluginDatabase:
    """Lazy captured-reference lookup, indexed by plugin name."""

    def __init__(self, references: Mapping[str, PluginReference] | None = None):
        self._references = dict(references or {})
        self._index: dict[str, str] | None = {} if references is not None else None

    def reference(self, name: str) -> PluginReference:
        if name in self._references:
            return self._references[name]
        if self._index is None:
            root = files("flpkit").joinpath(*_RESOURCE_DIR)
            self._index = json.loads(root.joinpath("index.json").read_text())
        filename = self._index.get(name)
        if filename is None:
            raise FlpError(f"no FL-authored default-state reference for effect {name!r}")
        record = json.loads(files("flpkit").joinpath(*_RESOURCE_DIR, filename).read_text())
        try:
            chunk = bytes.fromhex(record["chunk_hex"])
            reference = PluginReference(record["name"], chunk, record["fl_build"], record["kind"])
        except (KeyError, TypeError, ValueError) as error:
            raise FlpError(f"invalid captured plugin reference {filename!r}") from error
        if reference.name != name or sha256(chunk).hexdigest() != record.get("sha256"):
            raise FlpError(f"captured plugin reference {filename!r} failed its integrity check")
        self._references[name] = reference
        return reference


@dataclass(frozen=True)
class Effect:
    """A decoded effect tuple whose plugin state remains opaque."""

    name: str
    chunk: bytes


DEFAULT_PLUGIN_DATABASE = PluginDatabase()


class EffectFormat:
    """One mixer slot's plugin tuple: read it, or splice a captured reference
    into an empty slot.

    Facts, corpus-checked on FL Studio 2026's 164 bundled projects (FL's own
    wrappers agree on 3,544 of 3,545 slot plugins; the one left names insert 501):

    - Insert ``n`` of the mixer opens at the ``n``-th InsertID.Flags event
      (236, counting from 0 = Master). Its first u32 is flags, not the index.
    - Each slot is closed by a SlotID.Index event (98) whose low byte is the
      slot number; the slot's plugin tuple (201 internal name, 212 wrapper,
      optional 203 rename, 155/128/41, optional 213 state) comes BEFORE it.
      An empty slot is a 98 with no 201 since the previous slot or the insert's
      236. (FL's own save of "Parametric EQ 2 added to Master" put the tuple
      between ``98=0`` and ``98=1``: Master slot 1, its wrapper says ``0, 1``.)
    - The wrapper event's first two u32s are the insert and slot indices; a
      spliced reference gets them rewritten to its new place.
    """

    name = "effects"
    event_id = _INTERNAL_NAME

    def __init__(self, *, adding: bool = False):
        self._adding = adding
        self._target: Target | None = None

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        if target.insert is None or target.slot is None:
            raise FlpError("an effect is addressed by insert and slot")
        if self._adding and target.insert != 0:
            raise FlpError("effect add is only FL-authored for the Master insert (0)")
        self._target = target
        events = _events(stream)
        opens = [i for i, event in enumerate(events) if event[0] == EVENT_MIXER_FLAGS]
        if target.insert >= len(opens):
            raise FlpError(f"no mixer insert {target.insert}")
        first = opens[target.insert] + 1
        last = opens[target.insert + 1] if target.insert + 1 < len(opens) else len(events)
        slot_open = first
        for index in range(first, last):
            event_id, head, _end, payload = events[index]
            if event_id != _SLOT or len(payload) != 2:
                continue
            if payload[0] != target.slot:
                slot_open = index + 1
                continue
            plugin = next(
                (i for i in range(slot_open, index) if events[i][0] == _INTERNAL_NAME), None
            )
            if plugin is None:
                return SpliceSite(head, head, None)
            if self._adding:
                raise FlpError(f"mixer insert {target.insert} slot {target.slot} is not empty")
            effect_head = events[plugin][1]
            return SpliceSite(effect_head, head, bytes(stream[effect_head:head]))
        raise FlpError(f"mixer insert {target.insert} has no slot {target.slot}")

    def encode(self, data: Sequence[PluginReference], ppq: int) -> bytes:
        if len(data) != 1:
            raise FlpError(f"an effect insertion needs exactly one captured reference, got {len(data)}")
        (reference,) = data
        try:
            decoded = self.decode(reference.chunk, ppq)
        except (TypeError, AttributeError) as error:
            raise FlpError("effect reference has no valid captured chunk") from error
        if len(decoded) != 1 or decoded[0].name != reference.name:
            raise FlpError(f"captured chunk does not identify {reference.name!r}")
        # ``patch`` locates once more to verify its saved readback.  Occupancy
        # is only a pre-insertion requirement for this one-shot format.
        self._adding = False
        assert self._target is not None and self._target.insert is not None and self._target.slot is not None
        return _placed(reference.chunk, self._target.insert, self._target.slot)

    def frame(self, payload: bytes) -> bytes:
        """Plugin references are captured event tuples, not synthesized events."""
        return payload

    def decode(self, blob: bytes, ppq: int) -> list[Effect]:
        if not blob:
            return []
        try:
            events = _events(Stream(blob))
            if not events or events[0][0] != _INTERNAL_NAME:
                raise FlpError("effect tuple does not start with PluginID.InternalName")
            if len(events) < 2 or events[1][0] != _WRAPPER:
                raise FlpError("effect tuple has no plugin wrapper event")
            name = events[0][3].decode("utf-16-le").rstrip("\0")
            return [Effect(name, blob)]
        except UnicodeDecodeError as error:
            raise FlpError("effect name is not valid UTF-16-LE") from error

    def verify(self, sent: Sequence[Effect], readback: Sequence[Effect]) -> None:
        verify_identical(sent, readback, "the captured effect reference")


def _placed(chunk: bytes, insert: int, slot: int) -> bytes:
    """The captured tuple with its wrapper's insert and slot rewritten."""
    events = _events(Stream(chunk))
    _id, _head, end, payload = events[1]
    if len(payload) < 8:
        raise FlpError("effect wrapper event is too short to carry its insert and slot")
    at = end - len(payload)
    placed = bytearray(chunk)
    placed[at : at + 8] = insert.to_bytes(4, "little") + slot.to_bytes(4, "little")
    return bytes(placed)


def _events(stream: Stream) -> list[tuple[int, int, int, bytes]]:
    """Fully decoded events with their framing bounds."""
    events = []
    head = 0
    for event_id, offset, size in stream:
        end = offset + size
        events.append((event_id, head, end, bytes(stream[offset:end])))
        head = end
    return events
