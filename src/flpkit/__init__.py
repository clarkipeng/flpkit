"""Read and write FL Studio .flp project files, without FL Studio.

flpkit is a small, dependency-free library for the undocumented FLP format:

- **Reading**: ppq, tempo (modern and legacy events), channels (names + mix
  levels across four format generations), notes per pattern/channel, playlist
  clips per arrangement, and automation points per type-5 channel.
- **Writing**: raw byte surgery through ONE engine - ``codec.patch`` locates
  an element, encodes the new bytes, splices them (fixing chunk lengths), and
  verifies by re-reading the saved file. FL Studio rejects whole-file
  reserialization by third-party writers (verified live 2026-08-27), so a
  write patches exactly the bytes that express the change, never more.

Each element type is a Format spec under ``formats/`` (notes, playlist,
automation, levels, tempo, effects): reading one module tells you everything about
that element. Every constant is a reverse-engineered fact carrying its
evidence, verified against real FL Studio 2026 (macOS) and a corpus of 164
FL-authored projects; the live verification harness lives in the parent
project, https://github.com/origami-research/fl-studio-mcp.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

# ruff: noqa: F401
from . import codec, detect, formats
from .codec import (
    EVENT_CHANNEL_AUTOMATION,
    EVENT_CHANNEL_LEVELS,
    EVENT_CHANNEL_NEW,
    EVENT_CHANNEL_TYPE,
    EVENT_MIXER_FLAGS,
    EVENT_NAME_INTERNAL,
    EVENT_NAME_LEGACY,
    EVENT_NAME_USER,
    EVENT_PAN_BYTE,
    EVENT_PAN_WORD,
    EVENT_VOL_BYTE,
    EVENT_VOL_WORD,
    FlpError,
    Format,
    Mode,
    SpliceSite,
    Target,
)
from .codec import chunks as _chunks
from .detect import (
    EVENT_SIZE_OVERRIDES_FALLBACK,
    PATTERN_INDEX_BASE,
    PLAYLIST_STRIDE_MAX,
    PLAYLIST_STRIDE_MIN,
    PLAYLIST_TRACK_SPACE,
    _log_size_override_fallback,
)
from .formats import (
    AutomationFormat,
    EffectFormat,
    LevelsFormat,
    NotesFormat,
    PlaylistFormat,
    ShowInfoFormat,
    TempoFormat,
)
from .formats.automation import (
    AUTOMATION_TAIL_DEFAULT,
    CHANNEL_TYPE_AUTOMATION,
    AutomationPointLike,
    AutomationPointSpec,
    FlpAutomationPoint,
)
from .formats.effects import (
    DEFAULT_PLUGIN_DATABASE,
    Effect,
    PluginDatabase,
    PluginReference,
)
from .formats.levels import LEVEL_MAX, PAN_CENTRE, VOLUME_DEFAULT, Levels
from .formats.notes import (
    EVENT_CUR_GROUP_ID,
    EVENT_PATTERN_NEW,
    EVENT_PATTERN_NOTES,
    FINE_PITCH_CENTER,
    MOD_DEFAULT,
    NOTE_FLAGS_DEFAULT,
    NOTE_PAN_CENTER,
    NOTE_SIZE,
    NOTE_STRUCT,
    RELEASE_DEFAULT,
    NoteLike,
    NoteSpec,
)
from .formats.playlist import EVENT_ARRANGEMENT_NEW, EVENT_PLAYLIST, ClipLike, ClipSpec
from .formats.show_info import EVENT_SHOW_INFO
from .formats.tempo import EVENT_TEMPO, EVENT_TEMPO_COARSE, EVENT_TEMPO_FINE
from .plugins import (
    EVENT_PLUGIN_DATA,
    EVENT_PLUGIN_WRAPPER,
    WRAPPER_INTERNAL_NAME,
    FlpPlugin,
    FlpVst,
    vst_identity,
)

log = logging.getLogger("flpkit")

NoteWriteMode = Mode
EVENT_VERSION = 199  # ascii "major.minor..."; >= 11.5 -> text events are UTF-16-LE
EVENT_LICENSED = 28  # 1 byte: 1 when the project was last saved by a registered FL (pyflp ProjectID.Licensed)
EVENT_TITLE = 194  # project title text (pyflp ProjectID.Title)
EVENT_SAMPLE_PATH = 196  # a sampler/audio-clip channel's sample path (pyflp ChannelID.SamplePath)
EVENT_SLOT_INDEX = 98  # closes a mixer slot; low byte = slot number (see formats/effects.py)
PPQ_DEFAULT = 96
__version__ = version("flpkit")


def _events(stream, event_size_overrides: Mapping[int, int] | None = None):
    return iter(codec.Stream(stream, event_size_overrides))


# Private v0.5.0 aliases still imported by mcp-server's tests/e2e/matrix.py;
# the integration manager retires them when matrix.py ports to codec/detect.
_is_channel_scoped = codec.is_channel_scoped
_playlist_stride_fits = detect._stride_fits


def _splice(data: bytearray, at: int, event: bytes) -> None:
    """Insert event bytes at ``at`` and bump the FLdt chunk length to match."""
    data[at:at] = event
    codec._bump_fldt_length(data, len(event))


# -- the decoded read-only project view ---------------------------------------


@dataclass(frozen=True)
class FlpNote:
    """One decoded note, in file units (ticks)."""

    position: int  # ticks
    length: int  # ticks
    key: int  # MIDI 0-127
    channel: int  # rack channel index
    velocity: int  # 0-127
    pan: int  # 0-128, 64 = centre
    # Which pattern the note came from (FL patterns are 1-based; 0 means the
    # blob preceded any PatternID.New event, so the file did not attribute it).
    pattern: int = 0


@dataclass(frozen=True)
class FlpChannel:
    """One decoded channel: identity plus mix levels in raw file units."""

    index: int
    name: str
    volume: int  # 0..LEVEL_MAX, VOLUME_DEFAULT if the file omits it
    pan: int  # 0..LEVEL_MAX, PAN_CENTRE if omitted
    pitch_semitones: int
    kind: int | None = None  # EVENT_CHANNEL_TYPE byte; None when the file omits it
    automation: tuple[FlpAutomationPoint, ...] = ()
    plugin: FlpPlugin | None = None  # the generator plugin; None for samplers and audio clips
    sample_path: str = ""  # the sample a sampler/audio-clip channel plays, as FL stored it; "" when none

    @property
    def is_automation(self) -> bool:
        return self.kind == CHANNEL_TYPE_AUTOMATION


@dataclass(frozen=True)
class FlpMixerEffect:
    """One plugin loaded in a mixer insert's effect slot."""

    insert: int  # 0 = Master
    slot: int  # 0-based slot index within the insert
    plugin: FlpPlugin


