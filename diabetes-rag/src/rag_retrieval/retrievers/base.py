"""共用的候選證據格式與 retriever 協定。vector.py 和 graph.py（以及選用的
neo4j_backend.py）都會產生 `Candidate` 清單——這些是已經有分數、但尚未經過
門檻過濾、融合或風險標註的證據。這些步驟會在管線後段（tool.py）依照
其中記載的固定順序執行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class Candidate:
    chunk_id: str
    source: str
    version: str
    date: str
    status: str
    content: str
    retriever: str  # "vector" | "graph"
    score: float
    score_type: str  # "similarity" | "graph_traversal"
    entities: Optional[list[dict]] = None
    relations: Optional[list[dict]] = None
    # 僅 graph 使用：此 chunk 唯一的 relation 型別，直接供 risk.py 查表用。
    # vector 候選為 None。
    relation_type: Optional[str] = None
    # 僅 graph 使用：抽取階段的 confidence／negation_checked，供 fusion 前的
    # 信心門檻（Boundary B §1.1）使用。
    confidence: Optional[float] = None
    negation_checked: Optional[bool] = None
    warnings: list[str] = field(default_factory=list)


class Retriever(Protocol):
    def search(self, queries: list[str], top_k: int) -> list[Candidate]:
        ...
