"""Factory for ProductionSemanticRouter — single source of model truth."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from tfda_context_gate.run_config import env_value

from .config import SemanticRouterConfig
from .router import DeterministicFakeEmbedder, OllamaEmbedder, ProductionSemanticRouter


def _resolve_model_and_base() -> tuple[str, str, str]:
    """Resolve model truth from tfda_retriever + env, per spec.

    Priority for model: OLLAMA_EMBED_MODEL > EMBED_MODEL > tfda_retriever.DEFAULT_EMBEDDING_MODEL.
    Base URL via OLLAMA_BASE_URL or default.

    Returns:
        (model_name_without_prefix, base_url, raw_model_string)
    """
    # Import default lazily to honour single source of truth
    try:
        from tfda_context_gate.rag.tfda_retriever import DEFAULT_EMBEDDING_MODEL
    except Exception:
        DEFAULT_EMBEDDING_MODEL = "ollama/bge-m3:latest"

    raw = env_value("OLLAMA_EMBED_MODEL", None)
    if raw is None:
        raw = os.getenv("OLLAMA_EMBED_MODEL")
    if not raw:
        raw = env_value("EMBED_MODEL", None)
        if raw is None:
            raw = os.getenv("EMBED_MODEL")
    if not raw:
        raw = DEFAULT_EMBEDDING_MODEL

    base = env_value("OLLAMA_BASE_URL", None)
    if base is None:
        base = os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
    base = base.rstrip("/")

    model_name = raw.split("/", 1)[-1] if "/" in raw else raw
    return model_name, base, raw


def _should_use_fake() -> bool:
    """Return True when hermetic fake must be used."""
    return bool(os.getenv("PYTEST_CURRENT_TEST"))


def _probe_ollama(model_name: str, base_url: str, timeout_s: float = 2.0) -> bool:
    """Probe Ollama /api/tags for model availability.

    Args:
        model_name: model without prefix.
        base_url: Ollama base URL.
        timeout_s: request timeout.

    Returns:
        True if model is listed.
    """
    try:
        req = urllib.request.Request(base_url + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        names = {str(item.get("name", "")) for item in payload.get("models", [])}
        aliases = {model_name, model_name.removesuffix(":latest")}
        for name in names:
            if name in aliases or name.removesuffix(":latest") in aliases:
                return True
        # also check without suffix variations
        for name in names:
            nl = name.removesuffix(":latest")
            if nl == model_name.removesuffix(":latest"):
                return True
        return False
    except (OSError, ValueError, urllib.error.URLError, Exception):
        return False


def build_semantic_router(
    config: SemanticRouterConfig | None = None,
) -> ProductionSemanticRouter:
    """Build a ProductionSemanticRouter with lazy Ollama probe.

    No network is performed at import time; probe happens here on explicit
    build.  When ``PYTEST_CURRENT_TEST`` is set or Ollama is unreachable,
    a ``DeterministicFakeEmbedder`` is used and ``degraded=True``.

    Args:
        config: optional config override; defaults to ``from_env()``.

    Returns:
        ProductionSemanticRouter ready to route (never raises).
    """
    cfg = config or SemanticRouterConfig.from_env()

    if _should_use_fake():
        embedder = DeterministicFakeEmbedder()
        return ProductionSemanticRouter(embedder=embedder, config=cfg)

    model_name, base_url, _raw = _resolve_model_and_base()

    # quick probe — on failure fall back to fake
    available = _probe_ollama(model_name, base_url)
    if not available:
        # try to instantiate OllamaEmbedder and do a tiny embed as second chance
        # but don't fail — just fall back
        try:
            embedder_try = OllamaEmbedder(model_name=model_name, base_url=base_url)
            embedder_try.embed_query("test")
            return ProductionSemanticRouter(embedder=embedder_try, config=cfg)
        except Exception:
            pass
        fake = DeterministicFakeEmbedder()
        return ProductionSemanticRouter(embedder=fake, config=cfg)

    try:
        embedder = OllamaEmbedder(model_name=model_name, base_url=base_url)
        # lightweight live check
        embedder.embed_query("test")
        return ProductionSemanticRouter(embedder=embedder, config=cfg)
    except Exception:
        fake = DeterministicFakeEmbedder()
        return ProductionSemanticRouter(embedder=fake, config=cfg)


def is_available(router: ProductionSemanticRouter | None = None) -> bool:
    """Check if semantic routing is available (real embedder, not degraded).

    Args:
        router: optional router to check; if None, builds a temporary one
            (cheap when PYTEST_CURRENT_TEST is set).

    Returns:
        True if router is non-degraded and has prototype vectors.
    """
    if router is not None:
        return router.is_available() and not router.degraded
    # lightweight check without full build when in pytest
    if _should_use_fake():
        return False
    model_name, base_url, _raw = _resolve_model_and_base()
    return _probe_ollama(model_name, base_url)