@dataclass(frozen=True)
class FlpPlaylistItem:
    """One clip on an arrangement's playlist, in file units (ticks)."""

    position: int  # ticks from song start
    length: int  # ticks
    track: int  # 1-based playlist track number
    pattern: int | None  # set for pattern clips
    channel: int | None  # set for audio/automation channel clips
    group: int = 0
    arrangement: int = 0


@dataclass(frozen=True)
class ChannelLevels:
    """A channel's mix state in tool units - the readback of set_channel_levels."""

    channel: int
    volume: float  # 0..1
    pan: float  # -1..1, 0 = centre
    pitch_semitones: int


@dataclass(frozen=True)
class FlpProject:
    """Decoded read-only view of a project file."""

    ppq: int
    tempo: float | None  # None = FL's default (the file omits the event)
    channels: tuple[FlpChannel, ...]
    notes: tuple[FlpNote, ...]  # all patterns; filter by pattern via notes_at
    playlist: tuple[FlpPlaylistItem, ...] = ()  # all arrangements' clips
    fl_version: str = ""  # the FL Studio version that last saved the file, e.g. "26.1.6.5639"; "" when absent
    licensed: bool | None = None  # saved by a registered FL; None when the file omits the flag
    title: str = ""
    mixer: tuple[FlpMixerEffect, ...] = ()  # every loaded effect slot, by insert then slot
    mixer_slots: tuple[tuple[int, int], ...] = ()  # every (insert, slot) the file has, loaded or empty
    # Saved by an unregistered (trial) FL: it scrambles the saved values, and a trial
    # FL then refuses to reopen the file (it opens an empty project behind a Buy
    # dialog). Detected as: not licensed, and most slot-index events (98) out of
    # range. Evidence: all 40 FL 26.1.6 trial saves checked have every slot index
    # scrambled; none of the 86 unlicensed projects FL 2026 bundles has one.
    trial_saved: bool = False

    def empty_slots(self, insert: int) -> list[int]:
        """The insert's slots with no plugin loaded, in order."""
        loaded = {e.slot for e in self.mixer if e.insert == insert}
        return [slot for i, slot in self.mixer_slots if i == insert and slot not in loaded]

    def notes_in(self, pattern: int, channel: int | None = None) -> list[FlpNote]:
        """Notes in one pattern, optionally one channel."""
        return [
            note
            for note in self.notes
            if note.pattern == pattern and (channel is None or note.channel == channel)
        ]


