# Labels 詞彙表 — `labels.py` 完整講解

> 來源：`tfda_context_gate/a_router/labels.py`（9 枚舉、共 51 個值）  
> 定位：全域詞彙表（Labels）—— 所有路由與政策判斷的唯一真實來源（Single Source of Truth），`rules`、`policy`、`schemas` 皆引用此處定義。

---

## 0. 基底：`_CodeEnum(str, Enum)` 為何可直接字串比較

```python
from enum import Enum

class _CodeEnum(str, Enum):
    """字串枚舉基底：value 即字串本身，__str__ 回傳 value 方便日誌與序列化。"""
    def __str__(self) -> str:
        return self.value
```

| 特性 | 說明 |
|------|------|
| 繼承 `str` | 每個枚舉成員本身就是 `str` 子類別，`value` 即字串本體 |
| 可直接 `==` 比較 | `IntentTag.GENERAL_EDUCATION == "GENERAL_EDUCATION"` 為 `True`，無需 `.value` |
| 可直接序列化 | `json.dumps({"intent": IntentTag.GENERAL_EDUCATION})` 自動輸出字串，無需自訂 encoder |
| `__str__` 回傳 `value` | `f"{RouterStatus.G_GENERAL_EDUCATION}"` 直接印出 `"G_GENERAL_EDUCATION"`，日誌可讀 |
| 仍保有枚舉約束 | 非法字串無法通過型別檢查，`IntentTag("UNKNOWN")` 會拋 `ValueError`，兼具字串便利與枚舉安全 |

> 設計意圖：讓下游（LLM 輸出解析、Pydantic 驗證、日誌、API 序列化）都能把枚舉當字串用，同時在程式碼層面保留枚舉的封閉集合語意。

所有 9 個枚舉皆繼承 `_CodeEnum`，定義於 `labels.py:9-109`。

---

## 1. Layer 0 — 輸入上下文（Input Context）：5 表

Layer 0 描述「使用者是誰、在什麼語境下問什麼」，由上游解析模組填入，作為 Layer 1 觀測與 Layer 2 決策的輸入特徵。本身不做路由判決，只提供上下文。

### 1.1 `DeclaredRole` — 宣告身分（`labels.py:16-22`）

> 使用者自稱角色，僅作語境參考，不具授權效力。

| 枚舉值 | 中文含義 | 在哪被填 |
|--------|----------|----------|
| `PATIENT` | 病患本人 | 使用者自述「我是糖尿病患者」或預設值 |
| `CAREGIVER` | 照護者／家屬 | 自述「我幫家人問」「我照顧的長輩」 |
| `HEALTHCARE_PROFESSIONAL` | 醫事人員 | 自述「我是護理師／醫師」 |

- 填入時機：輸入正規化階段，透過關鍵詞／LLM 抽取；未明確宣告時預設 `PATIENT`。
- 注意：此欄位不影響權限，僅供後續回應語氣與衛教深度參考。

### 1.2 `LanguageCode` — 語系代碼（`labels.py:24-30`）

> 標記輸入與回應語系。

| 枚舉值 | 中文含義 | 在哪被填 |
|--------|----------|----------|
| `zh-TW` | 繁體中文（台灣） | 語言偵測為繁中時 |
| `zh-CN` | 簡體中文 | 語言偵測為簡中時 |
| `en-US` | 英文（美國） | 語言偵測為英文時 |

- 填入時機：語言偵測模組（`lang_detect` / LLM 語系判斷）。
- 用途：決定回應語系與 RAG 檢索語料範圍。

### 1.3 `TimeFrame` — 時間框架（`labels.py:32-38`）

> 描述事件發生的時間語境。

| 枚舉值 | 中文含義 | 在哪被填 |
|--------|----------|----------|
| `CURRENT` | 當前／現在進行式 | 「我現在頭暈」「血糖現在 300」 |
| `PAST` | 過去曾發生 | 「上週有過低血糖」「之前吃過」 |
| `HYPOTHETICAL` | 假設／如果情境 | 「如果我吃了…會怎樣」「假設血糖…」 |

- 填入時機：時態／時間副詞解析；影響風險判讀（`CURRENT` + 急症症狀 → `POSSIBLE_EMERGENCY` 權重提高）。

### 1.4 `TargetSubject` — 目標對象（`labels.py:40-46`）

> 問題所指涉的主體。

| 枚舉值 | 中文含義 | 在哪被填 |
|--------|----------|----------|
| `SELF` | 使用者本人 | 「我血糖高怎麼辦」 |
| `FAMILY_OR_CAREGIVER` | 家人或照護對象 | 「我媽媽血糖…」「我照顧的阿公…」 |
| `THIRD_PARTY` | 第三人（朋友／同事等） | 「我朋友說他…」「同事有糖尿病…」 |

