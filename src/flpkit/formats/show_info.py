"""Show info: the project flag (event 10, one byte) that makes FL open the
project's info panel (title, author, comments) over the window on load.

Facts: pyflp's ProjectID.ShowInfo; live on FL Studio 2026 for Windows
(26.1.6, 2026-10-23) a project with ``10 = 1`` opens its info panel on top of
FL, it takes the foreground, and only a click closes it (Escape does not).
FL expresses "off" either as ``10 = 0`` or by omitting the event, so turning
it off patches an existing event in place and leaves a file without one
untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

from flpkit.codec import EVENT_MIXER_FLAGS, FlpError, SpliceSite, Stream, Target, verify_identical

EVENT_SHOW_INFO = 10


class ShowInfoFormat:
    name = "show_info"
    event_id = EVENT_SHOW_INFO
    frame = None

    def locate(self, stream: Stream, target: Target) -> SpliceSite:
        """The project-level event, before any channel or mixer section."""
        prev_end = 0
        for event_id, off, size in stream:
            head = prev_end
            prev_end = off + size
            if event_id == EVENT_SHOW_INFO and size == 1:
                return SpliceSite(head, off + size, bytes(stream[off : off + size]))
            if event_id in (64, EVENT_MIXER_FLAGS):  # a channel or the mixer began
                break
        raise FlpError("the project has no show-info flag (event 10); FL treats that as off")

    def encode(self, data: Sequence[bool], ppq: int) -> bytes:
        if len(data) != 1:
            raise FlpError(f"a project has one show-info flag, got {len(data)}")
        return bytes([1 if data[0] else 0])

    def decode(self, blob: bytes, ppq: int) -> list[bool]:
        return [blob == b"\x01"] if blob else []

    def verify(self, sent: Sequence[bool], readback: Sequence[bool]) -> None:
        verify_identical(sent, readback, "the project's show-info flag")
