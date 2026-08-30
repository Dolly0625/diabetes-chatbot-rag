"""Shared eval helpers for semantic_router calibration & holdout.

Reuses the same metric definitions as
``experiments/semantic_router_eval/evaluate.py`` (threshold_sweep,
select_recommended, metrics_for) without importing that module —
copied verbatim then adapted for family-split datasets and the
production router factory path.

No network is performed at import time.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np

from .config import ROUTE_LABELS

# ---------------------------------------------------------------------------
# Dataset loading & family leakage
# ---------------------------------------------------------------------------

DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[2] / "experiments" / "semantic_router_production" / "dataset.json"
FALLBACK_DATASET_PATH = Path(__file__).resolve().parents[2].parents[1] / "tfda-diabetes-agent-semantic-router-eval" / "experiments" / "semantic_router_eval" / "dataset.json"


def load_dataset(path: Path | None = None) -> Dict[str, Any]:
    """Load dataset with family_id / split if present.

    Returns dict with ``primary``, ``boundary_comparison``, ``version``,
    ``description``.  Rows keep original keys plus ``family_id``/``split``
    when present.
    """
    if path is None:
        # prefer production dataset, fallback to eval dataset for local dev
        if DEFAULT_DATASET_PATH.exists():
            path = DEFAULT_DATASET_PATH
        elif FALLBACK_DATASET_PATH.exists():
            path = FALLBACK_DATASET_PATH
        else:
            path = DEFAULT_DATASET_PATH
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dataset must be an object")
    primary = payload.get("primary")
    boundary = payload.get("boundary_comparison", [])
    if not isinstance(primary, list):
        raise ValueError("dataset must contain primary list")
    if not isinstance(boundary, list):
        raise ValueError("boundary_comparison must be a list")
    for row in primary + boundary:
        if not row.get("text"):
            raise ValueError(f"dataset row {row.get('id','?')} has empty text")
    return {
        "primary": primary,
        "boundary_comparison": boundary,
        "version": payload.get("version", "unknown"),
        "description": payload.get("description", ""),
        "splits": payload.get("splits", {}),
        "_path": str(path),
    }


def check_family_leakage(primary: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Check whether any family_id appears in multiple splits.

    Returns dict with ``has_leak``, ``leak_families``, ``family_to_splits``.
    If no family_id column present, reports ``no_family_id``.
    """
    has_family = any("family_id" in r for r in primary)
    has_split = any("split" in r for r in primary)
    if not has_family or not has_split:
        return {
            "has_family_id": has_family,
            "has_split": has_split,
            "has_leak": False,
            "leak_families": [],
            "family_to_splits": {},
            "note": "no family_id or split column — skip leakage check",
        }
    fam_to_splits: Dict[str, set] = defaultdict(set)
    for row in primary:
        fid = str(row.get("family_id", ""))
        split = str(row.get("split", ""))
        if fid and split:
            fam_to_splits[fid].add(split)
    leaks = {fid: sorted(splits) for fid, splits in fam_to_splits.items() if len(splits) > 1}
    return {
        "has_family_id": True,
        "has_split": True,
        "has_leak": len(leaks) > 0,
        "leak_families": sorted(leaks.keys()),
        "leak_details": leaks,
        "family_to_splits": {k: sorted(v) for k, v in fam_to_splits.items()},
        "family_count": len(fam_to_splits),
        "note": f"{len(leaks)} leaked families" if leaks else "no leakage",
    }


def split_rows(primary: Sequence[Mapping[str, Any]], split_name: str) -> List[Mapping[str, Any]]:
    """Filter primary rows by split; if no split column, return all."""
    if not any("split" in r for r in primary):
        return list(primary)  # type: ignore[return-value]
    return [r for r in primary if str(r.get("split")) == split_name]


