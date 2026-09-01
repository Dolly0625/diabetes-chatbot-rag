"""載入並正規化打包的資料資產。

本模組是**唯一**會碰觸原始管道輸出的地方。資料問題（非 ISO 日期格式、
已知的斷詞器錯誤、部分圖譜三元組缺少來源日期）都在載入時於此修正——
絕不修改 `../pipelines/*`（CLAUDE.md §10）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Optional

_DATA_PKG = "rag_retrieval.data"

# tfda-risk-019_sec3_04 是已知的斷詞器錯誤：因為斷詞器是依「.」而非 URL
# 邊界切分，導致一個網址（"4.htm"）整個變成 chunk 內容。這筆已在上游被
# embedding，CLAUDE.md §4 說可以在載入時過濾或修正——過濾是較小、較安全的改法。
_BAD_VECTOR_CHUNK_IDS = frozenset({"tfda-risk-019_sec3_04"})

_SLASH_DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_BRACKET_DATE_RE = re.compile(r"^\[(\d{4})/(\d{1,2})/(\d{1,2})\]\s*")

# 有四個圖譜三元組的來源，在上游完全沒有發布日期的中繼資料（不像
# tfda-risk-NNN 那些來源在 source_excerpt 裡帶有日期方括號）。下面的日期是
# 從涵蓋同一成分／主題的 TFDA 風險溝通推算出來的（已對照
# pipelines/graph_pipeline/extract.py 的 TEST_DOCUMENTS 與原始 TFDA 語料
# 交叉核對），確保每個 chunk 都帶有真實、ISO 格式的日期，而不是憑空捏造的
# 佔位值。metformin 這筆不是推算——version／date 與 CONTRACT_v1 自己的
# 範例（chunk openfda_metformin_contraindications_tri_04）完全一致。
_GRAPH_SOURCE_DATE_OVERRIDES: dict[str, tuple[str, str]] = {
    "openfda_metformin_contraindications": ("2023-11-01", "ZITUVIMET-2023"),
    "tfda_canagliflozin_dapagliflozin_aki": ("2016-07-14", "tfda-risk-035"),
    "tfda_sglt2_ketoacidosis": ("2018-09-28", "tfda-risk-064"),
    "tfda_insulin_amyloidosis": ("2020-11-16", "tfda-risk-100"),
}


def normalise_date(raw: str) -> str:
    """'2016/7/14' -> '2016-07-14'。已經是 ISO 格式的輸入直接放行。"""
    m = _SLASH_DATE_RE.match(raw.strip())
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw.strip()):
        return raw.strip()
    raise ValueError(f"unrecognised date format: {raw!r}")


@dataclass(frozen=True)
class VectorChunkRecord:
    chunk_id: str
    source: str
    version: str
    date: str
    status: str
    content: str
    embedding: list[float]


@dataclass(frozen=True)
class GraphEntityRecord:
    id: str
    type: str
    label: str
    code: Optional[str]


@dataclass(frozen=True)
class GraphTripleRecord:
    chunk_id: str
    source: str
    version: str
    date: str
    status: str
    content: str
    subject: GraphEntityRecord
    subject_type: str
    relation: str
    object: GraphEntityRecord
    object_type: str
    condition: Optional[str]
    effect: Optional[str]
    confidence: Optional[float]
    negation_checked: Optional[bool]
    additional_sources: list[str] = field(default_factory=list)


_EDUCATION_CHUNKS_FILENAME = "education_chunks_embedded.json"


def _read_packaged_json(filename: str):
    with resources.files(_DATA_PKG).joinpath(filename).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_packaged_json_if_present(filename: str) -> Optional[list]:
    ref = resources.files(_DATA_PKG).joinpath(filename)
    if not ref.is_file():
        return None
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_vector_chunks(extra_paths: Optional[list[str]] = None) -> list[VectorChunkRecord]:
    """載入 85 筆基礎 TFDA chunk；一旦 scripts/build_index.py 產生了
    data/education_chunks_embedded.json（CLAUDE.md 建置順序第 6 步「合併
    進索引」），就會自動合併衛教語料，另外也接受呼叫端明確傳入的額外
    已 embedding chunk 檔案（主要供測試使用）。日期會正規化為 ISO-8601，
    已知的壞 chunk 會被捨棄。
    """
    raw = _read_packaged_json("embedded_chunks_output.json")
    education = _read_packaged_json_if_present(_EDUCATION_CHUNKS_FILENAME)
    if education:
        raw = raw + education
    for path in extra_paths or []:
        with open(path, "r", encoding="utf-8") as fh:
            raw = raw + json.load(fh)

    records: list[VectorChunkRecord] = []
    for c in raw:
        if c["chunk_id"] in _BAD_VECTOR_CHUNK_IDS:
            continue
        records.append(
            VectorChunkRecord(
                chunk_id=c["chunk_id"],
                source=c["source"],
                version=c["version"],
                date=normalise_date(c["date"]),
                status=c["status"],
                content=c["content"],
                embedding=c["embedding"],
            )
        )
    return records


def _derive_graph_source_meta(source: str, source_excerpt: str) -> tuple[str, str]:
    """回傳 (date_iso, version)。"""
    if source in _GRAPH_SOURCE_DATE_OVERRIDES:
        return _GRAPH_SOURCE_DATE_OVERRIDES[source]
    m = _BRACKET_DATE_RE.match(source_excerpt)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}", source
    raise ValueError(f"no date available for graph source {source!r}; add an override")


def _graph_content(source_excerpt: str) -> str:
    """若存在，去除管道殘留的 '[YYYY/M/D] ' 前綴。"""
    return _BRACKET_DATE_RE.sub("", source_excerpt, count=1).strip()


def load_graph_triples() -> list[GraphTripleRecord]:
    """載入 29 筆可檢索三元組，每筆各自成為一個 graph chunk。chunk_id 依照
    CONTRACT_v1 §2.4 格式：'{source}_tri_{seq:02d}'，序號依檔案內出現順序、
    每個 source 各自累計。
    """
    raw = _read_packaged_json("bronze_triples_retrievable.json")

    seq_by_source: dict[str, int] = {}
    records: list[GraphTripleRecord] = []
    for t in raw:
        source = t["source"]
        seq = seq_by_source.get(source, 0)
        seq_by_source[source] = seq + 1
        date, version = _derive_graph_source_meta(source, t["source_excerpt"])

        subj = t["subject"]
        obj = t["object"]
        records.append(
            GraphTripleRecord(
                chunk_id=f"{source}_tri_{seq:02d}",
                source=source,
                version=version,
                date=date,
                # 目前兩條上游管道皆寫死 status=active，所以 STALE 在真實
                # 資料上還無法被觸發（CONTRACT_v1 §5）。
                status="active",
                content=_graph_content(t["source_excerpt"]),
                subject=GraphEntityRecord(
                    id=subj["id"], type=subj["type"], label=subj["label"], code=subj.get("code")
                ),
                subject_type=subj["type"],
                relation=t["relation"],
                object=GraphEntityRecord(
                    id=obj["id"], type=obj["type"], label=obj["label"], code=obj.get("code")
                ),
                object_type=obj["type"],
                condition=t.get("condition"),
                effect=t.get("effect"),
                confidence=t.get("confidence"),
                negation_checked=t.get("negation_checked"),
                additional_sources=list(t.get("additional_sources") or []),
            )
        )
    return records


def load_education_documents() -> list[dict]:
    """原始的國健署《糖尿病與我》文件（21 篇），尚未切塊／embedding。
    由 scripts/build_index.py（第 6 步）使用。"""
    return _read_packaged_json("hpa_dm_book.json")
