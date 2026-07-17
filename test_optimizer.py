from visa_rate import get_visa_rate
from mastercard_rate import get_mastercard_rate
from buda_rate import get_buda_asks
from optimizer import optimize, DEFAULT_CARDS

print("Fetching live rates...")
visa = get_visa_rate("CLP","USD",1,0)
mc = get_mastercard_rate("CLP","USD",1,0)
asks = get_buda_asks()
print(f"  Visa fx (Fidelity/Chase): {visa['reverse_rate']:.4f} CLP/USD")
print(f"  MC fx   (CapitalOne)    : {mc['reverse_rate']:.4f} CLP/USD")
print(f"  Buda best ask           : {asks[0][0]:.2f} CLP/USDC  (depth: {len(asks)} levels)")

allocs, summ = optimize(DEFAULT_CARDS, visa['reverse_rate'], mc['reverse_rate'],
                        asks, total_budget_clp=12_000_000)

print("\n--- ALLOCATION ---")
for a in allocs:
    if a.clp > 0:
        print(f"  {a.card:11s}: spend {a.clp:>12,.0f} CLP | charged ${a.usd_charged:,.2f} "
              f"| cashback ${a.cashback_usd:,.2f} | USDC {a.usdc_bought:,.2f} | profit ${a.profit_usd:,.2f}")
    else:
        print(f"  {a.card:11s}: (not used)")

print("\n--- SUMMARY ---")
print(f"  Total CLP deployed : {summ['total_clp']:,.0f} CLP")
print(f"  Total USD charged  : ${summ['total_usd_charged']:,.2f}")
print(f"  Total cashback     : ${summ['total_cashback_usd']:,.2f}")
print(f"  USDC -> USD (sale)  : ${summ['total_usd_from_sale']:,.2f}")
print(f"  Buda eff. rate     : {summ['buda_effective_rate']:.4f} CLP/USDC")
print(f"  >>> PROFIT         : ${summ['total_profit_usd']:,.2f}  (ROI {summ['roi_pct']:.3f}%)")
print(f"  Profitable?        : {summ['profitable']}")
