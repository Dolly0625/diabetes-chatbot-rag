# CLAUDE.md — `rag_retrieval` 建置說明

你正在建置這個 repository。動手寫程式之前，請把這份文件完整讀過一遍。
Erich（RAG 組組長，Preprocessing B）是本 repo 的負責人。這裡寫的一切都已
拍板定案——真正還沒定案的地方會標上 **[DECIDE]**，遇到時務必先問過再選。

---

## 1. 這是什麼

一個 **Python 套件**，把 RAG 組現有的資料資產整合成一個可呼叫的檢索工具，
供 LLM 組的 LangGraph 管線使用。

```python
from rag_retrieval import EvidenceRetrievalTool

tool = EvidenceRetrievalTool(source_id="tfda+hpa")
response = tool.retrieve(request)      # RetrievalRequest -> RetrievalResponse
```

它**不是** HTTP 服務。LLM 組會直接 `import` 它，在自己的行程裡執行。這一點
已於 2026-08-29 與他們確認過。

**硬性截止時間：2026-09-03 14:00**（聯合展示，MB206）。可運作的版本必須在
**9/2** 前交到 LLM 組手上，才不會在展示現場才第一次嘗試整合。

### 這份說明必須撐過的展示腳本

由實驗室學長（子龍學長）現場提出兩個問題，之後他會隨機考問組員系統如何
運作：

1. **「如何施打胰島素？」** → 絕不能給出醫療指示。系統要拒答並導向仿單。
   RAG 的任務：回傳胰島素注射部位的證據，並附上正確的風險等級；拒答本身
   是 LLM 組 Output Gate 的責任。
2. **「糖尿病平常飲食要注意什麼？」** → 直接回答，語氣受控。
   RAG 的任務：回傳國健署手冊裡的衛教 chunk。

兩題都必須成功。第二題是較新的能力——衛教語料是 8/29 才加進來的。

---

## 2. 不可妥協的原則

違反以下任何一條，破壞的不只是測試，而是跨組之間的協議。

1. **契約是凍結的。** `../02_MS2_demo/contract/CONTRACT_v1.md` 是唯一的
   權威來源，已與 LLM 組議定。不可新增、改名或刪除任何欄位。若覺得契約
   有問題，**先停下來問**——不能單方面更動。
2. **Python 3.10。** 不可使用 3.11+ 的語法。LLM 組在本機用 3.9.6 驗證，
   還會另外跑一次 3.10 相容性檢查，所以任何邊緣寫法都要避免。
3. **Pydantic v2** 語意。
4. **絕不能把例外丟到呼叫端的行程裡。** 任何失敗——不合法的 schema、
   缺少的 key、內部例外——都要變成一個合法的 `RetrievalResponse`，
   `retrieval_status="ERROR"`、`chunks=[]`。要怎麼處理由 LLM 組的
   Context Gate 決定。traceback 逃出 `retrieve()` 就是一個 bug。
5. **`chunks` 永遠是 list，絕不是 `null`**——包含 `EMPTY` 與 `ERROR` 狀態
   在內。
6. **風險等級掛在每個 chunk 上。** `chunk.evidence_risk_level` 才是權威值，
   `response.max_evidence_risk_level` 只是摘要，必須是計算出來的，絕不能
   憑空給定。LLM 組已明確拒絕單一 response 層級的風險值這種設計。
7. **風險是查表得出的，不是判斷出來的。** `relation → evidence_risk_level`
   對照表來自 CONTRACT_v1 §2.5。絕不讓 LLM 推論風險等級，也絕不把
   `CAUTION_FOR` 升級成禁忌。
8. **repo 裡不能有機密資訊。** `GEMINI_API_KEY` 一律從環境變數讀取。
   實驗室有提供 key，只能傳入使用，絕不能提交進版控。確認
   `../pipelines/graph_pipeline/.env` 沒有被複製進來。
9. **Vector chunk 的風險等級是 `UNKNOWN`**，絕不是 `LOW`。沒有 relation
   不代表風險低。
10. **每個回應都要帶 `SOURCE_NOT_CLINICALLY_REVIEWED`。** 本專案沒有臨床
    審核人員。這是刻意保留、必須維持可見的警示。

---

## 3. 東西在哪裡

