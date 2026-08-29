# LLM ↔ RAG Retrieval Contract v0.1

實作位置：`tfda_context_gate/rag/external_contract.py`。此契約是外部 RAG 與現有
`QueryExpansionResult -> RAGResult -> Context Gate B` 之間的 adapter，不取代 B/D 安全門。

## Request

```json
{
  "request_id": "req-demo-001",
  "schema_version": "rag.request.v0.1",
  "user_raw_input": "請說明糖尿病一般飲食原則。",
  "retrieval_queries": ["糖尿病一般飲食原則"],
  "guardrail_result": {
    "intent_tags": ["GENERAL_EDUCATION"],
    "risk_flags": [],
    "context_modifiers": {
      "time_frame": "CURRENT",
      "target_subject": "SELF",
      "polarity": "AFFIRMATIVE",
      "language": "zh-TW"
    },
    "router_status": "G_GENERAL_EDUCATION",
    "reason_codes": ["INQUIRY_DIETARY_EDUCATION", "MEETS_SAFE_SCOPE"]
  },
  "language": "zh-TW",
  "timestamp": "2026-08-29T21:00:00+08:00"
}
```

- `user_raw_input` 原樣保留，不得被 `retrieval_queries` 取代。
- `timestamp` 必須帶 timezone。
- 僅 A gate 已放行的 `G_GENERAL_EDUCATION` 可建立 request；其他 route 在 adapter 即拒絕。
- `guardrail_result` 只讀，RAG 不得改寫 route 或自創 label。

## Response

```json
{
  "request_id": "req-demo-001",
  "schema_version": "rag.response.v0.1",
  "retrieval_route": "HYBRID",
  "retrieval_status": "PARTIAL",
  "graph_path_status": "PARTIAL",
  "rerun_suggested": true,
  "warnings": ["GRAPH_HOP_LIMIT_REACHED"],
  "chunks": [
    {
      "chunk_id": "tfda-001",
      "source": "TFDA",
      "content": "一般飲食原則需注意均衡飲食。",
      "score": 0.91,
      "evidence_risk_level": "LOW",
      "safety_signal_types": ["GENERAL"],
      "entities": [],
      "relations": [],
      "metadata": {}
    }
  ]
}
```

`retrieval_status` 固定為 `SUCCESS / EMPTY / PARTIAL / STALE / CONFLICT / ERROR`。
`EMPTY` 與 `ERROR` 必須回 `chunks: []`；`SUCCESS` 至少一筆 chunk。

`evidence_risk_level` 屬於每筆 chunk，固定為 `HIGH / MEDIUM / LOW / UNKNOWN`；
不把多筆證據壓成一個無法溯源的外層風險值。

## 舊流程相容

`retrieval_response_to_rag_result()` 會把 `chunks[]` 經 `normalize_evidence()` 轉成
`CanonicalEvidence[]`，並將 status / route / warnings 透過 `RAGResult` 帶入
`CanonicalBInput.tool_context`。檢索結果仍須 B PASS 才能給 C，並須 D PASS 才能輸出。
