"""Deterministic generators — 離線確定性夾具與臨床草稿生成器

本檔含：
- DeterministicFixtureCGenerator（E2E 契約驗證，不調 LLM）
- ClinicianDraftGenerator（詳細版 4 段結構，300-400 字）
- CLINICIAN_DISCLAIMER
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import re

from .c_workflow_input import CWorkflowInput
from .schemas import (
    ClinicianEvidenceDraft,
    ClinicianSourceRow,
    EvidenceAwareV2Answer,
    V2SupportedClaim,
    V2UnsupportedRequest,
)


CLINICIAN_DISCLAIMER = "本草稿僅供醫護人員參考，需經專業人員確認後使用，不得直接作為處方或診斷依據；最終臨床判斷由醫護人員負責。"

GROUNDED_PREFIX_TEMPLATES: list[str] = [
    "幫你整理了衛教重點（依 TFDA／國健署）：",
    "關於糖尿病成因，衛教文件提到幾個面向：",
]
GROUNDED_SUFFIX = "以上為衛教資訊，若有個人狀況請諮詢醫護人員。"
_SENT_SPLIT_RE = re.compile(r"[。；\n]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", re.UNICODE)


def _tokenize_for_overlap(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _select_sentences(content: str, query: str, max_sentences: int = 2) -> str:
    if not content or not content.strip():
        return ""
    parts = _SENT_SPLIT_RE.split(content)
    sentences = [s.strip() for s in parts if s.strip()]
    if not sentences:
        return content.strip()
    if len(sentences) <= max_sentences:
        if len(sentences) == 1:
            s = sentences[0]
            return s if s.endswith("。") else s + "。"
        query_tokens = _tokenize_for_overlap(query)
        if not query_tokens:
            result = "。".join(sentences)
            return result if result.endswith("。") else result + "。"
        scored: list[tuple[int, int, str]] = []
        for idx, sent in enumerate(sentences):
            overlap = len(_tokenize_for_overlap(sent) & query_tokens)
            scored.append((overlap, -idx, sent))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if scored[0][0] == 0:
            result = "。".join(sentences)
            return result if result.endswith("。") else result + "。"
        top = [s for _, _, s in scored[:max_sentences]]
        order = {s: i for i, s in enumerate(sentences)}
        top.sort(key=lambda s: order.get(s, 999))
        result = "。".join(top)
        return result if result.endswith("。") else result + "。"
    query_tokens = _tokenize_for_overlap(query)
    if not query_tokens:
        selected = sentences[:max_sentences]
        result = "。".join(selected)
        return result if result.endswith("。") else result + "。"
    scored = []
    for idx, sent in enumerate(sentences):
        overlap = len(_tokenize_for_overlap(sent) & query_tokens)
        scored.append((overlap, -idx, sent))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if scored[0][0] == 0:
        selected = sentences[:1]
        result = "。".join(selected)
        return result if result.endswith("。") else result + "。"
    top = [s for _, _, s in scored[:max_sentences]]
    if len(top) == 2:
        second_overlap = len(_tokenize_for_overlap(top[1]) & query_tokens)
        if second_overlap == 0 and len(_tokenize_for_overlap(top[0]) & query_tokens) > 0:
            top = top[:1]
    order = {s: i for i, s in enumerate(sentences)}
    top.sort(key=lambda s: order.get(s, 999))
    result = "。".join(top)
    return result if result.endswith("。") else result + "。"


def _pick_grounded_prefix(request_id: str, query: str) -> str:
    cause_keys = ["成因", "為什麼", "為何", "怎麼來的", "原因", "遺傳"]
    diet_keys = ["吃什麼", "飲食", "食物", "營養", "可以吃", "怎麼吃"]
    if any(k in query for k in cause_keys):
        return GROUNDED_PREFIX_TEMPLATES[1]
    if any(k in query for k in diet_keys):
        return GROUNDED_PREFIX_TEMPLATES[0]
    idx = (sum(ord(c) for c in request_id) + len(query)) % len(GROUNDED_PREFIX_TEMPLATES)
    return GROUNDED_PREFIX_TEMPLATES[idx % len(GROUNDED_PREFIX_TEMPLATES)]


class DeterministicFixtureCGenerator:
    """離線確定性 C v2 夾具（僅用於 E2E 契約驗證，不調 LLM）。

    與 LangChainCV2Generator 的區分：
    - 本類別：無需 chain、無網路呼叫，依 approved_evidence_ids 過濾 evidence 後
      直接組出 EvidenceAwareV2Answer；適合 CI / 契約測試。
    - LangChainCV2Generator：需注入已配置的 chain，實際呼叫 LLM 產生結構化輸出。

    decision 規則（夾具簡化版）：
    - 無可用 evidence → INSUFFICIENT
    - 有可用 evidence → ANSWER（每筆 evidence 各產一個 V2SupportedClaim）
    """

    name = "deterministic-c-v2-fixture"  # 識別名稱，供日誌與分流

    def __init__(self, *, max_evidence: int | None = None) -> None:
        """初始化夾具。

        參數 max_evidence：最多取用幾筆 evidence（用於測試截斷）；None 表示全取。
        """
        if max_evidence is not None and max_evidence < 1:  # 防呆：若有指定則必須 >=1
            raise ValueError("max_evidence must be >= 1 when provided")
        self.max_evidence = max_evidence  # 記錄截斷上限

    def generate(self, request: CWorkflowInput) -> EvidenceAwareV2Answer:
        """依正規輸入產生確定性 v2 回答（不過 LLM）。"""
        approved = set(request.approved_evidence_ids)  # 轉為集合以利快速過濾
        usable = [item for item in request.evidence if item.evidence_id in approved]  # 僅保留 B-approved 的 evidence
        if self.max_evidence is not None:  # 若有截斷上限則切片
            usable = usable[: self.max_evidence]
        if not usable:  # 無可用證據 → 回 INSUFFICIENT
            return EvidenceAwareV2Answer(
                decision="INSUFFICIENT",
                answer="這題我手上的衛教資料不夠，建議看診時問醫師。",
                supported_claims=[],
                unsupported_requests=[
                    V2UnsupportedRequest(
                        request=request.original_query,
                        reason="沒有可用的 B-approved evidence",
                    )
                ],
                limitations=["本次 workflow 使用的 evidence 不足。"],
            )

        claims = [
            V2SupportedClaim(
                claim_id=f"c{index}",
                claim=_select_sentences(item.content, request.original_query),
                evidence_ids=[item.evidence_id],
            )
            for index, item in enumerate(usable, 1)
        ]
        claims = [c for c in claims if c.claim.strip()]
        if not claims:
            claims = [
                V2SupportedClaim(
                    claim_id="c1",
                    claim=_select_sentences(usable[0].content, request.original_query) or usable[0].content[:80],
                    evidence_ids=[usable[0].evidence_id],
                )
            ]
        prefix = _pick_grounded_prefix(request.request_id, request.original_query)
        body = "".join(c.claim for c in claims)
        source_mark = "〔來源：" + "、".join(c.evidence_ids[0] for c in claims[:3]) + "〕" if claims else ""
        answer_text = f"{prefix}{body}\n\n{GROUNDED_SUFFIX}"
        if source_mark:
            answer_text = f"{answer_text}{source_mark}"
        return EvidenceAwareV2Answer(
            decision="ANSWER",
            answer=answer_text,
            supported_claims=claims,
            unsupported_requests=[],
            limitations=[],
        )

    def stream(self, request: CWorkflowInput, *, chunk_size: int = 20) -> Iterator[str]:
        result = self.generate(request)
        answer = result.answer
        for idx in range(0, len(answer), chunk_size):
            yield answer[idx : idx + chunk_size]
        self._last_streamed_result = result  # type: ignore[attr-defined]


class ClinicianDraftGenerator:
    """醫護證據草稿生成器（確定性夾具，不調 LLM）— 詳細版 4 段結構。

    與 DeterministicFixtureCGenerator 共用同一 B-approved 過濾邏輯，
    但輸出 ClinicianEvidenceDraft 詳細版：專業但易懂、含 4 段結構與來源表，待人工確認。
    詳細版 answer 為格式化文本（非 JSON only），含：
      一、基本資料（用藥/過敏/慢性/家族）
      二、時間軸（起始/描述/程度）
      三、安全訊號與限制（有限規則未命中不等於排除急症）
      四、待確認（藥袋提醒）
      另附來源對照表（5 列）與免責聲明；全文 300-400 字，不超過 800 字，禁止幻覺診斷。
    """

    name = "clinician-draft-fixture"

    def __init__(self, *, max_evidence: int | None = None) -> None:
        if max_evidence is not None and max_evidence < 1:
            raise ValueError("max_evidence must be >= 1 when provided")
        self.max_evidence = max_evidence
        self._last_streamed_result: ClinicianEvidenceDraft | None = None

    def stream(self, request: CWorkflowInput, *, chunk_size: int = 20) -> Iterator[str]:
        result = self.generate(request)
        self._last_streamed_result = result
        for idx in range(0, len(result.answer), chunk_size):
            yield result.answer[idx : idx + chunk_size]

    def _extract_intake_fields(self, intake: Any | None) -> dict[str, str]:
        if intake is None:
            return {}
        try:
            if isinstance(intake, dict):
                return {
                    "known_medications": ", ".join(intake.get("known_medications", []) or []) or "未提供",
                    "allergies": ", ".join(intake.get("allergies", []) or []) or "未提供",
                    "chronic_conditions": ", ".join(intake.get("chronic_conditions", []) or []) or "未提供",
                    "family_history": ", ".join(intake.get("family_history", []) or []) or "未提供",
                    "symptom_onset": intake.get("symptom_onset") or "未提供",
                    "symptom_description": intake.get("symptom_description") or "未提供",
                    "symptom_severity": intake.get("symptom_severity") or "未提供",
                    "questions_for_doctor": "；".join(intake.get("questions_for_doctor", []) or []) or "未提供",
                }
            # PreVisitIntake object
            return {
                "known_medications": ", ".join(getattr(intake, "known_medications", []) or []) or "未提供",
                "allergies": ", ".join(getattr(intake, "allergies", []) or []) or "未提供",
                "chronic_conditions": ", ".join(getattr(intake, "chronic_conditions", []) or []) or "未提供",
                "family_history": ", ".join(getattr(intake, "family_history", []) or []) or "未提供",
                "symptom_onset": getattr(intake, "symptom_onset", None) or "未提供",
                "symptom_description": getattr(intake, "symptom_description", None) or "未提供",
                "symptom_severity": getattr(intake, "symptom_severity", None) or "未提供",
                "questions_for_doctor": "；".join(getattr(intake, "questions_for_doctor", []) or []) or "未提供",
            }
        except Exception:
            return {}

    def _build_detailed_answer(
        self,
        *,
        intake_fields: dict[str, str],
        usable: list[Any],
        claims: list[V2SupportedClaim],
        conflicts: list[str],
        source_table: list[ClinicianSourceRow],
    ) -> str:
        meds = intake_fields.get("known_medications", "未提供")
        allergies = intake_fields.get("allergies", "未提供")
        chronic = intake_fields.get("chronic_conditions", "未提供")
        family = intake_fields.get("family_history", "未提供")
        onset = intake_fields.get("symptom_onset", "未提供")
        desc = intake_fields.get("symptom_description", "未提供")
        severity = intake_fields.get("symptom_severity", "未提供")
        questions = intake_fields.get("questions_for_doctor", "未提供")

        has_unknown = "待確認" in meds
        unknown_note = "（含待確認藥品，需核對藥袋）" if has_unknown else ""

        # Evidence summary text for context
        evidence_text = "；".join(f"{c.claim}（{c.evidence_ids[0]}）" for c in claims[:2]) if claims else "無直接證據支持的特定主張，僅整理 intake 事實"

        # Build 4 sections — aim 300-400 chars total, professional but understandable, concise
        sections: list[str] = []
        sections.append("【臨床證據草稿｜待醫護確認】")
        sections.append(
            f"一、基本資料：用藥：{meds}{unknown_note}；過敏：{allergies}；慢性病：{chronic}；家族史：{family}。"
        )
        sections.append(
            f"二、時間軸：{onset}：{desc}（{severity}）。"
        )
        sections.append(
            "三、安全訊號限制：依目前使用者提供的文字，未偵測到系統已定義的紅旗關鍵訊號；此結果不代表已排除急症或其他併發症，仍需由醫護人員評估。"
        )
        pending_items: list[str] = []
        if has_unknown:
            pending_items.append(f"待確認：{meds}")
        if questions != "未提供":
            pending_items.append(f"問：{questions}")
        pending_str = "；".join(pending_items) if pending_items else "無"
        sections.append(
            f"四、待確認：{pending_str}。請攜帶藥袋及紀錄至門診。摘要：{evidence_text}。"
        )
        if conflicts:
            sections.append(f"【衝突】{conflicts[0]}")
        # Source table as formatted text (5 columns) — concise, D will also append but keep answer concise
        table_header = "【來源對照表】"
        table_rows = []
        for row in source_table[:2]:
            score_str = f"{row.score:.2f}" if row.score is not None else ""
            table_rows.append(f"{row.evidence_id} | {row.source or ''} | {row.date or ''} | {row.version or ''} | {score_str}")
        table_text = table_header + "\n" + "\n".join(table_rows) if table_rows else table_header + "\n（無）"
        sections.append(table_text)
        sections.append(f"【免責聲明】{CLINICIAN_DISCLAIMER}")

        answer = "\n\n".join(sections)
        # Ensure 300-400 chars: if too short, add context; if too long, truncate gracefully (but keep disclaimer)
        if len(answer) < 300:
            answer += "\n\n【補充】本草稿基於 B-approved 證據與 intake 事實，僅供醫護參考；細節見來源表，判斷需結合完整病歷。"
        if len(answer) > 800:
            disclaimer_part = f"【免責聲明】{CLINICIAN_DISCLAIMER}"
            answer = answer[:600] + "\n\n" + disclaimer_part
        return answer

    def generate(self, request: CWorkflowInput) -> ClinicianEvidenceDraft:
        approved = set(request.approved_evidence_ids)
        usable = [item for item in request.evidence if item.evidence_id in approved]
        if self.max_evidence is not None:
            usable = usable[: self.max_evidence]
        # Extract intake if available (for detailed 4 sections)
        intake_fields = self._extract_intake_fields(getattr(request, "intake", None))
        if not usable:
            # Even without evidence, if intake exists we can still produce a draft with intake facts (but D requires source_table)
            # For INSUFFICIENT, keep source_table empty as per D gate, but provide detailed intake-based answer
            if intake_fields:
                # Produce INSUFFICIENT with intake context but no source_table (D will require source_table for CLINICIAN_DRAFT, but INSUFFICIENT allows empty)
                answer = (
                    "【臨床證據草稿｜待醫護確認】\n\n"
                    f"一、基本資料：已知用藥：{intake_fields.get('known_medications', '未提供')}；過敏史：{intake_fields.get('allergies', '未提供')}；"
                    f"慢性病史：{intake_fields.get('chronic_conditions', '未提供')}；家族史：{intake_fields.get('family_history', '未提供')}。\n\n"
                    f"二、時間軸：起始：{intake_fields.get('symptom_onset', '未提供')}；描述：{intake_fields.get('symptom_description', '未提供')}；"
                    f"程度：{intake_fields.get('symptom_severity', '未提供')}。\n\n"
                    "三、安全訊號限制：依目前文字未偵測到系統已定義的紅旗；不代表已排除急症或其他併發症，仍需醫護評估。\n\n"
                    f"四、待確認：請攜帶藥袋核對；想問醫師：{intake_fields.get('questions_for_doctor', '未提供')}。\n\n"
                    "【限制】本次無 B-approved 證據，無法提供證據支持的專業摘要，僅整理 intake 事實。\n\n"
                    f"【免責聲明】{CLINICIAN_DISCLAIMER}"
                )
                return ClinicianEvidenceDraft(
                    request_id=request.request_id,
                    decision="INSUFFICIENT",
                    answer=answer,
                    evidence_summary=[],
                    conflicts=[],
                    limitations=["本次 workflow 使用的 evidence 不足，無法形成草稿；僅整理 intake 事實。"],
                    source_table=[],
                    disclaimer=CLINICIAN_DISCLAIMER,
                )
            return ClinicianEvidenceDraft(
                request_id=request.request_id,
                decision="INSUFFICIENT",
                answer="目前提供的資料不足以形成臨床草稿。",
                evidence_summary=[],
                conflicts=[],
                limitations=["本次 workflow 使用的 evidence 不足，無法形成草稿。"],
                source_table=[],
                disclaimer=CLINICIAN_DISCLAIMER,
            )
        # Limit evidence_summary to 3, source_table to 5 (detailed version)
        claims = [
            V2SupportedClaim(
                claim_id=f"c{index}",
                claim=item.content,
                evidence_ids=[item.evidence_id],
            )
            for index, item in enumerate(usable[:3], 1)
        ]
        # Source table: up to 2 rows, preserve order (P4 slimming)
        source_table = [
            ClinicianSourceRow(
                evidence_id=item.evidence_id,
                source=item.source,
                date=item.date,
                version=item.version,
                score=item.score,
            )
            for item in usable[:2]
        ]
        conflicts: list[str] = []
        if len(usable) > 1:
            dates = {item.date for item in usable if item.date}
            if len(dates) > 1:
                conflicts.append(f"證據間發布日期不一致：{', '.join(sorted(dates))}，需留意時效與適用範圍。")
        # Check for content conflicts (simple heuristic: if evidence contents differ significantly)
        if len(usable) > 1 and len({item.content[:20] for item in usable}) > 1:
            # Only add if not already added date conflict
            if not conflicts:
                conflicts.append("證據間內容存在差異，需由醫護人員綜合判斷適用性。")
        limitations: list[str] = []
        if len(usable) < len(approved):
            limitations.append("部分 B-approved 證據因內容缺失未納入草稿。")
        # Build detailed answer with 4 sections
        answer = self._build_detailed_answer(
            intake_fields=intake_fields if intake_fields else {
                "known_medications": "未提供",
                "allergies": "未提供",
                "chronic_conditions": "未提供",
                "family_history": "未提供",
                "symptom_onset": "未提供",
                "symptom_description": "未提供",
                "symptom_severity": "未提供",
                "questions_for_doctor": "未提供",
            },
            usable=usable,
            claims=claims,
            conflicts=conflicts,
            source_table=source_table,
        )
        return ClinicianEvidenceDraft(
            request_id=request.request_id,
            decision="CLINICIAN_DRAFT",
            answer=answer,
            evidence_summary=claims,
            conflicts=conflicts,
            limitations=limitations,
            source_table=source_table,
            disclaimer=CLINICIAN_DISCLAIMER,
        )
