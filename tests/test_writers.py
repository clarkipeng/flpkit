"""flpkit writer tests: raw byte surgery, verified by re-reading the file.

set_tempo was a PORT of the live-verified v1 (flp_writer.set_tempo, commit
13f8afc): the byte-differential test below asserts both produce identical
files, so any byte-level drift in the port fails loudly.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest
from conftest import fl_app
from test_reader import VERSION_MODERN, event, note_record, write_flp

import flpkit as flp
from flpkit import NoteSpec as Note

_FL_DATA = fl_app() / "Contents/Resources/FL/Data"

requires_fl = pytest.mark.skipif(
    not _FL_DATA.is_dir(), reason="FL Studio is not installed on this machine"
)

# pyflp renders keys as sharp note names with C5 = MIDI 60 (its octaves run
# C0=0 .. B10=131); ours are firm MIDI ints. One inverse, used by every
# oracle comparison.
_PYFLP_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def pyflp_key_to_midi(key: str) -> int:
    letter = key.rstrip("0123456789")
    return _PYFLP_NOTE_NAMES.index(letter) + 12 * int(key[len(letter) :])


# -- set_tempo ----------------------------------------------------------------


def test_set_tempo_patches_existing_event_in_place(tmp_path):
    events = (
        VERSION_MODERN
        + event(flp.EVENT_TEMPO, struct.pack("<I", 140_000))
        + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 0))
    )
    path = write_flp(tmp_path, events)
    before = path.read_bytes()

    assert flp.set_tempo(path, 137) == 137.0

    after = path.read_bytes()
    assert len(after) == len(before)  # in-place patch, no splice
    assert flp.read(path).tempo == 137.0


def test_set_tempo_appends_at_end_of_stream_when_absent(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    before = path.read_bytes()

    assert flp.set_tempo(path, 133) == 133.0

    after = path.read_bytes()
    # The event lands literally at the end of the stream, and the FLdt length
    # grows by exactly the 5 appended bytes - the live-verified v1 decision.
    assert after[-5:] == bytes([flp.EVENT_TEMPO]) + struct.pack("<I", 133_000)
    assert len(after) == len(before) + 5
    assert flp.read(path).tempo == 133.0


def test_set_tempo_rejects_out_of_range():
    for bpm in (9.9, 999.1, 0, -10):
        with pytest.raises(ValueError, match="out of range"):
            flp.set_tempo(Path("/nonexistent.flp"), bpm)


def test_set_tempo_readback_is_the_stored_quantized_value(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    stored = flp.set_tempo(path, 128.5004)
    assert stored == round(128.5004 * 1000) / 1000


# The v1 byte-identity differential (test_set_tempo_byte_identical_to_v1_port_source)
# lived here until the cutover. It asserted that v2's set_tempo wrote the same bytes
# as v1's flp_writer.set_tempo for both the mutate and append paths, and it PASSED -
# which is what licensed deleting v1. It cannot be re-run now that flp_writer is gone,
# and re-freezing v2's own output as a golden would only assert v2 == v2, so it is
# retired rather than faked. The live gate (tests/test_flp_live.py) is what guards
# set_tempo from here on: FL itself reports the tempo back.


# -- write_notes --------------------------------------------------------------


def pattern_events(number: int, *blobs: bytes) -> bytes:
    out = event(flp.EVENT_PATTERN_NEW, struct.pack("<H", number))
    for blob in blobs:
        out += event(flp.EVENT_PATTERN_NOTES, blob)
    return out


def test_write_notes_creates_pattern_when_file_has_none(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    notes = [Note(key=60, start=0.0, length=1.0), Note(key=64, start=1.0, length=0.5)]

    result = flp.write_notes(path, notes, pattern=1, channel=0, mode="replace")

    assert [(n.key, n.position, n.length) for n in result] == [(60, 0, 96), (64, 96, 48)]
    # The result IS notes_at of the saved file, not an echo.
    assert result == flp.notes_at(path, 1, 0)


def test_write_notes_creates_pattern_after_cur_group_anchor(tmp_path):
    anchor = event(flp.EVENT_CUR_GROUP_ID, struct.pack("<i", -1))
    path = write_flp(tmp_path, VERSION_MODERN + anchor)

    flp.write_notes(path, [Note(key=60, start=0.0, length=1.0)], pattern=2, channel=0, mode="merge")

    data = path.read_bytes()
    # PatternID.New lands immediately after the anchor event, 224 after it -
    # v1's placement, which real FL accepts.
    anchor_at = data.index(bytes([flp.EVENT_CUR_GROUP_ID]))
    assert data[anchor_at + 5] == flp.EVENT_PATTERN_NEW
    assert flp.read(path).notes_in(2)[0].key == 60


def test_write_notes_merge_keeps_existing_notes(tmp_path):
    existing = note_record(position=0, key=48, channel=0)
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, existing))

    result = flp.write_notes(
        path, [Note(key=72, start=2.0, length=1.0)], pattern=1, channel=0, mode="merge"
    )

    assert sorted(n.key for n in result) == [48, 72]


def test_write_notes_replace_drops_existing_notes(tmp_path):
    existing = note_record(position=0, key=48) + note_record(position=24, key=50)
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, existing))

    result = flp.write_notes(
        path, [Note(key=72, start=0.0, length=1.0)], pattern=1, channel=0, mode="replace"
    )

    assert [n.key for n in result] == [72]
    assert flp.notes_at(path, 1, 0) == result


def test_write_notes_inserts_after_pattern_event_when_no_notes_yet(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(3))

    flp.write_notes(path, [Note(key=61, start=0.0, length=1.0)], pattern=3, channel=2, mode="merge")

    (note,) = flp.notes_at(path, 3, 2)
    assert (note.key, note.channel, note.pattern) == (61, 2, 3)


def test_write_notes_targets_only_the_requested_pattern(tmp_path):
    p1 = pattern_events(1, note_record(position=0, key=40))
    p2 = pattern_events(2, note_record(position=0, key=41))
    path = write_flp(tmp_path, VERSION_MODERN + p1 + p2)

    flp.write_notes(path, [Note(key=90, start=0.0, length=1.0)], pattern=2, channel=0, mode="merge")

    project = flp.read(path)
    assert [n.key for n in project.notes_in(1)] == [40]  # untouched
    assert sorted(n.key for n in project.notes_in(2)) == [41, 90]


def test_write_notes_does_not_adopt_another_patterns_blob(tmp_path):
    # Pattern 1 exists with no notes event; pattern 2 has one. v1's scan would
    # have merged into pattern 2's blob; the fixed attribution must not.
    p1 = pattern_events(1)
    p2 = pattern_events(2, note_record(position=0, key=41))
    path = write_flp(tmp_path, VERSION_MODERN + p1 + p2)

    flp.write_notes(path, [Note(key=90, start=0.0, length=1.0)], pattern=1, channel=0, mode="merge")

    project = flp.read(path)
    assert [n.key for n in project.notes_in(1)] == [90]
    assert [n.key for n in project.notes_in(2)] == [41]


def test_write_notes_uses_file_ppq_for_tick_quantization(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN, ppq=192)

    result = flp.write_notes(
        path, [Note(key=60, start=1.5, length=0.25)], pattern=1, channel=0, mode="replace"
    )

    (note,) = result
    assert (note.position, note.length) == (288, 48)


def test_write_notes_velocity_and_pan_quantization(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)

    (note,) = flp.write_notes(
        path,
        [Note(key=60, start=0.0, length=1.0, velocity=1.0, pan=0.0)],
        pattern=1, channel=0, mode="replace",
    )

    assert (note.velocity, note.pan) == (127, 0)


def test_write_notes_empty_replace_clears_the_pattern(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, note_record(key=48)))

    result = flp.write_notes(path, [], pattern=1, channel=0, mode="replace")

    assert result == []
    assert flp.read(path).notes_in(1) == []


def test_write_notes_result_still_a_valid_flp(tmp_path):
    # A large blob forces a multi-byte varint length; the file must re-parse.
    path = write_flp(tmp_path, VERSION_MODERN)
    notes = [Note(key=36 + (i % 48), start=i * 0.25, length=0.25) for i in range(64)]

    result = flp.write_notes(path, notes, pattern=1, channel=0, mode="replace")

    assert len(result) == 64
    assert len(flp.read(path).notes_in(1)) == 64


def test_patch_keeps_zero_length_notes_byte_identical(tmp_path):
    """FL's own demos carry note records with raw length 0 (29 corpus files,
    e.g. 601 in 'Astes - Bien Duro'); an identity patch must not mutate them
    to 1 tick (G5 fuzz finding: encode once floored length with max(1, .))."""
    from flpkit import codec

    blob = note_record(position=0, key=60, length=0) + note_record(position=24, key=62, length=0)
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, blob))
    before = path.read_bytes()

    target = codec.Target(pattern=1, channel=0)
    items = codec.read(path, flp.NotesFormat(), target)
    assert [i.length for i in items] == [0.0, 0.0]
    codec.patch(path, flp.NotesFormat(), target, items, "replace")

    assert path.read_bytes() == before


def channel_with_levels(iid: int, pan: int = 6400, volume: int = 10000, pitch: int = 0) -> bytes:
    return event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", iid)) + event(
        flp.EVENT_CHANNEL_LEVELS, struct.pack("<iIi", pan, volume, pitch) + b"\0" * 12
    )


def test_set_channel_levels_patches_all_three_fields(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + channel_with_levels(0))

    result = flp.set_channel_levels(path, 0, volume=0.5, pan=-0.25, pitch_semitones=3)

    assert result.channel == 0
    assert result.volume == pytest.approx(6400 / 12800)
    assert result.pan == pytest.approx((6400 - 1600 - 6400) / 6400)
    assert result.pitch_semitones == 3
    saved = flp.read(path).channels[0]
    assert (saved.pan, saved.volume, saved.pitch_semitones) == (4800, 6400, 3)


def test_set_channel_levels_none_leaves_fields_alone(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + channel_with_levels(0, volume=11000, pitch=-2))

    result = flp.set_channel_levels(path, 0, pan=1.0)

    saved = flp.read(path).channels[0]
    assert (saved.volume, saved.pitch_semitones) == (11000, -2)  # untouched
    assert saved.pan == 12800
    assert result.pan == pytest.approx(1.0)


def test_set_channel_levels_targets_only_the_requested_channel(tmp_path):
    events = VERSION_MODERN + channel_with_levels(0) + channel_with_levels(1)
    path = write_flp(tmp_path, events)

    flp.set_channel_levels(path, 1, volume=0.25)

    first, second = flp.read(path).channels
    assert first.volume == 10000  # untouched
    assert second.volume == 3200


def test_set_channel_levels_validates_ranges(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + channel_with_levels(0))
    for kwargs in ({"volume": 1.1}, {"pan": -1.5}, {"pitch_semitones": 49}):
        with pytest.raises(ValueError, match="out of range"):
            flp.set_channel_levels(path, 0, **kwargs)


def test_set_channel_levels_reaches_the_implicit_channel_zero(tmp_path):
    # 'Basic with limiter' layout: channel 0's Levels event precedes any
    # New(64); New(64)=1.. open the named channels.
    events = VERSION_MODERN + event(
        flp.EVENT_CHANNEL_LEVELS, struct.pack("<iIi", 6400, 10000, 0) + b"\0" * 12
    ) + channel_with_levels(1)
    path = write_flp(tmp_path, events, n_channels=2)

    result = flp.set_channel_levels(path, 0, volume=0.25)

    assert result.volume == pytest.approx(0.25)
    implicit, explicit = flp.read(path).channels
    assert (implicit.index, implicit.volume) == (0, 3200)
    assert explicit.volume == 10000  # untouched


def test_set_channel_levels_unknown_channel_raises_index_error(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + channel_with_levels(0))
    with pytest.raises(IndexError, match="no channel 5"):
        flp.set_channel_levels(path, 5, volume=0.5)


def test_set_channel_levels_refuses_legacy_channel_without_219(tmp_path):
    events = VERSION_MODERN + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 0)) + event(
        flp.EVENT_VOL_WORD, struct.pack("<H", 9000)
    )
    path = write_flp(tmp_path, events)
    before = path.read_bytes()

    with pytest.raises(flp.FlpError, match="no Levels event"):
        flp.set_channel_levels(path, 0, volume=0.5)
    assert path.read_bytes() == before  # refused writes touch nothing


@requires_fl
def test_set_tempo_on_stock_templates_both_paths(tmp_path):
    # Both templates carry a tempo event. Basic with limiter was long believed
    # to omit it - that was the event-172 walker desync eating the event; the
    # fixed walker finds and patches it in place (the append path is covered by
    # the synthetic no-tempo test above).
    cases = [
        ("Templates/Minimal/Empty/Empty.flp", "patch"),
        ("Templates/Minimal/Basic with limiter/Basic with limiter.flp", "patch"),
    ]
    for rel, label in cases:
        path = tmp_path / f"{label}.flp"
        shutil.copy(_FL_DATA / rel, path)
        grew = len(path.read_bytes())

        assert flp.set_tempo(path, 123) == 123.0

        expected_growth = 0 if label == "patch" else 5
        assert len(path.read_bytes()) == grew + expected_growth


def test_replace_is_scoped_to_the_channel_not_the_whole_pattern(tmp_path):
    """A pattern's Notes blob holds EVERY channel's notes. Replacing the whole
    blob silently destroyed other channels' work - measured against a stock
    template, one note written to channel 0 wiped 51 notes on channel 3.
    """
    project = write_flp(tmp_path, VERSION_MODERN)

    flp.write_notes(
        project,
        [Note(key=60, start=0.0, length=1.0), Note(key=64, start=1.0, length=1.0)],
        pattern=1,
        channel=3,
        mode="replace",
    )
    flp.write_notes(
        project, [Note(key=79, start=0.0, length=1.0)], pattern=1, channel=0, mode="replace"
    )

    assert [n.key for n in flp.notes_at(project, 1, 0)] == [79]
    # The other channel is untouched: replace scopes to its own channel.
    assert sorted(n.key for n in flp.notes_at(project, 1, 3)) == [60, 64]


# --- write_playlist -----------------------------------------------------------

from test_reader import playlist_record  # noqa: E402


def playlist_file(tmp_path, records: bytes, ppq: int = 96) -> Path:
    events = VERSION_MODERN + event(flp.EVENT_PLAYLIST, records)
    return write_flp(tmp_path, events, ppq=ppq)


def test_write_playlist_merges_and_keeps_position_sort(tmp_path):
    path = playlist_file(
        tmp_path,
        playlist_record(88, position=1536, item_index=20482, length=1536, track=2),
    )
    result = flp.write_playlist(
        path,
        [flp.ClipSpec(pattern=1, track=1, start=0.0, length=16.0)],
        mode="merge",
    )
    assert [(i.position, i.pattern, i.track) for i in result] == [
        (0, 1, 1), (1536, 2, 2)
    ]
    # the saved bytes are position-sorted, matching how FL itself writes
    reread = flp.read(path).playlist
    assert [i.position for i in reread] == sorted(i.position for i in reread)


def test_write_playlist_replace_scopes_to_the_arrangement(tmp_path):
    path = playlist_file(
        tmp_path,
        playlist_record(88, position=0, item_index=20481, length=96, track=1)
        + playlist_record(88, position=96, item_index=20482, length=96, track=2),
    )
    result = flp.write_playlist(
        path,
        [flp.ClipSpec(pattern=7, track=3, start=4.0, length=8.0)],
        mode="replace",
    )
    assert [(i.pattern, i.track, i.position, i.length) for i in result] == [(7, 3, 384, 768)]


def test_write_playlist_new_record_inherits_the_template_tail(tmp_path):
    template = bytearray(
        playlist_record(88, position=0, item_index=20481, length=96, track=1)
    )
    template[32:88] = bytes(range(56))  # a distinctive era tail
    path = playlist_file(tmp_path, bytes(template))
    flp.write_playlist(path, [flp.ClipSpec(pattern=2, track=2, start=2.0, length=1.0)])
    data = path.read_bytes()
    blob_at = data.find(bytes(range(56)))
    assert blob_at != -1
    assert data.find(bytes(range(56)), blob_at + 1) != -1, "new record lost the tail"


def test_write_playlist_refuses_without_a_template(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    with pytest.raises(flp.FlpError, match="template"):
        flp.write_playlist(path, [flp.ClipSpec(pattern=1, track=1, start=0.0, length=1.0)])


def test_write_playlist_targets_the_requested_arrangement(tmp_path):
    clip0 = playlist_record(88, position=0, item_index=20481, length=96, track=1)
    clip1 = playlist_record(88, position=0, item_index=20482, length=96, track=1)
    events = (
        VERSION_MODERN
        + event(flp.EVENT_ARRANGEMENT_NEW, struct.pack("<H", 0))
        + event(flp.EVENT_PLAYLIST, clip0)
        + event(flp.EVENT_ARRANGEMENT_NEW, struct.pack("<H", 1))
        + event(flp.EVENT_PLAYLIST, clip1)
    )
    path = write_flp(tmp_path, events)
    flp.write_playlist(
        path,
        [flp.ClipSpec(pattern=9, track=5, start=1.0, length=1.0)],
        arrangement=1,
    )
    project = flp.read(path)
    assert [i.pattern for i in project.playlist if i.arrangement == 0] == [1]
    assert sorted(
        i.pattern for i in project.playlist if i.arrangement == 1
    ) == [2, 9]


def test_write_playlist_rejects_track_out_of_space(tmp_path):
    path = playlist_file(
        tmp_path, playlist_record(88, position=0, item_index=20481, length=96, track=1)
    )
    with pytest.raises(ValueError, match="track"):
        flp.write_playlist(path, [flp.ClipSpec(pattern=1, track=0, start=0.0, length=1.0)])


@pytest.mark.parametrize(
    "window",
    [(0xBF800000, 0xBF800000), (0, 0x43B80000), (24, 96)],
    ids=["legacy-uncut-sentinel", "legacy-f32-end", "cut-clip"],
)
def test_write_playlist_carries_existing_windows_verbatim(tmp_path, window):
    """Existing records' window bytes are era-opaque and untouched: legacy
    eras stamp f32 shapes there, and cut clips carry real windows. flpkit 0.4
    wrote all of these and the fl-studio-mcp e2e matrix live-verified FL 2026
    accepting the modern window on NEW records beside them (a 0.6.0 guard
    refused these files - the regression the matrix caught on the pin bump)."""
    record = bytearray(playlist_record(88, position=0, item_index=20481, length=96, track=1))
    struct.pack_into("<II", record, 24, *window)
    path = playlist_file(tmp_path, bytes(record))

    clips = flp.write_playlist(path, [flp.ClipSpec(pattern=2, track=2, start=1.0, length=1.0)])
    assert len(clips) == 2  # template clip + the new one, re-read from the file
    blob = path.read_bytes()
    assert struct.pack("<II", *window) in blob  # the existing record's window survived


# -- detect-don't-assume: event-size overrides ---------------------------------


def stream_with_172() -> bytes:
    return (
        VERSION_MODERN
        + bytes([172, 0x01])  # the 1-byte event FL 2026 writes
        + event(flp.EVENT_TEMPO, struct.pack("<I", 130_000))
        + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 0))
    )


def test_read_accepts_explicit_event_size_overrides(tmp_path):
    path = write_flp(tmp_path, stream_with_172())
    project = flp.read(path, event_size_overrides={172: 1})
    assert project.tempo == 130.0
    assert len(project.channels) == 1


def test_read_with_empty_overrides_shows_the_classic_rule_desync(tmp_path):
    """Passing an explicit table REPLACES the fallback: with no 172 entry the
    classic 4-byte rule applies and the walker desyncs, eating the tempo event
    - the exact failure class the override table exists to prevent."""
    path = write_flp(tmp_path, stream_with_172())
    assert flp.read(path, event_size_overrides={}).tempo is None


def test_writers_thread_event_size_overrides_through(tmp_path):
    path = write_flp(tmp_path, stream_with_172())
    stored = flp.set_tempo(path, 141, event_size_overrides={172: 1})
    assert stored == 141.0
    (note,) = flp.write_notes(
        path, [Note(key=60, start=0.0, length=1.0)],
        pattern=1, channel=0, mode="replace", event_size_overrides={172: 1},
    )
    assert note.key == 60


def test_fallback_override_table_is_logged_once_per_process(tmp_path, caplog):
    flp._log_size_override_fallback.cache_clear()
    path = write_flp(tmp_path, stream_with_172())
    with caplog.at_level("INFO", logger="flpkit"):
        flp.read(path)
        flp.read(path)
    hits = [r for r in caplog.records if "event_size_overrides" in r.message]
    assert len(hits) == 1  # once per process, not once per call
    caplog.clear()
    with caplog.at_level("INFO", logger="flpkit"):
        flp.read(path, event_size_overrides={172: 1})
    assert not [r for r in caplog.records if "event_size_overrides" in r.message]


# -- detect-don't-assume: note flags templated from the file -------------------


def notes_blob_flags(path) -> list[int]:
    """The flags word of every note record in the saved file's 224 events."""
    data = path.read_bytes()
    _header, stream = flp._chunks(data)
    flags = []
    for event_id, off, size in flp._events(stream):
        if event_id == flp.EVENT_PATTERN_NOTES and size % flp.NOTE_SIZE == 0:
            flags.extend(
                int.from_bytes(stream[off + at + 4 : off + at + 6], "little")
                for at in range(0, size, flp.NOTE_SIZE)
            )
    return flags


