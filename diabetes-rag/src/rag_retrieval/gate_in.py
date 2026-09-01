"""入口閘門——RAG「自家大門」（CLAUDE.md §1）。即使 LLM 組的警衛室放行，
本模組仍會在任何檢索動作之前，獨立重新檢查 router_status。
"""

from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from .contract.enums import PERMITTED_ROUTER_STATUS, WarningCode
from .contract.errors import error_response, router_status_not_permitted
from .contract.models import RetrievalRequest, RetrievalResponse


def _extract_request_id(raw: object) -> Optional[str]:
    if isinstance(raw, dict):
        rid = raw.get("request_id")
        if isinstance(rid, str):
            return rid
    return None


def _is_unknown_enum_error(exc: ValidationError) -> bool:
    errors = exc.errors()
    return bool(errors) and all(e.get("type") == "enum" for e in errors)


def _summarize(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()))
        parts.append(f"{loc}: {e.get('msg')}")
    return "; ".join(parts)


def admit(raw: dict) -> tuple[Optional[RetrievalRequest], Optional[RetrievalResponse]]:
    """驗證並放行一筆原始請求 dict。

    成功回傳 `(request, None)`；請求被拒絕時回傳 `(None, error_response)`。
    絕不拋出例外——這裡的每一種失敗模式都對應到 CONTRACT_v1 §3 定義的
    合法 ERROR RetrievalResponse。
    """
    request_id = _extract_request_id(raw)

    try:
        request = RetrievalRequest.model_validate(raw)
    except ValidationError as exc:
        code = (
            WarningCode.UNKNOWN_ENUM_VALUE
            if _is_unknown_enum_error(exc)
            else WarningCode.SCHEMA_VALIDATION_FAILED
        )
        return None, error_response(request_id, code, _summarize(exc))

    if request.guardrail_result.router_status != PERMITTED_ROUTER_STATUS:
        return None, router_status_not_permitted(
            request.request_id, request.guardrail_result.router_status.value
        )

    return request, None
