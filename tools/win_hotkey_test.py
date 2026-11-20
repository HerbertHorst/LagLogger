#!/usr/bin/env python3
"""Windows: tests the hotkeys end to end, as the user would use them (used by GitHub Actions).

    python tools/win_hotkey_test.py [lagscope.exe] [--detached]

1. real key press Ctrl+Alt+L (SendInput)                          -> marker?
2. a window that behaves like a game (raw input with RIDEV_NOHOTKEYS, which switches normal Windows hotkeys
   off while it is in the foreground) + key press Ctrl+Alt+G   -> marker anyway (keyboard hook)?
3. --detached: started like a double click ("start" must return at once, the logger keeps running)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
args = [a for a in sys.argv[1:] if not a.startswith("--")]
exe = os.path.abspath(args[0]) if args else None
detached = "--detached" in sys.argv
base = [exe] if exe else [sys.executable, os.path.join(ROOT, "lagscope.py")]
tmp = tempfile.mkdtemp(prefix="lagscope-hk ")
env = dict(os.environ, LAGSCOPE_DATA=os.path.join(tmp, "data"))
os.makedirs(env["LAGSCOPE_DATA"])
with open(os.path.join(env["LAGSCOPE_DATA"], "config.json"), "w") as f:
    json.dump({"interval": 1.0, "web_port": 18799}, f)
URL = "http://127.0.0.1:18799/"
FAILS = []


def ok(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def status():
    with urllib.request.urlopen(URL + "api/status", timeout=10) as r:
        return json.loads(r.read())


def wait_logger():
    for _ in range(120):
        try:
            st = status()
            if (st.get("win") or {}).get("hotkey_thread"):
                return st
        except Exception:
            pass
        time.sleep(0.5)
    return None


user32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def press(*vks):
    seq = []
    for vk in vks:
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
        seq.append(i)
    for vk in reversed(vks):
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=2, time=0, dwExtraInfo=0)
        seq.append(i)
    arr = (INPUT * len(seq))(*seq)
    return user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def wait_markers(n, timeout=8):
    end = time.time() + timeout
    st = status()
    while st["n_markers"] < n and time.time() < end:
        time.sleep(0.5)
        st = status()
    return st


# ---------------------------------------------------------------- "Spiel"-Fenster (blockiert Hotkeys)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT), ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


game = {"hwnd": None, "raw": False}


def game_window():
    proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
    game["proc"] = proc
    wc = WNDCLASSW(lpfnWndProc=proc, hInstance=k32.GetModuleHandleW(None), lpszClassName="FakeGame")
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "FakeGame", "Overwatch (Test)", 0x10CF0000, 100, 100, 640, 480,
                                  None, None, wc.hInstance, None)  # WS_OVERLAPPEDWINDOW | WS_VISIBLE
    rid = RAWINPUTDEVICE(1, 6, 0x00000200, hwnd)  # Tastatur, RIDEV_NOHOTKEYS – wie viele Spiele
    game["raw"] = bool(user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)))
    game["hwnd"] = hwnd
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------- Ablauf
log = open(os.path.join(tmp, "run.log"), "w")
proc = None
print(f"Teste {' '.join(base)} ({'losgelöst wie Doppelklick' if detached else 'direkt'})", flush=True)
if detached:
    t0 = time.time()
    starter = subprocess.Popen(base + ["start"], env=env)
    try:
        starter.wait(timeout=30)
        ok(True, f"'start' kehrt sofort zurück ({time.time() - t0:.1f} s)")
    except subprocess.TimeoutExpired:
        ok(False, "'start' kehrt nicht zurück (hängt)")
        starter.kill()
else:
    proc = subprocess.Popen(base + ["run"], env=env, stdout=log, stderr=subprocess.STDOUT)

st = wait_logger()
ok(st is not None, "Logger läuft, Tastenkürzel registriert")
if st:
    info = st["win"].get("hotkey_info") or {}
    print("      Status:", info.get("status"), "| Tastatur-Hook:", info.get("hook"), flush=True)
    ok(bool(info.get("hook")), "Tastatur-Hook aktiv")

    # 1. normaler Tastendruck
    press(0x11, 0x12, 0x4C)  # Ctrl+Alt+L
    st = wait_markers(1)
    ok(st["n_markers"] == 1, f"Strg+Alt+L -> 1 Marker (nicht doppelt) [{st['n_markers']}]")
    last = (st["win"].get("hotkey_info") or {}).get("last") or {}
    print("      erkannt über:", last.get("via"), flush=True)

    # 2. Spiel-Fenster im Vordergrund, das normale Hotkeys abschaltet
    threading.Thread(target=game_window, daemon=True).start()
    for _ in range(40):
        if game["hwnd"]:
            break
        time.sleep(0.1)
    user32.SetForegroundWindow(game["hwnd"])
    time.sleep(1)
    fg = user32.GetForegroundWindow() == game["hwnd"]
    print(f"      Spiel-Fenster: RIDEV_NOHOTKEYS={game['raw']}, im Vordergrund={fg}", flush=True)
    time.sleep(1.2)  # Entprellung (1 s) abwarten
    press(0x11, 0x12, 0x47)  # Ctrl+Alt+G
    st = wait_markers(2)
    last = (st["win"].get("hotkey_info") or {}).get("last") or {}
    ok(st["n_markers"] == 2, f"Strg+Alt+G mit 'Spiel' im Vordergrund -> Marker [{st['n_markers']}, über {last.get('via')}]")
    if fg and game["raw"]:
        ok(last.get("via") == "Tastatur-Hook", "im 'Spiel' greift der Tastatur-Hook (normale Hotkeys sind blockiert)")
    ok(st["n_markers"] == 2 and "good" == last.get("kind"), "richtige Art (läuft gut)")

    req = urllib.request.Request(URL + "api/stop", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
if proc:
    proc.wait(timeout=30)
else:
    time.sleep(5)
log.close()
logfile = os.path.join(env["LAGSCOPE_DATA"], "lagscope.log") if detached else os.path.join(tmp, "run.log")
text = open(logfile, encoding="utf-8", errors="replace").read() if os.path.exists(logfile) else "(kein Log)"
ok("Traceback" not in text, "keine Fehler im Log")
ok("Tray-Symbol nicht möglich" not in text, "Tray-Symbol gestartet")
if FAILS:
    print("---- Log ----\n" + text[-3000:])
print("\n" + ("FEHLER: " + str(len(FAILS)) if FAILS else "Alles OK"))
sys.exit(1 if FAILS else 0)
