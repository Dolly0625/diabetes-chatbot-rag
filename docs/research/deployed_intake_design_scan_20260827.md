# 實際上線的醫療／看診前 intake 對話機器人與問卷式 intake 產品設計掃描

> **日期**：2026-08-27  
> **性質**：Research-only，不動程式碼。大量並行中英日文 websearch 交叉驗證，區分「官方文件 / 新聞報導 / 使用者評論」，找不到一手資料明說不腦補，排除純行銷落地頁。  
> **對象**：為 `/tfda-diabetes-agent` 的 LINE 看診前 8 欄 intake bot（`PreVisitIntake` 8 欄 + `intake_stage` 3-stage，workflow A→B→C→D 固定，B/D gates 不可繞過）找出可直接抄的「對話節奏」模式。本掃描聚焦**已上線、有真實使用者、能找到截圖/文件/試用紀錄**的產品，而非 demo。  
> **本 repo 現況對照**：`tfda_context_gate/intake/schemas.py:163-180` `INTAKE_FIELD_QUESTIONS` 為「第 1/8 題｜目前有固定吃藥…」式編號追問；`STAGE_QUESTIONS` 嘗試一次填多欄但後續仍逐欄確認；`line_bot/app.py:418-474` `_quick_actions_for_status` 依狀態給 2–3 個 Quick Reply（不清楚／沒有／暫停整理），無進度條、無隱式重述、無 repair 專路。使用者回饋「死板」主因即此。

---

## A. 案例總表（20 個，達標 15+）

證據等級定義：`A 官方文件`（產品官網/政府手冊/API doc）、`B 新聞/學術/第三方報導`（可驗證年份與數據）、`C 使用者痕跡`（App Store 評論、試用影片、社群回報）。`證據強度` 採 `A > B > C`，並標明是否找到**對話原文/截圖描述**。

| # | 產品名 | 地區 / 機構 | 大約年份 | 證據等級與規模 | 一句話特色 | 對話原文/截圖可得性 |
|---|---|---|---|---|---|---|
| 1 | **Epic MyChart eCheck-In / PreCheck-In / Get Ready** | 美國，Epic Systems，Asante/UVA/HHS 等 300+ 醫院系 | 2019–2024 持續 | A 官方 PDF + B 多院教學文件；全美 MyChart 活躍用戶 >1.3 億（Epic 公佈） | 醫療 intake 的全球事實標準：7 天前推播→分段完成→提交前可回顧修改 | 有，多份 tip sheet 含步驟截圖描述（見 B1） |
| 2 | **Kaiser Permanente Intelligent Navigator (KPIN)** | 美國 Kaiser Permanente 南加 4.9M 會員 | 2024-10 上線，2025 Nature 論文 | A 官方 + B Nature `s41746-025-01838-1`：日均 19,154 encounters，97.7% 高危偵測，abandon 2.94% | Free-text「reason for visit」→ NLP 分流，每頁 ≤3 題澄清 | 有，論文 Suppl. Fig2-3 對話流程圖 |
| 3 | **NHS App + eConsult / PATCHS 問診表單** | 英國 NHS England，GP surgeries | 2021–2025 | A NHS App Design System 官方 pattern + B 2026-07 AI triage pilot（Sussex 降 29% 電話） | NHS App 內「Ask about a health problem」→ 外包問卷→ Summary 供醫師 triage | 有，Design System 含原型截圖 |
| 4 | **NHS Talking Therapies Digital Front Doors：Limbic Access / Wysa / Censeo / AskFirst** | 英國 NHS Talking Therapies | 2022–2024 NICE EVA htg756 | A NICE Final Scope + EAG 報告（94 頁） | Limbic=對話式轉介 + 臨床報告；Wysa=階段式 eTriage + 正念練習 | 有，報告 Table 2–3 欄位與流程圖 |
| 5 | **Ada Health Symptom Assessment** | 德國/英國，全球 13M+ 用戶 | 2011–2024 | A 官方 help + B JMIR pilot（523 人，97.8% 易用） | 機率推理動態選下一題，progress bar 但 5 分鐘內完成，一題一問 + “What does this mean?” | 有，ScreensDesign 逐屏拆解 + Figure 1 |
| 6 | **Symptomate / Infermedica Intake API** | 波蘭/全球，Infermedica | 2018–2024 | A 官方 developer docs + B 整合文件 | Intake API 定義 16 標準段（risk_factors→dynamic→chronic_diseases…），`single/group_single/group_multiple` 題型，`unknown/don't know` 為一等公民 | 有，API spec 與 questionnaire 範例 |
| 7 | **Babylon Health Triage Chatbot** | 英國 Babylon（含 NHS 合作） | 2016–2020 | A 官方設計語言文件（Jack Roles）+ B arXiv/PMC 部署研究 | 曾用「選項自動跳下一頁」→研究發現用戶漏看選項，後改強制 Submit 按鈕 + popover 說明 | 有，設計文件含 before/after 截圖與滾動測試數據 |
| 8 | **K Health AI Intake Agent** | 美國 K Health，與 Penn Medicine 合作 | 2019–2026-07 | B Forbes + MedCity：平均 25 題 / <5 分鐘 | 對話式 intake agent 扮演「住院醫師」先做 history，再把摘要交給主治 | 有，Anders Sandell 設計拆解 |
| 9 | **Medical Note 症状ノート（LINE）** | 日本 Medical Note，4,000 名醫師協力 | 2025-09-30 發表 | A 官方 + B PRTimes | LINE 隨手 memo→ AI 自動整理「何時/何處/何程度」+ 診察用 report，免裝 App | 有，官方頁三步驟截圖 |
| 10 | **Nest診療 LINE 問診** | 日本 診所 SaaS，數百診所導入 | 2023–2025 | A 官方產品頁，含分支邏輯說明 | LINE 予約→問診→カルテ転記一鍵，條件分岐「必要質問だけ出す」，附件可傳傷口/檢驗照片 | 有，no-code builder 截圖與模板 |
| 11 | **march（マーチ）電子問診票** | 日本 march SaaS | 2024–2025 | A 官方功能 LP | 同 Nest：診療科別模板 + LINE 完結 + 自動提醒 + 電子カルテ連携 | 有，功能清單與流程圖 |
| 12 | **Medibot LINE 予約・問診・決済** | 日本 Medibot | 2022–2025 | A 官方 | LINE 上予約/問診/オンライン診療/決済全完結，營運數據不公開 | 部份，產品頁僅有概念圖，無對話原文 |
| 13 | **彰化基督教醫院 蘭醫師 LINE Bot** | 台灣 彰基 + HTC DeepQ | 2019–2021 升級 3.0，迄今服務 >23,000 人 | B 官方頁 + TVBS/ETtoday/HTC 新聞 | 全台首位 AI+區塊鏈 LINE 醫療 bot，「告訴症狀→建議科別→掛號」 | 無公開對話截圖，僅功能選單描述；需實測 |
| 14 | **大林慈濟 醫院 LINE「小慈」** | 台灣 大林慈濟 | 2018-04 上線，9 個月 11,000 好友 | B LINE Biz Solutions 案例 + 人醫心傳專訪，滿意度 82% | 「個人化行動篩檢」獲國健署金獎，掛號/進度/衛教推播整合 | 無逐輪對話原文，僅分眾推播截圖 |
| 15 | **部立臺北醫院 行動醫療助理（iota C.ai）** | 台灣 衛福部臺北醫院 | 2022-06 | B 叡揚資訊案例：1 個月上線，首月 2,500 人次，省 200–400 小時 | API 直連 HIS，LINE 內視訊問診 + 掛號提醒 + 自動串回病歷 | 無公開逐字稿，僅流程圖 |
| 16 | **台北慈濟 AI 智能小幫手（LLM+RAG）** | 台灣 台北慈濟 | 2025–2026-05 | B 官方新聞：LLM+RAG + 位置圖 + 語音問答 | 整合門診/住院/交通/長照等面向問答，後台統計熱門問題 | 無對話原文，僅架構描述 |
| 17 | **健保署 Line@ + 健保快易通 阿Ken** | 台灣 健保署 | 2020-04 健保快易通；2022 Line@ 升級 | A 衛福部公告 + B 數位時代：3 月 30 萬通電話觸發導入，284 家支援線上掛號 | 跨平台一站式（掛號/用藥/醫材價格），阿Ken 文字機器人 24h | 無 intake 對話，純資訊查詢對話 |
| 18 | **騰訊健康 智能預問診 + AI 就醫助手** | 中國 騰訊健康，深圳市人民醫院等 | 2023–2024（華佗 GPT 2024-09） | A 騰訊官方 + B 深圳新聞：覆蓋 100 科室 / 800 症狀 / 2000 疾病；深圳人民 13.73 萬人次 | 口語主訴智能識別→模擬醫師思路追問（時間/誘因/用藥/過敏），5 秒生結構化病歷 | 有，官方流程圖 + 新聞對話例「胸悶多久？刺痛還是酸痛？」 |
| 19 | **華佗 GPT（龍崗）** | 中國 深圳龍崗區 12 家公立醫院 | 2024-09 上線，新聞 2025-04 累計 50 萬人次 | B 深圳政府官網 + 香港中文大學（深圳）發布 | 模擬醫師診斷邏輯對話，導診準確率官方 95%+ | 有，新聞逐輪例「胸悶氣短→持續多久？有無伴隨症狀？」 |
| 20 | **浙大邵逸夫醫院 智能醫生助理（LLM）** | 中國 浙江大學附設邵逸夫醫院 | 2023-11-28 | B 騰訊新聞：掃候診區 QR→ 語音多輪問診→5 秒生預問診病歷 | 候診時用藥/過敏/家族史全採，醫師端語音轉結構化病歷 | 有，病患逐字回饋引用 |

