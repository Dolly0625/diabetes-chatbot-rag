from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

from tfda_context_gate.b_context_gate.adapters import normalize_evidence_list
from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import RAGResult


class Retriever(Protocol):
    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        ...


def adapt_legacy_retrieval(
    records: list[Any],
    *,
    request_id: str,
    original_query: str,
    retrieval_queries: list[str],
    retrieval_latency_ms: float | None = None,
) -> RAGResult:
    """Normalize rows from phase 2/3/5 without modifying those experiments."""

    return RAGResult(
        request_id=request_id,
        original_query=original_query,
        retrieval_queries=retrieval_queries,
        evidence=normalize_evidence_list(records),
        retrieval_latency_ms=retrieval_latency_ms,
    )


def default_fixture_evidence() -> list[CanonicalEvidence]:
    """Explicit fixture data for the offline E2E demo, not a clinical corpus."""

    return [
        CanonicalEvidence(
            evidence_id="E1",
            content="一般糖尿病飲食原則包括均衡飲食與控制總熱量。",
            source="fixture",
            metadata={"fixture_case": "normal", "fixture_b_approved": True},
        ),
        CanonicalEvidence(
            evidence_id="E2",
            content="飲食安排應依個人狀況與醫療專業人員建議調整。",
            source="fixture",
            metadata={"fixture_case": "normal", "fixture_b_approved": True},
        ),
        CanonicalEvidence(
            evidence_id="E3",
            content="本筆是檢查 evidence boundary 的未核准候選資料。",
            source="fixture",
            metadata={"fixture_case": "normal", "fixture_b_approved": False},
        ),
    ]


class FixtureRetriever:
    """Offline RAG fixture used by the deterministic workflow demo/tests."""

    name = "fixture-retriever"

    def __init__(self, evidence: list[CanonicalEvidence] | None = None) -> None:
        self.evidence = list(evidence) if evidence is not None else default_fixture_evidence()

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        started = time.perf_counter()
        # The fixture keeps retrieval deterministic and preserves the exact
        # original query; no result is claimed from the live phase scripts.
        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=request.retrieval_queries,
            evidence=list(self.evidence),
            retrieval_latency_ms=(time.perf_counter() - started) * 1000,
        )

