# Diabetes Care LLM / RAG / Agentic Workflow

This directory contains the diabetes-care LLM/RAG demo and research baseline.
It currently implements the A/B/C/D safety-oriented boundaries and E v0.1
observability layer. It is an MVP/demo, not an autonomous clinical decision
system and not a production clinical system.

Before changing the code, read [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)
and, for the audit evidence, [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md).

## Project Overview

The intended baseline is:

```text
User
  -> A: Input Router + Policy Gate
  -> Query Expansion
  -> RAG retrieval / Contract Gate / Context Gate
  -> B
  -> bounded Agent recovery when B is INSUFFICIENT
  -> C: Evidence-aware Generator
  -> D: Mandatory Output Gate
  -> Answer or Fallback
```

E is cross-cutting observability and evaluation data collection. It records
what happened in A, Query Expansion, RAG, B, Agent, C and D; it is
not a new medical Gate and does not decide the answer.

The executable deterministic baseline entry point is
`tfda_context_gate.workflow.runner.run_workflow()`.

## Current Scope

- Demo/MVP and research baseline.
- Evidence-aware generation and mandatory output validation.
- Offline deterministic tests and optional live model experiments.
- No autonomous clinical decisions, identity verification, tool authorization,
  or production PHI policy.

## Users

The demo accepts three declared roles:

- `PATIENT`
- `CAREGIVER`
- `HEALTHCARE_PROFESSIONAL`

`declared_role` is user-provided metadata. It is not verified identity and must
not increase access to sensitive data, tools, models or policy permissions.

## Architecture

### A — Input Router + Policy Gate

Source: `a_router/`.

`a_router.router.route_request()` validates `RequestContext`, normalizes input,
runs the prompt guard, extracts signals, applies deterministic policy and
returns `AResult`. Only `G_GENERAL_EDUCATION` sets `rag_allowed=True`.

The default offline path uses a deterministic regex prompt guard. The optional
`Qwen3GuardPromptInjectionGuard` adapter lazy-loads
`Qwen/Qwen3Guard-Gen-0.6B`; it reuses weights only while the same guard object
is alive, and dependency failure fails closed to `F_ROUTER_DEPENDENCY`.

### Query Expansion

Source: `query_expansion/`.

The v0.1 default is an identity deterministic expander: it preserves
`original_query` and emits exactly one identical `retrieval_query`. The
interface is injectable so a future approved rewriter can be tested without
changing the workflow contract.

### RAG / B — Retrieval, Contract Gate, Context Gate

The current implementation is in the numbered phase scripts:

- `01_build_documents.py`
- `02_similarity_retrieval.py`
- `03_reranker.py`
- `04_llm_judge.py`
- `05_hybrid.py`

The formal workflow boundary is now `b_context_gate/` and `rag/`. It defines
`CanonicalEvidence`, `CanonicalBInput` and `CanonicalBResult`, and adapters for
legacy `document_id`/`contexts`/`b_decision` phase outputs. The numbered scripts
remain research/experiment implementations and are not rewritten or moved.

`rag.TFDADrugSafetyRetriever` is the real corpus path: it loads the 129-record
TFDA `data/processed/langchain_documents.json`, keeps one processed record per
LangChain `Document`, and uses the existing
`HuggingFaceEmbeddings(intfloat/multilingual-e5-small)` plus
`InMemoryVectorStore`. It preserves `evidence_id`, source, date and metadata at
the B boundary. The index is lazy and injectable.

The unit/E2E contract tests still inject `FixtureRetriever` so they remain
fast and deterministic. `workflow.demo` defaults to the real TFDA retriever
and explicitly labels its deterministic B approval as a demo mode; it does not
silently turn retrieval into clinical approval. `FixtureRetriever` remains
available with `--retriever fixture`.

### C — Evidence-aware Generator (Canonical = v2)

Source: `c_generator/`.

C v2 is frozen as the formal workflow Generator contract:
`EvidenceAwareV2Answer` with `ANSWER`, `PARTIAL` and `INSUFFICIENT`,
`supported_claims`, `unsupported_requests` and `limitations`. The workflow
input is `CWorkflowInput` and the runtime adapter is in
`c_generator/workflow_adapter.py`.

