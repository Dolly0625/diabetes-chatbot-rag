from __future__ import annotations

import csv
import json
import os
import statistics
import time
from pathlib import Path
from typing import Literal
from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel, Field

from rate_limiter import RollingRequestRateLimiter, invoke_with_rate_limit
from run_config import (
    PROJECT_ROOT,
    PROCESSED_DIR,
    RESULTS_DIR,
    env_value,
    ensure_run_dirs,
)

ROOT = Path(__file__).resolve().parent
DOCS_PATH = PROCESSED_DIR / "langchain_documents.json"
PROMPT_PATH = ROOT / "prompts" / "hybrid_set_judge_v1.txt"

NARROW_QUERY = "TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？"
BROAD_QUERY = "TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？"
EMBED_MODEL = env_value("EMBED_MODEL", "intfloat/multilingual-e5-small")
RERANKER_MODEL = env_value("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_DEVICE = env_value("RERANKER_DEVICE", "cpu")
JUDGE_MODEL = env_value("JUDGE_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
OPENROUTER_ENDPOINT = env_value("base_url", "https://openrouter.ai/api/v1")
JUDGE_TEMPERATURE = 0
JUDGE_TIMEOUT_SECONDS = float(env_value("JUDGE_REQUEST_TIMEOUT", "60"))
JUDGE_MAX_TOKENS = int(env_value("JUDGE_MAX_TOKENS", "2048"))
JUDGE_REASONING_EFFORT = env_value("JUDGE_REASONING_EFFORT", "low")
RATE_LIMIT_STATE_PATH = PROJECT_ROOT / ".openrouter_rate_limit_state.json"
RATE_LIMIT_EVENT_PATH = RESULTS_DIR / "rate_limit_events.jsonl"
CANDIDATE_K = 20
RERANKER_TOP_NS = (3, 4, 5)
REPEAT_COUNT = 3


class HybridContextDecision(BaseModel):
    usable_document_ids: list[str] = Field(default_factory=list)
    excluded_document_ids: list[str] = Field(default_factory=list)
    sufficient_for_answer: bool
    decision: Literal["PASS", "REVIEW", "FALLBACK"]
    reason_codes: list[str] = Field(default_factory=list)


def build_judge() -> tuple[ChatOpenRouter, str]:
    api_key = env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY in .env or environment")
    return (
        ChatOpenRouter(
            model=JUDGE_MODEL,
            api_key=api_key,
            base_url=OPENROUTER_ENDPOINT,
            temperature=JUDGE_TEMPERATURE,
            timeout=int(JUDGE_TIMEOUT_SECONDS * 1000),
            max_retries=0,
            max_tokens=JUDGE_MAX_TOKENS,
            reasoning={"effort": JUDGE_REASONING_EFFORT},
        ),
        OPENROUTER_ENDPOINT,
    )


def load_documents() -> tuple[list[Document], dict[str, dict]]:
    serialized = json.loads(DOCS_PATH.read_text(encoding="utf-8"))
    documents = [
        Document(
            id=item["id"],
            page_content=item["page_content"],
            metadata=item["metadata"],
        )
        for item in serialized
    ]
    return documents, {item["id"]: item for item in serialized}


def run_contract_gate(documents: list[Document]) -> dict:
    started = time.perf_counter()
    seen: set[str] = set()
    passed: list[Document] = []
    rejected: list[dict[str, object]] = []
    for doc in documents:
        reasons = []
        document_id = doc.metadata.get("document_id")
        if not document_id:
            reasons.append("missing_document_id")
        elif document_id in seen:
            reasons.append("duplicate_document_id")
        else:
            seen.add(document_id)
        if doc.metadata.get("row_index") is None:
            reasons.append("missing_row_index")
        if not str(doc.metadata.get("藥品成分", "")).strip():
            reasons.append("empty_藥品成分")
        if not str(doc.metadata.get("發布日期", "")).strip():
            reasons.append("empty_發布日期")
        if not doc.page_content.strip():
            reasons.append("empty_page_content")
        if reasons:
            rejected.append({"document_id": document_id, "reasons": reasons})
        else:
            passed.append(doc)
    return {
        "passed_documents": passed,
        "rejected": rejected,
        "trace": {
            "stage": "contract_gate",
            "input_count": len(documents),
            "output_count": len(passed),
            "kept_ids": [doc.metadata["document_id"] for doc in passed],
            "rejected_ids": [item["document_id"] for item in rejected],
            "latency_seconds": time.perf_counter() - started,
        },
    }


def preview(text: str, limit: int = 360) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


def candidate_row(
    rank: int,
    doc: Document,
    score: float,
    raw_by_id: dict[str, dict],
) -> dict:
    raw = raw_by_id[doc.metadata["document_id"]]
    return {
        "similarity_rank": rank,
        "similarity_score": round(float(score), 6),
        "document_id": doc.metadata["document_id"],
        "row_index": doc.metadata["row_index"],
        "發布日期": doc.metadata["發布日期"],
        "藥品成分": doc.metadata["藥品成分"],
        "page_content_preview": preview(doc.page_content),
        "安全資訊分析_preview": preview(
            raw.get("藥品安全有關資訊分析及描述", "")
        ),
    }


def run_retrieval(
    store: InMemoryVectorStore,
    query: str,
    raw_by_id: dict[str, dict],
) -> dict:
    started = time.perf_counter()
    retrieved = store.similarity_search_with_score(query, k=CANDIDATE_K)
    rows = []
    doc_by_id = {}
    for rank, (doc, score) in enumerate(retrieved, 1):
        rows.append(candidate_row(rank, doc, score, raw_by_id))
        doc_by_id[doc.metadata["document_id"]] = doc
    return {
        "rows": rows,
        "documents_by_id": doc_by_id,
        "trace": {
            "stage": "similarity_retrieval",
            "input_count": 129,
            "output_count": len(rows),
            "kept_ids": [row["document_id"] for row in rows],
            "rejected_ids": [],
            "latency_seconds": time.perf_counter() - started,
        },
    }


def run_reranker(
    candidates: dict,
    cross_encoder: HuggingFaceCrossEncoder,
    query: str,
    raw_by_id: dict[str, dict],
) -> dict:
    started = time.perf_counter()
    documents = [
        candidates["documents_by_id"][row["document_id"]]
        for row in candidates["rows"]
    ]
    scores = list(
        cross_encoder.score([(query, doc.page_content) for doc in documents])
    )
    scored = []
    for doc, score in zip(documents, scores, strict=True):
        scored.append(
            {
                "document": doc,
                "reranker_score": float(score),
            }
        )
    scored.sort(key=lambda item: item["reranker_score"], reverse=True)
    rows = []
    for reranker_rank, item in enumerate(scored, 1):
        doc = item["document"]
        sim_row = next(
            row
            for row in candidates["rows"]
            if row["document_id"] == doc.metadata["document_id"]
        )
        row = dict(sim_row)
        row.update(
            {
                "reranker_rank": reranker_rank,
                "reranker_score": round(item["reranker_score"], 6),
                "original_similarity_rank": sim_row["similarity_rank"],
                "original_similarity_score": sim_row["similarity_score"],
            }
        )
        rows.append(row)
    return {
        "rows": rows,
        "documents_by_id": {row["document_id"]: item["document"] for row, item in zip(rows, scored, strict=True)},
        "trace": {
            "stage": "cross_encoder_reranker",
            "input_count": len(candidates["rows"]),
            "output_count": len(rows),
            "kept_ids": [row["document_id"] for row in rows],
            "rejected_ids": [],
            "latency_seconds": time.perf_counter() - started,
        },
    }


def context_text(rows: list[dict], raw_by_id: dict[str, dict]) -> str:
    blocks = []
    for row in rows:
        blocks.append(
            "\n".join(
                [
                    f"document_id: {row['document_id']}",
                    f"row_index: {row['row_index']}",
                    f"發布日期: {row['發布日期']}",
                    f"藥品成分: {row['藥品成分']}",
                    f"page_content:\n{raw_by_id[row['document_id']]['page_content']}",
                ]
            )
        )
    return "\n\n--- DOCUMENT SEPARATOR ---\n\n".join(blocks)


def invoke_judge(
    judge_chain,
    query: str,
    rows: list[dict],
    raw_by_id: dict[str, dict],
    prompt: str,
    limiter: RollingRequestRateLimiter,
    label: str,
) -> tuple[dict, dict[str, float | int], dict | None]:
    response, timing = invoke_with_rate_limit(
        lambda: judge_chain.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(
                    content=(
                        f"Query:\n{query}\n\n"
                        "Evaluate this context set as a whole. Do not answer the query.\n\n"
                        f"{context_text(rows, raw_by_id)}"
                    )
                ),
            ]
        ),
        limiter,
        label,
    )
    if not isinstance(response, dict) or response.get("parsed") is None:
        raise RuntimeError(f"Hybrid structured output parsing failed: {response!r}")
    raw_message = response.get("raw")
    usage = getattr(raw_message, "usage_metadata", None)
    if not isinstance(usage, dict):
        usage = None
    return response["parsed"].model_dump(mode="json"), timing, usage


