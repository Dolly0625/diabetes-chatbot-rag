from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_huggingface import HuggingFaceEmbeddings

from run_config import PROCESSED_DIR, RESULTS_DIR, ensure_run_dirs

DOCS_PATH = PROCESSED_DIR / "langchain_documents.json"

NARROW_QUERY = "TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？"
BROAD_QUERY = "TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？"

EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_DEVICE = os.getenv("RERANKER_DEVICE", "cpu")
CANDIDATE_K = 20
RERANKER_TOP_N = 10


def build_embeddings():
    return HuggingFaceEmbeddings(
        model_name=EMBED_MODEL,
        encode_kwargs={"normalize_embeddings": True, "prompt": "passage: "},
        query_encode_kwargs={"normalize_embeddings": True, "prompt": "query: "},
    )


def contract_gate(documents: list[Document]):
    """Use the same thin Contract Gate as Phase 2; do not change the corpus."""
    seen: set[str] = set()
    passed: list[Document] = []
    rejected: list[dict[str, object]] = []
    for doc in documents:
        reasons = []
        document_id = doc.metadata.get("document_id")
        if not document_id:
            reasons.append("missing_document_id")
        elif document_id in seen:
            reasons.append("duplicate_document_id")
        else:
            seen.add(document_id)
        if doc.metadata.get("row_index") is None:
            reasons.append("missing_row_index")
        if not str(doc.metadata.get("藥品成分", "")).strip():
            reasons.append("empty_藥品成分")
        if not str(doc.metadata.get("發布日期", "")).strip():
            reasons.append("empty_發布日期")
        if not doc.page_content.strip():
            reasons.append("empty_page_content")
        if reasons:
            rejected.append({"document_id": document_id, "reasons": reasons})
        else:
            passed.append(doc)
    return passed, rejected


def analysis_note(raw: dict) -> str:
    text = " ".join(
        str(raw.get(field) or "")
        for field in ["訊息緣由", "藥品安全有關資訊分析及描述", "TFDA風險溝通說明"]
    )
    if "酮酸" in text or "ketoacidosis" in text.lower():
        return "原文出現酮酸中毒／ketoacidosis"
    if "截肢" in text or "amputation" in text.lower():
        return "原文出現下肢截肢／amputation"
    if "Fournier" in text or "會陰" in text or "壞死性筋膜炎" in text:
        return "原文出現 Fournier gangrene／會陰部壞死性筋膜炎"
    return "未用規則指定安全主題"


def preview(text: str, limit: int = 520) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


def row_base(rank: int, doc: Document, raw: dict) -> dict:
    return {
        "rank": rank,
        "document_id": doc.metadata["document_id"],
        "row_index": doc.metadata["row_index"],
        "發布日期": doc.metadata["發布日期"],
        "藥品成分": doc.metadata["藥品成分"],
        "analysis_note": analysis_note(raw),
        "page_content_preview": preview(doc.page_content),
        "安全資訊分析_preview": preview(
            raw.get("藥品安全有關資訊分析及描述", ""), 420
        ),
    }


def retrieve_candidates(
    store: InMemoryVectorStore,
    docs_by_id: dict[str, dict],
    query: str,
) -> list[dict]:
    retrieved = store.similarity_search_with_score(query, k=CANDIDATE_K)
    rows = []
    for similarity_rank, (doc, similarity_score) in enumerate(retrieved, 1):
        raw = docs_by_id[doc.metadata["document_id"]]["raw_record"]
        row = row_base(similarity_rank, doc, raw)
        row["similarity_rank"] = similarity_rank
        row["similarity_score"] = round(float(similarity_score), 6)
        rows.append({"document": doc, "row": row})
    return rows


def rerank_candidates(
    candidates: list[dict],
    query: str,
    cross_encoder: HuggingFaceCrossEncoder,
    reranker: CrossEncoderReranker,
) -> list[dict]:
    documents = [item["document"] for item in candidates]

    # CrossEncoderReranker uses this exact score(query, document) operation
    # internally. We also call the public score method once so the experiment
    # can report the real score instead of inventing one.
    scores = list(cross_encoder.score([(query, doc.page_content) for doc in documents]))
    score_by_id = {
        doc.metadata["document_id"]: float(score)
        for doc, score in zip(documents, scores, strict=True)
    }

    # Use the official LangChain compressor for the ranking operation.
    reranked_documents = list(reranker.compress_documents(documents, query))
    reranked = []
    for reranker_rank, doc in enumerate(reranked_documents, 1):
        row = next(
            item["row"]
            for item in candidates
            if item["document"].metadata["document_id"]
            == doc.metadata["document_id"]
        )
        enriched = dict(row)
        enriched["reranker_rank"] = reranker_rank
        enriched["reranker_score"] = round(
            score_by_id[doc.metadata["document_id"]], 6
        )
        # Keep the requested names explicit in the final result.
        enriched["original_similarity_rank"] = row["similarity_rank"]
        enriched["original_similarity_score"] = row["similarity_score"]
        reranked.append(enriched)
    return reranked