這個 repo 雖然放在專案資料夾底下，但**是獨立的 git repo**。以下路徑都是
相對於這份檔案的位置。這個 repo 以外的一切都是**唯讀的參考資料**——可以
自由閱讀，但絕不能修改。

| 需要什麼 | 路徑 |
|---|---|
| **契約（權威版本）** | `../02_MS2_demo/contract/CONTRACT_v1.md` |
| 完整的請求／回應範例 | `../02_MS2_demo/contract/examples/*.json` |
| 現況評估與待辦優先順序 | `../02_MS2_demo/notes/2026-08-29_status_and_todo.md` |
| 發給 LLM 組的訊息 | `../02_MS2_demo/notes/msg_to_llm_group_interface.md` |
| **LLM 組的欄位字典**（intent_tags、risk_flags、router_status、reason_codes） | `../reference/LLM 組/LLM組與RAG組對齊問題.md` §3.2 |
| LLM 組的架構與工具設計 | `../reference/LLM 組/糖尿病多工具Agent提案書_V0.md` §5.3 |
| M1 報告——風險對照表 §3.6、envelope §4.1、所有待解問題 | `../01_MS1_archive/RAG 組 — M1 工作報告與計畫修訂.md` |
| Chunk JSON Schema（draft-07） | `../01_MS1_archive/revised/Multi-RAG - B (校對版).json` |
| 邊界規則（allow-list、門檻值、hop 上限） | `../01_MS1_archive/revised/Boundary - B (校對版).md` |
| 失敗模式與測試分類 | `../01_MS1_archive/revised/Boundary - C (校對版).md` |
| 查詢類型 → 路由分類（10 類） | `../01_MS1_archive/revised/Multi-RAG - A (校對版).md` |
| Graph schema v3 的設計理由 | `../01_MS1_archive/Preprocessing - B.md` |
| 8/26 會議紀錄（目標為何改變） | `../00_admin/medical-ai-meeting-minutes-0826.md` |

**不要**搜尋 `../reference/DCSS_*`——那是另一個專案（失智症），只是拿來當
schema 參考，跟本專案無關。

---

## 4. 資料資產

| 資產 | 數量 | 路徑 |
|---|---|---|
| 已 embedding 的 chunk（Gemini，3072 維，已做 L2 正規化） | **85** | `../pipelines/vector_pipeline/embedded_chunks_output.json` |
| Semantic chunk（無向量） | 85 | `../pipelines/vector_pipeline/bronze_chunks_semantic.json` |
| 可檢索的三元組 | **29**（共擷取 37 筆） | `../pipelines/graph_pipeline/bronze_triples_retrievable.json` |
| 衛教語料（國健署《糖尿病與我》） | 21 份文件，約 30.9k 字元 | `../data/education_corpus/hpa_dm_book.json` |
| TFDA 原始語料 | 129 份文件（**只有 8 份與糖尿病相關**） | `../data/tfda_dataset_129/` |

在建置時把這個套件需要用到的資產複製進 `src/rag_retrieval/data/`，讓
`git clone` 之後這個套件本身就是自足的。embeddings 檔案約 5 MB——要提交
進版控。**不要**提交 `../糖尿病與我.pdf`（28 MB）。

### 關於資料，這些事你不該感到意外

以下都是已驗證過的事實，不是猜測。有幾項跟 MS1 文件所暗示的不一樣。

- **目前沒有任何可檢索的 `CONTRAINDICATED_FOR` 三元組。** 全部 8 筆都被
  可檢索性關卡濾掉了（它們來自複方藥品的展開）。目前從 graph 能到達的
  最高風險等級來自 `INDUCES`（5 筆）與 `TRIGGERS`（6 筆）。不要設計一個
  假設會回傳禁忌關係的展示路徑。
- 可檢索的 relation 數量：`TREATS` 9、`RISK_FACTOR_FOR` 7、`TRIGGERS` 6、
  `INDUCES` 5、`CAUTION_FOR` 2。**schema v3 十種 relation 裡有四種
  （`INTERACTS_WITH`、`CAUSES_SIDE_EFFECT`、`REQUIRES_MONITORING`、
  `IS_A`）完全沒有實例。** 即使如此仍要保留在查表裡。
- metformin／eGFR 30–45 的 `CAUTION_FOR` 三元組**是**可檢索的，是最適合
  展示「一般注意事項」的案例。
