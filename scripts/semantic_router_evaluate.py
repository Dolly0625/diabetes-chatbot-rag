#!/usr/bin/env python3
"""Evaluate production semantic router dataset with family-split & leakage checks.

Supports:
  --dataset PATH           dataset.json (default: experiments/semantic_router_production/dataset.json)
  --split {all,train,calibration,holdout}
  --family-split           enforce family integrity (fail if family crosses splits)
  --check-leakage          enable text-similarity leakage warning (>0.95)
  --leak-threshold FLOAT   similarity threshold for text-level leakage (default 0.95)
  --cosine-threshold FLOAT fixed threshold (if given, skip sweep)
  --margin-threshold FLOAT fixed threshold
  --policy {cosine,margin,hybrid}
  --output PATH            Markdown report
  --json-output PATH       JSON metrics

Examples:
  python scripts/semantic_router_evaluate.py --split holdout --family-split --check-leakage
  python scripts/semantic_router_evaluate.py --dataset experiments/semantic_router_production/dataset.json --json-output /tmp/eval.json
  PYTEST_CURRENT_TEST=1 python scripts/semantic_router_evaluate.py --split all --json-output /tmp/fake.json

No LLM is invoked. Embedding uses factory (Ollama bge-m3 or fake when blocked).
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tfda_context_gate.semantic_router.config import ROUTE_LABELS
from tfda_context_gate.semantic_router.eval_common import (
    benchmark_embedding,
    check_family_leakage,
    distribution_by_split,
    evaluate_boundary,
    guarded_checks,
    load_dataset,
    metrics_for,
    score_rows,
    select_recommended,
    split_rows,
    threshold_sweep,
    top_confusions,
)
from tfda_context_gate.semantic_router.factory import build_semantic_router


def _text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


def check_text_leakage(primary, threshold: float = 0.95) -> dict:
    splits = {}
    for row in primary:
        sp = str(row.get("split", "all"))
        splits.setdefault(sp, []).append(row)
    warnings = []
    split_names = sorted(splits.keys())
    for i, s1 in enumerate(split_names):
        for s2 in split_names[i + 1:]:
            for r1 in splits[s1]:
                for r2 in splits[s2]:
                    sim = _text_similarity(r1["text"], r2["text"])
                    if sim > threshold:
                        warnings.append({
                            "text1_id": r1["id"],
                            "text2_id": r2["id"],
                            "split1": s1,
                            "split2": s2,
                            "similarity": round(sim, 4),
                            "text1": r1["text"][:40],
                            "text2": r2["text"][:40],
                            "family1": r1.get("family_id", ""),
                            "family2": r2.get("family_id", ""),
                        })
                    if r1["text"].strip() == r2["text"].strip() and sim <= threshold:
                        warnings.append({
                            "text1_id": r1["id"],
                            "text2_id": r2["id"],
                            "split1": s1,
                            "split2": s2,
                            "similarity": 1.0,
                            "text1": r1["text"][:40],
                            "text2": r2["text"][:40],
                            "family1": r1.get("family_id", ""),
                            "family2": r2.get("family_id", ""),
                            "note": "exact duplicate across splits",
                        })
    return {"warnings": warnings, "count": len(warnings), "threshold": threshold}


def _build_router_and_backend():
    router = build_semantic_router()
    from tfda_context_gate.semantic_router.factory import _resolve_model_and_base, _probe_ollama
    import os
    from urllib.parse import urlsplit
    try:
        model_name, base_url, raw = _resolve_model_and_base()
    except Exception:
        model_name, base_url, raw = "unknown", "unknown", "unknown"
    if os.getenv("OLLAMA_EMBED_MODEL"):
        model_source = "env:OLLAMA_EMBED_MODEL"
    elif os.getenv("EMBED_MODEL"):
        model_source = "env:EMBED_MODEL"
    else:
        model_source = "existing:tfda_context_gate.rag.tfda_retriever.DEFAULT_EMBEDDING_MODEL"
    host = urlsplit(base_url).hostname or "unknown"
    degraded = bool(getattr(router, "degraded", False))
    available = _probe_ollama(model_name, base_url) if not degraded else False
    if degraded:
        backend = "deterministic_fake_harness_only"
        blocked = True
        availability_check = "Ollama unavailable or PYTEST_CURRENT_TEST set — using fake embedder (BLOCKED mode)"
    else:
        backend = "ollama"
        blocked = False
        availability_check = "local Ollama model is listed" if available else "Ollama probe fell back to live embed attempt"
        if getattr(router, "degraded", False):
            backend = "deterministic_fake_harness_only"
            blocked = True
    return router, {
        "requested_model": raw,
        "model_name": model_name,
        "base_url": base_url,
        "model_source": model_source,
        "backend": backend,
        "availability_check": availability_check,
        "endpoint_host": host,
        "blocked": blocked,
    }


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def render_markdown(results) -> str:
    backend = results["backend"]
    leakage = results["leakage"]
    text_leak = results["text_leakage"]
    dist = results["distribution"]
    blocked_note = (
        "⚠️ **BLOCKED**：本次沒有可用的本機 bge-m3；以下數字僅為 deterministic fake harness plumbing，不得作為語意可行性結論。"
        if backend["blocked"]
        else "✅ 本次使用專案既有本機 Ollama embedding；沒有呼叫生成式 LLM。"
    )
    lines = [
        "# Semantic Router production evaluation",
        "",
        f"> Dataset: `{results['dataset_path']}` | Version: `{results['version']}` | Split evaluated: `{results['evaluated_split']}`",
        f"> Generated: {results['generated_at']} | Family-split: `{results['family_split']}` | Leakage threshold: `{text_leak['threshold']}`",
        "",
        blocked_note,
        "",
        "## 1. Dataset & family split",
        "",
        f"- Total primary: {results['total_primary']} | Evaluated rows: {results['evaluated_count']} | Families: {results['family_count']} | Boundary: {results['boundary_count']}",
        f"- Family leakage: {'❌ FAIL — 同一 family_id 跨 split' if leakage['has_leak'] else '✅ PASS — 無家族跨集合洩漏'}；{leakage['note']}",
    ]
    if leakage.get("family_count"):
        lines.append(f"  - 家族總數：{leakage['family_count']}")
    if text_leak["count"] > 0:
        lines.append(f"- Text similarity leakage (> {text_leak['threshold']}): ⚠️ {text_leak['count']} warnings — 近似改寫疑似跨集合！")
        for w in text_leak["warnings"][:10]:
            lines.append(f"  - `{w['text1_id']}` ({w['split1']}) ↔ `{w['text2_id']}` ({w['split2']}) sim={w['similarity']} | {w['text1']} | {w['text2']}")
    else:
        lines.append(f"- Text similarity leakage (> {text_leak['threshold']}): ✅ 0 warnings")
    lines.extend([
        "",
        "### 每 split 分佈",
        "",
        "| split | count | families | labels |",
        "|---|---:|---:|---|",
    ])
    for split in sorted(k for k in dist.keys() if not k.startswith("_")):
        info = dist[split]
        lines.append(f"| {split} | {info['count']} | {info.get('families','—')} | `{json.dumps(info['labels'], ensure_ascii=False)}` |")
    total = dist.get("_total", {})
    lines.append(f"| **total** | **{total.get('count','—')}** | **{total.get('families','—')}** | `{json.dumps(total.get('labels',{}), ensure_ascii=False)}` |")
    lines.extend([
        "",
        "## 2. Embedding latency",
        "",
        "| phase | p50 | p95 | rounds |",
        "|---|---:|---:|---:|",
        f"| cold | {results['latency']['cold_p50_ms']:.1f} ms | {results['latency']['cold_p95_ms']:.1f} ms | 1 |",
        f"| warm | {results['latency']['warm_p50_ms']:.1f} ms | {results['latency']['warm_p95_ms']:.1f} ms | {results['latency']['warm_rounds']} |",
        "",
        "## 3. Threshold sweep",
        "",
    ])
    if results.get("fixed_threshold"):
        ft = results["fixed_threshold"]
        lines.append(f"固定閾值模式：`policy={ft['policy']}` cos={ft.get('cosine_threshold','—')} margin={ft.get('margin_threshold','—')}")
        lines.append("")
        m = results["chosen_metrics"]
        lines.append(f"- macro F1 {_pct(float(m['macro_f1']))}, micro F1 {_pct(float(m['micro_f1']))}, MIXED recall {_pct(float(m['mixed_recall']))}, coverage {_pct(float(m['high_confidence_coverage']))}, false-fast {int(m['false_fast_route_count'])}")
    else:
        lines.append("擇優規則：`MIXED recall≥75% & false-fast=0` → `MIXED≥75%` → `false-fast=0` → `max macro_F1`")
        lines.append("")
        lines.append("| policy | thresholds | macro F1 | micro F1 | MIXED recall | coverage | fallback | false-fast |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for policy in ("cosine", "margin", "hybrid"):
            row = results["recommendations"][policy]
            if policy == "hybrid":
                thresholds = f"cos={row['cosine_threshold']:.2f}, margin={row['margin_threshold']:.2f}"
            else:
                thresholds = f"{row['threshold']:.2f}"
            lines.append(f"| {policy} | {thresholds} | {_pct(float(row['macro_f1']))} | {_pct(float(row['micro_f1']))} | {_pct(float(row['mixed_recall']))} | {_pct(float(row['high_confidence_coverage']))} | {_pct(float(row['abstain_llm_fallback_rate']))} | {int(row['false_fast_route_count'])} |")
        lines.append("")
        lines.append("最大 MIXED recall（診斷用）：")
        lines.append("")
        lines.append("| policy | threshold(s) | max MIXED recall | macro F1 | coverage | false-fast |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for policy in ("cosine", "margin", "hybrid"):
            row = results["best_mixed_recall"][policy]
            if policy == "hybrid":
                thresholds = f"cos={row['cosine_threshold']:.2f}, margin={row['margin_threshold']:.2f}"
            else:
                thresholds = f"{row['threshold']:.2f}"
            lines.append(f"| {policy} | {thresholds} | {_pct(float(row['mixed_recall']))} | {_pct(float(row['macro_f1']))} | {_pct(float(row['high_confidence_coverage']))} | {int(row['false_fast_route_count'])} |")
    lines.extend([
        "",
        "Per-class 指標（chosen）：",
        "",
        "| label | precision | recall | F1 | support |",
        "|---|---:|---:|---:|---:|",
    ])
    per_class = results["chosen_metrics"]["per_class"]
    for label in ROUTE_LABELS:
        item = per_class[label]
        lines.append(f"| {label} | {_pct(float(item['precision']))} | {_pct(float(item['recall']))} | {_pct(float(item['f1']))} | {int(item['support'])} |")
    lines.extend([
        "",
        f"- High-confidence coverage: {_pct(float(results['chosen_metrics']['high_confidence_coverage']))}",
        f"- Fallback rate: {_pct(float(results['chosen_metrics']['abstain_llm_fallback_rate']))}",
        f"- False-fast: **{int(results['chosen_metrics']['false_fast_route_count'])}**",
        "",
        "## 4. Confusions",
        "",
    ])
    if results["confusions"]:
        lines.extend(["| id | gold | pred | top score | margin | family | text |", "|---|---|---|---:|---:|---|---|"])
        for c in results["confusions"]:
            lines.append(f"| {c['id']} | {c['gold']} | {c['prediction']} | {c['top_score']:.4f} | {c['margin']:.4f} | {c.get('family_id','—')} | {c['text'][:50]} |")
    else:
        lines.append("無混淆（或皆為 UNKNOWN abstain）。")
    b = results["boundary"]
    lines.extend([
        "",
        "## 5. Boundary comparison (guard-before-router)",
        "",
        f"- Boundary rows: {b['total']}; bypassed: {b['bypassed']}; correct: {b['correct']}/{b['total']}",
        "",
        "| id | expected | detected | bypassed |",
        "|---|---|---|---|",
    ])
    for o in b["outcomes"]:
        lines.append(f"| {o['id']} | {o['category']} | {o['detected'] or 'NONE'} | {'yes' if o['bypassed'] else 'no'} |")
    if "guarded" in results and results["guarded"]:
        g = results["guarded"]
        lines.extend([
            "",
            "## 6. Guarded checks (holdout)",
            "",
            f"- Verdict: **{g['verdict']}**",
            f"- false-fast={g['false_fast']}, MIXED→PURE={g['mixed_to_pure_pure_education_or_intake']}, SUBJECT/CORRECTION fast={g['subject_correction_fast']}, boundary_leak={g['boundary_leak']}",
        ])
        if g["blocked_reasons"]:
            lines.append(f"- Blocked reasons: `{', '.join(g['blocked_reasons'])}`")
    lines.extend([
        "",
        "## 7. Reproduce",
        "",
        "```bash",
        f"python scripts/semantic_router_evaluate.py --dataset {results['dataset_path']} --split {results['evaluated_split']} --family-split --check-leakage",
        f"python scripts/semantic_router_evaluate.py --dataset {results['dataset_path']} --split holdout --family-split --json-output /tmp/holdout.json",
        "PYTEST_CURRENT_TEST=1 python scripts/semantic_router_evaluate.py --split all --json-output /tmp/fake.json  # fake mode",
        "```",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=str, default=None, help="dataset.json path")
    parser.add_argument("--split", type=str, default="all", choices=["all", "train", "calibration", "holdout"], help="which split to evaluate")
    parser.add_argument("--family-split", action="store_true", help="enforce family_id does not cross splits")
    parser.add_argument("--check-leakage", action="store_true", help="enable text similarity > threshold cross-split warning")
    parser.add_argument("--leak-threshold", type=float, default=0.95, help="similarity threshold for leakage warning")
    parser.add_argument("--cosine-threshold", type=float, default=None, help="fixed cosine threshold")
    parser.add_argument("--margin-threshold", type=float, default=None, help="fixed margin threshold")
    parser.add_argument("--policy", type=str, default="hybrid", choices=["cosine", "margin", "hybrid"], help="policy")
    parser.add_argument("--thresholds", type=Path, default=None, help="JSON file from calibrate (reads cosine_threshold/margin_threshold/policy)")
    parser.add_argument("--output", type=Path, default=None, help="Markdown output")
    parser.add_argument("--json-output", type=Path, default=None, help="JSON output")
    args = parser.parse_args()
    if args.thresholds and args.thresholds.exists():
        try:
            _tdata = json.loads(args.thresholds.read_text(encoding="utf-8"))
            _chosen = _tdata.get("chosen") or _tdata.get("recommendations", {}).get("hybrid") or {}
            if args.cosine_threshold is None and "cosine_threshold" in _chosen:
                args.cosine_threshold = float(_chosen["cosine_threshold"])
            elif args.cosine_threshold is None and "threshold" in _chosen:
                args.cosine_threshold = float(_chosen["threshold"])
            if args.margin_threshold is None and "margin_threshold" in _chosen:
                args.margin_threshold = float(_chosen["margin_threshold"])
        except Exception as _e:
            print(f"[warn] failed to read thresholds file {args.thresholds}: {_e}", file=sys.stderr)

    dataset = load_dataset(Path(args.dataset) if args.dataset else None)
    primary = dataset["primary"]
    boundary = dataset["boundary_comparison"]

    leakage = check_family_leakage(primary)
    text_leakage = check_text_leakage(primary, threshold=args.leak_threshold) if args.check_leakage else {"warnings": [], "count": 0, "threshold": args.leak_threshold}
    distribution = distribution_by_split(primary)

    if args.family_split and leakage["has_leak"]:
        print(f"[LEAKAGE FAIL] families crossing splits: {leakage['leak_details']}", file=sys.stderr)
    if text_leakage["count"] > 0:
        print(f"[TEXT LEAKAGE WARNING] {text_leakage['count']} cross-split pairs > {args.leak_threshold}", file=sys.stderr)
        for w in text_leakage["warnings"][:5]:
            print(f"  {w['text1_id']} ({w['split1']}) ↔ {w['text2_id']} ({w['split2']}) sim={w['similarity']}", file=sys.stderr)

    if args.split == "all":
        eval_rows = list(primary)
        eval_boundary = list(boundary)
    else:
        eval_rows = split_rows(primary, args.split)
        eval_boundary = [r for r in boundary if str(r.get("split", "")) == args.split] if any("split" in r for r in boundary) else list(boundary)
        if not eval_boundary and any("split" in r for r in boundary):
            eval_boundary = []

    router, backend = _build_router_and_backend()
    latency = benchmark_embedding(router.embedder)

    scored = score_rows(router, eval_rows)

    fixed_threshold = None
    if args.cosine_threshold is not None or args.margin_threshold is not None:
        ct = args.cosine_threshold if args.cosine_threshold is not None else 0.62
        mt = args.margin_threshold if args.margin_threshold is not None else 0.10
        fixed_threshold = {"policy": args.policy, "cosine_threshold": ct, "margin_threshold": mt, "threshold": ct if args.policy != "margin" else mt}
        from tfda_context_gate.semantic_router.eval_common import label_from_scores
        preds = [label_from_scores(r, policy=args.policy, cosine_threshold=ct, margin_threshold=mt) for r in scored]
        chosen_metrics = metrics_for(eval_rows, preds)
        sweeps = {}
        recommendations = {}
        for policy in ("cosine", "margin", "hybrid"):
            sweeps[policy] = threshold_sweep(scored, policy)
            recommendations[policy] = select_recommended(sweeps[policy])
        best_mixed = {p: dict(max(sweeps[p], key=lambda r: (float(r["mixed_recall"]), float(r["macro_f1"])))) for p in ("cosine", "margin", "hybrid")}
        predictions = preds
    else:
        sweeps = {}
        recommendations = {}
        for policy in ("cosine", "margin", "hybrid"):
            sweeps[policy] = threshold_sweep(scored, policy)
            recommendations[policy] = select_recommended(sweeps[policy])
        best_mixed = {p: dict(max(sweeps[p], key=lambda r: (float(r["mixed_recall"]), float(r["macro_f1"])))) for p in ("cosine", "margin", "hybrid")}
        chosen = recommendations[args.policy]
        params = {"cosine_threshold": float(chosen.get("cosine_threshold", chosen.get("threshold", 0.75))), "margin_threshold": float(chosen.get("margin_threshold", chosen.get("threshold", 0.05)))}
        from tfda_context_gate.semantic_router.eval_common import label_from_scores
        predictions = [label_from_scores(r, policy=args.policy, **params) for r in scored]
        chosen_metrics = metrics_for(eval_rows, predictions)

    confusions = top_confusions(scored, policy=args.policy, recommended=fixed_threshold if fixed_threshold else recommendations[args.policy], limit=20)
    boundary_result = evaluate_boundary(eval_boundary if eval_boundary else boundary)

    guarded = None
    if args.split == "holdout":
        guarded = guarded_checks(eval_rows, predictions, boundary_result)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = {
        "generated_at": generated_at,
        "dataset_path": dataset["_path"],
        "version": dataset["version"],
        "description": dataset["description"],
        "backend": backend,
        "leakage": leakage,
        "text_leakage": text_leakage,
        "distribution": distribution,
        "latency": latency,
        "evaluated_split": args.split,
        "family_split": args.family_split,
        "total_primary": len(primary),
        "evaluated_count": len(eval_rows),
        "family_count": len({str(r.get("family_id")) for r in primary if r.get("family_id")}),
        "boundary_count": len(boundary),
        "sweeps": sweeps,
        "recommendations": recommendations,
        "best_mixed_recall": best_mixed,
        "chosen_metrics": chosen_metrics,
        "chosen_policy": args.policy,
        "fixed_threshold": fixed_threshold,
        "confusions": confusions,
        "boundary": boundary_result,
        "guarded": guarded,
        "predictions": [{"id": r["id"], "gold": r["label"], "prediction": p, "family_id": r.get("family_id",""), "split": r.get("split","")} for r, p in zip(eval_rows, predictions)],
    }

    summary = {
        "backend": backend,
        "leakage": {"has_leak": leakage["has_leak"], "note": leakage["note"]},
        "text_leakage": {"count": text_leakage["count"], "threshold": args.leak_threshold},
        "evaluated_split": args.split,
        "evaluated_count": len(eval_rows),
        "chosen_metrics": chosen_metrics,
        "chosen_policy": args.policy,
        "fixed_threshold": fixed_threshold,
        "guarded": guarded,
        "boundary": {"total": boundary_result["total"], "correct": boundary_result["correct"]},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[evaluate] JSON written to {args.json_output}", file=sys.stderr)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_markdown(results), encoding="utf-8")
        print(f"[evaluate] Markdown written to {args.output}", file=sys.stderr)

    if args.family_split and leakage["has_leak"]:
        return 1
    if guarded and not guarded["guarded_pass"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
