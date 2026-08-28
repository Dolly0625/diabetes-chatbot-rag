# Current Architecture — Coding Agent Source of Truth

Last verified: 2026-08-21

This file describes the code that exists now. Read it before modifying this
project. When this file conflicts with an old report or generated artifact,
prefer the executable source and tests, then update this file deliberately.

## Source of Truth

Use this precedence order:

1. Executable source under `a_router/`, `b_context_gate/`,
   `query_expansion/`, `rag/`, `c_generator/`, `d_output_gate/`, `workflow/`
   and `e_observability/`.
2. Automated tests under `tests/`.
3. This file and `ARCHITECTURE_AUDIT.md`.
4. Research reports, run artifacts and staged deliverables.

The current directory has no `.git` metadata. Do not assume that a historical
diff or commit can be inspected; preserve existing files and avoid destructive
commands.

## Current Modules

### A — `a_router/`

Responsibilities: input validation, prompt-injection guard, semantic signal
extraction and deterministic policy routing. A does not generate a medical
answer.

Important files:

- `schemas.py`: `RequestContext`, `RouterSignals`, `AResult`.
- `labels.py`: declared role, language, intent, risk, route and reason enums.
- `guard.py`: deterministic guard and lazy Qwen3Guard adapter.
- `rules.py`: normalization and deterministic signal extraction.
- `policy.py`: deterministic `policy_gate`.
- `router.py`: `route_request`, LangChain extractor adapter and `run_a` alias.

### Query Expansion — `query_expansion/`

`IdentityQueryExpander` is the deterministic v0.1 implementation. Its contract
preserves `original_query` and emits exactly one identical retrieval query.
`QueryExpansionInput` is built from A through `query_expansion.adapters`.

### RAG/B — `rag/` and `b_context_gate/`

The formal workflow boundary is canonicalized in `rag/` and
`b_context_gate/`. `rag.tfda_retriever.TFDADrugSafetyRetriever` loads the
processed 129-record TFDA corpus and performs lazy HuggingFace
embedding/InMemoryVectorStore retrieval while preserving source, date and
metadata. Retrieval and context evaluation also remain available in the
numbered phase scripts; those scripts are not rewritten.
`b_context_gate.adapters` normalizes legacy identifiers and result names.
`DeterministicContextGate` is still a MOCK/FIXTURE gate for the offline
baseline; its explicitly named `all_retrieved` mode is used only by the real
corpus demo and is not a clinical context judge.

### C — `c_generator/` — Canonical Generator = v2

Responsibilities: experiment-driven evidence-aware generation and evaluation.

- v1: `EvidenceAwareAnswer` with `claims` and `ANSWER`/`INSUFFICIENT`.
- v2: `EvidenceAwareV2Answer` with `supported_claims`,
  `unsupported_requests` and `ANSWER`/`PARTIAL`/`INSUFFICIENT`.
- `build_interface` creates B-to-C fixture cases.
- `run_generators` and `invoke_one` run C experiments.
- `v2_run_experiment.run_generator` is the existing live experiment entry.
- `workflow_adapter.CWorkflowInput` is the canonical workflow input.
- `workflow_adapter.LangChainCV2Generator` adapts an injected structured chain.
- `workflow_adapter.DeterministicFixtureCGenerator` is the offline E2E mock.

C v1 remains legacy/experiment code and is not used by `workflow.run_workflow`.
Do not silently collapse v1 and v2.

### D — `d_output_gate/`

Responsibilities: mandatory final validation and safe fallback.

`run_output_gate(payload, verifier=..., policy_rules=..., fallback_response=...)`
is the stable entry. D adapts current A/B/C fixture shapes, validates policy,
evidence and candidate schemas, checks approved evidence IDs, checks output red
lines, invokes a pluggable semantic verifier and returns `OutputGateResult`.

The only D decisions are `PASS` and `FALLBACK`.

### E — `e_observability/`

Responsibilities: structured logging, request trace, metrics, failure-analysis
records and evaluation data collection.

Public API:

```python
from tfda_context_gate.e_observability import JsonlTraceSink, TraceRecorder

with TraceRecorder(
    "request-001",
    declared_role="PATIENT",
    original_query="一般衛教問題",
    sink=JsonlTraceSink("/tmp/tfda-trace.jsonl"),
) as trace:
    with trace.span("A", "input_router") as span:
        result = run_a(...)
        span.set(
            router_status=result.router_status.value,
            reason_codes=[code.value for code in result.reason_codes],
            rag_allowed=result.rag_allowed,
        )
    trace.record_evaluation(actual_decision="ANSWER", outcome="UNLABELED_DEMO")
```

Use `record_failure` for B insufficient, D fallback and dependency failures.
Use `span.set(status="BLOCKED", ...)` or direct `record(..., "BLOCKED", ...)`
for A blocks. The recorder is observational and fail-open if a sink fails.

