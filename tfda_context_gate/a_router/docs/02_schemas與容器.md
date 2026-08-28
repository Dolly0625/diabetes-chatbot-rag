# 02｜`schemas.py` 與容器

> 本篇沉澱 `schemas.py` 的設計講解：什麼是「容器」、為何這樣命名、四個模型的欄位契約，以及全檔最關鍵的 `rag_allowed` 硬邊界。

---

## 一、容器是什麼

### 生活比喻：信封與盒子

想像你要寄一份文件：

- **信封**：外觀固定、只能裝規定的東西（信紙、申請表），多塞一張不明紙條就會被退件。
- **盒子**：有隔層、有形狀，什麼放哪一格是事先定義好的，收件人打開就知道怎麼取用。

`schemas.py` 裡的每一個模型，就是這樣的**信封／盒子**——只負責「裝」與「定形」，不負責「算」與「判斷」。

### 對應的工程概念：資料傳輸物件／綱要

- **資料傳輸物件**：專門用來在不同層、不同函式之間搬運資料的物件，本身不含商業邏輯。
- **綱要**：定義資料長什麼樣子、有哪些欄位、型別是什麼、哪些必填、哪些有預設值。

`schemas.py` 同時具備兩者：用 `Pydantic BaseModel` 定義綱要，實例化後就是在管線中傳遞的資料傳輸物件。

### 為何叫「容器」不叫「類別」

| 稱呼 | 隱含意義 | 是否符合本檔 |
|------|----------|--------------|
| 類別 | 可能包含方法、計算、判斷、狀態變更 | ❌ 不符合 |
| 容器 | 只裝不算、有形狀檢查、在管線間傳遞 | ✅ 完全符合 |

**三個理由：**

1.  **只裝不算**：容器內沒有 `if`、`for`、運算邏輯，只有欄位定義。判斷交給 `rules.py` 與 `policy.py`。
2.  **有形狀檢查**：透過 `StrictModel` 與 `Field` 約束，裝進去的東西不合形狀就直接報錯（`ValidationError`），而不是默默接受。
3.  **在管線間傳遞**：容器的生命週期就是「被填入 → 被傳遞 → 被讀取」，從第 1 步的 `RequestContext` 一路傳到第 7 步的 `AResult`，是管線的資料載體。

> 一句話：**容器定義形狀，邏輯決定內容。**

---

## 二、底座：`StrictModel`

全檔所有模型都繼承自 `StrictModel`，它是唯一的底座。

```python
class StrictModel(BaseModel):
    """嚴格模式基底模型：禁止額外欄位，確保契約穩定。"""
    model_config = ConfigDict(extra="forbid")  # 禁止未定義欄位，避免下游誤用
```

| 項目 | 說明 |
|------|------|
| 繼承來源 | `Pydantic BaseModel` |
| 核心設定 | `extra="forbid"` |
| 效果 | 任何未在模型中定義的欄位，傳入時直接拋出 `ValidationError`，不會被靜默忽略 |
| 目的 | 確保契約穩定：上游不能多塞、下游不能誤用，避免欄位漂移導致的隱性錯誤 |

> 只要看到 `StrictModel`，就知道這是一個「形狀被鎖死」的容器。

---

## 三、四個模型逐個詳解

管線總覽（7 步）：`RequestContext` → 正規化 → 注入防禦 → 規則／大型語言模型訊號萃取 → 訊號合併 → 政策閘門 → `AResult`

### 3.1 `RequestContext`：第 1 步輸入容器（5 欄）

> 定位：整條管線的起點，承載使用者最原始的輸入與身分宣告。

```python
class RequestContext(StrictModel):
    request_id: str = Field(min_length=1)
    schema_version: str = Field(default="a.v0.1", min_length=1)
    user_raw_input: str = Field(min_length=1, max_length=8_000)
    declared_role: DeclaredRole
    language: LanguageCode = LanguageCode.ZH_TW
```

| 欄位 | 型別 | 約束／預設 | 說明 |
|------|------|------------|------|
| `request_id` | `str` | `min_length=1`，必填 | 請求唯一識別，用於追蹤與冪等 |
| `schema_version` | `str` | 預設 `a.v0.1`，`min_length=1` | 契約版本號，供日後相容判斷 |
| `user_raw_input` | `str` | `1~8000` 字元，必填 | 使用者原始輸入，未經任何清洗 |
| `declared_role` | `DeclaredRole` | 必填，無預設 | 宣告身分（病患／照護者／醫事人員），僅作參考，不作授權依據 |
| `language` | `LanguageCode` | 預設 `ZH_TW` | 輸入語系，預設繁中 |

### 3.2 `ContextModifiers`：語境修飾子（4 欄）

> 定位：不決定路由，只修飾語境，供下游細化回應語氣與對象。

```python
class ContextModifiers(StrictModel):
    time_frame: TimeFrame = TimeFrame.CURRENT
    target_subject: TargetSubject = TargetSubject.SELF
    polarity: Polarity = Polarity.AFFIRMATIVE
    language: LanguageCode = LanguageCode.ZH_TW
```

| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| `time_frame` | `TimeFrame` | `CURRENT` | 時間框架：當前／過去／假設 |
| `target_subject` | `TargetSubject` | `SELF` | 目標對象：本人／家人照護者／第三方 |
| `polarity` | `Polarity` | `AFFIRMATIVE` | 語氣極性：肯定／否定（會影響風險判讀） |
| `language` | `LanguageCode` | `ZH_TW` | 語系標記，預設繁中 |

> 四欄皆有預設值，代表「沒有明確語境時，就當作當前、本人、肯定、繁中」。

### 3.3 `RouterSignals`：第 4–5 步觀測容器（3 欄，強調沒有 `router_status`）

> 定位：第一層（Layer 1）的觀測結果，只描述「看到了什麼」，不決定「要去哪」。

```python
class RouterSignals(StrictModel):
    """Layer 1 輸出：僅為觀測訊號，不含最終路由決策（管線第 4-5 步產物）。"""

    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: ContextModifiers
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| `intent_tags` | `list[IntentTag]` | 意圖標籤（衛教／症狀／診斷請求等），由規則或大型語言模型萃取 |
| `risk_flags` | `list[RiskFlag]` | 風險旗標（急症／用藥個人化／注入攻擊等），觸發政策閘門 |
| `context_modifiers` | `ContextModifiers` | 語境修飾子，補充時間／對象／語氣資訊 |

> **關鍵強調：`RouterSignals` 裡沒有 `router_status`。**
>
> 這是刻意設計——第一層只能「觀測」，不能「判決」。最終要去哪（`router_status`）必須由第 6 步的 `policy_gate` 決定，避免觀測層越權。

### 3.4 `AResult`：第 7 步唯一契約（11 欄，文件註記為 7+3 結構）

> 定位：整條管線的唯一出口，下游只認這一個容器。`router_status` 是唯一的路由依據。

```python
class AResult(StrictModel):
    """A 路由器最終輸出：穩定傳遞給下游的唯一契約（管線第 7 步）。"""

    request_id: str
    schema_version: str
    user_raw_input: str
    declared_role: DeclaredRole
    language: LanguageCode
    intent_tags: list[IntentTag]
    risk_flags: list[RiskFlag]
    context_modifiers: ContextModifiers
    router_status: RouterStatus          # 唯一路由結果
    reason_codes: list[PolicyReasonCode] # 路由原因碼
    # [工程新增] Explicit downstream guard so callers do not re-implement policy.
    rag_allowed: bool                    # 下游檢索開關
```

| 分組 | 欄位 | 來源 | 說明 |
|------|------|------|------|
| **回填透傳（8 欄）** | `request_id` | `RequestContext` | 回填原始請求識別 |
|  | `schema_version` | `RequestContext` | 回填契約版本 |
|  | `user_raw_input` | `RequestContext` | 回填原始輸入，供稽核 |
|  | `declared_role` | `RequestContext` | 回填宣告身分 |
|  | `language` | `RequestContext` | 回填語系 |
|  | `intent_tags` | `RouterSignals` | 最終意圖標籤集合 |
|  | `risk_flags` | `RouterSignals` | 最終風險旗標集合 |
|  | `context_modifiers` | `RouterSignals` | 最終語境修飾子 |
| **決策輸出（3 欄）** | `router_status` | `policy_gate` 判定 | 唯一路由結果（8 種狀態含 `F_ROUTER_DEPENDENCY`），下游僅依此欄位分流 |
|  | `reason_codes` | `policy_gate` 判定 | 路由原因碼，可多個，供解釋與日誌 |
|  | `rag_allowed` | 硬編碼衍生（見下一節） | 下游檢索開關，僅 `G_GENERAL_EDUCATION` 為 `True` |

> 雖然原始碼共 11 欄，但設計上可理解為 **8 欄透傳＋3 欄決策**，文件簡記為「7+3」——重點是後 3 欄才是本步新增的判決，其餘皆為透傳。

工廠方法 `from_request_and_decision` 是組裝 `AResult` 的唯一入口：

```python
@classmethod
def from_request_and_decision(cls, request, signals, router_status, reason_codes) -> "AResult":
    return cls(
        request_id=request.request_id,
        schema_version=request.schema_version,
        user_raw_input=request.user_raw_input,
        declared_role=request.declared_role,
        language=request.language,
        intent_tags=signals.intent_tags,
        risk_flags=signals.risk_flags,
        context_modifiers=signals.context_modifiers,
        router_status=router_status,
        reason_codes=reason_codes,
        rag_allowed=router_status is RouterStatus.G_GENERAL_EDUCATION,
    )
