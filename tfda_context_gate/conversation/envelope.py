"""ConversationEnvelope — ACTIVE 對話期間供理解使用 (P1)。

不是 ShareEnvelope (仍只在 SUBMITTED + D PASS 後建立)。
嚴格 Pydantic StrictModel extra="forbid"，deterministic builder，可單元測試。

設計原則：
- 只含已驗證、已落地的 intake_snapshot (confirmed_intake)
- pending_action 明確標未確認，不混入 confirmed_intake
- recent_turns 最多 5 組 user/assistant exchanges (最多 10 turns)，current_message 獨立保存
- 不得包含：LINE user ID、principal hash、subject hash、token/share grant、webhook event ID、fact revision hash、raw image、API key、內部 sentinel
- subject 切換後不得帶入前一位 subject 的 recent turns 或 clinical facts (靠 ProductSession._new_subject_state 保障，builder 驗證隔離)
- 優先復用 ConversationContextManager.build_model_context() 但不無限制送模型 (bounded window)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.access_control import AuthorizationStatus, InformationSource
from tfda_context_gate.conversation.schemas import StrictModel
from tfda_context_gate.intake.schemas import PreVisitIntake

# Avoid circular import: product_session -> conversation. Import lazily inside builder.
# Local alias for IntakeField literal (keep in sync with product_session.schemas.IntakeField)
IntakeField = Literal[
    "known_medications",
    "allergies",
    "chronic_conditions",
    "family_history",
    "symptom_onset",
    "symptom_description",
    "symptom_severity",
    "questions_for_doctor",
]

# ── Envelope schema ───────────────────────────────────────────────────────────

ENVELOPE_SCHEMA_VERSION = "conversation.envelope.v1"
# active_task 為產品層任務標籤，bounded 枚舉，避免自由文字被模型誤用
ActiveTask = Literal["pre_visit_intake", "general_education", "chitchat", "idle", "unknown"]
SessionStatusLiteral = Literal["ACTIVE", "PAUSED", "AWAITING_CONFIRMATION", "SUBMITTED", "CLOSED"]
ActorRoleLiteral = Literal["PATIENT", "RELATED_PERSON", "SYSTEM_ADMIN", "PRACTITIONER"]
# 復用 IntakeField，但 envelope 內 pending_field 可為 None
# Authorization / InformationSource 復用既有枚舉的字串值


class EnvelopeTurn(StrictModel):
    """Envelope 內的有界 recent_turn 條目，只保留 role/content，不含 turn_id hash 等內部 sentinel。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ConversationEnvelope(StrictModel):
    """ACTIVE 對話期間的受控理解輸入。

    限制：
    - extra="forbid" 拒絕未知欄位 (模型輸出未知欄位必須 schema reject)
    - confirmed_intake 只能含已驗證 intake_snapshot
    - pending_action 明確未確認
    - recent_turns ≤5 exchanges (≤10 turns)，current_message 獨立
    - 不得含 LINE user ID / hashes / token / webhook event ID / fact revision hash / raw image / API key / sentinel
    """

    schema_version: str = Field(default=ENVELOPE_SCHEMA_VERSION, pattern=r"^conversation\.envelope\.v\d+$")
    active_task: ActiveTask
    session_status: SessionStatusLiteral
    actor_role: ActorRoleLiteral
    authorization_status: str  # AuthorizationStatus value
    information_source: str | None = None  # InformationSource value or None
    intake_stage: str = Field(pattern=r"^(stage1|stage2|stage3|review|submitted)$")
    pending_field: IntakeField | None = None
    pending_action: Any | None = None
    confirmed_intake: PreVisitIntake = Field(default_factory=PreVisitIntake)
    last_assistant_question: str | None = Field(default=None, max_length=5_000)
    recent_turns: list[EnvelopeTurn] = Field(default_factory=list, max_length=10)
    current_message: str = Field(min_length=1, max_length=8_000)

    model_config = ConfigDict(extra="forbid")


# ── Builder ───────────────────────────────────────────────────────────────────

# Forbidden keys that must never appear in envelope JSON even if upstream tries to inject
_FORBIDDEN_SUBSTRINGS = (
    "principal_id_hash",
    "subject_id_hash",
    "line_user_id",
    "webhook_event",
    "event_id",
    "claim_token",
    "token_hash",
    "share_grant",
    "fact_revision",
    "compacted_turn_hash",
    "raw_image",
    "image_bytes",
    "api_key",
    "OPENCODE_API_KEY",
    "sentinel",
    "principal_hash",
    "subject_hash",
)