def test_new_notes_template_flags_from_the_files_existing_notes(tmp_path):
    existing = note_record(position=0, key=48, flags=0x4008)
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, existing))

    flp.write_notes(path, [Note(key=72, start=2.0, length=1.0)], pattern=1, channel=0, mode="merge")

    assert notes_blob_flags(path) == [0x4008, 0x4008]


def test_new_notes_use_the_most_common_flags_in_the_file(tmp_path):
    blob = (
        note_record(position=0, key=48, flags=0x4000)
        + note_record(position=24, key=50, flags=0x4000)
        + note_record(position=48, key=52, flags=0x4008)
    )
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, blob))

    flp.write_notes(path, [Note(key=72, start=4.0, length=1.0)], pattern=2, channel=0, mode="merge")

    assert notes_blob_flags(path).count(0x4000) == 3  # the majority flags won


def test_new_notes_fall_back_to_the_corpus_flags_constant(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)

    flp.write_notes(path, [Note(key=60, start=0.0, length=1.0)], pattern=1, channel=0, mode="replace")

    assert notes_blob_flags(path) == [flp.NOTE_FLAGS_DEFAULT]


# -- write_automation ----------------------------------------------------------

from test_reader import automation_blob  # noqa: E402

DISTINCT_HEADER = bytes(range(1, 18))  # 17 distinctive header bytes
DISTINCT_TRAILER = bytes(range(100, 212))  # 112 distinctive trailer bytes


