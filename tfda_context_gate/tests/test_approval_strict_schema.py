from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from tfda_context_gate.semantic_router.approval import (
    compute_dataset_sha256,
    load_and_validate_approval,
    get_effective_route_mode,
    resolve_effective_config,
)
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation


def _base_pass(tmp_path):
    sha = compute_dataset_sha256()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "v1",
        "dataset_sha256": sha,
        "calibration_timestamp": now,
        "cosine_threshold": 0.82,
        "margin_threshold": 0.17,
        "holdout_size": 34,
        "false_fast": 0,
        "mixed_recall": 0.75,
        "correction_boundary_pass": True,
        "subject_boundary_pass": True,
        "guarded_pass": True,
    }


def _write(payload, tmp_path):
    p = tmp_path / f"strict_{os.urandom(4).hex()}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# 1 guarded_pass="false" string -> shadow
def test_guarded_pass_string_false(tmp_path):
    payload = _base_pass(tmp_path)
    payload["guarded_pass"] = "false"
    p = _write(payload, tmp_path)
    ok, reason, _ = load_and_validate_approval(path=p)
    assert not ok and reason == "GUARDED_DOWNGRADED_INVALID_SCHEMA"
    eff, _, _ = get_effective_route_mode("guarded", artifact_path_override=p)
    assert eff == "shadow"


# 2 correction_boundary_pass="false" string -> shadow
def test_correction_string_false(tmp_path):
    payload = _base_pass(tmp_path)
    payload["correction_boundary_pass"] = "false"
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 3 subject_boundary_pass="false" string -> shadow
def test_subject_string_false(tmp_path):
    payload = _base_pass(tmp_path)
    payload["subject_boundary_pass"] = "false"
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 4 boolean 0/1 -> shadow
def test_boolean_zero_one(tmp_path):
    payload = _base_pass(tmp_path)
    payload["guarded_pass"] = 1
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok
    payload["guarded_pass"] = 0
    p2 = _write(payload, tmp_path)
    ok2, _, _ = load_and_validate_approval(path=p2)
    assert not ok2


