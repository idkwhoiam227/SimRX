#!/usr/bin/env python3
"""SimRX local callsign dashboard — zero-dependency local server.

Uses only the Python standard library (http.server + sqlite3) so it can be
run with nothing but `python server.py` — no pip install needed.

Run:
    python server.py
Then open:
    http://127.0.0.1:8090/

The sightings_addon.py mitmproxy addon POSTs each live snapshot to
POST /api/ingest here; this server turns that into tracked "sessions"
(continuous on-air periods per callsign) and serves the dashboard UI + API.
"""
import getpass
import itertools
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import auth

DB_PATH = Path(__file__).parent / "simrx_local.db"
STATIC_DIR = Path(__file__).parent / "static"
INGEST_KEY_FILE = Path(__file__).parent / "ingest_key.txt"
HOST = os.environ.get("SIMRX_HOST", "0.0.0.0")  # 0.0.0.0 so other devices on the LAN can reach it
PORT = int(os.environ.get("SIMRX_PORT", "8090"))
STALE_AFTER_SECONDS = 90  # if a callsign hasn't appeared in a snapshot for this long, its session is closed

_lock = threading.Lock()
_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
_conn.row_factory = sqlite3.Row
SESSION_SECRET = None  # set in main() after ensure_session_secret()
INGEST_KEY = None  # set in main() after ensure_ingest_key()


def init_db():
    with _lock, _conn:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT NOT NULL,
                source_type TEXT,
                mode TEXT,
                freq_khz REAL,
                started_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                ended_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_callsign ON sessions(callsign);
            CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(ended_at);

            CREATE TABLE IF NOT EXISTS frequency_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT NOT NULL,
                freq_khz REAL,
                mode TEXT,
                logged_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_freqlog_callsign ON frequency_log(callsign);

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                token_version INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS server_starts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collector_outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL,
                ended_at INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outages_started ON collector_outages(started_at);
            """
        )
        _conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES ('freq_history_enabled', '0')"
        )


def ensure_session_secret():
    secret = get_config("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        set_config("session_secret", secret)
    return secret


def ensure_ingest_key():
    if INGEST_KEY_FILE.exists():
        return INGEST_KEY_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(24)
    INGEST_KEY_FILE.write_text(key, encoding="utf-8")
    return key


def ensure_admin_account():
    """If no admin exists yet, create one interactively right here in the
    terminal — no separate hashing script needed, this is a plain Python
    process so getpass + hashlib just work."""
    count = _conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"]
    if count > 0:
        return
    print("\nNo admin account exists yet — let's create one now.")
    while True:
        username = input("Username: ").strip()
        if username:
            break
        print("Username can't be empty.")
    while True:
        password = getpass.getpass("Password (hidden while typing): ")
        if len(password) >= 4:
            break
        print("Password too short (minimum 4 characters for local use).")
    salt = auth.new_salt()
    password_hash = auth.hash_password(password, salt)
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO admins (username, password_hash, password_salt, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash.hex(), salt.hex(), int(time.time())),
        )
    print(f"Done — admin '{username}' created.\n")


def get_admin_by_username(username):
    row = _conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_admin_by_id(admin_id):
    row = _conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
    return dict(row) if row else None


def is_locked(admin) -> bool:
    return bool(admin.get("locked_until")) and admin["locked_until"] > time.time()


def register_failed_attempt(admin):
    attempts = admin["failed_attempts"] + 1
    locked_until = int(time.time()) + auth.LOCKOUT_SECONDS if attempts >= auth.LOCKOUT_THRESHOLD else None
    with _lock, _conn:
        _conn.execute(
            "UPDATE admins SET failed_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, locked_until, admin["id"]),
        )


def clear_failed_attempts(admin):
    with _lock, _conn:
        _conn.execute("UPDATE admins SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (admin["id"],))


def get_session_admin(cookie_header, secret):
    """Returns the admin dict for a valid, non-revoked session cookie, else None."""
    token = auth.read_cookie(cookie_header)
    if not token:
        return None
    payload = auth.verify_session(token, secret)
    if not payload:
        return None
    admin = get_admin_by_id(payload.get("sub"))
    if not admin or admin["token_version"] != payload.get("tv"):
        return None
    return admin



def get_config(key, default=None):
    row = _conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key, value):
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def period_cutoff(period: str):
    now = int(time.time())
    spans = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
    if period == "all" or period not in spans:
        return 0
    return now - spans[period]


# How long the collector can go silent before it counts as "down" — used
# for the live health dot, the background watchdog log, and for recording
# a gap in collector_outages. Deliberately looser than STALE_AFTER_SECONDS
# (which just closes an individual session) since the real snapshot
# interval was never precisely measured — see the two manual captures this
# protocol was reverse-engineered from.
COLLECTOR_TIMEOUT_SECONDS = 300

SERVER_STARTED_AT = None  # set once in main(), read by /api/server-uptime


def record_server_start():
    global SERVER_STARTED_AT
    SERVER_STARTED_AT = int(time.time())
    with _lock, _conn:
        _conn.execute("INSERT INTO server_starts (started_at) VALUES (?)", (SERVER_STARTED_AT,))


def get_server_uptime():
    rows = _conn.execute(
        "SELECT started_at FROM server_starts ORDER BY started_at DESC LIMIT 50"
    ).fetchall()
    now = int(time.time())
    return {
        "started_at": SERVER_STARTED_AT,
        "uptime_seconds": (now - SERVER_STARTED_AT) if SERVER_STARTED_AT else None,
        "restart_count": _conn.execute("SELECT COUNT(*) AS c FROM server_starts").fetchone()["c"],
        "recent_starts": [r["started_at"] for r in rows],
    }


def get_collector_outages(limit=200):
    rows = _conn.execute(
        "SELECT started_at, ended_at, duration_seconds FROM collector_outages "
        "ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


LAST_INGEST_AT = None  # in-memory cache; source of truth is config['last_ingest_at'] (survives restarts)


def _record_outage_if_any(previous_ingest_at, now):
    """Called right before updating the last-ingest timestamp. If the gap
    since the previous heartbeat (which includes a server restart, since
    that resets the in-process world too) is long enough to count as a
    real outage, persist it so the uptime page can chart it later — this
    only detects outages retroactively, on the next heartbeat after one,
    since there's no background poller watching a silent collector."""
    if previous_ingest_at is None:
        return
    gap = now - previous_ingest_at
    if gap >= COLLECTOR_TIMEOUT_SECONDS:
        _conn.execute(
            "INSERT INTO collector_outages (started_at, ended_at, duration_seconds) VALUES (?, ?, ?)",
            (previous_ingest_at, now, gap),
        )


