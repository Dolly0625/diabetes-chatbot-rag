# Diabetes Chatbot RAG (糖尿病健康諮詢與看診前整理機器人)

> 結合 RAG 知識庫檢索、看診前問卷整理與藥袋 QR 解析的糖尿病 LINE 智慧健康助理 Demo。
> 本專案由 Agent 臨床安全閘門與對話編排，整合 RAG 組別研發之 diabetes-rag 檢索模組協同構建。

---

## 核心功能特色

### 1. 衛教與藥物諮詢（RAG 檢索模組）
* 模組化整合：內含 RAG 組別開發的 diabetes-rag 檢索引擎原始碼，clone 一次即可取得完整 Demo。
* 雲端向量檢索：採用 Google Gemini 雲端向量 API（models/gemini-embedding-2），無需在本地安裝 Ollama 或下載模型權重，具備 API Key 即可使用。
* 雙軌檢索架構：結合 Google Gemini 向量檢索（172 筆國健署衛教專書語料與 85 筆 TFDA 仿單）與 TFDA 藥品知識圖譜三元組，透過 RRF (Reciprocal Rank Fusion) 排名融合演算法進行召回。
* 知識邊界約束：透過安全閘門（Context Gate B）進行 15 欄位檢驗，依據官方證據產出衛教回答，並標註引用來源（如〔來源：E1、E2〕）與免責聲明。

### 知識庫處理管線標準規範 (RAG Knowledge Base & Pipeline Compliance)
專案內所有衛教專書與仿單語料，均嚴格遵守 RAG 組官方制訂之標準處理管線（`diabetes-rag/scripts/build_index.py`）進行建置與索引：
1. **語料來源**：
   * 衛生福利部國民健康署官方出版品《糖尿病與我》（涵蓋 21 篇完整衛教章節，包括：認識糖尿病成因、第 1 型／第 2 型分類、飲食原則、運動指引、用藥安全、低血糖緊急處置與共病照護）。
   * 衛生福利部食品藥物管理署（TFDA）藥品安全資訊與仿單警訊。
2. **切塊演算法（Chunking Algorithm）**：
   * 依段落與中文句尾標點符號（`。！？`）進行貪婪合併切分。
   * 單一 Chunk 長度嚴格限制在 **30 ～ 220 字**（避免過長稀釋語意或過短失去上下文）。
3. **向量嵌入規範（Embedding Specification）**：
   * 統一調用 Google Gemini Embedding 2（`models/gemini-embedding-2`，3072 維度）。
   * 建置語料庫時強制使用任務類型 `task_type="RETRIEVAL_DOCUMENT"`，檢索查詢時使用 `task_type="RETRIEVAL_QUERY"`，以維持向量空間檢索召回率一致性。
4. **重新建置指令**：
   ```bash
   export GEMINI_API_KEY="your_api_key"
   python3 diabetes-rag/scripts/build_index.py
   ```
   * 執行後自動將 21 篇專書切為 172 筆標準 Chunk 向量並寫入 `diabetes-rag/src/rag_retrieval/data/education_chunks_embedded.json`。
   * 確保所有新增衛教知識 100% 通過 RAG 單元測試與安全閘門（Gate B / Gate D）語意驗證。

### 2. 急性紅旗安全攔截（Fail-Closed 緊急防禦）
* 當系統正則與意圖規則偵測到急症關鍵字（如：胸口劇痛、呼吸困難、血糖過低冒冷汗、意識不清）時，立即中斷常規對話與 RAG 檢索，提供 119 與急診就醫引導。

### 3. 藥袋資訊解析（Medication Bag QR-First & OCR Prototype）
* QR Code 優先解析：支援病患在 LINE 聊天室傳送藥袋相片，系統優先解析台灣健保規範之藥袋處方 QR Code 與醫院網址，自動提取藥品名稱並帶入問卷。
* OCR 視覺辨識擴充介面：預留 OCR 視覺文字萃取與 TFDA 核心糖尿病藥品字典比對介面（目前為原型驗證階段，在無 GPU 或未安裝 PaddleOCR 環境下以 QR Code 解析為主）。
* 記憶體不留存防護：相片僅於記憶體中進行處方解析，解析完成即釋放，不持久化儲存病患原始照片檔案。

### 4. 看診前 3 階段對談室（Pre-visit Intake）
* 結構化問卷流程：
  - 第 1 階段：目前固定用藥（支援手動文字輸入或藥袋 QR 解析）、過敏史、過去病史、家族史。
  - 第 2 階段：發病時間、主要不適症狀描述、對生活影響程度。
  - 第 3 階段：就醫想詢問醫師的問題，完成後產生就醫摘要與 QR Code 分享碼。
