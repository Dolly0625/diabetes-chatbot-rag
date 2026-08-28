# LINE Bot 安全基線後架構（Demo v0.1）

## 目標與邊界

本文件以前置四項安全能力已通過測試為前提：固定 Agent scaffold、雙層上下文、確定性釐清政策、糖尿病文字風險分流。

Demo 不串接院方 HIS／EMR、不使用真實病歷，也不把 LINE 的 `declared_role` 當成醫護授權。所有展示個案使用合成資料，或由病患本人主動輸入並同意分享的資料。

## 角色定案：兩種前端、四種後端角色

使用者只看到兩種介面：

1. 病患／家屬端（`PATIENT_FAMILY`）
2. 醫護端（`CLINICIAN`）

後端保留四種 actor role：

```text
PATIENT          本人使用
RELATED_PERSON   家屬、照護者、法定代理人
PRACTITIONER     經驗證的醫護人員
SYSTEM_ADMIN     內部系統管理，不出現在 LINE Rich Menu
```

`RELATED_PERSON` 不建立第三套介面，而是在病患／家屬端選擇「代家人整理」。後端必須另外保存 `subject_id_hash`、`information_source`、`authorization_status` 與 `permission_scopes`。角色宣告只能決定呈現，資料操作一律依 permission scope。

## 建議元件

```text
LINE Messaging API
        │ webhookEventId（冪等）
        ▼
Webhook Adapter ── 驗證 X-Line-Signature、文字／圖片標準化
        │
        ▼
Identity & Session Boundary
  ├─ hashed_line_user_id
  ├─ ProductSession
  ├─ actor_role／frontend_persona（僅呈現與分流）
  └─ authorization_status（真正授權狀態）
        │
        ▼
Conversation Orchestrator
  ├─ RiskSignalPolicy（最先執行）
  ├─ ClinicalConversationState（不可被壓縮）
  ├─ Recent Conversation Window（可壓縮）
  ├─ ClarificationPolicy
  └─ PreVisitIntake 3 stages + Review & Confirm
        │
        ▼
固定安全工作流 A → RAG → B → C → D
        │
        ├─ E trace（只存 hash／必要稽核資訊）
        ▼
LINE Reply / LIFF Review UI
```

## 身分與授權

### 病患／家屬端

- LINE `userId` 先做 keyed hash，再作為內部索引。
- 第一次開始看診整理時顯示用途、保存時間與分享範圍，取得明確同意。
- `PATIENT` 是角色宣告；同意後才把 `authorization_status` 設為 `PATIENT_SELF`。
- 開始 intake 時只問「為自己整理」或「代家人整理」。後者映射為 `RELATED_PERSON`，介面仍共用。
- 代家人整理必須標示資料來自家人轉述或照護者觀察，且未授權時不得查看既有資料。

### 醫護人員

- 不允許只在聊天中輸入「我是醫師」便查看病患資料。
- Demo 使用獨立 LIFF／Web clinician portal 與預先建立的 demo clinician account。
- 正式導入時再替換成院方 SSO／OIDC 與 RBAC。

### 病患資料分享

Demo 優先採「病患主動分享」：病患在 Review & Confirm 後按下分享，系統建立一次性、短效、可撤銷的 `share_grant`。醫護 portal 只能看該 grant 指定的 intake snapshot，不能列舉全部病患。

授權碼不是唯一介面；可用 QR／深連結包裝一次性 grant，降低手動輸入負擔。真正安全控制仍是：短效、單次、綁定 clinician session、可撤銷與完整 audit。

## ProductSession

```text
ProductSession
  session_id
  hashed_line_user_id
  actor_role
  frontend_persona
  subject_id_hash
  information_source
  authorization_status
  permission_scopes
  conversation_context
  intake_snapshot
  intake_stage
  pending_question
  system_risk_classification
  version
  expires_at
```

- Demo 可使用具 TTL 的 SQLite／Redis repository；不可用 process-global dict 當正式 session。
- 每次更新使用 optimistic version，避免 LINE 重送或同時訊息覆蓋資料。
- 使用 `webhookEventId` 做 event idempotency；event 必須綁定 hashed principal，不能由另一位 LINE 使用者重播取得結果。
- webhook claim 使用短效 lease 與 claim token；crash 後可接手，舊 worker 恢復時不得覆寫新 worker 結果。
- Demo retention：session 7 天、share grant 到期刪除、webhook replay payload 1 天、clinician audit 90 天；定期 purge 並截斷 SQLite WAL。
- 原始藥袋圖片只在 OCR 呼叫期間存在，沿用「不得存入 WorkflowState」限制。

## 單次訊息處理順序

1. 驗證 webhook signature 與 event idempotency。
2. 取得並鎖定 ProductSession。
3. 對本次原始訊息執行 RiskSignalPolicy。
4. 明確紅旗立即停止一般回答並轉介；仍記錄具 provenance 的安全訊號。
5. 將使用者確認過的欄位寫入 ClinicalConversationState／PreVisitIntake。
6. ClarificationPolicy 決定是否一次追問一個主題群組。
7. 可回答時才進固定 A→RAG→B→C→D。
8. 將 D 通過的回覆加入 recent window；依 stage／60% token／4 exchanges 規則壓縮。
9. 原子保存 session，再回覆 LINE。

## 已完成的程式契約（2026-08-27）

