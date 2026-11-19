"use strict";
// Lagscope web UI. All heavy lifting happens on the server; the browser only ever gets
// pre-aggregated series (≤1500 points per line) for the visible time range.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const COLORS = ["#4e8cff", "#ff8a3d", "#3dd68c", "#e05ce0", "#f2c94c", "#5ad1e6", "#ff6369", "#a78bfa", "#9ccc65", "#c0c6d4"];
const KIND_LABEL = { lag: "Lag", good: "läuft gut", end: "Lag vorbei", auto: "FPS-Einbruch" };
const LS_KEY = "lagscope.groups";

const S = {
  from: 0, to: 0, live: false, info: null, metrics: {}, groups: [], groupsOn: new Set(),
  timeline: { sessions: [], markers: [] }, plots: [], abort: null, selTs: null, eventKinds: new Set(),
  analysisPoll: null, refreshTimer: null,
};

// ------------------------------------------------------------------ helpers
async function api(path, params, opts = {}) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) u.searchParams.set(k, v);
  const r = await fetch(u, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const post = (path, body) => api(path, null, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function fmtTime(ts, withDate = true) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return withDate ? `${d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" })} ${t}` : t;
}
function fmtDur(s) {
  s = Math.round(s);
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} Tage`;
}
function fmtVal(v, unit) {
  if (v == null || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  const n = a >= 1000 ? Math.round(v).toLocaleString("de-DE") : a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
  return unit ? `${n} ${unit}` : n;
}
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// ------------------------------------------------------------------ status
async function refreshStatus() {
  try {
    const st = await api("/api/status");
    S.info.status = st;
    const el = $("#status");
    const age = st.last_sample ? Date.now() / 1000 - st.last_sample : null;
    const ok = st.running && age != null && age < 180;
    el.className = "status " + (ok ? "ok" : "bad");
    let txt = st.running ? "Logger läuft" : "Logger läuft NICHT";
    if (st.self_cpu != null) txt += ` · Eigenverbrauch ${st.self_cpu.toFixed(2)} % CPU, ${st.self_rss ?? "?"} MB`;
    if (st.game_running) txt += " · Spiel läuft";
    $(".txt", el).textContent = txt;
  } catch (e) {
    $("#status").className = "status bad";
    $("#status .txt").textContent = "Keine Verbindung zum Logger";
  }
}

// ------------------------------------------------------------------ range handling
function setRange(from, to, live = false) {
  const now = Date.now() / 1000;
  S.from = Math.max(0, from);
  S.to = Math.min(to, now + 60);
  S.live = live;
  $("#live").classList.toggle("on", live);
  $("#rangeTxt").textContent = `${fmtTime(S.from)} – ${fmtTime(S.to)} (${fmtDur(S.to - S.from)})`;
  try { history.replaceState(null, "", `#from=${Math.round(S.from)}&to=${Math.round(S.to)}${live ? "&live=1" : ""}`); } catch (e) { /* ignore */ }
  loadTimeline();
  if ($("#tab-events").classList.contains("on")) loadEvents();
}
function span() { return S.to - S.from; }

function initToolbar() {
  $$("[data-span]").forEach((b) => b.addEventListener("click", () => {
    const s = +b.dataset.span, now = Date.now() / 1000;
    setRange(now - s, now, true);
  }));
  $("#prev").onclick = () => setRange(S.from - span() * 0.75, S.to - span() * 0.75);
  $("#next").onclick = () => { const n = Date.now() / 1000; const to = Math.min(n, S.to + span() * 0.75); setRange(to - span(), to, to >= n - 5); };
  $("#zoomout").onclick = () => { const c = (S.from + S.to) / 2, s = span() * 2; setRange(c - s / 2, c + s / 2, S.live); };
  $("#live").onclick = () => { const s = span(), n = Date.now() / 1000; setRange(n - s, n, !S.live); };
  $("#sessionSel").onchange = (e) => {
    const [a, b] = e.target.value.split(",").map(Number);
    if (a) setRange(a - 120, b + 120);
    e.target.value = "";
  };
  setInterval(() => {
    if (S.live && $("#tab-timeline").classList.contains("on") && !document.hidden) {
      const s = span(), n = Date.now() / 1000;
      setRange(n - s, n, true);
    }
  }, 30000);
}

async function loadOverview() {
  const ov = await api("/api/overview");
  const sel = $("#sessionSel");
  sel.innerHTML = '<option value="">Spielsitzung wählen …</option>';
  for (const s of ov.sessions.slice().reverse()) {
    const end = s.end || Date.now() / 1000;
    const o = document.createElement("option");
    o.value = `${s.start},${end}`;
    o.textContent = `${fmtTime(s.start)} · ${fmtDur(end - s.start)} · ${s.name || ""}`;
    sel.appendChild(o);
  }
  return ov;
}

