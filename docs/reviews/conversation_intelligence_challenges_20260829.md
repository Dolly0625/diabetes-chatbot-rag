# LINE Bot 對話智慧化：實作困難與架構演進紀錄

> 日期：2026-08-29  
> 範圍：病患／家屬 LINE Bot 的看診前資料蒐集、衛教問答、多輪上下文、多意圖與延遲優化  
> 用途：後續專題報告、技術簡報與研究限制章節的原始紀錄  
> 安全前提：固定維持 A→B→C→D 與 E trace；紅旗、授權、資料寫入及分享條件不交由生成式 AI 自由決定。

## 1. 問題背景

本專案最初已能完成糖尿病衛教、看診前資料蒐集與醫護摘要，但真實對話體感仍像規則式機器人：使用者換一種說法就可能答非所問，也容易讓人感覺每一輪都是第一次對話。

產品目標後來收斂為：病患可以在同一段 LINE 對話中自然聊天、詢問衛教，同時逐步整理看診前資訊；醫護端只能閱讀病患確認並授權分享的摘要。AI 應負責理解人話，程式與狀態機仍負責安全、授權、驗證及最終寫入。

## 2. 為什麼這段開發比預期困難

### 2.1 規則很快，但無法涵蓋人類說法

早期以 regex、關鍵字及封閉字串判斷意圖，延遲低且容易測試，但泛化能力有限。例如系統能理解「頻尿」，不一定理解「晚上一直跑廁所」；能識別少數 AI 身分問法，卻可能接不住「現在是機器人在回覆嗎」。持續增加 regex 只能治標，無法追完所有口語變體。

### 2.2 啟用 AI 後，控制權一度放錯位置

正式 `FormalConversationInterpreter` 上線後，曾發生 AI 先攔截「為自己整理」等產品命令，使 intake 正式流程無法啟動。單元測試使用 Fake 或 deterministic 路徑時全綠，生產 Formal 路徑卻失敗。

最後確立的優先序為：

```text
紅旗規則
→ 授權與身分規則
→ 明確產品命令
→ 高精度單值 fast path
→ AI 理解一般自然語句
→ 候選驗證與 PendingAction
→ 狀態機決定是否寫入
```

這次問題顯示：AI 可以解讀語意，但不能取得產品命令、權限或醫療資料寫入的最終控制權。

### 2.3 規則「部分命中」反而阻止 AI 理解完整句子

P1.1 雖已啟用 AI，但 deterministic extractor 只要產生任何候選就可能提前結束。例如：

> 我嘴巴很乾，晚上一直跑廁所。

規則先抓到「口乾」後便不再交給 AI，因此遺漏「頻尿」。P2A 將流程改為 deterministic 與 Formal candidates 合併：規則可以補充候選，但不能因為只理解半句就阻止 AI 查看完整輸入。

修改後，實際 Formal 路徑能將上述句子落地為「口乾；頻尿」，同時維持 AI 只提出候選、狀態機才可寫入的限制。

### 2.4 理解更多之後，必須防止錯誤寫入

生成式 AI 的泛化能力提高後，也帶來新的資料正確性風險。系統必須區分：

- 本人陳述：「我最近一直口渴。」
- 醫療問句：「一直口渴會是糖尿病嗎？」
- 假設情境：「如果以後開始頭暈怎麼辦？」
- 第三人資料：「我朋友最近一直口渴。」
- 資料對象修正：「不是我，是我媽媽在吃。」

以上句子可能包含相同醫療詞彙，但只有第一種可以直接成為本人 intake candidate。因而加入 confidence、source quote、provenance、否定、問句、假設、第三人與 subject clarification 等防護；低信心或歸屬不明時只追問，不直接寫入。

### 2.5 同義詞去重不能只做字串轉換

AI 與規則可能同時產生「高血壓」與 `hypertension`。若不去重，醫護摘要會出現重複資料；但過度正規化也可能造成更嚴重的錯併，例如把 `insulin glargine`、`insulin degludec` 與 `insulin lispro` 全部壓成同一個 `insulin`。

