"""
ROI alerter — a background thread inside the web service, every 10 minutes.

Fetches the three live datapoints (Visa CLP/USD, Mastercard CLP/USD, Buda ask
book), runs the optimizer on the BASE SCENARIO and, if ROI clears the threshold,
sends a Telegram alert. Nothing is persisted.

Alerting is stepped and edge-triggered: it fires once per new 0.5% band above
the threshold, then stays quiet until ROI drops back under and crosses again.
The latch lives in memory, so a redeploy re-arms it (at worst one repeat alert
if a window is open during a deploy). Telegram is skipped cleanly if its env
vars aren't set.

gunicorn runs several workers; a non-blocking file lock makes exactly one of
them run the loop.

One-off check from a laptop (sends an alert if ROI is above threshold):
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... ./venv/bin/python alerts.py
"""

import fcntl
import math
import os
import sys
import threading
import time
import traceback

from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks
from optimizer import optimize, DEFAULT_CARDS
import notify

# Base scenario — must mirror the dashboard defaults in web/app.js `state`.
BASE_BUDGET_CLP = 5_000_000
BASE_BUDA_FEE_PCT = 0.25
BASE_USDC_USD = 1.0

# Alert when ROI exceeds this (percent), then again at every further STEP.
# e.g. threshold 2.0 + step 0.5 -> alerts at 2.0%, 2.5%, 3.0%, 3.5%, ...
ROI_THRESHOLD = float(os.environ.get("ALERT_ROI_THRESHOLD", "2.0"))
ROI_STEP = float(os.environ.get("ALERT_ROI_STEP", "0.5"))
INTERVAL_S = int(os.environ.get("ALERT_INTERVAL_S", "600"))  # 10 min

DASHBOARD_URL = "https://web-production-cae25.up.railway.app/"
LOCK_PATH = "/tmp/arbitrage_alerts.lock"

# Highest band already alerted; None = below threshold / re-armed.
_last_band = None


def alert_band(roi):
    """
    Which 0.5% band the ROI falls in, or None if at/below the threshold.
      roi <= 2.0            -> None   (re-armed)
      2.0 < roi < 2.5       -> 0
      2.5 <= roi < 3.0      -> 1
      3.0 <= roi < 3.5      -> 2 ...
    """
    if roi <= ROI_THRESHOLD:
        return None
    # +1e-9 so a value landing exactly on a step isn't pushed down by float error
    return int(math.floor((roi - ROI_THRESHOLD) / ROI_STEP + 1e-9))


def band_level(band):
    """The ROI percentage this band represents (band 0 -> 2.0, band 1 -> 2.5)."""
    return ROI_THRESHOLD + band * ROI_STEP


def build_alert(roi, band, summary, allocs, visa_fx, mc_fx, buda_ask):
    used = [a for a in allocs if a.clp > 0.5]
    cards = ", ".join(f"{a.card} {a.clp:,.0f} CLP" for a in used) or "none"
    fx = lambda v: f"{v:,.2f}" if v is not None else "n/a"
    return (
        f"🚨 <b>Arbitrage window — {band_level(band):.1f}%+</b>\n\n"
        f"ROI <b>{roi:.3f}%</b>  (crossed the {band_level(band):.1f}% level)\n"
        f"Profit <b>${summary['total_profit_usd']:,.2f}</b> "
        f"on {summary['total_clp']:,.0f} CLP\n\n"
        f"Visa {fx(visa_fx)} · MC {fx(mc_fx)} · Buda {fx(buda_ask)}\n"
        f"Cards: {cards}\n\n"
        f"{DASHBOARD_URL}"
    )