// ------------------------------------------------------------------ groups / charts
function initGroups() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch (e) { /* ignore */ }
  const defaults = ["fps", "cpu", "cpufreq", "gpu", "vram", "mem", "pressure", "game"];
  S.groupsOn = new Set(saved || defaults);
  const box = $("#groupToggles");
  box.innerHTML = "";
  for (const g of S.groups) {
    const b = document.createElement("button");
    b.textContent = g.label;
    b.classList.toggle("on", S.groupsOn.has(g.key));
    b.onclick = () => {
      S.groupsOn.has(g.key) ? S.groupsOn.delete(g.key) : S.groupsOn.add(g.key);
      b.classList.toggle("on");
      try { localStorage.setItem(LS_KEY, JSON.stringify([...S.groupsOn])); } catch (e) { /* ignore */ }
      loadTimeline();
    };
    box.appendChild(b);
  }
}

function chartKeys() {
  return Object.values(S.metrics).filter((m) => m.chart && S.groupsOn.has(m.group)).map((m) => m.key);
}

async function loadTimeline() {
  if (S.abort) S.abort.abort();
  const ac = new AbortController();
  S.abort = ac;
  $("#charts").classList.add("loading");
  const width = $("#charts").clientWidth || 900;
  const points = Math.max(200, Math.min(1500, Math.round(width * 1.2)));
  try {
    const [tl, data] = await Promise.all([
      api("/api/timeline", { from: S.from - 3600, to: S.to }, { signal: ac.signal }),
      api("/api/series", { from: S.from, to: S.to, keys: chartKeys().join(","), points }, { signal: ac.signal }),
    ]);
    if (ac.signal.aborted) return;
    S.timeline = tl;
    renderMarkerChips();
    renderCharts(data);
  } catch (e) {
    if (e.name !== "AbortError") $("#charts").innerHTML = `<div class="empty">Fehler beim Laden: ${esc(e.message)}</div>`;
  } finally {
    if (S.abort === ac) $("#charts").classList.remove("loading");
  }
}

function renderMarkerChips() {
  const box = $("#markerChips");
  const ms = S.timeline.markers.filter((m) => m.ts >= S.from && m.ts <= S.to);
  if (!ms.length) { box.innerHTML = '<span class="small muted">Keine Marker in diesem Zeitraum.</span>'; return; }
  box.innerHTML = "";
  for (const m of ms.slice(-40)) {
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = `<i style="background:var(--${m.kind === "lag" ? "lag" : m.kind})"></i>${esc(KIND_LABEL[m.kind] || m.kind)} · ${fmtTime(m.ts, span() > 80000)}`;
    c.title = (m.fg_name ? `Vordergrund: ${m.fg_name}` : "") + (m.note ? `\nNotiz: ${m.note}` : "");
    c.onclick = () => showMarker(m.id, true);
    box.appendChild(c);
  }
}

// German 24h time axis
function timeTicks(u, splits, ai, space, incr) {
  let prevDay = null;
  return splits.map((v) => {
    const d = new Date(v * 1000);
    const day = d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
    const hm = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: incr < 60 ? "2-digit" : undefined });
    let label;
    if (incr >= 86400) label = d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
    else label = day !== prevDay && (prevDay !== null || incr >= 3600) ? `${hm}\n${day}` : hm;
    prevDay = day;
    return label;
  });
}

// uPlot plugin: game sessions as shaded background, markers as vertical lines
function overlayPlugin() {
  return {
    hooks: {
      drawClear: (u) => {
        const ctx = u.ctx, { top, height } = u.bbox;
        ctx.save();
        ctx.fillStyle = cssVar("--game");
        for (const s of S.timeline.sessions) {
          const x0 = u.valToPos(Math.max(s.start, u.scales.x.min), "x", true);
          const x1 = u.valToPos(Math.min(s.end || Date.now() / 1000, u.scales.x.max), "x", true);
          if (x1 > x0) ctx.fillRect(x0, top, x1 - x0, height);
        }
        ctx.restore();
      },
      draw: (u) => {
        const ctx = u.ctx, { top, height } = u.bbox;
        ctx.save();
        ctx.lineWidth = 2 * devicePixelRatio;
        for (const m of S.timeline.markers) {
          if (m.ts < u.scales.x.min || m.ts > u.scales.x.max) continue;
          const x = u.valToPos(m.ts, "x", true);
          ctx.strokeStyle = cssVar(m.kind === "lag" ? "--lag" : `--${m.kind}`);
          ctx.setLineDash(m.kind === "lag" ? [] : [4 * devicePixelRatio, 3 * devicePixelRatio]);
          ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        }
        if (S.selTs && S.selTs >= u.scales.x.min && S.selTs <= u.scales.x.max) {
          const x = u.valToPos(S.selTs, "x", true);
          ctx.strokeStyle = cssVar("--text"); ctx.globalAlpha = 0.5; ctx.setLineDash([2, 3]);
          ctx.lineWidth = 1 * devicePixelRatio;
          ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        }
        ctx.restore();
      },
    },
  };
}

