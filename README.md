# Intellectyx SEO dashboard - cloud refresh

Keeps https://claude.ai/artifact/4S1Emct6dWbtzPArryrMEo current when the Mac is off.

* `main` - code. `.github/workflows/daily-refresh.yml` runs every morning (10:35 IST):
  pulls day-by-day Search Console + GA4 data and builds the dashboard page.
* `weekly` - weekly reports (SEMrush + written insights) pushed from the Mac on Mondays. Encrypted.
* `state` - day-by-day data and the built page. Encrypted, rewritten each run.
  `status.json` holds dates only.

Every data file is encrypted (AES-256-GCM) with the dashboard key (`DASHBOARD_KEY` secret).
The Google key is only in the `GOOGLE_SERVICE_ACCOUNT_JSON` secret. Neither key is in this repo.
