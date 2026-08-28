# 執行計劃：Intake 對話自然化重構（Phase 1）

> 日期：2026-08-27
> 依據：`docs/research/preconsult_chatbot_research_20260827.md`（學術依據+施工圖）、`docs/research/deployed_intake_design_scan_20260827.md`（上線產品例句）
> 目標：解決 LINE 實測「回答太死板」。本計劃只做 Phase 1（文案層與確認節奏），Phase 2（Repair/Digression/自適應選題）另行立項。

## 範圍（Phase 1 必做 5 項）

| # | 改動 | 檔案 | 具體作法 |
|---|---|---|---|
| P1-1 | 去除「第 n/8 題」題號 | `intake/schemas.py:163-180` `INTAKE_FIELD_QUESTIONS`、`tool.py:291-327` | 刪除題號前綴，改用 E 章例句風格（開場引導+單題） |
| P1-2 | 隱式確認帶內容重述 | 新增確認模板（schemas.py 或新模組） | 每收一欄，回覆格式：「你提到「{原文}」，我記為「{正規化值}」，對嗎？」空泛「收到/了解」禁用 |
| P1-3 | 單輪只確認 1–2 項 | `tool.py` `extract_fields_from_utterance` 後的確認流程 | 多欄抽取後最多確認 2 項，其餘留待 REVIEW 摘要一次性確認 |
| P1-4 | 「不知道/忘記/不確定」優雅收斂 | `tool.py:182-227` 2-attempt 機制擴及 symptom 欄 | 接受為 `待確認`，句式「沒關係，先記為『待確認』，看診時再跟醫師確認」；單欄最多追問 2 次 |
| P1-5 | 階段式進度取代數字進度 | `tool.py` `get_missing_stages` / LINE 層 | 進度提示改「用藥與過敏 ✅，還差症狀那段」而非「第 5/8 題」 |

## 明確不做的（防擴張）

- 不動 8 欄位結構、`FHIR_LINKID_MAP`、`TimelineEntry` 模型
- 不動 B/D gates、deterministic 紅旗、`revalidate_via_a` 流程
- 不做 Repair 專路、Digression 側路、自適應選題（Phase 2）
- 不動狀態機節點（ConversationOrchestrator 現有 14 測試的行為語意不得改變，僅文案改變）
- 不引入新的 LLM 呼叫點（本階段純模板，零幻覺風險）

## 安全不變量（違反即驗收失敗）

1. B/D gates 不可繞過；D 仍擋診斷/治療字樣
2. 紅旗 `POSSIBLE_EMERGENCY` 固定轉 `U_URGENT_HUMAN`，路徑不經新模板
3. 不存 raw image；hash PII
4. `PreVisitIntake` StrictModel extra=forbid 行為不變
5. FHIR 輸出 unknown extension 不變

## 開發者驗收條件（oc-builder 自證）

- [ ] `python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -q` → 15 passed
- [ ] `python3 -m pytest -q` 全綠（原 74 tests；若有新測試則總數 ≥74 全過）
- [ ] 既有涉及問句文案的測試同步更新（改斷言而非刪測試；刪除需逐一說明理由）
- [ ] 冒煙：`run_workflow({'user_raw_input':'我下週要看醫生','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).question` 輸出不含「第.*題」且含單題引導句
- [ ] 新增測試覆蓋：P1-2 重述格式、P1-3 確認數上限、P1-4 待確認收斂、P1-5 進度文案

## 對抗審查者任務（oc-adversary，開發完成後啟動）

1. 逐項核對 5 個安全不變量（讀 diff 驗證，不只看測試過）
2. 用 E01–E20 案例中**Phase 1 相關**的子集（E01/E03/E04/E09/E18）手動構造輸入跑 workflow，找反例
3. 檢查「像人」沒做過頭：無過度同理句（「聽到你這樣我很難過」類禁出現）、無幻覺藥名、無新增 LLM 呼叫
4. 檢查繁中語感：例句是否自然、無對岸用語（信息/數據→資訊/資料）、長者可讀（單輪 ≤60 字）
5. 檢查測試誠實度：有沒有「把斷言改弱來讓測試過」的情事（比對 git diff 中被修改的既有測試）
6. 產出 `docs/reviews/p1_adversarial_review_20260827.md`：每項 PASS/FAIL + 反例重現步驟

## 最終驗收（Sisyphus 主導）

- 親跑 pytest 全套 + 冒煙四連（文字/intake/圖片/stream）
- 親讀 diff 抽查安全不變量與文案品質
- 讀對抗審查報告，FAIL 項退回開發者修
- 全過後更新 AGENTS.md 狀態並標記 Phase 1 完成
