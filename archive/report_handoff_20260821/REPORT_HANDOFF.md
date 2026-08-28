# TFDA Context Gate — REPORT_HANDOFF

## 0. Package purpose and snapshot rule

This package is a factual handoff for a separate AI to write the full
technical report. It is a snapshot of the files and run artifacts that exist
in the workspace on 2026-08-21. It does not introduce production behavior.

The copied Python files are source snapshots, not a second installable
package. Their imports still refer to the original `tfda_context_gate`
package. The original sources are the executable source of truth; the copies
under this directory are evidence for report writing.

No `.env`, API key, token, or secret is copied. The workspace has no Git
metadata, so no commit or diff claim can be made.

For exact source, see the files under `architecture/`, `agent/`,
`contracts/`, and `rag/`. For run evidence, see `traces/`; for tests, see
`tests/`; for known limits, see `limitations/LIMITATIONS.md` and the root
`REAL_FIXTURE_MATRIX.md`.

## 1. Final architecture

The final v0.1 workflow is a safety-oriented A/B/C/D pipeline with E as a
cross-cutting observability layer and a bounded Agent recovery branch:

```text
User request
    |
    v
A: Input Router + Policy Gate
    | only G_GENERAL_EDUCATION continues
    v
Query Expansion (v0.1 identity expander)
    |
    v
RAG Retriever -> Contract normalization -> B: Context Gate
                                      |
                         PASS --------+-------- INSUFFICIENT
                          |                       |
                          v                       v
                         C v2                 Agent Planner
                          |                 /       |        \
                          v             ASK_USER  REWRITE   FALLBACK
                         D                  |         |        |
                          |                END   Query Expansion  END
                          v                              ^
                 PASS answer / FALLBACK                 |
                                                        +-- RAG -> B

E: Trace / metrics / evaluation records wrap the stages and do not decide
the route or answer.
```

`workflow.runner.run_workflow()` compiles and invokes the LangGraph
`StateGraph` in `workflow.graph`. Without an injected Planner, the original
deterministic baseline is preserved: B `INSUFFICIENT` ends in fallback. With a
Planner and Rewriter, only recoverable B `INSUFFICIENT` enters the bounded
Agent branch.

The Agent Planner is an LLM decision component, not the orchestrator. The
Planner emits one validated action from a discriminated structured-output
union. LangGraph owns node execution, conditional routing, limits, and loop
termination.

## 2. A/B/C/D/E responsibilities

| Component | Responsibility | Current implementation status |
|---|---|---|
| A | Validate request shape, run prompt-injection guard, extract semantic/risk signals, apply deterministic policy, and decide whether general RAG is allowed. | Implemented; policy boundary is deterministic. Optional Qwen3Guard and LangChain signal-extractor adapters exist. |
| B | Receive normalized RAG evidence, evaluate context status, expose approved evidence and retrieval feedback, and report neutral missing-information observations. | Canonical contract exists. The workflow demo uses `DemoContextGate`/`DeterministicContextGate` fixtures; no production semantic B judge is wired into the Agent demo. |
| C | Generate an evidence-aware v2 candidate with claims tied to evidence IDs. | Canonical v2 contract and LangChain adapter exist; the main workflow/demo path uses `DeterministicFixtureCGenerator`. |
| D | Validate policy, candidate shape, evidence IDs, red lines, and semantic-verifier result. Return only `PASS` or `FALLBACK`. | Implemented as a deterministic gate with a heuristic demo verifier and injectable verifier. It is not a clinically validated semantic judge. |
| E | Record structured stage events, latency/counters, failure/evaluation records, redacted query data, and human-readable trajectory output. | Implemented and integrated around A, Query Expansion, RAG, B, Agent, C, and D. E is observational and fail-open. |

The Agent cannot override A, approve B evidence, bypass C/D, change graph
limits, or directly answer the medical question.

## 3. LangGraph graph topology

Source: `architecture/workflow/graph.py` (copied from
`tfda_context_gate/workflow/graph.py`).

Nodes:

- `A`: input router and policy gate.
- `QUERY_EXPANSION`: identity expansion in v0.1; preserves the original query.
- `RAG`: retriever call and evidence normalization.
- `B`: context-gate evaluation.
- `AGENT_PLANNER`: structured Agent action decision after recoverable B insufficiency.
- `ASK_USER`: deterministic Question Builder; ends with `NEEDS_CLARIFICATION`.
- `QUERY_REWRITER`: meaning-preserving LLM rewrite; loops back to Query Expansion.
- `C`: evidence-aware v2 generator.
- `D`: mandatory output gate.

