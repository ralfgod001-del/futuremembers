from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import quant_platform.config as config_module
from quant_platform.backtest import BacktestEngine
from quant_platform.data import generate_sample_bars, load_akshare_futures_bars
from quant_platform.ctp import (
    CTP_OFFSET_CLOSE_TODAY,
    CTP_OFFSET_CLOSE_YESTERDAY,
    CTP_OFFSET_OPEN,
    CTP_ORDER_PRICE_TYPE_LIMIT,
    CtpEventQueue,
    CtpConnectionConfig,
    CtpFuturesGateway,
    CtpGatewayError,
    CtpMarketDataSession,
    CtpRequestTimeoutError,
    CtpTradingAccount,
    CtpTradingSession,
    DryRunCtpMarketDataTransport,
    DryRunCtpTransport,
    NativeCtpMarketDataTransport,
    ctp_depth_market_data_to_tick,
    NativeCtpTraderTransport,
    futures_positions_from_ctp,
    order_from_ctp,
    split_order_for_ctp,
    trade_from_ctp,
)
from quant_platform.datacenter import resample_bars, validate_bars
from quant_platform.execution import ExecutionConfig, SymbolExecutionConfig
from quant_platform.events import EventRecorder
from quant_platform.futures import CommissionRule, ContractRegistry, ContractSpec, FuturesPosition
from quant_platform.models import Bar, Offset, Order, OrderStatus, OrderType, Side, Tick
from quant_platform.optimization import run_grid_search
from quant_platform.paper import run_paper_trading
from quant_platform.realtime import CtpRealtimeEngine, TickBarAggregator
from quant_platform.replay import run_market_replay
from quant_platform.risk import RiskConfig, RiskManager, SymbolRiskConfig
from quant_platform.sample_strategies import MovingAverageCrossStrategy
from quant_platform.strategy import Strategy, StrategyContext
from quant_platform.watchdog import CtpSessionWatchdog
from quant_platform.trading_calendar import TradingCalendar, session_template
import quant_platform.webapp as webapp_module
from quant_platform.webapp import (
    load_ctp_monitor,
    load_akshare_bars,
    list_config_files,
    run_akshare_backtest,
    run_quant_task,
)


def make_bar(index: int, open_price: float, close_price: float) -> Bar:
    return Bar(
        symbol="TEST",
        timestamp=datetime(2024, 1, 1) + timedelta(days=index),
        open=open_price,
        high=max(open_price, close_price),
        low=min(open_price, close_price),
        close=close_price,
        volume=100,
    )


class BuyFirstBarStrategy(Strategy):
    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        if len(context.history(bar.symbol)) == 1:
            context.buy(bar.symbol, 2)


class BuyThenSellStrategy(Strategy):
    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        history_length = len(context.history(bar.symbol))
        if history_length == 1:
            context.buy(bar.symbol, 2, offset=Offset.OPEN)
        elif history_length == 2:
            context.sell(bar.symbol, 2, offset=Offset.CLOSE)


class BuyThenCloseTodayStrategy(Strategy):
    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        history_length = len(context.history(bar.symbol))
        if history_length == 1:
            context.buy(bar.symbol, 1, offset=Offset.OPEN)
        elif history_length == 2:
            context.sell(bar.symbol, 1, offset=Offset.CLOSE_TODAY)


class BuyHoldThenCloseYesterdayStrategy(Strategy):
    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        history_length = len(context.history(bar.symbol))
        if history_length == 1:
            context.buy(bar.symbol, 1, offset=Offset.OPEN)
        elif history_length == 3:
            context.sell(bar.symbol, 1, offset=Offset.CLOSE_YESTERDAY)


class SubmitAndCancelLimitStrategy(Strategy):
    def __init__(self) -> None:
        self.order_id: str | None = None

    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        history_length = len(context.history(bar.symbol))
        if history_length == 1:
            order = context.buy(
                bar.symbol,
                1,
                order_type=OrderType.LIMIT,
                limit_price=bar.close - 5,
            )
            self.order_id = order.order_id
        elif history_length == 2 and self.order_id:
            context.engine.cancel_order(self.order_id)


class BuyOnFirstTickStrategy(Strategy):
    name = "buy_on_first_tick"

    def __init__(self, quantity: float = 1.0) -> None:
        self.quantity = quantity
        self.ticks: list[Tick] = []
        self.orders_seen: list[Order] = []
        self.trades_seen: list[Trade] = []
        self.last_tick_from_context: Tick | None = None

    def on_tick(self, context: StrategyContext, tick: Tick) -> None:
        self.ticks.append(tick)
        self.last_tick_from_context = context.last_tick(tick.symbol)
        if len(self.ticks) == 1:
            context.buy(tick.symbol, self.quantity, offset=Offset.OPEN)

    def on_order(self, context: StrategyContext, order: Order) -> None:
        self.orders_seen.append(order)

    def on_trade(self, context: StrategyContext, trade: Trade) -> None:
        self.trades_seen.append(trade)


class RecordRealtimeBarsStrategy(Strategy):
    name = "record_realtime_bars"

    def __init__(self) -> None:
        self.bars: list[Bar] = []
        self.closes_from_context: list[float] = []

    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        self.bars.append(bar)
        self.closes_from_context = context.closes(bar.symbol)


class StatefulRealtimeStrategy(Strategy):
    name = "stateful_realtime"

    def __init__(self) -> None:
        self.tick_count = 0
        self.has_submitted = False
        self.restored = False

    def on_init(self, context: StrategyContext) -> None:
        self.tick_count = 0
        self.has_submitted = False

    def on_tick(self, context: StrategyContext, tick: Tick) -> None:
        self.tick_count += 1
        if not self.has_submitted:
            context.buy(tick.symbol, 1, offset=Offset.OPEN)
            self.has_submitted = True

    def snapshot_state(self):
        return {
            "tick_count": self.tick_count,
            "has_submitted": self.has_submitted,
        }

    def restore_state(self, state):
        self.tick_count = int(state.get("tick_count", 0))
        self.has_submitted = bool(state.get("has_submitted", False))
        self.restored = True


class MigratingRealtimeStrategy(StatefulRealtimeStrategy):
    name = "migrating_realtime"
    state_schema_version = 2

    def __init__(self) -> None:
        super().__init__()
        self.migrated_from: int | None = None

    def migrate_state(self, state, from_version: int):
        self.migrated_from = from_version
        migrated = dict(state)
        if from_version <= 1 and "submitted" in migrated:
            migrated["has_submitted"] = bool(migrated.pop("submitted"))
        return migrated


