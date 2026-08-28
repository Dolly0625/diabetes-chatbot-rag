from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


QUERY_EXPANSION_SCHEMA_VERSION = "query_expansion.v0.1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryExpansionInput(StrictModel):
    request_id: str = Field(min_length=1)
    original_query: str = Field(min_length=1)
    router_status: str = Field(min_length=1)
    intent_tags: list[str] = Field(default_factory=list)
    declared_role: str | None = None
    language: str | None = None


class QueryExpansionResult(StrictModel):
    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=QUERY_EXPANSION_SCHEMA_VERSION, min_length=1)
    original_query: str = Field(min_length=1)
    retrieval_queries: list[str] = Field(min_length=1)
    strategy: str = Field(default="identity", min_length=1)

