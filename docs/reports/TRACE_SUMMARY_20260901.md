# Demo 實際 Trace 與耗時結果

**執行日期：** 2026-09-01
**執行環境：** 本機 Python 3.10、正式 workflow、已設定的 Gemini RAG 與正式生成模型。
**資料原則：** 全部是合成 Demo 輸入，沒有使用真實病患、LINE 使用者、病歷或分享碼。

## 這份報告在展示什麼

以下三筆是實際呼叫 `run_workflow(..., use_formal=True)` 後，從 E observability trace 擷取的結果：使用者輸入、實際路徑、每關耗時與最後狀態。

所有時間是同一輪的 wall-clock 量測，會受當時網路與模型負載影響；它們是 Demo 實測值，不是 SLA。

## 總覽

| 案例 | 使用者輸入 | 最終狀態 | 總耗時 | 重點 |
| --- | --- | --- | ---: | --- |
| `EDU-001` | 糖尿病飲食可以怎麼吃？ | `COMPLETED` | **7,093 ms** | 完整走 A → RAG → B → C → D，安全產生衛教回答。 |
| `EMERGENCY-001` | 我胸口很痛，喘不過氣。 | `FALLBACK` | **43 ms** | A 判為紅旗，立即急診／119 安全中止；未呼叫 RAG 或生成模型。 |
| `PREVISIT-001` | 我要準備看診。 | `NEEDS_CLARIFICATION` | **34 ms** | 引導進看診前資料流程，快速提出第一個需要補充的資料。 |

## 案例一：一般衛教的完整安全流程

**輸入：** `糖尿病飲食可以怎麼吃？`
**最後結果：** `COMPLETED`，無 fallback。

```text
使用者問題
  → A / input_router               COMPLETED    0.1 ms
  → QUERY_EXPANSION                COMPLETED    0.0 ms
  → RAG / retrieval                COMPLETED  747.8 ms
  → B / context_gate               COMPLETED    0.3 ms
  → C / generator                  COMPLETED 5533.1 ms
  → D / output_gate                COMPLETED   18.9 ms
  → SYSTEM / request               COMPLETED 7093.0 ms
```

白話解釋：系統先確認這是一題可以回答的衛教問題，再由 RAG 找官方資料；B 確認證據可用後，C 才把資料寫成患者看得懂的回答，最後 D 再檢查輸出邊界。

**這筆 trace 的結論：** 主要延遲來自 C 生成（約 5.53 秒），不是 B 或 D；RAG 檢索約 0.75 秒。

## 案例二：紅旗症狀優先攔截

**輸入：** `我胸口很痛，喘不過氣。`
**最後結果：** `FALLBACK`
**安全原因：** `A_EMERGENCY`／`RED_FLAG_DETERMINISTIC_ABORT`。

```text
使用者問題
  → SYSTEM / narrow_path_gate      COMPLETED    0.0 ms
  → A / input_router               BLOCKED      0.1 ms
  → FALLBACK / termination         FALLBACK     0.0 ms
  → SYSTEM / request               COMPLETED   43.2 ms
```

白話解釋：這類訊息不用等 AI「想答案」。A 一辨識到胸痛與呼吸困難，就停止 RAG、停止模型生成，改成 119／急診引導。因此這筆沒有 RAG、B、C、D 的耗時，總共約 43 ms。

## 案例三：看診前資料整理的引導

**輸入：** `我要準備看診。`
**最後結果：** `NEEDS_CLARIFICATION`，代表系統安全地等待使用者回答下一題，不是錯誤。

```text
使用者意圖
  → SYSTEM / narrow_path_gate      COMPLETED    0.0 ms
  → A / input_router               COMPLETED    0.1 ms
  → INTAKE_CHECK / stage_router    COMPLETED    0.0 ms
  → INTAKE_STAGE1 / extraction     NEEDS_CLARIFICATION 0.0 ms
  → SYSTEM / request               NEEDS_CLARIFICATION 34.4 ms
```

白話解釋：這不是一題衛教，因此不需要查 RAG 或等模型生成。系統直接切到看診前資料蒐集，等待使用者回答第一題。

目前對外 Demo 的實際 UX 是：LINE 收到「開始看診前整理」時，會先送病患到 `/demo/previsit` 專用網頁；本案例展示的是該網頁後端使用的 intake engine 如何判斷與推進，而不是要把八題問卷重新塞回 LINE。

## 怎麼自己重跑

在已設定 `.env` 的本機環境執行：

```bash
cd diabetes-chatbot-rag
set -a; . .env; set +a
python3 - <<'PY'
from time import perf_counter
from tfda_context_gate.workflow.runner import run_workflow

text = "糖尿病飲食可以怎麼吃？"
started = perf_counter()
result = run_workflow({
    "request_id": "demo-trace-001",
    "user_raw_input": text,
    "declared_role": "PATIENT",
    "language": "zh-TW",
}, use_formal=True)

print(result.status)
print(round((perf_counter() - started) * 1000, 1), "ms")
for event in result.trace["events"]:
    if event["status"] != "STARTED":
        print(event["component"], event["node_name"], event["status"], event["latency_ms"])
PY
```

## 驗證

同日執行：

```bash
python -m pytest -q \
  tfda_context_gate/tests/test_e_observability.py \
  tfda_context_gate/tests/test_workflow_integration.py
```

結果：**23 passed**。

## 隱私說明

原始 JSONL trace 可能含原始輸入與 request metadata，仍只保留在受控本機 archive，不公開到 GitHub。本報告只公開合成案例與去識別化的關卡／耗時結果。
