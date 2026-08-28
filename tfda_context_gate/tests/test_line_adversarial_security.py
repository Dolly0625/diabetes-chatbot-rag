from __future__ import annotations

from pathlib import Path
import importlib
import base64
import hashlib
import hmac
import json

from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.workflow import run_workflow


_KEY = "line-adversarial-test-key-123456"


def _submitted(orchestrator: ConversationOrchestrator, user_id: str = "U-risk") -> None:
    values = [
        "為自己整理",
        "吃 metformin，無過敏，有高血壓，家族無糖尿病",
        "三天前開始，早晨血糖偏高，程度4/10",
        "我想問醫師飲食要注意什麼？",
        "確認完成",
    ]
    for index, value in enumerate(values):
        orchestrator.handle_text(event_id=f"setup-{user_id}-{index}", line_user_id=user_id, text=value)


def test_emergency_response_is_explicit_and_never_downgraded_by_summary_command(tmp_path: Path):
    repository = SQLiteProductSessionRepository(tmp_path / "risk.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    _submitted(orchestrator)

    emergency = orchestrator.handle_text(
        event_id="risk-red", line_user_id="U-risk", text="我現在呼吸困難而且快昏倒"
    )
    summary = orchestrator.handle_text(
        event_id="risk-summary", line_user_id="U-risk", text="查看看診摘要"
    )
    session = orchestrator.session_for_user("U-risk")

    assert emergency.status == summary.status == "FALLBACK"
    assert "119" in emergency.reply and "急診" in emergency.reply
    assert "119" in summary.reply and "急診" in summary.reply
    assert session is not None
    assert session.system_risk_classification["level"] == "RED_FLAG"
    assert session.conversation_context.clinical_state.system_risk_classification.level == "RED_FLAG"


def test_workflow_emergency_uses_dedicated_emergency_fallback():
    result = run_workflow({
        "request_id": "emergency-output-001",
        "schema_version": "a.v0.1",
        "user_raw_input": "我現在呼吸困難而且快昏倒",
        "declared_role": "PATIENT",
        "language": "zh-TW",
    })

    assert result.fallback_reason in {"A_EMERGENCY", "A_URGENT_HUMAN"}
    assert "119" in result.final_response


def test_product_summary_is_rejected_when_previsit_d_gate_fails(tmp_path: Path, monkeypatch):
    repository = SQLiteProductSessionRepository(tmp_path / "gate.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    _submitted(orchestrator, "U-gate")

    from tfda_context_gate.d_output_gate.schemas import OutputGateResult
    monkeypatch.setattr(
        "tfda_context_gate.d_output_gate.gate.run_previsit_output_gate",
        lambda _payload: OutputGateResult(
            request_id="forced",
            schema_version="d.v0.1",
            decision="FALLBACK",
            passed=False,
            failure_type="POLICY",
            reason_codes=["FORCED_ADVERSARIAL_FAILURE"],
            final_response="D_GATE_FORCED_FALLBACK",
        ),
    )

    result = orchestrator.handle_text(
        event_id="forced-gate", line_user_id="U-gate", text="查看看診摘要"
    )

    assert result.status == "FALLBACK"
    assert result.reply == "D_GATE_FORCED_FALLBACK"


def test_webhook_fails_closed_when_channel_secret_is_missing(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "false")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")

    response = TestClient(line_app.app).post("/callback", json={"events": []})

    assert response.status_code == 503


def test_callback_returns_retryable_error_when_line_reply_fails(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", _KEY)
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "reply.sqlite3"))
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    monkeypatch.setattr(line_app, "_reply_text", lambda *_args, **_kwargs: False)
    payload = {"events": [{
        "type": "message", "webhookEventId": "reply-fail", "replyToken": "token",
        "source": {"type": "user", "userId": "U-reply"},
        "message": {"type": "text", "id": "message-reply", "text": "我要準備看診"},
    }]}

    response = TestClient(line_app.app).post("/callback", json=payload)

    assert response.status_code == 503
    monkeypatch.setattr(line_app, "_reply_text", lambda *_args, **_kwargs: True)
    retry = TestClient(line_app.app).post("/callback", json=payload)
    session = line_app._get_conversation_orchestrator().session_for_user("U-reply")
    assert retry.status_code == 200
    assert session is not None and len(session.conversation_context.recent_turns) == 2


def test_signed_webhook_rejects_missing_and_invalid_signatures(monkeypatch):
    from fastapi.testclient import TestClient
    line_app = importlib.import_module("line_bot.app")
    secret = "adversarial-signature-secret"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "false")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", secret)
    body = json.dumps({"events": []}, separators=(",", ":")).encode()
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    client = TestClient(line_app.app)

    missing = client.post("/callback", content=body, headers={"content-type": "application/json"})
    invalid = client.post("/callback", content=body, headers={"content-type": "application/json", "X-Line-Signature": "bad"})
    valid = client.post("/callback", content=body, headers={"content-type": "application/json", "X-Line-Signature": signature})

    assert missing.status_code == invalid.status_code == 400
    assert valid.status_code == 200


def test_cross_principal_event_replay_returns_safe_error_not_victim_result(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", _KEY)
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "bound.sqlite3"))
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    replies: list[str] = []
    monkeypatch.setattr(line_app, "_reply_text", lambda _token, text, **_kwargs: replies.append(text) is None)
    client = TestClient(line_app.app)

    def payload(user_id: str, text: str):
        return {"events": [{
            "type": "message", "webhookEventId": "same-event", "replyToken": "reply",
            "source": {"type": "user", "userId": user_id},
            "message": {"type": "text", "id": "same-message", "text": text},
        }]}

    assert client.post("/callback", json=payload("U-victim", "為自己整理")).status_code == 200
    assert client.post("/callback", json=payload("U-attacker", "代家人整理")).status_code == 200

    assert len(replies) == 2
    assert "目前系統無法完成安全處理" in replies[1]
    assert "目前使用的藥品" not in replies[1]
    assert line_app._get_conversation_orchestrator().session_for_user("U-attacker") is None


def test_health_fails_when_core_line_security_configuration_is_missing(monkeypatch):
    line_app = importlib.import_module("line_bot.app")
    for name in (
        "LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_ACCESS_TOKEN",
        "LINE_CHANNEL_TOKEN",
        "LINE_IDENTITY_HASH_KEY",
        "LINE_LOGIN_CHANNEL_ID",
        "LINE_LIFF_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)

    response = line_app.health()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["checks"]["webhook_signature"] is False
    assert payload["checks"]["messaging_api"] is False
    assert payload["checks"]["product_session"] is False


def test_health_exposes_capability_flags_but_never_secret_values(tmp_path: Path, monkeypatch):
    line_app = importlib.import_module("line_bot.app")
    secret = "health-channel-secret-do-not-return"
    token = "health-access-token-do-not-return"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", token)
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", _KEY)
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "health.sqlite3"))
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "login-channel-id")
    monkeypatch.setenv("LINE_LIFF_ID", "liff-id")
    monkeypatch.setenv("LINE_DEMO_MODE", "false")
    monkeypatch.setenv("DEMO_CLINICIAN_IDS", "demo-doctor")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)

    response = line_app.health()
    body = response.body.decode("utf-8")
    payload = json.loads(body)

    assert response.status_code == 200
    assert payload["checks"]["patient_liff"] is True
    assert payload["checks"]["demo_clinician"] is False
    assert secret not in body
    assert token not in body
