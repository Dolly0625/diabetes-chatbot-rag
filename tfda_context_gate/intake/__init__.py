"""Intake package — pre-visit structured intake (p6.2)."""

from .schemas import (
    INTAKE_FIELD_QUESTIONS,
    IntakeQuestion,
    PreVisitIntake,
    PreVisitSummary,
    TimelineEntry,
    STRUCTURED_INTAKE_FIELDS,
)
from .summary import generate_previsit_summary
from .timeline import build_timeline, build_timeline_from_entries
from .tool import PreVisitIntakeTool, PREVISIT_DISCLAIMER

try:
    from .qr_ocr_service import MedicationBagOCRService

    __all_qr__ = ["MedicationBagOCRService"]
except ImportError:
    __all_qr__ = []

__all__ = [
    "INTAKE_FIELD_QUESTIONS",
    "STRUCTURED_INTAKE_FIELDS",
    "IntakeQuestion",
    "PreVisitIntake",
    "PreVisitSummary",
    "TimelineEntry",
    "PreVisitIntakeTool",
    "PREVISIT_DISCLAIMER",
    "build_timeline",
    "build_timeline_from_entries",
    "generate_previsit_summary",
] + __all_qr__
