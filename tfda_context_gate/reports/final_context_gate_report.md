# B：Contract Gate + Context Gate 技術調查

## RAG 找回來的資料，到底能不能直接交給 LLM？

## 先講結論

這份實驗想處理的問題很簡單：RAG 已經找回資料之後，LLM 端要怎麼判斷這些 Context 能不能使用？

這次沒有假設 RAG 一定能被 LLM 系統完全控制，也沒有試圖證明 Retriever 永遠不會找錯。我們把問題拆成兩層：

```text
RAG 找回文件
    ↓
Contract Gate：格式和基本欄位有沒有問題？
    ↓
Context Gate：內容真的能不能回答目前 Query？
    ↓
Generator：最後才產生答案
```

實驗結果可以用一句話說完：

> Similarity 和 Reranker 負責把候選文件找出來、排前面；LLM Judge 才開始判斷「這些文件是不是在回答目前這一題」；Hybrid 則把這件事做成只需要一次 Set-level Judge 的流程。

在這次小型 TFDA 實驗裡，最後暫時建議的 MVP 流程是：

```text
Contract Gate
    ↓
Similarity Retriever Top-20
    ↓
Reranker Top-4
    ↓
一次 Set-level LLM Judge
    ↓
PASS / FALLBACK / REVIEW
```

這是目前實驗下的暫時建議，不是正式醫療產品的最終架構。

---

## 1. 這次到底在研究什麼

如果把整個 RAG 想成一個資料助理，它通常會先從資料庫找出幾份看起來相關的文件，再把文件交給 LLM。

問題是，「看起來相關」不一定代表「可以直接拿來回答」。

例如使用者問的是：

> SGLT2 抑制劑的酮酸中毒風險。

Retriever 可能找回：

- SGLT2 的酮酸中毒
- SGLT2 的下肢截肢
- SGLT2 的 Fournier’s gangrene
- SGLT2 的急性腎損傷

這些文件全部都和 SGLT2 以及藥品安全有關，但只有第一篇直接回答目前問題。

所以本次 B 模組不是在研究「怎麼讓 RAG 永遠找對」，而是在研究：

> RAG 找回來之後，怎麼避免 LLM 直接使用不適合的 Context？

---

## 2. 資料來源：129 筆 TFDA 真實風險溝通資料

### 2.1 這份資料是什麼

本次使用的是衛生福利部食品藥物管理署（TFDA）的「藥品安全資訊風險溝通資料」。

- 政府資料開放平台：<https://data.gov.tw/dataset/9573>
- 實際 JSON endpoint：`https://data.fda.gov.tw/data/opendata/export/53/json`
- 本次完整 corpus：129 筆

這裡要先釐清一件事：一筆資料不是一個藥品，也不是一張藥品許可證。

一筆比較像是：

> TFDA 在某個時間，針對某個藥品成分或藥品類別，發布的一次安全風險溝通資訊。

所以同一個藥品類別可以出現好幾筆，而且每筆可能在講不同安全主題。

這次沒有使用人工編的 Med-X toy dataset。SGLT2 的四個安全主題，是真實存在於完整 TFDA corpus 裡的文件：

| 文件 | 發布日期 | 主題 |
|---|---:|---|
| `tfda-risk-0019` | 2015/6/25 | 酮酸中毒 |
| `tfda-risk-0042` | 2017/3/22 | 下肢截肢 |
| `tfda-risk-0064` | 2018/9/28 | Fournier’s gangrene／會陰部壞死性筋膜炎 |
| `tfda-risk-0035` | 2016/7/14 | 急性腎損傷 |

Retriever 每次都是從完整 129 筆裡面自己找，不是先人工挑出這四篇。

### 2.2 為什麼選 SGLT2

我故意選 SGLT2，是因為資料庫裡同一類藥有好幾筆不同安全資訊。

這會製造一個很真實的 Retrieval 問題：使用者明明問「酮酸中毒」，但 Retriever 很容易一起找到「截肢」、「Fournier’s gangrene」和「急性腎損傷」，因為它們都在講 SGLT2 加上藥品安全。

這正好可以測試 Context Gate 到底能不能分辨「同一類藥品」和「同一個安全主題」。

### 2.3 兩個 Query

窄 Query：

> `TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？`

這題只想找酮酸中毒。

廣 Query：

> `TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？`

這題則希望酮酸中毒、截肢、Fournier’s gangrene 和急性腎損傷都可以被視為有用 Evidence。

