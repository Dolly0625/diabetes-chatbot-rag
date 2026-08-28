from __future__ import annotations

from tfda_medrax2_experiment.agent_lab.corpus import EXPECTED_SOURCE, TFDACorpus
from tfda_medrax2_experiment.agent_lab.gates import OutputGate
from tfda_medrax2_experiment.agent_lab.graph import TFDAToolAgent
from tfda_medrax2_experiment.agent_lab.models import LoopingModel, RuleBasedTFDAModel, ScriptedModel
from tfda_medrax2_experiment.agent_lab.schemas import AgentLimits, AssistantTurn, ToolCall
from tfda_medrax2_experiment.agent_lab.tools.tfda import build_default_registry


def sample_rows():
    return [
        {
            "id": "tfda-risk-0019",
            "page_content": (
                "藥品成分：SGLT2抑制劑類\n"
                "訊息緣由：TFDA 彙整 SGLT2 抑制劑類之藥品安全風險溝通資訊。"
            ),
            "metadata": {
                "document_id": "tfda-risk-0019",
                "source_dataset": EXPECTED_SOURCE,
                "發布日期": "2015/05/22",
                "藥品成分": "SGLT2抑制劑類",
            },
        }
    ]


def make_agent(model=None, limits=None):
    corpus = TFDACorpus(rows=sample_rows())
    return TFDAToolAgent(
        model=model or RuleBasedTFDAModel(),
        registry=build_default_registry(corpus),
        limits=limits,
    )


def test_end_to_end_tool_loop_passes_mandatory_gates():
    agent = make_agent()
    result = agent.run("SGLT2 抑制劑有哪些 TFDA 藥品安全資訊？", thread_id="thread-a")

    assert result.status == "COMPLETED"
    assert result.termination_reason == "OUTPUT_PASS"
    assert result.approved_evidence_ids == ["tfda-risk-0019"]
    assert "[tfda-risk-0019]" in result.final_response
    assert [item.tool_name for item in result.tool_results] == [
        "search_tfda_risk_communications",
        "lookup_tfda_ingredient_risks",
        "inspect_tfda_evidence_set",
    ]
    assert result.agent_steps == 3
    assert [item.stage for item in result.trace][-2:] == ["B", "D"]


def test_independent_retrieval_tools_share_one_batch():
    result = make_agent().run("SGLT2 抑制劑風險？")
    tool_batches = [item for item in result.trace if item.event == "tool_batch"]
    assert len(tool_batches) == 2
    assert len(tool_batches[0].data["results"]) == 2


def test_personalized_dose_request_is_blocked_before_tools():
    result = make_agent().run("我現在應該一天吃幾顆糖尿病藥？")
    assert result.status == "BLOCKED"
    assert result.termination_reason == "PERSONALIZED_MEDICATION_ADVICE_BLOCKED"
    assert result.tool_results == []
    assert [item.stage for item in result.trace] == ["A"]


def test_loop_is_stopped_by_application_step_limit():
    agent = make_agent(
        model=LoopingModel(),
        limits=AgentLimits(max_agent_steps=2, max_total_tool_calls=6, deadline_seconds=10),
    )
    result = agent.run("SGLT2 抑制劑的一般安全資訊")
    assert result.status == "FALLBACK"
    assert result.termination_reason == "MAX_AGENT_STEPS_EXCEEDED"
    assert result.agent_steps == 2


def test_unknown_model_requested_tool_is_blocked():
    model = ScriptedModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(call_id="bad-call", name="delete_patient_record", arguments={})]
            ),
            AssistantTurn(content="沒有證據的回答"),
        ]
    )
    result = make_agent(model=model).run("SGLT2 一般資訊")
    assert result.status == "FALLBACK"
    assert result.tool_results[0].status == "BLOCKED"
    assert result.tool_results[0].error_code == "TOOL_NOT_ALLOWED"
    assert result.termination_reason == "EVIDENCE_INSUFFICIENT"


def test_cache_is_reused_across_runs_but_evidence_is_reapproved():
    agent = make_agent()
    first = agent.run("SGLT2 抑制劑有哪些 TFDA 藥品安全資訊？", thread_id="same-thread")
    second = agent.run("SGLT2 抑制劑有哪些 TFDA 藥品安全資訊？", thread_id="same-thread")
    assert first.status == second.status == "COMPLETED"
    assert all(item.cache_hit for item in second.tool_results)
    assert any(item.stage == "B" for item in second.trace)


def test_thread_checkpoint_keeps_conversation_history_isolated():
    agent = make_agent()
    agent.run("SGLT2 抑制劑風險？", thread_id="thread-one")
    first_state = agent.workflow.get_state({"configurable": {"thread_id": "thread-one"}})
    first_count = len(first_state.values["messages"])

    agent.run("SGLT2 抑制劑安全資訊？", thread_id="thread-one")
    second_state = agent.workflow.get_state({"configurable": {"thread_id": "thread-one"}})
    other = make_agent()
    other.run("SGLT2 抑制劑風險？", thread_id="thread-two")
    other_state = other.workflow.get_state({"configurable": {"thread_id": "thread-two"}})

    assert len(second_state.values["messages"]) > first_count
    assert len(other_state.values["messages"]) == first_count


def test_output_gate_rejects_unapproved_citation_and_directive():
    decision = OutputGate().evaluate(
        "你應該停藥。[tfda-risk-9999] 這不是個別診斷、處方或停換藥建議。",
        ["tfda-risk-0019"],
    )
    assert decision.decision == "BLOCK"
    assert any(code.startswith("UNAPPROVED_CITATIONS") for code in decision.reason_codes)
    assert "PERSONALIZED_DIRECTIVE" in decision.reason_codes


def test_output_gate_requires_citation_and_scope_notice():
    decision = OutputGate().evaluate("只有一般敘述。", ["tfda-risk-0019"])
    assert decision.decision == "BLOCK"
    assert "NO_TFDA_CITATION" in decision.reason_codes
    assert "MISSING_SCOPE_NOTICE" in decision.reason_codes


def test_real_corpus_sglt2_run_stays_on_the_requested_drug_class():
    corpus = TFDACorpus()
    agent = TFDAToolAgent(
        model=RuleBasedTFDAModel(),
        registry=build_default_registry(corpus),
    )
    result = agent.run("SGLT2 抑制劑有哪些 TFDA 藥品安全資訊？")

    assert result.status == "COMPLETED"
    assert result.approved_evidence_ids
    for evidence_id in result.approved_evidence_ids:
        row = corpus.get(evidence_id)
        searchable = "%s\n%s" % (row["metadata"].get("藥品成分", ""), row["page_content"])
        assert "sglt2" in searchable.lower()
