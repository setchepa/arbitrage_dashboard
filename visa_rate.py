"""
Visa exchange-rate calculator scraper.

Mirrors the UI at:
  https://usa.visa.com/support/consumer/travel-support/exchange-rate-calculator.html

The page is JS-driven and sits behind Cloudflare, so plain requests/BeautifulSoup
cannot reach it. We call the same backend endpoint the calculator itself uses
(/cmsapi/fx/rates), using cloudscraper to clear the Cloudflare challenge.

UI -> API parameter mapping (verified against the response's originalValues echo):
  UI "Currencies to exchange: From X to Y"  ==>  toCurr = X, fromCurr = Y
  UI "Amount you paid"                      ==>  amount
  UI "Bank fee"                             ==>  fee   (percent, e.g. 0 for 0%)
"""

import cloudscraper

VISA_ENDPOINT = "https://usa.visa.com/cmsapi/fx/rates"
VISA_REFERER = (
    "https://usa.visa.com/support/consumer/travel-support/"
    "exchange-rate-calculator.html"
)


def get_visa_rate(from_curr, to_curr, amount=1, fee=0, date_mmddyyyy=None):
    """
    Replicate the Visa calculator for "From `from_curr` to `to_curr`".

    Returns a dict with the exchange rate and the converted amount.
    `date_mmddyyyy` defaults to today (US format MM/DD/YYYY) if not given.
    """
    if date_mmddyyyy is None:
        from datetime import datetime
        date_mmddyyyy = datetime.now().strftime("%m/%d/%Y")

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
    params = {
        "amount": str(amount),
        "fee": str(fee),
        "utcConvertedDate": date_mmddyyyy,
        "exchangedate": date_mmddyyyy,
        # NOTE the intentional swap: UI "From X to Y" -> toCurr=X, fromCurr=Y
        "fromCurr": to_curr,
        "toCurr": from_curr,
    }
    resp = scraper.get(
        VISA_ENDPOINT,
        params=params,
        headers={"Referer": VISA_REFERER},
        timeout=40,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        raise RuntimeError(f"Visa API returned status: {data.get('status')!r}")

    ov = data["originalValues"]
    return {
        "source": "Visa",
        "from_currency": ov["fromCurrency"],      # e.g. CLP
        "to_currency": ov["toCurrency"],          # e.g. USD
        "amount": float(ov["fromAmount"]),
        "exchange_rate": float(ov["fxRateVisa"]),          # 1 from_curr in to_curr
        "converted_amount": float(ov["toAmountWithVisaRate"]),
        "reverse_rate": float(data["reverseAmount"]),      # 1 to_curr in from_curr
        "as_of_date": data["disclaimerDate"],
        "bank_fee_pct": float(data["conversionBankFee"]),
    }


if __name__ == "__main__":
    # Your requested inputs: amount 1, From CLP to USD, 0% bank fee
    result = get_visa_rate(from_curr="CLP", to_curr="USD", amount=1, fee=0)
    print("Visa exchange rate (1 {} -> {}): {}".format(
        result["from_currency"], result["to_currency"], result["exchange_rate"]))
    print(f"  Converted amount : {result['converted_amount']} {result['to_currency']}")
    print(f"  Reverse (1 {result['to_currency']} -> {result['from_currency']}): "
          f"{result['reverse_rate']}")
    print(f"  As of            : {result['as_of_date']}")
