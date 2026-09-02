"""LINE webhook with medication bag image handling.

FastAPI app at line_bot/app.py that handles LINE Webhook (X-Line-Signature),
text and image messages, calls OCR service (MedicationBagOCRService) and
workflow, returns reply, with local test via image_bytes.

Design:
- POST /callback verifies X-Line-Signature (HMAC-SHA256), handles TextMessage
  and ImageMessage.
- For image: download via MessagingApiBlob, call MedicationBagOCRService.extract,
  merge into intake_data (known_medications), then run_workflow with image_bytes.
  Never stores raw image in WorkflowState (only passes image_bytes to runner
  which processes via _process_ocr_images and discards raw bytes).
- For text: normal run_workflow.
- Supports stream_workflow for streaming reply (internal helper + SSE endpoint).
- Local test via image_bytes without LINE server: simulate_text_message,
  simulate_image_message, simulate_front_back_images.
- B/D gates mandatory: never bypassed, workflow always runs through full A-E
  graph with DeterministicContextGate / SemanticVerifier.

Env: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN (or LINE_CHANNEL_ACCESS_TOKEN)
     Also supports LINE_ACCESS_TOKEN alias. Loaded via .env if present.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sys
import time
import unicodedata
import uuid
import logging
import threading
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)
HONEST_FALLBACK_PUSH_TEXT = "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？"
QUEUED_FALLBACK_TEXT = "查詢排隊中，稍後推送"
ASYNC_PLACEHOLDER_REPLY = "幫你查衛教資料中，查到後立刻傳給你。"
# Canonical async boundary classification; legacy reason codes remain
# readable for stored workflow records and compatibility tests.
DEPENDENCY_OR_TIMEOUT_REASON = "DEPENDENCY_OR_TIMEOUT"
ASYNC_FORMAL_TIMEOUT_S = float(os.getenv("ASYNC_FORMAL_TIMEOUT_S", "120"))
_pushed_events: set[str] = set()
# An in-flight reservation closes the check/send race.  It is released on a
# failed transport so a later retry can safely attempt the same event again;
# successful sends move to ``_pushed_events`` permanently for this process.
_pushing_events: set[str] = set()
_marker_pending_events: set[str] = set()
_marker_retrying_events: set[str] = set()
_pushed_lock = threading.Lock()
_async_jobs: set[str] = set()
_async_jobs_lock = threading.Lock()
_FORMAL_SEMAPHORE = threading.Semaphore(5)
ASYNC_ADMISSION_FALLBACK_TEXT = "目前同時查詢較多，這次無法完成查詢，請稍後再試。"

TEXT_DEDUP_TTL_S = 120
TEXT_DEDUP_TTL_SHORT_S = 10
TEXT_DEDUP_REPLY = "這題正在幫你查了，稍候"
TEXT_DEDUP_REPLY_WELCOME = "又見面了～有什麼想繼續的？"
_text_dedup: dict[tuple[str, str], float] = {}
_text_dedup_lock = threading.Lock()
import re as _re_dup
_EMPATHY_DUP_RE = _re_dup.compile(r"不人性化|好笨|很怪|無言|敷衍|不友善|冷淡|機械", _re_dup.IGNORECASE)

SEMANTIC_ROUTER_TIMEOUT_S = 0.2
_app_semantic_router: Any | None = None
_app_semantic_router_config: Any | None = None
_app_semantic_router_init_attempted = False


def _get_app_requested_route_mode() -> str:
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


def _get_app_route_mode() -> str:
    requested = _get_app_requested_route_mode()
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


def _app_guarded_fallback_reason() -> str | None:
    requested = _get_app_requested_route_mode()
    effective = _get_app_route_mode()
    if requested == "guarded" and effective != "guarded":
        try:
            from tfda_context_gate.semantic_router.approval import get_effective_route_mode as _eff2

            _, reason, _ = _eff2(requested)
            return reason or "GUARDED_DOWNGRADED_UNKNOWN"
        except Exception:
            return "GUARDED_DOWNGRADED_UNKNOWN"
    return None


def _app_record_guarded_downgrade(fallback_reason: str) -> None:
    try:
        from tfda_context_gate.e_observability.tracer import TraceRecorder

        tr = TraceRecorder(request_id="app-guarded-downgrade", declared_role=None, original_query=None)
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


def _app_should_use_semantic_router() -> bool:
    return _get_app_route_mode() != "off"


def _get_app_semantic_router() -> Any | None:
    global _app_semantic_router, _app_semantic_router_config, _app_semantic_router_init_attempted
    if _app_semantic_router_init_attempted:
        return _app_semantic_router
    try:
        from tfda_context_gate.semantic_router.config import SemanticRouterConfig as _SRC

        _cfg = _SRC.from_env()
        _app_semantic_router_config = _cfg
        if _cfg.mode != "off":
            try:
                from tfda_context_gate.semantic_router.factory import build_semantic_router as _bsr

                _app_semantic_router = _bsr(_cfg)
            except Exception as _e:
                logger.warning("app semantic router init failed (degraded): %s", _e)
                _app_semantic_router = None
        _app_semantic_router_init_attempted = True
    except ImportError:
        _app_semantic_router = None
        _app_semantic_router_init_attempted = True
    except Exception as _e:
        logger.warning("app semantic router config failed: %s", _e)
        _app_semantic_router = None
        _app_semantic_router_init_attempted = True
    return _app_semantic_router


def _app_semantic_predict_with_timeout(text: str, timeout_s: float = SEMANTIC_ROUTER_TIMEOUT_S) -> Any | None:
    if not _app_should_use_semantic_router():
        return None
    router = _get_app_semantic_router()
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
                try:
                    from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation as _Obs

                    return _Obs(
                        route="UNKNOWN",
                        confidence=0.0,
                        margin=0.0,
                        latency_ms=timeout_s * 1000,
                        mode=_get_app_route_mode(),
                        degraded=True,
                    )
                except Exception:
                    return None
            except Exception:
                return None
    except Exception:
        try:
            return fn(text)
        except Exception:
            return None


def _is_short_ttl_text(text: str) -> bool:
    try:
        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _R
        from tfda_context_gate.workflow.intake_router import is_welcome_trigger as _is_w
        if _is_w(text):
            return True
        if _R.is_chit_chat_text(text):
            return True
        if _R.is_identity_text(text):
            return True
        if _EMPATHY_DUP_RE.search(text):
            return True
    except Exception:
        pass
    try:
        import unicodedata as _ud2
        n = _ud2.normalize("NFKC", text).strip()
        if n in ("你好", "您好", "哈囉", "嗨", "hi", "hello"):
            return True
    except Exception:
        pass
    return False


def _dedup_ttl_for(text: str) -> int:
    return TEXT_DEDUP_TTL_SHORT_S if _is_short_ttl_text(text) else TEXT_DEDUP_TTL_S


def _dedup_reply_for(text: str) -> str:
    return TEXT_DEDUP_REPLY_WELCOME if _is_short_ttl_text(text) else TEXT_DEDUP_REPLY


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

# Ensure project root is on sys.path for direct execution (python line_bot/app.py)
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

# ── Env loading ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv

    # Load from project root .env if exists
    _env_path = Path(__file__).resolve().parents[1] / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path)
    else:
        load_dotenv()
except Exception:
    pass

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = (
    os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    or os.getenv("LINE_ACCESS_TOKEN")
    or os.getenv("LINE_CHANNEL_TOKEN")
    or ""
)

# ── FastAPI app ─────────────────────────────────────────────────────────────
app = FastAPI(title="TFDA Diabetes LINE Bot", version="0.1.0")
_conversation_orchestrator: Any | None = None


@app.on_event("startup")
def _preheat_vector_store() -> None:
    def _warm() -> None:
        try:
            from tfda_context_gate.rag.tfda_retriever import TFDADrugSafetyRetriever

            retriever = TFDADrugSafetyRetriever(embedding_model="ollama/bge-m3:latest")
            retriever._ensure_store()
        except Exception:
            pass

    try:
        threading.Thread(target=_warm, daemon=True).start()
    except Exception:
        pass


def _get_conversation_orchestrator() -> Any | None:
    """設定 identity key 時啟用持久化 session；未設定則保留既有單輪模式。"""
    global _conversation_orchestrator
    if _conversation_orchestrator is not None:
        return _conversation_orchestrator
    identity_key = os.getenv("LINE_IDENTITY_HASH_KEY", "")
    if len(identity_key) < 16:
        # Demo migration fallback：以 domain-separated HMAC 從 channel secret 派生，
        # 不直接把 webhook secret 當資料索引 key；正式環境仍建議提供獨立 key。
        channel_secret = _get_secret()
        if channel_secret:
            identity_key = hmac.new(
                channel_secret.encode("utf-8"),
                b"tfda-line-product-session-identity-v1",
                hashlib.sha256,
            ).hexdigest()
    if len(identity_key) < 16:
        return None
    from tfda_context_gate.line_orchestration import ConversationOrchestrator
    from tfda_context_gate.product_session import SQLiteProductSessionRepository

    configured_path = Path(os.getenv("LINE_SESSION_DB_PATH", "data/processed/line_sessions.sqlite3"))
    db_path = configured_path if configured_path.is_absolute() else _project_root / configured_path
    _conversation_orchestrator = ConversationOrchestrator(
        SQLiteProductSessionRepository(db_path),
        identity_hash_key=identity_key,
    )
    return _conversation_orchestrator


def _require_demo_clinician(clinician_id: str) -> Any:
    if os.getenv("LINE_DEMO_MODE", "false").strip().lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=503, detail="Demo clinician authentication is disabled")
    allowed = {value.strip() for value in os.getenv("DEMO_CLINICIAN_IDS", "").split(",") if value.strip()}
    if not clinician_id or clinician_id not in allowed:
        raise HTTPException(status_code=403, detail="Clinician identity is not verified")
    orchestrator = _get_conversation_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")
    from tfda_context_gate.access_control import ActorAccessContext
    return ActorAccessContext(
        principal_id_hash=orchestrator.principal_hash(f"clinician:{clinician_id}"),
        actor_role="PRACTITIONER",
        frontend_persona="CLINICIAN",
        authorization_status="CLINICIAN_VERIFIED",
        permission_scopes=["VIEW_GRANTED_CLINICAL_SUMMARY", "VIEW_EVIDENCE"],
    )


class CreateShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_clinician_id: str | None = Field(default=None, max_length=128)


class RedeemShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=256)


def _demo_identity_headers_enabled() -> bool:
    return os.getenv("LINE_DEMO_ALLOW_ID_HEADERS", "false").strip().lower() in {"1", "true", "yes"}


def _demo_clinician_enabled() -> bool:
    """Return only the safe capability flag; never expose the allowlist itself."""
    mode_enabled = os.getenv("LINE_DEMO_MODE", "false").strip().lower() in {"1", "true", "yes"}
    allowlist_configured = bool(
        {value.strip() for value in os.getenv("DEMO_CLINICIAN_IDS", "").split(",") if value.strip()}
    )
    return mode_enabled and allowlist_configured


def _unsigned_webhook_enabled() -> bool:
    return os.getenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "false").strip().lower() in {"1", "true", "yes"}


def _verify_line_id_token(id_token: str) -> str:
    """由 LINE Login v2.1 驗證 ID token，僅取回官方確認的 subject。"""
    channel_id = os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip()
    if not channel_id:
        raise HTTPException(status_code=503, detail="LINE Login is not configured")
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({"id_token": id_token, "client_id": channel_id}).encode("utf-8")
    line_request = urllib.request.Request(
        "https://api.line.me/oauth2/v2.1/verify",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(line_request, timeout=5) as response:
            claims = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="LINE identity verification failed") from exc
    subject = claims.get("sub") if isinstance(claims, dict) else None
    audience = str(claims.get("aud", "")) if isinstance(claims, dict) else ""
    if not subject or audience != channel_id:
        raise HTTPException(status_code=401, detail="Invalid LINE identity")
    return str(subject)


def _resolve_patient_line_user_id(authorization: str, demo_user_id: str) -> str:
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            return _verify_line_id_token(token)
    if _demo_identity_headers_enabled() and demo_user_id:
        return demo_user_id
    raise HTTPException(status_code=401, detail="Verified LINE LIFF identity is required")


# ── Signature verification ──────────────────────────────────────────────────
def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify X-Line-Signature = base64(HMAC-SHA256(secret, body))."""
    if not secret or not signature:
        return False
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _get_secret() -> str:
    return os.getenv("LINE_CHANNEL_SECRET", "") or LINE_CHANNEL_SECRET


def _get_access_token() -> str:
    return (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or os.getenv("LINE_ACCESS_TOKEN")
        or os.getenv("LINE_CHANNEL_TOKEN")
        or LINE_CHANNEL_ACCESS_TOKEN
    )


# ── OCR + Workflow helpers (testable without LINE server) ───────────────────
def _build_request_context(
    text: str,
    *,
    request_id: str | None = None,
    declared_role: str = "PATIENT",
) -> dict[str, Any]:
    from tfda_context_gate.access_control import (
        actor_role_from_declared_role,
        declared_role_from_actor_role,
    )

    normalized_role = declared_role_from_actor_role(
        actor_role_from_declared_role(declared_role)
    )
    return {
        "request_id": request_id or f"line-{uuid.uuid4().hex[:8]}",
        "schema_version": "a.v0.1",
        "user_raw_input": text,
        "declared_role": normalized_role,
        "language": "zh-TW",
    }


def handle_text_message(
    text: str,
    *,
    request_id: str | None = None,
    declared_role: str = "PATIENT",
    intake_data: Any | None = None,
    use_stream: bool = False,
    chunk_size: int = 20,
    sse_format: bool = False,
    use_formal: bool = True,
) -> Any:
    """Handle text message via workflow (no image). Returns WorkflowResult or iterator if streaming."""
    from tfda_context_gate.workflow.runner import run_workflow, stream_workflow

    req = _build_request_context(text, request_id=request_id, declared_role=declared_role)
    if use_stream:
        return stream_workflow(
            req,
            intake_data=intake_data,
            chunk_size=chunk_size,
            sse_format=sse_format,
            use_formal=use_formal,
        )
    return run_workflow(req, intake_data=intake_data, use_formal=use_formal)


