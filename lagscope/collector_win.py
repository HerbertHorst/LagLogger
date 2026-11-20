"""Windows: reads system state through Windows APIs (ctypes) – no extra Python packages needed.

* processes: one NtQuerySystemInformation call for all processes (cheap, no per-process handles)
* CPU per core: NtQuerySystemInformation(SystemProcessorPerformanceInformation)
* GPU load / VRAM, disk, network, CPU clock: performance counters (PDH, language independent English names)
* temperatures / exact clocks: LibreHardwareMonitor web server, if it runs (optional)
* FPS: PresentMon (optional, started automatically while the game runs)
* hotkeys: RegisterHotKey – no setup needed
* "kernel log": Windows System event log (errors/warnings: display driver resets, WHEA, resource exhaustion …)
"""
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

from . import config
from .common import NvidiaGpu, ProcInfo
from .osutil import popen_kwargs

ntdll = ctypes.WinDLL("ntdll")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
pdh = ctypes.WinDLL("pdh")

MY_PID = os.getpid()
NCPU = os.cpu_count() or 1

ULONG, USHORT = ctypes.c_ulong, ctypes.c_ushort


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", USHORT), ("MaximumLength", USHORT), ("Buffer", ctypes.c_void_p)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ULONG), ("NumberOfThreads", ULONG), ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ULONG), ("NumberOfThreadsHighWatermark", ULONG), ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING), ("BasePriority", ctypes.c_long), ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p), ("HandleCount", ULONG), ("SessionId", ULONG),
        ("UniqueProcessKey", ctypes.c_void_p), ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t), ("PageFaultCount", ULONG), ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


class SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [("IdleTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong),
                ("DpcTime", ctypes.c_longlong), ("InterruptTime", ctypes.c_longlong), ("InterruptCount", ULONG)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


ntdll.NtQuerySystemInformation.argtypes = [ULONG, ctypes.c_void_p, ULONG, ctypes.POINTER(ULONG)]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
k32.GetTickCount64.restype = ctypes.c_ulonglong
k32.GetSystemTimes.argtypes = [ctypes.POINTER(wt.FILETIME)] * 3
k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]

STATUS_INFO_LENGTH_MISMATCH = -1073741820  # 0xC0000004


def uptime_s():
    return k32.GetTickCount64() / 1000.0


def boot_id():
    # Windows hat keine boot_id; Startzeitpunkt (auf 10 s gerundet) – der Daemon vergleicht mit Toleranz
    return "t:%d" % int(round((time.time() - uptime_s()) / 10.0) * 10)


def _ft(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _query_sysinfo(cls, buf_holder):
    size = ULONG(0)
    buf = buf_holder[0]
    while True:
        status = ntdll.NtQuerySystemInformation(cls, buf, len(buf), ctypes.byref(size))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            buf = ctypes.create_string_buffer(max(size.value + 65536, len(buf) * 2))
            buf_holder[0] = buf
            continue
        if status < 0:
            return None
        return buf


# ---------------------------------------------------------------- processes

class ProcessTracker:
    def __init__(self, game_patterns):
        self.game_patterns = game_patterns
        self.prev = {}
        self.prev_t = None
        self.buf = [ctypes.create_string_buffer(512 * 1024)]

    def is_game(self, name, cmd=""):
        n = (name or "").lower()
        return any(p in n for p in self.game_patterns)

    def scan(self):
        now = time.monotonic()
        dt = (now - self.prev_t) if self.prev_t else None
        self.prev_t = now
        buf = _query_sysinfo(5, self.buf)  # SystemProcessInformation
        procs, cur = [], {}
        if buf is None:
            return procs, False
        off = 0
        while True:
            spi = SYSTEM_PROCESS_INFORMATION.from_buffer(buf, off)
            pid = spi.UniqueProcessId or 0
            if pid:
                if spi.ImageName.Buffer:
                    name = ctypes.wstring_at(spi.ImageName.Buffer, spi.ImageName.Length // 2)
                else:
                    name = "System"
                ticks = spi.UserTime + spi.KernelTime  # 100 ns
                p = ProcInfo()
                p.pid, p.name, p.threads, p.start, p.state = pid, name, spi.NumberOfThreads, spi.CreateTime, "R"
                p.rss = (spi.WorkingSetPrivateSize or spi.WorkingSetSize) / 1048576.0
                prev = self.prev.get(pid)
                p.ticks = (max(0, ticks - prev[0]) / 1e7 / dt * 100.0) if (dt and prev and prev[1] == spi.CreateTime) else 0.0
                cur[pid] = (ticks, spi.CreateTime)
                p.cmd = "" if pid == 4 or name in ("System", "Registry", "Secure System", "Memory Compression") else name
                p.is_game = self.is_game(name)
                procs.append(p)
            if not spi.NextEntryOffset:
                break
            off += spi.NextEntryOffset
        self.prev = cur
        return procs, dt is not None


def process_name(pid):
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(h)
    return ""


class Foreground:
    xprop = xdotool = kdotool = None

    def get(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return dict(pid=pid.value, name=process_name(pid.value), title=buf.value, via="win32")


# ---------------------------------------------------------------- performance counters (PDH)

class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]


pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
pdh.PdhOpenQueryW.restype = ctypes.c_uint32
pdh.PdhAddEnglishCounterW.argtypes = [wt.HANDLE, wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
pdh.PdhAddEnglishCounterW.restype = ctypes.c_uint32
pdh.PdhCollectQueryData.argtypes = [wt.HANDLE]
pdh.PdhCollectQueryData.restype = ctypes.c_uint32
pdh.PdhGetFormattedCounterValue.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                            ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
pdh.PdhGetFormattedCounterValue.restype = ctypes.c_uint32
pdh.PdhGetFormattedCounterArrayW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                             ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_uint32
pdh.PdhCloseQuery.argtypes = [wt.HANDLE]

PDH_FMT = 0x00000200 | 0x00008000  # DOUBLE | NOCAP100
PDH_MORE_DATA = 0x800007D2


class Pdh:
    def __init__(self, counters):
        self.q = wt.HANDLE()
        self.c = {}
        self.ok = pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) == 0
        if self.ok:
            for key, path in counters.items():
                h = wt.HANDLE()
                if pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h)) == 0:
                    self.c[key] = h
            self.collect()

    def collect(self):
        if self.ok:
            pdh.PdhCollectQueryData(self.q)

    def value(self, key):
        h = self.c.get(key)
        if not h:
            return None
        v = PDH_FMT_COUNTERVALUE()
        if pdh.PdhGetFormattedCounterValue(h, PDH_FMT, None, ctypes.byref(v)) == 0 and v.CStatus in (0, 1):
            return v.doubleValue
        return None

    def array(self, key):
        h = self.c.get(key)
        if not h:
            return []
        size, count = wt.DWORD(0), wt.DWORD(0)
        r = pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT, ctypes.byref(size), ctypes.byref(count), None)
        if r != PDH_MORE_DATA or not size.value:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return []
        items = (PDH_FMT_COUNTERVALUE_ITEM_W * count.value).from_buffer(buf)
        return [(it.szName, it.FmtValue.doubleValue) for it in items if it.FmtValue.CStatus in (0, 1)]

    def close(self):
        if self.ok:
            pdh.PdhCloseQuery(self.q)
            self.ok = False


SYS_COUNTERS = {
    "perf": r"\Processor Information(_Total)\% Processor Performance",
    "perf_all": r"\Processor Information(*)\% Processor Performance",
    "freq": r"\Processor Information(_Total)\Processor Frequency",
    "limit": r"\Processor Information(_Total)\% Performance Limit",
    "queue": r"\System\Processor Queue Length",
    "ctxt": r"\System\Context Switches/sec",
    "pagein": r"\Memory\Pages Input/sec",
    "pageout": r"\Memory\Pages Output/sec",
    "commit": r"\Memory\% Committed Bytes In Use",
    "disk_r": r"\PhysicalDisk(_Total)\Disk Read Bytes/sec",
    "disk_w": r"\PhysicalDisk(_Total)\Disk Write Bytes/sec",
    "disk_idle": r"\PhysicalDisk(_Total)\% Idle Time",
    "net_rx": r"\Network Interface(*)\Bytes Received/sec",
    "net_tx": r"\Network Interface(*)\Bytes Sent/sec",
}
GPU_COUNTERS = {
    "engine": r"\GPU Engine(*)\Utilization Percentage",
    "dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
    "shared": r"\GPU Adapter Memory(*)\Shared Usage",
}

POWER_PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Ausbalanciert",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "Höchstleistung",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Energiesparmodus",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimative Leistung",
}


