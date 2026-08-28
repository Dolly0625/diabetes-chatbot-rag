# Boundary 邊界設定團隊 — B 成員任務

## 限制機制（執行階段）與 LLM 組協調

> 對應提案計劃書第 4 節「Milestone 1（8/24前）」— Boundary 邊界設定團隊 B 成員
> 共用範例查詢（第 3.2 節）：\\\*\\\*「我有腎功能不好，可以吃 metformin 嗎？」\\\*\\\*

\---

## 摘要

本文件負責 Boundary 團隊 B 成員的任務：**列出 RAG 執行階段（query-time）的限制機制**（信心門檻、跳數限制、關係類型排除等），並釐清這些機制與 **LLM 組 Context Gate / CRAG Evaluator** 的分工，避免兩組重複造輪子。

已完整閱讀 LLM 組 Part B 的**兩份**獨立產出：

* **林子榕版**：以 Similarity → Reranker → LLM Judge → Hybrid 四種方法比較為主軸，用 TFDA 129 筆藥品安全資料做 SGLT2 酮酸中毒的小實驗，得出「Similarity 高不代表能用、Reranker 前面不代表能用、LLM Judge 才能細判」的結論。
* **邵崴版**：以 CRAG（Corrective RAG）為核心，把 Context Gate 具體拆成 Contract Validator（JSON Schema）→ Similarity（僅記錄不設門檻）→ Cross-Encoder Reranker → \*\*CRAG Evaluator（τ+=0.50 / τ−=-0.91，文獻可追溯的實際數值）\*\*→ Knowledge Refinement（strip-level, threshold=-0.50, Top-K=5）→ LLM Judge（structured boolean 輸出）→ Injection Filter，並給出可執行的 Hybrid Gate 邏輯與 Threshold 校準計畫。

兩份文件雖然作者不同，但範圍高度重疊：**都在處理「Chunk 已經被撈出來之後」的 Contract Gate + Context Gate**（格式驗證、Relevance、Sufficiency、Conflict、Freshness、Prompt Injection、Hybrid 判斷邏輯），且都已經把「不能把所有分數混成單一 Confidence」這件事講清楚。

本文件的定位刻意錯開：**Boundary 的限制機制發生在「Chunk 被撈出來之前 / Retriever 執行過程中」**，屬於結構性、規則型的門檻，不涉及語意判斷，也不重做 Contract Gate 的 JSON Schema 驗證或 CRAG Evaluator 的語意信心分數。兩者是同一條管線上前後不同的兩層，不是重複的兩層。

\---

## 1\. 執行階段限制機制清單

### 1.1 信心門檻（Confidence Threshold）

**定義**：在 Retriever 回傳候選 Chunk 的當下，依分數（`score` + `score\\\_type`）先做一次結構性篩選，決定候選是否進入候選池，而不是等到 Context Gate 才判斷。

**設計依據**：CRAG（Corrective Retrieval Augmented Generation）提出的檢索評估機制，將檢索結果依信心分數畫出「上界」與「下界」兩道門檻，高於上界者判為可用、低於下界者判為不可用、中間地帶才需要更進一步處理，藉此讓系統在不同信心程度下觸發不同動作，而非一律送進生成端。邵崴版文件已將此機制列為 Context Gate 的核心層，並直接採用原始論文 Pub/ARC 設定的可追溯數值（上閾值 0.50、下閾值 -0.91、strip 過濾閾值 -0.50、Top-K=5）作為第一版基準，同時說明這組數值是 dataset-specific，正式上線前需要用本地驗證集重新校準。

**與 CRAG Evaluator 的分工**：邵崴版的 CRAG Evaluator 是**語意信心分數**（Question–Document pair 由訓練過的 evaluator 模型估計相關性，範圍 \[-1, 1]），屬於 Context Gate 內部、Chunk 已完整產出後才執行的判斷。本文件的信心門檻則是**檢索分數的結構性篩選**（Retriever 原生分數，依 score\_type 分流），發生在 Chunk 尚未完整打包、也還沒送進 CRAG Evaluator 之前，兩者不是同一個分數，也不共用同一組閾值 —— 這點與邵崴版「不可混用的分數」原則一致，只是本文件把這個原則延伸到 Boundary 這一層更早的檢索分數上。

