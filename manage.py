#!/usr/bin/env python3
"""Command-line admin: create users and reset passwords.

    python manage.py create-user you@example.com --role admin
    python manage.py reset-password you@example.com
    python manage.py list-users
"""
import argparse
import getpass
import os
import sys
import time

from werkzeug.security import generate_password_hash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="path to the SQLite file (default: $MP_DB or ./mastersportal.sqlite)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("email")
    c.add_argument("--role", choices=["admin", "viewer"], default="admin")
    r = sub.add_parser("reset-password")
    r.add_argument("email")
    sub.add_parser("list-users")
    a = ap.parse_args()
    if a.db:
        os.environ["MP_DB"] = a.db
    import db as dbm  # after MP_DB is set
    dbm.init()
    conn = dbm.connect()

    def ask():
        pw = os.environ.get("MP_NEW_PASSWORD") or getpass.getpass("Password (10+ characters): ")
        if len(pw) < 10:
            sys.exit("Password must be at least 10 characters.")
        if not os.environ.get("MP_NEW_PASSWORD") and getpass.getpass("Repeat password: ") != pw:
            sys.exit("Passwords don't match.")
        return generate_password_hash(pw)

    if a.cmd == "create-user":
        if conn.execute("SELECT 1 FROM users WHERE email=?", (a.email,)).fetchone():
            sys.exit("That email already exists; use reset-password.")
        conn.execute("INSERT INTO users(email, pw, role, created) VALUES (?,?,?,?)", (a.email, ask(), a.role, time.time()))
        conn.commit()
        print(f"Created {a.role} {a.email}")
    elif a.cmd == "reset-password":
        if not conn.execute("SELECT 1 FROM users WHERE email=?", (a.email,)).fetchone():
            sys.exit("No such user.")
        conn.execute("UPDATE users SET pw=? WHERE email=?", (ask(), a.email))
        conn.commit()
        print(f"Password reset for {a.email}")
    else:
        for u in conn.execute("SELECT email, role FROM users ORDER BY email"):
            print(f"{u['role']:7} {u['email']}")


if __name__ == "__main__":
    main()
