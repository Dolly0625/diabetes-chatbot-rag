from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tfda_context_gate.access_control import (
    ActorAccessContext,
    ActorRole,
    AuthorizationStatus,
    FrontendPersona,
    InformationSource,
    PermissionScope,
)
from tfda_context_gate.conversation import ConversationContext
from tfda_context_gate.intake.schemas import PreVisitIntake


SessionStatus = Literal["ACTIVE", "PAUSED", "AWAITING_CONFIRMATION", "SUBMITTED", "CLOSED"]
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


class ProductSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    principal_id_hash: str = Field(min_length=64, max_length=64)
    actor_role: ActorRole = ActorRole.PATIENT
    frontend_persona: FrontendPersona = FrontendPersona.PATIENT_FAMILY
    subject_id_hash: str | None = Field(default=None, min_length=64, max_length=64)
    information_source: InformationSource | None = None
    authorization_status: AuthorizationStatus = AuthorizationStatus.UNVERIFIED
    permission_scopes: list[PermissionScope] = Field(default_factory=list)
    conversation_context: ConversationContext
    intake_snapshot: PreVisitIntake = Field(default_factory=PreVisitIntake)
    intake_stage: Literal["stage1", "stage2", "stage3", "review", "submitted"] = "stage1"
    pending_field: IntakeField | None = None
    pending_question: str | None = Field(default=None, max_length=5_000)
    system_risk_classification: dict[str, Any] | None = None
    status: SessionStatus = "ACTIVE"
    version: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_times_and_stage(self) -> "ProductSession":
        for value in (self.created_at, self.updated_at, self.expires_at):
            if value.tzinfo is None:
                raise ValueError("session timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.status == "SUBMITTED" and self.intake_stage != "submitted":
            raise ValueError("submitted session must use submitted intake stage")
        # Session 本身也必須符合角色／授權／scope 的硬限制，不能只靠 UI 或 prompt。
        ActorAccessContext(
            principal_id_hash=self.principal_id_hash,
            actor_role=self.actor_role,
            frontend_persona=self.frontend_persona,
            authorization_status=self.authorization_status,
            permission_scopes=self.permission_scopes,
            subject_id_hash=self.subject_id_hash,
            information_source=self.information_source,
        )
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(timezone.utc))


class WebhookEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    principal_id_hash: str = Field(min_length=64, max_length=64)
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    claim_token: str | None = Field(default=None, min_length=32, max_length=128)
    lease_expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class ShareGrant(BaseModel):
    """病患確認後建立的唯讀 snapshot 授權；repository 只保存 token hash。"""

    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=1, max_length=128)
    token_hash: str = Field(min_length=64, max_length=64)
    session_id: str = Field(min_length=1, max_length=128)
    grantor_principal_hash: str = Field(min_length=64, max_length=64)
    subject_id_hash: str = Field(min_length=64, max_length=64)
    allowed_practitioner_hash: str | None = Field(default=None, min_length=64, max_length=64)
    intake_snapshot: dict[str, Any]
    previsit_summary: dict[str, Any]
    output_gate_result: dict[str, Any] = Field(default_factory=dict)
    system_risk_classification: dict[str, Any] | None = None
    information_source: InformationSource | None = None
    status: Literal["ACTIVE", "USED", "REVOKED"] = "ACTIVE"
    single_use: bool = True
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ShareGrant":
        for value in (self.created_at, self.expires_at, self.used_at, self.revoked_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("share grant timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("share grant expires_at must be later than created_at")
        if self.status == "USED" and self.used_at is None:
            raise ValueError("used share grant requires used_at")
        if self.status == "REVOKED" and self.revoked_at is None:
            raise ValueError("revoked share grant requires revoked_at")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(timezone.utc))


class ClinicianAccessLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_id: str = Field(min_length=1, max_length=128)
    practitioner_hash: str = Field(min_length=64, max_length=64)
    grant_id: str = Field(min_length=1, max_length=128)
    action: Literal["VIEW_GRANTED_SUMMARY", "DENIED"]
    result: Literal["ALLOWED", "DENIED"]
    reason: str | None = Field(default=None, max_length=256)
    created_at: datetime
