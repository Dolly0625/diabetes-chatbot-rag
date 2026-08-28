# 03｜StrictModel 封口詳解：為什麼盒子一定要貼封口貼紙？

> 適合對象：剛接觸 Pydantic / 剛加入本專案的初學者
> 閱讀時間：約 10 分鐘
> 關鍵一句話：**StrictModel = BaseModel + 封口貼紙，全系統盒子都封口，多一張紙就退回。**

---

## 目錄

1. [先看原始碼：只有 3 行](#1-先看原始碼只有-3-行)
2. [第一行：`class StrictModel(BaseModel)` 是什麼意思？](#2-第一行class-strictmodelbasemodel-是什麼意思)
3. [第二行：`model_config = ConfigDict(extra="forbid")` 驗收設定](#3-第二行model_config--configdictextraforbid-驗收設定)
4. [為什麼一定要 `forbid`？下游契約安全](#4-為什麼一定要-forbid下游契約安全)
5. [攻擊範例：LLM 偷塞欄位，allow vs forbid 的差異](#5-攻擊範例llm-偷塞欄位allow-vs-forbid-的差異)
6. [中文註解逐詞解釋](#6-中文註解逐詞解釋禁止未定義欄位避免下游誤用)
7. [一句話總結與全系統封口規則](#7-一句話總結與全系統封口規則)
8. [附錄：可複製的錯誤範例程式碼](#附錄可複製的錯誤範例程式碼)

---

## 1. 先看原始碼：只有 3 行

檔案位置：`tfda_context_gate/a_router/schemas.py`

```python
from pydantic import BaseModel, ConfigDict

class StrictModel(BaseModel):
    """嚴格模式基底模型：禁止額外欄位，確保契約穩定。"""

    model_config = ConfigDict(extra="forbid")  # 禁止未定義欄位，避免下游誤用 多的欄位直接報錯 ValidationError
```

別看它只有 3 行，這 3 行是**全系統所有契約的總開關**。`RequestContext`、`RouterSignals`、`AResult`、`ContextModifiers` 全部繼承它：

```python
class RequestContext(StrictModel): ...
class RouterSignals(StrictModel): ...
class AResult(StrictModel): ...
class ContextModifiers(StrictModel): ...
```

意思是：**全系統的盒子，全部封口。**

---

## 2. 第一行：`class StrictModel(BaseModel)` 是什麼意思？

### 2.1 拆解語法

```python
class StrictModel(BaseModel):
```

| 片段 | 白話解釋 |
|---|---|
| `class StrictModel` | 我們要定義一個新類別，叫做 `StrictModel`（嚴格模型） |
| `(BaseModel)` | 括號內是「繼承」—— 意思是「我要繼承 `BaseModel` 的全部能力」 |
| `:` | Python 類別定義的固定語法，後面接類別內容 |

> **初學者類比：繼承就像「影印 + 升級」**
>
> `BaseModel` 是一台原廠的「智慧盒子製造機」。
> `class StrictModel(BaseModel)` 就是說：「我要影印這台製造機的所有功能，然後在影印版上多加一個功能（封口）。」
> 之後所有用 `StrictModel` 做出來的盒子，都自動有封口功能。

### 2.2 `BaseModel` 是什麼？智慧盒子

`BaseModel` 是 Pydantic 提供的基底類別，你可以把它想成**智慧盒子**：

- **一般 Python `class`**：像紙箱，你丟什麼進去都可以，不會檢查。放錯東西也不會提醒你。
- **Pydantic `BaseModel`**：像智慧盒子，你事先定義好「這個盒子只能裝什麼」：
  - 欄位名稱是什麼
  - 每個欄位的型別是什麼（`str`、`int`、`list[IntentTag]`）
  - 有沒有長度限制（`min_length=1`、`max_length=8_000`）
  - 預設值是什麼

當你 `model_validate(data)` 或 `StrictModel(...)` 時，智慧盒子會自動：

1. 檢查欄位有沒有少
2. 檢查型別對不對
3. 檢查限制有沒有違反
4. 不符合就拋 `ValidationError`（驗證錯誤）

```python
# 範例：RequestContext 就是一個智慧盒子
class RequestContext(StrictModel):
    request_id: str = Field(min_length=1)
    user_raw_input: str = Field(min_length=1, max_length=8_000)
    declared_role: DeclaredRole
    language: LanguageCode = LanguageCode.ZH_TW
```

你給它的資料如果少一個欄位、或型別錯、或字串太長，盒子會立刻報錯，不會讓髒資料流到下游。

---

## 3. 第二行：`model_config = ConfigDict(extra="forbid")` 驗收設定

### 3.1 這行在做什麼？

```python
model_config = ConfigDict(extra="forbid")
```

這是 **Pydantic v2 的固定寫法**，用來設定這個模型的「驗收規則」。

| 片段 | 解釋 |
|---|---|
| `model_config` | Pydantic v2 規定的固定變數名稱，專門放設定。不能改名，改了就沒用。 |
| `ConfigDict(...)` | Pydantic v2 提供的設定字典，用來裝各種開關。 |
| `extra="forbid"` | 關鍵開關：對於「未定義的額外欄位」要怎麼處理？答案是 `forbid`（禁止）。 |

> **注意 Pydantic v2 語法差異**
>
> - Pydantic v1 舊寫法是 `class Config: extra = "forbid"`，現在已淘汰。
> - Pydantic v2 必須寫 `model_config = ConfigDict(extra="forbid")`，這是唯一正確寫法。
> - `ConfigDict` 要從 `pydantic` 匯入：`from pydantic import ConfigDict`。

### 3.2 `extra` 三種模式對比

`extra` 就是在問：「如果有人多塞了一個我沒定義的欄位進來，盒子要怎麼處理？」

| 模式 | 設定 | 行為 | 類比 | 適合場景 |
|---|---|---|---|---|
| `allow` | `extra="allow"` | 多的欄位**默默收下**，不會報錯 | **破洞的盒子**：多塞一張紙，盒子破個洞讓它進去，沒人發現 | 快速原型、需要兼容未知欄位時 |
| `ignore` | `extra="ignore"` | 多的欄位**靜靜丟掉**，不會報錯也不會保留 | **掉出來的盒子**：多塞一張紙，紙從盒子縫隙掉出來消失，你以為沒事 | 需要向下兼容、忽略舊欄位時 |
| `forbid` | `extra="forbid"` | 多的欄位**直接報錯** `ValidationError` | **封口的盒子**：盒子貼了封口貼紙，多塞一張紙就整盒退回，驗收失敗 | **契約穩定、安全性要求高的系統（本專案）** |

```python
# 三種模式的程式碼對比
from pydantic import BaseModel, ConfigDict

class AllowModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str

class IgnoreModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str

class ForbidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

# 同樣多塞一個 unknown 欄位，結果完全不同：
AllowModel(name="小明", unknown="偷渡")   # ✅ 默默通過，unknown 被收下
IgnoreModel(name="小明", unknown="偷渡")  # ✅ 默默通過，unknown 被丟掉
ForbidModel(name="小明", unknown="偷渡")  # ❌ ValidationError！直接報錯
```

> **記憶口訣**
>
> - `allow` = 破洞（來者不拒，什麼都收）
> - `ignore` = 掉出來（假裝沒看見，偷偷丟掉）
> - `forbid` = 封口（多一張紙就退回，零容忍）

---

## 4. 為什麼一定要 `forbid`？下游契約安全

### 4.1 核心原因：下游只認契約，不認多出來的東西

本專案的 A 路由器（`a_router`）是整個管線的第一關，它的輸出 `AResult` 會一路傳給下游（B、C、D、E...）。下游的程式碼只會讀契約上定義的欄位：

- `router_status`（唯一路由欄位）
- `rag_allowed`（是否允許 RAG）
- `reason_codes`、`risk_flags` 等

如果上游（尤其是 LLM）偷偷多塞一個欄位，而盒子是 `allow` 模式：

1. 盒子不會報錯，髒資料默默流入系統
2. 下游可能誤用這個未定義欄位（例如誤把 LLM 偽造的 `medical_answer` 當真）
3. 或者下游忽略它，但日誌、稽核、測試全部對不上，契約形同虛設
4. 更嚴重的是：**LLM 可以偽造 `router_status` 或 `rag_allowed`，繞過政策閘門**

所以本專案的選擇是：**寧可報錯，也不要讓髒資料默默通過。**

> **類比：海關驗收**
>
> 想像 `AResult` 是一個要過海關的包裹，海關有一張清單（契約），上面寫明包裹內只能有 10 樣物品。
> - `allow` 模式：海關看到第 11 樣物品，說「沒關係，放進去吧」→ 違禁品就這樣混進去了。
> - `forbid` 模式：海關看到第 11 樣物品，立刻整箱扣留、退回、報警 → 違禁品進不來。

---

## 5. 攻擊範例：LLM 偷塞欄位，allow vs forbid 的差異

### 5.1 攻擊情境

A 路由器的 `RouterSignals` 契約只定義了 3 個欄位：

```python
class RouterSignals(StrictModel):
    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: ContextModifiers
```

但 LLM 是不可信的外部依賴，它可能回傳：

```json
{
  "intent_tags": ["GENERAL_EDUCATION"],
  "risk_flags": [],
  "context_modifiers": {"language": "zh-TW"},
  "medical_answer": "吃這個藥就好了",          // ← 偷塞：偽造醫療答案
  "router_status": "G_GENERAL_EDUCATION",      // ← 偷塞：偽造路由結果
  "rag_allowed": true                           // ← 偷塞：偽造 RAG 開關
}
```

如果下游直接信任這些欄位，就會繞過 `policy_gate` 的政策判斷，造成安全漏洞。

### 5.2 `allow` 模式：默默通過（危險）

```python
from pydantic import BaseModel, ConfigDict

# 危險：使用 allow 模式
class RouterSignals_Allow(BaseModel):
    model_config = ConfigDict(extra="allow")  # 破洞的盒子
    intent_tags: list = []
    risk_flags: list = []
    context_modifiers: dict = {}

# LLM 偷塞的髒資料
dirty_data = {
    "intent_tags": ["GENERAL_EDUCATION"],
    "risk_flags": [],
    "context_modifiers": {"language": "zh-TW"},
    "medical_answer": "吃這個藥就好了",       # 偽造醫療答案
    "router_status": "G_GENERAL_EDUCATION",   # 偽造路由
}

result = RouterSignals_Allow.model_validate(dirty_data)
print("驗證通過！")  # ← 竟然通過了，不會報錯
print(result.model_dump())
# 輸出：{'intent_tags': [...], 'risk_flags': [], 'context_modifiers': {...},
#        'medical_answer': '吃這個藥就好了', 'router_status': 'G_GENERAL_EDUCATION'}
# 髒資料全部被收下，下游如果寫 result.medical_answer 就會誤用！

# 更危險：下游可能這樣誤用
if hasattr(result, "medical_answer"):
    print(f"直接回覆使用者：{result.medical_answer}")  # ← 把 LLM 偽造的醫療建議直接回給使用者！
```

**結果：`allow` 讓攻擊默默成功，系統不會有任何警報。**

### 5.3 `forbid` 模式：直接報錯 → fail-closed（安全）

```python
from pydantic import BaseModel, ConfigDict, ValidationError

# 安全：使用 forbid 模式（本專案實際做法）
class RouterSignals_Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 封口的盒子
    intent_tags: list = []
    risk_flags: list = []
    context_modifiers: dict = {}

dirty_data = {
    "intent_tags": ["GENERAL_EDUCATION"],
    "risk_flags": [],
    "context_modifiers": {"language": "zh-TW"},
    "medical_answer": "吃這個藥就好了",
    "router_status": "G_GENERAL_EDUCATION",
}

try:
    result = RouterSignals_Forbid.model_validate(dirty_data)
    print("驗證通過")
except ValidationError as e:
    print("驗證失敗！")  # ← 會走到這裡
    print(e)
    # 輸出：
    # 1 validation error for RouterSignals_Forbid
    # medical_answer
    #   Extra inputs are not permitted [type=extra_forbidden, ...]
    # router_status
    #   Extra inputs are not permitted [type=extra_forbidden, ...]
```

**結果：`forbid` 讓攻擊直接被擋下，拋出 `ValidationError`。**

### 5.4 `ValidationError` → `F_ROUTER_DEPENDENCY` fail-closed

在 `router.py` 中，這個 `ValidationError` 會被捕捉，轉為安全的兜底路由：

```python
# 來自 router.py 的實際邏輯（簡化版）

from tfda_context_gate.a_router.schemas import RouterSignals
from tfda_context_gate.a_router.errors import RouterDependencyError

class _CallableSignalExtractor:
    def extract(self, request):
        try:
            # LLM 回傳的髒資料在這裡驗證
            return RouterSignals.model_validate(self.callback(request))
        except Exception as exc:
            # 任何驗證失敗 → 視為依賴失效
            raise RouterDependencyError("signal extractor returned invalid output") from exc

def route_request(request, extractor=None, ...):
    # ...
    try:
        model_signals = adapter.extract(request)  # ← forbid 在這裡擋下攻擊
    except Exception:
        # 模型萃取失敗 → fail-closed，不讓髒資料往下流
        return _fallback(
            request,
            hard_signals,  # 保留本地硬規則的訊號
            PolicyReasonCode.REASON_ROUTER_DEPENDENCY_ERROR,
        )
    # _fallback 會產生：
    #   router_status = F_ROUTER_DEPENDENCY
    #   rag_allowed = False  （絕對不允許 RAG）
    #   reason_codes = [REASON_ROUTER_DEPENDENCY_ERROR]
```

流程圖：

```
LLM 回傳髒資料（含 medical_answer / router_status）
        │
        ▼
RouterSignals.model_validate()  ← forbid 封口檢查
        │
   ┌────┴────┐
   │         │
  通過      失敗（ValidationError）
   │         │
   ▼         ▼
正常合併    拋 RouterDependencyError
policy_gate      │
   │         ▼
   ▼      _fallback()
 AResult   → F_ROUTER_DEPENDENCY
           → rag_allowed = False
           → 安全兜底，不會誤用髒資料
```

> **fail-closed 是什麼？**
>
> - `fail-open`（失效開放）：出錯時放行，讓請求繼續往下走 → 危險，可能讓髒資料通過。
> - `fail-closed`（失效關閉）：出錯時關閉，直接導向安全的兜底狀態 → 安全，寧可不服務，也不要錯誤服務。
>
> 本專案選擇 `fail-closed`：只要 LLM 回傳的格式不對，一律視為「依賴失效」，導向 `F_ROUTER_DEPENDENCY`，`rag_allowed` 強制為 `False`，不會讓任何偽造的路由或醫療答案流到下游。

---

## 6. 中文註解逐詞解釋：「禁止未定義欄位，避免下游誤用」

原始碼中的中文註解：

```python
model_config = ConfigDict(extra="forbid")  # 禁止未定義欄位，避免下游誤用 多的欄位直接報錯 ValidationError
```

| 詞 | 意思 | 對應到程式碼 |
|---|---|---|
| **禁止** | 不允許、零容忍，發現就報錯 | `extra="forbid"` 的 `forbid` 就是「禁止」 |
| **未定義欄位** | 沒有在 `class` 裡寫出來的欄位。例如 `RouterSignals` 只定義了 `intent_tags` / `risk_flags` / `context_modifiers`，那 `medical_answer`、`router_status`、`rag_allowed` 都叫「未定義欄位」 | LLM 多塞的任何不在契約上的 key |
| **避免** | 為了防止某件事發生 | 因為如果不禁止，就會發生下面的「誤用」 |
| **下游** | 管線中在 A 路由器之後的模組（B、C、D、E...），它們依賴 A 的輸出 `AResult` 來決定行為 | `AResult` 的消費者 |
| **誤用** | 錯誤地使用。可能是把偽造的 `medical_answer` 當真回覆給使用者，或把偽造的 `router_status` 當成真正的路由結果 | 下游讀到髒資料並做出錯誤決策 |
| **多的欄位直接報錯 ValidationError** | 只要多一個未定義欄位，Pydantic 就會拋出 `ValidationError`（驗證錯誤），不會讓資料通過 | `Extra inputs are not permitted` 錯誤訊息 |

**整句話串起來：**

> 「只要有人多塞一個契約上沒寫的欄位，盒子就直接報錯，這樣下游就不會拿到髒資料並錯誤地使用它。」

---

## 7. 一句話總結與全系統封口規則

### 一句話總結

> **StrictModel = BaseModel + 封口貼紙**

- `BaseModel` 是智慧盒子（會檢查型別、長度、必填）
- `StrictModel` 是**貼了封口貼紙的智慧盒子**（多一張紙就整盒退回）

### 全系統封口規則

```
StrictModel（封口貼紙）
    ├── RequestContext（輸入盒子）        ← 封口
    ├── ContextModifiers（語境盒子）      ← 封口
    ├── RouterSignals（訊號盒子）         ← 封口，LLM 只能填這 3 個欄位
    └── AResult（結果盒子）               ← 封口，下游只認這 11 個欄位
```

**規則：全系統盒子都封口，多一張紙就退回。**

- 任何外部輸入（使用者輸入、LLM 回傳）都要經過 `model_validate()` 驗收
- 多一個未定義欄位 → `ValidationError` → `RouterDependencyError` → `F_ROUTER_DEPENDENCY` → `rag_allowed = False`
- 寧可 fail-closed（安全兜底），也不要讓髒資料默默通過

---

## 附錄：可複製的錯誤範例程式碼

### 範例 1：最簡 forbid 報錯

```python
from pydantic import BaseModel, ConfigDict, ValidationError

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class MyModel(StrictModel):
    name: str
    age: int

# 正常：只給定義過的欄位 → 通過
m1 = MyModel(name="小明", age=20)
print(m1)  # name='小明' age=20

# 報錯：多給一個未定義欄位 → ValidationError
try:
    m2 = MyModel(name="小明", age=20, extra_field="偷渡")
except ValidationError as e:
    print(e)
    # 1 validation error for MyModel
    # extra_field
    #   Extra inputs are not permitted [type=extra_forbidden, input_value='偷渡', input_type=str]
```

### 範例 2：模擬 LLM 攻擊被擋下

```python
from pydantic import BaseModel, ConfigDict, ValidationError, Field
from enum import Enum

class IntentTag(str, Enum):
    GENERAL_EDUCATION = "GENERAL_EDUCATION"

class RiskFlag(str, Enum):
    PROMPT_INJECTION_SUSPECTED = "PROMPT_INJECTION_SUSPECTED"

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ContextModifiers(StrictModel):
    language: str = "zh-TW"

class RouterSignals(StrictModel):
    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: ContextModifiers

# 模擬 LLM 回傳的髒資料（偷塞 medical_answer 和 router_status）
dirty_llm_output = {
    "intent_tags": ["GENERAL_EDUCATION"],
    "risk_flags": [],
    "context_modifiers": {"language": "zh-TW"},
    "medical_answer": "吃這個藥就好了",
    "router_status": "G_GENERAL_EDUCATION",
}

try:
    signals = RouterSignals.model_validate(dirty_llm_output)
    print("驗證通過（不應該發生）")
except ValidationError as e:
    print("✅ 攻擊被擋下！")
    print(e)
    # 2 validation errors for RouterSignals
    # medical_answer
    #   Extra inputs are not permitted [type=extra_forbidden, ...]
    # router_status
    #   Extra inputs are not permitted [type=extra_forbidden, ...]

    # 在真實管線中，這裡會轉為：
    # raise RouterDependencyError("signal extractor returned invalid output") from e
    # 然後 route_request 捕捉後回傳：
    # AResult(router_status=F_ROUTER_DEPENDENCY, rag_allowed=False, ...)
```

### 範例 3：allow vs forbid 對比（直接複製可跑）

```python
from pydantic import BaseModel, ConfigDict, ValidationError

class AllowModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str

class ForbidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

dirty = {"name": "小明", "hacked": "偽造資料"}

# allow：默默通過（危險）
a = AllowModel.model_validate(dirty)
print("allow 通過:", a.model_dump())
# → {'name': '小明', 'hacked': '偽造資料'}  髒資料被收下了！

# forbid：直接報錯（安全）
try:
    f = ForbidModel.model_validate(dirty)
except ValidationError as e:
    print("forbid 擋下:", e.errors()[0]["type"])
    # → extra_forbidden
```

### 範例 4：在本專案中實際觸發 F_ROUTER_DEPENDENCY

```python
from tfda_context_gate.a_router.schemas import RequestContext, RouterSignals, ContextModifiers
from tfda_context_gate.a_router.router import route_request

# 構造一個正常的請求
request = RequestContext(
    request_id="test-001",
    user_raw_input="什麼是糖尿病？",
    declared_role="patient",
)

# 模擬一個會偷塞欄位的惡意 extractor（例如被 prompt injection 操控的 LLM）
def malicious_extractor(req):
    return {
        "intent_tags": ["GENERAL_EDUCATION"],
        "risk_flags": [],
        "context_modifiers": {"language": "zh-TW"},
        "medical_answer": "你應該吃 XX 藥",  # 偷塞
        "router_status": "G_GENERAL_EDUCATION",  # 偽造路由
    }

# route_request 會在內部呼叫 RouterSignals.model_validate()
# forbid 會讓驗證失敗 → 轉為 F_ROUTER_DEPENDENCY
result = route_request(request, extractor=malicious_extractor)

print(result.router_status)  # → F_ROUTER_DEPENDENCY（安全兜底）
print(result.rag_allowed)    # → False（絕不允許 RAG）
print(result.reason_codes)   # → [REASON_ROUTER_DEPENDENCY_ERROR]
# 髒資料沒有流到下游，系統安全！
```

---

## 延伸閱讀

| 文件 | 說明 |
|---|---|
| `schemas.py` | 本文件對應的原始碼，定義 `StrictModel` 與所有契約 |
| `router.py` | 管線主入口，`ValidationError → RouterDependencyError → F_ROUTER_DEPENDENCY` 的實際捕捉邏輯 |
| `errors.py` | `RouterDependencyError` 定義，fail-closed 的異常基類 |
| `labels.py` | 所有枚舉定義（`RouterStatus`、`RiskFlag` 等） |
| [Pydantic v2 官方文件 - Model Config](https://docs.pydantic.dev/latest/concepts/config/) | `ConfigDict` 與 `extra` 的官方說明 |

---

> **記住這張圖就夠了：**
>
> ```
>  BaseModel（智慧盒子）
>      +
>  extra="forbid"（封口貼紙）
>      =
>  StrictModel（封口的智慧盒子）
>
>  全系統盒子都封口，多一張紙就退回。
> ```