> **排除說明**：Zocdoc/Travelers/平安人壽等保險理賠 intake（雖有 85–90% 自動完成率等數據）與純行銷落地頁不列入主表，但納入 C 章的「一題一頁 / 進度可視」等模式來源。1925 依舊愛我安心專線（衛福部心理健康司）目前公開資訊僅為電話 24h 服務，無 LINE intake 實作，故不列。

證據等級自評：官方文件 6 例、新聞/學術 14 例、使用者痕跡 0 例達 App Store 級；但**逐輪對話原文**僅 8 例有直接引用（1,2,5,6,7,9,18,19），其餘需推斷，已於欄位明示。

---

## B. 最相關的 5 個深挖（含對話節奏逐輪拆解）

選取邏輯：對「台灣糖尿病患用 LINE 做看診前 8 欄 intake」的遷移價值最高者——① 直接可抄的 LINE 原生 stack、② 同為 8 欄 structured intake 的技術本體、③ 繁中醫療脈絡最接近的台灣實證、④ 中文 LLM 對話最自然的參照、⑤ 全球 intake 標準對照。

### B1. Epic MyChart eCheck-In / PreCheck-In — 全球 intake 的「教科書」

**為何最相關**：全美事實標準，設計哲學是「把 8 頁問卷拆成 8 個檢查點，每點可個別完成、提交前可回顧」，與本 repo 8 欄對應度 100%。

**開場→追問→確認→摘要（官方 tip sheet 原文引用）**：
- 開場（推播 + 入口）：`eUpdate will become available three days before your appointment. You will receive email/push when ready.` + 首頁 `Get Ready / eCheck-In` 綠色按鈕（HHS 官方 PDF）。
- 追問（分段）：依序 `Personal Information → Sign Documents → Insurance → Allergies → Health Issues → Travel/Questionnaires`。每段為獨立卡片，`Verify or Confirm → This information is correct → Next`。
- 確認：`You will have a chance to review your answers before clicking Submit`；`Each section will ask you to verify your information.`
- 摘要：最終 `Summary page for screening, where you can edit/review any of your previous screening responses. Click Submit`。

**節奏拆解**：
- 一輪訊息長度：極短，每頁 1 個主題 + 1 個主動作（Verify/Next）。非對話式，而是卡片式表單。
- 一次問幾件事：嚴格 **一主題一頁**（地址一頁、過敏一頁）。問卷段落 `Questionnaires` 內雖多題，但官方指引 `limited to three items or fewer per page`（KPIN 優化後亦同）。
- 不知道/忘記：顯式選項 `Yes / No / Unsure`、`Yes-Positive / Yes-Pending / Yes-Negative / No`，以及 `+ Add a trip / Edit/Remove` 的可增刪。
- 進度感：有。`You will only see the parts you still need to do` + 頂部段落指示器（非題號）。未完成段落紅點提示。
- 按鈕 vs 自由輸入：幾乎全為按鈕/選擇 + 少量文字欄（Travel history 自由描述）。自由輸入僅在「Health Issues / Medications」等長文字欄。

