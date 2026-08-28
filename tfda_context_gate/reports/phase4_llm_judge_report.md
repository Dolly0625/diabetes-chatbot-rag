# TFDA Context Gate Phase 4：LLM Judge 實驗報告

## 先用白話說這一階段在測什麼

Phase 3 的 Reranker 已經把比較相關的資料排到前面，可是它還是把 Fournier’s gangrene 排第二。

這不是因為那篇資料完全沒關係。它確實也是 SGLT2 的安全資訊，只是使用者現在問的不是 Fournier’s gangrene，而是酮酸中毒。

所以這一階段真正想測的是：

> LLM Judge 能不能看懂「同一種藥，但使用者問的是不同風險」，並把真正能回答 Query 的 Context 和只是相關的 Context 分開？

本階段只做 LLM Judge，沒有修改 Retrieval 或 Reranker，也沒有開始 Hybrid、Agent 或最終整合報告。

## 一、實驗輸入固定，不重新 Retrieval 或 Rerank

本次直接讀取 Phase 3 已經產生的：

`results/narrow_query_reranked_top10.json`

所以輸入流程是：

```text
Phase 3 Reranked Top-10
        ↓
Document-level LLM Judge
        ↓
Top-4 Context Set-level LLM Judge
```

Phase 3 JSON 保存了排名、score 和文件 ID；程式再依照這些固定的 `document_id`，從 Phase 2 的 `langchain_documents.json` 取回完整 `page_content` 給 Judge 閱讀。這不是重新 Retrieval，也不是重新 Rerank，只是把原本被截短的 preview 還原成同一份完整文件內容。

Query 固定為：

> `TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？`

## 二、使用模型與 Structured Output

### 模型與 endpoint

- Model：`deepseek-v4-flash`
- Endpoint：OpenCode Go 的 OpenAI-compatible Chat Completions endpoint
- 程式中的 normalized endpoint root：`https://opencode.ai/zen/go/v1`
- Temperature：`0`
- LangChain package：`langchain-openai`
- LangChain version：`1.3.15`
- `langchain-openai` version：`1.5.1`

API key 只從專案 `.env` 讀取，沒有寫入任何結果檔。`.env` 原本使用完整 `/chat/completions` URL，程式會在交給 `ChatOpenAI` 前轉成 OpenAI client 使用的 API root，避免重複接上 `/chat/completions`。

