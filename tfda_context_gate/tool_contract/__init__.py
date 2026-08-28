"""v0.2 Tool Contract — public API.

Proposal p5.4 / stage 1: ToolRequest/ToolResult, data-source-neutral
EvidenceRetrievalTool, registry/executor with timeout/trace.

All evidence goes through B gate; tool never bypasses B/C/D.
Deterministic workflow is preserved; CALL_TOOL is v0.3.
"""

from .executor import ToolExecutor
from .registry import (
    ALLOWED_TOOL_NAMES,
    EvidenceRetrievalTool,
    ToolRegistry,
    create_default_registry,
)
from .schemas import (
    ALLOWED_SOURCE_IDS,
    TOOL_CONTRACT_VERSION,
    CanonicalObservation,
    SourceId,
    TaskType,
    ToolError,
    ToolRequest,
    ToolRequestParams,
    ToolResult,
    ToolStatus,
    tool_result_to_b_input,
    tool_result_to_rag_result,
)

__all__ = [
    "ALLOWED_SOURCE_IDS",
    "ALLOWED_TOOL_NAMES",
    "TOOL_CONTRACT_VERSION",
    "CanonicalObservation",
    "EvidenceRetrievalTool",
    "SourceId",
    "TaskType",
    "ToolError",
    "ToolExecutor",
    "ToolRegistry",
    "ToolRequest",
    "ToolRequestParams",
    "ToolResult",
    "ToolStatus",
    "create_default_registry",
    "tool_result_to_b_input",
    "tool_result_to_rag_result",
]
