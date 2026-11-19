"""`lagscope selftest`: checks within a few seconds which values arrive on this PC and what is missing."""
import json
import os
import shutil
import socket
import sqlite3
import sys
import time

from . import __version__, collector, config, db, metrics, osutil

OK, WARN, BAD = ("OK", "!", "X") if osutil.IS_WIN else ("✓", "!", "✗")  # Windows-Konsolen: nur ASCII

# wichtig = ohne diese Werte ist die Analyse deutlich schwächer
IMPORTANT_LINUX = ["cpu_total", "cpu_max_core", "cpu_freq_avg", "cpu_temp", "mem_avail", "psi_mem", "gpu_busy",
                   "vram_used", "gpu_clock", "gpu_temp", "disk_read", "net_rx"]
IMPORTANT_WIN = ["cpu_total", "cpu_max_core", "cpu_freq_avg", "mem_avail", "commit_pct", "gpu_busy", "vram_used",
                 "vram_pct", "cpu_temp", "gpu_temp", "gpu_clock", "disk_read", "net_rx"]

WHY_MISSING = {
    "linux": {
        "cpu_temp": "kein CPU-Temperatursensor (Kernel-Modul k10temp/coretemp) gefunden",
        "cpu_freq_avg": "kein cpufreq im Kernel (in VMs normal)",
        "psi_mem": "Kernel ohne Pressure Stall Information (PSI) – nicht schlimm",
        "gpu_busy": "keine AMD/Intel-GPU unter /sys/class/drm und kein nvidia-smi (NVIDIA-Treiber) gefunden",
        "vram_used": "keine AMD-GPU unter /sys/class/drm und kein nvidia-smi gefunden",
        "gpu_clock": "keine GPU-Taktwerte (AMD: pp_dpm_sclk, NVIDIA: nvidia-smi)",
        "gpu_temp": "kein GPU-Temperatursensor gefunden",
    },
    "windows": {
        "cpu_temp": "LibreHardwareMonitor läuft nicht bzw. dessen Webserver ist aus (optional)",
        "gpu_temp": "nur mit NVIDIA-Treiber (nvidia-smi) oder LibreHardwareMonitor",
        "gpu_clock": "nur mit NVIDIA-Treiber (nvidia-smi) oder LibreHardwareMonitor",
        "vram_pct": "Grafikspeicher-Größe nicht aus der Registry lesbar",
        "gpu_busy": "Leistungsindikator 'GPU Engine' fehlt (sehr alter Treiber/Windows?)",
    },
}


def _line(status, text, detail=""):
    print(f"  [{status}] {text}" + (f"  – {detail}" if detail else ""))