* 支援對象辨識（本人填寫與家人代填）與斷點續填狀態恢復。

### 5. 醫護端調閱後台（Clinician Portal）
* 前端相機掃描：內建離線 jsQR 解碼函式庫，醫師可使用手機相機或筆電鏡頭掃描病患出示之 QR Code 分享碼，0.1 秒秒級辨識。
* 閱後即焚資安機制：分享碼具備 15 分鐘時效，後端兌換解密後立即標記為已使用並銷毀，防止二次調閱。
* 審計日誌：所有調閱操作均自動記錄於 audit_logs 資料庫。

### 6. 系統審計與全鏈路追蹤（Audit Logs & Latency Transparency）
* 毫秒級模組耗時分解：每次對話回覆後，系統自動送出全鏈路追蹤日誌，精確拆解 Gate A 安全掃描、語意意圖辨識（Semantic Router）、雙軌 RAG 知識檢索、Gate B 證據可靠度審核、Gate C 生成、對話狀態流轉與 Gate D 輸出防線之個別耗時與總耗時。
* 高度可解釋性與稽核軌跡：符合臨床醫療資安與 SHA-256 去識別化規範，提供落地評估之關鍵透明指標。

---

## 完整 Demo 操作成果報告 (Demo Walkthrough Report)

專案已完成包含病患端衛教整理與醫護端相機調閱之 **10 大閉環步驟實測與螢幕截圖**：
* **圖文完整報告**：詳見 [DEMO_WALKTHROUGH_REPORT.md](docs/DEMO_WALKTHROUGH_REPORT.md)
* **涵蓋流程**：
  1. 急性危急紅旗即時攔截（0.1ms 119 防禦）
  2. 雙軌 RAG 臨床衛教生成與免責聲明
  3. 藥袋處方 QR Code 優先解析（醫院藥袋秒級萃取藥名並自動帶入）
  4. 看診前對談室安全跳轉引導（LINE 與 Web 隱私安全隔離）
  5. 看診整理第一階段：用藥帶入與過敏病史
  6. 看診整理第二階段：口語化自然語言多實體抽取
  7. 看診整理第三階段：生活影響與就診提問釐清
  8. 結構化摘要確認卡片（8 欄位 Review & Confirm）
  9. 醫護端調閱入口與相機掃描 QR Code（前端離線 jsQR 秒級解碼）
  10. 醫護端調閱與閱後即焚（Gate D: PASS 臨床摘要）

---

## 系統安全架構 (Safety Gate Architecture)

系統採用 A → B → C → D 四道臨床安全防護閘門：

```text
[病患輸入 / 藥袋相片] ──► 【Gate A: 意圖/急症路由 + QR解析】 ──► 【RAG: diabetes-rag 向量與圖譜檢索 (RAG組)】
                                                                                        │
[安全回覆 / 結構化病歷] ◄── 【Gate D: 輸出合規稽核】 ◄── 【Gate C: 證據受限生成】 ◄── 【Gate B: 知識邊界守門】
```

* Gate A（路由層）：急症紅旗一票否決，意圖分流與藥袋圖像處理。
* RAG 檢索層：由 RAG 組別研發之 diabetes-rag 負責雙軌語意（Gemini Embedding）與圖譜召回。
* Gate B（知識層）：15 欄位正規化驗證，過濾不可靠資料。
* Gate C（生成層）：依據官方證據生成回答與引用標註。
* Gate D（輸出層）：8 道合規過濾（禁止確診、禁止開處方、個資去識別化）。
* Gate E（觀測層）：全鏈路 Trace 追蹤與效能監控。

去識別化的代表性 trace 結果與驗證方式見 [Trace 結果摘要](docs/reports/TRACE_SUMMARY_20260901.md)；原始 trace 不公開，以避免將使用者輸入或除錯資料帶入 GitHub。

---

## 展示與操作指南 (Demo Walkthrough)

> 說明：目前系統支援雙軌運行環境：
> 1. **GCP 雲端伺服器（Compute Engine VM）**：全天候 24 小時在線運行，搭配 Cloudflare 安全穿透隧道提供公開 Webhook 與醫護端入口。
> 2. **本機開發與測試**：支援本機 FastAPI 伺服器搭配 ngrok 外網穿透，便於本地單元除錯與功能快速驗證。

### 步驟一：環境建置與初始化

```bash
# 1. Clone 完整專案（已包含 RAG 組原始碼）
git clone https://github.com/Dolly0625/diabetes-chatbot-rag.git
cd diabetes-chatbot-rag

# 2. 建立 Python 虛擬環境並安裝相依套件 (建議使用 Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 步驟二：配置環境變數 (.env)

在專案根目錄建立 .env 檔案（可參考 .env.example）：

```env
# 1. LLM 模型核心配置
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1
OPENCODE_API_KEY=your_opencode_api_key_here
ROUTER_LLM_MODEL=opencode/mimo-v2.5

