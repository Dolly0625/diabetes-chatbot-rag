# FILE_INDEX

This index covers every file in the handoff package. Paths are relative to
`report_handoff/`. The source column identifies the original project path when
the file is a copied snapshot. Chapter references are suggested report use.

## Handoff documents

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `REPORT_HANDOFF.md` | New handoff document | Consolidated factual architecture, contracts, runs, tests, and limitations. | All chapters; primary briefing. |
| `README.md` | `tfda_context_gate/README.md` | Exact project README copy at the package root for handoff discoverability. | Introduction, system scope, entry points, limitations. |
| `REAL_FIXTURE_MATRIX.md` | New handoff document | Explicit REAL LLM / REAL DATA / FIXTURE / DETERMINISTIC / NOT IMPLEMENTED classification. | Architecture, experiment validity, limitations. |
| `FILE_INDEX.md` | New handoff document | File-by-file navigation index. | Appendix / reproducibility. |
| `architecture/CURRENT_ARCHITECTURE.md` | `tfda_context_gate/CURRENT_ARCHITECTURE.md` | Current source-of-truth architecture note. | Architecture and contracts. |
| `architecture/README.md` | `tfda_context_gate/README.md` | Project overview, scope, entry points, and status. | Introduction, system scope, limitations. |
| `architecture/ARCHITECTURE_AUDIT.md` | `tfda_context_gate/ARCHITECTURE_AUDIT.md` | Existing architecture audit. | Architecture history and audit context. |
| `limitations/LIMITATIONS.md` | New handoff document | Complete known limitations and unfinished work. | Limitations, safety, future work. |

## LangGraph and workflow source snapshots

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `architecture/workflow/__init__.py` | `tfda_context_gate/workflow/__init__.py` | Workflow package exports. | Implementation map. |
| `architecture/workflow/graph.py` | `tfda_context_gate/workflow/graph.py` | LangGraph `StateGraph`, nodes, edges, conditional routing, bounds, Question Builder, Agent loop. | Core architecture, topology, bounded recovery. |
| `architecture/workflow/runner.py` | `tfda_context_gate/workflow/runner.py` | Public `run_workflow`, dependency injection, trace finalization, error boundary. | Runtime lifecycle and testability. |
| `architecture/workflow/adapters.py` | `tfda_context_gate/workflow/adapters.py` | A/RAG/B/C/D contract adapters. | Contract transitions. |
| `architecture/workflow/fallbacks.py` | `tfda_context_gate/workflow/fallbacks.py` | Fixed fallback responses. | Safety termination behavior. |
| `architecture/workflow/schemas.py` | `tfda_context_gate/workflow/schemas.py` | Public `WorkflowResult` contract. | Output contract. |
| `architecture/workflow/demo.py` | `tfda_context_gate/workflow/demo.py` | Deterministic A–E baseline demo CLI. | Baseline demonstration and fixture distinction. |

## Agent source snapshots

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `agent/__init__.py` | `tfda_context_gate/agent/__init__.py` | Agent package exports and public contracts. | Agent module map. |
| `agent/config.py` | `tfda_context_gate/agent/config.py` | `AgentLimits` defaults: 2 steps, 1 rewrite, 1 clarification. | Bounded limits. |
| `agent/context.py` | `tfda_context_gate/agent/context.py` | Projects B output into narrow Planner context and evidence summaries. | Planner context/data minimization. |
| `agent/schemas.py` | `tfda_context_gate/agent/schemas.py` | `AgentDecision` union, `AgentDecisionContext`, attempts, evidence summaries. | Agent contracts. |
| `agent/planner.py` | `tfda_context_gate/agent/planner.py` | Planner protocol, full system prompt/policy, structured-output adapter. | Planner design and policy. |
| `agent/rewriter.py` | `tfda_context_gate/agent/rewriter.py` | Rewriter schema, prompt, LLM adapter, rewrite validation, fixture. | Query rewriting. |
| `agent/openrouter.py` | `tfda_context_gate/agent/openrouter.py` | Native `ChatOpenRouter` construction, DeepSeek model and request settings. | Cloud LLM configuration. |
| `agent/ollama.py` | `tfda_context_gate/agent/ollama.py` | Optional local Ollama adapter. | Provider comparison / non-final path. |
| `agent/demo.py` | `tfda_context_gate/agent/demo.py` | Three-case demo CLI, Cloud/fixture selection, trace export, simulated re-entry. | Demo method and real/fixture matrix. |