def label_for(query: str, document_id: str) -> str:
    narrow = {
        "tfda-risk-0019": "DIRECT",
        "tfda-risk-0064": "PARTIAL",
        "tfda-risk-0042": "PARTIAL",
        "tfda-risk-0035": "PARTIAL",
        "tfda-risk-0015": "IRRELEVANT",
        "tfda-risk-0102": "IRRELEVANT",
        "tfda-risk-0068": "IRRELEVANT",
        "tfda-risk-0112": "IRRELEVANT",
        "tfda-risk-0020": "IRRELEVANT",
        "tfda-risk-0053": "IRRELEVANT",
    }
    broad = {
        "tfda-risk-0064": "DIRECT",
        "tfda-risk-0019": "DIRECT",
        "tfda-risk-0042": "DIRECT",
        "tfda-risk-0035": "DIRECT",
        "tfda-risk-0020": "IRRELEVANT",
        "tfda-risk-0112": "IRRELEVANT",
        "tfda-risk-0024": "IRRELEVANT",
        "tfda-risk-0027": "IRRELEVANT",
        "tfda-risk-0026": "IRRELEVANT",
        "tfda-risk-0023": "IRRELEVANT",
    }
    return (narrow if query == NARROW_QUERY else broad)[document_id]


def metrics_for(
    query: str,
    rows: list[dict],
    reference_rows: list[dict],
    usable_ids: list[str],
) -> dict:
    labels = {row["document_id"]: label_for(query, row["document_id"]) for row in rows}
    reference_labels = {
        row["document_id"]: label_for(query, row["document_id"])
        for row in reference_rows
    }
    usable_labels = [labels[doc_id] for doc_id in usable_ids if doc_id in labels]
    direct_ids = [
        doc_id for doc_id, label in reference_labels.items() if label == "DIRECT"
    ]
    direct_kept = sum(label == "DIRECT" for label in usable_labels)
    return {
        "usable_count": len(usable_ids),
        "usable_direct_count": direct_kept,
        "context_precision": (
            direct_kept / len(usable_ids) if usable_ids else None
        ),
        "direct_evidence_recall": (
            direct_kept / len(direct_ids) if direct_ids else None
        ),
        "human_labels_in_candidate_set": labels,
        "human_labels_in_reference_top10": reference_labels,
        "direct_ids_in_reference_top10": direct_ids,
    }