def _coerce_freq_khz(value):
    """Best-effort float conversion; returns None instead of raising for
    anything malformed, so one bad field can't crash a whole snapshot.
    Also rejects implausible values (negative, or absurdly high) — this is
    the ingest boundary, so it shouldn't blindly trust whatever sent the
    data, even though today that's always our own parser."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if not (0 <= f <= 2_000_000_000):  # 0 .. 2 THz in kHz — generous, but not infinite
        return None
    return f


def ingest_snapshot(stations: list):
    """stations: list of {callsign, source_type, mode, freq_khz}. Tolerant
    of malformed entries (wrong types, missing fields, garbage values) —
    a snapshot from an evolving/reverse-engineered protocol will
    eventually contain something unexpected, and one bad entry shouldn't
    drop the rest of the batch or crash the collector."""
    global LAST_INGEST_AT
    now = int(time.time())
    freq_history_enabled = get_config("freq_history_enabled") == "1"

    with _lock, _conn:
        previous = LAST_INGEST_AT if LAST_INGEST_AT is not None else get_config("last_ingest_at")
        previous = int(previous) if previous is not None else None
        _record_outage_if_any(previous, now)
        LAST_INGEST_AT = now
        _conn.execute(
            "INSERT INTO config (key, value) VALUES ('last_ingest_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(now),),
        )

        for st in stations:
            if not isinstance(st, dict):
                continue
            callsign = str(st.get("callsign") or "").strip()[:100]
            if not callsign:
                continue
            freq_khz = _coerce_freq_khz(st.get("freq_khz"))
            mode = str(st.get("mode"))[:32] if st.get("mode") is not None else None
            source_type = str(st.get("source_type"))[:64] if st.get("source_type") is not None else None

            open_session = _conn.execute(
                "SELECT id FROM sessions WHERE callsign = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (callsign,),
            ).fetchone()

            if open_session:
                _conn.execute(
                    "UPDATE sessions SET last_seen_at = ?, freq_khz = ?, mode = ?, source_type = ? WHERE id = ?",
                    (now, freq_khz, mode, source_type, open_session["id"]),
                )
            else:
                _conn.execute(
                    "INSERT INTO sessions (callsign, source_type, mode, freq_khz, started_at, last_seen_at, ended_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (callsign, source_type, mode, freq_khz, now, now),
                )

            if freq_history_enabled:
                _conn.execute(
                    "INSERT INTO frequency_log (callsign, freq_khz, mode, logged_at) VALUES (?, ?, ?, ?)",
                    (callsign, freq_khz, mode, now),
                )

        # Close sessions that have gone stale (not present in recent snapshots)
        stale_before = now - STALE_AFTER_SECONDS
        _conn.execute(
            "UPDATE sessions SET ended_at = last_seen_at WHERE ended_at IS NULL AND last_seen_at < ?",
            (stale_before,),
        )


def get_collector_status():
    last = LAST_INGEST_AT
    if last is None:
        persisted = get_config("last_ingest_at")
        last = int(persisted) if persisted is not None else None
    if last is None:
        return {"last_ingest_at": None, "seconds_ago": None, "healthy": False}
    ago = int(time.time()) - last
    return {"last_ingest_at": last, "seconds_ago": ago, "healthy": ago < COLLECTOR_TIMEOUT_SECONDS}


def delete_session(session_id):
    with _lock, _conn:
        cur = _conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0


def get_live():
    rows = _conn.execute(
        "SELECT id, callsign, source_type, mode, freq_khz, started_at, last_seen_at "
        "FROM sessions WHERE ended_at IS NULL ORDER BY started_at ASC"
    ).fetchall()
    now = int(time.time())
    return [
        {
            "id": r["id"],
            "callsign": r["callsign"],
            "source_type": r["source_type"],
            "mode": r["mode"],
            "freq_khz": r["freq_khz"],
            "band": band_label(r["freq_khz"]),
            "started_at": r["started_at"],
            "on_air_seconds": now - r["started_at"],
        }
        for r in rows
    ]


def _shape_session_rows(rows):
    now = int(time.time())
    out = []
    for r in rows:
        end = r["ended_at"] if r["ended_at"] is not None else now
        out.append({
            "id": r["id"],
            "callsign": r["callsign"],
            "source_type": r["source_type"],
            "mode": r["mode"],
            "freq_khz": r["freq_khz"],
            "band": band_label(r["freq_khz"]),
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration_seconds": end - r["started_at"],
            "live": r["ended_at"] is None,
        })
    return out


def get_recent_sessions(limit=100):
    rows = _conn.execute(
        "SELECT id, callsign, source_type, mode, freq_khz, started_at, last_seen_at, ended_at "
        "FROM sessions ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    total = _conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    return {"sessions": _shape_session_rows(rows), "total_sessions": total, "limit": limit}


def get_sessions_for_month(year: int, month: int, limit=2000):
    start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc).timestamp())
    rows = _conn.execute(
        "SELECT id, callsign, source_type, mode, freq_khz, started_at, last_seen_at, ended_at "
        "FROM sessions WHERE started_at >= ? AND started_at < ? ORDER BY started_at DESC LIMIT ?",
        (start, end, limit),
    ).fetchall()
    total = _conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE started_at >= ? AND started_at < ?", (start, end)
    ).fetchone()["c"]
    return {"sessions": _shape_session_rows(rows), "total_sessions": total, "limit": limit,
            "range_start": start, "range_end": end}


def _day_bounds_utc(year: int, month: int, day: int):
    start = datetime(year, month, day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def get_sessions_for_day(year: int, month: int, day: int, limit=1000):
    start, end = _day_bounds_utc(year, month, day)
    rows = _conn.execute(
        "SELECT id, callsign, source_type, mode, freq_khz, started_at, last_seen_at, ended_at "
        "FROM sessions WHERE started_at >= ? AND started_at < ? ORDER BY started_at DESC LIMIT ?",
        (start, end, limit),
    ).fetchall()
    total = _conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE started_at >= ? AND started_at < ?", (start, end)
    ).fetchone()["c"]
    return {"sessions": _shape_session_rows(rows), "total_sessions": total, "limit": limit,
            "range_start": start, "range_end": end}


def get_days_with_sessions(year: int, month: int):
    """-> sorted list of day-of-month ints that have at least one session
    starting in them, for the calendar's small activity dots."""
    month_start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    month_end = int(datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc).timestamp())
    rows = _conn.execute(
        "SELECT DISTINCT CAST(strftime('%d', started_at, 'unixepoch') AS INTEGER) AS day "
        "FROM sessions WHERE started_at >= ? AND started_at < ?",
        (month_start, month_end),
    ).fetchall()
    return sorted(r["day"] for r in rows)