def automation_events(blob: bytes, iid: int = 0, kind: int | None = flp.CHANNEL_TYPE_AUTOMATION) -> bytes:
    out = VERSION_MODERN + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", iid))
    if kind is not None:
        out += event(flp.EVENT_CHANNEL_TYPE, bytes([kind]))
    if blob:
        out += event(flp.EVENT_CHANNEL_AUTOMATION, blob)
    return out


def distinct_blob(points: list[tuple[float, float, float]]) -> bytes:
    body = bytearray(DISTINCT_HEADER)
    body += struct.pack("<I", len(points))
    for delta, value, tension in points:
        body += struct.pack("<ddf", delta, value, tension) + bytes(4)
    return bytes(body) + DISTINCT_TRAILER


def test_write_automation_replaces_points_and_reads_back(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))

    written = flp.write_automation(path, 0, [
        flp.AutomationPointSpec(position=0.0, value=0.25),
        flp.AutomationPointSpec(position=4.0, value=0.75, tension=-0.5),
        flp.AutomationPointSpec(position=8.0, value=1.0),
    ])

    assert written == 3
    (channel,) = flp.read(path).channels
    assert [(p.position, p.value) for p in channel.automation] == [
        (0.0, 0.25), (4.0, 0.75), (8.0, 1.0)
    ]
    assert channel.automation[1].tension == pytest.approx(-0.5)


