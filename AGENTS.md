# AGENTS.md — Diabetes Chatbot RAG 專案交接與後續任務清單 (2026-09-01)

## 📌 專案定位與目標 (Project Goal)
本專案為 **糖尿病智慧健康助理（Diabetes Chatbot RAG）**，核心具備：
1. **臨床安全防護（A→B→C→D 四道安全閘門）**：確保所有醫療衛教嚴格依據官方仿單與專書，拒絕 AI 幻覺。
2. **急性紅旗緊急攔截（Fail-Closed 119 防禦）**：遇急症（胸痛、呼吸困難、嚴重低血糖冒冷汗）立即中斷轉介。
3. **看診前 3-Stage 整理室（Pre-visit Intake）**：具擬真溫度的衛教師問卷對話，支援 SSE 打字串流。
4. **醫護端調閱後台（Clinician Portal）**：手機相機離線 QR Code 掃描解碼，閱後即焚調閱結構化病歷摘要。
5. **雙軌 RAG 檢索（diabetes-rag）**：整合 Google Gemini 向量檢索 ＋ TFDA 知識圖譜三元組 ＋ RRF 融合排序。

---

## 🟢 當前系統完成狀態 (Current Status: 707 Tests PASS)
- **Gate A (Router)**：正則急症掃描（12 大類危急訊號）＋ LLM 意圖識別，支援衛教提問邊界區分（不誤擋知識提問）。
- **Gate B (Context Gate)**：強制 15 個正規化欄位驗證，嚴格過濾不可靠依據。
- **Gate C (Generator)**：受限生成與來源標註（`〔來源：E1、E2〕`）＋ 標準醫療免責聲明。
- **Gate D (Output Gate)**：8 道確定性輸出過濾防線（禁止確診、禁止開處方、禁止自造藥名）。
- **Gate E (Observability)**：全鏈路 Trace 追蹤（記錄各節點延遲、狀態流轉與審計資訊）。
- **RAG 引擎**：已打通 `diabetes-rag` 子模組（調用 Gemini Embedding-2），並保留本地 Ollama `bge-m3` 多來源快取備援。
- **前端與通訊**：
  - LINE 官方 Webhook (`/callback`)
  - 病患對談室 SSE 串流 (`/api/patient/previsit-room/chat/stream`)
  - 醫護端 QR 掃描 (`/api/clinician/share/redeem` ＋ 本地 `jsQR` 離線解碼)
- **測試驗證**：全套自動化測試 **707 passed, 0 failed**。

---

## 📋 目前待辦與後續維護任務清單 (Pending Tasks & Roadmap)

### 1. 倉庫瘦身與歷史檔案清理 (Repo Cleanup)
- [ ] **移除歷史封存目錄**：清理 `archive/` 與 `experiments/` 歷史檔案，保持公開倉庫極簡。
- [ ] **精簡 `docs/` 文件**：將過期的過渡型交接文件（如 `HANDOFF_20260831.md`）歸檔，保留 `PROJECT_STRUCTURE_AND_UPLOAD_GUIDE.md` 與公開 `README.md`。

### 2. 架構優化與模組化拆分 (v0.2 Refactor Blueprint)
- [ ] **`orchestrator.py` 瘦身拆分**（將目前 3,600 行依單一職責拆為 4 個專用模組）：
  1. `async_push_manager.py`（專責 LINE 1秒超時與背景推播）。
  2. `intake_normalizer.py`（專責 8 大問卷欄位口語解析與正規化）。
  3. `dialogue_interrupt.py`（專責插話提問與代填對象切換）。
  4. `session_checkpointer.py`（專責 SQLite 狀態保存與斷點續填）。
  5. `orchestrator.py`（保留核心狀態機調度骨架，降至 ~400 行）。

### 3. 多模態與 OCR 演進 (Multimodal OCR Roadmap)
- [ ] 目前以相機 QR Code 掃描為主；未來可擴充藥袋文字 OCR 介面（如 PaddleOCR 修正模型）。

---

## 🚀 常用指令速查 (Quick Runbook)

```bash
# 1. 執行全套回歸測試 (707 passed)
python3 -m pytest -q

# 2. 啟動後端主伺服器 (Port 8000)
export DEMO_INTAKE_TOKEN_ENABLED=true
export DEMO_WEB_ENABLED=true
export LINE_DEMO_MODE=true
export DEMO_CLINICIAN_IDS=doctor-demo
export RAG_BACKEND=diabetes_rag
python3 -m uvicorn line_bot.app:app --host 0.0.0.0 --port 8000 --reload

# 3. 本地快速驗證 RAG 衛教生成
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'1','user_raw_input':'糖尿病飲食可以吃什麼？','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).final_response)"
```

---

## ⚠️ 核心鐵律與邊界限制 (Constraints)
1. **嚴禁繞過 Gate B / Gate D**：所有對外回答必須經過 15 欄位證據檢驗與 8 步輸出安全檢查。
2. **嚴禁外洩個資與金鑰**：`.env` 與 SQLite 包含之病患資料絕不上傳，個資一律 SHA-256 雜湊。
3. **維持 707 測試全綠**：任何重構與調整必須確保全套回歸測試通過。