def run(as_json=False, seconds=4.0):
    plat = "windows" if osutil.IS_WIN else "linux"
    res = dict(version=__version__, os=osutil.os_name(), python=sys.version.split()[0], platform=plat,
               data_dir=config.DATA_DIR, checks=[], metrics={}, missing={})

    def check(status, text, detail=""):
        res["checks"].append(dict(status=status, text=text, detail=detail))
        if not as_json:
            _line(status, text, detail)

    if not as_json:
        print(f"Lagscope {__version__} – Selbsttest")
        print(f"  System: {res['os']} · Python {res['python']}")
        print(f"  Daten:  {config.DATA_DIR}\n")
        print("Grundlagen")

    if sys.version_info < (3, 8):
        check(BAD, "Python-Version", "mindestens Python 3.8 nötig")
    else:
        check(OK, "Python-Version", res["python"])
    try:
        con = db.connect()
        con.execute("SELECT COUNT(*) FROM samples").fetchone()
        con.close()
        check(OK, "Datenbank / Datenordner beschreibbar", config.DB_FILE)
    except (OSError, sqlite3.Error) as e:
        check(BAD, "Datenbank / Datenordner", repr(e))

    from . import daemon
    running = daemon.is_running()
    check(OK if running else WARN, "Logger läuft" if running else "Logger läuft gerade nicht",
          "" if running else "wird mit Doppelklick / `lagscope start` gestartet")

    cfg = config.load()
    if not as_json:
        print("\nMesswerte (Messung über ein paar Sekunden …)")
    sampler = collector.SystemSampler(cfg, passive=True)
    t0 = time.perf_counter()
    sampler.sample()
    first_cost = time.perf_counter() - t0
    time.sleep(seconds / 2)
    sampler.sample()
    time.sleep(seconds / 2)
    t0 = time.perf_counter()
    out, state, procs, ready, game = sampler.sample()
    cost = time.perf_counter() - t0
    res["metrics"] = {k: v for k, v in out.items() if k in metrics.M}
    res["state"] = state
    important = IMPORTANT_WIN if osutil.IS_WIN else IMPORTANT_LINUX
    for k in important:
        info = metrics.M.get(k, {"label": k, "unit": ""})
        if k in out:
            check(OK, info["label"], f"{out[k]} {info['unit']}".strip())
        else:
            why = WHY_MISSING[plat].get(k, "nicht verfügbar")
            res["missing"][k] = why
            check(WARN, info["label"] + " fehlt", why)
    extra = sorted(k for k in out if k in metrics.M and k not in important)
    if not as_json and extra:
        print(f"      weitere Werte: {', '.join(extra)}")
    for line in sampler.capabilities():
        check("i", line)
    if state:
        check("i", "Systemeinstellungen", ", ".join(f"{k}={v}" for k, v in state.items() if k != "foreground"))

    if not as_json:
        print("\nVordergrund, Spiel, Protokolle")
    fg = sampler.fg.get() or {}
    if fg.get("name") or fg.get("title"):
        check(OK, "Fenster im Vordergrund erkannt", f"{fg.get('name') or '?'} – {fg.get('title') or ''}".strip(" –"))
    else:
        hint = ""
        if not osutil.IS_WIN:
            if not (shutil.which("xprop") or shutil.which("xdotool") or shutil.which("kdotool")):
                hint = "xprop/xdotool fehlen"
            elif not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                hint = "keine grafische Sitzung (im Terminal ohne Desktop normal)"
            else:
                hint = "unter Wayland nur für Spiele/X11-Fenster – Spiele über Proton laufen über Xwayland und werden erkannt"
        check(WARN, "Fenster im Vordergrund nicht erkannt", hint)
    pats = ", ".join(cfg["game_patterns"])
    if game:
        check(OK, "Spiel erkannt", ", ".join(sorted({p.name for p in game})))
    else:
        check(OK, f"Spiel-Erkennung aktiv ({pats})", "Spiel läuft gerade nicht")
    check(OK, f"{len(procs)} Prozesse gelesen")

    if osutil.IS_WIN:
        try:
            r = osutil.run(["wevtutil", "qe", "System", "/c:1", "/rd:true", "/f:text"], timeout=15)
            check(OK if r.returncode == 0 else WARN, "Windows-Ereignisprotokoll lesbar" if r.returncode == 0 else
                  "Ereignisprotokoll nicht lesbar")
        except Exception as e:
            check(WARN, "Ereignisprotokoll nicht lesbar", repr(e))
        info = sampler.status_info()
        pm = info.get("presentmon_path")
        if pm:
            check(OK, "PresentMon gefunden (FPS)", pm)
        else:
            check(WARN, "PresentMon nicht gefunden", "ohne PresentMon keine FPS-Werte – PresentMon.exe neben lagscope.exe legen")
        if out.get("cpu_temp") is None:
            check(WARN, "LibreHardwareMonitor nicht erreichbar", "optional, nur für Temperaturen/Takt")
    else:
        ok = False
        if shutil.which("journalctl"):
            try:
                r = osutil.run(["journalctl", "-k", "-n", "1", "--no-pager", "-q"], timeout=10)
                ok = r.returncode == 0 and bool(r.stdout.strip())
            except Exception:
                ok = False
        if not ok and shutil.which("dmesg"):
            try:
                r = osutil.run(["dmesg"], timeout=10)
                ok = r.returncode == 0 and bool(r.stdout.strip())
            except Exception:
                ok = False
        check(OK if ok else WARN, "Kernel-Log lesbar" if ok else "Kernel-Log nicht lesbar",
              "" if ok else "Benutzer braucht Leserechte (Gruppe systemd-journal/wheel/adm) – nicht schlimm")
        for tool, why in (("notify-send", "Bestätigung beim Markieren"), ("mangohud", "FPS-Werte")):
            check(OK if shutil.which(tool) else WARN, f"{tool} {'vorhanden' if shutil.which(tool) else 'fehlt'}",
                  "" if shutil.which(tool) else why)

    # Webserver-Port
    if not running:
        s = socket.socket()
        try:
            s.bind((cfg["web_host"], int(cfg["web_port"])))
            check(OK, f"Port {cfg['web_port']} frei")
        except OSError:
            check(WARN, f"Port {cfg['web_port']} belegt", "es wird automatisch der nächste freie Port genommen")
        finally:
            s.close()

    interval = float(cfg["interval"])
    est = (cost / interval) * 100
    res["sample_ms"] = round(cost * 1000, 1)
    res["first_sample_ms"] = round(first_cost * 1000, 1)
    check(OK if est < 2 else WARN, "Aufwand pro Messung", f"{cost * 1000:.1f} ms alle {interval:g} s ≈ {est:.2f} % eines CPU-Kerns")

    bad = [c for c in res["checks"] if c["status"] == BAD]
    warn = [c for c in res["checks"] if c["status"] == WARN]
    res["result"] = "fehler" if bad else ("ok_mit_hinweisen" if warn else "ok")
    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        print()
        if bad:
            print(f"Ergebnis: {len(bad)} Fehler – Lagscope funktioniert so nicht.")
        elif warn:
            print(f"Ergebnis: funktioniert ({len(warn)} Hinweise – fehlende Werte schränken die Analyse nur etwas ein).")
        else:
            print("Ergebnis: alles in Ordnung.")
    return 1 if bad else 0