**可搬 vs 不可搬**：
- 可搬：**1 主題 1 頁、提交前可回顧、僅顯示待完成段落、No/Unsure 一等公民**——四項可直接對應本 repo 的 `intake_stage` 與 `missing_fields`。
- 不可搬：MyChart 依賴 Epic EHR 預填（`Personal Information will automatically file into your chart`）與 `pre-populating previously entered answers`；本 repo 無 EHR 預填，需改為「上次 intake 快照帶入」才能享同等「少問」效果。另其 email/push 3 天前觸發，在 LINE 需改為 `LINE Notify / 主動推播` 但需使用者已加好友且通過 `LINE Login` 同意。

---

### B2. Infermedica Intake / Symptomate — 8 欄 intake 的「API 本體」

**為何最相關**：唯一公開完整 **intake survey** 結構（`risk_factors / visit_reason / dynamic / chronic_diseases / specialist / hospitalization / drugs / allergies / message_to_doctor`）且明確定義 `Don't know` 題型，與本 repo `PreVisitIntake` 映射度最高。

**開場→追問→確認→摘要（官方 spec 原文）**：
- 開場：`sex / age`（必填，否則 engine 不啟）→ `risk_factors`（依 demographics 建議選項）→ `visit_reason`（new/changing vs administrative）。
- 追問（動態）：`suggest`（基於已選症狀的相關症狀推薦）→ `red_flags`（urgent 識別）→ `interview`（AI 依 Bayesian network 選最資訊量大的下一題），題型 `single / group_single / group_multiple`，每題含 `present / absent / unknown`。
- 文件明確：`Please note ... question can have null value`、`should_stop flag` 決策何時停（夠資訊即停，不問滿）。
- 確認：`You can change your answers and return to previous steps. If you change previous answers ... may ask new questions relevant to information provided`。
- 摘要：`Summary of all questions within Intake survey` 表格對應 FHIR（與本 repo `FHIR_LINKID_MAP` 一致）。

**節奏拆解**：
- 一輪訊息長度：題幹 + 2–3 個選項（Yes/No/Don't know），極短，`single` 題僅 1 問。
- 一次問幾件事：**一次一件事**，但 `group_multiple` 允許同群組多選（如 headache character 多選），`group_single` 嚴格單選。官方建議語音/簡易 chat 場景 `disable_groups` 僅用 `single`。
- 不知道/忘記：**一等公民**，每題皆有 `unknown / Don't know` 選項，且 engine 會據此調整後續追問（不會卡關）。
- 進度感：左側 menu `You can go back to earlier steps. Menu is only visible on widescreen... Click the name of step you want to return to`，無題號進度，而是**階段式 menu + should_stop 智慧停**。
- 按鈕 vs 自由輸入：主體按鈕（Yes/No/Don't know），`drugs_answer / allergy_answer / comment` 三欄保留自由輸入（與本 repo `known_medications/allergies` 對應）。

**可搬 vs 不可搬**：
- 可搬：**每題必含「不知道」、group 題型分流、should_stop 智慧停而非問滿 8 題、允許回頭改答案並重算後續**——四項可直接寫入 `IntakeQuestion` 與 `build_agent_question`。
- 不可搬：其 `red_flags` 與鑑別邏輯依賴 Infermedica Medical Knowledge Base（百萬級疾病模型），本 repo 不可做鑑別診斷；僅能搬「資料蒐集」段，`red_flags` 需替換為本 repo 的 **確定性 Safety Gate 在 LLM 前**（MediLink/IVF 共識）。

---

### B3. 日本 Medical Note 症状ノート + Nest/march LINE 問診 — LINE 原生可抄度最高的一組

**為何最相關**：技術棧與本 repo 100% 重疊（LINE 官方帳號 + Web 問診 + 画像添付），且 Medical Note 的「平時 memo→看診時 AI 整理成 report」正是糖尿病「連續血糖/症狀 memo→回診摘要」的理想隱喻。

**Medical Note 症状ノート（2025-09-30，PRTimes 原文引用）**：
- 開場：`LINEに症状をメモしておくだけで、AIが整理して診察用レポートを作成`；入口為官方 LINE 選單「症状をメモする」。
- 追問：平時不追問，**累積多則自由 memo**（文字/語音皆可），`AIが「どんな症状が」「いつ（時期）」「どこに（部位）」「どのように（程度）」を整理`。
- 確認→摘要：`「今までのメモを見る」を押すと…伝えたいメモを選ぶと診察用レポートが作成されます`；report 含 `主な5つの症状 / 症状の詳細 / 先生への相談ポイント + 適切な診療科選択ポイント + 近隣クリニック案内`。
- 節奏：**非即時問卷**，而是「平時零壓 memo + 看診前一鍵生 report」；一則 memo 長度不限，AI 內部結構化。
- 按鈕 vs 自由輸入：**自由輸入為主**，按鈕僅用於「選哪幾則 memo 生成 report」。
- 不知道處理：無「不知道」題，因為是 memo 制；缺漏在 report 以「未記載」呈現而非逼問。

**Nest診療 / march（產品頁原文）**：
- 開場：`LINE上で予約から問診まで完結…問診未回収ゼロへ`；`時間帯予約と連携し、来院直前に問診記入のリマインド`（自動推播）。
- 追問：`必要な質問だけが自動で出てくるストレスの少ない問診体験` + `条件に応じて出し分けられる複数の送信完了画面` + `回答内容に応じた分岐条件をGUIで安全に設定`；`選択肢＋自由記述で症状のニュアンスまで伝えられる`；`傷や検査票の画像・署名もその場で提出できる`。
- 確認：`入力漏れや添付状況も画面で確認でき、スマホでも迷わず送信できます`；`注意が必要な回答を自動で色分けし、確認漏れを防止`。
- 摘要：`問診回答を電子カルテへ反映できるカルテ連携 / ワンクリック転記`。
- 一次問幾件事：**no-code builder 可拖拉「1 頁 1 問」或「1 頁 1 主題」**，官方推薦「必要質問だけ出す」即 progressive disclosure。
- 進度感：有，`ページと質問をドラッグ&ドロップで組み立てる` + 頂部 progress；`時間帯予約 30分前` 再提醒算「時間觸發的進度」。
- 按鈕 vs 自由輸入：**混用典範**：選擇題為按鈕/單選，自由記述為文字框，画像添付為原生上傳。

