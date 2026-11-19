"""Reads system state from /proc and /sys (no external Python packages needed)."""
import glob
import os
import re
import shutil
import subprocess
import threading
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 1048576.0
NCPU = os.cpu_count() or 1
MY_PID = os.getpid()


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return default


def read_int(path, default=None):
    v = read(path)
    try:
        return int(v.strip())
    except Exception:
        return default


def boot_id():
    return (read("/proc/sys/kernel/random/boot_id") or "").strip()


def uptime_s():
    try:
        return float(read("/proc/uptime").split()[0])
    except Exception:
        return 0.0


# ---------------------------------------------------------------- processes

class ProcInfo:
    __slots__ = ("pid", "name", "cmd", "ticks", "threads", "rss", "state", "start", "is_game")


def _parse_stat(pid):
    s = read(f"/proc/{pid}/stat")
    if not s:
        return None
    try:
        l, r = s.index("("), s.rindex(")")
        name = s[l + 1:r]
        f = s[r + 2:].split()
        # f[0]=state, f[11]=utime, f[12]=stime, f[17]=num_threads, f[19]=starttime, f[21]=rss
        return name, f[0], int(f[11]) + int(f[12]), int(f[17]), int(f[19]), int(f[21])
    except Exception:
        return None


class ProcessTracker:
    """Keeps per-PID CPU tick counters to compute CPU% between two scans."""

    def __init__(self, game_patterns):
        self.game_patterns = game_patterns
        self.prev = {}        # pid -> (ticks, start)
        self.cmd_cache = {}   # (pid, start) -> cmdline
        self.prev_t = None

    def _cmd(self, pid, start):
        key = (pid, start)
        c = self.cmd_cache.get(key)
        if c is None:
            raw = read(f"/proc/{pid}/cmdline") or ""
            c = raw.replace("\0", " ").strip()[:300]
            self.cmd_cache[key] = c
        return c

    def is_game(self, name, cmd):
        n, c = name.lower(), cmd.lower()
        for p in self.game_patterns:
            if p in n:
                return True
            # Nur die Windows-EXE in der Kommandozeile prüfen (sonst würde z.B. ein Editor mit
            # "overwatch" im Dateinamen als Spiel gelten)
            if p in c and ".exe" in c:
                exe = c.split(".exe")[0]
                if p in os.path.basename(exe.replace("\\", "/")):
                    return True
        return False

    def scan(self):
        now = time.monotonic()
        dt = (now - self.prev_t) if self.prev_t else None
        self.prev_t = now
        procs = []
        cur = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            pid = int(d)
            st = _parse_stat(pid)
            if not st:
                continue
            name, state, ticks, threads, start, rss = st
            p = ProcInfo()
            p.pid, p.name, p.state, p.threads, p.start = pid, name, state, threads, start
            p.rss = rss * PAGE_MB
            prev = self.prev.get(pid)
            if dt and prev and prev[1] == start:
                p.ticks = max(0, ticks - prev[0]) / CLK_TCK / dt * 100.0
            else:
                p.ticks = 0.0
            cur[pid] = (ticks, start)
            p.cmd = self._cmd(pid, start)
            p.is_game = self.is_game(name, p.cmd)
            procs.append(p)
        self.prev = cur
        if len(self.cmd_cache) > 5000:
            alive = {(pid, v[1]) for pid, v in cur.items()}
            self.cmd_cache = {k: v for k, v in self.cmd_cache.items() if k in alive}
        return procs, dt is not None


