# TFDA Context Gate Phase 5：Hybrid Context Gate 實驗報告

## 先講為什麼需要 Hybrid

Phase 4 的 LLM Judge 判得很好：10 篇文件和人工標註全部一致，也能把只有錯安全主題的 Context Set 判成 `FALLBACK`。

但問題是成本和延遲。Phase 4 一輪要對 10 篇文件逐篇 Judge，再做 Set-level Judge，平均約 93.11 秒，而且一輪有 13 次 LLM 呼叫。

所以 Phase 5 想解決的不是：

> 怎麼讓 LLM 判得更聰明？

而是：

> 能不能先用便宜的方法縮小資料，最後只用一次 LLM Judge？

本次 Hybrid 流程是：

```text
Contract Gate
    ↓
Similarity Retriever Top-20
    ↓
Cross-Encoder Reranker
    ↓
選 Top-3 / Top-4 / Top-5
    ↓
一次 Set-level LLM Judge
    ↓
usable_document_ids
    ↓
PASS / REVIEW / FALLBACK
```

這一階段沒有加入 Agent 或 LangGraph，也沒有再做逐篇 Document-level Judge。

## 一、固定的資料、模型與 Query

### Corpus 與 Contract Gate

仍然使用同一份 TFDA 真實資料和 LangChain Documents：

```text
Corpus size: 129
Contract passed: 129
Contract rejected: 0
```

Contract Gate 仍然只檢查：

- `document_id`
- `row_index`
- `發布日期`
- `藥品成分`
- `page_content`

沒有自行新增 `status`、`version` 或 `superseded`，因為 TFDA 主資料沒有這些欄位。

### 模型

- Embedding：`intfloat/multilingual-e5-small`
- Reranker：`BAAI/bge-reranker-v2-m3`
- Judge：`deepseek-v4-flash`
- Temperature：`0`
- Endpoint：OpenCode Go OpenAI-compatible Chat Completions

所有模型先 warm-up，再開始量測。模型下載和第一次初始化沒有算進 inference latency。

### Query

Narrow Query：

> `TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？`

Broad Query：

> `TFDA 對 SGLT2 抑制劑類藥品有哪些安全警訊？`

## 二、Top-3、Top-4、Top-5 的實際結果

每個 Variant 都是：Similarity Top-20 → Reranker → 一次 Set-level Judge，並且每個結果重跑 3 次。

### Narrow Query

| Variant | Judge usable IDs | Judge excluded IDs | Decision | Context Precision | Direct Evidence Recall |
|---|---|---|---|---:|---:|
| Top-3 | `tfda-risk-0019`（酮酸中毒） | `tfda-risk-0064`、`tfda-risk-0042` | PASS | 1.00 | 1.00 |
| Top-4 | `tfda-risk-0019`（酮酸中毒） | 再加 `tfda-risk-0035` | PASS | 1.00 | 1.00 |
| Top-5 | `tfda-risk-0019`（酮酸中毒） | 再加 `tfda-risk-0015` | PASS | 1.00 | 1.00 |

三種 Top-N 的三次結果都一致：

- 真正酮酸中毒文件都被保留。
- Fournier’s gangrene、截肢、急性腎損傷都沒有被當成 Narrow Query 的 usable evidence。
- Top-5 多放進一篇 Codeine 不相關文件，但 Judge 把它排除。

因此 Narrow Query 其實 Top-3 就已經足夠通過 Gate。

### Broad Query

| Variant | Judge usable IDs | Judge excluded IDs | Decision | Context Precision | Direct Evidence Recall |
|---|---|---|---|---:|---:|
| Top-3 | `tfda-risk-0064`、`tfda-risk-0019`、`tfda-risk-0042` | 無 | PASS | 1.00 | 0.75 |
| Top-4 | 上述三篇 + `tfda-risk-0035` | 無 | PASS | 1.00 | 1.00 |
| Top-5 | 上述四篇 | `tfda-risk-0020` | PASS | 1.00 | 1.00 |

Broad Query 的人工 Direct 文件共有四篇：

- 酮酸中毒
- Fournier’s gangrene
- 下肢截肢
- 急性腎損傷

