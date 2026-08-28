# Boundary 邊界設定團隊 — B 成員任務

> **修訂說明（2026/08/23，Preprocessing B 成員 Erich 校對）**：本檔為原 `MS1/Boundary - B.md` 的校對版。原文 1.3、1.4 節的關係／節點名稱是 Graph schema 定案前的暫定版本，與 8/21 已定案的 **schema v3**（6 種節點、10 種邊）對不上，且其中兩處會直接讓共用範例查詢失效，故予以更正。**僅修正事實面內容**，本文件的定位、與 LLM 組的分工論述、對照表與文獻皆未更動。主要修訂處：1.1（補 Graph 側第二種分數）、1.2（`IS_A` 不計跳數、與抽取端拆分原則的相依性）、1.3（關係型別改用 schema v3 名稱）、1.4（節點型別 allow-list 更正）、第 3 節（新增責任真空 TBD）、第 5 節（示意圖同步）。原作者請自行 diff 後決定採用範圍。

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

**補充：Graph 側有兩種不同的分數，同樣不可混用**〔依 Preprocessing B schema v3 校對〕

上表的 `graph_traversal` 是**檢索時的路徑分數**。但 Graph 候選還帶有第二種分數：Preprocessing B 在三元組上產出的**抽取信心** `confidence`（每條 relation 一個），以及布林欄位 `negation_checked`（該事實是否已檢查否定詞／強度）。兩者來源完全不同——前者衡量「這條路徑與 Query 有多近」，後者衡量「這條事實當初抽得多可靠」。

依本節「不同計分方式不能混成單一 Confidence」的同一原則，Graph 候選應**同時**滿足兩道獨立門檻：

|分數／欄位|來源|門檻用途|
|-|-|-|
|`score`（`score_type=graph_traversal`）|Graph Retriever 檢索時計算|路徑與 Query 的關聯強度|
|`confidence`|Preprocessing B 抽取階段產出，**已存在於三元組契約中**|低信心事實不進候選池（對應 C 成員失敗模式表「抽取信心未分級」）|
|`negation_checked`|同上，布林|未通過否定詞／強度檢查的高風險事實（`CONTRAINDICATED_FOR` / `CAUTION_FOR` / `INDUCES`）不放行|

**[TBD-需 8/24 M1 會議確認]** `confidence` 的門檻數值。欄位本身已由 Preprocessing B 定義完成，不需另請 Multi-RAG B 新增，僅需定值。

**與 Context Gate 的界線**：信心門檻只回答「這個分數配不配進候選池」，不回答「內容是否真的能回答這題」— 後者是 LLM Judge 的工作，兩者不重疊。

\---

### 1.2 跳數限制（Hop Limit / Traversal Depth）

**定義**：Graph Retriever 在單次查詢中，實際允許走幾步（幾個 relation）才停止擴散搜尋，屬於「執行時」的動態預算，區別於 A 成員在設計階段定的「最大遍歷深度上限」。

**設計依據**：多跳知識圖譜推理研究指出，明確設定「跳數門檻」與「邏輯關係限制」兩項約束，是控制圖遍歷深度、避免無限擴散、確保推理可解釋與可控的關鍵機制；若不加限制，圖檢索容易在無關實體間持續擴散，反而拉入大量與原問題無關的節點。Microsoft GraphRAG 的做法則是先用社群偵測（community detection）縮小搜尋範圍，再於社群內做多跳擴散，同樣是為了避免在全圖上做無限制遍歷。

**在共用範例查詢下的應用**：「metformin + 腎功能」屬於典型 1～2 跳查詢（Metformin → 禁忌條件 → 腎功能不良），本組建議：

