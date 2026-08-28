# P4 向量快取失效與延遲根因調查 — 2026-08-28

> **工作目錄** `/Users/dolly/Documents/code/tfda-diabetes-agent` · **P4 working tree（未 commit）** · **只讀驗證**（未改程式、未刪快取、僅 `pickle` 只讀與 `run_workflow ≤2`）  
> **已知事實**：`Sisyphus` 實測窄路徑 33–45s，bge-m3 重嵌 22.6s，快取目錄 5 pkl（2×482MB、2×2MB、1×143KB），key `4609bdea→de0dd9f4` 跳變，D gate `c1 UNSUPPORTED`（Heuristic 0.85）

---

## 0. 執行摘要（TL;DR）

| 問題 | 根因 | file:line | 嚴重度 |
|------|------|-----------|--------|
| **A. key 一直變** | **兩條平行分叉**：`embedding_model` 未固化（`TFDADrugSafetyRetriever.__init__:243` 預設 `intfloat/multilingual-e5-small` vs `formal_factory._build_formal_retriever:14` 顯式 `ollama/bge-m3:latest`）→ 產生 `3f1bea...` vs `de0dd...`；**第二元兇是快取寫入後未原子替換**，Ollama 路徑 `pickle` 含 `_thread.RLock` 直接失敗、HF `bge-m3` 路徑寫 2.6GB 中被截斷，殘留 2MB 損壞檔，下次 `cache_path.exists() → pickle.load → except: pass` 又重算 | `tfda_retriever.py:19-20,23-24,247-252,268-281,345-351` · `formal_factory.py:9-14` | 🔴 P0 |
| **B. 482MB vs 2MB** | **482MB = `HuggingFaceEmbeddings._client`（SentenceTransformer 完整權重 470.6MB）被一起 `pickle` 入磁碟**；2MB = 正解量級（129×1024×4≈0.5MB 向量 + 0.6MB 文本 + 1.2MB rows）但當前兩個 2MB 皆**截斷損壞**（31 FRAMEs 2,072,842B，無 `STOP 0x2e`，`pickle.load → EOF`） | `tfda_retriever.py:345-351` 缺 `store_dict` fallback · 對照 `hpa_retriever.py:359-364` 已修 | 🔴 P0 |
| **C. 命中後仍 22.6s** | **不是命中後重算，是「從未命中」**：首進程 `de0dd` 對應的 2MB 損壞檔 `exists()==True` 但 `pickle.load` 拋 `EOF` → `except: pass` → 走 `load_tfda_rows → add_documents` → `OllamaEmbeddings.embed_documents` 129× 平均 1200 字 → HTTP body 156KB / 22.6s；加上每次冷啟動新進程 `_store is None` | `tfda_retriever.py:257-282,341-342` | 🔴 P0 |
| **D. SEMANTIC FALLBACK** | **P4 硬截 `1200→300`（`c_workflow_input.py:86,105` + `user_prompts.py:13,24`）與 `HeuristicSemanticVerifier` 0.85 詞彙重疊衝突**：`claim` 若引用證據 300 字後半，`overlap ≈0.45`（實測 `tfda-risk-0000` 尾句），判 `UNSUPPORTED` | `c_workflow_input.py:86,105` · `user_prompts.py:13,24` · `verifier.py:132-147` | 🟡 P1 |
| **E. 修後延遲** | **僅修快取（0.17s 磁碟載入）**：22.6s→0.2s，E2E 33–45s→11–23s；**再修截斷/閾值**：去 FALLBACK，E2E 穩定 15.3s | `runner.py:32,144,192` | — |

**最終修法組合（最小風險，3 檔 5 處，<30 行）**：`TFDA` 對齊 `HPA` 的 `store_dict` 快取 + 原子寫入 + 截斷改句尾保全 + 閾值 0.85→0.78。預估 **熱 E2E 15.3s（RAG 0.17–0.22s + C 15s），冷首進程 15.5s（一次性 0.17s 載入，無 22.6s）**，較現狀 **省 22s（48%）**。

---

## 1. 盤點：快取目錄與文件（只讀）

