# D：Output Gate 技術調查報告

> 對應「LLM 組下週技術調查分工 A～E」中的 D 模組
> 情境：醫療衛教 MVP
> 目標：這一關有哪些做法，以及我們為什麼最後要選其中一種

---

## 1. 我的模組是做什麼

Output Gate 站在整條 pipeline 的最後一關：

**Generator 已經產生回答 → Output Gate 判斷這個回答能不能真的送給使用者 → 通過就回答，不通過就 fallback。**

核心前提是：**不要預設 Generator 自己說安全就是真的安全。** LLM 的輸出受使用者輸入影響，本質上要當成「未經驗證的資料」來看待，而不是可信結果。OWASP 在 LLM05（Improper Output Handling）就把「LLM 產生的輸出沒有經過適當驗證、清理與處理就往下游傳遞」列為獨立風險項目，強調輸出在送給使用者或其他系統之前必須先驗證。

Output Gate 要檢查的東西，實際上可以分成兩個不同性質的層面，這點很重要，因為它決定了要用什麼技術：

**A. 內容安全 / 證據一致（對使用者的風險）** — 這是原規劃 D 的重點，尤其在醫療情境成本很高：

- 有沒有資料來源沒有支持的 Claim（幻覺）
- 有沒有個人化診斷
- 有沒有藥物劑量、停藥或換藥建議
- 有沒有過度保證
- 引用的 Evidence ID 是否真的存在
- 有沒有越過 A（Policy Gate）原本設定的邊界

**B. 技術性輸出處理（對下游系統的風險）** — 這是 OWASP LLM05 的原始重點：如果輸出會被前端渲染、被程式解析、或觸發任何下游動作，就要防 XSS、注入等。醫療衛教 MVP 如果只是純文字回覆給使用者，這一塊風險較低，但只要輸出進到任何會「執行」或「渲染」它的地方，就必須處理。

**這份報告的比較主要聚焦在 A（內容與證據層），並在結尾說明 B 要怎麼一起顧。**

一個關鍵的分類原則貫穿全篇：Output Gate 的檢查項目，有些是**結構性、可以用死規則判斷的**（格式、欄位、ID 是否存在、code 是否合法），有些是**語意性、需要理解內容才能判斷的**（Claim 有沒有被證據支持、有沒有過度保證、有沒有個人化診斷）。好的 Output Gate 不是選一種方法，而是把對的檢查交給對的機制。

---

## 2. 我找到哪些做法

依「便宜且確定 → 昂貴且需要語意」的順序排列。

### 方向 1：Rule / Schema Validation（規則 / 結構驗證）

用程式直接驗證輸出的**結構**，完全不需要 LLM。例如 Schema 格式是否合法（是否為合法 JSON、欄位是否齊全）、`decision code` 是否在允許清單內、`evidence IDs` 欄位是否存在、引用的 Evidence ID 是否真的對得上這次檢索回來的文件集合。

LangChain 官方 Guardrails 文件把這類稱為 rule-based（deterministic）guardrail，用 regex、關鍵字比對或明確條件檢查,並建議「先跑 deterministic guardrail（便宜、快），再跑 model-based guardrail（貴、深入）」。JSON validity 是最常見的一種輸出檢查。

### 方向 2：Policy Rules（政策規則 / 禁止行為比對）

用一組固定的政策規則去找禁止出現的行為，例如偵測藥物劑量數字、「停藥／換藥」等指示性字眼、過度保證的措辭（「保證痊癒」「一定不會」）。

這比純結構檢查多了「內容政策」的意味，但仍然是規則式的。**要注意的是單純敏感詞清單的缺點**：容易被改寫繞過（同義、換句話說），也容易誤殺（衛教內容本來就會提到藥名），無法理解語境（「請勿自行調整劑量」和「把劑量調整為 X」字面都含「劑量」但意義相反）。所以 Policy Rules 適合抓「明確、字面就能定義」的紅線,不適合當唯一防線。

### 方向 3：LLM-as-a-Judge（第二次 LLM 呼叫做裁判）

用第二個 LLM call 去判斷這個回答是否安全、是否相關、是否越界。LangChain 的做法是在 `after_agent()` 這種「產生回答之後、使用者看到之前」的 hook 用一個便宜快速的模型當「安全裁判」。

優點是能處理語意（過度保證、暗示性診斷這種規則抓不到的東西）。**但缺點在醫療情境特別要當心**：

- Generator 和 Judge 可能犯同樣的錯（相關性錯誤,correlated errors）,尤其兩者同模型、同 prompt 家族時，Judge 會「同意」Generator 的幻覺。
- Judge 本身也可能幻覺、也有延遲與 token 成本、判斷不完全穩定。
- 降低相關性錯誤的做法：用**不同模型**或**明顯不同的 prompt / 角色設定**當 Judge，並把它的任務縮小、聚焦（只判一件事而不是全部）。

