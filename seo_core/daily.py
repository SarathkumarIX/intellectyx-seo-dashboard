"""Day-by-day GA4 + Search Console store for the dashboard's custom date range picker.

Each day is saved as <data_dir>/daily/<site_id>/<YYYY-MM-DD>.json with every table split by
date, so the dashboard can add up any start/end range in the browser. Sums are exact for
additive metrics (sessions, clicks, impressions, events, engagement time...). User counts are
stored per day; the dashboard only shows them for a single day (multi-day user counts cannot be
added up because the same person can visit on several days).
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import os
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import bing
from . import promptwatch
from . import google_api as g
from .config import data_dir, load_config

SEARCH_TYPES = ["image", "video", "news", "discover", "googleNews"]
CHUNK_DAYS = 7
# Days this recent are re-pulled on every refresh (Search Console and GA4 keep updating them).
REFRESH_RECENT_DAYS = 4

GA_TOTAL_METRICS = ["sessions", "engagedSessions", "userEngagementDuration", "screenPageViews", "eventCount",
                    "newUsers", "activeUsers", "totalUsers", "averageSessionDuration"]


def _iso(d: dt.date) -> str:
    return d.isoformat()


def site_dir(cfg: dict, site_id: str) -> Path:
    p = data_dir(cfg) / "daily" / site_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ga_all(cfg, pid, start, end, metrics, dims, filter_spec=None, page=100000):
    rows, off = [], 0
    while True:
        batch = g.ga4_report(cfg, pid, start, end, metrics, dimensions=dims, filter_spec=filter_spec,
                             limit=page, offset=off)
        rows += batch
        if len(batch) < page:
            return rows
        off += page


def _split_by_date(rows: list[dict], date_key: str, fmt_ga: bool) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = r.pop(date_key)
        if fmt_ga:
            d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        out.setdefault(d, []).append(r)
    return out


def _pack(rows: list[dict], cols: list[str]) -> dict:
    return {"cols": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


def _ke_metric(cfg, pid, start, end) -> str:
    try:
        g.ga4_report(cfg, pid, start, end, ["keyEvents"])
        return "keyEvents"
    except Exception as e:
        if "keyEvents" in str(e):
            return "conversions"
        raise


def pull_ga(cfg: dict, site: dict, start: str, end: str, errors: list) -> tuple[dict[str, dict], str | None]:
    pid = site.get("ga4_property_id")
    if not pid:
        return {}, None
    ke = _ke_metric(cfg, pid, start, end)
    organic = {"field": "sessionDefaultChannelGroup", "match": "EXACT", "value": "Organic Search"}
    ai = {"field": "sessionSource", "match": "PARTIAL_REGEXP", "value": cfg["ai_source_regex"]}
    SM = ["activeUsers", "sessions", "engagedSessions", "userEngagementDuration", ke, "newUsers"]
    specs = {
        "totals": (["date"], GA_TOTAL_METRICS + [ke], None),
        "organic": (["date"], ["sessions", "engagedSessions", "userEngagementDuration", "screenPageViews", ke,
                               "newUsers", "activeUsers", "totalUsers", "averageSessionDuration"], organic),
        "channels": (["date", "sessionDefaultChannelGroup"], SM, None),
        "source_medium": (["date", "sessionSourceMedium"], SM, None),
        "new_vs_returning": (["date", "newVsReturning"], SM, None),
        "landing_pages": (["date", "landingPage"], SM, None),
        "organic_landing": (["date", "landingPage"], ["sessions", "engagedSessions", "activeUsers", "userEngagementDuration", ke], organic),
        "pages": (["date", "pagePath", "pageTitle"], ["screenPageViews", "activeUsers", "userEngagementDuration", "eventCount", ke], None),
        "countries": (["date", "country"], SM, None),
        "cities": (["date", "city", "country"], SM, None),
        "devices": (["date", "deviceCategory"], SM, None),
        "events": (["date", "eventName"], ["eventCount", "totalUsers", "activeUsers", ke], None),
        "ai": (["date", "sessionSource"], SM, ai),
    }
    days: dict[str, dict] = {}
    for name, (dims, mets, flt) in specs.items():
        try:
            rows = _ga_all(cfg, pid, start, end, mets, dims, flt)
        except Exception as e:
            errors.append({"source": "GA4", "table": name, "range": [start, end], "error": f"{type(e).__name__}: {e}"})
            continue
        for d, rs in _split_by_date(rows, "date", True).items():
            days.setdefault(d, {})[name] = _pack(rs, dims[1:] + mets)
    return days, ke


def pull_gsc(cfg: dict, site: dict, start: str, end: str, errors: list, qp_from: str | None = None) -> dict[str, dict]:
    prop = site.get("gsc_property")
    if not prop:
        return {}
    specs = {"totals": ["date"], "queries": ["date", "query"], "pages": ["date", "page"],
             "countries": ["date", "country"], "devices": ["date", "device"]}
    days: dict[str, dict] = {}
    cols = ["clicks", "impressions", "position"]
    for name, dims in specs.items():
        try:
            rows = g.gsc_query(cfg, prop, start, end, dimensions=dims, row_limit=1_000_000)
        except Exception as e:
            errors.append({"source": "GSC", "table": name, "range": [start, end], "error": f"{type(e).__name__}: {e}"})
            continue
        for r in rows:
            r.pop("ctr", None)
            r["position"] = round(r.get("position") or 0, 2)
        for d, rs in _split_by_date(rows, "date", False).items():
            days.setdefault(d, {})[name] = _pack(rs, dims[1:] + cols)
    # Which URL ranked for which query (kept for the most recent query_page_days only: it is the largest table)
    if qp_from and end >= qp_from:
        s0 = max(start, qp_from)
        try:
            rows = g.gsc_query(cfg, prop, s0, end, dimensions=["date", "query", "page"], row_limit=5_000_000)
            for r in rows:
                r.pop("ctr", None)
                r["position"] = round(r.get("position") or 0, 1)
            for d, rs in _split_by_date(rows, "date", False).items():
                days.setdefault(d, {})["query_page"] = _pack(rs, ["query", "page"] + cols)
        except Exception as e:
            errors.append({"source": "GSC", "table": "query_page", "range": [s0, end], "error": f"{type(e).__name__}: {e}"})
    # Search appearance cannot be grouped with the date, so it is pulled one day at a time.
    d = dt.date.fromisoformat(start)
    while d <= dt.date.fromisoformat(end):
        k = d.isoformat()
        try:
            rows = g.gsc_query(cfg, prop, k, k, dimensions=["searchAppearance"], row_limit=1000)
            days.setdefault(k, {})["appearance"] = {"cols": ["searchAppearance"] + cols, "rows": [
                [r["searchAppearance"], r["clicks"], r["impressions"], round(r.get("position") or 0, 2)] for r in rows]}
        except Exception:
            pass
        d += dt.timedelta(days=1)
    for t in SEARCH_TYPES:
        try:
            rows = g.gsc_query(cfg, prop, start, end, dimensions=["date"], search_type=t, row_limit=1000)
        except Exception:
            continue  # search type not available for this property
        for r in rows:
            if r.get("impressions"):
                days.setdefault(r["date"], {}).setdefault("search_types", {"cols": ["type"] + cols, "rows": []})["rows"].append(
                    [t, r["clicks"], r["impressions"], round(r.get("position") or 0, 2)])
    return days


def exact_users_file(cfg: dict, site_id: str) -> Path:
    return data_dir(cfg) / "daily" / f"{site_id}.exact_users.json"


def pull_exact_users(cfg: dict, site: dict, last: dt.date, errors: list) -> dict:
    """Unique users can't be added up day by day, so pull the exact totals from GA4 for the
    standard rolling views: last 7 / 28 days up to `last` and the periods just before them."""
    pid = site.get("ga4_property_id")
    if not pid:
        return {}
    organic = {"field": "sessionDefaultChannelGroup", "match": "EXACT", "value": "Organic Search"}
    out: dict[str, dict] = {}
    for n in (7, 28):
        for k in (0, 1):  # current period, previous period
            end = last - dt.timedelta(days=n * k)
            start = end - dt.timedelta(days=n - 1)
            key = f"{_iso(start)}_{_iso(end)}"
            rec = {}
            for name, flt in (("all", None), ("organic", organic)):
                try:
                    rows = g.ga4_report(cfg, pid, _iso(start), _iso(end), ["totalUsers", "activeUsers"], filter_spec=flt, limit=1)
                    r = rows[0] if rows else {}
                    rec[name] = {"totalUsers": int(r.get("totalUsers") or 0), "activeUsers": int(r.get("activeUsers") or 0)}
                except Exception as e:
                    errors.append({"source": "GA4", "table": "exact_users", "range": [_iso(start), _iso(end)], "error": f"{type(e).__name__}: {e}"})
            if rec:
                out[key] = rec
    return out


def gsc_day_closed(d: dt.date) -> bool:
    """Search Console counts days in US Pacific time; a day's numbers are near-final ~2 h after PT midnight
    (about 3:30 PM IST in winter, 2:30 PM IST in summer). Before that the latest day is still filling in."""
    close = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(2, 0), ZoneInfo("America/Los_Angeles"))
    return dt.datetime.now(dt.timezone.utc) >= close


def refresh(site_ids: list[str] | None = None, days: int | None = None, force: bool = False,
            progress: dict | None = None) -> dict:
    """Pull every missing day (and re-pull the last few days) for the configured window."""
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    window = int(days or cfg.get("daily_window_days", 112))
    yesterday = dt.datetime.now(tz).date() - dt.timedelta(days=1)
    first = yesterday - dt.timedelta(days=window - 1)
    qp_first = yesterday - dt.timedelta(days=int(cfg.get("query_page_days", 56)) - 1)
    sites = [s for s in cfg["sites"] if (s["id"] in site_ids if site_ids else s.get("enabled", True))]
    summary = {"window": [_iso(first), _iso(yesterday)], "sites": {}}
    for site in sites:
        sdir = site_dir(cfg, site["id"])
        need = []
        d = first
        while d <= yesterday:
            f = sdir / f"{_iso(d)}.json"
            stale = True
            if f.exists() and not force:
                try:
                    rec0 = json.loads(f.read_text())
                    pulled = dt.date.fromisoformat(rec0.get("pulled_on", "1970-01-01"))
                    stale = (pulled - d).days < REFRESH_RECENT_DAYS
                    if rec0.get("gsc_closed") is False:
                        stale = True
                    # days in the query→URL window that were saved before that table existed
                    if not stale and d >= qp_first and site.get("gsc_property") and "query_page" not in (rec0.get("gsc") or {}):
                        stale = True
                except Exception:
                    stale = True
            if stale:
                need.append(d)
            d += dt.timedelta(days=1)
        errors: list = []
        written = 0
        # contiguous chunks of up to CHUNK_DAYS
        chunks, cur = [], []
        for d in need:
            if cur and ((d - cur[-1]).days != 1 or len(cur) >= CHUNK_DAYS):
                chunks.append(cur)
                cur = []
            cur.append(d)
        if cur:
            chunks.append(cur)
        for i, ch in enumerate(chunks):
            if progress is not None:
                progress.update(site=site["id"], chunk=i + 1, chunks=len(chunks), range=[_iso(ch[0]), _iso(ch[-1])])
            s, e = _iso(ch[0]), _iso(ch[-1])
            n_err = len(errors)
            ga, ke = pull_ga(cfg, site, s, e, errors)
            gs = pull_gsc(cfg, site, s, e, errors, qp_from=_iso(qp_first))
            if len(errors) > n_err and any(x["table"] == "totals" for x in errors[n_err:]):
                continue  # don't save a day whose totals failed; it will be retried next time
            today = dt.datetime.now(tz).date()
            for d in ch:
                k = _iso(d)
                rec = {"date": k, "pulled_on": _iso(today), "key_event_metric": ke,
                       "gsc_closed": gsc_day_closed(d), "ga": ga.get(k, {}), "gsc": gs.get(k, {})}
                (sdir / f"{k}.json").write_text(json.dumps(rec, separators=(",", ":"), default=str))
                written += 1
        bing.refresh(cfg, site, errors)  # Bing Webmaster Tools snapshot (only when a Bing API key is set)
        if os.environ.get("PROMPTWATCH_API_KEY") or cfg.get("promptwatch_refresh_here"):
            promptwatch.refresh(cfg, site, errors)  # Promptwatch AI-search snapshot (cloud runs; the Mac only on request)
        exact = pull_exact_users(cfg, site, yesterday, errors)
        if exact:
            exact_users_file(cfg, site["id"]).write_text(json.dumps({"pulled_on": _iso(dt.datetime.now(tz).date()), "ranges": exact}))
        summary["sites"][site["id"]] = {"days_pulled": written, "days_in_window": window, "errors": errors[:20]}
    return summary


# ------------------------------------------------------------------ dashboard payload
def payload(cfg: dict, site_ids: list[str]) -> str | None:
    """Compact, gzip+base64 encoded day-by-day data for the dashboard (None if nothing stored)."""
    window = int(cfg.get("daily_window_days", 112))
    out: dict[str, Any] = {"sites": {}, "brand_regex": cfg.get("brand_regex", ""),
                           "thresholds": cfg.get("thresholds", {}), "window_days": window}
    strings: dict[str, int] = {}
    slist: list[str] = []

    def sid(v):
        v = "" if v is None else str(v)
        i = strings.get(v)
        if i is None:
            i = strings[v] = len(slist)
            slist.append(v)
        return i

    for site_id in site_ids:
        sdir = data_dir(cfg) / "daily" / site_id
        files = sorted(sdir.glob("*.json"))[-window:] if sdir.exists() else []
        if not files:
            continue
        recs = []
        for f in files:
            try:
                recs.append(json.loads(f.read_text()))
            except Exception:
                continue
        days = [r["date"] for r in recs]
        site: dict[str, Any] = {"days": days, "pulled_on": [r.get("pulled_on") for r in recs],
                                "gsc_open": [r["date"] for r in recs if r.get("gsc_closed") is False],
                                "key_event_metric": next((r.get("key_event_metric") for r in reversed(recs) if r.get("key_event_metric")), "keyEvents"),
                                "ga": {}, "gsc": {}}
        for src in ("ga", "gsc"):
            for di, r in enumerate(recs):
                for tname, t in (r.get(src) or {}).items():
                    cols = t["cols"]
                    dst = site[src].setdefault(tname, {"cols": cols, "rows": []})
                    if dst["cols"] != cols:  # columns changed between pulls: map by name
                        idx = [cols.index(c) if c in cols else None for c in dst["cols"]]
                        rows = [[row[i] if i is not None else None for i in idx] for row in t["rows"]]
                    else:
                        rows = t["rows"]
                    for row in rows:
                        dst["rows"].append([di] + [sid(v) if isinstance(v, str) else v for v in row])
        bsnap = bing.load(cfg, site_id)
        if bsnap:
            site["bing"] = bsnap
        psnap = promptwatch.load(cfg, site_id)
        if psnap and psnap.get("version") == 2:
            site["promptwatch"] = {k: psnap.get(k) for k in ("end", "pulled_at", "auth", "models", "vis_daily", "texts", "bundles", "own_domains", "run", "windows", "first_data", "ai_visits_daily")}
        ef = exact_users_file(cfg, site_id)
        if ef.exists():
            try:
                site["exact_users"] = json.loads(ef.read_text()).get("ranges", {})
            except Exception:
                pass
        out["sites"][site_id] = site
    if not out["sites"]:
        return None
    out["strings"] = slist
    raw = json.dumps(out, separators=(",", ":"), default=str).encode()
    return base64.b64encode(gzip.compress(raw, 9)).decode()
