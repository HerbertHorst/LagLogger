"""SQLite storage. Samples are stored as zlib-compressed JSON to keep the file small."""
import json
import os
import sqlite3
import time
import zlib

from . import config

SCHEMA = """
-- samples/procs: je Zeile eine Minute (ts = Minutenbeginn), data = [[ts, {...}], ...] – komprimiert viel besser
CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data BLOB NOT NULL);
-- Minuten-Zusammenfassung {key: [mittel, min, max]} für schnelle Übersichten über lange Zeiträume
CREATE TABLE IF NOT EXISTS minutes (ts INTEGER PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS procs (ts REAL PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,           -- lag | good | end | auto
    note TEXT,
    fg_pid INTEGER, fg_name TEXT, fg_title TEXT,
    snapshot BLOB
);
CREATE INDEX IF NOT EXISTS markers_ts ON markers(ts);
CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start REAL NOT NULL, end REAL, name TEXT, boot_id TEXT
);
CREATE TABLE IF NOT EXISTS fps (ts INTEGER PRIMARY KEY, fps REAL, ft_max REAL, ft_avg REAL);
CREATE TABLE IF NOT EXISTS imported (path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path=None):
    path = path or config.DB_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.execute("PRAGMA auto_vacuum=INCREMENTAL")  # wirkt nur bei neuen Datenbanken
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def connect_read(path=None):
    """Light connection for the web server: no schema/pragma changes (those would wait for the logger's lock)."""
    path = path or config.DB_FILE
    if not os.path.exists(path):
        return connect(path)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    return con


def pack(obj):
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode(), 6)


def unpack(blob):
    if blob is None:
        return None
    return json.loads(zlib.decompress(blob))


def add_event(con, kind, text, ts=None):
    con.execute("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (ts or time.time(), kind, text[:2000]))


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))


def write_chunk(con, table, items):
    """items: list of (ts, dict) of one minute."""
    if items:
        con.execute(f"INSERT OR REPLACE INTO {table}(ts, data) VALUES (?,?)",
                    (int(items[0][0] // 60) * 60, pack([[round(t, 2), d] for t, d in items])))


def _load_chunks(con, table, t0, t1):
    rows = con.execute(f"SELECT data FROM {table} WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0 - 60, t1)).fetchall()
    out = []
    for (blob,) in rows:
        for ts, d in unpack(blob):
            if t0 <= ts <= t1:
                out.append((ts, d))
    return out


def last_chunk_item(con, table):
    row = con.execute(f"SELECT data FROM {table} ORDER BY ts DESC LIMIT 1").fetchone()
    if not row:
        return None, None
    items = unpack(row[0])
    return items[-1][0], items[-1][1]


def load_samples(con, t0, t1):
    return _load_chunks(con, "samples", t0, t1)


def load_minutes(con, t0, t1):
    rows = con.execute("SELECT ts, data FROM minutes WHERE ts BETWEEN ? AND ? ORDER BY ts", (int(t0) - 60, int(t1))).fetchall()
    return [(ts, unpack(d)) for ts, d in rows]


def load_procs(con, t0, t1):
    return _load_chunks(con, "procs", t0, t1)


def load_fps(con, t0, t1):
    return con.execute("SELECT ts, fps, ft_max, ft_avg FROM fps WHERE ts BETWEEN ? AND ? ORDER BY ts",
                       (int(t0), int(t1) + 1)).fetchall()


def sessions(con, t0=0, t1=1e12):
    rows = con.execute(
        "SELECT id, start, end, name, boot_id FROM sessions WHERE start <= ? AND (end IS NULL OR end >= ?) ORDER BY start",
        (t1, t0)).fetchall()
    return [dict(id=r[0], start=r[1], end=r[2], name=r[3], boot_id=r[4]) for r in rows]


def markers(con, t0=0, t1=1e12, with_snapshot=False):
    rows = con.execute(
        "SELECT id, ts, kind, note, fg_pid, fg_name, fg_title, snapshot FROM markers WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (t0, t1)).fetchall()
    out = []
    for r in rows:
        m = dict(id=r[0], ts=r[1], kind=r[2], note=r[3], fg_pid=r[4], fg_name=r[5], fg_title=r[6])
        if with_snapshot:
            m["snapshot"] = unpack(r[7])
        out.append(m)
    return out


def events(con, t0, t1, limit=5000):
    rows = con.execute("SELECT ts, kind, text FROM events WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT ?",
                       (t0, t1, limit)).fetchall()
    return [dict(ts=r[0], kind=r[1], text=r[2]) for r in rows]


def cleanup(con, retention_days):
    cut = time.time() - retention_days * 86400
    for table in ("samples", "procs", "events"):
        con.execute(f"DELETE FROM {table} WHERE ts < ?", (cut,))
    con.execute("PRAGMA incremental_vacuum")
    con.execute("DELETE FROM fps WHERE ts < ?", (int(cut),))
    con.execute("DELETE FROM minutes WHERE ts < ?", (int(cut),))
    con.execute("DELETE FROM sessions WHERE end IS NOT NULL AND end < ?", (cut,))
    # Marker bleiben länger erhalten (klein, aber ohne Messdaten nur noch begrenzt nützlich)
    con.execute("DELETE FROM markers WHERE ts < ?", (cut - 60 * 86400,))
    con.commit()
