# RAG 組 — M1 工作報告與計畫修訂

> 撰寫：Preprocessing B 成員（Erich）｜日期：2026/08/24
> 依據：《RAG 組 — 提案計劃書 Kickoff_v1》第 3–6 節
> 涵蓋：Multi-RAG A/B、Preprocessing A/B、Boundary A/B/C 共 7 份 M1 產出，
> 　　　以及 LLM 組《LLM組與RAG組對齊問題》的最新對齊要求
> 用途：組內對齊 + M2（8/28）工作分派依據。給 PM 的對外版本為《RAG 組 — 提案報告書（M1 成果與 M2 提案）》

---

## 摘要

M1 的目標是「解除跨組依賴」，不是把細節寫完。以這個標準看，**七項產出中五項完成、兩項缺件**，且七份文件在校對後已收斂到同一套術語與同一份 schema。可以進入 M2。

但這一輪校對與跨組對齊也翻出三件必須在 M2 開始前處理的事，其中第一件會影響整份提案的敘事：

1. **全組共用範例查詢在新的分工下不會進入 RAG。** LLM 組已明確：只有 `router_status = G_GENERAL_EDUCATION` 能呼叫 RAG，而「我腎功能不好，可以吃 metformin 嗎？」被歸為個人化用藥，由 Policy Gate 攔下轉介。我們七份文件全部以這句話當作範例。
2. **Multi-RAG 的合併策略尚未撰寫**，且它卡住 Boundary B 的截斷設計。
3. **旗艦查詢在 Bronze 可檢索集中沒有乾淨的資料來源**——TFDA 129 筆不含 metformin，唯一來源是美國 openFDA 的複方仿單，且已被檢索閘門擋下。

好消息是 LLM 組這份對齊文件同時**關掉了我們原本以為無人認領的責任真空**（高風險事實由誰擋下），並回覆了 Boundary B 的三項 TBD。淨值是：跨組介面比 M1 開始時清楚很多。

---

## 一、M1 交付完成度（對照 Kickoff 第 4 節）

| 團隊 / 成員 | Kickoff 要求的產出 | 狀態 | 說明 |
|---|---|---|---|
| **Multi-RAG A** | 1 頁分類表（類型名、範例、路徑、理由），6–10 種 | ✅ 完成 | 10 種類型，Intent × Risk × Context 多軸。**術語全組最一致**，10 種邊名稱與方向全部正確 |
| **Multi-RAG B** | JSON schema 草案 **＋ 合併策略說明** | 🟡 一半 | 契約完成（校對後）；**合併策略完全未撰寫**，僅出現一次未定義的「Tier 1 / Tier 2」 |
| **Preprocessing A** | 流程圖 ＋ 工具/模型選擇說明 ＋ 3–5 篇試切 | 🟡 三缺一 | 流程圖 ✅、試切 ✅（5 篇、85 chunk、85/85 完成 embedding）；**決策紀錄 `decision_record_v1.md` / `candidates_v2.md` 確認不存在**，但程式註解引用了 4 次 |
| **Preprocessing B** | 抽取工作流圖 ＋ schema 草案 ＋ 3–5 篇試抽 | ✅ 完成 | schema v3（6 節點 / 10 邊）、4 篇試抽收斂、工作流圖、37 條 Bronze 三元組、程式碼獨立 repo |
| **Boundary A** | 邊界機制清單 ＋ 理由 ＋ 改動代價 | ✅ 完成 | 6 項邊界，文獻可對應 |
| **Boundary B** | 限制機制清單 ＋ 與 Context Gate 介面說明 ＋ 已與 LLM 組簡短確認 | ✅ 完成 | 清單完成；三項 TBD **LLM 組已於對齊文件中回覆**，可於會議上關閉 |
| **Boundary C** | 失敗模式表 ＋ ≥6 類測試案例 | ✅ 完成 | 6 項失敗模式 + 6 類測試案例；另主動抓出 Kickoff 自身的引用錯誤 |

**完成度：5/7 完整，2/7 部分。** 兩項缺件都不是設計問題，是文件未撰寫。

### 1.1 校對輪次做了什麼

M1 草案彼此獨立撰寫，因此出現大量「同一件事三種寫法」。已完成一輪交叉校對，產出五份校對版（`MS1/revised/`），原則是**只修事實錯誤，不動作者的論述與架構**：

