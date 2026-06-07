from __future__ import annotations

import argparse
import importlib
import json
from typing import Any

from .backtest import BacktestEngine
from .config import (
    account_mode_from_config,
    contract_registry_from_config,
    ctp_from_config,
    daily_settlement_from_config,
    execution_from_config,
    load_bars_from_sources,
    load_json_config,
    normalize_data_sources,
    risk_from_config,
)
from .ctp import CtpFuturesGateway, CtpMarketDataSession, CtpTradingSession
from .data import (
    generate_intraday_sample_bars,
    generate_sample_market,
    load_akshare_futures_bars,
    load_bars_csv,
    write_bars_csv,
)
from .datacenter import check_data_file, resample_file
from .execution import ExecutionConfig
from .optimization import run_grid_search
from .paper import run_paper_trading
from .realtime import CtpRealtimeEngine
from .replay import run_market_replay
from .risk import RiskManager
from .sample_strategies import MovingAverageCrossStrategy
from .strategy import Strategy
from .trading_calendar import session_template
from .models import OrderStatus


def parse_strategy(path: str, params: dict[str, Any]) -> Strategy:
    if path == "sample:ma_cross":
        return MovingAverageCrossStrategy(**params)

    if ":" not in path:
        raise ValueError("strategy must use module:ClassName format")

    module_name, class_name = path.split(":", 1)
    module = importlib.import_module(module_name)
    strategy_type = getattr(module, class_name)
    strategy = strategy_type(**params)
    if not isinstance(strategy, Strategy):
        expected_methods = ("on_init", "on_bar", "on_order", "on_trade", "on_finish")
        if not all(hasattr(strategy, method) for method in expected_methods):
            raise TypeError(f"{path} does not look like a Strategy")
    return strategy


