# P5 對話自然化計畫（Conversation Flexibility）— 2026-08-28

> 依據：真實 LINE 對話測試（22:01-22:04 transcript）+ 業界掃描 `docs/research/conversation_flexibility_industry_scan_20260828.md`（36+ 來源）+ 外部 AI 分析交叉驗證（兩者診斷一致）。
> 性質：體驗修復輪。**不動任何安全閘門**（B/D gate、紅旗 pre-check、緊急路徑原封不動）。

## 1. 真實對話暴露的 5 個痛點

| # | 使用者說 | Bot 回 | 痛點 |
|---|---|---|---|
| 1 | 「你好」×2 | 兩次一字不差的介紹 | 無記性、無變化 |
| 2 | 「你是誰」「是誰」×2 | 「可以多說一點嗎？」×2 | 身份問答答非所問（白名單缺變體）+ fallback loop |
| 3 | 「我不知道啊」 | 突然啟動看診整理流程 | 低信心誤入 workflow（危險設計） |
| 4 | 「你好不人性化噢」 | 「這個我幫不上」 | 抱怨被冷拒（無共情路由） |
| 5 | 開場自稱「衛教小幫手」、後稱「看診前整理助理」 | — | persona drift（身份不一致） |

## 2. 第一波修復（本計畫範圍）：零 LLM 成本、純規則

### P5-1 身份問答專路
- `a_router/rules.py` 與 `graph.py` 的 chitchat 白名單補：`你是誰|你是AI|你是機器人|叫什麼|什麼名字|怎麼稱呼`
- 回覆：persona 自介「您好，我是糖尿病衛教小幫手（非真人，依 TFDA／國健署衛教文件回答）。能幫您：🥗 衛教 📋 看診前整理 💊 藥袋查詢。個人用藥請諮詢醫師/藥師。」
- 準備 2-3 個變體輪替

### P5-2 罐頭句 Variation Pool + 去重
- `workflow/fallbacks.py`：`O_GENERIC / CHIT_CHAT_OUT_OF_SCOPE / Q_NEED_MORE / B_INSUFFICIENT` 各寫 3 句同義（台灣敬語、emoji 節制）
- `random.choice` 輪替 + session 內 seen-set：同一句話同 session 不重複出現
- 尾巴帶 Quick Reply 或文字選項（為什麼會有糖尿病／飲食怎麼吃／上傳藥袋／我能幫什麼）

### P5-3 intake「不知道」不污染
- `intake/tool.py` / `orchestrator.py`：ACTIVE 態命中「不知道/不清楚」→ **不寫入欄位**，回「沒關係，這題先記為待確認～」+ 重問或推進（attempts≥2 才標待確認）
- 「我不知道啊」不得觸發任何 workflow 啟動

### P5-4 去重 TTL 語境化
- `line_bot/app.py`：welcome/chitchat 的 TEXT_DEDUP_TTL 120s→10s（formal 長句維持 120s）
- 第二次「你好」→ 差異化回覆「又見面了～有什麼想繼續的？」

### P5-5 共情三段式
- 抱怨/挫折詞（不人性化|好笨|很怪|無言|敷衍）→ 「收到/抱歉 → 簡短承認 → 給 3 個具體選項」三段式
- 醫療不過界：不附和醫療判斷、情緒困擾嚴重時可給 1925 安心專線

## 3. 紅線（本計畫不可違反）

- 紅旗 pattern 表（risk_policy.py）零改動；紅旗 pre-check 位置（LLM 之前、同步）不動
- B 閘門 / D 閘門 / 緊急路徑（U_*、A_EMERGENCY）不碰、不走溫和話術
- LLM 不直寫 intake 欄位；不擴大醫療自由生成
- fallback 變體的醫療免責語必須保留

## 4. 驗收標準（Sisyphus 親測）

1. pytest 全綠（205 基準 + 新增單元測試）
2. 紅旗 5 條回歸（直接/間接/洗白/否定/轉折）全攔截
3. **真實對話重放**：22:01-22:04 transcript 的 7 條訊息逐條重放，不得再出現：罐頭重複、身份答非所問、我不知道誤觸發、冷拒抱怨
4. 快路延遲維持 <100ms（純規則）
5. 紅旗詞與新白名單詞零重疊（單元測試驗證）

## 5. 第二波候選（未來，不在本計畫）

- 對話歷史注入（k=5 turns）+ 罐頭句 LLM 改寫（小模型短 prompt）
- 統一 persona 檔（解 persona drift）
- 圖文選單 / Flex 能力卡
