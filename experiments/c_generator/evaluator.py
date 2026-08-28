from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from tfda_context_gate.rate_limiter import RollingRequestRateLimiter, invoke_with_rate_limit
from tfda_context_gate.c_generator.prompts import AUXILIARY_JUDGE_SYSTEM, evaluation_user_prompt


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, round((len(values) - 1) * percentile)))
    return values[index]


def _usage_total(row: dict) -> int | None:
    usage = row.get("usage") or {}
    value = usage.get("total_tokens")
    return int(value) if value is not None else None


def _claims_from_output(row: dict) -> list[dict]:
    output = row.get("output")
    if isinstance(output, dict):
        return output.get("claims", []) or []
    return []


def _deterministic_evidence_metrics(case: dict, row: dict) -> dict[str, Any]:
    if row["method"] != "evidence_aware" or not isinstance(row.get("output"), dict):
        return {"citation_accuracy": None, "citation_coverage": None, "evidence_claim_count": None}
    claims = _claims_from_output(row)
    approved = set(case["approved_document_ids"])
    important = [claim for claim in claims if str(claim.get("claim", "")).strip()]
    cited = [claim for claim in important if claim.get("evidence_ids")]
    correct = [claim for claim in cited if set(claim.get("evidence_ids", [])) <= approved]
    return {
        "citation_accuracy": len(correct) / len(cited) if cited else None,
        "citation_coverage": len(cited) / len(important) if important else None,
        "evidence_claim_count": len(important),
    }


def evaluate_outputs(
    cases: list[dict],
    output_rows: list[dict],
    judge_chain: Any,
    limiter: RollingRequestRateLimiter,
    aux_path: Path,
) -> list[dict]:
    case_by_id = {case["case_id"]: case for case in cases}
    aux_path.parent.mkdir(parents=True, exist_ok=True)
    evaluations: list[dict] = []
    with aux_path.open("w", encoding="utf-8") as handle:
        for row in output_rows:
            if row.get("error"):
                continue
            case = case_by_id[row["case_id"]]
            output = row.get("output")
            output_for_prompt = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            label = f"c.aux_judge.{row['method']}.{row['case_id']}"
            timing = None
            try:
                response, timing = invoke_with_rate_limit(
                    lambda: judge_chain.invoke(
                        [
                            SystemMessage(content=AUXILIARY_JUDGE_SYSTEM),
                            HumanMessage(content=evaluation_user_prompt(case, row["method"], output_for_prompt)),
                        ]
                    ),
                    limiter,
                    label,
                )
                if not isinstance(response, dict) or response.get("parsed") is None:
                    raise RuntimeError("auxiliary_structured_output_parse_failed")
                parsed = response["parsed"].model_dump(mode="json")
                raw = response.get("raw")
                usage = getattr(raw, "usage_metadata", None)
                record = {
                    "case_id": row["case_id"],
                    "case_type": row["case_type"],
                    "method": row["method"],
                    "evaluation_role": "auxiliary_not_ground_truth",
                    "evaluation": parsed,
                    "usage": usage if isinstance(usage, dict) else None,
                    "timing": timing,
                    "error": None,
                }
            except Exception as error:
                record = {
                    "case_id": row["case_id"],
                    "case_type": row["case_type"],
                    "method": row["method"],
                    "evaluation_role": "auxiliary_not_ground_truth",
                    "evaluation": None,
                    "usage": None,
                    "timing": timing or getattr(error, "timing", None),
                    "error": str(error),
                }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            evaluations.append(record)
    return evaluations


