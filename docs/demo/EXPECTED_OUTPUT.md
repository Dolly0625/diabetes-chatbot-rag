# Expected Output — Engineering Demo（deterministic）

> 執行：`python scripts/demo/run_engineering_demo.py`  
> 環境：離線，不需 LINE/GCP，預設 deterministic。重複跑 3 次應完全通過。

## 成功範例（節錄，人類可讀短句）

```
Engineering Demo — deterministic（不需 LINE/GCP，不輸出 ID/token/raw image）
工作區：/Users/dolly/Documents/code/tfda-diabetes-agent-demo-scenarios

=== 情境 1：病患看診前整理（8 欄 3-stage + Review & Confirm） ===
  -> Step1 為自己整理（對象=自己）
  ✓ 對象=自己，時間=當下（SELF_REPORTED）
  -> Stage1-1 用藥：metformin
  ✓ 已記錄 known_medications=['metformin']，workflow question 存在=True
  -> Stage1-2 過敏：沒有過敏
  ✓ 已記錄 allergies=['無']
  -> Stage1-3 慢性病：高血壓
  ✓ 已記錄 chronic_conditions=['高血壓']
  -> Stage1-4 家族史：無（家族無糖尿病）
  ✓ 已記錄 family_history=['無']
  -> Stage1 定義正確: 請問目前用藥、過敏、慢性病、家族史？...
  -> Stage2 症狀：口乾＋晚上頻尿，三個月前開始，程度中等
  ✓ 已記錄 onset=三個月前, description=口乾；晚上頻尿, severity=中度
  -> Stage2 定義: 請問症狀的相關資訊？（可一次說明，...
  -> Stage3 想問醫師：飲食與藥物副作用
  ✓ 已記錄 questions_for_doctor=['飲食怎麼控制', '藥物有什麼副作用']
  -> Stage3 定義: 請問您想在看診時詢問醫師哪些問題？...
  -> Review & Confirm：產生 PreVisitSummary
  ✓ 摘要已產生，provided=['known_medications', 'allergies', 'chronic_conditions', 'family_history', 'symptom_onset', 'symptom_description', 'symptom_severity', 'questions_for_doctor'], missing=[]
  ✓ 摘要本文（截斷）：已知用藥：metformin；過敏史：無；慢性病史：高血壓；家族史：無；症狀起始：...
  ✓ 免責聲明存在：本摘要僅整理您已提供...
  -> 確認提交（模擬 ProductSession SUBMITTED）
  ✓ Review & Confirm 完成，8 欄皆已整理（或標記待確認）
  ✓ Workflow Review & Confirm 節點可達
✓ 情境 1 通過

=== 情境 2：intake＋衛教多意圖（口渴 + 水果份數） ===
  -> 先備 intake：known_medications=metformin, allergies=無
  -> 多意圖輸入：我最近常口渴，糖尿病一天可以吃幾份水果？
  ✓ 口渴已抽取：常口渴
  ✓ intake 已寫入 symptom_description=常口渴
  -> 驗證衛教回答或誠實 fallback
  ✓ 衛教完成（COMPLETED/D PASS），回覆長度=...
  ✓ 衛教回答或誠實 fallback 已驗證
  -> 驗證 intake stage 不得遺失
  ✓ prior intake 仍保留（workflow 未污染）：['metformin']
  -> intake_stage=...（不得為空或錯誤清空）
  ✓ intake stage 未遺失
✓ 情境 2 通過

=== 情境 3：分享與醫護閱讀（短效 ShareGrant，唯讀） ===
  -> 已建立 SUBMITTED 病患 session（8 欄完整）
  ✓ session_id=demo-s***, principal=a*** 
  ✓ 已建立 ShareGrant grant_id=grant-***, TTL=600s, single_use=True
  ✓ raw token 未落盤（僅存 hash）
  ✓ 醫護已唯讀查看：grant_id=grant-***, intake=['metformin']
  ✓ 摘要 disclaimer 存在，D PASS 已驗證
  -> 驗證醫護不能修改病患資料
  ✓ 醫護無法冒用病患身份建立 ShareGrant（已阻擋）
  ✓ 單次使用限制生效：share grant is not active
  ✓ 權限檢查：PRACTITIONER 僅 VIEW_GRANTED_CLINICAL_SUMMARY，無病患寫入權
  -> 驗證 10 分鐘 TTL 過期後不可用
  ✓ TTL 過期正確拒絕：share grant expired
  ✓ 暫存 SQLite 已自動清理（tempfile）
✓ 情境 3 通過

=== 情境 4：紅旗（胸痛＋喘不過氣 → 119／急診） ===
  -> 輸入：我胸口很痛而且喘不過氣
  -> 先備 intake：['metformin']
  -> 回覆：偵測到可能的緊急警訊。請立即停止使用本系統，撥打 119 或...
  ✓ 回覆含 119／急診指引
  ✓ 狀態=FALLBACK, fallback_reason=A_EMERGENCY
  ✓ trace 顯示 RED_FLAG_DETERMINISTIC_ABORT（a_node 直接中斷）
  ✓ 未進入 RAG/檢索（不等待 AI）
  -> 驗證不污染 intake
  ✓ intake_snapshot 未被污染：...
  ✓ fallbacks.py A_EMERGENCY 含 119
✓ 情境 4 通過

=== 全部 4 情境通過 ===
```

## 失敗時（exit 非 0）

```
  ✗ 情境 X 失敗：...
Traceback ...
=== Demo 失敗（非 0 離開） ===
```

## 重試 3 次

```bash
for i in 1 2 3; do echo "Run $i"; python scripts/demo/run_engineering_demo.py || exit 1; done
# 每次皆應顯示「=== 全部 4 情境通過 ===」且 exit 0
```

## --live-formal 模式

```
NOTE: --live-formal 已啟用，將嘗試外部 LLM/RAG（若無 .env/Ollama 可能失敗）
  -> live-formal probe: status=COMPLETED, len=...
```

若無 `.env`（`OPENCODE_API_KEY`）或 Ollama，probe 可能顯示 `失敗（可忽略）`，不影響 deterministic 4 情境。

## 與測試對照

- `test_workflow_integration.py`（Fixture 路徑）應 `15 passed`
- `test_agent_demo_cases.py` 非 `real_retriever` 部分應通過（`real_retriever` 需本地模型，缺時 skip）
- `test_share_grants.py` 應 `7 passed`（含 TTL、單次使用、hash 不落地）

## 不應出現的資訊

- 完整 `session_id / grant_id / token / principal_id_hash` 明文
- `OPENCODE_API_KEY / LINE_CHANNEL_SECRET / raw image bytes`

> 若文件需擷取，請以 `***` 遮蔽或僅保留前 6 碼。
