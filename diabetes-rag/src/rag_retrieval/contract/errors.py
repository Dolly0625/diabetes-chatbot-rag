"""建立合法 ERROR RetrievalResponse 的輔助函式。依 CONTRACT_v1 §3，RAG 絕不
向呼叫端拋出例外——任何失敗都轉成這裡的其中一種回應。
"""

from __future__ import annotations

from typing import Optional

from .enums import EvidenceRiskLevel, GraphPathStatus, RetrievalRoute, RetrievalStatus
from .models import RetrievalResponse, Warning
from .enums import WarningCode

UNKNOWN_REQUEST_ID = "unknown"


def error_response(
    request_id: Optional[str],
    code: WarningCode,
    detail: Optional[str] = None,
    route: RetrievalRoute = RetrievalRoute.VECTOR,
) -> RetrievalResponse:
    """建立 CONTRACT_v1 §3 要求的 ERROR 回應，用於被拒絕或失敗的請求。
    `chunks` 恆為 `[]`，不得為 null。"""
    return RetrievalResponse(
        request_id=request_id or UNKNOWN_REQUEST_ID,
        retrieval_route=route,
        retrieval_status=RetrievalStatus.ERROR,
        graph_path_status=GraphPathStatus.NOT_APPLICABLE,
        rerun_suggested=False,
        max_evidence_risk_level=EvidenceRiskLevel.UNKNOWN,
        warnings=[Warning(code=code, detail=detail)] if detail else [Warning(code=code)],
        chunks=[],
    )


def schema_validation_failed(request_id: Optional[str], detail: str) -> RetrievalResponse:
    return error_response(request_id, WarningCode.SCHEMA_VALIDATION_FAILED, detail)


def router_status_not_permitted(request_id: str, router_status: str) -> RetrievalResponse:
    return error_response(
        request_id,
        WarningCode.ROUTER_STATUS_NOT_PERMITTED,
        f"router_status={router_status!r}；僅 G_GENERAL_EDUCATION 可進入檢索。"
        "RAG 端未執行任何檢索。",
    )


def empty_response(
    request_id: str,
    route: RetrievalRoute,
    detail: Optional[str] = None,
) -> RetrievalResponse:
    """合法、非錯誤的「查無資料」回應。對應 §2.1 的 EMPTY。"""
    warnings = (
        [Warning(code=WarningCode.EMPTY_AFTER_THRESHOLD_FILTER, detail=detail)]
        if detail
        else []
    )
    return RetrievalResponse(
        request_id=request_id,
        retrieval_route=route,
        retrieval_status=RetrievalStatus.EMPTY,
        graph_path_status=GraphPathStatus.NOT_APPLICABLE,
        rerun_suggested=True,
        max_evidence_risk_level=EvidenceRiskLevel.UNKNOWN,
        warnings=warnings,
        chunks=[],
    )
