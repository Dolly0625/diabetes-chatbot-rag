# LIMITATIONS

This file is intentionally conservative. It records what the current code and
artifacts do not establish.

## Product and safety scope

- The project is an MVP/demo and research baseline, not a clinical decision
  system or production clinical service.
- It must not be presented as a diagnosis, prescription, medication-change
  engine, emergency triage service, or substitute for a qualified medical
  professional.
- Acute/emergency thresholds in the demo are not formally clinically approved.
- `declared_role` is caller-provided metadata, not identity verification or
  authorization.
- There is no production PHI governance, access control, retention policy, or
  deployment security model.

## Agent and LangGraph

- The Planner is constrained to recovery actions. It does not generate the
  medical answer, approve evidence, or own graph execution.
- The final Cloud demo proves Cloud structured action selection, but not
  clinical correctness of the selected action.
- `ASK_USER` is implemented as a node that ends with `NEEDS_CLARIFICATION`.
  There is no interrupt/resume, checkpointer, persistent thread, or long-term
  memory. The demo starts a new invocation from A to simulate a reply.
- The case runner simulates the reply and the re-entry. This is not an
  interactive production conversation loop.
- Bounds are small v0.1 limits: two Agent steps, one rewrite, and one
  clarification by default. These limits are safety controls, not an
  evaluation of ideal conversation strategy.
- Query rewrite validation is deliberately narrow. It catches selected token
  loss and obvious added medical facts; it is not a complete semantic
  equivalence proof.
- Cloud behavior depends on provider availability, model changes, prompts,
  network latency, and structured-output compatibility. Three cases and three
  repeated ASK_USER runs are a small stability check, not a statistical
  reliability study.

## A / prompt injection

- A's default guard is deterministic regex-based protection. Its coverage is
  limited for varied natural-language prompt injection.
- The optional Qwen3Guard adapter exists, but the final Cloud Agent trace does
  not establish a full live Qwen guard evaluation.
- PI-1/PI-2 regression tests inject an existing blocked-result boundary to
  verify that Agent/RAG cannot bypass A. They are not evidence that every
  prompt-injection formulation is detected.
- The optional LangChain signal-extractor path is not the authority for policy;
  A's policy gate remains the boundary.

## RAG and TFDA data

- The processed corpus is 129 TFDA risk-communication records associated with
  dataset reference 9573. It is a small corpus, not a broad medical knowledge
  base or benchmark.
- Each record is retained as one LangChain Document. There is no chunking
  strategy in the current real retriever.
- Retrieval uses `intfloat/multilingual-e5-small` and an in-memory vector store.
  The index is lazy and ephemeral; there is no persistent vector database,
  index versioning, or serving layer.
- The retriever can resolve an explicit path or environment variable, but the
  expected `/mnt/data/langchain_documents.json` was absent in this workspace;
  the repository copy was used as fallback.
- Similarity retrieval and rank improvements do not prove that a document is
  sufficient, safe, current, or clinically applicable to an individual.
- The final Cloud Agent trace deliberately uses a fixture retriever, so it is
  not an end-to-end Cloud LLM plus real TFDA retrieval run.

## B / C / D

- The Agent workflow's B component is `DemoContextGate`/`DeterministicContextGate`.
  It is a fixture or explicit demo approval mode, not a live semantic context
  judge.
- In the ASK_USER case, the clarified retrieval rank improves, but the
  deterministic demo B observation can still remain `INSUFFICIENT`. The trace
  must not be described as proving a live B PASS.
- A live semantic B judge, with independent evaluation of relevance,
  sufficiency, conflict, and safety, is not implemented in the Agent v0.1
  workflow.
- The canonical C v2 contract and LangChain adapter exist, but the main Agent
  demo uses `DeterministicFixtureCGenerator`.
- The D verifier is a heuristic demo verifier/injectable test boundary. It is
  not an independently validated clinical semantic verifier.
- The real corpus workflow demo's `all_retrieved` approval mode is explicitly a
  demo mode and must not be called clinical B approval.

## Observability

- E records structured execution observations and is fail-open when a sink
  fails. It cannot guarantee durable delivery, distributed correlation, or
  production-grade audit retention.
- Redaction handles common credentials and query hashing, but this is not a
  complete privacy/PHI de-identification system.
- The trajectory formatter shows selected structured fields. It does not and
  cannot expose hidden model reasoning.
- Trace artifacts contain full JSON snapshots and should be handled as project
  evidence, not automatically as a production privacy-approved log format.

## Evaluation and operations

- Final tests are `74 passed, 10 skipped`; skipped tests remain skipped and
  have not been converted into evidence of success.
- No comprehensive network integration suite, provider failover test,
  concurrency test, load test, cost benchmark, or statistically meaningful
  latency benchmark is included in this handoff.
- No formal clinical expert adjudication, calibration study, or large
  adversarial benchmark is included.
- The three Agent cases are intentionally designed demo cases. They are not a
  representative sample of patient questions.
- Historical phase artifacts and current Agent traces use different harnesses
  and should not be pooled as if they were one experiment.
- The workspace has no Git metadata, so provenance is file/run based rather
  than commit based.

