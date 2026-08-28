本專案為糖尿病照護領域的本地部署對話系統，採用雙層 LLM 防守架構（Input Router + Policy Gate）進行安全把關，僅在確認符合規範時將結構化資料交付下游 RAG 模組。

---

### 專案基本設定

* **目標受眾**：糖尿病患者本人、照護者、專業醫護人員。
* **核心功能**：衛教諮詢與互動式糖尿病照護聊天機器人。
* **模型部屬與調優**：
* **環境**：本地端部署（Local LLM）。
* **調優策略**：系統 Prompt 迭代微調、推論參數調校（Temperature、Top-K、Top-P），並透過多輪對話測試驗證分流準確度。



---

### 雙層安全守門架構 (Two-Tier Guardrail)

為降低單次推論的誤判風險，系統將輸入檢驗拆分為兩階段：

```
[使用者原始輸入] 
       ↓
[第一層 LLM：特徵與語境抽取] 
  ├─ intent_tags (意圖)
  ├─ risk_flags (風險)
  └─ context_modifiers (語境/修飾)
       ↓
[第二層 LLM：安全判定與決策]
  ├─ router_status (決策狀態碼)
  └─ reason_codes (判斷原因與審計紀錄)
       ↓
[Gate 判定] ── (放行: G_GENERAL_EDUCATION) ──→ [整合 JSON Payload 送往 RAG]
       └── (攔截: 非 G 狀態) ──→ [觸發對應攔截模板 / 轉介流程]

```

**第一層：特徵與語境抽取**

* **`intent_tags`（意圖標籤）**：界定使用者行為目的。
* 選項：`一般衛教`、`症狀資訊`、`診斷要求`、`一般藥品資訊`、`停換藥／劑量要求`、`非醫療`。


* **`risk_flags`（風險旗標）**：觸發安全防護機制（採最高風險優先，不互抵）。
* 選項：`可能急症`、`心理危機`、`個人化用藥`、`無法排除高風險`、`疑似注入`。


* **`context_modifiers`（語境修飾詞）**：防止關鍵字誤判，補足時態與主體。
* 維度：時間（現在／過去／假設）、主體（本人／家屬／第三人）、極性（肯定／否定）、語言別。



**第二層：安全決策與審計追蹤**

* **`router_status`（決策狀態碼）**：評估第一層特徵後輸出的最終路由動作。
* **`reason_codes`（判斷原因）**：記錄 LLM 的推論邏輯，供開發除錯（Debug）與醫療爭議留倉審計（Audit Trail）。

---

### 路由狀態決策矩陣 (Router Status)

| 決策碼 (`router_status`) | 觸發情境 | 是否進 RAG | 預期處理動作 |
| --- | --- | --- | --- |
| **`E_EMERGENCY`** | 命中臨床核准的立即危險條件，或無法排除高風險 | **否** | 顯示核准且地區化的短版緊急處置引導 |
| **`U_URGENT_HUMAN`** | 需要儘快由真人評估，但未達立即危險 | **否** | 提供真實就醫管道、諮詢時段與備援聯絡資訊 |
| **`M_MEDICATION_REFERRAL`** | 要求加減劑量、停換藥、個案藥物交互作用判定 | **原則上否** | 引導至原處方醫師或藥師；通用資訊走限縮流程 |
| **`R_POLICY_BOUNDARY`** | 要求確診、排除疾病或提供個人化治療決策 | **否** | 說明系統能力邊界，提供合規之就醫建議步驟 |
| **`Q_CLARIFICATION`** | 資訊不足且僅需補充單一關鍵資訊即可安全分類 | **否** | 針對最小必要資訊進行一次性追問 |
| **`G_GENERAL_EDUCATION`** | 符合安全政策之一般糖尿病衛教需求 | **是** | **放行進入 RAG 檢索與生成流程** |
| **`O_OUT_OF_SCOPE`** | 非醫療需求或超出產品設計服務範圍 | **否** | 簡短說明系統服務範圍 |
| **`F_ROUTER_DEPENDENCY`** | 路由逾時、JSON Schema 格式無效或系統異常 | **否** | 安全降級退回，禁止未分類內容直通 RAG |

---

### 傳入 RAG 端的 JSON 交付規格

當判定結果為 `G_GENERAL_EDUCATION` 時，系統會將原始輸入與 1~5 點之結構化標籤封裝為單一 JSON 傳送至 RAG 端：

```json
{
  "user_raw_input": "我最近剛驗出飯後血糖 180，想請問第二型糖尿病一般飲食該怎麼控制？",
  "guardrail_result": {
    "intent_tags": ["一般衛教", "症狀資訊"],
    "risk_flags": [],
    "context_modifiers": {
      "time_frame": "現在",
      "target_subject": "本人",
      "polarity": "肯定",
      "language": "zh-TW"
    },
    "router_status": "G_GENERAL_EDUCATION",
    "reason_codes": [
      "INQUIRY_DIETARY_EDUCATION",
      "NO_CRITICAL_SYMPTOMS_DETECTED",
      "MEETS_SAFE_SCOPE"
    ]
  },
  "timestamp": "2026-08-20T18:58:57+08:00"
}

```

[備註]
```txt
　　在工程落地與系統穩定度上，建立統一的「標籤字典（Label Dictionary / Taxonomy）」與嚴格的 Schema 是絕對必要的核心做法。
　　如果沒有限制標籤字典，讓 LLM 自由生成標籤，模型很容易產生語意相近但字串不同的輸出（例如：今天輸出 一般衛教，明天輸出 衛教諮詢 或 飲食衛教），這會導致下游的 Gate 規則與 RAG 檢索邏輯直接崩潰。

標籤字典維護建議
- 第一層（客觀語意特徵抽取）：字典顆粒度適中即可，專注於「抽取語意事實」，避免在此層做主觀定罪。
- 第二層（臨床政策與合規裁決）：router_status 必須是 100% 固定的 8 組狀態碼；reason_codes 也建議維護一份標準代碼表（例如 REASON_ACUTE_HYPOGLYCEMIA、REASON_OUT_OF_DIABETES_SCOPE），以便進行自動化日誌分類與警示監控。

廣義而言，它繼承了 CoT（思維鏈，Chain of Thought）「將複雜問題拆解為多步推理」 的核心哲學；但在嚴謹的系統架構分類上，這種做法更精確的術語是 Prompt Chaining（提示鏈） 或 Multi-stage Pipeline（多階段模組化管線）。

兩者的核心相同點（思維本質）
任務分解（Task Decomposition）：兩者皆不要求模型「一步到位」給出最終決策，而是先產出中間過渡資訊（第一層抽取事實特徵），再基於中間資訊推導最終結果（第二層裁決）。

可解釋性與審計（Auditability）：藉由保留中間狀態（intent_tags、risk_flags 等），讓決策過程具備透明度，避免黑箱直出。
```