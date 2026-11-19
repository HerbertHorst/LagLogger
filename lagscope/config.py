"""Paths and user configuration.

lagscope is portable: all data lives in a folder `lagscope-data` right next to the program file.
If that folder can't be written (e.g. read-only medium), ~/.local/share/lagscope is used instead.
"""
import json
import os
import sys

HOME = os.path.expanduser("~")

# In a zipapp, __file__ is ".../lagscope(.pyz)/lagscope/config.py" -> APP_ROOT is the zip file itself.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_IS_FILE = os.path.isfile(APP_ROOT)
APP_DIR = os.path.dirname(APP_ROOT) if APP_IS_FILE else APP_ROOT


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
        return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "lagscope")


DATA_DIR = _pick_data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "lagscope.db")
LOCK_FILE = os.path.join(DATA_DIR, "lagscope.lock")
LOG_FILE = os.path.join(DATA_DIR, "lagscope.log")
MANGOHUD_DIR = os.path.join(DATA_DIR, "mangohud")


def launch_command(*args):
    """Shell command that runs this exact copy of lagscope (used for autostart / shortcut)."""
    import shlex
    if APP_IS_FILE:
        base = [sys.executable or "python3", APP_ROOT]
    else:
        base = [sys.executable or "python3", os.path.join(APP_ROOT, "lagscope.py")]
    return " ".join(shlex.quote(a) for a in base + list(args))


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
