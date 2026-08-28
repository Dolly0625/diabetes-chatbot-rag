# 醫療 Chatbot「拒答／界外／閒聊」處理設計研究（research-only）

> **日期**：2026-08-27（研究基準 2022–2026，LINE 官方與政府設計系統加分）  
> **痛點**（本專案真實對話實測）：
> 1. 「我想睡覺」→ 誤用緊急模板「目前無法處理此請求，請改由合格醫療專業人員評估。」
> 2. 「你可以跟我說什麼」→ 同上恐怖回應
> 3. 「為什麼會有糖尿病」→ RAG 檢索到飲食衛教文件就「根據提供的資料：」串接貼出，答非所問 + 機器腔
>
> **安全不變量**：C generator 只准從 TFDA 129 + HPA 9 chunks（`TFDA_RISK / HPA_DIET_GUIDE` allowlist，`tool_contract/registry.py:28`） grounded 生成；A 分流 G/M/E/Q + 風險旗標不可繞過；LLM 不可自由生成醫療內容。本報告只改「界外與閒聊的回應方式」（話術、模板分流、呈現改寫、chunk 補強），不動安全邊界。

---

## A. 證據／慣例摘要（分類標示＋年份＋來源連結）

> **分類定義**：**研究證據** = 同儕審查論文、系統性回顧、受控實驗、隨機對照試驗；**業界慣例** = 政府／大型平台官方文件或正式設計系統、大規模維運報告；**作者建議** = 業界觀察、推論或未經對照驗證的實務建議。2022–2026 優先。

### A1. 業界如何區分「safety abort」與「out-of-scope fallback」

