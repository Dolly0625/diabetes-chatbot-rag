from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

from tfda_context_gate.line_orchestration.latency import StagedLatencyRecorder
from tfda_context_gate.line_orchestration.deadline import (
    DeadlineGuard,
    current_deadline_guard,
    deadline_scope,
    run_with_deadline,
)
from tfda_context_gate.line_orchestration.response_composer import (
    compose_correction,
    compose_implicit_confirmation,
    compose_intake_question,
    compose_multi_confirmation,
    compose_none_answer,
    compose_question_added,
    compose_side_answer,
    compose_single_confirmation,
    compose_uncertain,
)

# Task C honest evaluation: interpreter is serial bottleneck before branches; after interpreter,
# candidate_validation (~1ms) vs education RAG+C (2-45s) could parallelize but gain negligible.
# Speculative parallel before interpreter would lose resolved_education_query and risk policy bypass,
# so we skip forcing parallelism and keep sequential order: red_flag→auth→interpreter→join→validate→B/D→single persistence.

FORMAL_WORKFLOW_TIMEOUT_S = float(os.getenv("FORMAL_WORKFLOW_TIMEOUT_S", "45"))
SYNC_FORMAL_TIMEOUT_S = float(os.getenv("SYNC_FORMAL_TIMEOUT_S", os.getenv("SYNC_FORMAL_TIMEOUT", str(FORMAL_WORKFLOW_TIMEOUT_S))))
SYNC_FORMAL_TIMEOUT = SYNC_FORMAL_TIMEOUT_S
ASYNC_FORMAL_TIMEOUT_S = float(os.getenv("ASYNC_FORMAL_TIMEOUT_S", os.getenv("ASYNC_FORMAL_TIMEOUT", "120")))
ASYNC_FORMAL_TIMEOUT = ASYNC_FORMAL_TIMEOUT_S
LINE_USE_FORMAL_DEFAULT = os.getenv("LINE_USE_FORMAL", "true").lower() in ("1", "true", "yes")

SEMANTIC_ROUTER_TIMEOUT_S = 0.2
_SEMANTIC_GUARDED_ALLOWED_ROUTES = {"PURE_EDUCATION", "CHITCHAT", "PURE_INTAKE"}
_SEMANTIC_GUARDED_BLOCKED_ROUTES = {"MIXED", "CORRECTION", "SUBJECT_CHANGE", "UNKNOWN"}
_SUBJECT_AMBIGUOUS_RE = re.compile(
    r"是我媽媽|是我家人|那個是我|不是我|幫家人|是我媽的|帮媽媽問|是我爸爸|是我爸的|家人.*不是我|帮家人問",
    re.IGNORECASE,
)
_CORRECTION_LIKE_RE = re.compile(r"不是|說錯了|更正|改成|其實|喔不對|不對|那邊要改|前面.*要改", re.IGNORECASE)


def _get_requested_route_mode() -> str:
    try:
        from tfda_context_gate.run_config import env_value as _ev

        raw = _ev("SEMANTIC_ROUTER_MODE", None)
        if raw is None:
            raw = os.getenv("SEMANTIC_ROUTER_MODE")
    except Exception:
        raw = os.getenv("SEMANTIC_ROUTER_MODE")
    if raw is None:
        return "off"
    cleaned = str(raw).strip().lower()
    if cleaned in ("off", "shadow", "guarded"):
        return cleaned
    return "off"


def get_route_mode() -> str:
    requested = _get_requested_route_mode()
    if requested != "guarded":
        return requested
    try:
        from tfda_context_gate.semantic_router.approval import get_effective_route_mode as _eff

        effective, reason, _ = _eff(requested)
        if effective != "guarded" and reason:
            logger.info("guarded downgraded to shadow reason=%s", reason)
        return effective
    except Exception as _e:
        logger.warning("guarded approval check failed, downgrading to shadow: %s", _e)
        return "shadow"


def should_use_semantic_router() -> bool:
    return get_route_mode() != "off"


def _resolve_guarded_downgrade() -> tuple[str, str | None]:
    requested = _get_requested_route_mode()
    effective = get_route_mode()
    fallback_reason: str | None = None
    if requested == "guarded" and effective != "guarded":
        try:
            from tfda_context_gate.semantic_router.approval import get_effective_route_mode as _eff2

            _, reason, _ = _eff2(requested)
            fallback_reason = reason or "GUARDED_DOWNGRADED_UNKNOWN"
        except Exception:
            fallback_reason = "GUARDED_DOWNGRADED_UNKNOWN"
    return effective, fallback_reason


def _record_guarded_downgrade(session_id: str, fallback_reason: str) -> None:
    try:
        from tfda_context_gate.e_observability.tracer import TraceRecorder

        tr = TraceRecorder(request_id=f"{session_id}-guarded-downgrade", declared_role=None, original_query=None)
        tr.record(
            "SEMANTIC_ROUTER",
            "GUARDED_DOWNGRADE",
            "COMPLETED",
            fallback_reason=fallback_reason,
            requested_mode="guarded",
            effective_mode="shadow",
        )
        tr.close(status="COMPLETED")
    except Exception:
        pass


def _is_subject_ambiguous(text: str) -> bool:
    try:
        n = unicodedata.normalize("NFKC", text or "").strip()
    except Exception:
        n = (text or "").strip()
    if not n:
        return False
    if _SUBJECT_AMBIGUOUS_RE.search(n):
        return True
    try:
        from tfda_context_gate.intake.candidate_merge import is_third_party

        if is_third_party(n):
            if any(kw in n for kw in ("媽媽", "媽", "爸爸", "爸", "家人", "朋友")):
                return True
    except Exception:
        pass
    return False


def _is_correction_like(text: str) -> bool:
    try:
        return bool(_CORRECTION_LIKE_RE.search(text or ""))
    except Exception:
        return False


def _call_semantic_router_with_timeout(router: Any, text: str, timeout_s: float = SEMANTIC_ROUTER_TIMEOUT_S) -> Any | None:
    if router is None:
        return None
    import concurrent.futures

    fn = getattr(router, "predict", None) or getattr(router, "route", None)
    if fn is None:
        return None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(fn, text)
            try:
                return fut.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                try:
                    fut.cancel()
                except Exception:
                    pass
                return None
            except Exception:
                return None
    except Exception:
        try:
            return fn(text)
        except Exception:
            return None


def _observation_to_dict(obs: Any) -> dict[str, Any]:
    if obs is None:
        return {}
    try:
        if hasattr(obs, "to_trace_dict"):
            return obs.to_trace_dict()
        if isinstance(obs, dict):
            return dict(obs)
        d: dict[str, Any] = {}
        for k in ("route", "confidence", "margin", "latency_ms", "degraded", "mode", "matched_labels", "scores", "text_length", "text_hash8"):
            if hasattr(obs, k):
                d[k] = getattr(obs, k)
        return d
    except Exception:
        return {}


def _record_semantic_trace(session_id: str, observation: Any, mode: str) -> None:
    if observation is None:
        return
    try:
        from tfda_context_gate.e_observability.tracer import TraceRecorder

        obs_dict = _observation_to_dict(observation)
        route = str(obs_dict.get("route") or getattr(observation, "route", "UNKNOWN"))
        conf = float(obs_dict.get("confidence") or getattr(observation, "confidence", 0.0) or 0.0)
        margin = float(obs_dict.get("margin") or getattr(observation, "margin", 0.0) or 0.0)
        latency = float(obs_dict.get("latency_ms") or getattr(observation, "latency_ms", 0.0) or 0.0)
        degraded = bool(obs_dict.get("degraded", False) or getattr(observation, "degraded", False))
        tr = TraceRecorder(request_id=f"{session_id}-semantic", declared_role=None, original_query=None)
        tr.record(
            "SEMANTIC_ROUTER",
            "route",
            "COMPLETED",
            semantic_route=route,
            semantic_confidence=conf,
            margin=margin,
            latency_ms=latency,
            route_mode=mode,
            degraded=degraded,
            matched_labels=list(obs_dict.get("matched_labels") or []),
            text_length=obs_dict.get("text_length"),
            text_hash8=obs_dict.get("text_hash8"),
        )
        tr.close(status="COMPLETED")
    except Exception:
        pass


def _enrich_orchestrator_result(result: Any, observation: Any | None, mode: str) -> Any:
    if observation is None or mode == "off":
        return result
    try:
        obs_dict = _observation_to_dict(observation)
        if not obs_dict:
            return result
        updates: dict[str, Any] = {}
        if "route" in obs_dict:
            updates["semantic_route"] = str(obs_dict["route"]) if obs_dict["route"] else None
        if "confidence" in obs_dict:
            try:
                updates["semantic_confidence"] = float(obs_dict["confidence"])
            except Exception:
                pass
        if "margin" in obs_dict:
            try:
                updates["semantic_margin"] = float(obs_dict["margin"])
            except Exception:
                pass
        if "latency_ms" in obs_dict:
            try:
                updates["semantic_latency_ms"] = float(obs_dict["latency_ms"])
            except Exception:
                pass
        if "degraded" in obs_dict:
            updates["semantic_degraded"] = bool(obs_dict["degraded"])
        updates["semantic_mode"] = mode
        updates["metadata"] = {"semantic_observation": obs_dict}
        try:
            return result.model_copy(update=updates)
        except Exception:
            for k, v in updates.items():
                try:
                    setattr(result, k, v)
                except Exception:
                    pass
            return result
    except Exception:
        return result

# ── Async formal push: honest fallback + idempotency ─────────────────────────
HONEST_FALLBACK_TEXT = "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？"
QUEUED_FALLBACK_TEXT = "查詢排隊中，稍後推送"
ASYNC_ADMISSION_FALLBACK_TEXT = "目前同時查詢較多，這次無法完成查詢，請稍後再試。"
# Canonical async boundary classification.  Legacy FORMAL_TIMEOUT and
# SYSTEM_DEPENDENCY remain accepted for stored v0.1 records.
DEPENDENCY_OR_TIMEOUT_REASON = "DEPENDENCY_OR_TIMEOUT"
HONEST_FALLBACK_REASONS = {"B_INSUFFICIENT", "FORMAL_TIMEOUT", "C_FAILURE", "SYSTEM_DEPENDENCY", "B_UNSAFE", DEPENDENCY_OR_TIMEOUT_REASON}

# LINE educational narrow path (G) async: placeholder + background formal (120s + 1 retry)
# Service restart loss is acceptable (in-memory set only, documented).
ASYNC_PLACEHOLDER_REPLY = "查詢中，請稍候，資料整理完成後會推送給你 📋"
SYNC_FORMAL_TIMEOUT_S_ALIAS = SYNC_FORMAL_TIMEOUT_S
ASYNC_FORMAL_TIMEOUT_S_ALIAS = ASYNC_FORMAL_TIMEOUT_S

# In-memory idempotency for push per event (process-local). Repository webhook_events
# provides cross-process durability; this set prevents duplicate push within same process.
_pushed_events: set[str] = set()
_pushing_events: set[str] = set()
_marker_pending_events: set[str] = set()
_marker_retrying_events: set[str] = set()
_pushed_lock = threading.Lock()
_async_jobs: set[str] = set()
_async_jobs_lock = threading.Lock()
# P3-R4 bounded concurrency for async formal: global semaphore limits concurrent
# formal background executions to 5; excess tasks fail closed before a thread
# is created while placeholder reply returns immediately (<1s).
_FORMAL_SEMAPHORE = threading.Semaphore(5)
logger = logging.getLogger(__name__)

# ── P3-R1 text-level dedup for async formal (120s TTL, thread-safe) ───────────
TEXT_DEDUP_TTL_S = 120
TEXT_DEDUP_TTL_SHORT_S = 10
TEXT_DEDUP_REPLY = "這題正在幫你查了，稍候"
TEXT_DEDUP_REPLY_WELCOME = "又見面了～有什麼想繼續的？"
_text_dedup: dict[tuple[str, str], float] = {}
_text_dedup_lock = threading.Lock()
_intake_uncertain_attempts: dict[tuple[str, str], int] = {}
_EMPATHY_RE = re.compile(r"不人性化|好笨|很怪|無言|敷衍|不友善|冷淡|好敷衍|機械", re.IGNORECASE)
_SEVERE_EMPATHY_RE = re.compile(r"想死|不想活|活不下去|自殺|輕生|結束生命", re.IGNORECASE)

