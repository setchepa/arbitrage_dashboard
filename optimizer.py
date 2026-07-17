"""
Arbitrage optimizer for the CLP -> USDC -> USD -> pay-card loop.

Loop (all profit in USD):
  1. Charge US card (USD) to fund CLP; network converts USD->CLP at card_fx
     (CLP per USD). Cashback earned in USD = (CLP/card_fx) * cashback%.
  2. Spend that CLP on Buda to buy USDC, minus a 0.3% fee on Monto.
  3. Transfer USDC -> Robinhood, sell USDC->USD at usdc_usd (~1.0).
  4. Pay the card with those USD.

Per CLP spent on card i:
  revenue_usd_per_clp  = (0.997 / buda_ask_price) * usdc_usd     (slippage-aware)
  cost_usd_per_clp     = (1 - cashback_i) / card_fx_i            (constant per card)
  profit_usd_per_clp   = revenue - cost

Because Buda's marginal price only rises as we buy more (order-book slippage),
revenue_per_clp is non-increasing in total CLP deployed, while each card's cost
is constant. So a greedy fill is optimal: spend cheapest-cost cards first, drawing
the cheapest Buda liquidity first, and stop the moment marginal profit turns
negative for the best remaining card.
"""

from dataclasses import dataclass, field


@dataclass
class Card:
    name: str
    network: str          # "Visa" or "Mastercard"
    cashback: float       # fraction, e.g. 0.02 for 2%
    cap_clp: float        # per-card CLP spend cap (inf if none)
    fx: float = 0.0       # CLP per USD for this card's network (filled at runtime)

    def cost_coeff(self):
        """USD cost per 1 CLP spent on this card, after cashback."""
        return (1.0 - self.cashback) / self.fx


@dataclass
class Allocation:
    card: str
    clp: float = 0.0            # CLP routed through this card
    usd_charged: float = 0.0    # USD the card bills you
    cashback_usd: float = 0.0   # USD cashback earned
    usdc_bought: float = 0.0    # USDC acquired on Buda (after 0.3%)
    usd_from_sale: float = 0.0  # USD received on Robinhood
    profit_usd: float = 0.0


DEFAULT_CARDS = [
    Card("Fidelity",   "Visa",       0.020, 5_000_000),
    Card("CapitalOne", "Mastercard", 0.015, float("inf")),
    Card("Chase",      "Visa",       0.010, float("inf")),
]


def optimize(cards, visa_fx, mc_fx, asks, total_budget_clp,
             buda_fee_pct=0.3, usdc_usd=1.0):
    """
    cards          : list[Card]
    visa_fx, mc_fx : CLP-per-USD reverse rates for the two networks
    asks           : list of (price_clp_per_usdc, size_usdc), best price first
    total_budget_clp: overall CLP cap across all cards
    Returns (allocations: list[Allocation], summary: dict).
    """
    fee_mult = 1.0 - buda_fee_pct / 100.0

    # assign each card its network FX
    for c in cards:
        c.fx = visa_fx if c.network == "Visa" else mc_fx

    # order cards cheapest-cost first (best card gets used first)
    ordered = sorted(cards, key=lambda c: c.cost_coeff())
    allocs = {c.name: Allocation(card=c.name) for c in cards}

    # mutable copy of the ask book: [price, remaining_size_usdc]
    book = [[p, s] for p, s in asks]
    lvl = 0
    budget_left = float(total_budget_clp)

    for card in ordered:
        cap_left = card.cap_clp
        while budget_left > 1e-9 and cap_left > 1e-9 and lvl < len(book):
            price, size = book[lvl]
            if size <= 1e-12:
                lvl += 1
                continue
            # marginal USD revenue per 1 CLP at this price level
            rev_coeff = (fee_mult / price) * usdc_usd
            if rev_coeff <= card.cost_coeff():
                # not profitable for this card; all later cards cost more -> stop
                break
            level_clp_capacity = price * size          # CLP this level can absorb
            take_clp = min(level_clp_capacity, cap_left, budget_left)
            take_usdc = (take_clp / price) * fee_mult   # after 0.3% fee

            a = allocs[card.name]
            a.clp += take_clp
            a.usdc_bought += take_usdc
            a.usd_charged += take_clp / card.fx
            a.cashback_usd += (take_clp / card.fx) * card.cashback

            book[lvl][1] -= take_clp / price            # consume USDC size
            cap_left -= take_clp
            budget_left -= take_clp
        else:
            continue
        # inner loop hit a `break` (unprofitable) -> stop assigning further cards
        break

    # finalize per-card economics
    result = []
    tot = dict(clp=0.0, usd_charged=0.0, cashback=0.0, usdc=0.0,
               usd_sale=0.0, profit=0.0)
    for c in cards:
        a = allocs[c.name]
        a.usd_from_sale = a.usdc_bought * usdc_usd
        a.profit_usd = a.usd_from_sale + a.cashback_usd - a.usd_charged
        result.append(a)
        tot["clp"] += a.clp
        tot["usd_charged"] += a.usd_charged
        tot["cashback"] += a.cashback_usd
        tot["usdc"] += a.usdc_bought
        tot["usd_sale"] += a.usd_from_sale
        tot["profit"] += a.profit_usd

    # USDC premium income: extra USD earned when USDC sells above $1 on Robinhood
    # (negative if it sells below $1). total_usd_from_sale = base ($1 each) + premium.
    total_usdc_premium = tot["usdc"] * (usdc_usd - 1.0)

    summary = {
        "total_clp": tot["clp"],
        "total_usd_charged": tot["usd_charged"],
        "total_cashback_usd": tot["cashback"],
        "total_usdc": tot["usdc"],
        "total_usd_from_sale": tot["usd_sale"],
        "usdc_usd": usdc_usd,
        "total_usdc_base_usd": tot["usdc"] * 1.0,        # value at a $1 peg
        "total_usdc_premium_usd": total_usdc_premium,    # income from USDC > $1
        "total_profit_usd": tot["profit"],
        "roi_pct": (tot["profit"] / tot["usd_charged"] * 100.0)
                   if tot["usd_charged"] > 1e-9 else 0.0,
        "profitable": tot["profit"] > 0,
        "buda_effective_rate": (tot["clp"] / tot["usdc"]) if tot["usdc"] > 1e-9 else None,
    }
    return result, summary
