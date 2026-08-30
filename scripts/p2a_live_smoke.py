#!/usr/bin/env python3
"""P2A Live Smoke — 驗證「deterministic 部分命中＋formal 補齊」實際落地，且紅旗仍優先。

正式執行（需 .env 真實模型）:
  python -m scripts.p2a_live_smoke
  python scripts/p2a_live_smoke.py
  PYTHONPATH=. python scripts/p2a_live_smoke.py

Dry-run（無需真模型，CI可用）:
  python scripts/p2a_live_smoke.py --dry-run
  python scripts/p2a_live_smoke.py --dry-run -q

報告: 每組 latency、fallback 分類（red_flag_safety / evidence_insufficient /
timeout_dependency）、intake_snapshot 落地內容（脫敏）、反例不污染、紅旗必 FALLBACK、p50/p95。
不納入 pytest（scripts/ 目錄、檔名非 test_*），不輸出 API key / LINE ID / hash。

P2A.1 Phase 2 擴充:
  - per-stage 值 (red_flag_and_auth_ms, conversation_interpreter_ms, candidate_validation_ms, rag_retrieval_ms, answer_generator_ms, b_gate_ms, d_gate_ms, persistence_ms, total_ms) 從 staged_latency
  - session-first vs session-warm labels from staged_latency; process-first is
    reported separately.  These are measurement-order labels, not model cold-start claims.
  - scenarios: pure intake, pure education, mixed intent (+ warm follow-up), red flag 各一，共 5 turns (mixed 跑 2 turns 同一 session)
  - p50/p95 per stage and total, split fallback categories, and process-first/warm statistics

參考: scripts/p1_live_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
import statistics
import time
import tempfile

# Load .env early (override False to respect real env, but allow explicit override)
# Worktree 的 PROJECT_ROOT/.env 可能不存在（gitignored），自動 fallback 到主專案 .env
_MAIN_PROJECT_ENV = Path("/Users/dolly/Documents/code/tfda-diabetes-agent/.env")

def _load_env_files(env_file: Path | str | None = None) -> None:
    """安全載入 .env 到當前 process（不寫檔、不複製），僅用 load_dotenv。

    - 若指定 env_file：只載入該檔 (override=False)
    - 未指定時：依序嘗試 worktree PROJECT_ROOT/.env 與主專案 _MAIN_PROJECT_ENV
      （worktree 優先，主專案作為 fallback；override=False 保證不覆蓋已存在的真實 env）
    """
    try:
        from dotenv import load_dotenv
        from tfda_context_gate.run_config import PROJECT_ROOT as _PR  # type: ignore
        proj_root = _PR
    except ImportError:
        try:
            from dotenv import load_dotenv  # type: ignore
        except ImportError:
            return
        proj_root = Path(__file__).resolve().parents[1]
        for _p in ([Path(env_file)] if env_file else [proj_root / ".env", _MAIN_PROJECT_ENV]):
            try:
                if _p.exists():
                    load_dotenv(dotenv_path=_p, override=False)
            except Exception:
                continue
        return
    try:
        from dotenv import load_dotenv as _ld  # type: ignore
    except ImportError:
        return
    candidates: list[Path]
    if env_file is not None and str(env_file).strip():
        candidates = [Path(str(env_file)).expanduser()]
    else:
        candidates = [proj_root / ".env", _MAIN_PROJECT_ENV]
    for _p in candidates:
        try:
            if _p.exists():
                _ld(dotenv_path=_p, override=False)
        except Exception:
            continue

try:
    from tfda_context_gate.run_config import PROJECT_ROOT
except ImportError:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 匯入時即嘗試自動載入（worktree .env → 主專案 .env），確保後續 env_value 能讀到 ROUTER_LLM_MODEL
try:
    _load_env_files(None)
except Exception:
    pass

from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.conversation.interpreter import (
    ConversationInterpreterFactory,
    FakeConversationInterpreter,
    ConversationTurnInterpretation,
    IntakeCandidate,
)
from tfda_context_gate.run_config import env_value

# Warm follow-up text for mixed-intent second turn (same session, validates multi-turn state)
WARM_FOLLOWUP_TEXT = "晚上常跑廁所會是糖尿病嗎？"
WARM_FOLLOWUP_TEXT_FALLBACK = "糖尿病適合吃什麼水果比較好？"


def _redacted_secret(val: str) -> str:
    if not val:
        return "***REDACTED***"
    v = str(val).strip()
    if len(v) <= 4:
        return "***REDACTED***"
    return v[:4] + "***REDACTED***"


def _masked_model() -> str:
    raw = env_value("CONVERSATION_LLM_MODEL", "") or env_value("ROUTER_LLM_MODEL", "") or ""
    if not raw:
        return "(deterministic)"
    short = raw.split("/")[-1] if "/" in raw else raw
    return short


def _desensitized_snapshot(snapshot) -> dict:
    if snapshot is None:
        return {}
    try:
        d = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
    except Exception:
        return {"_raw": str(snapshot)[:200]}
    out = {}
    for k in ("known_medications", "allergies", "chronic_conditions", "family_history", "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor"):
        v = d.get(k)
        if v:
            if isinstance(v, list):
                out[k] = [str(x)[:40] for x in v[:3]]
            else:
                out[k] = str(v)[:80]
    return out


def _fallback_category(result) -> str | None:
    """Classify a fallback without counting deterministic red-flag safety as failure."""

    if getattr(result, "status", "") != "FALLBACK":
        return None
    reason = str(getattr(result, "fallback_reason", "") or "").upper()
    reply = str(getattr(result, "reply", "") or "")
    if reason in {"A_EMERGENCY", "RED_FLAG", "RED_FLAG_FALLBACK"} or any(
        token in reply for token in ("呼吸困難", "胸痛", "119", "急診", "緊急")
    ):
        return "red_flag_safety"
    if reason in {"B_INSUFFICIENT", "B_UNSAFE", "D_FALLBACK", "AGENT_BOUNDED_FALLBACK"}:
        return "evidence_insufficient"
    if reason in {
        "FORMAL_TIMEOUT",
        "SYSTEM_DEPENDENCY",
        "C_FAILURE",
        "A_DEPENDENCY",
        "WORKFLOW_TIMEOUT",
    } or "timeout" in reason.lower() or "schema" in reason.lower():
        return "timeout_dependency"
    return "other_fallback"


def _extract_staged(orch, result) -> dict:
    staged = {}
    try:
        if hasattr(orch, "_last_staged_latency") and isinstance(orch._last_staged_latency, dict):
            staged = dict(orch._last_staged_latency)
        # also try result trace if available (for direct workflow tests)
        if not staged and hasattr(result, "trace") and isinstance(result.trace, dict):
            staged = result.trace.get("staged_latency", {}) or {}
        # fallback: try workflow trace attribute
        if not staged and hasattr(result, "trace") and isinstance(getattr(result, "trace", None), dict):
            pass
    except Exception:
        staged = {}
    # ensure required keys present with 0 default for reporting
    for k in ("red_flag_and_auth_ms", "conversation_interpreter_ms", "candidate_validation_ms", "rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms", "persistence_ms", "total_ms"):
        if k not in staged:
            staged[k] = 0.0
    return staged


def _create_formal_orchestrator(tmp: Path):
    repo = SQLiteProductSessionRepository(tmp)
    saved_pytest = os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        interp = ConversationInterpreterFactory.from_env()
    finally:
        if saved_pytest is not None:
            os.environ["PYTEST_CURRENT_TEST"] = saved_pytest
    is_formal = interp.__class__.__name__ == "FormalConversationInterpreter"
    model_name = _masked_model()
    timeout = os.getenv("CONVERSATION_LLM_TIMEOUT_S", "8")
    print(f"[Factory] interpreter={interp.__class__.__name__} is_formal={is_formal} model={model_name} timeout={timeout}s")
    init_err = getattr(interp, "_init_error", None)
    if init_err:
        print(f"[ERROR] 依賴失敗：無法建立 FormalConversationInterpreter / provider unreachable: {_redacted_secret(str(init_err)[:200])}")
    if not is_formal:
        if not init_err:
            print("[WARN] Expected FormalConversationInterpreter but got", interp.__class__.__name__)
            print("       Check .env CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL / OPENCODE_API_KEY presence")
        print("[ERROR] 依賴失敗：無法建立 FormalConversationInterpreter / provider unreachable")
    orch = ConversationOrchestrator(repo, identity_hash_key="p2a-live-smoke-key-12345678901234", interpreter=interp)
    return repo, orch, is_formal, model_name, interp


def _create_fake_orchestrator(tmp: Path):
    repo = SQLiteProductSessionRepository(tmp)
    fake = FakeConversationInterpreter(
        preset={
            "我嘴巴很乾，晚上一直跑廁所": ConversationTurnInterpretation(
                intents=["INTAKE_ANSWER"],
                resolved_education_query=None,
                intake_candidates=[
                    IntakeCandidate(field_name="symptom_description", candidate_value="口乾；頻尿", source_quote="嘴巴很乾，晚上一直跑廁所", confidence=0.88, explicitly_stated=True, requires_confirmation=False),
                ],
                references_resolved=False,
                needs_clarification=False,
                confidence=0.88,
            ),
            "我最近常口渴，糖尿病一天可以吃幾份水果？": ConversationTurnInterpretation(
                intents=["INTAKE_ANSWER", "EDUCATION_QUESTION"],
                resolved_education_query="糖尿病一天可以吃幾份水果？",
                intake_candidates=[
                    IntakeCandidate(field_name="symptom_description", candidate_value="口渴", source_quote="常口渴", confidence=0.85, explicitly_stated=True, requires_confirmation=False),
                ],
                references_resolved=False,
                needs_clarification=False,
                confidence=0.82,
            ),
            "晚上常跑廁所會是糖尿病嗎？": ConversationTurnInterpretation(
                intents=["EDUCATION_QUESTION"],
                resolved_education_query="晚上常跑廁所會是糖尿病嗎？",
                intake_candidates=[],
                references_resolved=False,
                needs_clarification=False,
                confidence=0.7,
            ),
            WARM_FOLLOWUP_TEXT: ConversationTurnInterpretation(
                intents=["EDUCATION_QUESTION"],
                resolved_education_query=WARM_FOLLOWUP_TEXT,
                intake_candidates=[],
                references_resolved=False,
                needs_clarification=False,
                confidence=0.7,
            ),
            "我胸口很痛喘不過氣": ConversationTurnInterpretation(
                intents=["INTAKE_ANSWER"],
                resolved_education_query=None,
                intake_candidates=[],
                references_resolved=False,
                needs_clarification=False,
                confidence=0.9,
            ),
        },
        default=ConversationTurnInterpretation(intents=["UNKNOWN"], confidence=0.5),
    )
    model_name = "(fake/dry-run)"
    print(f"[Factory] interpreter=FakeConversationInterpreter is_formal=False model={model_name} (dry-run)")
    orch = ConversationOrchestrator(repo, identity_hash_key="p2a-live-smoke-key-12345678901234", interpreter=fake, use_formal=False)
    return repo, orch, False, model_name, fake


CASES = [
    {
        "name": "pure-intake",
        "text": "我嘴巴很乾，晚上一直跑廁所",
        "expect": "純 intake：symptom_description 應保留多子句",
        "check": "landed_symptom",
        "must_not": [],
    },
    {
        "name": "pure-education",
        "text": "晚上常跑廁所會是糖尿病嗎？",
        "expect": "純 education：問句不得污染 intake",
        "check": "not_polluted",
        "must_not": ["symptom_description"],
    },
    {
        "name": "mixed-intent",
        "text": "我最近常口渴，糖尿病一天可以吃幾份水果？",
        "expect": "mixed：同時含 INTAKE_ANSWER + EDUCATION_QUESTION；成功路徑最多 2 LLM calls (interpreter + C generator)",
        "check": "multi_intent",
        "must_not": [],
    },
    {
        "name": "red-flag",
        "text": "我胸口很痛喘不過氣",
        "expect": "紅旗必須為 FALLBACK，優先於任何 merge",
        "check": "is_fallback",
        "must_not": [],
    },
]


def _p50_p95(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    p50 = float(statistics.median(vals))
    sorted_vals = sorted(vals)
    idx = int(len(sorted_vals) * 0.95)
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    p95 = float(sorted_vals[idx])
    return p50, p95


def _run_single_case(orch, repo, case: dict, dry_run: bool = False):
    name = case["name"]
    text = case["text"]
    user_id = f"U-p2a-{name}"
    event_base = f"p2a-{name}"
    latencies = []
    is_fallback = False
    fallback_reason = ""
    fallback_category = None
    is_timeout_fallback = False
    snapshot_desens = {}
    status = ""
    reply_preview = ""
    polluted = False
    passed = True
    notes = []
    staged_collected = {}
    model_calls = 0

    try:
        orch.handle_text(event_id=f"{event_base}-auth", line_user_id=user_id, text="為自己整理")
    except Exception as e:
        notes.append(f"auth step error: {e}")

    start = time.time()
    try:
        result = orch.handle_text(event_id=f"{event_base}-main", line_user_id=user_id, text=text)
        elapsed = (time.time() - start) * 1000
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        notes.append(f"handle_text exception: {e}")
        result = None
        is_fallback = True
        fallback_reason = f"exception:{e.__class__.__name__}"
        status = "EXCEPTION"

    if result is not None:
        status = getattr(result, "status", "")
        reply_preview = (getattr(result, "reply", "") or "")[:120].replace("\n", " ")
        latencies.append(elapsed)
        staged_collected = _extract_staged(orch, result)
        # Red-flag safety returns before interpretation, so a formal
        # interpreter instance alone does not prove an LLM call occurred.
        if (
            orch.interpreter.__class__.__name__ == "FormalConversationInterpreter"
            and float(staged_collected.get("conversation_interpreter_ms", 0.0) or 0.0) > 0
        ):
            model_calls += 1
        if (
            orch.interpreter.__class__.__name__ == "FormalConversationInterpreter"
            and float(staged_collected.get("answer_generator_ms", 0.0) or 0.0) > 0
        ):
            model_calls += 1
        interp = getattr(orch, "interpreter", None)
        has_timeout_fallback = False
        if interp is not None:
            if hasattr(interp, "_init_error") and getattr(interp, "_init_error"):
                has_timeout_fallback = True
        last_interp = getattr(orch, "_last_interpretation", None)
        is_timeout_fallback = False
        if status == "FALLBACK":
            is_fallback = True
            fallback_category = _fallback_category(result)
            fallback_reason = getattr(result, "fallback_reason", None) or fallback_category or "FALLBACK"
            is_timeout_fallback = fallback_category == "timeout_dependency"
        elif has_timeout_fallback:
            is_fallback = True
            fallback_category = "timeout_dependency"
            fallback_reason = "timeout_dependency"
            is_timeout_fallback = True
        if has_timeout_fallback and not is_fallback:
            is_fallback = True
            fallback_category = "timeout_dependency"
            fallback_reason = "timeout_dependency"
            is_timeout_fallback = True

    sess = None
    try:
        sess = orch.session_for_user(user_id)
    except Exception as e:
        notes.append(f"session_for_user error: {e}")

    if sess is not None:
        snapshot_desens = _desensitized_snapshot(sess.intake_snapshot)
        if case["check"] == "landed_symptom":
            sd = sess.intake_snapshot.symptom_description or ""
            if not sd:
                passed = False
                notes.append("FAIL: symptom_description 未落地")
            elif "口乾" not in sd and "嘴巴" not in sd and "口渴" not in sd:
                if "跑廁所" not in sd and "頻尿" not in sd and "夜" not in sd:
                    passed = False
                    notes.append(f"FAIL: symptom_description 內容不符預期: {sd!r}")
                else:
                    notes.append(f"OK: symptom_description 落地 {sd!r}（部分命中）")
            else:
                notes.append(f"OK: symptom_description 落地 {sd!r}")
        elif case["check"] == "not_polluted":
            sd = sess.intake_snapshot.symptom_description or ""
            if sd and ("跑廁所" in sd or "頻尿" in sd or "糖尿病嗎" in text):
                if sd.strip():
                    polluted = True
                    passed = False
                    notes.append(f"FAIL: counterexample 污染病史 sd={sd!r}（應為空）")
                else:
                    notes.append("OK: 未污染")
            else:
                if sd and "？" in sd or "?" in sd:
                    polluted = True
                    passed = False
                    notes.append(f"FAIL: 問句寫入 sd={sd!r}")
                else:
                    notes.append(f"OK: counterexample 未污染（sd={(sd or '空')!r}）")
        elif case["check"] == "is_fallback":
            if status != "FALLBACK":
                passed = False
                notes.append(f"FAIL: 紅旗應為 FALLBACK，實為 {status}")
            else:
                notes.append(f"OK: 紅旗正確為 FALLBACK ({fallback_reason})")
            sd = sess.intake_snapshot.symptom_description if sess else None
            if sd:
                notes.append(f"note: red-flag sd={sd!r}（理想為空，不應寫入）")
        elif case["check"] == "multi_intent":
            interp = getattr(orch, "_last_interpretation", None)
            intents = getattr(interp, "intents", []) if interp else []
            resolved = getattr(interp, "resolved_education_query", None) if interp else None
            notes.append(f"intents={intents} resolved={resolved!r}")
            sd = sess.intake_snapshot.symptom_description or "" if sess else ""
            if sd:
                if "口渴" not in sd and "口乾" not in sd and "很渴" not in sd:
                    passed = False
                    notes.append(f"FAIL: multi-intent 症狀內容不符預期 sd={sd!r}")
                elif "水果" in sd or "幾份" in sd:
                    passed = False
                    polluted = True
                    notes.append(f"FAIL: 衛教問句污染 symptom_description sd={sd!r}")
                else:
                    notes.append(f"OK: multi-intent 口渴落地 sd={sd!r}")
            else:
                passed = False
                notes.append("FAIL: multi-intent symptom_description 未落地")
    else:
        passed = False
        notes.append("FAIL: session not found")

    print(f"\n=== {name} ===")
    print(f"  input: {text!r}")
    print(f"  -> status={status} latency={latencies[0] if latencies else 0:.0f}ms fallback={is_fallback} reason={fallback_reason}")
    print(f"  -> staged: {json.dumps(staged_collected, ensure_ascii=False)}")
    print(f"  -> reply={reply_preview}...")
    print(f"  -> intake_snapshot (desensitized)={json.dumps(snapshot_desens, ensure_ascii=False)}")
    interp = getattr(orch, "_last_interpretation", None)
    if interp is not None:
        try:
            intents = getattr(interp, "intents", [])
            resolved = getattr(interp, "resolved_education_query", None)
            cands = getattr(interp, "intake_candidates", [])
            cand_str = [(c.field_name, c.candidate_value[:20], c.source_quote[:20]) for c in cands] if cands else []
            print(f"  -> interp intents={intents} resolved={resolved!r} cands={cand_str} confidence={getattr(interp, 'confidence', None)}")
        except Exception:
            pass
    for n in notes:
        print(f"     {n}")
    print(f"  -> check={case['check']} passed={passed} polluted={polluted}")

    return {
        "name": name,
        "text": text,
        "latency_ms": latencies[0] if latencies else 0,
        "staged": staged_collected,
        "is_fallback": is_fallback,
        "is_timeout_fallback": is_timeout_fallback,
        "fallback_category": fallback_category,
        "model_calls": model_calls,
        "fallback_reason": fallback_reason,
        "status": status,
        "reply_preview": reply_preview,
        "snapshot": snapshot_desens,
        "passed": passed,
        "polluted": polluted,
        "notes": notes,
        "user_id": user_id,
    }


def _run_warm_followup(orch, user_id: str, text: str, dry_run: bool = False) -> dict:
    """Run a second turn in the SAME session (warm) for mixed-intent multi-turn validation."""
    name = "mixed-intent-warm"
    latencies = []
    is_fallback = False
    fallback_reason = ""
    fallback_category = None
    is_timeout_fallback = False
    snapshot_desens = {}
    status = ""
    reply_preview = ""
    polluted = False
    passed = True
    notes = []
    staged_collected = {}
    model_calls = 0

    # Record intake before warm turn to validate persistence
    sess_before = None
    try:
        sess_before = orch.session_for_user(user_id)
    except Exception:
        sess_before = None
    sd_before = ""
    if sess_before is not None:
        try:
            sd_before = sess_before.intake_snapshot.symptom_description or ""
        except Exception:
            sd_before = ""

    start = time.time()
    try:
        result = orch.handle_text(event_id=f"p2a-mixed-intent-warm", line_user_id=user_id, text=text)
        elapsed = (time.time() - start) * 1000
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        notes.append(f"warm handle_text exception: {e}")
        result = None
        is_fallback = True
        fallback_reason = f"exception:{e.__class__.__name__}"
        status = "EXCEPTION"

    if result is not None:
        status = getattr(result, "status", "")
        reply_preview = (getattr(result, "reply", "") or "")[:120].replace("\n", " ")
        latencies.append(elapsed)
        staged_collected = _extract_staged(orch, result)
        if (
            orch.interpreter.__class__.__name__ == "FormalConversationInterpreter"
            and float(staged_collected.get("conversation_interpreter_ms", 0.0) or 0.0) > 0
        ):
            model_calls += 1
        if (
            orch.interpreter.__class__.__name__ == "FormalConversationInterpreter"
            and float(staged_collected.get("answer_generator_ms", 0.0) or 0.0) > 0
        ):
            model_calls += 1
        interp = getattr(orch, "interpreter", None)
        has_timeout_fallback = False
        if interp is not None:
            if hasattr(interp, "_init_error") and getattr(interp, "_init_error"):
                has_timeout_fallback = True
        is_timeout_fallback = False
        if status == "FALLBACK":
            is_fallback = True
            fallback_category = _fallback_category(result)
            fallback_reason = getattr(result, "fallback_reason", None) or fallback_category or "FALLBACK"
            is_timeout_fallback = fallback_category == "timeout_dependency"
        elif has_timeout_fallback:
            is_fallback = True
            fallback_category = "timeout_dependency"
            fallback_reason = "timeout_dependency"
            is_timeout_fallback = True
        if has_timeout_fallback and not is_fallback:
            is_fallback = True
            fallback_category = "timeout_dependency"
            fallback_reason = "timeout_dependency"
            is_timeout_fallback = True

    sess = None
    try:
        sess = orch.session_for_user(user_id)
    except Exception as e:
        notes.append(f"session_for_user error: {e}")

    if sess is not None:
        snapshot_desens = _desensitized_snapshot(sess.intake_snapshot)
        # Warm follow-up is pure education; must not pollute symptom, and prior symptom must persist (multi-turn state)
        sd_after = sess.intake_snapshot.symptom_description or ""
        if sd_before and sd_before not in sd_after and "口渴" not in sd_after:
            # Allow if symptom was overwritten? Should remain
            notes.append(f"WARN: multi-turn symptom persistence check: before={sd_before!r} after={sd_after!r}")
        else:
            if sd_before:
                notes.append(f"OK: multi-turn symptom persisted after warm turn sd={sd_after!r}")
            else:
                notes.append(f"OK: warm turn sd={sd_after!r}")
        if "？" in sd_after or "嗎" in sd_after:
            if "是不是" in sd_after or "糖尿病嗎" in sd_after or "適合吃" in sd_after:
                polluted = True
                passed = False
                notes.append(f"FAIL: warm turn polluted symptom sd={sd_after!r}")
            else:
                notes.append(f"OK: warm turn not polluted")
        else:
            notes.append(f"OK: warm follow-up not polluted (sd={(sd_after or '空')!r})")
        # Also ensure not fallback red-flag etc.
        if status == "FALLBACK" and "RED_FLAG" in fallback_reason:
            passed = False
            notes.append(f"FAIL: warm turn unexpected red flag FALLBACK")
    else:
        passed = False
        notes.append("FAIL: session not found after warm turn")

    print(f"\n=== {name} (warm follow-up, same session) ===")
    print(f"  input: {text!r} (user_id={user_id} reused)")
    print(f"  -> status={status} latency={latencies[0] if latencies else 0:.0f}ms fallback={is_fallback} reason={fallback_reason}")
    print(f"  -> staged: {json.dumps(staged_collected, ensure_ascii=False)}")
    print(f"  -> reply={reply_preview}...")
    print(f"  -> intake_snapshot (desensitized)={json.dumps(snapshot_desens, ensure_ascii=False)}")
    interp = getattr(orch, "_last_interpretation", None)
    if interp is not None:
        try:
            intents = getattr(interp, "intents", [])
            resolved = getattr(interp, "resolved_education_query", None)
            cands = getattr(interp, "intake_candidates", [])
            cand_str = [(c.field_name, c.candidate_value[:20], c.source_quote[:20]) for c in cands] if cands else []
            print(f"  -> interp intents={intents} resolved={resolved!r} cands={cand_str} confidence={getattr(interp, 'confidence', None)}")
        except Exception:
            pass
    for n in notes:
        print(f"     {n}")
    print(f"  -> check=warm_multi_turn passed={passed} polluted={polluted}")

    return {
        "name": name,
        "text": text,
        "latency_ms": latencies[0] if latencies else 0,
        "staged": staged_collected,
        "is_fallback": is_fallback,
        "is_timeout_fallback": is_timeout_fallback,
        "fallback_category": fallback_category,
        "model_calls": model_calls,
        "fallback_reason": fallback_reason,
        "status": status,
        "reply_preview": reply_preview,
        "snapshot": snapshot_desens,
        "passed": passed,
        "polluted": polluted,
        "notes": notes,
        "user_id": user_id,
    }


def main():
    parser = argparse.ArgumentParser(description="P2A Live Smoke — 正式: python scripts/p2a_live_smoke.py ; Dry-run: python scripts/p2a_live_smoke.py --dry-run")
    parser.add_argument("--dry-run", action="store_true", help="不需真模型，以 Fake 路徑演練（CI 用）")
    parser.add_argument("-q", "--quiet", action="store_true", help="簡潔輸出")
    parser.add_argument("--json", dest="json_out", action="store_true", help="額外輸出 JSON 到 stdout")
    parser.add_argument("--env-file", dest="env_file", type=str, default=None, help="指定 .env 路徑（預設自動嘗試 worktree PROJECT_ROOT/.env 與主專案 /Users/dolly/Documents/code/tfda-diabetes-agent/.env）")
    args, _ = parser.parse_known_args()
    try:
        _load_env_files(args.env_file)
    except Exception:
        pass

    dry_run = args.dry_run
    quiet = args.quiet

    if not dry_run:
        conv = (env_value("CONVERSATION_LLM_MODEL", "") or "").strip()
        router = (env_value("ROUTER_LLM_MODEL", "") or "").strip()
        api_key = (env_value("OPENCODE_API_KEY", "") or os.getenv("OPENCODE_API_KEY") or "").strip()
        if not (conv or router):
            print("[ERROR] 未偵測到 CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL，請確認 .env")
            print("       建議先用 --dry-run 驗證腳本，或設定真實模型後重試。")

    mode = "DRY-RUN (Fake)" if dry_run else "LIVE (Real .env)"
    print(f"[P2A Smoke] mode={mode}")
    print(f"[Honest] mixed-intent 成功且 B PASS 時最多 2 次 model calls: conversation interpreter (1) + formal C generator (0–1); B insufficient 時 C 會跳過，無額外 rephraser")
    print(f"[Multi-turn] mixed-intent 將在同一 session 追加一輪教育追問，驗證 multi-turn state 與 session-first 標記")

    if not quiet:
        print("  cases:多症狀/多意圖/反例/紅旗 各一，驗證 deterministic 部分命中＋formal 補齊、紅旗優先 (mixed 含同 session warm 追問，共 5 turns)")

    results = []
    staged_keys = ["red_flag_and_auth_ms", "conversation_interpreter_ms", "candidate_validation_ms", "rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms", "persistence_ms", "total_ms"]

    for case in CASES:
        tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
        try:
            if dry_run:
                repo, orch, is_formal, model_name, interp = _create_fake_orchestrator(tmp)
            else:
                repo, orch, is_formal, model_name, interp = _create_formal_orchestrator(tmp)
                if not is_formal:
                    print(f"[FAIL] LIVE mode interpreter 應為 FormalConversationInterpreter，實為 {interp.__class__.__name__} — 中止")
                elif not quiet:
                    print(f"  [Verified] interpreter is FormalConversationInterpreter (model={model_name})")
        except Exception as e:
            print(f"[ERROR] 建立 orchestrator 失敗: {e}")
            import traceback
            traceback.print_exc()
            continue

        r = _run_single_case(orch, None, case, dry_run=dry_run)
        r["model"] = model_name if 'model_name' in locals() else ""
        r["is_formal"] = is_formal if 'is_formal' in locals() else False
        results.append(r)

        # For mixed-intent, run a second warm turn in the SAME session to get warm data and validate multi-turn state
        if case["name"] == "mixed-intent":
            try:
                warm_text = WARM_FOLLOWUP_TEXT
                # Use same orch and same user_id as the mixed case's session
                warm_user_id = r.get("user_id") or f"U-p2a-{case['name']}"
                r2 = _run_warm_followup(orch, warm_user_id, warm_text, dry_run=dry_run)
                r2["model"] = model_name if 'model_name' in locals() else ""
                r2["is_formal"] = is_formal if 'is_formal' in locals() else False
                results.append(r2)
            except Exception as e:
                print(f"[WARN] warm follow-up failed: {e}")
                import traceback
                traceback.print_exc()

        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    total = len(results)
    fallback_count = sum(1 for r in results if r["is_fallback"])
    fallback_counts = {
        category: sum(1 for r in results if r.get("fallback_category") == category)
        for category in ("red_flag_safety", "evidence_insufficient", "timeout_dependency", "other_fallback")
    }
    # Red-flag fallback is the intended safety outcome, not a system failure.
    system_fallback_count = fallback_counts["evidence_insufficient"] + fallback_counts["timeout_dependency"] + fallback_counts["other_fallback"]
    total_model_calls = sum(int(r.get("model_calls", 0) or 0) for r in results)

    # Overall p50/p95 (all turns)
    all_lat = [r["latency_ms"] for r in results if r["latency_ms"]]
    if all_lat:
        p50, p95 = _p50_p95(all_lat)
    else:
        p50 = p95 = 0

    # Use session labels for comparable turn statistics.  A session-first turn
    # is not a model cold-start claim; process-first is reported separately
    # because auth/control turns may consume it first.
    cold_lats: list[float] = []
    warm_lats: list[float] = []
    per_stage_cold: dict[str, list[float]] = {k: [] for k in staged_keys}
    per_stage_warm: dict[str, list[float]] = {k: [] for k in staged_keys}
    per_stage_vals: dict[str, list[float]] = {k: [] for k in staged_keys}

    for r in results:
        staged = r.get("staged", {}) or {}
        is_cold_label = bool(staged.get("is_session_first_turn", False))
        lat = r.get("latency_ms", 0) or 0
        for k in staged_keys:
            v = float(staged.get(k, 0.0) or 0.0)
            per_stage_vals.setdefault(k, []).append(v)
            if is_cold_label:
                per_stage_cold[k].append(v)
            else:
                per_stage_warm[k].append(v)
        if lat:
            if is_cold_label:
                cold_lats.append(float(lat))
            else:
                warm_lats.append(float(lat))

    session_first_count = sum(1 for r in results if (r.get("staged", {}) or {}).get("is_session_first_turn") is True)
    session_warm_count = sum(1 for r in results if (r.get("staged", {}) or {}).get("is_warm_session_turn") is True)
    process_first_count = sum(1 for r in results if (r.get("staged", {}) or {}).get("is_process_first_measurement") is True)

    cold_p50, cold_p95 = _p50_p95(cold_lats)
    warm_p50, warm_p95 = _p50_p95(warm_lats)

    # per-stage p50/p95 (overall)
    per_stage_stats = {}
    for k in staged_keys:
        vals = per_stage_vals.get(k, [])
        if vals:
            sorted_vals = sorted(vals)
            p50_s = statistics.median(vals)
            idx = int(len(sorted_vals) * 0.95)
            if idx >= len(sorted_vals):
                idx = len(sorted_vals) - 1
            p95_s = sorted_vals[idx]
            per_stage_stats[k] = {"p50": round(float(p50_s), 1), "p95": round(float(p95_s), 1), "vals": [round(float(v),1) for v in vals]}
        else:
            per_stage_stats[k] = {"p50": 0, "p95": 0, "vals": []}

    # per-stage cold vs warm stats
    per_stage_cold_stats = {}
    per_stage_warm_stats = {}
    for k in staged_keys:
        c_vals = per_stage_cold.get(k, [])
        w_vals = per_stage_warm.get(k, [])
        if c_vals:
            cp50, cp95 = _p50_p95(c_vals)
            per_stage_cold_stats[k] = {"p50": round(float(cp50),1), "p95": round(float(cp95),1), "vals": [round(float(v),1) for v in c_vals]}
        else:
            per_stage_cold_stats[k] = {"p50": 0, "p95": 0, "vals": []}
        if w_vals:
            wp50, wp95 = _p50_p95(w_vals)
            per_stage_warm_stats[k] = {"p50": round(float(wp50),1), "p95": round(float(wp95),1), "vals": [round(float(v),1) for v in w_vals]}
        else:
            per_stage_warm_stats[k] = {"p50": 0, "p95": 0, "vals": []}

    is_formal_summary = results[0]["is_formal"] if results else False
    model_summary = results[0]["model"] if results else ""

    print("\n=== Smoke Stats ===")
    print(f"mode={mode} is_formal={is_formal_summary} model={model_summary}")
    print(f"total={total} latencies={[f'{x:.0f}ms' for x in all_lat]} p50={p50:.0f}ms p95={p95:.0f}ms total_fallbacks={fallback_count}/{total}")
    print(
        "fallback_categories="
        f"red_flag_safety={fallback_counts['red_flag_safety']}/{total} "
        f"evidence_insufficient={fallback_counts['evidence_insufficient']}/{total} "
        f"timeout_dependency={fallback_counts['timeout_dependency']}/{total} "
        f"other={fallback_counts['other_fallback']}/{total} "
        f"system_failure_rate={system_fallback_count}/{total}"
    )
    print(f"session_first_turn={session_first_count} session_warm_turn={session_warm_count} (not model cold-start)")
    print(f"process_first_measurement_in_report={process_first_count} (auth/control turns may consume process-first label)")
    print(f"observed_model_calls={total_model_calls} (formal interpreter + C only when C stage ran)")
    print(f"session-first p50={cold_p50:.0f}ms p95={cold_p95:.0f}ms session-warm p50={warm_p50:.0f}ms p95={warm_p95:.0f}ms")
    print(f"CONVERSATION_LLM_TIMEOUT_S={os.getenv('CONVERSATION_LLM_TIMEOUT_S', '8')} FORMAL_WORKFLOW_TIMEOUT_S={os.getenv('FORMAL_WORKFLOW_TIMEOUT_S','45')}")
    print("per-stage p50/p95 (ms) [overall]:")
    for k in staged_keys:
        stats = per_stage_stats[k]
        print(f"  {k}: p50={stats['p50']:.0f} p95={stats['p95']:.0f} vals={stats['vals']}")
    print("per-stage session-first p50/p95 (ms):")
    for k in staged_keys:
        stats = per_stage_cold_stats[k]
        print(f"  {k}: p50={stats['p50']:.0f} p95={stats['p95']:.0f} vals={stats['vals']}")
    print("per-stage session-warm p50/p95 (ms):")
    for k in staged_keys:
        stats = per_stage_warm_stats[k]
        print(f"  {k}: p50={stats['p50']:.0f} p95={stats['p95']:.0f} vals={stats['vals']}")
    for r in results:
        status_icon = "PASS" if r["passed"] else "FAIL"
        staged_label = r.get("staged", {}) or {}
        label = "session-first" if staged_label.get("is_session_first_turn") else "session-warm"
        print(f"  [{status_icon}] {r['name']} ({label}): status={r['status']} latency={r['latency_ms']:.0f}ms fallback={r['is_fallback']} staged={json.dumps(r['staged'], ensure_ascii=False)} snapshot={json.dumps(r['snapshot'], ensure_ascii=False)}")

    all_passed = all(r["passed"] for r in results)
    red = next((r for r in results if r["name"] == "red-flag"), None)
    if red and not red["is_fallback"]:
        all_passed = False
    ce = next((r for r in results if r["name"] == "pure-education"), None)
    if ce and ce["polluted"]:
        all_passed = False
    # also check legacy counterexample name if any
    ce2 = next((r for r in results if r["name"] == "counterexample"), None)
    if ce2 and ce2["polluted"]:
        all_passed = False
    # also check warm follow-up polluted
    warm_dup = next((r for r in results if r["name"] == "mixed-intent-warm"), None)
    if warm_dup and warm_dup.get("polluted"):
        all_passed = False

    if not quiet:
        print("\n--- 如何執行真實 live smoke ---")
        print("  1. 確認 .env 已含 CONVERSATION_LLM_MODEL=opencode/mimo-v2.5 與 OPENCODE_API_KEY")
        print("  2. 執行: python scripts/p2a_live_smoke.py")
        print("     或:   python -m scripts.p2a_live_smoke")
        print("  3. 觀察: [Factory] interpreter=FormalConversationInterpreter is_formal=True")
        print("     每組 latency/是否 fallback/timeout 與 intake_snapshot 脫敏 JSON 與 staged per-stage")
        print("  4. 預期參考值（mimo-v2.5 + bge-m3，網路/模型浮動）:")
        print("     p50 ~2-5s p95 ~4-8s；red_flag_safety 是預期 FALLBACK，不計入 system_failure_rate")
        print("     session-first/session-warm 只表示 session 量測順序，不宣稱模型或容器 cold-start")
        print("  5. 判斷: pure-intake 應落地；pure-education 不得污染；mixed 成功路徑最多 2 LLM calls；warm 追問同 session 狀態續存；紅旗必 FALLBACK")
        print("  6. CI dry-run: python scripts/p2a_live_smoke.py --dry-run -q")
        print("  7. Honest model calls: successful mixed turn up to 2 (interpreter + C generator); B insufficient can skip C; no rephraser/third call")

    if args.json_out:
        print("\n[JSON]")
        print(json.dumps({"mode": mode, "is_formal": is_formal_summary, "model": model_summary, "p50_ms": p50, "p95_ms": p95, "total_fallbacks": f"{fallback_count}/{total}", "fallback_categories": fallback_counts, "system_failure_rate": f"{system_fallback_count}/{total}", "observed_model_calls": total_model_calls, "session_first_turn": session_first_count, "session_warm_turn": session_warm_count, "process_first_measurement_in_report": process_first_count, "session_first_p50_ms": cold_p50, "session_first_p95_ms": cold_p95, "session_warm_p50_ms": warm_p50, "session_warm_p95_ms": warm_p95, "per_stage": per_stage_stats, "per_stage_session_first": per_stage_cold_stats, "per_stage_session_warm": per_stage_warm_stats, "results": results}, ensure_ascii=False, indent=2))

    if not dry_run and not is_formal_summary:
        print("\n[ERROR] 依賴失敗：無法建立 FormalConversationInterpreter / provider unreachable — live smoke 未真正連到 OpenCode (mimo-v2.5)，誠實報告為非 live")
        print("[RESULT] LIVE smoke 未就緒（非 Fake 冒充）— 以 non-zero 誠實報告")
        sys.exit(2)

    if not all_passed:
        print("\n[RESULT] SOME CHECKS FAILED")
        sys.exit(1)
    else:
        print("\n[RESULT] ALL CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