- `tfda-risk-019_sec3_04` 的內容是「訊息緣由：4.htm」——這是切塊器的一個
  URL 切割 bug，而且已經被 embedding 進去了。要嘛過濾掉、要嘛修正，不要
  讓它出現在展示畫面上。
- 有 9 個 chunk 短於 30 個字元。
- 兩條 pipeline 目前的日期格式仍是 `2016/7/14`。契約要求 `YYYY-MM-DD`。
  **要在這個套件的載入階段做正規化**——不要去改上游 pipeline 的輸出。
- 衛教語料目前是文件，不是 chunk。還需要切塊與 embedding（見下方步驟 6）。

---

## 5. 目標 repo 結構

```
rag_retrieval/
├── CLAUDE.md                     本檔案
├── README.md                     給 LLM 組看的：安裝、使用方式、範例
├── pyproject.toml                name=rag-retrieval, requires-python=">=3.10,<3.11"
├── .gitignore                    .env, __pycache__, *.pdf, .venv
├── src/rag_retrieval/
│   ├── __init__.py               匯出 EvidenceRetrievalTool
│   ├── contract/
│   │   ├── models.py             Pydantic v2：RetrievalRequest/Response, RetrievedChunk, Entity, Relation
│   │   ├── enums.py              CONTRACT_v1 裡所有固定的代碼
│   │   └── errors.py             error_response() 輔助函式
│   ├── gate_in.py                入口關卡 ←「自家大門」
│   ├── routing.py                intent_tags -> VECTOR/GRAPH/HYBRID
│   ├── retrievers/
│   │   ├── base.py               Retriever protocol
│   │   ├── vector.py             numpy cosine top-k
│   │   ├── graph.py              記憶體內鄰接表，1–2 跳
│   │   └── neo4j_backend.py      可選，同一套 protocol，不在關鍵路徑上
│   ├── fusion.py                 RRF
│   ├── risk.py                   relation -> risk 查表 + max_evidence_risk_level
│   ├── gate_out.py               信心門檻、allow-list、截斷
│   ├── loaders.py                載入並正規化資產（日期、壞掉的 chunk）
│   ├── embedding.py              Gemini query embedding，key 從環境變數讀取
│   ├── tool.py                   EvidenceRetrievalTool——整合協調
│   └── data/                     打包進套件的資產
├── tests/
│   ├── test_contract.py          5 個範例 JSON 必須能來回轉換不失真
│   ├── test_gate_in.py
│   ├── test_risk.py
│   ├── test_fusion.py
│   └── test_end_to_end.py        兩個展示問題
├── eval/
│   ├── queries.yaml               約 20 筆標註過、附預期 id 的查詢
│   └── run_eval.py                Recall@k / Precision / F1
└── scripts/
    └── build_index.py             把衛教語料切塊並 embedding
```

---

## 6. 建置順序

嚴格依序進行——每一步都要能單獨測試完才進下一步。每個步驟的邊界都要
commit。

| # | 步驟 | 完成標準 |
|---|---|---|
| 1 | `contract/`——enums、models、error 輔助函式 | `../02_MS2_demo/contract/examples/` 裡全部 5 個檔案都能解析成 model，再序列化回去內容不變 |
| 2 | `loaders.py`——載入 85 筆 chunk + 29 筆三元組，日期正規化為 ISO 格式，丟掉 `4.htm` 那個 chunk | Loader 測試斷言數量為 85/29，且沒有任何非 ISO 格式的日期 |
| 3 | `gate_in.py`——拒絕非 `G_GENERAL_EDUCATION`、驗證 schema、未知的 enum | 範例檔 `04_error_validation.json` 的兩種情況都能精準重現 |
| 4 | `retrievers/vector.py`——載入時對矩陣做一次正規化，cosine top-k | 查詢「飲食」會回傳飲食相關 chunk；延遲 < 50 ms |
| 5 | `retrievers/graph.py`——依查詢做 entity-label 比對，1–2 跳展開，輸出 graph chunk | metformin/eGFR 的 `CAUTION_FOR` 三元組能被查詢檢索到 |
| 6 | `scripts/build_index.py`——把衛教語料切塊、embedding，併入索引 | 衛教 chunk 可被檢索；飲食類展示問題會回傳國健署內容 |
| 7 | `risk.py`——查表 + `max_evidence_risk_level` | CONTRACT_v1 §2.5 裡每一種 relation 都對應正確；vector chunk 皆為 `UNKNOWN` |
| 8 | `fusion.py`——RRF，`k=60`，`w_graph > w_vector` | 排序穩定且有文件記錄；權重是具名常數，並附上說明安全考量的註解 |
| 9 | `gate_out.py`——信心門檻、node/relation allow-list、top-N 截斷 | 低信心的三元組會被捨棄，**且**會發出 `LOW_CONFIDENCE_EVIDENCE_DROPPED` 警告 |
| 10 | `tool.py`——串接整合 | 兩個展示問題都能端對端產生合理的回應 |
| 11 | `README.md` + 打包 | 全新 clone 下來後在 Python 3.10 上 `pip install -e .` 能正常運作 |
| 12 | `eval/`——標註資料集 + 指標 | 對約 20 個查詢印出 Recall@k / Precision / F1 |

