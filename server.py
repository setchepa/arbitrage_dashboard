"""
Flask backend for the CLP<->USD Arbitrage Dashboard (redesigned front-end).

Serves the hand-built HTML/CSS/JS page in web/ and exposes one JSON endpoint,
/api/rates, that returns live Visa & Mastercard reverse rates plus Buda's live
order book. The optimizer is re-implemented in JS on the client so the sidebar
controls recompute instantly; this backend only supplies the live market data.

Local:      ./venv/bin/python server.py            # http://localhost:8600
Production: gunicorn server:app --bind 0.0.0.0:$PORT   (see Procfile)
"""

import calendar
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, jsonify, request, send_from_directory

from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# Short in-memory cache so repeated page loads (and multiple viewers) don't hammer
# Visa/Mastercard/Buda on every request. "Refresh live rates" bypasses it with
# ?force=1. Note: with >1 gunicorn worker each worker keeps its own cache.
CACHE_TTL = 60  # seconds
_cache = {"data": None, "ts": 0.0}


def _fetch_live():
    visa = get_visa_rate("CLP", "USD", 1, 0)
    mc = get_mastercard_rate("CLP", "USD", 1, 0)
    asks = get_buda_asks()
    return {
        "ok": True,
        "visa_fx": visa["reverse_rate"],
        "visa_date": visa["as_of_date"],
        "mc_fx": mc["reverse_rate"],
        "mc_date": mc["as_of_date"],
        "buda_best_ask": asks[0][0],
        "buda_levels": len(asks),
        "buda_asks": asks,   # [[price_clp_per_usdc, size_usdc], ...]
    }


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


# Buda is the only source that moves intraday (Visa/Mastercard publish daily
# rates), so it gets its own lightweight endpoint the page can poll ~1/second.
# The short server-side cache means N concurrent viewers still produce at most
# ~2 requests/second to Buda, not N.
BUDA_CACHE_TTL = 0.5  # seconds
_buda_cache = {"data": None, "ts": 0.0}


@app.route("/api/buda")
def api_buda():
    now = time.time()
    if _buda_cache["data"] and (now - _buda_cache["ts"]) < BUDA_CACHE_TTL:
        return jsonify(_buda_cache["data"])
    try:
        asks = get_buda_asks()
        payload = {
            "ok": True,
            "buda_best_ask": asks[0][0],
            "buda_levels": len(asks),
            "buda_asks": asks,
        }
        _buda_cache["data"] = payload
        _buda_cache["ts"] = now
        return jsonify(payload)
    except Exception as e:
        if _buda_cache["data"]:
            stale = dict(_buda_cache["data"])
            stale["stale"] = True
            return jsonify(stale)
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/rates")
def api_rates():
    force = request.args.get("force")
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return jsonify(_cache["data"])
    try:
        payload = _fetch_live()
        _cache["data"] = payload
        _cache["ts"] = now
        return jsonify(payload)
    except Exception as e:
        # Anti-bot blocks / transient errors: serve the last good data if we have
        # it (flagged stale) so the dashboard degrades gracefully instead of dying.
        if _cache["data"]:
            stale = dict(_cache["data"])
            stale["stale"] = True
            return jsonify(stale)
        return jsonify({"ok": False, "error": str(e)}), 502


# Timezone the daily/monthly buckets are computed in. The whole loop is Chile-
# centric (CLP), so we bucket by Santiago local time; override with REPORT_TZ.
REPORT_TZ = os.environ.get("REPORT_TZ", "America/Santiago")


def _report_now():
    try:
        return datetime.now(ZoneInfo(REPORT_TZ)), REPORT_TZ
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo("UTC")), "UTC"


@app.route("/api/stats")
def api_stats():
    """
    Executed-trade profit aggregates for the bar charts:
      - daily   : sum of net_profit per day, current month (zero-filled)
      - monthly : sum of net_profit per month, current year (zero-filled)
    Only executed=1 rows count.
    """
    now_local, tz = _report_now()
    try:
        with db.connect() as conn:
            db.init_schema(conn)
            daily_rows = conn.execute(
                """
                SELECT (captured_at AT TIME ZONE %(tz)s)::date AS d,
                       COALESCE(SUM(net_profit), 0)
                FROM rate_snapshots
                WHERE executed = 1
                  AND date_trunc('month', captured_at AT TIME ZONE %(tz)s)
                      = date_trunc('month', now() AT TIME ZONE %(tz)s)
                GROUP BY d ORDER BY d
                """, {"tz": tz}).fetchall()
            monthly_rows = conn.execute(
                """
                SELECT EXTRACT(MONTH FROM captured_at AT TIME ZONE %(tz)s)::int AS m,
                       COALESCE(SUM(net_profit), 0)
                FROM rate_snapshots
                WHERE executed = 1
                  AND date_trunc('year', captured_at AT TIME ZONE %(tz)s)
                      = date_trunc('year', now() AT TIME ZONE %(tz)s)
                GROUP BY m ORDER BY m
                """, {"tz": tz}).fetchall()

        n_days = calendar.monthrange(now_local.year, now_local.month)[1]
        dmap = {r[0].day: float(r[1]) for r in daily_rows}
        daily = [{"label": str(d), "profit": dmap.get(d, 0.0)}
                 for d in range(1, n_days + 1)]

        mmap = {int(r[0]): float(r[1]) for r in monthly_rows}
        monthly = [{"label": calendar.month_abbr[m], "profit": mmap.get(m, 0.0)}
                   for m in range(1, 13)]

        return jsonify({
            "ok": True, "tz": tz,
            "month_label": now_local.strftime("%B %Y"),
            "year_label": str(now_local.year),
            "daily": daily, "monthly": monthly,
            "daily_total": sum(d["profit"] for d in daily),
            "monthly_total": sum(m["profit"] for m in monthly),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/executed", methods=["POST"])
def api_executed():
    """
    Record an executed trade: inserts a rate_snapshots row with executed=1 and
    the client's current on-screen figures (the scenario the user chose to run).
    """
    data = request.get_json(silent=True) or {}
    try:
        row = {
            "visa": round(float(data["visa"]), 2),
            "mc": round(float(data["mc"]), 2),
            "buda": round(float(data["buda"]), 2),
            "net_profit": round(float(data["net_profit"]), 2),
            "roi": round(float(data["roi"]), 3),
            "executed": 1,
        }
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"bad payload: {e}"}), 400

    try:
        with db.connect() as conn:
            db.init_schema(conn)
            new_id, captured_at = db.insert_snapshot(row, conn)
        return jsonify({"ok": True, "id": new_id, "captured_at": captured_at.isoformat()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8600))
    app.run(host="0.0.0.0", port=port)
