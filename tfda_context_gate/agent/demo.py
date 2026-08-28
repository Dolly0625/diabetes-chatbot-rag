"""Runnable Agent v0.1 trajectories.

Offline mode uses local retrieval-shaped fixtures and a scripted Planner only
to make the graph contract testable without network calls. ``--planner llm``
uses native ChatOpenRouter by default; local ChatOllama remains available with
``--provider ollama``.

【繁中註解｜可執行軌跡展示】
- 離線模式：LocalCaseRetriever + DemoContextGate + ScriptedAgentPlanner，僅為驗證圖契約（9 節點/3 條件邊/唯一回環）無需聯網。
- 三條展示軌跡：AG-ASK-001（ASK_USER→NEEDS_CLARIFICATION）、AG-REWRITE-001（REWRITE_QUERY→QUERY_REWRITER→QUERY_EXPANSION 回環）、AG-FALLBACK-001（FALLBACK 封閉）。
- 有界性：AGENT_LIMITS（max_agent_steps=2/max_rewrites=1/max_clarifications=1）由 run_workflow 持有，Planner 不可改。
- 重寫安全：DeterministicQueryRewriter 僅做映射，LLM 模式下仍經 validate_meaning_preserving_rewrite 校驗。
- 追問映射：build_agent_question 將 B 的 identified_missing_information 轉為封閉式追問句，不猜測醫療事實。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tfda_context_gate.agent import (
    AGENT_MODEL,
    DeterministicQueryRewriter,
    FallbackDecision,
    LangChainAgentPlanner,
    LangChainQueryRewriter,
    QueryRewriter,
    RewriteQueryDecision,
    ScriptedAgentPlanner,
    AskUserDecision,
    OLLAMA_AGENT_MODEL,
    build_agent_ollama_llm,
    build_agent_openrouter_llm,
)
from tfda_context_gate.agent.agent_demo_case_schema import AgentDemoCase, load_agent_demo_cases
from tfda_context_gate.b_context_gate.gate import ContextGate
from tfda_context_gate.b_context_gate.schemas import CanonicalBInput, CanonicalBResult, CanonicalEvidence
from tfda_context_gate.e_observability import format_trace_trajectory
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag import Retriever, TFDADrugSafetyRetriever
from tfda_context_gate.rag.schemas import RAGResult
from tfda_context_gate.workflow import run_workflow


class DemoContextGate:
    """Explicit non-production B fixture for the three ground-truth cases.

    【繁中註解】非生產 B 夾具：僅依 query 與 evidence_id 決定 PASS/INSUFFICIENT，中性回填
    identified_missing_information，是否 ASK_USER 仍由 Planner 三選一決定，B 不發控制指令。
    """

    name = "agent-demo-context-gate-fixture"

    def __init__(self, *, identified_missing_information: list[str] | None = None) -> None:
        # This is a neutral B observation supplied by the case fixture. The
        # Planner still makes the ASK_USER/REWRITE_QUERY/FALLBACK decision.
        self.identified_missing_information = list(identified_missing_information or [])

    def evaluate(self, request: CanonicalBInput) -> CanonicalBResult:
        query = " ".join([request.original_query] + request.retrieval_queries)
        ids = [item.evidence_id for item in request.evidence]
        target: str | None = None
        if "Semaglutide" in query:
            target = None
        elif "生殖器或會陰部" in query and "tfda-risk-0064" in ids:
            target = "tfda-risk-0064"
        elif "SGLT2" in query and "腳怪怪" in query and "tfda-risk-0042" in ids:
            target = "tfda-risk-0042"

        if target is None:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="INSUFFICIENT",
                evidence=request.evidence,
                reason_codes=["CONTEXT_INSUFFICIENT", "DEMO_NO_CASE_MATCH"],
                identified_missing_information=self.identified_missing_information,
                retrieval_feedback={"retrieval_queries": request.retrieval_queries},
                relevance="UNKNOWN",
                sufficiency="INSUFFICIENT",
                safety="NOT_ASSESSED",
            )
        return CanonicalBResult(
            request_id=request.request_id,
            decision="PASS",
            approved_evidence_ids=[target],
            evidence=request.evidence,
            reason_codes=["B_CONTEXT_CONTRACT_VALID", "DEMO_CASE_MATCH"],
            identified_missing_information=[],
            retrieval_feedback={"retrieval_queries": request.retrieval_queries},
            relevance="RETRIEVED",
            sufficiency="SUFFICIENT",
            safety="DEMO_APPROVED",
        )


class LocalCaseRetriever:
    """Retrieval-shaped offline fixture mirroring the documented rank changes.

    【繁中註解】離線檢索夾具：依 current_query 是否含「生殖器或會陰部」/「SGLT2+腳怪怪」/「Semaglutide」
    回傳不同排序的 evidence，供展示唯一回環（重寫後重檢索）與 B 判定變化。
    """

    name = "agent-case-retriever-fixture"

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        query = request.retrieval_queries[0]
        semaglutide = "Semaglutide" in query
        rewrite_hit = "生殖器或會陰部" in query
        clarified = "SGLT2" in query and "腳怪怪" in query
        if semaglutide:
            ids = ["tfda-risk-0027", "tfda-risk-0042", "tfda-risk-0064"]
        elif rewrite_hit:
            ids = ["tfda-risk-0064", "tfda-risk-0042", "tfda-risk-0019"]
        elif clarified:
            ids = ["tfda-risk-0042", "tfda-risk-0064", "tfda-risk-0019"]
        else:
            ids = ["tfda-risk-0042", "tfda-risk-0026", "tfda-risk-0064"]
        evidence = [
            CanonicalEvidence(
                evidence_id=evidence_id,
                content=(
                    "TFDA demo evidence summary for "
                    + ("SGLT2 抑制劑類" if "SGLT2" in query else evidence_id)
                    + "。"
                ),
                source="TFDA-demo-fixture",
                metadata={"藥品成分": "SGLT2抑制劑類" if "SGLT2" in query else evidence_id},
                score=0.90 - index * 0.005,
                date="2018/9/28",
            )
            for index, evidence_id in enumerate(ids)
        ]
        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=request.retrieval_queries,
            evidence=evidence,
            retrieval_latency_ms=0,
        )


def _fixture_planner(case: AgentDemoCase) -> ScriptedAgentPlanner:
    expected_action = case.expected_agent_action
    if expected_action == "ASK_USER":
        decisions = [
            AskUserDecision(
                action="ASK_USER",
                reason_code="MISSING_REQUIRED_CONTEXT",
                missing_information=case.identified_missing_information or ["missing_information"],
            )
        ]
    elif expected_action == "REWRITE_QUERY":
        decisions = [
            RewriteQueryDecision(
                action="REWRITE_QUERY",
                reason_code="QUERY_FORMULATION_NEEDS_REWRITE",
            )
        ]
    else:
        decisions = []
        if any(attempt.get("action") == "REWRITE_QUERY" for attempt in case.recovery_attempts):
            decisions.append(
                RewriteQueryDecision(
                    action="REWRITE_QUERY",
                    reason_code="QUERY_FORMULATION_NEEDS_REWRITE",
                )
            )
        decisions.append(FallbackDecision(action="FALLBACK", reason_code="RECOVERY_EXHAUSTED"))
    return ScriptedAgentPlanner(decisions)


def _fixture_rewriter(case: AgentDemoCase) -> QueryRewriter:
    mapping: dict[str, str] = {}
    if case.rewritten_query:
        mapping[case.user_query] = case.rewritten_query
    for attempt in case.recovery_attempts:
        if attempt.get("action") == "REWRITE_QUERY" and attempt.get("query"):
            mapping.setdefault(case.user_query, str(attempt["query"]))
    return DeterministicQueryRewriter(mapping)


def _real_components(provider: str) -> tuple[Any, Any]:
    if provider == "ollama":
        llm = build_agent_ollama_llm()
        return LangChainAgentPlanner.from_ollama(llm), LangChainQueryRewriter.from_ollama(llm)
    llm = build_agent_openrouter_llm()
    return LangChainAgentPlanner.from_llm(llm), LangChainQueryRewriter.from_llm(llm)


def _request(case: AgentDemoCase, *, request_id: str, text: str | None = None) -> dict[str, str]:
    return {
        "request_id": request_id,
        "schema_version": "a.v0.1",
        "user_raw_input": text or case.user_query,
        "declared_role": case.role,
        "language": "zh-TW",
    }


def _print_trajectory(
    label: str,
    result: Any,
    *,
    show_trace: bool = False,
    trace_output: Path | None = None,
) -> None:
    print(f"\nCASE: {label}")
    for event in result.trace["events"]:
        component = event["component"]
        status = event["status"]
        if component in {"A", "RAG", "B", "AGENT", "QUERY_REWRITER", "C", "D"} and status != "STARTED":
            details = {
                key: event[key]
                for key in (
                    "router_status", "retrieval_query", "decision", "agent_action",
                    "reason_codes", "termination_reason", "candidate_decision",
                )
                if event.get(key) not in (None, [], "")
            }
            print(f"{component}: {status} {json.dumps(details, ensure_ascii=False)}")
    print(f"FINAL: {result.status} {result.final_response}")
    trajectory = format_trace_trajectory(result.trace)
    if show_trace:
        print(trajectory)
    if trace_output is not None:
        trace_output.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "case_label": label,
            "status": result.status,
            "final_response": result.final_response,
            "trajectory": trajectory,
            "trace": result.trace,
        }
        with trace_output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def run_case(
    case: AgentDemoCase,
    *,
    planner_mode: str,
    provider: str,
    retriever_mode: str,
    planner: Any | None = None,
    rewriter: Any | None = None,
    show_trace: bool = False,
    trace_output: Path | None = None,
) -> None:
    if planner_mode == "llm":
        if planner is None or rewriter is None:
            planner, rewriter = _real_components(provider)
    else:
        planner, rewriter = _fixture_planner(case), _fixture_rewriter(case)
    retriever: Retriever = (
        TFDADrugSafetyRetriever(top_k=5)
        if retriever_mode == "real"
        else LocalCaseRetriever()
    )
    result = run_workflow(
        _request(case, request_id=f"{case.case_id}-initial"),
        retriever=retriever,
        context_gate=DemoContextGate(
            identified_missing_information=case.identified_missing_information
        ),
        agent_planner=planner,
        query_rewriter=rewriter,
    )
    _print_trajectory(
        case.case_id,
        result,
        show_trace=show_trace,
        trace_output=trace_output,
    )
    if case.simulated_user_reply and result.status == "NEEDS_CLARIFICATION":
        clarified = case.model_extra["clarified_query"]
        follow_up = run_workflow(
            _request(case, request_id=f"{case.case_id}-clarified", text=clarified),
            retriever=retriever,
            context_gate=DemoContextGate(),
        )
        _print_trajectory(
            f"{case.case_id} / re-entry from A",
            follow_up,
            show_trace=show_trace,
            trace_output=trace_output,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded Agent v0.1 LangGraph trajectories.")
    parser.add_argument("--case", choices=("AG-ASK-001", "AG-REWRITE-001", "AG-FALLBACK-001"), default=None)
    parser.add_argument("--planner", choices=("fixture", "llm"), default="fixture")
    parser.add_argument("--provider", choices=("ollama", "openrouter"), default="openrouter")
    parser.add_argument("--retriever", choices=("fixture", "real"), default="fixture")
    parser.add_argument("--show-trace", action="store_true")
    parser.add_argument(
        "--trace-output",
        type=Path,
        default=None,
        help="Append complete trace snapshots and formatted trajectories as JSONL.",
    )
    args = parser.parse_args()
    if args.planner == "llm":
        if args.provider == "ollama":
            print(f"PLANNER: native ChatOllama / {OLLAMA_AGENT_MODEL}")
        else:
            print(f"PLANNER: native ChatOpenRouter / {AGENT_MODEL}")
        shared_planner, shared_rewriter = _real_components(args.provider)
    else:
        shared_planner, shared_rewriter = None, None
    cases = load_agent_demo_cases()
    selected = [case for case in cases if case.expected_agent_action is not None]
    if args.case:
        selected = [case for case in selected if case.case_id == args.case]
    for case in selected:
        run_case(
            case,
            planner_mode=args.planner,
            provider=args.provider,
            retriever_mode=args.retriever,
            planner=shared_planner,
            rewriter=shared_rewriter,
            show_trace=args.show_trace,
            trace_output=args.trace_output,
        )


if __name__ == "__main__":
    main()
