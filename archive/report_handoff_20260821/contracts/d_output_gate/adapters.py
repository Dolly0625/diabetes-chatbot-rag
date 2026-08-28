from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from .schemas import (
    CandidateResponse,
    EvidenceSet,
    OutputGateRequest,
    PolicySnapshot,
    SupportedClaim,
    UnsupportedRequest,
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"expected an object, got {type(value).__name__}")


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def build_gate_request(payload: Mapping[str, Any] | OutputGateRequest) -> OutputGateRequest:
    """Adapt existing A/B/C payloads without changing their implementations.

    Accepted shapes include the canonical D shape and the existing fixture
    shape: ``a_result``, ``b_result``/``b_context``, and ``c_result``/``output``.
    Missing fields are kept missing so the gate can fail closed with a useful
    deterministic reason instead of inventing an interface.
    """

    if isinstance(payload, OutputGateRequest):
        return payload
    raw = dict(payload)

    a_raw = _first(raw, "policy", "a_result", "policy_result")
    if a_raw is None:
        a_raw = raw
    a = _as_dict(a_raw)

    b_raw = _first(raw, "evidence_set", "b_result", "b_context", "context_gate_result")
    if b_raw is None:
        b_raw = raw
    b = _as_dict(b_raw)

    c_raw = _first(raw, "candidate_response", "c_result", "output")
    if c_raw is None:
        raise ValueError("missing candidate_response/c_result/output")

    policy = {
        "router_status": a.get("router_status"),
        "rag_allowed": a.get("rag_allowed"),
        "risk_flags": a.get("risk_flags", []),
        "intent_tags": a.get("intent_tags", []),
        "reason_codes": a.get("reason_codes", []),
    }

    approved = _first(b, "approved_evidence_ids", "approved_document_ids")
    if approved is None:
        # Do not derive approval from retrieved context. B must explicitly
        # mark the evidence that it approved.
        approved = []
    raw_records = _first(b, "evidence", "contexts", "retrieved_contexts") or []
    evidence: list[dict[str, Any]] = []
    for record in raw_records:
        item = _as_dict(record)
        evidence_id = _first(item, "evidence_id", "document_id", "chunk_id", "id")
        content = _first(item, "content", "page_content", "text")
        metadata = item.get("metadata", {})
        evidence.append(
            {
                "evidence_id": evidence_id,
                "content": content,
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )

    evidence_set = {
        "decision": b.get("decision", b.get("b_decision")),
        "approved_evidence_ids": approved,
        "evidence": evidence,
    }
    return OutputGateRequest(
        request_id=raw.get("request_id"),
        schema_version=raw.get("schema_version", "d.v0.1"),
        policy=policy,
        evidence_set=evidence_set,
        candidate_response=c_raw,
    )


def parse_policy(value: Any) -> PolicySnapshot:
    return PolicySnapshot.model_validate(value)


def parse_evidence_set(value: Any) -> EvidenceSet:
    return EvidenceSet.model_validate(value)


def parse_candidate_response(value: Any) -> CandidateResponse:
    """Parse C v2 and adapt the unchanged C v1 schema.

    C v1 has ``claims`` instead of ``supported_claims`` and no PARTIAL code.
    This is an adapter only; it does not modify or reinterpret C's generator.
    """

    raw = _as_dict(value)
    if "supported_claims" in raw or "unsupported_requests" in raw:
        return CandidateResponse.model_validate(raw)

    if "claims" in raw:
        claims = []
        for claim in raw.get("claims", []):
            item = _as_dict(claim)
            claims.append(
                SupportedClaim(
                    claim_id=item.get("claim_id"),
                    claim=item.get("claim"),
                    evidence_ids=item.get("evidence_ids", []),
                )
            )
        return CandidateResponse(
            decision=raw.get("decision"),
            answer=raw.get("answer"),
            supported_claims=claims,
            unsupported_requests=[],
            limitations=raw.get("limitations", []),
        )
    raise ValueError("C response has neither v2 supported_claims nor v1 claims")


def validation_error_text(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return str(error).splitlines()[0]
    return str(error)