| # | 來源（年份、連結） | 分類 | 內容（模板差異＋語氣差異＋實例） |
|---|---|---|---|
| A1-1 | GOV.UK Chat 官方：*Guardrails* [政府文件 2024-11](https://docs.publishing.service.gov.uk/repos/govuk-chat/guardrails.html) 與 *How we're designing GOV.UK Chat* [2024-11-28](https://insidegovuk.blog.gov.uk/2024/11/28/how-were-designing-gov-uk-chat/) | **業界慣例** | 明確區分 **Input guardrail**（jailbreak、個資）與 **Output guardrail**（PII、違法／政治立場等），各自獨立 LLM 檢查；觸發時回「單一通用訊息告知觸發 guardrail」（*single message informing user they have failed a guardrail*），不告知細節，避免洩露規則。此即 safety abort 模板——**簡短、無細節、不引導追問**。 |
| A1-2 | GOV.UK Chat AI Hub / Algorithmic Transparency Record [行業報告 2025-10-07](https://www.gov.uk/algorithmic-transparency-records/dsit-gov-dot-uk-chat) / [GOV.UK Chat AI Hub](https://socialprotectionai.org/use-case/GBR-002) | **業界慣例** | 進一步區分：regex 直接擋個資（電話／email／卡號）→ 告知拒絕；檢索不到可用 GOV.UK 內容 → 不編造，附「check this answer」來源連結請使用者自行核實（*harmful vs insufficient* 的語氣差異）。 |
| A1-3 | NHS EPS Assist Me — Algorithmic Transparency Record [政府文件 2026-05-28](https://www.gov.uk/algorithmic-transparency-records/electronic-prescription-service-assist-me) | **業界慣例** | 健康小工具採 3 步 RAG（query reformulation → Bedrock KB 檢索 → grounded answer + Guardrails）；**onboarding 告知「不要貼 PII」**，命中時阻擋並提示人類覆核。此為 safety abort 實例：與 out-of-scope 的「可以幫你查文件」語氣完全不同。 |
| A1-4 | TFDA「食藥闢謠機器人」／疾管家 2.0 [衛福部官方 2018-2019/111年](https://www.mohw.gov.tw/cp-3800-43973-1.html)[https://www.mohw.gov.tw/cp-5273-71882-1.html](https://www.mohw.gov.tw/cp-5273-71882-1.html) | **業界慣例** | 台灣政府 LINE Bot 採 **關鍵字／知識庫對應**：命中則給闢謠正解＋來源；未命中則引導至「食藥闢謠專區」或旅遊門診查詢。此為 out-of-scope 範本：*承認未命中＋給替代路徑*，而非恐嚇式轉介。落差在於早期版本對閒聊無 graceful deflection。 |
| A1-5 | CallSphere “Handling Off-Topic Conversations: Graceful Deflection and Re-Engagement” [業界觀察 2025](https://callsphere.ai/blog/handling-off-topic-conversations-graceful-deflection-re-engagement.md) | **作者建議**（有 two-tier 實作範例，含 `ChitChatDeflection` / `SensitiveTopicDeflection` / `AdjacentTopicDeflection` 程式碼）| 提出 **ON_TOPIC / ADJACENT / CHIT_CHAT / SENSITIVE / INAPPROPRIATE** 五類，chitchat 給短暫友善回應＋engagement hook（「同時要不要繼續 {pending_task}？」），sensitive 給 *firm but polite boundary*，adjacent 給 bridge。此為模板差異的業界常見做法（非對照實驗）。 |
| A1-6 | Conferbot Glossary “Chatbot Fallback: Definition & How It Works” [行業百科 2026-05-30](https://www.conferbot.com/glossary/term/chatbot-fallback) | **作者建議**（彙整 Gartner/Forrester 指標） | 定義 **fallback rate 健康指標 <10–15%**，良性 tier：Level1 重述＋選項 → Level2 主題按鈕／FAQ → Level3 human handoff → Level4 自動升級；並引用 Gartner「處理不良的 fallback 是 abandon 首因」、Forrester「有智慧 fallback 多留 40% 用戶」。**非原始研究，轉引需標作者建議**。 |
| A1-7 | 本專案現況：`workflow/fallbacks.py:11-20`、`d_output_gate/gate.py:51`、`a_router/labels.py:76-89` | **業界慣例（本專案已實作）** | 唯一把 safety abort 與一般 fallback 分開的在地實例：`A_EMERGENCY/U_URGENT_HUMAN`（緊急）vs `A_BLOCKED`（政策阻擋）vs `B_INSUFFICIENT/UNSAFE`（證據不足）vs `A_DEPENDENCY/SYSTEM_DEPENDENCY`（依賴異常）。問題在於 **A_BLOCKED 與 A_DEPENDENCY 共用同一句**「目前無法處理…」，且 `O_OUT_OF_SCOPE` 非醫療風險卻被誤導向 `A_BLOCKED` 模板（見痛點 1/2 的 `workflow/graph.py:245-254` 映射）。 |

> **綜合判斷**：safety abort（E/U 類）**必須**短、威嚴、無追問、帶 119／人類轉介；out-of-scope fallback（O/Q/B_INSUFFICIENT 類）**必須**溫和、承認限制、給能力菜單／FAQ、保留上下文——兩者不可共用同一模板（違背 A1-1 GOV.UK 與 A1-5 的分流原則，且違反 D 8 步 `POLICY` vs `EVIDENCE/SEMANTIC` 的語意區分，`gate.py:15-22`）。

### A2. Out-of-scope / chit-chat 的最佳實踐：graceful deflection 與 capability menu

| # | 來源（年份、連結） | 分類 | 關鍵発見與呈現方式 |
|---|---|---|---|
| A2-1 | CallSphere 同上 [2025](https://callsphere.ai/blog/handling-off-topic-conversations-graceful-deflection-re-engagement.md) + Azure healthcare-assistant-chatbot 實測面板 [2024](https://github.com/nabankur14/healthcare-assistant-chatbot-azure) | **作者建議／實作案例** | deflection 策略：chit-chat → brief friendly + redirect；sensitive → firm boundary；adjacent → bridge；並設 `max_off_topic=3` 逐級趨嚴、重複界外時改顯性 scope 說明＋capability summary。Azure 版 48 測例中 out-of-scope 與 chit-chat 皆 100% 正確 deflection。 |
| A2-2 | GOV.UK Chat onboarding 設計 [2024-11-28](https://insidegovuk.blog.gov.uk/2024/11/28/how-were-designing-gov-uk-chat/) | **業界慣例** | 能力揭露改為 **in-chat onboarding 漸進式**（定時訊息＋「I understand / tell me more」決策點）而非靜態頁；此改動使「理解可能不準」的比例升至 ~80%（研究證據來自GOV.UK Chat 測試，非獨立論文，歸業界慣例）。Capability menu 在 onboarding 底部以兩顆按鈕呈現，研究顯示使用者對 chat 內 rich UI 反應良好。 |
| A2-3 | Taiwan HPA「長者量六力」@hpaicope LINE 案例 [LINE Biz 2025-03-17](https://tw.linebiz.com/case-study/HealthPromotionAdministration/) | **業界慣例** | 台灣政府 LINE 官方帳號實例：用 **圖文選單（Rich Menu）＋語音評估（國／台／客／英）＋一機多人註冊** 作為 capability 的常駐入口，並以每週問候圖與六力訊息維持留存，點擊率 +200%。對應本專案 Rich Menu 的 6 宮格（`line_bot/ui.py`）應擴充「我能做什麼」常駐入口。 |
| A2-4 | ChatNexus “Handling Chatbot Failures Gracefully” [2025-08-26](https://articles.chatnexus.io/knowledge-base/handling-chatbot-failures-gracefully-when-ai-does/) | **作者建議**（轉引未附方法的研究數據） | 轉引 **30% higher retention**（良設計 fallback）、**50% 更願意給好評**（從 fallback 恢復並完成任務者）。**有實證宣稱但無方法，歸作者建議**，僅作設計參考，不可當成研究證據引用。 |
| A2-5 | Liu et al. “How anthropomorphism facilitates reuse intention” *Technological Forecasting & Social Change* [2024](https://doi.org/10.1016/j.techfore.2024.123407) 等 5 篇 2023-2024 同儕研究（見文末） | **研究證據** | 在 mild 症狀情境，擬人化提升 social presence 進而提升信任與再用意願；在 severe 情境效果減弱——說明 capability menu 與適度擬人有助輕症／衛教場景的留存，但重症仍需專業信任（見 A5）。 |

### A3. Grounded RAG 去機器腔：引用 vs 改寫 vs 混合、citation 呈現、開場語

| # | 來源（年份、連結） | 分類 | 發現 |
|---|---|---|---|
| A3-1 | ALCE — *Enabling LLMs to Generate Text with Citations* [EMNLP 2023](https://arxiv.org/pdf/2305.14627) / 官方 GitHub | **研究證據** | 首次系統評「fluency / correctness / citation quality」，ALCE 顯示 **現有系統在 ELI5 約 50% 缺乏完整 citation support**；自動評估與人工 κ=0.698 高度相關。啟示：只貼 citation 不保證 correctness。 |
| A3-2 | FRONT — *Learning Fine-Grained Grounded Citations* [Findings ACL 2024](https://p.rst.im/q/aclanthology.org/2024.findings-acl.838.pdf) / ATTR FIRST *Attribute First, then Generate* [arXiv 2403.17104](https://arxiv.org/pdf/2403.17104) | **研究證據** | 採用 **select-then-generate**（先抽 grounding quote 再生成），citation 長度縮短且支援度上升；人工評審時間 **縮短近 50%**。啟示：混合式（抽句→改寫）優於整段貼。 |
| A3-3 | ReClaim—*Ground Every Sentence with Interleaved Reference-Claim* [arXiv 2407.01796](https://arxiv.org/html/2407.01796v2) | **研究證據** | 句級交錯生成 citation，**citation 準確率達 90%**，且 via constrained decoding 防止幻覺引用。此為「混合式可落地」的技術證據（但需 fine-tune）。 |
| A3-4 | AGREE—*Effective Adaptation for Grounding & Citation* [NAACL 2024](https://aclanthology.org/2024.naacl-long.346.pdf) | **研究證據** | 監督 tuning 使 LLM **自接地（self-ground）** 並產生 citation，較 few-shot ICLCITE 大幅提升 recall/precision >20%，且具 test-time adaptation（TTA）主動補證據。 |
| A3-5 | Liu et al. “WebGLM: rule-based matching to filter high-quality citation training data” 系列（[2024-07-28](https://barkingiguana.com/writing/measuring-hallucination-in-a-rag-system/) 測量 grounding relevance 雙分數） | **業界慣例** | Bedrock Guardrails 用 **grounding score + relevance score** 雙門檻；若 low grounding 為 retrieval 失敗、retrieval 有料但 low grounding 為 generation 失敗——**不可共用同一 fallback**（呼應 A1 的分流原則）。 |
| A3-6 | 本專案現況：`c_generator/deterministic_generators.py:81` `answer="根據提供的資料：" + "".join(item.claim for item in claims)` | **本專案實證** | 屬 **整段串接式（quote）**，且 `claims` 直接以 `item.content` 作為 claim（`tool.py:74`），開場語固定為「根據提供的資料：」，導致痛點 3 的機器腔與「檢索到飲食就貼飲食」的答非所問。 |

> **綜合判斷**：在禁止 LLM 自由醫療生成的約束下，可用 **規則／模板改寫** + **選擇性抽句引用** 的混合式（A3-2/3/4 證據支持）達成人話感——不是讓 LLM 自由發揮，而是讓 Deterministic Generator 依來源句做句型重組與引言替換，並以更細的 chunk 與 citation 呈現提升可核實性。

### A4. RAG 檢索答非所問與「沒有足夠資料」的誠實拒答

| # | 來源（年份、連結） | 分類 | 發現 |
|---|---|---|---|
| A4-1 | *Do You Know What You Are Talking About? Query-Knowledge Relevance* [EMNLP 2024](https://aclanthology.org/2024.emnlp-main.353.pdf) | **研究證據** | 提出 **Goodness-of-Fit 統計檢定** 偵測 out-of-knowledge 查詢，AUROC 在 GPT-4 Energy 上達 0.956–0.999，遠優於單純 embedding outlier。此為 relevance threshold 的統計學依據：**檢索分數不可等同可用性**（呼應本專案 `phase2/3` 報告「similarity 高分 ≠ 可用」）。 |
| A4-2 | *Learn to Refuse (L2R): soft + hard refusal* [arXiv 2405-2412](https://aclanthology.org/2024.emnlp-main.212.pdf) | **研究證據** | 以 **soft refusal（LLM 自判可答性）＋ hard refusal（檢索 confidence/similarity < α）** 雙保險；在 TruthfulQA 用 817 句知識庫，**hard 149 + soft 14** 拒答提升準確率 +18.5 pts。公式 `Ihard=1` iff 至少一筆 `S_i ≥ α`，閾值需 human 校準（圖 5/6 的 precision-recall 權衡）。 |
| A4-3 | *Contrastive Decoding with Abstention (CDA)* [arXiv 2412.12527](https://arxiv.org/pdf/2412.12527) | **研究證據** | 同時估計 **parametric vs contextual uncertainty**，缺料時導向 abstain；在 QA 三資料集上同步達成 accurate generation + abstention，無需額外訓練。啟示：可在 RAG 端以 entropy 量測 knowledge relevance 再決定走 abstain。 |
| A4-4 | *Do RALMs Know When They Don't Know?* [arXiv 2509.01476](https://arxiv.org/html/2509.01476v3) + *alignment-for-honesty* [NeurIPS 2024](https://papers.nips.cc/paper_files/paper/2024/file/7428e6db752171d6b832c53b2ed297ab-Paper-Conference.pdf) | **研究證據** | RAG LLM **過度拒答（over-refusal）風險**：負樣本上下文導致校準劣化；在拒答訓練中 **R-tuning／honesty alignment**（教模型對 unknown 回 idk）可緩解，但需 trade-off。對應本專案：**relevance 過嚴會誤擋衛教，過鬆會貼錯文件**。 |
| A4-5 | *RE-RAG: Relevance Estimator as Reranker + Unanswerable Classifier* [arXiv 2406.05794](https://arxiv.org/pdf/2406.05794) / *Credibility-aware Generation (CAG)* [EMNLP 2024](https://aclanthology.org/2024.emnlp-main.1109.pdf) / *RC-RAG: Risk-Control Counterfactual Prompting* [Findings EMNLP 2024](https://aclanthology.org/2024.findings-emnlp.133.pdf) | **研究證據** | RE 以 seq2seq 產生 `true/false` 重排，**precision 最佳**（召回略低於 retriever），適合做 knowledge-gap 分流；CAG 以 credibility 等級（high/medium/low） fine-tune 使 LLM 對 flawed info 具抗性；RC-RAG 以 counterfactual 估檢索品質與利用方式對風險的貢獻，明確產生 **answer + faithfulness prediction**。 |
| A4-6 | *Answering with Faithfulness (AwF)* [IJCNLP 2025](https://doi.org/10.18653/v1/2025.ijcnlp-long.56) / *Generate but Verify* [2025](https://arxiv.org/pdf/2410.11217) | **研究證據** | AwF 定義 **AwF-precision/recall** 並證明：提升信仰度預測直接提升下游 RAG 成效。Generate-then-Refine 在 WebGLM-QA/ASQA/ELI5 上顯著提升 citation 精準度。此為「誠實拒答」話術之外的量化基礎。 |
| A4-7 | 本專案現況：`b_context_gate/gate.py:28-121` 無相似度門檻；`rag/tfda_retriever.py:177-310` `similarity_search_with_score(k=5)` 直取 top-k；`phase3/phase4` 報告證實「SGLT2 四主題（酮酸中毒 vs 截肢 vs Fournier vs AKI）相似度皆高但 topic 不匹配」 | **本專案實證** | 現無 **relevance 硬門檻 + topic 檢查**，導致痛點 3「問病因卻貼飲食」的錯配在 `DeterministicContextGate`（`fixture` vs `all_retrieved`）被直接放行（`tool_contract/executor.py:235-240` 的 `hpa_retriever is None → fallback` 亦可能誤放）。 |

### A5. 閒聊（social talk）應否回應：是否提升醫療 bot 信任度

| # | 來源（年份、連結） | 分類 | 發現 |
|---|---|---|---|
| A5-1 | Safrai & Azaria “Does small talk affect ChatGPT’s medical counsel?” *PLOS ONE* [2024-04-30](https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0302217&type=printable) | **研究證據**（USMLE step3 122 題，MTurk 閒聊句） | **GPT-3.5 因 small talk 正確率 66.8%→56.6%（p=0.025），開放題 61.5%→44.3%（p=0.01）顯著下降；GPT-4 則不受影響**（83.6%/66.2% 持平）。啟示：**是否回應 social talk 會污染後續醫療推理**，與模型能力相關；本專案的 mimo-v2.5 小模型更接近 3.5 的敏感度，應採 *brief acknowledge + redirect* 而非長聊。 |
| A5-2 | Liu et al. “Anthropomorphism facilitates reuse intention” *Tech Forecasting & Social Change* [2024](https://doi.org/10.1016/j.techfore.2024.123407) + Huang & Ki “Anthropomorphic Design Cues” [IJHCI 2023-12-11](https://doi.org/10.1080/10447318.2023.2290378) + PMC “Anthropomorphic Cues & Trust” [2023](https://pmc.ncbi.nlm.nih.gov/articles/PMC10450539/) | **研究證據** | 在 mild 疾病情境，擬人化（social presence）顯著提升信任與再用意願；在 severe 情境效果減弱甚至溫暖語氣不再優於能力語氣。對糖尿衛教（屬 chronic mild-moderate）**適度擬人＋社交回應有助**，但需避免在緊急／高風險情境過度溫暖。 |
| A5-3 | Seitz “Artificial empathy: does it feel authentic?” *Computers in Human Behavior: Artificial Humans* [2024](https://doi.org/10.1016/j.chbah.2024.100067) | **研究證據** | 實驗性研究：**同理心（empathetic/sympathetic）語句提升 perceived warmth 但降低 perceived authenticity**（因使用者認為機器不該假裝有感受），進而抑制信任；**行為性同理（behavioral empathy，即 instrumental support）**則無此副作用。啟示：醫療 bot 的閒聊回應應選 **instrumental（給選項／幫你做什麼）**而非 **affective（假裝感受）**。 |
| A5-4 | JMIR Human Factors “Effects of Complexity and Persona” [2023](https://humanfactors.jmir.org/2023/1/e41017) + Furini et al. “Conversational Skills for Personalized Communications” [2024-08-07](https://doi.org/10.1145/3677525.3678693) + JMIR Review “Roles, Users, Benefits & Limitations” [2024-07-23](https://www.jmir.org/2024/1/e56930/) | **研究證據／系統性回顧** | 技術語句提高 effectiveness（OR 2.73），健康識能高者更信任（OR 2.04）；**擬人化需求因任務而異，重複閒聊與過度擬人會造成數位落差與過度依賴（over-reliance）**。 |

> **綜合判斷**：**要回應，但只回一次、只用 instrumental 語氣、並立即導回**（符合 A5-1 的污染風險控管＋A5-3 的 authenticity 原則＋A5-2 的 mild 情境擬人效益）。本專案實測「我想睡覺」本屬 benign chit-chat，卻被 `RISK_FLAG` 誤判為敏感，應走 A5 的 brief acknowledge 而非 safety abort（見 B 區 R2）。

---

## B. 界外／閒聊回應話術設計規則（8 條，每條附繁中例句）

> 依重要度排序。每條註分類與對應的 LINE 能力（Quick Reply / Rich Menu / Flex Message 引用說明見 A2-3／C 區）。

### B1. 緊急與非緊急**模板分離**，語氣差異化（研究證據＋業界慣例 → 作者落地方案）

- **原則**：`A_EMERGENCY / A_URGENT_HUMAN`（真紅旗）語氣威嚴、短句、無追問、帶 119／人類轉介；`O_OUT_OF_SCOPE / Q_CLARIFICATION / B_INSUFFICIENT`（界外／無資料）語氣溫和、承認限制、給替代路徑。
- **現況錯誤**（`fallbacks.py:12-14` 共用「目前無法處理…」）：痛點 1/2 的閒聊被映射到 `A_BLOCKED`（`graph.py:245-254`），與紅旗同語氣，造成恐慌。
- **分類**：**業界慣例**（GOV.UK Input vs Output guardrail 分離 A1-1）＋**作者建議**（具體繁中模板分離）。
- **繁中例句（界外溫和版，替代 A_BLOCKED）**：
  > 「這題目前超出我能可靠回答的衛教範圍（我只依據 TFDA／國健署的衛教文件回答）。你可以試試：① 糖尿病飲食／血糖量測／藥袋怎麼看 ② 直接點下方選單『我能幫什麼』看看範例，或輸入更具體的關鍵字（例如：『糖尿病早餐怎麼吃』）。」

### B2. 閒聊（social talk）**回一次、給選項、立刻導回**；絕不在緊急通道回閒聊（研究證據）

- **作法**：benign chit-chat（如「我想睡覺」「你好嗎」）走 `B2` 的 instrumental 風格：*brief friendly acknowledge（1 句）+ engagement hook（pending_task 或能力選項）+ 1 次容許*；`off_topic_count≥2` 再給顯性 scope 說明。
- **來源**：CallSphere `max_off_topic=3`（A2-1 作者建議）＋ PLOS ONE small talk 污染實驗（A5-1 研究證據）＋ Seitz authenticity（A5-3 研究證據）。
- **繁中例句（痛點 1 rewrite，前：恐怖模板）**：
  > **前**：目前無法處理此請求，請改由合格醫療專業人員評估。  
  > **後（首次）**：收到～想睡覺是身體在提醒休息😊 我是糖尿病衛教小幫手，沒辦法陪聊整晚，但可以幫你看「睡前血糖／夜間低血糖要注意什麼」或整理明天的看診資料。要不要選一個？`[睡前血糖小提醒] [整理看診資料] [我能幫什麼]`（1 次容許，第二次再閒聊才給 B1 溫和版）
- **反例不做**：「我懂你很累的感覺…（長篇同理）」——會降低 authenticity（A5-3）且污染醫療上下文（A5-1）。

### B3. 能力菜單（capability menu）常駐＋按鈕化，避免使用者猜測（業界慣例）

- **作法**：Rich Menu 6 宮格已具備（`line_bot/ui.py`），補強：① 把「我能幫什麼」固定在選單首格與 welcome 訊息；② 每次 fallback／界外回覆皆附 **Quick Reply**（LINE 官方支援最多 13 個〔https://developers.line.biz/en/docs/messaging-api/using-quick-reply/〕，實務 2–4 個為宜）。
- **來源**：GOV.UK Chat onboarding 按鈕化（A2-2）＋ HPA 長者量六力 Rich Menu 成功案例（A2-3）。
- **繁中例句（痛點 2 rewrite）**：
  > **前**：目前無法處理此請求，請改由合格醫療專業人員評估。  
  > **後**：我目前可以協助這幾類（都依據衛教文件）：  
  > ① 糖尿病常見問題（病因／症狀／併發症、飲食／運動）  
  > ② 用藥與藥袋怎麼看（可拍照上傳藥袋）  
  > ③ 看診前幫你整理資料、產生醫師摘要  
  > 點下方按鈕或直接輸入，例如「為什麼會有糖尿病」「血糖 180 怎麼辦」。`[為什麼會有糖尿病] [飲食怎麼吃] [上傳藥袋]`

### B4. Graceful deflection 三階梯：重述→選項→人手（graceful failure tier）（業界慣例）

- **作法**：Level1（首次界外）重述使用者原句＋給選項；Level2（連續界外）給主題按鈕／FAQ 選單；Level3（≥3 次或敏感）給人手轉介（本專案為「請改由合格醫療專業人員評估」但語氣改溫和，見 B1）。
- **來源**：ChatNexus 三階 tier（作者建議）／ Converbot fallback rate 指標 <10–15%（作者建議）——**有指標可落地但非對照實驗**。
- **繁中例句（連續界外第 2 次）**：
  > 「剛剛的『天氣如何』我沒辦法可靠回答。要不要改問糖尿病相關的？我幫你準備了幾個常見問法：`[糖尿病會遺傳嗎] [低血糖怎麼處理] [藥袋怎麼看]`」

### B5. 界外回應**保留上下文**，不重置任務（作者建議）

- **作法**：若使用者在 pre-visit intake 中岔題，需記住 `pending_task`（如「整理看診資料」），deflection 結尾帶回：`Meanwhile, shall we continue with {pending_task}?`。本專案已有 `have_intake_or_task_context` 與 `is_previsit_summary` 的分支（`workflow/graph.py`），應在界外路徑同樣透傳。
- **繁中例句**：
  > 「先記一下你剛剛的閒聊～回到原本的看診資料整理，剛剛問到『過敏史』，要不要繼續？`[繼續整理] [先看摘要]`」

### B6. 不確定時**主動澄清（disambiguation）**而非直接 fallback（業界慣例）

- **作法**：`len(normalized)<4` 或 `high risk not excluded` 等模糊輸入（`router.py:321` 已有短句→Q 分流）應給 **Yes/No 或選擇題**（如「你是指：① 睡眠與糖尿病的關係 ② 睡前血糖要怎麼量？」），而非直接 `A_BLOCKED`。
- **來源**：Gov't 機器人設計中常見的 two-stage clarification（見 A4-2 L2R soft judge 概念，作者建議落地為 UI 選擇題）。

### B7. 觸及紅旗（E/U）時**不得**用界外話術，必須維持 safety abort（研究證據＋安全不變量）

- **作法**：`RiskFlag.POSSIBLE_EMERGENCY / MENTAL_HEALTH_CRISIS / PERSONALIZED_MEDICATION`（`labels.py:67-74`）命中時，**不可**用 B1–B6 的溫和語氣，必須走 `A_EMERGENCY/A_URGENT_HUMAN` 威嚴模板（`fallbacks.py:12-13` 維持不變）。兩路徑由 `labels.RouterStatus` 的 `E_/U_` vs `O_/Q_/G_` 明確分叉（`labels.py:76-89`），日誌以 `PolicyReasonCode` 區分（`REASON_POSSIBLE_EMERGENCY` vs `REASON_OUT_OF_SCOPE`）。
- **來源**：GOV.UK 危險內容與一般不足的分離（A1-1/2，研究證據為政府維運報告，歸業界慣例）。

### B8. 變體輪替與度量：避免同一模板重複到厭煩（作者建議）

- **作法**：`A_BLOCKED/O` 類模板準備 2–3 個語氣等價變體，隨機輪替；並監控 `fallback_rate / recovery_rate / escalation_rate / abandonment_rate`（Conferbot 指標，作者建議），目標 `fallback_rate <10–15%` 且 `recovery_rate` 上升時才算改善。
- **度量原文**：CallSphere / Conferbot 皆提出 fallback rate 為健康指標（A1-6），可直接落地為 `e_observability/tracer.py` 的 `fallback_count` 擴充。

> **三痛點 rewrite 對照總表**

| 痛點 | 觸發前（現況） | 觸發後（本規則 rewrite） | 命中規則 |
|---|---|---|---|
| 1. 我想睡覺 | `A_BLOCKED`：目前無法處理…請改由合格… → 恐怖 | `Instrumental chit-chat`：收到～想睡覺是身體在提醒休息😊 …要不要選「睡前血糖小提醒」或「整理看診資料」？ | B2（研究證據：PLOS ONE＋Seitz） |
| 2. 你可以跟我說什麼 | `A_BLOCKED`：同上 | `Capability menu`：列 3 類＋按鈕 `[為什麼會有糖尿病] [飲食怎麼吃] [上傳藥袋]` | B3（業界慣例：GOV.UK＋HPA） |
| 3. 為什麼會有糖尿病 | `根據提供的資料：`＋飲食文件串接（`deterministic_generators.py:81`）→ 答非所問 | 見 C 區：人話引言＋抽句混合＋來源小標（例見 C2） | B1＋C1/C2（研究證據：FRONT/ALCE/ReClaim） |

---

## C. Grounded 但像人話的呈現模式（含「誠實說找不到」話術）

### C1. 去機器腔的模板改寫（不讓 LLM 自由生成，規則／模板即可）

**現況**（`c_generator/deterministic_generators.py:81`）：
```python
answer="根據提供的資料：" + "".join(item.claim for item in claims)
```
此為 **quote-only 整段串接**，且 `claims` 內容即 `item.content`（檢索文件片段）的直接貼上，導致：① 開場語生硬 ② 檢索錯文件就整段錯貼 ③ 無 sentence-level 歸因。

**改寫模式（混合式：select → rewrite → cite，符合 A3-2 FRONT 與 A3-3 ReClaim 的研究證據，但以確定性模板落地）**：

1. **開場語多樣化**（確定性輪替，非 LLM 自由句）：
   - 衛教通識類（`G_GENERAL_EDUCATION` 且 `INQUIRY_GENERAL_EDUCATION/DIETARY`）：`「幫你整理了衛教重點（依 TFDA／國健署文件）：」`
   - 病因／機轉類：`「關於糖尿病成因，衛教文件提到幾個面向：」`
   - 無個人化承諾：固定尾句 `「以上為衛教資訊，若有個人狀況請諮詢醫護人員。」`

2. **句型重組（規則）**：
   - 將 `item.content` 的長句按 `。；` 切句，取 **1–2 句核心**（非整段），句首加序號或小標：`「① 飲食原則：… ② 血糖監測：…」`
   - 引用標記由 `「根據提供的資料：」` 改為行內小標：`〔來源：HPA_DIET_GUIDE／TFDA_RISK〕`（ALCE/FRONT 的 fine-grained 做法，研究證據 A3-1/2）

3. **Citation 呈現**（三選一，按產品決定，皆 grounded）：
   - **行內短 citation**（推薦 LINE 文字）：每段末 `〔TFDA-0123〕`，點選可展開 `source/date/version`（`c_generator/schemas.py:138` 的 `source_table` 5 列原樣保留，與 ClinicianDraft 的 `【來源對照表】` 一致，`workflow_adapter.py` 的 `CWorkflowInput` 已有 `evidence.source/date/version/score`）。
   - **Flex Message 來源卡**（LINE 官方 Flex 支援〔https://developers.line.biz/en/docs/messaging-api/using-flex-messages/〕）：衛教本文用 Text，底部加 Separator＋Text 小字來源列，不占用對話主訊息空間（呼應 `line_bot/app.py:840` 目前一般的 `_send(reply_token, text)` 可擴充為 `messages=[TextMessage, FlexMessage]`）。
   - **Hybrid 混合**：首句為改寫摘要（模板），後括號附原文抽句 `「…（引自衛教文件）」`——對應 FRONT 的 *extractive grounding*（研究證據 A3-2），人工核實時間 -50%。

**繁中例句（痛點 3 rewrite，前：機器腔串接→後：人話混合）**：

> **前**（現況）：根據提供的資料：糖尿病飲食應…（貼 HPA 飲食文件整段，含與病因無關的飲食細節）…  
> **後（混合式，仍 grounded）**：  
> 關於糖尿病成因，衛教文件提到幾個面向：  
> ① 體質與家族史、② 長期飲食與體重、③ 胰島素阻抗等機制。  
> 以飲食為例：…（取 HPA_DIET_GUIDE 的 1–2 句核心，已核實）〔來源：HPA_DIET_GUIDE-07〕  
> 以上為衛教資訊，若有個人狀況請諮詢醫護人員。

### C2. 「誠實說找不到」的知識缺口話術（knowledge-gap response）

**觸發條件**（對應 A4-2 L2R hard refusal 的 `α` 閾值與 A4-4 GoF 檢定）：

- **Hard 門檻**：`max(S_i) < α`（檢索 confidence/similarity）或 `approved_evidence_ids` 為空 → 直接走 honesty 分支，不進 `DeterministicFixtureCGenerator` 的 `ANSWER`（`deterministic_generators.py:57-68` 的 `INSUFFICIENT` 分支）。
- **Soft 門檻**：即使有 `PASS`，若 `supported_claims` 的引用與問題 **topic mismatch**（如問病因卻只取到飲食文件，見 A4-7 SGLT2 四主題混淆），亦視為 knowledge gap。

**話術設計（分 2 層，皆溫和、給下一步，符合 A2-1/2 的 engagement hook）**：

1. **衛教缺口（`O_OUT_OF_SCOPE` 或 `Q_CLARIFICATION` 且無 approved 證據）**：
   > 「這題我目前在衛教文件裡沒有找到足夠的依據，所以先不臆測。  
   > 你可以試試更具體的問法，例如：『糖尿病早餐怎麼吃』『糖化血色素是什麼』，或點下方選單看看我能整理什麼。  
   > 若需要個人化建議，請諮詢醫護人員。」  
   > 〔附：我能幫什麼 `[常見病因] [飲食原則] [運動原則] [用藥提醒]` Quick Reply〕

2. **檢索命中但不適用（`B_INSUFFICIENT/UNSAFE`，有 `candidate_evidence` 但無 `approved_evidence_ids`）**：
   > 「我有找到一些相關文件，但沒有足夠可作證的內容來可靠回答『為什麼會有糖尿病』。  
   > 已整理的重點：…（若有 1 句可用的核心句則給 1 句，並標來源；否則不給）  
   > 想更精準，試試：『第二型糖尿病的成因有哪些』。」

> **度量**（A4-6 AwF）：以 **AwF-precision / recall** 追蹤「誠實拒答」的品質；以新增「拒答但給替代路徑」的比例作為 *recovery_rate*（A2-4），避免 over-refusal（A4-4）。

### C3. 追問（follow-up）與追溯（provenance）綁定

- 回覆尾巴固定帶 **來源小標**（`source_table` 5 列：`evidence_id / source / date / version / score`，見 `c_generator/schemas.py:138` 與 `d_output_gate/gate.py:283-285` 的表），呼應 GOV.UK Chat「check this answer」把來源連結放在答案之後的做法（業界慣例 A1-2）。
- 若為 `CLINICIAN_DRAFT`（`c_generator/deterministic_generators.py:95-319` 的 4 段結構），來源表已含且含 `disclaimer`，本節改寫僅動 `answer` 的語句重組，不動 `source_table` 與 `disclaimer`（安全不變量）。

---

## D. 對本專案的具體改法建議（標檔案行號，不動程式碼）

> **原則**：A 的 `RouterStatus` 8 路（`a_router/labels.py:76-88`：`E_/U_/M_/R_/Q_/G_/O_/F_`）已能區分緊急／界外／無知；問題在 **fallback 映射**把 `O` 與 `Q` 誤送 `A_BLOCKED` 模板，以及 **generator 呈現**把所有 `G` 都走同一句「根據提供的資料：」。下列改法皆 **determinstic / rule-based**，不新增 LLM 自由醫療生成。

### D1. `workflow/fallbacks.py:11-20` — A_BLOCKED 模板分流方案

**現況（行 11-20）**：
```python
FALLBACK_TEMPLATES = {
    "A_EMERGENCY": "偵測到可能的緊急警訊。請立即停止使用本系統，撥打119...",
    "A_URGENT_HUMAN": "偵測到需要立即由真人協助...",
    "A_BLOCKED": "目前無法處理此請求，請改由合格醫療專業人員評估。",
    "A_DEPENDENCY": "目前無法完成安全的輸入檢查，請稍後再試或改由...",
    ...
}
def fallback_response(reason: str) -> str:
    return FALLBACK_TEMPLATES.get(reason, DEFAULT_FALLBACK)  # DEFAULT_FALLBACK 見 gate.py:51
```

**具體分流建議（4 類，共用 mapping 但語氣分離，見 `a_router/labels.py:76-89` 的 RouterStatus）**：

| 觸發 `reason`（由 `workflow/graph.py:244-254` 的 `a_node` 與 `d_output_gate/gate.py` 產生） | 對應 `RouterStatus` / `PolicyReasonCode` | 建議模板 key（新增） | 語氣與內容（繁中初稿，3 變體輪替） | 安全保留 |
|---|---|---|---|---|
| `A_EMERGENCY` / `A_URGENT_HUMAN` | `E_EMERGENCY` / `U_URGENT_HUMAN` / `RiskFlag.POSSIBLE_EMERGENCY / MENTAL_HEALTH_CRISIS` (`labels.py:67-71`) | **維持不變** `A_EMERGENCY/A_URGENT_HUMAN` | 威嚴短句＋119／人類轉介，**無 Quick Reply 追問** | ✅ fail-closed，`A_EMERGENCY` 為最高優先 |
| `A_BLOCKED` + `reason_code == REASON_PROMPT_INJECTION_SUSPECTED` | `R_POLICY_BOUNDARY` (`reason_code` `REASON_PROMPT_INJECTION_SUSPECTED` `labels.py:102`) | 新增 `R_GUARDRAIL_BLOCKED` | 「這句話我沒辦法處理（可能包含系統指令）。你可以改用一般問法，例如：『糖尿病飲食怎麼吃』。」＋`[我能幫什麼]` | ✅ 不洩露規則，與 GOV.UK single-message guardrail 一致（A1-1） |
| `A_BLOCKED` + `REASON_DIAGNOSIS_OR_TREATMENT_REQUEST` | `R_POLICY_BOUNDARY` / `RiskFlag.HIGH_RISK_NOT_EXCLUDED` | 新增 `R_DIAGNOSIS_BOUNDARY` | 「關於個人診斷／處置（如要不要調整劑量），我沒辦法直接回答。已幫你整理好衛教重點（藥袋／飲食／血糖量測）或看診資料，要不要先選一個？`[飲食衛教] [藥袋怎麼看] [整理看診資料]`」 | ✅ 維持 `R_POLICY_BOUNDARY` 的封閉式邊界 |
| **`O_OUT_OF_SCOPE` 應新增的 reason**（現 `fallbacks.py` 無，導致界外被迫走 `A_BLOCKED`） | `O_OUT_OF_SCOPE` / `PolicyReasonCode.REASON_OUT_OF_SCOPE` (`labels.py:86/103`) | **新增 `O_GENERIC`**（溫和） | 3 變體輪替：<br>v1（B1）：「這題目前超出我能可靠回答的衛教範圍。我可以幫：飲食／運動／藥袋／看診整理，選一個試試？」<br>v2（B3）：能力菜單 Flex 卡<br>v3（B4-2）：主題按鈕列 `[為什麼會有糖尿病] [低血糖] [藥袋]` | ✅ 與緊急模板分離，解決痛點 1/2 的誤用 |
| `Q_CLARIFICATION` / `REASON_INSUFFICIENT_INFORMATION` | `Q_CLARIFICATION` (`labels.py:84`) | 新增 `Q_NEED_MORE` | 「我不太確定你想問哪一塊，你是指：① 睡眠與糖尿病的關係 ② 睡前血糖的衛教？點一個或重述關鍵字。」 | ✅ 不降級為 `A_BLOCKED`，走 disambiguation（B6） |
| `B_INSUFFICIENT` / `B_UNSAFE` | B 閘 `CanonicalBResult.decision` `INSUFFICIENT/UNSAFE` (`b_context_gate/schemas.py:16`) | **維持** `B_INSUFFICIENT/B_UNSAFE` 但文案改「誠實缺口版」（C2） | 現行「目前提供的資料不足以可靠回答…」保留，但**加下一步與範例問句**（C2-1/2），而非終止式語氣 | ✅ 不改 fail-closed，僅話術軟化 |
| `A_DEPENDENCY` / `F_ROUTER_DEPENDENCY` / `SYSTEM_DEPENDENCY` | `F_ROUTER_DEPENDENCY` (`labels.py:87`) | 維持 `A_DEPENDENCY/SYSTEM_DEPENDENCY` | 「目前系統無法完成安全處理，請稍後再試或改由合格醫療專業人員評估。」（**維持封閉式**，不加按鈕，避免在依賴異常時誤導重試） | ✅ 維持封閉式 |
| `social / chit-chat`（新增，A 的 `IntentTag.NON_MEDICAL` 與 `GENERAL_EDUCATION` 的交集外） | 建議新增 `IntentTag.CHIT_CHAT` 或在 `RuleBasedSignalExtractor` 先分流（見 D3） | 新增 `CHIT_CHAT` | B2 的 instrumental 1 句＋hook（見 B2 例句），**僅允許 1 次**，第二次始走 `O_GENERIC` | ✅ 不進醫療路徑，不污染 USMLE 式的醫療上下文（A5-1） |

**graph 映射修正**（`workflow/graph.py:244-254`）：
```python
# 現：reason = "A_BLOCKED" 廣泛覆蓋 O/Q
# 建議細分（仍 deterministic，不動 LLM）：
if result.router_status == RouterStatus.O_OUT_OF_SCOPE:
    reason = "O_GENERIC"
elif result.router_status == RouterStatus.Q_CLARIFICATION:
    reason = "Q_NEED_MORE"
elif PolicyReasonCode.REASON_DIAGNOSIS_OR_TREATMENT_REQUEST in result.reason_codes:
    reason = "R_DIAGNOSIS_BOUNDARY"
else:
    reason = "A_BLOCKED"
return {"status": "BLOCKED" if reason.startswith("O") or reason == "Q_NEED_MORE" else "FALLBACK", ...}
```
> `BLOCKED` vs `FALLBACK` 的狀態差異在 `e_observability/schemas.py:27-37`：`BLOCKED` 為 A/B 政策阻擋（非 D 失敗），可直接附 capability Quick Reply；`FALLBACK` 需走 D 的 `DEFAULT_FALLBACK` 兜底。

### D2. `c_generator/deterministic_generators.py:81` 與 `c_workflow_input.py` — 呈現改寫

**現況**（行 81）：
```python
answer="根據提供的資料：" + "".join(item.claim for item in claims)
```
且 `claims` 的 `claim` 直接等於 `item.content`（行 74），等於把 `tfda_retriever.py` 撈到的 `content`（含完整衛教段落）整段貼出。

**建議改寫（3 步，皆 deterministic，不調 LLM）**：

1. **來源句篩選**（對應 A3-2 *Attribute First* 的 select）：
   - 對每個 `item.content` 按 `。/；/\\n` 切句，取與 `original_query` 有詞彙重疊或 `evidence.metadata.topic == query intent` 的 **1–2 句**，其餘捨棄；若問「病因」而 `item.metadata.source_id == FOOD_NUTRITION` 則降低優先（見 D3）。
   - 行號：新增 `deterministic_generators.py:70-85` 的 `_select_sentences(content, query)`（純規則）。

2. **模板化重述**（對應 A3-1 ALCE 的 correctness 維度，不新增醫療事實）：
   ```python
   templates = [
       "幫你整理了衛教重點（依{source}）：{sentences} 〔{evidence_id}〕",
       "關於{query_topic}，衛教文件提到：{sentences} 〔{evidence_id}〕",
   ]
   answer = "\\n\\n".join(templates[i%2].format(...) for i, c in enumerate(selected))
           + "\\n\\n以上為衛教資訊，若有個人狀況請諮詢醫護人員。"
   ```
   行號：替換 `deterministic_generators.py:81` 的連接邏輯，或在 `ClinicianDraftGenerator._build_detailed_answer`（行 151-215）的 `evidence_text` 組裝前加入同樣切句。

3. **來源呈現**（行 198-204 `table_rows` 已正確）：
   - 保留 `source_table` 5 列（`evidence_id / source / date / version / score`）供 `gate.py:283-285` 最終附加；
   - 在 LINE 場景，建議 `line_bot/app.py:840` 附近同時送 **Flex Message 來源卡**（見 A3-6），而非把長表硬塞進文字訊息。

**免責聲明**：`d_output_gate/gate.py:51` 的 `DEFAULT_FALLBACK`「目前無法驗證…請改由合格…」**維持不變**，所有新規模板最終仍須通過 `run_output_gate` 的 8 步（行 151-287），特別是行 236-246 的 `check_policy_snapshot` 與 260-277 的 `HeuristicSemanticVerifier`（重疊 0.85）。

### D3. `a_router/rules.py` 與 `policy.py:DEFAULT_POLICY` — 避免「我想睡覺」進緊急通道

**現況推斷**：依 `router.py:321` 的極短句攔截與 `labels.py:68-71` 的 `RiskFlag`， benign 短句可能被 `NON_MEDICAL → O_OUT_OF_SCOPE` 或 `Q_CLARIFICATION` 正確分流，但**風險詞庫若含「睡」「休息」等廣義詞**或 `PROMPT_INJECTION` 誤判，會被拉向 `R_POLICY_BOUNDARY`→`A_BLOCKED`。本專案 `D1` review 曾記錄 `😊👍` emoji 被判 `BLOCKED`（`tool.py` 直寫入 `symptom_severity`）與 `胸口悶悶走幾步就喘` 漏攔（`workflow/graph.py` 的風險判斷）——顯示詞庫過寬／過窄皆有。

**建議**（皆規則，不動模型）：

- 在 `a_router/rules.py` 新增 **`CHIT_CHAT` 白名單**（優先於 `O`）：`["我想睡覺","想睡了","晚安","你好嗎","可以跟我說什麼","你能做什麼"]` 等 benign 短句，**先**映射為 `IntentTag.NON_MEDICAL + CHIT_CHAT`（需新增 tag），`policy_gate` 給 `O_OUT_OF_SCOPE + REASON_OUT_OF_SCOPE`（`labels.py:103`），導向 `O_GENERIC`（D1 表），而非 `A_BLOCKED`。
- `MENTAL_HEALTH_CRISIS` 僅限明確字眼（如「想自殺」「不想活」等 TFDA 心理危機詞庫），**「想睡覺」不得命中**，避免痛點 1 的恐怖模板。
- `policy.py:DEFAULT_POLICY` 維持 `RAG 僅 G_GENERAL_EDUCATION 為 True`（見 `workflow/graph.py` 的 `a_route_target`），不擴大；chitchat／O 類 `rag_allowed` 保持 False，走模板回覆而非 RAG。

### D4. `rag/tfda_retriever.py:177-310`、`hpa_retriever.py:33-381` 與 `b_context_gate/gate.py:28-121` — 解決「問病因貼飲食」的相關性

**現況**：`similarity_search_with_score(k=5)` **無硬門檻**（`tfda_retriever.py:303-321` 直取 top-k），`DeterministicContextGate` 僅檢查 `fixture_b_approved` 或 `all_retrieved`（`gate.py:86-91`），**無 replication-aware relevance**；HPA 有 3 個獨立索引（`FOOD_NUTRITION / HPA_DIET_GUIDE / HPA_DIABETES_BOOK` `hpa_retriever.py:33`），但 `MultiSourceRetriever`（`hpa_retriever.py:298-369`）僅按 score 取 max，未做 topic 過濾。

**建議（分 3 層，仍 grounded）**：

1. **Chunk 語意分段重整**（`hpa_ingest.py` 與 `tfda` 原始 `langchain_documents.json`）：
   - 把現行「飲食／營養」長段進一步按 **主題小標** 切 shallow chunk（如「病因／胰島素阻抗／家族史／症狀／飲食原則／運動原則」），每 chunk 的 `metadata.topic` 明確標示，對應 `PolicyReasonCode` 的 `INQUIRY_GENERAL_EDUCATION` vs `INQUIRY_DIETARY_EDUCATION`（`labels.py:93-96`）。
   - 現 `HPA_DIET_GUIDE` 的 9 chunks 與 TFDA 129 chunks 的 `source_id/source_dataset` 已在 `tool_contract/schemas.py:28-30` 列為 allowlist，不新增 source，僅補 topic 欄。

2. **Relevance 硬門檻 + topic 檢查**（A4-1/2/5 的研究證據落地為規則）：
   - 在 `rag/tfda_retriever.py:328` 前增：若 `max(score) < α`（建議初值 α=0.75，依 `phase2/phase3` 的 similarity 分布校準，見 `phase3_reranker_report.md:101` 的 reranker_score 0.99 誤判案例），則 **不進 B**，直接回 `INSUFFICIENT`（`gate.py:49-61` 的無證據分支），導向 C2 的「誠實缺口」話術。
   - 另增 **topic 一致性**：若 `query_intent == GENERAL_EDUCATION` 且問「為什麼」而 top-k 皆 `topic == diet`，視為 knowledge gap（即使 score 高），同樣 abstain。此即 A4-5 CAG 的 credibility-aware 思想以規則實作。

3. **B 的語意驗證加強**（`d_output_gate/verifier.py:HeuristicSemanticVerifier` 現為詞彙重疊 0.85）：
   - 維持 0.85 但對「病因」類查詢要求 **claim 與 query 的主題詞重疊**（如「胰島素」「遺傳」「肥胖」等病因關鍵詞）至少 1 個，否則標 `failed_claims`（`gate.py:136-141`），走 `SEMANTIC` 而非直接放行。

### D5. 需新增的 HPA FAQ 內容（ grounded 來源補強，不讓 LLM 自由編）

**對應痛點與缺口**（A4-7 現 9 chunks 不足以覆蓋「為什麼會有糖尿病」）：

| 新增 FAQ 主題（每主題 1–2 chunks，source_id 仍 `HPA_DIET_GUIDE/HPA_DIABETES_BOOK`，不新增 allowlist） | 內文要點（由衛教文件改寫，非 LLM 自編） | 命中關鍵字（供 `rules.py` 的 query understanding） |
|---|---|---|
| 糖尿病是怎麼來的？（成因總覽） | 第一／二型差異、家族史、生活型態、胰島素阻抗 | 為什麼、成因、為什麼會、怎麼來的 |
| 糖尿病會遺傳嗎？ | 家族風險、非單基因、可預防因子 | 遺傳、家族、會不會遺傳 |
| 睡眠與糖尿病有什麼關係？（回應「想睡覺」的延伸衛教） | 睡眠不足與血糖、夜間低血糖、睡前量測提醒 | 睡覺、睡不好、睡前、疲倦 |
| 糖尿病小幫手可以幫什麼？（capability 說明） | 3 類＋範例問句（病因／飲食／運動／藥袋／看診整理） | 可以做什麼、能幫、怎麼用、功能 |

> 每個 FAQ chunk 的 `metadata` 加 `faq_topic` 與 `is_faq=True`，在 `tfda_retriever.py` 的 `ranked` 排序前對 `query` 含對應關鍵字時 **加權 +0.05**（規則加權，不改 embedding），確保「為什麼會有糖尿病」**不會**再被飲食文件擠掉。

### D6. LINE 呈現的落地（行號）

| 能力 | 檔案與行號（現況） | 建議用法（D1/D2 的載體） |
|---|---|---|
| Quick Reply（最多 13 個） | `line_bot/app.py:840` 的 `_send(reply_token, text)` 現僅送 Text；官方〔https://developers.line.biz/en/docs/messaging-api/using-quick-reply/〕 | 界外／chitchat／缺口話術一律附 2–4 個 Quick Reply：`[為什麼會有糖尿病] [飲食怎麼吃] [上傳藥袋] [我能幫什麼]`；`O_GENERIC` 的能力菜單以 Quick Reply 承載 |
| Flex Message（卡片） | 尚未啟用；官方〔https://developers.line.biz/en/docs/messaging-api/using-flex-messages/〕 | `O_GENERIC` 的能力卡與衛教來源小卡用 Flex Bubble（主文 Text＋底部 Separator＋來源小字），避免主訊息冗長 |
| Rich Menu（6 宮格） | `line_bot/ui.py` 已有 `PATIENT_FAMILY_ACTIONS` / `SUBJECT_SELECTION_ACTIONS` 等 | 常駐「我能幫什麼」入口，內容與 `O_GENERIC` 的 Flex 卡一致（HPA 數據：點擊率 +200%，見 A2-3） |
| LIFF 前端 | `line_bot/static/patient.html` | 與本專題無直接關係，維持看診整理流程，不用於界外應對 |

---

## 附錄：本報告方法與限制

- **方法**：websearch 並行 5 主題（每主題 8–10 結果）＋ Context7 查詢 `line/line-developers-docs-source` 等官方鏡像；優先 2022–2026 同儕審查與政府設計系統；每條結論標分類與年份連結；排除純行銷落地頁（僅作作者建議引用時標示）。
- **限制**：Forrester 40% 留存、Gartner fallback 首因、ChatNexus 30%/50% 等轉引數據**無公開方法**，本報告僅作「作者建議」等級的設計參考，不列為研究證據；台灣衛福部 LINE Bot（食藥闢謠、疾管家）官方僅揭露功能與服務選單，未公布對照式可用性數據，故歸業界慣例。
- **與既有文件銜接**：本報告的 `fallbacks.py` 分流與 `deterministic_generators.py` 改寫可直接對接 `docs/plans/p1_dialog_naturalization_plan_20260827.md` 的執行清單；`phase2/3/4` 的 similarity vs usability 差異（見 `rag/phase_scripts/02-05` 報告）為 D4 的 threshold 依據。