C v1 is retained as legacy/experiment code and is not used by the formal
workflow. The existing live C v2 experiment runner remains available through
`v2_run_experiment.run_generator()`; the workflow can inject an existing
structured-output chain through `LangChainCV2Generator`. The default
`run_workflow()` contract path uses `DeterministicFixtureCGenerator`; the real
corpus CLI demo uses the same deterministic C fixture only as a clearly
labelled integration placeholder.

### D — Mandatory Output Gate

Source: `d_output_gate/`.

`d_output_gate.gate.run_output_gate()` is the only current D entry point. It
normalizes A/B/C payloads, validates schemas and evidence IDs, enforces A policy
and output red lines, invokes the demo semantic verifier, and returns only
`PASS` or `FALLBACK`.

### E — Observability / Evaluation Data Collection

Source: `e_observability/`.

E v0.1 provides:

- Pydantic `TraceEvent`, `EvaluationRecord` and metrics schemas.
- `TraceRecorder` with `span()` context manager and direct `record()` API.
- JSONL and in-memory sinks.
- Per-request counters and latency summaries.
- Failure and evaluation record helpers.
- Query/error/metadata secret redaction and optional query hashing.
- Agent action, bounded step/rewrite counters and termination metadata.
- Human-readable execution trajectory rendering from the same structured trace;
  it is enabled with `--show-trace` and does not control the workflow.

E does not modify prompts, policy, models or deployment; it cannot replace
A/B/C/D or decide a medical response. Sink errors are isolated from business
logic.

The workflow wraps every stage in E spans. Normal requests emit STARTED and
COMPLETED events for A, Query Expansion, RAG, B, C and D. Early exits emit
BLOCKED, INSUFFICIENT or FALLBACK plus a completed SYSTEM event. Dependency
exceptions emit ERROR events and return a fixed safe fallback.

## Safety Boundaries

- A is the policy authority; the Agent may not override A.
- `declared_role` never grants sensitive-data or tool permission.
- Only A-approved `G_GENERAL_EDUCATION` requests may enter general RAG.
- Evidence must be explicitly approved by B before C uses it.
- C may only make evidence-supported claims and must preserve evidence IDs.
- Every C candidate must pass through D before it is returned as an answer.
- Agent actions may not bypass D.
- E observes and collects data; it does not modify Policy, Prompt or Model.
- Acute/emergency thresholds in this demo are not formally clinically approved.

## Current Implementation Status

### A v0.1 — implemented

Input schemas, fixed labels, deterministic policy routes, prompt-injection
guard adapter, LangChain structured extractor adapter and tests are present.
TODO: formal clinical threshold approval and a validated semantic benchmark.

### B / RAG — canonical adapter plus research implementation

Retrieval, reranking, Contract Gate and context sufficiency/judge experiments
are present in phase scripts and run artifacts. The canonical B schema and
legacy adapters are now present. The default runnable gate/retriever are
fixture implementations; TODO: connect an approved live B implementation and
add its semantic evaluation.

### C v2 — canonical workflow contract

Evidence-aware v2 generation and evaluation runners are present. C v2 is the
canonical workflow contract. C v1 remains legacy/experiment only.

### D v0.1 — implemented

Adapter, schema/evidence/policy validation, demo semantic verifier and
`PASS`/`FALLBACK` behavior are present. TODO: independently evaluated semantic
verification; the current verifier is explicitly a demo component.

### E v0.1 — implemented

Request-scoped JSONL tracing, metrics, failure/evaluation data collection and
redaction are present. E is integrated into the LangGraph `run_workflow`,
including Agent decisions, rewrites and bounded termination.

### Agent v0.1 — implemented

`workflow.graph` is a LangGraph `StateGraph` with conditional edges for A,
B and Agent action routing. The Planner returns only the bounded
`ASK_USER`/`REWRITE_QUERY`/`FALLBACK` union. `REWRITE_QUERY` executes
Query Rewriter → RAG → B; `ASK_USER` returns `NEEDS_CLARIFICATION` and a new
user reply re-enters from A. The real Planner uses native
`langchain_openrouter.ChatOpenRouter` with
`deepseek/deepseek-v4-flash-0731` by default; use `--provider ollama` to
select the local adapter.

## Main Entry Points