def _flp_note(note: formats.notes.Note, ppq: int, pattern: int) -> FlpNote:
    return FlpNote(
        position=round(note.start * ppq),
        length=round(note.length * ppq),
        key=note.key,
        channel=note.channel,
        velocity=round(note.velocity * 127),
        pan=round(note.pan * 128),
        pattern=pattern,
    )


def _decode_notes(blob: bytes) -> list[FlpNote]:
    return [_flp_note(n, PPQ_DEFAULT, 0) for n in NotesFormat().decode(blob, PPQ_DEFAULT)]


# -- the project reader --------------------------------------------------------


def read(path: Path, *, event_size_overrides: Mapping[int, int] | None = None) -> FlpProject:
    """Parse the file. Raises FlpError naming the byte offset on bad input.

    ``event_size_overrides`` maps event ids to their measured payload sizes
    where FL breaks the classic range rule (a capability profile supplies it;
    None falls back to the built-in FL-2026 table, logged once per process).
    """
    header, raw = _chunks(path.read_bytes())
    stream = codec.Stream(raw, event_size_overrides)
    ppq = int.from_bytes(header[4:6], "little")
    if ppq == 0:
        raise FlpError("FLhd header declares PPQ 0 at offset 12")
    # CHANNEL 0 IS IMPLICIT (see Stream.channel_events). Verified corpus-wide:
    # FLhd nChannels == |{0} union {New payloads}| on all 164 bundled projects.
    n_channels = int.from_bytes(header[2:4], "little")

    tempo: float | None = None
    coarse: int | None = None  # legacy tempo pair, used only when 156 is absent
    fine = 0
    unicode_text = False  # flips when FLVersion says >= 11.5
    channel_map: dict[int, dict] = {}
    pattern = 0  # 0 = before any PatternID.New (A9: stock files do this)
    notes: list[FlpNote] = []
    arrangement = 0
    playlist: list[FlpPlaylistItem] = []
    fl_version = ""
    licensed: bool | None = None
    title = ""
    open_channel: int | None = None  # the rack channel later unscoped plugin/sample events belong to
    mixer = _MixerWalk()

    for channel, event_id, _head, off, size in stream.channel_events():
        payload = bytes(stream[off : off + size])
        if event_id == EVENT_MIXER_FLAGS:
            open_channel = None
        if channel is not None:
            open_channel = channel
            fields = channel_map.setdefault(channel, {"index": channel})
            if event_id != EVENT_CHANNEL_NEW:
                _apply_channel_event(fields, event_id, payload, unicode_text, off, ppq)
        elif open_channel is not None and event_id in _CHANNEL_PLUGIN_EVENTS:
            # Sample paths and plugin state are not channel-SCOPED (they never open
            # channel 0 on their own), but inside an open channel block they are its.
            _apply_channel_event(channel_map[open_channel], event_id, payload, unicode_text, off, ppq)
        elif mixer.started or event_id == EVENT_MIXER_FLAGS:
            mixer.event(event_id, payload, unicode_text, off)
        elif event_id == EVENT_VERSION:
            unicode_text = _version_is_unicode(payload, off)
            fl_version = payload.decode("ascii").rstrip("\0")
        elif event_id == EVENT_LICENSED and size == 1:
            licensed = payload[0] == 1
        elif event_id == EVENT_TITLE:
            title = _text(payload, unicode_text, off)
        elif event_id == EVENT_TEMPO and size == 4:
            tempo = int.from_bytes(payload, "little") / 1000
        elif event_id == EVENT_TEMPO_COARSE and size == 2 and coarse is None:
            coarse = int.from_bytes(payload, "little")
        elif event_id == EVENT_TEMPO_FINE and size == 2 and fine == 0:
            fine = int.from_bytes(payload, "little")
        elif event_id == EVENT_PATTERN_NEW and size == 2:
            pattern = int.from_bytes(payload, "little")
        elif event_id == EVENT_ARRANGEMENT_NEW and size == 2:
            arrangement = int.from_bytes(payload, "little")
        elif event_id == EVENT_PLAYLIST:
            try:
                playlist.extend(
                    FlpPlaylistItem(
                        position=round(c.start * ppq), length=round(c.length * ppq),
                        track=c.track, pattern=c.pattern, channel=c.channel,
                        group=c.group, arrangement=arrangement,
                    )
                    for c in PlaylistFormat().decode(payload, ppq)
                )
            except FlpError as exc:
                log.warning("%s: skipping playlist event at offset %d: %s", path.name, off, exc)
        elif event_id == EVENT_PATTERN_NOTES:
            if size % NOTE_SIZE:
                log.warning(
                    "%s: skipping notes event of size %d (not a multiple of %d) "
                    "at stream offset %d",
                    path.name, size, NOTE_SIZE, off,
                )
                continue
            notes.extend(_flp_note(n, ppq, pattern) for n in NotesFormat().decode(payload, ppq))

    mixer.flush()
    if tempo is None and coarse is not None:
        tempo = coarse + fine / 1000  # pre-156 files store tempo as a word pair
    if channel_map and len(channel_map) != n_channels:
        log.warning(
            "%s: decoded %d channels but the FLhd header says %d",
            path.name, len(channel_map), n_channels,
        )
    return FlpProject(
        ppq=ppq,
        tempo=tempo,
        channels=tuple(_build_channel(channel_map[iid]) for iid in sorted(channel_map)),
        notes=tuple(notes),
        playlist=tuple(playlist),
        fl_version=fl_version,
        licensed=licensed,
        title=title,
        mixer=tuple(mixer.effects),
        mixer_slots=tuple(mixer.slots),
        trial_saved=licensed is False and mixer.scrambled * 2 > mixer.slot_events > 0,
    )


