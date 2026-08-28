# Architecture Audit

Audit date: 2026-08-21

Scope: the code under `tfda_context_gate/` before the A–E integration pass.
This document is based on executable Python modules and test runs, not only on
previous reports. The current post-integration state is recorded at the end.

## Pre-integration execution flow

### A: Input Router + Policy Gate

The executable A entry point is `tfda_context_gate.a_router.router.route_request`
(`run_a` is a stable alias). The current sequence is:

1. Validate the input as `RequestContext`.
2. Normalize `user_raw_input`.
3. Run the injected prompt-injection guard, or the default deterministic regex
   guard. The Qwen3Guard adapter is opt-in and lazy-loads its model.
4. On guard failure, return `F_ROUTER_DEPENDENCY`; on a blocked input, create a
   prompt-injection risk signal and send it through the deterministic policy.
5. Run `RuleBasedSignalExtractor` to produce hard signals.
6. Optionally run the injected LangChain structured extractor and merge its
   signals with the hard signals. The model cannot provide the final route.
7. Run `policy_gate` and return one `RouterStatus` plus `reason_codes`.
8. Set `rag_allowed` to `True` only for `G_GENERAL_EDUCATION`.

There is no A-to-RAG workflow wrapper yet. `a_router/demo.py` is an executable
demo only.

### RAG and B: retrieval, Contract Gate, Context Gate

There is no `b_context_gate/` package or canonical B entry function. The actual
implementation is distributed across phase scripts:

- `01_build_documents.py`: raw TFDA records to LangChain `Document` JSON.
- `02_similarity_retrieval.py`: loads documents, applies a local
  `contract_gate`, builds an in-memory vector store, and writes similarity
  results.
- `03_reranker.py`: repeats the Contract Gate, retrieves candidates, and applies
  a cross-encoder reranker.
- `04_llm_judge.py`: evaluates document/context relevance and sufficiency.
- `05_hybrid.py`: repeats the Contract Gate, retrieval and reranking, then runs
  a set-level LLM judge and a fallback ablation. It writes experiment traces and
  metrics to JSON/CSV files.

The current scripts do not expose a single B result schema. Their result
artifacts use names such as `b_decision`, `approved_document_ids`,
`context_rows`, `usable_document_ids`, and `trace` depending on the phase.

There is no explicit Query Expansion function in the current executable code;
the phase scripts use fixed query constants or the case query directly.

### C: Evidence-aware Generator

The C experiment entry points are:

- `c_generator.b_to_c_interface.build_interface`: builds B-to-C fixture cases
  from a B run directory.
- `c_generator.generator.run_generators`: invokes the configured OpenRouter
  model for the baseline, grounded and evidence-aware methods.
- `c_generator.generator.invoke_one`: invokes one method/case and records the
  experiment result.
- `c_generator.v2_run_experiment.main`: runs the v2 partial-answer experiment.

C currently has two contracts:

- v1 `EvidenceAwareAnswer`: `claims`, decisions `ANSWER`/`INSUFFICIENT`.
- v2 `EvidenceAwareV2Answer`: `supported_claims`,
  `unsupported_requests`, decisions `ANSWER`/`PARTIAL`/`INSUFFICIENT`.

The default `generator.build_chains` uses the v1 schema. The v2 experiment uses
the v2 protocol. C is therefore an experiment suite rather than one stable
production generator node.

### D: Mandatory Output Gate

The executable D entry point is `d_output_gate.gate.run_output_gate`. Its actual
sequence is:

1. Adapt the existing A/B/C payload shapes into `OutputGateRequest`.
2. Validate the A policy snapshot, B evidence set, and C candidate response.
3. Validate candidate shape and claim evidence IDs.
4. Require B evidence decision `PASS` and explicit B-approved evidence IDs.
5. Enforce A route/risk policy and D candidate red lines.
6. Accept safe abstention when there are no supported claims.
7. Run the pluggable semantic verifier when claims exist.
8. Return only `PASS` or `FALLBACK`, with failure type, reason codes and
   fallback response when applicable.