def snapshot_list(procs, top=30, full_names=True):
    """Compact process list: top by CPU + top by RAM + all game processes, plus the set of all names."""
    by_cpu = sorted(procs, key=lambda p: -p.ticks)[:top]
    by_mem = sorted(procs, key=lambda p: -p.rss)[:10]
    chosen = {p.pid: p for p in by_cpu + by_mem + [p for p in procs if p.is_game]}
    rows = [[p.pid, p.name, round(p.ticks, 1), round(p.rss), p.threads, p.state, 1 if p.is_game else 0, p.cmd[:160]]
            for p in sorted(chosen.values(), key=lambda p: -p.ticks)]
    # CPU summed per name (all processes, also small ones) – for presence/commonality analysis
    per_name = {}
    for p in procs:
        per_name[p.name] = per_name.get(p.name, 0.0) + p.ticks
    if full_names:
        names = {k: round(v, 1) for k, v in per_name.items()}
    else:  # Zwischen-Schnappschüsse: nur Prozesse, die gerade CPU brauchen
        names = {k: round(v, 1) for k, v in per_name.items() if v >= 0.1}
    return {"rows": rows, "names": names, "full": 1 if full_names else 0}


# ---------------------------------------------------------------- foreground window

class Foreground:
    """Best effort: X11/Xwayland via xprop or xdotool, KDE Wayland via kdotool."""

    def __init__(self):
        self.xprop = shutil.which("xprop")
        self.xdotool = shutil.which("xdotool")
        self.kdotool = shutil.which("kdotool")

    @staticmethod
    def _run(args, timeout=1.5):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None

    def get(self):
        """Returns dict(pid, name, title, via) or None."""
        res = None
        if os.environ.get("DISPLAY"):
            if self.xprop:
                res = self._xprop()
            if not res and self.xdotool:
                res = self._dotool(self.xdotool)
        if not res and self.kdotool and os.environ.get("WAYLAND_DISPLAY"):
            res = self._dotool(self.kdotool)
            if res:
                res["via"] = "kdotool"
        if res and res.get("pid"):
            comm = read(f"/proc/{res['pid']}/comm")
            res["name"] = comm.strip() if comm else res.get("name")
        return res

    def _xprop(self):
        out = self._run([self.xprop, "-root", "_NET_ACTIVE_WINDOW"])
        if not out:
            return None
        mt = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
        if not mt or int(mt.group(1), 16) == 0:
            return None
        wid = mt.group(1)
        out = self._run([self.xprop, "-id", wid, "_NET_WM_PID", "_NET_WM_NAME", "WM_CLASS"]) or ""
        pid = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", out)
        title = re.search(r'_NET_WM_NAME\(\w+\) = "(.*)"', out)
        cls = re.search(r'WM_CLASS\(\w+\) = "(.*?)"(?:, "(.*?)")?', out)
        return dict(pid=int(pid.group(1)) if pid else None,
                    title=title.group(1) if title else "",
                    name=(cls.group(2) or cls.group(1)) if cls else "",
                    via="x11")

    def _dotool(self, tool):
        wid = (self._run([tool, "getactivewindow"]) or "").strip()
        if not wid:
            return None
        pid = (self._run([tool, "getwindowpid", wid]) or "").strip()
        title = (self._run([tool, "getwindowname", wid]) or "").strip()
        return dict(pid=int(pid) if pid.isdigit() else None, title=title, name="", via="xdotool")


# ---------------------------------------------------------------- GPU

