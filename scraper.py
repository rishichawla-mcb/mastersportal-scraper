"""Background scrape runner: one job at a time, resumable, stops itself when the site blocks it."""
import json
import os
import threading
import time
import traceback

import requests

import db as dbm

API = os.environ.get("MP_API", "https://omnisearch.mastersportal.com/api/search/")
USER_AGENT = os.environ.get("MP_USER_AGENT", "Mozilla/5.0 (research crawler)")
SPLIT_ABOVE = int(os.environ.get("MP_SPLIT_ABOVE", "1500"))
BACKOFF = float(os.environ.get("MP_BACKOFF", "10"))  # seconds, doubled per retry
RETRIES = 6
PAGE = 20
PORTAL_MAP = {
    "master": "https://www.mastersportal.com",
    "bachelor": "https://www.bachelorsportal.com",
    "phd": "https://www.phdportal.com",
    "short": "https://www.shortcoursesportal.com",
    "distance_learning": "https://www.distancelearningportal.com",
}
LEVEL = {"type": "lv", "isIndexable": True, "values": ["master"]}
ENDPOINT = {"orgs": "organisations", "progs": "programmes"}
KINDS = {"orgs": ["orgs"], "progs": ["progs"], "both": ["orgs", "progs"]}


class Stopped(Exception):
    pass


class Blocked(Exception):
    pass


def flt(t, v):
    return {"type": t, "isIndexable": True, "values": [v]}


