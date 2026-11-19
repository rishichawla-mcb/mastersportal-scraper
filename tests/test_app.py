"""End-to-end tests: the real runner, real storage and real web app, driven through a fake API (no network).

The fake is the only thing replaced (the transport seam, KT 2.3). Each KT-derived guard has a test, and
where a test asserts an absence there is a paired check that the thing is found when present (KT H2).
"""
import copy
import json
import os
import random
import re
import sqlite3
import sys
import time

import pytest

os.environ.update(MP_SECRET_KEY="test-secret", MP_COOKIE_SECURE="0", MP_SPLIT_ABOVE="30", MP_BACKOFF="0",
                  MP_COOLDOWN="0", MP_SPLIT_DIMS="at,de,dg,di", MP_BREAKER_WINDOW="10")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
from app import create_app  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def make_catalogue(gb=50):
    progs = []
    for cc, n, base in (("GB", gb, 1000), ("US", 25, 2000), ("NL", 7, 3000)):
        for i in range(n):
            progs.append({"id": str(base + i), "cc": cc, "at": ["face2face", "online", "blended"][i % 3],
                          "de": ["fulltime", "parttime"][i % 2], "dg": ["msc", ""][i % 2], "di": [str(i % 4), "9"],
                          "title": f"MSc Thing {cc} {i}", "organisationName": f"Uni {cc} {i % 2}", "organisationId": i % 2,
                          "url": f"https://example.org/studies/{base + i}", "locations": [f"City, {cc}"],
                          "tuition": {"amount": 1000 + i, "currency": "EUR", "period": "year"},
                          "standardFacts": [{"value": "M.Sc."}, {"value": "Full-time"}, {"value": "On-campus"}],
                          "reviewRating": {"rating": 4.25, "amount": 3}, "duration": "1 year", "isFeatured": False})
    orgs = [{"id": str(100 + k), "cc": cc, "at": "face2face", "title": f"University {cc} {k}",
             "url": f"https://example.org/universities/{cc}{k}", "locations": [f"City, {cc}"],
             "standardFacts": [{"value": "12 Masters"}, {"value": "On-campus"}], "ranking": {"globalRankingPosition": k + 1}}
            for k, cc in enumerate(["GB", "GB", "US", "NL", "US", "GB"])]
    return progs, orgs


HIDDEN = ("cc", "at", "de", "dg", "di")


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


class FakeSession:
    """Mimics the omnisearch API: filters, facets scoped to the current filter, offsets, totals.

    Fault switches let each test inject one failure mode."""

    def __init__(self, **faults):
        self.headers, self.calls = {}, []
        self.progs, self.orgs = make_catalogue(faults.pop("gb", 50))
        self.faults = faults
        self.seen_pages = set()

    def post(self, url, data, timeout):
        body = json.loads(data)
        n = len(self.calls)
        self.calls.append((url, body))
        f = self.faults
        if self.headers.get("content-type") != "application/json":
            return FakeResp(503, {})
        if f.get("fail_status") and n >= f.get("fail_after", 0):
            return FakeResp(f["fail_status"], {})
        if f.get("fail_every") and n % f["fail_every"] == 0 and n:   # scattered refusals, never 6 in a row
            return FakeResp(403, {})
        if f.get("soft_once") and n == f["soft_once"]:
            return FakeResp(200, ValueError("Expecting value: line 1 column 1"))  # HTML challenge page
        if f.get("no_results_once") and n == f["no_results_once"]:
            return FakeResp(200, {"message": "busy"})
        progs = url.endswith("programmes")
        filt = {}
        for x in body["filters"]:
            filt[x["type"]] = x["values"][0]
        pool = self.progs if progs else self.orgs
        hits = [p for p in pool if all(self._match(p, t, v) for t, v in filt.items() if t != "lv")]
        off = body["resultOffset"]
        if f.get("empty_once_at") is not None and off == f["empty_once_at"] and "empty_done" not in f and off < len(hits):
            f["empty_done"] = True
            return FakeResp(200, {"totalResultsCount": len(hits), "results": [], "filtersFacets": {}})
        order = list(hits)
        key = (url, json.dumps(body["filters"], sort_keys=True), off)
        if f.get("unstable") and key not in self.seen_pages:
            random.Random(n).shuffle(order)  # first read of each page: the order shifts between requests
        self.seen_pages.add(key)
        facets = {}
        if progs:
            facets["ci"] = {}
            for p in self.progs:
                facets["ci"][p["cc"]] = facets["ci"].get(p["cc"], 0) + 1
            for dim in ("at", "de", "dg", "di"):
                d = {}
                for p in hits:
                    for v in (p[dim] if isinstance(p[dim], list) else [p[dim]]):
                        d[v] = d.get(v, 0) + 1
                facets[dim] = d
        page = [{k: v for k, v in p.items() if k not in HIDDEN} for p in order[off:off + 20]]
        return FakeResp(200, {"totalResultsCount": len(hits), "filtersFacets": facets, "results": page})

    @staticmethod
    def _match(p, t, v):
        if v == "":
            return False  # like the real API, a blank facet value cannot be filtered on
        if t == "ci":
            return p["cc"] == v
        if t not in p:
            return True
        return v in p[t] if isinstance(p[t], list) else p[t] == v


