from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _CodeEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ActorRole(_CodeEnum):
    PATIENT = "PATIENT"
    RELATED_PERSON = "RELATED_PERSON"
    PRACTITIONER = "PRACTITIONER"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"


class FrontendPersona(_CodeEnum):
    PATIENT_FAMILY = "PATIENT_FAMILY"
    CLINICIAN = "CLINICIAN"
    INTERNAL = "INTERNAL"


class AuthorizationStatus(_CodeEnum):
    UNVERIFIED = "UNVERIFIED"
    PATIENT_SELF = "PATIENT_SELF"
    AUTHORIZED_CAREGIVER = "AUTHORIZED_CAREGIVER"
    LEGAL_GUARDIAN = "LEGAL_GUARDIAN"
    CLINICIAN_VERIFIED = "CLINICIAN_VERIFIED"
    SYSTEM_ADMIN_VERIFIED = "SYSTEM_ADMIN_VERIFIED"
    REVOKED = "REVOKED"


class InformationSource(_CodeEnum):
    SELF_REPORTED = "SELF_REPORTED"
    SUBJECT_REPORTED_VIA_PROXY = "SUBJECT_REPORTED_VIA_PROXY"
    PROXY_OBSERVED = "PROXY_OBSERVED"
    CLINICIAN_ENTERED = "CLINICIAN_ENTERED"


class PermissionScope(_CodeEnum):
    CREATE_OWN_INTAKE = "CREATE_OWN_INTAKE"
    VIEW_OWN_SUMMARY = "VIEW_OWN_SUMMARY"
    SHARE_OWN_SUMMARY = "SHARE_OWN_SUMMARY"
    CREATE_PROXY_INTAKE = "CREATE_PROXY_INTAKE"
    VIEW_PROXY_SUMMARY = "VIEW_PROXY_SUMMARY"
    SHARE_PROXY_SUMMARY = "SHARE_PROXY_SUMMARY"
    VIEW_GRANTED_CLINICAL_SUMMARY = "VIEW_GRANTED_CLINICAL_SUMMARY"
    VIEW_EVIDENCE = "VIEW_EVIDENCE"
    MANAGE_SYSTEM = "MANAGE_SYSTEM"


_PERSONA_BY_ROLE = {
    ActorRole.PATIENT: FrontendPersona.PATIENT_FAMILY,
    ActorRole.RELATED_PERSON: FrontendPersona.PATIENT_FAMILY,
    ActorRole.PRACTITIONER: FrontendPersona.CLINICIAN,
    ActorRole.SYSTEM_ADMIN: FrontendPersona.INTERNAL,
}
_AUTH_BY_ROLE = {
    ActorRole.PATIENT: {AuthorizationStatus.UNVERIFIED, AuthorizationStatus.PATIENT_SELF, AuthorizationStatus.REVOKED},
    ActorRole.RELATED_PERSON: {AuthorizationStatus.UNVERIFIED, AuthorizationStatus.AUTHORIZED_CAREGIVER, AuthorizationStatus.LEGAL_GUARDIAN, AuthorizationStatus.REVOKED},
    ActorRole.PRACTITIONER: {AuthorizationStatus.UNVERIFIED, AuthorizationStatus.CLINICIAN_VERIFIED, AuthorizationStatus.REVOKED},
    ActorRole.SYSTEM_ADMIN: {AuthorizationStatus.UNVERIFIED, AuthorizationStatus.SYSTEM_ADMIN_VERIFIED, AuthorizationStatus.REVOKED},
}
_SCOPES_BY_ROLE = {
    ActorRole.PATIENT: {PermissionScope.CREATE_OWN_INTAKE, PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.SHARE_OWN_SUMMARY},
    ActorRole.RELATED_PERSON: {PermissionScope.CREATE_PROXY_INTAKE, PermissionScope.VIEW_PROXY_SUMMARY, PermissionScope.SHARE_PROXY_SUMMARY},
    ActorRole.PRACTITIONER: {PermissionScope.VIEW_GRANTED_CLINICAL_SUMMARY, PermissionScope.VIEW_EVIDENCE},
    ActorRole.SYSTEM_ADMIN: {PermissionScope.MANAGE_SYSTEM},
}


class RoleClaim(BaseModel):
    """使用者選擇的角色，只決定介面，不構成資料授權。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_role: ActorRole
    frontend_persona: FrontendPersona

    @classmethod
    def from_actor_role(cls, actor_role: ActorRole | str) -> "RoleClaim":
        role = ActorRole(actor_role)
        return cls(actor_role=role, frontend_persona=_PERSONA_BY_ROLE[role])


class ActorAccessContext(BaseModel):
    """已解析的資料存取上下文；permission scopes 才是執行授權依據。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id_hash: str = Field(min_length=64, max_length=64)
    actor_role: ActorRole
    frontend_persona: FrontendPersona
    authorization_status: AuthorizationStatus = AuthorizationStatus.UNVERIFIED
    permission_scopes: list[PermissionScope] = Field(default_factory=list)
    subject_id_hash: str | None = Field(default=None, min_length=64, max_length=64)
    information_source: InformationSource | None = None

    @model_validator(mode="after")
    def validate_role_relationship(self) -> "ActorAccessContext":
        if self.frontend_persona != _PERSONA_BY_ROLE[self.actor_role]:
            raise ValueError("frontend_persona does not match actor_role")
        if self.authorization_status not in _AUTH_BY_ROLE[self.actor_role]:
            raise ValueError("authorization_status does not match actor_role")
        if not set(self.permission_scopes).issubset(_SCOPES_BY_ROLE[self.actor_role]):
            raise ValueError("permission scope is not allowed for actor_role")
        if self.actor_role is ActorRole.RELATED_PERSON:
            if self.subject_id_hash is None or self.information_source not in {
                InformationSource.SUBJECT_REPORTED_VIA_PROXY,
                InformationSource.PROXY_OBSERVED,
            }:
                raise ValueError("RELATED_PERSON requires subject and proxy information source")
        if self.authorization_status in {AuthorizationStatus.UNVERIFIED, AuthorizationStatus.REVOKED} and self.permission_scopes:
            raise ValueError("unverified or revoked actor cannot hold permission scopes")
        return self

    def can(self, scope: PermissionScope | str) -> bool:
        return PermissionScope(scope) in self.permission_scopes


def actor_role_from_declared_role(value: object) -> ActorRole:
    normalized = str(getattr(value, "value", value)).strip().upper()
    mapping = {
        "PATIENT": ActorRole.PATIENT,
        "CAREGIVER": ActorRole.RELATED_PERSON,
        "RELATED_PERSON": ActorRole.RELATED_PERSON,
        "HEALTHCARE_PROFESSIONAL": ActorRole.PRACTITIONER,
        "PRACTITIONER": ActorRole.PRACTITIONER,
        "SYSTEM_ADMIN": ActorRole.SYSTEM_ADMIN,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported role claim: {normalized}") from exc


def declared_role_from_actor_role(value: ActorRole | str) -> str:
    role = ActorRole(value)
    mapping = {
        ActorRole.PATIENT: "PATIENT",
        ActorRole.RELATED_PERSON: "CAREGIVER",
        ActorRole.PRACTITIONER: "HEALTHCARE_PROFESSIONAL",
    }
    if role is ActorRole.SYSTEM_ADMIN:
        raise ValueError("SYSTEM_ADMIN is not a medical conversation role")
    return mapping[role]
