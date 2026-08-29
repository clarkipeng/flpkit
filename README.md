# flpkit

Read and write FL Studio `.flp` project files - without FL Studio, without dependencies.

```python
import flpkit

project = flpkit.read(path)          # ppq, tempo, channels (names + levels), notes
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

## What it writes

- `set_tempo` - patches the tempo event in place, or appends one when the file omits it (FL expresses default tempo by omission; end-of-stream append is the placement real FL accepts)
- `write_notes` - splices a pattern's notes blob; `mode="replace"` is scoped to the target channel (a pattern's blob holds *every* channel's notes - naive replacement destroys other channels' work)
- `set_channel_levels` - patches pan/volume/pitch int32s inside the channel's `Levels` event; refuses legacy files rather than writing guessed units

## How it was verified

Every byte-level fact in the source carries its evidence in a comment.
The facts come from two directions:

1. **Differential reading** against pyflp across 164 FL-authored projects (dev-only oracle; flpkit ships with zero dependencies and no GPL code).
2. **Live FL Studio**: files written by flpkit are opened by real FL Studio 2026 (macOS) and read back over a control connection - tempo, note, and level writes are all confirmed by FL itself, not just by our own parser. The live harness lives in the parent project, [fl-studio-mcp](https://github.com/origami-research/fl-studio-mcp).

One example of why the live half matters: a note-record flags field of `0` parses fine everywhere, but every note FL itself writes carries `0x4000` (surveyed: 24,435 records across FL's bundled projects, not one with `0`).

## Scope, honestly

flpkit models what a composition agent needs: tempo, channels, levels, notes.
It does not (yet) decode automation clips, arrangements, plugin state, or the mixer graph - those events pass through writes untouched, but `read()` does not expose them.

## Bring your own note type

`write_notes` accepts any object with `key` (MIDI int), `start`/`length` (beats), `velocity`/`pan` (0..1) attributes - `NoteSpec` is provided for convenience, but a pydantic model or your own dataclass works as-is.

## License

MIT.
