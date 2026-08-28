from __future__ import annotations

import argparse
import json
from pathlib import Path

from tfda_context_gate.a_router.router import route_request
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .tfda_smoke_cases import (
    TFDA_SMOKE_CASES_BY_ID,
    TFDASmokeCase,
)
from .tfda_retriever import TFDADrugSafetyRetriever


def run_case(case: TFDASmokeCase, retriever: TFDADrugSafetyRetriever | None) -> dict:
    request = {
        "request_id": f"tfda-{case.case_id.lower()}",
        "schema_version": "a.v0.1",
        "user_raw_input": case.query,
        "declared_role": case.declared_role,
        "language": "zh-TW",
    }
    a_result = route_request(request)
    evidence = []
    if a_result.rag_allowed and not case.clarification_candidate and retriever is not None:
        rag_result = retriever.retrieve(
            QueryExpansionResult(
                request_id=request["request_id"],
                original_query=case.query,
                retrieval_queries=[case.query],
                strategy="identity-deterministic",
            )
        )
        evidence = rag_result.evidence
    return {
        "case_id": case.case_id,
        "declared_role": case.declared_role,
        "query": case.query,
        "a_router_status": a_result.router_status.value,
        "rag_allowed": a_result.rag_allowed,
        "boundary": case.boundary,
        "clarification_candidate": case.clarification_candidate,
        "expected_match": case.matches(evidence) if case.expected_retrieval else None,
        "top_k": [
            {
                "rank": rank,
                "evidence_id": item.evidence_id,
                "藥品成分": item.metadata.get("藥品成分"),
                "score": item.score,
                "發布日期": item.date,
                "source": item.source,
            }
            for rank, item in enumerate(evidence, 1)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run role-based real TFDA vector retrieval smoke cases.")
    parser.add_argument("--case", choices=tuple(TFDA_SMOKE_CASES_BY_ID), default=None)
    parser.add_argument("--query", default=None, help="Optional one-off query; --case is preferred for smoke cases.")
    parser.add_argument("--role", choices=("PATIENT", "HEALTHCARE_PROFESSIONAL", "CAREGIVER"), default="PATIENT")
    parser.add_argument("--all", action="store_true", help="Run all Patient/Healthcare Professional/Caregiver cases.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--documents-path", type=Path, default=None)
    args = parser.parse_args()

    if args.all:
        cases = list(TFDA_SMOKE_CASES_BY_ID.values())
    elif args.case:
        cases = [TFDA_SMOKE_CASES_BY_ID[args.case]]
    elif args.query:
        cases = [TFDASmokeCase("CUSTOM", args.role, args.query)]
    else:
        parser.error("provide --case, --all, or --query")

    needs_retriever = any(case.expected_retrieval and not case.clarification_candidate for case in cases)
    retriever = TFDADrugSafetyRetriever(documents_path=args.documents_path, top_k=args.top_k) if needs_retriever else None
    if retriever is not None:
        print(f"corpus={retriever.documents_path}")
    results = [run_case(case, retriever) for case in cases]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
