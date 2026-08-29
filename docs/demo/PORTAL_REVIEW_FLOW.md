# Portal Review／唯讀摘要 Demo

這份文件描述今晚的病患與醫護 portal 展示流程。portal 沿用既有 ProductSession、ShareGrant 與 D Output Gate，不另外建立病歷或編輯 API。

## 展示流程

1. 病患從 `/patient` 開啟入口。正式模式使用 LIFF ID token；本機 Demo 只有在明確開啟 identity-header 開關時，才可輸入測試用 LINE user ID。
2. 按「載入看診資料」後，Review 卡片會逐欄顯示 8 個欄位。每欄狀態是「已提供」、「待看診確認」或「尚未提供」，並顯示目前整理階段與確認狀態。
3. 確認前，病患回 LINE 對話檢查或修正資料，再回覆「確認完成」。portal 目前是唯讀 Review，不在瀏覽器直接改寫 intake。
4. 狀態變成「已確認，可分享」後，病患可選填 Demo 醫護 ID，建立 10 分鐘、單次使用的分享碼。分享碼只在建立回應中顯示；病患可以在醫護使用前按「立即撤銷」。
5. 醫護從 `/clinician` 輸入自己的 Demo ID 與病患分享碼。伺服器只在 `LINE_DEMO_MODE` 開啟且 ID 命中 `DEMO_CLINICIAN_IDS` allowlist 時允許讀取，並在成功讀取後消耗單次分享碼、留下 access audit。
6. 醫護看到的是 D gate 通過的唯讀摘要、資料來源、8 欄內容、缺漏／待確認、病患想問的問題、免責聲明與讀取時間；沒有病患資料寫入控制項，也不能列舉其他病患。

## 錯誤狀態

- 沒有 LIFF 驗證、Demo header 未開啟：病患 portal 顯示身分驗證失敗。
- Demo clinician 關閉、ID 不在 allowlist：醫護 portal 顯示入口未啟用或身分未授權。
- 分享碼過期、已使用、已撤銷、指定給其他醫護：醫護 portal 顯示對應原因，且不呈現摘要內容。
- 未確認的病患資料：分享按鈕保持停用，後端也會以 `409` 拒絕建立 grant。

## 邊界與限制

- Demo clinician header allowlist 不是院方 IAM；正式導入必須替換為院方 SSO/OIDC，不能把 header 當成臨床授權。
- 一次性 token 適合展示與短暫交接，不是長期病歷分享。token 不應貼到公開頻道。
- 病患仍需在 LINE 完成確認；portal 不提供修改、刪除或診斷功能。
- 所有資料都應使用合成資料或病患主動輸入的 Demo 資料；頁面不保存原始藥袋影像，也不回傳 LINE user ID、token hash 或 principal hash。
