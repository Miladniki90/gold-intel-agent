"""Fetch raw fundamental data for gold from free, official sources.

* FRED (St. Louis Fed): broad dollar index proxy, 10y real yield, CPI, nonfarm payrolls.
  For each series: latest value, previous value, absolute and percent change.
* CFTC Commitments of Traders: managed-money net positioning in COMEX gold futures.

Usage:
    export FRED_API_KEY=...       # free key: https://fred.stlouisfed.org/docs/api/api_key.html
    python scripts/fetch_fundamentals.py     # prints JSON to stdout
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
COT_URL = (
    "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    "?market_and_exchange_names=GOLD%20-%20COMMODITY%20EXCHANGE%20INC."
    "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=2"
)

SERIES = {
    "dxy_proxy": "DTWEXBGS",
    "real_yield_10y": "DFII10",
    "cpi": "CPIAUCSL",
    "nfp": "PAYEMS",
}


def get_api_key():
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("Set the FRED_API_KEY environment variable (free key from fred.stlouisfed.org).")
    return key


def summarize(observations):
    """Turn FRED observations (newest first) into latest/previous/change figures."""
    observations = [o for o in observations if o.get("value") not in (None, ".")]
    if not observations:
        return {"latest": None, "previous": None, "change": None, "change_pct": None}

    latest = observations[0]
    previous = observations[1] if len(observations) > 1 else None
    latest_val = float(latest["value"])
    previous_val = float(previous["value"]) if previous else None

    change = round(latest_val - previous_val, 4) if previous_val is not None else None
    change_pct = (
        round((latest_val - previous_val) / previous_val * 100, 4)
        if previous_val not in (None, 0) else None
    )
    return {
        "latest_date": latest["date"],
        "latest_value": latest_val,
        "previous_date": previous["date"] if previous else None,
        "previous_value": previous_val,
        "change": change,
        "change_pct": change_pct,
    }


def fetch_series(series_id, limit=6, api_key=None):
    query = urllib.parse.urlencode({
        "series_id": series_id, "api_key": api_key or get_api_key(),
        "file_type": "json", "sort_order": "desc", "limit": limit,
    })
    with urllib.request.urlopen(f"{BASE_URL}?{query}", timeout=15) as resp:
        data = json.load(resp)
    return summarize(data.get("observations", []))


def fetch_cot():
    """COT report for COMEX gold: managed-money (non-commercial) long/short and weekly change."""
    try:
        with urllib.request.urlopen(COT_URL, timeout=15) as resp:
            rows = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}
    if not rows:
        return {"error": "no COT data returned"}

    def net_long(row):
        return int(row["noncomm_positions_long_all"]) - int(row["noncomm_positions_short_all"])

    latest = rows[0]
    previous = rows[1] if len(rows) > 1 else None
    latest_net = net_long(latest)
    previous_net = net_long(previous) if previous else None
    return {
        "report_date": latest["report_date_as_yyyy_mm_dd"][:10],
        "managed_money_long": int(latest["noncomm_positions_long_all"]),
        "managed_money_short": int(latest["noncomm_positions_short_all"]),
        "managed_money_net_long": latest_net,
        "previous_report_date": previous["report_date_as_yyyy_mm_dd"][:10] if previous else None,
        "previous_net_long": previous_net,
        "change_in_net_long": (latest_net - previous_net) if previous_net is not None else None,
    }


def main():
    try:
        key = get_api_key()
    except RuntimeError as exc:
        sys.exit(str(exc))
    result = {}
    for name, series_id in SERIES.items():
        try:
            result[name] = fetch_series(series_id, api_key=key)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            result[name] = {"error": str(exc)}
    result["cot_gold"] = fetch_cot()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
