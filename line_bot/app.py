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
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator

# Ensure project root is on sys.path for direct execution (python line_bot/app.py)
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
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
        )
    return run_workflow(req, intake_data=intake_data)


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
        )
    return run_workflow(
        req,
        intake_data=intake_data,
        image_bytes=image_bytes,
        ocr_service=ocr_service,
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
        )
    return run_workflow(
        req,
        intake_data=intake_data,
        image_bytes_front=front_bytes,
        image_bytes_back=back_bytes,
        ocr_service=ocr_service,
    )


def stream_text_reply(
    text: str,
    *,
    request_id: str | None = None,
    chunk_size: int = 20,
    sse_format: bool = False,
) -> Iterator[str]:
    """Streaming helper for text: yields chunks via stream_workflow."""
    gen = handle_text_message(text, request_id=request_id, use_stream=True, chunk_size=chunk_size, sse_format=sse_format)
    yield from gen  # type: ignore[misc]


def stream_image_reply(
    image_bytes: bytes,
    *,
    text_fallback: str = "請幫我辨識藥袋上的藥品",
    request_id: str | None = None,
    chunk_size: int = 20,
    sse_format: bool = False,
) -> Iterator[str]:
    """Streaming helper for image: OCR + stream_workflow."""
    gen = handle_image_message(
        image_bytes, text_fallback=text_fallback, request_id=request_id, use_stream=True, chunk_size=chunk_size, sse_format=sse_format
    )
    yield from gen  # type: ignore[misc]


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
    except Exception:
        return False


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
    if status == "NEEDS_CONFIRMATION":
        return REVIEW_ACTIONS
    if status == "NEEDS_CLARIFICATION" and "家人本人描述" in reply:
        return PROXY_SOURCE_ACTIONS
    if status in {"PAUSED", "SIDE_ANSWER"}:
        return [{"label": "繼續整理", "text": "繼續整理"}]
    if status == "NEEDS_CLARIFICATION":
        if "第 1/8 題" in reply:
            return [
                {"label": "目前沒有用藥", "text": "目前沒有用藥"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "第 2/8 題" in reply:
            return [
                {"label": "沒有過敏", "text": "沒有過敏"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "第 3/8 題" in reply:
            return [
                {"label": "沒有其他慢性病", "text": "沒有其他慢性病"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "第 4/8 題" in reply:
            return [
                {"label": "沒有家族史", "text": "沒有家族史"},
                {"label": "不清楚", "text": "不清楚"},
                {"label": "暫停整理", "text": "暫停整理"},
            ]
        if "第 8/8 題" in reply:
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
    """Download image via MessagingApiBlob.get_message_content."""
    if not message_id:
        return None
    blob_api = _get_blob_api()
    if blob_api is None:
        return None
    try:
        # get_message_content returns bytes or file-like
        content = blob_api.get_message_content(message_id=message_id)
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        # Some SDK versions return response with .data or file object
        if hasattr(content, "read"):
            return content.read()  # type: ignore[union-attr]
        if hasattr(content, "data"):
            return bytes(getattr(content, "data"))
        return bytes(content)  # type: ignore[arg-type]
    except Exception:
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
    return FileResponse(Path(__file__).parent / "static" / "clinician.html")


@app.get("/api/line/rich-menu")
def rich_menu_definition(patient_portal_url: str) -> JSONResponse:
    """回傳部署用定義，不主動呼叫 LINE API 或覆寫既有 Rich Menu。"""
    from line_bot.ui import build_rich_menu_payload
    if not patient_portal_url.startswith("https://"):
        raise HTTPException(status_code=422, detail="patient_portal_url must use https")
    return JSONResponse(content=build_rich_menu_payload(patient_portal_url=patient_portal_url))


@app.get("/api/line/client-config")
def line_client_config() -> dict[str, Any]:
    return {
        "liff_id": os.getenv("LINE_LIFF_ID", ""),
        "demo_identity_headers": _demo_identity_headers_enabled(),
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
    return JSONResponse(content=issue.model_dump(mode="json"))


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

    def _send(reply_token: str, reply: str, *, quick_actions: list[dict[str, str]] | None = None) -> None:
        nonlocal reply_failed
        if not _reply_text(reply_token, reply, quick_actions=quick_actions):
            reply_failed = True

    for ev in events:
        try:
            ev_type = ev.get("type", "")
            if ev_type != "message":
                continue
            message = ev.get("message", {})
            msg_type = message.get("type", "")
            reply_token = ev.get("replyToken", "")
            source = ev.get("source", {})
            user_id = source.get("userId", "unknown")

            if msg_type == "text":
                text = message.get("text", "") or ""
                orchestrator = _get_conversation_orchestrator()
                webhook_event_id = ev.get("webhookEventId") or message.get("id")
                if orchestrator is not None and webhook_event_id and user_id != "unknown":
                    product_result = orchestrator.handle_text(
                        event_id=str(webhook_event_id),
                        line_user_id=str(user_id),
                        text=text,
                    )
                    reply = product_result.reply
                    quick_actions = _quick_actions_for_status(product_result.status, reply)
                else:
                    # 單輪相容模式仍完整經過 B/D gates。
                    result = handle_text_message(text, request_id=f"line-{user_id[:8]}-{uuid.uuid4().hex[:4]}")
                    reply = getattr(result, "final_response", str(result))
                    quick_actions = None
                _send(reply_token, reply, quick_actions=quick_actions)

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
                    quick_actions = _quick_actions_for_status(product_result.status, reply)
                else:
                    # OCR + 單輪相容工作流（raw image 永不存入 state）
                    result = handle_image_message(
                        image_bytes,
                        text_fallback="請幫我辨識藥袋上的藥品",
                        request_id=f"line-img-{user_id[:8]}-{uuid.uuid4().hex[:4]}",
                    )
                    reply = getattr(result, "final_response", str(result))
                    quick_actions = None
                # Optionally include OCR hint if available in trace
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