**與第 3.3 節欄位的關係**：由於 `score\\\_type` 分為 similarity / rerank / graph\_traversal 三種，且 LLM 組文件已明確指出「不同計分方式不能混成單一 Confidence Score」，因此本機制**不設單一全域門檻**，而是**依 score\_type 分別設定門檻**：

|score\_type|門檻性質|用途|
|-|-|-|
|similarity|第一輪寬鬆門檻|過濾明顯不相關的候選，避免污染候選池|
|rerank|中等門檻|決定進入 Top-N 的候選數量上限|
|graph\_traversal|依路徑分數 + 跳數共同判斷|見 1.2|

**與 Context Gate 的界線**：信心門檻只回答「這個分數配不配進候選池」，不回答「內容是否真的能回答這題」— 後者是 LLM Judge 的工作，兩者不重疊。

\---

### 1.2 跳數限制（Hop Limit / Traversal Depth）

**定義**：Graph Retriever 在單次查詢中，實際允許走幾步（幾個 relation）才停止擴散搜尋，屬於「執行時」的動態預算，區別於 A 成員在設計階段定的「最大遍歷深度上限」。

**設計依據**：多跳知識圖譜推理研究指出，明確設定「跳數門檻」與「邏輯關係限制」兩項約束，是控制圖遍歷深度、避免無限擴散、確保推理可解釋與可控的關鍵機制；若不加限制，圖檢索容易在無關實體間持續擴散，反而拉入大量與原問題無關的節點。Microsoft GraphRAG 的做法則是先用社群偵測（community detection）縮小搜尋範圍，再於社群內做多跳擴散，同樣是為了避免在全圖上做無限制遍歷。

**在共用範例查詢下的應用**：「metformin + 腎功能」屬於典型 1～2 跳查詢（Metformin → 禁忌條件 → 腎功能不良），本組建議：

* 預設跳數預算：2 跳
* 超過預算仍未收斂到目標實體時，**不強行繼續擴散**，而是回傳目前已走到的部分路徑

**跳數用盡時的行為，需與 LLM 組確認**（見第 3 節 TBD）：回傳空結果直接觸發 FALLBACK，還是回傳部分路徑讓 Context Gate 判斷 Sufficiency？這是本機制與 Context Gate 唯一有交集、必須先對齊的地方。

\---

### 1.3 關係類型排除（Relation Type Exclusion）

**定義**：Graph Retriever 在做遍歷時，明確限制「只能沿哪些關係類型走」（allow-list）或「不能沿哪些關係類型走」（deny-list），而不是任意關係都能擴散。

**設計依據**：知識圖譜用於 RAG 時，實體之間的關係型態本身就帶有語意（例如 treats、causes、contraindicated\_for、interacts\_with），若不對關係類型做篩選，遍歷容易滑向與安全問題無關的節點；已有醫療知識圖譜資源明確將「禁忌」「交互作用」等安全相關關係獨立建模，並在藥物安全查詢中優先沿這類關係擴散，也有研究顯示知識圖譜檢索在藥物副作用判斷上遠比純向量檢索精確，原因正是關係型態被明確保留而非被壓成純文字相似度。

此外，「看起來像有用、實際上會干擾」的候選內容已被證實會拖累生成品質（即使該候選在語意上與主題相關），這點與 LLM 組引用的 ACL 2025 研究結論一致——因此在 Boundary 這一層就先排除掉主題不符的關係類型，可以在候選池階段就減少這類 hard distracting 內容，而不是全部留給 Context Gate 之後才靠 LLM Judge 去分辨。

**建議分類**：

