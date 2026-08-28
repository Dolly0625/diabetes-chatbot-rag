# 07｜Prompt、控制權與安全邊界

MedRAX2 的 system prompt 很完整，但「模型被要求遵守」和「系統保證不會違反」是兩回事。

## 1. Prompt 實際要求什麼

[`system_prompts.txt`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/docs/system_prompts.txt#L1-L17) 的一般醫療助理 prompt 包含：

```text
先用自己的視覺與推理
→ 用工具補充
→ 可平行或連續呼叫
→ 批判工具輸出
→ RAG/web claims 要有 citation
```

MCQ v2 prompt 更進一步規定：

- 影像閱讀順序；
- 工具使用偏好；
- 衝突時要解釋；
- 不提供 treatment plan；
- 表達 uncertainty；
- 最後輸出單一 `\boxed{X}`。

這些 prompt 是 behavior policy，能顯著改善模型表現，但不是 enforcement boundary。

## 2. Prompt rule 與 code rule 對照

| 規則 | Prompt 要求 | 核心 code 強制 |
| --- | --- | --- |
| 只在有幫助時叫工具 | 有 | 無 |
| 批判工具輸出 | 有 | 無獨立 verifier |
| citation 對應真實來源 | 有 | 無 claim-source validator |
| 不提供 treatment plan | 有 | 無 output policy node |
| 一定選一個 MCQ option | 有 | 無 schema parser |
| 有 tool call 就執行 | — | `ToolNode` 強制 |
| 無 tool call 就結束 | — | conditional edge 強制 |
| message update append | — | reducer 強制 |

閱讀 Agent 時，最重要的是把這兩欄分開。

## 3. 誰擁有什麼控制權

| 決策 | 主要擁有者 |
| --- | --- |
| 可用工具集合 | application initialization |
| 是否呼叫工具 | LLM |
| tool name/args | LLM，受 schema 約束 |
| 工具真正執行 | ToolNode/runtime |
| 是否再叫工具 | LLM |
| 是否停止 | LLM 透過「不產生 tool call」表達 |
| final medical text | LLM |
| evidence 是否可信 | prompt 要求 LLM自行判斷 |
| final output 是否可交付 | 核心 graph 未設獨立 gate |

這種控制模型適合研究型通用 Agent，卻不等於適合所有醫療資訊流程。

## 4. 直接回答路徑會繞過所有工具

第一輪 model response 若沒有 tool call：

```text
agent → END
```

因此即使 prompt 說應使用 RAG 或專門工具，graph 仍允許模型直接回答。若某種 request 必須經過權威 evidence，應由 graph/policy 強制必經，而不是只寫在 description 或 prompt。

## 5. 工具失敗的語意可能被藏在 payload

許多 tool 使用：

```python
except Exception as e:
    return {"error": str(e)}, {"analysis_status": "failed"}
```

若 ToolNode 沒收到 exception，它可能只會建立一般 ToolMessage。接下來由 LLM 判斷 `analysis_status`。這種設計有彈性，但要避免把「tool transport completed」誤記為「medical analysis succeeded」。

建議至少區分：

```text
EXECUTION_OK / EXECUTION_ERROR
RESULT_VALID / RESULT_INVALID / RESULT_PARTIAL
EVIDENCE_APPROVED / EVIDENCE_REJECTED
```

## 6. 衝突解決仍是 prompt heuristic

MCQ prompt 要模型：

- 說明工具衝突；
- 偏好專門工具；
- 多工具交叉驗證；
- 在特定段落甚至偏好「most popular answer」。

這不是 calibrated ensemble，也不是正式仲裁器。多數決可能讓多個相關、同源或具有相同 bias 的工具放大錯誤。

較可靠的 conflict handling 需要：

- tool independence metadata；
- task-specific validation set；
- calibration；
- evidence provenance；
- deterministic escalation rule；
- 必要時人工 review。

## 7. Custom system prompt 是高權限介面

FastAPI request 可提供 custom system prompt，且暫時覆蓋 Agent prompt。這代表 client 可能改變 Agent persona、tool policy 與輸出格式。

企業系統通常應改為：

```text
client 提供 prompt_profile_id
→ server 驗證 caller 權限
→ server 選擇已審查模板
```

而不是接受任意 system prompt text。

## 8. 風險與控制位置

| 風險 | 不足的做法 | 建議強制層 |
| --- | --- | --- |
| 未授權工具 | prompt 說不要用 | tool allowlist + preflight policy |
| 無限循環 | 模型自行停止 | max steps/time/cost |
| 重複高成本工具 | history 可見 | deterministic cache/dedup |
| 錯誤 evidence | LLM 批判 | independent evidence gate |
| 不安全回答 | disclaimer | output verifier/policy gate |
| 病患資料跨 thread | thread ID | authenticated tenant binding |
| prompt 互相污染 | finally restore | immutable per-run config |

## 9. 安全不是否定 Agent 彈性

可以保留 LLM 的 tool selection，但把它放在 capability sandbox：

```text
Policy 決定可用工具集合
→ LLM 在集合內選工具
→ runtime 驗證 args/budget
→ tool result 正規化
→ domain gate 驗證
→ LLM synthesis
→ output gate
```

這就是「動態決策」與「程式化邊界」並存。

