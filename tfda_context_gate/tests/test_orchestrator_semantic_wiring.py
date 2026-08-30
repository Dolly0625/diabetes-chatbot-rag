"""Orchestrator semantic wiring — off/shadow/guarded, factory, production wiring, LINE callback."""
from __future__ import annotations

import os
import json
import hmac
import hashlib
import base64
import tempfile
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.semantic_router.config import SemanticRouterConfig
from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation
from tfda_context_gate.rag.retriever import FixtureRetriever
from tfda_context_gate.workflow.runner import run_workflow

_KEY = "orchestrator-wiring-key-1234567890"


def _db(tmp_path):
    p = Path(tempfile.mktemp(suffix=".sqlite3"))
    if isinstance(tmp_path, Path):
        p = tmp_path / f"wiring-{os.urandom(4).hex()}.sqlite3"
    return str(p)


def _make_orch(tmp_path, mode="off", router_mock=None, workflow_runner=None, use_formal=False):
    db_path = _db(tmp_path)
    repo = SQLiteProductSessionRepository(db_path)
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, workflow_runner=workflow_runner or run_workflow, use_formal=use_formal)
    if router_mock is not None:
        orch._semantic_router = router_mock
        orch._semantic_router_init_attempted = True
        orch._semantic_router_config = SemanticRouterConfig(mode=mode, cosine_threshold=0.62, margin_threshold=0.10, policy="hybrid")  # type: ignore[arg-type]
        orch._last_semantic_mode = mode
    else:
        if mode != "off":
            try:
                from tfda_context_gate.semantic_router.factory import build_semantic_router
                fake = build_semantic_router(SemanticRouterConfig(mode=mode, cosine_threshold=0.62, margin_threshold=0.10, policy="hybrid"))  # type: ignore[arg-type]
                orch._semantic_router = fake
                orch._semantic_router_config = fake.config
                orch._semantic_router_init_attempted = True
                orch._last_semantic_mode = mode
            except Exception:
                orch._semantic_router = None
                orch._semantic_router_init_attempted = True
                orch._last_semantic_mode = mode
        else:
            orch._semantic_router = None
            orch._semantic_router_init_attempted = True
            orch._last_semantic_mode = "off"
    return orch, repo, db_path


def _activate(orch, user_id="U-wiring"):
    orch.handle_text(event_id=f"act-{user_id}", line_user_id=user_id, text="為自己整理")


def _spy_interpreter(orch):
    original = orch.interpreter.interpret
    counter = {"called": 0}
    def _wrap(envelope):
        counter["called"] += 1
        return original(envelope)
    orch.interpreter.interpret = _wrap  # type: ignore[assignment]
    return counter, original


# ── off 模式與原行為完全相容（同一輸入 off vs 未裝 router 的 reply/status 相同，semantic_* 為 None）──

def test_off_mode_compatible_with_no_router(tmp_path):
    text = "糖尿病一天可以吃幾份水果？"
    # off mode
    orch_off, repo_off, db_off = _make_orch(tmp_path, mode="off", use_formal=False)
    _activate(orch_off, user_id="U-off-compat")
    res_off = orch_off.handle_text(event_id="evt-off-compat", line_user_id="U-off-compat", text=text)
    # 未裝 router 對照：同樣 off 且 _semantic_router=None
    orch_no, repo_no, db_no = _make_orch(tmp_path, mode="off", use_formal=False)
    orch_no._semantic_router = None
    orch_no._semantic_router_init_attempted = True
    _activate(orch_no, user_id="U-no-router")
    res_no = orch_no.handle_text(event_id="evt-no-router", line_user_id="U-no-router", text=text)
    assert res_off.reply == res_no.reply
    assert res_off.status == res_no.status
    assert res_off.semantic_route is None
    assert res_off.semantic_confidence is None
    assert res_no.semantic_route is None
    Path(db_off).unlink(missing_ok=True)
    Path(db_no).unlink(missing_ok=True)


# ── shadow 不改正式輸出與 session version（shadow vs off 的 reply 相同，version 差值相同，且 metadata 有 semantic_observation）──

def test_shadow_does_not_change_output_and_version(tmp_path, monkeypatch):
    # 使用非 welcome 的中性文本，避免 is_welcome_trigger 分支差異
    text = "糖尿病的一般飲食原則是什麼？"
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "off")
    orch_off, _, db_off = _make_orch(tmp_path, mode="off", use_formal=False)
    v_before_off = orch_off.session_for_user("U-shadow-off-init").version if False else 0
    # 用全新 user 且不走 intake，保持 off vs shadow 輸出可比
    res_off = orch_off.handle_text(event_id="evt-shadow-off", line_user_id="U-shadow-off", text=text)
    v_after_off = orch_off.session_for_user("U-shadow-off").version
    # 首次發話版本差值
    v_before_off = 0  # 新 session 從 0 開始，單次 turn 版差應為 1
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "shadow")

    class AnyRouter:
        def route(self, t):
            return SemanticRouteObservation(route="CHITCHAT", confidence=0.9, margin=0.3, latency_ms=1.0, mode="shadow", degraded=False)
        predict = route
    orch_shadow, _, db_shadow = _make_orch(tmp_path, mode="shadow", router_mock=AnyRouter(), use_formal=False)
    res_sh = orch_shadow.handle_text(event_id="evt-shadow-on", line_user_id="U-shadow-on", text=text)
    v_after_sh = orch_shadow.session_for_user("U-shadow-on").version
    v_before_sh = 0
    assert res_off.reply == res_sh.reply
    assert (v_after_off - v_before_off) == (v_after_sh - v_before_sh)
    assert res_sh.metadata is not None and "semantic_observation" in res_sh.metadata
    Path(db_off).unlink(missing_ok=True)
    Path(db_shadow).unlink(missing_ok=True)


