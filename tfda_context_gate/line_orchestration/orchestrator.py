from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

FORMAL_WORKFLOW_TIMEOUT_S = float(os.getenv("FORMAL_WORKFLOW_TIMEOUT_S", "45"))
SYNC_FORMAL_TIMEOUT_S = float(os.getenv("SYNC_FORMAL_TIMEOUT_S", os.getenv("SYNC_FORMAL_TIMEOUT", str(FORMAL_WORKFLOW_TIMEOUT_S))))
SYNC_FORMAL_TIMEOUT = SYNC_FORMAL_TIMEOUT_S
ASYNC_FORMAL_TIMEOUT_S = float(os.getenv("ASYNC_FORMAL_TIMEOUT_S", os.getenv("ASYNC_FORMAL_TIMEOUT", "120")))
ASYNC_FORMAL_TIMEOUT = ASYNC_FORMAL_TIMEOUT_S
LINE_USE_FORMAL_DEFAULT = os.getenv("LINE_USE_FORMAL", "true").lower() in ("1", "true", "yes")

# ── Async formal push: honest fallback + idempotency ─────────────────────────
HONEST_FALLBACK_TEXT = "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？"
QUEUED_FALLBACK_TEXT = "查詢排隊中，稍後推送"
HONEST_FALLBACK_REASONS = {"B_INSUFFICIENT", "FORMAL_TIMEOUT", "C_FAILURE", "SYSTEM_DEPENDENCY", "B_UNSAFE"}

# LINE educational narrow path (G) async: placeholder + background formal (120s + 1 retry)
# Service restart loss is acceptable (in-memory set only, documented).
ASYNC_PLACEHOLDER_REPLY = "查詢中，請稍候，資料整理完成後會推送給你 📋"
SYNC_FORMAL_TIMEOUT_S_ALIAS = SYNC_FORMAL_TIMEOUT_S
ASYNC_FORMAL_TIMEOUT_S_ALIAS = ASYNC_FORMAL_TIMEOUT_S

# In-memory idempotency for push per event (process-local). Repository webhook_events
# provides cross-process durability; this set prevents duplicate push within same process.
_pushed_events: set[str] = set()
_pushed_lock = threading.Lock()
# P3-R4 bounded concurrency for async formal: global semaphore limits concurrent
# formal background executions to 5; excess tasks block inside thread (FIFO queue)
# while placeholder reply returns immediately (<1s).
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

# ── P0 structured pending regexes ─────────────────────────────────────
_HEDGE_RE = re.compile(r"有點|稍微|好像|吧$|大概")
_SEVERITY_EXPLICIT_RE = re.compile(r"輕度|中度|重度|\d+\s*分|\d+/\d+|1–10|1-10")
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
    m = re.search(r"(\d+)\s*分", s)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 3:
                return "輕度"
            if 4 <= n <= 6:
                return "中度"
            if 7 <= n <= 10:
                return "重度"
        except Exception:
            pass
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
    if "輕度" in s:
        return "輕度"
    if "中度" in s:
        return "中度"
    if "重度" in s:
        return "重度"
    return s
def _extract_correction_target(text: str) -> tuple[str | None, str | None]:
    for field, pat in _FIELD_ALIAS_RE_MAP.items():
        if pat.search(text):
            m = re.search(r"改成\s*([^\s，,。；;]+)", text)
            if m:
                return field, m.group(1).strip().strip("，。")
            m2 = re.search(r"要改成\s*([^\s，,。；;]+)", text)
            if m2:
                return field, m2.group(1).strip().strip("，。")
            return field, None
    m = re.search(r"改成\s*([^\s，,。；;]+)", text)
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


