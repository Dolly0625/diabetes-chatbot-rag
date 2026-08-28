# P1 對話自然化 對抗審查報告（2026-08-27）

> 審查者：對抗審查者（Adversarial Reviewer）  
> 基準：`a39c3a1`（改動前快照）`git diff a39c3a1 --stat` 6 檔 376+45-  
> 新測試：`tfda_context_gate/tests/test_p1_dialog_naturalization.py`（5 測試，untracked）  
> 驗收依據：`docs/plans/p1_dialog_naturalization_plan_20260827.md`（5 範圍 + 5 安全不變量 + 明確不做 + 開發者驗收條件）

---

## 總判定：BLOCKED（需修後放行）

- **安全不變量：PASS（6/6）** — B/D、紅旗、raw image、hash PII、FHIR、StrictModel 全未被破壞
- **鐵律「明確不做」：PASS（3/3）** — 無新增 LLM、無改狀態機語意、未動 `revalidate_via_a`
- **P1-3 截斷邏輯：PASS** — `[:2]` 活碼，3 條執行路徑皆在 `NEEDS_CLARIFICATION` return 前生效，REVIEW 承接
- **測試誠實度：PASS** — 既有 4 處未改弱、新增 5 個皆有檢測力（舊行為必 FAIL）
- **過頭檢查：FAIL（拼接氣泡超長 6 處）** — 單條文案 PASS，實際送出氣泡因拼接超 60 字
- **對抗重現：CONDITIONAL PASS（3 反例需修）** — E01/E03/E04 核心收斂與重述符合，但 E09 存垃圾值、E18 資訊丟失、E04 單輪超長

**建議：修 過頭檢查的 6 處拼接 + E09/E18 兩反例後，降為 PASS 可合併。安全鐵律無阻擋。**

---

## 1. 安全不變量核對（讀 diff 驗證，不是看測試過）

### 1.1 B/D gates 未被繞過、D 仍擋診斷/治療字樣 — **PASS**

**證據：**
- `git diff a39c3a1 --stat` 0 檔觸 `d_output_gate/`（`d_output_gate/gate.py` `policy.py` 零改動）
- `tfda_context_gate/workflow/graph.py:900-930` 仍 `add_node("B",b_node)` + `add_node("C",c_node)` + `add_node("D",d_node)` + `add_edge("C","D")` + `add_conditional_edges("B",b_route,{"C":"C","AGENT_PLANNER":...})`
- `tfda_context_gate/workflow/runner.py:81-154` 仍 `build_workflow_graph(context_gate=..., verifier=...)` → `graph.invoke(state)` 必經 D；`line_bot/app.py:789` 註「單輪相容模式仍完整經過 B/D gates」
- D 阻擋能力：`d_output_gate/policy.py:48-55` `HARD_POLICY_RISKS` 與 `:91-103` `intake_prohibited_patterns`（含 `確診為.{0,12}糖尿病`、`建議...劑量`）未改；`gate.py:337-353` `run_previsit_output_gate` 仍先檢 `hard_risks intersection` → `PREVISIT_SUMMARY_CONTAINS_DIAGNOSIS_OR_TREATMENT` FALLBACK
- 重現：`PYTHONPATH=. pytest tfda_context_gate/tests/test_workflow_integration.py -q` → 15 passed

### 1.2 紅旗 `POSSIBLE_EMERGENCY` → `U_URGENT_HUMAN` 路徑未經新模板 — **PASS**

**證據：**
- 新模板僅在 `workflow/graph.py:339-359` `intake_stage1_node::_with_confirm` 與 `:373-405` `stage2::_with_confirm` 內部定義，僅修飾 `question/final_response = f"{confirm}\n{question}"`，不在紅旗路徑
- 紅旗短路在 `graph.py:199-217` `a_node` 頂部：`if _is_red_flag(raw_input): policy_gate → A_URGENT_HUMAN/E_EMERGENCY → return FALLBACK` 直接 END，不進 INTAKE 節點，故 `_with_confirm` 不執行
- 第二道防線 `line_orchestration/orchestrator.py:193-223` `RiskSignalPolicy().classify(text)` → `if cumulative_risk.level=="RED_FLAG": fallback_response("A_EMERGENCY")` 且 `orchestrator.py:684-698` RED_FLAG 單調不可降級
- `intake/tool.py:631-660` `revalidate_via_a` 仍 `route_request(req) → is_red_flag and is_fixed_referral → fixed_via_a` 不經 planner（見 §2）
- 重現：`PreVisitIntakeTool().revalidate_via_a("我胸痛呼吸困難要昏倒了", request_id="x")` → `{'is_red_flag':True,'fixed_via_a':True,'router_status':'E_EMERGENCY'}`（實測見重現指令）

### 1.3 不存 raw image — **PASS**

