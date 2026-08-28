# 糖尿病衛教 Chatbot 查詢類型分類表 (Multi-RAG Routing Taxonomy)

> **修訂說明（2026/08/23，Preprocessing B 成員 Erich 校對）**：本檔為原 `MS1/Multi-RAG - A.md` 的校對版，**僅修正與 Graph 側現況對不上的事實面內容**，分類架構、多軸標籤、路徑判斷與參考來源皆未更動。本表的關係型別名稱與方向**全部正確**，是目前全組術語最一致的一份，無須修改。修訂處為第 2 類（關係型別應為 `INTERACTS_WITH`）、第 9 類（Graph 側目前無妊娠相關資料）、以及表格下方新增一則對 Boundary 組的相依性提醒。原作者請自行 diff 後決定採用範圍。

### 分類邏輯：Intent × Risk × Context 多軸架構

| 類型名稱 | 多軸標籤 (Intent × Risk × Context) | 範例問題 | 建議路徑 | 判定理由 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 藥物禁忌與數值條件** | **Intent:** 一般藥品資訊，**Risk:** 藥物安全 / 無法排除高風險，**Context:** 現在 / 本人 | 「我有腎功能不好，可以吃 metformin 嗎？」 | **兩者皆用** | **Graph 負責硬邊界**：精準比對數值門檻與禁忌關係（如 `Metformin --CONTRAINDICATED_FOR--> eGFR < 30`）。**Vector 負責軟解釋**：補充禁忌背後的衛教說明與病理機制。 |
| **2. 共病風險與交互作用** | **Intent:** 一般藥品資訊，**Risk:** 個人化用藥 / 交互作用，**Context:** 現在 / 本人 | 「我有慢性腎病，最近醫生加開 dapagliflozin，跟原本的高血壓藥(ACEI)一起吃會傷腎嗎？」 | **Graph 優先** | 此類問題高度依賴結構化關係。Graph 能直接命中多重風險因子與交互作用疊加：藥物間的併用關係走 `Substance --INTERACTS_WITH--> Substance`（邊上以 `effect` 註記結果，如「增加急性腎損傷風險」），後果病況再由 `Substance --INDUCES--> Condition (急性腎損傷)` 與 `Condition --RISK_FACTOR_FOR--> Condition` 承接，由檢索端多跳組合。**〔校對修正〕** 原文寫成 `dapagliflozin + ACEIs` 疊加的 `INDUCES` 關聯；依 Preprocessing B 抽取原則 6，併用／交互作用**不得**抽成 `INDUCES`，且現有的 `ACEIs / 利尿劑 / NSAIDs --INDUCES--> 急性腎損傷` 三條已被標記為抽錯、待重抽為 `INTERACTS_WITH`（見 `MS1/Preprocessing - B.md` 第 12 節 A3）。路徑判斷（Graph 優先）不變。 |
| **3. 外在情境誘發急症** | **Intent:** 症狀資訊，**Risk:** 可能急症 (如 DKA)，**Context:** 過去 / 本人 (釐清成因) | 「之前感冒得腸胃炎又沒胃口，為什麼醫生說差點引發酮酸中毒 (DKA)？」 | **兩者皆用** | **Graph 負責因果對應**：捕捉外在誘因（如 `Trigger (急症/進食減少) --TRIGGERS--> Condition (DKA)`）。**Vector 負責情境衛教**：提供為何感染會引發此類併發症的完整說明。 |
| **4. 基礎病理與廣泛衛教** | **Intent:** 一般衛教，**Risk:** 低風險，**Context:** 一般 / 假設 | 「什麼是胰島素阻抗？為什麼第二型糖尿病患者會有這個問題？」 | **Vector 優先** | 這類廣泛性的知識查詢不涉及具體的用藥禁忌或數值限制。Graph 缺乏長文敘事能力，由 Vector 檢索學會指引與教科書的完整段落最為合適。 |
| **5. 衛教處置不當之副作用** | **Intent:** 症狀資訊 / 一般衛教，**Risk:** 低中風險，**Context:** 現在 / 本人 | 「我肚皮上打胰島素的地方最近硬硬的，是因為我都打同一個位置嗎？」 | **兩者皆用** | **Graph 負責確認成因**：立即證實 `Trigger (未輪替注射部位) --TRIGGERS--> Condition (皮膚澱粉樣變性症)` 的事實。**Vector 負責行為改善**：調出後續應如何正確輪替注射部位的實務護理建議。 |
| **6. 變更劑量之高風險行為** | **Intent:** 停換藥 / 劑量要求，**Risk:** 無法排除高風險，**Context:** 現在 / 本人 | 「我今天吃比較少，可以自己把胰島素劑量減半嗎？」 | **Graph 輔助阻擋** | 根據 LLM 組的設計，這類問題多半會由 Policy Gate 拒絕或轉介。但若需由 RAG 提供資訊，Graph 的 `Trigger (降低胰島素劑量) --TRIGGERS--> Condition (DKA)` 能提供明確的醫療風險事實，避免 Vector 生成不當安心建議。 |
| **7. 第三方照護與副作用確認** | **Intent:** 一般藥品資訊 / 症狀資訊，**Risk:** 低風險，**Context:** 現在 / 第三人稱 (家屬) | 「我媽媽吃這種糖尿病藥會一直噁心想吐，這種副作用正常嗎？」 | **兩者皆用** | **Graph 負責快速查核**：確認是否有 `Substance --CAUSES_SIDE_EFFECT--> Symptom` 的關聯。**Vector 負責家屬衛教**：檢索家屬照護指引，提供緩解副作用的日常照護注意事項。 |
| **8. 監測指標意義與解讀** | **Intent:** 一般衛教 / 診斷要求，**Risk:** 需澄清是否為確診要求，**Context:** 現在 / 本人 | 「我今天抽血的 HbA1c 數值是 7.5%，這樣算很高嗎？」 | **Vector 優先** | 這類數值解讀問題通常在臨床指引中有標準範圍描述。雖然 Graph Schema 有 `LabParameter`，但主要是用於 `REQUIRES_MONITORING`，單純解讀數值較適合 Vector 提供整體衛教說明，並由 Policy Gate 避免做出「確診」結論。 |
| **9. 特殊族群用藥考量** | **Intent:** 一般藥品資訊，**Risk:** 藥物安全 / 個人化用藥，**Context:** 假設 / 第三人稱 (如孕婦) | 「懷孕期間是不是很多糖尿病的藥都不能吃？只能打胰島素嗎？」 | **Vector 優先＋Policy Gate 轉介** **[TBD-資料缺口]** | **〔校對修正：此類的 Graph 路徑目前是空的〕** 原設計為「Graph 確認 `Insulin --TREATS--> 妊娠糖尿病` 並排除其他藥物的 `CONTRAINDICATED_FOR`」，但 TFDA 129 筆資料集與 openFDA metformin 仿單皆無妊娠相關內容，Preprocessing B 目前 37 條三元組中**沒有任何一條與懷孕／妊娠有關**，故 Graph 側現階段無資料可命中。又孕婦屬 LLM 組 Part A 附錄 C 的 must-pass 子群，不宜以空結果帶過。**M1 暫定**：Vector 優先（檢索特殊族群用藥考量與臨床建議）＋ 由 Policy Gate 轉介專業諮詢；**M2 補上妊娠糖尿病來源**（糖尿病學會指引相關章節）後，再恢復兩者皆用。 |
| **10. 藥品分類與機轉** | **Intent:** 一般藥品資訊，**Risk:** 低風險，**Context:** 一般 / 假設 | 「SGLT2 抑制劑跟 DPP-4 抑制劑有什麼不一樣？」 | **Vector 優先** | 雖然 Graph 可透過 `IS_A` 表達從屬關係，但要解釋「機轉有什麼不一樣」需要完整的段落說明。Vector 適合檢索藥物作用機轉的詳細比較與衛教資料。 |

