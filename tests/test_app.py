"""End-to-end tests against a fake Mastersportal API (no network)."""
import json
import os
import re
import sys
import time

import pytest

os.environ.update(MP_SECRET_KEY="test-secret", MP_COOKIE_SECURE="0", MP_SPLIT_ABOVE="30", MP_BACKOFF="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402
from app import create_app  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

# Fake catalogue: GB has 50 programmes (gets split), US 25, NL 7; programmes spread over 6 universities.
PROGS = []
for cc, n in (("GB", 50), ("US", 25), ("NL", 7)):
    for i in range(n):
        PROGS.append({"id": f"{cc}{i}".replace("GB", "1").replace("US", "2").replace("NL", "3"),
                      "cc": cc, "at": ["face2face", "online", "blended"][i % 3], "de": ["fulltime", "parttime"][i % 2],
                      "title": f"MSc Thing {cc} {i}", "organisationName": f"Uni {cc} {i % 2}", "organisationId": i % 2,
                      "url": f"https://example.org/studies/{cc}{i}", "locations": [f"City, {cc}"],
                      "tuition": {"amount": 1000 + i, "currency": "EUR", "period": "year"},
                      "standardFacts": [{"value": "M.Sc."}, {"value": "Full-time"}, {"value": "On-campus"}],
                      "reviewRating": {"rating": 4.25, "amount": 3}, "duration": "1 year"})
ORGS = [{"id": str(100 + k), "cc": cc, "at": "face2face", "title": f"University {cc} {k}",
         "url": f"https://example.org/universities/{cc}{k}", "locations": [f"City, {cc}"],
         "standardFacts": [{"value": "12 Masters"}, {"value": "On-campus"}], "ranking": {"globalRankingPosition": k + 1}}
        for k, cc in enumerate(["GB", "GB", "US", "NL", "US", "GB"])]


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class FakeSession:
    """Mimics the omnisearch API, optionally failing with a status code."""

    def __init__(self, fail_status=None, fail_after=None):
        self.headers, self.calls = {}, []
        self.fail_status, self.fail_after = fail_status, fail_after

    def post(self, url, data, timeout):
        body = json.loads(data)
        self.calls.append((url, body))
        if self.headers.get("content-type") != "application/json":
            return FakeResp(503, {})
        if self.fail_status and (self.fail_after is None or len(self.calls) > self.fail_after):
            return FakeResp(self.fail_status, {})
        f = {x["type"]: x["values"][0] for x in body["filters"]}
        pool = PROGS if url.endswith("programmes") else ORGS
        hits = [p for p in pool if (f.get("ci") in (None, p["cc"])) and (f.get("at") in (None, p["at"]))
                and (f.get("de") in (None, p.get("de")) if url.endswith("programmes") else True)]
        facets = {"ci": {}}
        for p in PROGS:
            facets["ci"][p["cc"]] = facets["ci"].get(p["cc"], 0) + 1
        off = body["resultOffset"]
        return FakeResp(200, {"totalResultsCount": len(hits), "filtersFacets": facets,
                              "results": [{k: v for k, v in p.items() if k not in ("cc", "at", "de")} for p in hits[off:off + 20]]})


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
            c.execute("INSERT INTO users(email, pw, role, created) VALUES (?,?,?,?)",
                      (email, generate_password_hash("password123"), role, time.time()))
        c.commit()
        c.close()
        return app, runner
    return _make


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def login(client, email="admin@x.com", pw="password123"):
    tok = csrf(client)
    return client.post("/login", data={"email": email, "password": pw, "csrf": tok})


def scrape(client, runner, kind="both", fresh=False):
    tok = csrf(client, "/")
    data = {"kind": kind, "delay": "1", "csrf": tok}
    if fresh:
        data["fresh"] = "1"
    real_wait = runner._stop.wait  # delay is clamped to >=1s; skip real sleeping in tests
    runner._stop.wait = lambda t=None: real_wait(0)
    client.post("/jobs/start", data=data)
    runner.wait(20)


def test_requires_login(make):
    app, _ = make()
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/export/programmes.csv").status_code == 302
    assert c.get("/healthz").json["ok"] is True


def test_bad_login_and_lockout(make):
    app, _ = make()
    c = app.test_client()
    for _ in range(5):
        r = login(c, pw="wrong")
        assert "Wrong email or password" in r.get_data(as_text=True)
    assert login(c).status_code == 429  # locked even with the right password


def test_csrf_required(make):
    app, _ = make()
    c = app.test_client()
    login(c)
    assert c.post("/jobs/start", data={"kind": "both"}).status_code == 400


def test_full_scrape_dedup_and_export(make):
    app, runner = make()
    c = app.test_client()
    login(c)
    scrape(c, runner)
    import db as dbm
    conn = dbm.connect()
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert job["status"] == "finished", job["message"]
    n = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) n FROM rec GROUP BY kind")}
    assert n == {"progs": len(PROGS), "orgs": len(ORGS)}
    # GB (50 > split 30) was split; universities split by delivery only (3 parts), programmes by 6
    parts = [json.loads(p["part"]) for p in conn.execute("SELECT part FROM done WHERE kind='progs'")]
    assert sum(1 for p in parts if p[0]["values"] == ["GB"]) == 6
    oparts = [json.loads(p["part"]) for p in conn.execute("SELECT part FROM done WHERE kind='orgs'")]
    assert sum(1 for p in oparts if p[0]["values"] == ["GB"]) == 3
    csv_text = c.get("/export/programmes.csv").get_data(as_text=True)
    assert csv_text.count("\n") == len(PROGS) + 1 and "tuition_amount" in csv_text
    gb = c.get("/export/universities.csv?country=GB").get_data(as_text=True)
    assert gb.count("\n") == 1 + 3
    page = c.get("/programmes?country=US&q=Thing").get_data(as_text=True)
    assert "25 found" in page
    assert "82" in c.get("/").get_data(as_text=True)  # dashboard shows stored count


