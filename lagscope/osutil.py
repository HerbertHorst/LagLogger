"""Small OS-specific helpers (Linux and Windows) so the rest of the code stays platform independent."""
import os
import subprocess
import sys
import threading

IS_WIN = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller-EXE

# Kein Konsolenfenster für Hilfsprogramme unter Windows
NOWINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW


def popen_kwargs():
    if IS_WIN:
        return {"creationflags": NOWINDOW}
    return {}


def run(args, timeout=5, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **popen_kwargs(), **kw)


# ------------------------------------------------------------------ single instance lock

class InstanceLock:
    """Exclusive lock on a file; released automatically when the process ends."""

    def __init__(self, path):
        self.path = path
        self.f = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        f = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self):
        if not self.f:
            return
        try:
            if IS_WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_UN)
        except OSError:
            pass
        self.f.close()
        self.f = None


def is_locked(path):
    lk = InstanceLock(path)
    if lk.acquire():
        lk.release()
        return False
    return True


# ------------------------------------------------------------------ priority

def lower_priority():
    """Run with low CPU and IO priority so the logger never competes with the game."""
    if IS_WIN:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetCurrentProcess()
            k32.SetPriorityClass(h, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
            k32.SetPriorityClass(h, 0x00100000)  # PROCESS_MODE_BACKGROUND_BEGIN (niedrige IO-/Speicherpriorität)
        except Exception:
            pass
        return
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        # ioprio_set(IOPRIO_WHO_PROCESS, 0, IOPRIO_CLASS_IDLE) – ohne externes ionice
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        nr = {"x86_64": 251, "aarch64": 30}.get(os.uname().machine)
        if nr:
            libc.syscall(nr, 1, 0, 3 << 13)
    except Exception:
        pass


# ------------------------------------------------------------------ processes

def spawn_detached(args, log_path):
    """Start a background process without a terminal/console window."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logf = open(log_path, "a")
    kw = dict(stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True)
    if IS_WIN:
        DETACHED_PROCESS, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
        flags = DETACHED_PROCESS | NEW_GROUP | NOWINDOW
        # Die EXE (PyInstaller) läuft in einem Windows-Job-Objekt. Ohne BREAKAWAY bliebe der Logger an den
        # startenden Prozess gekoppelt (würde mit ihm beendet bzw. hielte ihn am Leben).
        try:
            subprocess.Popen(args, creationflags=flags | BREAKAWAY, **kw)
            return
        except OSError:
            kw["creationflags"] = flags  # Job erlaubt kein Herauslösen
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)


def pid_alive(pid):
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate(pid):
    if IS_WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if h:
            ctypes.windll.kernel32.TerminateProcess(h, 0)
            ctypes.windll.kernel32.CloseHandle(h)
        return
    os.kill(pid, 15)


# ------------------------------------------------------------------ user feedback

def open_url(url):
    if IS_WIN:
        try:
            os.startfile(url)
            return True
        except OSError:
            pass
    else:
        import shutil
        for cmd in (["xdg-open", url], ["gio", "open", url], ["kde-open", url]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return True
    import webbrowser
    return webbrowser.open(url)


BEEPS = {"lag": [(880, 110), (880, 110)], "good": [(660, 160)], "end": [(440, 220)]}


def notify(title, body="", kind=None):
    """Short confirmation: desktop notification on Linux, a short beep on Windows (works in fullscreen games)."""
    if IS_WIN:
        def beep():
            try:
                import winsound
                for f, d in BEEPS.get(kind or "", [(700, 150)]):
                    winsound.Beep(f, d)
            except Exception:
                pass
        threading.Thread(target=beep, daemon=True).start()
        return
    import shutil
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", "lagscope", "-t", "2500", "-i", "dialog-information", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def error_box(title, text):
    """Visible error message even without a terminal (double click)."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:
            pass
    notify(title, text)


def attach_console():
    """The Windows EXE has no console window. When started from cmd/PowerShell, print there anyway."""
    if not (IS_WIN and FROZEN) or sys.stdout is not None:
        return  # Ausgabe ist schon umgeleitet (z. B. in eine Datei/Pipe) – nichts tun
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
            return
    except Exception:
        pass
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def os_name():
    if IS_WIN:
        import platform
        return f"Windows {platform.release()} ({platform.version()})"
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                info[k] = v.strip('"')
    except OSError:
        pass
    return info.get("PRETTY_NAME") or info.get("NAME") or "Linux"