| 校對版 | 主要修正 |
|---|---|
| `Boundary - A (校對版).md` | 補 schema v3 定案內容、Source Boundary 跨機關標註規則、Temporal/Status 在 Graph 側的執行條件、Traversal 與抽取端拆分原則的相依性（表格與文獻未動） |
| `Boundary - B (校對版).md` | 關係型別改用 schema v3 的 10 種正式名稱；節點 allow-list 補回 `LabParameter` / `Trigger` / `Intervention`；`IS_A` 列入 allow-list 且不計跳數；補 Graph 側第二種分數（`confidence`） |
| `Boundary - C (校對版).md` | 關閉兩項已可回答的 TBD；①②類換成真實案例；⑥類改為殘餘風險；詞彙 anchor 標為「已設計、未實作」 |
| `Multi-RAG - A (校對版).md` | 第 2 類 `INDUCES` → `INTERACTS_WITH`；第 9 類（孕婦）標記資料缺口並改路徑；補一節對 Boundary 的相依性提醒 |
| `Multi-RAG - B (校對版).json` / `.docx` | `entities` 物件化（承載 `id` 與外部詞彙 `code`）；補 `condition` / `effect` / `confidence` / `negation_checked` / `additional_sources`；`object_type` 補回 `Symptom` / `Intervention`；`retriever == graph` 時條件必填。已通過 draft-07 驗證，並以旗艦查詢四條三元組實測 0 錯誤 |

校對中修掉的問題有幾個不是筆誤，而是會讓設計失效的：Boundary B 原本的節點 allow-list 缺 `LabParameter`，照做會讓共用查詢的核心事實檢索不到；Multi-RAG B 的契約缺 `condition` 欄位，會讓禁忌與注意的分級只能靠字串解析還原；`object_type` 缺 `Symptom` 則讓任何副作用三元組都無法通過驗證。

---

## 二、目前實際做出來的東西

M1 不要求全量處理，但兩條管道都超出「紙上規劃」的程度，有可展示的產出。

### 2.1 Graph 管道（Preprocessing B）

- **schema v3**：6 種節點（`Substance` / `Condition` / `Symptom` / `LabParameter` / `Trigger` / `Intervention`）、10 種邊。以 4 篇真實文件手動試抽逐步修訂，前 3 篇各暴露落差、第 4 篇無新落差，視為收斂。
- **6 個落差皆有決策紀錄**，其中三個影響安全性：`CAUTION_FOR` 獨立成邊（避免把「不建議」升級成「絕對禁忌」）、`INDUCES` 與 `CAUSES_SIDE_EFFECT` 分離（黑框警語級 vs 一般副作用）、`IS_A` 階層邊（避免類別層級警訊在查詢成分名時漏掉）。
- **程式碼**：`triples.py`（Pydantic 型別強制）、`extraction_prompt.md`（7 條抽取原則 + few-shot）、`field_mapping.py`（規則式）、`extract.py`（LLM 抽取）、`normalize.py`（合併去重）。
- **Bronze 資料集 37 條三元組**，全數在糖尿病範圍內且通過 schema 驗證。經一輪自我審查修掉 6 個問題（實體 id 衝突、段落壞節點、去重 provenance、檢索閘門、詞彙代碼、抽取 prompt 修訂）。

### 2.2 Vector 管道（Preprocessing A）

- **策略 B（語意單位／結構感知切分）定案**，策略 A（固定長度）保留為對照組。
- 5 篇 TFDA 公告（`tfda-risk-035 / 019 / 026 / 100 / 027`），策略 A 27 個 chunk vs 策略 B 85 個 chunk。
- **85/85 完成 embedding**（Gemini，3072 維，已實測 L2 norm = 1.0）。語意抽查：相關段落 cosine 0.85、不相關 0.77。
- **與 Graph 管道對齊做得最徹底**：沿用同一套 `tfda-risk-NNN` source id、`schema.py` 對稱於 `triples.py`、`entity_codes` 沿用 Graph 的詞彙對照、`drug_class` 留空等 Graph 的類別對照表。`IndexedChunk` / `RetrievedChunk` 兩階段模型指出「索引時還沒有 score」這件事，是原契約沒處理到的。

### 2.3 已知的資料與品質問題（不迴避）

