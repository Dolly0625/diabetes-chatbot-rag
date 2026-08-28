# 04｜Tool 系統與 Selective Initialization

MedRAX2 的核心價值不在「工具很多」，而在於把彼此完全不同的能力壓成同一個 Agent-facing contract。

## 1. 一個標準工具的五個部分

以 [`TorchXRayVisionClassifierTool`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/tools/classification/torchxrayvision.py#L20-L50) 為例：

```python
class TorchXRayVisionInput(BaseModel):
    image_path: str = Field(
        ..., description="Path to the radiology image file..."
    )

class TorchXRayVisionClassifierTool(BaseTool):
    name = "torchxrayvision_classifier"
    description = "A tool that analyzes chest X-ray images..."
    args_schema = TorchXRayVisionInput
```

五個部分是：

| 部分 | 消費者 | 功能 |
| --- | --- | --- |
| `name` | LLM、ToolNode、trace/UI | 唯一工具識別 |
| `description` | LLM | 何時使用、輸入輸出語意 |
| `args_schema` | LLM schema + runtime | 引導並驗證參數 |
| `_run()` | runtime | 真正執行能力 |
| output/metadata | LLM、UI、trace | 結果與執行資訊 |

`description` 不是普通註解，而是 implicit prompt。描述寫得太廣，模型容易濫用；寫得太窄，模型可能不會選它。

## 2. Tool wrapper 隔離前處理與推論

分類工具的 `_run()`：

```python
def _run(self, image_path: str, run_manager=None):
    try:
        img = self._process_image(image_path)
        with torch.inference_mode():
            preds = self.model(img).cpu()[0]

        output = dict(zip(xrv.datasets.default_pathologies, preds.numpy()))
        metadata = {
            "image_path": image_path,
            "analysis_status": "completed",
        }
        return output, metadata
    except Exception as e:
        return {"error": str(e)}, {
            "image_path": image_path,
            "analysis_status": "failed",
        }
```

這個 wrapper 同時負責：

1. 檔案載入與灰階轉換；
2. 醫療影像 normalization；
3. tensor/device 處理；
4. inference mode；
5. 將 tensor 結果轉成可序列化 dictionary；
6. 形成 metadata/error envelope。

Agent loop 不需要知道 Torch、PIL 或 pathology label。

## 3. Tool output 並未完全標準化

多數工具傾向回傳：

```python
(output_dict, metadata_dict)
```

但這是專案慣例，不是獨立的統一 Pydantic result schema。不同工具的 `output_dict` 欄位不同，失敗時多半也是回傳 `{"error": ...}`，而非 raise exception。

這會造成一個重要語意差異：

```text
runtime 層：工具呼叫可能被視為成功完成
payload 層：analysis_status 其實是 failed
```

因此 orchestrator LLM 必須讀懂 payload 才知道工具失敗。若要用在受監管系統，應把 transport success、execution success、domain validity 分開建模。

## 4. 大型生成模型也只是 tool

[`CheXagentXRayVQATool`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/tools/vqa/xray_vqa.py#L31-L51) 的輸入：

```python
class XRayVQAToolInput(BaseModel):
    image_paths: List[str]
    prompt: str
    max_new_tokens: int = 512

class CheXagentXRayVQATool(BaseTool):
    name = "chexagent_xray_vqa"
    args_schema = XRayVQAToolInput
    return_direct = True
```

這表示 MedRAX2 可能形成 LLM-in-LLM 結構：

```text
orchestrator LLM
→ CheXagent tool（另一個生成模型）
→ free-text VQA result
→ orchestrator LLM synthesis
```

需要注意 `return_direct=True` 是 LangChain tool 行為設定，但 MedRAX2 使用自訂 LangGraph `ToolNode → agent` edge；不能只看到該旗標就推斷一定繞過後續 agent node，應以實際 graph/event trace 驗證。

## 5. Utility tool 可以建立下一個工具的輸入

DICOM tool 的用途不是回答，而是 artifact transformation：

```text
.dcm path
→ DicomProcessorTool
→ windowing / scaling
→ generated PNG path + DICOM metadata
→ 下一輪影像工具使用 PNG path
```

這展示 sequential tool dependency。若模型一次平行呼叫 DICOM converter 和 classifier，classifier 尚未取得 converter 產生的 path，流程就不成立。

## 6. Selective initialization 的兩層名稱

`main.py` registry 的 key 是 deployment/configuration name：

```python
all_tools = {
    "TorchXRayVisionClassifierTool": lambda: ...,
    "CheXagentXRayVQATool": lambda: ...,
    "MedicalRAGTool": lambda: RAGTool(config=rag_config),
}
```

tool instance 自己又有 Agent-facing `name`：

```text
registry key: TorchXRayVisionClassifierTool
tool.name:    torchxrayvision_classifier
```

前者給 CLI/operator 使用，後者給 LLM tool call 使用。這兩個 namespace 應被分清楚。

## 7. 為什麼只載入選定工具

```python
for tool_name in tools_to_use or []:
    if tool_name == "PythonSandboxTool":
        tools_dict["PythonSandboxTool"] = create_python_sandbox()
    if tool_name in all_tools:
        tools_dict[tool_name] = all_tools[tool_name]()
```

Selective initialization 解決：

- GPU/VRAM 不足；
- 模型權重很大；
- 某些權重需另外申請；
- 某些工具依賴外部 API；
- 不同 deployment 只允許不同能力；
- 減少模型要選擇的 tool 數量。

最後 `tools_dict.values()` 同時交給 `Agent` 的 `ToolNode` 與 `bind_tools()`，所以「沒載入」也代表「模型看不到」。

## 8. 固定快照中的 naming drift

預設清單使用：

```python
"XRayVQATool"
```

registry key 卻是：

```python
"CheXagentXRayVQATool"
```

未知 key 在該 loop 中會被安靜略過。因此印出 `Selected tools` 不代表所有工具都成功進入 `tools_dict`。正確驗證方式是：

```python
print(tools_dict.keys())
print([tool.name for tool in tools_dict.values()])
```

若要改善，初始化應回報 requested、loaded、skipped、failed 四種狀態，並可選擇 strict mode 在未知工具時直接失敗。

## 9. Web platform 的 tool lifecycle 更接近產品需求

`web_platform/backend/app/services/tool_manager.py` 進一步處理：

- `AVAILABLE / LOADING / LOADED / ERROR / UNAVAILABLE`；
- background loading；
- per-tool cancellation event；
- load semaphore；
- device selection；
- unload/cleanup；
- shutdown 時等待與回收。

這再次證明：Agent-facing tool contract 很小，但 resource lifecycle 可以非常複雜。

## 對 TFDA 的直接啟示

建議未來 tool contract 至少包含：

```text
name / version
description
typed input
typed result envelope
provenance
execution status
domain validity status
latency / cost
cache key
policy classification
```

每個 retrieval tool 的結果只能是 candidate evidence，仍必須重新經過 B。

