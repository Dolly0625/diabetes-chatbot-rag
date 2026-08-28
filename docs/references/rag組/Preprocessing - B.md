# Preprocessing 團隊 B 成員 — Graph 管道規劃（M1 目標：8/24）

> 對象：RAG 組 Preprocessing 團隊 B 成員（Erich）
> 依據：《RAG 組 — 提案計劃書 Kickoff_v1》第 4 節「Preprocessing 預處理團隊」B 成員任務
> 參考：DCSS（失智症照顧者支援系統）knowledge graph 專案的方法論（非內容，領域不同）
> 共用查詢（全組統一）：「我有腎功能不好，可以吃 metformin 嗎？」
> **v2 更新**：已用真實文件（TFDA 129 筆資料集 + openFDA metformin/canagliflozin/dapagliflozin 仿單）手動試抽，schema 依試抽結果修訂一輪。

---

## 1. 任務範圍（來自 Kickoff 文件）

- **任務**：選定實體/關係抽取方式（LLM 輔助 vs 規則）、schema 或 ontology 來源（SNOMED CT? 自訂?）
- **產出**：抽取工作流圖 + schema 草案 + 3–5 篇範例文件試抽結果
- **M1（8/24）前完成度**：方向定案 + 小規模試做（不用全量處理）
- 與 A 成員（Vector 管道）共用同一批資料來源起步：TFDA 129 筆藥品安全資訊 + 糖尿病學會指引
- 與 Multi-RAG B 成員的 Retrieved Chunk 契約（第 3.3 節）對接：Graph Retriever 產出的 chunk 需帶 `entities` / `relations` 欄位

## 2. 從 DCSS 專案借用什麼、不借用什麼

DCSS 是失智症領域的 knowledge graph 專案，**領域完全不同**（不能照抄節點/邊的內容），但它的**方法論**已經被驗證過一輪，值得直接沿用：

| 從 DCSS 借用的方法論 | 說明 |
|---|---|
| **Schema-first，型別由程式碼強制** | 用 Pydantic 定義 Triple 模型，`(subject_type, relation, object_type)` 的合法組合寫死在程式裡，擷取當下就拒絕不合法的組合。 |
| **禁忌關係獨立建模，不用否定詞** | `CONTRAINDICATED_FOR` 必須是獨立的正向邊，不能是被否定的 `TREATS`——漏掉一個否定詞，語意就整個反轉。 |
| **不要從零造 ontology，先錨定既有詞彙** | 用 RxNorm / SNOMED CT / LOINC 等外部詞彙當骨架，找不到才自建節點。 |
| **來源可追溯性（provenance）＝鐵律** | 每個三元組都要帶 `source + 段落 + 理由`，呼應 Kickoff 文件第 6 節「來源引用」。 |
| **domain/range 可以中途修訂，但要留決策紀錄** | DCSS 曾把 `CONTRAINDICATED_FOR` 的 range 從「只能是 Symptom」擴大成「Symptom 或 Cause」。我們的 schema 也在試抽後修訂了一輪（見第 4 節），理由都記錄下來。 |
| **節點型別維持通用，語意角色由邊承擔** | DCSS 沒有幫「藥物造成的結果」另開節點型別，而是靠邊的方向/型別表達角色。我們試抽後遇到同樣的情況（見第 4 節 Gap 2），採用同一原則解決，而不是讓節點型別暴增。 |
| **Bronze/Silver/Gold 分層查核** | 4 天內用不到完整三層，M1 階段只求「方向對、格式對」，不要求已查核。 |

## 3. 試抽用到的真實文件（已下載/已確認可用）

M1 前不要憑空設計 schema——已經找到並試抽了以下真實文件，結論寫在第 4 節：

| 文件 | 來源 | 狀態 |
|---|---|---|
| TFDA 129 筆藥品安全資訊（`tfda_dataset_129`） | 隊上已有（LLM 組提供），已確認結構：129 筆 TFDA 風險溝通公告，欄位為 發布日期/藥品成分/適應症/藥理作用機轉/訊息緣由/藥品安全有關資訊分析及描述/TFDA風險溝通說明。**注意：不含 metformin**，但有 16 筆糖尿病相關公告可用。 | ✅ 已試抽（canagliflozin/dapagliflozin 急性腎損傷公告） |
| openFDA Drug Label API（`api.fda.gov/drug/label.json`） | 美國 FDA 公開 API，免金鑰。可用 `openfda.generic_name` 查任何藥物，回傳結構化欄位：`contraindications`、`warnings_and_cautions`、`drug_interactions`、`dosage_and_administration`、`adverse_reactions` 等。 | ✅ 已試抽（metformin/ZITUVIMET 複方仿單） |
| 糖尿病學會指引（ADA 或中華民國糖尿病學會） | 待下載，用於補充「劑量調整」類的灰階敘述（非絕對禁忌） | 待辦 |

