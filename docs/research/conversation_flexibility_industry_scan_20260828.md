# 對話靈活度與「人味」業界掃描 2026-08-28 — 為何人家的 bot 靈活、我們的僵硬

> **痛點基準**（本專案實測 5 類）：
> 1. 「你好」×2 回一模一樣的介紹（無記性／無 variation）
> 2. 「你是誰／是誰」連續兩次答非所問（白名單沒涵蓋變體）
> 3. 「我不知道啊」誤觸發 intake 流程（完全斷線的亂接話）
> 4. 使用者抱怨「你好不人性化噢」→ 冷回「這個我幫不上」
> 5. 罐頭模板重複率高，沒有對話狀態感
>
> **安全不變量**（本報告所有建議皆不動）：B/D gate 不可繞過、證據僅 TFDA 129 + HPA 9 chunks（`tool_contract/registry.py:28` allowlist `TFDA_RISK/HPA_DIET_GUIDE`）、醫療內容 grounded、A 分流 `G/M/E/Q + R/Q/O/F` 與 `RiskFlag` 不可繞過、raw image 不進 `WorkflowState`、PII hash。
> **方法**：websearch 英文為主兼中文（2023–2026 優先，官方文件 > 論文 > 業界部落格）並行 4 線（背景任務 `bg_bb1fc846` / `bg_209e0fe2` / `bg_61565d66` / `bg_582e7f14`）+ 本地 codebase 抽樣。分類見附錄 B。

---

## 0. 本專案現況速寫 — 為什麼僵硬（codebase 證據）

> 抽樣路徑皆 `tfda-diabetes-agent/`，由 explore 子代理（`bg_bb1fc846`）逐檔定位。

### 0.1 `line_bot/app.py` — 入口去重與單一罐頭歸一