def handle_image_message(
    image_bytes: bytes,
    *,
    text_fallback: str = "請幫我辨識藥袋上的藥品",
    request_id: str | None = None,
    declared_role: str = "PATIENT",
    intake_data: Any | None = None,
    ocr_service: Any | None = None,
    use_stream: bool = False,
    chunk_size: int = 20,
    sse_format: bool = False,
    use_formal: bool = True,
) -> Any:
    """Handle single image message: OCR -> merge into intake_data -> workflow.

    Never stores raw image in WorkflowState; only passes image_bytes to runner
    which extracts meds via MedicationBagOCRService and merges into intake_data.
    """
    from tfda_context_gate.workflow.runner import run_workflow, stream_workflow

    req = _build_request_context(text_fallback, request_id=request_id, declared_role=declared_role)
    # Pass image_bytes to workflow; runner will call MedicationBagOCRService internally
    # and merge into intake_data without storing raw bytes in state.
    if use_stream:
        return stream_workflow(
            req,
            intake_data=intake_data,
            image_bytes=image_bytes,
            ocr_service=ocr_service,
            chunk_size=chunk_size,
            sse_format=sse_format,
            use_formal=use_formal,
        )
    return run_workflow(
        req,
        intake_data=intake_data,
        image_bytes=image_bytes,
        ocr_service=ocr_service,
        use_formal=use_formal,
    )


def handle_front_back_images(
    front_bytes: bytes | None,
    back_bytes: bytes | None,
    *,
    text_fallback: str = "請幫我辨識藥袋上的藥品",
    request_id: str | None = None,
    declared_role: str = "PATIENT",
    intake_data: Any | None = None,
    ocr_service: Any | None = None,
    use_stream: bool = False,
    chunk_size: int = 20,
    sse_format: bool = False,
    use_formal: bool = True,
) -> Any:
    """Handle front/back medication bag images: OCR both, merge, workflow.

    Uses MedicationBagOCRService.extract_front_back internally via runner's
    _process_ocr_images. Never stores raw image in WorkflowState.
    """
    from tfda_context_gate.workflow.runner import run_workflow, stream_workflow

    req = _build_request_context(text_fallback, request_id=request_id, declared_role=declared_role)
    if use_stream:
        return stream_workflow(
            req,
            intake_data=intake_data,
            image_bytes_front=front_bytes,
            image_bytes_back=back_bytes,
            ocr_service=ocr_service,
            chunk_size=chunk_size,
            sse_format=sse_format,
            use_formal=use_formal,
        )
    return run_workflow(
        req,
        intake_data=intake_data,
        image_bytes_front=front_bytes,
        image_bytes_back=back_bytes,
        ocr_service=ocr_service,
        use_formal=use_formal,
    )


def stream_text_reply(
    text: str,
    *,
    request_id: str | None = None,
    chunk_size: int = 20,
    sse_format: bool = False,
    use_formal: bool = True,
) -> Iterator[str]:
    """Streaming helper for text: yields chunks via stream_workflow."""
    gen = handle_text_message(text, request_id=request_id, use_stream=True, chunk_size=chunk_size, sse_format=sse_format, use_formal=use_formal)
    yield from gen  # type: ignore[misc]


def stream_image_reply(
    image_bytes: bytes,
    *,
    text_fallback: str = "請幫我辨識藥袋上的藥品",
    request_id: str | None = None,
    chunk_size: int = 20,
    sse_format: bool = False,
    use_formal: bool = True,
) -> Iterator[str]:
    """Streaming helper for image: OCR + stream_workflow."""
    gen = handle_image_message(
        image_bytes, text_fallback=text_fallback, request_id=request_id, use_stream=True, chunk_size=chunk_size, sse_format=sse_format, use_formal=use_formal
    )
    yield from gen  # type: ignore[misc]


def _format_formal_push_text(workflow: Any, original_text: str = "") -> str:
    try:
        status = getattr(workflow, "status", None) or (workflow.get("status") if isinstance(workflow, dict) else None)
        final = getattr(workflow, "final_response", None) or (workflow.get("final_response") if isinstance(workflow, dict) else "") or ""
        fallback_reason = getattr(workflow, "fallback_reason", None) or (workflow.get("fallback_reason") if isinstance(workflow, dict) else None) or ""
        if status == "COMPLETED" and final.strip():
            base = final.strip()
            sources: list[str] = []
            rag = getattr(workflow, "rag_result", None) or (workflow.get("rag_result") if isinstance(workflow, dict) else None) or {}
            if isinstance(rag, dict):
                for ev in (rag.get("evidences") or rag.get("chunks") or [])[:2]:
                    if isinstance(ev, dict):
                        src = ev.get("source") or ev.get("doc_id") or ev.get("title")
                        if src:
                            sources.append(str(src))
            c_res = getattr(workflow, "c_result", None) or (workflow.get("c_result") if isinstance(workflow, dict) else None) or {}
            if isinstance(c_res, dict):
                for k in ("source", "sources", "evidence_id"):
                    v = c_res.get(k)
                    if v and str(v) not in sources:
                        if isinstance(v, list):
                            sources.extend([str(x) for x in v[:2] if str(x) not in sources])
                        else:
                            sources.append(str(v))
            if sources:
                return f"{base}\n\n資料來源：{'、'.join(sources[:2])}"
            return base
        if status == "FALLBACK" and fallback_reason in {"B_INSUFFICIENT", "FORMAL_TIMEOUT", "C_FAILURE", "SYSTEM_DEPENDENCY", "B_UNSAFE", DEPENDENCY_OR_TIMEOUT_REASON}:
            return HONEST_FALLBACK_PUSH_TEXT
        if final.strip():
            return final.strip()
        return HONEST_FALLBACK_PUSH_TEXT
    except Exception:
        return HONEST_FALLBACK_PUSH_TEXT


def _is_duplicate_push(event_id: str | None, repository: Any | None = None) -> bool:
    if not event_id:
        return False
    marker_retry_needed = False
    with _pushed_lock:
        if event_id in _pushed_events:
            marker_retry_needed = event_id in _marker_pending_events
    if marker_retry_needed:
        # The external transport already acknowledged this event.  Repairing
        # its durable marker must never send the LINE message a second time.
        _mark_event_pushed(repository, event_id)
        return True
    if repository is not None:
        try:
            record = repository.get_webhook_event(event_id)
            durable = bool(
                record is not None
                and isinstance(record.result, dict)
                and record.result.get("pushed") is True
            )
            if durable:
                with _pushed_lock:
                    _pushed_events.add(event_id)
                    _marker_pending_events.discard(event_id)
            return durable
        except Exception:
            pass
    return False


def _mark_pushed(event_id: str | None) -> None:
    if not event_id:
        return
    with _pushed_lock:
        _pushed_events.add(event_id)


def _begin_push(event_id: str | None) -> bool:
    """Atomically reserve an event for one transport attempt."""

    if not event_id:
        return True
    with _pushed_lock:
        if event_id in _pushed_events or event_id in _pushing_events:
            return False
        _pushing_events.add(event_id)
        return True


def _finish_push(event_id: str | None, *, success: bool) -> None:
    if not event_id:
        return
    with _pushed_lock:
        _pushing_events.discard(event_id)
        if success:
            _pushed_events.add(event_id)


def _push_text(
    line_user_id: str,
    text: str,
    event_id: str | None = None,
    *,
    deadline_guard: Any | None = None,
) -> bool:
    if deadline_guard is None:
        try:
            from tfda_context_gate.e_observability.deadline import current_deadline_guard

            deadline_guard = current_deadline_guard()
        except Exception:
            deadline_guard = None
    if not line_user_id or not text:
        return False
    if deadline_guard is not None and deadline_guard.should_abort():
        return False
    if not _begin_push(event_id):
        return False
    if len(text) > 4900:
        text = text[:4900] + "…"
    success = False
    try:
        for attempt in range(2):
            try:
                if deadline_guard is not None and deadline_guard.should_abort():
                    return False
                api = _get_messaging_api()
                if api is None:
                    return False
                from linebot.v3.messaging import PushMessageRequest, TextMessage

                kwargs: dict[str, Any] = {}
                # LINE's generated SDK exposes both a retry key and a native
                # request timeout.  Inspecting the callable keeps test fakes
                # and older SDKs compatible without guessing unsupported args.
                try:
                    import inspect

                    params = inspect.signature(api.push_message).parameters
                    if "x_line_retry_key" in params or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                    ):
                        if event_id:
                            from tfda_context_gate.line_orchestration.retry_key import make_line_retry_key

                            kwargs["x_line_retry_key"] = make_line_retry_key(event_id)
                    if "_request_timeout" in params or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                    ):
                        remaining = deadline_guard.remaining_s() if deadline_guard is not None else None
                        if remaining is not None:
                            kwargs["_request_timeout"] = max(0.001, remaining)
                except Exception:
                    pass
                api.push_message(
                    PushMessageRequest(to=line_user_id, messages=[TextMessage(text=text)]),
                    **kwargs,
                )
                # A transport that returned success owns the event even if
                # the surrounding deadline expires immediately afterwards;
                # marking it failed would make a retry duplicate a real send.
                success = True
                return True
            except Exception as exc:
                logger.warning("push_message failed attempt %s for %s: %s", attempt + 1, event_id, exc)
                if attempt == 0:
                    continue
                return False
    finally:
        _finish_push(event_id, success=success)
    return False


def _mark_event_pushed(orchestrator: Any | None, event_id: str | None) -> bool:
    """Persist the post-transport push marker when the repository supports it."""

    if orchestrator is None or not event_id:
        return False
    try:
        repository = getattr(orchestrator, "repository", None)
        if repository is None and callable(getattr(orchestrator, "mark_webhook_event_pushed", None)):
            repository = orchestrator
        marker = getattr(repository, "mark_webhook_event_pushed", None)
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
        finally:
            with _pushed_lock:
                _marker_retrying_events.discard(event_id)
    except Exception:
        with _pushed_lock:
            _marker_pending_events.add(event_id)
        logger.warning("could not persist push marker for %s", event_id)
        return False


def _maybe_record_question_for_doctor(
    orchestrator: Any,
    line_user_id: str,
    original_text: str,
    workflow: Any,
    *,
    deadline_guard: Any | None = None,
) -> None:
    try:
        if deadline_guard is None:
            try:
                from tfda_context_gate.e_observability.deadline import current_deadline_guard

                deadline_guard = current_deadline_guard()
            except Exception:
                deadline_guard = None
        if deadline_guard is not None and deadline_guard.should_abort():
            return
        status = getattr(workflow, "status", None) or (workflow.get("status") if isinstance(workflow, dict) else None)
        if status != "FALLBACK":
            return
        push_text = _format_formal_push_text(workflow, original_text)
        if push_text != HONEST_FALLBACK_PUSH_TEXT:
            return
        if orchestrator is None:
            return
        try:
            session = orchestrator.session_for_user(line_user_id)
        except Exception:
            session = None
        if session is None:
            try:
                session = orchestrator._load_or_create(line_user_id)
            except Exception:
                return
        q = original_text.strip()[:200]
        if not q or q in session.intake_snapshot.questions_for_doctor:
            return
        if len(session.intake_snapshot.questions_for_doctor) >= 10:
            return
        if deadline_guard is not None and deadline_guard.should_abort():
            return
        # converge to single service method: only set pending, not direct append
        try:
            from tfda_context_gate.product_session.schemas import PendingAction
            import datetime
            pending = PendingAction(type="PENDING_CONFIRM_QUESTION", proposal=q, created_at=datetime.datetime.now(datetime.timezone.utc))
            orchestrator.repository.save(session.model_copy(update={"pending_action": pending, "pending_question_proposal": q}, deep=True), expected_version=session.version)
        except Exception:
            pass
    except Exception:
        pass


def _should_use_async_formal(text: str, task_type: str | None = None) -> bool:
    try:
        from tfda_context_gate.line_orchestration.orchestrator import _orch_should_use_formal

        return _orch_should_use_formal(text, task_type)
    except Exception:
        return False


def _should_schedule_formal_push(orchestrator: Any, line_user_id: str, text: str) -> bool:
    """Keep active-intake answers inside the product state machine.

    The webhook-level keyword check cannot distinguish a medication answer
    (for example, ``沒有打胰島素``) from a medication education request.  The
    orchestrator owns that distinction because it can see the persisted
    intake state.  On any lookup error we fail closed to the synchronous
    orchestrator path instead of bypassing intake through async RAG.
    """

    if orchestrator is None or not getattr(orchestrator, "use_formal", False):
        return False
    try:
        session = orchestrator.session_for_user(line_user_id)
        if session is not None:
            eligible = getattr(orchestrator, "_is_async_narrow_eligible", None)
            if callable(eligible):
                return bool(eligible(session, text))
    except Exception:
        return False
    return _should_use_async_formal(text, None)


