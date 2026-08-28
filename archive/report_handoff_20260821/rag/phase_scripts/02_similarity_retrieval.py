from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_huggingface import HuggingFaceEmbeddings

from run_config import PROCESSED_DIR, RESULTS_DIR, ensure_run_dirs

DOCS_PATH = PROCESSED_DIR / "langchain_documents.json"

NARROW_QUERY = "TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？"
BROAD_QUERY = "TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？"


def build_embeddings():
    return HuggingFaceEmbeddings(
        model_name=os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small"),
        encode_kwargs={"normalize_embeddings": True, "prompt": "passage: "},
        query_encode_kwargs={"normalize_embeddings": True, "prompt": "query: "},
    )


def contract_gate(documents: list[Document]):
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
        return "analysis_note: 原文出現酮酸中毒／ketoacidosis"
    if "截肢" in text or "amputation" in text.lower():
        return "analysis_note: 原文出現下肢截肢／amputation"
    if "Fournier" in text or "會陰" in text or "壞死性筋膜炎" in text:
        return "analysis_note: 原文出現 Fournier gangrene／會陰部壞死性筋膜炎"
    return "analysis_note: 未用規則指定安全主題"


def preview(text: str, limit: int = 520) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


def result_row(rank: int, doc: Document, score: float, raw: dict) -> dict:
    return {
        "rank": rank,
        "similarity_score": round(float(score), 6),
        "document_id": doc.metadata["document_id"],
        "row_index": doc.metadata["row_index"],
        "發布日期": doc.metadata["發布日期"],
        "藥品成分": doc.metadata["藥品成分"],
        "analysis_note": analysis_note(raw),
        "page_content_preview": preview(doc.page_content),
        "訊息緣由_preview": preview(raw.get("訊息緣由", ""), 300),
        "安全資訊分析_preview": preview(raw.get("藥品安全有關資訊分析及描述", ""), 420),
    }


def run_query(store, docs_by_id: dict[str, dict], query: str, output_name: str):
    results = store.similarity_search_with_score(query, k=10)
    rows = []
    for rank, (doc, score) in enumerate(results, 1):
        raw = docs_by_id[doc.metadata["document_id"]]["raw_record"]
        rows.append(result_row(rank, doc, score, raw))
    (RESULTS_DIR / output_name).write_text(
        json.dumps({"query": query, "top_k": 10, "results": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return rows


def format_rows(title: str, query: str, rows: list[dict]) -> list[str]:
    lines = ["", title, f"Query: {query}"]
    for row in rows:
        lines.append(
            f"{row['rank']}. score={row['similarity_score']:.6f} | "
            f"{row['document_id']} | row_index={row['row_index']} | "
            f"date={row['發布日期']} | ingredient={row['藥品成分']} | "
            f"{row['analysis_note']}"
        )
        lines.append(f"   page_content_preview: {row['page_content_preview']}")
        lines.append(f"   safety_preview: {row['安全資訊分析_preview']}")
    return lines


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
    print(f"Contract Gate: total={len(documents)} passed={len(passed)} rejected={len(rejected)}")
    if rejected:
        print(json.dumps(rejected, ensure_ascii=False, indent=2))

    store = InMemoryVectorStore(embedding=build_embeddings())
    store.add_documents(passed)
    narrow_rows = run_query(store, docs_by_id, NARROW_QUERY, "narrow_query_top10.json")
    broad_rows = run_query(store, docs_by_id, BROAD_QUERY, "broad_query_top10.json")
    narrow_lines = format_rows("NARROW QUERY TOP-10", NARROW_QUERY, narrow_rows)
    broad_lines = format_rows("BROAD QUERY TOP-10", BROAD_QUERY, broad_rows)
    print("\n".join(narrow_lines))
    print("\n".join(broad_lines))

    target_rows = [
        item
        for item in serialized
        if item["metadata"].get("藥品成分") == "SGLT2抑制劑類"
    ]
    target_lines = ["", "SGLT2 TARGET RECORDS"]
    for item in target_rows:
        document_id = item["id"]
        narrow = next((row for row in narrow_rows if row["document_id"] == document_id), None)
        broad = next((row for row in broad_rows if row["document_id"] == document_id), None)
        target_lines.append(
            f"{document_id} | row_index={item['metadata']['row_index']} | "
            f"date={item['metadata']['發布日期']} | "
            f"narrow_rank={narrow['rank'] if narrow else 'not in top-10'} | "
            f"narrow_score={narrow['similarity_score'] if narrow else 'n/a'} | "
            f"broad_rank={broad['rank'] if broad else 'not in top-10'} | "
            f"broad_score={broad['similarity_score'] if broad else 'n/a'}"
        )
    print("\n".join(target_lines))

    output = [
        f"Contract Gate: total={len(documents)} passed={len(passed)} rejected={len(rejected)}",
        *narrow_lines,
        *broad_lines,
        *target_lines,
    ]
    (RESULTS_DIR / "phase2_similarity_output.txt").write_text("\n".join(output) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