**證據：**
- `WorkflowState` TypedDict `graph.py:82-120` 無 `image_bytes` 欄位；`grep WorkflowState|image_bytes` 僅 `runner.py/app/adapter` 出現
- `runner.py:102-109` `_process_ocr_images(intake_data, image_bytes=...) → intake_data=_ocr_base` 合併後丟棄 raw bytes；`runner.py:142` `state:WorkflowState = {request_context, original_query, intake, intake_data, task_type}` 不含 image
- `runner.py:101` 註「永不存 raw image」、 `line_bot/app.py:260-284` 註「Never stores raw image in WorkflowState」
- `graph.py` 全文 `image` 零命中（`grep -n image graph.py` 無結果）

### 1.4 hash PII — **PASS**

**證據：**
- `orchestrator.py:717-718` `def _hash(value): hmac.new(_hash_key, value, sha256).hexdigest()`；`702-727` `session_id = f"line-{_hash(line_user_id)[:32]}"`
- `orchestrator.py:79-85,121-127` 僅存 `principal_id_hash`（64 hex），`repository.claim_webhook_event(event_id, principal_hash)`；`product_session/schemas.py:37,95` `principal_id_hash: str = Field(min_length=64)`
- diff 未改任何 hash 邏輯，P1 新增僅 `orchestrator.py:346-360` `format_stage_progress` 文案

### 1.5 FHIR unknown extension 不變 — **PASS**

**證據：**
- `schemas.py:159-160` `FHIR_MEDICATION_UNKNOWN_STATUS="unknown"` `FHIR_MEDICATION_UNKNOWN_SUFFIX="待確認"` 未動
- `tool.py:354-387` 保留：`370-371` `ans["extension"]=[{"url":"http://hl7.org/fhir/StructureDefinition/questionnaire-response-status","valueCode":"unknown"}]` 與 `378-380` `item["extension"]=[{"url":"http://hl7.org/fhir/StructureDefinition/questionnaire-response-unknown"}]`；diff 僅擴充 `306-327` `value/question` 未刪 extension
- 重現：`PreVisitIntake(known_medications=["白色藥丸-待確認"]) → to_fhir_questionnaire_response` 實測含雙層 unknown extension（見重現輸出 JSON）

### 1.6 PreVisitIntake 8 欄位 StrictModel extra=forbid 未動 — **PASS**

**證據：**
- `schemas.py:23-24` `class StrictModel: ConfigDict(extra="forbid")` 未動；`39-83` `PreVisitIntake(StrictModel)` 8 核心欄 + 2 provenance 維持
- diff `schemas.py:163-180` 僅改 `INTAKE_FIELD_QUESTIONS` **值**（刪 `第 n/8 題｜`），未改鍵/欄位/ `INTAKE_STAGES:203-207`
- `tool.py:157-161,201-203` 仍雙重 `PreVisitIntake.model_validate(...model_dump(mode="json"))` 重驗

---

## 2. 鐵律「明確不做」核對

### 2.1 沒有新增 LLM 呼叫點 — **PASS**

**證據：**
- `git diff HEAD -U0 | grep -E "^\+" | grep -i -E "openai|mimo|llm|chat|completion|\.generate|\.invoke"` → 零輸出
- `git diff HEAD -U0 | grep -E "^\+" | grep -E "import|from"` 僅 8 行，均為 `from tfda_context_gate.intake.tool import format_stage_progress / build_implicit_confirm*`
- 新增實作全純模板：`tool.py:31-83` `IMPLICIT_CONFIRM_TEMPLATE` + `build_implicit_confirm*`（切片/拼接）、`:93-130` 正則 `UNCERTAIN_RE`、`:659-720` `format_stage_progress` 僅 `[:60]` 截斷；`schemas.py:214-217` 僅常數；`line_bot/app.py:416-434` 僅調 `format_stage_progress`；`graph.py:340-360` `_with_confirm` 僅調 `build_implicit_confirm_for_fields` 且 `except Exception: pass`
- 全倉既有 LLM 點 `c_generator/langchain_adapter.py:157` `chain.invoke`、`a_router/router.py:117` 等 零改動

### 2.2 沒有動狀態機節點語意 — **PASS**

**證據：**
- `graph.py`：`diff | grep status|termination_reason` 僅 `final_response` 被 `_with_confirm` 包裝，`status="NEEDS_CLARIFICATION"` / `termination_reason="NEEDS_CLARIFICATION"/"NEXT_INTAKE_STAGE"` 不變；`build_agent_question:120-127` 僅刪前綴，key 不變；`stage1:340-362` 與 `stage2:387-409` 的 `span.set(status=...)`、`missing_field` 判斷、`intake_stage` 推進零改
- `orchestrator.py:151-170` `handle_image`、`301-325` `handle_text`、`340-390` `PAUSED/RESUME` 的 `status` 仍取 `workflow.status`，`stage_completed` 判斷不變，僅 `reply` 前綴 `format_stage_progress`/`checkpoint`
- 14 測試語意：`test_conversation_orchestrator.py:145,168,189,227` 的 `status/intake_snapshot/pending_field` 斷言 零改，僅文案斷言改（見 §5）

### 2.3 沒有動 revalidate_via_a — **PASS**

