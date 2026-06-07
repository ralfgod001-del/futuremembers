from __future__ import annotations

import importlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from time import monotonic, sleep
from typing import Any, Callable, Iterable, Mapping, Protocol

from .futures import ContractRegistry, ContractSpec, FuturesPosition
from .models import Offset, Order, OrderStatus, OrderType, Side, Tick, Trade


CTP_DIRECTION_BUY = "0"
CTP_DIRECTION_SELL = "1"

CTP_ORDER_PRICE_TYPE_ANY = "1"
CTP_ORDER_PRICE_TYPE_LIMIT = "2"

CTP_OFFSET_OPEN = "0"
CTP_OFFSET_CLOSE = "1"
CTP_OFFSET_FORCE_CLOSE = "2"
CTP_OFFSET_CLOSE_TODAY = "3"
CTP_OFFSET_CLOSE_YESTERDAY = "4"

CTP_HEDGE_SPECULATION = "1"

CTP_TIME_CONDITION_IOC = "1"
CTP_TIME_CONDITION_GFD = "3"
CTP_VOLUME_CONDITION_ANY = "1"
CTP_VOLUME_CONDITION_COMPLETE = "3"
CTP_CONTINGENT_IMMEDIATELY = "1"
CTP_FORCE_CLOSE_NOT = "0"
CTP_ACTION_DELETE = "0"

CTP_ORDER_STATUS_ALL_TRADED = "0"
CTP_ORDER_STATUS_PART_TRADED_QUEUEING = "1"
CTP_ORDER_STATUS_PART_TRADED_NOT_QUEUEING = "2"
CTP_ORDER_STATUS_NO_TRADE_QUEUEING = "3"
CTP_ORDER_STATUS_NO_TRADE_NOT_QUEUEING = "4"
CTP_ORDER_STATUS_CANCELED = "5"
CTP_ORDER_STATUS_UNKNOWN = "a"
CTP_ORDER_STATUS_NOT_TOUCHED = "b"
CTP_ORDER_STATUS_TOUCHED = "c"

CTP_POSITION_DIRECTION_NET = "1"
CTP_POSITION_DIRECTION_LONG = "2"
CTP_POSITION_DIRECTION_SHORT = "3"
CTP_POSITION_DATE_TODAY = "1"
CTP_POSITION_DATE_HISTORY = "2"

EXCHANGES_WITH_CLOSE_TODAY = {"SHFE", "INE"}


class CtpGatewayError(RuntimeError):
    pass


class CtpRequestTimeoutError(CtpGatewayError):
    pass


class CtpTraderApiProtocol(Protocol):
    def req_order_insert(self, field: dict[str, Any], request_id: int) -> int:
        ...

    def req_order_action(self, field: dict[str, Any], request_id: int) -> int:
        ...


class CtpTransportProtocol(CtpTraderApiProtocol, Protocol):
    def connect(self, config: "CtpConnectionConfig") -> int:
        ...

    def authenticate(self, field: dict[str, Any], request_id: int) -> int:
        ...

    def login(self, field: dict[str, Any], request_id: int) -> int:
        ...

    def confirm_settlement(self, field: dict[str, Any], request_id: int) -> int:
        ...

    def query_account(self, field: dict[str, Any], request_id: int) -> Mapping[str, Any] | int | None:
        ...

    def query_positions(
        self,
        field: dict[str, Any],
        request_id: int,
    ) -> Iterable[Mapping[str, Any]] | int | None:
        ...

    def query_orders(
        self,
        field: dict[str, Any],
        request_id: int,
    ) -> Iterable[Mapping[str, Any]] | int | None:
        ...

    def query_trades(
        self,
        field: dict[str, Any],
        request_id: int,
    ) -> Iterable[Mapping[str, Any]] | int | None:
        ...


class CtpMarketDataTransportProtocol(Protocol):
    def connect(self, config: "CtpConnectionConfig") -> int:
        ...

    def login(self, field: dict[str, Any], request_id: int) -> int:
        ...

    def subscribe_market_data(self, instruments: Iterable[str]) -> int:
        ...

    def unsubscribe_market_data(self, instruments: Iterable[str]) -> int:
        ...


@dataclass(frozen=True)
class CtpConnectionConfig:
    broker_id: str
    investor_id: str
    user_id: str = ""
    password: str = ""
    app_id: str = ""
    auth_code: str = ""
    product_info: str = ""
    front: str = ""
    md_front: str = ""
    flow_path: str = "flow/ctp"
    currency_id: str = "CNY"
    transport_module: str = ""
    trader_api_factory: str = "CreateFtdcTraderApi"
    md_transport_module: str = ""
    md_api_factory: str = "CreateFtdcMdApi"
    auth_required: bool = True
    settlement_confirm_required: bool = True
    query_account_on_start: bool = True
    query_positions_on_start: bool = True
    lifecycle_timeout: float = 5.0
    wait_for_lifecycle_callbacks: bool = True
    market_data_timeout: float = 5.0
    wait_for_market_data_callbacks: bool = True
    query_timeout: float = 5.0
    wait_for_query_callbacks: bool = True
    auto_recover_on_front_connected: bool = True
    auto_resubscribe_on_front_connected: bool = True
    watchdog_check_interval: float = 5.0
    watchdog_initial_backoff: float = 1.0
    watchdog_max_backoff: float = 30.0
    watchdog_backoff_multiplier: float = 2.0
    watchdog_max_recovery_attempts: int = 3
    auto_reconcile_after_watchdog_recovery: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "CtpConnectionConfig":
        source = raw or {}
        investor_id = str(source.get("investor_id", source.get("InvestorID", "")))
        return cls(
            broker_id=str(source.get("broker_id", source.get("BrokerID", ""))),
            investor_id=investor_id,
            user_id=str(source.get("user_id", source.get("UserID", investor_id))),
            password=str(source.get("password", source.get("Password", ""))),
            app_id=str(source.get("app_id", source.get("AppID", ""))),
            auth_code=str(source.get("auth_code", source.get("AuthCode", ""))),
            product_info=str(source.get("product_info", source.get("ProductInfo", ""))),
            front=str(source.get("front", source.get("Front", ""))),
            md_front=str(source.get("md_front", source.get("MdFront", ""))),
            flow_path=str(source.get("flow_path", source.get("FlowPath", "flow/ctp"))),
            currency_id=str(source.get("currency_id", source.get("CurrencyID", "CNY"))),
            transport_module=str(source.get("transport_module", source.get("TransportModule", ""))),
            trader_api_factory=str(
                source.get("trader_api_factory", source.get("TraderApiFactory", "CreateFtdcTraderApi"))
            ),
            md_transport_module=str(source.get("md_transport_module", source.get("MdTransportModule", ""))),
            md_api_factory=str(source.get("md_api_factory", source.get("MdApiFactory", "CreateFtdcMdApi"))),
            auth_required=bool(source.get("auth_required", source.get("AuthRequired", True))),
            settlement_confirm_required=bool(
                source.get(
                    "settlement_confirm_required",
                    source.get("SettlementConfirmRequired", True),
                )
            ),
            query_account_on_start=bool(
                source.get("query_account_on_start", source.get("QueryAccountOnStart", True))
            ),
            query_positions_on_start=bool(
                source.get("query_positions_on_start", source.get("QueryPositionsOnStart", True))
            ),
            lifecycle_timeout=float(source.get("lifecycle_timeout", source.get("LifecycleTimeout", 5.0))),
            wait_for_lifecycle_callbacks=bool(
                source.get(
                    "wait_for_lifecycle_callbacks",
                    source.get("WaitForLifecycleCallbacks", True),
                )
            ),
            market_data_timeout=float(source.get("market_data_timeout", source.get("MarketDataTimeout", 5.0))),
            wait_for_market_data_callbacks=bool(
                source.get(
                    "wait_for_market_data_callbacks",
                    source.get("WaitForMarketDataCallbacks", True),
                )
            ),
            query_timeout=float(source.get("query_timeout", source.get("QueryTimeout", 5.0))),
            wait_for_query_callbacks=bool(
                source.get(
                    "wait_for_query_callbacks",
                    source.get("WaitForQueryCallbacks", True),
                )
            ),
            auto_recover_on_front_connected=bool(
                source.get(
                    "auto_recover_on_front_connected",
                    source.get("AutoRecoverOnFrontConnected", True),
                )
            ),
            auto_resubscribe_on_front_connected=bool(
                source.get(
                    "auto_resubscribe_on_front_connected",
                    source.get("AutoResubscribeOnFrontConnected", True),
                )
            ),
            watchdog_check_interval=float(
                source.get("watchdog_check_interval", source.get("WatchdogCheckInterval", 5.0))
            ),
            watchdog_initial_backoff=float(
                source.get("watchdog_initial_backoff", source.get("WatchdogInitialBackoff", 1.0))
            ),
            watchdog_max_backoff=float(
                source.get("watchdog_max_backoff", source.get("WatchdogMaxBackoff", 30.0))
            ),
            watchdog_backoff_multiplier=float(
                source.get("watchdog_backoff_multiplier", source.get("WatchdogBackoffMultiplier", 2.0))
            ),
            watchdog_max_recovery_attempts=int(
                source.get("watchdog_max_recovery_attempts", source.get("WatchdogMaxRecoveryAttempts", 3))
            ),
            auto_reconcile_after_watchdog_recovery=bool(
                source.get(
                    "auto_reconcile_after_watchdog_recovery",
                    source.get("AutoReconcileAfterWatchdogRecovery", True),
                )
            ),
        )


@dataclass(frozen=True)
class CtpLifecycleEvent:
    timestamp: datetime
    event_type: str
    ok: bool
    request_id: int = 0
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CtpCallbackEvent:
    timestamp: datetime
    event_type: str
    ok: bool
    request_id: int = 0
    is_last: bool = True
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    rsp_info: dict[str, Any] = field(default_factory=dict)


class CtpEventQueue:
    def __init__(self) -> None:
        self._events: deque[CtpCallbackEvent] = deque()
        self._lock = Lock()

    def push(self, event: CtpCallbackEvent) -> None:
        with self._lock:
            self._events.append(event)

    def drain(self, event_type: str | None = None) -> list[CtpCallbackEvent]:
        with self._lock:
            if event_type is None:
                events = list(self._events)
                self._events.clear()
                return events

            kept: deque[CtpCallbackEvent] = deque()
            matched: list[CtpCallbackEvent] = []
            while self._events:
                event = self._events.popleft()
                if event.event_type == event_type:
                    matched.append(event)
                else:
                    kept.append(event)
            self._events = kept
            return matched

    def snapshot(self) -> list[CtpCallbackEvent]:
        with self._lock:
            return list(self._events)

    def wait_for(
        self,
        event_type: str,
        request_id: int | None = None,
        timeout: float = 0.0,
    ) -> CtpCallbackEvent | None:
        deadline = monotonic() + timeout
        while True:
            event = self._find(event_type, request_id)
            if event is not None or timeout <= 0 or monotonic() >= deadline:
                return event
            sleep(0.01)

    def wait_for_request(
        self,
        event_type: str,
        request_id: int,
        timeout: float = 0.0,
        require_last: bool = True,
    ) -> list[CtpCallbackEvent]:
        deadline = monotonic() + timeout
        while True:
            events = self._find_all(event_type, request_id)
            if events:
                if any(not event.ok for event in events):
                    return events
                if not require_last or any(event.is_last for event in events):
                    return events
            if timeout <= 0 or monotonic() >= deadline:
                return events
            sleep(0.01)

    def _find(
        self,
        event_type: str,
        request_id: int | None,
    ) -> CtpCallbackEvent | None:
        with self._lock:
            for event in self._events:
                if event.event_type != event_type:
                    continue
                if request_id is not None and event.request_id != request_id:
                    continue
                return event
        return None

    def _find_all(
        self,
        event_type: str,
        request_id: int,
    ) -> list[CtpCallbackEvent]:
        with self._lock:
            return [
                event
                for event in self._events
                if event.event_type == event_type and event.request_id == request_id
            ]


