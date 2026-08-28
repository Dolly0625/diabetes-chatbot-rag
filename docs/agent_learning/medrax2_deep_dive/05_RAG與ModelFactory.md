# 05｜RAG 與 ModelFactory

本章最重要的結論：MedRAX2 的 RAG 不只是「搜尋文件」，而是一個內含第二次生成的完整 QA tool。

## 1. RAG 在 graph 中不是必經 node

`RAGTool` 和 classifier、web search 一樣被放進 tools list：

```python
"MedicalRAGTool": lambda: RAGTool(config=rag_config)
```

所以路徑可能是：

```text
User → orchestrator LLM → 直接回答
User → orchestrator LLM → classifier → orchestrator LLM → 回答
User → orchestrator LLM → RAGTool → orchestrator LLM → 回答
```

RAG 是否執行由 LLM tool selection 決定，不是 graph 強制。

## 2. RAGTool 是薄 adapter

[`medrax/tools/rag.py`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/tools/rag.py#L26-L88)：

```python
class RAGTool(BaseTool):
    name = "medical_knowledge_rag"

    def __init__(self, config):
        self.rag = CohereRAG(config)
        self.chain = self.rag.initialize_rag(with_memory=True)

    def _run(self, query: str):
        result = self.chain.invoke({"query": query})
        output = {
            "answer": result["result"],
            "source_documents": [
                {"content": doc.page_content, "metadata": doc.metadata}
                for doc in result.get("source_documents", [])
            ],
        }
        return output, metadata
```

它回傳的是生成 answer 加 source documents，不是單純 ranked evidence list。

## 3. RAG 內部 pipeline

`CohereRAG` 初始化：

```python
self.chat_model = ChatCohere(model=config.model, ...)
self.embeddings = CohereEmbeddings(model=config.embedding_model)
self.reranker = CohereRerank(model=config.rerank_model)
self.memory = ConversationBufferMemory(...)
self.pinecone = Pinecone(api_key=...)
self.vectorstore = self.get_or_create_vectorstore()
```

查詢流程：

```python
docs = self.vectorstore.similarity_search(
    query, k=self.config.retriever_k * 2
)
reranked = self.reranker.rerank(
    query=query,
    documents=[doc.page_content for doc in docs],
)
return [docs[item["index"]] for item in reranked[:self.config.retriever_k]]
```

再由 `RetrievalQA` 的 `stuff` chain 把 reranked documents 塞給 Cohere chat model 產生 `result`。

完整資料流：

```text
query
→ Cohere embedding
→ Pinecone similarity search (2k candidates)
→ Cohere rerank (top k)
→ RetrievalQA stuff
→ Cohere answer generation
→ answer + source_documents
→ orchestrator LLM final synthesis
```

這是雙生成結構：

```text
RAG internal LLM generation
→ orchestrator LLM generation
```

## 4. 雙生成帶來的風險

優點：RAG tool 給 orchestrator 一個易讀的答案，不必讓外層模型自行消化大量原文。

風險：

- 內層 answer 可能已經丟失細節；
- 外層 synthesis 可能再次改寫或新增聲明；
- citation 編號由 prompt 要求，不代表 claim-source mapping 被程式驗證；
- source document 有回傳，不等於 final answer 每一 claim 都受其支持；
- RAG conversation memory 與 Agent checkpoint memory 是兩套 memory。

## 5. Corpus 建立是初始化副作用

如果 Pinecone index 為空，`get_or_create_vectorstore()` 會載入 local/HuggingFace documents 並批次寫入 index。

這表示建立 `RAGTool` 可能不是輕量 constructor：

```text
new RAGTool
→ new CohereRAG
→ connect Pinecone
→ inspect index
→ possibly load/split documents
→ embeddings + upload
```

在 production 中通常應將 offline indexing 與 online query service 分離，避免啟動 Agent 時意外進行長時間 ingestion。

## 6. ModelFactory 的責任

[`model_factory.py`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/models/model_factory.py#L12-L142) 依 model name prefix 選 provider：

```python
_model_providers = {
    "gpt": {"class": ChatOpenAI, "env_key": "OPENAI_API_KEY"},
    "gemini": {"class": ChatGoogleGenerativeAI,
               "env_key": "GOOGLE_API_KEY"},
    "openrouter": {"class": ChatOpenAI,
                   "env_key": "OPENROUTER_API_KEY",
                   "default_base_url": "https://openrouter.ai/api/v1"},
    "grok": {"class": ChatXAI, "env_key": "XAI_API_KEY"},
}
```

```python
provider_prefix = next(
    prefix for prefix in cls._model_providers
    if model_name.startswith(prefix)
)
return model_class(model=actual_model_name, ...)
```

Agent graph 因此不依賴特定 provider；只依賴 `BaseLanguageModel` 和 `bind_tools/invoke` 行為。

## 7. Model-agnostic 不等於 behavior-agnostic

即使 API interface 統一，不同模型仍可能有差異：

- tool schema 支援程度；
- 是否能平行 tool call；
- multimodal message 格式；
- system/tool message 相容性；
- stop behavior；
- token accounting；
- structured output 穩定性。

MedRAX2 在 tool result 後額外補 `HumanMessage` synthesis prompt，就是對 provider/model behavior drift 的 application workaround。

## 8. 與 TFDA RAG 的根本差異

```text
MedRAX2：
RAGTool = retrieve + rerank + generate
外層 Agent = 再生成 final answer

TFDA：
RAG = candidate retrieval
B = evidence approval
C = answer generation
D = output verification
```

對受監管的用藥資訊，後者的信任邊界更清楚。若借鏡 MedRAX2 的 tool 化，應改成：

```text
TFDA retrieval tool
→ normalized candidate evidence
→ B approval
→ C
→ D
```

而不是把 tool 內生成的 answer 當成 approved medical content。