**證據：**
- `grep -rn revalidate_via_a` 唯一命中 `tool.py:631`；`git diff HEAD -U10 -- tool.py | grep -A20 -B20 revalidate_via_a` → 無輸出（該方法區塊零改動）
- `tool.py:631-655` 方法簽名/ docstring / `route_request` 呼叫與回傳 7 鍵（`router_status/rag_allowed/is_red_flag/is_fixed_referral/fixed_via_a/a_result`）與 `git show a39c3a1:tool.py:625-655` 逐字一致；新增的 `format_stage_progress` 在 656 行後
- 呼叫端 `grep fixed_via_a` 在 diff 新增行零命中

---

## 3. 對抗重現（真實口語構造輸入跑 workflow）

> 重現指令均以 `PYTHONPATH=. python3` 執行，`ConversationOrchestrator` + `SQLiteProductSessionRepository` 隔離庫；tool 層 `is_uncertain_answer/handle_symptom_clarification/build_implicit_confirm` 亦直測。輸出節選於下，反例已標。

### E01「不知道欸，藥名忘了」— **PASS（收斂）但附帶 E18 同類缺陷**

**輸入：** `為自己整理` → `不知道欸，藥名忘了`（known_medications 欄）  
**指令：**
```bash
PYTHONPATH=. python3 -c "from tfda_context_gate.intake.tool import is_uncertain_answer, PreVisitIntakeTool; print(is_uncertain_answer('不知道欸，藥名忘了')); print(PreVisitIntakeTool().handle_medication_clarification('不知道欸，藥名忘了', attempt=1))"
PYTHONPATH=. python3 <<'PY'  # orchestrator
from tfda_context_gate.line_orchestration import ConversationOrchestrator; ...
orch.handle_text(event_id="e01-2", text="不知道欸，藥名忘了")
PY
```
**輸出：**
- `is_uncertain_answer → True`（`UNCERTAIN_RE` 命中 `不知道|忘了`）
- `handle_medication_clarification → {'status':'unknown','medications':['待確認'],'question':'沒關係，先記為『待確認』，看診時再跟醫師確認。','reason':'medication_unknown_uncertain'}`
- orchestrator：`reply='沒關係，我先把這一項標成「待看診確認」，不會替你猜。\n\n有沒有藥物或食物過敏？...'`；`intake_snapshot.known_medications==['不清楚（待看診確認）']`；`status=NEEDS_CLARIFICATION`；待確認收斂（1 次即收斂，無陷入追問）
- **判定**：符合 P1-4「單欄最多追問 2 次，接受為待確認」；確認句不含重述（刻意分支），但後續進度待確認收斂

### E03「我…我忘了什麼時候開始的」— **PASS**

**輸入：** stage1 四欄填完後，`我…我忘了什麼時候開始的`（symptom_onset）
**輸出：**
- `is_uncertain → True`；`handle_symptom_clarification("symptom_onset", ..., attempt=1) → {'status':'unknown','value':'待確認','question':'沒關係，先記為『待確認』，看診時再跟醫師確認。'}`
- orchestrator：`reply='沒關係，先記為『待確認』，看診時再跟醫師確認。\n\n目前最主要的症狀或困擾是什麼？'` len=40；`symptom_onset=='待確認'`；`包含待確認、無幻覺、收斂`
- **判定**：PASS。重現步驟同 E01，差在 symptom 欄走 `orchestrator.py:576-580` `setattr(intake, field, "待確認")` 分支，不經 `build_implicit_confirm`

### E04「口渴、頻尿、腳麻頭暈」多症狀一次出現 — **CONDITIONAL PASS（重述與收斂 PASS，長度 FAIL）**

**輸入：** stage2 `口渴、頻尿、腳麻頭暈`
**輸出：**
- `tool.extract_fields_from_utterance("口渴、頻尿、腳麻頭暈", stage="stage2") → {'symptom_description':'口渴、頻尿、腳麻頭暈'}`（單欄抽取，多症狀以原句存 description，未拆多欄 — 符合「僅抽顯式提及，不臆造」）
- `build_implicit_confirm_for_fields(extracted, raw_text=...) → '你提到「口渴、頻尿、腳麻頭暈」，我記為「口渴、頻尿、腳麻頭暈」，對嗎？'`（含重述、對嗎？、單輪確認 1 項 `；` 0 個 ≤2）
- orchestrator 第二輪：`reply='你提到「口渴、頻尿、腳麻頭暈」，我記為「口渴、頻尿、腳麻頭暈」，對嗎？\n\n程度大約是輕度、中度、重度，或 1–10 分中的幾分？'` **len=64 >60**（見 §4 超長清單）
- **判定**：重述/≤2 項/待確認收斂/無幻覺 **PASS**，但 **單輪 64 字超 60** 屬 §4 FAIL 同源

### E09 貼圖/亂碼輸入 — **FAIL（存垃圾值）**