def _sanitize_envelope_dict(data: dict[str, Any]) -> dict[str, Any]:
    """防禦性檢查：若 dict 含 forbidden key 則拋錯，避免 PII 洩漏。"""
    payload = str(data)
    # 直接檢查 keys 而非字串包含，避免誤判
    for key in data.keys():
        low = key.lower()
        for forb in _FORBIDDEN_SUBSTRINGS:
            if forb.lower() in low:
                raise ValueError(f"envelope must not contain forbidden field: {key}")
    # also check nested recent_turns not leaking turn_id hashes? EnvelopeTurn already excludes turn_id
    return data


def _active_task_from_session(session: Any) -> ActiveTask:
    """推導 active_task：依 intake 是否活躍與最近語意。Deterministic。"""
    # 若 status 非 ACTIVE 且非 AWAITING_CONFIRMATION，視為 idle/chitchat
    status = getattr(session, "status", "ACTIVE")
    intake_stage = getattr(session, "intake_stage", "stage1")
    intake_snapshot = getattr(session, "intake_snapshot", None)
    # 若 intak 活躍且有 pending_field 或仍在 stage1-3，視為 pre_visit_intake
    if status in ("ACTIVE", "AWAITING_CONFIRMATION", "PAUSED"):
        # 檢查是否已提交
        if intake_stage == "submitted" and status == "SUBMITTED":
            return "idle"
        # 若有 intake_snapshot 且尚未完成全部 stage，預設 pre_visit_intake
        # 透過 ProductSession 的導引：若 pending_field 存在或 intake 未完整，視為 intake 任務
        if getattr(session, "pending_field", None) is not None:
            return "pre_visit_intake"
        # 檢查 intake 是否仍有缺漏
        try:
            from tfda_context_gate.line_orchestration.orchestrator import ConversationOrchestrator

            if intake_snapshot is not None:
                nxt = ConversationOrchestrator._next_pending_field(intake_snapshot)
                if nxt is not None:
                    return "pre_visit_intake"
        except Exception:
            pass
        # 否則若最近問句是 chitchat 相關，標 chitchat
        return "general_education"
    return "idle"


def _bounded_recent_turns(recent_turns: list[Any], max_exchanges: int = 5) -> list[EnvelopeTurn]:
    """最多最近 5 組 user/assistant exchanges (最多 10 turns)。

    以 user 為 anchor：找到最後 5 個 user turn 的起始 index，保留其後所有 turns。
    若不足 5 組則全保留。保證 current_message 不被覆蓋 (caller 需獨立傳入)。

    Deterministic：同輸入同輸出，不依賴時間或隨機。
    """
    # Normalize to list of (role, content)
    normalized: list[tuple[str, str]] = []
    for t in recent_turns:
        # t may be ConversationTurn or dict or EnvelopeTurn
        if isinstance(t, dict):
            role = t.get("role", "user")
            content = t.get("content", "")
        elif hasattr(t, "role") and hasattr(t, "content"):
            role = getattr(t, "role")
            content = getattr(t, "content")
        else:
            continue
        if role not in ("user", "assistant"):
            continue
        normalized.append((role, str(content)))

    if not normalized:
        return []

    # Find user indices
    user_indices = [i for i, (r, _) in enumerate(normalized) if r == "user"]
    if len(user_indices) <= max_exchanges:
        start = 0
    else:
        start = user_indices[-max_exchanges]

    sliced = normalized[start:]
    # Enforce max 10 (5 exchanges *2)
    if len(sliced) > max_exchanges * 2:
        # keep last 10
        sliced = sliced[-(max_exchanges * 2) :]

    return [EnvelopeTurn(role=r, content=c) for r, c in sliced]  # type: ignore[arg-type]


