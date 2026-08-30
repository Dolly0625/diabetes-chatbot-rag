# 分層快速路由與 Demo 延遲改善 — 生產級語意路由研究與設計決策

> 日期：2026-08-30  
> 作者：Sisyphus（TFDA Diabetes Agent 專案）  
> 階段：第一階段 — 網路研究與專案盤點（**嚴禁 production 程式碼**，只提交本研究文件）  
> 專案位置：`/Users/dolly/Documents/code/tfda-diabetes-agent`（Python 3.10.20，`.venv`，完整測試基準 537 passed @ `91f403b`）  
> 方法：優先官方文件、原始 repo、正式論文；二手部落格僅作佐證，不單獨作為依據。所有主張均附可點擊網址、發布者、日期/版本。延遲與準確率數據僅引用實測或論文公開值，不捏造。  
> 來源基準：2026-08-30 當日抓取；官方文件以當日 Permalink 為準。

---

## 0. 摘要與結論先行

本研究回答兩個問題：(1) 業界如何做低成本、本地、可回退的語意分流？(2) 本專案既有 `semantic-router-eval` 為何尚未上線，哪些元件可重用？

**業界共識**是三件套 `Encoder → Index → Router` 疊加 `Signal → Policy → Confidence` 決策，再以 `fallback = UNKNOWN → LLM` 封口；閾值無萬用值，必須以 held-out 人審語料做 `fit()`/`evaluate()` 個性化校準；相似度採 dense 用 cosine、sparse 用 dot；多訊號投票優於單一 embedding。

**本專案結論**：採用**分層混合路由**，但 `Semantic Router` 僅作** shadow-only 本地分流**，不取得醫療資料寫入權，不取代紅旗/授權/產品命令閘。短期優先 deterministic fast path + shadow logging，待更大去識別語料與 threshold 重校後，再以高信心 `PURE_EDUCATION`/`CHITCHAT` 試行 fast route；`MIXED` 永遠低信心升級。詳見 §12。

---

## 1. Aurelio Labs `semantic-router` — routing / threshold / encoder / fallback

### 來源清單（可點擊）

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| 原始 Repo | https://github.com/aurelio-labs/semantic-router | Aurelio Labs / Aurelio AI，MIT，Created `2023-10-30` | Docs 以 `main` Permalink 為準 |
| 官方 Docs 首頁 | https://docs.aurelio.ai/semantic-router/get-started/introduction | Aurelio AI | — |
| 架構 | https://docs.aurelio.ai/semantic-router/user-guide/concepts/architecture | Aurelio AI | — |
| Routers 組件 | https://docs.aurelio.ai/semantic-router/user-guide/components/routers | Aurelio AI | — |
| Encoders 選型 | https://docs.aurelio.ai/semantic-router/user-guide/components/encoders | Aurelio AI | — |
| Threshold 優化 | https://docs.aurelio.ai/semantic-router/user-guide/features/threshold-optimization | Aurelio AI | — |
| Semantic Router 指南 | https://docs.aurelio.ai/semantic-router/user-guide/guides/semantic-router | Aurelio AI | — |
| Notebook | https://github.com/aurelio-labs/semantic-router/blob/main/docs/06-threshold-optimization.ipynb | Aurelio AI | — |
| Issue 140：score_threshold 意義 | https://github.com/aurelio-labs/semantic-router/issues/140 | Aurelio Labs | — |
| Discussion 61：threshold 如何決定 | https://github.com/aurelio-labs/semantic-router/discussions/61 | aurelio-labs | — |
| 核心原始碼 BaseRouter | https://raw.githubusercontent.com/aurelio-labs/semantic-router/main/semantic_router/routers/base.py | Aurelio Labs | `main` |
| Route 模型 | https://raw.githubusercontent.com/aurelio-labs/semantic-router/main/semantic_router/route.py | Aurelio Labs | `main` |
| HuggingFaceEncoder | https://raw.githubusercontent.com/aurelio-labs/semantic-router/main/semantic_router/encoders/huggingface.py | Aurelio Labs | `main` |
| PyPI 版本 | https://pypi.org/project/semantic-router/ | Aurelio AI | v0.1.15/0.1.16，Python `>=3.9,<3.14` |

### 業界常見架構（此一家）

**Routing 設計**：`Encoder → Vector Embedding → Router ↔ Index → Matched Route → Handler`。`Route` 含 `name/utterances/description/function_schemas/llm/score_threshold/metadata`；`Router` 分 `SemanticRouter`（單 dense，`LocalIndex`/`Pinecone`/`Qdrant`/`Postgres`）與 `HybridRouter`（`encoder` dense + `sparse_encoder` BM25/TFIDF/`AurelioSparseEncoder`/`LocalSparseEncoder` + `HybridLocalIndex` + `alpha` 權重；`alpha=0` 純 dense，`alpha=1` 純 sparse，官方範例 `alpha=0.3` 即 70% dense / 30% sparse）。

```python
from semantic_router import Route, SemanticRouter
from semantic_router.encoders import OpenAIEncoder
router = SemanticRouter(encoder=OpenAIEncoder(), routes=[weather, greeting])
router("What's the forecast?")  # → RouteChoice(name='weather', score≈0.92)
# Hybrid
from semantic_router.routers import HybridRouter
router = HybridRouter(encoder=OpenAIEncoder(), sparse_encoder=AurelioSparseEncoder(),
                      routes=routes, index=HybridLocalIndex(), alpha=0.3)
```

**Threshold 機制**：若向量相似度分數 > `Route.score_threshold` 則通過，否則回 `None`。三層：全局 `score_threshold`（由 encoder 預設；OpenAI/Cohere 0.5–0.8，`HuggingFaceEncoder` 預設 `0.5`，見 `huggingface.py:127`）、per-route 覆蓋、以及 `fit(X, y)`/`evaluate(X, y)`/`get_thresholds()` 的 learned 校準（官方 Notebook 展示 500 迭代後可顯著提升，`top_k` 預設 5，`aggregation` 可選 `mean|sum|max`）。

**Encoder 支援**：Dense（OpenAI、Cohere、`HuggingFaceEncoder`、FastEmbed、Mistral、Google、Bedrock）與 Sparse（BM25、TFIDF、AurelioSparse、`LocalSparseEncoder(naver/splade-v3)`）、Multimodal（CLIP/ViT）。任意 HF 模型可載入：`HuggingFaceEncoder(name="BAAI/bge-m3")`（以 `AutoModel/AutoTokenizer` 載入，無硬編碼白名單）。本地執行 `pip install "semantic-router[local]"` 搭配 `HuggingFaceEncoder` + `LlamaCppLLM`，宣稱本地模型如 Mistral 7B 在多數測試優於 GPT-3.5。

**Fallback/Decision**：原始碼 `base.py:_pass_routes()` 若無通過閾值的路由，回 `RouteChoice(name=None)`（不拋錯），上層自行 fallback 至 LLM 或預設流程；支援 `retrieve_multiple_routes()` / `limit>1` 多路回傳、`route_filter` 白名單、`acall()`/`aadd()` 非同步、`auto_sync="local|remote"`。

### 適合本專案的部分

- **本地極速前置**：`HuggingFaceEncoder` 可 CPU 執行，搭配 `HybridRouter(alpha≈0.3–0.5)` 同時兼顧醫學術語精準匹配與口語泛化，適合做毫秒級初篩，避免每輪皆付 6–8 秒 Conversation Interpreter 成本。
- **Per-route 閾值 + `fit()`**：7 子域可個別校準（0.05–0.8 範圍），適合資料少但需可解釋的場域；fallback 明確為 `None → Tier-2 LLM`，符合醫療安全「不得直接寫入」需求。
- **bge-m3 就緒**：`BAAI/bge-m3` 多語/長文 8192/sparse+dense+ColBERT 三合一，適合中英文混合衛教問句，優於 `all-MiniLM`。
- **Healthcare 範例**：官方含 `healthcare-administrative-routing.ipynb`（七種醫療行政流程、本地路由、threshold 優化與評估）。

### 不適合的部分

- `utterances` 品質敏感（每路由 <5 條易漂），需持續人工維護；缺乏原生 `confidence` 分布量化，需外加 `evaluate` + 人審。
- `LocalIndex` 無持久化，大規模需接 Qdrant/Postgres（增加維運）。
- Dynamic route 依賴 LLM 抽參，醫療場域不宜讓路由層直接調 LLM 取參數。

---

## 2. vLLM Semantic Router — signal / policy / confidence / production routing

### 來源清單

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| 原始 Repo | https://github.com/vllm-project/semantic-router | vLLM Project / vLLM Semantic Router Team，Apache 2.0，Created `2025-08-26` | `v0.3 Themis` 2026-06-05，`v0.2 Athena` 2026-03-10 |
| 官方站首頁 | https://vllm-semantic-router.com/docs/intro/ | vLLM SR Team | `v0.3` canonical |
| Routing Pipeline | https://vllm-semantic-router.com/docs/overview/signal-driven-decisions/ | vLLM SR Team | — |
| Signal 概覽 | https://vllm-semantic-router.com/docs/tutorials/signal/overview/ | vLLM SR Team | — |
| 配置總覽 | https://vllm-semantic-router.com/docs/installation/configuration | vLLM SR Team | `version: v0.3` |
| Canonical v0.3 YAML | https://raw.githubusercontent.com/vllm-project/semantic-router/main/config/config.yaml | vLLM SR Team | `version: v0.3`，600+ 行 |
| 生產整合提案 | https://github.com/vllm-project/semantic-router/blob/29aba60e/website/docs/proposals/production-stack-integration.md | vLLM SR Team | commit `29aba60e` |
| 論文 TeX | https://github.com/vllm-project/semantic-router/blob/main/paper/main.tex | vLLM SR Team | `@misc{semanticrouter2025}` |
| 官方 Recipe | https://github.com/vllm-project/semantic-router/blob/main/config/recipes/balance/config.yaml | vLLM SR Team | — |
| Blog v0.3 Themis | https://vllm.ai/blog/2026-06-05-v0.3-vllm-sr-themis-release | vLLM | 2026-06-05 |
| Blog Signal-Decision | https://blog.vllm.ai/2025/11/19/signal-decision.html | vLLM | 2025-11-19 |
| 生產堆疊整合文件 | https://docs.vllm.ai/projects/production-stack/en/vllm-stack-0.1.11/use_cases/semantic-router-integration.html | vLLM | — |

