from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

DATASET_PATH = Path(__file__).resolve().parents[1] / "dataset.json"


def _load():
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    return payload


def test_dataset_exists_and_version():
    assert DATASET_PATH.exists(), f"missing {DATASET_PATH}"
    payload = _load()
    assert payload["version"] == "semantic-router-production.v1"
    assert len(payload["primary"]) >= 180
    assert len(payload["boundary_comparison"]) >= 12


def test_every_label_at_least_20_and_mixed_more():
    payload = _load()
    counts = Counter(r["label"] for r in payload["primary"])
    for label in ["PURE_EDUCATION", "PURE_INTAKE", "MIXED", "CORRECTION", "SUBJECT_CHANGE", "CHITCHAT", "UNKNOWN"]:
        assert counts[label] >= 20, f"{label} {counts[label]} < 20"
    # mixed / subject / correction / unknown should be relatively larger than minimal
    assert counts["MIXED"] >= 25
    assert counts["CORRECTION"] >= 25


def test_family_id_and_split_present_and_no_cross_split_leak():
    payload = _load()
    primary = payload["primary"]
    assert all("family_id" in r and "split" in r for r in primary)
    assert all("family_id" in r and "split" in r for r in payload["boundary_comparison"])
    # same family_id must not cross splits
    from collections import defaultdict
    fam_to_splits = defaultdict(set)
    for r in primary:
        fam_to_splits[r["family_id"]].add(r["split"])
    leaks = {fid: splits for fid, splits in fam_to_splits.items() if len(splits) > 1}
    assert not leaks, f"family leakage: {leaks}"
    # also for boundary
    fam_to_splits_b = defaultdict(set)
    for r in payload["boundary_comparison"]:
        fam_to_splits_b[r["family_id"]].add(r["split"])
    leaks_b = {fid: s for fid, s in fam_to_splits_b.items() if len(s) > 1}
    assert not leaks_b, f"boundary family leakage: {leaks_b}"


def test_pii_free():
    payload = _load()
    text = "\n".join(r["text"] for r in payload["primary"] + payload["boundary_comparison"])
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text), "email found"
    assert not re.search(r"(?:09\d{8}|\+?886[- ]?9\d{8})", text), "phone found"
    assert not re.search(r"\b(?:U|user|patient)[-_ ]?\d{3,}\b", text, flags=re.I), "patient id found"


def test_no_exact_copy_of_original_84():
    orig_path = Path(__file__).resolve().parents[3].parents[1] / "tfda-diabetes-agent-semantic-router-eval" / "experiments" / "semantic_router_eval" / "dataset.json"
    if not orig_path.exists():
        # if sibling not present, skip
        return
    orig = json.loads(orig_path.read_text(encoding="utf-8"))
    orig_texts = {r["text"].strip() for r in orig["primary"] + orig["boundary_comparison"]}
    payload = _load()
    overlap = [r for r in payload["primary"] + payload["boundary_comparison"] if r["text"].strip() in orig_texts]
    assert not overlap, f"found {len(overlap)} exact copies of original: {overlap[:3]}"


def test_covers_required_phenomena():
    payload = _load()
    all_text = "\n".join(r["text"] for r in payload["primary"] + payload["boundary_comparison"])
    # Taiwan slang
    assert any(kw in all_text for kw in ["跑廁所", "口乾", "很渴"]), "missing Taiwan slang"
    # drug names mixed
    assert "metformin" in all_text
    assert "gliclazide" in all_text
    assert "insulin glargine" in all_text
    # red-flag mixed
    assert "胸悶" in all_text and "喘不過氣" in all_text
    # negation / question / hypothetical / other person / chitchat identity
    assert "沒有" in all_text or "不是" in all_text
    assert "嗎？" in all_text or "嗎?" in all_text
    assert "假如" in all_text or "如果" in all_text
    assert "同事" in all_text or "朋友" in all_text or "媽媽" in all_text
    assert "你是誰" in all_text or "你是醫生" in all_text


def test_split_distribution_present():
    payload = _load()
    splits = Counter(r["split"] for r in payload["primary"])
    assert splits["train"] > 0
    assert splits["calibration"] > 0
    assert splits["holdout"] > 0


def test_text_similarity_leakage_none():
    import difflib
    payload = _load()
    primary = payload["primary"]
    # group by split
    from collections import defaultdict
    by_split = defaultdict(list)
    for r in primary:
        by_split[r["split"]].append(r)
    splits = sorted(by_split.keys())
    leaks = []
    for i, s1 in enumerate(splits):
        for s2 in splits[i + 1:]:
            for r1 in by_split[s1]:
                for r2 in by_split[s2]:
                    sim = difflib.SequenceMatcher(None, r1["text"].strip(), r2["text"].strip()).ratio()
                    if sim > 0.95:
                        leaks.append((r1["id"], r2["id"], sim))
    assert not leaks, f"text similarity >0.95 cross-split leaks: {leaks[:5]}"
