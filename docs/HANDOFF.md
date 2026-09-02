# TFDA 糖尿病 Agent 交接手冊 - 2026-08-27

## 一句話
`v0.1` 主戰場：`A→B→C→D + E` 安全基線，三情境（衛教/看診前/醫護草稿）正式版 `mimo-v2.5 + bge-m3` 全 `PASS`。`V0.2` 僅藍圖。

## 目前進度（安全基線 + LINE ProductSession + 2026-09-02 最新實裝）
- `b_context_gate` 15欄 + `task_type/tool_context` 預留 + 風險等級
- `A` 正式版插頭（`mimo-v2.5` 來自 `.env`），`RAG bge-m3` 快取（24s→0.17s，Ollama `bge-m3:latest`）
- `HPA` 飲食庫：`食品營養 2塊 + 指標手冊 3塊 + 糖尿病與我 4塊` 共 9 塊，`TFDA` 129 + `HPA` 並存；對齊 RAG 組國健署手冊飲食題向量相似度門檻
- `ToolContract`（`Registry/Executor` allowlist）、`看診前 8欄3階段`（`known_medications/allergies/chronic/family/symptom_*`）、`醫護草稿` 詳細4段
- `Stream`（先緩存、D 驗過才推）、`藥袋多模態 Vision LLM + 醫院 QR 直連解析`、`雙輪次看診前互動問答`（Turn 1 健康史 ➔ Turn 2 承接並帶入藥袋確認）
- `藥名智慧去重與正規化`（同成分中文品名、英文學名、劑量規格聚合為單一標準品名）
- `手機端防快取與狀態同步`（動態時間戳 URL + Anti-Cache Fetch + 競爭條件樂觀鎖刷新）
- 修2卡關：飲食 `D 絕對缺乏` 誤判、`A 藥品一般題 M 誤擋`
- 藥袋提醒「看藥袋」2次追問 + FHIR `unknown`、重構 Step1-2（`.gitignore`、歸檔 `report_handoff`、`藥袋圖 → fixtures`）
- LINE 多輪 `ProductSession`、病患／家屬同介面、三階段 intake、Review & Confirm、一次性 10 分鐘分享 grant
- LINE webhook 預設 fail-closed；event id 綁定 hashed principal，具 lease recovery 與舊 worker fencing，跨使用者重播拒絕
- 紅旗風險對同一 subject 單調累積；摘要與分享 snapshot 都必須通過固定 D Gate，紅旗回覆明確要求停止一般操作並聯絡 119／急診
- SQLite TTL／retention 清理：session 7 天、share grant 到期、webhook replay payload 1 天、clinician audit 90 天；raw 藥袋圖片不落盤
- Demo 醫護入口需同時開啟 `LINE_DEMO_MODE=true` 且命中 allowlist；正式環境仍須院方 SSO/OIDC

## 目錄現況（剛重構）
```
tfda-diabetes-agent/
├── .env (mimo-v2.5, bge-m3, LINE keys) + .gitignore (runs/vector_cache/__pycache__)
├── archive/report_handoff_20260821/ + experiments/archive/medrax2/
├── docs/proposal/ (V0.2 主提案) + docs/HANDOFF.md (本檔) + docs/issues/
├── fixtures/images/medication_bag_front/back.jpg
├── line_bot/app.py (FastAPI /callback, X-Line-Signature, image_bytes → OCR → workflow)
└── tfda_context_gate/
    ├── a_router / b_context_gate / c_generator / d_output_gate / e_observability / workflow / rag / intake / tool_contract / agent / query_expansion
    ├── rag/phase_scripts/00-05*.py (剛搬)
    ├── agent/agent_demo_case_schema.py + agent_demo_cases.json
    ├── data/processed/langchain_documents.json (權威) + .vector_cache/
    └── tests/ (156 passed, 10 skipped；skip 為本機缺 langchain_huggingface)
```