LangChain 官方的 `ChatOpenAI` 文件支援用 `with_structured_output()` 取得 Pydantic 結果；本次使用 function-calling 方式，因為 OpenCode Go 是 OpenAI-compatible endpoint，這種方式比較適合目前環境。參考：[LangChain ChatOpenAI 文件](https://docs.langchain.com/oss/python/integrations/chat/openai)。

### Document-level schema

```python
class DocumentAssessment(BaseModel):
    document_id: str
    relevance: Literal["DIRECT", "PARTIAL", "IRRELEVANT"]
    sufficiency: Literal["SUFFICIENT", "INSUFFICIENT"]
    topic_match: Literal[
        "EXACT",
        "SAME_DRUG_DIFFERENT_RISK",
        "OTHER",
    ]
    reason_code: str
```

### Set-level schema

```python
class ContextSetAssessment(BaseModel):
    sufficient_for_answer: bool
    usable_document_ids: list[str]
    excluded_document_ids: list[str]
    decision: Literal["PASS", "REVIEW", "FALLBACK"]
    reason_codes: list[str]
```

System Prompt 明確要求 Judge 不回答醫療問題，只評估 Context 是否能支援目前 Query；也沒有要求 Chain-of-Thought，`reason_code` 只保留短代碼。

人工標註沒有放進 Prompt。Judge 先獨立判斷，完成後才和 Phase 2／Phase 3 的人工 label 比較。

## 三、Document-level Top-10：Human vs Judge

Phase 3 Reranker Top-10 的人工 label 和 Judge 結果如下：

| Reranker Rank | Document | Human Label | Judge Relevance | Match | Sufficiency | Topic Match | Reason Code |
|---:|---|---|---|---|---|---|---|
| 1 | `tfda-risk-0019` | DIRECT | DIRECT | Yes | SUFFICIENT | EXACT | `EXACT_KETOACIDOSIS_EVIDENCE` |
| 2 | `tfda-risk-0064` | PARTIAL | PARTIAL | Yes | INSUFFICIENT | SAME_DRUG_DIFFERENT_RISK | `SAME_DRUG_DIFFERENT_SAFETY_TOPIC` |
| 3 | `tfda-risk-0042` | PARTIAL | PARTIAL | Yes | INSUFFICIENT | SAME_DRUG_DIFFERENT_RISK | `SAME_DRUG_DIFFERENT_SAFETY_TOPIC` |
| 4 | `tfda-risk-0035` | PARTIAL | PARTIAL | Yes | INSUFFICIENT | SAME_DRUG_DIFFERENT_RISK | `SAME_DRUG_DIFFERENT_SAFETY_TOPIC` |
| 5 | `tfda-risk-0015` | IRRELEVANT | IRRELEVANT | Yes | INSUFFICIENT | OTHER | `UNRELATED_DRUG_OR_TOPIC` |
| 6 | `tfda-risk-0102` | IRRELEVANT | IRRELEVANT | Yes | INSUFFICIENT | OTHER | `UNRELATED_DRUG_OR_TOPIC` |
| 7 | `tfda-risk-0068` | IRRELEVANT | IRRELEVANT | Yes | INSUFFICIENT | OTHER | `UNRELATED_DRUG_OR_TOPIC` |
| 8 | `tfda-risk-0112` | IRRELEVANT | IRRELEVANT | Yes | INSUFFICIENT | OTHER | `UNRELATED_DRUG_OR_TOPIC` |
| 9 | `tfda-risk-0020` | IRRELEVANT | IRRELEVANT | Yes | INSUFFICIENT | OTHER | `UNRELATED_DRUG_OR_TOPIC` |
| 10 | `tfda-risk-0053` | IRRELEVANT | IRRELEVANT | Yes | INSUFFICIENT | OTHER | `UNRELATED_DRUG_OR_TOPIC` |

### Accuracy

Reference run 的結果是：

```text
10 / 10 = 1.00
```

但這只能解讀成：在一個 Query、10 份文件的示範中，Judge 和人工標註完全一致。這不是正式 benchmark，也不能推論到所有 Query 或所有醫療資料。

## 四、Confusion Matrix

列是人工標註，欄是 Judge 判斷：

| Human \ Judge | DIRECT | PARTIAL | IRRELEVANT |
|---|---:|---:|---:|
| DIRECT | 1 | 0 | 0 |
| PARTIAL | 0 | 3 | 0 |
| IRRELEVANT | 0 | 0 | 6 |

這次沒有出現誤判，所以矩陣剛好是對角線結果。但樣本只有 10 筆，不能把這個結果說成模型具有一般化醫療判斷能力。

## 五、四篇 SGLT2 文件的重點判斷

這是本階段最重要的比較：

| Reranker Rank | Document | 安全主題 | Human Label | Judge Relevance | Judge Sufficiency | Judge Topic Match | Reason Code |
|---:|---|---|---|---|---|---|---|
| 1 | `tfda-risk-0019` | 酮酸中毒 | DIRECT | DIRECT | SUFFICIENT | EXACT | `EXACT_KETOACIDOSIS_EVIDENCE` |
| 2 | `tfda-risk-0064` | Fournier’s gangrene | PARTIAL | PARTIAL | INSUFFICIENT | SAME_DRUG_DIFFERENT_RISK | `SAME_DRUG_DIFFERENT_SAFETY_TOPIC` |
| 3 | `tfda-risk-0042` | 下肢截肢 | PARTIAL | PARTIAL | INSUFFICIENT | SAME_DRUG_DIFFERENT_RISK | `SAME_DRUG_DIFFERENT_SAFETY_TOPIC` |
| 4 | `tfda-risk-0035` | 急性腎損傷 | PARTIAL | PARTIAL | INSUFFICIENT | SAME_DRUG_DIFFERENT_RISK | `SAME_DRUG_DIFFERENT_SAFETY_TOPIC` |

這正好回答了 Phase 4 的核心問題：

- Fournier’s gangrene 雖然排在 Reranker 第 2 名，但 Judge 沒有把它誤判成 DIRECT。
- 截肢雖然也是同一類藥品，也被判成 PARTIAL，而不是 DIRECT。
- 急性腎損傷同樣被判成 PARTIAL。
- 只有真的談酮酸中毒的 `tfda-risk-0019` 被判成 `DIRECT / SUFFICIENT / EXACT`。

也就是說，LLM Judge 補上了 Reranker 沒有完成的那一層語意判斷：

> 「同一個藥品類別」不等於「同一個安全問題」。

## 六、Set-level Judge：Top-4 Context

Top-4 是：

1. `tfda-risk-0019`：酮酸中毒
2. `tfda-risk-0064`：Fournier’s gangrene
3. `tfda-risk-0042`：下肢截肢
4. `tfda-risk-0035`：急性腎損傷

### 有正確 Context 的 Set

輸入包含四篇文件，Judge 三次都輸出：

```json
{
  "sufficient_for_answer": true,
  "usable_document_ids": ["tfda-risk-0019"],
  "excluded_document_ids": [
    "tfda-risk-0064",
    "tfda-risk-0042",
    "tfda-risk-0035"
  ],
  "decision": "PASS",
  "reason_codes": [
    "HAS_EXACT_KETOACIDOSIS_EVIDENCE",
    "DIFFERENT_TOPICS_NOT_CONFLICT",
    "EXCLUDES_PARTIAL_OR_UNRELATED_CONTEXT"
  ]
}
```

口語化解讀：這批資料裡面有一篇真的回答酮酸中毒，所以可以 PASS；但交給 Generator 的 usable context 主要保留 `tfda-risk-0019`，其他三篇雖然是同類藥品，卻不是這次 Query 的直接證據。

### 沒有正確 Context 的 Set

這組故意只放：

- `tfda-risk-0064`：Fournier’s gangrene
- `tfda-risk-0042`：下肢截肢
- `tfda-risk-0035`：急性腎損傷

三次結果完全一致：

```json
{
  "sufficient_for_answer": false,
  "usable_document_ids": [],
  "excluded_document_ids": [
    "tfda-risk-0064",
    "tfda-risk-0042",
    "tfda-risk-0035"
  ],
  "decision": "FALLBACK",
  "reason_codes": [
    "INSUFFICIENT_EVIDENCE_FOR_QUERY",
    "ONLY_DIFFERENT_SGLT2_SAFETY_TOPICS"
  ]
}
```

這個結果非常重要：即使三篇文件全部都和 SGLT2 有關，Judge 仍然沒有因為「同類藥品」就硬湊成答案，而是判定缺少酮酸中毒證據，要求 FALLBACK。

## 七、沒有把不同主題誤寫成 Conflict

本次沒有使用 `conflict` 欄位，也沒有把不同安全主題當成互相矛盾。

Fournier’s gangrene、截肢、急性腎損傷和酮酸中毒是不同 safety topics。它們目前沒有被本實驗證明互相衝突，所以 Judge 使用：

```text
DIFFERENT_TOPICS_NOT_CONFLICT
```

這個區分很重要：

- 不同主題：代表文件可能和 Query 有關，但不是同一問題。
- Conflict：代表兩份資料對同一個命題給出互相矛盾的資訊。

本階段只測 Relevance、Sufficiency 和 Topic Match，沒有測 Conflict。

## 八、三次執行穩定性

Document-level 每輪 10 篇，Set-level 每輪 3 組，總共 3 輪。

三次結果完全一致：

- 10 篇文件的 `relevance`、`sufficiency`、`topic_match`、`reason_code` 全部一致。
- `top4` Set 三次都是 `PASS`。
- `without_correct_context` 三次都是 `FALLBACK`。
- `with_correct_context` 三次都是 `PASS`。

Temperature 設為 0，但實驗仍然實際重跑三次，而不是假設一定 deterministic。這次在這個小樣本上觀察到 100% 一致；仍不能說在更長 Context、更複雜 Query 或不同 provider 狀態下也一定一致。

## 九、Latency 與 Token 使用量

三輪實際測得：

| 類型 | Mean | Median | Min | Max |
|---|---:|---:|---:|---:|
| 每輪 10 篇 Document-level 總耗時 | 65.93 秒 | 76.34 秒 | 44.23 秒 | 77.22 秒 |
| 每輪 3 組 Set-level 總耗時 | 27.18 秒 | 22.37 秒 | 20.66 秒 | 38.50 秒 |
| 每輪全部 13 次呼叫 | 93.11 秒 | 98.71 秒 | 64.89 秒 | 115.72 秒 |

Provider 有回傳 token usage，三輪、共 39 次呼叫合計：

```text
input tokens：111,378
output tokens：11,865
total tokens：123,243
```

這裡的 latency 是實際 API 呼叫時間，不包含人工閱讀或報告整理時間。

## 十、這一階段有沒有意外誤判

在這次 1 個 Query、10 篇文件的 Document-level 評估中，沒有發現意外誤判：

- 沒有把 Fournier’s gangrene 判成 DIRECT。
- 沒有把截肢判成 DIRECT。
- 沒有把急性腎損傷判成 DIRECT。
- 沒有把完全不同藥品的文件判成 PARTIAL 或 DIRECT。
- 沒有把只有錯安全主題的 Set 判成 PASS。
- 沒有把不同安全主題誤判成 Conflict。

但這只能說明目前這個示範案例的 Prompt 和模型表現符合預期，不能宣稱 Judge 已經被正式驗證。

## 十一、Phase 4 的結論

Phase 3 的 Reranker 解決的是：

> 哪些文件在排序上比較像目前的 Query？

Phase 4 的 LLM Judge 多做了一層：

> 這篇文件到底能不能支撐目前 Query？它是同一主題，還是只是同一種藥的另一個風險？

這次實驗中，LLM Judge 確實補上了 Reranker 的不足：

- 把酮酸中毒文件判成 `DIRECT / SUFFICIENT / EXACT`。
- 把 Fournier’s gangrene、截肢、急性腎損傷判成 `PARTIAL / INSUFFICIENT / SAME_DRUG_DIFFERENT_RISK`。
- 在有正確 Context 時輸出 `PASS`，並只保留酮酸中毒文件作為 usable context。
- 在只有錯安全主題時輸出 `FALLBACK`，沒有硬湊答案。

因此，LLM Judge 比較適合扮演「Context 是否能使用」的判斷層；但本階段仍然沒有處理 Hybrid Gate，也沒有測試真實回答生成或 Conflict。

## 十二、產出檔案

```text
tfda_context_gate/
├── 04_llm_judge.py
├── prompts/
│   ├── document_judge_v1.txt
│   └── set_judge_v1.txt
├── results/
│   ├── document_judge_results.json
│   ├── human_vs_judge.csv
│   ├── judge_confusion_matrix.csv
│   ├── set_without_correct_context.json
│   ├── set_with_correct_context.json
│   ├── phase4_latency.json
│   └── phase4_llm_judge_output.txt
└── reports/
    └── phase4_llm_judge_report.md
```

本階段到此停止，沒有開始 Hybrid。
