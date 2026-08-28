# 09｜對照 TFDA 與實驗藍圖

目標不是把 TFDA 改造成胸腔 X 光 Agent，而是把 MedRAX2 的工程能力翻譯成「受控的糖尿病用藥資訊 Agent」。

## 1. 根本控制模型

```text
MedRAX2
User → LLM → optional tools → LLM → final

TFDA current
User → A → Retrieval → B → C → D → final
                         └ insufficient → bounded planner
```

MedRAX2 的 LLM 是中心；TFDA 的 policy/evidence/output boundaries 是中心。

## 2. 不應追求表面功能對稱

「匹配 MedRAX2 等級」不代表一定要有影像模型或十幾個工具。對 TFDA 主題，等級應定義為：

- 標準、可擴充的 tool contract；
- selective tool initialization；
- dynamic but bounded tool selection；
- thread-scoped memory；
- parallel independent tool execution；
- artifact/provenance preservation；
- structured trace；
- benchmark + deterministic tests；
- API/CLI boundary；
- safety gates 仍為 mandatory。

## 3. MedRAX2 feature 的領域轉譯

| MedRAX2 能力 | TFDA 糖尿病用藥版本 |
| --- | --- |
| CXR classifier | request/risk classifier，但由 A 強制 |
| DICOM processor | TFDA 文件/metadata normalizer |
| image VQA | structured evidence inspection tool |
| medical RAG | TFDA risk communication retrieval tool |
| web search | 預設不進 approved evidence，或只限核准來源 |
| parallel image tools | 平行查 risk communication / license metadata |
| conversation thread | 受控 clarification thread |
| final synthesis | C generator，仍經 D |
| tool cards | E trajectory events |

## 4. 推薦的混合 graph

```text
A mandatory policy gate
  ↓
Tool-aware bounded orchestrator
  ├─ search_tfda_risk_communications
  ├─ lookup_drug_license_metadata
  ├─ inspect_candidate_evidence
  └─ compare_evidence_set
  ↓
Candidate evidence normalization
  ↓
B mandatory evidence approval
  ↓
C evidence-aware generation
  ↓
D mandatory output verification
  ↓
E trace across all stages
```

與 unrestricted ReAct 的差別：

- Agent 只能使用 policy 允許的 read-only tools；
- 所有 tool output 都是 candidate；
- B 不能被 Agent 跳過；
- final answer 不能由 tool 直接回傳；
- graph 擁有 steps、calls、timeout、cache 與 termination；
- D 是唯一交付出口。

## 5. Tool result envelope

建議實驗統一使用：

```python
class ToolResult:
    call_id: str
    tool_name: str
    status: Literal["OK", "ERROR", "BLOCKED"]
    payload: dict
    candidate_evidence: list[Evidence]
    provenance: dict
    latency_ms: float
    cache_hit: bool
```

避免每個 tool 自己決定 error shape，也避免 LLM 把 failed payload 當正常醫療結果。

## 6. Tool policy

可以對 tools 加上 policy metadata：

```text
risk_level: READ_ONLY / EXTERNAL_NETWORK / WRITE / HIGH_IMPACT
allowed_intents
max_calls_per_run
requires_approval
produces_candidate_evidence
```

目前 TFDA 實驗只需要 read-only tools；不應加入會修改病歷、開立處方或發送外部訊息的工具。

## 7. Memory 設計

```text
ThreadMessageStore
└─ 使用者問題、追問、系統回覆

RunState
└─ 當次 original/current query、calls、candidate evidence、limits

ApprovedEvidence
└─ 只存在當次 B result，不能從舊對話自動繼承
```

這保留多輪 UX，又避免 conversation history 越過 evidence boundary。

## 8. 實驗完成標準

實驗不以「LLM 看起來很聰明」為完成，而以以下 contract 為準：

- 離線 scripted planner 可重現多步 tool trajectory；
- unknown/blocked tool fail closed；
- max steps/calls 被程式強制；
- tool result schema 一致；
- candidate evidence 一律經 B；
- final response 一律經 D；
- thread A/B 隔離；
- trace 不保存整段敏感 raw content；
- tests 不需 API key；
- demo 只讀既有 TFDA corpus，不修改現有系統。

## 9. 為什麼放在獨立資料夾

這是 architecture spike，不應先混入正式 A–E path。獨立資料夾可以：

- 明確標記 experimental；
- 不改現有 imports；
- 不改現有 requirements；
- 可重複比較兩種控制模型；
- 通過檢核後再提出 integration proposal。