def distribution_by_split(primary: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return per-split counts and per-split label counters."""
    splits = sorted({str(r.get("split", "all")) for r in primary if "split" in r} or ["all"])
    out: Dict[str, Any] = {}
    for s in splits:
        rows = split_rows(primary, s) if "split" in primary[0] else list(primary)  # type: ignore
        out[s] = {
            "count": len(rows),
            "labels": dict(Counter(str(r.get("label")) for r in rows)),
            "families": len({str(r.get("family_id")) for r in rows if r.get("family_id")}),
            "sources": dict(Counter(str(r.get("source", "")) for r in rows)),
        }
    total = {
        "count": len(primary),
        "labels": dict(Counter(str(r.get("label")) for r in primary)),
        "families": len({str(r.get("family_id")) for r in primary if r.get("family_id")}),
    }
    out["_total"] = total
    return out


# ---------------------------------------------------------------------------
# Scoring helpers (router-agnostic)
# ---------------------------------------------------------------------------

def _percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), p))


def benchmark_embedding(embedder: Any, *, warm_rounds: int = 25) -> Dict[str, Any]:
    """Measure cold vs warm embedding latency (same as eval benchmark)."""
    cold_start = time.perf_counter()
    embedder.embed_query("糖尿病飲食的一般原則是什麼？")
    cold_ms = (time.perf_counter() - cold_start) * 1000.0
    warm: List[float] = []
    for i in range(warm_rounds):
        s = time.perf_counter()
        embedder.embed_query("回診前要整理哪些看診資料？" if i % 2 else "低血糖有哪些一般症狀？")
        warm.append((time.perf_counter() - s) * 1000.0)
    return {
        "cold_ms": round(cold_ms, 3),
        "cold_p50_ms": round(cold_ms, 3),
        "cold_p95_ms": round(cold_ms, 3),
        "warm_rounds": warm_rounds,
        "warm_p50_ms": round(_percentile(warm, 50), 3),
        "warm_p95_ms": round(_percentile(warm, 95), 3),
        "warm_min_ms": round(min(warm), 3) if warm else 0.0,
        "warm_max_ms": round(max(warm), 3) if warm else 0.0,
    }


def score_rows(router: Any, rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Score rows via router._score (production) or router.score (eval)."""
    scored: List[Dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text", ""))
        # Try production _score, then eval score, then route fallback
        scores: Dict[str, float] = {}
        if hasattr(router, "_score"):
            try:
                scores = router._score(text)  # type: ignore
            except Exception:
                scores = {}
        if not scores and hasattr(router, "score"):
            try:
                scores = router.score(text)  # type: ignore
            except Exception:
                scores = {}
        if not scores:
            # last resort: route and synthesize scores
            try:
                obs = router.route(text)  # type: ignore
                scores = dict(getattr(obs, "scores", {}) or {})
                if not scores and getattr(obs, "route", "") != "UNKNOWN":
                    scores = {obs.route: float(getattr(obs, "confidence", 0.0))}
            except Exception:
                scores = {}
        if not scores:
            # empty -> all zero
            scores = {label: 0.0 for label in ROUTE_LABELS if label != "UNKNOWN"}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_score = float(ranked[0][1]) if ranked else 0.0
        top_label = str(ranked[0][0]) if ranked else "UNKNOWN"
        second_score = float(ranked[1][1]) if len(ranked) > 1 else -1.0
        scored.append(
            {
                **dict(row),
                "scores": {k: round(float(v), 6) for k, v in scores.items()},
                "top_label": top_label,
                "top_score": top_score,
                "second_score": second_score,
                "margin": top_score - second_score,
            }
        )
    return scored


def label_from_scores(
    row: Mapping[str, Any],
    *,
    policy: str,
    cosine_threshold: float = 0.75,
    margin_threshold: float = 0.05,
) -> str:
    top_label = str(row["top_label"])
    top_score = float(row["top_score"])
    margin = float(row["margin"])
    if policy == "cosine":
        accepted = top_score >= cosine_threshold
    elif policy == "margin":
        accepted = margin >= margin_threshold
    elif policy == "hybrid":
        accepted = top_score >= cosine_threshold and margin >= margin_threshold
    else:
        raise ValueError("policy must be cosine, margin, or hybrid")
    return top_label if accepted else "UNKNOWN"


def metrics_for(rows: Sequence[Mapping[str, Any]], predictions: Sequence[str]) -> Dict[str, Any]:
    """Per-policy metrics, identical to eval/evaluate.py."""
    if len(rows) != len(predictions):
        raise ValueError("rows/predictions length mismatch")
    confusion: Dict[str, Dict[str, int]] = {
        label: {pred: 0 for pred in ROUTE_LABELS} for label in ROUTE_LABELS
    }
    for row, pred in zip(rows, predictions):
        gold = str(row["label"])
        if pred not in ROUTE_LABELS:
            raise ValueError("prediction outside ROUTE_LABELS: " + pred)
        confusion[gold][pred] += 1
    per_class: Dict[str, Dict[str, float]] = {}
    for label in ROUTE_LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[other][label] for other in ROUTE_LABELS if other != label)
        fn = sum(confusion[label][other] for other in ROUTE_LABELS if other != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "support": sum(confusion[label].values()),
        }
    total = len(rows)
    correct = sum(confusion[label][label] for label in ROUTE_LABELS)
    micro = correct / total if total else 0.0
    accepted = sum(p != "UNKNOWN" for p in predictions)
    false_fast = sum(p != "UNKNOWN" and p != str(row["label"]) for row, p in zip(rows, predictions))
    return {
        "total": total,
        "macro_f1": round(statistics.mean(item["f1"] for item in per_class.values()), 6),
        "micro_precision": round(micro, 6),
        "micro_recall": round(micro, 6),
        "micro_f1": round(micro, 6),
        "high_confidence_coverage": round(accepted / total, 6) if total else 0.0,
        "abstain_llm_fallback_rate": round(1.0 - accepted / total, 6) if total else 0.0,
        "false_fast_route_count": false_fast,
        "false_fast_route_rate": round(false_fast / total, 6) if total else 0.0,
        "mixed_recall": per_class["MIXED"]["recall"],
        "per_class": per_class,
        "confusion": confusion,
    }


def _grid(start: float, stop: float, step: float) -> List[float]:
    count = int(round((stop - start) / step))
    return [round(start + i * step, 6) for i in range(count + 1)]


def threshold_sweep(rows: Sequence[Mapping[str, Any]], policy: str) -> List[Dict[str, Any]]:
    """Sweep per spec: cos 0.50–0.95 step 0.02, margin 0.00–0.30 step 0.02."""
    candidates: List[Dict[str, Any]] = []
    if policy == "cosine":
        for th in _grid(0.50, 0.95, 0.02):
            preds = [label_from_scores(r, policy=policy, cosine_threshold=th) for r in rows]
            candidates.append({"threshold": th, **metrics_for(rows, preds)})
    elif policy == "margin":
        for th in _grid(0.00, 0.30, 0.02):
            preds = [label_from_scores(r, policy=policy, margin_threshold=th) for r in rows]
            candidates.append({"threshold": th, **metrics_for(rows, preds)})
    elif policy == "hybrid":
        for cos_th in _grid(0.50, 0.95, 0.02):
            for mar_th in _grid(0.00, 0.30, 0.02):
                preds = [
                    label_from_scores(r, policy=policy, cosine_threshold=cos_th, margin_threshold=mar_th)
                    for r in rows
                ]
                candidates.append({"cosine_threshold": cos_th, "margin_threshold": mar_th, **metrics_for(rows, preds)})
    else:
        raise ValueError("unknown sweep policy: " + policy)
    return candidates


def select_recommended(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Four-tier selection per research doc."""
    if not candidates:
        raise ValueError("no threshold candidates")
    tiers = (
        lambda r: float(r["mixed_recall"]) >= 0.75 and int(r["false_fast_route_count"]) == 0,
        lambda r: float(r["mixed_recall"]) >= 0.75,
        lambda r: int(r["false_fast_route_count"]) == 0,
        lambda r: True,
    )
    for eligible in tiers:
        subset = [r for r in candidates if eligible(r)]
        if subset:
            return dict(
                max(
                    subset,
                    key=lambda r: (
                        float(r["macro_f1"]),
                        float(r["mixed_recall"]),
                        -int(r["false_fast_route_count"]),
                        float(r["high_confidence_coverage"]),
                    ),
                )
            )
    return dict(candidates[0])


# ---------------------------------------------------------------------------
# Boundary guard (same as eval)
# ---------------------------------------------------------------------------

def boundary_guard(text: str) -> str | None:
    red_flag = ("胸痛" in text or "胸口很痛" in text or "胸悶" in text) and (
        "喘" in text or "呼吸" in text or "昏倒" in text
    )
    if red_flag:
        return "RED_FLAG"
    if any(term in text for term in ("另一位使用者", "查看他的資料", "朋友", "分享給我")):
        return "AUTHORIZATION"
    if any(term in text for term in ("刪除", "重設登入密碼", "帳號資料")):
        return "PRODUCT_COMMAND"
    return None


def evaluate_boundary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    outcomes = []
    for row in rows:
        detected = boundary_guard(str(row.get("text", "")))
        outcomes.append(
            {
                "id": row.get("id"),
                "category": row.get("category"),
                "detected": detected,
                "bypassed": detected is not None,
                "correct": detected == row.get("category"),
            }
        )
    return {
        "total": len(outcomes),
        "bypassed": sum(o["bypassed"] for o in outcomes),
        "correct": sum(o["correct"] for o in outcomes),
        "outcomes": outcomes,
        "policy": "guard-before-router; not counted in semantic metrics",
    }


def top_confusions(
    rows: Sequence[Mapping[str, Any]],
    *,
    policy: str,
    recommended: Mapping[str, Any],
    limit: int = 15,
    only_fast_routes: bool = False,
) -> List[Dict[str, Any]]:
    params = {
        "cosine_threshold": float(recommended.get("cosine_threshold", recommended.get("threshold", 0.75))),
        "margin_threshold": float(recommended.get("margin_threshold", recommended.get("threshold", 0.05))),
    }
    result: List[Dict[str, Any]] = []
    for row in rows:
        pred = label_from_scores(row, policy=policy, **params)
        if pred == str(row["label"]):
            continue
        if only_fast_routes and pred == "UNKNOWN":
            continue
        result.append(
            {
                "id": row.get("id"),
                "text": row.get("text"),
                "gold": row.get("label"),
                "prediction": pred,
                "top_label": row.get("top_label"),
                "top_score": round(float(row.get("top_score", 0)), 4),
                "margin": round(float(row.get("margin", 0)), 4),
                "source": row.get("source", ""),
                "family_id": row.get("family_id", ""),
                "split": row.get("split", ""),
            }
        )
    return result[:limit]


def guarded_checks(
    holdout_rows: Sequence[Mapping[str, Any]],
    predictions: Sequence[str],
    boundary_result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Compute guarded gate conditions for holdout."""
    # false-fast
    false_fast = sum(p != "UNKNOWN" and p != str(r["label"]) for r, p in zip(holdout_rows, predictions))
    # mixed -> pure
    mixed_to_pure = 0
    for r, p in zip(holdout_rows, predictions):
        if str(r["label"]) == "MIXED" and p in ("PURE_EDUCATION", "PURE_INTAKE"):
            mixed_to_pure += 1
    # subject_change / correction fast routes (should be UNKNOWN; any fast is violation)
    sc_fast = 0
    for r, p in zip(holdout_rows, predictions):
        if str(r["label"]) in ("SUBJECT_CHANGE", "CORRECTION") and p != "UNKNOWN":
            sc_fast += 1
    # boundary leakage: compare with split-aware boundary rows
    boundary_leak = int(boundary_result.get("total", 0) - boundary_result.get("correct", 0)) if boundary_result else 0
    # Also count non-zero false_fast is the primary block
    blocked_reasons: List[str] = []
    if false_fast != 0:
        blocked_reasons.append(f"false-fast={false_fast}≠0")
    if boundary_leak != 0:
        blocked_reasons.append(f"boundary_leak={boundary_leak}≠0")
    if mixed_to_pure != 0:
        blocked_reasons.append(f"MIXED→PURE={mixed_to_pure}≠0")
    if sc_fast != 0:
        blocked_reasons.append(f"SUBJECT_CHANGE/CORRECTION fast={sc_fast}≠0")
    guarded_pass = len(blocked_reasons) == 0
    return {
        "false_fast": false_fast,
        "mixed_to_pure_pure_education_or_intake": mixed_to_pure,
        "subject_correction_fast": sc_fast,
        "boundary_leak": boundary_leak,
        "blocked_reasons": blocked_reasons,
        "guarded_pass": guarded_pass,
        "verdict": "PASS" if guarded_pass else "BLOCKED — 建議僅 shadow",
    }
