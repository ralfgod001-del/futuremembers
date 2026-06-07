from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from .events import EventRecorder, RunEvent
from .execution import ExecutionConfig
from .futures import ContractRegistry, FuturesPosition
from .models import Bar, Offset, Order, OrderStatus, OrderType, Position, Side, Tick, Trade
from .reporting import calculate_metrics, export_backtest_report
from .risk import RiskManager
from .strategy import Strategy, StrategyContext


@dataclass
class PaperTradingResult:
    initial_cash: float
    final_equity: float
    metrics: dict[str, float]
    equity_curve: pd.DataFrame
    orders: list[Order]
    trades: list[Trade]
    positions: dict[str, Position]
    events: list[RunEvent]
    account_mode: str
    futures_positions: dict[str, FuturesPosition]
    steps: int
    gateway: str = "paper"

    def export(self, output_dir: str | Path) -> dict[str, Path]:
        return export_backtest_report(self, output_dir)


class PaperBrokerGateway:
    def __init__(
        self,
        initial_cash: float = 100_000.0,
        execution_config: ExecutionConfig | None = None,
        risk_manager: RiskManager | None = None,
        account_mode: str = "cash",
        contract_registry: ContractRegistry | None = None,
        events: EventRecorder | None = None,
    ) -> None:
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.execution_config = execution_config or ExecutionConfig.from_legacy()
        self.risk_manager = risk_manager or RiskManager()
        self.account_mode = account_mode
        if self.account_mode not in {"cash", "futures"}:
            raise ValueError("account_mode must be cash or futures")
        self.contract_registry = contract_registry or ContractRegistry()
        self.events = events or EventRecorder()

        self.current_time: datetime | None = None
        self._positions: dict[str, Position] = {}
        self._futures_positions: dict[str, FuturesPosition] = {}
        self._last_prices: dict[str, float] = {}
        self._orders: list[Order] = []
        self._pending_orders: list[Order] = []
        self._trades: list[Trade] = []
        self._order_seq = 0
        self._trade_seq = 0

    @property
    def orders(self) -> list[Order]:
        return self._orders

    @property
    def trades(self) -> list[Trade]:
        return self._trades

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    @property
    def futures_positions(self) -> dict[str, FuturesPosition]:
        return self._futures_positions

    @property
    def pending_orders(self) -> list[Order]:
        return [order for order in self._pending_orders if order.status == OrderStatus.PENDING]

    def update_market(self, bar: Bar) -> None:
        self._last_prices[bar.symbol] = bar.close

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
            raise RuntimeError("orders can only be submitted while paper trading is running")

        self._order_seq += 1
        order = Order(
            order_id=f"P{self._order_seq:08d}",
            symbol=symbol,
            side=side,
            quantity=float(quantity),
            submitted_at=self.current_time,
            order_type=order_type,
            offset=offset,
            limit_price=limit_price,
        )

        reject_reason = self._submission_reject_reason(order)
        if reject_reason:
            self._reject_order(order, reject_reason)
            self._orders.append(order)
            return order

        self._orders.append(order)
        self._pending_orders.append(order)
        self.events.record(
            self.current_time,
            "ORDER_SUBMITTED",
            f"paper accepted {side.value} {quantity:g} {symbol}",
            symbol=symbol,
            order_id=order.order_id,
            side=side.value,
            quantity=quantity,
            order_type=order_type.value,
            offset=offset.value,
            limit_price=limit_price,
            gateway="paper",
        )
        return order

    def cancel_order(self, order_id: str, reason: str = "canceled by strategy") -> Order | None:
        if self.current_time is None:
            raise RuntimeError("orders can only be canceled while paper trading is running")

        for order in list(self._pending_orders):
            if order.order_id != order_id or order.status != OrderStatus.PENDING:
                continue
            order.status = OrderStatus.CANCELED
            order.reject_reason = reason
            self._pending_orders.remove(order)
            self.events.record(
                self.current_time,
                "ORDER_CANCELED",
                reason,
                "WARN",
                symbol=order.symbol,
                order_id=order.order_id,
                gateway="paper",
            )
            return order
        return None

    def match_orders(self, current_by_symbol: dict[str, Bar]) -> tuple[list[Order], list[Trade]]:
        order_updates: list[Order] = []
        trades: list[Trade] = []
        remaining: list[Order] = []

        for order in self._pending_orders:
            if order.status != OrderStatus.PENDING:
                continue
            bar = current_by_symbol.get(order.symbol)
            if bar is None:
                remaining.append(order)
                continue
            if not self._order_crosses(order, bar):
                remaining.append(order)
                continue

            fill_reject_reason = self._futures_order_reject_reason(order)
            if fill_reject_reason:
                self._reject_order(order, fill_reject_reason)
                order_updates.append(order)
                continue

            trade = self._fill_order(order, bar)
            order_updates.append(order)
            trades.append(trade)

        self._pending_orders = remaining
        return order_updates, trades

    def position(self, symbol: str) -> Position:
        if symbol not in self._positions:
            self._positions[symbol] = Position(symbol=symbol)
        return self._positions[symbol]

    def last_price(self, symbol: str) -> float | None:
        return self._last_prices.get(symbol)

    def equity(self) -> float:
        if self.account_mode == "futures":
            return self.cash + self._futures_unrealized_pnl()
        return self.cash + sum(
            position.market_value(self._last_prices.get(symbol, position.avg_price))
            for symbol, position in self._positions.items()
        )

    def account_snapshot(self) -> dict[str, float]:
        equity = self.equity()
        margin = self._futures_margin() if self.account_mode == "futures" else 0.0
        return {
            "cash": self.cash,
            "equity": equity,
            "invested_value": self._invested_value(),
            "margin": margin,
            "available": equity - margin,
            "risk_ratio": margin / equity if equity else 0.0,
            "unrealized_pnl": self._futures_unrealized_pnl()
            if self.account_mode == "futures"
            else equity - self.cash - self._invested_value(),
        }

    def final_account_metrics(self, equity_curve: pd.DataFrame) -> dict[str, float]:
        if equity_curve.empty:
            return {}
        final = equity_curve.iloc[-1]
        return {
            "final_cash": float(final.get("cash", 0.0)),
            "final_margin": float(final.get("margin", 0.0)),
            "final_available": float(final.get("available", final.get("equity", 0.0))),
            "final_risk_ratio": float(final.get("risk_ratio", 0.0)),
            "final_unrealized_pnl": float(final.get("unrealized_pnl", 0.0)),
            "final_settlement_pnl": sum(
                position.settlement_pnl for position in self._futures_positions.values()
            ),
            "working_order_count": float(len(self.pending_orders)),
        }

    def settle_futures_positions(self, timestamp: datetime) -> None:
        total_pnl = 0.0
        for symbol, position in self._futures_positions.items():
            if position.long_quantity == 0 and position.short_quantity == 0:
                continue
            spec = self.contract_registry.for_symbol(symbol)
            settlement_price = self._last_prices.get(
                symbol,
                position.long_avg_price or position.short_avg_price,
            )
            pnl = position.settle(settlement_price, spec.multiplier)
            total_pnl += pnl
            self._positions[symbol] = position.to_net_position()
            self.events.record(
                timestamp,
                "DAILY_SETTLEMENT",
                f"{symbol} settled @ {settlement_price:.4f}",
                symbol=symbol,
                settlement_price=settlement_price,
                settlement_pnl=pnl,
                gateway="paper",
            )
        if total_pnl:
            self.cash += total_pnl

    def _submission_reject_reason(self, order: Order) -> str | None:
        if order.quantity <= 0:
            return "quantity must be positive"
        if order.order_type == OrderType.LIMIT and order.limit_price is None:
            return "limit_price is required for limit orders"

        reject_reason = self.risk_manager.check_order(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            current_position=self.position(order.symbol).quantity,
            reference_price=self.last_price(order.symbol),
            current_equity=self.equity(),
            initial_cash=self.initial_cash,
        )
        if reject_reason:
            return reject_reason
        return self._futures_order_reject_reason(order)

    def _reject_order(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.events.record(
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
            gateway="paper",
        )

    def _order_crosses(self, order: Order, bar: Bar) -> bool:
        if order.order_type == OrderType.MARKET:
            return True
        if order.limit_price is None:
            return False
        if order.side == Side.BUY:
            return bar.close <= order.limit_price
        return bar.close >= order.limit_price

    def _fill_order(self, order: Order, bar: Bar) -> Trade:
        symbol_execution = self.execution_config.for_symbol(order.symbol)
        fill_price = self._fill_price(order, bar, symbol_execution.slippage)
        if self.account_mode == "futures":
            return self._fill_futures_order(order, bar, fill_price)

        notional = fill_price * order.quantity
        commission = max(
            abs(notional) * symbol_execution.commission_rate,
            symbol_execution.min_commission,
        )

        if order.side == Side.BUY:
            self.cash -= notional + commission
        else:
            self.cash += notional - commission

        position = self.position(order.symbol)
        realized_pnl = position.apply_fill(order.side, order.quantity, fill_price)

        order.status = OrderStatus.FILLED
        order.filled_at = bar.timestamp
        order.fill_price = fill_price
        order.commission = commission

        self._trade_seq += 1
        trade = Trade(
            trade_id=f"PT{self._trade_seq:08d}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            timestamp=bar.timestamp,
            offset=order.offset,
            notional=notional,
            realized_pnl=realized_pnl - commission,
        )
        self._trades.append(trade)
        self._record_fill_event(order, trade, bar, commission, notional, 0.0)
        return trade

    def _fill_futures_order(self, order: Order, bar: Bar, fill_price: float) -> Trade:
        spec = self.contract_registry.for_symbol(order.symbol)
        notional = spec.notional(fill_price, order.quantity)
        margin = spec.margin(fill_price, order.quantity)

        position = self._futures_position(order.symbol)
        fill = position.apply_fill(
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            multiplier=spec.multiplier,
            offset=order.offset,
        )
        commission = spec.commission.calculate_breakdown(
            price=fill_price,
            multiplier=spec.multiplier,
            opened_quantity=fill.opened_quantity,
            closed_today_quantity=fill.closed_today_quantity,
            closed_yesterday_quantity=fill.closed_yesterday_quantity,
        )
        self.cash += fill.realized_pnl - commission
        self._positions[order.symbol] = position.to_net_position()

        order.status = OrderStatus.FILLED
        order.filled_at = bar.timestamp
        order.fill_price = fill_price
        order.commission = commission

        self._trade_seq += 1
        trade = Trade(
            trade_id=f"PT{self._trade_seq:08d}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            timestamp=bar.timestamp,
            offset=order.offset,
            notional=notional,
            margin=margin,
            realized_pnl=fill.realized_pnl - commission,
        )
        self._trades.append(trade)
        self.events.record(
            bar.timestamp,
            "ORDER_FILLED",
            f"paper filled {order.side.value} {order.quantity:g} {order.symbol} @ {fill_price:.4f}",
            symbol=order.symbol,
            order_id=order.order_id,
            trade_id=trade.trade_id,
            price=fill_price,
            quantity=order.quantity,
            commission=commission,
            margin=margin,
            notional=notional,
            realized_pnl=trade.realized_pnl,
            opened_quantity=fill.opened_quantity,
            closed_today_quantity=fill.closed_today_quantity,
            closed_yesterday_quantity=fill.closed_yesterday_quantity,
            gateway="paper",
        )
        return trade

    def _record_fill_event(
        self,
        order: Order,
        trade: Trade,
        bar: Bar,
        commission: float,
        notional: float,
        margin: float,
    ) -> None:
        self.events.record(
            bar.timestamp,
            "ORDER_FILLED",
            f"paper filled {order.side.value} {order.quantity:g} {order.symbol} @ {trade.price:.4f}",
            symbol=order.symbol,
            order_id=order.order_id,
            trade_id=trade.trade_id,
            price=trade.price,
            quantity=order.quantity,
            commission=commission,
            margin=margin,
            notional=notional,
            realized_pnl=trade.realized_pnl,
            gateway="paper",
        )

    def _fill_price(self, order: Order, bar: Bar, slippage: float) -> float:
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            if order.side == Side.BUY:
                base_price = min(order.limit_price, bar.close)
            else:
                base_price = max(order.limit_price, bar.close)
        else:
            base_price = bar.close
        return self._apply_slippage(order.side, base_price, slippage)

    def _apply_slippage(self, side: Side, price: float, slippage: float) -> float:
        if side == Side.BUY:
            return price + slippage
        return price - slippage

    def _futures_position(self, symbol: str) -> FuturesPosition:
        if symbol not in self._futures_positions:
            self._futures_positions[symbol] = FuturesPosition(symbol=symbol)
        return self._futures_positions[symbol]

    def _futures_order_reject_reason(self, order: Order) -> str | None:
        if self.account_mode != "futures":
            return None
        if order.offset not in {Offset.CLOSE, Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}:
            return None
        position = self._futures_position(order.symbol)
        available = position.close_available(order.side, order.offset)
        if order.quantity > available:
            return (
                f"close quantity {order.quantity:g} exceeds available "
                f"{available:g} for {order.offset.value}"
            )
        return None

    def _futures_unrealized_pnl(self) -> float:
        total = 0.0
        for symbol, position in self._futures_positions.items():
            spec = self.contract_registry.for_symbol(symbol)
            last_price = self._last_prices.get(symbol, position.long_avg_price or position.short_avg_price)
            total += position.unrealized_pnl(last_price, spec.multiplier)
        return total

    def _futures_margin(self) -> float:
        total = 0.0
        for symbol, position in self._futures_positions.items():
            spec = self.contract_registry.for_symbol(symbol)
            last_price = self._last_prices.get(symbol, position.long_avg_price or position.short_avg_price)
            total += position.margin(last_price, spec)
        return total

    def _invested_value(self) -> float:
        if self.account_mode == "futures":
            return sum(
                abs(
                    self.contract_registry.for_symbol(symbol).notional(
                        self._last_prices.get(
                            symbol,
                            position.long_avg_price or position.short_avg_price,
                        ),
                        position.long_quantity + position.short_quantity,
                    )
                )
                for symbol, position in self._futures_positions.items()
            )
        return sum(
            abs(position.market_value(self._last_prices.get(symbol, position.avg_price)))
            for symbol, position in self._positions.items()
        )


class PaperTradingEngine:
    def __init__(
        self,
        bars: Iterable[Bar],
        strategy: Strategy,
        initial_cash: float = 100_000.0,
        execution_config: ExecutionConfig | None = None,
        risk_manager: RiskManager | None = None,
        account_mode: str = "cash",
        contract_registry: ContractRegistry | None = None,
        daily_settlement: bool = False,
        max_steps: int | None = None,
    ) -> None:
        self.bars = sorted(bars, key=lambda item: (item.timestamp, item.symbol))
        if not self.bars:
            raise ValueError("paper trading requires at least one bar")

        self.strategy = strategy
        self.initial_cash = float(initial_cash)
        self.account_mode = account_mode
        self.contract_registry = contract_registry or ContractRegistry()
        self.daily_settlement = daily_settlement
        self.max_steps = max_steps
        self.current_time: datetime | None = None

        self._events = EventRecorder()
        self._gateway = PaperBrokerGateway(
            initial_cash=initial_cash,
            execution_config=execution_config,
            risk_manager=risk_manager,
            account_mode=account_mode,
            contract_registry=self.contract_registry,
            events=self._events,
        )
        self._histories: dict[str, list[Bar]] = defaultdict(list)
        self._equity_rows: list[dict[str, float | datetime]] = []
        self._context = StrategyContext(self)

    @property
    def orders(self) -> list[Order]:
        return self._gateway.orders

    @property
    def trades(self) -> list[Trade]:
        return self._gateway.trades

    def run(self) -> PaperTradingResult:
        self._events.record(
            self.current_time,
            "RUN_START",
            f"starting paper strategy {self.strategy.name}",
            initial_cash=self.initial_cash,
            account_mode=self.account_mode,
            gateway="paper",
        )
        self.strategy.on_init(self._context)

        bars_by_time: dict[datetime, list[Bar]] = defaultdict(list)
        for bar in self.bars:
            bars_by_time[bar.timestamp].append(bar)

        timestamps = sorted(bars_by_time)
        if self.max_steps is not None:
            timestamps = timestamps[: self.max_steps]

        for index, timestamp in enumerate(timestamps):
            self.current_time = timestamp
            self._gateway.current_time = timestamp
            current_bars = bars_by_time[timestamp]
            current_by_symbol = {bar.symbol: bar for bar in current_bars}

            for bar in current_bars:
                self._gateway.update_market(bar)
                self._histories[bar.symbol].append(bar)
                self._events.record(
                    timestamp,
                    "BAR",
                    f"{bar.symbol} paper bar close={bar.close:.4f}",
                    symbol=bar.symbol,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    gateway="paper",
                )
                self.strategy.on_bar(self._context, bar)

            order_updates, trades = self._gateway.match_orders(current_by_symbol)
            for order in order_updates:
                self.strategy.on_order(self._context, order)
            for trade in trades:
                self.strategy.on_trade(self._context, trade)

            self._record_equity(timestamp)
            if self._should_settle(index, timestamps, bars_by_time):
                self._gateway.settle_futures_positions(timestamp)
                self._refresh_last_equity_row()

        self.strategy.on_finish(self._context)
        self._events.record(
            self.current_time,
            "RUN_FINISH",
            f"finished paper strategy {self.strategy.name}",
            final_equity=self.equity(),
            trades=len(self.trades),
            working_orders=len(self._gateway.pending_orders),
            gateway="paper",
        )

        equity_curve = pd.DataFrame(self._equity_rows)
        metrics = calculate_metrics(
            equity_curve=equity_curve,
            trades=self.trades,
            initial_cash=self.initial_cash,
        )
        metrics.update(self._gateway.final_account_metrics(equity_curve))
        final_equity = float(equity_curve["equity"].iloc[-1])
        return PaperTradingResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            metrics=metrics,
            equity_curve=equity_curve,
            orders=self.orders,
            trades=self.trades,
            positions=self._gateway.positions,
            events=self._events.events,
            account_mode=self.account_mode,
            futures_positions=self._gateway.futures_positions,
            steps=len(timestamps),
        )

    def submit_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        offset: Offset = Offset.AUTO,
    ) -> Order:
        order = self._gateway.submit_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            offset=offset,
        )
        self.strategy.on_order(self._context, order)
        return order

    def cancel_order(self, order_id: str, reason: str = "canceled by strategy") -> Order | None:
        order = self._gateway.cancel_order(order_id, reason)
        if order is not None:
            self.strategy.on_order(self._context, order)
        return order

    def history(self, symbol: str, limit: int | None = None) -> list[Bar]:
        bars = self._histories.get(symbol, [])
        if limit is None:
            return list(bars)
        return list(bars[-limit:])

    def position(self, symbol: str) -> Position:
        return self._gateway.position(symbol)

    def last_price(self, symbol: str) -> float | None:
        return self._gateway.last_price(symbol)

    def last_tick(self, symbol: str) -> Tick | None:
        return None

    def equity(self) -> float:
        return self._gateway.equity()

    def _record_equity(self, timestamp: datetime) -> None:
        self._equity_rows.append({"timestamp": timestamp, **self._gateway.account_snapshot()})

    def _refresh_last_equity_row(self) -> None:
        if self._equity_rows:
            self._equity_rows[-1].update(self._gateway.account_snapshot())

    def _should_settle(
        self,
        index: int,
        timestamps: list[datetime],
        bars_by_time: dict[datetime, list[Bar]],
    ) -> bool:
        if self.account_mode != "futures" or not self.daily_settlement:
            return False
        current_trading_date = self._trading_date_for_bars(bars_by_time[timestamps[index]])
        if index == len(timestamps) - 1:
            return True
        next_trading_date = self._trading_date_for_bars(bars_by_time[timestamps[index + 1]])
        return current_trading_date != next_trading_date

    def _trading_date_for_bars(self, bars: list[Bar]) -> str:
        if not bars:
            return ""
        raw = bars[0].extra.get("trading_date")
        if raw:
            return str(raw)
        return bars[0].timestamp.date().isoformat()


def run_paper_trading(
    bars: list[Bar],
    strategy: Strategy,
    initial_cash: float = 100_000.0,
    execution_config: ExecutionConfig | None = None,
    risk_manager: RiskManager | None = None,
    account_mode: str = "cash",
    contract_registry: ContractRegistry | None = None,
    daily_settlement: bool = False,
    max_steps: int | None = None,
) -> PaperTradingResult:
    engine = PaperTradingEngine(
        bars=bars,
        strategy=strategy,
        initial_cash=initial_cash,
        execution_config=execution_config,
        risk_manager=risk_manager,
        account_mode=account_mode,
        contract_registry=contract_registry,
        daily_settlement=daily_settlement,
        max_steps=max_steps,
    )
    return engine.run()
