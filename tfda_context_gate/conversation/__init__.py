"""跨訊息對話上下文：結構化事實 + 有界近期對話視窗。"""

from .envelope import (
    ConversationEnvelope,
    EnvelopeTurn,
    build_conversation_envelope,
    envelope_to_model_context,
)
from .manager import ConversationContextManager, estimate_tokens
from tfda_context_gate.access_control import AuthorizationStatus

from .schemas import (
    ClinicalConversationState,
    CompactionDecision,
    CompactionPolicy,
    ConversationContext,
    ConversationTurn,
    FactRevision,
)

__all__ = [
    "AuthorizationStatus",
    "ClinicalConversationState",
    "CompactionDecision",
    "CompactionPolicy",
    "ConversationContext",
    "ConversationContextManager",
    "ConversationTurn",
    "ConversationEnvelope",
    "EnvelopeTurn",
    "FactRevision",
    "build_conversation_envelope",
    "envelope_to_model_context",
    "estimate_tokens",
]
