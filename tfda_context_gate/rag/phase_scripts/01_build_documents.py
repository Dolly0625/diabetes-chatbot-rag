from __future__ import annotations

import json
from pathlib import Path

from langchain_core.documents import Document

from run_config import RAW_DIR, PROCESSED_DIR, ensure_run_dirs

RAW_PATH = RAW_DIR / "drug_risk_communication.json"
OUTPUT_PATH = PROCESSED_DIR / "langchain_documents.json"

CONTENT_FIELDS = [
    "藥品成分",
    "適應症",
    "藥理作用機轉",
    "訊息緣由",
    "藥品安全有關資訊分析及描述",
    "TFDA風險溝通說明",
]


def value_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def build_page_content(record: dict) -> str:
    sections = []
    for field in CONTENT_FIELDS:
        value = value_text(record.get(field))
        if value:
            sections.append(f"{field}：\n{value}")
    return "\n\n".join(sections)


def build_documents(records: list[dict]) -> list[Document]:
    documents = []
    for row_index, record in enumerate(records):
        document_id = f"tfda-risk-{row_index:04d}"
        page_content = build_page_content(record)
        metadata = {
            # These three fields are added by this experiment pipeline.
            "document_id": document_id,
            "row_index": row_index,
            "source_dataset": "TFDA 藥品安全資訊風險溝通資料",
            "raw_source_file": "data/raw/drug_risk_communication.json",
            # These two fields are copied from the TFDA record.
            "發布日期": value_text(record.get("發布日期")),
            "藥品成分": value_text(record.get("藥品成分")),
        }
        documents.append(
            Document(
                id=document_id,
                page_content=page_content,
                metadata=metadata,
            )
        )
    return documents


def main() -> None:
    ensure_run_dirs()
    records = json.loads(RAW_PATH.read_text(encoding="utf-8-sig"))
    documents = build_documents(records)
    serialized = []
    for record, document in zip(records, documents):
        serialized.append(
            {
                "id": document.id,
                "page_content": document.page_content,
                "metadata": document.metadata,
                # Keep the exact raw record for provenance and later manual review.
                "raw_record": record,
            }
        )
    OUTPUT_PATH.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"raw_records={len(records)}")
    print(f"langchain_documents={len(documents)}")
    print(f"output={OUTPUT_PATH}")
    print(f"empty_page_content={sum(not doc.page_content for doc in documents)}")


if __name__ == "__main__":
    main()