def parse_params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant platform CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("generate-sample", help="write deterministic sample OHLCV data")
    sample.add_argument("--output", default="data/sample_bars.csv")
    sample.add_argument("--symbol", default="SAMPLE")
    sample.add_argument("--symbols", nargs="+", help="one or more symbols for multi-symbol data")
    sample.add_argument("--start", default="2024-01-02")
    sample.add_argument("--periods", type=int, default=240)
    sample.add_argument("--seed", type=int, default=7)

    intraday_sample = subparsers.add_parser(
        "generate-intraday-sample",
        help="write deterministic intraday OHLCV data",
    )
    intraday_sample.add_argument("--output", default="data/futures_rb2405_1m.csv")
    intraday_sample.add_argument("--symbol", default="RB2405")
    intraday_sample.add_argument("--start", default="2024-01-02")
    intraday_sample.add_argument("--days", type=int, default=3)
    intraday_sample.add_argument("--session", default="day")
    intraday_sample.add_argument("--seed", type=int, default=11)

    backtest = subparsers.add_parser("backtest", help="run a CSV-based bar backtest")
    backtest.add_argument("--config", help="JSON config file")
    backtest.add_argument("--data", nargs="+", help="CSV data paths")
    backtest.add_argument("--symbol")
    backtest.add_argument("--strategy")
    backtest.add_argument("--params", help='JSON strategy params, for example {"fast_window":8}')
    backtest.add_argument("--cash", type=float)
    backtest.add_argument("--commission-rate", type=float)
    backtest.add_argument("--slippage", type=float)
    backtest.add_argument("--output")

    optimize = subparsers.add_parser("optimize", help="run a grid-search optimization")
    optimize.add_argument("--config", help="JSON config file")
    optimize.add_argument("--data", nargs="+", help="CSV data paths")
    optimize.add_argument("--symbol")
    optimize.add_argument("--strategy")
    optimize.add_argument("--params", help="base JSON strategy params")
    optimize.add_argument("--grid", help='JSON grid, for example {"fast_window":[6,8]}')
    optimize.add_argument("--objective", help="metric to maximize")
    optimize.add_argument("--cash", type=float)
    optimize.add_argument("--commission-rate", type=float)
    optimize.add_argument("--slippage", type=float)
    optimize.add_argument("--output")

    replay = subparsers.add_parser("replay", help="replay historical bars as a paper session")
    replay.add_argument("--config", help="JSON config file")
    replay.add_argument("--data", nargs="+", help="CSV data paths")
    replay.add_argument("--symbol")
    replay.add_argument("--strategy")
    replay.add_argument("--params", help="JSON strategy params")
    replay.add_argument("--cash", type=float)
    replay.add_argument("--commission-rate", type=float)
    replay.add_argument("--slippage", type=float)
    replay.add_argument("--output")
    replay.add_argument("--max-steps", type=int, help="limit replay to N timestamps")
    replay.add_argument("--stream", action="store_true", help="print recent replay events")

    paper = subparsers.add_parser("paper", help="run a paper trading session through the simulated gateway")
    paper.add_argument("--config", help="JSON config file")
    paper.add_argument("--data", nargs="+", help="CSV data paths")
    paper.add_argument("--symbol")
    paper.add_argument("--strategy")
    paper.add_argument("--params", help="JSON strategy params")
    paper.add_argument("--cash", type=float)
    paper.add_argument("--commission-rate", type=float)
    paper.add_argument("--slippage", type=float)
    paper.add_argument("--output")
    paper.add_argument("--max-steps", type=int, help="limit paper trading to N timestamps")
    paper.add_argument("--stream", action="store_true", help="print recent paper events")

    serve = subparsers.add_parser("serve", help="start the local web workspace")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workspace", default=".")

    data_check = subparsers.add_parser("data-check", help="validate OHLCV data quality")
    data_check.add_argument("--input", required=True)
    data_check.add_argument("--output", default="output/data_quality/report.csv")
    data_check.add_argument("--symbol")
    data_check.add_argument("--expected-frequency")
    data_check.add_argument("--session", default="always")

    data_resample = subparsers.add_parser("data-resample", help="resample OHLCV bars")
    data_resample.add_argument("--input", required=True)
    data_resample.add_argument("--output", required=True)
    data_resample.add_argument("--frequency", required=True)
    data_resample.add_argument("--symbol")
    data_resample.add_argument("--session", default="always")

    data_akshare = subparsers.add_parser(
        "data-akshare",
        help="fetch AkShare futures historical bars and save them as CSV",
    )
    data_akshare.add_argument("--symbol", required=True, help="AkShare futures symbol, for example RB0")
    data_akshare.add_argument(
        "--api",
        default="futures_zh_daily_sina",
        choices=[
            "futures_zh_daily_sina",
            "futures_main_sina",
            "futures_hist_em",
            "get_futures_daily",
        ],
    )
    data_akshare.add_argument("--start-date", help="inclusive start date, for example 20240101")
    data_akshare.add_argument("--end-date", help="inclusive end date, for example 20241231")
    data_akshare.add_argument("--market", help="exchange market for get_futures_daily, for example SHFE")
    data_akshare.add_argument("--variety", help="filter get_futures_daily by variety, for example RB")
    data_akshare.add_argument("--period", default="daily", help="period for futures_hist_em")
    data_akshare.add_argument("--output-symbol", help="override symbol written into CSV")
    data_akshare.add_argument("--output", help="CSV output path")

    ctp_order = subparsers.add_parser(
        "ctp-order",
        help="build a CTP futures order insert request without sending it",
    )
    ctp_order.add_argument("--config", required=True, help="JSON config with ctp and contracts")
    ctp_order.add_argument("--symbol", required=True)
    ctp_order.add_argument("--side", choices=["buy", "sell"], required=True)
    ctp_order.add_argument("--quantity", type=float, required=True)
    ctp_order.add_argument("--order-type", choices=["market", "limit"], default="limit")
    ctp_order.add_argument("--limit-price", type=float)
    ctp_order.add_argument(
        "--offset",
        choices=["auto", "open", "close", "close-today", "close-yesterday"],
        default="open",
    )
    ctp_order.add_argument("--order-id", default="DRYRUN")

    ctp_session = subparsers.add_parser(
        "ctp-session",
        help="run a CTP futures session lifecycle check",
    )
    ctp_session.add_argument("--config", required=True, help="JSON config with ctp and contracts")
    ctp_session.add_argument(
        "--live",
        action="store_true",
        help="use the configured CTP Python binding instead of dry-run mode",
    )
    ctp_session.add_argument("--skip-auth", action="store_true")
    ctp_session.add_argument("--skip-settlement-confirm", action="store_true")
    ctp_session.add_argument("--skip-queries", action="store_true")
    ctp_session.add_argument("--transport-module", help="override ctp.transport_module")
    ctp_session.add_argument("--trader-api-factory", help="override ctp.trader_api_factory")
    ctp_session.add_argument(
        "--lifecycle-timeout",
        type=float,
        help="seconds to wait for auth/login/settlement callbacks",
    )
    ctp_session.add_argument(
        "--no-wait-lifecycle-callbacks",
        action="store_true",
        help="do not wait for auth/login/settlement callbacks after sending requests",
    )
    ctp_session.add_argument("--query-timeout", type=float, help="seconds to wait for async query callbacks")
    ctp_session.add_argument(
        "--no-wait-query-callbacks",
        action="store_true",
        help="do not wait for async query callbacks after sending query requests",
    )
    ctp_session.add_argument(
        "--simulate-callbacks",
        action="store_true",
        help="emit synthetic CTP callbacks after the session starts",
    )

    ctp_md = subparsers.add_parser(
        "ctp-md",
        help="run a CTP futures market data lifecycle and subscription check",
    )
    ctp_md.add_argument("--config", required=True, help="JSON config with ctp and contracts")
    ctp_md.add_argument(
        "--symbols",
        nargs="+",
        help="instruments to subscribe; defaults to contract symbols from config",
    )
    ctp_md.add_argument(
        "--live",
        action="store_true",
        help="use the configured CTP MD Python binding instead of dry-run mode",
    )
    ctp_md.add_argument("--transport-module", help="override ctp.md_transport_module")
    ctp_md.add_argument("--md-api-factory", help="override ctp.md_api_factory")
    ctp_md.add_argument(
        "--market-data-timeout",
        type=float,
        help="seconds to wait for market data login callbacks",
    )
    ctp_md.add_argument(
        "--no-wait-market-data-callbacks",
        action="store_true",
        help="do not wait for market data login callbacks after sending login",
    )
    ctp_md.add_argument(
        "--simulate-tick",
        action="store_true",
        help="emit one synthetic depth market data tick after subscribing",
    )

    ctp_realtime = subparsers.add_parser(
        "ctp-realtime",
        help="run a CTP futures realtime strategy dispatch check",
    )
    ctp_realtime.add_argument("--config", required=True, help="JSON config with ctp and contracts")
    ctp_realtime.add_argument(
        "--strategy",
        default="quant_platform.sample_strategies:BuyFirstTickStrategy",
        help="strategy path, defaults to a one-shot tick strategy",
    )
    ctp_realtime.add_argument("--params", help="JSON strategy params")
    ctp_realtime.add_argument(
        "--symbols",
        nargs="+",
        help="instruments to subscribe; defaults to contract symbols from config",
    )
    ctp_realtime.add_argument(
        "--live",
        action="store_true",
        help="use configured CTP Python bindings instead of dry-run mode",
    )
    ctp_realtime.add_argument("--skip-auth", action="store_true")
    ctp_realtime.add_argument("--skip-settlement-confirm", action="store_true")
    ctp_realtime.add_argument("--skip-queries", action="store_true")
    ctp_realtime.add_argument(
        "--simulate-tick",
        action="store_true",
        help="emit one synthetic tick after subscribing so strategy dispatch can be checked",
    )
    ctp_realtime.add_argument(
        "--bar-frequency",
        help="aggregate realtime ticks into bars, currently supports 1min",
    )
    ctp_realtime.add_argument(
        "--flush-bars",
        action="store_true",
        help="flush current realtime bars before printing the snapshot",
    )
    ctp_realtime.add_argument(
        "--simulate-fill",
        action="store_true",
        help="emit synthetic CTP order/trade returns after the simulated tick",
    )
    ctp_realtime.add_argument(
        "--simulate-insert-error",
        action="store_true",
        help="emit a synthetic OnRspOrderInsert error after the simulated tick",
    )
    ctp_realtime.add_argument(
        "--simulate-cancel",
        action="store_true",
        help="submit a synthetic cancel request and emit a canceled order return",
    )
    ctp_realtime.add_argument(
        "--simulate-cancel-error",
        action="store_true",
        help="submit a synthetic cancel request and emit an OnRspOrderAction error",
    )
    ctp_realtime.add_argument(
        "--simulate-disconnect",
        action="store_true",
        help="emit synthetic trading and market data front disconnect callbacks",
    )
    ctp_realtime.add_argument(
        "--simulate-reconnect",
        action="store_true",
        help="emit synthetic front disconnect and reconnect callbacks to check auto recovery",
    )
    ctp_realtime.add_argument(
        "--watchdog-checks",
        type=int,
        default=0,
        help="run N realtime watchdog checks before printing the snapshot",
    )
    ctp_realtime.add_argument(
        "--reconcile",
        action="store_true",
        help="query CTP account, positions, orders, and trades, then reconcile local realtime state",
    )
    ctp_realtime.add_argument(
        "--reconcile-symbols",
        nargs="+",
        help="limit reconciliation to these instruments",
    )
    ctp_realtime.add_argument(
        "--reconcile-start-time",
        help="CTP order/trade reconciliation start time, for example 09:30:00",
    )
    ctp_realtime.add_argument(
        "--reconcile-end-time",
        help="CTP order/trade reconciliation end time, for example 15:00:00",
    )
    ctp_realtime.add_argument(
        "--state-path",
        default="output/ctp_realtime_state.json",
        help="JSON file used by --load-state and --save-state",
    )
    ctp_realtime.add_argument(
        "--load-state",
        action="store_true",
        help="restore realtime orders, trades, and CTP order-ref mappings before starting",
    )
    ctp_realtime.add_argument(
        "--save-state",
        action="store_true",
        help="persist realtime orders, trades, and CTP order-ref mappings before printing the snapshot",
    )
    ctp_realtime.add_argument(
        "--event-log-path",
        help="append realtime engine events to this JSONL file",
    )
    ctp_realtime.add_argument(
        "--event-log-csv",
        help="export the in-memory realtime event log to this CSV file before printing the snapshot",
    )
    ctp_realtime.add_argument(
        "--event-log-max-bytes",
        type=int,
        help="rotate the JSONL event log when it would exceed this many bytes",
    )
    ctp_realtime.add_argument(
        "--event-log-backups",
        type=int,
        default=0,
        help="number of rotated JSONL event log backups to keep",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "generate-sample":
        symbols = args.symbols or [args.symbol]
        bars = generate_sample_market(
            symbols=symbols,
            start=args.start,
            periods=args.periods,
            seed=args.seed,
        )
        path = write_bars_csv(bars, args.output)
        print(f"sample data: {path}")
        return

    if args.command == "generate-intraday-sample":
        bars = generate_intraday_sample_bars(
            symbol=args.symbol,
            start=args.start,
            days=args.days,
            session=args.session,
            seed=args.seed,
        )
        path = write_bars_csv(bars, args.output)
        print(f"intraday sample data: {path}")
        print(f"bars: {len(bars)}")
        return

    if args.command == "data-akshare":
        output = args.output or f"data/akshare_{args.symbol}_{args.api}.csv"
        bars = load_akshare_futures_bars(
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            api=args.api,
            market=args.market,
            variety=args.variety,
            period=args.period,
            output_symbol=args.output_symbol,
        )
        path = write_bars_csv(bars, output)
        print(
            json.dumps(
                {
                    "output": str(path),
                    "bars": len(bars),
                    "symbol": args.output_symbol or args.symbol,
                    "api": args.api,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "backtest":
        setup = resolve_backtest_setup(args, default_output="output/backtests/latest")
        strategy = parse_strategy(setup["strategy"], setup["params"])
        engine = BacktestEngine(
            bars=setup["bars"],
            strategy=strategy,
            initial_cash=setup["cash"],
            commission_rate=setup["commission_rate"],
            slippage=setup["slippage"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
        )
        result = engine.run()
        paths = result.export(setup["output"])

        print(f"final equity: {result.final_equity:.2f}")
        print(f"total return: {result.metrics['total_return']:.2%}")
        print(f"max drawdown: {result.metrics['max_drawdown']:.2%}")
        print(f"trades: {int(result.metrics['trade_count'])}")
        if setup["account_mode"] == "futures":
            print(f"margin: {result.metrics['final_margin']:.2f}")
            print(f"available: {result.metrics['final_available']:.2f}")
            print(f"risk ratio: {result.metrics['final_risk_ratio']:.2%}")
        print(f"summary: {paths['summary']}")
        print(f"html: {paths['html']}")
        return

    if args.command == "optimize":
        setup = resolve_backtest_setup(args, default_output="output/optimizations/latest")
        config = setup["config"]
        optimization_config = config.get("optimization", {})
        grid = json.loads(args.grid) if args.grid else optimization_config.get("grid")
        if not grid:
            raise ValueError("optimize requires --grid or optimization.grid in config")
        objective = args.objective or optimization_config.get("objective", "sharpe")

        def strategy_factory(params: dict[str, Any]) -> Strategy:
            return parse_strategy(setup["strategy"], params)

        search = run_grid_search(
            bars=setup["bars"],
            strategy_factory=strategy_factory,
            param_grid=grid,
            base_params=setup["params"],
            initial_cash=setup["cash"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
            commission_rate=setup["commission_rate"],
            slippage=setup["slippage"],
            objective=objective,
        )
        paths = search.export(setup["output"])
        best_score = (
            float(search.results[objective].iloc[0]) if not search.results.empty else 0.0
        )
        print(f"runs: {len(search.results)}")
        print(f"objective: {objective}={best_score:.6f}")
        print(f"best params: {json.dumps(search.best_params, sort_keys=True)}")
        print(f"results: {paths['results']}")
        return

    if args.command == "replay":
        setup = resolve_backtest_setup(args, default_output="output/replays/latest")
        strategy = parse_strategy(setup["strategy"], setup["params"])
        replay = run_market_replay(
            bars=setup["bars"],
            strategy=strategy,
            initial_cash=setup["cash"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
            max_steps=args.max_steps,
        )
        paths = replay.export(setup["output"])

        print(f"steps: {replay.steps}")
        print(f"final equity: {replay.result.final_equity:.2f}")
        print(f"orders: {len(replay.result.orders)}")
        print(f"trades: {len(replay.result.trades)}")
        print(f"events: {paths['events']}")
        print(f"html: {paths['html']}")
        if args.stream:
            for event in replay.result.events[-20:]:
                timestamp = event.timestamp.isoformat() if event.timestamp else ""
                symbol = f" {event.symbol}" if event.symbol else ""
                print(f"{timestamp} [{event.event_type}]{symbol} {event.message}")
        return

    if args.command == "paper":
        setup = resolve_backtest_setup(args, default_output="output/paper/latest")
        strategy = parse_strategy(setup["strategy"], setup["params"])
        result = run_paper_trading(
            bars=setup["bars"],
            strategy=strategy,
            initial_cash=setup["cash"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
            max_steps=args.max_steps,
        )
        paths = result.export(setup["output"])

        print(f"steps: {result.steps}")
        print(f"final equity: {result.final_equity:.2f}")
        print(f"orders: {len(result.orders)}")
        print(f"trades: {len(result.trades)}")
        print(f"working orders: {int(result.metrics.get('working_order_count', 0.0))}")
        if setup["account_mode"] == "futures":
            print(f"margin: {result.metrics['final_margin']:.2f}")
            print(f"available: {result.metrics['final_available']:.2f}")
            print(f"risk ratio: {result.metrics['final_risk_ratio']:.2%}")
        print(f"events: {paths['events']}")
        print(f"html: {paths['html']}")
        if args.stream:
            for event in result.events[-20:]:
                timestamp = event.timestamp.isoformat() if event.timestamp else ""
                symbol = f" {event.symbol}" if event.symbol else ""
                print(f"{timestamp} [{event.event_type}]{symbol} {event.message}")
        return

    if args.command == "serve":
        from .webapp import run_server

        run_server(host=args.host, port=args.port, workspace=args.workspace)
        return

    if args.command == "data-check":
        report = check_data_file(
            input_path=args.input,
            output_path=args.output,
            symbol=args.symbol,
            expected_frequency=args.expected_frequency,
            sessions=session_template(args.session),
        )
        print(f"ok: {report.ok}")
        print(f"issues: {len(report.issues)}")
        print(f"report: {args.output}")
        return

    if args.command == "data-resample":
        bars = resample_file(
            input_path=args.input,
            output_path=args.output,
            frequency=args.frequency,
            symbol=args.symbol,
            sessions=session_template(args.session),
        )
        print(f"bars: {len(bars)}")
        print(f"output: {args.output}")
        return

    if args.command == "ctp-order":
        config = load_json_config(args.config)
        registry = contract_registry_from_config(config)
        gateway = CtpFuturesGateway(
            config=ctp_from_config(config),
            contract_registry=registry,
        )
        order = _order_from_ctp_order_args(args)
        requests = gateway.create_order_insert_requests(order)
        print(
            json.dumps(
                [request.field for request in requests],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.command == "ctp-session":
        config = load_json_config(args.config)
        if args.transport_module or args.trader_api_factory:
            config.setdefault("ctp", {})
            if args.transport_module:
                config["ctp"]["transport_module"] = args.transport_module
            if args.trader_api_factory:
                config["ctp"]["trader_api_factory"] = args.trader_api_factory
        if args.lifecycle_timeout is not None or args.no_wait_lifecycle_callbacks:
            config.setdefault("ctp", {})
            if args.lifecycle_timeout is not None:
                config["ctp"]["lifecycle_timeout"] = args.lifecycle_timeout
            if args.no_wait_lifecycle_callbacks:
                config["ctp"]["wait_for_lifecycle_callbacks"] = False
        if args.query_timeout is not None or args.no_wait_query_callbacks:
            config.setdefault("ctp", {})
            if args.query_timeout is not None:
                config["ctp"]["query_timeout"] = args.query_timeout
            if args.no_wait_query_callbacks:
                config["ctp"]["wait_for_query_callbacks"] = False
        registry = contract_registry_from_config(config)
        session = CtpTradingSession.from_mapping(
            config.get("ctp"),
            contract_registry=registry,
            dry_run=not args.live,
        )
        session.start(
            authenticate=not args.skip_auth,
            confirm_settlement=not args.skip_settlement_confirm,
            query_account=not args.skip_queries,
            query_positions=not args.skip_queries,
        )
        if args.simulate_callbacks:
            _simulate_ctp_callbacks(session)
        print(
            json.dumps(
                session.snapshot(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.command == "ctp-md":
        config = load_json_config(args.config)
        if (
            args.transport_module
            or args.md_api_factory
            or args.market_data_timeout is not None
            or args.no_wait_market_data_callbacks
        ):
            config.setdefault("ctp", {})
            if args.transport_module:
                config["ctp"]["md_transport_module"] = args.transport_module
            if args.md_api_factory:
                config["ctp"]["md_api_factory"] = args.md_api_factory
            if args.market_data_timeout is not None:
                config["ctp"]["market_data_timeout"] = args.market_data_timeout
            if args.no_wait_market_data_callbacks:
                config["ctp"]["wait_for_market_data_callbacks"] = False
        registry = contract_registry_from_config(config)
        symbols = args.symbols or list(config.get("contracts", {}).keys())
        if not symbols:
            raise ValueError("ctp-md requires --symbols or at least one configured contract")
        session = CtpMarketDataSession.from_mapping(
            config.get("ctp"),
            contract_registry=registry,
            dry_run=not args.live,
        )
        session.start()
        session.subscribe(symbols)
        if args.simulate_tick:
            _simulate_ctp_market_data(session, symbols)
        print(
            json.dumps(
                session.snapshot(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.command == "ctp-realtime":
        config = load_json_config(args.config)
        registry = contract_registry_from_config(config)
        symbols = args.symbols or list(config.get("contracts", {}).keys())
        if not symbols:
            raise ValueError("ctp-realtime requires --symbols or at least one configured contract")
        gateway = CtpFuturesGateway.from_mapping(
            config.get("ctp"),
            contract_registry=registry,
        )
        trading_session = CtpTradingSession(
            gateway=gateway,
            dry_run=not args.live,
        )
        market_data_session = CtpMarketDataSession(
            gateway=gateway,
            dry_run=not args.live,
        )
        engine_config = config.get("engine", {})
        initial_cash = float(
            engine_config.get("cash", engine_config.get("initial_cash", 100_000.0))
        )
        engine = CtpRealtimeEngine(
            strategy=parse_strategy(args.strategy, parse_params(args.params)),
            trading_session=trading_session,
            market_data_session=market_data_session,
            initial_cash=initial_cash,
            risk_manager=risk_from_config(config),
            bar_frequency=args.bar_frequency,
        )
        if args.event_log_path:
            engine.enable_event_log(
                args.event_log_path,
                max_bytes=args.event_log_max_bytes,
                backup_count=args.event_log_backups,
            )
        if args.load_state:
            engine.load_state(args.state_path)
        engine.start(
            symbols=symbols,
            authenticate=not args.skip_auth,
            confirm_settlement=not args.skip_settlement_confirm,
            query_account=not args.skip_queries,
            query_positions=not args.skip_queries,
        )
        if args.simulate_tick:
            _simulate_ctp_market_data(market_data_session, symbols)
        if args.simulate_insert_error:
            _simulate_ctp_order_insert_error(trading_session, engine.orders)
        elif args.simulate_cancel_error:
            _simulate_ctp_cancel(trading_session, engine, reject=True)
        elif args.simulate_cancel:
            _simulate_ctp_cancel(trading_session, engine, reject=False)
        elif args.simulate_fill:
            _simulate_ctp_order_trade_returns(trading_session, engine.orders)
        if args.flush_bars:
            engine.flush_bars()
        if args.simulate_disconnect or args.simulate_reconnect:
            trading_session.callback_adapter.OnFrontDisconnected(4097)
            market_data_session.callback_adapter.OnFrontDisconnected(8193)
        if args.simulate_reconnect:
            trading_session.callback_adapter.OnFrontConnected()
            market_data_session.callback_adapter.OnFrontConnected()
        for _ in range(max(args.watchdog_checks, 0)):
            engine.check_watchdog(force=True)
        if args.reconcile:
            engine.reconcile(
                symbols=args.reconcile_symbols,
                start_time=args.reconcile_start_time,
                end_time=args.reconcile_end_time,
            )
        if args.save_state:
            engine.save_state(args.state_path)
        if args.event_log_csv:
            engine.export_event_log_csv(args.event_log_csv)
        print(
            json.dumps(
                engine.snapshot(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return

    raise ValueError(f"unknown command: {args.command}")


def _order_from_ctp_order_args(args: argparse.Namespace):
    from datetime import datetime

    from .models import Offset, Order, OrderType, Side

    offset_map = {
        "auto": Offset.AUTO,
        "open": Offset.OPEN,
        "close": Offset.CLOSE,
        "close-today": Offset.CLOSE_TODAY,
        "close-yesterday": Offset.CLOSE_YESTERDAY,
    }
    return Order(
        order_id=args.order_id,
        symbol=args.symbol,
        side=Side.BUY if args.side == "buy" else Side.SELL,
        quantity=args.quantity,
        submitted_at=datetime.now(),
        order_type=OrderType.MARKET if args.order_type == "market" else OrderType.LIMIT,
        offset=offset_map[args.offset],
        limit_price=args.limit_price,
    )


def _simulate_ctp_callbacks(session: CtpTradingSession) -> None:
    adapter = session.callback_adapter
    config = session.gateway.config
    adapter.on_rsp_user_login(
        {
            "BrokerID": config.broker_id,
            "UserID": config.user_id or config.investor_id,
            "TradingDay": "20240102",
        },
        {"ErrorID": 0, "ErrorMsg": ""},
        session.gateway.next_request_id(),
        True,
    )
    adapter.on_rsp_settlement_info_confirm(
        {
            "BrokerID": config.broker_id,
            "InvestorID": config.investor_id,
            "ConfirmDate": "20240102",
        },
        {"ErrorID": 0, "ErrorMsg": ""},
        session.gateway.next_request_id(),
        True,
    )
    adapter.on_rsp_qry_trading_account(
        {
            "Balance": 100000,
            "Available": 98000,
            "CurrMargin": 2000,
            "PositionProfit": 120,
            "CurrencyID": config.currency_id,
        },
        {"ErrorID": 0, "ErrorMsg": ""},
        session.gateway.next_request_id(),
        True,
    )
    position_request_id = session.gateway.next_request_id()
    adapter.on_rsp_qry_investor_position(
        {
            "InstrumentID": "RB2405",
            "PosiDirection": "2",
            "PositionDate": "1",
            "Position": 2,
            "PositionCost": 72000,
        },
        {"ErrorID": 0, "ErrorMsg": ""},
        position_request_id,
        True,
    )
    adapter.on_rtn_order(
        {
            "OrderRef": "000000000001",
            "InstrumentID": "RB2405",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "OrderPriceType": "2",
            "LimitPrice": 3600,
            "VolumeTotalOriginal": 2,
            "VolumeTraded": 2,
            "OrderStatus": "0",
            "InsertDate": "20240102",
            "InsertTime": "09:30:00",
        }
    )
    adapter.on_rtn_trade(
        {
            "TradeID": "SIMT0001",
            "OrderRef": "000000000001",
            "InstrumentID": "RB2405",
            "Direction": "0",
            "OffsetFlag": "0",
            "Price": 3600,
            "Volume": 2,
            "TradeDate": "20240102",
            "TradeTime": "09:30:01",
        }
    )


def _simulate_ctp_market_data(session: CtpMarketDataSession, symbols: list[str]) -> None:
    symbol = symbols[0]
    session.callback_adapter.on_rtn_depth_market_data(
        {
            "TradingDay": "20240102",
            "ActionDay": "20240102",
            "UpdateTime": "09:30:00",
            "UpdateMillisec": 500,
            "InstrumentID": symbol,
            "ExchangeID": session.gateway.contract_registry.for_symbol(symbol).exchange,
            "LastPrice": 3600,
            "Volume": 120,
            "Turnover": 4320000,
            "OpenInterest": 180000,
            "BidPrice1": 3599,
            "BidVolume1": 8,
            "AskPrice1": 3601,
            "AskVolume1": 10,
            "OpenPrice": 3580,
            "HighestPrice": 3610,
            "LowestPrice": 3575,
            "PreClosePrice": 3570,
        }
    )


def _simulate_ctp_order_trade_returns(session: CtpTradingSession, orders: list[Any]) -> None:
    for order in orders:
        requests = session.gateway.local_to_ctp.get(order.order_id, [])
        for request in requests:
            tick = session.gateway.ticks.get(request.instruction.symbol)
            price = (
                request.instruction.limit_price
                if request.instruction.limit_price is not None
                else tick.last_price if tick is not None else 0.0
            )
            session.callback_adapter.on_rtn_order(
                {
                    "OrderRef": request.order_ref,
                    "InstrumentID": request.instruction.symbol,
                    "Direction": request.field["Direction"],
                    "CombOffsetFlag": request.field["CombOffsetFlag"],
                    "OrderPriceType": request.field["OrderPriceType"],
                    "LimitPrice": price,
                    "VolumeTotalOriginal": request.field["VolumeTotalOriginal"],
                    "VolumeTraded": request.field["VolumeTotalOriginal"],
                    "OrderStatus": "0",
                    "InsertDate": "20240102",
                    "InsertTime": "09:30:01",
                }
            )
            session.callback_adapter.on_rtn_trade(
                {
                    "TradeID": f"SIMT{request.order_ref}",
                    "OrderRef": request.order_ref,
                    "InstrumentID": request.instruction.symbol,
                    "Direction": request.field["Direction"],
                    "OffsetFlag": request.field["CombOffsetFlag"],
                    "Price": price,
                    "Volume": request.field["VolumeTotalOriginal"],
                    "TradeDate": "20240102",
                    "TradeTime": "09:30:02",
                }
            )


def _simulate_ctp_order_insert_error(session: CtpTradingSession, orders: list[Any]) -> None:
    for order in orders:
        requests = session.gateway.local_to_ctp.get(order.order_id, [])
        for request in requests:
            session.callback_adapter.on_rsp_order_insert(
                {
                    "OrderRef": request.order_ref,
                    "InstrumentID": request.instruction.symbol,
                },
                {"ErrorID": 88, "ErrorMsg": "simulated order insert rejected"},
                request.request_id,
                True,
            )


def _simulate_ctp_cancel(
    session: CtpTradingSession,
    engine: CtpRealtimeEngine,
    reject: bool,
) -> None:
    for order in list(engine.orders):
        if order.status != OrderStatus.PENDING:
            continue
        engine.cancel_order(order.order_id)
        requests = session.gateway.local_to_ctp.get(order.order_id, [])
        if not requests:
            continue
        request = requests[0]
        cancel_request_id = session.events[-1].request_id
        if reject:
            session.callback_adapter.on_rsp_order_action(
                {
                    "OrderRef": request.order_ref,
                    "InstrumentID": request.instruction.symbol,
                },
                {"ErrorID": 49, "ErrorMsg": "simulated cancel rejected"},
                cancel_request_id,
                True,
            )
            return
        session.callback_adapter.on_rtn_order(
            {
                "OrderRef": request.order_ref,
                "InstrumentID": request.instruction.symbol,
                "Direction": request.field["Direction"],
                "CombOffsetFlag": request.field["CombOffsetFlag"],
                "OrderPriceType": request.field["OrderPriceType"],
                "LimitPrice": request.field["LimitPrice"],
                "VolumeTotalOriginal": request.field["VolumeTotalOriginal"],
                "VolumeTraded": 0,
                "OrderStatus": "5",
                "InsertDate": "20240102",
                "InsertTime": "09:30:01",
            }
        )
        return


def resolve_backtest_setup(args: argparse.Namespace, default_output: str) -> dict[str, Any]:
    config = load_json_config(args.config) if getattr(args, "config", None) else {}
    params = {
        **config.get("params", {}),
        **parse_params(getattr(args, "params", None)),
    }
    strategy_path = args.strategy or config.get("strategy", "sample:ma_cross")

    data_sources = normalize_data_sources(config.get("data"))
    if args.data:
        data_sources = normalize_data_sources(args.data)
        if args.symbol and len(data_sources) == 1:
            data_sources[0]["symbol"] = args.symbol
    if not data_sources:
        raise ValueError("backtest requires --data or data in config")

    engine_config = config.get("engine", {})
    cash = float(
        args.cash
        if args.cash is not None
        else engine_config.get("cash", engine_config.get("initial_cash", 100_000.0))
    )
    commission_rate = float(
        args.commission_rate
        if args.commission_rate is not None
        else engine_config.get("commission_rate", 0.0002)
    )
    slippage = float(
        args.slippage if args.slippage is not None else engine_config.get("slippage", 0.0)
    )
    execution_config = (
        execution_from_config(
            config,
            fallback_commission_rate=commission_rate,
            fallback_slippage=slippage,
        )
        if config
        else ExecutionConfig.from_legacy(commission_rate, slippage)
    )
    risk_manager = risk_from_config(config) if config else RiskManager()
    account_mode = account_mode_from_config(config) if config else "cash"
    daily_settlement = daily_settlement_from_config(config) if config else False
    contract_registry = contract_registry_from_config(config) if config else None
    output = args.output or config.get("output", default_output)

    if len(data_sources) == 1 and args.symbol:
        bars = load_bars_csv(data_sources[0]["path"], symbol=args.symbol)
    else:
        bars = load_bars_from_sources(data_sources)

    return {
        "bars": bars,
        "strategy": strategy_path,
        "params": params,
        "cash": cash,
        "commission_rate": commission_rate,
        "slippage": slippage,
        "execution_config": execution_config,
        "risk_manager": risk_manager,
        "account_mode": account_mode,
        "contract_registry": contract_registry,
        "daily_settlement": daily_settlement,
        "output": output,
        "config": config,
    }
