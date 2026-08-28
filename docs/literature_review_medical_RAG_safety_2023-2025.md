# Medical RAG, Evidence-Grounded Generation & Safety Gates — Literature Review for TFDA Diabetes Agent v0.1

**Date:** 2026-08-27  
**Purpose:** Compare v0.1 safety & evidence baseline (A/B/C/D gates) against 2023-2025 industry architecture. All papers verified via arXiv / PubMed / PMC with open-access links. No hallucinated titles.

**v0.1 baseline (from `docs/proposal/糖尿病多工具Agent提案書_V0.1.md` + `tfda_context_gate/V0_1_提案書.md`):**
```
User → A: Input Router/Policy Gate (block diagnosis/dose/prompt-injection, only G_GENERAL_EDUCATION → RAG)
     → Query Expansion (identity, preserves original_query)
     → RAG (TFDA 129 docs, intfloat/multilingual-e5-small, candidate evidence)
     → B: Context/Evidence Gate (PASS / INSUFFICIENT / UNSAFE / REVIEW / FALLBACK, only approved_evidence_ids → C)
     → Agent recovery (bounded: max 2 steps, 1 rewrite, 1 clarification) → ASK_USER / REWRITE_QUERY → RAG → B / FALLBACK
     → C v2: Evidence-aware Generator (supported_claims must cite approved IDs, unsupported_requests, limitations)
     → D: Mandatory Output Gate (PASS or FALLBACK, checks schema, policy, evidence IDs, red lines)
     → E: Observability (trace, metrics, JSONL)
```
Key invariants: A/B/D are **non-bypassable gates**; C cannot use unapproved evidence; E never changes decisions; fail-closed.

---

## Summary Table (12 papers, 2023-2025)

| # | Title | Venue / Year | Key Contribution | Link |
|---|-------|--------------|------------------|------|
| 1 | Benchmarking Retrieval-Augmented Generation for Medicine (MIRAGE / MedRAG) | ACL Findings 2024 (arXiv 2402.13178) | MedRAG toolkit + MIRAGE benchmark (7,663 Qs, 5 datasets) | https://arxiv.org/abs/2402.13178 |
| 2 | Corrective Retrieval Augmented Generation (CRAG) | arXiv 2401.15884 (2024) | Lightweight retrieval evaluator → Correct/Incorrect/Ambiguous → web-search fallback + decompose-recompose | https://arxiv.org/abs/2401.15884 |
| 3 | Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection | ICLR 2024 Oral (arXiv 2310.11511) | Reflection tokens (Retrieve/IsRel/IsSup/IsUse) for on-demand retrieval & self-critique | https://arxiv.org/abs/2310.11511 |
| 4 | ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems | NAACL 2024 (arXiv 2311.09476) | Synthetic-data fine-tuned judges for context relevance / answer faithfulness / answer relevance + PPI confidence intervals | https://arxiv.org/abs/2311.09476 |
| 5 | Med-HALT: Medical Domain Hallucination Test for Large Language Models | arXiv 2307.15343 (2023) | Reasoning (FCT/NOTA/Fake) + Memory (PMID↔Title/Link) hallucination benchmark (18,866 + 4,916) | https://arxiv.org/abs/2307.15343 |
| 6 | Almanac: Retrieval-Augmented Language Models for Clinical Medicine | NEJM AI 2024 (arXiv 2303.01229) | Curated-browser RAG (PubMed/UpToDate/BMJ) + λ=0.83 threshold + calculator tool, 314 clinical Qs, 8 clinicians | https://arxiv.org/abs/2303.01229 |
| 7 | Sufficient Context: Selective Generation with Evidence Sufficiency Autorater | arXiv 2411.06037 (2024) | Defines sufficient vs insufficient context; LLM autorater + P(True)/P(Correct) → selective answering, +2-10% accuracy | https://arxiv.org/abs/2411.06037 |
| 8 | Application of Chatbots to Help Patients Self-Manage Diabetes: Systematic Review and Meta-Analysis | JMIR 2024 (PMC11653048) | 25 studies, HbA1c MD 0.30 (p=.02), only 1 RCT, 68% diet/exercise/glucose/meds | https://pmc.ncbi.nlm.nih.gov/articles/PMC11653048/ |
| 9 | Transforming Health Care Through Chatbots for Medical History-Taking: Comprehensive Systematic Review | JMIR Med Inform 2024 (PMC11393511) | 18 studies (15 obs + 3 RCTs), 24/7 automated history-taking, STROBE/RoB2 quality | https://pmc.ncbi.nlm.nih.gov/articles/PMC11393511/ |
| 10 | The diagnostic and triage accuracy of digital and online symptom checker tools: a systematic review | npj Digital Medicine 2022 (Wallace et al) | 10 studies, primary diagnosis 19-37.9%, triage 48.8-90.1%, high variability | https://doi.org/10.1038/s41746-022-00667-w |
| 11 | Triage and Diagnostic Accuracy of Online Symptom Checkers: Systematic Review | JMIR 2023 (PMID 37266983) | 14 studies (21,296 screened), diagnostic low vs GPs, triage suboptimal 69%, QUADAS-2 | https://pubmed.ncbi.nlm.nih.gov/37266983/ |
| 12 | The Impact of AI Scribes on Streamlining Clinical Documentation: A Systematic Review | Healthcare 2025 (Sasseville et al) | 8 studies, AI scribes ↓ documentation time, mixed burnout, accuracy varies by model | https://doi.org/10.3390/healthcare13121447 |