> **待群組同步**：openFDA 是美國仿單，不是 TFDA 資料。臨床內容（eGFR 門檻等）跨法規機關通常一致，但若要滿足「每項技術主張皆附可查來源」的規範，簡報時要註明「美國 FDA 仿單，交叉引用」，不要包裝成 TFDA 資料。

## 4. Schema 草案 v2（試抽後修訂，含決策紀錄）

第一版 schema 只用一份文件手動試抽，就浮現 4 個落差；用第二份文件（canagliflozin/dapagliflozin 腎損傷公告）覆核後，修訂版沒有再冒出新落差——這是 schema 開始穩定的訊號。以下是修訂後版本，**已修訂處標記「v2」**。

### 節點型別（6 種）

| 節點型別 | 說明 | 錨定來源優先順序 | 範例 |
|---|---|---|---|
| **Substance（藥物/物質）**【v2：原名 Drug，已擴大】 | 藥品成分，**也包含酒精等非藥品但會影響藥物作用的物質** | RxNorm > ATC code > TFDA 許可證字號 | Metformin、Canagliflozin、酒精 |
| **Condition（病症/共病/藥物引發之嚴重病況）** | 疾病或病理狀態，**同時涵蓋病患既有共病，以及藥物可能引發的嚴重病況**（兩者用邊區分角色，見下） | SNOMED CT > ICD-10-CM | 第二型糖尿病、慢性腎臟病（CKD）、乳酸中毒、急性腎損傷 |
| **Symptom（症狀）** | 病患自述或可觀察、通常較輕微的症狀/副作用 | SNOMED CT > HPO | 腹瀉、頭痛、上呼吸道感染 |
| **LabParameter（檢驗指標/風險門檻）** | 需要監測或設下安全閾值的數值型指標 | LOINC | eGFR、HbA1c、血肌酸酐 |
| **Trigger（誘發因子）**【v3 新增】 | **外在/情境性**的誘發因子，與 `Condition` 的內在/生理性狀態相對——直接借用 DCSS 的 Trigger vs Cause 區分 | DCSS-native（自訂，沿用 DCSS 做法：目前無合適公開詞彙） | 減少進食/水分攝取、自行減少胰島素劑量、急症誘因（感染、外傷） |
| **Intervention（介入措施，選用）** | 非藥物衛教建議 | MeSH > 自訂 | 飲食控制、定期監測血糖 |

**決策紀錄（Gap 5 → 新增 Trigger 節點型別，v3）**：試抽第三份文件（SGLT2 抑制劑酮酸中毒公告）時，原文本身用「誘發」與「引起」兩種不同動詞，分別對應外在/情境性因素（減少進食、自行減藥）與內在/生理性狀態（脫水、急性腎衰竭）——這正是 DCSS 的 Trigger／Cause 區分，且是原文自己給出的線索，不是我們自創的分類。決定現在就加，而不是拖到 M2：這個區分會反覆出現在糖尿病安全文獻中，越晚加入，需要回頭重新標記的既有三元組就越多；現在資料量還小，改的成本最低。

**決策紀錄（Gap 2 → 為何不新增 AdverseEvent 節點型別）**：試抽 metformin 仿單時，「乳酸中毒」既是藥物引發的嚴重後果，理論上也可能是別的藥物的禁忌條件。如果把它塞進一個新的 `AdverseEvent` 節點型別，就無法在圖譜中把它同時當作「這個藥造成的後果」與「另一個藥的禁忌條件」——會被迫複製兩份。改用 DCSS 的原則：節點型別維持通用（`Condition`），角色差異由**邊**表達（`INDUCES` vs 一般的共病/禁忌邊），這樣同一個節點可以在圖譜的不同路徑上扮演不同角色，也是 Graph RAG 多跳查詢真正需要的能力。

