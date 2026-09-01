# Diabetes Chatbot RAG (糖尿病健康諮詢與看診前整理機器人)

> 結合 RAG 知識庫檢索、看診前問卷整理與藥袋智慧辨識的糖尿病 LINE 智慧健康助理 Demo。
> 本專案由 Agent 臨床安全閘門與對話編排，整合 RAG 組別專屬研發之 diabetes-rag 檢索模組協同構建。

---

## 核心功能特色

### 1. 權威衛教與藥物諮詢（RAG 組別專屬檢索模組）
* 跨組別模組整合：透過 Git Submodule 深度串接 RAG 組別開發之 diabetes-rag 引擎。
* 零本機向量模型依賴：全面採用 Google Gemini 雲端向量 API（models/gemini-embedding-2），無需在本地安裝 Ollama 或下載數 GB 之巨大模型權重，具備 API Key 即可隨插即用。
* 雙軌檢索技術：融合 Google Gemini 向量檢索（254 筆國健署專書）與 TFDA 官方藥品知識圖譜三元組，透過 RRF (Reciprocal Rank Fusion) 排名融合演算法精準召回。
* 杜絕 AI 幻覺：透過安全閘門（Context Gate B）強制 15 欄位檢驗，模型僅能依據官方核准之證據回答，並標註引用來源（如〔來源：E1、E2〕）與免責聲明。

### 2. 急性紅旗安全攔截（Fail-Closed 緊急防禦）
* 當病患輸入涉及生命危急症狀（如：胸口劇痛、呼吸困難、血糖過低冒冷汗、意識不清）時，系統立即停止常規對話與 RAG 檢索，第一時間提供 119 與急診就醫引導。

### 3. 藥袋照片上傳與智慧辨識（Medication Bag OCR & QR Scan）
* 拍照即辨識：病患若不清楚藥品名稱，可直接在 LINE 聊天室拍攝或上傳藥袋正面照片。
* 雙軌解析與 TFDA 字典糾錯：優先解析藥袋 QR Code；若無 QR Code 則啟動 OCR 視覺文字辨識，並結合 TFDA 4.4 萬筆官方藥品字典進行模糊比對與糾錯（如自動校正為標準藥名 Metformin 500mg）。
* 自動帶入問卷：解析完成之藥物自動填入看診整理室之「目前固定用藥」欄位。
* 個資零留存防護：原始相片在提取藥名後立即自記憶體釋放，絕不持久化儲存原始圖片檔案，嚴格保護病患醫療隱私。

### 4. 看診前 3 階段對談室（Pre-visit Intake）
* 具備擬真溫度的衛教師對話：對病患的不適症狀表達同理心關懷，消除冷冰冰的填表感。
* 結構化就醫摘要：
  - 第 1 階段：目前固定用藥（支援文字輸入或藥袋拍照）、過敏史、過去病史、家族史。
  - 第 2 階段：發病時間、主要不適症狀描述、對生活影響程度。
  - 第 3 階段：就醫想詢問醫師的問題，自動打包為結構化就醫摘要與 QR Code 分享碼。

### 5. 醫護端調閱後台（Clinician Portal）
* 跨平台相機掃描：內建離線 QR Code 解碼器，醫師可直接用手機相機掃描病患手機上的 QR Code。
* 閱後即焚資安機制：分享碼具備 15 分鐘時效，醫師兌換解密後立即銷毀，兼顧臨床效率與個資隱私。

---

## 系統安全架構 (Safety Gate Architecture)

系統採用嚴格的 A → B → C → D 四道臨床安全防護閘門：

```text
[病患輸入 / 藥袋相片] ──► 【Gate A: 意圖/急症路由 + OCR】 ──► 【RAG: diabetes-rag 向量與圖譜檢索 (RAG組)】
                                                                                  │
[安全回覆 / 結構化病歷] ◄── 【Gate D: 輸出合規稽核】 ◄── 【Gate C: 證據受限生成】 ◄── 【Gate B: 知識邊界守門】
```

* Gate A（路由層）：急症紅旗一票否決，意圖分流與藥袋圖像處理。
* RAG 檢索層：由 RAG 組別研發之 diabetes-rag 負責雙軌語意（Gemini Embedding）與圖譜召回，無需本機模型。
* Gate B（知識層）：15 欄位正規化驗證，過濾不可靠資料。
* Gate C（生成層）：嚴格依據官方證據生成，拒絕胡說八道。
* Gate D（輸出層）：8 道合規過濾（禁止確診、禁止開處方、個資去識別化）。
* Gate E（觀測層）：全鏈路 Trace 追蹤與效能監控。

---

## 完整展示與操作指南 (Demo Walkthrough)

