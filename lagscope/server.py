"""Local web UI + JSON API. Everything heavy happens here, the browser only gets small, pre-aggregated data."""
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import analysis, config, db, metrics, osutil, setup

MAX_POINTS = 1500          # nie mehr Punkte pro Linie an den Browser
RAW_MAX_STEP = 15          # darüber werden die Minuten-Zusammenfassungen benutzt (statt Rohdaten zu entpacken)
PEAK_KEYS = {"cpu_max_core", "ft_max", "procs_blocked", "cpu_throttle_s", "compact_stall_s", "vram_pct",
             "vram_used", "shader_cpu", "psi_mem_full", "gpu_temp_hot"}
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/uPlot.min.js": ("uPlot.iife.min.js", "application/javascript; charset=utf-8"),
    "/uPlot.min.css": ("uPlot.min.css", "text/css; charset=utf-8"),
    "/icon.png": ("icon.png", "image/png"),
    "/favicon.ico": ("icon.ico", "image/x-icon"),
}
_static_cache = {}

_analysis = {"result": None, "key": None, "running": False, "error": None}
_analysis_lock = threading.Lock()


def static(name):
    """Web files: from the source folder, the zipapp or the Windows EXE (PyInstaller)."""
    if name not in _static_cache:
        import pkgutil
        import sys
        data = None
        base = getattr(sys, "_MEIPASS", None)
        if base and os.path.exists(os.path.join(base, "lagscope", "web", name)):
            with open(os.path.join(base, "lagscope", "web", name), "rb") as f:
                data = f.read()
        if data is None:
            data = pkgutil.get_data("lagscope", "web/" + name)
        _static_cache[name] = data
    return _static_cache[name]


def port_file():
    return os.path.join(config.DATA_DIR, "port")


def current_url():
    try:
        with open(port_file()) as f:
            return f.read().strip()
    except OSError:
        cfg = config.load()
        return f"http://{cfg['web_host']}:{cfg['web_port']}/"


# ------------------------------------------------------------------ data access

