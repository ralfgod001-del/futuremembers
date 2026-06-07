from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import monotonic, sleep
from typing import Callable

from .ctp import (
    CtpLifecycleEvent,
    CtpMarketDataSession,
    CtpTradingSession,
    event_to_dict,
)


@dataclass
class CtpSessionWatchdog:
    trading_session: CtpTradingSession
    market_data_session: CtpMarketDataSession
    check_interval: float = 5.0
    initial_backoff: float = 1.0
    max_backoff: float = 30.0
    backoff_multiplier: float = 2.0
    max_recovery_attempts: int = 3
    clock: Callable[[], float] = monotonic
    sleeper: Callable[[float], None] = sleep

    def __post_init__(self) -> None:
        self.check_interval = max(float(self.check_interval), 0.0)
        self.initial_backoff = max(float(self.initial_backoff), 0.0)
        self.max_backoff = max(float(self.max_backoff), self.initial_backoff)
        self.backoff_multiplier = max(float(self.backoff_multiplier), 1.0)
        self.max_recovery_attempts = max(int(self.max_recovery_attempts), 1)
        self.events: list[CtpLifecycleEvent] = []
        self._next_check_at = 0.0
        self._trading_attempts = 0
        self._trading_next_recovery_at = 0.0
        self._market_data_attempts = 0
        self._market_data_next_recovery_at = 0.0

    def check(self, now: float | None = None, force: bool = False) -> list[CtpLifecycleEvent]:
        current = self.clock() if now is None else float(now)
        if not force and current < self._next_check_at:
            return []
        self._next_check_at = current + self.check_interval

        start = len(self.events)
        self._check_trading(current)
        self._check_market_data(current)
        return self.events[start:]

    def run(self, cycles: int | None = None) -> None:
        count = 0
        while cycles is None or count < cycles:
            self.check(force=True)
            count += 1
            if cycles is None or count < cycles:
                self.sleeper(self.check_interval)

    def snapshot(self) -> dict[str, object]:
        return {
            "check_interval": self.check_interval,
            "initial_backoff": self.initial_backoff,
            "max_backoff": self.max_backoff,
            "backoff_multiplier": self.backoff_multiplier,
            "max_recovery_attempts": self.max_recovery_attempts,
            "next_check_at": self._next_check_at or None,
            "trading": self._trading_payload(),
            "market_data": self._market_data_payload(),
            "events": [event_to_dict(event) for event in self.events],
        }

    def _check_trading(self, now: float) -> None:
        if self._trading_healthy():
            self._trading_attempts = 0
            self._trading_next_recovery_at = 0.0
            self._record(
                "WATCHDOG_TRADING_HEALTHY",
                True,
                "trading session healthy",
                self._trading_payload(),
            )
            return

        if not self.trading_session.front_connected:
            self._record(
                "WATCHDOG_TRADING_WAITING_FOR_FRONT",
                False,
                "trading front is not connected",
                self._trading_payload(),
            )
            return

        self._recover_trading(now)

    def _check_market_data(self, now: float) -> None:
        if self._market_data_healthy():
            self._market_data_attempts = 0
            self._market_data_next_recovery_at = 0.0
            self._record(
                "WATCHDOG_MARKET_DATA_HEALTHY",
                True,
                "market data session healthy",
                self._market_data_payload(),
            )
            return

        if not self.market_data_session.front_connected:
            self._record(
                "WATCHDOG_MARKET_DATA_WAITING_FOR_FRONT",
                False,
                "market data front is not connected",
                self._market_data_payload(),
            )
            return

        self._recover_market_data(now)

    def _recover_trading(self, now: float) -> None:
        if self._trading_attempts >= self.max_recovery_attempts:
            self._record(
                "WATCHDOG_TRADING_GIVE_UP",
                False,
                "trading recovery attempt limit reached",
                self._trading_payload(),
            )
            return
        if now < self._trading_next_recovery_at:
            payload = self._trading_payload()
            payload["seconds_until_retry"] = self._trading_next_recovery_at - now
            self._record(
                "WATCHDOG_TRADING_BACKOFF",
                False,
                "trading recovery is backing off",
                payload,
            )
            return

        self._trading_attempts += 1
        self._record(
            "WATCHDOG_TRADING_RECOVER_START",
            True,
            "watchdog recovering trading session",
            self._trading_payload(),
        )
        ok = self.trading_session.recover_after_front_connected()
        if ok and self._trading_healthy():
            self._trading_attempts = 0
            self._trading_next_recovery_at = 0.0
            self._record(
                "WATCHDOG_TRADING_RECOVER_READY",
                True,
                "trading session recovered by watchdog",
                self._trading_payload(),
            )
            return
        self._schedule_trading_retry(now)

    def _recover_market_data(self, now: float) -> None:
        if self._market_data_attempts >= self.max_recovery_attempts:
            self._record(
                "WATCHDOG_MARKET_DATA_GIVE_UP",
                False,
                "market data recovery attempt limit reached",
                self._market_data_payload(),
            )
            return
        if now < self._market_data_next_recovery_at:
            payload = self._market_data_payload()
            payload["seconds_until_retry"] = self._market_data_next_recovery_at - now
            self._record(
                "WATCHDOG_MARKET_DATA_BACKOFF",
                False,
                "market data recovery is backing off",
                payload,
            )
            return

        self._market_data_attempts += 1
        self._record(
            "WATCHDOG_MARKET_DATA_RECOVER_START",
            True,
            "watchdog recovering market data session",
            self._market_data_payload(),
        )
        ok = self.market_data_session.recover_after_front_connected()
        if ok and self._market_data_healthy():
            self._market_data_attempts = 0
            self._market_data_next_recovery_at = 0.0
            self._record(
                "WATCHDOG_MARKET_DATA_RECOVER_READY",
                True,
                "market data session recovered by watchdog",
                self._market_data_payload(),
            )
            return
        self._schedule_market_data_retry(now)

    def _schedule_trading_retry(self, now: float) -> None:
        if self._trading_attempts >= self.max_recovery_attempts:
            self._record(
                "WATCHDOG_TRADING_GIVE_UP",
                False,
                "trading recovery attempt limit reached",
                self._trading_payload(),
            )
            return
        delay = self._delay_for_attempt(self._trading_attempts)
        self._trading_next_recovery_at = now + delay
        payload = self._trading_payload()
        payload["retry_delay_seconds"] = delay
        self._record(
            "WATCHDOG_TRADING_RETRY_SCHEDULED",
            False,
            "trading recovery will retry after backoff",
            payload,
        )

    def _schedule_market_data_retry(self, now: float) -> None:
        if self._market_data_attempts >= self.max_recovery_attempts:
            self._record(
                "WATCHDOG_MARKET_DATA_GIVE_UP",
                False,
                "market data recovery attempt limit reached",
                self._market_data_payload(),
            )
            return
        delay = self._delay_for_attempt(self._market_data_attempts)
        self._market_data_next_recovery_at = now + delay
        payload = self._market_data_payload()
        payload["retry_delay_seconds"] = delay
        self._record(
            "WATCHDOG_MARKET_DATA_RETRY_SCHEDULED",
            False,
            "market data recovery will retry after backoff",
            payload,
        )

    def _trading_healthy(self) -> bool:
        session = self.trading_session
        if not session.connected or not session.front_connected or not session.logged_in:
            return False
        options = session.recovery_options
        if options.get("authenticate", False) and not session.authenticated:
            return False
        if options.get("confirm_settlement", False) and not session.settlement_confirmed:
            return False
        if options.get("query_account", False) and session.gateway.account is None:
            return False
        return True

    def _market_data_healthy(self) -> bool:
        session = self.market_data_session
        return bool(session.connected and session.front_connected and session.logged_in)

    def _delay_for_attempt(self, attempts: int) -> float:
        delay = self.initial_backoff * (self.backoff_multiplier ** max(attempts - 1, 0))
        return min(delay, self.max_backoff)

    def _trading_payload(self) -> dict[str, object]:
        session = self.trading_session
        return {
            "healthy": self._trading_healthy(),
            "state": session.state,
            "connected": session.connected,
            "front_connected": session.front_connected,
            "authenticated": session.authenticated,
            "logged_in": session.logged_in,
            "settlement_confirmed": session.settlement_confirmed,
            "last_disconnect_reason": session.last_disconnect_reason,
            "attempts": self._trading_attempts,
            "next_recovery_at": self._trading_next_recovery_at or None,
        }

    def _market_data_payload(self) -> dict[str, object]:
        session = self.market_data_session
        return {
            "healthy": self._market_data_healthy(),
            "state": session.state,
            "connected": session.connected,
            "front_connected": session.front_connected,
            "logged_in": session.logged_in,
            "last_disconnect_reason": session.last_disconnect_reason,
            "subscribed_symbols": sorted(session.subscribed_symbols),
            "attempts": self._market_data_attempts,
            "next_recovery_at": self._market_data_next_recovery_at or None,
        }

    def _record(
        self,
        event_type: str,
        ok: bool,
        message: str,
        payload: dict[str, object],
    ) -> None:
        self.events.append(
            CtpLifecycleEvent(
                timestamp=datetime.now(),
                event_type=event_type,
                ok=ok,
                request_id=0,
                message=message,
                payload=payload,
            )
        )