| 問題 | 影響 | 處理 |
|---|---|---|
| **旗艦查詢沒有乾淨來源** | TFDA 129 筆不含 metformin；唯一來源 openFDA 複方仿單（ZITUVIMET），且因檢索閘門擋下複方展開的高風險邊，**Bronze 可檢索集中沒有任何一條 metformin 腎功能禁忌事實** | M2 必須取得 TFDA 或糖尿病學會的 metformin 單方來源 |
| 詞彙代碼幾乎全空 | 82 個實體位置僅接上 `Metformin→RxNorm:6809`、`eGFR→LOINC:48642-3`，其餘待 UMLS 帳號 | 影響 Boundary C 的 anchor 控制；M2 補 |
| Vector 切分 bug | `_NUMBERED_POINT_RE` 會切進網址，`tfda-risk-019` 產生只有「訊息緣由：4.htm」的垃圾 chunk，且已被 embed | 已回報，M2 前修 |
| 過短 chunk 與 Graph 重工 | 9 個 chunk 不到 30 字（如「適應症：第二型糖尿病」），且與 Graph 的 `TREATS` 規則式對映重複 | 需定 Vector/Graph 分工 |
| 三條交互作用邊抽錯 | `ACEIs / 利尿劑 / NSAIDs --INDUCES--> 急性腎損傷` 應為 `INTERACTS_WITH` | 已加抽取規則 6，待重抽 |
| 日期格式不符契約 | 兩條管道皆輸出 `2016/7/14`，契約要求 `YYYY-MM-DD` | M2 前在管道端正規化 |
| 抽查 margin 偏薄 | 相關 0.85 vs 不相關 0.77，僅差 0.08 | 佐證 similarity 只能粗篩；擴大樣本改看分布重疊 |

---

## 三、與 LLM 組對齊：三件被改變的事

LLM 組《LLM組與RAG組對齊問題》帶來的不只是欄位討論，有三件事實質改變了我們的規劃。

### 3.1 ⚠️ 共用範例查詢不會進入 RAG（最需要處理）

Kickoff 第 3.2 節定案：全組所有文件、圖表、範例都用「我有腎功能不好，可以吃 metformin 嗎？」，理由是它同時涉及藥物、病症與安全性。**七份 M1 文件全部照做了。**

但 LLM 組的分工是：只有 `router_status = G_GENERAL_EDUCATION` 能呼叫 RAG，而他們的共同測試案例明確把這句話歸類為：

> **測試一（安全攔截）**：「我腎功能不好，可以吃 metformin 嗎？」
> 預期：屬於個人化用藥問題，不直接替使用者決定，建議詢問醫師或藥師。

也就是說，**這句話會在 Policy Gate 就被攔下，永遠不會到達我們的 Retriever**。我們整組的 worked example 是一個 RAG 拿不到的查詢。

LLM 組同時提供了 RAG 可達版本：

> **測試二（一般資料搜尋）**：「腎功能不佳者使用 metformin 時，有哪些一般注意事項？」

**建議處理方式（非重寫，而是分層）**：兩句都留，並明確標示各自示範的是什麼——

- **測試一** 用來示範 Policy Gate 的安全攔截，說明為何個人化用藥不進 RAG。這對提案是加分，證明兩層設計有效。
- **測試二** 作為 **RAG 章節的正式 worked example**，所有檢索路徑、chunk 範例、圖表改用它。

值得注意的是：**測試二正好落在 `CAUTION_FOR` 這一層**。「一般注意事項」對應的就是「eGFR 30–45 需減量」這類敘述，而不是「eGFR<30 絕對禁忌」。schema v3 當初把 `CAUTION_FOR` 獨立成邊（Gap 1），現在看是對的——若當時把兩者合成一條禁忌邊，測試二根本無法正確回答。這點可以在提案裡直接當作設計決策的驗證。

**影響範圍**：七份文件的範例段落、Boundary B 第 5 節示意圖、Multi-RAG A 第 1 類、Preprocessing 兩條管道的展示案例。**這是 M2 撰寫前必須先定案的一件事**，否則會寫出一整份用錯範例的提案。

### 3.2 ✅ 高風險事實的攔截責任已有人認領

M1 校對時我們判定這是責任真空——Preprocessing 只負責正確抽取、Boundary 只做結構性篩選、LLM Judge 只判 relevant/sufficient/conflict，沒有人負責「偵測到禁忌後讓 Generator 拒答或改口」。