function renderCharts(data) {
  for (const p of S.plots) p.destroy();
  S.plots = [];
  const box = $("#charts");
  box.innerHTML = "";
  const keys = Object.keys(data.series);
  if (!data.t.length || !keys.length) {
    box.innerHTML = `<div class="empty">Keine Messdaten in diesem Zeitraum.${S.info.status.running ? "" : " Läuft der Logger?"}</div>`;
    return;
  }
  const width = Math.max(300, box.clientWidth - 18);
  const axisStroke = cssVar("--muted"), grid = cssVar("--line");
  let ci = 0;
  for (const g of S.groups) {
    if (!S.groupsOn.has(g.key)) continue;
    const gk = keys.filter((k) => S.metrics[k] && S.metrics[k].group === g.key);
    if (!gk.length) continue;
    const units = [...new Set(gk.map((k) => S.metrics[k].unit || ""))];
    const wrap = document.createElement("div");
    wrap.className = "chart";
    wrap.innerHTML = `<h3>${esc(g.label)}<span>${data.res ? "Auflösung: " + esc(data.res) : ""}</span></h3>`;
    box.appendChild(wrap);
    const series = [{ label: "Zeit", value: (u, v) => (v == null ? "–" : fmtTime(v, span() > 80000)) }];
    const scales = { x: { time: true, range: [S.from, S.to] } };
    for (const k of gk) {
      const m = S.metrics[k];
      const sc = "u_" + (m.unit || "n");
      scales[sc] = scales[sc] || { auto: true, range: (u, mn, mx) => [Math.min(0, mn), mx > 0 ? mx * 1.05 : 1] };
      series.push({
        label: m.label, scale: sc, stroke: COLORS[ci++ % COLORS.length], width: 1.5, spanGaps: false,
        value: (u, v) => fmtVal(v, m.unit),
      });
    }
    const axes = [{ stroke: axisStroke, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, values: timeTicks }];
    units.slice(0, 2).forEach((un, i) => axes.push({
      scale: "u_" + (un || "n"), side: i ? 1 : 3, stroke: axisStroke, size: 88,
      grid: { show: i === 0, stroke: grid, width: 1 }, ticks: { stroke: grid },
      values: (u, vals) => vals.map((v) => fmtVal(v, "") + (un && un.length <= 3 ? " " + un : "")),
    }));
    const plotData = [data.t, ...gk.map((k) => data.series[k])];
    const u = new uPlot({
      width, height: g.key === "fps" ? 200 : 170, series, scales, axes,
      cursor: { sync: { key: "ls" }, drag: { x: true, y: false, setScale: false } },
      legend: { live: true },
      plugins: [overlayPlugin()],
      hooks: {
        setSelect: [(u) => {
          if (u.select.width > 8) {
            const a = u.posToVal(u.select.left, "x"), b = u.posToVal(u.select.left + u.select.width, "x");
            u.setSelect({ left: 0, width: 0 }, false);
            setRange(a, b);
          }
        }],
      },
    }, plotData, wrap);
    let downX = null;
    u.over.addEventListener("mousedown", (e) => { downX = e.clientX; });
    u.over.addEventListener("click", (e) => {
      if (downX != null && Math.abs(e.clientX - downX) > 4) return;
      const ts = u.posToVal(u.cursor.left, "x");
      if (ts) showDetail(ts);
    });
    S.plots.push(u);
  }
  if (data.missing && data.missing.length && !S.plots.length) {
    box.innerHTML = '<div class="empty">Für die gewählten Gruppen gibt es in diesem Zeitraum keine Werte.</div>';
  }
}

window.addEventListener("resize", () => {
  clearTimeout(S.resizeT);
  S.resizeT = setTimeout(() => {
    const w = Math.max(300, $("#charts").clientWidth - 18);
    for (const p of S.plots) p.setSize({ width: w, height: p.height });
  }, 150);
});

// ------------------------------------------------------------------ detail panel
function procTable(rows, limit = 25) {
  if (!rows || !rows.length) return '<div class="muted small">Keine Prozessliste gespeichert.</div>';
  let h = '<table><tr><th>Prozess</th><th class="num">CPU %</th><th class="num">RAM MB</th><th class="num">Threads</th></tr>';
  for (const r of rows.slice(0, limit)) {
    const [pid, name, cpu, rss, thr, , game, cmd] = r;
    h += `<tr class="${game ? "game" : ""}" title="PID ${pid}\n${esc(cmd || "")}"><td>${esc(name)}</td><td class="num">${fmtVal(cpu)}</td><td class="num">${Math.round(rss)}</td><td class="num">${thr}</td></tr>`;
  }
  return h + "</table><div class='small muted'>CPU %: 100 = ein voller Kern. Blau = Spiel.</div>";
}

