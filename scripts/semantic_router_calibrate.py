#!/usr/bin/env python3
"""Calibration: threshold sweep on calibration split, family-leakage check, 4-tier selection.

Usage:
  python scripts/semantic_router_calibrate.py --dataset experiments/semantic_router_production/dataset.json
  python scripts/semantic_router_calibrate.py --json-output /tmp/calib.json --output docs/reviews/semantic_router_production_eval_calibration.md
  python -m scripts.semantic_router_calibrate --help

Uses the same embedding path as production via ``tfda_context_gate.semantic_router.factory.build_semantic_router``.
When Ollama is unavailable, falls back to DeterministicFakeEmbedder and marks BLOCKED in the report.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Ensure project root on sys.path when run as `python scripts/...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tfda_context_gate.semantic_router.config import ROUTE_LABELS
from tfda_context_gate.semantic_router.eval_common import (
    benchmark_embedding,
    check_family_leakage,
    distribution_by_split,
    load_dataset,
    score_rows,
    select_recommended,
    split_rows,
    threshold_sweep,
    top_confusions,
)
from tfda_context_gate.semantic_router.factory import build_semantic_router


def _resolve_production_dataset_arg(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value)


def _build_router_and_backend():
    # Use factory's single source of truth; factory already probes Ollama and falls back to fake
    router = build_semantic_router()
    # Derive backend info from router + factory resolution
    from tfda_context_gate.semantic_router.factory import _resolve_model_and_base, _probe_ollama
    import os
    try:
        model_name, base_url, raw = _resolve_model_and_base()
    except Exception:
        model_name, base_url, raw = "unknown", "unknown", "unknown"
    # Source string
    try:
        from tfda_context_gate.rag.tfda_retriever import DEFAULT_EMBEDDING_MODEL as _DEF
        _default = _DEF
    except Exception:
        _default = "ollama/bge-m3:latest"
    if os.getenv("OLLAMA_EMBED_MODEL"):
        model_source = "env:OLLAMA_EMBED_MODEL"
    elif os.getenv("EMBED_MODEL"):
        model_source = "env:EMBED_MODEL"
    else:
        model_source = "existing:tfda_context_gate.rag.tfda_retriever.DEFAULT_EMBEDDING_MODEL"
    from urllib.parse import urlsplit
    host = urlsplit(base_url).hostname or "unknown"
    degraded = bool(getattr(router, "degraded", False))
    # Probe for availability_check text
    available = _probe_ollama(model_name, base_url) if not degraded else False
    if degraded:
        backend = "deterministic_fake_harness_only"
        availability_check = "Ollama unavailable or PYTEST_CURRENT_TEST set — using fake embedder (BLOCKED mode)"
        blocked = True
    else:
        backend = "ollama"
        availability_check = "local Ollama model is listed" if available else "Ollama probe fell back to live embed attempt"
        blocked = False
        # double-check degraded flag overrides
        if getattr(router, "degraded", False):
            backend = "deterministic_fake_harness_only"
            blocked = True
    return router, {
        "requested_model": raw if 'raw' in locals() else model_name,
        "model_name": model_name,
        "base_url": base_url,
        "model_source": model_source,
        "backend": backend,
        "availability_check": availability_check,
        "endpoint_host": host,
        "blocked": blocked,
    }


def _pct(v: float) -> str:
    return f"{v*100:.1f}%"


def _recommendation_table(recommendations):
    lines = [
        "| policy | thresholds | macro F1 | micro F1 | MIXED recall | coverage | fallback | false-fast |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for policy in ("cosine", "margin", "hybrid"):
        row = recommendations[policy]
        if policy == "hybrid":
            thresholds = f"cos={row['cosine_threshold']:.2f}, margin={row['margin_threshold']:.2f}"
        else:
            thresholds = f"{row['threshold']:.2f}"
        lines.append(
            f"| {policy} | {thresholds} | {_pct(float(row['macro_f1']))} | {_pct(float(row['micro_f1']))} | {_pct(float(row['mixed_recall']))} | {_pct(float(row['high_confidence_coverage']))} | {_pct(float(row['abstain_llm_fallback_rate']))} | {int(row['false_fast_route_count'])} |"
        )
    return "\n".join(lines)


def render_calibration_markdown(results) -> str:
    backend = results["backend"]
    leakage = results["leakage"]
    dist = results["distribution"]
    blocked_note = (
        "⚠️ **BLOCKED**：本次沒有可用的本機 bge-m3；以下數字僅為 deterministic fake harness plumbing，不得作為語意可行性結論。"
        if backend["blocked"]
        else "✅ 本次使用專案既有本機 Ollama embedding；沒有呼叫生成式 LLM。"
    )
    ds_path = results["dataset_path"]
    latency = results["latency"]
    recommendations = results["recommendations"]
    chosen = recommendations["hybrid"]
    chosen_metrics = chosen  # same object

    lines = [
        "# Semantic Router 生產校準報告（calibration）",
        "",
        f"> 產生時間：{results['generated_at']}  |  資料集：`{ds_path}`  |  版本：`{results['version']}`",
        "> 指令：`python scripts/semantic_router_calibrate.py --dataset experiments/semantic_router_production/dataset.json --output docs/reviews/semantic_router_production_eval_calibration.md --json-output /tmp/semantic_router_calibration.json`",
        "",
        blocked_note,
        "",
        "## 1. 家族切分與洩漏檢查",
        "",
        f"- Backend: `{backend['backend']}`；模型：`{backend['requested_model']}`（source: `{backend['model_source']}`）；host：`{backend['endpoint_host']}`。",
        f"- Leakage 檢查：{'❌ FAIL — 同一 family_id 跨 split' if leakage['has_leak'] else '✅ PASS — 無跨 split 洩漏'}；{leakage['note']}。",
    ]
    if leakage.get("has_family_id"):
        lines.append(f"- 家族總數：{leakage.get('family_count', '—')}；洩漏家族：{leakage.get('leak_families', [])[:10]}")
        if leakage.get("leak_details"):
            lines.append(f"  - 詳細：`{json.dumps(leakage['leak_details'], ensure_ascii=False)[:400]}`")
    else:
        lines.append(f"- 注意：{leakage.get('note','')} — 建議補齊 family_id 以啟用防洩漏保障。")
    lines.extend([
        "",
        "### 每 split 分佈",
        "",
        "| split | count | families | labels | sources |",
        "|---|---:|---:|---|---|---|",
    ])
    for split in sorted(k for k in dist.keys() if not k.startswith("_")):
        info = dist[split]
        lines.append(f"| {split} | {info['count']} | {info.get('families','—')} | `{json.dumps(info['labels'], ensure_ascii=False)}` | `{json.dumps(info['sources'], ensure_ascii=False)}` |")
    total = dist.get("_total", {})
    lines.append(f"| **total** | **{total.get('count','—')}** | **{total.get('families','—')}** | `{json.dumps(total.get('labels',{}), ensure_ascii=False)}` | — |")
    lines.extend([
        "",
        f"- calibration 集大小：{results['calibration_count']}；holdout：{results.get('holdout_count','—')}；train：{results.get('train_count','—')}；boundary：{results['boundary_count']}",
        "",
        "## 2. Embedding 延遲",
        "",
        "| phase | p50 | p95 | rounds |",
        "|---|---:|---:|---:|",
        f"| cold first query | {latency['cold_p50_ms']:.1f} ms | {latency['cold_p95_ms']:.1f} ms | 1 |",
        f"| warm query | {latency['warm_p50_ms']:.1f} ms | {latency['warm_p95_ms']:.1f} ms | {latency['warm_rounds']} |",
        "",
        "## 3. 校準閾值擇優（calibration split，四階規則）",
        "",
        "擇優規則依序：`MIXED recall≥75% & false-fast=0` → `MIXED≥75%` → `false-fast=0` → `max macro_F1`；`UNKNOWN` 表示 abstain 交回 LLM。",
        "",
        _recommendation_table(recommendations),
        "",
        "最大 MIXED recall（診斷用，非安全推薦）：",
        "",
        "| policy | threshold(s) | max MIXED recall | macro F1 | coverage | false-fast |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for policy in ("cosine", "margin", "hybrid"):
        row = results["best_mixed_recall"][policy]
        if policy == "hybrid":
            thresholds = f"cos={row['cosine_threshold']:.2f}, margin={row['margin_threshold']:.2f}"
        else:
            thresholds = f"{row['threshold']:.2f}"
        lines.append(f"| {policy} | {thresholds} | {_pct(float(row['mixed_recall']))} | {_pct(float(row['macro_f1']))} | {_pct(float(row['high_confidence_coverage']))} | {int(row['false_fast_route_count'])} |")
    lines.extend([
        "",
        "Per-class 指標（chosen hybrid 在 calibration 集）：",
        "",
        "| label | precision | recall | F1 | support |",
        "|---|---:|---:|---:|---:|",
    ])
    per_class = chosen_metrics["per_class"]
    for label in ROUTE_LABELS:
        item = per_class[label]
        lines.append(f"| {label} | {_pct(float(item['precision']))} | {_pct(float(item['recall']))} | {_pct(float(item['f1']))} | {int(item['support'])} |")
    lines.extend([
        "",
        f"- High-confidence coverage：{_pct(float(chosen_metrics['high_confidence_coverage']))}",
        f"- Abstain / LLM fallback：{_pct(float(chosen_metrics['abstain_llm_fallback_rate']))}",
        f"- False-fast：**{int(chosen_metrics['false_fast_route_count'])}**（prediction != UNKNOWN 且 != gold）",
        f"- MIXED recall：{_pct(float(chosen_metrics['mixed_recall']))}",
        "",
        "### 校準推薦閾值（供 holdout 評估使用）",
        "",
        f"- 推薦 policy：`{results['chosen_policy']}`",
    ])
    # threshold display
    if results["chosen_policy"] == "hybrid":
        lines.append(f"- `cosine_threshold={chosen['cosine_threshold']:.2f}, margin_threshold={chosen['margin_threshold']:.2f}`")
    else:
        lines.append(f"- `threshold={chosen.get('threshold', '—')}`")
    lines.extend([
        f"- 建議後續以此閾值執行：`python scripts/semantic_router_evaluate.py --dataset {ds_path} --cosine-threshold {chosen.get('cosine_threshold', chosen.get('threshold', 0.62)):.2f} --margin-threshold {chosen.get('margin_threshold', 0.10):.2f} --policy {results['chosen_policy']} --split holdout`",
        "",
        "## 4. 混淆案例（calibration 集，chosen hybrid）",
        "",
    ])
    if results["confusions"]:
        lines.extend(["| id | gold | prediction | top score | margin | family | text |", "|---|---|---|---:|---:|---|---|"])
        for item in results["confusions"]:
            lines.append(f"| {item['id']} | {item['gold']} | {item['prediction']} | {item['top_score']:.4f} | {item['margin']:.4f} | {item.get('family_id','—')} | {item['text'][:60]} |")
    else:
        lines.append("在此閾值下 calibration 集無混淆（或皆為 UNKNOWN abstain）。")
    lines.extend([
        "",
        "## 5. 下一步",
        "",
        "- 請執行 `python scripts/semantic_router_evaluate.py --split holdout` 在 holdout 上驗證 guarded 門檻（false-fast=0、紅旗漏攔=0、MIXED→PURE=0、SUBJECT_CHANGE/CORRECTION 不得快寫入）；若不通過，報告須誠實寫明「建議僅 shadow」且脚本以 exit 2 標記 blocked。",
        "- 若需 deterministic 複現：`PYTEST_CURRENT_TEST=1 python scripts/semantic_router_calibrate.py --json-output /tmp/calib_fake.json`（報告將標示 BLOCKED）。",
        "",
        "## 6. 復現指令",
        "",
        "```bash",
        f"python scripts/semantic_router_calibrate.py --dataset {ds_path} --output docs/reviews/semantic_router_production_eval_calibration.md --json-output /tmp/semantic_router_calibration.json",
        f"python scripts/semantic_router_evaluate.py --dataset {ds_path} --split holdout --json-output /tmp/semantic_router_holdout.json --output docs/reviews/semantic_router_production_eval_holdout.md",
        "PYTEST_CURRENT_TEST=1 python scripts/semantic_router_calibrate.py --json-output /tmp/calib_fake.json  # fake 模式複現",
        "```",
        "",
        f"_機器生成報告 — backend blocked={backend['blocked']} — 請勿手動竄改閾值造假_",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=str, default=None, help="path to dataset.json (default: experiments/semantic_router_production/dataset.json)")
    parser.add_argument("--output", type=Path, default=None, help="write Markdown report")
    parser.add_argument("--json-output", type=Path, default=None, help="write machine-readable metrics")
    parser.add_argument("--policy", type=str, default="hybrid", choices=["cosine", "margin", "hybrid", "all"], help="which policy to report as chosen (default hybrid)")
    args = parser.parse_args()

    dataset_path = _resolve_production_dataset_arg(args.dataset)
    dataset = load_dataset(dataset_path)
    primary = dataset["primary"]
    boundary = dataset["boundary_comparison"]

    leakage = check_family_leakage(primary)
    if leakage["has_leak"]:
        # Honest FAIL — still produce report but exit 1 to signal leakage
        print(f"[LEAKAGE FAIL] families crossing splits: {leakage['leak_details']}", file=sys.stderr)

    distribution = distribution_by_split(primary)
    calibration_rows_raw = split_rows(primary, "calibration")
    # fallback: if no split column, use all primary as calibration (honest note)
    if not any("split" in r for r in primary):
        calibration_rows_raw = list(primary)
    train_rows = split_rows(primary, "train")
    holdout_rows = split_rows(primary, "holdout")

    router, backend = _build_router_and_backend()
    latency = benchmark_embedding(router.embedder)

    scored = score_rows(router, calibration_rows_raw)
    sweeps = {}
    recommendations = {}
    for policy in ("cosine", "margin", "hybrid"):
        sweeps[policy] = threshold_sweep(scored, policy)
        recommendations[policy] = select_recommended(sweeps[policy])
    best_mixed = {
        policy: dict(max(sweeps[policy], key=lambda r: (float(r["mixed_recall"]), float(r["macro_f1"]))))
        for policy in ("cosine", "margin", "hybrid")
    }
    chosen_policy = args.policy if args.policy != "all" else "hybrid"
    if chosen_policy not in ("cosine", "margin", "hybrid"):
        chosen_policy = "hybrid"
    chosen = recommendations[chosen_policy]
    # confusions at chosen
    confusions = top_confusions(scored, policy=chosen_policy, recommended=chosen, limit=20)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    results = {
        "generated_at": generated_at,
        "dataset_path": dataset["_path"],
        "version": dataset["version"],
        "description": dataset["description"],
        "backend": backend,
        "leakage": leakage,
        "distribution": distribution,
        "latency": latency,
        "calibration_count": len(calibration_rows_raw),
        "train_count": len(train_rows),
        "holdout_count": len(holdout_rows),
        "boundary_count": len(boundary),
        "sweeps": sweeps,
        "recommendations": recommendations,
        "best_mixed_recall": best_mixed,
        "chosen_policy": chosen_policy,
        "chosen": chosen,
        "confusions": confusions,
        "predictions": [],  # not dumping per-row for calibration brevity
    }

    # Console JSON summary (compact)
    print(json.dumps({
        "backend": backend,
        "leakage": {"has_leak": leakage["has_leak"], "note": leakage["note"]},
        "chosen_policy": chosen_policy,
        "chosen": chosen,
        "recommendations": recommendations,
        "distribution": distribution,
    }, ensure_ascii=False, indent=2))

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        # dump full results (sweeps may be large)
        args.json_output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[calibrate] JSON written to {args.json_output}", file=sys.stderr)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_calibration_markdown(results), encoding="utf-8")
        print(f"[calibrate] Markdown written to {args.output}", file=sys.stderr)

    # Exit code: 0 unless leakage
    if leakage["has_leak"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