`workflow.run_workflow` now wraps A, Query Expansion, RAG, B, C v2 and D with
E spans. E remains observational and cannot change a stage's safety result.

### Workflow — `workflow/`

`workflow.runner.run_workflow()` compiles and invokes the LangGraph `StateGraph`
in `workflow.graph`. The graph contains A, Query Expansion, RAG, B, an
optional bounded Agent Planner/recovery loop, C v2 and D. With no injected
Planner it preserves the deterministic baseline; with a Planner, only
`INSUFFICIENT` B results enter Agent recovery.

### Agent v0.1 runtime and evaluation cases

`agent_demo_cases.json`, `agent_demo_case_schema.py` and
`AGENT_V0_1_CASE_DESIGN.md` define real-TFDA ASK_USER, REWRITE_QUERY and
bounded FALLBACK cases plus prompt-injection regressions. They are evaluation
ground truth and test fixtures. Runtime contracts live under `agent/`; the
three-case command is `python -m tfda_context_gate.agent.demo`. `--planner
llm` uses native `langchain_openrouter.ChatOpenRouter` with
`deepseek/deepseek-v4-flash-0731` by default; use `--provider ollama` to
select the local adapter.

## Current Contracts

### A

Input: `a_router.schemas.RequestContext`:

- `request_id`
- `schema_version`
- `user_raw_input`
- `declared_role`
- `language`

Output: `a_router.schemas.AResult`:

- request identity and normalized input metadata
- `intent_tags`, `risk_flags`, `context_modifiers`
- exactly one `router_status`
- `reason_codes`
- `rag_allowed`

`rag_allowed` is true only for `G_GENERAL_EDUCATION`.

### Query Expansion

Input: `QueryExpansionInput` with `request_id`, `original_query`, A route,
intent tags, declared role and language.

Output: `QueryExpansionResult` with unchanged `original_query`,
`retrieval_queries[]` and strategy.

### RAG

Output: `RAGResult` with `original_query`, `retrieval_queries[]`, normalized
`CanonicalEvidence[]` and optional retrieval latency.

### B

Canonical input: `CanonicalBInput`:

- `request_id`
- `original_query`
- `retrieval_queries[]`
- `evidence[]`

Canonical output: `CanonicalBResult`:

- `request_id`
- `decision`: `PASS` / `INSUFFICIENT` / `UNSAFE` / `REVIEW` / `FALLBACK`
- `approved_evidence_ids[]`
- `evidence[]`
- `reason_codes[]`
- `retrieval_feedback`
- `relevance`, `sufficiency`, `conflict`, `safety`

`CanonicalEvidence.evidence_id` is the only identifier used by the formal
workflow. Legacy `document_id`/`chunk_id` values are converted at the adapter.

Do not treat retrieved context as B-approved evidence without an explicit
approval field.

### C v2

Canonical workflow input: `CWorkflowInput` with `request_id`, `original_query`,
`b_decision`, `approved_evidence_ids[]` and `evidence[]`.

Canonical output: `EvidenceAwareV2Answer`:

- `decision`: `ANSWER` / `PARTIAL` / `INSUFFICIENT`
- `answer`
- `supported_claims[]`: `claim_id`, `claim`, `evidence_ids[]`
- `unsupported_requests[]`
- `limitations[]`

Legacy v1 uses `claims[]` and is retained only for old experiments/D adapter
compatibility.

### D

Normalized input: `OutputGateRequest` containing policy, evidence set and
candidate response. Output: `OutputGateResult` with `PASS`/`FALLBACK`, failure
type, reason codes, invalid evidence IDs and final response.

### E

`TraceEvent` contains request/trace identity, execution timing, compact RAG
provenance, B decisions and optional Agent/recovery fields. `EvaluationRecord`
stores later offline evaluation labels/results.
`MetricsSnapshot` provides in-process counts and per-component latency summaries.
Future Agent fields (`agent_action`, `actions_taken`, `step_count`,
`retry_count`, `tool_name`, `termination_reason`) remain backward-compatible
optional fields. `format_trace_trajectory()` renders these records for the
CLI without participating in graph execution.

### Workflow result

`WorkflowResult` contains the status, fixed final/fallback response, intermediate
A/Query/RAG/B/C/D records and the complete E trace snapshot.

## Current Entry Functions