**輸入：** `為自己整理` → `😊👍` → `###///亂碼***`
**輸出：**
- `is_uncertain("😊👍") → False`；`is_uncertain("###///亂碼***") → False`
- orchestrator：`😊👍 → reply='你提到「😊👍」，我記為「😊👍」，對嗎？\n\n有沒有藥物或食物過敏？...'` len=45；`intake_snapshot.known_medications==['😊👍']`（垃圾值入庫）
- `###///亂碼*** → reply='你提到「###///亂碼***」，我記為「###///亂碼***」，對嗎？\n\n除了糖尿病，還有高血壓...'` len=60；`allergies==['###///亂碼***']`
- **判定**：無幻覺藥名（`metformin` 未出現）**PASS**，但 **存垃圾值入 PreVisitIntake** 屬反例。期望：亂碼/emoji 應視為無效輸入，不寫入，回退為澄清追問（或至少不確認為有效值）。目前 `_normalize_intake_answer:601-627` 對無法抽取的多欄回退 `direct: Any = [text.strip()[:100]]` 直接寫入，無內容有效性檢查

### E18 中英夾雜錯字 — **FAIL（資訊丟失）**

**輸入：** `我吃 metformin 但是 dose 忘了，有點 dizzy 頭暈`
**輸出：**
- `tool.extract_fields_from_utterance(text) → {'known_medications':['metformin'], 'symptom_description':'有點 dizzy 頭暈', 'questions_for_doctor':['我吃 metformin 但是 dose ...']}`（tool 層正確抽 3 欄）
- `build_implicit_confirm_for_fields(..., raw_text=text) → '你提到「我吃 metformin 但是 dose 忘了，有點 diz」，我記為「metformin；有點 dizzy 頭暈」，對嗎？'`（若走抽取路徑，應為此）
- orchestrator 實際：`reply='沒關係，我先把這一項標成「待看診確認」，不會替你猜。\n\n有沒有藥物或食物過敏？...'`；`known_medications==['不清楚（待看診確認）']`；`symptom_description is None` — **metformin 與 dizzy 頭暈皆丟失**
- **根因：** `orchestrator.py:561-600` `_normalize_intake_answer` 先判 `uncertain = bool(re.search(UNCERTAIN_PATTERNS...)) or "不太知道" in normalized` → 若含 `忘了/不知道` 即整句判 uncertain，**未先抽取有效欄位**，直接 `setattr(intake, field, "不清楚（待看診確認）")` 短路。導致「含有效藥名 + 含不確定」的混合句資訊全丟失
- **判定**：待確認收斂 PASS，但 **幻覺為反向：資訊丟失而非幻覺藥名**，屬反例

### 補充：多欄截斷驗證（P1-3 交叉）

**輸入：** `吃 metformin，無過敏，有高血壓，家族無糖尿病`（4 欄一次）
**輸出：** `reply='你提到「吃 metformin，無過敏，有高血壓，家族無糖尿病」，我記為「metformin；無」，對嗎？\n\n用藥與病史已記下：用藥 metformin；過敏 無；慢性病 高血壓；家族史 無。\n已完成：用藥與過敏 ✅ 還差：症狀、想問醫師 2 段\n\n這次想看診的狀況大約從什麼時候開始？'`；`known_meds/met.../allergies/chronic/family` **全量寫入** 4 欄；`normalized_part='metformin；無'` `count("；")==1 ≤1`（僅確認 2 項）；`pending_field==symptom_onset`（其餘 2 欄已寫但未確認，留待 REVIEW 摘要 `graph.py:444`）
- **判定**：PASS。符合「多欄抽取後最多確認 2 項，其餘留待 REVIEW 一次性確認」（全量寫入但確認截斷）

---

## 4. 過頭檢查（像人但別太像）

> 掃描範圍：`git diff a39c3a1` 376 行新增 + `schemas.py:163-221` `tool.py:38-712` `orchestrator.py:250-663` `graph.py:121-405` `line_bot/app.py:418-539`

### 4.1 無過度同理句 — **PASS**

- `Grep "很難過|心疼|理解你的感受|辛苦了|抱抱|聽到你這樣|感同身受|不容易|加油|心裡一定"` 全庫 0 命中於新文案（僅命中 `docs/plans` 例舉禁句與舊報告表格）
- 抽樣新文案皆中性事務句：
  - `schemas.py:164` `目前有固定吃藥或打胰島素嗎？...`；`tool.py:86` `沒關係，先記為『待確認』，看診時再跟醫師確認。`（「沒關係」屬中性承接，非過度同理）；`orchestrator.py:589` `沒關係，我先把這一項標成「待看診確認」，不會替你猜。`
- `git diff` 全文 `DIFF empathy check: PASS`

### 4.2 無對岸用語 — **PASS**

- `Grep "信息|数据|软件|视频|质量|水平"` 新文案 0 命中；反向 `資訊/資料` 22 處皆台灣寫法（`schemas.py:3` `看診前資訊`、`tool.py:244` `診前資訊` 等）
- 對照：信息→資訊、數據→資料、軟件→軟體、視頻→影片、質量→品質 均正確或未出現

### 4.3 單輪 ≤60 字（確認句豁免）— **FAIL（6 處拼接氣泡超長）**

