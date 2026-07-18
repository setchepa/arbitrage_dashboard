"""
Rate collector — one shot, meant to be run on a schedule (every 10 minutes).

1. Fetches the three live datapoints (Visa CLP/USD, Mastercard CLP/USD, Buda best
   ask CLP/USDC) and appends a row to rate_snapshots.
2. Runs the optimizer on the BASE SCENARIO and, if ROI clears the threshold,
   sends a Telegram alert.

Alerting is edge-triggered: it fires once when ROI crosses from below the
threshold to above it, then stays quiet until ROI drops back under and crosses
again. The latch lives in the alert_state table so it survives container
restarts. Telegram is skipped cleanly if its env vars aren't set.

Run on Railway as a cron service:
    start command : python collect.py
    cron schedule : */10 * * * *

Locally:
    DATABASE_URL=postgresql://... ./venv/bin/python collect.py

Exits non-zero on failure so the scheduler surfaces the error.
"""

import math
import os
import sys
import traceback

from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks
from optimizer import optimize, DEFAULT_CARDS
import db
import notify

# Base scenario — must mirror the dashboard defaults in web/app.js `state`.
BASE_BUDGET_CLP = 5_000_000
BASE_BUDA_FEE_PCT = 0.30
BASE_USDC_USD = 1.0

# Alert when ROI exceeds this (percent), then again at every further STEP.
# e.g. threshold 2.0 + step 0.5 -> alerts at 2.0%, 2.5%, 3.0%, 3.5%, ...
ROI_THRESHOLD = float(os.environ.get("ALERT_ROI_THRESHOLD", "2.0"))
ROI_STEP = float(os.environ.get("ALERT_ROI_STEP", "0.5"))

DASHBOARD_URL = "https://web-production-cae25.up.railway.app/"


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
    return (
        f"🚨 <b>Arbitrage window — {band_level(band):.1f}%+</b>\n\n"
        f"ROI <b>{roi:.3f}%</b>  (crossed the {band_level(band):.1f}% level)\n"
        f"Profit <b>${summary['total_profit_usd']:,.2f}</b> "
        f"on {summary['total_clp']:,.0f} CLP\n\n"
        f"Visa {visa_fx:,.2f} · MC {mc_fx:,.2f} · Buda {buda_ask:,.2f}\n"
        f"Cards: {cards}\n\n"
        f"{DASHBOARD_URL}"
    )


def collect_once():
    visa = get_visa_rate("CLP", "USD", 1, 0)
    mc = get_mastercard_rate("CLP", "USD", 1, 0)
    asks = get_buda_asks()

    visa_fx = visa["reverse_rate"]
    mc_fx = mc["reverse_rate"]
    buda_ask = asks[0][0]

    # Numbers only, 2 decimals (the NUMERIC(12,2) columns enforce it; we round
    # here too so the logged line matches exactly what lands in the table).
    row = {
        "visa": round(visa_fx, 2),   # CLP per USD
        "mc": round(mc_fx, 2),       # CLP per USD
        "buda": round(buda_ask, 2),  # CLP per USDC
    }

    # Base-scenario ROI (full precision rates, not the rounded stored values).
    allocs, summary = optimize(
        DEFAULT_CARDS, visa_fx, mc_fx, asks,
        total_budget_clp=BASE_BUDGET_CLP,
        buda_fee_pct=BASE_BUDA_FEE_PCT,
        usdc_usd=BASE_USDC_USD,
    )
    roi = summary["roi_pct"]

    with db.connect() as conn:
        db.init_schema(conn)                   # idempotent bootstrap
        new_id, captured_at = db.insert_snapshot(row, conn)

        band = alert_band(roi)
        last_band = db.get_last_band(conn)
        alerted = False

        if band is None:
            # Back at/below the threshold — re-arm so the next rise alerts again.
            if last_band is not None:
                db.set_last_band(None, conn=conn)
        elif last_band is None or band > last_band:
            # Entered a new, higher band (2.0 -> 2.5 -> 3.0 ...): alert once.
            if notify.enabled():
                try:
                    notify.send_message(
                        build_alert(roi, band, summary, allocs, visa_fx, mc_fx, buda_ask)
                    )
                    alerted = True
                except Exception as e:            # never lose the snapshot
                    print(f"  ! Telegram send failed: {e}", file=sys.stderr)
            else:
                print("  ! ROI above threshold but Telegram env vars unset —"
                      " no alert sent.", file=sys.stderr)
            # Advance the band when we actually alerted, or when Telegram isn't
            # configured at all. A transient send failure leaves it unchanged so
            # the next tick retries instead of silently skipping the step.
            if alerted or not notify.enabled():
                db.set_last_band(band, mark_alert=alerted, conn=conn)

    state = f"band {band} = {band_level(band):.1f}%+" if band is not None \
        else f"below {ROI_THRESHOLD:.2f}%"
    print(
        f"[{captured_at:%Y-%m-%d %H:%M:%S %Z}] snapshot #{new_id} — "
        f"Visa {row['visa']:.2f} | MC {row['mc']:.2f} | Buda {row['buda']:.2f} "
        f"| ROI {roi:.3f}% ({state})"
        + ("  → ALERT SENT" if alerted else "")
    )
    return new_id


if __name__ == "__main__":
    try:
        collect_once()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
