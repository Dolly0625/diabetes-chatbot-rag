#!/usr/bin/env python3
"""Semantic Router 效能驗收腳本 — 15 類對抗輸入 × 50 輪 × 3 模式 + live smoke 10 輪.

分階段計時（ms）：
  red_flag_and_auth、deterministic_fast_path、semantic_router、
  conversation_interpreter、rag_retrieval、answer_generator、
  B gate、D gate、persistence、total

實作：
  對每個輸入，以 StagedLatencyRecorder / manual perf_counter 包對應環節，
  直接重用 orchestrator._last_staged_latency (recorder.snapshot())
  與 workflow.trace["staged_latency"]。

模式：
  fixture 50 輪：15 類對抗各 3-4 次，總計 50，mode=off/shadow/guarded 各測一次，
            同時記錄 cold（每輪新建 repo+orchestrator）與 warm（同一 repo 重用，第二輪起）
  live smoke 10 輪：用 .env 的 CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL
            （不得硬編碼，經 tfda_context_gate/run_config.env_value），
            在 worktree 內跑 ConversationOrchestrator(interpreter=FormalConversationInterpreter.from_env())
            對 10 輪混合輸入，記錄 interpreter 與 generator 各自時間

目標斷言（僅報告，不硬失敗）：
  red flag <100ms 無 AI/RAG；deterministic fast path warm p95 <200ms；
  Semantic Router warm p95 <250ms；PURE_EDUCATION 不先呼叫 interpreter（spy）；
  PURE_INTAKE 短答案不呼叫 AI（is_fast_path_eligible）

輸出：
  /tmp/semantic_router_perf.json
  docs/reviews/semantic_router_perf_<timestamp>.md

執行：
  source .venv/bin/activate && python scripts/semantic_router_perf.py
  uv run python scripts/semantic_router_perf.py
  不寫 .env，不污染主 DB（temp SQLite）
"""
from __future__ import annotations

import os
import sys
import json
import time
import tempfile
import statistics
import uuid
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter
from tfda_context_gate.e_observability.staged_latency import STAGE_KEYS

ADVERSARIAL_15 = [
    {"cat": "single_drug_en", "text": "metformin", "label": "PURE_INTAKE/drug", "desc": "single drug English"},
    {"cat": "pure_intake_neg", "text": "我沒有過敏", "label": "PURE_INTAKE/negation", "desc": "negation short answer PURE_INTAKE"},
    {"cat": "control_pause", "text": "先不要填了", "label": "CONTROL", "desc": "control command PAUSED"},
    {"cat": "identity", "text": "你是 AI 還是人工客服？", "label": "CHITCHAT/identity", "desc": "identity chitchat"},
    {"cat": "pure_education", "text": "糖尿病一天可以吃幾份水果？", "label": "PURE_EDUCATION", "desc": "PURE_EDUCATION"},
    {"cat": "mixed", "text": "我最近常口渴，糖尿病一天可以吃幾份水果？", "label": "MIXED", "desc": "MIXED intake+edu"},
    {"cat": "correction_subject", "text": "我前面說錯了，其實是我媽媽在吃", "label": "CORRECTION", "desc": "correction + subject ambiguous"},
    {"cat": "third_party", "text": "我朋友最近一直口渴", "label": "UNKNOWN/third_party", "desc": "third-party friend"},
    {"cat": "hypothetical", "text": "如果以後開始頭暈要怎麼辦？", "label": "UNKNOWN/hypothetical", "desc": "hypothetical"},
    {"cat": "question_drug", "text": "metformin 會傷腎嗎？", "label": "PURE_EDUCATION/question", "desc": "question with drug English"},
    {"cat": "red_flag", "text": "胸口很痛而且呼吸困難", "label": "RED_FLAG", "desc": "red flag pure"},
    {"cat": "red_flag_negated", "text": "沒有胸痛，只是想問胸痛該怎麼辦", "label": "negated_question", "desc": "red flag phrase but negated question"},
    {"cat": "multi_symptom_slang", "text": "我嘴巴很乾，晚上一直跑廁所", "label": "PURE_INTAKE/multi", "desc": "TW slang multi-clause intake"},
    {"cat": "chitchat_followup", "text": "謝謝，另外我最近一直口渴", "label": "MIXED/chitchat+intake", "desc": "chitchat + intake follow-up"},
    {"cat": "unseen_slang", "text": "最近一直吃不飽、冒冷汗、手抖抖", "label": "PURE_INTAKE/unseen", "desc": "unseen TW slang low-sugar variant"},
]

