# TFDA MedRAX2-style Agent 實驗

這是一個**與現有專案隔離的架構實驗**。它把 MedRAX2 最值得學習的動態工具迴圈、選擇性工具註冊、狀態保存與工具結果正規化，套用到「TFDA 糖尿病藥品安全資訊」主題；同時保留原專案較重要的強制閘門概念。

它不是醫療產品，也不是已完成臨床驗證的系統。目前只讀取既有語料，不會改寫或匯入 `tfda_context_gate` 的程式碼。

## 你可以從這個實驗學到什麼

1. 如何把固定流程與動態 agent loop 組合，而不是二選一。
2. LLM 如何只負責「下一步要呼叫什麼工具」與「候選草稿」，而不能跳過輸入、證據、輸出閘門。
3. 如何把每一個工具輸出統一成 `ToolResult`，讓錯誤、延遲、快取與證據都能被追蹤。
4. 如何限制步數、總工具呼叫數、單一工具呼叫數與總處理時間。
5. 如何平行執行同一輪互不依賴的查詢工具。
6. 如何用 `thread_id` 與 LangGraph checkpointer 保存對話狀態，又以 `run_id` 分離每次執行的 trace。
7. 如何先用離線 deterministic model 驗證 orchestration，再換成真正的 tool-calling LLM。

## 架構概覽

```text
START
  │
  ▼
[A: input_gate] ── blocked ───────────────► END
  │ allowed
  ▼
[agent] ── tool calls ──► [tools]
  ▲                         │
  └─────────────────────────┘
  │ draft
  ▼
[B: evidence_gate] ── insufficient ──────► END
  │ approved evidence
  ▼
[D: output_gate] ── pass/block ──────────► END
```

`agent ↔ tools` 是 MedRAX2 式的動態內迴圈；A、B、D 是程式掌控的外層安全骨架。完整設計與 MedRAX2 對照見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目錄

```text
tfda_medrax2_experiment/
├── agent_lab/
│   ├── corpus.py       # 唯讀語料 adapter、驗證與 lexical retrieval
│   ├── demo.py         # CLI demo
│   ├── gates.py        # A/B/D 強制閘門
│   ├── graph.py        # LangGraph、執行限制、平行工具與快取
│   ├── models.py       # 模型協定、離線模型、LangChain adapter
│   ├── schemas.py      # state 邊界使用的 Pydantic schema
│   ├── tracing.py      # 結構化事件
│   └── tools/
│       ├── base.py     # 工具基類、registry、錯誤正規化
│       └── tfda.py     # 三個 TFDA 唯讀工具
├── tests/              # 閘門、工具、迴圈、快取、memory 與限制測試
├── ARCHITECTURE.md
├── VALIDATION_REPORT.md
├── pyproject.toml
└── requirements.txt
```

## 快速執行

請從 workspace 根目錄執行：

```bash
python3 -m pip install -r tfda_medrax2_experiment/requirements.txt
python3 -m tfda_medrax2_experiment.agent_lab.demo
```

指定問題：

```bash
python3 -m tfda_medrax2_experiment.agent_lab.demo \
  "SGLT2 抑制劑有哪些 TFDA 藥品安全風險溝通資訊？"
```

查看完整的 tool result、evidence、trace 與計數器：

```bash
python3 -m tfda_medrax2_experiment.agent_lab.demo --json
```

執行測試：

```bash
pytest -q tfda_medrax2_experiment/tests
# 或
python3 -m pytest -q tfda_medrax2_experiment/tests
```

## Demo 會真的做什麼

預設的 `RuleBasedTFDAModel` 不是假造固定答案，而是以 deterministic 規則扮演 tool-calling model：

1. 第一輪同時要求廣義風險搜尋與成分搜尋。
2. tools node 平行執行兩個唯讀工具。
3. 第二輪要求依 evidence ID 重新取得證據。
4. 第三輪依候選證據產生帶 citation 的草稿。
5. B gate 驗證證據來源與 schema。
6. D gate 驗證 citation、允許清單、範圍聲明與禁止的個人化指令。

這種設計讓你在沒有 API key、沒有模型輸出漂移的條件下，先測清楚 agent harness。它驗證的是「控制流」，不是在假裝規則模型具有 LLM 推理能力。

## 換成真正的 tool-calling LLM

實驗不綁定 provider，也不在 repo 內保存金鑰。呼叫端建立 LangChain chat model 後，再注入 adapter：

```python
from langchain_openai import ChatOpenAI

from tfda_medrax2_experiment.agent_lab.corpus import TFDACorpus
from tfda_medrax2_experiment.agent_lab.graph import TFDAToolAgent
from tfda_medrax2_experiment.agent_lab.models import LangChainToolCallingModel
from tfda_medrax2_experiment.agent_lab.tools import build_default_registry

corpus = TFDACorpus()
registry = build_default_registry(corpus)
llm = ChatOpenAI(model="your-tool-calling-model", temperature=0)
model = LangChainToolCallingModel(llm=llm, registry=registry)
agent = TFDAToolAgent(model=model, registry=registry)

result = agent.run(
    "SGLT2 抑制劑有哪些 TFDA 藥品安全風險溝通資訊？",
    thread_id="demo-user-thread",
)
print(result.final_response)
```

請另外安裝相符的 provider 套件。無論換成哪個模型，模型都只能看到 registry 中選定的 tool schema，且 A/B/D gate 仍由程式強制執行。

## 選擇性工具註冊

```python
from tfda_medrax2_experiment.agent_lab.graph import build_experimental_agent

agent = build_experimental_agent(
    selected_tools=[
        "search_tfda_risk_communications",
        "inspect_tfda_evidence_set",
    ]
)
```

指定不存在的名稱會在啟動時 `ValueError`，不會等到模型呼叫後才悄悄失敗。這對大型 agent 很重要：可用能力應該是顯式、可稽核的設定。

## 與現有專案的隔離邊界

- 新程式全部位於本資料夾。
- 只透過檔案路徑讀取 `tfda_context_gate/data/processed/langchain_documents.json`。
- 沒有 import `tfda_context_gate` 的 Python 模組。
- 沒有修改現有 router、gates、tests 或 production entrypoint。
- `MemorySaver`、快取與 trace 都只存在於目前 Python process 記憶體。

## 現階段限制

- 目前 retrieval 是為架構驗證而做的 lexical baseline，不是 production hybrid/vector retrieval。
- Latin drug-class anchor 修正了 `SGLT2` 因「抑制劑」共詞誤命中 JAK/CDK 的問題，但這仍不是完整的醫藥同義詞與實體解析。
- Evidence gate 只確認來源、唯一 ID、內容與分數；**它不是臨床正確性判斷器**。
- deadline 是節點間的 cooperative deadline，不能強制中斷已經卡住的底層 I/O。
- checkpoint 與 cache 是 in-memory，不能跨 process、不能水平擴展。
- 尚未加入 FastAPI、UI、身份驗證、持久化 trace、rate limit、外部監控與正式評測資料集。
- 規則式 input/output policy 只用於展示控制權，不能取代正式政策引擎與醫療審查。

請先把它視為可執行的「第二代架構草圖」。實測結果與已知警告見 [VALIDATION_REPORT.md](VALIDATION_REPORT.md)。
