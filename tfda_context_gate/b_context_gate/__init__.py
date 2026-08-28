"""Canonical B v0.1 contract and deterministic context-gate adapter.

The numbered retrieval/judge scripts remain research implementations. This
package is the narrow workflow boundary that normalizes their outputs without
rewriting them.
"""

from .adapters import adapt_legacy_b_result, normalize_evidence
from .gate import DeterministicContextGate
from .schemas import (
    B_SCHEMA_VERSION,
    BDecision,
    CanonicalBInput,
    CanonicalBResult,
    CanonicalEvidence,
)

__all__ = [
    "B_SCHEMA_VERSION",
    "BDecision",
    "CanonicalBInput",
    "CanonicalBResult",
    "CanonicalEvidence",
    "DeterministicContextGate",
    "adapt_legacy_b_result",
    "normalize_evidence",
]

