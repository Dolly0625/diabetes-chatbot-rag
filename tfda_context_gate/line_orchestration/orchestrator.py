from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
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
logger = logging.getLogger(__name__)


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
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.product_session import ProductSession, ProductSessionRepository
from tfda_context_gate.product_session import ProductSessionConflict
from tfda_context_gate.product_session import WebhookEventIdentityMismatch
from tfda_context_gate.workflow import run_workflow
from tfda_context_gate.workflow.schemas import WorkflowResult
from tfda_context_gate.workflow.fallbacks import fallback_response

from .schemas import OrchestratorResult


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
    PAUSE_COMMANDS = {"暫停整理", "先不要填"}
    CANCEL_COMMANDS = {"不填了", "取消整理"}
    RESUME_COMMANDS = {"繼續整理", "繼續填寫", "回到看診整理"}
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
            updated = intake.model_copy(update={"questions_for_doctor": [*intake.questions_for_doctor, q]}, deep=True)
            try:
                self.repository.save(session.model_copy(update={"intake_snapshot": updated}, deep=True), expected_version=session.version)
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

        def _background() -> None:
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
            if self._is_async_narrow_eligible(session, clean_text):
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
        context = self.context_manager.append_turn(
            session.conversation_context,
            role="user",
            content=text or "（空白訊息）",
        )
        session = session.model_copy(update={"conversation_context": context}, deep=True)

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

        # 一旦同一 subject 出現明確紅旗，後續產品命令也不得把它洗回一般狀態。
        if cumulative_risk.get("level") == "RED_FLAG":
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

        # 資料來源是臨床摘要的一部分，必須寫進結構化 state，不能只留在自由文字。
        if session.actor_role is ActorRole.RELATED_PERSON:
            if any(value in text for value in self.PROXY_SUBJECT_SOURCE_COMMANDS):
                session = session.model_copy(update={"information_source": InformationSource.SUBJECT_REPORTED_VIA_PROXY})
            elif any(value in text for value in self.PROXY_OBSERVED_SOURCE_COMMANDS):
                session = session.model_copy(update={"information_source": InformationSource.PROXY_OBSERVED})

        command_result = self._handle_product_command(session, text)
        if command_result is not None:
            session, reply, status = command_result
            intake_stage = session.intake_stage
        else:
            if self._is_intake_active(session, text) and self._looks_like_side_question(session, text):
                workflow = self._call_workflow({
                    "request_id": f"{session.session_id}-side-v{previous_version + 1}",
                    "schema_version": "a.v0.1",
                    "user_raw_input": text,
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
            if self._is_intake_active(session, text) and session.status == "ACTIVE":
                session, intake_note = self._normalize_intake_answer(session, text)
                # 「不知道／沒有／跳過」已先寫入結構化 intake；不要再把這類短句
                # 丟給醫療 intent router，否則容易被誤判為無法回答的請求。
                if intake_note:
                    workflow_text = "我要繼續整理看診前資料"
            workflow = self._call_workflow(
                {
                    "request_id": f"{session.session_id}-v{previous_version + 1}",
                    "schema_version": "a.v0.1",
                    "user_raw_input": workflow_text,
                    "declared_role": self._declared_role(session.actor_role),
                    "language": "zh-TW",
                },
                task_type="pre_visit_intake" if session.status in {"ACTIVE", "AWAITING_CONFIRMATION"} and self._is_intake_active(session, text) else None,
                intake=session.intake_snapshot if self._is_intake_active(session, text) else None,
            )
            updates: dict[str, Any] = {
                "pending_question": workflow.question,
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
            updates["pending_field"] = self._next_pending_field(resulting_intake)
            if workflow.status == "NEEDS_CONFIRMATION":
                updates["status"] = "AWAITING_CONFIRMATION"
            session = session.model_copy(update=updates, deep=True)
            reply, status, intake_stage = workflow.final_response, workflow.status, workflow.intake_stage
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

    @classmethod
    def _is_proxy_intent(cls, text: str) -> bool:
        if cls._PROXY_FUZZY_RE.search(text):
            return True
        if "幫" in text and "問" in text:
            return True
        if ("代" in text or "幫" in text) and "整理" in text:
            return True
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
        } and session.status == "PAUSED":
            pending_field = session.pending_field or self._next_pending_field(session.intake_snapshot)
            question = session.pending_question or self._question_for_field(pending_field)
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
            session = session.model_copy(update={"status": "SUBMITTED", "intake_stage": "submitted", "pending_field": None, "pending_question": None}, deep=True)
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
        cls, session: ProductSession, text: str
    ) -> tuple[ProductSession, str | None]:
        """將不知道、無、跳過與單欄自然語句轉成明確結構資料。"""
        field = session.pending_field or cls._next_pending_field(session.intake_snapshot)
        if field is None:
            return session, None
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
            text = stripped_for_len[:limit]

        candidates: dict[str, Any] = {}
        try:
            from tfda_context_gate.intake.tool import PreVisitIntakeTool

            tool = PreVisitIntakeTool()
            candidates = tool.extract_fields_from_utterance(text, stage=None)
            if "questions_for_doctor" in candidates:
                has_q = bool(re.search(r"想問|想請問|想了解|問題是|疑問|？|\?|嗎|如何|怎麼|為何|為什麼", text))
                if not has_q:
                    candidates.pop("questions_for_doctor", None)
            if "chronic_conditions" in candidates and "symptom_description" in candidates:
                desc_val = str(candidates["symptom_description"])
                distinct = any(kw in desc_val for kw in ["口渴", "頻尿", "頭暈", "疲倦", "喘", "疼痛", "麻", "視力", "血糖"])
                if not distinct and any(kw in desc_val for kw in ["高血壓", "高血脂", "高脂血", "腎臟病", "心臟病"]):
                    candidates.pop("symptom_description", None)
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
            if not existing or _is_placeholder(k, existing) or is_symptom:
                if isinstance(v, list):
                    tv = [str(x).strip()[:limit] for x in v]
                    v = tv
                elif isinstance(v, str) and len(v) > limit:
                    v = v[:limit]
                if k == "symptom_description" and "symptom_onset" in candidates and str(v).strip() == text.strip()[:limit]:
                    continue
                valid[k] = v

        if valid:
            for f, val in valid.items():
                setattr(intake, f, val)
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
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), confirm

        # F1-R1/R2: candidates hit already-filled non-symptom field -> don't pollute pending
        if candidates and not valid:
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

        if (uncertain or skip) and not valid:
            if field in {"symptom_onset", "symptom_description", "symptom_severity"}:
                setattr(intake, field, "待確認")
                return session.model_copy(update={"intake_snapshot": intake}, deep=True), (
                    "沒關係，先記為『待確認』，看診時再跟醫師確認。"
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
            direct: Any = [stripped[:limit]] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"
            } else stripped[:limit]
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
