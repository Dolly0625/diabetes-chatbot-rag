"""Native Ollama model factory for the local Agent v0.1 demo."""

from __future__ import annotations

from typing import Any

from tfda_context_gate.run_config import env_value


OLLAMA_AGENT_MODEL = "qwen3:1.7b"


def build_agent_ollama_llm() -> Any:
    """Build native ``langchain_ollama.ChatOllama`` for the local model."""

    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise RuntimeError(
            "Local Agent mode requires langchain-ollama; install "
            "tfda_context_gate/requirements.txt"
        ) from exc

    endpoint = env_value("OLLAMA_BASE_URL", "http://localhost:11434")
    num_predict = int(env_value("OLLAMA_NUM_PREDICT", "128"))
    timeout_seconds = float(env_value("AGENT_REQUEST_TIMEOUT", "60"))
    return ChatOllama(
        model=OLLAMA_AGENT_MODEL,
        base_url=endpoint,
        temperature=0,
        reasoning=False,
        num_predict=num_predict,
        sync_client_kwargs={"timeout": timeout_seconds},
    )
