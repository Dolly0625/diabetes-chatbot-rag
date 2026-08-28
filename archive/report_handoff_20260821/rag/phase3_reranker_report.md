# TFDA Context Gate Phase 3：Reranker 實驗報告

## 先講這一階段做了什麼

Phase 2 已經用 Similarity Retrieval 從 129 筆 TFDA 風險溝通資料中找出 Top-10。結果顯示，Retriever 找得到 SGLT2 相關文件，但同一類藥品的不同安全主題會混在一起。

所以這一階段只加入一個元件：Cross-Encoder Reranker。

流程固定為：

```text
129 筆 TFDA LangChain Documents
        ↓
Contract Gate：129 通過
        ↓
Similarity Retriever：先取 Top-20 candidate
        ↓
Cross-Encoder Reranker：重新閱讀 Query + 文件配對
        ↓
輸出 Reranked Top-10
```

這一階段沒有加入 LLM Judge、Hybrid Gate，也沒有修改資料、Query 或 Phase 2 的人工標註。

## 一、實驗設定完全沿用 Phase 2

### Corpus

仍然使用 Phase 2 產生的完整 129 筆 TFDA Documents：

`data/processed/langchain_documents.json`

沒有修改：

- `page_content`
- `metadata`
- `document_id`
- `row_index`
- 原始 TFDA 內容

Contract Gate 仍然只檢查文件結構：

```text
Contract Gate: total=129 passed=129 rejected=0
```

### Query

窄 Query：

> `TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？`

廣 Query：

> `TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？`

### 模型與數量

- Embedding：`intfloat/multilingual-e5-small`
- Reranker：`BAAI/bge-reranker-v2-m3`
- Similarity candidate：Top-20
- Reranker output：Top-10
- 執行裝置：CPU

為什麼不直接把 Similarity Top-10 拿去重排？因為那樣看不到原本第 11～20 名的文件能不能被拉上來。這次先取 Top-20，再交給 Cross-Encoder，才比較符合實際 RAG pipeline 的使用方式。

## 二、LangChain integration 怎麼用

目前 LangChain 官方 Cross-Encoder Reranker 文件使用的寫法是：

```python
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

cross_encoder = HuggingFaceCrossEncoder(
    model_name="BAAI/bge-reranker-v2-m3",
    model_kwargs={"device": "cpu"},
)
reranker = CrossEncoderReranker(model=cross_encoder, top_n=10)
```

官方文件說明 Cross-Encoder 會直接評分 `(query, document)` 配對，而不是像 embedding 那樣分別建立向量後再比較；這通常能得到更細的排序，但每一份候選文件都要額外推論一次。官方範例也是先取較大的 candidate set，再用 `CrossEncoderReranker` 壓縮成較小的結果集：<https://docs.langchain.com/oss/python/integrations/document_transformers/cross_encoder_reranker>

本機環境實際可以 import：

- `langchain_classic.retrievers.document_compressors.CrossEncoderReranker`
- `langchain_community.cross_encoders.HuggingFaceCrossEncoder`

執行時有看到 `langchain-community` sunset 的 `DeprecationWarning`。目前官方文件仍使用這個 `HuggingFaceCrossEncoder` import，所以本階段先照官方目前可用寫法完成實驗；但報告要明確記錄：**正式維護版本未來需要留意 integration 遷移，不能把這個 import 當成永遠不會變。**

另外，`CrossEncoderReranker` 本身負責產生排序，但不直接把 score 放進輸出文件。本程式使用同一個 `HuggingFaceCrossEncoder.score()` 公開方法取得實際分數，並用 `CrossEncoderReranker` 執行正式排序，因此沒有自己編造分數。

Reranker score 是模型分數，不是機率，也不是醫學正確率。不同模型的分數不能直接互相比較。

## 三、窄 Query：最重要的 Before / After

窄 Query 是本階段主分析對象。

下表的 `Similarity Rank` 是 Phase 2 的排名，`Reranker Rank` 是本階段用 Similarity Top-20 重排後的排名。Phase 2 已有的標註沿用；從第 11～20 名被拉進新 Top-10 的文件，則在這一階段重新人工閱讀並標註。

