"""CONTRACT_v1 的 Pydantic v2 模型。欄位集合、必填與否、巢狀結構完全依照
../../../02_MS2_demo/contract/CONTRACT_v1.md ——不得在未先更新契約的情況下
新增、改名或刪除欄位。

回填（round-trip）注意事項：模型以 `model_dump(mode="json",
exclude_unset=True)` 序列化輸出。若某欄位在 payload 中真的不存在（例如
vector chunk 的 `entities`/`relations`），建構時就不能傳入該參數，讓它保持
「unset」狀態才會在輸出時被省略。若某欄位存在但值為 null（例如
`entities[].code`），則必須明確傳入（即使是 None），讓它保持「set」狀態，
回填時才會正確輸出成 `null`。
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    EntityType,
    EvidenceRiskLevel,
    GraphPathStatus,
    IntentTag,
    ObjectType,
    Polarity,
    RelationType,
    RetrievalRoute,
    RetrievalStatus,
    RetrieverType,
    RiskFlag,
    RouterStatus,
    SafetySignalType,
    SCHEMA_VERSION,
    ScoreType,
    ChunkStatus,
    SubjectType,
    TargetSubject,
    TimeFrame,
    WarningCode,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# RetrievalRequest（LLM -> RAG）。RAG 只讀取，絕不修改。
# --------------------------------------------------------------------------


class ContextModifiers(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    time_frame: TimeFrame
    target_subject: TargetSubject
    polarity: Polarity
    language: str


class GuardrailResult(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_tags: list[IntentTag]
    risk_flags: list[RiskFlag]
    context_modifiers: ContextModifiers
    router_status: RouterStatus
    reason_codes: list[str]


class RetrievalRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    schema_version: Literal["rag-v1"]
    user_raw_input: str
    retrieval_queries: list[str]
    guardrail_result: GuardrailResult
    language: str
    timestamp: str


# --------------------------------------------------------------------------
# RetrievalResponse（RAG -> LLM）
# --------------------------------------------------------------------------


class Warning(StrictModel):
    code: WarningCode
    detail: Optional[str] = None


class Entity(StrictModel):
    id: str
    type: EntityType
    label: str
    code: Optional[str] = None


class Relation(StrictModel):
    subject: str
    subject_type: SubjectType
    relation: RelationType
    object: str
    object_type: ObjectType
    condition: Optional[str] = None
    effect: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    negation_checked: Optional[bool] = None
    additional_sources: list[str] = Field(default_factory=list)


class RetrievedChunk(StrictModel):
    chunk_id: str
    source: str
    version: str
    date: str
    score: float = Field(ge=0.0, le=1.0)
    score_type: ScoreType
    status: ChunkStatus
    content: str
    retriever: RetrieverType
    entities: Optional[list[Entity]] = None
    relations: Optional[list[Relation]] = None
    evidence_risk_level: EvidenceRiskLevel
    safety_signal_types: list[SafetySignalType] = Field(default_factory=list)
    risk_basis: Optional[str] = None

    @model_validator(mode="after")
    def _check_date_format(self) -> "RetrievedChunk":
        if not _DATE_RE.match(self.date):
            raise ValueError(f"date must be YYYY-MM-DD, got {self.date!r}")
        return self

    @model_validator(mode="after")
    def _check_graph_requires_entities_relations(self) -> "RetrievedChunk":
        if self.retriever == RetrieverType.GRAPH:
            if not self.entities or not self.relations:
                raise ValueError("retriever=='graph' requires non-empty entities and relations")
        return self


class RetrievalResponse(StrictModel):
    request_id: str
    schema_version: Literal["rag-v1"] = SCHEMA_VERSION
    retrieval_route: RetrievalRoute
    retrieval_status: RetrievalStatus
    graph_path_status: GraphPathStatus
    rerun_suggested: bool
    max_evidence_risk_level: EvidenceRiskLevel
    warnings: list[Warning] = Field(default_factory=list)
    chunks: list[RetrievedChunk] = Field(default_factory=list)
