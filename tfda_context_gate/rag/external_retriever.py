"""Production adapter for the cross-team RetrievalRequest/Response contract."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from tfda_context_gate.a_router.schemas import AResult
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .external_contract import (
    RetrievalRequest,
    retrieval_request_from_results,
    retrieval_response_to_rag_result,
)
from .schemas import RAGResult


ExternalRetrievalTransport = Callable[[RetrievalRequest], Mapping[str, Any]]
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class ExternalContractRetriever:
    """Call an external RAG service without weakening the internal B boundary.

    The workflow supplies both the A decision and query expansion through
    ``retrieve_with_guardrail``. Transport or schema failures become an
    explicit ``ERROR`` RAG result, which Context Gate B maps to FALLBACK.
    """

    name = "external-retrieval-contract-v0.1"

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float = 3.0,
        transport: ExternalRetrievalTransport | None = None,
    ) -> None:
        endpoint = endpoint.strip()
        parsed = urllib.parse.urlparse(endpoint)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not local_http:
            raise ValueError("external RAG endpoint must use HTTPS (HTTP allowed only for localhost)")
        if not parsed.netloc:
            raise ValueError("external RAG endpoint must include a host")
        self.endpoint = endpoint
        self.timeout_s = max(0.1, float(timeout_s))
        self._transport = transport or self._post_json

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """Fail closed if code tries to bypass the A guardrail input."""

        raise RuntimeError("ExternalContractRetriever requires retrieve_with_guardrail(AResult, QueryExpansionResult)")

    def retrieve_with_guardrail(
        self,
        a_result: AResult,
        expansion: QueryExpansionResult,
    ) -> RAGResult:
        request = retrieval_request_from_results(a_result, expansion)
        try:
            payload = self._transport(request)
            return retrieval_response_to_rag_result(payload, request=request)
        except Exception as exc:
            # Do not leak response bodies or credentials into trace/warnings.
            return RAGResult(
                request_id=request.request_id,
                original_query=request.user_raw_input,
                retrieval_queries=request.retrieval_queries,
                evidence=[],
                retrieval_status="ERROR",
                warnings=[f"EXTERNAL_RETRIEVAL_{type(exc).__name__.upper()}"],
            )

    def _post_json(self, request: RetrievalRequest) -> Mapping[str, Any]:
        body = request.model_dump_json().encode("utf-8")
        http_request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("external RAG response exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("external RAG response must be a JSON object")
        return payload


__all__ = ["ExternalContractRetriever", "ExternalRetrievalTransport"]