| 行號 | 發現 | 對應痛點 |
|------|------|----------|
| L41-43 | 罐頭常量 `HONEST_FALLBACK_PUSH_TEXT="這題我還沒整理出可靠的回答..."` / `QUEUED_FALLBACK_TEXT` / `ASYNC_PLACEHOLDER_REPLY="幫你查衛教資料中..."` | 痛點 4、5 |
| L49-87 | `TEXT_DEDUP_TTL_S=120`, `_normalize_text(NFKC+lower)` + `_is_text_duplicate(userId,norm)` + `_text_dedup[(userId,norm)]=ts`。`「你好」×2 在 120s 內 → 第二次直接 `TEXT_DEDUP_REPLY="這題正在幫你查了，稍候"` 不走 workflow | 痛點 1 |
| L433-467 | `_format_formal_push_text`：`status==FALLBACK 且 reason∈{B_INSUFFICIENT,FORMAL_TIMEOUT,C_FAILURE,SYSTEM_DEPENDENCY,B_UNSAFE}` 統一 return `HONEST_FALLBACK_PUSH_TEXT`，不帶原因分流 | 痛點 4 |
| L1097-1157 | Text 分支：`_is_duplicate_push(eventId) → "此訊息已在處理中"`；`use_formal && _should_use_async_formal(text)` 時先判去重→`TEXT_DEDUP_REPLY`，再 `_send(ASYNC_PLACEHOLDER_REPLY); _schedule_formal_push(...)` | 痛點 1、5 |
| L1159-1190 | Image 分支：`MessagingApiBlob` 下載，空則「無法取得圖片」，否則 `orchestrator.handle_image` → OCR，**raw bytes 永不進 `WorkflowState`**（合規保留） | — |

### 0.2 `workflow/runner.py` — 窄路徑 + 超時降級

- **L32-82 `_is_formal_eligible`**：`pre_visit_intake→False` / `is_red_flag→False` / `is_pre_visit_intake_text→False` / `is_chit_chat_text→False` / `len<4或含"怎麼辦"→False` / `"可以跟我說什麼"→False` / `policy_gate!=G_GENERAL_EDUCATION→False` 才走 formal。**白名單僅 `G_GENERAL_EDUCATION` 走 RAG+LLM，其餘走 45ms Fixture 快路 → 直接罐頭**。

- **L234-238**：`FORMAL_WORKFLOW_TIMEOUT_S=45`（`ThreadPoolExecutor`，env 可覆蓋），超時 → `FALLBACK(FORMAL_TIMEOUT)` → `HONEST_FALLBACK_PUSH_TEXT`。
- **L119-141 `stream_workflow`**：先 `run_workflow` 完整過 D，再 `buffered_stream_after_d(d_pass=COMPLETED)` 切塊 → **D PASS 前不推首字**，false streaming。

### 0.3 `workflow/graph.py` + `intake_router.py` — 白名單過窄的 welcome 陷阱

| 行號 | 發現 |
|------|------|
| L16-22, L38-47 | `WELCOME_MESSAGE`；`is_welcome_trigger: len(normalized)<4 && in("你好","您好","hi","hello","嗨","哈囉","開始","help","？")` 或 `""` → `COMPLETED + WELCOME_MESSAGE`。**「你好」必進 welcome，無 session 記憶差異 → 重複同一 3 行歡迎詞** |
| L43-60 | `_CHIT_CHAT_RE / _CAPABILITY_RE`：`想睡\|睡覺\|晚安\|無聊\|累了\|你好嗎\|早安` — **不含 `你是誰\|你叫什麼\|不人性化`** |
| L239-272 | G2 whitelist 短路：`is_chit_chat_text(raw) → reason=O_GENERIC or CHIT_CHAT_OUT_OF_SCOPE → BLOCKED + fallback_response(reason)`。**「你是誰/不人性化」未命中 chit_chat 但命中 `O_OUT_OF_SCOPE` 仍映射同一 `O_GENERIC` 罐頭，無自介** |
| L298-323 | `reason` 映射：`E→A_EMERGENCY, U→A_URGENT_HUMAN, F→A_DEPENDENCY, O+chit→CHIT_CHAT_OUT_OF_SCOPE else O_GENERIC, Q→Q_NEED_MORE, R+注入→R_GUARDRAIL_BLOCKED, R+診斷→R_DIAGNOSIS_BOUNDARY, M→A_BLOCKED` — **所有非 G 皆 `BLOCKED/FALLBACK + 罐頭`，無 LLM 改寫** |

### 0.4 `a_router/*` — 8 路白名單

- **`policy.py:43-134 `policy_gate`**：優先級 `注入>急症>緊急轉真人>個人化用藥>一般藥物資訊>診斷>超範圍>需釐清>一般衛教`，**僅 `G_GENERAL_EDUCATION rag_allowed=True`**，其餘 `END/FALLBACK`。
- **`rules.py:80-142`**：`_chit_chat` 正則 `想睡覺\|想睡了\|無聊\|你好\|哈囉\|晚安\|你好嗎\|嗨\|你可以跟我說什麼` — **「你是誰/你是AI嗎」不在清單 → `intents=[] → Q_CLARIFICATION → Q_NEED_MORE` 罐頭**；`_diabetes_scope`（糖尿病|血糖|胰島素|飲食|運動）決定 `GENERAL_EDUCATION`，無則 `O`。

### 0.5 `b_context_gate/gate.py` — 登入前拒絕

`evaluate` 分支1 `evidence==[]→INSUFFICIENT`；分支2 `duplicate_id→UNSAFE`；分支3 `approved==[]→INSUFFICIENT` else `PASS`。短句/閒聊/無命中 → `B_INSUFFICIENT → "這題我手上的衛教資料不夠，建議看診時問醫師。"`（`fallbacks.py:11-26`）。

### 0.6 `d_output_gate/*` + `workflow/fallbacks.py` — 15 罐頭清單

`fallbacks.py:11-26` 精要（`bg_bb1fc846` 實摘）：`A_EMERGENCY(119)` / `A_URGENT_HUMAN` / `A_BLOCKED(醫師評估)` / `CHIT_CHAT_OUT_OF_SCOPE(這個我幫不上，不過我可以：🥗📋💊)` / `Q_NEED_MORE(可以多說一點嗎？)` / `O_GENERIC(超出範圍...)` / `R_GUARDRAIL_BLOCKED` / `R_DIAGNOSIS_BOUNDARY` / `B_INSUFFICIENT(資料不夠)` / `B_UNSAFE` / `C_FAILURE` / `SYSTEM_DEPENDENCY` / `FORMAL_TIMEOUT(HONEST)` / `DEFAULT_FALLBACK(d_gate:51)`。`fallback_response(reason)` 未知回退 `DEFAULT_FALLBACK`。`d_output_gate/gate.py:151-288` 8 步（適配→A快照→B證據→C形狀→evidence歸屬→A紅線→棄權→語意驗證 `HeuristicSemanticVerifier 詞重疊≥0.78`），任一步失敗 → `FALLBACK + DEFAULT_FALLBACK` 固定字串。

### 0.7 `intake/*` — 誤觸發點

- **`intake/tool.py:82-83,242-247 `UNCERTAIN_PATTERNS=不知道|不記得|忘了|不確定|不清楚|沒印象`**，`is_uncertain_answer` 命中 `不知道`。**`「我不知道啊」在 `intake_active` 時被 `orchestrator._normalize_intake_answer(L1016)` 先攔：`UNCERTAIN_RE→SYMPTOM_UNKNOWN_VALUE="待確認"+SYMPTOM_UNKNOWN_QUESTION` → 直接寫入當前 `pending_field`（如 `allergies`），污染欄位**。
- **`tool.py:681-762` 抽取**：`_extract_allergies` 等硬關鍵詞，`「我不知道啊」→ candidates={} → valid={} → fallback direct 寫 pending`。
- **`graph.py:360-403`**：`medication_clarification_attempts<2 → NEEDS_CLARIFICATION` 追問，≥2 才標 `待確認` 推進。

### 0.8 Session Memory — 有存，但不進生成

- `product_session/schemas.py:33-54 ProductSession`：`conversation_context` + `intake_snapshot(8欄)` + `pending_field/question` + `system_risk_classification` + `version(樂觀鎖)` + `expires_at(7天)`，`SQLiteProductSessionRepository(data/processed/line_sessions.sqlite3)`。
- `conversation/manager.py:77-245 ConversationContextManager`：`create → append_turn(user/assistant) → apply_structured_updates(僅白名單欄位) → evaluate/compact(丟棄舊 turns 保留最近N組)`，`build_model_context` 僅輸出 `clinical_state + recent_turns`，**未注入 `CGenerator` prompt**（C 輸入僅 `approved_evidence_ids`），`runner.py:L172 previous_attempts=[]` 每 run 重建 → **重複「你好」無上下文差異**。
- 雙份 `_text_dedup(120s)+_pushed_events` 記憶體去重 → 同句重發回固定去重語，非差異化。

### 最僵硬 5 根因

1. **G2 白名單過窄**：`a_router/rules._chit_chat` 與 `graph._CHIT_CHAT_RE` 僅列 `你好/晚安/想睡`，`你是誰/不人性化` 漏表 → `O_GENERIC/Q_NEED_MORE` 罐頭答非所問。
2. **單一模板歸一**：15 罐頭 + `DEFAULT_FALLBACK` + `HONEST_FALLBACK_TEXT` 皆定字串，無 `{user_text}` 插值或 LLM 潤色 → 同句必同回。
3. **120s 文本去重鎖**：`(userId,NFKC)` 去重直接回 `這題正在幫你查了，稍候`，不看語境 → 重試被判重複。
4. **Intake 不確定詞過寬**：`UNCERTAIN_PATTERNS` 在 `ACTIVE` 態把 `我不知道啊` 當欄位值 `待確認` 寫入並推進 → 誤觸發污染。
5. **上下文不注入生成**：`recent_turns` 存於 `ProductSession` 但 `CGenerator` 僅吃 `approved_evidence_ids` → 無跨輪小聊記憶。

---

## 1. Small talk / Chitchat 設計模式

### 業界做法

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 1-1 | **Dialogflow ES Built-in + Prebuilt Small Talk** | 閒聊從 task intent 分離：Built-in 一鍵啟用自動處理 casual conversation，Prebuilt Import 產生 100+ intents 可按品牌客製回覆，Global region 限制下 Global agent 才可用 | [Dialogflow ES Small Talk 官方](https://docs.cloud.google.com/dialogflow/es/docs/agents-small-talk) / [Agents Design](https://docs.cloud.google.com/dialogflow/es/docs/agents-design) | **業界慣例 高** |
| 1-2 | **Rasa ResponseSelector + RulePolicy retrieval** | `retrieval_intent: chitchat/faq` 收斂 20+ 子意圖為單一 `utter_chitchat`，embedding 檢索最匹配 canned response，支援一對多 random sampling 避免罐頭感，與 stories 隔離 | [Rasa 3.x Chitchat and FAQs](https://legacy-docs-oss.rasa.com/docs/rasa/chitchat-faqs/) / [Rasa Blog Response Retrieval](https://rasa.com/blog/response-retrieval-models) | **業界慣例 高** |
| 1-3 | **Microsoft Personality Chat（ lexicon + CDSSM 雙層）** | lexical + semantic CDSSM 先判是否 small talk，再以子意圖（greetings/compliments/opinions）從 curated editorial library 選 persona 回覆，必要時 DNN 生成；預設 Professional/Friendly/Witty 三 persona | [Project Personality Chat Overview](https://github.com/microsoft/cognitive-research-technologies-docs/blob/master/project-personality-chat/overview.md) / [BotBuilder-PersonalityChat SDK](https://github.com/ntulsi/BotBuilder-PersonalityChat) / [CUX Guide](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-design-principles?view=azure-bot-service-4.0) | **業界慣例 高** |
| 1-4 | **Microsoft Writing for Bots — 邊界與信任** | 指引要求：誠實揭露 Bot 身分、限定任務範圍（不暗示 Ask me anything）、預先設計 fallback & human handoff、容忍拼寫錯誤、預判惡搞/重複提問 | [Writing for bots](https://learn.microsoft.com/en-us/style-guide/chatbots-virtual-agents/writing-bots) | **業界慣例 高** |
| 1-5 | **Character.AI — PipSqueak2 + Memory + Lorebook** | 2026 組合：PipSqueak2 強調 in-character + writing variety，Memory 重整擴大 tracking（髮型/瞳色/quirks 可視化存量），Lorebook 依 keyword-trigger 按需注入避免 Definition 膨脹，三層 `Definition常駐 + Lorebook按需 + Rolling window` 是自然感核心 | [PipSqueak2 & Memory Apr 2026](https://blog.character.ai/pipsqueak2-and-more/) / [Lorebook](https://blog.character.ai/lorebook) / [Lorebook Help Center](https://support.character.ai/hc/en-us/articles/52739596326811-Lorebooks) / [架構分析 sliding window ~8k](https://konshus.ai/character-ai-memory) | **業界慣例 中高** |
| 1-6 | **Replika — Long-term Memory + 可調 Personality + 回饋** | short-term context + long-term memory（自動抽事實/偏好）+ safety filter，性格滑條（健談/幽默/智慧）+ 訊息反應作 RLHF，日記 memo 作 store；自然感來「記得你」 | [Replika memory](https://help.replika.com/hc/en-us/articles/37208679176077-How-does-Replika-s-memory-work) / [Personalities Guide](https://www.funfun.ai/blog/replika-personalities-guide) / [繁中對照](https://replikazh.com/use.html) / [CharacterApp 中文](https://www.characterapp.cn/) | **業界慣例 中** |

> **為什麼有效（設計原理）**：把 small talk 視為獨立 retrieval 層而非主任務分支，關鍵 **intent insulation**：先用 high-level chitchat classifier 分流避免污染 task NLU；再以 **retrieval（ResponseSelector / editorial library / Lorebook keyword）**而非純生成保證 persona 一致與可審計，同時用 **variety（多模板隨機、PipSqueak2 多樣寫作、DNN 生成）**與 **memory（Pinned Memories / Lorebook / long-term graph）**打破罐頭與失憶感，達到「禮貌承接 off-topic、快速回到主線」。

---

## 2. 人格與語氣設計（Persona & Tone）

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 2-1 | **OpenAI Realtime Prompting — #Personality & Tone 區塊** | 官方建議 system prompt 拆為 `# Role & Objective / # Personality & Tone / # Instructions / # Safety`，Tone 明訂 Personality/Tone/Length(2-3句)/Pacing/Language + bullet + ALL CAPS 強調關鍵規則 + sample phrases few-shot | [Realtime Prompting Guide](https://developers.openai.com/cookbook/examples/realtime_prompting_guide) / [鏡像](https://cookbook.openai.com/examples/realtime_prompting_guide) | **業界慣例 高** |
| 2-2 | **OpenAI Prompt Personalities 範本庫** | 10+ 預設 persona 完整 instruction（Concise/Empathetic/Formal），以 voice attributes（formality/enthusiasm/emotion/filler words）組合穩定語氣 | [Prompt Personalities](https://developers.openai.com/cookbook/examples/gpt-5/prompt_personalities) | **業界慣例 高** |
| 2-3 | **System Prompt 6 要素模板** | Identity（你是誰）/ Behavior（怎麼幫）/ Constraints（不做什麼，含 do/don't）/ Style（用詞句長格式）/ Edge Cases（模糊/超能力/敏感 fallback）/ Example Interactions（理想對話） | [Structured Prompting Handbook Ch.10](https://zettai-seigi.github.io/StructuredPromptingHandbook/chapters/10-system-prompts/) / [UseInvent Template](https://www.useinvent.com/blog/instructions-aka-system-prompt-template-for-your-personal-assistant-best-practices-2025) | **作者建議 中高** |
| 2-4 | **Microsoft — Tone 依情境調節** | 帳務/資安要 empathetic but brief and straightforward，一般開戶可較輕鬆，Xbox 可 lighthearted；同時要求誠實揭露身分、承認錯誤給下一步、專注用戶利益 | [Writing for bots](https://learn.microsoft.com/en-us/style-guide/chatbots-virtual-agents/writing-bots) / [Chatbots overview](https://learn.microsoft.com/en-us/style-guide/chatbots-virtual-agents/) | **業界慣例 高** |
| 2-5 | **Mayo Clinic — Care Moment + Microcopy Empathy** | 圍繞單一 care moment「幫你判斷是否該看醫生」，語氣 calm, direct, trustworthy；microcopy `Invalid input → I’m sorry, I didn’t quite catch that. Could you tell me a bit more?` | [Mayo First Aid voice案例](https://patientexperience.wbresearch.com/blog/mayo-clinic-google-assistant-voice-powered-web-chat-strategy-health-wellness-information-to-at-home-patients) / [Conversational AI in Healthcare](https://www.clustox.com/blog/conversational-ai-in-healthcare) / [Adobe 訪談](https://blog.adobe.com/en/publish/2024/02/28/how-mayo-clinic-applies-empathy-digital-transformation) | **業界慣例 中高** |
| 2-6 | **Woebot / Youper / Wysa — 校準同理（Calibrated Empathy）** | 心理 bot 教訓：**instrumental support（給工具）優於 experiential「我懂你」**，否則觸發 authenticity gap；Woebot CBT 為 supportive, non-judgmental, encouraging（RCT 2 週降憂鬱）；一律邊界「不做診斷、鼓勵就醫」 | [Woebot Health](https://woebothealth.com/) / [Youper 介紹](https://intuitionlabs.ai/software/telepsychiatry-digital-mental-health/chatbots-and-ai-therapy-assistants/youper) / [10 Key Examples 含 Babylon 崩潰對照](https://intuitionlabs.ai/pdfs/ai-chatbots-in-healthcare-a-review-of-10-key-examples.pdf) / [Artificial empathy 論文](https://www.sciencedirect.com/science/article/pii/S2949882124000276) / [PETS 量表](https://ceur-ws.org/Vol-4221/short14.pdf) / [Babylon 事件](https://oecd.ai/en/incidents/2020-02-26-bc4b) | **研究證據 + 業界慣例 高** |

> **為什麼有效**：本質是 **Voice（不變品牌個性）vs Tone（隨情境調節）** 分離。有效 prompt 靠 **Identity anchoring（週期性強化「你是誰」）+ Behavioral examples（do/don't 對照）+ Explicit boundaries（何時說「我不確定，建議諮詢醫師」並 hand-off）** 錨定一致性，並用 Length/Pacing/Variety 防冗長與罐頭；醫療場域 **warmth 是信任前置（perceived warmth → trust → intention to use）**，但必須 **calibrated**：以 microcopy 修辭傳遞關懷而非 `我感同身受`，保持 warm, professional, compassionate 平衡與 transparent limits。

---

## 3. 對話狀態與記憶：如何避免罐頭重複

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 3-1 | **Response Variation Pools（同義句池 + 隨機抽樣）** | 每個 DialogueAct×Style 預建 3-5 句 paraphrase，`random.choice/choices` 保證無重複，回填 `{slot}`，20 模板 + 條件分支即可 | [Phrasing Pools discussion](https://github.com/web3guru888/asi-build/discussions/478) / [隨機池實作](https://www.ipipp.com/html/20260625/15935.html) / [Sprinklr Bot Reply Node variation](https://www.sprinklr.com/help/articles/nodes-in-a-dialogue-tree/bot-reply-node/63da6336405d8861d65aa120) | **中** |
| 3-2 | **Contextual Response Rephraser（LLM 改寫罐頭，保事實）** | 以模板為語意錨點，LLM 僅 `rephrase` 保留 slots，注入 `{{history}}`，`temperature 0.3`；Rasa 建議 `rephrase: True` 逐句或 `rephrase_all` 全域+法規句例外鎖定 | [Rasa Contextual Rephraser](https://rasa.com/docs/reference/primitives/contextual-response-rephraser/) **高** / [Assistant Tone](https://rasa.com/docs/pro/customize/assistant-tone/) **高** / [P2-Net 論文](https://ar5iv.labs.arxiv.org/html/2008.03391) **高** | **業界慣例+研究證據 高** |
| 3-3 | **De-duplication：Seen-set + Sliding Blacklist + 懲罰參數** | `temperature 0.8-1.0 + frequency_penalty 0.3-0.8 + repetition_penalty 1.05-1.2 + top_p 0.9` + 最近 5 回合 blacklist；n8n 注入 `timestamp/execution_id/random seed`，前 50 字重合即重試 | [n8n 去重實戰](https://www.rapidevelopers.com/n8n-tutorial/how-to-stop-repeated-answers-from-a-language-model-in-n8n-workflows) / [Dynamic responses](https://blog.com.bot/generating-dynamic-responses-in-chatbots-techniques/) / [騰訊雲重複根因](https://developer.cloud.tencent.com/article/2515584) | **中** |
| 3-4 | **Session Memory：Sliding Window 3-5 turns 精華 + 10-20 全量** | LangChain `ConversationBufferWindowMemory k=5`（預設）、LlamaIndex `ChatSummaryMemoryBuffer`、Prisme `working_memory 4000 tokens`；最近 3-5 turns verbatim + 更早摘要，70% 引用落在最近 3 turns，20 turns 覆蓋 92% 需求 | [LangChain k=5](https://sj-langchain.readthedocs.io/en/latest/memory/langchain.memory.buffer_window.ConversationBufferWindowMemory.html) **高** / [Prisme 三層](https://prismeai.mintlify.app/products/agent-factory/memory) **高** / [Head-tail eviction](https://jatinbansal.com/ai-engineering/short-term-memory/) / [架構綜述 hybrid window+summary+RAG](https://artificial-intelligence-wiki.com/conversational-ai/dialogue-management-and-context/conversation-memory-architectures/) / [多輪 prompt 錨點](https://www.flowpixai.com/prompt-engineering/multiturn-conversation-prompt-design.html) | **業界慣例 高** |
| 3-5 | **Token-budget Adaptive Injection + Compact 4 階段** | CALMem MOIM：`r=current_tokens/window`，`r≥0.8` 抑制 episodic 注入；Hermes 50% agent compressor + 85% gateway safety net（prune → align → LLM summary middle → reassemble） | [CALMem 論文](https://arxiv.org/html/2605.20724v1) **高** / [Hermes/Claude 壓縮](https://mem0.ai/blog/how-hermes-and-claude-handle-context-compression-in-real-production-agents-(and-what-you-should-extract)) / [Fim 5 層 defense](https://docs.fim.ai/architecture/context-management) | **研究證據 中高** |
| 3-6 | **三層記憶增量/壓縮雙 Prompt（最簡可存活）** | `rolling_summary≤300字 + key_qa_pairs 6對 + verbatim 最近2 turns ≈2-3K tokens 恆定`，增量每輪跑、壓縮每 4 輪 | [Simplest survivable memory](https://blog.sourceshift.io/p/the-simplest-survivable-form-of-chat-memory/) / [GenAI Patterns](https://www.genaipatterns.dev/patterns/memory/conversation-memory) / [動態 temperature -62%](https://intelliparadigm.com/article/weixin_28718487/2166594) | **中** |

> **為什麼有效**：人類容忍同義不同形，零容忍逐字重複。Pool 保下限可控，Rephraser 提上限自然度；seen-set/參數懲罰作確定性去重；3-5 turns verbatim 解 pronoun resolution（「那個呢？」），token-budget + 滾動摘要解 context rot 與成本線性膨脹，組合技比單一技巧穩定。

---

## 4. Unknown / Miss 時的優雅 fallback（Graceful Fallback / Repair）

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 4-1 | **Rasa FallbackPolicy / TwoStageFallbackPolicy** | `nlu_threshold 0.3-0.4 + core_threshold 0.3` 觸發 `action_default_fallback(UserUtteranceReverted)`；TwoStage `ask_affirmation(按鈕列 top intent) → ask_rephrase → ultimate fallback(handoff)` | [Rasa fallback docs](https://github.com/RasaHQ/rasa_core/blob/master/docs/core/fallbacks.rst) / [legacy 0.14.5](https://legacy-docs.rasa.com/docs/core/0.14.5/fallbacks/) / [Failing gracefully](https://medium.com/rasa-blog/failing-gracefully-with-rasa-8ead6b43f2f4) / [Error flows demo](https://chatbotdesign.substack.com/p/rasa-implementing-error-flows-and) | **高** |
| 4-2 | **Dialogflow CX Generative Fallback** | 在 `sys.no-match-default` 掛 LLM，prompt 注入 `$conversation + $last-user-utterance + $flow-description + $route-descriptions`，生成失敗退回預設並過 banned phrases | [Generative Fallback 官方](https://docs.cloud.google.com/dialogflow/cx/docs/concept/generative-fallback) / [Ultimate guide](https://medium.com/google-cloud/the-ultimate-guide-to-using-generative-fallbacks-in-dialogflow-cx-4ccd6d60512) | **高** |
| 4-3 | **修復三件套：提示+引導+下一步** | 好 fallback = 1)提示「沒太懂」2)引導「可試『預約/營業時間/客服』」3)下一步「帶回主選單」；反對單句 “Sorry I don't know” | [FIRST LINE 三節點](https://blog.firstline.cc/chatbot-design-3-principles/) / [UXPlanet](https://uxplanet.org/perfecting-the-chatbot-fallback-experience-f76d119c45d4) / [Botpress](https://botpress.com/tw/blog/conversation-design) | **中** |
| 4-4 | **Context-sensitive Fallback + 重試計數器 + 自動升級** | 用 `fallback intent` 帶 input context（預約流 vs 閒聊流不同話術），`fallback_retry_count≥2` 自動轉真人/精簡表單；診所 KPI：意圖準確率≥80%、Fallback<10%、Bounce<40% | [Rasa context-sensitive](https://forum.rasa.com/t/fallback-intents-for-context-sensitive-fallbacks/963) / [診所 5 步容錯](https://blog.firstline.cc/clinic-chatbot-case-study-5-steps/) / [轉真人設計](https://www.brightalk.ai/blog/ai-phone-handoff-to-human-design) | **中** |
| 4-5 | **決策透明 + 結構化 Escalation Payload** | clarification 用 `request_clarification` tool（非自由文本），escalation 載 7 欄位（case id/摘要/假說+信心/觸發原因/過濾證據/2-3 建議行動+可逆性） | [CCA-F 範式](https://examlab.net/zh-tw/certs/anthropic/cca-f/topics/escalation-and-ambiguity-resolution) / [Agent UX 錯誤恢復](https://cheesecat.net/blog/agent-ux-dialogue-design-production-2026-zh-tw/) | **中** |

> **為什麼有效**：使用者能忍「不懂」，不能忍「不懂又不幫忙」。兩段式確認給修正機會，生成式 fallback 給長尾覆蓋，三件套保證每次 miss 都給可點擊出路，結構化升級保證最終有人接住。業界共識：**fallback 不是道歉，而是引導**。

---

## 5. 情緒偵測與共情回應

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 5-1 | **Frustration / Sentiment 雙層偵測** | VADER 快掃 lexicon + BERT/BiLSTM 分類 + LLM 兜底；`score -1~1` + `frustration 0~1` 追蹤 trend；BERT 準率 93.1%，Frustration LLM 版 F1 +16% vs keyword | [Nature BERT+BiLSTM](https://preview-www.nature.com/articles/s41598-025-15501-y) **高** / [Coling Frustration](https://aclanthology.org/2025.coling-industry.23/) **高** / [IJERT BERT](https://www.ijert.org/sentiment-analysis-for-personalized-chatbot-in-e-commerce-using-transformer-bert-ijertv15is020149) / [Callsphere SentimentScore](https://callsphere.ai/blog/sentiment-analysis-customer-support-detecting-frustrated-users-escalation.md) | **研究證據 高** |
| 5-2 | **道歉→安撫→給選項 三段式（醫療不過界）** | `Acknowledge(具體) → Validate → Clarify → Offer path`，避免「我理解你的沮喪」空泛，改「聽起來這已經困擾你一陣子了」+ 2-3 下一步；experiential empathy 會被判不真誠，改 behavioral | [Empathy Ladder](https://callsphere.ai/blog/vw7d-voice-agent-handling-angry-callers-2026.md) / [ScienceDirect Artificial Empathy](https://www.sciencedirect.com/science/article/pii/S2949882124000276) **高** / [Twente 投訴](https://essay.utwente.nl/fileshare/file/95117/Pompe_MA_EEMCS_2.pdf) / [Rote/Explanatory/Empathic](https://arxiv.org/html/2507.02745v1) / [洋洋 CBT 設計](https://www.bamboodd.com/article/AI%E8%99%9B%E6%93%AC%E7%99%82%E7%99%92%E5%B8%AB%E6%B4%8B%E6%B4%8B%EF%BC%9A%E6%88%91%E5%80%91%E6%80%8E%E9%BA%BC%E8%A8%AD%E8%A8%88%E5%AE%83%EF%BC%9F) | **高** |
| 5-3 | **Woebot/Wysa — 先驗證，再給工具（不越界）** | `validation-before-advise`：先 reflective validation 再給 CBT/DBT/mindfulness；Wysa affect labeling（emotion+intensity），Woebot emoji-scale；用 memory callback 連續敘事 | [Woebot AI core](https://woebothealth.com/ai-core-principles/) / [Technology](https://woebothealth.com/technology-overview/) / [Wysa FAQ](https://www.wysa.com/faq) / [Loop scorecard](https://aimentalhealthadvisor.com/blog/woebot-vs-wysa-loop-by-loop-cbt-scorecard-no-head-to-head-data.php) / [EHDChat 多策略](https://aclanthology.org/2024.sicon-1.10.pdf) | **高** |
| 5-4 | **閾值與路由：冷回 vs 過度道歉的平衡** | 起始 `frustration 0.75 / sentiment -0.6` 且連續 2 輪負面才升級，目標升級率 5-15%；醫療明確標「非真人/非醫療建議」，3000+ 次/專案級已導向實體資源 | [三級路由](https://dev.to/ismail_zamareh_d099419122bc4f/taming-the-digital-temper-building-ai-agents-that-actually-de-escalate-frustration-27d8) / [Sentiment 僅內部路由](https://supp.support/blog/sentiment-analysis-customer-support) | **中** |
| 5-5 | **同理的可驗證性（SLEEC + Formal Verification）** | 將同理視為 non-functional requirement，用 SHA+SMC 驗證 `therapist.symp_reasoning` 可達、終態正向 | [Verified Empathy 2601.08477](https://doi.org/10.48550/arxiv.2601.08477) | **研究證據 高** |

> **為什麼有效**：抱怨者要「被聽見」先於「被解決」。雙層偵測提供可調閾值；三段式保證道歉有具體指涉、出路可點擊；醫療場域用 **驗證先於建議 + 行為性同理 + 明確不越界聲明（instrumental support）** 同時滿足溫暖與合規，Lieberman fMRI：**命名情緒即降 amygdala 活躍**。

---

## 6. LLM 時代的混合架構：規則 + LLM 分工與成本/延遲控制

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 6-1 | **三層 Hybrid Classification：Rule → Embedding → LLM Fallback** | Layer1 regex/keyword <1ms 高頻固定意圖，Layer2 embedding nearest-neighbour + threshold ~5ms 近義改寫，Layer3 才 LLM；實測 60-90% cost reduction | [AppScale 2026](https://appscale.blog/en/blog/microservices-pattern-hybrid-classification-2026) / [Rasa LLM arch](https://rasa.com/blog/llm-chatbot-architecture) | **中高** |
| 6-2 | **Orchestrator + NeMo Guardrails（Colang，三階 Rails）** | `NemoGuard NIM → LLM → NemoGuard NIM` 二次檢查，分 Tier1 Rule(μs-10ms)/Tier2 BERT(20-100ms)/Tier3 LLM-as-judge(500ms-8s)，early exit + 併行 | [NeMo Architecture](https://docs.nvidia.com/nemo/microservices/26.3.1/guardrails/concepts/architecture.html) / [GitHub](https://github.com/NVIDIA-NeMo/Guardrails) / [PremAI 對比](https://www.premai.io/blog/production-llm-guardrails-nemo-guardrails-ai-llama-guard-compared) / [Reintech](https://reintech.io/blog/implement-guardrails-llm-applications-nemo-guardrails) | **高** |
| 6-3 | **小模型跑閒聊層（7B 蒸餾）** | WikiChat `GPT-4 → 7B LLaMA distillation` 達 91.1% factual, 3.2x 更快；Mistral 7B ~0.8s/1k vs GPT-4 ~5s；7-13B fine-tuned 在分類/領域 QA 常勝 70B，latency -2-4x | [WikiChat 2305.14292](https://arxiv.org/pdf/2305.14292) / [WebClues](https://www.webcluesinfotech.com/optimizing-llm-based-chatbots/) / [PremAI latency 5s→500ms](https://www.premai.io/blog/llm-latency-optimization-from-5s-to-500ms-2026/) | **研究證據 高** |
| 6-4 | **Semantic Router + Model-tier Routing（sub-100ms 決策）** | embedding similarity routing 到 handlers（support/billing/chitchat）~90% acc 無需 LLM；RouteLLM 在 MT Bench 85% 省費 at 95% quality（僅 14% 需強模型），企業 35-70% cost cut | [Lushbinary 70%](https://dev.to/robat_das_3c6e956212f6408/how-semantic-routing-cut-my-llm-costs-by-70-without-touching-model-quality-1hdp) / [Zylos](https://zylos.ai/research/2026-01-29-llm-routing-intelligent-model-selection) / [DigitalApplied](https://www.digitalapplied.com/blog/llm-model-routing-2026-cost-quality-optimization-engineering-guide) / [AgentGateway](https://agentgateway.dev/blog/2026-07-17-semantic-routing-llm-costs/) / [Aurelio](https://dev.co/ai/frameworks/aurelio-labs-semantic-router) | **中高** |
| 6-5 | **分層快取/串流：Semantic Cache + Prefix Cache + FP8** | Semantic Cache hit 61-85%, API -68.8%, latency 1.67s→0.052s (-96.9%), bill -15-30%（FAQ -65%）；Provider prefix cache 90%/50% discount；Streaming 感知快 5-10x；FP8 +33% throughput | [GPT Semantic Cache 2411.05276](https://arxiv.org/html/2411.05276v3) / [Scale guide 60-88%](https://aiworkflowlab.dev/article/llm-cost-optimization-production-semantic-caching-model-routing-token-management-scale) / [Maxim guide](https://www.getmaxim.ai/articles/reduce-llm-cost-and-latency-a-comprehensive-guide-for-2026/) / [5 that matter](https://dev.to/rikuq/llm-cost-reduction-techniques-ranked-by-roi-the-5-that-matter-the-9-that-dont-much-57d0) | **高** |
| 6-6 | **LLM 作為工具編排器而非自由行動者** | Rasa 定義 5 role：fallback / RAG rephraser / classifier-router / safety filter / tool orchestrator，`start narrow, gradually layer`；deterministic 管 math/IDs/regulatory/billing，generative 管 Q&A/drafting | [Rasa blog](https://rasa.com/blog/llm-chatbot-architecture) / [Nasscom 比較](https://community.nasscom.in/communities/application/llm-powered-agents-vs-rule-based-agents-technical-comparison) / [Cumberland](https://dancumberlandlabs.com/blog/chatbot-architecture/) | **中高** |

> **為什麼有效**：業界共識 **hybrid = determinism where cost of being wrong is highest, generation where flexibility is the point**。把 80% routine 攔在廉價層，LLM 只處理長尾；安全留在 deterministic+classifier（Tiered Rails）快、準、可稽核；小模型+語意路由+快取三件套同時把 **成本 -60-90% 與感知延遲 -5-10x** 兌現，與本專案 `fixed workflow A→B→C→D with bounded 3-choice` 完全同構。

---

## 7. 繁體中文 / 台灣 LINE bot 語境

| # | 做法 | 一句話說明 | 來源 | 可信度 |
|---|------|-----------|------|--------|
| 7-1 | **擬人人設 + 克制親切（疾管家「韓系歐巴」/ 萬小芳/蘭醫師）** | 疾管家由 HTC DeepQ 打造，`韓系雅痞+健康痣+撩妹金句「願意讓我保護你嗎？」` 0.5秒回 + 1秒1000則，3個月10萬→210萬追蹤；人設由 deterministic 模板+敬語詞庫控 | [天下 未來城市](https://futurecity.cw.com.tw/article/1373) / [DeepQ 紀念頁](https://www.deepq.com/jgj) / [衛福部 疾管家](https://www.mohw.gov.tw/cp-3558-37646-1.html) / [衛福部 疾管家2.0](https://www.mohw.gov.tw/cp-16-53351-1.html) | **業界慣例 高（繁中）** |
| 7-2 | **圖文選單 + 主動彙整圖卡 + 一問一答導診** | 最熱門資訊主動整理入口，避免複雜搜尋；串 AI 科別導診→看診紀錄→吃藥/回診提醒，150萬同時點擊仍扛住；國軍高雄 FHIR-LINE 串國際標準 | [同 7-1](https://futurecity.cw.com.tw/article/1373) / [802 FHIR Bot 2026-06](https://802.mnd.gov.tw/articles/fhir-line-bot-health-expo) / [健康存摺](https://www.nhi.gov.tw/ch/cp-3166-56af4-2457-1.html) | **高（繁中）** |
| 7-3 | **銀行/電商 LINE 語氣：敬語+效率+ emoji 節制** | 國泰/玉山：`簡潔+敬語+動詞明確（請點擊/請驗證）` 少 emoji；momo×微軟 LLM+RAG 客服 **正確率>90%，自助意願+5%**；Hiii 統計 LINE 開啟率 60-80% vs Email 15-25% | [國泰 Cube](https://www.cathay-cube.com.tw/cathaybk/personal/digital-service/intro/line) / [玉山 LINE](https://event.esunbank.com.tw/mkt/LINEBC/index.html) / [Microsoft momo](https://news.microsoft.com/source/asia/2025/10/20/momo-ai-customer-sevice/?lang=zh-hant) / [蝦皮賣家中心](https://seller.shopee.tw/edu/article/24002) / [Hiii](https://hiii.com.tw/development/line_bot) | **高（繁中）** |
| 7-4 | **三階段漸進（FAQ → 半自動草稿 → 限定全自動）** | 美勢 2026 指南：1)FAQ 知識庫依頻率排序 2)AI 起草真人審核標 `可用/小修/重寫` 累積信任分 3)僅穩定題型全自動，隨時降級；4 防線：禁承諾金錢/規格僅知識庫/個資遮蔽/永遠留「轉真人」 | [DigitalOrigin 指南](https://www.digitalorigin.tw/ai-customer-service-ecommerce-guide/) | **中高（繁中）** |
| 7-5 | **在地 LLM 化堆疊 + 小聊層** | LabGrimoire `Ollama+Qwen3.5+Hermes Gateway` 本地部署 + 6 踩坑；iThome `LINE+ChatGLM/Gemini` 同 `line_bot/app.py` X-Line-Signature；小聊限問候/感謝/道歉用小模型短 prompt | [LabGrimoire](https://labgrimoire.com/zh/blog/local-llm-line-customer-service-bot/) / [iThome Day23](https://ithelp.ithome.com.tw/articles/10348419) / [FlyPig](https://flypigai.icareu.tw/line-bot-service) | **中（繁中）** |
| 7-6 | **平台規則：LINE 彈性 > 蝦皮，LIFF/FHIR** | LINE 官方帳號（Messaging API+LIFF）彈性最大，Hiii 6 場景 40-60% 節省；momo RAG 解繁中語料不足 + PTU 撐峰；LIFF 是長者最無痛 FHIR 路徑 | [DigitalOrigin](https://www.digitalorigin.tw/ai-customer-service-ecommerce-guide/) / [Hiii](https://hiii.com.tw/development/line_bot) / [momo](https://news.microsoft.com/source/asia/2025/10/20/momo-ai-customer-sevice/?lang=zh-hant) / [ACT 指南 2026](https://actgsys.com/zh/blog/line-bot-enterprise-guide-2026) | **中高（繁中）** |

> **為什麼有效**：台灣 `88.2% 手機、86.5% 用 LINE，56歲以上 >90%` 且長者門檻高，成功關鍵 **`親切可信 + 敬語節制 + 圖卡易讀 + 永遠可轉真人`**。人設記憶點提升轉傳但不過度賣萌，圖卡>純文字對低健康識能友善，金融/醫療的「金額/療程承諾」一律禁 AI 決定，與 TFDA 禁區一致。

---

## 8. 為什麼人家的 bot 靈活 — 設計原則總結（7 條）

> 蒸餾自 §1-§7 業界共識，按對「人味」貢獻度排序。

| # | 原則 | 一句話 | 反觀我們哪裡違背 |
|---|------|--------|------------------|
| P1 | **分層（Hybrid Layering）：規則管安全與結構，LLM 管自由對話** | `determinism where cost of being wrong is highest, generation where flexibility is the point`（Rasa/NeMo）。安全留在 Tier1/2 deterministic+classifier，LLM 只做 `rephrase/routing/fallback` | 我們 `僅 G 走 LLM` 其餘全定字串，且 LLM 不作 chitchat/rephrase，無分層 |
| P2 | **隔離（Intent Insulation）：閒聊是獨立 retrieval 層** | Dialogflow/Rasa/Personality Chat 皆把 chitchat 先分流（high-level classifier + retrieval），不污染 task NLU | 我們 `G2 正則過窄 + welcome 短路` 把小聊當主任務或當未知，無獨立層 |
| P3 | **多樣（Variation by Design）：同一意圖 3-5 句同義 + 去重）** | Pool `random.choice` + contextual rephraser + seen-set/frequency_penalty，目標「同義不同形」 | 我們 15 罐頭皆單例，無 pool、無 rephraser、無 `{slot}` 插值 |
| P4 | **記憶（Contextual Continuity）：3-5 turns verbatim + rolling summary + history injection** | 業界 `k=5` sliding window + 滾動摘要，`{{history}}` 注入改寫與 Lorebook keyword-trigger；記得你 = 信任 | 我們 `ConversationContext` 有存但 **不注入 `CGenerator`**，`previous_attempts=[]` 每輪重建，去重是記憶體固定語 |
| P5 | **修復（Repair, not Apology）：Fallback = 引導** | `提示 + 引導 + 下一步` 三件套 + Two-stage `affirmation → rephrase → handoff` + context-sensitive；`fallback 不是道歉，而是引導` | 我們 `_format_formal_push_text` 與 `DEFAULT_FALLBACK` 把所有 failure 歸一到同一句 `沒整理出可靠回答` |
| P6 | **共情（Calibrated Empathy）：先驗證，再給工具，不過度擬人）** | Sentiment/frustration 偵測 → `Acknowledge(具體)→Validate→Offer path`，instrumental > experiential，醫療永遠標 `非醫療建議+轉介` | 我們對 `不人性化` 的抱怨走冷回 `這個我幫不上`，無 emotion routing |
| P7 | **在地語氣（Localized Persona）：Voice不變、Tone隨情境調節，敬語/圖卡/轉真人）** | 台灣要 `簡潔+敬語（您/請）+ 台灣用語 + emoji節制 + 圖文選單常駐 + LIFF`，疾管家靠人設+0.5秒回+1秒1000則 滾大；金融禁 AI 承諾 | 我們語氣單一威嚴/冷漠，無 `Professional/Friendly/Witty` persona，無台灣用語詞庫與 Quick Reply/Flex 卡 |

> **一句話總結**：人家的靈活不是「LLM 更強」，而是 **先靠規則與 embedding 把 80% 擋在便宜層、再給小聊獨立層與多樣池、再讓 LLM 只做有記憶的改寫與修復、最後用校準同理與在地語氣收尾** —— 全部在 B/D allowlist 之外，做的是「包裝」而非「放寬」。

---

## 9. 映射到我們 bot：安全不變式不動、成本可控下的優先序

> 評分維度：**Impact（對痛點 1-5 的覆蓋）× Effort（改動行數/風險）× Safety（是否碰 allowlist/B/D gate）**。`Safety=🔒 不動` 方可 P0/P1。

### P0 — 不碰 allowlist、只改模板與路由（本週可上，純規則，零 LLM 成本）

| 優先 | 改動 | 檔案與行號 | 解決痛點 | 成本 | 業界對應 |
|------|------|-----------|----------|------|----------|
| P0-1 | **Chitchat 白名單擴充 + `O_GENERIC` 與 `CHIT_CHAT_OUT_OF_SCOPE` 分離**：`_chit_chat` / `_CHIT_CHAT_RE` 補 `你是誰\|你是AI\|叫什麼\|不人性化\|敷衍\|罐頭` → 新 `IDENTITY` 分支，回覆 persona 自介 + `您好，我是糖尿病衛教小幫手（非真人，依 TFDA/國健署文件回答）`，情緒句另走 §5 修復 | `a_router/rules.py:80-90` / `workflow/graph.py:43-60,239-272,298-323` | 痛點 2、4 | 0 | §1 P1-P2 Intent insulation（Rasa/Personality Chat） |
| P0-2 | **Variation Pool（3 變體）+ 去重 seen-set**：`FALLBACK_TEMPLATES[O_GENERIC|CHIT_CHAT_OUT_OF_SCOPE|Q_NEED_MORE|B_INSUFFICIENT]` 各 3 句同義（輪替 `random.choice` + session 內 seen-set 避重），尾巴統一加 Quick Reply `[為什麼會有糖尿病][飲食怎麼吃][上傳藥袋][我能幫什麼]` | `workflow/fallbacks.py:11-26` / `line_bot/app.py:433-467 _format_formal_push_text` / `line_bot/app.py:840` 擴為 `messages=[TextMessage, QuickReply]` | 痛點 1、5 | 0 | §3 P3 Pool + §4 P3 三件套 |
| P0-3 | **去重 TTL 語境化：welcome/chitchat 旁路**：`TEXT_DEDUP_TTL_S` 對 `is_welcome_trigger / is_chit_chat_text` 的二連擊降為 `10s` 或走 variation pool 而非固定 `TEXT_DEDUP_REPLY`，僅對 `use_formal && 長句` 保留 120s | `line_bot/app.py:49-87` | 痛點 1 | 0 | §3 P3 De-duplication |
| P0-4 | **Intake 污染修復：`UNCERTAIN_PATTERNS` 在 ACTIVE 態改「確認而非寫入」**：命中 `不知道/不清楚` 時 **不寫入** `intake_snapshot`，而回 `SYMPTOM_UNKNOWN_QUESTION` + `pending_field` 重問（`attempts<2` 仍追問，≥2 才標 `待確認`），並把 `我不知道啊` 從 `_normalize_intake_answer` 的直接寫 pending 改為 `ASK_USER` 分支 | `intake/tool.py:82-83,242-247,681-762` / `line_orchestration/orchestrator.py:1016,1053-1069` / `workflow/graph.py:360-403` | 痛點 3 | 0 | §4 Repair：`ask_affirmation → ask_rephrase` 二階 |
| P0-5 | **`_format_formal_push_text` 原因分流**：`FORMAL_TIMEOUT` 保留 `HONEST_FALLBACK_PUSH_TEXT` 含「記到想問醫師的問題」；`B_INSUFFICIENT/B_UNSAFE` 改 C2 知識缺口話術（溫和+範例問句）；`CHIT_CHAT` 不進此分支（由 P0-1 接管） | `line_bot/app.py:433-467` / `d_output_gate/gate.py:51` | 痛點 4 | 0 | §4 P3 三件套 + §2 Mayo microcopy |

### P1 — 加記憶與改寫（小改動，小模型或短 prompt，成本可控，需 Design Review）

| 優先 | 改動 | 檔案與行號 | 解決痛點 | 成本 | 業界對應 |
|------|------|-----------|----------|------|----------|
| P1-1 | **對話歷史注入 +  rolling summary（3-5 turns）**：`ConversationContext.recent_turns`（`product_session/schemas.py:33-54`, `conversation/manager.py:77-245`）注入 `fallback 模板插值 {last_user_text}` 與 D 未命中時的 clarification 選項，`k=5` verbatim + 超窗 rolling summary ≤300字（cheap model）| `workflow/runner.py:119-141 stream` / `line_orchestration/orchestrator.py:194-1069` | 痛點 1、5 | 低（摘要每 4 輪一次，<300字 prompt，7B 即可） | §3 P4-P6 Sliding window |
| P1-2 | **Contextual Response Rephraser（僅對罐頭句，不動醫療事實）**：對 `O_GENERIC/CHIT_CHAT/Q_NEED_MORE` 的定字串做 LLM rephrase（`temperature 0.3`，prompt 含 `{{history}} + Persona`），`B/D 相關與承諾句` 鎖定不重寫 | 新增 `tfda_context_gate/conversation/rephraser.py`（仿 `Rasa rephrase: True`） | 痛點 1、4、5 | 低（短句 30-60 tokens，7B 0.8s/1k，僅 miss 時觸發，6-1 三層中僅 Layer3） | §3 P2 Rephraser / §6 P3 7B |
| P1-3 | **G2 白名單上游加 Embedding Semantic Router（小模型）**：Layer1 keyword 精確→ Layer2 embedding `bge-m3`（現有 Ollama 已有）`cosine>0.82` 判 chitchat/capability → Layer3 才 LLM；閾值可校準，early exit | `a_router/rules.py` 上游新增 `semantic_router.py`（复用 `rag/tfda_retriever.py` 的 `bge-m3` + cache `data/processed/.vector_cache/*.pkl`） | 痛點 2、3 邊界 | 低（embedding ~5ms，無 LLM） | §6 P1 Hybrid + P4 Router |
| P1-4 | **結構化 persona（#Personality & Tone）**：`# Role & Objective / # Personality & Tone(Warm, concise, 2-3句, 台灣敬語) / # Instructions / # Safety` + do/don't 範例 + `Voice vs Tone` 規則，從 fallback/push/rephraser 共用 | `tfda_context_gate/prompts/persona.md` 新增 | 痛點 4、5 | 0（prompt） | §2 P1-P3 OpenAI cookbook |

### P2 — 需要模型/架構投資（成本與延遲可控，但需 A/B 與額度管控）

| 優先 | 改動 | 說明 | 成本 | 業界對應 |
|------|------|------|------|----------|
| P2-1 | **NeMo-style Tiered Guardrails + Early Exit** | Tier1 regex 擋 PII/劑量，Tier2 BERT/toxicity，Tier3 LLM-as-judge，early exit + 併行；`B_context_gate/D_output_gate` 仍為最終 gate，第二層僅增體驗層 | 中（需評估） | §6 P2 |
| P2-2 | **Frustration 偵測 + 三段式同理路由** | `frustration 0.75 / sentiment -0.6 連2輪` 觸發 `Acknowledge→Validate→Offer`，醫療標 `非醫療建議`，復用小模型短句，不越界 | 中（classifier 20-100ms，可先 keyword 版） | §5 P1-P4 |
| P2-3 | **Semantic Cache + Prefix Cache（D PASS 後）** | `D PASS` 後衛教句做 semantic cache（閾值 0.95）+ provider prefix cache，對 FAQ workload 命中 61-85%, latency -96.9% | 低（infra） | §6 P5 |
| P2-4 | **圖文選單 / Flex 卡常駐 capability** | `line_bot/ui.py` Rich Menu 6宮格補「我能幫什麼」常駐 + Flex 能力卡（主文+來源小字），Quick Reply 最多 13 個實務 2-4 個 | 低 | §7 P1-P2 |

> **不做的紅線（本報告明確反對）**：擴大 `RAG allowlist` 以外自由生成醫療內容、讓 LLM 直寫 `intake_snapshot` 欄位、把 `A_EMERGENCY/U_*` 走溫和話術、語意路由繞過 B/D gate、快取未 `D PASS` 的輸出。

---

## 10. 最小改動最大人味 — 5 件事清單

> **約束**：安全不變式不動、零或極低 LLM 成本、本週可驗（對應 §9 P0）。每件皆可獨立上線、可 A/B、可回滾。

| # | 做什麼（Why 痛點） | 怎麼做（最小改動，含檔案行號） | 人味提升在那裡 | 預期指標變化 | 來源 |
|---|-------------------|-------------------------------|---------------|--------------|------|
| 1 | **把 `你是誰/不人性化` 從答非所問變自介**（痛點 2、4） | `a_router/rules.py:80-90` 與 `graph.py:43-60` 補正則 `你是誰\|你是AI\|叫什麼\|不人性化\|敷衍\|罐頭` → 新 `IDENTITY/EMPATHY` 分支，回 `您好，我是糖尿病衛教小幫手（非真人，依 TFDA/國健署衛教文件回答，能做：🥗衛教 / 📋看診整理 / 💊藥袋查詢）` + `話術末加免責「個人用藥請諮詢醫師/藥師」` | 使用者首次被「聽懂＋承認身分」，類似 Personality Chat 的 persona anchoring | `O_GENERIC 誤命中率 -80%`、`「你是誰」滿意 ≥4/5` | §1 1-3/1-4 + §2 2-5/2-6 |
| 2 | **讓同一句不再同一回覆（Variation Pool 3 變體 + Seen-set）**（痛點 1、5） | `workflow/fallbacks.py:11-26` 為 `O_GENERIC/CHIT_CHAT_OUT_OF_SCOPE/Q_NEED_MORE/B_INSUFFICIENT` 各寫 3 句同義（敬語+台灣用語 + 1 emoji 節制）+ `random.choice` 輪替 + session 內 seen-set 避重；尾巴統一 Quick Reply `[為什麼會有糖尿病][飲食怎麼吃][上傳藥袋][我能幫什麼]`（`line_bot/app.py:433-467,840`） | 兩次「你好」不再照鏡子，體感「有記性」 | `重複感知 -60%`（§3 動態 temperature 實測 -62%）、`Quick Reply 點擊率 +15-30%` | §3 3-1 + §4 4-3 三件套 |
| 3 | **把 `我不知道啊` 從污染欄位改為「確認再記」**（痛點 3） | `intake/tool.py:82-83,242-247,681-762` 與 `orchestrator.py:1016`：`ACTIVE` 態命中 `UNCERTAIN` 時 **不寫入** `待確認`，而回 `沒關係，這題先記為「待確認」，要補充再告訴我～接下來問「過敏史：您是否有藥物/食物過敏？」`（`SYMPTOM_UNKNOWN_QUESTION` 復用），僅 `attempts≥2` 才標 `待確認` 推進（`graph.py:360-403`） | 亂接話不再斷線，下一步永遠可預期（repair） | `intake 欄位污染 -90%`、`完成率不降` | §4 4-1 TwoStage + §4 4-4 重試計數器 |
| 4 | **去重 TTL 語境化 + welcome 差異化**（痛點 1） | `line_bot/app.py:49-87`：`is_welcome_trigger / is_chit_chat_text` 的二連擊 `TEXT_DEDUP_TTL_S` 從 120s 降 10s 或改走 variation pool，長句 formal 仍 120s；welcome 第二次補 `{記得上次問候} + 最近熱門選單` 插值 | 「你好」連發不再 `正在幫你查了，稍候` 冷凍，第二次變體歡迎 | `去重誤判 -70%`、`二次問候流失 -30%` | §3 3-3 De-duplication |
| 5 | **把冷回「這個我幫不上」換成 `道歉→安撫→給選項` 三段式（醫療不過界）**（痛點 4） | `workflow/fallbacks.py` 的 `CHIT_CHAT_OUT_OF_SCOPE` 現 `這個我幫不上，不過我可以：...` 改 `收到，剛剛的回覆比較罐頭讓您覺得不夠貼心，抱歉～我是依衛教文件回答的衛教小幫手，先幫您整理幾個試試：` + 能力卡 + `若有情緒困擾可試試：[衛福部安心專線1925] / [轉真人客服]`（路由採 keyword frustration 初版，閾值 `frustration>0.75 連2輪` 再升級小模型） | 抱怨被驗證而非被擋，instrumental support 取代 experiential | `抱怨後續留存 +30%`（ChatNexus 50% 更願意給好評的降維版） | §2 2-6 校準同理 + §5 5-2 三段式（ScienceDirect/PETS） |

> **上線順序**：1 → 2 → 5 可同 PR（皆 `fallbacks.py / app.py / rules.py`），3 需 `intake/tool.py + orchestrator` 單 PR，4 為參數 PR。全部完成後，再考慮 §9 P1 的 **history 注入 + 7B rephraser**（下一階段，成本 `30-60 tokens/次`）。

---

## 附錄 A. 來源一覽（按主題分組，36+ 主要來源）

> 去重後列主要 URL，次級補充見各節表格。

**Small talk & Persona（§1–§2）**
- https://docs.cloud.google.com/dialogflow/es/docs/agents-small-talk
- https://legacy-docs-oss.rasa.com/docs/rasa/chitchat-faqs/
- https://rasa.com/blog/response-retrieval-models
- https://github.com/microsoft/cognitive-research-technologies-docs/blob/master/project-personality-chat/overview.md
- https://github.com/ntulsi/BotBuilder-PersonalityChat
- https://learn.microsoft.com/en-us/azure/bot-service/bot-service-design-principles?view=azure-bot-service-4.0
- https://learn.microsoft.com/en-us/style-guide/chatbots-virtual-agents/writing-bots
- https://blog.character.ai/pipsqueak2-and-more/
- https://blog.character.ai/lorebook
- https://help.replika.com/hc/en-us/articles/37208679176077-How-does-Replika-s-memory-work
- https://developers.openai.com/cookbook/examples/realtime_prompting_guide
- https://developers.openai.com/cookbook/examples/gpt-5/prompt_personalities
- https://patientexperience.wbresearch.com/blog/mayo-clinic-google-assistant-voice-powered-web-chat-strategy-health-wellness-information-to-at-home-patients
- https://woebothealth.com/ai-core-principles

**記憶 / Fallback / 共情（§3–§5）**
- https://rasa.com/docs/reference/primitives/contextual-response-rephraser/
- https://ar5iv.labs.arxiv.org/html/2008.03391
- https://sj-langchain.readthedocs.io/en/latest/memory/langchain.memory.buffer_window.ConversationBufferWindowMemory.html
- https://arxiv.org/html/2605.20724v1 (CALMem)
- https://docs.cloud.google.com/dialogflow/cx/docs/concept/generative-fallback
- https://uxplanet.org/perfecting-the-chatbot-fallback-experience-f76d119c45d4
- https://www.sciencedirect.com/science/article/pii/S2949882124000276
- https://dev.to/ismail_zamareh_d099419122bc4f/taming-the-digital-temper-building-ai-agents-that-actually-de-escalate-frustration-27d8

**混合架構（§6）**
- https://appscale.blog/en/blog/microservices-pattern-hybrid-classification-2026
- https://rasa.com/blog/llm-chatbot-architecture
- https://docs.nvidia.com/nemo/microservices/26.3.1/guardrails/concepts/architecture.html
- https://github.com/NVIDIA-NeMo/Guardrails
- https://arxiv.org/pdf/2305.14292 (WikiChat)
- https://arxiv.org/html/2411.05276v3 (Semantic Cache)
- https://dev.to/robat_das_3c6e956212f6408/how-semantic-routing-cut-my-llm-costs-by-70-without-touching-model-quality-1hdp

**台灣 LINE 語境（§7）**
- https://futurecity.cw.com.tw/article/1373
- https://www.mohw.gov.tw/cp-3558-37646-1.html
- https://news.microsoft.com/source/asia/2025/10/20/momo-ai-customer-sevice/?lang=zh-hant
- https://www.digitalorigin.tw/ai-customer-service-ecommerce-guide/
- https://labgrimoire.com/zh/blog/local-llm-line-customer-service-bot/
- https://hiii.com.tw/development/line_bot

---

## 附錄 B. 方法與限制

| 項 | 說明 |
|----|------|
| **分類** | **研究證據** = 同儕審查論文/RCT/FMRI（如 SciDirect empathy、Nature BERT、CALMem、WikiChat）；**業界慣例** = 政府/大平台官方文件（Dialogflow、Rasa、Microsoft、NeMo、Woebot、疾管家）；**作者建議** = 部落格/觀察推論（Sprinklr、n8n、Hiii 等，雖互證但單篇不算對照） |
| **時效** | 優先 2023–2026，2026-07/08 新版 Character.AI PipSqueak2、NeMo 26.3.1、momo×Microsoft 2025-10 均已納入 |
| **覆蓋** | 背景 3 線 websearch 各 ≥2 輪（英文+中文）、每輪 `numResults 8-10`，共 >30 輪；外加 codebase 逐檔 grep（`fallback/canned/whitelist/allowlist/unknown/intake trigger`）。未做：台灣銀行/衛福部官方對照實驗數據多未公開，歸業界慣例 |
| **限制** | 转引的 40% 留存、30% retention 等無公開方法者僅作設計參考；Forrester/Gartner 指標多為 vendor 轉引；台灣衛福部 bot 未公布小聊層 A/B 數據；本報告 **僅研究不改碼**，P0/P1 建議待 Design Review 後入 `docs/plans/` |
| **與既有文件銜接** | `out_of_scope_response_research_20260827.md`（A1-A5/B1-B8/C1-C3/D1-D6 safety abort vs deflection 分離）與本報告互補；`latency_optimization_industry_scan_20260828.md` 的 streaming/caching 數據與 §6 互證 |

---

*報告完成時間 2026-08-28 22:15 Asia/Taipei · 4 線並行耗時 ~145s · 下一步：由 Design Review 決定 P0 5 件是否入 `docs/plans/p1_dialog_naturalization_plan_*.md` 增量。*