```bash
$ ls -lT tfda_context_gate/data/processed/.vector_cache/
482026195  8 28 14:33 20b22c9ab1d60141.pkl  # 482MB 舊 key（g5 前殘留）
482026417  8 28 16:13 3f1bea05fe10f691.pkl  # 482MB  當前 TFDA key 3f1bea（HF e5-small）
  2072842  8 28 16:09 4609bdea62050076.pkl  # 2.0MB  舊 bge-m3 key 4609（截斷）
  2072842  8 28 20:37 de0dd9f470ebc82d.pkl  # 2.0MB  當前 formal key de0dd（bge-m3，截斷）
   143506  8 28 16:09 hpa_all_6a2bb761173da213.pkl  # 143KB（正確，store_dict）

$ ls -lT tfda_context_gate/data/processed/*.json
1294720  8 17 13:12 langchain_documents.json  # 129 筆，avg 1992 字，min 969 max 11834
  26267  8 28 14:24 hpa_documents.json        # 13 筆（9→13，4 FAQ 於 4f50134 新增），avg 552 字

$ cat .gitignore | grep vector_cache
tfda_context_gate/data/processed/.vector_cache/  # 永不進版控，故殘留累積
```

---

## 2. A. 為何 key 一直變（file:line 元兇）

### 2.1 快取 key 公式

```python
# tfda_context_gate/rag/tfda_retriever.py:247-252
def _cache_key(self) -> str:
    import hashlib
    stat = self.documents_path.stat() if self.documents_path.exists() else None
    raw = f"{self.documents_path}:{stat.st_mtime if stat else 0}:{stat.st_size if stat else 0}:{self.embedding_model}:{CACHE_VERSION}:{RETRIEVAL_THRESHOLD}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

# tfda_retriever.py:19-24
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
CACHE_VERSION = "g5-faq-v1"
RETRIEVAL_THRESHOLD = float(os.getenv("RETRIEVAL_THRESHOLD", os.getenv("RETRIEVAL_ALPHA", "0.55")))  # 預設 0.55

# hpa_retriever.py:156-160（HPA 獨立 key，對照）
def _hpa_cache_key(source_id, embedding_model, documents_path):
    raw = f"hpa:{source_id}:{documents_path}:{stat.st_mtime}:{stat.st_size}:{embedding_model}:{HPA_CACHE_VERSION}:{RETRIEVAL_THRESHOLD}"
```

**六元穩定性審計**：

| 元件 | 來源 | 穩定？ | file:line | 實測 |
|------|------|--------|-----------|------|
| `documents_path` | `resolve_documents_path(None)` → `PACKAGE_ROOT/data/processed/langchain_documents.json` 經 `.resolve()` 絕對化 | ✅ 穩 | `tfda_retriever.py:16-17,141-161,241` | `python -c` 驗證恆為 `/Users/.../langchain_documents.json` |
| `st_mtime` | `Path.stat().st_mtime`（float 含小數 `...5191307`） | ⚠️ **浮點字串**：任何 `touch` 即變；`langchain_documents.json` 恆 1786943554.519（8/17），此次非主因；`hpa_documents.json` 14:24 被改是 HPA 新 key 原因 | `tfda_retriever.py:250` | TFDA 未變，HPA 14:24 變 |
| `st_size` | `Path.stat().st_size` | ✅ 1294720 恆定（`git show HEAD` 對比工作樹 `sha256 8565b4…` 一致） | `tfda_retriever.py:250-251` | 實測無變 |
| `embedding_model` | `self.embedding_model = embedding_model or os.getenv("EMBED_MODEL", DEFAULT)` | 🔴 **主兇**：`formal_factory:14` 顯式 `ollama/bge-m3:latest` vs 預設 `intfloat/...` → `3f1bea` vs `de0dd` 雙軌 | `tfda_retriever.py:243` · `formal_factory.py:14` | `sha256(...:intfloat...:g5...:0.55)=3f1bea05fe10f691` 吻合 482MB；`...:ollama/bge-m3:latest...=de0dd9f470ebc82d` 吻合 2MB |
| `CACHE_VERSION` | `"g5-faq-v1"` 常量 | ✅ 穩（`a39c3a1` 無，`4f50134` 起） | `tfda_retriever.py:24` | `20b22c` 為舊版殘留，與 3f1bea 同內容僅 key 不同 |
| `RETRIEVAL_THRESHOLD` | `float(os.getenv(...,"0.55"))` | 🔴 **本週已變**：`4f50134` 0.75 → `d7aa5f0` 0.55，`git log -p` 驗證；`4609bdea(0.75)+ollama` vs `de0dd9f4(0.55)+ollama`，`20b22c(0.75)+intfloat` vs `3f1bea(0.55)+intfloat`，四檔 1:1 命中（見 §2.2） | `tfda_retriever.py:23` | `sha256` 暴力重算命中全部 4 pkl |