def test_write_automation_carries_header_and_trailer_verbatim(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))

    flp.write_automation(path, 0, [
        flp.AutomationPointSpec(position=1.0, value=0.5),
        flp.AutomationPointSpec(position=2.0, value=0.5),
    ])

    data = path.read_bytes()
    head_at = data.find(DISTINCT_HEADER)
    assert head_at != -1
    # count changed from 1 to 2 but the header and the era trailer are FL's
    # own bytes, untouched.
    assert struct.unpack_from("<I", data, head_at + 17)[0] == 2
    assert data.find(DISTINCT_TRAILER) == head_at + 17 + 4 + 2 * 24


def test_write_automation_converts_absolute_positions_to_deltas(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))

    flp.write_automation(path, 0, [
        flp.AutomationPointSpec(position=1.0, value=0.1),
        flp.AutomationPointSpec(position=2.5, value=0.2),
        flp.AutomationPointSpec(position=4.0, value=0.3),
    ])

    data = path.read_bytes()
    at = data.find(DISTINCT_HEADER) + 21
    deltas = [struct.unpack_from("<d", data, at + i * 24)[0] for i in range(3)]
    assert deltas == [1.0, 1.5, 1.5]


def test_write_automation_sorts_points_by_position(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))

    flp.write_automation(path, 0, [
        flp.AutomationPointSpec(position=8.0, value=0.9),
        flp.AutomationPointSpec(position=0.0, value=0.1),
    ])

    (channel,) = flp.read(path).channels
    assert [(p.position, p.value) for p in channel.automation] == [(0.0, 0.1), (8.0, 0.9)]


