from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field

from ..corpus import TFDACorpus
from .base import ExperimentTool, ToolExecutionPayload, ToolRegistry


class SearchRiskInput(BaseModel):
    query: str = Field(min_length=2, description="TFDA drug-safety search query")
    top_k: int = Field(default=5, ge=1, le=8)


class SearchRiskCommunicationsTool(ExperimentTool):
    name = "search_tfda_risk_communications"
    description = (
        "Search the approved local TFDA drug risk-communication corpus. "
        "Returns ranked candidate evidence, never a final medical answer."
    )
    input_model = SearchRiskInput
    max_calls_per_run = 2

    def __init__(self, corpus: TFDACorpus):
        self.corpus = corpus

    def execute(self, value: SearchRiskInput) -> ToolExecutionPayload:
        evidence = self.corpus.search(value.query, value.top_k)
        return ToolExecutionPayload(
            payload={
                "query": value.query,
                "count": len(evidence),
                "evidence_ids": [item.evidence_id for item in evidence],
            },
            candidate_evidence=evidence,
        )


class IngredientLookupInput(BaseModel):
    ingredient: str = Field(min_length=2, description="Drug ingredient or class, such as SGLT2")
    top_k: int = Field(default=5, ge=1, le=8)


class IngredientRiskLookupTool(ExperimentTool):
    name = "lookup_tfda_ingredient_risks"
    description = (
        "Look up TFDA risk communications whose ingredient metadata matches a drug ingredient "
        "or diabetes-drug class. Returns candidate evidence only."
    )
    input_model = IngredientLookupInput
    max_calls_per_run = 2

    def __init__(self, corpus: TFDACorpus):
        self.corpus = corpus

    def execute(self, value: IngredientLookupInput) -> ToolExecutionPayload:
        evidence = self.corpus.search(value.ingredient, value.top_k, ingredient_only=True)
        return ToolExecutionPayload(
            payload={
                "ingredient": value.ingredient,
                "count": len(evidence),
                "evidence_ids": [item.evidence_id for item in evidence],
            },
            candidate_evidence=evidence,
        )


class InspectEvidenceInput(BaseModel):
    evidence_ids: List[str] = Field(min_length=1, max_length=8)


class InspectEvidenceSetTool(ExperimentTool):
    name = "inspect_tfda_evidence_set"
    description = (
        "Inspect selected TFDA candidate evidence IDs and report provenance, ingredients, dates, "
        "and a short non-generative excerpt. Does not approve evidence."
    )
    input_model = InspectEvidenceInput
    max_calls_per_run = 1

    def __init__(self, corpus: TFDACorpus):
        self.corpus = corpus

    def execute(self, value: InspectEvidenceInput) -> ToolExecutionPayload:
        evidence = self.corpus.evidence(value.evidence_ids)
        summaries = []
        for item in evidence:
            compact = re.sub(r"\s+", " ", item.content).strip()[:280]
            summaries.append(
                {
                    "evidence_id": item.evidence_id,
                    "ingredient": item.ingredient,
                    "published_date": item.published_date,
                    "source": item.source,
                    "excerpt": compact,
                }
            )
        missing = sorted(set(value.evidence_ids) - {item.evidence_id for item in evidence})
        return ToolExecutionPayload(
            payload={"summaries": summaries, "missing_ids": missing},
            candidate_evidence=evidence,
        )


DEFAULT_TOOL_NAMES = [
    "search_tfda_risk_communications",
    "lookup_tfda_ingredient_risks",
    "inspect_tfda_evidence_set",
]


def build_default_registry(
    corpus: TFDACorpus,
    selected_names: Optional[List[str]] = None,
) -> ToolRegistry:
    available = [
        SearchRiskCommunicationsTool(corpus),
        IngredientRiskLookupTool(corpus),
        InspectEvidenceSetTool(corpus),
    ]
    return ToolRegistry.select(available, selected_names or DEFAULT_TOOL_NAMES)