# 2. RAG 檢索模組配置 (無需安裝本機模型，填入 Gemini API Key)
GEMINI_API_KEY=your_gemini_api_key_here
RAG_BACKEND=diabetes_rag

# 3. LINE Bot 與 Messaging API 配置
LINE_CHANNEL_SECRET=your_line_channel_secret_here
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token_here
LINE_IDENTITY_HASH_KEY=your_16_chars_random_hash_key_here

# 4. Demo 展示與測試旗標
LINE_DEMO_MODE=true
DEMO_INTAKE_TOKEN_ENABLED=true
DEMO_WEB_ENABLED=true
DEMO_CLINICIAN_IDS=doctor-demo
LINE_SESSION_DB_PATH=data/processed/line_sessions.sqlite3
```

### 步驟三：啟動後端主伺服器

```bash
# 啟動本機伺服器 (Port 8000)
python3 -m uvicorn line_bot.app:app --host 0.0.0.0 --port 8000 --reload
```

---

## LINE 官方帳號串接指引 (LINE Webhook Setup)

1. **取得外網公開網址（使用 ngrok）**：
   ```bash
   ngrok http 8000
   ```
   複製產生的 HTTPS 網址，例如：`https://xxxx.ngrok-free.dev`

2. **登入 LINE Developers 後台**：
   * 前往 LINE Developers Console 進入 Messaging API Channel。
   * 在 **Messaging API** 頁籤中：
     - **Webhook URL** 填入：`https://xxxx.ngrok-free.dev/callback`
     - 點擊 **Verify** 確認回傳 Success。
     - 開啟 **Use webhook**。
   * 在 **LINE Official Account Manager**「回應設定」中**關閉「自動回應訊息」**，並**開啟「Webhook」**。

---

## 醫護端後台與 QR Code 調閱機制 (Clinician Portal Flow)

```text
[病患手機 LINE / 網頁] ──► [完成問卷生成 QR Code] ──► [醫師後台相機掃碼] ──► [解密調閱並銷毀 Token]
```

### 1. 醫護端入口連結
醫師使用電腦瀏覽器或手機打開以下網址：
* 雲端線上展示：`https://dsc-neural-annual-physics.trycloudflare.com/clinician`（Demo 模式自動預填 `doctor-demo` 授權身分）
* 本地開發測試：`http://localhost:8000/clinician`

### 2. 病患端產生分享碼（QR Code）
* **途徑 A（LINE 聊天室）**：病患在 LINE 輸入 `準備看診`，回答問卷（或傳送藥袋 QR 相片），完成後系統生成 15 分鐘時效之 QR Code 圖片與代碼。
* **途徑 B（網頁對談室）**：病患打開 `http://localhost:8000/demo/previsit`，以打字機動畫完成對話後產生 QR Code。

### 3. 診間調閱與閱後即焚機制
1. 醫師在醫護端點擊 **「開啟相機掃描」**，對準病患手機上的 QR Code。
2. 前端透過 `jsQR` 離線解析字串，向後端 `/api/clinician/share/redeem` 發送兌換請求。
3. 後端核對 Token 有效期（15 分鐘），解密成功後**立即將該 Token 標記為已使用並銷毀**。
4. 醫護端螢幕即時渲染 4 大結構化病歷區塊（用藥歷程、過敏病史、主訴症狀、就醫提問），並寫入 `audit_logs` 審計庫。

---

## 執行自動化測試

本專案具備完整的自動化回歸測試套件：

```bash
# 執行全套 700+ 個單元與整合測試
python3 -m pytest -q
```

---

## 專案模組分工

```text
diabetes-chatbot-rag/
├── tfda_context_gate/        # 【Agent 組】四道安全閘門、臨床急症政策、藥袋解析、看診問卷狀態機
├── line_bot/                 # 【Agent 組】FastAPI 主伺服器、LINE Webhook 與網頁前端
├── diabetes-rag/             # 【RAG 組別】隨專案提供的 RAG 原始碼 (Gemini Vector + TFDA Graph Triples)
├── docs/                     # 專案架構規範與技術文件
├── fixtures/                 # 測試資源與範例藥袋照片
└── scripts/                  # LINE 圖文選單產生與維運工具
```

---

## 免責聲明 (Disclaimer)

本專案為健康衛教與看診前資料整理之原型研究展示系統（Demo Prototype），所提供之衛教資訊依據官方公開文件整理，僅供參考，絕不能取代合格醫療專業人員之診斷、諮詢與治療。若出現身體不適或急性症狀，請立即前往醫療院所就醫。