這裡是整份實驗的一個核心觀察：

> 同一篇文件是不是 Relevant，不是文件自己固定的屬性，而是跟使用者問什麼有關。

---

## 3. Contract Gate：先檢查資料能不能進流程

### 3.1 白話說明

Contract Gate 比較像收資料時先檢查包裹有沒有基本資訊。

它會確認：

- `document_id` 存不存在，而且不能重複
- `row_index` 存不存在
- `發布日期` 是否有值
- `藥品成分` 是否有值
- `page_content` 是否有正文

本次結果是：

```text
Corpus：129
Contract passed：129
Contract rejected：0
```

但 Contract Gate 不會自己知道：

- 資料是不是最新
- 藥證是不是已經註銷
- 文件是不是被新版取代

因為 TFDA 主資料本身沒有提供這些欄位。這次沒有把 `status=superseded` 之類的欄位硬加進來。

### 3.2 核心 LangChain 程式

完整版本請看 [`01_build_documents.py`](../01_build_documents.py) 和 [`05_hybrid.py`](../05_hybrid.py)。核心概念如下：

```python
documents = build_documents(tfda_records)
seen = set()
passed = []

for doc in documents:
    reasons = []
    doc_id = doc.metadata.get("document_id")
    if not doc_id or doc_id in seen:
        reasons.append("invalid_document_id")
    if doc.metadata.get("row_index") is None:
        reasons.append("missing_row_index")
    if not str(doc.metadata.get("發布日期", "")).strip():
        reasons.append("empty_發布日期")
    if not str(doc.metadata.get("藥品成分", "")).strip():
        reasons.append("empty_藥品成分")
    if not doc.page_content.strip():
        reasons.append("empty_page_content")
    if not reasons:
        passed.append(doc)
```

### 3.3 這一層的優缺點

優點是規則清楚、速度快、結果容易追蹤。格式錯誤可以在進入向量搜尋前就被擋下來。

缺點是它只懂欄位，不懂語意。它不會知道一篇文件是不是在回答酮酸中毒，也不會知道兩篇文件是不是互相衝突。

---

## 4. Similarity：先找「看起來像」的文件

### 4.1 白話說明

Similarity 可以想成先問：

> 這個 Query 跟哪幾篇文件在語意上比較像？

本次使用 `HuggingFaceEmbeddings`，模型是 `intfloat/multilingual-e5-small`，再把 129 筆文件放進 LangChain `InMemoryVectorStore`。

### 4.2 核心 LangChain 程式

完整版本請看 [`02_similarity_retrieval.py`](../02_similarity_retrieval.py)。

```python
embeddings = HuggingFaceEmbeddings(
    model_name="intfloat/multilingual-e5-small",
    encode_kwargs={"normalize_embeddings": True, "prompt": "passage: "},
    query_encode_kwargs={"normalize_embeddings": True, "prompt": "query: "},
)
store = InMemoryVectorStore(embedding=embeddings)
store.add_documents(contract_passed_documents)

results = store.similarity_search_with_score(query, k=10)
for rank, (doc, score) in enumerate(results, 1):
    print(rank, doc.metadata["document_id"], score)
```

### 4.3 真實結果

Narrow Query 的 Top-10 是：

- Directly Relevant：1 筆
- Partially Relevant：3 筆
- Irrelevant：6 筆

酮酸中毒文件是 Rank 1，但截肢和 Fournier’s gangrene 也排在 Rank 2、Rank 3，而且分數很高。

Broad Query 則把四篇 SGLT2 相關安全資訊放在前四名，這符合廣 Query 的需求。

### 4.4 結果代表什麼

Similarity 知道這些文件都在講 SGLT2，也知道它們都屬於藥品安全資訊；但它不一定分得清楚使用者現在問的是哪一種安全風險。

所以 Similarity 的高分不能直接解讀成「這篇可以交給 Generator」。Similarity score 是模型的相似度分數，不是正確率，也不是可以使用的機率。

### 4.5 優缺點

優點是便宜、快，適合先從完整資料庫縮小候選範圍。

缺點是容易把同一類藥品、不同安全主題的文件一起找回來。它適合當第一層 Retriever，不適合單獨當 Context Gate。

---

## 5. Reranker：再仔細排一次順序

### 5.1 白話說明

既然 Similarity 有點粗，就先用 Retriever 找一批候選，再讓 Reranker 把 Query 和每篇 Candidate 直接配對比較一次。

本次先取 Similarity Top-20，再使用 `BAAI/bge-reranker-v2-m3` 重排成 Top-10。

