"""
tfda_context_gate.c_generator.generator — C 層 v1 生成器（實驗用三方法對照）

【本檔定位】
- 這是 v1 生成器（非 v2 workflow 正規路徑）；v2 正規路徑見 workflow_adapter.py。
- v1 三方法對照：
  baseline＝直接回答，不要求引用；
  grounded＝逐項對照文件，僅陳述文件能支持的內容；
  evidence_aware＝結構化輸出 EvidenceAwareAnswer（decision 僅 2 態 ANSWER/INSUFFICIENT，無 PARTIAL）。
- v1 vs v2 核心差異：v1 無 PARTIAL、無 unsupported_requests、claims 型別為 EvidenceClaim；
  v2 才有 PARTIAL 與 V2SupportedClaim/V2UnsupportedRequest。

【呼叫流程】
build_llm → build_chains → run_generators → invoke_one（逐 case×逐 method）
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter

from tfda_context_gate.rate_limiter import (
    RateLimitInvocationError,
    RollingRequestRateLimiter,
    invoke_with_rate_limit,
)
from tfda_context_gate.run_config import PROJECT_ROOT, env_value
from tfda_context_gate.c_generator.schemas import EvidenceAwareAnswer
from tfda_context_gate.c_generator.prompts import (
    BASELINE_SYSTEM,
    EVIDENCE_AWARE_SYSTEM,
    GROUNDED_SYSTEM,
    generator_user_prompt,
)


METHODS = ("baseline", "grounded", "evidence_aware")  # v1 三方法：baseline / grounded / evidence_aware（v2 另見 workflow_adapter）


def content_to_text(content: Any) -> str:
    """將 LLM 回傳的 content 轉為純文字（相容字串／區塊陣列／物件）。"""
    if isinstance(content, str):  # 已是字串 → 直接回傳
        return content
    if isinstance(content, list):  # 區塊陣列 → 逐塊抽 text/content
        parts = []
        for block in content:  # 遍歷每個區塊
            if isinstance(block, dict):  # dict 區塊 → 取 text 或 content 欄
                parts.append(str(block.get("text", block.get("content", ""))))
            else:  # 非 dict → 轉字串
                parts.append(str(block))
        return "".join(parts)  # 串接所有區塊文字
    return str(content)  # 其他型別 → 轉字串


def extract_usage(message: Any) -> dict[str, int] | None:
    """從回應物件抽取 token 使用量（相容 usage_metadata 與 response_metadata 兩種位置）。"""
    usage = getattr(message, "usage_metadata", None)  # 先嘗試 usage_metadata
    if isinstance(usage, dict):  # 若為 dict 則抽三欄
        values = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        if any(value is not None for value in values.values()):  # 至少一欄有值才回傳
            return {key: int(value) for key, value in values.items() if value is not None}
    metadata = getattr(message, "response_metadata", {}) or {}  # 再嘗試 response_metadata
    usage = metadata.get("token_usage") or metadata.get("usage")  # 相容兩種鍵名
    if isinstance(usage, dict):  # 若為 dict 則抽三欄（鍵名可能不同）
        values = {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            "total_tokens": usage.get("total_tokens"),
        }
        if any(value is not None for value in values.values()):  # 至少一欄有值才回傳
            return {key: int(value) for key, value in values.items() if value is not None}
    return None  # 無可用 usage 資訊


def build_llm(
    max_tokens_override: int | None = None,
    reasoning_override: str | None = None,
) -> tuple[ChatOpenRouter, str, dict[str, Any]]:
    """建立 OpenRouter LLM 實例（v1 三方法共用同一個 LLM）。"""
    api_key = env_value("OPENROUTER_API_KEY")  # 讀取 API 金鑰
    if not api_key:  # 缺金鑰 → 明確報錯
        raise RuntimeError("Missing OPENROUTER_API_KEY in .env or environment")
    model = env_value("GENERATOR_MODEL", env_value("JUDGE_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"))  # 模型優先 GENERATOR_MODEL，退回 JUDGE_MODEL
    endpoint = env_value("base_url", "https://openrouter.ai/api/v1")  # API 端點
    timeout_seconds = float(env_value("GENERATOR_REQUEST_TIMEOUT", env_value("JUDGE_REQUEST_TIMEOUT", "60")))  # 請求逾時（秒）
    max_tokens = max_tokens_override or int(env_value("GENERATOR_MAX_TOKENS", "3584"))  # 最大輸出 token
    reasoning_effort = reasoning_override or env_value("GENERATOR_REASONING_EFFORT", env_value("JUDGE_REASONING_EFFORT", "low"))  # 推理強度
    # User-facing configuration is in seconds. langchain-openrouter's timeout
    # field is milliseconds, so convert exactly once at the SDK boundary.
    llm = ChatOpenRouter(
        model=model,
        api_key=api_key,
        base_url=endpoint,
        temperature=0,  # 固定 0 以確保可重現
        timeout=int(timeout_seconds * 1000),  # 秒轉毫秒（僅在此邊界轉一次）
        max_retries=0,  # 重試由外部 rate_limiter 控制
        max_tokens=max_tokens,
        reasoning={"effort": reasoning_effort},
    )
    config = {
        "model": model,
        "base_url": endpoint,
        "temperature": 0,
        "request_timeout_seconds": timeout_seconds,
        "sdk_timeout_milliseconds": int(timeout_seconds * 1000),
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
    }
    return llm, endpoint, config  # 回傳 LLM、端點、設定快照


def build_chains(llm: ChatOpenRouter):
    """為 v1 三方法建立對應的 chain。

    - baseline / grounded：直接使用 LLM（純文字輸出）
    - evidence_aware：包一層 with_structured_output，輸出 EvidenceAwareAnswer（v1 2 態契約）
    """
    structured = llm.with_structured_output(
        EvidenceAwareAnswer,  # v1 契約（僅 ANSWER/INSUFFICIENT）
        method="json_schema",
        strict=True,
        include_raw=True,  # 同時保留 raw 回應以抽 usage 與 raw_content
    )
    return {"baseline": llm, "grounded": llm, "evidence_aware": structured}  # 三方法對應的 chain


def invoke_one(
    chain: Any,
    method: str,
    case: dict,
    limiter: RollingRequestRateLimiter,
) -> dict:
    """執行單一 case×單一 method 的生成（v1 流程核心）。"""
    system_prompt = {
        "baseline": BASELINE_SYSTEM,  # v1 baseline 系統提示
        "grounded": GROUNDED_SYSTEM,  # v1 grounded 系統提示
        "evidence_aware": EVIDENCE_AWARE_SYSTEM,  # v1 evidence_aware 系統提示（2 態）
    }[method]
    user_prompt = generator_user_prompt(case, method)  # 組 user prompt（含 context 與 approved IDs）
    label = f"c.generator.{method}.{case['case_id']}"  # 限流標籤
    started = time.perf_counter()  # 記錄起始時間
    try:
        response, timing = invoke_with_rate_limit(
            lambda: chain.invoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            ),
            limiter,
            label,
        )
        if method == "evidence_aware":  # v1 結構化分支
            if not isinstance(response, dict) or response.get("parsed") is None:  # 解析失敗 → 報錯
                raise RuntimeError(f"Evidence-aware structured parsing failed: {response!r}")
            parsed = response["parsed"]  # 已驗證的 EvidenceAwareAnswer 物件
            raw_message = response.get("raw")  # 原始 LLM 回應
            output = parsed.model_dump(mode="json")  # 轉為 JSON dict 存檔
            raw_content = content_to_text(getattr(raw_message, "content", ""))  # 抽原始文字
            usage = extract_usage(raw_message)  # 抽 token 用量
            response_metadata = getattr(raw_message, "response_metadata", {}) or {}
        else:  # baseline / grounded 純文字分支
            raw_message = response
            raw_content = content_to_text(getattr(response, "content", response))  # 抽文字
            output = raw_content  # 純文字即輸出
            usage = extract_usage(response)  # 抽 token 用量
            response_metadata = getattr(response, "response_metadata", {}) or {}
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "case_id": case["case_id"],
            "case_type": case["case_type"],
            "method": method,
            "query": case["query"],
            "b_decision": case["b_decision"],
            "approved_document_ids": case["approved_document_ids"],
            "output": output,
            "raw_content": raw_content,
            "usage": usage,
            "response_metadata_keys": sorted(response_metadata.keys()) if isinstance(response_metadata, dict) else [],
            "timing": timing,
            "error": None,
        }
    except Exception as error:  # 捕捉所有異常，轉為結構化錯誤回傳
        timing = getattr(error, "timing", None) or {
            "model_latency": 0.0,
            "rate_limit_wait_time": 0.0,
            "retry_wait_time": 0.0,
            "total_wall_time": time.perf_counter() - started,
            "retry_count": 0,
        }
        if isinstance(error, RateLimitInvocationError):  # 限流錯誤 → 取其 timing
            timing = error.timing
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "case_id": case["case_id"],
            "case_type": case["case_type"],
            "method": method,
            "query": case["query"],
            "b_decision": case["b_decision"],
            "approved_document_ids": case["approved_document_ids"],
            "output": None,
            "raw_content": None,
            "usage": None,
            "response_metadata_keys": [],
            "timing": timing,
            "error": repr(error),
        }


def run_generators(
    cases: list[dict],
    results_path: Path,
    limiter: RollingRequestRateLimiter,
    smoke_only: bool = False,
) -> list[dict]:
    """批次執行 v1 三方法生成（逐 case × 逐 method，逐行寫檔）。"""
    llm, endpoint, config = build_llm()  # 建立共用 LLM
    chains = build_chains(llm)  # 建立三方法 chains
    selected = cases[:1] if smoke_only else cases  # smoke 模式僅跑第一個 case
    results: list[dict] = []
    results_path.parent.mkdir(parents=True, exist_ok=True)  # 確保輸出目錄存在
    with results_path.open("a", encoding="utf-8") as handle:  # 追加寫入（JSONL）
        for case in selected:  # 遍歷每個 case
            for method in METHODS:  # 每個 case 跑三種方法
                print(f"generator case={case['case_id']} method={method}", flush=True)
                result = invoke_one(chains[method], method, case, limiter)  # 執行單次生成
                result["model_config"] = config  # 附上模型設定快照
                result["endpoint_configured"] = endpoint  # 附上端點
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")  # 寫入一行 JSON
                handle.flush()  # 立即落盤
                results.append(result)
    return results
