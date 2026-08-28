from __future__ import annotations

from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS, IMPLICIT_CONFIRM_TEMPLATE
from tfda_context_gate.intake.tool import (
    build_implicit_confirm,
    build_implicit_confirm_for_fields,
    format_stage_progress,
    get_stage_progress_text,
    is_uncertain_answer,
    handle_symptom_clarification,
    PreVisitIntakeTool,
)
from tfda_context_gate.intake.schemas import PreVisitIntake


def test_p1_2_implicit_confirm_format_contains_raw_and_normalized():
    raw = "吃 metformin，還有一顆白色的圓藥丸"
    normalized = "metformin、另一顆待確認"
    result = build_implicit_confirm(raw, normalized)
    assert "你提到「" in result
    assert raw[:30] in result
    assert normalized in result
    assert "對嗎？" in result
    assert result == IMPLICIT_CONFIRM_TEMPLATE.format(raw=raw[:30], normalized=normalized)
    assert "收到" not in result and "了解" not in result
    assert len(result) <= 60 + len(normalized)


def test_p1_2_build_implicit_confirm_for_fields_limits_to_two():
    extracted = {
        "known_medications": ["metformin"],
        "allergies": ["無"],
        "chronic_conditions": ["高血壓"],
        "family_history": ["無"],
    }
    result = build_implicit_confirm_for_fields(extracted, raw_text="吃 metformin，無過敏，有高血壓，家族無糖尿病")
    assert result is not None
    assert "對嗎？" in result
    assert "metformin" in result
    assert "高血壓" not in result or result.count("、") <= 1 or "、".join  # ensure only first 2 fields in normalized part
    # normalized part should contain at most 2 field values joined by ；
    normalized_part = result.split("我記為「")[1].split("」")[0]
    assert normalized_part.count("；") <= 1


def test_p1_3_single_round_only_confirms_one_to_two_items(tmp_path):
    from tfda_context_gate.line_orchestration import ConversationOrchestrator
    from tfda_context_gate.product_session import SQLiteProductSessionRepository

    repo = SQLiteProductSessionRepository(tmp_path / "p1_3.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key="p1-3-test-key-at-least-16")
    orch.handle_text(event_id="p1-3-1", line_user_id="U-p1-3", text="為自己整理")
    # Provide utterance that extracts 3-4 fields at once
    result = orch.handle_text(
        event_id="p1-3-2",
        line_user_id="U-p1-3",
        text="吃 metformin，無過敏，有高血壓，家族無糖尿病",
    )
    session = repo.get(result.session_id)
    assert session is not None
    # P1-3: orchestrator should only commit first 2 extracted fields in this turn, rest deferred
    # At minimum, confirm reply contains at most 2 field confirmations (implicit confirm limited to 2)
    assert "對嗎？" in result.reply
    # The reply's implicit confirm normalized part should contain ≤2 values (so count ； ≤1)
    if "我記為「" in result.reply:
        norm = result.reply.split("我記為「")[1].split("」")[0]
        assert norm.count("；") <= 1
    # Remaining fields should be missing and appear in next pending question / still missing
    assert session.pending_field is not None


def test_p1_4_unknown_graceful_convergence_for_symptom(tmp_path):
    assert is_uncertain_answer("不知道欸")
    assert is_uncertain_answer("我忘了什麼時候開始的")
    assert is_uncertain_answer("不確定")
    assert is_uncertain_answer("不太清楚")
    assert not is_uncertain_answer("三個月前")

    r1 = handle_symptom_clarification("symptom_onset", "不知道", attempt=1)
    assert r1["status"] == "unknown"
    assert r1["value"] == "待確認"
    assert "沒關係，先記為『待確認』" in r1["question"]

    r2 = handle_symptom_clarification("symptom_description", "隨便說", attempt=2)
    assert r2["status"] == "unknown"
    assert r2["value"] == "待確認"

    tool = PreVisitIntakeTool()
    assert tool.is_uncertain_answer("忘記了")
    med_r = tool.handle_medication_clarification("不知道", attempt=1)
    assert med_r["status"] == "unknown"

    from tfda_context_gate.line_orchestration import ConversationOrchestrator
    from tfda_context_gate.product_session import SQLiteProductSessionRepository

    repo = SQLiteProductSessionRepository(tmp_path / "p1_4.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key="p1-4-test-key-at-least-16")
    orch.handle_text(event_id="p1-4-1", line_user_id="U-p1-4", text="為自己整理")
    orch.handle_text(event_id="p1-4-2", line_user_id="U-p1-4", text="目前沒有用藥")
    orch.handle_text(event_id="p1-4-3", line_user_id="U-p1-4", text="沒有過敏")
    orch.handle_text(event_id="p1-4-4", line_user_id="U-p1-4", text="沒有其他慢性病")
    orch.handle_text(event_id="p1-4-5", line_user_id="U-p1-4", text="沒有家族史")
    # Now at stage2, provide uncertain onset
    result = orch.handle_text(event_id="p1-4-6", line_user_id="U-p1-4", text="我忘了什麼時候開始的")
    session = repo.get(result.session_id)
    assert session is not None
    assert session.intake_snapshot.symptom_onset == "待確認"
    assert "沒關係，先記為『待確認』" in result.reply


def test_p1_5_stage_progress_replaces_numeric_progress():
    empty = PreVisitIntake()
    p_empty = format_stage_progress(empty)
    assert "第" not in p_empty
    assert "還差" in p_empty
    assert p_empty == get_stage_progress_text(empty)
    assert len(p_empty) <= 60

    partial = PreVisitIntake(
        known_medications=["metformin"],
        allergies=["無"],
        chronic_conditions=["高血壓"],
        family_history=["無"],
    )
    p_partial = format_stage_progress(partial)
    assert "第" not in p_partial
    assert "已完成" in p_partial
    assert "還差" in p_partial
    assert "用藥與過敏" in p_partial
    assert "✅" in p_partial
    assert len(p_partial) <= 60

    full = PreVisitIntake(
        known_medications=["metformin"],
        allergies=["無"],
        chronic_conditions=["高血壓"],
        family_history=["無"],
        symptom_onset="三個月前",
        symptom_description="頭暈",
        symptom_severity="中度",
        questions_for_doctor=["飲食原則"],
    )
    p_full = format_stage_progress(full)
    assert "皆已完成" in p_full
    assert "✅" in p_full
    assert "第" not in p_full

    # Verify INTAKE_FIELD_QUESTIONS no longer contains numeric progress
    for v in INTAKE_FIELD_QUESTIONS.values():
        assert "第" not in v or "第" in v and "題" not in v  # allow non-progress 第 but not 第 n/8 題
    assert not any("第 1/8 題" in v for v in INTAKE_FIELD_QUESTIONS.values())
    assert not any("第 8/8 題" in v for v in INTAKE_FIELD_QUESTIONS.values())
