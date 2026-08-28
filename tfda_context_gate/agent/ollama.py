"""Native Ollama model factory for the local Agent v0.1 demo.

【繁中註解｜Ollama 本地模型工廠】
- 僅供本地 Agent v0.1 的 Planner / Rewriter 使用，對應 OpenRouter 的離線替代；圖的有界控制與唯一回環不變。
- 預設模型由 env 決定（OLLAMA_AGENT_MODEL > ROUTER_LLM_MODEL > opencode/mimo-v2.5），透過 ChatOllama 原生介面呼叫，預設端點 http://localhost:11434。
- 惰性匯入 langchain_ollama，離線測試無需安裝；參數 temperature=0、reasoning=False、num_predict 預設 128。
- 與 OpenRouter 工廠一致：僅提供 LLM 實例，實際三選一決策仍由 planner.py 的 LangChainAgentPlanner 封裝與校驗。
"""

from __future__ import annotations

from typing import Any

from tfda_context_gate.run_config import env_value


OLLAMA_AGENT_MODEL = env_value("OLLAMA_AGENT_MODEL", env_value("ROUTER_LLM_MODEL", "opencode/mimo-v2.5") or "opencode/mimo-v2.5") or "opencode/mimo-v2.5"


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
    num_predict = int(env_value("OLLAMA_NUM_PREDICT", "128") or "128")
    timeout_seconds = float(env_value("AGENT_REQUEST_TIMEOUT", "60") or "60")
    model = env_value("OLLAMA_AGENT_MODEL", env_value("ROUTER_LLM_MODEL", "opencode/mimo-v2.5") or "opencode/mimo-v2.5") or OLLAMA_AGENT_MODEL or "opencode/mimo-v2.5"
    return ChatOllama(
        model=model,
        base_url=endpoint,
        temperature=0,
        reasoning=False,
        num_predict=num_predict,
        sync_client_kwargs={"timeout": timeout_seconds},
    )