def variant_result(
    query: str,
    top_n: int,
    contract: dict,
    retrieval: dict,
    reranker: dict,
    judge_chain,
    raw_by_id: dict[str, dict],
    prompt: str,
    run_index: int,
    limiter: RollingRequestRateLimiter,
) -> dict:
    selected = reranker["rows"][:top_n]
    assessment, judge_timing, usage = invoke_judge(
        judge_chain,
        query,
        selected,
        raw_by_id,
        prompt,
        limiter,
        f"phase5.run{run_index}.{query[:12]}.top{top_n}",
    )
    selected_ids = [row["document_id"] for row in selected]
    trace = {
        "query": query,
        "top_n": top_n,
        "stages": [
            contract["trace"],
            retrieval["trace"],
            {
                **reranker["trace"],
                "output_count": top_n,
                "kept_ids": selected_ids,
            },
            {
                "stage": "set_level_llm_judge",
                "input_count": top_n,
                "output_count": len(assessment["usable_document_ids"]),
                "kept_ids": assessment["usable_document_ids"],
                "excluded_ids": assessment["excluded_document_ids"],
                "latency_seconds": judge_timing["total_wall_time"],
            },
        ],
    }
    shared_latency = (
        contract["trace"]["latency_seconds"]
        + retrieval["trace"]["latency_seconds"]
        + reranker["trace"]["latency_seconds"]
    )
    return {
        "run_index": run_index,
        "query": query,
        "top_n": top_n,
        "candidate_k": CANDIDATE_K,
        "reranker_model": RERANKER_MODEL,
        "context_rows": selected,
        "judge": assessment,
        "metrics": metrics_for(
            query,
            selected,
            reranker["rows"][:10],
            assessment["usable_document_ids"],
        ),
        "latency_seconds": {
            "retrieval_reranker_shared": shared_latency,
            "judge": judge_timing["model_latency"],
            "judge_model_latency": judge_timing["model_latency"],
            "judge_rate_limit_wait_time": judge_timing["rate_limit_wait_time"],
            "judge_retry_wait_time": judge_timing["retry_wait_time"],
            "judge_total_wall_time": judge_timing["total_wall_time"],
            "end_to_end_variant": shared_latency + judge_timing["total_wall_time"],
            "end_to_end_model_time": shared_latency + judge_timing["model_latency"],
        },
        "usage": usage,
        "trace": trace,
    }


