"""D v0.1: a final, evidence-aware output gate.

The package is intentionally independent from A, B, and C implementations.
Use :func:`run_output_gate` at the integration boundary, or use the schemas
and adapters directly when wiring a pipeline node.
"""

from .gate import run_output_gate
from .schemas import OutputGateRequest, OutputGateResult

__all__ = ["OutputGateRequest", "OutputGateResult", "run_output_gate"]