def _schedule_formal_push(
    orchestrator: Any,
    line_user_id: str,
    event_id: str,
    text: str,
) -> None:
    from tfda_context_gate.e_observability.deadline import DeadlineGuard, deadline_scope

    if _is_duplicate_push(event_id, getattr(orchestrator, "repository", None)):
        return

    # Start the deadline at admission, so semaphore queue time is part of the
    # user-facing budget rather than an unbounded prelude.
    job_guard = DeadlineGuard(ASYNC_FORMAL_TIMEOUT_S)
    now = time.time()
    norm = _normalize_text(text)
    key = (line_user_id, norm)
    # Durable pending replay is event-scoped and must bypass the text-level
    # dedup cache.  A failed transport may leave the event pending while the
    # cache still contains the original turn; replay must be able to retry it.
    pending_replay = False
    try:
        record = orchestrator.repository.get_webhook_event(event_id)
        pending_replay = bool(
            record is not None
            and record.status == "COMPLETED"
            and isinstance(record.result, dict)
            and record.result.get("status") in {"ASYNC_PENDING", "ASYNC_PLACEHOLDER"}
            and record.result.get("pushed") is not True
        )
    except Exception:
        pending_replay = False
    if not pending_replay:
        with _text_dedup_lock:
            expired = [k for k, ts in list(_text_dedup.items()) if now - ts > TEXT_DEDUP_TTL_S]
            for k in expired:
                _text_dedup.pop(k, None)
            ts = _text_dedup.get(key)
            if ts is not None and now - ts < TEXT_DEDUP_TTL_S:
                return
            if norm:
                _text_dedup[key] = now

    # A replay in the same process must not start a second job for the same
    # webhook event.  The durable ``pushed`` marker remains authoritative
    # after a process restart.
    with _async_jobs_lock:
        if event_id in _async_jobs:
            return
        _async_jobs.add(event_id)

    def _execute_formal_and_push() -> None:
        from tfda_context_gate.e_observability.deadline import run_with_deadline

        # This guard bounds the whole background job, including retries.  A
        # worker that returns after this point is abandoned and cannot push or
        # mutate ProductSession.
        try:
            wf = None
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    if job_guard.should_abort():
                        return
                    remaining = job_guard.remaining_s()
                    if remaining is None or remaining <= 0:
                        job_guard.mark_abandoned()
                        return
                    if orchestrator is not None:
                        session = orchestrator.session_for_user(line_user_id)
                        declared_role = "PATIENT"
                        if session is not None:
                            try:
                                from tfda_context_gate.line_orchestration.orchestrator import ConversationOrchestrator as _CO

                                declared_role = _CO._declared_role(session.actor_role)  # type: ignore[attr-defined]
                            except Exception:
                                declared_role = "PATIENT"
                        if hasattr(orchestrator, "_run_formal_with_timeout"):
                            wf = orchestrator._run_formal_with_timeout(  # type: ignore[attr-defined]
                                text,
                                session if session is not None else orchestrator._load_or_create(line_user_id),
                                remaining,
                            )
                        else:
                            def _call() -> Any:
                                return orchestrator._call_workflow(
                                    {"request_id": f"line-push-{event_id[:8]}", "schema_version": "a.v0.1", "user_raw_input": text, "declared_role": declared_role, "language": "zh-TW"},
                                    use_formal=True,
                                )

                            wf, timed_out, child_guard = run_with_deadline(_call, timeout_s=remaining)
                            if timed_out or wf is None or child_guard.should_abort():
                                job_guard.mark_abandoned()
                                return
                    else:
                        def _compat_call() -> Any:
                            return handle_text_message(text, request_id=f"compat-{event_id[:8]}", use_formal=True)

                        wf, timed_out, child_guard = run_with_deadline(_compat_call, timeout_s=remaining)
                        if timed_out or wf is None or child_guard.should_abort():
                            job_guard.mark_abandoned()
                            return
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    is_timeout = "TimeoutError" in type(exc).__name__ or "FuturesTimeoutError" in type(exc).__name__
                    logger.warning("formal workflow %s attempt %s for %s: %s", "timeout" if is_timeout else "error", attempt + 1, event_id[:8], exc)
                    if is_timeout:
                        # A timed-out workflow may still be unwinding in its
                        # bounded worker.  Abandon its result so that the
                        # eventual answer cannot trigger a push or write.
                        job_guard.mark_abandoned()
                        from tfda_context_gate.workflow.schemas import WorkflowResult as _WR

                        safe_fallback = _WR(
                            request_id=event_id,
                            status="FALLBACK",
                            final_response=HONEST_FALLBACK_PUSH_TEXT,
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
                            intake_snapshot=None,
                            intake_stage=None,
                            previsit_summary=None,
                            system_risk_classification=None,
                            trace={"events": [], "evaluations": []},
                        )
                        # The honest timeout notice is a new safe outcome, not
                        # the abandoned workflow result; it is never persisted
                        # into ProductSession.
                        timeout_text = _format_formal_push_text(safe_fallback, text)
                        if _push_text(
                            line_user_id,
                            timeout_text,
                            event_id=event_id,
                            # The abandoned job guard must not block the
                            # deterministic timeout notice itself.
                            deadline_guard=DeadlineGuard(5.0),
                        ):
                            _mark_event_pushed(orchestrator, event_id)
                        return
                    if attempt == 0 and not job_guard.should_abort():
                        continue
                    from tfda_context_gate.workflow.schemas import WorkflowResult as _WR

                    reason = "FORMAL_TIMEOUT" if is_timeout else DEPENDENCY_OR_TIMEOUT_REASON
                    wf = _WR(request_id=event_id, status="FALLBACK", final_response=HONEST_FALLBACK_PUSH_TEXT, fallback_reason=reason, a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query=text, execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason=reason, intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
            if wf is None:
                return
            if job_guard.should_abort():
                return
            push_text = _format_formal_push_text(wf, text)
            ok = _push_text(line_user_id, push_text, event_id=event_id, deadline_guard=job_guard)
            if ok:
                # Mark durable idempotency only after LINE acknowledged the
                # push.  ProductSession is likewise updated only afterwards.
                _mark_event_pushed(orchestrator, event_id)
                if job_guard.should_abort():
                    return
                if orchestrator is not None:
                    try:
                        sess_push = orchestrator.session_for_user(line_user_id)
                        if sess_push is not None and not job_guard.should_abort():
                            ctx_push = orchestrator.context_manager.append_turn(sess_push.conversation_context, role="assistant", content=push_text)
                            ctx_push, _ = orchestrator.context_manager.compact(ctx_push, stage_completed=False)
                            sess_push = sess_push.model_copy(update={"conversation_context": ctx_push}, deep=True)
                            sess_push = orchestrator._sync_clinical_context(sess_push)
                            if not job_guard.should_abort():
                                orchestrator.repository.save(sess_push, expected_version=sess_push.version)
                    except Exception:
                        pass
            if ok and push_text == HONEST_FALLBACK_PUSH_TEXT and orchestrator is not None:
                _maybe_record_question_for_doctor(orchestrator, line_user_id, text, wf, deadline_guard=job_guard)
            if not ok and not job_guard.should_abort() and not _is_duplicate_push(event_id, getattr(orchestrator, "repository", None)):
                try:
                    fallback_ok = _push_text(line_user_id, HONEST_FALLBACK_PUSH_TEXT, event_id=event_id, deadline_guard=job_guard)
                    if fallback_ok:
                        _mark_event_pushed(orchestrator, event_id)
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("schedule formal push crashed %s: %s", event_id, exc)

    # Admission happens synchronously before creating a thread.  Saturated
    # requests fail closed with an event-owned safe notice; they never create
    # one delayed daemon thread each.
    if not _FORMAL_SEMAPHORE.acquire(blocking=False):
        with _async_jobs_lock:
            _async_jobs.discard(event_id)
        # Fail closed at admission.  Never synchronously call LINE from the
        # webhook caller when the async workers are saturated; the pending
        # event remains replayable and a later webhook can retry admission.
        logger.warning("async formal admission rejected for %s", event_id[:8])
        return

    def _bg() -> None:
        with deadline_scope(job_guard):
            try:
                if _is_duplicate_push(event_id, getattr(orchestrator, "repository", None)):
                    return
                _execute_formal_and_push()
            finally:
                try:
                    _FORMAL_SEMAPHORE.release()
                except Exception:
                    pass
                with _async_jobs_lock:
                    _async_jobs.discard(event_id)

    try:
        threading.Thread(target=_bg, daemon=True).start()
    except Exception:
        _FORMAL_SEMAPHORE.release()
        with _async_jobs_lock:
            _async_jobs.discard(event_id)
        raise


# ── LINE Messaging API helpers ──────────────────────────────────────────────
def _get_messaging_api() -> Any | None:
    token = _get_access_token()
    if not token:
        return None
    try:
        from linebot.v3.messaging import Configuration, MessagingApi, ApiClient

        config = Configuration(access_token=token)
        client = ApiClient(configuration=config)
        return MessagingApi(api_client=client)
    except Exception:
        return None


def _get_blob_api() -> Any | None:
    token = _get_access_token()
    if not token:
        return None
    try:
        from linebot.v3.messaging import Configuration, ApiClient
        from linebot.v3.messaging import MessagingApiBlob

        config = Configuration(access_token=token)
        client = ApiClient(configuration=config)
        return MessagingApiBlob(api_client=client)
    except Exception:
        return None


def _reply_text(reply_token: str, text: str, *, quick_actions: list[dict[str, str]] | None = None) -> bool:
    """Reply via LINE Messaging API; no-op if token missing or API unavailable."""
    if not reply_token or not text:
        return False
    api = _get_messaging_api()
    if api is None:
        return False
    try:
        from linebot.v3.messaging import (
            MessageAction,
            QuickReply,
            QuickReplyItem,
            ReplyMessageRequest,
            TextMessage,
        )

        # LINE reply limit 5000 chars; truncate if needed
        if len(text) > 4900:
            text = text[:4900] + "…"
        quick_reply = None
        if quick_actions:
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label=item["label"], text=item["text"]))
                for item in quick_actions
            ])
        api.reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text, quick_reply=quick_reply)],
        ))
        return True
    except Exception as exc:
        # Do not expose tokens or payloads, but make real LINE delivery
        # failures diagnosable instead of silently returning a webhook 503.
        logger.warning("LINE reply_message failed: %s", exc)
        return False


def _enrich_reply_with_stage_progress(reply: str, status: str, intake: Any | None = None) -> str:
    if status != "NEEDS_CLARIFICATION":
        return reply
    if "已完成" in reply or ("還差" in reply and "✅" in reply):
        return reply
    if intake is None:
        return reply
    try:
        from tfda_context_gate.intake.tool import format_stage_progress

        progress = format_stage_progress(intake)
        if not progress or "第" in progress:
            return reply
        if "皆已完成" in progress:
            return f"{reply}\n\n{progress}"
        if "已完成" in progress or "還差" in progress:
            return f"{reply}\n\n{progress}"
    except Exception:
        return reply
    return reply


def _resolve_resume_quick_actions(product_result: Any) -> list[dict[str, str]] | None:
    try:
        from line_bot.intake_entry import get_resume_actions_for_result

        return get_resume_actions_for_result(product_result)
    except Exception:
        return None


def _maybe_enrich_entry_reply(reply: str, original_text: str) -> str:
    try:
        from line_bot.intake_entry import build_entry_enriched_reply, is_entry_trigger

        if is_entry_trigger(original_text):
            return build_entry_enriched_reply(reply, is_entry=True)
    except Exception:
        pass
    return reply


