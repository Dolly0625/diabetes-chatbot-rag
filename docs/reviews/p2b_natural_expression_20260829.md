# P2B 自然表達層評估（2026-08-29）

## 結論

已在 `tonight-p2b` 完成一個只負責呈現的 deterministic response composer。它接收既有 interpreter/intake merge 已選出的欄位與答案，不重新判斷意圖、不呼叫 LLM，也不修改紅旗、授權、PendingAction 或 D gate。

目前適合進入 P2B shadow/demo 比較；不需要新增第二次串行模型呼叫。Semantic Router 仍未接到 production。

## 改動

- `line_orchestration/response_composer.py`：集中管理單欄追問、跨輪回接、隱式確認、修正、不確定與明確「沒有」的短句。
- `orchestrator.py`：既有 production construction path 使用 composer；身份回覆改為既有 fallback pool 的 session 內輪替，仍在 interpreter 前處理。
- `workflow/fallbacks.py`：身份與共情變體改成較口語的繁體中文，保留 TFDA／國健署依據、非真人／非醫師、不提供診斷、個人用藥諮詢與 119 邊界。
- 歡迎與重複問候沿用既有三句輪替池，但移除第一句的編號式選單。
- `scripts/p2b_natural_expression_eval.py`：不啟動 workflow/LLM 的 golden smoke script，輸出 JSON 供 demo 前後比較。
- `tests/test_p2b_natural_expression.py`：涵蓋未見口語、跨輪 SIDE_ANSWER、低信心、隱式確認、身份輪替、紅旗優先、interpreter call count 與 claim boundary。

## 文案前後示例

| 情境 | 原本 | P2B |
|---|---|---|
| 用藥追問 | `目前有固定吃藥或打胰島素嗎？知道藥名就直接說；不確定也沒關係。` | `先從用藥開始：目前有固定吃藥或打胰島素嗎？知道藥名就直接說，不確定也沒關係。` |
| 回到 intake | `資料已保留，想繼續可點「繼續整理」：` | `資料已保留。這題先到這裡；想繼續整理時按「繼續整理」就好。下一步是：` |
| 隱式確認 | `你提到「平常有吃 metformin」，我記為「metformin」，對嗎？` | `你提到「平常有吃 metformin」，我記為「metformin」，對嗎？如果不對，直接告訴我就好。` |
| 不確定 | `沒關係，我先把這一項標成「待看診確認」，不會替你猜。` | `沒關係，我先把這項記成「待看診確認」，不替你猜；之後想補充再告訴我。` |

原始衛教答案在 SIDE_ANSWER 中不被重寫，只在後面加上返回提示與既有 pending question；因此自然化不會變成新的醫療內容。
固定的 `HONEST_FALLBACK_TEXT` 保持不變，避免把安全轉介語句改成未驗證的自由改寫。

## 測試與限制

- P2B targeted：8 tests passed；composer/intake regression：12 tests passed。
- 既有 intake、LINE callback、interpreter/multi-intent、P2A.1、async、PendingAction 回歸：101 tests passed。
- 最終全套 pytest：504 tests passed（3 個既有 deprecation warnings）。
- golden smoke：5/5 cases passed，`python3 scripts/p2b_natural_expression_eval.py`。
- 身份與紅旗測試確認：身份路徑不呼叫 interpreter/workflow；身份＋紅旗仍先回 119/急診固定安全句。
- 低信心測試確認：同一輪只消費一次既有 interpreter 結果，沒有 rephraser。
- 本評估未啟動 live Formal LLM；模型內容與醫療正確性仍由既有 workflow、B/D gate 負責。後續若進 shadow mode，需用 production trace 另量測回覆延遲與真人偏好，不可讓 composer 取代 safety gate。