def series(con, cfg, t0, t1, keys, points):
    points = max(50, min(MAX_POINTS, points))
    span = max(1.0, t1 - t0)
    interval = float(cfg["interval"])
    step = max(interval, span / points)
    want = set(keys)
    out_t, cols = [], {k: [] for k in keys}
    fps_keys = [k for k in keys if k in ("fps", "ft_max")]
    other = [k for k in keys if k not in fps_keys]

    if other:
        if step < RAW_MAX_STEP:
            rows = db.load_samples(con, t0, t1)
            res = "roh" if step <= interval * 1.01 else f"{step:.0f}s"
            agg_from_rows(rows, t0, step, other, out_t, cols, lambda d, k: d.get(k))
        else:
            rows = db.load_minutes(con, t0, t1)
            step = max(60.0, math.ceil(step / 60) * 60)
            res = f"{step / 60:.0f} min"
            agg_from_rows(rows, t0, step, other, out_t, cols,
                          lambda d, k: (d.get(k) or [None, None, None])[2 if k in PEAK_KEYS else 0])
    else:
        res = None
        n = int(span // step) + 1
        out_t.extend(t0 + i * step + step / 2 for i in range(n))
    if fps_keys:
        istep = max(1, int(step))
        rows = con.execute(
            "SELECT (ts - ?) / ? AS b, AVG(fps), MAX(ft_max), MIN(fps) FROM fps WHERE ts BETWEEN ? AND ? GROUP BY b",
            (int(t0), istep, int(t0), int(t1))).fetchall()
        by_b = {int(r[0]): r for r in rows}
        for k in fps_keys:
            col = []
            for t in out_t:
                r = by_b.get(int((t - int(t0)) // istep))
                v = None if r is None else (r[1] if k == "fps" else r[2])
                col.append(None if v is None else round(v, 1))
            cols[k] = col
    present = [k for k in keys if any(v is not None for v in cols.get(k, []))]
    return dict(t=[round(t, 1) for t in out_t], series={k: cols[k] for k in present}, res=res, step=step,
                missing=sorted(want - set(present)))


def agg_from_rows(rows, t0, step, keys, out_t, cols, getter):
    if not rows:
        return
    if step <= 0:
        step = 1
    buckets = {}
    order = []
    for ts, d in rows:
        b = int((ts - t0) // step)
        acc = buckets.get(b)
        if acc is None:
            acc = buckets[b] = {}
            order.append(b)
        for k in keys:
            v = getter(d, k)
            if isinstance(v, (int, float)):
                a = acc.get(k)
                if a is None:
                    acc[k] = [v, 1, v]
                else:
                    a[0] += v
                    a[1] += 1
                    if v > a[2]:
                        a[2] = v
    prev_b = None
    for b in order:
        # Lücke (Logger aus / PC aus) -> null, damit die Linie nicht durchgezogen wird
        if prev_b is not None and b - prev_b > max(2, 60 / step * 2):
            out_t.append(t0 + (prev_b + 1) * step)
            for k in keys:
                cols[k].append(None)
        prev_b = b
        out_t.append(t0 + b * step + step / 2)
        acc = buckets[b]
        for k in keys:
            a = acc.get(k)
            if a is None:
                cols[k].append(None)
            elif k in PEAK_KEYS:
                cols[k].append(round(a[2], 2))  # Spitzen nicht wegmitteln
            else:
                cols[k].append(round(a[0] / a[1], 2))


def status(con, cfg):
    from . import daemon
    last_ts, last_d = db.last_chunk_item(con, "samples")
    last_d = last_d or {}
    first = con.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    try:
        size = os.path.getsize(config.DB_FILE) + sum(
            os.path.getsize(config.DB_FILE + s) for s in ("-wal", "-shm") if os.path.exists(config.DB_FILE + s))
    except OSError:
        size = 0
    # Eigenverbrauch der letzten 10 Minuten
    rows = db.load_samples(con, time.time() - 600, time.time())
    self_cpu = [d.get("self_cpu") for _, d in rows if d.get("self_cpu") is not None]
    mh_dir = cfg.get("mangohud_dir")
    return dict(
        running=daemon.is_running(), last_sample=last_ts, first_sample=first,
        game_running=bool(last_d.get("game_running")), foreground=(last_d.get("_st") or {}).get("foreground"),
        self_cpu=round(sum(self_cpu) / len(self_cpu), 2) if self_cpu else None, self_rss=last_d.get("self_rss"),
        db_size_mb=round(size / 1048576, 1), data_dir=config.DATA_DIR,
        capabilities=db.get_meta(con, "capabilities", ""),
        n_markers=con.execute("SELECT COUNT(*) FROM markers").fetchone()[0],
        n_fps_seconds=con.execute("SELECT COUNT(*) FROM fps").fetchone()[0],
        mangohud_dir=mh_dir, mangohud_files=con.execute("SELECT COUNT(*) FROM imported").fetchone()[0],
        interval=cfg["interval"], retention_days=cfg["retention_days"], game_patterns=cfg["game_patterns"],
        commands={k: config.launch_command(*v) for k, v in (
            ("mark", ("mark",)), ("good", ("mark", "--good")), ("end", ("mark", "--end")),
            ("start", ("start",)), ("open", ("open",)), ("stop", ("stop",)))},
        setup=setup.status(),
        platform="windows" if osutil.IS_WIN else "linux", os_name=osutil.os_name(),
        win=(daemon.CURRENT.sampler.status_info() if daemon.CURRENT is not None else {}),
        hotkeys=cfg.get("hotkeys") if osutil.IS_WIN else None,
        state=last_d.get("_st") or {},
    )


def run_analysis(cfg, include_auto):
    with _analysis_lock:
        if _analysis["running"]:
            return
        _analysis["running"] = True
    try:
        con = db.connect_read()
        res = analysis.analyze(con, cfg, include_auto=include_auto)
        con.close()
        _analysis.update(result=res, error=None, key=include_auto)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _analysis.update(error=repr(e))
    finally:
        _analysis["running"] = False


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "lagscope"

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, separators=(",", ":"), allow_nan=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        # Schutz gegen DNS-Rebinding: nur localhost-Hostnamen akzeptieren
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1", "[::1]", "")

    def do_GET(self):
        if not self._host_ok():
            return self.send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in STATIC:
            name, ctype = STATIC[u.path]
            return self.send(200, static(name), ctype, cache=name.startswith(("uPlot", "icon")))
        try:
            con = db.connect_read()
            try:
                return self.route_get(u.path, q, con)
            finally:
                con.close()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None  # Browser hat die Anfrage abgebrochen (z. B. beim schnellen Zoomen)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self.send(500, {"error": repr(e)})

    def route_get(self, path, q, con):
        cfg = self.cfg
        now = time.time()
        t0 = float(q.get("from", now - 3600))
        t1 = float(q.get("to", now))
        if path == "/api/info":
            return self.send(200, dict(catalogue=metrics.catalogue(), status=status(con, cfg)))
        if path == "/api/status":
            return self.send(200, status(con, cfg))
        if path == "/api/series":
            keys = [k for k in q.get("keys", "").split(",") if k]
            return self.send(200, series(con, cfg, t0, t1, keys, int(q.get("points", 1000))))
        if path == "/api/timeline":
            sess = db.sessions(con, t0, t1)
            return self.send(200, dict(sessions=sess, markers=db.markers(con, t0, t1)))
        if path == "/api/overview":
            days = con.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') d, COUNT(*), MIN(ts), MAX(ts) FROM minutes GROUP BY d ORDER BY d DESC LIMIT 60"
            ).fetchall()
            mk = con.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') d, kind, COUNT(*) FROM markers GROUP BY d, kind").fetchall()
            mkd = {}
            for d, k, n in mk:
                mkd.setdefault(d, {})[k] = n
            sess = db.sessions(con, now - 60 * 86400, now)
            return self.send(200, dict(days=[dict(day=d, minutes=n, start=a, end=b, markers=mkd.get(d, {}))
                                             for d, n, a, b in days], sessions=sess[-200:]))
        if path == "/api/events":
            kinds = [k for k in q.get("kinds", "").split(",") if k]
            evs = db.events(con, t0, t1, limit=20000)
            if kinds:
                evs = [e for e in evs if e["kind"] in kinds]
            counts = {}
            for e in evs:
                counts[e["kind"]] = counts.get(e["kind"], 0) + 1
            limit = int(q.get("limit", 500))
            return self.send(200, dict(events=evs[-limit:], total=len(evs), counts=counts))
        if path == "/api/procs":
            ts = float(q.get("ts", now))
            ps = db.load_procs(con, ts - 60, ts + 60)
            ss = db.load_samples(con, ts - 30, ts + 30)
            p = min(ps, key=lambda x: abs(x[0] - ts)) if ps else None
            s_ = min(ss, key=lambda x: abs(x[0] - ts)) if ss else None
            return self.send(200, dict(ts=p[0] if p else None, procs=p[1].get("rows") if p else [],
                                       sample_ts=s_[0] if s_ else None, sample=s_[1] if s_ else None))
        if path == "/api/marker":
            mid = int(q.get("id", 0))
            ms = [m for m in db.markers(con, with_snapshot=True) if m["id"] == mid]
            if not ms:
                return self.send(404, {"error": "not found"})
            m = ms[0]
            snap = m.pop("snapshot") or {}
            m["procs"] = snap.get("rows", [])
            return self.send(200, m)
        if path == "/api/analysis":
            include_auto = q.get("auto", "1") == "1"
            if q.get("refresh") == "1" or _analysis["result"] is None or _analysis["key"] != include_auto:
                threading.Thread(target=run_analysis, args=(cfg, include_auto), daemon=True).start()
                time.sleep(0.05)
            return self.send(200, dict(running=_analysis["running"], error=_analysis["error"],
                                       result=_analysis["result"] if _analysis["key"] == include_auto else None))
        return self.send(404, {"error": "unknown"})

    def do_POST(self):
        if not self._host_ok():
            return self.send(403, {"error": "forbidden"})
        # Nur Anfragen von der eigenen Seite (Schutz gegen fremde Webseiten)
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return self.send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        try:
            if path == "/api/marker":
                kind = body.get("kind", "lag")
                if kind not in ("lag", "good", "end"):
                    return self.send(400, {"error": "kind"})
                con = db.connect()
                ts = float(body.get("ts") or time.time())
                cur = con.execute("INSERT INTO markers(ts, kind, note, fg_name) VALUES (?,?,?,?)",
                                  (ts, kind, (body.get("note") or "")[:500] or None, "(nachträglich im Browser)"))
                con.commit()
                con.close()
                return self.send(200, {"id": cur.lastrowid})
            if path == "/api/marker/delete":
                con = db.connect()
                con.execute("DELETE FROM markers WHERE id=?", (int(body.get("id")),))
                con.commit()
                con.close()
                return self.send(200, {"ok": True})
            if path == "/api/marker/note":
                con = db.connect()
                con.execute("UPDATE markers SET note=? WHERE id=?", ((body.get("note") or "")[:500], int(body.get("id"))))
                con.commit()
                con.close()
                return self.send(200, {"ok": True})
            if path == "/api/config":
                pats = [str(x).strip().lower() for x in body.get("game_patterns", []) if str(x).strip()][:10]
                if not pats:
                    return self.send(400, {"error": "mindestens ein Spielname"})
                self.cfg["game_patterns"][:] = pats   # gilt sofort (der Logger nutzt dieselbe Liste)
                config.save({"game_patterns": pats})
                return self.send(200, {"ok": True, "game_patterns": pats})
            if path == "/api/setup":
                return self.send(200, setup.apply(body.get("action", "")))
            if path == "/api/stop":
                from . import daemon
                if daemon.CURRENT is not None:
                    daemon.STOP.set()          # Logger speichert und beendet sich
                else:                          # nur Weboberfläche (`lagscope serve`)
                    threading.Timer(0.3, lambda: os._exit(0)).start()
                return self.send(200, {"ok": True})
        except Exception as e:
            return self.send(500, {"error": repr(e)})
        return self.send(404, {"error": "unknown"})


def make_server(cfg):
    Handler.cfg = cfg
    host = cfg["web_host"]
    base = int(cfg["web_port"])
    last_err = None
    for port in range(base, base + 15):
        try:
            srv = ThreadingHTTPServer((host, port), Handler)
            srv.daemon_threads = True
            url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
            with open(port_file(), "w") as f:
                f.write(url)
            return srv, url
        except OSError as e:
            last_err = e
    raise last_err


def start_background(cfg):
    srv, url = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    print(f"Weboberfläche: {url}", flush=True)
    return url


def serve_forever(cfg):
    srv, url = make_server(cfg)
    print(f"Weboberfläche: {url}  (Strg+C zum Beenden)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
