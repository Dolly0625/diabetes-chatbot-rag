"""A → Query Expansion → RAG → B → bounded Agent → C v2 → D workflow.

【繁中註解｜工作流程套件】
- 9 節點：A / QUERY_EXPANSION / RAG / B / AGENT_PLANNER / ASK_USER / QUERY_REWRITER / C / D
- 3 條件邊：a_route（A→QUERY_EXPANSION/END）、b_route（B→C/AGENT_PLANNER/END）、agent_route（AGENT_PLANNER→ASK_USER/QUERY_REWRITER/END）
- 唯一回環：QUERY_REWRITER→QUERY_EXPANSION；僅 B=INSUFFICIENT 且有 Planner 才進 Agent。
"""

from .graph import WorkflowState, build_workflow_graph
from .runner import run_workflow, stream_workflow
from .schemas import WorkflowResult

__all__ = ["WorkflowResult", "WorkflowState", "build_workflow_graph", "run_workflow", "stream_workflow"]
