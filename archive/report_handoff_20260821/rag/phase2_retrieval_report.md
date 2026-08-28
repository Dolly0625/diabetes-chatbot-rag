# TFDA Context Gate Phase 2：Similarity Retrieval 實驗報告

## 先講這一階段在做什麼

這一階段還沒有讓 LLM 判斷答案，也還沒有加入 Reranker、LLM Judge 或 Hybrid Gate。

我們先做一件比較基本、但一定要先做的事：

> 把 TFDA 的完整風險溝通資料放進向量資料庫，看看使用者的 Query 送進去之後，Retriever 實際會找回哪些文件。

原因很簡單。如果連 Retriever 找回來的文件都沒有包含真正相關的資料，後面再加 Context Gate 也沒有辦法憑空補回資料。反過來，如果 Top-10 裡混進很多不相關文件，這就會成為後面測試 Context Gate 的真實輸入。

本階段只回答三個問題：

1. Query 能不能找回三筆 SGLT2 風險溝通資料？
2. Top-10 裡有多少是直接相關、部分相關或完全不相關？
3. Similarity score 高，是不是就代表文件真的適合回答這個問題？

## 一、資料來源：不是自己編的 Med-X，而是 TFDA 真實資料

這次使用的是 Phase 1 已下載的衛生福利部食品藥物管理署（TFDA）「藥品安全資訊風險溝通資料」。資料集本身每一筆不是「一張藥品許可證」，而是 TFDA 曾經發布的一筆藥品安全資訊或風險溝通事件。

主資料來源如下：

- 政府資料開放平台：<https://data.gov.tw/dataset/9573>
- 實際下載 endpoint：`https://data.fda.gov.tw/data/opendata/export/53/json`
- 本地原始檔：`data/raw/drug_risk_communication.json`
- 本次 corpus：129 筆風險溝通紀錄

例如，SGLT2 抑制劑在這份資料裡不是一筆，而是三筆不同日期、不同風險主題的 TFDA 資訊：

| 文件 | 發布日期 | 安全主題 |
|---|---:|---|
| `tfda-risk-0019` | 2015/6/25 | 酮酸中毒（ketoacidosis） |
| `tfda-risk-0042` | 2017/3/22 | 下肢截肢 |
| `tfda-risk-0064` | 2018/9/28 | Fournier’s gangrene／會陰部壞死性筋膜炎 |

這三筆資料很適合做實驗，因為它們同時具備「同一類藥品」和「不同安全主題」這兩個特性。也就是說，Retriever 可能知道它們都和 SGLT2 有關，但不一定能精準分辨 Query 問的是哪一種風險。

另外，Phase 1 下載的 72,008 筆「藥品許可證資料」這一階段沒有拿來 join，也沒有混進向量資料庫。原因是這一階段只測試風險溝通文件的 retrieval；如果現在加入許可證資料，會把「找風險溝通文件」和「藥品許可證 join」混成另一個實驗問題。

## 二、資料怎麼展示成 LangChain Document

原始 TFDA JSON 的每一筆資料，先轉成一個 LangChain `Document`。可以把它想成：一筆官方安全資訊，包成一個 Retriever 可以讀的文件物件。

### `page_content` 放什麼？

`page_content` 放的是文件正文，包含：

- 藥品成分
- 適應症
- 藥理作用機轉
- 訊息緣由
- 藥品安全有關資訊分析及描述
- TFDA 風險溝通說明

這樣做的意思是，向量模型不只看到「SGLT2」這個成分名稱，也會看到這筆資料到底是在講酮酸中毒、截肢、急性腎損傷，還是其他安全問題。

### `metadata` 放什麼？

`metadata` 保留方便追蹤的欄位：

- `document_id`：本實驗新增，例如 `tfda-risk-0019`
- `row_index`：原始 JSON 陣列的位置，本實驗使用 0-based index
- `發布日期`：TFDA 原始欄位
- `藥品成分`：TFDA 原始欄位
- `source_dataset`：標示資料來自 TFDA 風險溝通資料
- `raw_source_file`：標示本地原始檔位置

這裡要特別分清楚：`document_id` 和 `row_index` 是實驗 pipeline 加上的追蹤資訊，不是 TFDA 原始欄位；`發布日期` 和 `藥品成分` 則是從 TFDA 原始資料複製過來的欄位。