* 預設跳數預算：2 跳
* 超過預算仍未收斂到目標實體時，**不強行繼續擴散**，而是回傳目前已走到的部分路徑
* **`IS_A` 為結構性階層邊，不計入跳數預算**〔依 Preprocessing B schema v3 校對〕：`IS_A`（成分 ⊂ 藥物類別）表達的是同型別內的階層關係，不是語意上的一步推理。若它佔用預算，類別層級的安全事實（如 `Canagliflozin --IS_A--> SGLT2抑制劑 --INDUCES--> 酮酸中毒`）等於只剩 1 跳可用。詳見 1.3 節說明。

**與抽取端事實拆分原則的相依性**〔依 Preprocessing B schema v3 校對〕：Preprocessing B 的抽取原則 2 明訂條件式／三方風險關係**不造三元邊**，而是拆成兩條獨立事實，**預期由檢索端多跳重組**。以共用範例查詢為例，「腎功能不全患者用 metformin 有乳酸中毒風險」在圖譜中是 `Metformin --INDUCES--> 乳酸中毒` 與 `腎功能不全 --RISK_FACTOR_FOR--> 乳酸中毒` 兩條獨立事實，需要兩個起點各走 1 跳才能在檢索時匯合。2 跳預算恰好可容納，但**沒有餘裕**——因此本預算須以旗艦查詢實測後才可視為定案，不宜僅依直觀判斷。

**跳數用盡時的行為，需與 LLM 組確認**（見第 3 節 TBD）：回傳空結果直接觸發 FALLBACK，還是回傳部分路徑讓 Context Gate 判斷 Sufficiency？這是本機制與 Context Gate 唯一有交集、必須先對齊的地方。

\---

### 1.3 關係類型排除（Relation Type Exclusion）

**定義**：Graph Retriever 在做遍歷時，明確限制「只能沿哪些關係類型走」（allow-list）或「不能沿哪些關係類型走」（deny-list），而不是任意關係都能擴散。

**設計依據**：知識圖譜用於 RAG 時，實體之間的關係型態本身就帶有語意（例如 treats、causes、contraindicated\_for、interacts\_with），若不對關係類型做篩選，遍歷容易滑向與安全問題無關的節點；已有醫療知識圖譜資源明確將「禁忌」「交互作用」等安全相關關係獨立建模，並在藥物安全查詢中優先沿這類關係擴散，也有研究顯示知識圖譜檢索在藥物副作用判斷上遠比純向量檢索精確，原因正是關係型態被明確保留而非被壓成純文字相似度。

此外，「看起來像有用、實際上會干擾」的候選內容已被證實會拖累生成品質（即使該候選在語意上與主題相關），這點與 LLM 組引用的 ACL 2025 研究結論一致——因此在 Boundary 這一層就先排除掉主題不符的關係類型，可以在候選池階段就減少這類 hard distracting 內容，而不是全部留給 Context Gate 之後才靠 LLM Judge 去分辨。

**分類清單（依 Preprocessing B schema v3 定案名稱，2026/08/23 更新）**：

Preprocessing B 的 schema 已於 8/21 收斂為 v3 並定案，共 **10 種邊型別**。本表原為 schema 定案前的暫定版本，現改用實際名稱；名稱一律沿用 schema 的 UPPER_SNAKE 寫法，不另創別名（Kickoff 第 3.1 節）。

|類別|schema v3 關係型別|處理方式|
|-|-|-|
|安全相關關係（高／中高風險）|`CONTRAINDICATED_FOR`、`CAUTION_FOR`、`INDUCES`、`INTERACTS_WITH`、`RISK_FACTOR_FOR`、`TRIGGERS`|允許遍歷（allow-list）|
|安全輔助關係（中風險）|`REQUIRES_MONITORING`、`CAUSES_SIDE_EFFECT`|允許遍歷；`CAUSES_SIDE_EFFECT` 為一般副作用，可於純安全性查詢時降權|
|結構性階層關係|`IS_A`|**必須允許遍歷，且不計入跳數預算**（理由見下）|
|一般衛教關係|`TREATS`|依查詢類型決定是否允許（衛教型查詢允許，純安全性查詢可排除）|