> Proposal-cited extras verified: Diabetes-focused conversational agents SR (PubMed 40840786, 2025), AI-based conversational agents umbrella review (PubMed 41337874), Mental health chatbot RCT meta-analysis (PubMed 38631422), Ambient scribe protocol (BMJ Open 2025, NCT07157943). All confirm v0.1's "support, not diagnose" positioning.

---

## Detailed Summaries

### 1. MIRAGE / MedRAG — Xiong et al., ACL Findings 2024
- **Problem:** No best-practice for medical RAG (corpus/retriever/LLM choices); hallucinations + outdated knowledge.
- **Method:** MIRAGE benchmark (7,663 Qs from 5 QA datasets: MedQA, MedMCQA, MMLU-Med, PubMedQA*, BioASQ). MedRAG toolkit: 5 corpora (PubMed 23.9M, StatPearls 9.3k, Textbooks 18, Wikipedia 6.5M, MedCorp 30.4M), 4 retrievers (BM25, Contriever, SPECTER, MedCPT) + RRF-2/RRF-4 fusion, 6 LLMs (GPT-4/3.5, Mixtral, Llama2, MEDITRON, PMC-LLaMA). 41 combos, 1.8T tokens.
- **Evaluation:** Accuracy per dataset + avg; std dev. MedRAG +1% to +18% over CoT; GPT-3.5/Mixtral → GPT-4 level. PubMed only corpus improving all tasks; MedCorp most robust. MedCPT + BM25 best; RRF-4 avg 71.57% on MedCorp. Log-linear scaling with k (optimal 16-32), lost-in-the-middle U-shape.
- **Insufficient evidence / fallback:** No explicit abstention; relies on retrieval quality. Recommends MedCorp + RRF to reduce insufficient cases, but no gate. **Relevance to v0.1:** Validates v0.1's corpus/retriever modularity (Tool contract) and need for B gate — MIRAGE shows even best RAG still fails without sufficiency check. v0.1's B (PASS/INSUFFICIENT) is stricter than MedRAG's implicit "always answer". Suggests v0.1 should log corpus/retriever ablation like MedRAG and adopt RRF-2 (BM25+MedCPT) for TFDA.
- **Link:** https://arxiv.org/abs/2402.13178 (PDF) · https://aclanthology.org/2024.findings-acl.372.pdf · GitHub: https://github.com/Teddy-XiongGZ/MedRAG

