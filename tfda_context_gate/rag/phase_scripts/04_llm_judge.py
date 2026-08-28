from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from pathlib import Path
from typing import Literal
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel, Field

from rate_limiter import (
    RateLimitInvocationError,
    RollingRequestRateLimiter,
    invoke_with_rate_limit,
)
from run_config import (
    PROJECT_ROOT,
    PROCESSED_DIR,
    REPORT_DIR,
    RESULTS_DIR,
    RUN_DIR,
    env_value,
    ensure_run_dirs,
    relative_to_run,
)

ROOT = Path(__file__).resolve().parent
DOCS_PATH = PROCESSED_DIR / "langchain_documents.json"
PHASE3_PATH = RESULTS_DIR / "narrow_query_reranked_top10.json"
DOCUMENT_PROMPT_PATH = ROOT / "prompts" / "document_judge_v1.txt"
SET_PROMPT_PATH = ROOT / "prompts" / "set_judge_v1.txt"

QUERY = "TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？"
MODEL = env_value("JUDGE_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
OPENROUTER_ENDPOINT = env_value("base_url", "https://openrouter.ai/api/v1")
TEMPERATURE = 0
REPEAT_COUNT = 3
REQUEST_TIMEOUT_SECONDS = float(env_value("JUDGE_REQUEST_TIMEOUT", "60"))
JUDGE_MAX_TOKENS = int(env_value("JUDGE_MAX_TOKENS", "2048"))
JUDGE_REASONING_EFFORT = env_value("JUDGE_REASONING_EFFORT", "low")
RATE_LIMIT_STATE_PATH = PROJECT_ROOT / ".openrouter_rate_limit_state.json"
RATE_LIMIT_EVENT_PATH = RESULTS_DIR / "rate_limit_events.jsonl"

DIRECT = "DIRECT"
PARTIAL = "PARTIAL"
IRRELEVANT = "IRRELEVANT"
SUFFICIENT = "SUFFICIENT"
INSUFFICIENT = "INSUFFICIENT"
EXACT = "EXACT"
SAME_DRUG_DIFFERENT_RISK = "SAME_DRUG_DIFFERENT_RISK"
OTHER = "OTHER"


class DocumentAssessment(BaseModel):
    document_id: str = Field(description="The exact document_id supplied in the context")
    relevance: Literal["DIRECT", "PARTIAL", "IRRELEVANT"]
    sufficiency: Literal["SUFFICIENT", "INSUFFICIENT"]
    topic_match: Literal["EXACT", "SAME_DRUG_DIFFERENT_RISK", "OTHER"]
    reason_code: str = Field(description="One short reason code, not an explanation")


class ContextSetAssessment(BaseModel):
    sufficient_for_answer: bool
    usable_document_ids: list[str]
    excluded_document_ids: list[str]
    decision: Literal["PASS", "REVIEW", "FALLBACK"]
    reason_codes: list[str]


def build_llm() -> tuple[ChatOpenRouter, str]:
    api_key = env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY in .env or environment")
    llm = ChatOpenRouter(
        model=MODEL,
        api_key=api_key,
        base_url=OPENROUTER_ENDPOINT,
        temperature=TEMPERATURE,
        timeout=int(REQUEST_TIMEOUT_SECONDS * 1000),
        max_retries=0,
        max_tokens=JUDGE_MAX_TOKENS,
        reasoning={"effort": JUDGE_REASONING_EFFORT},
    )
    return llm, OPENROUTER_ENDPOINT


def load_phase3_input() -> tuple[dict, dict[str, dict]]:
    phase3 = json.loads(PHASE3_PATH.read_text(encoding="utf-8"))
    serialized = json.loads(DOCS_PATH.read_text(encoding="utf-8"))
    raw_by_id = {item["id"]: item for item in serialized}
    for row in phase3["results"]:
        if row["document_id"] not in raw_by_id:
            raise RuntimeError(f"Phase 3 document missing from corpus: {row['document_id']}")
    return phase3, raw_by_id


def human_label(document_id: str) -> str:
    labels = {
        "tfda-risk-0019": DIRECT,
        "tfda-risk-0042": PARTIAL,
        "tfda-risk-0064": PARTIAL,
        "tfda-risk-0035": PARTIAL,
        "tfda-risk-0015": IRRELEVANT,
        "tfda-risk-0102": IRRELEVANT,
        "tfda-risk-0068": IRRELEVANT,
        "tfda-risk-0112": IRRELEVANT,
        "tfda-risk-0020": IRRELEVANT,
        "tfda-risk-0053": IRRELEVANT,
    }
    return labels[document_id]


def document_context(row: dict, raw_by_id: dict[str, dict]) -> str:
    raw = raw_by_id[row["document_id"]]
    return (
        f"document_id: {row['document_id']}\n"
        f"row_index: {row['row_index']}\n"
        f"發布日期: {row['發布日期']}\n"
        f"藥品成分: {row['藥品成分']}\n"
        f"page_content:\n{raw['page_content']}"
    )


def set_context(rows: list[dict], raw_by_id: dict[str, dict]) -> str:
    blocks = []
    for row in rows:
        blocks.append(document_context(row, raw_by_id))
    return "\n\n--- DOCUMENT SEPARATOR ---\n\n".join(blocks)


def extract_usage(raw_message) -> dict[str, int] | None:
    usage = getattr(raw_message, "usage_metadata", None)
    if isinstance(usage, dict):
        keys = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        if any(value is not None for value in keys.values()):
            return {key: int(value) for key, value in keys.items() if value is not None}
    response_metadata = getattr(raw_message, "response_metadata", {}) or {}
    token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
    if isinstance(token_usage, dict):
        mapping = {
            "input_tokens": token_usage.get("prompt_tokens", token_usage.get("input_tokens")),
            "output_tokens": token_usage.get(
                "completion_tokens", token_usage.get("output_tokens")
            ),
            "total_tokens": token_usage.get("total_tokens"),
        }
        if any(value is not None for value in mapping.values()):
            return {key: int(value) for key, value in mapping.items() if value is not None}
    return None


def invoke_structured(
    chain,
    system_prompt: str,
    user_prompt: str,
    limiter: RollingRequestRateLimiter,
    label: str,
):
    response, timing = invoke_with_rate_limit(
        lambda: chain.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]
        ),
        limiter,
        label,
    )
    if not isinstance(response, dict) or response.get("parsed") is None:
        raise RuntimeError(f"Structured output parsing failed: {response!r}")
    parsed = response["parsed"]
    raw_message = response.get("raw")
    return parsed, timing, extract_usage(raw_message)


