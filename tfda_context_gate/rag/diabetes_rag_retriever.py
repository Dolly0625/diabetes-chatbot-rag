"""In-process adapter for the separately maintained ``diabetes-rag`` package.

The two projects intentionally have different versioned contracts.  This file
is the only place that translates from the main workflow's A/QueryExpansion
boundary to diabetes-rag's frozen ``rag-v1`` boundary.  It then converts the
untrusted retrieval response back to ``RAGResult`` so Context Gate B remains
the sole authority that may approve evidence for generation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from tfda_context_gate.a_router.schemas import AResult
from tfda_context_gate.b_context_gate.adapters import normalize_evidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import RAGResult


class DiabetesRAGRetriever:
    """Use ``rag_retrieval.EvidenceRetrievalTool`` without an HTTP hop.

    ``retrieve_with_guardrail`` is deliberate: the graph recognises this
    method and supplies the original A result.  Therefore callers cannot send
    an expansion to this backend while bypassing the main router's decision.
    """

    name = "diabetes-rag-v1-inprocess"

    def __init__(self, *, source_id: str = "tfda+hpa", top_n: int | None = None, tool: Any | None = None) -> None:
        if tool is None:
            try:
                from rag_retrieval import EvidenceRetrievalTool
            except ImportError:
                import sys
                from pathlib import Path

                submodule_src = Path(__file__).resolve().parents[2] / "diabetes-rag" / "src"
                if submodule_src.is_dir() and str(submodule_src) not in sys.path:
                    sys.path.insert(0, str(submodule_src))
                try:
                    from rag_retrieval import EvidenceRetrievalTool
                except ImportError as exc:
                    raise RuntimeError(
                        "diabetes-rag is not installed; initialise submodules and install it with "
                        "'python -m pip install -e ./diabetes-rag'"
                    ) from exc
            kwargs: dict[str, Any] = {"source_id": source_id}
            if top_n is not None:
                kwargs["top_n"] = top_n
            tool = EvidenceRetrievalTool(**kwargs)
        self._tool = tool

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """Refuse the legacy call shape so the A guardrail cannot be skipped."""

        raise RuntimeError("DiabetesRAGRetriever requires retrieve_with_guardrail(AResult, QueryExpansionResult)")

    def retrieve_with_guardrail(self, a_result: AResult, expansion: QueryExpansionResult) -> RAGResult:
        if not a_result.rag_allowed:
            return self._error_result(a_result, expansion, "DIABETES_RAG_A_GUARDRAIL_DENIED")
        if a_result.request_id != expansion.request_id:
            return self._error_result(a_result, expansion, "DIABETES_RAG_REQUEST_ID_MISMATCH")

        payload = {
            "request_id": a_result.request_id,
            "schema_version": "rag-v1",
            "user_raw_input": a_result.user_raw_input,
            "retrieval_queries": list(expansion.retrieval_queries),
            "guardrail_result": {
                "intent_tags": [self._value(item) for item in a_result.intent_tags],
                "risk_flags": [self._value(item) for item in a_result.risk_flags],
                "context_modifiers": {
                    "time_frame": self._value(a_result.context_modifiers.time_frame),
                    "target_subject": self._value(a_result.context_modifiers.target_subject),
                    "polarity": self._value(a_result.context_modifiers.polarity),
                    "language": self._value(a_result.context_modifiers.language),
                },
                "router_status": self._value(a_result.router_status),
                "reason_codes": [self._value(item) for item in a_result.reason_codes],
            },
            "language": self._value(a_result.language),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        started = perf_counter()
        try:
            response = self._tool.retrieve(payload)
            return self._to_rag_result(response, a_result, expansion, (perf_counter() - started) * 1000)
        except Exception:
            # The dependency promises not to throw, but its import/object
            # boundary is still untrusted.  Do not expose exception details.
            return self._error_result(a_result, expansion, "DIABETES_RAG_DEPENDENCY_ERROR")

    @staticmethod
    def _value(value: Any) -> str:
        return str(getattr(value, "value", value))

    def _to_rag_result(
        self,
        response: Any,
        a_result: AResult,
        expansion: QueryExpansionResult,
        elapsed_ms: float,
    ) -> RAGResult:
        request_id = str(getattr(response, "request_id", ""))
        if request_id != a_result.request_id:
            return self._error_result(a_result, expansion, "DIABETES_RAG_RESPONSE_ID_MISMATCH", elapsed_ms)

        status = self._value(getattr(response, "retrieval_status", "ERROR"))
        route = self._value(getattr(response, "retrieval_route", ""))
        graph_path = self._value(getattr(response, "graph_path_status", ""))
        warnings = [self._value(getattr(item, "code", item)) for item in (getattr(response, "warnings", None) or [])]

        if status not in {"SUCCESS", "EMPTY", "PARTIAL", "STALE", "CONFLICT", "ERROR"}:
            return self._error_result(a_result, expansion, "DIABETES_RAG_INVALID_STATUS", elapsed_ms)
        if route not in {"VECTOR", "GRAPH", "HYBRID"}:
            route = None
        # The existing main envelope predates rag-v1's NOT_APPLICABLE state.
        # Omit it rather than pretending it means COMPLETE/PARTIAL.
        if graph_path not in {"COMPLETE", "PARTIAL"}:
            graph_path = None

        evidence = []
        try:
            for chunk in getattr(response, "chunks", None) or []:
                # rag-v1 uses Enum fields.  ``mode='json'`` converts those to
                # their wire values ("LOW", "graph", …), which is exactly
                # the input shape the main CanonicalEvidence adapter expects.
                dumped = chunk.model_dump(mode="json") if hasattr(chunk, "model_dump") else dict(chunk)
                evidence.append(normalize_evidence(dumped))
        except Exception:
            return self._error_result(a_result, expansion, "DIABETES_RAG_INVALID_CHUNK", elapsed_ms)

        # A malformed dependency response must not make ERROR look successful.
        if status in {"EMPTY", "ERROR"} and evidence:
            return self._error_result(a_result, expansion, "DIABETES_RAG_STATUS_CHUNK_MISMATCH", elapsed_ms)

        return RAGResult(
            request_id=a_result.request_id,
            original_query=a_result.user_raw_input,
            retrieval_queries=list(expansion.retrieval_queries),
            evidence=evidence,
            retrieval_latency_ms=elapsed_ms,
            retrieval_status=status,
            retrieval_route=route,
            graph_path_status=graph_path,
            rerun_suggested=bool(getattr(response, "rerun_suggested", False)),
            warnings=warnings,
        )

    @staticmethod
    def _error_result(
        a_result: AResult,
        expansion: QueryExpansionResult,
        warning: str,
        elapsed_ms: float | None = None,
    ) -> RAGResult:
        return RAGResult(
            request_id=a_result.request_id,
            original_query=a_result.user_raw_input,
            retrieval_queries=list(expansion.retrieval_queries),
            evidence=[],
            retrieval_latency_ms=elapsed_ms,
            retrieval_status="ERROR",
            warnings=[warning],
        )


__all__ = ["DiabetesRAGRetriever"]