def save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def format_rows(title: str, query: str, rows: list[dict]) -> list[str]:
    lines = ["", title, f"Query: {query}"]
    for row in rows:
        lines.append(
            f"{row['reranker_rank']}. reranker_score={row['reranker_score']:.6f} | "
            f"{row['document_id']} | row_index={row['row_index']} | "
            f"date={row['發布日期']} | ingredient={row['藥品成分']} | "
            f"similarity_rank={row['similarity_rank']} | "
            f"similarity_score={row['similarity_score']:.6f} | "
            f"{row['analysis_note']}"
        )
        lines.append(f"   page_content_preview: {row['page_content_preview']}")
        lines.append(f"   safety_preview: {row['安全資訊分析_preview']}")
    return lines


def run_one_query(
    store: InMemoryVectorStore,
    docs_by_id: dict[str, dict],
    query: str,
    result_name: str,
    candidate_name: str,
    cross_encoder: HuggingFaceCrossEncoder,
    reranker: CrossEncoderReranker,
) -> tuple[list[dict], list[dict], list[str]]:
    candidates = retrieve_candidates(store, docs_by_id, query)
    candidate_rows = [item["row"] for item in candidates]
    reranked_rows = rerank_candidates(candidates, query, cross_encoder, reranker)
    save_json(
        RESULTS_DIR / candidate_name,
        {"query": query, "candidate_k": CANDIDATE_K, "results": candidate_rows},
    )
    save_json(
        RESULTS_DIR / result_name,
        {
            "query": query,
            "candidate_k": CANDIDATE_K,
            "reranker_model": RERANKER_MODEL,
            "reranker_top_n": RERANKER_TOP_N,
            "results": reranked_rows,
        },
    )
    return candidate_rows, reranked_rows, format_rows(
        "RERANKED TOP-10", query, reranked_rows
    )


def main() -> None:
    ensure_run_dirs()
    serialized = json.loads(DOCS_PATH.read_text(encoding="utf-8"))
    documents = [
        Document(
            id=item["id"],
            page_content=item["page_content"],
            metadata=item["metadata"],
        )
        for item in serialized
    ]
    docs_by_id = {item["id"]: item for item in serialized}
    passed, rejected = contract_gate(documents)

    store = InMemoryVectorStore(embedding=build_embeddings())
    store.add_documents(passed)

    cross_encoder = HuggingFaceCrossEncoder(
        model_name=RERANKER_MODEL,
        model_kwargs={"device": RERANKER_DEVICE},
    )
    reranker = CrossEncoderReranker(model=cross_encoder, top_n=RERANKER_TOP_N)

    narrow_candidates, narrow_rows, narrow_lines = run_one_query(
        store,
        docs_by_id,
        NARROW_QUERY,
        "narrow_query_reranked_top10.json",
        "narrow_query_similarity_top20_candidates.json",
        cross_encoder,
        reranker,
    )
    broad_candidates, broad_rows, broad_lines = run_one_query(
        store,
        docs_by_id,
        BROAD_QUERY,
        "broad_query_reranked_top10.json",
        "broad_query_similarity_top20_candidates.json",
        cross_encoder,
        reranker,
    )

    target_ids = [
        item["id"]
        for item in serialized
        if item["metadata"].get("藥品成分") == "SGLT2抑制劑類"
    ]
    target_lines = ["", "SGLT2 TARGET RECORDS"]
    for document_id in target_ids:
        narrow_candidate = next(
            (r for r in narrow_candidates if r["document_id"] == document_id), None
        )
        broad_candidate = next(
            (r for r in broad_candidates if r["document_id"] == document_id), None
        )
        narrow = next((r for r in narrow_rows if r["document_id"] == document_id), None)
        broad = next((r for r in broad_rows if r["document_id"] == document_id), None)
        target_lines.append(
            f"{document_id} | narrow_similarity_rank="
            f"{narrow_candidate['similarity_rank'] if narrow_candidate else 'not in candidate-20'} | "
            f"narrow_reranker_rank="
            f"{narrow['reranker_rank'] if narrow else 'not in reranked-top-10'} | "
            f"broad_similarity_rank="
            f"{broad_candidate['similarity_rank'] if broad_candidate else 'not in candidate-20'} | "
            f"broad_reranker_rank="
            f"{broad['reranker_rank'] if broad else 'not in reranked-top-10'}"
        )

    output = [
        f"Contract Gate: total={len(documents)} passed={len(passed)} rejected={len(rejected)}",
        f"Embedding model: {EMBED_MODEL}",
        f"Reranker model: {RERANKER_MODEL}",
        f"Reranker device: {RERANKER_DEVICE}",
        f"Candidate K: {CANDIDATE_K}",
        f"Reranker top_n: {RERANKER_TOP_N}",
        *narrow_lines,
        *broad_lines,
        *target_lines,
    ]
    print("\n".join(output))
    (RESULTS_DIR / "phase3_reranker_output.txt").write_text(
        "\n".join(output) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
