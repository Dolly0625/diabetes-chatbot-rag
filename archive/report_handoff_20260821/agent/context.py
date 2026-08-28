from __future__ import annotations

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
    """Project evidence without exposing raw documents to the Planner."""

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
    """Keep only small, non-authoritative B feedback fields."""

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
    """Build the only object passed into an Agent Planner."""

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
