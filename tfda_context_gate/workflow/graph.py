"""LangGraph orchestration for the bounded Agent v0.1 workflow — 3-stage topic-chunked intake."""

from __future__ import annotations

from typing import Any, Optional, TypedDict
import re
import unicodedata

from tfda_context_gate.a_router.router import route_request
from tfda_context_gate.a_router.schemas import AResult, RequestContext
from tfda_context_gate.agent import AGENT_LIMITS, AgentDecision, AgentLimits, AgentPlanner, AgentAttempt, DeterministicClarificationPolicy, FallbackDecision, QueryRewriter, build_agent_decision_context
from tfda_context_gate.b_context_gate.gate import ContextGate
from tfda_context_gate.b_context_gate.schemas import CanonicalBResult
from tfda_context_gate.c_generator.workflow_adapter import CGenerator
from tfda_context_gate.d_output_gate.gate import run_output_gate
from tfda_context_gate.d_output_gate.verifier import SemanticVerifier
from tfda_context_gate.e_observability import TraceRecorder
from tfda_context_gate.query_expansion import QueryExpander
from tfda_context_gate.query_expansion.schemas import QueryExpansionInput, QueryExpansionResult
from tfda_context_gate.rag import Retriever
from tfda_context_gate.agent.rewriter import validate_meaning_preserving_rewrite

from .adapters import a_to_query_expansion, b_to_c, c_to_d, rag_to_b
from .fallbacks import fallback_response
from .intake_router import WELCOME_MESSAGE, WELCOME_QUICK_REPLIES, is_welcome_trigger, _is_red_flag, should_append_post_answer_invitation, append_post_answer_invitation, a_route_target

try:
    from langgraph.graph import END, START, StateGraph
except ImportError as exc:
    raise RuntimeError("Agent v0.1 requires langgraph") from exc

INTAKE_STAGE_DEFS: dict[str, list[str]] = {
    "stage1": ["known_medications", "allergies", "chronic_conditions", "family_history"],
    "stage2": ["symptom_onset", "symptom_description", "symptom_severity"],
    "stage3": ["questions_for_doctor"],
}
STAGE_QUESTIONS: dict[str, str] = {
    "stage1": "為了幫您整理看診資料，請問目前使用的藥品、過敏史、慢性病史及家族史？（可一次說明多項，如「吃 metformin，無過敏，有高血壓，家族無糖尿病」）",
    "stage2": "請問症狀的相關資訊？（可一次說明，如「三個月前開始，早上血糖偏高約180，程度中等」包含時間、描述與嚴重度）",
    "stage3": "請問您想在看診時詢問醫師哪些問題？（可列多個問題）",
}

_CHIT_CHAT_RE = re.compile(r"想睡|睡覺|晚安|無聊|累了|好累|想休息|你好嗎|早安|午安", re.IGNORECASE)
_CAPABILITY_RE = re.compile(r"可以跟我說什麼|可以說什麼|能做什麼|會做什麼|能幫什麼|我能幫什麼|介紹一下|你會什麼|功能有哪些", re.IGNORECASE)
_IDENTITY_RE = re.compile(r"你是誰|你是AI|你是機器人|叫什麼|什麼名字|怎麼稱呼", re.IGNORECASE)
_EMPATHY_RE = re.compile(r"不人性化|好笨|很怪|無言|敷衍|不友善|冷淡|機械", re.IGNORECASE)
_SEVERE_EMPATHY_RE = re.compile(r"想死|不想活|活不下去|自殺|輕生|結束生命", re.IGNORECASE)


def _is_chit_chat(text: str) -> bool:
    try:
        n = unicodedata.normalize("NFKC", text).strip()
    except Exception:
        n = text or ""
    return bool(_CHIT_CHAT_RE.search(n))


def _is_identity(text: str) -> bool:
    try:
        n = unicodedata.normalize("NFKC", text).strip()
    except Exception:
        n = text or ""
    if _IDENTITY_RE.search(n):
        return True
    if n.strip().strip("？?。.!！") == "是誰":
        return True
    return False


def _is_empathy(text: str) -> bool:
    try:
        n = unicodedata.normalize("NFKC", text).strip()
    except Exception:
        n = text or ""
    return bool(_EMPATHY_RE.search(n))


def _is_capability_query(text: str) -> bool:
    try:
        n = unicodedata.normalize("NFKC", text).strip()
    except Exception:
        n = text or ""
    return bool(_CAPABILITY_RE.search(n))


def get_welcome_message() -> dict[str, Any]:
    return {"message": WELCOME_MESSAGE, "quick_replies": WELCOME_QUICK_REPLIES, "task_type": None}

def _get_intake_stage(intake: Any) -> str | None:
    if intake is None:
        return "stage1"
    try:
        from tfda_context_gate.intake.schemas import PreVisitIntake
        if isinstance(intake, dict):
            obj = PreVisitIntake.model_validate(intake)
        elif isinstance(intake, PreVisitIntake):
            obj = intake
        else:
            return "stage1"
        for stage, fields in INTAKE_STAGE_DEFS.items():
            for f in fields:
                val = getattr(obj, f, None)
                if not val:
                    return stage
        return None
    except Exception:
        return "stage1"

def _extract_multi_fields(utterance: str, stage: str) -> dict[str, Any]:
    try:
        from tfda_context_gate.intake.tool import PreVisitIntakeTool
        tool = PreVisitIntakeTool()
        return tool.extract_fields_from_utterance(utterance, stage=stage)
    except Exception:
        return {}


def _next_intake_question(intake: Any, stage: str) -> tuple[str | None, str | None]:
    """一次只問一個尚缺欄位，降低病患填答負擔。"""
    fields = INTAKE_STAGE_DEFS.get(stage, [])
    for field in fields:
        if not getattr(intake, field, None):
            return field, build_agent_question([field])
    return None, None

class WorkflowState(TypedDict, total=False):
    request_context: RequestContext
    request_id: str
    original_query: str
    current_query: str
    a_result: AResult
    query_expansion: QueryExpansionResult
    rag_result: Any
    b_result: CanonicalBResult
    c_result: Any
    d_result: Any
    trace: TraceRecorder
    agent_planner: Any
    query_rewriter: Any
    agent_limits: AgentLimits
    agent_decision: Any
    previous_attempts: list[AgentAttempt]
    pending_agent_action: Optional[str]
    agent_steps: int
    rewrite_count: int
    clarification_count: int
    retrieval_attempt: int
    b_attempt: int
    actions_taken: list[str]
    agent_reason_code: Optional[str]
    question: Optional[str]
    status: Optional[str]
    final_response: Optional[str]
    fallback_reason: Optional[str]
    termination_reason: Optional[str]
    intake: Any
    previsit_summary: Any
    intake_stage: Optional[str]
    review_confirmed: Optional[bool]
    task_type: Optional[str]
    intake_data: Any
    medication_clarification_attempts: int
    medication_original_text: Optional[str]

def build_agent_question(missing_information: list[str]) -> str:
    intake_questions = {
        "known_medications": "目前有固定吃藥或打胰島素嗎？知道藥名就直接說；不確定也沒關係。",
        "allergies": "有沒有藥物或食物過敏？沒有、不確定都可以直接說。",
        "chronic_conditions": "除了糖尿病，還有高血壓、高血脂等慢性病嗎？",
        "family_history": "家人中有人有糖尿病或相關疾病嗎？",
        "symptom_onset": "這次想看診的狀況大約從什麼時候開始？",
        "symptom_description": "目前最主要的症狀或困擾是什麼？",
        "symptom_severity": "程度大約是輕度、中度、重度，或 1–10 分中的幾分？",
        "questions_for_doctor": "這次最想問醫師什麼？還沒想到也可以先跳過。",
        "time_frame": "請問這些症狀是現在發生、過去曾發生，還是假設性詢問？",
        "target_subject": "請問這些症狀是您本人、家人，還是其他對象的情況？",
    }
    legacy_questions = {"drug_type": "請問家人目前使用的是哪一類糖尿病藥物？", "medication_class": "請問家人目前使用的是哪一類糖尿病藥物？", "medicine_name": "請問目前使用的藥物名稱或成分是什麼？", "symptom": "請問目前具體有哪些症狀？"}
    for field in missing_information:
        if field in intake_questions:
            return intake_questions[field]
    for field in missing_information:
        if field in legacy_questions:
            return legacy_questions[field]
    try:
        from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS
        for field in missing_information:
            if field in INTAKE_FIELD_QUESTIONS:
                return INTAKE_FIELD_QUESTIONS[field]
    except Exception:
        pass
    labels = "、".join(missing_information)
    return f"為了縮小可可靠查找的範圍，請補充以下資訊：{labels}。"

def _expand_current_query(a_result: AResult, *, current_query: str, query_expander: QueryExpander) -> QueryExpansionResult:
    if current_query == a_result.user_raw_input:
        return query_expander.expand(a_to_query_expansion(a_result))
    input_value = QueryExpansionInput(request_id=a_result.request_id, original_query=current_query, router_status=a_result.router_status.value, intent_tags=[item.value for item in a_result.intent_tags], declared_role=a_result.declared_role.value, language=a_result.language.value)
    expanded = query_expander.expand(input_value)
    return QueryExpansionResult(request_id=expanded.request_id, original_query=a_result.user_raw_input, retrieval_queries=expanded.retrieval_queries, strategy=expanded.strategy)

def _retrieval_outcome(b_result: CanonicalBResult) -> dict[str, object]:
    return {"evidence_count": len(b_result.evidence), "top_evidence_ids": [item.evidence_id for item in b_result.evidence[:5]], "retrieval_queries": list(b_result.retrieval_feedback.get("retrieval_queries", []) if isinstance(b_result.retrieval_feedback, dict) else [])}

def _retrieved_evidence_trace(evidence: list[Any]) -> list[dict[str, Any]]:
    return [{"evidence_id": item.evidence_id, "rank": rank, "score": item.score, "source": item.source, "date": item.date} for rank, item in enumerate(evidence, start=1)]

