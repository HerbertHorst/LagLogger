"""The background logger."""
import fcntl
import os
import shutil
import subprocess
import sys
import time

from . import collector, config, db, mangohud

# Names that never count as interesting process start/stop events
QUIET_NAMES = {"xprop", "xdotool", "kdotool", "nvidia-smi", "journalctl", "dmesg", "sh", "bash", "zsh", "fish",
               "sleep", "cat", "grep", "sed", "awk", "ps", "notify-send", "xdg-open", "python3"}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def acquire_lock():
    """Single instance lock. Returns the open file (keep it!) or None if another logger runs."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    f = open(config.LOCK_FILE, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def is_running():
    try:
        with open(config.LOCK_FILE, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                return False
            except OSError:
                return True
    except OSError:
        return False


def be_nice():
    """Run with low CPU and IO priority so the logger never competes with the game."""
    try:
        os.nice(10)
    except OSError:
        pass
    if shutil.which("ionice"):
        subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], capture_output=True)


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
        if db.get_meta(con, "boot_id") != bid:
            up = collector.uptime_s()
            db.add_event(con, "boot", f"Systemstart (Uptime beim Loggerstart: {up / 60:.0f} min)", time.time() - up)
            db.set_meta(con, "boot_id", bid)
        db.add_event(con, "logger", f"Logger gestartet (PID {os.getpid()})")
        gpus = []
        if self.sampler.amd.kind:
            gpus.append({"amd": "AMD", "intel": "Intel"}[self.sampler.amd.kind])
        if self.sampler.nv.available():
            gpus.append("NVIDIA")
        fg = self.sampler.fg
        tools = [n for n, v in (("xprop", fg.xprop), ("xdotool", fg.xdotool), ("kdotool", fg.kdotool)) if v]
        info = ["GPU: " + (", ".join(gpus) or "keine Werte"),
                "Vordergrund-Erkennung: " + (", ".join(tools) or "kein Tool (xprop/xdotool/kdotool fehlt)"),
                "Kernel-Log: " + ("an" if self.klog else "aus")]
        db.set_meta(con, "capabilities", " | ".join(info))
        con.commit()
        log("Gestartet. " + " | ".join(info))

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

    def run(self):
        self.startup()
        interval = float(self.cfg["interval"])
        next_t = time.monotonic()
        last_commit = last_import = last_cleanup = 0.0
        while True:
            try:
                self.tick()
            except Exception as e:  # ein Fehler darf den Logger nicht beenden
                log(f"Fehler beim Messen: {e!r}")
            now = time.monotonic()
            if now - last_commit >= 10:
                self.con.commit()
                last_commit = now
            if now - last_import >= 60:
                last_import = now
                try:
                    mangohud.import_dir(self.con, self.cfg.get("mangohud_dir"), log)
                except Exception as e:
                    log(f"MangoHud-Import: {e!r}")
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
            time.sleep(delay)


def run(web=True):
    lock = acquire_lock()
    if lock is None:
        print("lagscope läuft bereits.")
        return 1
    cfg = config.load()
    be_nice()
    if web:
        from . import server
        server.start_background(cfg)
    import signal
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    d = Daemon(cfg)
    try:
        d.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            d.flush()
            if d.session_id is not None:
                d.con.execute("UPDATE sessions SET end=? WHERE id=?", (time.time(), d.session_id))
            db.add_event(d.con, "logger", "Logger beendet")
            d.con.commit()
        except Exception:
            pass
        log("Beendet.")
    return 0


def start_detached():
    """Start the logger as a detached background process (no terminal needed)."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    logf = open(config.LOG_FILE, "a")
    # Log-Datei klein halten
    try:
        if os.path.getsize(config.LOG_FILE) > 2_000_000:
            logf.close()
            os.replace(config.LOG_FILE, config.LOG_FILE + ".old")
            logf = open(config.LOG_FILE, "a")
    except OSError:
        pass
    target = [sys.executable, config.APP_ROOT] if config.APP_IS_FILE else \
        [sys.executable, os.path.join(config.APP_ROOT, "lagscope.py")]
    subprocess.Popen(target + ["run"], stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def stop():
    try:
        with open(config.LOCK_FILE) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and is_running():
        os.kill(pid, 15)
        return True
    return False

