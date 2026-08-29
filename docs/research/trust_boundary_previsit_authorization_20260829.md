# Trust Boundary — 外部 pre-visit 授權邊界 (P0.5)

> 目的：`run_workflow()` 是內部可信任 engine，正式 LINE/HTTP 的 pre-visit 與藥袋必須經 `ConversationOrchestrator` 的身分、同意與 permission scope 檢查。

## 1. 信任邊界

```
外部不可信任
  ├─ LINE /callback  (X-Line-Signature 驗證後)
  ├─ patient API  (/api/patient/*, LIFF ID token)
  └─ clinician API (/api/clinician/*, 僅讀)

         ↓ 必須經

ConversationOrchestrator (ProductSession + Authorization)

         ↓ 通過後才

run_workflow / stream_workflow (內部可信任 engine)
  ├─ 正式三情境：衛教 / pre-visit / 藥袋 (use_formal, intake_data, image_bytes)
  └─ 測試/內部：simulate_* / local_test_* / pytest / CLI
```

`run_workflow` 保持低階可信任，不增加 `trusted=True` / `bypass` 布林參數；所有可被外部傳入的 `task_type/intake_data/image_bytes/declared_role` 在 `line_bot` 外部入口皆不可直接注入。

## 2. 入口清單

| 入口 | 類型 | 是否經 Orchestrator | 說明 |
|---|---|---|---|
| `line_bot/app.py:POST /callback` Text | 外部正式 | 是 (`_get_conversation_orchestrator()->handle_text`) | 成功時回 `OrchestratorResult`，失敗時走相容 `handle_text_message` 但對 pre-visit fail-closed |
| `line_bot/app.py:POST /callback` Image | 外部正式 | 是 (`handle_image` → OCR) | 未授權回 `請先選擇...`，不進 OCR |
| `line_bot/app.py:handle_text_message` | 內部/測試 | 否 (僅 `use_formal` 透傳) | `simulate_text_message` / `local_test_*` / pytest 專用，不暴露於 HTTP |
| `line_bot/app.py:handle_image_message` | 內部/測試 | 否 | 同上，`image_bytes` 僅內部 `run_workflow` 透傳，`WorkflowState` 不存 raw |
| `workflow/runner.py:run_workflow` | 內部 engine | — | 唯一 A-E 確定性入口，`intake_data/image_bytes/task_type` 僅內部 Orchestrator 供給 |
| `workflow/runner.py:stream_workflow` | 內部 engine | — | `run_workflow` 的 buffered-then-stream 包裝 |

正式外部 `task_type="pre_visit_intake"` 來源僅 `orchestrator._process_text: task_type="pre_visit_intake" if status in (ACTIVE,AWAITING_CONFIRMATION) and _is_intake_active`，LINE payload 無此欄位。

## 3. 已關閉的繞過路徑

* **pre-visit 文字繞過**：`line_bot` 兼容路徑對 `is_pre_visit_intake_text(text)` 直接 `handle_text_message` → 現改 `目前無法安全開始整理，請先完成身分與授權` 且不建 `ProductSession` 健康欄位，`run_workflow` 不被呼叫。
* **圖片繞過**：`handle_image_message` 在 `orchestrator is None` 時直接 OCR → 現改 fail-closed 回相同安全提示，不呼叫 OCR/`run_workflow`。
* **`questions_for_doctor` 雙軌**：`line_bot:_maybe_record` 曾直接 `[*questions,q]` → 現改 `PendingAction PENDING_CONFIRM_QUESTION`，僅 `orchestrator` 的同意閘 `is_agree` 才落地。
* **外部 `task_type` 注入**：`handle_text_message` 不接受 `task_type` 參數，`line_bot` 未將 HTTP body 透傳至 `run_workflow` 的 `task_type`，僅 `orchestrator` 內部 `status+is_intake_active` 決定。

## 4. 降級規則

* **無 ProductSession**：一般衛教 (`_should_use_async_formal` 真) 可走 `handle_text_message` 安全單輪；pre-visit 與圖片 fail-closed。
* **未授權**：`orchestrator.handle_text/handle_image` 回 `NEEDS_AUTHORIZATION` / `NEEDS_ROLE_SELECTION` (`請先選擇為自己或代家人整理`)，不寫 `intake_snapshot`，不進 OCR。
* **紅旗**：`RiskSignalPolicy` 優先於角色選擇與 pending 消費，`system_risk_classification.level==RED_FLAG` 單調不降級，後續產品命令不得恢復問卷。

## 5. 驗證

見 `tests/test_authorization_boundary.py` 10 測：`no_session_pre_visit`、`no_session_image`、`general_no_session_ok`、`unauthorized_image`、`authorized_image`、`proxy_auth`、`no_injection`、`api_still_green`、`direct_run_workflow`、`redflag_priority`。
