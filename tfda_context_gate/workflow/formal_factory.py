from __future__ import annotations

"""Formal factory — 從 runner 抽 _build_formal_extractor/retriever/generator。

純搬運，不改邏輯，僅瘦身 runner.py。
"""


def _build_formal_extractor():
    from tfda_context_gate.a_router.router import LangChainSignalExtractor

    return LangChainSignalExtractor.from_env()


def _build_formal_retriever():
    try:
        from tfda_context_gate.rag.tfda_retriever import TFDADrugSafetyRetriever

        retriever = TFDADrugSafetyRetriever(embedding_model="ollama/bge-m3:latest")
        retriever._ensure_store()
        return retriever
    except Exception:
        from tfda_context_gate.rag import FixtureRetriever

        return FixtureRetriever()


def _build_formal_generator():
    from tfda_context_gate.c_generator.workflow_adapter import LangChainCV2Generator
    from tfda_context_gate.c_generator.schemas import EvidenceAwareV2Answer
    from tfda_context_gate.run_config import env_value

    model = env_value("ROUTER_LLM_MODEL", "opencode/mimo-v2.5") or "opencode/mimo-v2.5"
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
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("formal C requires langchain-openai") from exc
    llm = ChatOpenAI(**kwargs)
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