def summarize(cases: list[dict], output_rows: list[dict], evaluations: list[dict]) -> tuple[dict, list[dict]]:
    eval_by_key = {(row["case_id"], row["method"]): row for row in evaluations}
    case_by_id = {case["case_id"]: case for case in cases}
    by_method: dict[str, list[dict]] = defaultdict(list)
    for row in output_rows:
        if not row.get("error"):
            by_method[row["method"]].append(row)

    summaries = []
    for method, rows in sorted(by_method.items()):
        assessed = [eval_by_key[(row["case_id"], method)] for row in rows if (row["case_id"], method) in eval_by_key]
        statuses = []
        unsupported = 0
        partial = 0
        total_claims = 0
        insufficient_correct = []
        citation_accuracy = []
        citation_coverage = []
        model_latency = []
        wall_latency = []
        token_counts = []
        manual_decisions = []
        manual_stress_decisions = []
        for row in rows:
            case = case_by_id[row["case_id"]]
            if method == "evidence_aware" and isinstance(row.get("output"), dict):
                expected = case["ground_truth"]["expected_decision"]
                actual = row["output"].get("decision")
                manual_decisions.append(actual == expected)
                if case["case_type"] in {"partial", "insufficient", "partial_guess", "hard_insufficient"}:
                    manual_stress_decisions.append(actual == expected)
            timing = row.get("timing") or {}
            if timing.get("model_latency") is not None:
                model_latency.append(float(timing["model_latency"]))
            if timing.get("total_wall_time") is not None:
                wall_latency.append(float(timing["total_wall_time"]))
            total = _usage_total(row)
            if total is not None:
                token_counts.append(total)
            metrics = _deterministic_evidence_metrics(case_by_id[row["case_id"]], row)
            if metrics["citation_accuracy"] is not None:
                citation_accuracy.append(metrics["citation_accuracy"])
            if metrics["citation_coverage"] is not None:
                citation_coverage.append(metrics["citation_coverage"])
        for record in assessed:
            evaluation = record.get("evaluation") or {}
            supported_count = int(evaluation.get("supported_claim_count", 0))
            partial_count = int(evaluation.get("partially_supported_claim_count", 0))
            unsupported_count = int(evaluation.get("unsupported_claim_count", 0))
            total_claims += supported_count + partial_count + unsupported_count
            statuses.extend(["SUPPORTED"] * supported_count)
            statuses.extend(["PARTIALLY_SUPPORTED"] * partial_count)
            statuses.extend(["UNSUPPORTED"] * unsupported_count)
            unsupported += unsupported_count
            partial += partial_count
            if isinstance(evaluation.get("insufficient_handling_correct"), bool):
                case = case_by_id[record["case_id"]]
                if case["case_type"] in {"partial", "insufficient", "partial_guess", "hard_insufficient"}:
                    insufficient_correct.append(evaluation["insufficient_handling_correct"])
        case_type_metrics = {}
        case_types = sorted({case["case_type"] for case in cases})
        for case_type in case_types:
            type_records = [r for r in assessed if case_by_id[r["case_id"]]["case_type"] == case_type]
            correct = [r for r in type_records if (r.get("evaluation") or {}).get("insufficient_handling_correct")]
            manual_type = []
            if method == "evidence_aware":
                for row in rows:
                    case = case_by_id[row["case_id"]]
                    if case["case_type"] == case_type and isinstance(row.get("output"), dict):
                        manual_type.append(row["output"].get("decision") == case["ground_truth"]["expected_decision"])
            case_type_metrics[case_type] = {
                "n": len(type_records),
                "insufficient_handling_accuracy": len(correct) / len(type_records) if type_records else None,
                "manual_decision_accuracy": sum(manual_type) / len(manual_type) if manual_type else None,
            }
        summaries.append(
            {
                "method": method,
                "n_outputs": len(rows),
                "n_auxiliary_evaluations": len(assessed),
                "claim_support_rate": statuses.count("SUPPORTED") / total_claims if total_claims else None,
                "claim_partial_rate": partial / total_claims if total_claims else None,
                "unsupported_claim_rate": unsupported / total_claims if total_claims else None,
                "citation_accuracy": statistics.mean(citation_accuracy) if citation_accuracy else None,
                "citation_coverage": statistics.mean(citation_coverage) if citation_coverage else None,
                "manual_decision_accuracy": (
                    sum(manual_decisions) / len(manual_decisions) if manual_decisions else None
                ),
                "manual_partial_and_insufficient_decision_accuracy": (
                    sum(manual_stress_decisions) / len(manual_stress_decisions)
                    if manual_stress_decisions else None
                ),
                "insufficient_handling_accuracy_partial_and_stress": (
                    sum(insufficient_correct) / len(insufficient_correct) if insufficient_correct else None
                ),
                "model_latency_mean_seconds": statistics.mean(model_latency) if model_latency else None,
                "model_latency_median_seconds": statistics.median(model_latency) if model_latency else None,
                "model_latency_p95_seconds": _percentile(model_latency, 0.95),
                "total_wall_time_mean_seconds": statistics.mean(wall_latency) if wall_latency else None,
                "total_tokens_mean": statistics.mean(token_counts) if token_counts else None,
                "case_type_metrics": case_type_metrics,
                "evaluation_note": "Auxiliary LLM Judge; manual Ground Truth remains primary.",
            }
        )
    overall = {
        "ground_truth": "manual_case_specs",
        "auxiliary_judge_is_not_ground_truth": True,
        "case_counts": {case_type: sum(case["case_type"] == case_type for case in cases) for case_type in sorted({case["case_type"] for case in cases})},
        "methods": summaries,
    }
    return overall, summaries


def write_summary(summary: dict, rows: list[dict], json_path: Path, csv_path: Path) -> None:
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "method", "n_outputs", "n_auxiliary_evaluations", "claim_support_rate",
        "claim_partial_rate", "unsupported_claim_rate", "citation_accuracy",
        "citation_coverage", "insufficient_handling_accuracy_partial_and_stress",
        "model_latency_mean_seconds", "model_latency_median_seconds", "model_latency_p95_seconds",
        "total_wall_time_mean_seconds", "total_tokens_mean",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
