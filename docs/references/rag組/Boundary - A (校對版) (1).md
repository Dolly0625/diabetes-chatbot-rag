# RAG Boundary 設計階段邊界機制清單

> **修訂說明（2026/08/23，Preprocessing B 成員 Erich 校對）**：本檔為原 `MS1/Boundary - A.md` 的校對版，**僅補上與 Graph schema v3 對不上的事實面內容**。上方的邊界機制表格與文獻**完全未更動**，所有補充集中在新增的「校對補充」一節（Schema Boundary 定案內容、Source Boundary 跨機關標註、Temporal/Status 在 Graph 側的執行條件、Traversal 與抽取端拆分原則的相依性）。原作者請自行 diff 後決定採用範圍。

> **來源欄位說明：** 下列 Boundary
> 名稱是本專案為方便規格管理所整理的工程分類，並非每篇論文都使用完全相同的「X
> Boundary」術語。來源欄列的是可支持該設計概念的代表性論文；其中部分
> Boundary 是根據多篇 RAG / GraphRAG 方法歸納出的設計原則。

  ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  邊界機制       直接作用對象   設計理由                                                          對應影響／改動代價                                                                                            來源（代表性論文）
  -------------- -------------- ----------------------------------------------------------------- ------------------------------------------------------------------------------------------------------------- ----------------------------------------------
  **Source       Vector + Graph 限定 RAG 可使用的可信資料來源，例如                               新增或移除來源時，需要重新確認來源可信度；若已建立索引，可能需重新進行資料清理、Chunking、Embedding、Vector   **Dai et al. (2026), *Careful Queries,
  Boundary**                    TFDA、openFDA、糖尿病學會資料，避免未審核來源進入正式檢索範圍。   Index 或 Graph 建置。                                                                                         Credible Results: Teaching RAG Models Advanced
                                                                                                                                                                                                                Web Search Tools with Reinforcement Learning*
                                                                                                                                                                                                                (AAAI 2026)**：提出 WebFilter，以
                                                                                                                                                                                                                source-restricted queries
                                                                                                                                                                                                                與不可靠內容過濾控制檢索來源。

  **Domain /     Vector + Graph 將知識範圍限制在糖尿病病患衛教相關內容。目前 TFDA 129             若未來從糖尿病擴展至其他疾病，需要調整範圍過濾規則，並重新處理相關 Vector / Graph 資料。                      **Poliakov & Shvai (2024), *Multi-Meta-RAG:
  Scope                         筆資料已排除大量非糖尿病藥物，避免無關內容進入檢索空間。                                                                                                                        Improving RAG for Multi-Hop Queries using
  Boundary**                                                                                                                                                                                                    Database Filtering with LLM-Extracted
                                                                                                                                                                                                                Metadata***：利用 metadata/database filtering
                                                                                                                                                                                                                縮小候選文件範圍。此處的糖尿病 scope filter
                                                                                                                                                                                                                是依相同「先縮小檢索空間」概念做專案化設計。

  **Schema       Graph          限定 Graph 可接受的 Entity、Relation 與合法結構。目前 Graph       新增 Entity / Relation 類型時，需要修改 schema、抽取規則與既有 Graph，也可能影響 Graph Retriever、Retrieved   **Edge et al. (2024), *From Local to Global: A
  Boundary**                    schema 已由真實文件試抽後收斂，避免 LLM                           Chunk 欄位及 Contract Gate。                                                                                  Graph RAG Approach to Query-Focused
                                抽取時任意產生未定義關係。                                                                                                                                                      Summarization***：以來源文件抽取 entity
                                                                                                                                                                                                                knowledge graph；另可參考 **Nishida et
                                                                                                                                                                                                                al. (2026), *Dissecting GraphRAG*** 對
                                                                                                                                                                                                                GraphRAG knowledge structuring
                                                                                                                                                                                                                的模組化分析。Schema Boundary 是在此類 Graph
                                                                                                                                                                                                                建構流程上加入固定 schema 的工程約束。

  **Relation     Graph          區分 Graph 中不同 Relation 的語意與安全強度，例如 `CAUTION_FOR`   新增、刪除或重新分類 Relation 時，需要調整 Graph Retrieval、Query routing，並重新檢查既有三元組。             **Zhu et al. (2025), *Knowledge Graph-Guided
  Boundary**                    與 `CONTRAINDICATED_FOR`                                                                                                                                                        Retrieval Augmented Generation (KG²RAG)*
                                不應視為相同關係，避免將「注意／劑量調整」與「禁忌」混淆。                                                                                                                      (NAACL 2025)**：利用 KG 中的 fact-level
                                                                                                                                                                                                                relationships 進行 graph-guided expansion 與
                                                                                                                                                                                                                retrieval。Relation Boundary 是依此類
                                                                                                                                                                                                                relationship-aware retrieval 再加入「哪些
                                                                                                                                                                                                                relation 可用／如何區分」的專案規則。

  **Traversal    Graph          限制 Graph Retriever                                              修改最大 traversal depth 會影響召回範圍、查詢效能及候選結果數量。實際 hop 數仍需透過 Retrieval 測試確認。     **Zhu et al. (2025), *KG²RAG* (NAACL
  Boundary**                    可向外延伸的最大範圍，避免經過過多節點後取得與 Query                                                                                                                            2025)**：由 semantic seed chunks 取得相關
                                關聯較弱的資訊，並控制 Graph Retrieval 的搜尋空間。                                                                                                                             subgraph，再以 graph traversal 擴展相關
                                                                                                                                                                                                                chunks。論文支持 graph-guided expansion /
                                                                                                                                                                                                                traversal；「最大
                                                                                                                                                                                                                hop」則是本專案為控制擴張範圍所加的 Boundary
                                                                                                                                                                                                                設計，並非論文原名。

  **Temporal /   Vector + Graph 利用 `version`、`date`、`status` 控制資料有效性，避免 `revoked`   需要維護文件版本與狀態 metadata；資料更新時需同步更新 Vector Index 或 Graph 中的對應資料。                    **Han et al. (2025), *RAG Meets Temporal
  Status                        或 `superseded` 資料因相關度較高而被採用。                                                                                                                                      Graphs: Time-Sensitive Modeling and Retrieval
  Boundary**                                                                                                                                                                                                    for Evolving Knowledge
                                                                                                                                                                                                                (TG-RAG)***：將時間資訊納入 GraphRAG
                                                                                                                                                                                                                表示與檢索，以處理持續演變的知識。另可參考
                                                                                                                                                                                                                **Zhu et al. (2025), *Right Answer at the
                                                                                                                                                                                                                Right Time --- Temporal RAG via Graph
                                                                                                                                                                                                                Summarization (STAR-RAG)*** 的 time-consistent
                                                                                                                                                                                                                retrieval。`active/revoked/superseded`
                                                                                                                                                                                                                是本專案依 temporal/freshness
                                                                                                                                                                                                                原則設計的狀態規則。
  ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