## Module inventory

| Area | Current files / entry points | Classification | Current status |
| --- | --- | --- | --- |
| A | `a_router/` | Formal module | A v0.1 is executable and has schemas, policy, guard and adapters. |
| B/RAG | `00_download_and_inspect.py` through `05_hybrid.py` | Research/experiment scripts | Retrieval and context evaluation work, but no canonical B package or contract. |
| C | `c_generator/` | Formalized experiment module | Generator experiments and v1/v2 schemas exist; not one stable runtime node. |
| D | `d_output_gate/` | Formal module | D v0.1 gate, adapters, schemas and demo verifier exist. |
| E | No existing package | Missing | No formal cross-cutting observability/evaluation implementation exists. |
| Shared runtime | `run_config.py`, `rate_limiter.py` | Shared experiment helpers | Configuration and API experiment utilities only; no central model factory or logging layer. |
| Tests | `tests/test_a_router.py`, `tests/test_d_output_gate.py` | Tests | A and D only; no B/C/E test files at audit time. |
| Data | `data/`, `runs/`, `results/`, `reports/` | Dataset and generated artifacts | Includes current and historical experiment outputs. |
| Deliverables | `deliverables/` | Archived/sample deliverables | Contains zipped and staging copies of C code and run artifacts. |

### Formal modules, experiments, tests, fixtures, old versions, deletion candidates

- Formal modules: `a_router/`, `c_generator/` contracts and runners,
  `d_output_gate/`, plus `run_config.py` and `rate_limiter.py` as current
  experiment infrastructure.
- Experiments: numbered phase scripts, `c_generator/*experiment*`,
  `run_experiment.py`, `v2_run_experiment.py`, and `runs/`/`results/` outputs.
- Tests: `tests/` and its two test modules.
- Sample fixtures: C interface JSON files under the current and archived
  `results/` trees, plus `c_generator/experiment_cases.py` and
  `hard_experiment_cases.py`.
- Historical/archived copies: `deliverables/staging/` and the zip files under
  `deliverables/`. The staged C source hashes match the current C source for
  the files checked, but it remains a separate copy.
- Deletion candidates: duplicate staged source copies, old run directories,
  generated result files, and the numbered phase scripts after a future
  migration to a canonical RAG/B package. They are not deleted in this pass.

No files were moved during the audit. Moving the numbered scripts would change
their script-local imports (`from run_config ...`, `from rate_limiter ...`) and
would risk breaking reproducibility.

## Schema and interface matrix

| Boundary | Input schema | Output schema | Main entry | Directly chainable? |
| --- | --- | --- | --- | --- |
| A | `RequestContext` (`a_router.schemas`) | `AResult` | `route_request` | Yes as a Python call, but no workflow wrapper consumes it. |
| RAG phase 2/3 | LangChain `Document` plus query string | phase-specific dict/JSON rows | `run_query`, `run_one_query` | No canonical result model. |
| B/context judge | phase-specific dicts and LLM assessment models | `b_decision`/judge result plus rows | functions in `04_llm_judge.py` and `05_hybrid.py` | No; names and shapes vary by phase. |
| B → C | B run artifacts and manual case specs | JSON interface cases | `build_interface` | Adapter exists for experiment fixtures only. |
| C v1 | case dict | `EvidenceAwareAnswer` or raw text | `run_generators`/`invoke_one` | Not directly to D without D adapter. |
| C v2 | case dict with approved IDs and contexts | `EvidenceAwareV2Answer` | `v2_run_experiment` | Semantically close to D canonical candidate. |
| D | `OutputGateRequest` or dict with `a_result`, `b_result`, `c_result` | `OutputGateResult` | `run_output_gate` | Yes only after D's adapter normalizes names. |

Important mismatches:

- B calls the final context result `b_decision` in fixtures, while D expects
  `EvidenceSet.decision` (the adapter accepts both).
- Retrieval/context rows use `document_id`, `context_rows` or `contexts`, while
  D normalizes them to `evidence_id` and `evidence`.
