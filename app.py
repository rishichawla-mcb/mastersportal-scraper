"""Mastersportal scraper web app: login, roles, scrape control, browse, CSV export, user admin."""
import csv
import io
import json
import os
import secrets
import time
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, session,
                   stream_with_context, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db as dbm
from exports import ORG_COLS, PROG_COLS, org_row, prog_row
from scraper import Runner

PER_PAGE = 50
MAX_FAILS, LOCK_SECONDS = 5, 600


def create_app(runner=None):
    app = Flask(__name__)
    key = os.environ.get("MP_SECRET_KEY")
    if not key:
        raise RuntimeError("Set MP_SECRET_KEY (a long random string).")
    app.config.update(
        SECRET_KEY=key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("MP_COOKIE_SECURE", "1") == "1",
        PERMANENT_SESSION_LIFETIME=12 * 3600,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    dbm.init()
    app.runner = runner or Runner()
    fails = {}  # ip -> [count, first_fail_ts]

    # ---- helpers -------------------------------------------------------
    def db():
        if "db" not in g:
            g.db = dbm.connect()
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c:
            c.close()

    def current_user():
        uid = session.get("uid")
        if not uid:
            return None
        return db().execute("SELECT id, email, role FROM users WHERE id=?", (uid,)).fetchone()

    def login_required(fn):
        @wraps(fn)
        def wrap(*a, **kw):
            g.user = current_user()
            if not g.user:
                session.clear()
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return wrap

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrap(*a, **kw):
            if g.user["role"] != "admin":
                abort(403)
            return fn(*a, **kw)
        return wrap

    @app.before_request
    def csrf_protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]):
                abort(400, "Form expired. Go back, reload the page and try again.")

    @app.context_processor
    def inject():
        return {"csrf": session.get("csrf", ""), "user": g.get("user"),
                "fmt_ts": lambda t: time.strftime("%d %b %H:%M", time.localtime(t)) if t else "—"}

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    # ---- auth ----------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ip, now = request.remote_addr or "?", time.time()
            cnt, first = fails.get(ip, [0, now])
            if now - first >= LOCK_SECONDS:
                cnt, first = 0, now
            if cnt >= MAX_FAILS:
                flash("Too many failed attempts. Try again in 10 minutes.", "error")
                return render_template("login.html"), 429
            email = request.form.get("email", "").strip()
            u = db().execute("SELECT id, pw FROM users WHERE email=?", (email,)).fetchone()
            if u and check_password_hash(u["pw"], request.form.get("password", "")):
                fails.pop(ip, None)
                csrf = session.get("csrf")
                session.clear()
                session.permanent = True
                session["uid"], session["csrf"] = u["id"], csrf or secrets.token_hex(16)
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
            fails[ip] = [cnt + 1, first]
            flash("Wrong email or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        if request.method == "POST":
            u = db().execute("SELECT pw FROM users WHERE id=?", (g.user["id"],)).fetchone()
            new = request.form.get("new", "")
            if not check_password_hash(u["pw"], request.form.get("current", "")):
                flash("Current password is wrong.", "error")
            elif len(new) < 10:
                flash("New password must be at least 10 characters.", "error")
            else:
                db().execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
                db().commit()
                flash("Password changed.", "ok")
        return render_template("account.html")

    # ---- dashboard & jobs ---------------------------------------------
    @app.get("/")
    @login_required
    def dashboard():
        c = db()
        counts = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) n FROM rec GROUP BY kind")}
        jobs = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 10").fetchall()
        current = jobs[0] if jobs else None
        logs = c.execute("SELECT ts, line FROM log WHERE job_id=? ORDER BY id DESC LIMIT 40",
                         (current["id"],)).fetchall() if current else []
        countries = c.execute(
            "SELECT country, SUM(kind='progs') progs, SUM(kind='orgs') orgs FROM rec "
            "GROUP BY country ORDER BY progs DESC, orgs DESC LIMIT 40").fetchall()
        return render_template("dashboard.html", counts=counts, jobs=jobs, current=current, logs=logs,
                               countries=countries, running=app.runner.is_running())

    @app.post("/jobs/start")
    @admin_required
    def job_start():
        try:
            app.runner.start(request.form.get("kind", "both"), request.form.get("delay", "4"),
                             fresh=request.form.get("fresh") == "1", user=g.user["email"])
            flash("Scrape started.", "ok")
        except (RuntimeError, ValueError) as e:
            flash(str(e), "error")
        return redirect(url_for("dashboard"))

    @app.post("/jobs/stop")
    @admin_required
    def job_stop():
        app.runner.stop()
        flash("Stopping after the current request. Progress is saved.", "ok")
        return redirect(url_for("dashboard"))

    # ---- browse --------------------------------------------------------
    def browse(kind, title_key, cols):
        q = request.args.get("q", "").strip()
        cc = request.args.get("country", "").strip().upper()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        where, args = ["kind=?"], [kind]
        if q:
            where.append(f"(json_extract(json, '$.title') LIKE ? OR json_extract(json, '$.{title_key}') LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if cc:
            where.append("country=?")
            args.append(cc)
        w = " AND ".join(where)
        c = db()
        total = c.execute(f"SELECT COUNT(*) FROM rec WHERE {w}", args).fetchone()[0]
        rows = c.execute(f"SELECT country, json, first_seen, last_seen FROM rec WHERE {w} "
                         f"ORDER BY CAST(id AS INTEGER) LIMIT ? OFFSET ?", args + [PER_PAGE, (page - 1) * PER_PAGE])
        fn = org_row if kind == "orgs" else prog_row
        flat = [dict(zip(cols, fn(r))) for r in rows]
        countries = [r[0] for r in c.execute("SELECT DISTINCT country FROM rec WHERE kind=? ORDER BY country", (kind,))]
        return dict(rows=flat, cols=cols, total=total, page=page, pages=max(1, -(-total // PER_PAGE)),
                    q=q, country=cc, countries=countries)

    @app.get("/programmes")
    @login_required
    def programmes():
        show = ["id", "title", "university", "country_code", "degree", "delivery", "duration",
                "tuition_amount", "tuition_currency", "tuition_period"]
        d = browse("progs", "organisationName", PROG_COLS)
        return render_template("browse.html", title="Programmes", kind="programmes", show=show, **d)

    @app.get("/universities")
    @login_required
    def universities():
        show = ["id", "name", "country_code", "locations", "global_rank", "rating", "review_count", "masters_count"]
        d = browse("orgs", "title", ORG_COLS)
        return render_template("browse.html", title="Universities", kind="universities", show=show, **d)

    @app.get("/export/<kind>.csv")
    @login_required
    def export(kind):
        if kind not in ("programmes", "universities"):
            abort(404)
        k, cols, fn = ("progs", PROG_COLS, prog_row) if kind == "programmes" else ("orgs", ORG_COLS, org_row)
        cc = request.args.get("country", "").strip().upper()

        def gen():
            c = dbm.connect()
            try:
                buf = io.StringIO()
                w = csv.writer(buf)
                buf.write("﻿")
                w.writerow(cols)
                sql, args = "SELECT country, json, first_seen, last_seen FROM rec WHERE kind=?", [k]
                if cc:
                    sql, args = sql + " AND country=?", args + [cc]
                for i, row in enumerate(c.execute(sql + " ORDER BY CAST(id AS INTEGER)", args)):
                    w.writerow(fn(row))
                    if i % 500 == 0:
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate()
                yield buf.getvalue()
            finally:
                c.close()

        name = f"mastersportal_{kind}{'_' + cc if cc else ''}_{time.strftime('%Y-%m-%d')}.csv"
        return Response(stream_with_context(gen()), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---- users ---------------------------------------------------------
    @app.route("/users", methods=["GET", "POST"])
    @admin_required
    def users():
        c = db()
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                email, pw = request.form.get("email", "").strip(), request.form.get("password", "")
                role = request.form.get("role", "viewer")
                if "@" not in email or len(pw) < 10 or role not in ("admin", "viewer"):
                    flash("Enter a valid email, a password of 10+ characters and a role.", "error")
                elif c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    flash("That email already has an account.", "error")
                else:
                    c.execute("INSERT INTO users(email, pw, role, created) VALUES (?,?,?,?)",
                              (email, generate_password_hash(pw), role, time.time()))
                    c.commit()
                    flash(f"Added {email}.", "ok")
            elif action in ("delete", "reset", "role"):
                uid = int(request.form.get("uid", 0))
                if uid == g.user["id"] and action != "reset":
                    flash("You can't delete or demote your own account.", "error")
                elif action == "delete":
                    c.execute("DELETE FROM users WHERE id=?", (uid,))
                    c.commit()
                    flash("User deleted.", "ok")
                elif action == "role":
                    role = request.form.get("role")
                    if role in ("admin", "viewer"):
                        c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
                        c.commit()
                        flash("Role changed.", "ok")
                else:
                    pw = request.form.get("password", "")
                    if len(pw) < 10:
                        flash("Password must be at least 10 characters.", "error")
                    else:
                        c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(pw), uid))
                        c.commit()
                        flash("Password reset.", "ok")
            return redirect(url_for("users"))
        rows = c.execute("SELECT id, email, role, created FROM users ORDER BY email").fetchall()
        return render_template("users.html", users=rows)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "running": app.runner.is_running()}

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("message.html", msg="Only admins can do that."), 403

    return app
