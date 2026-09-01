#!/usr/bin/env python3
"""將 21 篇國健署《糖尿病與我》衛教語料切塊、embedding，並合併進向量索引。

需要網路連線與環境變數 `GEMINI_API_KEY`——這是一次性（或資料變動時重跑）的
建置索引步驟，不是套件在 import 時會做的事。請在 demo 前先跑一次：

    export GEMINI_API_KEY=...
    python scripts/build_index.py

輸出寫到 src/rag_retrieval/data/education_chunks_embedded.json，格式與
embedded_chunks_output.json 相同，因此
loaders.load_vector_chunks(extra_paths=[...]) 會自動把它合併進去
（CLAUDE.md 建置順序第 6 步）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_retrieval.embedding import embed_document  # noqa: E402
from rag_retrieval.loaders import load_education_documents  # noqa: E402

SOURCE_ID = "hpa-dm-book"
# 對應 CONTRACT_v1 自己的範例（05_success_education.json）。
SOURCE_VERSION = "3rd-2022"
SOURCE_DATE = "2022-01-21"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])")
MAX_CHUNK_CHARS = 220
MIN_CHUNK_CHARS = 30
# 條列式段落完全沒有句尾標點；沒有硬上限的話，一個條列清單會變成
# 好幾千字的單一 chunk。
_HARD_CAP_CHARS = MAX_CHUNK_CHARS * 2

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "rag_retrieval"
    / "data"
    / "education_chunks_embedded.json"
)


def _split_oversized(unit: str) -> list[str]:
    if len(unit) <= _HARD_CAP_CHARS:
        return [unit]
    return [unit[i : i + MAX_CHUNK_CHARS] for i in range(0, len(unit), MAX_CHUNK_CHARS)]


def chunk_document(text: str) -> list[str]:
    """先依段落、再依句子邊界切分，貪婪地合併到 MAX_CHUNK_CHARS 為止；
    對完全沒有句尾標點的條列式段落則採硬換行後備方案。刻意做得簡單——
    不引入標準函式庫以外的依賴，這個規模（21 篇文件）足夠用了
    （CLAUDE.md §10：新增依賴前要先問過）。"""
    units: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        pieces = [p.strip() for p in _SENTENCE_SPLIT_RE.split(paragraph) if p.strip()]
        for piece in pieces or [paragraph]:
            units.extend(_split_oversized(piece))

    chunks: list[str] = []
    buf = ""
    for unit in units:
        if buf and len(buf) + len(unit) > MAX_CHUNK_CHARS:
            chunks.append(buf)
            buf = unit
        else:
            buf = f"{buf} {unit}" if buf else unit
    if buf:
        chunks.append(buf)
    if len(chunks) >= 2 and len(chunks[-1]) < MIN_CHUNK_CHARS:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}"
        chunks.pop()
    return chunks


def build(output_path: Path = DEFAULT_OUTPUT, sleep_seconds: float = 0.2) -> list[dict]:
    documents = load_education_documents()
    results: list[dict] = []
    for doc_idx, doc in enumerate(documents):
        pieces = chunk_document(doc["page_content"])
        for content in pieces:
            chunk_id = f"{SOURCE_ID}_sec{doc_idx}_{len(results):02d}"
            vector = embed_document(content)
            results.append(
                {
                    "chunk_id": chunk_id,
                    "source": SOURCE_ID,
                    "version": SOURCE_VERSION,
                    "date": SOURCE_DATE,
                    "status": "active",
                    "content": content,
                    "retriever": "vector",
                    "embedding_dim": len(vector),
                    "embedding": vector,
                }
            )
            print(f"  [{len(results)}] {chunk_id} -> {len(vector)} 維", file=sys.stderr)
            time.sleep(sleep_seconds)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    results = build(args.output)
    print(f"wrote {len(results)} chunks -> {args.output}")


if __name__ == "__main__":
    main()
