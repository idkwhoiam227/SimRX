"""Map + online-history extension for the SimRX dashboard.

Kept in its own module so server.py only needs a few hook lines:
    migrate(conn)                      - from init_db(), creates columns/tables
    ingest_extras(conn, lock, body)    - from POST /api/ingest, after ingest_snapshot()
    handle_get(handler, path, qs)      - from do_GET(), serves /api/map/*, /api/stats/online-history, /map.js, /map.css

Rules this module enforces:
  * only sessions that have real coordinates ever reach the map; old rows
    (lat/lon NULL) and the placeholder position 0,0 are excluded
  * `address` never enters this system (the collector doesn't forward it)
  * all date ranges are UTC, like the calendar in the Records tab
"""
from __future__ import annotations

import csv
import io
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

# Decimal places kept for coordinates in API responses. None = as received.
# 2 is roughly 1 km, 1 roughly 11 km - set this if you ever want a coarser public map.
MAP_COORD_DECIMALS = None

SAMPLE_EVERY_SECONDS = 30      # online history resolution
RECEIVER_FRESH_SECONDS = 300   # receivers not refreshed for this long disappear from "now"
MAX_TIMELINE_ROWS = 20000
MAX_AGGREGATE_ROWS = 5000
MAX_TIMELINE_DAYS = 31

_state = {"last_sample": 0}


# ------------------------------------------------------------------ helpers --
def _coord(lat, lon):
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(la) and math.isfinite(lo)):
        return None, None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None, None
    if abs(la) < 1e-9 and abs(lo) < 1e-9:  # 0,0 = "position not set"
        return None, None
    return la, lo


def _num(value, lo=0.0, hi=1e12):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or not (lo <= f <= hi):
        return None
    return f


def _int(value, lo=0, hi=10_000_000):
    f = _num(value, lo, hi)
    return None if f is None else int(f)


def _r(v):
    if v is None or MAP_COORD_DECIMALS is None:
        return v
    return round(v, MAP_COORD_DECIMALS)


# ---------------------------------------------------------------- migration --
def migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    for name in ("lat", "lon", "power_w"):
        if name not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS online_samples ("
        " ts INTEGER PRIMARY KEY, clients INTEGER NOT NULL, stations INTEGER NOT NULL,"
        " transmitters INTEGER NOT NULL, receivers INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS receivers_now ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, callsign TEXT, center_khz REAL,"
        " lat REAL NOT NULL, lon REAL NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_pos ON sessions(started_at) WHERE lat IS NOT NULL")
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(frequency_log)")}
    for name, typ in (("lat", "REAL"), ("lon", "REAL"), ("power_w", "REAL"), ("reason", "TEXT")):
        if name not in fcols:
            conn.execute(f"ALTER TABLE frequency_log ADD COLUMN {name} {typ}")


# ------------------------------------------------------------------- ingest --
# ----------------------------------------------------------- frequency log --
def _differs(a, b, tol=1e-6):
    """True only when both values are known and really different (unknown never counts as a change)."""
    if a is None or b is None:
        return False
    if isinstance(a, str) or isinstance(b, str):
        return a != b
    return abs(a - b) > tol


def _change_reasons(prev, cur):
    out = []
    if _differs(prev["freq_khz"], cur["freq_khz"], 1e-3):
        out.append("freq")
    if _differs(prev["mode"], cur["mode"]):
        out.append("mode")
    if _differs(prev["power_w"], cur["power_w"], 1e-3):
        out.append("power")
    if _differs(prev["lat"], cur["lat"]) or _differs(prev["lon"], cur["lon"]):
        out.append("position")
    return out


