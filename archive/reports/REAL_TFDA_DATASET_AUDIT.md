# Real TFDA Corpus Audit

盤點日期：2026-08-21

實際可用的 processed corpus 是
`data/processed/langchain_documents.json`；使用者指定的
`/mnt/data/langchain_documents.json` 在目前 workspace 不存在，因此 retriever
會優先接受明確傳入路徑或 `TFDA_DOCUMENTS_PATH`，再 fallback 到 repo 內這份檔案。

- top-level：JSON list，共 129 筆。
- 每筆是一個 TFDA「藥品安全資訊風險溝通資料」record，未再切 chunk。
- 欄位：`id`、`page_content`、`metadata`、`raw_record`。
- metadata：`document_id`、`row_index`、`source_dataset`、`raw_source_file`、`發布日期`、`藥品成分`。
- `id` 全部唯一；無 malformed row、空 `page_content` 或缺少藥品成分的 record。
- `page_content` 長度約 969–11,834 字元；平均約 1,992 字元。
- provenance 由原始 metadata 保留；RAG 對外映射為 `evidence_id`、`source`、`date`、`metadata`、`score`。

向量依賴沿用既有 `requirements.txt` 的
`langchain-huggingface`、`sentence-transformers`、`torch`，索引採
`HuggingFaceEmbeddings(intfloat/multilingual-e5-small)` +
`InMemoryVectorStore`，不新增大型向量資料庫或 Agent infrastructure。