完整轉換結果保存在：

`data/processed/langchain_documents.json`

這個檔案除了 `page_content` 和 `metadata`，也保留每一筆原始 `raw_record`，所以後面看到某一筆排名時，可以回頭確認原文，不必只相信摘要或人工備註。

## 三、實驗流程：從原始資料到 Top-10

這次的執行流程如下：

1. 讀取 TFDA 原始 JSON，共 129 筆。
2. 將每筆資料轉成一個 LangChain `Document`。
3. 先跑薄版 Contract Gate，只檢查結構是否可以進入 retrieval。
4. 使用 `HuggingFaceEmbeddings`，模型為 `intfloat/multilingual-e5-small`。
5. 將 129 筆文件全部放入 LangChain `InMemoryVectorStore`。
6. 分別執行一個窄 Query 和一個廣 Query，兩者都取 `Top-K=10`。
7. Retrieval 結束後，才人工閱讀 Top-10，標成 Directly Relevant、Partially Relevant 或 Irrelevant。

本階段沒有使用 LLM API，因此沒有產生模型回答，也不需要 API key。Embedding 模型第一次執行時會從 Hugging Face 載入；之後可以使用本機快取。

### Contract Gate 的結果

```text
Contract Gate: total=129 passed=129 rejected=0
```

這代表 129 筆資料都有：

- 不重複的 `document_id`
- `row_index`
- 非空的藥品成分
- 非空的發布日期
- 非空的 `page_content`

但這個 Gate 只是在檢查「文件格式能不能進流程」，不是在檢查「內容是不是正確」或「風險是不是最新」。它沒有判斷醫學語意，也沒有判斷 TFDA 後來是否發布了更新資料。

## 四、窄 Query 的結果

窄 Query 是：

> `TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？`

### Top-10 展示

下表是完整結果的口語化整理。`page_content preview` 是從實際 Document 內容截取的預覽；`analysis_note` 是本實驗為了快速辨認主題加上的 pipeline 分析備註，不是 TFDA 原始欄位，也不是官方判定。

| Rank | Score | Document | 日期 | 成分 | page_content preview／主題 | 人工標註 |
|---:|---:|---|---|---|---|---|
| 1 | 0.910423 | `tfda-risk-0019` | 2015/6/25 | SGLT2 抑制劑類 | 原文出現酮酸中毒／ketoacidosis，內容直接說明 DKA、誘發因素與風險警告。 | Directly Relevant |
| 2 | 0.893141 | `tfda-risk-0042` | 2017/3/22 | SGLT2 抑制劑類 | 原文出現下肢截肢／amputation，屬同類藥品但安全主題不同。 | Partially Relevant |
| 3 | 0.892442 | `tfda-risk-0064` | 2018/9/28 | SGLT2 抑制劑類 | 原文出現 Fournier gangrene／會陰部壞死性筋膜炎，屬同類藥品但安全主題不同。 | Partially Relevant |
| 4 | 0.886340 | `tfda-risk-0020` | 2015/7/24 | 氫離子幫浦抑制劑類 | 內容是 PPI 與沙門氏菌感染風險，與 SGLT2 和酮酸中毒都不同。 | Irrelevant |
| 5 | 0.884840 | `tfda-risk-0023` | 2015/8/11 | Gadolinium | 內容是 GBCAs 蓄積於腦部的風險，與本 Query 無關。 | Irrelevant |
| 6 | 0.884395 | `tfda-risk-0035` | 2016/7/14 | Canagliflozin 及 dapagliflozin | 成分屬 SGLT2，但內容是在講急性腎損傷，不是酮酸中毒。 | Partially Relevant |
| 7 | 0.883728 | `tfda-risk-0099` | 2020/11/5 | Pirfenidone | 內容是藥物性肝損傷，藥品成分也不同。 | Irrelevant |
| 8 | 0.881287 | `tfda-risk-0053` | 2017/9/29 | Sodium polystyrene sulfonate | 內容是藥物交互作用，與本 Query 無關。 | Irrelevant |
| 9 | 0.880480 | `tfda-risk-0065` | 2019/1/8 | Hydrochlorothiazide | 內容是皮膚惡性腫瘤風險，與本 Query 無關。 | Irrelevant |
| 10 | 0.880095 | `tfda-risk-0112` | 2021/11/29 | 含 JAK 抑制劑類成分 | 內容是心臟事件、癌症、血栓與死亡風險，與本 Query 無關。 | Irrelevant |

