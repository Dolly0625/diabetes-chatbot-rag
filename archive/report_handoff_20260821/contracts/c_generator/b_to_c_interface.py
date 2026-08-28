from __future__ import annotations

import json
import os
from pathlib import Path

from tfda_context_gate.c_generator.experiment_cases import build_case_specs
from tfda_context_gate.c_generator.hard_experiment_cases import build_hard_case_specs


def build_interface(b_run_dir: Path, output_path: Path) -> list[dict]:
    docs_path = b_run_dir / "data" / "processed" / "langchain_documents.json"
    phase5_path = b_run_dir / "results" / "hybrid_narrow_top4.json"
    documents = json.loads(docs_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in documents}

    phase5 = json.loads(phase5_path.read_text(encoding="utf-8"))
    s1_rows = phase5["runs"][0]["context_rows"]
    s1_ids = [row["document_id"] for row in s1_rows]

    case_builder = build_hard_case_specs if os.getenv("C_CASE_SET", "baseline") == "hard" else build_case_specs
    interface_cases: list[dict] = []
    for spec in case_builder():
        context_ids = s1_ids if spec.case_id == "S1" else list(spec.context_ids)
        for document_id in context_ids:
            if document_id not in by_id:
                raise RuntimeError(f"C fixture references missing B document: {document_id}")
        contexts = []
        for document_id in context_ids:
            item = by_id[document_id]
            metadata = item.get("metadata", {})
            contexts.append(
                {
                    "document_id": document_id,
                    "row_index": metadata.get("row_index"),
                    "發布日期": metadata.get("發布日期", ""),
                    "藥品成分": metadata.get("藥品成分", ""),
                    "page_content": item["page_content"],
                    "source_dataset": metadata.get("source_dataset", ""),
                }
            )
        interface_cases.append(
            {
                "case_id": spec.case_id,
                "case_type": spec.case_type,
                "query": spec.query,
                "b_decision": spec.b_decision,
                "stress_test": spec.stress_test,
                "approved_document_ids": list(spec.approved_ids),
                "context_document_ids": context_ids,
                "contexts": contexts,
                "ground_truth": {
                    "source": "manual",
                    "expected_decision": getattr(
                        spec,
                        "expected_decision",
                        "INSUFFICIENT" if spec.case_type == "insufficient" else "ANSWER",
                    ),
                    "expected_handling": spec.expected_handling,
                    "supported_facts": list(spec.supported_facts),
                    "unavailable_facts": list(spec.unavailable_facts),
                    "expected_evidence_ids": list(spec.approved_ids),
                },
                "provenance": {
                    "b_run_dir": str(b_run_dir),
                    "b_phase5_reference": str(phase5_path) if spec.case_id == "S1" else None,
                    "fixture_note": (
                        "S1 uses the actual B narrow_top4 context and PASS result. "
                        "Other cases use manually specified B-to-C interface fixtures "
                        "over the same TFDA corpus; no new retrieval result is claimed."
                    ),
                },
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(interface_cases, ensure_ascii=False, indent=2), encoding="utf-8")
    return interface_cases


if __name__ == "__main__":
    import os
    from tfda_context_gate.run_config import RESULTS_DIR

    b_dir = Path(os.getenv("C_B_RUN_DIR", "runs/b_nemotron_20260818"))
    if not b_dir.is_absolute():
        b_dir = Path(__file__).resolve().parents[1] / b_dir
    out = Path(os.getenv("C_INTERFACE_PATH", str(RESULTS_DIR / "interface_cases.json")))
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[1] / out
    cases = build_interface(b_dir, out)
    print(f"wrote {len(cases)} B-to-C interface cases to {out}")