class AmdIntelGpu:
    def __init__(self):
        self.dev = None
        self.kind = None
        best = -1
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            if "-" in os.path.basename(os.path.dirname(card)):
                continue
            driver = os.path.basename(os.path.realpath(os.path.join(card, "driver")))
            vram = read_int(os.path.join(card, "mem_info_vram_total"), 0) or 0
            if driver == "amdgpu" and vram > best:
                best, self.dev, self.kind = vram, card, "amd"
            elif driver in ("i915", "xe") and self.dev is None:
                self.dev, self.kind = card, "intel"
        self.hwmon = None
        if self.dev:
            hw = glob.glob(os.path.join(self.dev, "hwmon", "hwmon*"))
            self.hwmon = hw[0] if hw else None

    def available(self):
        return self.dev is not None

    def sample(self, out, state):
        d = self.dev
        if self.kind == "intel":
            card = os.path.dirname(d)
            f = read_int(os.path.join(card, "gt_act_freq_mhz")) or read_int(os.path.join(card, "gt_cur_freq_mhz"))
            if f is not None:
                out["gpu_clock"] = f
            return
        v = read_int(os.path.join(d, "gpu_busy_percent"))
        if v is not None:
            out["gpu_busy"] = v
        used = read_int(os.path.join(d, "mem_info_vram_used"))
        total = read_int(os.path.join(d, "mem_info_vram_total"))
        if used is not None:
            out["vram_used"] = round(used / 1048576)
            if total:
                out["vram_pct"] = round(used * 100.0 / total, 1)
        gtt = read_int(os.path.join(d, "mem_info_gtt_used"))
        if gtt is not None:
            out["gtt_used"] = round(gtt / 1048576)
        for f, key in (("pp_dpm_sclk", "gpu_clock"), ("pp_dpm_mclk", "gpu_mclk")):
            txt = read(os.path.join(d, f)) or ""
            mt = re.search(r"(\d+)\s*[Mm]hz\s*\*", txt)
            if mt:
                out[key] = int(mt.group(1))
        lvl = read(os.path.join(d, "power_dpm_force_performance_level"))
        if lvl:
            state["gpu_perf_level"] = lvl.strip()
        prof = read(os.path.join(d, "pp_power_profile_mode")) or ""
        mt = re.search(r"^\s*\d+\s+(\S+)\s*\*", prof, re.M)
        if mt:
            state["gpu_power_profile"] = mt.group(1)
        if self.hwmon:
            h = self.hwmon
            for i in range(1, 4):
                lab = (read(os.path.join(h, f"temp{i}_label")) or "").strip()
                t = read_int(os.path.join(h, f"temp{i}_input"))
                if t is None:
                    continue
                if lab in ("edge", "") and "gpu_temp" not in out:
                    out["gpu_temp"] = t / 1000
                elif lab == "junction":
                    out["gpu_temp_hot"] = t / 1000
            p = read_int(os.path.join(h, "power1_average")) or read_int(os.path.join(h, "power1_input"))
            if p is not None:
                out["gpu_power"] = round(p / 1e6, 1)
            f = read_int(os.path.join(h, "freq1_input"))
            if f:
                out["gpu_clock"] = round(f / 1e6)


class NvidiaGpu:
    """Runs one long-lived `nvidia-smi -lms` process and keeps the latest line."""

    FIELDS_NEW = "utilization.gpu,clocks.gr,clocks.mem,temperature.gpu,power.draw,memory.used,memory.total,pstate,clocks_event_reasons.active"
    FIELDS_OLD = "utilization.gpu,clocks.gr,clocks.mem,temperature.gpu,power.draw,memory.used,memory.total,pstate,clocks_throttle_reasons.active"
    FIELDS_MIN = "utilization.gpu,clocks.gr,clocks.mem,temperature.gpu,power.draw,memory.used,memory.total,pstate"

    def __init__(self, interval):
        self.exe = shutil.which("nvidia-smi")
        self.latest = None
        self.interval_ms = int(max(500, interval * 1000))
        self.fields = None
        if self.exe:
            for f in (self.FIELDS_NEW, self.FIELDS_OLD, self.FIELDS_MIN):
                try:
                    r = subprocess.run([self.exe, f"--query-gpu={f}", "--format=csv,noheader,nounits"],
                                       capture_output=True, text=True, timeout=10)
                    if r.returncode == 0 and r.stdout.strip():
                        self.fields = f
                        break
                except Exception:
                    pass
        if self.fields:
            threading.Thread(target=self._loop, daemon=True, name="nvidia-smi").start()

    def available(self):
        return self.fields is not None

    def _loop(self):
        while True:
            try:
                p = subprocess.Popen([self.exe, f"--query-gpu={self.fields}", "--format=csv,noheader,nounits",
                                      f"-lms={self.interval_ms}"], stdout=subprocess.PIPE, text=True)
                for line in p.stdout:
                    self.latest = (time.time(), line.strip().split("\n")[0])
                p.wait()
            except Exception:
                pass
            time.sleep(10)

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except Exception:
            return None

    def sample(self, out, state):
        if not self.latest or time.time() - self.latest[0] > 10:
            return
        parts = [x.strip() for x in self.latest[1].split(",")]
        if len(parts) < 8:
            return
        u, gr, mem, temp, pw, mu, mt, ps = parts[:8]
        for key, v in (("gpu_busy", u), ("gpu_clock", gr), ("gpu_mclk", mem), ("gpu_temp", temp),
                       ("gpu_power", pw), ("vram_used", mu)):
            n = self._num(v)
            if n is not None:
                out[key] = n
        mu_n, mt_n = self._num(mu), self._num(mt)
        if mu_n is not None and mt_n:
            out["vram_pct"] = round(mu_n * 100 / mt_n, 1)
        if ps.startswith("P") and ps[1:].isdigit():
            out["gpu_pstate"] = int(ps[1:])
        if len(parts) >= 9:
            try:
                reasons = int(parts[8], 16)
                # ignore "GPU idle" (0x1) and "applications clocks setting" (0x2)
                out["gpu_throttle"] = 1 if reasons & ~0x3 else 0
                if reasons & ~0x3:
                    state["gpu_throttle_reasons"] = parts[8]
            except Exception:
                pass