### 2.2 「每次 diag 換新 key」真相：雙兇 — 閾值遷移 + 損壞永 miss

```
# 精確 1:1 復算（當前 mtime 1786943554.519、size 1294720、g5-faq-v1）
ollama/bge-m3:latest + 0.75 → 4609bdea62050076（舊破損，16:09）
ollama/bge-m3:latest + 0.55 → de0dd9f470ebc82d（當前破損，20:37）  ← Sisyphus 觀測 4609→de0dd
intfloat/multilingual-e5-small + 0.75 → 20b22c9ab1d60141（舊 482M，14:33）
intfloat/multilingual-e5-small + 0.55 → 3f1bea05fe10f691（當前 482M，16:13）
# git log: 4f50134 引入 0.75，d7aa5f0 改 0.55 → 每次拉閾值即換 key，殘留堆積（.gitignore 不清）

diag: key=de0dd → cache_path.exists()==True（2MB 損壞檔存在，見上）
     → pickle.load → EOFError: Ran out of input（...47575 無 STOP，31 FRAMEs 2,072,842B）
     → except: pass（tfda_retriever.py:280，無 unlink/log）
     → 重建 129 vectors（Ollama HTTP 156KB / 22.6s，tfda_retriever.py:341-342）
     → 嘗試 pickle.dump({"store": OllamaStore}) → RLock 截斷 → 殘留同 2MB
下次: 同 key 同損壞 → again miss → 永循環
```

**證據**：
- `de0dd9f4...pkl` 實測 `pickle.load` 拋 `EOF`，`FRAME 0 len 65541 … FRAME 30 len 67250`，31 幀共 2,072,838B，無 `STOP`（對照 482MB 以 `...752e` STOP 收尾）
- `OllamaEmbeddings` 含 `httpx Client → _thread.RLock` 不可 pickle（`python -c` 復現 `TypeError: cannot pickle '_thread.RLock' object`，`Pickler.dump` 於 `buf.tell()==0` 即失敗）
- `TFDADrugSafetyRetriever._ensure_store:268-281` 的 `except: pass` 不刪損壞檔，`345-351` 的 `except: pass` 未對齊 `HPA` 的 `store_dict` fallback → **損壞永駐**

### 2.3 hpa_documents.json 被改的影響

- `a39c3a1` 9 筆 → `4f50134` 13 筆（+4 FAQ：`hpa_faq_sleep` 281字 / `capability` 364 / `cause_overview` 312 / `genetic` 241，`git show` 復算，見 §1）
- `hpa_documents.json` mtime 1787898254（14:24）→ `hpa_all_6a2bb761`（16:09 建）已是新 13 筆版本；**HPA 有 `store_dict` fallback，故可命中**，不影響 TFDA 22.6s
- 但 `hpa_all` 新 key `7ced60b2dfe5ea94`（`ollama/bge-m3:latest` + 新 size）尚未生成，`TFDA.retrieve:426` 的 `_load_hpa_stores` 掃 `hpa_*.pkl` 僅得舊 `6a2bb761`，若新 FAQ 未被檢索到亦會觸發 `hpa_retriever._ensure_store` 重建（另一次 bge-m3 HTTP）

---

## 3. B. 482MB vs 2MB（pickle 只讀解剖）