|類別|範例（暫定，待 Preprocessing B 定案 schema 後補齊）|處理方式|
|-|-|-|
|安全相關關係|contraindicated\_for、interacts\_with、causes\_adverse\_event|允許遍歷（allow-list）|
|一般衛教關係|treats、indicated\_for|依查詢類型決定是否允許|
|與安全無關的關係|manufactured\_by、marketed\_as、priced\_at|預設排除（deny-list）|

> 這份清單目前只能列到「分類邏輯」，具體關係名稱需要等 Preprocessing B 成員定案 Graph 管道的 schema / ontology 來源（SNOMED CT 或自訂節點）後才能列出實際名稱，已標記為 TBD。

\---

### 1.4 其他建議的執行階段機制（待與其他組確認範圍）

|機制|說明|需要協調的對象|
|-|-|-|
|Top-K / Top-N 截斷|每個 Retriever 各自先截斷候選數量|與 Multi-RAG B 成員的「合併策略」可能重疊，需釐清截斷放在 Boundary 還是 Multi-RAG|
|節點型別限制（entity type allow-list）|只允許 Drug / Disease / Symptom 等醫療相關節點型別被回傳，排除非醫療型別節點，降低外部資料被夾帶惡意內容的攻擊面|與 LLM 組 Prompt Injection 檢查（OWASP LLM01）的分工需確認：節點型別限制是否能取代／減輕 Context Gate 的 Prompt Injection 檢查負擔|

OWASP 將向量與 Embedding 層級的風險（未授權資料被取回、不同 Context 間資訊洩漏、資料被下毒等）列為 RAG 系統的專屬安全風險類別，並建議搭配資料驗證、來源驗證、權限控制與檢索紀錄；Boundary 這一層的節點型別限制與關係型別排除，可以視為對「來源驗證 / 權限控制」這一項建議的具體落地方式之一。

\---

## 2\. 與 LLM 組 Context Gate / CRAG Evaluator 的分工

### 2.1 兩層各自負責什麼

```text
RAG Retrieval（Vector / Graph）
  │
  ▼
【Boundary 執行階段限制】← 本文件範圍
  - 信心門檻（依 score\\\_type，僅結構性分流）
  - 跳數限制
  - 關係類型排除
  作用位置：Retriever 內部 / 候選尚未打包成 Chunk 之前
  判斷方式：規則 / 分數比較，不需要語意理解
  │
  ▼
① Contract Gate（LLM 組，兩份文件皆有）
  - JSON Schema / Rule：chunk\\\_id、source、version、date、
    score\\\_type、status 等 required 欄位 100% 通過才 PASS
  │
  ▼
② Context Gate（LLM 組，兩份文件皆有，邵崴版拆解最細）
  - Similarity（僅記錄，不設跨模型硬門檻）
  - Cross-Encoder Reranker（重新排序）
  - CRAG Evaluator（τ+ / τ− 語意信心分數 → Correct/Ambiguous/Incorrect）
  - Knowledge Refinement（strip-level 篩選）
  - LLM Judge（structured：relevant / sufficient / conflict）
  - Injection Filter（binary flag）
  作用位置：Chunk 已完整產出、通過 Contract Gate 之後
  │
  ▼
Generator
```

這個順序延續 LLM 組兩份文件共同的「Contract Gate 跟 Context Gate 不一樣」框架，本組在最前面再補一層：**Boundary 執行階段限制是比 Contract Gate 更早發生的結構性守門**，先決定「值不值得被撈出來」，Contract Gate 再決定「格式合不合格」，Context Gate 最後決定「內容能不能用」。邵崴版文件的 Hybrid Gate 邏輯（`PASS = ContractPass AND RetrievalState==CORRECT AND Judge.relevant AND Judge.sufficient AND NOT Judge.conflict AND NOT InjectionFlag`）完整涵蓋了①②兩層，本文件不重複設計這條邏輯，只在最前面補上 Boundary 這一段。

### 2.2 介面說明表（誰負責什麼、資料如何交接）

