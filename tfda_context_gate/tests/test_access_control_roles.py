from __future__ import annotations

import pytest
from pydantic import ValidationError

from line_bot.app import _build_request_context
from tfda_context_gate.access_control import (
    ActorAccessContext,
    ActorRole,
    AuthorizationStatus,
    FrontendPersona,
    InformationSource,
    PermissionScope,
    RoleClaim,
    actor_role_from_declared_role,
    declared_role_from_actor_role,
)


_PRINCIPAL = "a" * 64
_SUBJECT = "b" * 64


def test_patient_and_related_person_share_patient_family_frontend():
    patient = RoleClaim.from_actor_role("PATIENT")
    proxy = RoleClaim.from_actor_role("RELATED_PERSON")

    assert patient.frontend_persona is FrontendPersona.PATIENT_FAMILY
    assert proxy.frontend_persona is FrontendPersona.PATIENT_FAMILY
    assert patient.actor_role is not proxy.actor_role


@pytest.mark.parametrize(
    ("legacy", "actor"),
    [
        ("PATIENT", ActorRole.PATIENT),
        ("CAREGIVER", ActorRole.RELATED_PERSON),
        ("RELATED_PERSON", ActorRole.RELATED_PERSON),
        ("HEALTHCARE_PROFESSIONAL", ActorRole.PRACTITIONER),
        ("PRACTITIONER", ActorRole.PRACTITIONER),
    ],
)
def test_legacy_and_new_role_names_map_to_same_actor(legacy: str, actor: ActorRole):
    assert actor_role_from_declared_role(legacy) is actor


def test_related_person_requires_subject_source_and_authorization_scope():
    access = ActorAccessContext(
        principal_id_hash=_PRINCIPAL,
        actor_role="RELATED_PERSON",
        frontend_persona="PATIENT_FAMILY",
        subject_id_hash=_SUBJECT,
        information_source="PROXY_OBSERVED",
        authorization_status="AUTHORIZED_CAREGIVER",
        permission_scopes=["CREATE_PROXY_INTAKE", "VIEW_PROXY_SUMMARY"],
    )

    assert access.can(PermissionScope.CREATE_PROXY_INTAKE)
    assert not access.can(PermissionScope.VIEW_GRANTED_CLINICAL_SUMMARY)


def test_related_person_without_subject_is_rejected():
    with pytest.raises(ValidationError, match="requires subject"):
        ActorAccessContext(
            principal_id_hash=_PRINCIPAL,
            actor_role="RELATED_PERSON",
            frontend_persona="PATIENT_FAMILY",
            authorization_status="AUTHORIZED_CAREGIVER",
        )


def test_unverified_role_cannot_hold_permission_scope():
    with pytest.raises(ValidationError, match="cannot hold permission"):
        ActorAccessContext(
            principal_id_hash=_PRINCIPAL,
            actor_role="PATIENT",
            frontend_persona="PATIENT_FAMILY",
            permission_scopes=["VIEW_OWN_SUMMARY"],
        )


def test_patient_cannot_receive_practitioner_permission():
    with pytest.raises(ValidationError, match="permission scope is not allowed"):
        ActorAccessContext(
            principal_id_hash=_PRINCIPAL,
            actor_role="PATIENT",
            frontend_persona="PATIENT_FAMILY",
            authorization_status="PATIENT_SELF",
            permission_scopes=["VIEW_EVIDENCE"],
        )


def test_line_request_accepts_new_actor_names_but_keeps_router_contract():
    proxy = _build_request_context("代家人整理", declared_role="RELATED_PERSON")
    practitioner = _build_request_context("查看草稿", declared_role="PRACTITIONER")

    assert proxy["declared_role"] == "CAREGIVER"
    assert practitioner["declared_role"] == "HEALTHCARE_PROFESSIONAL"


def test_system_admin_cannot_enter_medical_conversation_contract():
    with pytest.raises(ValueError, match="not a medical conversation role"):
        declared_role_from_actor_role("SYSTEM_ADMIN")