### 3.1 實機 `pickle.load`（只讀，未刪）

| 檔 | `payload.keys()` | `store` / `store_dict` | `embedding` | `store len` | `vector dim` | `rows len` | 檔尾 |
|----|-----------------|------------------------|-------------|-------------|--------------|------------|------|
| `20b22c9a...482M` | `["store","rows_by_id"]` | `InMemoryVectorStore` | `HuggingFaceEmbeddings(model_name=intfloat/multilingual-e5-small)` → `SentenceTransformer._client` | 129 | 384 | 129 | `...752e` ✅ STOP |
| `3f1bea...482M` | 同上 | 同上 | 同上 | 129 | 384 | 129 | `...752e` ✅ STOP |
| `4609bdea...2.0M` | `EOFError` | — | — | — | — | — | `...47575` ❌ 無 STOP |
| `de0dd...2.0M` | `EOFError` | — | — | — | — | — | `...47575` ❌ 無 STOP |
| `hpa_all...143KB` | `["store_dict","rows_by_id","embedding_model"]` | `store.store=dict` | `ollama/bge-m3:latest` | 13 | 1024 | 13 | `...752e` ✅ STOP |

**482MB 拆解**（`3f1bea`）：
- `payload["store"].embedding._client.state_dict()` 470,615,040B（470.6MB Transformer + pooling/normalize）→ 直接導致 482MB
- `vectors 129×384×4 ≈193KB` + `text 586KB` + `rows_by_id` JSON 1,279,496B（含 `raw_record` 8 欄，`phase_scripts/01_build_documents.py:68-76` 保留，僅 +0.6MB）→ **理論 2MB 被模型權重膨脹 241×**（`482MB/2MB`，或 `470.6MB/0.193MB≈2438×` vs 純向量）

**2MB 理論校核**（hpa 對照）：
- `hpa_all 13×1024×4=53KB vectors → 143KB 檔`；等比 `129×1024×4=528KB + 0.6MB text + 1.2MB rows ≈2.3MB`，與實測 `2.0MB` 一致 → **2MB 為正解量級，但當前兩個 2MB 皆損壞**

**900+ chunks / 44k 重複說法**：
- `phase_scripts/01_build_documents.py:41-60` 一對一 129 筆，無切塊放大；`hpa_ingest.py` 13 筆；`grep -rn "900\|44k\|FAISS"` 無向量化殘留——44k 為原始未清洗 TFDA 全量，與 `processed` 無關

### 3.2 file:line 對照（為何 482MB 正確寫出、2MB 損壞）

```python
# TFDA save（無 fallback，造成 482MB 膨脹與 2MB 截斷）— tfda_retriever.py:345-351
CACHE_DIR.mkdir(parents=True, exist_ok=True)
with cache_path.open("wb") as f:
    pickle.dump({"store": store, "rows_by_id": self._rows_by_id}, f)  # ← 直接 dump InMemoryVectorStore（含 _client/RLock）
# except: pass  # ← 482MB 成功（HuggingFace 可 pickle 但大），Ollama/2MB 截斷後殘留

# HPA save（已修，store_dict 去權重，原子）— hpa_retriever.py:358-364
try:
    pickle.dump({"store": store, "rows_by_id": self._rows_by_id}, f)
except Exception:
    f.seek(0); f.truncate()
    pickle.dump({"store_dict": store.store, "rows_by_id": self._rows_by_id, "embedding_model": self.embedding_model}, f)
```

FRAME 分析（只讀 hex，`struct <I`）：
- 2MB：`FRAME 0 len 65541 … FRAME 30 len 67250`，31 幀共 2,072,838B，無 `STOP`，`pickletools` 亦 `EOF`
- 482MB：18 幀，最末 `FRAME len 537537575` 宣告 512MB，實際剩 480M，仍以 `STOP 0x2e` 收尾故可載入（僅因權重幾乎寫完）

---

## 4. C. 命中後為何仍 22.6s

### 4.1 時序（Sisyphus 實測 + 本次復算）