### 業界常見架構（此一家）

**Signal 定義**（檢測與決策分離；*Keep signals detection-only*）：`routing.signals` 下分 Heuristic（`keywords` BM25/ngram/fuzzy、`structure` regex/density、`context` tokens、`conversation`/`language`/`metadata`）與 Learned（`embeddings` threshold 0.72–0.75、`domains` MMLU、`complexity` needs_reasoning hard/easy、`classifier` SAFE/JAILBREAK、`jailbreak` hybrid 0.8、`pii` 0.85、`kb` privacy_kb、`fact_check`/`preference`/`reask` 0.8 等 15+ 家族）。

**Policy 配置**：`Signal → Projections → Decisions` 三層。`Projections` 含 `partitions`（exclusive，temperature 0.3）、`scores`（`weighted_sum`）、`mappings`（`threshold_bands` → `support_fast/balanced/escalated` 具名分流）。`Decisions` 以 `priority`（200→125）與 `rules: {operator: AND/OR/NOT, conditions:[{type: domain,name: law}]}` 的遞迴布林樹選勝；`strategy: priority` 決定勝者，`emits`/`plugins` 掛載。

**Confidence 計算**：7 種 Algorithm，關鍵為 `confidence` 型：`threshold: 0.72, confidence_method: hybrid, hybrid_weights: {logprob_weight:0.65, margin_weight:0.35}, escalation_order: small_to_large, cost_quality_tradeoff:0.45`（小模型先跑，低 confidence 升級）。另有 `static`、`ratings`、`router_dc`（`min_similarity:0.7, dimension:384`）、`automix`（`verification_threshold:0.78`）、`remom`（`breadth_schedule:[3,2]`）、`fusion`、`workflows`。

**Production Routing**：`semantic_cache`（Redis HNSW `M:16 efConstruction:64`, dimension 256, COSINE，`similarity_threshold:0.85`, `ttl:3600`，`response_cache: exact_then_semantic, scope: user`，RAG `cache_ttl:600`）、`max_concurrent:3`、`round_timeout_seconds:90-120`、副本 2 自動擴 2→10（70% CPU）、Envoy 外掛 gRPC `ExtProc`、Milvus 向量庫、PII/jailbreak 預檢、Multi-signal 分類（keyword → embedding → ModernBERT）。Fail-safe：Semantic Router 不可用時 Envoy 透傳原請求，回退至 Production-Stack 既有 `round-robin/prefix-aware/KV-aware`。

### 適合本專案的部分

- **Signal→Projection→Decision 四層分離**極適合對應本專案「紅旗/授權/產品命令 → Semantic Router → LLM」的硬約束分層；`clinical_intent 0.72 + complexity hard` 等可直接映射糖尿病 `PURE_EDUCATION/PURE_INTAKE/MIXED`。
- **Policy 即程式碼**：`priority + AND/OR/NOT` 可表達「授權/本地處理 等硬規則先於成本優化」，符合醫療法規可審核性。
- **Confidence Hybrid** 是業界最完整的「小→大模型升級」範式，`0.72 + logprob 0.65/margin 0.35` 可作為 Tier-1→Tier-2 閾值起點。
- **Caching 策略**（`0.85 semantic`）可降低重複衛教問句成本。

### 不適合的部分

- 基礎設施過重（K8s/Redis/Envoy/Milvus）對 TFDA 單機/地端過度設計；本專案可僅摘取 signal/policy/confidence 概念，改 `sqlite + LocalIndex` 輕量實現。
- 面向多模型艦隊（MoM），若僅 2–3 模型，多數算法（`remom/fusion`）不需要。
- 需收集路由回饋才發揮 `adaptation: observe/apply` 價值，冷啟動期無效。

---

## 3. Sentence Transformers 官方 — semantic similarity / classification

### 來源清單

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| 原始 Repo | https://github.com/huggingface/sentence-transformers | UKPLab (TU Darmstadt) → Hugging Face，Apache 2.0，Created `2019-07-24` | — |
| 官方 Docs 首頁/Quickstart | https://sbert.net/ · https://www.sbert.net/docs/quickstart.html | Hugging Face / UKPLab | Docs 含 `v5→v6 migration`，Python 3.10+, PyTorch 2.2+ |
| STS 官方做法 | https://sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html | Hugging Face | — |
| 相似度 API | https://sbert.net/docs/package_reference/util/similarity.html | Hugging Face | — |
| Encoder 選型 | https://sbert.net/docs/sentence_transformer/pretrained_models.html | Hugging Face | — |
| Semantic Search 範例 | https://www.sbert.net/examples/sentence_transformer/applications/semantic-search/README.html | Hugging Face | — |
| 訓練總覽 | https://sbert.net/docs/sentence_transformer/training_overview.html | Hugging Face | — |
| Sparse/SPLADE | https://sbert.net/docs/sparse_encoder/pretrained_models.html | Hugging Face | — |
| STS 訓練 | https://sbert.net/examples/sentence_transformer/training/sts/README.html | Hugging Face | — |
| Computing Embeddings | https://www.sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html | Hugging Face | — |

### 建議作法（官方）

**STS**：`SentenceTransformer.encode()` 產生固定長度向量 → `model.similarity(emb1, emb2)`（預設 cosine）或 `util.cos_sim`。Cross-Encoder（`CrossEncoder("cross-encoder/stsb-roberta-base").predict([("It's sunny!","It's sunny today!")]) → 0.60`）作高精重排；SBERT 常用 `Retrieve & Re-Rank` 兩階段（先 bi-encoder 檢索，再 cross-encoder 重排）。

**Classification**：非獨立分類頭，而是以語意搜尋 + 閾值判定；Sparse 可另作分類。官方 Pretrained 含 `all-MiniLM-L6-v2`（通用 384-dim）、`all-mpnet-base-v2`、多語 `paraphrase-multilingual-*`、`multi-qa-*`/`msmarco-*`、長文 `BGE-M3 (8192)`。訓練以 `MultipleNegativesRankingLoss` / `CosineSimilarityLoss`（含 `CoSENTLoss`/`AnglELoss`）微調，資料採 `(anchor, positive, negative)` triple。

**相似度計算**：

| 類型 | 官方推薦 | API | 正規化 |
|---|---|---|---|
| Dense (SentenceTransformer) | **Cosine**（預設，`SimilarityFunction.COSINE`） | `model.similarity()`, `util.cos_sim(a,b)` → `dot/(‖a‖‖b‖)` | `encode(normalize_embeddings=True)` 後 `cosine == dot`，後者更快 |
| Sparse (SPLADE/CSR) | **Dot Product** | `model.similarity()`（sparse 預設 dot），`util.dot_score(a,b)` | 不正規化，>99% 稀疏 |

STS 訓練採 `STSb` 0–1 歸一、loss `CosineSimilarityLoss` 最大化相似句 cosine、最小化相異句。官方 **無萬用閾值**，明示「相似度閾值需經資料校準」（如近重複圖像 `0.99`、semantic cache/路由需 `evaluate + ROC 選 F1 max`）。

### 適合 / 不適合

- **適合**：`SentenceTransformer` 即 `HuggingFaceEncoder` 底座；直接複用 `BAAI/bge-m3` 可獲最佳中文醫療語意；`cosine (dense) + dot (sparse)` 與 `HybridRouter(alpha)` 一致；`CrossEncoder` 適合 Tier-2 精審（高召回→高精）。
- **不適合**：SBERT 本身不是 Router，無 `Route/Decision` 抽象，需搭配 `semantic-router` 或自製政策層；無內建閾值管理，需自建 `evaluate + fit`；大批量需 `multi-process/ONNX/OpenVINO` 加速。

---

## 4. SetFit — 官方文件與原始論文，標註量是否適合

### 來源清單

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| 原始論文 | https://arxiv.org/pdf/2209.11055 | Lewis Tunstall, Nils Reimers, Unso Eun Seo Jo, et al.（Intel Labs + UKP Lab + Hugging Face） | `2022-09-22`，`cs.CL 2209.11055v1` |
| Paper page | https://huggingface.co/papers/2209.11055 | Hugging Face | 同上 |
| 官方 Repo | https://github.com/huggingface/setfit | Hugging Face | — |
| 官方 Docs 總覽 | https://huggingface.co/docs/setfit/index | Hugging Face | — |
| 概念指南 | https://huggingface.co/docs/setfit/main/en/conceptual_guides/setfit | Hugging Face | — |
| Quickstart | https://huggingface.co/docs/setfit/main/quickstart | Hugging Face | — |
| Blog 介紹 | https://huggingface.co/blog/setfit | Hugging Face, Lewis Tunstall 等 | `2022-09-26` |
| HF 模型/資料集 | https://huggingface.co/setfit | Hugging Face | — |

### 設計（論文 §3.1）

**兩階段**：(1) 以對比 Siamese 方式在少量標註的句對上微調 ST（正樣本同類隨機配對 `(xi,xj,1)`、負樣本跨類 `(xi,xj,0)`，每類生成 `R=20` 正/負，共 `|T|=2R|C|`；8 正 + 8 負可擴成 28 正 + 64 負 = 92 對，成指數擴張），(2) 以微調後 ST 編碼原標註生成句向量，訓練分類頭（全程預設 `logistic regression`，`T_CH = {(Emb(xi), yi)}`，`|T_CH|=|D|`）。

**標註量與成效**：論文以 `8 labeled examples per class` 在 Customer Reviews 上匹敵 `RoBERTa Large` 全量 3k；RAFT 基準每任務 50 訓練樣本；作者建議 few-shot 典型 `8–16 per class` 即可獲顯著提升，但**隨更多資料成效遞增**（`more data, not more training`；`sample_dataset` 示範 8 per class，鼓勵增至 16–32 觀察 90%+）。底座三檔：`SETFITROBERTA 355M` / `SETFITMPNET 110M` / `SETFITMINILM 15M`。

**效率**：`SETFITMPNET` 訓練 8 例約 30 秒於 `p3.2xlarge`（$0.025/split），推論與訓練較 `T-FEW 3B` 快一個數量級，儲存 70–420MB vs 11.4GB；支援多語（換 ST 底座即可）、可跑 CPU。

```python
from setfit import SetFitModel, SetFitTrainer
from sentence_transformers.losses import CosineSimilarityLoss
model = SetFitModel.from_pretrained("sentence-transformers/paraphrase-mpnet-base-v2")
trainer = SetFitTrainer(model=model, train_dataset=train_ds, eval_dataset=test_ds,
                        loss_class=CosineSimilarityLoss, batch_size=16,
                        num_iterations=20, num_epochs=1)
trainer.train(); trainer.evaluate()
```