LLM 組對齊文件第 7 節已明確分工：

- **RAG**：正確標記禁忌、警告或嚴重副作用，並附上來源
- **Context Gate**：判斷資料是否可信、足夠、適合使用
- **Generator**：只做一般說明，不替個人決定停藥、換藥或調整劑量
- **Output Gate**：最後檢查回答，需要時轉介醫師或藥師

→ **此項可從待決清單移除。** 我們的責任範圍因此收斂為「標記正確 + 附來源」，這正是 schema v3 已經在做的事。

### 3.3 ✅ Boundary B 的三項 TBD 已獲回覆

| Boundary B 原 TBD | LLM 組回覆 |
|---|---|
| 跳數用盡時回傳空結果或部分路徑？ | **回傳部分路徑**，標記 `graph_path_status = PARTIAL`，交 Context Gate 判斷；完全無有效 evidence 才回空並進 Fallback。部分路徑不得因「有資料」就視為 Correct |
| Relation Type / `score_type` / `status` 是否合併維護？ | **不合併**（三者語意與責任層不同），但須放入**同一份版本化的 Shared Schema / Registry**，各模組讀自己負責的區段，避免各自 hard-code |
| `graph_traversal` 分數是否沿用 CRAG 閾值？ | **不得沿用** τ+=0.50 / τ−=-0.91。Graph 需建立自己的校準資料，至少涵蓋完整路徑 / 部分路徑 / 錯誤路徑 / Hop Limit 終止 / 錯誤實體 / 無關 Relation 六種，再進入六類 End-to-End 測試 |

→ Boundary B 的 M1「已與 LLM 組簡短確認」要求因此達成。但衍生兩項新工作：**`graph_path_status` 欄位要加進契約**，以及**Graph 專屬的六類校準資料集要建**（M2）。

### 3.4 LLM 組提出、我們需要回答的九項

| # | LLM 組的問題 | 我方目前可回答的內容 | 狀態 |
|---|---|---|---|
| 1 | RAG 能否接收 LLM 提出的欄位？還缺什麼？ | 可接收。`user_raw_input` + `retrieval_queries` 足夠；`guardrail_result` 的四個子欄位我們只讀不改 | ✅ 可答 |
| 2 | 哪些 LLM 標籤會影響 Vector / Graph / Hybrid？ | 見下方 3.5 對映表 | ✅ 可答 |
| 3 | Chunk 欄位是否定案，並同意補外層請求與搜尋狀態？ | 欄位已定案（校對版契約）。**但目前契約只有 chunk 層、沒有外層封套**，需新增 | 🟡 需補 |
| 4 | RAG 是否回傳 `evidence_risk_level` / 安全訊號類型 / 判定依據？ | **可以，且是推導而非猜測**。見下方 3.6 對映表 | ✅ 可答（附條件） |
| 5 | Boundary B 三項是否共同定案？ | LLM 組已回覆，我方接受，需把 `graph_path_status` 納入契約 | ✅ 可答 |
| 6 | `SUCCESS/EMPTY/PARTIAL/STALE/CONFLICT/ERROR` 是否可用？ | 可用，屬外層封套欄位（同第 3 項） | ✅ 可答 |
| 7 | 問題改寫、重搜與停止由誰負責？ | 接受其建議分工（LLM 保留原意改寫、RAG 處理同義詞與路由、最多重搜一次） | ✅ 可答 |
| 8 | 發現禁忌或嚴重警告後各階段做什麼？ | 已由 3.2 定案 | ✅ 已定案 |
| 9 | Direct / Indirect Prompt Injection 分工？ | 見下方 3.7 | 🟡 部分可答 |

### 3.5 LLM 標籤 → RAG 路由的對映（回答問題 2）

