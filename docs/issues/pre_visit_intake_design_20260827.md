# 看診前 Intake 設計待改 - 記錄 2026-08-27

## 現況
`PreVisitIntake {known_medications, symptom_onset, symptom_description, questions_for_doctor}` + `ASK_USER 逐欄4次` + `timeline sorted` + `D 擋診斷`

## 業界對比結論（來源：HL7 FHIR R5, Epic MyChart, Medplum, Oscar Health, JMIR 2024 review 等 10 套）
**方向對，欄位過窄、流程過細、驗證過弱**

## 必改 3 點（高優先）
1. **欄位擴充 4→8-10欄，對接 FHIR**
   - 補 `allergies`（用藥安全關鍵）、`chronic_conditions`、`family_history`、`social_history`、`consent`
   - 用 FHIR `linkId` 命名（如 `allergy-substance` → `AllergyIntolerance`），`QuestionnaireResponse → $extract → Bundle`
   - 參考：HL7 FHIR Questionnaire + SDC, Medplum linkId 範例

2. **流程改 topic-chunked（3階段，非逐欄）**
   - 現 `4次 ASK_USER` → 改 `階段1 用藥/過敏`、`階段2 症狀(時間/描述/程度/持續)`、`階段3 待問醫師問題 + Review & Confirm`
   - 允許一次回答填多欄：如「吃 metformin 三個月，早上血糖180」同時填 `known_medications + symptom_onset + symptom_description`
   - 參考：NIH minimal dataset, Oscar RFV動態選題, Gravity Rail 5階段

3. **紅旗改 deterministic pre-check（LLM 前）**
   - 現 `A 重驗紅旗` 在 LLM 後 → 改 `regex` 在 LLM 前 `abort + warm handoff`
   - 命中 `POSSIBLE_EMERGENCY`（胸痛/意識不清等）立即中斷，不讓模型投票
   - 參考：voice-triage, Agent Patterns Catalog mandatory-red-flag-escalation

## 次優先
4. Timeline 從 `sorted` 升級 Gantt + 實體抽取（NER/RE）
5. 增加 Review & Confirm 覆核 + confidence 視覺化，擋病人幻覺（身高體重顛倒等）

## 影響範圍
- `intake/schemas.py`、`intake/tool.py`、`workflow/graph.py` 的 `ASK_USER` 分流、`a_router/rules.py` 紅旗、`d_output_gate/policy.py`

## 狀態
- 已記錄，待排入 v0.2 前置（資料前置完成後）
- 來源報告：librarian `bg_407cca47` 2026-08-27
