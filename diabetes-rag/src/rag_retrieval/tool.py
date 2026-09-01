"""EvidenceRetrievalTool——整條管線的組裝點。本套件唯一的對外入口：

    from rag_retrieval import EvidenceRetrievalTool
    tool = EvidenceRetrievalTool(source_id="tfda+hpa")
    response = tool.retrieve(request)

固定的管線順序（CLAUDE.md 建置順序第 10 步的「關鍵順序限制」）：RRF 只看
名次、不看分數大小，所以如果門檻檢查放在融合之後才做，一筆低信心的候選
可能在自己軌道裡排名第一，因而拿到該軌道在融合時的最大貢獻值。順序是
固定的：

    gate_in -> route -> 各軌道檢索 -> 門檻過濾 -> 排序 ->
    RRF 融合 -> 截斷 -> 風險標註 -> gate_out

（最後的「-> gate_out」代表出口／最終組裝 response 的階段——實際的門檻、
allow-list、截斷等 gate_out.py 提供的呼叫，依上面箭頭所示，是在這個順序
中較早執行的。）

不可退讓事項 #4（CLAUDE.md §2）：本函式絕不向呼叫端拋出例外。任何失敗
——schema 錯誤、缺欄位、內部例外——都會變成合法的 RetrievalResponse，
`retrieval_status="ERROR"`、`chunks=[]`。`retrieve()` 把整條管線包在
一個 try/except 裡，就是為了做到這件事。
"""

from __future__ import annotations

from typing import Union

from . import gate_in, gate_out, routing
from .contract.enums import GraphPathStatus, RetrievalRoute, RetrievalStatus, WarningCode
from .contract.errors import error_response
from .contract.models import Entity, Relation, RetrievalResponse, RetrievedChunk, Warning
from .fusion import reciprocal_rank_fusion
from .loaders import load_graph_triples, load_vector_chunks
from .retrievers.base import Candidate
from .retrievers.graph import GraphRetriever
from .retrievers.vector import VectorRetriever
from .risk import annotate_candidate, max_evidence_risk_level

# 每個軌道在門檻過濾／融合／截斷之前，各自先拉取的候選數量。
TOP_K_PER_TRACK = 10


class EvidenceRetrievalTool:
    def __init__(self, source_id: str = "tfda+hpa", top_n: int = gate_out.DEFAULT_TOP_N):
        # source_id 對應 LLM 組 EvidenceRetrievalTool(source_id, ...) 的
        # 介面形狀（提案書 §5.3）。本套件目前只出貨一份合併語料
        # （TFDA 風險溝通 + 國健署《糖尿病與我》），所以 source_id 只是為了
        # 介面穩定而接受，還沒有真的用來在多份語料間切換——詳見 README。
        self.source_id = source_id
        self.top_n = top_n
        self._vector_retriever = VectorRetriever(load_vector_chunks())
        self._graph_retriever = GraphRetriever(load_graph_triples())

    def retrieve(self, raw_request: Union[dict, object]) -> RetrievalResponse:
        try:
            return self._retrieve(raw_request)
        except Exception as exc:  # noqa: BLE001 —— CLAUDE.md 不可退讓事項 #4
            request_id = raw_request.get("request_id") if isinstance(raw_request, dict) else None
            return error_response(
                request_id,
                WarningCode.SCHEMA_VALIDATION_FAILED,
                f"internal exception: {exc}",
            )

    def _retrieve(self, raw_request: Union[dict, object]) -> RetrievalResponse:
        request, error = gate_in.admit(raw_request)
        if error is not None:
            return error
        assert request is not None

        route = routing.decide_route(request.guardrail_result.intent_tags)

        collected_warnings: list[Warning] = []
        graph_path_status = GraphPathStatus.NOT_APPLICABLE
        hop_limit_reached = False
        retriever_degraded = False

        vector_kept: list[Candidate] = []
        if route in (RetrievalRoute.VECTOR, RetrievalRoute.HYBRID):
            try:
                raw_vector = self._vector_retriever.search(
                    request.retrieval_queries, top_k=TOP_K_PER_TRACK
                )
            except Exception as exc:  # 例如 GEMINI_API_KEY 未設定、網路不通
                retriever_degraded = True
                raw_vector = []
                collected_warnings.append(
                    Warning(code=WarningCode.RETRIEVER_DEGRADED, detail=f"vector retriever failed: {exc}")
                )
            vector_kept, vector_warnings = gate_out.gate_out_per_track(raw_vector)
            collected_warnings.extend(Warning(code=w) for w in vector_warnings)

        graph_kept: list[Candidate] = []
        if route in (RetrievalRoute.GRAPH, RetrievalRoute.HYBRID):
            graph_result = self._graph_retriever.search(
                request.retrieval_queries, top_k=TOP_K_PER_TRACK
            )
            graph_path_status = GraphPathStatus(graph_result.graph_path_status)
            hop_limit_reached = graph_result.hop_limit_reached
            if hop_limit_reached:
                collected_warnings.append(Warning(code=WarningCode.GRAPH_HOP_LIMIT_REACHED))
            graph_kept, graph_warnings = gate_out.gate_out_per_track(graph_result.candidates)
            collected_warnings.extend(Warning(code=w) for w in graph_warnings)

        fused = reciprocal_rank_fusion(vector_kept, graph_kept)
        truncated = gate_out.truncate_balanced(fused, top_n=self.top_n)
        chunks = [self._to_chunk(c) for c in truncated]

        if not chunks:
            status = RetrievalStatus.EMPTY
            rerun_suggested = True
        elif retriever_degraded or hop_limit_reached:
            status = RetrievalStatus.PARTIAL
            rerun_suggested = False
        else:
            status = RetrievalStatus.SUCCESS
            rerun_suggested = False

        # CONTRACT_v1 自己的範例只在 chunks 非空時帶這個 warning
        # （02_empty.json 與 04 的兩個變體都沒有；01/03/05 都有）——
        # CLAUDE.md 不可退讓事項 #10 說的「每個回應」，指的是每個真的
        # 回傳了證據的回應，不包含檢索完全沒有產出的那些。
        if chunks:
            collected_warnings.append(Warning(code=WarningCode.SOURCE_NOT_CLINICALLY_REVIEWED))

        return RetrievalResponse(
            request_id=request.request_id,
            retrieval_route=route,
            retrieval_status=status,
            graph_path_status=graph_path_status,
            rerun_suggested=rerun_suggested,
            max_evidence_risk_level=max_evidence_risk_level([c.evidence_risk_level for c in chunks]),
            warnings=_dedupe_warnings(collected_warnings),
            chunks=chunks,
        )

    @staticmethod
    def _to_chunk(candidate: Candidate) -> RetrievedChunk:
        level, signals, basis = annotate_candidate(candidate)
        kwargs = dict(
            chunk_id=candidate.chunk_id,
            source=candidate.source,
            version=candidate.version,
            date=candidate.date,
            score=candidate.score,
            score_type=candidate.score_type,
            status=candidate.status,
            content=candidate.content,
            retriever=candidate.retriever,
            evidence_risk_level=level,
            safety_signal_types=signals,
            risk_basis=basis,
        )
        if candidate.entities is not None:
            kwargs["entities"] = [Entity(**e) for e in candidate.entities]
        if candidate.relations is not None:
            kwargs["relations"] = [Relation(**r) for r in candidate.relations]
        return RetrievedChunk(**kwargs)


def _dedupe_warnings(warnings: list[Warning]) -> list[Warning]:
    seen: set[tuple] = set()
    result: list[Warning] = []
    for warning in warnings:
        key = (warning.code, warning.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result
