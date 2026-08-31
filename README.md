# flpkit

Read and write FL Studio `.flp` project files - without FL Studio, without dependencies.

```python
import flpkit

project = flpkit.read(path)          # ppq, tempo, channels (names + levels + automation), notes, playlist
flpkit.set_tempo(path, 128.5)        # returns the tempo the SAVED file contains
flpkit.write_notes(
    path,
    [flpkit.NoteSpec(key=60, start=0, length=1)],  # beats; velocity/pan 0..1
    pattern=1, channel=0, mode="merge",
)                                     # returns the notes read back from the saved file
flpkit.set_channel_levels(path, 0, volume=0.8, pan=-0.25)
```

## Why this exists

The FLP format is proprietary and undocumented.
The existing reverse-engineered library (pyflp, GPL) has a broad *parser*, but its *serializer* rewrites bytes it shouldn't - we observed it write a wrong channel count into the file header and mangle a UTF-16 text event, producing files that parsers read back happily and **real FL Studio refuses to open**.

flpkit takes the opposite approach for writing: **raw byte surgery**.
A write patches or appends exactly the bytes that express the change and never reserializes the file, so everything the library does not model survives untouched.
Every writer then **verifies itself**: it re-reads the saved file and field-matches the result against what was sent, raising `FlpError` instead of returning hope.

## What it reads

- PPQ and tempo, including the legacy pre-`156` coarse/fine word pair
- Channels with display names (user rename → legacy name → plugin internal name) and mix levels across four format generations (`Levels` 219, word events, byte events)
- Notes per pattern and channel (the 24-byte packed record), with correct attribution for the implicit channel 0 and pre-pattern note blobs that stock FL files contain
- UTF-16/Latin-1 text switching keyed off the file's `FLVersion`
- Playlist items per arrangement (pattern/audio clips: position, length, track, group), with the record stride DETECTED per blob - FL grew the record from 32 to 60 to 80 to 88 bytes across eras, and the constant `pattern_base` signature identifies the true size instead of a hardcoded list
- Automation clips: each type-5 channel's points (position in beats from clip start, value 0..1, tension), decoded from the delta-encoded f64 records and verified point-identical to pyflp across 1,100 real blobs

## What it writes

- `set_tempo` - patches the tempo event in place, or appends one when the file omits it (FL expresses default tempo by omission; end-of-stream append is the placement real FL accepts)
- `write_notes` - splices a pattern's notes blob; `mode="replace"` is scoped to the target channel (a pattern's blob holds *every* channel's notes - naive replacement destroys other channels' work)
- `set_channel_levels` - patches pan/volume/pitch int32s inside the channel's `Levels` event; refuses legacy files rather than writing guessed units
- `write_playlist` - splices pattern clips into an arrangement's playlist event; every new record is built from the first EXISTING record as a byte template (so the era-specific tail carries FL's own defaults), kept position-sorted the way FL writes them. Live-verified: FL Studio 2026 loads the written clips (song length grows to match) and its OWN re-save round-trips them byte-identically
- `write_automation` - replaces the points inside an EXISTING automation channel's blob; the 17-byte header and the opaque era trailer are carried verbatim, absolute positions convert back to FL's stored x-deltas, and each point's opaque 4-byte tail rides along.
  Feeding a channel's decoded points straight back rewrites its blob byte-identically (verified across all 1,100 corpus blobs).
  Creating a NEW clip is out of scope until the link bytes are decoded - a non-automation channel or a missing blob is an error, not an invitation to fabricate

## One engine, formats as data (0.6.0)

Every writer and element reader above flows through ONE generic engine, `flpkit.codec`:

```python
from flpkit import codec
from flpkit.formats import NotesFormat

notes = codec.read(path, NotesFormat(), codec.Target(pattern=1, channel=0))
codec.patch(path, NotesFormat(), codec.Target(pattern=1, channel=0), notes, mode="merge")
```

A `Format` (see `flpkit/formats/`) states one `.flp` element - `locate`, `encode`, `decode`, `verify` - and the engine does the rest: splice, chunk-length fixing, and verify-by-readback live in `codec.patch`, once.
Reading `formats/notes.py` plus `codec.py` tells you everything about how notes work; the same goes for playlist, automation, levels, and tempo.
The public functions (`write_notes`, `set_tempo`, ...) are thin shims over `codec.patch`, and `tests/test_differential.py` proves the codec writes byte-identical output to the pre-0.6.0 hand-rolled writers.

## Detect, don't assume

Format constants are DETECTED from the file itself wherever the data carries an invariant, with hardcoded values only as logged fallbacks:

- The playlist record stride is detected per blob (the constant `pattern_base` signature), never taken from a version table.
- The note-flags word is templated from the target file's own notes; the corpus-surveyed `0x4000` covers files with none.
- The playlist cut window (bytes 24-31) is only written when every existing record in the file exhibits the verified `0..length` relation; otherwise the write refuses.
- Event-size overrides (event 172 is one byte on FL 2026, not the classic four) are an `event_size_overrides` argument on every public function, so a capability profile can supply a measured table for other FL versions; the built-in FL-2026 table is the once-logged fallback.

## How it was verified

Every byte-level fact in the source carries its evidence in a comment.
The facts come from two directions:

1. **Differential reading** against pyflp across 164 FL-authored projects (dev-only oracle; flpkit ships with zero dependencies and no GPL code).
2. **Live FL Studio**: files written by flpkit are opened by real FL Studio 2026 (macOS) and read back over a control connection - tempo, note, and level writes are all confirmed by FL itself, not just by our own parser. The live harness lives in the parent project, [fl-studio-mcp](https://github.com/origami-research/fl-studio-mcp).

One example of why the live half matters: a note-record flags field of `0` parses fine everywhere, but every note FL itself writes carries `0x4000` (surveyed: 24,435 records across FL's bundled projects, not one with `0`).

## Scope, honestly

flpkit models what a composition agent needs: tempo, channels, levels, notes.
It does not (yet) decode plugin state or the mixer graph - those events pass through writes untouched, but `read()` does not expose them. Playlist reading landed in 0.2.0, automation-clip reading in 0.3.0, playlist writing in 0.4.0 (FL-2026 live-verified), automation-point writing in 0.5.0 (corpus-verified byte-identical; live FL round-trip pending).
Automation clip CREATION (a new curve on a new target) stays out until the target-link bytes are decoded by live minimal-pair experiments.

## Bring your own note type

`write_notes` accepts any object with `key` (MIDI int), `start`/`length` (beats), `velocity`/`pan` (0..1) attributes - `NoteSpec` is provided for convenience, but a pydantic model or your own dataclass works as-is.

## License

MIT.
