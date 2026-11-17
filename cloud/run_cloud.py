"""Cloud daily refresh (GitHub Actions).

work/state/daily.tar.enc            day-by-day data (encrypted), from the previous run
work/weekly/reports/<week>/report.json.enc   weekly reports pushed from the Mac (SEMrush + insights)
-> out/daily.tar.enc, out/dashboard.html.enc, out/status.json (no data, just dates)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
work, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
KEY = os.environ["DASHBOARD_KEY"]

from seo_core import vault  # noqa: E402

data = Path(tempfile.mkdtemp(prefix="seo-data-"))
# 1) saved day-by-day data
enc = work / "state" / "daily.tar.enc"
if enc.exists():
    with tarfile.open(fileobj=io.BytesIO(vault.decrypt(enc.read_bytes(), KEY))) as t:
        t.extractall(data, filter="data")
# 2) weekly reports from the Mac
n_weeks = 0
for f in sorted((work / "weekly" / "reports").glob("*/report.json.enc")):
    d = data / "reports" / f.parent.name
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_bytes(vault.decrypt(f.read_bytes(), KEY))
    n_weeks += 1
if not n_weeks:
    sys.exit("No weekly reports found on the weekly branch.")

# 3) config for seo_core
home = Path(tempfile.mkdtemp(prefix="seo-home-"))
cfg = json.loads((ROOT / "cloud" / "config.json").read_text())
cfg["credentials_file"] = os.environ["GOOGLE_SA_FILE"]
cfg["data_dir"] = str(data)
(home / "config.json").write_text(json.dumps(cfg))
os.environ["INTELLECTYX_SEO_HOME"] = str(home)

from seo_core import daily, render  # noqa: E402

sites = [s["id"] for s in cfg["sites"] if s.get("enabled", True)]
summary = daily.refresh(sites)
errors = [e for s in summary["sites"].values() for e in s.get("errors", [])]
res = render.render()
page = Path(res["dashboard_file"]).read_bytes()

# 4) save encrypted
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t:
    t.add(data / "daily", arcname="daily")
(out / "daily.tar.enc").write_bytes(vault.encrypt(buf.getvalue(), KEY))
(out / "dashboard.html.enc").write_bytes(vault.encrypt(page, KEY))
now = dt.datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Kolkata")))
status = {"built_at": now.isoformat(timespec="seconds"), "built_on": now.date().isoformat(),
          "daily_window": summary["window"], "latest_week": res["weeks_included"][0],
          "weeks": len(res["weeks_included"]), "page_bytes": len(page), "errors": len(errors)}
(out / "status.json").write_text(json.dumps(status, indent=2))
(out / "README.md").write_text("Encrypted dashboard data, rewritten by the daily workflow. status.json holds dates only.\n")
print(json.dumps(status, indent=2))
for e in errors[:10]:
    print("ERROR", e.get("source"), e.get("table"), e.get("error"), file=sys.stderr)
if not res["daily_data_bytes"]:
    sys.exit("Day-by-day data is empty.")
