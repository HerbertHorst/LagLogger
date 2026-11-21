"""Parts shared by the Linux and Windows collectors."""
import shutil
import subprocess
import threading
import time

from .osutil import popen_kwargs


class ProcInfo:
    __slots__ = ("pid", "name", "cmd", "ticks", "threads", "rss", "state", "start", "is_game")


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
                                       capture_output=True, text=True, timeout=10, **popen_kwargs())
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
                                      f"-lms={self.interval_ms}"], stdout=subprocess.PIPE, text=True,
                                     stderr=subprocess.DEVNULL, **popen_kwargs())
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


class FrameClock:
    """Assigns each frame of a buffered frame log (PresentMon) to the second in which it was rendered.

    PresentMon writes its CSV in bursts every few seconds. Taking the arrival time would put a whole burst into
    one second and leave the seconds in between empty (gaps in the FPS chart). Instead: anchor each swapchain to
    the wall clock once and then follow PresentMon's own time (TimeInSeconds) or the sum of the frame times.
    """

    def __init__(self, max_drift=30.0):
        self.anchor = {}
        self.max_drift = max_drift

    def stamp(self, swapchain, ft_ms, t=None, now=None):
        now = time.time() if now is None else now
        a = self.anchor.get(swapchain)
        if a is None:
            a = self.anchor[swapchain] = {"wall": now, "t0": t, "cum": 0.0}
        else:
            a["cum"] += ft_ms / 1000.0
        if t is not None and a["t0"] is not None:
            ts = a["wall"] + (t - a["t0"])
        else:
            ts = a["wall"] + a["cum"]
        if abs(ts - now) > self.max_drift:  # Uhr gesprungen oder sehr lange Pause: neu verankern
            self.anchor[swapchain] = {"wall": now, "t0": t, "cum": 0.0}
            ts = now
        return ts

