"""Thin wrappers around the Search Console and GA4 APIs (service-account auth).

All calls are read-only (webmasters.readonly / analytics.readonly scopes).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

_services: dict[str, Any] = {}
_override: dict[str, Any] = {}  # tests inject fake services here


def set_test_services(**services: Any) -> None:
    _override.clear()
    _override.update(services)
    _services.clear()


def _credentials(cfg: dict):
    from google.oauth2 import service_account

    path = Path(cfg["credentials_file"]).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"Service-account key not found at {path}. Run the seo-dashboard-setup skill "
            "or set credentials_file in the config."
        )
    return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)


def service_account_email(cfg: dict) -> str | None:
    import json

    path = Path(cfg["credentials_file"]).expanduser()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("client_email")
    except Exception:
        return None


def _svc(cfg: dict, name: str):
    if name in _override:
        return _override[name]
    # googleapiclient service objects are not thread-safe: keep one per thread
    key = (name, threading.get_ident())
    if key not in _services:
        from googleapiclient.discovery import build

        creds = _credentials(cfg)
        api, version = {
            "gsc": ("searchconsole", "v1"),
            "ga4data": ("analyticsdata", "v1beta"),
            "ga4admin": ("analyticsadmin", "v1beta"),
        }[name]
        import google_auth_httplib2
        import httplib2

        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=90))  # never hang forever
        _services[key] = build(api, version, http=http, cache_discovery=False)
    return _services[key]


def _execute(req, retries: int = 6):
    """Execute with exponential backoff on quota / transient errors (network timeouts included).
    Waits 2, 4, 8, 16, 30, 30 s between attempts, so a short network drop on the Mac is ridden out."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            return req.execute()
        except Exception as e:  # googleapiclient.errors.HttpError and network errors
            status = getattr(getattr(e, "resp", None), "status", None)
            transient = status in (429, 500, 502, 503, 504) or isinstance(e, (TimeoutError, ConnectionError, OSError))
            if attempt < retries and transient:
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            raise


# ---------------------------------------------------------------- Search Console
def gsc_sites(cfg: dict) -> list[dict]:
    res = _execute(_svc(cfg, "gsc").sites().list())
    return res.get("siteEntry", [])


def gsc_query(
    cfg: dict,
    prop: str,
    start: str,
    end: str,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    row_limit: int = 1000,
    search_type: str = "web",
    data_state: str = "all",
) -> list[dict]:
    """Search Analytics query. Paginates up to row_limit rows.

    Returns rows as dicts: {<dimension>: value, clicks, impressions, ctr, position}.
    """
    dimensions = dimensions or []
    rows: list[dict] = []
    start_row = 0
    page = min(row_limit, 25000)
    while True:
        body: dict[str, Any] = {
            "startDate": start,
            "endDate": end,
            "dimensions": dimensions,
            "rowLimit": page,
            "startRow": start_row,
            "type": search_type,
            "dataState": data_state,
        }
        if filters:
            body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
        res = _execute(_svc(cfg, "gsc").searchanalytics().query(siteUrl=prop, body=body))
        batch = res.get("rows", [])
        for r in batch:
            d = {dim: r["keys"][i] for i, dim in enumerate(dimensions)} if dimensions else {}
            d.update(
                clicks=r.get("clicks", 0),
                impressions=r.get("impressions", 0),
                ctr=r.get("ctr", 0.0),
                position=r.get("position", 0.0),
            )
            rows.append(d)
        start_row += len(batch)
        if len(batch) < page or start_row >= row_limit:
            break
    return rows[:row_limit]


def gsc_sitemaps(cfg: dict, prop: str) -> list[dict]:
    res = _execute(_svc(cfg, "gsc").sitemaps().list(siteUrl=prop))
    return res.get("sitemap", [])