另一個邊界是跨子句否定：

> 我沒有高血壓，但有糖尿病。

若只掃描整句是否出現「沒有」，可能連糖尿病也一起刪除。因此 canonicalization 必須限定欄位與封閉概念，否定判斷則必須回到候選所在子句。原始 `source_quote` 仍需保存，供 Review & Confirm 與醫護端追溯。

### 2.6 多意圖不是單純的分類問題

核心使用情境之一是病患同時提供資料並詢問衛教：

> 我最近常口渴，糖尿病一天可以吃幾份水果？

理想結果是「口渴」經驗證後寫入 intake，同時回答水果衛教，且 intake stage 不被打亂。實作中曾出現 dry-run 可以落地，但 live Formal 路徑被 `SIDE_ANSWER` 提前返回，造成資料未保存。

這說明多意圖不能只輸出 `INTAKE + EDUCATION` 標籤；還需要切分子句、分別執行資料與衛教分支、等待安全驗證，再以單一且冪等的 session transition 合併結果。

### 2.7 準確度提升造成明顯延遲

P1.1 live smoke 的參考結果為：

| 階段 | p50 | p95 | 備註 |
|---|---:|---:|---|
| P1.1 Formal interpreter | 3.842s | 5.632s | 11 輪，fallback 0/11 |
| P2A 整體 live smoke | 9.9s | 16.9s | 少量且題型較重；包含複合與多意圖 |

P2A 的「口乾＋跑廁所」單輪約 5.2 秒完成語意泛化；紅旗混合句則約 13ms 由規則攔截，不等待 AI。這證明延遲主要出現在需要生成式理解或衛教生成的路徑，而非狀態機與安全閘本身。

目前完整 mixed-intent 請求可能依序執行：

```text
Conversation Interpreter（理解與結構化候選）
→ RAG（搜尋衛教證據）
→ Formal C Generator（根據證據生成答案）
→ D gate
```

因此「單次 conversation LLM」不等於整輪只有一次模型呼叫。純 intake 通常只需要理解模型；純衛教不應先付出一次 conversation interpreter；mixed intent 才可能同時需要理解與 grounded answer generation。

### 2.8 原有 timeout 是邏輯超時，不一定是使用者等待上限

目前部分程式以 `ThreadPoolExecutor` context manager 包住 `future.result(timeout=...)`。即使 `result` 已拋出 timeout，離開 `with` 時仍可能執行 `shutdown(wait=True)`，繼續等待底層網路請求完成。因此系統可能已決定 fallback，使用者卻仍等待超過設定秒數。

真正的延遲上限需要使用 HTTP／SDK 原生 timeout、可傳遞的 deadline，以及確保超時工作不再寫入 session 或推送 LINE 回覆。單純提高 45／120 秒上限不算修復。

## 3. 架構決策：採用混合式快速路由

經討論後，不建議讓每一句都直接進生成式 AI，也不建議完全以 Semantic Router 取代 interpreter。較合適的目標架構為：

```text
第一層：deterministic safety（數毫秒）
- 紅旗、授權、subject、產品命令

第二層：本地 Semantic Router（目標 warm p95 <150ms）
- PURE_EDUCATION / PURE_INTAKE / MIXED
- CORRECTION / SUBJECT_CHANGE / CHITCHAT / UNKNOWN

第三層：依複雜度執行
- 純衛教：直接 RAG＋C
- 明確短答案：deterministic fast path
- 一般自然 intake：結構化 interpreter
- mixed／修正／指代不明／低信心：完整 AI
```

現階段優先以專案既有本地 `bge-m3` 建立 semantic prototype，避免立即增加模型與訓練流程。Semantic Router 只負責分流，不得直接寫入病患資料，也不得取代 deterministic red-flag gate。SetFit 留待累積足夠人工標註語料後再評估。

上線策略應先採 shadow mode：記錄 Semantic Router 會選哪條路，但暫時不改正式結果；通過未見變體、multi-label、信心門檻與 fallback 評估後，才讓高信心純衛教或閒聊跳過 conversation interpreter。