| Component | Function |
| --- | --- |
| A | `tfda_context_gate.a_router.router.route_request` |
| A alias | `tfda_context_gate.a_router.router.run_a` |
| Phase 2 retrieval | `02_similarity_retrieval.run_query` |
| Phase 3 reranking | `03_reranker.run_one_query` |
| Phase 5 hybrid | `05_hybrid.variant_result`, module `main` |
| B → C fixture adapter | `c_generator.b_to_c_interface.build_interface` |
| C | `c_generator.generator.run_generators`, `invoke_one` |
| C v2 live experiment | `c_generator.v2_run_experiment.run_generator` |
| C v2 workflow input | `c_generator.workflow_adapter.CWorkflowInput` |
| Query Expansion | `query_expansion.IdentityQueryExpander.expand` |
| RAG fixture | `rag.retriever.FixtureRetriever.retrieve` |
| Real TFDA RAG | `rag.tfda_retriever.TFDADrugSafetyRetriever.retrieve` |
| B canonical gate | `b_context_gate.gate.DeterministicContextGate.evaluate` |
| D | `d_output_gate.gate.run_output_gate` |
| E | `e_observability.tracer.TraceRecorder` |
| A–E baseline | `workflow.runner.run_workflow` |

`workflow.graph.build_workflow_graph` is the LangGraph entry point. Conditional
edges handle A boundary, B PASS/INSUFFICIENT/non-recoverable routing and Agent
action routing. `REWRITE_QUERY` loops to Query Rewriter → RAG → B;
`ASK_USER` ends with `NEEDS_CLARIFICATION`; system limits force `FALLBACK`.

## Hard Boundaries

- A policy is authoritative and cannot be overridden by C, D adapters or a
  future Agent.
- Prompt guard failure and malformed semantic extraction fail closed.
- Only `G_GENERAL_EDUCATION` may enter general RAG.
- B approval is explicit; retrieval alone is not approval.
- C claims must cite evidence IDs.
- C v2 may cite only B-approved evidence IDs.
- D is mandatory before returning a generated answer.
- Tool/Agent code may not bypass D.
- E cannot modify prompt, policy, model, deployment or medical answer.
- A declared role is not identity verification or authorization.
- Agent cannot bypass A/B/C/D or modify graph limits.
- B non-PASS ends the baseline with deterministic fallback; only recoverable
  `INSUFFICIENT` enters the bounded Agent branch.

## Mock, Demo and Non-production Components

- `RuleBasedPromptInjectionGuard`: deterministic offline guard/fallback.
- `RuleBasedSignalExtractor`: deterministic demo semantic extractor; not a
  clinical triage engine.
- `Qwen3GuardPromptInjectionGuard`: optional local model adapter; not a policy
  authority.
- `04_llm_judge.py` / `05_hybrid.py`: experiment judges, not approved clinical
  context adjudicators.
- `HeuristicSemanticVerifier`: D demo verifier, not formal medical verification.
- `FixtureRetriever`, `DeterministicContextGate` and
  `DeterministicFixtureCGenerator`: offline MOCK/FIXTURE components for E2E
  contract validation, not production retrieval/judging/generation.
- `JsonlTraceSink`: E demo persistence; production needs privacy/retention and
  access controls.

## Tests

Verified command:

```bash
python3 -m pytest -q
```

Last verified result: **68 passed, 10 skipped**.

- A: 17 tests.
- D: 11 tests.
- E: 8 tests.
- Workflow integration: 15 tests.
- Live B/C implementations: no independent live-component tests; canonical
  adapters and fixture workflow contracts are covered by integration tests.

## Known TODO

1. Connect an approved live RAG/B implementation to the canonical adapters.
2. Replace the fixture C v2 generator with an approved injected live chain for
   a separate live baseline; keep the fixture path for contract tests.
3. Add semantic B/C live-component tests and an offline corpus fixture policy.
4. Introduce a central model/config factory only when live components are
   promoted beyond experiments.
5. Evaluate Qwen3Guard caching/thread safety for production serving.
6. Define PHI redaction, retention, encryption and access-control policy for E.
7. Evaluate an independent D semantic verifier.
8. Keep all archived deliverables and research outputs until provenance and
    reproducibility requirements are resolved.

## Agent v0.1 bounded path

```text
B insufficient
  -> Agent Planner: ASK_USER / REWRITE_QUERY / FALLBACK
  -> REWRITE_QUERY -> Query Rewriter -> RAG -> B
  -> C
  -> D
```

The Agent owns bounded recovery selection only. A remains policy authority, B
owns evidence approval, C owns grounded candidate generation, D owns final
output acceptance, and E observes every graph step including Agent metadata.

## Architecture Decisions

- Preserve A/B/C/D code and behavior; use adapters at boundaries.
- Implement E with stdlib + existing Pydantic only; JSONL is the first sink so
  it can later be replaced by SQLite/OpenTelemetry/etc. without changing event
  producers.
- Redact common secrets before persistence and retain only an optional query
  hash for correlation.
- Do not move numbered phase scripts in this pass because their script-local
  imports and generated artifact paths are part of current reproducibility.
- Do not delete duplicate/staged artifacts; classify them and leave removal for
  an explicit cleanup pass.
