"""SQLite storage. Compatible with the mastersportal.sqlite file made by the CLI script."""
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rec (kind TEXT, id TEXT, country TEXT, json TEXT, PRIMARY KEY(kind, id));
CREATE TABLE IF NOT EXISTS done (kind TEXT, part TEXT, PRIMARY KEY(kind, part));
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
"""


def db_path():
    return os.environ.get("MP_DB", "mastersportal.sqlite")


def connect(path=None):
    c = sqlite3.connect(path or db_path(), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init(path=None):
    c = connect(path)
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(rec)")}
    for col in ("first_seen", "last_seen"):
        if col not in cols:  # files from the CLI script lack these
            c.execute(f"ALTER TABLE rec ADD COLUMN {col} REAL")
    # A restart kills any running job thread; mark those rows so they can be resumed.
    c.execute("UPDATE jobs SET status='interrupted', finished=?, message='Server restarted; start again to resume' "
              "WHERE status IN ('running', 'stopping')", (time.time(),))
    c.commit()
    c.close()


def log(c, job_id, line):
    c.execute("INSERT INTO log(job_id, ts, line) VALUES (?,?,?)", (job_id, time.time(), line))
    c.commit()
