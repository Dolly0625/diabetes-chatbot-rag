"""Canonical home: tfda_context_gate.e_observability.deadline.

Kept as a re-export for backwards-compatible imports in line_orchestration.
"""
from tfda_context_gate.e_observability.deadline import (
    DeadlineGuard,
    MAX_DEADLINE_WORKERS,
    current_deadline_guard,
    deadline_scope_active,
    fire_and_forget_with_deadline,
    run_with_deadline,
)

__all__ = [
    "DeadlineGuard",
    "MAX_DEADLINE_WORKERS",
    "current_deadline_guard",
    "deadline_scope_active",
    "fire_and_forget_with_deadline",
    "run_with_deadline",
]
