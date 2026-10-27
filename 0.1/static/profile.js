/* SimRX: callsign profiles (the "Callsigns" tab).
   Opened from the search box, from any callsign in the On Air / Timeline tabs and map popups,
   or with a link like  /#callsign=NTD81  . Loaded after the dashboard's inline script and map.js. */
(function () {
  "use strict";

  const root = document.getElementById("profile-root");
  if (!root) return;

  const $ = (id) => document.getElementById(id);
  const E = (v) => (typeof esc === "function" ? esc(v) : String(v ?? ""));
  const dur = (s) => (typeof fmtDuration === "function" ? fmtDuration(s) : `${Math.round(s / 60)}m`);
  const when = (t) => (t && typeof fmtTime === "function" ? fmtTime(t) : "—");
  const freq = (f) => (typeof fmtFreq === "function" ? fmtFreq(f) : `${f} kHz`);
  const none = '<span class="none">NONE</span>';
  const pos = (v) => (v == null ? none : Number(v).toFixed(4));

  let current = null;           // name of the profile on screen
  let dayChart = null, miniMap = null;
  let searchTimer = null, reqId = 0;

  root.innerHTML = `
    <div class="card">
      <div class="cs-search">
        <label>Callsign<input type="text" id="cs-input" list="cs-list" placeholder="Start typing a callsign" autocomplete="off" /></label>
        <datalist id="cs-list"></datalist>
        <button type="button" class="primary" id="cs-open">Open profile</button>
      </div>
    </div>
    <div id="profile-body" style="margin-top:14px"></div>`;

  /* -------------------------------------------------------------- search -- */
  $("cs-input").addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = $("cs-input").value.trim();
    if (q.length < 1) { $("cs-list").innerHTML = ""; return; }
    searchTimer = setTimeout(async () => {
      try {
        const res = await fetch(`${API}/api/callsigns/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        $("cs-list").innerHTML = data.results.map((r) => `<option value="${E(r.callsign)}">${r.sessions} TX</option>`).join("");
      } catch (_) { /* suggestions are optional */ }
    }, 180);
  });
  const submit = () => { const v = $("cs-input").value.trim(); if (v) openProfile(v); };
  $("cs-open").addEventListener("click", submit);
  $("cs-input").addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  $("cs-input").addEventListener("change", submit);        // picking a suggestion

  /* --------------------------------------------------------- navigation -- */
  function showTab() {
    const btn = document.querySelector('#tabs button[data-tab="callsigns"]');
    if (btn && !btn.classList.contains("active")) btn.click();
  }
  function openProfile(name) {
    const hash = `#callsign=${encodeURIComponent(name)}`;
    if (location.hash === hash) { showTab(); loadProfile(name); return; }
    location.hash = hash;          // hashchange does the rest, and the back button works
  }
  window.openCallsignProfile = openProfile;
  window.reloadProfile = () => { if (current) loadProfile(current); };

  function fromHash() {
    const name = new URLSearchParams(location.hash.slice(1)).get("callsign");
    if (name) { showTab(); loadProfile(name); }
  }
  window.addEventListener("hashchange", fromHash);

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-callsign]");
    if (!el || e.target.closest("button")) return;
    e.preventDefault();
    openProfile(el.dataset.callsign);
  });

  /* -------------------------------------------------------------- render -- */
  const bars = (items, label, value, fmt) => {
    const max = Math.max(1, ...items.map(value));
    return items.length ? items.map((it) => `
      <div class="bar-row"><span class="bar-label">${E(label(it))}</span>
        <span class="bar"><span style="width:${Math.max(3, (value(it) / max) * 100)}%"></span></span>
        <span class="bar-val">${fmt(it)}</span></div>`).join("") : '<div class="small-note">No data</div>';
  };

  async function loadProfile(name) {
    const my = ++reqId;
    current = name;
    $("cs-input").value = name;
    const body = $("profile-body");
    body.innerHTML = '<div class="small-note">Loading…</div>';
    let p;
    try {
      const res = await fetch(`${API}/api/callsign?name=${encodeURIComponent(name)}`);
      p = await res.json();
    } catch (e) { body.innerHTML = '<div class="empty-state">Server unreachable</div>'; return; }
    if (my !== reqId) return;
    destroyCharts();
    if (!p.found) { body.innerHTML = `<div class="card"><div class="empty-state">No transmissions recorded for “${E(name)}”.</div></div>`; return; }
    current = p.callsign;
    render(p);
  }

  function destroyCharts() {
    if (dayChart) dayChart.destroy();
    dayChart = null;
    if (miniMap) { miniMap.remove(); miniMap = null; }
  }

  function render(p) {
    const ago = Math.max(0, Math.floor(Date.now() / 1000) - p.last_seen);
    const status = p.on_air ? '<span class="pill-on">on air now</span>'
      : `<span class="pill-off">last seen ${dur(ago)} ago</span>`;
    $("profile-body").innerHTML = `
      <div class="profile-head"><h2>${E(p.callsign)}</h2>${status}
        <span class="pill-off" title="Place by total time on air, all time">#${p.rank} by time on air</span>
        <a class="export-link" href="${API}/api/export/sessions.csv?callsign=${encodeURIComponent(p.callsign)}">Export CSV</a></div>

      <div class="profile-cards">
        <div class="card record-card"><div class="period-label">Transmissions</div><div class="duration">${p.sessions}</div></div>
        <div class="card record-card"><div class="period-label">Time on air</div><div class="duration">${dur(p.total_seconds)}</div></div>
        <div class="card record-card"><div class="period-label">Longest transmission</div><div class="duration">${dur(p.longest_seconds)}</div>
          <div class="small-note">${when(p.longest_at)}</div></div>
        <div class="card record-card"><div class="period-label">Active days</div><div class="duration">${p.days_active}</div></div>
        <div class="card record-card"><div class="period-label">First seen</div><div class="small-note" style="font-size:.9rem;color:var(--text)">${when(p.first_seen)}</div></div>
      </div>

      <div class="profile-cols">
        <div class="card"><h3>Favourite bands</h3>${bars(p.bands, (b) => b.name, (b) => b.seconds, (b) => dur(b.seconds))}</div>
        <div class="card"><h3>Modes</h3>${bars(p.modes, (m) => m.name, (m) => m.seconds, (m) => dur(m.seconds))}
          <h3 style="margin-top:16px">Sources</h3>${bars(p.sources, (m) => m.name, (m) => m.sessions, (m) => `${m.sessions} TX`)}</div>
        <div class="card"><h3>Most used frequencies</h3>${bars(p.frequencies, (f) => freq(f.freq_khz), (f) => f.seconds, (f) => dur(f.seconds))}</div>
      </div>

      <div class="profile-cols">
        <div class="card"><h3>Time on air, last 90 days</h3><canvas id="pf-day" height="110"></canvas></div>
      </div>

      <div class="card profile-sec"><h3>Where</h3>${p.places.length
        ? '<div id="pf-map" class="profile-map"></div><div class="places-list" id="pf-places"></div>'
        : '<div class="small-note">No positions recorded for this callsign (older transmissions have none).</div>'}</div>

      <div class="card profile-sec"><h3>Recent transmissions</h3>
        <table><thead><tr><th>Started</th><th>Frequency</th><th>Mode</th><th>Source</th><th>Duration</th><th>Lat</th><th>Lon</th><th>Status</th></tr></thead><tbody>
        ${p.recent.map((r) => `<tr>
          <td data-label="Started">${when(r.started_at)}</td><td data-label="Frequency">${freq(r.freq_khz)}</td>
          <td data-label="Mode">${E(r.mode)}</td><td data-label="Source">${E(r.source_type)}</td>
          <td data-label="Duration">${dur(r.duration)}</td><td data-label="Lat">${pos(r.lat)}</td><td data-label="Lon">${pos(r.lon)}</td>
          <td data-label="Status"><span class="pill ${r.live ? "live" : ""}">${r.live ? "on air" : "finished"}</span></td></tr>`).join("")}
        </tbody></table>
        ${p.sessions > p.recent.length ? `<div class="small-note" style="margin-top:8px">Latest ${p.recent.length} of ${p.sessions}. “Export CSV” has all of them.</div>` : ""}
      </div>

      <div class="card profile-sec"><h3>Changes of frequency, power and position</h3>${changesTable(p)}</div>`;

    drawDays(p);
    if (p.places.length) drawMap(p);
  }

  function changesTable(p) {
    if (!p.changes.length) {
      return `<div class="small-note">${p.changes_enabled ? "No changes recorded yet." : "Not recorded."}</div>`;
    }
    const emptyOr = (v, f) => (v == null ? '<span class="empty">empty</span>' : f(v));
    const pills = typeof reasonPills === "function" ? reasonPills : (r) => E(r);
    return `<table><thead><tr><th>Time</th><th>Frequency</th><th>Mode</th><th>Power</th><th>Lat</th><th>Lon</th><th>Reason</th></tr></thead><tbody>
      ${p.changes.map((c) => `<tr><td data-label="Time">${when(c.logged_at)}</td><td data-label="Frequency">${freq(c.freq_khz)}</td>
        <td data-label="Mode">${E(c.mode)}</td><td data-label="Power">${emptyOr(c.power_w, (v) => `${v} W`)}</td>
        <td data-label="Lat">${emptyOr(c.lat, (v) => v.toFixed(4))}</td><td data-label="Lon">${emptyOr(c.lon, (v) => v.toFixed(4))}</td>
        <td data-label="Reason">${pills(c.reason)}</td></tr>`).join("")}</tbody></table>`;
  }

  const chartBase = (labels, data, color) => ({
    type: "bar",
    data: { labels, datasets: [{ data, backgroundColor: color, borderRadius: 3 }] },
    options: { responsive: true, plugins: { legend: { display: false } }, animation: false,
      scales: { x: { ticks: { color: "#7f9189", maxTicksLimit: 12 }, grid: { display: false } },
                y: { beginAtZero: true, ticks: { color: "#7f9189", precision: 0 }, grid: { color: "#23302a" } } } },
  });

  function drawDays(p) {
    if (typeof Chart === "undefined") return;
    const by = new Map(p.daily.map((d) => [d.day, d.seconds]));
    const labels = [], data = [], now = Date.now();
    for (let i = 89; i >= 0; i--) {
      const k = new Date(now - i * 86400000).toISOString().slice(0, 10);
      labels.push(k.slice(5)); data.push(Math.round((by.get(k) || 0) / 60));
    }
    const cfg = chartBase(labels, data, "#33AFC7");
    cfg.options.plugins.tooltip = { callbacks: { label: (c) => `${c.parsed.y} min on air` } };
    dayChart = new Chart($("pf-day"), cfg);
  }

  async function drawMap(p) {
    $("pf-places").innerHTML = p.places.slice(0, 12).map((pl) => `<span>${pl.lat.toFixed(3)}, ${pl.lon.toFixed(3)} · ${pl.sessions} TX</span>`).join("")
      + (p.places_total > 12 ? `<span>+${p.places_total - 12} more</span>` : "");
    if (typeof L === "undefined") { $("pf-map").outerHTML = ""; return; }
    miniMap = L.map("pf-map", { worldCopyJump: true, minZoom: 2, zoomSnap: 0.5, scrollWheelZoom: false }).setView([30, 10], 2);
    if (window.simrxAddBasemap) window.simrxAddBasemap(miniMap, $("pf-map"));
    const pts = [];
    p.places.forEach((pl) => {
      pts.push([pl.lat, pl.lon]);
      L.circleMarker([pl.lat, pl.lon], { radius: Math.min(14, 5 + 2 * Math.sqrt(pl.sessions)), color: "#05080C", weight: 1, fillColor: "#E05A47", fillOpacity: 0.85 })
        .bindPopup(`<b>${E(p.callsign)}</b><br>${pl.sessions} TX<br>${pl.lat.toFixed(4)}, ${pl.lon.toFixed(4)}`).addTo(miniMap);
    });
    if (pts.length === 1) miniMap.setView(pts[0], 5); else miniMap.fitBounds(L.latLngBounds(pts), { padding: [30, 30], maxZoom: 8 });
    setTimeout(() => miniMap && miniMap.invalidateSize(), 80);
  }

  const callsignsBtn = document.querySelector('#tabs button[data-tab="callsigns"]');
  if (callsignsBtn) callsignsBtn.addEventListener("click", () => setTimeout(() => miniMap && miniMap.invalidateSize(), 80));
  fromHash();
})();