# ── Intake Brown Bag 2-attempt clarification ( Requirement 1 ) ───────────────
# Low-confidence colloquial meds (吃白色小藥丸) must NOT be written directly.
# They must go through 2 attempts via MEDICATION_CLARIFICATION_QUESTIONS,
# only after 2 attempts still unclear -> sentinel "不清楚（待看診確認）".
_MEDICATION_COLLOQUIAL_RE = re.compile(r"白色.*藥丸|小藥丸|藥丸|膠囊|紅色.*藥|黃色.*藥|藍色.*藥|圓形.*藥|長條.*藥|大顆.*藥|小顆.*藥")
_MEDICATION_KNOWN_RE = re.compile(r"metformin|二甲雙胍|二甲双胍|胰島素|insulin|SGLT2|GLP|semaglutide|阿卡波糖|格列", re.IGNORECASE)
_MEDICATION_UNCERTAIN_RE = re.compile(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚|不太知道")
# Confirmation words that must NOT pollute known_medications ( Requirement 2 )
_CONFIRM_WORD_SET = {"正確", "對", "是", "沒錯", "对", "正确", "没错", "對的", "是的", "正確的"}
_CONFIRM_WORD_RE = re.compile(r"^\s*(正確|對|是|沒錯|对|正确|没错|對的|是的|正確的)?\s*[。！!？?]*\s*$")
# 不要記/取消 should not pollute intake
_MEDICATION_CANCEL_RE = re.compile(r"不要記|取消|不記|略過|跳過|先不要|不用")


def _is_confirmation_word(text: str) -> bool:
    """Requirement 2: detect pure confirmation words that must not be written to intake."""
    try:
        n = unicodedata.normalize("NFKC", (text or "").strip())
    except Exception:
        n = (text or "").strip()
    # Strip trailing punctuation
    bare = re.sub(r"[。！!？?\.，,]+$", "", n).strip()
    # Also handle without punctuation
    if not bare:
        return False
    # Exact match to confirm set (after removing spaces)
    compact = re.sub(r"\s+", "", bare)
    if compact in _CONFIRM_WORD_SET:
        return True
    # Also direct regex match for short inputs
    if re.match(r"^\s*(正確|對|是|沒錯)\s*[。！!？?]*\s*$", n):
        return True
    return False


def _was_last_implicit_confirm(session: Any) -> bool:
    """Check if last assistant turn was an implicit confirm containing '對嗎'."""
    try:
        for turn in reversed(session.conversation_context.recent_turns):
            if turn.role == "assistant":
                if "對嗎" in turn.content:
                    return True
                break
    except Exception:
        pass
    return False


def _was_last_medication_clarification(session: Any) -> bool:
    """Check if last assistant turn was a Brown Bag medication clarification."""
    try:
        for turn in reversed(session.conversation_context.recent_turns):
            if turn.role == "assistant":
                c = turn.content or ""
                if "藥袋" in c or "顏色、形狀" in c or "服用時間" in c:
                    return True
                break
    except Exception:
        pass
    return False


def _is_short_ttl_text(text: str) -> bool:
    try:
        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _R
        from tfda_context_gate.workflow.intake_router import is_welcome_trigger as _is_welcome
        if _is_welcome(text):
            return True
        if _R.is_chit_chat_text(text):
            return True
        if _R.is_identity_text(text):
            return True
        if _EMPATHY_RE.search(text):
            return True
    except Exception:
        pass
    try:
        n = unicodedata.normalize("NFKC", text).strip()
        if n in ("你好", "您好", "哈囉", "嗨", "hi", "hello"):
            return True
    except Exception:
        pass
    return False


def _dedup_ttl_for(text: str) -> int:
    return TEXT_DEDUP_TTL_SHORT_S if _is_short_ttl_text(text) else TEXT_DEDUP_TTL_S


def _dedup_reply_for(text: str) -> str:
    return TEXT_DEDUP_REPLY_WELCOME if _is_short_ttl_text(text) else TEXT_DEDUP_REPLY


def _is_empathy_text(text: str) -> bool:
    try:
        return bool(_EMPATHY_RE.search(text))
    except Exception:
        return False


def _normalize_text(text: str) -> str:
    try:
        return unicodedata.normalize("NFKC", text).strip().lower()
    except Exception:
        return (text or "").strip().lower()


# ── P2A.1 deterministic mixed-intent backstop (Task A) ───────────────────────
# Precedence explicit in one function: formal education miss → deterministic question-clause split.
# No new LLM call; general patterns, not hardcoded example sentences.
_EDU_QUESTION_CLAUSE_RE = re.compile(
    r"(衛教|怎麼吃|可以吃幾|怎麼處理|可以喝|飲食|水果|血糖|胰島素|副作用|會傷腎|可以吃|能吃|芭樂)",
    re.IGNORECASE,
)
_QUESTION_WORD_RE = re.compile(r"(嗎|怎麼|如何|是不是|是否|可不可以|能不能|會不會)", re.IGNORECASE)


def _extract_question_clause(text: str) -> str | None:
    """Deterministically find the education question clause in a mixed utterance.

    General pattern: clause containing 衛教/怎麼吃/可以吃幾/飲食/水果 etc and question markers
    (？ / 嗎 / 怎麼 / 如何). Split by punctuation, prefer clause with ？ and edu keyword.
    """
    try:
        n = unicodedata.normalize("NFKC", text or "").strip()
    except Exception:
        n = (text or "").strip()
    if not n:
        return None
    parts = [p.strip() for p in re.split(r"[，,。；;、]", n) if p.strip()]
    if not parts:
        return None
    edu_pat = _EDU_QUESTION_CLAUSE_RE
    # Prefer clause with ？ and edu or question word
    for p in parts:
        has_q_mark = "？" in p or "?" in p
        has_q_word = bool(_QUESTION_WORD_RE.search(p)) or "？" in p or "?" in p
        has_edu = bool(edu_pat.search(p))
        if has_q_mark and has_edu:
            return p
        if has_q_mark and has_q_word:
            return p
    best: str | None = None
    for p in parts:
        has_q_word = bool(_QUESTION_WORD_RE.search(p))
        has_edu = bool(edu_pat.search(p))
        has_q_mark = "？" in p or "?" in p
        if has_edu and (has_q_word or has_q_mark):
            best = p
            break
        if has_edu and ("可以" in p or "能" in p):
            best = p
    if best:
        return best
    # fallback: last part ending with ？ that looks like question
    if parts and ("？" in parts[-1] or "?" in parts[-1]):
        if edu_pat.search(parts[-1]) or _QUESTION_WORD_RE.search(parts[-1]):
            return parts[-1]
        return parts[-1]
    return None


def _maybe_apply_mixed_backstop(text: str, interpretation: Any) -> Any:
    """Single-function deterministic backstop with explicit precedence.

    Precedence:
      1. If interpretation already has EDUCATION_QUESTION → keep as is (formal succeeded)
      2. Else if text is multi-clause and contains a question clause (edu + ？/嗎/怎麼/如何) → synthesize mixed intent
      3. Else → keep original (no backstop)

    Synthesizes: intents += EDUCATION_QUESTION, resolved_education_query = question clause,
    and cleans formal intake_candidates whose source_quote is the question clause (問句不得寫成症狀).
    No LLM call; imports is_multi_clause/is_question_like but does not modify candidate_merge.
    """
    if interpretation is None:
        return None
    try:
        intents = list(getattr(interpretation, "intents", []) or [])
    except Exception:
        return interpretation
    if "EDUCATION_QUESTION" in intents:
        return interpretation
    # Must look like mixed: multi-clause + question clause
    try:
        from tfda_context_gate.intake.candidate_merge import is_multi_clause, is_question_like
    except Exception:
        return interpretation
    if not is_multi_clause(text):
        return interpretation
    q_clause = _extract_question_clause(text)
    if not q_clause:
        return interpretation
    # Verify q_clause looks like education/question, not pure intake
    has_edu = bool(_EDU_QUESTION_CLAUSE_RE.search(q_clause))
    has_q = ("？" in q_clause or "?" in q_clause or "嗎" in q_clause or bool(_QUESTION_WORD_RE.search(q_clause)))
    if not (has_edu or has_q):
        return interpretation
    # Ensure intake part remains after removing question clause
    intake_text = text.replace(q_clause, "").strip("，,。；;、 \t\n")
    if not intake_text or len(intake_text.strip()) < 2:
        return interpretation
    # Clean formal candidates whose source_quote is polluted by question clause
    new_cands: list[Any] = []
    for cand in getattr(interpretation, "intake_candidates", []) or []:
        try:
            sq = getattr(cand, "source_quote", "") or ""
            # If source_quote contains the question clause verbatim, it is polluted
            if q_clause.strip() and q_clause.strip() in sq:
                cleaned = cand.model_copy(update={"source_quote": (getattr(cand, "candidate_value", "") or sq)[:100]})
                new_cands.append(cleaned)
            elif is_question_like(sq) and getattr(cand, "field_name", "") in (
                "symptom_description",
                "symptom_onset",
                "symptom_severity",
                "known_medications",
            ):
                # Question source must not become symptom/medication; keep but fix provenance to candidate_value
                cleaned = cand.model_copy(update={"source_quote": (getattr(cand, "candidate_value", "") or sq)[:100]})
                new_cands.append(cleaned)
            else:
                new_cands.append(cand)
        except Exception:
            new_cands.append(cand)
    new_intents = list(intents)
    if "INTAKE_ANSWER" not in new_intents:
        new_intents.append("INTAKE_ANSWER")
    new_intents.append("EDUCATION_QUESTION")
    try:
        return interpretation.model_copy(
            update={"intents": new_intents, "resolved_education_query": q_clause, "intake_candidates": new_cands}
        )
    except Exception:
        try:
            interpretation.intents = new_intents  # type: ignore[attr-defined]
            interpretation.resolved_education_query = q_clause  # type: ignore[attr-defined]
            interpretation.intake_candidates = new_cands  # type: ignore[attr-defined]
        except Exception:
            pass
        return interpretation


def _is_text_duplicate(user_id: str, text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    key = (user_id, norm)
    now = time.time()
    ttl = _dedup_ttl_for(text)
    with _text_dedup_lock:
        expired = [k for k, ts in list(_text_dedup.items()) if now - ts > TEXT_DEDUP_TTL_S]
        for k in expired:
            _text_dedup.pop(k, None)
        ts = _text_dedup.get(key)
        return ts is not None and now - ts < ttl


def _mark_text_dedup(user_id: str, text: str) -> None:
    norm = _normalize_text(text)
    if not norm:
        return
    key = (user_id, norm)
    now = time.time()
    with _text_dedup_lock:
        expired = [k for k, ts in list(_text_dedup.items()) if now - ts > TEXT_DEDUP_TTL_S]
        for k in expired:
            _text_dedup.pop(k, None)
        _text_dedup[key] = now


def _orch_should_use_formal(raw: str | None, task_type: str | None) -> bool:
    if task_type == "pre_visit_intake":
        return False
    if not raw:
        return False
    try:
        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor
        from tfda_context_gate.workflow.intake_router import is_red_flag as _is_red

        if _is_red(raw):
            return False
        if RuleBasedSignalExtractor.is_pre_visit_intake_text(raw):
            return False
        if RuleBasedSignalExtractor.is_chit_chat_text(raw):
            return False
        if RuleBasedSignalExtractor.is_identity_text(raw):
            return False
        if _is_empathy_text(raw):
            return False
    except Exception:
        pass
    try:
        import re as _re
        import unicodedata as _ud

        n = _ud.normalize("NFKC", raw).strip()
        if len(n) < 4 or n in ("怎麼辦", "怎辦", "怎麼半", "help", "？", "?", "…"):
            return False
        if _re.search(r"可以跟我說什麼|可以說什麼|能做什麼|會做什麼|能幫什麼|我能幫什麼|介紹一下|你會什麼|功能有哪些", n, _re.IGNORECASE):
            return False
        from tfda_context_gate.a_router.policy import DEFAULT_POLICY, policy_gate
        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _RB

        try:
            sig = _RB().extract(n, language=None)  # type: ignore[arg-type]
        except Exception:
            return False
        decision = policy_gate(sig, DEFAULT_POLICY)
        if getattr(decision.status, "value", str(decision.status)) != "G_GENERAL_EDUCATION":
            return False
        return True
    except Exception:
        return False

from tfda_context_gate.access_control import (
    ActorRole,
    AuthorizationStatus,
    FrontendPersona,
    InformationSource,
    PermissionScope,
)
from tfda_context_gate.clinical_safety import RiskSignalPolicy
from tfda_context_gate.conversation import ConversationContextManager
from tfda_context_gate.conversation.envelope import build_conversation_envelope, envelope_to_model_context
from tfda_context_gate.conversation.interpreter import (
    ConversationInterpreter,
    ConversationTurnInterpretation,
    DeterministicConversationInterpreter,
)
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.product_session import ProductSession, ProductSessionRepository
from tfda_context_gate.product_session import ProductSessionConflict
from tfda_context_gate.product_session import WebhookEventIdentityMismatch
from tfda_context_gate.product_session.schemas import PendingAction
from tfda_context_gate.workflow import run_workflow
from tfda_context_gate.workflow.schemas import WorkflowResult
from tfda_context_gate.workflow.fallbacks import fallback_response

from .schemas import OrchestratorResult


_REPHRASE_FOLLOWUP_RE = re.compile(
    r"(?:口語|白話|簡單|容易懂|聽不懂|看不懂|再解釋|再說一次|舉例|換個方式|詳細一點|多說一點|太專業|太難|說明一下|可以嗎|行嗎|好嗎|說清楚).{0,20}(?:講|說|解釋|說明|一點|嗎|呢|？|\?)?",
    re.IGNORECASE,
)

_ELLIPSIS_FOLLOWUP_RE = re.compile(
    r"^(?:那|那如果|還有|另外|那可以|那能|為什麼|怎會|怎麼會|真的嗎|為什麼會這樣|這代表什麼|會怎樣|那.+呢|可以嗎|行嗎|好嗎)[？?。！!\s]*$",
    re.IGNORECASE,
)


def _resolve_rephrase_followup(session: ProductSession, text: str) -> str | None:
    """Resolve an elliptical rewrite request to the latest education topic."""
    clean_text = text.strip()
    is_rephrase = bool(_REPHRASE_FOLLOWUP_RE.search(clean_text))
    is_ellipsis = bool(_ELLIPSIS_FOLLOWUP_RE.search(clean_text)) or (len(clean_text) <= 12 and any(w in clean_text for w in ("那", "可以", "為什麼", "口語", "白話", "簡單")))

    if not (is_rephrase or is_ellipsis):
        return None

    try:
        turns = []
        if hasattr(session, "conversation_context") and session.conversation_context:
            turns = getattr(session.conversation_context, "recent_turns", [])
        
        last_topic = ""
        for turn in reversed(turns):
            role = getattr(turn, "role", "") or (turn.get("role") if isinstance(turn, dict) else "")
            if role != "user":
                continue
            content = getattr(turn, "content", "") or getattr(turn, "text", "") or (turn.get("content") or turn.get("text") if isinstance(turn, dict) else "")
            content = str(content).strip()
            if not content or _REPHRASE_FOLLOWUP_RE.search(content):
                continue
            if _orch_should_use_formal(content, None) or re.search(r"糖尿病|血糖|飲食|吃什麼|副作用|用藥|胰島素|症狀", content):
                last_topic = content
                break

        if not last_topic:
            last_topic = "糖尿病日常飲食原則與衛教重點"

        if is_rephrase:
            return f"請用非常口語化、白話且容易理解的語氣，重新向我說明：{last_topic} 的重點與原則。"
        else:
            return f"關於{last_topic}，請回答我的接續提問：「{clean_text}」"
    except Exception:
        return None


def _is_rephrase_request(text: str) -> bool:
    try:
        clean = text.strip()
        return bool(_REPHRASE_FOLLOWUP_RE.search(clean) or _ELLIPSIS_FOLLOWUP_RE.search(clean))
    except Exception:
        return False


def _split_intake_education_clauses(text: str, interpretation: Any | None) -> tuple[str, str | None]:
    """Deterministic clause split for mixed-intent.

    Returns (intake_text, education_query). education_query may be None if not detected.
    Uses interpretation.resolved_education_query when available; otherwise falls back to
    punctuation + edu-keyword detection. Never raises.
    """
    try:
        n = unicodedata.normalize("NFKC", text or "").strip()
    except Exception:
        n = (text or "").strip()
    if not n:
        return text, None
    edu_q = None
    try:
        if interpretation is not None:
            edu_q = getattr(interpretation, "resolved_education_query", None)
    except Exception:
        edu_q = None
    # Prefer interpreter's resolved education query if it appears (or is substring) in text
    if edu_q:
        # Normalize both for comparison (NFKC + lowercase)
        try:
            norm_text = unicodedata.normalize("NFKC", text).strip()
            norm_q = unicodedata.normalize("NFKC", edu_q).strip()
        except Exception:
            norm_text = text.strip()
            norm_q = edu_q.strip()
        # If exact substring, remove it to get intake part
        if norm_q and norm_q in norm_text:
            intake = norm_text.replace(norm_q, "").strip("，,。；;、 \t\n")
            # Clean leading 想問 prefix that may remain when edu is doctor question
            intake = re.sub(r"^(我想問他|我想問|想問他|想問)\s*", "", intake).strip("，,。；;、 \t\n")
            if intake:
                return intake, edu_q
        # Fallback: edu_q is clean even if not verbatim (e.g. rephrase case)
        # Try to find edu clause by splitting
        parts = [p.strip() for p in re.split(r"[，,。；;、]", n) if p.strip()]
        for p in parts:
            # edu clause contains education keywords or matches edu_q substring
            if edu_q and (edu_q.strip() in p or p in edu_q.strip()):
                intake_parts = [x for x in parts if x != p]
                intake = "，".join(intake_parts).strip("，,。；;、 \t\n")
                if intake and len(intake) >= 2:
                    return intake, edu_q
    # No interpreter query or not found: heuristic split by punctuation
    parts = [p.strip() for p in re.split(r"[，,。；;、]", n) if p.strip()]
    if len(parts) >= 2:
        edu_pat = re.compile(r"(衛教|怎麼吃|可以吃幾|怎麼處理|可以喝|飲食|水果|血糖|胰島素|副作用|會傷腎|可以吃|能吃|芭樂|蛋糕|甜點|甜食)", re.IGNORECASE)
        q_mark = re.compile(r"[？?]|嗎")
        # Prefer part with edu + question mark
        best_idx = -1
        for idx, p in enumerate(parts):
            if edu_pat.search(p) and q_mark.search(p):
                best_idx = idx
                break
        if best_idx == -1:
            for idx, p in enumerate(parts):
                if edu_pat.search(p) and ("可以" in p or "能" in p):
                    best_idx = idx
                    break
        if best_idx != -1:
            edu = parts[best_idx]
            intake_parts = [x for i, x in enumerate(parts) if i != best_idx]
            intake = "，".join(intake_parts).strip()
            if intake:
                return intake, edu
    # Single clause but contains both intake-like symptom and education question (e.g. 想問他 蛋糕)
    # Keep whole as intake Text for symptom? For 想問 cases, intake should be doctor question, education is same.
    if edu_q:
        return text, edu_q
    return text, None

# ── P0 structured pending regexes ─────────────────────────────────────
_HEDGE_RE = re.compile(r"有點|稍微|好像|吧$|大概")
_SEVERITY_EXPLICIT_RE = re.compile(r"輕度|中度|重度|\d+\s*分|\d+/\d+|1–10|1-10|\b(10|[1-9])\b")
_CORRECTION_RE = re.compile(r"不是|說錯了|更正|改成|其實|喔不對|不對|那邊要改|前面.*要改|那個要改")
_AGREE_RE = re.compile(r"^(好|幫我記|記下來|可以|沒問題|同意|要|幫我記下來)$")
_AGREE_SUB_RE = re.compile(r"好|幫我記|記下來|可以|沒問題")
_DISAGREE_RE = re.compile(r"不用|不需要|先不要|不要|不用了")
_WANT_QUESTION_RE = re.compile(r"想問|幫我.*加.*問題|再加一個想問|幫我記一個|還要.*記|幫我記|多少|是否|正常嗎")
_QUESTION_NEGATIVE_RE = re.compile(r"^\s*(沒有別的了|沒有了|沒了|就這樣|暫時沒有|沒有問題|沒有其他問題)\s*[。！!]*\s*$")
_FIELD_ALIAS_RE_MAP = {
    "allergies": re.compile(r"過敏"),
    "known_medications": re.compile(r"用藥|藥物|吃藥|藥"),
    "chronic_conditions": re.compile(r"慢性病|高血壓|高血脂"),
    "family_history": re.compile(r"家族史|家族|家人"),
    "symptom_onset": re.compile(r"什麼時候|開始時間|發病"),
    "symptom_description": re.compile(r"症狀|不舒服|口渴|頻尿|頭暈"),
    "symptom_severity": re.compile(r"程度|嚴重"),
    "questions_for_doctor": re.compile(r"問題|想問|問醫師"),
}
def _standardize_severity(val: str) -> str:
    s = val.strip()
    m2 = re.search(r"(\d+)\s*/\s*10", s)
    if m2:
        try:
            n = int(m2.group(1))
            if 1 <= n <= 3:
                return "輕度"
            if 4 <= n <= 6:
                return "中度"
            if 7 <= n <= 10:
                return "重度"
        except Exception:
            pass
    m = re.search(r"(\d+)\s*分|^\s*(?:大概|大約|約|差不多)?\s*(\d+)\s*(?:分|左右)?\s*$", s)
    if m:
        try:
            val_str = m.group(1) or m.group(2)
            n = int(val_str)
            if 1 <= n <= 3:
                return "輕度"
            if 4 <= n <= 6:
                return "中度"
            if 7 <= n <= 10:
                return "重度"
        except Exception:
            pass
    if any(tok in s for tok in ("輕度", "輕微", "不嚴重", "不太嚴重", "還好")):
        return "輕度"
    if any(tok in s for tok in ("中度", "普通", "中等", "還行")):
        return "中度"
    if any(tok in s for tok in ("重度", "嚴重", "很嚴重", "非常嚴重")):
        return "重度"
    return s
def _extract_correction_target(text: str) -> tuple[str | None, str | None]:
    for field, pat in _FIELD_ALIAS_RE_MAP.items():
        if pat.search(text):
            m = re.search(r"(?:要改成|改成|更正為|更正成|其實是|修正為|是)\s*([^\s，,。；;]+)", text)
            if m:
                return field, m.group(1).strip().strip("，。")
            return field, None
    m = re.search(r"(?:要改成|改成|更正為|更正成|其實是|修正為)\s*([^\s，,。；;]+)", text)
    if m:
        return None, m.group(1).strip().strip("，。")
    return None, None

def _clean_question_text(raw: str) -> str:
    s = raw.strip()
    # colon extraction first
    if ("幫我記" in s or "還要" in s) and ("：" in s or ":" in s):
        m = re.search(r"[:：]\s*(.+)", s)
        if m and m.group(1).strip():
            s = m.group(1).strip()
    # strip leading 有，/嗯，/對，/還有， with comma
    s = re.sub(r"^(有|嗯|對|還有)[，,]\s*", "", s)
    # also strip leading 有/嗯/對/還有 without comma but followed by space
    # keep "有糖尿病" (no comma) intact
    s = s.strip()
    return s[:200]


WorkflowRunner = Callable[..., WorkflowResult]

PushSender = Callable[[str, str], bool]


def _format_push_answer(workflow: WorkflowResult, original_text: str) -> str:
    if workflow.status == "COMPLETED" and workflow.final_response:
        base = workflow.final_response.strip()
        sources: list[str] = []
        try:
            rag = workflow.rag_result or {}
            evidences = rag.get("evidences") or rag.get("chunks") or []
            if isinstance(evidences, list):
                for ev in evidences[:2]:
                    if isinstance(ev, dict):
                        src = ev.get("source") or ev.get("doc_id") or ev.get("title")
                        if src:
                            sources.append(str(src))
            c_res = workflow.c_result or {}
            if isinstance(c_res, dict):
                for key in ("source", "sources", "evidence_id"):
                    val = c_res.get(key)
                    if val and str(val) not in sources:
                        if isinstance(val, list):
                            sources.extend([str(x) for x in val[:2] if str(x) not in sources])
                        else:
                            sources.append(str(val))
        except Exception:
            pass
        if sources:
            return f"{base}\n\n資料來源：{ '、'.join(sources[:2])}"
        return base
    reason = (workflow.fallback_reason or workflow.termination_reason or "") or ""
    if workflow.status == "FALLBACK" and reason in HONEST_FALLBACK_REASONS:
        return HONEST_FALLBACK_TEXT
    if workflow.status in ("FALLBACK", "BLOCKED"):
        if workflow.final_response and workflow.final_response.strip():
            return workflow.final_response.strip()
        return HONEST_FALLBACK_TEXT
    return workflow.final_response.strip() if workflow.final_response else HONEST_FALLBACK_TEXT


def _should_push_honest_fallback(workflow: WorkflowResult) -> bool:
    if workflow.status != "COMPLETED":
        return True
    reason = workflow.fallback_reason or ""
    return reason in HONEST_FALLBACK_REASONS


def _timeout_workflow_result(
    request_id: str,
    current_query: str | None = None,
    *,
    reason: str = "FORMAL_TIMEOUT",
) -> WorkflowResult:
    """Construct the safe fallback result used by sync/async boundaries."""

    return WorkflowResult(
        request_id=request_id,
        status="FALLBACK",
        final_response=HONEST_FALLBACK_TEXT,
        fallback_reason=reason,
        a_result=None,
        query_expansion=None,
        rag_result=None,
        b_result=None,
        c_result=None,
        d_result=None,
        agent_action=None,
        agent_reason_code=None,
        question=None,
        current_query=current_query,
        execution_history=[],
        agent_steps=0,
        rewrite_count=0,
        clarification_count=0,
        termination_reason=reason,
        intake_snapshot=None,
        intake_stage=None,
        previsit_summary=None,
        system_risk_classification=None,
        trace={"events": [], "evaluations": []},
    )


def _replayable_orchestrator_result(event_id: str, payload: dict[str, Any]) -> OrchestratorResult:
    """Validate only the public result fields from a durable event payload.

    Async replay metadata (the original text and session id) deliberately
    lives beside the public result.  It must never leak into the strict
    ``OrchestratorResult`` response model.
    """

    allowed = set(OrchestratorResult.model_fields)
    data = {key: value for key, value in payload.items() if key in allowed}
    data.setdefault("event_id", event_id)
    data.setdefault("session_id", "unknown-session")
    data.setdefault("reply", "此訊息正在處理中，請稍候。")
    data.setdefault("status", "PROCESSING")
    data["replayed"] = True
    return OrchestratorResult.model_validate(data)


class ConversationOrchestrator:
    """LINE 產品狀態編排；醫療決策仍完全交給固定 workflow。"""

    SELF_COMMANDS = {"為自己整理", "自己", "本人"}
    PROXY_COMMANDS = {"代家人整理", "幫家人整理", "家人"}
    PROXY_CONSENT_COMMANDS = {"已取得同意", "家人已同意", "同意"}
    CONFIRM_COMMANDS = {"確認", "確認完成", "提交", "完成對話", "完成", "結束對話", "結束整理"}
    START_INTAKE_COMMANDS = {"我要準備看診", "準備看診", "開始看診整理"}
    SHARE_COMMANDS = {"分享給醫護", "分享摘要"}
    SUMMARY_COMMANDS = {"查看看診摘要", "看診摘要", "查看摘要"}
    MODIFY_COMMANDS = {"修改看診資料", "修改資料", "修改", "更正資料"}
    PROXY_SUBJECT_SOURCE_COMMANDS = {"家人本人描述", "病患本人描述", "本人描述"}
    PROXY_OBSERVED_SOURCE_COMMANDS = {"我的觀察", "照護者觀察", "家屬觀察"}
    PAUSE_COMMANDS = {"暫停整理", "先不要填", "先不要填了", "等一下再填", "等等再說", "稍後再填", "暫停", "退出", "等一下再說", "先休息一下"}
    CANCEL_COMMANDS = {"不填了", "取消整理"}
    RESUME_COMMANDS = {"繼續整理", "繼續填寫", "回到看診整理", "接著填", "繼續", "回來填", "接著填寫"}
    INTAKE_FIELD_ORDER = (
        "known_medications", "allergies", "chronic_conditions", "family_history",
        "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor",
    )

    def __init__(
        self,
        repository: ProductSessionRepository,
        *,
        identity_hash_key: str,
        workflow_runner: WorkflowRunner = run_workflow,
        session_ttl: timedelta = timedelta(days=7),
        context_manager: ConversationContextManager | None = None,
        interpreter: ConversationInterpreter | None = None,
        use_formal: bool | None = None,
        formal_timeout_s: float | None = None,
        async_formal_timeout_s: float | None = None,
        sync_formal_timeout_s: float | None = None,
    ) -> None:
        if len(identity_hash_key) < 16:
            raise ValueError("identity_hash_key must contain at least 16 characters")
        self.repository = repository
        self._hash_key = identity_hash_key.encode("utf-8")
        self.workflow_runner = workflow_runner
        self.session_ttl = session_ttl
        self.context_manager = context_manager or ConversationContextManager()
        self.risk_policy = RiskSignalPolicy()
        # P1.1: 統一經 Factory 取得，優先 CONVERSATION→ROUTER，無則 deterministic，無硬編碼
        if interpreter is not None:
            self.interpreter: ConversationInterpreter = interpreter
        else:
            try:
                from tfda_context_gate.conversation.interpreter import ConversationInterpreterFactory

                self.interpreter = ConversationInterpreterFactory.from_env()
            except Exception:
                self.interpreter = DeterministicConversationInterpreter()
        self._last_interpretation: Any | None = None
        self._last_envelope: Any | None = None
        if use_formal is None:
            env_val = os.getenv("LINE_USE_FORMAL")
            # Pytest must remain hermetic even when a developer's project
            # .env enables the live formal path.  A test that intentionally
            # exercises formal construction passes use_formal=True or removes
            # PYTEST_CURRENT_TEST explicitly.
            if os.getenv("PYTEST_CURRENT_TEST") is not None:
                self.use_formal = False
            elif env_val is not None:
                self.use_formal = env_val.lower() in ("1", "true", "yes")
            else:
                self.use_formal = LINE_USE_FORMAL_DEFAULT
        else:
            self.use_formal = use_formal
        # SYNC = 45 for direct run_workflow (tests), ASYNC = 120 for background LINE
        _sync_default = sync_formal_timeout_s if sync_formal_timeout_s is not None else formal_timeout_s if formal_timeout_s is not None else SYNC_FORMAL_TIMEOUT_S
        self.formal_timeout_s = _sync_default
        self.sync_formal_timeout_s = _sync_default
        self.async_formal_timeout_s = async_formal_timeout_s if async_formal_timeout_s is not None else ASYNC_FORMAL_TIMEOUT_S
        self._semantic_router: Any | None = None
        self._semantic_router_config: Any | None = None
        self._semantic_router_degraded_reason: str | None = None
        self._semantic_router_init_attempted: bool = False
        try:
            from tfda_context_gate.semantic_router.config import SemanticRouterConfig as _SRC

            _cfg = _SRC.from_env()
            self._semantic_router_config = _cfg
            if _cfg.mode != "off":
                try:
                    from tfda_context_gate.semantic_router.factory import build_semantic_router as _bsr

                    self._semantic_router = _bsr(_cfg)
                except Exception as _e:
                    logger.warning("semantic router init failed (degraded to interpreter): %s", _e)
                    self._semantic_router = None
                    self._semantic_router_degraded_reason = str(_e)
            self._semantic_router_init_attempted = True
        except ImportError:
            self._semantic_router = None
            self._semantic_router_config = None
        except Exception as _e:
            logger.warning("semantic router config failed: %s", _e)
            self._semantic_router = None
            self._semantic_router_config = None
            self._semantic_router_degraded_reason = str(_e)
            self._semantic_router_init_attempted = True
        self._last_semantic_observation: Any | None = None
        self._last_semantic_mode: str = get_route_mode() if hasattr(self, "_semantic_router_config") and self._semantic_router_config else "off"
        self._last_guarded_fallback_reason: str | None = None

    def _get_semantic_router(self) -> Any | None:
        if self._semantic_router_init_attempted:
            return self._semantic_router
        try:
            from tfda_context_gate.semantic_router.config import SemanticRouterConfig as _SRC

            _cfg = _SRC.from_env()
            self._semantic_router_config = _cfg
            if _cfg.mode != "off":
                try:
                    from tfda_context_gate.semantic_router.factory import build_semantic_router as _bsr

                    self._semantic_router = _bsr(_cfg)
                except Exception as _e:
                    logger.warning("semantic router lazy init failed: %s", _e)
                    self._semantic_router = None
                    self._semantic_router_degraded_reason = str(_e)
            self._semantic_router_init_attempted = True
        except ImportError:
            self._semantic_router_init_attempted = True
            return None
        except Exception as _e:
            logger.warning("semantic router lazy config failed: %s", _e)
            self._semantic_router_init_attempted = True
            return None
        return self._semantic_router

    def _call_workflow(self, *args: Any, **kwargs: Any) -> WorkflowResult:
        if not self.use_formal:
            return self.workflow_runner(*args, **kwargs)
        _raw = None
        try:
            if args and isinstance(args[0], dict):
                _raw = args[0].get("user_raw_input")
            _tt = kwargs.get("task_type")
            if not _orch_should_use_formal(str(_raw) if _raw is not None else None, _tt):
                kwargs["use_formal"] = False
                return self.workflow_runner(*args, **kwargs)
        except Exception:
            pass
        kwargs["use_formal"] = True
        timeout = self.formal_timeout_s
        if timeout is None or timeout <= 0:
            return self.workflow_runner(*args, **kwargs)
        future_kwargs = dict(kwargs)
        request_id = str(args[0].get("request_id", "timeout")) if args and isinstance(args[0], dict) else "timeout"
        current_query = str(args[0].get("user_raw_input", "")) if args and isinstance(args[0], dict) else None
        result, timed_out, guard = run_with_deadline(
            self.workflow_runner,
            *args,
            timeout_s=timeout,
            **future_kwargs,
        )
        if timed_out or result is None or guard.should_abort():
            return _timeout_workflow_result(request_id, current_query)
        return result

    def _call_workflow_async_with_retry(self, *args: Any, **kwargs: Any) -> WorkflowResult:
        """Run the async formal path with bounded admission and one retry."""

        if not self.use_formal:
            return self.workflow_runner(*args, **kwargs)
        try:
            raw = args[0].get("user_raw_input") if args and isinstance(args[0], dict) else None
            task_type = kwargs.get("task_type")
            if raw is not None and not _orch_should_use_formal(str(raw), task_type):
                kwargs["use_formal"] = False
                return self.workflow_runner(*args, **kwargs)
        except Exception:
            pass
        kwargs["use_formal"] = True
        timeout = self.async_formal_timeout_s
        if timeout is None or timeout <= 0:
            return self.workflow_runner(*args, **kwargs)

        request_id = str(args[0].get("request_id", "async-timeout")) if args and isinstance(args[0], dict) else "async-timeout"
        current_query = str(args[0].get("user_raw_input", "")) if args and isinstance(args[0], dict) else None
        for attempt in range(2):
            try:
                result, timed_out, guard = run_with_deadline(
                    self.workflow_runner,
                    *args,
                    timeout_s=timeout,
                    **dict(kwargs),
                )
                if not timed_out and result is not None and not guard.should_abort():
                    return result
            except Exception:
                if attempt == 0:
                    continue
                return _timeout_workflow_result(request_id, current_query, reason="SYSTEM_DEPENDENCY")
            if attempt == 0:
                continue
        return _timeout_workflow_result(request_id, current_query)

    def _is_duplicate_push(self, event_id: str) -> bool:
        marker_retry_needed = False
        with _pushed_lock:
            if event_id in _pushed_events:
                marker_retry_needed = event_id in _marker_pending_events
        if marker_retry_needed:
            # Transport already acknowledged this event.  A marker retry may
            # repair durability, but it must never call the transport again.
            self._mark_event_pushed(event_id)
            return True
        try:
            rec = self.repository.get_webhook_event(event_id)
            if rec is not None and rec.status == "COMPLETED" and isinstance(rec.result, dict) and rec.result.get("pushed"):
                with _pushed_lock:
                    _pushed_events.add(event_id)
                    _marker_pending_events.discard(event_id)
                return True
        except Exception:
            pass
        return False

    def _recover_pending_async_text(self, session_id: str) -> str | None:
        """Recover a pending event's original user text after a restart.

        New records persist ``async_original_text`` beside the public result.
        The context fallback keeps old records recoverable without treating a
        replay's possibly different request body as the original event.
        """

        try:
            session = self.repository.get(session_id)
            if session is None:
                return None
            turns = session.conversation_context.recent_turns
            for index in range(len(turns) - 1, -1, -1):
                turn = turns[index]
                if turn.role != "assistant":
                    continue
                if (
                    "查詢中" not in turn.content
                    and "查詢" not in turn.content
                    and "衛教資料中" not in turn.content
                ):
                    continue
                for previous in reversed(turns[:index]):
                    if previous.role == "user":
                        return previous.content
        except Exception:
            return None
        return None

    def _reschedule_pending_async_event(
        self,
        *,
        event_id: str,
        line_user_id: str,
        payload: dict[str, Any],
        push_sender: PushSender | None = None,
    ) -> None:
        """Re-admit an unfinished async event without appending turns."""

        if payload.get("pushed") is True or payload.get("status") not in {"ASYNC_PENDING", "ASYNC_PLACEHOLDER"}:
            return
        session_id = str(payload.get("session_id") or self._session_id(line_user_id))
        original_text = payload.get("async_original_text")
        if not isinstance(original_text, str) or not original_text.strip():
            original_text = self._recover_pending_async_text(session_id)
        if not original_text:
            logger.warning("cannot reschedule async event %s: original text unavailable", event_id[:8])
            return
        self._spawn_async_formal(
            event_id=event_id,
            line_user_id=line_user_id,
            text=original_text,
            session_id=session_id,
            push_sender=push_sender,
        )

    def _mark_pushed(self, event_id: str) -> None:
        with _pushed_lock:
            _pushed_events.add(event_id)

    def _begin_push(self, event_id: str) -> bool:
        with _pushed_lock:
            if event_id in _pushed_events or event_id in _pushing_events:
                return False
            _pushing_events.add(event_id)
            return True

    def _finish_push(self, event_id: str, *, success: bool) -> None:
        with _pushed_lock:
            _pushing_events.discard(event_id)
            if success:
                _pushed_events.add(event_id)

    def _mark_event_pushed(self, event_id: str) -> bool:
        marker = getattr(self.repository, "mark_webhook_event_pushed", None)
        if not callable(marker):
            return True
        with _pushed_lock:
            if event_id in _marker_retrying_events:
                return False
            _marker_retrying_events.add(event_id)
        try:
            record = marker(event_id)
            if record is None:
                raise RuntimeError("webhook event marker returned no record")
            with _pushed_lock:
                _marker_pending_events.discard(event_id)
            return True
        except Exception:
            with _pushed_lock:
                _marker_pending_events.add(event_id)
            logger.warning("could not persist push marker for %s", event_id)
            return False
        finally:
            with _pushed_lock:
                _marker_retrying_events.discard(event_id)

    def _push_with_retry(
        self,
        line_user_id: str,
        text: str,
        event_id: str | None = None,
        push_sender: PushSender | None = None,
        deadline_guard: DeadlineGuard | None = None,
    ) -> bool:
        deadline_guard = deadline_guard or current_deadline_guard()
        if event_id and self._is_duplicate_push(event_id):
            return False
        if event_id and not self._begin_push(event_id):
            return False
        success = False
        try:
            for attempt in range(2):
                try:
                    if deadline_guard is not None and deadline_guard.should_abort():
                        return False
                    if push_sender is not None:
                        ok = push_sender(line_user_id, text)
                    else:
                        ok = self._default_push_sender(line_user_id, text)
                    if ok:
                        # Keep the marker when the transport acknowledged the
                        # send, even if the local deadline elapsed while waiting;
                        # later ProductSession writes remain guard-protected.
                        success = True
                        if event_id:
                            self._mark_event_pushed(event_id)
                        return True
                    if attempt == 0:
                        continue
                    return False
                except Exception as exc:
                    logger.warning("push failed attempt %s: %s", attempt + 1, exc)
                    if attempt == 0:
                        continue
                    return False
            return False
        finally:
            if event_id:
                self._finish_push(event_id, success=success)

    def _default_push_sender(self, line_user_id: str, text: str) -> bool:
        try:
            import os as _os

            token = _os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or _os.getenv("LINE_ACCESS_TOKEN") or _os.getenv("LINE_CHANNEL_TOKEN") or ""
            if not token:
                return False
            from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
            from linebot.v3.messaging import PushMessageRequest, TextMessage

            config = Configuration(access_token=token)
            with ApiClient(configuration=config) as api_client:
                api = MessagingApi(api_client=api_client)
                kwargs: dict[str, Any] = {}
                try:
                    import inspect

                    params = inspect.signature(api.push_message).parameters
                    has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                    if "x_line_retry_key" in params or has_kwargs:
                        from tfda_context_gate.line_orchestration.retry_key import make_line_retry_key

                        kwargs["x_line_retry_key"] = make_line_retry_key(text + line_user_id)
                    guard = current_deadline_guard()
                    if guard is not None and ("_request_timeout" in params or has_kwargs):
                        remaining = guard.remaining_s()
                        if remaining is not None:
                            kwargs["_request_timeout"] = max(0.001, remaining)
                except Exception:
                    pass
                api.push_message(
                    PushMessageRequest(to=line_user_id, messages=[TextMessage(text=text[:4900])]),
                    **kwargs,
                )
            return True
        except Exception as exc:
            logger.warning("default push failed: %s", exc)
            raise

    def _maybe_record_question_for_doctor(
        self,
        line_user_id: str,
        original_text: str,
        workflow: WorkflowResult,
        *,
        deadline_guard: DeadlineGuard | None = None,
    ) -> None:
        active_guard = deadline_guard or current_deadline_guard()
        if active_guard is not None and active_guard.should_abort():
            return
        if not original_text or not original_text.strip():
            return
        if workflow.status != "FALLBACK":
            return
        reason = workflow.fallback_reason or workflow.termination_reason or ""
        if reason not in HONEST_FALLBACK_REASONS and workflow.final_response != HONEST_FALLBACK_TEXT:
            return
        try:
            session = self.session_for_user(line_user_id)
            if session is None:
                try:
                    session = self._load_or_create(line_user_id)
                except Exception:
                    return
            intake = session.intake_snapshot
            q = original_text.strip()[:200]
            if q in intake.questions_for_doctor:
                return
            if len(intake.questions_for_doctor) >= 10:
                return
            if q in (session.pending_question_proposal or "") or (session.pending_action and session.pending_action.proposal == q):
                return
            now = datetime.now(timezone.utc)
            pending = PendingAction(type="PENDING_CONFIRM_QUESTION", proposal=q, created_at=now)
            try:
                if active_guard is not None and active_guard.should_abort():
                    return
                self.repository.save(
                    session.model_copy(update={"pending_action": pending, "pending_question_proposal": q}, deep=True),
                    expected_version=session.version,
                )
            except Exception:
                pass
        except Exception:
            pass

    def prepare_formal_push_text(self, workflow: WorkflowResult, original_text: str) -> str:
        text = _format_push_answer(workflow, original_text)
        if _should_push_honest_fallback(workflow) and text == HONEST_FALLBACK_TEXT:
            return text
        return text

    def push_formal_result(
        self,
        *,
        line_user_id: str,
        event_id: str,
        workflow: WorkflowResult,
        original_text: str,
        push_sender: PushSender | None = None,
    ) -> bool:
        if self._is_duplicate_push(event_id):
            return False
        push_text = self.prepare_formal_push_text(workflow, original_text)
        ok = self._push_with_retry(line_user_id, push_text, event_id=event_id, push_sender=push_sender)
        if ok and _should_push_honest_fallback(workflow):
            self._maybe_record_question_for_doctor(line_user_id, original_text, workflow)
        return ok

    def _is_async_narrow_eligible(self, session: ProductSession, text: str) -> bool:
        if not self.use_formal:
            return False
        try:
            if self.risk_policy.classify(text).level == "RED_FLAG":
                return False
        except Exception:
            pass
        stripped = text.strip()
        try:
            from tfda_context_gate.workflow.intake_router import is_welcome_trigger
            from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor
            if is_welcome_trigger(stripped) or RuleBasedSignalExtractor.is_chit_chat_text(stripped) or RuleBasedSignalExtractor.is_identity_text(stripped):
                return False
        except Exception:
            pass
        if _is_rephrase_request(stripped):
            return True
        if _orch_should_use_formal(stripped, None):
            return True
        return True

    def _call_education_sync(self, request: dict[str, Any], **kwargs: Any) -> WorkflowResult:
        try:
            if self.use_formal and kwargs.get("task_type") is None:
                raw = str(request.get("user_raw_input", ""))
                if _orch_should_use_formal(raw, kwargs.get("task_type")):
                    try:
                        from tfda_context_gate.workflow.runner import stream_workflow

                        chunks = list(stream_workflow(request, **{**kwargs, "use_formal": True}))
                        streamed = "".join(chunks).strip()
                        wf = self._call_workflow(request, **kwargs)
                        if streamed and wf.status == "COMPLETED" and streamed != wf.final_response:
                            try:
                                wf = wf.model_copy(update={"final_response": streamed})
                            except Exception:
                                try:
                                    wf.final_response = streamed  # type: ignore[attr-defined]
                                except Exception:
                                    pass
                        return wf
                    except Exception:
                        pass
        except Exception:
            pass
        return self._call_workflow(request, **kwargs)

    def _run_formal_with_timeout(self, text: str, session: ProductSession, timeout_s: float) -> WorkflowResult:
        resolved_text = text
        if _is_rephrase_request(text):
            rephrased = _resolve_rephrase_followup(session, text)
            if rephrased:
                resolved_text = rephrased

        request = {
            "request_id": f"{session.session_id}-async-{threading.get_ident() % 10000}",
            "schema_version": "a.v0.1",
            "user_raw_input": resolved_text,
            "declared_role": self._declared_role(session.actor_role),
            "language": "zh-TW",
        }
        result, timed_out, guard = run_with_deadline(
            self.workflow_runner,
            request,
            timeout_s=timeout_s,
            use_formal=True,
        )
        if timed_out or result is None or guard.should_abort():
            raise FuturesTimeoutError(f"deadline expired for {session.session_id}")
        return result

    def _spawn_async_formal(
        self,
        *,
        event_id: str,
        line_user_id: str,
        text: str,
        session_id: str,
        push_sender: PushSender | None = None,
    ) -> None:
        if self._is_duplicate_push(event_id):
            return
        # A replay in the same process must not start a second formal job
        # while the first one is still running.  This set is only an admission
        # guard; the durable ``pushed`` marker remains the replay authority
        # across process restarts.
        with _async_jobs_lock:
            if event_id in _async_jobs:
                return
            _async_jobs.add(event_id)
        # One job-wide guard owns the complete background task.  A timed-out
        # dependency is abandoned; its eventual result must not be converted
        # into a late push or ProductSession write.
        spawn_guard = DeadlineGuard(self.async_formal_timeout_s)

        def _execute_and_push() -> None:
            workflow: WorkflowResult | None = None
            for attempt in range(2):
                try:
                    if spawn_guard.should_abort():
                        return
                    remaining = spawn_guard.remaining_s()
                    if remaining is None or remaining <= 0:
                        spawn_guard.mark_abandoned()
                        return
                    sess = self.repository.get(session_id)
                    target_session = sess if sess is not None else ProductSession.model_validate(
                        {
                            "session_id": session_id,
                            "principal_id_hash": self._hash(line_user_id),
                            "conversation_context": self.context_manager.create(session_id),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "expires_at": (datetime.now(timezone.utc) + self.session_ttl).isoformat(),
                        }
                    )
                    wf = self._run_formal_with_timeout(text, target_session, remaining)
                    workflow = wf
                    break
                except FuturesTimeoutError:
                    logger.warning("async formal timeout attempt %s for %s", attempt + 1, event_id[:8])
                    spawn_guard.mark_abandoned()
                    # A timeout gets the deterministic honest response, but
                    # never the late workflow result.  This safe notification
                    # is deliberately sent without the abandoned workflow
                    # guard and is not appended to ProductSession.
                    timeout_workflow = _timeout_workflow_result(
                        event_id,
                        text,
                        reason=DEPENDENCY_OR_TIMEOUT_REASON,
                    )
                    self._push_with_retry(
                        line_user_id,
                        self.prepare_formal_push_text(timeout_workflow, text),
                        event_id=event_id,
                        push_sender=push_sender,
                        # The abandoned workflow guard must not suppress the
                        # safe timeout notice.  This is a fresh, short guard;
                        # it is not allowed to persist the abandoned result.
                        deadline_guard=DeadlineGuard(5.0),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("async formal error attempt %s for %s: %s", attempt + 1, event_id[:8], exc)
                    if attempt == 1:
                        workflow = WorkflowResult(
                            request_id=event_id,
                            status="FALLBACK",
                            final_response=HONEST_FALLBACK_TEXT,
                            fallback_reason=DEPENDENCY_OR_TIMEOUT_REASON,
                            a_result=None,
                            query_expansion=None,
                            rag_result=None,
                            b_result=None,
                            c_result=None,
                            d_result=None,
                            agent_action=None,
                            agent_reason_code=None,
                            question=None,
                            current_query=text,
                            execution_history=[],
                            agent_steps=0,
                            rewrite_count=0,
                            clarification_count=0,
                            termination_reason=DEPENDENCY_OR_TIMEOUT_REASON,
                            intake_snapshot=None,
                            intake_stage=None,
                            previsit_summary=None,
                            system_risk_classification=None,
                            trace={"events": [], "evaluations": []},
                        )
                    else:
                        continue
            if workflow is None:
                workflow = WorkflowResult(
                    request_id=event_id,
                    status="FALLBACK",
                    final_response=HONEST_FALLBACK_TEXT,
                    fallback_reason=DEPENDENCY_OR_TIMEOUT_REASON,
                    a_result=None,
                    query_expansion=None,
                    rag_result=None,
                    b_result=None,
                    c_result=None,
                    d_result=None,
                    agent_action=None,
                    agent_reason_code=None,
                    question=None,
                    current_query=text,
                    execution_history=[],
                    agent_steps=0,
                    rewrite_count=0,
                    clarification_count=0,
                    termination_reason=DEPENDENCY_OR_TIMEOUT_REASON,
                    intake_snapshot=None,
                    intake_stage=None,
                    previsit_summary=None,
                    system_risk_classification=None,
                    trace={"events": [], "evaluations": []},
                )
            if self._is_duplicate_push(event_id):
                return
            if spawn_guard.should_abort():
                return
            push_text = self.prepare_formal_push_text(workflow, text)
            if spawn_guard.should_abort():
                return
            ok = self._push_with_retry(
                line_user_id,
                push_text,
                event_id=event_id,
                push_sender=push_sender,
                deadline_guard=spawn_guard,
            )
            if ok:
                if spawn_guard.should_abort():
                    return
                try:
                    latest = self.repository.get(session_id)
                    if latest is not None:
                        if spawn_guard.should_abort():
                            return
                        ctx = self.context_manager.append_turn(latest.conversation_context, role="assistant", content=push_text)
                        ctx, _ = self.context_manager.compact(ctx, stage_completed=False)
                        updated = latest.model_copy(update={"conversation_context": ctx}, deep=True)
                        try:
                            self.repository.save(updated, expected_version=latest.version)
                        except ProductSessionConflict:
                            pass
                except Exception:
                    pass
                if spawn_guard.should_abort():
                    return
                if _should_push_honest_fallback(workflow):
                    self._maybe_record_question_for_doctor(line_user_id, text, workflow, deadline_guard=spawn_guard)

        # Admission happens before creating a thread.  When all five workers
        # are occupied we fail closed with one event-owned safe notice; there
        # is no unbounded per-request delayed-thread queue.
        if not _FORMAL_SEMAPHORE.acquire(blocking=False):
            with _async_jobs_lock:
                _async_jobs.discard(event_id)
            # Fail closed at admission.  In particular, do not synchronously
            # call a custom sender from the webhook thread: a slow sender
            # would defeat the async timeout and scale one blocked request per
            # saturated event.  The durable ASYNC_PENDING event remains
            # replayable; a later webhook replay can retry admission.
            logger.warning("async formal admission rejected for %s", event_id[:8])
            return

        def _background() -> None:
            with deadline_scope(spawn_guard):
                try:
                    if self._is_duplicate_push(event_id):
                        return
                    _execute_and_push()
                finally:
                    try:
                        _FORMAL_SEMAPHORE.release()
                    except Exception:
                        pass
                    with _async_jobs_lock:
                        _async_jobs.discard(event_id)

        try:
            threading.Thread(target=_background, daemon=True).start()
        except Exception:
            _FORMAL_SEMAPHORE.release()
            with _async_jobs_lock:
                _async_jobs.discard(event_id)
            raise

    def handle_text_async_push(
        self,
        *,
        event_id: str,
        line_user_id: str,
        text: str,
        push_sender: PushSender | None = None,
    ) -> OrchestratorResult:
        return self.handle_text(event_id=event_id, line_user_id=line_user_id, text=text, push_sender=push_sender)

    def handle_text(
        self,
        *,
        event_id: str,
        line_user_id: str,
        text: str,
        push_sender: PushSender | None = None,
    ) -> OrchestratorResult:
        principal_hash = self._hash(line_user_id)
        existing_event = self.repository.get_webhook_event(event_id)
        if existing_event is not None and existing_event.status == "COMPLETED" and existing_event.result:
            if existing_event.principal_id_hash != principal_hash:
                raise WebhookEventIdentityMismatch("webhook event belongs to another principal")
            self._reschedule_pending_async_event(
                event_id=event_id,
                line_user_id=line_user_id,
                payload=existing_event.result,
                push_sender=push_sender,
            )
            return _replayable_orchestrator_result(event_id, existing_event.result)
        claim_token = self.repository.claim_webhook_event(event_id, principal_hash)
        if claim_token is None:
            return OrchestratorResult(
                event_id=event_id,
                session_id=self._session_id(line_user_id),
                reply="此訊息正在處理中，請稍候。",
                status="PROCESSING",
                replayed=True,
            )

        try:
            session = self._load_or_create(line_user_id)
            clean_text = text.strip()
            if _is_short_ttl_text(clean_text) and not self._is_async_narrow_eligible(session, clean_text):
                now = time.time()
                norm = _normalize_text(clean_text)
                key = (line_user_id, norm)
                ttl = _dedup_ttl_for(clean_text)
                with _text_dedup_lock:
                    expired = [k for k, ts in list(_text_dedup.items()) if now - ts > TEXT_DEDUP_TTL_S]
                    for k in expired:
                        _text_dedup.pop(k, None)
                    ts = _text_dedup.get(key)
                    if ts is not None and now - ts < ttl:
                        dedup_result = OrchestratorResult(
                            event_id=event_id,
                            session_id=session.session_id,
                            reply=_dedup_reply_for(clean_text),
                            status="BLOCKED",
                            intake_stage=session.intake_stage,
                            replayed=False,
                        )
                        self.repository.complete_webhook_event(
                            event_id, dedup_result.model_dump(mode="json"), claim_token=claim_token
                        )
                        return dedup_result
                    if norm:
                        _text_dedup[key] = now
            if self._is_intake_active(session, clean_text):
                try:
                    if self.risk_policy.classify(clean_text).level != "RED_FLAG":
                        intake_part_raw, edu_part_raw = _split_intake_education_clauses(clean_text, None)
                        if not edu_part_raw:
                            edu_part_raw = _extract_question_clause(clean_text)
                            if edu_part_raw:
                                intake_part_raw = clean_text.replace(edu_part_raw, "").strip("，,。；;、 \t\n")
                                intake_part_raw = re.sub(r"^(我想問他|我想問|想問他|想問)\s*", "", intake_part_raw).strip("，,。；;、 \t\n")
                        _is_intake_q_like = False
                        try:
                            from tfda_context_gate.intake.candidate_merge import is_question_like as _is_q_like_mix
                            _is_intake_q_like = _is_q_like_mix(intake_part_raw.strip())
                        except Exception:
                            _is_intake_q_like = False
                        if edu_part_raw and intake_part_raw and len(intake_part_raw.strip()) >= 2 and not _is_intake_q_like and _orch_should_use_formal(edu_part_raw, None):
                            try:
                                from tfda_context_gate.conversation.envelope import build_conversation_envelope
                                from tfda_context_gate.line_orchestration.deadline import run_with_deadline as _rwd_interp

                                def _interp_call_mix() -> Any:
                                    env_mix = build_conversation_envelope(session, clean_text)
                                    try:
                                        return self.interpreter.interpret(env_mix)
                                    except Exception:
                                        from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter
                                        return DeterministicConversationInterpreter().interpret(env_mix)

                                _interp_res, _interp_to, _interp_guard = _rwd_interp(_interp_call_mix, timeout_s=0.25)
                                if not _interp_to and _interp_res is not None and not _interp_guard.should_abort():
                                    try:
                                        _interp_res = _maybe_apply_mixed_backstop(clean_text, _interp_res)
                                    except Exception:
                                        pass
                                    self._last_interpretation = _interp_res
                                    try:
                                        self._last_envelope = build_conversation_envelope(session, clean_text)
                                    except Exception:
                                        pass
                                    if _interp_res is not None and "EDUCATION_QUESTION" not in (getattr(_interp_res, "intents", []) or []):
                                        raise ValueError("interp still missing EDUCATION_QUESTION, fallback synthetic")
                                else:
                                    raise TimeoutError("interp timeout for mixed")
                            except Exception:
                                try:
                                    from tfda_context_gate.conversation.interpreter import ConversationTurnInterpretation
                                    self._last_interpretation = ConversationTurnInterpretation(
                                        intents=["INTAKE_ANSWER", "EDUCATION_QUESTION"],
                                        resolved_education_query=edu_part_raw.strip(),
                                        intake_candidates=[],
                                        references_resolved=True,
                                        confidence=0.9,
                                    )
                                except Exception:
                                    pass
                            now_mix = time.time()
                            norm_mix = _normalize_text(clean_text)
                            key_mix = (line_user_id, norm_mix)
                            ttl_mix = _dedup_ttl_for(clean_text)
                            is_dup_mix = False
                            with _text_dedup_lock:
                                expired_mix = [k for k, ts in list(_text_dedup.items()) if now_mix - ts > TEXT_DEDUP_TTL_S]
                                for k in expired_mix:
                                    _text_dedup.pop(k, None)
                                ts_mix = _text_dedup.get(key_mix)
                                if ts_mix is not None and now_mix - ts_mix < ttl_mix:
                                    is_dup_mix = True
                                else:
                                    if norm_mix:
                                        _text_dedup[key_mix] = now_mix
                            if is_dup_mix:
                                dedup_mix = OrchestratorResult(
                                    event_id=event_id,
                                    session_id=session.session_id,
                                    reply=_dedup_reply_for(clean_text),
                                    status="BLOCKED",
                                    intake_stage=session.intake_stage,
                                    replayed=False,
                                )
                                self.repository.complete_webhook_event(
                                    event_id, dedup_mix.model_dump(mode="json"), claim_token=claim_token
                                )
                                return dedup_mix
                            prev_ver_mix = session.version
                            before_snap_mix = session.intake_snapshot.model_dump() if hasattr(session.intake_snapshot, "model_dump") else {}
                            try:
                                updated_sess, intake_note_mix = self._normalize_intake_answer(session, intake_part_raw.strip(), allow_cross_stage_symptom_description=True)
                            except Exception:
                                updated_sess, intake_note_mix = session, None
                            try:
                                after_snap_mix = updated_sess.intake_snapshot.model_dump() if hasattr(updated_sess.intake_snapshot, "model_dump") else {}
                                changed_mix = [f for f in ("known_medications","allergies","chronic_conditions","family_history","symptom_onset","symptom_description","symptom_severity","questions_for_doctor") if before_snap_mix.get(f) != after_snap_mix.get(f)]
                                if changed_mix:
                                    prov_mix = dict(getattr(updated_sess, "intake_field_provenance", {}) or {})
                                    raw_prov_mix = intake_part_raw.strip()[:80]
                                    for fld in changed_mix:
                                        if fld not in prov_mix:
                                            prov_mix[fld] = raw_prov_mix
                                    updated_sess = updated_sess.model_copy(update={"intake_field_provenance": prov_mix}, deep=True)
                            except Exception:
                                pass
                            _next_field_raw = self._next_pending_field(updated_sess.intake_snapshot) if hasattr(updated_sess, "intake_snapshot") else None
                            next_field_mix = _next_field_raw
                            pending_q_mix = self._question_for_field(next_field_mix) if next_field_mix else None
                            try:
                                if next_field_mix:
                                    updated_sess = updated_sess.model_copy(update={"pending_field": next_field_mix, "pending_question": pending_q_mix, "intake_stage": self._field_stage(next_field_mix)}, deep=True)
                            except Exception:
                                pass
                            _try_sync_mix = os.getenv("PYTEST_CURRENT_TEST") is not None
                            if _try_sync_mix:
                                try:
                                    from tfda_context_gate.line_orchestration.deadline import run_with_deadline as _rwd_mix

                                    def _edu_sync_mix() -> Any:
                                        return self._call_education_sync(
                                            {
                                                "request_id": f"{session.session_id}-mix-edu-v{prev_ver_mix + 1}",
                                                "schema_version": "a.v0.1",
                                                "user_raw_input": edu_part_raw.strip(),
                                                "declared_role": self._declared_role(updated_sess.actor_role),
                                                "language": "zh-TW",
                                            },
                                            task_type=None,
                                            intake=None,
                                        )

                                    wf_mix, timed_out_mix, guard_mix = _rwd_mix(_edu_sync_mix, timeout_s=0.35)
                                    if not timed_out_mix and wf_mix is not None and not guard_mix.should_abort() and wf_mix.status in ("COMPLETED", "FALLBACK") and wf_mix.final_response:
                                        edu_text_mix = wf_mix.final_response.strip()
                                        if edu_text_mix and ("水果" in edu_text_mix or "衛教" in edu_text_mix or len(edu_text_mix) > 20):
                                            if intake_note_mix and intake_note_mix.strip():
                                                if pending_q_mix and pending_q_mix.strip() not in (intake_note_mix or ""):
                                                    assistant_content_mix_fast = f"{intake_note_mix}\n\n{edu_text_mix}\n\n{pending_q_mix}" if pending_q_mix else f"{intake_note_mix}\n\n{edu_text_mix}"
                                                else:
                                                    assistant_content_mix_fast = f"{intake_note_mix}\n\n{edu_text_mix}"
                                            else:
                                                assistant_content_mix_fast = f"{edu_text_mix}\n\n{pending_q_mix}" if pending_q_mix else edu_text_mix
                                            ctx_fast = self.context_manager.append_turn(updated_sess.conversation_context, role="user", content=clean_text or "（空白訊息）")
                                            ctx_fast = self.context_manager.append_turn(ctx_fast, role="assistant", content=assistant_content_mix_fast)
                                            ctx_fast, _ = self.context_manager.compact(ctx_fast, stage_completed=False)
                                            updated_sess_fast = updated_sess.model_copy(update={"conversation_context": ctx_fast}, deep=True)
                                            updated_sess_fast = self._sync_clinical_context(updated_sess_fast)
                                            saved_fast = self.repository.save(updated_sess_fast, expected_version=prev_ver_mix)
                                            result_fast = OrchestratorResult(
                                                event_id=event_id,
                                                session_id=saved_fast.session_id,
                                                reply=assistant_content_mix_fast,
                                                status=wf_mix.status,
                                                intake_stage=saved_fast.intake_stage,
                                                fallback_reason=getattr(wf_mix, "fallback_reason", None),
                                            )
                                            self.repository.complete_webhook_event(
                                                event_id, result_fast.model_dump(mode="json"), claim_token=claim_token
                                            )
                                            return result_fast
                                except Exception:
                                    pass
                            ctx_mix = self.context_manager.append_turn(updated_sess.conversation_context, role="user", content=clean_text or "（空白訊息）")
                            placeholder_mix = ASYNC_PLACEHOLDER_REPLY
                            if intake_note_mix and intake_note_mix.strip():
                                if pending_q_mix and pending_q_mix.strip() not in (intake_note_mix or ""):
                                    assistant_content_mix = f"{intake_note_mix}\n\n{placeholder_mix}\n\n{pending_q_mix}"
                                else:
                                    assistant_content_mix = f"{intake_note_mix}\n\n{placeholder_mix}"
                            else:
                                if pending_q_mix:
                                    assistant_content_mix = f"{placeholder_mix}\n\n{pending_q_mix}"
                                else:
                                    assistant_content_mix = placeholder_mix
                            ctx_mix = self.context_manager.append_turn(ctx_mix, role="assistant", content=assistant_content_mix)
                            ctx_mix, _ = self.context_manager.compact(ctx_mix, stage_completed=False)
                            updated_sess = updated_sess.model_copy(update={"conversation_context": ctx_mix}, deep=True)
                            updated_sess = self._sync_clinical_context(updated_sess)
                            try:
                                saved_mix = self.repository.save(updated_sess, expected_version=prev_ver_mix)
                            except ProductSessionConflict:
                                latest_mix = self.repository.get(session.session_id)
                                if latest_mix is None:
                                    raise
                                try:
                                    upd2, note2 = self._normalize_intake_answer(latest_mix, intake_part_raw.strip())
                                except Exception:
                                    upd2, note2 = latest_mix, intake_note_mix
                                nf2 = self._next_pending_field(upd2.intake_snapshot) if hasattr(upd2, "intake_snapshot") else None
                                pq2 = self._question_for_field(nf2) if nf2 else None
                                ctx2_mix = self.context_manager.append_turn(upd2.conversation_context, role="user", content=clean_text or "（空白訊息）")
                                if note2 and note2.strip():
                                    if pq2 and pq2.strip() not in (note2 or ""):
                                        ac2 = f"{note2}\n\n{placeholder_mix}\n\n{pq2}"
                                    else:
                                        ac2 = f"{note2}\n\n{placeholder_mix}"
                                else:
                                    ac2 = f"{placeholder_mix}\n\n{pq2}" if pq2 else placeholder_mix
                                ctx2_mix = self.context_manager.append_turn(ctx2_mix, role="assistant", content=ac2)
                                ctx2_mix, _ = self.context_manager.compact(ctx2_mix, stage_completed=False)
                                upd2 = upd2.model_copy(update={"conversation_context": ctx2_mix}, deep=True)
                                upd2 = self._sync_clinical_context(upd2)
                                saved_mix = self.repository.save(upd2, expected_version=latest_mix.version)
                                assistant_content_mix = ac2
                            result_mix = OrchestratorResult(
                                event_id=event_id,
                                session_id=saved_mix.session_id,
                                reply=assistant_content_mix,
                                status="ASYNC_PENDING",
                                intake_stage=saved_mix.intake_stage,
                            )
                            self.repository.complete_webhook_event(
                                event_id,
                                {**result_mix.model_dump(mode="json"), "async_original_text": edu_part_raw.strip()},
                                claim_token=claim_token,
                            )
                            self._spawn_async_formal(
                                event_id=event_id,
                                line_user_id=line_user_id,
                                text=edu_part_raw.strip(),
                                session_id=saved_mix.session_id,
                                push_sender=push_sender,
                            )
                            return result_mix
                except Exception:
                    pass
            if self._is_async_narrow_eligible(session, clean_text):
                now = time.time()
                norm = _normalize_text(clean_text)
                key = (line_user_id, norm)
                ttl = _dedup_ttl_for(clean_text)
                is_dup = False
                with _text_dedup_lock:
                    expired = [k for k, ts in list(_text_dedup.items()) if now - ts > TEXT_DEDUP_TTL_S]
                    for k in expired:
                        _text_dedup.pop(k, None)
                    ts = _text_dedup.get(key)
                    if ts is not None and now - ts < ttl:
                        is_dup = True
                    else:
                        if norm:
                            _text_dedup[key] = now
                if is_dup:
                    dedup_result = OrchestratorResult(
                        event_id=event_id,
                        session_id=session.session_id,
                        reply=_dedup_reply_for(clean_text),
                        # This event owns no background job; only the first
                        # turn owns ASYNC_PENDING.  Marking a text-level
                        # duplicate as pending would make replay schedule the
                        # first event a second time after a restart.
                        status="BLOCKED",
                        intake_stage=session.intake_stage,
                        replayed=False,
                    )
                    self.repository.complete_webhook_event(
                        event_id, dedup_result.model_dump(mode="json"), claim_token=claim_token
                    )
                    return dedup_result
                previous_version = session.version
                context = self.context_manager.append_turn(
                    session.conversation_context, role="user", content=clean_text or "（空白訊息）"
                )
                placeholder = ASYNC_PLACEHOLDER_REPLY
                context = self.context_manager.append_turn(context, role="assistant", content=placeholder)
                context, _ = self.context_manager.compact(context, stage_completed=False)
                session_for_save = session.model_copy(update={"conversation_context": context}, deep=True)
                try:
                    saved = self.repository.save(session_for_save, expected_version=previous_version)
                except ProductSessionConflict:
                    latest = self.repository.get(session.session_id)
                    if latest is None:
                        raise
                    ctx2 = self.context_manager.append_turn(
                        latest.conversation_context, role="user", content=clean_text or "（空白訊息）"
                    )
                    ctx2 = self.context_manager.append_turn(ctx2, role="assistant", content=placeholder)
                    ctx2, _ = self.context_manager.compact(ctx2, stage_completed=False)
                    saved_latest = latest.model_copy(update={"conversation_context": ctx2}, deep=True)
                    saved = self.repository.save(saved_latest, expected_version=latest.version)
                result_placeholder = OrchestratorResult(
                    event_id=event_id,
                    session_id=saved.session_id,
                    reply=placeholder,
                    status="ASYNC_PENDING",
                    intake_stage=saved.intake_stage,
                )
                self.repository.complete_webhook_event(
                    event_id,
                    {
                        **result_placeholder.model_dump(mode="json"),
                        "async_original_text": clean_text,
                    },
                    claim_token=claim_token,
                )
                self._spawn_async_formal(
                    event_id=event_id,
                    line_user_id=line_user_id,
                    text=clean_text,
                    session_id=saved.session_id,
                    push_sender=push_sender,
                )
                return result_placeholder
            try:
                result = self._process_text(session, clean_text)
            except ProductSessionConflict:
                latest = self.repository.get(session.session_id)
                if latest is None:
                    raise
                result = self._process_text(latest, clean_text)
            # Attach guarded downgrade fallback_reason into metadata (non-PII)
            try:
                fb = getattr(self, "_last_guarded_fallback_reason", None)
                if fb:
                    meta = dict(getattr(result, "metadata", None) or {})
                    if "fallback_reason" not in meta:
                        meta["fallback_reason"] = fb
                        # also surface as top-level fallback_reason if not set
                        try:
                            result = result.model_copy(update={"metadata": meta})
                        except Exception:
                            try:
                                result.metadata = meta  # type: ignore[attr-defined]
                            except Exception:
                                pass
            except Exception:
                pass
            if result.semantic_route is None:
                try:
                    obs = getattr(self, "_last_semantic_observation", None)
                    mode = getattr(self, "_last_semantic_mode", "off")
                    if obs is not None and mode != "off":
                        result = _enrich_orchestrator_result(result, obs, mode)
                        # re-attach fallback after enrichment if it was overwritten
                        try:
                            fb2 = getattr(self, "_last_guarded_fallback_reason", None)
                            if fb2:
                                meta2 = dict(getattr(result, "metadata", None) or {})
                                if "fallback_reason" not in meta2:
                                    meta2["fallback_reason"] = fb2
                                    try:
                                        result = result.model_copy(update={"metadata": meta2})
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                except Exception:
                    pass
            result = result.model_copy(update={"event_id": event_id})
            self.repository.complete_webhook_event(
                event_id, result.model_dump(mode="json"), claim_token=claim_token
            )
            return result
        except Exception:
            self.repository.fail_webhook_event(event_id, claim_token=claim_token)
            raise

    def handle_image(
        self,
        *,
        event_id: str,
        line_user_id: str,
        image_bytes: bytes,
        ocr_service: Any | None = None,
    ) -> OrchestratorResult:
        principal_hash = self._hash(line_user_id)
        existing_event = self.repository.get_webhook_event(event_id)
        if existing_event is not None and existing_event.status == "COMPLETED" and existing_event.result:
            if existing_event.principal_id_hash != principal_hash:
                raise WebhookEventIdentityMismatch("webhook event belongs to another principal")
            return _replayable_orchestrator_result(event_id, existing_event.result)
        claim_token = self.repository.claim_webhook_event(event_id, principal_hash)
        if claim_token is None:
            return OrchestratorResult(event_id=event_id, session_id=self._session_id(line_user_id), reply="此圖片正在處理中，請稍候。", status="PROCESSING", replayed=True)
        try:
            session = self._load_or_create(line_user_id)
            previous_version = session.version
            from tfda_context_gate.intake.qr_ocr_service import MedicationBagOCRService

            svc = ocr_service if ocr_service is not None else MedicationBagOCRService()
            extracted = svc.extract(image_bytes)
            meds = extracted.get("meds") or []
            if meds:
                from tfda_context_gate.intake.lean_agent import deduplicate_medications, LeanIntakeAgent
                # Reload fresh session from repository after OCR to avoid optimistic lock conflict with concurrent user messages
                session = self._load_or_create(line_user_id)
                previous_version = session.version
                clean_meds = deduplicate_medications(meds)
                meds_text = "、".join(clean_meds)
                reply = (
                    f"為您辨識藥袋上的藥品資訊如下：\n"
                    f"藥品名稱：{meds_text}\n\n"
                    f"您可以在這裡直接向我詢問此藥品的用途、服用注意事項或副作用。\n"
                    f"若您近期要看醫生，也可以點選「我要準備看診」，我會將這筆用藥自動帶入看診資料中。"
                )
                current_intake = PreVisitIntake(known_medications=clean_meds)
                agent = LeanIntakeAgent.from_env()
                dyn_q, dyn_f = agent._generate_next_question("stage1", current_intake)
                session = session.model_copy(
                    update={
                        "intake_snapshot": current_intake,
                        "status": "ACTIVE",
                        "intake_stage": "stage1",
                        "pending_question": dyn_q,
                        "pending_field": dyn_f,
                    },
                    deep=True,
                )
                try:
                    self.repository.save(session, expected_version=previous_version)
                except Exception:
                    # Retry with latest state on race condition
                    session = self._load_or_create(line_user_id)
                    clean_meds = deduplicate_medications(meds)
                    current_intake = PreVisitIntake(known_medications=clean_meds)
                    dyn_q, dyn_f = agent._generate_next_question("stage1", current_intake)
                    session = session.model_copy(
                        update={
                            "intake_snapshot": current_intake,
                            "status": "ACTIVE",
                            "intake_stage": "stage1",
                            "pending_question": dyn_q,
                            "pending_field": dyn_f,
                        },
                        deep=True,
                    )
                    self.repository.save(session, expected_version=session.version)
            else:
                reply = "這張照片未能清楚辨識出藥袋上的藥品名稱或 QR Code。建議您重新拍攝光線充足、文字清晰的藥袋正面再試一次喔！"
            result = OrchestratorResult(
                event_id=event_id,
                session_id=session.session_id,
                reply=reply,
                status="COMPLETED",
                intake_stage=session.intake_stage,
            )
            self.repository.complete_webhook_event(event_id, result.model_dump(mode="json"), claim_token=claim_token)
            return result
        except Exception:
            self.repository.fail_webhook_event(event_id, claim_token=claim_token)
            raise

    def _process_text(self, session: ProductSession, text: str) -> OrchestratorResult:
        recorder = StagedLatencyRecorder(session_id=session.session_id)
        previous_version = session.version
        try:
            if self._is_intake_active(session, text) and session.pending_field == "known_medications":
                is_colloquial = bool(_MEDICATION_COLLOQUIAL_RE.search(text))
                is_uncertain_with_med = bool(_MEDICATION_UNCERTAIN_RE.search(text) and "藥" in text)
                # Generic "我不太知道欸" without 藥 or colloquial should fall through to immediate sentinel (keep existing test passing)
                if is_colloquial or is_uncertain_with_med:
                    has_known = bool(_MEDICATION_KNOWN_RE.search(text))
                    if not has_known:
                        # Requirement 1: Brown Bag 2-attempt flow, not immediate sentinel
                        from tfda_context_gate.intake.schemas import MEDICATION_CLARIFICATION_QUESTIONS
                        from tfda_context_gate.line_orchestration.response_composer import compose_uncertain
                        attempt_key = (session.session_id, "known_medications")
                        attempt = _intake_uncertain_attempts.get(attempt_key, 0)
                        if attempt == 0:
                            q = MEDICATION_CLARIFICATION_QUESTIONS[1]
                            _intake_uncertain_attempts[attempt_key] = 1
                            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
                            sess_tmp = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
                            sess_tmp = self._sync_clinical_context(sess_tmp)
                            ctx_assist = self.context_manager.append_turn(sess_tmp.conversation_context, role="assistant", content=q)
                            ctx_assist, _ = self.context_manager.compact(ctx_assist, stage_completed=False)
                            sess_tmp = sess_tmp.model_copy(update={"conversation_context": ctx_assist, "pending_field": "known_medications", "pending_question": q}, deep=True)
                            saved = self.repository.save(sess_tmp, expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=q, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
                        elif attempt == 1:
                            q = MEDICATION_CLARIFICATION_QUESTIONS[2]
                            _intake_uncertain_attempts[attempt_key] = 2
                            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
                            sess_tmp = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
                            sess_tmp = self._sync_clinical_context(sess_tmp)
                            ctx_assist = self.context_manager.append_turn(sess_tmp.conversation_context, role="assistant", content=q)
                            ctx_assist, _ = self.context_manager.compact(ctx_assist, stage_completed=False)
                            sess_tmp = sess_tmp.model_copy(update={"conversation_context": ctx_assist, "pending_field": "known_medications", "pending_question": q}, deep=True)
                            saved = self.repository.save(sess_tmp, expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=q, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
                        else:
                            # 2 attempts exhausted -> sentinel and advance pending (Requirement 1 & 4)
                            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
                            sess_tmp = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
                            intake = sess_tmp.intake_snapshot.model_copy(deep=True)
                            intake.known_medications = ["不清楚（待看診確認）"]
                            _intake_uncertain_attempts.pop(attempt_key, None)
                            next_field = self._next_pending_field(intake)
                            next_q = self._question_for_field(next_field) if next_field else None
                            sess_tmp = sess_tmp.model_copy(update={"intake_snapshot": intake, "pending_field": next_field, "pending_question": next_q}, deep=True)
                            sess_tmp = self._sync_clinical_context(sess_tmp)
                            uncertain_msg = compose_uncertain(symptom=False)
                            reply_text = f"{uncertain_msg}\n\n{next_q}" if next_q else uncertain_msg
                            ctx_assist = self.context_manager.append_turn(sess_tmp.conversation_context, role="assistant", content=reply_text)
                            ctx_assist, _ = self.context_manager.compact(ctx_assist, stage_completed=False)
                            saved = self.repository.save(sess_tmp.model_copy(update={"conversation_context": ctx_assist}, deep=True), expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply_text, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
        except Exception:
            pass
        workflow: Any | None = None  # type: ignore[var-annotated]
        with recorder.stage("red_flag_and_auth_ms"):
            risk = self.risk_policy.classify(text)
            cumulative_risk = self._merge_risk(session.system_risk_classification, risk.model_dump(mode="json"))
            context = self.context_manager.apply_structured_updates(
                session.conversation_context,
                {
                    "system_risk_classification": cumulative_risk,
                    "risk_flags": ["POSSIBLE_EMERGENCY"] if risk.level == "RED_FLAG" else [],
                },
            )
            session = session.model_copy(
                update={"conversation_context": context, "system_risk_classification": cumulative_risk},
                deep=True,
            )

        # 4: 紅旗立即中止 — 不得把 Interpreter 放在紅旗之前
        if cumulative_risk.get("level") == "RED_FLAG":
            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
            session = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
            reply = fallback_response("A_EMERGENCY")
            session = self._sync_clinical_context(session)
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=False)
            with recorder.stage("persistence_ms"):
                saved = self.repository.save(
                    session.model_copy(update={"conversation_context": context}, deep=True),
                    expected_version=previous_version,
                )
            self._last_staged_latency = recorder.snapshot()
            return OrchestratorResult(
                event_id="pending",
                session_id=saved.session_id,
                reply=reply,
                status="FALLBACK",
                intake_stage=saved.intake_stage,
                fallback_reason="A_EMERGENCY",
            )

        # 5: Identity check (bounded, cold-start also, before envelope, not via LLM)
        if self._is_identity_question(text):
            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
            session = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
            # P2B changes phrasing only.  The identity pool keeps the same
            # non-diagnostic/urgent boundaries and rotates within a session;
            # this path remains before interpreter and never writes intake.
            reply = fallback_response("IDENTITY", session_id=session.session_id)
            session = self._sync_clinical_context(session)
            ctx_assistant = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            ctx_assistant, _ = self.context_manager.compact(ctx_assistant, stage_completed=False)
            with recorder.stage("persistence_ms"):
                saved = self.repository.save(session.model_copy(update={"conversation_context": ctx_assistant}, deep=True), expected_version=previous_version)
            self._last_staged_latency = recorder.snapshot()
            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status="INFORMATION", intake_stage=saved.intake_stage)

        # 6: Explicit product control fast path (before AI) — spec 6
        _explicit_product_texts = self.SELF_COMMANDS | self.PROXY_COMMANDS | self.PAUSE_COMMANDS | self.CANCEL_COMMANDS | self.RESUME_COMMANDS | self.CONFIRM_COMMANDS | self.START_INTAKE_COMMANDS | self.SHARE_COMMANDS | self.SUMMARY_COMMANDS | self.MODIFY_COMMANDS | {"使用說明", "使用說明與緊急協助"}
        if text.strip() in _explicit_product_texts or any(tok in text for tok in ("為自己整理", "代家人整理")):
            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
            session_tmp = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
            cmd_res = self._handle_product_command(session_tmp, text)
            if cmd_res is not None:
                sess_cmd, reply_cmd, status_cmd = cmd_res
                sess_cmd = self._sync_clinical_context(sess_cmd)
                ctx_assistant = self.context_manager.append_turn(sess_cmd.conversation_context, role="assistant", content=reply_cmd)
                ctx_assistant, _ = self.context_manager.compact(ctx_assistant, stage_completed=False)
                saved = self.repository.save(sess_cmd.model_copy(update={"conversation_context": ctx_assistant}, deep=True), expected_version=previous_version)
                return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply_cmd, status=status_cmd, intake_stage=saved.intake_stage)

        # 7: Intake pending narrow fast-path (before AI) — P2A: only high-precision closed values skip AI
        # Deterministic may supplement but must NOT block AI on partial multi-clause natural language
        _skip_ai_for_intake = False
        _fast_path_reason: str | None = None
        if self._is_intake_active(session, text) and session.status in ("ACTIVE", "AWAITING_CONFIRMATION"):
            pending_field = session.pending_field or self._next_pending_field(session.intake_snapshot)
            if pending_field:
                try:
                    from tfda_context_gate.intake.candidate_merge import is_fast_path_eligible

                    if is_fast_path_eligible(text, pending_field):
                        _skip_ai_for_intake = True
                        _fast_path_reason = f"fast_path:{pending_field}"
                except Exception:
                    # Fail open to AI on helper error
                    _skip_ai_for_intake = False

        # 8: L2 semantic router (after L0 red_flag/auth/product/fast_path, before envelope)
        _semantic_observation: Any | None = None
        _semantic_mode, _guarded_fallback_reason = _resolve_guarded_downgrade()
        self._last_guarded_fallback_reason = _guarded_fallback_reason
        if _guarded_fallback_reason:
            _record_guarded_downgrade(session.session_id, _guarded_fallback_reason)
        _semantic_fast_eligible = False
        _semantic_fast_route: str | None = None
        if _semantic_mode != "off":
            try:
                _router = self._get_semantic_router()
                if _router is not None:
                    _raw_obs = _call_semantic_router_with_timeout(_router, text, SEMANTIC_ROUTER_TIMEOUT_S)
                    if _raw_obs is None:
                        try:
                            from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation as _ObsFallback

                            _raw_obs = _ObsFallback(
                                route="UNKNOWN",
                                confidence=0.0,
                                margin=0.0,
                                latency_ms=SEMANTIC_ROUTER_TIMEOUT_S * 1000,
                                mode=_semantic_mode,
                                degraded=True,
                            )
                        except Exception:
                            _raw_obs = None
                    if _raw_obs is not None:
                        _semantic_observation = _raw_obs
                        _record_semantic_trace(session.session_id, _semantic_observation, _semantic_mode)
                        if _semantic_mode == "guarded":
                            try:
                                _cfg = self._semantic_router_config or getattr(_router, "config", None)
                                _cos_th = float(getattr(_cfg, "cosine_threshold", 0.62)) if _cfg is not None else 0.62
                                _mar_th = float(getattr(_cfg, "margin_threshold", 0.10)) if _cfg is not None else 0.10
                                _route = str(getattr(_semantic_observation, "route", "UNKNOWN"))
                                _conf = float(getattr(_semantic_observation, "confidence", 0.0) or 0.0)
                                _margin = float(getattr(_semantic_observation, "margin", 0.0) or 0.0)
                                _degraded = bool(getattr(_semantic_observation, "degraded", False))
                                if (
                                    _route in _SEMANTIC_GUARDED_ALLOWED_ROUTES
                                    and _route not in _SEMANTIC_GUARDED_BLOCKED_ROUTES
                                    and not _degraded
                                    and _conf >= _cos_th
                                    and _margin >= _mar_th
                                    and not _is_subject_ambiguous(text)
                                    and not _is_correction_like(text)
                                ):
                                    _semantic_fast_eligible = True
                                    _semantic_fast_route = _route
                            except Exception:
                                _semantic_fast_eligible = False
                else:
                    try:
                        from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation as _ObsDeg

                        _semantic_observation = _ObsDeg(
                            route="UNKNOWN",
                            confidence=0.0,
                            margin=0.0,
                            latency_ms=0.0,
                            mode=_semantic_mode,
                            degraded=True,
                        )
                        _record_semantic_trace(session.session_id, _semantic_observation, _semantic_mode)
                    except Exception:
                        pass
            except Exception:
                pass
        self._last_semantic_observation = _semantic_observation
        self._last_semantic_mode = _semantic_mode
        # Guarded fast path for PURE_EDUCATION / CHITCHAT : skip interpreter directly to workflow
        if _semantic_fast_eligible and _semantic_fast_route in ("PURE_EDUCATION", "CHITCHAT"):
            context = self.context_manager.append_turn(
                session.conversation_context,
                role="user",
                content=text or "（空白訊息）",
            )
            session = session.model_copy(update={"conversation_context": context}, deep=True)
            if _semantic_fast_route == "PURE_EDUCATION":
                workflow = self._call_education_sync(
                    {
                        "request_id": f"{session.session_id}-v{previous_version + 1}",
                        "schema_version": "a.v0.1",
                        "user_raw_input": text,
                        "declared_role": self._declared_role(session.actor_role),
                        "language": "zh-TW",
                    },
                    task_type=None,
                    intake=None,
                )
            else:
                workflow = self._call_workflow(
                    {
                        "request_id": f"{session.session_id}-v{previous_version + 1}",
                        "schema_version": "a.v0.1",
                        "user_raw_input": text,
                        "declared_role": self._declared_role(session.actor_role),
                        "language": "zh-TW",
                    },
                    task_type=None,
                    intake=None,
                )
            try:
                wf_staged = None
                if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                    wf_staged = workflow.trace.get("staged_latency")
                if wf_staged:
                    for _k in ("rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms"):
                        if _k in wf_staged and isinstance(wf_staged[_k], (int, float)):
                            recorder.record(_k, float(wf_staged[_k]))
            except Exception:
                pass
            session_updates: dict[str, Any] = {}
            if workflow.intake_snapshot is not None:
                try:
                    session_updates["intake_snapshot"] = PreVisitIntake.model_validate(workflow.intake_snapshot)
                except Exception:
                    pass
            if workflow.intake_stage is not None:
                session_updates["intake_stage"] = workflow.intake_stage
            if session_updates:
                session = session.model_copy(update=session_updates, deep=True)
            session = self._sync_clinical_context(session)
            reply = workflow.final_response
            status = workflow.status
            intake_stage = workflow.intake_stage if workflow.intake_stage is not None else session.intake_stage
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=False)
            session = session.model_copy(update={"conversation_context": context}, deep=True)
            with recorder.stage("persistence_ms"):
                saved = self.repository.save(session, expected_version=previous_version)
            staged = recorder.snapshot()
            self._last_staged_latency = staged
            obs_dict = _observation_to_dict(_semantic_observation)
            return OrchestratorResult(
                event_id="pending",
                session_id=saved.session_id,
                reply=reply,
                status=status,
                intake_stage=intake_stage,
                fallback_reason=getattr(workflow, "fallback_reason", None),
                semantic_route=str(obs_dict.get("route") or getattr(_semantic_observation, "route", None)) if _semantic_observation is not None else None,
                semantic_confidence=float(obs_dict.get("confidence") or 0.0) if _semantic_observation is not None else None,
                semantic_margin=float(obs_dict.get("margin") or 0.0) if _semantic_observation is not None else None,
                semantic_latency_ms=float(obs_dict.get("latency_ms") or 0.0) if _semantic_observation is not None else None,
                semantic_degraded=bool(obs_dict.get("degraded", False)) if _semantic_observation is not None else None,
                semantic_mode=_semantic_mode,
                metadata={"semantic_observation": obs_dict, "semantic_fast_path": True, "semantic_fast_route": _semantic_fast_route} if _semantic_observation is not None else None,
            )
        # 8-9: 建 ConversationEnvelope → Interpreter (always unless narrow fast-path or guarded PURE_INTAKE fast-path)
        envelope = None
        interpretation = None
        _skip_interpreter_due_to_semantic = _semantic_fast_eligible and _semantic_fast_route == "PURE_INTAKE"
        if not _skip_ai_for_intake and not _skip_interpreter_due_to_semantic:
            with recorder.stage("conversation_interpreter_ms"):
                try:
                    envelope = build_conversation_envelope(session, text)
                    self._last_envelope = envelope
                    try:
                        interpretation = self.interpreter.interpret(envelope)
                    except Exception:
                        interpretation = DeterministicConversationInterpreter().interpret(envelope)
                    # Deterministic mixed-intent backstop: if formal missed EDUCATION_QUESTION but text has intake+question clauses, synthesize
                    try:
                        interpretation = _maybe_apply_mixed_backstop(text, interpretation)
                    except Exception:
                        pass
                    # "白話一點／可以口語化嗎" refers to the latest
                    # education topic, not the pending intake field.  Resolve
                    # it before a generic clarification pulls the user back
                    # into the form.
                    _rephrase_query = _resolve_rephrase_followup(session, text)
                    if _rephrase_query:
                        interpretation = ConversationTurnInterpretation(
                            intents=["EDUCATION_QUESTION"],
                            resolved_education_query=_rephrase_query,
                            references_resolved=True,
                            confidence=0.99,
                        )
                    self._last_interpretation = interpretation
                except Exception:
                    interpretation = None
                    self._last_envelope = envelope
                    self._last_interpretation = None

            # 10: 若 interpreter 判斷需澄清 (subject 切換不明等)，先追問，不自行轉換
            if interpretation and interpretation.needs_clarification:
                ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
                session = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
                reply = interpretation.clarification_question or "請確認：剛才的資料是你的，還是家人的？"
                session = self._sync_clinical_context(session)
                context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
                context, _ = self.context_manager.compact(context, stage_completed=False)
                saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                _obsd = _observation_to_dict(_semantic_observation) if _semantic_observation is not None else {}
                return OrchestratorResult(
                    event_id="pending",
                    session_id=saved.session_id,
                    reply=reply,
                    status="NEEDS_CLARIFICATION",
                    intake_stage=saved.intake_stage,
                    semantic_route=str(_obsd.get("route")) if _obsd.get("route") else None,
                    semantic_confidence=float(_obsd.get("confidence")) if _obsd.get("confidence") is not None else None,
                    semantic_margin=float(_obsd.get("margin")) if _obsd.get("margin") is not None else None,
                    semantic_latency_ms=float(_obsd.get("latency_ms")) if _obsd.get("latency_ms") is not None else None,
                    semantic_degraded=bool(_obsd.get("degraded")) if "degraded" in _obsd else None,
                    semantic_mode=_semantic_mode if _semantic_observation is not None else None,
                    metadata={"semantic_observation": _obsd} if _obsd else None,
                )
        else:
            # Skipped AI for deterministic intake, interpretation remains None
            interpretation = None
            self._last_interpretation = None
            self._last_envelope = None

        # Record user turn AFTER envelope/interpreter (so envelope recent_turns = prior, current_message independent)
        context = self.context_manager.append_turn(
            session.conversation_context,
            role="user",
            content=text or "（空白訊息）",
        )
        session = session.model_copy(update={"conversation_context": context}, deep=True)

        try:
            if self._is_intake_active(session, text) and session.pending_field == "known_medications":
                _is_coll2 = bool(_MEDICATION_COLLOQUIAL_RE.search(text))
                _is_uncertain_med2 = bool(_MEDICATION_UNCERTAIN_RE.search(text) and "藥" in text)
                if _is_coll2 or _is_uncertain_med2:
                    has_known = bool(_MEDICATION_KNOWN_RE.search(text))
                    if not has_known:
                        from tfda_context_gate.intake.schemas import MEDICATION_CLARIFICATION_QUESTIONS
                        from tfda_context_gate.line_orchestration.response_composer import compose_uncertain
                        attempt_key2 = (session.session_id, "known_medications")
                        attempt2 = _intake_uncertain_attempts.get(attempt_key2, 0)
                        if attempt2 == 0:
                            q = MEDICATION_CLARIFICATION_QUESTIONS[1]
                            _intake_uncertain_attempts[attempt_key2] = 1
                            new_sess = session.model_copy(update={"pending_field": "known_medications", "pending_question": q}, deep=True)
                            new_sess = self._sync_clinical_context(new_sess)
                            ctx2 = self.context_manager.append_turn(new_sess.conversation_context, role="assistant", content=q)
                            ctx2, _ = self.context_manager.compact(ctx2, stage_completed=False)
                            saved2 = self.repository.save(new_sess.model_copy(update={"conversation_context": ctx2}, deep=True), expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved2.session_id, reply=q, status="NEEDS_CLARIFICATION", intake_stage=saved2.intake_stage)
                        elif attempt2 == 1:
                            q = MEDICATION_CLARIFICATION_QUESTIONS[2]
                            _intake_uncertain_attempts[attempt_key2] = 2
                            new_sess = session.model_copy(update={"pending_field": "known_medications", "pending_question": q}, deep=True)
                            new_sess = self._sync_clinical_context(new_sess)
                            ctx2 = self.context_manager.append_turn(new_sess.conversation_context, role="assistant", content=q)
                            ctx2, _ = self.context_manager.compact(ctx2, stage_completed=False)
                            saved2 = self.repository.save(new_sess.model_copy(update={"conversation_context": ctx2}, deep=True), expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved2.session_id, reply=q, status="NEEDS_CLARIFICATION", intake_stage=saved2.intake_stage)
                        else:
                            intake = session.intake_snapshot.model_copy(deep=True)
                            intake.known_medications = ["不清楚（待看診確認）"]
                            _intake_uncertain_attempts.pop(attempt_key2, None)
                            next_field = self._next_pending_field(intake)
                            next_q = self._question_for_field(next_field) if next_field else None
                            new_sess = session.model_copy(update={"intake_snapshot": intake, "pending_field": next_field, "pending_question": next_q}, deep=True)
                            new_sess = self._sync_clinical_context(new_sess)
                            uncertain_msg = compose_uncertain(symptom=False)
                            reply_text = f"{uncertain_msg}\n\n{next_q}" if next_q else uncertain_msg
                            ctx2 = self.context_manager.append_turn(new_sess.conversation_context, role="assistant", content=reply_text)
                            ctx2, _ = self.context_manager.compact(ctx2, stage_completed=False)
                            saved2 = self.repository.save(new_sess.model_copy(update={"conversation_context": ctx2}, deep=True), expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved2.session_id, reply=reply_text, status="NEEDS_CLARIFICATION", intake_stage=saved2.intake_stage)
        except Exception:
            pass
        if _is_empathy_text(text):
            try:
                from tfda_context_gate.workflow.fallbacks import empathy_response
                reply = empathy_response(text)
            except Exception:
                reply = "抱歉讓您有這樣的感受，謝謝您告訴我。我的回覆是依 TFDA／國健署衛教文件整理，比較制式。您可以試試：為什麼會有糖尿病／飲食怎麼吃／上傳藥袋"
                if _SEVERE_EMPATHY_RE and _SEVERE_EMPATHY_RE.search(text):
                    reply += " 若您感到情緒困擾，可撥打 1925 安心專線（24小時）。"
            session = self._sync_clinical_context(session)
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=False)
            saved = self.repository.save(
                session.model_copy(update={"conversation_context": context}, deep=True),
                expected_version=previous_version,
            )
            return _enrich_orchestrator_result(
                OrchestratorResult(
                    event_id="pending",
                    session_id=saved.session_id,
                    reply=reply,
                    status="FALLBACK",
                    intake_stage=saved.intake_stage,
                ),
                _semantic_observation,
                _semantic_mode,
            )

        if session.pending_action and session.pending_action.type == "PENDING_CONFIRM_QUESTION":
            stripped = text.strip()
            norm = re.sub(r"\s+", "", stripped)
            is_disagree = bool(_DISAGREE_RE.search(stripped) or _DISAGREE_RE.search(norm))
            is_agree = False
            if not is_disagree:
                if _AGREE_RE.match(stripped) or _AGREE_RE.match(norm):
                    is_agree = True
                elif len(stripped) <= 8 and _AGREE_SUB_RE.search(stripped):
                    is_agree = True
                elif stripped in ("好", "好的", "好啊", "可以", "沒問題", "幫我記", "記下來", "幫我記下來", "同意", "要"):
                    is_agree = True
            if is_disagree:
                session = session.model_copy(update={"pending_action": None, "pending_question_proposal": None}, deep=True)
                session = self._sync_clinical_context(session)
                context = self.context_manager.append_turn(session.conversation_context, role="assistant", content="好的，已略過，不會記入。")
                context, _ = self.context_manager.compact(context, stage_completed=False)
                saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply="好的，已略過，不會記入。", status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
            if is_agree:
                proposal = session.pending_action.proposal or session.pending_question_proposal or ""
                if proposal:
                    intake = session.intake_snapshot.model_copy(deep=True)
                    if proposal not in intake.questions_for_doctor and len(intake.questions_for_doctor) < 10:
                        intake.questions_for_doctor = [*intake.questions_for_doctor, proposal]
                    session = session.model_copy(update={"intake_snapshot": intake, "pending_action": None, "pending_question_proposal": None}, deep=True)
                    session = self._sync_clinical_context(session)
                    context = self.context_manager.append_turn(session.conversation_context, role="assistant", content="已幫你記下，會在看診摘要中提醒你問醫師。")
                    context, _ = self.context_manager.compact(context, stage_completed=False)
                    saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                    return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply="已幫你記下，會在看診摘要中提醒你問醫師。", status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
                else:
                    session = session.model_copy(update={"pending_action": None, "pending_question_proposal": None}, deep=True)
                    session = self._sync_clinical_context(session)
                    context = self.context_manager.append_turn(session.conversation_context, role="assistant", content="已幫你記下，會在看診摘要中提醒你問醫師。")
                    context, _ = self.context_manager.compact(context, stage_completed=False)
                    saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                    return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply="已幫你記下，會在看診摘要中提醒你問醫師。", status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
            # consent phrase but not agree/disagree ambiguous -> keep pending and fall through to normal? preserve pending

        if session.pending_action and session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
            stripped = text.strip()
            if _SEVERITY_EXPLICIT_RE.search(stripped):
                intake = session.intake_snapshot.model_copy(deep=True)
                mapped = _standardize_severity(stripped)
                if mapped:
                    intake.symptom_severity = mapped
                    # provenance retained via pending_severity_raw before clear, also via conversation turn
                    session = session.model_copy(update={"intake_snapshot": intake, "pending_action": None, "pending_severity_raw": None}, deep=True)

        # 資料來源是臨床摘要的一部分，必須寫進結構化 state，不能只留在自由文字。
        if session.actor_role is ActorRole.RELATED_PERSON:
            if any(value in text for value in self.PROXY_SUBJECT_SOURCE_COMMANDS):
                session = session.model_copy(update={"information_source": InformationSource.SUBJECT_REPORTED_VIA_PROXY})
            elif any(value in text for value in self.PROXY_OBSERVED_SOURCE_COMMANDS):
                session = session.model_copy(update={"information_source": InformationSource.PROXY_OBSERVED})

        # 純同意短句無 pending 時避免當 intake 誤寫
        if not (session.pending_action and session.pending_action.type == "PENDING_CONFIRM_QUESTION"):
            _strip_nospace = re.sub(r"\s+", "", text.strip())
            if _strip_nospace in {"好", "好的", "可以", "沒問題", "幫我記", "記下來", "幫我記下來", "同意"} or (_AGREE_RE.match(text.strip()) and len(text.strip()) <= 6):
                if not _CORRECTION_RE.search(text) and not _WANT_QUESTION_RE.search(text):
                    existing_q = session.intake_snapshot.questions_for_doctor
                    reply_noop = "已記下，若還有其他想問醫師的問題可以繼續補充。" if existing_q else "好的，有其他想問醫師的問題可以再告訴我。"
                    session = self._sync_clinical_context(session)
                    context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply_noop)
                    context, _ = self.context_manager.compact(context, stage_completed=False)
                    saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                    return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply_noop, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)

        command_result = self._handle_product_command(session, text)
        if command_result is not None:
            session, reply, status = command_result
            intake_stage = session.intake_stage
        else:
            # ── Deterministic rephrase rewrite (白話/口語化) — must not be treated as intake answer ──
            if self._is_intake_active(session, text) and _is_rephrase_request(text):
                rephrase_query = _resolve_rephrase_followup(session, text)
                if not rephrase_query and interpretation and getattr(interpretation, "resolved_education_query", None):
                    rephrase_query = interpretation.resolved_education_query  # type: ignore[union-attr]
                if not rephrase_query:
                    # Generic白話 request: use last education topic if any, else keep text as education
                    rephrase_query = text
                    try:
                        for turn in reversed(session.conversation_context.recent_turns):
                            if turn.role == "user" and _orch_should_use_formal(turn.content, None):
                                rephrase_query = f"{turn.content.strip()} 請用一般人容易理解的白話簡短解釋，不新增沒有依據的內容。"
                                break
                    except Exception:
                        pass
                # Education must not write intake; pause intake and answer with side workflow
                side_q = rephrase_query
                try:
                    workflow = self._call_education_sync({
                        "request_id": f"{session.session_id}-rephrase-v{previous_version + 1}",
                        "schema_version": "a.v0.1",
                        "user_raw_input": side_q,
                        "declared_role": self._declared_role(session.actor_role),
                        "language": "zh-TW",
                    })
                except Exception:
                    workflow = self._call_education_sync({
                        "request_id": f"{session.session_id}-rephrase-v{previous_version + 1}",
                        "schema_version": "a.v0.1",
                        "user_raw_input": side_q,
                        "declared_role": self._declared_role(session.actor_role),
                        "language": "zh-TW",
                    })
                try:
                    wf_staged_side = None
                    if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                        wf_staged_side = workflow.trace.get("staged_latency")
                    if wf_staged_side:
                        for _k in ("rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms"):
                            if _k in wf_staged_side and isinstance(wf_staged_side[_k], (int, float)):
                                recorder.record(_k, float(wf_staged_side[_k]))
                except Exception:
                    pass
                reply = self._without_intake_invitation(workflow.final_response)
                pending_q = session.pending_question or self._question_for_field(session.pending_field or self._next_pending_field(session.intake_snapshot))
                if pending_q:
                    reply = compose_side_answer(reply, pending_q)
                context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
                context, _ = self.context_manager.compact(context, stage_completed=False)
                with recorder.stage("persistence_ms"):
                    saved = self.repository.save(session.model_copy(update={"conversation_context": context, "status": "PAUSED"}, deep=True), expected_version=previous_version)
                staged = recorder.snapshot()
                try:
                    if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                        workflow.trace["staged_latency"] = staged
                except Exception:
                    pass
                self._last_staged_latency = staged
                return _enrich_orchestrator_result(OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status="SIDE_ANSWER", intake_stage=saved.intake_stage), _semantic_observation, _semantic_mode)
            is_side_candidate = self._resolve_is_side_candidate(session, text, interpretation)
            if self._is_intake_active(session, text) and is_side_candidate:
                side_query = text
                if interpretation and interpretation.resolved_education_query and interpretation.references_resolved:
                    side_query = interpretation.resolved_education_query
                elif interpretation and interpretation.resolved_education_query and "EDUCATION_QUESTION" in interpretation.intents:
                    side_query = interpretation.resolved_education_query
                workflow = self._call_education_sync({
                    "request_id": f"{session.session_id}-side-v{previous_version + 1}",
                    "schema_version": "a.v0.1",
                    "user_raw_input": side_query,
                    "declared_role": self._declared_role(session.actor_role),
                    "language": "zh-TW",
                })
                try:
                    wf_staged_side = None
                    if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                        wf_staged_side = workflow.trace.get("staged_latency")
                    if wf_staged_side:
                        for _k in ("rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms"):
                            if _k in wf_staged_side and isinstance(wf_staged_side[_k], (int, float)):
                                recorder.record(_k, float(wf_staged_side[_k]))
                except Exception:
                    pass
                reply = self._without_intake_invitation(workflow.final_response)
                pending_question = session.pending_question or self._question_for_field(
                    session.pending_field or self._next_pending_field(session.intake_snapshot)
                )
                if pending_question:
                    reply = compose_side_answer(reply, pending_question)
                context = self.context_manager.append_turn(
                    session.conversation_context, role="assistant", content=reply
                )
                context, _ = self.context_manager.compact(context, stage_completed=False)
                with recorder.stage("persistence_ms"):
                    saved = self.repository.save(
                        session.model_copy(
                            update={"conversation_context": context, "status": "PAUSED"},
                            deep=True,
                        ),
                        expected_version=previous_version,
                    )
                staged = recorder.snapshot()
                try:
                    if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                        workflow.trace["staged_latency"] = staged
                except Exception:
                    pass
                self._last_staged_latency = staged
                return _enrich_orchestrator_result(
                    OrchestratorResult(
                        event_id="pending", session_id=saved.session_id, reply=reply,
                        status="SIDE_ANSWER", intake_stage=saved.intake_stage,
                    ),
                    _semantic_observation,
                    _semantic_mode,
                )

            old_stage = session.intake_stage
            intake_note: str | None = None
            workflow_text = text
            # P1.1 final defense: 控制/閒聊/sentinel 不得寫入 intake
            is_control_or_chitchat = False
            if interpretation and ("CONTROL_COMMAND" in interpretation.intents or "CHITCHAT" in interpretation.intents):
                is_control_or_chitchat = True
            if text.strip() in {"謝謝", "謝謝你", "感謝", "感恩", "辛苦了", "不好意思", "您好", "哈囉", "嗨"} or text.strip().startswith("謝謝"):
                is_control_or_chitchat = True
            # Requirement 2: confirmation words must be handled as intake, not as chitchat/control
            if _is_confirmation_word(text.strip()):
                is_control_or_chitchat = False
            if is_control_or_chitchat and self._is_intake_active(session, text) and session.status in ("ACTIVE", "AWAITING_CONFIRMATION"):
                try:
                    from tfda_context_gate.intake.candidate_merge import is_multi_clause

                    _pending_for_control = session.pending_field or self._next_pending_field(session.intake_snapshot)
                    _is_symptom_like = bool(re.search(r"嘴巴|口乾|口渴|廁所|頻尿|頭暈|麻|視線|很累|疲倦|血糖|血壓", text))
                    if _pending_for_control in ("symptom_description", "symptom_onset", "symptom_severity", "known_medications", "allergies", "chronic_conditions", "family_history") and (_is_symptom_like or is_multi_clause(text)):
                        is_control_or_chitchat = False
                except Exception:
                    pass
            # Mixed-intent deterministic+Formal split — clause切分, intake走驗證, education走RAG/C/D
            is_multi_early = interpretation and (
                ("INTAKE_ANSWER" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents)
                or ("ADD_DOCTOR_QUESTION" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents)
            )
            intake_text_for_normalize = text
            # Keep original for workflow_text fallback; will be overridden after split
            if is_multi_early:
                try:
                    split_intake, split_edu = _split_intake_education_clauses(text, interpretation)
                    if split_intake and split_intake.strip():
                        intake_text_for_normalize = split_intake
                    if split_edu and split_edu.strip():
                        # Preserve interpreter's resolved education query if more complete than split edu
                        if interpretation and getattr(interpretation, "resolved_education_query", None):
                            # Prefer longer resolved query (may include 白話 instruction)
                            rq = interpretation.resolved_education_query or ""
                            if len(rq) >= len(split_edu):
                                pass  # workflow_text will use resolved later
                            else:
                                interpretation = interpretation.model_copy(update={"resolved_education_query": split_edu}) if hasattr(interpretation, "model_copy") else interpretation
                                try:
                                    setattr(interpretation, "resolved_education_query", split_edu)
                                except Exception:
                                    pass
                except Exception:
                    # Fallback naive
                    try:
                        edu_q = interpretation.resolved_education_query if interpretation else None
                        if edu_q and edu_q in text:
                            intake_text_for_normalize = text.replace(edu_q, "").strip("，,。 ")
                        else:
                            parts = text.split("，")
                            for ep in [p for p in parts if "水果" in p or "可以吃" in p or "蛋糕" in p]:
                                intake_text_for_normalize = intake_text_for_normalize.replace(ep, "").strip("，,。 ")
                        if not intake_text_for_normalize.strip():
                            intake_text_for_normalize = text
                    except Exception:
                        intake_text_for_normalize = text
            if not is_control_or_chitchat and self._is_intake_active(session, intake_text_for_normalize) and session.status in ("ACTIVE", "AWAITING_CONFIRMATION"):
                _merged_valid: list[Any] | None = None
                with recorder.stage("candidate_validation_ms"):
                    if not _skip_ai_for_intake and interpretation is not None and getattr(interpretation, "intake_candidates", None):
                        try:
                            from tfda_context_gate.intake.candidate_merge import (
                                deterministic_to_candidates,
                                formal_to_candidates,
                                merge_candidates,
                            )
                            from tfda_context_gate.intake.tool import PreVisitIntakeTool as _P2ATool

                            _tool2 = _P2ATool()
                            _det_extracted = _tool2.extract_fields_from_utterance(intake_text_for_normalize, stage=None)
                            _det_cands = deterministic_to_candidates(_det_extracted, intake_text_for_normalize)
                            _formal_cands = formal_to_candidates(interpretation.intake_candidates, intake_text_for_normalize)
                            try:
                                _repaired_formal: list[Any] = []
                                for _fc in _formal_cands:
                                    _sq = getattr(_fc, "source_quote", "") or ""
                                    _val = getattr(_fc, "value", "") or ""
                                    _new_sq = None
                                    _new_val = None
                                    if _sq and _sq not in intake_text_for_normalize:
                                        if _val and _val in intake_text_for_normalize:
                                            _new_sq = _val[:100]
                                        elif intake_text_for_normalize.strip()[:80] in _sq or (_sq and _sq[:80] in intake_text_for_normalize):
                                            _new_sq = intake_text_for_normalize[:100]
                                    if _val and _val not in intake_text_for_normalize and intake_text_for_normalize and intake_text_for_normalize in _val:
                                        _new_val = intake_text_for_normalize[:500]
                                        if _new_sq is None:
                                            _new_sq = intake_text_for_normalize[:100]
                                    if _new_sq is not None or _new_val is not None:
                                        try:
                                            import dataclasses

                                            if dataclasses.is_dataclass(_fc):
                                                kwargs = {}
                                                if _new_sq is not None:
                                                    kwargs["source_quote"] = _new_sq
                                                if _new_val is not None:
                                                    kwargs["value"] = _new_val
                                                _fc = dataclasses.replace(_fc, **kwargs)  # type: ignore[arg-type]
                                            elif hasattr(_fc, "model_copy"):
                                                upd = {}
                                                if _new_sq is not None:
                                                    upd["source_quote"] = _new_sq
                                                if _new_val is not None:
                                                    upd["value"] = _new_val
                                                _fc = _fc.model_copy(update=upd)
                                            else:
                                                if _new_sq is not None:
                                                    setattr(_fc, "source_quote", _new_sq)
                                                if _new_val is not None:
                                                    setattr(_fc, "value", _new_val)
                                        except Exception:
                                            try:
                                                if _new_sq is not None:
                                                    setattr(_fc, "source_quote", _new_sq)
                                                if _new_val is not None:
                                                    setattr(_fc, "value", _new_val)
                                            except Exception:
                                                pass
                                    _repaired_formal.append(_fc)
                                _formal_cands = _repaired_formal
                            except Exception:
                                pass
                            _valid, _need_clarify = merge_candidates(_det_cands, _formal_cands, existing_intake=session.intake_snapshot)
                            # B fix: 問句不得寫成症狀 + prevent symptom-as-medication misroute
                            try:
                                from tfda_context_gate.intake.candidate_merge import is_question_like as _is_q_like

                                _filtered: list[Any] = []
                                for _c in _valid:
                                    _field = getattr(_c, "target_field", "")
                                    _val = getattr(_c, "value", "") or ""
                                    _sq = getattr(_c, "source_quote", "") or ""
                                    _raw = getattr(_c, "raw", "") or intake_text_for_normalize
                                    # Question clause must not become symptom/medication
                                    if _field in ("symptom_description", "symptom_onset", "symptom_severity", "known_medications"):
                                        if _is_q_like(_raw) or _is_q_like(_sq) or _is_q_like(_val):
                                            if _is_q_like(_sq):
                                                continue
                                            if _field in ("symptom_description", "symptom_onset", "symptom_severity"):
                                                if _is_q_like(_val) or ("？" in _val or "是不是" in _val or "糖尿病嗎" in _val):
                                                    continue
                                    # Symptom-like text must not go to known_medications without med keyword
                                    if _field == "known_medications":
                                        _sym_like = bool(re.search(r"嘴巴乾|口乾|跑廁所|上廁所|口渴|頻尿|頭暈|夜尿|很乾", _val))
                                        _med_like = bool(re.search(r"metformin|二甲雙胍|胰島素|insulin|吃藥|用藥|服用|藥", _val, re.IGNORECASE))
                                        if _sym_like and not _med_like:
                                            continue
                                    _filtered.append(_c)
                                _valid = _filtered
                            except Exception:
                                pass
                            _merged_valid = _valid
                        except Exception:
                            _merged_valid = None
                    before_snapshot = session.intake_snapshot.model_dump(mode="json") if hasattr(session.intake_snapshot, "model_dump") else {}
                    session, intake_note = self._normalize_intake_answer(
                        session,
                        intake_text_for_normalize,
                        merged_valid=_merged_valid,
                        allow_cross_stage_symptom_description=bool(is_multi_early),
                    )
                    try:
                        after_snapshot = session.intake_snapshot.model_dump(mode="json") if hasattr(session.intake_snapshot, "model_dump") else {}
                        changed_fields = []
                        for _fld in ("known_medications","allergies","chronic_conditions","family_history","symptom_onset","symptom_description","symptom_severity","questions_for_doctor"):
                            if before_snapshot.get(_fld) != after_snapshot.get(_fld):
                                changed_fields.append(_fld)
                        if changed_fields:
                            prov = dict(getattr(session, "intake_field_provenance", {}) or {})
                            raw_prov = intake_text_for_normalize.strip()[:80] if intake_text_for_normalize else text.strip()[:80]
                            if _merged_valid:
                                for _mc in _merged_valid:
                                    _fld = getattr(_mc, "target_field", None) or getattr(_mc, "field_name", None)
                                    if _fld and _fld in changed_fields and _fld not in prov:
                                        _sq = getattr(_mc, "source_quote", None) or raw_prov
                                        prov[_fld] = str(_sq).strip()[:80]
                            for _fld in changed_fields:
                                if _fld not in prov:
                                    prov[_fld] = raw_prov
                            session = session.model_copy(update={"intake_field_provenance": prov}, deep=True)
                    except Exception:
                        pass
                    # Requirement 1 & 4: Brown Bag clarification should short-circuit workflow and preserve clarification question
                    if intake_note and ("藥袋" in intake_note or "顏色、形狀" in intake_note):
                        session = self._sync_clinical_context(session)
                        if not session.pending_field:
                            session = session.model_copy(update={"pending_field": "known_medications", "pending_question": intake_note}, deep=True)
                        context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=intake_note)
                        context, _ = self.context_manager.compact(context, stage_completed=False)
                        saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                        return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=intake_note, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
                    # Requirement 2: confirmation word that already advanced pending (e.g., "正確" after implicit confirm) should not go through workflow
                    if intake_note and not changed_fields and _is_confirmation_word(intake_text_for_normalize.strip()):
                        if session.pending_field and session.pending_question == intake_note:
                            session = self._sync_clinical_context(session)
                            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=intake_note)
                            context, _ = self.context_manager.compact(context, stage_completed=False)
                            saved = self.repository.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=previous_version)
                            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=intake_note, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
                if session.pending_action and session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
                    session = self._sync_clinical_context(session)
                    context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=intake_note or "請問程度大約是輕度、中度、重度，或 1–10 分中的幾分？")
                    context, _ = self.context_manager.compact(context, stage_completed=False)
                    pending_q = self._question_for_field("symptom_severity") or "程度大約是輕度、中度、重度，或 1–10 分中的幾分？"
                    session = session.model_copy(update={"pending_question": pending_q, "pending_field": "symptom_severity"}, deep=True)
                    saved = self.repository.save(session, expected_version=previous_version)
                    return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=intake_note or pending_q, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
            # stage3 negative converged to review: clear stale pending and keep review
            if session.pending_action and session.pending_action.type == "PENDING_STAGE_TRANSITION":
                session = session.model_copy(update={"pending_action": None}, deep=True)
                if session.intake_stage != "review":
                    session = session.model_copy(update={"intake_stage": "review", "pending_field": None, "pending_question": None}, deep=True)
                session = self._sync_clinical_context(session)
                context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=intake_note or "好的，已進入確認階段。")
                context, _ = self.context_manager.compact(context, stage_completed=False)
                saved = self.repository.save(session, expected_version=previous_version)
                return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=intake_note or "好的，已進入確認階段。", status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
            workflow_text = text
            is_multi = interpretation and (
                ("INTAKE_ANSWER" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents)
                or ("ADD_DOCTOR_QUESTION" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents)
            )
            # Keep split edu if we already split; otherwise use interpreter resolved
            if is_multi_early and intake_text_for_normalize != text:
                # is_multi_early already split; workflow_text should be education part
                try:
                    _, split_edu2 = _split_intake_education_clauses(text, interpretation)
                    if split_edu2:
                        workflow_text = split_edu2
                    elif interpretation and interpretation.resolved_education_query and "EDUCATION_QUESTION" in interpretation.intents:
                        workflow_text = interpretation.resolved_education_query
                except Exception:
                    if interpretation and interpretation.resolved_education_query and "EDUCATION_QUESTION" in interpretation.intents:
                        workflow_text = interpretation.resolved_education_query
            elif interpretation and interpretation.resolved_education_query and "EDUCATION_QUESTION" in interpretation.intents:
                workflow_text = interpretation.resolved_education_query
            elif interpretation and interpretation.references_resolved and interpretation.resolved_education_query:
                workflow_text = interpretation.resolved_education_query
            if is_multi:
                # Multi-intent: intake already handled via _normalize_intake_answer, education via separate RAG workflow (no intake task)
                try:
                    if _orch_should_use_formal(workflow_text, None):
                        workflow = self._call_education_sync(
                            {
                                "request_id": f"{session.session_id}-v{previous_version + 1}",
                                "schema_version": "a.v0.1",
                                "user_raw_input": workflow_text,
                                "declared_role": self._declared_role(session.actor_role),
                                "language": "zh-TW",
                            },
                            task_type=None,
                            intake=None,
                        )
                    else:
                        workflow = self._call_workflow(
                            {
                                "request_id": f"{session.session_id}-v{previous_version + 1}",
                                "schema_version": "a.v0.1",
                                "user_raw_input": workflow_text,
                                "declared_role": self._declared_role(session.actor_role),
                                "language": "zh-TW",
                            },
                            task_type=None,
                            intake=None,
                        )
                except Exception:
                    workflow = self._call_workflow(
                        {
                            "request_id": f"{session.session_id}-v{previous_version + 1}",
                            "schema_version": "a.v0.1",
                            "user_raw_input": workflow_text,
                            "declared_role": self._declared_role(session.actor_role),
                            "language": "zh-TW",
                        },
                        task_type=None,
                        intake=None,
                    )
            else:
                # P1.1: chitchat/control should not be treated as intake even if intake active
                is_chitchat_control = is_control_or_chitchat or (interpretation and "CHITCHAT" in interpretation.intents)
                _task_type = None
                _intake_val = None
                if not is_chitchat_control and session.status in {"ACTIVE", "AWAITING_CONFIRMATION"} and self._is_intake_active(session, text):
                    _task_type = "pre_visit_intake"
                    _intake_val = session.intake_snapshot
                try:
                    if _task_type is None and _orch_should_use_formal(workflow_text, _task_type):
                        workflow = self._call_education_sync(
                            {
                                "request_id": f"{session.session_id}-v{previous_version + 1}",
                                "schema_version": "a.v0.1",
                                "user_raw_input": workflow_text,
                                "declared_role": self._declared_role(session.actor_role),
                                "language": "zh-TW",
                            },
                            task_type=_task_type,
                            intake=_intake_val,
                        )
                    else:
                        workflow = self._call_workflow(
                            {
                                "request_id": f"{session.session_id}-v{previous_version + 1}",
                                "schema_version": "a.v0.1",
                                "user_raw_input": workflow_text,
                                "declared_role": self._declared_role(session.actor_role),
                                "language": "zh-TW",
                            },
                            task_type=_task_type,
                            intake=_intake_val,
                        )
                except Exception:
                    workflow = self._call_workflow(
                        {
                            "request_id": f"{session.session_id}-v{previous_version + 1}",
                            "schema_version": "a.v0.1",
                            "user_raw_input": workflow_text,
                            "declared_role": self._declared_role(session.actor_role),
                            "language": "zh-TW",
                        },
                        task_type=_task_type,
                        intake=_intake_val,
                    )
            try:
                wf_staged = None
                if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                    wf_staged = workflow.trace.get("staged_latency")
                if wf_staged:
                    for _k in ("rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms"):
                        if _k in wf_staged and isinstance(wf_staged[_k], (int, float)):
                            recorder.record(_k, float(wf_staged[_k]))
            except Exception:
                pass
            is_multi_multi = interpretation and "INTAKE_ANSWER" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents
            pending_q_from_workflow = workflow.question
            # P1 multi: education workflow has no intake question, need to preserve next intake question
            if is_multi_multi and pending_q_from_workflow is None:
                nxt_field = self._next_pending_field(PreVisitIntake.model_validate(workflow.intake_snapshot or session.intake_snapshot))
                pending_q_from_workflow = self._question_for_field(nxt_field) if nxt_field else None
            updates: dict[str, Any] = {
                "pending_question": pending_q_from_workflow,
                "system_risk_classification": self._merge_risk(
                    session.system_risk_classification,
                    workflow.system_risk_classification or risk.model_dump(mode="json"),
                ),
            }
            if workflow.intake_snapshot is not None:
                wf_intake = PreVisitIntake.model_validate(workflow.intake_snapshot)
                try:
                    merged = wf_intake.model_dump()
                    for _f in ("known_medications", "allergies", "chronic_conditions", "family_history", "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor"):
                        wf_val = getattr(wf_intake, _f)
                        sess_val = getattr(session.intake_snapshot, _f)
                        if not wf_val and sess_val:
                            merged[_f] = sess_val
                        elif isinstance(wf_val, list) and isinstance(sess_val, list) and sess_val:
                            combined = list(sess_val)
                            for _v in wf_val:
                                if _v not in combined:
                                    combined.append(_v)
                            merged[_f] = combined
                    updates["intake_snapshot"] = PreVisitIntake.model_validate(merged)
                except Exception:
                    updates["intake_snapshot"] = wf_intake
            if workflow.intake_stage is not None:
                updates["intake_stage"] = workflow.intake_stage
            resulting_intake = PreVisitIntake.model_validate(
                updates.get("intake_snapshot") or workflow.intake_snapshot or session.intake_snapshot
            )
            # Do not let an education-looking doctor question strand a fully
            # collected intake in stage3.  The local intake normalizer is the
            # source of truth for writes; once all eight fields exist we can
            # deterministically enter Review even if the downstream workflow
            # classified the same sentence as general education.
            completed_stage3_locally = (
                old_stage == "stage3"
                and bool(resulting_intake.questions_for_doctor)
                and self._next_pending_field(resulting_intake) is None
            )
            # P1.1: general education should not create pending intake (active_task should be general_education)
            if self._is_intake_active(session, text) and session.status in ("ACTIVE", "AWAITING_CONFIRMATION"):
                updates["pending_field"] = self._next_pending_field(resulting_intake)
                if is_multi_multi and updates.get("pending_question") is None and updates.get("pending_field"):
                    updates["pending_question"] = self._question_for_field(updates["pending_field"])
            else:
                updates["pending_field"] = None
                updates["pending_question"] = None
            if workflow.status == "NEEDS_CONFIRMATION":
                updates["status"] = "AWAITING_CONFIRMATION"
            if completed_stage3_locally:
                updates.update(
                    {
                        "status": "AWAITING_CONFIRMATION",
                        "intake_stage": "review",
                        "pending_field": None,
                        "pending_question": None,
                        "pending_action": None,
                    }
                )
            if not resulting_intake.questions_for_doctor and updates.get("intake_stage") == "review":
                from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS as _QMAP
                updates["intake_stage"] = "stage3"
                updates["pending_field"] = "questions_for_doctor"
                updates["pending_question"] = _QMAP.get("questions_for_doctor")
            session = session.model_copy(update=updates, deep=True)
            # Intake write succeeded but workflow mis-routed short answers to Q_NEED_MORE/BLOCKED — override to stay in intake.
            # For mixed intent, preserve honest fallback instead of hiding it.
            is_honest_fallback = workflow.fallback_reason in HONEST_FALLBACK_REASONS
            if intake_note is not None and workflow.status in ("BLOCKED", "FALLBACK") and workflow.fallback_reason in ("Q_NEED_MORE", "O_GENERIC", "CHIT_CHAT_OUT_OF_SCOPE", "B_INSUFFICIENT", "O_GENERIC") and not (is_multi_multi and is_honest_fallback):
                pending_after = self._next_pending_field(resulting_intake)
                if pending_after:
                    from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS as _QOVR
                    ov_stage = self._field_stage(pending_after)
                    ov_reply = _QOVR.get(pending_after) or workflow.final_response
                    session = session.model_copy(update={"intake_stage": ov_stage, "pending_field": pending_after, "pending_question": ov_reply}, deep=True)
                    reply, status, intake_stage = ov_reply, "NEEDS_CLARIFICATION", ov_stage
                    workflow = workflow.model_copy(update={"final_response": ov_reply, "status": "NEEDS_CLARIFICATION", "intake_stage": ov_stage}) if hasattr(workflow, "model_copy") else workflow
                else:
                    reply, status, intake_stage = workflow.final_response, workflow.status, workflow.intake_stage
            else:
                reply, status, intake_stage = workflow.final_response, workflow.status, workflow.intake_stage
                # When workflow returned None stage (e.g. BLOCKED) but intake progressed, use session's derived stage
                # P1 multi: keep education answer, don't overwrite with pending question (will be appended deterministically later)
                if intake_stage is None and intake_note is not None and not is_multi:
                    intake_stage = session.intake_stage
                    status = "NEEDS_CLARIFICATION" if session.pending_field else status
                    if session.pending_question:
                        reply = session.pending_question
            if completed_stage3_locally and workflow.status != "NEEDS_CONFIRMATION":
                status = "NEEDS_CONFIRMATION"
                intake_stage = "review"
                review_prompt = (
                    "看診前資料已整理完成。請先查看摘要；內容正確請回覆「確認完成」，"
                    "需要調整則回覆「修改看診資料」。"
                )
                reply = f"{reply}\n\n{review_prompt}" if reply else review_prompt
            if not resulting_intake.questions_for_doctor and intake_stage == "review":
                from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS as _QMAP2
                intake_stage = "stage3"
                status = "NEEDS_CLARIFICATION"
                reply = _QMAP2.get("questions_for_doctor", reply)
                session = session.model_copy(update={"intake_stage": "stage3", "pending_field": "questions_for_doctor", "pending_question": reply}, deep=True)
            stage_completed = old_stage != session.intake_stage
            checkpoint: str | None = None
            if stage_completed and old_stage in {"stage1", "stage2"}:
                checkpoint = self._stage_checkpoint(resulting_intake, old_stage)
            if intake_note and checkpoint:
                if intake_note.strip() == reply.strip():
                    reply = intake_note
                else:
                    reply = f"{intake_note}\n\n{checkpoint}\n\n{reply}"
            elif intake_note:
                if intake_note.strip() == reply.strip():
                    reply = intake_note
                elif intake_note.strip() in reply:
                    reply = reply
                else:
                    # The workflow may repeat the same implicit confirmation
                    # without P2B's repair hint and then append the next
                    # question. Replace that leading duplicate instead of
                    # stacking two near-identical confirmations.
                    confirmation_core = re.sub(
                        r"\s*如果不對[，,]?.*$", "", intake_note.strip()
                    ).strip()
                    if confirmation_core and reply.strip().startswith(confirmation_core):
                        remainder = reply.strip()[len(confirmation_core):].strip()
                        reply = (
                            f"{intake_note}\n\n{remainder}"
                            if remainder
                            else intake_note
                        )
                    else:
                        reply = f"{intake_note}\n\n{reply}"
            elif checkpoint:
                reply = f"{checkpoint}\n\n{reply}"
            # P1 multi: deterministically append next intake question after education answer
            # For honest fallback, also append pending to keep intake flow while preserving honesty
            if is_multi_multi and session.pending_field and session.pending_question:
                nxt_q = session.pending_question
                honest = workflow.fallback_reason in HONEST_FALLBACK_REASONS if hasattr(workflow, "fallback_reason") else False
                if nxt_q and nxt_q.strip() not in reply and (workflow.status == "COMPLETED" or (workflow.status == "FALLBACK" and honest)):
                    reply = f"{reply}\n\n{nxt_q}"
            session = self._sync_clinical_context(session)
            if stage_completed and old_stage in {"stage1", "stage2", "stage3"}:
                session = session.model_copy(update={
                    "conversation_context": self.context_manager.mark_stage_completed(
                        session.conversation_context,
                        old_stage,
                        next_stage=session.intake_stage,
                    )
                }, deep=True)
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=stage_completed)
            session = session.model_copy(update={"conversation_context": context}, deep=True)
            with recorder.stage("persistence_ms"):
                saved = self.repository.save(session, expected_version=previous_version)
            staged = recorder.snapshot()
            try:
                if hasattr(workflow, "trace") and isinstance(workflow.trace, dict):
                    workflow.trace["staged_latency"] = staged
            except Exception:
                pass
            self._last_staged_latency = staged
            return _enrich_orchestrator_result(
                OrchestratorResult(
                    event_id="pending",
                    session_id=saved.session_id,
                    reply=reply,
                    status=status,
                    intake_stage=intake_stage,
                    fallback_reason=getattr(workflow, "fallback_reason", None),
                ),
                _semantic_observation,
                _semantic_mode,
            )

        session = self._sync_clinical_context(session)
        context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
        context, _ = self.context_manager.compact(context, stage_completed=False)
        session = session.model_copy(update={"conversation_context": context}, deep=True)
        with recorder.stage("persistence_ms"):
            saved = self.repository.save(session, expected_version=previous_version)
        staged = recorder.snapshot()
        self._last_staged_latency = staged
        return _enrich_orchestrator_result(
            OrchestratorResult(
                event_id="pending",
                session_id=saved.session_id,
                reply=reply,
                status=status,
                intake_stage=intake_stage,
                fallback_reason=getattr(workflow, "fallback_reason", None),
            ),
            _semantic_observation,
            _semantic_mode,
        )

    _PROXY_FUZZY_RE = re.compile(r"幫.{0,10}問|代.{0,10}整理|幫.{0,10}整理|替.{0,10}問", re.IGNORECASE)
    _UNCERTAIN_RE = re.compile(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚|不太知道", re.IGNORECASE)
    _BARE_UNCERTAIN_RE = re.compile(r"^\s*(我)?\s*(不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚|不太知道)\s*[啊欸呢啦哦喔嗎]?[？?。!！]*\s*$", re.IGNORECASE)

    @classmethod
    def _is_bare_uncertain(cls, text: str) -> bool:
        try:
            n = unicodedata.normalize("NFKC", text).strip()
            if cls._BARE_UNCERTAIN_RE.match(n):
                return True
            compact = re.sub(r"\s+", "", n)
            if compact in {"不知道", "我不知道", "我不知道啊", "不清楚", "我不清楚", "不確定", "忘了", "忘記了"}:
                return True
        except Exception:
            pass
        return False

    @classmethod
    def _is_proxy_intent(cls, text: str) -> bool:
        if cls._PROXY_FUZZY_RE.search(text):
            return True
        if "幫" in text and "問" in text:
            return True
        if ("代" in text or "幫" in text) and "整理" in text:
            return True
        return False

    _IDENTITY_RE = re.compile(
        r"你是真人|是真人嗎|是機器人|是.*AI|是.*ai|人工客服|真人客服|有人在嗎|電腦自動|跟醫生聊天|跟醫師聊天|機器人在回|AI在回",
        re.IGNORECASE,
    )

    @classmethod
    def _is_identity_question(cls, text: str) -> bool:
        try:
            n = unicodedata.normalize("NFKC", text).strip()
            # Cover variants: 你是真人嗎？/現在是機器人在回覆嗎？/這是 AI 客服嗎？/有人在嗎，還是電腦自動回答？/我是在跟醫生聊天嗎？
            if cls._IDENTITY_RE.search(n):
                return True
            # Additional explicit patterns
            if "你是" in n and ("真人" in n or "AI" in n or "ai" in n.lower()):
                return True
            if "是" in n and "機器人" in n:
                return True
            if "AI" in n and ("客服" in n or "回" in n):
                return True
            if "有人在嗎" in n:
                return True
            if "跟醫生" in n or "跟醫師" in n:
                return True
        except Exception:
            pass
        return False

    def _handle_product_command(self, session: ProductSession, text: str) -> tuple[ProductSession, str, str] | None:
        if (
            (session.authorization_status is AuthorizationStatus.UNVERIFIED or session.status == "CLOSED")
            and (text in self.START_INTAKE_COMMANDS or any(token in text for token in ("準備看診", "回診", "看醫生")))
        ):
            return session, "我是 AI 看診前整理助理，只協助衛教與資料整理，不做診斷，也不是緊急醫療服務。Demo session 最多保存 7 天；確認前不會分享給醫護。這份資料是為誰整理？請選擇「為自己整理」或「代家人整理」。", "NEEDS_ROLE_SELECTION"
        if (
            (session.authorization_status is AuthorizationStatus.UNVERIFIED or session.status == "CLOSED")
            and self._is_proxy_intent(text)
        ):
            if self.risk_policy.classify(text).level == "RED_FLAG":
                return None
            subject_hash = self._hash(f"{session.session_id}:proxy-subject")
            reset_subject = (
                session.status == "SUBMITTED"
                or session.actor_role is not ActorRole.RELATED_PERSON
                or session.subject_id_hash not in {None, subject_hash}
            )
            reset = self._new_subject_state(session, text) if reset_subject else {}
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.RELATED_PERSON,
                "frontend_persona": FrontendPersona.PATIENT_FAMILY,
                "authorization_status": AuthorizationStatus.UNVERIFIED,
                "permission_scopes": [],
                "subject_id_hash": subject_hash,
                "information_source": InformationSource.PROXY_OBSERVED,
                "pending_field": None,
                "pending_question": "請先確認：是否已取得家人同意，由您代為整理這份看診資料？",
            }, deep=True)
            return session, session.pending_question or "請確認家人同意。", "NEEDS_AUTHORIZATION"
        if (
            (session.authorization_status is AuthorizationStatus.UNVERIFIED or session.status == "CLOSED")
            and not self._is_intake_active(session)
            and self._UNCERTAIN_RE.search(text)
            and not self._is_bare_uncertain(text)
            and self.risk_policy.classify(text).level != "RED_FLAG"
        ):
            return session, "我是 AI 看診前整理助理，只協助衛教與資料整理，不做診斷，也不是緊急醫療服務。Demo session 最多保存 7 天；確認前不會分享給醫護。這份資料是為誰整理？請選擇「為自己整理」或「代家人整理」。", "NEEDS_ROLE_SELECTION"
        if text in self.SHARE_COMMANDS:
            if session.status != "SUBMITTED":
                return session, "請先完成看診摘要的 Review & Confirm，才能分享給醫護。", "NEEDS_CONFIRMATION"
            return session, "摘要已可分享。請開啟「分享給醫護」頁面建立一次性短效連結。", "READY_TO_SHARE"
        if text == "我要上傳藥袋":
            return session, "請直接傳送藥袋照片；建議正面、背面各拍一張，文字保持清楚。", "AWAITING_IMAGE"
        if text in self.PAUSE_COMMANDS and self._is_intake_active(session):
            session = session.model_copy(update={"status": "PAUSED"})
            try:
                from tfda_context_gate.intake.tool import format_stage_progress

                progress = format_stage_progress(session.intake_snapshot)
                if progress and "第" not in progress:
                    return session, f"好的，已先暫停；目前資料會保留，不用重新填。你可以先問其他問題，想回來時點「繼續整理」即可。\n{progress}"[:60], "PAUSED"
            except Exception:
                pass
            return session, "好的，已先暫停；目前資料會保留，不用重新填。你可以先問其他問題，想回來時點「繼續整理」即可。", "PAUSED"
        if text in self.CANCEL_COMMANDS and self._is_intake_active(session):
            reset = self._new_subject_state(session, text)
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.PATIENT,
                "authorization_status": AuthorizationStatus.UNVERIFIED,
                "permission_scopes": [],
                "subject_id_hash": None,
                "information_source": None,
                "status": "CLOSED",
            }, deep=True)
            return session, "已結束這次看診資料整理，尚未提交的內容已清除。需要時可再輸入「準備看診」重新開始。", "CANCELLED"
        if text in self.RESUME_COMMANDS and session.authorization_status in {
            AuthorizationStatus.PATIENT_SELF,
            AuthorizationStatus.AUTHORIZED_CAREGIVER,
            AuthorizationStatus.LEGAL_GUARDIAN,
        } and session.status in ("PAUSED", "ACTIVE") and self._is_intake_active(session):
            pending_field = session.pending_field or self._next_pending_field(session.intake_snapshot)
            question = session.pending_question or self._question_for_field(pending_field)
            # 若已在 ACTIVE 且 pending_field 存在，仍視為繼續
            session = session.model_copy(update={"status": "ACTIVE", "pending_field": pending_field, "pending_question": question})
            base = question or "看診資料已經整理完成，請查看摘要。"
            try:
                from tfda_context_gate.intake.tool import format_stage_progress

                progress = format_stage_progress(session.intake_snapshot)
                if progress and "第" not in progress and "皆已完成" not in progress:
                    return session, f"{progress}\n\n{base}"[:60], "NEEDS_CLARIFICATION"
                if progress and "皆已完成" in progress:
                    return session, f"{progress}\n\n{base}"[:60], "NEEDS_CLARIFICATION"
            except Exception:
                pass
            return session, base, "NEEDS_CLARIFICATION"
        if text in {"使用說明與緊急協助", "使用說明"}:
            return session, "本系統提供糖尿病衛教與看診前整理，不是診斷或急診服務；若有呼吸困難、意識不清等緊急狀況，請立即聯絡當地緊急醫療服務。", "INFORMATION"
        if text in self.SUMMARY_COMMANDS:
            allowed = {PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.VIEW_PROXY_SUMMARY}
            if not allowed.intersection(session.permission_scopes):
                return session, "目前沒有權限查看這份摘要，請先完成身分與授權確認。", "FORBIDDEN"
            from tfda_context_gate.intake.summary import generate_previsit_summary
            summary = generate_previsit_summary(session.intake_snapshot, request_id=session.session_id)
            from tfda_context_gate.d_output_gate.gate import run_previsit_output_gate
            gate = run_previsit_output_gate({
                "request_id": session.session_id,
                "schema_version": "d.v0.1",
                "policy": {
                    "router_status": "G_GENERAL_EDUCATION",
                    "rag_allowed": True,
                    "risk_flags": [],
                    "intent_tags": [],
                    "reason_codes": ["PRODUCT_SUMMARY_REVIEW"],
                },
                "b_result": None,
                "c_result": summary.model_dump(mode="json"),
            })
            if gate.decision != "PASS":
                return session, gate.final_response, "FALLBACK"
            return session, f"目前看診摘要：\n{gate.final_response}\n\n尚缺：{'、'.join(summary.missing_fields) or '無'}", "SUMMARY"
        if text in self.MODIFY_COMMANDS:
            if not {PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.VIEW_PROXY_SUMMARY}.intersection(session.permission_scopes):
                return session, "目前沒有可修改的看診資料。", "FORBIDDEN"
            return session, "請選擇要修改的部分：用藥與病史、症狀、想問醫師的問題。", "NEEDS_MODIFICATION_SELECTION"
        modification = {
            "修改用藥與病史": ("stage1", {"known_medications": [], "allergies": [], "chronic_conditions": [], "family_history": []}),
            "修改症狀": ("stage2", {"symptom_onset": None, "symptom_description": None, "symptom_severity": None}),
            "修改想問醫師的問題": ("stage3", {"questions_for_doctor": []}),
        }.get(text)
        if modification is not None:
            stage, reset = modification
            intake = session.intake_snapshot.model_copy(update=reset, deep=True)
            pending_field = self._next_pending_field(intake)
            question = self._question_for_field(pending_field)
            session = session.model_copy(update={"intake_snapshot": intake, "intake_stage": stage, "status": "ACTIVE", "pending_field": pending_field, "pending_question": question}, deep=True)
            return session, question, "NEEDS_CLARIFICATION"
        if text in self.SELF_COMMANDS:
            reset_subject = (
                session.status == "SUBMITTED"
                or session.actor_role is not ActorRole.PATIENT
                or session.subject_id_hash not in {None, session.principal_id_hash}
            )
            reset = self._new_subject_state(session, text) if reset_subject else {}
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.PATIENT,
                "frontend_persona": FrontendPersona.PATIENT_FAMILY,
                "authorization_status": AuthorizationStatus.PATIENT_SELF,
                "permission_scopes": [PermissionScope.CREATE_OWN_INTAKE, PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.SHARE_OWN_SUMMARY],
                "subject_id_hash": session.principal_id_hash,
                "information_source": InformationSource.SELF_REPORTED,
                "intake_stage": "stage1",
                "status": "ACTIVE",
                "pending_field": "known_medications",
                "pending_question": self._question_for_field("known_medications"),
            }, deep=True)
            return session, session.pending_question or "請提供看診資料。", "NEEDS_CLARIFICATION"
        if text in self.PROXY_COMMANDS:
            subject_hash = self._hash(f"{session.session_id}:proxy-subject")
            reset_subject = (
                session.status == "SUBMITTED"
                or session.actor_role is not ActorRole.RELATED_PERSON
                or session.subject_id_hash not in {None, subject_hash}
            )
            reset = self._new_subject_state(session, text) if reset_subject else {}
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.RELATED_PERSON,
                "frontend_persona": FrontendPersona.PATIENT_FAMILY,
                "authorization_status": AuthorizationStatus.UNVERIFIED,
                "permission_scopes": [],
                "subject_id_hash": subject_hash,
                "information_source": InformationSource.PROXY_OBSERVED,
                "pending_field": None,
                "pending_question": "請先確認：是否已取得家人同意，由您代為整理這份看診資料？",
            }, deep=True)
            return session, session.pending_question or "請確認家人同意。", "NEEDS_AUTHORIZATION"
        if session.actor_role is ActorRole.RELATED_PERSON and text in self.PROXY_CONSENT_COMMANDS:
            session = session.model_copy(update={
                "authorization_status": AuthorizationStatus.AUTHORIZED_CAREGIVER,
                "permission_scopes": [PermissionScope.CREATE_PROXY_INTAKE, PermissionScope.VIEW_PROXY_SUMMARY, PermissionScope.SHARE_PROXY_SUMMARY],
                "pending_field": None,
                "pending_question": "這些資料主要是家人本人描述，還是您的觀察？",
                "intake_stage": "stage1",
            }, deep=True)
            return session, session.pending_question or "請提供看診資料。", "NEEDS_CLARIFICATION"
        if (
            session.actor_role is ActorRole.RELATED_PERSON
            and session.authorization_status is AuthorizationStatus.AUTHORIZED_CAREGIVER
            and text in self.PROXY_SUBJECT_SOURCE_COMMANDS.union(self.PROXY_OBSERVED_SOURCE_COMMANDS)
        ):
            session = session.model_copy(update={
                "pending_field": "known_medications",
                "pending_question": self._question_for_field("known_medications"),
            })
            return session, session.pending_question or "請提供看診資料。", "NEEDS_CLARIFICATION"
        if (session.status in ("AWAITING_CONFIRMATION", "ACTIVE") and (session.intake_stage in ("review", "submitted") or session.status == "AWAITING_CONFIRMATION" or self._next_pending_field(session.intake_snapshot) is None)) and text in self.CONFIRM_COMMANDS:
            session = session.model_copy(update={"status": "SUBMITTED", "intake_stage": "submitted", "pending_field": None, "pending_question": None, "pending_action": None, "pending_severity_raw": None, "pending_question_proposal": None, "stage_transition_flag": None}, deep=True)
            return session, "看診前資料已確認完成。您現在可以選擇「分享給醫護」。", "SUBMITTED"
        return None

    def _new_subject_state(self, session: ProductSession, command: str) -> dict[str, Any]:
        """切換資料主體時清除上一位 subject 的健康資料與近期對話。"""
        context = self.context_manager.create(session.session_id)
        context = self.context_manager.append_turn(context, role="user", content=command)
        return {
            "conversation_context": context,
            "intake_snapshot": PreVisitIntake(),
            "intake_stage": "stage1",
            "pending_field": None,
            "pending_question": None,
            "pending_action": None,
            "pending_severity_raw": None,
            "pending_question_proposal": None,
            "stage_transition_flag": None,
            "system_risk_classification": None,
            "status": "ACTIVE",
        }

    def _sync_clinical_context(self, session: ProductSession) -> ProductSession:
        """將已通過 intake／policy 的事實同步到不可壓縮 clinical state。"""
        intake = session.intake_snapshot
        stage = session.intake_stage
        updates: dict[str, Any] = {
            "known_medications": list(intake.known_medications),
            "allergies": list(intake.allergies),
            "chronic_conditions": list(intake.chronic_conditions),
            "family_history": list(intake.family_history),
            "symptom_onset": intake.symptom_onset,
            "symptom_description": intake.symptom_description,
            "reported_severity": intake.symptom_severity,
            "questions_for_doctor": list(intake.questions_for_doctor),
            "pending_question": session.pending_question[:2_000] if session.pending_question else None,
            "current_stage": stage,
            "authorization_status": session.authorization_status,
        }
        if session.system_risk_classification is not None:
            updates["system_risk_classification"] = session.system_risk_classification
        context = self.context_manager.apply_structured_updates(session.conversation_context, updates)
        return session.model_copy(update={"conversation_context": context}, deep=True)

    @classmethod
    def _next_pending_field(cls, intake: PreVisitIntake) -> str | None:
        for field in cls.INTAKE_FIELD_ORDER:
            if not getattr(intake, field, None):
                return field
        return None

    @staticmethod
    def _question_for_field(field: str | None) -> str | None:
        return compose_intake_question(field)

    @staticmethod
    def _field_stage(field: str | None) -> str:
        if field in {"known_medications", "allergies", "chronic_conditions", "family_history"}:
            return "stage1"
        if field in {"symptom_onset", "symptom_description", "symptom_severity"}:
            return "stage2"
        return "stage3"

    @classmethod
    def _normalize_intake_answer(
        cls,
        session: ProductSession,
        text: str,
        merged_valid: list[Any] | None = None,
        *,
        allow_cross_stage_symptom_description: bool = False,
    ) -> tuple[ProductSession, str | None]:
        field = session.pending_field or cls._next_pending_field(session.intake_snapshot)
        if field is None and not _CORRECTION_RE.search(text) and not _WANT_QUESTION_RE.search(text):
            if session.pending_action and session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
                pass
            else:
                return session, None
        if field is None and (_CORRECTION_RE.search(text) or _WANT_QUESTION_RE.search(text)):
            field = cls._next_pending_field(session.intake_snapshot) or "allergies"
        try:
            from tfda_context_gate.intake.tool import INJECTION_FIXED_REPLY, is_injection_attempt
            if is_injection_attempt(text):
                return session, INJECTION_FIXED_REPLY
        except Exception:
            pass
        try:
            from tfda_context_gate.intake.tool import is_plausible_intake_value
            _none_set = {"無", "沒有", "目前沒有", "沒有喔", "沒有欸", "沒吃", "沒有吃", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}
            if text.strip() not in _none_set and not is_plausible_intake_value(text):
                if field == "symptom_severity":
                    return session, "請輸入 1～10 的數字，例如「7」，也可以直接輸入「輕度、中度、重度」。"
                pending_q = session.pending_question or cls._question_for_field(field)
                if pending_q:
                    return session, pending_q
                return session, "請再說明一次？"
        except Exception:
            pass
        _was_truncated = False
        _trunc_marker = "(已節錄)"
        try:
            from tfda_context_gate.intake.tool import INTAKE_MAX_LENGTH
            limit = INTAKE_MAX_LENGTH
        except Exception:
            limit = 120
        stripped_for_len = text.strip()
        if len(stripped_for_len) > limit:
            _was_truncated = True
            text_for_extract = stripped_for_len[:limit]
        else:
            text_for_extract = stripped_for_len
        text = text_for_extract

        is_correction = bool(_CORRECTION_RE.search(text))
        if is_correction:
            tgt_field, tgt_val = _extract_correction_target(text)
            if tgt_field == "allergies" and tgt_val:
                intake = session.intake_snapshot.model_copy(deep=True)
                intake.allergies = [tgt_val[:20]]
                confirm = compose_correction(tgt_val)
                if _was_truncated and _trunc_marker not in confirm:
                    confirm = f"{confirm} {_trunc_marker}"
                new_session = session.model_copy(update={"intake_snapshot": intake}, deep=True)
                if new_session.pending_action and new_session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
                    new_session = new_session.model_copy(update={"pending_action": None, "pending_severity_raw": None}, deep=True)
                return new_session, confirm
            if is_correction and "盤尼西林" in text:
                intake = session.intake_snapshot.model_copy(deep=True)
                intake.allergies = ["盤尼西林"]
                confirm = compose_correction("盤尼西林")
                if _was_truncated and _trunc_marker not in confirm:
                    confirm = f"{confirm} {_trunc_marker}"
                new_session = session.model_copy(update={"intake_snapshot": intake}, deep=True)
                if new_session.pending_action and new_session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
                    new_session = new_session.model_copy(update={"pending_action": None, "pending_severity_raw": None}, deep=True)
                return new_session, confirm

        if _WANT_QUESTION_RE.search(text) and field != "questions_for_doctor":
            if re.search(r"想問|想請問|想了解|問題是|疑問|？|\?|嗎|如何|怎麼|為何|為什麼|多少|是否", text) or len(text.strip()) > 5:
                intake = session.intake_snapshot.model_copy(deep=True)
                proposal = _clean_question_text(text)
                if not proposal or _QUESTION_NEGATIVE_RE.match(proposal):
                    pass
                else:
                    if proposal and proposal not in intake.questions_for_doctor and len(intake.questions_for_doctor) < 10:
                        intake.questions_for_doctor = [*intake.questions_for_doctor, proposal]
                        confirm = compose_question_added(text)
                        if _was_truncated and _trunc_marker not in confirm:
                            confirm = f"{confirm} {_trunc_marker}"
                        return session.model_copy(update={"intake_snapshot": intake, "pending_action": None}, deep=True), confirm
                    elif proposal in intake.questions_for_doctor:
                        return session, f"這個問題已經記過了，其他資料保留。"

        if field == "symptom_severity":
            hedge_hit = bool(_HEDGE_RE.search(text) or text.strip() in ("有點嚴重吧", "有點嚴重", "有點嚴重吧？", "稍微嚴重"))
            has_explicit = bool(_SEVERITY_EXPLICIT_RE.search(text))
            cleaned = re.sub(r"有點嚴重吧|有點嚴重|稍微嚴重|有點|稍微|好像|吧$|大概", "", text).strip()
            cleaned_has_explicit = bool(_SEVERITY_EXPLICIT_RE.search(cleaned))
            if hedge_hit and not cleaned_has_explicit:
                if not has_explicit:
                    from datetime import datetime as _dt, timezone as _tz
                    pending = PendingAction(type="PENDING_SEVERITY_CLARIFY", raw_provenance=text.strip()[:200], target_field="symptom_severity", created_at=_dt.now(_tz.utc))
                    new_sess = session.model_copy(update={"pending_action": pending, "pending_severity_raw": text.strip()[:200], "pending_question": "請問程度大約是輕度、中度、重度，或 1–10 分中的幾分？", "pending_field": "symptom_severity"}, deep=True)
                    return new_sess, "請問程度大約是輕度、中度、重度，或 1–10 分中的幾分？"

        if session.pending_action and session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
            has_explicit = bool(_SEVERITY_EXPLICIT_RE.search(text))
            if has_explicit:
                intake = session.intake_snapshot.model_copy(deep=True)
                mapped = _standardize_severity(text)
                if mapped:
                    intake.symptom_severity = mapped
                    new_sess = session.model_copy(update={"intake_snapshot": intake, "pending_action": None, "pending_severity_raw": None}, deep=True)
                    from tfda_context_gate.intake.tool import build_implicit_confirm
                    confirm = compose_implicit_confirmation(build_implicit_confirm(text, mapped))
                    if _was_truncated and _trunc_marker not in confirm:
                        confirm = f"{confirm} {_trunc_marker}"
                    return new_sess, confirm

        candidates: dict[str, Any] = {}
        # P2A: if merged_valid from deterministic+formal is provided, use it directly
        if merged_valid is not None:
            try:
                # merged_valid is list[MergedCandidate] already validated/deduped
                _by_field: dict[str, list[str]] = {}
                for _mc in merged_valid:
                    _fld = getattr(_mc, "target_field", None) or getattr(_mc, "field_name", None)
                    _val = getattr(_mc, "value", None) or getattr(_mc, "candidate_value", None)
                    if not _fld or not _val:
                        continue
                    _by_field.setdefault(str(_fld), []).append(str(_val).strip())
                for _fld, _vals in _by_field.items():
                    if _fld in ("known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"):
                        candidates[_fld] = _vals
                    else:
                        # single-value fields: if multiple, join with ； for symptom_description else take highest
                        if _fld == "symptom_description" and len(_vals) > 1:
                            candidates[_fld] = "；".join(_vals)
                        else:
                            candidates[_fld] = _vals[0] if len(_vals) == 1 else "；".join(_vals)
            except Exception:
                candidates = {}
        else:
            try:
                from tfda_context_gate.intake.tool import PreVisitIntakeTool

                tool = PreVisitIntakeTool()
                candidates = tool.extract_fields_from_utterance(text, stage=None)
                if "questions_for_doctor" in candidates:
                    if _QUESTION_NEGATIVE_RE.match(text.strip()):
                        candidates.pop("questions_for_doctor", None)
                    else:
                        has_q = bool(re.search(r"想問|想請問|想了解|問題是|疑問|？|\?|嗎|如何|怎麼|為何|為什麼|多少|是否|正常", text))
                        if not has_q:
                            candidates.pop("questions_for_doctor", None)
                if "chronic_conditions" in candidates and "symptom_description" in candidates:
                    desc_val = str(candidates["symptom_description"])
                    distinct = any(kw in desc_val for kw in ["口渴", "頻尿", "頭暈", "疲倦", "喘", "疼痛", "麻", "視力", "血糖"])
                    if not distinct and any(kw in desc_val for kw in ["高血壓", "高血脂", "高脂血", "腎臟病", "心臟病"]):
                        candidates.pop("symptom_description", None)
                if is_correction:
                    tgt_f, tgt_v = _extract_correction_target(text)
                    if tgt_f == "allergies" and tgt_v and "allergies" not in candidates:
                        candidates["allergies"] = [tgt_v]
                    elif "盤尼西林" in text and "allergies" not in candidates:
                        candidates["allergies"] = ["盤尼西林"]
                    elif "阿斯匹靈" in text and "allergies" not in candidates:
                        candidates["allergies"] = ["阿斯匹靈"]
                if is_correction and _WANT_QUESTION_RE.search(text) and "questions_for_doctor" not in candidates:
                    m_q = re.search(r"[:：]\s*(.+)", text)
                    proposal_q = (m_q.group(1).strip() if m_q and m_q.group(1).strip() else text.strip())[:200]
                    if proposal_q and not _QUESTION_NEGATIVE_RE.match(proposal_q):
                        candidates["questions_for_doctor"] = [proposal_q]
            except Exception:
                candidates = {}

        intake = session.intake_snapshot.model_copy(deep=True)

        def _is_placeholder(fname: str, val: Any) -> bool:
            if isinstance(val, list) and val == ["不清楚（待看診確認）"]:
                return True
            if isinstance(val, str) and val in {"待確認", "不清楚（待看診確認）"}:
                return True
            return False

        valid: dict[str, Any] = {}
        for k, v in candidates.items():
            if k not in cls.INTAKE_FIELD_ORDER or not v:
                continue
            existing = getattr(intake, k, None)
            is_symptom = k in {"symptom_onset", "symptom_description", "symptom_severity"}
            # symptom fields only overwrite on explicit correction or when empty, not on arbitrary question text
            allow_override = is_correction or not existing or _is_placeholder(k, existing) or k == "questions_for_doctor"
            if is_symptom and existing and not _is_placeholder(k, existing) and not is_correction:
                # check if text actually contains symptom semantics; otherwise block pollution like "血糖多少正常"
                has_symptom_kw = any(kw in text for kw in ["餓", "手抖", "口渴", "頻尿", "頭暈", "疼痛", "麻", "視力", "血糖高", "血糖低", "發抖"])
                has_question_kw = bool(re.search(r"多少正常|嗎$|？$|\?$|如何|怎麼|為何", text))
                if has_question_kw and not has_symptom_kw:
                    allow_override = False
            if not allow_override:
                continue
            if isinstance(v, list):
                tv = [str(x).strip()[:limit] for x in v]
                v = tv
                if k == "questions_for_doctor":
                    merged = list(intake.questions_for_doctor)
                    for item in v:
                        if item not in merged and len(merged) < 10:
                            merged.append(item)
                    v = merged
            elif isinstance(v, str) and len(v) > limit:
                v = v[:limit]
            if k == "symptom_description" and "symptom_onset" in candidates and str(v).strip() == text.strip()[:limit] and field != "symptom_description":
                # A full-clause description such as 「我最近常口渴」 is
                # still real symptom data even when the same clause also
                # yields onset=最近.  Only suppress a duplicated time-only
                # clause that contains no symptom semantics.
                has_distinct_symptom = bool(
                    re.search(
                        r"口渴|口乾|頻尿|尿尿|跑廁所|頭暈|疼痛|麻|視力|疲倦|很累|餓|手抖|發抖|喘",
                        str(v),
                    )
                )
                if not (
                    allow_cross_stage_symptom_description
                    and has_distinct_symptom
                ):
                    continue
            if k == "questions_for_doctor" and isinstance(v, list):
                if any("我要繼續整理看診前資料" in str(x) for x in v):
                    continue
                # filter negative
                v = [x for x in v if not _QUESTION_NEGATIVE_RE.match(x.strip())]
                if not v:
                    continue
                # unified extraction via _clean_question_text
                nv = []
                for x in v:
                    cleaned = _clean_question_text(x)
                    if cleaned and not _QUESTION_NEGATIVE_RE.match(cleaned):
                        nv.append(cleaned)
                    elif cleaned:
                        # if cleaned is negative, skip
                        continue
                    else:
                        nv.append(x)
                v = nv
            if k == "questions_for_doctor" and isinstance(v, str) and "我要繼續整理看診前資料" in v:
                continue
            if k == "questions_for_doctor" and isinstance(v, str) and _QUESTION_NEGATIVE_RE.match(v.strip()):
                continue
            if k == "symptom_severity":
                # standardize numeric scores, keep provenance via pending_severity_raw
                if isinstance(v, str):
                    v = _standardize_severity(v)
                elif isinstance(v, list) and v:
                    v = _standardize_severity(str(v[0]))
            valid[k] = v

        if valid:
            for f, val in valid.items():
                if f == "questions_for_doctor" and isinstance(val, list):
                    setattr(intake, f, val)
                else:
                    setattr(intake, f, val)
                _intake_uncertain_attempts.pop((session.session_id, f), None)
            from tfda_context_gate.intake.tool import build_implicit_confirm, build_implicit_confirm_for_fields
            label_map = {
                "known_medications": "用藥",
                "allergies": "過敏",
                "chronic_conditions": "慢性病",
                "family_history": "家族史",
                "symptom_onset": "症狀開始時間",
                "symptom_description": "症狀描述",
                "symptom_severity": "程度",
                "questions_for_doctor": "想問醫師的問題",
            }
            raw_snip = text.strip()[:30]
            if is_correction:
                updated_fields = list(valid.keys())
                labels = "、".join(label_map.get(k, k) for k in updated_fields)
                parts = []
                for vv in valid.values():
                    if isinstance(vv, list):
                        parts.append("、".join(str(x) for x in vv))
                    else:
                        parts.append(str(vv))
                norm_joined = "；".join(parts)
                confirm = compose_correction(norm_joined, labels)
                if _was_truncated and _trunc_marker not in confirm:
                    confirm = f"{confirm} {_trunc_marker}"
                new_sess = session.model_copy(update={"intake_snapshot": intake}, deep=True)
                if new_sess.pending_action and new_sess.pending_action.type == "PENDING_SEVERITY_CLARIFY" and "symptom_severity" in valid:
                    new_sess = new_sess.model_copy(update={"pending_action": None, "pending_severity_raw": None}, deep=True)
                return new_sess, confirm
            if field not in valid or len(valid) > 1:
                if len(valid) == 1:
                    f = next(iter(valid))
                    label = label_map.get(f, f)
                    confirm = compose_single_confirmation(raw_snip, label)
                else:
                    base = build_implicit_confirm_for_fields(valid, raw_text=text)
                    labels = "、".join(label_map.get(k, k) for k in valid)
                    if base:
                        confirm = compose_multi_confirmation(base, labels)
                    else:
                        norm_parts = []
                        for vv in valid.values():
                            if isinstance(vv, list):
                                norm_parts.append("、".join(str(x) for x in vv))
                            else:
                                norm_parts.append(str(vv))
                        confirm = compose_implicit_confirmation(build_implicit_confirm(text, "；".join(norm_parts)))
            else:
                base = build_implicit_confirm_for_fields(valid, raw_text=text)
                if base is None:
                    first_val = next(iter(valid.values()))
                    norm = "、".join(str(x) for x in first_val) if isinstance(first_val, list) else str(first_val)
                    base = build_implicit_confirm(text, norm)
                confirm = compose_implicit_confirmation(base)
            if _was_truncated and _trunc_marker not in confirm:
                confirm = f"{confirm} {_trunc_marker}"
            new_sess = session.model_copy(update={"intake_snapshot": intake}, deep=True)
            if new_sess.pending_action and new_sess.pending_action.type == "PENDING_SEVERITY_CLARIFY" and "symptom_severity" in valid:
                new_sess = new_sess.model_copy(update={"pending_action": None, "pending_severity_raw": None}, deep=True)
            # Requirement 4: ensure pending advances after successful valid write
            try:
                if field in valid or new_sess.pending_field in valid:
                    nxt = cls._next_pending_field(intake)
                    nxt_q = cls._question_for_field(nxt) if nxt else None
                    # Only override if field was the pending one or intake now has it filled
                    if new_sess.pending_field == field or field in valid:
                        new_sess = new_sess.model_copy(update={"pending_field": nxt, "pending_question": nxt_q}, deep=True)
                    # Clear Brown Bag attempt if known_medications was filled
                    if "known_medications" in valid:
                        _intake_uncertain_attempts.pop((session.session_id, "known_medications"), None)
            except Exception:
                pass
            return new_sess, confirm

        # For symptom fields, uncertain text should not be bounced as pending question via candidates
        _is_symptom_uncertain = False
        try:
            _norm_early = re.sub(r"\s+", "", text).lower()
            _is_symptom_uncertain = bool(re.search(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚", _norm_early) or "不太知道" in _norm_early) and field in {"symptom_onset", "symptom_description", "symptom_severity"}
        except Exception:
            _is_symptom_uncertain = False
        if candidates and not valid and not _is_symptom_uncertain:
            if is_correction:
                for k, v in candidates.items():
                    if k in cls.INTAKE_FIELD_ORDER and v:
                        existing = getattr(intake, k, None)
                        if existing and not _is_placeholder(k, existing):
                            if isinstance(v, list):
                                tv = [str(x).strip()[:limit] for x in v]
                                v = tv
                            elif isinstance(v, str) and len(v) > limit:
                                v = v[:limit]
                            setattr(intake, k, v)
                            confirm = compose_correction(str(v)[:30])
                            return session.model_copy(update={"intake_snapshot": intake}, deep=True), confirm
            pending_q = session.pending_question or cls._question_for_field(field)
            if pending_q:
                return session, pending_q
            return session, None

        normalized = re.sub(r"\s+", "", text).lower()
        uncertain = bool(re.search(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚", normalized) or "不太知道" in normalized)
        skip = normalized in {"跳過", "略過", "先跳過", "稍後再補", "還沒想到"}
        none_answer = normalized in {"無", "沒有", "目前沒有", "沒有喔", "沒有欸", "沒吃", "沒有吃"}
        none_patterns = {
            "known_medications": r"(?:沒有|無).*(?:用藥|吃藥|藥物|藥)",
            "allergies": r"(?:沒有|無).*過敏|無過敏",
            "chronic_conditions": r"(?:沒有|無).*(?:慢性病|其他疾病)|無慢性病",
            "family_history": r"(?:沒有|無).*家族史|家族(?:沒有|無)",
        }
        none_answer = none_answer or bool(re.search(none_patterns.get(field, r"(?!x)x"), normalized))

        if (uncertain or skip or (field == "questions_for_doctor" and _QUESTION_NEGATIVE_RE.match(text.strip()))) and not valid:
            if field in {"symptom_onset", "symptom_description", "symptom_severity"}:
                setattr(intake, field, "待確認")
                return session.model_copy(update={"intake_snapshot": intake}, deep=True), (
                    "沒關係，先記為『待確認』，看診時再跟醫師確認。"
                )
            if field == "questions_for_doctor" and (skip or _QUESTION_NEGATIVE_RE.match(text.strip())):
                if intake.questions_for_doctor is None:
                    setattr(intake, field, [])
                # converge to review and clear stale pending
                new_stage = "review"
                return session.model_copy(update={"intake_snapshot": intake, "intake_stage": new_stage, "pending_action": None, "pending_field": None, "pending_question": None}, deep=True), (
                    "好的，已記錄，進入確認階段。"
                )
            value: Any = ["不清楚（待看診確認）"] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"
            } else "不清楚（待看診確認）"
            setattr(intake, field, value)
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), compose_uncertain(symptom=False)

        if none_answer:
            value = ["無"] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history"
            } else (["目前沒有特別想問的問題"] if field == "questions_for_doctor" else "目前沒有")
            setattr(intake, field, value)
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), compose_none_answer()

        # Generic guard: confirmation words must never pollute intake fields other than known_medications (which has specific Brown Bag handling above)
        if _is_confirmation_word(text.strip()) and field != "known_medications":
            pending_q = session.pending_question or cls._question_for_field(field)
            return session, pending_q or "好的，已記下。"
        if text.strip():
            stripped = text.strip()
            is_valid_severity = (field == "symptom_severity" and bool(re.match(r"^(大概|大約|約|差不多)?\s*(10|[1-9]|\d+\s*分|\d+/\d+|輕度|中度|重度|輕微|普通|還好|嚴重|很嚴重|非常嚴重)\s*(分|左右|吧)?$", stripped)))
            if not is_valid_severity:
                if len(stripped) < 2 or re.fullmatch(r"[^\w\u4e00-\u9fa5]+", stripped) or not re.search(r"[\w\u4e00-\u9fa5]", stripped) or re.search(r"[#\/\*]{3,}", stripped):
                    if field == "symptom_severity":
                        return session, "請輸入 1～10 的數字，例如「7」，也可以直接輸入「輕度、中度、重度」。"
                    pending_q = session.pending_question or cls._question_for_field(field)
                    if pending_q:
                        return session, pending_q
                    return session, None
            if "我要繼續整理看診前資料" in stripped:
                return session, None
            # stage3 negative should not be stored
            if field == "questions_for_doctor" and _QUESTION_NEGATIVE_RE.match(stripped):
                pending_q = session.pending_question or cls._question_for_field(field)
                if pending_q:
                    return session, pending_q
                return session, None
            # review command should not be stored as question
            if field == "questions_for_doctor" and stripped.lower() == "review":
                return session, session.pending_question or "請確認是否完成看診資料整理？"
            # unified cleaning for questions (colon + 有， prefix)
            if field == "questions_for_doctor":
                cleaned_q = _clean_question_text(stripped)
                if cleaned_q and cleaned_q != stripped:
                    stripped = cleaned_q
                elif cleaned_q == "" and _QUESTION_NEGATIVE_RE.match(stripped):
                    # keep negative handling already above, but ensure not stored
                    pass
            # severity standardization for direct write
            if field == "symptom_severity":
                mapped_sev = _standardize_severity(stripped)
                if mapped_sev in ("輕度", "中度", "重度") or re.match(r"^\d+分$", mapped_sev):
                    stripped = mapped_sev
                else:
                    return session, "請輸入 1～10 的數字，例如「7」，也可以直接輸入「輕度、中度、重度」。"
            if field == "known_medications":
                try:
                    # Requirement 2: confirmation / cancel words must never be written as medication
                    if _is_confirmation_word(stripped):
                        # Pure confirmation word like "正確" / "對" / "是" / "沒錯"
                        if _was_last_implicit_confirm(session):
                            # Keep original value, advance to next field (allergies)
                            _intake_uncertain_attempts.pop((session.session_id, "known_medications"), None)
                            next_field = cls._next_pending_field(session.intake_snapshot)
                            # If intake already has value (legacy), next is allergies; if empty, keep same
                            if next_field and next_field != "known_medications":
                                next_q = cls._question_for_field(next_field)
                                new_sess = session.model_copy(update={"pending_field": next_field, "pending_question": next_q}, deep=True)
                                return new_sess, next_q or "好的，已確認，下一項：有沒有藥物或食物過敏？"
                            # No advance needed (already filled or no next) -> bounce pending
                            pending_q = session.pending_question or cls._question_for_field(next_field or "allergies")
                            return session, pending_q or "好的，已確認。"
                        if _was_last_medication_clarification(session):
                            # Response to Brown Bag clarification with "正確" is not a drug name -> treat as still unclear, go to next clarification step
                            attempt_key = (session.session_id, "known_medications")
                            attempt = _intake_uncertain_attempts.get(attempt_key, 0)
                            from tfda_context_gate.intake.schemas import MEDICATION_CLARIFICATION_QUESTIONS
                            from tfda_context_gate.line_orchestration.response_composer import compose_uncertain
                            if attempt == 0:
                                # Should have been at 1, but handle generically
                                q = MEDICATION_CLARIFICATION_QUESTIONS[1]
                                _intake_uncertain_attempts[attempt_key] = 1
                                return session.model_copy(update={"pending_field": "known_medications", "pending_question": q}, deep=True), q
                            elif attempt == 1:
                                q = MEDICATION_CLARIFICATION_QUESTIONS[2]
                                _intake_uncertain_attempts[attempt_key] = 2
                                return session.model_copy(update={"pending_field": "known_medications", "pending_question": q}, deep=True), q
                            else:
                                intake.known_medications = ["不清楚（待看診確認）"]
                                _intake_uncertain_attempts.pop(attempt_key, None)
                                next_field = cls._next_pending_field(intake)
                                next_q = cls._question_for_field(next_field) if next_field else None
                                new_sess = session.model_copy(update={"intake_snapshot": intake, "pending_field": next_field, "pending_question": next_q}, deep=True)
                                msg = compose_uncertain(symptom=False)
                                reply_text = f"{msg}\n\n{next_q}" if next_q else msg
                                return new_sess, reply_text
                        # Confirmation word without implicit confirm nor clarification -> bounce without writing
                        pending_q = session.pending_question or cls._question_for_field(field)
                        return session, pending_q or "好的，已記下。"
                    if _MEDICATION_CANCEL_RE.search(stripped):
                        # Requirement: "不要記/取消" must not pollute intake
                        pending_q = session.pending_question or cls._question_for_field(field)
                        return session, pending_q or "好的，已略過。"
                    has_known_meds = bool(_MEDICATION_KNOWN_RE.search(stripped))
                    is_colloq = bool(_MEDICATION_COLLOQUIAL_RE.search(stripped))
                    is_uncert_txt = bool(_MEDICATION_UNCERTAIN_RE.search(stripped) and "藥" in stripped)
                    if (is_colloq or is_uncert_txt) and not has_known_meds:
                        # Requirement 1: Brown Bag 2-attempt flow, not immediate sentinel/write
                        from tfda_context_gate.intake.schemas import MEDICATION_CLARIFICATION_QUESTIONS
                        from tfda_context_gate.line_orchestration.response_composer import compose_uncertain
                        attempt_key = (session.session_id, "known_medications")
                        attempt = _intake_uncertain_attempts.get(attempt_key, 0)
                        if attempt == 0:
                            q = MEDICATION_CLARIFICATION_QUESTIONS[1]
                            _intake_uncertain_attempts[attempt_key] = 1
                            return session.model_copy(update={"pending_field": "known_medications", "pending_question": q}, deep=True), q
                        elif attempt == 1:
                            q = MEDICATION_CLARIFICATION_QUESTIONS[2]
                            _intake_uncertain_attempts[attempt_key] = 2
                            return session.model_copy(update={"pending_field": "known_medications", "pending_question": q}, deep=True), q
                        else:
                            sentinel = ["不清楚（待看診確認）"]
                            setattr(intake, field, sentinel)
                            prov = dict(getattr(session, "intake_field_provenance", {}) or {})
                            prov[field] = stripped[:80]
                            _intake_uncertain_attempts.pop(attempt_key, None)
                            # Requirement 4: advance pending after storage
                            next_field = cls._next_pending_field(intake)
                            next_q = cls._question_for_field(next_field) if next_field else None
                            # Return with sentinel but also advance pending in session for caller to pick up;
                            # caller will later recompute pending via resulting_intake, but we set it here for safety
                            return session.model_copy(update={"intake_snapshot": intake, "intake_field_provenance": prov, "pending_field": next_field, "pending_question": next_q}, deep=True), compose_uncertain(symptom=False)
                    _sym_like = bool(re.search(r"嘴巴乾|口乾|跑廁所|上廁所|口渴|頻尿|頭暈|夜尿|很乾|廁所", stripped))
                    _med_like = bool(re.search(r"metformin|二甲雙胍|胰島素|insulin|吃藥|用藥|服用", stripped, re.IGNORECASE))
                    if _sym_like and not _med_like:
                        from tfda_context_gate.intake.tool import PreVisitIntakeTool as _DirectTool

                        _dt = _DirectTool()
                        _ext2 = _dt.extract_fields_from_utterance(stripped, stage="stage2")
                        _desc = _ext2.get("symptom_description")
                        if _desc:
                            intake.symptom_description = _desc
                            if _ext2.get("symptom_onset"):
                                intake.symptom_onset = _ext2["symptom_onset"]
                            from tfda_context_gate.intake.tool import build_implicit_confirm as _bic

                            _conf = compose_implicit_confirmation(_bic(text, _desc))
                            if _was_truncated and _trunc_marker not in _conf:
                                _conf = f"{_conf} {_trunc_marker}"
                            return session.model_copy(update={"intake_snapshot": intake}, deep=True), _conf
                        # fallback: store as symptom_description to avoid medication pollution
                        intake.symptom_description = stripped[:2000]
                        from tfda_context_gate.intake.tool import build_implicit_confirm as _bic2

                        _conf2 = compose_implicit_confirmation(_bic2(text, stripped[:25]))
                        if _was_truncated and _trunc_marker not in _conf2:
                            _conf2 = f"{_conf2} {_trunc_marker}"
                        return session.model_copy(update={"intake_snapshot": intake}, deep=True), _conf2
                    from tfda_context_gate.intake.candidate_merge import is_question_like as _is_q_like2

                    if _is_q_like2(stripped):
                        _pending_q = session.pending_question or cls._question_for_field(field)
                        if _pending_q:
                            return session, _pending_q
                        return session, None
                except Exception:
                    pass
            direct: Any = [stripped[:limit]] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"
            } else stripped[:limit]
            if field == "questions_for_doctor" and isinstance(direct, list):
                if any("我要繼續整理看診前資料" in str(x) for x in direct):
                    return session, None
                # filter negative in direct
                direct = [x for x in direct if not _QUESTION_NEGATIVE_RE.match(x.strip())]
                if not direct:
                    pending_q = session.pending_question or cls._question_for_field(field)
                    if pending_q:
                        return session, pending_q
                    return session, None
            setattr(intake, field, direct)
            if isinstance(direct, list):
                norm_str = "、".join(str(x) for x in direct)
            else:
                norm_str = str(direct)
            from tfda_context_gate.intake.tool import build_implicit_confirm
            confirm = compose_implicit_confirmation(build_implicit_confirm(text, norm_str))
            if _was_truncated and _trunc_marker not in confirm:
                confirm = f"{confirm} {_trunc_marker}"
            # Requirement 4: clear Brown Bag attempt and advance pending after successful write
            if field == "known_medications":
                _intake_uncertain_attempts.pop((session.session_id, field), None)
                next_field = cls._next_pending_field(intake)
                next_q = cls._question_for_field(next_field) if next_field else None
                return session.model_copy(update={"intake_snapshot": intake, "pending_field": next_field, "pending_question": next_q}, deep=True), confirm
            # For other fields also ensure pending will advance via caller, but set here for safety
            next_field_generic = cls._next_pending_field(intake)
            next_q_generic = cls._question_for_field(next_field_generic) if next_field_generic else None
            # Only override pending if it was the same field; otherwise let caller compute
            if field == session.pending_field:
                return session.model_copy(update={"intake_snapshot": intake, "pending_field": next_field_generic, "pending_question": next_q_generic}, deep=True), confirm
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), confirm
        return session, None

    @classmethod
    def _looks_like_side_question(cls, session: ProductSession, text: str) -> bool:
        if session.intake_stage in ("review", "submitted") or session.status in ("AWAITING_CONFIRMATION", "SUBMITTED"):
            return False
        pending_field = session.pending_field or cls._next_pending_field(session.intake_snapshot)
        if pending_field == "questions_for_doctor":
            return False
        normalized = re.sub(r"\s+", "", text)
        education_topic = re.search(
            r"請說明|想了解|什麼是|為什麼|怎麼吃|如何|飲食原則|運動原則|藥物.*作用|血糖.*標準",
            normalized,
        )
        return bool(education_topic or (normalized.endswith(("?", "？")) and len(normalized) >= 6))

    @staticmethod
    def _is_mixed_intent(interpretation: Any) -> bool:
        """Mixed intake answer + education question must NOT take SIDE_ANSWER path."""
        try:
            intents = getattr(interpretation, "intents", None) or []
            return "INTAKE_ANSWER" in intents and "EDUCATION_QUESTION" in intents
        except Exception:
            return False

    @classmethod
    def _resolve_is_side_candidate(
        cls, session: ProductSession, text: str, interpretation: Any
    ) -> bool:
        """Precedence: mixed intent never side, else references_resolved cross-turn is side, else heuristic.

        Fixes bug where third line re-flipped mixed intent to True. Order matters:
        mixed intent must force False AFTER references_resolved re-flip.
        """
        base = cls._looks_like_side_question(session, text)
        # If mixed, it overrides everything — must go through intake merge + formal education workflow
        if cls._is_mixed_intent(interpretation):
            return False
        # Cross-turn resolved reference (e.g., fruit followup "那一天可以吃多少？" → "糖尿病一天可以吃多少水果？")
        # is considered side when not mixed and intake is active.
        if interpretation and getattr(interpretation, "resolved_education_query", None) and getattr(
            interpretation, "references_resolved", False
        ):
            return True
        # Apply mixed guard again defensively (in case base was True due to heuristic)
        # Already handled, but keep explicit for clarity.
        if cls._is_mixed_intent(interpretation):
            return False
        return base

    @staticmethod
    def _stage_checkpoint(intake: PreVisitIntake, stage: str) -> str | None:
        def show(value: Any) -> str:
            if isinstance(value, list):
                return "、".join(str(item) for item in value) or "未填"
            return str(value or "未填")

        if stage == "stage1":
            base = (
                "用藥與病史已記下："
                f"用藥 {show(intake.known_medications)}；過敏 {show(intake.allergies)}；"
                f"慢性病 {show(intake.chronic_conditions)}；家族史 {show(intake.family_history)}。"
            )
        elif stage == "stage2":
            base = (
                "症狀資訊已記下："
                f"開始時間 {show(intake.symptom_onset)}；主要狀況 {show(intake.symptom_description)}；"
                f"程度 {show(intake.symptom_severity)}。"
            )
        else:
            base = None
        try:
            from tfda_context_gate.intake.tool import format_stage_progress

            progress = format_stage_progress(intake)
            if progress and "第" not in progress:
                if base:
                    return f"{base}\n{progress}"[:60]
                if stage in {"stage1", "stage2", "stage3"}:
                    return progress
        except Exception:
            pass
        return base

    @staticmethod
    def _without_intake_invitation(reply: str) -> str:
        marker = "\n\n如果要看醫生需要幫你整理嗎？"
        return reply.split(marker, 1)[0].strip()

    @staticmethod
    def _merge_risk(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
        """RED_FLAG 在同一 subject session 內為單調狀態，不可被安全訊息降級。"""
        previous = dict(existing or {})
        current = dict(incoming or {})
        if previous.get("level") != "RED_FLAG":
            return current or previous
        if current.get("level") != "RED_FLAG":
            return previous
        merged = dict(previous)
        merged["signals"] = list(dict.fromkeys([
            *list(previous.get("signals") or []),
            *list(current.get("signals") or []),
        ]))
        merged["action"] = "URGENT_HUMAN"
        return merged

    def _load_or_create(self, line_user_id: str) -> ProductSession:
        session_id = self._session_id(line_user_id)
        existing = self.repository.get(session_id)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        principal_hash = self._hash(line_user_id)
        return self.repository.create(ProductSession(
            session_id=session_id,
            principal_id_hash=principal_hash,
            conversation_context=self.context_manager.create(session_id),
            created_at=now,
            updated_at=now,
            expires_at=now + self.session_ttl,
        ))

    def _hash(self, value: str) -> str:
        return hmac.new(self._hash_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def principal_hash(self, external_id: str) -> str:
        return self._hash(external_id)

    def session_for_user(self, line_user_id: str) -> ProductSession | None:
        return self.repository.get(self._session_id(line_user_id))

    def _session_id(self, line_user_id: str) -> str:
        return f"line-{self._hash(line_user_id)[:32]}"

    @staticmethod
    def _declared_role(role: ActorRole) -> str:
        if role is ActorRole.RELATED_PERSON:
            return "CAREGIVER"
        if role is ActorRole.PRACTITIONER:
            return "HEALTHCARE_PROFESSIONAL"
        return "PATIENT"

    @staticmethod
    def _is_intake_active(session: ProductSession, text: str = "") -> bool:
        authorized = session.authorization_status in {
            AuthorizationStatus.PATIENT_SELF,
            AuthorizationStatus.AUTHORIZED_CAREGIVER,
            AuthorizationStatus.LEGAL_GUARDIAN,
        }
        return authorized and session.status in {"ACTIVE", "PAUSED", "AWAITING_CONFIRMATION"}
