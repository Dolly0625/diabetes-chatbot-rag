"""Native OpenRouter model factory for the Agent v0.1 demo.

【繁中註解｜OpenRouter 模型工廠】
- 僅供 Agent v0.1 的 Planner / Rewriter 使用，不參與 A/B/C/D 執行；圖仍保有唯一回環與有界控制。
- 預設模型 AGENT_MODEL=deepseek/deepseek-v4-flash-0731，透過 ChatOpenRouter 原生介面呼叫。
- 惰性匯入 langchain_openrouter，離線測試無需安裝；無 provider fallback，缺 OPENROUTER_API_KEY 直接拋錯。
- 推理參數：temperature=0、reasoning={"effort":"none"}（僅需有界結構化動作，無需長思考）、max_tokens 預設 400。
"""

from __future__ import annotations

from typing import Any

from tfda_context_gate.run_config import env_value


AGENT_MODEL = "deepseek/deepseek-v4-flash-0731"


def build_agent_openrouter_llm() -> Any:
    """Build native ``langchain_openrouter.ChatOpenRouter`` for DeepSeek.

    The import is lazy so offline tests do not require the optional provider
    package. There is intentionally no provider fallback here.
    """

    try:
        from langchain_openrouter import ChatOpenRouter
    except ImportError as exc:
        raise RuntimeError(
            "Real Agent mode requires langchain-openrouter; install "
            "tfda_context_gate/requirements.txt"
        ) from exc

    api_key = env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY in .env or environment")
    endpoint = env_value("base_url", "https://openrouter.ai/api/v1")
    timeout_seconds = float(env_value("AGENT_REQUEST_TIMEOUT", "60"))
    max_tokens = int(env_value("AGENT_MAX_TOKENS", "400"))
    return ChatOpenRouter(
        model=AGENT_MODEL,
        api_key=api_key,
        base_url=endpoint,
        # langchain-openrouter 0.1.0 currently passes its default app_title as
        # `x_title`, while the companion OpenRouter SDK expects
        # `x_open_router_title`; omit optional attribution to stay compatible.
        app_url=None,
        app_title=None,
        temperature=0,
        # DeepSeek reasoning models can spend a long time in hidden thinking
        # before returning a small Planner decision.  v0.1 only needs a
        # bounded structured action, so explicitly disable reasoning.
        reasoning={"effort": "none"},
        request_timeout=int(timeout_seconds * 1000),
        max_retries=0,
        max_tokens=max_tokens,
    )