**決策紀錄（Gap 4 → 藥物節點以成分為單位，不建品牌/複方節點）**：ZITUVIMET 是 sitagliptin + metformin 複方，若把 Drug 節點設為品牌名，會跟使用者「問 metformin」的問法對不起來，且品牌/複方組合會讓節點數量不必要地暴增。決定：**Substance 節點一律以單一活性成分為單位**；複方仿單只當來源文件，抽取時把段落歸到個別成分，不建立複方節點。

### 邊型別（10 種，較 v1 新增 4 種）

| 邊 | 方向 | 語意 | 安全等級 | 備註 |
|---|---|---|---|---|
| **CONTRAINDICATED_FOR** | Substance → Condition 或 LabParameter | 絕對禁用 | **高風險，需人工複核** | |
| **CAUTION_FOR**【v2 新增】 | Substance → Condition 或 LabParameter | 相對限制／需調整劑量／官方用語為「不建議」而非「禁忌」 | 中高風險 | **決策紀錄（Gap 1）**：metformin 仿單裡「eGFR 30–45 不建議」與「eGFR <30 禁忌」是兩種不同強度的敘述。若都塞進 `CONTRAINDICATED_FOR`，等於把「不建議」的原文悄悄升級成「絕對禁止」——這本身就是一種抽取錯誤。獨立成邊而非欄位，是因為 Boundary B 成員的限制機制設計本來就是用「關係類型排除」在做過濾，獨立的邊型別讓他們可以直接按型別篩選，不必逐筆檢查欄位。 |
| **INDUCES**【v2 新增】 | Substance → Condition | 藥物可能引發的**嚴重**病況（仿單黑框警語等級） | 高風險 | **決策紀錄（Gap 2）**：與 `CAUSES_SIDE_EFFECT`（→ Symptom）分開，因為兩者風險等級與下游處理方式不同；乳酸中毒、急性腎損傷等用這條邊，一般腸胃不適用下面那條。 |
| **CAUSES_SIDE_EFFECT** | Substance → Symptom | 一般副作用（仿單「不良反應」清單等級） | 中風險 | |
| **REQUIRES_MONITORING** | Substance → LabParameter | 使用前後需監測此指標 | 中風險 | |
| **TREATS** | Substance → Condition | 用於治療此病症（適應症） | 一般 | |
| **INTERACTS_WITH** | Substance → Substance | 交互作用；邊上可附 `effect` 註記說明結果（如「增加乳酸中毒風險」） | 中風險 | |
| **RISK_FACTOR_FOR** | Condition → Condition | 某病症提高另一病症風險 | 高風險（涉及禁忌時） | 見下方抽取原則——不要為了「條件式風險」硬造三元關係 |
| **TRIGGERS**【v3 新增】 | Trigger → Condition | 外在/情境性因素誘發某病況（如減少進食誘發酮酸中毒） | 高風險（常與禁忌情境共同出現） | 與 `RISK_FACTOR_FOR`（Condition→Condition）平行但主詞型別不同；仿 DCSS 的 TRIGGERS/CAUSED_BY 拆分 |
| **IS_A**【v3 新增】 | Substance → Substance | 同型別內的階層關係（成分 ⊂ 藥物類別），如 Canagliflozin IS_A SGLT2Inhibitor | 一般（非安全關鍵，但影響查詢完整性） | 仿 DCSS：同型別階層用類似 `rdfs:subClassOf` 的關係，不算在安全相關的 6+2 種邊之內，但仍需程式檢查 domain/range 相同型別 |

**決策紀錄（Gap 6 → 新增 IS_A 階層邊，v3）**：SGLT2 抑制劑酮酸中毒公告是對整個藥物類別發出的警訊，不是對單一成分。若只建一個孤立的「SGLT2 抑制劑（類別）」節點，之後有人問「canagliflozin 會不會有酮酸中毒風險」時，這條類別層級的事實會因為查詢詞是成分名而找不到——這是安全性完整度的問題，不只是資料整潔度問題。選擇加 `IS_A` 結構性階層邊（而非在抽取時把同一事實複製貼到每個已知成分），是因為階層邊對「未來新上市的同類別藥物」自動生效，複製貼上的做法則每次都要手動維護成分清單，未來漏掉新藥就會重演同樣的安全漏洞。

