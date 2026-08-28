# Real TFDA Role-based Smoke Report

執行日期：2026-08-21  
Retriever：`TFDADrugSafetyRetriever`  及
`intfloat/multilingual-e5-small` + `InMemoryVectorStore`  
Corpus：`data/processed/langchain_documents.json`（129 筆）  
Top-k：5

`expected_match` 代表 top-k evidence 的內容與 metadata 包含該題預期的
藥品／安全主題 terms；不是臨床正確性判定。

| Case | declared_role | A status | top-k evidence_id | top-1 藥品成分 | top-1 score | top-1 發布日期 | expected_match |
|---|---|---|---|---|---:|---|---|
| P1 | PATIENT | G_GENERAL_EDUCATION | tfda-risk-0100, tfda-risk-0042, tfda-risk-0051, tfda-risk-0074, tfda-risk-0026 | Insulin | 0.8957 | 2020/11/16 | true |
| P2 | PATIENT | G_GENERAL_EDUCATION | tfda-risk-0042, tfda-risk-0026, tfda-risk-0064, tfda-risk-0096, tfda-risk-0019 | SGLT2抑制劑類 | 0.8992 | 2017/3/22 | true |
| P3 | PATIENT | M_MEDICATION_REFERRAL | — | — | — | — | N/A；A_BLOCK |
| H1 | HEALTHCARE_PROFESSIONAL | G_GENERAL_EDUCATION | tfda-risk-0042, tfda-risk-0064, tfda-risk-0019, tfda-risk-0026, tfda-risk-0065 | SGLT2抑制劑類 | 0.9121 | 2017/3/22 | true |
| H2 | HEALTHCARE_PROFESSIONAL | G_GENERAL_EDUCATION | tfda-risk-0100, tfda-risk-0065, tfda-risk-0051, tfda-risk-0042, tfda-risk-0026 | Insulin | 0.9309 | 2020/11/16 | true |
| H3 | HEALTHCARE_PROFESSIONAL | G_GENERAL_EDUCATION | tfda-risk-0100, tfda-risk-0042, tfda-risk-0051, tfda-risk-0026, tfda-risk-0020 | Insulin | 0.9079 | 2020/11/16 | true |
| C1 | CAREGIVER | G_GENERAL_EDUCATION | tfda-risk-0042, tfda-risk-0064, tfda-risk-0100, tfda-risk-0019, tfda-risk-0026 | SGLT2抑制劑類 | 0.9136 | 2017/3/22 | true |
| C2 | CAREGIVER | G_GENERAL_EDUCATION | tfda-risk-0100, tfda-risk-0012, tfda-risk-0126, tfda-risk-0042, tfda-risk-0003 | Insulin | 0.8956 | 2020/11/16 | true |
| C3 | CAREGIVER | G_GENERAL_EDUCATION | — | — | — | — | N/A；future ASK_USER candidate |

Workflow integration verification：P1/P2/H1/H2/H3/C1/C2 使用 real retriever、
deterministic B demo approval 與 evidence-aware C fixture 均完成 `D=PASS`。
P3 在 A 後 `BLOCKED`；C3 不進行 retrieval，避免在藥物類型不明時假設為
SGLT2。角色只影響請求 metadata 與未來回答表達，不改變 evidence truth 或
任何 A/B/D 權限。