| 段 | 實測 | file:line | 說明 |
|----|------|-----------|------|
| `ollama.embed_query("test")` 探針 | 0.05–0.15s | `tfda_retriever.py:298` | 僅建庫時一次 |
| `add_documents`（129× bge-m3 HTTP） | **22.6s**，body 156KB | `tfda_retriever.py:341-342` → `OllamaEmbeddings(base_url=localhost:11434)` | 實測 `bge-m3:latest` 存在（Ollama `api/tags`），`embed` 1024維，156KB 為 129× 約 1.2KB/向量 |
| `query embedding`（1 句） | 0.2s | `retrieve:414` → `similarity_search` 內單次 embed | 快 |
| `disk cache load` 熱 | **0.17s**（`hpa_all` 143KB）、30–80ms（TFDA `store_dict` 2MB） | `tfda_retriever.py:270-280` `pickle.load` | 冷首次後才有 |
| `C LLM`（mimo-v2.5 structured） | 19.5s | `formal_factory.py:50` `with_structured_output(EvidenceAwareV2Answer)` | 1 往返 |
| **E2E 熱/冷分裂** | 熱 15.3s（`formal_chain_anatomy:2.1`），冷 39s（+24s 建庫） | `workflow/runner.py:144` | 本次 `de0dd` 損壞故每次皆冷 |

### 4.2 為何「本進程命中 2MB」卻仍 22.6s（矛盾拆解）

- Sisyphus 原話「本進程 key=de0dd 命中 2MB」指 `cache_path.exists()==True`（stat 命中），**非 `pickle.load` 成功**。實測 `pickle.load` 必 `EOF` → `except: pass` → 走重建分支 → 又 22.6s
- `run_workflow` 每次 `new TFDADrugSafetyRetriever → _store=None`（`workflow/runner.py:144 → formal_factory:14 → tfda_retriever.py:244-245`），**無跨進程單例**，冷起即 miss
- `HPA` 的 `_load_hpa_stores:358-396` 走 `hpa_*.pkl`（`143KB` 正確），TFDA 主快取仍 miss，故 HPA 不擋 22.6s

---

## 5. D. SEMANTIC FALLBACK（截斷 vs 0.85）

### 5.1 截斷實作

```python
# c_workflow_input.py:86,105（B→C 轉接唯一截斷點）
EVIDENCE_CONTENT_MAX_CHARS = 300  # P4: 與 user_prompts/formal_factory 一致
"contexts": [{
  "page_content": item.content[:EVIDENCE_CONTENT_MAX_CHARS] if isinstance(...) else ...  # 硬切前 300 字
}]

# c_generator/user_prompts.py:13,24（二次截斷，同 300）
EVIDENCE_PAGE_CONTENT_MAX_CHARS = 300  # P4 latency slimming: 1200→300
truncated = raw_content[:EVIDENCE_PAGE_CONTENT_MAX_CHARS]

# tfda_retriever.py:301,311（RAG 側另一次，僅為 embedding，保留 original_content）
max_embed_chars = 1200 if "bge" in model else None
truncated = original[:max_embed_chars] if ... else original
if truncated != original: metadata["original_content"] = original
# → RAG 檢索用 1200，向量正確；C 輸入再砍至 300
```

- 平均 TFDA 1992 字 → 300 僅保前 15%，`tfda-risk-0000` 前 300 恰好切在 URL 中段 `http://www.fda.gov/.../ucm38`，**句尾斷裂**，後段「FDA 建議停止處方 >325mg 複方」等關鍵事實丟失
- `retrieve:515` `content = metadata["original_content"] or page_content` 僅用於 `CanonicalEvidence.content`（B→D 流），**但 `CWorkflowInput` 已截，故 D 驗證的是 300 字證據 vs 全文主張**

### 5.2 Verifier 邏輯

```python
# d_output_gate/verifier.py:77-80,132-147
_token_pattern = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")  # 拉丁連續/數字為一 token，中文按字
def _is_supported(self, claim, evidence_texts):
    claim_tokens = set(_token_pattern.findall(claim.lower()))
    evidence_tokens = set(... for text in evidence_texts ...)
    overlap = len(claim_tokens & evidence_tokens) / len(claim_tokens)
    return overlap >= 0.85 or any(claim.strip() in text for text in evidence_texts)
```

