# CLP ↔ USD Arbitrage Dashboard

Live dashboard for the loop:

**charge US credit card (USD→CLP via Visa/MC) → buy USDC on Buda → transfer to
Robinhood/Binance → sell USDC→USD → pay the card.** Profit and cashback are in USD.

The front-end is a hand-built HTML/CSS/JS page (light/dark, IBM Plex) served by a
small Flask backend. The optimizer runs client-side in JS so the sidebar controls
recompute instantly; the backend only supplies live market data.

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
This repo is Railway-ready (Nixpacks):
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

## Legacy
`app.py` is the original Streamlit prototype (needs `streamlit`, see the commented
extras in `requirements.txt`). `explore_*.py` / `test_*.py` are dev scripts.