### 適合 / 不適合本專案

- **適合**：few-shot 冷啟動利器；當前語料僅 84 筆（每類 12）若擴至 30–50 per class，可望以 ` few-shot → 傳統 finetune` 的漸進路徑提升 `MIXED` 等難類，無需 prompt engineering。
- **不適合（現階段）**：
  - 現有 84 筆尚少，且 `MIXED/CORRECTION/SUBJECT_CHANGE` 等多意圖/指代邊界需**高品質人工標註**與去識別；論文亦警示過擬合風險（對比擴張非無限）。
  - `SetFit` 為**有監督分類器**，一旦接管即具「寫入」語意風險，與本專案「分類器不得直接寫入醫療資料」的紅線衝突；需額外包裝為「僅分流、不寫入」的純路由層。
  - 決策為**結論前置**：作者提示**留待累積足夠人工標註語料後再評估**（同 `conversation_intelligence_challenges` §3），短期先以 shadow 語意路由驗證分流價值，避免 premature supervised。

---

## 5. RouteLLM / 同類 model routing — threshold calibration 與 holdout 評估

### 來源清單

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| 論文 | https://arxiv.org/pdf/2406.18665 · https://arxiv.org/html/2406.18665v4 | Isaac Ong, Amjad Almahairi, Vin-Cent Wu, Wei-Lin Chiang, Tianhao Wu, Joseph E. Gonzalez, et al. | `2024-06`，`arXiv:2406.18665` |
| ICLR 2025 版 | https://proceedings.iclr.cc/paper_files/paper/2025/file/5503a7c69d48a2f86fc00b3dc09de686-Paper-Conference.pdf | 同上 | ICLR 2025 |
| 原始 Repo | https://github.com/lm-sys/RouteLLM | LMSYS | Created `2024-06-03` |
| Blog | http://lmsys.org/blog/2024-07-01-routellm/ | LMSYS | `2024-07-01` |
| 閾值校準程式 | https://github.com/lm-sys/RouteLLM/blob/main/routellm/calibrate_threshold.py | LMSYS | `main` |

### 設計

**問題**：在 `M_strong`（GPT-4 類，高品質高成本）與 `M_weak`（Mixtral-8x7B 類，低成本）間做二元路由。學習 `Pθ(win_s | q)`（strong 勝 weak 的機率），以 `preference data D_pref = {(q, ls,w)}` 極大似然訓練；以**成本閾值** `α ∈ [0,1]` 作決策：`Rα(q)= M_weak if P(win_s|q)<α else M_strong`。`α` 控品質-成本 tradeoff（越高越省錢，越低越偏強模型）。

**校準**：**分位數校準**，對校準集（如 Chatbot Arena 65k 對話，80k 中保留 5k validation）計算 `win_rate = P(win_s|q)` 分布，取 `quantile(1 - strong_pct)` 得閾值。

```bash
python -m routellm.calibrate_threshold --routers mf --strong-model-pct 0.5 --config config.example.yaml
# For 50.0% strong model calls for mf, threshold = 0.11593  (論文/Repo 示範)
# 另一示範：0.1881 對應 50% 於不同校準集
```

> 關鍵提醒（Repo 原文）：*Because we calibrate based on an existing dataset, the % of calls routed to each model will differ based on actual queries received. Therefore, we recommend calibrating on a dataset that closely resembles your types of queries.* — 不可在 Arena 上校好直接套用醫療語料，必須**以本專案去識別語料重校**。

**評估**：三基準 `MMLU (14,042×57)` / `MT Bench (160, LLM-as-a-judge)` / `GSM8K (1k+)`；以 held-out 5k 與去汙檢查報告未汙染結果。路由器選型以 validation 集 model selection。以兩指標刻畫 tradeoff：

- `Performance Gap Recovered (PGR) = (r(M_Rα) - r(Mw)) / (r(Ms) - r(Mw))`
- `Average PGR (APGR) = ∫ PGR d c(M_Rα)`，實務離散 `{ci}∈[10]` 加權；面積越大越好。
- `Call-Performance Threshold CPT(x%)`：達成 `PGR=x%` 所需最少 strong 呼叫比例；論文示範 `CPT(50%)≈37%`（即 37% GPT-4 即可追回 50% 差距）。

論文示範路由器（`text-embedding-3-small` + 全參數 `BERT`/`Causal LM`，或 `matrix factorization`）在 `MMLU/MT Bench` 可**降成本 2×+ 而品質近 GPT-4 95%**，且換對 `Claude 3 Opus/Sonnet` 或 `Llama 3.1 70B/8B` 免重訓仍泛化。

### 適合 / 不適合本專案

- **適合（必取）**：
  - **閾值校準心法**：分位數校準 + `strong_pct` 量化成本預算，正可作為本專案「fallback 77% → 逐步放寬至 50%」的校準腳本；`routellm/calibrate_threshold.py` 的 `quantile` 邏輯可直接移植為 `scripts/calibrate_router_threshold.py`。
  - **Holdout 與去汙**：5k validation + similarity `0.95` 去汙 + 人審，正可規範本專案「held-out 人審集、不可用訓練集自嗨」。
  - **多指標**：`PGR/APGR/CPT` 的「成本-品質曲線」比單點 accuracy 更適合 Demo 延遲-準確率的技術報告。
- **不適合**：論文聚焦跨模型成本路由，非本專案「同模型內 fast path vs 複雜 interpreter」的語意分流；其 `Pθ(win_s|q)` 需偏好標註，短期無此標註則無法直接套用。應取「校準/評估方法論」，而非直接搬運模型。

---

## 6. 醫療聊天機器人高風險路由原則 — 安全規則優先、低信心升級、分類器不得寫入

### 來源清單

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| FDA CDS 最終指引（520(o)(1)(E) 四要件） | https://www.fda.gov/media/109618/download | FDA | Final Guidance `2022-09`；裝置/非裝置判定 |
| FDA AI-enabled Device：生命週期與送審建議（Draft） | https://www.hhs.gov/guidance/sites/default/files/hhs-guidance-documents/FDA/guidance-ai-enabled-device-software-functions.pdf | FDA | Draft `2025-01-06` |
| FDA SaMD 概念 | https://www.fda.gov/medical-devices/software-medical-device-samd/artificial-intelligence-software-medical-device | FDA | `2025-03-25` |
| CDS 解讀（2026 更新整理） | https://meddeviceguide.com/blog/fda-clinical-decision-support-cds-guide | MedDeviceGuide（彙整 FDA） | `2026-04-17` |
| 綜述（FDA 監管流程） | https://pmc.ncbi.nlm.nih.gov/articles/PMC12264609/ | PMC（彙整 FDA/IMDRF） | — |
| FDA 跨中心 AI 醫療產品論文 | https://content.govdelivery.com/attachments/USFDA/2024/03/15/file_attachments/2815628/omp_aimedicalproducts_final_240313%20%281%29.pdf | FDA CBER/CDER/CDRH/OCP | `2024-03-15` |
| WHO 倫理與治理（健康照護 AI） | https://iris.who.int/server/api/core/bitstreams/4f2c477c-4b72-4ca1-9a78-a1e73af64e50/content | WHO | — |
| WHO 大型多模態模型治理 | https://www.who.int/publications/i/item/9789240084759 | WHO | 出版 `2024-01-18` guidance |
| WHO 監管考量（AI for Health） | https://iris.who.int/server/api/core/bitstreams/ad62580f-540f-4e36-b957-e7f2946ae1fb/content | WHO | — |
| WHO 呼籲安全倫理 AI | https://www.who.int/news/item/16-05-2023-who-calls-for-safe-and-ethical-ai-for-health | WHO | `2023-05-16` |
| WHO 發布 LMM 倫理指引 | https://www.who.int/news/item/18-01-2024-who-releases-ai-ethics-and-governance-guidance-for-large-multi-modal-models | WHO | `2024-01-18` |
| IMDRF SaMD 風險框架 | https://www.imdrf.org/documents/software-medical-device-samd-clinical-evaluation | IMDRF | — |
| FG-AI4H 最終報告 | https://www.itu.int/dms_pub/itu-t/opb/fg/T-FG-AI4H-2025-1-PDF-E.pdf | ITU/WHO FG AI4H | `2018-2023` 活動總結 |
| Hastings Bioethics Briefing | https://www.thehastingscenter.org/briefingbook/ai-in-healthcare/ | The Hastings Center，Athmeya Jayaram & Kellie Owens | `2026-03-25` |
| FDA 分析 | https://www.akingump.com/en/insights/alerts/fda-changes-direction-in-final-cds-guidance | Akin Gump | — |

### 原則萃取（可執行化）

- **安全規則優先（援引 FDA 四要件 + IMDRF 風險框架）**：
  - `520(o)(1)(E)` 非裝置 CDS 四要件：(1) 非採集/處理影像、IVD、生理訊號；(2) 僅顯示/分析醫療資訊；(3) 支援而非取代 HCP 判斷（`*enhance, inform, influence* 不等於 *replace or direct*`，不提供單一確定診斷或治療指令，不用於 time-critical，且以多選項+資訊為主）；(4) 使 HCP 能**獨立審視建議依據**而非主要依賴軟體（需以 plain language 揭露目的、對象、輸入、演算法方法、資料來源、驗證結果、患者特異資訊與未知數）。只要任一不滿足，即為 Device，需 `510(k)/De Novo/PMA` 與 `QMSR 21 CFR 820`。
  - **2026 更新**對單一建議的立場軟化：若臨床上僅一選項合理，FDA 行使 enforcement discretion 不視為直接取代判斷；但**風險分數/確定診斷/處置指令**仍屬 Device。
  - **IMDRF 風險框架**：以「資訊對決策的**重要性**（treat/diagnose vs drive vs inform）」×「患者狀況（critical/serious/non-serious）」分四級，決定審視強度與獨立複核義務。
  - 落地為本專案的 **`紅旗 → 授權 → 產品命令 → 高精度單值 fast path → AI 理解`** 前置鏈（見 `conversation_intelligence_challenges_20260829.md` §2.2），**任何路由不得繞過**，Semantic Router 僅在之後可選。