function sampleKv(sample) {
  if (!sample) return "";
  const pick = ["fps", "cpu_total", "cpu_max_core", "cpu_freq_avg", "cpu_temp", "gpu_busy", "gpu_clock", "gpu_temp",
    "vram_pct", "vram_used", "gtt_used", "mem_avail", "swap_used", "psi_cpu", "psi_mem", "psi_io", "game_cpu", "game_rss", "shader_cpu", "uptime_h", "game_min"];
  let h = '<div class="kv small">';
  for (const k of pick) {
    if (sample[k] == null) continue;
    const m = S.metrics[k] || { label: k, unit: "" };
    h += `<div>${esc(m.label)}</div><div>${fmtVal(sample[k], m.unit)}</div>`;
  }
  const st = sample._st || {};
  for (const [k, v] of Object.entries(st)) h += `<div>${esc(k)}</div><div>${esc(v)}</div>`;
  return h + "</div>";
}

function markButtons(ts) {
  return `<div class="toolbar" style="margin-top:8px">
    <button data-mk="lag" class="danger">Hier war Lag</button>
    <button data-mk="good">Hier lief es gut</button>
    <button data-mk="end">Lag vorbei</button></div>`;
}
function bindMarkButtons(el, ts) {
  $$("[data-mk]", el).forEach((b) => b.onclick = async () => {
    await post("/api/marker", { kind: b.dataset.mk, ts });
    loadTimeline();
  });
}

async function showDetail(ts) {
  S.selTs = ts;
  S.plots.forEach((p) => p.redraw(false));
  const el = $("#detail");
  el.innerHTML = '<div class="empty">Lade …</div>';
  const d = await api("/api/procs", { ts });
  el.innerHTML = `<h3 style="margin:0 0 6px">${fmtTime(ts)}</h3>
    ${d.ts ? `<div class="small muted">Prozessliste von ${fmtTime(d.ts, false)}</div>` : ""}
    ${procTable(d.procs)}
    <details style="margin-top:8px"><summary>Messwerte</summary>${sampleKv(d.sample)}</details>
    ${markButtons(ts)}`;
  bindMarkButtons(el, ts);
}

async function showMarker(id, zoom) {
  const m = await api("/api/marker", { id });
  if (zoom) {
    switchTab("timeline");
    setRange(m.ts - 600, m.ts + 300);
  }
  S.selTs = m.ts;
  const el = $("#detail");
  el.innerHTML = `<h3 style="margin:0 0 4px"><span class="pill ${m.kind}">${esc(KIND_LABEL[m.kind] || m.kind)}</span> ${fmtTime(m.ts)}</h3>
    <div class="small">Vordergrund: <b>${esc(m.fg_name || "?")}</b> ${m.fg_title ? "· " + esc(m.fg_title) : ""}</div>
    <div style="margin:6px 0"><textarea id="mnote" rows="2" placeholder="Notiz (z. B. Map, was gerade passiert ist)">${esc(m.note || "")}</textarea>
    <div class="toolbar"><button id="msave">Notiz speichern</button><button id="mdel" class="danger">Marker löschen</button></div></div>
    <div class="small muted">Prozesse beim Drücken des Shortcuts:</div>
    ${procTable(m.procs, 30)}`;
  $("#msave").onclick = async () => { await post("/api/marker/note", { id: m.id, note: $("#mnote").value }); loadTimeline(); };
  $("#mdel").onclick = async () => {
    if (!confirm("Diesen Marker wirklich löschen?")) return;
    await post("/api/marker/delete", { id: m.id });
    el.innerHTML = '<div class="empty">Marker gelöscht.</div>';
    loadTimeline();
  };
}

// ------------------------------------------------------------------ analysis
async function loadAnalysis(refresh = false) {
  clearTimeout(S.analysisPoll);
  const out = $("#analysisOut");
  const auto = $("#useAuto").checked ? "1" : "0";
  let r;
  try {
    r = await api("/api/analysis", { refresh: refresh ? "1" : "0", auto });
  } catch (e) {
    out.innerHTML = `<div class="empty">Fehler: ${esc(e.message)}</div>`;
    return;
  }
  if (r.error) out.innerHTML = `<div class="card alert">Analyse-Fehler: ${esc(r.error)}</div>`;
  if (r.running || (!r.result && !r.error)) {
    $("#analysisMeta").textContent = "Analyse läuft …";
    if (!r.result) out.innerHTML = '<div class="empty">Analyse läuft – das kann bei vielen Daten einige Sekunden dauern …</div>';
    S.analysisPoll = setTimeout(() => loadAnalysis(false), 1000);
    if (!r.result) return;
  }
  if (r.result) renderAnalysis(r.result);
}

