# AGENTS.md — Diabetes Chatbot RAG 專案交接與 AI 協同操作指南 (2026-09-01)

## 專案定位與架構目標 (Project Overview)
本專案為 糖尿病智慧健康助理（Diabetes Chatbot RAG），由 Agent 組 與 RAG 組別 協同開發：
1. 臨床安全閘門（A→B→C→D）：確保所有醫療衛教回答嚴格依據官方仿單與專書，杜絕 AI 幻覺。
2. 急性紅旗即時攔截（Fail-Closed 119 防禦）：遇急症（胸痛、呼吸困難、嚴重低血糖冒冷汗）0.1ms 內一票否決並強制轉介。
3. 藥袋解析（Medication Bag QR-First）：支援藥袋 QR Code 優先解析自動帶入問卷，並預留 OCR 視覺辨識介面（原型驗證階段）；原始圖片於記憶體處理後即銷毀，絕不持久化儲存。
4. 看診前 3-Stage 整理室（Pre-visit Intake）：具擬真溫度的衛教師問卷對話，支援 SSE 打字串流與個資雜湊。
5. 醫護端調閱後台（Clinician Portal）：手機相機離線 QR Code 掃描解碼，15 分鐘時效與「閱後即焚」調閱機制。
6. 雙軌 RAG 檢索（diabetes-rag 內嵌原始碼）：整合 Google Gemini 雲端向量檢索（零本機模型負擔，僅需 GEMINI_API_KEY）＋ TFDA 知識圖譜三元組 ＋ RRF 排名融合。
7. 目前部署架構：本機 FastAPI 伺服器搭配 ngrok 外網穿透，尚未部署至雲端主機；未來的雲端主機與部署方案待團隊後續共同討論定案。

---

## 當前系統完成狀態 (Current Status: 707 Tests PASS)
- Gate A (Router)：正則急症掃描（12 大類危急訊號）＋ LLM 意圖識別，支援衛教提問邊界區分（不誤擋知識提問）。
- Gate B (Context Gate)：強制 15 個正規化欄位驗證，嚴格過濾不可靠依據。
- Gate C (Generator)：受限生成與來源標註（〔來源：E1、E2〕）＋ 標準醫療免責聲明。
- Gate D (Output Gate)：8 道確定性輸出過濾防線（禁止確診、禁止開處方、禁止自造藥名）。
- Gate E (Observability)：全鏈路 Trace 追蹤（記錄各節點延遲、狀態流轉與審計資訊）。
- RAG 引擎：已打通隨主專案提供的 diabetes-rag 原始碼（全面採用 Google Gemini 雲端 API，無需在本地安裝 Ollama 或下載模型權重，具備 API Key 即可隨插即用）。
- 測試驗證：全套自動化回歸測試 707 passed, 0 failed。

---

## LINE 官方帳號串接與展示操作指南 (LINE & Demo Walkthrough)

### 1. 本地啟動與外網穿透 (Ngrok Setup)
```bash
# 終端機 1：啟動主伺服器 (Port 8000)
python3 -m uvicorn line_bot.app:app --host 0.0.0.0 --port 8000 --reload

# 終端機 2：啟動 ngrok 取得公開 HTTPS 網址
ngrok http 8000
# 複製產生的網址，例如：https://xxxx.ngrok-free.dev
```

### 2. LINE Developers Console 後台設定
1. 前往 LINE Developers Console 進入您的 Messaging API Channel。
2. Webhook URL 設定為：https://xxxx.ngrok-free.dev/callback
3. 點擊 Verify 確認回傳 Success，並開啟 Use webhook。
4. 進入 LINE Official Account Manager 後台，在「回應設定」中關閉「自動回應訊息」，並開啟「Webhook」。

### 3. 病患端操作與看診前整理流程 (Patient Journey)
* 衛教與急症諮詢：病患在 LINE 輸入 糖尿病可以吃什麼？ 或 我現在胸口劇痛，系統自動執行 A→B→C→D 閘門。
* 藥袋資訊解析 (Medication Bag QR & OCR Prototype)：
  - 病患在 LINE 聊天室傳送藥袋相片。
  - 後端 MedicationBagOCRService 優先解析藥袋處方 QR Code，提取藥名（如 Metformin 500mg）並帶入問卷目前用藥欄位。
  - 預留 OCR 視覺辨識與 TFDA 核心糖尿病字典比對介面（在無 GPU 或未裝 OCR 深度學習套件之環境下，以 QR Code 解析為主）。
  - 原始相片於記憶體解析後立即銷毀，絕不持久化儲存。