- **低信心升級（援引 FDA 生命週期 + WHO 治理）**：
  - FDA `AI-enabled Device` Draft 要點：風險管理文件（`14971` + `CR34971` AI 擴充）、資料管理透明度、偏見控制、TPLC 監控與 `PCCP`（`SaMD Pre-Spec + ACP` 預定義變更控制）。
  - WHO 六原則（`protect autonomy / promote well-being & safety / transparency & explainability / responsibility & accountability / inclusiveness & equity / responsive & sustainable`）與 LMM 指引：**自動化偏見（automation bias）**與技能退化風險，要求**獨立複核、來源揭露、信心量化、個體化資訊**；健康聊天機器人應標示 AI 身分、保留人類監督點、提供申訴/追溯；政府應指定監管機關、強制上市後第三方稽核與影響評估（LMM 指引 40+ 建議）。
  - 落地：本專案 **低信心一律升級**（`UNKNOWN` → LLM interpreter，`MIXED` 在 false-fast=0 點 recall 0 故全升級）、保留 `PendingAction + provenance + confidence + source_quote + 第三人/問句/假設/否定` 防護、結果附 `資料來源` 且可追 `source_quote`。

- **分類器不得直接寫入醫療資料（援引 FHIR/存儲邊界 + WHO 責任歸屬）**：
  - FDA 要求「能獨立審視依據」才算可解釋 CDS；否則視為 Device 且需臨床驗證。
  - Hastings：消費者聊天機器人繞過醫師媒介時，責任鏈模糊；若產生不當安心感致延誤就醫，須有**高風險症狀人審**與**免責與轉醫**路徑；未來審查應納入關係/信任影響。
  - 落地：本專案**狀態機才可寫入**：`IntakeCandidate` 僅候選，寫入由 `Orchestrator/PendingAction + PreVisitIntakeTool.canonicalization + 人親自 `確認`` 決定；Semantic Router/解釋器皆**不得直接 `repository.save()`**；分享採 `CreateShareRequest/RedeemShareRequest` 與 `VIEW_GRANTED_CLINICAL_SUMMARY` 授權，醫護唯讀。

---

## 7. 線上聊天系統對 perceived latency 的做法 — acknowledgement / async response / streaming / deadline

### 來源清單

| 來源 | URL | 發布者 | 日期/版本 |
|---|---|---|---|
| LINE Webhook 官方（接收訊息） | https://developers.line.biz/en/docs/messaging-api/receiving-messages/ | LINE Developers | 官方 Docs |
| LINE 訊息發送 | https://developers.line.biz/en/docs/messaging-api/sending-messages/ | LINE Developers | 官方 Docs |
| LINE Webhook Event Surface（AsyncAPI） | https://apis.io/asyncapis/line/line-messaging-webhook/ | APIs.io（引 LINE OpenAPI） | — |
| LINE Bot SDK Python async_api | https://github.com/line/line-bot-sdk-python/blob/master/linebot/async_api.py | LINE | `master` |
| 具彈性 LINE Bot（webhook 安全與效能） | https://news.lavx.hu/article/building-resilient-line-bots-webhook-security-performance-and-operational-best-practices | LavX News（彙整 LINE 官方指南） | `2026-01-11` |
| paperclip-plugin-line 實作（Fast-ACK 範式） | https://github.com/NahuelGenchi/paperclip-plugin-line | Nahuel Genchi / Paperclip | — |
| Messaging API Reference | https://developers.line.biz/en/reference/messaging-api/ | LINE Developers | — |
| Streaming：SSE/WebSocket 架構抉擇 | https://www.channel.tel/blog/streaming-ai-responses-sse-websockets-real-time | Chanl AI | `2026-03-08` |
| Optimistic UI for Buffered Streams | https://pressbot.io/optimistic-ui-for-buffered-chatbot-streams/ | PressBot | `2026-02-15` |
| Streaming Typed Events 模式 | https://github.com/agentpatternscatalog/patterns/blob/main/patterns/streaming-typed-events.md | Agent Patterns Catalog | — |
| Typing Indicators 體驗研究 | https://callsphere.ai/blog/typing-indicators-streaming-chat-agents-feel-responsive.md · https://tianpan.co/blog/2026-04-20-latency-perception-gap-ai-interfaces · https://cloudsignal.io/blog/ai-chat-presence-and-typing/ | Callsphere / TianPan / CloudSignal | `2026-04-20` |
| Streaming 切首字延遲（TTFT） | https://barkingiguana.com/writing/streaming-responses-to-cut-first-token-latency/ | Barking Iguana | `2026-07-13` |
| GetStream AI 訊息串流 | https://getstream.io/chat/docs/javascript/ai-message-streaming.md | Stream | — |

### 官方與業界作法

**Acknowledgement（立即回 2xx，異步處理）**：LINE 官方明文*We recommend processing events asynchronously*；LavX 彙整官方指南要求 **1 秒內回 HTTP 200**（否則 timeout，連續失敗將被平台暫停/退訂），流程為*驗證簽章 → 立即 200 → 入隊異步 → 執事業務 → Messaging API 回覆*；支援 `webhookEventId` 去重、`deliveryContext.isRedelivery` 識別重送、`X-Line-Signature` HMAC-SHA256 驗證。`paperclip-plugin-line` 實作佐證：**Fast-ACK**（毫秒級 2xx）+ 佇列 + 冪等（`webhookEventId` 去重）+ `replyToken` 快取（60 秒有效，過期掃除）。

**Async Response（placeholder + push）**：LINE webhook 回 200 後，以 `replyToken`（一次性、短期有效）或 `pushMessage`（`to=userId`，可重試，需 `X-Line-Retry-Key`）送最終答覆。本專案既有 `ASYNC_PLACEHOLDER_REPLY = "幫你查衛教資料中，查到後立刻傳給你 📋"` 即為此範式；`push_message` 支援 `retry_key` 與 `notificationDisabled`、`customAggregationUnits`。

**Streaming（降低 TTFT）**：業界以 `SSE`（單向，`fetch + response.body.getReader()` 分段，或 `EventSource`）或 `WebSocket`（雙向）推送 `text_delta/card/tool_start/done/error` 型別化事件。關鍵指標 **`TTFT`（Time to First Token）**，`p95 <500ms` 感覺靈敏、`<200ms` 瞬間、`>1.5s` 遲滯；**`TPOT`（Time per Output Token）**需穩定，否則突發到達感官更差。實測流式 `TTFT 800ms + TPOT 140 tok/s` 的感知等待僅 800ms，同內容非流式 4.2s 棄置率高 11%→2.3%（Barking Iguana 案例；總時長流式略慢但感知勝）。`SSE` 為 90% 聊天場景首選，`WebSocket` 留給語音中斷/雙向。

**Deadline（真正逾時，而非假超時）**：LINE Bot 需區分「邏輯 timeout」與「使用者等待上限」。業界反模式：`ThreadPoolExecutor`  `with future.result(timeout=…)` 在 `with` 退出時 `shutdown(wait=True)` 仍等待網路完成（本專案 `conversation_intelligence_challenges` §2.8 已揭露）。正確為 HTTP/SDK 原生 timeout + 可傳遞 `DeadlineGuard` + 超時工作不再寫 `session` 或 `push`（`deadline_scope`/`run_with_deadline` 封口），`SYNC 45s / ASYNC 120s` 僅作硬上界，不算修復。

**其他感知技巧**：Optimistic UI（先顯示使用者訊息、按鈕變 loading）、Typing indicator（`typing_start/stop`）與骨架屏、進度分段（`AI_STATE_THINKING/GENERATING/EXTERNAL_SOURCES/ERROR`）、`ephemeral_message_update`（生前顯示）+ `update_message_partial`（落盤）分離、節流 5–20 updates/s、`requestAnimationFrame` 合批、`sigmoid_distance` 對閾值距離做 confidence 視覺化。

### 適合 / 不適合本專案

- **適合（已部分落地、可深化）**：本專案 `ConversationOrchestrator._spawn_async_formal` 已實作 `DeadlineGuard` + `Semaphore(5)` 有界併發 + `push idempotency` + `placeholder` + `webhookEventId` 去重，符合 LINE 1 秒 ACK 規範；後續可補 `typing indicator`（`showLoadingAnimation`/`AI_STATE_THINKING`）與 `SSE` 的 `text_delta`（對應 `stream_workflow` 的 `buffered_stream_after_d`，現為 buffered-then-stream，需改為真正的 token streaming 才有 TTFT 增益）。
- **不適合照搬**：`WebSocket` 雙向與 `Redis` 語意 cache 對本專案初期過重；`nginx`/`ALB` 緩衝需關閉才有真 streaming（`X-Accel-Buffering: no` 等），地端部署需額外驗證。

---

## 8. 業界常見架構綜合（可直接放入技術報告）

| 維度 | 業界共識 | 本專案落地建議 |
|---|---|---|
| **Routing** | `Encoder → Index → Router` 外包為 `Signal → Policy → Algorithm` | Tier-1 用 Aurelio 輕量 `HybridRouter(alpha 0.3–0.5)` 做毫秒級初篩；Tier-2 用 vLLM 的 `decision priority + AND/OR/NOT` 做可審核政策 |
| **Threshold** | 靜態（0.5/0.75）+ 動態 `fit()` 個別校準，無萬用值 | 每子域獨立閾值，初值 `dense:0.62–0.75 / cache:0.85`，上線後 `evaluate + fit` 迭代；以分位數校準對齊 `strong_pct` 預算 |
| **Encoder** | Dense cosine + Sparse dot 雙軌，BGE-M3 多語長文當前最優 | 唯一指定 `BAAI/bge-m3`（1024-d，8192，dense+sparse+ColBERT 三合一）；短期 `all-MiniLM` 作基線 |
| **Fallback** | 無匹配→空回傳（`None/UNKNOWN`）+ 預設 LLM + 語意/精確 cache | 明確 `None → Tier-2 LLM (Formal interpreter)` + 語意 cache `0.85` + `最少保留1條` 避免空轉 |
| **Signal** | 啟發式（keyword/context/language）+ 學習式（embedding/domain/complexity/pii）分離，多訊號投票 | 複用 `clinical_intent 0.72 + complexity hard + many_questions + long_context` 即可，無需全量 15+ signals |
| **Policy** | `priority + AND/OR/NOT` 規則樹 + `projections` 聚合 | 硬約束（紅旗/授權/產品命令）高優先，成本/延遲低優先，保持可讀 |
| **Confidence** | `hybrid (logprob 0.65 + margin 0.35) threshold 0.72 small→large` | 作為 Tier-1→Tier-2 升級金標準，保留 `cost_quality_tradeoff 0.45` 可調 |
| **Production** | Async ACK 1s + Placeholder + Push + Streaming SSE + DeadlineGuard + Semantic Cache | 本專案已有 `DeadlineGuard`/`Semaphore(5)`/`webhookEventId` 去重/`ASYNC_PLACEHOLDER_REPLY`，補 `typing + SSE text_delta` 即達標 |