class _MixerWalk:
    """Attribute mixer-section plugin tuples to (insert, slot).

    The layout facts and their evidence are formats/effects.py's EffectFormat:
    the n-th InsertID.Flags (236) opens insert n (0 = Master), and a slot's
    plugin tuple comes BEFORE the SlotID.Index (98) that closes it, whose low
    byte is the slot number. Files from before slot events (FL 10 era) carry no
    98s; there a tuple's slot is the one its wrapper event (212) names.
    """

    def __init__(self) -> None:
        self.started = False
        self.effects: list[FlpMixerEffect] = []
        self.slots: list[tuple[int, int]] = []
        self.slot_events = 0
        self.scrambled = 0  # slot-index values no FL slot layout uses (see FlpProject.trial_saved)
        self._insert = -1
        self._pending: dict | None = None

    def event(self, event_id: int, payload: bytes, unicode_text: bool, offset: int) -> None:
        if event_id == EVENT_MIXER_FLAGS:
            self.flush()
            self.started = True
            self._insert += 1  # the n-th InsertID.Flags opens insert n (its u32 is flags)
        elif event_id == EVENT_SLOT_INDEX and len(payload) == 2 and self._insert >= 0:
            self.slot_events += 1
            self.scrambled += int.from_bytes(payload, "little") > 0x3F
            self.slots.append((self._insert, payload[0]))
            self._close(payload[0])
        elif event_id == EVENT_NAME_INTERNAL:
            self.flush()
            self._pending = {"name_internal": _text(payload, unicode_text, offset)}
        elif self._pending is None:
            return
        elif event_id == EVENT_NAME_USER:
            self._pending["name_user"] = _text(payload, unicode_text, offset)
        elif event_id == EVENT_PLUGIN_WRAPPER and len(payload) >= 8:
            self._pending["wrapper_slot"] = payload[4]
        elif event_id == EVENT_PLUGIN_DATA:
            self._pending["vst"] = vst_identity(payload)

    def flush(self) -> None:
        """A tuple no slot event closed (pre-slot-event files): its wrapper names the slot."""
        if self._pending is not None and "wrapper_slot" in self._pending:
            self._close(self._pending["wrapper_slot"])
        self._pending = None

    def _close(self, slot: int) -> None:
        plugin = _plugin(self._pending) if self._pending is not None else None
        if plugin is not None:
            self.effects.append(FlpMixerEffect(self._insert, slot, plugin))
        self._pending = None


