# 04｜三個最終 Cloud Trace

資料來源是 `report_handoff/traces/CLOUD_LLM_final_three_cases_trace.jsonl`。檔案共有 4 行：三個案例，其中 ASK_USER 的補充回答以另一個 re-entry record 保存。

這份 trace 使用真實 Cloud LLM Planner 與真實 TFDA retrieval；demo 中的 B approval 仍是明確標示的 deterministic demo gate，不能解讀成已驗證的臨床判定。

## 案例一：AG-ASK-001

原始問題：

> 我家人吃糖尿病藥後腳怪怪的，我要注意什麼？

第一段軌跡：

```text
A COMPLETED
→ QUERY_EXPANSION
→ RAG
→ B INSUFFICIENT
→ AGENT ASK_USER / MISSING_REQUIRED_CONTEXT
→ ASK_USER NEEDS_CLARIFICATION
→ SYSTEM NEEDS_CLARIFICATION
```

B 提供 `medication_class` 這個 missing-information signal。Planner 沒有從 top-k 候選猜「使用者一定在吃 SGLT2」，而是要求補充用藥類別。

Question Builder 不是 LLM 自由生成；graph 依欄位映射成固定問題。

補充後問題：

> 我家人吃 SGLT2 抑制劑後腳怪怪的，我要注意什麼？

第二段是新 request：

```text
A → QUERY_EXPANSION → RAG → B PASS → C → D PASS → COMPLETED
```

學習重點：

- ASK_USER 只在 B 明確指出必要缺失資訊時成立。
- 補充資料必須重新經過 A。
- 第二段沒有延續第一段的 graph instance。

## 案例二：AG-REWRITE-001

原始問題：

> 吃 SGLT2 下體不舒服要注意什麼？

軌跡：

```text
A → Expansion → RAG → B INSUFFICIENT
→ AGENT REWRITE_QUERY / QUERY_FORMULATION_NEEDS_REWRITE
→ QUERY_REWRITER
→ Expansion → RAG → B PASS → C → D PASS → COMPLETED
```

Cloud Rewriter 產生：

> 服用 SGLT2 藥物後生殖器或會陰部不舒服，需要注意什麼？

這個 rewrite：

- 保留 SGLT2。
- 保留不舒服與注意事項的原始問題範圍。
- 把口語「下體」轉成 corpus 更容易匹配的標準詞。
- 沒有直接回答問題。

學習重點：

- Agent Planner 只選 action，真正文字由 Rewriter 產生。
- Rewrite 完成後 graph 固定回到 Query Expansion，而非直接呼叫 RAG 內部方法。
- 第二次 B PASS 後才允許進入 C。

## 案例三：AG-FALLBACK-001

原始問題：

> 糖尿病患者使用 Semaglutide 後視力模糊風險有哪些？

軌跡：

```text
A → Expansion → RAG → B INSUFFICIENT
→ AGENT #1 REWRITE_QUERY
→ Rewriter → Expansion → RAG → B INSUFFICIENT
→ AGENT #2 FALLBACK / RECOVERY_EXHAUSTED
→ FALLBACK → SYSTEM
```

第一次 Agent 嘗試把 query 收斂為：

> 糖尿病患者使用 Semaglutide 後視力模糊風險

但第二次 retrieval 仍沒有對應 evidence，Planner 從 `previous_attempts` 看見一次合理 rewrite 已失敗，因此選擇 `FALLBACK`。

學習重點：

- 不是每個 B insufficient 都立即 fallback；可以做一次有限恢復。
- 沒有證據時不使用近鄰文件硬答。
- `max_agent_steps=2`、`max_rewrites=1` 與 previous attempts 共同避免無限循環。

## 三案比較

| 案例 | 真正問題 | 第一次 action | 是否再跑 RAG/B | 終點 |
| --- | --- | --- | --- | --- |
| ASK | 缺使用者必要事實 | ASK_USER | 補充後以新 request 重跑 | COMPLETED |
| REWRITE | 事實完整但用詞不利檢索 | REWRITE_QUERY | 同一 workflow 重跑 | COMPLETED |
| FALLBACK | corpus 沒有對應證據 | REWRITE_QUERY，再 FALLBACK | 重跑一次 | FALLBACK |

## 自己讀 JSONL 時先找的欄位

- record：`case_label`、`status`、`final_response`
- event：`component`、`status`
- Planner：`agent_action`、`requested_action`、`reason_code`
- Recovery：`current_query`、`rewritten_query`、`retrieval_attempt`、`b_attempt`
- Limits：`step_count`、`rewrite_count`、`clarification_count`
- End：`termination_reason`、`fallback_reason`

不要只看最終 status。Agent 設計是否正確，通常要從事件順序、requested action 與實際 action 的差異判斷。