**單條字串本體** 多數 PASS（`format_stage_progress` 有 `[:60]`，8 題問句 15-31 字，`MEDICATION_CLARIFICATION` 20-29 字）：

| 檔案:行號 | 文案 | len | 判定 |
|-----------|------|-----|------|
| `schemas.py:164-171` | 8 題問句 | 15-31 | PASS |
| `tool.py:86` | `沒關係，先記為『待確認』...` | 23 | PASS |
| `tool.py:693-711` | `還差：…3段…` / `皆已完成✅` | ≤28 | PASS（有`[:60]`） |
| `orchestrator.py:589` | `沒關係，我先把這一項標成「待看診確認」...` | 26 | PASS |

**拼接後實際氣泡 FAIL（6 條，非豁免）：**

1. `schemas.py:211` `STAGE_QUESTIONS.stage1` — `為了幫您整理看診資料，請問目前使用的藥品、過敏史、慢性病史及家族史？（可一次說明多項，如「吃 metformin，無過敏，有高血壓，家族無糖尿病」）` **len=74 >60** — FAIL
2. `orchestrator.py:355+357` `PAUSED` 拼接 — `好的，已先暫停；目前資料會保留，不用重新填。你可以先問其他問題，想回來時點「繼續整理」即可。\n還差：用藥與過敏、症狀、想問醫師 3段，先從用藥開始吧` **len=75** — FAIL（單段各自 ≤46/28，但 `f"{pause}\n{progress}"` 未再截）
3. `orchestrator.py:387` `RESUME` 拼接 — `還差：…3段…\n\n目前有固定吃藥…不確定也沒關係。` **len=61** — FAIL（剛好超 1）
4. `orchestrator.py:652-663` `checkpoint stage1` — `用藥與病史已記下：用藥 metformin；過敏 無；慢性病 高血壓；家族史 無。\n已完成：用藥與過敏 ✅ 還差：症狀、想問醫師 2段` **len=68** — FAIL（`base≈42 + "\n" + progress26`）
5. `orchestrator.py:659-663` `checkpoint stage2` — 同理 **len=61** — FAIL
6. `orchestrator.py:250` `SIDE_ANSWER hint` — `看診資料我先幫你保留，不用重填。想繼續時可以回答這題，或點「繼續整理」：\n目前有固定吃藥…` **len=68** — FAIL

**邊界（豁免但關注）：** `IMPLICIT_CONFIRM_TEMPLATE` 豁免，但 `build_implicit_confirm(raw[:30], normalized)` 最壞 `30+40+12` 可達 82 字（實測 E04  confirm 35 + 下題 27 = 62 已超），若使用者貼長串藥名仍可能>60。建議 `normalized` 再 `[:25]` 或豁免註明不計。

---

## 5. 測試誠實度

### 5.1 被修改的既有測試 — **PASS（誠實，未改弱）**

> `git diff a39c3a1 -- tfda_context_gate/tests/test_conversation_orchestrator.py` 4 處 `1行→1行`，無 `D` 刪除；`git ls-tree` 舊 17 檔 vs 新 18 檔多出 `test_p1_dialog_naturalization.py` 唯一新增。

改動模板：`assert "第 2/8 題" in reply` → `assert "過敏" in reply and "第" not in reply`（3 處：`144,167,188`）；`assert "第 5/8 題" in reply` → `assert "什麼時候開始" in reply and "第" not in reply`（1 處：`226`）

| 位置 | 舊斷言 | 新斷言 | 誠實度 | 理由 |
|------|--------|--------|--------|------|
| `144 test_unknown_answer_is_valid_and_advances_to_next_single_question` | `第 2/8 題` | `過敏 and 第 not in` | **PASS** | 前半精確指向 `INTAKE_FIELD_QUESTIONS["allergies"]`（含 `過敏`），若停留在 `known_medications`/`chronic` 則缺 `過敏` 而 FAIL，推進檢測力等價；後半增加 P1-5 禁數字進度，門檻更嚴 |
| `167 test_general_education_digression_answers_then_returns_to_saved_intake` | `第 2/8 題` | `過敏 and 第 not in` | **PASS** | 同 144，且該用例還校驗 `SIDE_ANSWER`+`pending_field==allergies`，新斷言語意化更強 |
| `188 test_intake_can_pause_and_resume_without_losing_progress` | `第 2/8 題` | `過敏 and 第 not in` | **PASS** | 同 144，resume 後欄位校驗保留 |
| `226 test_stage_checkpoint_summarizes_before_next_section` | `第 5/8 題` | `什麼時候開始 and 第 not in` | **PASS** | `什麼時候開始` 唯一對應 `symptom_onset`，若進到 `symptom_description/ severity` 則 FAIL，特異性等於舊；保留 `用藥與病史已記下` checkpoint |

**結論**：4 處皆因 `schemas.py:163-180` 已全量移除 `第 n/8 題｜` 前綴，舊字串在新碼必失敗，屬規格變更必改；新字串改欄位語意 + 負向禁數字，**未改弱**。

### 5.2 新測試 5 個是否真的會失敗於舊行為（有檢測力）— **PASS（5/5 有檢測力）**