---

## 9. Semantic Router vs SetFit vs 規則式路由 — 比較與選型

| 構面 | Aurelio Semantic Router（含 Hybrid） | SetFit（Hugging Face） | 規則式（Regex/關鍵字） |
|---|---|---|---|
| **本質** | 訓練-free / 少樣本 exemplar 餘弦分流，無監督分類器再校閾值 | 有監督兩階段對比微調 ST + logistic 分類頭 | 封閉模式匹配，無語意泛化 |
| **標註需求** | 每路由 5–12 句 exemplar 即可啟動，可人審增量 `fit()`；本專案 84 筆已可跑 sweep | 典型 8–16 per class 起步，RAFT 50 per task；論文 `8 per class ≈ RoBERTa Large 3k`；越多越好 | 無需標註，需持續手寫規則（含口語變體、否定、問句等） |
| **準確率**（**僅引實測/論文，不捏造**） | 本專案 84 筆 bge-m3：`cos=0.90` macro 10%/micro 17.9%/MIXED 0%/coverage 3.6%/fallback 96.4%/false-fast 0；`margin=0.13` 25.6/31/0/16.7/83.3/0；**`hybrid cos=0.62 margin=0.10` 34.4/36.9/0/22.6/77.4/0**（推薦點）；放寬至 MIXED 50% recall 時 false-fast 25–29 | 論文：8 per class 在 CR 匹敵全量；SETFITMPNET 快 T-FEW 3B 一個量級；本專案尚未以 SetFit 評估 | 早期 `口渴/頻尿` 等泛化不足（見 `conversation_intelligence_challenges` §2.1）；但 `紅旗/授權/產品命令` 恰需規則的可控性 |
| **延遲**（實測） | 本專案 bge-m3 本地 Ollama：`cold 170ms / warm p50 161.2ms p95 172.3ms`（25 warm rounds），目標 `warm p95 <150ms` 尚未達標；Aurelio 本地 `HuggingFaceEncoder` 宣稱可 <10ms 級（需自測），Hybrid 略增 | 論文：訓練 30s / 推論較 T-FEW 快 10×；本專案未測實際推論延遲 | `ms` 級，deterministic，永不觸發 LLM |
| **錯誤代價** | `false-fast` 可壓至 0（靠 `UNKNOWN` 高 fallback），但 MIXED 等難類 recall 歸零；需外加校準與人審 | 一旦接管即具寫入語意風險（分類器直出即被視為決策），需額外「僅分流不寫入」封裝 | 無語意泛化，漏召率高；但 `部分命中` 阻斷（§2.3）已修復為「規則與 Formal candidates 合併」 |
| **可審核/安全** | `None` 明確 abstain，可外接政策層與 cache；不直接寫入 | 需自行保證低信心升級與來源封裝，否則與「分類器不得寫入」紅線衝突 | 最可控、最可審核，但無法涵蓋長尾口語 |
| **維運** | 需維護 `utterances/prototypes`、定期 `fit/evaluate`、監控分布漂移 | 需標註管線、訓練/版控、漂移與偏差監控 | 規則累積易治標不治本，迴歸面大 |

> 結論：**規則守底（安全/授權/產品）→ Semantic Router 作 shadow 分流（低成本、毫秒級、可回退）→ SetFit 待語料成熟後再評估有監督**，符合論文章節與專案既有決策（`conversation_intelligence_challenges` §3 已定此分層）。

---

## 10. 安全風險（醫療聊天機器人專屬）

1. **Automation bias 與過度依賴**：WHO 點名的 `automation bias / skill degradation` 會使第一線忽略錯誤；緩解：強制 `獨立審視依據`（FDA 四要件）、每條 `IntakeCandidate` 需 `source_quote + confidence + provenance + requires_confirmation`，最終寫入必經 `人親自「確認」`；Semantic Router 僅分流，不具寫入權。
2. **分類器直寫/隱式寫入**：若讓 `MIXED/PURE_INTAKE` 置信度中等即寫 `symptom_description/medications`，將繞過 `PendingAction`/`canonicalization`/`否定/問句/第三人` 防護；緩解：**任何分類器輸出僅作路由**，寫入仍走 `Orchestrator._sync_clinical_context` + `intake.tool.extract_fields_from_utterance` + `candidate_merge` 的欄位級驗證（限定 `known_medications/allergies/chronic_conditions/family_history/symptom_*` 封閉域，去重與子句級否定）。
3. **單一建議/風險分數越線**：FDA 視「確定診斷、治療指令、風險分數」為 Device；本專案緩解：C/D gate 保留，`CGenerator` 僅在 `B PASS` 且 `rag_result` 具足夠證據時產答，且附 `資料來源` 與免責聲明；高風險問句走 `HONEST_FALLBACK_PUSH_TEXT`（誠實回退）而非幻覺。
4. **資料外洩與 PII**：84 筆評估集已 PII-free；Router 層不得索引可識別資訊；`BGE-M3` 本地 Ollama 免雲端傳輸，符合 `proposed data minimization`。
5. **閾值漂移與選擇偏誤**：若僅在 84 筆小集上校準並強制 top-1，會把 `MIXED/CORRECTION` 錯分至 `PURE_INTAKE`（本專案 diagnostic 已見 12 例錯分）；緩解：要求 `held-out 人審集 + false-fast 預算 + MIXED recall ≥75%` 雙門檻，`UNKNOWN → LLM`，`shadow logging` 先行。

---

## 11. 既有實驗為何未上線（技術原因，引用原始數據）

### 11.1 實驗事實（branch `semantic-router-eval`，commit `747c5f1`，基於 `41f4725`）

- **隔離性**：8 檔案 1213 行**純新增**（`docs/reviews/semantic_router_eval_20260829.md` `121` + `experiments/semantic_router_eval/{README.md, __init__.py, dataset.json:104, evaluate.py:607, router.py:257, tests/test_harness.py:82}`），**未動** `line_bot/app.py` / `tfda_context_gate/line_orchestration/orchestrator.py` / `tfda_context_gate/conversation/interpreter.py` / `tfda_context_gate/workflow/*` / `scripts/p2a_live_smoke.py`，故可安全 shadow 而不汙染正式路徑。
- **Encoder**：復用專案既有 `tfda_context_gate.rag.tfda_retriever.DEFAULT_EMBEDDING_MODEL = "ollama/bge-m3:latest"`，`configured_embedding()` 優先 `OLLAMA_EMBED_MODEL → EMBED_MODEL → DEFAULT`，`embedding_config_source()` 留存 `model_source`；探活 `GET http://localhost:11434/api/tags`，失敗則 `BLOCKED` 僅跑 `DeterministicFakeEmbedder(64-dim sha256 bigram)` 驗 plumbing，不下載模型、不替代生產。
- **語料**：`experiments/semantic_router_eval/dataset.json`（`version: semantic-router-eval.v1`，`primary:84` 7 類×12 均衡 + `boundary_comparison:6`），`source=existing_fixture:13 + unseen_variant:71`，PII 由 `test_dataset_has_no_obvious_pii_tokens` 掃 email/09 手機/patient id；ID `edu-01~12, intake-01~12, mixed-01~12, correction-01~12, subject-01~12, chitchat-01~12, unknown-01~12` + `boundary-red-01/02, boundary-auth-01/02, boundary-product-01/02`。
- **Routing**：`PrototypeSemanticRouter` 每類 4 prototypes → L2 歸一化 → `score = max cosine`；`label_from_scores(policy=cosine/margin/hybrid)` 三型：`cosine (top_score≥cos_th)` / `margin (top−second≥margin_th)` / `hybrid (兩者皆滿足)`；`matched_labels` 暴露多標籤；`sweep` 網格 `cos 0.50–0.95/0.01, margin 0.00–0.40/0.01, hybrid 0.50–0.95/0.02 × 0.00–0.30/0.02`；`select_recommended` 四階優先 `MIXED recall≥75% & false-fast=0 → MIXED≥75% → false-fast=0 → max macro_F1`，排序鍵 `(macro_F1, mixed_recall, −false_fast, coverage)`。
- **Fallback**：`UNKNOWN` = abstain → 交回既有 LLM interpreter；`boundary_guard()` 關鍵字（胸痛+喘→RED_FLAG / 另一位使用者/朋友→AUTHORIZATION / 刪除/重設登入密碼→PRODUCT_COMMAND）在 `evaluate_boundary()` 前置跑，不計入語意指標。
- **延遲**（`evaluate.benchmark_embedding`，25 warm rounds，`OLLAMA_BASE_URL=http://localhost:11434`）：`cold 170.0/170.0 (1) / warm p50 161.2 p95 172.3`。**目標 `warm p95 <150ms` 未達標**（超出約 22ms，含 Ollama 模型載入外部性）。
- **未上線的量化原因**：

| policy | 推薦閾值 | macro F1 | micro F1 | MIXED recall | coverage | fallback | false-fast |
|---|---:|---:|---:|---:|---:|---:|---:|
| cosine | 0.90 | 10.0% | 17.9% | 0.0% | 3.6% | 96.4% | **0** |
| margin | 0.13 | 25.6% | 31.0% | 0.0% | 16.7% | 83.3% | **0** |
| **hybrid（chosen）** | **cos=0.62, margin=0.10** | **34.4%** | **36.9%** | **0.0%** | **22.6%** | **77.4%** | **0** |

放寬至 MIXED 50% recall 的 diagnostic（非安全建議）：`cos 0.57: macro 61.6% coverage 92.9% false-fast 25`；`margin 0.00: 60.0% 100% 29`；`hybrid 0.50/0.00: 60.8% 97.6% 27`。**任何 false-fast=0 的安全點 MIXED recall 皆 0%**，證實 bge 原型在此小語料上**無法安全分流混合意圖**；若放寬則 25–29 例 false-fast（`mixed→PURE_INTAKE/subject→PURE_INTAKE` 等）不可接受。

### 11.2 未上線的定性原因