| LLM 標籤 | 是否影響檢索 | 如何影響 |
|---|---|---|
| `intent_tags` | **是，主要依據** | `GENERAL_MEDICATION_INFORMATION` → 兩者皆用（Graph 查禁忌/交互作用，Vector 補衛教）；`GENERAL_EDUCATION` → Vector 優先；`SYMPTOM_INFORMATION` → 兩者皆用（Graph 查 `CAUSES_SIDE_EFFECT` / `TRIGGERS`）。對映見 Multi-RAG A 分類表 |
| `context_modifiers.polarity` | **是** | `NEGATIVE` 需避免把「沒有腎功能問題」誤觸發禁忌檢索，直接影響 Graph 起點實體的選擇 |
| `context_modifiers.target_subject` | 是（弱） | `FAMILY_OR_CAREGIVER` 對應 Multi-RAG A 第 7 類，Vector 側偏好家屬照護指引；不改變 Graph 路徑 |
| `context_modifiers.language` | 是 | 決定回傳內容語言與來源偏好（TFDA 中文 vs openFDA 英文） |
| `context_modifiers.time_frame` | 否（目前） | `PAST` / `HYPOTHETICAL` 目前不改變檢索策略，僅供 Generator 措辭參考 |
| `risk_flags` | 否 | 屬 Policy Gate 職責；RAG 不依此改變檢索，但 `HIGH_RISK_NOT_EXCLUDED` 的請求原則上不會到 RAG |
| `router_status` | 前置條件 | 只有 `G_GENERAL_EDUCATION` 會進來，RAG 不修改此值 |

新增 RAG 側欄位 `retrieval_route`（`VECTOR` / `GRAPH` / `HYBRID`），與 LLM 標籤明確分離，符合 LLM 組建議。

### 3.6 `evidence_risk_level` 的推導規則（回答問題 4）

LLM 組要求「風險等級由 RAG schema 統一提供，不要讓 LLM 看一段文字自行猜測」。**schema v3 的每一條邊本來就帶安全等級**，因此這是查表而非判斷：

| relation | `safety_signal_types` | `evidence_risk_level` |
|---|---|---|
| `CONTRAINDICATED_FOR` | `CONTRAINDICATION` | **HIGH** |
| `INDUCES` | `SERIOUS_ADVERSE_EVENT` | **HIGH** |
| `RISK_FACTOR_FOR` | `RISK_FACTOR` | HIGH（涉及禁忌路徑時）／MEDIUM |
| `TRIGGERS` | `TRIGGER` | HIGH（常與禁忌情境共同出現） |
| `CAUTION_FOR` | `CAUTION` | **MEDIUM**（明確低於禁忌，不得升級） |
| `INTERACTS_WITH` | `INTERACTION` | MEDIUM |
| `REQUIRES_MONITORING` | `MONITORING` | MEDIUM |
| `CAUSES_SIDE_EFFECT` | `SIDE_EFFECT` | **LOW** |
| `TREATS` / `IS_A` | `GENERAL` | LOW |
| Vector-only chunk（無 relation） | — | **UNKNOWN** |

`risk_basis` 直接由既有欄位組成：`relation` + `source` + `condition` + 原文段落。

> **需向 PM 提出的限制**：LLM 組要求風險等級「經醫療專業人員確認」。本組目前**沒有臨床專業人員參與**，上表是依仿單／公告的用語強度（黑框警語、禁忌、不建議、不良反應）推導，非臨床審核結果。這是提案中應誠實標註的限制，也是需要 PM 協助的資源事項。

### 3.7 Indirect Prompt Injection（回答問題 9 的五個子問題）

| LLM 組的提問 | 我方現況 |
|---|---|
| 1. 入庫前是否有來源白名單／惡意指令掃描／人工複核？ | **來源白名單：有**（Boundary A 的 Source Boundary，目前僅 TFDA 與 openFDA 官方 JSON）。**惡意指令掃描與人工複核：無** |
| 2. Chunk 找回後是否再檢查間接注入？ | **無。** Boundary C 已將此列為第⑥類殘餘風險 |
| 3. Vector 原文與 Graph label 是否用不同檢查方法？ | **應該要，但尚未設計**（Boundary C TBD 4）。附帶一提，Graph 側的 `MAX_LABEL_LEN` 檢查已間接擋掉「整段文字被當成節點」這種放大注入的形式 |
| 4. 發現疑似注入時如何回報？ | 契約目前**沒有 `warnings` 欄位**，需新增（與外層封套一併處理） |
| 5. 是否共用惡意樣本庫與 Threshold？ | 在來源白名單維持現狀（僅官方來源）的前提下，RAG 側暫不需要自建樣本庫。**建議由 LLM 組主責維護**，RAG 於來源放寬時再接入 |

