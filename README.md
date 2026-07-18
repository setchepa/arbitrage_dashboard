# CLP ↔ USD Arbitrage Dashboard

**🔴 Live:** https://web-production-cae25.up.railway.app/

Live dashboard for the loop:

**charge US credit card (USD→CLP via Visa/MC) → buy USDC on Buda → transfer to
Robinhood/Binance → sell USDC→USD → pay the card.** Profit and cashback are in USD.

The front-end is a hand-built HTML/CSS/JS page (light/dark, IBM Plex) served by a
small Flask backend. The optimizer runs client-side in JS so the sidebar controls
recompute instantly; the backend only supplies live market data. The layout is
**responsive**: the desktop spec applies at `≥ 900px`; below that it switches to a
mobile layout (sticky header, collapsible parameters sheet, vertical loop, per-card
economics, stacked totals) — one codebase, gated on `@media (max-width: 899px)`.

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

## Telegram alerts (ROI > 2%)
`collect.py` also runs the optimizer on the **base scenario** (5,000,000 CLP,
0.30% Buda fee, 1.0 peg) each tick and alerts when ROI clears the threshold.

**Telegram cannot text a phone number.** The Bot API sends to a `chat_id` and the
recipient must message the bot first (anti-spam by design); the Gateway API does
target phone numbers but is limited to verification codes. So alerts arrive as a
Telegram push notification on your phone.

Setup:
1. Message **@BotFather** -> `/newbot` -> copy the token
2. Message your new bot (e.g. `/start`) so it's allowed to reply
3. `TELEGRAM_BOT_TOKEN=... python notify.py --chat-id` to find your id
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` on the `collector` service
5. `python notify.py --test` sends a test message

`ALERT_ROI_THRESHOLD` overrides the default `2.0` (percent).

**Stepped alerting.** ROI is bucketed into `ALERT_ROI_STEP` (default 0.5%) bands
above the threshold, and an alert fires only on entering a *new, higher* band:

| ROI | Band | Behaviour |
|-----|------|-----------|
| ≤ 2.0% | — | quiet; re-arms the ladder |
| 2.0–2.5% | 0 | alert once at the **2.0%** level |
| 2.5–3.0% | 1 | alert once at the **2.5%** level |
| 3.0–3.5% | 2 | alert once at the **3.0%** level |

So a window climbing 2.1 → 2.3 → 2.6 → 3.1% sends three alerts (2.0, 2.5, 3.0),
not twenty. Only the *highest* band reached is remembered, so a dip back to 2.8%
after alerting at 3.0% stays quiet — no flapping. Dropping to/below 2.0% re-arms
the whole ladder. A jump straight from 1.9% to 4.7% sends one alert, at the 4.5%
level.

State is a single-row `alert_state` table holding `last_band` (not a history
table), so it survives container restarts. If the Telegram env vars are unset the
alert is skipped cleanly and the snapshot is still recorded; a transient send
failure leaves the band unchanged so the next tick retries.

## Legacy
`app.py` is the original Streamlit prototype (needs `streamlit`, see the commented
extras in `requirements.txt`). `explore_*.py` / `test_*.py` are dev scripts.
