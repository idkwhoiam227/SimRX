"""Parser for SimTX's active-transmissions snapshot protocol.

Reverse-engineered from a live TCP/TLS capture (port 5556) with explicit
permission from the SimTX developer.

Record layout (repeats until the buffer is exhausted):
    uint16 BE   uuid_len
    bytes       uuid (ASCII, uuid_len bytes, standard UUID-with-dashes form)
    uint16 BE   callsign_len
    bytes       callsign (ASCII/UTF-8)
    uint32 BE   source_type_len   <- NOTE: 4-byte length here, not 2
    bytes       source_type (e.g. "GNU_RADIO_IQ", "MICROPHONE", "AUDIO_STREAM",
                 "WHITE_NOISE")
    float32 BE  frequency_hz      <- confirmed exact match against live UI value
    uint16 BE   mode_len
    bytes       mode ("IQ", "USB", "AM_DSB_FC", "NFM", "WFM", ...)
    ...         remaining bytes: sample rate / channel / stream telemetry,
                 not needed for the callsign DB, left unparsed.

A different, shorter layout is used for "RTLTCP" entries — these are other
players' SDR receivers (rtl_tcp feeds), not transmitting stations, and are
skipped by this parser rather than guessed at.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

_UUID_RE = re.compile(
    rb"\x00\x24([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


@dataclass
class StationSighting:
    uuid: str
    callsign: str
    source_type: str
    freq_hz: float
    mode: str

    @property
    def freq_khz(self) -> float:
        return round(self.freq_hz / 1000, 3)


def _read_str16(buf: bytes, off: int) -> tuple[str, int]:
    length = struct.unpack(">H", buf[off:off + 2])[0]
    return buf[off + 2:off + 2 + length].decode("utf-8", errors="replace"), off + 2 + length


def _read_str32(buf: bytes, off: int) -> tuple[str, int]:
    length = struct.unpack(">I", buf[off:off + 4])[0]
    return buf[off + 4:off + 4 + length].decode("utf-8", errors="replace"), off + 4 + length


def parse_snapshot(data: bytes) -> list[StationSighting]:
    """Parse one active-sessions snapshot message into a list of sightings.

    Records that don't match the expected layout (RTLTCP receiver entries,
    or anything unexpected) are silently skipped rather than raising, since
    a single malformed/unknown record shouldn't drop the rest of the batch.
    """
    boundaries = [m.start() for m in _UUID_RE.finditer(data)]
    if not boundaries:
        return []
    boundaries.append(len(data))

    results: list[StationSighting] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        try:
            off = start
            uuid_str, off = _read_str16(data, off)
            callsign, off = _read_str16(data, off)
            source_type, off = _read_str32(data, off)
            if source_type == "" or len(source_type) > 64:
                # Not the layout we expect (e.g. an RTLTCP-style record) — skip.
                continue
            freq_hz = struct.unpack(">f", data[off:off + 4])[0]
            off += 4
            mode, off = _read_str16(data, off)
            if not (1_000 <= freq_hz <= 2_000_000_000):
                # Sanity check: reject anything that doesn't look like a
                # plausible RF frequency, rather than pushing garbage further
                # down the pipeline.
                continue
            results.append(StationSighting(uuid_str, callsign, source_type, freq_hz, mode))
        except Exception:
            # The protocol was reverse-engineered from two captures, not a
            # spec — isolate against ANY per-record parsing failure here
            # (not just the ones seen so far) rather than letting one bad
            # record take out the rest of the batch. The caller (the
            # mitmproxy addon) is responsible for logging if the whole
            # snapshot comes back suspiciously empty.
            continue
    return results


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as f:
        raw = f.read()
    for s in parse_snapshot(raw):
        print(f"{s.callsign:20s} | {s.source_type:14s} | {s.freq_hz:>12.1f} Hz ({s.freq_khz} kHz) | mode={s.mode}")