| Area | Entry point |
| --- | --- |
| A | `tfda_context_gate.a_router.router.route_request()` |
| A demo | `python3 -m tfda_context_gate.a_router.demo --guard regex` |
| RAG/B phase 1 | `python3 00_download_and_inspect.py` |
| RAG/B phase 2 | `python3 01_build_documents.py`, `python3 02_similarity_retrieval.py` |
| Real TFDA RAG demo | `python3 -m tfda_context_gate.rag.demo --all` |
| Agent v0.1 offline demo | `python3 -m tfda_context_gate.agent.demo --planner fixture --retriever fixture --show-trace` |
| Agent v0.1 OpenRouter Planner | `python3 -m tfda_context_gate.agent.demo --planner llm --provider openrouter --retriever fixture` |
| Agent v0.1 local Ollama Planner | `python3 -m tfda_context_gate.agent.demo --planner llm --provider ollama --retriever fixture` |
| RAG/B phase 3–5 | `python3 03_reranker.py`, `python3 04_llm_judge.py`, `python3 05_hybrid.py` |
| C fixture adapter | `tfda_context_gate.c_generator.b_to_c_interface.build_interface()` |
| C generator | `tfda_context_gate.c_generator.generator.run_generators()` |
| C v2 workflow adapter | `tfda_context_gate.c_generator.workflow_adapter.LangChainCV2Generator` |
| D | `tfda_context_gate.d_output_gate.gate.run_output_gate()` |
| E demo | `python3 -m tfda_context_gate.e_observability.demo --log-path /tmp/tfda-e.jsonl` |
| A–E workflow | `tfda_context_gate.workflow.runner.run_workflow()` |

The numbered scripts import `run_config` and `rate_limiter` as script-local
modules. Run them from this directory; they have not been moved.

## Data Contracts

- A input: `a_router.schemas.RequestContext`.
- A signals: `a_router.schemas.RouterSignals`.
- A output: `a_router.schemas.AResult`.
- Query Expansion input/output: `query_expansion.schemas.QueryExpansionInput`
  and `QueryExpansionResult`.
- RAG output: `rag.schemas.RAGResult`.
- B input/output: `b_context_gate.schemas.CanonicalBInput` and
  `CanonicalBResult`.
- C v1: `c_generator.schemas.EvidenceAwareAnswer`.
- C v2 canonical workflow input: `c_generator.workflow_adapter.CWorkflowInput`.
- C v2 output: `c_generator.schemas.EvidenceAwareV2Answer`.
- D normalized input: `d_output_gate.schemas.OutputGateRequest`.
- D output: `d_output_gate.schemas.OutputGateResult`.
- E trace: `e_observability.schemas.TraceEvent`.
- E evaluation: `e_observability.schemas.EvaluationRecord`.

The B and RAG adapters handle current naming differences
(`b_decision`/`decision`, `contexts`/`evidence`, `document_id`/`evidence_id`).
The C v2 adapter converts the canonical workflow input to the existing v2
experiment prompt shape only at the live-chain boundary. D receives only the
canonical B result and C v2 result.

## Running Tests

From the repository root (`langchain_1.2/`):

```bash
python3 -m pytest -q
```

The last verified contract result is **53 passed, 7 skipped**: the original
A/D/E/workflow tests remain green, the two non-model TFDA boundary tests pass,
and seven embedding smoke cases are skipped when the active interpreter does
not have the optional HuggingFace stack. The real vector smoke run was also
executed with the repository `.venv` and all seven retrieval cases matched
their expected TFDA evidence.

## Running the E Demo

```bash
python3 -m tfda_context_gate.e_observability.demo \
  --log-path /tmp/tfda-e-demo.jsonl
```

The command prints a request snapshot and appends structured trace/evaluation
records to the JSONL file. The demo emits representative A/RAG/B/C/D events;
it does not claim to be an end-to-end clinical workflow.

## Running the A–E E2E Baseline

```bash
python3 -m tfda_context_gate.workflow.demo \
  --log-path /tmp/tfda-a-e-workflow.jsonl
```

With the default CLI options this runs A → identity Query Expansion → real
TFDA vector RAG → deterministic all-retrieved B demo → deterministic C v2
fixture → D. It is a corpus integration demo, not a production clinical
workflow. Use `--retriever fixture` for the fully offline contract path. To
use a live C v2 chain, inject `LangChainCV2Generator` in Python; the runner
never creates an external model or Agent implicitly.

