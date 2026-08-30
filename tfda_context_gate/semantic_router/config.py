"""Production config for Semantic Router — modes, labels, thresholds.

All env reads go through ``tfda_context_gate.run_config.env_value`` so that
``.env`` is honoured without hard-coding.  Defaults match the research
recommendation (hybrid cosine 0.62 / margin 0.10) and may be overwritten by
calibration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from tfda_context_gate.run_config import env_value

ROUTE_LABELS: tuple[str, ...] = (
    "PURE_EDUCATION",
    "PURE_INTAKE",
    "MIXED",
    "CORRECTION",
    "SUBJECT_CHANGE",
    "CHITCHAT",
    "UNKNOWN",
)

SemanticRouterMode = Literal["off", "shadow", "guarded"]


def _parse_float(name: str, default: float) -> float:
    """Parse env var as float, falling back to default on missing/invalid."""
    raw = env_value(name, None)
    # also honour direct os.getenv for hermetic tests that set env directly
    if raw is None:
        raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _parse_mode(raw: str | None) -> SemanticRouterMode:
    """Normalize mode string; unknown values fall back to 'off'."""
    if raw is None:
        return "off"
    cleaned = str(raw).strip().lower()
    if cleaned in ("off", "shadow", "guarded"):
        return cleaned  # type: ignore[return-value]
    return "off"


@dataclass(frozen=True)
class SemanticRouterConfig:
    """Validated config for :class:`ProductionSemanticRouter`.

    Attributes:
        mode: ``off`` disables routing, ``shadow`` logs only, ``guarded``
            allows downstream use gated by confidence.
        cosine_threshold: minimum top cosine to accept (used by cosine/hybrid).
        margin_threshold: minimum (top - second) gap to accept (margin/hybrid).
        policy: ``cosine`` / ``margin`` / ``hybrid`` (hybrid requires both).
    """

    mode: SemanticRouterMode
    cosine_threshold: float
    margin_threshold: float
    policy: Literal["cosine", "margin", "hybrid"]

    @classmethod
    def from_env(cls) -> SemanticRouterConfig:
        """Build config from environment with calibrated defaults.

        Env vars:
            SEMANTIC_ROUTER_MODE — off|shadow|guarded (default off)
            SEMANTIC_ROUTER_COSINE_THRESHOLD — float (default 0.62)
            SEMANTIC_ROUTER_MARGIN_THRESHOLD — float (default 0.10)
            SEMANTIC_ROUTER_POLICY — cosine|margin|hybrid (default hybrid)
        """
        mode_raw = env_value("SEMANTIC_ROUTER_MODE", None)
        if mode_raw is None:
            mode_raw = os.getenv("SEMANTIC_ROUTER_MODE")
        mode = _parse_mode(mode_raw)

        cosine_threshold = _parse_float("SEMANTIC_ROUTER_COSINE_THRESHOLD", 0.62)
        margin_threshold = _parse_float("SEMANTIC_ROUTER_MARGIN_THRESHOLD", 0.10)

        policy_raw = env_value("SEMANTIC_ROUTER_POLICY", None)
        if policy_raw is None:
            policy_raw = os.getenv("SEMANTIC_ROUTER_POLICY")
        policy_clean = str(policy_raw).strip().lower() if policy_raw else "hybrid"
        if policy_clean not in ("cosine", "margin", "hybrid"):
            policy_clean = "hybrid"
        policy: Literal["cosine", "margin", "hybrid"] = policy_clean  # type: ignore[assignment]

        return cls(
            mode=mode,
            cosine_threshold=cosine_threshold,
            margin_threshold=margin_threshold,
            policy=policy,
        )
