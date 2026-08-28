from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any

from tfda_context_gate.access_control import AuthorizationStatus
from tfda_context_gate.clinical_safety import SystemRiskClassification

from .schemas import (
    ClinicalConversationState,
    CompactionDecision,
    CompactionPolicy,
    ConversationContext,
    ConversationStage,
    ConversationTurn,
    FactRevision,
)


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_EDITABLE_FIELDS = {
    "known_medications",
    "allergies",
    "chronic_conditions",
    "family_history",
    "symptom_onset",
    "symptom_description",
    "reported_severity",
    "questions_for_doctor",
    "pending_question",
    "current_stage",
    "authorization_status",
    "system_risk_classification",
}
_MONOTONIC_LIST_FIELDS = {"risk_flags", "negated_red_flags", "completed_stages"}


def estimate_tokens(text: str) -> int:
    """保守估算中英混合文字 token 數，不依賴特定 provider tokenizer。

    CJK 字元各計一 token；其餘非空白字元每四個估一 token。正式模型若提供
    tokenizer，呼叫端可直接把精確值傳給 ``evaluate`` 覆蓋本估算。
    """

    value = str(text)
    cjk_count = len(_CJK.findall(value))
    non_cjk = _CJK.sub("", value)
    non_space_count = len(re.sub(r"\s+", "", non_cjk))
    return cjk_count + math.ceil(non_space_count / 4)


def _value_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _turn_hash(turn: ConversationTurn) -> str:
    return _value_hash(turn.model_dump(mode="json"))


