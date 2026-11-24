"""Promptwatch (AI search visibility / AEO / GEO) for the dashboard's "AI Search" tab. Read-only.

Mirrors the team's "IX AI Visibility" daily / weekly e-mail: brand rank per prompt, won / lost, gaps, close to #1,
7-day tracker, LLM-wise view, our cited pages, AI referral clicks, visibility by LLM and brand comparison.

API: https://server.promptwatch.com/api/v2 , header X-API-Key (Promptwatch -> Settings -> API Keys).
Organization keys also need X-Project-Id (site config "promptwatch_project_id", or picked automatically when the
key sees exactly one project / one project whose website matches the site).
Key: config "promptwatch_api_key" (Mac) or env PROMPTWATCH_API_KEY (cloud). Never returned by any tool.

Every value is calculated by Promptwatch. Brand ranks only exist for a whole window, so the pull asks Promptwatch
for each window the dashboard can show ("bundles"): every UTC day, every Mon-Sun week, the last 7 / 28 days and
the 7 / 28 days before them. Finished windows are kept, so each run only asks for new ones. Dates are UTC days,
matching Promptwatch. A run stops early when the hourly API quota runs low and continues on the next run.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import data_dir

API = "https://server.promptwatch.com/api/v2"
KEEP_DAYS = 35     # day bundles kept (Day view + 7-day tracker)
KEEP_WEEKS = 12    # Mon-Sun week bundles kept
TRACK_DAYS = 14    # days that always get at least the brand ranks (tracker + "vs prev. day")
REFERRERS = ["chatgpt.com", "gemini.google.com", "perplexity.ai", "copilot.microsoft.com", "copilot.com", "m365.cloud.microsoft",
             "copilot.cloud.microsoft", "claude.ai", "claude.com", "grok.com", "chat.mistral.ai", "mistral.ai",
             "chat.deepseek.com", "deepseek.com", "kimi.com"]


class PromptwatchError(Exception):
    pass


class OutOfQuota(PromptwatchError):
    pass


def api_key(cfg: dict) -> str | None:
    return (os.environ.get("PROMPTWATCH_API_KEY") or cfg.get("promptwatch_api_key") or "").strip() or None


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


class Client:
    def __init__(self, key: str, project_id: str | None = None, max_calls: int = 1500, reserve: int = 15):
        self.key, self.project_id = key, project_id
        self.calls, self.max_calls, self.reserve = 0, max_calls, reserve
        self.limit = self.remaining = self.reset = None
        self._last = 0.0
        self.ctx = _ssl_context()

    def ok(self, need: int = 1) -> bool:
        """Enough quota left for `need` more calls in this run?"""
        if self.calls + need > self.max_calls:
            return False
        return self.remaining is None or self.remaining - need >= self.reserve

    def get(self, _path: str, **params):
        path = _path
        if not self.ok():
            raise OutOfQuota("Promptwatch API quota for this run is used up; the rest is fetched on the next run.")
        q = []
        for k, v in params.items():
            if v is None:
                continue
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                q.append((k, ("true" if x else "false") if isinstance(x, bool) else str(x)))
        url = API + path + ("?" + urllib.parse.urlencode(q) if q else "")
        h = {"X-API-Key": self.key, "Accept": "application/json", "User-Agent": "intellectyx-seo-dashboard"}
        if self.project_id:
            h["X-Project-Id"] = self.project_id
        for attempt in range(5):
            wait = 0.12 - (time.time() - self._last)  # stay under the per-second burst limit
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120, context=self.ctx) as r:
                    self._quota(r.headers)
                    raw = r.read()
                    return json.loads(raw) if raw and r.status != 204 else None
            except urllib.error.HTTPError as e:
                self._quota(e.headers)
                msg = e.read().decode(errors="replace")[:300]
                if e.code == 429:
                    ra = int(float(e.headers.get("Retry-After") or 60))
                    if ra <= 65 and attempt < 4:  # per-second / per-minute burst: wait it out
                        time.sleep(ra + 1)
                        continue
                    self.remaining = 0
                    raise OutOfQuota(f"Promptwatch hourly API quota reached; the rest is fetched on the next run (resets in about {max(1, ra // 60)} min).")
                if e.code in (502, 503, 504) and attempt < 4:
                    time.sleep(3 * (attempt + 1))
                    continue
                if e.code == 401:
                    raise PromptwatchError("Promptwatch refused the API key (401). Create a new key in Promptwatch -> Settings -> API Keys.")
                if e.code == 403:
                    raise PromptwatchError(f"Promptwatch 403 on {path}: the key needs a project (X-Project-Id) or lacks access. {msg}")
                raise PromptwatchError(f"Promptwatch {path} -> {e.code}: {msg}")
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise PromptwatchError(f"Cannot reach Promptwatch: {e}")

    def _quota(self, hd):
        try:
            if hd and hd.get("X-RateLimit-Limit") is not None:
                self.limit = int(float(hd.get("X-RateLimit-Limit")))
                self.remaining = int(float(hd.get("X-RateLimit-Remaining")))
                self.reset = hd.get("X-RateLimit-Reset")
        except (TypeError, ValueError):
            pass


# ------------------------------------------------------------------ helpers
def _host(u: str | None) -> str:
    u = (u or "").strip().lower()
    h = urllib.parse.urlparse(u).netloc if "//" in u else u.split("/")[0]
    return h[4:] if h.startswith("www.") else h


def _r2(v):
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _iso(d: dt.date) -> str:
    return d.isoformat()


def own_domains(site: dict) -> list[str]:
    doms = site.get("promptwatch_own_domains") or [_host(site.get("url"))]
    out = []
    for d in doms:
        d = _host(d)
        if d:
            out += [d, "www." + d]
    return out


def resolve_project(cl: Client, site: dict) -> dict:
    """Validate the key and pick the project. Returns auth info (no secrets)."""
    v = cl.get("/auth/validate") or {}
    info = {"key_type": v.get("keyType"), "permission": (v.get("apiKey") or {}).get("permission"),
            "organization": (v.get("organization") or {}).get("name")}
    if v.get("keyType") == "project" or v.get("project"):
        p = v.get("project") or {}
        info["project"] = {"id": p.get("id"), "name": p.get("name")}
        return info
    want = site.get("promptwatch_project_id")
    projs = v.get("availableProjects") or []
    if not want:
        if not projs:
            projs = (cl.get("/projects") or {}).get("projects") or []
        info["available_projects"] = [{"id": p.get("id"), "name": p.get("name"), "website": p.get("website")} for p in projs]
        dom = _host(site.get("url"))
        match = [p for p in projs if dom and _host(p.get("website")) == dom]
        if len(match) == 1:
            want = match[0]["id"]
        elif len(projs) == 1:
            want = projs[0]["id"]
        else:
            raise PromptwatchError("This organization key sees several Promptwatch projects; set promptwatch_project_id for the site. Projects: "
                                   + ", ".join(f"{p.get('name')} ({p.get('website') or p.get('slug')}, id {p.get('id')})" for p in projs))
    cl.project_id = want
    p = (cl.get("/auth/validate") or {}).get("project") or {}
    info["project"] = {"id": p.get("id") or want, "name": p.get("name")}
    return info


# ------------------------------------------------------------------ windows
def plan_windows(end: dt.date) -> dict:
    """All windows the dashboard can show, keyed 'start_end'."""
    k = lambda a, b: f"{_iso(a)}_{_iso(b)}"
    days = [end - dt.timedelta(days=i) for i in range(KEEP_DAYS)]
    last_sun = end - dt.timedelta(days=(end.weekday() + 1) % 7)  # Sunday on/before end
    weeks = [(last_sun - dt.timedelta(days=6 + 7 * i), last_sun - dt.timedelta(days=7 * i)) for i in range(KEEP_WEEKS + 1)]
    r = {n: (end - dt.timedelta(days=n - 1), end) for n in (7, 28)}
    p = {n: (end - dt.timedelta(days=2 * n - 1), end - dt.timedelta(days=n)) for n in (7, 28)}
    return {"days": [k(d, d) for d in days], "weeks": [k(a, b) for a, b in weeks],
            "r7": k(*r[7]), "p7": k(*p[7]), "r28": k(*r[28]), "p28": k(*p[28])}


# ------------------------------------------------------------------ bundle pieces
def _brand_rank(cl: Client, a: str, b: str, model: str | None = None) -> dict:
    """{promptId: [[brandName, domain, relation, rank, score], ...]} plus prompt texts."""
    got, texts, page = {}, {}, 1
    while True:
        r = cl.get("/prompts/brand-rank", startDate=a, endDate=b, page=page, size=100, models=model) or {}
        for p in r.get("prompts") or []:
            texts[p["id"]] = [p.get("prompt"), p.get("type")]
            got[p["id"]] = [[x.get("name"), x.get("domain"), x.get("relation"), x.get("rank"), _r2(x.get("visibilityScore"))]
                            for x in p.get("brands") or []]
        if page >= int(r.get("totalPages") or 1) or page >= 20:
            break
        page += 1
    return {"ranks": got, "texts": texts}


def _self_rank(brands: list) -> list | None:
    """[rank, score] of our brand, 0 = gap (competitor ranked, we are not), -1 = no tracked brand mentioned."""
    if not brands:
        return [-1, None]
    for x in brands:
        if x[2] == "SELF":
            return [x[3], x[4]]
    return [0, None]


def _heat(cl: Client, a: str, b: str, metric: str, relations: list, prompt_id: str | None = None):
    h = cl.get("/performance-heatmap", startDate=a, endDate=b, rowAxis="brands", columnAxis="models", metric=metric,
               relations=relations, limit=20, promptId=prompt_id) or {}
    rows = {r.get("id"): [r.get("label"), r.get("domain"), r.get("relation")] for r in h.get("rows") or []}
    cols = [[c.get("id"), c.get("label")] for c in h.get("columns") or []]
    cells = [[c.get("rowId"), c.get("columnId"), _r2(c.get("value")), c.get("responseCount")] for c in h.get("cells") or []]
    return {"brands": rows, "models": cols, "cells": cells}


def _self_cells(h: dict) -> dict:
    self_ids = {k for k, v in h["brands"].items() if v[2] == "SELF"}
    return {c[1]: c[2] for c in h["cells"] if c[0] in self_ids}


def _grouped(cl: Client, a: str, b: str, domains: list, size: int, prompt_id: str | None = None) -> dict:
    r = cl.get("/citations/grouped", startDate=a, endDate=b, domains=domains, promptId=prompt_id, size=size, page=1,
               sortBy="count", sortOrder="desc") or {}
    rows = []
    for x in r.get("data") or []:
        u = x.get("url") or {}
        rows.append([u.get("url"), u.get("title"), x.get("count"), _r2(x.get("citationShare")), _r2(x.get("avgPosition")),
                     x.get("minPosition"), x.get("maxPosition"), x.get("models") or [], x.get("totalModels")])
    return {"total": (r.get("pagination") or {}).get("total"), "rows": rows}


def build_bundle(cl: Client, a: str, b: str, tier: str, site: dict, tz: str, prev_bundle: dict | None = None) -> dict:
    """One window. 'light' = what a comparison needs; 'full' = everything the e-mail shows."""
    B: dict = {"w": [a, b], "tier": tier, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    s = cl.get("/responses/summary", startDate=a, endDate=b) or {}
    B["sum"] = [s.get("totalResponses"), s.get("totalBrandMentions"), _r2(s.get("brandMentionRate"))]
    br = _brand_rank(cl, a, b)
    B["rank"], B["texts"] = br["ranks"], br["texts"]
    vh = _heat(cl, a, b, "visibility", ["SELF", "DIRECT_COMPETITOR"])
    B["vis_llm"] = vh
    if tier == "light":
        return B
    models = [m[0] for m in vh["models"]]
    B["pos_llm"] = _self_cells(_heat(cl, a, b, "position", ["SELF"]))
    # brand rank on each LLM (one request per LLM)
    B["rank_llm"] = {}
    for m in models:
        rm = _brand_rank(cl, a, b, model=m)["ranks"]
        B["rank_llm"][m] = {pid: _self_rank(v) for pid, v in rm.items()}
    pids = list(B["rank"].keys())
    # position in answer per prompt per LLM (one request per prompt)
    B["pos_prompt"] = {}
    for pid in pids:
        if B["rank"][pid]:
            c = _self_cells(_heat(cl, a, b, "position", ["SELF"], prompt_id=pid))
            if c:
                B["pos_prompt"][pid] = c
    # our pages cited by AI, and per prompt (most cited first)
    own = own_domains(site)
    B["cites"] = _grouped(cl, a, b, own, 100)
    B["cites_prompt"] = {}
    for pid in pids:
        g = _grouped(cl, a, b, own, 10, prompt_id=pid)
        if g["rows"]:
            B["cites_prompt"][pid] = [[r[0], r[1], r[2], r[4]] for r in g["rows"]]
    # lost prompts: the #1 brand's most-cited page for that prompt
    B["top_cite"] = {}
    if prev_bundle and prev_bundle.get("rank"):
        for pid, brands in B["rank"].items():
            was = _self_rank(prev_bundle["rank"].get(pid) or [])
            now = _self_rank(brands)
            if was and was[0] == 1 and now[0] != 1:
                top = next((x for x in brands if x[3] == 1), None)
                if top and top[1]:
                    g = _grouped(cl, a, b, [_host(top[1]), "www." + _host(top[1])], 1, prompt_id=pid)
                    B["top_cite"][pid] = [top[0]] + ([g["rows"][0][0], g["rows"][0][1], g["rows"][0][2], g["rows"][0][4]] if g["rows"] else [])
    # AI referral clicks (Promptwatch Visitor Analytics): platform totals, then pages + countries per platform
    rs = cl.get("/analytics/visitors/referrer-stats", startDate=a, endDate=b, timezone="UTC") or {}
    B["refs"] = [[d.get("referrer"), d.get("visits"), d.get("visitsPrevious"), d.get("hits"), d.get("hitsPrevious")] for d in rs.get("data") or []]
    B["ref_pages"] = {}
    for ref in [r[0] for r in B["refs"] if (r[1] or 0) > 0]:
        tp = (cl.get("/analytics/visitors/top-pages", startDate=a, endDate=b, timezone="UTC", referrerDomains=ref) or {}).get("data") or []
        pages = []
        for pg in tp[:40]:
            loc = (cl.get("/analytics/visitors/top-locations", startDate=a, endDate=b, timezone="UTC", referrerDomains=ref,
                          path=pg.get("name"), pathMatch="exact") or {}).get("data") or []
            pages.append([pg.get("name"), pg.get("value"), [[x.get("name"), x.get("value")] for x in loc[:5]]])
        B["ref_pages"][ref] = pages
    return B


def _est_calls(tier: str, n_prompts: int, n_models: int = 6) -> int:
    return 4 if tier == "light" else 6 + n_models * 2 + n_prompts * 2 + 40


# ------------------------------------------------------------------ pull (incremental)
def pull(cfg: dict, site: dict, prev: dict | None = None, max_calls: int | None = None) -> dict:
    key = api_key(cfg)
    if not key:
        raise PromptwatchError("No Promptwatch API key configured.")
    prev = prev if (prev or {}).get("version") == 2 else None
    end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)  # last full UTC day
    cl = Client(key, max_calls=int(max_calls or cfg.get("promptwatch_max_calls") or 1500))
    out: dict = {"version": 2, "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "end": _iso(end),
                 "errors": [], "bundles": dict((prev or {}).get("bundles") or {}), "own_domains": own_domains(site)}
    out["auth"] = resolve_project(cl, site)
    for k in ("models", "vis_daily", "texts"):
        if prev and k in prev:
            out[k] = prev[k]
    try:
        ms = (cl.get("/models", includeDeprecated=True) or {}).get("models") or []
        out["models"] = {m["id"]: [m.get("label") or m["id"], m.get("providerName")] for m in ms}
        start92 = _iso(end - dt.timedelta(days=91))
        out["vis_daily"] = [[r["date"][:10], _r2(r.get("value"))] for r in
                            cl.get("/visibility-time-series", startDate=start92, endDate=_iso(end), range="day") or []]
    except PromptwatchError as e:
        out["errors"].append({"call": "visibility", "error": str(e)})

    W = plan_windows(end)
    out["windows"] = W
    wanted = set(W["days"]) | set(W["weeks"]) | {W["r7"], W["p7"], W["r28"], W["p28"]}
    out["bundles"] = {k: v for k, v in out["bundles"].items() if k in wanted}
    days, weeks = W["days"], W["weeks"]
    prevday = lambda k: days[days.index(k) + 1] if k in days and days.index(k) + 1 < len(days) else None
    # priority order: latest day, last 7 / 28 days, latest week, then the tracker days, then older weeks / days
    # (the comparison window is fetched first, so "lost" prompts can be found while building the main window)
    tasks = [(days[1], "light", None), (days[0], "full", days[1]),
             (W["p7"], "light", None), (W["r7"], "full", W["p7"]),
             (W["p28"], "light", None), (W["r28"], "full", W["p28"]),
             (weeks[1], "light", None), (weeks[0], "full", weeks[1])]
    tasks += [(d, "light", None) for d in days[2:TRACK_DAYS + 1]]
    tasks += [(w, "full", weeks[i + 2] if i + 2 < len(weeks) else None) for i, w in enumerate(weeks[1:KEEP_WEEKS])]
    tasks += [(d, "full", prevday(d)) for d in days[1:KEEP_DAYS - 1]]
    done, skipped = [], []
    n_prompts = max([len(v.get("rank") or {}) for v in out["bundles"].values()] + [100])
    for k, tier, prev_k in tasks:
        have = out["bundles"].get(k)
        if have and (have.get("tier") == "full" or tier == "light"):
            continue
        if not cl.ok(_est_calls(tier, n_prompts)):
            skipped.append(k)
            continue
        a, b = k.split("_")
        try:
            pb = out["bundles"].get(prev_k) if prev_k else None
            bun = build_bundle(cl, a, b, tier, site, "UTC", pb)
            texts = out.setdefault("texts", {})
            texts.update(bun.pop("texts", {}))
            out["bundles"][k] = bun
            done.append(f"{k}:{tier}")
            n_prompts = max(n_prompts, len(bun.get("rank") or {}))
        except OutOfQuota as e:
            out["errors"].append({"call": k, "error": str(e)})
            skipped.append(k)
            break
        except PromptwatchError as e:
            out["errors"].append({"call": k, "error": str(e)})
        except Exception as e:  # unexpected response shape: keep going with the other windows
            out["errors"].append({"call": k, "error": f"{type(e).__name__}: {e}"})
    out["run"] = {"windows_fetched": done, "windows_waiting": skipped, "api_calls": cl.calls,
                  "quota_limit_per_hour": cl.limit, "quota_remaining": cl.remaining}
    if not out["bundles"] and out["errors"]:
        raise PromptwatchError(out["errors"][0]["error"])
    return out


# ------------------------------------------------------------------ storage
def snapshot_file(cfg: dict, site_id: str) -> Path:
    p = data_dir(cfg) / "daily"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{site_id}.promptwatch.json"


def load(cfg: dict, site_id: str) -> dict | None:
    f = snapshot_file(cfg, site_id)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def refresh(cfg: dict, site: dict, errors: list, max_calls: int | None = None) -> dict | None:
    if not api_key(cfg):
        return None
    prev = load(cfg, site["id"])
    try:
        snap = pull(cfg, site, prev, max_calls=max_calls)
    except PromptwatchError as e:
        errors.append({"source": "Promptwatch", "table": "all", "error": str(e)})
        return None
    for e in snap.get("errors", []):
        errors.append({"source": "Promptwatch", "table": e["call"], "error": e["error"]})
    snapshot_file(cfg, site["id"]).write_text(json.dumps(snap, separators=(",", ":")))
    return snap
