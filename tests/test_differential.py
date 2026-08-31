"""Differential byte-identity suite: the Format codec vs the v0.5.0 writers.

Each scenario builds a synthetic project and applies one public write; the
resulting file bytes were FROZEN as goldens from the v0.5.0 hand-rolled
writers (commit 0cdf5d8) BEFORE the codec refactor. The codec-based writers
must reproduce every golden byte-for-byte - this is the proof that folding
four writers into one engine changed no output.

Do NOT regenerate the goldens from post-refactor code: that would only assert
the codec against itself. `python tests/test_differential.py` rewrites them
and exists solely for the one pre-refactor freeze.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"

sys.path.insert(0, str(Path(__file__).parent))  # for the direct-run regen mode
from test_reader import (  # noqa: E402
    VERSION_MODERN,
    automation_blob,
    build_flp,
    event,
    note_record,
    playlist_record,
)

import flpkit as flp  # noqa: E402
from flpkit import AutomationPointSpec, ClipSpec, NoteSpec  # noqa: E402


def _anchor() -> bytes:
    return event(flp.EVENT_CUR_GROUP_ID, struct.pack("<i", -1))


def _pattern(number: int, *blobs: bytes) -> bytes:
    out = event(flp.EVENT_PATTERN_NEW, struct.pack("<H", number))
    for blob in blobs:
        out += event(flp.EVENT_PATTERN_NOTES, blob)
    return out


def _channel(iid: int, pan: int = 6400, volume: int = 10000, pitch: int = 0) -> bytes:
    return event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", iid)) + event(
        flp.EVENT_CHANNEL_LEVELS, struct.pack("<iIi", pan, volume, pitch) + b"\0" * 12
    )


def _automation_channel(blob: bytes, iid: int = 0) -> bytes:
    return (
        event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", iid))
        + event(flp.EVENT_CHANNEL_TYPE, bytes([flp.CHANNEL_TYPE_AUTOMATION]))
        + event(flp.EVENT_CHANNEL_AUTOMATION, blob)
    )


def _tail_template() -> bytes:
    record = bytearray(playlist_record(88, position=0, item_index=20481, length=96, track=1))
    record[32:88] = bytes(range(56))  # a distinctive era tail the new record must inherit
    return bytes(record)


TWO_NOTES = [NoteSpec(key=60, start=0.0, length=1.0), NoteSpec(key=64, start=1.5, length=0.5)]

# name -> (initial file bytes, [(writer, args, kwargs), ...])
SCENARIOS: dict[str, tuple[bytes, list]] = {
    "notes-create-pattern": (
        build_flp(VERSION_MODERN),
        [("write_notes", (TWO_NOTES,), dict(pattern=1, channel=0, mode="replace"))],
    ),
    "notes-create-after-anchor": (
        build_flp(VERSION_MODERN + _anchor() + _channel(0)),
        [("write_notes", ([NoteSpec(key=61, start=0.0, length=1.0)],), dict(pattern=2, channel=0, mode="merge"))],
    ),
    "notes-merge-keeps-existing": (
        build_flp(VERSION_MODERN + _pattern(1, note_record(position=0, key=48, flags=0x4008))),
        [("write_notes", ([NoteSpec(key=72, start=2.0, length=1.0)],), dict(pattern=1, channel=0, mode="merge"))],
    ),
    "notes-replace-scoped-to-channel": (
        build_flp(
            VERSION_MODERN
            + _pattern(
                1,
                note_record(position=0, key=40, channel=0)
                + note_record(position=24, key=50, channel=3)
                + note_record(position=48, key=41, channel=0),
            )
        ),
        [("write_notes", ([NoteSpec(key=79, start=0.0, length=1.0)],), dict(pattern=1, channel=0, mode="replace"))],
    ),
    "notes-insert-after-pattern-event": (
        build_flp(VERSION_MODERN + _pattern(3)),
        [("write_notes", ([NoteSpec(key=61, start=0.0, length=1.0)],), dict(pattern=3, channel=2, mode="merge"))],
    ),
    "notes-ppq-192-quantization": (
        build_flp(VERSION_MODERN, ppq=192),
        [(
            "write_notes",
            ([NoteSpec(key=60, start=1.5, length=0.25, velocity=1.0, pan=0.0)],),
            dict(pattern=1, channel=0, mode="replace"),
        )],
    ),
    "notes-big-blob-multibyte-varint": (
        build_flp(VERSION_MODERN),
        [(
            "write_notes",
            ([NoteSpec(key=36 + (i % 48), start=i * 0.25, length=0.25) for i in range(64)],),
            dict(pattern=1, channel=0, mode="replace"),
        )],
    ),
    "notes-empty-replace-clears": (
        build_flp(VERSION_MODERN + _pattern(1, note_record(key=48))),
        [("write_notes", ([],), dict(pattern=1, channel=0, mode="replace"))],
    ),
    "playlist-merge-sorts-before-existing": (
        build_flp(
            VERSION_MODERN
            + event(
                flp.EVENT_PLAYLIST,
                playlist_record(88, position=1536, item_index=20482, length=1536, track=2),
            )
        ),
        [("write_playlist", ([ClipSpec(pattern=1, track=1, start=0.0, length=16.0)],), dict(mode="merge"))],
    ),
    "playlist-replace": (
        build_flp(
            VERSION_MODERN
            + event(
                flp.EVENT_PLAYLIST,
                playlist_record(88, position=0, item_index=20481, length=96, track=1)
                + playlist_record(88, position=96, item_index=20482, length=96, track=2),
            )
        ),
        [("write_playlist", ([ClipSpec(pattern=7, track=3, start=4.0, length=8.0)],), dict(mode="replace"))],
    ),
    "playlist-template-tail-inherited": (
        build_flp(VERSION_MODERN + event(flp.EVENT_PLAYLIST, _tail_template())),
        [("write_playlist", ([ClipSpec(pattern=2, track=2, start=2.0, length=1.0)],), dict())],
    ),
    "playlist-second-arrangement": (
        build_flp(
            VERSION_MODERN
            + event(flp.EVENT_ARRANGEMENT_NEW, struct.pack("<H", 0))
            + event(
                flp.EVENT_PLAYLIST,
                playlist_record(88, position=0, item_index=20481, length=96, track=1),
            )
            + event(flp.EVENT_ARRANGEMENT_NEW, struct.pack("<H", 1))
            + event(
                flp.EVENT_PLAYLIST,
                playlist_record(88, position=0, item_index=20482, length=96, track=1),
            )
        ),
        [("write_playlist", ([ClipSpec(pattern=9, track=5, start=1.0, length=1.0)],), dict(arrangement=1))],
    ),
    "automation-replace-points": (
        build_flp(_automation_channel(automation_blob([(0.0, 1.0, 0.0)]))),
        [(
            "write_automation",
            (0, [
                AutomationPointSpec(position=0.0, value=0.25),
                AutomationPointSpec(position=4.0, value=0.75, tension=-0.5),
                AutomationPointSpec(position=8.0, value=1.0),
            ]),
            dict(),
        )],
    ),
    "automation-empty-replace": (
        build_flp(_automation_channel(automation_blob([(0.0, 1.0, 0.0), (2.0, 0.5, 0.1)]))),
        [("write_automation", (0, []), dict())],
    ),
    "automation-custom-tails": (
        build_flp(_automation_channel(automation_blob([(0.0, 1.0, 0.0)], tail=136))),
        [(
            "write_automation",
            (0, [
                flp.FlpAutomationPoint(position=0.5, value=0.5, tension=0.0, tail=b"\x01\x02\x03\x04"),
                flp.FlpAutomationPoint(position=1.5, value=0.9, tension=0.2, tail=b"\x00\x00\x00\xff"),
            ]),
            dict(),
        )],
    ),
    "levels-all-three-fields": (
        build_flp(VERSION_MODERN + _channel(0)),
        [("set_channel_levels", (0,), dict(volume=0.5, pan=-0.25, pitch_semitones=3))],
    ),
    "levels-partial-pan-only": (
        build_flp(VERSION_MODERN + _channel(0, volume=11000, pitch=-2) + _channel(1)),
        [("set_channel_levels", (0,), dict(pan=1.0))],
    ),
    "levels-implicit-channel-zero": (
        build_flp(
            VERSION_MODERN
            + event(flp.EVENT_CHANNEL_LEVELS, struct.pack("<iIi", 6400, 10000, 0) + b"\0" * 12)
            + _channel(1),
            n_channels=2,
        ),
        [("set_channel_levels", (0,), dict(volume=0.25))],
    ),
    "tempo-patch-in-place": (
        build_flp(VERSION_MODERN + event(flp.EVENT_TEMPO, struct.pack("<I", 140_000)) + _channel(0)),
        [("set_tempo", (137.5,), dict())],
    ),
    "tempo-append-when-absent": (
        build_flp(VERSION_MODERN + _channel(0)),
        [("set_tempo", (133,), dict())],
    ),
    "sequenced-writes-compose": (
        build_flp(VERSION_MODERN + _channel(0)),
        [
            ("set_tempo", (128.0,), dict()),
            ("write_notes", (TWO_NOTES,), dict(pattern=1, channel=0, mode="replace")),
            ("write_notes", ([NoteSpec(key=79, start=0.0, length=2.0)],), dict(pattern=1, channel=1, mode="merge")),
            ("set_channel_levels", (0,), dict(volume=0.8)),
        ],
    ),
}


def _run(name: str, out_dir: Path) -> bytes:
    initial, writes = SCENARIOS[name]
    path = out_dir / f"{name}.flp"
    path.write_bytes(initial)
    for fn, args, kwargs in writes:
        getattr(flp, fn)(path, *args, **kwargs)
    return path.read_bytes()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_codec_output_is_byte_identical_to_v050_writers(name, tmp_path):
    golden = GOLDEN_DIR / f"{name}.flp"
    assert golden.is_file(), f"missing golden {golden.name}; see the module docstring"
    assert _run(name, tmp_path) == golden.read_bytes(), (
        f"{name}: output diverged from the frozen v0.5.0 writer bytes"
    )


if __name__ == "__main__":  # the one pre-refactor freeze; see the module docstring
    GOLDEN_DIR.mkdir(exist_ok=True)
    for name in sorted(SCENARIOS):
        (GOLDEN_DIR / f"{name}.flp").write_bytes(_run(name, GOLDEN_DIR))
        print(f"froze {name}.flp")