To run the real TFDA corpus path with the role-based Patient / Healthcare
Professional / Caregiver cases:

```bash
python3 -m tfda_context_gate.rag.demo --all --top-k 5
python3 -m tfda_context_gate.workflow.demo \
  --retriever real \
  --case P1 \
  --log-path /tmp/tfda-real-workflow.jsonl
```

P1/P2/H1/H2/H3/C1/C2 are real retrieval cases; P3 is an A medication
boundary case and C3 is a future `ASK_USER` clarification candidate. Role
changes presentation intent only; it does not change the corpus, evidence
truth, A permission, B boundary or D validation.

## Running Live RAG/C Experiments

Install `tfda_context_gate/requirements.txt`, provide the required model/API
configuration, and run the numbered scripts from `tfda_context_gate/`. Live
experiments may download embedding/reranker models and call OpenRouter. Do not
put API keys in source, query text, JSON fixtures or logs; use environment or a
local untracked `.env` according to your privacy policy.

## Project Structure

```text
tfda_context_gate/
├── a_router/             # A v0.1 formal module
├── b_context_gate/       # canonical B v0.1 schema, adapter and demo gate
├── c_generator/          # C generator and evaluation experiments
├── d_output_gate/        # D v0.1 formal module
├── e_observability/      # E v0.1 trace, metrics and evaluation layer
├── query_expansion/      # deterministic Query Expansion interface
├── rag/                  # retrieval result and legacy adapter boundary
├── workflow/             # deterministic A–E runner and fixed fallbacks
├── tests/                # unit, contract and E2E tests
├── data/                 # current raw/processed corpus
├── runs/                 # isolated experiment runs
├── results/              # generated phase results and fixtures
├── reports/              # research reports
├── deliverables/         # archived/staged deliverables at repository level
├── experiments/          # index for phase scripts; source paths preserved
├── fixtures/             # index for fixture provenance; files not moved yet
├── examples/             # runnable-example index
├── 00_*.py ... 05_*.py   # current phase scripts, intentionally not moved
├── ARCHITECTURE_AUDIT.md
├── CURRENT_ARCHITECTURE.md
└── README.md
```

The numbered research scripts remain in place for reproducibility. Existing
research files and generated results were not deleted or moved in this pass.

## Known Limitations

- This is not a formal clinical system.
- Emergency thresholds need formal clinical approval.
- D's semantic verifier is a demo heuristic/test boundary, not a medical
  verifier.
- The Qwen3Guard prompt guard needs a Chinese medical-domain benchmark.
- `run_workflow()` keeps fixture defaults for fast contract tests. The
  `workflow.demo --retriever real` path uses the real TFDA corpus, but B and C
  remain deterministic demo components and are not clinical adjudication or a
  production generator.
- The live phase scripts are not yet wired into the workflow runner.
- C v1 remains in the repository as legacy/experiment code but is excluded from
  the formal workflow.
- E JSONL persistence is a demo sink; production needs retention, access
  control, encryption and PHI handling policy.
- `declared_role` is not identity verification.
- Live model/API runs require external dependencies and credentials.
- This directory is not currently a git repository, so historical diffs and
  uncommitted-change protection cannot be verified here.

## Agent v0.1 LangGraph Flow

Baseline graph:

```text
A -> Query Expansion -> RAG -> B -> C v2 -> D
```

Bounded Agent branch when B is insufficient:

```text
B insufficient
  -> Agent Planner
       -> REWRITE_QUERY -> Query Rewriter -> RAG -> B
       -> ASK_USER -> NEEDS_CLARIFICATION -> END
       -> FALLBACK -> END
  -> C
  -> D
```

The Agent does not replace A/B/C/D, override A policy, approve evidence, bypass
D, or modify limits. The three cases in `agent_demo_cases.json` cover
`ASK_USER`, meaning-preserving `REWRITE_QUERY`, bounded `FALLBACK`, and two
prompt-injection regressions.

Run the non-model checks with:

```bash
python3 -m pytest -q tfda_context_gate/tests/test_agent_demo_cases.py tfda_context_gate/tests/test_agent_runtime.py
```
