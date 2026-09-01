"""The one confirmed mixer-effect add: Parametric EQ 2 on an empty master slot.

The FL-authored minimal pair establishes this exact 519-byte chunk.  The
plugin state is opaque, so this format deliberately exposes no general plugin
encoder: callers get a refusal for every name, insert, or slot shape not
proved by that pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from flpkit.codec import EVENT_MIXER_FLAGS, FlpError, Stream

_INTERNAL_NAME = 201
_WRAPPER = 212
_USER_NAME = 203
_STATE = 213
_SLOT = 98
PluginKind = Literal["native", "vst"]

# FL 26.1.5, authored 2026-09-01: Empty master slot 0 -> add Fruity
# Parametric EQ 2 -> save.  This is the complete event framing, not a
# synthesized plugin state.
_PARAMETRIC_EQ_2_CHUNK = bytes.fromhex(
    "c92e460072007500690074007900200050006100720061006d0065007400720069006300200045005100200032000000"
    "d434000000000100000002000000000000004001000000000000000000000000000000000000540000007e000000bc0200005e010000"
    "cb2e460072007500690074007900200050006100720061006d0065007400720069006300200045005100200032000000"
    "9b0000000080485156002900"
    "d5e2020800000000000000000000000000000000000000000000000000000000000000ab2a00001c4700008e63000000800000729c0000e4b8000055d50000bc9c00004463000044630000446300004463000044630000bc9c000005000000060000000600000006000000060000000600000007000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ab2a00001c4700008e63000000800000729c0000e4b8000055d50000bc9c00004463000044630000446300004463000044630000bc9c000005000000060000000600000006000000060000000600000007000000000000000000000000000000000000000000000000000000000000000000000003000000000000000102000000010000000200000000000000407f0000000100000000000000030000000100000002000000020000000200000000000000"
)


@dataclass(frozen=True)
class PluginReference:
    """One FL-authored opaque plugin record, indexed by identity and kind."""

    name: str
    kind: PluginKind
    chunk: bytes


@dataclass(frozen=True)
class PluginDatabase:
    """The only source of plugin bytes; a missing reference is an honest refusal."""

    references: tuple[PluginReference, ...]

    def reference(self, name: str, kind: PluginKind) -> PluginReference:
        for reference in self.references:
            if reference.name == name and reference.kind == kind:
                return reference
        raise FlpError(f"no FL-authored {kind} default-state reference for plugin {name!r}")


@dataclass(frozen=True)
class Effect:
    """A confirmed mixer-effect instance; state stays opaque and byte-faithful."""

    name: str
    chunk: bytes


DEFAULT_PLUGIN_DATABASE = PluginDatabase(
    (PluginReference("Fruity Parametric EQ 2", "native", _PARAMETRIC_EQ_2_CHUNK),)
)


class EffectFormat:
    """Locate, encode, decode, and verify the one FL-authored add-effect rule."""

    def locate(self, stream: Stream, insert_index: int) -> int:
        """Return the empty slot-0 insertion head for the named mixer insert.

        Insert ids are carried in the first u32 of event 236.  Only an empty
        slot zero is proven: its marker must precede slot one with no plugin
        tuple between them.  Anything else is a different format rule.
        """
        events = _events(stream)
        target = next(
            (
                index
                for index, (event_id, _head, _end, payload) in enumerate(events)
                if event_id == EVENT_MIXER_FLAGS and len(payload) == 12
                and int.from_bytes(payload[:4], "little") == insert_index
            ),
            None,
        )
        if target is None:
            raise IndexError(f"no mixer insert {insert_index}")
        for index, (event_id, head, _end, payload) in enumerate(events[target + 1 :], target + 1):
            if event_id == EVENT_MIXER_FLAGS:
                break
            if event_id == _SLOT and payload == bytes(2):
                if index + 1 < len(events) and events[index + 1][0] == _SLOT:
                    return events[index + 1][1]
                break
        raise FlpError(f"mixer insert {insert_index} has no confirmed empty slot 0")

    def encode(self, reference: PluginReference) -> bytes:
        return reference.chunk

    def decode(self, stream: Stream, insert_index: int) -> list[Effect]:
        events = _events(stream)
        active = False
        effects: list[Effect] = []
        for index, (event_id, head, end, payload) in enumerate(events):
            if event_id == EVENT_MIXER_FLAGS:
                if active:
                    break
                active = len(payload) == 12 and int.from_bytes(payload[:4], "little") == insert_index
                continue
            if not active or event_id != _INTERNAL_NAME or index + 1 >= len(events):
                continue
            wrapper, _wrapper_head, _wrapper_end, _wrapper_payload = events[index + 1]
            if wrapper != _WRAPPER:
                continue
            name = payload.decode("utf-16-le").rstrip("\0")
            # A state event is optional in the general observed tuple.  The
            # byte range through it is the only stable representation here.
            state = next(
                (
                    candidate
                    for candidate in events[index + 2 :]
                    if candidate[0] in (_STATE, _INTERNAL_NAME, EVENT_MIXER_FLAGS)
                ),
                None,
            )
            end = state[2] if state is not None and state[0] == _STATE else events[index + 1][2]
            effects.append(Effect(name, bytes(stream[head:end])))
        return effects

    def verify(self, reference: PluginReference, effects: list[Effect]) -> None:
        if any(effect.name == reference.name and effect.chunk == reference.chunk for effect in effects):
            return
        raise FlpError(f"readback has no byte-identical {reference.name!r} effect chunk")


def _events(stream: Stream) -> list[tuple[int, int, int, bytes]]:
    """Fully decoded events with framing heads, from flpkit's canonical walker."""
    events = []
    head = 0
    for event_id, offset, size in stream:
        end = offset + size
        events.append((event_id, head, end, bytes(stream[offset:end])))
        head = end
    return events

