# LLM 與 RAG 跨組對齊追蹤

> 文件用途：持續追蹤 LLM 組與 RAG 組之間的介面問題、共同決策、規格落地與驗證證據。  
> 維護方式：後續有新回覆、新 Schema、新程式或測試結果時，直接更新本檔，不另開 `v2`、`final` 或日期副本。  
> 最後更新：2026-08-24  
> 目前階段：M1 對齊完成度檢查／M2 開工前

## 一、目前結論

LLM 組於 2026-08-23 提供的《LLM 組與 RAG 組對齊問題》**確實有發揮作用**。它至少完成以下三件事：

1. 揭露原共用查詢會在 Policy Gate 被攔截，不會進入 RAG。
2. 關閉「高風險事實由誰處理」的責任真空。
3. 讓 RAG 組開始補外層回應、風險欄位、部分 Graph 路徑與重新搜尋等跨組介面。

但目前不能說九項問題都已解決。現況是：

- **4 項已有明確設計決策**；
- **4 項只有原則同意，正式 Schema、程式或測試仍缺**；
- **1 項尚未共同定案**；
- RAG 文件中的「接受／可答」目前主要是 RAG 組單方書面回覆，尚未看到雙方簽認的版本化 Shared Schema 或會議決議紀錄。

因此最準確的說法是：

> 昨天的文件成功把模糊問題變成可追蹤的跨組工作，但尚未把所有工作變成可執行、可驗證的共同契約。

## 二、判定標準

| 標記 | 意義 |
|---|---|
| ✅ 已對齊 | 雙方責任或行為已有清楚答案，剩下屬正常實作工作 |
| 🟡 部分對齊 | 已同意方向，但 Schema、程式、數值或驗證證據尚缺 |
| 🔴 未定案 | 問題仍有關鍵選項未決，無法視為共同規格 |
| ⚪ 未驗證 | 文件宣稱已有產物，但目前工作區沒有足夠檔案可重跑或複驗 |

## 三、九項問題追蹤表

| # | 對齊問題 | RAG 組目前回覆 | 決策狀態 | 實作／驗證狀態 | 下一個必要動作 |
|---|---|---|---|---|---|
| 1 | RAG 能否接收 LLM 請求欄位？ | 認為 `user_raw_input`、`retrieval_queries` 足夠；`guardrail_result` 只讀不改 | ✅ | 🟡 尚無正式 LLM→RAG Request Schema 與契約測試 | 建立版本化 `RetrievalRequest` Schema，驗證未知 enum、缺欄位與錯誤型別 |
| 2 | 哪些 LLM 標籤影響 Vector／Graph／Hybrid？ | 已提出 `intent_tags`、`polarity`、`target_subject`、`language` 的路由對映；`risk_flags` 不由 RAG 改寫 | ✅ | 🟡 目前是文件規則，尚無 routing table／程式測試 | 將對映移入 Shared Registry，為每種標籤建立 routing 測試 |
| 3 | Chunk 是否定案，是否補外層請求與搜尋狀態？ | Chunk 草案已校對；同意新增外層 `RetrievalResponse` | 🟡 | 🔴 外層封套尚不存在；`chunk_id` 與日期格式仍有 TBD | 完成外層 Schema，並修正 chunk id、日期與 Graph 條件必填規則 |
| 4 | 是否回傳證據風險等級、訊號類型與依據？ | 已提出 relation→`evidence_risk_level`／`safety_signal_types` 對映，`risk_basis` 由來源與關係組成 | 🟡 | 🔴 三欄尚未加入目前 JSON Schema，也未經臨床專業審核 | 納入 Schema、建立推導測試，再由臨床人員審核高風險對映 |
| 5 | 部分路徑、Shared Registry、Graph 分數校準是否定案？ | 接受部分路徑回傳、Shared Registry 與 Graph 自有校準，不沿用 CRAG 門檻 | 🟡 | 🔴 `graph_path_status` 未入契約；Registry 與六類校準集不存在 | 先凍結 registry schema，再產生 Graph 六類校準資料與 threshold report |
| 6 | 六種 Retrieval Status 是否可用？ | 接受 `SUCCESS／EMPTY／PARTIAL／STALE／CONFLICT／ERROR` | 🟡 | 🔴 目前 Chunk Schema 無外層 `retrieval_status` | 加入 `RetrievalResponse` 並測試六種狀態的 end-to-end 行為 |
| 7 | 問題改寫、重新搜尋與停止由誰負責？ | LLM 保留原意並產生搜尋問題；RAG 做同義詞與路由；RAG 可建議重搜；workflow 決定；最多一次 | ✅ | 🟡 尚未看到跨組 end-to-end trace | 用同一 `request_id` 跑一次 rewrite／rerun trace，驗證最多重搜一次 |
| 8 | 找到禁忌或嚴重警告後如何分工？ | RAG 標記並附來源；Context Gate 判斷可用性；Generator 不做個人決策；Output Gate 最終攔截 | ✅ | 🟡 責任已清楚，但 RAG 的風險欄位與真實整合測試尚缺 | 建立一個禁忌、一個注意、一個一般副作用的跨組測試 |
| 9 | Direct／Indirect Prompt Injection 如何分工？ | Direct 由 LLM；RAG 同意負責來源控制與 retrieved content 檢查；目前只做來源白名單 | 🔴 | 🔴 無入庫掃描、取回後掃描、`warnings`、隔離流程、共同樣本庫或 threshold | 雙方先定事件格式與處置矩陣，再決定是否共用樣本庫／模型／版本 |

