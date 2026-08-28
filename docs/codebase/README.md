# tfda_context_gate 程式碼導讀 — 索引

> 便宜模型 `muse-spark-1.2-contributor` 產出，逐檔對照原始碼撰寫。正式契約以 `CURRENT_ARCHITECTURE.md` / `ARCHITECTURE_AUDIT.md` 為準，本目錄為開發者 10 分鐘定位地圖。

## 文件清單

| 序 | 文件 | 一句話 | 閱讀時間 |
|---|---|---|---|
| 00 | [00_overview.md](./00_overview.md) | 一頁式地圖：Mermaid 流程圖 + 目錄職責表 + 閱讀順序 + 安全邊界 | 10 min |
| 01 | [01_a_router.md](./01_a_router.md) | A 輸入路由：7 步管線 + `labels.py` 8 枚舉 + 雙 Guard + `policy_gate` 10 級優先序 | 15 min |
| 02 | [02_rag_b.md](./02_rag_b.md) | RAG/B 檢索：`TFDADrugSafetyRetriever`(129筆) + `FixtureRetriever` + `CanonicalBResult` + Adapter 映射 | 12 min |
| 03 | [03_c_generator.md](./03_c_generator.md) | C 生成器：v1 vs v2 差異 + `CWorkflowInput` + 雙 Generator + 證據引用 6 規則 | 12 min |
| 04 | [04_d_output_gate.md](./04_d_output_gate.md) | D 輸出閘：8 步驗證 + `OutputGateResult` + 紅線/棄權/Heuristic Verifier | 12 min |
| 05 | [05_e_observability.md](./05_e_observability.md) | E 觀測層：`TraceRecorder` span 生命週期 + 雙 Sink + 脫敏 + trajectory 渲染 | 10 min |
| 06 | [06_workflow_agent.md](./06_workflow_agent.md) | 編排：LangGraph 9 節點 + 3 條件邊 + `run_workflow` 注入 + 有界 Agent (ASK/REWRITE/FALLBACK) | 15 min |

## 建議閱讀路徑

- **新人首次**：00 → 01 → 02 → 06 → 03/04/05
- **只想跑起來**：00 的「如何跑」 + 06 的最小範例
- **改 A 策略**：01 的 `policy_gate` 優先序表 + `guard.py`
- **改檢索**：02 的 `TFDADrugSafetyRetriever` 懶加載 + `tfda_smoke_cases.py`
- **改生成**：03 的 v2 10 條規則 + `workflow_adapter.py`
- **改驗證**：04 的 8 步流水線 + `policy.py` 紅線
- **加觀測**：05 的 `tracer.py` API + `privacy.py` 脫敏

## 與正式文件的關係

```
CURRENT_ARCHITECTURE.md  ← Source of Truth（契約）
ARCHITECTURE_AUDIT.md    ← 整合前後證據
README.md                ← 專案總覽
*.md       ← 本目錄：程式碼導讀（本索引）
```

## 維護約定

- 改動 `a_router/` `b_context_gate/` `rag/` `c_generator/` `d_output_gate/` `e_observability/` `workflow/` `agent/` 任一模組後，請同步更新對應 `0*.md`
- 文件中的行號/枚舉值定期用 `grep -n` 核對，避免與 `labels.py` / `gate.py` 脫節
- 本目錄全部使用 `muse-spark-1.2-contributor` 低成本產出，保持中文、表格、最小可跑範例三件套風格
