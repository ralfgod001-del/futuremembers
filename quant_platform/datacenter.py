from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .data import bars_to_frame, load_bars_csv, write_bars_csv
from .models import Bar
from .trading_calendar import SessionTemplate, TradingCalendar, session_template


@dataclass
class DataIssue:
    severity: str
    issue_type: str
    message: str
    symbol: str | None = None
    timestamp: str | None = None
    count: int = 1


@dataclass
class DataQualityReport:
    issues: list[DataIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)

    def to_frame(self) -> pd.DataFrame:
        columns = ["severity", "issue_type", "symbol", "timestamp", "count", "message"]
        return pd.DataFrame(
            [
                {
                    "severity": issue.severity,
                    "issue_type": issue.issue_type,
                    "symbol": issue.symbol,
                    "timestamp": issue.timestamp,
                    "count": issue.count,
                    "message": issue.message,
                }
                for issue in self.issues
            ],
            columns=columns,
        )

    def export(self, path: str | Path) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.to_frame().to_csv(output_path, index=False)
        return output_path


def load_data_center_config(raw: dict[str, Any] | None) -> tuple[TradingCalendar, SessionTemplate]:
    raw = raw or {}
    calendar = TradingCalendar.from_mapping(raw.get("calendar"))
    sessions = SessionTemplate.from_mapping(raw.get("sessions"))
    return calendar, sessions


def enrich_with_trading_date(
    bars: list[Bar],
    calendar: TradingCalendar | None = None,
    sessions: SessionTemplate | None = None,
) -> pd.DataFrame:
    calendar = calendar or TradingCalendar()
    sessions = sessions or session_template("always")
    frame = bars_to_frame(bars)
    if frame.empty:
        return frame
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["trading_date"] = frame["timestamp"].apply(
        lambda value: sessions.trading_date(value.to_pydatetime(), calendar).isoformat()
    )
    frame["in_session"] = frame["timestamp"].apply(
        lambda value: sessions.contains(value.to_pydatetime())
    )
    return frame


def resample_bars(
    bars: list[Bar],
    frequency: str,
    calendar: TradingCalendar | None = None,
    sessions: SessionTemplate | None = None,
) -> list[Bar]:
    frame = enrich_with_trading_date(bars, calendar, sessions)
    if frame.empty:
        return []
    frequency = frequency.lower()
    if frequency in {"1d", "d", "daily"}:
        return _resample_daily(frame)
    return _resample_intraday(frame, frequency)


def validate_bars(
    bars: list[Bar],
    expected_frequency: str | None = None,
    calendar: TradingCalendar | None = None,
    sessions: SessionTemplate | None = None,
) -> DataQualityReport:
    report = DataQualityReport()
    frame = enrich_with_trading_date(bars, calendar, sessions)
    if frame.empty:
        report.issues.append(
            DataIssue("ERROR", "empty_dataset", "data set contains no bars", count=0)
        )
        return report

    duplicates = frame.duplicated(subset=["symbol", "timestamp"], keep=False)
    if duplicates.any():
        duplicated_rows = frame.loc[duplicates, ["symbol", "timestamp"]]
        for (symbol, timestamp), group in duplicated_rows.groupby(["symbol", "timestamp"]):
            report.issues.append(
                DataIssue(
                    "ERROR",
                    "duplicate_bar",
                    f"duplicate bar for {symbol} at {timestamp}",
                    symbol=str(symbol),
                    timestamp=str(timestamp),
                    count=len(group),
                )
            )

    invalid_ohlc = frame[
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    ]
    for row in invalid_ohlc.to_dict("records"):
        report.issues.append(
            DataIssue(
                "ERROR",
                "invalid_ohlc",
                "OHLC values are inconsistent",
                symbol=str(row["symbol"]),
                timestamp=str(row["timestamp"]),
            )
        )

    non_positive = frame[
        (frame["open"] <= 0)
        | (frame["high"] <= 0)
        | (frame["low"] <= 0)
        | (frame["close"] <= 0)
    ]
    for row in non_positive.to_dict("records"):
        report.issues.append(
            DataIssue(
                "ERROR",
                "non_positive_price",
                "price columns must be positive",
                symbol=str(row["symbol"]),
                timestamp=str(row["timestamp"]),
            )
        )

    out_of_session = frame[~frame["in_session"]]
    if sessions and sessions.name != "always" and not out_of_session.empty:
        for row in out_of_session.head(20).to_dict("records"):
            report.issues.append(
                DataIssue(
                    "WARN",
                    "out_of_session",
                    "bar timestamp is outside configured sessions",
                    symbol=str(row["symbol"]),
                    timestamp=str(row["timestamp"]),
                )
            )

    for symbol, group in frame.sort_values("timestamp").groupby("symbol"):
        if not group["timestamp"].is_monotonic_increasing:
            report.issues.append(
                DataIssue(
                    "ERROR",
                    "non_monotonic_time",
                    "timestamps must be sorted ascending per symbol",
                    symbol=str(symbol),
                )
            )
        if expected_frequency:
            _add_missing_bar_issues(
                report,
                str(symbol),
                group,
                expected_frequency,
                calendar,
                sessions,
            )

    return report


