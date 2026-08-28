from __future__ import annotations

# ── 轉接層（繁中註解）────────────────────────────────────────────────────
# 本檔僅做「形狀轉換」，不含業務判斷：
# A → QueryExpansion → RAG → B → C → D 的輸入輸出在此對齊，
# 確保 LangGraph 各節點拿到正確的 Pydantic / dict 形狀。
# 邏輯不變，僅加註解說明資料流向。

from typing import Any, Union

from tfda_context_gate.a_router.schemas import AResult
from tfda_context_gate.b_context_gate.schemas import CanonicalBResult
from tfda_context_gate.c_generator.schemas import ClinicianEvidenceDraft, EvidenceAwareV2Answer
from tfda_context_gate.c_generator.workflow_adapter import CWorkflowInput, c_input_from_b_result
from tfda_context_gate.query_expansion.adapters import from_a_result
from tfda_context_gate.query_expansion.schemas import QueryExpansionInput
from tfda_context_gate.rag.schemas import RAGResult, rag_to_b_input


def a_to_query_expansion(a_result: AResult) -> QueryExpansionInput:
    # A 結果轉查詢擴寫輸入：沿用 query_expansion.adapters.from_a_result
    return from_a_result(a_result)


def rag_to_b(rag_result: RAGResult):
    # RAG 結果轉 B 輸入：沿用 rag.schemas.rag_to_b_input
    return rag_to_b_input(rag_result)


def b_to_c(b_result: CanonicalBResult, *, original_query: str, intake: Any | None = None) -> CWorkflowInput:
    # B 結果轉 C 輸入：需帶 original_query 以保留原始提問溯源（非 current_query）；intake 用於詳細版 4 段結構
    return c_input_from_b_result(b_result, original_query=original_query, intake=intake)


def _normalize_c_answer_for_d(c_payload: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    try:
        ans = c_payload.get("answer", "")
        if not isinstance(ans, str) or not ans:
            return c_payload
        decision = c_payload.get("decision")
        if decision == "CLINICIAN_DRAFT":
            return c_payload
        if "根據提供的資料" not in ans:
            if decision in ("ANSWER", "PARTIAL"):
                from tfda_context_gate.c_generator.deterministic_generators import (
                    GROUNDED_PREFIX_TEMPLATES,
                    _pick_grounded_prefix,
                )
                if not any(ans.startswith(p) for p in GROUNDED_PREFIX_TEMPLATES):
                    query = c_payload.get("request_id", "") or ""
                    prefix = _pick_grounded_prefix(request_id, query)
                    c_payload["answer"] = prefix + ans
            return c_payload
        from tfda_context_gate.c_generator.deterministic_generators import _pick_grounded_prefix

        cleaned = ans.replace("根據提供的資料：", "").replace("根據提供的資料:", "").replace("根據提供的資料", "").lstrip(" ：: \n。")
        query_hint = c_payload.get("request_id", "") or ""
        prefix = _pick_grounded_prefix(request_id, query_hint)
        c_payload["answer"] = prefix + cleaned
    except Exception:
        pass
    return c_payload


def c_to_d(
    *,
    request_id: str,
    a_result: AResult,
    b_result: CanonicalBResult,
    c_result: Union[EvidenceAwareV2Answer, ClinicianEvidenceDraft, dict[str, Any]],
) -> dict[str, Any]:
    if hasattr(c_result, "model_dump"):
        c_payload = c_result.model_dump(mode="json")
    else:
        c_payload = dict(c_result)
    c_payload = _normalize_c_answer_for_d(c_payload, request_id=request_id)
    return {
        "request_id": request_id,
        "schema_version": "d.v0.1",
        "a_result": a_result.model_dump(mode="json"),
        "b_result": b_result.model_dump(mode="json"),
        "c_result": c_payload,
    }

