"""E 觀測層 Sink（寫出端）— append-only、fail-open 的持久化邊界。

本模組定義 E 層如何將 TraceRecord 寫出：
- TraceSink Protocol：統一 emit(record) 介面，任何實作皆可替換
- InMemoryTraceSink：記憶體 sink，用於測試與呼叫方自行匯出
- JsonlTraceSink：append-only JSONL 檔案 sink，無外部依賴，執行緒安全

與 tracer.py 的 fail-open 對應：
- TraceRecorder._emit() 以 try/except 包住 sink.emit()，任何序列化或檔案系統錯誤
  僅記錄到 sink_errors，不會拋出或覆蓋業務結果（見 tracer.py _emit 註解）
- JsonlTraceSink 內部以 threading.Lock 保證多執行緒 append 安全
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol, Union

from .schemas import EvaluationRecord, MetricsSnapshot, TraceEvent


# TraceRecord 聯合型別：E 層所有可持久化紀錄（事件 / 評估 / 指標快照）
TraceRecord = Union[TraceEvent, EvaluationRecord, MetricsSnapshot]


class TraceSink(Protocol):
    """TraceSink 協定：任何可接收 TraceRecord 的寫出端皆需實作 emit。"""

    def emit(self, record: TraceRecord) -> None:
        """寫出一筆紀錄（由 TraceRecorder._emit 呼叫，fail-open 包裝）。"""
        ...


class InMemoryTraceSink:
    """記憶體 Sink：用於測試與呼叫方自行匯出。

    特性：
    - 無 I/O，直接將 record.model_dump(mode="json") 存入 self.records
    - 適合單測斷言與上層自行批次匯出
    """

    def __init__(self) -> None:
        self.records: list[dict] = []  # 已寫出的 JSON 化紀錄列表

    def emit(self, record: TraceRecord) -> None:
        """將紀錄序列化為 JSON 相容 dict 並存入記憶體列表。"""
        self.records.append(record.model_dump(mode="json"))


class JsonlTraceSink:
    """Append-only JSONL 檔案 Sink，無外部可觀測依賴。

    特性：
    - 每次 emit 以 JSONL 形式 append 一行（json.dumps + sort_keys + ensure_ascii=False）
    - 以 threading.Lock 保證多執行緒併發寫入安全
    - 構造時自動建立父目錄（mkdir parents=True）
    - 失敗時由上層 TraceRecorder._emit 捕獲並 fail-open，不影響業務
    """

    def __init__(self, path: Path | str) -> None:
        """初始化 JSONL Sink。

        參數:
            path: JSONL 檔案路徑（字串或 Path），父目錄不存在時自動建立
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 確保父目錄存在
        self._lock = threading.Lock()  # 保護併發 append 寫入

    def emit(self, record: TraceRecord) -> None:
        """將紀錄序列化為 JSON 行並 append 寫入檔案。

        參數:
            record: 待寫出的 TraceRecord（TraceEvent / EvaluationRecord / MetricsSnapshot）

        實作細節：
        - 先 json.dumps（sort_keys=True 確保穩定輸出，ensure_ascii=False 保留中文）
        - 再以 _lock 保護、以 "a" 模式開啟檔案並寫入一行
        """
        line = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._lock:  # 執行緒安全：確保多執行緒下 JSONL 行不交錯
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