**測試查詢對應**：「腎功能不好可以吃 metformin 嗎」→
`Metformin --CONTRAINDICATED_FOR--> LabParameter(eGFR, condition:"<30")`、
`Metformin --CAUTION_FOR--> LabParameter(eGFR, condition:"30–45，需減量")`、
`Metformin --INDUCES--> Condition(乳酸中毒)`、
`Condition(腎功能不全) --RISK_FACTOR_FOR--> Condition(乳酸中毒)`。
四條facts 各自獨立、互不依賴，讓 Graph Retriever 一次撈出「能不能吃、為什麼危險、危險程度」三層資訊，而不是被壓縮成一個過度簡化的是/否答案。

### 抽取原則（試抽後歸納，寫進 LLM prompt 的規則）

1. **否定詞/條件完整保留**（沿用 DCSS）：原文的「不建議」「除非」「僅在」不可被抽成無條件的絕對敘述。
2. **不要為條件式/三方風險關係造新邊**：例如「腎功能不全患者使用此藥時，急性腎損傷風險較高」不需要一條「Substance+Condition→Condition」的三元邊，拆成兩條獨立事實（`Substance --INDUCES--> Condition` 與 `Condition --RISK_FACTOR_FOR--> Condition`）即可，讓 Graph RAG 用多跳查詢在檢索時組合，不要在抽取階段就硬綁。
3. **區分「臨床主張」與「案例敘述/行政程序」**：安全訊息公告裡常夾雜通報案例的人口學細節（如「部分案例小於 65 歲」）或行政動作（「FDA 已修訂仿單」），這些不是可泛化的臨床事實，不應抽成三元組，只留在來源全文供人工參考。
4. **區分 Trigger（外在/情境性）與 Condition（內在/生理性）**：原文常見「誘發」（→ Trigger）與「引起」（→ 內在成因，仍歸 Condition）兩種動詞，抽取時依動詞語意分流，不要一律歸進 Condition。
5. **不硬套邊型別**：若一句話是真實的臨床建議，但套不進現有任何一種邊而不失真，寧可先不抽成三元組、留在來源全文供人工參考，也不要為了「有抽到」而勉強套用不合適的邊型別。

## 5. 抽取方式：以 LLM 輔助為主，欄位對映為輔（非原先設想的規則式為主）

實際打開 TFDA 129 筆資料集後發現：**它不是結構化仿單，而是敘事性的風險溝通公告**（訊息緣由、風險分析都是完整段落，沒有「禁忌」「交互作用」這種可比對的標題）。這推翻了「TFDA 用規則式、指引用 LLM」的原始假設。修正後的分工：

| 內容類型 | 抽取方式 | 理由 |
|---|---|---|
| 129 筆公告中的 `藥品成分` + `適應症` 欄位 | **簡單欄位對映**（非 LLM）：`藥品成分 --TREATS--> 適應症` | 這兩個欄位本身就短、乾淨，直接映射即可，不需要 LLM |
| 129 筆公告中的 `訊息緣由` / `藥品安全有關資訊分析及描述`（敘事段落） | **LLM 輔助**，輸出強制符合 Pydantic Triple schema，並套用上方三條抽取原則 | 段落複雜、常有條件句與案例敘述夾雜，需要 LLM 判讀語意但由 schema 擋掉不合法組合 |
| openFDA 仿单的 `contraindications` / `warnings_and_cautions` 等欄位 | **LLM 輔助**（欄位本身仍是完整句子/段落，不是條列） | 同上；但可用欄位名稱本身當作抽取線索（例如落在 `contraindications` 欄位的句子預設抽成 `CONTRAINDICATED_FOR` 或 `CAUTION_FOR`，由句意判斷是哪一種強度） |

### 抽取工作流

```
[TFDA 129 筆 JSON] ──欄位對映──► [藥品成分→適應症 TREATS 事實]
        │
        └─敘事段落──► [LLM 抽取器]
[openFDA 仿單 JSON] ──分段送入──►      │  (prompt: schema 定義
                                        │   + 3 條抽取原則
                                        │   + few-shot：1 禁忌/1 caution/1 一般適應症)
                                        ▼
                          [Pydantic Triple 驗證]
                        （型別組合不合法 → 拒絕重試）
                                        │
                                        ▼
                          [否定詞/強度人工抽查]
                        （M1 階段：抽樣，非全查）
                                        │
                                        ▼
                [Bronze 三元組] → entities/relations
                  供 Retrieved Chunk 契約使用
```

