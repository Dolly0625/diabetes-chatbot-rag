from __future__ import annotations


# 本模組定義路由器依賴錯誤：所有外部依賴失效的統一異常
# 觸發情境：LLM 超時、結構化輸出格式錯誤、Qwen3Guard 加載/推理失敗、訊號萃取異常等
# 上游捕捉後一律導向 F_ROUTER_DEPENDENCY（fail-closed），rag_allowed 為 False

class RouterDependencyError(RuntimeError):
    """路由器依賴錯誤：LLM 超時、結構化輸出異常或其他依賴失效。
    LLM timeout, malformed structured output, or another dependency error."""

    # 特性：為 RuntimeError 子類，fail-closed 語意；被 route_request 捕捉後轉為 F_ROUTER_DEPENDENCY + 對應 REASON_*