def get_records():
    now = int(time.time())
    out = {}
    for period in ["week", "month", "year", "all"]:
        cutoff = period_cutoff(period)
        row = _conn.execute(
            "SELECT callsign, freq_khz, mode, started_at, ended_at, "
            "(COALESCE(ended_at, ?) - started_at) AS duration_seconds "
            "FROM sessions WHERE started_at >= ? ORDER BY duration_seconds DESC LIMIT 1",
            (now, cutoff),
        ).fetchone()
        out[period] = dict(row) if row else None
    return out


# ---------------------------------------------------------------------------
# Band plan
#
# The band plan is read from an SDR++ band-plan JSON (bandplans/general.json by
# default; Ryzerth's "General / Worldwide" plan). Any other plan in the same
# format can be used by pointing SIMRX_BANDPLAN at it. File values are in Hz;
# they're converted to kHz here, like everything else in this project.
#
# Each band is mapped to one of five display groups:
#   ham   - type "amateur" (except CB / PMR446, which have their own handling)
#   swbc  - shortwave broadcast
#   pmr   - PMR446
#   fm    - FM broadcast
#   other - everything else: LW/MW, CB, aviation, marine, satellite, military,
#           and anything the plan doesn't cover at all
#
# When bands overlap (PMR446 sits inside 70cm, Military Sat inside Military
# Air), the NARROWEST matching band wins, so the specific one is used.
# ---------------------------------------------------------------------------
GROUP_LABELS = {
    "ham": "Amateur (Ham)",
    "swbc": "Shortwave Broadcast",
    "pmr": "PMR446",
    "fm": "FM Broadcast",
    "other": "Other Bands",
}
GROUP_ORDER = ["ham", "swbc", "pmr", "fm", "other"]