function renderAnalysis(a) {
  $("#analysisMeta").textContent = `Stand ${fmtTime(a.generated)} · ${a.took}s Rechenzeit`;
  const out = $("#analysisOut");
  let h = `<div class="card"><h2>Zusammenfassung</h2>
    <p class="small muted">${a.n_incidents} Vorfälle: ${a.n_lag} per Shortcut markiert, ${a.n_auto} automatisch aus MangoHud-FPS erkannt · ${a.n_good} „läuft gut“-Marker</p>
    <ul class="summary">${a.summary.map((s) => `<li>${s.startsWith("⚠") ? `<div class="alert">${esc(s)}</div>` : esc(s)}</li>`).join("")}</ul></div>`;

  // Findings
  h += `<div class="card"><h2>Messwerte, die bei Lags auffallen</h2>`;
  if (a.findings.length) {
    h += `<div style="overflow-x:auto"><table><tr><th>Messwert</th><th class="num">normal</th><th class="num">bei Lag</th><th>bei Vorfällen</th><th>Bedeutung</th></tr>`;
    for (const f of a.findings) {
      h += `<tr><td>${f.direction === "up" ? "↑" : "↓"} ${esc(f.label)}</td>
        <td class="num">${fmtVal(f.base, f.unit)}</td><td class="num">${fmtVal(f.bad, f.unit)}</td>
        <td style="min-width:90px"><div class="small">${f.hits} von ${f.n}</div><div class="bar"><div style="width:${Math.round(f.consistency * 100)}%"></div></div></td>
        <td class="small muted">${esc(f.hint || "")}</td></tr>`;
    }
    h += `</table></div><p class="small muted">Vergleich: Werte im Lag-Zeitfenster gegen normales Spielen (vorher in derselben Sitzung, „läuft gut“-Marker oder Sitzungen ohne Lag).</p>`;
  } else h += '<div class="muted">Noch nichts Eindeutiges.</div>';
  h += '</div><div class="grid2">';

  // Processes
  h += `<div class="card"><h2>Prozesse bei Lags</h2>`;
  if (a.processes.length) {
    h += `<table><tr><th>Prozess</th><th class="num">CPU normal</th><th class="num">CPU bei Lag</th><th class="num">mehr CPU</th><th class="num">neu</th></tr>`;
    for (const p of a.processes) {
      h += `<tr title="${esc(p.hint || "")}"><td>${esc(p.name)}</td><td class="num">${fmtVal(p.cpu_base)}</td><td class="num">${fmtVal(p.cpu_bad)}</td><td class="num">${p.up}/${p.n}</td><td class="num">${p.new}/${p.n}</td></tr>`;
    }
    h += "</table>";
  } else h += '<div class="muted">Keine Prozesse, die bei Lags regelmäßig auffallen.</div>';
  if (a.session_procs.length) {
    h += `<h2 style="margin-top:12px">Läuft in Sitzungen mit Lag, aber selten ohne</h2><table><tr><th>Prozess</th><th class="num">mit Lag</th><th class="num">ohne Lag</th></tr>`;
    for (const p of a.session_procs) h += `<tr title="${esc(p.hint || "")}"><td>${esc(p.name)}</td><td class="num">${Math.round(p.bad * 100)} %</td><td class="num">${Math.round(p.clean * 100)} %</td></tr>`;
    h += "</table>";
  }
  h += "</div>";

  // Kernel + context
  h += `<div class="card"><h2>Kernel-Meldungen rund um Lags</h2>`;
  if (a.kernel.length) {
    h += `<table><tr><th>Meldung</th><th class="num">Vorfälle</th></tr>`;
    for (const k of a.kernel) h += `<tr title="${esc((k.examples || []).join("\n"))}"><td class="mono">${esc(k.text)}${k.hint ? `<div class="small muted">${esc(k.hint)}</div>` : ""}</td><td class="num">${k.n}</td></tr>`;
    h += "</table>";
  } else h += '<div class="muted">Keine Kernel-Meldungen in der Nähe von Lags.</div>';
  h += "</div>";

  const c = a.context || {};
  h += `<div class="card"><h2>Zeitlicher Zusammenhang</h2><div class="kv">`;
  if (c.game_min_median != null) h += `<div>Minuten nach Spielstart</div><div>Median ${Math.round(c.game_min_median)} · alle: ${(c.game_min_list || []).join(", ")}</div>`;
  if (c.uptime_median_h != null) h += `<div>PC-Laufzeit bei Lag</div><div>Median ${c.uptime_median_h} h${c.clean_uptime_median_h != null ? ` (Sitzungen ohne Lag: ${c.clean_uptime_median_h} h)` : ""}</div>`;
  h += `<div>Spielsitzungen</div><div>${c.sessions_total ?? 0} gesamt · ${c.sessions_with_lag ?? 0} mit Lag · ${c.sessions_clean ?? 0} ohne</div>`;
  if (c.fg && c.fg.length) h += `<div>Vordergrund beim Marker</div><div>${c.fg.map(([n, k]) => `${esc(n)} (${k}×)`).join(", ")}</div>`;
  for (const [k, vals] of Object.entries(c.states || {})) h += `<div>${esc(k)}</div><div>${vals.map(([v, n]) => `${esc(v)} (${n}×)`).join(", ")}</div>`;
  if (c.proc_starts && c.proc_starts.length) h += `<div>Kurz vorher gestartet</div><div>${c.proc_starts.map(([n, k]) => `${esc(n)} (${k}×)`).join(", ")}</div>`;
  h += "</div></div>";

  if (a.fps_corr && a.fps_corr.length) {
    h += `<div class="card"><h2>Was mit den FPS zusammenhängt (MangoHud)</h2><p class="small muted">Rangkorrelation über ${a.fps_corr_points} Messpunkte beim Spielen. Negativ = je höher der Wert, desto weniger FPS.</p><table><tr><th>Messwert</th><th class="num">r</th></tr>`;
    for (const f of a.fps_corr) h += `<tr title="${esc(f.hint || "")}"><td>${esc(f.label)}</td><td class="num">${f.r.toFixed(2)}</td></tr>`;
    h += "</table></div>";
  }
  h += "</div>";

  // Incidents
  h += `<div class="card"><h2>Alle Vorfälle</h2>`;
  if (a.incidents.length) {
    h += `<table><tr><th>Zeit</th><th>Art</th><th>Vordergrund</th><th class="num">min im Spiel</th><th>Auffällig</th><th>Prozesse</th></tr>`;
    for (const i of a.incidents) {
      const mets = (i.metrics || []).slice(0, 3).map((m) => `${m.z > 0 ? "↑" : "↓"}${esc(m.label)}`).join(", ");
      const prs = (i.procs || []).slice(0, 3).map((p) => esc(p.name) + (p.new ? "*" : "")).join(", ");
      h += `<tr class="click" data-from="${i.start - 300}" data-to="${i.end + 300}" data-ts="${i.ts}">
        <td>${fmtTime(i.ts)}</td><td><span class="pill ${i.kind}">${KIND_LABEL[i.kind]}</span></td>
        <td>${esc(i.fg || "")}</td><td class="num">${i.game_min != null ? Math.round(i.game_min) : "–"}</td>
        <td class="small">${mets || "–"}</td><td class="small">${prs || "–"}</td></tr>`;
    }
    h += `</table><p class="small muted">Klick auf eine Zeile öffnet den Verlauf um diesen Zeitpunkt. * = Prozess lief vorher nicht.</p>`;
  } else h += '<div class="muted">Noch keine Vorfälle. Shortcut einrichten (Tab „Einrichtung“) und beim nächsten Lag drücken.</div>';
  h += "</div>";

  out.innerHTML = h;
  $$("tr.click", out).forEach((tr) => tr.onclick = () => {
    switchTab("timeline");
    setRange(+tr.dataset.from, +tr.dataset.to);
    S.selTs = +tr.dataset.ts;
  });
}

