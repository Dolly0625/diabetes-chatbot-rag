"""LangChain adapter — 線上 LLM 轉接器

本檔僅含 LangChainCV2Generator，需外部注入已配置的 chain。
與 deterministic_generators 嚴格分離，避免循環依賴。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Union

from .c_workflow_input import CWorkflowInput, to_legacy_v2_case
from .schemas import ClinicianEvidenceDraft, EvidenceAwareV2Answer
from .system_prompts import CLINICIAN_DRAFT_SYSTEM, EVIDENCE_AWARE_V2_SYSTEM
from .user_prompts import clinician_draft_user_prompt, evidence_aware_v2_user_prompt

def _ensure_grounded_prefix_answer(
    result: EvidenceAwareV2Answer | ClinicianEvidenceDraft,
    *,
    request_id: str,
    query: str,
) -> EvidenceAwareV2Answer | ClinicianEvidenceDraft:
    try:
        from .deterministic_generators import (
            GROUNDED_PREFIX_TEMPLATES,
            _pick_grounded_prefix,
        )
    except Exception:
        return result
    if isinstance(result, ClinicianEvidenceDraft):
        return result
    decision = getattr(result, "decision", None)
    if decision == "INSUFFICIENT":
        ans = getattr(result, "answer", "") or ""
        if "根據提供的資料" in ans:
            cleaned = ans.replace("根據提供的資料：", "").replace("根據提供的資料:", "").replace("根據提供的資料", "").lstrip(" ：: \n")
            try:
                return result.model_copy(update={"answer": cleaned})  # type: ignore[attr-defined]
            except Exception:
                result.answer = cleaned  # type: ignore[attr-defined]
        return result
    ans = getattr(result, "answer", "") or ""
    if not ans:
        return result
    if any(ans.startswith(p) for p in GROUNDED_PREFIX_TEMPLATES):
        if "根據提供的資料" in ans[:20]:
            ans = ans.replace("根據提供的資料：", "", 1).replace("根據提供的資料:", "", 1).replace("根據提供的資料", "", 1).lstrip(" ：: \n")
            if not any(ans.startswith(p) for p in GROUNDED_PREFIX_TEMPLATES):
                prefix = _pick_grounded_prefix(request_id, query)
                ans = prefix + ans
            try:
                return result.model_copy(update={"answer": ans})  # type: ignore[attr-defined]
            except Exception:
                result.answer = ans  # type: ignore[attr-defined]
        return result
    if "根據提供的資料" in ans:
        cleaned = ans.replace("根據提供的資料：", "").replace("根據提供的資料:", "").replace("根據提供的資料", "")
        cleaned = cleaned.lstrip(" ：: \n。")
        prefix = _pick_grounded_prefix(request_id, query)
        new_ans = prefix + cleaned
        try:
            return result.model_copy(update={"answer": new_ans})  # type: ignore[attr-defined]
        except Exception:
            result.answer = new_ans  # type: ignore[attr-defined]
        return result
    prefix = _pick_grounded_prefix(request_id, query)
    new_ans = prefix + ans
    try:
        return result.model_copy(update={"answer": new_ans})  # type: ignore[attr-defined]
    except Exception:
        result.answer = new_ans  # type: ignore[attr-defined]
    return result


class LangChainCV2Generator:
    """已配置 C v2 structured-output chain 的轉接器（線上 LLM 版）。

    與 DeterministicFixtureCGenerator 的區分：
    - 本類別：需外部注入 chain，實際呼叫 LLM；workflow 不自行建模。
    - 夾具版：不需 chain，本地確定性回覆。

    設計原則：workflow 永不隱式建模；呼叫方注入既有 chain，保留 C v2 的
    prompt 與結構化輸出契約。
    """

    name = "langchain-c-v2-adapter"  # 識別名稱

    def __init__(self, chain: Any, *, role: str | None = None, llm: Any | None = None) -> None:
        """初始化轉接器。

        參數 chain：已配置好的 C v2 structured-output chain（由呼叫方提供）。
        參數 role：宣告角色，若為 HEALTHCARE_PROFESSIONAL 則使用臨床草稿提示。
        參數 llm：底層 ChatOpenAI 實例（可選），用於 stream 時直接呼叫 llm.stream
        """
        self.chain = chain
        self.role = role
        self.llm = llm
        self._last_streamed_result: Union[EvidenceAwareV2Answer, ClinicianEvidenceDraft] | None = None

    def _build_messages(self, request: CWorkflowInput) -> list[Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        if self.role == "HEALTHCARE_PROFESSIONAL":
            return [
                SystemMessage(content=CLINICIAN_DRAFT_SYSTEM),
                HumanMessage(content=clinician_draft_user_prompt(to_legacy_v2_case(request))),
            ]
        return [
            SystemMessage(content=EVIDENCE_AWARE_V2_SYSTEM),
            HumanMessage(content=evidence_aware_v2_user_prompt(to_legacy_v2_case(request))),
        ]

    def _chunk_answer(self, answer: str, chunk_size: int) -> Iterator[str]:
        for idx in range(0, len(answer), chunk_size):
            yield answer[idx : idx + chunk_size]

    def stream(self, request: CWorkflowInput, *, chunk_size: int = 20) -> Iterator[str]:
        messages = self._build_messages(request)
        full_text = ""
        parsed_result: Union[EvidenceAwareV2Answer, ClinicianEvidenceDraft] | None = None
        streamed_via_llm = False
        if self.llm is not None and hasattr(self.llm, "stream"):
            try:
                chunks: list[str] = []
                for chunk in self.llm.stream(messages):  # type: ignore[union-attr]
                    content = getattr(chunk, "content", None)
                    if content:
                        if isinstance(content, list):
                            text = "".join(
                                str(b.get("text", b.get("content", "")) if isinstance(b, dict) else str(b))
                                for b in content
                            )
                        else:
                            text = str(content)
                        chunks.append(text)
                        full_text += text
                if full_text:
                    streamed_via_llm = True
                    import json as _json

                    try:
                        data = _json.loads(full_text)
                        if self.role == "HEALTHCARE_PROFESSIONAL" and data.get("decision") == "CLINICIAN_DRAFT":
                            parsed_result = ClinicianEvidenceDraft.model_validate(data)
                        else:
                            parsed_result = EvidenceAwareV2Answer.model_validate(data)
                    except Exception:
                        try:
                            start = full_text.find("{")
                            end = full_text.rfind("}")
                            if start != -1 and end != -1 and end > start:
                                data = _json.loads(full_text[start : end + 1])
                                if self.role == "HEALTHCARE_PROFESSIONAL" and data.get("decision") == "CLINICIAN_DRAFT":
                                    parsed_result = ClinicianEvidenceDraft.model_validate(data)
                                else:
                                    parsed_result = EvidenceAwareV2Answer.model_validate(data)
                        except Exception:
                            parsed_result = None
            except Exception:
                streamed_via_llm = False
                full_text = ""
        if not streamed_via_llm or parsed_result is None:
            try:
                if hasattr(self.chain, "stream"):
                    accumulated: list[Any] = []
                    raw_buffer = ""
                    for chunk in self.chain.stream(messages):  # type: ignore[union-attr]
                        accumulated.append(chunk)
                        if isinstance(chunk, dict) and chunk.get("parsed") is not None:
                            parsed_result = chunk["parsed"]
                            if hasattr(parsed_result, "answer"):
                                raw_buffer = str(parsed_result.answer)
                        elif hasattr(chunk, "content") and chunk.content:
                            raw_buffer += str(chunk.content)
                        elif isinstance(chunk, str):
                            raw_buffer += chunk
                    if parsed_result is None and accumulated:
                        last = accumulated[-1]
                        if isinstance(last, dict) and last.get("parsed") is not None:
                            parsed_result = last["parsed"]
                        elif hasattr(last, "model_dump"):
                            parsed_result = last
                    if parsed_result is not None and hasattr(parsed_result, "model_validate"):
                        try:
                            if isinstance(parsed_result, dict):
                                if self.role == "HEALTHCARE_PROFESSIONAL" and parsed_result.get("decision") == "CLINICIAN_DRAFT":
                                    parsed_result = ClinicianEvidenceDraft.model_validate(parsed_result)
                                else:
                                    parsed_result = EvidenceAwareV2Answer.model_validate(parsed_result)
                        except Exception:
                            pass
                    if parsed_result is None:
                        parsed_result = self.generate(request)
                else:
                    parsed_result = self.generate(request)
            except Exception:
                parsed_result = self.generate(request)
        if parsed_result is None:
            parsed_result = self.generate(request)
        if isinstance(parsed_result, dict):
            if self.role == "HEALTHCARE_PROFESSIONAL" and parsed_result.get("decision") == "CLINICIAN_DRAFT":
                parsed_result = ClinicianEvidenceDraft.model_validate(parsed_result)
            else:
                parsed_result = EvidenceAwareV2Answer.model_validate(parsed_result)
        if parsed_result is not None:
            parsed_result = _ensure_grounded_prefix_answer(parsed_result, request_id=request.request_id, query=request.original_query)  # type: ignore[arg-type]
        self._last_streamed_result = parsed_result
        answer = getattr(parsed_result, "answer", "") if parsed_result is not None else ""
        yield from self._chunk_answer(str(answer), chunk_size)

    def generate(self, request: CWorkflowInput) -> Union[EvidenceAwareV2Answer, ClinicianEvidenceDraft]:
        from langchain_core.messages import HumanMessage, SystemMessage

        if self.role == "HEALTHCARE_PROFESSIONAL":
            response = self.chain.invoke(
                [
                    SystemMessage(content=CLINICIAN_DRAFT_SYSTEM),
                    HumanMessage(content=clinician_draft_user_prompt(to_legacy_v2_case(request))),
                ]
            )
            parsed = response.get("parsed") if isinstance(response, dict) else response
            if parsed is None:
                raise ValueError("C clinician draft structured output did not contain parsed data")
            if isinstance(parsed, dict) and parsed.get("decision") == "CLINICIAN_DRAFT":
                validated = ClinicianEvidenceDraft.model_validate(parsed)
                return _ensure_grounded_prefix_answer(validated, request_id=request.request_id, query=request.original_query)
            validated = EvidenceAwareV2Answer.model_validate(parsed)
            return _ensure_grounded_prefix_answer(validated, request_id=request.request_id, query=request.original_query)  # type: ignore[return-value]
        response = self.chain.invoke(
            [
                SystemMessage(content=EVIDENCE_AWARE_V2_SYSTEM),
                HumanMessage(content=evidence_aware_v2_user_prompt(to_legacy_v2_case(request))),
            ]
        )
        parsed = response.get("parsed") if isinstance(response, dict) else response
        if parsed is None:
            raise ValueError("C v2 structured output did not contain parsed data")
        validated = EvidenceAwareV2Answer.model_validate(parsed)
        return _ensure_grounded_prefix_answer(validated, request_id=request.request_id, query=request.original_query)  # type: ignore[return-value]
