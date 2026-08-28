"""產品角色、代理關係與授權範圍；不以 declared_role 取代授權。"""

from .schemas import (
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

__all__ = [
    "ActorAccessContext",
    "ActorRole",
    "AuthorizationStatus",
    "FrontendPersona",
    "InformationSource",
    "PermissionScope",
    "RoleClaim",
    "actor_role_from_declared_role",
    "declared_role_from_actor_role",
]