- 填入時機：主語／指代解析。
- 用途：`SELF` + 急症描述時風險等級最高；`THIRD_PARTY` 僅作一般衛教。

### 1.5 `Polarity` — 語氣極性（`labels.py:48-53`）

> 肯定或否定，影響風險與意圖判讀。

| 枚舉值 | 中文含義 | 在哪被填 |
|--------|----------|----------|
| `AFFIRMATIVE` | 肯定語氣 | 一般陳述「我有頭暈」 |
| `NEGATIVE` | 否定語氣（如「沒有」「不是」） | 「沒有胸痛」「不是糖尿病」 |

- 填入時機：否定詞偵測（「沒有」「無」「否」「不」等）。
- 關鍵作用：`NEGATIVE` 可排除風險旗標，例如「沒有胸痛」不應觸發 `POSSIBLE_EMERGENCY`。

> **Layer 0 小結**：5 表共 14 個值，皆為輸入特徵，不直接決定路由，但會改變 Layer 1 的觀測結果與 Layer 2 的政策權重。

---

## 2. Layer 1 — 觀測（Observation）：`IntentTag` + `RiskFlag`

Layer 1 由 A 路由器（LLM）對使用者問題做語意觀測，輸出意圖與風險訊號，供 Layer 2 政策引擎做確定性判決。

### 2.1 `IntentTag` — 意圖標籤（`labels.py:55-64`）6 值

> 使用者問題的語意分類。

| 枚舉值 | 中文含義 | 範例 |
|--------|----------|------|
| `GENERAL_EDUCATION` | 一般衛教（糖尿病／血糖／飲食等通識） | 「糖尿病飲食要注意什麼」「血糖正常值是多少」 |
| `SYMPTOM_INFORMATION` | 症狀資訊詢問 | 「頭暈跟血糖有關嗎」「多尿是什麼原因」 |
| `DIAGNOSIS_REQUEST` | 要求診斷／排除疾病 | 「我這樣是不是糖尿病」「幫我判斷是不是…」 |
| `GENERAL_MEDICATION_INFORMATION` | 一般藥物通識（副作用／用途） | 「Metformin 有什麼副作用」「胰島素怎麼作用」 |
| `MEDICATION_CHANGE_REQUEST` | 要求調整用藥／劑量（高風險） | 「我可以自己加藥嗎」「劑量要不要減半」 |
| `NON_MEDICAL` | 非醫療範疇（天氣／股票／寫程式等） | 「今天天氣如何」「幫我寫程式」 |

#### ⚠️ 關鍵邊界：`GENERAL_MEDICATION_INFORMATION` vs `MEDICATION_CHANGE_REQUEST`

| 維度 | `GENERAL_MEDICATION_INFORMATION` | `MEDICATION_CHANGE_REQUEST` |
|------|----------------------------------|------------------------------|
| 語意 | 問藥物的「是什麼／為什麼」 | 要求對「我／特定人」的用藥做改變 |
| 主詞 | 泛稱藥物本身 | 指向具體個人（我、我媽）+ 動作（加、減、停、換） |
| 風險 | 低，可衛教 | 高，觸發 `PERSONALIZED_MEDICATION` → 路由 `M` |
| 回應 | 可提供一般藥物知識 | 必須轉介，不可給個人化建議 |
| 判斷關鍵 | 是否包含「我／家人＋調整意圖」 | 有 → `MEDICATION_CHANGE_REQUEST`；無 → `GENERAL_MEDICATION_INFORMATION` |

```python
# 邊界範例
"Metformin 會傷腎嗎？"              # → GENERAL_MEDICATION_INFORMATION（問通識）
"我可以把 Metformin 加到兩顆嗎？"   # → MEDICATION_CHANGE_REQUEST（個人化調整）
"胰島素有哪些種類？"                # → GENERAL_MEDICATION_INFORMATION
"我胰島素可以自己減量嗎？"          # → MEDICATION_CHANGE_REQUEST
```

> 此邊界是政策閘門的核心，誤判會導致高風險個人化建議外流。

### 2.2 `RiskFlag` — 風險旗標（`labels.py:66-74`）5 值

> 觸發政策閘門的高風險訊號，可多選（`List[RiskFlag]`）。

