from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from .market_data import MARKET_COLUMNS, SETTLEMENT_COLUMNS, contract_spec_rows


POSITION_COLUMNS = [
    "trade_date",
    "exchange",
    "product",
    "contract",
    "rank",
    "metric",
    "member",
    "value",
    "change",
    "source_url",
    "fetched_at",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def date_range(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _records(frame: pd.DataFrame) -> list[tuple]:
    cleaned = frame[POSITION_COLUMNS].copy()
    cleaned = cleaned.where(pd.notna(cleaned), None)
    return [tuple(row) for row in cleaned.itertuples(index=False, name=None)]


def _column_records(frame: pd.DataFrame, columns: list[str]) -> list[tuple]:
    cleaned = frame[columns].copy()
    cleaned = cleaned.where(pd.notna(cleaned), None)
    return [tuple(row) for row in cleaned.itertuples(index=False, name=None)]


def _migrate_sync_tables(connection) -> None:
    """One-time migration: add an `exchange` column to the sync tables.

    Older schemas used `trade_date` as the sole primary key, so a single
    date could only carry one sync record even when both SHFE and CFFEX ran
    in the same incremental update. We recreate the tables with a composite
    `(trade_date, exchange)` key, backfilling existing rows with the
    exchange they most likely belonged to (default `'SHFE'`). Idempotent:
    tables already carrying the `exchange` column are left untouched.
    """
    definitions = {
        "sync_status": (
            "CREATE TABLE sync_status ("
            " trade_date TEXT NOT NULL,"
            " exchange TEXT NOT NULL DEFAULT 'SHFE',"
            " status TEXT NOT NULL,"
            " rows_count INTEGER NOT NULL DEFAULT 0,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " last_attempt_at TEXT NOT NULL,"
            " message TEXT NOT NULL DEFAULT '',"
            " source_url TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (trade_date, exchange)) WITHOUT ROWID"
        ),
        "market_sync_status": (
            "CREATE TABLE market_sync_status ("
            " trade_date TEXT NOT NULL,"
            " exchange TEXT NOT NULL DEFAULT 'SHFE',"
            " status TEXT NOT NULL,"
            " market_rows INTEGER NOT NULL DEFAULT 0,"
            " settlement_rows INTEGER NOT NULL DEFAULT 0,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " last_attempt_at TEXT NOT NULL,"
            " message TEXT NOT NULL DEFAULT '',"
            " source_url TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (trade_date, exchange)) WITHOUT ROWID"
        ),
    }
    for table, ddl in definitions.items():
        cols = [row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)]
        if not cols or "exchange" in cols:
            continue
        col_list = ", ".join(cols)
        connection.execute("ALTER TABLE %s RENAME TO _%s_old" % (table, table))
        connection.execute(ddl)
        connection.execute(
            "INSERT INTO %s (%s, exchange) SELECT %s, 'SHFE' FROM _%s_old"
            % (table, col_list, col_list, table)
        )
        connection.execute("DROP TABLE _%s_old" % table)


class PositionsDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.session() as connection:
            # Migrate old sync tables to the exchange-aware schema BEFORE the
            # executescript, because the index below references the new
            # `exchange` column. On fresh databases this is a no-op.
            _migrate_sync_tables(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    trade_date TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    product TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 20),
                    metric TEXT NOT NULL CHECK (metric IN ('volume', 'long', 'short')),
                    member TEXT NOT NULL,
                    value REAL,
                    change REAL,
                    source_url TEXT,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, exchange, contract, rank, metric)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS idx_positions_date_metric
                    ON positions (trade_date, metric);
                CREATE INDEX IF NOT EXISTS idx_positions_member_product_date
                    ON positions (member, product, trade_date);
                CREATE INDEX IF NOT EXISTS idx_positions_product_date
                    ON positions (product, trade_date);

                CREATE TABLE IF NOT EXISTS sync_status (
                    trade_date TEXT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'SHFE',
                    status TEXT NOT NULL,
                    rows_count INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (trade_date, exchange)
                );
                CREATE INDEX IF NOT EXISTS idx_sync_status_exchange
                    ON sync_status (exchange, status);

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contract_specs (
                    exchange TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    contract_multiplier REAL NOT NULL CHECK (contract_multiplier > 0),
                    multiplier_unit TEXT NOT NULL DEFAULT '',
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    source_url TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (exchange, product_code, effective_from)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS contract_daily_market (
                    trade_date TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    settlement_price REAL,
                    pre_settlement_price REAL,
                    volume REAL,
                    open_interest REAL,
                    open_interest_change REAL,
                    turnover REAL,
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, exchange, contract)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS idx_contract_daily_market_product_date
                    ON contract_daily_market (product_code, trade_date);
                CREATE INDEX IF NOT EXISTS idx_contract_daily_market_date_notional
                    ON contract_daily_market (trade_date, open_interest, settlement_price);

                CREATE TABLE IF NOT EXISTS contract_settlement_params (
                    trade_date TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    settlement_price REAL,
                    spec_long_margin_rate REAL,
                    spec_short_margin_rate REAL,
                    hedge_long_margin_rate REAL,
                    hedge_short_margin_rate REAL,
                    trade_fee_ratio REAL,
                    close_today_fee_ratio REAL,
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, exchange, contract)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS market_sync_status (
                    trade_date TEXT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'SHFE',
                    status TEXT NOT NULL,
                    market_rows INTEGER NOT NULL DEFAULT 0,
                    settlement_rows INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (trade_date, exchange)
                );
                CREATE INDEX IF NOT EXISTS idx_market_sync_status_exchange
                    ON market_sync_status (exchange, status);

                CREATE VIEW IF NOT EXISTS contract_market_value AS
                SELECT
                    m.trade_date,
                    m.exchange,
                    m.product_code,
                    m.product_name,
                    m.contract,
                    m.settlement_price,
                    m.volume,
                    m.open_interest,
                    m.open_interest_change,
                    s.contract_multiplier,
                    s.multiplier_unit,
                    p.spec_long_margin_rate,
                    p.spec_short_margin_rate,
                    p.hedge_long_margin_rate,
                    p.hedge_short_margin_rate,
                    m.open_interest * m.settlement_price * s.contract_multiplier
                        AS notional_value,
                    m.open_interest * m.settlement_price * s.contract_multiplier
                        * (COALESCE(p.spec_long_margin_rate, 0) + COALESCE(p.spec_short_margin_rate, 0))
                        AS estimated_spec_margin,
                    m.open_interest * m.settlement_price * s.contract_multiplier
                        * (COALESCE(p.hedge_long_margin_rate, 0) + COALESCE(p.hedge_short_margin_rate, 0))
                        AS estimated_hedge_margin
                FROM contract_daily_market AS m
                JOIN contract_specs AS s
                  ON s.exchange = m.exchange
                 AND s.product_code = m.product_code
                 AND m.trade_date >= s.effective_from
                 AND (s.effective_to IS NULL OR m.trade_date <= s.effective_to)
                LEFT JOIN contract_settlement_params AS p
                  ON p.trade_date = m.trade_date
                 AND p.exchange = m.exchange
                 AND p.contract = m.contract;
                """
            )
            specs = contract_spec_rows()
            connection.executemany(
                """
                INSERT INTO contract_specs (
                    exchange, product_code, product_name, contract_multiplier,
                    multiplier_unit, effective_from, effective_to, source_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, product_code, effective_from) DO UPDATE SET
                    product_name=excluded.product_name,
                    contract_multiplier=excluded.contract_multiplier,
                    multiplier_unit=excluded.multiplier_unit,
                    effective_to=excluded.effective_to,
                    source_url=excluded.source_url,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        item["exchange"],
                        item["product_code"],
                        item["product_name"],
                        item["contract_multiplier"],
                        item["multiplier_unit"],
                        item["effective_from"],
                        item["effective_to"],
                        item["source_url"],
                        item["updated_at"],
                    )
                    for item in specs
                ],
            )

    def upsert_frame(self, frame: pd.DataFrame, replace_trade_date: bool = False) -> int:
        self.initialize()
        if frame.empty:
            return 0
        working = frame.copy()
        working = working[
            working["rank"].between(1, 20)
            & working["member"].fillna("").astype(str).str.strip().ne("")
            & working["metric"].isin(["volume", "long", "short"])
        ]
        if working.empty:
            return 0
        working["exchange"] = working["exchange"].fillna("SHFE")
        working["rank"] = working["rank"].astype(int)
        working["member"] = working["member"].astype(str).str.strip()
        working["fetched_at"] = working["fetched_at"].fillna(now_iso())

        sql = """
            INSERT INTO positions (
                trade_date, exchange, product, contract, rank, metric, member,
                value, change, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, exchange, contract, rank, metric)
            DO UPDATE SET
                product=excluded.product,
                member=excluded.member,
                value=excluded.value,
                change=excluded.change,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at
        """
        with self.session() as connection:
            if replace_trade_date:
                exchanges = working["exchange"].dropna().astype(str).unique()
                for trade_date in working["trade_date"].drop_duplicates():
                    for exchange in exchanges:
                        connection.execute(
                            "DELETE FROM positions WHERE trade_date = ? AND exchange = ?",
                            (trade_date, exchange),
                        )
            connection.executemany(sql, _records(working))
        return len(working)

    def upsert_market_day(
        self,
        market: pd.DataFrame,
        settlement: pd.DataFrame,
        replace_trade_date: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        market_working = market.copy()
        settlement_working = settlement.copy()
        if not market_working.empty:
            market_working = market_working[
                market_working["contract"].fillna("").astype(str).str.strip().ne("")
                & market_working["settlement_price"].notna()
                & market_working["open_interest"].notna()
            ]
        if not settlement_working.empty:
            settlement_working = settlement_working[
                settlement_working["contract"].fillna("").astype(str).str.strip().ne("")
            ]

        market_sql = """
            INSERT INTO contract_daily_market (
                trade_date, exchange, product_code, product_name, contract,
                open_price, high_price, low_price, close_price, settlement_price,
                pre_settlement_price, volume, open_interest, open_interest_change,
                turnover, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, exchange, contract) DO UPDATE SET
                product_code=excluded.product_code,
                product_name=excluded.product_name,
                open_price=excluded.open_price,
                high_price=excluded.high_price,
                low_price=excluded.low_price,
                close_price=excluded.close_price,
                settlement_price=excluded.settlement_price,
                pre_settlement_price=excluded.pre_settlement_price,
                volume=excluded.volume,
                open_interest=excluded.open_interest,
                open_interest_change=excluded.open_interest_change,
                turnover=excluded.turnover,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at
        """
        settlement_sql = """
            INSERT INTO contract_settlement_params (
                trade_date, exchange, product_code, contract, settlement_price,
                spec_long_margin_rate, spec_short_margin_rate,
                hedge_long_margin_rate, hedge_short_margin_rate,
                trade_fee_ratio, close_today_fee_ratio, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, exchange, contract) DO UPDATE SET
                product_code=excluded.product_code,
                settlement_price=excluded.settlement_price,
                spec_long_margin_rate=excluded.spec_long_margin_rate,
                spec_short_margin_rate=excluded.spec_short_margin_rate,
                hedge_long_margin_rate=excluded.hedge_long_margin_rate,
                hedge_short_margin_rate=excluded.hedge_short_margin_rate,
                trade_fee_ratio=excluded.trade_fee_ratio,
                close_today_fee_ratio=excluded.close_today_fee_ratio,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at
        """
        trade_dates = set()
        for frame in (market_working, settlement_working):
            if not frame.empty:
                trade_dates.update(frame["trade_date"].dropna().astype(str).unique())
        with self.session() as connection:
            if replace_trade_date:
                # Only delete rows for the specific exchange(s) + date(s) being
                # upserted. Previous code wiped ALL exchanges for the date,
                # causing CFFEX upserts to destroy SHFE history (and vice versa).
                exchanges = set()
                for frame in (market_working, settlement_working):
                    if not frame.empty:
                        exchanges.update(frame["exchange"].dropna().astype(str).unique())
                for trade_date in trade_dates:
                    for exchange in exchanges:
                        connection.execute(
                            "DELETE FROM contract_daily_market WHERE trade_date = ? AND exchange = ?",
                            (trade_date, exchange),
                        )
                        connection.execute(
                            "DELETE FROM contract_settlement_params WHERE trade_date = ? AND exchange = ?",
                            (trade_date, exchange),
                        )
            if not market_working.empty:
                connection.executemany(
                    market_sql,
                    _column_records(market_working, MARKET_COLUMNS),
                )
            if not settlement_working.empty:
                connection.executemany(
                    settlement_sql,
                    _column_records(settlement_working, SETTLEMENT_COLUMNS),
                )
        return {"market_rows": len(market_working), "settlement_rows": len(settlement_working)}

    def mark_market_sync(
        self,
        trade_date: date | str,
        status: str,
        market_rows: int = 0,
        settlement_rows: int = 0,
        message: str = "",
        source_url: str = "",
        exchange: str = "SHFE",
    ) -> None:
        self.initialize()
        value = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO market_sync_status (
                    trade_date, exchange, status, market_rows, settlement_rows, attempts,
                    last_attempt_at, message, source_url
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(trade_date, exchange) DO UPDATE SET
                    status=excluded.status,
                    market_rows=excluded.market_rows,
                    settlement_rows=excluded.settlement_rows,
                    attempts=market_sync_status.attempts + 1,
                    last_attempt_at=excluded.last_attempt_at,
                    message=excluded.message,
                    source_url=excluded.source_url
                """,
                (
                    value,
                    exchange,
                    status,
                    market_rows,
                    settlement_rows,
                    now_iso(),
                    message[:1000],
                    source_url,
                ),
            )

    def missing_market_days(
        self,
        start_date: date,
        end_date: date,
        no_data_attempts: int = 3,
        trading_days: set[str] | None = None,
    ) -> list[date]:
        self.initialize()
        with self.session() as connection:
            stored = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT trade_date
                    FROM contract_daily_market
                    WHERE trade_date BETWEEN ? AND ?
                    """,
                    (start_date.isoformat(), end_date.isoformat()),
                )
            }
            completed_no_data = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT trade_date
                    FROM market_sync_status
                    WHERE trade_date BETWEEN ? AND ?
                      AND status = 'no_data'
                      AND attempts >= ?
                    """,
                    (start_date.isoformat(), end_date.isoformat(), no_data_attempts),
                )
            }
        return [
            day
            for day in date_range(start_date, end_date)
            if day.weekday() < 5
            and (trading_days is None or day.strftime("%Y%m%d") in trading_days)
            and day.isoformat() not in stored
            and day.isoformat() not in completed_no_data
        ]

    def mark_sync(
        self,
        trade_date: date | str,
        status: str,
        rows_count: int = 0,
        message: str = "",
        source_url: str = "",
        exchange: str = "SHFE",
    ) -> None:
        self.initialize()
        value = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO sync_status (
                    trade_date, exchange, status, rows_count, attempts, last_attempt_at, message, source_url
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(trade_date, exchange) DO UPDATE SET
                    status=excluded.status,
                    rows_count=excluded.rows_count,
                    attempts=sync_status.attempts + 1,
                    last_attempt_at=excluded.last_attempt_at,
                    message=excluded.message,
                    source_url=excluded.source_url
                """,
                (value, exchange, status, rows_count, now_iso(), message[:1000], source_url),
            )

    def missing_weekdays(
        self,
        start_date: date,
        end_date: date,
        no_data_attempts: int = 3,
        trading_days: set[str] | None = None,
    ) -> list[date]:
        self.initialize()
        with self.session() as connection:
            stored = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT trade_date FROM positions WHERE trade_date BETWEEN ? AND ?",
                    (start_date.isoformat(), end_date.isoformat()),
                )
            }
            completed_no_data = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT trade_date
                    FROM sync_status
                    WHERE trade_date BETWEEN ? AND ?
                      AND status = 'no_data'
                      AND attempts >= ?
                    """,
                    (start_date.isoformat(), end_date.isoformat(), no_data_attempts),
                )
            }
        return [
            day
            for day in date_range(start_date, end_date)
            if day.weekday() < 5
            and (trading_days is None or day.strftime("%Y%m%d") in trading_days)
            and day.isoformat() not in stored
            and day.isoformat() not in completed_no_data
        ]

    def missing_weekdays_for_exchange(
            self,
            start_date: date,
            end_date: date,
            exchange: str,
            no_data_attempts: int = 3,
            trading_days: set[str] | None = None,
        ) -> list[date]:
        """Like missing_weekdays but scoped to a single exchange.

        Used by the multi-exchange incremental flow so that CFFEX backfill
        is not blocked by SHFE having already stored rows for the same date.
        Also honours per-exchange `no_data` marks recorded by
        :meth:mark_sync, so genuine no-data dates (holidays, exchange
        outages) are not retried indefinitely.
        """
        self.initialize()
        with self.session() as connection:
            stored = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT trade_date FROM positions "
                    "WHERE trade_date BETWEEN ? AND ? AND exchange=?",
                    (start_date.isoformat(), end_date.isoformat(), exchange),
                )
            }
            completed_no_data = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT trade_date
                    FROM sync_status
                    WHERE trade_date BETWEEN ? AND ?
                      AND exchange=?
                      AND status = 'no_data'
                      AND attempts >= ?
                    """,
                    (start_date.isoformat(), end_date.isoformat(), exchange, no_data_attempts),
                )
            }
        return [
            day
            for day in date_range(start_date, end_date)
            if day.weekday() < 5
            and (trading_days is None or day.strftime("%Y%m%d") in trading_days)
            and day.isoformat() not in stored
            and day.isoformat() not in completed_no_data
        ]

    def missing_market_days_for_exchange(
            self,
            start_date: date,
            end_date: date,
            exchange: str,
            no_data_attempts: int = 3,
            trading_days: set[str] | None = None,
        ) -> list[date]:
        """Like missing_market_days but scoped to a single exchange.

        Honours per-exchange `no_data` marks recorded by
        :meth:mark_market_sync so genuine no-data dates are not retried
        indefinitely (the root cause of the earlier CFFEX infinite-retry
        backfill runs).
        """
        self.initialize()
        with self.session() as connection:
            stored = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT trade_date FROM contract_daily_market "
                    "WHERE trade_date BETWEEN ? AND ? AND exchange=?",
                    (start_date.isoformat(), end_date.isoformat(), exchange),
                )
            }
            completed_no_data = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT trade_date
                    FROM market_sync_status
                    WHERE trade_date BETWEEN ? AND ?
                      AND exchange=?
                      AND status = 'no_data'
                      AND attempts >= ?
                    """,
                    (start_date.isoformat(), end_date.isoformat(), exchange, no_data_attempts),
                )
            }
        return [
            day
            for day in date_range(start_date, end_date)
            if day.weekday() < 5
            and (trading_days is None or day.strftime("%Y%m%d") in trading_days)
            and day.isoformat() not in stored
            and day.isoformat() not in completed_no_data
        ]

    def import_csv(self, path: str | Path, chunksize: int = 100_000) -> int:
        self.initialize()
        imported = 0
        imported_dates: set[str] = set()
        for chunk in pd.read_csv(path, chunksize=chunksize):
            if "exchange" in chunk.columns:
                chunk = chunk[chunk["exchange"].fillna("SHFE").eq("SHFE")]
            if chunk.empty:
                continue
            imported += self.upsert_frame(chunk)
            imported_dates.update(chunk["trade_date"].dropna().astype(str).unique())
        for trade_date in imported_dates:
            self.mark_sync(trade_date, "imported", message=f"imported from {Path(path).name}")
        return imported

    def status(self) -> dict:
        self.initialize()
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT trade_date) AS trading_days,
                    MIN(trade_date) AS earliest_date,
                    MAX(trade_date) AS latest_date,
                    COUNT(DISTINCT product) AS products,
                    COUNT(DISTINCT contract) AS contracts,
                    COUNT(DISTINCT member) AS members
                FROM positions
                """
            ).fetchone()
            statuses = {
                item["status"]: item["count"]
                for item in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM sync_status GROUP BY status"
                )
            }
            market = connection.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT trade_date) AS trading_days,
                    MIN(trade_date) AS earliest_date,
                    MAX(trade_date) AS latest_date,
                    COUNT(DISTINCT product_code) AS products,
                    COUNT(DISTINCT contract) AS contracts
                FROM contract_daily_market
                """
            ).fetchone()
            market_statuses = {
                item["status"]: item["count"]
                for item in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM market_sync_status GROUP BY status"
                )
            }
        result = dict(row)
        result["sync_status"] = statuses
        result["market"] = dict(market)
        result["market"]["sync_status"] = market_statuses
        result["database"] = str(self.path.resolve())
        result["size_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return result

    def dashboard_payload(self, days: int | None = None) -> dict:
        self.initialize()
        status = self.status()
        latest_date = status["latest_date"]
        if not latest_date:
            return {
                "rowCount": 0,
                "daily": [],
                "members": [],
                "products": [],
                "contracts": [],
                "memberDaily": [],
                "latestMemberProduct": [],
                "marketDaily": [],
                "marketProducts": [],
                "marketContracts": [],
                "marketLatestDate": "",
                "productDailyMarket": [],
                "latestDate": "",
            }

        if days:
            start_date = (date.fromisoformat(latest_date) - timedelta(days=days - 1)).isoformat()
        else:
            start_date = status["earliest_date"]
        params = (start_date, latest_date)

        with self.session() as connection:
            daily_long = pd.read_sql_query(
                """
                SELECT trade_date, metric, SUM(value) AS value
                FROM positions
                WHERE trade_date BETWEEN ? AND ?
                GROUP BY trade_date, metric
                ORDER BY trade_date
                """,
                connection,
                params=params,
            )
            member_daily = pd.read_sql_query(
                """
                SELECT trade_date, member, product,
                    SUM(CASE WHEN metric='volume' THEN value ELSE 0 END) AS volume,
                    SUM(CASE WHEN metric='long' THEN value ELSE 0 END) AS long,
                    SUM(CASE WHEN metric='short' THEN value ELSE 0 END) AS short
                FROM positions
                WHERE trade_date BETWEEN ? AND ?
                GROUP BY trade_date, member, product
                ORDER BY trade_date, member, product
                """,
                connection,
                params=params,
            )
            latest_member_long = pd.read_sql_query(
                """
                SELECT metric, member, SUM(value) AS value
                FROM positions
                WHERE trade_date = ?
                GROUP BY metric, member
                ORDER BY metric, value DESC
                """,
                connection,
                params=(latest_date,),
            )
            products = pd.read_sql_query(
                """
                SELECT product,
                    SUM(CASE WHEN metric='volume' THEN value ELSE 0 END) AS volume,
                    SUM(CASE WHEN metric='long' THEN value ELSE 0 END) AS long,
                    SUM(CASE WHEN metric='short' THEN value ELSE 0 END) AS short
                FROM positions
                WHERE trade_date = ?
                GROUP BY product
                ORDER BY long + short DESC
                """,
                connection,
                params=(latest_date,),
            )
            contracts = pd.read_sql_query(
                """
                SELECT product, contract,
                    SUM(CASE WHEN metric='volume' THEN value ELSE 0 END) AS volume,
                    SUM(CASE WHEN metric='long' THEN value ELSE 0 END) AS long,
                    SUM(CASE WHEN metric='short' THEN value ELSE 0 END) AS short
                FROM positions
                WHERE trade_date = ?
                GROUP BY product, contract
                ORDER BY long + short DESC
                LIMIT 30
                """,
                connection,
                params=(latest_date,),
            )
            latest_member_product = pd.read_sql_query(
                """
                SELECT member, product,
                    SUM(CASE WHEN metric='volume' THEN value ELSE 0 END) AS volume,
                    SUM(CASE WHEN metric='long' THEN value ELSE 0 END) AS long,
                    SUM(CASE WHEN metric='short' THEN value ELSE 0 END) AS short
                FROM positions
                WHERE trade_date = ?
                GROUP BY member, product
                ORDER BY member, product
                """,
                connection,
                params=(latest_date,),
            )
            row_count = connection.execute(
                "SELECT COUNT(*) FROM positions WHERE trade_date BETWEEN ? AND ?",
                params,
            ).fetchone()[0]
            market_latest_date = status["market"]["latest_date"]
            if market_latest_date:
                market_params = (start_date, market_latest_date)
                market_daily = pd.read_sql_query(
                    """
                    SELECT trade_date,
                        SUM(open_interest) AS open_interest,
                        SUM(notional_value) AS notional_value,
                        SUM(estimated_spec_margin) AS estimated_spec_margin,
                        SUM(estimated_hedge_margin) AS estimated_hedge_margin
                    FROM contract_market_value
                    WHERE trade_date BETWEEN ? AND ?
                    GROUP BY trade_date
                    ORDER BY trade_date
                    """,
                    connection,
                    params=market_params,
                )
                market_products = pd.read_sql_query(
                    """
                    SELECT product_code, product_name AS product,
                        SUM(open_interest) AS open_interest,
                        SUM(notional_value) AS notional_value,
                        SUM(estimated_spec_margin) AS estimated_spec_margin,
                        SUM(estimated_hedge_margin) AS estimated_hedge_margin
                    FROM contract_market_value
                    WHERE trade_date = ?
                    GROUP BY product_code, product_name
                    ORDER BY notional_value DESC
                    """,
                    connection,
                    params=(market_latest_date,),
                )
                market_contracts = pd.read_sql_query(
                    """
                    SELECT exchange, product_code, product_name AS product, contract,
                        settlement_price, open_interest, contract_multiplier,
                        multiplier_unit, spec_long_margin_rate, spec_short_margin_rate,
                        notional_value, estimated_spec_margin, estimated_hedge_margin
                    FROM contract_market_value
                    WHERE trade_date = ?
                    ORDER BY notional_value DESC
                    LIMIT 50
                    """,
                    connection,
                    params=(market_latest_date,),
                )
                product_daily_market = pd.read_sql_query(
                    """
                    SELECT trade_date,
                        product_name AS product,
                        product_code,
                        SUM(open_interest) AS open_interest,
                        SUM(open_interest * settlement_price) AS oi_times_price,
                        CASE WHEN SUM(open_interest) > 0
                            THEN SUM(open_interest * settlement_price) * 1.0 / SUM(open_interest)
                            ELSE NULL END AS settlement_price
                    FROM contract_market_value
                    WHERE trade_date BETWEEN ? AND ?
                      AND product_name IS NOT NULL
                      AND product_name != ''
                    GROUP BY trade_date, product_name, product_code
                    ORDER BY trade_date, product_name
                    """,
                    connection,
                    params=market_params,
                )
            else:
                market_daily = pd.DataFrame()
                market_products = pd.DataFrame()
                market_contracts = pd.DataFrame()
                product_daily_market = pd.DataFrame()

        daily = (
            daily_long.pivot(index="trade_date", columns="metric", values="value")
            .reset_index()
            .fillna(0)
        )
        for column in ["volume", "long", "short"]:
            if column not in daily.columns:
                daily[column] = 0
        daily["net_long_short"] = daily["long"] - daily["short"]

        latest_member_long["rank"] = latest_member_long.groupby("metric")["value"].rank(
            method="first", ascending=False
        )
        latest_member_long = latest_member_long[latest_member_long["rank"] <= 30]
        member_daily["net"] = member_daily["long"] - member_daily["short"]
        products["open_interest_total"] = products["long"] + products["short"]
        contracts["open_interest_total"] = contracts["long"] + contracts["short"]

        def rows(frame: pd.DataFrame) -> list[dict]:
            return json.loads(frame.to_json(orient="records", force_ascii=False))

        return {
            "rowCount": int(row_count),
            "daily": rows(daily),
            "members": rows(latest_member_long),
            "products": rows(products),
            "contracts": rows(contracts),
            "memberDaily": rows(member_daily),
            "latestMemberProduct": rows(latest_member_product),
            "marketDaily": rows(market_daily),
            "marketProducts": rows(market_products),
            "marketContracts": rows(market_contracts),
            "productDailyMarket": rows(product_daily_market),
            "marketLatestDate": market_latest_date or "",
            "latestDate": latest_date,
            "startDate": start_date,
        }

    def member_series(
        self,
        member: str,
        product: str = "all",
        metric: str = "long",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        if metric not in {"long", "short", "volume", "net"}:
            raise ValueError(f"unsupported metric: {metric}")
        status = self.status()
        start = start_date or status["earliest_date"]
        end = end_date or status["latest_date"]
        if not start or not end:
            return []

        product_sql = "" if product == "all" else " AND product = ?"
        params: list[str] = [start, end, member]
        if product != "all":
            params.append(product)
        with self.session() as connection:
            dates = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT trade_date FROM positions WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (start, end),
                )
            ]
            rows = connection.execute(
                f"""
                SELECT trade_date,
                    SUM(CASE WHEN metric='volume' THEN value ELSE 0 END) AS volume,
                    SUM(CASE WHEN metric='long' THEN value ELSE 0 END) AS long,
                    SUM(CASE WHEN metric='short' THEN value ELSE 0 END) AS short
                FROM positions
                WHERE trade_date BETWEEN ? AND ? AND member = ? {product_sql}
                GROUP BY trade_date
                """,
                params,
            ).fetchall()
        values = {row["trade_date"]: dict(row) for row in rows}
        result = []
        for trade_date in dates:
            row = values.get(trade_date, {"volume": 0, "long": 0, "short": 0})
            value = (row.get("long") or 0) - (row.get("short") or 0) if metric == "net" else row.get(metric) or 0
            result.append({"trade_date": trade_date, "value": value})
        return result