### 窄 Query 怎麼解讀？

這個結果很漂亮地把三件事分開了：

- 第 1 名是 `tfda-risk-0019`，它真的就是酮酸中毒資料，代表 Retriever 找得到核心文件。
- 第 2、3 名雖然不是酮酸中毒，但因為同樣是 SGLT2，所以被排得很前面。
- 第 6 名也因為成分是 canagliflozin 和 dapagliflozin 而被找回來，但它談的是急性腎損傷。
- 第 4、5、7、8、9、10 名已經是完全不同的藥品或風險主題，仍然有不低的分數。

用人工標註計算，窄 Query 的 Top-10 是：

- Directly Relevant：1 筆
- Partially Relevant：3 筆
- Irrelevant：6 筆

所以這裡最重要的觀察是：

> Similarity score 高，只能說文字或語意向量相似，不能直接等同於「這份資料可以拿來回答目前問題」。

例如第 2 名分數是 0.893141，看起來很高，但它實際上回答的是截肢，不是酮酸中毒。這正是後面需要 Context Gate 的地方。

## 五、廣 Query 的結果

廣 Query 是：

> `TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？`

### Top-10 展示

| Rank | Score | Document | 日期 | 成分 | page_content preview／主題 | 人工標註 |
|---:|---:|---|---|---|---|---|
| 1 | 0.908149 | `tfda-risk-0042` | 2017/3/22 | SGLT2 抑制劑類 | 下肢截肢／amputation。 | Directly Relevant |
| 2 | 0.906021 | `tfda-risk-0064` | 2018/9/28 | SGLT2 抑制劑類 | Fournier gangrene／會陰部壞死性筋膜炎。 | Directly Relevant |
| 3 | 0.905498 | `tfda-risk-0019` | 2015/6/25 | SGLT2 抑制劑類 | 酮酸中毒／ketoacidosis。 | Directly Relevant |
| 4 | 0.893758 | `tfda-risk-0035` | 2016/7/14 | Canagliflozin 及 dapagliflozin | 急性腎損傷，仍然是 SGLT2 相關安全警訊。 | Directly Relevant |
| 5 | 0.892882 | `tfda-risk-0065` | 2019/1/8 | Hydrochlorothiazide | 皮膚惡性腫瘤，與 SGLT2 無關。 | Irrelevant |
| 6 | 0.890922 | `tfda-risk-0020` | 2015/7/24 | 氫離子幫浦抑制劑類 | 沙門氏菌感染，與 SGLT2 無關。 | Irrelevant |
| 7 | 0.890796 | `tfda-risk-0112` | 2021/11/29 | 含 JAK 抑制劑類成分 | 心臟事件、癌症、血栓與死亡，與 SGLT2 無關。 | Irrelevant |
| 8 | 0.890366 | `tfda-risk-0026` | 2015/10/28 | DPP-4 抑制劑類 | 嚴重關節痛，與 SGLT2 無關。 | Irrelevant |
| 9 | 0.889173 | `tfda-risk-0099` | 2020/11/5 | Pirfenidone | 藥物性肝損傷，與 SGLT2 無關。 | Irrelevant |
| 10 | 0.888820 | `tfda-risk-0027` | 2015/12/22 | Repaglinide | 與 clopidogrel 的交互作用，與 SGLT2 無關。 | Irrelevant |

### 廣 Query 怎麼解讀？

廣 Query 問的是「SGLT2 有哪些安全警訊」，所以三筆 SGLT2 的不同風險都算直接相關；第 4 名雖然成分欄位寫的是兩個具體成分，但仍然是 SGLT2 藥品的安全資訊，也算直接相關。

廣 Query 的 Top-10 是：

- Directly Relevant：4 筆
- Partially Relevant：0 筆
- Irrelevant：6 筆

換句話說，廣 Query 對「找出 SGLT2 這一類的多個安全主題」比較合適；窄 Query 則更能測出 Retriever 是否會把「同一類藥品、但不同風險」混在一起。

## 六、三筆目標資料的排名

這三筆是本階段最重要的 target records：