- C v1 uses `claims`; C v2 and D use `supported_claims`.
- The default C generator uses the v1 schema while D's canonical candidate
  schema is the v2-shaped contract.
- A's Pydantic enums are strict typed values; D's policy snapshot is a
  deliberately independent string-based snapshot. The adapter performs no
  full A model conversion.
- There is no shared request envelope carrying A, RAG, B, C and D results.

## Dependencies and duplicated infrastructure

| Concern | Current finding |
| --- | --- |
| Model loading | Qwen3Guard loading is in `a_router/guard.py`; C and B/judge scripts each create their own OpenRouter model. |
| Qwen3Guard caching | `Qwen3GuardPromptInjectionGuard` is lazy per object: repeated calls on the same object reuse the model, but there is no singleton/global cache. Constructing one per request reloads the weights. |
| Qwen3Guard fallback | Load/inference errors become `RouterDependencyError`; A catches them and returns `F_ROUTER_DEPENDENCY` with `rag_allowed=False`. The default offline path is the regex guard, not Qwen3Guard. |
| Model factory | None. `c_generator.generator.build_llm`, `04_llm_judge.build_llm`, and `05_hybrid.build_judge` duplicate model construction/configuration. |
| Embeddings/reranker | Embedding construction and Contract Gate logic are repeated in phase 2, phase 3 and phase 5 scripts. |
| Config | `run_config.py` centralizes run directories and dotenv lookup, but each experiment also has local model, query and rate-limit constants. |
| Schemas | A and D have formal schemas; C has v1/v2 duplicates; B has no canonical package schema. |
| Logging | No central request logging. Existing `rate_limiter.py` writes experiment-specific JSONL events, and phase 5 writes local trace artifacts. |
| E | No formal E implementation was present at audit time. Existing phase traces are experiment outputs, not a request-level cross-cutting layer. |

## Tests baseline at audit time

Commands run from the repository directory with `python3`:

| Command | Result |
| --- | --- |
| `python3 -m pytest -q` | **28 passed** in 0.16s |
| `python3 -m pytest -q tfda_context_gate/tests/test_a_router.py` | **17 passed** in 0.07s |
| `python3 -m pytest -q tfda_context_gate/tests/test_d_output_gate.py` | **11 passed** in 0.06s |
| B test collection | No independent B tests; the repository test set contains only A and D modules. |
| C test collection | No independent C tests; the repository test set contains only A and D modules. |

The reported contradiction is not reproducible in the current checkout:

- A tests pass 17/17.
- D tests pass 11/11.
- Full tests pass 28/28.
- A and D are separate packages in the current test suite, and A imports no D
  code. There is no git history in this directory from which to identify the
  exact earlier two-failure snapshot.

The most supportable explanation is that the earlier reports refer to different
intermediate repository states or C/D contract transitions. The current D
tests include the v1 adapter and all listed policy/evidence/semantic cases, and
there is no current D failure to fix. No safety semantics were changed to make
the current tests pass.

## Known inconsistencies and integration blockers

1. No canonical B module or B output schema exists.
2. No executable A → RAG → B → C → D orchestration function exists.
3. Query Expansion is described in the baseline architecture but has no current
   executable implementation.
4. C has two response protocols, and the default generator still uses v1.
5. RAG phase scripts duplicate gate/retrieval/model/config helpers.
6. B/C tests are absent, so current full-test green status does not validate
   those experiments.
7. Evidence IDs are normalized only at D's boundary; upstream names are not
   stable.
8. Model/API configuration is spread across experiment files and requires
   external credentials for live runs.
9. `python3` is available in this environment but `python` is not; README
   commands should use `python3` or an activated virtual environment.
10. The directory is not a git repository; no pre-existing uncommitted diff
    could be inspected and no git diff can be recorded.

## Technical debt

- Extract B/RAG into a package only after freezing a canonical contract.
- Decide whether C v2 replaces v1; keep D's legacy adapter until migration is
  explicitly approved.
- Add B/C contract tests and an offline integration fixture.
- Add a central model factory/config boundary if live components are promoted.
- Add privacy-aware request redaction and retention policy before production
  logging.
