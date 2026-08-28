from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .labels import (
    DeclaredRole,
    IntentTag,
    LanguageCode,
    PolicyReasonCode,
    Polarity,
    RiskFlag,
    RouterStatus,
    TargetSubject,
    TimeFrame,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestContext(StrictModel):
    request_id: str = Field(min_length=1)
    schema_version: str = Field(default="a.v0.1", min_length=1)
    user_raw_input: str = Field(min_length=1, max_length=8_000)
    declared_role: DeclaredRole
    language: LanguageCode = LanguageCode.ZH_TW


class ContextModifiers(StrictModel):
    time_frame: TimeFrame = TimeFrame.CURRENT
    target_subject: TargetSubject = TargetSubject.SELF
    polarity: Polarity = Polarity.AFFIRMATIVE
    language: LanguageCode = LanguageCode.ZH_TW


class RouterSignals(StrictModel):
    """Layer 1 output: observations only, with no final route field."""

    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: ContextModifiers


class AResult(StrictModel):
    """Stable A-to-downstream payload; `router_status` is the sole route."""

    request_id: str
    schema_version: str
    user_raw_input: str
    declared_role: DeclaredRole
    language: LanguageCode
    intent_tags: list[IntentTag]
    risk_flags: list[RiskFlag]
    context_modifiers: ContextModifiers
    router_status: RouterStatus
    reason_codes: list[PolicyReasonCode]
    # [工程新增] Explicit downstream guard so callers do not re-implement policy.
    rag_allowed: bool

    @classmethod
    def from_request_and_decision(
        cls,
        request: RequestContext,
        signals: RouterSignals,
        router_status: RouterStatus,
        reason_codes: list[PolicyReasonCode],
    ) -> "AResult":
        return cls(
            request_id=request.request_id,
            schema_version=request.schema_version,
            user_raw_input=request.user_raw_input,
            declared_role=request.declared_role,
            language=request.language,
            intent_tags=signals.intent_tags,
            risk_flags=signals.risk_flags,
            context_modifiers=signals.context_modifiers,
            router_status=router_status,
            reason_codes=reason_codes,
            rag_allowed=router_status is RouterStatus.G_GENERAL_EDUCATION,
        )
