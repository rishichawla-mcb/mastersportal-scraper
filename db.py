"""SQLite storage. Opens the mastersportal.sqlite made by the CLI script as is and migrates it additively."""
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rec (kind TEXT, id TEXT, country TEXT, json TEXT, PRIMARY KEY(kind, id));
CREATE TABLE IF NOT EXISTS done (kind TEXT, part TEXT, PRIMARY KEY(kind, part));   -- legacy (CLI script)
-- One row per section = both the work queue and the result (KT 2).
CREATE TABLE IF NOT EXISTS parts (
    kind TEXT, part TEXT, cc TEXT, depth INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued' CHECK (status IN ('queued', 'short', 'split', 'done')),
    listed INTEGER, uniq INTEGER DEFAULT 0, next_offset INTEGER DEFAULT 0, passes INTEGER DEFAULT 0,
    note TEXT, updated REAL,
    PRIMARY KEY (kind, part)
);
CREATE TABLE IF NOT EXISTS part_ids (kind TEXT, part TEXT, id TEXT, PRIMARY KEY (kind, part, id));
CREATE TABLE IF NOT EXISTS listed (kind TEXT, cc TEXT, n INTEGER, ts REAL, PRIMARY KEY (kind, cc));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY, kind TEXT, rec_id TEXT, field TEXT, old TEXT, new TEXT, ts REAL, job_id INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pw TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    created REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT, status TEXT, delay REAL, fresh INTEGER DEFAULT 0,
    started REAL, finished REAL,
    requests INTEGER DEFAULT 0, new INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
    parts_done INTEGER DEFAULT 0, parts_total INTEGER DEFAULT 0,
    message TEXT, started_by TEXT
);
CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, job_id INTEGER, ts REAL, line TEXT);
CREATE INDEX IF NOT EXISTS log_job ON log(job_id, id);
CREATE INDEX IF NOT EXISTS rec_country ON rec(kind, country);
CREATE INDEX IF NOT EXISTS changes_ts ON changes(ts);
CREATE INDEX IF NOT EXISTS parts_status ON parts(kind, status);
"""

# Additive migrations: table -> {column: type}. Never dropped, never renamed.
ADD_COLUMNS = {
    "rec": {"first_seen": "REAL", "last_seen": "REAL", "last_changed": "REAL", "content_hash": "TEXT"},
    "jobs": {"changed": "INTEGER DEFAULT 0", "unchanged": "INTEGER DEFAULT 0", "auto_resume": "INTEGER DEFAULT 0",
             "resumes": "INTEGER DEFAULT 0", "failures": "INTEGER DEFAULT 0"},
}

LIVE_STATUSES = ("running", "stopping", "cooling")


def db_path():
    return os.environ.get("MP_DB", "mastersportal.sqlite")


def connect(path=None):
    c = sqlite3.connect(path or db_path(), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init(path=None):
    c = connect(path)
    c.executescript(SCHEMA)
    for table, cols in ADD_COLUMNS.items():
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # Sections finished by the CLI script count as done here too.
    c.execute("INSERT OR IGNORE INTO parts(kind, part, cc, status, note, updated) "
              "SELECT kind, part, json_extract(part, '$[0].values[0]'), 'done', 'imported from CLI script', ? FROM done",
              (time.time(),))
    # A restart kills any running job thread; mark those rows so they can be resumed (KT G4).
    c.execute(f"UPDATE jobs SET status='interrupted', finished=?, message='Server restarted; start again to resume' "
              f"WHERE status IN ({','.join('?' * len(LIVE_STATUSES))})", (time.time(), *LIVE_STATUSES))
    c.commit()
    c.close()


def log(c, job_id, line):
    c.execute("INSERT INTO log(job_id, ts, line) VALUES (?,?,?)", (job_id, time.time(), line))
    c.commit()


def meta_get(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
