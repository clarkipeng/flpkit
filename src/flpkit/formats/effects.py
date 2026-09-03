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
_STATE = 213
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
    """The proven empty Master slot 0 insertion, expressed as a Format."""

    name = "effects"
    event_id = _INTERNAL_NAME

    def __init__(self, *, adding: bool = False):
        self._adding = adding

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        if target.insert != 0:
            raise FlpError("effect add is only FL-authored for Master insert 0")
        events = _events(stream)
        insert = next(
            (
                index
                for index, (event_id, _head, _end, payload) in enumerate(events)
                if event_id == EVENT_MIXER_FLAGS and len(payload) == 12
                and int.from_bytes(payload[:4], "little") == target.insert
            ),
            None,
        )
        if insert is None:
            raise FlpError(f"no mixer insert {target.insert}")
        for index, (event_id, _head, _end, payload) in enumerate(events[insert + 1 :], insert + 1):
            if event_id == EVENT_MIXER_FLAGS:
                break
            if event_id != _SLOT or payload != bytes(2):
                continue
            next_event = events[index + 1] if index + 1 < len(events) else None
            if next_event is not None and next_event[0] == _SLOT:
                return SpliceSite(next_event[1], next_event[1], None)
            if next_event is not None and next_event[0] == _INTERNAL_NAME:
                effect_head = next_event[1]
                effect_end = _effect_end(events, index + 1)
                if self._adding:
                    raise FlpError("mixer insert 0 has no confirmed empty slot 0")
                return SpliceSite(effect_head, effect_end, bytes(stream[effect_head:effect_end]))
            break
        raise FlpError("mixer insert 0 has no confirmed empty slot 0")

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
        return reference.chunk

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
            name = events[0][3].decode("utf-16-le").rstrip("\0")
            end = _effect_end(events, 0)
            return [Effect(name, blob[:end])]
        except UnicodeDecodeError as error:
            raise FlpError("effect name is not valid UTF-16-LE") from error

    def verify(self, sent: Sequence[Effect], readback: Sequence[Effect]) -> None:
        verify_identical(sent, readback, "the captured effect reference")


def _events(stream: Stream) -> list[tuple[int, int, int, bytes]]:
    """Fully decoded events with their framing bounds."""
    events = []
    head = 0
    for event_id, offset, size in stream:
        end = offset + size
        events.append((event_id, head, end, bytes(stream[offset:end])))
        head = end
    return events


def _effect_end(events: Sequence[tuple[int, int, int, bytes]], name_index: int) -> int:
    if name_index + 1 >= len(events) or events[name_index + 1][0] != _WRAPPER:
        raise FlpError("effect tuple has no plugin wrapper event")
    state = next(
        (
            event
            for event in events[name_index + 2 :]
            if event[0] in (_STATE, _INTERNAL_NAME, EVENT_MIXER_FLAGS)
        ),
        None,
    )
    if state is not None and state[0] == _STATE:
        return state[2]
    return events[name_index + 1][2]
