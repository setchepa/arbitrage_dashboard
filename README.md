# CLP ↔ USD Arbitrage Dashboard

**🔴 Live:** https://web-production-cae25.up.railway.app/

Live dashboard for the loop:

**charge US credit card (USD→CLP via Visa/MC) → buy USDC on Buda → transfer to
Robinhood/Binance → sell USDC→USD → pay the card.** Profit and cashback are in USD.

The front-end is a hand-built HTML/CSS/JS page (light/dark, IBM Plex) served by a
small Flask backend. The optimizer runs client-side in JS so the sidebar controls
recompute instantly; the backend only supplies live market data. The layout is
**responsive**: the desktop spec applies at `≥ 900px`; below that it switches to a
mobile layout (sticky header, collapsible parameters sheet, vertical loop,
stacked totals) — one codebase, gated on `@media (max-width: 899px)`.

Behind the dashboard a **cron service** captures the three rates to Postgres every
10 minutes and **alerts over Telegram** when base-scenario ROI climbs past 2%
(then every further 0.5%). Three pieces in one repo:

| Piece | Entry point | Runs |
|-------|-------------|------|
| Dashboard | `server.py` + `web/` | always on (`web` service) |
| Rate capture | `collect.py` → `db.py` | every 10 min (`collector` cron) |
| Alerts | `collect.py` → `notify.py` | same tick, when ROI enters a new band |

## Sources (no official API keys — the pages' own backends)
| File | Source | Access method |
|------|--------|---------------|
| `visa_rate.py` | Visa exchange-rate calculator | `/cmsapi/fx/rates` via **cloudscraper** (Cloudflare) |
| `mastercard_rate.py` | Mastercard converter | `/marketingservices/.../conversion-rates` via **curl_cffi** (Akamai TLS) |
| `buda_rate.py` | Buda USDC-CLP | `/api/v2/markets/usdc-clp/{quotations,order_book}` via **cloudscraper** |

All rates are stored as the **reverse rate** = CLP per USD/USDC.

## Optimizer (`optimizer.py` — and its 1:1 JS port in `web/app.js`)
Greedy, slippage-aware allocation across cards. Each card's effective cost per CLP
is `(1 − cashback) / card_fx`; Buda revenue per CLP is `0.997 / ask_price` walked
down the live order book. Cheapest cards fill first; deployment stops when the
marginal Buda price no longer beats the best remaining card.

Cards: Fidelity (Visa, 2%, cap 5M CLP), CapitalOne (MC, 1.5%), Chase (Visa, 1%).
Sell venue is Robinhood at a $1.00 peg, else Binance. All editable in the sidebar.

## Run locally
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python server.py          # dev server on http://localhost:8600
# or, exactly like production:
PORT=8600 ./venv/bin/gunicorn server:app --bind 0.0.0.0:8600
```
Logos: drop PNGs into `web/logos/` (`fidelity.png`, `capitalone.png`, `chase.png`,
`buda.png`, `binance.png`, `robinhood.png`, `sadface.png`). Missing files fall back
to a placeholder — no errors.

## Deploy on Railway
Deployed at **https://web-production-cae25.up.railway.app/**. This repo is
Railway-ready (Nixpacks):
- `Procfile` → `gunicorn server:app --bind 0.0.0.0:$PORT`
- `.python-version` pins Python 3.12
- `requirements.txt` lists only the runtime deps

Steps: create a Railway project from this GitHub repo → it auto-builds with Nixpacks
and starts the `Procfile` web process → open the generated URL. `PORT` is injected
by Railway; no env vars are required. `/healthz` is a cheap health-check endpoint.

**Caveat:** Visa/Buda sit behind Cloudflare and Mastercard behind Akamai. These may
block Railway's datacenter IPs more aggressively than a residential IP. If
`/api/rates` starts returning 502s in production, the fix is to route the scrapers
through a residential/proxy egress — the app already serves the last good data
(flagged `stale`) when a live fetch fails.

## Live data (no refresh button)
The page updates itself — there is no manual refresh. The three sources have very
different refresh characteristics, so they're polled differently:

| Source | Changes | Endpoint | Cadence |
|--------|---------|----------|---------|
| Buda order book | continuously (live market) | `/api/buda` | client polls **every 1s** |
| Visa / Mastercard | **once a day** (daily published FX) | `/api/rates` | on load, then every 10 min |

Polling the card networks per second would be pointless (the rate is a daily
figure) and would invite Cloudflare/Akamai rate-limiting — the real risk from a
datacenter IP. Only Buda actually moves, and it drives the VWAP, ROI and profit.

Latency: `buda_rate.py` keeps one long-lived cloudscraper session, which cuts an
order-book fetch from ~740ms to **~330ms**. `/api/buda` caches for 0.5s, so N
concurrent viewers still produce at most ~2 req/s to Buda rather than N. Polling
pauses while the browser tab is hidden and catches up on re-focus.

Rendering note: `render()` runs every second, so the Step-1 card block is only
rebuilt when the allocation actually changes — otherwise recreating its `<img>`
tags each tick makes the logos flicker back to their placeholder.

## Rate history (Postgres, every 10 minutes)
`collect.py` captures the three live datapoints and appends one row to Postgres.
It's a one-shot script run on a schedule — no long-lived process.

| File | Purpose |
|------|---------|
| `db.py` | `DATABASE_URL` connection, idempotent schema bootstrap, insert/query helpers |
| `collect.py` | Fetch the 3 rates → insert one row. Exits non-zero on failure |
| `railway.cron.json` | Cron service config: `python collect.py`, `*/10 * * * *`, restart `NEVER` |

Table `rate_snapshots` — numbers only:

| Column | Type |
|--------|------|
| `id` | `BIGSERIAL` PK |
| `captured_at` | `TIMESTAMPTZ` (indexed `DESC`) |
| `visa` | `NUMERIC(12,2)` — CLP per USD |
| `mc` | `NUMERIC(12,2)` — CLP per USD |
| `buda` | `NUMERIC(12,2)` — CLP per USDC (best ask) |

Schema is created on first run and `init_schema()` carries an idempotent migration
(renames legacy `visa_fx`/`mc_fx`/`buda_best_ask`, forces 2 decimals, drops the old
`visa_as_of`/`mc_as_of`/`buda_levels` columns), so existing databases upgrade
automatically.

Run it by hand:
```bash
DATABASE_URL=postgresql://... ./venv/bin/python collect.py
```

**Railway setup** — the project has three services: `web`, `Postgres`, `collector`.
The `collector` service has `DATABASE_URL=${{Postgres.DATABASE_URL}}` and must be
pointed at **config-as-code path `railway.cron.json`**, which supplies its start
command and the 10-minute schedule. Each cron tick spins up a container, writes one
row, and exits.

## Telegram alerts (ROI ≥ 2%)
Each 10-minute tick, `collect.py` also runs the optimizer on the **base scenario**
(5,000,000 CLP, 0.30% Buda fee, 1.0 peg) and sends a Telegram alert when ROI
climbs through the alert ladder.

| File | Purpose |
|------|---------|
| `notify.py` | Telegram Bot API client (stdlib only, no dependency) + `--chat-id` / `--test` helpers |
| `collect.py` | Computes base-scenario ROI, decides the band, sends the alert |
| `db.py` | `alert_state` single-row table holding `last_band` |

### Telegram cannot text a phone number
This is a hard API limitation, not a workaround problem:

- The **Bot API** sends to a `chat_id`, and the recipient must message the bot
  first — bots cannot cold-message anyone (anti-spam by design).
- The **Gateway API** *does* target phone numbers but is restricted to
  verification codes, so it cannot carry alerts.

Alerts therefore arrive as a Telegram push notification on your phone, which is
functionally equivalent.

### Setup
1. Message **@BotFather** → `/newbot` → copy the token
2. Message your new bot (e.g. `/start`) so it's allowed to reply to you
3. `TELEGRAM_BOT_TOKEN=... python notify.py --chat-id` to discover your chat id
4. Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` on the **`collector`** service
5. `TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python notify.py --test`

