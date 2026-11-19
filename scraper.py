"""Background scrape runner.

Design rules taken from the Collegedunia/Shiksha KT (section numbers refer to it):
- the `parts` table is the queue AND the result; resume is `WHERE status IN ('queued','short')` (KT 2.1, 2.2)
- progress is checkpointed after every page, so a restart costs one page, not a section (KT G1)
- a section that returns fewer unique records than it lists is never silently "done" (KT B4, B6)
- every non-answer is named: refusal, soft block (unusable 200), empty page before the end (KT A5)
- an aggregate circuit breaker watches the whole run, not just one request (KT rule 12)
- the fingerprint is taken from what is stored (after a preserve-nonempty merge), covers only content
  fields, and a first observation is never logged as a change (KT D1, D2, D3, rules 15-17)
"""
import collections
import hashlib
import json
import os
import threading
import time
import traceback

import requests

import db as dbm

API = os.environ.get("MP_API", "https://omnisearch.mastersportal.com/api/search/")
USER_AGENT = os.environ.get("MP_USER_AGENT", "Mozilla/5.0 (research crawler)")
SPLIT_ABOVE = int(os.environ.get("MP_SPLIT_ABOVE", "400"))       # sections bigger than this get split
SPLIT_DIMS = os.environ.get("MP_SPLIT_DIMS", "at,de,dg,di").split(",")
MAX_PASSES = int(os.environ.get("MP_MAX_PASSES", "3"))          # re-reads of a section that came back short
BACKOFF = float(os.environ.get("MP_BACKOFF", "10"))             # seconds, doubled per retry
COOLDOWN = float(os.environ.get("MP_COOLDOWN", "2700"))         # auto-resume wait after a block (45 min)
MAX_AUTO_RESUMES = int(os.environ.get("MP_MAX_AUTO_RESUMES", "4"))
BREAKER_WINDOW = int(os.environ.get("MP_BREAKER_WINDOW", "50"))
BREAKER_RATE = 0.20
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

# Content fields only. Ratings, review counts, "featured" (paid placement) and logos churn without the
# programme changing, so they are stored but never fingerprinted (KT D2). Guarded by a contract test.
HASH_FIELDS = {
    "progs": ["title", "summary", "url", "organisationName", "organisationId", "locations", "tuition",
              "duration", "standardFacts"],
    "orgs": ["title", "summary", "url", "locations", "ranking", "standardFacts"],
}


class Stopped(Exception):
    pass


class Blocked(Exception):
    pass


def flt(t, v):
    return {"type": t, "isIndexable": True, "values": [v]}


def _empty(v):
    return v is None or v == "" or v == [] or v == {}