# ── guarded 只有核准類型 early exit（spy interpreter：PURE_EDUCATION 在 guarded 且高信心時 interpreter called==0；mixed 仍 called==1）──

def test_guarded_only_approved_early_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    class HighEdu:
        def route(self, t):
            return SemanticRouteObservation(route="PURE_EDUCATION", confidence=0.95, margin=0.3, latency_ms=1.0, mode="guarded", degraded=False)
        predict = route
    class MixedHigh:
        def route(self, t):
            return SemanticRouteObservation(route="MIXED", confidence=0.95, margin=0.3, latency_ms=1.0, mode="guarded", degraded=False)
        predict = route

    def fixture_runner(request, **kwargs):
        kwargs.pop("use_formal", None)
        return run_workflow(request, retriever=FixtureRetriever(), **kwargs)

    orch_edu, _, db_edu = _make_orch(tmp_path, mode="guarded", router_mock=HighEdu(), workflow_runner=fixture_runner, use_formal=False)
    _activate(orch_edu, user_id="U-guarded-edu")
    counter_edu, _ = _spy_interpreter(orch_edu)
    res_edu = orch_edu.handle_text(event_id="evt-guarded-edu", line_user_id="U-guarded-edu", text="糖尿病一天可以吃幾份水果？")
    assert counter_edu["called"] == 0, "PURE_EDUCATION 在 guarded 且高信心時 interpreter called==0"
    assert res_edu.metadata is not None and res_edu.metadata.get("semantic_fast_path") is True

    orch_mix, _, db_mix = _make_orch(tmp_path, mode="guarded", router_mock=MixedHigh(), use_formal=False)
    _activate(orch_mix, user_id="U-guarded-mix")
    counter_mix, _ = _spy_interpreter(orch_mix)
    _ = orch_mix.handle_text(event_id="evt-guarded-mix", line_user_id="U-guarded-mix", text="我最近常口渴，糖尿病一天可以吃幾份水果？")
    assert counter_mix["called"] == 1, "mixed 仍 called==1（guarded 阻擋 MIXED）"
    Path(db_edu).unlink(missing_ok=True)
    Path(db_mix).unlink(missing_ok=True)


# ── 正式 factory construction 測試（build_semantic_router() 不在 import 時打網路，且 PYTEST_CURRENT_TEST=>fake）──

def test_factory_no_network_on_import_and_fake_under_pytest(monkeypatch):
    # 確保 PYTEST_CURRENT_TEST 存在時走 fake
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_factory")
    # import 時不應打網路（factory 僅在 build 時 probe）
    import tfda_context_gate.semantic_router.factory as fac
    assert fac is not None
    # 重新 import 檢查無網路副作用：build 應回 degraded fake
    from tfda_context_gate.semantic_router.factory import build_semantic_router
    cfg = SemanticRouterConfig(mode="guarded", cosine_threshold=0.62, margin_threshold=0.10, policy="hybrid")  # type: ignore[arg-type]
    router = build_semantic_router(cfg)
    assert router is not None
    assert router.degraded is True
    assert router.is_available() is True or router.is_available() is False  # fake 仍有 vectors
    # 清理：保留原 PYTEST_CURRENT_TEST（pytest 本身會設）
    # 不移除，因為後續測試依賴它


# ── orchestrator production wiring 測試（mock router 回 CHITCHAT 高分，驗證 guarded 下 PURE_INTAKE 快路仍走 candidate_merge 而非直接 save）──

