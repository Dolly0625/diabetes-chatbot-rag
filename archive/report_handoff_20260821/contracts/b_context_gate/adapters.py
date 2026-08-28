from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .schemas import B_SCHEMA_VERSION, CanonicalBResult, CanonicalEvidence


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"expected mapping-like value, got {type(value).__name__}")


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def normalize_evidence(value: Any) -> CanonicalEvidence:
    """Adapt old ``document_id``/``chunk_id`` retrieval rows to evidence_id."""

    raw = _as_dict(value)
    evidence_id = _first(raw, "evidence_id", "document_id", "chunk_id", "id")
    content = _first(raw, "content", "page_content", "text")
    if evidence_id is None:
        raise ValueError("retrieval record has no evidence/document/chunk identifier")
    if content is None or not str(content).strip():
        raise ValueError(f"retrieval record {evidence_id!r} has no content")

    raw_metadata = raw.get("metadata", {})
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    known_keys = {
        "evidence_id",
        "document_id",
        "chunk_id",
        "id",
        "content",
        "page_content",
        "text",
        "metadata",
        "source",
        "source_dataset",
        "score",
        "similarity_score",
        "reranker_score",
        "date",
        "version",
    }
    for key, item in raw.items():
        if key not in known_keys and key not in metadata:
            metadata[key] = item

    source = _first(raw, "source", "source_dataset")
    if source is None:
        source = metadata.get("source") or metadata.get("source_dataset")
    date = _first(raw, "date")
    if date is None:
        date = metadata.get("date") or metadata.get("發布日期")
    version = _first(raw, "version")
    if version is None:
        version = metadata.get("version")
    score = _first(raw, "score", "similarity_score", "reranker_score")

    return CanonicalEvidence(
        evidence_id=str(evidence_id),
        content=str(content),
        source=str(source) if source is not None else None,
        metadata=metadata,
        score=float(score) if score is not None else None,
        date=str(date) if date is not None else None,
        version=str(version) if version is not None else None,
    )


def normalize_evidence_list(values: list[Any] | None) -> list[CanonicalEvidence]:
    return [normalize_evidence(value) for value in (values or [])]


def adapt_legacy_b_result(
    value: Any,
    *,
    request_id: str,
    original_query: str,
    retrieval_queries: list[str] | None = None,
) -> CanonicalBResult:
    """Adapt phase-script B results while keeping their source untouched."""

    raw = _as_dict(value)
    decision = _first(raw, "decision", "b_decision")
    if decision is None:
        raise ValueError("legacy B result has no decision/b_decision")
    approved = _first(raw, "approved_evidence_ids", "approved_document_ids") or []
    raw_evidence = _first(raw, "evidence", "contexts", "retrieved_contexts", "context_rows") or []
    evidence = normalize_evidence_list(raw_evidence)
    return CanonicalBResult(
        request_id=request_id,
        schema_version=str(raw.get("schema_version", B_SCHEMA_VERSION)),
        decision=str(decision),
        approved_evidence_ids=[str(item) for item in approved],
        evidence=evidence,
        reason_codes=[str(item) for item in (raw.get("reason_codes") or [])],
        identified_missing_information=[
            str(item) for item in (raw.get("identified_missing_information") or [])
        ],
        retrieval_feedback={
            "original_query": original_query,
            "retrieval_queries": retrieval_queries or [],
            **(dict(raw.get("retrieval_feedback")) if isinstance(raw.get("retrieval_feedback"), Mapping) else {}),
        },
        relevance=raw.get("relevance"),
        sufficiency=raw.get("sufficiency"),
        conflict=raw.get("conflict"),
        safety=raw.get("safety"),
    )