Edges and conditional edges:

```text
START -> A
A -- rag_allowed=True --> QUERY_EXPANSION -> RAG -> B
A -- rag_allowed=False --> END
B -- PASS --> C -> D -> END
B -- INSUFFICIENT and Planner configured --> AGENT_PLANNER
B -- other non-recoverable result --> END
AGENT_PLANNER -- ASK_USER --> ASK_USER -> END
AGENT_PLANNER -- REWRITE_QUERY --> QUERY_REWRITER -> QUERY_EXPANSION
AGENT_PLANNER -- FALLBACK --> END
```

`ASK_USER` intentionally does not use LangGraph interrupt/checkpointer in this
v0.1 package. The command/demo simulates the user reply by starting a new
workflow invocation from A. There is no persistent thread resume or
long-term memory.

## 4. WorkflowState

`WorkflowState` is the internal `TypedDict` in `workflow.graph`. It is not
passed wholesale to the Planner. Its fields are:

| Field | Role |
|---|---|
| `request_context`, `request_id` | Validated request and identity. |
| `original_query`, `current_query` | Immutable user query and current retrieval query. |
| `a_result`, `query_expansion`, `rag_result`, `b_result`, `c_result`, `d_result` | Stage outputs. |
| `trace` | Request-scoped E `TraceRecorder`. |
| `agent_planner`, `query_rewriter`, `agent_limits` | Runtime dependencies/configuration held by the graph closure/state. |
| `agent_decision` | Already validated Agent decision. |
| `previous_attempts`, `pending_agent_action`, `actions_taken` | System-owned recovery history. |
| `agent_steps`, `rewrite_count`, `clarification_count` | Bounded counters. |
| `retrieval_attempt`, `b_attempt` | Retrieval/B attempt counters. |
| `agent_reason_code`, `question`, `status`, `final_response` | Result and display state. |
| `fallback_reason`, `termination_reason` | Safe termination metadata. |

The runner initializes the counters at zero. Graph nodes update them; the
Planner cannot set them.

## 5. AgentDecisionContext

Source: `agent/context.py` and `agent/schemas.py`.

`build_agent_decision_context()` is the only projection into the Planner. It
contains:

- `original_query`
- `current_query`
- `b_decision`
- up to eight B `b_reason_codes`
- neutral B observation `identified_missing_information` (up to eight)
- limited retrieval feedback (`retrieval_queries`, `duplicate_ids`, or
  `retrieval_status` when present)
- up to five compact `EvidenceSummary` items
- the last two system-written `AgentAttempt` records

Evidence is projected to ID, rank, score, ingredient, title, source, date, and
a short snippet. Raw documents are not passed into the Planner. The neutral
missing-information list is an observation; it does not contain a recommended
Agent action or control instruction.

## 6. AgentDecision schema

Source: `agent/schemas.py`.

The action is a discriminated union on `action`:

```text
AskUserDecision {
  action: "ASK_USER"
  reason_code: AgentReasonCode
  missing_information: 1..4 strings
}

RewriteQueryDecision {
  action: "REWRITE_QUERY"
  reason_code: AgentReasonCode
}

FallbackDecision {
  action: "FALLBACK"
  reason_code: AgentReasonCode
}
```

Allowed reason codes are `MISSING_REQUIRED_CONTEXT`,
`QUERY_FORMULATION_NEEDS_REWRITE`, `RECOVERY_EXHAUSTED`, `LIMIT_EXCEEDED`,
`PLANNER_FAILURE`, and `REWRITER_FAILURE`. The Planner cannot return a medical
answer, evidence approval, node name, tool call, limit, or arbitrary field.

`AgentDecisionStructuredOutput` is the Pydantic root wrapper used for provider
structured-output APIs. `LangChainAgentPlanner` validates the provider result
again at the Agent boundary.

## 7. Agent Planner prompt and policy

The authoritative prompt is `agent/planner.py` and is copied verbatim. Its
policy is:

1. `ASK_USER` is allowed only when a key fact needed for a reliable answer is
   absent, must come from the user, and rewriting cannot derive it.