# 5 mixed_recall NaN -> shadow (via parse_constant)
def test_mixed_recall_nan(tmp_path):
    p = tmp_path / f"nan_{os.urandom(4).hex()}.json"
    # write raw with NaN literal (json standard allows via parse_constant)
    raw = json.dumps(_base_pass(tmp_path), ensure_ascii=False)
    # replace mixed_recall value with NaN literal
    raw = raw.replace('"mixed_recall": 0.75', '"mixed_recall": NaN')
    p.write_text(raw, encoding="utf-8")
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 6 threshold Infinity -> shadow
def test_threshold_infinity(tmp_path):
    p = tmp_path / f"inf_{os.urandom(4).hex()}.json"
    raw = json.dumps(_base_pass(tmp_path), ensure_ascii=False).replace('"cosine_threshold": 0.82', '"cosine_threshold": Infinity')
    p.write_text(raw, encoding="utf-8")
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 7 threshold -0.1 -> shadow
def test_threshold_negative(tmp_path):
    payload = _base_pass(tmp_path)
    payload["cosine_threshold"] = -0.1
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 8 threshold 1.1 -> shadow
def test_threshold_above_one(tmp_path):
    payload = _base_pass(tmp_path)
    payload["cosine_threshold"] = 1.1
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 9 numeric string "0.62" -> shadow
def test_numeric_string(tmp_path):
    payload = _base_pass(tmp_path)
    payload["cosine_threshold"] = "0.62"
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 10 holdout_size 29 -> shadow
def test_holdout_small(tmp_path):
    payload = _base_pass(tmp_path)
    payload["holdout_size"] = 29
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 11 holdout_size true (bool) -> shadow
def test_holdout_bool(tmp_path):
    payload = _base_pass(tmp_path)
    payload["holdout_size"] = True
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 12 unknown field -> shadow (extra forbid)
def test_unknown_field(tmp_path):
    payload = _base_pass(tmp_path)
    payload["extra_unknown"] = "oops"
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 13 naive timestamp no timezone -> shadow
def test_naive_timestamp(tmp_path):
    payload = _base_pass(tmp_path)
    payload["calibration_timestamp"] = "2026-08-30T03:00:00"
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 14 future timestamp beyond skew -> shadow
def test_future_timestamp(tmp_path):
    payload = _base_pass(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat().replace("+00:00", "Z")
    payload["calibration_timestamp"] = future
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 15 dataset hash uppercase or non-64 -> shadow
def test_hash_uppercase(tmp_path):
    payload = _base_pass(tmp_path)
    payload["dataset_sha256"] = payload["dataset_sha256"].upper()
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok

def test_hash_short(tmp_path):
    payload = _base_pass(tmp_path)
    payload["dataset_sha256"] = "abc123"
    p = _write(payload, tmp_path)
    ok, _, _ = load_and_validate_approval(path=p)
    assert not ok


# 16 legal synthetic PASS -> guarded
def test_legal_pass_guarded(tmp_path):
    payload = _base_pass(tmp_path)
    p = _write(payload, tmp_path)
    ok, _, art = load_and_validate_approval(path=p)
    assert ok and art is not None and art.guarded_pass
    eff, _, _ = get_effective_route_mode("guarded", artifact_path_override=p)
    assert eff == "guarded"


# 17 threshold binding: artifact 0.82/0.17 vs env 0.10/0.00, router actually uses 0.82/0.17
def test_threshold_binding_uses_artifact(tmp_path, monkeypatch):
    payload = _base_pass(tmp_path)
    payload["cosine_threshold"] = 0.82
    payload["margin_threshold"] = 0.17
    p = _write(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_COSINE_THRESHOLD", "0.10")
    monkeypatch.setenv("SEMANTIC_ROUTER_MARGIN_THRESHOLD", "0.00")
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    eff_cfg = resolve_effective_config()
    assert eff_cfg.effective_mode == "guarded"
    assert eff_cfg.cosine_threshold == 0.82
    assert eff_cfg.margin_threshold == 0.17
    # factory should build with same
    from tfda_context_gate.semantic_router.factory import build_semantic_router
    r = build_semantic_router()
    assert r.config.cosine_threshold == 0.82
    assert r.config.margin_threshold == 0.17


# 18 MIXED not early exit even with legal artifact
def test_mixed_not_early_even_with_pass(tmp_path, monkeypatch):
    payload = _base_pass(tmp_path)
    p = _write(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation
    from tfda_context_gate.product_session import SQLiteProductSessionRepository
    from tfda_context_gate.line_orchestration import ConversationOrchestrator
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(tmp)
    orch = ConversationOrchestrator(repo, identity_hash_key="test-mixed-pass-12345678901234")
    class MixedHigh:
        def route(self, t):
            return SemanticRouteObservation(route="MIXED", confidence=0.99, margin=0.45, latency_ms=1, mode="guarded", degraded=False)
        predict = route
    orch._semantic_router = MixedHigh()  # type: ignore
    orch._semantic_router_init_attempted = True
    orch.handle_text(event_id="mx-auth", line_user_id="U-mx", text="為自己整理")
    orig = orch.interpreter.interpret
    c = {"n": 0}
    def spy(e):
        c["n"] += 1
        return orig(e)
    orch.interpreter.interpret = spy  # type: ignore
    orch.handle_text(event_id="mx-main", line_user_id="U-mx", text="我最近常口渴，糖尿病一天可以吃幾份水果？")
    assert c["n"] == 1


# 19 CORRECTION not early exit
def test_correction_not_early(tmp_path, monkeypatch):
    payload = _base_pass(tmp_path)
    p = _write(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(tmp)
    orch = ConversationOrchestrator(repo, identity_hash_key="test-corr-12345678901234")
    class CorrHigh:
        def route(self, t):
            return SemanticRouteObservation(route="CORRECTION", confidence=0.99, margin=0.45, latency_ms=1, mode="guarded", degraded=False)
        predict = route
    orch._semantic_router = CorrHigh()  # type: ignore
    orch._semantic_router_init_attempted = True
    orch.handle_text(event_id="co-auth", line_user_id="U-co", text="為自己整理")
    orig = orch.interpreter.interpret
    c = {"n": 0}
    def spy(e):
        c["n"] += 1
        return orig(e)
    orch.interpreter.interpret = spy  # type: ignore
    orch.handle_text(event_id="co-main", line_user_id="U-co", text="我前面說錯了，其實是我媽媽在吃")
    assert c["n"] == 1


# 20 SUBJECT_CHANGE not early exit
def test_subject_not_early(tmp_path, monkeypatch):
    payload = _base_pass(tmp_path)
    p = _write(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(tmp)
    orch = ConversationOrchestrator(repo, identity_hash_key="test-subj-12345678901234")
    class SubjHigh:
        def route(self, t):
            return SemanticRouteObservation(route="SUBJECT_CHANGE", confidence=0.99, margin=0.45, latency_ms=1, mode="guarded", degraded=False)
        predict = route
    orch._semantic_router = SubjHigh()  # type: ignore
    orch._semantic_router_init_attempted = True
    orch.handle_text(event_id="su-auth", line_user_id="U-su", text="為自己整理")
    orig = orch.interpreter.interpret
    c = {"n": 0}
    def spy(e):
        c["n"] += 1
        return orig(e)
    orch.interpreter.interpret = spy  # type: ignore
    orch.handle_text(event_id="su-main", line_user_id="U-su", text="換個話題，我想問睡眠。")
    assert c["n"] == 1


# 21 artifact validation exception -> shadow not interrupt
def test_validation_exception_not_interrupt(tmp_path, monkeypatch):
    # unreadable file
    p = tmp_path / "unreadable.json"
    p.write_text("{}", encoding="utf-8")
    p.chmod(0o000)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason is not None
    # orchestrator should still work
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(tmp)
    orch = ConversationOrchestrator(repo, identity_hash_key="test-exc-12345678901234")
    res = orch.handle_text(event_id="exc-1", line_user_id="U-exc", text="你好")
    assert res.reply
    p.chmod(0o644)


# 22 downgrade not write intake, not increase version
def test_downgrade_no_write(tmp_path, monkeypatch):
    payload = _base_pass(tmp_path)
    payload["false_fast"] = 4  # will downgrade
    p = _write(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(tmp)
    orch = ConversationOrchestrator(repo, identity_hash_key="test-dg-12345678901234")
    orch.handle_text(event_id="dg-auth", line_user_id="U-dg", text="為自己整理")
    v_before = orch.session_for_user("U-dg").version
    res = orch.handle_text(event_id="dg-edu", line_user_id="U-dg", text="糖尿病一天可以吃幾份水果？")
    v_after = orch.session_for_user("U-dg").version
    assert (v_after - v_before) == 1
    sess = orch.session_for_user("U-dg")
    assert sess.intake_snapshot.symptom_description in (None, "")


# 23 log/telemetry not contain artifact content or patient raw
def test_log_no_artifact_content(tmp_path, monkeypatch, caplog):
    import logging
    payload = _base_pass(tmp_path)
    p = _write(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    caplog.set_level(logging.INFO)
    get_effective_route_mode("guarded")
    for rec in caplog.records:
        msg = rec.getMessage()
        assert payload["dataset_sha256"] not in msg
        assert "metformin" not in msg.lower()