def fingerprint(kind, rec):
    body = json.dumps({f: rec.get(f) for f in HASH_FIELDS[kind]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(body.encode()).hexdigest()


def usable(j):
    """A 200 is only an answer if it has the shape of one (KT A5)."""
    return isinstance(j, dict) and isinstance(j.get("results"), list) and isinstance(j.get("totalResultsCount"), int)


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
        self._window = collections.deque(maxlen=BREAKER_WINDOW)
        self.job_id = None

    # ---- control -------------------------------------------------------
    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, kind, delay, fresh=False, user=None, auto_resume=True):
        if kind not in KINDS:
            raise ValueError("unknown kind")
        delay = max(1.0, min(30.0, float(delay)))
        with self._lock:
            if self.is_running():
                raise RuntimeError("A job is already running.")
            self._stop.clear()
            self._window.clear()
            c = dbm.connect()
            cur = c.execute(
                "INSERT INTO jobs(kind, status, delay, fresh, auto_resume, started, started_by, message) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (kind, "running", delay, int(bool(fresh)), int(bool(auto_resume)), time.time(), user, "Starting"))
            c.commit()
            c.close()
            self.job_id = cur.lastrowid
            self._thread = threading.Thread(target=self._run, args=(self.job_id, kind, delay, fresh, auto_resume),
                                            daemon=True)
            self._thread.start()
            return self.job_id

    def stop(self):
        if self.is_running():
            self._stop.set()
            c = dbm.connect()
            c.execute("UPDATE jobs SET status='stopping', message='Stopping after the current request' "
                      "WHERE id=? AND status IN ('running', 'cooling')", (self.job_id,))
            c.commit()
            c.close()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # ---- job -----------------------------------------------------------
    def _run(self, job_id, kind, delay, fresh, auto_resume):
        c = dbm.connect()
        st = {"requests": 0, "failures": 0, "new": 0, "changed": 0, "unchanged": 0}
        ctx = {"job_id": job_id, "delay": delay, "seen": set()}  # records counted once per job (KT G5)

        def upd(**kw):
            kw.update(requests=st["requests"], failures=st["failures"], new=st["new"],
                      changed=st["changed"], unchanged=st["unchanged"])
            c.execute("UPDATE jobs SET " + ", ".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), job_id))
            c.commit()

        def say(line):
            dbm.log(c, job_id, line)

        kinds = KINDS[kind]
        ctx.update(upd=upd, say=say, st=st, kinds=kinds)
        resumes = 0
        try:
            if fresh:
                for k in kinds:
                    c.execute("DELETE FROM parts WHERE kind=?", (k,))
                    c.execute("DELETE FROM part_ids WHERE kind=?", (k,))
                    c.execute("DELETE FROM done WHERE kind=?", (k,))
                c.commit()
                say("Fresh run: every section is planned and fetched again; stored records are updated, never deleted.")
            while True:
                try:
                    for k in kinds:
                        self._plan(c, k, ctx)
                    for k in kinds:
                        self._work(c, k, ctx)
                    break
                except Blocked as e:
                    if not auto_resume or resumes >= MAX_AUTO_RESUMES:
                        raise
                    resumes += 1
                    ctx["delay"] = min(30.0, round(ctx["delay"] * 1.5, 1))
                    msg = (f"{e} Auto-resume {resumes}/{MAX_AUTO_RESUMES} in {COOLDOWN / 60:.0f} min "
                           f"at a slower delay of {ctx['delay']} s.")
                    upd(status="cooling", message=msg, resumes=resumes)
                    say(msg)
                    if self._stop.wait(COOLDOWN):
                        raise Stopped()
                    self._window.clear()
                    upd(status="running", message="Resumed after cool-down", delay=ctx["delay"])
                    say("Resuming.")
            upd(status="finished", finished=time.time(), message=self._summary(c, kinds), **self._progress(c, kinds))
            say("Finished. " + self._summary(c, kinds))
        except Stopped:
            upd(status="stopped", finished=time.time(), message="Stopped; start again to resume",
                **self._progress(c, kinds))
            say("Stopped by user.")
        except Blocked as e:
            upd(status="blocked", finished=time.time(), message=str(e), **self._progress(c, kinds))
            say(str(e))
        except Exception as e:  # noqa: BLE001 - record any failure on the job
            upd(status="failed", finished=time.time(), message=repr(e))
            say(traceback.format_exc()[-1500:])
        finally:
            c.close()

    def _progress(self, c, kinds):
        q = ",".join("?" * len(kinds))
        r = c.execute(f"SELECT COUNT(*) total, SUM(status IN ('done','split')) fin FROM parts WHERE kind IN ({q})",
                      kinds).fetchone()
        return {"parts_total": r["total"] or 0, "parts_done": r["fin"] or 0}

    def _summary(self, c, kinds):
        out = []
        for k in kinds:
            stored = c.execute("SELECT COUNT(*) FROM rec WHERE kind=?", (k,)).fetchone()[0]
            short = c.execute("SELECT COUNT(*), SUM(listed - uniq) FROM parts WHERE kind=? AND status='done' "
                              "AND uniq < listed", (k,)).fetchone()
            label = "universities" if k == "orgs" else "programmes"
            part = f"{stored:,} {label} stored"
            if k == "progs":
                api = dbm.meta_get(c, "api_total_progs")
                if api:
                    part += f" of {int(api):,} listed ({100 * stored / int(api):.1f}%)"
            if short[0]:
                part += f"; {short[0]} sections still short by {short[1]:,} after {MAX_PASSES} passes"
            out.append(part)
        return " · ".join(out)

    # ---- transport (the one seam tests replace: self.session) ------------
    def _call(self, ep, filters, offset, ctx):
        body = json.dumps({"filters": filters + [LEVEL], "resultOffset": offset, "portalMap": PORTAL_MAP})
        st, say = ctx["st"], ctx["say"]
        last = ""
        for attempt in range(RETRIES):
            if self._stop.is_set():
                raise Stopped()
            ok = False
            try:
                r = self.session.post(API + ep, data=body, timeout=30)
                st["requests"] += 1
                if r.status_code == 200:
                    try:
                        j = r.json()
                    except ValueError:
                        j = None
                    if usable(j):
                        ok = True
                    else:
                        last = "soft block (HTTP 200 without usable results)"
                else:
                    last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = type(e).__name__
            self._window.append(ok)
            if ok:
                self._stop.wait(ctx["delay"])
                return j
            st["failures"] += 1
            fails = self._window.count(False)
            if len(self._window) >= BREAKER_WINDOW and fails / len(self._window) > BREAKER_RATE:
                raise Blocked(f"Circuit breaker: {fails} of the last {len(self._window)} requests failed "
                              f"(latest: {last}). The site is limiting this server.")
            say(f"{last} on {ep} offset {offset}; retry {attempt + 1}/{RETRIES}")
            if self._stop.wait(min(300, BACKOFF * 2 ** attempt)):
                raise Stopped()
        raise Blocked(f"Gave up after {RETRIES} tries ({last}). The site is probably limiting this server. "
                      f"Progress is saved.")

    # ---- planning ------------------------------------------------------
    def _plan(self, c, kind, ctx):
        """Queue one section per country, once. Existing sections (including finished ones) are kept."""
        if c.execute("SELECT 1 FROM parts WHERE kind=? LIMIT 1", (kind,)).fetchone():
            return
        base = self._call("programmes", [], 0, ctx)
        ci = (base.get("filtersFacets") or {}).get("ci") or {}
        if not ci:
            raise Blocked("The API returned no country list, so the work cannot be planned. Nothing was skipped.")
        now = time.time()
        dbm.meta_set(c, "api_total_progs", base["totalResultsCount"])
        dbm.meta_set(c, "api_total_progs_ts", now)
        for cc, n in sorted(ci.items(), key=lambda x: -x[1]):
            c.execute("INSERT OR IGNORE INTO parts(kind, part, cc, depth, status, updated) VALUES (?,?,?,0,'queued',?)",
                      (kind, json.dumps([flt("ci", cc)], sort_keys=True), cc, now))
            if kind == "progs":
                c.execute("INSERT INTO listed(kind, cc, n, ts) VALUES ('progs',?,?,?) "
                          "ON CONFLICT(kind, cc) DO UPDATE SET n=excluded.n, ts=excluded.ts", (cc, n, now))
        c.commit()
        ctx["say"](f"Planned {len(ci)} country sections for {kind}; the API lists "
                   f"{base['totalResultsCount']:,} Master's programmes.")

    def _children(self, filters, j, total):
        """Split a big section on the next facet whose values provably cover it (no blank bucket)."""
        used = {f["type"] for f in filters}
        facets = j.get("filtersFacets") or {}
        for dim in SPLIT_DIMS:
            if dim in used:
                continue
            f = facets.get(dim) or {}
            vals = {v: n for v, n in f.items() if n}
            if len(vals) < 2 or "" in vals or sum(vals.values()) < total:
                continue  # a blank bucket or a shortfall means the children would miss records
            return dim, [filters + [flt(dim, v)] for v in vals]
        return None, []

    # ---- work ----------------------------------------------------------
    def _work(self, c, kind, ctx):
        while True:
            if self._stop.is_set():
                raise Stopped()
            p = c.execute("SELECT * FROM parts WHERE kind=? AND status IN ('queued','short') "
                          "ORDER BY status='short', depth, rowid LIMIT 1", (kind,)).fetchone()
            if not p:
                return
            self._do_part(c, kind, p, ctx)
            ctx["upd"](**self._progress(c, ctx["kinds"]))

    def _do_part(self, c, kind, p, ctx):
        say, key = ctx["say"], p["part"]
        filters = json.loads(key)
        label = " ".join(f["values"][0] for f in filters)
        off, empty_retry, total = p["next_offset"] or 0, False, p["listed"]
        while True:
            j = self._call(ENDPOINT[kind], filters, off, ctx)
            total, rows = j["totalResultsCount"], j["results"]
            if off == 0 and p["passes"] == 0 and kind == "progs" and total > SPLIT_ABOVE:
                dim, kids = self._children(filters, j, total)
                if kids:
                    now = time.time()
                    for kf in kids:
                        c.execute("INSERT OR IGNORE INTO parts(kind, part, cc, depth, status, updated) "
                                  "VALUES (?,?,?,?,'queued',?)",
                                  (kind, json.dumps(kf, sort_keys=True), p["cc"], p["depth"] + 1, now))
                    self._store(c, kind, key, p["cc"], rows, ctx)  # page 1 is real data; keep it
                    c.execute("UPDATE parts SET status='split', listed=?, note=?, updated=? WHERE kind=? AND part=?",
                              (total, f"split by {dim} into {len(kids)}", now, kind, key))
                    c.commit()
                    say(f"{kind} {label}: {total:,} listed, split by {dim} into {len(kids)} sections")
                    return
            if not rows and off < total:
                if not empty_retry:  # an empty page before the end is a non-answer, not the end (KT A5, B6)
                    empty_retry = True
                    say(f"{kind} {label}: empty page at offset {off} of {total}; asking again")
                    continue
                say(f"{kind} {label}: empty page again at offset {off} of {total}; ending this pass early")
                break
            empty_retry = False
            self._store(c, kind, key, p["cc"], rows, ctx)
            off += PAGE
            c.execute("UPDATE parts SET next_offset=?, listed=?, updated=? WHERE kind=? AND part=?",
                      (off, total, time.time(), kind, key))
            c.commit()  # checkpoint after every page (KT G1)
            if off >= total or not rows:
                break
        uniq = c.execute("SELECT COUNT(*) FROM part_ids WHERE kind=? AND part=?", (kind, key)).fetchone()[0]
        passes = p["passes"] + 1
        if uniq >= total:
            status, note = "done", None
        elif passes < MAX_PASSES:
            status, note = "short", f"pass {passes}: {uniq:,} unique of {total:,} listed; reading it again"
        else:  # finished, but say so in the row: short is recorded, never silent (KT B4)
            status, note = "done", f"short by {total - uniq:,} after {passes} passes"
        c.execute("UPDATE parts SET status=?, passes=?, uniq=?, listed=?, next_offset=0, note=?, updated=? "
                  "WHERE kind=? AND part=?", (status, passes, uniq, total, note, time.time(), kind, key))
        c.commit()
        say(f"{kind} {label}: {total:,} listed, {uniq:,} unique after pass {passes}"
            + (f" — {note}" if note else ""))

    # ---- storage -------------------------------------------------------
    def _store(self, c, kind, key, cc, rows, ctx):
        st, now, job_id = ctx["st"], time.time(), ctx["job_id"]
        for r in rows:
            rid = str(r["id"])
            first_visit = (kind, rid) not in ctx["seen"]
            ctx["seen"].add((kind, rid))
            c.execute("INSERT OR IGNORE INTO part_ids(kind, part, id) VALUES (?,?,?)", (kind, key, rid))
            old = c.execute("SELECT json, content_hash FROM rec WHERE kind=? AND id=?", (kind, rid)).fetchone()
            if not old:
                c.execute("INSERT INTO rec(kind, id, country, json, first_seen, last_seen, last_changed, content_hash) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (kind, rid, cc, json.dumps(r, ensure_ascii=False), now, now, now, fingerprint(kind, r)))
                st["new"] += 1
                continue
            stored = json.loads(old["json"])
            merged = dict(stored)  # preserve-nonempty: a blank incoming value never erases a stored one (KT 2.5)
            merged.update({k: v for k, v in r.items() if not _empty(v)})
            h = fingerprint(kind, merged)  # fingerprint what will be stored, not what arrived (KT D3)
            diffs = [f for f in HASH_FIELDS[kind] if stored.get(f) != merged.get(f)]
            if old["content_hash"] is None or h == old["content_hash"] or not diffs:
                # first observation (legacy row), unchanged, or stale-hash drift: no change logged (KT D1, D6)
                c.execute("UPDATE rec SET json=?, content_hash=?, last_seen=? WHERE kind=? AND id=?",
                          (json.dumps(merged, ensure_ascii=False), h, now, kind, rid))
                st["unchanged"] += first_visit
                continue
            for f in diffs:
                c.execute("INSERT INTO changes(kind, rec_id, field, old, new, ts, job_id) VALUES (?,?,?,?,?,?,?)",
                          (kind, rid, f, json.dumps(stored.get(f), ensure_ascii=False),
                           json.dumps(merged.get(f), ensure_ascii=False), now, job_id))
            c.execute("UPDATE rec SET json=?, content_hash=?, last_seen=?, last_changed=? WHERE kind=? AND id=?",
                      (json.dumps(merged, ensure_ascii=False), h, now, now, kind, rid))
            st["changed"] += 1
        c.commit()