| 測試 | 測什麼 | 舊行為下是否會 FAIL | 證據 |
|------|--------|---------------------|------|
| `test_p1_2_implicit_confirm_format_contains_raw_and_normalized` | `build_implicit_confirm(raw, normalized) == "你提到「{raw}」我記為「{normalized}」，對嗎？"` 且不含 `收到/了解` 且 `len ≤60+len(normalized)` | **會 FAIL** | 舊版無此函數（`ImportError`）且舊 reply 為 `好，已記下。` 不含 `你提到/對嗎？` |
| `test_p1_2_build_implicit_confirm_for_fields_limits_to_two` | 4 欄輸入 `normalized_part.count("；") <=1` 僅取前 2 | **會 FAIL** | 舊無此函數；若強行比，舊會 `；` 2 個（4 值以 `；` 連）不含 `對嗎？` |
| `test_p1_3_single_round_only_confirms_one_to_two_items` | 一次說 4 欄 `對嗎？` 且 `； ≤1` 且 `pending_field is not None` | **會 FAIL** | 舊 `reply="好，已記下。"` 不含 `對嗎？/我記為` 即 FAIL |
| `test_p1_4_unknown_graceful_convergence_for_symptom` | `is_uncertain` 真值表 + `handle_symptom_clarification("不知道")→unknown/待確認/沒關係…` + E2E `symptom_onset=="待確認"` | **會 FAIL** | 舊 `is_uncertain` 少 `不記得|忘了|不太清楚`，無 `handle_symptom_clarification`；E2E 舊存 `不清楚（待看診確認）` 且 reply 非該句 |
| `test_p1_5_stage_progress_replaces_numeric_progress` | `format_stage_progress(empty/partial/full)` 均 `第 not in`/`還差/已完成/皆已完成✅`/`len≤60` 且 `INTAKE_FIELD_QUESTIONS` 無 `第 1/8 題` | **會 FAIL** | 舊 `INTAKE_FIELD_QUESTIONS` 8 條全含 `第 n/8 題｜`，`format_stage_progress` 不存在，兩組斷言皆 FAIL |

**刪除測試檢查：** `git diff --name-status` 無 `D`，4 處皆 `M` 1 行替 1 行，無未說明刪除。

### 5.3 總體 — **PASS**

既有 4 處誠實、新增 5 個有檢測力、無未說明刪除。Builder 改動符合 P1 自然化規格且門檻未降低。

---

## 6. P1-3 重點打擊：截斷邏輯是否真的在執行路徑

**總判定：PASS — 非死碼，三條正式路徑皆生效**

### 6.1 定義點

- `tfda_context_gate/intake/tool.py:48` `def build_implicit_confirm_for_fields(extracted, raw_text)`；`58` `for _field, value in list(filtered.items())[:2]:` — 全倉唯一業務 `[:2]`（`grep "[:2]"` 僅 3 命中，另 2 為 `line_bot/app.py:496 base[:2]` 與 `deterministic_generators.py:173 claims[:2]` 非業務）

### 6.2 呼叫鏈（定義→呼叫→執行路徑）

| 呼叫點 | 檔案:行號 | 在哪個節點/方法 | 是否在 NEEDS_CLARIFICATION return 前 |
|--------|-----------|----------------|--------------------------------------|
| 定義 | `tool.py:48` | `build_implicit_confirm_for_fields` | — |
| 呼叫 1 | `workflow/graph.py:347` `c = build_implicit_confirm_for_fields(extracted, raw_text=original)` | `intake_stage1_node::_with_confirm` (`342-352`) | 是：`354 final_q = _with_confirm(next_question)` 緊接 `355 return {question:final_q, status:NEEDS_CLARIFICATION}`；`357` NEXT_INTAKE_STAGE 分支亦同 |
| 呼叫 2 | `workflow/graph.py:394` 同簽名 `raw_text=text_to_extract` | `intake_stage2_node::_with_confirm` (`389-399`) | 是：`401 final_q = _with_confirm(...)` → `402 return NEEDS_CLARIFICATION`；`404` NEXT_INTAKE_STAGE 同 |
| 呼叫 3 | `line_orchestration/orchestrator.py:610` `confirm = build_implicit_confirm_for_fields(extracted, raw_text=text)` | `ConversationOrchestrator._normalize_intake_answer` (`558` 起) | 是：`601-609 for k,v in extracted: setattr(intake,k,v)` 全量寫入後 `610` 截斷確認 `618 return session, confirm` |

### 6.3 extract_fields_from_utterance → 是否走截斷

- `tool.py:449` 定義 `extract_fields_from_utterance(utterance, stage)` → `graph.py:65` `_extract_multi_fields` 封裝
- **graph.py stage1**: `327 extracted = _extract_multi_fields(original,"stage1")` → `334-338 for k,v in extracted: setattr(obj,k,v)` 全量寫入 → `342-352 _with_confirm build_implicit_confirm_for_fields[:2]` → 兩次 return 前皆呼叫
- **graph.py stage2**: 同構 `370` → `381-385` 全量寫入 → `389-399` `_with_confirm` → `401-405` 兩次 return 前皆呼叫
- **orchestrator.py**: `601 extracted = PreVisitIntakeTool().extract_fields_from_utterance(text, stage=...)` → `606-609 setattr` 全量寫入 → `610` 截斷確認（寫與確認分離）

