"""
Binance connectors for the second arbitrage route (dashboard page 2):

    CLP --Buda--> USDC --Binance Spot--> USDT --Binance P2P--> CLP

Legs handled here (the Buda USDC/CLP leg lives in buda_rate.py):
  - Spot USDC/USDT : buy USDT with USDC (sell USDC on the USDCUSDT pair -> hit
    the bids). Public market data via data-api.binance.vision, because the main
    api.binance.com host returns HTTP 451 (geo-block) from many IPs while this
    official market-data mirror does not.
  - P2P USDT/CLP   : sell USDT for CLP via the C2C advertisement search.

No API keys — all endpoints are public. Uses curl_cffi (Chrome impersonation),
already a project dependency.
"""

from curl_cffi import requests as creq

SPOT_HOST = "https://data-api.binance.vision"
SPOT_HOST_FALLBACKS = ["https://api.binance.com", "https://api-gcp.binance.com"]
P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
TIMEOUT = 25


def _session():
    return creq.Session(impersonate="chrome")


# --------------------------------------------------------------------------- #
#  Binance Spot — USDC/USDT
# --------------------------------------------------------------------------- #
def get_spot_book(symbol="USDCUSDT", limit=100):
    """
    Live order book for a Binance spot pair.
    Returns {'bids': [(price, qty), ...], 'asks': [(price, qty), ...]}, best
    first. For USDCUSDT the price is USDT per USDC.
    """
    sess = _session()
    last = None
    for host in [SPOT_HOST, *SPOT_HOST_FALLBACKS]:
        try:
            r = sess.get(f"{host}/api/v3/depth",
                         params={"symbol": symbol, "limit": limit}, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            return {
                "bids": [(float(p), float(q)) for p, q in d["bids"]],
                "asks": [(float(p), float(q)) for p, q in d["asks"]],
                "host": host,
            }
        except Exception as e:               # 451 geo-block etc. -> try next host
            last = e
    raise RuntimeError(f"All Binance spot hosts failed: {last}")


def get_usdc_usdt(limit=100):
    """
    USDC -> USDT conversion (we hold USDC, want USDT). Buying USDT means selling
    USDC on USDCUSDT, so the effective rate is the best bid (USDT per USDC).
    """
    book = get_spot_book("USDCUSDT", limit)
    best_bid = book["bids"][0][0] if book["bids"] else None
    best_ask = book["asks"][0][0] if book["asks"] else None
    return {
        "source": "Binance Spot",
        "pair": "USDCUSDT",
        "usdt_per_usdc": best_bid,   # sell USDC -> receive USDT at the bid
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bids": book["bids"],        # walk these to sell a large USDC amount
        "asks": book["asks"],
        "host": book["host"],
    }


# --------------------------------------------------------------------------- #
#  Binance P2P — USDT/CLP
# --------------------------------------------------------------------------- #
def get_p2p_ads(asset="USDT", fiat="CLP", trade_type="SELL", rows=20, pay_types=None):
    """
    C2C advertisement search. `trade_type="SELL"` = *we* sell USDT for CLP, so
    these are the merchant buy-orders we could hit; the API returns them best
    (highest CLP/USDT) first. Returns a list of normalized ad dicts.
    """
    payload = {
        "asset": asset, "fiat": fiat, "tradeType": trade_type,
        "page": 1, "rows": rows, "payTypes": pay_types or [], "publisherType": None,
    }
    r = _session().post(P2P_URL, json=payload, timeout=TIMEOUT,
                        headers={"Content-Type": "application/json"})
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"P2P search failed: {body.get('message')!r}")

    ads = []
    for item in body.get("data", []):
        adv = item["adv"]
        who = item.get("advertiser", {})
        ads.append({
            "price": float(adv["price"]),                              # CLP per USDT
            "available_usdt": float(adv.get("tradableQuantity") or 0),
            "min_clp": float(adv.get("minSingleTransAmount") or 0),
            "max_clp": float(adv.get("maxSingleTransAmount") or 0),
            "merchant": who.get("nickName"),
            "month_orders": who.get("monthOrderCount"),
            "completion_rate": who.get("monthFinishRate"),
        })
    return ads


def get_usdt_clp_sell(rows=20):
    """Best (highest) CLP/USDT price for selling USDT on P2P, plus the ad list."""
    ads = get_p2p_ads("USDT", "CLP", "SELL", rows)
    return {
        "source": "Binance P2P",
        "pair": "USDT/CLP",
        "clp_per_usdt": ads[0]["price"] if ads else None,   # best sell price
        "ads": ads,
    }


if __name__ == "__main__":
    from buda_rate import get_buda_asks

    print("Leg 1 — Buda (buy USDC with CLP)")
    buda_ask = get_buda_asks()[0][0]
    print(f"  best ask: {buda_ask:,.2f} CLP/USDC")

    print("\nLeg 2 — Binance Spot (buy USDT with USDC)")
    spot = get_usdc_usdt()
    print(f"  {spot['usdt_per_usdc']:.5f} USDT/USDC  (host {spot['host']})")

    print("\nLeg 3 — Binance P2P (sell USDT for CLP)")
    p2p = get_usdt_clp_sell()
    print(f"  best: {p2p['clp_per_usdt']:,.2f} CLP/USDT  ({len(p2p['ads'])} ads)")
    for a in p2p["ads"][:3]:
        print(f"    {a['price']:,.2f} CLP/USDT · {a['available_usdt']:,.0f} USDT · {a['merchant']}")

    # quick end-to-end preview for a sample budget
    clp_in = 5_000_000
    usdc = clp_in / buda_ask
    usdt = usdc * spot["usdt_per_usdc"]
    clp_out = usdt * p2p["clp_per_usdt"]
    print(f"\nPreview: {clp_in:,.0f} CLP -> {usdc:,.2f} USDC -> {usdt:,.2f} USDT "
          f"-> {clp_out:,.0f} CLP  ({(clp_out/clp_in - 1) * 100:+.3f}%, gross of fees)")
