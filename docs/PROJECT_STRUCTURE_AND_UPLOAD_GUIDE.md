# TFDA 糖尿病安全防護與看診前整理系統 — 專案架構全貌與 GitHub 上傳指南

本文件為開發團隊、架構審查者與維運人員提供**全專案深度解析**，包含：
1. **各資料夾與每一個關鍵檔案的具體職責與技術實作**。
2. **`orchestrator.py` 核心調度器的架構設計剖析與未來演進路線**。
3. **端到端資料流轉與四大安全閘門機制**。
4. **GitHub 上傳與資安過濾清單（哪些要上傳 vs 哪些嚴禁上傳）**。

---

## 🧭 一、全專案整體目錄架構

```text
tfda-diabetes-agent/
├── tfda_context_gate/        # 核心架構：A→B→C→D 四道安全閘門與業務邏輯引擎
├── line_bot/                 # 應用入口：FastAPI 伺服器、LINE Webhook 與網頁前端
├── diabetes-rag/             # 子模組：Vector + 知識圖譜 RRF 檢索大腦 (Git Submodule)
├── docs/                     # 專案文件：提案書、架構規範、API 規格與交接紀錄
├── fixtures/                 # 測試資源：藥袋相片範例、模擬資料集
├── scripts/                  # 工具腳本：LINE 圖文選單生成、自動化維運
├── agent/                    # 代理人評測結構定義與測試案例
├── experiments/              # 演算法實驗、評測與消融分析紀錄
├── archive/                  # 歷史封存：舊版交接紀錄與階段性產出
└── data/                     # 資料存放區（包含 SQLite 資料庫與向量快取，不上傳）
```

---

## 🏛️ 二、各資料夾與檔案詳細功能解析

---

### 1. `tfda_context_gate/`（核心醫療安全閘門與對話引擎）

這是整個系統最重要的核心套件，實現了嚴格的四道安全閘門、對話狀態機與臨床安全防護：

#### 🚪 (1) `a_router/`（Gate A 意圖與安全路由）
* `router.py`：`LangChainSignalExtractor`，調用 LLM 判定使用者意圖標籤（如衛教 `GENERAL_EDUCATION`、藥物諮詢 `MEDICATION_INFO`、看診整理 `PRE_VISIT_INTAKE`、閒聊 `NON_MEDICAL`）。
* `policy.py`：決策政策層。若偵測到急症標記（`POSSIBLE_EMERGENCY`）或提示注入（Prompt Injection），給予「一票否決」，決定是否允許進入 RAG（`rag_allowed`）。
* `rules.py`：確定性規則萃取器，呼叫急症政策進行正則攔截。
* `schemas.py`：定義 Gate A 的輸出結構 `AResult`（包含 `intent_tags`、`risk_flags`、`router_status`）。

#### 🛡️ (2) `b_context_gate/`（Gate B 知識邊界守門員 — 防幻覺）
* `gate.py`：驗證 RAG 檢索出的證據。強制檢查 15 個正規化欄位，過濾不相關或低信心資料。
* `adapters.py`：`normalize_evidence`，將不同來源（TFDA 仿單、國健署專書）的多鍵值資料轉為統一的 `CanonicalEvidence` 格式。
* `schemas.py`：定義 `CanonicalEvidence` 與 `BResult`（核准通過的證據清單）。

#### ✍️ (3) `c_generator/`（Gate C 證據受限生成器）
* `workflow_adapter.py`：`LangChainCV2Generator`，將 Gate B 核准的官方依據包裝成 Prompt 餵給 LLM 生成衛教回答。
* `schemas.py`：定義衛教回覆格式 `EvidenceAwareV2Answer`（要求模型必須標註引用來源 `citations` 與免責聲明）。

#### 🔒 (4) `d_output_gate/`（Gate D 輸出稽核防線）
* `gate.py`：8 道確定性輸出過濾器：
  1. 檢查是否違反診斷禁令（禁止給出確定診斷）。
  2. 檢查是否違反處方禁令（禁止給出劑量調藥指示）。
  3. 檢查藥品名稱是否合規（杜絕模型自造藥名）。
  4. 個資遮蔽檢查。
* `schemas.py`：定義 `DResult`（記錄 8 道檢查是否通過）。

#### 📈 (5) `e_observability/`（Gate E 全鏈路可觀測性）
* `collector.py`：記錄請求從進來到出去的完整生命週期。
* `schemas.py`：記錄各模組耗時（`staged_latency`）、狀態流轉與審計 Trace。

