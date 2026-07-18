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

# Alert when ROI exceeds this (percent). Override with ALERT_ROI_THRESHOLD.
ROI_THRESHOLD = float(os.environ.get("ALERT_ROI_THRESHOLD", "2.0"))

DASHBOARD_URL = "https://web-production-cae25.up.railway.app/"


def build_alert(roi, summary, allocs, visa_fx, mc_fx, buda_ask):
    used = [a for a in allocs if a.clp > 0.5]
    cards = ", ".join(f"{a.card} {a.clp:,.0f} CLP" for a in used) or "none"
    return (
        "🚨 <b>Arbitrage window open</b>\n\n"
        f"ROI <b>{roi:.3f}%</b>  (threshold {ROI_THRESHOLD:.2f}%)\n"
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

        is_above = roi > ROI_THRESHOLD
        was_above = db.get_was_above(conn)
        alerted = False

        if is_above and not was_above:
            if notify.enabled():
                try:
                    notify.send_message(
                        build_alert(roi, summary, allocs, visa_fx, mc_fx, buda_ask)
                    )
                    alerted = True
                except Exception as e:            # never lose the snapshot
                    print(f"  ! Telegram send failed: {e}", file=sys.stderr)
            else:
                print("  ! ROI above threshold but Telegram env vars unset —"
                      " no alert sent.", file=sys.stderr)
            db.set_was_above(True, mark_alert=alerted, conn=conn)
        elif not is_above and was_above:
            db.set_was_above(False, conn=conn)    # re-arm for the next crossing

    state = "ABOVE" if is_above else "below"
    print(
        f"[{captured_at:%Y-%m-%d %H:%M:%S %Z}] snapshot #{new_id} — "
        f"Visa {row['visa']:.2f} | MC {row['mc']:.2f} | Buda {row['buda']:.2f} "
        f"| ROI {roi:.3f}% ({state} {ROI_THRESHOLD:.2f}%)"
        + ("  → ALERT SENT" if alerted else "")
    )
    return new_id


if __name__ == "__main__":
    try:
        collect_once()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
