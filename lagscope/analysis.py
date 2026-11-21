"""Finds what lag moments have in common.

Every "incident" (a lag marker set by hotkey, or an FPS drop found in MangoHud data) gets a *bad window*
and is compared with a *baseline* of normal gaming: preferably the same game session before the first
lag, otherwise good-markers and sessions without any lag. Then the per-incident differences are combined:
only effects that show up in most incidents are reported.
"""
import math
import re
import statistics
import time
from collections import Counter, defaultdict

from . import db, metrics

Z_HIT = 1.5          # ab dieser robusten Abweichung zählt ein Messwert als "auffällig" bei einem Vorfall
MIN_BASE = 20        # mindestens so viele Messpunkte als Vergleich


def _median(v):
    return statistics.median(v) if v else None


def _pct(v, q):
    """q-quantile (0..1) of a list."""
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


# "Normale" FPS einer Sitzung = 80. Perzentil, nicht Median: wenn der Lag bis zum Neustart anhält,
# besteht oft mehr als die halbe Sitzung aus Lag – der Median wäre dann schon der Lag-Wert.
FPS_REF_Q = 0.8


def _mad(v, med):
    return statistics.median([abs(x - med) for x in v]) if v else 0.0


def norm_name(name):
    # Kernel-Threads wie "kworker/u16:3-events" zusammenfassen
    if "/" in name:
        return re.sub(r"\d+", "#", name.split("-")[0])
    return name


def norm_kernel(text):
    t = re.sub(r"^\[[^\]]*\]\s*", "", text)
    t = re.sub(r"0x[0-9a-fA-F]+", "#", t)
    t = re.sub(r"\b[0-9a-fA-F]{6,}\b", "#", t)
    t = re.sub(r"\d+", "#", t)
    return t[:160]


class Loader:
    """Loads and caches samples / process snapshots per time window."""

    def __init__(self, con):
        self.con = con
        self.cache = {}

    def samples(self, t0, t1):
        key = ("s", int(t0), int(t1))
        if key not in self.cache:
            self.cache[key] = db.load_samples(self.con, t0, t1)
        return self.cache[key]

    def procs(self, t0, t1):
        key = ("p", int(t0), int(t1))
        if key not in self.cache:
            self.cache[key] = db.load_procs(self.con, t0, t1)
        return self.cache[key]


def abs_threshold(cfg):
    """Fixed 'FPS below = lag' limit from the settings, or 0 (= relative to the session's normal FPS)."""
    try:
        return max(0.0, float(cfg.get("lag_fps_below") or 0))
    except (TypeError, ValueError):
        return 0.0


def ignored_drops(con):
    try:
        return [r[0] for r in con.execute("SELECT ts FROM ignored_drops")]
    except Exception:
        return []


def detect_fps_drops(con, cfg, sessions, with_ignored=False):
    """Automatic incidents from FPS data (PresentMon/MangoHud): FPS clearly too low for a while.

    Limit: the fixed 'lag_fps_below' if set, otherwise a share of the session's normal FPS."""
    out = []
    ratio = float(cfg.get("auto_fps_ratio", 0.6))
    min_s = int(cfg.get("auto_min_seconds", 10))
    fixed = abs_threshold(cfg)
    ign = ignored_drops(con)
    for s in sessions:
        end = s["end"] or time.time()
        rows = db.load_fps(con, s["start"] + 60, end)
        if len(rows) < 120:
            continue
        fps = [r[1] for r in rows]
        med = _pct(fps, FPS_REF_Q)  # Bezug "normale FPS" (siehe FPS_REF_Q)
        if not med or med < 10:
            continue
        thr = fixed or med * ratio
        by_sec = {r[0]: r[1] for r in rows}

        def finish(a, b):
            if b - a < min_s:
                return
            vals = [by_sec[x] for x in range(int(a), int(b) + 1) if x in by_sec]
            d = dict(start=a, end=b, session=s, median_fps=med, threshold=round(thr, 1),
                     fps_min=round(min(vals), 1) if vals else None,
                     fps_avg=round(sum(vals) / len(vals), 1) if vals else None,
                     ignored=any(a - 30 <= x <= b + 30 for x in ign))
            if with_ignored or not d["ignored"]:
                out.append(d)

        win, run_start, last_bad = [], None, None
        for i, (ts, f, _, _) in enumerate(rows):
            win.append(f)
            if len(win) > 5:
                win.pop(0)
            avg = sum(win) / len(win)
            if avg < thr:
                if run_start is None:
                    run_start = ts
                last_bad = ts
            elif run_start is not None and ts - last_bad > 3:
                finish(run_start, last_bad)
                run_start = None
        if run_start is not None:
            finish(run_start, last_bad)
    return out


