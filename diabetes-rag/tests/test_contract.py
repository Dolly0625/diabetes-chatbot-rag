"""第 1 步驗收測試：5 個契約範例必須透過 Pydantic 模型完整回填、內容不變。
不要憑對 schema 的印象手寫斷言——要對照實際的範例檔案。
"""

import json
from pathlib import Path

import pytest

from rag_retrieval.contract.models import RetrievalRequest, RetrievalResponse

EXAMPLES_DIR = (
    Path(__file__).resolve().parents[2] / "02_MS2_demo" / "contract" / "examples"
)

# 02_MS2_demo 在上一層，不屬於本 repo（CLAUDE.md §3：rag_retrieval 是
# 自己獨立的 git repo；repo 之外的都是唯讀的實驗室共用資料）。單獨 clone
# 本 repo（例如 LLM 組）不會有這個相鄰目錄——用 skip 而非 fail，讓
# `pytest` 在那邊仍然是綠的。
pytestmark = pytest.mark.skipif(
    not EXAMPLES_DIR.exists(),
    reason="02_MS2_demo/contract/examples/ 不在本 repo 內（僅限實驗室 monorepo 的測試素材）",
)


def _round_trip(model_cls, payload: dict) -> None:
    model = model_cls.model_validate(payload)
    dumped = model.model_dump(mode="json", exclude_unset=True)
    assert dumped == payload


@pytest.mark.parametrize(
    "filename",
    [
        "01_success_hybrid.json",
        "02_empty.json",
        "03_partial.json",
        "05_success_education.json",
    ],
)
def test_request_response_round_trip(filename):
    data = json.loads((EXAMPLES_DIR / filename).read_text(encoding="utf-8"))
    _round_trip(RetrievalRequest, data["request"])
    _round_trip(RetrievalResponse, data["response"])


def test_error_validation_example_both_variants():
    data = json.loads(
        (EXAMPLES_DIR / "04_error_validation.json").read_text(encoding="utf-8")
    )
    _round_trip(RetrievalRequest, data["request"])
    _round_trip(RetrievalResponse, data["response"])

    variant = data["_variant_schema_validation_failure"]
    # bad_request 是刻意設計成不合法的（schema_version 錯誤、
    # retrieval_queries 型別錯誤）——它應該要驗證失敗，而不是能夠回填。
    with pytest.raises(Exception):
        RetrievalRequest.model_validate(variant["bad_request"])
    _round_trip(RetrievalResponse, variant["response"])


def test_all_five_example_files_present():
    expected = {
        "01_success_hybrid.json",
        "02_empty.json",
        "03_partial.json",
        "04_error_validation.json",
        "05_success_education.json",
    }
    actual = {p.name for p in EXAMPLES_DIR.glob("*.json")}
    assert expected <= actual