// ------------------------------------------------------------------ events
const EV_LABEL = { kernel: "Kernel", proc_start: "Prozess gestartet", proc_exit: "Prozess beendet", focus: "Vordergrund",
  state: "Einstellung", game: "Spiel", boot: "Systemstart", logger: "Logger" };

async function loadEvents() {
  const d = await api("/api/events", { from: S.from, to: S.to, limit: 500, kinds: [...S.eventKinds].join(",") });
  const kinds = $("#eventKinds");
  kinds.innerHTML = "";
  for (const k of Object.keys(EV_LABEL)) {
    const b = document.createElement("button");
    b.textContent = `${EV_LABEL[k]}${d.counts[k] ? ` (${d.counts[k]})` : ""}`;
    b.classList.toggle("on", S.eventKinds.has(k));
    b.onclick = () => { S.eventKinds.has(k) ? S.eventKinds.delete(k) : S.eventKinds.add(k); loadEvents(); };
    kinds.appendChild(b);
  }
  const out = $("#eventsOut");
  if (!d.events.length) { out.innerHTML = '<div class="empty">Keine Ereignisse im gewählten Zeitraum.</div>'; return; }
  let h = d.total > d.events.length ? `<p class="small muted">Zeige die letzten ${d.events.length} von ${d.total}. Zeitraum im Verlauf verkleinern, um mehr zu sehen.</p>` : "";
  h += '<div class="card"><table><tr><th>Zeit</th><th>Art</th><th>Text</th></tr>';
  for (const e of d.events.slice().reverse()) {
    h += `<tr class="click" data-ts="${e.ts}"><td class="num">${fmtTime(e.ts)}</td><td>${esc(EV_LABEL[e.kind] || e.kind)}</td><td class="${e.kind === "kernel" ? "ev-kernel mono" : ""}">${esc(e.text)}</td></tr>`;
  }
  out.innerHTML = h + "</table></div>";
  $$("tr.click", out).forEach((tr) => tr.onclick = () => { switchTab("timeline"); setRange(+tr.dataset.ts - 300, +tr.dataset.ts + 300); });
}

