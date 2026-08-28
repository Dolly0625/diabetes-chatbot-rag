"""C: Generator experiments and the frozen v2 workflow adapter."""

from .c_workflow_input import CWorkflowInput, c_input_from_b_result, to_legacy_v2_case
from .deterministic_generators import ClinicianDraftGenerator, DeterministicFixtureCGenerator
from .langchain_adapter import LangChainCV2Generator
from .schemas import EvidenceAwareV2Answer

__all__ = [
    "CWorkflowInput",
    "ClinicianDraftGenerator",
    "DeterministicFixtureCGenerator",
    "EvidenceAwareV2Answer",
    "LangChainCV2Generator",
    "c_input_from_b_result",
    "to_legacy_v2_case",
]