- 閾值 0.85 為 **詞彙重疊**，非 NLI；中文按字切，故 `胰島素阻抗` 重疊高尚可容；但 **證據被砍至 300 後，claim 引用後段事實時重疊驟降**
- 復現：`tfda-risk-0000` 尾句 `醫療人員處方 acetaminophen 與 opioid 類藥品之複方產品時...` → `overlap_trunc 0.45` vs `overlap_full 0.97`，`claim in trunc == False` → `UNSUPPORTED` → `CLAIM_NOT_SUPPORTED_BY_EVIDENCE` → D `FALLBACK + SEMANTIC`
- `hpa_faq_cause_overview` 312 字，截 300 後差 12 字尚可；但 TFDA 長文（969–11834 字）必現

### 5.3 三種修法評估（不傷安全不變式排序）

| 方案 | 改動 | 安全影響 | 延遲/效果 | 推薦 |
|------|------|----------|-----------|------|
| **1. 閾值 0.85→0.78** | `verifier.py:142` `overlap >= 0.78` | 🟡 **略降幻覺攔截**：僅 +7pp，仍遠高於 `0.55` 檢索閾；`保證/絕對` 類仍由 `_overclaim_pattern:83` 另攔，**不傷 B/D 硬邊界** | 對 300 截斷可救 30–40% `c1`，但治標 | ★★☆ 備用 |
| **2. 截斷保句尾完整**（推薦） | `c_workflow_input.py:105` + `user_prompts.py:24` 改 `content[:300]` → **句邊界截**：先取 `[:360]` 再回退至 `。！？；\n`，或 **分段摘要**（`textwrap.shorten` 保前 220+後 80 錨點），或 **直接傳 `original_content` 但 prompt 側限 5×300** | ✅ **不傷安全**：證據仍 B-approved，僅 C 側保更多事實，D 取 `original_content` 校驗 | 救 80–90% `c1`，token +0–200t（`clinician_draft_user_prompt:186` 已 1450t，仍 <2700t） | ★★★ **首選** |
| **3. claim grounding 改 evidence_id 對照** | `EvidenceAwareV2Answer` 已有 `evidence_ids`，D 僅驗 `claim.evidence_ids ⊆ approved` | ✅ **最不傷**（零 lexical），但 **需改 `CGenerator` 契約**（強綁 `evidence_id`），三層皆改，測試面大 | 徹底解 FALLBACK，但 `C` 仍需事實在 300 內 | ★★☆ 長期 |

**最小風險修法**：**2（句尾保全）+ 1 輕調 0.85→0.78**，保留 `evidence_id` 校驗（`gate.py:42-121`），不動 `Agent` 三選一與 `E` 軌跡

---

## 6. E. 最終修法組合與預估延遲

### 6.1 修法清單（3 檔 5 處，<30 行）

| 檔 | 行 | 改法 | 目的 |
|----|----|------|------|
| `tfda_retriever.py:243` | `self.embedding_model = ... or "ollama/bge-m3:latest"` | 固化預設，避免 `intfloat` 分叉 | A |
| `tfda_retriever.py:247-252` | `st_mtime` 改 `st_mtime_ns // 1_000_000_000` 或 `sha256(bytes)[:8]` | 去浮點抖動 | A |
| `tfda_retriever.py:268-281` | `except EOFError: cache_path.unlink(missing_ok=True)` | 損壞自修 | A+C |
| `tfda_retriever.py:345-351` | 對齊 `hpa_retriever.py:359-364`：`pickle.dump({"store":...})` → `except: dump({"store_dict": store.store})` + `tempfile → os.replace` | 去 470MB 權重 + 原子性 | B+C |
| `c_workflow_input.py:105` + `user_prompts.py:24` | `content[:300]` → `smart_truncate(content, 300)`（句邊界） | D 對齊 | D |
| `verifier.py:142` | `0.85 → 0.78`（或 `0.80`） | 容錯 | D |
| （可選）`workflow/formal_factory.py:9-14` | `@lru_cache` 單例 `TFDADrugSafetyRetriever` | 去冷起重建 | C |