### 5.2 核心 LangChain 程式

完整版本請看 [`03_reranker.py`](../03_reranker.py)。

```python
candidate_pairs = [
    (query, doc.page_content)
    for doc in candidate_documents
]
scores = cross_encoder.score(candidate_pairs)

ranked = sorted(
    zip(candidate_documents, scores, strict=True),
    key=lambda item: item[1],
    reverse=True,
)

reranked_top10 = ranked[:10]
```

實驗使用的 LangChain integration 是 `HuggingFaceCrossEncoder`。目前環境會出現 `langchain-community` sunset deprecation warning；本次仍依照目前官方文件可用的 integration 完成實驗，正式維護版本未來需要遷移。

### 5.3 窄 Query 的真實結果

| 文件 | Similarity | Reranker |
|---|---:|---:|
| 酮酸中毒 | Rank 1 | Rank 1 |
| Fournier’s gangrene | Rank 3 | Rank 2 |
| 下肢截肢 | Rank 2 | Rank 3 |
| 急性腎損傷 | Rank 6 | Rank 4 |

Fournier’s gangrene 甚至升到 Rank 2，但這不代表 Reranker 完全失敗。它確實是「SGLT2 加上安全風險」的文件，只是它不是使用者目前問的「酮酸中毒」。

### 5.4 結果代表什麼

Reranker 比較適合改善 Query-document ranking，但：

> 排得很前面，不等於這篇已經能直接回答問題。

它沒有判斷 sufficiency，也沒有決定哪些文件可以交給 Generator。

### 5.5 優缺點

優點是比單純 embedding 相似度更細，能把相關文件重新排序。

缺點是每篇 Candidate 都要額外做一次推論，成本和 CPU latency 都增加；而且「同藥不同風險」仍可能拿到很高分。

---

## 6. LLM Judge：判斷 Context 到底能不能用

### 6.1 白話說明

前兩層主要在找文件和排順序，現在還缺一個能力：真的看懂文件是不是在回答這一題。

LLM Judge 的角色不是回答醫療問題，而是評估 Context。

Document-level 使用三組判斷：

- `DIRECT`、`PARTIAL`、`IRRELEVANT`
- `SUFFICIENT`、`INSUFFICIENT`
- `EXACT`、`SAME_DRUG_DIFFERENT_RISK`、`OTHER`

### 6.2 核心 LangChain 程式

完整版本請看 [`04_llm_judge.py`](../04_llm_judge.py)。

```python
class DocumentAssessment(BaseModel):
    document_id: str
    relevance: Literal["DIRECT", "PARTIAL", "IRRELEVANT"]
    sufficiency: Literal["SUFFICIENT", "INSUFFICIENT"]
    topic_match: Literal[
        "EXACT", "SAME_DRUG_DIFFERENT_RISK", "OTHER"
    ]
    reason_code: str

llm = ChatOpenAI(
    model="deepseek-v4-flash",
    temperature=0,
    base_url=normalized_base_url,
)
judge = llm.with_structured_output(
    DocumentAssessment,
    method="function_calling",
)
assessment = judge.invoke([system_message, user_message])
```

### 6.3 真實結果

10 篇文件的結果是：

- Direct：1
- Partial：3
- Irrelevant：6
- Judge vs Human：10/10 一致

四篇 SGLT2 文件是整份報告最重要的對照：

| 安全主題 | Judge 結果 |
|---|---|
| 酮酸中毒 | `DIRECT / SUFFICIENT / EXACT` |
| Fournier’s gangrene | `PARTIAL / INSUFFICIENT / SAME_DRUG_DIFFERENT_RISK` |
| 下肢截肢 | `PARTIAL / INSUFFICIENT / SAME_DRUG_DIFFERENT_RISK` |
| 急性腎損傷 | `PARTIAL / INSUFFICIENT / SAME_DRUG_DIFFERENT_RISK` |

這代表 Judge 確實補上了 Reranker 做不到的那一層：同一個藥品類別，不等於同一個安全問題。

但這只有一個 Query、10 篇文件的小型測試，不能解讀成 Judge 普遍準確率就是 100%。

### 6.4 Fallback 實驗

Set A 只有 Fournier’s gangrene、截肢、急性腎損傷，沒有酮酸中毒：

```text
usable_document_ids = []
decision = FALLBACK
```

Set B 加入真正的酮酸中毒文件：

```text
decision = PASS
usable_document_ids = ["tfda-risk-0019"]
```

