from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class ExchangeData:
    exchange: str
    normalized: pd.DataFrame
    raw_tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    source_urls: list[str] = field(default_factory=list)


@dataclass
class CollectResult:
    normalized: pd.DataFrame
    raw_tables: dict[str, pd.DataFrame]
    successes: list[str]
    errors: list[dict[str, str]]
