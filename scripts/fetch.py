#!/usr/bin/env python3
"""
Pull CAISO hourly generation by fuel type from the EIA-930 API (v2.1.13) and
write a compact daily-margin JSON file for the clean-vs-fossil chart.

Usage:
    python scripts/fetch.py --backfill   # 2021-01-01 through yesterday (Pacific)
    python scripts/fetch.py --refresh    # trailing 45 days, overwrite by date

Requires EIA_API_KEY in the environment. Never logs or writes the key anywhere.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

API_BASE = "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
CAISO_RESPONDENT = "CISO"
CAISO_TZ = ZoneInfo("America/Los_Angeles")

# Verified 2026-08-26 against a live API response for respondent=CISO: these
# are the only 9 fuel codes CAISO actually reports in this route. Codes that
# exist in the route's global facet list (BAT, PS, SNB, WNB, UES, UNK, OES)
# never appear for CISO here -- battery storage is not broken out separately
# in this dataset, which is consistent with OTH running net-negative for
# long stretches (looks like unclassified storage charge/discharge folded
# into "other" rather than a contamination of GEO, which is its own code).
#
#   low-emitting: nuclear, wind, solar, hydro, geothermal
#   fossil:       natural gas, coal, petroleum
#   excluded:     "other" -- not confidently classifiable (see above);
#                 no distinct battery code is ever reported for CAISO here,
#                 so there is nothing to exclude on that basis specifically
FUEL_CLASSIFICATION = {
    "NUC": "low",       # Nuclear
    "WND": "low",       # Wind
    "SUN": "low",       # Solar
    "WAT": "low",       # Hydro (conventional)
    "GEO": "low",       # Geothermal -- distinct code, not lumped into OTH
    "NG": "fossil",     # Natural Gas
    "COL": "fossil",    # Coal
    "OIL": "fossil",    # Petroleum
    "OTH": "excluded",  # Unclassified / apparent storage net -- not confidently placeable
}
EXCLUDED_FUEL_CODES = sorted(c for c, cls in FUEL_CLASSIFICATION.items() if cls == "excluded")

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "caiso-daily.json")
REQUEST_SLEEP_SECONDS = 0.4
PAGE_LENGTH = 5000


def api_key():
    key = os.environ.get("EIA_API_KEY")
    if not key:
        print("ERROR: EIA_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(1)
    return key


def local_offset_str(dt_local):
    """±HH offset string for a naive local date at 00:00, e.g. '-07' or '-08'."""
    aware = datetime(dt_local.year, dt_local.month, dt_local.day, tzinfo=CAISO_TZ)
    offset = aware.utcoffset()
    total_hours = int(offset.total_seconds() // 3600)
    return f"{total_hours:+03d}"


def build_url(params):
    # Facet/data/sort keys use literal brackets, as confirmed against the live
    # API (percent-encoding was not tested and the brief warns not to guess --
    # this exact bracket syntax is what a working curl request used).
    parts = []
    for k, v in params:
        parts.append(f"{k}={urllib.request.quote(str(v), safe='')}")
    return API_BASE + "?" + "&".join(parts)


def fetch_page(start_str, end_str, offset, key):
    params = [
        ("frequency", "local-hourly"),
        ("data[]", "value"),
        ("facets[respondent][]", CAISO_RESPONDENT),
        ("start", start_str),
        ("end", end_str),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("offset", offset),
        ("length", PAGE_LENGTH),
        ("api_key", key),
    ]
    url = build_url(params)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    return json.loads(body)


def fetch_range_rows(start_date, end_date, key):
    """
    Fetch all hourly rows covering [start_date, end_date] inclusive, in CAISO
    local calendar terms. Request bounds are padded by one day on each side
    and the correct DST offset is computed per-boundary; results are then
    filtered client-side on the row's own local date substring, so any
    lexical/DST edge case in the request bounds is self-correcting.
    """
    padded_start = start_date - timedelta(days=1)
    padded_end = end_date + timedelta(days=1)
    start_str = f"{padded_start.isoformat()}T00{local_offset_str(padded_start)}"
    end_str = f"{padded_end.isoformat()}T23{local_offset_str(padded_end)}"

    rows = []
    offset = 0
    total = None
    while total is None or offset < total:
        page = fetch_page(start_str, end_str, offset, key)
        resp = page["response"]
        total = int(resp["total"])
        page_rows = resp["data"]
        rows.extend(page_rows)
        offset += PAGE_LENGTH
        print(f"  fetched {min(offset, total)}/{total} rows", file=sys.stderr)
        if offset < total:
            time.sleep(REQUEST_SLEEP_SECONDS)

    lo, hi = start_date.isoformat(), end_date.isoformat()
    filtered = [r for r in rows if lo <= r["period"][:10] <= hi]
    return filtered


def aggregate_daily(rows):
    """local date -> {'low': GWh, 'fossil': GWh}. Skips excluded/unknown codes."""
    daily_mwh = {}
    skipped_codes = set()
    for r in rows:
        fuel = r["fueltype"]
        cls = FUEL_CLASSIFICATION.get(fuel)
        if cls is None:
            skipped_codes.add(fuel)
            continue
        if cls == "excluded":
            continue
        val = r["value"]
        if val is None or val == "":
            continue  # missing hour: do not fabricate, do not count as zero
        local_date = r["period"][:10]
        bucket = daily_mwh.setdefault(local_date, {"low": 0.0, "fossil": 0.0})
        bucket[cls] += float(val)

    if skipped_codes:
        print(f"WARNING: unrecognized fuel codes encountered and skipped: {sorted(skipped_codes)}",
              file=sys.stderr)

    daily_gwh = {
        d: {"low": round(v["low"] / 1000.0, 1), "fossil": round(v["fossil"] / 1000.0, 1)}
        for d, v in daily_mwh.items()
    }
    return daily_gwh


def load_existing():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def write_output(days_by_date):
    dates_sorted = sorted(days_by_date.keys())
    through_date = dates_sorted[-1] if dates_sorted else None
    payload = {
        "generated_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "EIA Hourly Electric Grid Monitor (Form EIA-930)",
        "excluded_fuel_codes": EXCLUDED_FUEL_CODES,
        "through_date": through_date,
        "days": [
            {"date": d, "low": days_by_date[d]["low"], "fossil": days_by_date[d]["fossil"]}
            for d in dates_sorted
        ],
    }
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return payload


def print_sanity_report(days_by_date, label):
    dates_sorted = sorted(days_by_date.keys())
    print(f"\n=== {label} sanity report ===")
    print(f"Days: {len(dates_sorted)}")
    if dates_sorted:
        print(f"Range: {dates_sorted[0]} .. {dates_sorted[-1]}")
        clamp_exceeded = 0
        sample = dates_sorted[:3] + dates_sorted[-3:]
        for d in sorted(set(sample)):
            v = days_by_date[d]
            margin = round(v["low"] - v["fossil"], 1)
            flag = " *** exceeds +/-350 clamp ***" if abs(margin) > 350 else ""
            print(f"  {d}: low={v['low']:>8.1f} GWh  fossil={v['fossil']:>8.1f} GWh  margin={margin:>8.1f}{flag}")
        for d in dates_sorted:
            if abs(days_by_date[d]["low"] - days_by_date[d]["fossil"]) > 350:
                clamp_exceeded += 1
        if clamp_exceeded:
            print(f"  NOTE: {clamp_exceeded} day(s) exceed the +/-350 GWh clamp specified in the brief.")
    print(f"Excluded fuel codes: {EXCLUDED_FUEL_CODES}")
    print("=" * 40)


def run_backfill():
    key = api_key()
    start = date(2021, 1, 1)
    yesterday_pacific = (datetime.now(CAISO_TZ) - timedelta(days=1)).date()
    print(f"Backfilling {start.isoformat()} .. {yesterday_pacific.isoformat()} (CAISO local dates)")
    rows = fetch_range_rows(start, yesterday_pacific, key)
    print(f"Total raw rows fetched: {len(rows)}")
    days_by_date = aggregate_daily(rows)
    payload = write_output(days_by_date)
    print_sanity_report(days_by_date, "Backfill")
    missing = []
    d = start
    dates_present = set(days_by_date.keys())
    while d <= yesterday_pacific:
        if d.isoformat() not in dates_present:
            missing.append(d.isoformat())
        d += timedelta(days=1)
    if missing:
        print(f"\nWARNING: {len(missing)} date(s) with zero data, left out of the file (not fabricated):")
        print(f"  {missing[:10]}{' ...' if len(missing) > 10 else ''}")
    print(f"\nWrote {DATA_PATH}")


def run_refresh():
    key = api_key()
    existing = load_existing()
    if existing is None:
        print("ERROR: no existing data/caiso-daily.json to refresh. Run --backfill first.", file=sys.stderr)
        sys.exit(1)

    today_pacific = datetime.now(CAISO_TZ).date()
    start = today_pacific - timedelta(days=45)
    end = today_pacific  # include partial current day
    print(f"Refreshing trailing window {start.isoformat()} .. {end.isoformat()} (CAISO local dates)")

    rows = fetch_range_rows(start, end, key)
    print(f"Total raw rows fetched: {len(rows)}")
    refreshed_days = aggregate_daily(rows)

    merged = {d["date"]: {"low": d["low"], "fossil": d["fossil"]} for d in existing["days"]}
    overwritten = sum(1 for d in refreshed_days if d in merged)
    added = sum(1 for d in refreshed_days if d not in merged)
    merged.update(refreshed_days)

    payload = write_output(merged)
    print_sanity_report(refreshed_days, "Refresh window")
    print(f"\nOverwrote {overwritten} existing date(s), added {added} new date(s).")
    print(f"Wrote {DATA_PATH}")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backfill", action="store_true")
    group.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if args.backfill:
        run_backfill()
    else:
        run_refresh()


if __name__ == "__main__":
    main()
