"""Plugin identity: which plugin a channel or mixer slot names, without its state.

A plugin instance in an .flp is a short event tuple (the same events for a rack
channel's generator and a mixer slot's effect):

- ``201`` PluginID.InternalName: the plugin FL loads, UTF-16 text such as
  ``"Fruity Reeverb 2"`` or ``"FL MCP Bridge"``. Every hosted VST/VST3/AU is
  ``"Fruity Wrapper"`` here; the real plugin is named inside ``213``.
- ``203`` PluginID.Name: the user-visible rename, when there is one.
- ``212`` PluginID.Wrapper: window/page state, carried opaquely.
- ``213`` PluginID.Data: the plugin's own state. For ``"Fruity Wrapper"`` it is
  a ``u32`` marker (8 or 10) followed by records of ``u32 id, u64 length,
  data``; record 54 is the plugin name, 55 its file path, 56 its vendor (UTF-8).
  Native plugin state stays opaque.

Facts: the record layout is pyflp's ``VSTPluginEvent`` (ids 54/55/56), checked
against the FL-authored files that host VSTs in FL Studio 2026's bundled
projects. Decoding is bounded by the payload: a record whose length overruns it
ends the walk (the identity is then whatever was read before it).
"""

from __future__ import annotations

from dataclasses import dataclass

WRAPPER_INTERNAL_NAME = "Fruity Wrapper"

EVENT_PLUGIN_WRAPPER = 212
EVENT_PLUGIN_DATA = 213

_VST_NAME = 54
_VST_PATH = 55
_VST_VENDOR = 56
_VST_MARKERS = (8, 10)
_RECORD_HEAD = 12  # u32 id + u64 length


@dataclass(frozen=True)
class FlpVst:
    """The plugin a ``Fruity Wrapper`` hosts, as its state records name it."""

    name: str
    vendor: str = ""
    path: str = ""


@dataclass(frozen=True)
class FlpPlugin:
    """One plugin instance's identity (its state is not decoded)."""

    internal_name: str  # what FL loads; "Fruity Wrapper" for every hosted VST
    name: str = ""  # the user-visible rename (event 203); "" when absent
    vst: FlpVst | None = None  # set when internal_name is "Fruity Wrapper" and the state names it

    @property
    def is_wrapper(self) -> bool:
        """True for a hosted third-party plugin (VST, VST3, AU)."""
        return self.internal_name == WRAPPER_INTERNAL_NAME


def vst_identity(payload: bytes) -> FlpVst | None:
    """Decode a ``Fruity Wrapper``'s state (event 213) to the hosted plugin's
    name, vendor and path; None when the payload is not wrapper state or names
    no plugin."""
    if len(payload) < 4 or int.from_bytes(payload[:4], "little") not in _VST_MARKERS:
        return None
    fields: dict[int, str] = {}
    at = 4
    while at + _RECORD_HEAD <= len(payload):
        record = int.from_bytes(payload[at : at + 4], "little")
        size = int.from_bytes(payload[at + 4 : at + _RECORD_HEAD], "little")
        start = at + _RECORD_HEAD
        if size > len(payload) - start:
            break
        if record in (_VST_NAME, _VST_PATH, _VST_VENDOR):
            fields[record] = payload[start : start + size].decode("utf-8", "replace").rstrip("\0")
        at = start + size
    name = fields.get(_VST_NAME, "")
    if not name:
        return None
    return FlpVst(name=name, vendor=fields.get(_VST_VENDOR, ""), path=fields.get(_VST_PATH, ""))