### 統計

- ✅ 已有明確決策：4／9（第 1、2、7、8 項）
- 🟡 部分對齊：4／9（第 3、4、5、6 項）
- 🔴 未共同定案：1／9（第 9 項）
- 完整做到「規格＋程式＋測試證據」：目前 0／9

## 四、已被昨天文件實際解決的問題

### 4.1 共用案例改為兩層展示

原案例：

> 我腎功能不好，可以吃 metformin 嗎？

它屬個人化用藥問題，應由 LLM Policy Gate 攔截，因此不應再被描述成會直接進入 RAG 的查詢。

目前建議保留兩個版本：

| 查詢 | 用途 | 預期模組 |
|---|---|---|
| 我腎功能不好，可以吃 metformin 嗎？ | 安全攔截與專業轉介 | Policy Gate |
| 腎功能不佳者使用 metformin 時，有哪些一般注意事項？ | 一般資訊檢索 | Vector＋Graph Retriever |

這項問題已被成功發現，也有合理的設計答案；但 RAG 組仍缺可檢索的本地 metformin 權威來源，所以第二條展示目前尚不能完整跑通。

### 4.2 高風險事實的責任真空已關閉

目前共同分工是：

```text
RAG：標記風險事實並保留來源
  ↓
Context Gate：判斷證據是否可信、足夠、可用
  ↓
Generator：只產生一般說明，不替個人做用藥決定
  ↓
Output Gate：最後安全檢查與必要轉介
```

這項分工可以視為已對齊。後續問題是實作和測試，不是責任歸屬。

### 4.3 Graph 的三個 Boundary TBD 已有答案

目前方向為：

- Hop Limit 用盡但已有有效證據：回傳部分路徑並標記 `PARTIAL`；
- Relation Type、`score_type`、`status` 分開治理，但放在同一份版本化 Shared Registry；
- `graph_traversal` 不沿用 CRAG 的門檻，另建 Graph 校準資料。

這些方向合理，但尚未出現在可執行的共同 Schema 與測試中，因此仍列為部分對齊。

## 五、尚未解決的核心問題

### P0：阻擋跨組整合

