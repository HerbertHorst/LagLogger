"""Import MangoHud CSV frame logs and store them as per-second FPS values."""
import csv
import glob
import os
import re
import time

FNAME_TS = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")


def parse(path):
    """Returns list of (unix_second, fps_avg, frametime_max_ms, frametime_avg_ms)."""
    with open(path, newline="", errors="replace") as f:
        lines = f.read().splitlines()
    hdr_i = None
    for i, line in enumerate(lines):
        if line.lower().startswith("fps,"):
            hdr_i = i
            break
    if hdr_i is None:
        return []
    header = [h.strip().lower() for h in lines[hdr_i].split(",")]
    try:
        i_fps = header.index("fps")
        i_ft = header.index("frametime")
        i_el = header.index("elapsed")
    except ValueError:
        return []
    rows = []
    for r in csv.reader(lines[hdr_i + 1:]):
        try:
            rows.append((float(r[i_fps]), float(r[i_ft]), float(r[i_el])))
        except (ValueError, IndexError):
            continue
    if len(rows) < 2:
        return []

    # Unit of "elapsed": usually nanoseconds; detect by comparing with the summed frame times.
    span = rows[-1][2] - rows[0][2]
    if span <= 0:
        return []
    ft_sum_ms = sum(r[1] for r in rows)
    ratio = span / max(ft_sum_ms, 1e-9)  # ns -> ~1e6, µs -> ~1e3, ms -> ~1
    if ratio > 3e4:
        to_s = 1e-9
    elif ratio > 30:
        to_s = 1e-6
    else:
        to_s = 1e-3
    duration = span * to_s

    mt = FNAME_TS.search(os.path.basename(path))
    if mt:
        start = time.mktime(time.strptime(mt.group(1), "%Y-%m-%d_%H-%M-%S"))
    else:
        start = os.path.getmtime(path) - duration
    t0 = rows[0][2]

    buckets = {}
    for fps, ft, el in rows:
        sec = int(start + (el - t0) * to_s)
        b = buckets.setdefault(sec, [0.0, 0, 0.0, 0.0])
        b[0] += fps
        b[1] += 1
        b[2] = max(b[2], ft)
        b[3] += ft
    return [(s, round(b[0] / b[1], 1), round(b[2], 2), round(b[3] / b[1], 2)) for s, b in sorted(buckets.items())]


def import_dir(con, folder, log=print):
    """Import new/changed CSVs from folder (and subfolders). Returns number of imported files."""
    if not folder or not os.path.isdir(folder):
        return 0
    n = 0
    now = time.time()
    for path in glob.glob(os.path.join(folder, "**", "*.csv"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if now - st.st_mtime < 5:  # wird gerade geschrieben
            continue
        row = con.execute("SELECT mtime, size FROM imported WHERE path=?", (path,)).fetchone()
        if row and row[0] == st.st_mtime and row[1] == st.st_size:
            continue
        n += import_file(con, path, log)
        con.execute("INSERT OR REPLACE INTO imported(path, mtime, size) VALUES (?,?,?)",
                    (path, st.st_mtime, st.st_size))
        con.commit()
    return n


def import_file(con, path, log=print):
    try:
        data = parse(path)
    except Exception as e:
        log(f"MangoHud-Import fehlgeschlagen ({path}): {e}")
        return 0
    if not data:
        return 0
    con.executemany("INSERT OR REPLACE INTO fps(ts, fps, ft_max, ft_avg) VALUES (?,?,?,?)", data)
    con.commit()
    log(f"MangoHud-Log importiert: {os.path.basename(path)} ({len(data)} s)")
    return 1
