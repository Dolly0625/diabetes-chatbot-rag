# 🩺 Diabetes Chatbot RAG (糖尿病健康諮詢與看診前整理機器人)

> 結合 **RAG 知識庫檢索** 與 **看診前問卷整理** 的糖尿病 LINE 智慧健康助理 Demo。
> 本專案由 **Agent 臨床安全閘門與對話編排** 整合 **RAG 組別專屬研發之 `diabetes-rag` 檢索模組** 協同構建。

---

## 🌟 核心功能特色

### 1. 🥗 權威衛教與藥物諮詢（RAG 組別專屬檢索模組）
* **跨組別模組整合**：透過 Git Submodule 深度串接 **RAG 組別開發之 `diabetes-rag` 引擎**。
* **雙軌檢索技術**：融合 **Google Gemini 向量檢索（254 筆國健署專書）** 與 **TFDA 官方藥品知識圖譜三元組**，透過 RRF (Reciprocal Rank Fusion) 排名融合演算法精準召回。
* **杜絕 AI 幻覺**：透過安全閘門（Context Gate B）強制 15 欄位檢驗，模型僅能依據官方核准之證據回答，並標註引用來源（如 `〔來源：E1、E2〕`）與免責聲明。

### 2. 🚨 急性紅旗安全攔截（Fail-Closed 緊急防禦）
* 當病患輸入涉及生命危急症狀（如：`胸口劇痛`、`呼吸困難`、`血糖過低冒冷汗`、`意識不清`）時，系統**立即停止常規對話與 RAG 檢索**，第一時間提供 119 與急診就醫引導。

### 3. 📋 看診前 3 階段對談室（Pre-visit Intake）
* **具備擬真溫度的衛教師對話**：對病患的不適症狀表達同理心關懷，消除冷冰冰的填表感。
* **結構化就醫摘要**：
  - **第 1 階段**：目前固定用藥、藥物/食物過敏史、過去病史、家族史。
  - **第 2 階段**：發病時間、主要不適症狀描述、對生活影響程度。
  - **第 3 階段**：就醫想詢問醫師的問題，自動打包為結構化就醫摘要與 QR Code 分享碼。

### 4. 🏥 醫護端調閱後台（Clinician Portal）
* **跨平台相機掃描**：內建離線 QR Code 解碼器，醫師可直接用手機相機掃描病患手機上的 QR Code。
* **閱後即焚資安機制**：分享碼具備 15 分鐘時效，醫師兌換解密後立即銷毀，兼顧臨床效率與個資隱私。

---

## 🏛️ 系統安全架構 (Safety Gate Architecture)

系統採用嚴格的 **A → B → C → D** 四道臨床安全防護閘門：

```
[病患輸入] ──► 【Gate A: 意圖與急症路由】 ──► 【RAG: diabetes-rag 向量與圖譜檢索 (RAG組)】
                                                                      │
[安全回覆] ◄── 【Gate D: 輸出合規稽核】 ◄── 【Gate C: 證據受限生成】 ◄── 【Gate B: 知識邊界守門】
```

* **Gate A（路由層）**：急症紅旗一票否決，意圖分流。
* **RAG 檢索層**：由 **RAG 組別** 研發之 `diabetes-rag` 負責雙軌語意與圖譜召回。
* **Gate B（知識層）**：15 欄位正規化驗證，過濾不可靠資料。
* **Gate C（生成層）**：嚴格依據官方證據生成，拒絕胡說八道。
* **Gate D（輸出層）**：8 道合規過濾（禁止確診、禁止開處方、個資去識別化）。
* **Gate E（觀測層）**：全鏈路 Trace 追蹤與效能監控。

---

## 🚀 快速開始 (Quick Start)

### 1. 下載專案與初始化 RAG 子模組
```bash
# Clone 專案並遞迴抓取 RAG 組別的 diabetes-rag 子模組
git clone https://github.com/Dolly0625/diabetes-chatbot-rag.git
cd diabetes-chatbot-rag
git submodule update --init --recursive
```

### 2. 安裝環境依賴
```bash
# 建議使用 Python 3.10+
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 設定環境變數
在專案根目錄建立 `.env` 檔案：
```env
# LLM 核心金鑰
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1
OPENCODE_API_KEY=your_api_key_here
ROUTER_LLM_MODEL=opencode/mimo-v2.5

# RAG 檢索模組設定 (Gemini API，由 RAG 組別模型驅動)
GEMINI_API_KEY=your_gemini_api_key_here
RAG_BACKEND=diabetes_rag
```

### 4. 啟動後端服務
```bash
# 啟動本機伺服器 (Port 8000)
export DEMO_INTAKE_TOKEN_ENABLED=true
export DEMO_WEB_ENABLED=true
export LINE_DEMO_MODE=true
export DEMO_CLINICIAN_IDS=doctor-demo
python3 -m uvicorn line_bot.app:app --host 0.0.0.0 --port 8000 --reload
```

* **醫護端入口**：`http://localhost:8000/clinician`
* **病患看診對談室**：`http://localhost:8000/demo/previsit`

---

## 🧪 執行自動化測試

本專案具備完整的自動化回歸測試套件（涵蓋 Agent 流程與 RAG 檢索適配）：

```bash
# 執行全套 700+ 個單元與整合測試
python3 -m pytest -q
```

---

## 📂 專案模組分工

```text
diabetes-chatbot-rag/
├── tfda_context_gate/        # 【Agent 組】四道安全閘門、臨床急症政策、看診問卷狀態機
├── line_bot/                 # 【Agent 組】FastAPI 主伺服器、LINE Webhook 與網頁前端
├── diabetes-rag/             # 【RAG 組別】RAG 檢索子模組 (Gemini Vector + TFDA Graph Triples)
├── docs/                     # 專案架構規範與技術文件
├── fixtures/                 # 測試資源與範例藥袋照片
└── scripts/                  # LINE 圖文選單產生與維運工具
```

---

## 📄 免責聲明 (Disclaimer)

本專案為健康衛教與看診前資料整理之**原型研究展示系統（Demo Prototype）**，所提供之衛教資訊依據官方公開文件整理，僅供參考，**絕不能取代合格醫療專業人員之診斷、諮詢與治療**。若出現身體不適或急性症狀，請立即前往醫療院所就醫。