## A, B, C, D, E and Query Expansion contracts

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `contracts/a_router/__init__.py` | `tfda_context_gate/a_router/__init__.py` | A public exports. | A module map. |
| `contracts/a_router/schemas.py` | `tfda_context_gate/a_router/schemas.py` | Request, signals, and A result schemas. | A contract. |
| `contracts/a_router/labels.py` | `tfda_context_gate/a_router/labels.py` | Roles, language, intent, risk, route, and reason labels. | A vocabulary. |
| `contracts/a_router/rules.py` | `tfda_context_gate/a_router/rules.py` | Deterministic signal normalization/extraction. | A policy inputs. |
| `contracts/a_router/policy.py` | `tfda_context_gate/a_router/policy.py` | Deterministic route policy. | A authority and safety boundary. |
| `contracts/a_router/guard.py` | `tfda_context_gate/a_router/guard.py` | Regex guard and optional Qwen3Guard adapter. | Prompt injection and guard limitations. |
| `contracts/a_router/router.py` | `tfda_context_gate/a_router/router.py` | A execution and optional structured extractor. | A runtime. |
| `contracts/a_router/errors.py` | `tfda_context_gate/a_router/errors.py` | A dependency error type. | Failure handling. |
| `contracts/a_router/demo.py` | `tfda_context_gate/a_router/demo.py` | A CLI demo. | Demonstration appendix. |
| `contracts/b_context_gate/__init__.py` | `tfda_context_gate/b_context_gate/__init__.py` | B public exports. | B module map. |
| `contracts/b_context_gate/schemas.py` | `tfda_context_gate/b_context_gate/schemas.py` | Canonical evidence, input, and B result schemas. | B contract and evidence identity. |
| `contracts/b_context_gate/gate.py` | `tfda_context_gate/b_context_gate/gate.py` | Context Gate protocol and deterministic fixture/all-retrieved modes. | B implementation status and limitations. |
| `contracts/b_context_gate/adapters.py` | `tfda_context_gate/b_context_gate/adapters.py` | Legacy-to-canonical B normalization. | Contract compatibility. |
| `contracts/c_generator/__init__.py` | `tfda_context_gate/c_generator/__init__.py` | C public exports. | C module map. |
| `contracts/c_generator/schemas.py` | `tfda_context_gate/c_generator/schemas.py` | C v1/v2 answer and claim schemas. | C evidence-aware contract. |
| `contracts/c_generator/workflow_adapter.py` | `tfda_context_gate/c_generator/workflow_adapter.py` | Canonical C workflow input, fixture generator, LangChain adapter. | C implementation matrix. |
| `contracts/c_generator/generator.py` | `tfda_context_gate/c_generator/generator.py` | Legacy/experiment generator execution. | C history and experiments. |
| `contracts/c_generator/prompts.py` | `tfda_context_gate/c_generator/prompts.py` | C prompts. | Generator design. |
| `contracts/c_generator/b_to_c_interface.py` | `tfda_context_gate/c_generator/b_to_c_interface.py` | B-to-C experiment interface builder. | Experiment provenance. |
| `contracts/c_generator/experiment_cases.py` | `tfda_context_gate/c_generator/experiment_cases.py` | C experiment cases. | Evaluation design. |
| `contracts/c_generator/hard_experiment_cases.py` | `tfda_context_gate/c_generator/hard_experiment_cases.py` | Hard C experiment cases. | Stress/evaluation design. |
| `contracts/c_generator/evaluator.py` | `tfda_context_gate/c_generator/evaluator.py` | C output metrics/evaluation. | Evaluation method. |
| `contracts/c_generator/run_experiment.py` | `tfda_context_gate/c_generator/run_experiment.py` | C experiment runner. | Reproducibility appendix. |
| `contracts/c_generator/v2_run_experiment.py` | `tfda_context_gate/c_generator/v2_run_experiment.py` | C v2 experiment runner and reports. | Canonical v2 experiments. |
| `contracts/d_output_gate/__init__.py` | `tfda_context_gate/d_output_gate/__init__.py` | D public exports. | D module map. |
| `contracts/d_output_gate/schemas.py` | `tfda_context_gate/d_output_gate/schemas.py` | Policy, evidence, candidate, failure, and D result schemas. | D contract. |
| `contracts/d_output_gate/adapters.py` | `tfda_context_gate/d_output_gate/adapters.py` | A/B/C payload normalization into D request. | D boundary. |
| `contracts/d_output_gate/policy.py` | `tfda_context_gate/d_output_gate/policy.py` | D policy snapshot and red-line rules. | Output safety policy. |
| `contracts/d_output_gate/verifier.py` | `tfda_context_gate/d_output_gate/verifier.py` | Heuristic and mapping semantic verifier protocols. | Verifier status/limits. |
| `contracts/d_output_gate/gate.py` | `tfda_context_gate/d_output_gate/gate.py` | D validation and PASS/FALLBACK authority. | Final answer gate. |
| `contracts/e_observability/__init__.py` | `tfda_context_gate/e_observability/__init__.py` | E public exports. | E module map. |
| `contracts/e_observability/schemas.py` | `tfda_context_gate/e_observability/schemas.py` | TraceEvent, EvaluationRecord, metrics schemas. | Trace data model. |
| `contracts/e_observability/tracer.py` | `tfda_context_gate/e_observability/tracer.py` | Request recorder/span lifecycle and fail-open sink boundary. | Observability implementation. |
| `contracts/e_observability/sinks.py` | `tfda_context_gate/e_observability/sinks.py` | In-memory and JSONL sinks. | Trace persistence. |
| `contracts/e_observability/privacy.py` | `tfda_context_gate/e_observability/privacy.py` | Credential redaction, query hash, recursive sanitization. | Privacy limitations. |
| `contracts/e_observability/metrics.py` | `tfda_context_gate/e_observability/metrics.py` | Counters and latency summaries. | Runtime metrics. |
| `contracts/e_observability/trajectory.py` | `tfda_context_gate/e_observability/trajectory.py` | Display-only trace trajectory formatter. | Trace presentation. |
| `contracts/e_observability/demo.py` | `tfda_context_gate/e_observability/demo.py` | E demo CLI. | Observability example. |
| `contracts/query_expansion/__init__.py` | `tfda_context_gate/query_expansion/__init__.py` | Query Expansion exports. | Query pipeline. |
| `contracts/query_expansion/schemas.py` | `tfda_context_gate/query_expansion/schemas.py` | Query Expansion input/result schemas. | Query contract. |
| `contracts/query_expansion/adapters.py` | `tfda_context_gate/query_expansion/adapters.py` | A-to-expansion adapter. | Stage boundary. |
| `contracts/query_expansion/expander.py` | `tfda_context_gate/query_expansion/expander.py` | Identity deterministic expander protocol/implementation. | v0.1 query behavior. |

