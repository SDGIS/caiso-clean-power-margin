# CAISO clean-vs-fossil daily margin

A recreation of a stripe-heatmap chart by [John Bistline](https://twitter.com/john_bistline) showing the daily margin
between low-emitting and fossil generation on the CAISO grid, one stripe per day since Jan 1, 2021. This reproduces
his chart's design for personal use; it is not the original.

## Structure

- `scripts/fetch.py` — pulls hourly CAISO generation by fuel type from the EIA-930 API (`EIA_API_KEY` required) and
  aggregates it into `data/caiso-daily.json`. `--backfill` pulls the full history; `--refresh` re-pulls and
  overwrites the trailing 45 days (EIA-930 data is preliminary and revises for about a week after publication).
- `data/caiso-daily.json` — committed daily totals (GWh), low-emitting and fossil separately, plus the classification
  used and the most recent date with data.
- `chart/index.html` — self-contained renderer. Open directly from disk (`file://`); it fetches the committed JSON
  from `raw.githubusercontent.com` on this repo's `main` branch.
- `.github/workflows/refresh.yml` — runs `fetch.py --refresh` weekly and commits the result if it changed.

## Fuel classification

Verified live against the EIA API (`/v2/electricity/rto/fuel-type-data/`) on 2026-08-26. CAISO reports exactly 9 fuel
codes in this route; no separate battery code appears for CAISO here.

| Class | Codes |
|---|---|
| Low-emitting | `NUC` (nuclear), `WND` (wind), `SUN` (solar), `WAT` (hydro), `GEO` (geothermal) |
| Fossil | `NG` (natural gas), `COL` (coal), `OIL` (petroleum) |
| Excluded | `OTH` — runs net-negative for long stretches (looks like unclassified storage charge/discharge); not confidently classifiable |

Geothermal (`GEO`) is included in low-emitting: it is its own distinct EIA fuel code, not folded into `OTH`, so it's
cleanly separable and matches the original chart's subtitle.

## Timezone convention

Day boundaries are CAISO local time (`America/Los_Angeles`), using the API's `local-hourly` frequency, which returns
already-local-time-stamped periods (`YYYY-MM-DDTHH±TZH`). The local calendar date is taken directly from the period
string. Request windows are padded by a day on each side and results are filtered client-side on the row's own local
date, so any DST-boundary imprecision in the request bounds is self-correcting.

## Known deviations from the original chart

- Geothermal included in low-emitting (see above) — the original's stated exclusion reason didn't hold up against
  the live API.
- Color clamp is fixed at ±350 GWh, matching the original. 38 days in 2026 (all on the clean-winning side, up to
  +423.6 GWh) exceed it and render at the saturated green endpoint rather than a distinct shade — a deliberate
  choice to keep the scale and legend identical to the original.
- Win-share percentages computed here run within 1–3 points of the original's stated 2021–2025 values (exact match
  for 2025 at 74%), consistent with the geothermal-inclusion difference.

## Setup

1. Get an EIA API key: https://www.eia.gov/opendata/register.php
2. Set it as the `EIA_API_KEY` secret on this repo (Settings → Secrets and variables → Actions).
3. Run `python scripts/fetch.py --backfill` locally once, commit `data/caiso-daily.json`.
4. The weekly workflow takes over from there. Trigger it manually via workflow_dispatch to test.
