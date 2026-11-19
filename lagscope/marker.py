"""`lagscope mark` – called by the keyboard shortcut. Must be quick and must never fail loudly."""
import shutil
import subprocess
import time

from . import collector, config, db

TITLES = {
    "lag": "Lag markiert",
    "good": "'Läuft gut' markiert",
    "end": "'Lag vorbei' markiert",
}


def notify(title, body=""):
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", "lagscope", "-t", "2500", "-i", "dialog-information", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def mark(kind="lag", note=None, ts=None, quiet=False):
    ts = ts or time.time()
    cfg = config.load()
    fg = collector.Foreground().get() or {}
    # Prozessliste mit CPU-Werten über eine halbe Sekunde
    tracker = collector.ProcessTracker(cfg["game_patterns"])
    tracker.scan()
    time.sleep(0.5)
    procs, _ = tracker.scan()
    snap = collector.snapshot_list(procs, top=40)
    snap["foreground"] = fg
    con = db.connect()
    cur = con.execute(
        "INSERT INTO markers(ts, kind, note, fg_pid, fg_name, fg_title, snapshot) VALUES (?,?,?,?,?,?,?)",
        (ts, kind, note, fg.get("pid"), fg.get("name"), fg.get("title"), db.pack(snap)))
    con.commit()
    mid = cur.lastrowid
    con.close()

    from . import daemon
    started = False
    if not daemon.is_running():
        daemon.start_detached()
        started = True
    if not quiet:
        body = f"Vordergrund: {fg.get('name') or '?'}"
        if started:
            body += "\nLogger lief nicht und wurde gestartet."
        notify(TITLES.get(kind, "Markiert"), body)
    return mid
