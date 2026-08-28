from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol, Union

from .schemas import EvaluationRecord, MetricsSnapshot, TraceEvent


TraceRecord = Union[TraceEvent, EvaluationRecord, MetricsSnapshot]


class TraceSink(Protocol):
    def emit(self, record: TraceRecord) -> None:
        ...


class InMemoryTraceSink:
    """Useful for tests and a caller that wants to export records itself."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit(self, record: TraceRecord) -> None:
        self.records.append(record.model_dump(mode="json"))


class JsonlTraceSink:
    """Append-only JSONL sink with no external observability dependency."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, record: TraceRecord) -> None:
        line = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