**執行順序對截止時間至關重要。** 步驟 1–3 是 LLM 組開始整合所需要的，要
最先 commit 並 push 出去，甚至在檢索功能真正跑起來之前。

### 關鍵的順序限制

**信心門檻要在 RRF 融合之前執行，絕不能之後。** RRF 會捨棄分數大小、只看
排名，所以單一一筆低信心的 graph 三元組，只要在自己那條軌道排第一，就會
拿到那條軌道的最大融合貢獻值。順序是固定的：

```
gate_in → route → retrieve per track → threshold filter → rank → RRF fuse → truncate → risk annotate → gate_out
```

把這個順序寫成 `tool.py` 裡的註解。

---

## 7. 模組負責人

以下每個模組都有一份**由 Erich 寫好、可運作的預設實作**。組員可以用更好的
實作取代預設值。如果組員沒有交付，就維持預設值，展示不受影響——所以絕不
要把一個委派出去的模組當成必要的環節。

| 模組 | 負責人 | 沒交付時的預設值 |
|---|---|---|
| `contract/`、`gate_in`、`tool` | Erich——**不可委派** | — |
| `retrievers/vector`、`retrievers/graph`、`fusion` | Erich | — |
| `retrievers/neo4j_backend` | Preprocessing A | 記憶體內的 graph retriever |
| `routing` | Multi-RAG A | 永遠回傳 `HYBRID` |
| `risk` 查表 | Boundary A | Erich 依 CONTRACT_v1 §2.5 做的表 |
| `gate_out` 門檻值 | Boundary B | 寬鬆的預設值 |
| `eval/queries.yaml` | Boundary C | Erich 準備的 10 筆起始查詢集 |
| 衛教語料擴充 | Preprocessing A/B | 國健署那 21 份文件 |

當你要建置一個可委派的模組時，除了寫出預設實作，也要**留下明顯的介面
接縫**：一個在 `retrievers/base.py` 裡有文件說明的 protocol、一個常數區塊，
或一個資料檔——總之要讓組員能在不動到整合協調程式碼的情況下替換掉它。

---

## 8. Git 工作流程

repo 會 push 到實驗室的 GitLab。LLM 組透過 clone 這個 repo 來使用它。

- **`main` 永遠要保持展示可用狀態。** 絕不直接 commit 到 `main`。
- 分支命名：`feat/<module>`、`fix/<what>`、`chore/<what>`。組員分支：
  `member/<name>/<module>`。
- 只有在 `pytest` 通過、且兩個展示問題仍能端對端正常運作時，才能合併進
  `main`。
- Commit 訊息：祈使句、單行、有範疇——例如 `feat(fusion): add RRF with
  k=60 and graph-weighted tracks`。適用時要標註對應的建置步驟編號。
- 在交給 LLM 組的那個 commit 上打上 `v0.1-demo` 標籤。9/3 若出狀況，回滾
  就是回到這個 tag。
- **絕不對 `main` 做 force-push。**
- `.gitignore` 必須涵蓋 `.env`、`.venv`、`__pycache__`、`*.pdf`。第一次
  push 前先確認沒有 key 被提交進去——用 `git log -p | grep -i "api.*key"`
  檢查。