**可搬 vs 不可搬**：
- 可搬（高度可抄）：**LINE 選單常駐入口 + 予約/リマインド 30 分前觸發 + 條件分岐只問必要題 + 自由記述 + 画像添付 + 注意回答色塊標示 + 發送前漏填檢查**——六項皆可直接對應本 repo `line_bot/ui.py` 的 Rich Menu + `handle_image_message` 藥袋上傳 + `MEDICATION_CLARIFICATION_QUESTIONS` 2-attempt。
- 不可搬：日本 SaaS 的「ワンクリックカルテ転記」依賴 ORCA/CLIUS 等電子カルテ API；台灣需對接 FHIR `QuestionnaireResponse → $extract → Bundle`（本 repo 已有 `FHIR_LINKID_MAP`），但需醫院端配合。另日本在宅醫療法規對「症状ノート」的免責較寬鬆，台灣需更強的 `disclaimer: 非診斷、需醫師確認`（本 repo `PreVisitSummary.disclaimer` 已有）。

---

### B4. 台灣實證：彰基 蘭醫師 / 大林慈濟 小慈 / 部北醫院 iota C.ai — 為何台灣 LINE 醫療 bot 至今仍「選單重、對話輕」

**為何最相關**：同語言、同 LINE 生態、同法規，同為繁中病患，且有真實規模數據（>23,000 人、大林 11,000 好友 82% 滿意、部北 2,500 人/月），是「搬不動」的現實對照組。

**逐家拆解（皆無公開逐輪對話原文，僅能依官方/新聞描述還原，誠實標示）**：

- **彰基 蘭醫師**（2019 上線，HTC DeepQ 合作，跨院 AI+區塊鏈）：
  - 開場：`只要將自己的症狀告訴人工智慧醫療客服-蘭醫師，他就會透過問診來建議合適的就診科別`（官方頁）。TVBS 2021-06-29 補充：`家人可以事先將「這期間有2次低血糖」情況寫在上面，醫護人員看到後更能掌握病情`。
  - 節奏：**選單導向**：`1 就醫指引 2 我要掛號 3 看診進度 4 今日活動 5 電話諮詢 6 我…`，無「一問一答」intake；新增的視訊三合一混合門診亦走「電話約診→資料同步至 LineBot→點進入線上診間」。
  - 按鈕 vs 自由輸入：**按鈕/選單為主**，自由輸入僅用於「症狀描述」後由 AI 分科。
  - 進度感：無題號進度，有「掛號成功→推播」狀態通知。
- **大林慈濟 小慈**（2018-04，LINE Biz 案例）：
  - 開場：服務清單 `掛號/進度/健康管理/醫藥查詢/快速連結共二十項`；`小慈…線上詢問疾病相關問題，會提供最準確就醫指引與衛教影音`。
  - 節奏：**服務入口制**，非 intake 問卷；亮點是「個人化行動篩檢」自動比對國健署雲端四癌篩檢資格並推播。
  - 進度感：推播式（`系統結合國健署雲端資料庫…告知與提醒您按時接受四癌篩檢`），非問卷式進度。
- **部北醫院 iota C.ai**（2022-06）：
  - 開場：`搜尋部立臺北醫院 LINE 官方帳號，加入好友並輸入身分證字號建立初診…依預約時間進入指定 LINE 診間開啟視訊`。
  - 節奏：**API 直連 HIS** 為賣點（少數能把 `掛號資料透過 API 即時串回 HIS` 的案例），對話為 GUI 拖拉式選單，非自由 intake。
  - 規模：`短短一個月內就完成上線…上線至今已吸引 2,500 人次使用，同時也節省 200~400 小時的溝通時間`（叡揚新聞）。

**共同觀察（可信度高，因三家皆選單重）**：
- 一輪訊息長度：**長**（功能清單一次 6–20 項），與「一輪一問」相反。
- 一次問幾件事：**一次展示所有服務**，無 progressive disclosure。
- 不知道/忘記：無專門設計，靠「電話諮詢」fallback。
- 按鈕 vs 自由輸入：**按鈕/圖文選單 >90%**，自由輸入僅在單一「問小慈」入口。
- 為何如此：① HIS 整合優先於對話體驗（部北明言「串接系統與資料表設立是最大挑戰」）；② 法規保守（不做問診推定，只做掛號/查詢）；③ 維運成本（大林靠 LINE Notify 免費推播，大量客製問卷需人力）。

**可搬 vs 不可搬**：
- 可搬：三家的 **選單入口 + 推播提醒 + HIS 直連** 正是本 repo 欠缺的「觸發與預填」；大林的「自動比對資格後推播」可改為「依上次 intake `missing_fields` 主動推播續填」。
- 不可搬：若照搬「一次展示 20 項服務」，會與本 repo 的「8 欄聚焦 intake」衝突，加重認知負荷（Intercom 研究證實表單比來回對話更快完成）。台灣醫院對「問診」二字的法規敏感度高，需明確文案為「整理看診資料」而非「線上問診」。

---

### B5. 中文 LLM 對話式預問診：騰訊 智能預問診 / 華佗 GPT + 英美 K Health — 自然對話的「天花板」參照

**為何最相關**：同為中文、口語化主訴、模擬醫師思路追問，且有真實規模（深圳人民 13.73 萬人次、華佗 50 萬人次、K Health 平均 25 題 5 分鐘），是本 repo 若要「變自然」時對話品質的參照系，但**診斷部分不可抄**。

**騰訊 智能預問診（深圳市人民醫院，2024-06 新聞原文）**：
- 開場：掃碼或掛號後 `診前信息收集` 入口，非閒聊開場。
- 追問（官方所述模擬醫師思路）：`從患病時間、發病緩急、病因誘因、是否服用過藥物、過敏史等進行全面問診，結合患者回答智能調整追問問題`；新聞例：`「哪裡不舒服？」「胸痛了多久了？」「是刺痛、跳著痛還是酸痛？」`
- 確認→摘要：`智能理解對話內容，提取主訴、現病史、既往史、過敏史等信息，遵循國家門診電子病歷書寫規範，自動生成結構化診前報告`；`5 秒自動生成預問診病歷，並同步至醫生工作站`（浙大邵逸夫同架構）。
- 一輪訊息長度：**短**，一問一答，對話框型而非選項卡。
- 一次問幾件事：**一次一件事**，但會依回答動態插入相關症狀（與 Infermedica `suggest` 同理）。
- 不知道/忘記：`口語化主訴智能識別…智能識別檢驗、檢查、用藥、手術等具體意圖，進一步智能追問`；不強迫選，自由文字為主。
- 進度感：弱（無題號），靠「已收集診前報告進度」暗示；測評 `病歷小結準確率 87%`。