def check_data_file(
    input_path: str | Path,
    output_path: str | Path,
    symbol: str | None = None,
    expected_frequency: str | None = None,
    calendar: TradingCalendar | None = None,
    sessions: SessionTemplate | None = None,
) -> DataQualityReport:
    bars = load_bars_csv(input_path, symbol=symbol)
    report = validate_bars(bars, expected_frequency, calendar, sessions)
    report.export(output_path)
    return report


def resample_file(
    input_path: str | Path,
    output_path: str | Path,
    frequency: str,
    symbol: str | None = None,
    calendar: TradingCalendar | None = None,
    sessions: SessionTemplate | None = None,
) -> list[Bar]:
    bars = load_bars_csv(input_path, symbol=symbol)
    result = resample_bars(bars, frequency, calendar, sessions)
    write_bars_csv(result, output_path)
    return result


def _resample_intraday(frame: pd.DataFrame, frequency: str) -> list[Bar]:
    rows: list[pd.DataFrame] = []
    for symbol, group in frame.sort_values("timestamp").groupby("symbol"):
        indexed = group.set_index("timestamp")
        aggregated = indexed.resample(frequency, label="right", closed="right").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "trading_date": "last",
            }
        )
        aggregated = aggregated.dropna(subset=["open", "high", "low", "close"])
        aggregated["symbol"] = symbol
        rows.append(aggregated.reset_index())
    if not rows:
        return []
    return _frame_to_bars(pd.concat(rows, ignore_index=True))


def _resample_daily(frame: pd.DataFrame) -> list[Bar]:
    rows: list[dict[str, Any]] = []
    ordered = frame.sort_values(["symbol", "trading_date", "timestamp"])
    for (symbol, trading_date), group in ordered.groupby(["symbol", "trading_date"]):
        rows.append(
            {
                "timestamp": pd.Timestamp(trading_date).to_pydatetime(),
                "symbol": symbol,
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": float(group["volume"].sum()),
            }
        )
    return _frame_to_bars(pd.DataFrame(rows))


def _frame_to_bars(frame: pd.DataFrame) -> list[Bar]:
    bars: list[Bar] = []
    for row in frame.sort_values(["timestamp", "symbol"]).to_dict("records"):
        bars.append(
            Bar(
                symbol=str(row["symbol"]),
                timestamp=pd.Timestamp(row["timestamp"]).to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0) or 0.0),
            )
        )
    return bars


def _add_missing_bar_issues(
    report: DataQualityReport,
    symbol: str,
    group: pd.DataFrame,
    expected_frequency: str,
    calendar: TradingCalendar | None,
    sessions: SessionTemplate | None,
) -> None:
    if len(group) < 2:
        return
    expected_delta = pd.Timedelta(expected_frequency)
    if expected_delta <= pd.Timedelta(0):
        return
    timestamps = group["timestamp"].sort_values().reset_index(drop=True)
    gaps = timestamps.diff().dropna()
    missing = gaps[gaps > expected_delta]
    for index, gap in missing.items():
        previous = timestamps.iloc[index - 1].to_pydatetime()
        current = timestamps.iloc[index].to_pydatetime()
        if not _gap_should_be_checked(previous, current, calendar, sessions):
            continue
        missing_count = max(int(gap / expected_delta) - 1, 1)
        report.issues.append(
            DataIssue(
                "WARN",
                "missing_bar_gap",
                f"gap of {gap} implies about {missing_count} missing bars",
                symbol=symbol,
                timestamp=str(timestamps.iloc[index]),
                count=missing_count,
            )
        )


def _gap_should_be_checked(
    previous: Any,
    current: Any,
    calendar: TradingCalendar | None,
    sessions: SessionTemplate | None,
) -> bool:
    if not sessions or sessions.name == "always":
        return True
    calendar = calendar or TradingCalendar()
    if sessions.trading_date(previous, calendar) != sessions.trading_date(current, calendar):
        return False
    return sessions.interval_index(previous) == sessions.interval_index(current)