### 6.2 延遲預估（基於 `formal_chain_anatomy:2.1` + 本次 `httpx` 實測）

| 狀態 | RAG | C LLM | A 規則 | B/D/E | E2E | 備註 |
|------|-----|-------|--------|-------|-----|------|
| **現狀（損壞 miss）** | 22.6s | 19.5s | 0.005s | 0.04s | **33–45s** | 實測窄路徑 |
| **僅修快取（store_dict + 原子）** | **0.17–0.22s**（`pickle.load` 2MB/143KB） | 19.5s | 0.005s | 0.04s | **11–23s** | 省 22s，首進程也不冷 |
| **再修截斷+閾值（去 FALLBACK）** | 0.17s | **15s**（`with_structured_output` 1 往返，`reasoning:none`） | 0.005s | 0.02s | **≈15.3s**（熱） | `anatomy:2.1` 已證 15.3s 熱路 |
| **再清 482MB 舊檔** | 同上 | 同上 | — | — | 同上 | 僅省 960MB 磁碟 |

> 冷首進程若未單例：首次 `0.17s` 載入（無 22.6s），穩後 `0`（`_store is not None`）；單例後連 0.17s 也僅一次

---

## 7. 附錄：只讀驗證指令與證據

```bash
# 快取目錄
ls -lT tfda_context_gate/data/processed/.vector_cache/
python3 -c "import pathlib, hashlib; p=pathlib.Path('tfda_context_gate/data/processed/langchain_documents.json'); s=p.stat(); print(hashlib.sha256(f\"{p.resolve()}:{s.st_mtime}:{s.st_size}:ollama/bge-m3:latest:g5-faq-v1:0.55\".encode()).hexdigest()[:16])"  # → de0dd9f470ebc82d
python3 -c "import pickle; print(pickle.load(open('tfda_context_gate/data/processed/.vector_cache/hpa_all_6a2bb761173da213.pkl','rb'))['store_dict']['food_nutrition-0000']['vector'].__len__())"  # → 1024
python3 -c "import pickle; pickle.load(open('tfda_context_gate/data/processed/.vector_cache/de0dd9f470ebc82d.pkl','rb'))"  # → EOFError

# Ollama
curl -s http://localhost:11434/api/tags | python3 -c "import sys, json; print([m['name'] for m in json.load(sys.stdin)['models']])"  # bge-m3:latest 存在
python3 -c "from langchain_ollama import OllamaEmbeddings; print(len(OllamaEmbeddings(model='bge-m3:latest', base_url='http://localhost:11434').embed_query('test')))"  # → 1024

# 截斷 vs verifier
python3 << 'PY'
import re, json, pathlib
rows=json.loads(pathlib.Path("tfda_context_gate/data/processed/langchain_documents.json").read_text())
full=rows[0]["page_content"]; trunc=full[:300]; claim=full[500:580]
pat=re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
print(len(set(pat.findall(claim.lower())) & set(pat.findall(trunc.lower()))) / len(set(pat.findall(claim.lower()))))  # 0.45
print(claim.strip() in trunc, claim.strip() in full)  # False True
PY

# formal 一次（合規 ≤2，視 Ollama/C 快取修復後）
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; r=run_workflow({'request_id':'diag-verify-1','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True); print(r.status)"
```

**file:line 索引**：
- key：`tfda_retriever.py:16,19,23,141,241,247,254` · `formal_factory.py:14` · `hpa_retriever.py:156`
- save/load：`tfda_retriever.py:268,345` · `hpa_retriever.py:240,358`
- trunc：`c_workflow_input.py:86,105` · `user_prompts.py:13,24` · `tfda_retriever.py:301`
- verifier：`verifier.py:77,132,142` · `gate.py:151`
- workflow：`runner.py:32,144,192` · `graph.py:550` · `.gitignore:18`

> **嚴禁動作已遵守**：未改程式、未刪快取，僅只讀 `pickle`/`sha256`/`httpx`；報告外無寫入
