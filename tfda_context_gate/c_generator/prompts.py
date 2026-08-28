"""
tfda_context_gate.c_generator.prompts — 兼容 re-export 層

本檔僅為兼容層，實際定義已拆至 system_prompts.py 與 user_prompts.py。
保留此檔確保 `from tfda_context_gate.c_generator.prompts import EVIDENCE_AWARE_V2_SYSTEM` 仍可用。
"""

from __future__ import annotations

from .system_prompts import (  # noqa: F401
    AUXILIARY_JUDGE_SYSTEM,
    AUXILIARY_V2_JUDGE_SYSTEM,
    BASE_SYSTEM,
    BASELINE_SYSTEM,
    CLINICIAN_DRAFT_SYSTEM,
    EVIDENCE_AWARE_SYSTEM,
    EVIDENCE_AWARE_V2_SYSTEM,
    GROUNDED_SYSTEM,
)
from .user_prompts import (  # noqa: F401
    clinician_draft_user_prompt,
    context_block,
    evaluation_user_prompt,
    evaluation_v2_user_prompt,
    evidence_aware_v2_user_prompt,
    generator_user_prompt,
)

__all__ = [
    "AUXILIARY_JUDGE_SYSTEM",
    "AUXILIARY_V2_JUDGE_SYSTEM",
    "BASE_SYSTEM",
    "BASELINE_SYSTEM",
    "CLINICIAN_DRAFT_SYSTEM",
    "EVIDENCE_AWARE_SYSTEM",
    "EVIDENCE_AWARE_V2_SYSTEM",
    "GROUNDED_SYSTEM",
    "clinician_draft_user_prompt",
    "context_block",
    "evaluation_user_prompt",
    "evaluation_v2_user_prompt",
    "evidence_aware_v2_user_prompt",
    "generator_user_prompt",
]