def test_write_automation_decode_rewrite_is_byte_identical(tmp_path):
    """Feeding a channel's decoded points straight back must rewrite the file
    byte-identically - the writer proves the reader (varied per-point tails
    and all)."""
    blob = automation_blob(
        [(0.5, 0.25, 0.0), (1.5, 0.5, 0.3), (2.0, 1.0, -0.4)],
        point_tails=[b"\x00\x00\x00\x00", b"\x00\x00\x00\xff", b"\x00\x00\x00\x02"],
    )
    path = write_flp(tmp_path, automation_events(blob))
    before = path.read_bytes()

    (channel,) = flp.read(path).channels
    written = flp.write_automation(path, 0, channel.automation)

    assert written == 3
    assert path.read_bytes() == before


def test_write_automation_empty_replace_clears_the_points(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))

    assert flp.write_automation(path, 0, []) == 0

    (channel,) = flp.read(path).channels
    assert channel.automation == ()
    data = path.read_bytes()
    assert data.find(DISTINCT_TRAILER) == data.find(DISTINCT_HEADER) + 21


def test_write_automation_refuses_a_non_automation_channel(tmp_path):
    # kind 0 = a generator channel; its 234 event (if any) is not a curve.
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)]), kind=0))
    before = path.read_bytes()
    with pytest.raises(flp.FlpError, match="not an automation channel"):
        flp.write_automation(path, 0, [flp.AutomationPointSpec(position=0.0, value=0.5)])
    assert path.read_bytes() == before