class QuantPlatformTests(unittest.TestCase):
    def test_order_submitted_on_bar_fills_next_bar_open(self) -> None:
        bars = [
            make_bar(0, 10, 11),
            make_bar(1, 12, 13),
            make_bar(2, 14, 15),
        ]

        result = BacktestEngine(
            bars=bars,
            strategy=BuyFirstBarStrategy(),
            initial_cash=1_000,
            commission_rate=0,
        ).run()

        self.assertEqual(len(result.orders), 1)
        self.assertEqual(result.orders[0].status, OrderStatus.FILLED)
        self.assertEqual(result.orders[0].fill_price, 12)
        self.assertEqual(result.trades[0].timestamp, bars[1].timestamp)
        self.assertEqual(result.positions["TEST"].quantity, 2)
        self.assertEqual(result.final_equity, 1_006)

    def test_symbol_execution_config_controls_fill_costs(self) -> None:
        bars = [
            make_bar(0, 10, 11),
            make_bar(1, 12, 13),
            make_bar(2, 14, 15),
        ]
        execution = ExecutionConfig(
            default=SymbolExecutionConfig(commission_rate=0, slippage=0),
            symbols={
                "TEST": SymbolExecutionConfig(
                    commission_rate=0,
                    slippage=1,
                    min_commission=2,
                )
            },
        )

        result = BacktestEngine(
            bars=bars,
            strategy=BuyFirstBarStrategy(),
            initial_cash=1_000,
            execution_config=execution,
        ).run()

        self.assertEqual(result.orders[0].fill_price, 13)
        self.assertEqual(result.orders[0].commission, 2)
        self.assertEqual(result.final_equity, 1_002)

    def test_grid_search_sorts_by_objective(self) -> None:
        bars = generate_sample_bars(periods=90)

        search = run_grid_search(
            bars=bars,
            strategy_factory=lambda params: MovingAverageCrossStrategy(**params),
            param_grid={"fast_window": [3, 5], "slow_window": [12, 18]},
            base_params={"quantity": 1},
            objective="total_return",
        )

        self.assertEqual(len(search.results), 4)
        self.assertGreaterEqual(
            search.results["total_return"].iloc[0],
            search.results["total_return"].iloc[-1],
        )
        self.assertIn("fast_window", search.best_params)

    def test_risk_manager_rejects_oversized_order(self) -> None:
        bars = [
            make_bar(0, 10, 11),
            make_bar(1, 12, 13),
        ]
        risk = RiskManager(
            RiskConfig(default=SymbolRiskConfig(max_order_quantity=1))
        )

        result = BacktestEngine(
            bars=bars,
            strategy=BuyFirstBarStrategy(),
            initial_cash=1_000,
            risk_manager=risk,
        ).run()

        self.assertEqual(result.orders[0].status, OrderStatus.REJECTED)
        self.assertIn("exceeds max", result.orders[0].reject_reason or "")
        self.assertEqual(len(result.trades), 0)
        self.assertTrue(
            any(event.event_type == "ORDER_REJECTED" for event in result.events)
        )

    def test_replay_records_bar_events_and_limits_steps(self) -> None:
        bars = [
            make_bar(0, 10, 11),
            make_bar(1, 12, 13),
            make_bar(2, 14, 15),
        ]

        replay = run_market_replay(
            bars=bars,
            strategy=BuyFirstBarStrategy(),
            initial_cash=1_000,
            max_steps=2,
        )

        self.assertEqual(replay.steps, 2)
        self.assertTrue(any(event.event_type == "BAR" for event in replay.result.events))
        self.assertEqual(replay.result.orders[0].status, OrderStatus.FILLED)

    def test_paper_trading_fills_market_order_on_current_bar_close(self) -> None:
        bars = [
            make_bar(0, 10, 11),
            make_bar(1, 12, 13),
        ]

        result = run_paper_trading(
            bars=bars,
            strategy=BuyFirstBarStrategy(),
            initial_cash=1_000,
            execution_config=ExecutionConfig.from_legacy(commission_rate=0, slippage=0),
        )

        self.assertEqual(result.steps, 2)
        self.assertEqual(result.orders[0].status, OrderStatus.FILLED)
        self.assertEqual(result.orders[0].fill_price, 11)
        self.assertEqual(result.trades[0].timestamp, bars[0].timestamp)
        self.assertEqual(result.positions["TEST"].quantity, 2)
        self.assertEqual(result.final_equity, 1_004)

    def test_paper_limit_order_can_be_canceled_before_crossing(self) -> None:
        bars = [
            make_bar(0, 10, 11),
            make_bar(1, 12, 13),
            make_bar(2, 14, 15),
        ]

        result = run_paper_trading(
            bars=bars,
            strategy=SubmitAndCancelLimitStrategy(),
            initial_cash=1_000,
            execution_config=ExecutionConfig.from_legacy(commission_rate=0, slippage=0),
        )

        self.assertEqual(len(result.orders), 1)
        self.assertEqual(result.orders[0].status, OrderStatus.CANCELED)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.metrics["working_order_count"], 0)
        self.assertTrue(
            any(event.event_type == "ORDER_CANCELED" for event in result.events)
        )

    def test_webapp_lists_configs_and_runs_backtest(self) -> None:
        workspace = Path.cwd()
        configs = list_config_files(workspace)
        self.assertTrue(any(config["path"] == "examples/ma_cross_config.json" for config in configs))

        report = run_quant_task(
            workspace,
            {
                "mode": "backtest",
                "configPath": "examples/ma_cross_config.json",
            },
        )

        self.assertIn("metrics", report)
        self.assertTrue((workspace / report["outputDir"] / "report.html").exists())

        paper_report = run_quant_task(
            workspace,
            {
                "mode": "paper",
                "configPath": "examples/paper_config.json",
                "maxSteps": 80,
            },
        )

        self.assertEqual(paper_report["mode"], "paper")
        self.assertIn("working_order_count", paper_report["metrics"])
        self.assertTrue((workspace / paper_report["outputDir"] / "report.html").exists())

    def test_webapp_loads_ctp_monitor_from_state_and_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            output = workspace / "output"
            output.mkdir()
            state_path = output / "ctp_state.json"
            event_log_path = output / "events.jsonl"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "saved_at": "2024-01-02T09:30:05",
                        "current_time": "2024-01-02T09:30:05",
                        "orders": [
                            {
                                "order_id": "L1",
                                "symbol": "RB2405",
                                "side": "BUY",
                                "quantity": 1,
                                "submitted_at": "2024-01-02T09:30:00",
                                "order_type": "LIMIT",
                                "offset": "OPEN",
                                "status": "PENDING",
                            },
                            {
                                "order_id": "L2",
                                "symbol": "RB2405",
                                "side": "SELL",
                                "quantity": 1,
                                "submitted_at": "2024-01-02T09:30:01",
                                "order_type": "LIMIT",
                                "offset": "CLOSE",
                                "status": "REJECTED",
                                "reject_reason": "risk check",
                            },
                        ],
                        "trades": [
                            {
                                "trade_id": "T1",
                                "order_id": "L1",
                                "symbol": "RB2405",
                                "side": "BUY",
                                "quantity": 1,
                                "price": 3601,
                                "timestamp": "2024-01-02T09:30:02",
                                "offset": "OPEN",
                                "notional": 36010,
                            }
                        ],
                        "last_ticks": {
                            "RB2405": {
                                "symbol": "RB2405",
                                "timestamp": "2024-01-02T09:30:03",
                                "last_price": 3602,
                            }
                        },
                        "strategy": {
                            "name": "buy_on_first_tick",
                            "class": "tests.BuyOnFirstTickStrategy",
                            "state_schema_version": 1,
                            "state": {"has_submitted": True},
                        },
                        "watchdog": {
                            "trading": {
                                "healthy": True,
                                "state": "ready",
                                "connected": True,
                                "logged_in": True,
                            },
                            "market_data": {
                                "healthy": True,
                                "state": "ready",
                                "connected": True,
                                "logged_in": True,
                                "subscribed_symbols": ["RB2405"],
                            },
                        },
                        "last_reconcile": {"event_type": "RECONCILE_READY"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            event_rows = [
                {"timestamp": "2024-01-02T09:30:00", "event_type": "RUN_START", "severity": "INFO"},
                {"timestamp": "2024-01-02T09:30:01", "event_type": "TICK", "severity": "INFO"},
                {"timestamp": "2024-01-02T09:30:02", "event_type": "ORDER_SUBMITTED", "severity": "INFO"},
            ]
            event_log_path.write_text(
                "\n".join(json.dumps(row) for row in event_rows),
                encoding="utf-8",
            )

            monitor = load_ctp_monitor(
                workspace,
                state_path="output/ctp_state.json",
                event_log_path="output/events.jsonl",
                limit=2,
            )

            self.assertTrue(monitor["stateExists"])
            self.assertTrue(monitor["eventLogExists"])
            self.assertEqual(monitor["summary"]["strategyName"], "buy_on_first_tick")
            self.assertEqual(monitor["summary"]["orderCount"], 2)
            self.assertEqual(monitor["summary"]["workingOrderCount"], 1)
            self.assertEqual(monitor["summary"]["rejectedOrderCount"], 1)
            self.assertEqual(monitor["summary"]["tradeNotional"], 36010)
            self.assertEqual(monitor["summary"]["symbols"], ["RB2405"])
            self.assertEqual(monitor["summary"]["trading"]["healthy"], True)
            self.assertEqual(monitor["summary"]["healthStatus"], "WARN")
            self.assertIn(
                "ORDER_REJECTED",
                {alert["code"] for alert in monitor["summary"]["alerts"]},
            )
            self.assertEqual(len(monitor["events"]), 2)
            self.assertEqual(monitor["events"][0]["event_type"], "TICK")
            self.assertEqual(monitor["events"][1]["event_type"], "ORDER_SUBMITTED")

    def test_webapp_ctp_monitor_reports_missing_files_as_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            monitor = load_ctp_monitor(
                workspace,
                state_path="output/missing_state.json",
                event_log_path="output/missing_events.jsonl",
            )

            self.assertFalse(monitor["stateExists"])
            self.assertFalse(monitor["eventLogExists"])
            self.assertEqual(monitor["summary"]["healthStatus"], "ERROR")
            codes = {alert["code"] for alert in monitor["summary"]["alerts"]}
            self.assertIn("STATE_MISSING", codes)
            self.assertIn("EVENT_LOG_MISSING", codes)

    def test_webapp_ctp_monitor_reports_unhealthy_connection_and_event_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            output = workspace / "output"
            output.mkdir()
            state_path = output / "ctp_state.json"
            event_log_path = output / "events.jsonl"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "saved_at": datetime.now().isoformat(),
                        "orders": [],
                        "trades": [],
                        "watchdog": {
                            "trading": {
                                "healthy": False,
                                "state": "front_disconnected",
                                "connected": False,
                                "front_connected": False,
                                "logged_in": False,
                                "last_disconnect_reason": 4097,
                            },
                            "market_data": {
                                "healthy": True,
                                "state": "ready",
                                "connected": True,
                                "front_connected": True,
                                "logged_in": True,
                                "subscribed_symbols": [],
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            event_log_path.write_text(
                json.dumps(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "event_type": "WATCHDOG_TRADING_GIVE_UP",
                        "severity": "INFO",
                        "message": "trading recovery attempt limit reached",
                    }
                ),
                encoding="utf-8",
            )

            monitor = load_ctp_monitor(
                workspace,
                state_path="output/ctp_state.json",
                event_log_path="output/events.jsonl",
                stale_seconds=3600,
            )

            self.assertEqual(monitor["summary"]["healthStatus"], "ERROR")
            codes = {alert["code"] for alert in monitor["summary"]["alerts"]}
            self.assertIn("TRADING_UNHEALTHY", codes)
            self.assertIn("WATCHDOG_TRADING_GIVE_UP", codes)
            self.assertIn("MARKET_DATA_NO_SUBSCRIPTIONS", codes)

    def test_akshare_futures_daily_data_maps_to_bars(self) -> None:
        class FakeAkShare:
            @staticmethod
            def futures_zh_daily_sina(symbol: str):
                self.assertEqual(symbol, "RB0")
                return pd.DataFrame(
                    [
                        {
                            "date": "2024-01-02",
                            "open": "4005",
                            "high": "4058",
                            "low": "3983",
                            "close": "4047",
                            "volume": "970,394",
                            "hold": 1541082,
                            "settle": 4036,
                        },
                        {
                            "date": "2024-01-03",
                            "open": "4048",
                            "high": "4072",
                            "low": "4040",
                            "close": "4055",
                            "volume": 964192,
                            "hold": 1583796,
                            "settle": 4055,
                        },
                    ]
                )

        bars = load_akshare_futures_bars(
            "RB0",
            start_date="2024-01-03",
            end_date="2024-01-03",
            ak_module=FakeAkShare,
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "RB0")
        self.assertEqual(bars[0].timestamp, datetime(2024, 1, 3))
        self.assertEqual(bars[0].open, 4048)
        self.assertEqual(bars[0].close, 4055)
        self.assertEqual(bars[0].volume, 964192)
        self.assertEqual(bars[0].extra["open_interest"], 1583796)
        self.assertEqual(bars[0].extra["settle"], 4055)

    def test_akshare_config_source_can_be_loaded_without_csv_path(self) -> None:
        original_loader = config_module.load_akshare_futures_bars
        try:
            config_module.load_akshare_futures_bars = lambda **kwargs: [
                Bar(
                    symbol=kwargs["output_symbol"] or kwargs["symbol"],
                    timestamp=datetime(2024, 1, 2),
                    open=4000,
                    high=4010,
                    low=3990,
                    close=4005,
                    volume=100,
                )
            ]

            bars = config_module.load_bars_from_sources(
                [
                    {
                        "provider": "akshare",
                        "symbol": "RB0",
                        "output_symbol": "RB0",
                        "start_date": "20240102",
                        "end_date": "20240102",
                    }
                ]
            )
        finally:
            config_module.load_akshare_futures_bars = original_loader

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "RB0")

    def test_webapp_runs_akshare_backtest_and_caches_bars(self) -> None:
        original_loader = webapp_module.load_akshare_futures_bars
        try:
            webapp_module.load_akshare_futures_bars = lambda **kwargs: [
                Bar(
                    symbol=kwargs["output_symbol"] or kwargs["symbol"],
                    timestamp=datetime(2024, 1, 1) + timedelta(days=index),
                    open=100 + index,
                    high=102 + index,
                    low=99 + index,
                    close=101 + index,
                    volume=100,
                )
                for index in range(30)
            ]
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace = Path(tmpdir)
                report = run_akshare_backtest(
                    workspace,
                    {
                        "symbol": "RB0",
                        "api": "futures_zh_daily_sina",
                        "startDate": "20240101",
                        "endDate": "20240131",
                        "fastWindow": 3,
                        "slowWindow": 8,
                        "quantity": 1,
                        "cash": 200000,
                        "multiplier": 20,
                        "marginRate": 0.15,
                        "commissionRate": 0.0001,
                        "slippage": 1,
                        "exchange": "SHFE",
                        "market": "SHFE",
                        "period": "daily",
                    },
                )

                self.assertEqual(report["dataSource"], "akshare")
                self.assertEqual(report["bars"], 30)
                self.assertEqual(report["cash"], 200000)
                self.assertEqual(report["multiplier"], 20)
                self.assertEqual(report["marginRate"], 0.15)
                self.assertEqual(report["commissionRate"], 0.0001)
                self.assertEqual(report["slippage"], 1)
                self.assertEqual(report["exchange"], "SHFE")
                self.assertIn("metrics", report)
                self.assertTrue((workspace / report["outputDir"] / "report.html").exists())
                self.assertTrue((workspace / report["cachePath"]).exists())
        finally:
            webapp_module.load_akshare_futures_bars = original_loader

    def test_webapp_loads_akshare_bars_for_kline_chart(self) -> None:
        original_loader = webapp_module.load_akshare_futures_bars
        try:
            webapp_module.load_akshare_futures_bars = lambda **kwargs: [
                Bar(
                    symbol=kwargs["output_symbol"] or kwargs["symbol"],
                    timestamp=datetime(2024, 1, 1) + timedelta(days=index),
                    open=100 + index,
                    high=103 + index,
                    low=99 + index,
                    close=102 + index,
                    volume=1000 + index,
                )
                for index in range(5)
            ]
            with tempfile.TemporaryDirectory() as tmpdir:
                payload = load_akshare_bars(
                    Path(tmpdir),
                    {
                        "symbol": ["RB0"],
                        "api": ["futures_zh_daily_sina"],
                        "startDate": ["20240101"],
                        "endDate": ["20240131"],
                        "market": ["SHFE"],
                        "period": ["daily"],
                        "limit": ["3"],
                    },
                )

                self.assertEqual(payload["dataSource"], "akshare")
                self.assertEqual(payload["symbol"], "RB0")
                self.assertEqual(payload["count"], 3)
                self.assertEqual(payload["totalCount"], 5)
                self.assertEqual(payload["bars"][0]["timestamp"], "2024-01-03T00:00:00")
                self.assertEqual(payload["bars"][-1]["close"], 106)
        finally:
            webapp_module.load_akshare_futures_bars = original_loader

    def test_webapp_static_ui_uses_simplified_chinese_labels(self) -> None:
        root = Path.cwd()
        index_text = (root / "quant_platform" / "web_assets" / "index.html").read_text(
            encoding="utf-8"
        )
        script_text = (root / "quant_platform" / "web_assets" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('lang="zh-CN"', index_text)
        self.assertIn("量化工作台", index_text)
        self.assertIn("期货量化工作台", index_text)
        self.assertIn("选择合约", index_text)
        self.assertIn("策略回测", index_text)
        self.assertIn("运行 AkShare", index_text)
        self.assertIn("运行 AkShare 回测", index_text)
        self.assertIn("CTP 监控", index_text)
        self.assertIn("初始资金", index_text)
        self.assertIn("合约乘数", index_text)
        self.assertIn("手续费率", index_text)
        self.assertIn("自选列表", index_text)
        self.assertIn("K线图", index_text)
        self.assertIn("K线指标", index_text)
        self.assertIn("MA1", index_text)
        self.assertIn("kline-pan-left-button", index_text)
        self.assertIn("kline-zoom-in-button", index_text)
        self.assertIn("kline-latest-button", index_text)
        self.assertIn("最终权益", script_text)
        self.assertIn("暂无挂单", script_text)
        self.assertIn("当前参数没有返回K线", script_text)
        self.assertIn("movingAverage", script_text)
        self.assertIn("handleKlineMouseMove", script_text)
        self.assertIn("panKline", script_text)
        self.assertIn("zoomKline", script_text)

    def test_futures_account_marks_margin_and_unrealized_pnl(self) -> None:
        bars = [
            make_bar(0, 100, 100),
            make_bar(1, 110, 112),
            make_bar(2, 115, 120),
        ]
        contracts = ContractRegistry(
            contracts={
                "TEST": ContractSpec(
                    symbol="TEST",
                    multiplier=10,
                    margin_rate=0.1,
                    commission=CommissionRule(per_contract=1),
                )
            }
        )

        result = BacktestEngine(
            bars=bars,
            strategy=BuyFirstBarStrategy(),
            initial_cash=100_000,
            account_mode="futures",
            contract_registry=contracts,
        ).run()

        self.assertEqual(result.trades[0].notional, 2200)
        self.assertEqual(result.trades[0].commission, 2)
        self.assertEqual(result.futures_positions["TEST"].long_quantity, 2)
        self.assertEqual(result.final_equity, 100_198)
        self.assertEqual(result.metrics["final_margin"], 240)
        self.assertEqual(result.metrics["final_unrealized_pnl"], 200)

    def test_futures_close_realizes_multiplier_adjusted_pnl(self) -> None:
        bars = [
            make_bar(0, 100, 100),
            make_bar(1, 110, 112),
            make_bar(2, 115, 120),
        ]
        contracts = ContractRegistry(
            contracts={
                "TEST": ContractSpec(
                    symbol="TEST",
                    multiplier=10,
                    margin_rate=0.1,
                    commission=CommissionRule(per_contract=1),
                )
            }
        )

        result = BacktestEngine(
            bars=bars,
            strategy=BuyThenSellStrategy(),
            initial_cash=100_000,
            account_mode="futures",
            contract_registry=contracts,
        ).run()

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[1].realized_pnl, 98)
        self.assertEqual(result.futures_positions["TEST"].long_quantity, 0)
        self.assertEqual(result.final_equity, 100_096)
        self.assertEqual(result.metrics["final_margin"], 0)

    def test_close_today_uses_close_today_commission_rule(self) -> None:
        bars = [
            make_bar(0, 100, 100),
            make_bar(1, 110, 112),
            make_bar(2, 115, 115),
        ]
        contracts = ContractRegistry(
            contracts={
                "TEST": ContractSpec(
                    symbol="TEST",
                    multiplier=10,
                    margin_rate=0.1,
                    commission=CommissionRule(
                        per_contract=1,
                        close_today_per_contract=5,
                    ),
                )
            }
        )

        result = BacktestEngine(
            bars=bars,
            strategy=BuyThenCloseTodayStrategy(),
            initial_cash=100_000,
            account_mode="futures",
            contract_registry=contracts,
        ).run()

        self.assertEqual(result.trades[0].commission, 1)
        self.assertEqual(result.trades[1].commission, 5)
        self.assertEqual(result.trades[1].realized_pnl, 45)
        self.assertEqual(result.final_equity, 100_044)
        self.assertEqual(result.futures_positions["TEST"].long_today_quantity, 0)

    def test_daily_settlement_moves_today_to_yesterday_and_resets_cost(self) -> None:
        bars = [
            make_bar(0, 100, 100),
            make_bar(1, 110, 112),
            make_bar(2, 115, 120),
            make_bar(3, 118, 118),
        ]
        contracts = ContractRegistry(
            contracts={
                "TEST": ContractSpec(
                    symbol="TEST",
                    multiplier=10,
                    margin_rate=0.1,
                    commission=CommissionRule(per_contract=1),
                )
            }
        )

        result = BacktestEngine(
            bars=bars,
            strategy=BuyHoldThenCloseYesterdayStrategy(),
            initial_cash=100_000,
            account_mode="futures",
            contract_registry=contracts,
            daily_settlement=True,
        ).run()

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[1].offset, Offset.CLOSE_YESTERDAY)
        self.assertEqual(result.trades[1].realized_pnl, -21)
        self.assertEqual(result.metrics["final_settlement_pnl"], 100)
        self.assertEqual(result.final_equity, 100_078)
        self.assertEqual(result.futures_positions["TEST"].long_yesterday_quantity, 0)
        self.assertTrue(
            any(event.event_type == "DAILY_SETTLEMENT" for event in result.events)
        )

    def test_ctp_order_insert_maps_local_order_fields(self) -> None:
        gateway = CtpFuturesGateway(
            config=CtpConnectionConfig(broker_id="9999", investor_id="1001"),
            contract_registry=ContractRegistry(
                contracts={
                    "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)
                }
            ),
        )
        order = Order(
            order_id="LOCAL1",
            symbol="TEST",
            side=Side.BUY,
            quantity=2,
            submitted_at=datetime(2024, 1, 2, 9, 30),
            order_type=OrderType.LIMIT,
            limit_price=3600,
            offset=Offset.OPEN,
        )

        request = gateway.create_order_insert_requests(order)[0]

        self.assertEqual(request.field["BrokerID"], "9999")
        self.assertEqual(request.field["InvestorID"], "1001")
        self.assertEqual(request.field["InstrumentID"], "TEST")
        self.assertEqual(request.field["Direction"], "0")
        self.assertEqual(request.field["CombOffsetFlag"], CTP_OFFSET_OPEN)
        self.assertEqual(request.field["OrderPriceType"], CTP_ORDER_PRICE_TYPE_LIMIT)
        self.assertEqual(request.field["LimitPrice"], 3600)
        self.assertEqual(request.field["VolumeTotalOriginal"], 2)
        self.assertEqual(request.order_ref, "000000000001")

    def test_ctp_auto_offset_splits_shfe_close_today_yesterday_and_open(self) -> None:
        position = FuturesPosition(
            symbol="TEST",
            long_today_quantity=2,
            long_today_avg_price=100,
            long_yesterday_quantity=1,
            long_yesterday_avg_price=98,
        )
        order = Order(
            order_id="LOCAL2",
            symbol="TEST",
            side=Side.SELL,
            quantity=5,
            submitted_at=datetime(2024, 1, 2, 9, 30),
            order_type=OrderType.LIMIT,
            limit_price=101,
            offset=Offset.AUTO,
        )
        spec = ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)

        instructions = split_order_for_ctp(order, spec, position)

        self.assertEqual([item.offset for item in instructions], [
            Offset.CLOSE_TODAY,
            Offset.CLOSE_YESTERDAY,
            Offset.OPEN,
        ])
        self.assertEqual([item.quantity for item in instructions], [2, 1, 2])

        gateway = CtpFuturesGateway(
            config=CtpConnectionConfig(broker_id="9999", investor_id="1001"),
            contract_registry=ContractRegistry(contracts={"TEST": spec}),
        )
        requests = gateway.create_order_insert_requests(order, position)
        self.assertEqual(
            [request.field["CombOffsetFlag"] for request in requests],
            [CTP_OFFSET_CLOSE_TODAY, CTP_OFFSET_CLOSE_YESTERDAY, CTP_OFFSET_OPEN],
        )

    def test_ctp_position_account_order_and_trade_callbacks_map_to_local_models(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(
                    symbol="TEST",
                    exchange="SHFE",
                    multiplier=10,
                    margin_rate=0.12,
                )
            }
        )
        positions = futures_positions_from_ctp(
            [
                {
                    "InstrumentID": "TEST",
                    "PosiDirection": "2",
                    "PositionDate": "1",
                    "Position": 2,
                    "PositionCost": 2000,
                },
                {
                    "InstrumentID": "TEST",
                    "PosiDirection": "3",
                    "PositionDate": "2",
                    "Position": 1,
                    "PositionCost": 1200,
                },
            ],
            registry,
        )

        self.assertEqual(positions["TEST"].long_today_quantity, 2)
        self.assertEqual(positions["TEST"].long_today_avg_price, 100)
        self.assertEqual(positions["TEST"].short_yesterday_quantity, 1)
        self.assertEqual(positions["TEST"].short_yesterday_avg_price, 120)

        account = CtpTradingAccount.from_ctp(
            {"Balance": 100000, "Available": 98000, "CurrMargin": 2000, "PositionProfit": 300}
        )
        self.assertEqual(account.to_snapshot()["risk_ratio"], 0.02)
        self.assertEqual(account.to_snapshot()["unrealized_pnl"], 300)

        order = order_from_ctp(
            {
                "OrderRef": "000000000001",
                "InstrumentID": "TEST",
                "Direction": "0",
                "CombOffsetFlag": "0",
                "OrderPriceType": "2",
                "LimitPrice": 100,
                "VolumeTotalOriginal": 2,
                "VolumeTraded": 2,
                "OrderStatus": "0",
                "InsertDate": "20240102",
                "InsertTime": "09:30:00",
            }
        )
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.side, Side.BUY)
        self.assertEqual(order.offset, Offset.OPEN)

        trade = trade_from_ctp(
            {
                "TradeID": "T1",
                "OrderRef": "000000000001",
                "InstrumentID": "TEST",
                "Direction": "1",
                "OffsetFlag": "3",
                "Price": 101,
                "Volume": 2,
                "TradeDate": "20240102",
                "TradeTime": "09:31:00",
            },
            registry.for_symbol("TEST"),
        )
        self.assertEqual(trade.side, Side.SELL)
        self.assertEqual(trade.offset, Offset.CLOSE_TODAY)
        self.assertEqual(trade.notional, 2020)
        self.assertAlmostEqual(trade.margin, 242.4)

    def test_ctp_session_dry_run_lifecycle_syncs_account_and_positions(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)
            }
        )
        transport = DryRunCtpTransport(
            account_response={
                "Balance": 100000,
                "Available": 99000,
                "CurrMargin": 1000,
                "CurrencyID": "CNY",
            },
            position_responses=[
                {
                    "InstrumentID": "TEST",
                    "PosiDirection": "2",
                    "PositionDate": "1",
                    "Position": 1,
                    "PositionCost": 1000,
                }
            ],
        )
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "user_id": "1001",
                "password": "secret",
                "auth_code": "auth-secret",
                "front": "tcp://127.0.0.1:41205",
            },
            contract_registry=registry,
            dry_run=True,
            transport=transport,
        )

        session.start()
        snapshot = session.snapshot()

        self.assertEqual(session.state, "settlement_confirmed")
        self.assertEqual(snapshot["account"]["available"], 99000)
        self.assertEqual(session.gateway.positions["TEST"].long_today_quantity, 1)
        self.assertEqual(
            [event.event_type for event in session.events],
            [
                "CONNECT",
                "AUTHENTICATE",
                "RSP_AUTHENTICATE_READY",
                "LOGIN",
                "RSP_USER_LOGIN_READY",
                "SETTLEMENT_CONFIRM",
                "RSP_SETTLEMENT_INFO_CONFIRM_READY",
                "QUERY_ACCOUNT",
                "QUERY_POSITIONS",
            ],
        )
        self.assertEqual(transport.calls[1]["field"]["AuthCode"], "***")
        self.assertEqual(transport.calls[2]["field"]["Password"], "***")

    def test_ctp_session_reconcile_queries_orders_and_trades(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)
            }
        )
        transport = DryRunCtpTransport(
            account_response={
                "Balance": 100000,
                "Available": 98000,
                "CurrMargin": 2000,
                "CurrencyID": "CNY",
            },
            position_responses=[
                {
                    "InstrumentID": "TEST",
                    "PosiDirection": "2",
                    "PositionDate": "1",
                    "Position": 1,
                    "PositionCost": 1000,
                }
            ],
            order_responses=[
                {
                    "OrderRef": "000000000021",
                    "InstrumentID": "TEST",
                    "Direction": "0",
                    "CombOffsetFlag": "0",
                    "OrderPriceType": "2",
                    "LimitPrice": 101,
                    "VolumeTotalOriginal": 1,
                    "VolumeTraded": 1,
                    "OrderStatus": "0",
                    "InsertDate": "20240102",
                    "InsertTime": "09:30:00",
                }
            ],
            trade_responses=[
                {
                    "TradeID": "TR21",
                    "OrderRef": "000000000021",
                    "InstrumentID": "TEST",
                    "Direction": "0",
                    "OffsetFlag": "0",
                    "Price": 101,
                    "Volume": 1,
                    "TradeDate": "20240102",
                    "TradeTime": "09:30:01",
                }
            ],
        )
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
            dry_run=True,
            transport=transport,
        )
        session.start(
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )

        ok = session.reconcile()

        self.assertTrue(ok)
        self.assertEqual(session.gateway.account.available, 98000)
        self.assertEqual(session.gateway.positions["TEST"].long_today_quantity, 1)
        self.assertEqual(session.gateway.orders["000000000021"].status, OrderStatus.FILLED)
        self.assertEqual(session.gateway.trades["TR21"].notional, 1010)
        self.assertEqual(
            [call["action"] for call in transport.calls[-4:]],
            ["query_account", "query_positions", "query_orders", "query_trades"],
        )
        event_types = [event.event_type for event in session.events]
        self.assertIn("RECONCILE_START", event_types)
        self.assertIn("QUERY_ORDERS", event_types)
        self.assertIn("QUERY_TRADES", event_types)
        self.assertIn("RECONCILE_READY", event_types)

    def test_ctp_session_reconcile_can_filter_orders_and_trades(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10),
                "OTHER": ContractSpec(symbol="OTHER", exchange="CFFEX", multiplier=300),
            }
        )
        transport = DryRunCtpTransport(
            order_responses=[
                {
                    "OrderRef": "000000000031",
                    "InstrumentID": "TEST",
                    "Direction": "0",
                    "CombOffsetFlag": "0",
                    "OrderPriceType": "2",
                    "LimitPrice": 101,
                    "VolumeTotalOriginal": 1,
                    "VolumeTraded": 1,
                    "OrderStatus": "0",
                    "InsertDate": "20240102",
                    "InsertTime": "09:31:00",
                },
                {
                    "OrderRef": "000000000032",
                    "InstrumentID": "OTHER",
                    "Direction": "0",
                    "CombOffsetFlag": "0",
                    "OrderPriceType": "2",
                    "LimitPrice": 4000,
                    "VolumeTotalOriginal": 1,
                    "VolumeTraded": 1,
                    "OrderStatus": "0",
                    "InsertDate": "20240102",
                    "InsertTime": "09:31:00",
                },
                {
                    "OrderRef": "000000000033",
                    "InstrumentID": "TEST",
                    "Direction": "0",
                    "CombOffsetFlag": "0",
                    "OrderPriceType": "2",
                    "LimitPrice": 102,
                    "VolumeTotalOriginal": 1,
                    "VolumeTraded": 1,
                    "OrderStatus": "0",
                    "InsertDate": "20240102",
                    "InsertTime": "10:01:00",
                },
            ],
            trade_responses=[
                {
                    "TradeID": "TR31",
                    "OrderRef": "000000000031",
                    "InstrumentID": "TEST",
                    "Direction": "0",
                    "OffsetFlag": "0",
                    "Price": 101,
                    "Volume": 1,
                    "TradeDate": "20240102",
                    "TradeTime": "09:31:01",
                },
                {
                    "TradeID": "TR32",
                    "OrderRef": "000000000032",
                    "InstrumentID": "OTHER",
                    "Direction": "0",
                    "OffsetFlag": "0",
                    "Price": 4000,
                    "Volume": 1,
                    "TradeDate": "20240102",
                    "TradeTime": "09:31:01",
                },
                {
                    "TradeID": "TR33",
                    "OrderRef": "000000000033",
                    "InstrumentID": "TEST",
                    "Direction": "0",
                    "OffsetFlag": "0",
                    "Price": 102,
                    "Volume": 1,
                    "TradeDate": "20240102",
                    "TradeTime": "10:01:01",
                },
            ],
        )
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
            dry_run=True,
            transport=transport,
        )
        session.start(
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )

        ok = session.reconcile(
            query_account=False,
            query_positions=False,
            query_orders=True,
            query_trades=True,
            symbols=["TEST"],
            start_time="09:30:00",
            end_time="10:00:00",
        )

        self.assertTrue(ok)
        self.assertEqual(list(session.gateway.orders), ["000000000031"])
        self.assertEqual(list(session.gateway.trades), ["TR31"])
        self.assertEqual(transport.calls[-2]["field"]["InstrumentID"], "TEST")
        self.assertEqual(transport.calls[-2]["field"]["InsertTimeStart"], "09:30:00")
        self.assertEqual(transport.calls[-2]["field"]["InsertTimeEnd"], "10:00:00")
        self.assertEqual(transport.calls[-1]["field"]["TradeTimeStart"], "09:30:00")
        self.assertEqual(transport.calls[-1]["field"]["TradeTimeEnd"], "10:00:00")

    def test_ctp_session_can_submit_order_through_transport(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)
            }
        )
        transport = DryRunCtpTransport()
        session = CtpTradingSession.from_mapping(
            {"broker_id": "9999", "investor_id": "1001", "auth_required": False},
            contract_registry=registry,
            dry_run=True,
            transport=transport,
        )
        session.start(authenticate=False, query_account=False, query_positions=False)
        order = Order(
            order_id="LOCAL3",
            symbol="TEST",
            side=Side.BUY,
            quantity=1,
            submitted_at=datetime(2024, 1, 2, 9, 30),
            order_type=OrderType.LIMIT,
            limit_price=100,
            offset=Offset.OPEN,
        )

        requests = session.submit_order(order)

        self.assertEqual(len(requests), 1)
        self.assertEqual(transport.calls[-1]["action"], "req_order_insert")
        self.assertEqual(transport.calls[-1]["field"]["InstrumentID"], "TEST")
        self.assertEqual(session.events[-1].event_type, "ORDER_INSERT")

    def test_ctp_callback_adapter_updates_gateway_models_and_queue(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(
                    symbol="TEST",
                    exchange="SHFE",
                    multiplier=10,
                    margin_rate=0.1,
                )
            }
        )
        session = CtpTradingSession.from_mapping(
            {"broker_id": "9999", "investor_id": "1001"},
            contract_registry=registry,
            dry_run=True,
            transport=DryRunCtpTransport(),
        )
        adapter = session.callback_adapter

        adapter.on_rsp_user_login(
            {"BrokerID": "9999", "UserID": "1001", "TradingDay": "20240102"},
            {"ErrorID": 0, "ErrorMsg": ""},
            1,
            True,
        )
        adapter.on_rsp_qry_trading_account(
            {"Balance": 100000, "Available": 98000, "CurrMargin": 2000},
            {"ErrorID": 0, "ErrorMsg": ""},
            2,
            True,
        )
        adapter.on_rsp_qry_investor_position(
            {
                "InstrumentID": "TEST",
                "PosiDirection": "2",
                "PositionDate": "1",
                "Position": 1,
                "PositionCost": 1000,
            },
            {"ErrorID": 0, "ErrorMsg": ""},
            3,
            True,
        )
        adapter.on_rtn_order(
            {
                "OrderRef": "000000000011",
                "InstrumentID": "TEST",
                "Direction": "0",
                "CombOffsetFlag": "0",
                "OrderPriceType": "2",
                "LimitPrice": 100,
                "VolumeTotalOriginal": 1,
                "VolumeTraded": 1,
                "OrderStatus": "0",
                "InsertDate": "20240102",
                "InsertTime": "09:30:00",
            }
        )
        adapter.on_rtn_trade(
            {
                "TradeID": "TRADE11",
                "OrderRef": "000000000011",
                "InstrumentID": "TEST",
                "Direction": "0",
                "OffsetFlag": "0",
                "Price": 100,
                "Volume": 1,
                "TradeDate": "20240102",
                "TradeTime": "09:30:01",
            }
        )

        self.assertTrue(session.logged_in)
        self.assertEqual(session.gateway.account.available, 98000)
        self.assertEqual(session.gateway.positions["TEST"].long_today_quantity, 1)
        self.assertEqual(session.gateway.orders["000000000011"].status, OrderStatus.FILLED)
        self.assertEqual(session.gateway.trades["TRADE11"].notional, 1000)
        self.assertIsNotNone(session.callback_queue.wait_for("RTN_ORDER"))
        event_types = [event.event_type for event in session.callback_queue.snapshot()]
        self.assertIn("RSP_USER_LOGIN", event_types)
        self.assertIn("RTN_TRADE", event_types)

    def test_ctp_callback_error_event_does_not_mark_login(self) -> None:
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auto_recover_on_front_connected": False,
            },
            dry_run=True,
            transport=DryRunCtpTransport(),
        )

        session.callback_adapter.on_rsp_user_login(
            {},
            {"ErrorID": 7, "ErrorMsg": "login failed"},
            9,
            True,
        )
        event = session.callback_queue.wait_for("RSP_USER_LOGIN", request_id=9)

        self.assertFalse(session.logged_in)
        self.assertIsNotNone(event)
        self.assertFalse(event.ok)
        self.assertEqual(event.message, "login failed")

    def test_ctp_trading_front_disconnect_updates_session_health(self) -> None:
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auto_recover_on_front_connected": False,
            },
            dry_run=True,
            transport=DryRunCtpTransport(),
        )
        session.start(query_account=False, query_positions=False)

        session.callback_adapter.OnFrontDisconnected(4097)

        self.assertEqual(session.state, "disconnected")
        self.assertFalse(session.connected)
        self.assertFalse(session.logged_in)
        self.assertEqual(session.last_disconnect_reason, 4097)
        self.assertEqual(session.events[-1].event_type, "FRONT_DISCONNECTED")
        self.assertIn(
            "FRONT_DISCONNECTED",
            [event.event_type for event in session.callback_queue.snapshot()],
        )

        session.callback_adapter.OnFrontConnected()

        self.assertTrue(session.connected)
        self.assertTrue(session.front_connected)
        self.assertIsNone(session.last_disconnect_reason)
        self.assertEqual(session.events[-1].event_type, "FRONT_CONNECTED")

    def test_ctp_trading_front_reconnect_auto_recovers_lifecycle_and_queries(self) -> None:
        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)
            }
        )
        transport = DryRunCtpTransport(
            account_response={
                "Balance": 100000,
                "Available": 88000,
                "CurrMargin": 12000,
                "CurrencyID": "CNY",
            },
            position_responses=[
                {
                    "InstrumentID": "TEST",
                    "PosiDirection": "2",
                    "PositionDate": "1",
                    "Position": 1,
                    "PositionCost": 1000,
                }
            ],
        )
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "lifecycle_timeout": 0.2,
                "query_timeout": 0.2,
            },
            contract_registry=registry,
            dry_run=True,
            transport=transport,
        )
        session.start()
        original_call_count = len(transport.calls)

        session.callback_adapter.OnFrontDisconnected(4097)
        session.callback_adapter.OnFrontConnected()

        self.assertEqual(session.state, "settlement_confirmed")
        self.assertTrue(session.logged_in)
        self.assertEqual(session.gateway.account.available, 88000)
        self.assertEqual(session.gateway.positions["TEST"].long_today_quantity, 1)
        new_actions = [call["action"] for call in transport.calls[original_call_count:]]
        self.assertEqual(
            new_actions,
            [
                "authenticate",
                "login",
                "confirm_settlement",
                "query_account",
                "query_positions",
            ],
        )
        event_types = [event.event_type for event in session.events]
        self.assertIn("AUTO_RECOVER_START", event_types)
        self.assertIn("AUTO_RECOVER_READY", event_types)

    def test_ctp_event_queue_can_drain_by_type(self) -> None:
        queue = CtpEventQueue()
        session = CtpTradingSession.from_mapping(
            {"broker_id": "9999", "investor_id": "1001"},
            dry_run=True,
            transport=DryRunCtpTransport(),
        )
        adapter = session.callback_adapter
        adapter.event_queue = queue

        adapter.on_rsp_error({"ErrorID": 1, "ErrorMsg": "bad request"}, 1, True)
        adapter.on_rsp_error({"ErrorID": 2, "ErrorMsg": "bad action"}, 2, True)

        self.assertEqual(len(queue.drain("RSP_ERROR")), 2)
        self.assertEqual(queue.snapshot(), [])

    def test_ctp_session_waits_for_async_account_and_position_queries(self) -> None:
        class AsyncQueryTransport(DryRunCtpTransport):
            def query_account(self, field, request_id: int):
                self._record("query_account", field, request_id)
                self.callback_adapter.on_rsp_qry_trading_account(
                    {
                        "Balance": 200000,
                        "Available": 180000,
                        "CurrMargin": 20000,
                    },
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

            def query_positions(self, field, request_id: int):
                self._record("query_positions", field, request_id)
                self.callback_adapter.on_rsp_qry_investor_position(
                    {
                        "InstrumentID": "TEST",
                        "PosiDirection": "2",
                        "PositionDate": "2",
                        "Position": 1,
                        "PositionCost": 1000,
                    },
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    False,
                )
                self.callback_adapter.on_rsp_qry_investor_position(
                    {
                        "InstrumentID": "TEST",
                        "PosiDirection": "3",
                        "PositionDate": "1",
                        "Position": 2,
                        "PositionCost": 2200,
                    },
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

        registry = ContractRegistry(
            contracts={
                "TEST": ContractSpec(symbol="TEST", exchange="SHFE", multiplier=10)
            }
        )
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
                "query_timeout": 0.2,
            },
            contract_registry=registry,
            dry_run=True,
            transport=AsyncQueryTransport(),
        )

        session.start(authenticate=False, confirm_settlement=False)

        self.assertEqual(session.gateway.account.available, 180000)
        self.assertEqual(session.gateway.positions["TEST"].long_yesterday_quantity, 1)
        self.assertEqual(session.gateway.positions["TEST"].short_today_quantity, 2)
        event_types = [event.event_type for event in session.events]
        self.assertIn("RSP_QRY_TRADING_ACCOUNT_READY", event_types)
        self.assertIn("RSP_QRY_INVESTOR_POSITION_READY", event_types)

    def test_ctp_session_query_timeout_is_reported(self) -> None:
        class SilentAsyncQueryTransport(DryRunCtpTransport):
            def query_account(self, field, request_id: int):
                self._record("query_account", field, request_id)
                return 0

        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
                "query_timeout": 0.01,
            },
            dry_run=True,
            transport=SilentAsyncQueryTransport(),
        )

        with self.assertRaises(CtpRequestTimeoutError):
            session.start(
                authenticate=False,
                confirm_settlement=False,
                query_account=True,
                query_positions=False,
            )

        self.assertEqual(session.events[-1].event_type, "RSP_QRY_TRADING_ACCOUNT_TIMEOUT")

    def test_ctp_session_query_callback_error_is_reported(self) -> None:
        class ErrorAsyncQueryTransport(DryRunCtpTransport):
            def query_account(self, field, request_id: int):
                self._record("query_account", field, request_id)
                self.callback_adapter.on_rsp_qry_trading_account(
                    {},
                    {"ErrorID": 31, "ErrorMsg": "account query rejected"},
                    request_id,
                    True,
                )
                return 0

        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
                "query_timeout": 0.2,
            },
            dry_run=True,
            transport=ErrorAsyncQueryTransport(),
        )

        with self.assertRaises(CtpGatewayError):
            session.start(
                authenticate=False,
                confirm_settlement=False,
                query_account=True,
                query_positions=False,
            )

        self.assertEqual(session.events[-1].event_type, "RSP_QRY_TRADING_ACCOUNT_ERROR")
        self.assertEqual(session.events[-1].message, "account query rejected")

    def test_ctp_session_waits_for_lifecycle_callbacks(self) -> None:
        class AsyncLifecycleTransport(DryRunCtpTransport):
            def authenticate(self, field, request_id: int):
                self._record("authenticate", field, request_id)
                self.callback_adapter.on_rsp_authenticate(
                    field,
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

            def login(self, field, request_id: int):
                self._record("login", field, request_id)
                self.callback_adapter.on_rsp_user_login(
                    field,
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

            def confirm_settlement(self, field, request_id: int):
                self._record("confirm_settlement", field, request_id)
                self.callback_adapter.on_rsp_settlement_info_confirm(
                    field,
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "lifecycle_timeout": 0.2,
            },
            dry_run=True,
            transport=AsyncLifecycleTransport(),
        )

        session.start(query_account=False, query_positions=False)

        self.assertTrue(session.authenticated)
        self.assertTrue(session.logged_in)
        self.assertTrue(session.settlement_confirmed)
        event_types = [event.event_type for event in session.events]
        self.assertIn("RSP_AUTHENTICATE_READY", event_types)
        self.assertIn("RSP_USER_LOGIN_READY", event_types)
        self.assertIn("RSP_SETTLEMENT_INFO_CONFIRM_READY", event_types)

    def test_ctp_session_lifecycle_timeout_is_reported(self) -> None:
        class SilentLifecycleTransport(DryRunCtpTransport):
            def authenticate(self, field, request_id: int):
                self._record("authenticate", field, request_id)
                return 0

        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "lifecycle_timeout": 0.01,
            },
            dry_run=True,
            transport=SilentLifecycleTransport(),
        )

        with self.assertRaises(CtpRequestTimeoutError):
            session.start(
                authenticate=True,
                confirm_settlement=False,
                query_account=False,
                query_positions=False,
            )

        self.assertEqual(session.events[-1].event_type, "RSP_AUTHENTICATE_TIMEOUT")

    def test_ctp_session_lifecycle_callback_error_is_reported(self) -> None:
        class ErrorLifecycleTransport(DryRunCtpTransport):
            def login(self, field, request_id: int):
                self._record("login", field, request_id)
                self.callback_adapter.on_rsp_user_login(
                    {},
                    {"ErrorID": 51, "ErrorMsg": "login rejected"},
                    request_id,
                    True,
                )
                return 0

        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
                "lifecycle_timeout": 0.2,
            },
            dry_run=True,
            transport=ErrorLifecycleTransport(),
        )

        with self.assertRaises(CtpGatewayError):
            session.start(
                authenticate=False,
                confirm_settlement=False,
                query_account=False,
                query_positions=False,
            )

        self.assertEqual(session.events[-1].event_type, "RSP_USER_LOGIN_ERROR")
        self.assertEqual(session.events[-1].message, "login rejected")

    def test_ctp_depth_market_data_maps_to_tick(self) -> None:
        tick = ctp_depth_market_data_to_tick(
            {
                "TradingDay": "20240103",
                "ActionDay": "20240102",
                "UpdateTime": "21:01:02",
                "UpdateMillisec": 250,
                "InstrumentID": "RB2405",
                "ExchangeID": "SHFE",
                "LastPrice": 3601,
                "Volume": 120,
                "Turnover": 4321200,
                "OpenInterest": 180000,
                "BidPrice1": 3600,
                "BidVolume1": 8,
                "AskPrice1": 3602,
                "AskVolume1": 9,
                "OpenPrice": 3588,
                "HighestPrice": 3610,
                "LowestPrice": 3579,
                "PreClosePrice": 3570,
            }
        )

        self.assertIsInstance(tick, Tick)
        self.assertEqual(tick.symbol, "RB2405")
        self.assertEqual(tick.timestamp, datetime(2024, 1, 2, 21, 1, 2, 250000))
        self.assertEqual(tick.last_price, 3601)
        self.assertEqual(tick.bid_price_1, 3600)
        self.assertEqual(tick.ask_volume_1, 9)
        self.assertEqual(tick.extra["trading_day"], "20240103")

    def test_ctp_market_data_session_subscribes_and_updates_ticks(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        transport = DryRunCtpMarketDataTransport(
            tick_responses=[
                {
                    "TradingDay": "20240102",
                    "ActionDay": "20240102",
                    "UpdateTime": "09:30:00",
                    "UpdateMillisec": 100,
                    "InstrumentID": "RB2405",
                    "ExchangeID": "SHFE",
                    "LastPrice": 3600,
                    "Volume": 10,
                    "BidPrice1": 3599,
                    "BidVolume1": 3,
                    "AskPrice1": 3601,
                    "AskVolume1": 4,
                }
            ]
        )
        session = CtpMarketDataSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "user_id": "1001",
                "password": "secret",
                "md_front": "tcp://127.0.0.1:41213",
                "market_data_timeout": 0.2,
            },
            contract_registry=registry,
            dry_run=True,
            transport=transport,
        )

        session.start()
        subscribed = session.subscribe(["RB2405"])
        snapshot = session.snapshot()

        self.assertEqual(subscribed, ["RB2405"])
        self.assertEqual(session.state, "subscribed")
        self.assertTrue(session.logged_in)
        self.assertEqual(session.gateway.ticks["RB2405"].last_price, 3600)
        self.assertEqual(snapshot["ticks"]["RB2405"]["bid_volume_1"], 3)
        event_types = [event.event_type for event in session.events]
        callback_types = [event.event_type for event in session.callback_queue.snapshot()]
        self.assertIn("RSP_MD_USER_LOGIN_READY", event_types)
        self.assertIn("RSP_SUB_MARKET_DATA", callback_types)
        self.assertIn("RTN_DEPTH_MARKET_DATA", callback_types)
        self.assertEqual(transport.calls[1]["field"]["Password"], "***")

    def test_ctp_market_data_front_disconnect_updates_session_health(self) -> None:
        session = CtpMarketDataSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "md_front": "tcp://127.0.0.1:41213",
                "auto_recover_on_front_connected": False,
            },
            dry_run=True,
            transport=DryRunCtpMarketDataTransport(),
        )
        session.start()
        session.subscribe(["RB2405"])

        session.callback_adapter.OnFrontDisconnected(8193)

        self.assertEqual(session.state, "disconnected")
        self.assertFalse(session.connected)
        self.assertFalse(session.logged_in)
        self.assertEqual(session.last_disconnect_reason, 8193)
        self.assertEqual(session.subscribed_symbols, {"RB2405"})
        self.assertEqual(session.events[-1].event_type, "MD_FRONT_DISCONNECTED")

        session.callback_adapter.OnFrontConnected()

        self.assertTrue(session.connected)
        self.assertTrue(session.front_connected)
        self.assertIsNone(session.last_disconnect_reason)
        self.assertEqual(session.state, "subscribed")

    def test_ctp_market_data_front_reconnect_auto_resubscribes(self) -> None:
        transport = DryRunCtpMarketDataTransport()
        session = CtpMarketDataSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "md_front": "tcp://127.0.0.1:41213",
                "market_data_timeout": 0.2,
            },
            dry_run=True,
            transport=transport,
        )
        session.start()
        session.subscribe(["RB2405", "IF2406"])
        original_call_count = len(transport.calls)

        session.callback_adapter.OnFrontDisconnected(8193)
        session.callback_adapter.OnFrontConnected()

        self.assertEqual(session.state, "subscribed")
        self.assertTrue(session.logged_in)
        self.assertEqual(session.subscribed_symbols, {"IF2406", "RB2405"})
        new_actions = [call["action"] for call in transport.calls[original_call_count:]]
        self.assertEqual(new_actions, ["md_login", "subscribe_market_data"])
        self.assertEqual(
            transport.calls[-1]["field"]["InstrumentIDs"],
            ["IF2406", "RB2405"],
        )
        event_types = [event.event_type for event in session.events]
        self.assertIn("AUTO_MD_RECOVER_START", event_types)
        self.assertIn("AUTO_MD_RECOVER_READY", event_types)

    def test_ctp_watchdog_recovers_sessions_when_front_is_connected(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        market_data_transport = DryRunCtpMarketDataTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=market_data_transport,
            dry_run=True,
        )
        trading_session.start(
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.start()
        market_data_session.subscribe(["RB2405"])
        original_trading_calls = len(trading_transport.calls)
        original_market_data_calls = len(market_data_transport.calls)
        trading_session.logged_in = False
        market_data_session.logged_in = False

        watchdog = CtpSessionWatchdog(
            trading_session=trading_session,
            market_data_session=market_data_session,
            initial_backoff=0.1,
        )
        events = watchdog.check(now=10.0, force=True)

        self.assertTrue(trading_session.logged_in)
        self.assertTrue(market_data_session.logged_in)
        self.assertEqual(
            [call["action"] for call in trading_transport.calls[original_trading_calls:]],
            ["login"],
        )
        self.assertEqual(
            [call["action"] for call in market_data_transport.calls[original_market_data_calls:]],
            ["md_login", "subscribe_market_data"],
        )
        event_types = [event.event_type for event in events]
        self.assertIn("WATCHDOG_TRADING_RECOVER_READY", event_types)
        self.assertIn("WATCHDOG_MARKET_DATA_RECOVER_READY", event_types)
        snapshot = watchdog.snapshot()
        self.assertTrue(snapshot["trading"]["healthy"])
        self.assertTrue(snapshot["market_data"]["healthy"])

    def test_ctp_watchdog_backs_off_and_gives_up_after_failed_recovery(self) -> None:
        class FailingRecoveryTransport(DryRunCtpTransport):
            def __init__(self) -> None:
                super().__init__()
                self.fail_login = False

            def login(self, field: dict[str, object], request_id: int) -> int:
                if self.fail_login:
                    self._record("login", field, request_id)
                    return 7
                return super().login(field, request_id)

        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            }
        )
        trading_transport = FailingRecoveryTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        trading_session.start(
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.start()
        trading_transport.fail_login = True
        trading_session.logged_in = False
        original_call_count = len(trading_transport.calls)

        watchdog = CtpSessionWatchdog(
            trading_session=trading_session,
            market_data_session=market_data_session,
            initial_backoff=2.0,
            max_recovery_attempts=2,
        )
        first_events = watchdog.check(now=10.0, force=True)
        backoff_events = watchdog.check(now=11.0, force=True)
        second_events = watchdog.check(now=12.0, force=True)

        self.assertFalse(trading_session.logged_in)
        self.assertEqual(
            len(
                [
                    call
                    for call in trading_transport.calls[original_call_count:]
                    if call["action"] == "login"
                ]
            ),
            2,
        )
        self.assertIn(
            "WATCHDOG_TRADING_RETRY_SCHEDULED",
            [event.event_type for event in first_events],
        )
        self.assertIn(
            "WATCHDOG_TRADING_BACKOFF",
            [event.event_type for event in backoff_events],
        )
        self.assertIn(
            "WATCHDOG_TRADING_GIVE_UP",
            [event.event_type for event in second_events],
        )

    def test_ctp_realtime_engine_dispatches_tick_and_submits_order(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        market_data_transport = DryRunCtpMarketDataTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=market_data_transport,
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )

        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
                "Volume": 10,
            }
        )

        self.assertEqual(len(strategy.ticks), 1)
        self.assertIs(strategy.last_tick_from_context, strategy.ticks[0])
        self.assertEqual(len(engine.orders), 1)
        self.assertEqual(engine.orders[0].status, OrderStatus.PENDING)
        self.assertEqual(engine.last_price("RB2405"), 3600)
        self.assertEqual(trading_transport.calls[-1]["action"], "req_order_insert")
        self.assertEqual(trading_transport.calls[-1]["field"]["InstrumentID"], "RB2405")
        event_types = [event.event_type for event in engine.events]
        self.assertIn("TICK", event_types)
        self.assertIn("ORDER_SUBMITTED", event_types)

    def test_tick_bar_aggregator_emits_completed_minute_bars(self) -> None:
        aggregator = TickBarAggregator("1min")

        self.assertEqual(
            aggregator.update(
                Tick(
                    symbol="RB2405",
                    timestamp=datetime(2024, 1, 2, 9, 30, 10),
                    last_price=3600,
                    volume=100,
                )
            ),
            [],
        )
        self.assertEqual(
            aggregator.update(
                Tick(
                    symbol="RB2405",
                    timestamp=datetime(2024, 1, 2, 9, 30, 40),
                    last_price=3605,
                    volume=104,
                )
            ),
            [],
        )
        completed = aggregator.update(
            Tick(
                symbol="RB2405",
                timestamp=datetime(2024, 1, 2, 9, 31, 0),
                last_price=3602,
                volume=109,
            )
        )

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].timestamp, datetime(2024, 1, 2, 9, 30))
        self.assertEqual(completed[0].open, 3600)
        self.assertEqual(completed[0].high, 3605)
        self.assertEqual(completed[0].low, 3600)
        self.assertEqual(completed[0].close, 3605)
        self.assertEqual(completed[0].volume, 4)

    def test_ctp_realtime_engine_dispatches_completed_bars_from_ticks(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=DryRunCtpTransport(),
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = RecordRealtimeBarsStrategy()
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
            bar_frequency="1min",
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        for time_text, price, volume in [
            ("09:30:10", 3600, 100),
            ("09:30:40", 3605, 104),
            ("09:31:00", 3602, 109),
        ]:
            market_data_session.callback_adapter.on_rtn_depth_market_data(
                {
                    "TradingDay": "20240102",
                    "ActionDay": "20240102",
                    "UpdateTime": time_text,
                    "InstrumentID": "RB2405",
                    "LastPrice": price,
                    "Volume": volume,
                }
            )

        self.assertEqual(len(strategy.bars), 1)
        self.assertEqual(strategy.bars[0].close, 3605)
        self.assertEqual(strategy.closes_from_context, [3605])
        self.assertEqual(engine.history("RB2405")[0].volume, 4)
        self.assertEqual(engine.snapshot()["bars"]["RB2405"][0]["close"], 3605)
        self.assertIn("BAR", [event.event_type for event in engine.events])

    def test_ctp_realtime_engine_dispatches_order_and_trade_returns(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )
        local_order_id = engine.orders[0].order_id
        ctp_order_ref = trading_transport.calls[-1]["field"]["OrderRef"]

        trading_session.callback_adapter.on_rtn_order(
            {
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "CombOffsetFlag": "0",
                "OrderPriceType": "1",
                "LimitPrice": 0,
                "VolumeTotalOriginal": 1,
                "VolumeTraded": 1,
                "OrderStatus": "0",
                "InsertDate": "20240102",
                "InsertTime": "09:30:01",
            }
        )
        trading_session.callback_adapter.on_rtn_trade(
            {
                "TradeID": "TCTP1",
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "OffsetFlag": "0",
                "Price": 3601,
                "Volume": 1,
                "TradeDate": "20240102",
                "TradeTime": "09:30:02",
            }
        )

        self.assertEqual(engine.orders[0].order_id, local_order_id)
        self.assertEqual(engine.orders[0].status, OrderStatus.FILLED)
        self.assertEqual(engine.orders[0].fill_price, 3601)
        self.assertEqual(len(engine.trades), 1)
        self.assertEqual(engine.trades[0].order_id, local_order_id)
        self.assertEqual(engine.trades[0].trade_id, "TCTP1")
        self.assertEqual(strategy.orders_seen[-1].status, OrderStatus.FILLED)
        self.assertEqual(strategy.trades_seen[0].order_id, local_order_id)
        event_types = [event.event_type for event in engine.events]
        self.assertIn("ORDER_RETURN", event_types)
        self.assertIn("TRADE_RETURN", event_types)

    def test_ctp_realtime_engine_reconciles_orders_and_trades_from_queries(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )
        local_order_id = engine.orders[0].order_id
        ctp_order_ref = trading_transport.calls[-1]["field"]["OrderRef"]
        trading_transport.order_responses = [
            {
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "CombOffsetFlag": "0",
                "OrderPriceType": "1",
                "LimitPrice": 0,
                "VolumeTotalOriginal": 1,
                "VolumeTraded": 1,
                "OrderStatus": "0",
                "InsertDate": "20240102",
                "InsertTime": "09:30:01",
            }
        ]
        trading_transport.trade_responses = [
            {
                "TradeID": "TQRY1",
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "OffsetFlag": "0",
                "Price": 3601,
                "Volume": 1,
                "TradeDate": "20240102",
                "TradeTime": "09:30:02",
            }
        ]

        ok = engine.reconcile(
            query_account=False,
            query_positions=False,
            query_orders=True,
            query_trades=True,
        )

        self.assertTrue(ok)
        self.assertEqual(engine.orders[0].order_id, local_order_id)
        self.assertEqual(engine.orders[0].status, OrderStatus.FILLED)
        self.assertEqual(engine.orders[0].fill_price, 3601)
        self.assertEqual(len(engine.trades), 1)
        self.assertEqual(engine.trades[0].order_id, local_order_id)
        self.assertEqual(engine.trades[0].trade_id, "TQRY1")
        self.assertEqual(strategy.trades_seen[0].trade_id, "TQRY1")
        self.assertEqual(
            [call["action"] for call in trading_transport.calls[-2:]],
            ["query_orders", "query_trades"],
        )
        event_types = [event.event_type for event in engine.events]
        self.assertIn("ORDER_RECONCILED", event_types)
        self.assertIn("TRADE_RECONCILED", event_types)
        self.assertIn("RECONCILE_READY", event_types)

    def test_ctp_realtime_engine_auto_reconciles_after_watchdog_recovery(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )
        ctp_order_ref = trading_transport.calls[-1]["field"]["OrderRef"]
        trading_transport.order_responses = [
            {
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "CombOffsetFlag": "0",
                "OrderPriceType": "1",
                "LimitPrice": 0,
                "VolumeTotalOriginal": 1,
                "VolumeTraded": 1,
                "OrderStatus": "0",
                "InsertDate": "20240102",
                "InsertTime": "09:30:01",
            }
        ]
        trading_transport.trade_responses = [
            {
                "TradeID": "TAUTO1",
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "OffsetFlag": "0",
                "Price": 3601,
                "Volume": 1,
                "TradeDate": "20240102",
                "TradeTime": "09:30:02",
            }
        ]
        trading_session.logged_in = False

        engine.check_watchdog(force=True)

        self.assertEqual(engine.orders[0].status, OrderStatus.FILLED)
        self.assertEqual(engine.trades[0].trade_id, "TAUTO1")
        self.assertEqual(
            [call["action"] for call in trading_transport.calls[-4:]],
            ["login", "query_positions", "query_orders", "query_trades"],
        )
        self.assertEqual(trading_transport.calls[-2]["field"]["InstrumentID"], "RB2405")
        event_types = [event.event_type for event in engine.events]
        self.assertIn("WATCHDOG_TRADING_RECOVER_READY", event_types)
        self.assertIn("AUTO_RECONCILE_AFTER_WATCHDOG", event_types)
        self.assertIn("RECONCILE_READY", event_types)

    def test_ctp_realtime_engine_persists_and_restores_runtime_state(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        engine = CtpRealtimeEngine(
            strategy=BuyOnFirstTickStrategy(quantity=1),
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
                "Volume": 10,
            }
        )
        local_order_id = engine.orders[0].order_id
        ctp_order_ref = trading_transport.calls[-1]["field"]["OrderRef"]
        trading_session.callback_adapter.on_rtn_trade(
            {
                "TradeID": "TPERSIST1",
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "OffsetFlag": "0",
                "Price": 3601,
                "Volume": 1,
                "TradeDate": "20240102",
                "TradeTime": "09:30:02",
            }
        )
        engine.reconcile(
            query_account=False,
            query_positions=False,
            query_orders=False,
            query_trades=False,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "ctp_state.json"
            engine.save_state(state_path)
            payload = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["local_to_ctp"][local_order_id][0]["order_ref"], ctp_order_ref)
            self.assertEqual(payload["last_reconcile"]["event_type"], "RECONCILE_READY")
            self.assertIn("watchdog", payload)

            restored_gateway = CtpFuturesGateway.from_mapping(
                {
                    "broker_id": "9999",
                    "investor_id": "1001",
                    "auth_required": False,
                    "settlement_confirm_required": False,
                },
                contract_registry=registry,
            )
            restored_transport = DryRunCtpTransport()
            restored_trading_session = CtpTradingSession(
                gateway=restored_gateway,
                transport=restored_transport,
                dry_run=True,
            )
            restored_market_data_session = CtpMarketDataSession(
                gateway=restored_gateway,
                transport=DryRunCtpMarketDataTransport(),
                dry_run=True,
            )
            restored = CtpRealtimeEngine(
                strategy=BuyOnFirstTickStrategy(quantity=1),
                trading_session=restored_trading_session,
                market_data_session=restored_market_data_session,
            )
            restored.load_state(state_path)
            restored_trading_session.logged_in = True

            self.assertEqual(restored.orders[0].order_id, local_order_id)
            self.assertEqual(restored.trades[0].trade_id, "TPERSIST1")
            self.assertEqual(
                restored_trading_session.gateway.local_order_id_for_order_ref(ctp_order_ref),
                local_order_id,
            )
            new_order = restored.submit_order("RB2405", Side.BUY, 1)
            self.assertEqual(new_order.order_id, "L00000002")
            self.assertEqual(restored_transport.calls[-1]["field"]["OrderRef"], "000000000002")

    def test_ctp_realtime_engine_restores_strategy_state_after_init(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=DryRunCtpTransport(),
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = StatefulRealtimeStrategy()
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
                "Volume": 10,
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "strategy_state.json"
            engine.save_state(state_path)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["strategy"]["state"]["tick_count"], 1)
            self.assertTrue(payload["strategy"]["state"]["has_submitted"])

            restored_gateway = CtpFuturesGateway.from_mapping(
                {
                    "broker_id": "9999",
                    "investor_id": "1001",
                    "auth_required": False,
                    "settlement_confirm_required": False,
                },
                contract_registry=registry,
            )
            restored_strategy = StatefulRealtimeStrategy()
            restored_engine = CtpRealtimeEngine(
                strategy=restored_strategy,
                trading_session=CtpTradingSession(
                    gateway=restored_gateway,
                    transport=DryRunCtpTransport(),
                    dry_run=True,
                ),
                market_data_session=CtpMarketDataSession(
                    gateway=restored_gateway,
                    transport=DryRunCtpMarketDataTransport(),
                    dry_run=True,
                ),
            )
            restored_engine.load_state(state_path)
            restored_engine.start(start_trading=False, start_market_data=False)

            self.assertTrue(restored_strategy.restored)
            self.assertEqual(restored_strategy.tick_count, 1)
            self.assertTrue(restored_strategy.has_submitted)
            self.assertIn(
                "STRATEGY_STATE_LOADED",
                [event.event_type for event in restored_engine.events],
            )

    def test_ctp_realtime_engine_migrates_strategy_state_versions(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        engine = CtpRealtimeEngine(
            strategy=StatefulRealtimeStrategy(),
            trading_session=CtpTradingSession(
                gateway=gateway,
                transport=DryRunCtpTransport(),
                dry_run=True,
            ),
            market_data_session=CtpMarketDataSession(
                gateway=gateway,
                transport=DryRunCtpMarketDataTransport(),
                dry_run=True,
            ),
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        engine.market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
                "Volume": 10,
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "old_strategy_state.json"
            engine.save_state(state_path)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["strategy"]["state_schema_version"] = 1
            payload["strategy"]["state"] = {
                "tick_count": 3,
                "submitted": True,
            }
            state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            restored_gateway = CtpFuturesGateway.from_mapping(
                {
                    "broker_id": "9999",
                    "investor_id": "1001",
                    "auth_required": False,
                    "settlement_confirm_required": False,
                },
                contract_registry=registry,
            )
            strategy = MigratingRealtimeStrategy()
            restored_engine = CtpRealtimeEngine(
                strategy=strategy,
                trading_session=CtpTradingSession(
                    gateway=restored_gateway,
                    transport=DryRunCtpTransport(),
                    dry_run=True,
                ),
                market_data_session=CtpMarketDataSession(
                    gateway=restored_gateway,
                    transport=DryRunCtpMarketDataTransport(),
                    dry_run=True,
                ),
            )
            restored_engine.load_state(state_path)
            restored_engine.start(start_trading=False, start_market_data=False)

            self.assertEqual(strategy.migrated_from, 1)
            self.assertEqual(strategy.tick_count, 3)
            self.assertTrue(strategy.has_submitted)
            state = restored_engine.runtime_state()
            self.assertEqual(state["strategy"]["state_schema_version"], 2)
            event_types = [event.event_type for event in restored_engine.events]
            self.assertIn("STRATEGY_STATE_MIGRATED", event_types)
            self.assertIn("STRATEGY_STATE_LOADED", event_types)

    def test_ctp_realtime_engine_persists_event_log_jsonl_and_csv(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=DryRunCtpTransport(),
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        engine = CtpRealtimeEngine(
            strategy=BuyOnFirstTickStrategy(quantity=1),
            trading_session=trading_session,
            market_data_session=market_data_session,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            csv_path = Path(tmpdir) / "events.csv"
            state_path = Path(tmpdir) / "state.json"
            engine.enable_event_log(log_path)
            engine.start(
                ["RB2405"],
                authenticate=False,
                confirm_settlement=False,
                query_account=False,
                query_positions=False,
            )
            market_data_session.callback_adapter.on_rtn_depth_market_data(
                {
                    "TradingDay": "20240102",
                    "ActionDay": "20240102",
                    "UpdateTime": "09:30:00",
                    "InstrumentID": "RB2405",
                    "LastPrice": 3600,
                    "Volume": 10,
                }
            )
            engine.save_state(state_path)
            engine.export_event_log_csv(csv_path)

            rows = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_types = [row["event_type"] for row in rows]
            self.assertIn("EVENT_LOG_ENABLED", event_types)
            self.assertIn("RUN_START", event_types)
            self.assertIn("TICK", event_types)
            self.assertIn("ORDER_SUBMITTED", event_types)
            self.assertIn("STATE_SAVED", event_types)
            self.assertIn("EVENT_LOG_CSV_EXPORTED", event_types)
            self.assertEqual(rows[-1]["payload"]["path"], str(csv_path))
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn("event_type", csv_text)
            self.assertIn("ORDER_SUBMITTED", csv_text)

    def test_event_recorder_rotates_jsonl_and_keeps_backup_limit(self) -> None:
        recorder = EventRecorder()

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            recorder.enable_jsonl(log_path, max_bytes=220, backup_count=2)
            for index in range(8):
                recorder.record(
                    datetime(2024, 1, 2, 9, 30, index),
                    f"EVENT_{index}",
                    "rotation check",
                    payload="x" * 100,
                )

            self.assertTrue(log_path.exists())
            self.assertTrue(Path(str(log_path) + ".1").exists())
            self.assertTrue(Path(str(log_path) + ".2").exists())
            self.assertFalse(Path(str(log_path) + ".3").exists())

            active_rows = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(active_rows[-1]["event_type"], "EVENT_7")
            retained = 0
            for path in [log_path, Path(str(log_path) + ".1"), Path(str(log_path) + ".2")]:
                retained += len(path.read_text(encoding="utf-8").splitlines())
            self.assertLess(retained, len(recorder.events))

    def test_ctp_realtime_engine_dispatches_order_insert_error(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )
        insert_call = trading_transport.calls[-1]

        trading_session.callback_adapter.on_rsp_order_insert(
            {"OrderRef": insert_call["field"]["OrderRef"], "InstrumentID": "RB2405"},
            {"ErrorID": 88, "ErrorMsg": "insert rejected"},
            insert_call["request_id"],
            True,
        )

        self.assertEqual(engine.orders[0].status, OrderStatus.REJECTED)
        self.assertEqual(engine.orders[0].reject_reason, "insert rejected")
        self.assertEqual(strategy.orders_seen[-1].status, OrderStatus.REJECTED)
        self.assertEqual(engine.events[-1].event_type, "ORDER_INSERT_REJECTED")

    def test_ctp_realtime_engine_can_submit_cancel_and_handle_canceled_return(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )
        local_order = engine.orders[0]
        ctp_order_ref = trading_transport.calls[-1]["field"]["OrderRef"]

        engine.cancel_order(local_order.order_id)
        trading_session.callback_adapter.on_rtn_order(
            {
                "OrderRef": ctp_order_ref,
                "InstrumentID": "RB2405",
                "Direction": "0",
                "CombOffsetFlag": "0",
                "OrderPriceType": "1",
                "LimitPrice": 0,
                "VolumeTotalOriginal": 1,
                "VolumeTraded": 0,
                "OrderStatus": "5",
                "InsertDate": "20240102",
                "InsertTime": "09:30:01",
            }
        )

        self.assertEqual(trading_transport.calls[-1]["action"], "req_order_action")
        self.assertEqual(engine.orders[0].status, OrderStatus.CANCELED)
        self.assertEqual(strategy.orders_seen[-1].status, OrderStatus.CANCELED)
        event_types = [event.event_type for event in engine.events]
        self.assertIn("ORDER_CANCEL_SUBMITTED", event_types)
        self.assertIn("ORDER_RETURN", event_types)

    def test_ctp_realtime_engine_dispatches_cancel_reject(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=1)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
        )
        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )
        ctp_order_ref = trading_transport.calls[-1]["field"]["OrderRef"]

        engine.cancel_order(engine.orders[0].order_id)
        cancel_call = trading_transport.calls[-1]
        trading_session.callback_adapter.on_rsp_order_action(
            {"OrderRef": ctp_order_ref, "InstrumentID": "RB2405"},
            {"ErrorID": 49, "ErrorMsg": "cancel rejected"},
            cancel_call["request_id"],
            True,
        )

        self.assertEqual(engine.orders[0].status, OrderStatus.PENDING)
        self.assertEqual(engine.orders[0].reject_reason, "cancel rejected")
        self.assertEqual(strategy.orders_seen[-1].reject_reason, "cancel rejected")
        self.assertEqual(engine.events[-1].event_type, "ORDER_CANCEL_REJECTED")

    def test_ctp_realtime_engine_applies_risk_before_ctp_order_insert(self) -> None:
        registry = ContractRegistry(
            contracts={
                "RB2405": ContractSpec(symbol="RB2405", exchange="SHFE", multiplier=10)
            }
        )
        gateway = CtpFuturesGateway.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "auth_required": False,
                "settlement_confirm_required": False,
            },
            contract_registry=registry,
        )
        trading_transport = DryRunCtpTransport()
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            transport=DryRunCtpMarketDataTransport(),
            dry_run=True,
        )
        trading_session = CtpTradingSession(
            gateway=gateway,
            transport=trading_transport,
            dry_run=True,
        )
        strategy = BuyOnFirstTickStrategy(quantity=2)
        engine = CtpRealtimeEngine(
            strategy=strategy,
            trading_session=trading_session,
            market_data_session=market_data_session,
            risk_manager=RiskManager.from_mapping({"max_order_quantity": 1}),
        )

        engine.start(
            ["RB2405"],
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )
        market_data_session.callback_adapter.on_rtn_depth_market_data(
            {
                "TradingDay": "20240102",
                "ActionDay": "20240102",
                "UpdateTime": "09:30:00",
                "InstrumentID": "RB2405",
                "LastPrice": 3600,
            }
        )

        self.assertEqual(engine.orders[0].status, OrderStatus.REJECTED)
        self.assertEqual(engine.orders[0].reject_reason, "order quantity 2 exceeds max 1")
        self.assertNotIn(
            "req_order_insert",
            [call["action"] for call in trading_transport.calls],
        )
        self.assertEqual(strategy.orders_seen[0].status, OrderStatus.REJECTED)
        self.assertEqual(engine.events[-1].event_type, "ORDER_REJECTED")

    def test_ctp_market_data_login_timeout_is_reported(self) -> None:
        class SilentMarketDataTransport(DryRunCtpMarketDataTransport):
            def login(self, field, request_id: int):
                self._record("md_login", field, request_id)
                return 0

        session = CtpMarketDataSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "market_data_timeout": 0.01,
            },
            dry_run=True,
            transport=SilentMarketDataTransport(),
        )

        with self.assertRaises(CtpRequestTimeoutError):
            session.start()

        self.assertEqual(session.events[-1].event_type, "RSP_MD_USER_LOGIN_TIMEOUT")

    def test_native_market_data_transport_registers_callback_adapter_when_api_is_provided(self) -> None:
        class FakeMdApi:
            def __init__(self) -> None:
                self.spi = None
                self.front = ""
                self.initialized = False

            def RegisterSpi(self, spi) -> None:
                self.spi = spi

            def RegisterFront(self, front: str) -> None:
                self.front = front

            def Init(self) -> None:
                self.initialized = True

            def ReqUserLogin(self, field, request_id: int) -> int:
                self.login_field = field
                self.login_request_id = request_id
                self.spi.OnRspUserLogin(
                    field,
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

            def SubscribeMarketData(self, instruments, count: int) -> int:
                self.subscribed = list(instruments)
                self.count = count
                self.spi.OnRspSubMarketData(
                    {"InstrumentID": instruments[0]},
                    {"ErrorID": 0, "ErrorMsg": ""},
                    0,
                    True,
                )
                self.spi.OnRtnDepthMarketData(
                    {
                        "TradingDay": "20240102",
                        "ActionDay": "20240102",
                        "UpdateTime": "09:30:00",
                        "InstrumentID": instruments[0],
                        "LastPrice": 100,
                    }
                )
                return 0

        fake = FakeMdApi()
        transport = NativeCtpMarketDataTransport(module_name="", api=fake)
        session = CtpMarketDataSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "md_front": "tcp://127.0.0.1:41213",
            },
            dry_run=False,
            transport=transport,
        )

        session.start()
        session.subscribe(["TEST"])

        self.assertIs(fake.spi, session.callback_adapter)
        self.assertEqual(fake.front, "tcp://127.0.0.1:41213")
        self.assertTrue(fake.initialized)
        self.assertEqual(fake.subscribed, ["TEST"])
        self.assertEqual(fake.count, 1)
        self.assertEqual(session.gateway.ticks["TEST"].last_price, 100)

    def test_native_transport_registers_callback_adapter_when_api_is_provided(self) -> None:
        class FakeApi:
            def __init__(self) -> None:
                self.spi = None
                self.front = ""
                self.initialized = False

            def RegisterSpi(self, spi) -> None:
                self.spi = spi

            def RegisterFront(self, front: str) -> None:
                self.front = front

            def SubscribePrivateTopic(self, mode: int) -> None:
                self.private_mode = mode

            def SubscribePublicTopic(self, mode: int) -> None:
                self.public_mode = mode

            def Init(self) -> None:
                self.initialized = True

            def ReqUserLogin(self, field, request_id: int) -> int:
                self.login_field = field
                self.login_request_id = request_id
                self.spi.OnRspUserLogin(
                    field,
                    {"ErrorID": 0, "ErrorMsg": ""},
                    request_id,
                    True,
                )
                return 0

        fake = FakeApi()
        transport = NativeCtpTraderTransport(module_name="", api=fake)
        session = CtpTradingSession.from_mapping(
            {
                "broker_id": "9999",
                "investor_id": "1001",
                "front": "tcp://127.0.0.1:41205",
            },
            dry_run=False,
            transport=transport,
        )
        session.start(
            authenticate=False,
            confirm_settlement=False,
            query_account=False,
            query_positions=False,
        )

        self.assertIs(fake.spi, session.callback_adapter)
        self.assertEqual(fake.front, "tcp://127.0.0.1:41205")
        self.assertTrue(fake.initialized)
        self.assertTrue(session.logged_in)

    def test_native_ctp_transport_missing_module_fails_clearly(self) -> None:
        transport = NativeCtpTraderTransport(
            module_name="definitely_missing_ctp_module_for_tests"
        )

        with self.assertRaises(ModuleNotFoundError):
            transport.connect(CtpConnectionConfig(broker_id="9999", investor_id="1001"))

    def test_night_session_maps_to_next_trading_date(self) -> None:
        sessions = session_template("cn_futures")

        self.assertEqual(
            sessions.trading_date(datetime(2024, 1, 2, 21, 30), TradingCalendar()),
            date(2024, 1, 3),
        )
        self.assertEqual(
            sessions.trading_date(datetime(2024, 1, 3, 9, 30), TradingCalendar()),
            date(2024, 1, 3),
        )

    def test_resample_minute_bars_to_five_minutes(self) -> None:
        bars = [
            Bar(
                symbol="TEST",
                timestamp=datetime(2024, 1, 2, 9, minute),
                open=100 + minute,
                high=101 + minute,
                low=99 + minute,
                close=100.5 + minute,
                volume=10,
            )
            for minute in range(1, 6)
        ]

        resampled = resample_bars(bars, "5min")

        self.assertEqual(len(resampled), 1)
        self.assertEqual(resampled[0].timestamp, datetime(2024, 1, 2, 9, 5))
        self.assertEqual(resampled[0].open, 101)
        self.assertEqual(resampled[0].high, 106)
        self.assertEqual(resampled[0].low, 100)
        self.assertEqual(resampled[0].close, 105.5)
        self.assertEqual(resampled[0].volume, 50)

    def test_data_quality_report_detects_duplicate_invalid_and_gap(self) -> None:
        bars = [
            Bar("TEST", datetime(2024, 1, 2, 9, 1), 100, 101, 99, 100, 10),
            Bar("TEST", datetime(2024, 1, 2, 9, 4), 100, 98, 99, 100, 10),
            Bar("TEST", datetime(2024, 1, 2, 9, 4), 100, 101, 99, 100, 10),
        ]

        report = validate_bars(
            bars,
            expected_frequency="1min",
            sessions=session_template("day"),
        )
        issue_types = {issue.issue_type for issue in report.issues}

        self.assertIn("duplicate_bar", issue_types)
        self.assertIn("invalid_ohlc", issue_types)
        self.assertIn("missing_bar_gap", issue_types)
        self.assertFalse(report.ok)


if __name__ == "__main__":
    unittest.main()