BANDPLAN_PATH = Path(os.environ.get("SIMRX_BANDPLAN", str(Path(__file__).parent / "bandplans" / "general.json")))

# Bands that aren't in the SDR++ general plan but show up in SimTX (e.g. 868 MHz
# LoRa/SRD) or that the plan simply doesn't list. (lo_khz, hi_khz, group, label)
BAND_EXTRAS = [
    (135.7, 137.8, "ham", "Ham 2200 m"),
    (472, 479, "ham", "Ham 630 m"),
    (863000, 870000, "other", "ISM 868 MHz"),
]

# The plan calls all 15 shortwave broadcast bands "Shortwave Broadcast", which
# would be indistinguishable in a chart; label them by their usual meter-band
# names instead. (lo_khz, hi_khz, label) — an entry is named by where it starts.
SW_BROADCAST_NAMES = [
    (2300, 2495, "120 m"), (3200, 3400, "90 m"), (3900, 4000, "75 m"), (4750, 5060, "60 m"),
    (5900, 6200, "49 m"), (7200, 7450, "41 m"), (9400, 9900, "31 m"), (11600, 12100, "25 m"),
    (13570, 13870, "22 m"), (15100, 15800, "19 m"), (17480, 17900, "16 m"), (18900, 19020, "15 m"),
    (21450, 21850, "13 m"), (25670, 26100, "11 m"),
]

# Tidied display names for the non-amateur bands of the general plan. Names not
# listed here (e.g. from a different plan file) are shown as they appear in the file.
NAME_EN = {
    "Long Wave": "LW Broadcast",
    "Medium Wave": "MW Broadcast",
    "FM Broadcast": "FM Broadcast",
    "CB": "CB 27 MHz",
    "Air Band VOR/ILS": "Air VOR/ILS",
    "Air Band Voice": "Air Voice",
    "Polar Orbiting Satellites": "Polar Satellites",
    "Marine": "Marine VHF",
    "Military Air": "Military Air",
    "Military Sat": "Military Sat",
}
GROUP_BY_NAME = {"PMR446": "pmr", "CB": "other"}  # both are typed "amateur" in the file


def _khz(hz):
    v = hz / 1000.0
    return int(v) if v.is_integer() else v


def _plan_group(name, btype):
    if name in GROUP_BY_NAME:
        return GROUP_BY_NAME[name]
    t = (btype or "").lower()
    if t == "amateur":
        return "ham"
    if t == "broadcast":
        n = name.lower()
        if re.search(r"\bfm\b", n):
            return "fm"
        if "shortwave" in n:
            return "swbc"
    return "other"


def _plan_label(name, lo_khz):
    n = name.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(m|cm)\s+ham band$", n, re.IGNORECASE)
    if m:
        return f"Ham {m.group(1)} {'m' if m.group(2).lower() == 'm' else 'cm'}"
    if n.lower() == "shortwave broadcast":
        for lo, hi, label in SW_BROADCAST_NAMES:
            if lo <= lo_khz < hi:
                return f"SW Bcast {label}"
        return "SW Broadcast"
    return NAME_EN.get(n, n)