// ------------------------------------------------------------------ setup
function copyRow(cmd) {
  return `<div class="copy"><code>${esc(cmd)}</code><button data-copy="${esc(cmd)}">Kopieren</button></div>`;
}

async function renderSetup() {
  const st = await api("/api/status");
  S.info.status = st;
  const su = st.setup, c = st.commands;
  const mh = st.mangohud_dir;
  const launch = `MANGOHUD=1 MANGOHUD_CONFIG="autostart_log=1,output_folder=${mh}" %command%`;
  let h = `<div class="grid2">
  <div class="card"><h2>1. Autostart</h2>
    <p>Damit der Logger nach jedem Neustart automatisch mitläuft (im Hintergrund, mit niedriger Priorität).</p>
    <p>Status: <b>${su.autostart ? (su.autostart_here ? "aktiv" : "aktiv, aber für eine andere Kopie von Lagscope") : "aus"}</b></p>
    <div class="toolbar"><button class="primary" data-setup="autostart_on">${su.autostart ? "Autostart neu setzen (auf diese Kopie)" : "Autostart aktivieren"}</button>
    ${su.autostart ? '<button data-setup="autostart_off">Autostart entfernen</button>' : ""}</div>
    <p class="small muted">Wenn du den Lagscope-Ordner verschiebst, hier einmal neu setzen.</p>
  </div>

  <div class="card"><h2>2. Tastenkürzel zum Markieren</h2>
    <p>Lege ein globales Tastenkürzel an, das du drückst, sobald es ruckelt. Es speichert den Zeitpunkt, das Fenster im Vordergrund und alle laufenden Prozesse.</p>
    <div class="toolbar"><button class="primary" data-setup="launchers_on">${su.launchers ? "Menüeinträge neu anlegen" : "Menüeinträge anlegen"}</button>
    ${su.launchers ? '<button data-setup="launchers_off">Menüeinträge entfernen</button>' : ""}</div>
    <p class="small"><b>KDE Plasma:</b> Systemeinstellungen → Tastatur → Kurzbefehle → „Neu“ → „Anwendung“ → <i>Lagscope: Lag markieren</i> wählen → Taste festlegen (z. B. <code>Strg+Alt+L</code>). Optional genauso für <i>Läuft gut</i> und <i>Lag vorbei</i>.</p>
    <p class="small"><b>GNOME:</b> Einstellungen → Tastatur → Tastenkombinationen → Eigene Tastenkombinationen → „+“ → Befehl einfügen:</p>
    <div class="small">Lag:</div>${copyRow(c.mark)}
    <div class="small">Läuft gut:</div>${copyRow(c.good)}
    <div class="small">Lag vorbei:</div>${copyRow(c.end)}
    <p class="small muted">Nach dem Drücken erscheint kurz eine Benachrichtigung. Testen: einmal drücken und im Verlauf nachsehen.</p>
  </div>

  <div class="card"><h2>3. FPS mitschreiben (MangoHud, empfohlen)</h2>
    <p>Lagscope kann FPS nicht selbst messen. Mit MangoHud werden FPS-Einbrüche zusätzlich <b>automatisch</b> erkannt – auch die, bei denen niemand den Shortcut drückt.</p>
    <p class="small">MangoHud ist ${su.tools.mangohud ? "<b>installiert</b>" : "<b>nicht gefunden</b> – über die Paketverwaltung installieren (Paket „mangohud“)"}.
    In Steam: Rechtsklick auf Overwatch → Eigenschaften → Startoptionen:</p>
    ${copyRow(launch)}
    <p class="small muted">Lagscope liest neue Logs aus <code>${esc(mh)}</code> jede Minute ein. MangoHud schreibt die Datei je nach Version erst, wenn das Spiel beendet wird. Bisher importiert: ${st.mangohud_files} Datei(en), ${st.n_fps_seconds} Sekunden FPS-Daten.</p>
  </div>

  <div class="card"><h2>Logger</h2><div class="kv small">
    <div>Status</div><div>${st.running ? "läuft" : "läuft nicht"}</div>
    <div>Letzte Messung</div><div>${st.last_sample ? fmtTime(st.last_sample) : "–"} <span class="muted">(wird minutenweise gespeichert)</span></div>
    <div>Eigenverbrauch</div><div>${st.self_cpu != null ? st.self_cpu.toFixed(2) + " % CPU (eines Kerns)" : "–"}, ${st.self_rss ?? "–"} MB RAM</div>
    <div>Messintervall</div><div>${st.interval} s</div>
    <div>Spiel-Erkennung</div><div>${esc(st.game_patterns.join(", "))}</div>
    <div>Daten</div><div class="mono">${esc(st.data_dir)}</div>
    <div>Datenbank</div><div>${st.db_size_mb} MB · Aufbewahrung ${st.retention_days} Tage</div>
    <div>Erkannt</div><div>${esc(st.capabilities || "–")}</div>
    <div>Desktop</div><div>${esc(su.desktop || "?")} / ${esc(su.session || "?")}</div>
    <div>Hilfsprogramme</div><div>${Object.entries(su.tools).map(([k, v]) => `${v ? "✓" : "✗"} ${k}`).join(" · ")}</div>
  </div>
  ${!su.tools.xprop && !su.tools.xdotool && !su.tools.kdotool ? '<p class="alert small">Ohne <code>xprop</code> (Paket meist „xorg-xprop“ bzw. „x11-utils“) kann das Fenster im Vordergrund nicht erkannt werden. Unter KDE-Wayland hilft zusätzlich „kdotool“.</p>' : ""}
  ${!su.tools["notify-send"] ? '<p class="small muted">Tipp: Mit <code>notify-send</code> (Paket „libnotify“) erscheint beim Markieren eine Bestätigung.</p>' : ""}
  <p class="small muted">Einstellungen (z. B. Messintervall, Spielname) stehen in <code>config.json</code> im Datenordner. Nach Änderungen den Logger neu starten.</p>
  <div class="toolbar"><button class="danger" id="stopLogger">Logger beenden</button></div>
  </div></div>`;
  $("#setupOut").innerHTML = h;
  $$("[data-setup]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    const r = await post("/api/setup", { action: b.dataset.setup });
    if (!r.ok) alert(r.error || "Fehler");
    renderSetup();
  });
  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Kopiert ✓"; } catch (e) { b.textContent = "Bitte manuell kopieren"; }
    setTimeout(() => { b.textContent = "Kopieren"; }, 1500);
  });
  $("#stopLogger").onclick = async () => {
    if (!confirm("Logger wirklich beenden? Bis zum nächsten Start (Doppelklick oder Autostart) wird nichts aufgezeichnet.")) return;
    await post("/api/stop", {});
    setTimeout(refreshStatus, 1000);
  };
}

