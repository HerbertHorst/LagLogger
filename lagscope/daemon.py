"""The background logger."""
import os
import queue
import threading
import time

from . import collector, config, db, mangohud, osutil

# Names that never count as interesting process start/stop events
QUIET_NAMES = {"xprop", "xdotool", "kdotool", "nvidia-smi", "journalctl", "dmesg", "sh", "bash", "zsh", "fish",
               "sleep", "cat", "grep", "sed", "awk", "ps", "notify-send", "xdg-open", "python3",
               "nvidia-smi.exe", "wevtutil.exe", "conhost.exe", "WmiPrvSE.exe", "backgroundTaskHost.exe",
               "RuntimeBroker.exe", "dllhost.exe", "SearchProtocolHost.exe", "SearchFilterHost.exe", "smartscreen.exe"}

CURRENT = None             # laufender Daemon (für Statusinfos in der Weboberfläche)
STOP = threading.Event()   # gesetzt von "Logger beenden" in der Weboberfläche oder von `lagscope stop`
_LOCK = osutil.InstanceLock(config.LOCK_FILE)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def is_running():
    return osutil.is_locked(config.LOCK_FILE)


def same_boot(a, b):
    """Windows liefert keine boot_id, sondern den (gerundeten) Startzeitpunkt – mit Toleranz vergleichen."""
    if a == b:
        return True
    if a and b and a.startswith("t:") and b.startswith("t:"):
        try:
            return abs(int(a[2:]) - int(b[2:])) <= 120
        except ValueError:
            return False
    return False