def _plugin(fields: dict) -> FlpPlugin | None:
    internal = fields.get("name_internal", "")
    if not internal:
        return None
    vst = fields.get("vst") if internal == WRAPPER_INTERNAL_NAME else None
    return FlpPlugin(internal_name=internal, name=fields.get("name_user", ""), vst=vst)


# Events that belong to the open rack channel without being channel-scoped.
_CHANNEL_PLUGIN_EVENTS = frozenset((EVENT_SAMPLE_PATH, EVENT_PLUGIN_DATA))


# Raw-field keys a channel block accumulates before _build_channel resolves them.
_CHANNEL_EVENT_FIELDS = {
    EVENT_NAME_USER: "name_user",
    EVENT_NAME_LEGACY: "name_legacy",
    EVENT_NAME_INTERNAL: "name_internal",
    EVENT_VOL_WORD: "vol_word",
    EVENT_PAN_WORD: "pan_word",
    EVENT_VOL_BYTE: "vol_byte",
    EVENT_PAN_BYTE: "pan_byte",
}


def _apply_channel_event(
    fields: dict, event_id: int, payload: bytes, unicode_text: bool, offset: int, ppq: int
) -> None:
    """Decode one channel-scoped event into the channel's raw-field dict."""
    if event_id == EVENT_CHANNEL_LEVELS:
        if len(payload) != 24:
            log.warning(
                "skipping Levels event of size %d (expected 24) at stream offset %d",
                len(payload), offset,
            )
            return
        pan, volume, pitch = struct.unpack_from("<iIi", payload)
        fields.update(levels_pan=pan, levels_volume=volume, levels_pitch=pitch)
    elif event_id in (EVENT_NAME_USER, EVENT_NAME_LEGACY, EVENT_NAME_INTERNAL):
        fields[_CHANNEL_EVENT_FIELDS[event_id]] = _text(payload, unicode_text, offset)
    elif event_id in (EVENT_VOL_WORD, EVENT_PAN_WORD, EVENT_VOL_BYTE, EVENT_PAN_BYTE):
        fields[_CHANNEL_EVENT_FIELDS[event_id]] = int.from_bytes(payload, "little")
    elif event_id == EVENT_CHANNEL_TYPE and len(payload) == 1:
        fields["kind"] = payload[0]
    elif event_id == EVENT_CHANNEL_AUTOMATION:
        fields["automation"] = tuple(AutomationFormat().decode(payload, ppq))
    elif event_id == EVENT_SAMPLE_PATH:
        fields["sample_path"] = _text(payload, unicode_text, offset)
    elif event_id == EVENT_PLUGIN_DATA:
        fields["vst"] = vst_identity(payload)