|項目|Boundary（本文件）|LLM 組 Context Gate|
|-|-|-|
|作用時機|Retrieval 進行中|Retrieval 完成、Chunk 已生成後|
|判斷依據|Rule-based：分數門檻、跳數、關係類型|Semantic：LLM Judge、Metadata|
|輸入|Retriever 原始候選（尚未打包成第 3.3 節格式）|已符合 Contract Gate 格式的完整 Chunk|
|輸出|通過門檻的候選集合，並填入 `score`、`score\\\_type` 欄位供下游判斷|`decision\\\_code`（PASS / REVIEW / FALLBACK）、`usable\\\_document\\\_ids`|
|成本|低（規則比對，無需呼叫 LLM）|較高（LLM Judge 需要 Token 與延遲）|
|對應本組共用範例|Metformin+腎功能查詢的跳數與關係型別預算|沿用同一查詢，但改為判斷已收集到的 Chunk 是否足以回答|

兩層不重複的關鍵在於：**Boundary 不對「內容是否正確、是否足夠」下判斷，只對「這個候選值不值得往下送」下判斷**；語意層的對錯留給 Context Gate 的 LLM Judge。

\---

## 3\. 待與 LLM 組確認事項（本文件標記，未自行假設）

* \[TBD-需 LLM 組確認]：Graph Retriever 因跳數限制提前終止、尚未走到目標實體時，應直接回傳空結果觸發 CRAG 的 `Incorrect`／邵崴版的 Fallback，還是回傳「部分路徑」交給 Context Gate 判斷 Sufficiency（類似林子榕版 Ambiguous/PARTIAL 的處理）？
* \[TBD-需 LLM 組確認]：關係類型排除清單，是否要跟 Contract Gate 的 `score\\\_type`／`status` 欄位檢查合併維護一份，避免兩組各自維護造成不一致。
* \[TBD-需 LLM 組確認]：Graph Retriever 的 `graph\\\_traversal` 分數是否也要納入邵崴版 Threshold 校準計畫（六類測試案例：可回答、部分回答、無資料、資料衝突、過期、注入），還是 Graph 路徑需要另外一組校準數據？


\---

## 4\. 對照兩份 LLM 組文件，本文件刻意不重複的內容

已完整閱讀 LLM 組 Part B 的兩份獨立產出（林子榕版、邵崴版），以下內容兩份文件已完整處理，本文件不再重做：

**兩份都涵蓋、本文件不重做**：

* Contract Gate 基本欄位檢查（ID / Source / Version / Date / Status / score\_type）
* Relevance、Sufficiency、Conflict、Prompt Injection 的語意層判斷邏輯
* Similarity / Reranker / LLM Judge 的優缺點比較

**林子榕版已做，不重做**：

* Freshness、Chunk Integrity、Traceability 三項檢查
* TFDA 129 筆真實資料、SGLT2 酮酸中毒案例的小實驗與 Gate vs Ranking（FALLBACK）示範
* Similarity/Reranker/Judge/Hybrid 四法在同一批真實資料上的排名比較表
* LangSmith / Langfuse / Arize Phoenix 等評估與觀測工具建議

**邵崴版已做，不重做**：

* Contract Gate 的 JSON Schema（Draft 2020-12）具體驗證規則
* CRAG Evaluator 的可追溯數值（τ+=0.50、τ−=-0.91、strip 閾值=-0.50、Top-K=5）與 Knowledge Refinement 流程
* LLM Judge 的 structured boolean 輸出格式與 Judge bias 文獻（MT-Bench、ContextualJudgeBench）
* 完整 Hybrid Gate 的七步驟流程與最終 PASS 判斷式
* Threshold 校準計畫（六類測試案例、以降低錯誤放行率為校準目標）

本文件只聚焦在「Chunk 被撈出來之前」的結構性限制機制（信心門檻的分流設計、Graph 的跳數與關係類型限制），並改用本組共用範例查詢（Metformin + 腎功能）而非 LLM 組用的 SGLT2 / 高血壓飲食案例，避免範例重複、方向重複。

\---

## 5\. 共用範例查詢下的具體示意

以「我有腎功能不好，可以吃 metformin 嗎？」為例，三項機制的實際作用：

