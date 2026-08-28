from __future__ import annotations

import argparse
import json
from pathlib import Path

from .sinks import JsonlTraceSink
from .tracer import TraceRecorder


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the E v0.1 offline trace demo.")
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--query", default="請說明糖尿病的一般飲食原則。")
    args = parser.parse_args()

    with TraceRecorder(
        "e-demo-001",
        declared_role="PATIENT",
        original_query=args.query,
        sink=JsonlTraceSink(args.log_path),
    ) as trace:
        with trace.span(
            "A",
            "input_router",
            router_status="G_GENERAL_EDUCATION",
            intent_tags=["GENERAL_EDUCATION"],
            risk_flags=[],
            reason_codes=["MEETS_SAFE_SCOPE"],
            rag_allowed=True,
            prompt_guard_result="Safe",
        ):
            pass
        with trace.span(
            "RAG",
            "retrieval",
            retrieval_query=args.query,
            retrieved_count=1,
            retrieved_evidence_ids=["e1"],
        ):
            pass
        with trace.span(
            "B",
            "context_gate",
            decision="PASS",
            approved_evidence_ids=["e1"],
            relevance="DIRECT",
            sufficiency="SUFFICIENT",
            safety="PASS",
        ):
            pass
        with trace.span(
            "C",
            "generator",
            candidate_decision="ANSWER",
            claim_count=1,
            evidence_ids=["e1"],
        ):
            pass
        with trace.span("D", "output_gate", decision="PASS", reason_codes=["OUTPUT_GATE_PASSED"]):
            pass
        trace.record_evaluation(
            expected_decision="ANSWER",
            actual_decision="ANSWER",
            outcome="UNLABELED_DEMO",
            metadata={"source": "e_observability.demo"},
        )

    print(json.dumps(trace.snapshot(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