#### 🚨 (6) `clinical_safety/`（臨床安全與急症紅旗）
* `risk_policy.py`：定義 12 大類危急急症正則庫（胸痛、呼吸困難、嚴重低血糖、冒冷汗、意識不清、傷口壞疽），並實作 `_is_general_education_inquiry` 區分真急症發作 vs 知識提問。

#### 📋 (7) `intake/`（看診前整理室狀態機）
* `state.py`：3-Stage 結構化問卷狀態機（Stage 1: 用藥/過敏/慢病/家族史；Stage 2: 發病/症狀/程度；Stage 3: 提問/摘要）。
* `candidate_merge.py`：病患回覆資訊提取器（自動清洗「我有、對...」雜訊前綴，去重合併）。
* `summary.py`：將收集到的 8 個欄位轉換為「病患版確認摘要」與「醫護版專業報告」。
* `fhir_bundle.py`：將問卷摘要轉為國際醫療標準 HL7 FHIR Bundle JSON 格式。

#### 💬 (8) `line_orchestration/`（LINE 對話編排與真人同理心）
* `orchestrator.py`：對話總調度器，協調問卷狀態機、RAG 引擎、非同步長任務推播與 Session 保存（詳見第三章深度專題解析）。
* `response_composer.py`：擬真衛教師語氣生成器（對病患的「口渴/頻尿/疲倦」主動表達同理關懷，消除機械化題號）。

#### 🔎 (9) `rag/`（RAG 檢索適配器）
* `diabetes_rag_retriever.py`：對接外部獨立 submodule `diabetes-rag` 的核心轉換器。
* `hpa_retriever.py`：本地 `MultiSourceRetriever`，包含 Ollama `bge-m3` 向量快取備援機制。
* `tfda_retriever.py`：TFDA 藥品仿單專用檢索器。

#### 🔑 (10) `product_session/` & `sharing/`（會話與閱後即焚分享）
* `product_session/repository.py`：SQLite 對話持久化存儲（儲存對話進度、IntakeSnapshot）。
* `sharing/service.py`：產生給醫師的一次回饋分享碼（Token）、管理 15 分鐘過期與閱後即焚銷毀機制。

#### ⚙️ (11) `workflow/`（LangGraph 工作流組裝）
* `runner.py`：`run_workflow` 與 `stream_workflow`，整個 A→B→C→D 的一鍵執行與串流進入點。
* `graph.py`：使用 `langgraph` 定義的狀態圖節點與邊緣走向。
* `formal_factory.py`：正式版組件工廠（負責組裝 A-Extractor、RAG-Retriever 與 C-Generator）。

---

### 2. `line_bot/`（對外 Web 服務與使用者介面）

* `app.py`：FastAPI 主伺服器（`/callback`、`/api/patient/previsit-room/chat/stream`、`/api/clinician/*`）。
* `ui.py`：LINE Flex Message 視覺卡片產生器。
* `static/previsit-room.html`：病患專用看診整理對談室網頁。
* `static/clinician.html`：醫護人員專用調閱後台。
* `static/jsqr.min.js`：純前端離線 QR Code 解碼函式庫。

---

### 3. `diabetes-rag/`（子模組：RAG 檢索大腦）

* `src/rag_retrieval/tool.py`：檢索總入口 `EvidenceRetrievalTool`。
* `src/rag_retrieval/embedding.py`：調用 Google Gemini API（`models/gemini-embedding-2`）計算 3072 維度向量。
* `src/rag_retrieval/retrievers/vector.py`：純 numpy 矩陣運算，快速計算餘弦相似度排名。
* `src/rag_retrieval/retrievers/graph.py`：TFDA 藥品知識圖譜比對器。
* `src/rag_retrieval/fusion.py`：RRF (Reciprocal Rank Fusion) 雙軌排名融合演算法。
* `src/rag_retrieval/data/`：包含 29 筆圖譜三元組、國健署專書與 254 筆已 Embedding 衛教庫。

---

## 🔬 三、深度專題：`orchestrator.py` 架構設計與演進藍圖