- 5 MB 的 embeddings 檔案要直接 commit。不要為此另外加 Git LFS；就一個
  檔案，實驗室的 GitLab 上多裝一個 LFS 只是展示前多一個可能出錯的東西。

---

## 9. 驗證

沒有實際跑過東西，不要回報某個步驟已完成。

- 每次合併進 `main` 之前，`pytest` 必須通過。
- 5 個契約範例是步驟 1 的驗收測試——把每一個都拿來實際做來回轉換，不要
  憑對 schema 的印象手寫斷言。
- 針對兩個展示問題，要把實際的回應 JSON 印出來看過。「測試通過」不等於
  「答案合理」。
- 步驟 12 之後，要老實回報實際數字。如果 Recall@5 是 0.6，就寫 0.6。
  **不要為了讓指標好看而去調整標註資料集**——這個 eval 之所以存在，就是
  因為實驗室學長曾具體批評過本組只描述失敗模式卻沒有量化，灌水的數字比
  偏低的數字更糟。

---

## 10. 範圍限制——這些事不要做

以下每一項要嘛已經明確延後，要嘛在 9/3 之前做了反而有害。

- **關鍵路徑上不要用 Neo4j。** 29 筆三元組不需要圖資料庫。這個規模下 dict
  就是正確答案，老實說出這一點比假裝不是這樣更站得住腳。Neo4j 只是一個
  可選的替代 backend。
- **不要做 Personalized PageRank。** 這是留給之後的設計方向，現在不實作。
- **不要做 UMLS／實體消歧。** 字串比對已經足夠處理 29–60 筆三元組。
- **不要換模型重新 embedding。** 語料是用 Gemini 3072 維做的。衛教語料也
  要用同一套，換模型代表所有東西都要重做一次。
- **不要加 rerank 階段。** `score_type: "rerank"` 留在 enum 裡，但不使用。
- **不要加 `retriever: "hybrid"` 這種 chunk 型別**，也不要對 vector chunk
  做 entity 豐富化。留到契約 v2 再做。
- **任何地方都不能出現臨床判斷的說法**——程式碼、註解、README 或輸出都
  一樣。
- **不要修改 `../01_MS1_archive/` 底下的任何東西。** 那是凍結的 MS1 提交
  紀錄。
- **不要修改上游 pipeline**，也就是 `../pipelines/graph_pipeline/` 和
  `../pipelines/vector_pipeline/`。資料問題要在 `loaders.py` 的載入階段
  修正，讓這個 repo 保持自足。
- 不要加入以下清單以外的依賴套件：`pydantic>=2`、`numpy`、
  `google-generativeai`、`pytest`。其他的都要先問過。

---

## 11. 9/3 的完成定義

- [ ] 在乾淨的 Python 3.10 環境下 `pip install -e .` 能正常運作
- [ ] 兩個展示問題都能回傳合理、附來源的證據
- [ ] 每一種失敗模式都會回傳合法的 `RetrievalResponse`，絕不丟出例外
- [ ] 非 `G_GENERAL_EDUCATION` 的請求會被 RAG 獨立拒絕
- [ ] 5 個契約範例都能來回轉換不失真
- [ ] `README.md` 清楚示範 LLM 組該如何呼叫這個套件，附一個完整範例
- [ ] Eval 數字如實回報
- [ ] 打上 `v0.1-demo` 標籤、已 push、已通知 LLM 組

---

## 12. 卡住的時候怎麼辦

遇到以下情況，去問 Erich，不要自己亂猜：

- 契約看起來要求了互相矛盾的東西
- 某項資料資產跟 §4 裡的數量對不上
- 某個步驟需要一個不在允許清單裡的依賴套件
- 你即將改動任何 LLM 組會依賴的東西

時間是硬性限制，程式碼品質不是。一條能跑的窄路，勝過一條做一半的廣路。
如果非砍不可，就砍功能——絕不要砍掉 `gate_in`、契約，或錯誤處理，因為
這些正是實驗室學長回饋裡真正在意的東西。

> **註：** 如果 git 回報 `index.lock` 已存在，執行一次 `rm -f
> .git/index.lock` 即可。透過 Claude 桌面版橋接執行 git 時，可能會留下
> 一個殘留的 lock 檔（因為它在這個資料夾沒有刪除權限）。用自己的終端機
> 跑 git 就沒有這個問題。