2. A non-empty `identified_missing_information` list is the only authoritative
   source for an ASK_USER gap. Generic B insufficiency, low scores, mixed top-k
   results, colloquial wording, or a request that could be more specific do not
   justify inventing an ASK_USER gap.
3. If that list is empty and core user facts are present, prefer
   `REWRITE_QUERY` for a first retrieval recovery.
4. `REWRITE_QUERY` must preserve named medicines, symptoms, scope, and intent;
   it cannot invent medication, class, diagnosis, severity, treatment change,
   or turn a retrieved candidate into the user’s actual medication.
5. `FALLBACK` is selected when no reasonable recovery remains or a previous
   recovery is still insufficient.

The real Cloud path uses native `langchain_openrouter.ChatOpenRouter` with the
configured model `deepseek/deepseek-v4-flash-0731`. The OpenRouter adapter sets
`reasoning={"effort": "none"}` for this bounded structured decision, uses
`AGENT_REQUEST_TIMEOUT` (default 60 seconds), `AGENT_MAX_TOKENS` (default 400),
and `max_retries=0`. Exact provider configuration is in `agent/openrouter.py`;
secrets are intentionally not copied.

The Planner’s output is real Cloud LLM output in the final Cloud traces. The
fixture Planner remains only for offline tests/demo mode and is explicitly
labelled in `REAL_FIXTURE_MATRIX.md`.

## 8. Query Rewriter design

Source: `agent/rewriter.py`.

The Rewriter receives `original_query` and `current_query`, returns only the
structured `RewrittenQuery.rewritten_query`, and never answers the question.
Its prompt requires meaning preservation and permits terminology
normalization, for example mapping colloquial `下體` to
`生殖器或會陰部` when appropriate.

`validate_meaning_preserving_rewrite()` performs a narrow post-LLM check:

- user-provided alphanumeric/named tokens cannot be dropped;
- obvious unprovided medical facts such as pain, redness, fever, infection,
  shock, coma, stopping medicine, or changing dose cannot be added.

The graph validates a non-empty rewrite, runs this check, increments
`rewrite_count`, and returns to Query Expansion → RAG → B. The production
workflow has a LangChain structured-output adapter; tests/offline demo use a
deterministic mapping fixture.

## 9. ASK_USER and Question Builder

The LLM chooses `ASK_USER`; the graph, not the LLM, builds the question. The
Question Builder in `workflow.graph` maps:

- `medication_class` or `drug_type` → `請問家人目前使用的是哪一類糖尿病藥物？`
- `medicine_name` → asks for medicine name or ingredient
- `symptom` → asks for concrete symptoms
- unknown fields → a generic bounded-information request

The node records a structured `ASK_USER/question_builder` event, sets
`NEEDS_CLARIFICATION`, and ends. The demo then demonstrates re-entry by
starting a new request from A with the simulated user reply. The graph does
not silently derive a medication class from top-k retrieval.

## 10. Bounded limits

Default `AgentLimits` in `agent/config.py`:

| Limit | Default | Enforcement |
|---|---:|---|
| `max_agent_steps` | 2 | Planner node fails closed to fallback when reached. |
| `max_rewrites` | 1 | A second rewrite selection is converted to `LIMIT_EXCEEDED` fallback. |
| `max_clarifications` | 1 | A second ASK_USER selection is converted to bounded fallback. |
| Planner evidence summaries | 5 | Context projection only. |
| Planner previous attempts | 2 | Context projection only. |
| Planner B reason codes | 8 | Context projection only. |
| ASK_USER missing fields | 1–4 | Pydantic decision contract. |

There is no unbounded Agent loop. A successful B `PASS` exits the recovery
branch to C/D; a failed recovery either asks once, rewrites once, or falls
back.

## 11. E Trace / Observability

E source snapshots are under `contracts/e_observability/`.

`TraceRecorder` creates request-scoped structured events and wraps each graph
stage with STARTED plus terminal events. `TraceEvent` records component,
node, status, timing, route/decision, evidence IDs, B fields, Agent action and
counter fields, rewrite/question fields, model name, termination reason, and
sanitized error metadata. `EvaluationRecord` stores expected/actual decision
and outcome metadata. `MetricsSnapshot` stores in-process counts and latency
summaries.