# ---------------------------------------------------------------- kernel log

class KernelLog:
    """Follows `journalctl -k`; falls back to `dmesg -w`. Lines are pushed into a list."""

    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()
        self.ok = None
        threading.Thread(target=self._loop, daemon=True, name="kernel-log").start()

    def _loop(self):
        cmds = []
        if shutil.which("journalctl"):
            cmds.append(["journalctl", "-k", "-f", "-n", "0", "-o", "cat", "--no-pager"])
        if shutil.which("dmesg"):
            cmds.append(["dmesg", "-w", "-x", "--level=emerg,alert,crit,err,warn,notice"])
        while True:
            for cmd in cmds:
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    count, window = 0, time.time()
                    spawned = time.time()
                    for line in p.stdout:
                        self.ok = True
                        line = line.strip()
                        now = time.time()
                        # dmesg -w gibt zuerst den alten Puffer aus – den überspringen
                        if not line or (cmd[0] == "dmesg" and now - spawned < 2):
                            continue
                        if now - window > 60:
                            count, window = 0, now
                        count += 1
                        if count > 120:  # Flut begrenzen
                            continue
                        with self.lock:
                            self.lines.append((now, line))
                    p.wait()
                except Exception:
                    pass
            self.ok = False
            time.sleep(60)

    def drain(self):
        with self.lock:
            out, self.lines = self.lines, []
        return out


# ---------------------------------------------------------------- system sampler

def _find_cpu_temp_file():
    """Pick the CPU package temperature sensor once (Tctl/Tdie on AMD, Package on Intel)."""
    fallback = None
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = (read(os.path.join(h, "name")) or "").strip()
        if name not in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "k8temp"):
            continue
        for t in sorted(glob.glob(os.path.join(h, "temp*_input"))):
            lab = (read(t.replace("_input", "_label")) or "").strip()
            if lab.startswith(("Tctl", "Tdie", "Package")):
                return t
            fallback = fallback or t
    return fallback


def _whole_disks():
    try:
        return {d for d in os.listdir("/sys/block") if not d.startswith(("loop", "ram", "zram", "dm-", "sr", "md"))}
    except Exception:
        return set()