| Document | Similarity Rank | Reranker Rank | Reranker score | Label | 主要內容與判斷理由 |
|---|---:|---:|---:|---|---|
| `tfda-risk-0019` | 1 | 1 | 0.999754 | Directly Relevant | SGLT2 酮酸中毒，直接回答 Query。 |
| `tfda-risk-0064` | 3 | 2 | 0.994688 | Partially Relevant | SGLT2 Fournier’s gangrene；同類藥品，但不是酮酸中毒。 |
| `tfda-risk-0042` | 2 | 3 | 0.991983 | Partially Relevant | SGLT2 下肢截肢；同類藥品，但不是酮酸中毒。 |
| `tfda-risk-0035` | 6 | 4 | 0.895197 | Partially Relevant | canagliflozin／dapagliflozin 的急性腎損傷；同類藥品，但風險主題不同。 |
| `tfda-risk-0015` | 13 | 5 | 0.715484 | Irrelevant | Codeine 兒童咳嗽與呼吸副作用，與 SGLT2 酮酸中毒無關；這是新拉進 Top-10 的文件。 |
| `tfda-risk-0102` | 17 | 6 | 0.597590 | Irrelevant | Colchicine 腎功能不全與藥物交互作用，與本 Query 無關；這是新拉進 Top-10 的文件。 |
| `tfda-risk-0068` | 19 | 7 | 0.529221 | Irrelevant | Fluoroquinolone 主動脈風險，與本 Query 無關；這是新拉進 Top-10 的文件。 |
| `tfda-risk-0112` | 10 | 8 | 0.506170 | Irrelevant | JAK 抑制劑的心臟事件、癌症與血栓，與本 Query 無關。 |
| `tfda-risk-0020` | 4 | 9 | 0.505622 | Irrelevant | PPI 與沙門氏菌感染，與 SGLT2 酮酸中毒無關。 |
| `tfda-risk-0053` | 8 | 10 | 0.395850 | Irrelevant | Sodium polystyrene sulfonate 交互作用，與本 Query 無關。 |

### 三筆 SGLT2 目標文件的變化

| 安全主題 | Document | Similarity Rank | Reranker Rank | 觀察 |
|---|---|---:|---:|---|
| 酮酸中毒 | `tfda-risk-0019` | 1 | 1 | 維持第一名，沒有被真正相關文件蓋掉。 |
| 下肢截肢 | `tfda-risk-0042` | 2 | 3 | 往後一名，但仍在前三名。 |
| Fournier’s gangrene | `tfda-risk-0064` | 3 | 2 | 往前一名，但仍然是部分相關，不是酮酸中毒答案。 |

## 四、窄 Query 的結果怎麼解讀

這次結果不能簡化成「Reranker 讓所有結果都變好」。比較準確的說法是：

### 有改善的地方

第一，真正回答酮酸中毒的 `tfda-risk-0019` 維持 Rank 1，這是最重要的結果。Reranker 沒有把真正相關文件誤排到後面。

第二，四筆和 SGLT2 有關的文件，原本分布在 Similarity Rank 1、2、3、6；重排後變成 Reranker Rank 1、2、3、4。也就是說，Reranker 把同類藥品的相關文件集中到前四名。

### 沒有改善的地方

如果把標註分成 Directly Relevant、Partially Relevant、Irrelevant，窄 Query 的 Top-10 前後都是：

- Directly Relevant：1 筆
- Partially Relevant：3 筆
- Irrelevant：6 筆

所以 Top-10 的簡單 precision 沒有提升。Reranker 只是換了一批不相關文件進來：原本的 Gadolinium、Pirfenidone、Hydrochlorothiazide 被排除，但第 11～20 名的 Codeine、Colchicine、Fluoroquinolone 被拉進來。

這代表 Reranker 確實在重新評分，但它不是一個能保證「只留下可回答文件」的 Context Gate。

尤其是 `tfda-risk-0064` 的 Reranker score 是 0.994688，幾乎和酮酸中毒文件一樣高；但它實際談的是 Fournier’s gangrene。這個例子再次證明：

> Cross-Encoder 可以改善 Query 和文件的配對排序，但「同一類藥品、不同安全主題」仍可能被判成高度相關。

## 五、廣 Query 的結果

廣 Query 問的是 SGLT2 類藥品有哪些安全警訊，所以三筆不同安全主題的 SGLT2 文件，以及 canagliflozin／dapagliflozin 的急性腎損傷文件，都算直接相關。

| Document | Similarity Rank | Reranker Rank | Reranker score | Label | 主要內容與判斷理由 |
|---|---:|---:|---:|---|---|
| `tfda-risk-0064` | 2 | 1 | 0.999105 | Directly Relevant | SGLT2 Fournier’s gangrene。 |
| `tfda-risk-0019` | 3 | 2 | 0.997694 | Directly Relevant | SGLT2 酮酸中毒。 |
| `tfda-risk-0042` | 1 | 3 | 0.995328 | Directly Relevant | SGLT2 下肢截肢。 |
| `tfda-risk-0035` | 4 | 4 | 0.946585 | Directly Relevant | canagliflozin／dapagliflozin 急性腎損傷，仍是 SGLT2 相關警訊。 |
| `tfda-risk-0020` | 6 | 5 | 0.758129 | Irrelevant | PPI 與沙門氏菌感染，與 SGLT2 無關。 |
| `tfda-risk-0112` | 7 | 6 | 0.710326 | Irrelevant | JAK 抑制劑風險，與 SGLT2 無關。 |
| `tfda-risk-0024` | 19 | 7 | 0.637704 | Irrelevant | NSAID 心血管風險，與 SGLT2 無關；這是新拉進 Top-10 的文件。 |
| `tfda-risk-0027` | 10 | 8 | 0.400035 | Irrelevant | Repaglinide 與 clopidogrel 交互作用，與 SGLT2 無關。 |
| `tfda-risk-0026` | 8 | 9 | 0.353044 | Irrelevant | DPP-4 抑制劑關節痛，與 SGLT2 無關。 |
| `tfda-risk-0023` | 16 | 10 | 0.305651 | Irrelevant | Gadolinium 腦部蓄積，與 SGLT2 無關。 |

