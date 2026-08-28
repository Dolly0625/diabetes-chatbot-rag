from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Type

from pydantic import BaseModel, Field, ValidationError

from ..schemas import CandidateEvidence, ToolCall, ToolResult


class ToolExecutionPayload(BaseModel):
    payload: Dict[str, Any]
    candidate_evidence: List[CandidateEvidence] = Field(default_factory=list)


class ExperimentTool(ABC):
    name: str
    description: str
    input_model: Type[BaseModel]
    max_calls_per_run: int = 2
    risk_level: str = "READ_ONLY"

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            validated = self.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult(
                call_id=call.call_id,
                tool_name=self.name,
                status="ERROR",
                payload={"validation_errors": exc.errors(include_url=False)},
                error_code="INVALID_ARGUMENTS",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        try:
            value = self.execute(validated)
            return ToolResult(
                call_id=call.call_id,
                tool_name=self.name,
                status="OK",
                payload=value.payload,
                candidate_evidence=value.candidate_evidence,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # tool boundary: normalize every dependency failure
            return ToolResult(
                call_id=call.call_id,
                tool_name=self.name,
                status="ERROR",
                payload={"error_type": type(exc).__name__},
                error_code="TOOL_EXECUTION_FAILED",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    @abstractmethod
    def execute(self, value: BaseModel) -> ToolExecutionPayload:
        raise NotImplementedError

    def llm_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }

    @staticmethod
    def cache_key(call: ToolCall) -> str:
        normalized = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return "%s:%s" % (call.name, digest)


class ToolRegistry:
    def __init__(self, tools: Iterable[ExperimentTool]):
        self._tools = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError("duplicate tool name: %s" % tool.name)
            self._tools[tool.name] = tool

    @classmethod
    def select(cls, available: Iterable[ExperimentTool], selected_names: Iterable[str]) -> "ToolRegistry":
        available_by_name = {tool.name: tool for tool in available}
        selected = list(selected_names)
        unknown = sorted(set(selected) - set(available_by_name))
        if unknown:
            raise ValueError("unknown tools: %s" % ", ".join(unknown))
        return cls(available_by_name[name] for name in selected)

    def get(self, name: str) -> Optional[ExperimentTool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def llm_schemas(self) -> List[Dict[str, Any]]:
        return [self._tools[name].llm_schema() for name in sorted(self._tools)]
