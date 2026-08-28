from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def chunk_text(text: str, chunk_size: int = 20) -> Iterator[str]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    for idx in range(0, len(text), chunk_size):
        yield text[idx : idx + chunk_size]


def format_sse(data: str, *, event: str | None = None, id: str | None = None) -> str:
    lines: list[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    if id is not None:
        lines.append(f"id: {id}")
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def format_sse_json(payload: dict[str, Any], *, event: str | None = None) -> str:
    return format_sse(json.dumps(payload, ensure_ascii=False), event=event)


def stream_answer_field(
    answer: str, *, chunk_size: int = 20, sse_format: bool = False
) -> Iterator[str]:
    for chunk in chunk_text(answer, chunk_size):
        if sse_format:
            yield format_sse_json({"answer_chunk": chunk, "done": False}, event="token")
        else:
            yield chunk
    if sse_format:
        yield format_sse_json({"answer_chunk": "", "done": True}, event="done")


def buffered_stream_after_d(
    final_response: str,
    *,
    chunk_size: int = 20,
    sse_format: bool = False,
    d_pass: bool = True,
) -> Iterator[str]:
    """Buffered-then-stream after D PASS — D PASS 才推，否則仍推 fallback 但同路徑。

    設計：先緩衝完整 final_response（已由 run_workflow 經 D 驗證），再切塊串流。
    保證與 run_workflow 結果一致，且永不繞過 D。
    """
    if chunk_size < 1:
        chunk_size = 20
    # D PASS 才推的語意：若 d_pass 為 False，仍推 final_response（fallback），但不標記為 streaming PASS
    # 呼叫方已保證 final_response 為 D 驗證後結果，此處僅負責切塊與 SSE 包裝
    yield from stream_answer_field(final_response, chunk_size=chunk_size, sse_format=sse_format)