def power_plan():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes") as k:
            guid = str(winreg.QueryValueEx(k, "ActivePowerScheme")[0]).lower()
        return POWER_PLANS.get(guid, guid)
    except Exception:
        return None


def vram_total_mb():
    """Largest dedicated video memory of all display adapters (registry), in MB."""
    best = 0
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        for name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                v = winreg.QueryValueEx(sk, name)[0]
                            except OSError:
                                continue
                            if isinstance(v, bytes):
                                v = int.from_bytes(v[:8], "little")
                            best = max(best, int(v))
                            break
                except OSError:
                    continue
    except Exception:
        pass
    return best / 1048576.0 if best else None


# ---------------------------------------------------------------- LibreHardwareMonitor (optional)

def _num(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.match(r"\s*(-?[\d.,]+)", str(text))
    if not m:
        return None
    s = m.group(1)
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


class LibreHardwareMonitor:
    """Reads http://localhost:8085/data.json (LibreHardwareMonitor → Options → Remote Web Server → Run)."""

    def __init__(self, url):
        self.url = url
        self.next_try = 0
        self.ok = False

    def sample(self, out):
        if not self.url or time.time() < self.next_try:
            return
        try:
            with urllib.request.urlopen(self.url, timeout=0.5) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            self.ok = True
        except Exception:
            self.ok = False
            self.next_try = time.time() + 60
            return
        cpu_temp = {}
        cpu_clocks = []
        gpu = {}
        for node in self._walk(data):
            sid = node.get("SensorId") or ""
            if not sid:
                continue
            parts = sid.strip("/").split("/")
            if len(parts) < 3:
                continue
            hw, typ, text = parts[0], parts[-2], node.get("Text", "")
            val = _num(node.get("Value"))
            if val is None:
                continue
            if hw in ("amdcpu", "intelcpu"):
                if typ == "temperature":
                    cpu_temp[text] = val
                elif typ == "clock" and text.startswith("Core #"):
                    cpu_clocks.append(val)
            elif hw.startswith("gpu-"):
                prio = 0 if hw == "gpu-intel" else 1
                gpu.setdefault(prio, {})[(typ, text)] = val
        for name in ("Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Core (Tdie)", "Core Max", "CPU Core"):
            if name in cpu_temp:
                out["cpu_temp"] = cpu_temp[name]
                break
        if cpu_clocks:
            out["cpu_freq_avg"] = round(sum(cpu_clocks) / len(cpu_clocks))
            out["cpu_freq_max"] = round(max(cpu_clocks))
        if gpu:
            g = gpu[max(gpu)]
            for (typ, text), key in ((("temperature", "GPU Core"), "gpu_temp"), (("temperature", "GPU Hot Spot"), "gpu_temp_hot"),
                                     (("clock", "GPU Core"), "gpu_clock"), (("clock", "GPU Memory"), "gpu_mclk")):
                if (typ, text) in g and key not in out:
                    out[key] = round(g[(typ, text)], 1)
            for text in ("GPU Package", "GPU Power", "GPU Core"):
                if ("power", text) in g and "gpu_power" not in out:
                    out["gpu_power"] = round(g[("power", text)], 1)

    def _walk(self, node):
        stack = [node]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(n.get("Children") or [])


# ---------------------------------------------------------------- PresentMon (FPS)

def find_presentmon(cfg):
    p = cfg.get("presentmon_path")
    if p and os.path.isfile(p):
        return p
    for d in (config.APP_DIR, os.path.join(config.APP_DIR, "tools"), config.DATA_DIR):
        hits = sorted(glob.glob(os.path.join(d, "PresentMon*.exe")))
        if hits:
            return hits[-1]
    return shutil.which("PresentMon") or shutil.which("PresentMon.exe")


class PresentMon:
    """Runs PresentMon for the game's exe while the game runs and turns frames into per-second FPS."""

    def __init__(self, cfg):
        self.exe = find_presentmon(cfg)
        self.proc = None
        self.target = None
        self.status = "nicht gefunden" if not self.exe else "bereit"
        self.lock = threading.Lock()
        self.buckets = {}   # sec -> {swapchain: [frametimes]}
        self.ready = []
        self.legacy = False

    def update(self, game_names):
        if not self.exe:
            return
        name = sorted(game_names)[0] if game_names else None
        if name == self.target and (self.proc is None or self.proc.poll() is None):
            return
        self.stop()
        self.target = name
        if name and self.status != "keine Rechte":
            self._start(name)

    def _start(self, name):
        if self.legacy:
            args = [self.exe, "-process_name", name, "-output_stdout", "-no_top", "-stop_existing_session",
                    "-session_name", "lagscope"]
        else:
            args = [self.exe, "--process_name", name, "--output_stdout", "--no_console_stats",
                    "--stop_existing_session", "--session_name", "lagscope", "--v1_metrics"]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         errors="replace", **popen_kwargs())
        except OSError as e:
            self.status = f"Fehler: {e}"
            return
        self.status = "läuft"
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="presentmon").start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.proc = None
        if self.status == "läuft":
            self.status = "bereit"

    def _read(self, proc):
        cols = None
        first_lines = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if cols is None:
                low = line.lower()
                if low.startswith("application,"):
                    cols = [c.strip() for c in line.split(",")]
                    idx = {c: i for i, c in enumerate(cols)}
                    ft_i = next((idx[c] for c in ("MsBetweenPresents", "MsBetweenAppStart", "FrameTime",
                                                  "MsBetweenDisplayChange") if c in idx), None)
                    sc_i = idx.get("SwapChainAddress")
                    if ft_i is None:
                        self.status = "unbekanntes Ausgabeformat"
                        break
                    continue
                if len(first_lines) < 20:
                    first_lines.append(line)  # Fehlermeldungen vor der Kopfzeile merken
                continue
            f = line.split(",")
            try:
                ft = float(f[ft_i])
            except (ValueError, IndexError):
                continue
            if ft <= 0 or ft > 5000:
                continue
            sec = int(time.time())
            sc = f[sc_i] if sc_i is not None and sc_i < len(f) else ""
            with self.lock:
                self.buckets.setdefault(sec, {}).setdefault(sc, []).append(ft)
        proc.wait()
        text = " ".join(first_lines).lower()
        if "access denied" in text or "performance log users" in text or "administrator" in text:
            self.status = "keine Rechte"
        elif cols is None and not self.legacy and ("unrecognized" in text or "unknown" in text or "usage" in text):
            self.legacy = True  # alte PresentMon-Version (1.x) – mit alten Argumenten erneut versuchen
            self.target = None
        elif self.status == "läuft":
            self.status = "bereit"

    def drain(self):
        """Finished seconds as (sec, fps, ft_max, ft_avg)."""
        now = int(time.time())
        out = []
        with self.lock:
            for sec in sorted(s for s in self.buckets if s < now - 1):
                chains = self.buckets.pop(sec)
                fts = max(chains.values(), key=len)  # Haupt-Swapchain (Overlays ignorieren)
                avg = sum(fts) / len(fts)
                out.append((sec, round(1000.0 / avg, 1), round(max(fts), 2), round(avg, 2)))
        return out