def test_resume_skips_done_and_fresh_refetches(make):
    app, runner = make()
    c = app.test_client()
    login(c)
    scrape(c, runner, kind="orgs")
    first = len(runner.session.calls)
    scrape(c, runner, kind="orgs")
    assert len(runner.session.calls) - first == 1  # only the facets call; every section already done
    before = len(runner.session.calls)
    scrape(c, runner, kind="orgs", fresh=True)
    assert len(runner.session.calls) - before > 1
    import db as dbm
    job = dbm.connect().execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert job["new"] == 0 and job["updated"] >= len(ORGS)


def test_blocked_stops_job(make):
    app, runner = make(FakeSession(fail_status=403, fail_after=3))
    c = app.test_client()
    login(c)
    scrape(c, runner, kind="progs")
    import db as dbm
    job = dbm.connect().execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert job["status"] == "blocked" and "HTTP 403" in job["message"]


def test_stop(make):
    app, runner = make()
    c = app.test_client()
    login(c)
    tok = csrf(c, "/")
    c.post("/jobs/start", data={"kind": "progs", "delay": "1", "csrf": tok})  # real 1s delay
    time.sleep(0.3)
    c.post("/jobs/stop", data={"csrf": tok})
    runner.wait(10)
    import db as dbm
    assert dbm.connect().execute("SELECT status FROM jobs ORDER BY id DESC").fetchone()[0] == "stopped"


def test_viewer_cannot_admin(make):
    app, _ = make()
    c = app.test_client()
    login(c, "viewer@x.com")
    tok = csrf(c, "/")
    assert c.post("/jobs/start", data={"kind": "both", "csrf": tok}).status_code == 403
    assert c.get("/users").status_code == 403
    assert c.get("/programmes").status_code == 200


def test_user_management(make):
    app, _ = make()
    c = app.test_client()
    login(c)
    tok = csrf(c, "/users")
    c.post("/users", data={"csrf": tok, "action": "add", "email": "new@x.com", "password": "longpassword1", "role": "viewer"})
    assert "new@x.com" in c.get("/users").get_data(as_text=True)
    c2 = app.test_client()
    assert login(c2, "new@x.com", "longpassword1").status_code == 302


def test_restart_marks_running_jobs_interrupted(make, tmp_path):
    app, _ = make()
    import db as dbm
    conn = dbm.connect()
    conn.execute("INSERT INTO jobs(kind, status, started) VALUES ('both','running',?)", (time.time(),))
    conn.commit()
    dbm.init()
    assert conn.execute("SELECT status FROM jobs ORDER BY id DESC").fetchone()[0] == "interrupted"


def test_reads_cli_sqlite(tmp_path, monkeypatch):
    """A mastersportal.sqlite made by the CLI script opens and gains the new columns."""
    import sqlite3
    p = tmp_path / "cli.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE rec (kind TEXT, id TEXT, country TEXT, json TEXT, PRIMARY KEY(kind,id))")
    c.execute("CREATE TABLE done (kind TEXT, part TEXT, PRIMARY KEY(kind,part))")
    c.execute("INSERT INTO rec VALUES ('orgs','5','GB',?)", (json.dumps({"id": "5", "title": "Old Uni"}),))
    c.commit()
    c.close()
    monkeypatch.setenv("MP_DB", str(p))
    app = create_app(runner=scraper.Runner(session=FakeSession()))
    import db as dbm
    conn = dbm.connect()
    conn.execute("INSERT INTO users(email,pw,role,created) VALUES ('a@x.com',?,'admin',0)", (generate_password_hash("password123"),))
    conn.commit()
    cl = app.test_client()
    login(cl, "a@x.com")
    assert "Old Uni" in cl.get("/export/universities.csv").get_data(as_text=True)