**三點與原暫定清單的實質差異**：

1. **原表的 `causes_adverse_event` 在 schema 中不存在，且對應到兩條風險等級不同的邊。** schema v3 刻意把「藥物引發的**嚴重**病況」（`INDUCES` → `Condition`，仿單黑框警語等級，高風險）與「一般副作用」（`CAUSES_SIDE_EFFECT` → `Symptom`，中風險）分開，因為兩者下游處理方式不同。合成一條會讓乳酸中毒與腸胃不適同級。
2. **原表的 `indicated_for`、`manufactured_by`、`marketed_as`、`priced_at` 在 schema 中皆不存在。** 適應症已由 `TREATS` 承擔；製造商／商品名／價格從未被建模（`Substance` 節點一律以單一活性成分為單位，不建品牌／複方節點），因此 deny-list 中無需列出這三項。
3. **`CAUTION_FOR` 必須與 `CONTRAINDICATED_FOR` 分開處理**（呼應 A 成員的 Relation Boundary）。schema v3 之所以獨立成邊而非欄位，正是為了讓本節的關係型別排除能直接按型別篩選，不必逐筆檢查欄位。查詢「能不能吃 metformin」時兩者都要撈——`eGFR<30 禁忌`與`eGFR 30–45 不建議、需減量`是分級答案，只回其中一條都會失真。

**為何 `IS_A` 必須列入 allow-list（重要）**：

`IS_A`（如 `Canagliflozin IS_A SGLT2抑制劑`）在 schema 中被歸為「非安全關鍵」，容易被誤判為可排除的結構性關係。但實際情形相反：**部分安全警訊是對整個藥物類別發出的**，例如 TFDA 的 SGLT2 抑制劑酮酸中毒公告。若 `IS_A` 被排除，使用者以成分名查詢（「canagliflozin 會不會酮酸中毒」）時，類別層級的事實將永遠檢索不到——Preprocessing B 加這條邊就是為了堵這個安全漏洞，在 Boundary 層排除它等於把漏洞原封不動打開。

**節點型別的對應調整**：`TRIGGERS` 的主詞是 schema v3 的 `Trigger` 節點型別（外在／情境性誘因，如減少進食、自行減少胰島素劑量），與 `RISK_FACTOR_FOR`（`Condition → Condition`，內在／生理性成因）平行但主詞型別不同。因此 1.4 節的節點型別 allow-list 必須同時納入 `Trigger`，否則本節允許了 `TRIGGERS` 邊也走不通。

\---

### 1.4 其他建議的執行階段機制（待與其他組確認範圍）

|機制|說明|需要協調的對象|
|-|-|-|
|Top-K / Top-N 截斷|每個 Retriever 各自先截斷候選數量|與 Multi-RAG B 成員的「合併策略」可能重疊，需釐清截斷放在 Boundary 還是 Multi-RAG|
|節點型別限制（entity type allow-list）|**依 schema v3 定案的 6 種節點型別**：`Substance`、`Condition`、`Symptom`、`LabParameter`、`Trigger`、`Intervention`——六種**全部允許**回傳，排除任何不屬於這 6 種的節點型別，降低外部資料被夾帶惡意內容的攻擊面。詳見下方說明|與 LLM 組 Prompt Injection 檢查（OWASP LLM01）的分工需確認：節點型別限制是否能取代／減輕 Context Gate 的 Prompt Injection 檢查負擔|

**節點型別 allow-list 的名稱更正**〔依 Preprocessing B schema v3 校對〕：本表原寫「Drug / Disease / Symptom」，係 schema 定案前的暫定寫法，與 v3 有四處實質落差，其中兩處會直接影響共用範例查詢：

