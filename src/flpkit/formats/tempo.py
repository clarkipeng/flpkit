"""Tempo: the project's u32 tempo event (156), BPM x 1000 (set 137 -> FL
reads back 137000). FL expresses default tempo by omission; when the event is
absent it is APPENDED at the end of the stream (end-of-stream wins by
sequential application - live-verified; mid-stream splices load-but-ignore or
reject depending on position).
"""

from __future__ import annotations

from collections.abc import Sequence

from flpkit.codec import SpliceSite, Stream, Target, verify_identical

EVENT_TEMPO = 156  # u32 payload, BPM x 1000
EVENT_TEMPO_COARSE = 66  # legacy u16 whole BPM (pre-156 files); 156 wins
EVENT_TEMPO_FINE = 93  # legacy u16 milli-BPM fraction added to coarse


class TempoFormat:
    name = "tempo"
    event_id = EVENT_TEMPO

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """The first tempo event, else the end-of-stream append point."""
        prev_end = 0
        for event_id, off, size in stream:
            head = prev_end
            prev_end = off + size
            if event_id == EVENT_TEMPO and size == 4:
                return SpliceSite(head, off + size, bytes(stream[off : off + size]))
        return SpliceSite(len(stream), len(stream), None)

    def encode(self, data: Sequence[float], ppq: int) -> bytes:
        (bpm,) = data  # the project has exactly one tempo
        return round(bpm * 1000).to_bytes(4, "little")

    def decode(self, blob: bytes, ppq: int) -> list[float]:
        return [int.from_bytes(blob, "little") / 1000]

    def verify(self, sent: Sequence[float], readback: Sequence[float]) -> None:
        verify_identical(sent, readback, "the project tempo")
