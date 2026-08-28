# Agent 設計自學入口

這套文件用來理解本專案的 Agent 設計。前七章聚焦本專案的 runtime 與控制邊界；第八章以 MedRAX2 作為外部參考，整理兩種 Agent 架構的差異與可借鏡方向。它不涵蓋完整 TFDA RAG 實驗。

## 先記住一句話

本專案的 Agent 不是能自由使用工具、任意規劃的通用 Agent，而是 **B Context Gate 判定資料不足後，才會啟動的 bounded recovery planner（受限恢復規劃器）**。

LLM 只能選擇：

- `ASK_USER`
- `REWRITE_QUERY`
- `FALLBACK`

LangGraph 才擁有實際執行權、路由權與重試上限。

## 建議閱讀順序

| 順序 | 文件 | 讀完應該能回答 |
| --- | --- | --- |
| 1 | [01_架構心智模型.md](01_架構心智模型.md) | Agent 為什麼存在？位於哪裡？ |
| 2 | [02_執行流程與狀態.md](02_執行流程與狀態.md) | 一次 request 怎麼穿過 graph？ |
| 3 | [03_Planner與Rewriter.md](03_Planner與Rewriter.md) | LLM 看見什麼、能輸出什麼？ |
| 4 | [04_三個真實Trace.md](04_三個真實Trace.md) | ASK、REWRITE、FALLBACK 實際怎麼發生？ |
| 5 | [05_安全邊界與失敗處理.md](05_安全邊界與失敗處理.md) | 模型亂回、超限、注入時怎麼辦？ |
| 6 | [06_真實元件與Fixture.md](06_真實元件與Fixture.md) | 哪些結果來自真實 LLM／資料，哪些是測試替身？ |
| 7 | [07_測試地圖與練習.md](07_測試地圖與練習.md) | 如何用測試驗證理解並開始修改？ |
| 8 | [08_MedRAX2架構對照與討論筆記.md](08_MedRAX2架構對照與討論筆記.md) | MedRAX2 如何運作？和我們的控制權、安全邊界有何差異？ |
| 9 | [medrax2_deep_dive/README.md](medrax2_deep_dive/README.md) | 如何逐行理解 MedRAX2 graph、tools、RAG、memory、API、prompt 與 benchmark？ |

如果時間很少，先讀 01、03、04。若要參加 MedRAX2 架構討論，讀 08；若要實際學會 MedRAX2 的 Agent 設計，再依序閱讀第 9 項的深入導讀。

## 原始碼閱讀入口

- Graph 與所有路由：[workflow/graph.py](../../tfda_context_gate/workflow/graph.py)
- Workflow 外層入口：[workflow/runner.py](../../tfda_context_gate/workflow/runner.py)
- Agent 決策 schema：[agent/schemas.py](../../tfda_context_gate/agent/schemas.py)
- Planner prompt 與 LangChain adapter：[agent/planner.py](../../tfda_context_gate/agent/planner.py)
- Planner context 投影：[agent/context.py](../../tfda_context_gate/agent/context.py)
- Query Rewriter：[agent/rewriter.py](../../tfda_context_gate/agent/rewriter.py)
- Agent 限制：[agent/config.py](../../tfda_context_gate/agent/config.py)
- Agent tests：[tests/test_agent_runtime.py](../../tfda_context_gate/tests/test_agent_runtime.py)
- 三案例定義：[agent_demo_cases.json](../../tfda_context_gate/agent_demo_cases.json)
- 最終 Cloud trace：[CLOUD_LLM_final_three_cases_trace.jsonl](../../report_handoff/traces/CLOUD_LLM_final_three_cases_trace.jsonl)

## 閱讀時的四個問題

看到任何 Agent 程式碼時，固定問：

1. 這份資料是誰寫入的：使用者、B、LLM，還是系統？
2. 這個值只是觀察／建議，還是真的能控制 graph？
3. 這個限制由 prompt 提醒，還是由程式碼強制？
4. 失敗時會繼續執行、重試，還是 fail closed？

這四題能幫你快速辨認專案真正的控制權。