FPS_OK = 0.85       # FPS ≥ 85 % des Sitzungs-Medians = "normal"
FPS_NORMAL = 0.9    # für die Vergleichsbasis etwas strenger


class SessionFps:
    """Per-second FPS of one game session (from MangoHud or PresentMon)."""

    def __init__(self, rows, start, fixed=0.0):
        self.fps = {r[0]: r[1] for r in rows}
        vals = [r[1] for r in rows if r[0] >= start + 60] or [r[1] for r in rows]
        self.ref = _pct(vals, FPS_REF_Q) or 0
        # Grenze "normal": feste Grenze aus den Einstellungen oder 85 % der normalen FPS der Sitzung
        self.fixed = fixed
        self.ok_thr = fixed or FPS_OK * self.ref
        self.normal_thr = fixed or FPS_NORMAL * self.ref

    def at(self, ts):
        """FPS around a timestamp (average of the two seconds before it), or None."""
        v = [self.fps[s] for s in (int(ts), int(ts) - 1, int(ts) - 2) if s in self.fps]
        return sum(v) / len(v) if v else None

    def rolling(self, sec, width=5):
        v = [self.fps[x] for x in range(sec - width + 1, sec + 1) if x in self.fps]
        return sum(v) / len(v) if v else None

    def ok(self, sec):
        r = self.rolling(sec)
        return None if r is None else r >= self.ok_thr

    def lag_span(self, t, lo, hi):
        """Around a lag marker at t: when did the FPS drop start and when were they normal again?

        Returns (start, end) or None if the FPS were not low around t."""
        near = [self.fps[x] for x in range(int(t) - 20, int(t) + 21) if x in self.fps]
        if len(near) < 10 or sum(near) / len(near) >= self.ok_thr:
            return None
        start, run = None, 0
        for sec in range(int(t), int(max(lo, t - 600)), -1):   # rückwärts: Beginn des Einbruchs (max. 10 min)
            st = self.ok(sec)
            run = run + 1 if st else 0
            if run >= 15:
                start = sec + 15
                break
        end, run = None, 0
        for sec in range(int(t), int(min(hi, t + 1800))):      # vorwärts: wieder normal (max. 30 min)
            st = self.ok(sec)
            if st is None and sec - int(t) > 120 and not any(x in self.fps for x in range(sec, sec + 60)):
                end = sec  # keine FPS-Daten mehr (Spiel beendet)
                break
            run = run + 1 if st else 0
            if run >= 30:
                end = sec - 30
                break
        return (start if start is not None else max(lo, t - 600)), (end if end is not None else min(hi, t + 1800))


def session_fps(con, s, cache, cfg=None):
    if s is None:
        return None
    if s["id"] not in cache:
        rows = db.load_fps(con, s["start"], s["end"] or time.time())
        fixed = abs_threshold(cfg or {})
        cache[s["id"]] = SessionFps(rows, s["start"], fixed) if len(rows) >= 120 else None
    return cache[s["id"]]


def _session_for(ts, sessions):
    for s in sessions:
        if s["start"] - 30 <= ts <= (s["end"] or time.time()) + 30:
            return s
    return None