---

### 〔校對補充〕本表對 Boundary 組設定的相依性

本表第 3、5、6 類（外在情境誘發急症／注射部位副作用／自行變更劑量）**全部建立在 `Trigger` 節點型別與 `TRIGGERS` 邊之上**——十類中占三類。

Boundary B 成員文件第 1.4 節現行的節點型別 allow-list 寫的是「Drug / Disease / Symptom」（schema 定案前的暫定寫法），照此執行會把 `Trigger` 節點整批排除，等於直接停用本表三成的查詢類型。同節的關係型別 deny-list 若把 `IS_A` 預設排除，第 10 類（藥品分類與機轉）的 Graph 輔助路徑也會失效。

→ 本表的路徑設計無須修改，但需在 8/24 會議上與 Boundary B 對齊節點／關係 allow-list（該文件的校對版已補，見 `MS1/revised/Boundary - B (校對版).md`）。

---

*參考資料：*
**Reddit r/diabetes** https://www.reddit.com/r/diabetes/
**Reddit r/diabetes_t2** https://www.reddit.com/r/diabetes_t2/?tl=zh-hant
**Reddit r/Gestational Diabetes** https://www.reddit.com/r/GestationalDiabetes/
**台灣急診醫學會** https://www.sem.org.tw/EJournal/Detail/156
**國民健康署「健康九九」** https://health99.hpa.gov.tw/health99/ContentSection?siteId=1&nodeId=614
**中華民國糖尿病衛教學會** https://www.tade.org.tw/