`JsonlTraceSink` writes append-only JSONL; `InMemoryTraceSink` is used by
tests/callers. The privacy layer redacts common credentials and stores an
optional query hash. The trajectory formatter is display-only: it renders
events, including RAG provenance and Agent planner context, but never controls
graph execution and never exposes hidden model reasoning.

The JSONL files in `traces/` contain the complete demo record emitted by the
Agent demo: case label, status, final response, formatted trajectory, and the
full trace snapshot.

## 12. TFDA dataset 9573 and 129-record corpus

The processed corpus is `rag/langchain_documents.json`, copied from
`tfda_context_gate/data/processed/langchain_documents.json`.

The audit establishes:

- source dataset reference: TFDA government open-data dataset 9573;
- current processed corpus: a top-level JSON list of 129 records;
- each record is one TFDA drug-safety risk-communication record, not a newly
  chunked synthetic document;
- each row includes `id`, `page_content`, `metadata`, and `raw_record`;
- IDs are unique and the audit found no malformed row, empty content, or
  missing drug ingredient metadata;
- provenance fields include `document_id`, `row_index`, `source_dataset`,
  `raw_source_file`, publication date, and drug ingredient.

See `rag/REAL_TFDA_DATASET_AUDIT.md` for the original audit text and the
phase reports for retrieval experiments. The current workspace did not contain
`/mnt/data/langchain_documents.json`; the retriever resolves an explicit path,
`TFDA_DOCUMENTS_PATH`, that path, then the repository copy.

## 13. Embedding, vector store, and retriever

`rag/tfda_retriever.py` implements `TFDADrugSafetyRetriever`:

1. lazily load and validate the 129 processed rows;
2. preserve one processed row as one LangChain `Document` (no chunking);
3. create `HuggingFaceEmbeddings` using
   `intfloat/multilingual-e5-small` with normalized passage/query prompts;
4. add documents to LangChain `InMemoryVectorStore`;
5. search each retrieval query, merge duplicate evidence IDs by the best score,
   sort, and return top-k `CanonicalEvidence` with source/date/metadata.

The real retriever is injectable. The Agent Cloud trace deliberately uses
`--retriever fixture` to avoid spending the demo on local embedding/model
startup. The real corpus path is exercised by the retrieval tests and
`test_agent_demo_cases.py` validation cases; this distinction is material and
is recorded in `REAL_FIXTURE_MATRIX.md`.

## 14. Three Agent demo cases

Case ground truth is in `demo_cases/agent_demo_cases.json` and the design
explanation is in `demo_cases/AGENT_V0_1_CASE_DESIGN.md`.

| Case | Natural-language question | Intended Agent action | Intended outcome |
|---|---|---|---|
| `AG-ASK-001` | `我家人吃糖尿病藥後腳怪怪的，我要注意什麼？` | `ASK_USER` because neutral B observation says `medication_class` is missing. | Ask which diabetes-drug class; a new invocation with `SGLT2 抑制劑` demonstrates re-entry. |
| `AG-REWRITE-001` | `吃 SGLT2 下體不舒服要注意什麼？` | `REWRITE_QUERY`. | Normalize colloquial wording and re-run RAG/B. |
| `AG-FALLBACK-001` | `糖尿病患者使用 Semaglutide 後視力模糊風險有哪些？` | One conservative rewrite, then `FALLBACK` when evidence remains insufficient. | Bounded termination; no infinite retry and no invented evidence. |

The case file also contains PI-1 and PI-2 prompt-injection regression cases.
Those are expected to stop at A with no RAG or Agent action.

Important execution distinction: the Cloud JSONL in `traces/` is a Cloud
Planner/Rewriter run over the explicit Agent demo fixtures. It is not a claim
that B/C/D in that trace are real semantic implementations. The real TFDA
retrieval rank checks are separately preserved in the tests/case design.

## 15. ASK_USER improvement before and after

Before the neutral missing-information signal and stronger Planner policy:

- `traces/ASK_USER_before_improvement_agent_trace_deepseek_20260821.jsonl`
- `AG-ASK-001` chose `REWRITE_QUERY`, then chose `FALLBACK`.
- No question was built; the result was `FALLBACK`.

After the change:

- `traces/CLOUD_LLM_final_three_cases_trace.jsonl` contains the final three
  case run. `AG-ASK-001` chose `ASK_USER` with
  `identified_missing_information=["medication_class"]`, built
  `請問家人目前使用的是哪一類糖尿病藥物？`, and returned
  `NEEDS_CLARIFICATION`.