def test_write_automation_refuses_a_channel_without_a_kind(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)]), kind=None))
    with pytest.raises(flp.FlpError, match="not an automation channel"):
        flp.write_automation(path, 0, [flp.AutomationPointSpec(position=0.0, value=0.5)])


def test_write_automation_refuses_a_channel_without_a_blob(tmp_path):
    path = write_flp(tmp_path, automation_events(b""))
    with pytest.raises(flp.FlpError, match="no automation blob"):
        flp.write_automation(path, 0, [flp.AutomationPointSpec(position=0.0, value=0.5)])


def test_write_automation_unknown_channel_raises_index_error(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))
    with pytest.raises(IndexError, match="no channel 7"):
        flp.write_automation(path, 7, [flp.AutomationPointSpec(position=0.0, value=0.5)])


def test_write_automation_refuses_a_blob_with_a_lying_count(tmp_path):
    blob = distinct_blob([(0.0, 1.0, 0.0)])[:30]  # count says 1, points cut off
    path = write_flp(tmp_path, automation_events(blob))
    before = path.read_bytes()
    with pytest.raises(flp.FlpError, match="claims 1 point"):
        flp.write_automation(path, 0, [flp.AutomationPointSpec(position=0.0, value=0.5)])
    assert path.read_bytes() == before