1. `WorkflowResult` 已增加唯讀 `intake_snapshot`、`intake_stage`、`previsit_summary` 與 `system_risk_classification`，session 不需從回覆文字反向解析。
2. 已建立 `ProductSessionRepository` protocol 與 SQLite WAL demo adapter，包含 TTL、optimistic version、webhook event replay。
3. 已建立 `ConversationOrchestrator`；LINE adapter 不直接修改 clinical state，多輪 intake 可跨 process restart 延續。
4. 已完成病患本人／代家人整理、家屬同意、資料來源標示、三階段 intake、Review & Confirm、指定區段重開修改。
5. 已完成藥袋圖片 session flow；repository 只保存 OCR 後結構化藥品與 `［藥袋圖片］` 事件，不保存 raw bytes。
6. 已完成一次性 10 分鐘 `ShareGrant`、可指定 Demo 醫護、撤銷、過期／使用後拒絕及 clinician access audit。
7. 已建立 `/patient` 病患入口、`/clinician` 醫護唯讀入口、Quick Reply 動作與 `/api/line/rich-menu` 六格選單定義。
8. 病患 portal 正式模式使用 LIFF ID token，後端呼叫 LINE Login v2.1 verify endpoint 取得 subject；自填 `X-Line-User-Id` 只有明確設定 `LINE_DEMO_ALLOW_ID_HEADERS=true` 才啟用。
9. webhook 未設定 channel secret 時預設 503 fail-closed；unsigned webhook 只有本機明確開啟 `LINE_ALLOW_UNSIGNED_WEBHOOK=true` 才可使用。
10. `system_risk_classification` 對同一 subject 採單調合併；一旦為 `RED_FLAG`，查看摘要或其他產品命令都必須維持緊急轉介，不能降回一般回答。
11. 使用者查看摘要與建立 share snapshot 都會執行固定 pre-visit D Output Gate；D 非 `PASS` 時只回安全 fallback，不分享未通過內容。
12. webhook event 綁定 principal 並使用 120 秒 lease／claim-token fencing；reply API 失敗回 503，LINE 重送時重播已完成結果而不重複寫入 intake。
13. repository 會清除到期 session／grant、1 天前 webhook payload 與 90 天前 clinician audit；SQLite 啟用 `secure_delete`，資料庫與 WAL/SHM 已排除版本控制及 Docker build context。

## 執行設定

```text
LINE_IDENTITY_HASH_KEY       session principal 的 HMAC key（至少 16 字元）
LINE_SESSION_DB_PATH         SQLite demo repository
LINE_ALLOW_UNSIGNED_WEBHOOK  僅本機測試；預設 false
LINE_LOGIN_CHANNEL_ID        後端驗證 LIFF ID token 的 expected audience
LINE_LIFF_ID                 病患 portal 初始化 LIFF
LINE_DEMO_MODE               Demo 醫護入口總開關；預設 false
LINE_DEMO_ALLOW_ID_HEADERS   僅本機測試可設 true；公開部署必須 false
DEMO_CLINICIAN_IDS           Demo 醫護 allowlist；正式導入替換院方 SSO/OIDC
```

目前不呼叫 LINE API 自動覆寫帳號既有 Rich Menu；部署者取得 `/api/line/rich-menu?patient_portal_url=https://.../patient` 的 payload 後，再搭配自有選單圖片建立及綁定，避免程式啟動造成外部狀態變更。

## Demo 與正式導入邊界

- SQLite、環境變數醫護 allowlist 與 Demo clinician header 是展示用 adapter，不是醫院正式 IAM。
- 正式院方導入需將 clinician verifier 換成 SSO／OIDC、將 repository 換成院方核准的加密資料庫與 retention policy，並完成法遵、威脅模型與滲透測試。
- 系統目前不串 HIS／EMR，不會讓醫護搜尋或列舉病患；醫護只能兌換病患主動建立的 grant snapshot。
- 病患頁的分享碼目前是文字 token；可再用 QR／deep link 包裝，但後端仍維持短效、單次、可撤銷、可綁醫護的相同控制。

## 驗收情境

- 病患分三輪完成 intake，重新啟動服務後仍能從正確 stage 繼續。
- 超過四組對話或 token 60% 後，過敏史、用藥、紅旗與授權狀態仍完整。
- 「沒有胸痛，但現在呼吸困難」不得被否定詞洗掉。
- 家屬資料必須標示為轉述，不能冒充病患本人確認。
- 未驗證的醫護角色無法列舉或查看病患摘要。
- 一次性 share grant 過期、使用後或撤銷後均不可再讀取。
- LINE 重送同一 webhook event 不得重複新增資料或重複推送回覆。

目前自動測試已覆蓋上述情境；完整測試命令為 `python3 -m pytest tfda_context_gate/tests -q -rs`。2026-08-27 本機結果為 `156 passed, 10 skipped`；10 個 skip 是本機缺少 `langchain_huggingface`，不可誤記成正式 RAG 整合已在此環境驗完。

## 當前部署狀態邊界（2026-08-27）

- 目前 `.env` 已能讓 Messaging webhook 與 ProductSession 核心 ready；未另設 identity key 時，Demo 會以 channel secret 做 domain-separated HMAC 派生。正式環境仍應配置獨立 key，並制定 key rotation／session 失效策略。
- `LINE_LOGIN_CHANNEL_ID`／`LINE_LIFF_ID` 尚未完整配置，所以病患 LIFF 身分驗證仍待 LINE Console 與實機驗收。
- Demo clinician 預設關閉；只有同時設定 `LINE_DEMO_MODE=true` 與 allowlist 才可進入。正式醫護 IAM 尚未完成，不能宣稱已具醫院上線資格。
- Dockerfile 已補齊 runtime 套件與 non-root 執行，但本機 Docker daemon 未啟動，尚未完成 image build 驗證。
