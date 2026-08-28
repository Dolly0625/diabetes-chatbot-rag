"""人類可讀的 E 軌跡渲染（Human-readable trajectory）— 純展示層。

This module is display-only. It does not make routing decisions, mutate graph
state, or expose model hidden reasoning.

本模組為純展示層：
- 只讀取已結構化的 E trace（TraceRecorder.snapshot() 產物），不做任何路由決策
- 不改動 graph 狀態，不暴露模型隱藏推理
- 核心函式 format_trace_trajectory 跳過 STARTED 事件，僅渲染已完成的結構化事件
- 與 workflow/graph.py 的 span 包裝一一對應（見下方 _label 與 format 註解）
"""

from __future__ import annotations

from typing import Any, Mapping


def _value(event: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """取值輔助：若值為 None / "" / [] 則視為缺失，回傳 default。

    用於 trajectory 渲染時過濾空值，避免印出無意義的空欄位。

    參數:
        event: 單筆事件 dict（已 JSON 化的 TraceEvent）
        key: 欄位名
        default: 缺省值

    回傳:
        若 event[key] 為 None/"" /[] 則回傳 default，否則回傳原值
    """
    value = event.get(key, default)
    return default if value in (None, "", []) else value


def _reason_code(event: Mapping[str, Any]) -> str | None:
    """提取原因碼：優先取 reason_code，否則取 reason_codes[0]。

    對應 schemas.TraceEvent 的 reason_code / reason_codes 欄位，
    兼容單一與列表兩種寫法。

    參數:
        event: 單筆事件 dict

    回傳:
        原因碼字串或 None
    """
    return _value(event, "reason_code") or next(
        iter(event.get("reason_codes", [])), None
    )


def _label(event: Mapping[str, Any]) -> str:
    """產生事件的顯示標籤（對應 workflow/graph.py 的 span 包裝）。

    標籤規則與 graph.py 節點對應：
    - RAG → "RAG #<retrieval_attempt>"（對應 rag_node 的 retrieval_attempt）
    - B → "B #<b_attempt>"（對應 b_node 的 b_attempt）
    - AGENT → "AGENT #<step>" 或 "AGENT / ASK_USER"（對應 planner_node / ask_user_node）
    - QUERY_REWRITER → "QUERY_REWRITE"（對應 rewrite_node）
    - SYSTEM → "SYSTEM"（對應 TraceRecorder 的 SYSTEM/request 事件）
    - 其他 → 直接顯示 component 名

    參數:
        event: 單筆事件 dict

    回傳:
        顯示標籤字串
    """
    component = event.get("component", "UNKNOWN")
    status = event.get("status", "")
    if component == "RAG":
        attempt = _value(event, "retrieval_attempt")
        return f"RAG #{attempt}" if attempt is not None else "RAG"
    if component == "B":
        attempt = _value(event, "b_attempt")
        return f"B #{attempt}" if attempt is not None else "B"
    if component == "AGENT":
        step = _value(event, "agent_step") or _value(event, "step_count")
        if event.get("node_name") == "question_builder":
            return "AGENT / ASK_USER"
        return f"AGENT #{step}" if step is not None else "AGENT"
    if component == "QUERY_REWRITER":
        return "QUERY_REWRITE"
    if component == "SYSTEM":
        return "SYSTEM"
    return str(component)


def _append_evidence(lines: list[str], event: Mapping[str, Any]) -> None:
    """附加 RAG 證據摘要（最多 5 筆，僅顯示 id/rank/score/source/date）。

    對應 graph.py 的 _retrieved_evidence_trace() 產生的 retrieved_evidence，
    不含原文，僅為可審計的來源資訊。

    參數:
        lines: 待寫入的行列表（就地附加）
        event: 單筆 RAG 事件 dict
    """
    evidence = event.get("retrieved_evidence", [])
    for item in evidence[:5]:  # 僅顯示前 5 筆，避免過長
        evidence_id = item.get("evidence_id", "?")
        rank = item.get("rank", "?")
        score = item.get("score")
        score_text = f" score={score:.6f}" if isinstance(score, (int, float)) else ""
        source = f" source={item['source']}" if item.get("source") else ""
        date = f" date={item['date']}" if item.get("date") else ""
        lines.append(f"    top{rank}: {evidence_id}{score_text}{source}{date}")


def format_trace_trajectory(trace: Mapping[str, Any]) -> str:
    """將已完成的結構化事件渲染為緊湊的執行軌跡（純展示，跳過 STARTED）。

    特性：
    - 純展示：不做路由、不改狀態、不暴露隱藏推理
    - 跳過 STARTED：僅渲染 COMPLETED / BLOCKED / FALLBACK / ERROR 等已完成事件
      （STARTED 為 TraceSpan.__enter__ 寫入的生命週期起始標記，無需展示）
    - 按 component 分支顯示對應欄位（A / QUERY_EXPANSION / RAG / B / AGENT / ASK_USER / QUERY_REWRITER / C / D / FALLBACK / SYSTEM）

    與 workflow/graph.py 的對應：
    - 每個 graph 節點皆以 trace.span(component, node_name) 包裝，產生的事件在此被渲染
    - 例如 A/input_router → 顯示 route / prompt_guard；RAG/retrieval → 顯示 query 與證據；B/context_gate → 顯示 decision 與四維評估

    參數:
        trace: TraceRecorder.snapshot() 產物，含 request / events / evaluations / metrics

    回傳:
        人類可讀的多行軌跡字串
    """

    request = trace.get("request", {})
    request_id = request.get("request_id", "unknown")
    trace_id = request.get("trace_id") or request_id
    lines = ["=" * 60, f"TRACE: {trace_id} (request_id={request_id})", "=" * 60]
    display_index = 0

    for event in trace.get("events", []):
        if event.get("status") == "STARTED":
            continue  # 【純展示跳過】STARTED 為 span 生命週期起始標記，不計入展示序號
        display_index += 1
        label = _label(event)  # 依 component / attempt / step 產生標籤
        status = event.get("status", "UNKNOWN")
        lines.append(f"\n[{display_index}] {label}")
        lines.append(f"    status: {status}")
        if event.get("latency_ms") is not None:
            lines.append(f"    latency: {event['latency_ms']:.2f} ms")

        component = event.get("component")
        if component == "A":
            if _value(event, "router_status"):
                lines.append(f"    route: {event['router_status']}")
            if _value(event, "prompt_guard_result"):
                lines.append(f"    prompt_guard: {event['prompt_guard_result']}")
        elif component == "QUERY_EXPANSION":
            if _value(event, "retrieval_query"):
                lines.append(f"    query: {event['retrieval_query']}")
        elif component == "RAG":
            if _value(event, "retrieval_query"):
                lines.append(f"    query: {event['retrieval_query']}")
            _append_evidence(lines, event)  # 附加證據摘要（最多 5 筆）
        elif component == "B":
            if _value(event, "decision"):
                lines.append(f"    decision: {event['decision']}")
            if _value(event, "approved_evidence_count") is not None:
                lines.append(f"    approved_evidence_count: {event['approved_evidence_count']}")
            for key in ("relevance", "sufficiency", "conflict", "safety"):
                if _value(event, key) is not None:
                    lines.append(f"    {key}: {event[key]}")
            if _value(event, "reason_codes"):
                lines.append(f"    reason_codes: {', '.join(event['reason_codes'])}")
        elif component == "AGENT":
            planner_context = event.get("planner_context")
            if isinstance(planner_context, Mapping) and planner_context.get(
                "identified_missing_information"
            ):
                lines.append(
                    "    identified_missing_information: "
                    f"{planner_context['identified_missing_information']}"
                )
            if _value(event, "requested_action"):
                lines.append(f"    requested_action: {event['requested_action']}")
            if _value(event, "agent_action"):
                lines.append(f"    action: {event['agent_action']}")
            if _reason_code(event):
                lines.append(f"    reason_code: {_reason_code(event)}")
            for key in ("agent_step", "rewrite_count", "clarification_count"):
                if _value(event, key) is not None:
                    lines.append(f"    {key}: {event[key]}")
            if _value(event, "termination_reason"):
                lines.append(f"    termination_reason: {event['termination_reason']}")
        elif component == "ASK_USER":
            if _value(event, "missing_information"):
                lines.append(f"    missing_information: {event['missing_information']}")
            if _value(event, "question"):
                lines.append(f"    question: {event['question']}")
        elif component == "QUERY_REWRITER":
            if _value(event, "current_query"):
                lines.append(f"    old: {event['current_query']}")
            if _value(event, "rewritten_query"):
                lines.append(f"    new: {event['rewritten_query']}")
            if _value(event, "rewrite_attempt") is not None:
                lines.append(f"    attempt: {event['rewrite_attempt']}")
        elif component == "C":
            if _value(event, "candidate_decision"):
                lines.append(f"    decision: {event['candidate_decision']}")
        elif component == "D":
            if _value(event, "decision"):
                lines.append(f"    decision: {event['decision']}")
        elif component == "FALLBACK":
            if _value(event, "termination_reason"):
                lines.append(f"    termination_reason: {event['termination_reason']}")
            if _value(event, "fallback_reason"):
                lines.append(f"    fallback_reason: {event['fallback_reason']}")
        elif component == "SYSTEM":
            if _value(event, "outcome"):
                lines.append(f"    outcome: {event['outcome']}")
            if _value(event, "fallback_reason"):
                lines.append(f"    fallback_reason: {event['fallback_reason']}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)
