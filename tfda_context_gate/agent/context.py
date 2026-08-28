from __future__ import annotations

# ── Agent 上下文組裝（繁中註解）──────────────────────────────────────────────
# 職責：將 B 結果窄化為 Planner 可見的 AgentDecisionContext，不含 WorkflowState 全量。
# 關鍵：evidence_summaries 僅投影前 5 筆、_limited_feedback 僅保留小欄位，避免洩漏原文或誤導 Planner。

from collections.abc import Mapping
from typing import Any

from tfda_context_gate.b_context_gate.schemas import CanonicalBResult, CanonicalEvidence

from .schemas import AgentAttempt, AgentDecisionContext, EvidenceSummary


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _short_snippet(content: str, limit: int = 220) -> str:
    compact = " ".join(str(content).split())
    return compact[:limit]


def evidence_summaries(
    evidence: list[CanonicalEvidence], *, top_k: int = 5
) -> list[EvidenceSummary]:
    """Project evidence without exposing raw documents to the Planner.

    【繁中註解】僅投影前 top_k 筆證據的 id/rank/score/ingredient/title/source/date/snippet，
    不暴露原文全文；snippet 截斷至 220 字，避免 Planner 將候選誤作使用者事實。
    """

    summaries: list[EvidenceSummary] = []
    for rank, item in enumerate(evidence[:top_k], 1):
        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        summaries.append(
            EvidenceSummary(
                evidence_id=item.evidence_id,
                rank=rank,
                score=item.score,
                ingredient=_metadata_text(metadata, "藥品成分", "ingredient", "drug") ,
                title=_metadata_text(metadata, "title", "標題", "風險標題"),
                source=item.source or _metadata_text(metadata, "source", "source_dataset"),
                date=item.date or _metadata_text(metadata, "發布日期", "date"),
                snippet=_short_snippet(item.content),
            )
        )
    return summaries


def _limited_feedback(value: Mapping[str, Any]) -> dict[str, object]:
    """Keep only small, non-authoritative B feedback fields.

    【繁中註解】僅保留 retrieval_queries/duplicate_ids/retrieval_status 三小欄位，
    避免將 B 的內部細節或權威性判斷洩漏給 Planner。
    """

    allowed = {"retrieval_queries", "duplicate_ids", "retrieval_status"}
    result: dict[str, object] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or (
            isinstance(item, list) and all(isinstance(v, (str, int, float, bool)) for v in item)
        ):
            result[key] = item
    return result


def build_agent_decision_context(
    *,
    original_query: str,
    current_query: str,
    b_result: CanonicalBResult,
    previous_attempts: list[AgentAttempt],
) -> AgentDecisionContext:
    """Build the only object passed into an Agent Planner.

    【繁中註解｜窄化上下文】此為唯一傳入 Planner 的物件，非 WorkflowState：
    - original_query/current_query 雙軌溯源
    - b_decision/b_reason_codes/identified_missing_information（中性觀察）
    - 受限 retrieval_feedback + 至多 5 筆 evidence_summaries + 至多 2 筆 previous_attempts
    """

    feedback = b_result.retrieval_feedback
    return AgentDecisionContext(
        original_query=original_query,
        current_query=current_query,
        b_decision=b_result.decision,
        b_reason_codes=list(b_result.reason_codes)[:8],
        identified_missing_information=list(b_result.identified_missing_information)[:8],
        retrieval_feedback=_limited_feedback(feedback),
        evidence_summaries=evidence_summaries(b_result.evidence),
        previous_attempts=list(previous_attempts)[-2:],
    )
