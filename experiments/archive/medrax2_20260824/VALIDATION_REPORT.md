# 實驗驗證報告

## 驗證範圍

本報告只驗證隔離資料夾內的 architecture spike 是否照設計運作，不代表醫療、臨床或正式產品驗證。

## 自動測試結果

執行指令：

```bash
python3 -m py_compile $(rg --files tfda_medrax2_experiment -g '*.py')
pytest -q tfda_medrax2_experiment/tests
python3 -m pytest -q tfda_medrax2_experiment/tests
```

結果：

- Python compile：通過。
- `pytest`：17 passed。
- `python3 -m pytest`：17 passed。
- 測試時間約 0.3 秒；沒有失敗。

涵蓋行為包括：

- 正常 SGLT2 問題完成三輪 agent/tool trajectory。
- 同一輪兩個 retrieval calls 都被執行，結果順序可預期。
- 不在 allowlist 的 tool 被阻擋。
- tool input schema 錯誤被正規化。
- 工具失敗不讓 graph crash。
- 相同呼叫第二次命中 deterministic cache。
- 同一 thread 的 history 存在，但當次結果不混入前一次 run。
- looping model 會被 step/tool limit 終止。
- 無證據、未核准 citation、缺範圍聲明與個人化指令會 fail closed。
- corpus duplicate ID 會在載入時拒絕。
- Latin drug class anchor 避免只因共同中文詞「抑制劑」誤命中其他藥物類別。
- 英文問句中的 `what`、`are` 等普通詞不會被誤當成藥名錨點。
- 真實 129 筆 corpus 的 SGLT2 run 中，每一筆核准 evidence 都實際含有 SGLT2 主題。

## 真實語料 Demo

執行：

```bash
python3 -m tfda_medrax2_experiment.agent_lab.demo --json
```

使用 workspace 既有的 129 筆 TFDA processed documents，沒有 fixture 替代。觀察結果：

```text
status: COMPLETED
termination: OUTPUT_PASS
agent steps: 3
tools:
  - search_tfda_risk_communications
  - lookup_tfda_ingredient_risks
  - inspect_tfda_evidence_set
approved evidence:
  - tfda-risk-0019  SGLT2 抑制劑／酮酸中毒
  - tfda-risk-0042  SGLT2 抑制劑／下肢截肢潛在風險
  - tfda-risk-0064  SGLT2 抑制劑／會陰部壞死性筋膜炎
  - tfda-risk-0035  canagliflozin、dapagliflozin／急性腎損傷
```

第一個 agent turn 的兩個查詢工具平行執行，第二個 turn 檢視 evidence set，第三個 turn 產生引用前三筆證據的草稿。A、B、D 的 trace 分別為 PASS、PASS、PASS。

## 原專案回歸測試

因為本實驗只應讀取既有 corpus、不應改變原專案，所以另行執行：

```bash
python3 -m pytest -q tfda_context_gate/tests
```

結果為 `74 passed, 10 skipped`。Skipped 項目是測試本身的條件式跳過，沒有新增 failure；這支持本次隔離實驗沒有破壞原有 agent/runtime、gates、retriever 與 workflow 測試。

## 自我檢查時發現並修正的問題

第一次真實語料執行時，query 含 `SGLT2 抑制劑`，早期 lexical scorer 因中文 bigram「抑制／制劑」而讓 JAK、CDK 抑制劑資料進入候選集合。

修正方式：若 query 含具辨識力的 Latin token（例如 `sglt2`），先要求該 anchor 必須存在於 ingredient 或文件內容，再做中文/英文詞彙評分。修正後的真實 demo 不再包含 JAK/CDK，並新增 regression test 防止復發。

這也顯示 B gate 目前只做 provenance/schema 核准仍不夠：錯誤主題的資料也可能來自正確來源。正式版本必須增加 query-evidence relevance/entailment gate。

## 已知環境警告

測試與 demo 會出現兩個第三方環境警告：

1. 系統 Python 3.9 的 `ssl` 使用 LibreSSL，而目前安裝的 urllib3 v2 偏好 OpenSSL 1.1.1+。
2. 目前 LangGraph 版本的 `MemorySaver` serializer 對未來 `allowed_objects` 預設值變更發出 pending deprecation warning。

兩者都沒有造成此輪測試失敗，但若進入正式服務，應使用隔離 virtual environment、鎖定相依版本，並明確配置 checkpoint serializer。

## 尚未被驗證的事情

- 沒有驗證模型生成答案的臨床正確性。
- 沒有使用真正外部 LLM provider 做整合測試。
- 沒有測高併發、process restart、分散式 checkpoint/cache。
- 沒有測外部 API timeout，因目前工具全為本機唯讀資料。
- 沒有完整 adversarial prompt 與醫療政策資料集。
- 沒有建立 retrieval recall、precision 或回答品質 benchmark。
- 沒有 FastAPI/UI，因此沒有驗證 streaming、auth、rate limit 與 client cancellation。

## 結論

這個隔離實驗已證明「MedRAX2 式動態工具迴圈 + 原專案強制安全閘門」可以共同存在，且核心控制流可離線、快速、deterministic 地測試。它已達到可供學習與下一輪工程演進的 architecture spike 水準，但不能宣稱已是 MedRAX2 同等產品或可上線醫療 agent。