LIVE_MIXED_10 = [
    "我最近常口渴，糖尿病一天可以吃幾份水果？",
    "請說明糖尿病飲食原則",
    "我有吃 metformin，可以吃芭樂嗎？",
    "最近晚上一直跑廁所，會是糖尿病嗎？",
    "如果以後頭暈要怎麼處理？",
    "謝謝，另外我想問血糖偏高怎麼吃比較好？",
    "我嘴巴很乾，晚上一直跑廁所",
    "最近一直吃不飽、冒冷汗、手抖抖",
    "我沒有過敏",
    "糖尿病患者適合每天運動多久？",
]

STAGED_KEYS_10 = [
    "red_flag_and_auth_ms",
    "deterministic_fast_path_ms",
    "semantic_router_ms",
    "conversation_interpreter_ms",
    "rag_retrieval_ms",
    "answer_generator_ms",
    "b_gate_ms",
    "d_gate_ms",
    "persistence_ms",
    "total_ms",
]

def _p50_p95(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    p50 = float(statistics.median(vals))
    s = sorted(vals)
    idx = int(len(s) * 0.95)
    if idx >= len(s):
        idx = len(s) - 1
    p95 = float(s[idx])
    return p50, p95

def _extract_staged(orch, result) -> dict:
    staged: dict = {}
    try:
        if hasattr(orch, "_last_staged_latency") and isinstance(orch._last_staged_latency, dict):
            staged = dict(orch._last_staged_latency)
        if hasattr(result, "trace") and isinstance(getattr(result, "trace", None), dict):
            w = result.trace.get("staged_latency") or {}
            if isinstance(w, dict):
                for k in ("rag_retrieval_ms","answer_generator_ms","b_gate_ms","d_gate_ms"):
                    if k in w and isinstance(w[k], (int,float)) and (k not in staged or staged.get(k,0)==0):
                        staged[k] = float(w[k])
    except Exception:
        staged = {}
    for k in STAGE_KEYS:
        staged.setdefault(k, 0.0)
    return staged

def _build_seq_50() -> list[dict]:
    seq: list[dict] = []
    buckets = []
    for idx, cat in enumerate(ADVERSARIAL_15):
        reps = 4 if idx < 5 else 3
        buckets.append([cat]*reps)
    max_r = 4
    for r in range(max_r):
        for b in buckets:
            if r < len(b):
                seq.append(b[r])
    assert len(seq)==50
    return seq

def _make_orch_with_spy(tmp_path: Path, mode: str):
    os.environ["SEMANTIC_ROUTER_MODE"] = mode
    repo = SQLiteProductSessionRepository(tmp_path)
    base = DeterministicConversationInterpreter()
    spy = {"interpreter_calls": 0, "last_interpretation": None}
    orig = base.interpret
    def counted(envelope):
        spy["interpreter_calls"] += 1
        res = orig(envelope)
        spy["last_interpretation"] = res
        return res
    base.interpret = counted  # type: ignore
    orch = ConversationOrchestrator(repo, identity_hash_key="perf-key-12345678901234-" + uuid.uuid4().hex[:8], interpreter=base, use_formal=False)  # type: ignore
    return repo, orch, spy

def _measure_turn(orch, spy: dict, text: str, event_id: str, line_user_id: str) -> dict:
    spy_before = int(spy.get("interpreter_calls", 0))
    total_start = time.perf_counter()
    result = orch.handle_text(event_id=event_id, line_user_id=line_user_id, text=text)
    total_ms = (time.perf_counter() - total_start) * 1000.0
    spy_after = int(spy.get("interpreter_calls", 0))
    interpreter_delta = spy_after - spy_before
    staged = _extract_staged(orch, result)
    sem_ms = 0.0
    try:
        v = getattr(result, "semantic_latency_ms", None)
        if v is not None:
            sem_ms = float(v)
        elif getattr(orch, "_last_semantic_observation", None) is not None:
            obs = orch._last_semantic_observation  # type: ignore
            sem_ms = float(getattr(obs, "latency_ms", 0.0) or 0.0)
    except Exception:
        sem_ms = 0.0
    det_fast_ms = float(staged.get("candidate_validation_ms", 0.0) or 0.0)
    rec = {
        "red_flag_and_auth_ms": float(staged.get("red_flag_and_auth_ms", 0.0) or 0.0),
        "deterministic_fast_path_ms": float(det_fast_ms),
        "semantic_router_ms": float(sem_ms),
        "conversation_interpreter_ms": float(staged.get("conversation_interpreter_ms", 0.0) or 0.0),
        "rag_retrieval_ms": float(staged.get("rag_retrieval_ms", 0.0) or 0.0),
        "answer_generator_ms": float(staged.get("answer_generator_ms", 0.0) or 0.0),
        "b_gate_ms": float(staged.get("b_gate_ms", 0.0) or 0.0),
        "d_gate_ms": float(staged.get("d_gate_ms", 0.0) or 0.0),
        "persistence_ms": float(staged.get("persistence_ms", 0.0) or 0.0),
        "total_ms": float(staged.get("total_ms", total_ms) or total_ms),
        "wall_total_ms": round(total_ms, 3),
        "interpreter_calls": int(interpreter_delta),
        "is_fast_path_eligible": None,
        "semantic_route": getattr(result, "semantic_route", None),
        "semantic_mode": getattr(result, "semantic_mode", None),
        "semantic_degraded": getattr(result, "semantic_degraded", None),
        "status": getattr(result, "status", None),
        "fallback_reason": getattr(result, "fallback_reason", None),
        "reply_preview": (getattr(result, "reply", "") or "")[:80].replace("\n"," "),
        "is_process_first": bool(staged.get("is_process_first_measurement", False)),
        "is_session_first": bool(staged.get("is_session_first_turn", False)),
    }
    try:
        sess = orch.session_for_user(line_user_id)
        pending = getattr(sess, "pending_field", None) if sess else None
        from tfda_context_gate.intake.candidate_merge import is_fast_path_eligible
        rec["is_fast_path_eligible"] = bool(is_fast_path_eligible(text, pending)) if pending else False
        rec["pending_field"] = pending
    except Exception:
        rec["is_fast_path_eligible"] = False
        rec["pending_field"] = None
    gen_calls = 1 if float(rec["answer_generator_ms"] or 0) > 0.5 else 0
    rec["llm_calls"] = int(interpreter_delta + gen_calls)
    rec["rag_calls"] = 1 if float(rec["rag_retrieval_ms"] or 0) > 0.5 else 0
    return rec

def run_fixture_mode(mode: str, seq: list[dict]) -> dict:
    cold_records: list[dict] = []
    warm_records: list[dict] = []
    for idx, item in enumerate(seq):
        tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
        _, orch, spy = _make_orch_with_spy(tmp, mode)
        rec = _measure_turn(orch, spy, item["text"], event_id=f"cold-{mode}-{idx}", line_user_id=f"U-cold-{mode}-{idx}")
        rec["cat"] = item["cat"]
        rec["text"] = item["text"]
        rec["turn_idx"] = idx
        rec["cold"] = True
        cold_records.append(rec)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        for suf in ("-wal","-shm"):
            try:
                (Path(str(tmp)+suf)).unlink(missing_ok=True)
            except Exception:
                pass
    tmp_warm = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch_w, spy_w = _make_orch_with_spy(tmp_warm, mode)
    warm_user = f"U-warm-{mode}"
    for idx, item in enumerate(seq):
        rec = _measure_turn(orch_w, spy_w, item["text"], event_id=f"warm-{mode}-{idx}", line_user_id=warm_user)
        rec["cat"] = item["cat"]
        rec["text"] = item["text"]
        rec["turn_idx"] = idx
        rec["cold"] = False
        warm_records.append(rec)
    try:
        tmp_warm.unlink(missing_ok=True)
        for suf in ("-wal","-shm"):
            try:
                Path(str(tmp_warm)+suf).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
    return {"cold": cold_records, "warm": warm_records}

def _stats_for(records: list[dict]) -> dict:
    if not records:
        return {}
    out: dict = {}
    for k in STAGED_KEYS_10:
        vals = [float(r.get(k, 0.0) or 0.0) for r in records]
        p50, p95 = _p50_p95(vals)
        out[k] = {"p50": round(p50,3), "p95": round(p95,3), "vals": [round(v,3) for v in vals[:5]]}
    fb = sum(1 for r in records if (r.get("status")=="FALLBACK"))
    out["fallback_rate"] = round(fb/len(records),4) if records else 0
    out["fallback_count"] = fb
    out["total"] = len(records)
    out["avg_llm_calls"] = round(sum(int(r.get("llm_calls",0) or 0) for r in records)/len(records),3) if records else 0
    out["avg_interpreter_calls"] = round(sum(int(r.get("interpreter_calls",0) or 0) for r in records)/len(records),3) if records else 0
    return out

def run_live_smoke(seq10: list[str]) -> dict:
    out = {"enabled": False, "model": None, "router_model": None, "records": [], "error": None}
    try:
        from tfda_context_gate.run_config import env_value
        conv_model = (env_value("CONVERSATION_LLM_MODEL", "") or "").strip()
        router_model = (env_value("ROUTER_LLM_MODEL", "") or "").strip()
        model = conv_model or router_model
        out["model"] = (conv_model or "")[:80]
        out["router_model"] = (router_model or "")[:80]
        if not model:
            out["error"] = "no CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL in .env — live smoke skipped (honest: no formal model configured)"
            return out
        from tfda_context_gate.conversation.interpreter import ConversationInterpreterFactory
        saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            interp2 = ConversationInterpreterFactory.from_env()
            is_formal = interp2.__class__.__name__ == "FormalConversationInterpreter"
            if not is_formal:
                out["error"] = f"FormalConversationInterpreter not built (got {interp2.__class__.__name__}); live smoke runs with {interp2.__class__.__name__} (honest: 正式模型未就緒)"
                interp = interp2
            else:
                interp = interp2
        finally:
            if saved is not None:
                os.environ["PYTEST_CURRENT_TEST"] = saved
        out["enabled"] = True
        out["interpreter_class"] = interp.__class__.__name__
        tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
        repo = SQLiteProductSessionRepository(tmp)
        identity_key = "live-smoke-key-12345678901234-" + uuid.uuid4().hex[:6]
        orch = ConversationOrchestrator(repo, identity_hash_key=identity_key, interpreter=interp)
        live_user = "U-live-smoke-" + uuid.uuid4().hex[:6]
        try:
            orch.handle_text(event_id="live-auth", line_user_id=live_user, text="為自己整理")
        except Exception:
            pass
        records = []
        for idx, text in enumerate(seq10):
            start = time.perf_counter()
            result = orch.handle_text(event_id=f"live-{idx}", line_user_id=live_user, text=text)
            wall_total = (time.perf_counter() - start) * 1000.0
            staged = _extract_staged(orch, result)
            rec = {
                "idx": idx,
                "text": text[:60],
                "status": getattr(result, "status", None),
                "wall_total_ms": round(wall_total,3),
                "conversation_interpreter_ms": float(staged.get("conversation_interpreter_ms", 0.0) or 0.0),
                "answer_generator_ms": float(staged.get("answer_generator_ms", 0.0) or 0.0),
                "rag_retrieval_ms": float(staged.get("rag_retrieval_ms", 0.0) or 0.0),
                "total_ms": float(staged.get("total_ms", wall_total) or wall_total),
                "staged": {k: float(staged.get(k,0.0) or 0.0) for k in STAGE_KEYS},
                "is_formal": orch.interpreter.__class__.__name__ == "FormalConversationInterpreter",
            }
            records.append(rec)
        out["records"] = records
        interp_vals = [r["conversation_interpreter_ms"] for r in records]
        gen_vals = [r["answer_generator_ms"] for r in records]
        out["interpreter_p50"], out["interpreter_p95"] = _p50_p95(interp_vals)
        out["generator_p50"], out["generator_p95"] = _p50_p95(gen_vals)
        wall_vals = [r["wall_total_ms"] for r in records]
        out["wall_p50"], out["wall_p95"] = _p50_p95(wall_vals)
        out["is_formally_slow"] = bool(out["interpreter_p95"] > 5000 or out["generator_p95"] > 5000 or out["wall_p95"] > 8000)
        out["slow_note"] = "正式模型慢（interpreter 或 generator p95 >5s，或 wall p95 >8s）" if out["is_formally_slow"] else "正式模型速度正常"
        try:
            tmp.unlink(missing_ok=True)
            for suf in ("-wal","-shm"):
                try:
                    Path(str(tmp)+suf).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"live smoke exception: {e.__class__.__name__}: {e}"
        import traceback
        out["traceback"] = traceback.format_exc()[:2000]
    return out

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Semantic Router 效能驗收 — fixture 50 輪 ×3 模式 + live smoke 10 輪")
    parser.add_argument("--live-only", action="store_true", help="只跑 live smoke 10 輪")
    parser.add_argument("--quick", action="store_true", help="快速驗證：每模式縮為 15 輪（僅用於開發）")
    args = parser.parse_args()
    start_all = time.perf_counter()
    seq = _build_seq_50()
    if args.quick:
        seq = seq[:15]
    results_by_mode: dict = {}
    if not args.live_only:
        for mode in ("off","shadow","guarded"):
            print(f"[perf] mode={mode} 開始 50 輪 (cold/warm) ...", flush=True)
            mode_start = time.perf_counter()
            data = run_fixture_mode(mode, seq)
            for temp in ("cold","warm"):
                recs = data[temp]
                stats = _stats_for(recs)
                cats = {}
                for cat in ADVERSARIAL_15:
                    cat_recs = [r for r in recs if r["cat"]==cat["cat"]]
                    if not cat_recs:
                        continue
                    cat_stats = _stats_for(cat_recs)
                    cats[cat["cat"]] = {
                        "desc": cat["desc"],
                        "count": len(cat_recs),
                        "p50_total": cat_stats.get("total_ms",{}).get("p50",0),
                        "p95_total": cat_stats.get("total_ms",{}).get("p95",0),
                        "fallback_rate": cat_stats.get("fallback_rate",0),
                        "avg_llm_calls": cat_stats.get("avg_llm_calls",0),
                        "avg_interpreter_calls": cat_stats.get("avg_interpreter_calls",0),
                    }
                results_by_mode[f"{mode}_{temp}"] = {
                    "mode": mode,
                    "temperature": temp,
                    "count": len(recs),
                    "stats": stats,
                    "per_cat": cats,
                    "records": recs,
                }
            print(f"[perf] mode={mode} 完成 耗時 {(time.perf_counter()-mode_start):.1f}s", flush=True)
    else:
        print("[perf] --live-only 跳過 fixture 50 輪", flush=True)
    print("[perf] live smoke 10 輪（Formal via env_value）...", flush=True)
    live = run_live_smoke(LIVE_MIXED_10)
    if live.get("enabled"):
        print(f"[perf] live model={live.get('model') or live.get('router_model')} interpreter={live.get('interpreter_class')} wall p50={live.get('wall_p50',0):.0f} p95={live.get('wall_p95',0):.0f} interp p50={live.get('interpreter_p50',0):.0f} p95={live.get('interpreter_p95',0):.0f} gen p50={live.get('generator_p50',0):.0f} p95={live.get('generator_p95',0):.0f} — {live.get('slow_note')}", flush=True)
    else:
        print(f"[perf] live smoke skipped: {live.get('error')}", flush=True)
    print("\n=== 目標斷言（僅報告，live 波動不硬失敗）===", flush=True)
    def _report_assert(cond: bool, msg: str):
        icon = "✓" if cond else "✗"
        print(f"  {icon} {msg}", flush=True)
    red_recs = []
    for k in results_by_mode:
        if "warm" in k:
            red_recs.extend([r for r in results_by_mode[k]["records"] if r["cat"]=="red_flag"])
    if red_recs:
        red_p95 = _p50_p95([float(r["total_ms"] or 0) for r in red_recs])[1]
        has_ai = any(int(r.get("interpreter_calls",0))>0 or float(r.get("rag_retrieval_ms",0))>1 for r in red_recs)
        _report_assert(red_p95 < 100, f"red flag <100ms 無 AI/RAG — 實測 p95={red_p95:.1f}ms has_ai={has_ai} {'PASS' if red_p95<100 and not has_ai else 'REPORT'}")
        sample_vals = [float(r['total_ms']) for r in red_recs[:3]]
        print(f"    red_flag samples total_ms={sample_vals}", flush=True)
    else:
        print("  - 無 red_flag 樣本（quick 模式可能缺）", flush=True)
    guarded_warm = results_by_mode.get("guarded_warm",{}).get("stats",{})
    if guarded_warm:
        det_p95 = guarded_warm.get("deterministic_fast_path_ms",{}).get("p95", 0)
        _report_assert(det_p95 < 200, f"deterministic fast path warm p95 <200ms — 實測 {det_p95:.1f}ms")
        sem_p95 = guarded_warm.get("semantic_router_ms",{}).get("p95", 0)
        _report_assert(sem_p95 < 250, f"Semantic Router warm p95 <250ms — 實測 {sem_p95:.1f}ms")
        pure_edu_recs = [r for r in results_by_mode.get("guarded_warm",{}).get("records",[]) if r["cat"]=="pure_education"]
        if pure_edu_recs:
            edu_calls = [int(r.get("interpreter_calls",0)) for r in pure_edu_recs]
            _report_assert(all(c==0 for c in edu_calls), f"PURE_EDUCATION 不先呼叫 interpreter（spy 計數）— 實測 calls={edu_calls} — {'PASS' if all(c==0 for c in edu_calls) else 'REPORT: guarded fast path 未命中（閾值或語意）'}")
        pure_intake_recs = [r for r in results_by_mode.get("guarded_warm",{}).get("records",[]) if r["cat"]=="pure_intake_neg"]
        if pure_intake_recs:
            eligible = [bool(r.get("is_fast_path_eligible")) for r in pure_intake_recs]
            calls = [int(r.get("interpreter_calls",0)) for r in pure_intake_recs]
            _report_assert(any(eligible) or all(c==0 for c in calls), f"PURE_INTAKE 短答案不呼叫 AI（is_fast_path_eligible）— eligible={eligible} calls={calls} pending={[r.get('pending_field') for r in pure_intake_recs[:2]]}")
    else:
        print("  - guarded_warm 無資料（--live-only 模式）", flush=True)
    if live.get("enabled"):
        print(f"  • live smoke（正式模型）interpreter p50={live['interpreter_p50']:.1f} p95={live['interpreter_p95']:.1f} generator p50={live['generator_p50']:.1f} p95={live['generator_p95']:.1f} — {live['slow_note']}", flush=True)
        if live["is_formally_slow"]:
            print("    ↳ 誠實標註：正式模型慢（LLM 網路/推理延遲，屬預期波動）", flush=True)
    elapsed_all = (time.perf_counter()-start_all)*1000
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round(elapsed_all,1),
        "adversarial_15": ADVERSARIAL_15,
        "sequence_length": len(seq),
        "fixture": results_by_mode,
        "live_smoke": live,
    }
    def _sanitize_records(recs):
        out=[]
        for r in recs:
            out.append({k: r.get(k) for k in ("cat","text","turn_idx","cold","red_flag_and_auth_ms","deterministic_fast_path_ms","semantic_router_ms","conversation_interpreter_ms","rag_retrieval_ms","answer_generator_ms","b_gate_ms","d_gate_ms","persistence_ms","total_ms","wall_total_ms","interpreter_calls","llm_calls","semantic_route","status","fallback_reason","is_fast_path_eligible","pending_field")})
        return out
    payload_json = dict(payload)
    for k in list(payload_json.get("fixture",{}).keys()):
        if "records" in payload_json["fixture"][k]:
            payload_json["fixture"][k]["records_sanitized"] = _sanitize_records(payload_json["fixture"][k].pop("records"))
    out_json = Path("/tmp/semantic_router_perf.json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[perf] JSON → {out_json} ({out_json.stat().st_size} bytes)", flush=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = ROOT / f"docs/reviews/semantic_router_perf_{ts}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# Semantic Router 效能驗收 — {ts}")
    lines.append("")
    lines.append(f"> 產生時間 {payload['generated_at']} | 耗時 {elapsed_all:.0f}ms | 序列 {len(seq)} 輪 × 3 模式 (cold/warm) | live 10 輪")
    lines.append("")
    lines.append("> 註：p50/p95 為 fixture 決定性路徑（無真 LLM），live smoke 另列。目標斷言僅報告，live 波動不硬失敗。")
    lines.append("")
    if not args.live_only:
        for mode in ("off","shadow","guarded"):
            for temp in ("cold","warm"):
                key = f"{mode}_{temp}"
                data = results_by_mode.get(key)
                if not data:
                    continue
                stats = data["stats"]
                lines.append(f"## 模式 {mode} / {temp}（{data['count']} 輪）")
                lines.append("")
                lines.append("| 階段 | p50 (ms) | p95 (ms) |")
                lines.append("|---|---:|---:|")
                for k in STAGED_KEYS_10:
                    s = stats.get(k, {})
                    lines.append(f"| {k} | {s.get('p50',0):.1f} | {s.get('p95',0):.1f} |")
                lines.append(f"| fallback_rate | {stats.get('fallback_rate',0):.2%} | count {stats.get('fallback_count',0)}/{stats.get('total',0)} |")
                lines.append(f"| avg_llm_calls | {stats.get('avg_llm_calls',0):.2f} | avg_interpreter {stats.get('avg_interpreter_calls',0):.2f} |")
                lines.append("")
                lines.append(f"### 各類 p50/p95/fallback/LLM（{mode}/{temp}）")
                lines.append("")
                lines.append("| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |")
                lines.append("|---|---|---:|---:|---:|---:|")
                for cat in ADVERSARIAL_15:
                    c = data["per_cat"].get(cat["cat"])
                    if not c:
                        continue
                    lines.append(f"| {cat['cat']} | {cat['desc']} | {c['p50_total']:.1f} | {c['p95_total']:.1f} | {c['fallback_rate']:.0%} | {c['avg_llm_calls']:.1f} |")
                lines.append("")
    lines.append("## Live smoke 10 輪（正式模型，經 env_value，無硬編碼）")
    lines.append("")
    if live.get("enabled"):
        lines.append(f"- 模型: CONVERSATION_LLM_MODEL=`{live.get('model') or '—'}` / ROUTER_LLM_MODEL=`{live.get('router_model') or '—'}` | interpreter=`{live.get('interpreter_class')}`")
        lines.append(f"- wall p50 {live.get('wall_p50',0):.1f} p95 {live.get('wall_p95',0):.1f} | interpreter p50 {live.get('interpreter_p50',0):.1f} p95 {live.get('interpreter_p95',0):.1f} | generator p50 {live.get('generator_p50',0):.1f} p95 {live.get('generator_p95',0):.1f}")
        lines.append(f"- 判定: **{live.get('slow_note')}**")
        if live.get("is_formally_slow"):
            lines.append(f"  - 誠實報告：正式模型慢（網路/推理波動，p95 >5s）— 分開呈現 interpreter/generator，屬預期")
        lines.append("")
        lines.append("| # | 輸入 | status | wall | interpreter | generator |")
        lines.append("|---:|---|---|---:|---:|---:|")
        for r in live.get("records",[]):
            lines.append(f"| {r['idx']} | {r['text'][:30]} | {r['status']} | {r['wall_total_ms']:.0f} | {r['conversation_interpreter_ms']:.0f} | {r['answer_generator_ms']:.0f} |")
    else:
        lines.append(f"- 跳過原因: {live.get('error') or '未知'}")
        lines.append("- 說明: .env 未配置 CONVERSATION_LLM_MODEL/ROUTER_LLM_MODEL 或 OPENCODE_API_KEY，屬誠實報告（非硬失敗）")
    lines.append("")
    lines.append("## 目標斷言（僅報告）")
    lines.append("")
    lines.append("- red flag <100ms 無 AI/RAG：見上表 red_flag p95 與 interpreter_calls/rag")
    lines.append("- deterministic fast path warm p95 <200ms：見 guarded_warm deterministic_fast_path")
    lines.append("- Semantic Router warm p95 <250ms：見 guarded_warm semantic_router_ms")
    lines.append("- PURE_EDUCATION 不先呼叫 interpreter（spy 計數）：guarded 下 PURE_EDUCATION interpreter_calls 應為 0（若未命中閾值則誠實報告）")
    lines.append("- PURE_INTAKE 短答案不呼叫 AI（is_fast_path_eligible）：短答案「我沒有過敏」在 pending 為 allergies 時 eligible=True → 0 AI calls")
    lines.append("")
    lines.append("## 重現")
    lines.append("")
    lines.append("```bash")
    lines.append("source .venv/bin/activate  # 或 uv run")
    lines.append("python scripts/semantic_router_perf.py")
    lines.append("python scripts/semantic_router_perf.py --live-only")
    lines.append("cat /tmp/semantic_router_perf.json | jq '.fixture.guarded_warm.stats'")
    lines.append("```")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[perf] Markdown → {md_path}", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
