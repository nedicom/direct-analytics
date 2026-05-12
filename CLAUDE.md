# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deploy

Push to `main` → GitHub Actions deploys automatically via SSH to the server.

Manual deploy on server:
```bash
cd /home/forge/direct.nedicom.ru
git pull origin main
systemctl restart direct-analytics
```

Check logs:
```bash
journalctl -u direct-analytics -n 50 --no-pager -l
```

## Local run

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env  # fill in values
venv/bin/python app.py
```

## Architecture

Two-file backend:

- **`app.py`** — Flask app. Handles auth (session + `DASHBOARD_PASSWORD`), renders the dashboard, exposes `/api/analyze` (calls Claude) and `/api/refresh`. Sorts campaigns: active (`State=ON`) first, then by `StartDate` descending.
- **`direct_client.py`** — Yandex Direct API v5 client. Two functions:
  - `get_campaigns()` — fetches up to 1000 campaigns with fields `Id, Name, Status, State, StartDate`. Uses `session.trust_env = False` to prevent `requests` from picking up `HTTPS_PROXY` (which is socks5, only needed for Anthropic).
  - `get_campaign_stats()` — fetches `CAMPAIGN_PERFORMANCE_REPORT` for all campaigns (no ID filter) for the last 30 days. Returns `dict[campaign_id → {impressions, clicks, cost, ctr}]`. TSV columns: `CampaignId(0), Date(1), Impressions(2), Clicks(3), Cost(4), Ctr(5)`.

## Key behaviours

- `HTTPS_PROXY=socks5://127.0.0.1:1080` in `.env` is used **only** for Anthropic API (via `httpx`). The `requests` calls to Yandex Direct bypass it via `trust_env=False`.
- Campaign `Status` = moderation status (ACCEPTED/MODERATION). Campaign `State` = operational status (ON/OFF/SUSPENDED/ENDED/ARCHIVED). The UI uses `State`.
- `history.json` lives on the server only (not in git) — stores Claude analysis history.
- Anthropic model: `claude-sonnet-4-6`, max_tokens 1500.

## Server

- URL: https://direct.nedicom.ru
- SSH: `root@178.208.94.106`
- Path: `/home/forge/direct.nedicom.ru`
- Service: `direct-analytics` (systemd + gunicorn, port 8002, behind nginx)
- venv created via `python3 -m venv --without-pip` + `curl get-pip.py` (ensurepip unavailable)

## Environment variables (server `.env`)

| Variable | Description |
|---|---|
| `DIRECT_TOKEN` | Yandex Direct OAuth token (expires in 1 year) |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `DASHBOARD_PASSWORD` | Login password |
| `SECRET_KEY` | Flask session secret |
| `HTTPS_PROXY` | `socks5://127.0.0.1:1080` — for Anthropic only |