def _retry(label, fn, attempts=4, base_delay=4):
    """
    Call fn(), retrying transient upstream failures with linear backoff. The
    scrapers sit behind Cloudflare/Akamai, which intermittently return edge
    errors (e.g. HTTP 520) that succeed on a retry seconds later. Re-raises the
    last error if every attempt fails.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                wait = base_delay * (i + 1)
                print(f"  ! {label} attempt {i + 1}/{attempts} failed: {e} "
                      f"— retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
    raise last


def _try(label, fn):
    """Fetch a source, retrying transient errors. Returns None (never raises) if
    the webpage is genuinely unavailable, so one broken source doesn't kill the
    whole tick."""
    try:
        return _retry(label, fn)
    except Exception as e:
        print(f"  ! {label} unavailable after retries: {e}", file=sys.stderr)
        return None


def check_once():
    global _last_band

    visa = _try("visa", lambda: get_visa_rate("CLP", "USD", 1, 0))
    mc = _try("mastercard", lambda: get_mastercard_rate("CLP", "USD", 1, 0))
    asks = _try("buda", get_buda_asks)

    visa_fx = visa["reverse_rate"] if visa else None
    mc_fx = mc["reverse_rate"] if mc else None
    buda_ask = asks[0][0] if asks else None

    # Base-scenario ROI, using whatever rates we actually got. Buda is required
    # (it's the whole trade); cards are included only when their network's rate
    # is available. A Mastercard-only failure still yields a valid ROI because
    # the base scenario is virtually always won by Fidelity (Visa).
    roi = None
    allocs = summary = None
    if asks:
        usable = [c for c in DEFAULT_CARDS
                  if (c.network == "Visa" and visa_fx is not None)
                  or (c.network == "Mastercard" and mc_fx is not None)]
        if usable:
            allocs, summary = optimize(
                usable, visa_fx, mc_fx, asks,
                total_budget_clp=BASE_BUDGET_CLP,
                buda_fee_pct=BASE_BUDA_FEE_PCT,
                usdc_usd=BASE_USDC_USD,
            )
            roi = summary["roi_pct"]

    band = alert_band(roi) if roi is not None else None
    alerted = False
    if roi is not None:
        if band is None:
            _last_band = None              # back at/below threshold — re-arm
        elif _last_band is None or band > _last_band:
            # Entered a new, higher band (2.0 -> 2.5 -> 3.0 ...): alert once.
            if notify.enabled():
                try:
                    notify.send_message(
                        build_alert(roi, band, summary, allocs, visa_fx, mc_fx, buda_ask)
                    )
                    alerted = True
                except Exception as e:
                    print(f"  ! Telegram send failed: {e}", file=sys.stderr)
            else:
                print("  ! ROI above threshold but Telegram env vars unset —"
                      " no alert sent.", file=sys.stderr)
            # Advance the band when we alerted, or when Telegram isn't
            # configured. A transient send failure leaves it unchanged so the
            # next tick retries instead of silently skipping the step.
            if alerted or not notify.enabled():
                _last_band = band

    def fx(v):
        return f"{v:.2f}" if v is not None else "n/a"
    missing = [s for s, v in (("Visa", visa_fx), ("MC", mc_fx), ("Buda", buda_ask))
               if v is None]
    if roi is not None:
        state = f"band {band} = {band_level(band):.1f}%+" if band is not None \
            else f"below {ROI_THRESHOLD:.2f}%"
        roi_txt = f"ROI {roi:.3f}% ({state})"
    else:
        roi_txt = "ROI n/a"
    print(
        f"[alerts {time.strftime('%Y-%m-%d %H:%M:%S %Z')}] "
        f"Visa {fx(visa_fx)} | MC {fx(mc_fx)} | Buda {fx(buda_ask)} | {roi_txt}"
        + (f"  [degraded: {', '.join(missing)} unavailable]" if missing else "")
        + ("  → ALERT SENT" if alerted else ""),
        flush=True,
    )


def _loop():
    while True:
        try:
            check_once()
        except Exception:
            traceback.print_exc()
        time.sleep(INTERVAL_S)


def start():
    """Start the alert loop in a daemon thread — in one gunicorn worker only."""
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()                       # another worker owns the loop
        return
    start._lock = lock                     # held for the life of the process
    threading.Thread(target=_loop, name="roi-alerts", daemon=True).start()


if __name__ == "__main__":
    check_once()