1. **安全預算不通過**：即便推薦點 `false-fast=0`，`MIXED` 與多數 `PURE_EDUCATION` 皆被壓為 `UNKNOWN`（如 `edu-01` top `0.8843` margin `0.0188` 仍 UNKNOWN），覆蓋僅 22.6%，效益不足以改路由；放寬則產生「水果衛教被當 intake 寫入」等污染，違背 `challenges §2.4/2.6`。
2. **資料規模與多標籤本質**：84 筆中 7 類各 12，MIXED 需人審子句切分（`口渴 + 水果`）非單純分類；原型法在少樣本下邊界模糊，`margin 0.00` 才能 50% recall，顯示**語意空間可分性不足**，非閾值可救。
3. **與分層設計衝突**：`challenges §3` 定義語意路由「只判斷 `PURE_INTAKE/PURE_EDUCATION/MIXED/CORRECTION/SUBJECT_CHANGE/CHITCHAT/UNKNOWN`，不直接寫入」，本原型符合此定位，但須**先有 deterministic safety/product gates** 與 `shadow logging`，不可直接接線。
4. **分支落後**：`semantic-router-eval` 自 `41f4725` 後未跟進 `main` 的 `c64bcf7→91f403b` 間 20  commits（`P2A.1 data quality/latency/line reliability closure/engine demo/readiness`），diff 刪 9486 增 1903，演示/整合代碼缺失，無 CI 引用。

**評估原文結論**（`semantic_router_eval_20260829.md` Recommendation）：*Do not wire this prototype into production routing yet. It is suitable for a P2A.2 shadow-only trial only if every shadow decision is logged, existing red-flag/authorization/product gates run first, and UNKNOWN always falls through to the current LLM interpreter. Promote to shadow when MIXED recall and false-fast meet safety budget on held-out human-reviewed set.*

---

## 12. 可重用元件清單（路徑與函式名，禁止重做）

### 可直接重用（禁止重寫，僅引用/搬遷）

| 路徑 | 關鍵符號 | 重用方式 |
|---|---|---|
| `experiments/semantic_router_eval/router.py` | `ROUTE_LABELS`, `PROTOTYPES(6×4, UNKNOWN=空)`, `Embedder(Protocol)`, `configured_embedding()`, `embedding_config_source()`, `ollama_model_available()`, `OllamaEmbedder`, `DeterministicFakeEmbedder(64-dim, sha256 bigram)`, `Prediction(label/top_score/second_score/margin/scores/matched_labels)`, `PrototypeSemanticRouter.score/predict/_normalize` | 作為 shadow router 的**餘弦+abstention** 框架與 `matched_labels` 多標籤暴露；僅替換 `PROTOTYPES` 為更精的錨點句 |
| `experiments/semantic_router_eval/evaluate.py` | `load_dataset()`, `benchmark_embedding(warm_rounds=25)`, `score_rows()`, `label_from_scores(policy)`, `metrics_for()`, `threshold_sweep()`, `select_recommended(4-tier)`, `top_confusions()`, `boundary_guard()`, `evaluate_boundary()`, `build_embedder()`, `run_evaluation()`, `render_markdown()`, `main(--output,--json-output)` | 全套**評估管線**（含 `BLOCKED` 探活、latency 分桶、false-fast/mixed_recall/coverage、推薦/診斷雙表、confusion/boundary）；直接用 `python3 -m experiments.semantic_router_eval.evaluate --output docs/reviews/semantic_router_eval_20260829.md --json-output /tmp/semantic_router_eval_20260829.json` 重跑 |
| `experiments/semantic_router_eval/dataset.json` | `{version, description, primary:[84]{id,label,source,text}, boundary_comparison:[6]}` | **84+6 結構與校驗**（`Counter==ROUTE_LABELS` + PII 掃描）作增量標註基線；`existing_fixture` 13 + `unseen_variant` 71 的分層可保留用於泛化測試 |
| `experiments/semantic_router_eval/tests/test_harness.py` | `test_dataset_is_balanced`, `test_dataset_has_no_obvious_pii_tokens`, `test_fake_embedder_is_repeatable`, `test_router_emits_unknown`, `test_router_has_multilabel_score_surface`, `test_boundary_guard_precedes`, `test_metrics_count_false_fast` | **Harness 7 項** 作為新路由的最小門檻（不含生成 LLM） |
| `experiments/semantic_router_eval/README.md` | 運行說明 | 直接引用為操作手冊 |
| `docs/reviews/semantic_router_eval_20260829.md` | 機器生成報告（含 `backend/model_source/host/阈值雙表/per-class/chosen vs diagnostic/confusion/boundary/reproduce`） | 作為**可行性證據**，後續增書 `docs/reviews/semantic_router_shadow_*.md` 延續 |
| `tfda_context_gate/rag/tfda_retriever.py:19,243-248` | `DEFAULT_EMBEDDING_MODEL = "ollama/bge-m3:latest"` | **唯一模型真相來源**，`router.configured_embedding()` 已復用，消除模型分叉 |

### 需重設計後再用（不可照搬）

| 物件 | 問題 | 重設計方向 |
|---|---|---|
| `PROTOTYPES` 錨點句 | 過通用，與 `intake.tool.extract_fields_from_utterance` 的藥品細類/`candidate_merge` 的子句級否定脫節 | 擴充並去重，與 `intake/candidate_merge.py: is_multi_clause/is_question_like/canonicalize` 對齊；補 `metformin/insulin glargine/degludec/lispro` 不合併等邊界 |
| Threshold | 僅 84 筆 feasibility，非臨床驗證 | 以去識別真實語料重校，引入 held-out 人審集 + `quantile(calibrate_threshold.py)` + `select_recommended` 四階門檻 |
| Latency `warm p95 172.3ms` | 略超目標 150ms，含 Ollama 外部性 | 改 `sentence-transformers` 直載本地 `BAAI/bge-m3`（省 HTTP 往返）或改 async shadow（不卡首字） |

---

## 13. 正式對話路徑呼叫鏈（LINE callback → orchestrator → ... → persistence）

> 基於 `main@91f403b` 全量 537 passed 實證，路徑以 `line_bot/app.py` 與 `tfda_context_gate/line_orchestration/orchestrator.py` 為準，未納入 `semantic-router-eval` 實驗碼。