def _log_frequency_changes(conn, stations, now):
    """Frequency log: one row when a callsign starts, and one row each time its
    frequency, mode, power or position changes (never once per snapshot)."""
    row = conn.execute("SELECT value FROM config WHERE key = 'freq_history_enabled'").fetchone()
    if not row or row[0] != "1":
        return
    for st in stations[:5000]:
        if not isinstance(st, dict):
            continue
        callsign = str(st.get("callsign") or "").strip()[:100]
        if not callsign:
            continue
        la, lo = _coord(st.get("lat"), st.get("lon"))
        cur = {"freq_khz": _num(st.get("freq_khz"), 0, 1e9),
               "mode": str(st["mode"])[:32] if st.get("mode") is not None else None,
               "power_w": _num(st.get("power_w"), 0, 1e7), "lat": la, "lon": lo}
        prev = conn.execute(
            "SELECT freq_khz, mode, power_w, lat, lon, logged_at FROM frequency_log WHERE callsign = ? ORDER BY id DESC LIMIT 1",
            (callsign,)).fetchone()
        sess = conn.execute(
            "SELECT started_at, last_seen_at FROM sessions WHERE callsign = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (callsign,)).fetchone()
        reasons = []
        # new transmission = the newest log row is older than the open session itself
        if prev is None or (sess is not None and prev["logged_at"] < sess["started_at"]):
            reasons.append("start")
        if prev is not None:
            reasons += _change_reasons(prev, cur)
        if not reasons:
            continue
        conn.execute(
            "INSERT INTO frequency_log (callsign, freq_khz, mode, logged_at, lat, lon, power_w, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (callsign, cur["freq_khz"], cur["mode"], now, la, lo, cur["power_w"], ",".join(reasons)))


def freqlog_cleanup(conn, lock, action):
    """Admin tool. Touches ONLY the frequency_log table.
    compact: drop rows that repeat the previous row of the same callsign (keeps real changes)
    clear:   delete the whole frequency log"""
    with lock:
        total = conn.execute("SELECT COUNT(*) FROM frequency_log").fetchone()[0]
        if action == "clear":
            with conn:
                conn.execute("DELETE FROM frequency_log")
            removed, kept = total, 0
        elif action == "compact":
            to_delete, relabel, last_cs, last = [], [], None, None
            for r in conn.execute("SELECT id, callsign, freq_khz, mode, power_w, lat, lon, reason "
                                  "FROM frequency_log ORDER BY callsign, logged_at, id"):
                if r["callsign"] != last_cs:
                    last_cs, last = r["callsign"], None
                key = (r["freq_khz"], r["mode"], r["power_w"], r["lat"], r["lon"])
                if last is not None and key == last[0]:
                    to_delete.append(r["id"])
                    continue
                if r["reason"] is None:   # old row: work out why it was a change
                    why = ["start"] if last is None else _change_reasons(last[1], r)
                    if why:
                        relabel.append((",".join(why), r["id"]))
                last = (key, r)
            with conn:
                for i in range(0, len(to_delete), 500):
                    chunk = to_delete[i:i + 500]
                    conn.execute(f"DELETE FROM frequency_log WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                conn.executemany("UPDATE frequency_log SET reason = ? WHERE id = ?", relabel)
            removed, kept = len(to_delete), total - len(to_delete)
        else:
            raise ValueError("action must be compact or clear")
        try:
            conn.commit()
            conn.execute("VACUUM")      # give the freed space back to the file
        except Exception:
            pass
    return {"removed": removed, "kept": kept}


def ingest_extras(conn, lock, body):
    """Positions/power for open sessions, online sample, current receivers.
    Runs after server.ingest_snapshot(), so every transmitting callsign already
    has an open session. Never raises on malformed input."""
    if not isinstance(body, dict):
        return
    now = int(time.time())
    stations = body.get("stations") if isinstance(body.get("stations"), list) else []
    online = body.get("online") if isinstance(body.get("online"), dict) else None
    receivers = body.get("receivers") if isinstance(body.get("receivers"), list) else None

    with lock, conn:
        for st in stations[:5000]:
            if not isinstance(st, dict):
                continue
            callsign = str(st.get("callsign") or "").strip()[:100]
            if not callsign:
                continue
            la, lo = _coord(st.get("lat"), st.get("lon"))
            power = _num(st.get("power_w"), 0, 1e7)
            if la is None and power is None:
                continue
            conn.execute(
                "UPDATE sessions SET lat = COALESCE(?, lat), lon = COALESCE(?, lon), "
                "power_w = COALESCE(?, power_w) WHERE callsign = ? AND ended_at IS NULL",
                (la, lo, power, callsign),
            )

        _log_frequency_changes(conn, stations, now)

        if online and now - _state["last_sample"] >= SAMPLE_EVERY_SECONDS:
            vals = [_int(online.get(k)) for k in ("clients", "stations", "transmitters", "receivers")]
            if all(v is not None for v in vals):
                conn.execute(
                    "INSERT OR REPLACE INTO online_samples (ts, clients, stations, transmitters, receivers) "
                    "VALUES (?, ?, ?, ?, ?)", (now, *vals),
                )
                _state["last_sample"] = now

        if receivers is not None:
            conn.execute("DELETE FROM receivers_now")
            for rx in receivers[:2000]:
                if not isinstance(rx, dict):
                    continue
                la, lo = _coord(rx.get("lat"), rx.get("lon"))
                if la is None:
                    continue
                center_khz = _num(rx.get("center_khz"), 0, 2e9)
                conn.execute(
                    "INSERT INTO receivers_now (callsign, center_khz, lat, lon, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (str(rx.get("callsign") or "")[:100], center_khz, la, lo, now),
                )


# ------------------------------------------------------------------ leaders --
LEADER_MARGIN_SECONDS = 5     # a challenger must be this far ahead to take a place
LEADER_SLOTS = 10
_leader_order = []
_leader_lock = threading.Lock()


def leader_ranks(conn, now=None):
    """{callsign: 1|2|3} for the callsigns with the most total time on air, ever.
    Two callsigns a few seconds apart would otherwise swap places on every refresh
    (their running totals tick at different moments), so an existing order only
    changes when the challenger is clearly ahead. Open sessions count up to `now`."""
    now = int(now or time.time())
    rows = conn.execute(
        "SELECT callsign, SUM(COALESCE(ended_at, ?) - started_at) AS t FROM sessions "
        "GROUP BY callsign ORDER BY t DESC LIMIT ?", (now, LEADER_SLOTS * 2)).fetchall()
    totals = {r["callsign"]: r["t"] or 0 for r in rows}
    with _leader_lock:
        order = [c for c in _leader_order if c in totals]
        order += [c for c in sorted(totals, key=lambda c: -totals[c]) if c not in order]
        changed = True
        while changed:
            changed = False
            for i in range(len(order) - 1):
                a, b = order[i], order[i + 1]
                if totals[b] > totals[a] + LEADER_MARGIN_SECONDS:
                    order[i], order[i + 1] = b, a
                    changed = True
        _leader_order[:] = order[:LEADER_SLOTS]
        return {c: i + 1 for i, c in enumerate(order[:3])}


# ------------------------------------------------------------------ queries --
def get_now(conn, band_label):
    now = int(time.time())
    rows = conn.execute(
        "SELECT callsign, source_type, mode, freq_khz, power_w, lat, lon, started_at "
        "FROM sessions WHERE ended_at IS NULL AND lat IS NOT NULL AND lon IS NOT NULL "
        "ORDER BY started_at ASC"
    ).fetchall()
    total_open = conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
    rx = conn.execute(
        "SELECT callsign, center_khz, lat, lon FROM receivers_now WHERE updated_at >= ?",
        (now - RECEIVER_FRESH_SECONDS,),
    ).fetchall()
    return {
        "updated_at": now,
        "transmitters": [
            {"callsign": r["callsign"], "source_type": r["source_type"], "mode": r["mode"],
             "freq_khz": r["freq_khz"], "band": band_label(r["freq_khz"]), "power_w": r["power_w"],
             "lat": _r(r["lat"]), "lon": _r(r["lon"]), "started_at": r["started_at"]}
            for r in rows
        ],
        "transmitters_without_position": max(0, total_open - len(rows)),
        "receivers": [
            {"callsign": r["callsign"], "center_khz": r["center_khz"], "lat": _r(r["lat"]), "lon": _r(r["lon"])}
            for r in rx
        ],
    }


def parse_range(qs):
    """-> (start, end_exclusive, label, span_days). Raises ValueError on bad input.
    day: that date | week: 7 days ending on that date | month/year: the calendar
    month/year containing that date | all | custom: from..to inclusive. All UTC."""
    def q(name, default=""):
        return (qs.get(name, [default])[0] or default).strip()

    def d(s):
        return datetime.strptime(s, "%Y-%m-%d").date()

    today = datetime.now(timezone.utc).date()
    period = q("period", "day")
    anchor = d(q("date")) if q("date") else today

    def ts(day):  # midnight UTC
        return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())

    if period == "day":
        first, last = anchor, anchor
    elif period == "week":
        first, last = anchor - timedelta(days=6), anchor
    elif period == "month":
        first = anchor.replace(day=1)
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt - timedelta(days=1)
    elif period == "year":
        first, last = date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    elif period == "custom":
        first, last = d(q("from")), d(q("to"))
        if last < first:
            first, last = last, first
        if (last - first).days > 3660:
            raise ValueError("range too long")
    elif period == "all":
        return 0, int(time.time()) + 1, "All time", None
    else:
        raise ValueError("unknown period")
    start, end = ts(first), ts(last) + 86400
    label = first.isoformat() if first == last else f"{first.isoformat()} → {last.isoformat()}"
    return start, end, label, (last - first).days + 1


BAND_GROUPS = {"mf": (0, 3000), "hf": (3000, 30000), "vhf": (30000, 300000), "uhf": (300000, 1e12)}   # kHz


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def parse_filters(qs):
    """-> (callsign substring or None, band group or None)"""
    q = (qs.get("q", [""])[0] or "").strip()[:50] or None
    band = (qs.get("band", [""])[0] or "").strip().lower()
    return q, (band if band in BAND_GROUPS else None)


def _filter_sql(q, band):
    sql, params = "", []
    if q:
        sql += " AND callsign LIKE ? ESCAPE '\\'"
        params.append(_like(q))
    if band:
        lo, hi = BAND_GROUPS[band]
        sql += " AND freq_khz >= ? AND freq_khz < ?"
        params += [lo, hi]
    return sql, params


def get_range(conn, start, end, view, q=None, band=None):
    fsql, fparams = _filter_sql(q, band)
    no_pos = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE started_at >= ? AND started_at < ? AND lat IS NULL" + fsql,
        (start, end, *fparams),
    ).fetchone()[0]
    if view == "timeline":
        rows = conn.execute(
            "SELECT id, callsign, source_type, mode, freq_khz, power_w, lat, lon, started_at, ended_at, last_seen_at "
            "FROM sessions WHERE started_at >= ? AND started_at < ? AND lat IS NOT NULL AND lon IS NOT NULL" + fsql +
            " ORDER BY started_at ASC LIMIT ?", (start, end, *fparams, MAX_TIMELINE_ROWS + 1),
        ).fetchall()
        truncated = len(rows) > MAX_TIMELINE_ROWS
        rows = rows[:MAX_TIMELINE_ROWS]
        return {
            "view": "timeline", "range_start": start, "range_end": end, "truncated": truncated,
            "sessions_without_position": no_pos,
            "sessions": [
                {"id": r["id"], "callsign": r["callsign"], "source_type": r["source_type"], "mode": r["mode"],
                 "freq_khz": r["freq_khz"], "power_w": r["power_w"], "lat": _r(r["lat"]), "lon": _r(r["lon"]),
                 "started_at": r["started_at"], "ended_at": r["ended_at"] or r["last_seen_at"],
                 "open": r["ended_at"] is None}
                for r in rows
            ],
        }
    rows = conn.execute(
        "SELECT callsign, ROUND(lat, 3) AS lat, ROUND(lon, 3) AS lon, COUNT(*) AS tx_count, "
        "SUM(COALESCE(ended_at, last_seen_at) - started_at) AS total_seconds, MAX(started_at) AS last_at "
        "FROM sessions WHERE started_at >= ? AND started_at < ? AND lat IS NOT NULL AND lon IS NOT NULL" + fsql +
        " GROUP BY callsign, ROUND(lat, 3), ROUND(lon, 3) ORDER BY tx_count DESC, callsign LIMIT ?",
        (start, end, *fparams, MAX_AGGREGATE_ROWS + 1),
    ).fetchall()
    truncated = len(rows) > MAX_AGGREGATE_ROWS
    rows = rows[:MAX_AGGREGATE_ROWS]
    return {
        "view": "aggregate", "range_start": start, "range_end": end, "truncated": truncated,
        "sessions_without_position": no_pos,
        "places": [
            {"callsign": r["callsign"], "lat": _r(r["lat"]), "lon": _r(r["lon"]), "tx_count": r["tx_count"],
             "total_seconds": r["total_seconds"] or 0, "last_at": r["last_at"]}
            for r in rows
        ],
    }


# -------------------------------------------------------------- callsign profile --
def _top(d, key, n=8):
    return sorted(d.values(), key=lambda x: (-x[key], str(x.get("name", ""))))[:n]


def get_profile(conn, name, band_label):
    name = (name or "").strip()
    if not name or len(name) > 100:
        return {"found": False, "callsign": name}
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, callsign, source_type, mode, freq_khz, started_at, ended_at, last_seen_at, lat, lon, power_w "
        "FROM sessions WHERE callsign = ? COLLATE NOCASE ORDER BY started_at DESC, id DESC", (name,)).fetchall()
    if not rows:
        return {"found": False, "callsign": name}

    def end_of(r):
        return r["ended_at"] if r["ended_at"] is not None else max(now, r["started_at"])

    bands, modes, sources, freqs, places = {}, {}, {}, {}, {}
    hourly, days, daily = [0] * 24, set(), {}
    total, longest, longest_at, first_seen, last_seen = 0, 0, None, rows[-1]["started_at"], 0
    for r in rows:
        e = end_of(r)
        d = max(0, e - r["started_at"])
        total += d
        if d > longest:
            longest, longest_at = d, r["started_at"]
        last_seen = max(last_seen, e if r["ended_at"] is not None else now)
        days.add(time.strftime("%Y-%m-%d", time.gmtime(r["started_at"])))
        label = band_label(r["freq_khz"]) if r["freq_khz"] is not None else "Unknown"
        for table, key, nm in ((bands, label, label), (modes, r["mode"] or "Unknown", None),
                               (sources, r["source_type"] or "Unknown", None)):
            ent = table.setdefault(key, {"name": key, "sessions": 0, "seconds": 0})
            ent["sessions"] += 1
            ent["seconds"] += d
        if r["freq_khz"] is not None:
            f = round(r["freq_khz"], 3)
            ent = freqs.setdefault(f, {"name": str(f), "freq_khz": f, "sessions": 0, "seconds": 0})
            ent["sessions"] += 1
            ent["seconds"] += d
        if r["lat"] is not None and r["lon"] is not None:
            k = (round(r["lat"], 3), round(r["lon"], 3))
            ent = places.setdefault(k, {"lat": _r(k[0]), "lon": _r(k[1]), "sessions": 0, "last_at": 0})
            ent["sessions"] += 1
            ent["last_at"] = max(ent["last_at"], r["started_at"])
        h0, h1 = r["started_at"] // 3600, e // 3600
        for h in ({x % 24 for x in range(h0, h1 + 1)} if h1 - h0 < 24 else range(24)):
            hourly[h] += 1
        # seconds per UTC day, last 90 days only
        t = r["started_at"]
        while t < e and t > now - 90 * 86400 - 86400:
            day_end = (t // 86400 + 1) * 86400
            seg = min(e, day_end) - t
            key = time.strftime("%Y-%m-%d", time.gmtime(t))
            daily[key] = daily.get(key, 0) + seg
            t = day_end

    totals = conn.execute(
        "SELECT SUM(COALESCE(ended_at, ?) - started_at) AS t FROM sessions GROUP BY LOWER(callsign)", (now,)).fetchall()
    rank = 1 + sum(1 for x in totals if (x["t"] or 0) > total)

    ch = conn.execute(
        "SELECT freq_khz, mode, logged_at, lat, lon, power_w, reason FROM frequency_log "
        "WHERE callsign = ? COLLATE NOCASE ORDER BY logged_at DESC, id DESC LIMIT 50", (name,)).fetchall()
    enabled = conn.execute("SELECT value FROM config WHERE key = 'freq_history_enabled'").fetchone()
    return {
        "found": True, "callsign": rows[0]["callsign"], "sessions": len(rows), "total_seconds": total,
        "longest_seconds": longest, "longest_at": longest_at, "first_seen": first_seen, "last_seen": last_seen,
        "on_air": any(r["ended_at"] is None for r in rows), "days_active": len(days), "rank": rank,
        "bands": _top(bands, "seconds"), "modes": _top(modes, "seconds"), "sources": _top(sources, "sessions"),
        "frequencies": _top(freqs, "seconds"),
        "places": sorted(places.values(), key=lambda x: -x["sessions"])[:200],
        "places_total": len(places),
        "hourly_utc": hourly,
        "daily": [{"day": k, "seconds": v} for k, v in sorted(daily.items())],
        "recent": [{"id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                    "duration": max(0, end_of(r) - r["started_at"]), "freq_khz": r["freq_khz"], "mode": r["mode"],
                    "source_type": r["source_type"], "lat": _r(r["lat"]), "lon": _r(r["lon"]),
                    "power_w": r["power_w"], "live": r["ended_at"] is None} for r in rows[:25]],
        "changes": [dict(c) for c in ch], "changes_enabled": bool(enabled and enabled[0] == "1"),
    }


def search_callsigns(conn, q, limit=10):
    q = (q or "").strip()[:50]
    if not q:
        return []
    rows = conn.execute(
        "SELECT callsign, COUNT(*) AS n, MAX(started_at) AS last_at FROM sessions "
        "WHERE callsign LIKE ? ESCAPE '\\' GROUP BY LOWER(callsign) LIMIT 200", (_like(q),)).fetchall()
    ql = q.lower()
    rows = sorted(rows, key=lambda r: (not r["callsign"].lower().startswith(ql), -r["n"], r["callsign"].lower()))
    return [{"callsign": r["callsign"], "sessions": r["n"], "last_at": r["last_at"]} for r in rows[:limit]]


# ------------------------------------------------------------------- CSV export --
MAX_EXPORT_ROWS = 100_000


def _csv_safe(v):
    """Callsigns are typed by strangers: stop spreadsheets from running them as formulas."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_sessions_csv(conn, band_label, start=None, end=None, q=None, band=None, callsign=None):
    where, params = "WHERE 1 = 1", []
    if start is not None:
        where += " AND started_at >= ? AND started_at < ?"
        params += [start, end]
    if callsign:
        where += " AND callsign = ? COLLATE NOCASE"
        params.append(callsign)
    fsql, fparams = _filter_sql(q, band)
    rows = conn.execute(
        "SELECT id, callsign, source_type, mode, freq_khz, started_at, ended_at, last_seen_at, lat, lon, power_w "
        f"FROM sessions {where}{fsql} ORDER BY started_at DESC LIMIT ?", (*params, *fparams, MAX_EXPORT_ROWS)).fetchall()
    now = int(time.time())
    iso = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) if t else ""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["id", "callsign", "source_type", "mode", "frequency_khz", "band", "started_utc", "ended_utc",
                "duration_seconds", "lat", "lon", "power_w", "on_air"])
    for r in rows:
        e = r["ended_at"] if r["ended_at"] is not None else now
        w.writerow([r["id"], _csv_safe(r["callsign"]), _csv_safe(r["source_type"] or ""), _csv_safe(r["mode"] or ""),
                    r["freq_khz"] if r["freq_khz"] is not None else "",
                    _csv_safe(band_label(r["freq_khz"]) if r["freq_khz"] is not None else ""),
                    iso(r["started_at"]), iso(r["ended_at"]), max(0, e - r["started_at"]),
                    _r(r["lat"]) if r["lat"] is not None else "", _r(r["lon"]) if r["lon"] is not None else "",
                    r["power_w"] if r["power_w"] is not None else "", "yes" if r["ended_at"] is None else "no"])
    return ("\ufeff" + out.getvalue()).encode("utf-8"), len(rows)


ONLINE_SPANS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400, "all": None}
SILENT_AFTER_SECONDS = 120   # no data for this long => the collector is considered down right now


def _outages(conn, start, now, last_data):
    rows = conn.execute(
        "SELECT started_at, ended_at FROM collector_outages WHERE ended_at > ? AND started_at < ? ORDER BY started_at",
        (start, now),
    ).fetchall()
    out = [(r["started_at"], r["ended_at"]) for r in rows]
    if last_data is not None and now - last_data > SILENT_AFTER_SECONDS:
        out.append((last_data, now))   # silent right now, not logged as an outage yet
    return out


def get_online(conn, period):
    """History of how many stations transmit / receive.
    TX is rebuilt from the sessions table, so it works for all data ever recorded,
    including rows from before coordinates existed. RX only exists from the moment
    the collector started sending receiver lists (online_samples); earlier it is None.
    A bucket is "unknown" (None) before the first record and while the collector was down."""
    now = int(time.time())
    first_s = conn.execute("SELECT MIN(started_at) FROM sessions").fetchone()[0]
    first_o = conn.execute("SELECT MIN(ts) FROM online_samples").fetchone()[0]
    firsts = [x for x in (first_s, first_o) if x is not None]
    if not firsts:
        return {"period": period, "from": now - (ONLINE_SPANS[period] or 86400), "to": now, "bucket_seconds": 60,
                "points": [], "peak_tx": None, "peak_rx": None, "avg_tx": None, "avg_rx": None, "current": None}
    first_data = min(firsts)
    span = ONLINE_SPANS[period]
    start = first_data if span is None else now - span
    bucket = max(SAMPLE_EVERY_SECONDS, math.ceil(max(now - start, 1) / 600))
    t0 = start - start % bucket

    last_s = conn.execute("SELECT MAX(last_seen_at) FROM sessions").fetchone()[0]
    last_o = conn.execute("SELECT MAX(ts) FROM online_samples").fetchone()[0]
    lasts = [x for x in (last_s, last_o) if x is not None]
    last_data = max(lasts) if lasts else None
    outages = _outages(conn, start, now, last_data)

    # --- TX: concurrency sweep over sessions
    rows = conn.execute(
        "SELECT started_at, ended_at FROM sessions WHERE started_at < ? AND COALESCE(ended_at, ?) > ?",
        (now, now, t0),
    ).fetchall()
    ev = []
    for r in rows:
        a = max(r["started_at"], t0)
        b = min(r["ended_at"] if r["ended_at"] is not None else now, now)
        if b > a:
            ev.append((a, 1))
            ev.append((b, -1))
    ev.sort(key=lambda e: (e[0], e[1]))      # at equal times, ends before starts

    # --- RX: online_samples, bucketed
    rx = {r["b"]: r["r"] for r in conn.execute(
        "SELECT (ts / ?) * ? AS b, AVG(receivers) AS r FROM online_samples WHERE ts >= ? GROUP BY b", (bucket, bucket, t0))}

    points, i, cur, n = [], 0, 0, len(ev)
    peak_tx, peak_tx_at = 0, None
    for b in range(t0, now + bucket, bucket):
        b1 = b + bucket
        area, last = 0.0, b
        while i < n and ev[i][0] < b1:
            t, d = ev[i]
            area += cur * (t - last)
            last = t
            cur += d
            if cur > peak_tx and t >= start:
                peak_tx, peak_tx_at = cur, t
            i += 1
        area += cur * (b1 - last)
        covered = sum(max(0, min(b1, oe) - max(b, os_)) for os_, oe in outages)
        known = b1 > first_data and covered * 2 < bucket
        points.append({"t": b, "tx": round(area / bucket, 1) if known else None,
                       "rx": round(rx[b], 1) if b in rx else None})

    tx_vals = [p["tx"] for p in points if p["tx"] is not None]
    rx_vals = [p["rx"] for p in points if p["rx"] is not None]
    prx = conn.execute(
        "SELECT receivers, ts FROM online_samples WHERE ts >= ? ORDER BY receivers DESC, ts DESC LIMIT 1", (start,)
    ).fetchone()

    cur_tx = conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
    smp = conn.execute("SELECT ts, clients, receivers FROM online_samples ORDER BY ts DESC LIMIT 1").fetchone()
    fresh = smp is not None and now - smp["ts"] <= RECEIVER_FRESH_SECONDS
    return {
        "period": period, "from": start, "to": now, "bucket_seconds": bucket, "points": points,
        "peak_tx": {"value": peak_tx, "at": peak_tx_at} if peak_tx_at is not None else None,
        "peak_rx": {"value": prx["receivers"], "at": prx["ts"]} if prx else None,
        "avg_tx": round(sum(tx_vals) / len(tx_vals), 1) if tx_vals else None,
        "avg_rx": round(sum(rx_vals) / len(rx_vals), 1) if rx_vals else None,
        # clients = what the server itself reports; shown only as a reference, never charted
        "current": {"tx": cur_tx, "rx": smp["receivers"] if fresh else None,
                    "server_clients": smp["clients"] if fresh else None, "ts": now},
    }


# ------------------------------------------------------------------- routes --
_STATIC = {"/map.js": ("map.js", "application/javascript; charset=utf-8"),
           "/extra.css": ("extra.css", "text/css; charset=utf-8"),
           "/profile.js": ("profile.js", "application/javascript; charset=utf-8"),
           "/map.css": ("map.css", "text/css; charset=utf-8")}


def handle_get(handler, path, qs, conn, lock, band_label):
    """Returns True if the path was ours (response already sent)."""
    if path in _STATIC:
        name, ctype = _STATIC[path]
        try:
            body = (STATIC_DIR / name).read_bytes()
        except FileNotFoundError:
            handler._send_json({"error": "not found"}, 404)
            return True
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if path == "/api/map/now":
        with lock:
            data = get_now(conn, band_label)
        handler._send_json(data)
        return True

    if path == "/api/map/sessions":
        try:
            start, end, label, span_days = parse_range(qs)
        except ValueError as e:
            handler._send_json({"error": f"bad range: {e}"}, 400)
            return True
        view = qs.get("view", ["aggregate"])[0]
        q, band = parse_filters(qs)
        if view == "timeline" and (span_days is None or span_days > MAX_TIMELINE_DAYS):
            handler._send_json({"error": f"timeline is limited to {MAX_TIMELINE_DAYS} days"}, 400)
            return True
        with lock:
            data = get_range(conn, start, end, "timeline" if view == "timeline" else "aggregate", q, band)
        data["label"], data["span_days"] = label, span_days
        handler._send_json(data)
        return True

    if path == "/api/callsign":
        with lock:
            data = get_profile(conn, qs.get("name", [""])[0], band_label)
        handler._send_json(data)
        return True

    if path == "/api/callsigns/search":
        with lock:
            data = search_callsigns(conn, qs.get("q", [""])[0])
        handler._send_json({"results": data})
        return True

    if path == "/api/export/sessions.csv":
        callsign = (qs.get("callsign", [""])[0] or "").strip()[:100] or None
        q, band = parse_filters(qs)
        start = end = None
        label = "all"
        if "period" in qs:
            try:
                start, end, label, _ = parse_range(qs)
            except ValueError as e:
                handler._send_json({"error": f"bad range: {e}"}, 400)
                return True
        if callsign:
            label = callsign
        with lock:
            body, _n = export_sessions_csv(conn, band_label, start, end, q, band, callsign)
        fname = "simrx-sessions-" + re.sub(r"[^A-Za-z0-9._-]+", "_", str(label)).strip("_")[:60] + ".csv"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/csv; charset=utf-8")
        handler.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if path == "/api/stats/online-history":
        period = qs.get("period", ["day"])[0]
        if period not in ONLINE_SPANS:
            handler._send_json({"error": "period must be day, week, month, year or all"}, 400)
            return True
        with lock:
            data = get_online(conn, period)
        handler._send_json(data)
        return True

    return False
