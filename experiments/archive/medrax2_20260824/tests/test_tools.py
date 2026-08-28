from __future__ import annotations

from pydantic import BaseModel

from tfda_medrax2_experiment.agent_lab.corpus import EXPECTED_SOURCE, TFDACorpus
from tfda_medrax2_experiment.agent_lab.schemas import ToolCall
from tfda_medrax2_experiment.agent_lab.tools.base import ExperimentTool, ToolExecutionPayload
from tfda_medrax2_experiment.agent_lab.tools.tfda import (
    IngredientRiskLookupTool,
    SearchRiskCommunicationsTool,
    build_default_registry,
)


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
        },
        {
            "id": "tfda-risk-0026",
            "page_content": "藥品成分：DPP-4抑制劑類\n訊息緣由：TFDA 發布相關安全資訊。",
            "metadata": {
                "document_id": "tfda-risk-0026",
                "source_dataset": EXPECTED_SOURCE,
                "發布日期": "2015/08/28",
                "藥品成分": "DPP-4抑制劑類",
            },
        },
    ]


def test_keyword_search_returns_candidate_evidence():
    tool = SearchRiskCommunicationsTool(TFDACorpus(rows=sample_rows()))
    result = tool.invoke(
        ToolCall(
            call_id="call-1",
            name=tool.name,
            arguments={"query": "SGLT2 糖尿病用藥風險", "top_k": 3},
        )
    )
    assert result.status == "OK"
    assert result.candidate_evidence[0].evidence_id == "tfda-risk-0019"


def test_latin_drug_class_anchor_filters_shared_generic_terms():
    rows = sample_rows() + [
        {
            "id": "tfda-risk-0083",
            "page_content": "藥品成分：CDK 4/6抑制劑類藥品\nTFDA 發布抑制劑安全風險資訊。",
            "metadata": {
                "document_id": "tfda-risk-0083",
                "source_dataset": EXPECTED_SOURCE,
                "藥品成分": "CDK 4/6抑制劑類藥品",
            },
        }
    ]
    tool = SearchRiskCommunicationsTool(TFDACorpus(rows=rows))
    result = tool.invoke(
        ToolCall(
            call_id="call-anchor",
            name=tool.name,
            arguments={"query": "SGLT2 抑制劑 TFDA 風險", "top_k": 8},
        )
    )
    assert [item.evidence_id for item in result.candidate_evidence] == ["tfda-risk-0019"]


def test_english_question_words_are_not_treated_as_drug_anchors():
    tool = SearchRiskCommunicationsTool(TFDACorpus(rows=sample_rows()))
    result = tool.invoke(
        ToolCall(
            call_id="call-english",
            name=tool.name,
            arguments={"query": "What are the TFDA risks for SGLT2 drugs?", "top_k": 3},
        )
    )
    assert [item.evidence_id for item in result.candidate_evidence] == ["tfda-risk-0019"]


def test_ingredient_lookup_is_metadata_scoped():
    tool = IngredientRiskLookupTool(TFDACorpus(rows=sample_rows()))
    result = tool.invoke(
        ToolCall(
            call_id="call-2",
            name=tool.name,
            arguments={"ingredient": "DPP-4", "top_k": 3},
        )
    )
    assert [item.evidence_id for item in result.candidate_evidence] == ["tfda-risk-0026"]


def test_invalid_arguments_return_structured_error():
    tool = SearchRiskCommunicationsTool(TFDACorpus(rows=sample_rows()))
    result = tool.invoke(
        ToolCall(call_id="call-3", name=tool.name, arguments={"query": "x", "top_k": 99})
    )
    assert result.status == "ERROR"
    assert result.error_code == "INVALID_ARGUMENTS"


def test_registry_fails_fast_on_unknown_selected_tool():
    corpus = TFDACorpus(rows=sample_rows())
    try:
        build_default_registry(corpus, ["not_a_tool"])
    except ValueError as exc:
        assert "unknown tools" in str(exc)
    else:
        raise AssertionError("unknown tool should fail fast")


class _NoInput(BaseModel):
    pass


class _FailingTool(ExperimentTool):
    name = "failing_tool"
    description = "test-only failing tool"
    input_model = _NoInput

    def execute(self, value: BaseModel) -> ToolExecutionPayload:
        raise ConnectionError("sensitive dependency detail")


def test_tool_dependency_exception_is_normalized_without_message_leak():
    result = _FailingTool().invoke(
        ToolCall(call_id="call-failure", name="failing_tool", arguments={})
    )
    assert result.status == "ERROR"
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert result.payload == {"error_type": "ConnectionError"}
