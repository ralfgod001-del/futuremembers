from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .models import Bar
from .trading_calendar import TradingCalendar, session_template


TIME_COLUMNS = ("timestamp", "datetime", "date", "trade_date")
REQUIRED_PRICE_COLUMNS = ("open", "high", "low", "close")
AKSHARE_TIME_COLUMNS = ("timestamp", "datetime", "date", "trade_date", "日期", "时间")
AKSHARE_COLUMN_ALIASES = {
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价"),
    "volume": ("volume", "成交量"),
    "symbol": ("symbol", "合约", "代码"),
    "open_interest": ("open_interest", "hold", "持仓量", "持仓"),
    "settle": ("settle", "动态结算价", "结算价"),
    "turnover": ("turnover", "成交额"),
    "pre_settle": ("pre_settle", "前结算价"),
    "variety": ("variety", "品种"),
}


def load_bars_csv(path: str | Path, symbol: str | None = None) -> list[Bar]:
    csv_path = Path(path)
    frame = pd.read_csv(csv_path)
    frame.columns = [column.strip().lower() for column in frame.columns]

    time_column = next((column for column in TIME_COLUMNS if column in frame.columns), None)
    if not time_column:
        raise ValueError(
            f"{csv_path} must contain one of these time columns: {', '.join(TIME_COLUMNS)}"
        )

    missing = [column for column in REQUIRED_PRICE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{csv_path} is missing price columns: {', '.join(missing)}")

    if "symbol" not in frame.columns and not symbol:
        raise ValueError(f"{csv_path} must contain a symbol column or receive --symbol")

    frame[time_column] = pd.to_datetime(frame[time_column])
    if "volume" not in frame.columns:
        frame["volume"] = 0.0

    bars: list[Bar] = []
    for record in frame.sort_values(time_column).to_dict("records"):
        bar_symbol = str(record.get("symbol") or symbol)
        bars.append(
            Bar(
                symbol=bar_symbol,
                timestamp=record[time_column].to_pydatetime(),
                open=float(record["open"]),
                high=float(record["high"]),
                low=float(record["low"]),
                close=float(record["close"]),
                volume=float(record.get("volume") or 0.0),
                extra={
                    key: value
                    for key, value in record.items()
                    if key
                    not in {
                        "symbol",
                        time_column,
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    }
                },
            )
        )
    return bars


def bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": bar.timestamp,
                "symbol": bar.symbol,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]
    )


def write_bars_csv(bars: list[Bar], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bars_to_frame(bars).to_csv(output_path, index=False)
    return output_path


def load_akshare_futures_bars(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    api: str = "futures_zh_daily_sina",
    market: str | None = None,
    variety: str | None = None,
    period: str = "daily",
    output_symbol: str | None = None,
    ak_module: Any | None = None,
) -> list[Bar]:
    frame = fetch_akshare_futures_frame(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        api=api,
        market=market,
        period=period,
        ak_module=ak_module,
    )
    return akshare_frame_to_bars(
        frame,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        api=api,
        market=market,
        variety=variety,
        output_symbol=output_symbol,
    )


def fetch_akshare_futures_frame(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    api: str = "futures_zh_daily_sina",
    market: str | None = None,
    period: str = "daily",
    ak_module: Any | None = None,
) -> pd.DataFrame:
    ak = ak_module or _import_akshare()
    api_name = api or "futures_zh_daily_sina"
    if api_name == "futures_zh_daily_sina":
        return ak.futures_zh_daily_sina(symbol=symbol)
    if api_name == "futures_main_sina":
        return ak.futures_main_sina(
            symbol=symbol,
            start_date=_akshare_date(start_date, "19900101"),
            end_date=_akshare_date(end_date, "22220101"),
        )
    if api_name == "futures_hist_em":
        return ak.futures_hist_em(
            symbol=symbol,
            period=period,
            start_date=_akshare_date(start_date, "19900101"),
            end_date=_akshare_date(end_date, "20500101"),
        )
    if api_name == "get_futures_daily":
        if not market:
            raise ValueError("AkShare get_futures_daily requires market")
        return ak.get_futures_daily(
            start_date=_akshare_date(start_date, "19900101"),
            end_date=_akshare_date(end_date, "22220101"),
            market=market,
        )
    raise ValueError(
        "unsupported AkShare futures api: "
        f"{api_name}; expected futures_zh_daily_sina, futures_main_sina, "
        "futures_hist_em, or get_futures_daily"
    )


def akshare_frame_to_bars(
    frame: pd.DataFrame,
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    api: str = "futures_zh_daily_sina",
    market: str | None = None,
    variety: str | None = None,
    output_symbol: str | None = None,
) -> list[Bar]:
    if frame is None or frame.empty:
        return []

    normalized = frame.copy()
    column_map = {
        canonical: _first_existing_column(normalized, aliases)
        for canonical, aliases in AKSHARE_COLUMN_ALIASES.items()
    }
    time_column = _first_existing_column(normalized, AKSHARE_TIME_COLUMNS)
    if not time_column:
        raise ValueError("AkShare futures data must include a date or timestamp column")
    missing = [
        name
        for name in ("open", "high", "low", "close")
        if not column_map.get(name)
    ]
    if missing:
        raise ValueError(f"AkShare futures data is missing columns: {', '.join(missing)}")

    normalized[time_column] = pd.to_datetime(normalized[time_column])
    start = _optional_timestamp(start_date)
    end = _optional_timestamp(end_date)
    if start is not None:
        normalized = normalized[normalized[time_column] >= start]
    if end is not None:
        normalized = normalized[normalized[time_column] <= end]

    symbol_column = column_map.get("symbol")
    variety_column = column_map.get("variety")
    if symbol_column and api == "get_futures_daily" and symbol:
        normalized = normalized[normalized[symbol_column].astype(str).str.upper() == symbol.upper()]
    if variety_column and variety:
        normalized = normalized[normalized[variety_column].astype(str).str.upper() == variety.upper()]

    bars: list[Bar] = []
    for record in normalized.sort_values(time_column).to_dict("records"):
        record_symbol = (
            output_symbol
            or (str(record.get(symbol_column)) if symbol_column and record.get(symbol_column) else None)
            or symbol
        )
        extra = {
            "source": "akshare",
            "akshare_api": api,
            **({"market": market} if market else {}),
        }
        for key in ("open_interest", "settle", "turnover", "pre_settle", "variety"):
            column = column_map.get(key)
            if column and record.get(column) is not None:
                extra[key] = record.get(column)
        bars.append(
            Bar(
                symbol=str(record_symbol),
                timestamp=record[time_column].to_pydatetime(),
                open=_number(record[column_map["open"]]),
                high=_number(record[column_map["high"]]),
                low=_number(record[column_map["low"]]),
                close=_number(record[column_map["close"]]),
                volume=_number(record.get(column_map.get("volume")) if column_map.get("volume") else 0.0),
                extra=extra,
            )
        )
    return bars


def generate_sample_bars(
    symbol: str = "SAMPLE",
    start: str | datetime = "2024-01-02",
    periods: int = 240,
    seed: int = 7,
) -> list[Bar]:
    start_dt = pd.Timestamp(start).to_pydatetime() if isinstance(start, str) else start
    rng = random.Random(seed)
    bars: list[Bar] = []
    close = 100.0

    for index in range(periods):
        timestamp = start_dt + timedelta(days=index)
        drift = 0.035
        cycle = math.sin(index / 13.0) * 0.55
        shock = rng.gauss(0, 0.85)
        open_price = max(1.0, close + rng.gauss(0, 0.35))
        close = max(1.0, open_price + drift + cycle + shock)
        high = max(open_price, close) + abs(rng.gauss(0.25, 0.2))
        low = min(open_price, close) - abs(rng.gauss(0.25, 0.2))
        volume = max(1.0, 1000 + rng.gauss(0, 120))
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp,
                open=round(open_price, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=round(volume, 2),
            )
        )

    return bars


