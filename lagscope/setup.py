"""Autostart and menu entries (for keyboard shortcuts). Only touches files named lagscope*.desktop."""
import os
import shutil
import subprocess

from . import config

HOME = os.path.expanduser("~")
AUTOSTART = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "autostart",
                         "lagscope.desktop")
APPS = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "applications")

LAUNCHERS = {
    "lagscope-open.desktop": ("Lagscope", "Lagscope-Auswertung im Browser öffnen", ("open",), "utilities-system-monitor"),
    "lagscope-mark-lag.desktop": ("Lagscope: Lag markieren", "Markiert: jetzt laggt es", ("mark",), "dialog-warning"),
    "lagscope-mark-good.desktop": ("Lagscope: Läuft gut markieren", "Markiert: jetzt läuft es normal",
                                   ("mark", "--good"), "emblem-ok"),
    "lagscope-mark-end.desktop": ("Lagscope: Lag vorbei markieren", "Markiert: der Lag ist vorbei",
                                  ("mark", "--end"), "emblem-default"),
}


def desktop_entry(name, comment, args, icon, extra=""):
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={name}\nComment={comment}\n"
            f"Exec={config.launch_command(*args)}\n"
            f"Icon={icon}\nTerminal=false\nCategories=Utility;System;\n{extra}")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def _refresh_menu():
    for cmd in (["kbuildsycoca6"], ["kbuildsycoca5"], ["update-desktop-database", APPS]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)


def _points_here(path):
    try:
        with open(path) as f:
            text = f.read()
        return config.launch_command("start") in text or config.launch_command("open") in text
    except OSError:
        return False


def status():
    autostart = os.path.exists(AUTOSTART)
    launchers = all(os.path.exists(os.path.join(APPS, n)) for n in LAUNCHERS)
    return dict(
        autostart=autostart,
        autostart_here=autostart and _points_here(AUTOSTART),
        launchers=launchers,
        launchers_here=launchers and _points_here(os.path.join(APPS, "lagscope-open.desktop")),
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", ""),
        session=os.environ.get("XDG_SESSION_TYPE", ""),
        tools={t: bool(shutil.which(t)) for t in ("xprop", "xdotool", "kdotool", "notify-send", "nvidia-smi",
                                                   "journalctl", "mangohud")},
    )


def apply(action):
    if action == "autostart_on":
        _write(AUTOSTART, desktop_entry("Lagscope Logger", "Zeichnet Systemwerte für die Lag-Analyse auf",
                                        ("start",), "utilities-system-monitor",
                                        "X-GNOME-Autostart-enabled=true\nX-KDE-autostart-after=panel\n"))
    elif action == "autostart_off":
        if os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)
    elif action == "launchers_on":
        for fname, (name, comment, args, icon) in LAUNCHERS.items():
            _write(os.path.join(APPS, fname), desktop_entry(name, comment, args, icon))
        _refresh_menu()
    elif action == "launchers_off":
        for fname in LAUNCHERS:
            p = os.path.join(APPS, fname)
            if os.path.exists(p):
                os.remove(p)
        _refresh_menu()
    else:
        return dict(ok=False, error="unbekannte Aktion")
    return dict(ok=True, status=status())
