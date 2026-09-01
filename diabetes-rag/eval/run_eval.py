#!/usr/bin/env python3
"""對 queries.json 裡約 20 筆標記查詢（10 筆 graph 軌道、10 筆 vector
軌道）計算 Recall@k／Precision／F1。建置順序第 12 步——回應子龍學長 8/26
的批評：只描述失敗模式而沒有量化。

vector 軌道需要 GEMINI_API_KEY 與網路。沒有的話，vector 軌道的查詢仍會
執行（tool.py 會優雅降級——只回傳 graph 結果，並帶 RETRIEVER_DEGRADED
warning），但這裡會分開回報，不會併成單一數字，因為網路缺失與真正的
檢索品質問題是兩種不同的失敗模式，混在一起會誤導判讀。依 CLAUDE.md
§9：回報真實數字，不為了讓指標好看而調整標記資料集。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_retrieval import EvidenceRetrievalTool  # noqa: E402
from rag_retrieval.contract.enums import WarningCode  # noqa: E402
from rag_retrieval.gate_out import DEFAULT_TOP_N  # noqa: E402

QUERIES_PATH = Path(__file__).resolve().parent / "queries.json"


def _request(query: dict) -> dict:
    return {
        "request_id": query["query_id"],
        "schema_version": "rag-v1",
        "user_raw_input": query["user_raw_input"],
        "retrieval_queries": query["retrieval_queries"],
        "guardrail_result": {
            "intent_tags": ["GENERAL_EDUCATION"],
            "risk_flags": [],
            "context_modifiers": {
                "time_frame": "CURRENT",
                "target_subject": "SELF",
                "polarity": "AFFIRMATIVE",
                "language": "zh-TW",
            },
            "router_status": "G_GENERAL_EDUCATION",
            "reason_codes": ["MEETS_SAFE_SCOPE"],
        },
        "language": "zh-TW",
        "timestamp": "2026-09-03T14:00:00+08:00",
    }


def _score(expected: set[str], retrieved: list[str]) -> tuple[float, float, float]:
    retrieved_set = set(retrieved)
    hits = len(expected & retrieved_set)
    recall = hits / len(expected) if expected else 0.0
    precision = hits / len(retrieved_set) if retrieved_set else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return recall, precision, f1


def run(queries_path: Path = QUERIES_PATH) -> list[dict]:
    queries = json.loads(queries_path.read_text(encoding="utf-8"))
    tool = EvidenceRetrievalTool()

    rows = []
    for query in queries:
        response = tool.retrieve(_request(query))
        retrieved_ids = [chunk.chunk_id for chunk in response.chunks]
        expected = set(query["expected_chunk_ids"])
        recall, precision, f1 = _score(expected, retrieved_ids)
        vector_degraded = WarningCode.RETRIEVER_DEGRADED in {w.code for w in response.warnings}
        # 每筆查詢都走 HYBRID 路由，所以沒有網路的環境會在**每個**回應上
        # 都標記 RETRIEVER_DEGRADED——但這只有在該查詢的預期證據真的落在
        # vector 軌道時才會讓分數失真。graph 軌道查詢的分數不會受到旁邊
        # vector 軌道失敗的影響。
        affected = vector_degraded and query["track"] == "vector"
        rows.append(
            {
                "query_id": query["query_id"],
                "track": query["track"],
                "recall": recall,
                "precision": precision,
                "f1": f1,
                "retrieved": retrieved_ids,
                "expected": sorted(expected),
                "vector_degraded": vector_degraded,
                "excluded": affected,
            }
        )
    return rows


def _macro_avg(rows: list[dict], key: str) -> float:
    return sum(row[key] for row in rows) / len(rows) if rows else 0.0


def report(rows: list[dict]) -> None:
    print(f"{'query_id':<9}{'track':<8}{'recall':>7}{'precision':>11}{'f1':>7}  note")
    for row in rows:
        note = "vector track degraded (no network) — excluded" if row["excluded"] else ""
        print(
            f"{row['query_id']:<9}{row['track']:<8}{row['recall']:>7.2f}"
            f"{row['precision']:>11.2f}{row['f1']:>7.2f}  {note}"
        )

    print()
    for track in ("graph", "vector", None):
        subset = rows if track is None else [r for r in rows if r["track"] == track]
        label = "ALL" if track is None else track
        clean = [r for r in subset if not r["excluded"]]
        skipped = len(subset) - len(clean)
        skip_note = f" (skipped {skipped}: vector track degraded, no network)" if skipped else ""
        print(
            f"{label:<6} n={len(subset)}{skip_note}  "
            f"Recall@{DEFAULT_TOP_N}={_macro_avg(clean, 'recall'):.2f}  "
            f"Precision={_macro_avg(clean, 'precision'):.2f}  "
            f"F1={_macro_avg(clean, 'f1'):.2f}"
        )


def print_module_status() -> None:
    """Print which delegable modules are still on their default implementation.

    Reads the table in MODULE_STATUS.md rather than inspecting code: whether a
    module counts as "delivered" is a review decision (was the merge request
    accepted), not something that can be inferred from a file existing. The
    file is the single source of truth; this only surfaces it next to the
    numbers so the two are always read together.
    """
    status_path = Path(__file__).resolve().parents[1] / "MODULE_STATUS.md"
    if not status_path.exists():
        return

    rows = []
    for line in status_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in {"Module", "---"} or set(cells[0]) <= {"-"}:
            continue
        module, _file, owner, status = cells[0], cells[1], cells[2], cells[3]
        rows.append((module, owner, status.replace("**", "")))

    if not rows:
        return

    on_default = [r for r in rows if r[2] != "delivered"]
    print()
    print(f"module status: {len(on_default)}/{len(rows)} still on default implementations")
    for module, owner, status in rows:
        mark = " " if status == "delivered" else "!"
        print(f"  {mark} {module:<28}{owner:<18}{status}")
    if on_default:
        print("  see MODULE_STATUS.md for what each default costs")


if __name__ == "__main__":
    report(run())
    print_module_status()