廣 Query 的前後標註數量都是：

- Directly Relevant：4 筆
- Partially Relevant：0 筆
- Irrelevant：6 筆

四筆真正和 SGLT2 相關的文件，在 Similarity 時已經是前四名；Reranker 仍然保留在前四名，只是把順序從「截肢、Fournier、酮酸中毒、急性腎損傷」改成「Fournier、酮酸中毒、截肢、急性腎損傷」。因此在廣 Query 上，Reranker 主要是調整相關文件的相對順序，沒有增加 Top-10 的相關文件數量。

## 六、Phase 2 與 Phase 3 的整體比較

### 窄 Query

| 指標 | Phase 2 Similarity Top-10 | Phase 3 Reranker Top-10 | 解讀 |
|---|---:|---:|---|
| 酮酸中毒文件排名 | 1 | 1 | 核心結果維持。 |
| SGLT2 酮酸／截肢／Fournier 三筆排名 | 1、2、3 | 1、3、2 | 都在前三，沒有被排除。 |
| 四筆 SGLT2 相關文件的最差排名 | 6 | 4 | 相關文件更集中。 |
| Directly Relevant | 1 | 1 | 沒有增加。 |
| Partially Relevant | 3 | 3 | 沒有減少。 |
| Irrelevant | 6 | 6 | Top-10 precision 沒有提升。 |

### 廣 Query

| 指標 | Phase 2 Similarity Top-10 | Phase 3 Reranker Top-10 | 解讀 |
|---|---:|---:|---|
| 四筆 SGLT2 相關文件排名 | 1、2、3、4 | 1、2、3、4 | 相關文件仍然完整保留。 |
| Directly Relevant | 4 | 4 | 沒有增加。 |
| Partially Relevant | 0 | 0 | 沒有變化。 |
| Irrelevant | 6 | 6 | Top-10 precision 沒有提升。 |

## 七、這個實驗回答了什麼

本階段的答案是：

> Reranker 對「文件和 Query 的配對排序」有幫助，但在這個 TFDA SGLT2 實驗裡，它主要做到的是保住並集中 SGLT2 相關文件，沒有把「酮酸中毒」和「同類藥品的其他安全主題」完全分開。

具體來說：

1. 酮酸中毒文件維持第一名，沒有發生真正相關文件被錯誤降到後面的情況。
2. 截肢和 Fournier’s gangrene 仍然排得非常前面，因為它們和 Query 同時包含 SGLT2、藥品安全警訊等共同語意。
3. 原本 Similarity Rank 11～20 的文件確實有被拉進 Top-10，證明這次不是只重排原本 Top-10。
4. 新拉進來的文件全部是不相關文件，表示 candidate-20 擴大了搜尋範圍，但沒有自動帶來更好的 Top-10 precision。
5. Reranker 不能單獨判斷資料是否足夠、是否衝突，也不能決定某份文件是否可以直接支撐答案。

所以在整個 B 模組裡，Reranker 比較適合扮演：

> 先把候選文件排序得更合理、縮小 Context 的前處理元件。

它不適合單獨扮演：

> 最後決定 Context 能不能使用的 Gate。

## 八、人工標註說明

這次沒有讓 LLM 自動產生 Ground Truth。

人工標註的規則是：

- Directly Relevant：文件直接回答 Query 所問的安全主題。
- Partially Relevant：藥品類別或成分相關，但安全主題不同，不能直接支撐目前問題的完整答案。
- Irrelevant：藥品成分與安全主題都不能支撐目前 Query。

Phase 2 Top-10 的標註直接沿用；只有被 Reranker 從 Similarity Rank 11～20 拉進新 Top-10 的文件重新閱讀：

- 窄 Query：`tfda-risk-0015`、`tfda-risk-0102`、`tfda-risk-0068`，三筆都是 Irrelevant。
- 廣 Query：`tfda-risk-0024`，是 Irrelevant。

這些標註是本次小型實驗的人工評估，不是 TFDA 官方分類，也不是完整醫學 ground truth。

## 九、產出檔案

```text
tfda_context_gate/
├── 03_reranker.py
├── results/narrow_query_similarity_top20_candidates.json
├── results/broad_query_similarity_top20_candidates.json
├── results/narrow_query_reranked_top10.json
├── results/broad_query_reranked_top10.json
├── results/phase3_reranker_output.txt
└── reports/phase3_reranker_report.md
```

檔案用途：

- `03_reranker.py`：固定使用 Phase 2 corpus、Query 與 Contract Gate 的 Phase 3 程式。
- `*_similarity_top20_candidates.json`：保留送進 Reranker 的完整 Top-20 candidate。
- `*_reranked_top10.json`：保存重排後 Top-10、Reranker score、原 Similarity rank 和原 Similarity score。
- `phase3_reranker_output.txt`：本次實際執行的文字輸出。
- 本報告：用口語方式說明流程、Before／After、人工標註與限制。

本階段到此停止，沒有開始 LLM Judge、Hybrid 或最終整合報告。
