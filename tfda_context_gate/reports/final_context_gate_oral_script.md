# B：Contract Gate + Context Gate 口頭報告稿

## 開場：我這次到底想解決什麼問題

大家好，我這次負責的是 B 模組，主題是 Contract Gate 加上 Context Gate。

我想處理的問題其實很直白：RAG 已經找回資料之後，這些資料是不是可以直接交給 LLM？

我們先做一個假設：RAG 不一定是 LLM 系統完全能控制的。也就是說，我們不先研究怎麼讓 RAG 永遠找對，而是研究它找回來之後，怎麼避免 LLM 直接使用不適合的 Context。

所以整個位置大概是：RAG 先找資料，Contract Gate 檢查格式，Context Gate 檢查內容，最後才交給 Generator 產生答案。

## 資料：這不是我自己編的測試資料

這次使用的是 TFDA 的藥品安全資訊風險溝通資料，一共 129 筆。

這裡要先說清楚，一筆不是一個藥品，也不是一張藥證。一筆比較像是 TFDA 在某個時間，針對某個藥品成分或藥品類別發布的一次安全資訊。

我故意選 SGLT2，是因為同一個藥品類別在資料庫裡有好幾個不同的安全主題：酮酸中毒、下肢截肢、Fournier’s gangrene，還有急性腎損傷。

這些都是真實存在於完整 129 筆資料裡的文件，不是我先挑出來，也不是我人工製造的衝突資料。

## 兩個問題

我設計兩個 Query。

第一個是窄 Query，問 SGLT2 的酮酸中毒風險。這題只想找酮酸中毒。

第二個是廣 Query，問 SGLT2 有哪些安全警訊。這題就希望酮酸中毒、截肢、Fournier’s gangrene 和急性腎損傷都能被留下來。

這裡有一個很重要的觀念：同一篇文件是不是 Relevant，不是文件自己固定的，而是跟使用者問什麼有關。

## 第一層：Contract Gate

Contract Gate 可以想成收包裹時先看基本資料有沒有填好。

我檢查 document_id、row_index、發布日期、藥品成分和 page_content。

129 筆資料全部通過，沒有拒絕的資料。

但它只知道格式，不知道內容。它不會自己判斷資料是不是最新，也不知道藥證有沒有註銷，因為原始 TFDA 主資料沒有這些欄位。

## 第二層：Similarity

Similarity 做的事情是看 Query 和文件在語意上像不像。

我用 multilingual-e5-small，把 129 筆文件放進 InMemoryVectorStore。

結果很有意思。窄 Query 的 Top-10 裡，只有 1 筆是直接相關，3 筆是部分相關，還有 6 筆完全不相關。

酮酸中毒是第一名，但截肢和 Fournier’s gangrene 也在非常前面。

這表示 Similarity 知道它們都在講 SGLT2 和藥品安全，但不一定知道使用者現在問的是哪一個安全風險。

所以高分不等於可以直接使用。

## 第三層：Reranker

既然 Similarity 有點粗，我再用 Reranker 把 Query 和每一篇候選文件仔細比一次。

我先取 Similarity Top-20，再用 bge-reranker-v2-m3 重排。

窄 Query 的變化是：酮酸中毒 Rank 1 維持 Rank 1；Fournier’s gangrene 從 Rank 3 升到 Rank 2；截肢從 Rank 2 變 Rank 3；急性腎損傷從 Rank 6 變 Rank 4。

這裡不能說 Reranker 失敗。Fournier’s gangrene 確實和 SGLT2、藥品安全有關，所以它排得前面是合理的。

但是它仍然不是使用者問的酮酸中毒。

所以 Reranker 主要改善的是排序，不是最後的可使用性判斷。

## 第四層：LLM Judge

這時候才需要 LLM Judge。

Judge 不回答醫療問題，它只判斷 Context。

它會判 DIRECT、PARTIAL 或 IRRELEVANT；也會判 SUFFICIENT 或 INSUFFICIENT；最後還會看是不是 EXACT，還是同一種藥但不同安全風險。

10 篇文件的結果是 Direct 1、Partial 3、Irrelevant 6，和人工標註 10/10 一致。

最重要的四篇結果是：酮酸中毒被判 DIRECT、SUFFICIENT、EXACT；Fournier’s gangrene、截肢和急性腎損傷都被判 PARTIAL、INSUFFICIENT、SAME_DRUG_DIFFERENT_RISK。

這正好補上 Reranker 做不到的事情：同一種藥，不代表同一個安全問題。

我們也做了一個 fallback 測試。只放錯的三個 SGLT2 安全主題，不放酮酸中毒，Judge 三次都判 FALLBACK；加入酮酸中毒之後，才判 PASS，而且只保留酮酸中毒文件。

不過 Judge 有一個很現實的問題：成本。Phase 4 一輪有 13 次 LLM call，平均約 93 秒，約 41,081 tokens。

## 第五層：Hybrid

所以 Hybrid 解決的不是「再判得更聰明」，而是「不要每篇都叫一次 LLM」。

Hybrid 先用 Contract Gate，再用 Similarity 取 Top-20，Reranker 排序後，最後只做一次 Set-level Judge。

我們測 Top-3、Top-4 和 Top-5。

Narrow Query 三種都 PASS，而且最後都只保留酮酸中毒，Precision 和 Recall 都是 1.0。

Broad Query 的 Top-3 也 PASS，但只保留 3/4 篇 Direct evidence，所以 Recall 是 0.75。Top-4 才完整保留四篇，Recall 變成 1.0。Top-5 沒有再增加 Recall。

所以如果 Query 可以自適應，Narrow 用 Top-3、Broad 用 Top-4；如果系統只能固定一個數字，我會先建議 Top-4。

## 成本比較與限制

Phase 4 Standalone 是 13 次 LLM call、93.11 秒、約 41,081 tokens。

Hybrid Narrow Top-3 是 1 次 LLM call、約 40.80 秒、約 4,867 tokens。

呼叫次數少了約 92.3%，但這裡也要誠實說，Hybrid 還是需要約 40 到 50 秒，因為 CPU Reranker 也算在 end-to-end 裡面。

所以它還不能直接叫做 real-time production solution。

## 最後結論

如果把四種方法用一句話分別說：

Similarity 在回答「像不像」；Reranker 在回答「誰比較值得排前面」；LLM Judge 在回答「這些資料真的能不能回答」；Hybrid 則是把前面的工作拆開，最後只花一次 LLM 判斷。

這次最重要的三個結果是：第一，Reranker 仍可能把同藥不同風險排很前面；第二，LLM Judge 能把真正的 Query topic 和其他安全主題分開；第三，Hybrid 能在保留這個判斷能力的同時，把 LLM call 從 13 次降到 1 次。

目前的暫時 MVP 建議是 Contract Gate、Similarity Top-20、Reranker Top-4，再做一次 Set-level LLM Judge，最後決定 PASS、FALLBACK 或 REVIEW。

但這仍然是 129 筆 TFDA corpus、主要一組 SGLT2 Query 的小型實驗，還沒有測大型 benchmark、真正 Generator 品質和正式 Conflict case。
