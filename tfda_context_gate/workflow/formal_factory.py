from __future__ import annotations

"""Formal factory — 從 runner 抽 _build_formal_extractor/retriever/generator。

純搬運，不改邏輯，僅瘦身 runner.py。
"""

from functools import lru_cache  # noqa: E402


def _build_formal_extractor():
    from tfda_context_gate.a_router.router import LangChainSignalExtractor

    return LangChainSignalExtractor.from_env()


@lru_cache(maxsize=1)
def _build_formal_retriever():
    from tfda_context_gate.run_config import env_value

    external_url = (env_value("RAG_RETRIEVAL_URL", "") or "").strip()
    if external_url:
        from tfda_context_gate.rag.external_retriever import ExternalContractRetriever

        timeout_raw = env_value("RAG_RETRIEVAL_TIMEOUT_S", "3") or "3"
        try:
            timeout_s = float(timeout_raw)
        except (TypeError, ValueError):
            timeout_s = 3.0
        # An explicitly configured external trust boundary must never degrade
        # silently to fixture evidence because of a typo or unsafe URL.
        return ExternalContractRetriever(external_url, timeout_s=timeout_s)

    try:
        from tfda_context_gate.rag.tfda_retriever import TFDADrugSafetyRetriever

        embedding_model = (
            env_value("EMBED_MODEL", "")
            or env_value("OLLAMA_EMBED_MODEL", "")
            or ""
        ).strip()
        if not embedding_model:
            raise RuntimeError("EMBED_MODEL or OLLAMA_EMBED_MODEL is required for formal retrieval")
        if "/" not in embedding_model and embedding_model.startswith("bge-"):
            embedding_model = f"ollama/{embedding_model}"
        retriever = TFDADrugSafetyRetriever(embedding_model=embedding_model)
        retriever._ensure_store()
        return retriever
    except Exception:
        from tfda_context_gate.rag import FixtureRetriever

        return FixtureRetriever()


def _build_formal_generator():
    from tfda_context_gate.c_generator.workflow_adapter import LangChainCV2Generator
    from tfda_context_gate.c_generator.schemas import EvidenceAwareV2Answer
    from tfda_context_gate.run_config import env_value

    model = env_value("ROUTER_LLM_MODEL", "") or ""
    if not model:
        raise RuntimeError("ROUTER_LLM_MODEL is required for formal generator; set it in .env or use deterministic fallback")
    base_url = env_value("OPENCODE_BASE_URL") or env_value("OPENAI_BASE_URL")
    api_key = env_value("OPENCODE_API_KEY") or env_value("OPENAI_API_KEY")
    bare = model.split("/", 1)[-1] if "/" in model else model
    kwargs: dict[str, object] = {"model": bare, "temperature": 0}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    if "mimo" in model.lower():
        kwargs["extra_body"] = {"reasoning": {"effort": "none"}}
        kwargs["reasoning_effort"] = "none"
    # Keep C's transport timeout at the client boundary.  It is independently
    # configurable and defaults to the interpreter timeout when present; the
    # outer 45/120s workflow boundaries are not used as HTTP timeouts.
    timeout_raw = (
        env_value("C_GENERATOR_LLM_TIMEOUT_S", "")
        or env_value("FORMAL_C_LLM_TIMEOUT_S", "")
        or env_value("CONVERSATION_LLM_TIMEOUT_S", "")
        or "25"
    )
    try:
        c_timeout = max(0.1, float(timeout_raw))
    except (TypeError, ValueError):
        c_timeout = 25.0
    kwargs["timeout"] = c_timeout
    # request_timeout is accepted by older langchain-openai versions and is
    # harmlessly ignored by newer versions that alias it to timeout.
    kwargs["request_timeout"] = c_timeout
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("formal C requires langchain-openai") from exc
    llm = ChatOpenAI(**kwargs)
    try:
        chain = llm.with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True, tool_choice="required")  # type: ignore[call-arg]
    except TypeError:
        try:
            chain = llm.with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True, strict=True)  # type: ignore[call-arg]
        except TypeError:
            chain = llm.with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True)
    return LangChainCV2Generator(chain, llm=llm)


# Public aliases for external import
build_formal_extractor = _build_formal_extractor
build_formal_retriever = _build_formal_retriever
build_formal_generator = _build_formal_generator

__all__ = [
    "_build_formal_extractor",
    "_build_formal_retriever",
    "_build_formal_generator",
    "build_formal_extractor",
    "build_formal_retriever",
    "build_formal_generator",
]
