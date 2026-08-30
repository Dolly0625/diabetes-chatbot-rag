"""tfda_context_gate.semantic_router — production semantic routing."""

from .config import ROUTE_LABELS, SemanticRouterConfig
from .factory import build_semantic_router, is_available
from .router import DeterministicFakeEmbedder, OllamaEmbedder, ProductionSemanticRouter, PROTOTYPES
from .telemetry import SemanticRouteObservation, hash_text_prefix

__all__ = [
    "ROUTE_LABELS",
    "PROTOTYPES",
    "SemanticRouterConfig",
    "SemanticRouteObservation",
    "ProductionSemanticRouter",
    "OllamaEmbedder",
    "DeterministicFakeEmbedder",
    "build_semantic_router",
    "is_available",
    "hash_text_prefix",
]
