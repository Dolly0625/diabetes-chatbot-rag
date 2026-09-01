"""第 3 步驗收測試：gate_in.admit() 能重現 04_error_validation.json 的兩種
情境——retrieval_status／warning code／chunks 都要一致，但自由文字的
`detail` 不需要與範例逐字相同。
"""

import json
from pathlib import Path

import pytest

from rag_retrieval.contract.enums import RetrievalStatus, WarningCode
from rag_retrieval.gate_in import admit

EXAMPLES_DIR = (
    Path(__file__).resolve().parents[2] / "02_MS2_demo" / "contract" / "examples"
)

# 參見 test_contract.py：02_MS2_demo 不在本 repo 內，單獨 clone 時不會存在。
pytestmark = pytest.mark.skipif(
    not EXAMPLES_DIR.exists(),
    reason="02_MS2_demo/contract/examples/ 不在本 repo 內（僅限實驗室 monorepo 的測試素材）",
)


def _load(name):
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def test_router_status_not_permitted_rejects_independently():
    data = _load("04_error_validation.json")
    request, error = admit(data["request"])
    assert request is None
    assert error is not None
    assert error.retrieval_status == RetrievalStatus.ERROR
    assert error.chunks == []
    assert error.warnings[0].code == WarningCode.ROUTER_STATUS_NOT_PERMITTED
    assert error.request_id == data["request"]["request_id"]


def test_schema_validation_failure_variant():
    data = _load("04_error_validation.json")
    bad_request = data["_variant_schema_validation_failure"]["bad_request"]
    request, error = admit(bad_request)
    assert request is None
    assert error is not None
    assert error.retrieval_status == RetrievalStatus.ERROR
    assert error.chunks == []
    assert error.warnings[0].code == WarningCode.SCHEMA_VALIDATION_FAILED
    assert error.request_id == bad_request["request_id"]


def test_valid_general_education_request_is_admitted():
    data = _load("01_success_hybrid.json")
    request, error = admit(data["request"])
    assert error is None
    assert request is not None
    assert request.request_id == data["request"]["request_id"]


def test_unknown_enum_value_is_distinguished_from_generic_schema_failure():
    data = _load("01_success_hybrid.json")
    bad = json.loads(json.dumps(data["request"]))
    bad["guardrail_result"]["intent_tags"] = ["NOT_A_REAL_INTENT_TAG"]
    request, error = admit(bad)
    assert request is None
    assert error.warnings[0].code == WarningCode.UNKNOWN_ENUM_VALUE
