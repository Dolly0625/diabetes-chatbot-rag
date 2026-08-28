from __future__ import annotations

from typing import Protocol

from .schemas import QueryExpansionInput, QueryExpansionResult


class QueryExpander(Protocol):
    def expand(self, request: QueryExpansionInput) -> QueryExpansionResult:
        ...


class IdentityQueryExpander:
    """Safe v0.1 expansion: preserve the user's query exactly."""

    name = "identity-deterministic"

    def expand(self, request: QueryExpansionInput) -> QueryExpansionResult:
        return QueryExpansionResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=[request.original_query],
            strategy=self.name,
        )