用人話說：即使 Retriever 找到很多 SGLT2 文件，只要沒有真的回答酮酸中毒，Judge 不應該硬讓 Generator 回答。

### 6.5 成本與限制

Phase 4 Standalone Judge 一輪有 13 次 LLM call：10 次 Document-level 加上 3 次 Set-level。

- Document-level 平均：65.93 秒
- Set-level 平均：27.18 秒
- 整輪平均：93.11 秒
- 平均約：41,081 tokens

它的判斷能力有用，但如果每篇文件都要叫一次 LLM，成本很快就會變高。這就是下一階段需要 Hybrid 的原因。

---

## 7. Hybrid：前面先縮小，最後只判一次

### 7.1 白話說明

Hybrid 想解決的是：既然 Judge 很會判，但是很慢，那能不能前面先用便宜的方法把資料縮小，最後只 Call 一次 Judge？

本次實際測試三種 Variant：

```text
Similarity Top-20
    ↓
Reranker Top-3 / Top-4 / Top-5
    ↓
一次 Set-level LLM Judge
```

完整版本請看 [`05_hybrid.py`](../05_hybrid.py)。

### 7.2 核心 LangChain 程式

```python
contract = run_contract_gate(documents)
retrieval = run_retrieval(store, query, raw_by_id)
reranked = run_reranker(
    retrieval, cross_encoder, query, raw_by_id
)

selected = reranked["rows"][:top_n]
decision = run_context_judge(
    query=query,
    rows=selected,
    judge_chain=judge_chain,
)

return {
    "usable_document_ids": decision.usable_document_ids,
    "decision": decision.decision,
}
```

### 7.3 Narrow Query 結果

| Variant | Decision | Usable | Precision | Recall |
|---|---|---|---:|---:|
| Top-3 | PASS | `tfda-risk-0019` | 1.00 | 1.00 |
| Top-4 | PASS | `tfda-risk-0019` | 1.00 | 1.00 |
| Top-5 | PASS | `tfda-risk-0019` | 1.00 | 1.00 |

三種 Top-N 的三次結果都一致。Fournier’s gangrene、截肢、急性腎損傷沒有被當成 Narrow Query 的 usable evidence。

### 7.4 Broad Query 結果

| Variant | Decision | Usable | Precision | Recall |
|---|---|---|---:|---:|
| Top-3 | PASS | Fournier’s gangrene、酮酸中毒、截肢 | 1.00 | 0.75 |
| Top-4 | PASS | 上述三篇 + 急性腎損傷 | 1.00 | 1.00 |
| Top-5 | PASS | 上述四篇 | 1.00 | 1.00 |

Broad Query 的 Direct 文件共有四篇。Top-3 漏掉急性腎損傷，所以 Recall 是 0.75；Top-4 才完整保留四篇。Top-5 沒有增加 Recall，只是多放進一篇最後被排除的 PPI 文件。

這個結果也證明：Judge 不是永遠只保留酮酸中毒。它會依照 Query 改變 usable documents。

### 7.5 Fallback ablation

錯安全主題 ablation 三次都是：

```text
usable_document_ids = []
decision = FALLBACK
```

這不是 Conflict。Fournier’s gangrene、截肢、急性腎損傷只是不同安全主題，不是和酮酸中毒互相矛盾。

---

## 8. 成本與 latency：Hybrid 有改善，但還不是 real-time

| 方法 | LLM Calls | 平均 End-to-end | 平均 Tokens |
|---|---:|---:|---:|
| Standalone Judge（Phase 4） | 13 | 93.11 秒 | 41,081 |
| Hybrid Narrow Top-3 | 1 | 40.80 秒 | 4,867 |
| Hybrid Narrow Top-4 | 1 | 52.57 秒 | 6,404 |
| Hybrid Narrow Top-5 | 1 | 45.63 秒 | 7,556 |
| Hybrid Broad Top-3 | 1 | 43.71 秒 | 4,940 |
| Hybrid Broad Top-4 | 1 | 46.59 秒 | 6,399 |
| Hybrid Broad Top-5 | 1 | 48.16 秒 | 7,327 |

以 Narrow Query 為例，LLM call 從 13 次降到 1 次，減少約 92.3%。但 Hybrid 還是需要約 40～50 秒，因為這裡的 end-to-end 已經把 Similarity Retrieval、CPU Reranker 和一次 Judge 都算進去。

所以不能把這次結果直接說成 real-time production solution。