def load_band_plan(path):
    """-> (plan, info); plan is a list of (lo_khz, hi_khz, group, label).
    A missing/broken file is reported and skipped, leaving just BAND_EXTRAS."""
    plan, info = [], {"path": str(path), "name": None, "country": None, "author": None, "from_file": 0, "warnings": []}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for b in data.get("bands", []):
            lo, hi = _khz(b["start"]), _khz(b["end"])
            if hi <= lo:
                info["warnings"].append(f"skipped band with invalid range: {b.get('name')}")
                continue
            plan.append((lo, hi, _plan_group(b["name"], b.get("type")), _plan_label(b["name"], lo)))
        info.update(name=data.get("name"), country=data.get("country_name"), author=data.get("author_name"),
                    from_file=len(plan))
    except (OSError, ValueError, KeyError, TypeError) as e:
        info["warnings"].append(f"could not read {path}: {e} — falling back to built-in bands only")
    plan.extend(BAND_EXTRAS)

    # Nested overlaps are fine (narrowest wins); partial overlaps are ambiguous, so say so.
    for a, b in itertools.combinations(plan, 2):
        if a[0] < b[1] and b[0] < a[1]:
            nested = (b[0] <= a[0] and a[1] <= b[1]) or (a[0] <= b[0] and b[1] <= a[1])
            if not nested:
                info["warnings"].append(f"bands partially overlap: {a[3]} and {b[3]}")
    return plan, info


BAND_PLAN, BAND_PLAN_INFO = load_band_plan(BANDPLAN_PATH)

# Full extent of every named band (some, like "Ham 13 cm" or "SW Bcast 60 m", are
# made of several separate pieces) — used for chart tooltips.
BAND_SPANS = {}
for _lo, _hi, _g, _label in BAND_PLAN:
    _old = BAND_SPANS.get((_g, _label))
    BAND_SPANS[(_g, _label)] = (_lo, _hi) if _old is None else (min(_old[0], _lo), max(_old[1], _hi))

# Fallback for frequencies that sit outside every band of the plan.
GENERIC_BANDS = [
    (0, 30, "VLF (0–30 kHz)"),
    (30, 300, "LF (30–300 kHz)"),
    (300, 3_000, "MF (0.3–3 MHz)"),
    (3_000, 30_000, "HF (3–30 MHz)"),
    (30_000, 300_000, "VHF (30–300 MHz)"),
    (300_000, float("inf"), "UHF+ (>300 MHz)"),
]


def classify_band(freq_khz):
    """-> {"group", "band", "lo_khz", "hi_khz"} (hi_khz is None for the open-ended top band),
    or None if the frequency is unknown."""
    if freq_khz is None:
        return None
    best = None
    for lo, hi, group, label in BAND_PLAN:
        if lo <= freq_khz < hi and (best is None or hi - lo < best[1] - best[0]):
            best = (lo, hi, group, label)
    if best:
        return {"group": best[2], "band": best[3], "lo_khz": best[0], "hi_khz": best[1]}
    for lo, hi, label in GENERIC_BANDS:
        if lo <= freq_khz < hi:
            return {"group": "other", "band": f"Other · {label}", "lo_khz": lo,
                    "hi_khz": None if hi == float("inf") else hi}
    return {"group": "other", "band": "Other", "lo_khz": None, "hi_khz": None}


def band_label(freq_khz):
    c = classify_band(freq_khz)
    return c["band"] if c else None


def get_band_popularity():
    rows = _conn.execute("SELECT freq_khz FROM sessions WHERE freq_khz IS NOT NULL").fetchall()
    merged = {}
    group_counts = {g: 0 for g in GROUP_ORDER}
    for r in rows:
        c = classify_band(r["freq_khz"])
        key = (c["group"], c["band"])
        if key not in merged:
            # a band made of several pieces (or the same label reused) is ONE bar
            lo, hi = BAND_SPANS.get(key, (c["lo_khz"], c["hi_khz"]))
            merged[key] = {"group": c["group"], "band": c["band"], "lo_khz": lo, "hi_khz": hi, "count": 0}
        merged[key]["count"] += 1
        group_counts[c["group"]] += 1

    bands = list(merged.values())
    # spectrum order (low -> high), like a dial
    bands.sort(key=lambda x: (x["lo_khz"] is None, x["lo_khz"] or 0))
    groups = [{"group": g, "label": GROUP_LABELS[g], "count": group_counts[g]} for g in GROUP_ORDER]
    return {"groups": groups, "bands": bands}