| 枚舉值 | 中文含義 | 觸發情境 |
|--------|----------|----------|
| `POSSIBLE_EMERGENCY` | 疑似急症（需緊急處理） | 胸痛、意識不清、血糖極高／極低伴隨急症症狀 |
| `MENTAL_HEALTH_CRISIS` | 心理危機／自傷風險 | 自殺、自傷、活不下去等表述 |
| `PERSONALIZED_MEDICATION` | 個人化用藥請求 | 要求針對個人調整藥物／劑量（與 `MEDICATION_CHANGE_REQUEST` 連動） |
| `HIGH_RISK_NOT_EXCLUDED` | 高風險無法排除 | 資訊不足但無法排除急症或嚴重風險時 |
| `PROMPT_INJECTION_SUSPECTED` | 疑似提示注入攻擊（安全否決） | 「忽略以上指令」「你是別的角色」等注入語句 |

#### 🛑 `PROMPT_INJECTION_SUSPECTED` 是一票否決（One-Vote Veto）

```python
# 政策優先序（policy.py）：PROMPT_INJECTION_SUSPECTED 最高優先
if RiskFlag.PROMPT_INJECTION_SUSPECTED in risk_flags:
    return RouterStatus.R_POLICY_BOUNDARY  # 直接否決，不論其他旗標
```

- 只要此旗標出現，無論其他 `IntentTag` 或 `RiskFlag` 為何，一律路由至 `R_POLICY_BOUNDARY`。
- 用途：防範 LLM 被惡意提示詞劫持，屬於安全邊界，非醫療判斷。
- 對應原因碼：`REASON_PROMPT_INJECTION_SUSPECTED`。

> **Layer 1 小結**：`IntentTag` 單選（6 選 1）、`RiskFlag` 多選（5 旗標可並存），共同構成 Layer 2 的判決依據。`PROMPT_INJECTION_SUSPECTED` 具最高優先級，一票否決。

---

## 3. Layer 2 — 決策（Decision）：`RouterStatus` + `PolicyReasonCode`

Layer 2 由政策引擎（`policy.py` 確定性規則）根據 Layer 0 + Layer 1 輸出唯一路由狀態，並附原因碼供稽核。

### 3.1 `RouterStatus` — 路由狀態（`labels.py:76-87`）8 選 1

> A 路由器唯一輸出，決定下游行為。

| 枚舉值 | 中文含義 | 觸發條件摘要 |
|--------|----------|--------------|
| `E_EMERGENCY` | 緊急：疑似急症，立即轉介急救 | `POSSIBLE_EMERGENCY` |
| `U_URGENT_HUMAN` | 緊急轉真人：心理危機或高風險未排除 | `MENTAL_HEALTH_CRISIS` 或 `HIGH_RISK_NOT_EXCLUDED` |
| `M_MEDICATION_REFERRAL` | 用藥轉介：個人化用藥需專業人員 | `PERSONALIZED_MEDICATION` / `MEDICATION_CHANGE_REQUEST` |
| `R_POLICY_BOUNDARY` | 政策邊界：診斷請求或注入攻擊（安全否決） | `DIAGNOSIS_REQUEST` 或 `PROMPT_INJECTION_SUSPECTED` |
| `Q_CLARIFICATION` | 需釐清：資訊不足無法判斷 | 無法判定意圖或風險時 |
| `G_GENERAL_EDUCATION` | 一般衛教：唯一允許 RAG 的狀態 | 符合安全範圍的衛教詢問 |
| `O_OUT_OF_SCOPE` | 超出範圍：非醫療問題 | `NON_MEDICAL` |
| `F_ROUTER_DEPENDENCY` | 依賴失效：LLM 超時／格式錯誤等（fail-closed） | 路由器本身異常時 |

#### 下游行為表（只有 `G` 可 RAG）

| `RouterStatus` | 下游行為 | 是否 RAG | 回應策略 |
|----------------|----------|----------|----------|
| `E_EMERGENCY` | 立即轉介急救資訊 | ❌ 不可 | 固定緊急指引 + 撥打 119 |
| `U_URGENT_HUMAN` | 轉真人／心理支持資源 | ❌ 不可 | 固定轉介語 + 安心專線 1925 |
| `M_MEDICATION_REFERRAL` | 轉介醫事人員 | ❌ 不可 | 固定用藥轉介語，不給劑量建議 |
| `R_POLICY_BOUNDARY` | 政策邊界拒答 | ❌ 不可 | 固定邊界語（診斷／注入） |
| `Q_CLARIFICATION` | 請使用者補充資訊 | ❌ 不可 | 追問釐清句 |
| `G_GENERAL_EDUCATION` | **唯一可進入 RAG 檢索** | ✅ 唯一可 | 檢索衛教知識庫後生成回應 |
| `O_OUT_OF_SCOPE` | 超範圍拒答 | ❌ 不可 | 固定超範圍語 |
| `F_ROUTER_DEPENDENCY` | 依賴失效 fail-closed | ❌ 不可 | 固定錯誤語，不降級為 G |