* 啟動看診前整理：
  - LINE 管道：在 LINE 聊天室輸入 準備看診，依序回答 8 題問卷（用藥、過敏、慢性病、家族史、發病時間、症狀描述、生活影響程度、想問醫師的問題）。
  - 網頁對談室管道：打開 http://localhost:8000/demo/previsit（或 ngrok 網址），以打字機動畫完成對話。
* 生成專屬分享碼（QR Code）：
  - 問卷填寫完畢後，系統自動將 8 欄位打包為結構化 Snapshot，並調用 sharing/service.py 產出具備 15 分鐘時效 的加密 share_token 與 QR Code 圖片。

### 4. 醫護端調閱與 QR 碼兌換機制 (Clinician Portal & QR Flow)
* 醫護端入口：瀏覽器打開 http://localhost:8000/clinician（Demo 模式自動帶入 doctor-demo 授權身分）。
* 相機掃描解碼：醫師點擊「開啟相機掃描」直接對準病患手機上的 QR Code，前端內建 jsQR 函式庫進行純本地離線解碼，取得 share_token。
* 兌換與閱後即焚（Burn-After-Reading）：
  1. 前端向後端 POST /api/clinician/share/redeem 發送請求。
  2. 後端驗證 Token 是否有效且未過期；驗證成功後立即將該 Token 標記為已使用並銷毀，防止二次調閱洩漏。
  3. 醫護端螢幕於 1 秒內渲染 4 大區塊臨床摘要（用藥歷程、過敏病史、主訴症狀、就醫提問）。
  4. 全程操作自動寫入 audit_logs 審計資料庫。

---

## 常用指令速查 (Quick Runbook)

```bash
# 1. 執行全套回歸測試 (707 passed)
python3 -m pytest -q

# 2. 測試特定模組
python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -v
python3 -m pytest line_bot/tests/test_previsit_room_api.py -v
python3 -m pytest tfda_context_gate/tests/test_diabetes_rag_integration.py -v

# 3. 本地快速模擬 4 大劇本
# (1) 衛教諮詢
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'1','user_raw_input':'糖尿病飲食可以吃什麼？','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).final_response)"
# (2) 急症攔截
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'2','user_raw_input':'我現在胸口劇痛而且全身冒冷汗','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).final_response)"
# (3) 啟動看診整理
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'3','user_raw_input':'準備看診','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).question)"
# (4) 藥袋圖片辨識
python3 -c "from pathlib import Path; from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'4','user_raw_input':'我要準備看診','declared_role':'PATIENT','language':'zh-TW'}, image_bytes=Path('fixtures/images/medication_bag_front.jpg').read_bytes(), use_formal=True).status)"
```

---

## 後續架構優化藍圖 (v0.2 Refactor Blueprint)
- [ ] orchestrator.py 瘦身拆分（將 3,600 行依單一職責拆為 4 個專用模組）：
  1. async_push_manager.py（專責 LINE 1秒超時與背景推播）。
  2. intake_normalizer.py（專責 8 大問卷欄位口語解析與正規化）。
  3. dialogue_interrupt.py（專責插話提問與代填對象切換）。
  4. session_checkpointer.py（專責 SQLite 狀態保存與斷點續填）。
  5. orchestrator.py（保留核心狀態機調度骨架，降至 ~400 行）。
- [ ] 雲端化生產部署：雲端主機與容器化部署方案待團隊後續共同討論定案。
- [ ] 多模態 OCR 演進：正式整合 PaddleOCR 繁體中文深度學習視覺模型。

---

## 核心鐵律與邊界限制 (Constraints)
1. 嚴禁繞過 Gate B / Gate D：所有對外回答必須經過 15 欄位證據檢驗與 8 步輸出安全檢查。
2. 嚴禁外洩個資與金鑰：.env 與 SQLite 包含之病患資料絕不上傳，個資一律 SHA-256 雜湊。
3. 維持 707 測試全綠：任何重構與調整必須確保全套回歸測試通過。
