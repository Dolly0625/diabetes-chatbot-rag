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


METHODS = ("baseline", "grounded", "evidence_aware")


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", block.get("content", ""))))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def extract_usage(message: Any) -> dict[str, int] | None:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        values = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        if any(value is not None for value in values.values()):
            return {key: int(value) for key, value in values.items() if value is not None}
    metadata = getattr(message, "response_metadata", {}) or {}
    usage = metadata.get("token_usage") or metadata.get("usage")
    if isinstance(usage, dict):
        values = {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            "total_tokens": usage.get("total_tokens"),
        }
        if any(value is not None for value in values.values()):
            return {key: int(value) for key, value in values.items() if value is not None}
    return None


def build_llm(
    max_tokens_override: int | None = None,
    reasoning_override: str | None = None,
) -> tuple[ChatOpenRouter, str, dict[str, Any]]:
    api_key = env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY in .env or environment")
    model = env_value("GENERATOR_MODEL", env_value("JUDGE_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"))
    endpoint = env_value("base_url", "https://openrouter.ai/api/v1")
    timeout_seconds = float(env_value("GENERATOR_REQUEST_TIMEOUT", env_value("JUDGE_REQUEST_TIMEOUT", "60")))
    max_tokens = max_tokens_override or int(env_value("GENERATOR_MAX_TOKENS", "3584"))
    reasoning_effort = reasoning_override or env_value("GENERATOR_REASONING_EFFORT", env_value("JUDGE_REASONING_EFFORT", "low"))
    # User-facing configuration is in seconds. langchain-openrouter's timeout
    # field is milliseconds, so convert exactly once at the SDK boundary.
    llm = ChatOpenRouter(
        model=model,
        api_key=api_key,
        base_url=endpoint,
        temperature=0,
        timeout=int(timeout_seconds * 1000),
        max_retries=0,
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
    return llm, endpoint, config


def build_chains(llm: ChatOpenRouter):
    structured = llm.with_structured_output(
        EvidenceAwareAnswer,
        method="json_schema",
        strict=True,
        include_raw=True,
    )
    return {"baseline": llm, "grounded": llm, "evidence_aware": structured}


def invoke_one(
    chain: Any,
    method: str,
    case: dict,
    limiter: RollingRequestRateLimiter,
) -> dict:
    system_prompt = {
        "baseline": BASELINE_SYSTEM,
        "grounded": GROUNDED_SYSTEM,
        "evidence_aware": EVIDENCE_AWARE_SYSTEM,
    }[method]
    user_prompt = generator_user_prompt(case, method)
    label = f"c.generator.{method}.{case['case_id']}"
    started = time.perf_counter()
    try:
        response, timing = invoke_with_rate_limit(
            lambda: chain.invoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            ),
            limiter,
            label,
        )
        if method == "evidence_aware":
            if not isinstance(response, dict) or response.get("parsed") is None:
                raise RuntimeError(f"Evidence-aware structured parsing failed: {response!r}")
            parsed = response["parsed"]
            raw_message = response.get("raw")
            output = parsed.model_dump(mode="json")
            raw_content = content_to_text(getattr(raw_message, "content", ""))
            usage = extract_usage(raw_message)
            response_metadata = getattr(raw_message, "response_metadata", {}) or {}
        else:
            raw_message = response
            raw_content = content_to_text(getattr(response, "content", response))
            output = raw_content
            usage = extract_usage(response)
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
    except Exception as error:
        timing = getattr(error, "timing", None) or {
            "model_latency": 0.0,
            "rate_limit_wait_time": 0.0,
            "retry_wait_time": 0.0,
            "total_wall_time": time.perf_counter() - started,
            "retry_count": 0,
        }
        if isinstance(error, RateLimitInvocationError):
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
    llm, endpoint, config = build_llm()
    chains = build_chains(llm)
    selected = cases[:1] if smoke_only else cases
    results: list[dict] = []
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a", encoding="utf-8") as handle:
        for case in selected:
            for method in METHODS:
                print(f"generator case={case['case_id']} method={method}", flush=True)
                result = invoke_one(chains[method], method, case, limiter)
                result["model_config"] = config
                result["endpoint_configured"] = endpoint
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                results.append(result)
    return results