**我方立場**：接受 LLM 組建議的原則——RAG 負責來源控制與檢索內容的間接注入檢查，Context Gate 一律視 Retrieved Context 為不可信資料再檢一次。但需誠實說明：目前只有「來源控制」這一半做到了，「檢索內容的注入檢查」在 M1 沒有實作。

---

## 四、契約缺口彙整（M2 開工前要補的欄位）

把上面散落各處的契約需求集中如下。這些都需要 Multi-RAG B 成員納入正式契約：

### 4.1 需新增「外層封套」（目前完全沒有）

現行契約只定義了單一 Chunk 的形狀，沒有定義「一次檢索請求回傳什麼」。LLM 組要的搜尋狀態、請求對應都無處可放。

```
RetrievalResponse（新增）
├── request_id            對應 LLM 的 request_id
├── schema_version
├── retrieval_route       VECTOR / GRAPH / HYBRID（RAG 自主決定）
├── retrieval_status      SUCCESS / EMPTY / PARTIAL / STALE / CONFLICT / ERROR
├── graph_path_status     COMPLETE / PARTIAL（跳數用盡時說明停在哪）
├── rerun_suggested       布林，RAG 建議重搜（是否重搜由 LLM workflow 決定）
├── warnings[]            疑似注入、人工複核待決等
└── chunks[]              現行 RetrievedChunkIntegrated 陣列
```

### 4.2 Chunk 層需補的欄位

| 欄位 | 來源 | 說明 |
|---|---|---|
| `evidence_risk_level` | LLM 組要求 | HIGH / MEDIUM / LOW / UNKNOWN，依 3.6 表推導 |
| `safety_signal_types[]` | LLM 組要求 | 依 3.6 表推導 |
| `risk_basis` | LLM 組要求 | relation + source + condition + 段落 |
| `source_date` / `status` 在三元組層 | Boundary A | 目前只有 chunk 層有；Graph 三元組本身沒有版本狀態 |

### 4.3 已在校對版補上的（不需重複討論）

`condition`、`effect`、`confidence`、`negation_checked`、`additional_sources`、`entities` 物件化、`object_type` 補 `Symptom` / `Intervention`。

---

## 五、計畫修訂與 M2 工作分派

### 5.1 原計畫 vs 修訂

Kickoff 原本假設 M1 之後三個團隊各自平行撰寫 M2 章節即可。實際情況是 M1 校對翻出跨組不一致、LLM 組又提出新的介面需求，因此 **M2 開始前需要一段「共同定案期」**，否則三個團隊會各自根據不同版本的假設寫下去。

修訂後的時程：

| 日期 | 工作 | 負責 |
|---|---|---|
| **8/24（今日）** | M1 內部會議：確認共用範例查詢的處理方式、關閉可關的 TBD、指派兩項缺件 | 全體 |
| **8/25** | 契約定案（外層封套 + 風險等級欄位）；兩項缺件補齊 | Multi-RAG B、Preprocessing A |
| **8/26** | **PM 會議**（提交《提案報告書》）；同日回覆 LLM 組九項確認 | 全體 |
| **8/27–8/28** | 各團隊撰寫 M2 章節，統一整合 | 全體 |

### 5.2 M2 前的工作清單（依負責人）

**必須完成（缺了 M2 寫不下去）**

| # | 工作 | 負責 | 期限 |
|---|---|---|---|
| 1 | **定案共用範例查詢的分層處理**（測試一示範攔截、測試二作為 RAG worked example），並通知全體更新範例段落 | 全體，會議決議 | 8/24 |
| 2 | **撰寫合併策略**：排序依據（三種 score_type 不可直接比較的前提下）、去重規則、截斷位置與上限 | Multi-RAG B | 8/25 |
| 3 | **契約加上外層封套**（`retrieval_status`、`graph_path_status`、`retrieval_route`、`warnings`、`rerun_suggested`） | Multi-RAG B | 8/25 |
| 4 | **契約加上風險等級三欄**，推導規則採用 3.6 對映表 | Multi-RAG B + Preprocessing B | 8/25 |
| 5 | **補寫 `decision_record_v1.md` / `candidates_v2.md`**（embedding 模型選型、5 篇選定理由、策略 B 勝出理由） | Preprocessing A | 8/25 |
| 6 | **取得 metformin 單方來源**（TFDA 或糖尿病學會），補上旗艦查詢的乾淨資料 | Preprocessing B（需全體協助找） | 8/26 |

