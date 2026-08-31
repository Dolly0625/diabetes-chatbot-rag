from line_bot.intake_entry import (
    build_entry_enriched_reply,
    build_intake_entry_message,
    build_resume_choice_actions,
    get_resume_actions_for_result,
    is_entry_trigger,
    should_show_resume_controls,
)
from line_bot import app as line_app
from line_bot.ui import RESUME_CHOICE_ACTIONS


class DummyResult:
    def __init__(self, metadata=None, status="NEEDS_CLARIFICATION", reply="x"):
        self.metadata = metadata
        self.status = status
        self.reply = reply


def test_entry_card_explains_dedicated_flow_not_silent_reuse():
    msg = build_intake_entry_message()
    assert "專用流程" in msg
    assert "不會悄悄沿用舊資料" in msg
    assert "看診前整理" in msg

    ui_msg = line_app._maybe_enrich_entry_reply("請問你是為自己還是代家人整理？", "我要準備看診")
    assert "專用流程" in ui_msg
    assert "不會悄悄沿用舊資料" in ui_msg

    # also via ui.py
    from line_bot.ui import build_intake_entry_message as ui_build

    assert ui_build() == msg

    # is_entry_trigger precise
    assert is_entry_trigger("我要準備看診") is True
    assert is_entry_trigger(" 我要準備看診 ") is True
    assert is_entry_trigger("我要準備看診，") is False
    assert is_entry_trigger("準備看診") is False
    assert is_entry_trigger("我要準備看診 ") is True


def test_three_buttons_label_action_exact():
    actions = build_resume_choice_actions()
    assert len(actions) == 3
    assert [a["label"] for a in actions] == ["繼續上次整理", "開始新的整理", "取消整理"]
    assert [a["text"] for a in actions] == ["繼續上次整理", "開始新的整理", "取消整理"]
    # ensure each is MessageAction-compatible (label/text string)
    for a in actions:
        assert isinstance(a["label"], str) and isinstance(a["text"], str)
        assert a["label"] == a["text"]
        assert 1 <= len(a["label"]) <= 20

    # ui.py mirrors contract
    assert RESUME_CHOICE_ACTIONS == actions

    # adapter via metadata
    meta_ok = {"requires_resume_decision": True, "has_existing_draft": True}
    result_ok = DummyResult(metadata=meta_ok)
    got = get_resume_actions_for_result(result_ok)
    assert got is not None and len(got) == 3
    assert [x["text"] for x in got] == ["繼續上次整理", "開始新的整理", "取消整理"]

    # also via dict result (synthetic)
    assert get_resume_actions_for_result({"metadata": meta_ok}) is not None
    assert get_resume_actions_for_result({"requires_resume_decision": True, "has_existing_draft": True}) is not None

    # _resolve path in app.py
    assert line_app._resolve_resume_quick_actions(result_ok) is not None


def test_general_education_no_misdisplay():
    # metadata missing -> no resume
    assert should_show_resume_controls(None) is False
    assert should_show_resume_controls(DummyResult(metadata=None)) is False
    assert should_show_resume_controls(DummyResult(metadata={})) is False
    assert should_show_resume_controls({"metadata": {}}) is False
    assert get_resume_actions_for_result(DummyResult(metadata=None)) is None

    # requires true but has false / missing -> no resume (never guess)
    assert should_show_resume_controls(DummyResult(metadata={"requires_resume_decision": True})) is False
    assert should_show_resume_controls(DummyResult(metadata={"requires_resume_decision": True, "has_existing_draft": False})) is False
    assert should_show_resume_controls(DummyResult(metadata={"requires_resume_decision": False, "has_existing_draft": True})) is False
    assert should_show_resume_controls(DummyResult(metadata={"requires_resume_decision": "true", "has_existing_draft": True})) is False

    # education path: status COMPLETED, no metadata -> quick_actions stays None, not resume
    qa = line_app._quick_actions_for_status("COMPLETED", "糖尿病的一般飲食原則…")
    assert qa is None
    # even Chinese substring must not trigger resume
    fake_reply = "已有草稿，是否繼續？請選繼續上次整理"
    assert should_show_resume_controls(DummyResult(metadata=None, reply=fake_reply)) is False
    assert line_app._resolve_resume_quick_actions(DummyResult(metadata=None, reply=fake_reply)) is None

    # entry enrichment does not add resume controls
    enriched = build_entry_enriched_reply("請問你是為自己還是代家人整理？", is_entry=True)
    assert "專用流程" in enriched


def test_cancel_does_not_send_wrong_command():
    actions = build_resume_choice_actions()
    cancel = [a for a in actions if a["label"] == "取消整理"]
    assert len(cancel) == 1
    assert cancel[0]["text"] == "取消整理"
    assert cancel[0]["text"] != "不填了"
    assert cancel[0]["text"] != "取消"
    # ensure no old strings leak into resume actions
    all_texts = {a["text"] for a in actions}
    assert "不填了" not in all_texts
    assert "繼續整理" not in all_texts
    assert all_texts == {"繼續上次整理", "開始新的整理", "取消整理"}

    # via app resolver
    meta_ok = {"requires_resume_decision": True, "has_existing_draft": True}
    resume = line_app._resolve_resume_quick_actions(DummyResult(metadata=meta_ok))
    assert resume is not None
    assert any(a["text"] == "取消整理" for a in resume)
    assert not any(a["text"] == "不填了" for a in resume)


def test_adapter_strict_boolean_not_string_truthy():
    # string "True" or int 1 must not trigger
    assert should_show_resume_controls({"requires_resume_decision": "True", "has_existing_draft": True}) is False
    assert should_show_resume_controls({"requires_resume_decision": 1, "has_existing_draft": 1}) is False
    assert should_show_resume_controls({"requires_resume_decision": True, "has_existing_draft": "True"}) is False
    # only strict True passes
    assert should_show_resume_controls({"requires_resume_decision": True, "has_existing_draft": True}) is True