def ablation_result(
    narrow_reranker: dict,
    judge_chain,
    raw_by_id: dict[str, dict],
    prompt: str,
    contract: dict,
    retrieval: dict,
    run_index: int,
    limiter: RollingRequestRateLimiter,
) -> dict:
    selected = [
        row
        for row in narrow_reranker["rows"]
        if row["document_id"]
        in {"tfda-risk-0064", "tfda-risk-0042", "tfda-risk-0035"}
    ]
    assessment, judge_timing, usage = invoke_judge(
        judge_chain,
        NARROW_QUERY,
        selected,
        raw_by_id,
        prompt,
        limiter,
        f"phase5.run{run_index}.fallback_ablation",
    )
    return {
        "run_index": run_index,
        "ablation_only": True,
        "query": NARROW_QUERY,
        "context_description": "Only different SGLT2 safety topics; ketoacidosis document removed",
        "context_rows": selected,
        "judge": assessment,
        "latency_seconds": {
            "judge": judge_timing["model_latency"],
            "judge_model_latency": judge_timing["model_latency"],
            "judge_rate_limit_wait_time": judge_timing["rate_limit_wait_time"],
            "judge_retry_wait_time": judge_timing["retry_wait_time"],
            "judge_total_wall_time": judge_timing["total_wall_time"],
        },
        "usage": usage,
        "trace": {
            "query": NARROW_QUERY,
            "ablation_only": True,
            "stages": [
                contract["trace"],
                retrieval["trace"],
                {
                    **narrow_reranker["trace"],
                    "output_count": len(selected),
                    "kept_ids": [row["document_id"] for row in selected],
                },
                {
                    "stage": "set_level_llm_judge",
                    "input_count": len(selected),
                    "output_count": len(assessment["usable_document_ids"]),
                    "kept_ids": assessment["usable_document_ids"],
                    "excluded_ids": assessment["excluded_document_ids"],
                    "latency_seconds": judge_timing["total_wall_time"],
                },
            ],
        },
    }


