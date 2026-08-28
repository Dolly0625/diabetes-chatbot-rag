"""Workflow adapter — 兼容 re-export 層

本檔僅為兼容層，實際定義已拆至：
- c_workflow_input.py（CWorkflowInput / C_V2_SCHEMA_VERSION / c_input_from_b_result / to_legacy_v2_case / CGenerator）
- deterministic_generators.py（DeterministicFixtureCGenerator / ClinicianDraftGenerator）
- langchain_adapter.py（LangChainCV2Generator）

保留此檔確保 `from tfda_context_gate.c_generator.workflow_adapter import CWorkflowInput` 仍可用。
"""

from __future__ import annotations

from .c_workflow_input import (
    C_V2_SCHEMA_VERSION,
    CGenerator,
    CWorkflowInput,
    c_input_from_b_result,
    to_legacy_v2_case,
)
from .deterministic_generators import (
    CLINICIAN_DISCLAIMER,
    ClinicianDraftGenerator,
    DeterministicFixtureCGenerator,
)
from .langchain_adapter import LangChainCV2Generator

__all__ = [
    "CLINICIAN_DISCLAIMER",
    "CGenerator",
    "CWorkflowInput",
    "C_V2_SCHEMA_VERSION",
    "ClinicianDraftGenerator",
    "DeterministicFixtureCGenerator",
    "LangChainCV2Generator",
    "c_input_from_b_result",
    "to_legacy_v2_case",
]
