# 🩺 Diabetes Chatbot RAG (糖尿病健康諮詢與看診前整理機器人)

> 結合 **RAG 知識庫檢索** 與 **看診前問卷整理** 的糖尿病 LINE 智慧健康助理 Demo。

---

## 🌟 核心功能特色

### 1. 🥗 權威衛教與藥物諮詢（RAG 知識檢索）
* **雙知識庫整合**：結合 **TFDA 官方藥品仿單** 與 **國健署（HPA）糖尿病衛教專書**。
* **杜絕 AI 幻覺**：透過安全閘門（Context Gate）嚴格約束，模型僅能依據官方核准之證據回答，並標註引用來源（如 `〔來源：E1、E2〕`）與免責聲明。

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
[病患輸入] ──► 【Gate A: 意圖與急症路由】 ──► 【RAG: 向量與知識圖譜檢索】
                                                        │
[安全回覆] ◄── 【Gate D: 輸出合規稽核】 ◄── 【Gate C: 證據受限生成】 ◄── 【Gate B: 知識邊界守門】
```

* **Gate A（路由層）**：急症紅旗一票否決，意圖分流。
* **Gate B（知識層）**：15 欄位正規化驗證，過濾不可靠資料。
* **Gate C（生成層）**：嚴格依據官方證據生成，拒絕胡說八道。
* **Gate D（輸出層）**：8 道合規過濾（禁止確診、禁止開處方、個資去識別化）。
* **Gate E（觀測層）**：全鏈路 Trace 追蹤與效能監控。

---

## 🚀 快速開始 (Quick Start)

### 1. 安裝環境依賴
```bash
# 建議使用 Python 3.10+
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 設定環境變數
在專案根目錄建立 `.env` 檔案：
```env
# LLM 核心金鑰
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1
OPENCODE_API_KEY=your_api_key_here
ROUTER_LLM_MODEL=opencode/mimo-v2.5

# RAG 向量檢索設定 (Gemini API)
GEMINI_API_KEY=your_gemini_api_key_here
RAG_BACKEND=diabetes_rag
```

### 3. 啟動後端服務
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

本專案具備完整的自動化回歸測試套件：

```bash
# 執行全套 700+ 個單元與整合測試
python3 -m pytest -q
```

---

## 📂 專案目錄結構速覽

```text
diabetes-chatbot-rag/
├── tfda_context_gate/        # 四道安全閘門、臨床急症政策、看診問卷狀態機
├── line_bot/                 # FastAPI 主伺服器、LINE Webhook 與網頁前端
├── diabetes-rag/             # RAG 檢索模組 (Vector + Knowledge Graph Triples)
├── docs/                     # 專案詳細架構與技術交接文件
├── fixtures/                 # 測試資源與範例藥袋照片
└── scripts/                  # LINE 圖文選單產生與維運工具
```

---

## 📄 免責聲明 (Disclaimer)

本專案為健康衛教與看診前資料整理之**原型研究展示系統（Demo Prototype）**，所提供之衛教資訊依據官方公開文件整理，僅供參考，**絕不能取代合格醫療專業人員之診斷、諮詢與治療**。若出現身體不適或急性症狀，請立即前往醫療院所就醫。
