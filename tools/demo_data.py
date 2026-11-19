#!/usr/bin/env python3
"""Creates a demo database with 4 days of fake data, to try the web UI without playing.

    LAGSCOPE_DATA=/tmp/lagscope-demo python3 tools/demo_data.py
    LAGSCOPE_DATA=/tmp/lagscope-demo python3 lagscope.py serve

Scenario: VRAM slowly fills up during long sessions; once it is nearly full the FPS collapse.
Sometimes Steam's shader compiler runs in the background at the same time.
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lagscope import config, db  # noqa: E402
from lagscope.daemon import MinuteRollup  # noqa: E402

random.seed(4)
INTERVAL = 2.0
con = db.connect()
for t in ("samples", "procs", "markers", "events", "sessions", "fps", "minutes"):
    con.execute(f"DELETE FROM {t}")

now = time.time()
day0 = now - 4 * 86400
rollup = MinuteRollup()
BASE_PROCS = {"kwin_wayland": 4, "plasmashell": 1.5, "Xwayland": 3, "pipewire": 0.8, "steam": 1.2,
              "steamwebhelper": 2.0, "Discord": 1.5, "firefox": 0.8, "systemd": 0.1, "dbus-broker": 0.2}

boot_t = day0
sessions = [  # (day offset hours, duration min, lag after min or None, shader during lag)
    (19.0, 95, 48, True), (21.0, 40, None, False),
    (24 + 18.5, 120, 62, False), (24 + 21.2, 30, None, False),
    (48 + 20.0, 80, 35, True),
    (72 + 17.0, 50, None, False), (72 + 19.0, 110, 55, True),
]


def sample_at(ts, in_game, gmin, lagging, shader, vram_frac):
    jitter = random.gauss
    d = {
        "cpu_total": max(1, (38 if in_game else 6) + (12 if shader else 0) + jitter(0, 4)),
        "cpu_max_core": min(100, (78 if in_game else 20) + (20 if shader else 0) + jitter(0, 6)),
        "cpu_iowait": abs(jitter(0.3, 0.3)), "procs_running": max(1, int(jitter(4 if in_game else 1, 1))),
        "procs_blocked": 0, "load1": 3.2 if in_game else 0.7,
        "cpu_freq_avg": 4300 + jitter(0, 80) if in_game else 2900 + jitter(0, 200),
        "cpu_freq_max": 5100 + jitter(0, 30), "cpu_temp": (72 if in_game else 48) + jitter(0, 1.5),
        "gpu_busy": (95 if in_game and not lagging else 55 if lagging else 3) + jitter(0, 3),
        "gpu_clock": (2450 if in_game and not lagging else 1900 if lagging else 500) + jitter(0, 40),
        "gpu_mclk": 1250 if in_game else 96, "gpu_temp": (71 if in_game else 42) + jitter(0, 1),
        "gpu_temp_hot": (88 if in_game else 45) + jitter(0, 1.5),
        "gpu_power": (245 if in_game and not lagging else 160 if lagging else 18) + jitter(0, 6),
        "vram_used": round(16368 * vram_frac), "vram_pct": round(vram_frac * 100, 1),
        "gtt_used": 250 + (1800 if lagging else 0) + jitter(0, 20),
        "mem_used": 9800 + (gmin * 15 if in_game else 0) + jitter(0, 50),
        "mem_avail": 22000 - (gmin * 15 if in_game else 0), "swap_used": 0,
        "psi_cpu": abs(jitter(1 + (6 if shader else 0), 0.5)), "psi_mem": 0.0, "psi_mem_full": 0.0,
        "psi_io": abs(jitter(0.2, 0.2)), "pgmajfault_s": abs(jitter(2, 2)), "pswpin_s": 0, "pswpout_s": 0,
        "compact_stall_s": 0, "pgscan_direct_s": 0, "thp_fallback_s": 0,
        "disk_read": abs(jitter(0.5 + (8 if shader else 0), 0.5)), "disk_write": abs(jitter(0.4, 0.3)),
        "disk_busy": abs(jitter(1, 1)), "net_rx": abs(jitter(60 if in_game else 5, 10)),
        "net_tx": abs(jitter(25 if in_game else 2, 5)),
        "game_running": 1 if in_game else 0, "wine_cpu": 3 + jitter(0, 0.5) if in_game else 0,
        "shader_cpu": 180 + jitter(0, 20) if shader else 0,
        "nprocs": 412, "nthreads": 2100, "self_cpu": 0.12, "self_rss": 27,
        "uptime_h": round((ts - boot_t) / 3600, 3),
        "_st": {"cpu_governor": "powersave", "cpu_epp": "balance_performance", "thp": "madvise",
                "foreground": "Overwatch.exe" if in_game else "firefox", "gpu_perf_level": "auto"},
    }
    if in_game:
        d.update(game_cpu=310 + jitter(0, 25) - (80 if lagging else 0), game_rss=6200 + gmin * 12,
                 game_threads=96, game_min=round(gmin, 2), fg_is_game=1)
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}


def procs_snapshot(in_game, shader, lagging):
    names = dict(BASE_PROCS)
    if in_game:
        names.update({"Overwatch.exe": 310 - (80 if lagging else 0), "wineserver": 3, "pressure-vessel": 0.1})
    if shader:
        names["fossilize_replay"] = 180
    names = {k: round(max(0, v + random.gauss(0, v * 0.1)), 1) for k, v in names.items()}
    rows = [[1000 + i, n, c, 300, 10, "S", 1 if n == "Overwatch.exe" else 0, n] for i, (n, c) in
            enumerate(sorted(names.items(), key=lambda x: -x[1]))]
    return {"rows": rows[:30], "names": names}


t = day0 + 17 * 3600
end_t = now
sess_list = [(day0 + h * 3600, dur * 60, lag, sh) for h, dur, lag, sh in sessions]
n = 0
batch_s, batch_p, batch_f = [], [], []
cur_session = None
while t < end_t:
    in_game, gmin, lagging, shader, vram = False, 0, False, False, 0.08
    for (st, dur, lag, sh) in sess_list:
        if st <= t < st + dur:
            in_game = True
            gmin = (t - st) / 60
            vram = min(0.995, 0.55 + 0.0085 * gmin * (1.0 if lag else 0.4))
            lagging = lag is not None and gmin >= lag
            shader = sh and lag is not None and lag - 3 <= gmin <= lag + 8
            if cur_session is None or cur_session[0] != st:
                cur = con.execute("INSERT INTO sessions(start, end, name, boot_id) VALUES (?,?,?,?)",
                                  (st, st + dur, "Overwatch.exe", "demo"))
                cur_session = (st, cur.lastrowid)
                db.add_event(con, "game", "Spiel gestartet: Overwatch.exe", st)
                db.add_event(con, "game", f"Spiel beendet nach {dur / 60:.0f} min", st + dur)
            if lag is not None and abs(gmin - lag) < INTERVAL / 120:
                if sh:
                    db.add_event(con, "proc_start", "fossilize_replay", t - 60)
                db.add_event(con, "kernel", "[drm] evicting device resources from VRAM (demo)", t + 5)
            if lag is not None and abs(gmin - (lag + 1.5)) < INTERVAL / 120:
                con.execute("INSERT INTO markers(ts, kind, fg_pid, fg_name, fg_title, snapshot) VALUES (?,?,?,?,?,?)",
                            (t, "lag", 4242, "Overwatch.exe", "Overwatch", db.pack(procs_snapshot(True, shader, True))))
            if lag is None and abs(gmin - dur / 120) < INTERVAL / 120:
                con.execute("INSERT INTO markers(ts, kind, fg_name, fg_title) VALUES (?,?,?,?)",
                            (t, "good", "Overwatch.exe", "Overwatch"))
            break
    # PC ist nachts aus
    hour = time.localtime(t).tm_hour
    if 1 <= hour < 16 and not in_game:
        t += 600
        continue
    s = sample_at(t, in_game, gmin, lagging, shader, vram)
    if batch_s and int(batch_s[0][0] // 60) != int(t // 60):
        db.write_chunk(con, "samples", batch_s)
        db.write_chunk(con, "procs", batch_p)
        batch_s, batch_p = [], []
    batch_s.append((t, s))
    fl = rollup.add(t, s)
    if fl:
        con.execute("INSERT OR REPLACE INTO minutes(ts, data) VALUES (?,?)", (fl[0], db.pack(fl[1])))
    if n % 3 == 0:
        batch_p.append((t, procs_snapshot(in_game, shader, lagging)))
    if in_game:
        for sec in (int(t), int(t) + 1):
            base = 150 if not lagging else 42
            f = max(5, random.gauss(base, base * 0.06))
            batch_f.append((sec, round(f, 1), round(1000 / f * random.uniform(1.2, 2.5), 2), round(1000 / f, 2)))
    n += 1
    t += INTERVAL
db.write_chunk(con, "samples", batch_s)
db.write_chunk(con, "procs", batch_p)
con.executemany("INSERT OR REPLACE INTO fps VALUES (?,?,?,?)", batch_f)
db.set_meta(con, "capabilities", "GPU: AMD | Vordergrund-Erkennung: xprop | Kernel-Log: an (DEMO-DATEN)")
con.commit()
print(f"Demo-Daten geschrieben: {n} Messpunkte in {config.DB_FILE}")