## 校對補充：與 Graph schema v3 對照後的事實修正

> 由 Preprocessing B 成員（Graph 管道）依 `MS1/Preprocessing - B.md` schema v3
> 校對，僅修正事實面落差，不更動上表的分類架構與論述。

**① Schema Boundary — schema 定案內容補實**

上表「Graph schema 已由真實文件試抽後收斂」正確。具體版本為 **schema v3**，
以 4 篇真實文件（openFDA metformin/ZITUVIMET 仿單、TFDA canagliflozin/dapagliflozin
急性腎損傷公告、TFDA SGLT2 酮酸中毒公告、TFDA Insulin 皮膚澱粉樣變性症公告）
手動試抽後收斂，第 4 篇未再產生新落差。定案內容為：

- **節點型別 6 種**：`Substance`、`Condition`、`Symptom`、`LabParameter`、
  `Trigger`、`Intervention`
- **邊型別 10 種**：`CONTRAINDICATED_FOR`、`CAUTION_FOR`、`INDUCES`、
  `CAUSES_SIDE_EFFECT`、`REQUIRES_MONITORING`、`TREATS`、`INTERACTS_WITH`、
  `RISK_FACTOR_FOR`、`TRIGGERS`、`IS_A`

**② Source Boundary — 需補「跨法規機關引用標註」規則**

上表將 TFDA、openFDA、糖尿病學會並列為可信來源。事實面需補一點：
**openFDA 是美國 FDA 仿單，不是 TFDA 資料**，且本組共用範例查詢的主角
metformin **不在** TFDA 129 筆資料集內，目前唯一來源即為 openFDA。

因此 Source Boundary 除了「哪些來源可用」之外，還需包含一條標註規則：
**跨法規機關引用時須於文件與簡報中註明機關別**（例：「美國 FDA 仿單，
交叉引用」），不得包裝成 TFDA 資料——否則將牴觸 Kickoff 第 6 節第 3 條
「每項技術主張皆附可查來源」。

**③ Temporal / Status Boundary — Graph 側目前無法執行**

上表以 `version` / `date` / `status（active / revoked / superseded）` 作為
有效性控制欄位。事實面落差：**Preprocessing B 的三元組契約（`entities` /
`relations`）目前不含這三個欄位**，來源資訊僅有 `source` + 段落 + 理由
+ `additional_sources`。TFDA 風險溝通公告本身帶有發布日期、且會被後續公告
取代，因此此 Boundary 在 Graph 側目前屬「已設計、未具備執行條件」。

**[TBD-需 8/24 M1 會議確認]** 版本／狀態欄位應由 Preprocessing 在三元組層
產出，或由 Multi-RAG B 在第 3.3 節 Chunk 層統一帶入。

**④ Traversal Boundary — 最大深度須與抽取端的事實拆分原則一併驗證**

Preprocessing B 的抽取原則 2 明訂：條件式／三方風險關係**不造三元邊**，
而是拆成兩條獨立事實（如 `Substance --INDUCES--> Condition` 與
`Condition --RISK_FACTOR_FOR--> Condition`），**預期由檢索端多跳重組**。
最大遍歷深度若設得過緊，等同於在檢索層關掉抽取層刻意保留的組合能力。

**[TBD-需 8/24 M1 會議確認]** 最大深度須以共用範例查詢（Metformin + 腎功能）
實測後定案，不宜僅依「屬 1～2 跳查詢」的直觀判斷。另註：`IS_A` 屬結構性
階層邊（成分 ⊂ 藥物類別），與語意跳躍性質不同，是否計入深度預算需一併決定
（見 B 成員文件 1.2、1.3 節）。

## CRAG-style Evaluator 與 Boundary 的關係

上述設計階段 Boundary 主要直接限制 **Vector / Graph Retriever
的知識與檢索空間**。CRAG-style Evaluator 位於 Retrieval
之後，負責評估已找回文件的品質與相關性。

代表性來源：

**Yan et al. (2024), *Corrective Retrieval Augmented Generation
(CRAG)***：提出 lightweight retrieval evaluator，對 retrieved documents
產生 confidence degree，並依評估結果觸發不同 retrieval actions。

因此，本專案中可將角色區分為：

-   **Boundary：** 規定 Retriever 可以觸及什麼。
-   **Vector / Graph Retriever：** 在 Boundary 允許的空間內搜尋。
-   **CRAG-style Evaluator：** 評估已找回的結果。
-   **Evaluator threshold 等執行規則：** 歸入後續 Runtime Constraint。