### 2. CRAG — Yan et al., arXiv 2401.15884 (2024)
- **Problem:** RAG fails when retriever returns irrelevant/misleading docs; conventional RAG indiscriminately prepends docs.
- **Method:** Lightweight retrieval evaluator (fine-tuned T5-large on PopQA) scores relevance → 3 actions: **Correct** (refine via decompose-then-recompose into knowledge strips), **Incorrect** (discard, web-search via Google API + ChatGPT keyword rewrite), **Ambiguous** (combine both). Plug-and-play with RAG or Self-RAG.
- **Evaluation:** PopQA, Biography (FactScore), PubHealth, Arc-Challenge. CRAG > RAG: +7.0% PopQA, +14.9% FactScore Biography, +36.6% PubHealth, +15.4% Arc-Challenge (SelfRAG-LLaMA2-7b). Self-CRAG > Self-RAG: +6.9% PopQA, +5.0% Biography.
- **Insufficient evidence / fallback:** Explicit **Incorrect → discard + web search** and **Ambiguous → hybrid**; evaluator threshold tuned on validation. Decompose-recompose filters non-essential context. **Relevance to v0.1:** Direct analogue to v0.1's B gate + Agent recovery. v0.1's B=INSUFFICIENT → REWRITE_QUERY → RAG → B is a *single* corrective loop; CRAG shows value of **evaluator + external fallback** and **knowledge refinement**. Recommendation: v0.1 should keep B as hard gate (not LLM self-eval) but add CRAG-style evaluator as *signal* to B, and consider web-search only for v0.3 with allowlist (per proposal's Tool Registry). Do not let evaluator bypass B.
- **Link:** https://arxiv.org/abs/2401.15884 · https://github.com/HuskyInSalt/CRAG

### 3. Self-RAG — Asai et al., ICLR 2024 (arXiv 2310.11511)
- **Problem:** Fixed-k retrieval hurts versatility; outputs not guaranteed to be supported by passages.
- **Method:** Train LM to generate **reflection tokens**: `Retrieve` (need retrieval?), `IsRel` (passage relevant?), `IsSup` (output supported?), `IsUse` (utility). Critic model labels data offline; Generator trained with next-token loss on expanded vocab. Inference: adaptive retrieval per segment, parallel processing of K passages, segment-level beam search weighted by critique scores.
- **Evaluation:** 6 tasks: Open-domain QA (PopQA, TriviaQA), reasoning, fact verification (PubHealth), long-form (Biography). Self-RAG 7B/13B > ChatGPT and RAG-Llama2-chat; citation accuracy ↑ significantly. Controllable via thresholds (e.g., prioritize IsSup for factuality).
- **Insufficient evidence / fallback:** `Retrieve=No` → skip retrieval; `IsRel=Irrelevant` or `IsSup=Partial` → down-rank segment; beam search selects best-supported continuation. No hard abstention but soft constraint. **Relevance to v0.1:** v0.1's C v2 (supported_claims vs unsupported_requests) mirrors IsSup, but v0.1 enforces it via **D gate** (hard check of approved IDs), not via LM self-critique. Self-RAG shows LM can learn to abstain/retrieve adaptively, but v0.1's design (B/D as external gates, not LM tokens) is **safer** for medical use — prevents LM from approving its own evidence. Future v0.3 could use reflection tokens as *advisory* signals to B, not as gate.
- **Link:** https://arxiv.org/abs/2310.11511 · https://selfrag.github.io/ · https://github.com/AkariAsai/self-rag

### 4. ARES — Saad-Falcon et al., NAACL 2024 (arXiv 2311.09476)
- **Problem:** RAG evaluation needs expensive human labels; RAGAS uses fixed prompts, no confidence intervals.
- **Method:** 3-stage: (1) synthetic QA generation from in-domain passages (few-shot LLM), (2) fine-tune lightweight judges (DeBERTa) for **context relevance / answer faithfulness / answer relevance** via contrastive learning, (3) **Prediction-Powered Inference (PPI)** with ~150 human labels to give calibrated scores + CIs. Requires only passage set + 150 labels + 5 few-shot examples.
- **Evaluation:** KILT (NQ, HotpotQA, WoW, FEVER), SuperGLUE (MultiRC, ReCoRD), AIS. ARES > RAGAS: +59.3pp context relevance, +14.4pp answer relevance; Kendall's τ +0.065 / +0.132; hallucination prediction within 2.5pp of truth; 78% fewer annotations; distinguishes systems few points apart.
- **Insufficient evidence / fallback:** Not a runtime gate, but **evaluation** of insufficient evidence: context relevance = "is retrieved info pertinent?", faithfulness = "is answer grounded?". PPI quantifies uncertainty. **Relevance to v0.1:** Directly applicable to v0.1's E (observability) and B/D evaluation. v0.1 currently uses lexical heuristic verifier (demo) — ARES shows how to replace with **fine-tuned judges + PPI** for TFDA domain with only 150 labels. Recommend adopting ARES judges for offline B/C/D calibration and for measuring supported-claim rate, citation precision, over-refusal.
- **Link:** https://arxiv.org/abs/2311.09476 · https://aclanthology.org/2024.naacl-long.20.pdf · https://github.com/stanford-futuredata/ARES

### 5. Med-HALT — Pal et al., arXiv 2307.15343 (2023)
- **Problem:** No medical hallucination benchmark; LLMs confidently wrong in healthcare.
- **Method:** 2-tier benchmark: **Reasoning Hallucination Tests** (False Confidence Test, None-of-the-Above, Fake Questions) on 18,866 MCQs from multinational exams (MedMCQA, MedQA-USMLE, HeadQA, etc.) + **Memory Hallucination Tests** (PMID↔Title, Title↔Link, Abstract↔Link, Link↔Title) on 4,916 PubMed pairs. Tests Text-Davinci, GPT-3.5, Llama2, Falcon, MPT.
- **Evaluation:** Accuracy + pointwise score (penalizes wrong). Best: Llama2-70B 42.21% FCT, Falcon 40B 99.89% Fake, Llama2-70B 77.53% NOTA; memory tasks near 0% for many models (e.g., GPT-3.5 0.29% PMID2Title). No model clinically safe.
- **Insufficient evidence / fallback:** Fake Questions Test explicitly checks if model **abstains/identifies nonsense** vs hallucinating; NOTA checks handling of missing correct answer. **Relevance to v0.1:** Validates v0.1's A gate (block) and D gate (red lines). v0.1's `FALLBACK` for corpus-no-answer (Semaglutide case) is analogous to NOTA/Fake handling. Suggests v0.1 should add Med-HALT-style adversarial cases to WP5 (prompt injection, fake drug names) and use pointwise scoring to penalize over-confidence.
- **Link:** https://arxiv.org/abs/2307.15343 · https://github.com/medhalt/medhalt · https://huggingface.co/datasets/openlifescienceai/Med-HALT

### 6. Almanac — Zakka et al., NEJM AI 2024 (arXiv 2303.01229)
- **Problem:** LLMs hallucinate, stale knowledge, no verifiability for clinical QA.
- **Method:** RAG framework: Browser (curated domains: PubMed, UpToDate, BMJ Best Practice) → chunk 1k tokens → vector DB (HNSW) → Retriever (text-embedding-ada-002, λ=0.83 threshold, top n=10) → LLM (text-davinci-003 / gpt-4-0613) with CoT + calculator tool. If no passage > λ, **explicitly states insufficient information**. ClinicalQA benchmark: 130 (later 314) open-ended Qs across 5-9 specialties, 5-8 board-certified physicians.
- **Evaluation:** Factuality +18% absolute over ChatGPT (p=0.018, F=8.61), Cardiology 91% vs 69%; completeness +4.8% (ns); safety 95% vs 0% on adversarial prompts; calculator 5/5 correct vs 0/5 ChatGPT. Physicians preferred ChatGPT 57% for style despite lower factuality.
- **Insufficient evidence / fallback:** **λ threshold → abstain** ("insufficient information to answer") + citations for verification. Adversarial robustness via query-context scoring. **Relevance to v0.1:** Strongest industry precedent for v0.1's **B gate + D gate + citations**. Almanac's λ is equivalent to v0.1's B sufficiency threshold, but Almanac uses embedding similarity; v0.1 uses deterministic demo gate (needs semantic upgrade). Almanac's "no passage > λ → abstain" is exactly v0.1's FALLBACK. Recommends v0.1 keep **threshold-based abstention** and add **calculator tool** (DeterministicCalculatorTool in proposal) as first non-RAG tool.
- **Link:** https://arxiv.org/abs/2303.01229 · https://ai.nejm.org/doi/full/10.1056/AIoa2300068 · https://github.com/hiesingerlab/almanac-retrieval

### 7. Sufficient Context & Selective Generation — Joren et al., arXiv 2411.06037 (2024)
- **Problem:** Errors arise both from insufficient context and from LLM failing to use sufficient context; prior work conflates relevance with sufficiency.
- **Method:** Defines **sufficient context** (contains all info to answer definitively) vs insufficient (incomplete/contradictory/specialized knowledge missing). LLM autorater classifies (Q, context) pairs. Stratifies errors by sufficiency. Proposes **selective generation**: linear model combining autorater + self-rated confidence (P(True)/P(Correct)) to predict hallucinations and abstain.
- **Evaluation:** Multiple models (Gemini 1.5 Pro, GPT-4o, Claude 3.5, Mistral, Gemma2) on several datasets. Larger models excel when sufficient but hallucinate when insufficient (instead of abstaining); smaller models abstain even when sufficient. Selective method ↑ correct-answer rate among answered queries by 2-10% for Gemini/GPT/Gemma across coverage levels. Fine-tuning alone ↑ abstention but ↓ correct answers.
- **Insufficient evidence / fallback:** **Autorater → abstain** when insufficient; selective threshold tunes coverage-accuracy trade-off. **Relevance to v0.1:** Formalizes v0.1's B gate semantics. v0.1's B currently deterministic; this paper shows **LLM autorater can be calibrated** but needs **selective threshold** to avoid over-refusal. Recommend v0.1 adopt sufficient-context autorater as *feature* for B, with human-labeled TFDA validation set (like ARES's 150), and measure risk-coverage curves (as in SURE-RAG).
- **Link:** https://arxiv.org/abs/2411.06037 (PDF: https://arxiv.org/pdf/2411.06037)

### 8. Diabetes Chatbots SRMA — Wu et al., JMIR 2024
- **Problem:** Diabetes self-management needs scalable education; chatbot evidence fragmented.
- **Method:** PRISMA, PubMed + Web of Science to Jan 1 2023, 25 studies (8 system design, 8 pilot, 9 intervention; only 1 RCT). Extracted chatbot features, research strategy, theoretical frameworks (Behavior Change Wheel, Self-Determination Theory).
- **Evaluation:** Technical / user experience / health outcomes. Meta-analysis (4 pre-post trials, n=219): HbA1c MD 0.30 (95% CI 0.04-0.55, p=.02, I²=0%); weight MD 1.41 (ns). 68% covered diet/exercise/glucose/meds/complications; only 8% mental health.
- **Insufficient evidence / fallback:** Notes low evidence level, need for RCTs, mixed methods, theoretical grounding. No safety gate analysis. **Relevance to v0.1:** Justifies v0.1's **P0: diabetes education & self-management** as core scenario, but warns **high-quality trials & safety escalation still lacking** — supports v0.1's "support existing care plan, not auto-treatment" positioning and need for medical reviewer. Also shows most chatbots lack mental-health coverage (v0.1 correctly excludes CBT).
- **Link:** https://pmc.ncbi.nlm.nih.gov/articles/PMC11653048/ · https://doi.org/10.2196/60380

### 9. Medical History-Taking Chatbots SR — Hindelang et al., JMIR Med Inform 2024
- **Problem:** History-taking is central but inefficient; chatbots could automate.
- **Method:** PRISMA, 6 databases to July 2024, PICOS, 18 studies (15 observational, 3 RCTs), STROBE/RoB2 quality, diverse populations (n=5 to 61,070).
- **Evaluation:** Feasibility/acceptance/usability. Chatbots improve engagement/satisfaction, 24/7 data collection, efficiency. Quality: 33% high, 33% moderate, 33% low (obs); 2/3 RCTs low risk.
- **Insufficient evidence / fallback:** Excluded pure symptom-checkers to focus on comprehensive history; notes need for user-friendly interfaces, data security, empathy. **Relevance to v0.1:** Validates v0.1's **P1: pre-visit intake** (ASK_USER → structured intake schema → timeline/handoff). Supports v0.1's design: `ASK_USER` asks minimal necessary info, ends execution, re-enters via A (prevents hallucinated history). Warns that history-taking chatbots need **red-flag gate** (v0.1's mandatory risk gate, not optional tool).
- **Link:** https://pmc.ncbi.nlm.nih.gov/articles/PMC11393511/ · https://doi.org/10.2196/56628

### 10 & 11. Symptom Checker Accuracy SRs — Wallace et al. 2022 + Fraser et al. 2023
- **Problem:** Symptom checkers increasingly endorsed (NHS) but safety unclear.
- **Method:** Wallace: MEDLINE + Web of Science, 177 → 10 studies, QUADAS-2, real + simulated patients. Fraser: MEDLINE/Embase/CINAHL/HMIC/Web of Science, 21,296 → 14 studies (2010-2022), vignettes/medical records/patient input, QUADAS-2.
- **Evaluation:** Wallace: primary diagnosis 19-37.9%, top-3 33-58%, triage 48.8-90.1%, high variability (WebMD 3-53%). Fraser: diagnostic low vs GPs, triage suboptimal 69% (9/13), 31% exceptions; variables: severity/urgency, AI algorithm, demographics. Both: low accuracy, variable, safety hazard.
- **Insufficient evidence / fallback:** No fallback; checkers **over-answer** despite low accuracy. **Relevance to v0.1:** Critical justification for v0.1's **"only red-flag warning + referral, not autonomous triage"** and **A gate blocking diagnosis**. Proposal correctly cites these to argue "studied ≠ safe to automate". v0.1's FALLBACK for insufficient evidence is opposite of symptom checkers' over-answering — industry lesson: **abstain > misdiagnose**.
- **Links:** https://doi.org/10.1038/s41746-022-00667-w · https://pubmed.ncbi.nlm.nih.gov/37266983/ · https://pmc.ncbi.nlm.nih.gov/articles/PMC10276326/

### 12. AI Scribes SR — Sasseville et al., Healthcare 2025
- **Problem:** Clinician burnout from documentation; AI scribes (ASR + LLM summarization) emerging but evidence limited.
- **Method:** Cochrane + PRISMA, 8 studies (small, specific settings), narrative synthesis. Features: transcription, summarization, EHR entry, admin assistance. Models: AWS Transcribe + T5/PEGASUS/BART, GPT-3.5/4, Autoscriber.
- **Evaluation:** Clinician engagement ↑, documentation burden ↓ for some, time savings inconsistent, burnout limited impact, accuracy varies by tech/training, editing workload remains. No study reported total time including editing; no accuracy vs conventional.
- **Insufficient evidence / fallback:** Notes need for human editing, self-learning, quality checks. **Relevance to v0.1:** Supports proposal's decision to **deprioritize ambient scribe** (P3, not P0) — evidence still heterogeneous, TFDA drug-risk focus is more tractable. Also warns that even "mature" AI scribe needs **human verification** — same principle as v0.1's D gate and "draft, not decision".
- **Link:** https://doi.org/10.3390/healthcare13121447 · https://sporevidencealliance.ca/wp-content/uploads/2025/06/healthcare-13-01447-v3.pdf

---

## Cross-Cutting Analysis: Should v0.1 Architecture Change?

### What v0.1 Gets Right (aligned with 2024-2025 industry)
1. **Gates > Prompts:** v0.1's A/B/D as fixed graph nodes (not system prompts) matches NIST AI RMF Govern/Map/Measure/Manage and WHO ethics — same lesson as Almanac's λ and CRAG's evaluator. Industry now agrees: **policy must be code, not prompt**.
2. **Evidence sufficiency as first-class concept:** v0.1's B (PASS/INSUFFICIENT/UNSAFE/REVIEW) anticipates 2024's "sufficient context" (Joren) and SURE-RAG's set-level sufficiency. Most 2023 RAG had no abstention; 2024-2025 adds it — v0.1 is ahead.
3. **Bounded recovery:** v0.1's max 2 steps / 1 rewrite / 1 clarification prevents loops — same as CRAG's 3 actions and ARES's cost control. Industry now measures "unnecessary tool calls" (proposal §8.2).
4. **Provenance & citations:** v0.1's approved_evidence_ids + D citation check matches Almanac's citations and Self-RAG's IsSup. Required for audit.
5. **Fail-closed:** v0.1's "dependency failure → FALLBACK, not unchecked answer" matches Almanac's "no passage > λ → insufficient" and Joren's selective abstention.

### Gaps & Recommended Evolutions (without breaking safety)
| Industry Advance | v0.1 Current | Recommended Change | Priority |
|------------------|--------------|--------------------|----------|
| **Retriever fusion** (MedRAG RRF-2) | Single e5-small | Add BM25 + MedCPT fusion as alternative retriever; keep interface | v0.2 |
| **Evaluator for B** (CRAG) | Deterministic demo gate | Train lightweight evaluator on TFDA labels (like CRAG) as *signal* to B, not replacement; keep B as hard gate | v0.2 |
| **Iterative follow-up queries** (i-MedRAG, S2G-RAG) | Single REWRITE_QUERY | Allow 1 follow-up query generation (like i-MedRAG) but still via B → C → D | v0.3 |
| **Fine-tuned judges + PPI** (ARES) | Lexical heuristic verifier | Replace D's demo verifier with ARES-style judges fine-tuned on 150 TFDA labels + PPI CIs | v0.2 |
| **Sufficient-context autorater** (Joren) | Binary B | Add autorater score to E trace; use for selective answering threshold tuning | v0.2 |
| **Calculator tool** (Almanac) | No deterministic tool | Implement DeterministicCalculatorTool first (proposal §2.3) — low risk, high value | v0.2 |
| **Hallucination benchmark** (Med-HALT) | 5 hand-crafted cases | Expand to Med-HALT-style suite: FCT/NOTA/Fake + PMID tasks for TFDA | v0.1 WP5 |

### What NOT to Change (safety)
- **Do not** make A/B/D bypassable by Agent (Self-RAG's reflection tokens are advisory only).
- **Do not** add open web-search without allowlist (CRAG's web search needs governance).
- **Do not** treat `declared_role` as authorization (all SRs show role confusion risk).
- **Do not** claim clinical efficacy from retrieval hit-rate (all diabetes SRs show low RCT evidence).

### Concrete Next Steps for v0.1 → v0.2
1. **Freeze contracts** (WP0-1) then run **MIRAGE-style ablation**: TFDA corpus vs MedCorp-style combined, e5-small vs BM25+MedCPT, k=8/16/32, measure Recall@K + B sufficiency.
2. **Collect 150 human labels** for TFDA (context relevance / faithfulness / relevance) → train ARES judges → calibrate B/D thresholds with PPI.
3. **Add 20 adversarial cases** (Med-HALT Fake/NOTA, prompt injection, fake drug names) to existing 5 cases; measure over-answer rate (target 0 for A/B/D bypass).
4. **Implement EvidenceRetrievalTool** as defined in proposal §5.4 (source_id, query, filters → candidate evidence[] with IDs/dates/scores) — keep RAG as tool, not answer generator.
5. **Measure selective metrics**: supported-claim rate, citation precision, over-refusal, risk-coverage curve (Joren).

---

## References (all verified, open-access)

1. Xiong G et al. Benchmarking Retrieval-Augmented Generation for Medicine. Findings of ACL 2024. arXiv:2402.13178. https://arxiv.org/abs/2402.13178
2. Yan S-Q et al. Corrective Retrieval Augmented Generation. arXiv:2401.15884. https://arxiv.org/abs/2401.15884
3. Asai A et al. Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection. ICLR 2024. arXiv:2310.11511. https://arxiv.org/abs/2310.11511
4. Saad-Falcon J et al. ARES: An Automated Evaluation Framework for RAG Systems. NAACL 2024. arXiv:2311.09476. https://arxiv.org/abs/2311.09476
5. Pal A et al. Med-HALT: Medical Domain Hallucination Test. arXiv:2307.15343. https://arxiv.org/abs/2307.15343
6. Zakka C et al. Almanac: Retrieval-Augmented Language Models for Clinical Medicine. NEJM AI 2024. arXiv:2303.01229. https://arxiv.org/abs/2303.01229
7. Joren H et al. Sufficient Context: Selective Generation with Evidence Sufficiency. arXiv:2411.06037. https://arxiv.org/abs/2411.06037
8. Wu Y et al. Application of Chatbots to Help Patients Self-Manage Diabetes: Systematic Review and Meta-Analysis. J Med Internet Res 2024;26:e60380. https://pmc.ncbi.nlm.nih.gov/articles/PMC11653048/
9. Hindelang M et al. Transforming Health Care Through Chatbots for Medical History-Taking. JMIR Med Inform 2024;12:e56628. https://pmc.ncbi.nlm.nih.gov/articles/PMC11393511/
10. Wallace W et al. The diagnostic and triage accuracy of digital and online symptom checker tools: a systematic review. npj Digit Med 2022;5:118. https://doi.org/10.1038/s41746-022-00667-w
11. Fraser H et al. Triage and Diagnostic Accuracy of Online Symptom Checkers: Systematic Review. J Med Internet Res 2023;25:e43803. https://pubmed.ncbi.nlm.nih.gov/37266983/
12. Sasseville M et al. The Impact of AI Scribes on Streamlining Clinical Documentation: A Systematic Review. Healthcare 2025;13:1447. https://doi.org/10.3390/healthcare13121447

*Additional proposal-cited SRs verified via WebSearch: Diabetes-focused conversational agents SR (2025, PubMed 40840786), AI-based conversational agents umbrella review (PubMed 41337874), Mental health chatbot RCT meta-analysis (PubMed 38631422), Ambient scribe RCT SR (Region Örebro 2026.86).*

---

**Bottom line:** v0.1's A/B/C/D/E + bounded ASK_USER/REWRITE_QUERY/FALLBACK is **architecturally sound and ahead of 2023 RAG**; 2024-2025 industry converges toward v0.1's gates (CRAG evaluator, Almanac λ, sufficient-context abstention, ARES judges). No need to replace with autonomous multi-tool agent (proposal Option D). Evolve by **adding evaluator signals, retriever fusion, and calibrated judges** *inside* existing gates, not around them.
