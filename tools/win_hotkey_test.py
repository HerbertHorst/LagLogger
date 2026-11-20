#!/usr/bin/env python3
"""Windows: checks the hotkeys end to end (used by GitHub Actions).

1. simulated key press Ctrl+Alt+L via SendInput  -> does Windows deliver it?
2. WM_HOTKEY posted straight to the hotkey thread -> does Lagscope turn it into a marker?
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
exe = args[0] if args else None
detached = "--detached" in sys.argv   # wie beim Doppelklick: "start" startet den Logger losgelöst
base = [exe] if exe else [sys.executable, os.path.join(ROOT, "lagscope.py")]
tmp = tempfile.mkdtemp(prefix="lagscope-hk ")
env = dict(os.environ, LAGSCOPE_DATA=os.path.join(tmp, "data"))
os.makedirs(env["LAGSCOPE_DATA"])
with open(os.path.join(env["LAGSCOPE_DATA"], "config.json"), "w") as f:
    json.dump({"interval": 1.0, "web_port": 18799}, f)
log = open(os.path.join(tmp, "run.log"), "w")
if detached:
    subprocess.run(base + ["start"], env=env, timeout=60)
    proc = None
else:
    proc = subprocess.Popen(base + ["run"], env=env, stdout=log, stderr=subprocess.STDOUT)
url = "http://127.0.0.1:18799/"


def status():
    with urllib.request.urlopen(url + "api/status", timeout=10) as r:
        return json.loads(r.read())


for _ in range(80):
    time.sleep(0.5)
    try:
        st = status()
        if st.get("win", {}).get("hotkey_thread"):
            break
    except Exception:
        pass
time.sleep(3)
st = status()
print("Hotkey-Status:", st["win"].get("hotkeys"), "Thread:", st["win"].get("hotkey_thread"), flush=True)

# --- 1. echter Tastendruck (SendInput)
user32 = ctypes.WinDLL("user32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def key(vk, up=False):
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=2 if up else 0, time=0, dwExtraInfo=0)
    return i


seq = [key(0x11), key(0x12), key(0x4C), key(0x4C, True), key(0x12, True), key(0x11, True)]  # Ctrl, Alt, L
arr = (INPUT * len(seq))(*seq)
sent = user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
print("SendInput gesendet:", sent, "Fehler:", ctypes.get_last_error(), flush=True)
time.sleep(5)
st = status()
print("Nach Tastendruck: angekommen =", st["win"].get("hotkey_pressed"), "Marker =", st["n_markers"], flush=True)
key_ok = st["n_markers"] >= 1

# --- 2. WM_HOTKEY direkt an den Thread
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
ok = user32.PostThreadMessageW(st["win"]["hotkey_thread"], 0x0312, 2, 0)  # id 2 = "good"
print("PostThreadMessage:", ok, flush=True)
time.sleep(5)
st2 = status()
print("Nach WM_HOTKEY: angekommen =", st2["win"].get("hotkey_pressed"), "Marker =", st2["n_markers"], flush=True)
chain_ok = st2["n_markers"] > st["n_markers"]

req = urllib.request.Request(url + "api/stop", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=5).read()
if proc:
    proc.wait(timeout=30)
else:
    time.sleep(5)
log.close()
logfile = os.path.join(env["LAGSCOPE_DATA"], "lagscope.log") if detached else os.path.join(tmp, "run.log")
text = open(logfile, encoding="utf-8", errors="replace").read() if os.path.exists(logfile) else "(kein Log)"
print("---- Log ----\n" + text[-3000:])
print("ERGEBNIS: Tastendruck->Marker:", key_ok, "| WM_HOTKEY->Marker:", chain_ok)
sys.exit(0 if (chain_ok and key_ok) else 1)