```text
Query: 我有腎功能不好，可以吃 metformin 嗎？
  │
  ▼ Vector Retriever
  信心門檻（similarity 門檻）→ 過濾掉與「腎功能」「metformin」皆無關的衛教文本
  │
  ▼ Graph Retriever
  關係類型排除 → 只允許沿 contraindicated\\\_for / interacts\\\_with 等安全關係擴散，
                  排除 manufactured\\\_by 等無關關係
  跳數限制 → 預算 2 跳：Metformin → contraindicated\\\_for → 腎功能不良條件
             第 3 跳若仍未收斂，暫停擴散，回傳目前路徑
  │
  ▼ 候選集合交給 Contract Gate → Context Gate
```

\---

## 參考文獻

### 論文

1. Yan, S.-Q., Gu, J.-C., Zhu, Y., \& Ling, Z.-H. **Corrective Retrieval Augmented Generation.** arXiv:2401.15884, 2024.
[https://arxiv.org/abs/2401.15884](https://arxiv.org/abs/2401.15884)
2. **DRKG: Faithful and Interpretable Multi-Hop Knowledge Graph Question Answering via LLM-Guided Reasoning Plans.** *Applied Sciences*, MDPI, 2025.
[https://www.mdpi.com/2076-3417/15/12/6722](https://www.mdpi.com/2076-3417/15/12/6722)
3. Amiraz et al. **The Distracting Effect: Understanding Irrelevant Passages in RAG.** ACL 2025.
[https://aclanthology.org/2025.acl-long.892/](https://aclanthology.org/2025.acl-long.892/)
4. **SIDEKICK: A Semantically Integrated Resource for Drug Effects, Indications, and Contraindications.** arXiv:2602.19183.
[https://arxiv.org/pdf/2602.19183](https://arxiv.org/pdf/2602.19183)
5. **RAG-based architectures for drug side effect retrieval using compact LLMs.** *Scientific Reports*, 2026.
[https://www.nature.com/articles/s41598-026-41495-2](https://www.nature.com/articles/s41598-026-41495-2)

### 官方技術 / 安全文件

6. OWASP LLM08:2025 — Vector and Embedding Weaknesses
[https://genai.owasp.org/llmrisk/llm082025-vector-and-embedding-weaknesses/](https://genai.owasp.org/llmrisk/llm082025-vector-and-embedding-weaknesses/)
7. OWASP LLM01:2025 — Prompt Injection
[https://genai.owasp.org/llmrisk/llm01-prompt-injection/](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
8. Microsoft GraphRAG — Global Community Summary Retriever（社群偵測縮小搜尋範圍之設計）
[https://graphrag.com/reference/graphrag/global-community-summary-retriever/](https://graphrag.com/reference/graphrag/global-community-summary-retriever/)

### JSON Schema / Judge 相關文獻（邵崴版已引用，本文件沿用其可追溯數值）

9. Bhutton, H., Andrews, H., Wright, A., Dennis, G. **JSON Schema: Validation Vocabulary.** Draft 2020-12.
[https://json-schema.org/draft/2020-12/json-schema-validation](https://json-schema.org/draft/2020-12/json-schema-validation)
10. Nogueira, R. \& Cho, K. **Passage Re-ranking with BERT.** arXiv:1901.04085, 2019.
[https://arxiv.org/abs/1901.04085](https://arxiv.org/abs/1901.04085)

### 內部文件

11. 林子榕. **B：Contract Gate + Context Gate.**（本組對照、避免重複之依據文件一）
12. 邵崴. **B：Contract Gate + Context Gate — RAG 後資料品質閘門｜技術調查交付文件.**（本組對照、避免重複之依據文件二，本文件之信心門檻分流設計特別參照其 CRAG 閾值與「不可混用的分數」原則）
13. TFDA / 政府資料開放平臺 — 藥品安全資訊風險溝通資料（Preprocessing 團隊已沿用之資料來源）
[https://data.gov.tw/dataset/9573](https://data.gov.tw/dataset/9573)