def document_user_prompt(row: dict, raw_by_id: dict[str, dict]) -> str:
    return (
        f"Query:\n{QUERY}\n\n"
        "Evaluate only this one retrieved document.\n\n"
        f"{document_context(row, raw_by_id)}"
    )


def set_user_prompt(rows: list[dict], raw_by_id: dict[str, dict]) -> str:
    return (
        f"Query:\n{QUERY}\n\n"
        "Evaluate the supplied context set as a whole. Do not answer the query.\n\n"
        f"{set_context(rows, raw_by_id)}"
    )


def assessment_to_dict(assessment: BaseModel) -> dict:
    return assessment.model_dump(mode="json")


def run_one_round(
    document_chain,
    set_chain,
    rows: list[dict],
    raw_by_id: dict[str, dict],
    document_prompt: str,
    set_prompt: str,
    run_index: int,
    limiter: RollingRequestRateLimiter,
) -> dict:
    document_results = []
    document_latencies = []
    document_usage = []
    for row in rows:
        print(
            f"run={run_index} document_judge={row['document_id']}",
            flush=True,
        )
        assessment, timing, usage = invoke_structured(
            document_chain,
            document_prompt,
            document_user_prompt(row, raw_by_id),
            limiter,
            f"phase4.run{run_index}.document.{row['document_id']}",
        )
        item = {
            "reranker_rank": row["reranker_rank"],
            "document_id": row["document_id"],
            "human_label": human_label(row["document_id"]),
            "judge": assessment_to_dict(assessment),
        }
        document_results.append(item)
        document_latencies.append(timing)
        document_usage.append(usage)

    top4_rows = rows[:4]
    only_wrong_topic_rows = [
        row
        for row in rows
        if row["document_id"] in {"tfda-risk-0064", "tfda-risk-0042", "tfda-risk-0035"}
    ]
    set_inputs = {
        "top4": top4_rows,
        "without_correct_context": only_wrong_topic_rows,
        "with_correct_context": top4_rows,
    }
    set_results = {}
    set_latencies = {}
    set_usage = {}
    for set_name, set_rows in set_inputs.items():
        print(f"run={run_index} set_judge={set_name}", flush=True)
        assessment, timing, usage = invoke_structured(
            set_chain,
            set_prompt,
            set_user_prompt(set_rows, raw_by_id),
            limiter,
            f"phase4.run{run_index}.set.{set_name}",
        )
        set_results[set_name] = {
            "document_ids": [row["document_id"] for row in set_rows],
            "assessment": assessment_to_dict(assessment),
        }
        set_latencies[set_name] = timing
        set_usage[set_name] = usage

    return {
        "run_index": run_index,
        "document_assessments": document_results,
        "set_assessments": set_results,
        "latency_seconds": {
            "document_total": sum(item["total_wall_time"] for item in document_latencies),
            "document_model_latency_total": sum(item["model_latency"] for item in document_latencies),
            "document_rate_limit_wait_total": sum(item["rate_limit_wait_time"] for item in document_latencies),
            "document_retry_wait_total": sum(item["retry_wait_time"] for item in document_latencies),
            "document_each": document_latencies,
            "set_each": set_latencies,
            "set_total": sum(item["total_wall_time"] for item in set_latencies.values()),
            "set_model_latency_total": sum(item["model_latency"] for item in set_latencies.values()),
            "set_rate_limit_wait_total": sum(item["rate_limit_wait_time"] for item in set_latencies.values()),
            "set_retry_wait_total": sum(item["retry_wait_time"] for item in set_latencies.values()),
            "total": sum(item["total_wall_time"] for item in document_latencies)
            + sum(item["total_wall_time"] for item in set_latencies.values()),
            "model_latency_total": sum(item["model_latency"] for item in document_latencies)
            + sum(item["model_latency"] for item in set_latencies.values()),
            "rate_limit_wait_total": sum(item["rate_limit_wait_time"] for item in document_latencies)
            + sum(item["rate_limit_wait_time"] for item in set_latencies.values()),
            "retry_wait_total": sum(item["retry_wait_time"] for item in document_latencies)
            + sum(item["retry_wait_time"] for item in set_latencies.values()),
        },
        "usage": {
            "document_each": document_usage,
            "set_each": set_usage,
        },
    }