@pytest.fixture()
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("MP_DB", str(tmp_path / "t.sqlite"))

    def _make(session=None):
        runner = scraper.Runner(session=session or FakeSession())
        app = create_app(runner=runner)
        app.testing = True
        import db as dbm
        c = dbm.connect()
        for email, role in (("admin@x.com", "admin"), ("viewer@x.com", "viewer")):
            c.execute("INSERT OR IGNORE INTO users(email, pw, role, created) VALUES (?,?,?,?)",
                      (email, generate_password_hash("password123"), role, time.time()))
        c.commit()
        c.close()
        return app, runner
    return _make


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def login(client, email="admin@x.com", pw="password123"):
    return client.post("/login", data={"email": email, "password": pw, "csrf": csrf(client)})


def scrape(client, runner, kind="both", fresh=False, auto=False, real_delay=False):
    data = {"kind": kind, "delay": "1", "csrf": csrf(client, "/")}
    if fresh:
        data["fresh"] = "1"
    if auto:
        data["auto_resume"] = "1"
    if not real_delay and not getattr(runner, "_patched", False):
        real_wait = runner._stop.wait  # the 1 s minimum delay is skipped in tests
        runner._stop.wait = lambda t=None: real_wait(0)
        runner._patched = True
    client.post("/jobs/start", data=data)
    runner.wait(30)
    return last_job()


def conn():
    import db as dbm
    return dbm.connect()


def last_job():
    return conn().execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()


def count(kind):
    return conn().execute("SELECT COUNT(*) FROM rec WHERE kind=?", (kind,)).fetchone()[0]


# ---------------------------------------------------------------- auth and roles
def test_requires_login(make):
    app, _ = make()
    c = app.test_client()
    for path in ("/", "/coverage", "/changes", "/export/programmes.csv", "/export/changes.csv"):
        assert c.get(path).status_code == 302, path
    assert c.get("/healthz").json["ok"] is True


def test_bad_login_and_lockout(make):
    app, _ = make()
    c = app.test_client()
    for _ in range(5):
        assert "Wrong email or password" in login(c, pw="wrong").get_data(as_text=True)
    assert login(c).status_code == 429


def test_csrf_required(make):
    app, _ = make()
    c = app.test_client()
    login(c)
    assert c.post("/jobs/start", data={"kind": "both"}).status_code == 400


def test_viewer_cannot_admin(make):
    app, _ = make()
    c = app.test_client()
    login(c, "viewer@x.com")
    assert c.post("/jobs/start", data={"kind": "both", "csrf": csrf(c, "/")}).status_code == 403
    assert c.get("/users").status_code == 403
    assert c.get("/coverage").status_code == 200


def test_user_management(make):
    app, _ = make()
    c = app.test_client()
    login(c)
    c.post("/users", data={"csrf": csrf(c, "/users"), "action": "add", "email": "new@x.com",
                           "password": "longpassword1", "role": "viewer"})
    assert login(app.test_client(), "new@x.com", "longpassword1").status_code == 302


