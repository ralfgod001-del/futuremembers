"""Backfill CZCE daily-market (settle/OI/volume + synthesized margin).

CZCE publishes a single FutureDataDaily.xlsx per trading day, so each day is
one HTTP request + >=1s pause. ~497 trading days x ~2s ~ 15-20 min.
Official source only (http+xlsx); no akshare.
"""
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
    date(2026, 6, 7),
    pause_seconds=1.0,
    include_cffex=False,   # CFFEX already complete
    include_czce=True,
)
elapsed = time.time() - t0
print(f"elapsed: {elapsed:.1f}s")
print(json.dumps({"summary": r, "elapsed_sec": round(elapsed, 1)}, ensure_ascii=False, indent=2))
