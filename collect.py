"""
Rate collector — one shot, meant to be run on a schedule (every 10 minutes).

Fetches the three live datapoints (Visa CLP/USD, Mastercard CLP/USD, Buda best
ask CLP/USDC) and appends a row to the rate_snapshots table.

Run on Railway as a cron service:
    start command : python collect.py
    cron schedule : */10 * * * *

Locally:
    DATABASE_URL=postgresql://... ./venv/bin/python collect.py

Exits non-zero on failure so the scheduler surfaces the error.
"""

import sys
import traceback

from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks
import db


def collect_once():
    visa = get_visa_rate("CLP", "USD", 1, 0)
    mc = get_mastercard_rate("CLP", "USD", 1, 0)
    asks = get_buda_asks()

    # Stored to 2 decimals (the NUMERIC(12,2) columns enforce it; we round here
    # too so the logged line matches exactly what lands in the table).
    row = {
        "visa": round(visa["reverse_rate"], 2),   # CLP per USD
        "mc": round(mc["reverse_rate"], 2),       # CLP per USD
        "buda": round(asks[0][0], 2),             # CLP per USDC
        "visa_as_of": visa["as_of_date"],
        "mc_as_of": mc["as_of_date"],
        "buda_levels": len(asks),
    }

    with db.connect() as conn:
        db.init_schema(conn)                   # idempotent bootstrap
        new_id, captured_at = db.insert_snapshot(row, conn)

    print(
        f"[{captured_at:%Y-%m-%d %H:%M:%S %Z}] snapshot #{new_id} — "
        f"Visa {row['visa']:.2f} | MC {row['mc']:.2f} | "
        f"Buda {row['buda']:.2f} ({row['buda_levels']} levels)"
    )
    return new_id


if __name__ == "__main__":
    try:
        collect_once()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
