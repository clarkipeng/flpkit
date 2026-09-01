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
automation, levels, tempo): reading one module tells you everything about
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
from .formats import AutomationFormat, EffectFormat, LevelsFormat, NotesFormat, PlaylistFormat, TempoFormat
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
    PluginKind,
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
from .formats.tempo import EVENT_TEMPO, EVENT_TEMPO_COARSE, EVENT_TEMPO_FINE

log = logging.getLogger("flpkit")

NoteWriteMode = Mode
EVENT_VERSION = 199  # ascii "major.minor..."; >= 11.5 -> text events are UTF-16-LE
PPQ_DEFAULT = 96


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

    @property
    def is_automation(self) -> bool:
        return self.kind == CHANNEL_TYPE_AUTOMATION


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

    for channel, event_id, _head, off, size in stream.channel_events():
        payload = bytes(stream[off : off + size])
        if channel is not None:
            fields = channel_map.setdefault(channel, {"index": channel})
            if event_id != EVENT_CHANNEL_NEW:
                _apply_channel_event(fields, event_id, payload, unicode_text, off, ppq)
        elif event_id == EVENT_VERSION:
            unicode_text = _version_is_unicode(payload, off)
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
    )


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
    Feeding a channel's decoded points straight back rewrites its blob
    byte-identically (verified across all 1,100 corpus blobs)."""
    if mode != "replace":
        raise ValueError(f"unsupported mode {mode!r}; write_automation only replaces")
    saved = codec.patch(
        path, AutomationFormat(), Target(channel=channel), points, mode,
        event_size_overrides=event_size_overrides,
    )
    return len(saved)


def effects_at(
    path: Path, insert_index: int, *, event_size_overrides: Mapping[int, int] | None = None
) -> list[Effect]:
    """The decoded effect instances in one mixer insert of the saved file."""
    _header, stream = _chunks(path.read_bytes())
    return EffectFormat().decode(codec.Stream(stream, event_size_overrides), insert_index)


def add_plugin(
    path: Path,
    target: int,
    plugin_name: str,
    kind: PluginKind,
    *,
    database: PluginDatabase = DEFAULT_PLUGIN_DATABASE,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[Effect]:
    """Add a referenced mixer plugin, then reparse and verify its opaque bytes.

    ``database`` supplies complete FL-authored records.  It deliberately has
    no synthetic default-state fallback: absent native or VST references fail
    before the file changes.  The currently proven placement remains empty
    slot 0 of a mixer insert.
    """
    raw = bytearray(path.read_bytes())
    header, stream = _chunks(bytes(raw))
    fmt = EffectFormat()
    decoded = codec.Stream(stream, event_size_overrides)
    reference = database.reference(plugin_name, kind)
    head = fmt.locate(decoded, target)
    chunk = fmt.encode(reference)
    base = 8 + len(header) + 8
    raw[base + head : base + head] = chunk
    codec._bump_fldt_length(raw, len(chunk))
    path.write_bytes(bytes(raw))
    saved = effects_at(path, target, event_size_overrides=event_size_overrides)
    fmt.verify(reference, saved)
    return saved


def add_effect(
    path: Path,
    insert_index: int,
    plugin_name: str,
    *,
    event_size_overrides: Mapping[int, int] | None = None,
) -> list[Effect]:
    """Compatibility name for adding a native mixer plugin."""
    return add_plugin(path, insert_index, plugin_name, "native", event_size_overrides=event_size_overrides)


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
    if volume is not None and not 0.0 <= volume <= 1.0:
        raise ValueError(f"volume {volume} out of range (0.0-1.0)")
    if pan is not None and not -1.0 <= pan <= 1.0:
        raise ValueError(f"pan {pan} out of range (-1.0 left to 1.0 right)")
    if pitch_semitones is not None and not -48 <= pitch_semitones <= 48:
        raise ValueError(f"pitch {pitch_semitones} out of range (-48 to 48)")

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
    if not 10.0 <= bpm <= 999.0:
        raise ValueError(f"tempo {bpm} out of range (10-999 BPM)")
    (stored,) = codec.patch(
        path, TempoFormat(), Target(), [bpm], "replace",
        event_size_overrides=event_size_overrides,
    )
    return stored
