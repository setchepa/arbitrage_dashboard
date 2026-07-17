"""
Mastercard currency-conversion scraper.

Mirrors the UI at:
  https://www.mastercard.com/us/en/personal/get-support/currency-exchange-rate-converter.html

The page is JS-driven and behind Akamai bot protection (TLS-fingerprint based),
so cloudscraper does NOT get through. We use curl_cffi, which impersonates a real
Chrome TLS/JA3 fingerprint, and call the same backend the widget uses:
  /marketingservices/public/mccom-services/currency-conversions/conversion-rates

UI -> API parameter mapping (read straight from the widget's JS):
  UI "From" currency ==> transaction_currency
  UI "To"   currency ==> cardholder_billing_currency
  UI "Amount"        ==> transaction_amount
  UI "Bank fee" (%)  ==> bank_fee
  exchange_date "0000-00-00" means "now" (latest available).

The API returns only conversionRate (1 transaction_currency in billing_currency);
we compute the reverse rate (billing per transaction) ourselves.
"""

from curl_cffi import requests as creq

MC_PAGE = (
    "https://www.mastercard.com/us/en/personal/get-support/"
    "currency-exchange-rate-converter.html"
)
MC_ENDPOINT = (
    "https://www.mastercard.com/marketingservices/public/mccom-services/"
    "currency-conversions/conversion-rates"
)


def get_mastercard_rate(from_curr, to_curr, amount=1, fee=0, exchange_date="0000-00-00"):
    """
    Replicate the Mastercard converter for "From `from_curr` to `to_curr`".
    Returns a dict; `reverse_rate` is `to_curr` per 1 `from_curr` -> inverted to
    give `from_curr` per 1 `to_curr` (our canonical CLP-per-USD direction).
    """
    session = creq.Session(impersonate="chrome")
    session.get(MC_PAGE, timeout=40)  # warm cookies / clear Akamai

    params = {
        "exchange_date": exchange_date,
        "transaction_currency": from_curr,
        "cardholder_billing_currency": to_curr,
        "bank_fee": str(fee),
        "transaction_amount": str(amount),
    }
    resp = session.get(
        MC_ENDPOINT,
        params=params,
        headers={"Referer": MC_PAGE, "Accept": "application/json, text/plain, */*"},
        timeout=40,
    )
    resp.raise_for_status()
    d = resp.json()["data"]

    conversion_rate = float(d["conversionRate"])   # 1 from_curr in to_curr
    return {
        "source": "Mastercard",
        "from_currency": d["transCurr"],           # e.g. CLP
        "to_currency": d["crdhldBillCurr"],        # e.g. USD
        "amount": float(d["transAmt"]),
        "exchange_rate": conversion_rate,          # 1 from_curr -> to_curr
        "converted_amount": float(d["crdhldBillAmt"]),
        "reverse_rate": (1.0 / conversion_rate) if conversion_rate else None,  # to->from
        "as_of_date": d["fxDate"],
        "bank_fee_pct": float(fee),
    }


if __name__ == "__main__":
    result = get_mastercard_rate(from_curr="CLP", to_curr="USD", amount=1, fee=0)
    print("Mastercard exchange rate (1 {} -> {}): {}".format(
        result["from_currency"], result["to_currency"], result["exchange_rate"]))
    print(f"  Reverse (1 {result['to_currency']} -> {result['from_currency']}): "
          f"{result['reverse_rate']:.6f}")
    print(f"  As of : {result['as_of_date']}")