- Add thread-safe/model-cache policy for Qwen3Guard if it is used per request.
- Separate generated runs and deliverables from source modules in a future
  migration, preserving existing imports and provenance.

## TODO at audit time

- Implement E v0.1 as a dependency-light, cross-cutting JSONL trace and
  evaluation collection package.
- Add E tests for normal, blocked, insufficient, fallback, exception, secret
  redaction, request isolation and future Agent fields.
- Add README and `CURRENT_ARCHITECTURE.md` as the source-of-truth documents.
- Build a later offline end-to-end adapter test using fixed A/B/C/D fixtures;
  do not develop an Agent in this pass.

## Post-audit verification

The requested non-invasive E v0.1 implementation and source-of-truth documents
were added after this audit:

- `e_observability/` provides Pydantic trace/evaluation schemas, JSONL and
  in-memory sinks, request-scoped spans, metrics, failure recording and secret
  redaction.
- `tests/test_e_observability.py` covers all eight requested E behaviors.
- `README.md` and `CURRENT_ARCHITECTURE.md` document the current boundaries and
  remaining blockers.
- Final verification: `python3 -m pytest -q` → **36 passed** (A 17, D 11, E 8).
- No A/B/C/D source file was moved or rewritten; no Agent was developed.

## Post-integration verification

The next baseline iteration added the missing deterministic workflow boundary:

- `b_context_gate/` freezes `CanonicalEvidence`, `CanonicalBInput` and
  `CanonicalBResult`; legacy B result names are normalized by adapters.
- `query_expansion/` preserves the original query and emits one deterministic
  retrieval query.
- `rag/` exposes `RAGResult`, legacy retrieval normalization and an explicit
  `FixtureRetriever`.
- `c_generator/workflow_adapter.py` freezes C v2 as the canonical workflow
  generator contract while leaving C v1 as legacy/experiment code.
- `workflow.run_workflow()` executes A → Query Expansion → RAG → B → C v2 → D.
- `e_observability` now records STARTED and terminal stage events, including
  `BLOCKED`, `INSUFFICIENT`, `FALLBACK` and `ERROR` paths.
- Default RAG, B and C implementations are labelled MOCK/FIXTURE; existing
  live research scripts are not rewritten or silently treated as production.
- Integration/E2E verification: `python3 -m pytest -q` → **51 passed**
  (A 17, D 11, E 8, workflow integration 15).
- Agent decision, retry, tool calling and multi-agent orchestration remain
  **NOT IMPLEMENTED**.

## Safety conclusion

The current code is a demo/MVP boundary system, not a production clinical
system. A remains the policy authority, B must explicitly approve evidence, C
is evidence-aware, D is mandatory before an answer is returned, and E must
remain observational. No autonomous Agent or clinical decision authority is
present in this audit.

## Real TFDA corpus integration verification

The real retrieval path was added without removing the deterministic fixture
path used by contract tests:

- `rag/tfda_retriever.py` loads and validates the processed 129-record TFDA
  corpus and builds a lazy `InMemoryVectorStore` using the already-declared
  HuggingFace embedding dependency.
- `rag/tfda_smoke_cases.py` defines role-based Patient, Healthcare Professional
  and Caregiver cases. P1/P2/H1/H2/H3/C1/C2 are retrieval cases; P3 is blocked
  by A before RAG; C3 is marked as a future clarification/`ASK_USER` candidate
  and does not assume SGLT2.
- `rag/demo.py --all` reports declared role, query, A status, top-k evidence ID,
  ingredient, score, date, source and expected-match status.
- The processed corpus audit found 129 unique IDs, no malformed rows and no
  empty page content. See `REAL_TFDA_DATASET_AUDIT.md`.

The real vector smoke run completed with all seven retrieval cases finding the
expected evidence terms. The runtime `workflow.demo` uses a clearly named
`all_retrieved` deterministic B demo mode; this is not a replacement for a
clinical B judge. No Agent, retry loop or role-based permission bypass was
introduced.
