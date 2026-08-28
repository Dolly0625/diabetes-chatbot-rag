from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .schemas import B_SCHEMA_VERSION, CanonicalBResult, CanonicalEvidence


def _as_dict(value: Any) -> dict[str, Any]:
    """將輸入轉為 dict：支援 Pydantic BaseModel 與一般 Mapping，否則拋錯。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")  # Pydantic 模型 → 字典
    if isinstance(value, Mapping):
        return dict(value)  # 一般字典／Mapping → 淺拷貝
    raise TypeError(f"expected mapping-like value, got {type(value).__name__}")


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    """多鍵回退取值：依序嘗試多個鍵，回傳第一個非 None 的值，皆無則回 None。

    用途：相容舊欄位命名（如 evidence_id / document_id / chunk_id / id），
    讓新舊資料都能被正確解析。
    """
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def normalize_evidence(value: Any) -> CanonicalEvidence:
    """將舊版 document_id/chunk_id 檢索列轉為標準 CanonicalEvidence。

    多鍵回退策略：
      - evidence_id 依序嘗試：evidence_id → document_id → chunk_id → id
      - content     依序嘗試：content → page_content → text
      - source      依序嘗試：source → source_dataset（含 metadata 回退）
      - score       依序嘗試：score → similarity_score → reranker_score
      - date/version 亦有對應回退邏輯

    參數:
        value: 單筆檢索記錄（dict / BaseModel / Mapping）
    回傳:
        標準化的 CanonicalEvidence
    拋錯:
        缺少識別或內容時拋 ValueError
    """

    raw = _as_dict(value)  # 先統一轉為 dict
    # 多鍵回退：相容新舊欄位命名
    evidence_id = _first(raw, "evidence_id", "document_id", "chunk_id", "id")
    content = _first(raw, "content", "page_content", "text")
    if evidence_id is None:
        raise ValueError("retrieval record has no evidence/document/chunk identifier")
    if content is None or not str(content).strip():
        raise ValueError(f"retrieval record {evidence_id!r} has no content")

    # metadata 處理：若原始有 metadata 則拷貝，否則空 dict
    raw_metadata = raw.get("metadata", {})
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    # 已知鍵集合：這些鍵屬於頂層欄位，不應重複塞入 metadata
    known_keys = {
        "evidence_id",
        "document_id",
        "chunk_id",
        "id",
        "content",
        "page_content",
        "text",
        "metadata",
        "source",
        "source_dataset",
        "score",
        "similarity_score",
        "reranker_score",
        "date",
        "version",
        "score_type",
        "status",
        "retriever",
        "evidence_risk_level",
        "risk_level",
        "safety_signal_types",
        "safety_signals",
        "risk_basis",
        "entities",
        "relations",
    }
    # 未知鍵 → 保留到 metadata，避免資訊遺失
    for key, item in raw.items():
        if key not in known_keys and key not in metadata:
            metadata[key] = item

    # source 多鍵回退：頂層 source/source_dataset → metadata 內回退
    source = _first(raw, "source", "source_dataset")
    if source is None:
        source = metadata.get("source") or metadata.get("source_dataset")
    # date 多鍵回退：頂層 date → metadata date / 發布日期
    date = _first(raw, "date")
    if date is None:
        date = metadata.get("date") or metadata.get("發布日期")
    # version 回退
    version = _first(raw, "version")
    if version is None:
        version = metadata.get("version")
    # score 多鍵回退：score → similarity_score → reranker_score
    score = _first(raw, "score", "similarity_score", "reranker_score")
    # RAG Kickoff 擴充欄位
    score_type = _first(raw, "score_type")
    if score_type is None:
        score_type = metadata.get("score_type")
    status = _first(raw, "status")
    if status is None:
        status = metadata.get("status")
    retriever = _first(raw, "retriever")
    if retriever is None:
        retriever = metadata.get("retriever")
    # 風險等級（文件 §二-3）
    evidence_risk_level = _first(raw, "evidence_risk_level", "risk_level")
    if evidence_risk_level is None:
        evidence_risk_level = metadata.get("evidence_risk_level") or metadata.get("risk_level")
    safety_signal_types = _first(raw, "safety_signal_types", "safety_signals")
    if safety_signal_types is None:
        safety_signal_types = metadata.get("safety_signal_types") or metadata.get("safety_signals")
    # 正規化為 list[str]
    if safety_signal_types is None:
        safety_signal_types = []
    elif isinstance(safety_signal_types, str):
        safety_signal_types = [safety_signal_types]
    elif isinstance(safety_signal_types, (list, tuple, set)):
        safety_signal_types = [str(x) for x in safety_signal_types]
    else:
        safety_signal_types = [str(safety_signal_types)]
    risk_basis = _first(raw, "risk_basis")
    if risk_basis is None:
        risk_basis = metadata.get("risk_basis")
    # Graph 專屬
    entities = _first(raw, "entities")
    if entities is None:
        entities = metadata.get("entities")
    if entities is None:
        entities = []
    elif not isinstance(entities, list):
        entities = [entities]
    relations = _first(raw, "relations")
    if relations is None:
        relations = metadata.get("relations")
    if relations is None:
        relations = []
    elif not isinstance(relations, list):
        relations = [relations]

    return CanonicalEvidence(
        evidence_id=str(evidence_id),
        content=str(content),
        source=str(source) if source is not None else None,
        metadata=metadata,
        score=float(score) if score is not None else None,
        date=str(date) if date is not None else None,
        version=str(version) if version is not None else None,
        score_type=str(score_type) if score_type is not None else None,
        status=str(status) if status is not None else None,
        retriever=str(retriever) if retriever is not None else None,
        evidence_risk_level=str(evidence_risk_level).upper() if evidence_risk_level is not None else None,
        safety_signal_types=safety_signal_types,
        risk_basis=str(risk_basis) if risk_basis is not None else None,
        entities=list(entities),
        relations=list(relations),
    )


def normalize_evidence_list(values: list[Any] | None) -> list[CanonicalEvidence]:
    """批次標準化：將多筆檢索記錄逐一轉為 CanonicalEvidence 列表。"""
    return [normalize_evidence(value) for value in (values or [])]


def adapt_legacy_b_result(
    value: Any,
    *,
    request_id: str,
    original_query: str,
    retrieval_queries: list[str] | None = None,
) -> CanonicalBResult:
    """轉接舊版階段腳本的 B 結果，保留其原始來源不被竄改。

    多鍵回退：decision/b_decision、approved_evidence_ids/approved_document_ids、
    evidence/contexts/retrieved_contexts/context_rows 皆有相容處理。

    參數:
        value: 舊版 B 結果（dict / BaseModel）
        request_id: 新契約的請求 ID
        original_query: 使用者原始提問
        retrieval_queries: 檢索查詢列表
    回傳:
        標準化的 CanonicalBResult
    """

    raw = _as_dict(value)
    decision = _first(raw, "decision", "b_decision")  # 多鍵回退取決策
    if decision is None:
        raise ValueError("legacy B result has no decision/b_decision")
    approved = _first(raw, "approved_evidence_ids", "approved_document_ids") or []
    raw_evidence = _first(raw, "evidence", "contexts", "retrieved_contexts", "context_rows") or []
    evidence = normalize_evidence_list(raw_evidence)  # 證據列表標準化
    return CanonicalBResult(
        request_id=request_id,
        schema_version=str(raw.get("schema_version", B_SCHEMA_VERSION)),
        decision=str(decision),
        approved_evidence_ids=[str(item) for item in approved],
        evidence=evidence,
        reason_codes=[str(item) for item in (raw.get("reason_codes") or [])],
        identified_missing_information=[
            str(item) for item in (raw.get("identified_missing_information") or [])
        ],
        retrieval_feedback={
            "original_query": original_query,
            "retrieval_queries": retrieval_queries or [],
            **(dict(raw.get("retrieval_feedback")) if isinstance(raw.get("retrieval_feedback"), Mapping) else {}),
        },
        relevance=raw.get("relevance"),
        sufficiency=raw.get("sufficiency"),
        conflict=raw.get("conflict"),
        safety=raw.get("safety"),
    )
