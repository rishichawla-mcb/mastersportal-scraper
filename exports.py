"""Flatten stored API records into CSV rows."""
import json

ORG_COLS = ["id", "name", "url", "country_code", "locations", "global_rank", "global_top_pct", "rating",
            "review_count", "masters_count", "delivery", "featured", "logo_url", "first_seen", "last_seen",
            "last_changed"]
PROG_COLS = ["id", "title", "url", "university", "university_id", "country_code", "locations", "degree",
             "attendance", "delivery", "duration", "tuition_amount", "tuition_currency", "tuition_period",
             "rating", "review_count", "featured", "first_seen", "last_seen", "last_changed"]


def _facts(r):
    return [x.get("value", "") for x in r.get("standardFacts") or []]


def _rating(r):
    rv = r.get("reviewRating") or {}
    return (round(rv["rating"], 1) if rv.get("rating") else ""), rv.get("amount", "")


def _ts(v):
    import datetime
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime("%Y-%m-%d") if v else ""


def org_row(row):
    r = json.loads(row["json"])
    fa = _facts(r)
    mc = next((x for x in fa if x.lower().endswith(("master", "masters"))), "")
    rk = r.get("ranking") or {}
    rating, reviews = _rating(r)
    logo = r.get("logoUrl") or ""
    return [r.get("id"), r.get("title"), r.get("url"), row["country"], " | ".join(r.get("locations") or []),
            rk.get("globalRankingPosition", ""), rk.get("globalRankingPercentage", ""), rating, reviews,
            "".join(ch for ch in mc if ch.isdigit()), "; ".join(x for x in fa if x != mc),
            "yes" if r.get("isFeatured") else "no", ("https:" + logo) if logo.startswith("//") else logo,
            _ts(row["first_seen"]), _ts(row["last_seen"]), _ts(row["last_changed"])]


def prog_row(row):
    r = json.loads(row["json"])
    fa = _facts(r) + ["", "", ""]
    t = r.get("tuition") or {}
    rating, reviews = _rating(r)
    return [r.get("id"), r.get("title"), r.get("url"), r.get("organisationName"), r.get("organisationId"),
            row["country"], " | ".join(r.get("locations") or []), fa[0], fa[1], fa[2], r.get("duration"),
            round(t["amount"]) if t.get("amount") else "", t.get("currency", ""), t.get("period", ""),
            rating, reviews, "yes" if r.get("isFeatured") else "no", _ts(row["first_seen"]), _ts(row["last_seen"]),
            _ts(row["last_changed"])]
