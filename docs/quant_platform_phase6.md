# Quant Platform Phase 6

Phase 6 adds the first data-center layer for futures research.

## New Capabilities

- Trading calendar with weekend and holiday support.
- Session templates:
  - `always`
  - `day`
  - `cn_futures`
- Night-session trading-date mapping.
- Intraday sample data generation.
- OHLCV data quality checks.
- K-line resampling, including minute-to-minute and intraday-to-daily bars.

## Generate Intraday Data

```powershell
python -m quant_platform generate-intraday-sample --symbol RB2405 --session cn_futures --days 3 --output data/futures_rb2405_1m.csv
```

## Check Data Quality

```powershell
python -m quant_platform data-check --input data/futures_rb2405_1m.csv --session cn_futures --expected-frequency 1min --output output/data_quality/futures_rb2405_1m.csv
```

## Resample K Lines

```powershell
python -m quant_platform data-resample --input data/futures_rb2405_1m.csv --frequency 5min --session cn_futures --output data/futures_rb2405_5m.csv
```

```powershell
python -m quant_platform data-resample --input data/futures_rb2405_1m.csv --frequency 1d --session cn_futures --output data/futures_rb2405_1d_from_1m.csv
```

## Quality Checks

The current report detects:

- Empty datasets.
- Duplicate symbol/timestamp bars.
- Invalid OHLC relationships.
- Non-positive prices.
- Out-of-session bars.
- Gaps relative to an expected frequency inside the same continuous session.

The next hardening step is exchange-specific calendars with official holiday
files and product-specific night-session templates.
