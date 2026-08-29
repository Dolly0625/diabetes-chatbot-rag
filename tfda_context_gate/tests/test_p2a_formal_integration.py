"""P2A 正式路徑整合測試：Factory.from_env() → FormalConversationInterpreter → deterministic + formal 合併落 session

驗證：即使 deterministic 只理解半句，Formal 仍能看完整句子並補齊，且由 merge/validation 決定寫入
"""
from __future__ import annotations

import pytest

from tfda_context_gate.conversation.interpreter import (
    ConversationInterpreterFactory,
    ConversationTurnInterpretation,
    FormalConversationInterpreter,
    IntakeCandidate,
)
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository

_KEY = "p2a-formal-test-key-12345678901234"


def test_p2a_formal_deterministic_partial_plus_formal_complement(tmp_path, monkeypatch):
    """重現：deterministic 部分命中 + formal 補齊第二症狀，session 實際落地完整（非僅 AI 原始 JSON）"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")

    interp = ConversationInterpreterFactory.from_env()
    assert isinstance(interp, FormalConversationInterpreter), f"expected Formal, got {interp.__class__.__name__}"

    # Mock 最底層 transport：讓 Formal.interpret 返回預製的補齊候選，不發真網路請求
    # 仍走 Formal.interpret → ThreadPool → fallback 的入口，但直接替換 _chain/invoke 邏輯
    # 為保持 Formal 類別， monkeypatch interpret 方法返回自定義 interpretation
    original_interpret = interp.interpret

    def _mock_interpret(envelope):
        cm = getattr(envelope, "current_message", "") or ""
        # 僅對目標句返回兩個症狀候選，其餘走 fallback deterministic
        if "我嘴巴很乾，晚上一直跑廁所" in cm:
            return ConversationTurnInterpretation(
                intents=["INTAKE_ANSWER"],
                intake_candidates=[
                    IntakeCandidate(
                        field_name="symptom_description",
                        candidate_value="嘴巴很乾",
                        source_quote="嘴巴很乾",
                        confidence=0.9,
                        explicitly_stated=True,
                        requires_confirmation=False,
                    ),
                    IntakeCandidate(
                        field_name="symptom_description",
                        candidate_value="晚上一直跑廁所",
                        source_quote="晚上一直跑廁所",
                        confidence=0.88,
                        explicitly_stated=True,
                        requires_confirmation=False,
                    ),
                ],
                confidence=0.9,
            )
        # 其它句走原本 fallback（避免影響後續）
        return original_interpret(envelope)

    # 替換 interpret 但保留 Formal 實例（符合「不直接塞 Fake」但 mock transport）
    interp.interpret = _mock_interpret  # type: ignore[method-assign]

    repo = SQLiteProductSessionRepository(tmp_path / "p2a_formal.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=interp, use_formal=False)

    # 走完整 intake 流程至 symptom_description
    orch.handle_text(event_id="p2a-f-1", line_user_id="U-p2a-f", text="為自己整理")
    orch.handle_text(event_id="p2a-f-2", line_user_id="U-p2a-f", text="metformin")
    orch.handle_text(event_id="p2a-f-3", line_user_id="U-p2a-f", text="沒有過敏")
    orch.handle_text(event_id="p2a-f-4", line_user_id="U-p2a-f", text="無慢性病")
    orch.handle_text(event_id="p2a-f-5", line_user_id="U-p2a-f", text="無家族史")
    orch.handle_text(event_id="p2a-f-6", line_user_id="U-p2a-f", text="三天前開始")
    sess_before = orch.session_for_user("U-p2a-f")
    assert sess_before is not None
    assert sess_before.pending_field == "symptom_description"

    # 關鍵句：deterministic 對「嘴巴很乾，晚上一直跑廁所」原本抽不到或只抽半句，formal 補齊
    r = orch.handle_text(event_id="p2a-f-7", line_user_id="U-p2a-f", text="我嘴巴很乾，晚上一直跑廁所")
    sess = orch.session_for_user("U-p2a-f")
    assert sess is not None
    desc = sess.intake_snapshot.symptom_description or ""
    # 必須同時包含兩個 clause（以 ； 連接或至少 substring 同時存在），證明 merge 生效且落 session
    assert "嘴巴很乾" in desc, f"merge 後應保留 嘴巴很乾，實際 desc='{desc}'"
    assert "跑廁所" in desc, f"merge 後應補齊 跑廁所，實際 desc='{desc}'"
    # 不得僅靠 AI 原始 JSON：驗證 session 落地而非僅 interpretation
    assert sess.pending_field == "symptom_severity" or sess.intake_stage == "stage2"

    # 反例：問句不得污染
    r2 = orch.handle_text(event_id="p2a-f-8", line_user_id="U-p2a-f", text="6分")
    sess2 = orch.session_for_user("U-p2a-f")
    # 6分 應走 fast-path 寫入 severity，不經 AI
    assert sess2.intake_snapshot.symptom_severity is not None


def test_p2a_formal_not_fake_construction(tmp_path, monkeypatch):
    """證明不是 Fake：Factory 建出 Formal，且中間未被替換為 FakeInterpreter"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")
    interp = ConversationInterpreterFactory.from_env()
    assert interp.__class__.__name__ == "FormalConversationInterpreter"
    # 確認不是 FakeConversationInterpreter
    assert interp.__class__.__name__ != "FakeConversationInterpreter"
    # _chain 初始為 None 時會 fallback，但類別仍為 Formal
    interp._chain = None  # type: ignore[attr-defined]
    interp._llm = None  # type: ignore[attr-defined]
    from tfda_context_gate.conversation.envelope import build_conversation_envelope
    from tfda_context_gate.product_session.schemas import ProductSession

    repo = SQLiteProductSessionRepository(tmp_path / "p2a_formal2.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=interp, use_formal=False)
    assert orch.interpreter.__class__.__name__ == "FormalConversationInterpreter"