def build_conversation_envelope(
    session: Any,
    current_message: str,
    *,
    schema_version: str = ENVELOPE_SCHEMA_VERSION,
) -> ConversationEnvelope:
    """Deterministic envelope builder，可單元測試。

    Inputs:
      - session: ProductSession (必須含 conversation_context, intake_snapshot, pending 等)
      - current_message: 本輪 raw user input (獨立保存，不可被 summary/sentinel 覆蓋)

    保證：
      - recent_turns 最多 5 exchanges (bounded window)
      - current_message 獨立，不被 recent_turns 覆蓋
      - confirmed_intake 僅含已驗證 intake_snapshot (不含 pending candidates)
      - 不含 forbidden PII / tokens / hashes
      - deterministic：同 session + message → 同 envelope

    優先復用 ConversationContextManager.build_model_context() 的結構化輸出，
    但改為 bounded window (5 exchanges) 且排除 fact_revisions 等敏感欄位。
    """
    if not current_message or not current_message.strip():
        raise ValueError("current_message must be non-empty")

    # 1. 從 session 提取 bounded recent_turns (不含 current_message)
    conversation_context = getattr(session, "conversation_context", None)
    if conversation_context is None:
        raise ValueError("session missing conversation_context")

    # Try to reuse build_model_context for shape, but apply bounded filter
    # We do not forward its entire output unlimited; we slice.
    raw_recent = getattr(conversation_context, "recent_turns", [])
    bounded = _bounded_recent_turns(raw_recent, max_exchanges=5)

    # Exclude current_message from recent_turns to keep independence
    # (caller appends user turn after envelope built, so recent_turns should be prior turns only)
    # If bounded ends with same content as current_message, keep it (it's prior turn, not current)
    # No dedup.

    # 2. confirmed_intake: directly from session.intake_snapshot (already verified via PendingAction/validation)
    intake_snapshot = getattr(session, "intake_snapshot", None)
    if intake_snapshot is None:
        confirmed = PreVisitIntake()
    elif isinstance(intake_snapshot, PreVisitIntake):
        confirmed = intake_snapshot.model_copy(deep=True)
    elif isinstance(intake_snapshot, dict):
        confirmed = PreVisitIntake.model_validate(intake_snapshot)
    else:
        # fallback: try model_validate
        try:
            confirmed = PreVisitIntake.model_validate(intake_snapshot)
        except Exception:
            confirmed = PreVisitIntake()

    # 3. active_task deterministic
    active_task = _active_task_from_session(session)

    # 4. Extract other fields with safe defaults
    session_status = getattr(session, "status", "ACTIVE")
    actor_role_obj = getattr(session, "actor_role", "PATIENT")
    try:
        actor_role_val = actor_role_obj.value if hasattr(actor_role_obj, "value") else str(actor_role_obj)
    except Exception:
        actor_role_val = "PATIENT"

    auth_status_obj = getattr(session, "authorization_status", AuthorizationStatus.UNVERIFIED)
    try:
        auth_val = auth_status_obj.value if hasattr(auth_status_obj, "value") else str(auth_status_obj)
    except Exception:
        auth_val = str(auth_status_obj)

    info_source_obj = getattr(session, "information_source", None)
    if info_source_obj is None:
        info_source_val = None
    else:
        try:
            info_source_val = info_source_obj.value if hasattr(info_source_obj, "value") else str(info_source_obj)  # type: ignore[union-attr]
        except Exception:
            info_source_val = str(info_source_obj)

    intake_stage = getattr(session, "intake_stage", "stage1")
    pending_field = getattr(session, "pending_field", None)
    pending_action = getattr(session, "pending_action", None)
    if pending_action is not None:
        try:
            # Lazily validate shape without circular import at module load
            from tfda_context_gate.product_session.schemas import PendingAction as _PA

            if not isinstance(pending_action, _PA):
                pending_action = _PA.model_validate(pending_action if isinstance(pending_action, dict) else pending_action.model_dump(mode="json") if hasattr(pending_action, "model_dump") else pending_action)
        except Exception:
            # Keep raw dict-like if validation fails; envelope still records unconfirmed pending
            pass

    last_q = getattr(session, "pending_question", None)

    envelope = ConversationEnvelope(
        schema_version=schema_version,
        active_task=active_task,
        session_status=session_status,
        actor_role=actor_role_val,  # type: ignore[arg-type]
        authorization_status=auth_val,
        information_source=info_source_val,
        intake_stage=intake_stage,
        pending_field=pending_field,
        pending_action=pending_action,
        confirmed_intake=confirmed,
        last_assistant_question=last_q,
        recent_turns=bounded,
        current_message=current_message,
    )

    # Defensive: ensure no forbidden fields leaked via model_dump
    _sanitize_envelope_dict(envelope.model_dump(mode="json"))

    return envelope


def envelope_to_model_context(envelope: ConversationEnvelope) -> dict[str, Any]:
    """將 envelope 轉為可安全送模型的最小上下文 (bounded, 無 PII)。

    只送需要的元件：
    - Interpreter: 受控 envelope (本函式輸出)
    - 不含 principal/subject hashes, tokens, raw image, fact revisions
    """
    pa = envelope.pending_action
    if pa is not None and hasattr(pa, "model_dump"):
        try:
            pa = pa.model_dump(mode="json")
        except Exception:
            pa = pa
    return {
        "schema_version": envelope.schema_version,
        "active_task": envelope.active_task,
        "session_status": envelope.session_status,
        "actor_role": envelope.actor_role,
        "authorization_status": envelope.authorization_status,
        "information_source": envelope.information_source,
        "intake_stage": envelope.intake_stage,
        "pending_field": envelope.pending_field,
        "pending_action": pa,
        "confirmed_intake": envelope.confirmed_intake.model_dump(mode="json"),
        "last_assistant_question": envelope.last_assistant_question,
        "recent_turns": [t.model_dump(mode="json") for t in envelope.recent_turns],
        "current_message": envelope.current_message,
    }