```

---

## 四、全檔最重要的一行：`rag_allowed` 硬邊界

### 硬編碼

```python
rag_allowed=router_status is RouterStatus.G_GENERAL_EDUCATION,  # 嚴格邊界：只有一般衛教可檢索
```

這一行位於 `AResult.from_request_and_decision` 的最後一個參數，是全檔最關鍵的邊界。

| 項目 | 內容 |
|------|------|
| 位置 | `schemas.py:103`，`AResult` 工廠方法內 |
| 邏輯 | `router_status is G_GENERAL_EDUCATION` |
| 結果 | 僅當路由狀態為「一般衛教」時為 `True`，其餘 7 種狀態一律 `False` |
| 性質 | 硬編碼、不可繞過、不可由呼叫方重算 |

### `[工程新增]` 註解的含義

```python
# [工程新增] Explicit downstream guard so callers do not re-implement policy.
rag_allowed: bool  # 下游檢索開關：僅 G_GENERAL_EDUCATION 為 True，其餘皆 False（不可自行重算）
```

- **工程新增**：代表此欄位是工程團隊為了「防呆」而主動加入的，不是業務邏輯自然長出來的。
- **下游防護**：明確告訴下游「不要自己再寫一次判斷」，直接讀 `rag_allowed` 即可，避免各處重複實作政策、導致不一致。
- **不可自行重算**：註解與程式碼共同強調——`rag_allowed` 的真值由 `schemas.py` 唯一決定，下游禁止用 `if router_status == ...` 自行推導。

### 唯一性的強調

> **全系統只有這一行能決定 `rag_allowed`，沒有第二處。**

- 不在 `policy.py` 重算
- 不在 `router.py` 重算
- 不在下游服務重算
- 呼叫方只能讀取，不能覆寫（`StrictModel` 禁止額外欄位，工廠方法不接受外部傳入 `rag_allowed`）

這就是「硬邊界」——用程式碼把政策寫死，而不是用文件約束。

---

## 五、一句話串起四檔關係

> **`labels.py` 定義詞彙 → `schemas.py` 用詞彙組成容器形狀 → `rules.py` 填入觀測訊號 → `policy.py` 依訊號判決路由並透過 `AResult` 封裝出口。**

| 檔案 | 角色 | 一句話 |
|------|------|--------|
| `labels.py` | 詞彙表 | 定義所有枚舉（身分、意圖、風險、路由狀態等），是全系統的共同語言 |
| `schemas.py` | 容器 | 用詞彙組成四個容器的形狀，約束資料長什麼樣子 |
| `rules.py` | 觀測 | 依規則填入 `RouterSignals`，只觀測不判決 |
| `policy.py` | 判決 | 依 `RouterSignals` 決定 `router_status` 與 `reason_codes`，並透過 `AResult` 輸出 |

---

## 六、對比：什麼是容器、什麼不是容器

### 這是容器（只定義形狀）✅

```python
# schemas.py — 容器：只有欄位與型別，沒有判斷
class RequestContext(StrictModel):
    request_id: str = Field(min_length=1)
    user_raw_input: str = Field(min_length=1, max_length=8_000)
    declared_role: DeclaredRole
    language: LanguageCode = LanguageCode.ZH_TW
```

```python
# schemas.py — 容器：只有形狀，沒有 if
class RouterSignals(StrictModel):
    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: ContextModifiers
```

> 特徵：只有 `Field`、`型別`、`預設值`，沒有任何 `if`／`for`／運算。

### 這不是容器（有 `if` 判斷）❌

```python
# policy.py — 這不是容器，這是邏輯：有 if 判斷
def policy_gate(signals: RouterSignals) -> tuple[RouterStatus, list[PolicyReasonCode]]:
    if RiskFlag.EMERGENCY in signals.risk_flags:
        return RouterStatus.F_EMERGENCY, [PolicyReasonCode.EMERGENCY_DETECTED]
    if IntentTag.DIAGNOSIS_REQUEST in signals.intent_tags:
        return RouterStatus.F_DIAGNOSIS_REQUEST, [PolicyReasonCode.DIAGNOSIS_REQUESTED]
    # ... 更多判斷
```

```python
# 錯誤示範：在容器內寫判斷 — 這就不是容器了
class BadResult(StrictModel):
    router_status: RouterStatus
    rag_allowed: bool

    def check_rag(self):  # ❌ 容器不該有判斷邏輯
        if self.router_status == RouterStatus.G_GENERAL_EDUCATION:
            self.rag_allowed = True
        else:
            self.rag_allowed = False
```

> 對比：`schemas.py` 的 `AResult` 把 `rag_allowed` 寫成**工廠方法內的硬編碼一行**，而不是讓容器自己去 `if` 判斷——容器依然只負責「裝」，連這一行的 `is` 判斷都是在「組裝時」由外部填入，而非容器內部的行為。

---

## 小結

| 問題 | 答案 |
|------|------|
| 容器是什麼 | 信封／盒子，只裝不算的資料載體 |
| 底座是什麼 | `StrictModel`，`extra="forbid"` 鎖死形狀 |
| 有幾個容器 | 4 個：`RequestContext`（5 欄）、`ContextModifiers`（4 欄）、`RouterSignals`（3 欄，無 `router_status`）、`AResult`（11 欄，7+3 結構） |
| 最重要的一行 | `rag_allowed=router_status is RouterStatus.G_GENERAL_EDUCATION`，全系統唯一決定點，`[工程新增]` 防下游重算 |
| 四檔關係 | 詞彙 → 形狀 → 觀測 → 判決 |

