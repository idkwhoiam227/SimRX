#!/usr/bin/env python3
"""Standalone SimTX collector: asks the server for the station list itself,
so neither simtx-transmitter.jar nor mitmproxy is needed.

Wire format reverse-engineered from simtx-transmitter 0.3.7 (protocol v24),
with the SimTX developer's permission (see README of simrx-local).

Flow:  TLS -> handshake (role 4 "control") -> server result (id 30)
       -> control ack (id 8) -> the server then pushes id 16 "health snapshot"
       (server stats + every active transmitter + receivers); id 9 "stations"
       is read too if it ever arrives. Every snapshot is forwarded to the
       local dashboard /api/ingest. The empty requests id 4 + id 10 are sent
       every --interval seconds, like the official client does.

Everything is big-endian; UTF = uint16 length + (modified) UTF-8; there is no
length prefix per packet, so the reader must know every packet layout it can
receive. Unknown packet id => reconnect instead of guessing.

The `address` field of a station is deliberately NEVER read into the output.

Usage:
    python simtx_collector.py                      # anonymous handshake
    python simtx_collector.py --ticket-file t.txt  # game ticket, re-read on every connect
    python simtx_collector.py --stdout             # just print JSON lines
    python simtx_collector.py --gui                # dev window: stations table, packet log
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path

MAGIC = 0x53494D54  # "SIMT"
VERSION = 24
ROLE_CONTROL = 4
AUTH_ANONYMOUS, AUTH_GAME_TICKET = 0, 1

# S2C packet ids we know how to read
S2C_HANDSHAKE, S2C_CONTROL_ACK, S2C_STATIONS = 30, 8, 9
S2C_NOTICE, S2C_MESSAGE, S2C_KICK, S2C_SESSIONS_KICKED = 17, 12, 11, 13
S2C_HEALTH = 16
C2S_REQUEST_A = 4    # empty; the official app sends it right before id 10
C2S_REQUEST_B = 10   # empty; answered by S2C 16 (health snapshot)

HANDSHAKE_STATUS = {
    0: "ACCEPTED", 1: "VERSION_UNSUPPORTED", 2: "AUTHENTICATION_REQUIRED",
    3: "CREDENTIAL_INVALID", 4: "ACCOUNT_RESTRICTED", 5: "ROLE_FORBIDDEN",
    6: "SERVER_UNAVAILABLE", 7: "IP_BANNED",
}


class ProtocolError(Exception):
    pass


class Reader:
    def __init__(self, sock):
        self.sock = sock
        self.trace = bytearray()  # every byte consumed since the last take_trace()

    def take_trace(self) -> bytes:
        t = bytes(self.trace)
        self.trace.clear()
        return t

    def raw(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("server closed the connection")
            buf += chunk
        self.trace += buf
        return bytes(buf)

    def u8(self): return self.raw(1)[0]
    def i32(self): return struct.unpack(">i", self.raw(4))[0]
    def i64(self): return struct.unpack(">q", self.raw(8))[0]
    def f32(self): return struct.unpack(">f", self.raw(4))[0]
    def f64(self): return struct.unpack(">d", self.raw(8))[0]
    def boolean(self): return self.u8() != 0

    def utf(self) -> str:
        n = struct.unpack(">H", self.raw(2))[0]
        return self.raw(n).decode("utf-8", errors="replace")


def w_utf(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > 65535:
        raise ValueError("string too long")
    return struct.pack(">H", len(b)) + b


def build_handshake(auth: int, credential: str) -> bytes:
    return struct.pack(">iiBB", MAGIC, VERSION, ROLE_CONTROL, auth) + w_utf(credential)


def read_handshake_result(r: Reader) -> tuple[str, str]:
    status = HANDSHAKE_STATUS.get(r.u8(), "UNKNOWN")
    message = r.utf()
    if r.boolean():          # accountId (UUID as two longs)
        r.i64(); r.i64()
    r.utf(); r.utf()         # handle, displayName
    for _ in range(r.u8()):  # permissions / account attributes
        r.utf()
    r.i64(); r.i64()         # authorizationRevision, leaseExpiresAtEpochMillis
    return status, message


def read_station(r: Reader) -> dict:
    uuid = r.utf()
    callsign = r.utf()
    transmitter = r.boolean()
    freq_hz = r.f32()
    modulation = r.utf()
    power_w = r.f32()
    lat = r.f64()
    lon = r.f64()
    r.utf()                  # address: read to stay in sync, never kept
    bandwidth_hz = r.f32()
    return {
        "uuid": uuid, "callsign": callsign, "transmitter": transmitter,
        "freq_hz": freq_hz, "mode": modulation, "power_w": power_w,
        "lat": lat, "lon": lon, "bandwidth_hz": bandwidth_hz, "source_type": "",
    }


def read_health(r: Reader) -> dict:
    """S2C 16: server health snapshot with the live transmitter / receiver lists."""
    stats = {"captured_ms": r.i64(), "status": r.utf(), "uptime_ms": r.i64(), "cpus": r.i32()}
    r.f32(); r.f32(); r.f64()          # processCpuLoad, systemCpuLoad, systemLoadAverage
    for _ in range(9):                 # cpu time, heap x3, non-heap, phys used/total, fds open/max
        r.i64()
    stats["threads"] = r.i32(); r.i32()                      # liveThreads, peakThreads
    stats["clients"] = r.i32(); stats["stations"] = r.i32()  # connectedClients, connectedStations
    stats["active_tx"] = r.i32(); stats["rx_groups"] = r.i32()
    r.boolean()                        # acceptorAlive
    for _ in range(4):                 # worldTick, tick duration, tick interval, simulationTime
        r.i64()
    for _ in range(6):                 # session tasks, receiver DSP, propagation jobs (active/queued)
        r.i32()
    r.i64(); r.i64()                   # streamedReceiverChunks, receiverOverrunChunks
    r.i32(); r.i32()                   # stalledTransmitterWrites, stalledReceiverGroups

    def count(what):
        n = r.i32()
        if n < 0 or n > 4096:
            raise ProtocolError(f"bad {what} count {n}")
        return n

    transmitters = []
    for _ in range(count("transmitter")):
        uuid, callsign = r.utf(), r.utf()
        r.utf()                        # address: read to stay in sync, never kept
        source_type = r.utf()
        freq_hz = r.f32()
        mode = r.utf()
        r.i32(); r.i32(); r.i32()      # sampleRate, bufferCapacity, bufferRetained
        for _ in range(12):            # buffer / processing / write counters
            r.i64()
        transmitters.append({
            "uuid": uuid, "callsign": callsign, "transmitter": True, "source_type": source_type,
            "freq_hz": freq_hz, "mode": mode, "power_w": None, "lat": None, "lon": None,
            "bandwidth_hz": None,
        })
    receivers = []
    for _ in range(count("receiver")):
        uuid, callsign = r.utf(), r.utf()
        r.utf()                        # address, dropped
        center_hz = r.f32()
        r.i32(); r.i32(); r.boolean()  # sampleRate, chunkSamples, wideband
        for _ in range(4):
            r.i32()
        for _ in range(15):
            r.i64()
        receivers.append({"uuid": uuid, "callsign": callsign, "center_hz": center_hz})
    stats["transmitters"], stats["receivers"] = len(transmitters), len(receivers)
    return {"stats": stats, "transmitters": transmitters, "receivers": receivers}


def read_packet(r: Reader):
    pid = r.u8()
    if pid == S2C_STATIONS:
        n = r.i32()
        if n < 0 or n > 100_000:
            raise ProtocolError(f"bad station count {n}")
        return pid, [read_station(r) for _ in range(n)]
    if pid == S2C_HEALTH:
        return pid, read_health(r)
    if pid == S2C_CONTROL_ACK:
        return pid, None
    if pid == S2C_HANDSHAKE:
        return pid, read_handshake_result(r)
    if pid == S2C_NOTICE:
        return pid, {"level": r.u8(), "text": r.utf(), "expires_ms": r.i64()}
    if pid == S2C_MESSAGE:
        return pid, r.utf()
    if pid == S2C_KICK:
        return pid, (r.utf(), r.utf())
    if pid == S2C_SESSIONS_KICKED:
        return pid, r.i32()
    raise ProtocolError(f"unknown server packet id {pid}")


def connect(host: str, port: int, use_tls: bool, timeout: float) -> socket.socket:
    raw = socket.create_connection((host, port), timeout=timeout)
    if not use_tls:
        return raw
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx.wrap_socket(raw, server_hostname=host)


def read_ticket(args) -> str:
    if getattr(args, "ticket", ""):
        return args.ticket.strip()
    if args.ticket_file:
        return Path(args.ticket_file).read_text(encoding="utf-8").strip()
    return os.environ.get("SIMTX_TICKET", "").strip()


def session(args, on_stations, log=None, on_packet=None, stop=None, wake=None):
    """One connection. Runs until an error, or until `stop` is set.
    log(text) gets human messages, on_packet(pid, raw_bytes) every packet read,
    `wake` (Event) makes the next station-list request go out immediately."""
    log = log or (lambda m: print(f"[collector] {m}", file=sys.stderr))
    stop = stop or threading.Event()
    wake = wake or threading.Event()
    ticket = read_ticket(args)
    auth = AUTH_GAME_TICKET if ticket else AUTH_ANONYMOUS

    sock = connect(args.host, args.port, not args.no_tls, 10)
    try:
        sock.sendall(build_handshake(auth, ticket))
        r = Reader(sock)

        def packet():
            pid, body = read_packet(r)
            raw = r.take_trace()
            if on_packet:
                on_packet(pid, raw)
            return pid, body

        sock.settimeout(10)
        pid, body = packet()
        if pid != S2C_HANDSHAKE:
            raise ProtocolError(f"expected handshake result, got packet {pid}")
        status, message = body
        if status != "ACCEPTED":
            raise ProtocolError(f"handshake rejected: {status}: {message}")
        pid, _ = packet()
        if pid != S2C_CONTROL_ACK:
            raise ProtocolError(f"expected control ack, got packet {pid}")
        log(f"connected to {args.host}:{args.port} ({'ticket' if auth else 'anonymous'})")

        sock.settimeout(30)  # generous: only applies once a packet has started arriving
        next_request = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_request or wake.is_set():
                # same pair, same order as the official client (aA() in TransmitterSwingApp)
                for rid in (C2S_REQUEST_A, C2S_REQUEST_B):
                    sock.sendall(bytes([rid]))
                    if on_packet:
                        on_packet(-rid, bytes([rid]))
                wake.clear()
                next_request = now + args.interval
            # only start reading a packet when bytes are there, so the timeout
            # can never fire in the middle of one and desync the stream
            if not (getattr(sock, "pending", lambda: 0)() or select.select([sock], [], [], 0.2)[0]):
                continue
            pid, body = packet()
            if pid == S2C_HEALTH:
                on_stations(body["transmitters"], body["stats"], "health", body["receivers"])
            elif pid == S2C_STATIONS:
                on_stations(body, None, "stations", None)
            elif pid in (S2C_NOTICE, S2C_MESSAGE):
                log(f"server says: {body}")
    finally:
        sock.close()


def make_forwarder(args):
    ingest_key = args.ingest_key
    if not ingest_key and Path(args.ingest_key_file).exists():
        ingest_key = Path(args.ingest_key_file).read_text(encoding="utf-8").strip()

    positions = {}  # uuid -> latest id-9 record; gives lat/lon/power to the id-16 transmitters

    def forward(stations, stats=None, kind="health", receivers=None):
        if kind == "stations":
            positions.clear()
            positions.update({s["uuid"]: s for s in stations})
        picked = stations if args.all else [s for s in stations if s["transmitter"]]
        if kind == "health":
            picked = [dict(s, **{k: v for k, v in
                                 ((k, positions.get(s["uuid"], {}).get(k)) for k in ("lat", "lon", "power_w", "bandwidth_hz"))
                                 if v is not None}) for s in picked]
        rx_out = None
        if receivers is not None:
            rx_out = []
            for r in receivers:
                pos = positions.get(r["uuid"], {})
                rx_out.append({"callsign": r["callsign"], "center_khz": round(r["center_hz"] / 1000, 3),
                               "lat": pos.get("lat"), "lon": pos.get("lon")})
        if args.stdout:
            print(json.dumps({"t": int(time.time()), "kind": kind, "stats": stats, "stations": picked,
                              "receivers": rx_out}, ensure_ascii=False), flush=True)
            return
        if kind != "health":
            return  # sessions come from the active-transmitter list (id 16); id 9 only supplies positions
        import requests  # imported lazily so --stdout needs nothing extra
        payload = {
            "stations": [
                {"callsign": s["callsign"], "source_type": s.get("source_type") or "UNKNOWN",
                 "mode": s["mode"], "freq_khz": round(s["freq_hz"] / 1000, 3),
                 "lat": s.get("lat"), "lon": s.get("lon"), "power_w": s.get("power_w"),
                 "bandwidth_hz": s.get("bandwidth_hz")}
                for s in picked
            ],
            "online": ({"clients": stats["clients"], "stations": stats["stations"],
                        "transmitters": stats["transmitters"], "receivers": stats["receivers"]} if stats else None),
            "receivers": rx_out,
        }
        try:
            requests.post(args.ingest_url, json=payload, timeout=3,
                          headers={"X-Ingest-Key": ingest_key})
        except Exception as e:  # keep collecting even if the dashboard is down
            print(f"[collector] ingest failed: {e}", file=sys.stderr)
    return forward


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="argon.simtx.net")
    p.add_argument("--port", type=int, default=5556)
    p.add_argument("--interval", type=float, default=10.0, help="seconds between station-list requests (min 5)")
    p.add_argument("--ticket", default="", help="game ticket string (short-lived; prefer --ticket-file)")
    p.add_argument("--gui", action="store_true", help="open the development window")
    p.add_argument("--ticket-file", help="file with a stx_gt1_... game ticket, re-read on every connect")
    p.add_argument("--stdout", action="store_true", help="print JSON lines instead of posting to the dashboard")
    p.add_argument("--all", action="store_true", help="forward every station, not only transmitter=true")
    p.add_argument("--ingest-url", default="http://127.0.0.1:8090/api/ingest")
    p.add_argument("--ingest-key", default="")
    p.add_argument("--ingest-key-file", default=str(Path(__file__).parent / "ingest_key.txt"))
    p.add_argument("--no-tls", action="store_true", help="plain TCP, for local tests only")
    args = p.parse_args()
    args.interval = max(5.0, args.interval)

    if args.gui:
        run_gui(args)
        return

    forward = make_forwarder(args)
    backoff = 2
    while True:
        try:
            session(args, forward)
        except KeyboardInterrupt:
            return
        except (OSError, ProtocolError, ssl.SSLError) as e:
            print(f"[collector] {type(e).__name__}: {e}; retry in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 2


# ---------------------------------------------------------------- dev GUI ---
def run_gui(args):
    import queue
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    events: "queue.Queue[tuple]" = queue.Queue()
    state = {"thread": None, "stop": None, "wake": None}

    root = tk.Tk()
    root.title("SimTX collector (dev)")
    root.geometry("1100x700")

    top = ttk.Frame(root, padding=6)
    top.pack(fill="x")
    v_host = tk.StringVar(value=args.host)
    v_port = tk.StringVar(value=str(args.port))
    v_int = tk.StringVar(value=str(args.interval))
    v_ticket = tk.StringVar(value=args.ticket)
    v_tls = tk.BooleanVar(value=not args.no_tls)
    v_all = tk.BooleanVar(value=True)       # dev: show everything by default
    v_fwd = tk.BooleanVar(value=False)      # dev: don't touch the dashboard DB by default
    for i, (label, var, w) in enumerate([("Host", v_host, 22), ("Port", v_port, 6),
                                          ("Interval s", v_int, 5), ("Ticket", v_ticket, 28)]):
        ttk.Label(top, text=label).grid(row=0, column=i * 2, padx=(6, 2))
        ttk.Entry(top, textvariable=var, width=w).grid(row=0, column=i * 2 + 1)
    opts = ttk.Frame(root, padding=(6, 0))
    opts.pack(fill="x")
    ttk.Checkbutton(opts, text="TLS", variable=v_tls).pack(side="left")
    ttk.Checkbutton(opts, text="show non-transmitters", variable=v_all, command=lambda: redraw()).pack(side="left", padx=10)
    ttk.Checkbutton(opts, text="forward to dashboard (/api/ingest)", variable=v_fwd).pack(side="left")

    bar = ttk.Frame(root, padding=6)
    bar.pack(fill="x")
    b_start = ttk.Button(bar, text="Start")
    b_stop = ttk.Button(bar, text="Stop", state="disabled")
    b_now = ttk.Button(bar, text="Request now", state="disabled")
    b_save = ttk.Button(bar, text="Save snapshot JSON")
    for b in (b_start, b_stop, b_now, b_save):
        b.pack(side="left", padx=3)
    v_status = tk.StringVar(value="stopped")
    ttk.Label(bar, textvariable=v_status).pack(side="left", padx=15)

    pane = ttk.PanedWindow(root, orient="vertical")
    pane.pack(fill="both", expand=True, padx=6, pady=6)

    cols = ("callsign", "source", "mode", "freq_khz", "power_w", "lat", "lon", "bw_hz", "tx", "uuid")
    tree = ttk.Treeview(pane, columns=cols, show="headings", height=14)
    widths = (140, 110, 80, 110, 70, 80, 80, 80, 40, 280)
    for c, w in zip(cols, widths):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="w")
    pane.add(tree, weight=3)

    log_box = ScrolledText(pane, height=12, font=("Consolas", 9))
    pane.add(log_box, weight=2)

    last = {"stations": []}

    def log(msg):
        log_box.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
        log_box.see("end")

    live = {"health": {}, "list": {}, "stats": None}   # latest id-16 and id-9 data, keyed by uuid

    def merged():
        """One row per station: id 9 supplies lat/lon/power/bandwidth, id 16 the live
        source/mode/frequency. A later packet of one kind never erases the other's fields."""
        rows = []
        for uuid in list(live["health"]) + [u for u in live["list"] if u not in live["health"]]:
            row = {}
            for src in (live["list"].get(uuid), live["health"].get(uuid)):
                if src:
                    row.update({k: v for k, v in src.items() if v not in (None, "")})
            row["transmitter"] = bool(live["health"].get(uuid)) or bool(
                live["list"].get(uuid, {}).get("transmitter"))
            for k in ("source_type", "mode", "callsign"):
                row.setdefault(k, "")
            for k in ("freq_hz", "power_w", "lat", "lon", "bandwidth_hz"):
                row.setdefault(k, None)
            rows.append(row)
        return rows

    def redraw():
        rows = merged()
        last["stations"] = rows
        tree.delete(*tree.get_children())
        shown = rows if v_all.get() else [s for s in rows if s["transmitter"]]

        def fmt(v, nd=None):
            return "" if v is None else (round(v, nd) if nd is not None else round(v))
        for s in shown:
            f = s["freq_hz"]
            tree.insert("", "end", values=(
                s["callsign"], s["source_type"], s["mode"], fmt(f / 1000, 3) if f is not None else "",
                fmt(s["power_w"], 2), fmt(s["lat"], 4), fmt(s["lon"], 4), fmt(s["bandwidth_hz"]),
                "Y" if s["transmitter"] else "", s["uuid"]))
        stats = live["stats"]
        n_tx = sum(1 for s in rows if s["transmitter"])
        extra = (f" | server: {stats['clients']} clients, {stats['stations']} stations, "
                 f"{stats['receivers']} receivers") if stats else ""
        has9 = "id9: yes" if live["list"] else "id9: not received"
        v_status.set(f"connected | {len(rows)} listed, {n_tx} transmitting{extra} | {has9} | "
                     f"{time.strftime('%H:%M:%S')}")

    def drain():
        try:
            while True:
                kind, data = events.get_nowait()
                if kind == "log":
                    log(data)
                elif kind == "packet":
                    pid, raw = data
                    name = f"C2S {-pid} (empty request)" if pid < 0 else f"S2C {pid}"
                    hexs = raw[:48].hex(" ") + (" ..." if len(raw) > 48 else "")
                    log(f"{name}  {len(raw)} B  {hexs}")
                elif kind == "stations":
                    data, stats, src = data
                    if src == "health":
                        live["health"] = {x["uuid"]: x for x in data}
                        if stats:
                            live["stats"] = stats
                    else:
                        live["list"] = {x["uuid"]: x for x in data}
                    redraw()
                elif kind == "state":
                    v_status.set(data)
                elif kind == "stopped":
                    b_start.config(state="normal")
                    b_stop.config(state="disabled")
                    b_now.config(state="disabled")
        except queue.Empty:
            pass
        root.after(100, drain)

    def worker(a, stop, wake):
        fwd = make_forwarder(a) if a.forward else None
        backoff = 2

        def on_stations(st, stats=None, src="health", receivers=None):
            events.put(("stations", (st, stats, src)))
            if fwd:
                fwd(st, stats, src, receivers)

        while not stop.is_set():
            try:
                events.put(("state", f"connecting to {a.host}:{a.port} ..."))
                session(a, on_stations, log=lambda m: events.put(("log", m)),
                        on_packet=lambda pid, raw: events.put(("packet", (pid, raw))),
                        stop=stop, wake=wake)
                backoff = 2
            except (OSError, ProtocolError, ssl.SSLError, ValueError) as e:
                events.put(("log", f"{type(e).__name__}: {e}"))
                events.put(("state", f"error, retry in {backoff}s"))
                for _ in range(backoff * 10):
                    if stop.is_set():
                        break
                    time.sleep(0.1)
                backoff = min(backoff * 2, 60)
        events.put(("state", "stopped"))
        events.put(("stopped", None))

    def start():
        try:
            a = argparse.Namespace(**vars(args))
            a.host, a.port = v_host.get().strip(), int(v_port.get())
            a.interval = max(5.0, float(v_int.get()))
        except ValueError:
            log("bad port / interval")
            return
        a.ticket, a.no_tls = v_ticket.get().strip(), not v_tls.get()
        a.all, a.forward, a.stdout = v_all.get(), v_fwd.get(), False
        state["stop"], state["wake"] = threading.Event(), threading.Event()
        state["thread"] = threading.Thread(target=worker, args=(a, state["stop"], state["wake"]), daemon=True)
        state["thread"].start()
        b_start.config(state="disabled")
        b_stop.config(state="normal")
        b_now.config(state="normal")

    def stop():
        if state["stop"]:
            state["stop"].set()
            v_status.set("stopping ...")

    def save():
        path = Path(__file__).parent / f"snapshot_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(last["stations"], ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"saved {len(last['stations'])} stations -> {path}")

    b_start.config(command=start)
    b_stop.config(command=stop)
    b_now.config(command=lambda: state["wake"] and state["wake"].set())
    b_save.config(command=save)
    root.protocol("WM_DELETE_WINDOW", lambda: (stop(), root.destroy()))
    root.after(100, drain)
    root.mainloop()


if __name__ == "__main__":
    main()