> 說明：目前展示與原型測試環境採用本機伺服器搭配 ngrok 外網穿透方式運行，尚未部署至 GCP（Google Cloud Platform）雲端主機；雲端容器化部署列為後續生產環境規劃。

### 步驟一：環境建置與初始化

```bash
# 1. Clone 專案並初始化 RAG 組別子模組
git clone https://github.com/Dolly0625/diabetes-chatbot-rag.git
cd diabetes-chatbot-rag
git submodule update --init --recursive

# 2. 建立 Python 虛擬環境並安裝相依套件 (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 步驟二：配置環境變數 (.env)

在專案根目錄建立 .env 檔案（可直接複製 .env.example 並修改）：

```env
# 1. LLM 模型核心配置
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1
OPENCODE_API_KEY=your_opencode_api_key_here
ROUTER_LLM_MODEL=opencode/mimo-v2.5

# 2. RAG 檢索模組配置 (無需安裝本機模型，僅需填入 Gemini API Key)
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

若要讓真實的手機 LINE 能夠收到機器人回覆，請依序執行以下設定：

1. **取得外網公開網址（使用 ngrok）**：
   ```bash
   ngrok http 8000
   ```
   複製終端機中產生的 HTTPS 網址，例如：`https://xxxx.ngrok-free.dev`

2. **登入 LINE Developers 後台**：
   * 前往 LINE Developers Console 進入您的 Messaging API Channel。
   * 在 **Messaging API** 頁籤中：
     - **Webhook URL** 填入：`https://xxxx.ngrok-free.dev/callback`
     - 點擊 **Verify** 確認回傳 Success。
     - 開啟 **Use webhook** 開關。
   * 在 **LINE Official Account features** 中：
     - 點擊進入 LINE Official Account Manager，在「回應設定」中**關閉「自動回應訊息」**，並**開啟「Webhook」**。

---

## 醫護端後台與 QR Code 調閱機制 (Clinician Portal Flow)

```text
[病患手機 LINE / 網頁] ──► [完成問卷生成 QR Code] ──► [醫師後台相機掃碼] ──► [解密調閱並銷毀 Token]
```

### 1. 醫護端入口連結
醫師使用電腦瀏覽器或手機打開以下網址：
* 本地測試：`http://localhost:8000/clinician`
* 外網展示：`https://xxxx.ngrok-free.dev/clinician`

### 2. 病患端產生分享碼（QR Code）
* **途徑 A（LINE 聊天室）**：病患在 LINE 輸入 `準備看診`，依序回答問卷（或直接傳送藥袋照片），最後機器人自動生成 15 分鐘時效之 QR Code 圖片與代碼。
* **途徑 B（網頁對談室）**：病患直接打開 `http://localhost:8000/demo/previsit`，以打字機動畫完成對話後，畫面一鍵產生 QR Code。

### 3. 診間調閱與閱後即焚機制
1. 醫師在醫護端點擊 **「開啟相機掃描」**，直接對準病患手機上的 QR Code。
2. 系統前端透過內建的 `jsQR` 離線解析出加密字串，並向後端 `/api/clinician/share/redeem` 發送兌換請求。
3. **閱後即焚安全保護**：
   - 後端核對 Token 有效期（15 分鐘），解密成功後**立即將該 Token 標記為 USED 並銷毀**。
   - 系統即時在醫師螢幕上渲染 4 大結構化病歷區塊（用藥歷程、過敏病史、主訴症狀、就醫提問）。
   - 全程操作自動寫入 `audit_logs` 審計資料庫。

---

## 執行自動化測試

本專案具備完整的自動化回歸測試套件（涵蓋 Agent 流程、藥袋 OCR 適配與 RAG 檢索）：

```bash
# 執行全套 700+ 個單元與整合測試
python3 -m pytest -q
```

---

## 專案模組分工

```text
diabetes-chatbot-rag/
├── tfda_context_gate/        # 【Agent 組】四道安全閘門、臨床急症政策、藥袋OCR、看診問卷狀態機
├── line_bot/                 # 【Agent 組】FastAPI 主伺服器、LINE Webhook 與網頁前端
├── diabetes-rag/             # 【RAG 組別】RAG 檢索子模組 (Gemini Vector + TFDA Graph Triples)
├── docs/                     # 專案架構規範與技術文件
├── fixtures/                 # 測試資源與範例藥袋照片 (fixtures/images/medication_bag_front.jpg)
└── scripts/                  # LINE 圖文選單產生與維運工具
```

---

## 免責聲明 (Disclaimer)

本專案為健康衛教與看診前資料整理之原型研究展示系統（Demo Prototype），所提供之衛教資訊依據官方公開文件整理，僅供參考，絕不能取代合格醫療專業人員之診斷、諮詢與治療。若出現身體不適或急性症狀，請立即前往醫療院所就醫。