也不能宣稱 Top-3 一定比 Top-4 快。API latency 會波動；這次 Top-3 的平均較低，但每個 Variant 的實際時間仍受 endpoint 狀態影響。

---

## 9. 四種方法放在一起比較

| 方法 | 它主要回答什麼問題 | 這次看到的能力 | 主要限制 |
|---|---|---|---|
| Similarity | 問題和文件像不像？ | 能從 129 筆中找回 SGLT2 相關文件 | 同類藥品不同風險容易混在一起 |
| Reranker | 哪些候選比較值得排前面？ | 讓相關文件排序更集中 | 排前面不代表能直接回答 |
| LLM Judge | 這些資料真的能不能回答？ | 能分辨 EXACT 和 SAME_DRUG_DIFFERENT_RISK | 逐篇判斷成本高、速度慢 |
| Hybrid | 能不能保留判斷能力又少叫幾次 LLM？ | 先縮小，再一次 Set-level 判斷 | 仍有數十秒 latency，需更多 Query 驗證 |

---

## 10. MVP 建議

目前比較合理的 MVP 流程是：

```text
Contract Gate
    ↓
Similarity Retriever Top-20
    ↓
Reranker Top-4
    ↓
一次 Set-level LLM Judge
    ↓
PASS / FALLBACK / REVIEW
```

為什麼選 Top-4？不是因為 Top-4 在所有情況都最好，而是因為在這次小型測試裡：

- Narrow Top-3 已經夠用。
- Broad Top-3 只保留 3/4 個 Direct evidence。
- Broad Top-4 能完整保留四個 SGLT2 安全主題。
- Top-5 沒有增加 Recall，只增加 Context 和 token。

如果未來允許依 Query 類型調整，則可以考慮 Narrow Top-3、Broad Top-4。但目前固定 Top-4 是比較容易落地的折衷。

這只是目前實驗下的暫時建議，不是正式醫療產品的最終架構。

---

## 11. 實驗限制

這些限制需要直接說清楚：

1. TFDA corpus 只有 129 筆。
2. 目前主要深入測試的是 SGLT2 這組 Query。
3. Judge 10/10 一致不能代表普遍 100% accuracy。
4. 沒有正式測 inter-context conflict。
5. Human ground truth 樣本很小，是小型人工 reference label。
6. Hybrid latency 仍然在數十秒。
7. Reranker integration 出現 `langchain-community` sunset warning。
8. 還沒有測真正 Generator 的最後回答品質。
9. 還沒有做大型 benchmark。

另外，這次的 Recall 分母是各 Query 的 Phase 3 Reranker Top-10 人工 Direct 文件，不是整個 TFDA corpus 的完整醫學 ground truth。因此這些 Precision／Recall 是小型示範指標，不應包裝成正式評測結果。

---

## 12. Future Work

後續可以增加更多 TFDA Query、建立正式 Ground Truth、測更多藥物與安全主題、加入真正的 Conflict case，以及改善 Judge latency。

如果未來改成 Agent，Context Gate 判定 `FALLBACK` 後，Agent 可以再重新搜尋或改寫 Query；這裡只把它列為一句 Future Work，不延伸成新的架構實驗。

---

## 附錄 A：完整程式與結果檔

正文只放核心程式；完整程式保留在專案中：

- [`00_download_and_inspect.py`](../00_download_and_inspect.py)
- [`01_build_documents.py`](../01_build_documents.py)
- [`02_similarity_retrieval.py`](../02_similarity_retrieval.py)
- [`03_reranker.py`](../03_reranker.py)
- [`04_llm_judge.py`](../04_llm_judge.py)
- [`05_hybrid.py`](../05_hybrid.py)

主要結果：

- [`phase2_retrieval_report.md`](./phase2_retrieval_report.md)
- [`phase3_reranker_report.md`](./phase3_reranker_report.md)
- [`phase4_llm_judge_report.md`](./phase4_llm_judge_report.md)
- [`phase5_hybrid_report.md`](./phase5_hybrid_report.md)
- [`final_experiment_summary.csv`](./final_experiment_summary.csv)

---

## 附錄 B：官方與資料來源

- TFDA 藥品安全資訊風險溝通資料：<https://data.gov.tw/dataset/9573>
- LangChain Cross-Encoder Reranker：<https://docs.langchain.com/oss/python/integrations/document_transformers/cross_encoder_reranker>
- LangChain ChatOpenAI：<https://docs.langchain.com/oss/python/integrations/chat/openai>