|原寫法|schema v3|落差說明|
|-|-|-|
|`Drug`|`Substance`|v2 已更名並擴大範圍，涵蓋酒精等非藥品但會影響藥物作用的物質；沿用 `Drug` 會漏掉這類節點|
|`Disease`|`Condition`|v2 定名，同時涵蓋既有共病與藥物引發的嚴重病況（角色差異由邊承擔，不另開節點型別）|
|（缺）|`LabParameter`|**必須納入，否則共用範例查詢直接失效**：「腎功能不好可以吃 metformin 嗎」的核心事實是 `Metformin --CONTRAINDICATED_FOR--> LabParameter(eGFR, <30)`，eGFR 是檢驗指標而非疾病|
|（缺）|`Trigger`|v3 新增，`TRIGGERS` 邊的主詞型別；排除後 1.3 節允許的 `TRIGGERS` 邊無法遍歷|
|（缺）|`Intervention`|非藥物衛教建議（飲食控制、定期監測血糖）。本專案是**糖尿病病患衛教** chatbot，排除此型別等同排除衛教類回答的圖譜依據|

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
* \[TBD-需 8/24 會議當場指派]〔依 Preprocessing B 校對新增〕：**偵測到高風險事實（`CONTRAINDICATED_FOR` / `CAUTION_FOR` / `INDUCES`）之後，由誰負責讓 Generator 拒答或改口？** 依本文件的定位，Boundary 只做結構性篩選、不判斷內容對錯；LLM 組兩份文件的 LLM Judge 只判 relevant / sufficient / conflict；Preprocessing B 亦已聲明只負責「安全事實被正確抽取並可被檢索到」。三組目前**皆未認領**這一段，不能預設「圖譜裡有資料，安全性就自動達成」。此項為責任真空，非一般 TBD，建議列為 8/24 第一順位議題。
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
  節點型別 allow-list → Substance / Condition / Symptom / LabParameter /
                        Trigger / Intervention（eGFR 屬 LabParameter，必須在列）
  關係類型排除 → 允許沿 CONTRAINDICATED_FOR / CAUTION_FOR / INDUCES /
                  INTERACTS_WITH / RISK_FACTOR_FOR 等安全關係擴散；
                  IS_A 一併允許但不計跳數
  跳數限制 → 預算 2 跳：
             ① Metformin --CONTRAINDICATED_FOR--> LabParameter(eGFR<30)    〔1 跳〕
             ② Metformin --CAUTION_FOR--> LabParameter(eGFR 30–45，需減量) 〔1 跳〕
             ③ Metformin --INDUCES--> 乳酸中毒                             〔1 跳〕
             ④ 腎功能不全 --RISK_FACTOR_FOR--> 乳酸中毒                    〔1 跳，另一起點〕
             ①②為分級答案，兩條都要撈；③④由檢索端匯合成「為什麼危險」
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


### JSON Schema / Judge 相關文獻（邵崴版已引用，本文件沿用其可追溯數值）

6. Bhutton, H., Andrews, H., Wright, A., Dennis, G. **JSON Schema: Validation Vocabulary.** Draft 2020-12.
[https://json-schema.org/draft/2020-12/json-schema-validation](https://json-schema.org/draft/2020-12/json-schema-validation)
7. Nogueira, R. \& Cho, K. **Passage Re-ranking with BERT.** arXiv:1901.04085, 2019.
[https://arxiv.org/abs/1901.04085](https://arxiv.org/abs/1901.04085)

### 內部文件

8. 林子榕. **B：Contract Gate + Context Gate.**（本組對照、避免重複之依據文件一）
9. 邵崴. **B：Contract Gate + Context Gate — RAG 後資料品質閘門｜技術調查交付文件.**（本組對照、避免重複之依據文件二，本文件之信心門檻分流設計特別參照其 CRAG 閾值與「不可混用的分數」原則）
10. TFDA / 政府資料開放平臺 — 藥品安全資訊風險溝通資料（Preprocessing 團隊已沿用之資料來源）
[https://data.gov.tw/dataset/9573](https://data.gov.tw/dataset/9573)

