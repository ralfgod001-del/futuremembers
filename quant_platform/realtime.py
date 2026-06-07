from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .ctp import (
    CtpCallbackEvent,
    CtpMarketDataSession,
    CtpOrderInsertRequest,
    CtpOrderInstruction,
    CtpTradingSession,
    tick_to_dict,
)
from .events import EventRecorder, RunEvent
from .models import Bar, Offset, Order, OrderStatus, OrderType, Position, Side, Tick, Trade
from .risk import RiskManager
from .strategy import Strategy, StrategyContext
from .watchdog import CtpSessionWatchdog


class CtpRealtimeEngine:
    def __init__(
        self,
        strategy: Strategy,
        trading_session: CtpTradingSession,
        market_data_session: CtpMarketDataSession,
        initial_cash: float = 100_000.0,
        risk_manager: RiskManager | None = None,
        bar_frequency: str | None = None,
    ) -> None:
        self.strategy = strategy
        self.trading_session = trading_session
        self.market_data_session = market_data_session
        self.initial_cash = float(initial_cash)
        self.risk_manager = risk_manager or RiskManager()
        self.bar_frequency = bar_frequency
        self.current_time: datetime | None = None

        self._events = EventRecorder()
        self._context = StrategyContext(self)
        self._strategy_initialized = False
        self._pending_strategy_state: dict[str, Any] | None = None
        self._orders: list[Order] = []
        self._orders_by_id: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._last_ticks: dict[str, Tick] = {}
        self._histories: dict[str, list[Bar]] = defaultdict(list)
        self._bar_aggregator = TickBarAggregator(bar_frequency) if bar_frequency else None
        config = trading_session.gateway.config
        self.watchdog = CtpSessionWatchdog(
            trading_session=trading_session,
            market_data_session=market_data_session,
            check_interval=config.watchdog_check_interval,
            initial_backoff=config.watchdog_initial_backoff,
            max_backoff=config.watchdog_max_backoff,
            backoff_multiplier=config.watchdog_backoff_multiplier,
            max_recovery_attempts=config.watchdog_max_recovery_attempts,
        )
        self._ctp_order_ref_to_local_id: dict[str, str] = {}
        self._cancel_request_to_local_order_id: dict[int, str] = {}
        self._filled_quantity_by_order_id: dict[str, float] = {}
        self._fill_notional_by_order_id: dict[str, float] = {}
        self._order_seq = 0
        self.market_data_session.add_tick_handler(self.on_tick)
        self.trading_session.add_order_handler(self.on_order_return)
        self.trading_session.add_trade_handler(self.on_trade_return)
        self.trading_session.add_order_insert_error_handler(self.on_order_insert_error)
        self.trading_session.add_order_action_error_handler(self.on_order_action_error)

    @property
    def events(self) -> list[RunEvent]:
        return self._events.events

    @property
    def orders(self) -> list[Order]:
        return self._orders

    @property
    def trades(self) -> list[Trade]:
        return self._trades

    def enable_event_log(
        self,
        path: str | Path,
        include_existing: bool = True,
        max_bytes: int | None = None,
        backup_count: int = 0,
    ) -> Path:
        target = self._events.enable_jsonl(
            path,
            include_existing=include_existing,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        self._events.record(
            self.current_time,
            "EVENT_LOG_ENABLED",
            f"enabled realtime event JSONL log at {target}",
            gateway="ctp",
            path=str(target),
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        return target

    def export_event_log_csv(self, path: str | Path) -> Path:
        target = self._events.export_csv(path)
        self._events.record(
            self.current_time,
            "EVENT_LOG_CSV_EXPORTED",
            f"exported realtime event CSV log to {target}",
            gateway="ctp",
            path=str(target),
        )
        return target

    def start(
        self,
        symbols: Iterable[str] | None = None,
        start_trading: bool = True,
        start_market_data: bool = True,
        authenticate: bool | None = None,
        confirm_settlement: bool | None = None,
        query_account: bool | None = None,
        query_positions: bool | None = None,
    ) -> None:
        self._events.record(
            self.current_time,
            "RUN_START",
            f"starting CTP realtime strategy {self.strategy.name}",
            gateway="ctp",
        )
        self.strategy.on_init(self._context)
        self._strategy_initialized = True
        self._restore_pending_strategy_state()

        if start_trading:
            self.trading_session.start(
                authenticate=authenticate,
                confirm_settlement=confirm_settlement,
                query_account=query_account,
                query_positions=query_positions,
            )
        if start_market_data:
            self.market_data_session.start()
            instruments = [str(symbol) for symbol in symbols or [] if str(symbol)]
            if instruments:
                self.market_data_session.subscribe(instruments)

    def on_tick(self, tick: Tick) -> None:
        self.current_time = tick.timestamp
        self._last_ticks[tick.symbol] = tick
        payload = tick_to_dict(tick)
        payload.pop("symbol", None)
        payload["tick_timestamp"] = payload.pop("timestamp")
        self._events.record(
            tick.timestamp,
            "TICK",
            f"{tick.symbol} tick last={tick.last_price:.4f}",
            symbol=tick.symbol,
            gateway="ctp",
            **payload,
        )
        self.strategy.on_tick(self._context, tick)
        self._dispatch_completed_bars(tick)

    def submit_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        offset: Offset = Offset.AUTO,
    ) -> Order:
        if self.current_time is None:
            raise RuntimeError("orders can only be submitted after a realtime tick")

        order = self._new_order(symbol, side, quantity, order_type, limit_price, offset)
        reject_reason = self._reject_reason(order)
        if reject_reason:
            self._reject_order(order, reject_reason)
            return order

        futures_position = self.trading_session.gateway.positions.get(symbol)
        requests = self.trading_session.submit_order(order, futures_position)
        self._orders.append(order)
        self._orders_by_id[order.order_id] = order
        for request in requests:
            self._ctp_order_ref_to_local_id[request.order_ref] = order.order_id
        self._events.record(
            self.current_time,
            "ORDER_SUBMITTED",
            f"ctp submitted {side.value} {quantity:g} {symbol}",
            symbol=symbol,
            order_id=order.order_id,
            side=side.value,
            quantity=quantity,
            order_type=order_type.value,
            offset=offset.value,
            limit_price=limit_price,
            request_ids=[request.request_id for request in requests],
            order_refs=[request.order_ref for request in requests],
            gateway="ctp",
        )
        self.strategy.on_order(self._context, order)
        return order

    def cancel_order(
        self,
        order_id: str,
        front_id: int | None = None,
        session_id: int | None = None,
        order_sys_id: str = "",
        exchange_id: str | None = None,
    ) -> Order | None:
        order = self._orders_by_id.get(order_id)
        if order is None:
            self._events.record(
                self.current_time,
                "ORDER_CANCEL_REJECTED",
                f"unknown order {order_id}",
                "WARN",
                order_id=order_id,
                gateway="ctp",
            )
            return None
        if order.status != OrderStatus.PENDING:
            self._events.record(
                self.current_time,
                "ORDER_CANCEL_REJECTED",
                f"order {order_id} is not pending",
                "WARN",
                symbol=order.symbol,
                order_id=order.order_id,
                status=order.status.value,
                gateway="ctp",
            )
            return order

        request = self.trading_session.cancel_order(
            order,
            front_id=front_id,
            session_id=session_id,
            order_sys_id=order_sys_id,
            exchange_id=exchange_id,
        )
        self._cancel_request_to_local_order_id[request.request_id] = order.order_id
        self._events.record(
            self.current_time,
            "ORDER_CANCEL_SUBMITTED",
            f"ctp cancel submitted {order.order_id}",
            symbol=order.symbol,
            order_id=order.order_id,
            request_id=request.request_id,
            gateway="ctp",
        )
        return order

    def on_order_return(self, ctp_order: Order) -> None:
        local_order = self._local_order_for_ctp_order_id(ctp_order.order_id)
        if local_order is None:
            self._events.record(
                ctp_order.submitted_at,
                "ORDER_RETURN_UNMATCHED",
                f"unmatched CTP order return {ctp_order.order_id}",
                "WARN",
                symbol=ctp_order.symbol,
                order_id=ctp_order.order_id,
                status=ctp_order.status.value,
                gateway="ctp",
            )
            return

        self.current_time = ctp_order.submitted_at
        if ctp_order.status != OrderStatus.PENDING:
            local_order.status = ctp_order.status
        if ctp_order.filled_at is not None:
            local_order.filled_at = ctp_order.filled_at
        if ctp_order.fill_price is not None:
            local_order.fill_price = ctp_order.fill_price
        if ctp_order.reject_reason:
            local_order.reject_reason = ctp_order.reject_reason
        self._events.record(
            ctp_order.submitted_at,
            "ORDER_RETURN",
            f"ctp order {local_order.order_id} status={local_order.status.value}",
            symbol=local_order.symbol,
            order_id=local_order.order_id,
            ctp_order_ref=ctp_order.order_id,
            status=local_order.status.value,
            gateway="ctp",
        )
        self.strategy.on_order(self._context, local_order)

    def on_order_insert_error(self, event: CtpCallbackEvent) -> None:
        local_order = self._local_order_for_order_error(event)
        if local_order is None:
            self._events.record(
                self.current_time,
                "ORDER_INSERT_REJECTED_UNMATCHED",
                event.message,
                "WARN",
                order_id=str(event.data.get("OrderRef", "")) or None,
                request_id=event.request_id,
                gateway="ctp",
            )
            return

        local_order.status = OrderStatus.REJECTED
        local_order.reject_reason = event.message
        self._events.record(
            self.current_time,
            "ORDER_INSERT_REJECTED",
            event.message,
            "WARN",
            symbol=local_order.symbol,
            order_id=local_order.order_id,
            request_id=event.request_id,
            ctp_order_ref=self._order_ref_from_event(event),
            gateway="ctp",
        )
        self.strategy.on_order(self._context, local_order)

    def on_order_action_error(self, event: CtpCallbackEvent) -> None:
        local_order = self._local_order_for_action_error(event)
        if local_order is None:
            self._events.record(
                self.current_time,
                "ORDER_CANCEL_REJECTED_UNMATCHED",
                event.message,
                "WARN",
                order_id=str(event.data.get("OrderRef", "")) or None,
                request_id=event.request_id,
                gateway="ctp",
            )
            return

        local_order.reject_reason = event.message
        self._events.record(
            self.current_time,
            "ORDER_CANCEL_REJECTED",
            event.message,
            "WARN",
            symbol=local_order.symbol,
            order_id=local_order.order_id,
            request_id=event.request_id,
            ctp_order_ref=self._order_ref_from_event(event),
            gateway="ctp",
        )
        self.strategy.on_order(self._context, local_order)

    def on_trade_return(self, ctp_trade: Trade) -> None:
        local_order = self._local_order_for_ctp_order_id(ctp_trade.order_id)
        if local_order is None:
            self._events.record(
                ctp_trade.timestamp,
                "TRADE_RETURN_UNMATCHED",
                f"unmatched CTP trade return {ctp_trade.trade_id}",
                "WARN",
                symbol=ctp_trade.symbol,
                trade_id=ctp_trade.trade_id,
                ctp_order_ref=ctp_trade.order_id,
                gateway="ctp",
            )
            return

        self.current_time = ctp_trade.timestamp
        local_trade = Trade(
            trade_id=ctp_trade.trade_id,
            order_id=local_order.order_id,
            symbol=ctp_trade.symbol,
            side=ctp_trade.side,
            quantity=ctp_trade.quantity,
            price=ctp_trade.price,
            commission=ctp_trade.commission,
            timestamp=ctp_trade.timestamp,
            offset=ctp_trade.offset,
            notional=ctp_trade.notional,
            margin=ctp_trade.margin,
            realized_pnl=ctp_trade.realized_pnl,
        )
        self._trades.append(local_trade)
        self._apply_trade_to_order(local_order, local_trade)
        self._events.record(
            ctp_trade.timestamp,
            "TRADE_RETURN",
            f"ctp trade {local_trade.trade_id} {local_trade.symbol} @ {local_trade.price:.4f}",
            symbol=local_trade.symbol,
            order_id=local_order.order_id,
            trade_id=local_trade.trade_id,
            ctp_order_ref=ctp_trade.order_id,
            price=local_trade.price,
            quantity=local_trade.quantity,
            gateway="ctp",
        )
        self.strategy.on_order(self._context, local_order)
        self.strategy.on_trade(self._context, local_trade)

    def history(self, symbol: str, limit: int | None = None) -> list[Bar]:
        bars = self._histories.get(symbol, [])
        if limit is None:
            return list(bars)
        return list(bars[-limit:])

    def position(self, symbol: str) -> Position:
        futures_position = self.trading_session.gateway.positions.get(symbol)
        if futures_position is not None:
            return futures_position.to_net_position()
        return Position(symbol=symbol)

    def last_price(self, symbol: str) -> float | None:
        tick = self.last_tick(symbol)
        return tick.last_price if tick is not None else None

    def last_tick(self, symbol: str) -> Tick | None:
        return self._last_ticks.get(symbol) or self.market_data_session.gateway.ticks.get(symbol)

    def equity(self) -> float:
        account = self.trading_session.gateway.account
        if account is None:
            return self.initial_cash
        return account.to_snapshot().get("equity", self.initial_cash)

    def check_watchdog(self, force: bool = True) -> None:
        watchdog_events = self.watchdog.check(force=force)
        for event in watchdog_events:
            payload = dict(event.payload)
            payload["gateway"] = "ctp"
            self._events.record(
                self.current_time,
                event.event_type,
                event.message,
                "INFO" if event.ok else "WARN",
                **payload,
            )
        if self._should_reconcile_after_watchdog(watchdog_events):
            symbols = self._active_reconcile_symbols()
            self._events.record(
                self.current_time,
                "AUTO_RECONCILE_AFTER_WATCHDOG",
                "starting lightweight reconciliation after watchdog recovery",
                gateway="ctp",
                symbols=symbols,
            )
            self.reconcile(
                query_account=False,
                query_positions=True,
                query_orders=True,
                query_trades=True,
                symbols=symbols,
            )

    def reconcile(
        self,
        query_account: bool = True,
        query_positions: bool = True,
        query_orders: bool = True,
        query_trades: bool = True,
        symbols: Iterable[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> bool:
        reconcile_symbols = _normalize_symbols(symbols)
        self._events.record(
            self.current_time,
            "RECONCILE_START",
            "starting CTP realtime reconciliation",
            gateway="ctp",
            query_account=query_account,
            query_positions=query_positions,
            query_orders=query_orders,
            query_trades=query_trades,
            symbols=reconcile_symbols,
            start_time=start_time,
            end_time=end_time,
        )
        ok = self.trading_session.reconcile(
            query_account=query_account,
            query_positions=query_positions,
            query_orders=query_orders,
            query_trades=query_trades,
            symbols=reconcile_symbols,
            start_time=start_time,
            end_time=end_time,
        )
        if not ok:
            self._events.record(
                self.current_time,
                "RECONCILE_ERROR",
                "CTP realtime reconciliation failed",
                "WARN",
                gateway="ctp",
            )
            return False

        order_count = (
            self._sync_orders_from_ctp_gateway(reconcile_symbols)
            if query_orders
            else 0
        )
        trade_count = (
            self._sync_trades_from_ctp_gateway(reconcile_symbols)
            if query_trades
            else 0
        )
        self._events.record(
            self.current_time,
            "RECONCILE_READY",
            "CTP realtime reconciliation complete",
            gateway="ctp",
            orders=order_count,
            trades=trade_count,
            positions=len(self.trading_session.gateway.positions),
            symbols=reconcile_symbols,
        )
        return True

    def snapshot(self) -> dict[str, object]:
        return {
            "current_time": self.current_time.isoformat() if self.current_time else None,
            "orders": [_order_to_dict(order) for order in self._orders],
            "trades": [_trade_to_dict(trade) for trade in self._trades],
            "ticks": {
                symbol: tick_to_dict(tick)
                for symbol, tick in self._last_ticks.items()
            },
            "bars": {
                symbol: [_bar_to_dict(bar) for bar in bars]
                for symbol, bars in self._histories.items()
            },
            "events": [
                {
                    "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "symbol": event.symbol,
                    "order_id": event.order_id,
                    "trade_id": event.trade_id,
                    "message": event.message,
                    "payload": event.payload,
                }
                for event in self._events.events
            ],
            "trading": self.trading_session.snapshot(),
            "market_data": self.market_data_session.snapshot(),
            "watchdog": self.watchdog.snapshot(),
        }

    def runtime_state(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "saved_at": datetime.now().isoformat(),
            "current_time": self.current_time.isoformat() if self.current_time else None,
            "order_seq": self._order_seq,
            "gateway_request_id": self.trading_session.gateway.request_id,
            "next_order_ref": self.trading_session.gateway._next_order_ref,
            "orders": [_order_to_dict(order) for order in self._orders],
            "trades": [_trade_to_dict(trade) for trade in self._trades],
            "strategy": {
                "name": self.strategy.name,
                "class": f"{self.strategy.__class__.__module__}.{self.strategy.__class__.__qualname__}",
                "state_schema_version": _strategy_state_schema_version(self.strategy),
                "state": dict(self.strategy.snapshot_state() or {}),
            },
            "last_ticks": {
                symbol: tick_to_dict(tick)
                for symbol, tick in self._last_ticks.items()
            },
            "ctp_order_ref_to_local_id": dict(self._ctp_order_ref_to_local_id),
            "cancel_request_to_local_order_id": {
                str(request_id): order_id
                for request_id, order_id in self._cancel_request_to_local_order_id.items()
            },
            "local_to_ctp": {
                local_order_id: [
                    _ctp_order_insert_request_to_dict(request)
                    for request in requests
                ]
                for local_order_id, requests in self.trading_session.gateway.local_to_ctp.items()
            },
            "filled_quantity_by_order_id": dict(self._filled_quantity_by_order_id),
            "fill_notional_by_order_id": dict(self._fill_notional_by_order_id),
            "watchdog": self.watchdog.snapshot(),
            "last_reconcile": self._last_reconcile_event(),
        }

    def save_state(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.runtime_state()
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._events.record(
            self.current_time,
            "STATE_SAVED",
            f"saved CTP realtime state to {target}",
            gateway="ctp",
            path=str(target),
            orders=len(self._orders),
            trades=len(self._trades),
        )
        return target

    def load_state(self, path: str | Path) -> dict[str, object]:
        source = Path(path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        self.current_time = _optional_datetime(payload.get("current_time"))
        self._orders = [_order_from_dict(item) for item in payload.get("orders", [])]
        self._orders_by_id = {order.order_id: order for order in self._orders}
        self._trades = [_trade_from_dict(item) for item in payload.get("trades", [])]
        self._last_ticks = {
            str(symbol): _tick_from_dict(tick)
            for symbol, tick in dict(payload.get("last_ticks", {})).items()
        }
        self._ctp_order_ref_to_local_id = {
            str(order_ref): str(local_id)
            for order_ref, local_id in dict(
                payload.get("ctp_order_ref_to_local_id", {})
            ).items()
        }
        self._cancel_request_to_local_order_id = {
            int(request_id): str(order_id)
            for request_id, order_id in dict(
                payload.get("cancel_request_to_local_order_id", {})
            ).items()
        }
        self.trading_session.gateway.local_to_ctp = {
            str(local_order_id): [
                _ctp_order_insert_request_from_dict(item)
                for item in requests
            ]
            for local_order_id, requests in dict(payload.get("local_to_ctp", {})).items()
        }
        self._filled_quantity_by_order_id = {
            str(order_id): float(quantity)
            for order_id, quantity in dict(
                payload.get("filled_quantity_by_order_id", {})
            ).items()
        }
        self._fill_notional_by_order_id = {
            str(order_id): float(notional)
            for order_id, notional in dict(
                payload.get("fill_notional_by_order_id", {})
            ).items()
        }
        if not self._filled_quantity_by_order_id and self._trades:
            self._rebuild_fill_totals()
        self._order_seq = int(payload.get("order_seq") or self._infer_order_seq())
        self.trading_session.gateway.request_id = int(
            payload.get("gateway_request_id", self.trading_session.gateway.request_id)
        )
        self.trading_session.gateway._next_order_ref = int(
            payload.get("next_order_ref", self._infer_next_order_ref())
        )
        strategy_payload = _strategy_state_from_payload(payload)
        if strategy_payload is not None:
            strategy_state, state_version = strategy_payload
            current_version = _strategy_state_schema_version(self.strategy)
            if state_version != current_version:
                strategy_state = dict(
                    self.strategy.migrate_state(strategy_state, state_version) or {}
                )
                self._events.record(
                    self.current_time,
                    "STRATEGY_STATE_MIGRATED",
                    f"migrated strategy state for {self.strategy.name}",
                    gateway="ctp",
                    strategy=self.strategy.name,
                    from_version=state_version,
                    to_version=current_version,
                    keys=sorted(strategy_state.keys()),
                )
            self._pending_strategy_state = dict(strategy_state)
            self.strategy.restore_state(self._pending_strategy_state)
            if self._strategy_initialized:
                self._restore_pending_strategy_state()
        self._events.record(
            self.current_time,
            "STATE_LOADED",
            f"loaded CTP realtime state from {source}",
            gateway="ctp",
            path=str(source),
            orders=len(self._orders),
            trades=len(self._trades),
        )
        return payload

    def _new_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType,
        limit_price: float | None,
        offset: Offset,
    ) -> Order:
        self._order_seq += 1
        return Order(
            order_id=f"L{self._order_seq:08d}",
            symbol=symbol,
            side=side,
            quantity=float(quantity),
            submitted_at=self.current_time,
            order_type=order_type,
            offset=offset,
            limit_price=limit_price,
        )

    def _reject_reason(self, order: Order) -> str | None:
        if order.quantity <= 0:
            return "quantity must be positive"
        if order.order_type == OrderType.LIMIT and order.limit_price is None:
            return "limit_price is required for limit orders"
        if not self.trading_session.logged_in:
            return "CTP trading session is not logged in"
        return self.risk_manager.check_order(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            current_position=self.position(order.symbol).quantity,
            reference_price=self.last_price(order.symbol),
            current_equity=self.equity(),
            initial_cash=self.initial_cash,
        )

    def _reject_order(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self._orders.append(order)
        self._orders_by_id[order.order_id] = order
        self._events.record(
            self.current_time,
            "ORDER_REJECTED",
            reason,
            "WARN",
            symbol=order.symbol,
            order_id=order.order_id,
            side=order.side.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            offset=order.offset.value,
            gateway="ctp",
        )
        self.strategy.on_order(self._context, order)

    def _local_order_for_ctp_order_id(self, ctp_order_id: str) -> Order | None:
        local_order_id = self._ctp_order_ref_to_local_id.get(ctp_order_id)
        if local_order_id is None:
            local_order_id = self.trading_session.gateway.local_order_id_for_order_ref(
                ctp_order_id
            )
            if local_order_id is not None:
                self._ctp_order_ref_to_local_id[ctp_order_id] = local_order_id
        if local_order_id is None:
            return None
        return self._orders_by_id.get(local_order_id)

    def _local_order_for_order_error(self, event: CtpCallbackEvent) -> Order | None:
        order_ref = self._order_ref_from_event(event)
        if order_ref:
            return self._local_order_for_ctp_order_id(order_ref)
        local_order_id = self.trading_session.gateway.local_order_id_for_request_id(
            event.request_id
        )
        if local_order_id is None:
            return None
        return self._orders_by_id.get(local_order_id)

    def _local_order_for_action_error(self, event: CtpCallbackEvent) -> Order | None:
        local_order_id = self._cancel_request_to_local_order_id.get(event.request_id)
        if local_order_id is not None:
            return self._orders_by_id.get(local_order_id)
        return self._local_order_for_order_error(event)

    def _order_ref_from_event(self, event: CtpCallbackEvent) -> str:
        order_ref = str(event.data.get("OrderRef", "") or "")
        if order_ref:
            return order_ref
        return self.trading_session.gateway.order_ref_for_request_id(event.request_id) or ""

    def _sync_orders_from_ctp_gateway(self, symbols: Iterable[str] | None = None) -> int:
        symbol_set = set(_normalize_symbols(symbols))
        count = 0
        for ctp_order in self.trading_session.gateway.orders.values():
            if symbol_set and ctp_order.symbol not in symbol_set:
                continue
            local_order = self._local_order_for_ctp_order_id(ctp_order.order_id)
            if local_order is None:
                self._events.record(
                    ctp_order.submitted_at,
                    "ORDER_RECONCILE_UNMATCHED",
                    f"unmatched CTP order during reconciliation {ctp_order.order_id}",
                    "WARN",
                    symbol=ctp_order.symbol,
                    order_id=ctp_order.order_id,
                    status=ctp_order.status.value,
                    gateway="ctp",
                )
                continue

            changed = (
                local_order.status != ctp_order.status
                or local_order.fill_price != ctp_order.fill_price
                or local_order.filled_at != ctp_order.filled_at
                or local_order.reject_reason != ctp_order.reject_reason
            )
            local_order.status = ctp_order.status
            local_order.fill_price = ctp_order.fill_price
            local_order.filled_at = ctp_order.filled_at
            local_order.reject_reason = ctp_order.reject_reason
            count += 1
            self._events.record(
                ctp_order.submitted_at,
                "ORDER_RECONCILED",
                f"ctp order {local_order.order_id} reconciled status={local_order.status.value}",
                symbol=local_order.symbol,
                order_id=local_order.order_id,
                ctp_order_ref=ctp_order.order_id,
                status=local_order.status.value,
                changed=changed,
                gateway="ctp",
            )
            if changed:
                self.strategy.on_order(self._context, local_order)
        return count

    def _sync_trades_from_ctp_gateway(self, symbols: Iterable[str] | None = None) -> int:
        symbol_set = set(_normalize_symbols(symbols))
        existing_trade_ids = {trade.trade_id for trade in self._trades}
        count = 0
        for ctp_trade in self.trading_session.gateway.trades.values():
            if symbol_set and ctp_trade.symbol not in symbol_set:
                continue
            local_order = self._local_order_for_ctp_order_id(ctp_trade.order_id)
            if local_order is None:
                self._events.record(
                    ctp_trade.timestamp,
                    "TRADE_RECONCILE_UNMATCHED",
                    f"unmatched CTP trade during reconciliation {ctp_trade.trade_id}",
                    "WARN",
                    symbol=ctp_trade.symbol,
                    trade_id=ctp_trade.trade_id,
                    ctp_order_ref=ctp_trade.order_id,
                    gateway="ctp",
                )
                continue

            if ctp_trade.trade_id in existing_trade_ids:
                count += 1
                continue

            local_trade = Trade(
                trade_id=ctp_trade.trade_id,
                order_id=local_order.order_id,
                symbol=ctp_trade.symbol,
                side=ctp_trade.side,
                quantity=ctp_trade.quantity,
                price=ctp_trade.price,
                commission=ctp_trade.commission,
                timestamp=ctp_trade.timestamp,
                offset=ctp_trade.offset,
                notional=ctp_trade.notional,
                margin=ctp_trade.margin,
                realized_pnl=ctp_trade.realized_pnl,
            )
            self._trades.append(local_trade)
            existing_trade_ids.add(local_trade.trade_id)
            self._apply_trade_to_order(local_order, local_trade)
            count += 1
            self._events.record(
                ctp_trade.timestamp,
                "TRADE_RECONCILED",
                f"ctp trade {local_trade.trade_id} reconciled",
                symbol=local_trade.symbol,
                order_id=local_order.order_id,
                trade_id=local_trade.trade_id,
                ctp_order_ref=ctp_trade.order_id,
                price=local_trade.price,
                quantity=local_trade.quantity,
                gateway="ctp",
            )
            self.strategy.on_order(self._context, local_order)
            self.strategy.on_trade(self._context, local_trade)
        return count

    def _should_reconcile_after_watchdog(self, events: Iterable[object]) -> bool:
        if not self.trading_session.gateway.config.auto_reconcile_after_watchdog_recovery:
            return False
        return any(
            getattr(event, "event_type", "") == "WATCHDOG_TRADING_RECOVER_READY"
            for event in events
        )

    def _active_reconcile_symbols(self) -> list[str]:
        symbols = {
            *self.market_data_session.subscribed_symbols,
            *self._last_ticks.keys(),
            *self.trading_session.gateway.positions.keys(),
            *(order.symbol for order in self._orders),
        }
        return sorted(symbol for symbol in symbols if symbol)

    def _last_reconcile_event(self) -> dict[str, object] | None:
        for event in reversed(self._events.events):
            if event.event_type in {"RECONCILE_READY", "RECONCILE_ERROR"}:
                return {
                    "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "message": event.message,
                    "payload": event.payload,
                }
        return None

    def _restore_pending_strategy_state(self) -> None:
        if self._pending_strategy_state is None:
            return
        self.strategy.restore_state(self._pending_strategy_state)
        self._events.record(
            self.current_time,
            "STRATEGY_STATE_LOADED",
            f"restored strategy state for {self.strategy.name}",
            gateway="ctp",
            strategy=self.strategy.name,
            state_schema_version=_strategy_state_schema_version(self.strategy),
            keys=sorted(self._pending_strategy_state.keys()),
        )
        self._pending_strategy_state = None

    def _rebuild_fill_totals(self) -> None:
        self._filled_quantity_by_order_id = {}
        self._fill_notional_by_order_id = {}
        for trade in self._trades:
            self._filled_quantity_by_order_id[trade.order_id] = (
                self._filled_quantity_by_order_id.get(trade.order_id, 0.0)
                + trade.quantity
            )
            self._fill_notional_by_order_id[trade.order_id] = (
                self._fill_notional_by_order_id.get(trade.order_id, 0.0)
                + trade.price * trade.quantity
            )

    def _infer_order_seq(self) -> int:
        max_seq = 0
        for order in self._orders:
            if order.order_id.startswith("L"):
                try:
                    max_seq = max(max_seq, int(order.order_id[1:]))
                except ValueError:
                    continue
        return max_seq

    def _infer_next_order_ref(self) -> int:
        max_ref = 0
        for requests in self.trading_session.gateway.local_to_ctp.values():
            for request in requests:
                try:
                    max_ref = max(max_ref, int(request.order_ref))
                except ValueError:
                    continue
        return max(max_ref + 1, self.trading_session.gateway._next_order_ref)

    def _apply_trade_to_order(self, order: Order, trade: Trade) -> None:
        old_quantity = self._filled_quantity_by_order_id.get(order.order_id, 0.0)
        old_notional = self._fill_notional_by_order_id.get(order.order_id, 0.0)
        new_quantity = old_quantity + trade.quantity
        new_notional = old_notional + trade.price * trade.quantity
        self._filled_quantity_by_order_id[order.order_id] = new_quantity
        self._fill_notional_by_order_id[order.order_id] = new_notional

        order.fill_price = new_notional / new_quantity if new_quantity else None
        order.filled_at = trade.timestamp
        order.commission += trade.commission
        if new_quantity + 1e-12 >= order.quantity:
            order.status = OrderStatus.FILLED

    def _dispatch_completed_bars(self, tick: Tick) -> None:
        if self._bar_aggregator is None:
            return
        for bar in self._bar_aggregator.update(tick):
            self._histories[bar.symbol].append(bar)
            self._events.record(
                bar.timestamp,
                "BAR",
                f"{bar.symbol} realtime bar close={bar.close:.4f}",
                symbol=bar.symbol,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                frequency=self.bar_frequency,
                gateway="ctp",
            )
            self.strategy.on_bar(self._context, bar)

    def flush_bars(self) -> list[Bar]:
        if self._bar_aggregator is None:
            return []
        bars = self._bar_aggregator.flush()
        for bar in bars:
            self._histories[bar.symbol].append(bar)
            self._events.record(
                bar.timestamp,
                "BAR",
                f"{bar.symbol} realtime bar close={bar.close:.4f}",
                symbol=bar.symbol,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                frequency=self.bar_frequency,
                gateway="ctp",
            )
            self.strategy.on_bar(self._context, bar)
        return bars


class TickBarAggregator:
    def __init__(self, frequency: str = "1min") -> None:
        self.frequency = frequency
        self._current: dict[str, Bar] = {}
        self._last_volume: dict[str, float] = {}

    def update(self, tick: Tick) -> list[Bar]:
        bucket = _tick_bucket(tick.timestamp, self.frequency)
        volume_delta = self._volume_delta(tick)
        current = self._current.get(tick.symbol)
        if current is None:
            self._current[tick.symbol] = _bar_from_tick(tick, bucket, volume_delta)
            return []
        if current.timestamp == bucket:
            current.extra["last_tick_time"] = tick.timestamp.isoformat()
            self._current[tick.symbol] = Bar(
                symbol=current.symbol,
                timestamp=current.timestamp,
                open=current.open,
                high=max(current.high, tick.last_price),
                low=min(current.low, tick.last_price),
                close=tick.last_price,
                volume=current.volume + volume_delta,
                extra=current.extra,
            )
            return []
        completed = current
        self._current[tick.symbol] = _bar_from_tick(tick, bucket, volume_delta)
        return [completed]

    def flush(self) -> list[Bar]:
        bars = list(self._current.values())
        self._current.clear()
        return bars

    def _volume_delta(self, tick: Tick) -> float:
        previous = self._last_volume.get(tick.symbol)
        self._last_volume[tick.symbol] = tick.volume
        if previous is None:
            return 0.0
        return max(tick.volume - previous, 0.0)


def _order_to_dict(order: Order) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": order.quantity,
        "submitted_at": order.submitted_at.isoformat(),
        "order_type": order.order_type.value,
        "offset": order.offset.value,
        "limit_price": order.limit_price,
        "status": order.status.value,
        "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        "fill_price": order.fill_price,
        "commission": order.commission,
        "reject_reason": order.reject_reason,
    }


def _bar_to_dict(bar: Bar) -> dict[str, object]:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "extra": bar.extra,
    }


def _trade_to_dict(trade: Trade) -> dict[str, object]:
    return {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "quantity": trade.quantity,
        "price": trade.price,
        "commission": trade.commission,
        "timestamp": trade.timestamp.isoformat(),
        "offset": trade.offset.value,
        "notional": trade.notional,
        "margin": trade.margin,
        "realized_pnl": trade.realized_pnl,
    }


def _order_from_dict(raw: Mapping[str, Any]) -> Order:
    return Order(
        order_id=str(raw.get("order_id", "")),
        symbol=str(raw.get("symbol", "")),
        side=Side(str(raw.get("side", Side.BUY.value))),
        quantity=float(raw.get("quantity", 0.0)),
        submitted_at=_datetime_from_value(raw.get("submitted_at")),
        order_type=OrderType(str(raw.get("order_type", OrderType.MARKET.value))),
        offset=Offset(str(raw.get("offset", Offset.AUTO.value))),
        limit_price=_optional_float(raw.get("limit_price")),
        status=OrderStatus(str(raw.get("status", OrderStatus.PENDING.value))),
        filled_at=_optional_datetime(raw.get("filled_at")),
        fill_price=_optional_float(raw.get("fill_price")),
        commission=float(raw.get("commission", 0.0)),
        reject_reason=(
            str(raw.get("reject_reason"))
            if raw.get("reject_reason") is not None
            else None
        ),
    )


def _trade_from_dict(raw: Mapping[str, Any]) -> Trade:
    return Trade(
        trade_id=str(raw.get("trade_id", "")),
        order_id=str(raw.get("order_id", "")),
        symbol=str(raw.get("symbol", "")),
        side=Side(str(raw.get("side", Side.BUY.value))),
        quantity=float(raw.get("quantity", 0.0)),
        price=float(raw.get("price", 0.0)),
        commission=float(raw.get("commission", 0.0)),
        timestamp=_datetime_from_value(raw.get("timestamp")),
        offset=Offset(str(raw.get("offset", Offset.AUTO.value))),
        notional=float(raw.get("notional", 0.0)),
        margin=float(raw.get("margin", 0.0)),
        realized_pnl=float(raw.get("realized_pnl", 0.0)),
    )


def _tick_from_dict(raw: Mapping[str, Any]) -> Tick:
    return Tick(
        symbol=str(raw.get("symbol", "")),
        timestamp=_datetime_from_value(raw.get("timestamp")),
        last_price=float(raw.get("last_price", 0.0)),
        volume=float(raw.get("volume", 0.0)),
        turnover=float(raw.get("turnover", 0.0)),
        open_interest=float(raw.get("open_interest", 0.0)),
        bid_price_1=_optional_float(raw.get("bid_price_1")),
        bid_volume_1=float(raw.get("bid_volume_1", 0.0)),
        ask_price_1=_optional_float(raw.get("ask_price_1")),
        ask_volume_1=float(raw.get("ask_volume_1", 0.0)),
        open_price=_optional_float(raw.get("open_price")),
        high_price=_optional_float(raw.get("high_price")),
        low_price=_optional_float(raw.get("low_price")),
        pre_close_price=_optional_float(raw.get("pre_close_price")),
        extra=dict(raw.get("extra", {})),
    )


def _ctp_order_insert_request_to_dict(
    request: CtpOrderInsertRequest,
) -> dict[str, object]:
    instruction = request.instruction
    return {
        "field": request.field,
        "local_order_id": request.local_order_id,
        "order_ref": request.order_ref,
        "request_id": request.request_id,
        "instruction": {
            "local_order_id": instruction.local_order_id,
            "symbol": instruction.symbol,
            "side": instruction.side.value,
            "offset": instruction.offset.value,
            "quantity": instruction.quantity,
            "order_type": instruction.order_type.value,
            "limit_price": instruction.limit_price,
        },
    }


def _ctp_order_insert_request_from_dict(
    raw: Mapping[str, Any],
) -> CtpOrderInsertRequest:
    instruction_raw = dict(raw.get("instruction", {}))
    field = dict(raw.get("field", {}))
    instruction = CtpOrderInstruction(
        local_order_id=str(
            instruction_raw.get(
                "local_order_id",
                raw.get("local_order_id", ""),
            )
        ),
        symbol=str(instruction_raw.get("symbol", field.get("InstrumentID", ""))),
        side=Side(str(instruction_raw.get("side", Side.BUY.value))),
        offset=Offset(str(instruction_raw.get("offset", Offset.AUTO.value))),
        quantity=float(
            instruction_raw.get(
                "quantity",
                field.get("VolumeTotalOriginal", 0.0),
            )
        ),
        order_type=OrderType(
            str(instruction_raw.get("order_type", OrderType.MARKET.value))
        ),
        limit_price=_optional_float(instruction_raw.get("limit_price")),
    )
    return CtpOrderInsertRequest(
        field=field,
        local_order_id=str(raw.get("local_order_id", instruction.local_order_id)),
        order_ref=str(raw.get("order_ref", field.get("OrderRef", ""))),
        request_id=int(raw.get("request_id", field.get("RequestID", 0))),
        instruction=instruction,
    )


def _strategy_state_from_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], int] | None:
    strategy_payload = payload.get("strategy")
    if isinstance(strategy_payload, Mapping):
        state = strategy_payload.get("state")
        if isinstance(state, Mapping):
            version = int(
                strategy_payload.get(
                    "state_schema_version",
                    strategy_payload.get("schema_version", 0),
                )
            )
            return dict(state), version
    legacy_state = payload.get("strategy_state")
    if isinstance(legacy_state, Mapping):
        return dict(legacy_state), 0
    return None


def _strategy_state_schema_version(strategy: Strategy) -> int:
    return int(getattr(strategy, "state_schema_version", 1))


def _datetime_from_value(value: Any) -> datetime:
    parsed = _optional_datetime(value)
    return parsed if parsed is not None else datetime.now()


def _optional_datetime(value: Any) -> datetime | None:
    if value in {"", None}:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _optional_float(value: Any) -> float | None:
    if value in {"", None}:
        return None
    return float(value)


def _bar_from_tick(tick: Tick, timestamp: datetime, volume: float) -> Bar:
    return Bar(
        symbol=tick.symbol,
        timestamp=timestamp,
        open=tick.last_price,
        high=tick.last_price,
        low=tick.last_price,
        close=tick.last_price,
        volume=volume,
        extra={
            "source": "ctp_tick",
            "frequency": "1min",
            "first_tick_time": tick.timestamp.isoformat(),
            "last_tick_time": tick.timestamp.isoformat(),
        },
    )


def _tick_bucket(timestamp: datetime, frequency: str) -> datetime:
    normalized = frequency.lower().replace(" ", "")
    if normalized in {"1m", "1min", "1minute"}:
        return timestamp.replace(second=0, microsecond=0)
    raise ValueError(f"unsupported realtime bar frequency: {frequency}")


def _normalize_symbols(symbols: Iterable[str] | None) -> list[str]:
    if symbols is None:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        text = str(symbol).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized
