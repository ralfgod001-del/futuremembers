"""Backfill CZCE positions (FutureDataHolding.xlsx) from 2024-05-20.

CZCE publishes a single holding xlsx per trading day (one request/day, unlike
CFFEX's 8 per-product files), so this is fast: ~497 days x ~2s ~ 15-20 min.
Official source only; no akshare.
"""
import json
import time
from datetime import date
from futures_positions.database import PositionsDatabase
from futures_positions.adapters import CZCEAdapter
from futures_positions.system import update_incremental

db = PositionsDatabase("data/shfe_positions.sqlite")
t0 = time.time()
r = update_incremental(
    db,
    date(2024, 5, 20),
    date(2026, 6, 7),
    pause_seconds=1.0,
    adapters=[CZCEAdapter()],  # CZCE only
)
elapsed = time.time() - t0
print(f"elapsed: {elapsed:.1f}s")
print(json.dumps({"summary": r, "elapsed_sec": round(elapsed, 1)}, ensure_ascii=False, indent=2))
