"""Bing Webmaster Tools (read-only) for the dashboard's Bing tab.

API key: Bing Webmaster Tools → Settings (gear) → API access → API key.
Stored as config "bing_api_key" (Mac) or the env var BING_API_KEY (cloud). Never returned by any tool.

Bing reports site clicks/impressions and crawl numbers per day, but query and page numbers
per week (each row's Date is the start of a week).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import data_dir

API = "https://ssl.bing.com/webmaster/api.svc/json/"


class BingError(Exception):
    pass


def api_key(cfg: dict) -> str | None:
    return (os.environ.get("BING_API_KEY") or cfg.get("bing_api_key") or "").strip() or None


def _ssl_context():
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _call(method: str, key: str, **params):
    q = urllib.parse.urlencode({**params, "apikey": key})
    req = urllib.request.Request(API + method + "?" + q, headers={"Accept": "application/json", "User-Agent": "intellectyx-seo-dashboard"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:
                body = json.loads(r.read() or b"{}")
            return body.get("d", body)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code in (400, 401, 403):
                raise BingError(f"Bing refused {method} ({e.code}). Check the API key and that the site URL is verified in Bing Webmaster Tools. {msg}")
            raise BingError(f"Bing {method} -> {e.code}: {msg}")
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise BingError(f"Cannot reach Bing: {e}")


_DATE = re.compile(r"/Date\((-?\d+)")


def _date(v) -> str | None:
    """Bing dates look like /Date(1700000000000-0800)/."""
    if not v:
        return None
    m = _DATE.search(str(v))
    if not m:
        return str(v)[:10]
    return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.timezone.utc).date().isoformat()


def _num(v):
    try:
        return round(float(v), 2) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def site_url(site: dict) -> str:
    return site.get("bing_site_url") or site.get("url") or ""


def pull(cfg: dict, site: dict) -> dict:
    """Everything the Bing tab shows, in one snapshot (a few small API calls)."""
    key = api_key(cfg)
    if not key:
        raise BingError("No Bing API key configured.")
    su = site_url(site)
    out: dict = {"site_url": su, "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "errors": []}

    def safe(name, fn):
        try:
            return fn()
        except BingError as e:
            out["errors"].append({"call": name, "error": str(e)})
            return None

    tr = safe("GetRankAndTrafficStats", lambda: _call("GetRankAndTrafficStats", key, siteUrl=su)) or []
    out["daily"] = sorted(({"date": _date(r.get("Date")), "clicks": int(r.get("Clicks") or 0), "impressions": int(r.get("Impressions") or 0)}
                           for r in tr if _date(r.get("Date"))), key=lambda r: r["date"])

    def weekly(rows, field):
        return [{field: r.get("Query"), "week": _date(r.get("Date")), "clicks": int(r.get("Clicks") or 0),
                 "impressions": int(r.get("Impressions") or 0),
                 "avg_click_position": _num(r.get("AvgClickPosition")), "avg_impression_position": _num(r.get("AvgImpressionPosition"))}
                for r in rows or [] if r.get("Query")]
    out["queries"] = weekly(safe("GetQueryStats", lambda: _call("GetQueryStats", key, siteUrl=su)), "query")
    out["pages"] = weekly(safe("GetPageStats", lambda: _call("GetPageStats", key, siteUrl=su)), "page")

    cs = safe("GetCrawlStats", lambda: _call("GetCrawlStats", key, siteUrl=su)) or []
    out["crawl"] = sorted(({"date": _date(r.get("Date")), "crawled_pages": r.get("CrawledPages"), "crawl_errors": r.get("CrawlErrors"),
                            "in_index": r.get("InIndex"), "in_links": r.get("InLinks"), "code_2xx": r.get("Code2xx"),
                            "code_301": r.get("Code301"), "code_302": r.get("Code302"), "code_4xx": r.get("Code4xx"),
                            "code_5xx": r.get("Code5xx"), "blocked_by_robots": r.get("BlockedByRobotsTxt"),
                            "contains_malware": r.get("ContainsMalware")} for r in cs if _date(r.get("Date"))), key=lambda r: r["date"])

    issues = safe("GetCrawlIssues", lambda: _call("GetCrawlIssues", key, siteUrl=su)) or []
    out["crawl_issues"] = [{"url": r.get("Url"), "http_code": r.get("HttpCode"), "issues": r.get("Issues"), "in_links": r.get("InLinks")}
                           for r in issues][:2000]

    feeds = safe("GetFeeds", lambda: _call("GetFeeds", key, siteUrl=su)) or []
    out["sitemaps"] = [{"url": r.get("Url"), "status": r.get("Status"), "last_crawled": _date(r.get("LastCrawled")),
                        "submitted": _date(r.get("Submitted")), "url_count": r.get("UrlCount"), "type": r.get("Type")} for r in feeds]
    if not (out["daily"] or out["queries"] or out["pages"]) and out["errors"]:
        raise BingError(out["errors"][0]["error"])
    return out


def snapshot_file(cfg: dict, site_id: str) -> Path:
    p = data_dir(cfg) / "daily"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{site_id}.bing.json"


def refresh(cfg: dict, site: dict, errors: list) -> dict | None:
    if not api_key(cfg):
        return None
    try:
        snap = pull(cfg, site)
    except BingError as e:
        errors.append({"source": "Bing", "table": "all", "error": str(e)})
        return None
    for e in snap.get("errors", []):
        errors.append({"source": "Bing", "table": e["call"], "error": e["error"]})
    snapshot_file(cfg, site["id"]).write_text(json.dumps(snap, separators=(",", ":")))
    return snap


def load(cfg: dict, site_id: str) -> dict | None:
    f = snapshot_file(cfg, site_id)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None