### 方向 4：Claim-level Verification（逐句對證據驗證）

把答案拆成一個一個原子 Claim，再逐一對照 Evidence，標記為：

- **Supported**（有證據支持 / 蘊含 entailed）
- **Contradicted**（與證據矛盾）
- **Not in source**（證據裡根本沒有 / baseless）

這正是 RAG 評估裡 **Groundedness / Faithfulness** 的核心：檢查每一個 Claim 能不能追回到提供的 context。學界與工具常用 NLI（自然語言推論）把回答拆成原子 claim,再逐一判斷 entailed / contradicted / neutral,支持比例即 faithfulness 分數。它比「回答完才補來源」嚴格得多，因為它是**逐句驗證證據是否真的支持**，而不是事後貼標籤。

值得注意的一個細節：Groundedness 通常當**硬性 pass/fail 閘門**，Faithfulness 則常給連續分數（用來看趨勢、抓部分幻覺）。實務上有人用不同門檻分流：低於某分先擋、中間分數送人工複查。這對 Output Gate 很有用——它天然支援「擋 / 放行 / 送複查」三種出口。

### 方向 5：Hybrid Output Gate（分層閘門，推薦形態）

把上面幾種**串成一條有順序的閘門**，例如：

**Schema/結構驗證 → Rule/Policy → Claim-level Verification →（必要時）LLM Judge → 通過 / fallback**

精神是「防禦縱深」＋「便宜的先擋」：能用死規則秒殺的（格式錯、Evidence ID 不存在、非法 decision code）就不要花 LLM 成本；語意層的（unsupported claim、過度保證）才交給 Claim 驗證或 Judge。任何一關 fail 就走 fallback，不硬送。

---

## 3. 各自優缺點

比較角度：準確性、安全性、延遲、Token/成本、開發難度、可維護性、可測試性。

| 方法 | 準確性 | 安全性 | 延遲 | Token/成本 | 開發難度 | 可維護性 | 可測試性 |
|---|---|---|---|---|---|---|---|
| **1. Rule/Schema** | 結構問題 100% 準；語意問題完全抓不到 | 只擋得住格式與 ID 類錯誤 | 極低（毫秒級） | 幾乎零 | 低 | 高（規則明確） | 極高（輸入固定→輸出固定） |
| **2. Policy Rules** | 明確紅線準；改寫易漏、語境易誤殺 | 擋得住字面禁止行為，擋不住換句話說 | 極低 | 幾乎零 | 低～中 | 中（規則會越積越多、需維護清單） | 高 |
| **3. LLM Judge** | 能抓語意問題；但判斷不穩、可能與 Generator 同錯 | 中～高，但有 correlated error 風險 | 高（多一次 LLM call） | 高 | 中 | 中（prompt 需版本管理） | 較低（非決定性，需統計式測試） |
| **4. Claim-level Verification** | 對「證據是否支持」最準、可定位到句 | 對幻覺 / 越權醫療 claim 最有效 | 中～高（依實作，NLI 比大模型便宜） | 中～高 | 中～高 | 中 | 中（可用標註集量化 faithfulness） |
| **5. Hybrid** | 綜合最佳：各檢查交給對的機制 | 最高（多層、可設多個出口） | 可控（便宜的先擋，貴的少觸發） | 可控（大多數請求不會走到 LLM 層） | 高（要設計串接與 fallback） | 中～高（分層清楚但元件多） | 高（每層可分開測） |

一句話總結各自的定位：

- **Rule/Schema、Policy** 在檢查「**格式與明確紅線**」——確定性高、幾乎免費，但看不懂語意。
- **Claim-level Verification** 在檢查「**證據是否真的支持這句話**」（groundedness）——是醫療幻覺的主要防線。
- **LLM Judge** 在檢查「**整體語氣與越界**」這種難以規則化的語意——彈性最高但最不穩、成本最高，且不能讓它和 Generator 同錯。

---

## 4. 我目前比較推薦哪一種

**推薦：方向 5 Hybrid Output Gate，順序為 Schema → Rule/Policy → Claim-level Verification →（必要時）LLM Judge。**

原因我覺得有以下幾點：

**（1）醫療衛教的錯誤成本不對稱，單一機制都有致命缺口。** 給錯劑量、做出個人化診斷、過度保證這類輸出，一旦送出成本極高。任何單一方法都有明顯漏洞：純規則看不懂語意、純 LLM Judge 會和 Generator 同錯又不穩。Hybrid 用多層把不同性質的風險分開擋，才撐得住醫療門檻。

**（2）成本與延遲其實可控，而不是更貴。** Hybrid 的重點是「便宜且確定的先擋」。大量請求會在 Schema / Rule 這層就被放行或攔截，只有少數需要語意判斷的才走到 Claim 驗證或 Judge。相比「全部丟給第二個 LLM」，Hybrid 平均延遲與 token 反而更低。這也呼應 LangChain「先 deterministic、後 model-based」的官方建議。

