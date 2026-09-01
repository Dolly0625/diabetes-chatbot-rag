"""第 10 步驗收測試：兩個 demo 問題（CLAUDE.md §1）端到端都能產出合理、
標明來源、風險等級正確的回應。

vector 軌道需要網路與 GEMINI_API_KEY，本測試環境沒有。測試 1 讓它失敗
（藉此驗證 RETRIEVER_DEGRADED——一個真實的失敗模式），因為胰島素問題
只需要 graph 端的證據。測試 2 則把 VectorRetriever.search 換成不需要
網路的替代實作（自我檢索，做法同 test_vector.py），因為飲食問題明確
需要 vector 端的證據。
"""

import rag_retrieval.retrievers.vector as vector_module
import rag_retrieval.tool as tool_module
from rag_retrieval import EvidenceRetrievalTool
from rag_retrieval.contract.enums import EvidenceRiskLevel, RetrievalStatus, WarningCode
from rag_retrieval.loaders import VectorChunkRecord, load_education_documents, load_vector_chunks


def _request(request_id: str, user_raw_input: str, retrieval_queries: list[str]) -> dict:
    return {
        "request_id": request_id,
        "schema_version": "rag-v1",
        "user_raw_input": user_raw_input,
        "retrieval_queries": retrieval_queries,
        "guardrail_result": {
            "intent_tags": ["GENERAL_EDUCATION"],
            "risk_flags": [],
            "context_modifiers": {
                "time_frame": "CURRENT",
                "target_subject": "SELF",
                "polarity": "AFFIRMATIVE",
                "language": "zh-TW",
            },
            "router_status": "G_GENERAL_EDUCATION",
            "reason_codes": ["MEETS_SAFE_SCOPE"],
        },
        "language": "zh-TW",
        "timestamp": "2026-09-03T14:00:00+08:00",
    }


def test_insulin_injection_question_returns_graph_evidence_with_correct_risk(monkeypatch):
    def failing_search(self, queries, top_k=10):
        raise RuntimeError("no network in test environment")

    monkeypatch.setattr(vector_module.VectorRetriever, "search", failing_search)

    tool = EvidenceRetrievalTool()
    response = tool.retrieve(
        _request(
            "req_test_insulin",
            "如何施打胰島素？",
            ["胰島素 注射部位", "胰島素 皮膚 澱粉樣變性症"],
        )
    )

    assert isinstance(response.chunks, list)
    assert response.chunks  # RAG 的工作是回傳證據；拒答是 LLM 組 Output Gate 的事
    assert response.retrieval_status == RetrievalStatus.PARTIAL  # vector 軌道降級
    assert WarningCode.RETRIEVER_DEGRADED in {w.code for w in response.warnings}
    assert any("insulin_amyloidosis" in c.chunk_id for c in response.chunks)

    risk_levels = {c.evidence_risk_level for c in response.chunks}
    assert EvidenceRiskLevel.HIGH in risk_levels  # TRIGGERS -> HIGH
    for chunk in response.chunks:
        if chunk.retriever == "graph":
            assert chunk.evidence_risk_level != EvidenceRiskLevel.UNKNOWN


def test_diet_question_returns_hpa_education_content_directly(monkeypatch):
    # 基礎的 85 筆 TFDA chunk 是藥物風險文字，不是病人衛教內容——這正是
    # CLAUDE.md §1 指出的缺口（「衛教語料是 8/29 才加進來的」），由
    # scripts/build_index.py（第 6 步）補上。真正做 embedding 需要網路與
    # GEMINI_API_KEY，這裡沒有，所以用一句真實的國健署句子搭配合成的
    # embedding——格式與 build_index.py 實際會產出的相同——依完成後索引
    # 真正承載的方式合併進去。
    diet_text = next(
        doc["page_content"]
        for doc in load_education_documents()
        if "均衡飲食" in doc["page_content"]
    )
    dim = 3072
    vector = [0.0] * dim
    vector[0] = 1.0
    diet_chunk = VectorChunkRecord(
        chunk_id="hpa-dm-book_sec1_00",
        source="hpa-dm-book",
        version="3rd-2022",
        date="2022-01-21",
        status="active",
        content=diet_text[:200],
        embedding=vector,
    )

    monkeypatch.setattr(
        tool_module, "load_vector_chunks", lambda: load_vector_chunks() + [diet_chunk]
    )

    def fake_search(self, queries, top_k=10):
        return self.search_by_vector(diet_chunk.embedding, top_k=top_k)

    monkeypatch.setattr(vector_module.VectorRetriever, "search", fake_search)

    tool = EvidenceRetrievalTool()
    response = tool.retrieve(
        _request(
            "req_test_diet",
            "糖尿病平常飲食要注意什麼？",
            ["第二型糖尿病 飲食原則", "糖尿病 均衡飲食"],
        )
    )

    assert isinstance(response.chunks, list)
    assert response.chunks
    # routing.py 出貨的預設值恆為 HYBRID（CLAUDE.md §7 對於還沒交付的
    # Multi-RAG A 模組所提供的預設實作），所以這個查詢也會跑 graph 軌道；
    # 「糖尿病」／「第二型糖尿病」是常見的 TREATS 三元組 object label，
    # 因此合理地可能碰到 2 跳預算上限而回傳 PARTIAL 而非 SUCCESS。兩者
    # 都是合理、帶有證據的回應——CONTRACT_v1 自己的範例
    # （05_success_education.json）則是把同一個問題用純 VECTOR 路由回答，
    # 未來可以由更聰明的 routing.py 特別處理這種情況。
    assert response.retrieval_status in (RetrievalStatus.SUCCESS, RetrievalStatus.PARTIAL)
    assert any(c.chunk_id == diet_chunk.chunk_id for c in response.chunks)
    assert any(c.evidence_risk_level == EvidenceRiskLevel.UNKNOWN for c in response.chunks)
    assert WarningCode.SOURCE_NOT_CLINICALLY_REVIEWED in {w.code for w in response.warnings}


def test_non_general_education_request_is_refused_independently():
    tool = EvidenceRetrievalTool()
    request = _request("req_test_referral", "我腎功能不好，可以吃 metformin 嗎？", ["metformin 腎功能"])
    request["guardrail_result"]["router_status"] = "M_MEDICATION_REFERRAL"
    request["guardrail_result"]["intent_tags"] = ["MEDICATION_CHANGE_REQUEST"]

    response = tool.retrieve(request)

    assert response.retrieval_status == RetrievalStatus.ERROR
    assert response.chunks == []
    assert response.warnings[0].code == WarningCode.ROUTER_STATUS_NOT_PERMITTED


def test_malformed_request_never_raises():
    tool = EvidenceRetrievalTool()
    response = tool.retrieve({"not": "a valid request"})
    assert response.retrieval_status == RetrievalStatus.ERROR
    assert response.chunks == []
