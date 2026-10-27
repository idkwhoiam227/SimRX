"""Backend tests for the SimRX dashboard. No third-party packages needed.

    python -m unittest discover -s tests -v

They run the real server code against a throw-away SQLite file (SIMRX_DB), so your own
simrx_local.db is never touched. UI behaviour is not covered here.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="simrx-test-")
os.environ["SIMRX_DB"] = str(Path(_TMP) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth            # noqa: E402
import server as S     # noqa: E402
import simrx_map as M  # noqa: E402

auth.PBKDF2_ITERATIONS = 2000       # fast hashes for tests (production default: 600000)
KEY = "test-ingest-key"
httpd = None
BASE = ""


def setUpModule():
    global httpd, BASE
    S.init_db()
    S.INGEST_KEY, S.SESSION_SECRET = KEY, "test-secret"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def tearDownModule():
    httpd.shutdown()


def call(path, body=None, key=True, cookie=None, raw=False):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Ingest-Key"] = KEY
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read()
            return (r.status, data, r.headers) if raw else (r.status, json.loads(data))
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return (e.code, json.loads(data))
        except ValueError:
            return (e.code, data)


def reset_db():
    with S._lock, S._conn:
        for t in ("sessions", "frequency_log", "online_samples", "receivers_now", "collector_outages", "server_starts"):
            S._conn.execute(f"DELETE FROM {t}")
        S._conn.execute("UPDATE config SET value = '0' WHERE key = 'freq_history_enabled'")
    M._leader_order.clear()
    M._state["last_sample"] = 0
    S._latest_data_at = 0
    S._login_ip_attempts.clear()


def add_session(callsign, start, dur, freq=7100, lat=None, lon=None, mode="USB", open_=False, power=None):
    now = int(time.time())
    with S._lock, S._conn:
        S._conn.execute(
            "INSERT INTO sessions (callsign, source_type, mode, freq_khz, started_at, last_seen_at, ended_at, lat, lon, power_w) "
            "VALUES (?, 'MICROPHONE', ?, ?, ?, ?, ?, ?, ?, ?)",
            (callsign, mode, freq, start, now if open_ else start + dur, None if open_ else start + dur, lat, lon, power))


def station(callsign, freq=7100, **kw):
    return {"callsign": callsign, "source_type": "MICROPHONE", "mode": "USB", "freq_khz": freq, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        reset_db()


class TestMapIngest(Base):
    def test_only_real_coordinates_reach_the_map(self):
        call("/api/ingest", {"stations": [station("AAA", lat=50.1, lon=14.4, power_w=5), station("BBB", lat=0, lon=0),
                                          station("CCC", lat="x", lon=None), station("DDD"), station("EEE", lat=95, lon=10)],
                             "receivers": [{"callsign": "RX1", "center_khz": 1000, "lat": 48.8, "lon": 2.3},
                                           {"callsign": "RX2", "center_khz": 1, "lat": None, "lon": None}]})
        now = call("/api/map/now", key=False)[1]
        self.assertEqual([t["callsign"] for t in now["transmitters"]], ["AAA"])
        self.assertEqual(now["transmitters_without_position"], 4)
        self.assertEqual([r["callsign"] for r in now["receivers"]], ["RX1"])

    def test_legacy_rows_without_coordinates_never_shown(self):
        add_session("OLD", int(time.time()) - 3600, 600)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for view in ("timeline", "aggregate"):
            d = call(f"/api/map/sessions?period=all&view={view}" if view == "aggregate"
                     else f"/api/map/sessions?period=day&date={today}&view=timeline", key=False)[1]
            self.assertNotIn("OLD", json.dumps(d))
            self.assertEqual(d["sessions_without_position"], 1)

    def test_bad_input_is_rejected(self):
        self.assertEqual(call("/api/map/sessions?period=day&date=garbage", key=False)[0], 400)
        self.assertEqual(call("/api/map/sessions?period=all&view=timeline", key=False)[0], 400)
        self.assertEqual(call("/api/map/sessions?period=nope", key=False)[0], 400)

    def test_old_ingest_format_still_works(self):
        self.assertEqual(call("/api/ingest", {"stations": [station("ZZZ")]})[0], 200)

    def test_search_and_band_filters(self):
        t = int(time.time()) - 600
        add_session("HFONE", t, 100, 7100, 50, 10)
        add_session("FMONE", t, 100, 98200, 51, 11)
        add_session("HFTWO", t, 100, 14200, 52, 12)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        names = lambda qs: sorted(p["callsign"] for p in call(f"/api/map/sessions?period=day&date={today}&view=aggregate{qs}", key=False)[1]["places"])
        self.assertEqual(names("&band=hf"), ["HFONE", "HFTWO"])
        self.assertEqual(names("&band=vhf"), ["FMONE"])
        self.assertEqual(names("&q=two"), ["HFTWO"])
        self.assertEqual(names("&q=hf&band=hf"), ["HFONE", "HFTWO"])
        self.assertEqual(names("&q=hf&band=vhf"), [])
        self.assertEqual(names("&band=nonsense"), ["FMONE", "HFONE", "HFTWO"])   # unknown band = no filter


class TestOnlineHistory(Base):
    def test_history_is_rebuilt_from_old_sessions(self):
        now, day = int(time.time()), 86400
        add_session("A", now - 3 * day + 1000, 7200); add_session("B", now - 3 * day + 2000, 3600); add_session("C", now - 3 * day + 2500, 1800)
        with S._lock, S._conn:
            S._conn.execute("INSERT INTO collector_outages (started_at, ended_at, duration_seconds) VALUES (?, ?, 7200)", (now - day - 7200, now - day))
        d = call("/api/stats/online-history?period=all", key=False)[1]
        self.assertEqual(d["peak_tx"]["value"], 3)
        self.assertTrue(all(p["rx"] is None for p in d["points"]))
        inside = [p for p in d["points"] if now - day - 7200 + d["bucket_seconds"] <= p["t"] <= now - day - d["bucket_seconds"]]
        self.assertTrue(inside and all(p["tx"] is None for p in inside), "collector outage must show as a gap")

    def test_online_samples_throttled_and_rx_recorded(self):
        for rx in (3, 9):
            call("/api/ingest", {"stations": [], "online": {"clients": 50, "stations": 10, "transmitters": 2, "receivers": rx}})
        d = call("/api/stats/online-history?period=day", key=False)[1]
        self.assertEqual(d["peak_rx"]["value"], 3)           # the second sample came < 30 s later and was skipped
        self.assertEqual(d["current"]["server_clients"], 50)


class TestFrequencyLog(Base):
    def setUp(self):
        super().setUp()
        S.set_config("freq_history_enabled", "1")

    def history(self):
        return call("/api/freq-history?callsign=X", key=False)[1]["entries"]

    def test_logs_only_changes(self):
        ing = lambda **k: call("/api/ingest", {"stations": [station("X", **k)]})
        for _ in range(5):
            ing(freq_khz=7100)
        self.assertEqual(len(self.history()), 1)
        ing(freq_khz=7100, lat=50.1, lon=14.4, power_w=5)       # newly known position/power is not a change
        self.assertEqual(len(self.history()), 1)
        ing(freq_khz=7200, lat=50.1, lon=14.4, power_w=5)
        ing(freq_khz=7200, lat=50.1, lon=14.4, power_w=50)
        ing(freq_khz=7200, lat=51.0, lon=14.4, power_w=50)
        ing(freq_khz=7300, lat=51.0, lon=14.4, power_w=100)
        self.assertEqual([e["reason"] for e in self.history()], ["start", "freq", "power", "position", "freq,power"])

    def test_disabled_writes_nothing(self):
        S.set_config("freq_history_enabled", "0")
        call("/api/ingest", {"stations": [station("X")]})
        S.set_config("freq_history_enabled", "1")
        self.assertEqual(self.history(), [])

    def test_cleanup_needs_admin_and_touches_only_the_frequency_log(self):
        add_session("KEEP", int(time.time()) - 100, 50)
        with S._lock, S._conn:
            t0 = int(time.time()) - 100000
            for i in range(300):
                S._conn.execute("INSERT INTO frequency_log (callsign, freq_khz, mode, logged_at) VALUES ('OLD', 4625, 'USB', ?)", (t0 + i,))
            for i in range(100):
                S._conn.execute("INSERT INTO frequency_log (callsign, freq_khz, mode, logged_at) VALUES ('OLD', 4700, 'USB', ?)", (t0 + 300 + i,))
        self.assertEqual(call("/api/freq-history/cleanup", {"action": "compact"}, key=False)[0], 401)
        res = M.freqlog_cleanup(S._conn, S._lock, "compact")
        self.assertEqual((res["removed"], res["kept"]), (398, 2))
        rows = S._conn.execute("SELECT freq_khz, reason FROM frequency_log ORDER BY logged_at").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(4625.0, "start"), (4700.0, "freq")])
        self.assertEqual(S._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        M.freqlog_cleanup(S._conn, S._lock, "clear")
        self.assertEqual(S._conn.execute("SELECT COUNT(*) FROM frequency_log").fetchone()[0], 0)
        self.assertEqual(S._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            M.freqlog_cleanup(S._conn, S._lock, "drop everything")


class TestStatsAndLeaders(Base):
    def test_hourly_counts_people_across_the_hours_they_were_on_air(self):
        base = int(time.mktime((2026, 10, 10, 10, 30, 0, 0, 0, -1)))
        add_session("P1", base, 6000)                 # 10:30-12:10 -> hours 10, 11, 12
        add_session("P2", base + 1800, 200)           # 11:00-11:03
        add_session("P1", base + 86400, 60)           # same person next day: still one person
        h = {x["hour"]: x["count"] for x in call("/api/stats/hourly", key=False)[1]["hours"]}
        self.assertEqual((h[9], h[10], h[11], h[12], h[13]), (0, 1, 2, 1, 0))

    def test_leaders_do_not_swap_inside_the_margin(self):
        for c, d in (("A", 1000), ("B", 1003), ("C", 500)):
            add_session(c, 0, d)
        now = 10 ** 6
        self.assertEqual(M.leader_ranks(S._conn, now), {"B": 1, "A": 2, "C": 3})
        first = lambda: [c for c, k in M.leader_ranks(S._conn, now).items() if k == 1][0]
        setA = lambda v: S._conn.execute("UPDATE sessions SET ended_at=?, last_seen_at=? WHERE callsign='A'", (v, v)) and S._conn.commit()
        for v, expect in ((1006, "B"), (1008, "B"), (1010, "A"), (1005, "A"), (1003, "A")):
            setA(v)
            self.assertEqual(first(), expect, f"A={v}")
        setA(990)
        self.assertEqual(first(), "B")

    def test_live_rows_carry_the_rank(self):
        add_session("TOP", 0, 5000, open_=True)
        live = call("/api/live", key=False)[1]["stations"]
        self.assertEqual([(x["callsign"], x["rank"]) for x in live], [("TOP", 1)])


class TestProfile(Base):
    def test_profile_numbers(self):
        t = int(time.time()) - 5 * 86400
        add_session("NTD81", t, 600, 7100, 50.1, 14.4, "USB", power=5)
        add_session("ntd81", t + 86400, 1800, 7100, 50.1, 14.4, "USB")          # same callsign, other case
        add_session("NTD81", t + 2 * 86400, 60, 98200, None, None, "WFM")       # old row without position
        add_session("OTHER", t, 5000)
        p = call("/api/callsign?name=ntd81", key=False)[1]
        self.assertTrue(p["found"])
        self.assertEqual((p["sessions"], p["total_seconds"], p["longest_seconds"], p["days_active"]), (3, 2460, 1800, 3))
        self.assertEqual(p["rank"], 2)                                           # OTHER has more time on air
        self.assertEqual(p["places_total"], 1)
        self.assertEqual(p["places"][0]["sessions"], 2)
        self.assertEqual({m["name"] for m in p["modes"]}, {"USB", "WFM"})
        self.assertEqual(sum(p["hourly_utc"]) >= 3, True)
        self.assertEqual(len(p["recent"]), 3)
        self.assertIsNone([r for r in p["recent"] if r["freq_khz"] == 98200][0]["lat"])
        self.assertFalse(call("/api/callsign?name=NOBODY", key=False)[1]["found"])
        self.assertFalse(call("/api/callsign?name=", key=False)[1]["found"])

    def test_search_treats_wildcards_literally(self):
        for c in ("A_B", "AXB", "50%off", "50Xoff"):
            add_session(c, 1, 1)
        names = lambda q: [r["callsign"] for r in call("/api/callsigns/search?q=" + urllib.request.quote(q), key=False)[1]["results"]]
        self.assertEqual(names("a_b"), ["A_B"])
        self.assertEqual(names("50%"), ["50%off"])
        self.assertEqual(names(""), [])


class TestExport(Base):
    def test_csv_content_and_formula_protection(self):
        t = int(time.time()) - 100
        add_session("=HYPERLINK(1)", t, 50, 7100, 1.5, 2.5)
        add_session("NORMAL", t, 50, 98200)
        status, body, headers = call("/api/export/sessions.csv?period=all", key=False, raw=True)
        text = body.decode("utf-8-sig")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        lines = text.strip().split("\r\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(any(l.split(",")[1] == "'=HYPERLINK(1)" for l in lines), "formula must be neutralised")
        self.assertIn("frequency_khz", lines[0])

    def test_export_filters(self):
        t = int(time.time()) - 100
        add_session("AAA", t, 5, 7100); add_session("BBB", t, 5, 98200)
        body = call("/api/export/sessions.csv?callsign=aaa", key=False, raw=True)[1].decode("utf-8-sig")
        self.assertEqual(len(body.strip().split("\r\n")), 2)
        body = call("/api/export/sessions.csv?band=vhf", key=False, raw=True)[1].decode("utf-8-sig")
        self.assertIn("BBB", body); self.assertNotIn("AAA", body)


class TestAuth(Base):
    def make_admin(self, username="boss", password="correct horse", iterations=1000):   # "old" hash, below the 2000 used in tests
        salt = auth.new_salt()
        with S._lock, S._conn:
            S._conn.execute("DELETE FROM admins")
            S._conn.execute("INSERT INTO admins (username, password_hash, password_salt, password_iterations, created_at) VALUES (?, ?, ?, ?, ?)",
                            (username, auth.hash_password(password, salt, iterations).hex(), salt.hex(), iterations, int(time.time())))

    def login(self, u, p):
        return call("/api/login", {"username": u, "password": p}, key=False)

    def test_login_and_transparent_hash_upgrade(self):
        self.make_admin()
        self.assertEqual(S._conn.execute("SELECT password_iterations FROM admins").fetchone()[0], 1000)
        self.assertEqual(self.login("boss", "correct horse")[0], 200)
        self.assertEqual(S._conn.execute("SELECT password_iterations FROM admins").fetchone()[0], auth.PBKDF2_ITERATIONS)
        self.assertEqual(self.login("boss", "correct horse")[0], 200)       # still works after the upgrade
        self.assertEqual(self.login("boss", "wrong")[0], 401)

    def test_unknown_user_and_bad_input_get_generic_answers(self):
        self.make_admin()
        self.assertEqual(self.login("nobody", "x")[1]["error"], "Invalid username or password")
        self.assertEqual(call("/api/login", {"username": ["a"], "password": 5}, key=False)[0], 400)
        self.assertEqual(call("/api/login", {"username": "", "password": ""}, key=False)[0], 400)

    def test_lockout_only_reveals_itself_to_the_right_password(self):
        self.make_admin()
        for _ in range(auth.LOCKOUT_THRESHOLD):
            self.assertEqual(self.login("boss", "wrong")[0], 401)
        self.assertEqual(self.login("boss", "wrong")[0], 401)               # attacker still sees the generic error
        self.assertEqual(self.login("boss", "correct horse")[0], 423)       # the owner is told

    def test_ip_rate_limit(self):
        self.make_admin()
        codes = [self.login("nobody", "x")[0] for _ in range(12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_only_one_login_handler_exists(self):
        self.assertEqual(Path(S.__file__).read_text(encoding="utf-8").count("def _handle_login"), 1)


class TestClockGuard(Base):
    def tearDown(self):
        S._latest_data_at = 0
        S.MIN_VALID_EPOCH = 1735689600
        S.check_clock()

    def test_ingest_is_refused_when_the_clock_is_behind_stored_data(self):
        S._latest_data_at = int(time.time()) + 10_000
        status, body = call("/api/ingest", {"stations": [station("LATE")]})
        self.assertEqual(status, 503)
        self.assertTrue(body["clock_problem"])
        self.assertEqual(S._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        st = call("/api/collector-status", key=False)[1]
        self.assertFalse(st["clock_ok"])
        S._latest_data_at = 0
        self.assertEqual(call("/api/ingest", {"stations": [station("OK")]})[0], 200)
        self.assertTrue(call("/api/collector-status", key=False)[1]["clock_ok"])

    def test_ingest_is_refused_when_the_clock_was_reset_to_an_old_date(self):
        S.MIN_VALID_EPOCH = int(time.time()) + 10_000
        self.assertEqual(call("/api/ingest", {"stations": [station("LATE")]})[0], 503)

    def test_small_backwards_steps_are_tolerated(self):
        S._latest_data_at = int(time.time()) + 120      # an NTP correction of 2 minutes
        self.assertEqual(call("/api/ingest", {"stations": [station("OK")]})[0], 200)


if __name__ == "__main__":
    unittest.main()
