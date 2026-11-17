"""Configuration for the Intellectyx SEO dashboard connector.

Config lives on the user's Mac (not inside the plugin, so plugin updates never
overwrite it):  ~/.config/intellectyx-seo/config.json
Override the folder with the INTELLECTYX_SEO_HOME environment variable.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

HOME_DIR = Path(os.environ.get("INTELLECTYX_SEO_HOME", "~/.config/intellectyx-seo")).expanduser()
CONFIG_PATH = HOME_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "credentials_file": str(HOME_DIR / "service-account.json"),
    # Where SEMrush exports are dropped and reports/dashboards are written.
    "data_dir": "~/Desktop/Coworker-Plugins/SEO-Analytics-Dashboard/workspace",
    "timezone": "Asia/Kolkata",
    "sites": [
        {
            "id": "intellectyx-com",
            "label": "intellectyx.com",
            "url": "https://www.intellectyx.com/",
            "gsc_property": "",       # e.g. "sc-domain:intellectyx.com" or "https://www.intellectyx.com/"
            "ga4_property_id": "",    # numeric, e.g. "312345678"
            "semrush_folder": "intellectyx.com",
            # SEMrush screens read through the logged-in browser (ids from the SEMrush Projects page)
            "semrush_browser": {
                "project_domain": "www.intellectyx.com",
                "site_audit_campaign": "17376075",
                "position_tracking_id": "17376075_1948678",
                "backlinks_domain": "intellectyx.com",
            },
        },
        {
            "id": "intellectyx-ai",
            "label": "intellectyx.ai",
            "url": "https://www.intellectyx.ai/",
            "gsc_property": "",
            "ga4_property_id": "",
            "semrush_folder": "intellectyx.ai",
            "semrush_browser": {
                "project_domain": "intellectyx.ai",
                "site_audit_campaign": "25329370",
                "position_tracking_id": "25329370_3166818",
                "backlinks_domain": "intellectyx.ai",
            },
        },
    ],
    # Queries matching this regex count as branded search (RE2 syntax, used by GSC).
    "brand_regex": "(?i)intellectyx|intelectyx|intellectix|intellect yx",
    # Session sources that count as AI assistants in GA4 (partial regex).
    "ai_source_regex": "chatgpt|openai|perplexity|gemini|bard|copilot|claude|anthropic|deepseek|grok|you\\.com|meta\\.ai|mistral",
    "thresholds": {
        "min_impressions_query": 50,      # min weekly impressions for a query to be flagged
        "min_impressions_page": 100,      # min weekly impressions for a page to be flagged
        "striking_distance_min_pos": 4.0,
        "striking_distance_max_pos": 20.0,
        "low_ctr_ratio": 0.5,             # page CTR below 50% of the site's CTR for the same position bucket
        "min_sessions_landing": 20,       # min weekly organic sessions for a landing page to be flagged
        "low_engagement_ratio": 0.6,      # engagement rate below 60% of the site's organic average
        "drop_pct_alert": 20.0,           # week-over-week drop that counts as an alert
        "max_audit_age_days": 8,          # SEMrush Site Audit crawl older than this is flagged
        "inspect_top_pages": 15,          # how many pages to run URL Inspection on per site
        "inspect_sitemap_urls": 60,       # extra URLs sampled from the sitemaps for indexing checks
        "trend_weeks": 12,
    },
    "daily_window_days": 112,             # days of day-by-day data kept for the dashboard date picker
    "pagespeed_api_key": "",              # optional; PageSpeed Insights works without a key at low volume
    "dashboard": {
        "title": "Intellectyx SEO Weekly Dashboard",
        "artifact_slug": "",              # filled after the first publish
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if k == "sites" and isinstance(v, list):
            out["sites"] = _merge_sites(out.get("sites", []), v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_sites(existing: list, patch: list) -> list:
    by_id = {s.get("id"): copy.deepcopy(s) for s in existing}
    order = [s.get("id") for s in existing]
    for s in patch:
        sid = s.get("id")
        if sid in by_id:
            by_id[sid].update(s)
        else:
            by_id[sid] = copy.deepcopy(s)
            order.append(sid)
    return [by_id[i] for i in order]


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            user = json.load(f)
        cfg = _deep_merge(cfg, user)
    return cfg


def save_config(cfg: dict) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)


def update_config(patch: dict) -> dict:
    cfg = _deep_merge(load_config(), patch)
    save_config(cfg)
    return cfg


def data_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    p = Path(cfg["data_dir"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_site(cfg: dict, site_id: str) -> dict:
    for s in cfg["sites"]:
        if site_id in (s.get("id"), s.get("label")):
            return s
    raise ValueError(f"Unknown site '{site_id}'. Known: {[s['id'] for s in cfg['sites']]}")
