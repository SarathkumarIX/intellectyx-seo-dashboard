"""Render the weekly report JSON (+ Claude-written insights) into one
self-contained HTML dashboard page."""
from __future__ import annotations

import base64
import gzip
import html
import json
import shutil
from pathlib import Path

from . import daily
from .config import data_dir, load_config

TEMPLATE = Path(__file__).with_name("dashboard_template.html")
INSIGHT_KEYS = {"headline", "summary", "issues", "wins", "actions"}


def latest_report(ddir: Path) -> Path:
    reports = sorted((ddir / "reports").glob("*_to_*/report.json"))
    if not reports:
        raise FileNotFoundError("No weekly report found. Run build_weekly_report first.")
    return reports[-1]


def _clean_insights(ins: dict) -> dict:
    out = {k: v for k, v in (ins or {}).items() if k in INSIGHT_KEYS}
    for k in ("summary", "issues", "wins", "actions"):
        if k in out and not isinstance(out[k], list):
            out[k] = [out[k]]
    return out


def _slim(report: dict) -> dict:
    page = dict(report)
    stored = page.get("insights", {}) or {}
    page["sites"] = []
    for s0 in report.get("sites", []):
        s = json.loads(json.dumps(s0))
        s["insights"] = stored.get(s["id"]) or stored.get(s["label"]) or {}
        for sec in ("gsc", "ga4"):
            if isinstance(s.get(sec), dict):
                s[sec].pop("trace", None)
        if isinstance(s.get("gsc"), dict):
            s["gsc"].pop("inspections", None)
        sr = s.get("semrush") or {}
        for blk in ("current", "previous"):
            for f in (sr.get(blk) or {}).get("files", []) or []:
                f.pop("sample_rows", None)
        page["sites"].append(s)
    page.pop("insights", None)
    page.pop("_report_path", None)
    return page


def render(insights: dict | None = None, report_path: str | None = None, history_weeks: int | None = None) -> dict:
    cfg = load_config()
    ddir = data_dir(cfg)
    rp = Path(report_path).expanduser() if report_path else latest_report(ddir)
    report = json.loads(rp.read_text())
    insights = insights or {}

    # keep insights with the report so a re-render without them keeps the text
    stored = report.get("insights", {})
    for sid, ins in insights.items():
        stored[sid] = _clean_insights(ins)
    report["insights"] = stored
    rp.write_text(json.dumps(report, indent=2, default=str))

    # the latest week first, then earlier saved weeks (week picker)
    limit = history_weeks or cfg.get("history_weeks", 26)
    weeks = [_slim(report)]
    for other in sorted((ddir / "reports").glob("*_to_*/report.json"), reverse=True):
        if other.resolve() == rp.resolve() or other.parent.name > rp.parent.name:
            continue
        try:
            weeks.append(_slim(json.loads(other.read_text())))
        except Exception:
            continue
        if len(weeks) >= limit:
            break

    title = cfg["dashboard"]["title"]
    # all weekly tables are stored in full, so the page carries them gzip-compressed (base64)
    raw = json.dumps({"title": title, "weeks": weeks}, default=str, ensure_ascii=False, separators=(",", ":")).encode()
    data = base64.b64encode(gzip.compress(raw, 9)).decode()
    tpl = TEMPLATE.read_text()
    # day-by-day data for the custom date range picker (gzip + base64; empty if never pulled)
    try:
        daily_blob = daily.payload(cfg, [s["id"] for s in report.get("sites", [])]) or ""
    except Exception:
        daily_blob = ""
    tpl = tpl.replace("__DAILY__", daily_blob)
    out_html = tpl.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)

    week_file = rp.with_name("dashboard.html")
    week_file.write_text(out_html, encoding="utf-8")
    live_dir = ddir / "dashboard"
    live_dir.mkdir(exist_ok=True)
    live = live_dir / "index.html"
    shutil.copyfile(week_file, live)
    return {
        "dashboard_file": str(live),
        "weekly_copy": str(week_file),
        "bytes": len(out_html.encode("utf-8")),
        "weeks_included": [w["period"]["current"] for w in weeks],
        "daily_data_bytes": len(daily_blob),
        "artifact_slug": cfg["dashboard"].get("artifact_slug") or None,
    }