| Document | 安全主題 | 窄 Query | 廣 Query |
|---|---|---:|---:|
| `tfda-risk-0019` | 酮酸中毒 | Rank 1，0.910423 | Rank 3，0.905498 |
| `tfda-risk-0042` | 下肢截肢 | Rank 2，0.893141 | Rank 1，0.908149 |
| `tfda-risk-0064` | Fournier’s gangrene | Rank 3，0.892442 | Rank 2，0.906021 |

三筆目標資料在兩個 Query 都有進 Top-10，表示這次的 corpus 和 embedding 設定至少能把同一類藥品的三筆安全資訊找回來。

但是「找回來」不等於「每一筆都可以拿來回答」。窄 Query 的情況尤其清楚：截肢和 Fournier’s gangrene 兩筆資料有關聯，但不能直接拿來回答酮酸中毒的細節。這就是 retrieval relevance 和 answer usability 之間的差別。

## 七、這一階段對 Context Gate 的意義

這個實驗已經先證明，Similarity Retrieval 本身有能力找回相關文件，但它沒有能力單獨完成「能不能使用」的判斷。

口語化地說：

> Retriever 像是在資料櫃裡找「看起來最像 Query 的文件」；Context Gate 還要再問一次：「這份文件是不是在回答我現在問的那個風險？資料夠不夠？有沒有和另一份文件衝突？」

本次結果中的典型例子是：

- `tfda-risk-0019`：和窄 Query 直接對應，可以進入後續回答流程。
- `tfda-risk-0042`、`tfda-risk-0064`：同屬 SGLT2，但風險主題不同，不能因為分數高就直接當成酮酸中毒的證據。
- `tfda-risk-0035`：成分和 SGLT2 有關，但談的是急性腎損傷；它對廣 Query 有用，對窄 Query 只能算部分相關。
- 其他文件：分數仍然不低，但藥品成分與安全主題都對不上，應該被後續 gate 排除或降權。

因此，下一階段才適合在相同 Query、相同 129 筆 corpus 和相同 Contract Gate 基礎上，加入 Reranker，測試它能不能把不相關文件往後排。再下一步才測 LLM Judge 或 Hybrid Context Gate，判斷 relevant、sufficient 和 conflict。

## 八、限制與目前不能宣稱的事情

這次結果可以支持的說法是：

- 129 筆 TFDA 風險溝通資料已成功轉成 LangChain Documents。
- 所有文件通過本階段的薄版 Contract Gate。
- `multilingual-e5-small` 加上 InMemoryVectorStore 能找回三筆 SGLT2 target records。
- Similarity Top-10 中確實存在同類藥品但不同安全主題的部分相關文件，以及完全不相關文件。

這次結果不能直接支持的說法是：

- 不能說 score 超過某個數字就一定可以使用。
- 不能說這是醫學正確率或臨床安全率；本階段沒有讓 LLM 產生醫療答案，也沒有做臨床效度驗證。
- 不能把 `analysis_note` 當成 TFDA 的官方分類。它只是根據原文關鍵字做的 pipeline 觀察備註。
- 不能說資料一定是目前最新的風險狀態。`發布日期` 是原始資料日期，不等於現在仍然有效或沒有後續更新。
- 不能從這次 Top-10 結果推算完整 recall，因為本階段只對三筆選定的 SGLT2 target records 做檢查，還沒有建立完整人工 ground truth。

## 九、產出檔案

本階段產出如下：

```text
tfda_context_gate/
├── data/processed/langchain_documents.json
├── 01_build_documents.py
├── 02_similarity_retrieval.py
├── results/narrow_query_top10.json
├── results/broad_query_top10.json
├── results/phase2_similarity_output.txt
└── reports/phase2_retrieval_report.md
```

其中：

- `langchain_documents.json`：129 筆轉換後的 Document 資料與原始 record。
- `narrow_query_top10.json`：窄 Query 的完整 Top-10，包含 rank、score、metadata、page preview 與分析備註。
- `broad_query_top10.json`：廣 Query 的完整 Top-10。
- `phase2_similarity_output.txt`：本次執行的可讀文字輸出。
- `phase2_retrieval_report.md`：本報告，包含資料來源、執行流程、結果與人工標註。

本階段到此結束；沒有把結果往 Reranker、LLM Judge 或 Hybrid 實驗延伸。
