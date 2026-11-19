"""Paths and user configuration.

lagscope is portable: all data lives in a folder `lagscope-data` right next to the program file.
If that folder can't be written (e.g. read-only medium), ~/.local/share/lagscope (Linux) or
%LOCALAPPDATA%/lagscope (Windows) is used instead.
"""
import json
import os
import sys

HOME = os.path.expanduser("~")
IS_WIN = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # Windows-EXE (PyInstaller): das Programm ist die EXE selbst
    APP_ROOT = os.path.abspath(sys.executable)
else:
    # In a zipapp, __file__ is ".../lagscope/lagscope/config.py" -> APP_ROOT is the zip file itself.
    APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_IS_FILE = os.path.isfile(APP_ROOT)
APP_DIR = os.path.dirname(APP_ROOT) if APP_IS_FILE else APP_ROOT


def _fallback_data_dir():
    if IS_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA") or HOME, "lagscope")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "lagscope")


def _pick_data_dir():
    if os.environ.get("LAGSCOPE_DATA"):
        return os.environ["LAGSCOPE_DATA"]
    portable = os.path.join(APP_DIR, "lagscope-data")
    try:
        os.makedirs(portable, exist_ok=True)
        test = os.path.join(portable, ".write-test")
        with open(test, "w") as f:
            f.write("x")
        os.remove(test)
        return portable
    except OSError:
        return _fallback_data_dir()


DATA_DIR = _pick_data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "lagscope.db")
LOCK_FILE = os.path.join(DATA_DIR, "lagscope.lock")
PID_FILE = os.path.join(DATA_DIR, "lagscope.pid")
LOG_FILE = os.path.join(DATA_DIR, "lagscope.log")
MANGOHUD_DIR = os.path.join(DATA_DIR, "mangohud")


def launch_args(*args):
    """Argument list that runs this exact copy of lagscope."""
    if FROZEN:
        return [APP_ROOT] + list(args)
    exe = sys.executable or "python3"
    if IS_WIN and exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"  # ohne Konsolenfenster
    script = APP_ROOT if APP_IS_FILE else os.path.join(APP_ROOT, "lagscope.py")
    return [exe, script] + list(args)


def launch_command(*args):
    """Command line that runs this exact copy of lagscope (used for autostart / shortcut)."""
    a = launch_args(*args)
    if IS_WIN:
        import subprocess
        return subprocess.list2cmdline(a)
    import shlex
    return " ".join(shlex.quote(x) for x in a)


DEFAULTS = {
    # Sekunden zwischen zwei Messungen
    "interval": 2.0,
    # Jede wievielte Messung eine Prozessliste gespeichert wird
    "proc_snapshot_every": 3,
    # Teile des Prozessnamens / der Kommandozeile, die das Spiel erkennen (Groß/Klein egal)
    "game_patterns": ["overwatch"],
    # Wie viele Tage Daten aufgehoben werden
    "retention_days": 14,
    # Weboberfläche
    "web_host": "127.0.0.1",
    "web_port": 8765,
    # Ordner, in den MangoHud seine FPS-Logs (CSV) schreibt ("" = lagscope-data/mangohud neben dem Programm)
    "mangohud_dir": "",
    # Kernel-Log (journalctl -k) mitschreiben
    "kernel_log": True,
    # Analyse: Zeitfenster um einen "Lag"-Marker (Sekunden davor / danach)
    "lag_before": 90,
    "lag_after": 60,
    # Automatische Lag-Erkennung aus MangoHud-FPS: FPS unter diesem Anteil des Sitzungs-Medians
    "auto_fps_ratio": 0.6,
    "auto_min_seconds": 10,
    # Nur Windows: eingebaute Tastenkürzel (Strg+Alt+…). Möglich: ctrl, alt, shift, win + Buchstabe/Ziffer/F1–F24
    "hotkeys": {"lag": "ctrl+alt+l", "good": "ctrl+alt+g", "end": "ctrl+alt+k"},
    # Nur Windows: LibreHardwareMonitor-Webserver (für Temperaturen und Takt)
    "lhm_url": "http://127.0.0.1:8085/data.json",
    # Nur Windows: Pfad zu PresentMon (leer = neben lagscope.exe suchen)
    "presentmon_path": "",
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        write_default_if_missing()
    except Exception as e:  # kaputte Config soll den Logger nicht stoppen
        print(f"[lagscope] Konnte {CONFIG_FILE} nicht lesen: {e}")
    cfg["game_patterns"] = [p.lower() for p in cfg.get("game_patterns") or []]
    cfg["mangohud_dir"] = os.path.expanduser(cfg.get("mangohud_dir") or "") or MANGOHUD_DIR
    try:
        os.makedirs(cfg["mangohud_dir"], exist_ok=True)
    except OSError:
        pass
    return cfg


def save(changes):
    """Merge changes into config.json (keeps everything else the user wrote there)."""
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = dict(DEFAULTS)
    data.update(changes)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_default_if_missing():
    if os.path.exists(CONFIG_FILE):
        return False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULTS, f, indent=2)
        return True
    except OSError:
        return False