**（3）Claim-level Verification 應該是這一關的核心防線，而不是 LLM Judge。** 醫療 MVP 最怕的是「講了 context 沒有的醫療資訊」，這正是 groundedness / faithfulness 要解的問題，逐句對 Evidence 判 Supported / Contradicted / Not-in-source 比讓 Judge「整體感覺一下安不安全」可靠得多，而且能定位到是哪一句出問題，對 debug 和 E 的 trace 都友善。LLM Judge 我建議只當「規則和 Claim 驗證都覆蓋不到的語意殘餘」（例如語氣過度保證）的補充，而且要用**不同模型 / 不同 prompt** 來降低與 Generator 的相關性錯誤。

**（4）回答分工文件的三個核心問題：**

- **哪些檢查適合寫成硬規則**：Schema 格式、欄位缺失、Evidence ID 是否存在、decision code 是否合法、明確字面禁止行為（劑量數字、停換藥指示詞）。
- **哪些檢查需要語意理解**：Claim 是否被證據支持、是否過度保證、是否隱含個人化診斷、是否越過 Policy 精神（非字面）。
- **哪些檢查不應該讓 Generator 自己判斷**：所有安全與 groundedness 判斷都不該由 Generator 自我裁決——裁判必須是獨立的一關（規則、Claim 驗證器、或獨立的 Judge 模型），否則等於讓考生自己改考卷。

**（5）不要遺漏 OWASP LLM05 的技術輸出層。** 就算內容都安全，只要輸出會被前端渲染或被下游程式解析／觸發動作，就要在同一關做輸出清理（sanitization / encoding），避免 XSS、注入等問題。MVP 若只回純文字給使用者，這塊可先最小化，但 Gate 的設計要預留位置。

**MVP 落地建議：** 第一版可以先做 **Schema 驗證 + Evidence ID 存在性檢查 + 少量明確 Policy 紅線 + Groundedness 硬門檻（低於門檻就 fallback）**,LLM Judge 列為第二階段再加。這樣既守住醫療底線,又不會一開始就把延遲和成本推高。

---

## 5. 來源

**OWASP — LLM05:2025 Improper Output Handling**
[https://genai.owasp.org/llmrisk/llm052025-improper-output-handling/](https://genai.owasp.org/llmrisk/llm052025-improper-output-handling/)
定義「LLM 輸出在傳給下游前未經充分驗證、清理與處理」為獨立風險；強調 LLM 輸出受 prompt 影響，必須當成未經驗證的輸入看待，不能直接信任。是「Generator Output 不能直接視為可信」的一手依據。

**LangChain — Guardrails（官方文件）**
[https://docs.langchain.com/oss/python/langchain/guardrails](https://docs.langchain.com/oss/python/langchain/guardrails)
說明 Guardrail 在 Agent 執行流程的關鍵位置（before / after / around）做驗證與過濾，並區分 rule-based（deterministic）與 model-based 兩種互補做法，官方建議「先便宜的 deterministic、後昂貴的 model-based」，直接對應本報告的分層順序。

**LangSmith — Evaluate a RAG application（Groundedness）**
[https://docs.smith.langchain.com/](https://docs.smith.langchain.com/)（RAG evaluation 章節）
把 RAG 評估拆成 Correctness、Answer Relevance、Groundedness、Retrieval Relevance。其中 Groundedness（生成回答 vs 檢索文件是否一致）就是 Claim-level Verification 的入門概念與量化基礎。
（開源替代：Langfuse、Arize Phoenix、RAGAS Faithfulness，皆可本地免費使用。）

**Groundedness / Faithfulness 與 Claim-level 驗證（概念與方法）**
Openlayer, "RAG Evaluation in Production: Groundedness, Faithfulness, and Retrieval Quality"（2026）
[https://www.openlayer.com/blog/rag-pipeline-evaluation-groundedness-faithfulness](https://www.openlayer.com/blog/rag-pipeline-evaluation-groundedness-faithfulness)
說明 groundedness 與 faithfulness 是不同分數，並示範用門檻分流（低於某分擋下 / 送複查），支持本報告「擋 / 放行 / 送複查」三出口的設計。

**Claim 逐句分類（Supported / Contradicted / Not-in-context）的形式化定義**
"Correctness is not Faithfulness in RAG Attributions"（arXiv 2412.18004）
[https://arxiv.org/abs/2412.18004](https://arxiv.org/abs/2412.18004)
一手論文，區分 citation correctness 與 faithfulness，說明只驗「引用正確」不足以建立信任，需同時驗「每個 claim 是否真被證據支持」——為 Evidence-aware 驗證提供學理依據。

**參考交叉：OWASP LLM01 — Prompt Injection**
Output Gate 的 Policy 越界檢查，與注入攻擊試圖讓模型產生越界輸出直接相關，可與 A 的 Input 層對照理解。
