"""mitmproxy addon — captures SimTX live station snapshots and forwards
each one to the local dashboard server (server.py) for session tracking.

Usage:
    mitmweb --mode reverse:tls://193.24.208.6:5556 --listen-port 5556 \
            --set tcp_hosts=".*" --set ssl_insecure=true \
            -s sightings_addon.py

Requires simrx-local/server.py to already be running (python server.py)
so there's something listening on LOCAL_SERVER_URL to receive snapshots.

Also requires the SimTX Transmitter (or any client that keeps a session
open — it doesn't need to be actively transmitting) running with its
traffic routed through this proxy, same setup as during manual capture.
"""
from pathlib import Path

from mitmproxy import tcp

from simrx_snapshot_parser import parse_snapshot

LOCAL_SERVER_URL = "http://127.0.0.1:8090/api/ingest"
INGEST_KEY_FILE = Path(__file__).parent / "ingest_key.txt"

# Main scenario: server.py, this addon, and simtx-transmitter.jar itself all
# live on the same machine (e.g. a t520) — in that case nothing below needs
# touching, the key is picked up automatically from ingest_key.txt next to
# this file as soon as server.py creates it on first run.
#
# INGEST_KEY only needs filling in by hand if you ever split the server and
# the collector across different machines — then ingest_key.txt physically
# lives on the other machine and has to be copied here once by hand (check
# server.py's startup log, or just open the file over there).
INGEST_KEY = ""  # leave empty for the "everything on one machine" scenario

SEEN_FILE = Path(__file__).parent / "seen_callsigns.json"

try:
    import requests
except ImportError:
    requests = None

import json


def _load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


class SightingsCapture:
    def __init__(self):
        self.seen_callsigns = _load_seen()
        self._local_server_ok = None  # None = unknown yet, True/False once we've tried
        print(f"[sightings] loaded {len(self.seen_callsigns)} previously-seen callsigns")
        if requests is None:
            print("[sightings] WARNING: 'requests' isn't installed in mitmproxy's Python env — "
                  "run 'pip install requests' there, otherwise nothing gets forwarded anywhere")

    def tcp_message(self, flow: tcp.TCPFlow):
        msg = flow.messages[-1]
        if msg.from_client:
            flow.messages.clear()   # nothing to keep, and the list would grow for the whole connection
            return  # only server -> client snapshots carry station data

        # The protocol was reverse-engineered from two captures. Alpha
        # traffic will eventually contain something the parser wasn't
        # built for (a new source_type, a shifted field, a truncated
        # message) — catch it here so one bad message logs a warning
        # instead of silently killing the whole mitmproxy addon.
        try:
            self._handle_message(msg.content)
        except Exception as e:
            preview = msg.content[:60].hex()
            print(f"[sightings] failed to process a snapshot ({type(e).__name__}: {e}); "
                  f"first 60 bytes: {preview}")
        finally:
            flow.messages.clear()

    def _handle_message(self, content: bytes):
        sightings = parse_snapshot(content)

        new_this_batch = []
        for s in sightings:
            if s.callsign not in self.seen_callsigns:
                self.seen_callsigns.add(s.callsign)
                new_this_batch.append(s)
                print(f"[sightings] NEW callsign: {s.callsign} | {s.mode} | {s.freq_khz} kHz")

        if new_this_batch:
            _save_seen(self.seen_callsigns)

        # Forward even an empty snapshot: the server uses each ingest call
        # to (a) know the collector is alive and (b) sweep stale sessions
        # closed. Skipping empty ones would make "nobody's on air right
        # now" look identical to "the collector died" from the server's
        # point of view.
        self._forward_to_local_server(sightings)

    def _forward_to_local_server(self, sightings):
        if requests is None:
            return
        ingest_key = INGEST_KEY
        if not ingest_key and INGEST_KEY_FILE.exists():
            ingest_key = INGEST_KEY_FILE.read_text(encoding="utf-8").strip()
        if not ingest_key:
            if self._local_server_ok is not False:
                print("[sightings] no ingest key configured — set INGEST_KEY at the top of this file "
                      "(copy it from server.py's startup log on the machine running the dashboard), "
                      f"or run server.py on this same machine so {INGEST_KEY_FILE.name} can be auto-read")
            self._local_server_ok = False
            return
        payload = {
            "stations": [
                {
                    "callsign": s.callsign,
                    "source_type": s.source_type,
                    "mode": s.mode,
                    "freq_khz": s.freq_khz,
                }
                for s in sightings
            ]
        }
        try:
            resp = requests.post(
                LOCAL_SERVER_URL, json=payload, timeout=3,
                headers={"X-Ingest-Key": ingest_key},
            )
            if self._local_server_ok is not True:
                print(f"[sightings] local server reachable, ingest -> {resp.status_code}")
            self._local_server_ok = resp.ok
        except Exception as e:
            if self._local_server_ok is not False:
                print(f"[sightings] local server NOT reachable at {LOCAL_SERVER_URL}: {e} "
                      f"(is `python server.py` running?)")
            self._local_server_ok = False


addons = [SightingsCapture()]