# ---------------------------------------------------------------- hotkeys

VK_NAMES = {"pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
            "pagedown": 0x22, "space": 0x20}
MODS = {"alt": 1, "ctrl": 2, "control": 2, "strg": 2, "shift": 4, "win": 8}


def parse_hotkey(combo):
    mods, vk = 0, None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in VK_NAMES:
            vk = VK_NAMES[part]
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", part):
            vk = 0x70 + int(part[1:]) - 1
        elif re.fullmatch(r"num[0-9]", part):
            vk = 0x60 + int(part[3])
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
    return (mods, vk) if vk else None


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
k32.GetModuleHandleW.restype = wt.HMODULE

WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP, WM_HOTKEY, WM_TIMER = 0x100, 0x101, 0x104, 0x105, 0x312, 0x113
MOD_VKS = {1: (0x12,), 2: (0x11,), 4: (0x10,), 8: (0x5B, 0x5C)}  # alt, ctrl, shift, win


class Hotkeys:
    """Global hotkeys on two ways at once:

    * RegisterHotKey – the normal Windows way. Many games switch it off while they are in the foreground
      (raw input with RIDEV_NOHOTKEYS) – exactly when you need it.
    * a low-level keyboard hook (like Discord push-to-talk) – also sees keys while such a game is active.
      Keys are only looked at, never swallowed. Windows silently drops slow hooks, so it is renewed every minute.

    Both report into the same queue; a second report of the same key within a second is ignored.
    """

    def __init__(self, combos):
        self.combos = combos or {}
        self.events = []
        self.status = {}
        self.lock = threading.Lock()
        self.thread_id = None
        self.pressed = 0          # wie oft ein Kürzel erkannt wurde (Diagnose)
        self.last = None          # (Zeit, Art, Weg)
        self.hook_ok = False
        self._last_fire = {}
        self._down = set()
        self._by_vk = {}
        self._hook = None
        self._proc = HOOKPROC(self._ll)  # Referenz behalten
        threading.Thread(target=self._loop, daemon=True, name="hotkeys").start()

    def _fire(self, kind, via):
        now = time.time()
        with self.lock:
            if now - self._last_fire.get(kind, 0) < 1.0:
                return  # schon über den anderen Weg erkannt
            self._last_fire[kind] = now
            self.pressed += 1
            self.last = (now, kind, via)
            self.events.append((now, kind))

    def _mods_down(self, mods):
        for bit, vks in MOD_VKS.items():
            if mods & bit and not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
                return False
        return True

    def _ll(self, code, wparam, lparam):
        try:
            if code == 0:
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                vk = kb.vkCode
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if vk in self._by_vk and vk not in self._down:  # nur beim ersten Drücken, nicht beim Halten
                        self._down.add(vk)
                        for mods, kind in self._by_vk[vk]:
                            if self._mods_down(mods):
                                self._fire(kind, "Tastatur-Hook")
                                break
                elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                    self._down.discard(vk)
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _install_hook(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        self._hook = user32.SetWindowsHookExW(13, self._proc, k32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
        self.hook_ok = bool(self._hook)

    def _loop(self):
        self.thread_id = k32.GetCurrentThreadId()
        ids = {}
        for i, (kind, combo) in enumerate(self.combos.items(), start=1):
            hk = parse_hotkey(combo or "")
            if not hk:
                self.status[kind] = f"{combo}: ungültig"
                continue
            self._by_vk.setdefault(hk[1], []).append((hk[0], kind))
            if user32.RegisterHotKey(None, i, hk[0] | 0x4000, hk[1]):  # MOD_NOREPEAT
                ids[i] = kind
                self.status[kind] = f"{combo}: aktiv"
            else:
                # von einem anderen Programm belegt – der Tastatur-Hook erkennt es trotzdem
                self.status[kind] = f"{combo}: aktiv (über Tastatur-Hook; Kürzel ist auch in einem anderen Programm belegt)"
        if not self._by_vk:
            return
        self._install_hook()
        user32.SetTimer(None, 0, 60000, None)  # Hook jede Minute erneuern
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                self._fire(ids[msg.wParam], "Windows-Hotkey")
            elif msg.message == WM_TIMER:
                self._install_hook()

    def drain(self):
        with self.lock:
            out, self.events = self.events, []
        return out

    def info(self):
        with self.lock:
            last = self.last
        return dict(status=self.status, hook=self.hook_ok, pressed=self.pressed,
                    last=dict(ts=last[0], kind=last[1], via=last[2]) if last else None)


# ---------------------------------------------------------------- event log

class KernelLog:
    """Polls the Windows System event log (critical/error/warning) once a minute."""

    QUERY = "*[System[(Level=1 or Level=2 or Level=3)]]"

    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()
        self.ok = None
        self.seen = set()
        threading.Thread(target=self._loop, daemon=True, name="eventlog").start()

    def _poll(self):
        r = subprocess.run(["wevtutil", "qe", "System", f"/q:{self.QUERY}", "/rd:true", "/c:50", "/f:RenderedXml"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                           **popen_kwargs())
        if r.returncode != 0:
            return None
        root = ET.fromstring("<Events>" + r.stdout + "</Events>")
        out = []
        for ev in root.findall("{*}Event"):
            sysn = ev.find("{*}System")
            if sysn is None:
                continue
            rid = (sysn.findtext("{*}EventRecordID") or "").strip()
            prov = sysn.find("{*}Provider")
            prov = prov.get("Name") if prov is not None else "?"
            eid = (sysn.findtext("{*}EventID") or "").strip()
            tc = sysn.find("{*}TimeCreated")
            ts = _parse_time(tc.get("SystemTime") if tc is not None else None)
            msg = ev.findtext("{*}RenderingInfo/{*}Message") or ""
            msg = " ".join(msg.split())
            out.append((rid, ts, f"{prov} (ID {eid}): {msg}"[:600]))
        return out

    def _loop(self):
        first = True
        while True:
            try:
                evs = self._poll()
                self.ok = evs is not None
                for rid, ts, text in reversed(evs or []):
                    if rid in self.seen:
                        continue
                    self.seen.add(rid)
                    if not first:
                        with self.lock:
                            self.lines.append((ts or time.time(), text))
                first = False
            except Exception:
                self.ok = False
            time.sleep(60)

    def drain(self):
        with self.lock:
            out, self.lines = self.lines, []
        return out


def _parse_time(s):
    if not s:
        return None
    import datetime
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", s)
    if not m:
        return None
    dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    frac = float(m.group(2) or 0)
    return dt.timestamp() + frac


# ---------------------------------------------------------------- system sampler

class SystemSampler:
    def __init__(self, cfg, passive=False):
        """passive=True (Selbsttest): keine Tastenkürzel registrieren und kein PresentMon starten."""
        self.cfg = cfg
        self.passive = passive
        self.procs = ProcessTracker(cfg["game_patterns"])
        self.fg = Foreground()
        self.nv = NvidiaGpu(cfg["interval"])
        self.amd = type("NoSysfsGpu", (), {"kind": None, "available": lambda self: False})()
        self.lhm = LibreHardwareMonitor(cfg.get("lhm_url"))
        self.pm = PresentMon(cfg)
        self.hotkeys = None if passive else Hotkeys(cfg.get("hotkeys"))
        self.sys = Pdh(SYS_COUNTERS)
        self.gpu = None
        self.gpu_created = 0
        self.vram_total = vram_total_mb()
        self.cpu_buf = [ctypes.create_string_buffer(ctypes.sizeof(SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION) * 256)]
        self.prev_sys = None
        self.prev_cores = None
        self.prev_t = None
        self.n = 0
        self.last_fg = None
        self.last_plan = (0, None)

    def _gpu_pdh(self):
        # GPU-Instanzen ändern sich mit jedem Prozess – Abfrage regelmäßig neu anlegen
        if self.gpu is None or time.time() - self.gpu_created > 60:
            if self.gpu:
                self.gpu.close()
            self.gpu = Pdh(GPU_COUNTERS)
            self.gpu_created = time.time()
            return False
        self.gpu.collect()
        return True

    def sample(self):
        now = time.monotonic()
        dt = (now - self.prev_t) if self.prev_t else None
        self.prev_t = now
        out, state = {}, {}

        # CPU gesamt
        idle, kern, usr = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(usr)):
            cur = (_ft(idle), _ft(kern), _ft(usr))
            if self.prev_sys:
                di, dk, du = (cur[i] - self.prev_sys[i] for i in range(3))
                if dk + du > 0:
                    out["cpu_total"] = round(100.0 * (1 - di / (dk + du)), 1)
            self.prev_sys = cur
        # CPU pro Kern
        buf = _query_sysinfo(8, self.cpu_buf)
        if buf is not None:
            sz = ctypes.sizeof(SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION)
            cores = [SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION.from_buffer(buf, i * sz) for i in range(min(NCPU, 256))]
            cur = [(c.IdleTime, c.KernelTime + c.UserTime) for c in cores]
            if self.prev_cores and len(self.prev_cores) == len(cur):
                busy = []
                for (i0, t0), (i1, t1) in zip(self.prev_cores, cur):
                    if t1 > t0:
                        busy.append(100.0 * (1 - (i1 - i0) / (t1 - t0)))
                if busy:
                    out["cpu_max_core"] = round(max(0.0, max(busy)), 1)
            self.prev_cores = cur

        # Leistungsindikatoren
        self.sys.collect()
        v = self.sys.value
        perf, freq = v("perf"), v("freq")
        if perf is not None and freq:
            out["cpu_freq_avg"] = round(perf * freq / 100)
            arr = [x for n, x in self.sys.array("perf_all") if "_Total" not in n]
            if arr:
                out["cpu_freq_max"] = round(max(arr) * freq / 100)
        for key, outk, scale in (("limit", "cpu_perf_limit", 1), ("queue", "procs_running", 1),
                                 ("ctxt", "ctxt_s", 1), ("pagein", "pgmajfault_s", 1), ("pageout", "pswpout_s", 1),
                                 ("commit", "commit_pct", 1), ("disk_r", "disk_read", 1 / 1048576),
                                 ("disk_w", "disk_write", 1 / 1048576)):
            x = v(key)
            if x is not None:
                out[outk] = round(x * scale, 2)
        idle_pct = v("disk_idle")
        if idle_pct is not None:
            out["disk_busy"] = round(max(0.0, min(100.0, 100 - idle_pct)), 1)
        for key, outk in (("net_rx", "net_rx"), ("net_tx", "net_tx")):
            arr = self.sys.array(key)
            if arr:
                out[outk] = round(sum(x for _, x in arr) / 1024, 1)

        # Speicher
        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            used = (ms.ullTotalPhys - ms.ullAvailPhys) / 1048576
            out["mem_used"] = round(used)
            out["mem_avail"] = round(ms.ullAvailPhys / 1048576)
            commit = (ms.ullTotalPageFile - ms.ullAvailPageFile) / 1048576
            out["swap_used"] = round(max(0.0, commit - used))

        # GPU
        if self._gpu_pdh():
            engines = {}
            for name, val in self.gpu.array("engine"):
                m = re.search(r"(luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+).*?(eng_\d+)", name)
                if m:
                    engines[m.groups()] = engines.get(m.groups(), 0.0) + val
            if engines:
                out["gpu_busy"] = round(min(100.0, max(engines.values())), 1)
            ded = self.gpu.array("dedicated")
            if ded:
                used = max(x for _, x in ded) / 1048576
                out["vram_used"] = round(used)
                if self.vram_total:
                    out["vram_pct"] = round(used * 100 / self.vram_total, 1)
            sh = self.gpu.array("shared")
            if sh:
                out["gtt_used"] = round(max(x for _, x in sh) / 1048576)
        if self.nv.available():
            self.nv.sample(out, state)
        if self.n % 2 == 0:
            self.lhm.sample(out)

        if time.time() - self.last_plan[0] > 30:
            self.last_plan = (time.time(), power_plan())
        if self.last_plan[1]:
            state["power_plan"] = self.last_plan[1]

        # Prozesse
        procs, ready = self.procs.scan()
        out["nprocs"] = len(procs)
        out["nthreads"] = sum(p.threads for p in procs)
        game = [p for p in procs if p.is_game]
        out["game_running"] = 1 if game else 0
        if game:
            out["game_cpu"] = round(sum(p.ticks for p in game), 1)
            out["game_rss"] = round(sum(p.rss for p in game))
            out["game_threads"] = sum(p.threads for p in game)
        out["uptime_h"] = round(uptime_s() / 3600, 3)
        me = [p for p in procs if p.pid == MY_PID]
        if me:
            out["self_cpu"] = round(me[0].ticks, 2)
            out["self_rss"] = round(me[0].rss)
        if not self.passive:
            self.pm.update({p.name for p in game})

        # Vordergrund
        self.n += 1
        fg = self.fg.get() or {}
        if fg:
            state["foreground"] = fg.get("name") or ""
            gp = {p.pid for p in game}
            out["fg_is_game"] = 1 if fg.get("pid") in gp or self.procs.is_game(fg.get("name", "")) else 0
        return out, state, procs, (ready and dt is not None), game

    def hotkey_events(self):
        return self.hotkeys.drain() if self.hotkeys else []

    def fps_rows(self):
        return self.pm.drain()

    def capabilities(self):
        gpu = "Windows-Leistungsindikatoren" + (" + NVIDIA" if self.nv.available() else "")
        return ["GPU: " + gpu,
                "Temperaturen: " + ("LibreHardwareMonitor" if self.lhm.ok else "keine (LibreHardwareMonitor läuft nicht)"),
                "FPS: PresentMon " + self.pm.status,
                "Tastenkürzel: " + (", ".join(self.hotkeys.status.values()) if self.hotkeys else "–")]

    def status_info(self):
        return dict(presentmon=self.pm.status, presentmon_path=self.pm.exe, lhm=self.lhm.ok,
                    hotkeys=self.hotkeys.status if self.hotkeys else {}, vram_total=self.vram_total,
                    hotkey_thread=self.hotkeys.thread_id if self.hotkeys else None,
                    hotkey_pressed=self.hotkeys.pressed if self.hotkeys else 0,
                    hotkey_info=self.hotkeys.info() if self.hotkeys else None)