def get_top_frequency_ranges(limit=15, width_khz=3000):
    """Most-used frequency RANGES: every session is dropped into a fixed-width
    bucket (aligned to multiples of the width, e.g. 3000 kHz -> 102–105 MHz,
    105–108 MHz, ...), which also absorbs float32 noise around a nominal
    frequency. One width per call so bucket counts stay comparable."""
    width = float(width_khz)
    rows = _conn.execute(
        "SELECT CAST(freq_khz / ? AS INTEGER) AS bucket, COUNT(*) AS c FROM sessions "
        "WHERE freq_khz IS NOT NULL AND freq_khz >= 0 "
        "GROUP BY bucket ORDER BY c DESC, bucket ASC LIMIT ?",
        (width, limit),
    ).fetchall()

    def clean(v):
        return int(v) if float(v).is_integer() else v

    return [
        {"lo_khz": clean(r["bucket"] * width), "hi_khz": clean((r["bucket"] + 1) * width), "count": r["c"]}
        for r in rows
    ]


def get_online_stats():
    current = len(get_live())

    rows = _conn.execute("SELECT started_at, ended_at FROM sessions").fetchall()
    if not rows:
        return {"current": current, "peak": 0, "peak_at": None}

    now = int(time.time())
    events = []
    for r in rows:
        end = r["ended_at"] if r["ended_at"] is not None else now
        events.append((r["started_at"], 1))
        events.append((end, -1))
    # process all the +1s at a given timestamp before the -1s, so a station
    # that starts at the exact instant another ends still counts as an
    # overlap for that instant rather than an artificial gap
    events.sort(key=lambda e: (e[0], -e[1]))

    running = 0
    peak = 0
    peak_at = None
    for ts, delta in events:
        running += delta
        if running > peak:
            peak = running
            peak_at = ts

    return {"current": current, "peak": peak, "peak_at": peak_at}


def get_top_callsigns(limit=10):
    """Top callsigns by total time on air, summing across all their sessions
    (ongoing sessions count up to now, not just their last known duration)."""
    now = int(time.time())
    rows = _conn.execute(
        "SELECT callsign, SUM(COALESCE(ended_at, ?) - started_at) AS total_seconds, COUNT(*) AS session_count "
        "FROM sessions GROUP BY callsign ORDER BY total_seconds DESC LIMIT ?",
        (now, limit),
    ).fetchall()
    return [{"callsign": r["callsign"], "total_seconds": r["total_seconds"], "session_count": r["session_count"]} for r in rows]


def get_mode_popularity():
    rows = _conn.execute(
        "SELECT COALESCE(mode, 'unknown') AS mode, COUNT(*) AS c FROM sessions GROUP BY mode ORDER BY c DESC"
    ).fetchall()
    return [{"mode": r["mode"], "count": r["c"]} for r in rows]


def get_hourly_activity():
    """Sessions started, bucketed by local hour-of-day (0-23) — shows when
    people tend to be on air, using SQLite's own local-time conversion of
    the stored unix timestamps."""
    rows = _conn.execute(
        "SELECT CAST(strftime('%H', started_at, 'unixepoch', 'localtime') AS INTEGER) AS hour, COUNT(*) AS c "
        "FROM sessions GROUP BY hour"
    ).fetchall()
    counts = {r["hour"]: r["c"] for r in rows}
    return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]


def get_summary_stats():
    row = _conn.execute(
        "SELECT COUNT(DISTINCT callsign) AS unique_callsigns, COUNT(*) AS total_sessions, "
        "AVG(COALESCE(ended_at, strftime('%s','now')) - started_at) AS avg_duration_seconds FROM sessions"
    ).fetchone()
    return {
        "unique_callsigns": row["unique_callsigns"] or 0,
        "total_sessions": row["total_sessions"] or 0,
        "avg_duration_seconds": round(row["avg_duration_seconds"]) if row["avg_duration_seconds"] else 0,
    }


def get_freq_history(callsign):
    if get_config("freq_history_enabled") != "1":
        return {"enabled": False, "entries": []}
    rows = _conn.execute(
        "SELECT freq_khz, mode, logged_at FROM frequency_log WHERE callsign = ? ORDER BY logged_at ASC",
        (callsign,),
    ).fetchall()
    return {"enabled": True, "entries": [dict(r) for r in rows]}


