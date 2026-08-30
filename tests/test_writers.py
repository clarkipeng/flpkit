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