def build_incidents(con, cfg, sessions, include_auto=True, fps_cache=None):
    before, after = float(cfg["lag_before"]), float(cfg["lag_after"])
    fps_cache = {} if fps_cache is None else fps_cache
    ms = db.markers(con)
    stops = sorted(m["ts"] for m in ms if m["kind"] in ("end", "good"))
    incidents = []
    for m in ms:
        if m["kind"] != "lag":
            continue
        t = m["ts"]
        s = _session_for(t, sessions)
        end = t + after
        nxt = [x for x in stops if x > t]
        if nxt:
            end = min(end, nxt[0])
        if s and s["end"]:
            end = min(end, s["end"])
        start, by_fps = t - before, False
        sf = session_fps(con, s, fps_cache, cfg)
        if sf:
            hi = min([x for x in (s["end"], nxt[0] if nxt else None) if x] or [time.time()])
            span = sf.lag_span(t, s["start"] + 60, hi)
            if span:
                # Zeitfenster aus den FPS: vom Beginn des Einbruchs bis die FPS wieder normal sind
                start, end, by_fps = min(span[0] - 30, t - 30), max(span[1], t + 5), True
        incidents.append(dict(kind="lag", ts=t, start=start, end=max(end, t + 5), session=s, marker=m,
                              by_fps=by_fps))
    if include_auto:
        for d in detect_fps_drops(con, cfg, sessions):
            if any(i["start"] - 60 <= d["start"] <= i["end"] + 60 for i in incidents if i["kind"] == "lag"):
                continue  # schon durch einen Marker abgedeckt
            incidents.append(dict(kind="auto", ts=d["start"], start=d["start"] - 30, end=d["end"],
                                  session=d["session"], marker=None, median_fps=d["median_fps"], by_fps=True))
    for i in incidents:
        # Ausgewertet wird der Beginn des Einbruchs (dort sieht man den Auslöser); bei langen Lags würde das
        # ganze Fenster kurze Auslöser verwässern. "end" bleibt das Ende des Lags (FPS wieder normal).
        i["a_end"] = min(i["end"], max(i["ts"], i["start"] + 60) + 120)
    incidents.sort(key=lambda i: i["ts"])
    return incidents


def baseline_windows(inc, incidents, sessions, good_markers):
    """Time windows considered 'normal' for this incident."""
    wins = []
    s = inc["session"]
    if s:
        first_bad = min(i["start"] for i in incidents if i["session"] is s)
        a, b = s["start"] + 90, first_bad - 60
        if b - a >= 60:
            wins.append((a, b))
    for g in good_markers:
        wins.append((g["ts"] - 60, g["ts"] + 30))
    return wins


