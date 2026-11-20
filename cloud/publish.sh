#!/usr/bin/env bash
# Used by the Claude publish task: download the latest encrypted dashboard from the state branch,
# decrypt it with $DASHBOARD_KEY and leave OUTDIR/index.html + OUTDIR/status.json.
# usage: DASHBOARD_KEY=... bash publish.sh OUTDIR
set -euo pipefail
OUT="${1:?usage: publish.sh OUTDIR}"; mkdir -p "$OUT"; cd "$OUT"
REPO="SarathkumarIX/intellectyx-seo-dashboard"
GIT="https://github.com/$REPO.git"; RAW="https://raw.githubusercontent.com/$REPO"
S=$(git ls-remote "$GIT" refs/heads/state | cut -f1); M=$(git ls-remote "$GIT" refs/heads/main | cut -f1)
[ -n "$S" ] && [ -n "$M" ] || { echo "ERROR: cannot read the repo branches"; exit 1; }
curl -fsSL -o status.json "$RAW/$S/status.json"
curl -fsSL -o page.enc "$RAW/$S/dashboard.html.enc"
curl -fsSL -o vault.py "$RAW/$M/seo_core/vault.py"
python3 -c "import cryptography" 2>/dev/null || pip install -q cryptography --break-system-packages >/dev/null 2>&1 || pip install -q cryptography >/dev/null
python3 vault.py decrypt page.enc index.html
python3 - <<'PY'
import json, os, datetime, zoneinfo
st = json.load(open("status.json")); size = os.path.getsize("index.html")
ok = open("index.html", "rb").read(15) == b"<!DOCTYPE html>" and size > 1_000_000
today = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata")).date().isoformat()
print(json.dumps({"page_ok": ok, "index_html": os.path.abspath("index.html"), "built_at": st["built_at"],
                  "built_today": st["built_on"] == today, "data_up_to": st["daily_window"][1],
                  "weekly_report": st["latest_week"], "errors": st["errors"]}))
PY