# ---------------------------------------------------------------- full run, splitting, export
def test_full_scrape_split_and_export(make):
    app, runner = make()
    c = app.test_client()
    login(c)
    job = scrape(c, runner)
    assert job["status"] == "finished", job["message"]
    s = runner.session
    assert count("progs") == len(s.progs) and count("orgs") == len(s.orgs)
    db = conn()
    gb = db.execute("SELECT status, note FROM parts WHERE kind='progs' AND depth=0 AND cc='GB'").fetchone()
    assert gb["status"] == "split" and "split by at" in gb["note"]  # 50 > split limit 30
    # dg has a blank bucket (programmes with no degree type), so it must never be used to split
    parts = [json.loads(p[0]) for p in db.execute("SELECT part FROM parts WHERE kind='progs'")]
    assert not any(f["type"] == "dg" for p in parts for f in p)
    # paired check: the same rule does split when a dimension covers everything
    assert any(f["type"] == "at" for p in parts for f in p)
    assert job["new"] == len(s.progs) + len(s.orgs) and job["changed"] == 0
    csv_text = c.get("/export/programmes.csv").get_data(as_text=True)
    assert csv_text.count("\n") == len(s.progs) + 1 and "last_changed" in csv_text
    assert c.get("/export/universities.csv?country=GB").get_data(as_text=True).count("\n") == 1 + 3
    assert "25 found" in c.get("/programmes?country=US&q=Thing").get_data(as_text=True)
    cov = c.get("/coverage").get_data(as_text=True)
    assert "82 listed by the API" in cov and "100.0%" in cov


def test_never_splits_on_a_dimension_with_a_blank_bucket(make, monkeypatch):
    """Degree type has a blank bucket (no degree set); splitting on it would silently lose those records."""
    monkeypatch.setattr(scraper, "SPLIT_DIMS", ["dg", "at"])
    app, runner = make()
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "finished"
    assert count("progs") == len(runner.session.progs)
    gb = conn().execute("SELECT note FROM parts WHERE kind='progs' AND depth=0 AND cc='GB'").fetchone()[0]
    assert "split by at" in gb  # dg was skipped, at was used


def test_unstable_order_gets_reread_not_marked_done(make):
    """KT B4/B6: a section with fewer unique records than listed is 'short' and read again."""
    app, runner = make(FakeSession(unstable=True))
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "finished", job["message"]
    assert count("progs") == len(runner.session.progs)  # repasses recovered everything
    passes = [r[0] for r in conn().execute("SELECT passes FROM parts WHERE kind='progs' AND status='done'")]
    assert max(passes) >= 2  # at least one section was short on its first pass and re-read


def test_short_after_max_passes_is_recorded(make, monkeypatch):
    monkeypatch.setattr(scraper, "MAX_PASSES", 1)
    app, runner = make(FakeSession(unstable=True))
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    short = conn().execute("SELECT note FROM parts WHERE uniq < listed AND status='done'").fetchall()
    assert short and all("short by" in r[0] for r in short)
    assert "still short" in job["message"]
    assert "Short sections" in c.get("/coverage").get_data(as_text=True)


def test_empty_page_mid_section_is_asked_again(make):
    app, runner = make(FakeSession(empty_once_at=20))
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "finished"
    assert count("progs") == len(runner.session.progs)
    assert any("asking again" in r[0] for r in conn().execute("SELECT line FROM log"))


@pytest.mark.parametrize("fault", [{"soft_once": 3}, {"no_results_once": 3}])
def test_soft_block_is_retried_not_data(make, fault):
    """KT A5: a 200 that is not an answer (HTML challenge, missing results) is retried, never stored."""
    app, runner = make(FakeSession(**fault))
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "finished" and job["failures"] == 1
    assert count("progs") == len(runner.session.progs)
    assert any("soft block" in r[0] for r in conn().execute("SELECT line FROM log"))