def _build_channel(fields: dict) -> FlpChannel:
    """Resolve a channel's raw fields: Levels wins over the legacy word/byte
    events; report a default only when the file stored nothing at all. Name
    priority mirrors pyflp's display_name: user rename, else legacy name event,
    else the plugin's internal name."""
    name = fields["name_user"] if "name_user" in fields else fields.get("name_legacy", "")
    name = name or fields.get("name_internal", "")
    volume = fields.get("levels_volume", fields.get("vol_word", fields.get("vol_byte")))
    pan = fields.get("levels_pan", fields.get("pan_word", fields.get("pan_byte")))
    kind = fields.get("kind")
    return FlpChannel(
        index=int(fields["index"]),
        name=str(name),
        volume=int(volume) if volume is not None else VOLUME_DEFAULT,
        pan=int(pan) if pan is not None else PAN_CENTRE,
        pitch_semitones=int(fields.get("levels_pitch", 0)),
        kind=int(kind) if kind is not None else None,
        automation=fields.get("automation", ()),
        plugin=_plugin(fields),
        sample_path=str(fields.get("sample_path", "")),
    )


def _version_is_unicode(payload: bytes, offset: int) -> bool:
    """Decode an FLVersion (ascii) payload; True when >= 11.5, the version at
    which FL switched text events to UTF-16-LE."""
    try:
        text = payload.decode("ascii").rstrip("\0")
        parts = [int(part) for part in text.split(".")]
    except (UnicodeDecodeError, ValueError) as exc:
        raise FlpError(f"malformed FLVersion event at stream offset {offset}: {exc}") from exc
    return parts[:2] >= [11, 5]


def _text(payload: bytes, unicode_text: bool, offset: int) -> str:
    """Decode one text event payload; NUL-terminated, encoding per FLVersion."""
    encoding = "utf-16-le" if unicode_text else "latin-1"
    try:
        return payload.decode(encoding).rstrip("\0")
    except UnicodeDecodeError as exc:
        raise FlpError(f"undecodable {encoding} text event at stream offset {offset}: {exc}") from exc


# -- the tool-unit write/read surface (thin shims over the ONE codec engine) ---