## 怎麼跑（本地，不用 LINE/GCP）
```bash
# 規則版基線（15 passed）
python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -q

# 正式版 3情境（mimo + bge-m3）
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'1','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).status)"
# 預期 COMPLETED / G / B PASS / D PASS

# 看診前
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'intake','user_raw_input':'我下週要看醫生','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).question)"

# 藥袋圖片
python3 -c "from pathlib import Path; from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'bag','user_raw_input':'我要準備看診','declared_role':'PATIENT','language':'zh-TW'}, image_bytes=Path('fixtures/images/medication_bag_front.jpg').read_bytes(), use_formal=True).status)"
```

## LINE + GCP（MVP 最簡）
- `line_bot/app.py` 已寫好：`POST /callback` 驗 `X-Line-Signature`，`Text/Image` 分流，`image_bytes` → `MedicationBagOCRService` → `intake_data` → `run_workflow`，`D` 必過
- 未設定 `LINE_CHANNEL_SECRET` 時，webhook 預設回 503；只有明確設定 `LINE_ALLOW_UNSIGNED_WEBHOOK=true` 才可做本機 unsigned 測試
- `LINE_IDENTITY_HASH_KEY` 建議使用獨立密鑰；Demo 遷移期間未提供時會由 channel secret 做 domain-separated HMAC 派生，因此目前 `.env` 可啟用持久化 ProductSession
- `/health` 的核心 ready 條件是 webhook signature、Messaging API token、ProductSession；回應會另列 `patient_liff` 與 `demo_clinician`，不得把兩者的 `false` 說成入口已可用
- 本地：`uvicorn line_bot.app:app --reload` → `ngrok http 8000` → 貼 `Webhook URL` 到 `developers.line.biz`
- 上線：根目錄已有 non-root `Dockerfile` 與 `.dockerignore`；先 `docker build -t tfda-agent .`，再依部署平台以 Secret Manager 注入環境變數
- 身份：Messaging webhook 驗 `X-Line-Signature`；病患 portal 以 LIFF ID token 交由 LINE Login v2.1 驗證。自填 `X-Line-User-Id` 僅在 `LINE_DEMO_ALLOW_ID_HEADERS=true` 的本機測試啟用。醫護端目前為 Demo allowlist，正式導入仍須替換院方 SSO/OIDC。

## 尚未宣稱完成的外部項目
1. `.env` 尚無完整 `LINE_LOGIN_CHANNEL_ID`／`LINE_LIFF_ID`，因此病患 LIFF portal 的正式 ID token 流程仍待 LINE Console 建立與實機驗證。
2. Demo clinician 目前刻意關閉；要展示需設定 `LINE_DEMO_MODE=true` 與 `DEMO_CLINICIAN_IDS`。正式院方導入不可沿用 header allowlist，必須替換 SSO/OIDC。
3. 本機 Docker daemon 未啟動，Dockerfile 已做靜態檢查但尚未完成 image build；本機也因 PyPI DNS 失敗無法補裝 `langchain_huggingface`，故 10 個 RAG 整合測試為 skip。
4. Rich Menu 只有 API payload 定義，尚未對 LINE 帳號建立／上傳／綁定，避免測試程式擅自修改外部帳號狀態。

## 驗收結果（2026-08-27）
- `python3 -m pytest tfda_context_gate/tests -q -rs`：`156 passed, 10 skipped`
- 攻擊型案例通過：跨 principal event replay 拒絕、過期 lease 可接手且舊 worker 不可提交、紅旗不得被摘要降級、D Gate fail 不得輸出摘要、share grant 單次競態只有一方成功、reply API 失敗回 503 並安全重送、retention 後資料實體清除

## 聯絡
- `.env` 有 secret 明文但已加 `.gitignore`；正式部署改由 Secret Manager 注入並安排輪替
- 測試圖：`fixtures/images/medication_bag_front/back.jpg`（原根藥袋圖已搬）