def test_write_automation_validates_inputs(tmp_path):
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))
    before = path.read_bytes()
    cases = [
        ([flp.AutomationPointSpec(position=0.0, value=2.0)], "value"),
        ([flp.AutomationPointSpec(position=0.0, value=-0.5)], "value"),
        ([flp.AutomationPointSpec(position=0.0, value=0.5, tension=1.5)], "tension"),
        ([flp.AutomationPointSpec(position=-1.0, value=0.5)], "negative"),
        ([flp.FlpAutomationPoint(position=0.0, value=0.5, tension=0.0, tail=b"\x00")], "tail"),
    ]
    for points, match in cases:
        with pytest.raises(ValueError, match=match):
            flp.write_automation(path, 0, points)
    with pytest.raises(ValueError, match="mode"):
        flp.write_automation(
            path, 0, [flp.AutomationPointSpec(position=0.0, value=0.5)], mode="merge"
        )
    assert path.read_bytes() == before


def test_write_automation_accepts_fl_style_value_overshoot(tmp_path):
    # FL itself stores values a hair above 1.0 (corpus max 1.0000403); refusing
    # them would refuse FL's own data.
    path = write_flp(tmp_path, automation_events(distinct_blob([(0.0, 1.0, 0.0)])))
    assert flp.write_automation(
        path, 0, [flp.AutomationPointSpec(position=0.0, value=1.0000403)]
    ) == 1


def test_write_automation_targets_only_the_requested_channel(tmp_path):
    first = distinct_blob([(0.0, 0.1, 0.0)])
    second = automation_blob([(0.0, 0.9, 0.0)])
    events = (
        VERSION_MODERN
        + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 0))
        + event(flp.EVENT_CHANNEL_TYPE, bytes([flp.CHANNEL_TYPE_AUTOMATION]))
        + event(flp.EVENT_CHANNEL_AUTOMATION, first)
        + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 1))
        + event(flp.EVENT_CHANNEL_TYPE, bytes([flp.CHANNEL_TYPE_AUTOMATION]))
        + event(flp.EVENT_CHANNEL_AUTOMATION, second)
    )
    path = write_flp(tmp_path, events, n_channels=2)

    flp.write_automation(path, 1, [flp.AutomationPointSpec(position=2.0, value=0.5)])

    channels = flp.read(path).channels
    assert [(p.position, p.value) for p in channels[0].automation] == [(0.0, 0.1)]
    assert [(p.position, p.value) for p in channels[1].automation] == [(2.0, 0.5)]
    assert path.read_bytes().find(first) != -1  # channel 0's blob is untouched bytes