// ------------------------------------------------------------------ tabs / init
function switchTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  if (name === "analysis") loadAnalysis(false);
  if (name === "events") loadEvents();
  if (name === "setup") renderSetup();
  if (name === "timeline") {
    // Diagramme brauchen die richtige Breite, wenn der Tab vorher versteckt war
    const w = Math.max(300, $("#charts").clientWidth - 18);
    for (const p of S.plots) p.setSize({ width: w, height: p.height });
  }
}

async function init() {
  $$("#tabs button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
  $("#runAnalysis").onclick = () => loadAnalysis(true);
  $("#useAuto").onchange = () => loadAnalysis(false);
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (b && !b.onclick) navigator.clipboard?.writeText(b.dataset.copy);
  });
  const info = await api("/api/info");
  S.info = info;
  S.groups = info.catalogue.groups;
  for (const m of info.catalogue.metrics) S.metrics[m.key] = m;
  initGroups();
  initToolbar();
  refreshStatus();
  setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
  const ov = await loadOverview();

  // Startbereich: aus der URL, sonst die letzte Spielsitzung (wenn < 24 h her), sonst die letzte Stunde
  const hp = new URLSearchParams(location.hash.slice(1));
  const now = Date.now() / 1000;
  if (hp.get("from")) {
    setRange(+hp.get("from"), +hp.get("to"), hp.get("live") === "1");
  } else {
    const last = ov.sessions[ov.sessions.length - 1];
    if (last && (last.end || now) > now - 86400) setRange(last.start - 300, Math.min(now, (last.end || now) + 300), !last.end);
    else if (info.status.last_sample) setRange(info.status.last_sample - 3600, info.status.last_sample + 60, true);
    else setRange(now - 3600, now, true);
  }
  if (!info.status.n_markers && !info.status.setup.launchers) {
    $("#detail").innerHTML = `<div class="card" style="margin:0"><h2>Willkommen!</h2>
      <p>Lagscope zeichnet jetzt im Hintergrund auf. Als Nächstes im Tab <b>Einrichtung</b>:</p>
      <ol><li>Autostart aktivieren</li><li>Tastenkürzel zum Markieren anlegen</li><li>optional MangoHud für FPS</li></ol>
      <button class="primary" id="goSetup">Zur Einrichtung</button></div>`;
    $("#goSetup").onclick = () => switchTab("setup");
  }
}

init().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="alert">Fehler beim Start: ${esc(e.message)} – läuft der Logger?</div>`);
});