Secrets live only in Railway service variables — never in this repo.

### Environment variables
| Variable | Default | Meaning |
|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token. Absent ⇒ alerting silently disabled |
| `TELEGRAM_CHAT_ID` | — | Target chat. Absent ⇒ alerting silently disabled |
| `ALERT_ROI_THRESHOLD` | `2.0` | ROI % at which the ladder starts |
| `ALERT_ROI_STEP` | `0.5` | ROI % increment between alerts |

### Stepped alerting
ROI is bucketed into `ALERT_ROI_STEP` bands above the threshold; an alert fires
only on entering a **new, higher** band:

| ROI | Band | Behaviour |
|-----|------|-----------|
| ≤ 2.0% | `NULL` | quiet; re-arms the ladder |
| 2.0–2.5% | 0 | alert once at the **2.0%** level |
| 2.5–3.0% | 1 | alert once at the **2.5%** level |
| 3.0–3.5% | 2 | alert once at the **3.0%** level |

Consequences worth knowing:

- A window climbing 2.1 → 2.3 → 2.6 → 3.1% sends **three** alerts (2.0, 2.5, 3.0),
  not one per tick.
- Only the *highest* band reached is remembered, so a dip to 2.8% after alerting
  at 3.0% stays quiet — no flapping. The next alert would be at 3.5%.
- Falling to/below the threshold re-arms the whole ladder.
- A jump straight from 1.9% to 4.7% sends **one** alert, at the 4.5% level — not
  one message per level crossed.

State is the single-row `alert_state` table (`last_band`, `last_alert_at`) — not a
history table — so it survives container restarts. If the Telegram vars are unset
the alert is skipped cleanly and the snapshot is still recorded. A transient send
failure leaves the band unchanged so the next tick retries rather than silently
skipping a step.

### Message format
```
🚨 Arbitrage window — 2.5%+

ROI 2.634%  (crossed the 2.5% level)
Profit $142.10 on 5,000,000 CLP

Visa 934.92 · MC 923.96 · Buda 936.26
Cards: Fidelity 5,000,000 CLP

https://web-production-cae25.up.railway.app/
```

### Operational gotchas
- **Railway bakes env vars into a deployment.** Setting variables with
  `--skip-deploys` leaves the running deployment untouched, so the next cron tick
  still uses the old values. Trigger a redeploy (or set variables without that
  flag) and confirm a new `SUCCESS` deployment before expecting new behaviour.
- **`getUpdates` returns an empty list once updates are consumed or expire**
  (they're kept ~24h). If `--chat-id` finds nothing even though you messaged the
  bot, send a fresh message while long-polling:
  `curl "https://api.telegram.org/bot<TOKEN>/getUpdates?timeout=30"`.
  Also check `getWebhookInfo` — a registered webhook suppresses `getUpdates`
  entirely.
- Only one `getUpdates` consumer may run at a time; concurrent pollers conflict.

## Legacy
`app.py` is the original Streamlit prototype (needs `streamlit`, see the commented
extras in `requirements.txt`). `explore_*.py` / `test_*.py` are dev scripts.
