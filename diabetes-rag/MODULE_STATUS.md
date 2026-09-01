# Module status

哪些可委派的模組目前是跑 Erich 寫的預設實作，哪些已經有交付的替代版本
合併進 `main`。merge request 合併之後要更新 Status 欄——這裡的內容不會
自動推導出來。

任務說明：`../02_MS2_demo/delegation/`
GitLab：http://140.125.81.71:8080/M11423016/diabetes-rag

最後更新：2026-08-29 · Baseline eval（top_n=5）：

```
track    Recall@5   Precision     F1
graph      0.95        0.36      0.51
vector     0.40        0.08      0.13
all        0.68        0.22      0.32
```

> Precision 在這裡有一個結構性的天花板：大多數查詢都只有一個正確答案，
> 但 `top_n = 5`，所以即使結果完美，分數也只有 0.20。這裡請把
> **Recall@5** 當成主要指標來看。縮小 `top_n` 只會在算式上把 Precision
> 拉高，不代表檢索真的變好——詳見
> `../02_MS2_demo/delegation/06_boundary_c_eval.md`。

| 模組 | 檔案 | 負責人（MS1 角色） | 狀態 | 用預設值要付出的代價 |
|---|---|---|---|---|
| Query routing | `src/rag_retrieval/routing.py` | Multi-RAG A | **預設值** | 永遠是 HYBRID。`intent_tags` 被忽略，所以 LLM 組傳來的意圖訊號完全沒被用上——而且每一個單純的藥品仿單查詢也會跑 graph 這條軌道，接著被下面那個 fusion 缺陷拖累 recall。這是 vector Recall@5 只有 0.40 的原因之一。 |
| RRF fusion | `src/rag_retrieval/fusion.py` | Multi-RAG B | **預設值** | **已實測的缺陷，是 vector Recall@5 只有 0.40 的主因。** 在 `RRF_K=60`、`W_GRAPH=2.0`、每軌道 10 個候選的情況下，graph 軌道裡最差的候選（2.0/70 = 0.0286）分數仍高於 vector 軌道裡最好的候選（1.0/61 = 0.0164）——所以不論排名如何，每一筆 graph 命中都會贏過每一筆 vector 命中，RRF 的排名訊號完全失效。vector 的結果只能靠 `truncate_balanced` 「每軌道至少保留一個名額」的機制才能露出。`k=60` 是 TREC 規模常用的預設值，在只有 10 個候選的清單裡會把排名差異拉平。 |
| Neo4j backend | `src/rag_retrieval/retrievers/neo4j_backend.py` | Preprocessing A | **未交付** | 檔案不存在。graph 檢索目前是在記憶體裡跑，以 29 筆三元組的規模來說這是對的做法。代價是展示層面的：我們沒辦法示範「同一套介面、兩種 backend」，而這正是 8/26 學長要求的模組化最直接的證據。 |
| Risk table justification | `docs/risk_table_justification.md` | Boundary A | **未交付** | `risk.py` 裡 relation → risk 的對照表運作正確，但每一列都沒有標註出處。被問到的時候，我們能說出風險等級是什麼，卻說不出為什麼。`RISK_FACTOR_FOR` 的 HIGH vs. MEDIUM 判斷規則仍未解決，目前預設為 MEDIUM。 |
| Threshold calibration | `src/rag_retrieval/gate_out.py` 裡的常數 | Boundary B | **預設值** | `DEFAULT_GRAPH_CONFIDENCE_THRESHOLD=0.5` 從來沒有真正被觸發過——全部 29 筆三元組的信心值都落在 0.9–0.95。`DEFAULT_VECTOR_SIMILARITY_THRESHOLD=0.70` 在 2026-08-29 掃過 0.60/0.50/0.40 三個值：不論哪個值，vector Recall@5 都持平在 0.40，因為查詢失敗的候選分數全部落在 0.75–0.90，離門檻值還很遠。**已排除是低 recall 的原因。** 這個機制本身是存在的，只是在目前的資料下不起作用。 |
| Failure-mode eval | `eval/queries.json` | Boundary C | **預設值** | 20 筆查詢，全部是正面案例，而且全部是照著要測試的資料寫出來的。完全沒有負面或邊界案例，所以上面那些數字只描述了最佳情況下的表現。這正是 8/26 那次回饋直接點出來的落差。 |

## 9/3 當天該怎麼解讀這份文件

這張表是狀態報告，不是究責清單。模組化架構本來就會讓每個元件的狀態
可以被清楚看見——這種可見性正是這個設計的目的所在，而且它是雙向的：
同樣也會照出哪些部分是 Erich 自己寫的，而且還停留在預設值上。

展示的時候，就引用這些數字（有多少模組還在跑預設值，以及用實測結果
量化那代價是什麼）。姓名與角色留在這份檔案裡就好，不要帶出去講。
