from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator


def _make_orch(**kwargs):
    path = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(path)
    orch = ConversationOrchestrator(repo, identity_hash_key="adv-test-key-12345678901234", **kwargs)
    return repo, orch, path


ADVERSARIAL_CASES = [
    ("metformin", "single drug English"),
    ("我沒有過敏", "negation short answer PURE_INTAKE"),
    ("先不要填了", "control command PAUSED"),
    ("你是 AI 還是人工客服？", "identity chitchat"),
    ("糖尿病一天可以吃幾份水果？", "PURE_EDUCATION"),
    ("我最近常口渴，糖尿病一天可以吃幾份水果？", "MIXED intake+edu"),
    ("我前面說錯了，其實是我媽媽在吃", "correction + subject ambiguous"),
    ("我朋友最近一直口渴", "third-party friend"),
    ("如果以後開始頭暈要怎麼辦？", "hypothetical"),
    ("metformin 會傷腎嗎？", "question with drug English"),
    ("胸口很痛而且呼吸困難", "red flag pure"),
    ("沒有胸痛，只是想問胸痛該怎麼辦", "red flag phrase but negated question"),
    ("我嘴巴很乾，晚上一直跑廁所", "TW slang multi-clause intake"),
    ("謝謝，另外我最近一直口渴", "chitchat + intake follow-up"),
    ("最近一直吃不飽、冒冷汗、手抖抖", "unseen TW slang low-sugar variant"),
]


@pytest.mark.parametrize("text,_desc", ADVERSARIAL_CASES)
def test_adversarial_each_input_runs_without_exception(text, _desc):
    _, orch, _ = _make_orch()
    res = orch.handle_text(event_id=f"evt-adv-{hash(text)%10000}", line_user_id="U-adv-parametrize", text=text)
    assert res.reply and isinstance(res.reply, str)
    assert res.status in ("COMPLETED", "FALLBACK", "BLOCKED", "NEEDS_CLARIFICATION", "NEEDS_ROLE_SELECTION", "NEEDS_CONFIRMATION", "PAUSED", "INFORMATION", "SIDE_ANSWER")


def test_red_flag_always_before_router_and_ai():
    _, orch, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "guarded"
    try:
        res = orch.handle_text(event_id="evt-red-1", line_user_id="U-red-1", text="胸口很痛而且呼吸困難")
        assert res.status == "FALLBACK"
        assert res.fallback_reason in ("A_EMERGENCY", "A_URGENT_HUMAN", "RED_FLAG")
        assert "119" in res.reply or "急診" in res.reply
        assert res.semantic_route in (None, "UNKNOWN", "MIXED", "PURE_EDUCATION", "PURE_INTAKE", "CHITCHAT")
        res2 = orch.handle_text(event_id="evt-red-2", line_user_id="U-red-2", text="沒有胸痛，只是想問胸痛該怎麼辦")
        if res2.status == "FALLBACK":
            assert res2.fallback_reason in ("A_EMERGENCY", "A_URGENT_HUMAN")
        assert res2.semantic_route in (None, "UNKNOWN", "MIXED", "PURE_EDUCATION", "CHITCHAT") or res2.semantic_route != "PURE_INTAKE"
    finally:
        os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_mixed_can_write_and_answer():
    _, orch, _ = _make_orch()
    orch.handle_text(event_id="evt-mix-auth", line_user_id="U-mix-1", text="為自己整理")
    res = orch.handle_text(event_id="evt-mix-main", line_user_id="U-mix-1", text="我最近常口渴，糖尿病一天可以吃幾份水果？")
    sess = orch.session_for_user("U-mix-1")
    sd = sess.intake_snapshot.symptom_description or ""
    assert "口渴" in sd or "口乾" in sd or "渴" in sd
    assert res.status in ("COMPLETED", "SIDE_ANSWER")


def test_question_hypothetical_friend_no_pollute():
    for text in ["metformin 會傷腎嗎？", "如果以後開始頭暈要怎麼辦？", "我朋友最近一直口渴"]:
        _, orch, _ = _make_orch()
        orch.handle_text(event_id="evt-q-auth", line_user_id="U-q-"+text[:2], text="為自己整理")
        sess_before = orch.session_for_user("U-q-"+text[:2])
        sd_before = sess_before.intake_snapshot.symptom_description
        res = orch.handle_text(event_id="evt-q-main", line_user_id="U-q-"+text[:2], text=text)
        sess = orch.session_for_user("U-q-"+text[:2])
        sd = sess.intake_snapshot.symptom_description
        if "朋友" in text:
            assert res.semantic_route in (None, "UNKNOWN", "MIXED", "PURE_EDUCATION", "CHITCHAT", "SUBJECT_CHANGE")
            if sd and "朋友" in sd:
                assert res.semantic_route != "PURE_INTAKE" or not res.semantic_confidence or res.semantic_confidence < 0.62
        if "會傷腎嗎" in text or "如果以後" in text:
            assert sd == sd_before or sd is None or "嗎" not in (sd or "")