## 6. Retrieved Chunk 契約：`entities` / `relations` 欄位草案（v2）

```json
{
  "entities": [
    {"id": "ent_1", "type": "Substance", "label": "Metformin", "code": "RxNorm:6809"},
    {"id": "ent_2", "type": "LabParameter", "label": "eGFR", "code": "LOINC:48642-3"},
    {"id": "ent_3", "type": "Condition", "label": "乳酸中毒", "code": "SNOMED:..."}
  ],
  "relations": [
    {
      "subject": "ent_1", "relation": "CONTRAINDICATED_FOR", "object": "ent_2",
      "condition": "eGFR < 30 mL/min/1.73m²", "confidence": 0.92, "negation_checked": true
    },
    {
      "subject": "ent_1", "relation": "INDUCES", "object": "ent_3",
      "confidence": 0.9, "negation_checked": true
    }
  ]
}
```

- `condition` 欄位容納數值門檻式禁忌。
- `relation` 現在有 10 種合法值（見第 4 節），需請 Multi-RAG B 成員把這份清單納入 Contract Gate 的檢查項目。

## 7. 3–5 篇範例文件（已確認可用）

1. ✅ openFDA metformin（ZITUVIMET）仿單 — 已試抽，對應共用查詢；驗證出 Gap 1/2/4
2. ✅ TFDA 129 筆中 canagliflozin/dapagliflozin 急性腎損傷公告（2016/7/14）— 已試抽，schema v2 無新落差
3. ✅ TFDA 129 筆中 SGLT2 抑制劑酮酸中毒公告（2015/6/25）— 已試抽，驗證出 Gap 5（Trigger）與 Gap 6（藥物類別階層）→ schema v3
4. ✅ TFDA 129 筆中 Insulin 皮膚澱粉樣變性症公告（2020/11/16）— 已試抽，**schema v3 無新落差**——收斂訊號（4 篇中 3 篇需要改 schema，第 4 篇不需要）
5.（備選，視時間決定是否還需要）糖尿病學會指引中腎功能分級劑量調整段落——CAUTION_FOR 的分級案例已在文件 1（metformin 仿單）測試過，此篇非必要

**Schema v3 已視為穩定，可進入下一步（抽取工具開發）。**

## 8. 4 天時程（8/20 → 8/24，已更新進度）

| 日期 | 工作 | 狀態 |
|---|---|---|
| **8/20** | ~~定案 schema 草案~~ → 4 篇真實文件手動試抽 → schema v1→v2→v3（6 個落差、10 種邊、6 種節點）→ 第 4 篇無新落差，視為收斂；建好 `triples.py`（Pydantic Triple 模型，domain/range 已測試）與 `extraction_prompt.md`（含 5 條抽取原則 + 5 個 few-shot） | ✅ 已完成 |
| **8/21** | 接上真正的 LLM API 跑抽取（修正 3 個原先未測試的假設，見 CLAUDE.md）；`field_mapping.py` 規則式欄位對映；**抓到並修好 3 個真實問題**：① ZITUVIMET 品牌名違反 Substance 需為活性成分的決定 → 寫 `normalize.py` 正規化 ② 欄位對映初版盲跑全部 129 筆，混進非糖尿病藥物（白血病、C肝等）→ 加範圍過濾 ③ 範圍過濾初版誤收 Apixaban（糖尿病只是它適應症裡的風險因子之一，不是治療對象）→ 加危險因子子句排除；抽取工作流圖畫完並發布為 Artifact；`graph_pipeline/` 建立獨立 git 倉庫 | ✅ 已完成（進度超前原排程，8/22–8/23 的工作提前做完） |
| **8/22–8/23（提前完成）** | ~~產出完整 Bronze 三元組資料集~~ → 已完成，`normalize.py` 合併兩條抽取路徑，最終 41 條三元組，全數在糖尿病範圍內且通過 schema 驗證；~~畫抽取工作流圖~~ → 已完成 | ✅ 已完成 |
| **8/24 前** | 把 schema v3 新增邊型別同步給 Multi-RAG B / Boundary B（群組訊息草稿見第 11 節，**尚未發送**）；已另發訊息給 LLM 組詢問「高風險事實由誰擋下」的分工（**已發送**，等回覆） | 進行中 |
| **8/24 M1 會議** | 簡報：schema v3、7 個落差/問題的決策紀錄、真實試抽與 129 筆範圍過濾結果、待決 TBD（IS_A 類別對照表、Duloxetine 邊界案例、Gate 責任歸屬） | 待辦 |

