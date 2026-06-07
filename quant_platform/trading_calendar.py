from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any


@dataclass(frozen=True)
class TradingCalendar:
    holidays: set[date] = field(default_factory=set)
    weekend_days: set[int] = field(default_factory=lambda: {5, 6})

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "TradingCalendar":
        if not raw:
            return cls()
        holidays = {
            datetime.strptime(item, "%Y-%m-%d").date()
            for item in raw.get("holidays", [])
        }
        weekend_days = set(raw.get("weekend_days", [5, 6]))
        return cls(holidays=holidays, weekend_days=weekend_days)

    def is_trading_day(self, value: date | datetime) -> bool:
        current = value.date() if isinstance(value, datetime) else value
        return current.weekday() not in self.weekend_days and current not in self.holidays

    def next_trading_day(self, value: date | datetime) -> date:
        current = value.date() if isinstance(value, datetime) else value
        current += timedelta(days=1)
        while not self.is_trading_day(current):
            current += timedelta(days=1)
        return current

    def previous_trading_day(self, value: date | datetime) -> date:
        current = value.date() if isinstance(value, datetime) else value
        current -= timedelta(days=1)
        while not self.is_trading_day(current):
            current -= timedelta(days=1)
        return current


@dataclass(frozen=True)
class SessionInterval:
    start: time
    end: time

    @classmethod
    def parse(cls, value: str) -> "SessionInterval":
        start_raw, end_raw = value.split("-", 1)
        return cls(start=_parse_time(start_raw), end=_parse_time(end_raw))

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def contains(self, value: time) -> bool:
        if self.crosses_midnight:
            return value >= self.start or value < self.end
        return self.start <= value < self.end


@dataclass(frozen=True)
class SessionTemplate:
    name: str
    intervals: tuple[SessionInterval, ...]
    night_session_start: time | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "SessionTemplate":
        if not raw:
            return session_template("day")
        if "name" in raw and "intervals" not in raw:
            return session_template(str(raw["name"]))
        intervals = tuple(SessionInterval.parse(item) for item in raw.get("intervals", []))
        night_start = raw.get("night_session_start")
        return cls(
            name=str(raw.get("name", "custom")),
            intervals=intervals,
            night_session_start=_parse_time(night_start) if night_start else None,
        )

    def contains(self, value: datetime) -> bool:
        return any(interval.contains(value.time()) for interval in self.intervals)

    def interval_index(self, value: datetime) -> int | None:
        for index, interval in enumerate(self.intervals):
            if interval.contains(value.time()):
                return index
        return None

    def trading_date(self, value: datetime, calendar: TradingCalendar | None = None) -> date:
        calendar = calendar or TradingCalendar()
        current = value.date()
        if self.night_session_start and value.time() >= self.night_session_start:
            return calendar.next_trading_day(current)
        return current


def session_template(name: str) -> SessionTemplate:
    normalized = name.lower()
    if normalized in {"day", "cn_futures_day"}:
        return SessionTemplate(
            name="day",
            intervals=(
                SessionInterval.parse("09:00-10:15"),
                SessionInterval.parse("10:30-11:30"),
                SessionInterval.parse("13:30-15:00"),
            ),
        )
    if normalized in {"cn_futures", "cn_futures_day_night", "futures"}:
        return SessionTemplate(
            name="cn_futures_day_night",
            intervals=(
                SessionInterval.parse("21:00-23:00"),
                SessionInterval.parse("09:00-10:15"),
                SessionInterval.parse("10:30-11:30"),
                SessionInterval.parse("13:30-15:00"),
            ),
            night_session_start=time(21, 0),
        )
    if normalized == "always":
        return SessionTemplate(
            name="always",
            intervals=(SessionInterval.parse("00:00-00:00"),),
        )
    raise ValueError(f"unknown session template: {name}")


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()