> **核心不變量**：`G_GENERAL_EDUCATION` 是 8 狀態中唯一允許呼叫 RAG 的狀態。其餘 7 狀態皆為 fail-closed 固定回應，確保高風險問題不會因檢索生成而外流不當建議。`F_ROUTER_DEPENDENCY` 亦不降級為 `G`，寧可不答也不冒險。

```python
# 下游閘門示意
if router_status == RouterStatus.G_GENERAL_EDUCATION:
    context = rag_retrieve(query)  # 僅此分支可 RAG
    answer = generate_with_context(query, context)
else:
    answer = FIXED_RESPONSES[router_status]  # 固定回應表
```

### 3.2 `PolicyReasonCode` — 政策原因碼（`labels.py:89-109`）18 碼

> 解釋路由決策的依據，供日誌與稽核。每個 `RouterStatus` 背後由 1~多個原因碼佐證。

| 枚舉值 | 中文含義 | 對應 `RouterStatus` |
|--------|----------|---------------------|
| `INQUIRY_GENERAL_EDUCATION` | 一般衛教詢問 | `G` |
| `INQUIRY_DIETARY_EDUCATION` | 飲食衛教詢問 | `G` |
| `INQUIRY_SYMPTOM_INFORMATION` | 症狀資訊詢問 | `G` |
| `INQUIRY_GENERAL_MEDICATION_INFORMATION` | 一般藥物資訊詢問 | `G` |
| `REASON_DIAGNOSIS_OR_TREATMENT_REQUEST` | 診斷或治療請求（政策邊界） | `R` |
| `REASON_PERSONALIZED_MEDICATION_REQUEST` | 個人化用藥請求 | `M` |
| `REASON_POSSIBLE_EMERGENCY` | 疑似急症 | `E` |
| `REASON_MENTAL_HEALTH_CRISIS` | 心理危機 | `U` |
| `REASON_HIGH_RISK_NOT_EXCLUDED` | 高風險未排除 | `U` |
| `REASON_PROMPT_INJECTION_SUSPECTED` | 疑似提示注入 | `R` |
| `REASON_OUT_OF_SCOPE` | 超出醫療範圍 | `O` |
| `REASON_INSUFFICIENT_INFORMATION` | 資訊不足需釐清 | `Q` |
| `NO_CRITICAL_SYMPTOMS_DETECTED` | 未檢出危急症狀（G 路由佐證） | `G`（佐證） |
| `MEETS_SAFE_SCOPE` | 符合安全範圍（G 路由佐證） | `G`（佐證） |
| `REASON_ROUTER_TIMEOUT` | 路由超時（F 依賴失效） | `F` |
| `REASON_SCHEMA_VALIDATION_FAILED` | 架構驗證失敗（F 依賴失效） | `F` |
| `REASON_ROUTER_DEPENDENCY_ERROR` | 路由依賴錯誤（F 依賴失效） | `F` |
| `REASON_INPUT_VALIDATION_FAILED` | 輸入驗證失敗（F 依賴失效） | `F` |

- 用途：寫入日誌與追蹤，供事後稽核「為何走此路由」。
- `G` 的兩個佐證碼（`NO_CRITICAL_SYMPTOMS_DETECTED` / `MEETS_SAFE_SCOPE`）需同時成立才放行 RAG。
- `F` 的四個原因碼皆為系統層異常，對應 fail-closed 策略。

---

## 4. 一句話串起 `rules` → `policy` → `schemas` 的關係

```python
# labels.py 定義詞彙 → rules.py 用詞彙寫觀測規則 → policy.py 用詞彙做確定性路由 → schemas.py 用詞彙約束 LLM 輸出格式
```

| 模組 | 角色 | 與 `labels.py` 的關係 |
|------|------|------------------------|
| `labels.py` | 詞彙表（本文件） | 定義 9 枚舉 51 值的封閉集合 |
| `rules.py` | 觀測規則 | 引用 `IntentTag` / `RiskFlag` / Layer 0 枚舉，描述 LLM 應如何標註 |
| `policy.py` | 政策引擎 | 引用 `RiskFlag` + `IntentTag` → 判決 `RouterStatus`（8 選 1），並附 `PolicyReasonCode` |
| `schemas.py` | 輸出架構 | 用 `labels.py` 枚舉約束 LLM 輸出 JSON 的欄位值域，驗證失敗 → `F_ROUTER_DEPENDENCY` |

