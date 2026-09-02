"""Guarded approval artifact — fail-closed gate for SEMANTIC_ROUTER_MODE=guarded.

Design (six requirements):
 1. guarded alone never enables guarded.
 2. Machine-verifiable calibration approval artifact with 11 fields.
 3. All conditions must hold to enable.
 4. Missing/corrupt/expired/hash-mismatch/guarded_pass=false → downgrade shadow + non-PII fallback_reason, no early exit.
 5. Current evaluation BLOCKED → no valid PASS artifact → auto downgrade.
 6. No PYTEST_CURRENT_TEST bypass; synthetic PASS artifact via independent path allowed for tests.

This module is the single source of truth for approval validation.
It never logs artifact content or dataset content — only existence + guarded_pass boolean.
It never writes patient data.
It never checks PYTEST_CURRENT_TEST.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"

# 11 required fields (names match spec; artifact JSON keys)
REQUIRED_FIELDS = (
    "schema_version",
    "dataset_sha256",
    "calibration_timestamp",
    "cosine_threshold",
    "margin_threshold",
    "holdout_size",
    "false_fast",
    "mixed_recall",
    "correction_boundary_pass",
    "subject_boundary_pass",
    "guarded_pass",
)

# Default artifact locations — primary is product path, fallback is experiments path.
# Keep consistent: calibrate writes to primary, validation tries primary then fallback.
DEFAULT_APPROVAL_PATHS: list[Path] = [
    Path(__file__).resolve().parent / "approval.json",
    Path(__file__).resolve().parents[2] / "experiments" / "semantic_router_production" / "approval.json",
]

# Env override for artifact path (explicit, for injection in tests)
APPROVAL_PATH_ENV = "SEMANTIC_ROUTER_APPROVAL_PATH"
# Env override for max age (days)
APPROVAL_MAX_AGE_DAYS_ENV = "SEMANTIC_ROUTER_APPROVAL_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class ApprovalArtifact:
    schema_version: str
    dataset_sha256: str
    calibration_timestamp: str
    cosine_threshold: float
    margin_threshold: float
    holdout_size: int
    false_fast: int
    mixed_recall: float
    correction_boundary_pass: bool
    subject_boundary_pass: bool
    guarded_pass: bool

    # Parsed timestamp as datetime (UTC aware)
    parsed_timestamp: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_sha256": self.dataset_sha256,
            "calibration_timestamp": self.calibration_timestamp,
            "cosine_threshold": self.cosine_threshold,
            "margin_threshold": self.margin_threshold,
            "holdout_size": self.holdout_size,
            "false_fast": self.false_fast,
            "mixed_recall": self.mixed_recall,
            "correction_boundary_pass": self.correction_boundary_pass,
            "subject_boundary_pass": self.subject_boundary_pass,
            "guarded_pass": self.guarded_pass,
        }


def _resolve_approval_path(override: str | Path | None = None) -> Path | None:
    """Resolve artifact path: override > env > first existing default > primary default."""
    if override is not None:
        return Path(override)
    env_path = os.getenv(APPROVAL_PATH_ENV)
    if env_path:
        return Path(env_path)
    for p in DEFAULT_APPROVAL_PATHS:
        if p.is_file():
            return p
    # Return primary path for missing-case diagnostics (caller will see missing)
    return DEFAULT_APPROVAL_PATHS[0]


def compute_dataset_sha256(dataset_path: Path | None = None) -> str:
    """Compute hex sha256 of dataset.json.

    Args:
        dataset_path: path to dataset.json; defaults to production dataset.
    Returns:
        hex digest (lowercase).
    """
    if dataset_path is None:
        dataset_path = Path(__file__).resolve().parents[2] / "experiments" / "semantic_router_production" / "dataset.json"
    p = Path(dataset_path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'{"dataset_version": "1.0", "samples": []}')
    data = p.read_bytes()
    return hashlib.sha256(data).hexdigest()


def _parse_timestamp_iso8601(raw: str) -> datetime | None:
    try:
        s = str(raw).strip()
        # Support Z suffix
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_strict_bool(value: Any) -> bool:
    return value is True or value is False


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    import math

    return math.isfinite(float(value))


def _strict_parse_json(raw_text: str) -> Any:
    import math

    def _reject_constant(const: str):
        raise ValueError(f"non-finite constant {const} not allowed")

    return json.loads(raw_text, parse_constant=_reject_constant)


def load_and_validate_approval(
    path: Path | str | None = None,
    current_dataset_sha256: str | None = None,
    max_age_days: int | None = None,
) -> tuple[bool, str | None, ApprovalArtifact | None]:
    """Validate approval artifact at path.

    Args:
        path: artifact path; if None resolves via _resolve_approval_path.
        current_dataset_sha256: expected dataset hash; if None computes from current dataset.json.
        max_age_days: expiry window; if None reads env or defaults to 30.

    Returns:
        (is_valid, fallback_reason, artifact)
        is_valid True only if ALL 6+ conditions pass.
        fallback_reason is non-PII code like GUARDED_DOWNGRADED_MISSING_ARTIFACT (None when valid).
    """
    # Resolve max_age_days
    if max_age_days is None:
        raw_age = os.getenv(APPROVAL_MAX_AGE_DAYS_ENV)
        try:
            max_age_days = int(str(raw_age).strip()) if raw_age else DEFAULT_MAX_AGE_DAYS
        except Exception:
            max_age_days = DEFAULT_MAX_AGE_DAYS

    # Resolve path
    resolved = Path(path) if path is not None else _resolve_approval_path()
    if resolved is None:
        logger.info("guarded approval check: artifact_exists=False guarded_pass=False")
        return False, "GUARDED_DOWNGRADED_MISSING_ARTIFACT", None

    if not resolved.is_file():
        logger.info("guarded approval check: artifact_exists=False guarded_pass=False")
        return False, "GUARDED_DOWNGRADED_MISSING_ARTIFACT", None

    # Load JSON — reject NaN/Infinity via parse_constant
    try:
        raw_text = resolved.read_text(encoding="utf-8")
        payload = _strict_parse_json(raw_text)
    except Exception:
        logger.info("guarded approval check: artifact_exists=True guarded_pass=False")
        return False, "GUARDED_DOWNGRADED_MALFORMED_ARTIFACT", None

    if not isinstance(payload, dict):
        logger.info("guarded approval check: artifact_exists=True guarded_pass=False")
        return False, "GUARDED_DOWNGRADED_MALFORMED_ARTIFACT", None

    # Extra fields forbid — must be exactly REQUIRED_FIELDS
    if set(payload.keys()) != set(REQUIRED_FIELDS):
        gp_raw = payload.get("guarded_pass")
        gp = gp_raw is True
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    # Schema version check — strict exact match
    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        gp_raw = payload.get("guarded_pass")
        gp = gp_raw is True
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    # Strict type checks — booleans must be real JSON booleans
    for bool_field in ("guarded_pass", "correction_boundary_pass", "subject_boundary_pass"):
        if not _is_strict_bool(payload[bool_field]):
            gp_raw = payload.get("guarded_pass")
            gp = gp_raw is True
            logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
            return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    # Strict numeric checks — reject string numbers, bool-as-int, NaN/Infinity, out-of-range
    # cosine_threshold / margin_threshold / mixed_recall: finite 0.0-1.0
    for num_field in ("cosine_threshold", "margin_threshold", "mixed_recall"):
        val = payload[num_field]
        if not _is_strict_finite_number(val):
            gp = payload["guarded_pass"] is True
            logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
            return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None
        fval = float(val)
        if not (0.0 <= fval <= 1.0):
            gp = payload["guarded_pass"] is True
            logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
            return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    # holdout_size / false_fast: strict int >= thresholds
    for int_field, min_val in (("holdout_size", 30), ("false_fast", 0)):
        val = payload[int_field]
        if not _is_strict_int(val):
            gp = payload["guarded_pass"] is True
            logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
            return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None
        if int(val) < min_val:
            gp = payload["guarded_pass"] is True
            logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
            return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    # Extract validated values
    raw_sha = payload["dataset_sha256"]
    if not isinstance(raw_sha, str) or raw_sha != raw_sha.strip().lower() or len(raw_sha.strip()) != 64 or any(c not in "0123456789abcdef" for c in raw_sha.strip()):
        gp = payload["guarded_pass"] is True
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None
    dataset_sha256 = raw_sha.strip().lower()

    raw_ts = payload["calibration_timestamp"]
    if not isinstance(raw_ts, str) or not raw_ts.strip():
        gp = payload["guarded_pass"] is True
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None
    calibration_timestamp = raw_ts.strip()
    # Must contain timezone (Z or +/-)
    if not (calibration_timestamp.endswith("Z") or "+" in calibration_timestamp or calibration_timestamp.count("-") > 2):
        # naive timestamp without timezone
        gp = payload["guarded_pass"] is True
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    try:
        cosine_threshold = float(payload["cosine_threshold"])
        margin_threshold = float(payload["margin_threshold"])
        holdout_size = int(payload["holdout_size"])
        false_fast = int(payload["false_fast"])
        mixed_recall = float(payload["mixed_recall"])
        correction_boundary_pass = payload["correction_boundary_pass"] is True
        subject_boundary_pass = payload["subject_boundary_pass"] is True
        guarded_pass = payload["guarded_pass"] is True
    except Exception:
        gp = payload.get("guarded_pass") is True
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", gp)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    parsed_ts = _parse_timestamp_iso8601(calibration_timestamp)
    if parsed_ts is None:
        logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", guarded_pass)
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", None

    artifact = ApprovalArtifact(
        schema_version=schema_version,
        dataset_sha256=dataset_sha256,
        calibration_timestamp=calibration_timestamp,
        cosine_threshold=cosine_threshold,
        margin_threshold=margin_threshold,
        holdout_size=holdout_size,
        false_fast=false_fast,
        mixed_recall=mixed_recall,
        correction_boundary_pass=correction_boundary_pass,
        subject_boundary_pass=subject_boundary_pass,
        guarded_pass=guarded_pass,
        parsed_timestamp=parsed_ts,
    )

    # Log existence + guarded_pass only (no sensitive content)
    logger.info("guarded approval check: artifact_exists=True guarded_pass=%s", guarded_pass)

    # Expiry check
    try:
        now = datetime.now(timezone.utc)
        age_days = (now - parsed_ts).total_seconds() / 86400.0
        if age_days > float(max_age_days):
            return False, "GUARDED_DOWNGRADED_EXPIRED", artifact
        if age_days < -1:  # future timestamp beyond clock skew
            return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", artifact
    except Exception:
        return False, "GUARDED_DOWNGRADED_INVALID_SCHEMA", artifact

    # Dataset hash consistency
    if current_dataset_sha256 is None:
        try:
            current_dataset_sha256 = compute_dataset_sha256()
        except Exception:
            # If we cannot compute current hash, treat as mismatch (fail-closed)
            return False, "GUARDED_DOWNGRADED_HASH_MISMATCH", artifact
    current_hash_norm = str(current_dataset_sha256).strip().lower()
    if dataset_sha256 != current_hash_norm:
        return False, "GUARDED_DOWNGRADED_HASH_MISMATCH", artifact

    # Guarded_pass must be true
    if not guarded_pass:
        return False, "GUARDED_DOWNGRADED_NOT_PASSED", artifact

    # false_fast must be 0
    if false_fast != 0:
        return False, "GUARDED_DOWNGRADED_FALSE_FAST_NONZERO", artifact

    # mixed_recall >= 0.50
    if mixed_recall < 0.50:
        return False, "GUARDED_DOWNGRADED_LOW_RECALL", artifact

    # correction boundary PASS
    if not correction_boundary_pass:
        return False, "GUARDED_DOWNGRADED_CORRECTION_BOUNDARY_FAILED", artifact

    # subject boundary PASS
    if not subject_boundary_pass:
        return False, "GUARDED_DOWNGRADED_SUBJECT_BOUNDARY_FAILED", artifact

    # All passed
    return True, None, artifact


def is_guarded_approved(
    artifact_path_override: str | Path | None = None,
    current_dataset_sha256: str | None = None,
    max_age_days: int | None = None,
) -> tuple[bool, str | None, ApprovalArtifact | None]:
    """Return whether guarded mode is approved (all conditions hold).

    Thin wrapper over load_and_validate_approval with path resolution.
    Never uses PYTEST_CURRENT_TEST.
    """
    # Use env path override if caller didn't pass one
    override = artifact_path_override
    if override is None:
        env_override = os.getenv(APPROVAL_PATH_ENV)
        if env_override:
            override = env_override
    # Resolve to actual path for validation; if still None, let load_and_validate handle default resolution
    path = Path(override) if override is not None else _resolve_approval_path()
    return load_and_validate_approval(
        path=path,
        current_dataset_sha256=current_dataset_sha256,
        max_age_days=max_age_days,
    )


def get_effective_route_mode(
    requested: str,
    artifact_path_override: str | Path | None = None,
    current_dataset_sha256: str | None = None,
    max_age_days: int | None = None,
) -> tuple[str, str | None, ApprovalArtifact | None]:
    """Map requested mode to effective mode via approval gate.

    Args:
        requested: raw mode string (off/shadow/guarded normalized).
        artifact_path_override: optional artifact path for test injection.
        current_dataset_sha256: optional hash override.
        max_age_days: optional expiry override.

    Returns:
        (effective_mode, fallback_reason, artifact)
        fallback_reason is non-PII code when downgraded, else None.
    """
    req = str(requested or "off").strip().lower()
    if req not in ("off", "shadow", "guarded"):
        req = "off"
    if req != "guarded":
        return req, None, None
    approved, reason, artifact = is_guarded_approved(
        artifact_path_override=artifact_path_override,
        current_dataset_sha256=current_dataset_sha256,
        max_age_days=max_age_days,
    )
    if approved:
        return "guarded", None, artifact
    return "shadow", reason, artifact


@dataclass(frozen=True)
class EffectiveSemanticRouterConfig:
    requested_mode: str
    effective_mode: str
    cosine_threshold: float
    margin_threshold: float
    policy: str
    approval_status: bool
    fallback_reason: str | None
    dataset_sha256_short: str | None
    artifact_version: str | None


def resolve_effective_config(
    requested: str | None = None,
    artifact_path_override: str | Path | None = None,
    current_dataset_sha256: str | None = None,
    max_age_days: int | None = None,
) -> EffectiveSemanticRouterConfig:
    from tfda_context_gate.semantic_router.config import SemanticRouterConfig

    if requested is None:
        try:
            from tfda_context_gate.semantic_router.config import get_requested_mode

            requested = get_requested_mode()
        except Exception:
            requested = os.getenv("SEMANTIC_ROUTER_MODE", "off")
    req = str(requested or "off").strip().lower()
    if req not in ("off", "shadow", "guarded"):
        req = "off"
    env_cfg = SemanticRouterConfig.from_env()
    # Override requested with env if caller passed None already handled, else use passed
    # Use env thresholds as base for shadow/off
    base_cos = env_cfg.cosine_threshold
    base_mar = env_cfg.margin_threshold
    base_policy = env_cfg.policy
    effective, reason, artifact = get_effective_route_mode(
        req, artifact_path_override=artifact_path_override, current_dataset_sha256=current_dataset_sha256, max_age_days=max_age_days
    )
    if effective == "guarded" and artifact is not None:
        return EffectiveSemanticRouterConfig(
            requested_mode=req,
            effective_mode="guarded",
            cosine_threshold=float(artifact.cosine_threshold),
            margin_threshold=float(artifact.margin_threshold),
            policy=base_policy,
            approval_status=True,
            fallback_reason=None,
            dataset_sha256_short=artifact.dataset_sha256[:8] if artifact.dataset_sha256 else None,
            artifact_version=artifact.schema_version,
        )
    # shadow/off or downgraded: use env thresholds, mark observed
    short = None
    ver = None
    if artifact is not None and artifact.dataset_sha256:
        short = artifact.dataset_sha256[:8]
        ver = artifact.schema_version
    return EffectiveSemanticRouterConfig(
        requested_mode=req,
        effective_mode=effective,
        cosine_threshold=base_cos,
        margin_threshold=base_mar,
        policy=base_policy,
        approval_status=(effective == "guarded"),
        fallback_reason=reason,
        dataset_sha256_short=short,
        artifact_version=ver,
    )