class ConversationOrchestrator:
    """LINE 產品狀態編排；醫療決策仍完全交給固定 workflow。"""

    SELF_COMMANDS = {"為自己整理", "自己", "本人"}
    PROXY_COMMANDS = {"代家人整理", "幫家人整理", "家人"}
    PROXY_CONSENT_COMMANDS = {"已取得同意", "家人已同意", "同意"}
    CONFIRM_COMMANDS = {"確認", "確認完成", "提交"}
    START_INTAKE_COMMANDS = {"我要準備看診", "準備看診", "開始看診整理"}
    SHARE_COMMANDS = {"分享給醫護", "分享摘要"}
    SUMMARY_COMMANDS = {"查看看診摘要", "看診摘要", "查看摘要"}
    MODIFY_COMMANDS = {"修改看診資料", "修改資料"}
    PROXY_SUBJECT_SOURCE_COMMANDS = {"家人本人描述", "病患本人描述", "本人描述"}
    PROXY_OBSERVED_SOURCE_COMMANDS = {"我的觀察", "照護者觀察", "家屬觀察"}
    PAUSE_COMMANDS = {"暫停整理", "先不要填", "先不要填了", "等一下再填"}
    CANCEL_COMMANDS = {"不填了", "取消整理"}
    RESUME_COMMANDS = {"繼續整理", "繼續填寫", "回到看診整理", "接著填"}
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
            if env_val is not None:
                self.use_formal = env_val.lower() in ("1", "true", "yes")
            elif os.getenv("PYTEST_CURRENT_TEST") is not None:
                self.use_formal = False
            else:
                self.use_formal = LINE_USE_FORMAL_DEFAULT
        else:
            self.use_formal = use_formal
        # SYNC = 45 for direct run_workflow (tests), ASYNC = 120 for background LINE
        _sync_default = sync_formal_timeout_s if sync_formal_timeout_s is not None else formal_timeout_s if formal_timeout_s is not None else SYNC_FORMAL_TIMEOUT_S
        self.formal_timeout_s = _sync_default
        self.sync_formal_timeout_s = _sync_default
        self.async_formal_timeout_s = async_formal_timeout_s if async_formal_timeout_s is not None else ASYNC_FORMAL_TIMEOUT_S

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
        # Ensure formal path also respects timeout
        timeout = self.formal_timeout_s
        if timeout is None or timeout <= 0:
            return self.workflow_runner(*args, **kwargs)
        future_kwargs = dict(kwargs)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.workflow_runner, *args, **future_kwargs)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                # Formal timeout -> honest educational fallback (P3 15->45, not system error)
                _honest = "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？"
                return WorkflowResult(
                    request_id=str(args[0].get("request_id", "timeout")) if args and isinstance(args[0], dict) else "timeout",
                    status="FALLBACK",
                    final_response=_honest,
                    fallback_reason="FORMAL_TIMEOUT",
                    a_result=None,
                    query_expansion=None,
                    rag_result=None,
                    b_result=None,
                    c_result=None,
                    d_result=None,
                    agent_action=None,
                    agent_reason_code=None,
                    question=None,
                    current_query=None,
                    execution_history=[],
                    agent_steps=0,
                    rewrite_count=0,
                    clarification_count=0,
                    termination_reason="FORMAL_TIMEOUT",
                    intake_snapshot=None,
                    intake_stage=None,
                    previsit_summary=None,
                    system_risk_classification=None,
                    trace={"events": [], "evaluations": []},
                )

    def _call_workflow_async_with_retry(self, *args: Any, **kwargs: Any) -> WorkflowResult:
        if not self.use_formal:
            return self.workflow_runner(*args, **kwargs)
        try:
            _raw = args[0].get("user_raw_input") if args and isinstance(args[0], dict) else None
            _tt = kwargs.get("task_type")
            if _raw is not None and not _orch_should_use_formal(str(_raw), _tt):
                kwargs["use_formal"] = False
                return self.workflow_runner(*args, **kwargs)
        except Exception:
            pass
        kwargs["use_formal"] = True
        timeout = self.async_formal_timeout_s
        if timeout is None or timeout <= 0:
            return self.workflow_runner(*args, **kwargs)
        for attempt in range(2):
            future_kwargs = dict(kwargs)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.workflow_runner, *args, **future_kwargs)
                try:
                    return future.result(timeout=timeout)
                except FuturesTimeoutError:
                    if attempt == 0:
                        continue
                    _honest = HONEST_FALLBACK_TEXT
                    return WorkflowResult(
                        request_id=str(args[0].get("request_id", "timeout")) if args and isinstance(args[0], dict) else "timeout",
                        status="FALLBACK",
                        final_response=_honest,
                        fallback_reason="FORMAL_TIMEOUT",
                        a_result=None,
                        query_expansion=None,
                        rag_result=None,
                        b_result=None,
                        c_result=None,
                        d_result=None,
                        agent_action=None,
                        agent_reason_code=None,
                        question=None,
                        current_query=None,
                        execution_history=[],
                        agent_steps=0,
                        rewrite_count=0,
                        clarification_count=0,
                        termination_reason="FORMAL_TIMEOUT",
                        intake_snapshot=None,
                        intake_stage=None,
                        previsit_summary=None,
                        system_risk_classification=None,
                        trace={"events": [], "evaluations": []},
                    )
                except Exception:
                    if attempt == 0:
                        continue
                    _honest = HONEST_FALLBACK_TEXT
                    return WorkflowResult(
                        request_id=str(args[0].get("request_id", "timeout")) if args and isinstance(args[0], dict) else "timeout",
                        status="FALLBACK",
                        final_response=_honest,
                        fallback_reason="SYSTEM_DEPENDENCY",
                        a_result=None,
                        query_expansion=None,
                        rag_result=None,
                        b_result=None,
                        c_result=None,
                        d_result=None,
                        agent_action=None,
                        agent_reason_code=None,
                        question=None,
                        current_query=None,
                        execution_history=[],
                        agent_steps=0,
                        rewrite_count=0,
                        clarification_count=0,
                        termination_reason="SYSTEM_DEPENDENCY",
                        intake_snapshot=None,
                        intake_stage=None,
                        previsit_summary=None,
                        system_risk_classification=None,
                        trace={"events": [], "evaluations": []},
                    )
        _honest = HONEST_FALLBACK_TEXT
        return WorkflowResult(
            request_id=str(args[0].get("request_id", "async-timeout")) if args and isinstance(args[0], dict) else "async-timeout",
            status="FALLBACK",
            final_response=_honest,
            fallback_reason="FORMAL_TIMEOUT",
            a_result=None,
            query_expansion=None,
            rag_result=None,
            b_result=None,
            c_result=None,
            d_result=None,
            agent_action=None,
            agent_reason_code=None,
            question=None,
            current_query=None,
            execution_history=[],
            agent_steps=0,
            rewrite_count=0,
            clarification_count=0,
            termination_reason="FORMAL_TIMEOUT",
            intake_snapshot=None,
            intake_stage=None,
            previsit_summary=None,
            system_risk_classification=None,
            trace={"events": [], "evaluations": []},
        )

    def _is_duplicate_push(self, event_id: str) -> bool:
        with _pushed_lock:
            if event_id in _pushed_events:
                return True
        try:
            rec = self.repository.get_webhook_event(event_id)
            if rec is not None and rec.status == "COMPLETED" and isinstance(rec.result, dict) and rec.result.get("pushed"):
                return True
        except Exception:
            pass
        return False

    def _mark_pushed(self, event_id: str) -> None:
        with _pushed_lock:
            _pushed_events.add(event_id)

    def _push_with_retry(
        self,
        line_user_id: str,
        text: str,
        event_id: str | None = None,
        push_sender: PushSender | None = None,
    ) -> bool:
        if event_id and self._is_duplicate_push(event_id):
            return False
        for attempt in range(2):
            try:
                if push_sender is not None:
                    ok = push_sender(line_user_id, text)
                else:
                    ok = self._default_push_sender(line_user_id, text)
                if ok:
                    if event_id:
                        self._mark_pushed(event_id)
                        try:
                            rec = self.repository.get_webhook_event(event_id)
                            if rec is not None and rec.result is not None:
                                updated = dict(rec.result)
                                updated["pushed"] = True
                                pass
                        except Exception:
                            pass
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
                api.push_message(PushMessageRequest(to=line_user_id, messages=[TextMessage(text=text[:4900])]))
            return True
        except Exception as exc:
            logger.warning("default push failed: %s", exc)
            raise

    def _maybe_record_question_for_doctor(self, line_user_id: str, original_text: str, workflow: WorkflowResult) -> None:
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
        if self._is_intake_active(session, text):
            return False
        try:
            stripped = text.strip()
            if stripped in self.SELF_COMMANDS or stripped in self.PROXY_COMMANDS or stripped in self.PROXY_CONSENT_COMMANDS or stripped in self.CONFIRM_COMMANDS or stripped in self.START_INTAKE_COMMANDS or stripped in self.SHARE_COMMANDS or stripped in self.SUMMARY_COMMANDS or stripped in self.MODIFY_COMMANDS or stripped in self.PAUSE_COMMANDS or stripped in self.CANCEL_COMMANDS or stripped in self.RESUME_COMMANDS:
                return False
            if stripped in self.PROXY_SUBJECT_SOURCE_COMMANDS or stripped in self.PROXY_OBSERVED_SOURCE_COMMANDS:
                return False
        except Exception:
            pass
        try:
            return _orch_should_use_formal(text, None)
        except Exception:
            return False

    def _run_formal_with_timeout(self, text: str, session: ProductSession, timeout_s: float) -> WorkflowResult:
        request = {
            "request_id": f"{session.session_id}-async-{threading.get_ident() % 10000}",
            "schema_version": "a.v0.1",
            "user_raw_input": text,
            "declared_role": self._declared_role(session.actor_role),
            "language": "zh-TW",
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.workflow_runner, request, use_formal=True)
            return future.result(timeout=timeout_s)

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

        def _execute_and_push() -> None:
            workflow: WorkflowResult | None = None
            for attempt in range(2):
                try:
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
                    wf = self._run_formal_with_timeout(text, target_session, self.async_formal_timeout_s)
                    workflow = wf
                    break
                except FuturesTimeoutError:
                    logger.warning("async formal timeout attempt %s for %s", attempt + 1, event_id[:8])
                    if attempt == 1:
                        workflow = WorkflowResult(
                            request_id=event_id,
                            status="FALLBACK",
                            final_response=HONEST_FALLBACK_TEXT,
                            fallback_reason="FORMAL_TIMEOUT",
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
                            termination_reason="FORMAL_TIMEOUT",
                            intake_snapshot=None,
                            intake_stage=None,
                            previsit_summary=None,
                            system_risk_classification=None,
                            trace={"events": [], "evaluations": []},
                        )
                    else:
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("async formal error attempt %s for %s: %s", attempt + 1, event_id[:8], exc)
                    if attempt == 1:
                        workflow = WorkflowResult(
                            request_id=event_id,
                            status="FALLBACK",
                            final_response=HONEST_FALLBACK_TEXT,
                            fallback_reason="SYSTEM_DEPENDENCY",
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
                            termination_reason="SYSTEM_DEPENDENCY",
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
                    fallback_reason="SYSTEM_DEPENDENCY",
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
                    termination_reason="SYSTEM_DEPENDENCY",
                    intake_snapshot=None,
                    intake_stage=None,
                    previsit_summary=None,
                    system_risk_classification=None,
                    trace={"events": [], "evaluations": []},
                )
            if self._is_duplicate_push(event_id):
                return
            push_text = self.prepare_formal_push_text(workflow, text)
            ok = self._push_with_retry(line_user_id, push_text, event_id=event_id, push_sender=push_sender)
            if ok:
                try:
                    latest = self.repository.get(session_id)
                    if latest is not None:
                        ctx = self.context_manager.append_turn(latest.conversation_context, role="assistant", content=push_text)
                        ctx, _ = self.context_manager.compact(ctx, stage_completed=False)
                        updated = latest.model_copy(update={"conversation_context": ctx}, deep=True)
                        try:
                            self.repository.save(updated, expected_version=latest.version)
                        except ProductSessionConflict:
                            pass
                except Exception:
                    pass
                if _should_push_honest_fallback(workflow):
                    self._maybe_record_question_for_doctor(line_user_id, text, workflow)

        def _background() -> None:
            acquired = _FORMAL_SEMAPHORE.acquire(blocking=False)
            if not acquired:
                if self._is_duplicate_push(event_id):
                    return
                try:
                    self._push_with_retry(line_user_id, QUEUED_FALLBACK_TEXT, event_id=None, push_sender=push_sender)
                except Exception:
                    pass

                def _delayed() -> None:
                    with _FORMAL_SEMAPHORE:
                        if self._is_duplicate_push(event_id):
                            return
                        _execute_and_push()

                try:
                    threading.Thread(target=_delayed, daemon=True).start()
                except Exception:
                    pass
                return
            try:
                if self._is_duplicate_push(event_id):
                    return
                _execute_and_push()
            finally:
                try:
                    _FORMAL_SEMAPHORE.release()
                except Exception:
                    pass

        threading.Thread(target=_background, daemon=True).start()

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
            return OrchestratorResult.model_validate({**existing_event.result, "replayed": True})
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
                        status="ASYNC_PENDING",
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
                    event_id, result_placeholder.model_dump(mode="json"), claim_token=claim_token
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
            return OrchestratorResult.model_validate({**existing_event.result, "replayed": True})
        claim_token = self.repository.claim_webhook_event(event_id, principal_hash)
        if claim_token is None:
            return OrchestratorResult(event_id=event_id, session_id=self._session_id(line_user_id), reply="此圖片正在處理中，請稍候。", status="PROCESSING", replayed=True)
        try:
            session = self._load_or_create(line_user_id)
            previous_version = session.version
            if not self._is_intake_active(session):
                result = OrchestratorResult(event_id=event_id, session_id=session.session_id, reply="請先選擇「為自己整理」或「代家人整理」，再上傳藥袋。", status="NEEDS_AUTHORIZATION", intake_stage=session.intake_stage)
            else:
                workflow = self._call_workflow(
                    {"request_id": f"{session.session_id}-img-v{previous_version + 1}", "schema_version": "a.v0.1", "user_raw_input": "我上傳藥袋供看診前整理", "declared_role": self._declared_role(session.actor_role), "language": "zh-TW"},
                    task_type="pre_visit_intake",
                    intake=session.intake_snapshot,
                    image_bytes=image_bytes,
                    ocr_service=ocr_service,
                )
                updates: dict[str, Any] = {"pending_question": workflow.question}
                if workflow.intake_snapshot is not None:
                    updates["intake_snapshot"] = PreVisitIntake.model_validate(workflow.intake_snapshot)
                if workflow.intake_stage is not None:
                    updates["intake_stage"] = workflow.intake_stage
                next_intake = PreVisitIntake.model_validate(
                    workflow.intake_snapshot or session.intake_snapshot
                )
                updates["pending_field"] = self._next_pending_field(next_intake)
                if workflow.status == "NEEDS_CONFIRMATION":
                    updates["status"] = "AWAITING_CONFIRMATION"
                new_stage = updates.get("intake_stage", session.intake_stage)
                stage_completed = session.intake_stage != new_stage
                reply_text = workflow.final_response
                if stage_completed and session.intake_stage in {"stage1", "stage2"}:
                    try:
                        checkpoint = self._stage_checkpoint(next_intake, session.intake_stage)
                        if checkpoint:
                            reply_text = f"{checkpoint}\n\n{reply_text}"
                    except Exception:
                        pass
                context = self.context_manager.append_turn(session.conversation_context, role="user", content="［藥袋圖片］")
                context = self.context_manager.append_turn(context, role="assistant", content=reply_text)
                if stage_completed and session.intake_stage in {"stage1", "stage2", "stage3"}:
                    context = self.context_manager.mark_stage_completed(
                        context, session.intake_stage, next_stage=new_stage
                    )
                context, _ = self.context_manager.compact(context, stage_completed=stage_completed)
                updates["conversation_context"] = context
                session = session.model_copy(update=updates, deep=True)
                session = self._sync_clinical_context(session)
                saved = self.repository.save(session, expected_version=previous_version)
                result = OrchestratorResult(event_id=event_id, session_id=saved.session_id, reply=reply_text, status=workflow.status, intake_stage=workflow.intake_stage)
            self.repository.complete_webhook_event(
                event_id, result.model_dump(mode="json"), claim_token=claim_token
            )
            return result
        except Exception:
            self.repository.fail_webhook_event(event_id, claim_token=claim_token)
            raise

    def _process_text(self, session: ProductSession, text: str) -> OrchestratorResult:
        previous_version = session.version
        # 3: RiskSignalPolicy on current_message + existing monotonic risk state (before envelope/interpreter)
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
            # Still need to record user turn for trace before returning
            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
            session = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
            reply = fallback_response("A_EMERGENCY")
            session = self._sync_clinical_context(session)
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=False)
            saved = self.repository.save(
                session.model_copy(update={"conversation_context": context}, deep=True),
                expected_version=previous_version,
            )
            return OrchestratorResult(
                event_id="pending",
                session_id=saved.session_id,
                reply=reply,
                status="FALLBACK",
                intake_stage=saved.intake_stage,
            )

        # 5: Identity check (bounded, cold-start also, before envelope, not via LLM)
        if self._is_identity_question(text):
            ctx_user = self.context_manager.append_turn(session.conversation_context, role="user", content=text or "（空白訊息）")
            session = session.model_copy(update={"conversation_context": ctx_user}, deep=True)
            reply = "這是 AI 糖尿病衛教／看診前整理助理，不是真人客服，也不提供診斷。緊急狀況如呼吸困難、胸痛、意識不清等，請立即尋求緊急醫療協助（例如撥打 119）。"
            session = self._sync_clinical_context(session)
            ctx_assistant = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            ctx_assistant, _ = self.context_manager.compact(ctx_assistant, stage_completed=False)
            saved = self.repository.save(session.model_copy(update={"conversation_context": ctx_assistant}, deep=True), expected_version=previous_version)
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

        # 8-9: 建 ConversationEnvelope → Interpreter (always unless narrow fast-path)
        envelope = None
        interpretation = None
        if not _skip_ai_for_intake:
            try:
                envelope = build_conversation_envelope(session, text)
                self._last_envelope = envelope
                try:
                    interpretation = self.interpreter.interpret(envelope)
                except Exception:
                    interpretation = DeterministicConversationInterpreter().interpret(envelope)
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
                return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status="NEEDS_CLARIFICATION", intake_stage=saved.intake_stage)
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
            return OrchestratorResult(
                event_id="pending",
                session_id=saved.session_id,
                reply=reply,
                status="FALLBACK",
                intake_stage=saved.intake_stage,
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
            # P1: side question handling must respect interpreter multi-intent and cross-turn resolution
            is_side_candidate = self._looks_like_side_question(session, text)
            if interpretation and "INTAKE_ANSWER" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents:
                is_side_candidate = False
            if interpretation and interpretation.resolved_education_query and interpretation.references_resolved:
                is_side_candidate = True
            if self._is_intake_active(session, text) and is_side_candidate:
                side_query = text
                if interpretation and interpretation.resolved_education_query and interpretation.references_resolved:
                    side_query = interpretation.resolved_education_query
                elif interpretation and interpretation.resolved_education_query and "EDUCATION_QUESTION" in interpretation.intents:
                    side_query = interpretation.resolved_education_query
                workflow = self._call_workflow({
                    "request_id": f"{session.session_id}-side-v{previous_version + 1}",
                    "schema_version": "a.v0.1",
                    "user_raw_input": side_query,
                    "declared_role": self._declared_role(session.actor_role),
                    "language": "zh-TW",
                })
                reply = self._without_intake_invitation(workflow.final_response)
                pending_question = session.pending_question or self._question_for_field(
                    session.pending_field or self._next_pending_field(session.intake_snapshot)
                )
                if pending_question:
                    reply = (
                        f"{reply}\n\n資料已保留，想繼續可點「繼續整理」：\n{pending_question}"
                    )
                context = self.context_manager.append_turn(
                    session.conversation_context, role="assistant", content=reply
                )
                context, _ = self.context_manager.compact(context, stage_completed=False)
                saved = self.repository.save(
                    session.model_copy(update={"conversation_context": context}, deep=True),
                    expected_version=previous_version,
                )
                return OrchestratorResult(
                    event_id="pending", session_id=saved.session_id, reply=reply,
                    status="SIDE_ANSWER", intake_stage=saved.intake_stage,
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
            if is_control_or_chitchat and self._is_intake_active(session, text) and session.status in ("ACTIVE", "AWAITING_CONFIRMATION"):
                try:
                    from tfda_context_gate.intake.candidate_merge import is_multi_clause

                    _pending_for_control = session.pending_field or self._next_pending_field(session.intake_snapshot)
                    _is_symptom_like = bool(re.search(r"嘴巴|口乾|口渴|廁所|頻尿|頭暈|麻|視線|很累|疲倦|血糖|血壓", text))
                    if _pending_for_control in ("symptom_description", "symptom_onset", "symptom_severity", "known_medications", "allergies", "chronic_conditions", "family_history") and (_is_symptom_like or is_multi_clause(text)):
                        is_control_or_chitchat = False
                except Exception:
                    pass
            # P1 early multi detection for intake: avoid education being added as doctor question
            is_multi_early = interpretation and "INTAKE_ANSWER" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents
            intake_text_for_normalize = text
            if is_multi_early and interpretation and interpretation.resolved_education_query:
                edu_q = interpretation.resolved_education_query
                if edu_q and edu_q in text:
                    intake_text_for_normalize = text.replace(edu_q, "").strip("，,。 ")
                else:
                    parts = text.split("，")
                    for ep in [p for p in parts if "水果" in p or "可以吃" in p]:
                        intake_text_for_normalize = intake_text_for_normalize.replace(ep, "").strip("，,。 ")
                if not intake_text_for_normalize.strip():
                    intake_text_for_normalize = text
            if not is_control_or_chitchat and self._is_intake_active(session, intake_text_for_normalize) and session.status in ("ACTIVE", "AWAITING_CONFIRMATION"):
                _merged_valid: list[Any] | None = None
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
                        _valid, _need_clarify = merge_candidates(_det_cands, _formal_cands, existing_intake=session.intake_snapshot)
                        _merged_valid = _valid
                    except Exception:
                        _merged_valid = None
                session, intake_note = self._normalize_intake_answer(session, intake_text_for_normalize, merged_valid=_merged_valid)
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
            is_multi = interpretation and "INTAKE_ANSWER" in interpretation.intents and "EDUCATION_QUESTION" in interpretation.intents
            # P1: interpreter resolved education query should be used for A→RAG→B→C→D
            if interpretation and interpretation.resolved_education_query and "EDUCATION_QUESTION" in interpretation.intents:
                workflow_text = interpretation.resolved_education_query
            elif interpretation and interpretation.references_resolved and interpretation.resolved_education_query:
                workflow_text = interpretation.resolved_education_query
            if is_multi:
                # Multi-intent: intake already handled via _normalize_intake_answer, education via separate RAG workflow (no intake task)
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
                updates["intake_snapshot"] = PreVisitIntake.model_validate(workflow.intake_snapshot)
            if workflow.intake_stage is not None:
                updates["intake_stage"] = workflow.intake_stage
            resulting_intake = PreVisitIntake.model_validate(
                workflow.intake_snapshot or session.intake_snapshot
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
            if not resulting_intake.questions_for_doctor and updates.get("intake_stage") == "review":
                from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS as _QMAP
                updates["intake_stage"] = "stage3"
                updates["pending_field"] = "questions_for_doctor"
                updates["pending_question"] = _QMAP.get("questions_for_doctor")
            session = session.model_copy(update=updates, deep=True)
            # Intake write succeeded but workflow mis-routed short answers to Q_NEED_MORE/BLOCKED — override to stay in intake
            if intake_note is not None and workflow.status in ("BLOCKED", "FALLBACK") and workflow.fallback_reason in ("Q_NEED_MORE", "O_GENERIC", "CHIT_CHAT_OUT_OF_SCOPE", "B_INSUFFICIENT", "O_GENERIC"):
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
                    reply = f"{intake_note}\n\n{reply}"
            elif checkpoint:
                reply = f"{checkpoint}\n\n{reply}"
            # P1 multi: deterministically append next intake question after education answer
            if is_multi_multi and session.pending_field and session.pending_question:
                nxt_q = session.pending_question
                if nxt_q and nxt_q.strip() not in reply and workflow.status == "COMPLETED":
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
            saved = self.repository.save(session, expected_version=previous_version)
            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status=status, intake_stage=intake_stage)

        session = self._sync_clinical_context(session)
        context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
        context, _ = self.context_manager.compact(context, stage_completed=False)
        session = session.model_copy(update={"conversation_context": context}, deep=True)
        saved = self.repository.save(session, expected_version=previous_version)
        return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status=status, intake_stage=intake_stage)

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
        if session.status == "AWAITING_CONFIRMATION" and text in self.CONFIRM_COMMANDS:
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
        if field is None:
            return None
        from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS
        return INTAKE_FIELD_QUESTIONS.get(field)

    @staticmethod
    def _field_stage(field: str | None) -> str:
        if field in {"known_medications", "allergies", "chronic_conditions", "family_history"}:
            return "stage1"
        if field in {"symptom_onset", "symptom_description", "symptom_severity"}:
            return "stage2"
        return "stage3"

    @classmethod
    def _normalize_intake_answer(
        cls, session: ProductSession, text: str, merged_valid: list[Any] | None = None
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
            if not is_plausible_intake_value(text):
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
                confirm = f"已更新為「{tgt_val}」，其他已填資料保留。"
                if _was_truncated and _trunc_marker not in confirm:
                    confirm = f"{confirm} {_trunc_marker}"
                new_session = session.model_copy(update={"intake_snapshot": intake}, deep=True)
                if new_session.pending_action and new_session.pending_action.type == "PENDING_SEVERITY_CLARIFY":
                    new_session = new_session.model_copy(update={"pending_action": None, "pending_severity_raw": None}, deep=True)
                return new_session, confirm
            if is_correction and "盤尼西林" in text:
                intake = session.intake_snapshot.model_copy(deep=True)
                intake.allergies = ["盤尼西林"]
                confirm = f"已更新為「盤尼西林」，其他已填資料保留。"
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
                        confirm = f"你說的「{text.strip()[:30]}」我已幫你加到「想問醫師的問題」，其他資料保留。"
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
                    confirm = build_implicit_confirm(text, mapped)
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
            if k == "symptom_description" and "symptom_onset" in candidates and str(v).strip() == text.strip()[:limit]:
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
                confirm = f"已更新為「{norm_joined}」（{labels}），其他已填資料保留。"
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
                    confirm = f"你說的「{raw_snip}」我記在「{label}」"
                else:
                    base = build_implicit_confirm_for_fields(valid, raw_text=text)
                    labels = "、".join(label_map.get(k, k) for k in valid)
                    if base:
                        confirm = f"{base}（已分別記在「{labels}」）"
                    else:
                        norm_parts = []
                        for vv in valid.values():
                            if isinstance(vv, list):
                                norm_parts.append("、".join(str(x) for x in vv))
                            else:
                                norm_parts.append(str(vv))
                        confirm = build_implicit_confirm(text, "；".join(norm_parts))
            else:
                confirm = build_implicit_confirm_for_fields(valid, raw_text=text)
                if confirm is None:
                    first_val = next(iter(valid.values()))
                    norm = "、".join(str(x) for x in first_val) if isinstance(first_val, list) else str(first_val)
                    confirm = build_implicit_confirm(text, norm)
            if _was_truncated and _trunc_marker not in confirm:
                confirm = f"{confirm} {_trunc_marker}"
            new_sess = session.model_copy(update={"intake_snapshot": intake}, deep=True)
            if new_sess.pending_action and new_sess.pending_action.type == "PENDING_SEVERITY_CLARIFY" and "symptom_severity" in valid:
                new_sess = new_sess.model_copy(update={"pending_action": None, "pending_severity_raw": None}, deep=True)
            return new_sess, confirm

        if candidates and not valid:
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
                            confirm = f"已更新為「{str(v)[:30]}」，其他已填資料保留。"
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
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), (
                "沒關係，我先把這一項標成「待看診確認」，不會替你猜。"
            )

        if none_answer:
            value = ["無"] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history"
            } else (["目前沒有特別想問的問題"] if field == "questions_for_doctor" else "目前沒有")
            setattr(intake, field, value)
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), "好，已記下目前沒有。"

        if text.strip():
            stripped = text.strip()
            if len(stripped) < 2 or re.fullmatch(r"[^\w\u4e00-\u9fa5]+", stripped) or not re.search(r"[\w\u4e00-\u9fa5]", stripped) or re.search(r"[#\/\*]{3,}", stripped):
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
                stripped = _standardize_severity(stripped)
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
            confirm = build_implicit_confirm(text, norm_str)
            if _was_truncated and _trunc_marker not in confirm:
                confirm = f"{confirm} {_trunc_marker}"
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), confirm
        return session, None

    @classmethod
    def _looks_like_side_question(cls, session: ProductSession, text: str) -> bool:
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