## RAG and dataset evidence

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `rag/__init__.py` | `tfda_context_gate/rag/__init__.py` | RAG public exports. | RAG module map. |
| `rag/schemas.py` | `tfda_context_gate/rag/schemas.py` | RAG result schema and B adapter. | RAG contract. |
| `rag/retriever.py` | `tfda_context_gate/rag/retriever.py` | Retriever protocol and fixture retriever. | Real/fixture comparison. |
| `rag/tfda_retriever.py` | `tfda_context_gate/rag/tfda_retriever.py` | Real 129-record TFDA loader, embeddings, in-memory vector retrieval. | Dataset/RAG implementation. |
| `rag/tfda_smoke_cases.py` | `tfda_context_gate/rag/tfda_smoke_cases.py` | TFDA retrieval smoke-case definitions. | Retrieval demo cases. |
| `rag/demo.py` | `tfda_context_gate/rag/demo.py` | Real TFDA retrieval demo CLI. | Real data demonstration. |
| `rag/REAL_TFDA_DATASET_AUDIT.md` | `tfda_context_gate/REAL_TFDA_DATASET_AUDIT.md` | Audit of 129 processed rows and provenance. | Dataset chapter. |
| `rag/langchain_documents.json` | `tfda_context_gate/data/processed/langchain_documents.json` | Complete current 129-record processed corpus. | Dataset appendix/evidence. |
| `rag/phase_scripts/01_build_documents.py` | `tfda_context_gate/01_build_documents.py` | Data-to-LangChain document build script. | Corpus construction. |
| `rag/phase_scripts/02_similarity_retrieval.py` | `tfda_context_gate/02_similarity_retrieval.py` | Similarity retrieval phase script. | Retrieval method. |
| `rag/phase_scripts/03_reranker.py` | `tfda_context_gate/03_reranker.py` | Reranking phase script. | Retrieval experiments. |
| `rag/phase_scripts/05_hybrid.py` | `tfda_context_gate/05_hybrid.py` | Hybrid phase experiment script. | Retrieval/context experiments. |
| `rag/phase2_retrieval_report.md` | `tfda_context_gate/reports/phase2_retrieval_report.md` | Existing Phase 2 retrieval report. | RAG results. |
| `rag/phase3_reranker_report.md` | `tfda_context_gate/reports/phase3_reranker_report.md` | Existing Phase 3 reranker report. | RAG results/limitations. |
| `rag/phase5_hybrid_report.md` | `tfda_context_gate/reports/phase5_hybrid_report.md` | Existing Phase 5 hybrid report. | B/context experiments. |

