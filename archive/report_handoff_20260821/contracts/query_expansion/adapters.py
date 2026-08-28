from __future__ import annotations

from typing import Any

from tfda_context_gate.a_router.schemas import AResult

from .schemas import QueryExpansionInput


def from_a_result(a_result: AResult | Any) -> QueryExpansionInput:
    """Build the expansion input without changing A's result contract."""

    if not isinstance(a_result, AResult):
        a_result = AResult.model_validate(a_result)
    return QueryExpansionInput(
        request_id=a_result.request_id,
        original_query=a_result.user_raw_input,
        router_status=a_result.router_status.value,
        intent_tags=[tag.value for tag in a_result.intent_tags],
        declared_role=a_result.declared_role.value,
        language=a_result.language.value,
    )

