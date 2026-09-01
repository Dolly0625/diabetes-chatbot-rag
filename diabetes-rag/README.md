# rag_retrieval

供糖尿病衛教 LangGraph 管線使用的證據檢索模組。這是一個**Python 套件**，
不是 HTTP 服務——你直接 `import` 它，在自己的行程裡呼叫（已於
2026-08-29 與 LLM 組確認）。

```python
from rag_retrieval import EvidenceRetrievalTool

tool = EvidenceRetrievalTool(source_id="tfda+hpa")
response = tool.retrieve(request)  # dict（或 RetrievalRequest）-> RetrievalResponse
```

請求／回應的格式已凍結於
[`CONTRACT_v1`](../02_MS2_demo/contract/CONTRACT_v1.md)——本套件完全依照
該契約實作；五組完整的請求／回應範例見
[`02_MS2_demo/contract/examples/`](../02_MS2_demo/contract/examples/)。

## 安裝

需要 Python **3.10**（不支援 3.11+）與 Pydantic **v2**。

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -e .
```

設定查詢時 embedding 所需的 Gemini API key（實驗室提供，絕不可提交進版控）：

```bash
export GEMINI_API_KEY=...
```

## 使用方式

```python
from rag_retrieval import EvidenceRetrievalTool

tool = EvidenceRetrievalTool(source_id="tfda+hpa")

request = {
    "request_id": "req_20260903_0001",
    "schema_version": "rag-v1",
    "user_raw_input": "糖尿病平常飲食要注意什麼？",
    "retrieval_queries": ["第二型糖尿病 飲食原則", "糖尿病 均衡飲食"],
    "guardrail_result": {
        "intent_tags": ["GENERAL_EDUCATION"],
        "risk_flags": [],
        "context_modifiers": {
            "time_frame": "CURRENT",
            "target_subject": "SELF",
            "polarity": "AFFIRMATIVE",
            "language": "zh-TW",
        },
        "router_status": "G_GENERAL_EDUCATION",  # 只有這個值可以進入檢索
        "reason_codes": ["MEETS_SAFE_SCOPE"],
    },
    "language": "zh-TW",
    "timestamp": "2026-09-03T14:00:00+08:00",
}

response = tool.retrieve(request)
print(response.retrieval_status)     # SUCCESS / EMPTY / PARTIAL / STALE / CONFLICT / ERROR
print(response.max_evidence_risk_level)
for chunk in response.chunks:
    print(chunk.chunk_id, chunk.evidence_risk_level, chunk.content[:40])
```

`response` 是一個 `rag_retrieval.contract.models.RetrievalResponse`
（Pydantic v2 模型）——若需要純 dict／JSON，用 `.model_dump(mode="json")`。

### 絕不會發生的事

- `retrieve()` 絕不拋出例外。不合法的請求、不支援的 `schema_version`、
  非 `G_GENERAL_EDUCATION` 的 `router_status`，或任何內部例外，都會回傳
  合法的 `RetrievalResponse`，`retrieval_status="ERROR"`、`chunks=[]`——
  絕不會有 traceback，`chunks` 也絕不會是 `null`。
- `evidence_risk_level` 絕不是由 LLM 判斷出來的——它是依三元組的
  `relation` 型別查表得出（`CONTRACT_v1` §2.5）。vector chunk（沒有
  結構化 relation）永遠是 `UNKNOWN`，絕不是 `LOW`。
- 每個真的回傳證據的回應都帶有 `SOURCE_NOT_CLINICALLY_REVIEWED`
  warning。本專案沒有臨床審核人員；這是刻意保留的警示，不是疏漏，
  你的 Context Gate 必須讓它保持可見。

## 內部結構

| 軌道 | 資料 | 做法 |
|---|---|---|
| Vector | TFDA 藥物風險 chunk + 國健署《糖尿病與我》衛教語料 | 對 Gemini embedding（3072 維）做 numpy 餘弦 top-k |
| Graph | 29 筆可檢索的安全三元組（schema v3，10 種關係型別） | 記憶體內 dict，依 label 做 1–2 跳展開 |

兩條軌道永遠都會執行（`routing.py` 出貨的預設值是 `HYBRID`），並以
Reciprocal Rank Fusion 合併（`k=60`，圖譜端加權——這是 `fusion.py` 裡
明文記載的安全決策，不是隨意調整的相關性參數）。

兩條軌道都沒有使用資料庫（沒有 Neo4j，也沒有向量資料庫）——在 85 筆
向量與 29 筆三元組的規模下，用資料庫反而是用錯工具，不是偷工減料。
完整的「刻意還沒做」清單見 `CLAUDE.md` §10。

## 一次性設定：建立衛教語料索引

基礎的 85 筆 TFDA chunk 已預先 embedding，隨套件放在
`src/rag_retrieval/data/`。21 篇國健署《糖尿病與我》衛教語料需要另外
切塊並 embedding：

```bash
export GEMINI_API_KEY=...
python scripts/build_index.py
```

這會寫出 `src/rag_retrieval/data/education_chunks_embedded.json`，
`loaders.load_vector_chunks()` 之後每次載入都會自動合併它。若沒有先跑
過這一步，衛教／飲食類問題就只能（如果有的話）靠 graph 軌道回答——
vector 軌道會完全沒有除了藥物風險文字以外的內容。

（此索引已用實驗室的 GEMINI_API_KEY 建立過一次並提交進版控，2026-08-29；
語料若有更新才需要重跑。）

## 已知限制（完整清單見 CLAUDE.md）

- 風險等級對照表沒有臨床審核人員把關；它是依法規公告的用語強度推導，
  不是臨床判斷。
- `RISK_FACTOR_FOR` 預設為 `MEDIUM`（絕不會因為位於禁忌路徑上而升級成
  `HIGH`），因為目前沒有任何可檢索的 `CONTRAINDICATED_FOR` 三元組可供
  升級判斷。
- Boundary B 負責的圖譜三元組真實信心門檻，上游一直沒有校準完成；
  `gate_out.py` 出貨的是文件明訂的寬鬆預設值（`0.5`）。
- `negation_checked` 沒有被當成通過／擋下的門檻執行——目前每一筆三元組
  這個欄位都是 `False`（上游的否定詞檢查其實還沒真的跑過），若強制執行
  會把本套件存在的目的——CAUTION_FOR／INDUCES 證據——直接清空。這個
  欄位仍會透過 `relations[]` 往下傳，讓你自己的 Context Gate 看得到。

## 開發

```bash
pip install -e ".[dev]"
pytest
```

`src/rag_retrieval/` 裡的每個模組都可以獨立測試；對應 `CLAUDE.md` §6
每一個建置步驟的驗收測試都在 `tests/` 裡。

## Module status

有六個元件是先用預設實作出貨，之後由其他組員透過 merge request 替換。
`MODULE_STATUS.md` 記錄了哪些還在跑預設值、每個預設值的代價是什麼；
`python eval/run_eval.py` 也會在印出指標的同時印出同一份摘要。任務說明
放在 `../02_MS2_demo/delegation/`。
