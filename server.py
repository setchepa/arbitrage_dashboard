"""
Flask backend for the CLP<->USD Arbitrage Dashboard (redesigned front-end).

Serves the hand-built HTML/CSS/JS page in web/ and exposes one JSON endpoint,
/api/rates, that returns live Visa & Mastercard reverse rates plus Buda's live
order book. The optimizer is re-implemented in JS on the client so the sidebar
controls recompute instantly; this backend only supplies the live market data.

Local:      ./venv/bin/python server.py            # http://localhost:8600
Production: gunicorn server:app --bind 0.0.0.0:$PORT   (see Procfile)
"""

import os
import time

from flask import Flask, jsonify, request, send_from_directory

from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8600))
    app.run(host="0.0.0.0", port=port)