### 1. 為什麼 `orchestrator.py` 有 3,600 多行？
它是系統面對真實病患與 LINE 通訊時的**「權威調度中心（Authoritative Central Orchestrator）」**，集中解決了 5 大硬核工程挑戰：
1. **併發限流與冪等性防重複（L1 ~ L800）**：全局 Semaphore 限制並發最多 5 筆，防止大量請求打垮 LLM；雙重 TTL 消除 LINE Webhook 重發造成的重複回答。
2. **LINE 1 秒超時之非同步長任務推播（L801 ~ L1520）**：解決 RAG/LLM 生成耗時 2~5 秒問題，先回傳即時確認語，背景完成後以 Push Message 補發，內建重試與降級。
3. **隨意插話與代填管理（L1521 ~ L2680）**：精準辨識病患在問卷填寫中途突然詢問藥物（Mixed Intent），能先解答再溫柔導回問卷；支援「幫家人問」的對象切換。
4. **8 大問卷欄位海量口語正規化（L2681 ~ L3460）**：收斂「我都沒吃」、「不曉得」、「二星期前」、「痛到走不動」等口語變體為臨床標準值。
5. **Session 持久化與斷點續填（L3461 ~ L3624）**：進度寫入 SQLite，病患中斷後重啟能主動詢問「繼續填寫 vs 重新開始」；強制個資 SHA-256 雜湊。

### 2. 架構有效性評估
* **當前評估**：**極度有效**。在醫療場景中，單一狀態機杜絕了狀態分散造成的時序競爭（Race Condition）與個資洩漏，是全系統 **707 個測試全綠、0 報錯** 的關鍵基石。
* **未來 v0.2 重構藍圖（模組化拆分路線）**：
  ```text
  tfda_context_gate/line_orchestration/
  ├── async_push_manager.py     # 負責 LINE 超時與背景推播（約 700 行）
  ├── intake_normalizer.py      # 負責 8 大欄位口語解析（約 800 行）
  ├── dialogue_interrupt.py     # 負責插話與混合意圖辨識（約 600 行）
  ├── session_checkpointer.py   # 負責 SQLite 狀態保存與斷點續填（約 300 行）
  └── orchestrator.py           # 瘦身為純狀態調度器骨架（約 400 行）
  ```

---

## 📤 四、GitHub 上傳確認清單（要上傳 vs 嚴禁上傳）

### ✅ 1. 必須上傳的目錄與檔案（Version Controlled）
| 目錄 / 檔案 | 說明 |
| :--- | :--- |
| `tfda_context_gate/` | 全部原始碼與測試（核心邏輯） |
| `line_bot/` | 全部伺服器與前端網頁檔案（包含 `jsqr.min.js` 與 HTML） |
| `diabetes-rag` | 子模組關聯指標（`.gitmodules` 與 submodule commit） |
| `docs/` | 所有架構與說明文件 |
| `fixtures/` | 測試所需的藥袋相片與假資料 |
| `scripts/` | 圖文選單與自動化工具 |
| `agent/` & `experiments/` | 評測結構與實驗紀錄 |
| `.gitignore` | Git 忽略規則定義檔 |
| `requirements.txt` | Python 相依套件清單 |
| `README.md` & `AGENTS.md` | 專案說明與 Agent 協同規範 |

---

### 🚫 2. 嚴禁上傳的檔案（已由 `.gitignore` 自動阻擋）
| 檔案 / 目錄 | 為什麼不能上傳？ | 目前防護狀態 |
| :--- | :--- | :---: |
| `.env` | 包含 `OPENCODE_API_KEY`、`GEMINI_API_KEY` 等真實金鑰 | 🛡️ 已忽略 |
| `data/processed/*.sqlite3` | 包含病患在本地測試時輸入的個資與對話 Session | 🛡️ 已忽略 |
| `data/processed/*.sqlite3-wal` | SQLite 交易暫存日誌 | 🛡️ 已忽略 |
| `data/processed/*.sqlite3-shm` | SQLite 共享記憶體暫存 | 🛡️ 已忽略 |
| `data/processed/.vector_cache/` | 本機編譯之二進位向量快取檔（體積過大且為衍生品） | 🛡️ 已忽略 |
| `__pycache__/` & `*.pyc` | Python 編譯暫存位元組碼 | 🛡️ 已忽略 |
| `.venv/` | 本地虛擬環境目錄 | 🛡️ 已忽略 |

---

## 🚀 五、標準 Git 提交與推送指令

當您確認完畢後，可以使用以下標準指令安全提交並上傳：

```bash
# 1. 確保子模組狀態乾淨
cd diabetes-rag && git add src/rag_retrieval/data/__init__.py && git commit -m "fix(data): add __init__.py for resource loading" || true
cd ..

# 2. 暫存主專案變更
git add .

# 3. 檢查是否有不小心暫存到敏感檔案（確認不含 .env 或 .sqlite3）
git status

# 4. 提交版本
git commit -m "feat: complete previsit-room SSE, diabetes-rag integration, empathetic composer, and QR scanner"

# 5. 推送至遠端
git push origin previsit-room-sse-integration
```