## 4. 測試與版本演進

| 階段 | Commit | 測試數 | 主要成果 |
|---|---|---:|---|
| P5＋P0＋PendingAction | `f9a7e6d` | 243 | 對話自然化基礎、資料修正、pending lifecycle |
| P0.5 authorization boundary | `08600e1` | 253 | 外部入口 fail-closed、分享與角色邊界 |
| P1 ContextEnvelope | `ef8da93` | — | 有界上下文、subject 隔離 |
| P1 multi-turn/multi-intent | `2e0f5d6` | 278 | 跨輪修正、多意圖基礎 |
| P1.1 Formal interpreter | `683b5dd`、`5e5cb8e` | 296 | 正式 AI 理解上線、控制句防污染 |
| P1.1.1 safety closure | `b496308` | 313 | 修正產品命令優先序、身分與 subject 安全 |
| P2A candidate merge | `c64bcf7` | 405 | partial match 不再阻止 AI、多症狀泛化 |
| P2A.1-A data quality worktree | `a3595cb`（待 amend） | 439 | 中英同義去重；審核後要求補藥品子類與跨子句否定 |

截至本文撰寫時：

- 主線 HEAD 為 `c64bcf7`，405 tests，tree 乾淨。
- `p2a1-data-quality` worktree 尚未合併，正補修審核邊界。
- `p2a1-latency` worktree 正處理 mixed-intent、分階段 latency、真正 timeout 與安全並行。
- 所有列出的 commits 均尚未 push；後續應在整合驗收後建立遠端備份分支。

## 5. 可用於報告的研究觀察

1. **規則與 LLM 不是二選一。** 規則適合安全、授權與封閉控制；LLM 適合口語、多輪與非封閉語意。
2. **模型輸出結構化不代表資料可直接寫入。** 還需要 provenance、confidence、polarity、subject 與 state-machine validation。
3. **測試全綠不保證正式路徑可用。** Fake interpreter 曾遮蔽生產 Formal 路由失敗，因此每輪至少要有一條 production construction path 與獨立 live smoke。
4. **多意圖需要執行層合併，不只分類層標籤。** 每個意圖可能對應不同工具、閘門與 state transition。
5. **提高理解率會增加模型使用率與延遲。** partial-match 修復後，更多自然句進入 AI；因此必須同時設計 fast path、semantic routing、deadline 與並行。
6. **醫療資料正規化必須保守。** 錯誤合併不同藥品，可能比保留兩個待確認項目更危險。
7. **端到端延遲要拆階段量測。** 只報 total p50/p95 無法判斷 interpreter、RAG 或 generator 才是瓶頸。

## 6. 後續工作順序

1. 完成並獨立驗收 P2A.1-A：同義詞、藥品子類與子句級否定。
2. 完成並獨立驗收 P2A.1-B：mixed-intent 落地、stage latency、真正 timeout、安全並行。
3. 在主線依序整合兩個 worktree，重跑完整 pytest、紅旗、授權、PendingAction 與 live Formal smoke。
4. 開始 P2A.2 Semantic Router：先離線評估，再 shadow mode，最後只啟用高信心快速路徑。
5. 完成 P2B 自然表達層；不得增加新的串行 LLM rephraser。
6. 完成病患 Review／分享 UI、醫護摘要 UI 與 LINE 手機端 E2E 展示。

## 7. 報告撰寫建議

這段經驗可以整理成「醫療對話系統的準確性、延遲與安全三角」：

- 規則式系統快速且可控，但口語泛化不足。
- 生成式 AI 提升理解能力，但增加延遲與錯誤寫入風險。
- 混合架構透過 deterministic safety、semantic routing、LLM interpretation 與 state validation 分工，嘗試在三者之間取得平衡。

建議報告保留三組對照案例：「口乾＋跑廁所」代表泛化、「問句／朋友／假設」代表資料防污染、「intake＋水果衛教」代表多意圖與延遲。這三組案例能完整呈現架構演進的理由，而不只是羅列測試數量。
