"""正式路徑整合測試：Factory.from_env() → FormalConversationInterpreter → intake 寫入"""
from __future__ import annotations

import hashlib
from pathlib import Path

from tfda_context_gate.conversation.interpreter import (
    ConversationInterpreterFactory,
    FormalConversationInterpreter,
)
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository

_KEY = "p1-1-test-key-12345678901234"


def test_formal_for_intake_bare_metformin(tmp_path: Path, monkeypatch):
    # 1. 讓 Factory 走正式路徑：PYTEST hermetic 會強制 deterministic，需先移除
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # CONVERSATION 優先，設一個 fake tiny 模型名（不發真請求，僅驗證 Factory 選擇）
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")
    # 確保即使無真實 key 也能 init（Formal mimo 分支不強制 base_url/api_key）
    # 若需要避免實際網路呼叫，mock _init_llm 不影響類別判斷（可選）
    # 此處不 mock ChatOpenAI init（本地建物件不聯網），但為避免 interpret 真聯網，後續將 _chain 設為 None 使其走 fallback 路徑
    interp = ConversationInterpreterFactory.from_env()
    assert isinstance(interp, FormalConversationInterpreter), f"expected Formal, got {interp.__class__.__name__}"

    # 2. 替換 _chain 使 interpret 不發真請求但仍走 Formal.interpret → fallback deterministic
    # 保留 Formal 類別與 interpret 入口，符合「真正走 Formal 類別的 interpret 路徑」
    # 若 _chain 保留，interpret 會在 ThreadPool 中嘗試 invoke → 8s timeout 拖慢測試；設為 None 立即 fallback
    interp._chain = None  # type: ignore[attr-defined]
    interp._llm = None  # type: ignore[attr-defined]

    # 3. 用該 Formal 建立 orchestrator（hermetic，但走 Formal 類別）
    repo = SQLiteProductSessionRepository(tmp_path / "formal_integration.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=interp, use_formal=False)

    # 4. 為自己整理 → metformin
    r1 = orch.handle_text(event_id="formal-1", line_user_id="U-formal", text="為自己整理")
    # r1 應進入 intake，第一個 pending 為 known_medications
    sess1 = orch.session_for_user("U-formal")
    assert sess1 is not None
    assert sess1.pending_field == "known_medications"

    r2 = orch.handle_text(event_id="formal-2", line_user_id="U-formal", text="metformin")
    # 不得走 ASYNC_PENDING（intake 中 async narrow 應被禁）
    assert r2.status != "ASYNC_PENDING", f"bare metformin in intake should not be async, got {r2.status}"

    sess2 = orch.session_for_user("U-formal")
    assert sess2 is not None
    # meds 正確寫入：標準化為小寫 metformin（Deterministic fallback 會抽 metformin）
    assert sess2.intake_snapshot.known_medications == ["metformin"], f"meds={sess2.intake_snapshot.known_medications}"
    # pending 正確前進至 allergies
    assert sess2.pending_field == "allergies", f"pending={sess2.pending_field}"