class CtpCallbackAdapter:
    def __init__(
        self,
        session: "CtpTradingSession",
        event_queue: CtpEventQueue | None = None,
    ) -> None:
        self.session = session
        self.event_queue = event_queue or CtpEventQueue()
        self._position_query_rows: dict[int, list[dict[str, Any]]] = {}
        self._order_query_rows: dict[int, list[dict[str, Any]]] = {}
        self._trade_query_rows: dict[int, list[dict[str, Any]]] = {}

    def on_front_connected(self) -> None:
        self.session.on_front_connected()
        self._push_event(
            "FRONT_CONNECTED",
            True,
            0,
            True,
            "front connected",
            {},
            {},
        )

    def OnFrontConnected(self) -> None:
        self.on_front_connected()

    def on_front_disconnected(self, reason: int = 0) -> None:
        self.session.on_front_disconnected(reason)
        self._push_event(
            "FRONT_DISCONNECTED",
            False,
            0,
            True,
            f"front disconnected: {reason}",
            {},
            {"reason": reason},
        )

    def OnFrontDisconnected(self, reason: int) -> None:
        self.on_front_disconnected(reason)

    def on_rsp_authenticate(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_AUTHENTICATE", data, rsp_info, request_id, is_last)
        if event.ok:
            self.session.authenticated = True

    def OnRspAuthenticate(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_authenticate(data, rsp_info, request_id, is_last)

    def on_rsp_user_login(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_USER_LOGIN", data, rsp_info, request_id, is_last)
        if event.ok:
            self.session.connected = True
            self.session.logged_in = True

    def OnRspUserLogin(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_user_login(data, rsp_info, request_id, is_last)

    def on_rsp_settlement_info_confirm(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response(
            "RSP_SETTLEMENT_INFO_CONFIRM",
            data,
            rsp_info,
            request_id,
            is_last,
        )
        if event.ok:
            self.session.settlement_confirmed = True

    def OnRspSettlementInfoConfirm(
        self,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        self.on_rsp_settlement_info_confirm(data, rsp_info, request_id, is_last)

    def on_rsp_qry_trading_account(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_QRY_TRADING_ACCOUNT", data, rsp_info, request_id, is_last)
        if event.ok and event.data:
            self.session.gateway.sync_account(event.data)

    def OnRspQryTradingAccount(
        self,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        self.on_rsp_qry_trading_account(data, rsp_info, request_id, is_last)

    def on_rsp_qry_investor_position(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_QRY_INVESTOR_POSITION", data, rsp_info, request_id, is_last)
        if event.ok and event.data:
            self._position_query_rows.setdefault(request_id, []).append(event.data)
        if event.ok and is_last:
            rows = self._position_query_rows.pop(request_id, [])
            self.session.gateway.sync_positions(rows)

    def OnRspQryInvestorPosition(
        self,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        self.on_rsp_qry_investor_position(data, rsp_info, request_id, is_last)

    def on_rsp_qry_order(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_QRY_ORDER", data, rsp_info, request_id, is_last)
        if event.ok and event.data:
            self._order_query_rows.setdefault(request_id, []).append(event.data)
        if event.ok and is_last:
            rows = self._order_query_rows.pop(request_id, [])
            self.session.gateway.sync_orders(rows)

    def OnRspQryOrder(
        self,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        self.on_rsp_qry_order(data, rsp_info, request_id, is_last)

    def on_rsp_qry_trade(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_QRY_TRADE", data, rsp_info, request_id, is_last)
        if event.ok and event.data:
            self._trade_query_rows.setdefault(request_id, []).append(event.data)
        if event.ok and is_last:
            rows = self._trade_query_rows.pop(request_id, [])
            self.session.gateway.sync_trades(rows)

    def OnRspQryTrade(
        self,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        self.on_rsp_qry_trade(data, rsp_info, request_id, is_last)

    def on_rtn_order(self, data: Any = None) -> None:
        payload = ctp_to_mapping(data)
        order = self.session.gateway.on_rtn_order(payload)
        local_order_id = self.session.gateway.local_order_id_for_order_ref(order.order_id)
        self._push_event(
            "RTN_ORDER",
            True,
            0,
            True,
            "order return",
            payload,
            {
                "local_order_id": local_order_id or "",
                "ctp_order_ref": order.order_id,
                "status": order.status.value,
            },
        )
        self.session.on_order_return(order)

    def OnRtnOrder(self, data: Any) -> None:
        self.on_rtn_order(data)

    def on_rtn_trade(self, data: Any = None) -> None:
        payload = ctp_to_mapping(data)
        trade = self.session.gateway.on_rtn_trade(payload)
        self._push_event(
            "RTN_TRADE",
            True,
            0,
            True,
            "trade return",
            payload,
            {"trade_id": trade.trade_id, "order_id": trade.order_id},
        )
        self.session.on_trade_return(trade)

    def OnRtnTrade(self, data: Any) -> None:
        self.on_rtn_trade(data)

    def on_rsp_order_insert(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_ORDER_INSERT", data, rsp_info, request_id, is_last)
        if not event.ok:
            self.session.on_order_insert_error(event)

    def OnRspOrderInsert(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_order_insert(data, rsp_info, request_id, is_last)

    def on_rsp_order_action(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_ORDER_ACTION", data, rsp_info, request_id, is_last)
        if not event.ok:
            self.session.on_order_action_error(event)

    def OnRspOrderAction(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_order_action(data, rsp_info, request_id, is_last)

    def on_rsp_error(
        self,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        info = ctp_to_mapping(rsp_info)
        self._push_event(
            "RSP_ERROR",
            ctp_response_ok(info),
            request_id,
            is_last,
            ctp_response_message(info),
            {},
            info,
        )

    def OnRspError(self, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_error(rsp_info, request_id, is_last)

    def _push_response(
        self,
        event_type: str,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> CtpCallbackEvent:
        payload = ctp_to_mapping(data)
        info = ctp_to_mapping(rsp_info)
        return self._push_event(
            event_type,
            ctp_response_ok(info),
            request_id,
            is_last,
            ctp_response_message(info),
            payload,
            info,
        )

    def _push_event(
        self,
        event_type: str,
        ok: bool,
        request_id: int,
        is_last: bool,
        message: str,
        data: Mapping[str, Any],
        rsp_info: Mapping[str, Any],
    ) -> CtpCallbackEvent:
        event = CtpCallbackEvent(
            timestamp=datetime.now(),
            event_type=event_type,
            ok=ok,
            request_id=request_id,
            is_last=is_last,
            message=message,
            data=_redact_field(dict(data)),
            rsp_info=_redact_field(dict(rsp_info)),
        )
        self.event_queue.push(event)
        return event


class CtpMarketDataCallbackAdapter:
    def __init__(
        self,
        session: "CtpMarketDataSession",
        event_queue: CtpEventQueue | None = None,
    ) -> None:
        self.session = session
        self.event_queue = event_queue or CtpEventQueue()

    def on_front_connected(self) -> None:
        self.session.on_front_connected()
        self._push_event(
            "MD_FRONT_CONNECTED",
            True,
            0,
            True,
            "market data front connected",
            {},
            {},
        )

    def OnFrontConnected(self) -> None:
        self.on_front_connected()

    def on_front_disconnected(self, reason: int = 0) -> None:
        self.session.on_front_disconnected(reason)
        self._push_event(
            "MD_FRONT_DISCONNECTED",
            False,
            0,
            True,
            f"market data front disconnected: {reason}",
            {},
            {"reason": reason},
        )

    def OnFrontDisconnected(self, reason: int) -> None:
        self.on_front_disconnected(reason)

    def on_rsp_user_login(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        event = self._push_response("RSP_MD_USER_LOGIN", data, rsp_info, request_id, is_last)
        if event.ok:
            self.session.connected = True
            self.session.logged_in = True

    def OnRspUserLogin(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_user_login(data, rsp_info, request_id, is_last)

    def on_rsp_sub_market_data(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        self._push_response("RSP_SUB_MARKET_DATA", data, rsp_info, request_id, is_last)

    def OnRspSubMarketData(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_sub_market_data(data, rsp_info, request_id, is_last)

    def on_rsp_unsub_market_data(
        self,
        data: Any = None,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        self._push_response("RSP_UNSUB_MARKET_DATA", data, rsp_info, request_id, is_last)

    def OnRspUnSubMarketData(self, data: Any, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_unsub_market_data(data, rsp_info, request_id, is_last)

    def on_rtn_depth_market_data(self, data: Any = None) -> None:
        payload = ctp_to_mapping(data)
        tick = self.session.gateway.on_rtn_depth_market_data(payload)
        self.session.on_tick(tick)
        self._push_event(
            "RTN_DEPTH_MARKET_DATA",
            True,
            0,
            True,
            "depth market data",
            payload,
            {
                "symbol": tick.symbol,
                "timestamp": tick.timestamp.isoformat(),
                "last_price": tick.last_price,
            },
        )

    def OnRtnDepthMarketData(self, data: Any) -> None:
        self.on_rtn_depth_market_data(data)

    def on_rsp_error(
        self,
        rsp_info: Any = None,
        request_id: int = 0,
        is_last: bool = True,
    ) -> None:
        info = ctp_to_mapping(rsp_info)
        self._push_event(
            "RSP_MD_ERROR",
            ctp_response_ok(info),
            request_id,
            is_last,
            ctp_response_message(info),
            {},
            info,
        )

    def OnRspError(self, rsp_info: Any, request_id: int, is_last: bool) -> None:
        self.on_rsp_error(rsp_info, request_id, is_last)

    def _push_response(
        self,
        event_type: str,
        data: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> CtpCallbackEvent:
        payload = ctp_to_mapping(data)
        info = ctp_to_mapping(rsp_info)
        return self._push_event(
            event_type,
            ctp_response_ok(info),
            request_id,
            is_last,
            ctp_response_message(info),
            payload,
            info,
        )

    def _push_event(
        self,
        event_type: str,
        ok: bool,
        request_id: int,
        is_last: bool,
        message: str,
        data: Mapping[str, Any],
        rsp_info: Mapping[str, Any],
    ) -> CtpCallbackEvent:
        event = CtpCallbackEvent(
            timestamp=datetime.now(),
            event_type=event_type,
            ok=ok,
            request_id=request_id,
            is_last=is_last,
            message=message,
            data=_redact_field(dict(data)),
            rsp_info=_redact_field(dict(rsp_info)),
        )
        self.event_queue.push(event)
        return event


@dataclass
class DryRunCtpTransport:
    account_response: Mapping[str, Any] | None = None
    position_responses: list[Mapping[str, Any]] = field(default_factory=list)
    order_responses: list[Mapping[str, Any]] = field(default_factory=list)
    trade_responses: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.connected = False
        self.calls: list[dict[str, Any]] = []
        self.callback_adapter: CtpCallbackAdapter | None = None

    def set_callback_adapter(self, adapter: CtpCallbackAdapter) -> None:
        self.callback_adapter = adapter

    def connect(self, config: CtpConnectionConfig) -> int:
        self.connected = True
        self._record(
            "connect",
            {
                "front": config.front,
                "broker_id": config.broker_id,
                "investor_id": config.investor_id,
            },
        )
        return 0

    def authenticate(self, field: dict[str, Any], request_id: int) -> int:
        self._record("authenticate", field, request_id)
        if self.callback_adapter is not None:
            self.callback_adapter.on_rsp_authenticate(
                field,
                {"ErrorID": 0, "ErrorMsg": ""},
                request_id,
                True,
            )
        return 0

    def login(self, field: dict[str, Any], request_id: int) -> int:
        self._record("login", field, request_id)
        if self.callback_adapter is not None:
            self.callback_adapter.on_rsp_user_login(
                field,
                {"ErrorID": 0, "ErrorMsg": ""},
                request_id,
                True,
            )
        return 0

    def confirm_settlement(self, field: dict[str, Any], request_id: int) -> int:
        self._record("confirm_settlement", field, request_id)
        if self.callback_adapter is not None:
            self.callback_adapter.on_rsp_settlement_info_confirm(
                field,
                {"ErrorID": 0, "ErrorMsg": ""},
                request_id,
                True,
            )
        return 0

    def query_account(self, field: dict[str, Any], request_id: int) -> Mapping[str, Any]:
        self._record("query_account", field, request_id)
        return self.account_response or {
            "Balance": 0.0,
            "Available": 0.0,
            "CurrMargin": 0.0,
            "PositionProfit": 0.0,
            "CurrencyID": field.get("CurrencyID", "CNY"),
        }

    def query_positions(
        self,
        field: dict[str, Any],
        request_id: int,
    ) -> list[Mapping[str, Any]]:
        self._record("query_positions", field, request_id)
        return _filter_ctp_query_rows(
            self.position_responses,
            instrument_id=field.get("InstrumentID"),
        )

    def query_orders(
        self,
        field: dict[str, Any],
        request_id: int,
    ) -> list[Mapping[str, Any]]:
        self._record("query_orders", field, request_id)
        return _filter_ctp_query_rows(
            self.order_responses,
            instrument_id=field.get("InstrumentID"),
            time_key="InsertTime",
            start_time=field.get("InsertTimeStart"),
            end_time=field.get("InsertTimeEnd"),
        )

    def query_trades(
        self,
        field: dict[str, Any],
        request_id: int,
    ) -> list[Mapping[str, Any]]:
        self._record("query_trades", field, request_id)
        return _filter_ctp_query_rows(
            self.trade_responses,
            instrument_id=field.get("InstrumentID"),
            time_key="TradeTime",
            start_time=field.get("TradeTimeStart"),
            end_time=field.get("TradeTimeEnd"),
        )

    def req_order_insert(self, field: dict[str, Any], request_id: int) -> int:
        self._record("req_order_insert", field, request_id)
        return 0

    def req_order_action(self, field: dict[str, Any], request_id: int) -> int:
        self._record("req_order_action", field, request_id)
        return 0

    def _record(
        self,
        action: str,
        field: Mapping[str, Any],
        request_id: int = 0,
    ) -> None:
        self.calls.append(
            {
                "action": action,
                "request_id": request_id,
                "field": _redact_field(dict(field)),
            }
        )


@dataclass
class DryRunCtpMarketDataTransport:
    tick_responses: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.connected = False
        self.calls: list[dict[str, Any]] = []
        self.callback_adapter: CtpMarketDataCallbackAdapter | None = None

    def set_callback_adapter(self, adapter: CtpMarketDataCallbackAdapter) -> None:
        self.callback_adapter = adapter

    def connect(self, config: CtpConnectionConfig) -> int:
        self.connected = True
        self._record(
            "connect_md",
            {
                "front": config.md_front,
                "broker_id": config.broker_id,
                "investor_id": config.investor_id,
            },
        )
        return 0

    def login(self, field: dict[str, Any], request_id: int) -> int:
        self._record("md_login", field, request_id)
        if self.callback_adapter is not None:
            self.callback_adapter.on_rsp_user_login(
                field,
                {"ErrorID": 0, "ErrorMsg": ""},
                request_id,
                True,
            )
        return 0

    def subscribe_market_data(self, instruments: Iterable[str]) -> int:
        symbols = list(instruments)
        self._record("subscribe_market_data", {"InstrumentIDs": symbols})
        if self.callback_adapter is not None:
            for index, symbol in enumerate(symbols):
                self.callback_adapter.on_rsp_sub_market_data(
                    {"InstrumentID": symbol},
                    {"ErrorID": 0, "ErrorMsg": ""},
                    0,
                    index == len(symbols) - 1,
                )
            symbol_set = set(symbols)
            for tick in self.tick_responses:
                if str(tick.get("InstrumentID", "")) in symbol_set:
                    self.callback_adapter.on_rtn_depth_market_data(tick)
        return 0

    def unsubscribe_market_data(self, instruments: Iterable[str]) -> int:
        symbols = list(instruments)
        self._record("unsubscribe_market_data", {"InstrumentIDs": symbols})
        if self.callback_adapter is not None:
            for index, symbol in enumerate(symbols):
                self.callback_adapter.on_rsp_unsub_market_data(
                    {"InstrumentID": symbol},
                    {"ErrorID": 0, "ErrorMsg": ""},
                    0,
                    index == len(symbols) - 1,
                )
        return 0

    def _record(
        self,
        action: str,
        field: Mapping[str, Any],
        request_id: int = 0,
    ) -> None:
        self.calls.append(
            {
                "action": action,
                "request_id": request_id,
                "field": _redact_field(dict(field)),
            }
        )


@dataclass
class NativeCtpTraderTransport:
    module_name: str
    factory_name: str = "CreateFtdcTraderApi"
    api: Any | None = None
    callback_adapter: CtpCallbackAdapter | None = None

    def connect(self, config: CtpConnectionConfig) -> int:
        if self.api is None:
            self.api = self._create_api(config)
        self._register_callback_adapter()
        if config.front:
            self._call_api(("RegisterFront", "register_front"), config.front)
        self._call_optional(("SubscribePrivateTopic", "subscribe_private_topic"), 0)
        self._call_optional(("SubscribePublicTopic", "subscribe_public_topic"), 0)
        self._call_api(("Init", "init"))
        return 0

    def set_callback_adapter(self, adapter: CtpCallbackAdapter) -> None:
        self.callback_adapter = adapter
        if self.api is not None:
            self._register_callback_adapter()

    def authenticate(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqAuthenticate", "req_authenticate"), field, request_id)

    def login(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqUserLogin", "req_user_login"), field, request_id)

    def confirm_settlement(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(
            ("ReqSettlementInfoConfirm", "req_settlement_info_confirm"),
            field,
            request_id,
        )

    def query_account(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqQryTradingAccount", "req_qry_trading_account"), field, request_id)

    def query_positions(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqQryInvestorPosition", "req_qry_investor_position"), field, request_id)

    def query_orders(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqQryOrder", "req_qry_order"), field, request_id)

    def query_trades(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqQryTrade", "req_qry_trade"), field, request_id)

    def req_order_insert(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqOrderInsert", "req_order_insert"), field, request_id)

    def req_order_action(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqOrderAction", "req_order_action"), field, request_id)

    def _create_api(self, config: CtpConnectionConfig) -> Any:
        if not self.module_name:
            raise CtpGatewayError("ctp.transport_module is required for live CTP mode")
        module = importlib.import_module(self.module_name)
        factory = _resolve_attr(module, self.factory_name)
        try:
            return factory(config.flow_path)
        except TypeError:
            return factory()

    def _call_api(self, names: tuple[str, ...], *args: Any) -> Any:
        if self.api is None:
            raise CtpGatewayError("CTP API is not initialized")
        for name in names:
            if hasattr(self.api, name):
                return getattr(self.api, name)(*args)
        raise CtpGatewayError(f"CTP API does not expose any of: {', '.join(names)}")

    def _call_optional(self, names: tuple[str, ...], *args: Any) -> Any:
        if self.api is None:
            return None
        for name in names:
            if hasattr(self.api, name):
                return getattr(self.api, name)(*args)
        return None

    def _register_callback_adapter(self) -> None:
        if self.api is None or self.callback_adapter is None:
            return
        for name in ("RegisterSpi", "register_spi", "set_callback", "SetCallback"):
            if hasattr(self.api, name):
                getattr(self.api, name)(self.callback_adapter)
                return


@dataclass
class NativeCtpMarketDataTransport:
    module_name: str
    factory_name: str = "CreateFtdcMdApi"
    api: Any | None = None
    callback_adapter: CtpMarketDataCallbackAdapter | None = None

    def connect(self, config: CtpConnectionConfig) -> int:
        if self.api is None:
            self.api = self._create_api(config)
        self._register_callback_adapter()
        if config.md_front:
            self._call_api(("RegisterFront", "register_front"), config.md_front)
        self._call_api(("Init", "init"))
        return 0

    def set_callback_adapter(self, adapter: CtpMarketDataCallbackAdapter) -> None:
        self.callback_adapter = adapter
        if self.api is not None:
            self._register_callback_adapter()

    def login(self, field: dict[str, Any], request_id: int) -> int:
        return self._call_api(("ReqUserLogin", "req_user_login"), field, request_id)

    def subscribe_market_data(self, instruments: Iterable[str]) -> int:
        symbols = list(instruments)
        return self._call_api_variants(
            ("SubscribeMarketData", "subscribe_market_data"),
            ((symbols, len(symbols)), (symbols,)),
        )

    def unsubscribe_market_data(self, instruments: Iterable[str]) -> int:
        symbols = list(instruments)
        return self._call_api_variants(
            ("UnSubscribeMarketData", "un_subscribe_market_data", "unsubscribe_market_data"),
            ((symbols, len(symbols)), (symbols,)),
        )

    def _create_api(self, config: CtpConnectionConfig) -> Any:
        module_name = self.module_name or config.transport_module
        if not module_name:
            raise CtpGatewayError(
                "ctp.md_transport_module or ctp.transport_module is required for live CTP market data mode"
            )
        module = importlib.import_module(module_name)
        factory = _resolve_attr(module, self.factory_name)
        try:
            return factory(config.flow_path)
        except TypeError:
            return factory()

    def _call_api(self, names: tuple[str, ...], *args: Any) -> Any:
        if self.api is None:
            raise CtpGatewayError("CTP market data API is not initialized")
        for name in names:
            if hasattr(self.api, name):
                return getattr(self.api, name)(*args)
        raise CtpGatewayError(f"CTP market data API does not expose any of: {', '.join(names)}")

    def _call_api_variants(
        self,
        names: tuple[str, ...],
        arg_variants: tuple[tuple[Any, ...], ...],
    ) -> Any:
        if self.api is None:
            raise CtpGatewayError("CTP market data API is not initialized")
        errors: list[TypeError] = []
        for name in names:
            if not hasattr(self.api, name):
                continue
            method = getattr(self.api, name)
            for args in arg_variants:
                try:
                    return method(*args)
                except TypeError as exc:
                    errors.append(exc)
        if errors:
            raise errors[-1]
        raise CtpGatewayError(f"CTP market data API does not expose any of: {', '.join(names)}")

    def _register_callback_adapter(self) -> None:
        if self.api is None or self.callback_adapter is None:
            return
        for name in ("RegisterSpi", "register_spi", "set_callback", "SetCallback"):
            if hasattr(self.api, name):
                getattr(self.api, name)(self.callback_adapter)
                return


@dataclass(frozen=True)
class CtpOrderInstruction:
    local_order_id: str
    symbol: str
    side: Side
    offset: Offset
    quantity: float
    order_type: OrderType
    limit_price: float | None = None


@dataclass(frozen=True)
class CtpOrderInsertRequest:
    field: dict[str, Any]
    local_order_id: str
    order_ref: str
    request_id: int
    instruction: CtpOrderInstruction


@dataclass(frozen=True)
class CtpOrderActionRequest:
    field: dict[str, Any]
    local_order_id: str
    request_id: int


@dataclass
class CtpTradingAccount:
    balance: float = 0.0
    available: float = 0.0
    curr_margin: float = 0.0
    position_profit: float = 0.0
    close_profit: float = 0.0
    commission: float = 0.0
    frozen_margin: float = 0.0
    frozen_commission: float = 0.0
    pre_balance: float = 0.0
    deposit: float = 0.0
    withdraw: float = 0.0
    currency_id: str = "CNY"

    @classmethod
    def from_ctp(cls, raw: Mapping[str, Any]) -> "CtpTradingAccount":
        return cls(
            balance=_float_field(raw, "Balance"),
            available=_float_field(raw, "Available"),
            curr_margin=_float_field(raw, "CurrMargin"),
            position_profit=_float_field(raw, "PositionProfit"),
            close_profit=_float_field(raw, "CloseProfit"),
            commission=_float_field(raw, "Commission"),
            frozen_margin=_float_field(raw, "FrozenMargin"),
            frozen_commission=_float_field(raw, "FrozenCommission"),
            pre_balance=_float_field(raw, "PreBalance"),
            deposit=_float_field(raw, "Deposit"),
            withdraw=_float_field(raw, "Withdraw"),
            currency_id=str(raw.get("CurrencyID", "CNY") or "CNY"),
        )

    def to_snapshot(self) -> dict[str, float]:
        return {
            "cash": self.balance,
            "equity": self.balance,
            "margin": self.curr_margin,
            "available": self.available,
            "risk_ratio": self.curr_margin / self.balance if self.balance else 0.0,
            "unrealized_pnl": self.position_profit,
            "frozen_margin": self.frozen_margin,
            "frozen_commission": self.frozen_commission,
        }


@dataclass
class CtpTradingSession:
    gateway: "CtpFuturesGateway"
    transport: CtpTransportProtocol | None = None
    dry_run: bool = True

    def __post_init__(self) -> None:
        self.events: list[CtpLifecycleEvent] = []
        self.callback_queue = CtpEventQueue()
        self.callback_adapter = CtpCallbackAdapter(self, self.callback_queue)
        self.connected = False
        self.front_connected = False
        self.authenticated = False
        self.logged_in = False
        self.settlement_confirmed = False
        self.last_disconnect_reason: int | None = None
        self._last_start_options: dict[str, bool] = {}
        self._order_handlers: list[Callable[[Order], None]] = []
        self._trade_handlers: list[Callable[[Trade], None]] = []
        self._order_insert_error_handlers: list[Callable[[CtpCallbackEvent], None]] = []
        self._order_action_error_handlers: list[Callable[[CtpCallbackEvent], None]] = []
        if self.transport is None:
            if self.dry_run:
                self.transport = DryRunCtpTransport()
            else:
                self.transport = NativeCtpTraderTransport(
                    module_name=self.gateway.config.transport_module,
                    factory_name=self.gateway.config.trader_api_factory,
                )
        if hasattr(self.transport, "set_callback_adapter"):
            self.transport.set_callback_adapter(self.callback_adapter)
        self.gateway.api = self.transport

    def add_order_handler(self, handler: Callable[[Order], None]) -> None:
        self._order_handlers.append(handler)

    def add_trade_handler(self, handler: Callable[[Trade], None]) -> None:
        self._trade_handlers.append(handler)

    def add_order_insert_error_handler(self, handler: Callable[[CtpCallbackEvent], None]) -> None:
        self._order_insert_error_handlers.append(handler)

    def add_order_action_error_handler(self, handler: Callable[[CtpCallbackEvent], None]) -> None:
        self._order_action_error_handlers.append(handler)

    def on_order_return(self, order: Order) -> None:
        for handler in list(self._order_handlers):
            handler(order)

    def on_trade_return(self, trade: Trade) -> None:
        for handler in list(self._trade_handlers):
            handler(trade)

    def on_order_insert_error(self, event: CtpCallbackEvent) -> None:
        for handler in list(self._order_insert_error_handlers):
            handler(event)

    def on_order_action_error(self, event: CtpCallbackEvent) -> None:
        for handler in list(self._order_action_error_handlers):
            handler(event)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        contract_registry: ContractRegistry | None = None,
        dry_run: bool = True,
        transport: CtpTransportProtocol | None = None,
    ) -> "CtpTradingSession":
        gateway = CtpFuturesGateway.from_mapping(
            raw,
            contract_registry=contract_registry,
        )
        return cls(gateway=gateway, transport=transport, dry_run=dry_run)

    @property
    def state(self) -> str:
        if not self.connected and self.last_disconnect_reason is not None:
            return "disconnected"
        if self.settlement_confirmed:
            return "settlement_confirmed"
        if self.logged_in:
            return "logged_in"
        if self.authenticated:
            return "authenticated"
        if self.connected:
            return "connected"
        return "created"

    def start(
        self,
        authenticate: bool | None = None,
        confirm_settlement: bool | None = None,
        query_account: bool | None = None,
        query_positions: bool | None = None,
    ) -> None:
        config = self.gateway.config
        options = self._resolve_start_options(
            authenticate,
            confirm_settlement,
            query_account,
            query_positions,
        )
        self._last_start_options = dict(options)

        assert self.transport is not None
        self._ensure_ok("CONNECT", self.transport.connect(config), 0, {"front": config.front})
        self.connected = True
        self.front_connected = True
        self.last_disconnect_reason = None

        self._run_recoverable_start_steps(options)

    def submit_order(
        self,
        order: Order,
        position: FuturesPosition | None = None,
    ) -> list[CtpOrderInsertRequest]:
        requests = self.gateway.submit_order(order, position)
        for request in requests:
            self._record(
                "ORDER_INSERT",
                True,
                request.request_id,
                "submitted",
                request.field,
            )
        return requests

    def cancel_order(
        self,
        order: Order,
        front_id: int | None = None,
        session_id: int | None = None,
        order_sys_id: str = "",
        exchange_id: str | None = None,
    ) -> CtpOrderActionRequest:
        request = self.gateway.cancel_order(
            order,
            front_id=front_id,
            session_id=session_id,
            order_sys_id=order_sys_id,
            exchange_id=exchange_id,
        )
        self._record(
            "ORDER_ACTION",
            True,
            request.request_id,
            "submitted",
            request.field,
        )
        return request

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
        field_symbol = reconcile_symbols[0] if len(reconcile_symbols) == 1 else None
        options = {
            "query_account": query_account,
            "query_positions": query_positions,
            "query_orders": query_orders,
            "query_trades": query_trades,
            "symbols": reconcile_symbols,
            "start_time": start_time or "",
            "end_time": end_time or "",
        }
        self._record(
            "RECONCILE_START",
            True,
            0,
            "starting CTP reconciliation",
            options,
        )
        try:
            config = self.gateway.config
            if query_account:
                self._send_query_account(config)
            if query_positions:
                self._send_query_positions(config, symbol=field_symbol)
            if query_orders:
                self._send_query_orders(
                    config,
                    symbol=field_symbol,
                    start_time=start_time,
                    end_time=end_time,
                )
            if query_trades:
                self._send_query_trades(
                    config,
                    symbol=field_symbol,
                    start_time=start_time,
                    end_time=end_time,
                )
        except CtpGatewayError as exc:
            self._record(
                "RECONCILE_ERROR",
                False,
                0,
                str(exc),
                options,
            )
            return False

        payload = {
            **options,
            "account": self.gateway.account is not None,
            "positions": len(self.gateway.positions),
            "orders": len(self.gateway.orders),
            "trades": len(self.gateway.trades),
        }
        self._record(
            "RECONCILE_READY",
            True,
            0,
            "CTP reconciliation complete",
            payload,
        )
        return True

    def snapshot(self) -> dict[str, Any]:
        account_snapshot = (
            self.gateway.account.to_snapshot() if self.gateway.account else {}
        )
        return {
            "state": self.state,
            "dry_run": self.dry_run,
            "connected": self.connected,
            "front_connected": self.front_connected,
            "last_disconnect_reason": self.last_disconnect_reason,
            "authenticated": self.authenticated,
            "logged_in": self.logged_in,
            "settlement_confirmed": self.settlement_confirmed,
            "recovery_options": self.recovery_options,
            "account": account_snapshot,
            "positions": {
                symbol: dict(position.__dict__)
                for symbol, position in self.gateway.positions.items()
            },
            "ticks": {
                symbol: tick_to_dict(tick)
                for symbol, tick in self.gateway.ticks.items()
            },
            "events": [event_to_dict(event) for event in self.events],
            "callback_events": [
                callback_event_to_dict(event)
                for event in self.callback_queue.snapshot()
            ],
        }

    def _ensure_ok(
        self,
        event_type: str,
        result: Mapping[str, Any] | Iterable[Mapping[str, Any]] | int | None,
        request_id: int,
        payload: Mapping[str, Any],
    ) -> None:
        ok = result is None or not isinstance(result, int) or result == 0
        message = "ok" if ok else f"CTP request returned {result}"
        self._record(event_type, ok, request_id, message, payload)
        if not ok:
            raise CtpGatewayError(message)

    def on_front_connected(self) -> None:
        should_recover = (
            self.last_disconnect_reason is not None
            and self.gateway.config.auto_recover_on_front_connected
        )
        self.connected = True
        self.front_connected = True
        self.last_disconnect_reason = None
        self._record("FRONT_CONNECTED", True, 0, "front connected", {})
        if should_recover:
            self.recover_after_front_connected()

    def on_front_disconnected(self, reason: int = 0) -> None:
        self.connected = False
        self.front_connected = False
        self.authenticated = False
        self.logged_in = False
        self.settlement_confirmed = False
        self.last_disconnect_reason = reason
        self._record(
            "FRONT_DISCONNECTED",
            False,
            0,
            f"front disconnected: {reason}",
            {"reason": reason},
        )

    @property
    def recovery_options(self) -> dict[str, bool]:
        return dict(
            self._last_start_options
            or self._resolve_start_options(None, None, None, None)
        )

    def recover_after_front_connected(self) -> bool:
        options = self.recovery_options
        self._record(
            "AUTO_RECOVER_START",
            True,
            0,
            "auto recovering trading session",
            options,
        )
        try:
            self._run_recoverable_start_steps(options)
        except CtpGatewayError as exc:
            self._record(
                "AUTO_RECOVER_ERROR",
                False,
                0,
                str(exc),
                options,
            )
            return False
        self._record(
            "AUTO_RECOVER_READY",
            True,
            0,
            "trading session recovered",
            options,
        )
        return True

    def _resolve_start_options(
        self,
        authenticate: bool | None,
        confirm_settlement: bool | None,
        query_account: bool | None,
        query_positions: bool | None,
    ) -> dict[str, bool]:
        config = self.gateway.config
        return {
            "authenticate": config.auth_required if authenticate is None else authenticate,
            "confirm_settlement": (
                config.settlement_confirm_required
                if confirm_settlement is None
                else confirm_settlement
            ),
            "query_account": (
                config.query_account_on_start if query_account is None else query_account
            ),
            "query_positions": (
                config.query_positions_on_start
                if query_positions is None
                else query_positions
            ),
        }

    def _run_recoverable_start_steps(self, options: Mapping[str, bool]) -> None:
        config = self.gateway.config
        if options.get("authenticate", False):
            self._send_authenticate(config)

        self._send_login(config)

        if options.get("confirm_settlement", False):
            self._send_settlement_confirm(config)

        if options.get("query_account", False):
            self._send_query_account(config)

        if options.get("query_positions", False):
            self._send_query_positions(config)

    def _send_authenticate(self, config: CtpConnectionConfig) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        field = ctp_authenticate_field(config)
        response = self.transport.authenticate(field, request_id)
        self._ensure_ok("AUTHENTICATE", response, request_id, field)
        if self._wait_for_lifecycle_callbacks(response):
            self._wait_for_callback_response(
                "RSP_AUTHENTICATE",
                request_id,
                config.lifecycle_timeout,
            )
        self.authenticated = True

    def _send_login(self, config: CtpConnectionConfig) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        login_field = ctp_login_field(config)
        response = self.transport.login(login_field, request_id)
        self._ensure_ok("LOGIN", response, request_id, login_field)
        if self._wait_for_lifecycle_callbacks(response):
            self._wait_for_callback_response(
                "RSP_USER_LOGIN",
                request_id,
                config.lifecycle_timeout,
            )
        self.logged_in = True

    def _send_settlement_confirm(self, config: CtpConnectionConfig) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        settlement_field = ctp_settlement_confirm_field(config)
        response = self.transport.confirm_settlement(settlement_field, request_id)
        self._ensure_ok("SETTLEMENT_CONFIRM", response, request_id, settlement_field)
        if self._wait_for_lifecycle_callbacks(response):
            self._wait_for_callback_response(
                "RSP_SETTLEMENT_INFO_CONFIRM",
                request_id,
                config.lifecycle_timeout,
            )
        self.settlement_confirmed = True

    def _send_query_account(self, config: CtpConnectionConfig) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        account_field = ctp_query_account_field(config)
        response = self.transport.query_account(account_field, request_id)
        self._ensure_ok("QUERY_ACCOUNT", response, request_id, account_field)
        if isinstance(response, Mapping):
            self.gateway.sync_account(response)
        elif self._wait_for_query_callbacks(response):
            self._wait_for_callback_response(
                "RSP_QRY_TRADING_ACCOUNT",
                request_id,
                config.query_timeout,
            )

    def _send_query_positions(
        self,
        config: CtpConnectionConfig,
        symbol: str | None = None,
    ) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        position_field = ctp_query_position_field(config, symbol=symbol)
        response = self.transport.query_positions(position_field, request_id)
        self._ensure_ok("QUERY_POSITIONS", response, request_id, position_field)
        rows = _position_rows(response)
        if rows is not None:
            self.gateway.sync_positions(rows, symbols=[symbol] if symbol else None)
        elif self._wait_for_query_callbacks(response):
            self._wait_for_callback_response(
                "RSP_QRY_INVESTOR_POSITION",
                request_id,
                config.query_timeout,
            )

    def _send_query_orders(
        self,
        config: CtpConnectionConfig,
        symbol: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        order_field = ctp_query_order_field(
            config,
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
        )
        response = self.transport.query_orders(order_field, request_id)
        self._ensure_ok("QUERY_ORDERS", response, request_id, order_field)
        rows = _position_rows(response)
        if rows is not None:
            self.gateway.sync_orders(
                rows,
                symbols=[symbol] if symbol else None,
                replace=not (start_time or end_time),
            )
        elif self._wait_for_query_callbacks(response):
            self._wait_for_callback_response(
                "RSP_QRY_ORDER",
                request_id,
                config.query_timeout,
            )

    def _send_query_trades(
        self,
        config: CtpConnectionConfig,
        symbol: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        trade_field = ctp_query_trade_field(
            config,
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
        )
        response = self.transport.query_trades(trade_field, request_id)
        self._ensure_ok("QUERY_TRADES", response, request_id, trade_field)
        rows = _position_rows(response)
        if rows is not None:
            self.gateway.sync_trades(
                rows,
                symbols=[symbol] if symbol else None,
                replace=not (start_time or end_time),
            )
        elif self._wait_for_query_callbacks(response):
            self._wait_for_callback_response(
                "RSP_QRY_TRADE",
                request_id,
                config.query_timeout,
            )

    def _wait_for_query_callbacks(
        self,
        result: Mapping[str, Any] | Iterable[Mapping[str, Any]] | int | None,
    ) -> bool:
        if not self.gateway.config.wait_for_query_callbacks:
            return False
        if isinstance(result, int):
            return result == 0
        return result is None

    def _wait_for_lifecycle_callbacks(
        self,
        result: Mapping[str, Any] | Iterable[Mapping[str, Any]] | int | None,
    ) -> bool:
        if not self.gateway.config.wait_for_lifecycle_callbacks:
            return False
        if isinstance(result, int):
            return result == 0
        return result is None

    def _wait_for_callback_response(
        self,
        event_type: str,
        request_id: int,
        timeout: float,
    ) -> list[CtpCallbackEvent]:
        events = self.callback_queue.wait_for_request(
            event_type,
            request_id=request_id,
            timeout=timeout,
            require_last=True,
        )
        if not events:
            message = f"timeout waiting for {event_type} request_id={request_id}"
            self._record(
                f"{event_type}_TIMEOUT",
                False,
                request_id,
                message,
                {"timeout": timeout},
            )
            raise CtpRequestTimeoutError(message)

        bad_event = next((event for event in events if not event.ok), None)
        if bad_event is not None:
            message = bad_event.message or f"{event_type} failed"
            self._record(
                f"{event_type}_ERROR",
                False,
                request_id,
                message,
                bad_event.rsp_info,
            )
            raise CtpGatewayError(message)

        if not any(event.is_last for event in events):
            message = f"incomplete {event_type} response request_id={request_id}"
            self._record(
                f"{event_type}_TIMEOUT",
                False,
                request_id,
                message,
                {"timeout": timeout},
            )
            raise CtpRequestTimeoutError(message)

        self._record(
            f"{event_type}_READY",
            True,
            request_id,
            "callback response complete",
            {"count": len(events)},
        )
        return events

    def _record(
        self,
        event_type: str,
        ok: bool,
        request_id: int,
        message: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.events.append(
            CtpLifecycleEvent(
                timestamp=datetime.now(),
                event_type=event_type,
                ok=ok,
                request_id=request_id,
                message=message,
                payload=_redact_field(dict(payload)),
            )
        )


@dataclass
class CtpMarketDataSession:
    gateway: "CtpFuturesGateway"
    transport: CtpMarketDataTransportProtocol | None = None
    dry_run: bool = True

    def __post_init__(self) -> None:
        self.events: list[CtpLifecycleEvent] = []
        self.callback_queue = CtpEventQueue()
        self.callback_adapter = CtpMarketDataCallbackAdapter(self, self.callback_queue)
        self.connected = False
        self.front_connected = False
        self.logged_in = False
        self.last_disconnect_reason: int | None = None
        self.subscribed_symbols: set[str] = set()
        self._tick_handlers: list[Callable[[Tick], None]] = []
        if self.transport is None:
            if self.dry_run:
                self.transport = DryRunCtpMarketDataTransport()
            else:
                self.transport = NativeCtpMarketDataTransport(
                    module_name=(
                        self.gateway.config.md_transport_module
                        or self.gateway.config.transport_module
                    ),
                    factory_name=self.gateway.config.md_api_factory,
                )
        if hasattr(self.transport, "set_callback_adapter"):
            self.transport.set_callback_adapter(self.callback_adapter)

    def add_tick_handler(self, handler: Callable[[Tick], None]) -> None:
        self._tick_handlers.append(handler)

    def on_tick(self, tick: Tick) -> None:
        for handler in list(self._tick_handlers):
            handler(tick)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        contract_registry: ContractRegistry | None = None,
        dry_run: bool = True,
        transport: CtpMarketDataTransportProtocol | None = None,
    ) -> "CtpMarketDataSession":
        gateway = CtpFuturesGateway.from_mapping(
            raw,
            contract_registry=contract_registry,
        )
        return cls(gateway=gateway, transport=transport, dry_run=dry_run)

    @property
    def state(self) -> str:
        if not self.connected and self.last_disconnect_reason is not None:
            return "disconnected"
        if not self.connected:
            return "created"
        if self.subscribed_symbols:
            return "subscribed"
        if self.logged_in:
            return "logged_in"
        if self.connected:
            return "connected"
        return "created"

    def start(self, login: bool = True) -> None:
        config = self.gateway.config
        assert self.transport is not None
        self._ensure_ok("MD_CONNECT", self.transport.connect(config), 0, {"front": config.md_front})
        self.connected = True
        self.front_connected = True
        self.last_disconnect_reason = None

        if login:
            self._send_login(config)

    def subscribe(self, symbols: Iterable[str]) -> list[str]:
        instruments = [str(symbol) for symbol in symbols if str(symbol)]
        if not instruments:
            return []

        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        response = self.transport.subscribe_market_data(instruments)
        self._ensure_ok(
            "MD_SUBSCRIBE",
            response,
            request_id,
            {"InstrumentIDs": instruments},
        )
        self.subscribed_symbols.update(instruments)
        return instruments

    def unsubscribe(self, symbols: Iterable[str]) -> list[str]:
        instruments = [str(symbol) for symbol in symbols if str(symbol)]
        if not instruments:
            return []

        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        response = self.transport.unsubscribe_market_data(instruments)
        self._ensure_ok(
            "MD_UNSUBSCRIBE",
            response,
            request_id,
            {"InstrumentIDs": instruments},
        )
        for symbol in instruments:
            self.subscribed_symbols.discard(symbol)
        return instruments

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "dry_run": self.dry_run,
            "connected": self.connected,
            "front_connected": self.front_connected,
            "last_disconnect_reason": self.last_disconnect_reason,
            "logged_in": self.logged_in,
            "subscribed_symbols": sorted(self.subscribed_symbols),
            "ticks": {
                symbol: tick_to_dict(tick)
                for symbol, tick in self.gateway.ticks.items()
            },
            "events": [event_to_dict(event) for event in self.events],
            "callback_events": [
                callback_event_to_dict(event)
                for event in self.callback_queue.snapshot()
            ],
        }

    def _ensure_ok(
        self,
        event_type: str,
        result: int | None,
        request_id: int,
        payload: Mapping[str, Any],
    ) -> None:
        ok = result is None or not isinstance(result, int) or result == 0
        message = "ok" if ok else f"CTP market data request returned {result}"
        self._record(event_type, ok, request_id, message, payload)
        if not ok:
            raise CtpGatewayError(message)

    def on_front_connected(self) -> None:
        should_recover = (
            self.last_disconnect_reason is not None
            and self.gateway.config.auto_recover_on_front_connected
        )
        self.connected = True
        self.front_connected = True
        self.last_disconnect_reason = None
        self._record("MD_FRONT_CONNECTED", True, 0, "market data front connected", {})
        if should_recover:
            self.recover_after_front_connected()

    def on_front_disconnected(self, reason: int = 0) -> None:
        self.connected = False
        self.front_connected = False
        self.logged_in = False
        self.last_disconnect_reason = reason
        self._record(
            "MD_FRONT_DISCONNECTED",
            False,
            0,
            f"market data front disconnected: {reason}",
            {"reason": reason},
        )

    def recover_after_front_connected(self) -> bool:
        symbols = sorted(self.subscribed_symbols)
        payload = {"InstrumentIDs": symbols}
        self._record(
            "AUTO_MD_RECOVER_START",
            True,
            0,
            "auto recovering market data session",
            payload,
        )
        try:
            self._send_login(self.gateway.config)
            if self.gateway.config.auto_resubscribe_on_front_connected and symbols:
                self.subscribe(symbols)
        except CtpGatewayError as exc:
            self._record(
                "AUTO_MD_RECOVER_ERROR",
                False,
                0,
                str(exc),
                payload,
            )
            return False
        self._record(
            "AUTO_MD_RECOVER_READY",
            True,
            0,
            "market data session recovered",
            payload,
        )
        return True

    def _send_login(self, config: CtpConnectionConfig) -> None:
        assert self.transport is not None
        request_id = self.gateway.next_request_id()
        login_field = ctp_md_login_field(config)
        response = self.transport.login(login_field, request_id)
        self._ensure_ok("MD_LOGIN", response, request_id, login_field)
        if self._wait_for_market_data_callbacks(response):
            self._wait_for_callback_response(
                "RSP_MD_USER_LOGIN",
                request_id,
                config.market_data_timeout,
            )
        self.logged_in = True

    def _wait_for_market_data_callbacks(self, result: int | None) -> bool:
        if not self.gateway.config.wait_for_market_data_callbacks:
            return False
        if isinstance(result, int):
            return result == 0
        return result is None

    def _wait_for_callback_response(
        self,
        event_type: str,
        request_id: int,
        timeout: float,
    ) -> list[CtpCallbackEvent]:
        events = self.callback_queue.wait_for_request(
            event_type,
            request_id=request_id,
            timeout=timeout,
            require_last=True,
        )
        if not events:
            message = f"timeout waiting for {event_type} request_id={request_id}"
            self._record(
                f"{event_type}_TIMEOUT",
                False,
                request_id,
                message,
                {"timeout": timeout},
            )
            raise CtpRequestTimeoutError(message)

        bad_event = next((event for event in events if not event.ok), None)
        if bad_event is not None:
            message = bad_event.message or f"{event_type} failed"
            self._record(
                f"{event_type}_ERROR",
                False,
                request_id,
                message,
                bad_event.rsp_info,
            )
            raise CtpGatewayError(message)

        if not any(event.is_last for event in events):
            message = f"incomplete {event_type} response request_id={request_id}"
            self._record(
                f"{event_type}_TIMEOUT",
                False,
                request_id,
                message,
                {"timeout": timeout},
            )
            raise CtpRequestTimeoutError(message)

        self._record(
            f"{event_type}_READY",
            True,
            request_id,
            "callback response complete",
            {"count": len(events)},
        )
        return events

    def _record(
        self,
        event_type: str,
        ok: bool,
        request_id: int,
        message: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.events.append(
            CtpLifecycleEvent(
                timestamp=datetime.now(),
                event_type=event_type,
                ok=ok,
                request_id=request_id,
                message=message,
                payload=_redact_field(dict(payload)),
            )
        )


@dataclass
class CtpFuturesGateway:
    config: CtpConnectionConfig
    contract_registry: ContractRegistry = field(default_factory=ContractRegistry)
    api: CtpTraderApiProtocol | None = None
    request_id: int = 0
    order_ref_start: int = 1
    hedge_flag: str = CTP_HEDGE_SPECULATION
    time_condition: str = CTP_TIME_CONDITION_GFD
    volume_condition: str = CTP_VOLUME_CONDITION_ANY

    def __post_init__(self) -> None:
        self._next_order_ref = self.order_ref_start
        self.local_to_ctp: dict[str, list[CtpOrderInsertRequest]] = {}
        self.account: CtpTradingAccount | None = None
        self.positions: dict[str, FuturesPosition] = {}
        self.orders: dict[str, Order] = {}
        self.trades: dict[str, Trade] = {}
        self.ticks: dict[str, Tick] = {}

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        contract_registry: ContractRegistry | None = None,
        api: CtpTraderApiProtocol | None = None,
    ) -> "CtpFuturesGateway":
        source = raw or {}
        return cls(
            config=CtpConnectionConfig.from_mapping(source),
            contract_registry=contract_registry or ContractRegistry(),
            api=api,
            request_id=int(source.get("request_id_start", 0)),
            order_ref_start=int(source.get("order_ref_start", 1)),
            hedge_flag=str(source.get("hedge_flag", CTP_HEDGE_SPECULATION)),
            time_condition=str(source.get("time_condition", CTP_TIME_CONDITION_GFD)),
            volume_condition=str(source.get("volume_condition", CTP_VOLUME_CONDITION_ANY)),
        )

    def create_order_insert_requests(
        self,
        order: Order,
        position: FuturesPosition | None = None,
    ) -> list[CtpOrderInsertRequest]:
        spec = self.contract_registry.for_symbol(order.symbol)
        instructions = split_order_for_ctp(order, spec, position)
        requests: list[CtpOrderInsertRequest] = []
        for instruction in instructions:
            self.next_request_id()
            order_ref = self._next_order_ref_text()
            field = self._order_insert_field(
                instruction=instruction,
                spec=spec,
                order_ref=order_ref,
            )
            requests.append(
                CtpOrderInsertRequest(
                    field=field,
                    local_order_id=order.order_id,
                    order_ref=order_ref,
                    request_id=self.request_id,
                    instruction=instruction,
                )
            )
        return requests

    def submit_order(
        self,
        order: Order,
        position: FuturesPosition | None = None,
    ) -> list[CtpOrderInsertRequest]:
        requests = self.create_order_insert_requests(order, position)
        self.local_to_ctp.setdefault(order.order_id, []).extend(requests)
        if self.api:
            for request in requests:
                result = self.api.req_order_insert(request.field, request.request_id)
                if result != 0:
                    raise CtpGatewayError(
                        f"ReqOrderInsert failed: {result} order_ref={request.order_ref}"
                    )
        return requests

    def create_order_action_request(
        self,
        order: Order,
        front_id: int | None = None,
        session_id: int | None = None,
        order_sys_id: str = "",
        exchange_id: str | None = None,
    ) -> CtpOrderActionRequest:
        self.next_request_id()
        spec = self.contract_registry.for_symbol(order.symbol)
        related = self.local_to_ctp.get(order.order_id, [])
        order_ref = related[0].order_ref if related else order.order_id
        field = {
            "BrokerID": self.config.broker_id,
            "InvestorID": self.config.investor_id,
            "UserID": self.config.user_id or self.config.investor_id,
            "InstrumentID": order.symbol,
            "ExchangeID": exchange_id if exchange_id is not None else spec.exchange,
            "ActionFlag": CTP_ACTION_DELETE,
            "OrderRef": order_ref,
            "OrderSysID": order_sys_id,
            "RequestID": self.request_id,
        }
        if front_id is not None:
            field["FrontID"] = int(front_id)
        if session_id is not None:
            field["SessionID"] = int(session_id)
        return CtpOrderActionRequest(
            field=field,
            local_order_id=order.order_id,
            request_id=self.request_id,
        )

    def next_request_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def cancel_order(
        self,
        order: Order,
        front_id: int | None = None,
        session_id: int | None = None,
        order_sys_id: str = "",
        exchange_id: str | None = None,
    ) -> CtpOrderActionRequest:
        request = self.create_order_action_request(
            order,
            front_id=front_id,
            session_id=session_id,
            order_sys_id=order_sys_id,
            exchange_id=exchange_id,
        )
        if self.api:
            result = self.api.req_order_action(request.field, request.request_id)
            if result != 0:
                raise CtpGatewayError(
                    f"ReqOrderAction failed: {result} order_id={order.order_id}"
                )
        return request

    def on_rtn_order(self, raw: Mapping[str, Any]) -> Order:
        order = order_from_ctp(raw)
        self.orders[order.order_id] = order
        return order

    def sync_orders(
        self,
        rows: Iterable[Mapping[str, Any]],
        symbols: Iterable[str] | None = None,
        replace: bool = True,
    ) -> dict[str, Order]:
        symbol_set = set(_normalize_symbols(symbols))
        new_orders = {
            order.order_id: order
            for order in (order_from_ctp(row) for row in rows)
            if not symbol_set or order.symbol in symbol_set
        }
        if symbol_set and replace:
            self.orders = {
                order_id: order
                for order_id, order in self.orders.items()
                if order.symbol not in symbol_set
            }
            self.orders.update(new_orders)
        elif replace:
            self.orders = new_orders
        else:
            self.orders.update(new_orders)
        return self.orders

    def on_rtn_trade(
        self,
        raw: Mapping[str, Any],
        contract_registry: ContractRegistry | None = None,
    ) -> Trade:
        registry = contract_registry or self.contract_registry
        trade = trade_from_ctp(raw, registry.for_symbol(str(raw.get("InstrumentID", ""))))
        self.trades[trade.trade_id] = trade
        return trade

    def sync_trades(
        self,
        rows: Iterable[Mapping[str, Any]],
        symbols: Iterable[str] | None = None,
        replace: bool = True,
    ) -> dict[str, Trade]:
        symbol_set = set(_normalize_symbols(symbols))
        new_trades: dict[str, Trade] = {}
        for row in rows:
            trade = trade_from_ctp(
                row,
                self.contract_registry.for_symbol(str(row.get("InstrumentID", ""))),
            )
            if symbol_set and trade.symbol not in symbol_set:
                continue
            new_trades[trade.trade_id] = trade
        if symbol_set and replace:
            self.trades = {
                trade_id: trade
                for trade_id, trade in self.trades.items()
                if trade.symbol not in symbol_set
            }
            self.trades.update(new_trades)
        elif replace:
            self.trades = new_trades
        else:
            self.trades.update(new_trades)
        return self.trades

    def local_order_id_for_order_ref(self, order_ref: str) -> str | None:
        for local_order_id, requests in self.local_to_ctp.items():
            if any(request.order_ref == order_ref for request in requests):
                return local_order_id
        return None

    def local_order_id_for_request_id(self, request_id: int) -> str | None:
        for local_order_id, requests in self.local_to_ctp.items():
            if any(request.request_id == request_id for request in requests):
                return local_order_id
        return None

    def order_ref_for_request_id(self, request_id: int) -> str | None:
        for requests in self.local_to_ctp.values():
            for request in requests:
                if request.request_id == request_id:
                    return request.order_ref
        return None

    def on_rtn_depth_market_data(self, raw: Mapping[str, Any]) -> Tick:
        tick = ctp_depth_market_data_to_tick(raw)
        self.ticks[tick.symbol] = tick
        return tick

    def sync_account(self, raw: Mapping[str, Any]) -> CtpTradingAccount:
        self.account = CtpTradingAccount.from_ctp(raw)
        return self.account

    def sync_positions(
        self,
        rows: Iterable[Mapping[str, Any]],
        symbols: Iterable[str] | None = None,
    ) -> dict[str, FuturesPosition]:
        symbol_set = set(_normalize_symbols(symbols))
        new_positions = futures_positions_from_ctp(rows, self.contract_registry)
        if symbol_set:
            self.positions = {
                symbol: position
                for symbol, position in self.positions.items()
                if symbol not in symbol_set
            }
            self.positions.update(
                {
                    symbol: position
                    for symbol, position in new_positions.items()
                    if symbol in symbol_set
                }
            )
        else:
            self.positions = new_positions
        return self.positions

    def _next_order_ref_text(self) -> str:
        value = f"{self._next_order_ref:012d}"
        self._next_order_ref += 1
        return value

    def _order_insert_field(
        self,
        instruction: CtpOrderInstruction,
        spec: ContractSpec,
        order_ref: str,
    ) -> dict[str, Any]:
        volume = ctp_volume(instruction.quantity)
        return {
            "BrokerID": self.config.broker_id,
            "InvestorID": self.config.investor_id,
            "UserID": self.config.user_id or self.config.investor_id,
            "ExchangeID": spec.exchange,
            "InstrumentID": instruction.symbol,
            "OrderRef": order_ref,
            "OrderPriceType": ctp_order_price_type(instruction.order_type),
            "Direction": ctp_direction(instruction.side),
            "CombOffsetFlag": ctp_offset(instruction.offset),
            "CombHedgeFlag": self.hedge_flag,
            "LimitPrice": ctp_limit_price(instruction),
            "VolumeTotalOriginal": volume,
            "TimeCondition": self.time_condition,
            "GTDDate": "",
            "VolumeCondition": self.volume_condition,
            "MinVolume": 1 if self.volume_condition == CTP_VOLUME_CONDITION_ANY else volume,
            "ContingentCondition": CTP_CONTINGENT_IMMEDIATELY,
            "StopPrice": 0.0,
            "ForceCloseReason": CTP_FORCE_CLOSE_NOT,
            "IsAutoSuspend": 0,
            "UserForceClose": 0,
            "IsSwapOrder": 0,
            "CurrencyID": self.config.currency_id,
            "RequestID": self.request_id,
        }


def split_order_for_ctp(
    order: Order,
    spec: ContractSpec,
    position: FuturesPosition | None = None,
) -> list[CtpOrderInstruction]:
    if order.offset != Offset.AUTO:
        return [_instruction_from_order(order, order.offset, order.quantity)]

    position = position or FuturesPosition(symbol=order.symbol)
    remaining = order.quantity
    instructions: list[CtpOrderInstruction] = []
    exchange = spec.exchange.upper()

    if exchange in EXCHANGES_WITH_CLOSE_TODAY:
        close_offsets = [Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY]
    else:
        close_offsets = [Offset.CLOSE]

    for close_offset in close_offsets:
        available = position.close_available(order.side, close_offset)
        close_qty = min(remaining, available)
        if close_qty > 0:
            instructions.append(_instruction_from_order(order, close_offset, close_qty))
            remaining -= close_qty
        if remaining <= 0:
            break

    if remaining > 0:
        instructions.append(_instruction_from_order(order, Offset.OPEN, remaining))

    return instructions


def order_from_ctp(raw: Mapping[str, Any]) -> Order:
    timestamp = _parse_ctp_datetime(raw.get("InsertDate"), raw.get("InsertTime"))
    status = order_status_from_ctp(str(raw.get("OrderStatus", "")))
    quantity = _float_field(raw, "VolumeTotalOriginal")
    traded = _float_field(raw, "VolumeTraded")
    order = Order(
        order_id=str(raw.get("OrderRef") or raw.get("OrderSysID") or ""),
        symbol=str(raw.get("InstrumentID", "")),
        side=side_from_ctp_direction(str(raw.get("Direction", ""))),
        quantity=quantity,
        submitted_at=timestamp,
        order_type=order_type_from_ctp(str(raw.get("OrderPriceType", ""))),
        offset=offset_from_ctp_flag(_first_char(raw.get("CombOffsetFlag"))),
        limit_price=_optional_price(raw.get("LimitPrice")),
        status=status,
        fill_price=_optional_price(raw.get("LimitPrice")) if traded else None,
        reject_reason=str(raw.get("StatusMsg", "") or "") or None,
    )
    if status == OrderStatus.FILLED:
        order.filled_at = timestamp
    return order


def trade_from_ctp(raw: Mapping[str, Any], spec: ContractSpec | None = None) -> Trade:
    spec = spec or ContractSpec(symbol=str(raw.get("InstrumentID", "")))
    price = _float_field(raw, "Price")
    quantity = _float_field(raw, "Volume")
    timestamp = _parse_ctp_datetime(raw.get("TradeDate"), raw.get("TradeTime"))
    return Trade(
        trade_id=str(raw.get("TradeID") or ""),
        order_id=str(raw.get("OrderRef") or raw.get("OrderSysID") or ""),
        symbol=str(raw.get("InstrumentID", "")),
        side=side_from_ctp_direction(str(raw.get("Direction", ""))),
        quantity=quantity,
        price=price,
        commission=0.0,
        timestamp=timestamp,
        offset=offset_from_ctp_flag(_first_char(raw.get("OffsetFlag"))),
        notional=spec.notional(price, quantity),
        margin=spec.margin(price, quantity),
    )


def ctp_depth_market_data_to_tick(raw: Mapping[str, Any]) -> Tick:
    return Tick(
        symbol=str(raw.get("InstrumentID", "")),
        timestamp=_parse_ctp_tick_datetime(raw),
        last_price=_float_or_zero(raw, "LastPrice"),
        volume=_float_or_zero(raw, "Volume"),
        turnover=_float_or_zero(raw, "Turnover"),
        open_interest=_float_or_zero(raw, "OpenInterest"),
        bid_price_1=_optional_float_field(raw, "BidPrice1"),
        bid_volume_1=_float_or_zero(raw, "BidVolume1"),
        ask_price_1=_optional_float_field(raw, "AskPrice1"),
        ask_volume_1=_float_or_zero(raw, "AskVolume1"),
        open_price=_optional_float_field(raw, "OpenPrice"),
        high_price=_optional_float_field(raw, "HighestPrice"),
        low_price=_optional_float_field(raw, "LowestPrice"),
        pre_close_price=_optional_float_field(raw, "PreClosePrice"),
        extra={
            "exchange_id": str(raw.get("ExchangeID", "") or ""),
            "trading_day": str(raw.get("TradingDay", "") or ""),
            "action_day": str(raw.get("ActionDay", "") or ""),
        },
    )


def futures_positions_from_ctp(
    rows: Iterable[Mapping[str, Any]],
    contract_registry: ContractRegistry | None = None,
) -> dict[str, FuturesPosition]:
    registry = contract_registry or ContractRegistry()
    positions: dict[str, FuturesPosition] = {}
    for row in rows:
        symbol = str(row.get("InstrumentID", ""))
        if not symbol:
            continue
        position = positions.setdefault(symbol, FuturesPosition(symbol=symbol))
        spec = registry.for_symbol(symbol)
        quantity = _float_field(row, "Position")
        if quantity <= 0:
            continue
        avg_price = _position_avg_price(row, quantity, spec.multiplier)
        direction = str(row.get("PosiDirection", ""))
        position_date = str(row.get("PositionDate", ""))

        if direction == CTP_POSITION_DIRECTION_LONG:
            if position_date == CTP_POSITION_DATE_TODAY:
                position.long_today_avg_price = _weighted_average(
                    position.long_today_avg_price,
                    position.long_today_quantity,
                    avg_price,
                    quantity,
                )
                position.long_today_quantity += quantity
            else:
                position.long_yesterday_avg_price = _weighted_average(
                    position.long_yesterday_avg_price,
                    position.long_yesterday_quantity,
                    avg_price,
                    quantity,
                )
                position.long_yesterday_quantity += quantity
        elif direction == CTP_POSITION_DIRECTION_SHORT:
            if position_date == CTP_POSITION_DATE_TODAY:
                position.short_today_avg_price = _weighted_average(
                    position.short_today_avg_price,
                    position.short_today_quantity,
                    avg_price,
                    quantity,
                )
                position.short_today_quantity += quantity
            else:
                position.short_yesterday_avg_price = _weighted_average(
                    position.short_yesterday_avg_price,
                    position.short_yesterday_quantity,
                    avg_price,
                    quantity,
                )
                position.short_yesterday_quantity += quantity
    return positions


def ctp_direction(side: Side) -> str:
    if side == Side.BUY:
        return CTP_DIRECTION_BUY
    if side == Side.SELL:
        return CTP_DIRECTION_SELL
    raise ValueError(f"unsupported side: {side}")


def side_from_ctp_direction(value: str) -> Side:
    if value == CTP_DIRECTION_BUY:
        return Side.BUY
    if value == CTP_DIRECTION_SELL:
        return Side.SELL
    raise ValueError(f"unsupported CTP direction: {value}")


def ctp_order_price_type(order_type: OrderType) -> str:
    if order_type == OrderType.MARKET:
        return CTP_ORDER_PRICE_TYPE_ANY
    if order_type == OrderType.LIMIT:
        return CTP_ORDER_PRICE_TYPE_LIMIT
    raise ValueError(f"unsupported order type: {order_type}")


def order_type_from_ctp(value: str) -> OrderType:
    if value == CTP_ORDER_PRICE_TYPE_ANY:
        return OrderType.MARKET
    if value == CTP_ORDER_PRICE_TYPE_LIMIT:
        return OrderType.LIMIT
    raise ValueError(f"unsupported CTP price type: {value}")


def ctp_offset(offset: Offset) -> str:
    mapping = {
        Offset.OPEN: CTP_OFFSET_OPEN,
        Offset.CLOSE: CTP_OFFSET_CLOSE,
        Offset.CLOSE_TODAY: CTP_OFFSET_CLOSE_TODAY,
        Offset.CLOSE_YESTERDAY: CTP_OFFSET_CLOSE_YESTERDAY,
    }
    if offset not in mapping:
        raise ValueError("AUTO offset must be split before sending to CTP")
    return mapping[offset]


def offset_from_ctp_flag(value: str) -> Offset:
    mapping = {
        CTP_OFFSET_OPEN: Offset.OPEN,
        CTP_OFFSET_CLOSE: Offset.CLOSE,
        CTP_OFFSET_CLOSE_TODAY: Offset.CLOSE_TODAY,
        CTP_OFFSET_CLOSE_YESTERDAY: Offset.CLOSE_YESTERDAY,
    }
    return mapping.get(value, Offset.AUTO)


def order_status_from_ctp(value: str) -> OrderStatus:
    if value == CTP_ORDER_STATUS_ALL_TRADED:
        return OrderStatus.FILLED
    if value in {
        CTP_ORDER_STATUS_CANCELED,
        CTP_ORDER_STATUS_NO_TRADE_NOT_QUEUEING,
        CTP_ORDER_STATUS_PART_TRADED_NOT_QUEUEING,
    }:
        return OrderStatus.CANCELED
    return OrderStatus.PENDING


def ctp_volume(quantity: float) -> int:
    if quantity <= 0:
        raise ValueError("CTP volume must be positive")
    if abs(quantity - round(quantity)) > 1e-12:
        raise ValueError("CTP futures volume must be an integer number of contracts")
    return int(round(quantity))


def ctp_limit_price(instruction: CtpOrderInstruction) -> float:
    if instruction.order_type == OrderType.LIMIT:
        if instruction.limit_price is None:
            raise ValueError("limit_price is required for CTP limit orders")
        return float(instruction.limit_price)
    return 0.0


def ctp_authenticate_field(config: CtpConnectionConfig) -> dict[str, Any]:
    return {
        "BrokerID": config.broker_id,
        "UserID": config.user_id or config.investor_id,
        "UserProductInfo": config.product_info,
        "AuthCode": config.auth_code,
        "AppID": config.app_id,
    }


def ctp_login_field(config: CtpConnectionConfig) -> dict[str, Any]:
    return {
        "BrokerID": config.broker_id,
        "UserID": config.user_id or config.investor_id,
        "Password": config.password,
        "UserProductInfo": config.product_info,
        "InterfaceProductInfo": config.product_info,
    }


def ctp_md_login_field(config: CtpConnectionConfig) -> dict[str, Any]:
    return {
        "BrokerID": config.broker_id,
        "UserID": config.user_id or config.investor_id,
        "Password": config.password,
    }


def ctp_settlement_confirm_field(config: CtpConnectionConfig) -> dict[str, Any]:
    return {
        "BrokerID": config.broker_id,
        "InvestorID": config.investor_id,
        "CurrencyID": config.currency_id,
    }


def ctp_query_account_field(config: CtpConnectionConfig) -> dict[str, Any]:
    return {
        "BrokerID": config.broker_id,
        "InvestorID": config.investor_id,
        "CurrencyID": config.currency_id,
    }


def ctp_query_position_field(
    config: CtpConnectionConfig,
    symbol: str | None = None,
) -> dict[str, Any]:
    field = {
        "BrokerID": config.broker_id,
        "InvestorID": config.investor_id,
    }
    if symbol:
        field["InstrumentID"] = symbol
    return field


def ctp_query_order_field(
    config: CtpConnectionConfig,
    symbol: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    field = {
        "BrokerID": config.broker_id,
        "InvestorID": config.investor_id,
    }
    if symbol:
        field["InstrumentID"] = symbol
    if start_time:
        field["InsertTimeStart"] = start_time
    if end_time:
        field["InsertTimeEnd"] = end_time
    return field


def ctp_query_trade_field(
    config: CtpConnectionConfig,
    symbol: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    field = {
        "BrokerID": config.broker_id,
        "InvestorID": config.investor_id,
    }
    if symbol:
        field["InstrumentID"] = symbol
    if start_time:
        field["TradeTimeStart"] = start_time
    if end_time:
        field["TradeTimeEnd"] = end_time
    return field


def event_to_dict(event: CtpLifecycleEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type,
        "ok": event.ok,
        "request_id": event.request_id,
        "message": event.message,
        "payload": event.payload,
    }


def callback_event_to_dict(event: CtpCallbackEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type,
        "ok": event.ok,
        "request_id": event.request_id,
        "is_last": event.is_last,
        "message": event.message,
        "data": event.data,
        "rsp_info": event.rsp_info,
    }


def tick_to_dict(tick: Tick) -> dict[str, Any]:
    return {
        "symbol": tick.symbol,
        "timestamp": tick.timestamp.isoformat(),
        "last_price": tick.last_price,
        "volume": tick.volume,
        "turnover": tick.turnover,
        "open_interest": tick.open_interest,
        "bid_price_1": tick.bid_price_1,
        "bid_volume_1": tick.bid_volume_1,
        "ask_price_1": tick.ask_price_1,
        "ask_volume_1": tick.ask_volume_1,
        "open_price": tick.open_price,
        "high_price": tick.high_price,
        "low_price": tick.low_price,
        "pre_close_price": tick.pre_close_price,
        "extra": tick.extra,
    }


def ctp_to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "_asdict"):
        return dict(value._asdict())

    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if callable(item):
            continue
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="ignore")
        result[name] = item
    return result


def ctp_response_ok(rsp_info: Mapping[str, Any] | None) -> bool:
    info = rsp_info or {}
    try:
        error_id = int(info.get("ErrorID", info.get("error_id", 0)) or 0)
    except (TypeError, ValueError):
        return False
    return error_id == 0


def ctp_response_message(rsp_info: Mapping[str, Any] | None) -> str:
    info = rsp_info or {}
    message = str(info.get("ErrorMsg", info.get("error_msg", "")) or "")
    return message or "ok"


def _instruction_from_order(
    order: Order,
    offset: Offset,
    quantity: float,
) -> CtpOrderInstruction:
    return CtpOrderInstruction(
        local_order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        offset=offset,
        quantity=quantity,
        order_type=order.order_type,
        limit_price=order.limit_price,
    )


def _resolve_attr(module: Any, dotted_name: str) -> Any:
    current = module
    for part in dotted_name.split("."):
        current = getattr(current, part)
    return current


def _redact_field(field: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(field)
    for key in ("Password", "AuthCode", "password", "auth_code"):
        if key in redacted and redacted[key]:
            redacted[key] = "***"
    return redacted


def _is_position_rows(value: Any) -> bool:
    return _position_rows(value) is not None


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


def _filter_ctp_query_rows(
    rows: Iterable[Mapping[str, Any]],
    instrument_id: Any = None,
    time_key: str | None = None,
    start_time: Any = None,
    end_time: Any = None,
) -> list[Mapping[str, Any]]:
    symbol = str(instrument_id or "")
    start = str(start_time or "")
    end = str(end_time or "")
    filtered: list[Mapping[str, Any]] = []
    for row in rows:
        if symbol and str(row.get("InstrumentID", "")) != symbol:
            continue
        if time_key:
            row_time = str(row.get(time_key, "") or "")
            if start and row_time < start:
                continue
            if end and row_time > end:
                continue
        filtered.append(row)
    return filtered


def _position_rows(value: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(value, (str, bytes, Mapping)) or value is None:
        return None
    try:
        rows = list(value)
    except TypeError:
        return None
    if all(isinstance(row, Mapping) for row in rows):
        return rows
    return None


def _float_field(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key, 0)
    if value in {"", None}:
        return 0.0
    return float(value)


def _optional_float_field(raw: Mapping[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value in {"", None}:
        return None
    number = float(value)
    if abs(number) > 1e100:
        return None
    return number


def _float_or_zero(raw: Mapping[str, Any], key: str) -> float:
    value = _optional_float_field(raw, key)
    return 0.0 if value is None else value


def _optional_price(value: Any) -> float | None:
    if value in {"", None}:
        return None
    price = float(value)
    if abs(price) > 1e100:
        return None
    return price if price else None


def _first_char(value: Any) -> str:
    text = str(value or "")
    return text[0] if text else ""


def _parse_ctp_datetime(date_value: Any, time_value: Any) -> datetime:
    date_text = str(date_value or "19700101").replace("-", "")
    time_text = str(time_value or "00:00:00")
    if "." in time_text:
        time_text = time_text.split(".", 1)[0]
    return datetime.strptime(f"{date_text} {time_text}", "%Y%m%d %H:%M:%S")


def _parse_ctp_tick_datetime(raw: Mapping[str, Any]) -> datetime:
    timestamp = _parse_ctp_datetime(
        raw.get("ActionDay") or raw.get("TradingDay"),
        raw.get("UpdateTime"),
    )
    try:
        millis = int(raw.get("UpdateMillisec", raw.get("UpdateMilliSec", 0)) or 0)
    except (TypeError, ValueError):
        millis = 0
    if millis:
        timestamp = timestamp.replace(microsecond=millis * 1000)
    return timestamp


def _position_avg_price(
    row: Mapping[str, Any],
    quantity: float,
    multiplier: float,
) -> float:
    cost = _float_field(row, "PositionCost") or _float_field(row, "OpenCost")
    if cost and quantity and multiplier:
        return cost / quantity / multiplier
    return _float_field(row, "OpenPrice")


def _weighted_average(
    old_price: float,
    old_quantity: float,
    price: float,
    quantity: float,
) -> float:
    new_quantity = old_quantity + quantity
    if new_quantity == 0:
        return 0.0
    return (old_price * old_quantity + price * quantity) / new_quantity