def _dedupe(values: list[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for item in values:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


class ConversationContextManager:
    """管理不可壓縮臨床事實與有界近期對話。

    所有方法回傳 deep copy，避免共享 mutable state。這個服務不執行醫療抽取、
    不呼叫 LLM，也不改變 A/B/C/D 決策。
    """

    def __init__(self, policy: CompactionPolicy | None = None) -> None:
        self.policy = policy or CompactionPolicy()

    def create(self, session_id: str, *, original_query: str | None = None) -> ConversationContext:
        return ConversationContext(
            session_id=session_id,
            clinical_state=ClinicalConversationState(original_query=original_query),
        )

    def append_turn(
        self,
        context: ConversationContext,
        *,
        role: str,
        content: str,
        turn_id: str | None = None,
    ) -> ConversationContext:
        updated = context.model_copy(deep=True)
        updated.recent_turns.append(
            ConversationTurn(
                turn_id=turn_id or f"turn-{uuid.uuid4().hex[:12]}",
                role=role,
                content=content,
            )
        )
        return ConversationContext.model_validate(updated.model_dump(mode="json"))

    def apply_structured_updates(
        self,
        context: ConversationContext,
        updates: Mapping[str, Any],
        *,
        source_turn_id: str | None = None,
    ) -> ConversationContext:
        """套用已驗證欄位；拒絕未知欄位並保留修訂 hash。

        ``risk_flags``、``negated_red_flags``、``completed_stages`` 採單調聯集，
        壓縮或後續更新不能移除曾觀察到的安全訊號。``original_query`` 一旦設定
        即不可改寫，確保 rewrite 仍可回查原意。
        """

        allowed = _EDITABLE_FIELDS | _MONOTONIC_LIST_FIELDS | {"original_query"}
        unknown = sorted(set(updates) - allowed)
        if unknown:
            raise ValueError(f"unsupported clinical state fields: {', '.join(unknown)}")

        updated = context.model_copy(deep=True)
        state = updated.clinical_state
        for field_name, incoming in updates.items():
            previous = getattr(state, field_name)
            if field_name == "original_query" and previous is not None and incoming != previous:
                raise ValueError("original_query is immutable once set")
            if field_name in _MONOTONIC_LIST_FIELDS:
                incoming_list = list(incoming or [])
                value = _dedupe(list(previous or []) + incoming_list)
            elif field_name == "authorization_status":
                value = AuthorizationStatus(incoming)
            elif field_name == "system_risk_classification" and incoming is not None:
                value = SystemRiskClassification.model_validate(incoming)
            else:
                value = incoming
            if value == previous:
                continue
            setattr(state, field_name, value)
            state.fact_revisions.append(
                FactRevision(
                    field_name=field_name,
                    previous_value_hash=_value_hash(previous) if previous is not None else None,
                    new_value_hash=_value_hash(value),
                    source_turn_id=source_turn_id,
                )
            )
        return ConversationContext.model_validate(updated.model_dump(mode="json"))

    def mark_stage_completed(
        self,
        context: ConversationContext,
        stage: ConversationStage,
        *,
        next_stage: ConversationStage | None = None,
        source_turn_id: str | None = None,
    ) -> ConversationContext:
        updates: dict[str, Any] = {"completed_stages": [stage]}
        if next_stage is not None:
            updates["current_stage"] = next_stage
        return self.apply_structured_updates(context, updates, source_turn_id=source_turn_id)

    def evaluate(
        self,
        context: ConversationContext,
        *,
        prompt_tokens: int | None = None,
        stage_completed: bool = False,
    ) -> CompactionDecision:
        estimated = prompt_tokens if prompt_tokens is not None else self.estimate_context_tokens(context)
        exchange_count = sum(1 for turn in context.recent_turns if turn.role == "user")
        reasons: list[str] = []
        if stage_completed:
            reasons.append("STAGE_COMPLETED")
        if estimated >= self.policy.token_threshold:
            reasons.append("TOKEN_THRESHOLD_REACHED")
        if exchange_count > self.policy.recent_exchanges:
            reasons.append("RECENT_WINDOW_EXCEEDED")
        return CompactionDecision(
            should_compact=bool(reasons),
            reasons=reasons,
            estimated_tokens=estimated,
            token_threshold=self.policy.token_threshold,
            exchange_count=exchange_count,
        )

    def compact(
        self,
        context: ConversationContext,
        *,
        prompt_tokens: int | None = None,
        stage_completed: bool = False,
    ) -> tuple[ConversationContext, CompactionDecision]:
        decision = self.evaluate(
            context,
            prompt_tokens=prompt_tokens,
            stage_completed=stage_completed,
        )
        if not decision.should_compact:
            return context.model_copy(deep=True), decision

        updated = context.model_copy(deep=True)
        start = self._recent_window_start(updated.recent_turns)
        if "TOKEN_THRESHOLD_REACHED" in decision.reasons:
            start = self._token_pressure_start(
                updated,
                initial_start=start,
                exact_prompt_tokens_supplied=prompt_tokens is not None,
            )
        removed = updated.recent_turns[:start]
        updated.recent_turns = updated.recent_turns[start:]
        updated.compacted_turn_hashes.extend(_turn_hash(turn) for turn in removed)
        updated.compacted_turn_count += len(removed)
        updated.compaction_count += 1
        updated.last_compaction_reasons = list(decision.reasons)
        validated = ConversationContext.model_validate(updated.model_dump(mode="json"))
        return validated, decision

    def estimate_context_tokens(self, context: ConversationContext) -> int:
        model_payload = self.build_model_context(context)
        return estimate_tokens(json.dumps(model_payload, ensure_ascii=False, sort_keys=True))

    def build_model_context(self, context: ConversationContext) -> dict[str, Any]:
        """只輸出結構化現況與近期對話，不把舊明文或修訂 hash 餵回模型。"""

        state = context.clinical_state.model_dump(
            mode="json",
            exclude={"fact_revisions"},
        )
        return {
            "clinical_state": state,
            "recent_conversation": [
                {"role": turn.role, "content": turn.content}
                for turn in context.recent_turns
            ],
        }

    def _recent_window_start(self, turns: list[ConversationTurn]) -> int:
        user_indices = [index for index, turn in enumerate(turns) if turn.role == "user"]
        if len(user_indices) <= self.policy.recent_exchanges:
            return 0
        return user_indices[-self.policy.recent_exchanges]

    def _token_pressure_start(
        self,
        context: ConversationContext,
        *,
        initial_start: int,
        exact_prompt_tokens_supplied: bool,
    ) -> int:
        """在 token 壓力下逐組移除最舊 exchange，至少保留最新一組。

        外部傳入的精確 token 數通常包含 system prompt、RAG 等本服務看不到的
        內容，因此觸發時至少釋放一組舊對話；之後再用本地估算持續縮減。
        """

        start = initial_start
        user_indices = [
            index
            for index, turn in enumerate(context.recent_turns)
            if turn.role == "user" and index >= start
        ]
        if exact_prompt_tokens_supplied and len(user_indices) > 1:
            start = user_indices[1]

        while True:
            retained_user_indices = [
                index
                for index, turn in enumerate(context.recent_turns)
                if turn.role == "user" and index >= start
            ]
            if len(retained_user_indices) <= 1:
                return start

            candidate = context.model_copy(deep=True)
            candidate.recent_turns = candidate.recent_turns[start:]
            if self.estimate_context_tokens(candidate) < self.policy.token_threshold:
                return start
            start = retained_user_indices[1]
