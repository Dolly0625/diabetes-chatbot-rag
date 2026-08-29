"""回歸：stage1 裸藥名不再寫入（P1.1.1 4026e45 回歸）"""

from pathlib import Path
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter

_KEY = "regression-test-key-12345678901234"

def _new_orch(tmp_path: Path):
    repo = SQLiteProductSessionRepository(tmp_path / f"{tmp_path.name}.sqlite3")
    return ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=DeterministicConversationInterpreter())

def test_bare_metformin_after_role_selection(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="r1", line_user_id="U-reg", text="為自己整理")
    r = orch.handle_text(event_id="r2", line_user_id="U-reg", text="metformin")
    sess = orch.session_for_user("U-reg")
    assert sess.intake_snapshot.known_medications == ["metformin"], f"bare metformin should write, got {sess.intake_snapshot.known_medications}, reply={r.reply}"

def test_bare_erjia_variants(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="r1", line_user_id="U-reg2", text="為自己整理")
    r = orch.handle_text(event_id="r2", line_user_id="U-reg2", text="醫生給我開了二甲雙胍")
    sess = orch.session_for_user("U-reg2")
    assert sess.intake_snapshot.known_medications == ["二甲雙胍"], f"variant should write, got {sess.intake_snapshot.known_medications}"

def test_all_stage1_bare_fields(tmp_path: Path):
    # 逐欄驗證其他 7 欄位的單句裸值寫入
    cases = [
        ("metformin", "known_medications", ["metformin"]),
        ("沒有過敏", "allergies", ["無"]),
        ("高血壓", "chronic_conditions", ["高血壓"]),
        ("沒有家族史", "family_history", ["無"]),
        ("三天前開始", "symptom_onset", "三天前開始"),
        ("嘴巴很乾", "symptom_description", "嘴巴很乾"),
        ("中度", "symptom_severity", "中度"),
    ]
    # We need to walk through the intake sequentially, not in isolation, because pending advances
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="s0", line_user_id="U-all", text="為自己整理")
    for idx, (txt, field, expected) in enumerate(cases):
        r = orch.handle_text(event_id=f"s{idx+1}", line_user_id="U-all", text=txt)
        sess = orch.session_for_user("U-all")
        actual = getattr(sess.intake_snapshot, field)
        if isinstance(expected, list):
            assert actual == expected, f"field {field} bare {txt!r} expected {expected}, got {actual}, reply={r.reply[:100]}"
        else:
            assert actual == expected, f"field {field} bare {txt!r} expected {expected}, got {actual}"