**語意**：「全量寫入但確認只發 2 項，其餘進 REVIEW」符合 P1-3 規格（`多欄抽取後最多確認 2 項，其餘留待 REVIEW 摘要一次性確認`），非「只存 2 項」

### 6.4 其餘欄位延後到 REVIEW 證據

- `workflow/graph.py:411-438` `intake_stage3_node` 完成後 `return {intake_stage:"review"}` → `444 review_confirm_node` → `458 summary = generate_previsit_summary(obj)` → `467 return {question:review_text, status:NEEDS_CONFIRMATION}` — 截斷未確認的欄位已寫入 `PreVisitIntake`，最終以 `summary_text + missing/provided_fields` 一次性呈現

### 6.5 死碼排除

- `grep build_implicit_confirm_for_fields` 命中 4 處（定義 + 3 生產 + 1 測試 `test_p1_dialog_naturalization.py:36`），且在節點內 `from tfda_context_gate.intake.tool import build_implicit_confirm_for_fields` 動態載入後立即呼叫；測試 `42-43` `assert normalized_part.count("；") <=1` 驗活碼
- 實測多欄 `吃 metformin，無過敏，有高血壓，家族無糖尿病` → `normalized_part='metformin；無'` `count==1`，其餘 2 欄已寫但未在單輪確認（見 §3 補充）

---

## 反例清單（按嚴重度）

| # | 來源 | 反例 | 嚴重度 | 是否阻擋 |
|---|------|------|--------|----------|
| R1 | E18 | `我吃 metformin 但是 dose 忘了，有點 dizzy 頭暈` → `known_medications==['不清楚（待看診確認）']` 丟失 `metformin` 與 `頭暈` | **高** | 需修 |
| R2 | E09 | `😊👍` / `###///亂碼***` → 存入 `known_medications/allergies` 為垃圾值 `['😊👍']` | **中** | 需修 |
| R3 | §4 拼接 | 6 條氣泡因 `confirm+question`/`pause+progress`/`checkpoint+progress` 拼接後 61-75 字 >60（含 E04 64 字） | **中** | 需修 |
| R4 | E04 | `口渴、頻尿、腳麻頭暈` 單欄描述雖重述 PASS，但整輪 64 字 >60 已計入 R3 | 低 | 同 R3 |

**非反例**：E01/E03 的「無重述僅待確認句」、E04 的「多症狀存 description 原句」、P1-3 的「全量寫入僅確認截斷」均符合規格。

---

## 給開發者的修正清單（按檔）

### 必須修（BLOCKED 解鎖）

**1. `tfda_context_gate/line_orchestration/orchestrator.py:561-610` — E18 資訊丟失**

- 現狀：`uncertain = re.search(UNCERTAIN_PATTERNS... )` 先判整句，若命中即短路 `setattr(field, "不清楚（待看診確認）")`，丟棄 `metformin` 等有效抽取
- 建議：先 `extracted = PreVisitIntakeTool().extract_fields_from_utterance(text, stage=...)`，若 `extracted` 非空且含高置信藥名（如 `metformin/insulin`），則 **保留抽取結果**，僅對未抽到的欄位或不確定片段記 `待確認`；或判 `uncertain` 時若 `extracted` 有值則走 `build_implicit_confirm_for_fields` 分支而非直接 `不清楚（待看診確認）`。參考 `tool.py:306-327` `handle_medication_clarification` 已對 `is_uncertain` 返回 `unknown` 但保留上下文的模式
- 驗收：`handle_text(..., "我吃 metformin 但是 dose 忘了...")` 後 `known_medications` 含 `metformin` 且無 `待看診確認` 覆蓋

**2. `tfda_context_gate/line_orchestration/orchestrator.py:601-627` + `tool.py:449-504` — E09 垃圾值入庫**

- 現狀：`extracted` 為空時回退 `direct = [text.strip()[:100]]` 無有效性檢查，emoji/亂碼直接入庫並被確認
- 建議：`direct` 分支前加有效性閾值：如 `len(text.strip()) <2` 或 `re.fullmatch(r"[^\w\u4e00-\u9fa5]+", text)`（僅符號/emoji）或 `text` 不含任何中英數關鍵字時，視為無效輸入，不 `setattr`，返回 `None` 使外層回退為 `pending_question` 重問（不寫入）；或複用 `is_uncertain`/`extract_fields` 皆空時回 `None`
- 驗收：`handle_text(..., "😊👍")` 後 `known_medications` 仍空或不含 `😊👍`，`reply` 不為 `你提到「😊👍」...對嗎？` 而是重問 `目前有固定吃藥...`

**3. `docs`/`orchestrator.py:652-663,355,387,250` + `schemas.py:211` — 拼接超長（§4 的 6 條）**