## 9. 需要的資源/工具

- LLM API 存取（與組內確認統一模型）
- ~~TFDA 129 筆藥品安全資訊原始檔~~ → 已取得（`tfda_dataset_129/`），但不含 metformin
- openFDA API（免金鑰，`api.fda.gov/drug/label.json`）→ 已驗證可用
- 糖尿病學會指引文件（ADA 或中華民國糖尿病學會）— 仍待下載，用於 CAUTION_FOR 灰階案例
- 外部詞彙表存取：RxNorm、SNOMED CT、LOINC（可能需要 UMLS UTS 帳號）
- Python + Pydantic 開發環境
- 與 A 成員的協調時間

## 10. 需要提早在群組同步的問題

- **schema v3 新增了 `CAUTION_FOR`、`INDUCES`、`TRIGGERS`、`IS_A` 四種邊，以及 `Trigger` 節點型別**——這會改變 Multi-RAG B 成員的 Contract Gate 檢查清單，以及 Boundary B 成員的「關係類型排除」設計，兩邊都要在 8/24 前看過
- TFDA 129 筆資料集**不含 metformin**——共用查詢的直接來源目前是美國 openFDA 仿單而非 TFDA 資料，簡報時需誠實標註來源機關，並詢問是否需要額外找 TFDA 或學會的 metformin 腎功能劑量指引來補強
- 「腎功能不好可以吃 metformin」的答案在真實仿單裡是**分級**的（eGFR<30 絕對禁忌／30–45 不建議），不是單純二分——已用 `CAUTION_FOR` 邊處理，需在會議上說明為何不能只用一種禁忌邊
- 與 Boundary C 成員預告：抽取階段最容易出的錯是「否定詞/強度漏抽」與「把案例敘述誤抽成通則」（見第 4 節抽取原則 3），讓他們納入測試案例分類
- **Gate 責任歸屬待確認**：Preprocessing 只負責確保 `CONTRAINDICATED_FOR`/`CAUTION_FOR`/`INDUCES` 這類安全事實被正確抽取並可被檢索到；「偵測到高風險事實後是否要讓 LLM 直接拒答/改口」是下游 Boundary 限制機制或 LLM 組 Context Gate/CRAG evaluator 的職責，需要在 8/24 會議前確認這個分工點確實有人接手，不能預設「反正圖譜裡有資料，安全性就自動達成」

## 11. 群組訊息草稿（尚未發送——待 M1 試抽工作完成後再貼）

> 用途：向 Multi-RAG B 成員與 Boundary B 成員同步 schema v3 的新增內容，讓他們在 8/24 前有時間消化。**先留底稿，不要現在就發，等 M2 前的試抽/流程圖都做完、確認不會再變動後再貼出。**

```
[Preprocessing B / Graph 管道] schema 進度同步（試抽後修訂版，非最終定案）

用 openFDA + TFDA 129 筆資料集試抽了 3 篇文件後，Graph schema 從最初的 6 種邊
擴充到 10 種，主要是因為真實仿單/公告的用詞比想像中更分層，直接抽成單一
禁忌邊會扭曲原文語氣。新增：

- CAUTION_FOR（Substance→Condition/LabParameter）：對應「不建議」「劑量需調整」
  這種比絕對禁忌弱的敘述，不再被迫塞進 CONTRAINDICATED_FOR
- INDUCES（Substance→Condition）：藥物引發的嚴重病況（黑框警語等級），
  和一般副作用（CAUSES_SIDE_EFFECT→Symptom）分開
- TRIGGERS（Trigger→Condition）+ 新節點型別 Trigger：外在/情境性誘因
  （如減少進食、自行減藥），與內在生理成因（Condition）分開
- IS_A（Substance→Substance）：藥物成分 ⊂ 藥物類別的階層關係，避免類別層級
  的安全資訊（例如「SGLT2 抑制劑類可能導致酮酸中毒」）在查詢特定成分時找不到

想請 Multi-RAG B 成員看一下這是否能放進 Contract Gate 的檢查清單，
Boundary B 成員看一下「關係類型排除」設計要不要把 CAUTION_FOR vs
CONTRAINDICATED_FOR 分開處理。另外想確認一下：偵測到高風險事實
（CONTRAINDICATED_FOR 等）之後，是由 Boundary 限制機制還是 LLM 組
Context Gate 負責擋下/改寫回答？想確認這段有人接，避免兩邊都以為
對方會做。

還有一個資料缺口：TFDA 129 筆資料集沒有 metformin（我們共用查詢的主角），
目前是用美國 openFDA 仿單頂著，缺 TFDA 或糖尿病學會對 metformin 腎功能
劑量的正式來源，有人手上有的話麻煩分享。
```

