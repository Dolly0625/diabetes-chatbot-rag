#!/usr/bin/env python3
"""P1.1 Live Smoke — 不進 pytest，使用 .env 真實模型測試未見語句。

正式執行（擇一）:
  python -m scripts.p1_live_smoke
  python scripts/p1_live_smoke.py
相容舊用法（shell hack）:
  PYTHONPATH=. python scripts/p1_live_smoke.py

報告: 是否真的使用 Formal、模型名稱（非秘密）、每輪 latency、intents、resolved query、candidate field、confidence、session snapshot、是否 fallback。
不輸出 API key、LINE ID、hash 等秘密。
量測 p50/p95、timeout/fallback 比例，CONVERSATION_LLM_TIMEOUT_S 可由 .env 設定。
不納入 pytest（scripts/ 目錄、檔名非 test_*）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 支援 python scripts/p1_live_smoke.py 直接執行（無需 PYTHONPATH=.）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import time
import hashlib
import statistics
from datetime import datetime, timezone, timedelta

# Ensure .env loaded but not overriding test hermetic for live run
try:
    from dotenv import load_dotenv
    from tfda_context_gate.run_config import PROJECT_ROOT
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from tfda_context_gate.conversation.interpreter import ConversationInterpreterFactory, DeterministicConversationInterpreter
from tfda_context_gate.conversation.envelope import build_conversation_envelope
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.run_config import env_value

def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def _create_orchestrator(tmp: Path):
    repo = SQLiteProductSessionRepository(tmp)
    # Factory will pick CONVERSATION → ROUTER → deterministic ; in live run it should be Formal if .env has ROUTER
    interp = ConversationInterpreterFactory.from_env()
    is_formal = interp.__class__.__name__ == "FormalConversationInterpreter"
    model_name = env_value("CONVERSATION_LLM_MODEL", "") or env_value("ROUTER_LLM_MODEL", "") or "(deterministic)"
    # mask model id: only show provider prefix
    masked = model_name.split("/")[-1] if "/" in model_name else model_name
    print(f"[Factory] interpreter={interp.__class__.__name__} is_formal={is_formal} model={masked} timeout={os.getenv('CONVERSATION_LLM_TIMEOUT_S', '8')}s")
    orch = ConversationOrchestrator(repo, identity_hash_key="live-smoke-key-12345678901234", interpreter=interp)
    return repo, orch, is_formal, masked

def _run_case(orch, repo, case_name, texts):
    latencies = []
    print(f"\n=== {case_name} ===")
    for i, txt in enumerate(texts):
        start = time.time()
        r = orch.handle_text(event_id=f"smoke-{case_name}-{i}", line_user_id=f"U-smoke-{case_name}", text=txt)
        elapsed = (time.time() - start) * 1000
        latencies.append(elapsed)
        sess = orch.session_for_user(f"U-smoke-{case_name}")
        # Get last envelope/interpretation if available
        interp = getattr(orch, "_last_interpretation", None)
        env = getattr(orch, "_last_envelope", None)
        intents = getattr(interp, "intents", []) if interp else []
        resolved = getattr(interp, "resolved_education_query", None) if interp else None
        cands = getattr(interp, "intake_candidates", []) if interp else []
        is_fallback = isinstance(orch.interpreter, DeterministicConversationInterpreter) or (hasattr(orch.interpreter, "_init_error") and orch.interpreter._init_error)
        print(f"  Turn {i} txt={txt!r}")
        print(f"    -> reply={r.reply[:80]}... status={r.status} latency={elapsed:.0f}ms")
        print(f"    -> intents={intents} resolved={resolved!r} cands={[ (c.field_name, c.candidate_value[:10]) for c in cands ]} conf={getattr(interp,'confidence',None) if interp else None} fallback={bool(is_fallback)}")
        if sess:
            print(f"    -> pending={sess.pending_field} stage={sess.intake_stage} auth={sess.authorization_status} intake={ {k:v for k,v in sess.intake_snapshot.model_dump().items() if v} }")
    return latencies

def main():
    import tempfile
    all_lat = []
    fallbacks = 0
    total = 0
    # Case 1: 跨輪指代未見變體
    tmp1 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch1, is_formal1, model1 = _create_orchestrator(tmp1)
    lat = _run_case(orch1, None, "cross-fruit-unseen", ["糖尿病可以吃水果嗎？", "所以每天大概能碰幾份啊？"])
    all_lat.extend(lat); total+=len(lat)

    # Case 2: 同輪多意圖 二甲雙胍+芭樂
    tmp2 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch2, _, _ = _create_orchestrator(tmp2)
    orch2.handle_text(event_id="m1", line_user_id="U-smoke-multi", text="為自己整理")
    lat = _run_case(orch2, None, "multi-erjia-芭樂", ["醫生有開二甲雙胍給我，另外芭樂能吃嗎？"])
    all_lat.extend(lat); total+=len(lat)
    # Check not written for pure question
    tmp2b = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch2b, _, _ = _create_orchestrator(tmp2b)
    orch2b.handle_text(event_id="q1", line_user_id="U-smoke-q", text="為自己整理")
    lat = _run_case(orch2b, None, "pure-question", ["二甲雙胍會有什麼副作用？"])
    all_lat.extend(lat); total+=len(lat)
    # Verify not written
    sess_q = orch2b.session_for_user("U-smoke-q")
    if sess_q and sess_q.intake_snapshot.known_medications:
        print("  !! FAIL pure question wrote intake", sess_q.intake_snapshot.known_medications)
        fallbacks+=1
    else:
        print("  pure question correctly not written")

    # Case 3: 控制語句
    tmp3 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch3, _, _ = _create_orchestrator(tmp3)
    orch3.handle_text(event_id="c1", line_user_id="U-smoke-ctrl", text="為自己整理")
    orch3.handle_text(event_id="c2", line_user_id="U-smoke-ctrl", text="吃 metformin")
    lat = _run_case(orch3, None, "control", ["先不要填了"])
    all_lat.extend(lat); total+=len(lat)
    sess_c = orch3.session_for_user("U-smoke-ctrl")
    if sess_c and "先不要填了" in str(sess_c.intake_snapshot.model_dump()):
        print("  !! FAIL control polluted")
        fallbacks+=1
    # Continue
    lat = _run_case(orch3, None, "control-resume", ["謝謝"])
    all_lat.extend(lat); total+=len(lat)
    lat = _run_case(orch3, None, "control-resume2", ["繼續整理"])
    all_lat.extend(lat); total+=len(lat)
    sess_c2 = orch3.session_for_user("U-smoke-ctrl")
    print(f"  after control resume pending={sess_c2.pending_field if sess_c2 else None} should be original pending not polluted")

    # Case 4: 身份
    tmp4 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch4, _, _ = _create_orchestrator(tmp4)
    lat = _run_case(orch4, None, "identity", ["請問現在是人工客服還是 AI 在回？"])
    all_lat.extend(lat); total+=len(lat)

    # Case 5: 修正
    tmp5 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch5, _, _ = _create_orchestrator(tmp5)
    orch5.handle_text(event_id="sub1", line_user_id="U-smoke-sub", text="為自己整理")
    lat = _run_case(orch5, None, "subject-correction", ["我前面講錯，是我媽媽在吃，不是我"])
    all_lat.extend(lat); total+=len(lat)

    # Case 6: 多症狀
    tmp6 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch6, _, _ = _create_orchestrator(tmp6)
    orch6.handle_text(event_id="s1", line_user_id="U-smoke-sym", text="為自己整理")
    lat = _run_case(orch6, None, "multi-symptom", ["最近嘴巴很乾，晚上又一直跑廁所"])
    all_lat.extend(lat); total+=len(lat)

    # Case 7: 拒答
    tmp7 = Path(tempfile.mktemp(suffix=".sqlite3"))
    _, orch7, _, _ = _create_orchestrator(tmp7)
    orch7.handle_text(event_id="r1", line_user_id="U-smoke-refuse", text="為自己整理")
    lat = _run_case(orch7, None, "refuse", ["這個我真的完全沒概念"])
    all_lat.extend(lat); total+=len(lat)
    sess_r = orch7.session_for_user("U-smoke-refuse")
    if sess_r:
        # Should not store raw refuse as field value
        has_polluted = "完全沒概念" in str(sess_r.intake_snapshot.model_dump())
        print(f"  refuse polluted? {has_polluted} (should be False)")

    # Stats
    if all_lat:
        p50 = statistics.median(all_lat)
        p95 = sorted(all_lat)[int(len(all_lat)*0.95)] if len(all_lat)>1 else all_lat[0]
        print(f"\n=== Smoke Stats ===")
        print(f"is_formal={is_formal1} model={model1}")
        print(f"total_turns={total} p50={p50:.0f}ms p95={p95:.0f}ms fallback_rate={fallbacks}/{total}")
        print(f"CONVERSATION_LLM_TIMEOUT_S={os.getenv('CONVERSATION_LLM_TIMEOUT_S', '8')}")

if __name__ == "__main__":
    import argparse
    _p = argparse.ArgumentParser(description="P1.1 Live Smoke — 正式執行: python -m scripts.p1_live_smoke （或 python scripts/p1_live_smoke.py）")
    _p.add_argument("--help-long", action="store_true", help="顯示完整說明")
    _a, _ = _p.parse_known_args()
    if _a.help_long:
        _p.print_help()
        print("\n正式指令:\n  python -m scripts.p1_live_smoke\n  python scripts/p1_live_smoke.py")
        raise SystemExit(0)
    main()
