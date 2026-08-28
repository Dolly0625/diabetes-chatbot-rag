"""A v0.1 Input Router and Policy Gate.

The package deliberately keeps signal extraction separate from the final,
deterministic policy decision.  It does not generate medical answers.
"""

from .labels import (
    DeclaredRole,
    IntentTag,
    LanguageCode,
    PolicyReasonCode,
    RiskFlag,
    RouterStatus,
)
from .guard import (
    QWEN3GUARD_MODEL_ID,
    Qwen3GuardPromptInjectionGuard,
    RuleBasedPromptInjectionGuard,
    parse_qwen3guard_output,
)
from .router import (
    LangChainSignalExtractor,
    RuleBasedSignalExtractor,
    RouterDependencyError,
    route_request,
)
from .schemas import AResult, ContextModifiers, RequestContext, RouterSignals

__all__ = [
    "AResult",
    "ContextModifiers",
    "DeclaredRole",
    "IntentTag",
    "LanguageCode",
    "LangChainSignalExtractor",
    "QWEN3GUARD_MODEL_ID",
    "PolicyReasonCode",
    "Qwen3GuardPromptInjectionGuard",
    "RequestContext",
    "RiskFlag",
    "RouterDependencyError",
    "RouterSignals",
    "RouterStatus",
    "RuleBasedPromptInjectionGuard",
    "RuleBasedSignalExtractor",
    "route_request",
    "parse_qwen3guard_output",
]
