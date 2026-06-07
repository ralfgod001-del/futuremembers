from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .data import load_akshare_futures_bars, load_bars_csv, write_bars_csv
from .ctp import CtpConnectionConfig
from .execution import ExecutionConfig
from .futures import ContractRegistry
from .models import Bar
from .risk import RiskManager


def load_json_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    return json.loads(config_path.read_text(encoding="utf-8"))


def normalize_data_sources(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [{"path": raw}]
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        sources: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                sources.append({"path": item})
            elif isinstance(item, dict):
                sources.append(item)
            else:
                raise TypeError("data entries must be file paths or objects")
        return sources
    raise TypeError("data must be a file path, object, or list")


def load_bars_from_sources(sources: list[dict[str, Any]]) -> list[Bar]:
    bars: list[Bar] = []
    for source in sources:
        provider = str(
            source.get("provider")
            or source.get("type")
            or ("csv" if source.get("path") else "")
        ).lower()
        if provider in {"akshare", "ak"}:
            bars.extend(_load_akshare_source(source))
            continue
        path = source.get("path")
        if not path:
            raise ValueError("each CSV data source must include path")
        bars.extend(load_bars_csv(path, symbol=source.get("symbol")))
    return bars


def _load_akshare_source(source: dict[str, Any]) -> list[Bar]:
    symbol = source.get("symbol")
    if not symbol:
        raise ValueError("AkShare data source requires symbol")
    cache_path = source.get("cache_path") or source.get("cache")
    refresh = bool(source.get("refresh", False))
    if cache_path and Path(cache_path).exists() and not refresh:
        return load_bars_csv(cache_path, symbol=source.get("output_symbol") or symbol)

    bars = load_akshare_futures_bars(
        symbol=str(symbol),
        start_date=source.get("start_date"),
        end_date=source.get("end_date"),
        api=str(source.get("api", source.get("function", "futures_zh_daily_sina"))),
        market=source.get("market"),
        variety=source.get("variety"),
        period=str(source.get("period", "daily")),
        output_symbol=source.get("output_symbol"),
    )
    if cache_path:
        write_bars_csv(bars, cache_path)
    return bars


def execution_from_config(
    config: dict[str, Any],
    fallback_commission_rate: float = 0.0002,
    fallback_slippage: float = 0.0,
) -> ExecutionConfig:
    engine = config.get("engine", {})
    return ExecutionConfig.from_mapping(
        config.get("execution"),
        fallback_commission_rate=float(engine.get("commission_rate", fallback_commission_rate)),
        fallback_slippage=float(engine.get("slippage", fallback_slippage)),
    )


def risk_from_config(config: dict[str, Any]) -> RiskManager:
    return RiskManager.from_mapping(config.get("risk"))


def account_mode_from_config(config: dict[str, Any]) -> str:
    account = config.get("account", {})
    engine = config.get("engine", {})
    return str(account.get("mode", engine.get("account_mode", "cash")))


def daily_settlement_from_config(config: dict[str, Any]) -> bool:
    account = config.get("account", {})
    engine = config.get("engine", {})
    return bool(account.get("daily_settlement", engine.get("daily_settlement", False)))


def contract_registry_from_config(config: dict[str, Any]) -> ContractRegistry:
    return ContractRegistry.from_mapping(config.get("contracts"))


def ctp_from_config(config: dict[str, Any]) -> CtpConnectionConfig:
    return CtpConnectionConfig.from_mapping(config.get("ctp"))