def _quick_actions_for_status(status: str, reply: str = "") -> list[dict[str, str]] | None:
    from line_bot.ui import PROXY_SOURCE_ACTIONS, REVIEW_ACTIONS, SUBJECT_SELECTION_ACTIONS
    if status == "NEEDS_ROLE_SELECTION":
        return SUBJECT_SELECTION_ACTIONS
    if status == "NEEDS_AUTHORIZATION":
        if "為自己整理" in reply:
            return SUBJECT_SELECTION_ACTIONS
        return [{"label": "已取得同意", "text": "已取得同意"}]
    if status == "NEEDS_MODIFICATION_SELECTION":
        return [
            {"label": "用藥與病史", "text": "修改用藥與病史"},
            {"label": "症狀", "text": "修改症狀"},
            {"label": "想問醫師", "text": "修改想問醫師的問題"},
        ]
    if "對嗎？" in reply:
        base: list[dict[str, str]] | None = None
        if status == "NEEDS_CLARIFICATION":
            if "家人本人描述" in reply:
                base = PROXY_SOURCE_ACTIONS
            elif "固定吃藥" in reply or "胰島素" in reply:
                base = [
                    {"label": "目前沒有用藥", "text": "目前沒有用藥"},
                    {"label": "不清楚", "text": "不清楚"},
                    {"label": "暫停整理", "text": "暫停整理"},
                ]
            elif "過敏" in reply:
                base = [
                    {"label": "沒有過敏", "text": "沒有過敏"},
                    {"label": "不清楚", "text": "不清楚"},
                    {"label": "暫停整理", "text": "暫停整理"},
                ]
            elif "慢性病" in reply or "高血壓" in reply:
                base = [
                    {"label": "沒有其他慢性病", "text": "沒有其他慢性病"},
                    {"label": "不清楚", "text": "不清楚"},
                    {"label": "暫停整理", "text": "暫停整理"},
                ]
            elif "家人" in reply and "糖尿病" in reply:
                base = [
                    {"label": "沒有家族史", "text": "沒有家族史"},
                    {"label": "不清楚", "text": "不清楚"},
                    {"label": "暫停整理", "text": "暫停整理"},
                ]
            elif "想問醫師" in reply:
                base = [
                    {"label": "還沒想到", "text": "還沒想到"},
                    {"label": "跳過", "text": "跳過"},
                    {"label": "暫停整理", "text": "暫停整理"},
                ]
            else:
                base = [
                    {"label": "不清楚", "text": "不清楚"},
                    {"label": "跳過", "text": "跳過"},
                    {"label": "暫停整理", "text": "暫停整理"},
                ]
        if base is not None:
            return [{"label": "正確", "text": "正確"}, {"label": "更正", "text": "更正"}] + base[:2]
        return [{"label": "正確", "text": "正確"}, {"label": "更正", "text": "更正"}]
    if status in ("NEEDS_CONFIRMATION", "AWAITING_CONFIRMATION") or "請確認是否完成" in reply or "確認完成" in reply or "看診前資料已整理完成" in reply or "修改看診資料" in reply:
        return REVIEW_ACTIONS
    if status == "NEEDS_CLARIFICATION" and "家人本人描述" in reply:
        return PROXY_SOURCE_ACTIONS
    if status in {"PAUSED", "SIDE_ANSWER"}:
        return [{"label": "繼續整理", "text": "繼續整理"}]
    if status == "NEEDS_CLARIFICATION":
        if "固定吃藥" in reply or "胰島素" in reply:
            return [
                {"label": "目前沒有用藥", "text": "目前沒有用藥"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "過敏" in reply:
            return [
                {"label": "沒有過敏", "text": "沒有過敏"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "慢性病" in reply or "高血壓" in reply:
            return [
                {"label": "沒有其他慢性病", "text": "沒有其他慢性病"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "家人" in reply and "糖尿病" in reply:
            return [
                {"label": "沒有家族史", "text": "沒有家族史"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "想問醫師" in reply:
            return [
                {"label": "還沒想到", "text": "還沒想到"},
                {"label": "跳過", "text": "跳過"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        return [
            {"label": "不清楚", "text": "不清楚"},
            {"label": "跳過", "text": "跳過"},
            {"label": "暫停整理", "text": "暫停整理"},
        ]
    return None


def _download_image_content(message_id: str) -> bytes | None:
    """Download image via httpx direct endpoint with MessagingApiBlob fallback."""
    if not message_id:
        return None
    token = _get_access_token()
    if not token:
        logger.warning("No LINE access token available to download image")
        return None

    # Method 1: Direct HTTPX download from LINE Data API (fastest, immune to SDK wrapper variations)
    try:
        import httpx

        url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
        headers = {"Authorization": f"Bearer {token}"}
        resp = httpx.get(url, headers=headers, timeout=20.0)
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning("HTTPX image download returned status %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("HTTPX image download exception for message %s: %s", message_id, exc)

    # Method 2: SDK MessagingApiBlob fallback
    blob_api = _get_blob_api()
    if blob_api is not None:
        try:
            content = blob_api.get_message_content(message_id=message_id)
            if isinstance(content, (bytes, bytearray)):
                return bytes(content)
            if hasattr(content, "read"):
                return content.read()
            if hasattr(content, "data"):
                return bytes(getattr(content, "data"))
            if hasattr(content, "raw_data"):
                return bytes(getattr(content, "raw_data"))
            return bytes(content)
        except Exception as exc:
            logger.warning("Blob API download error for message %s: %s", message_id, exc)

    return None


# ── FastAPI routes ──────────────────────────────────────────────────────────
@app.get("/health")
def health() -> JSONResponse:
    checks = {
        "webhook_signature": bool(_get_secret()),
        "messaging_api": bool(_get_access_token()),
        "product_session": _get_conversation_orchestrator() is not None,
        "patient_liff": bool(os.getenv("LINE_LOGIN_CHANNEL_ID") and os.getenv("LINE_LIFF_ID")),
        "demo_clinician": bool(
            os.getenv("LINE_DEMO_MODE", "false").lower() == "true"
            and os.getenv("DEMO_CLINICIAN_IDS", "").strip()
        ),
    }
    required = checks["webhook_signature"] and checks["messaging_api"] and checks["product_session"]
    return JSONResponse(
        status_code=200 if required else 503,
        content={"status": "ok" if required else "degraded", "service": "line_bot", "checks": checks},
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok", "message": "TFDA LINE Bot webhook at POST /callback"}


@app.get("/patient", response_class=FileResponse)
def patient_portal() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "patient.html")


@app.get("/clinician", response_class=FileResponse)
def clinician_portal() -> FileResponse:
    # Demo pages evolve frequently; an old cached clinician page can hide a
    # newly deployed safety/scan control while the API is already current.
    return FileResponse(
        Path(__file__).parent / "static" / "clinician.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/static/{file_name}", response_class=FileResponse)
def get_static_asset(file_name: str) -> FileResponse:
    # Sanitize to base filename only to prevent path traversal
    safe_name = Path(file_name).name
    p = Path(__file__).parent / "static" / safe_name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Static asset not found")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/line/rich-menu")
def rich_menu_definition(patient_portal_url: str) -> JSONResponse:
    """回傳部署用定義，不主動呼叫 LINE API 或覆寫既有 Rich Menu。"""
    from line_bot.ui import build_rich_menu_payload, is_valid_rich_menu_url
    if not is_valid_rich_menu_url(patient_portal_url):
        raise HTTPException(status_code=422, detail="patient_portal_url must be a tokenless HTTPS URL")
    return JSONResponse(content=build_rich_menu_payload(patient_portal_url=patient_portal_url))


@app.get("/api/line/client-config")
def line_client_config() -> JSONResponse:
    return JSONResponse(
        content={
            "liff_id": os.getenv("LINE_LIFF_ID", ""),
            "demo_identity_headers": _demo_identity_headers_enabled(),
            # This is intentionally a boolean.  The configured clinician IDs are
            # an authorization boundary and must not be enumerated to browsers.
            "demo_clinician_enabled": _demo_clinician_enabled(),
            "demo_intake_token_enabled": _is_demo_intake_token_enabled(),
            "previsit_room_url": "/patient/previsit-room",
        },
        headers={"Cache-Control": "no-store"},
    )


# ── Previsit Room (dedicated) ─────────────────────────────────────────────
# Only line_bot/intake_token.py and this section may handle demo tokens.
# LIFF Bearer (verified via api.line.me) takes precedence, fail-closed.

def _is_demo_intake_token_enabled() -> bool:
    return os.getenv("DEMO_INTAKE_TOKEN_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def _is_public_demo_web_enabled() -> bool:
    """A deliberately opt-in, isolated no-login route for presentations."""
    return (
        os.getenv("LINE_DEMO_MODE", "false").strip().lower() in {"1", "true", "yes"}
        and os.getenv("DEMO_WEB_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    )


def _hash_intake_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_previsit_base_url(request: Any | None = None) -> str:
    env_url = os.getenv("PATIENT_INTAKE_BASE_URL", "").strip() or os.getenv("LINE_PATIENT_PORTAL_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    cb_url = os.getenv("LINE_CALLBACK_URL", "").strip()
    if cb_url:
        from urllib.parse import urlparse
        parsed = urlparse(cb_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    if request is not None:
        try:
            base = str(request.base_url).rstrip("/")
            return base
        except Exception:
            pass
    return "https://example.com"


def _previsit_launch_url(request: Any | None = None, line_user_id: str | None = None) -> tuple[str, str | None]:
    """Return the pre-visit entry URL for this session/user."""
    base = _get_previsit_base_url(request)
    if line_user_id and line_user_id != "unknown":
        try:
            raw_token, sess_id = _create_previsit_token_for_user(line_user_id)
            return f"{base}/patient/previsit-room?token={raw_token}", sess_id
        except Exception as exc:
            logger.warning("Failed to create previsit token for user %s: %s", line_user_id, exc)
    if _is_public_demo_web_enabled():
        return f"{base}/demo/previsit", None
    # LIFF mode: no token in URL; the browser verifies the LIFF ID token.
    return f"{base}/patient/previsit-room", None


def _create_previsit_token_for_user(line_user_id: str) -> tuple[str, str]:
    import secrets as _sec
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    orch = _get_conversation_orchestrator()
    if orch is None:
        raise RuntimeError("ProductSession not configured")
    sess = orch._load_or_create(line_user_id)
    raw = _sec.token_urlsafe(16)
    h = _hash_intake_token(raw)
    exp = _dt.now(_tz.utc) + _td(minutes=30)
    orch.repository.create_intake_token(h, sess.session_id, exp)
    return raw, sess.session_id


def _record_previsit_room_entry(orchestrator: Any, line_user_id: str, text: str) -> None:
    """Audit a delivered room card without starting the old LINE intake flow.

    The dedicated room owns the clinical interview.  LINE still needs the
    minimal user/assistant pair so retry handling and subsequent conversation
    context remain coherent.  This is deliberately called only after LINE has
    accepted the card, so a failed one-time reply can be retried without
    duplicate history.
    """
    session = orchestrator.session_for_user(line_user_id)
    if session is None:
        session = orchestrator._load_or_create(line_user_id)
    context = orchestrator.context_manager.append_turn(
        session.conversation_context, role="user", content=text,
    )
    context = orchestrator.context_manager.append_turn(
        context, role="assistant", content="已開啟看診前對談室，請點卡片開始整理。",
    )
    updated = session.model_copy(update={"conversation_context": context}, deep=True)
    orchestrator.repository.save(updated, expected_version=session.version)


PREVISIT_TRIGGER_TEXTS = {
    "我要準備看診",
    "開啟看診前對談室",
    "整理看診資料",
    "準備看診",
    # Existing production Rich Menu sends this exact message action.  Keep it
    # as an explicit product entry point so tapping the old menu cannot fall
    # into the retired LINE intake state machine.
    "開始看診前整理",
    "開始看診整理",
    "看診前整理",
    "看診整理",
    "我要整理看診資料",
    "我要看診",
    "我要看醫生",
    "我要回診",
    "回診",
}
PREVISIT_KEYWORDS = [
    "我要準備看診",
    "開啟看診前對談室",
    "整理看診資料",
    "準備看診",
    "開始看診前整理",
    "看診前整理",
    "看診整理",
    "我要整理看診資料",
]
PREVISIT_NATURAL_INTENT_RE = re.compile(
    r"(?:我要|我想(?:要)?|想看|想要看|(?:下週|最近|近期)要|準備|開始|需要).{0,8}(?:看診|看醫生|回診)"
)


def _is_previsit_trigger_text(text: str) -> bool:
    norm = unicodedata.normalize("NFKC", text).strip()
    if norm in PREVISIT_TRIGGER_TEXTS:
        return True
    if any(kw in norm for kw in PREVISIT_KEYWORDS):
        return True
    return bool(PREVISIT_NATURAL_INTENT_RE.search(norm))


def _is_expired_line_reply_event(event: dict[str, Any], *, now_ms: int | None = None) -> bool:
    """Ignore webhook retries whose reply token can no longer be valid.

    LINE retries a non-2xx callback with the *same* one-time reply token.
    Recreating a pre-visit launch token for each retry invalidates the link
    that was already delivered.  A callback older than one minute cannot
    safely produce a reply, so it must be acknowledged without side effects.
    """
    try:
        event_ms = int(event.get("timestamp"))
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return event_ms > 0 and current_ms - event_ms > 60_000
    except (TypeError, ValueError, OverflowError):
        return False


def _portal_available() -> bool:
    has_liff = bool(os.getenv("LINE_LIFF_ID", "").strip() and os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip())
    return has_liff or _is_demo_intake_token_enabled()


def _build_previsit_flex_message(previsit_url: str) -> dict:
    # UI payload has one source of truth; this transport layer only supplies the
    # already-authorized short-lived URL.
    from line_bot.ui import build_previsit_room_flex_message

    return build_previsit_room_flex_message(room_url=previsit_url)


# Web chat idempotency (dedup by client_message_id, in-memory per process)
_web_chat_dedup: dict[tuple[str, str], dict[str, Any]] = {}
_web_chat_lock = threading.Lock()


def _get_previsit_session(authorization: str, demo_user_id: str, intake_token: str):
    orch = _get_conversation_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")

    bearer_token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    candidate_token = intake_token.strip() if intake_token else ""
    # 若前端以 Bearer 傳送 demo intake token（非 JWT），優先作為 intake token 解析
    if not candidate_token and bearer_token and "." not in bearer_token:
        candidate_token = bearer_token

    if candidate_token and _is_demo_intake_token_enabled():
        import re as _re
        if not _re.match(r"^[A-Za-z0-9_-]{16,64}$", candidate_token):
            raise HTTPException(status_code=401, detail="Invalid intake token")
        h = _hash_intake_token(candidate_token)
        rec = orch.repository.get_intake_token(h)
        if rec is None:
            raise HTTPException(status_code=403, detail="Invalid intake token")
        from datetime import datetime as _dt, timezone as _tz
        try:
            exp_raw = rec.get("expires_at") or rec.get("expiresAt") or ""
            exp_dt = _dt.fromisoformat(str(exp_raw))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=_tz.utc)
            if _dt.now(_tz.utc) >= exp_dt:
                raise HTTPException(status_code=401, detail="Intake token expired")
        except HTTPException:
            raise
        except Exception:
            pass
        if rec.get("consumed_at"):
            pass
        sess_id = str(rec.get("product_session_id") or "")
        sess = orch.repository.get(sess_id)
        if sess is None:
            raise HTTPException(status_code=403, detail="Token session not found")
        return sess

    if bearer_token:
        channel_id = os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip()
        if channel_id:
            uid = _verify_line_id_token(bearer_token)
            sess = orch.session_for_user(uid)
            if sess is None:
                sess = orch._load_or_create(uid)
            return sess

    if _demo_identity_headers_enabled() and demo_user_id:
        sess = orch.session_for_user(demo_user_id)
        if sess is None:
            sess = orch._load_or_create(demo_user_id)
        return sess
    raise HTTPException(status_code=401, detail="Verified LINE LIFF identity is required")


def _previsit_room_token(
    request: Request,
    x_intake_token: str,
    intake_token: str,
) -> str:
    """Resolve the opaque room token exactly as the room read/chat routes do."""
    token_q = ""
    try:
        token_q = request.query_params.get("token") or request.query_params.get("intake_token") or ""
    except Exception:
        token_q = ""
    return x_intake_token or intake_token or token_q


def _is_general_education_in_previsit(text: str) -> bool:
    """Detect general education question to avoid polluting intake stage."""
    low = text.strip().lower()
    edu_keywords = ["糖尿病的一般飲食", "飲食原則", "衛教", "一般飲食", "血糖正常值", "胰島素是什麼"]
    if any(k in text for k in edu_keywords):
        return True
    # Fallback heuristic: if text looks like general knowledge without personal context
    if len(text) < 60 and any(k in low for k in ["什麼是", "是什麼", "如何", "怎麼"]):
        # limit to education-like
        if "看診" not in text and "症狀" not in text and "藥" not in text:
            return True
    return False


_PREVISIT_ROOM_META_TEXTS = {
    "你好", "您好", "嗨", "哈囉", "hello", "hi",
    "我要看診", "我要看診啊", "我要準備看診", "整理看診資料",
    "有病啊", "我有病啊",
}


def _is_previsit_room_meta_text(text: str) -> bool:
    """A room-opening/greeting utterance is not a clinical field answer."""
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    return normalized in _PREVISIT_ROOM_META_TEXTS


def _previsit_room_current_question(session: Any) -> str:
    if hasattr(session, "pending_question") and session.pending_question:
        return str(session.pending_question)
    if hasattr(session, "intake_snapshot") and session.intake_snapshot:
        try:
            from tfda_context_gate.intake.lean_agent import LeanIntakeAgent

            agent = LeanIntakeAgent.from_env()
            q, _ = agent._generate_next_question(
                getattr(session, "intake_stage", "stage1") or "stage1", session.intake_snapshot
            )
            if q:
                return q
        except Exception:
            pass
    return "目前有固定吃藥或打胰島素嗎？知道藥名就直接說；不確定也沒關係。"


_PREVISIT_CONFIRMATION_RE = re.compile(
    r"你提到[「\"](?P<raw>.*?)[」\"]\s*[，,]?\s*"
    r"我記為[「\"](?P<normalized>.*?)[」\"]\s*[，,]?\s*對嗎？"
    r"(?:\s*如果不對，直接告訴我就好。)?",
    re.DOTALL,
)
_PREVISIT_LEGACY_CONFIRMATION_RE = re.compile(
    r"我先把[「\"](?P<raw>.*?)[」\"]記為[「\"](?P<normalized>.*?)[」\"]"
    r"[；;]\s*如果哪裡不對，直接說要改哪一項就好。?",
    re.DOTALL,
)
_PREVISIT_NONE_ACK_RE = re.compile(
    r"(?:好|好的)，我先記成目前沒有[；;，,][^\n。]*。?",
    re.DOTALL,
)
_PREVISIT_UNCERTAIN_RE = re.compile(r"不確定|不清楚|不知道|沒概念|不曉得")
_PREVISIT_CORRECTION_RE = re.compile(r"剛才|剛剛|前面|其實|不是|更正|改成|改為|修正|錯了")
_PREVISIT_DOCTOR_QUESTION_PREFIX_RE = re.compile(
    r"^(?:我\s*)?(?:(?:想|要)\s*)?(?:請\s*)?問(?:一下)?(?:醫師|醫生)(?:的問題)?[：:\s，,]*"
)


def _clean_previsit_doctor_question(raw: str) -> str:
    """Store the question, not its conversational lead-in, in a room summary."""
    cleaned = _PREVISIT_DOCTOR_QUESTION_PREFIX_RE.sub("", raw.strip())
    return cleaned.strip()[:200]


def _previsit_natural_ack(field: str, text: str, snapshot: Any) -> str:
    """Return a short patient-facing acknowledgement for a completed field.

    This is deliberately a presentation projection.  The intake normalizer may
    use sentinel values such as ``none`` internally; those values must never be
    exposed in a patient-facing reply, and this helper never writes anything.
    """
    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    negative = bool(re.search(r"(?:^|\s)(?:沒有|無|未有|没|否)(?:\s|$)", normalized))
    if field == "known_medications" and ("沒有吃藥" in normalized or "沒有固定吃藥" in normalized):
        return "了解，我先記為目前沒有固定用藥。"
    if field == "allergies" and negative:
        return "了解，我先記為目前沒有已知的藥物或食物過敏。"

    labels = {
        "known_medications": "目前用藥",
        "allergies": "過敏資訊",
        "chronic_conditions": "慢性病史",
        "family_history": "家族病史",
        "symptom_onset": "症狀開始時間",
        "symptom_description": "主要症狀",
        "symptom_severity": "症狀程度",
        "questions_for_doctor": "想問醫師的問題",
    }
    label = labels.get(field, "這項資料")
    if negative:
        return f"了解，我先記為目前沒有{label}。"

    # Do not derive a patient-facing value from a sentinel or an absent field.
    # The original utterance is safe to acknowledge when no normalized value is
    # available, but avoid repeating a long/raw sentence as a pseudo-summary.
    value: Any = None
    try:
        value = getattr(snapshot, field, None)
    except Exception:
        value = None
    values = value if isinstance(value, list) else [value]
    display_values = [
        str(item).strip()
        for item in values
        if item is not None and str(item).strip().lower() not in {"none", "null", "unknown"}
    ]
    if display_values:
        shown = "、".join(display_values)[:120]
        return f"了解，我先記下{label}：{shown}。"
    if field == "questions_for_doctor":
        return "了解，我先幫你記下這個想問醫師的問題。"
    return f"了解，我先記下你的{label}。"


def _naturalize_previsit_reply(reply: str, user_text: str, before: Any, after: Any, status: str) -> str:
    """Naturalize only a dedicated web intake transition response.

    The web room calls the existing orchestrator, so this function is kept at
    the response boundary.  It is intentionally conservative: red flags,
    uncertainty, corrections, errors, submitted summaries, and responses that
    did not advance to another pending field are returned unchanged.
    """
    original = str(reply or "")
    if not original or status in {"FALLBACK", "SUBMITTED", "CANCELLED", "ERROR"}:
        return original
    # In the dedicated room, a question for the clinician is a note to save,
    # not a second request to answer through the education/RAG path.  The core
    # interpreter can classify wording such as 「可以吃炸雞嗎」 as both; once it
    # landed safely, keep the patient on the review path.
    if getattr(before, "pending_field", None) == "questions_for_doctor":
        before_questions = list(getattr(getattr(before, "intake_snapshot", None), "questions_for_doctor", []) or [])
        after_questions = list(getattr(getattr(after, "intake_snapshot", None), "questions_for_doctor", []) or [])
        added_questions = [question for question in after_questions if question not in before_questions]
        if added_questions:
            added = "、".join(str(question) for question in added_questions[:2])
            if getattr(after, "intake_stage", None) == "review":
                return (
                    f"了解，我先幫你記下想問醫師的問題：{added}。\n\n"
                    "資料已整理好，請查看摘要；內容正確請按「確認完成」，需要調整可按「修改資料」。"
                )
            return f"了解，我先幫你記下想問醫師的問題：{added}。"
    if getattr(after, "status", None) in {"SUBMITTED", "CLOSED"} or getattr(after, "intake_stage", None) in {"submitted", "review"}:
        return original
    if "119" in original or "急診" in original or "目前無法驗證" in original:
        return original
    text = str(user_text or "").strip()
    # Check the user's turn only.  The next-question prompt itself normally
    # contains "不確定也可以說", which must not make every successful answer
    # look like an uncertainty response.
    if _PREVISIT_UNCERTAIN_RE.search(text):
        return original
    if _PREVISIT_CORRECTION_RE.search(text) or "已更新成" in original:
        return original

    before_field = getattr(before, "pending_field", None)
    after_field = getattr(after, "pending_field", None)
    next_question = str(getattr(after, "pending_question", "") or "").strip()
    if not before_field or not after_field or before_field == after_field or not next_question:
        return original

    match = _PREVISIT_CONFIRMATION_RE.search(original) or _PREVISIT_LEGACY_CONFIRMATION_RE.search(original)
    if match is None and str(before_field) == "known_medications":
        # The deterministic fast path has a shorter variant for an explicit
        # negative answer ("好，我先記成目前沒有；...").  Treat it as the
        # same presentation-only confirmation, without touching its write.
        match = _PREVISIT_NONE_ACK_RE.match(original.lstrip())
    # Only replace known confirmation templates.  This avoids changing an AI
    # education answer or an honest fallback that happens to contain a question.
    if match is None:
        return original

    ack = _previsit_natural_ack(str(before_field), text, getattr(after, "intake_snapshot", None))
    remainder = original[match.end():].strip()
    question_pos = remainder.find(next_question)
    if question_pos >= 0:
        leading = remainder[:question_pos].strip()
        question_and_tail = remainder[question_pos:]
        if leading:
            follow_up = f"{leading}\n\n接下來想確認：{question_and_tail}"
        else:
            follow_up = f"接下來想確認：{question_and_tail}"
    else:
        follow_up = f"接下來想確認：{next_question}"
        if remainder:
            follow_up = f"{follow_up}\n\n{remainder}"
    # A final guard prevents a sentinel from leaking if an upstream template
    # changes but still happens to match this boundary adapter.
    follow_up = re.sub(r"(?i)(?<![A-Za-z])(?:none|null|unknown)(?![A-Za-z])", "目前沒有", follow_up)
    return f"{ack}\n\n{follow_up}"


@app.get("/patient/previsit-room", response_class=FileResponse)
def previsit_room_portal() -> FileResponse:
    p = Path(__file__).parent / "static" / "previsit-room.html"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Previsit room portal not found")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/demo/previsit")
def start_public_demo_previsit() -> RedirectResponse:
    """Start an isolated browser-only demo without LINE/LIFF login.

    This route intentionally creates a fresh anonymous ProductSession every
    time.  It is unavailable unless both demo flags are explicitly enabled,
    so it cannot become a production authentication bypass.
    """
    if not _is_public_demo_web_enabled():
        raise HTTPException(status_code=404, detail="Demo entry is not enabled")
    raw_token, _ = _create_previsit_token_for_user(f"web-demo-{uuid.uuid4().hex}")
    return RedirectResponse(
        url=f"/patient/previsit-room?token={raw_token}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/patient/previsit-room")
def get_previsit_room(
    request: Request,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
    x_intake_token: str = Header(default="", alias="X-Intake-Token"),
    intake_token: str = "",
) -> JSONResponse:
    token = _previsit_room_token(request, x_intake_token, intake_token)
    sess = _get_previsit_session(authorization, x_line_user_id, token)

    pending_question = sess.pending_question
    pending_field = sess.pending_field
    quick_replies: list[dict[str, str]] = []
    try:
        from tfda_context_gate.intake.lean_agent import LeanIntakeAgent

        agent = LeanIntakeAgent.from_env()
        stage = sess.intake_stage or "stage1"
        dyn_q, dyn_f = agent._generate_next_question(stage, sess.intake_snapshot)
        if not pending_question:
            pending_question = dyn_q
        if not pending_field:
            pending_field = dyn_f
        quick_replies = agent._generate_quick_replies(stage, sess.intake_snapshot)
    except Exception:
        pass

    return JSONResponse(
        content={
            "session_id": sess.session_id,
            "version": sess.version,
            "status": sess.status,
            "intake_stage": sess.intake_stage or "stage1",
            "intake_snapshot": sess.intake_snapshot.model_dump(mode="json"),
            "pending_question": pending_question,
            "pending_field": pending_field,
            "quick_replies": quick_replies,
            "system_risk_classification": sess.system_risk_classification,
        },
        headers={"Cache-Control": "no-store"},
    )


class PrevisitChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    version: int = Field(ge=0)
    client_message_id: str | None = Field(default=None, max_length=256)


def _execute_previsit_chat_core(sess: Any, body: PrevisitChatRequest, orch: Any) -> dict[str, Any]:
    """Shared core for chat and chat/stream: all safety checks, no HTTP layer.

    Reuses exact validation order of the original post_previsit_chat:
    idempotency(replay) → 409 SUBMITTED → 409 version → 422 empty → RED_FLAG → education → normal intake.
    Caller is responsible for auth/session retrieval and 503 orch check.
    """
    # idempotency before version check — cached reply is replay verbatim
    if body.client_message_id:
        key = (sess.session_id, body.client_message_id)
        with _web_chat_lock:
            cached = _web_chat_dedup.get(key)
            if cached is not None:
                return cached
    if sess.status == "SUBMITTED":
        raise HTTPException(status_code=409, detail="Cannot modify SUBMITTED session")
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="message is required")
    # "開始新的整理" is the idempotent reset / bootstrap command: allow it even if initial version is 0
    if text not in ("開始新的整理", "開始整理"):
        if body.version != sess.version:
            raise HTTPException(status_code=409, detail="Version conflict")
    # red-flag pre-check before AI, does not pollute intake
    try:
        from tfda_context_gate.clinical_safety import RiskSignalPolicy
        risk = RiskSignalPolicy().classify(text)
        if risk.level == "RED_FLAG":
            red_payload = {"level": risk.level, "signals": risk.signals}
            new_sess = sess.model_copy(update={"system_risk_classification": red_payload}, deep=True)
            try:
                saved = orch.repository.save(new_sess, expected_version=sess.version)
            except Exception as exc:
                from tfda_context_gate.product_session import ProductSessionConflict as _PSC
                if isinstance(exc, _PSC) or "conflict" in str(exc).lower():
                    raise HTTPException(status_code=409, detail="Version conflict") from exc
                raise
            resp = {"reply": "偵測到可能的緊急警訊。請立即撥打 119 或前往急診；若身旁有人請請他協助。本系統不做診斷，已為你保留目前進度。", "status": "FALLBACK", "intake_stage": saved.intake_stage, "version": saved.version, "intake_snapshot": saved.intake_snapshot.model_dump(mode="json")}
            if body.client_message_id:
                with _web_chat_lock:
                    _web_chat_dedup[(sess.session_id, body.client_message_id)] = resp
            return resp
    except HTTPException:
        raise
    except Exception:
        pass
    if _is_previsit_room_meta_text(text):
        # The room is a focused intake conversation, not a second copy of the
        # LINE general-chat bot.  Never turn greetings/frustration/opening
        # phrases into a medication, condition, or family-history value.
        return {
            "reply": f"這裡是看診前資料整理室，我們直接從目前這題開始：\n{_previsit_room_current_question(sess)}",
            "status": sess.status,
            "intake_stage": sess.intake_stage,
            "version": sess.version,
            "intake_snapshot": sess.intake_snapshot.model_dump(mode="json"),
        }
    if _is_general_education_in_previsit(text):
        resp = {
            "reply": "這個問題比較偏一般衛教，建議回 LINE 聊天問衛教小幫手；我這裡先幫你保留看診前整理的進度。你可以繼續整理看診資料。",
            "status": sess.status,
            "intake_stage": sess.intake_stage,
            "version": sess.version,
            "intake_snapshot": sess.intake_snapshot.model_dump(mode="json"),
        }
        if body.client_message_id:
            with _web_chat_lock:
                _web_chat_dedup[(sess.session_id, body.client_message_id)] = resp
        return resp
    # Pre-Visit Room 專屬授權與狀態保證
    from tfda_context_gate.product_session.schemas import AuthorizationStatus, PreVisitIntake
    from tfda_context_gate.access_control import InformationSource, PermissionScope
    if sess.authorization_status == AuthorizationStatus.UNVERIFIED or sess.status not in ("ACTIVE", "AWAITING_CONFIRMATION", "PAUSED"):
        sess = sess.model_copy(update={
            "authorization_status": AuthorizationStatus.PATIENT_SELF,
            "subject_id_hash": sess.principal_id_hash,
            "information_source": InformationSource.SELF_REPORTED,
            "permission_scopes": [
                PermissionScope.CREATE_OWN_INTAKE,
                PermissionScope.VIEW_OWN_SUMMARY,
                PermissionScope.SHARE_OWN_SUMMARY,
            ],
            "status": "ACTIVE",
        }, deep=True)
        try:
            sess = orch.repository.save(sess, expected_version=sess.version)
        except Exception:
            pass

    try:
        from tfda_context_gate.intake.lean_agent import LeanIntakeAgent
        agent = LeanIntakeAgent.from_env()
        updated_sess, agent_resp = agent.process_turn(sess, text)
        saved = orch.repository.save(updated_sess, expected_version=sess.version)
        agent_resp["version"] = saved.version
        if body.client_message_id:
            with _web_chat_lock:
                _web_chat_dedup[(sess.session_id, body.client_message_id)] = agent_resp
        return agent_resp

    except HTTPException:
        raise
    except Exception as exc:
        from tfda_context_gate.product_session import ProductSessionConflict as _PSC
        if isinstance(exc, _PSC) or "conflict" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Version conflict") from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/patient/previsit-room/chat")
def post_previsit_chat(
    body: PrevisitChatRequest,
    request: Request,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
    x_intake_token: str = Header(default="", alias="X-Intake-Token"),
    intake_token: str = "",
) -> JSONResponse:
    token = _previsit_room_token(request, x_intake_token, intake_token)
    sess = _get_previsit_session(authorization, x_line_user_id, token)
    orch = _get_conversation_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")
    resp = _execute_previsit_chat_core(sess, body, orch)
    return JSONResponse(content=resp)


@app.post("/api/patient/previsit-room/chat/stream")
def post_previsit_chat_stream(
    body: PrevisitChatRequest,
    request: Request,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
    x_intake_token: str = Header(default="", alias="X-Intake-Token"),
    intake_token: str = "",
) -> StreamingResponse:
    """Minimal SSE variant: reuses all safety/processing of post_previsit_chat, streams final_only events.

    Sequence: phase → delta (single, full reply) → complete.  NOT a token stream: no character slicing,
    no fake verbatim; each event JSON contains stream_mode:"final_only".
    """
    import json as _json

    token = _previsit_room_token(request, x_intake_token, intake_token)
    sess = _get_previsit_session(authorization, x_line_user_id, token)
    orch = _get_conversation_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")
    # All validation + processing reused; on 409/422 etc this raises before streaming (no SSE body).
    resp = _execute_previsit_chat_core(sess, body, orch)

    def _sse_event(payload: dict[str, Any], *, event: str) -> str:
        # Use same SSE wire format as callback/stream; payload already contains stream_mode
        data = _json.dumps(payload, ensure_ascii=False)
        # event + data + blank line
        return f"event: {event}\ndata: {data}\n\n"

    def _gen() -> Iterator[str]:
        # Phase event (processing start)
        yield _sse_event({"type": "phase", "phase": "processing", "stream_mode": "final_only"}, event="phase")
        # Single delta — full reply, not sliced
        yield _sse_event({"type": "delta", "content": resp.get("reply", ""), "stream_mode": "final_only"}, event="delta")
        # Complete — full final result with final_only marker
        complete_payload: dict[str, Any] = {**resp, "type": "complete", "stream_mode": "final_only"}
        yield _sse_event(complete_payload, event="complete")

    return StreamingResponse(_gen(), media_type="text/event-stream")


def _create_previsit_room_share(
    *,
    expected_session_id: str | None,
    body: CreateShareRequest | None,
    request: Request,
    authorization: str,
    x_line_user_id: str,
    x_intake_token: str,
    intake_token: str,
) -> JSONResponse:
    """Issue a short-lived grant using the room's own token-bound session.

    The browser does not need to send a session id.  The optional path variant
    exists only for explicit API callers and is checked against the token-bound
    session, so a token can never be used to share another session.
    """
    token = _previsit_room_token(request, x_intake_token, intake_token)
    session = _get_previsit_session(authorization, x_line_user_id, token)
    if expected_session_id is not None and session.session_id != expected_session_id:
        raise HTTPException(status_code=403, detail="Session does not belong to this intake token")
    orchestrator = _get_conversation_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")

    allowed_hash = None
    clinician_id = body.allowed_clinician_id.strip() if body and body.allowed_clinician_id else ""
    if clinician_id:
        _require_demo_clinician(clinician_id)
        allowed_hash = orchestrator.principal_hash(f"clinician:{clinician_id}")

    from tfda_context_gate.product_session import ShareGrantDenied
    from tfda_context_gate.sharing import ShareGrantService

    try:
        issue = ShareGrantService(orchestrator.repository).create(
            session,
            allowed_practitioner_hash=allowed_hash,
        )
    except ShareGrantDenied as exc:
        # Keep the existing share API's conflict semantics for unconfirmed,
        # pending, red-flag, or otherwise non-shareable sessions.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Build the QR locally from the same short-lived, one-time share code.
    # A third-party QR service would receive the share credential, so never use
    # one here.
    try:
        from io import BytesIO

        import qrcode

        image = qrcode.make(issue.token)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        qr_code_data_uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="QR code generator is unavailable") from exc

    payload = issue.model_dump(mode="json")
    payload["qr_code_data_uri"] = qr_code_data_uri
    return JSONResponse(content=payload)


@app.post("/api/patient/previsit-room/share")
def create_previsit_room_share(
    request: Request,
    body: CreateShareRequest | None = None,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
    x_intake_token: str = Header(default="", alias="X-Intake-Token"),
    intake_token: str = "",
) -> JSONResponse:
    return _create_previsit_room_share(
        expected_session_id=None,
        body=body,
        request=request,
        authorization=authorization,
        x_line_user_id=x_line_user_id,
        x_intake_token=x_intake_token,
        intake_token=intake_token,
    )


@app.post("/api/patient/previsit-room/share/{session_id}")
def create_previsit_room_share_for_session(
    session_id: str,
    request: Request,
    body: CreateShareRequest | None = None,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
    x_intake_token: str = Header(default="", alias="X-Intake-Token"),
    intake_token: str = "",
) -> JSONResponse:
    return _create_previsit_room_share(
        expected_session_id=session_id,
        body=body,
        request=request,
        authorization=authorization,
        x_line_user_id=x_line_user_id,
        x_intake_token=x_intake_token,
        intake_token=intake_token,
    )


_PATIENT_REVIEW_FIELDS: tuple[tuple[str, str], ...] = (
    ("known_medications", "目前用藥"),
    ("allergies", "過敏史"),
    ("chronic_conditions", "慢性病史"),
    ("family_history", "家族史"),
    ("symptom_onset", "症狀開始時間"),
    ("symptom_description", "主要症狀"),
    ("symptom_severity", "症狀程度"),
    ("questions_for_doctor", "想問醫師的問題"),
)


def _patient_review_payload(session: Any) -> dict[str, Any]:
    """Build a display DTO without making the browser infer intake state.

    Empty fields and explicit uncertainty are kept distinct so the portal can
    show "尚未提供" versus "待看診確認".  This is a read-only projection;
    the LINE conversation remains the only path that changes intake data.
    """
    from tfda_context_gate.intake.summary import generate_previsit_summary

    snapshot = session.intake_snapshot.model_dump(mode="json")
    summary = generate_previsit_summary(snapshot, request_id=session.session_id)
    summary_missing = set(summary.missing_fields)
    pending_fields: set[str] = set()
    if session.pending_field:
        pending_fields.add(session.pending_field)

    def _has_pending_marker(value: Any) -> bool:
        values = value if isinstance(value, list) else [value]
        return any(
            isinstance(item, str)
            and ("待確認" in item or "待看診確認" in item)
            for item in values
        )

    fields: list[dict[str, Any]] = []
    for field_name, label in _PATIENT_REVIEW_FIELDS:
        value = snapshot.get(field_name)
        if field_name in pending_fields or _has_pending_marker(value):
            state = "PENDING"
            pending_fields.add(field_name)
        elif field_name in summary_missing:
            state = "MISSING"
        else:
            state = "PROVIDED"
        fields.append({"name": field_name, "label": label, "value": value, "state": state})

    status = str(session.status)
    intake_stage = str(session.intake_stage)
    if status == "SUBMITTED" and intake_stage == "submitted":
        confirmation_status = "CONFIRMED"
    elif status == "AWAITING_CONFIRMATION" or intake_stage == "review":
        confirmation_status = "AWAITING_CONFIRMATION"
    else:
        confirmation_status = "IN_PROGRESS"

    return {
        "fields": fields,
        "provided_fields": list(summary.provided_fields),
        "missing_fields": [field for field in summary.missing_fields if field not in pending_fields],
        "pending_fields": [field for field, _ in _PATIENT_REVIEW_FIELDS if field in pending_fields],
        "pending_question": session.pending_question,
        "confirmation_status": confirmation_status,
        "can_share": confirmation_status == "CONFIRMED",
        "disclaimer": summary.disclaimer,
    }


@app.get("/api/patient/session")
def patient_session(
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
) -> JSONResponse:
    orchestrator = _get_conversation_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")
    line_user_id = _resolve_patient_line_user_id(authorization, x_line_user_id)
    session = orchestrator.session_for_user(line_user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    review = _patient_review_payload(session)
    return JSONResponse(content={
        "session_id": session.session_id,
        "actor_role": str(session.actor_role),
        "information_source": str(session.information_source) if session.information_source else None,
        "authorization_status": str(session.authorization_status),
        "intake_stage": session.intake_stage,
        "status": session.status,
        "intake_snapshot": session.intake_snapshot.model_dump(mode="json"),
        "pending_question": session.pending_question,
        "system_risk_classification": session.system_risk_classification,
        "review": review,
    })


@app.post("/api/patient/sessions/{session_id}/share")
def create_patient_share(
    session_id: str,
    body: CreateShareRequest,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
) -> JSONResponse:
    orchestrator = _get_conversation_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")
    line_user_id = _resolve_patient_line_user_id(authorization, x_line_user_id)
    session = orchestrator.session_for_user(line_user_id)
    if session is None or session.session_id != session_id:
        raise HTTPException(status_code=403, detail="Session does not belong to this LINE user")
    allowed_hash = None
    if body.allowed_clinician_id:
        _require_demo_clinician(body.allowed_clinician_id)
        allowed_hash = orchestrator.principal_hash(f"clinician:{body.allowed_clinician_id}")
    from tfda_context_gate.product_session import ShareGrantDenied
    from tfda_context_gate.sharing import ShareGrantService
    try:
        issue = ShareGrantService(orchestrator.repository).create(
            session, allowed_practitioner_hash=allowed_hash
        )
    except ShareGrantDenied as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    qr_code_data_uri = ""
    try:
        from io import BytesIO
        import qrcode

        image = qrcode.make(issue.token)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        qr_code_data_uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:
        logger.warning("QR code generation failed: %s", exc)

    payload = issue.model_dump(mode="json")
    payload["qr_code_data_uri"] = qr_code_data_uri
    return JSONResponse(content=payload)


@app.post("/api/patient/share/{grant_id}/revoke")
def revoke_patient_share(
    grant_id: str,
    authorization: str = Header(default="", alias="Authorization"),
    x_line_user_id: str = Header(default="", alias="X-Line-User-Id"),
) -> JSONResponse:
    orchestrator = _get_conversation_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="ProductSession is not configured")
    line_user_id = _resolve_patient_line_user_id(authorization, x_line_user_id)
    from tfda_context_gate.product_session import ShareGrantDenied
    from tfda_context_gate.sharing import ShareGrantService
    try:
        grant = ShareGrantService(orchestrator.repository).revoke(
            grant_id, orchestrator.principal_hash(line_user_id)
        )
    except ShareGrantDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return JSONResponse(content={"grant_id": grant.grant_id, "status": grant.status})


@app.post("/api/clinician/share/redeem")
def redeem_clinician_share(
    body: RedeemShareRequest,
    x_demo_clinician_id: str = Header(default="", alias="X-Demo-Clinician-Id"),
) -> JSONResponse:
    practitioner = _require_demo_clinician(x_demo_clinician_id)
    orchestrator = _get_conversation_orchestrator()
    from tfda_context_gate.product_session import ShareGrantDenied
    from tfda_context_gate.sharing import ShareGrantService
    try:
        view = ShareGrantService(orchestrator.repository).redeem(body.token, practitioner)
    except ShareGrantDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return JSONResponse(content=view.model_dump(mode="json"))


@app.get("/api/clinician/audit")
def clinician_audit(
    x_demo_clinician_id: str = Header(default="", alias="X-Demo-Clinician-Id"),
) -> JSONResponse:
    practitioner = _require_demo_clinician(x_demo_clinician_id)
    orchestrator = _get_conversation_orchestrator()
    logs = orchestrator.repository.list_clinician_access_logs(practitioner.principal_id_hash)
    return JSONResponse(content={"events": [log.model_dump(mode="json") for log in logs]})


@app.post("/callback")
async def callback(
    request: Request,
    x_line_signature: str = Header(default="", alias="X-Line-Signature"),
) -> JSONResponse:
    """LINE Webhook entry: verifies signature, handles TextMessage and ImageMessage."""
    body = await request.body()
    secret = _get_secret()

    if not secret and not _unsigned_webhook_enabled():
        raise HTTPException(status_code=503, detail="LINE webhook signature verification is not configured")
    if secret and (not x_line_signature or not verify_signature(body, x_line_signature, secret)):
        raise HTTPException(status_code=400, detail="Invalid X-Line-Signature")

    # Parse events
    try:
        payload = body.decode("utf-8")
    except Exception:
        payload = ""

    # Try LINE SDK WebhookHandler parsing, fallback to manual JSON
    events: list[dict[str, Any]] = []
    try:
        import json

        data = json.loads(payload) if payload else {}
        events = data.get("events", []) if isinstance(data, dict) else []
    except Exception:
        events = []

    # If no events, return ok (LINE verification)
    if not events:
        return JSONResponse(content={"status": "ok", "events": 0})

    reply_failed = False

    def _send(reply_token: str, reply: str, *, quick_actions: list[dict[str, str]] | None = None) -> bool:
        nonlocal reply_failed
        delivered = _reply_text(reply_token, reply, quick_actions=quick_actions)
        if not delivered:
            reply_failed = True
        return delivered

    for ev in events:
        try:
            ev_type = ev.get("type", "")
            if _is_expired_line_reply_event(ev):
                # A retry cannot use its original reply token.  Returning 200
                # stops retry storms and, crucially, avoids replacing a valid
                # pre-visit-room launch token with an undeliverable one.
                continue
            if ev_type != "message":
                continue
            message = ev.get("message", {})
            msg_type = message.get("type", "")
            reply_token = ev.get("replyToken", "")
            source = ev.get("source", {})
            user_id = source.get("userId", "unknown")

            if msg_type == "text":
                text = message.get("text", "") or ""
                try:
                    _downgrade = _app_guarded_fallback_reason()
                    if _downgrade:
                        _app_record_guarded_downgrade(_downgrade)
                except Exception:
                    pass
                try:
                    _app_obs = _app_semantic_predict_with_timeout(text)
                    if _app_obs is not None:
                        try:
                            from tfda_context_gate.e_observability.tracer import TraceRecorder as _AppTrace

                            _app_dict = {}
                            try:
                                if hasattr(_app_obs, "to_trace_dict"):
                                    _app_dict = _app_obs.to_trace_dict()
                                elif isinstance(_app_obs, dict):
                                    _app_dict = dict(_app_obs)
                                else:
                                    for _k in ("route", "confidence", "margin", "latency_ms", "degraded", "mode"):
                                        if hasattr(_app_obs, _k):
                                            _app_dict[_k] = getattr(_app_obs, _k)
                            except Exception:
                                _app_dict = {}
                            _tr = _AppTrace(request_id=f"line-webhook-{text[:8]}", declared_role=None, original_query=None)
                            _tr.record(
                                "SEMANTIC_ROUTER",
                                "webhook",
                                "COMPLETED",
                                semantic_route=str(_app_dict.get("route") or getattr(_app_obs, "route", "UNKNOWN")),
                                semantic_confidence=float(_app_dict.get("confidence") or 0.0),
                                margin=float(_app_dict.get("margin") or 0.0),
                                latency_ms=float(_app_dict.get("latency_ms") or 0.0),
                                route_mode=str(_app_dict.get("mode") or _get_app_route_mode()),
                                degraded=bool(_app_dict.get("degraded", False)),
                            )
                            _tr.close(status="COMPLETED")
                        except Exception:
                            pass
                except Exception:
                    pass
                orchestrator = _get_conversation_orchestrator()
                if _is_previsit_trigger_text(text):
                    # A pre-visit phrase is a product boundary, not an intake
                    # answer.  If the dedicated room is not configured, fail
                    # closed with an actionable message; never fall through to
                    # the legacy multi-field LINE questionnaire.
                    if not _portal_available():
                        _send(reply_token, "看診前整理網頁目前尚未設定，請稍後再試。")
                        _mark_text_dedup(str(user_id), text)
                        continue
                    try:
                        previsit_event_id = str(ev.get("webhookEventId") or message.get("id") or "")
                        previsit_claim = None
                        if previsit_event_id and orchestrator is not None:
                            previsit_claim = orchestrator.repository.claim_webhook_event(
                                previsit_event_id, orchestrator._hash(str(user_id)),
                            )
                            if not previsit_claim:
                                existing = orchestrator.repository.get_webhook_event(previsit_event_id)
                                if (
                                    existing is not None
                                    and existing.status == "COMPLETED"
                                    and isinstance(existing.result, dict)
                                    and existing.result.get("kind") == "PREVISIT_ROOM_OPENED"
                                ):
                                    _send(reply_token, "看診前對談室已開啟，請使用剛剛收到的卡片繼續。")
                                else:
                                    _send(reply_token, "這則訊息正在處理中，請稍候再試。")
                                continue
                        # fail-closed: any exception skips card and falls through to normal flow
                        # The old LINE questionnaire has been retired.  This
                        # card always opens the dedicated patient room; demo
                        # uses /demo/previsit so it cannot resurrect a stale
                        # LINE intake session.
                        previsit_url, sess_id = _previsit_launch_url(request, line_user_id=str(user_id))
                        flex = _build_previsit_flex_message(previsit_url)
                        sent = False
                        try:
                            api = _get_messaging_api()
                            if api is not None:
                                from linebot.v3.messaging import FlexContainer, FlexMessage, ReplyMessageRequest

                                # The generated SDK accepts a plain dict at construction but
                                # silently serializes it as an empty ``{"type": "bubble"}``.
                                # Convert it first so LINE receives the actual body/footer.
                                contents = FlexContainer.from_dict(flex["contents"])
                                api.reply_message(ReplyMessageRequest(
                                    reply_token=reply_token,
                                    messages=[FlexMessage(altText=flex["altText"], contents=contents)],
                                ))
                                sent = True
                        except Exception:
                            sent = False
                        if not sent:
                            sent = _send(reply_token, f"已為你準備好看診前對談室：{previsit_url}")
                        if sent:
                            # Keep a minimal durable turn pair for webhook
                            # retries/context, but do not start LINE intake.
                            if orchestrator is not None:
                                try:
                                    _record_previsit_room_entry(orchestrator, str(user_id), text)
                                except Exception:
                                    # Delivery of the entry card is the user-
                                    # visible success.  A best-effort audit
                                    # write must not turn that into a second
                                    # error reply or legacy intake fallback.
                                    logger.warning("previsit room audit write failed")
                            _mark_text_dedup(str(user_id), text)
                            if previsit_claim:
                                orchestrator.repository.complete_webhook_event(
                                    previsit_event_id,
                                    {
                                        "kind": "PREVISIT_ROOM_OPENED",
                                        "session_id": sess_id,
                                        "reply": "已開啟看診前對談室。",
                                        "pushed": True,
                                    },
                                    claim_token=previsit_claim,
                                )
                            _mark_pushed(previsit_event_id)
                        elif previsit_claim:
                            # Release a failed reply so LINE's retry can make
                            # a fresh launch link instead of waiting for lease.
                            orchestrator.repository.fail_webhook_event(
                                previsit_event_id, claim_token=previsit_claim,
                            )
                        continue
                    except Exception:
                        # Do not route a failed room launch into the legacy
                        # LINE intake.  That would make the same user intent
                        # behave differently depending on a transient error.
                        _send(reply_token, "看診前整理網頁目前無法開啟，請稍後再試。")
                        _mark_text_dedup(str(user_id), text)
                        continue
                webhook_event_id = ev.get("webhookEventId") or message.get("id")
                if orchestrator is not None and webhook_event_id and user_id != "unknown":
                    # Crash recovery: the first process may have committed
                    # the placeholder and durable ASYNC_PENDING record before
                    # it reached the background scheduler.  Resume that job
                    # directly and never append the user/placeholder turns a
                    # second time.  The original text is persisted as event
                    # metadata; a replay body is not trusted as a substitute.
                    try:
                        pending_event = orchestrator.repository.get_webhook_event(str(webhook_event_id))
                    except Exception:
                        pending_event = None
                    if (
                        pending_event is not None
                        and pending_event.status == "COMPLETED"
                        and isinstance(pending_event.result, dict)
                        and pending_event.result.get("status") in {"ASYNC_PENDING", "ASYNC_PLACEHOLDER"}
                        and pending_event.result.get("pushed") is not True
                    ):
                        try:
                            if pending_event.principal_id_hash != orchestrator._hash(str(user_id)):
                                _send(reply_token, "此訊息已在處理中，請稍候。")
                                continue
                            pending_payload = pending_event.result
                            pending_reply = str(pending_payload.get("reply") or ASYNC_PLACEHOLDER_REPLY)
                            pending_session_id = str(
                                pending_payload.get("session_id") or orchestrator._session_id(str(user_id))
                            )
                            pending_text = pending_payload.get("async_original_text")
                            if not isinstance(pending_text, str) or not pending_text.strip():
                                recover = getattr(orchestrator, "_recover_pending_async_text", None)
                                pending_text = recover(pending_session_id) if callable(recover) else None
                            if isinstance(pending_text, str) and pending_text.strip():
                                _send(reply_token, pending_reply)
                                _schedule_formal_push(
                                    orchestrator,
                                    str(user_id),
                                    str(webhook_event_id),
                                    pending_text,
                                )
                                continue
                            _send(reply_token, "此訊息正在處理中，稍候再試。")
                            continue
                        except Exception:
                            # Return a processing response; never write a
                            # second turn when recovery cannot be completed.
                            _send(reply_token, "此訊息正在處理中，稍候再試。")
                            continue
                    if _is_duplicate_push(str(webhook_event_id), getattr(orchestrator, "repository", None)):
                        try:
                            existing = orchestrator.repository.get_webhook_event(str(webhook_event_id))
                            if existing is not None and existing.status == "COMPLETED" and existing.result:
                                if existing.result.get("kind") == "PREVISIT_ROOM_OPENED":
                                    _send(reply_token, "看診前對談室已開啟，請使用剛剛收到的卡片繼續。")
                                    continue
                                product_result = orchestrator.handle_text(
                                    event_id=str(webhook_event_id),
                                    line_user_id=str(user_id),
                                    text=text,
                                )
                                reply = product_result.reply
                                try:
                                    session = orchestrator.session_for_user(str(user_id))
                                    intake = getattr(session, "intake_snapshot", None) if session else None
                                    reply = _enrich_reply_with_stage_progress(reply, product_result.status, intake)
                                except Exception:
                                    pass
                                resume_actions = _resolve_resume_quick_actions(product_result)
                                if resume_actions is not None:
                                    quick_actions = resume_actions
                                else:
                                    reply = _maybe_enrich_entry_reply(reply, text)
                                    quick_actions = _quick_actions_for_status(product_result.status, reply)
                                _send(reply_token, reply, quick_actions=quick_actions)
                                continue
                        except Exception:
                            pass
                        _send(reply_token, "此訊息已在處理中，請稍候。")
                        continue
                    if _should_schedule_formal_push(orchestrator, str(user_id), text):
                        if _is_text_duplicate(str(user_id), text):
                            _send(reply_token, _dedup_reply_for(text))
                            continue
                        # P1.1 unified: claim event + write user turn + placeholder to same session before async push
                        try:
                            # Try to claim webhook event for async placeholder (SQLite is source of truth)
                            claim_token = None
                            try:
                                claim_token = orchestrator.repository.claim_webhook_event(str(webhook_event_id), orchestrator._hash(str(user_id)))
                            except Exception:
                                claim_token = None
                            if not claim_token:
                                # Another callback owns this event.  Never
                                # append a second user/placeholder pair while
                                # that owner is committing the durable record.
                                current = orchestrator.repository.get_webhook_event(str(webhook_event_id))
                                if current is not None and current.principal_id_hash != orchestrator._hash(str(user_id)):
                                    _send(reply_token, "此訊息正在處理中，請稍候。")
                                    continue
                                if current is not None and isinstance(current.result, dict) and current.result.get("status") in {"ASYNC_PENDING", "ASYNC_PLACEHOLDER"} and current.result.get("pushed") is not True:
                                    pending_text = current.result.get("async_original_text")
                                    pending_session_id = str(current.result.get("session_id") or orchestrator._session_id(str(user_id)))
                                    if not isinstance(pending_text, str) or not pending_text.strip():
                                        recover = getattr(orchestrator, "_recover_pending_async_text", None)
                                        pending_text = recover(pending_session_id) if callable(recover) else None
                                    _send(reply_token, str(current.result.get("reply") or ASYNC_PLACEHOLDER_REPLY))
                                    if isinstance(pending_text, str) and pending_text.strip():
                                        _schedule_formal_push(orchestrator, str(user_id), str(webhook_event_id), pending_text)
                                else:
                                    _send(reply_token, "此訊息正在處理中，請稍候。")
                                continue
                            # Load or create session and write user turn + placeholder
                            sess_async = orchestrator._load_or_create(str(user_id))
                            prev_ver = sess_async.version
                            ctx_async = orchestrator.context_manager.append_turn(sess_async.conversation_context, role="user", content=text)
                            ctx_async = orchestrator.context_manager.append_turn(ctx_async, role="assistant", content=ASYNC_PLACEHOLDER_REPLY)
                            ctx_async, _ = orchestrator.context_manager.compact(ctx_async, stage_completed=False)
                            sess_async = sess_async.model_copy(update={"conversation_context": ctx_async}, deep=True)
                            sess_async = orchestrator._sync_clinical_context(sess_async)
                            try:
                                saved_async = orchestrator.repository.save(sess_async, expected_version=prev_ver)
                                if claim_token:
                                    try:
                                        orchestrator.repository.complete_webhook_event(
                                            str(webhook_event_id),
                                            {
                                                "event_id": str(webhook_event_id),
                                                "session_id": saved_async.session_id,
                                                "reply": ASYNC_PLACEHOLDER_REPLY,
                                                "status": "ASYNC_PENDING",
                                                "intake_stage": saved_async.intake_stage,
                                                "async_original_text": text.strip(),
                                            },
                                            claim_token=claim_token,
                                        )
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        except Exception:
                            pass
                        _send(reply_token, ASYNC_PLACEHOLDER_REPLY)
                        _schedule_formal_push(orchestrator, str(user_id), str(webhook_event_id), text)
                        continue
                    if _is_text_duplicate(str(user_id), text) and _is_short_ttl_text(text):
                        _send(reply_token, _dedup_reply_for(text))
                        continue
                    product_result = orchestrator.handle_text(
                        event_id=str(webhook_event_id),
                        line_user_id=str(user_id),
                        text=text,
                    )
                    reply = product_result.reply
                    try:
                        session = orchestrator.session_for_user(str(user_id))
                        intake = getattr(session, "intake_snapshot", None) if session else None
                        reply = _enrich_reply_with_stage_progress(reply, product_result.status, intake)
                    except Exception:
                        pass
                    resume_actions = _resolve_resume_quick_actions(product_result)
                    if resume_actions is not None:
                        quick_actions = resume_actions
                    else:
                        reply = _maybe_enrich_entry_reply(reply, text)
                        quick_actions = _quick_actions_for_status(product_result.status, reply)
                    _send(reply_token, reply, quick_actions=quick_actions)
                    _mark_text_dedup(str(user_id), text)
                else:
                    # P0.5 fail-closed: no ProductSession → pre-visit must not start, general education may degrade; red flag remains priority
                    try:
                        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _RB2
                        from tfda_context_gate.clinical_safety import RiskSignalPolicy as _RSP2
                        is_red = _RSP2().classify(text).level == "RED_FLAG"
                    except Exception:
                        is_red = False
                    try:
                        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _RBPre2
                        is_pre_visit2 = _RBPre2.is_pre_visit_intake_text(text)
                    except Exception:
                        is_pre_visit2 = any(kw in text for kw in ["準備看診", "我要.*看診", "看醫生", "回診"])
                    if is_red:
                        from tfda_context_gate.workflow.fallbacks import fallback_response as _fb
                        _send(reply_token, _fb("A_EMERGENCY"))
                        _mark_text_dedup(str(user_id), text)
                    elif is_pre_visit2:
                        _send(reply_token, "目前無法安全開始整理，請先完成身分與授權設定後再試。")
                        _mark_text_dedup(str(user_id), text)
                    elif _should_use_async_formal(text, None) and not _is_duplicate_push(str(webhook_event_id) if webhook_event_id else None):
                        if _is_text_duplicate(str(user_id), text):
                            _send(reply_token, _dedup_reply_for(text))
                        else:
                            _send(reply_token, ASYNC_PLACEHOLDER_REPLY)
                            _schedule_formal_push(None, str(user_id), str(webhook_event_id) if webhook_event_id else f"compat-{uuid.uuid4().hex[:8]}", text)
                    else:
                        if _is_text_duplicate(str(user_id), text) and _is_short_ttl_text(text):
                            _send(reply_token, _dedup_reply_for(text))
                            _mark_text_dedup(str(user_id), text)
                        else:
                            result = handle_text_message(text, request_id=f"line-{user_id[:8]}-{uuid.uuid4().hex[:4]}")
                            reply = getattr(result, "final_response", str(result))
                            quick_actions = None
                            _send(reply_token, reply, quick_actions=quick_actions)
                            _mark_text_dedup(str(user_id), text)

            elif msg_type == "image":
                message_id = message.get("id", "")
                image_bytes = _download_image_content(message_id)
                if image_bytes is None:
                    # Fallback: try to get from event directly (for testing)
                    image_bytes = b""
                if not image_bytes:
                    _send(reply_token, "無法取得圖片，請重新傳送或確認圖片格式。")
                    continue
                orchestrator = _get_conversation_orchestrator()
                webhook_event_id = ev.get("webhookEventId") or message_id
                if orchestrator is not None and webhook_event_id and user_id != "unknown":
                    product_result = orchestrator.handle_image(
                        event_id=str(webhook_event_id),
                        line_user_id=str(user_id),
                        image_bytes=image_bytes,
                    )
                    reply = product_result.reply
                    quick_actions = [{"label": "我要準備看診", "text": "我要準備看診"}]
                else:
                    # P0.5 fail-closed: no ProductSession → image/OCR must not start intake
                    reply = "目前無法安全開始整理，請先完成身分與授權設定後再試。"
                    quick_actions = None
                _send(reply_token, reply, quick_actions=quick_actions)

            else:
                # Unsupported message type
                _send(reply_token, "目前僅支援文字與圖片訊息。請傳送文字或藥袋照片。")
        except Exception:
            # Never fail webhook; log and continue
            try:
                reply_token = ev.get("replyToken", "")
                if reply_token:
                    _send(reply_token, "目前系統無法完成安全處理，請稍後再試或改由合格醫療專業人員評估。")
            except Exception:
                pass
            continue

    if reply_failed:
        raise HTTPException(status_code=503, detail="LINE reply delivery failed; event may be retried")
    return JSONResponse(content={"status": "ok", "events": len(events)})


@app.post("/callback/stream")
async def callback_stream(
    request: Request,
    x_line_signature: str = Header(default="", alias="X-Line-Signature"),
) -> StreamingResponse:
    """Streaming variant: verifies signature, streams reply via SSE.

    For LINE, this is for internal testing; actual LINE reply still needs
    single message, so this endpoint is for local SSE clients.
    """
    body = await request.body()
    secret = _get_secret()
    if not secret and not _unsigned_webhook_enabled():
        raise HTTPException(status_code=503, detail="LINE webhook signature verification is not configured")
    if secret and (not x_line_signature or not verify_signature(body, x_line_signature, secret)):
        raise HTTPException(status_code=400, detail="Invalid X-Line-Signature")

    import json

    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        data = {}
    events = data.get("events", []) if isinstance(data, dict) else []
    if not events:
        # Direct JSON for local test: {text: "...", image_base64: "..."}
        text = data.get("text") or data.get("user_raw_input") or "請說明糖尿病的一般飲食原則。"
        image_b64 = data.get("image_base64") or data.get("image_bytes")
        if image_b64:
            try:
                image_bytes = base64.b64decode(image_b64)
                gen = handle_image_message(image_bytes, text_fallback=text, use_stream=True, sse_format=True)
            except Exception:
                gen = handle_text_message(text, use_stream=True, sse_format=True)
        else:
            gen = handle_text_message(text, use_stream=True, sse_format=True)

        def _iter() -> Iterator[str]:
            for chunk in gen:  # type: ignore[union-attr]
                yield chunk

        return StreamingResponse(_iter(), media_type="text/event-stream")

    # For webhook events, stream first event only
    ev = events[0]
    message = ev.get("message", {})
    msg_type = message.get("type", "")
    if msg_type == "text":
        text = message.get("text", "") or ""
        gen = handle_text_message(text, use_stream=True, sse_format=True)
    elif msg_type == "image":
        message_id = message.get("id", "")
        image_bytes = _download_image_content(message_id) or b""
        gen = handle_image_message(image_bytes or b"", use_stream=True, sse_format=True)
    else:
        gen = handle_text_message("請說明糖尿病的一般飲食原則。", use_stream=True, sse_format=True)

    def _iter2() -> Iterator[str]:
        for chunk in gen:  # type: ignore[union-attr]
            yield chunk

    return StreamingResponse(_iter2(), media_type="text/event-stream")


# ── Local test without LINE server (simulate via image_bytes) ───────────────
def simulate_text_message(text: str, **kwargs: Any) -> Any:
    """Simulate text message without LINE server: direct workflow call."""
    return handle_text_message(text, **kwargs)


def simulate_image_message(image_bytes: bytes, **kwargs: Any) -> Any:
    """Simulate image message without LINE server: OCR + workflow."""
    return handle_image_message(image_bytes, **kwargs)


def simulate_front_back_images(
    front_path: str | Path | None = None,
    back_path: str | Path | None = None,
    *,
    front_bytes: bytes | None = None,
    back_bytes: bytes | None = None,
    **kwargs: Any,
) -> Any:
    """Simulate front/back medication bag images without LINE server.

    Accepts either file paths or raw bytes. Loads from default 藥袋 images
    if no args provided.
    """
    if front_bytes is None and front_path is not None:
        front_bytes = Path(front_path).read_bytes()
    if back_bytes is None and back_path is not None:
        back_bytes = Path(back_path).read_bytes()
    # Default to repo root images if still None
    if front_bytes is None and back_bytes is None and front_path is None and back_path is None:
        root = Path(__file__).resolve().parents[1]
        for cand in [root / "fixtures/images/medication_bag_front.jpg", root / "藥袋 (正面).jpg"]:
            if cand.is_file():
                front_bytes = cand.read_bytes()
                break
        for cand in [root / "fixtures/images/medication_bag_back.jpg", root / "藥袋 (背面).jpg"]:
            if cand.is_file():
                back_bytes = cand.read_bytes()
                break
    return handle_front_back_images(front_bytes, back_bytes, **kwargs)


def local_test_with_images() -> dict[str, Any]:
    """Run local test with front/back images via image_bytes (no LINE server).

    Returns summary dict with OCR and workflow results. Keeps B/D gates mandatory,
    never stores raw image in WorkflowState.
    """
    root = Path(__file__).resolve().parents[1]
    front_candidates = [root / "fixtures/images/medication_bag_front.jpg", root / "藥袋 (正面).jpg"]
    back_candidates = [root / "fixtures/images/medication_bag_back.jpg", root / "藥袋 (背面).jpg"]
    front_path = next((p for p in front_candidates if p.is_file()), front_candidates[0])
    back_path = next((p for p in back_candidates if p.is_file()), back_candidates[0])

    front_bytes = front_path.read_bytes() if front_path.is_file() else None
    back_bytes = back_path.read_bytes() if back_path.is_file() else None

    # Also test OCR service directly
    ocr_summary: dict[str, Any] = {}
    try:
        from tfda_context_gate.intake.qr_ocr_service import MedicationBagOCRService

        svc = MedicationBagOCRService()
        if front_bytes:
            front_result = svc.extract(front_bytes)
            ocr_summary["front"] = {
                "meds": front_result.get("meds", []),
                "confidence": front_result.get("confidence", 0),
                "qr_used": front_result.get("qr_used", False),
                "ocr_used": front_result.get("ocr_used", False),
            }
        if back_bytes:
            back_result = svc.extract(back_bytes)
            ocr_summary["back"] = {
                "meds": back_result.get("meds", []),
                "confidence": back_result.get("confidence", 0),
                "qr_used": back_result.get("qr_used", False),
                "ocr_used": back_result.get("ocr_used", False),
            }
        if front_bytes or back_bytes:
            merged = svc.extract_front_back(front_bytes, back_bytes)
            ocr_summary["merged"] = {
                "meds": merged.get("meds", []),
                "confidence": merged.get("confidence", 0),
                "merged_from": merged.get("merged_from", ""),
            }
    except Exception as exc:
        ocr_summary["error"] = str(exc)

    # Test workflow with image_bytes (B/D gates mandatory)
    workflow_results: dict[str, Any] = {}
    try:
        # Single front image
        if front_bytes:
            r1 = handle_image_message(front_bytes, request_id="local-test-front")
            workflow_results["front_workflow"] = {
                "status": getattr(r1, "status", ""),
                "final_response": getattr(r1, "final_response", "")[:500],
                "fallback_reason": getattr(r1, "fallback_reason", None),
            }
        # Front+back
        if front_bytes or back_bytes:
            r2 = handle_front_back_images(front_bytes, back_bytes, request_id="local-test-both")
            workflow_results["front_back_workflow"] = {
                "status": getattr(r2, "status", ""),
                "final_response": getattr(r2, "final_response", "")[:500],
                "fallback_reason": getattr(r2, "fallback_reason", None),
            }
        # Text only
        r3 = handle_text_message("請說明糖尿病的一般飲食原則。", request_id="local-test-text")
        workflow_results["text_workflow"] = {
            "status": getattr(r3, "status", ""),
            "final_response": getattr(r3, "final_response", "")[:500],
            "fallback_reason": getattr(r3, "fallback_reason", None),
        }
        # Streaming test
        chunks = list(handle_text_message("請說明糖尿病的一般飲食原則。", request_id="local-test-stream", use_stream=True))
        workflow_results["stream_chunks"] = len(chunks)
        workflow_results["stream_preview"] = "".join(chunks)[:300] if chunks else ""
    except Exception as exc:
        workflow_results["error"] = str(exc)
        import traceback

        workflow_results["traceback"] = traceback.format_exc()[:2000]

    return {"ocr": ocr_summary, "workflow": workflow_results}


if __name__ == "__main__":
    import json

    print("=== LINE Bot Local Test (no LINE server, via image_bytes) ===")
    result = local_test_with_images()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Also test signature verification
    print("\n=== Signature Verification Test ===")
    test_secret = "test_secret"
    test_body = b'{"events":[]}'
    mac = hmac.new(test_secret.encode(), test_body, hashlib.sha256).digest()
    sig = base64.b64encode(mac).decode()
    print(f"verify_signature valid: {verify_signature(test_body, sig, test_secret)}")
    print(f"verify_signature invalid: {verify_signature(test_body, 'invalid', test_secret)}")
    print("\n=== FastAPI app ready: uvicorn line_bot.app:app --reload ===")