def build_workflow_graph(*, trace: TraceRecorder, query_expander: QueryExpander, retriever: Retriever, context_gate: ContextGate, generator: CGenerator, verifier: SemanticVerifier | None, agent_planner: AgentPlanner | None, query_rewriter: QueryRewriter | None, prompt_injection_guard: Any | None = None, extractor: Any | None = None, agent_limits: AgentLimits = AGENT_LIMITS, tool_executor: Any | None = None, tool_source_id: str | None = None, task_type: str | None = None) -> tuple[Any, dict[str, str]]:
    runtime_stage = {"current": "SYSTEM"}
    def stage(name: str) -> None:
        runtime_stage["current"] = name

    def a_node(state: WorkflowState) -> dict[str, Any]:
        stage("A")
        request = state["request_context"]
        raw_input = request.user_raw_input if hasattr(request, "user_raw_input") else str(request.get("user_raw_input", "")) if isinstance(request, dict) else ""
        if is_welcome_trigger(raw_input):
            from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor
            try:
                is_intake_welcome = RuleBasedSignalExtractor.is_pre_visit_intake_text(raw_input)
            except Exception:
                is_intake_welcome = False
            if not is_intake_welcome:
                try:
                    from tfda_context_gate.workflow.fallbacks import WELCOME_VARIANTS, _fallback_seen, _fallback_lock
                    _w_key = f"global:WELCOME"
                    with _fallback_lock:
                        _seen = _fallback_seen.get(_w_key, set())
                        if not _seen:
                            _welcome_variant = WELCOME_VARIANTS[0]
                            _seen.add(_welcome_variant)
                        else:
                            _remaining = [v for v in WELCOME_VARIANTS if v not in _seen]
                            if _remaining:
                                _welcome_variant = _remaining[0]
                                if "又見面了" in _welcome_variant:
                                    for v in _remaining:
                                        if "又見面了" in v:
                                            _welcome_variant = v
                                            break
                            else:
                                _welcome_variant = WELCOME_VARIANTS[1]
                                _seen = set()
                            _seen.add(_welcome_variant)
                        _fallback_seen[_w_key] = _seen
                except Exception:
                    _welcome_variant = WELCOME_MESSAGE
                with trace.span("A", "welcome_message") as span:
                    span.set(status="COMPLETED", reason_codes=["WELCOME_MESSAGE"], question=_welcome_variant)
                try:
                    from tfda_context_gate.a_router.labels import DeclaredRole, LanguageCode, RouterStatus, PolicyReasonCode
                    from tfda_context_gate.a_router.schemas import AResult, ContextModifiers
                    req_id = request.request_id if hasattr(request, "request_id") else state.get("request_id", "unknown")
                    schema_ver = request.schema_version if hasattr(request, "schema_version") else "a.v0.1"
                    decl_role = request.declared_role if hasattr(request, "declared_role") else DeclaredRole.PATIENT
                    if isinstance(decl_role, str):
                        try:
                            decl_role = DeclaredRole(decl_role)
                        except Exception:
                            decl_role = DeclaredRole.PATIENT
                    result = AResult(request_id=req_id, schema_version=schema_ver, user_raw_input=raw_input, declared_role=decl_role, language=LanguageCode.ZH_TW, intent_tags=[], risk_flags=[], context_modifiers=ContextModifiers(language=LanguageCode.ZH_TW), router_status=RouterStatus.G_GENERAL_EDUCATION, reason_codes=[PolicyReasonCode.INQUIRY_GENERAL_EDUCATION, PolicyReasonCode.NO_CRITICAL_SYMPTOMS_DETECTED, PolicyReasonCode.MEETS_SAFE_SCOPE], rag_allowed=True, task_type=None)
                except Exception:
                    from tfda_context_gate.a_router.labels import DeclaredRole, LanguageCode, RouterStatus, PolicyReasonCode
                    from tfda_context_gate.a_router.schemas import AResult, ContextModifiers
                    result = AResult(request_id=state.get("request_id", "unknown"), schema_version="a.v0.1", user_raw_input=raw_input, declared_role=DeclaredRole.PATIENT, language=LanguageCode.ZH_TW, intent_tags=[], risk_flags=[], context_modifiers=ContextModifiers(language=LanguageCode.ZH_TW), router_status=RouterStatus.G_GENERAL_EDUCATION, reason_codes=[PolicyReasonCode.INQUIRY_GENERAL_EDUCATION], rag_allowed=True, task_type=None)
                return {"a_result": result, "status": "COMPLETED", "final_response": _welcome_variant, "fallback_reason": None, "termination_reason": "WELCOME_MESSAGE"}
        if _is_red_flag(raw_input):
            from tfda_context_gate.a_router.labels import RiskFlag, RouterStatus, PolicyReasonCode
            from tfda_context_gate.a_router.schemas import AResult, RouterSignals, ContextModifiers
            from tfda_context_gate.a_router.labels import LanguageCode
            signals = RouterSignals(intent_tags=[], risk_flags=[RiskFlag.POSSIBLE_EMERGENCY], context_modifiers=ContextModifiers(language=LanguageCode.ZH_TW))
            from tfda_context_gate.a_router.policy import policy_gate
            decision = policy_gate(signals, __import__("tfda_context_gate.a_router.policy", fromlist=["DEFAULT_POLICY"]).DEFAULT_POLICY)
            if decision.status.value not in ("E_EMERGENCY", "U_URGENT_HUMAN"):
                from tfda_context_gate.a_router.labels import RouterStatus as RS
                decision_status = RS.U_URGENT_HUMAN
                reason_codes = [PolicyReasonCode.REASON_POSSIBLE_EMERGENCY]
            else:
                decision_status = decision.status
                reason_codes = list(decision.reason_codes)
            result = AResult.from_request_and_decision(request, signals, decision_status, reason_codes)
            with trace.span("A", "input_router") as span:
                span.set(status="BLOCKED", router_status=result.router_status.value, intent_tags=[item.value for item in result.intent_tags], risk_flags=[item.value for item in result.risk_flags], reason_codes=[item.value for item in result.reason_codes], rag_allowed=result.rag_allowed, prompt_guard_result="BLOCKED", termination_reason="RED_FLAG_DETERMINISTIC_ABORT")
            emergency_reason = "A_EMERGENCY" if result.router_status.value == "E_EMERGENCY" else "A_URGENT_HUMAN"
            return {"a_result": result, "status": "FALLBACK", "final_response": fallback_response(emergency_reason), "fallback_reason": emergency_reason, "termination_reason": "RED_FLAG_DETERMINISTIC_ABORT"}
        # P5-1 IDENTITY short-circuit: after red-flag, before G2 whitelist
        try:
            from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _IdExtractor
            _is_ident = _IdExtractor.is_identity_text(raw_input)
        except Exception:
            _is_ident = _is_identity(raw_input)
        if _is_ident:
            try:
                from tfda_context_gate.a_router.labels import DeclaredRole as _IdDR, LanguageCode as _IdLC, RouterStatus as _IdRS, PolicyReasonCode as _IdPRC, IntentTag as _IdIT
                from tfda_context_gate.a_router.schemas import AResult as _IdAR, ContextModifiers as _IdCM
                _id_req_id = request.request_id if hasattr(request, "request_id") else state.get("request_id", "unknown")
                _id_schema_ver = request.schema_version if hasattr(request, "schema_version") else "a.v0.1"
                _id_decl_role = request.declared_role if hasattr(request, "declared_role") else _IdDR.PATIENT
                if isinstance(_id_decl_role, str):
                    try:
                        _id_decl_role = _IdDR(_id_decl_role)
                    except Exception:
                        _id_decl_role = _IdDR.PATIENT
                _id_lang = request.language if hasattr(request, "language") else _IdLC.ZH_TW
                if isinstance(_id_lang, str):
                    try:
                        _id_lang = _IdLC(_id_lang)
                    except Exception:
                        _id_lang = _IdLC.ZH_TW
                _id_result = _IdAR(request_id=_id_req_id, schema_version=_id_schema_ver, user_raw_input=raw_input, declared_role=_id_decl_role, language=_id_lang, intent_tags=[_IdIT.IDENTITY], risk_flags=[], context_modifiers=_IdCM(language=_id_lang), router_status=_IdRS.O_OUT_OF_SCOPE, reason_codes=[_IdPRC.REASON_OUT_OF_SCOPE], rag_allowed=False, task_type=None)
            except Exception:
                from tfda_context_gate.a_router.labels import DeclaredRole as _IdDR2, LanguageCode as _IdLC2, RouterStatus as _IdRS2, PolicyReasonCode as _IdPRC2, IntentTag as _IdIT2
                from tfda_context_gate.a_router.schemas import AResult as _IdAR2, ContextModifiers as _IdCM2
                _id_result = _IdAR2(request_id=state.get("request_id", "unknown"), schema_version="a.v0.1", user_raw_input=raw_input, declared_role=_IdDR2.PATIENT, language=_IdLC2.ZH_TW, intent_tags=[_IdIT2.IDENTITY], risk_flags=[], context_modifiers=_IdCM2(language=_IdLC2.ZH_TW), router_status=_IdRS2.O_OUT_OF_SCOPE, reason_codes=[_IdPRC2.REASON_OUT_OF_SCOPE], rag_allowed=False, task_type=None)
            with trace.span("A", "input_router") as span:
                span.set(status="BLOCKED", router_status=_id_result.router_status.value, intent_tags=[item.value for item in _id_result.intent_tags], risk_flags=[], reason_codes=[item.value for item in _id_result.reason_codes], rag_allowed=False, termination_reason="IDENTITY_SHORT_CIRCUIT", fallback_reason="IDENTITY")
            return {"a_result": _id_result, "status": "BLOCKED", "final_response": fallback_response("IDENTITY"), "fallback_reason": "IDENTITY", "termination_reason": "IDENTITY_SHORT_CIRCUIT"}
        if _is_empathy(raw_input):
            try:
                from tfda_context_gate.a_router.labels import DeclaredRole as _EmDR, LanguageCode as _EmLC, RouterStatus as _EmRS, PolicyReasonCode as _EmPRC, IntentTag as _EmIT
                from tfda_context_gate.a_router.schemas import AResult as _EmAR, ContextModifiers as _EmCM
                from tfda_context_gate.workflow.fallbacks import empathy_response
                _em_req_id = request.request_id if hasattr(request, "request_id") else state.get("request_id", "unknown")
                _em_schema_ver = request.schema_version if hasattr(request, "schema_version") else "a.v0.1"
                _em_decl_role = request.declared_role if hasattr(request, "declared_role") else _EmDR.PATIENT
                if isinstance(_em_decl_role, str):
                    try:
                        _em_decl_role = _EmDR(_em_decl_role)
                    except Exception:
                        _em_decl_role = _EmDR.PATIENT
                _em_lang = request.language if hasattr(request, "language") else _EmLC.ZH_TW
                if isinstance(_em_lang, str):
                    try:
                        _em_lang = _EmLC(_em_lang)
                    except Exception:
                        _em_lang = _EmLC.ZH_TW
                _em_result = _EmAR(request_id=_em_req_id, schema_version=_em_schema_ver, user_raw_input=raw_input, declared_role=_em_decl_role, language=_em_lang, intent_tags=[_EmIT.NON_MEDICAL], risk_flags=[], context_modifiers=_EmCM(language=_em_lang), router_status=_EmRS.O_OUT_OF_SCOPE, reason_codes=[_EmPRC.REASON_OUT_OF_SCOPE], rag_allowed=False, task_type=None)
            except Exception:
                from tfda_context_gate.a_router.labels import DeclaredRole as _EmDR2, LanguageCode as _EmLC2, RouterStatus as _EmRS2, PolicyReasonCode as _EmPRC2, IntentTag as _EmIT2
                from tfda_context_gate.a_router.schemas import AResult as _EmAR2, ContextModifiers as _EmCM2
                _em_result = _EmAR2(request_id=state.get("request_id", "unknown"), schema_version="a.v0.1", user_raw_input=raw_input, declared_role=_EmDR2.PATIENT, language=_EmLC2.ZH_TW, intent_tags=[_EmIT2.NON_MEDICAL], risk_flags=[], context_modifiers=_EmCM2(language=_EmLC2.ZH_TW), router_status=_EmRS2.O_OUT_OF_SCOPE, reason_codes=[_EmPRC2.REASON_OUT_OF_SCOPE], rag_allowed=False, task_type=None)
            with trace.span("A", "input_router") as span:
                span.set(status="BLOCKED", router_status=_em_result.router_status.value, intent_tags=[item.value for item in _em_result.intent_tags], risk_flags=[], reason_codes=[item.value for item in _em_result.reason_codes], rag_allowed=False, termination_reason="EMPATHY_SHORT_CIRCUIT", fallback_reason="EMPATHY")
            try:
                from tfda_context_gate.workflow.fallbacks import empathy_response as _er
                _empathy_text = _er(raw_input)
            except Exception:
                _empathy_text = fallback_response("EMPATHY")
            return {"a_result": _em_result, "status": "BLOCKED", "final_response": _empathy_text, "fallback_reason": "EMPATHY", "termination_reason": "EMPATHY_SHORT_CIRCUIT"}
        # G2 whitelist short-circuit (P3 latency fix): chit-chat before LLM, after red-flag invariant
        try:
            from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _G2Extractor
            _g2_hit = _G2Extractor.is_chit_chat_text(raw_input)
        except Exception:
            _g2_hit = False
        if _g2_hit:
            _is_cap = _is_capability_query(raw_input)
            reason = "O_GENERIC" if _is_cap else "CHIT_CHAT_OUT_OF_SCOPE"
            try:
                from tfda_context_gate.a_router.labels import DeclaredRole as _G2DR, LanguageCode as _G2LC, RouterStatus as _G2RS, PolicyReasonCode as _G2PRC, IntentTag as _G2IT
                from tfda_context_gate.a_router.schemas import AResult as _G2AR, ContextModifiers as _G2CM
                _g2_req_id = request.request_id if hasattr(request, "request_id") else state.get("request_id", "unknown")
                _g2_schema_ver = request.schema_version if hasattr(request, "schema_version") else "a.v0.1"
                _g2_decl_role = request.declared_role if hasattr(request, "declared_role") else _G2DR.PATIENT
                if isinstance(_g2_decl_role, str):
                    try:
                        _g2_decl_role = _G2DR(_g2_decl_role)
                    except Exception:
                        _g2_decl_role = _G2DR.PATIENT
                _g2_lang = request.language if hasattr(request, "language") else _G2LC.ZH_TW
                if isinstance(_g2_lang, str):
                    try:
                        _g2_lang = _G2LC(_g2_lang)
                    except Exception:
                        _g2_lang = _G2LC.ZH_TW
                _g2_result = _G2AR(request_id=_g2_req_id, schema_version=_g2_schema_ver, user_raw_input=raw_input, declared_role=_g2_decl_role, language=_g2_lang, intent_tags=[_G2IT.NON_MEDICAL], risk_flags=[], context_modifiers=_G2CM(language=_g2_lang), router_status=_G2RS.O_OUT_OF_SCOPE, reason_codes=[_G2PRC.REASON_OUT_OF_SCOPE], rag_allowed=False, task_type=None)
            except Exception:
                from tfda_context_gate.a_router.labels import DeclaredRole as _G2DR2, LanguageCode as _G2LC2, RouterStatus as _G2RS2, PolicyReasonCode as _G2PRC2, IntentTag as _G2IT2
                from tfda_context_gate.a_router.schemas import AResult as _G2AR2, ContextModifiers as _G2CM2
                _g2_result = _G2AR2(request_id=state.get("request_id", "unknown"), schema_version="a.v0.1", user_raw_input=raw_input, declared_role=_G2DR2.PATIENT, language=_G2LC2.ZH_TW, intent_tags=[_G2IT2.NON_MEDICAL], risk_flags=[], context_modifiers=_G2CM2(language=_G2LC2.ZH_TW), router_status=_G2RS2.O_OUT_OF_SCOPE, reason_codes=[_G2PRC2.REASON_OUT_OF_SCOPE], rag_allowed=False, task_type=None)
            with trace.span("A", "input_router") as span:
                span.set(status="BLOCKED", router_status=_g2_result.router_status.value, intent_tags=[item.value for item in _g2_result.intent_tags], risk_flags=[], reason_codes=[item.value for item in _g2_result.reason_codes], rag_allowed=False, termination_reason="G2_WHITELIST_SHORT_CIRCUIT", fallback_reason=reason)
            return {"a_result": _g2_result, "status": "BLOCKED", "final_response": fallback_response(reason), "fallback_reason": reason, "termination_reason": "G2_WHITELIST_SHORT_CIRCUIT"}
        with trace.span("A", "input_router") as span:
            result = route_request(request, extractor=extractor, prompt_injection_guard=prompt_injection_guard)
            span.set(status=("COMPLETED" if result.rag_allowed else ("FALLBACK" if result.router_status.value == "F_ROUTER_DEPENDENCY" else "BLOCKED")), router_status=result.router_status.value, intent_tags=[item.value for item in result.intent_tags], risk_flags=[item.value for item in result.risk_flags], reason_codes=[item.value for item in result.reason_codes], rag_allowed=result.rag_allowed, prompt_guard_result="BLOCKED" if not result.rag_allowed else "ALLOWED")
        if not result.rag_allowed:
            raw = request.user_raw_input if hasattr(request, "user_raw_input") else ""
            try:
                from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor
                is_intake = RuleBasedSignalExtractor.is_pre_visit_intake_text(raw)
            except Exception:
                is_intake = bool(re.search(r"準備看診|看診前|整理.*資料|pre.?visit|intake|要看醫生|回診", raw, re.IGNORECASE))
            is_m_block = result.router_status.value == "M_MEDICATION_REFERRAL"
            if is_intake and is_m_block:
                try:
                    from tfda_context_gate.intake.tool import PreVisitIntakeTool
                    tool = PreVisitIntakeTool()
                    if tool.is_colloquial_medication(raw):
                        from tfda_context_gate.a_router.labels import RouterStatus, PolicyReasonCode
                        from tfda_context_gate.a_router.schemas import AResult
                        result = AResult(request_id=result.request_id, schema_version=result.schema_version, user_raw_input=result.user_raw_input, declared_role=result.declared_role, language=result.language, intent_tags=result.intent_tags, risk_flags=[f for f in result.risk_flags if str(f) != "PERSONALIZED_MEDICATION"], context_modifiers=result.context_modifiers, router_status=RouterStatus.G_GENERAL_EDUCATION, reason_codes=[PolicyReasonCode.INQUIRY_GENERAL_EDUCATION, PolicyReasonCode.NO_CRITICAL_SYMPTOMS_DETECTED, PolicyReasonCode.MEETS_SAFE_SCOPE], rag_allowed=True)
                        with trace.span("A", "input_router_override") as span:
                            span.set(status="COMPLETED", router_status="G_GENERAL_EDUCATION", intent_tags=[item.value for item in result.intent_tags], risk_flags=[item.value for item in result.risk_flags], reason_codes=[item.value for item in result.reason_codes], rag_allowed=True, prompt_guard_result="ALLOWED", termination_reason="INTAKE_COLLOQUIAL_M_OVERRIDDEN_TO_G")
                        return {"a_result": result}
                except Exception:
                    pass
            if result.router_status.value == "E_EMERGENCY":
                reason = "A_EMERGENCY"
            elif result.router_status.value == "U_URGENT_HUMAN":
                reason = "A_URGENT_HUMAN"
            elif result.router_status.value == "F_ROUTER_DEPENDENCY":
                reason = "A_DEPENDENCY"
            elif result.router_status.value == "O_OUT_OF_SCOPE":
                reason = "CHIT_CHAT_OUT_OF_SCOPE" if _is_chit_chat(raw) else "O_GENERIC"
            elif result.router_status.value == "Q_CLARIFICATION":
                if _is_chit_chat(raw):
                    reason = "CHIT_CHAT_OUT_OF_SCOPE"
                elif _is_capability_query(raw):
                    reason = "O_GENERIC"
                else:
                    reason = "Q_NEED_MORE"
            elif result.router_status.value == "R_POLICY_BOUNDARY":
                rc_vals = [r.value if hasattr(r, "value") else str(r) for r in result.reason_codes]
                if "REASON_PROMPT_INJECTION_SUSPECTED" in rc_vals:
                    reason = "R_GUARDRAIL_BLOCKED"
                elif "REASON_DIAGNOSIS_OR_TREATMENT_REQUEST" in rc_vals:
                    reason = "R_DIAGNOSIS_BOUNDARY"
                else:
                    reason = "O_GENERIC"
            elif result.router_status.value == "M_MEDICATION_REFERRAL":
                reason = "A_BLOCKED"
            else:
                reason = "O_GENERIC"
            is_dependency = reason == "A_DEPENDENCY"
            is_emergency = reason in {"A_EMERGENCY", "A_URGENT_HUMAN"}
            return {
                "a_result": result,
                "status": "FALLBACK" if is_dependency or is_emergency else "BLOCKED",
                "final_response": fallback_response(reason),
                "fallback_reason": reason,
                "termination_reason": "A_DEPENDENCY" if is_dependency else ("RED_FLAG_DETERMINISTIC_ABORT" if is_emergency else reason),
            }
        return {"a_result": result}

    def a_route(state: WorkflowState) -> str:
        return a_route_target(task_type=task_type or state.get("task_type"), intake=state.get("intake"), intake_data=state.get("intake_data"), a_result=state.get("a_result"), original_query=state.get("original_query", ""), termination_reason=state.get("termination_reason"), rag_allowed=state.get("a_result").rag_allowed if state.get("a_result") else None)

    def intake_check_node(state: WorkflowState) -> dict[str, Any]:
        stage("INTAKE_CHECK")
        intake = state.get("intake") or state.get("intake_data")
        current_stage = _get_intake_stage(intake)
        with trace.span("INTAKE_CHECK", "intake_stage_router") as span:
            span.set(status="COMPLETED", question=current_stage or "COMPLETE", missing_information=[current_stage] if current_stage else [])
        if current_stage is None:
            return {"intake_stage": "review", "intake": intake}
        return {"intake_stage": current_stage, "intake": intake}

    def intake_route(state: WorkflowState) -> str:
        stage_name = state.get("intake_stage")
        if stage_name == "review":
            return "REVIEW_CONFIRM"
        if stage_name == "stage1":
            return "INTAKE_STAGE1"
        if stage_name == "stage2":
            return "INTAKE_STAGE2"
        if stage_name == "stage3":
            return "INTAKE_STAGE3"
        return "REVIEW_CONFIRM"

    def intake_stage1_node(state: WorkflowState) -> dict[str, Any]:
        stage("INTAKE_STAGE1")
        intake = state.get("intake") or state.get("intake_data") or {}
        original = state.get("original_query", "")
        current = state.get("current_query", original)
        text_to_check = current if current != original else original
        med_attempts = state.get("medication_clarification_attempts", 0)
        med_original = state.get("medication_original_text")
        try:
            from tfda_context_gate.intake.schemas import PreVisitIntake
            from tfda_context_gate.intake.tool import PreVisitIntakeTool
            tool = PreVisitIntakeTool()
            is_colloquial = tool.is_colloquial_medication(text_to_check)
            confidence = tool.assess_medication_confidence(text_to_check)
            needs_med_clarify = is_colloquial and confidence < 0.7
            if needs_med_clarify and med_attempts < 2:
                next_attempt = med_attempts + 1
                q_info = tool.get_medication_clarification_question(next_attempt)
                question = q_info["question"]
                with trace.span("INTAKE_STAGE1", "intake_extraction") as span:
                    span.set(status="NEEDS_CLARIFICATION", question=question, missing_information=["known_medications"], identified_missing_information=["known_medications"], reason_code=q_info["reason"])
                return {"intake": intake if isinstance(intake, PreVisitIntake) else (PreVisitIntake.model_validate(intake) if isinstance(intake, dict) and intake else PreVisitIntake()), "intake_stage": "stage1", "question": question, "status": "NEEDS_CLARIFICATION", "final_response": question, "termination_reason": "NEEDS_CLARIFICATION", "clarification_count": state.get("clarification_count", 0) + 1, "medication_clarification_attempts": next_attempt, "medication_original_text": text_to_check.strip()[:50] if not med_original else med_original}
            if needs_med_clarify and med_attempts >= 2:
                unknown_text = tool.mark_medication_unknown(med_original or text_to_check)
                if isinstance(intake, dict):
                    obj = PreVisitIntake.model_validate(intake) if intake else PreVisitIntake()
                elif isinstance(intake, PreVisitIntake):
                    obj = intake
                else:
                    obj = PreVisitIntake()
                obj.known_medications = [unknown_text]
                extracted = _extract_multi_fields(text_to_check, "stage1")
                for k, v in extracted.items():
                    if k in INTAKE_STAGE_DEFS["stage1"] and k != "known_medications":
                        current_val = getattr(obj, k, None)
                        if not current_val:
                            setattr(obj, k, v)
                missing_field, next_question = _next_intake_question(obj, "stage1")
                with trace.span("INTAKE_STAGE1", "intake_extraction") as span:
                    span.set(status="NEEDS_CLARIFICATION" if missing_field else "COMPLETED", question=next_question, missing_information=[missing_field] if missing_field else [], identified_missing_information=[missing_field] if missing_field else [], reason_code="medication_unknown_after_2_attempts")
                if missing_field and next_question:
                    return {"intake": obj, "intake_stage": "stage1", "question": next_question, "status": "NEEDS_CLARIFICATION", "final_response": next_question, "termination_reason": "NEEDS_CLARIFICATION", "medication_clarification_attempts": med_attempts, "medication_original_text": med_original or text_to_check.strip()[:50]}
                next_question = build_agent_question(["symptom_onset"])
                return {"intake": obj, "intake_stage": "stage2", "question": next_question, "status": "NEEDS_CLARIFICATION", "final_response": next_question, "termination_reason": "NEXT_INTAKE_STAGE", "medication_clarification_attempts": med_attempts, "medication_original_text": med_original or text_to_check.strip()[:50]}
            extracted = _extract_multi_fields(original, "stage1")
            if isinstance(intake, dict):
                obj = PreVisitIntake.model_validate(intake) if intake else PreVisitIntake()
            elif isinstance(intake, PreVisitIntake):
                obj = intake
            else:
                obj = PreVisitIntake()
            for k, v in extracted.items():
                if k in INTAKE_STAGE_DEFS["stage1"]:
                    current_val = getattr(obj, k, None)
                    if not current_val:
                        setattr(obj, k, v)
            missing_field, next_question = _next_intake_question(obj, "stage1")
            with trace.span("INTAKE_STAGE1", "intake_extraction") as span:
                span.set(status="NEEDS_CLARIFICATION" if missing_field else "COMPLETED", question=next_question, missing_information=[missing_field] if missing_field else [], identified_missing_information=[missing_field] if missing_field else [])
            def _with_confirm(q: str | None) -> str | None:
                if q is None or not extracted:
                    return q
                try:
                    from tfda_context_gate.intake.tool import build_implicit_confirm_for_fields
                    c = build_implicit_confirm_for_fields(extracted, raw_text=original)
                    if c:
                        return f"{c}\n{q}"
                except Exception:
                    pass
                return q
            if missing_field and next_question:
                final_q = _with_confirm(next_question)
                return {"intake": obj, "intake_stage": "stage1", "question": final_q, "status": "NEEDS_CLARIFICATION", "final_response": final_q, "termination_reason": "NEEDS_CLARIFICATION", "clarification_count": state.get("clarification_count", 0) + 1}
            next_question = build_agent_question(["symptom_onset"])
            final_q2 = _with_confirm(next_question)
            return {"intake": obj, "intake_stage": "stage2", "question": final_q2, "status": "NEEDS_CLARIFICATION", "final_response": final_q2, "termination_reason": "NEXT_INTAKE_STAGE"}
        except Exception as e:
            with trace.span("INTAKE_STAGE1", "intake_extraction") as span:
                span.set(status="ERROR", error_type="IntakeExtractionError", error_message=str(e)[:200])
            return {"intake": intake, "intake_stage": "stage1", "question": STAGE_QUESTIONS["stage1"], "status": "NEEDS_CLARIFICATION", "final_response": STAGE_QUESTIONS["stage1"]}

    def intake_stage2_node(state: WorkflowState) -> dict[str, Any]:
        stage("INTAKE_STAGE2")
        intake = state.get("intake") or {}
        original = state.get("original_query", "")
        current = state.get("current_query", original)
        text_to_extract = current if current != original else original
        extracted = _extract_multi_fields(text_to_extract, "stage2")
        if not extracted:
            extracted = _extract_multi_fields(original, "stage2")
        try:
            from tfda_context_gate.intake.schemas import PreVisitIntake
            if isinstance(intake, dict):
                obj = PreVisitIntake.model_validate(intake)
            elif isinstance(intake, PreVisitIntake):
                obj = intake
            else:
                obj = PreVisitIntake()
            for k, v in extracted.items():
                if k in INTAKE_STAGE_DEFS["stage2"]:
                    current_val = getattr(obj, k, None)
                    if not current_val:
                        setattr(obj, k, v)
            missing_field, next_question = _next_intake_question(obj, "stage2")
            with trace.span("INTAKE_STAGE2", "intake_extraction") as span:
                span.set(status="NEEDS_CLARIFICATION" if missing_field else "COMPLETED", question=next_question, missing_information=[missing_field] if missing_field else [], identified_missing_information=[missing_field] if missing_field else [])
            def _with_confirm(q: str | None) -> str | None:
                if q is None or not extracted:
                    return q
                try:
                    from tfda_context_gate.intake.tool import build_implicit_confirm_for_fields
                    c = build_implicit_confirm_for_fields(extracted, raw_text=text_to_extract)
                    if c:
                        return f"{c}\n{q}"
                except Exception:
                    pass
                return q
            if missing_field and next_question:
                final_q = _with_confirm(next_question)
                return {"intake": obj, "intake_stage": "stage2", "question": final_q, "status": "NEEDS_CLARIFICATION", "final_response": final_q, "termination_reason": "NEEDS_CLARIFICATION", "clarification_count": state.get("clarification_count", 0) + 1}
            next_question = build_agent_question(["questions_for_doctor"])
            final_q2 = _with_confirm(next_question)
            return {"intake": obj, "intake_stage": "stage3", "question": final_q2, "status": "NEEDS_CLARIFICATION", "final_response": final_q2, "termination_reason": "NEXT_INTAKE_STAGE"}
        except Exception as e:
            with trace.span("INTAKE_STAGE2", "intake_extraction") as span:
                span.set(status="ERROR", error_type="IntakeExtractionError", error_message=str(e)[:200])
            return {"intake": intake, "intake_stage": "stage2", "question": STAGE_QUESTIONS["stage2"], "status": "NEEDS_CLARIFICATION", "final_response": STAGE_QUESTIONS["stage2"]}

    def intake_stage3_node(state: WorkflowState) -> dict[str, Any]:
        stage("INTAKE_STAGE3")
        intake = state.get("intake") or {}
        original = state.get("original_query", "")
        current = state.get("current_query", original)
        text_to_extract = current if current != original else original
        extracted = _extract_multi_fields(text_to_extract, "stage3")
        if not extracted:
            extracted = _extract_multi_fields(original, "stage3")
        try:
            from tfda_context_gate.intake.schemas import PreVisitIntake
            if isinstance(intake, dict):
                obj = PreVisitIntake.model_validate(intake)
            elif isinstance(intake, PreVisitIntake):
                obj = intake
            else:
                obj = PreVisitIntake()
            for k, v in extracted.items():
                if k in INTAKE_STAGE_DEFS["stage3"]:
                    current_val = getattr(obj, k, None)
                    if not current_val:
                        setattr(obj, k, v)
            has_any = any(getattr(obj, f) for f in INTAKE_STAGE_DEFS["stage3"])
            with trace.span("INTAKE_STAGE3", "intake_extraction") as span:
                span.set(status="COMPLETED" if has_any else "NEEDS_CLARIFICATION", missing_information=list(extracted.keys()), identified_missing_information=["stage3"] if not has_any else [])
            if not has_any:
                return {"intake": obj, "intake_stage": "stage3", "question": STAGE_QUESTIONS["stage3"], "status": "NEEDS_CLARIFICATION", "final_response": STAGE_QUESTIONS["stage3"], "termination_reason": "NEEDS_CLARIFICATION", "clarification_count": state.get("clarification_count", 0) + 1}
            return {"intake": obj, "intake_stage": "review"}
        except Exception as e:
            with trace.span("INTAKE_STAGE3", "intake_extraction") as span:
                span.set(status="ERROR", error_type="IntakeExtractionError", error_message=str(e)[:200])
            return {"intake": intake, "intake_stage": "review"}

    def review_confirm_node(state: WorkflowState) -> dict[str, Any]:
        stage("REVIEW_CONFIRM")
        intake = state.get("intake")
        if intake is None:
            intake = state.get("intake_data") or {}
        try:
            from tfda_context_gate.intake.schemas import PreVisitIntake
            from tfda_context_gate.intake.summary import generate_previsit_summary
            if isinstance(intake, dict):
                obj = PreVisitIntake.model_validate(intake)
            elif isinstance(intake, PreVisitIntake):
                obj = intake
            else:
                obj = PreVisitIntake()
            summary = generate_previsit_summary(obj, request_id=state["request_id"])
            unknown_items = [m for m in obj.known_medications if "待確認" in m] if obj.known_medications else []
            if unknown_items:
                unknown_note = f"\n\n【待確認項目】以下藥品資訊需於看診時請醫師協助確認：{', '.join(unknown_items)}（已標記為待確認，不影響後續流程）"
                review_text = f"請確認以下看診前摘要是否正確：\n{summary.summary_text}{unknown_note}\n\n{summary.disclaimer}\n\n請回覆「確認」以提交，或說明需要修改的內容。"
            else:
                review_text = f"請確認以下看診前摘要是否正確：\n{summary.summary_text}\n\n{summary.disclaimer}\n\n請回覆「確認」以提交，或說明需要修改的內容。"
            with trace.span("REVIEW_CONFIRM", "review_and_confirm") as span:
                span.set(status="NEEDS_CLARIFICATION", question=review_text, missing_information=summary.missing_fields, identified_missing_information=summary.provided_fields)
            return {"intake": obj, "previsit_summary": summary, "question": review_text, "review_confirmed": False, "status": "NEEDS_CONFIRMATION", "final_response": review_text, "termination_reason": "REVIEW_CONFIRMATION_REQUIRED"}
        except Exception as e:
            with trace.span("REVIEW_CONFIRM", "review_and_confirm") as span:
                span.set(status="ERROR", error_type="ReviewConfirmError", error_message=str(e)[:200])
            return {"intake": intake, "question": "請確認您的看診資料是否正確？", "status": "NEEDS_CONFIRMATION", "final_response": "請確認您的看診資料是否正確？"}

    def query_expansion_node(state: WorkflowState) -> dict[str, Any]:
        stage("QUERY_EXPANSION")
        with trace.span("QUERY_EXPANSION", "query_expansion") as span:
            result = _expand_current_query(state["a_result"], current_query=state["current_query"], query_expander=query_expander)
            span.set(retrieval_query=result.retrieval_queries[0], reason_codes=["ORIGINAL_QUERY_PRESERVED" if state["current_query"] == state["original_query"] else "AGENT_REWRITTEN_QUERY"])
        return {"query_expansion": result}

    def rag_node(state: WorkflowState) -> dict[str, Any]:
        stage("RAG")
        attempt = state.get("retrieval_attempt", 0) + 1
        with trace.span("RAG", "retrieval") as span:
            if tool_executor is not None:
                from tfda_context_gate.tool_contract.schemas import ToolRequest, ToolRequestParams
                q = state["query_expansion"].retrieval_queries[0]
                src = tool_source_id or "TFDA_RISK"
                ttype = task_type or state.get("task_type")
                req = ToolRequest(tool_name="EvidenceRetrievalTool", request_id=state["request_id"], params=ToolRequestParams(source_id=src, query=q, filters={}), task_type=ttype)
                tool_result = tool_executor.execute(req)
                from tfda_context_gate.tool_contract.schemas import tool_result_to_rag_result
                result = tool_result_to_rag_result(tool_result, original_query=state["query_expansion"].original_query)
                span.set(retrieval_query=q, retrieved_count=len(result.evidence), retrieved_evidence_ids=[item.evidence_id for item in result.evidence], retrieved_evidence=_retrieved_evidence_trace(result.evidence), retrieval_latency_ms=result.retrieval_latency_ms, retrieval_attempt=attempt, tool_name=tool_result.tool_name, reason_codes=[tool_result.status, tool_result.source_id] if tool_result.source_id else [tool_result.status], decision=tool_result.status)
            else:
                result = retriever.retrieve(state["query_expansion"])
                span.set(retrieval_query=state["query_expansion"].retrieval_queries[0], retrieved_count=len(result.evidence), retrieved_evidence_ids=[item.evidence_id for item in result.evidence], retrieved_evidence=_retrieved_evidence_trace(result.evidence), retrieval_latency_ms=result.retrieval_latency_ms, retrieval_attempt=attempt)
        return {"rag_result": result, "retrieval_attempt": attempt}

    def b_node(state: WorkflowState) -> dict[str, Any]:
        stage("B")
        attempt = state.get("b_attempt", 0) + 1
        ttype = task_type or state.get("task_type")
        intake = state.get("intake") or state.get("intake_data")
        if state.get("intake_stage") in ("stage1", "stage2", "stage3", "review") or ttype == "pre_visit_intake":
            from tfda_context_gate.b_context_gate.schemas import CanonicalBResult
            from tfda_context_gate.intake.schemas import PreVisitIntake as _PI
            try:
                if isinstance(intake, dict):
                    intake_obj = _PI.model_validate(intake)
                elif isinstance(intake, _PI):
                    intake_obj = intake
                else:
                    intake_obj = _PI.model_validate(intake) if intake else _PI()
            except Exception:
                intake_obj = _PI()
            missing: list[str] = []
            for field in ["known_medications", "allergies", "chronic_conditions", "family_history", "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor"]:
                val = getattr(intake_obj, field, None)
                if not val:
                    missing.append(field)
            if not missing or state.get("intake_stage") == "review":
                result = CanonicalBResult(request_id=state["request_id"], decision="PASS", approved_evidence_ids=[], evidence=[], reason_codes=["INTAKE_SUFFICIENT"], identified_missing_information=[], retrieval_feedback={"retrieval_queries": [state["original_query"]]}, relevance="INTAKE", sufficiency="SUFFICIENT", safety="INTAKE_APPROVED")
            else:
                current_stage = state.get("intake_stage", "stage1")
                stage_fields = INTAKE_STAGE_DEFS.get(current_stage, missing[:3])
                stage_missing = [f for f in stage_fields if f in missing]
                if not stage_missing:
                    stage_missing = missing[:3]
                result = CanonicalBResult(request_id=state["request_id"], decision="INSUFFICIENT", approved_evidence_ids=[], evidence=[], reason_codes=["INTAKE_INSUFFICIENT"], identified_missing_information=stage_missing, retrieval_feedback={"retrieval_queries": [state["original_query"]]}, relevance="INTAKE", sufficiency="INSUFFICIENT", safety="NOT_ASSESSED")
            with trace.span("B", "context_gate") as span:
                b_status = "COMPLETED" if result.decision == "PASS" else "INSUFFICIENT"
                span.set(status=b_status, decision=result.decision, approved_evidence_ids=result.approved_evidence_ids, approved_evidence_count=len(result.approved_evidence_ids), b_attempt=attempt, reason_codes=result.reason_codes, identified_missing_information=result.identified_missing_information, relevance=result.relevance, sufficiency=result.sufficiency, conflict=result.conflict, safety=result.safety, step_count=state.get("agent_steps", 0), retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0), actions_taken=state.get("actions_taken", []))
            attempts = list(state.get("previous_attempts", []))
            pending_action = state.get("pending_agent_action")
            if pending_action is not None:
                attempts.append(AgentAttempt(query=state["current_query"], completed_agent_action=pending_action, b_decision=result.decision, b_reason_codes=list(result.reason_codes)[:8], retrieval_outcome=_retrieval_outcome(result)))
            result_state: dict[str, Any] = {"b_result": result, "b_attempt": attempt, "previous_attempts": attempts, "pending_agent_action": None}
            if result.decision == "PASS":
                result_state.update(fallback_reason=None, termination_reason=None)
            else:
                result_state.update(status="FALLBACK", final_response=fallback_response("B_INSUFFICIENT"), fallback_reason="B_INSUFFICIENT", termination_reason="B_INSUFFICIENT")
            return result_state
        with trace.span("B", "context_gate") as span:
            result = context_gate.evaluate(rag_to_b(state["rag_result"]))
            if result.decision == "INSUFFICIENT" and not result.identified_missing_information:
                gaps = DeterministicClarificationPolicy.identify_required_facts(state["original_query"])
                if gaps:
                    result = result.model_copy(update={"identified_missing_information": gaps})
            b_status = "COMPLETED" if result.decision == "PASS" else "INSUFFICIENT" if result.decision == "INSUFFICIENT" else "FALLBACK"
            span.set(status=b_status, decision=result.decision, approved_evidence_ids=result.approved_evidence_ids, approved_evidence_count=len(result.approved_evidence_ids), b_attempt=attempt, reason_codes=result.reason_codes, identified_missing_information=result.identified_missing_information, relevance=result.relevance, sufficiency=result.sufficiency, conflict=result.conflict, safety=result.safety, step_count=state.get("agent_steps", 0), retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0), actions_taken=state.get("actions_taken", []))
        attempts = list(state.get("previous_attempts", []))
        pending_action = state.get("pending_agent_action")
        if pending_action is not None:
            attempts.append(AgentAttempt(query=state["current_query"], completed_agent_action=pending_action, b_decision=result.decision, b_reason_codes=list(result.reason_codes)[:8], retrieval_outcome=_retrieval_outcome(result)))
        result_state = {"b_result": result, "b_attempt": attempt, "previous_attempts": attempts, "pending_agent_action": None}
        if result.decision == "PASS":
            result_state.update(fallback_reason=None, termination_reason=None)
        else:
            result_state.update(status="FALLBACK", final_response=fallback_response("B_INSUFFICIENT" if result.decision == "INSUFFICIENT" else "B_UNSAFE"), fallback_reason="B_INSUFFICIENT" if result.decision == "INSUFFICIENT" else "B_UNSAFE", termination_reason="B_INSUFFICIENT" if result.decision == "INSUFFICIENT" else "B_NON_RECOVERABLE")
        return result_state

    def b_route(state: WorkflowState) -> str:
        result = state["b_result"]
        if result.decision == "PASS":
            return "C"
        if result.decision == "INSUFFICIENT" and (
            agent_planner is not None or result.identified_missing_information
        ):
            return "AGENT_PLANNER"
        ttype = task_type or state.get("task_type")
        if ttype == "pre_visit_intake" or state.get("intake_stage"):
            return "AGENT_PLANNER"
        return "END"

    def planner_node(state: WorkflowState) -> dict[str, Any]:
        stage("AGENT")
        steps = state.get("agent_steps", 0)
        actions = list(state.get("actions_taken", []))
        ttype = task_type or state.get("task_type")
        is_intake = ttype == "pre_visit_intake" or state.get("intake_stage") is not None
        max_steps = 5 if is_intake else agent_limits.max_agent_steps
        if steps >= max_steps:
            decision: AgentDecision = FallbackDecision(action="FALLBACK", reason_code="LIMIT_EXCEEDED")
            with trace.span("AGENT", "planner") as span:
                span.set(status="FALLBACK", agent_action=decision.action, requested_action=decision.action, reason_codes=[decision.reason_code], reason_code=decision.reason_code, requested_reason_code=decision.reason_code, agent_step=steps, step_count=steps, retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0), actions_taken=actions, termination_reason="MAX_AGENT_STEPS_EXCEEDED", model_name=getattr(agent_planner, "model_name", getattr(agent_planner, "name", None)))
            return {"agent_decision": decision, "agent_reason_code": decision.reason_code, "status": "FALLBACK", "final_response": fallback_response("B_INSUFFICIENT"), "fallback_reason": "AGENT_BOUNDED_FALLBACK", "termination_reason": "MAX_AGENT_STEPS_EXCEEDED", "agent_steps": steps}
        context = build_agent_decision_context(original_query=state["original_query"], current_query=state["current_query"], b_result=state["b_result"], previous_attempts=state.get("previous_attempts", []))
        planner_context = context.model_dump(mode="json")
        steps += 1
        try:
            if agent_planner is not None:
                decision = agent_planner.decide(context)
            else:
                decision = DeterministicClarificationPolicy().decide(
                    context,
                    allow_rewrite=query_rewriter is not None,
                )
            action = decision.action
            reason_code = decision.reason_code
            bounded = False
            termination_reason = None
            max_rewrites = 2 if is_intake else agent_limits.max_rewrites
            max_clarifications = 3 if is_intake else agent_limits.max_clarifications
            if action == "REWRITE_QUERY" and state.get("rewrite_count", 0) >= max_rewrites:
                decision = FallbackDecision(action="FALLBACK", reason_code="LIMIT_EXCEEDED")
                bounded = True
                termination_reason = "MAX_REWRITES_EXCEEDED"
            elif action == "ASK_USER" and state.get("clarification_count", 0) >= max_clarifications:
                decision = FallbackDecision(action="FALLBACK", reason_code="LIMIT_EXCEEDED")
                bounded = True
                termination_reason = "MAX_CLARIFICATIONS_EXCEEDED"
            if not bounded:
                termination_reason = "AGENT_SELECTED_FALLBACK" if decision.action == "FALLBACK" else "ACTION_SELECTED"
            with trace.span("AGENT", "planner") as span:
                span.set(status="FALLBACK" if bounded else "COMPLETED", agent_action=decision.action, requested_action=action, reason_codes=[decision.reason_code], reason_code=decision.reason_code, requested_reason_code=reason_code, agent_step=steps, step_count=steps, retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0), actions_taken=actions + [decision.action], identified_missing_information=context.identified_missing_information, planner_context=planner_context, termination_reason=termination_reason, model_name=getattr(agent_planner, "model_name", getattr(agent_planner, "name", None)))
            actions.append(decision.action)
            result: dict[str, Any] = {"agent_decision": decision, "agent_reason_code": decision.reason_code, "agent_steps": steps, "actions_taken": actions}
            if decision.action == "FALLBACK":
                result.update(status="FALLBACK", final_response=fallback_response("B_INSUFFICIENT"), fallback_reason="AGENT_SELECTED_FALLBACK" if termination_reason == "AGENT_SELECTED_FALLBACK" else "AGENT_BOUNDED_FALLBACK", termination_reason=termination_reason or "PLANNER_FALLBACK")
            return result
        except Exception as exc:
            with trace.span("AGENT", "planner") as span:
                span.set(status="ERROR", agent_action="FALLBACK", requested_action=None, reason_codes=["PLANNER_FAILURE"], reason_code="PLANNER_FAILURE", agent_step=steps, step_count=steps, retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0), actions_taken=actions, identified_missing_information=context.identified_missing_information, planner_context=planner_context, termination_reason="PLANNER_FAILURE", error_type=type(exc).__name__, error_message="Planner invocation or schema validation failed", model_name=getattr(agent_planner, "model_name", getattr(agent_planner, "name", None)))
            decision = FallbackDecision(action="FALLBACK", reason_code="PLANNER_FAILURE")
            return {"agent_decision": decision, "agent_reason_code": decision.reason_code, "agent_steps": steps, "actions_taken": actions + ["FALLBACK"], "status": "FALLBACK", "final_response": fallback_response("B_INSUFFICIENT"), "fallback_reason": "AGENT_FAILURE", "termination_reason": "PLANNER_FAILURE"}

    def agent_route(state: WorkflowState) -> str:
        action = state["agent_decision"].action
        if action == "ASK_USER":
            return "ASK_USER"
        if action == "REWRITE_QUERY":
            return "QUERY_REWRITER"
        return "END"

    def ask_user_node(state: WorkflowState) -> dict[str, Any]:
        stage("AGENT")
        decision = state["agent_decision"]
        ttype = task_type or state.get("task_type")
        is_intake = ttype == "pre_visit_intake" or state.get("intake_stage") is not None
        if is_intake:
            missing = decision.missing_information if hasattr(decision, "missing_information") else []  # type: ignore[union-attr]
            if "known_medications" in missing:
                try:
                    from tfda_context_gate.intake.tool import PreVisitIntakeTool
                    tool = PreVisitIntakeTool()
                    original = state.get("original_query", "")
                    current_q = state.get("current_query", original)
                    text_to_check = current_q if current_q != original else original
                    med_attempts = state.get("medication_clarification_attempts", 0)
                    if tool.is_colloquial_medication(text_to_check) and tool.assess_medication_confidence(text_to_check) < 0.7:
                        next_attempt = med_attempts + 1
                        if next_attempt <= 2:
                            q_info = tool.get_medication_clarification_question(next_attempt)
                            question = q_info["question"]
                            with trace.span("ASK_USER", "question_builder") as span:
                                span.set(status="NEEDS_CLARIFICATION", agent_action="ASK_USER", reason_codes=[decision.reason_code], reason_code=decision.reason_code, missing_information=missing, question=question, agent_step=state.get("agent_steps", 0), step_count=state.get("agent_steps", 0), retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0) + 1, actions_taken=state.get("actions_taken", []), termination_reason="NEEDS_CLARIFICATION", intake_stage="stage1")
                            return {"question": question, "clarification_count": state.get("clarification_count", 0) + 1, "status": "NEEDS_CLARIFICATION", "final_response": question, "fallback_reason": None, "termination_reason": "NEEDS_CLARIFICATION", "intake_stage": "stage1", "medication_clarification_attempts": next_attempt, "medication_original_text": text_to_check.strip()[:50]}
                except Exception:
                    pass
            current_stage = None
            for stage_name, fields in INTAKE_STAGE_DEFS.items():
                if any(f in missing for f in fields):
                    current_stage = stage_name
                    break
            if current_stage and current_stage in STAGE_QUESTIONS:
                question = STAGE_QUESTIONS[current_stage]
            else:
                question = build_agent_question(missing)  # type: ignore[union-attr]
            with trace.span("ASK_USER", "question_builder") as span:
                span.set(status="NEEDS_CLARIFICATION", agent_action="ASK_USER", reason_codes=[decision.reason_code], reason_code=decision.reason_code, missing_information=missing, question=question, agent_step=state.get("agent_steps", 0), step_count=state.get("agent_steps", 0), retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0) + 1, actions_taken=state.get("actions_taken", []), termination_reason="NEEDS_CLARIFICATION", intake_stage=current_stage)
            return {"question": question, "clarification_count": state.get("clarification_count", 0) + 1, "status": "NEEDS_CLARIFICATION", "final_response": question, "fallback_reason": None, "termination_reason": "NEEDS_CLARIFICATION", "intake_stage": current_stage}
        with trace.span("ASK_USER", "question_builder") as span:
            role = state["request_context"].declared_role.value
            policy_question = (
                DeterministicClarificationPolicy.build_question(decision.missing_information, role)
                if agent_planner is None
                else None
            )
            question = policy_question or build_agent_question(decision.missing_information)  # type: ignore[union-attr]
            span.set(status="NEEDS_CLARIFICATION", agent_action="ASK_USER", reason_codes=[decision.reason_code], reason_code=decision.reason_code, missing_information=decision.missing_information, question=question, agent_step=state.get("agent_steps", 0), step_count=state.get("agent_steps", 0), retry_count=state.get("rewrite_count", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0) + 1, actions_taken=state.get("actions_taken", []), termination_reason="NEEDS_CLARIFICATION")
        return {"question": question, "clarification_count": state.get("clarification_count", 0) + 1, "status": "NEEDS_CLARIFICATION", "final_response": question, "fallback_reason": None, "termination_reason": "NEEDS_CLARIFICATION"}

    def rewrite_node(state: WorkflowState) -> dict[str, Any]:
        stage("QUERY_REWRITER")
        if query_rewriter is None:
            raise RuntimeError("REWRITE_QUERY selected but no QueryRewriter was configured")
        with trace.span("QUERY_REWRITER", "query_rewriter") as span:
            rewritten = query_rewriter.rewrite(original_query=state["original_query"], current_query=state["current_query"])
            rewritten_query = rewritten.rewritten_query.strip()
            if not rewritten_query:
                raise RuntimeError("QueryRewriter returned an empty query")
            validate_meaning_preserving_rewrite(state["original_query"], rewritten_query)
            span.set(status="COMPLETED", retrieval_query=rewritten_query, current_query=state["current_query"], rewritten_query=rewritten_query, rewrite_attempt=state.get("rewrite_count", 0) + 1, reason_codes=["MEANING_PRESERVING_REWRITE"], step_count=state.get("agent_steps", 0), retry_count=state.get("rewrite_count", 0) + 1, actions_taken=state.get("actions_taken", []), termination_reason="REENTER_RAG_B", model_name=getattr(query_rewriter, "model_name", getattr(query_rewriter, "name", None)))
        return {"current_query": rewritten_query, "rewrite_count": state.get("rewrite_count", 0) + 1, "pending_agent_action": "REWRITE_QUERY"}

    def c_node(state: WorkflowState) -> dict[str, Any]:
        stage("C")
        with trace.span("C", "generator") as span:
            declared_role = state["request_context"].declared_role.value if hasattr(state["request_context"].declared_role, "value") else str(state["request_context"].declared_role)
            intake = state.get("intake")
            intake_data = state.get("intake_data")
            ttype = task_type or state.get("task_type")
            # Detailed clinician draft with intake: when HEALTHCARE_PROFESSIONAL + intake/pre_visit_intake, produce ClinicianEvidenceDraft with 4 sections
            is_clinician = declared_role == "HEALTHCARE_PROFESSIONAL"
            has_intake = intake is not None or intake_data is not None or ttype == "pre_visit_intake" or state.get("intake_stage") == "review" or state.get("previsit_summary") is not None
            if is_clinician and has_intake:
                from tfda_context_gate.c_generator.workflow_adapter import ClinicianDraftGenerator
                from tfda_context_gate.b_context_gate.schemas import CanonicalBResult

                # Resolve intake object
                resolved_intake = intake if intake is not None else intake_data
                if resolved_intake is None and state.get("previsit_summary") is not None:
                    # Extract intake from previsit_summary if available
                    try:
                        ps = state["previsit_summary"]
                        if isinstance(ps, dict):
                            resolved_intake = ps.get("intake")
                        else:
                            resolved_intake = getattr(ps, "intake", None)
                    except Exception:
                        resolved_intake = None
                if resolved_intake is None:
                    resolved_intake = {}
                # Build C input with intake — handle missing b_result for intake flow (B not run)
                b_res = state.get("b_result")
                if b_res is None:
                    # Create dummy B result for intake flow (no evidence, but intake present)
                    b_res = CanonicalBResult(
                        request_id=state["request_id"],
                        decision="PASS",
                        approved_evidence_ids=[],
                        evidence=[],
                        reason_codes=["INTAKE_SUFFICIENT"],
                        identified_missing_information=[],
                        retrieval_feedback={"retrieval_queries": [state["original_query"]]},
                        relevance="INTAKE",
                        sufficiency="SUFFICIENT",
                        safety="INTAKE_APPROVED",
                    )
                c_input = b_to_c(b_res, original_query=state["original_query"])
                # Attach intake to C input for detailed 4-section generation
                try:
                    c_input.intake = resolved_intake
                except Exception:
                    pass
                # Also ensure to_legacy case includes intake for prompt
                active_gen = ClinicianDraftGenerator(max_evidence=getattr(generator, "max_evidence", None))
                raw = active_gen.generate(c_input)
                if hasattr(raw, "model_dump"):
                    raw_dict = raw.model_dump(mode="json")
                else:
                    raw_dict = dict(raw)
                from tfda_context_gate.c_generator.schemas import ClinicianEvidenceDraft

                result = ClinicianEvidenceDraft.model_validate(raw_dict)
                span.set(
                    candidate_decision=result.decision,
                    claim_count=len(result.evidence_summary),
                    evidence_ids=[eid for c in result.evidence_summary for eid in c.evidence_ids],
                    presentation_mode="CLINICIAN_DRAFT",
                    draft_type="clinician_evidence_draft",
                    source_table_count=len(result.source_table),
                    conflicts_count=len(result.conflicts),
                )
                return {"c_result": result}
            # Check if intake flow - generate PreVisitSummary (non-clinician)
            if ttype == "pre_visit_intake" or intake is not None or state.get("intake_stage") == "review" or state.get("previsit_summary") is not None:
                from tfda_context_gate.intake.schemas import PreVisitIntake
                from tfda_context_gate.intake.summary import generate_previsit_summary
                # Use previsit_summary if already generated in review
                if state.get("previsit_summary") is not None:
                    result = state["previsit_summary"]
                    if isinstance(result, dict):
                        from tfda_context_gate.intake.schemas import PreVisitSummary as _PVS
                        result = _PVS.model_validate(result)
                    span.set(
                        candidate_decision="PREVISIT_SUMMARY",
                        claim_count=len(result.timeline),
                        evidence_ids=[],
                        presentation_mode="PREVISIT_SUMMARY",
                        draft_type="previsit_summary",
                    )
                    return {"c_result": result, "previsit_summary": result}
                if intake is None:
                    intake_data_tmp = state.get("intake_data") or {}
                    if isinstance(intake_data_tmp, dict) and intake_data_tmp:
                        intake = PreVisitIntake.model_validate(intake_data_tmp)
                    else:
                        intake = PreVisitIntake()
                elif isinstance(intake, dict):
                    from tfda_context_gate.intake.schemas import PreVisitIntake as _PI
                    intake = _PI.model_validate(intake)
                result = generate_previsit_summary(intake, request_id=state["request_id"])
                span.set(
                    candidate_decision="PREVISIT_SUMMARY",
                    claim_count=len(result.timeline),
                    evidence_ids=[],
                    presentation_mode="PREVISIT_SUMMARY",
                    draft_type="previsit_summary",
                )
                return {"c_result": result, "previsit_summary": result}
            c_input = b_to_c(state["b_result"], original_query=state["original_query"])
            active_generator = generator
            if declared_role == "HEALTHCARE_PROFESSIONAL":
                gen_name = getattr(generator, "name", "")
                if gen_name == "deterministic-c-v2-fixture":
                    from tfda_context_gate.c_generator.workflow_adapter import ClinicianDraftGenerator
                    active_generator = ClinicianDraftGenerator(max_evidence=getattr(generator, "max_evidence", None))
                elif gen_name == "langchain-c-v2-adapter" and getattr(generator, "role", None) != "HEALTHCARE_PROFESSIONAL":
                    generator.role = "HEALTHCARE_PROFESSIONAL"
            raw = active_generator.generate(c_input)
            if hasattr(raw, "model_dump"):
                raw_dict = raw.model_dump(mode="json")
            else:
                raw_dict = dict(raw)
            decision = raw_dict.get("decision")
            if decision == "CLINICIAN_DRAFT" or "evidence_summary" in raw_dict or "source_table" in raw_dict:
                from tfda_context_gate.c_generator.schemas import ClinicianEvidenceDraft
                result = ClinicianEvidenceDraft.model_validate(raw_dict)
                span.set(
                    candidate_decision=result.decision,
                    claim_count=len(result.evidence_summary),
                    evidence_ids=[eid for c in result.evidence_summary for eid in c.evidence_ids],
                    presentation_mode="CLINICIAN_DRAFT",
                    draft_type="clinician_evidence_draft",
                    source_table_count=len(result.source_table),
                    conflicts_count=len(result.conflicts),
                )
            else:
                from tfda_context_gate.c_generator.schemas import EvidenceAwareV2Answer
                result = EvidenceAwareV2Answer.model_validate(raw_dict)
                span.set(
                    candidate_decision=result.decision,
                    claim_count=len(result.supported_claims),
                    evidence_ids=[eid for c in result.supported_claims for eid in c.evidence_ids],
                    presentation_mode="PATIENT_EDUCATION",
                    draft_type="patient_education",
                )
        return {"c_result": result}

    def d_node(state: WorkflowState) -> dict[str, Any]:
        stage("D")
        with trace.span("D", "output_gate") as span:
            c_res = state["c_result"]
            is_previsit = False
            try:
                from tfda_context_gate.intake.schemas import PreVisitSummary
                if isinstance(c_res, PreVisitSummary) or (isinstance(c_res, dict) and "summary_text" in c_res):
                    is_previsit = True
            except Exception:
                pass
            if is_previsit:
                from tfda_context_gate.d_output_gate.gate import run_previsit_output_gate
                from tfda_context_gate.intake.schemas import PreVisitSummary as _PVS
                if isinstance(c_res, dict):
                    c_res = _PVS.model_validate(c_res)
                b_res = state.get("b_result")
                payload = {"request_id": state["request_context"].request_id, "schema_version": "d.v0.1", "a_result": state["a_result"].model_dump(mode="json"), "b_result": b_res.model_dump(mode="json") if hasattr(b_res, "model_dump") else (dict(b_res) if b_res else None), "c_result": c_res.model_dump(mode="json")}
                result = run_previsit_output_gate(payload)
                span.set(status="COMPLETED" if result.decision == "PASS" else "FALLBACK", decision=result.decision, failure_type=result.failure_type, reason_codes=result.reason_codes, failed_claims=[claim.model_dump(mode="json") for claim in result.failed_claims], invalid_evidence_ids=result.invalid_evidence_ids, fallback_reason=None if result.decision == "PASS" else "D_FALLBACK", presentation_mode="PREVISIT_SUMMARY", draft_type="previsit_summary", candidate_decision="PREVISIT_SUMMARY")
                if result.decision == "PASS":
                    if state.get("termination_reason") == "REVIEW_CONFIRMATION_REQUIRED":
                        return {
                            "d_result": result,
                            "status": "NEEDS_CONFIRMATION",
                            "final_response": state.get("question") or result.final_response,
                        }
                    return {"d_result": result, "status": "COMPLETED", "final_response": result.final_response}
                return {"d_result": result, "status": "FALLBACK", "final_response": result.final_response, "fallback_reason": "D_FALLBACK", "termination_reason": "D_FALLBACK"}
            is_clinician = getattr(c_res, "decision", None) == "CLINICIAN_DRAFT" or hasattr(c_res, "source_table")
            b_res_for_d = state.get("b_result")
            if b_res_for_d is not None:
                payload = c_to_d(request_id=state["request_context"].request_id, a_result=state["a_result"], b_result=b_res_for_d, c_result=c_res)
            else:
                c_dict = c_res.model_dump(mode="json") if hasattr(c_res, "model_dump") else dict(c_res)
                try:
                    from tfda_context_gate.workflow.adapters import _normalize_c_answer_for_d

                    c_dict = _normalize_c_answer_for_d(c_dict, request_id=state["request_context"].request_id)
                except Exception:
                    pass
                payload = {"request_id": state["request_context"].request_id, "schema_version": "d.v0.1", "a_result": state["a_result"].model_dump(mode="json"), "b_result": None, "c_result": c_dict}
            result = run_output_gate(payload, verifier=verifier)
            span.set(status="COMPLETED" if result.decision == "PASS" else "FALLBACK", decision=result.decision, failure_type=result.failure_type, reason_codes=result.reason_codes, failed_claims=[claim.model_dump(mode="json") for claim in result.failed_claims], invalid_evidence_ids=result.invalid_evidence_ids, fallback_reason=None if result.decision == "PASS" else "D_FALLBACK", presentation_mode="CLINICIAN_DRAFT" if is_clinician else "PATIENT_EDUCATION", draft_type="clinician_evidence_draft" if is_clinician else "patient_education", candidate_decision=getattr(c_res, "decision", None))
            if result.decision == "PASS":
                final = result.final_response
                has_intake = bool(state.get("intake") or state.get("intake_data") or state.get("intake_stage"))
                ttype_check = task_type or state.get("task_type") or getattr(state.get("a_result"), "task_type", None)
                if ttype_check == "pre_visit_intake":
                    has_intake = True
                if should_append_post_answer_invitation(state.get("a_result"), has_intake):
                    final = append_post_answer_invitation(final, has_intake=False)
                return {"d_result": result, "status": "COMPLETED", "final_response": final}
            return {"d_result": result, "status": "FALLBACK", "final_response": result.final_response, "fallback_reason": "D_FALLBACK", "termination_reason": "D_FALLBACK"}

    def intake_stage1_route(state: WorkflowState) -> str:
        if state.get("status") == "NEEDS_CLARIFICATION":
            return "END"
        return "INTAKE_STAGE2"
    def intake_stage2_route(state: WorkflowState) -> str:
        if state.get("status") == "NEEDS_CLARIFICATION":
            return "END"
        return "INTAKE_STAGE3"
    def intake_stage3_route(state: WorkflowState) -> str:
        if state.get("status") == "NEEDS_CLARIFICATION":
            return "END"
        return "REVIEW_CONFIRM"

    graph = StateGraph(WorkflowState)
    graph.add_node("A", a_node)
    graph.add_node("INTAKE_CHECK", intake_check_node)
    graph.add_node("INTAKE_STAGE1", intake_stage1_node)
    graph.add_node("INTAKE_STAGE2", intake_stage2_node)
    graph.add_node("INTAKE_STAGE3", intake_stage3_node)
    graph.add_node("REVIEW_CONFIRM", review_confirm_node)
    graph.add_node("QUERY_EXPANSION", query_expansion_node)
    graph.add_node("RAG", rag_node)
    graph.add_node("B", b_node)
    graph.add_node("AGENT_PLANNER", planner_node)
    graph.add_node("ASK_USER", ask_user_node)
    graph.add_node("QUERY_REWRITER", rewrite_node)
    graph.add_node("C", c_node)
    graph.add_node("D", d_node)
    graph.add_edge(START, "A")
    graph.add_conditional_edges("A", a_route, {"INTAKE_CHECK": "INTAKE_CHECK", "QUERY_EXPANSION": "QUERY_EXPANSION", "END": END})
    graph.add_conditional_edges("INTAKE_CHECK", intake_route, {"INTAKE_STAGE1": "INTAKE_STAGE1", "INTAKE_STAGE2": "INTAKE_STAGE2", "INTAKE_STAGE3": "INTAKE_STAGE3", "REVIEW_CONFIRM": "REVIEW_CONFIRM"})
    graph.add_conditional_edges("INTAKE_STAGE1", intake_stage1_route, {"INTAKE_STAGE2": "INTAKE_STAGE2", "END": END})
    graph.add_conditional_edges("INTAKE_STAGE2", intake_stage2_route, {"INTAKE_STAGE3": "INTAKE_STAGE3", "END": END})
    graph.add_conditional_edges("INTAKE_STAGE3", intake_stage3_route, {"REVIEW_CONFIRM": "REVIEW_CONFIRM", "END": END})
    graph.add_edge("REVIEW_CONFIRM", "C")
    graph.add_edge("QUERY_EXPANSION", "RAG")
    graph.add_edge("RAG", "B")
    graph.add_conditional_edges("B", b_route, {"C": "C", "AGENT_PLANNER": "AGENT_PLANNER", "END": END})
    graph.add_conditional_edges("AGENT_PLANNER", agent_route, {"ASK_USER": "ASK_USER", "QUERY_REWRITER": "QUERY_REWRITER", "END": END})
    graph.add_edge("ASK_USER", END)
    graph.add_edge("QUERY_REWRITER", "QUERY_EXPANSION")
    graph.add_edge("C", "D")
    graph.add_edge("D", END)
    return graph.compile(), runtime_stage