**華佗 GPT（龍崗，政府官網原文）**：
- 追問例：`患者輸入「我最近有點胸悶氣短」→ 約3秒回應：「這種症狀持續多久了，有沒有其他症狀伴隨？」→ 經幾個回合推薦「呼吸與危重症醫學科」和「心血管內科」→ 點擊跳轉預約掛號`。
- 按鈕 vs 自由輸入：**自由輸入為主**（`文字、語音甚至圖片都能理解`），按鈕僅在科室推薦階段出現。
- 不知道：`支持自然語言輸入（如「肚子一陣陣絞痛」），患者滿意度提升`（武漢中南同架構 98% 導診準確率）。

**K Health（Penn Medicine 2026-07 合作）**：
- 追問：`平均回答 25 題 / <5 分鐘`，`AI 像住院醫師一樣先做 history，再把摘要交給主治`。
- 節奏：`從 chief complaint 開始 → 問細節 → 跑遍相關症狀以擴大理解並縮小鑑別`，每輪一題，由 AI 動態選下一題。

**可搬 vs 不可搬**：
- 可搬（對話品質）：**口語主訴識別、模擬醫師思路的追問（時間/緩急/誘因/用藥/過敏）、自由輸入為主、推薦僅在最後一步用按鈕**——皆可作為本 repo `AgentPlanner` 3 擇一中 `REWRITE` 的 prompt 範例。
- 不可搬（法規/安全）：三者皆在「導診/預問診報告」中隱含**鑑別/科室推薦**甚至 `診前報告自動生成`，在台灣屬**醫療器材/診斷行為**，本 repo 依 `d_output_gate` 與 `ToolContract` 僅能做 `TFDA_RISK/HPA_DIET_GUIDE` 衛教檢索與資料整理，**不可照搬科室推薦與鑑別報告**；且其 `準確率 87–95%` 仍遠低於臨床可用門檻，需保留本 repo 的 `DeterministicContextGate + SemanticVerifier` 擋診斷字樣。

---

## C. 共通設計模式清單（出現 ≥3 次＝可信，附出現次數與來源）

> 計數範圍為 A 表 20 案例 + 保險/政府補充案例（Zocdoc, GOV.UK, Intercom），≥3 次標 `✓可信`。

### C1. 結構與節奏（最可信，≥7 次）

| # | 模式 | 出現 | 來源舉例 | 對 8 欄 intake 的啟示 |
|---|---|---|---|---|
| 1 | **一題一頁 / 一主題一頁** | 9 次 ✓ | Epic(1) 每主題一頁、GOV.UK one question per page、Infermedica single、Babylon 分離 question/card、K Health 逐題、騰訊/華佗 一問一答、Nest 必要質問だけ | 本 repo 現 `第 n/8` 連發與 `stage` 一次拋多欄，建議**拆為 `1 輪 1 欄`，僅 `stage1` 中 `chronic_conditions+family_history` 可合併** |
| 2 | **提交前可回顧與編輯** | 8 次 ✓ | Epic `review before Submit`、Infermedica `change answers and return`、Babylon `Back`、Nest `入力漏れ確認`、Medical Note 選 memo 再生 report、GOV.UK Back link | 本 repo `REVIEW Flex` 已有雛形，需**改為三段 carousel + 每段修改鈕**（見 E10） |
| 3 | **Progressive disclosure：只問必要題（分支）** | 8 次 ✓ | Nest `条件分岐`、march `条件分岐`、GOV.UK branching、Intercom conditional fields、Infermedica `suggest/red_flags` 動態、騰訊 智能追問、Wuhan 200+ 节点工作流 | 本 repo 已有 `INTAKE_STAGES`，需補 `分岐條件：如 `family_history=無` 則跳過細問、`known_medications` 含胰島素則追問低血糖史 |
| 4 | **進度指示（非題號）** | 6 次 ✓ | Epic 僅顯示待完成段落、GOV.UK Step indicator（可選）、Symptomate 左側 menu、Frontiers 研究進度條、Nest progress | 本 repo `第 1/8 題` 為最差實踐（使用者壓力感，Frontiers 2023 證實長問卷 NPS -12），建議改為 **階段式進度：`已完成 用藥/過敏，還差 症狀 與 想問醫師 2 段`** |
| 5 | **預填/帶入（Prefill）以少問** | 5 次 ✓ | Epic `pre-populating previously entered answers`、NHS Prefill、Zocdoc `Save demographic details for future visits`、Infermedica `patient creation affects questions`、大林 自動比對國健署資料 | 本 repo 無 EHR，但有 `intake_snapshot` 與 `product_session` 持久化，可**帶入上次 `provided_fields`** |

### C2. 輸入與容錯（≥5 次）

| # | 模式 | 出現 | 來源 |
|---|---|---|---|
| 6 | **「不知道 / 不確定 / 忘記」為顯式選項** | 7 次 ✓ | Epic `No/Unsure`、Infermedica `unknown/Don't know`、GOV.UK `allow users to answer I do not know`、Babylon `multiple answers including none`、Symptomate `Don't know/skip`、K Health skip、騰訊 不強迫選 |
| 7 | **按鈕與自由輸入混用（Hybrid）** | 8 次 ✓ | Intercom `Hybrid model: form within conversation` 原文、Nest `選択肢＋自由記述`、Infermedica `free text drugs/allergy/comment`、GOV.UK 原則 2 `deterministic UI for specific data`、騰訊 文字/語音/圖片皆可 |
| 8 | **單輪字數 ≤60 中文字 + 最多 3 個選項** | 5 次 ✓ | KPIN `≤3 items per page`、Gov.uk `one question per page helps users focus`、Intercom `keep sentences short sweet, ≤4 bubbles`、Babylon `Submit button mandatory after last option to prevent missing`、Frontiers 短問卷有用性 5.0 vs 長 4.3（p=.048） |
| 9 | **智慧停而非問滿（should_stop）** | 4 次 ✓ | Infermedica `should_stop`、Ada 機率推理選最資訊量下一題、騰訊 `5 秒生成` 暗示動態停、K Health 動態選題 |
| 10 | **容錯與澄清（Sorry, didn't get that / clarification）** | 5 次 ✓ | Infermedica `/parse` 無理解時 `Sorry, I didn't get that. Could you rephrase?`、Ada `What does this mean?`、Intercom `Quick Reply消失後可繼續對話`、Babylon `explanation in popover not new page` |