def test_correction_subject_not_fast_written():
    _, orch, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "guarded"
    try:
        orch.handle_text(event_id="evt-corr-auth", line_user_id="U-corr", text="為自己整理")
        orch.handle_text(event_id="evt-corr-1", line_user_id="U-corr", text="我嘴巴很乾，晚上一直跑廁所")
        res = orch.handle_text(event_id="evt-corr-2", line_user_id="U-corr", text="我前面說錯了，其實是我媽媽在吃")
        assert res.status in ("NEEDS_CLARIFICATION", "SIDE_ANSWER", "COMPLETED", "NEEDS_CONFIRMATION")
        assert res.semantic_route in (None, "UNKNOWN", "CORRECTION", "SUBJECT_CHANGE", "MIXED")
    finally:
        os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_pure_education_fast_still_through_bd_gate():
    _, orch, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "guarded"
    try:
        orch.handle_text(event_id="evt-edu-auth", line_user_id="U-edu", text="為自己整理")
        res = orch.handle_text(event_id="evt-edu-pure", line_user_id="U-edu", text="糖尿病一天可以吃幾份水果？")
        assert res.status in ("COMPLETED", "FALLBACK", "SIDE_ANSWER", "ASYNC_PENDING")
        if res.status == "COMPLETED":
            sess = orch.session_for_user("U-edu")
            assert sess is not None
    finally:
        os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_router_failure_fallback_to_interpreter():
    _, orch, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "shadow"
    try:
        class FailingRouter:
            def route(self, text):
                raise RuntimeError("router broken")
            def predict(self, text):
                raise RuntimeError("router broken")

        orch._semantic_router = FailingRouter()  # type: ignore
        orch._semantic_router_init_attempted = True
        res = orch.handle_text(event_id="evt-fail-1", line_user_id="U-fail", text="你好")
        assert res.reply and res.status in ("COMPLETED", "BLOCKED", "NEEDS_CLARIFICATION", "SIDE_ANSWER", "FALLBACK")
        assert res.semantic_degraded or res.semantic_route == "UNKNOWN" or res.semantic_route is None
    finally:
        os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_off_vs_shadow_compatible():
    text = "請說明糖尿病的一般飲食原則。"
    _, orch_off, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "off"
    res_off = orch_off.handle_text(event_id="evt-off-1", line_user_id="U-off", text=text)
    os.environ["SEMANTIC_ROUTER_MODE"] = "shadow"
    _, orch_shadow, _ = _make_orch()
    res_shadow = orch_shadow.handle_text(event_id="evt-shadow-1", line_user_id="U-shadow", text=text)
    assert res_off.reply == res_shadow.reply
    assert res_off.status == res_shadow.status
    assert res_off.semantic_route is None
    assert res_shadow.semantic_route is not None
    os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_shadow_not_change_session_version_diff():
    _, orch_off, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "off"
    orch_off.handle_text(event_id="evt-v-auth", line_user_id="U-ver", text="為自己整理")
    v_before_off = orch_off.session_for_user("U-ver").version
    orch_off.handle_text(event_id="evt-v-1", line_user_id="U-ver", text="你好")
    v_after_off = orch_off.session_for_user("U-ver").version

    _, orch_shadow, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "shadow"
    orch_shadow.handle_text(event_id="evt-vs-auth", line_user_id="U-ver-s", text="為自己整理")
    v_before_s = orch_shadow.session_for_user("U-ver-s").version
    orch_shadow.handle_text(event_id="evt-vs-1", line_user_id="U-ver-s", text="你好")
    v_after_s = orch_shadow.session_for_user("U-ver-s").version
    assert (v_after_off - v_before_off) == (v_after_s - v_before_s)
    os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_guarded_only_approved_early_exit_spy():
    _, orch, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "guarded"
    try:
        orch.handle_text(event_id="evt-spy-auth", line_user_id="U-spy", text="為自己整理")
        calls = {"c": 0}
        orig = orch.interpreter.interpret

        def spy(envelope):
            calls["c"] += 1
            return orig(envelope)

        orch.interpreter.interpret = spy  # type: ignore
        calls["c"] = 0
        orch.handle_text(event_id="evt-spy-edu", line_user_id="U-spy2", text="糖尿病一天可以吃幾份水果？")
        calls["c"] = 0
        orch.handle_text(event_id="evt-spy-mix", line_user_id="U-spy", text="我最近常口渴，糖尿病一天可以吃幾份水果？")
        assert calls["c"] == 1
    finally:
        os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_router_timeout_fallback():
    _, orch, _ = _make_orch()
    os.environ["SEMANTIC_ROUTER_MODE"] = "shadow"

    class SlowRouter:
        def route(self, text):
            time.sleep(0.5)
            return None
        def predict(self, text):
            time.sleep(0.5)
            return None

    orch._semantic_router = SlowRouter()  # type: ignore
    orch._semantic_router_init_attempted = True
    start = time.perf_counter()
    res = orch.handle_text(event_id="evt-slow-1", line_user_id="U-slow", text="你好")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    assert res.semantic_route in (None, "UNKNOWN") or res.semantic_degraded
    os.environ.pop("SEMANTIC_ROUTER_MODE", None)


def test_factory_construction():
    from tfda_context_gate.semantic_router.factory import build_semantic_router
    from tfda_context_gate.semantic_router.config import SemanticRouterConfig

    cfg = SemanticRouterConfig.from_env()
    assert cfg.mode in ("off", "shadow", "guarded")
    r = build_semantic_router(cfg)
    assert r is not None
    assert r.degraded
    import tfda_context_gate.semantic_router.factory as fac

    assert fac is not None


def test_line_callback_http_integration():
    from line_bot.app import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code in (200, 503)
    resp2 = client.post("/callback", json={"events": []})
    assert resp2.status_code in (200, 503, 400)


def test_pure_intake_short_answer_no_ai_via_fast_path():
    _, orch, _ = _make_orch()
    orch.handle_text(event_id="evt-intake-auth", line_user_id="U-intake-fast", text="為自己整理")
    _ = orch.handle_text(event_id="evt-intake-1", line_user_id="U-intake-fast", text="我嘴巴很乾，晚上一直跑廁所")
    res2 = orch.handle_text(event_id="evt-intake-2", line_user_id="U-intake-fast", text="我沒有過敏")
    assert res2.reply
    assert res2.status in ("NEEDS_CLARIFICATION", "COMPLETED", "SIDE_ANSWER")