- `schemas.py:211` `STAGE_QUESTIONS.stage1` 74 字 → 縮至 ≤60：建議 `請問目前用藥、過敏、慢性病、家族史？（可一次說多項）`（23 字）例句改 QuickReply 提示
- `orchestrator.py:355,387` `PAUSED`/`RESUME` 與 `652,659` `checkpoint`：`f"{a}\n{b}"` 拼接後需再 `[:60]` 或改雙氣泡（先送 checkpoint/pause，再送 progress）；最簡修：`return f"{pause}\n{progress}"[:60]` 與 `f"{base}\n{progress}"[:60]`
- `orchestrator.py:250` `SIDE_ANSWER hint` 68 字 → `資料已保留，想繼續可點「繼續整理」` + 單題（或分兩段）
- 驗收：`len(reply.split("\n")[0])≤60` 或全氣泡 `len(reply)≤60`（豁免 confirm 句外）；`format_stage_progress` 已 `[:60]`，補拼接層截斷即可

### 建議修（不阻擋但加分）

- `tool.py:38-74` `build_implicit_confirm` 的 `normalized` 對長藥名串再 `[:25]` 二次截斷，避免確認句豁免外仍 80+ 字
- `test_conversation_orchestrator.py:144/167/188/226` 的 `過敏`/`什麼時候開始` 斷言旁註解關聯 `INTAKE_FIELD_QUESTIONS["allergies"/"symptom_onset"]` 防漂移

### 無需修（已 PASS）

- 安全 6 項、鐵律 3 項、P1-3 截斷、測試誠實度 — 均 PASS 保留

---

## 附錄：重現指令與 pytest

```bash
# 基準 diff
git diff a39c3a1 --stat
# 6 files changed, 376 insertions(+), 45 deletions(-)

# 全量測試
PYTHONPATH=. python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -q  # 15 passed
PYTHONPATH=. python3 -m pytest tfda_context_gate/tests/test_p1_dialog_naturalization.py -v  # 5 passed
PYTHONPATH=. python3 -m pytest tfda_context_gate/tests/ -q  # 167 passed, 10 skipped

# 對抗重現（節選）
PYTHONPATH=. python3 -c "from tfda_context_gate.intake.tool import is_uncertain_answer; print(is_uncertain_answer('不知道欸，藥名忘了'))"  # True
PYTHONPATH=. python3 <<'PY'
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository
import tempfile, pathlib
tmp=pathlib.Path(tempfile.mktemp(suffix=".sqlite3"))
repo=SQLiteProductSessionRepository(tmp)
orch=ConversationOrchestrator(repo, identity_hash_key="test-key-at-least-16-chars")
orch.handle_text(event_id="e01-1", line_user_id="U-E01", text="為自己整理")
print(orch.handle_text(event_id="e01-2", line_user_id="U-E01", text="不知道欸，藥名忘了").reply)
PY

# 長度檢查
PYTHONPATH=. python3 -c "from tfda_context_gate.intake.tool import format_stage_progress; from tfda_context_gate.intake.schemas import PreVisitIntake; print(len(format_stage_progress(PreVisitIntake())))"  # 28 ≤60
PYTHONPATH=. python3 -c "from tfda_context_gate.intake.tool import build_implicit_confirm; print(len(build_implicit_confirm('口渴、頻尿、腳麻頭暈','口渴、頻尿、腳麻頭暈')))"  # 35

# 死碼檢查
grep -rn "build_implicit_confirm_for_fields" tfda_context_gate/  # 4 命中：定義+3 生產+1 測試
grep -rn "\[:2\]" tfda_context_gate/intake/tool.py  # 1 命中：tool.py:58
```

---

## 審查方法聲明

- **安全不變量**：讀 `git diff a39c3a1` + 現場檔案 `WorkflowState`/`d_output_gate/policy.py`/`hash`/`FHIR` 對照，非僅看測試過
- **鐵律**：`grep -i "openai|mimo|llm|completion|invoke"` 於 diff 新增行 0 命中；狀態機 `status/termination_reason` 比對；`revalidate_via_a` 逐字比 `git show a39c3a1:tool.py`
- **對抗重現**：5 案例（E01/E03/E04/E09/E18）以真實口語經 `ConversationOrchestrator` 與 `tool` 直測，檢查 重述/≤2/收斂/幻覺/長度
- **過頭**：`grep` 全量新文案對「聽到你這樣我很難過」類 0 命中、對岸用語 0 命中、單輪 `len()` 實測 6 處 FAIL
- **測試誠實度**：`git diff` 4 處逐條比新舊斷言語意等價性，5 新測試推論「舊行為下是否會 FAIL」
- **P1-3**：`grep` + `ast-grep` 驗 `[:2]` 僅 `tool.py:58` 且 3 生產呼叫皆在 `NEEDS_CLARIFICATION` return 前，REVIEW 於 `graph.py:444` 承接

> 不要自己修碼，你只負責審。— 已遵守：本報告僅審查與開修清單，未改任何業務碼。