所以 Broad Top-3 雖然可以 `PASS`，但只保留 3/4 篇 Direct evidence，recall 是 `0.75`；Top-4 才完整保留四篇。

這個結果證明 usable context 不是文件本身固定的屬性，而是跟 Query 有關：

- Narrow Query 只保留酮酸中毒。
- Broad Query 則保留多個 SGLT2 安全主題。

## 三、Fallback ablation 測試

這組測試故意移除真正的酮酸中毒文件，只留下：

- `tfda-risk-0064`：Fournier’s gangrene
- `tfda-risk-0042`：下肢截肢
- `tfda-risk-0035`：急性腎損傷

這不是修改原始 Corpus，而是另外建立的 evaluation ablation，並標記：

```text
ablation_only = true
```

三次結果完全一致：

```json
{
  "usable_document_ids": [],
  "sufficient_for_answer": false,
  "decision": "FALLBACK"
}
```

這表示 Hybrid 沒有因為三篇文件都屬於 SGLT2，就硬湊出酮酸中毒答案。當正確 Evidence 被移除時，它可以拒絕把資料交給 Generator。

## 四、Standalone Judge 和 Hybrid 的成本比較

Phase 4 Standalone Judge 的基準是一輪 Narrow 實驗：10 次 Document-level + 3 次 Set-level，共 13 次 LLM call。

Hybrid 每一個 Variant 只需要 1 次 Set-level LLM call；Retrieval 和 Reranker 的時間也納入下表的 end-to-end latency。

| 方法 | LLM Calls | Judge 平均時間 | End-to-end 平均時間 | 平均 Total Tokens |
|---|---:|---:|---:|---:|
| Standalone Judge（Phase 4） | 13 | — | 93.11 秒 | 41,081 |
| Hybrid Narrow Top-3 | 1 | 9.07 秒 | 40.80 秒 | 4,867 |
| Hybrid Narrow Top-4 | 1 | 20.85 秒 | 52.57 秒 | 6,404 |
| Hybrid Narrow Top-5 | 1 | 13.90 秒 | 45.63 秒 | 7,556 |
| Hybrid Broad Top-3 | 1 | 12.08 秒 | 43.71 秒 | 4,940 |
| Hybrid Broad Top-4 | 1 | 14.96 秒 | 46.59 秒 | 6,399 |
| Hybrid Broad Top-5 | 1 | 16.53 秒 | 48.16 秒 | 7,327 |

### 成本怎麼解讀？

以 Narrow Query 為例：

- LLM calls：13 次降到 1 次，減少約 92.3%。
- Top-3 total tokens：41,081 降到約 4,867，減少約 88.2%。
- Top-4 total tokens：減少約 84.4%。
- Top-5 total tokens：減少約 81.6%。

而且這裡不是只比較 Judge latency；Hybrid 的 end-to-end 時間已包含 Similarity Retrieval 和 CPU Cross-Encoder Reranker。

Phase 5 全部測試工作量如果一起算，包括 Narrow Top-3/4/5、Broad Top-3/4/5 和 fallback ablation，每輪是 7 次 LLM call，平均完整執行時間約 163.22 秒。但這和 Phase 4 的 13 次 Narrow-only workload 不是完全同一個工作量，所以不能直接說 163 秒比 93 秒慢或快；真正適合比較的是同一個 Narrow Variant 的 end-to-end latency。

## 五、哪個 Top-N 最合理？

這次實測後，答案不是所有 Query 都固定同一個數字：

- Narrow Query：Top-3 已經 PASS，precision 和 recall 都是 1.0，而且平均 end-to-end 約 40.80 秒，是三個 Narrow Variant 中最低。
- Broad Query：Top-3 雖然 PASS，但 recall 只有 0.75；Top-4 才保留全部四篇 Direct evidence。
- Top-5：沒有增加 recall，只是多放一篇候選，然後由 Judge 排除。

因此如果允許 Query-dependent Top-N：

> Narrow 用 Top-3，Broad 用 Top-4。

如果 MVP 必須設定一個固定 Top-N：

> 建議使用 Top-4。

理由是 Top-4 能保留 Broad Query 的完整四篇 SGLT2 Direct evidence；對 Narrow Query 也不會造成錯誤使用，因為 Set-level Judge 會排除其他不同安全主題。Top-5 則增加 token 和 Context，沒有帶來 recall 改善。