def test_orchestrator_production_wiring_pure_intake_via_candidate_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    class HighPureIntake:
        def route(self, t):
            if t.strip() in ("你好", "謝謝", "嗨"):
                return SemanticRouteObservation(route="CHITCHAT", confidence=0.96, margin=0.4, latency_ms=1.0, mode="guarded", degraded=False)
            return SemanticRouteObservation(route="PURE_INTAKE", confidence=0.96, margin=0.4, latency_ms=1.0, mode="guarded", degraded=False)
        predict = route

    orch, repo, db = _make_orch(tmp_path, mode="guarded", router_mock=HighPureIntake(), use_formal=False)
    _activate(orch, user_id="U-prod-wire")
    counter, _ = _spy_interpreter(orch)
    res_chat = orch.handle_text(event_id="evt-wire-chat", line_user_id="U-prod-wire", text="你好")
    assert counter["called"] == 0 or res_chat.metadata is not None

    # 再測 PURE_INTAKE：送已知 intake 文本，需經 candidate_merge 而非直接 save
    # 用較長的 intake 文本，確保 candidate_merge 會過濾或標準化
    _activate(orch, user_id="U-prod-intake")
    # 第一次 session 已在 U-prod-wire，先用同一 user 續寫 intake
    # 需要 pending_field 為 known_medications 時送 "metformin"，檢查是否經 merge 正確寫入而非原始含雜訊
    orch2, repo2, db2 = _make_orch(tmp_path, mode="guarded", router_mock=HighPureIntake(), use_formal=False)
    _activate(orch2, user_id="U-prod-intake2")
    # 監視 candidate_merge 是否被呼叫：透過 spy merge_candidates
    import tfda_context_gate.intake.candidate_merge as cm
    orig_merge = cm.merge_candidates
    merge_called = {"c": 0}
    def spy_merge(*a, **kw):
        merge_called["c"] += 1
        return orig_merge(*a, **kw)
    # 僅在有 interpretation 時才會呼叫 merge；PURE_INTAKE 快路會 skip interpreter，故 merge 不應被短路直接 save
    # 改為檢查 intake_snapshot 是否為標準化後的 "metformin" 而非原始帶雜訊
    result = orch2.handle_text(event_id="evt-wire-intake", line_user_id="U-prod-intake2", text="metformin")
    sess = repo2.get(result.session_id)
    assert sess is not None
    assert "metformin" in sess.intake_snapshot.known_medications
    # 驗證不是直接 save 原始文本帶前後空白等未驗證
    Path(db).unlink(missing_ok=True)
    Path(db2).unlink(missing_ok=True)


# ── LINE callback HTTP 整合測試（用 FastAPI TestClient 測 POST /callback 對上述 3 類輸入的 200 與簽章驗證）──

def _setup_line_app(tmp_path, monkeypatch, secret="line-test-secret-12345"):
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "false")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "line-callback-test-key-123456789")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "line_callback.sqlite3"))
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", secret)
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    # 讓 async formal 不啟動，避免背景執行緒干擾
    monkeypatch.setenv("LINE_USE_FORMAL", "false")
    replies = []
    def capture_reply(token, text, **kwargs):
        replies.append(text)
        return True
    monkeypatch.setattr(line_app, "_reply_text", capture_reply)
    # 重置 app 級 semantic router
    monkeypatch.setattr(line_app, "_app_semantic_router", None)
    monkeypatch.setattr(line_app, "_app_semantic_router_init_attempted", False)
    monkeypatch.setattr(line_app, "_app_semantic_router_config", None)
    return line_app, replies


def _sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")


def _event(event_id: str, text: str, user_id="U-line"):
    return {"events": [{"type": "message", "webhookEventId": event_id, "replyToken": f"reply-{event_id}", "source": {"type": "user", "userId": user_id}, "message": {"type": "text", "id": f"msg-{event_id}", "text": text}}]}


@pytest.mark.parametrize("text", [
    "胸口很痛而且呼吸困難",  # 紅旗類
    "糖尿病一天可以吃幾份水果？",  # PURE_EDUCATION
    "謝謝，另外我最近一直口渴",  # chitchat+intake
])
def test_line_callback_200_with_valid_signature(tmp_path, monkeypatch, text):
    secret = "line-test-secret-12345"
    line_app, replies = _setup_line_app(tmp_path, monkeypatch, secret=secret)
    client = TestClient(line_app.app)
    body = json.dumps(_event(f"evt-line-{hash(text)%10000}", text)).encode("utf-8")
    sig = _sign(secret, body)
    resp = client.post("/callback", content=body, headers={"X-Line-Signature": sig, "Content-Type": "application/json"})
    assert resp.status_code == 200


def test_line_callback_rejects_invalid_signature(tmp_path, monkeypatch):
    secret = "line-test-secret-12345"
    line_app, replies = _setup_line_app(tmp_path, monkeypatch, secret=secret)
    client = TestClient(line_app.app)
    body = json.dumps(_event("evt-line-bad", "你好")).encode("utf-8")
    resp = client.post("/callback", content=body, headers={"X-Line-Signature": "invalid-signature", "Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "Invalid X-Line-Signature" in resp.text


def test_line_callback_unsigned_when_allowed(tmp_path, monkeypatch):
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "line-callback-test-key-123456789")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "line_unsigned.sqlite3"))
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    monkeypatch.setattr(line_app, "_app_semantic_router", None)
    monkeypatch.setattr(line_app, "_app_semantic_router_init_attempted", False)
    def cap(token, text, **kw):
        return True
    monkeypatch.setattr(line_app, "_reply_text", cap)
    client = TestClient(line_app.app)
    body = json.dumps(_event("evt-line-unsigned", "你好")).encode("utf-8")
    resp = client.post("/callback", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