## 12. 8/21 晚間補丁（自我審查後修正，不需等其他組）

對 `graph_pipeline/` 做了一輪自我審查，修掉幾個會讓 Bronze 帶錯誤/壞資料的問題。這些都是本組能獨立完成的，已改完並重跑驗證：

- **三元組數字校正**：LLM 抽取原始 24 條 → 23 條通過驗證 → 複方（ZITUVIMET）展開 +5 → 28 條 LLM；加規則式路徑後，第一版 Bronze 是 **41** 條。此版補丁後為 **37** 條（規則式因丟棄無法拆分的 SSRI/SNRI 段落清單由 13 → 10；合併時再去重 1 條）。文件先前並存的「24 條 / 41 條」不是矛盾，是抽取→驗證→展開→合併四個階段的不同計數，特此對齊。
- **A1 實體 id 衝突**：複方展開時 Metformin 與 Sitagliptin 原本共用同一個 id，建圖會被併成一個節點——已改成每個成分帶唯一 id。
- **A4 段落節點**：tfda-risk-115（SSRI/SNRI/vortioxetine 共用適應症）原本被塞出 3 條「object 是 697 字整段」的壞節點；已在 schema 層加 `MAX_LABEL_LEN` 檢查，並讓 `field_mapping.py` 對無法逐成分拆分的清單整段不抽、改列人工複核。這不是「Duloxetine 邊界案例」，是記錄層關鍵字過濾把整批抗憂鬱藥一起放進來——粒度問題，待 M2 改成成分層過濾。
- **A5 去重**：合併時對「相同 subject/relation/object/condition」的事實去重，保留最高 confidence 一條，其餘來源收進新欄位 `additional_sources`，不丟 provenance。
- **A2 檢索閘門**：新增 `is_retrievable()` 與輸出 `bronze_triples_retrievable.json`——複方展開的高風險邊（`requires_manual_split=True` 且屬高風險）在人工確認前不進可檢索集。**重要副作用**：這會連「Metformin CONTRAINDICATED_FOR eGFR<30」這條*正確*事實一起擋掉，因為它唯一來源是 ZITUVIMET 複方仿單。這正好量化了第 10 節的資料缺口——**旗艦查詢目前沒有一條乾淨、可檢索的 metformin 腎功能禁忌事實**，非拿到單方（TFDA/學會指引）metformin 來源不可。
- **B7 詞彙代碼**：先前 82 個實體位置的 `code` 全為 null（與第 6 節契約範例不符）。已接上已核對的 `Metformin→RxNorm:6809`、`eGFR→LOINC:48642-3`，其餘留 UMLS 帳號後補；不憑印象亂填。
- **A3 抽取 prompt**：新增規則 6（併用/交互作用不可抽成 INDUCES，改 INTERACTS_WITH）與規則 7（通報案例/病史句不抽泛化事實），下次跑 LLM 時生效；現有 `ACEIs/利尿劑/NSAIDs --INDUCES--> 急性腎損傷` 三條需重抽修正。

**仍待其他組/LLM 組回應（本補丁未動）**：Gate 責任歸屬、schema v3 新增邊的 Contract Gate/關係類型排除同步、metformin 單方來源、以及 entity resolution、IS_A 類別對照表、數值門檻結構化、file→LLM 全量抽取（皆為 M2）。

---

*本文件為 Preprocessing B 成員（Graph 管道）的 M1 前規劃文件（已更新至 8/21 進度）。schema、抽取工具、
工作流圖、Bronze 資料集的初版已完成並經一輪自我審查修正（見第 12 節）；「已收斂/完成」的說法宜理解為
「M1 方向與格式已定、可展示」，entity resolution 與詞彙接地等仍屬 M2。詳細技術狀態以
`graph_pipeline/CLAUDE.md` 為準（更新頻率較高）。*