## 六、Hybrid 的角色要拆清楚

Hybrid 不是「把很多模型堆在一起，所以效果最好」，而是把不同工作分開：

### Contract Gate

處理程式可以確定的格式問題：文件 ID、row index、日期、成分、正文是否存在。

### Retriever

從 129 筆文件快速找出 Top-20 候選資料。

### Reranker

把 Query-specific relevance 比較高的文件排到前面，但仍可能把同一類藥品的不同安全主題排得很前面。

### 一次 Set-level LLM Judge

只針對縮小後的 Top-N Context，判斷哪些文件真的能使用，以及整批資料最後要 `PASS`、`REVIEW` 或 `FALLBACK`。

這樣做的重點是：LLM 不再逐篇審查完整 Top-10，而是只在前面便宜方法縮小範圍後，做一次整體決策。

## 七、流程 Trace 範例

Narrow Top-4 的實際流程可以簡化成：

```text
Query：SGLT2 + 酮酸中毒
        ↓
Contract Gate：129 → 129
        ↓
Similarity Retrieval：129 → Top-20
        ↓
Reranker：20 → Top-4
        ↓
Set-level Judge：4 → usable 1
        ↓
PASS
```

Judge 最後保留：

```text
usable：tfda-risk-0019
excluded：tfda-risk-0064、tfda-risk-0042、tfda-risk-0035
```

另外，Trace 也記錄每個 stage 的 input_count、output_count、kept_ids、rejected／excluded IDs 和 latency，因此可以看出文件是在 Contract、Retrieval、Reranker 還是 Judge 階段被篩掉。

## 八、穩定性與意外結果

三輪結果中：

- Narrow Top-3／Top-4／Top-5 都是 `PASS`。
- Broad Top-3／Top-4／Top-5 都是 `PASS`。
- Fallback ablation 三次都是 `FALLBACK`。
- 沒有出現 `REVIEW`，因為本次沒有真正的互相矛盾內容。
- 沒有把不同安全主題誤判成 Conflict。
- Narrow 三個 Variant 都只保留酮酸中毒。
- Broad Judge 能根據 Query 保留多個安全主題，而不是永遠只留酮酸中毒。

這次沒有發現 Hybrid 的意外失敗。唯一需要保留的限制是：這仍然只有一個 Narrow Query、一個 Broad Query，以及人工定義的小型 reference label，不是正式 benchmark。

## 九、Phase 5 結論

這次實驗支持 Hybrid Context Gate 作為 MVP 建議，原因不是它在所有指標都最好，而是它把 Phase 4 的判斷能力用更少的 LLM 呼叫完成：

- Narrow Top-3 已經能達到 `PASS`、precision 1.0、recall 1.0。
- Broad Top-4 能保留完整的四篇 SGLT2 直接相關安全資訊。
- 只有錯安全主題時，能正確 `FALLBACK`。
- 同一個 Judge 可以依照 Narrow 或 Broad Query 改變 usable documents。
- 相較逐篇 Judge，LLM call 和 token 都大幅減少。

目前最合理的 MVP 策略是：

```text
固定 Top-N：Top-4
或依 Query 類型：Narrow Top-3、Broad Top-4
```

下一階段若要繼續，應先確認是否要把這個 Hybrid 流程整理成正式 Workflow；本階段不開始 Agent 或 LangGraph。

## 十、產出檔案

```text
tfda_context_gate/
├── 05_hybrid.py
├── prompts/
│   └── hybrid_set_judge_v1.txt
├── results/
│   ├── hybrid_narrow_top3.json
│   ├── hybrid_narrow_top4.json
│   ├── hybrid_narrow_top5.json
│   ├── hybrid_broad_top3.json
│   ├── hybrid_broad_top4.json
│   ├── hybrid_broad_top5.json
│   ├── hybrid_fallback_ablation.json
│   ├── phase5_cost_latency.json
│   ├── phase5_trace.json
│   └── phase5_hybrid_output.txt
└── reports/
    └── phase5_hybrid_report.md
```

本階段到此停止，沒有開始 Agent、LangGraph 或最終完整報告。
