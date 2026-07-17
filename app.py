"""
Arbitrage Dashboard — CLP -> USDC (Buda) -> USD (Robinhood) -> pay US card.

Live web app. Run with:
    ./venv/bin/streamlit run app.py

Pulls Visa, Mastercard and Buda rates on demand and optimizes how much CLP to
route through each credit card to maximize USD profit, subject to per-card caps
and a total budget.
"""

import streamlit as st

from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks
from optimizer import optimize, Card

st.set_page_config(page_title="CLP↔USD Arbitrage Dashboard", page_icon="💱", layout="wide")


@st.cache_data(show_spinner=False)
def fetch_rates():
    visa = get_visa_rate("CLP", "USD", 1, 0)
    mc = get_mastercard_rate("CLP", "USD", 1, 0)
    asks = get_buda_asks()
    return visa, mc, asks


# ------------------------------------------------------------------ Sidebar
st.sidebar.header("⚙️ Parameters")
total_budget = st.sidebar.number_input(
    "Total budget (CLP)", value=12_000_000, step=500_000, min_value=0)
buda_fee = st.sidebar.number_input(
    "Buda fee removed from Monto (%)", value=0.3, step=0.05, format="%.2f")
usdc_usd = st.sidebar.number_input(
    "USDC → USD rate (Robinhood)", value=1.0, step=0.001, format="%.4f")

st.sidebar.markdown("**Cards**")
cards_cfg = []
for name, network, cb_def, cap_def in [
    ("Fidelity", "Visa", 2.0, 5_000_000),
    ("CapitalOne", "Mastercard", 1.5, 0),
    ("Chase", "Visa", 1.0, 0),
]:
    with st.sidebar.expander(f"{name} ({network})", expanded=False):
        cb = st.number_input(f"{name} cashback %", value=cb_def, step=0.1,
                             key=f"cb_{name}", format="%.2f")
        capped = st.checkbox(f"{name} has a CLP cap", value=(cap_def > 0),
                             key=f"has_cap_{name}")
        cap = st.number_input(f"{name} cap (CLP)", value=cap_def or 5_000_000,
                              step=500_000, key=f"cap_{name}",
                              disabled=not capped) if capped else float("inf")
    cards_cfg.append(Card(name, network, cb / 100.0,
                          cap if capped else float("inf")))

if st.sidebar.button("🔄 Refresh live rates", type="primary"):
    fetch_rates.clear()

# ------------------------------------------------------------------ Main
st.title("💱 CLP ↔ USD Arbitrage Dashboard")
st.caption("Loop: charge US card (USD→CLP) → buy USDC on Buda → sell USDC→USD on "
           "Robinhood → pay the card. Profit is in USD; cashback is earned in USD.")

try:
    with st.spinner("Fetching live rates from Visa, Mastercard and Buda…"):
        visa, mc, asks = fetch_rates()
except Exception as e:
    st.error(f"Failed to fetch rates: {e}")
    st.stop()

visa_fx = visa["reverse_rate"]
mc_fx = mc["reverse_rate"]
best_ask = asks[0][0]

c1, c2, c3 = st.columns(3)
c1.metric("Visa  (Fidelity / Chase)", f"{visa_fx:,.2f} CLP/USD", help=f"As of {visa['as_of_date']}")
c2.metric("Mastercard  (CapitalOne)", f"{mc_fx:,.2f} CLP/USD", help=f"As of {mc['as_of_date']}")
c3.metric("Buda  best ask", f"{best_ask:,.2f} CLP/USDC", help=f"{len(asks)} order-book levels")

# ---- optimize
allocs, summ = optimize(cards_cfg, visa_fx, mc_fx, asks,
                        total_budget_clp=total_budget,
                        buda_fee_pct=buda_fee, usdc_usd=usdc_usd)

st.divider()

# ---- verdict banner
if summ["profitable"]:
    st.success(f"### ✅ Profitable — deploy {summ['total_clp']:,.0f} CLP "
               f"for **${summ['total_profit_usd']:,.2f}** profit "
               f"(ROI {summ['roi_pct']:.3f}%)")
else:
    st.warning("### ⚠️ Not profitable right now — best action is to deploy nothing.")

# ---- card economics table
st.subheader("Card economics & allocation")
rows = []
for c in sorted(cards_cfg, key=lambda x: x.cost_coeff()):
    a = next(a for a in allocs if a.card == c.name)
    cap_txt = "—" if c.cap_clp == float("inf") else f"{c.cap_clp:,.0f}"
    rows.append({
        "Card": c.name,
        "Network": c.network,
        "Cashback": f"{c.cashback*100:.1f}%",
        "Cap (CLP)": cap_txt,
        "Cost/CLP (USD)": f"{c.cost_coeff():.8f}",
        "→ Spend (CLP)": f"{a.clp:,.0f}" if a.clp else "—",
        "USDC bought": f"{a.usdc_bought:,.2f}" if a.clp else "—",
        "Cashback (USD)": f"${a.cashback_usd:,.2f}" if a.clp else "—",
        "Profit (USD)": f"${a.profit_usd:,.2f}" if a.clp else "—",
    })
st.dataframe(rows, use_container_width=True, hide_index=True)
st.caption("Cards are ranked by effective cost per CLP (lower = used first). "
           "The optimizer walks Buda's live order book, so each extra CLP buys "
           "USDC at a slightly worse price (slippage).")

# ---- income breakdown (where the money comes from)
st.subheader("Income breakdown")
premium = summ["total_usdc_premium_usd"]
income_rows = [
    {"Income source": "Sell USDC on Robinhood @ $1.00 (base)",
     "USD": f"${summ['total_usdc_base_usd']:,.2f}"},
    {"Income source": f"USDC premium (USDC @ ${summ['usdc_usd']:.4f} vs $1.00)",
     "USD": f"${premium:,.2f}"},
    {"Income source": "Credit-card cashback",
     "USD": f"${summ['total_cashback_usd']:,.2f}"},
    {"Income source": "— Less: USD charged to cards",
     "USD": f"-${summ['total_usd_charged']:,.2f}"},
    {"Income source": "= Net profit",
     "USD": f"${summ['total_profit_usd']:,.2f}"},
]
st.dataframe(income_rows, use_container_width=True, hide_index=True)
if premium > 0:
    st.caption(f"💰 USDC is trading **above** peg (${summ['usdc_usd']:.4f}), adding "
               f"**${premium:,.2f}** of premium income on {summ['total_usdc']:,.2f} USDC.")
elif premium < 0:
    st.caption(f"⚠️ USDC is trading **below** peg (${summ['usdc_usd']:.4f}), costing "
               f"**${-premium:,.2f}** vs a $1.00 sale.")
else:
    st.caption("USDC assumed exactly at the $1.00 peg — no premium income.")

# ---- totals
st.subheader("Loop totals")
t1, t2, t3, t4, t5 = st.columns(5)
t1.metric("CLP deployed", f"{summ['total_clp']:,.0f}")
t2.metric("USD charged to cards", f"${summ['total_usd_charged']:,.2f}")
t3.metric("Cashback earned", f"${summ['total_cashback_usd']:,.2f}")
t4.metric("USDC premium income", f"${premium:,.2f}")
t5.metric("Net profit", f"${summ['total_profit_usd']:,.2f}", delta=f"{summ['roi_pct']:.3f}% ROI")

if summ["buda_effective_rate"]:
    st.caption(f"Effective Buda purchase rate for this size: "
               f"{summ['buda_effective_rate']:,.2f} CLP/USDC "
               f"(best ask {best_ask:,.2f}).")