def notes_at(
    path: Path,
    pattern: int,
    channel: int,
    *,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[FlpNote]:
    """The notes actually in the SAVED file - THE readback for verification."""
    return read(path, event_size_overrides=event_size_overrides).notes_in(pattern, channel)


def write_notes(
    path: Path,
    notes: Sequence[NoteLike],
    *,
    pattern: int,
    channel: int,
    mode: Mode,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[FlpNote]:
    """Splice the pattern's notes blob; return ``notes_at`` of the RESULT.
    ``mode="replace"`` is scoped to the target channel (see formats/notes.py);
    raises FlpError when the readback does not match what was spliced."""
    codec.patch(
        path, NotesFormat(), Target(pattern=pattern, channel=channel), notes, mode,
        event_size_overrides=event_size_overrides,
    )
    return notes_at(path, pattern, channel, event_size_overrides=event_size_overrides)


def write_playlist(
    path: Path,
    clips: Sequence[ClipLike],
    *,
    mode: Mode = "merge",
    arrangement: int = 0,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[FlpPlaylistItem]:
    """Splice pattern clips into the arrangement's playlist event; return the
    arrangement's decoded playlist AFTER the write. ``mode="replace"``
    replaces the WHOLE arrangement playlist (see formats/playlist.py)."""
    codec.patch(
        path, PlaylistFormat(), Target(arrangement=arrangement), clips, mode,
        event_size_overrides=event_size_overrides,
    )
    project = read(path, event_size_overrides=event_size_overrides)
    return [i for i in project.playlist if i.arrangement == arrangement]


def write_automation(
    path: Path,
    channel: int,
    points: Sequence[AutomationPointLike],
    *,
    mode: str = "replace",
    event_size_overrides: Mapping[int, int] | None = None,
) -> int:
    """Replace the points inside an EXISTING automation channel's blob (see
    formats/automation.py); return the number of points the saved file holds.
    Feeding a channel's decoded points straight back is readback-identical;
    FL-authored blobs are byte-identical in corpus tests."""
    if mode != "replace":
        raise FlpError(f"unsupported mode {mode!r}; write_automation only replaces")
    saved = codec.patch(
        path, AutomationFormat(), Target(channel=channel), points, mode,
        event_size_overrides=event_size_overrides,
    )
    return len(saved)


def effects_at(
    path: Path,
    insert_index: int,
    *,
    slot: int,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[Effect]:
    """The effect in one mixer slot of the saved file: ``[effect]``, or ``[]``
    when the slot is empty. ``read(path).mixer`` lists every loaded slot."""
    return codec.read(
        path, EffectFormat(), Target(insert=insert_index, slot=slot),
        event_size_overrides=event_size_overrides,
    )


def add_effect(
    path: Path,
    insert_index: int,
    plugin_name: str,
    *,
    slot: int,
    database: PluginDatabase = DEFAULT_PLUGIN_DATABASE,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[Effect]:
    """Splice an FL-authored effect reference into an EMPTY slot of the Master
    insert, then reparse and verify it. Returns the slot's readback.

    The reference is captured FL output (``data/plugins``), placed with its
    wrapper's insert/slot rewritten to the target, the way FL writes them.
    Unknown names, other inserts and occupied slots refuse before the file
    changes.
    """
    reference = database.reference(plugin_name)
    return codec.patch(
        path, EffectFormat(adding=True), Target(insert=insert_index, slot=slot), [reference],
        "replace", event_size_overrides=event_size_overrides,
    )


def set_channel_levels(
    path: Path,
    channel: int,
    *,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
    event_size_overrides: Mapping[int, int] | None = None,
) -> ChannelLevels:
    """Patch the channel's levels; None leaves a value alone (filled from the
    file's current Levels, so untouched fields rewrite byte-identically).
    Returns the readback. See formats/levels.py for the layout and refusals."""
    fmt, target = LevelsFormat(), Target(channel=channel)
    (current,) = codec.read(path, fmt, target, event_size_overrides=event_size_overrides)
    merged = Levels(
        volume=volume if volume is not None else current.volume,
        pan=pan if pan is not None else current.pan,
        pitch_semitones=pitch_semitones if pitch_semitones is not None else current.pitch_semitones,
        tail=current.tail,
    )
    (saved,) = codec.patch(
        path, fmt, target, [merged], "replace", event_size_overrides=event_size_overrides
    )
    return ChannelLevels(
        channel=channel, volume=saved.volume, pan=saved.pan,
        pitch_semitones=saved.pitch_semitones,
    )


def set_tempo(
    path: Path, bpm: float, *, event_size_overrides: Mapping[int, int] | None = None
) -> float:
    """Patch the tempo event in place, or APPEND one at the end of the event
    stream when FL omitted it (see formats/tempo.py). Returns the readback."""
    (stored,) = codec.patch(
        path, TempoFormat(), Target(), [bpm], "replace",
        event_size_overrides=event_size_overrides,
    )
    return stored


def hide_info_on_open(path: Path, *, event_size_overrides: Mapping[int, int] | None = None) -> bool:
    """Turn off the project's "show info on open" panel (see formats/show_info.py),
    which otherwise takes FL's foreground when the project loads. Returns True
    when the file changed; a file without the flag (FL's off) is left untouched."""
    try:
        (shown,) = codec.read(path, ShowInfoFormat(), Target(), event_size_overrides=event_size_overrides)
    except FlpError:
        return False
    if not shown:
        return False
    codec.patch(path, ShowInfoFormat(), Target(), [False], "replace", event_size_overrides=event_size_overrides)
    return True