### C3. 確認與信任（≥4 次）

| # | 模式 | 出現 | 來源 |
|---|---|---|---|
| 11 | **隱式確認帶重述（Paraphrase）** | 4 次 ✓ | Papenmeier 2023 `Paraphrase显著提升参与感`、Infermedica `I understood: Sore throat and Cough. Is that correct?`、騰訊 提取主訴後確認、K Health 摘要給醫師前重述 |
| 12 | **來源可追溯與免責** | 6 次 ✓ | 騰訊 `回答結束後顯示參考文獻來源`、GOV.UK `check this answer 附來源連結`、NHS 要求可及性、Epic `This information is correct` 明確承諾、本 repo `PreVisitSummary.disclaimer` 非診斷 |
| 13 | **主動修補（Repair）入口** | 4 次 ✓ | Infermedica `menu to return to earlier steps`、Babylon `Back button at bottom`、Symptomate `change answers and return`、GOV.UK `Back link at top` |

### C4. 觸發與續填（台灣特別重要）

| # | 模式 | 出現 | 來源 |
|---|---|---|---|
| 14 | **觸發：預約/時間驅動 + 自然句觸發** | 5 次 ✓ | Epic `3 days before push`、Nest `予約30分前リマインド`、KPIN `free-text reason for visit`、本 repo `RuleBasedSignalExtractor.is_pre_visit_intake_text("要看醫生/回診")`、騰訊 `掛號付款後推送預問診消息` |
| 15 | **續填：Save and return / 暫停後回來** | 5 次 ✓ | NHS `save and return will be available`、GOV.UK `save answers automatically as they go`、Zocdoc `save details for future visits`、Frontiers 長者研究 `需進度條與可編輯`、本 repo `PAUSED` + `SQLiteProductSessionRepository` |

> **未達 3 次、不可信故不列**：如「語音輸入為主」（僅騰訊/華佗/Kaiser 提）、「區塊鏈存證」（僅彰基蘭醫師）、「虛擬人形象」（僅平安人壽）等，不作為可信模式。

---

## D. 「死板 vs 自然」實際產品對照表

> 左欄為本 repo 現況（`schemas.py` 原文或 `line_bot/app.py` 邏輯），右欄為上線產品的實際一句話，均有來源；中欄為差異本質。

| 情境 | 死板（本 repo 現況，2026-08-27） | 自然（上線產品實際做法） | 差異與為何自然較好 |
|---|---|---|---|
| 開場 | `第 1/8 題｜目前有固定吃藥或打胰島素嗎？知道藥名就直接說；不確定也沒關係。`（schemas.py:164） | Epic：`eCheck-In will guide you through several sections. Each section will ask you to verify your information.`；Medical Note：`LINEに症状をメモしておくだけで、AIが整理してレポート作成` | 死板開場即「考試感」8 題預告；自然開場說「我來幫你整理，逐段驗證/隨手記即可」 |
| 一次問幾件事 | `為了幫您整理看診資料，請問目前使用的藥品、過敏史、慢性病史及家族史？（可一次說明多項…）`（STAGE_QUESTIONS stage1，4 項一次拋） | GOV.UK：`Asking just one question per question page helps users understand… focus on the specific question`；KPIN：`limiting each page to three items or fewer` | 一次 4 項→認知超載；一題一頁→聚焦與可回頭 |
| 不知道處理 | 同題幹末尾 `不確定也沒關係。` 但仍進 2-attempt `請問藥物的顏色、形狀或服用時間？`（無顯式 Don't know 鈕） | Infermedica：`choices: [present, absent, unknown]` 每題皆有 `Don't know`；GOV.UK：`allow users to answer I do not know or I'm not sure if they are valid responses`；Epic：`No/Unsure` | 死板把「不知道」當文字容忍，自然把它當**一等公民選項**，點即過關不重問 |
| 確認 | 無隱式重述，直接進下一題 `第 2/8 題…` | Infermedica：`I understood: Sore throat and Cough. Is that correct?`；Papenmeier 研究：`Paraphrase显著提升能力感`，而 `OK/了解` 無效 | 死板讓使用者不確定機器是否聽懂；自然用**帶內容重述**給可糾正的機會 |
| 進度感 | `第 n/8 題` 數字進度（1/8..8/8） | Epic：`You will only see the parts you still need to do`（待完成段落式）；Frontiers：短問卷 NPS 0 vs 長 -12，建議階段式而非題號 | 題號進度→「還有 7 題」壓力；段落進度→「已完成 用藥，還差 症狀 1 段」掌控感 |
| 按鈕 vs 自由輸入 | `line_bot/app.py:439-473` 依題號給 2–3 個固定 Quick Reply（沒有用藥/不清楚/暫停整理），自由輸入僅在文字框，無混用設計 | Intercom：`Hybrid: deliver a form within the conversation… clearly signals optional, efficient`；Nest：`選択肢＋自由記述でニュアンスまで`；Infermedica：`free text drugs_answer` | 死板按鈕與自由輸入分離；自然在**同一輪混用**（按鈕選常見值 + 文字補充） |
| 多症狀齊發 | `extract_fields_from_utterance()` 雖抽多欄，但後續仍逐欄 `第 5/8→6/8→7/8` 追問 | K Health：`run through all related symptoms to expand understanding and narrow down` 但逐題；Infermedica：`suggest` 相關症狀而非一次轟炸 | 死板把一次說的 3 症狀拆成 3 輪重問；自然**先摘要最關鍵 1–2 項確認，其餘進摘要卡一次性確認** |
| 岔題 | 無 FAQ 側路，答非所問僅進下一題 | 騰訊：正確區分 `找醫生/找科室/病情咨询` 並 `15條跳轉規則`；Wuhan：`全局意圖監聽，在對話節點嵌入輕量意圖識別…先科普再調回導診` | 死板岔題即迷路；自然**1 層側路→主動回指原題** |
| 摘要 | `PreVisitSummary` 為連續長文 + 單一 disclaimer | Medical Note：`主な5つの症状/詳細/相談ポイント` 分段 report + `選擇哪幾則 memo 生成`；Epic：`Summary page where you can edit/review any previous responses` | 長文摘要難掃描；分段卡片 + 每段修改鈕可掃描 |
| 語氣 | 固定 `第 n/8 題｜…` 無同理與為何而問 | GOV.UK：`Use hint text to show information that helps the majority… how their information will be used`；Babylon：`popovers keep user on page` 而非跳頁 | 死板像表單；自然在題幹下加**一行 hint：為何而問、將如何用** |

