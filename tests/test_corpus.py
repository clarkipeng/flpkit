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
def test_corpus_files_all_read(tmp_path):
    """Every FL-authored project parses: the never-crash contract corpus-wide."""
    files = corpus_files()
    assert len(files) >= 100
    for source in files:
        project = flp.read(source)
        assert project.ppq > 0
