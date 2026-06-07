"""Backfill CFFEX ccpm positions from 2024-05-20 to today.

CFFEXAdapter internally throttles 1.2s/product × 8 products = ~10s/day.
Total: 496 days × ~10s ≈ 80-90 minutes.
"""
import json
import time
from datetime import date
from futures_positions.database import PositionsDatabase
from futures_positions.adapters import CFFEXAdapter
from futures_positions.system import update_incremental

db = PositionsDatabase("data/shfe_positions.sqlite")
t0 = time.time()
r = update_incremental(
    db,
    date(2024, 5, 20),
    date(2026, 6, 4),
    pause_seconds=1.0,           # extra pause between days
    adapters=[CFFEXAdapter()],   # CFFEX only; SHFE already complete
)
elapsed = time.time() - t0
print(f"elapsed: {elapsed:.1f}s")
print(json.dumps({"summary": r, "elapsed_sec": round(elapsed,1)}, ensure_ascii=False, indent=2))