---

## E. 對本 repo 的 10 條可直接套用的具體寫法（附台灣糖尿病 LINE 情境的繁中例句）

> 每條標 `可直接改哪個檔案哪個常數/函式`，並給**繁中例句**（台灣 50–79 歲糖尿病患口吻），同時標 `法規/語言/LINE 限制` 的搬運動作。

### E1. 去題號，改「階段 + 一題一頁」

- **改哪裡**：`tfda_context_gate/intake/schemas.py:163-180` `INTAKE_FIELD_QUESTIONS` 刪 `第 n/8 題｜` 前綴；`line_bot/app.py:439-473` 刪題號判斷，改依 `intake_stage` 給進度。
- **搬運判斷**：✓ 可搬。語言無礙；LINE 無限制；法規更友善（不像考試）。
- **例句（取代第 1/8 題）**：
  - 死板：`第 1/8 題｜目前有固定吃藥或打胰島素嗎？`
  - 自然：`先記用藥就好～現在固定吃的藥或打的胰島素，有哪些？知道藥名就說，不知道也沒關係。[按鈕：目前沒有用藥｜不清楚｜暫停整理]`
- **一句 hint 補為何而問**：`（告訴我用藥，醫師比較好判斷血糖藥是否需調整）`——抄 GOV.UK hint text 與 Epic `why we ask`。

### E2. 每題必給「不知道 / 沒有 / 跳過」顯式按鈕（不再只靠文字容忍）

- **改哪裡**：`line_bot/app.py:_quick_actions_for_status` 的 `NEEDS_CLARIFICATION` 分支，每題固定 3 鈕，複寫 `schemas.py:138-160` 的 `unknown` 語意。
- **搬運判斷**：✓ 可搬。Infermedica/GOV.UK/Epic 皆為一等公民；LINE Quick Reply 13 鈕上限足夠；法規上 `unknown` 比逼問更安全。
- **例句**：
  - `有沒有藥物或食物過敏？[沒有過敏｜不確定｜暫停整理]`（點 `不確定` → 直接記 `待確認`，不二次追問，與 E6 的 2-attempt 僅針對口語藥名）
  - `想問醫師什麼？[還沒想到｜跳過｜查看摘要]`（呼應 `symptom_description` 可跳過的設計）

### E3. 隱式確認帶重述，而非「好的，下一題」

- **改哪裡**：新增 `IMPLICIT_CONFIRM_TEMPLATES`，在 `extract_fields_from_utterance` 命中後先回重述再進下一題（現 `tool.py:343-398` 後僅進 `build_intake_question`）。
- **搬運判斷**：✓ 可搬。Papenmeier 2023 與 Infermedica `Is that correct?` 皆證實有效；LINE 文字無限制。
- **例句**：
  - U：`吃 metformin，還有一顆白色的圓藥丸`
  - B：`你說的「metformin + 白色圓藥丸」，我記成「metformin、另一顆待確認」，對嗎？[正確｜更正｜幫我看藥袋]`
  - 關鍵：**重述含原文片段**，讓長者可直接糾正（抄 Papenmeier `Paraphrase`）。

### E4. 一次只確認 1–2 項，其餘進摘要卡（解多症狀齊發）

- **改哪裡**：`tool.py:extract_fields_from_utterance` 多欄命中時，僅對 Top 2 做隱式確認，其餘寫入 `PreVisitIntake` 但不逐輪轟炸。
- **搬運判斷**：✓ 可搬。K Health/Infermedica `suggest` 皆只追最相關；LINE 一輪 3 鈕限制正好強迫精簡。
- **例句**：
  - U：`口渴、頻尿、腳麻、頭暈都有`
  - B：`先確認最不舒服的兩項：頭暈與腳麻，對嗎？其他口渴、頻尿我先記著，最後摘要再一起確認。[正確｜更正]`

### E5. 按鈕與自由輸入在同一輪混用（Hybrid）

- **改哪裡**：`line_bot/app.py:_reply_text` 的 `quick_reply` 與 `TextMessage` 同發時，文案明確「可點按鈕，也可直接打字」。
- **搬運判斷**：✓ 可搬。Intercom Hybrid 與 Nest `選択肢＋自由記述` 皆為典範；LINE 官方允許 `quick_reply` 依附任意文字訊息，點後文字即為 `text`。
- **例句**：
  - `程度大約輕度、中度、重度，或 1–10 分幾分？[輕度｜中度｜重度]（也可直接打「大概 6 分」）`
  - 補充：自由輸入的 `6 分` 需正規化為 `中度`（可寫小映射表），呼應 gov.uk deterministic UI 原則。

### E6. 口語藥名走 2-attempt，但用自然句而非模板

- **改哪裡**：`schemas.py:138-141` `MEDICATION_CLARIFICATION_QUESTIONS` 兩句保留，但文案按 B3 的 `白色圓藥丸（待確認）` 語氣軟化，並與 E3 的重述整合。
- **搬運判斷**：✓ 可搬。台灣藥袋格式與日本「画像添付」同為拍照場景；LINE 已有 `MessagingApiBlob` + `MedicationBagOCRService`，法規上不推定藥名比幻覺安全。
- **例句**：
  - 第1次：`白色圓藥丸我先記「待確認」。方便的話幫我拍一下藥袋正面（有 QR 的那面）嗎？[現在拍｜手邊沒有]`
  - 第2次：`沒關係，先記待確認。記得顏色、形狀或吃藥時間也行（如白色圓形、早上吃）。[白色圓形｜早上吃｜都忘了]`
  - 2 次後：`已記「待確認」，看診時再請醫師幫你確認。`（寫入 `FHIR_MEDICATION_UNKNOWN_SUFFIX=待確認`）

### E7. 階段式進度 + 預填帶入（取代 1/8 題號）

- **改哪裡**：`line_bot/app.py` 在每次 `handle_text_message` 回覆前，依 `missing_fields` 與 `intake_snapshot` 組 `進度句`。
- **搬運判斷**：✓ 可搬。Epic `only see parts you still need` 與 Zocdoc `Save details for future visits` 可用 `SQLiteProductSessionRepository` 實現；LINE 無限制。
- **例句**：
  - `已完成：用藥、過敏 ✅ 還差：症狀（時間/描述/程度）與 想問醫師 2 段。要先補哪一段？[補症狀｜補想問醫師｜先看摘要]`
  - 續填情境（隔天回來）：`歡迎回來～上次記到「吃 metformin、無過敏、有高血壓」，要從「症狀時間」繼續嗎？[繼續｜重看摘要｜重新開始]`