class SystemSampler:
    def __init__(self, cfg):
        self.cfg = cfg
        self.procs = ProcessTracker(cfg["game_patterns"])
        self.fg = Foreground()
        self.amd = AmdIntelGpu()
        self.nv = NvidiaGpu(cfg["interval"])
        self.prev = {}
        self.prev_t = None
        self.disks = _whole_disks()
        self.cpu_freq_files = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))
        self.throttle_files = glob.glob("/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/*throttle_count")
        self.cpu_temp_file = _find_cpu_temp_file()
        self.n = 0
        self.last_fg = None

    def _rate(self, key, value, dt, out, outkey, scale=1.0):
        prev = self.prev.get(key)
        self.prev[key] = value
        if prev is not None and dt and value >= prev:
            out[outkey] = round((value - prev) / dt * scale, 2)

    def sample(self):
        """Returns (metrics dict, state dict of strings, process list, ready flag)."""
        now = time.monotonic()
        dt = (now - self.prev_t) if self.prev_t else None
        self.prev_t = now
        out, state = {}, {}

        # CPU
        stat = read("/proc/stat") or ""
        cores = []
        for line in stat.splitlines():
            if line.startswith("cpu"):
                f = line.split()
                vals = list(map(int, f[1:9]))
                total = sum(vals)
                idle = vals[3] + vals[4]
                key = "stat_" + f[0]
                prev = self.prev.get(key)
                self.prev[key] = (total, idle, vals[4], vals[7])
                if prev and total > prev[0]:
                    dtot = total - prev[0]
                    busy = 100.0 * (1 - (idle - prev[1]) / dtot)
                    if f[0] == "cpu":
                        out["cpu_total"] = round(busy, 1)
                        out["cpu_iowait"] = round(100.0 * (vals[4] - prev[2]) / dtot, 1)
                        out["cpu_steal"] = round(100.0 * (vals[7] - prev[3]) / dtot, 1)
                    else:
                        cores.append(busy)
            elif line.startswith("ctxt"):
                self._rate("ctxt", int(line.split()[1]), dt, out, "ctxt_s")
            elif line.startswith("procs_running"):
                out["procs_running"] = int(line.split()[1])
            elif line.startswith("procs_blocked"):
                out["procs_blocked"] = int(line.split()[1])
        if cores:
            out["cpu_max_core"] = round(max(cores), 1)
        try:
            out["load1"] = float(read("/proc/loadavg").split()[0])
        except Exception:
            pass

        freqs = [read_int(f) for f in self.cpu_freq_files]
        freqs = [f / 1000 for f in freqs if f]
        if freqs:
            out["cpu_freq_avg"] = round(sum(freqs) / len(freqs))
            out["cpu_freq_max"] = round(max(freqs))
        gov = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if gov:
            state["cpu_governor"] = gov.strip()
        epp = read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
        if epp:
            state["cpu_epp"] = epp.strip()
        pp = read("/sys/firmware/acpi/platform_profile")
        if pp:
            state["platform_profile"] = pp.strip()
        thp = read("/sys/kernel/mm/transparent_hugepage/enabled")
        if thp:
            mt = re.search(r"\[(\w+)\]", thp)
            if mt:
                state["thp"] = mt.group(1)
        if self.throttle_files:
            self._rate("throttle", sum(read_int(f, 0) for f in self.throttle_files), dt, out, "cpu_throttle_s")
        if self.cpu_temp_file:
            t = read_int(self.cpu_temp_file)
            if t is not None:
                out["cpu_temp"] = t / 1000

        # Memory
        mi = {}
        for line in (read("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            try:
                mi[k] = int(v.split()[0]) / 1024
            except Exception:
                pass
        if mi:
            out["mem_used"] = round(mi.get("MemTotal", 0) - mi.get("MemAvailable", 0))
            out["mem_avail"] = round(mi.get("MemAvailable", 0))
            out["swap_used"] = round(mi.get("SwapTotal", 0) - mi.get("SwapFree", 0))
            out["mem_dirty"] = round(mi.get("Dirty", 0))
            out["anon_huge"] = round(mi.get("AnonHugePages", 0))

        vm = {}
        for line in (read("/proc/vmstat") or "").splitlines():
            k, _, v = line.partition(" ")
            vm[k] = v
        for k, outk in (("pgmajfault", "pgmajfault_s"), ("pswpin", "pswpin_s"), ("pswpout", "pswpout_s"),
                        ("compact_stall", "compact_stall_s"), ("thp_fault_fallback", "thp_fallback_s")):
            if k in vm:
                self._rate("vm_" + k, int(vm[k]), dt, out, outk)
        direct = sum(int(v) for k, v in vm.items() if k.startswith("pgscan_direct"))
        self._rate("vm_pgscan_direct", direct, dt, out, "pgscan_direct_s")

        # Pressure stall information
        for res, key in (("cpu", "psi_cpu"), ("memory", "psi_mem"), ("io", "psi_io")):
            txt = read(f"/proc/pressure/{res}")
            if not txt:
                continue
            for line in txt.splitlines():
                mt = re.search(r"avg10=([\d.]+)", line)
                if not mt:
                    continue
                if line.startswith("some"):
                    out[key] = float(mt.group(1))
                elif line.startswith("full") and res == "memory":
                    out["psi_mem_full"] = float(mt.group(1))

        # Disks
        rd = wr = busy_ms = 0
        for line in (read("/proc/diskstats") or "").splitlines():
            f = line.split()
            if len(f) > 12 and f[2] in self.disks:
                rd += int(f[5])
                wr += int(f[9])
                busy_ms = max(busy_ms, int(f[12]))
        self._rate("disk_rd", rd, dt, out, "disk_read", 512 / 1048576)
        self._rate("disk_wr", wr, dt, out, "disk_write", 512 / 1048576)
        self._rate("disk_busy", busy_ms, dt, out, "disk_busy", 0.1)
        if "disk_busy" in out:
            out["disk_busy"] = min(100.0, out["disk_busy"])

        # Network
        rx = tx = 0
        for line in (read("/proc/net/dev") or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr")):
                continue
            f = rest.split()
            if len(f) >= 9:
                rx += int(f[0])
                tx += int(f[8])
        self._rate("net_rx", rx, dt, out, "net_rx", 1 / 1024)
        self._rate("net_tx", tx, dt, out, "net_tx", 1 / 1024)

        # GPU
        if self.amd.available():
            self.amd.sample(out, state)
        if self.nv.available():
            self.nv.sample(out, state)

        # Processes
        procs, ready = self.procs.scan()
        out["nprocs"] = len(procs)
        out["nthreads"] = sum(p.threads for p in procs)
        game = [p for p in procs if p.is_game]
        out["game_running"] = 1 if game else 0
        if game:
            out["game_cpu"] = round(sum(p.ticks for p in game), 1)
            out["game_rss"] = round(sum(p.rss for p in game))
            out["game_threads"] = sum(p.threads for p in game)
        out["wine_cpu"] = round(sum(p.ticks for p in procs if p.name == "wineserver"), 1)
        out["shader_cpu"] = round(sum(p.ticks for p in procs if p.name.startswith("fossilize")), 1)
        out["uptime_h"] = round(uptime_s() / 3600, 3)
        me = [p for p in procs if p.pid == MY_PID]
        if me:
            out["self_cpu"] = round(me[0].ticks, 2)
            out["self_rss"] = round(me[0].rss)

        # Foreground window (only every 2nd sample – spawning xprop is the most expensive part)
        self.n += 1
        if self.n % 2 == 1 or self.last_fg is None:
            self.last_fg = self.fg.get() or {}
        fg = self.last_fg
        if fg:
            fgname = fg.get("name") or ""
            state["foreground"] = fgname
            gp = {p.pid for p in game}
            out["fg_is_game"] = 1 if (fg.get("pid") in gp or self.procs.is_game(fgname, fg.get("title", ""))
                                       or any(pt in (fg.get("title") or "").lower() for pt in self.cfg["game_patterns"])) else 0

        return out, state, procs, (ready and dt is not None), game