def fps_baseline(loader, inc, incidents, sessions, fps_cache, con, want=400):
    """'Normal' = moments while gaming in which the FPS were really in the usual range.

    Same session first (before *and* after the lag – the FPS tell us when it was fine), then other
    sessions with FPS data. Returns (samples, snapshots, description) or None without FPS data."""
    def good_times(s, sf):
        windows = [(i["start"] - 60, i["end"] + 60) for i in incidents if i["session"] is s]
        return lambda ts: (ts >= s["start"] + 90 and not any(a <= ts <= b for a, b in windows)
                           and (sf.at(ts) or 0) >= sf.normal_thr)
    order = [inc["session"]] + [x for x in reversed(sessions) if x is not inc["session"]]
    base, snaps, used = [], [], 0
    for s in order:
        sf = session_fps(con, s, fps_cache)
        if not sf or not sf.ref:
            continue
        good = good_times(s, sf)
        end = s["end"] or time.time()
        smp = [x for x in loader.samples(s["start"], end) if x[1].get("game_running", 1) and good(x[0])]
        if not smp:
            continue
        step = max(1, len(smp) // want) if s is not inc["session"] else 1
        base.extend(smp[::step])
        sn = [x for x in loader.procs(s["start"], end) if good(x[0])]
        snaps.extend(sn[::max(1, len(sn) // 150)] if s is not inc["session"] else sn)
        used += 1
        if len(base) >= max(MIN_BASE * 5, want) or used >= 10:
            break
    if len(base) < MIN_BASE:
        return None
    same = inc["session"] is not None and used == 1 and session_fps(con, inc["session"], fps_cache) is not None
    return base, snaps, ("Momente mit normalen FPS in derselben Sitzung" if same else "Momente mit normalen FPS")


def clean_sessions(incidents, sessions):
    bad_ids = {i["session"]["id"] for i in incidents if i["session"]}
    return [s for s in sessions if s["id"] not in bad_ids and s["end"] and s["end"] - s["start"] > 300]


def _values(samples, key):
    return [d[key] for _, d in samples if isinstance(d.get(key), (int, float))]


def _proc_stats(snaps):
    """Per process name: (average CPU %, fraction of snapshots in which it was running).

    Presence uses only the 'full' snapshots (all process names); the others list only busy processes."""
    n = len(snaps)
    tot, pres = defaultdict(float), defaultdict(int)
    for _, sn in snaps:
        for name, cpu in (sn.get("names") or {}).items():
            tot[norm_name(name)] += cpu
    src = [sn for _, sn in snaps if sn.get("full", 1)] or [sn for _, sn in snaps]
    for sn in src:
        for nn in {norm_name(x) for x in (sn.get("names") or {})}:
            pres[nn] += 1
    den = len(src) or 1
    return {k: (tot[k] / max(1, n), pres[k] / den) for k in set(tot) | set(pres)}, n


def _spearman(x, y):
    n = len(x)
    if n < 30:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def fps_correlations(con, loader, sessions, limit_points=20000):
    """Which metrics move together with FPS during gaming (needs MangoHud data)."""
    pairs = defaultdict(lambda: ([], []))
    total = 0
    for s in sessions[-30:]:
        end = s["end"] or time.time()
        fps = {r[0]: r[1] for r in db.load_fps(con, s["start"], end)}
        if len(fps) < 120:
            continue
        for ts, d in loader.samples(s["start"] + 60, end):
            f = fps.get(int(ts)) or fps.get(int(ts) - 1)
            if f is None:
                continue
            total += 1
            for k, v in d.items():
                if isinstance(v, (int, float)) and k in metrics.M and metrics.M[k]["group"] != "fps":
                    pairs[k][0].append(v)
                    pairs[k][1].append(f)
    out = []
    for k, (xs, ys) in pairs.items():
        if len(xs) > limit_points:
            step = len(xs) / limit_points
            idx = [int(i * step) for i in range(limit_points)]
            xs, ys = [xs[i] for i in idx], [ys[i] for i in idx]
        r = _spearman(xs, ys)
        if r is not None and abs(r) >= 0.25:
            info = metrics.M[k]
            out.append(dict(key=k, label=info["label"], unit=info["unit"], r=round(r, 2),
                            hint=info["hint_down"] if r > 0 else info["hint_up"]))
    out.sort(key=lambda x: -abs(x["r"]))
    return out[:15], total


def analyze(con, cfg, include_auto=True):
    t_start = time.time()
    loader = Loader(con)
    sessions = db.sessions(con)
    fps_cache = {}
    incidents = build_incidents(con, cfg, sessions, include_auto, fps_cache)
    all_markers = db.markers(con)
    good = [m for m in all_markers if m["kind"] == "good"]
    clean = clean_sessions(incidents, sessions)

    # Globale Vergleichsbasis: Sitzungen ohne Lag (gleichmäßig verteilt, max ~4000 Messpunkte)
    global_base = []
    for s in clean[-10:]:
        smp = loader.samples(s["start"] + 90, s["end"])
        step = max(1, len(smp) // 400)
        global_base.extend(smp[::step])
    global_snaps = []
    for s in clean[-10:]:
        sn = loader.procs(s["start"] + 90, s["end"])
        step = max(1, len(sn) // 150)
        global_snaps.extend(sn[::step])

    per_metric = defaultdict(list)
    per_proc = defaultdict(lambda: dict(up=0, new=0, n=0, bad=[], base=[]))
    kernel_hits = defaultdict(lambda: dict(n=0, examples=[]))
    start_hits = Counter()
    fg_counter = Counter()
    state_hits = defaultdict(Counter)
    inc_out = []

    for inc in incidents:
        bad = loader.samples(inc["start"], inc["a_end"])
        bad = [x for x in bad if x[1].get("game_running", 1)] or bad
        fb = fps_baseline(loader, inc, incidents, sessions, fps_cache, con)
        if fb:
            base, base_snaps, base_kind = fb
            for g in good:  # "Läuft gut"-Marker zählen zusätzlich
                base = base + loader.samples(g["ts"] - 60, g["ts"] + 30)
                base_snaps = base_snaps + loader.procs(g["ts"] - 60, g["ts"] + 30)
        else:
            wins = baseline_windows(inc, incidents, sessions, good)
            base, base_snaps = [], []
            for a, b in wins:
                base.extend(loader.samples(a, b))
                base_snaps.extend(loader.procs(a, b))
            base_kind = "Sitzung vor dem Lag" if len(base) >= MIN_BASE else None
        if len(base) < MIN_BASE and len(global_base) >= MIN_BASE:
            base, base_snaps, base_kind = base + global_base, base_snaps + global_snaps, "Sitzungen ohne Lag"
        if len(base) < MIN_BASE:
            # Notlösung: Zeit 5–20 min vor dem Vorfall
            base = loader.samples(inc["start"] - 1200, inc["start"] - 300)
            base_snaps = loader.procs(inc["start"] - 1200, inc["start"] - 300)
            base_kind = "5–20 min vorher (wenig Vergleichsdaten)"

        rec = dict(kind=inc["kind"], ts=inc["ts"], start=inc["start"], end=inc["end"], baseline=base_kind,
                   by_fps=inc.get("by_fps", False), lag_seconds=round(inc["end"] - inc["start"]) if inc.get("by_fps") else None,
                   n_bad=len(bad), n_base=len(base),
                   session=inc["session"]["id"] if inc["session"] else None,
                   marker_id=inc["marker"]["id"] if inc["marker"] else None,
                   note=(inc["marker"] or {}).get("note"), median_fps=inc.get("median_fps"))
        if inc["marker"]:
            fgn = inc["marker"].get("fg_name") or "?"
            rec["fg"] = fgn
            rec["fg_title"] = inc["marker"].get("fg_title")
            fg_counter[fgn] += 1
        mid = [d for t, d in bad if abs(t - inc["ts"]) < 30] or [d for _, d in bad]
        if mid:
            rec["uptime_h"] = mid[0].get("uptime_h")
            rec["game_min"] = mid[0].get("game_min")
            for k, v in (mid[0].get("_st") or {}).items():
                state_hits[k][v] += 1

        # --- Messwerte vergleichen
        tops = []
        if len(bad) >= 3 and len(base) >= 5:
            keys = set()
            for _, d in bad[:5]:
                keys.update(k for k in d if k in metrics.M and metrics.floor(k) < 9999)
            for k in keys:
                b, a = _values(bad, k), _values(base, k)
                if len(b) < 3 or len(a) < 5:
                    continue
                mb, ma = _median(b), _median(a)
                spread = max(1.4826 * _mad(a, ma), metrics.floor(k))
                z = (mb - ma) / spread
                per_metric[k].append(dict(z=z, bad=mb, base=ma, peak=max(b)))
                if abs(z) >= Z_HIT:
                    tops.append(dict(key=k, label=metrics.M[k]["label"], unit=metrics.M[k]["unit"],
                                     z=round(z, 1), bad=round(mb, 1), base=round(ma, 1)))
        tops.sort(key=lambda x: -abs(x["z"]))
        rec["metrics"] = tops[:8]

        # --- Prozesse vergleichen
        bsn = loader.procs(inc["start"], inc["a_end"])
        procs_rec = []
        if bsn and base_snaps:
            pb, _ = _proc_stats(bsn)
            pa, _ = _proc_stats(base_snaps)
            for name, (cpu_b, pres_b) in pb.items():
                cpu_a, pres_a = pa.get(name, (0.0, 0.0))
                d = per_proc[name]
                d["n"] += 1
                d["bad"].append(cpu_b)
                d["base"].append(cpu_a)
                is_up = cpu_b - cpu_a >= 5 and cpu_b >= 1.5 * cpu_a
                is_new = pres_b >= 0.5 and pres_a <= 0.1
                if is_up:
                    d["up"] += 1
                if is_new:
                    d["new"] += 1
                if is_up or (is_new and cpu_b >= 0.5):
                    procs_rec.append(dict(name=name, cpu_bad=round(cpu_b, 1), cpu_base=round(cpu_a, 1),
                                          new=is_new))
        procs_rec.sort(key=lambda x: -(x["cpu_bad"] - x["cpu_base"]))
        rec["procs"] = procs_rec[:8]

        # --- Ereignisse rund um den Vorfall
        evs = db.events(con, inc["start"] - 300, inc["a_end"])
        kern, starts, states = [], [], []
        seen_k = set()
        for e in evs:
            if e["kind"] == "kernel":
                nk = norm_kernel(e["text"])
                kern.append(e)
                if nk not in seen_k:
                    seen_k.add(nk)
                    kh = kernel_hits[nk]
                    kh["n"] += 1
                    if len(kh["examples"]) < 3:
                        kh["examples"].append(e["text"][:300])
            elif e["kind"] == "proc_start" and e["ts"] <= inc["ts"] + 60:
                starts.append(e)
            elif e["kind"] in ("state", "focus", "game", "boot"):
                states.append(e)
        for nm in {norm_name(e["text"]) for e in starts}:
            start_hits[nm] += 1
        rec["kernel"] = kern[-10:]
        rec["proc_starts"] = starts[-10:]
        rec["states"] = states[-10:]
        inc_out.append(rec)

    n_inc = len(incidents)

    # --- Zusammenführen: Messwerte, die bei den meisten Vorfällen auffällig sind
    findings = []
    for k, lst in per_metric.items():
        n = len(lst)
        up = sum(1 for x in lst if x["z"] >= Z_HIT)
        down = sum(1 for x in lst if x["z"] <= -Z_HIT)
        direction = "up" if up >= down else "down"
        hits = max(up, down)
        cons = hits / n
        zmed = _median([x["z"] for x in lst])
        if hits == 0 or cons < 0.5 or abs(zmed) < 1.0:
            continue
        info = metrics.M[k]
        score = cons * min(abs(zmed), 8) * math.sqrt(hits)
        findings.append(dict(key=k, label=info["label"], unit=info["unit"], group=info["group"],
                             direction=direction, hits=hits, n=n, consistency=round(cons, 2),
                             z=round(zmed, 1), bad=round(_median([x["bad"] for x in lst]), 1),
                             base=round(_median([x["base"] for x in lst]), 1), score=round(score, 2),
                             hint=info["hint_up"] if direction == "up" else info["hint_down"]))
    findings.sort(key=lambda f: -f["score"])

    # --- Absolute Warnsignale (unabhängig vom Vergleich)
    alerts = []

    def peak_check(key, thr, text):
        lst = per_metric.get(key) or []
        hit = [x for x in lst if x["peak"] >= thr]
        if hit:
            alerts.append(dict(key=key, hits=len(hit), n=len(lst), text=text,
                               value=round(max(x["peak"] for x in hit), 1)))

    peak_check("vram_pct", 92, "Grafikspeicher war fast voll (≥92 %). Volles VRAM ist ein sehr typischer Grund "
                               "für FPS-Einbrüche nach einigen Runden – Texturqualität senken, andere GPU-Programme "
                               "schließen.")
    peak_check("cpu_temp", 90, "CPU-Temperatur ≥90 °C – die CPU drosselt sehr wahrscheinlich.")
    peak_check("gpu_temp_hot", 100, "GPU-Hotspot ≥100 °C – die Grafikkarte drosselt sehr wahrscheinlich.")
    peak_check("gpu_temp", 85, "GPU-Temperatur ≥85 °C.")
    peak_check("swap_used", 1024, "Über 1 GB Swap belegt – RAM reicht nicht.")
    peak_check("psi_mem_full", 5, "Starker Speicherdruck (PSI full ≥5 %) – Prozesse standen still, weil RAM fehlte.")
    peak_check("shader_cpu", 20, "Steam-Shader-Kompilierung (fossilize_replay) lief während des Lags.")
    peak_check("cpu_max_core", 98, "Mindestens ein CPU-Kern lief auf 100 %.")

    # --- Prozesse, die bei Vorfällen typischerweise mehr CPU brauchen / neu auftauchen
    procs = []
    for name, d in per_proc.items():
        score = d["up"] + d["new"] * 0.7
        if score <= 0 or (d["up"] == 0 and _median(d["bad"]) < 0.5):
            continue
        procs.append(dict(name=name, up=d["up"], new=d["new"], n=d["n"],
                          cpu_bad=round(_median(d["bad"]), 1), cpu_base=round(_median(d["base"]), 1),
                          score=round(score / max(1, n_inc), 2), hint=metrics.process_hint(name)))
    procs.sort(key=lambda p: (-p["score"], -p["cpu_bad"]))

    # --- Prozesse, die in Lag-Sitzungen laufen, aber in Sitzungen ohne Lag kaum
    sess_procs = []
    bad_sessions = [s for s in sessions if any(i["session"] is s for i in incidents)]
    if bad_sessions and clean:
        def present(s):
            snaps = loader.procs(s["start"], s["end"] or time.time())
            step = max(1, len(snaps) // 60)
            names = set()
            for _, sn in snaps[::step]:
                names.update(norm_name(n) for n, c in (sn.get("names") or {}).items() if c >= 0.1 or sn.get("full", 1))
            return names
        bs = [present(s) for s in bad_sessions[-15:]]
        cs = [present(s) for s in clean[-15:]]
        allnames = set().union(*bs) if bs else set()
        for name in allnames:
            fb = sum(name in x for x in bs) / len(bs)
            fc = sum(name in x for x in cs) / len(cs)
            if fb >= 0.75 and fc <= 0.35:
                sess_procs.append(dict(name=name, bad=round(fb, 2), clean=round(fc, 2),
                                       hint=metrics.process_hint(name)))
        sess_procs.sort(key=lambda x: -(x["bad"] - x["clean"]))

    # --- Kernel-Meldungen
    kernel = []
    for text, d in kernel_hits.items():
        total = None
        kernel.append(dict(text=text, n=d["n"], examples=d["examples"], hint=metrics.kernel_hint(text), total=total))
    kernel.sort(key=lambda k: -k["n"])

    # --- Zeitlicher Kontext
    ctx = {}
    ups = [r["uptime_h"] for r in inc_out if r.get("uptime_h") is not None]
    gm = [r["game_min"] for r in inc_out if r.get("game_min") is not None]
    if ups:
        ctx["uptime_median_h"] = round(_median(ups), 1)
    if gm:
        ctx["game_min_median"] = round(_median(gm), 1)
        ctx["game_min_list"] = sorted(round(x) for x in gm)
    clean_up = []
    for s in clean[-20:]:
        smp = loader.samples(s["start"] + 60, s["start"] + 70)
        if smp:
            clean_up.append(smp[0][1].get("uptime_h"))
    clean_up = [x for x in clean_up if x is not None]
    if clean_up:
        ctx["clean_uptime_median_h"] = round(_median(clean_up), 1)
    ctx["sessions_total"] = len(sessions)
    ctx["sessions_with_lag"] = len(bad_sessions)
    ctx["sessions_clean"] = len(clean)
    ctx["fg"] = fg_counter.most_common(8)
    ctx["states"] = {k: v.most_common(4) for k, v in state_hits.items() if k != "foreground"}
    ctx["proc_starts"] = start_hits.most_common(10)

    corr, n_corr = fps_correlations(con, loader, sessions)

    summary = summarize(n_inc, findings, alerts, procs, sess_procs, kernel, ctx, corr, inc_out)

    return dict(generated=time.time(), took=round(time.time() - t_start, 2),
                n_incidents=n_inc, n_lag=sum(1 for i in incidents if i["kind"] == "lag"),
                n_auto=sum(1 for i in incidents if i["kind"] == "auto"), n_good=len(good),
                findings=findings[:20], alerts=alerts, processes=procs[:20], session_procs=sess_procs[:15],
                kernel=kernel[:15], context=ctx, fps_corr=corr, fps_corr_points=n_corr,
                incidents=inc_out[::-1][:200], summary=summary)


def summarize(n_inc, findings, alerts, procs, sess_procs, kernel, ctx, corr, inc_out):
    out = []
    if n_inc == 0:
        out.append("Noch keine Lag-Marker vorhanden. Beim nächsten FPS-Einbruch den Shortcut drücken – "
                   "am besten auch ab und zu 'läuft gut' markieren.")
        return out
    if n_inc < 3:
        out.append(f"Erst {n_inc} Vorfall/Vorfälle – Ergebnisse sind noch unsicher. Ab ca. 5 Markern über mehrere "
                   "Tage werden Gemeinsamkeiten belastbar.")
    for a in alerts:
        if a["hits"] / max(1, a["n"]) >= 0.5:
            out.append(f"⚠ {a['text']} (bei {a['hits']} von {a['n']} Vorfällen, max. {a['value']})")
    for f in findings[:5]:
        arrow = "höher" if f["direction"] == "up" else "niedriger"
        out.append(f"{f['label']} ist bei {f['hits']} von {f['n']} Vorfällen deutlich {arrow} "
                   f"(typisch {f['bad']} statt {f['base']} {f['unit']}). " + (f["hint"] or ""))
    for p in procs[:3]:
        if p["score"] >= 0.4:
            what = []
            if p["up"]:
                what.append(f"braucht bei {p['up']} Vorfällen mehr CPU")
            if p["new"]:
                what.append(f"taucht bei {p['new']} Vorfällen neu auf")
            out.append(f"Prozess „{p['name']}“ " + " und ".join(what) +
                       f" ({p['cpu_base']} → {p['cpu_bad']} %). " + (p["hint"] or ""))
    for s in sess_procs[:3]:
        out.append(f"„{s['name']}“ lief in {round(s['bad'] * 100)} % der Sitzungen mit Lag, aber nur in "
                   f"{round(s['clean'] * 100)} % der Sitzungen ohne Lag. " + (s["hint"] or ""))
    for k in kernel[:2]:
        if k["n"] >= max(2, n_inc * 0.4):
            out.append(f"Kernel-Meldung bei {k['n']} Vorfällen: „{k['text'][:90]}“. " + (k["hint"] or ""))
    if ctx.get("game_min_median") is not None and n_inc >= 2:
        out.append(f"Lags treten im Mittel {ctx['game_min_median']:.0f} min nach Spielstart auf.")
    if ctx.get("uptime_median_h") is not None and ctx.get("clean_uptime_median_h") is not None:
        a, b = ctx["uptime_median_h"], ctx["clean_uptime_median_h"]
        if a > b * 1.5 + 0.5:
            out.append(f"Lags kommen eher, wenn der PC schon länger läuft (Uptime im Mittel {a} h vs. {b} h "
                       "bei Sitzungen ohne Lag) – passt zu 'Neustart hilft' (Speicher-Fragmentierung, Leck, "
                       "Hintergrunddienste).")
    if corr:
        c = corr[0]
        out.append(f"Stärkster Zusammenhang mit den FPS (MangoHud): {c['label']} (r = {c['r']}).")
    if len(out) <= (1 if n_inc < 3 else 0):
        out.append("Bisher keine eindeutige Gemeinsamkeit gefunden. Mehr Marker sammeln – und prüfen, ob die "
                   "Vergleichsbasis passt (mit 'läuft gut'-Markern verbessern).")
    return out