### E8. 岔題給 1–2 輪 FAQ 側路，結束主動回指

- **改哪裡**：`tfda_context_gate/intake/tool.py` 新增 `handle_digression` 判斷（`什麼是/算嚴重嗎/可以吃` 等關鍵字），`workflow/graph.py` 的 Agent 3 擇一中 `REWRITE` 僅在側路內用，結束補回指句。
- **搬運判斷**：⚠️ 半可搬。騰訊/Wuhan 的 15 條跳轉規則與全局意圖監聽可抄，但台灣法規下**不可在側路給個人化處置**，僅能給 TFDA/HPA 衛教片段（本 repo `ToolContract` 僅允 `TFDA_RISK/HPA_DIET_GUIDE`）。
- **例句**：
  - U：`什麼是 SGLT2？`
  - B：`SGLT2 抑制劑是一類降血糖藥，特色是…（衛教，非處方建議，來源：TFDA 糖尿病用藥指引）。回到整理——剛剛問到「症狀從何時開始」，是三個月前對嗎？[對｜更正｜先看衛教全文]`

### E9. 常駐「更正 / 暫停整理 / 查看摘要」三鈕

- **改哪裡**：`line_bot/app.py:_quick_actions_for_status` 的 `PAUSED` 與 `NEEDS_CLARIFICATION` 分支常駐，與 `line_bot/ui.py` 的 Rich Menu `查看摘要` 連動。
- **搬運判斷**：✓ 可搬。GOV.UK `Back link` + Infermedica `menu to return` + Epic `review` 皆指向「可回頭」；LINE Quick Reply 雖點後消失，但可在每輪重發，Flex 摘要則持久。
- **例句**：每輪 Quick Reply 固定含 `[更正上一筆｜暫停整理｜查看摘要]`（第三鈕僅在已收 ≥2 欄後出現，避免早期洗版）。
- **對應**：`更正上一筆` 觸發 `handle_repair` 覆蓋單欄並重述（抄 B2 的 `change answers and return`）。

### E10. 摘要改三段 Flex 卡片，每段可點「修改此段」

- **改哪裡**：`tfda_context_gate/intake/summary.py:11-79` `build_summary` 由長文改三 bubble：`用藥與病史 / 症狀 / 想問醫師`，每段 footer 含按鈕；`line_bot/app.py:_quick_actions_for_status` 的 `NEEDS_CONFIRMATION` 轉 Flex。
- **搬運判斷**：✓ 可搬。Medical Note `主な5つの症状/詳細/相談ポイント` 與 Epic `Summary page where you can edit` 皆為分段可編輯；LINE Flex 官方支援 `bubble/carousel + button`，持久留存可回溯。法規上需保留 `disclaimer` 與 `provided/missing/待確認` Provenance。
- **例句（Flex 三段）**：
  ```
  【用藥與病史】metformin、白色圓藥丸（待確認）；無過敏；高血壓；家族無糖尿病 [修改此段]
  【症狀】三個月前起，早上空腹血糖偏高約180，中度 [修改此段]
  【想問醫師】1) 是否需調整藥物 2) 飲食原則 [修改此段]
  底部：[確認完成] [暫停整理]
  ```
  - 免責：`以上為你提供的資料整理，非診斷，實際處置請以醫師判斷為準`（沿用 `PreVisitSummary.disclaimer`）。
- **為何必要**：本 repo 現 `summary_text` 為 `；` 串接長文，Frontiers 研究證實長摘要 `NPS -12`、Symptomate 使用者 `change answers and return` 為高頻需求；分段卡片可掃描且可單段修補，降低完成前放棄。

---

## 附錄

### 附錄 1. 方法、限制與誠實聲明

- **方法**：並行 websearch（中英日文各 4–6 條，含 `site:developers.line.biz` 與 `site:gov.uk` 驗證），交叉檢查官方文件/新聞/學術三級證據；優先收「已上線 + 有規模數據 + 有截圖/原文」的案例，純行銷落地頁排除。
- **限制**：
  - 台灣醫院 LINE 案例（彰基/大林/部北）**無公開逐輪對話原文**，僅能依官方頁與新聞描述還原，已於 B4 明示「需實測」。
  - 部分中國案例（騰訊/華佗）新聞稿含官方口徑的準確率（95%+），未經第三方 RCT 驗證，標為 `B` 而非 `A`。
  - 本掃描聚焦「資料蒐集對話 UX」，不評估診斷準確率；所有涉及鑑別/科室推薦的設計，於 E 章標 `不可搬` 並保留 `D gate` 擋診斷。
- **未找到一手資料而未列入**：1925 安心專線（僅電話）、台大醫院 LINE（公開資訊僅掛號流程，無 intake 文件）、健保快易通（僅資訊查詢，無問卷）、1880 等；GOV.UK Forms 的 LINE 對應物需自建。

### 附錄 2. 關鍵 LINE / GOV.UK 官方文件（查證 2026-08-27）

- Quick Reply：https://developers.line.biz/en/docs/messaging-api/using-quick-reply/（最多 13 個，依附訊息，點後消失）
- Flex Message / elements / layout：https://developers.line.biz/en/docs/messaging-api/using-flex-messages/ / https://developers.line.biz/en/docs/messaging-api/flex-message-elements/
- LIFF：https://developers.line.biz/en/docs/liff/overview/
- GOV.UK Design System Question pages / Service Manual Form structure：https://design-system.service.gov.uk/patterns/question-pages/ / https://www.gov.uk/service-manual/design/form-structure
- GOV.UK AI Principles：https://alphagov.github.io/govuk-ai/principles-for-conversational-and-deterministic-ui/

### 附錄 3. 與前一研究報告的分工

- `preconsult_chatbot_research_20260827.md` 為學術/政府設計系統為主的「原理報告」（含 29 條 A 級證據與 10 條規則、狀態機、adversarial 測試）。
- 本報告為「**已上線產品掃描**」，專注**可直接抄的節奏與文案**（20 案例 + 5 深挖 + 10 例句），兩份互補；重構時以本報告 E 章的 10 例句為文案稿，以前報告 F 章的檔案行號為施工圖，安全不變量（B/D gates、確定性紅旗在前、不存 raw image、FHIR unknown）兩份一致。