```
LINE Platform
  └─ HTTPS POST /callback  (X-Line-Signature = base64(HMAC-SHA256(secret, body)), 1s 內需 200，支援 isRedelivery/webhookEventId 去重)
       ├─ line_bot/app.py: FastAPI app (startup: _preheat_vector_store 暖 RAG)
       │   ├─ verify_signature(body, signature, secret)  @line_bot/app.py:305
       │   ├─ _get_secret() / _get_access_token() (支援 LINE_CHANNEL_SECRET/LINE_CHANNEL_ACCESS_TOKEN/LINE_ACCESS_TOKEN/LINE_CHANNEL_TOKEN)
       │   └─ POST /callback handler（展開見下方）
       ├─ tfda_context_gate/line_orchestration/orchestrator.py: ConversationOrchestrator.handle_text()
       │   ├─ _is_text_duplicate / _mark_text_dedup (TEXT_DEDUP_TTL_S 120 / SHORT 10, 針對 「你好/謝謝/身份」短 TTL)
       │   ├─ _is_short_ttl_text / _dedup_ttl_for / _dedup_reply_for / _EMPATHY_DUP_RE / _normalize_text
       │   ├─ handle_text(event_id, line_user_id, text)
       │   │   ├─ 去重與短路：_is_text_duplicate → 直接回 _dedup_reply_for
       │   │   ├─ 身分與授權：principal_hash(line_user_id) → ProductSessionRepository.get / _load_or_create / _hash
       │   │   ├─ 狀態機分支（Orchestrator 類常量）：
       │   │   │   SELF_COMMANDS / PROXY_COMMANDS / PROXY_CONSENT_COMMANDS / CONFIRM/CANCEL/PAUSE/RESUME/START_INTAKE/SHARE/SUMMARY/MODIFY 等
       │   │   │   → 若為產品命令，直接走 deterministic product fast path（<200ms 目標），不進 interpreter/ workflow
       │   │   ├─ deterministic safety 前置（任何路由不得繞過）：
       │   │   │   _is_red_flag (tfda_context_gate/workflow/intake_router.py: _is_red_flag)
       │   │   │   → RuleBasedSignalExtractor.is_pre_visit_intake_text / is_chit_chat_text / is_identity_text / _is_empathy_text
       │   │   │   → 若觸發，直接 fallback/block，不進 AI
       │   │   ├─ _orch_should_use_formal(raw, task_type)  @line_orchestration/orchestrator.py:297
       │   │   │   規則：pre_visit_intake→False；紅旗/intake/chit-chat/identity/empathy→False；短句<4/能力詢問→False；
       │   │   │   否則以 RuleBasedSignalExtractor.extract + policy_gate(DEFAULT_POLICY) 僅 G_GENERAL_EDUCATION 允許 formal
       │   │   ├─ interpreter 選擇：ConversationInterpreterFactory.from_env() → FormalConversationInterpreter | DeterministicConversationInterpreter
       │   │   │   formal 模型來源：env_value("CONVERSATION_LLM_MODEL") fallback "ROUTER_LLM_MODEL"（.env 的 opencode/mimo-v2.5，OPENCODE_API_KEY 已 gitignore）
       │   │   ├─ FormalConversationInterpreter.interpret(envelope) @conversation/interpreter.py:561
       │   │   │   失敗→fallback DeterministicConversationInterpreter.interpret
       │   │   ├─ 若 _is_async_narrow_eligible(session, text) 且非 intake-active：
       │   │   │   → _spawn_async_formal(event_id, line_user_id, text, session_id, push_sender)
       │   │   │     立即回 ASYNC_PLACEHOLDER_REPLY（"查詢中，請稍候，資料整理完成後會推送給你 📋"），入 DeadlineGuard(self.async_formal_timeout_s=120) + _FORMAL_SEMAPHORE(5) 有界併發
       │   │   ├─ 否則同步：_call_workflow(session) → _run_formal_with_timeout 或 run_with_deadline（SYNC_FORMAL_TIMEOUT_S 45）
       │   │   └─ 產 OrchestratorResult(reply, status, session_id, event_id, replayed)
       │   └─ tfda_context_gate.workflow.runner.run_workflow()  @workflow/runner.py:184
       │       ├─ _is_formal_eligible(request_context, task_type)（fast path：pre_visit_intake/chit-chat/capability/vague Q_CLARIFICATION/非 G status→False）
       │       ├─ 若 eligible 且 use_formal→ formal 分支：_build_formal_retriever/_build_formal_generator（RuleBased extractor 為 None 以省 20s），否則 deterministic FixtureRetriever/DeterministicFixtureCGenerator
       │       └─ tfda_context_gate.workflow.graph.build_workflow_graph()  @workflow/graph.py:208  （LangGraph StateGraph）
       │           ├─ a_node @graph.py:213
       │           │   1. is_welcome_trigger → WELCOME_MESSAGE（langgraph 任務，非 LLM）
       │           │   2. _is_red_flag → BLOCKED/FALLBACK（fallback_response("A_EMERGENCY/A_URGENT_HUMAN")）
       │           │   3. _is_identity / _is_empathy / G2 whitelist (is_chit_chat_text) → BLOCKED（IDENTITY/EMPATHY/CHIT_CHAT_OUT_OF_SCOPE）
       │           │   4. route_request(extractor, prompt_injection_guard) → AResult
       │           │      支援 M→G 口語覆蓋：整句含 intake 語且 M_BLOCK 但 is_colloquial_medication→重判為 G_GENERAL_EDUCATION
       │           │   ───────────────────────────────────────────────────────────────
       │           ├─ a_route @graph.py:446 → a_route_target(task_type/intake/a_result) 決定是否進 intake branch
       │           ├─ intake branch（若 a_route 為 intake）：
       │           │   intake_check_node → intake_stage1/2/3 → review_confirm_node
       │           │   採 tfda_context_gate/intake/tool.py: PreVisitIntakeTool.extract_fields_from_utterance + PreVisitIntake
       │           │   階段：stage1(用藥/過敏/慢性/家族) → stage2(發病/描述/程度) → stage3(想問醫師) → review_confirmed
       │           │   口語藥品二階確認：is_colloquial_medication && confidence<0.7 → needs_med_clarify，2 次追問後 mark_medication_unknown
       │           ├─ 非 intake → query_expansion_node → rag_node → b_node → c_node → d_node（標準 G 流程）
       │           │   query_expansion: IdentityQueryExpander（或 formal expander）
       │           │   rag_node: ToolExecutor.execute(EvidenceRetrievalTool，source_id=TFDA_RISK/HPA_DIET_GUIDE) 或 Retriever.retrieve_with_guardrail
       │           │   b_node: ContextGate （DeterministicContextGate approval_mode="all_retrieved"）輸出 CanonicalBResult
       │           │   c_node: CGenerator（DeterministicFixtureCGenerator 或 Formal _build_formal_generator 產 grounded answer）
       │           │   d_node: run_output_gate + SemanticVerifier
       │           └─ _finish(trace, staged_recorder) → WorkflowResult(status, final_response, fallback_reason, trace, staged_latency, intake_snapshot)
       ├─ line_bot/app.py: _schedule_formal_push / _push_text / _mark_event_pushed / _maybe_record_question_for_doctor
       │   ├─ _push_text(line_user_id, text, event_id, deadline_guard)
       │   │   4000 截斷 + _request_timeout 殘餘秒數 + x_line_retry_key 去重 + _FORMAL_SEMAPHORE 二重嘗試
       │   └─ 若 HONEST_FALLBACK_TEXT，_maybe_record_question_for_doctor → PendingAction(type="PENDING_CONFIRM_QUESTION")
       ├─ tfda_context_gate/product_session/repository.py: SQLiteProductSessionRepository
       │   ├─ get / save(expected_version) / get_webhook_event / mark_webhook_event_pushed
       │   ├─ ProductSession (session_id, principal_id_hash, conversation_context, intake_snapshot:PreVisitIntake, pending_action, version)
       │   └─ WebhookEvent (event_id, line_user_id, status: ASYNC_PENDING/ASYNC_PLACEHOLDER/COMPLETED, result:{pushed, async_original_text, session_id})
       └─ line_bot/app.py: reply/push 送回 LINE
           _reply_text(reply_token, text, quick_actions) 或 MessagingApi.push_message(PushMessageRequest+TextMessage) / ApiClient
           監測：TraceRecorder + StagedLatencyRecorder（red_flag_and_auth_ms, conversation_interpreter_ms, candidate_validation_ms, rag_retrieval_ms, answer_generator_ms, b_gate_ms, d_gate_ms, persistence_ms, total_ms）
```

**關鍵不變量**：

1. **A→B→C→D with E trace** 固定；`deterministic safety (red_flag, auth, product command)` 永遠先於任何語意路由；`IntakeCandidate` 僅候選，寫入必經 `Orchestrator._sync_clinical_context` + `repository.save()`。
2. **圖片路徑**：`line_bot/app.py: handle_image_message / handle_front_back_images / _download_image_content (MessagingApiBlob.get_message_content) → _process_ocr_images (MedicationBagOCRService QR-first → PaddleOCR) → merge into intake_data (known_medications)`，**絕不將 raw image 存入 WorkflowState**，`stream_workflow` 為 `buffered-then-stream after D PASS`。
3. **逾時**：`e_observability/deadline.py: DeadlineGuard/current_deadline_guard/deadline_scope/run_with_deadline` + `LINE_IDENTITY_HASH_KEY` 衍生、`LINE_SESSION_DB_PATH=data/processed/line_sessions.sqlite3`。

### 可插入語意分流的唯一合法切口

```
red_flag/auth/product command 檢查
  → deterministic product fast path（<200ms）
  → [NEW] local semantic router shadow（ms 級，local bge-m3，UNKNOWN→fallback，僅 log，不改結果）
  → ConversationInterpreter (Formal/Deterministic) —— 複雜 intake / mixed / correction / subject change 專屬
  → candidate_merge + PendingAction + state machine
  → RAG+B/C/D（僅 pure/mixed 中教育子句）
```

---

## 14. 主線現況（HEAD 與測試數，可重現）

- **HEAD**：`91f403b fix: close adversarial demo integration gaps`（`main`），`git status --porcelain` 僅 `M docs/reviews/conversation_intelligence_challenges_20260829.md`（本研究準備），其餘乾淨；commits 至 `747c5f1` 間 `main` 領先 ~20 commits（`P2A.1 latency/p2b/clinician portal/line reliability/readiness` 等）。
- **Python**：`3.10.20`，`.venv`。
- **測試**：`.venv/bin/python -m pytest -q` → **`537 passed, 2 warnings in ~54s`**（warnings 為 FastAPI `on_event is deprecated, use lifespan`，非本研究引入）。

```bash
.venv/bin/python -m pytest -q
python3 -m experiments.semantic_router_eval.evaluate --output docs/reviews/semantic_router_eval_20260829.md --json-output /tmp/semantic_router_eval_20260829.json
pytest -q experiments/semantic_router_eval/tests  # （需檢出該 branch）
python scripts/p2a_live_smoke.py --dry-run -q     # P2A.1 延遲/混合意圖驗證，無需真模型
```

---

## 15. 最後選擇與理由（不捏造數據，保留反證）

### 選擇：分層混合路由 + Semantic Router shadow-only + 閾值延後決策

```text
Layer 0：deterministic safety（紅旗、授權、subject 與身份邊界；任何路由不得繞過）

Layer 1：deterministic product fast path（明確短答案、暫停/繼續、確認、身份詢問；目標 <200ms）

Layer 2：local semantic routing（只判斷 PURE_INTAKE/PURE_EDUCATION/MIXED/CHITCHAT/UNKNOWN，不直接寫入；低信心一律升級）

Layer 3：complex interpretation（自然 intake、跨輪修正、本人/家屬切換、指代不明與 MIXED 才用 LLM；Formal 8s + run_with_deadline + DeadlineGuard）

Layer 4：grounded education（純衛教或 mixed 中衛教子句才 RAG＋C＋D；必要時非同步 push，placeholder 先行）
```

**為何不是純規則**：規則在 §2.1/2.3 已證泛化不足（`晚上一直跑廁所` 需語意泛化），且 `conversation_intelligence_challenges §2.9` 直言「讓所有訊息共用最貴管線」是產品設計失誤。

**為何不是讓 Semantic Router 直接接管**：84 筆實證 **false-fast=0 時 MIXED recall=0**，放寬則 25–29 false-fast；MIXED 需子句切分與多執行緒合併，非單標籤分類；且與「分類器不得寫入」紅線衝突。

**為何不是 SetFit 直接接管**：標註量與人審尚未就緒；論文亦言`more data, not more training`；本專案決議「累積足夠人工標註後再評估」（`challenges §3`），短期以影子分流證明價值更安全。

**為何不是取消而直接上線 vLLM 方案**：vLLM 的 K8s/Redis/Envoy 對單機地端過重；取其 `signal→projection→decision` 與 `hybrid confidence (0.72, logprob 0.65/margin 0.35, cost_quality 0.45)` 心法，以本地 `HybridRouter(alpha 0.3) + LightGBM/規則 policy` 輕量重現即可。

### 具體落地（P2A.2 shadow → P2B 分階放行）

1. **Shadow logging（立即）**：在 `Orchestrator.handle_text()` 的 `product fast path` 之後插入 `PrototypeSemanticRouter.predict()`，**不改回覆**，僅以 `TraceRecorder.span("SEMANTIC_ROUTER", "shadow")` 記錄 `prediction/top_score/margin/scores/matched_labels` 與 `fallback`，落 `WebhookEvent.result` 與 `TraceSink`；與 `staged_latency` 並列，验证 `warm p95` 是否可壓至 `<150ms`（現 `172.3ms` 超 22ms，需改直載本地 `sentence-transformers` 消 HTTP 往返）。
2. **高信心放行（有條件）**：僅當 `top_score ≥ cos_th AND margin ≥ margin_th` 且 `prediction in {PURE_EDUCATION, CHITCHAT}` 且 `intake-active == False` 時，**跳過** `ConversationInterpreter` 直送 `RAG+B/C/D`；`MIXED/CORRECTION/SUBJECT_CHANGE` 即便高分亦升級（不可快轉）。
3. **校準（必要）**：複用 `RouteLLM` 的 `quantile(1 - strong_pct)` 腳本（`routellm/calibrate_threshold.py`）在去識別 held-out（≥300 人審句，含 `水果份量/側睡/跑廁所` 等未見變體、multi-label）上重校，并報告 `APGR/CPT` 曲線；採用 `challenges §3` 的安全預算【`MIXED recall ≥75% & false-fast=0` → `false-fast=0` → `macro_F1`】四階擇優，`UNKNOWN` 永遠回退。
4. **延遲體感（並行）**：完成 `typing indicator`（`AI_STATE_THINKING`）與 `SSE text_delta`（`stream_workflow` 由 `buffered-then-stream` 改 token streaming），配合既有 `ASYNC_PLACEHOLDER_REPLY + _FORMAL_SEMAPHORE(5) + DeadlineGuard`，將 `P1.1 Formal 3.8s / P2A 9.9s（p95 16.9s）` 的感知等待降至 `TTFT <500ms` 級。