# ---------------------------------------------------------------- stopping, resuming, checkpoints
def test_stop_then_resume_from_page_checkpoint(make, monkeypatch):
    monkeypatch.setattr(scraper, "SPLIT_ABOVE", 1000)  # GB stays one 3-page section
    app, runner = make()
    c = app.test_client()
    login(c)
    tok = csrf(c, "/")
    c.post("/jobs/start", data={"kind": "progs", "delay": "1", "csrf": tok})  # real 1 s delay
    deadline = time.time() + 10
    while time.time() < deadline and not conn().execute("SELECT 1 FROM parts WHERE next_offset > 0").fetchone():
        time.sleep(0.05)
    c.post("/jobs/stop", data={"csrf": tok})
    runner.wait(10)
    assert last_job()["status"] == "stopped"
    ckpt = conn().execute("SELECT part, next_offset FROM parts WHERE next_offset > 0").fetchone()
    assert ckpt, "a page checkpoint was saved"
    before = len(runner.session.calls)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "finished"
    resumed = [b for u, b in runner.session.calls[before:] if json.dumps(
        [f for f in b["filters"] if f["type"] != "lv"], sort_keys=True) == ckpt["part"]]
    assert resumed and resumed[0]["resultOffset"] == ckpt["next_offset"]  # did not restart the section at 0
    assert count("progs") == len(runner.session.progs)


def test_resume_skips_done_and_fresh_refetches(make):
    app, runner = make()
    c = app.test_client()
    login(c)
    scrape(c, runner, kind="orgs")
    first = len(runner.session.calls)
    scrape(c, runner, kind="orgs")
    assert len(runner.session.calls) == first  # nothing queued: zero requests
    job = scrape(c, runner, kind="orgs", fresh=True)
    assert len(runner.session.calls) > first
    assert job["new"] == 0 and job["changed"] == 0 and job["unchanged"] >= len(runner.session.orgs)


def test_restart_marks_live_jobs_interrupted(make):
    make()
    import db as dbm
    db = conn()
    for s in ("running", "cooling"):
        db.execute("INSERT INTO jobs(kind, status, started) VALUES ('both',?,?)", (s, time.time()))
    db.commit()
    dbm.init()
    assert {r[0] for r in db.execute("SELECT status FROM jobs")} == {"interrupted"}


# ---------------------------------------------------------------- blocking
def test_consecutive_refusals_block(make):
    app, runner = make(FakeSession(fail_status=403, fail_after=3))
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "blocked" and "HTTP 403" in job["message"]


def test_circuit_breaker_on_aggregate_failure_rate(make):
    """KT rule 12: scattered refusals (never 6 in a row) above 20% still stop the run."""
    app, runner = make(FakeSession(fail_every=3, gb=400))  # 1 in 3 refused
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["status"] == "blocked" and "Circuit breaker" in job["message"]


def test_breaker_tolerates_low_failure_rate(make):
    """Paired check: 1 refusal in 10 is below the breaker and the run finishes."""
    app, runner = make(FakeSession(fail_every=7, gb=400))  # at most 2 in any 10 = 20%, not above it
    c = app.test_client()
    login(c)
    job = scrape(c, runner, kind="progs")
    assert job["requests"] > 30  # the window really filled
    assert job["status"] == "finished" and job["failures"] > 0
    assert count("progs") == len(runner.session.progs)


def test_auto_resume_after_block_slows_down_and_finishes(make):
    s = FakeSession(fail_status=403, fail_after=5)
    app, runner = make(s)
    c = app.test_client()
    login(c)
    orig = s.post

    def heal(url, data, timeout):  # the block clears during the cool-down
        if s.faults.get("fail_status") and len(s.calls) > 12:
            s.faults.pop("fail_status")
        return orig(url, data, timeout)
    s.post = heal
    job = scrape(c, runner, kind="progs", auto=True)
    assert job["status"] == "finished", job["message"]
    assert job["resumes"] == 1 and job["delay"] == 1.5  # resumed once, 1.5x slower
    assert count("progs") == len(s.progs)


def test_auto_resume_off_stays_blocked(make):
    app, runner = make(FakeSession(fail_status=403, fail_after=5))
    c = app.test_client()
    login(c)
    assert scrape(c, runner, kind="progs", auto=False)["status"] == "blocked"