- Its second JSONL record is the explicitly simulated re-entry from A; it
  completes the bounded trajectory.
- `traces/ASK_USER_after_improvement_run1.jsonl`, `run2.jsonl`, and
  `run3.jsonl` each reproduce the same ASK_USER decision and question for the
  initial run, followed by the same re-entry shape.

The change did not make B emit an Agent command. B emits a neutral observation;
the real structured-output Planner makes the action decision.

## 16. Cloud LLM ×3 stability result

Final Cloud command used the native OpenRouter path:

```bash
python -u -m tfda_context_gate.agent.demo \
  --planner llm --provider openrouter --retriever fixture \
  --trace-output tfda_context_gate/results/agent_trace_cloud_missing_signal_final3_20260821.jsonl
```

Configured Planner/Rewriter model: `deepseek/deepseek-v4-flash-0731`.

Final three-case result in `traces/CLOUD_LLM_final_three_cases_trace.jsonl`:

- `AG-ASK-001`: `ASK_USER` → `NEEDS_CLARIFICATION`; simulated re-entry from A
  completes the trajectory.
- `AG-REWRITE-001`: `REWRITE_QUERY` → rewritten retrieval → fixture B pass →
  C/D completion.
- `AG-FALLBACK-001`: `REWRITE_QUERY` → second B insufficiency → `FALLBACK`.

The repeated ASK_USER files contain three valid runs. All three selected
`ASK_USER` and carried `medication_class` as the neutral missing-information
signal. This is a small stability check, not a statistical reliability claim.

## 17. Prompt-injection regression

PI-1 and PI-2 are in `demo_cases/agent_demo_cases.json` and are tested in the
Agent runtime tests. The test injects an existing blocked-result boundary into
A, then verifies that the request is blocked before RAG and Agent. Expected
signals include `R_POLICY_BOUNDARY` and
`REASON_PROMPT_INJECTION_SUSPECTED`; Agent action is `None`.

The regression does not claim that every sentence is caught by the default
regex guard. The optional Qwen3Guard adapter exists, and the current tests use
the explicit blocked-result test double to verify the boundary. This is an
important limitation, not a hidden success claim.

## 18. Final tests

Command executed against the project tests:

```bash
/Users/dolly/miniforge3/envs/langchain1.2_Learning/bin/python -m pytest -q tfda_context_gate/tests
```

Final result:

```text
74 passed, 10 skipped in 0.66s
```

The copied test files are under `tests/`; the handoff copy of the final output
is `tests/pytest_final.txt`. The skipped tests are part of the recorded test
result; the handoff does not reinterpret them as passes.

## 19. Real vs Fixture component matrix

The complete component-by-component classification is in
`REAL_FIXTURE_MATRIX.md`. The central conclusion is:

- Cloud Planner and Cloud Query Rewriter: REAL LLM in the final Cloud traces.
- TFDA 129-record retrieval: REAL DATA/REAL retriever implementation exists,
  but the final Cloud Agent trace uses a FIXTURE retriever.
- B in the Agent demo: FIXTURE/DETERMINISTIC `DemoContextGate`.
- C in the workflow/demo: FIXTURE/DETERMINISTIC generator.
- D: deterministic gate plus heuristic demo verifier.
- E: real structured trace emission to JSONL, observational only.
- interrupt/checkpointer/persistent resume/long-term memory: NOT IMPLEMENTED.

No fixture is relabelled as a real component in the handoff.

## 20. Known limitations and unfinished work

See `limitations/LIMITATIONS.md` for the complete honest list. The most
important limitations are:

- this is an MVP/demo and not a clinical decision system;
- B has no live semantic context judge in the Agent workflow;
- C and D use deterministic/demo components in the main Agent demo;
- the final Cloud trace is not an end-to-end Cloud LLM + real TFDA retrieval +
  live B/C/D run;
- the corpus is only 129 records and the vector store is in-memory;
- ASK_USER ends the invocation and relies on a new A invocation rather than
  interrupt/resume/checkpointing;
- Cloud stability evidence is only three cases plus three repeated ASK_USER
  runs;
- the default regex prompt guard has limited coverage and the Qwen guard is
  optional;
- no production PHI, identity, authorization, load, cost, or latency
  evaluation is established.
