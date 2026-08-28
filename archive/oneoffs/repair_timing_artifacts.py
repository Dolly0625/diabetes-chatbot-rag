"""Repair timing fields written before the rolling-window wait double-count fix."""

from __future__ import annotations

import json
from pathlib import Path


def repair_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    rows = []
    changed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        timing = row.get("timing")
        if isinstance(timing, dict) and all(
            timing.get(key) is not None
            for key in ("model_latency", "retry_wait_time", "total_wall_time")
        ):
            actual_wait = max(
                0.0,
                float(timing["total_wall_time"])
                - float(timing["model_latency"])
                - float(timing["retry_wait_time"]),
            )
            if abs(actual_wait - float(timing.get("rate_limit_wait_time", 0.0))) > 1e-6:
                timing["rate_limit_wait_time"] = actual_wait
                changed += 1
        rows.append(row)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return changed


if __name__ == "__main__":
    import os

    run_dir = Path(os.getenv("TFDA_RUN_DIR", "runs/c_nemotron_20260818"))
    if not run_dir.is_absolute():
        run_dir = Path(__file__).resolve().parent / run_dir
    result_dir = run_dir / "results"
    total = 0
    for filename in ("smoke_outputs.jsonl", "generator_outputs.jsonl", "llm_judge_evaluations.jsonl"):
        total += repair_jsonl(result_dir / filename)
    print(f"repaired timing rows: {total}")

