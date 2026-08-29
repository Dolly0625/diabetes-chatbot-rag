from __future__ import annotations

from typing import Literal, Protocol

from .schemas import CanonicalBInput, CanonicalBResult


class ContextGate(Protocol):
    """B 審查閘門的協定介面：任何實作只需提供 evaluate 方法即可替換。"""

    def evaluate(self, request: CanonicalBInput) -> CanonicalBResult:
        """輸入 CanonicalBInput，回傳 CanonicalBResult 審查結果。"""
        ...


class DeterministicContextGate:
    """離線確定性 B 邊界，給確定性基線流程的展示用閘門。

    這是工作流程的轉接器／展示用閘門，並非取代既有的 B LLM 上下文裁判。
    雙模式說明：
      - fixture 模式（預設）：僅當證據的 ``metadata['fixture_b_approved'] == True``
        才視為核准；沒有此標記的證據不會自動放行，閘門採「預設拒絕」原則，
        避免檢索到的上下文悄悄變成 B 證據。
      - all_retrieved 模式：所有傳入證據一律視為核准，僅供 demo 展示。
    """

    name = "deterministic-context-gate-fixture"  # 預設名稱，對應 fixture 模式

    def __init__(self, *, approval_mode: Literal["fixture", "all_retrieved"] = "fixture") -> None:
        """初始化閘門。

        參數:
            approval_mode: "fixture" 僅核准標記為 fixture_b_approved 的證據；
                           "all_retrieved" 則全部核准（demo 用）。
        """
        if approval_mode not in {"fixture", "all_retrieved"}:
            raise ValueError("approval_mode must be 'fixture' or 'all_retrieved'")
        self.approval_mode = approval_mode  # 記錄當前核准模式
        if approval_mode == "all_retrieved":
            self.name = "deterministic-context-gate-demo-all-retrieved"  # demo 模式改名以利識別

    def evaluate(self, request: CanonicalBInput) -> CanonicalBResult:
        """執行 B 審查，依序檢查三個分支，回傳對應的 CanonicalBResult。

        分支 1：無證據 → INSUFFICIENT
        分支 2：重複 evidence_id → UNSAFE
        分支 3：依 approval_mode 篩選核准清單，無核准則 INSUFFICIENT，否則 PASS
        """
        retrieval_status = str(request.tool_context.get("retrieval_status") or "").upper()
        warnings = [str(value).upper() for value in request.tool_context.get("warnings", [])]

        # External envelope is advisory data from an untrusted dependency;
        # fail closed before considering individual chunks.  PARTIAL remains
        # eligible for the normal sufficiency check below.
        if any("PROMPT_INJECTION" in warning for warning in warnings):
            return CanonicalBResult(
                request_id=request.request_id,
                decision="UNSAFE",
                approved_evidence_ids=[],
                evidence=request.evidence,
                reason_codes=["RETRIEVAL_PROMPT_INJECTION_WARNING"],
                retrieval_feedback={"retrieval_queries": request.retrieval_queries, "warnings": warnings},
                relevance="UNKNOWN",
                sufficiency="UNSAFE",
                safety="FAIL",
            )
        if retrieval_status in {"STALE", "CONFLICT"}:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="REVIEW",
                approved_evidence_ids=[],
                evidence=request.evidence,
                reason_codes=[f"RETRIEVAL_{retrieval_status}"],
                retrieval_feedback={"retrieval_queries": request.retrieval_queries, "retrieval_status": retrieval_status},
                relevance="UNKNOWN",
                sufficiency="REVIEW",
                conflict="CONFLICT" if retrieval_status == "CONFLICT" else None,
                safety="NOT_ASSESSED",
            )
        if retrieval_status == "ERROR":
            return CanonicalBResult(
                request_id=request.request_id,
                decision="FALLBACK",
                approved_evidence_ids=[],
                evidence=[],
                reason_codes=["RETRIEVAL_ERROR"],
                retrieval_feedback={"retrieval_queries": request.retrieval_queries, "retrieval_status": retrieval_status},
                relevance="NONE",
                sufficiency="INSUFFICIENT",
                safety="NOT_ASSESSED",
            )

        # ── 分支 1：完全沒有證據，直接判 INSUFFICIENT ──
        if not request.evidence:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="INSUFFICIENT",  # 證據不足
                approved_evidence_ids=[],
                evidence=[],
                reason_codes=[
                    "CONTEXT_INSUFFICIENT",
                    "RETRIEVAL_EMPTY" if retrieval_status == "EMPTY" else "NO_RETRIEVED_EVIDENCE",
                ],
                retrieval_feedback={
                    "retrieval_queries": request.retrieval_queries,
                    **({"retrieval_status": retrieval_status} if retrieval_status else {}),
                },
                relevance="NONE",  # 無相關性可言
                sufficiency="INSUFFICIENT",
                safety="NOT_ASSESSED",  # 尚未進入安全評估
            )

        # ── 分支 2：檢查是否有重複的 evidence_id，有則判 UNSAFE ──
        seen: set[str] = set()  # 已看過的 ID 集合
        duplicate_ids: list[str] = []  # 重複的 ID 列表
        for evidence in request.evidence:
            if evidence.evidence_id in seen:
                duplicate_ids.append(evidence.evidence_id)
            seen.add(evidence.evidence_id)
        if duplicate_ids:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="UNSAFE",  # 重複 ID 視為不安全
                approved_evidence_ids=[],
                evidence=request.evidence,
                reason_codes=["DUPLICATE_EVIDENCE_ID"],
                retrieval_feedback={"duplicate_ids": duplicate_ids},
                relevance="UNKNOWN",
                sufficiency="UNSAFE",
                safety="FAIL",
            )

        # ── 分支 3：依 approval_mode 篩選核准清單 ──
        #   fixture 模式：僅 metadata fixture_b_approved == True 的才核准
        #   all_retrieved 模式：全部核准
        approved = [
            evidence.evidence_id
            for evidence in request.evidence
            if self.approval_mode == "all_retrieved"
            or evidence.metadata.get("fixture_b_approved") is True
        ]
        if not approved:
            # 沒有任何一筆被核准 → INSUFFICIENT（預設拒絕，避免未審核上下文外洩）
            return CanonicalBResult(
                request_id=request.request_id,
                decision="INSUFFICIENT",
                approved_evidence_ids=[],
                evidence=request.evidence,
                reason_codes=["CONTEXT_INSUFFICIENT", "NO_APPROVED_EVIDENCE"],
                retrieval_feedback={"retrieval_queries": request.retrieval_queries},
                relevance="UNKNOWN",
                sufficiency="INSUFFICIENT",
                safety="NOT_ASSESSED",
            )
        # 有核准證據 → PASS
        return CanonicalBResult(
            request_id=request.request_id,
            decision="PASS",
            approved_evidence_ids=approved,
            evidence=request.evidence,
            reason_codes=[
                "B_CONTEXT_CONTRACT_VALID",
                "DEMO_RETRIEVED_EVIDENCE_APPROVED"
                if self.approval_mode == "all_retrieved"
                else "EVIDENCE_APPROVED",
            ],
            retrieval_feedback={"retrieval_queries": request.retrieval_queries},
            relevance="RETRIEVED",
            sufficiency="SUFFICIENT",
            safety="DEMO_RETRIEVED_APPROVED" if self.approval_mode == "all_retrieved" else "FIXTURE_APPROVED",
        )