# ---------------------------------------------------------------- change tracking
def test_change_tracking_logs_only_real_changes(make):
    s = FakeSession()
    app, runner = make(s)
    c = app.test_client()
    login(c)
    scrape(c, runner, kind="progs")
    assert conn().execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0  # first observation is not a change
    p = next(x for x in s.progs if x["id"] == "1003")
    p["tuition"] = {"amount": 9999, "currency": "EUR", "period": "year"}
    for x in s.progs:  # noise that must not count as change
        x["reviewRating"] = {"rating": 3.0, "amount": 99}
        x["isFeatured"] = True
    job = scrape(c, runner, kind="progs", fresh=True)
    assert job["changed"] == 1 and job["unchanged"] == len(s.progs) - 1
    rows = conn().execute("SELECT rec_id, field, old, new FROM changes").fetchall()
    assert [(r["rec_id"], r["field"]) for r in rows] == [("1003", "tuition")]
    assert '"amount": 1003' in rows[0]["old"] and '"amount": 9999' in rows[0]["new"]
    page = c.get("/changes").get_data(as_text=True)
    assert "MSc Thing GB 3" in page and "tuition" in page
    assert c.get("/export/changes.csv").get_data(as_text=True).count("\n") == 2


def test_blank_incoming_value_never_erases_stored(make):
    s = FakeSession()
    app, runner = make(s)
    c = app.test_client()
    login(c)
    scrape(c, runner, kind="progs")
    p = next(x for x in s.progs if x["id"] == "1004")
    p["tuition"], p["summary"] = None, ""
    job = scrape(c, runner, kind="progs", fresh=True)
    assert job["changed"] == 0
    stored = json.loads(conn().execute("SELECT json FROM rec WHERE id='1004'").fetchone()[0])
    assert stored["tuition"]["amount"] == 1004


def test_hash_fields_contract():
    """KT D2: no volatile or bookkeeping field may enter the fingerprint."""
    volatile = {"reviewRating", "isFeatured", "highlightedFacts", "logoUrl", "organisationLogoUrl", "position"}
    for kind, fields in scraper.HASH_FIELDS.items():
        assert not volatile & set(fields), kind
        assert not any(f.endswith(("_at", "At", "_seen", "Seen", "_ts", "Timestamp", "Date")) for f in fields), kind
    a = {"title": "X", "tuition": {"amount": 1}, "reviewRating": {"rating": 4}}
    b = dict(a, reviewRating={"rating": 1}, isFeatured=True)
    assert scraper.fingerprint("progs", a) == scraper.fingerprint("progs", b)
    assert scraper.fingerprint("progs", a) != scraper.fingerprint("progs", dict(a, tuition={"amount": 2}))


# ---------------------------------------------------------------- legacy data
def test_reads_cli_sqlite_and_imports_done_sections(tmp_path, monkeypatch):
    p = tmp_path / "cli.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE rec (kind TEXT, id TEXT, country TEXT, json TEXT, PRIMARY KEY(kind,id))")
    c.execute("CREATE TABLE done (kind TEXT, part TEXT, PRIMARY KEY(kind,part))")
    c.execute("INSERT INTO rec VALUES ('orgs','100','GB',?)", (json.dumps({"id": "100", "title": "University GB 0"}),))
    c.execute("INSERT INTO done VALUES ('orgs', ?)", (json.dumps([scraper.flt("ci", "GB")]),))
    c.commit()
    c.close()
    monkeypatch.setenv("MP_DB", str(p))
    s = FakeSession()
    runner = scraper.Runner(session=s)
    app = create_app(runner=runner)
    db = conn()
    db.execute("INSERT INTO users(email,pw,role,created) VALUES ('a@x.com',?,'admin',0)",
               (generate_password_hash("password123"),))
    db.commit()
    assert db.execute("SELECT status FROM parts WHERE kind='orgs'").fetchone()[0] == "done"
    cl = app.test_client()
    login(cl, "a@x.com")
    assert "University GB 0" in cl.get("/export/universities.csv").get_data(as_text=True)
    # the legacy row has no fingerprint; seeing it again is a first observation, not a change
    job = scrape(cl, runner, kind="orgs", fresh=True)
    assert job["status"] == "finished" and job["changed"] == 0
    assert conn().execute("SELECT content_hash FROM rec WHERE id='100'").fetchone()[0]
