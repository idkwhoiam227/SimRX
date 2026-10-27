/* SimRX: "Players online" chart + map (now / per-period timeline / all-callsigns heatmap).
   Loaded after the dashboard's inline script, so API, esc() and fmtDuration() exist.
   All dates and times here are UTC, matching the calendar in the Records tab. */
(function () {
  "use strict";

  const onlineRoot = document.getElementById("online-section");
  const mapRoot = document.getElementById("map-section");
  if (!onlineRoot && !mapRoot) return;

  const $ = (id) => document.getElementById(id);
  const pad = (n) => String(n).padStart(2, "0");
  const nowSec = () => Math.floor(Date.now() / 1000);
  const todayUtc = () => new Date().toISOString().slice(0, 10);
  const hhmm = (s) => { const d = new Date(s * 1000); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; };
  const ymdhm = (s) => new Date(s * 1000).toISOString().slice(0, 16).replace("T", " ");
  const dur = (sec) => (typeof fmtDuration === "function" ? fmtDuration(sec) : `${Math.round(sec / 60)}m`);
  const E = (v) => (typeof esc === "function" ? esc(v) : String(v ?? ""));
  const fmtKhz = (f) => (typeof fmtFreq === "function" ? fmtFreq(f)
    : (f == null ? "—" : `${Number(f).toLocaleString("en-US", { maximumFractionDigits: 3 })} kHz`));
  const fmtPos = (la, lo) => `${la.toFixed(2)}, ${lo.toFixed(2)}`;
  const statsTabActive = () => { const t = $("tab-stats"); return !!t && t.classList.contains("active"); };

  async function getJson(url) {
    const res = await fetch(`${API}${url}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  /* ======================================================== online chart == */
  let onlineChart = null;
  let onlinePeriod = "day";
  const ONLINE_LABELS = { day: "Day", week: "Week", month: "Month", year: "Year", all: "All time" };

  function buildOnlineUi() {
    onlineRoot.innerHTML = `
      <h1 style="margin-top:36px">Players online</h1>
      <div class="card">
        <div class="pill-row" id="online-zoom">
          ${Object.entries(ONLINE_LABELS).map(([k, v]) =>
            `<button type="button" class="mpill${k === onlinePeriod ? " active" : ""}" data-p="${k}">${v}</button>`).join("")}
        </div>
        <div class="online-stats" id="online-stats"></div>
        <canvas id="online-chart" height="110"></canvas>
        <div class="small-note" id="online-note"></div>
      </div>`;
    $("online-zoom").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-p]");
      if (!b) return;
      onlinePeriod = b.dataset.p;
      document.querySelectorAll("#online-zoom .mpill").forEach((x) => x.classList.toggle("active", x === b));
      loadOnline();
    });
  }

  function tickLabel(period, ms) {
    const d = new Date(ms);
    if (period === "day") return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    if (period === "week" || period === "month") return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}`;
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}`;
  }

  function withGaps(points, key, bucket) {
    // a null value = unknown (collector down / not recorded): break the line there
    const out = [];
    points.forEach((p) => out.push({ x: p.t * 1000, y: p[key] }));
    return out;
  }

  async function loadOnline() {
    if (!onlineRoot) return;
    let data;
    try { data = await getJson(`/api/stats/online-history?period=${onlinePeriod}`); }
    catch (e) { console.warn("online history failed", e); return; }

    const none = (v) => (v == null ? "NONE" : v);
    const cur = data.current, label = ONLINE_LABELS[onlinePeriod].toLowerCase();
    const nowTotal = cur ? cur.tx + (cur.rx || 0) : null;
    $("online-stats").innerHTML = `
      <div class="stat"><div class="label">Online now</div><div class="value">${cur ? nowTotal : "—"}</div>
        <div class="sub">${cur ? `TX ${cur.tx}<br>RX ${none(cur.rx)}<br>Server reply: ${none(cur.server_clients)}` : "no data yet"}</div></div>
      <div class="stat"><div class="label">Peak TX (${label})</div>
        <div class="value">${data.peak_tx ? data.peak_tx.value : "—"}</div>
        <div class="sub">${data.peak_tx ? ymdhm(data.peak_tx.at) + " UTC" : ""}</div></div>
      <div class="stat"><div class="label">Peak RX (${label})</div>
        <div class="value">${data.peak_rx ? data.peak_rx.value : "NONE"}</div>
        <div class="sub">${data.peak_rx ? ymdhm(data.peak_rx.at) + " UTC" : "not recorded yet"}</div></div>
      <div class="stat"><div class="label">Average TX</div><div class="value">${none(data.avg_tx)}</div>
        <div class="sub">RX avg: ${none(data.avg_rx)}</div></div>`;

    const canvas = $("online-chart");
    if (onlineChart) { onlineChart.destroy(); onlineChart = null; }
    const hasTx = data.points.some((p) => p.tx != null);
    $("online-note").textContent = hasTx ? "" : "No data yet.";
    if (!hasTx) { canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height); return; }

    const mk = (label, key, color, fill) => ({
      label, data: withGaps(data.points, key, data.bucket_seconds), borderColor: color,
      backgroundColor: fill ? color + "22" : color, fill: !!fill, borderWidth: 2,
      pointRadius: 0, pointHoverRadius: 3, tension: 0.25, spanGaps: false,
    });
    onlineChart = new Chart(canvas, {
      type: "line",
      data: { datasets: [mk("Transmitting", "tx", "#E05A47", true), mk("Receivers", "rx", "#33AFC7", false)] },
      options: {
        responsive: true, parsing: false, animation: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: {
          legend: { labels: { color: "#7f9189" } },
          tooltip: { callbacks: { title: (items) => items.length ? ymdhm(items[0].parsed.x / 1000) + " UTC" : "" } },
        },
        scales: {
          x: { type: "linear", min: data.from * 1000, max: data.to * 1000,
               ticks: { color: "#7f9189", maxTicksLimit: 8, callback: (v) => tickLabel(onlinePeriod, v) },
               grid: { color: "#23302a" } },
          y: { beginAtZero: true, ticks: { color: "#7f9189", precision: 0 }, grid: { color: "#23302a" } },
        },
      },
    });
  }


  /* ================================================================== map == */
  const COL = { tx: "#E05A47", rx: "#33AFC7", faint: "#7f9189", agg: "#33AFC7" };

  const S = {
    period: "now", date: todayUtc(), from: todayUtc(), to: todayUtc(),
    view: "timeline", heat: false,   // view = what the user prefers; see effView()
    q: "", band: "",
    measure: { on: false, pts: [], layer: null },
    map: null, layers: {}, fitted: false, nowTimer: null, loaded: false, reqId: 0,
    // timeline
    sessions: [], lo: 0, hi: 0, cursor: 0, playTimer: null,
  };

  function buildMapUi() {
    mapRoot.innerHTML = `
      <h1 style="margin-top:36px">Map</h1>
      <div class="card">
        <div class="map-controls">
          <div class="pill-row" id="map-period">
            <button type="button" class="mpill active" data-p="now">Now</button>
            <span class="pill-sep"></span>
            <button type="button" class="mpill" data-p="day">Day</button>
            <button type="button" class="mpill" data-p="week">Week</button>
            <button type="button" class="mpill" data-p="month">Month</button>
            <button type="button" class="mpill" data-p="year">Year</button>
            <button type="button" class="mpill" data-p="all">All time</button>
            <button type="button" class="mpill" data-p="custom">Date range</button>
          </div>
          <button type="button" class="mpill" id="map-measure" title="Measure the distance between two points">Measure</button>
          <input type="date" id="map-date" title="Date (UTC)" />
          <span id="map-to-wrap" hidden>to <input type="date" id="map-to" /></span>
        </div>
        <div class="map-controls">
          <input type="search" id="map-q" placeholder="Find a callsign" autocomplete="off" />
          <select id="map-band" title="Band">
            <option value="">All bands</option><option value="mf">LF / MF (below 3 MHz)</option><option value="hf">HF (3–30 MHz)</option>
            <option value="vhf">VHF (30–300 MHz)</option><option value="uhf">UHF (above 300 MHz)</option>
          </select>
          <a class="export-link" id="map-export" href="#" title="Download the sessions of this period as a spreadsheet">Export CSV</a>
        </div>
        <div class="map-controls" id="map-view-row" hidden>
          <div class="pill-row" id="map-view">
            <button type="button" class="mpill active" data-v="timeline">Timeline</button>
            <button type="button" class="mpill" data-v="aggregate">All callsigns</button>
          </div>
          <label class="inline" id="map-heat-wrap" hidden><input type="checkbox" id="map-heat" /> Heatmap</label>
        </div>
        <div class="map-range-label" id="map-label"></div>
        <div class="map-measure" id="map-measure-out" hidden></div>
        <div id="map-canvas"></div>
        <div class="tl-bar" id="map-tl" hidden>
          <button type="button" class="mpill" id="map-play" title="Play">▶</button>
          <input type="range" id="map-slider" min="0" max="1" step="60" value="0" />
          <span class="tl-time" id="map-tl-time"></span>
        </div>
        <div class="map-legend" id="map-legend"></div>
        <div id="map-notes"></div>
      </div>`;

    $("map-date").max = todayUtc();
    $("map-to").max = todayUtc();
    $("map-date").value = S.date;
    $("map-to").value = S.to;

    $("map-period").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-p]");
      if (b) setPeriod(b.dataset.p);
    });
    $("map-view").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (b && !b.disabled) { S.view = b.dataset.v; refresh(); }
    });
    $("map-date").addEventListener("change", (e) => {
      const v = e.target.value || todayUtc();
      if (S.period === "custom") S.from = v; else S.date = v;
      if (S.period === "now") S.period = "day";
      refresh();
    });
    $("map-to").addEventListener("change", (e) => { S.to = e.target.value || todayUtc(); refresh(); });
    $("map-heat").addEventListener("change", (e) => { S.heat = e.target.checked; refresh(); });
    $("map-slider").addEventListener("input", (e) => { S.cursor = Number(e.target.value); drawActive(); });
    $("map-play").addEventListener("click", togglePlay);
    $("map-measure").addEventListener("click", toggleMeasure);
    let qTimer = null;
    $("map-q").addEventListener("input", (e) => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => { S.q = e.target.value.trim(); S.fitted = false; refresh(); }, 250);
    });
    $("map-band").addEventListener("change", (e) => { S.band = e.target.value; S.fitted = false; refresh(); });
  }

  function setPeriod(p) {
    S.period = p;
    if (p === "custom") { S.from = $("map-date").value || todayUtc(); S.to = $("map-to").value || todayUtc(); }
    refresh();
  }

  function spanDays() {
    if (S.period === "day") return 1;
    if (S.period === "week") return 7;
    if (S.period === "month") return 31;
    if (S.period === "custom") {
      const a = Date.parse(S.from), b = Date.parse(S.to);
      return Number.isFinite(a) && Number.isFinite(b) ? Math.abs(b - a) / 86400000 + 1 : 999;
    }
    return 9999; // year / all
  }
  const timelineAllowed = () => S.period !== "now" && spanDays() <= 31;
  // timeline only makes sense for short periods; otherwise fall back without losing the preference
  const effView = () => (S.view === "timeline" && timelineAllowed() ? "timeline" : "aggregate");

  function syncControls() {
    $("map-q").value = S.q;
    $("map-band").value = S.band;
    $("map-export").href = `${API}/api/export/sessions.csv?${rangeParams(S.period === "now" ? "day" : S.period).toString()}`;
    document.querySelectorAll("#map-period .mpill").forEach((b) => b.classList.toggle("active", b.dataset.p === S.period));
    const isNow = S.period === "now", isCustom = S.period === "custom", isAll = S.period === "all";
    $("map-date").hidden = isAll;
    $("map-date").value = isCustom ? S.from : S.date;
    $("map-to-wrap").hidden = !isCustom;
    $("map-to").value = S.to;
    $("map-view-row").hidden = isNow;
    document.querySelectorAll("#map-view .mpill").forEach((b) => {
      b.classList.toggle("active", b.dataset.v === effView());
      if (b.dataset.v === "timeline") b.disabled = !timelineAllowed();
    });
    $("map-heat-wrap").hidden = isNow || effView() !== "aggregate" || typeof L === "undefined" || typeof L.heatLayer !== "function";
    $("map-heat").checked = S.heat;
    $("map-tl").hidden = isNow || effView() !== "timeline";
  }

  /* ---- map plumbing ---- */
  // Basemap providers, tried in order. The dashboard sends "Referrer-Policy: no-referrer", which
  // OpenStreetMap rejects (403), so tiles are requested with referrerPolicy "origin" instead.
  // A provider is chosen by probing one tile, because a refused tile still *looks* like a loaded image.
  // To force your own tiles: set window.SIMRX_TILES = { url, attribution, maxZoom, invert } before map.js.
  const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  const TILE_PROVIDERS = [
    { name: "OpenStreetMap", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", invert: true,
      opts: { maxZoom: 19, referrerPolicy: "origin", attribution: OSM_ATTR } },
    { name: "Esri Dark Gray", url: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      invert: false, opts: { maxZoom: 16, referrerPolicy: "origin", attribution: "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ" } },
    { name: "OSM France", url: "https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png", invert: true,
      opts: { maxZoom: 19, subdomains: "abc", referrerPolicy: "origin", attribution: OSM_ATTR + " &middot; OSM France" } },
  ];
  if (window.SIMRX_TILES) {
    const c = window.SIMRX_TILES;
    TILE_PROVIDERS.unshift({ name: "custom", url: c.url, invert: !!c.invert,
      opts: { maxZoom: c.maxZoom || 19, referrerPolicy: "origin", attribution: c.attribution || "" } });
  }

  async function probeProvider(p) {
    // 1 = tile served, 0 = refused (403/401/...), -1 = could not tell (CORS/network)
    const url = p.url.replace("{s}", (p.opts.subdomains || "a")[0]).replace("{z}", 2).replace("{x}", 1).replace("{y}", 1);
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 6000);
    try {
      const res = await fetch(url, { mode: "cors", referrerPolicy: "origin", signal: ctl.signal });
      return res.ok ? 1 : 0;
    } catch (_) { return -1; } finally { clearTimeout(timer); }
  }
  let providerPromise = null;
  function chooseProvider() {
    // probed once per page load, then shared by every map
    if (!providerPromise) providerPromise = (async () => {
      let chosen = null, unsure = null;
      for (const p of TILE_PROVIDERS) {
        const r = await probeProvider(p);
        if (r === 1) { chosen = p; break; }
        if (r === -1 && !unsure) unsure = p;
      }
      return chosen || unsure;
    })();
    return providerPromise;
  }
  async function addBasemapTo(map, container) {
    const chosen = await chooseProvider();
    if (!chosen) return null;
    container.classList.toggle("tiles-invert", chosen.invert);
    const layer = L.tileLayer(chosen.url, chosen.opts).addTo(map);
    layer.bringToBack();
    return chosen;
  }
  window.simrxAddBasemap = addBasemapTo;
  async function addBasemap() {
    const chosen = await addBasemapTo(S.map, $("map-canvas"));
    if (!chosen) {
      $("map-canvas").classList.add("no-tiles");
      notes({ text: "No basemap could be loaded (no provider accepted the request); markers are still drawn.", warn: true });
    } else S.basemap = chosen.name;
  }

  function ensureMap() {
    if (S.map) return true;
    if (typeof L === "undefined") {
      $("map-canvas").innerHTML = '<div class="empty-state">Map library did not load (needs access to cdn.jsdelivr.net)</div>';
      return false;
    }
    S.map = L.map("map-canvas", { worldCopyJump: true, minZoom: 2, zoomSnap: 0.5 }).setView([30, 10], 2);
    ["now", "faint", "active", "agg"].forEach((k) => { S.layers[k] = L.layerGroup().addTo(S.map); });
    S.map.on("popupopen", () => { if (S.measure.on) S.map.closePopup(); });   // no popups while measuring
    addBasemap();
    return true;
  }
  function clearLayers() {
    Object.values(S.layers).forEach((g) => g.clearLayers());
    if (S.heatLayer) { S.map.removeLayer(S.heatLayer); S.heatLayer = null; }
  }
  function fit(points, maxZoom) {
    if (!points.length) return;
    if (points.length === 1) { S.map.setView(points[0], Math.min(maxZoom || 6, 6)); return; }
    S.map.fitBounds(L.latLngBounds(points), { padding: [30, 30], maxZoom: maxZoom || 8 });
  }
  const dot = (ll, o) => {
    const m = L.circleMarker(ll, Object.assign({ weight: 1, color: "#05080C", fillOpacity: 0.9 }, o));
    // Leaflet stops the click once a popup opens, so the map never sees it: pick the point here
    m.on("click", () => { if (S.measure.on) measurePick({ ll: m.getLatLng(), label: m.options.label || null }); });
    return m;
  };
  function legend(items) {
    $("map-legend").innerHTML = items.map(([c, t]) => `<span><span class="ldot" style="background:${c}"></span>${t}</span>`).join("");
  }
  function notes(...lines) {
    $("map-notes").innerHTML = lines.filter(Boolean).map((l) => `<div class="map-note${l.warn ? " warn" : ""}">${l.text ?? l}</div>`).join("");
  }

  /* ---- refresh: fetch + render for the current selection ---- */
  function rangeParams(period) {
    const p = new URLSearchParams({ period });
    if (period === "custom") { p.set("from", S.from); p.set("to", S.to); } else if (period !== "all") p.set("date", S.period === "now" ? todayUtc() : S.date);
    if (S.q) p.set("q", S.q);
    if (S.band) p.set("band", S.band);
    return p;
  }
  function query() {
    const p = rangeParams(S.period);
    p.set("view", effView());
    return p.toString();
  }
  const bandOf = (f) => (f == null ? null : f < 3000 ? "mf" : f < 30000 ? "hf" : f < 300000 ? "vhf" : "uhf");
  const wanted = (name, f) => (!S.q || String(name || "").toLowerCase().includes(S.q.toLowerCase())) && (!S.band || bandOf(f) === S.band);

  // the view is shareable: /#period=week&date=2026-10-20&band=hf&q=ntd
  function writeHash() {
    if (!statsTabActive()) return;
    const mapKeys = ["tab", "period", "date", "from", "to", "view", "q", "band", "heat"];
    if (S.period === "now" && !S.q && !S.band) {
      if (mapKeys.some((k) => new URLSearchParams(location.hash.slice(1)).has(k))) history.replaceState(null, "", location.pathname + location.search);
      return;
    }
    const p = rangeParams(S.period);
    p.set("view", S.view);
    if (S.heat) p.set("heat", "1");
    history.replaceState(null, "", "#" + p.toString());
  }
  function readHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    if (!h.has("period")) return false;
    const day = /^\d{4}-\d{2}-\d{2}$/;
    if (["now", "day", "week", "month", "year", "all", "custom"].includes(h.get("period"))) S.period = h.get("period");
    if (day.test(h.get("date") || "")) S.date = h.get("date");
    if (day.test(h.get("from") || "")) S.from = h.get("from");
    if (day.test(h.get("to") || "")) S.to = h.get("to");
    if (["timeline", "aggregate"].includes(h.get("view"))) S.view = h.get("view");
    S.q = (h.get("q") || "").slice(0, 50);
    S.band = ["mf", "hf", "vhf", "uhf"].includes(h.get("band")) ? h.get("band") : "";
    S.heat = h.get("heat") === "1";
    return true;
  }

  async function refresh() {
    syncControls();
    stopPlay();
    if (S.nowTimer) { clearInterval(S.nowTimer); S.nowTimer = null; }
    if (!ensureMap()) return;
    S.map.invalidateSize();
    const my = ++S.reqId;
    try {
      if (S.period === "now") {
        const d = await getJson("/api/map/now");
        if (my !== S.reqId) return;
        renderNow(d);
        S.nowTimer = setInterval(async () => {
          if (!statsTabActive() || document.hidden || S.period !== "now") return;
          try { renderNow(await getJson("/api/map/now")); } catch (_) { /* keep last picture */ }
        }, 10000);
      } else {
        const d = await getJson(`/api/map/sessions?${query()}`);
        if (my !== S.reqId) return;
        $("map-label").textContent = `${d.label} (UTC)`;
        if (d.view === "timeline") renderTimeline(d); else renderAggregate(d);
      }
      S.loaded = true;
      writeHash();
    } catch (e) {
      clearLayers();
      notes({ text: `Could not load map data: ${E(e.message)}`, warn: true });
    }
  }

  /* ---- NOW ---- */
  function renderNow(d) {
    clearLayers();
    $("map-label").textContent = "Live, updates every 10 s";
    $("map-tl").hidden = true;
    const pts = [];
    d = Object.assign({}, d, {
      receivers: d.receivers.filter((r) => wanted(r.callsign, r.center_khz)),
      transmitters: d.transmitters.filter((t) => wanted(t.callsign, t.freq_khz)),
    });
    d.receivers.forEach((r) => {
      pts.push([r.lat, r.lon]);
      dot([r.lat, r.lon], { radius: 5, fillColor: COL.rx, label: `Receiver ${r.callsign || ""}`.trim() })
        .bindPopup(`<b>${E(r.callsign) || "receiver"}</b><br>Receiver${r.center_khz ? ` · ${fmtKhz(r.center_khz)}` : ""}`)
        .addTo(S.layers.now);
    });
    d.transmitters.forEach((t) => {
      pts.push([t.lat, t.lon]);
      dot([t.lat, t.lon], { radius: 8, fillColor: COL.tx, color: "#fff", label: t.callsign })
        .bindPopup(txPopup(t, true)).bindTooltip(E(t.callsign)).addTo(S.layers.now);
    });
    if (!S.fitted && pts.length) { fit(pts, 6); S.fitted = true; }
    legend([[COL.tx, "Transmitting"], [COL.rx, "Receivers"]]);
    notes(
      d.transmitters_without_position > 0 && "Some transmitting stations have no known position and are not shown.",
      !pts.length && "No positions to show right now.",
    );
  }
  const profileLink = (name) => `<br><a href="#callsign=${encodeURIComponent(name)}" class="profile-link" data-callsign="${E(name)}" style="margin-left:0">Open profile ↗</a>`;
  function txPopup(t, live) {
    return `<b>${E(t.callsign)}</b><br>${fmtKhz(t.freq_khz)} · ${E(t.mode)}${t.band ? ` · ${E(t.band)}` : ""}<br>` +
      `${E(t.source_type)}${t.power_w != null ? ` · ${t.power_w} W` : ""}<br>` +
      (live ? `On air since ${ymdhm(t.started_at)} UTC` : `${ymdhm(t.started_at)} UTC`) + `<br>${fmtPos(t.lat, t.lon)}` + profileLink(t.callsign);
  }

  /* ---- TIMELINE ---- */
  function renderTimeline(d) {
    clearLayers();
    S.sessions = d.sessions;
    S.lo = d.range_start;
    S.hi = Math.max(S.lo + 60, Math.min(d.range_end, nowSec()));
    const sl = $("map-slider");
    sl.min = S.lo; sl.max = S.hi;
    S.cursor = busiestMoment(d.sessions, S.lo, S.hi);
    sl.value = S.cursor;

    // faint trail: every distinct (callsign, place) of the period
    const seen = new Set(), pts = [];
    d.sessions.forEach((s) => {
      const k = `${s.callsign}|${s.lat}|${s.lon}`;
      if (seen.has(k)) return;
      seen.add(k); pts.push([s.lat, s.lon]);
      dot([s.lat, s.lon], { radius: 3, fillColor: COL.faint, fillOpacity: 0.35, color: COL.faint, weight: 0, interactive: false }).addTo(S.layers.faint);
    });
    fit(pts, 7);
    drawActive();
    legend([[COL.tx, "Transmitting at the selected time"], [COL.faint, "Seen at some point in this period"]]);
    notes(
      d.sessions_without_position > 0 && "Sessions without coordinates are not shown.",
      d.truncated && { text: "Too many sessions: only the first 20 000 are shown.", warn: true },
      !d.sessions.length && "No sessions with coordinates in this period.",
    );
  }

  function sessionEnd(s) { return s.open ? Math.max(s.ended_at || 0, nowSec()) : s.ended_at || s.started_at; }

  function busiestMoment(sessions, lo, hi) {
    // the instant with the most simultaneous transmitters = a sensible starting point
    const ev = [];
    sessions.forEach((s) => { ev.push([Math.max(s.started_at, lo), 1]); ev.push([Math.min(sessionEnd(s), hi) + 1, -1]); });
    ev.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    let cur = 0, best = 0, at = hi;
    ev.forEach(([t, d]) => { cur += d; if (cur > best) { best = cur; at = t; } });
    return best ? Math.min(Math.max(at, lo), hi) : hi;
  }

  function drawActive() {
    S.layers.active.clearLayers();
    const t = S.cursor;
    S.sessions.forEach((s) => {
      if (s.started_at <= t && sessionEnd(s) >= t) {
        dot([s.lat, s.lon], { radius: 7, fillColor: COL.tx, color: "#fff", label: s.callsign })
          .bindPopup(txPopup(s, false)).bindTooltip(E(s.callsign)).addTo(S.layers.active);
      }
    });
    $("map-tl-time").textContent = `${ymdhm(t)} UTC`;
    $("map-slider").value = t;
  }

  function togglePlay() { if (S.playTimer) stopPlay(); else startPlay(); }
  function startPlay() {
    if (effView() !== "timeline" || S.hi <= S.lo) return;
    if (S.cursor >= S.hi - 1) S.cursor = S.lo;
    const step = Math.max(60, Math.round((S.hi - S.lo) / 240));
    $("map-play").textContent = "❚❚";
    S.playTimer = setInterval(() => {
      S.cursor = Math.min(S.cursor + step, S.hi);
      drawActive();
      if (S.cursor >= S.hi) stopPlay();
    }, 120);
  }
  function stopPlay() {
    if (S.playTimer) { clearInterval(S.playTimer); S.playTimer = null; }
    const b = $("map-play"); if (b) b.textContent = "▶";
  }

  /* ---- ALL CALLSIGNS (no time) + heatmap ---- */
  function renderAggregate(d) {
    clearLayers();
    $("map-tl").hidden = true;
    const places = d.places;
    const pts = places.map((p) => [p.lat, p.lon]);
    const max = Math.max(1, ...places.map((p) => p.tx_count));

    if (S.heat && typeof L.heatLayer === "function") {
      const counts = places.map((p) => p.tx_count).sort((a, b) => a - b);
      const p95 = counts.length ? counts[Math.floor(counts.length * 0.95)] : 1;
      S.heatLayer = L.heatLayer(places.map((p) => [p.lat, p.lon, p.tx_count]),
        { radius: 22, blur: 18, minOpacity: 0.3, max: Math.max(1, p95) }).addTo(S.map);
      legend([["linear-gradient(90deg,#2f5bd8,#3fd1c5,#f0e35a,#e8453c)", "More transmissions → hotter"]]);
    } else {
      places.forEach((p) => {
        dot([p.lat, p.lon], { radius: Math.min(16, 4 + 2 * Math.sqrt(p.tx_count)), fillColor: COL.agg, fillOpacity: 0.65, label: p.callsign })
          .bindPopup(`<b>${E(p.callsign)}</b><br>${p.tx_count} TX · ${dur(p.total_seconds)} on air<br>` +
                     `last: ${ymdhm(p.last_at)} UTC<br>${fmtPos(p.lat, p.lon)}` + profileLink(p.callsign))
          .bindTooltip(`${E(p.callsign)} · ${p.tx_count}`).addTo(S.layers.agg);
      });
      legend([[COL.agg, "One dot per callsign and place; bigger = more TX"]]);
    }
    fit(pts, 7);

    notes(
      d.sessions_without_position > 0 && "Sessions without coordinates are not shown.",
      d.truncated && { text: "Too many places: only the 5 000 busiest are shown.", warn: true },
      !places.length && "No sessions with coordinates in this period.",
    );
  }


  /* ======================================================== measure tool == */
  // Great-circle distance (haversine, mean Earth radius). The map is flat, the Earth is not:
  // the line is drawn along the shortest path over the globe, so it bends on the map.
  const R_EARTH_KM = 6371.0088;
  const rad = (d) => (d * Math.PI) / 180, deg = (r) => (r * 180) / Math.PI;

  function distanceKm(a, b) {
    const dLat = rad(b.lat - a.lat), dLon = rad(b.lng - a.lng);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
    return 2 * R_EARTH_KM * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function bearingDeg(a, b) {
    const y = Math.sin(rad(b.lng - a.lng)) * Math.cos(rad(b.lat));
    const x = Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) - Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lng - a.lng));
    return (deg(Math.atan2(y, x)) + 360) % 360;
  }
  function greatCircle(a, b, steps = 96) {
    const v = (p) => [Math.cos(rad(p.lat)) * Math.cos(rad(p.lng)), Math.cos(rad(p.lat)) * Math.sin(rad(p.lng)), Math.sin(rad(p.lat))];
    const A = v(a), B = v(b);
    const omega = Math.acos(Math.min(1, Math.max(-1, A[0] * B[0] + A[1] * B[1] + A[2] * B[2])));
    if (omega < 1e-9) return [[a.lat, a.lng], [b.lat, b.lng]];
    const out = [];
    let prevLon = a.lng;
    for (let i = 0; i <= steps; i++) {
      const f = i / steps, k1 = Math.sin((1 - f) * omega) / Math.sin(omega), k2 = Math.sin(f * omega) / Math.sin(omega);
      const x = k1 * A[0] + k2 * B[0], y = k1 * A[1] + k2 * B[1], z = k1 * A[2] + k2 * B[2];
      let lon = deg(Math.atan2(y, x));
      while (lon - prevLon > 180) lon -= 360;      // keep the line continuous across the date line
      while (lon - prevLon < -180) lon += 360;
      prevLon = lon;
      out.push([deg(Math.atan2(z, Math.hypot(x, y))), lon]);
    }
    return out;
  }
  function fmtDist(km) {
    if (km < 1) return `${Math.round(km * 1000)} m`;
    const f = (v) => v.toLocaleString("en-US", { maximumFractionDigits: km < 100 ? 1 : 0 });
    return `${f(km)} km (${f(km * 0.621371)} mi)`;
  }

  function nearestMarker(latlng) {
    // closest station/receiver dot to the click (within its own radius, at least 14 px)
    let best = null, bestD = Infinity;
    const at = S.map.latLngToContainerPoint(latlng);
    ["now", "active", "agg"].forEach((k) => S.layers[k].eachLayer((m) => {
      if (!m.getLatLng) return;
      const d = at.distanceTo(S.map.latLngToContainerPoint(m.getLatLng()));
      if (d <= Math.max(14, (m.options.radius || 0) + 4) && d < bestD) { best = m; bestD = d; }
    }));
    return best;
  }

  function measureOut(text) {
    const o = $("map-measure-out");
    o.hidden = !text;
    o.innerHTML = text || "";
  }
  function resetMeasure() {
    const M = S.measure;
    M.pts = [];
    if (M.layer) M.layer.clearLayers();
  }
  function toggleMeasure() {
    const M = S.measure;
    if (!ensureMap()) return;
    M.on = !M.on;
    $("map-measure").classList.toggle("active", M.on);
    $("map-canvas").classList.toggle("measuring", M.on);
    if (!M.layer) M.layer = L.layerGroup().addTo(S.map);
    resetMeasure();
    measureOut(M.on ? "Measure: click two points on the map (stations and receivers snap)." : "");
    if (M.on && !M.bound) {
      M.bound = true;
      S.map.on("click", (e) => {
        if (!M.on) return;
        const m = nearestMarker(e.latlng);
        measurePick(m ? { ll: m.getLatLng(), label: m.options.label || null } : { ll: e.latlng, label: null });
        S.map.closePopup();
      });
    }
  }
  function measurePick(p) {
    const M = S.measure;
    if (Date.now() - (M.lastAt || 0) < 80) return;     // marker click and map click can both report one click
    M.lastAt = Date.now();
    if (M.pts.length >= 2) resetMeasure();
    M.pts.push(p);
    L.circleMarker(p.ll, { radius: 9, color: "#fff", weight: 2, fillColor: "#d9a441", fillOpacity: 1, interactive: false }).addTo(M.layer);
    if (M.pts.length === 1) { measureOut(`From <b>${E(p.label) || "point 1"}</b>: click the second point.`); return; }
    const [A, B] = M.pts, a = A.ll, b = B.ll;
    const km = distanceKm(a, b), path = greatCircle(a, b);
    L.polyline(path, { color: "#d9a441", weight: 3, dashArray: "8 6", interactive: false }).addTo(M.layer);
    L.circleMarker(path[Math.floor(path.length / 2)], { radius: 0, opacity: 0, interactive: false })
      .bindTooltip(fmtDist(km), { permanent: true, direction: "top", className: "measure-tip" }).addTo(M.layer);
    measureOut(`<b>${E(A.label) || "Point 1"}</b> → <b>${E(B.label) || "Point 2"}</b>: <b>${fmtDist(km)}</b> · bearing ${Math.round(bearingDeg(a, b))}°`);
  }

  /* ============================================================== boot == */
  if (onlineRoot) buildOnlineUi();
  if (mapRoot) buildMapUi();

  let booted = false;
  function onStatsShown() {
    if (onlineRoot) loadOnline();
    if (mapRoot) {
      if (!booted) { booted = true; refresh(); }
      else if (S.map) { setTimeout(() => S.map.invalidateSize(), 50); if (S.period === "now") refresh(); }
    }
  }
  const statsBtn = document.querySelector('[data-tab="stats"]');
  if (statsBtn) statsBtn.addEventListener("click", () => setTimeout(onStatsShown, 0));
  if (readHash() && statsBtn) { syncControls(); statsBtn.click(); }       // opened from a shared link
  else if (statsTabActive()) onStatsShown();
  window.addEventListener("hashchange", () => {
    if (!new URLSearchParams(location.hash.slice(1)).has("period")) return;
    readHash(); syncControls();
    if (statsBtn && !statsTabActive()) statsBtn.click(); else if (booted) refresh();
  });
})();