1. **沒有正式 `RetrievalRequest`／`RetrievalResponse` Schema。**
2. **沒有 Vector＋Graph Merge Strategy。**
3. **RAG 的 evidence risk 三欄尚未加入實際 Schema。**
4. **缺少能完整跑通的旗艦案例資料。**
5. **RAG 實作 repo、37 triples 與 85 chunks／embeddings 未隨目前資料夾交付，無法獨立複驗。**

### P1：阻擋可信評測

1. Graph 三條交互作用邊仍待重抽。
2. Vector URL 切分 bug 仍待修正與重新 embedding。
3. 日期格式尚未正規化。
4. 大部分醫學實體尚未完成詞彙接地。
5. 風險等級缺臨床專業審核。
6. Graph 分數與 Merge 排序都沒有校準資料。

### P2：產品化前必須補

1. Retrieved content 的 Indirect Prompt Injection 檢查。
2. 資料更新、撤銷、取代與重建索引流程。
3. 人工複核佇列與異常處理責任人。
4. 全量 129 筆處理與可重現報告。

## 六、LLM 組與 RAG 組應共同凍結的最小契約

### 6.1 LLM → RAG：`RetrievalRequest`

至少包含：

- `request_id`
- `schema_version`
- `user_raw_input`
- `retrieval_queries[]`
- `guardrail_result`
- `language`
- `timestamp`

規則：僅 `router_status = G_GENERAL_EDUCATION` 可進入一般檢索；RAG 不修改 LLM 標籤。

### 6.2 RAG → LLM：`RetrievalResponse`

至少包含：

- `request_id`
- `schema_version`
- `retrieval_route`
- `retrieval_status`
- `graph_path_status`
- `rerun_suggested`
- `warnings[]`
- `chunks[]`

每個 Chunk 除既有欄位外，需共同確認：

- `evidence_risk_level`
- `safety_signal_types[]`
- `risk_basis`
- Graph 高風險 relation 的 `confidence` 與 `negation_checked`

## 七、下一次更新本檔時的規則

每次收到 RAG 組新檔案或回覆時，依下列順序更新：

1. 更新頁首的「最後更新」日期與階段。
2. 更新九項追蹤表的「決策狀態」與「實作／驗證狀態」。
3. 只有看到下列任一證據，才能把實作狀態改成完成：
   - 正式版本化 Schema；
   - 可執行程式碼；
   - 可重現輸出檔；
   - 自動化測試與結果；
   - 雙方會議決議或明確書面確認。
4. 不能只因文件寫「已完成」「已定案」就視為技術完成。
5. 若規格改名或棄用，直接更新本檔主表，並在下方變更紀錄保留摘要。
6. 不建立另一份追蹤文件；本檔是此議題的唯一維護入口。

## 八、來源文件

- LLM 組提問與建議：[`docs/proposal/LLM組與RAG組對齊問題.md`](../../docs/proposal/LLM組與RAG組對齊問題.md)
- RAG 組整體回覆：[`RAG 組 — M1 工作報告與計畫修訂.md`](RAG%20組%20—%20M1%20工作報告與計畫修訂.md)
- RAG Chunk Schema：[`Multi-RAG - B (校對版).json`](Multi-RAG%20-%20B%20(校對版).json)
- RAG 路由分類：[`Multi-RAG - A (校對版).md`](Multi-RAG%20-%20A%20(校對版).md)
- Graph 管道狀態：[`Preprocessing - B.md`](Preprocessing%20-%20B.md)
- Boundary 執行限制：[`Boundary - B (校對版) (1).md`](Boundary%20-%20B%20(校對版)%20(1).md)
- Boundary 失敗模式：[`Boundary - C (校對版) (1).md`](Boundary%20-%20C%20(校對版)%20(1).md)

## 九、變更紀錄

### 2026-08-24｜建立追蹤文件

- 逐項對照 LLM 組九項問題與 RAG 組 M1 工作報告。
- 將「有書面回答」與「已有可驗證實作」分開標示。
- 建立 P0／P1／P2 缺口與固定維護規則。

