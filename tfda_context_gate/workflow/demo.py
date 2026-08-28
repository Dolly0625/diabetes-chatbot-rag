from __future__ import annotations

import argparse
import json
from pathlib import Path

from tfda_context_gate.b_context_gate.gate import DeterministicContextGate
from tfda_context_gate.c_generator.workflow_adapter import DeterministicFixtureCGenerator
from tfda_context_gate.e_observability import JsonlTraceSink
from tfda_context_gate.rag import FixtureRetriever, TFDADrugSafetyRetriever
from tfda_context_gate.rag.tfda_smoke_cases import TFDA_SMOKE_CASES_BY_ID

from .runner import run_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the A-E workflow demo (formal by default).")
    parser.add_argument("--input", default=None)
    parser.add_argument("--request-id", default="workflow-demo-001")
    parser.add_argument("--role", default="PATIENT", choices=("PATIENT", "CAREGIVER", "HEALTHCARE_PROFESSIONAL"))
    parser.add_argument("--case", choices=tuple(TFDA_SMOKE_CASES_BY_ID), default=None)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--retriever", choices=("real", "fixture"), default="real")
    parser.add_argument("--documents-path", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--formal", action="store_true", default=True, help="Use formal LLM components from .env (mimo-v2.5)")
    parser.add_argument("--no-formal", dest="formal", action="store_false", help="Use deterministic fixtures")
    args = parser.parse_args()
    selected_case = TFDA_SMOKE_CASES_BY_ID[args.case] if args.case else (
        TFDA_SMOKE_CASES_BY_ID["P1"] if args.input is None else None
    )
    if selected_case and selected_case.clarification_candidate:
        parser.error("C3 is a future ASK_USER candidate; use tfda_context_gate.rag.demo --case C3")
    input_text = args.input or (
        selected_case.query if selected_case else "請說明糖尿病的一般飲食原則。"
    )
    role = selected_case.declared_role if selected_case else args.role
    request_id = selected_case.case_id.lower() if selected_case else args.request_id
    if args.formal:
        from tfda_context_gate.a_router.router import LangChainSignalExtractor

        try:
            extractor = LangChainSignalExtractor.from_env()
        except Exception:
            extractor = None
        # Formal mode: real RAG + all_retrieved gate + try formal C, fallback to fixture on failure
        retriever = (
            TFDADrugSafetyRetriever(documents_path=args.documents_path, top_k=args.top_k)
            if args.retriever == "real"
            else FixtureRetriever()
        )
        context_gate = DeterministicContextGate(
            approval_mode="all_retrieved" if args.retriever == "real" else "fixture"
        )
        try:
            from tfda_context_gate.workflow.runner import _build_formal_generator

            generator = _build_formal_generator()
        except Exception:
            generator = DeterministicFixtureCGenerator(max_evidence=1 if args.retriever == "real" else None)
        result = run_workflow(
            {
                "request_id": request_id,
                "schema_version": "a.v0.1",
                "user_raw_input": input_text,
                "declared_role": role,
                "language": "zh-TW",
            },
            extractor=extractor,
            retriever=retriever,
            context_gate=context_gate,
            generator=generator,
            trace_sink=JsonlTraceSink(args.log_path),
        )
    else:
        retriever = (
            TFDADrugSafetyRetriever(documents_path=args.documents_path, top_k=args.top_k)
            if args.retriever == "real"
            else FixtureRetriever()
        )
        context_gate = DeterministicContextGate(
            approval_mode="all_retrieved" if args.retriever == "real" else "fixture"
        )
        generator = DeterministicFixtureCGenerator(max_evidence=1 if args.retriever == "real" else None)
        result = run_workflow(
            {
                "request_id": request_id,
                "schema_version": "a.v0.1",
                "user_raw_input": input_text,
                "declared_role": role,
                "language": "zh-TW",
            },
            retriever=retriever,
            context_gate=context_gate,
            generator=generator,
            trace_sink=JsonlTraceSink(args.log_path),
        )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