def generate_sample_market(
    symbols: list[str],
    start: str | datetime = "2024-01-02",
    periods: int = 240,
    seed: int = 7,
) -> list[Bar]:
    bars: list[Bar] = []
    for offset, symbol in enumerate(symbols):
        bars.extend(
            generate_sample_bars(
                symbol=symbol,
                start=start,
                periods=periods,
                seed=seed + offset * 101,
            )
        )
    return sorted(bars, key=lambda item: (item.timestamp, item.symbol))


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "AkShare is required for provider=akshare; install requirements.txt first"
        ) from exc
    return ak


def _first_existing_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _akshare_date(value: str | None, default: str) -> str:
    if not value:
        return default
    return str(value).replace("-", "")


def _optional_timestamp(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    return pd.Timestamp(str(value))


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return 0.0
    if pd.isna(value):
        return 0.0
    return float(value)


def generate_intraday_sample_bars(
    symbol: str = "RB2405",
    start: str | datetime = "2024-01-02",
    days: int = 3,
    session: str = "day",
    seed: int = 11,
) -> list[Bar]:
    start_date = pd.Timestamp(start).date() if isinstance(start, str) else start.date()
    sessions = session_template(session)
    calendar = TradingCalendar()
    rng = random.Random(seed)
    bars: list[Bar] = []
    close = 3600.0
    current_date = start_date

    while len({bar.extra.get("trading_date") for bar in bars}) < days:
        if not calendar.is_trading_day(current_date):
            current_date += timedelta(days=1)
            continue

        for interval in sessions.intervals:
            interval_date = current_date
            if sessions.night_session_start and interval.start >= sessions.night_session_start:
                interval_date = calendar.previous_trading_day(current_date)
            start_dt = datetime.combine(interval_date, interval.start)
            end_dt = datetime.combine(interval_date, interval.end)
            if interval.crosses_midnight:
                end_dt += timedelta(days=1)
            timestamp = start_dt
            while timestamp < end_dt:
                open_price = close
                change = rng.gauss(0.0, 1.8)
                close = max(1.0, open_price + change)
                high = max(open_price, close) + abs(rng.gauss(0.6, 0.25))
                low = min(open_price, close) - abs(rng.gauss(0.6, 0.25))
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=timestamp,
                        open=round(open_price, 2),
                        high=round(high, 2),
                        low=round(low, 2),
                        close=round(close, 2),
                        volume=round(max(1.0, 60 + rng.gauss(0, 12)), 2),
                        extra={"trading_date": current_date.isoformat()},
                    )
                )
                timestamp += timedelta(minutes=1)

        current_date = calendar.next_trading_day(current_date)

    return sorted(bars, key=lambda item: (item.timestamp, item.symbol))