def compare_rows(rows: list[dict], reference_run: dict) -> list[dict]:
    judge_by_id = {
        item["document_id"]: item["judge"]
        for item in reference_run["document_assessments"]
    }
    output = []
    for row in rows:
        judge = judge_by_id[row["document_id"]]
        output.append(
            {
                "document_id": row["document_id"],
                "reranker_rank": row["reranker_rank"],
                "human_label": human_label(row["document_id"]),
                "judge_label": judge["relevance"],
                "match": human_label(row["document_id"]) == judge["relevance"],
                "judge_sufficiency": judge["sufficiency"],
                "judge_topic_match": judge["topic_match"],
                "reason_code": judge["reason_code"],
            }
        )
    return output


def confusion_matrix(comparisons: list[dict]) -> list[dict]:
    labels = [DIRECT, PARTIAL, IRRELEVANT]
    counts = {(human, judge): 0 for human in labels for judge in labels}
    for row in comparisons:
        counts[(row["human_label"], row["judge_label"])] += 1
    return [
        {
            "human_label": human,
            **{f"judge_{judge}": counts[(human, judge)] for judge in labels},
        }
        for human in labels
    ]


def aggregate_usage(runs: list[dict]) -> dict:
    values = []
    for run in runs:
        for usage in run["usage"]["document_each"]:
            if usage:
                values.append(usage)
        for usage in run["usage"]["set_each"].values():
            if usage:
                values.append(usage)
    if not values:
        return {"available": False, "note": "Provider did not return token usage metadata."}
    totals = {}
    for key in ["input_tokens", "output_tokens", "total_tokens"]:
        present = [item[key] for item in values if key in item]
        if present:
            totals[key] = sum(present)
    return {"available": True, "aggregate_over_all_calls": totals}


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_set_file(path: Path, phase3: dict, runs: list[dict], set_name: str) -> None:
    payload = {
        "query": QUERY,
        "source_phase3_file": relative_to_run(PHASE3_PATH),
        "set_name": set_name,
        "runs": [
            {
                "run_index": run["run_index"],
                "document_ids": run["set_assessments"][set_name]["document_ids"],
                "assessment": run["set_assessments"][set_name]["assessment"],
                "latency_seconds": run["latency_seconds"]["set_each"][set_name],
            }
            for run in runs
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_output_text(
    phase3: dict,
    runs: list[dict],
    comparisons: list[dict],
    matrix: list[dict],
    latency: dict,
    model_base_url: str,
) -> str:
    lines = [
        f"Model: {MODEL}",
        f"Endpoint root: {model_base_url}",
        "Endpoint type: OpenAI-compatible Chat Completions",
        "LangChain package: langchain-openrouter",
        f"Temperature: {TEMPERATURE}",
        f"Request timeout seconds: {REQUEST_TIMEOUT_SECONDS}",
        f"Phase 3 input: {PHASE3_PATH.name}",
        f"Query: {QUERY}",
        f"Document-level calls per run: {len(phase3['results'])}",
        "",
        "HUMAN VS JUDGE",
    ]
    for row in comparisons:
        lines.append(
            f"{row['reranker_rank']}. {row['document_id']} | "
            f"human={row['human_label']} | judge={row['judge_label']} | "
            f"match={row['match']} | sufficiency={row['judge_sufficiency']} | "
            f"topic_match={row['judge_topic_match']} | reason={row['reason_code']}"
        )
    lines.extend(["", "CONFUSION MATRIX"])
    for row in matrix:
        lines.append(json.dumps(row, ensure_ascii=False))
    lines.extend(["", "SET-LEVEL RESULTS"])
    for run in runs:
        lines.append(f"Run {run['run_index']}")
        for set_name, result in run["set_assessments"].items():
            lines.append(
                f"{set_name}: ids={result['document_ids']} | "
                f"{json.dumps(result['assessment'], ensure_ascii=False)}"
            )
    lines.extend(
        [
            "",
            "LATENCY",
            json.dumps(latency, ensure_ascii=False, indent=2),
        ]
    )
    return "\n".join(lines) + "\n"


def smoke_test() -> None:
    ensure_run_dirs()
    phase3, raw_by_id = load_phase3_input()
    llm, base_url = build_llm()
    limiter = RollingRequestRateLimiter(RATE_LIMIT_STATE_PATH, RATE_LIMIT_EVENT_PATH)
    document_prompt = DOCUMENT_PROMPT_PATH.read_text(encoding="utf-8")
    document_chain = llm.with_structured_output(
        DocumentAssessment, method="json_schema", strict=True, include_raw=True
    )
    assessment, timing, usage = invoke_structured(
        document_chain,
        document_prompt,
        document_user_prompt(phase3["results"][0], raw_by_id),
        limiter,
        "phase4.smoke_test.document",
    )
    print(
        json.dumps(
            {
                "model": MODEL,
                "endpoint_root": base_url,
                "document_id": phase3["results"][0]["document_id"],
                "assessment": assessment_to_dict(assessment),
                "timing": timing,
                "usage": usage,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    ensure_run_dirs()
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    if args.smoke_test:
        smoke_test()
        return

    phase3, raw_by_id = load_phase3_input()
    llm, base_url = build_llm()
    limiter = RollingRequestRateLimiter(RATE_LIMIT_STATE_PATH, RATE_LIMIT_EVENT_PATH)
    document_prompt = DOCUMENT_PROMPT_PATH.read_text(encoding="utf-8")
    set_prompt = SET_PROMPT_PATH.read_text(encoding="utf-8")
    document_chain = llm.with_structured_output(
        DocumentAssessment, method="json_schema", strict=True, include_raw=True
    )
    set_chain = llm.with_structured_output(
        ContextSetAssessment, method="json_schema", strict=True, include_raw=True
    )

    runs = []
    for run_index in range(1, REPEAT_COUNT + 1):
        runs.append(
            run_one_round(
                document_chain,
                set_chain,
                phase3["results"],
                raw_by_id,
                document_prompt,
                set_prompt,
                run_index,
                limiter,
            )
        )

    comparisons = compare_rows(phase3["results"], runs[0])
    matrix = confusion_matrix(comparisons)
    accuracy = sum(row["match"] for row in comparisons) / len(comparisons)
    total_latencies = [run["latency_seconds"]["total"] for run in runs]
    document_latencies = [run["latency_seconds"]["document_total"] for run in runs]
    set_latencies = [run["latency_seconds"]["set_total"] for run in runs]
    latency = {
        "repeat_count": REPEAT_COUNT,
        "document_total_seconds": {
            "values": document_latencies,
            "mean": statistics.mean(document_latencies),
            "median": statistics.median(document_latencies),
            "min": min(document_latencies),
            "max": max(document_latencies),
        },
        "set_total_seconds": {
            "values": set_latencies,
            "mean": statistics.mean(set_latencies),
            "median": statistics.median(set_latencies),
            "min": min(set_latencies),
            "max": max(set_latencies),
        },
        "all_calls_seconds": {
            "values": total_latencies,
            "mean": statistics.mean(total_latencies),
            "median": statistics.median(total_latencies),
            "min": min(total_latencies),
            "max": max(total_latencies),
            "model_latency_values": [run["latency_seconds"]["model_latency_total"] for run in runs],
            "model_latency_mean": statistics.mean(
                run["latency_seconds"]["model_latency_total"] for run in runs
            ),
            "rate_limit_wait_values": [run["latency_seconds"]["rate_limit_wait_total"] for run in runs],
            "retry_wait_values": [run["latency_seconds"]["retry_wait_total"] for run in runs],
        },
        "token_usage": aggregate_usage(runs),
    }

    document_payload = {
        "model": MODEL,
        "endpoint_type": "OpenAI-compatible Chat Completions",
        "langchain_package": "langchain-openrouter",
        "temperature": TEMPERATURE,
        "query": QUERY,
        "source_phase3_file": relative_to_run(PHASE3_PATH),
        "reference_run_for_comparison": 1,
        "human_vs_judge_accuracy_reference_run": accuracy,
        "runs": runs,
    }
    (RESULTS_DIR / "document_judge_results.json").write_text(
        json.dumps(document_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(RESULTS_DIR / "human_vs_judge.csv", comparisons)
    write_csv(RESULTS_DIR / "judge_confusion_matrix.csv", matrix)
    write_set_file(
        RESULTS_DIR / "set_without_correct_context.json",
        phase3,
        runs,
        "without_correct_context",
    )
    write_set_file(
        RESULTS_DIR / "set_with_correct_context.json",
        phase3,
        runs,
        "with_correct_context",
    )
    (RESULTS_DIR / "phase4_latency.json").write_text(
        json.dumps(latency, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    output = build_output_text(
        phase3, runs, comparisons, matrix, latency, base_url
    )
    (RESULTS_DIR / "phase4_llm_judge_output.txt").write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