# ---------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[server] {self.address_string()} - {fmt % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path, status=200):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_css(self, path: Path):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _current_admin(self):
        return get_session_admin(self.headers.get("Cookie"), SESSION_SECRET)

    def _require_admin_or_401(self):
        admin = self._current_admin()
        if not admin:
            self._send_json({"error": "Login required"}, 401)
            return None
        return admin

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._send_html(STATIC_DIR / "index.html")
        elif path == "/login":
            self._send_html(STATIC_DIR / "login.html")
        elif path == "/uptime":
            self._send_html(STATIC_DIR / "uptime.html")
        elif path == "/style.css":
            self._send_css(STATIC_DIR / "style.css")
        elif path == "/api/session":
            admin = self._current_admin()
            self._send_json({"authenticated": bool(admin), "username": admin["username"] if admin else None})
        elif path == "/api/live":
            self._send_json({"stations": get_live()})
        elif path == "/api/collector-status":
            self._send_json(get_collector_status())
        elif path == "/api/server-uptime":
            self._send_json(get_server_uptime())
        elif path == "/api/collector-outages":
            self._send_json({"outages": get_collector_outages()})
        elif path == "/api/sessions/recent":
            try:
                limit = int(qs.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            limit = max(1, min(limit, 1000))
            self._send_json(get_recent_sessions(limit))
        elif path == "/api/sessions/month":
            try:
                year = int(qs.get("year", [""])[0])
                month = int(qs.get("month", [""])[0])
                if not (1 <= month <= 12):
                    raise ValueError
            except (ValueError, TypeError):
                self._send_json({"error": "year and month query params are required (e.g. ?year=2026&month=9)"}, 400)
                return
            self._send_json(get_sessions_for_month(year, month))
        elif path == "/api/sessions/day":
            try:
                year = int(qs.get("year", [""])[0])
                month = int(qs.get("month", [""])[0])
                day = int(qs.get("day", [""])[0])
                if not (1 <= month <= 12):
                    raise ValueError
                datetime(year, month, day)  # validates day is in range for that month
            except (ValueError, TypeError):
                self._send_json({"error": "valid year, month and day query params are required (e.g. ?year=2026&month=9&day=15)"}, 400)
                return
            self._send_json(get_sessions_for_day(year, month, day))
        elif path == "/api/calendar-activity":
            try:
                year = int(qs.get("year", [""])[0])
                month = int(qs.get("month", [""])[0])
                if not (1 <= month <= 12):
                    raise ValueError
            except (ValueError, TypeError):
                self._send_json({"error": "year and month query params are required"}, 400)
                return
            self._send_json({"days": get_days_with_sessions(year, month)})
        elif path == "/api/records":
            self._send_json({"records": get_records()})
        elif path == "/api/stats/bands":
            self._send_json(get_band_popularity())
        elif path == "/api/stats/top-frequencies":
            limit = int(qs.get("limit", ["15"])[0])
            try:
                width_khz = float(qs.get("width_khz", ["3000"])[0])
            except ValueError:
                width_khz = 3000.0
            if not (10 <= width_khz <= 100_000):
                width_khz = 3000.0
            self._send_json({"width_khz": width_khz, "ranges": get_top_frequency_ranges(limit, width_khz)})
        elif path == "/api/stats/online":
            self._send_json(get_online_stats())
        elif path == "/api/stats/top-callsigns":
            limit = int(qs.get("limit", ["10"])[0])
            self._send_json({"callsigns": get_top_callsigns(limit)})
        elif path == "/api/stats/modes":
            self._send_json({"modes": get_mode_popularity()})
        elif path == "/api/stats/hourly":
            self._send_json({"hours": get_hourly_activity()})
        elif path == "/api/stats/summary":
            self._send_json(get_summary_stats())
        elif path == "/api/freq-history":
            callsign = qs.get("callsign", [""])[0]
            self._send_json(get_freq_history(callsign))
        elif path == "/api/config":
            self._send_json({"freq_history_enabled": get_config("freq_history_enabled") == "1"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/login":
            self._handle_login(body)
        elif path == "/api/logout":
            self.send_response(200)
            self.send_header("Set-Cookie", auth.clear_session_cookie())
            self.send_header("Content-Type", "application/json")
            body_bytes = b'{"ok": true}'
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        elif path == "/api/ingest":
            key = self.headers.get("X-Ingest-Key", "")
            if not secrets.compare_digest(key, INGEST_KEY or ""):
                self._send_json({"error": "invalid ingest key"}, 401)
                return
            stations = body.get("stations", [])
            ingest_snapshot(stations)
            self._send_json({"ok": True, "count": len(stations)})
        elif path == "/api/config":
            if self._require_admin_or_401() is None:
                return
            enabled = bool(body.get("freq_history_enabled"))
            set_config("freq_history_enabled", "1" if enabled else "0")
            self._send_json({"ok": True, "freq_history_enabled": enabled})
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_login(self, body):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        generic_error = "Invalid username or password"
        if not username or not password:
            self._send_json({"error": "Enter a username and password"}, 400)
            return

        admin = get_admin_by_username(username)
        if not admin:
            self._send_json({"error": generic_error}, 401)
            return
        if is_locked(admin):
            self._send_json({"error": "Account temporarily locked after several failed attempts. Try again later."}, 423)
            return
        if not auth.verify_password(password, admin["password_salt"], admin["password_hash"]):
            register_failed_attempt(admin)
            self._send_json({"error": generic_error}, 401)
            return

        clear_failed_attempts(admin)
        exp = int(time.time()) + auth.SESSION_TTL_SECONDS
        token = auth.sign_session({"sub": admin["id"], "tv": admin["token_version"], "exp": exp}, SESSION_SECRET)

        body_bytes = json.dumps({"ok": True, "username": admin["username"]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Set-Cookie", auth.make_session_cookie(token))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/sessions/"):
            if self._require_admin_or_401() is None:
                return
            raw_id = path[len("/api/sessions/"):]
            if not raw_id.isdigit():
                self._send_json({"error": "invalid session id"}, 400)
                return
            deleted = delete_session(int(raw_id))
            if not deleted:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)


def reset_admin_password():
    """`python3 server.py --reset-password` — changes the admin's password
    in place, without touching sessions/config. This exists specifically so
    resetting a forgotten password doesn't require deleting simrx_local.db,
    which would also wipe all collected station history."""
    init_db()
    admins = _conn.execute("SELECT id, username FROM admins").fetchall()
    if not admins:
        print("No admin accounts exist yet — just run `python3 server.py` normally, "
              "it will offer to create the first one.")
        return
    if len(admins) == 1:
        admin = admins[0]
        print(f"Changing password for '{admin['username']}'.")
    else:
        print("Multiple admin accounts:")
        for a in admins:
            print(f"  - {a['username']}")
        username = input("Which username do you want to reset? ").strip()
        admin = next((a for a in admins if a["username"] == username), None)
        if not admin:
            print("No such username.")
            return

    while True:
        password = getpass.getpass("New password (hidden while typing): ")
        if len(password) >= 4:
            break
        print("Password too short (minimum 4 characters).")

    salt = auth.new_salt()
    password_hash = auth.hash_password(password, salt)
    with _lock, _conn:
        _conn.execute(
            "UPDATE admins SET password_hash = ?, password_salt = ?, "
            "token_version = token_version + 1, failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (password_hash.hex(), salt.hex(), admin["id"]),
        )
    print(f"Done — password for '{admin['username']}' updated. "
          f"All previous login sessions were invalidated (token_version bumped).")


def _collector_watchdog(poll_seconds=60):
    """Background thread: logs a line to stdout/the service log whenever the
    collector transitions between healthy and silent, so an operator can
    `grep`/monitor the log (e.g. via `journalctl` under systemd) without
    needing a browser tab open on the dashboard."""
    was_healthy = None
    while True:
        time.sleep(poll_seconds)
        try:
            status = get_collector_status()
        except Exception as e:
            print(f"[watchdog] error checking collector status: {e}")
            continue
        healthy = status["healthy"]
        if was_healthy is not None and healthy != was_healthy:
            if healthy:
                print(f"[watchdog] collector recovered (was silent for ~{status['seconds_ago']}s)")
            else:
                print(f"[watchdog] WARNING: collector has gone silent "
                      f"({status['seconds_ago']}s since the last snapshot, threshold is {COLLECTOR_TIMEOUT_SECONDS}s)")
        was_healthy = healthy


def main():
    global SESSION_SECRET, INGEST_KEY
    init_db()
    SESSION_SECRET = ensure_session_secret()
    INGEST_KEY = ensure_ingest_key()
    ensure_admin_account()
    record_server_start()

    watchdog = threading.Thread(target=_collector_watchdog, daemon=True)
    watchdog.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    lan_ip = "?"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    print(f"SimRX local dashboard listening on {HOST}:{PORT}")
    print(f"  Local:   http://127.0.0.1:{PORT}/")
    if HOST == "0.0.0.0":
        print(f"  Network: http://{lan_ip}:{PORT}/  (use this address in sightings_addon.py on another machine)")
    print(f"DB file: {DB_PATH}")
    bp = BAND_PLAN_INFO
    print(f"Band plan: {bp['name']} ({bp['country']}), by {bp['author']} — {bp['from_file']} bands from file "
          f"+ {len(BAND_EXTRAS)} built-in  [{bp['path']}]")
    for w in bp["warnings"]:
        print(f"  [bandplan] WARNING: {w}")
    print(f"Ingest key: {INGEST_KEY}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    import sys
    if "--reset-password" in sys.argv:
        reset_admin_password()
    else:
        main()