### 不適合的直接排除

- Semantic Router **不得**擁有 `repository.save()/intake_snapshot` 寫入權；**不得**取代 `b_context_gate`/`d_output_gate`；**不得**在 `RED_FLAG/AUTHORIZATION/PRODUCT_COMMAND` 之前運行。
- 低信心、跨輪指代不明（`是我媽媽在吃`）、模糊短句（`幫我看看`）、假設/問句（`會是糖尿病嗎？`）一律 `UNKNOWN → LLM`。

---

## 附錄 A. 來源可點擊清單總表（供審稿逐條點開）

| # | 主題 | 來源 | URL | 發布者/日期 |
|---|---|---|---|---|
| 1 | semantic-router 概覽 | Repo | https://github.com/aurelio-labs/semantic-router | Aurelio Labs 2023-10-30 |
| 2 | 架構 | Docs | https://docs.aurelio.ai/semantic-router/user-guide/concepts/architecture | Aurelio AI |
| 3 | Routers | Docs | https://docs.aurelio.ai/semantic-router/user-guide/components/routers | Aurelio AI |
| 4 | Encoders | Docs | https://docs.aurelio.ai/semantic-router/user-guide/components/encoders | Aurelio AI |
| 5 | Threshold 優化 | Docs | https://docs.aurelio.ai/semantic-router/user-guide/features/threshold-optimization | Aurelio AI |
| 6 | Notebook | 官方 | https://github.com/aurelio-labs/semantic-router/blob/main/docs/06-threshold-optimization.ipynb | Aurelio AI |
| 7 | Issue 140 | GitHub | https://github.com/aurelio-labs/semantic-router/issues/140 | Aurelio Labs |
| 8 | Discussion 61 | GitHub | https://github.com/aurelio-labs/semantic-router/discussions/61 | aurelio-labs |
| 9 | vLLM SR Repo | Repo | https://github.com/vllm-project/semantic-router | vLLM 2025-08-26 |
| 10 | vLLM 官站 | Docs | https://vllm-semantic-router.com/docs/intro/ | vLLM SR Team v0.3 |
| 11 | Signal Pipeline | Docs | https://vllm-semantic-router.com/docs/overview/signal-driven-decisions/ | vLLM SR Team |
| 12 | Signal 概覽 | Docs | https://vllm-semantic-router.com/docs/tutorials/signal/overview/ | vLLM SR Team |
| 13 | 配置 | Docs | https://vllm-semantic-router.com/docs/installation/configuration | vLLM SR Team v0.3 |
| 14 | Canonical YAML | 原始碼 | https://raw.githubusercontent.com/vllm-project/semantic-router/main/config/config.yaml | vLLM SR Team |
| 15 | Sentence Transformers | Repo | https://github.com/huggingface/sentence-transformers | UKPLab→HF 2019-07-24 |
| 16 | STS | Docs | https://sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html | Hugging Face |
| 17 | Similarity API | Docs | https://sbert.net/docs/package_reference/util/similarity.html | Hugging Face |
| 18 | Pretrained | Docs | https://sbert.net/docs/sentence_transformer/pretrained_models.html | Hugging Face |
| 19 | 訓練總覽 | Docs | https://sbert.net/docs/sentence_transformer/training_overview.html | Hugging Face |
| 20 | BGE-M3 | Repo | https://github.com/FlagOpen/FlagEmbedding/tree/master/research/BGE_M3 | BAAI Paper 2402.03216 |
| 21 | SetFit 論文 | Paper | https://arxiv.org/pdf/2209.11055 | Tunstall et al. 2022-09-22 |
| 22 | SetFit Repo | Repo | https://github.com/huggingface/setfit | Hugging Face |
| 23 | SetFit Docs | Docs | https://huggingface.co/docs/setfit/index | Hugging Face |
| 24 | SetFit 概念 | Docs | https://huggingface.co/docs/setfit/main/en/conceptual_guides/setfit | Hugging Face |
| 25 | RouteLLM 論文 | Paper | https://arxiv.org/pdf/2406.18665 · https://arxiv.org/html/2406.18665v4 | Ong et al. 2024-06 |
| 26 | RouteLLM Repo | Repo | https://github.com/lm-sys/RouteLLM | LMSYS 2024-06-03 |
| 27 | 校準程式 | 原始碼 | https://github.com/lm-sys/RouteLLM/blob/main/routellm/calibrate_threshold.py | LMSYS |
| 28 | FDA CDS 指引 | Guidance | https://www.fda.gov/media/109618/download | FDA 2022-09 |
| 29 | FDA AI Draft | Guidance | https://www.hhs.gov/guidance/sites/default/files/hhs-guidance-documents/FDA/guidance-ai-enabled-device-software-functions.pdf | FDA Draft 2025-01-06 |
| 30 | WHO 倫理 | Guidance | https://iris.who.int/server/api/core/bitstreams/4f2c477c-4b72-4ca1-9a78-a1e73af64e50/content | WHO |
| 31 | WHO LMM | Guidance | https://www.who.int/publications/i/item/9789240084759 | WHO 2024-01-18 |
| 32 | WHO 監管 | Guidance | https://iris.who.int/server/api/core/bitstreams/ad62580f-540f-4e36-b957-e7f2946ae1fb/content | WHO |
| 33 | IMDRF | Framework | https://www.imdrf.org/documents/software-medical-device-samd-clinical-evaluation | IMDRF |
| 34 | FG-AI4H | 報告 | https://www.itu.int/dms_pub/itu-t/opb/fg/T-FG-AI4H-2025-1-PDF-E.pdf | ITU/WHO 2018-2023 |
| 34a | NHS DTAC v2 | 標準 | https://www.digitalregulations.innovation.nhs.uk/regulations-and-guidance-for-developers/all-developers-guidance/using-the-digital-technology-assessment-criteria-dtac/ | NHS England 2026-02 Form 2.0 / 2026-04-06 全面切換 |
| 34b | NHS DCB0129/0160 | 指引 | https://www.england.nhs.uk/long-read/digital-clinical-safety-assurance/ | NHS England 常駐 |
| 34c | NHS Ambient Scribe | 指引 | https://www.england.nhs.uk/long-read/guidance-on-the-use-of-ai-enabled-ambient-scribing-products-in-health-and-care-settings/ | NHS England 2025-10-22 |
| 35 | Hastings | Briefing | https://www.thehastingscenter.org/briefingbook/ai-in-healthcare/ | Hastings 2026-03-25 |
| 36 | LINE Webhook | Docs | https://developers.line.biz/en/docs/messaging-api/receiving-messages/ | LINE Developers |
| 37 | LINE 訊息 | Docs | https://developers.line.biz/en/docs/messaging-api/sending-messages/ | LINE Developers |
| 38 | LINE AsyncAPI | Spec | https://apis.io/asyncapis/line/line-messaging-webhook/ | APIs.io / LINE OpenAPI |
| 39 | paperclip Fast-ACK | Repo | https://github.com/NahuelGenchi/paperclip-plugin-line | Nahuel Genchi |
| 40 | SSE/WS 選型 | Blog | https://www.channel.tel/blog/streaming-ai-responses-sse-websockets-real-time | Chanl AI 2026-03-08 |
| 41 | Optimistic UI | Blog | https://pressbot.io/optimistic-ui-for-buffered-chatbot-streams/ | PressBot 2026-02-15 |
| 42 | Typed Events | Pattern | https://github.com/agentpatternscatalog/patterns/blob/main/patterns/streaming-typed-events.md | Agent Patterns Catalog |
| 43 | TTFT 優化 | Blog | https://barkingiguana.com/writing/streaming-responses-to-cut-first-token-latency/ | Barking Iguana 2026-07-13 |
| 44 | 安裝臉譜 | Docs | https://getstream.io/chat/docs/javascript/ai-message-streaming.md | Stream |

---

## 附錄 B. 無捏造聲明與數據邊界

- 本文件**未捏造任何延遲或準確率數據**：
  - `formal interpreter p50 3.842s/p95 5.632s（11 輪，fallback 0/11）` 與 `P2A live p50 9.9s/p95 16.9s / 口乾+跑廁所約 5.2s / 紅旗 13ms` 來自 `docs/reviews/conversation_intelligence_challenges_20260829.md §2.7`（P2A 階段量測，經 `staged_latency` 分階驗證）。
  - `bge-m3 cold 170ms / warm p50 161.2ms p95 172.3ms（25 rounds）` 來自 `experiments/semantic_router_eval/evaluate.py:benchmark_embedding` 與 `semantic_router_eval_20260829.md` 原始報告（ollama 本地，非雲端推論）。
  - `macro/micro/MIXED recall/coverage/fallback/false-fast` 皆引 `evaluate.py: metrics_for/select_recommended` 的 CSV/JSON 輸出，非手填。
  - 業界延遲（如 `TTFT p95 <500ms 感覺靈敏`、`TPOT 140 tok/s`）均為引述（Chanl/Barking Iguana/CloudSignal），非本專案數據。
- 不確定性已標註：`BAAI/bge-m3` 的 `LocalSparseEncoder` 支援經 `docs.aurelio.ai` 間接佐證但無官方單獨列點，需自測 `normalize_embeddings=True`；vLLM 的 `pricing/reliability` 等僅作生產對照，不作為本專案參數。

---

## 附錄 C. 授權與追溯

- `semantic-router` MIT；`vllm-project/semantic-router` Apache 2.0；`sentence-transformers` Apache 2.0；`setfit` Apache 2.0；`RouteLLM` Apache 2.0。引用皆以 Permalink 指向原始 repo/docs/paper，不另行抄襲程式碼。
- 實驗分支 `semantic-router-eval@747c5f1` 與 `main@91f403b` 的報告與程式碼互不汙染，forensic 可重現（`git show 747c5f1:…`）。