class MinuteRollup:
    def __init__(self):
        self.minute = None
        self.acc = {}

    def add(self, ts, sample):
        minute = int(ts // 60) * 60
        flushed = None
        if self.minute is not None and minute != self.minute and self.acc:
            flushed = (self.minute, {k: [round(v[0] / v[1], 2), v[2], v[3]] for k, v in self.acc.items()})
            self.acc = {}
        self.minute = minute
        for k, v in sample.items():
            if not isinstance(v, (int, float)):
                continue
            a = self.acc.get(k)
            if a is None:
                self.acc[k] = [v, 1, v, v]
            else:
                a[0] += v
                a[1] += 1
                if v < a[2]:
                    a[2] = v
                if v > a[3]:
                    a[3] = v
        return flushed


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.con = db.connect()
        self.sampler = collector.SystemSampler(cfg)
        self.klog = collector.KernelLog() if cfg.get("kernel_log") else None
        self.state = {}
        self.session_id = None
        self.session_start = None
        self.rollup = MinuteRollup()
        self.names_last = {}    # name -> last seen ts
        self.names_first = {}   # name -> first seen ts (current appearance)
        self.pending_start = {}
        self.n = 0
        self.n_snap = 0
        self.buf_s, self.buf_p = [], []
        self.buf_minute = None
        self.last_procs = []
        self.ui_marks = queue.Queue()   # Marker aus dem Tray-Menü
        self.tray = None

    def flush(self, keep=False):
        """Write the current minute. keep=True: write a partial minute (it is overwritten later)."""
        db.write_chunk(self.con, "samples", self.buf_s)
        db.write_chunk(self.con, "procs", self.buf_p)
        if not keep:
            self.buf_s, self.buf_p = [], []

    # ----------------------------------------------------------- startup
    def startup(self):
        con = self.con
        # Offene Sitzungen eines früheren Laufs schließen
        for sid, start in con.execute("SELECT id, start FROM sessions WHERE end IS NULL").fetchall():
            last = con.execute("SELECT MAX(ts) FROM samples WHERE ts >= ?", (start - 60,)).fetchone()[0]
            con.execute("UPDATE sessions SET end=? WHERE id=?", (max(start, (last or start) + 60), sid))
        bid = collector.boot_id()
        if not same_boot(db.get_meta(con, "boot_id"), bid):
            up = collector.uptime_s()
            db.add_event(con, "boot", f"Systemstart (Uptime beim Loggerstart: {up / 60:.0f} min)", time.time() - up)
            db.set_meta(con, "boot_id", bid)
        db.add_event(con, "logger", f"Logger gestartet (PID {os.getpid()})")
        self.update_capabilities()
        con.commit()
        log("Gestartet. " + db.get_meta(con, "capabilities", ""))

    def start_tray(self):
        """Symbol im Infobereich der Taskleiste (Windows: Win32, Linux: StatusNotifierItem über D-Bus)."""
        if not self.cfg.get("tray", True):
            return
        from . import server
        actions = dict(on_open=lambda: osutil.open_url(server.current_url()), on_mark=self.ui_marks.put,
                       on_quit=STOP.set)
        try:
            if osutil.IS_WIN:
                from . import tray_win
                icon = os.path.join(config.DATA_DIR, "icon.ico")
                with open(icon, "wb") as f:
                    f.write(server.static("icon.ico"))
                self.tray = tray_win.Tray(icon, **actions)
            else:
                from . import tray_linux
                self.tray = tray_linux.Tray(server.static("icon.png"), **actions)
            log("Tray-Symbol aktiv")
        except ImportError:
            log("Tray-Symbol nicht möglich: PyGObject (python3-gobject / python3-gi) fehlt")
        except Exception as e:
            log(f"Tray-Symbol nicht möglich: {e}")

    def update_tray(self):
        if not self.tray:
            return
        n = self.con.execute("SELECT COUNT(*) FROM markers").fetchone()[0]
        tip = "Lagscope – zeichnet auf"
        if self.session_id is not None:
            tip += f" · Spiel läuft ({(time.time() - self.session_start) / 60:.0f} min)"
        self.tray.set_tip(f"{tip} · {n} Marker")

    def update_capabilities(self):
        info = self.sampler.capabilities()
        info.append(("Ereignisprotokoll: " if osutil.IS_WIN else "Kernel-Log: ") +
                    ("aus" if not self.klog else "kein Zugriff" if self.klog.ok is False else "an"))
        db.set_meta(self.con, "capabilities", " | ".join(info))

    # ----------------------------------------------------------- per sample
    def handle_states(self, ts, state):
        for k, v in state.items():
            old = self.state.get(k)
            if old != v:
                if old is not None or k != "foreground":
                    if k == "foreground":
                        db.add_event(self.con, "focus", f"Vordergrund: {v or '?'}", ts)
                    elif old is not None:
                        db.add_event(self.con, "state", f"{k}: {old} → {v}", ts)
                    else:
                        db.add_event(self.con, "state", f"{k} = {v}", ts)
                self.state[k] = v

    def handle_session(self, ts, sample, game_procs):
        running = bool(game_procs)
        if running and self.session_id is None:
            name = sorted({p.name for p in game_procs})[0]
            cur = self.con.execute("INSERT INTO sessions(start, name, boot_id) VALUES (?,?,?)",
                                   (ts, name, collector.boot_id()))
            self.session_id, self.session_start = cur.lastrowid, ts
            db.add_event(self.con, "game", f"Spiel gestartet: {name}", ts)
            log(f"Spielsitzung gestartet: {name}")
        elif not running and self.session_id is not None:
            self.con.execute("UPDATE sessions SET end=? WHERE id=?", (ts, self.session_id))
            mins = (ts - self.session_start) / 60
            db.add_event(self.con, "game", f"Spiel beendet nach {mins:.0f} min", ts)
            log("Spielsitzung beendet")
            self.session_id = self.session_start = None
        if self.session_id is not None:
            sample["game_min"] = round((ts - self.session_start) / 60, 2)

    def handle_processes(self, ts, procs):
        names = {p.name for p in procs if p.cmd}  # leere cmdline = Kernel-Thread
        warm = self.n > 3
        for name in names:
            if name not in self.names_first:
                last = self.names_last.get(name)
                self.names_first[name] = ts
                if warm and (last is None or ts - last > 600) and name not in QUIET_NAMES:
                    self.pending_start[name] = ts
            elif name in self.pending_start and ts - self.pending_start[name] >= 1:
                db.add_event(self.con, "proc_start", name, self.pending_start.pop(name))
            self.names_last[name] = ts
        for name in list(self.names_first):
            if name not in names:
                first = self.names_first.pop(name)
                self.pending_start.pop(name, None)
                if ts - first > 30 and name not in QUIET_NAMES:
                    db.add_event(self.con, "proc_exit", f"{name} (lief {(ts - first) / 60:.0f} min)", ts)
        if len(self.names_last) > 3000:
            cut = ts - 3600
            self.names_last = {k: v for k, v in self.names_last.items() if v > cut}

    def tick(self):
        ts = time.time()
        sample, state, procs, ready, game = self.sampler.sample()
        self.n += 1
        if not ready:
            return
        self.handle_states(ts, state)
        self.handle_session(ts, sample, game)
        self.handle_processes(ts, procs)
        sample["_st"] = state
        minute = int(ts // 60)
        if self.buf_minute is not None and minute != self.buf_minute:
            self.flush()
        elif len(self.buf_s) and len(self.buf_s) % 8 == 0:
            self.flush(keep=True)  # Zwischenstand, damit die Weboberfläche aktuell bleibt
        self.buf_minute = minute
        self.buf_s.append((ts, sample))
        if self.n % max(1, int(self.cfg["proc_snapshot_every"])) == 0:
            self.n_snap += 1
            full = self.n_snap % 10 == 1
            self.buf_p.append((ts, collector.snapshot_list(procs, full_names=full)))
        flushed = self.rollup.add(ts, sample)
        if flushed:
            self.con.execute("INSERT OR REPLACE INTO minutes(ts, data) VALUES (?,?)", (flushed[0], db.pack(flushed[1])))
        if self.klog:
            for kts, line in self.klog.drain():
                db.add_event(self.con, "kernel", line, kts)
        rows = self.sampler.fps_rows()
        if rows:
            self.con.executemany("INSERT OR REPLACE INTO fps(ts, fps, ft_max, ft_avg) VALUES (?,?,?,?)", rows)
        for hts, kind in self.sampler.hotkey_events():
            self.hotkey_marker(hts, kind, procs)
        while not self.ui_marks.empty():
            self.hotkey_marker(time.time(), self.ui_marks.get_nowait(), procs)
        self.last_procs = procs

    def hotkey_marker(self, ts, kind, procs):
        """Windows: Tastenkürzel direkt im Logger – Vordergrund und Prozessliste sind sofort da."""
        from . import marker
        fg = self.sampler.fg.get() or {}
        snap = collector.snapshot_list(procs, top=40)
        snap["foreground"] = fg
        marker.insert(self.con, ts, kind, None, fg, snap)
        self.con.commit()
        osutil.notify(marker.TITLES.get(kind, "Markiert"), kind=kind)
        log(f"Marker per Tastenkürzel: {kind} (Vordergrund: {fg.get('name') or '?'})")

    def run(self):
        self.startup()
        self.start_tray()
        interval = float(self.cfg["interval"])
        next_t = time.monotonic()
        last_import = last_cleanup = last_caps = 0.0
        while not STOP.is_set():
            try:
                self.tick()
            except Exception as e:  # ein Fehler darf den Logger nicht beenden
                log(f"Fehler beim Messen: {e!r}")
            now = time.monotonic()
            # jede Messung sofort festschreiben, damit Weboberfläche/Marker nie auf eine Sperre warten
            self.con.commit()
            if now - last_import >= 60:
                last_import = now
                try:
                    skipped = set()
                    mangohud.import_dir(self.con, self.cfg.get("mangohud_dir"), log,
                                        self.cfg["game_patterns"], skipped)
                    db.set_meta(self.con, "mangohud_skipped", ", ".join(sorted(skipped)[:10]))
                except Exception as e:
                    log(f"MangoHud-Import: {e!r}")
            if now - last_caps >= 60:
                last_caps = now
                self.update_capabilities()
                self.update_tray()
            if now - last_cleanup >= 3600:
                last_cleanup = now
                try:
                    db.cleanup(self.con, float(self.cfg["retention_days"]))
                except Exception as e:
                    log(f"Aufräumen: {e!r}")
            next_t += interval
            delay = next_t - time.monotonic()
            if delay < 0:  # z.B. nach Standby
                next_t = time.monotonic()
                delay = interval
            STOP.wait(delay)


def run(web=True):
    if not _LOCK.acquire():
        print("lagscope läuft bereits.")
        return 1
    try:
        with open(config.PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    cfg = config.load()
    osutil.lower_priority()
    if web:
        from . import server
        server.start_background(cfg)
    import signal
    try:
        signal.signal(signal.SIGTERM, lambda *a: STOP.set())
    except (ValueError, OSError):
        pass
    global CURRENT
    d = Daemon(cfg)
    CURRENT = d
    try:
        d.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            d.flush()
            if d.session_id is not None:
                d.con.execute("UPDATE sessions SET end=? WHERE id=?", (time.time(), d.session_id))
            db.add_event(d.con, "logger", "Logger beendet")
            d.con.commit()
            d.con.close()
        except Exception:
            pass
        if hasattr(d.sampler, "pm"):
            d.sampler.pm.stop()
        if d.tray:
            d.tray.close()
        log("Beendet.")
        _LOCK.release()
    return 0


def start_detached():
    """Start the logger as a detached background process (no terminal/console window)."""
    try:
        if os.path.getsize(config.LOG_FILE) > 2_000_000:  # Log-Datei klein halten
            os.replace(config.LOG_FILE, config.LOG_FILE + ".old")
    except OSError:
        pass
    osutil.spawn_detached(config.launch_args("run"), config.LOG_FILE)


def stop():
    """Ask the running logger to stop (via its web API, so it can save), otherwise terminate it."""
    if not is_running():
        return False
    from . import server
    try:
        import urllib.request
        req = urllib.request.Request(server.current_url() + "api/stop", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
        for _ in range(40):
            if not is_running():
                return True
            time.sleep(0.25)
    except Exception:
        pass
    try:
        with open(config.PID_FILE) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and osutil.pid_alive(pid):
        osutil.terminate(pid)
        return True
    return False
