"""Autostart and menu entries.

Linux: only touches files named lagscope*.desktop. Windows: the value "Lagscope" under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (autostart); hotkeys are built in.
"""
import os
import shutil
import subprocess

from . import config, osutil

HOME = os.path.expanduser("~")
AUTOSTART = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "autostart",
                         "lagscope.desktop")
APPS = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "applications")

LAUNCHERS = {
    "lagscope-open.desktop": ("Lagscope", "Lagscope-Auswertung im Browser öffnen", ("open",), "lagscope"),
    "lagscope-mark-lag.desktop": ("Lagscope: Lag markieren", "Markiert: jetzt laggt es", ("mark",), "dialog-warning"),
    "lagscope-mark-good.desktop": ("Lagscope: Läuft gut markieren", "Markiert: jetzt läuft es normal",
                                   ("mark", "--good"), "emblem-ok"),
    "lagscope-mark-end.desktop": ("Lagscope: Lag vorbei markieren", "Markiert: der Lag ist vorbei",
                                  ("mark", "--end"), "emblem-default"),
}


def desktop_exec(args):
    """Exec= value following the Desktop Entry spec (double quotes; \\ " ` $ escaped; % doubled)."""
    out = []
    for a in args:
        a = a.replace("%", "%%")
        if any(c in a for c in ' \t\n"\'\\><~|&;$*?#()`'):
            a = '"' + "".join("\\" + c if c in '"`$\\' else c for c in a) + '"'
        out.append(a)
    # Auf Ebene der Schlüsseldatei muss jeder Backslash noch einmal verdoppelt werden
    return " ".join(out).replace("\\", "\\\\")


def icon_path():
    """Put Lagscope's own icon next to the data so menu entries can show it."""
    path = os.path.join(config.DATA_DIR, "icon.png")
    try:
        from .server import static
        with open(path, "wb") as f:
            f.write(static("icon.png"))
        return path
    except Exception:
        return "utilities-system-monitor"


def desktop_entry(name, comment, args, icon, extra=""):
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={name}\nComment={comment}\n"
            f"Exec={desktop_exec(config.launch_args(*args))}\n"
            f"Icon={icon_path() if icon == 'lagscope' else icon}\nTerminal=false\nCategories=Utility;\n{extra}")


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
        return any(desktop_exec(config.launch_args(a)) in text for a in ("start", "open"))
    except OSError:
        return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_autostart_value():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "Lagscope")[0]
    except OSError:
        return None


def _win_status():
    val = _win_autostart_value()
    return dict(
        autostart=val is not None,
        autostart_here=val == config.launch_command("start"),
        launchers=True, launchers_here=True,
        desktop="Windows", session="",
        tools={"nvidia-smi": bool(shutil.which("nvidia-smi")), "wevtutil": bool(shutil.which("wevtutil"))},
    )


def _win_apply(action):
    import winreg
    if action == "autostart_on":
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, "Lagscope", 0, winreg.REG_SZ, config.launch_command("start"))
    elif action == "autostart_off":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "Lagscope")
        except OSError:
            pass
    elif action == "fps_rights":
        # PresentMon braucht die Gruppe "Leistungsprotokollbenutzer" (SID S-1-5-32-559) – einmalig mit Admin-Rechten
        import ctypes
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        cmd = (f"Add-LocalGroupMember -SID 'S-1-5-32-559' -Member '{user}' -ErrorAction SilentlyContinue; "
               "Write-Host 'Fertig. Bitte einmal ab- und wieder anmelden (oder neu starten).'; Start-Sleep 5")
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                                f'-NoProfile -Command "{cmd}"', None, 1)
        if r <= 32:
            return dict(ok=False, error="Abgebrochen oder keine Admin-Rechte.")
    else:
        return dict(ok=False, error="unbekannte Aktion")
    return dict(ok=True, status=status())


def status():
    if osutil.IS_WIN:
        return _win_status()
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
    if osutil.IS_WIN:
        return _win_apply(action)
    if action == "autostart_on":
        _write(AUTOSTART, desktop_entry("Lagscope Logger", "Zeichnet Systemwerte für die Lag-Analyse auf",
                                        ("start",), "lagscope",
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