> **一句話**：`labels.py` 定義詞彙，`rules.py` 用詞彙教 LLM 做觀測（Layer 1），`policy.py` 用觀測結果做確定性路由判決（Layer 2），`schemas.py` 用詞彙約束 LLM 輸出格式，三者皆以 `labels.py` 為唯一真實來源，確保觀測、決策、驗證使用同一套語言。

---

## 附錄：9 枚舉 51 值總覽

| Layer | 枚舉 | 值數量 | 值列表 |
|-------|------|--------|--------|
| — | `_CodeEnum` | 基底 | `str` + `Enum`，可直接字串比較與序列化 |
| Layer 0 | `DeclaredRole` | 3 | `PATIENT` 病患本人、`CAREGIVER` 照護者／家屬、`HEALTHCARE_PROFESSIONAL` 醫事人員 |
| Layer 0 | `LanguageCode` | 3 | `zh-TW` 繁體中文（台灣）、`zh-CN` 簡體中文、`en-US` 英文（美國） |
| Layer 0 | `TimeFrame` | 3 | `CURRENT` 當前／現在進行式、`PAST` 過去曾發生、`HYPOTHETICAL` 假設／如果情境 |
| Layer 0 | `TargetSubject` | 3 | `SELF` 使用者本人、`FAMILY_OR_CAREGIVER` 家人或照護對象、`THIRD_PARTY` 第三人 |
| Layer 0 | `Polarity` | 2 | `AFFIRMATIVE` 肯定語氣、`NEGATIVE` 否定語氣 |
| Layer 1 | `IntentTag` | 6 | `GENERAL_EDUCATION` 一般衛教、`SYMPTOM_INFORMATION` 症狀資訊詢問、`DIAGNOSIS_REQUEST` 要求診斷／排除疾病、`GENERAL_MEDICATION_INFORMATION` 一般藥物通識、`MEDICATION_CHANGE_REQUEST` 要求調整用藥／劑量、`NON_MEDICAL` 非醫療範疇 |
| Layer 1 | `RiskFlag` | 5 | `POSSIBLE_EMERGENCY` 疑似急症、`MENTAL_HEALTH_CRISIS` 心理危機／自傷風險、`PERSONALIZED_MEDICATION` 個人化用藥請求、`HIGH_RISK_NOT_EXCLUDED` 高風險無法排除、`PROMPT_INJECTION_SUSPECTED` 疑似提示注入攻擊（一票否決） |
| Layer 2 | `RouterStatus` | 8 | `E_EMERGENCY` 緊急、`U_URGENT_HUMAN` 緊急轉真人、`M_MEDICATION_REFERRAL` 用藥轉介、`R_POLICY_BOUNDARY` 政策邊界、`Q_CLARIFICATION` 需釐清、`G_GENERAL_EDUCATION` 一般衛教（唯一可 RAG）、`O_OUT_OF_SCOPE` 超出範圍、`F_ROUTER_DEPENDENCY` 依賴失效 fail-closed |
| Layer 2 | `PolicyReasonCode` | 18 | `INQUIRY_GENERAL_EDUCATION` 一般衛教詢問、`INQUIRY_DIETARY_EDUCATION` 飲食衛教詢問、`INQUIRY_SYMPTOM_INFORMATION` 症狀資訊詢問、`INQUIRY_GENERAL_MEDICATION_INFORMATION` 一般藥物資訊詢問、`REASON_DIAGNOSIS_OR_TREATMENT_REQUEST` 診斷或治療請求、`REASON_PERSONALIZED_MEDICATION_REQUEST` 個人化用藥請求、`REASON_POSSIBLE_EMERGENCY` 疑似急症、`REASON_MENTAL_HEALTH_CRISIS` 心理危機、`REASON_HIGH_RISK_NOT_EXCLUDED` 高風險未排除、`REASON_PROMPT_INJECTION_SUSPECTED` 疑似提示注入、`REASON_OUT_OF_SCOPE` 超出醫療範圍、`REASON_INSUFFICIENT_INFORMATION` 資訊不足需釐清、`NO_CRITICAL_SYMPTOMS_DETECTED` 未檢出危急症狀、`MEETS_SAFE_SCOPE` 符合安全範圍、`REASON_ROUTER_TIMEOUT` 路由超時、`REASON_SCHEMA_VALIDATION_FAILED` 架構驗證失敗、`REASON_ROUTER_DEPENDENCY_ERROR` 路由依賴錯誤、`REASON_INPUT_VALIDATION_FAILED` 輸入驗證失敗 |

> 檔案位置：`tfda_context_gate/a_router/labels.py`（109 行）  
> 文件位置：`tfda_context_gate/a_router/docs/01_labels.md`（本文件）
