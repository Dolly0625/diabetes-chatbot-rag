"""Run and evaluate the C v2 partial-answer experiment.

This module deliberately does not call the baseline or grounded generators.  It
reuses the frozen C v1 interface and changes only the Evidence-aware output
schema and decision policy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from tfda_context_gate.c_generator.generator import (
    content_to_text,
    extract_usage,
    build_llm,
)
from tfda_context_gate.c_generator.prompts import (
    AUXILIARY_V2_JUDGE_SYSTEM,
    EVIDENCE_AWARE_V2_SYSTEM,
    evidence_aware_v2_user_prompt,
    evaluation_v2_user_prompt,
)
from tfda_context_gate.c_generator.schemas import (
    EvidenceAwareV2Answer,
    V2AuxiliaryEvaluation,
)
from tfda_context_gate.rate_limiter import (
    RateLimitInvocationError,
    RollingRequestRateLimiter,
    invoke_with_rate_limit,
)
from tfda_context_gate.run_config import (
    PROJECT_ROOT,
    REPORT_DIR,
    RESULTS_DIR,
    env_value,
    ensure_run_dirs,
)


V1_RUN_DEFAULT = PROJECT_ROOT / "tfda_context_gate/runs/c_hard_nemotron_20260819"
SMOKE_CASE_IDS = ("X2", "P1", "I1")
PARTIAL_CASE_TYPES = {"numeric_trap", "partial_guess"}
SAFETY_CASE_TYPES = {"near_match", "temporal", "same_risk_conflict", "hard_insufficient"}
PROMPT_VERSION = "partial_answer_v2_compact_final"


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_frozen_interface() -> tuple[list[dict], Path, str]:
    v1_run = Path(env_value("C_V1_RUN_DIR", str(V1_RUN_DEFAULT)))
    if not v1_run.is_absolute():
        v1_run = PROJECT_ROOT / v1_run
    source = v1_run / "results/interface_cases.json"
    if not source.exists():
        raise FileNotFoundError(f"C v1 interface not found: {source}")
    target = RESULTS_DIR / "interface_cases.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Byte-for-byte copy makes the controlled input auditable.
    shutil.copyfile(source, target)
    cases = json.loads(target.read_text(encoding="utf-8"))
    return cases, source, sha256_file(target)


def new_limiter() -> RollingRequestRateLimiter:
    state_path = PROJECT_ROOT / ".openrouter_rate_limit_state.json"
    event_path = RESULTS_DIR / "c_v2_request_events.jsonl"
    return RollingRequestRateLimiter(state_path=state_path, event_log_path=event_path)


def invoke_v2(chain: Any, case: dict, limiter: RollingRequestRateLimiter) -> dict:
    started = time.perf_counter()
    label = f"c.v2.evidence_aware.{case['case_id']}"
    timing = None
    try:
        response, timing = invoke_with_rate_limit(
            lambda: chain.invoke(
                [
                    SystemMessage(content=EVIDENCE_AWARE_V2_SYSTEM),
                    HumanMessage(content=evidence_aware_v2_user_prompt(case)),
                ]
            ),
            limiter,
            label,
        )
        if not isinstance(response, dict) or response.get("parsed") is None:
            raise RuntimeError(f"v2 structured parsing failed: {response!r}")
        parsed = response["parsed"]
        raw_message = response.get("raw")
        output = parsed.model_dump(mode="json")
        raw_content = content_to_text(getattr(raw_message, "content", ""))
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "case_id": case["case_id"],
            "case_type": case["case_type"],
            "method": "evidence_aware_v2",
            "strategy_version": "partial_answer_v2",
            "prompt_version": PROMPT_VERSION,
            "query": case["query"],
            "b_decision": case["b_decision"],
            "approved_document_ids": case["approved_document_ids"],
            "output": output,
            "raw_content": raw_content,
            "usage": extract_usage(raw_message),
            "response_metadata_keys": sorted(
                (getattr(raw_message, "response_metadata", {}) or {}).keys()
            ),
            "timing": timing,
            "error": None,
        }
    except Exception as error:
        timing = timing or getattr(error, "timing", None) or {
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
            "method": "evidence_aware_v2",
            "strategy_version": "partial_answer_v2",
            "prompt_version": PROMPT_VERSION,
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


def run_generator(cases: list[dict], output_path: Path, smoke_only: bool) -> list[dict]:
    llm, endpoint, config = build_llm()
    chain = llm.with_structured_output(
        EvidenceAwareV2Answer,
        method="json_schema",
        strict=True,
        include_raw=True,
    )
    selected = (
        [case for case in cases if case["case_id"] in SMOKE_CASE_IDS]
        if smoke_only
        else cases
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    limiter = new_limiter()
    with output_path.open("w", encoding="utf-8") as handle:
        for case in selected:
            print(f"v2 generator case={case['case_id']}", flush=True)
            row = invoke_v2(chain, case, limiter)
            row["model_config"] = config
            row["endpoint_configured"] = endpoint
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            rows.append(row)
    return rows


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_auxiliary_judge(cases: list[dict], rows: list[dict], path: Path) -> list[dict]:
    llm, _, config = build_llm(
        max_tokens_override=int(env_value("AUX_JUDGE_MAX_TOKENS", "1024")),
        reasoning_override="none",
    )
    chain = llm.with_structured_output(
        V2AuxiliaryEvaluation,
        method="json_schema",
        strict=True,
        include_raw=True,
    )
    case_by_id = {case["case_id"]: case for case in cases}
    limiter = new_limiter()
    records: list[dict] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if row.get("error"):
                continue
            case = case_by_id[row["case_id"]]
            output = row.get("output") or {}
            label = f"c.v2.aux_judge.{case['case_id']}"
            timing = None
            try:
                response, timing = invoke_with_rate_limit(
                    lambda: chain.invoke(
                        [
                            SystemMessage(content=AUXILIARY_V2_JUDGE_SYSTEM),
                            HumanMessage(
                                content=evaluation_v2_user_prompt(
                                    case, json.dumps(output, ensure_ascii=False)
                                )
                            ),
                        ]
                    ),
                    limiter,
                    label,
                )
                if not isinstance(response, dict) or response.get("parsed") is None:
                    raise RuntimeError("v2_auxiliary_structured_output_parse_failed")
                raw = response.get("raw")
                record = {
                    "case_id": case["case_id"],
                    "case_type": case["case_type"],
                    "method": "evidence_aware_v2",
                    "evaluation_role": "auxiliary_not_ground_truth",
                    "evaluation": response["parsed"].model_dump(mode="json"),
                    "usage": extract_usage(raw),
                    "timing": timing,
                    "model_config": config,
                    "error": None,
                }
            except Exception as error:
                record = {
                    "case_id": case["case_id"],
                    "case_type": case["case_type"],
                    "method": "evidence_aware_v2",
                    "evaluation_role": "auxiliary_not_ground_truth",
                    "evaluation": None,
                    "usage": None,
                    "timing": timing or getattr(error, "timing", None),
                    "model_config": config,
                    "error": repr(error),
                }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            records.append(record)
    return records


def expected_v2_decision(case: dict) -> str:
    if case["case_type"] == "hard_insufficient":
        return "INSUFFICIENT"
    if case["case_type"] in PARTIAL_CASE_TYPES:
        return "PARTIAL"
    return "ANSWER"


def output_protocol_metrics(case: dict, row: dict) -> dict[str, Any]:
    output = row.get("output") if isinstance(row.get("output"), dict) else {}
    claims = [claim for claim in output.get("supported_claims", []) if str(claim.get("claim", "")).strip()]
    approved = set(case["approved_document_ids"])
    cited = [claim for claim in claims if claim.get("evidence_ids")]
    valid = [claim for claim in cited if set(claim.get("evidence_ids", [])) <= approved]
    missing_ids = [claim for claim in claims if not claim.get("evidence_ids")]
    invalid_ids = [
        evidence_id
        for claim in claims
        for evidence_id in (claim.get("evidence_ids") or [])
        if evidence_id not in approved
    ]
    return {
        "citation_accuracy": len(valid) / len(cited) if cited else None,
        "citation_coverage": len(cited) / len(claims) if claims else None,
        "supported_claim_count": len(claims),
        "supported_claim_missing_evidence_id_count": len(missing_ids),
        "evidence_id_boundary_violation_count": len(invalid_ids),
        "unsupported_request_count": len(output.get("unsupported_requests", []) or []),
        "protocol_partial_answer": (
            output.get("decision") == "PARTIAL"
            and bool(claims)
            and bool(output.get("unsupported_requests"))
            and not missing_ids
            and not invalid_ids
        ),
    }


def summarize_v2(cases: list[dict], rows: list[dict], aux: list[dict]) -> tuple[dict, list[dict]]:
    case_by_id = {case["case_id"]: case for case in cases}
    aux_by_id = {record["case_id"]: record for record in aux}
    successful = [row for row in rows if not row.get("error") and isinstance(row.get("output"), dict)]
    manual_correct = []
    partial_protocol = []
    over_refusals = []
    supported_missing = 0
    boundary_violations = 0
    citations_accuracy = []
    citations_coverage = []
    model_latency = []
    wall_latency = []
    token_counts = []
    decision_counts = Counter()
    aux_statuses = Counter()
    aux_partial = []
    aux_insufficient = []
    per_case: list[dict] = []

    for row in successful:
        case = case_by_id[row["case_id"]]
        output = row["output"]
        expected = expected_v2_decision(case)
        actual = output.get("decision")
        decision_counts[actual] += 1
        manual_correct.append(actual == expected)
        if case["case_type"] in PARTIAL_CASE_TYPES:
            partial_protocol.append(output_protocol_metrics(case, row)["protocol_partial_answer"])
        if expected != "INSUFFICIENT":
            over_refusals.append(actual == "INSUFFICIENT")
        metrics = output_protocol_metrics(case, row)
        supported_missing += metrics["supported_claim_missing_evidence_id_count"]
        boundary_violations += metrics["evidence_id_boundary_violation_count"]
        if metrics["citation_accuracy"] is not None:
            citations_accuracy.append(metrics["citation_accuracy"])
        if metrics["citation_coverage"] is not None:
            citations_coverage.append(metrics["citation_coverage"])
        timing = row.get("timing") or {}
        if timing.get("model_latency") is not None:
            model_latency.append(float(timing["model_latency"]))
        if timing.get("total_wall_time") is not None:
            wall_latency.append(float(timing["total_wall_time"]))
        usage = row.get("usage") or {}
        if usage.get("total_tokens") is not None:
            token_counts.append(int(usage["total_tokens"]))
        aux_record = aux_by_id.get(row["case_id"], {})
        evaluation = aux_record.get("evaluation") or {}
        for status, key in (
            ("SUPPORTED", "supported_claim_count"),
            ("PARTIALLY_SUPPORTED", "partially_supported_claim_count"),
            ("UNSUPPORTED", "unsupported_claim_count"),
        ):
            aux_statuses[status] += int(evaluation.get(key, 0))
        if isinstance(evaluation.get("partial_answer_correct"), bool) and case["case_type"] in PARTIAL_CASE_TYPES:
            aux_partial.append(evaluation["partial_answer_correct"])
        if isinstance(evaluation.get("insufficient_handling_correct"), bool) and case["case_type"] == "hard_insufficient":
            aux_insufficient.append(evaluation["insufficient_handling_correct"])
        per_case.append(
            {
                "case_id": case["case_id"],
                "case_type": case["case_type"],
                "expected_v2_decision": expected,
                "actual_decision": actual,
                "manual_decision_correct": actual == expected,
                "protocol_partial_answer": metrics["protocol_partial_answer"],
                "citation_accuracy": metrics["citation_accuracy"],
                "citation_coverage": metrics["citation_coverage"],
                "supported_claim_missing_evidence_id_count": metrics["supported_claim_missing_evidence_id_count"],
                "evidence_id_boundary_violation_count": metrics["evidence_id_boundary_violation_count"],
                "auxiliary_partial_answer_correct": evaluation.get("partial_answer_correct"),
                "auxiliary_over_refusal": evaluation.get("over_refusal"),
            }
        )

    type_metrics: dict[str, dict[str, Any]] = {}
    for case_type in sorted({case["case_type"] for case in cases}):
        type_rows = [row for row in successful if row["case_type"] == case_type]
        type_correct = [row["output"].get("decision") == expected_v2_decision(case_by_id[row["case_id"]]) for row in type_rows]
        type_metrics[case_type] = {
            "n": len(type_rows),
            "manual_decision_accuracy": sum(type_correct) / len(type_correct) if type_correct else None,
        }

    total_claims = sum(aux_statuses.values())
    summary = {
        "run_type": "C v2 partial-answer experiment",
        "ground_truth": "manual_case_specs",
        "auxiliary_judge_is_not_ground_truth": True,
        "case_counts": {case_type: sum(case["case_type"] == case_type for case in cases) for case_type in sorted({case["case_type"] for case in cases})},
        "n_outputs": len(successful),
        "n_errors": len(rows) - len(successful),
        "decision_counts": dict(decision_counts),
        "manual_decision_accuracy": sum(manual_correct) / len(manual_correct) if manual_correct else None,
        "partial_answer_accuracy_protocol": sum(partial_protocol) / len(partial_protocol) if partial_protocol else None,
        "partial_answer_accuracy_auxiliary": sum(aux_partial) / len(aux_partial) if aux_partial else None,
        "over_refusal_rate_all_answerable": sum(over_refusals) / len(over_refusals) if over_refusals else None,
        "over_refusal_count_all_answerable": sum(over_refusals),
        "over_refusal_rate_partial_cases": sum(
            row["actual_decision"] == "INSUFFICIENT"
            for row in per_case
            if row["case_type"] in PARTIAL_CASE_TYPES
        ) / sum(row["case_type"] in PARTIAL_CASE_TYPES for row in per_case) if per_case else None,
        "claim_support_rate_auxiliary": aux_statuses["SUPPORTED"] / total_claims if total_claims else None,
        "claim_partial_rate_auxiliary": aux_statuses["PARTIALLY_SUPPORTED"] / total_claims if total_claims else None,
        "unsupported_claim_rate_auxiliary": aux_statuses["UNSUPPORTED"] / total_claims if total_claims else None,
        "auxiliary_claim_status_counts": dict(aux_statuses),
        "citation_accuracy": statistics.mean(citations_accuracy) if citations_accuracy else None,
        "citation_coverage": statistics.mean(citations_coverage) if citations_coverage else None,
        "supported_claim_missing_evidence_id_count": supported_missing,
        "evidence_id_boundary_violation_count": boundary_violations,
        "insufficient_fallback_accuracy": (
            sum(row["actual_decision"] == "INSUFFICIENT" for row in per_case if row["case_type"] == "hard_insufficient")
            / sum(row["case_type"] == "hard_insufficient" for row in per_case)
            if per_case else None
        ),
        "insufficient_handling_accuracy_auxiliary": sum(aux_insufficient) / len(aux_insufficient) if aux_insufficient else None,
        "model_latency_mean_seconds": statistics.mean(model_latency) if model_latency else None,
        "model_latency_median_seconds": statistics.median(model_latency) if model_latency else None,
        "model_latency_p95_seconds": sorted(model_latency)[min(len(model_latency) - 1, round((len(model_latency) - 1) * 0.95))] if model_latency else None,
        "total_wall_time_mean_seconds": statistics.mean(wall_latency) if wall_latency else None,
        "total_tokens_mean": statistics.mean(token_counts) if token_counts else None,
        "case_type_metrics": type_metrics,
        "safety_case_types": sorted(SAFETY_CASE_TYPES),
        "evaluation_note": "Partial protocol metrics are deterministic structure checks; semantic auxiliary metrics are not Ground Truth.",
    }
    return summary, per_case


def write_metrics(summary: dict, per_case: list[dict]) -> None:
    json_dump(RESULTS_DIR / "v2_evaluation_summary.json", summary)
    fields = sorted({key for row in per_case for key in row}) if per_case else []
    with (RESULTS_DIR / "v2_evaluation_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(per_case)


def write_comparison(v2_summary: dict) -> None:
    v1_summary_path = V1_RUN_DEFAULT / "results/evaluation_summary.json"
    v1_summary = json.loads(v1_summary_path.read_text(encoding="utf-8")) if v1_summary_path.exists() else {}
    v1 = next((item for item in v1_summary.get("methods", []) if item.get("method") == "evidence_aware"), {})
    comparison = {
        "v1_run": str(V1_RUN_DEFAULT),
        "v2_run": str(RESULTS_DIR.parent),
        "same_frozen_interface": True,
        "v1": {
            "manual_decision_accuracy": v1.get("manual_decision_accuracy"),
            "manual_partial_and_insufficient_decision_accuracy": v1.get("manual_partial_and_insufficient_decision_accuracy"),
            "numeric_trap_accuracy": v1.get("case_type_metrics", {}).get("numeric_trap", {}).get("manual_decision_accuracy"),
            "partial_guess_accuracy": v1.get("case_type_metrics", {}).get("partial_guess", {}).get("manual_decision_accuracy"),
            "near_match_accuracy": v1.get("case_type_metrics", {}).get("near_match", {}).get("manual_decision_accuracy"),
            "temporal_accuracy": v1.get("case_type_metrics", {}).get("temporal", {}).get("manual_decision_accuracy"),
            "same_risk_conflict_accuracy": v1.get("case_type_metrics", {}).get("same_risk_conflict", {}).get("manual_decision_accuracy"),
            "hard_insufficient_accuracy": v1.get("case_type_metrics", {}).get("hard_insufficient", {}).get("manual_decision_accuracy"),
            "citation_accuracy": v1.get("citation_accuracy"),
            "citation_coverage": v1.get("citation_coverage"),
            "model_latency_mean_seconds": v1.get("model_latency_mean_seconds"),
        },
        "v2": {
            "manual_decision_accuracy": v2_summary.get("manual_decision_accuracy"),
            "partial_answer_accuracy_protocol": v2_summary.get("partial_answer_accuracy_protocol"),
            "partial_answer_accuracy_auxiliary": v2_summary.get("partial_answer_accuracy_auxiliary"),
            "over_refusal_rate_all_answerable": v2_summary.get("over_refusal_rate_all_answerable"),
            "near_match_accuracy": v2_summary.get("case_type_metrics", {}).get("near_match", {}).get("manual_decision_accuracy"),
            "temporal_accuracy": v2_summary.get("case_type_metrics", {}).get("temporal", {}).get("manual_decision_accuracy"),
            "same_risk_conflict_accuracy": v2_summary.get("case_type_metrics", {}).get("same_risk_conflict", {}).get("manual_decision_accuracy"),
            "hard_insufficient_accuracy": v2_summary.get("case_type_metrics", {}).get("hard_insufficient", {}).get("manual_decision_accuracy"),
            "citation_accuracy": v2_summary.get("citation_accuracy"),
            "citation_coverage": v2_summary.get("citation_coverage"),
            "model_latency_mean_seconds": v2_summary.get("model_latency_mean_seconds"),
        },
    }
    json_dump(RESULTS_DIR / "v1_v2_comparison.json", comparison)
    rows = []
    for metric in sorted(set(comparison["v1"]) | set(comparison["v2"])):
        rows.append({"metric": metric, "v1": comparison["v1"].get(metric), "v2": comparison["v2"].get(metric)})
    with (RESULTS_DIR / "v1_v2_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "v1", "v2"])
        writer.writeheader()
        writer.writerows(rows)


def write_report(summary: dict, source: Path, interface_hash: str) -> None:
    by_type = summary.get("case_type_metrics", {})
    lines = [
        "# C v2：Partial Answer Generator 實驗結果",
        "",
        "這一版只改 Evidence-aware Generator 的判斷方式，沒有重跑或改動 B、Retriever、Reranker、Corpus、Query、Context 或模型。",
        "原本 C v1 的 30 題介面被逐字複製到本 run；v1 結果完整保留在 `c_hard_nemotron_20260819`。",
        "",
        "## 這次改了什麼",
        "",
        "v1 只有 `ANSWER / INSUFFICIENT`，遇到『一半有證據、一半沒證據』時容易整題拒答。v2 改成 `ANSWER / PARTIAL / INSUFFICIENT`，並要求模型把有證據的內容放進 `supported_claims`，把缺少的要求放進 `unsupported_requests`。",
        "",
        "`PARTIAL` 的意思很簡單：有證據的先回答，沒有證據的明講缺口，不要因為缺一半就全部說不知道，也不要自己補猜。",
        "",
        "## 控制條件",
        "",
        f"- 固定輸入：{len(summary.get('case_counts', {})) and sum(summary['case_counts'].values()) or 0} 題 C v1 困難案例。",
        f"- Frozen interface：`{source}`",
        f"- Interface SHA-256：`{interface_hash}`",
        "- 模型、temperature、max tokens、reasoning、timeout 與 B approved evidence IDs 沿用 v1。",
        "- Generator prompt 沒有放人工 Ground Truth；人工 Ground Truth 只在評估階段使用。",
        "- `model_latency` 與 `total_wall_time` 分開記錄；rate-limit wait 不算模型推論延遲。",
        "",
        "## 結果摘要",
        "",
        f"- 成功輸出：{summary.get('n_outputs')}；錯誤：{summary.get('n_errors')}。",
        f"- 人工決策正確率：{summary.get('manual_decision_accuracy')}。",
        f"- Numeric trap + partial_guess 的結構化 Partial Answer Accuracy：{summary.get('partial_answer_accuracy_protocol')}。",
        f"- 同一批 partial 案例的輔助 Judge Partial Answer Accuracy：{summary.get('partial_answer_accuracy_auxiliary')}（僅輔助，不是 Ground Truth）。",
        f"- 可回答案例的 Over-refusal Rate：{summary.get('over_refusal_rate_all_answerable')}。",
        f"- Citation accuracy：{summary.get('citation_accuracy')}；citation coverage：{summary.get('citation_coverage')}。",
        f"- Evidence ID 越界：{summary.get('evidence_id_boundary_violation_count')}；有 claim 卻沒有 Evidence ID：{summary.get('supported_claim_missing_evidence_id_count')}。",
        f"- Hard-insufficient fallback accuracy：{summary.get('insufficient_fallback_accuracy')}。",
        f"- 平均 model latency：{summary.get('model_latency_mean_seconds')} 秒；平均 total wall time：{summary.get('total_wall_time_mean_seconds')} 秒。",
        "",
        "## 安全性回歸檢查",
        "",
    ]
    for case_type in ("near_match", "temporal", "same_risk_conflict", "hard_insufficient", "numeric_trap", "partial_guess"):
        lines.append(f"- `{case_type}`：{by_type.get(case_type, {}).get('manual_decision_accuracy')}")
    lines += [
        "",
        "## 如何解讀",
        "",
        "這份報告的主要結論要看人工 Ground Truth 的決策正確率、partial protocol 檢查，以及四個安全性類別是否維持。輔助 LLM Judge 只用來補充『模型回答的 claim 大致是否被 context 支持』，不能取代人工標準。",
        "",
        "完整逐題結果在 `results/v2_evaluation_results.csv`，原始 Generator JSONL 在 `results/v2_generator_outputs.jsonl`，v1/v2 對照在 `results/v1_v2_comparison.csv`。",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "C_v2_Partial_Answer_Generator.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--evaluate-only", action="store_true")
    args = parser.parse_args()
    ensure_run_dirs()
    cases, source, interface_hash = load_frozen_interface()
    config_path = RESULTS_DIR / "v2_config.json"
    json_dump(
        config_path,
        {
            "v1_run": str(source.parent.parent),
            "v1_interface_sha256": interface_hash,
            "case_count": len(cases),
            "case_ids": [case["case_id"] for case in cases],
            "smoke_case_ids": list(SMOKE_CASE_IDS),
            "strategy": "evidence_aware_v2_partial_answer",
            "prompt_version": PROMPT_VERSION,
            "generator_prompt_includes_ground_truth": False,
            "controls_changed": ["Evidence-aware schema and decision policy only"],
        },
    )
    if args.smoke_only:
        rows = run_generator(cases, RESULTS_DIR / "smoke_v2_outputs.jsonl", smoke_only=True)
        print(json.dumps({"smoke_rows": len(rows), "cases": SMOKE_CASE_IDS}, ensure_ascii=False))
        return
    output_path = RESULTS_DIR / "v2_generator_outputs.jsonl"
    if not args.evaluate_only:
        rows = run_generator(cases, output_path, smoke_only=False)
    else:
        rows = read_jsonl(output_path)
    aux_path = RESULTS_DIR / "v2_llm_judge_evaluations.jsonl"
    expected_aux_ids = {row["case_id"] for row in rows if not row.get("error")}
    existing_aux_ids = {row["case_id"] for row in read_jsonl(aux_path) if not row.get("error")}
    if not args.evaluate_only or not aux_path.exists() or existing_aux_ids != expected_aux_ids:
        aux = run_auxiliary_judge(cases, rows, aux_path)
    else:
        aux = read_jsonl(aux_path)
    summary, per_case = summarize_v2(cases, rows, aux)
    summary["interface_source"] = str(source)
    summary["interface_sha256"] = interface_hash
    write_metrics(summary, per_case)
    write_comparison(summary)
    write_report(summary, source, interface_hash)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