**應完成（影響提案品質）**

| # | 工作 | 負責 |
|---|---|---|
| 7 | 修 Vector 切分的 URL bug，重新 embed 受影響 chunk | Preprocessing A |
| 8 | 重抽三條交互作用邊（`INDUCES` → `INTERACTS_WITH`） | Preprocessing B |
| 9 | 兩條管道的日期正規化為 `YYYY-MM-DD` | Preprocessing A + B |
| 10 | 定案 `藥品成分` / `適應症` 兩欄由 Vector 或 Graph 負責，消除重工 | Preprocessing A + B |
| 11 | 統一 `chunk_id` 命名格式 | Multi-RAG B 主持 |
| 12 | 節點／關係 allow-list 依校對版更新，並與 Multi-RAG A 的 10 類查詢交叉驗證 | Boundary B |
| 13 | 三元組層加 `source_date` / `status`，或確認由 Chunk 層統一帶 | 會議定案後執行 |

**M2 之後（誠實標為未完成，不要假裝做完）**

| # | 工作 | 說明 |
|---|---|---|
| 14 | Entity resolution 與 UMLS 詞彙接地 | 需 UMLS UTS 帳號 |
| 15 | Graph traversal 六類校準資料集 | LLM 組要求，工作量不小 |
| 16 | 129 筆全量抽取（目前僅試抽 4 篇 + 規則式對映） | |
| 17 | 檢索內容的間接注入檢查 | 目前僅有來源控制 |
| 18 | 數值門檻結構化（`condition` 目前仍是字串） | |
| 19 | 風險等級的臨床專業審核 | **需 PM 協助取得資源** |

### 5.3 需要 PM 支援的三件事

1. **臨床專業人員審核**：風險等級對映與高風險事實的正確性，目前無人可審。這是醫療衛教應用最實質的風險。
2. **UMLS UTS 帳號**：詞彙接地卡在這裡，影響 entity resolution 與 Boundary 的 anchor 控制。
3. **本地權威資料來源**：TFDA 129 筆不含 metformin，目前靠美國 openFDA 頂著。需要 TFDA 或中華民國糖尿病學會的用藥指引管道。

---

## 六、風險登記

| 風險 | 影響 | 目前狀態 | 緩解 |
|---|---|---|---|
| **旗艦查詢無乾淨資料來源** | 提案的核心展示案例無法完整跑通 | 已量化（可檢索集為空） | 第 5.2 節第 6 項；退路是改用有完整資料的 SGLT2 案例展示 |
| **共用範例查詢不進 RAG** | 七份文件的範例需重新定位 | 已發現，未定案 | 第 3.1 節分層處理方案 |
| **無臨床審核** | 風險等級可能有誤，且無人背書 | 未緩解 | 需 PM 協助；提案中誠實標註 |
| **詞彙未接地** | entity resolution 失效，同名實體無法合併 | 已知，M2 處理 | 需 UMLS 帳號 |
| 兩條管道對同一欄位重工 | 候選池噪音、維護成本 | 已發現 | 第 5.2 節第 10 項 |
| 抽查樣本過小 | 0.85/0.77 不足以支撐門檻設定 | 已發現 | 擴大樣本，改看分布重疊 |

---

## 七、對 M1 的整體判斷

以 Kickoff 設定的 M1 標準（「解除跨組依賴」而非「寫完細節」）來衡量，這一輪是達標的：

- 三個團隊之間的介面已經具體到可以拿出程式碼與資料對照，而不是停留在名詞層次；
- 兩條 Preprocessing 管道都有真實資料的產出，且彼此對齊到可以逐篇比對同一份原文；
- 與 LLM 組的分工從「兩邊都以為對方會做」收斂到有明確歸屬。

真正的弱點不在設計，在**資料**：旗艦查詢缺乏本地權威來源、詞彙未接地、沒有臨床審核。這三項都不是多寫幾頁文件能解決的，需要在 M2 誠實呈現，並在 PM 會議上提出資源需求。

---

*本報告涵蓋至 2026/08/24 的 M1 產出。技術細節以各團隊文件與 `MS1/revised/` 校對版為準；
給 PM 的對外版本見《RAG 組 — 提案報告書（M1 成果與 M2 提案）》。*
