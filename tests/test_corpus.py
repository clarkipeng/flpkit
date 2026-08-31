"""Corpus tests against FL Studio's own bundled projects (skip when FL is
absent): the strongest offline acceptance rung short of a live FL round-trip.

The automation writer's contract is byte identity: rewriting a channel's own
decoded points must reproduce the file exactly - header, per-point tails,
x-deltas, and the opaque era trailer all carried faithfully. Any decode gap
(a dropped tail byte, a delta that does not re-derive) fails loudly here.
"""

from __future__ import annotations

import shutil

import pytest
from conftest import fl_app

import flpkit as flp

_FL_DATA = fl_app() / "Contents/Resources/FL/Data"

requires_fl = pytest.mark.skipif(
    not _FL_DATA.is_dir(), reason="FL Studio is not installed on this machine"
)


def corpus_files():
    return sorted(_FL_DATA.rglob("*.flp"))


@requires_fl
def test_corpus_automation_decode_rewrite_is_byte_identical(tmp_path):
    files = corpus_files()
    assert files, "FL Data directory holds no .flp files"
    rewritten = 0
    for source in files:
        try:
            project = flp.read(source)
        except flp.FlpError:
            continue  # reader corpus coverage is its own concern
        targets = [c for c in project.channels if c.is_automation and c.automation]
        if not targets:
            continue
        copy = tmp_path / source.name
        shutil.copy(source, copy)
        original = copy.read_bytes()
        for channel in targets:
            written = flp.write_automation(copy, channel.index, channel.automation)
            assert written == len(channel.automation)
            rewritten += 1
        assert copy.read_bytes() == original, (
            f"{source.name}: rewriting decoded automation points changed bytes"
        )
        copy.unlink()
    # The corpus carries 1,100 automation blobs across 48 files; a collapse in
    # either number means the walk or the channel attribution regressed.
    assert rewritten >= 1000, f"only {rewritten} automation channels exercised"


@requires_fl
def test_corpus_notes_and_playlist_reencode_byte_identical():
    """encode(decode(blob)) == blob for EVERY notes and playlist event in the
    corpus - the byte-faithful contract that makes kept items safe through any
    patch. Catches the length-0 note records FL's own demos carry (G5 fuzz
    finding: 29 files; a max(1, .) floor in encode silently mutated them)."""
    notes_fmt, playlist_fmt = flp.NotesFormat(), flp.PlaylistFormat()
    checked = zero_length = 0
    for source in corpus_files():
        data = source.read_bytes()
        try:
            header, stream = flp._chunks(data)
        except flp.FlpError:
            continue
        ppq = int.from_bytes(header[4:6], "little")
        for event_id, off, size in flp._events(stream):
            blob = bytes(stream[off : off + size])
            if event_id == flp.EVENT_PATTERN_NOTES and size and size % flp.NOTE_SIZE == 0:
                decoded = notes_fmt.decode(blob, ppq)
                zero_length += sum(1 for n in decoded if n.length == 0)
            elif event_id == flp.EVENT_PLAYLIST and blob:
                try:
                    decoded = playlist_fmt.decode(blob, ppq)
                except flp.FlpError:
                    continue  # unresolvable stride is the reader's concern
            else:
                continue
            fmt = notes_fmt if event_id == flp.EVENT_PATTERN_NOTES else playlist_fmt
            assert fmt.encode(decoded, ppq) == blob, f"{source.name}: {fmt.name} event at {off}"
            checked += 1
    assert checked >= 500, f"only {checked} events exercised"
    assert zero_length >= 1000, f"only {zero_length} zero-length notes seen (corpus carries thousands)"


@requires_fl
def test_corpus_files_all_read(tmp_path):
    """Every FL-authored project parses: the never-crash contract corpus-wide."""
    files = corpus_files()
    assert len(files) >= 100
    for source in files:
        project = flp.read(source)
        assert project.ppq > 0