## Demo cases and raw traces

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `demo_cases/agent_demo_cases.json` | `tfda_context_gate/agent_demo_cases.json` | Machine-readable three Agent cases plus PI regression cases. | Case design and ground truth. |
| `demo_cases/agent_demo_case_schema.py` | `tfda_context_gate/agent_demo_case_schema.py` | Case loader/schema; case data is not the runtime Agent schema. | Evaluation contract distinction. |
| `demo_cases/AGENT_V0_1_CASE_DESIGN.md` | `tfda_context_gate/AGENT_V0_1_CASE_DESIGN.md` | Human-readable case design, retrieval validation, baseline/expected behavior. | Demo methodology. |
| `traces/ASK_USER_before_improvement_agent_trace_deepseek_20260821.jsonl` | `tfda_context_gate/results/agent_trace_deepseek_20260821.jsonl` | Pre-improvement three-case Cloud trace; AG-ASK chose rewrite then fallback. | ASK_USER before/after. |
| `traces/CLOUD_LLM_final_three_cases_trace.jsonl` | `tfda_context_gate/results/agent_trace_cloud_missing_signal_final3_20260821.jsonl` | Final Cloud three-case JSONL, including simulated ASK_USER re-entry. | Final demo results and full trace. |
| `traces/ASK_USER_after_improvement_run1.jsonl` | `tfda_context_gate/results/agent_trace_ask_final_run1_20260821.jsonl` | Final repeated ASK_USER run 1. | Stability. |
| `traces/ASK_USER_after_improvement_run2.jsonl` | `tfda_context_gate/results/agent_trace_ask_final_run2_20260821.jsonl` | Final repeated ASK_USER run 2. | Stability. |
| `traces/ASK_USER_after_improvement_run3.jsonl` | `tfda_context_gate/results/agent_trace_ask_final_run3_20260821.jsonl` | Final repeated ASK_USER run 3. | Stability. |

## Tests and final test output

| Handoff path | Original/source | Purpose | Report use |
|---|---|---|---|
| `tests/__init__.py` | `tfda_context_gate/tests/__init__.py` | Test package marker. | Test inventory. |
| `tests/test_a_router.py` | `tfda_context_gate/tests/test_a_router.py` | A routing, policy, and guard tests. | A validation. |
| `tests/test_agent_demo_cases.py` | `tfda_context_gate/tests/test_agent_demo_cases.py` | Case schema, real TFDA retrieval rank/evidence checks, and PI boundaries. | Case and real-data validation. |
| `tests/test_agent_runtime.py` | `tfda_context_gate/tests/test_agent_runtime.py` | Planner context, LangGraph actions, rewrites, limits, failures, trace, and injection tests. | Agent/workflow validation. |
| `tests/test_workflow_integration.py` | `tfda_context_gate/tests/test_workflow_integration.py` | A–E workflow integration and fallback behavior. | End-to-end contract validation. |
| `tests/test_d_output_gate.py` | `tfda_context_gate/tests/test_d_output_gate.py` | D schema/evidence/policy/verifier tests. | D validation. |
| `tests/test_e_observability.py` | `tfda_context_gate/tests/test_e_observability.py` | E trace, sink, privacy, and metrics tests. | Observability validation. |
| `tests/test_tfda_retriever.py` | `tfda_context_gate/tests/test_tfda_retriever.py` | 129-row corpus validation and real retriever contract tests. | RAG/data validation. |
| `tests/requirements.txt` | `tfda_context_gate/requirements.txt` | Project dependency snapshot used by the test/demo project. | Reproducibility. |
| `tests/pytest_final.txt` | New handoff artifact | Exact final pytest command, output, and exit code. | Test results. |
