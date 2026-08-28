# REAL_FIXTURE_MATRIX

This matrix classifies the current implementation and the final recorded
Agent demo. `REAL LLM`, `REAL DATA`, `FIXTURE`, `DETERMINISTIC`, and
`NOT IMPLEMENTED` are deliberately separate labels. A fixture is never
described as real.

| Area / component | Current implementation | Final Cloud Agent demo run | Classification | Evidence |
|---|---|---|---|---|
| A request schema and routing policy | `RequestContext`, signal normalization, policy gate, `route_request` | Used | DETERMINISTIC | `contracts/a_router/schemas.py`, `policy.py`, `router.py` |
| A default prompt-injection guard | Rule-based guard | Used by default unless a guard is injected | DETERMINISTIC | `contracts/a_router/guard.py` |
| A optional prompt-injection guard | Lazy Qwen3Guard adapter | Not used in final Cloud JSONL | REAL LLM / OPTIONAL | `contracts/a_router/guard.py`, `architecture/README.md` |
| A optional semantic signal extractor | LangChain structured extractor adapter | Not used in final Cloud Agent trace | REAL LLM / OPTIONAL | `contracts/a_router/router.py` |
| Query Expansion | `IdentityQueryExpander` preserves the query exactly | Used | DETERMINISTIC | `contracts/query_expansion/expander.py` |
| TFDA processed corpus | 129 processed risk-communication records from the TFDA dataset 9573 workflow | Not loaded by final Agent Cloud command | REAL DATA | `rag/langchain_documents.json`, `rag/REAL_TFDA_DATASET_AUDIT.md` |
| TFDA embedding | `intfloat/multilingual-e5-small` via `HuggingFaceEmbeddings` | Not used by final Agent Cloud command | REAL MODEL / REAL DATA PATH | `rag/tfda_retriever.py` |
| TFDA vector store | LangChain `InMemoryVectorStore`, lazy-built | Not used by final Agent Cloud command | REAL DATA PATH / EPHEMERAL | `rag/tfda_retriever.py` |
| `TFDADrugSafetyRetriever` | Real retrieval implementation over the 129-row corpus | Not used by final Agent Cloud command; used by retrieval tests/case validation | REAL DATA | `rag/tfda_retriever.py`, `tests/test_tfda_retriever.py` |
| Agent demo retriever | `AgentCaseRetrieverFixture` with retrieval-shaped evidence and documented rank behavior | Used via `--retriever fixture` | FIXTURE / DETERMINISTIC | `agent/demo.py`, final Cloud trace `RAG` events |
| B canonical schemas/adapters | `CanonicalEvidence`, `CanonicalBInput`, `CanonicalBResult`, legacy adapters | Used as contracts | DETERMINISTIC CONTRACT | `contracts/b_context_gate/schemas.py`, `adapters.py` |
| B context gate in workflow | `DeterministicContextGate` with `fixture` or explicit `all_retrieved` mode | Agent demo uses `DemoContextGate` | FIXTURE / DETERMINISTIC | `contracts/b_context_gate/gate.py`, `agent/demo.py` |
| B neutral missing-information signal | Case data supplies `identified_missing_information` to the demo B fixture | `medication_class` is recorded for AG-ASK-001 | FIXTURE OBSERVATION | `demo_cases/agent_demo_cases.json`, final Cloud trace |
| B live semantic sufficiency judge | No live semantic B judge wired into Agent v0.1 workflow | Not used | NOT IMPLEMENTED | `architecture/CURRENT_ARCHITECTURE.md`, limitations |
| Agent Planner offline implementation | `ScriptedAgentPlanner`/test planners | Not used in final Cloud run | FIXTURE / DETERMINISTIC | `agent/demo.py`, `tests/test_agent_runtime.py` |
| Agent Planner Cloud implementation | Native `langchain_openrouter.ChatOpenRouter`, structured output, model `deepseek/deepseek-v4-flash-0731` | Used | REAL LLM | `agent/openrouter.py`, `agent/planner.py`, final Cloud trace `model_name` |
| Query Rewriter offline implementation | `DeterministicQueryRewriter` mappings | Not used in final Cloud run | FIXTURE / DETERMINISTIC | `agent/rewriter.py`, `agent/demo.py` |
| Query Rewriter Cloud implementation | LangChain structured-output adapter with meaning-preserving validation | Used | REAL LLM | `agent/rewriter.py`, final Cloud trace `QUERY_REWRITER` events |
| Question Builder | Field-to-question mapping in LangGraph node | Used after Cloud Planner selects ASK_USER | DETERMINISTIC | `architecture/workflow/graph.py` |
| LangGraph orchestration | `StateGraph`, nodes, conditional edges, bounded recovery loop | Used | REAL EXECUTION LAYER / DETERMINISTIC CONTROL | `architecture/workflow/graph.py` |
| C v2 contract | Evidence-aware `ANSWER`/`PARTIAL`/`INSUFFICIENT` schema and adapter | Used as boundary | DETERMINISTIC CONTRACT | `contracts/c_generator/schemas.py`, `workflow_adapter.py` |
| C workflow generator | `DeterministicFixtureCGenerator` | Used | FIXTURE / DETERMINISTIC | `contracts/c_generator/workflow_adapter.py`, final Cloud trace |
| C live LangChain generator adapter | `LangChainCV2Generator` exists and accepts an injected structured chain | Not used by final Agent demo | OPTIONAL / NOT WIRED IN DEMO | `contracts/c_generator/workflow_adapter.py` |
| D output gate | Schema, policy, evidence-ID and red-line checks | Used | DETERMINISTIC GATE | `contracts/d_output_gate/gate.py`, `schemas.py`, `policy.py` |
| D semantic verifier | `HeuristicSemanticVerifier` and injectable mapping verifier | Heuristic demo verifier is used by workflow defaults | FIXTURE / DETERMINISTIC DEMO | `contracts/d_output_gate/verifier.py` |
| D independently evaluated clinical verifier | No such verifier | Not used | NOT IMPLEMENTED | `architecture/CURRENT_ARCHITECTURE.md`, limitations |
| E structured trace recorder | Pydantic events, evaluations, metrics, redaction, sinks | Used | DETERMINISTIC OBSERVABILITY | `contracts/e_observability/` |
| E JSONL trace output | Full case records written to JSONL with `--trace-output` | Used for all handoff traces | REAL ARTIFACT OUTPUT | `traces/` |
| E trajectory formatter | Display-only rendering from structured events | Used in demo output and JSONL | DETERMINISTIC DISPLAY | `contracts/e_observability/trajectory.py` |
| Prompt-injection regression | Existing blocked-result boundary injected in tests | Not a live Qwen Cloud run | FIXTURE REGRESSION INPUT | `tests/test_agent_runtime.py`, case JSON |
| ASK_USER interrupt/checkpointer | No interrupt, checkpoint, persistent resume, or long-term memory | New invocation from A is simulated | NOT IMPLEMENTED | `REPORT_HANDOFF.md` §3/§9, limitations |

## Final Cloud run composition

The final command was `--planner llm --provider openrouter
--retriever fixture`. Therefore the truthful composition is:

```text
REAL LLM:        Cloud Agent Planner + Cloud Query Rewriter
REAL DATA:       present in repository and validated separately, but not loaded
                 by this command
FIXTURE:         Agent-case retriever and DemoContextGate/B observations
DETERMINISTIC:   A, identity Query Expansion, LangGraph control, Question
                 Builder, C fixture, D demo gate/verifier, E formatting
NOT IMPLEMENTED: live semantic B/C/D end-to-end, interrupt/checkpointer,
                 persistent resume
```

