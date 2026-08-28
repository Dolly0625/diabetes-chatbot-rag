from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from tfda_context_gate.access_control import ActorAccessContext, PermissionScope
from tfda_context_gate.intake.summary import generate_previsit_summary
from tfda_context_gate.product_session import (
    ClinicianAccessLog,
    ProductSession,
    ProductSessionRepository,
    ShareGrant,
    ShareGrantDenied,
)

from .schemas import ClinicianSharedSummary, ShareGrantIssue


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareGrantService:
    def __init__(
        self,
        repository: ProductSessionRepository,
        *,
        grant_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self.repository = repository
        self.grant_ttl = grant_ttl

    def create(
        self,
        session: ProductSession,
        *,
        allowed_practitioner_hash: str | None = None,
        single_use: bool = True,
    ) -> ShareGrantIssue:
        required_scope = (
            PermissionScope.SHARE_PROXY_SUMMARY
            if str(session.actor_role) == "RELATED_PERSON"
            else PermissionScope.SHARE_OWN_SUMMARY
        )
        if session.status != "SUBMITTED" or required_scope not in session.permission_scopes:
            raise ShareGrantDenied("session is not confirmed or actor cannot share it")
        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)
        grant_id = f"grant-{uuid.uuid4().hex}"
        summary = generate_previsit_summary(session.intake_snapshot, request_id=grant_id)
        from tfda_context_gate.d_output_gate.gate import run_previsit_output_gate
        red_flag = (session.system_risk_classification or {}).get("level") == "RED_FLAG"
        gate = run_previsit_output_gate({
            "request_id": grant_id,
            "schema_version": "d.v0.1",
            "policy": {
                "router_status": "E_EMERGENCY" if red_flag else "G_GENERAL_EDUCATION",
                "rag_allowed": not red_flag,
                "risk_flags": ["POSSIBLE_EMERGENCY"] if red_flag else [],
                "intent_tags": [],
                "reason_codes": ["SHARE_GRANT_SNAPSHOT"],
            },
            "b_result": None,
            "c_result": summary.model_dump(mode="json"),
        })
        if gate.decision != "PASS":
            raise ShareGrantDenied("pre-visit summary did not pass the mandatory output gate")
        grant = ShareGrant(
            grant_id=grant_id,
            token_hash=_token_hash(token),
            session_id=session.session_id,
            grantor_principal_hash=session.principal_id_hash,
            subject_id_hash=session.subject_id_hash or session.principal_id_hash,
            allowed_practitioner_hash=allowed_practitioner_hash,
            intake_snapshot=session.intake_snapshot.model_dump(mode="json"),
            previsit_summary=summary.model_dump(mode="json"),
            output_gate_result=gate.model_dump(mode="json"),
            system_risk_classification=session.system_risk_classification,
            information_source=session.information_source,
            single_use=single_use,
            created_at=now,
            expires_at=now + self.grant_ttl,
        )
        self.repository.create_share_grant(grant)
        return ShareGrantIssue(
            grant_id=grant_id,
            token=token,
            expires_at=grant.expires_at,
            single_use=single_use,
        )

    def redeem(
        self,
        token: str,
        practitioner: ActorAccessContext,
    ) -> ClinicianSharedSummary:
        now = datetime.now(timezone.utc)
        if not practitioner.can(PermissionScope.VIEW_GRANTED_CLINICAL_SUMMARY):
            self._log(practitioner.principal_id_hash, "unknown", "DENIED", "missing permission")
            raise ShareGrantDenied("practitioner is not authorized to view shared summaries")
        try:
            grant = self.repository.consume_share_grant(
                _token_hash(token), practitioner.principal_id_hash, now=now
            )
        except ShareGrantDenied as exc:
            self._log(practitioner.principal_id_hash, "unknown", "DENIED", str(exc))
            raise
        if grant.output_gate_result.get("decision") != "PASS":
            self._log(practitioner.principal_id_hash, grant.grant_id, "DENIED", "snapshot was not output-gated")
            raise ShareGrantDenied("shared summary was not approved by the mandatory output gate")
        self._log(practitioner.principal_id_hash, grant.grant_id, "ALLOWED", None)
        return ClinicianSharedSummary(
            grant_id=grant.grant_id,
            intake_snapshot=grant.intake_snapshot,
            previsit_summary=grant.previsit_summary,
            output_gate_result=grant.output_gate_result,
            system_risk_classification=grant.system_risk_classification,
            information_source=grant.information_source,
            expires_at=grant.expires_at,
            accessed_at=now,
        )

    def revoke(self, grant_id: str, grantor_hash: str) -> ShareGrant:
        return self.repository.revoke_share_grant(grant_id, grantor_hash)

    def _log(self, practitioner_hash: str, grant_id: str, result: str, reason: str | None) -> None:
        self.repository.append_clinician_access_log(ClinicianAccessLog(
            log_id=f"access-{uuid.uuid4().hex}",
            practitioner_hash=practitioner_hash,
            grant_id=grant_id,
            action="VIEW_GRANTED_SUMMARY" if result == "ALLOWED" else "DENIED",
            result=result,
            reason=reason,
            created_at=datetime.now(timezone.utc),
        ))