class Runner:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "content-type": "application/json",  # required, otherwise the API answers 503
            "accept": "application/json",
            "user-agent": USER_AGENT,
        })
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.job_id = None

    # ---- control -------------------------------------------------------
    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, kind, delay, fresh=False, user=None):
        if kind not in KINDS:
            raise ValueError("unknown kind")
        delay = max(1.0, min(30.0, float(delay)))
        with self._lock:
            if self.is_running():
                raise RuntimeError("A job is already running.")
            self._stop.clear()
            c = dbm.connect()
            cur = c.execute(
                "INSERT INTO jobs(kind, status, delay, fresh, started, started_by, message) VALUES (?,?,?,?,?,?,?)",
                (kind, "running", delay, int(bool(fresh)), time.time(), user, "Starting"))
            c.commit()
            c.close()
            self.job_id = cur.lastrowid
            self._thread = threading.Thread(target=self._run, args=(self.job_id, kind, delay, fresh), daemon=True)
            self._thread.start()
            return self.job_id

    def stop(self):
        if self.is_running():
            self._stop.set()
            c = dbm.connect()
            c.execute("UPDATE jobs SET status='stopping', message='Stopping after the current request' "
                      "WHERE id=? AND status='running'", (self.job_id,))
            c.commit()
            c.close()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # ---- work ----------------------------------------------------------
    def _run(self, job_id, kind, delay, fresh):
        c = dbm.connect()
        stats = {"requests": 0, "new": 0, "updated": 0}

        def upd(**kw):
            c.execute("UPDATE jobs SET " + ", ".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), job_id))
            c.commit()

        def say(line):
            dbm.log(c, job_id, line)

        try:
            kinds = KINDS[kind]
            if fresh:
                for k in kinds:
                    c.execute("DELETE FROM done WHERE kind=?", (k,))
                c.commit()
                say("Fresh run: all sections will be fetched again; existing records are updated, not deleted.")
            plans = {k: self._partitions(k, delay, stats, say) for k in kinds}
            upd(parts_total=sum(len(p) for p in plans.values()))
            n = 0
            for k in kinds:
                for cc, filters in plans[k]:
                    key = json.dumps(filters, sort_keys=True)
                    if not c.execute("SELECT 1 FROM done WHERE kind=? AND part=?", (k, key)).fetchone():
                        self._harvest(c, k, cc, filters, delay, stats, say)
                        c.execute("INSERT OR IGNORE INTO done VALUES (?,?)", (k, key))
                        c.commit()
                    n += 1
                    upd(parts_done=n, requests=stats["requests"], new=stats["new"], updated=stats["updated"])
            upd(status="finished", finished=time.time(), message="Finished",
                requests=stats["requests"], new=stats["new"], updated=stats["updated"])
            say("Finished.")
        except Stopped:
            upd(status="stopped", finished=time.time(), message="Stopped; start again to resume",
                requests=stats["requests"], new=stats["new"], updated=stats["updated"])
            say("Stopped by user.")
        except Blocked as e:
            upd(status="blocked", finished=time.time(), message=str(e),
                requests=stats["requests"], new=stats["new"], updated=stats["updated"])
            say(str(e))
        except Exception as e:  # noqa: BLE001 - record any failure on the job
            upd(status="failed", finished=time.time(), message=repr(e))
            say(traceback.format_exc()[-1500:])
        finally:
            c.close()

    def _call(self, ep, filters, offset, delay, stats, say):
        body = json.dumps({"filters": filters + [LEVEL], "resultOffset": offset, "portalMap": PORTAL_MAP})
        last = ""
        for attempt in range(RETRIES):
            if self._stop.is_set():
                raise Stopped()
            try:
                r = self.session.post(API + ep, data=body, timeout=30)
                stats["requests"] += 1
                if r.status_code == 200:
                    j = r.json()
                    self._stop.wait(delay)
                    return j
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = type(e).__name__
            say(f"{last} on {ep} offset {offset}; retry {attempt + 1}/{RETRIES}")
            if self._stop.wait(min(300, BACKOFF * 2 ** attempt)):
                raise Stopped()
        raise Blocked(f"Gave up after {RETRIES} tries ({last}). The site is probably limiting this server: "
                      f"wait at least 30 minutes, then start again with a higher delay. Progress is saved.")

    def _partitions(self, kind, delay, stats, say):
        base = self._call("programmes", [], 0, delay, stats, say)
        say(f"API reports {base.get('totalResultsCount', 0):,} Master's programmes")
        parts = []
        # For universities only the delivery filter changes results; full/part-time does not.
        splits = ([(at, None) for at in ("face2face", "online", "blended")] if kind == "orgs" else
                  [(at, de) for at in ("face2face", "online", "blended") for de in ("fulltime", "parttime")])
        for cc, n in sorted(base["filtersFacets"]["ci"].items(), key=lambda x: -x[1]):
            if n <= SPLIT_ABOVE:
                parts.append((cc, [flt("ci", cc)]))
            else:
                for at, de in splits:
                    f = [flt("ci", cc), flt("at", at)] + ([flt("de", de)] if de else [])
                    parts.append((cc, f))
        return parts

    def _harvest(self, c, kind, cc, filters, delay, stats, say):
        ep = ENDPOINT[kind]
        off, total, new, seen = 0, None, 0, 0
        while total is None or off < total:
            if self._stop.is_set():
                raise Stopped()
            j = self._call(ep, filters, off, delay, stats, say)
            total = j.get("totalResultsCount", 0)
            rows = j.get("results") or []
            if not rows:
                break
            now = time.time()
            for r in rows:
                rid = str(r["id"])
                existed = c.execute("SELECT 1 FROM rec WHERE kind=? AND id=?", (kind, rid)).fetchone()
                c.execute(
                    "INSERT INTO rec(kind, id, country, json, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(kind, id) DO UPDATE SET json=excluded.json, last_seen=excluded.last_seen",
                    (kind, rid, cc, json.dumps(r, ensure_ascii=False), now, now))
                if existed:
                    stats["updated"] += 1
                else:
                    stats["new"] += 1
                    new += 1
                seen += 1
            c.commit()
            off += PAGE
        extra = [x["values"][0] for x in filters[1:]]
        say(f"{kind} {cc} {' '.join(extra)}: {total} listed, {seen} fetched, +{new} new".rstrip())
