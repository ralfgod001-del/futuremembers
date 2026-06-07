from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class RunEvent:
    timestamp: datetime | None
    event_type: str
    message: str
    severity: str = "INFO"
    symbol: str | None = None
    order_id: str | None = None
    trade_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self._jsonl_path: Path | None = None
        self._jsonl_max_bytes: int | None = None
        self._jsonl_backup_count: int = 0

    def enable_jsonl(
        self,
        path: str | Path,
        include_existing: bool = True,
        max_bytes: int | None = None,
        backup_count: int = 0,
    ) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = target
        self._jsonl_max_bytes = (
            int(max_bytes) if max_bytes is not None and int(max_bytes) > 0 else None
        )
        self._jsonl_backup_count = max(int(backup_count), 0)
        if include_existing and self.events:
            target.touch(exist_ok=True)
            for event in self.events:
                self._append_jsonl(event)
        else:
            target.touch(exist_ok=True)
        return target

    def record(
        self,
        timestamp: datetime | None,
        event_type: str,
        message: str,
        severity: str = "INFO",
        symbol: str | None = None,
        order_id: str | None = None,
        trade_id: str | None = None,
        **payload: Any,
    ) -> None:
        self.events.append(
            RunEvent(
                timestamp=timestamp,
                event_type=event_type,
                message=message,
                severity=severity,
                symbol=symbol,
                order_id=order_id,
                trade_id=trade_id,
                payload={key: value for key, value in payload.items() if value is not None},
            )
        )
        self._append_jsonl(self.events[-1])

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                event_to_row(event)
                for event in self.events
            ]
        )

    def export_csv(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.to_frame().to_csv(target, index=False)
        return target

    def _append_jsonl(self, event: RunEvent) -> None:
        if self._jsonl_path is None:
            return
        line = json.dumps(event_to_dict(event), ensure_ascii=False, default=str) + "\n"
        self._rotate_jsonl_if_needed(len(line.encode("utf-8")))
        with self._jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _rotate_jsonl_if_needed(self, incoming_bytes: int) -> None:
        if self._jsonl_path is None or self._jsonl_max_bytes is None:
            return
        if not self._jsonl_path.exists():
            self._jsonl_path.touch()
            return
        current_size = self._jsonl_path.stat().st_size
        if current_size == 0:
            return
        if current_size + incoming_bytes <= self._jsonl_max_bytes:
            return
        self._rotate_jsonl()

    def _rotate_jsonl(self) -> None:
        if self._jsonl_path is None:
            return
        if self._jsonl_backup_count <= 0:
            self._jsonl_path.unlink(missing_ok=True)
            self._jsonl_path.touch()
            return

        oldest = self._backup_path(self._jsonl_backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self._jsonl_backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        if self._jsonl_path.exists():
            self._jsonl_path.replace(self._backup_path(1))
        self._jsonl_path.touch()

    def _backup_path(self, index: int) -> Path:
        assert self._jsonl_path is not None
        return self._jsonl_path.with_name(f"{self._jsonl_path.name}.{index}")


def event_to_dict(event: RunEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "event_type": event.event_type,
        "severity": event.severity,
        "symbol": event.symbol,
        "order_id": event.order_id,
        "trade_id": event.trade_id,
        "message": event.message,
        "payload": event.payload,
    }


def event_to_row(event: RunEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "event_type": event.event_type,
        "severity": event.severity,
        "symbol": event.symbol,
        "order_id": event.order_id,
        "trade_id": event.trade_id,
        "message": event.message,
        "payload": json.dumps(event.payload, sort_keys=True, default=str),
    }
