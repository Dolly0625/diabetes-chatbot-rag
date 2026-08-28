from __future__ import annotations

import pytest

from tfda_context_gate.conversation import (
    CompactionPolicy,
    ConversationContextManager,
)


def _conversation(manager: ConversationContextManager, exchanges: int):
    context = manager.create("session-001", original_query="我要準備回診")
    for index in range(exchanges):
        context = manager.append_turn(
            context,
            role="user",
            content=f"病患訊息 {index}",
            turn_id=f"u-{index}",
        )
        context = manager.append_turn(
            context,
            role="assistant",
            content=f"系統回覆 {index}",
            turn_id=f"a-{index}",
        )
    return context


def test_recent_window_compacts_old_raw_turns_and_keeps_four_exchanges():
    manager = ConversationContextManager(
        CompactionPolicy(max_context_tokens=8_192, compact_at_ratio=1.0, recent_exchanges=4)
    )
    context = _conversation(manager, 6)

    compacted, decision = manager.compact(context)

    assert decision.should_compact is True
    assert decision.reasons == ["RECENT_WINDOW_EXCEEDED"]
    assert [turn.turn_id for turn in compacted.recent_turns] == [
        "u-2", "a-2", "u-3", "a-3", "u-4", "a-4", "u-5", "a-5"
    ]
    assert compacted.compacted_turn_count == 4
    assert len(compacted.compacted_turn_hashes) == 4
    assert all(len(value) == 64 for value in compacted.compacted_turn_hashes)


def test_token_threshold_triggers_even_with_short_conversation():
    manager = ConversationContextManager(
        CompactionPolicy(max_context_tokens=1_000, compact_at_ratio=0.60, recent_exchanges=4)
    )
    context = _conversation(manager, 1)

    decision = manager.evaluate(context, prompt_tokens=600)

    assert decision.should_compact is True
    assert decision.token_threshold == 600
    assert decision.reasons == ["TOKEN_THRESHOLD_REACHED"]


def test_token_threshold_releases_old_exchange_even_before_window_limit():
    manager = ConversationContextManager(
        CompactionPolicy(max_context_tokens=1_000, compact_at_ratio=0.60, recent_exchanges=4)
    )
    context = _conversation(manager, 3)

    compacted, decision = manager.compact(context, prompt_tokens=600)

    assert decision.reasons == ["TOKEN_THRESHOLD_REACHED"]
    assert [turn.turn_id for turn in compacted.recent_turns] == [
        "u-1", "a-1", "u-2", "a-2"
    ]
    assert compacted.compacted_turn_count == 2


def test_completed_stage_forces_snapshot_without_dropping_recent_window():
    manager = ConversationContextManager()
    context = _conversation(manager, 2)
    context = manager.mark_stage_completed(context, "stage1", next_stage="stage2")

    compacted, decision = manager.compact(context, stage_completed=True)

    assert decision.reasons == ["STAGE_COMPLETED"]
    assert compacted.compaction_count == 1
    assert compacted.compacted_turn_count == 0
    assert compacted.clinical_state.completed_stages == ["stage1"]
    assert compacted.clinical_state.current_stage == "stage2"
    assert len(compacted.recent_turns) == 4


def test_clinical_facts_survive_raw_conversation_compaction():
    manager = ConversationContextManager(
        CompactionPolicy(max_context_tokens=8_192, compact_at_ratio=1.0, recent_exchanges=1)
    )
    context = _conversation(manager, 3)
    context = manager.apply_structured_updates(
        context,
        {
            "known_medications": ["metformin"],
            "allergies": ["無"],
            "symptom_onset": "三週前",
            "symptom_description": "早晨血糖偏高",
            "reported_severity": "4/10",
            "risk_flags": ["POSSIBLE_EMERGENCY"],
            "negated_red_flags": ["NO_CHEST_PAIN"],
            "questions_for_doctor": ["是否需要調整飲食？"],
            "authorization_status": "PATIENT_SELF",
        },
        source_turn_id="u-2",
    )

    compacted, _ = manager.compact(context)

    state = compacted.clinical_state
    assert state.known_medications == ["metformin"]
    assert state.allergies == ["無"]
    assert state.symptom_onset == "三週前"
    assert state.symptom_description == "早晨血糖偏高"
    assert state.reported_severity == "4/10"
    assert state.risk_flags == ["POSSIBLE_EMERGENCY"]
    assert state.negated_red_flags == ["NO_CHEST_PAIN"]
    assert state.questions_for_doctor == ["是否需要調整飲食？"]
    assert state.authorization_status == "PATIENT_SELF"


def test_safety_signals_are_monotonic_and_cannot_be_removed_by_later_update():
    manager = ConversationContextManager()
    context = manager.create("session-002")
    context = manager.apply_structured_updates(
        context,
        {"risk_flags": ["POSSIBLE_EMERGENCY"]},
    )
    context = manager.apply_structured_updates(context, {"risk_flags": []})
    context = manager.apply_structured_updates(
        context,
        {"risk_flags": ["HIGH_RISK_NOT_EXCLUDED", "POSSIBLE_EMERGENCY"]},
    )

    assert context.clinical_state.risk_flags == [
        "POSSIBLE_EMERGENCY",
        "HIGH_RISK_NOT_EXCLUDED",
    ]


def test_user_correction_replaces_editable_fact_and_records_hash_only_revision():
    manager = ConversationContextManager()
    context = manager.create("session-003")
    context = manager.apply_structured_updates(
        context,
        {"known_medications": ["白色藥丸（待確認）"]},
        source_turn_id="u-1",
    )
    context = manager.apply_structured_updates(
        context,
        {"known_medications": ["metformin"]},
        source_turn_id="u-2",
    )

    revisions = context.clinical_state.fact_revisions
    assert context.clinical_state.known_medications == ["metformin"]
    assert len(revisions) == 2
    assert revisions[-1].source_turn_id == "u-2"
    assert len(revisions[-1].previous_value_hash or "") == 64
    assert len(revisions[-1].new_value_hash) == 64
    assert "白色藥丸" not in revisions[-1].model_dump_json()


def test_original_query_is_immutable_and_unknown_updates_are_rejected():
    manager = ConversationContextManager()
    context = manager.create("session-004", original_query="原始問題")

    with pytest.raises(ValueError, match="original_query is immutable"):
        manager.apply_structured_updates(context, {"original_query": "被改寫的問題"})
    with pytest.raises(ValueError, match="unsupported clinical state fields"):
        manager.apply_structured_updates(context, {"diagnosis": "糖尿病"})


def test_model_context_excludes_old_turns_revision_hashes_and_compaction_metadata():
    manager = ConversationContextManager(
        CompactionPolicy(max_context_tokens=8_192, compact_at_ratio=1.0, recent_exchanges=1)
    )
    context = _conversation(manager, 3)
    context = manager.apply_structured_updates(
        context,
        {"allergies": ["無"]},
        source_turn_id="u-2",
    )
    context, _ = manager.compact(context)

    payload = manager.build_model_context(context)
    rendered = str(payload)

    assert payload["clinical_state"]["allergies"] == ["無"]
    assert len(payload["recent_conversation"]) == 2
    assert "病患訊息 0" not in rendered
    assert "fact_revisions" not in rendered
    assert "compacted_turn_hashes" not in rendered
