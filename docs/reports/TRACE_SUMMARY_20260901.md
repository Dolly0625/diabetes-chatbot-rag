# 去識別化 Trace 結果摘要

**整理日期：** 2026-09-01
**用途：** 專題展示與架構說明，不是病患資料、臨床研究結果或模型效能保證。

## 為什麼公開的是摘要，而不是原始 trace

E 觀測層會記錄每一輪的 A→RAG→B→C→D 決策路徑，也可能保有供本機除錯用的使用者原句與雜湊。原始 JSONL trace 因此保留在本機 archive，**不放到公開 GitHub**。

本報告只保留去識別化後的案例代號、各關卡狀態與最終結果；不包含原始問題、姓名、病歷資料、request ID、query hash、access token、API key 或完整模型回答。

## Trace 格式

每一請求會有下列結構化節點：

```text
SYSTEM → A（路由）→ QUERY_EXPANSION → RAG（檢索）→ B（證據檢核）
       → C（生成）→ D（輸出檢核）→ SYSTEM 結束
```

- B 證據不足時，系統可選擇詢問澄清、重寫查詢，或安全 fallback。
- 只有 B 通過後才會進 C，且 C 的回答仍要通過 D 才能輸出。
- E 只做觀測與脫敏記錄，不會改寫回覆、不會放寬安全規則。

## 擷取的四筆代表性結果

來源為 2026-08-21 的本機 formal-workflow trace；均使用合成案例代號。這些結果用來展示安全流程的分支行為，**不是目前 RAG 檢索品質的統計數字**。

| 案例 | 觀察到的路徑 | 最終結果 | 說明 |
| --- | --- | --- | --- |
| `AG-ASK-001` | A 完成 → RAG 完成 → B 證據不足 → Agent → ASK_USER | `NEEDS_CLARIFICATION` | 證據不足時不直接編造回答，改為要求補充必要資訊。 |
| `AG-ASK-001` 澄清後 | A → RAG → B 通過 → C → D | `COMPLETED` | 使用者補足可辨識的資訊後，才生成並通過輸出檢核。 |
| `AG-REWRITE-001` | A → RAG → B 證據不足 → Query Rewriter → B 通過 → C → D | `COMPLETED` | 第一次檢索不足時，使用受限的查詢重寫重新檢索；不是讓模型任意回答。 |
| `AG-FALLBACK-001` | A → RAG → B 證據不足 → Agent／Rewriter → FALLBACK | `FALLBACK` | 重寫後仍不足時，安全結束並要求由合格醫療人員評估。 |

## 本次程式驗證

2026-09-01 在主專案 Python 3.10 環境執行：

```bash
python -m pytest -q \
  tfda_context_gate/tests/test_e_observability.py \
  tfda_context_gate/tests/test_workflow_integration.py
```

結果：**23 passed**。

這組測試確認 E 觀測層的基本 trace 生命週期、資料脫敏、錯誤 fail-open 行為，以及工作流整合沒有因觀測功能而中斷。

## 展示時可以怎麼說

> 系統不是只看最後答案，而是會留下去識別化的流程紀錄：問題先經過意圖與急症判斷、再檢索官方資料、檢查證據是否足夠，最後才產生並檢查回答。若證據不足，系統會要求補充或安全地不回答，而不是硬湊一個答案。

## 限制與後續建議

- 這份報告刻意不公開 raw trace；若要做團隊內除錯，應在受控環境檢視原始資料。
- 代表性案例只能說明分支是否存在，不能當作準確率、召回率或臨床效益宣稱。
- 目前 RAG 組的檢索品質應以其獨立 eval 資料集量測；主專案負責記錄 B/D gate 是否放行與安全 fallback 是否正確。
