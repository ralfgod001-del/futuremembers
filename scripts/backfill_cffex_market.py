"""Backfill CFFEX rtj daily-market from 2024-05-20 to today."""
import json
import time
from datetime import date
from futures_positions.database import PositionsDatabase
from futures_positions.system import update_market_incremental

db = PositionsDatabase("data/shfe_positions.sqlite")
t0 = time.time()
r = update_market_incremental(
    db,
    date(2024, 5, 20),
    date(2026, 6, 4),
    pause_seconds=1.5,
    include_cffex=True,
)
elapsed = time.time() - t0
print(f"elapsed: {elapsed:.1f}s")
print(json.dumps({"summary": r, "elapsed_sec": round(elapsed,1)}, ensure_ascii=False, indent=2))