def warm_up(
    embeddings,
    cross_encoder,
    judge_chain,
    prompt: str,
    limiter: RollingRequestRateLimiter,
) -> None:
    embeddings.embed_query("warm-up query")
    cross_encoder.score([("warm-up query", "warm-up document")])
    invoke_judge(
        judge_chain,
        "warm-up query",
        [
            {
                "document_id": "warm-up",
                "row_index": -1,
                "發布日期": "warm-up",
                "藥品成分": "warm-up",
            }
        ],
        {"warm-up": {"page_content": "warm-up document"}},
        prompt,
        limiter,
        "phase5.warm_up",
    )


def flatten_variant_records(all_runs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for run in all_runs:
        for key, result in run["variants"].items():
            grouped.setdefault(key, []).append(result)
    return grouped


def summarize_stats(values: list[float]) -> dict:
    return {
        "values": values,
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def usage_summary(records: list[dict]) -> dict:
    calls = [record.get("usage") for record in records if record.get("usage")]
    if not calls:
        return {"available": False}
    totals = {}
    for key in ["input_tokens", "output_tokens", "total_tokens"]:
        values = [int(item[key]) for item in calls if key in item]
        if values:
            totals[key] = {
                "values": values,
                "sum": sum(values),
                "mean": statistics.mean(values),
            }
    return {"available": True, **totals}


def build_cost_latency(
    grouped: dict[str, list[dict]],
    ablations: list[dict],
    all_runs: list[dict],
) -> dict:
    phase4_latency = json.loads((RESULTS_DIR / "phase4_latency.json").read_text())
    phase4_runs = json.loads(
        (RESULTS_DIR / "document_judge_results.json").read_text()
    )["runs"]
    phase4_tokens = []
    for run in phase4_runs:
        for item in run["usage"]["document_each"]:
            if item:
                phase4_tokens.append(item)
        for item in run["usage"]["set_each"].values():
            if item:
                phase4_tokens.append(item)
    baseline_per_round = {
        "llm_calls": 13,
        "mean_model_latency_seconds": phase4_latency["all_calls_seconds"].get(
            "model_latency_mean", phase4_latency["all_calls_seconds"]["mean"]
        ),
        "mean_total_wall_time_seconds": phase4_latency["all_calls_seconds"]["mean"],
        "token_usage": {
            "input_tokens": sum(item.get("input_tokens", 0) for item in phase4_tokens)
            / len(phase4_runs),
            "output_tokens": sum(item.get("output_tokens", 0) for item in phase4_tokens)
            / len(phase4_runs),
            "total_tokens": sum(item.get("total_tokens", 0) for item in phase4_tokens)
            / len(phase4_runs),
        },
        "source": "Phase 4 one-round mean over 3 measured rounds",
    }
    variants = {}
    for key, records in grouped.items():
        variants[key] = {
            "runs": len(records),
            "llm_calls_per_run": 1,
            "judge_model_latency_seconds": summarize_stats(
                [record["latency_seconds"]["judge_model_latency"] for record in records]
            ),
            "judge_total_wall_time_seconds": summarize_stats(
                [record["latency_seconds"]["judge_total_wall_time"] for record in records]
            ),
            "rate_limit_wait_seconds": summarize_stats(
                [record["latency_seconds"]["judge_rate_limit_wait_time"] for record in records]
            ),
            "retry_wait_seconds": summarize_stats(
                [record["latency_seconds"]["judge_retry_wait_time"] for record in records]
            ),
            "end_to_end_variant_latency_seconds": summarize_stats(
                [record["latency_seconds"]["end_to_end_variant"] for record in records]
            ),
            "token_usage": usage_summary(records),
            "context_precision_values": [
                record["metrics"]["context_precision"] for record in records
            ],
            "direct_evidence_recall_values": [
                record["metrics"]["direct_evidence_recall"] for record in records
            ],
        }
    variants["fallback_ablation"] = {
        "runs": len(ablations),
        "llm_calls_per_run": 1,
        "judge_model_latency_seconds": summarize_stats(
            [record["latency_seconds"]["judge_model_latency"] for record in ablations]
        ),
        "judge_total_wall_time_seconds": summarize_stats(
            [record["latency_seconds"]["judge_total_wall_time"] for record in ablations]
        ),
        "token_usage": usage_summary(ablations),
    }
    full_run_latency = []
    full_run_calls = []
    full_run_usage = []
    for run in all_runs:
        records = list(run["variants"].values()) + [run["ablation"]]
        full_run_latency.append(
            run["shared_stage_latency_seconds"]
            + sum(record["latency_seconds"]["judge_total_wall_time"] for record in records)
        )
        full_run_calls.append(len(records))
        full_run_usage.extend(record for record in records)
    return {
        "standalone_judge_phase4_reference": baseline_per_round,
        "hybrid_variants": variants,
        "full_phase5_workload": {
            "calls_per_run": full_run_calls,
            "end_to_end_seconds": summarize_stats(full_run_latency),
            "token_usage": usage_summary(full_run_usage),
            "description": "Narrow Top-3/4/5 + Broad Top-3/4/5 + fallback ablation",
        },
    }


def main() -> None:
    ensure_run_dirs()
    documents, raw_by_id = load_documents()
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        encode_kwargs={"normalize_embeddings": True, "prompt": "passage: "},
        query_encode_kwargs={"normalize_embeddings": True, "prompt": "query: "},
    )
    store = InMemoryVectorStore(embedding=embeddings)
    contract = run_contract_gate(documents)
    store.add_documents(contract["passed_documents"])
    cross_encoder = HuggingFaceCrossEncoder(
        model_name=RERANKER_MODEL,
        model_kwargs={"device": RERANKER_DEVICE},
    )
    llm, base_url = build_judge()
    limiter = RollingRequestRateLimiter(RATE_LIMIT_STATE_PATH, RATE_LIMIT_EVENT_PATH)
    judge_chain = llm.with_structured_output(
        HybridContextDecision,
        method="json_schema",
        strict=True,
        include_raw=True,
    )

    # Warm-up is deliberately outside the measured runs.
    warm_up(embeddings, cross_encoder, judge_chain, prompt, limiter)

    all_runs = []
    traces = []
    for run_index in range(1, REPEAT_COUNT + 1):
        run_started = time.perf_counter()
        narrow_retrieval = run_retrieval(store, NARROW_QUERY, raw_by_id)
        broad_retrieval = run_retrieval(store, BROAD_QUERY, raw_by_id)
        narrow_reranker = run_reranker(
            narrow_retrieval, cross_encoder, NARROW_QUERY, raw_by_id
        )
        broad_reranker = run_reranker(
            broad_retrieval, cross_encoder, BROAD_QUERY, raw_by_id
        )
        variants = {}
        for query_name, query, retrieval, reranker in [
            ("narrow", NARROW_QUERY, narrow_retrieval, narrow_reranker),
            ("broad", BROAD_QUERY, broad_retrieval, broad_reranker),
        ]:
            for top_n in RERANKER_TOP_NS:
                key = f"{query_name}_top{top_n}"
                print(f"run={run_index} variant={key}", flush=True)
                variants[key] = variant_result(
                    query,
                    top_n,
                    contract,
                    retrieval,
                    reranker,
                    judge_chain,
                    raw_by_id,
                    prompt,
                    run_index,
                    limiter,
                )
        print(f"run={run_index} variant=fallback_ablation", flush=True)
        ablation = ablation_result(
            narrow_reranker,
            judge_chain,
            raw_by_id,
            prompt,
            contract,
            narrow_retrieval,
            run_index,
            limiter,
        )
        shared_stage_latency = (
            contract["trace"]["latency_seconds"]
            + narrow_retrieval["trace"]["latency_seconds"]
            + broad_retrieval["trace"]["latency_seconds"]
            + narrow_reranker["trace"]["latency_seconds"]
            + broad_reranker["trace"]["latency_seconds"]
        )
        run_record = {
            "run_index": run_index,
            "variants": variants,
            "ablation": ablation,
            "shared_stage_latency_seconds": shared_stage_latency,
            "run_wall_seconds": time.perf_counter() - run_started,
            "contract_trace": contract["trace"],
            "narrow_retrieval_trace": narrow_retrieval["trace"],
            "broad_retrieval_trace": broad_retrieval["trace"],
            "narrow_reranker_trace": narrow_reranker["trace"],
            "broad_reranker_trace": broad_reranker["trace"],
        }
        all_runs.append(run_record)
        traces.append(
            {
                "run_index": run_index,
                "contract": contract["trace"],
                "queries": {
                    "narrow": {
                        "retrieval": narrow_retrieval["trace"],
                        "reranker": narrow_reranker["trace"],
                        "variants": {
                            key: value["trace"]
                            for key, value in variants.items()
                            if key.startswith("narrow_")
                        },
                    },
                    "broad": {
                        "retrieval": broad_retrieval["trace"],
                        "reranker": broad_reranker["trace"],
                        "variants": {
                            key: value["trace"]
                            for key, value in variants.items()
                            if key.startswith("broad_")
                        },
                    },
                },
                "fallback_ablation": ablation["trace"],
            }
        )

    grouped = flatten_variant_records(all_runs)
    ablations = [run["ablation"] for run in all_runs]
    cost_latency = build_cost_latency(grouped, ablations, all_runs)

    for key in [
        "narrow_top3",
        "narrow_top4",
        "narrow_top5",
        "broad_top3",
        "broad_top4",
        "broad_top5",
    ]:
        payload = {
            "variant": key,
            "embedding_model": EMBED_MODEL,
            "reranker_model": RERANKER_MODEL,
            "judge_model": JUDGE_MODEL,
            "temperature": JUDGE_TEMPERATURE,
            "runs": grouped[key],
        }
        (RESULTS_DIR / f"hybrid_{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (RESULTS_DIR / "hybrid_fallback_ablation.json").write_text(
        json.dumps(
            {
                "ablation_only": True,
                "judge_model": JUDGE_MODEL,
                "runs": ablations,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (RESULTS_DIR / "phase5_cost_latency.json").write_text(
        json.dumps(cost_latency, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RESULTS_DIR / "phase5_trace.json").write_text(
        json.dumps(
            {
                "corpus_size": len(documents),
                "contract_passed": len(contract["passed_documents"]),
                "contract_rejected": len(contract["rejected"]),
                "runs": traces,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    output_lines = [
        f"Corpus size: {len(documents)}",
        f"Contract passed: {len(contract['passed_documents'])}",
        f"Contract rejected: {len(contract['rejected'])}",
        f"Embedding: {EMBED_MODEL}",
        f"Reranker: {RERANKER_MODEL}",
        f"Judge: {JUDGE_MODEL}",
        f"Judge endpoint root: {base_url}",
        f"Repeats: {REPEAT_COUNT}",
        "",
    ]
    for key, records in grouped.items():
        output_lines.append(f"{key}")
        for record in records:
            output_lines.append(
                f"  run={record['run_index']} | "
                f"usable={record['judge']['usable_document_ids']} | "
                f"excluded={record['judge']['excluded_document_ids']} | "
                f"decision={record['judge']['decision']} | "
                f"precision={record['metrics']['context_precision']} | "
                f"recall={record['metrics']['direct_evidence_recall']} | "
                f"judge_latency={record['latency_seconds']['judge']:.3f}s"
            )
    output_lines.append("fallback_ablation")
    for record in ablations:
        output_lines.append(
            f"  run={record['run_index']} | "
            f"usable={record['judge']['usable_document_ids']} | "
            f"decision={record['judge']['decision']} | "
            f"judge_latency={record['latency_seconds']['judge']:.3f}s"
        )
    output_lines.extend(
        [
            "",
            "COST_LATENCY_SUMMARY",
            json.dumps(cost_latency, ensure_ascii=False, indent=2),
        ]
    )
    (RESULTS_DIR / "phase5_hybrid_output.txt").write_text(
        "\n".join(output_lines) + "\n", encoding="utf-8"
    )
    print("\n".join(output_lines))


if __name__ == "__main__":
    main()
