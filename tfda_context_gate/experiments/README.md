# Experiments Index

The numbered phase scripts remain at the `tfda_context_gate/` package root in
this iteration because they use script-local imports (`run_config`,
`rate_limiter`) and write to established run directories.

Current experiment groups:

- `00_download_and_inspect.py` — data inspection.
- `01_build_documents.py` — LangChain document construction.
- `02_similarity_retrieval.py` — embedding retrieval and Contract Gate.
- `03_reranker.py` — cross-encoder reranking.
- `04_llm_judge.py` — document/context judge experiments.
- `05_hybrid.py` — hybrid retrieval, set-level judge and metrics.
- `c_generator/*experiment*.py` and `*_run_experiment.py` — C experiments.

Future cleanup may move these scripts here after compatibility wrappers and
tests are added. Do not delete historical `runs/`, `results/` or deliverables
without a provenance decision.

## Conversation optimization notes

- [`docs/reviews/conversation_intelligence_challenges_20260829.md`](../../docs/reviews/conversation_intelligence_challenges_20260829.md)
  records the P1.1–P2A conversation-understanding, multi-intent, data-quality,
  latency, timeout and semantic-routing challenges for later reports.