def gsc_inspect(cfg: dict, prop: str, url: str) -> dict:
    body = {"inspectionUrl": url, "siteUrl": prop, "languageCode": "en-US"}
    res = _execute(_svc(cfg, "gsc").urlInspection().index().inspect(body=body))
    r = res.get("inspectionResult", {})
    idx = r.get("indexStatusResult", {})
    rich = r.get("richResultsResult", {})
    return {
        "url": url,
        "verdict": idx.get("verdict"),
        "coverage_state": idx.get("coverageState"),
        "indexing_state": idx.get("indexingState"),
        "robots_txt_state": idx.get("robotsTxtState"),
        "page_fetch_state": idx.get("pageFetchState"),
        "last_crawl_time": idx.get("lastCrawlTime"),
        "google_canonical": idx.get("googleCanonical"),
        "user_canonical": idx.get("userCanonical"),
        "rich_results_verdict": rich.get("verdict"),
        "rich_results_items": [
            {"type": d.get("richResultType"), "issues": [i.get("issueMessage") for it in d.get("items", []) for i in it.get("issues", [])]}
            for d in rich.get("detectedItems", [])
        ],
        "link": r.get("inspectionResultLink"),
    }


# ---------------------------------------------------------------- GA4
def ga4_properties(cfg: dict) -> list[dict]:
    out = []
    req = _svc(cfg, "ga4admin").accountSummaries().list(pageSize=200)
    res = _execute(req)
    for acc in res.get("accountSummaries", []):
        for p in acc.get("propertySummaries", []):
            out.append(
                {
                    "account": acc.get("displayName"),
                    "property_id": p.get("property", "").split("/")[-1],
                    "property_name": p.get("displayName"),
                }
            )
    return out


def _ga4_filter(spec: dict | None) -> dict | None:
    """Simple filter spec -> GA4 FilterExpression.

    spec: {"field": "sessionDefaultChannelGroup", "match": "EXACT"|"CONTAINS"|"BEGINS_WITH"|
           "FULL_REGEXP"|"PARTIAL_REGEXP", "value": "Organic Search", "not": false}
    or {"and": [spec, ...]}
    """
    if not spec:
        return None
    if "and" in spec:
        return {"andGroup": {"expressions": [_ga4_filter(s) for s in spec["and"]]}}
    expr = {
        "filter": {
            "fieldName": spec["field"],
            "stringFilter": {
                "matchType": spec.get("match", "EXACT"),
                "value": spec["value"],
                "caseSensitive": False,
            },
        }
    }
    return {"notExpression": expr} if spec.get("not") else expr


def ga4_report(
    cfg: dict,
    property_id: str,
    start: str,
    end: str,
    metrics: list[str],
    dimensions: list[str] | None = None,
    filter_spec: dict | None = None,
    limit: int = 1000,
    order_by_metric: str | None = None,
    offset: int = 0,
) -> list[dict]:
    body: dict[str, Any] = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "metrics": [{"name": m} for m in metrics],
        "dimensions": [{"name": d} for d in (dimensions or [])],
        "limit": limit,
        "keepEmptyRows": False,
    }
    if offset:
        body["offset"] = offset
    f = _ga4_filter(filter_spec)
    if f:
        body["dimensionFilter"] = f
    if order_by_metric:
        body["orderBys"] = [{"metric": {"metricName": order_by_metric}, "desc": True}]
    res = _execute(
        _svc(cfg, "ga4data").properties().runReport(property=f"properties/{property_id}", body=body)
    )
    dim_names = [h["name"] for h in res.get("dimensionHeaders", [])]
    met_names = [h["name"] for h in res.get("metricHeaders", [])]
    rows = []
    for r in res.get("rows", []):
        d: dict[str, Any] = {n: v.get("value") for n, v in zip(dim_names, r.get("dimensionValues", []))}
        for n, v in zip(met_names, r.get("metricValues", [])):
            val = v.get("value")
            try:
                d[n] = float(val) if val is not None and ("." in val or "e" in val.lower()) else int(val)
            except (TypeError, ValueError):
                d[n] = val
        rows.append(d)
    if not dimensions and not rows:
        rows = [{m: 0 for m in metrics}]
    return rows
