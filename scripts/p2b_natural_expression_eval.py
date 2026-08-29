#!/usr/bin/env python3
"""Print the P2B phrasing golden set without starting an LLM or workflow.

This is a tiny demo/eval harness for comparing deterministic response
composition across revisions.  It intentionally contains synthetic, non-PII
inputs only and checks that a phrasing change cannot add clinical claims.
Run from the repository root:

    python3 scripts/p2b_natural_expression_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tfda_context_gate.line_orchestration.response_composer import (
    compose_intake_question,
    compose_side_answer,
)


GOLDEN_CASES = (
    {"id": "medication", "field": "known_medications"},
    {"id": "allergy", "field": "allergies"},
    {"id": "onset", "field": "symptom_onset"},
    {"id": "doctor_question", "field": "questions_for_doctor"},
)
_BANNED_CLAIM_TOKENS = ("診斷", "劑量", "療效承諾", "治癒")


def evaluate() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case in GOLDEN_CASES:
        question = compose_intake_question(str(case["field"])) or ""
        cases.append(
            {
                **case,
                "output": question,
                "pass": (
                    bool(question)
                    and "第" not in question
                    and question.count("？") == 1
                    and not any(token in question for token in _BANNED_CLAIM_TOKENS)
                ),
            }
        )

    side_answer = compose_side_answer(
        "這題依既有衛教資料整理如下。",
        compose_intake_question("allergies"),
    )
    cases.append(
        {
            "id": "side_answer_return",
            "output": side_answer,
            "pass": "資料已保留" in side_answer and "繼續整理" in side_answer,
        }
    )
    return {"suite": "p2b-natural-expression", "cases": cases, "all_pass": all(bool(item["pass"]) for item in cases)}


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
