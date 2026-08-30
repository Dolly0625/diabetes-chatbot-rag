from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from tfda_context_gate.semantic_router.approval import (
    compute_dataset_sha256,
    load_and_validate_approval,
    get_effective_route_mode,
)
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator


def _make_orch(tmp_path, mode="guarded"):
    p = Path(tempfile.mktemp(suffix=".sqlite3"))
    repo = SQLiteProductSessionRepository(p)
    orch = ConversationOrchestrator(repo, identity_hash_key="guarded-test-key-12345678901234")
    return orch, p


def _base_pass_payload():
    sha = compute_dataset_sha256()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "v1",
        "dataset_sha256": sha,
        "calibration_timestamp": now,
        "cosine_threshold": 0.62,
        "margin_threshold": 0.10,
        "holdout_size": 34,
        "false_fast": 0,
        "mixed_recall": 0.75,
        "correction_boundary_pass": True,
        "subject_boundary_pass": True,
        "guarded_pass": True,
    }


def _write_artifact(payload, tmp_path):
    p = tmp_path / f"approval_{os.urandom(4).hex()}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_guarded_no_artifact_downgrades_to_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    monkeypatch.delenv("SEMANTIC_ROUTER_APPROVAL_PATH", raising=False)
    # ensure no file at default locations
    for p in [Path("tfda_context_gate/semantic_router/approval.json"), Path("experiments/semantic_router_production/approval.json")]:
        if p.is_file():
            # not deleting, just ensure env points to missing
            pass
    # point to missing
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(tmp_path / "missing.json"))
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason == "GUARDED_DOWNGRADED_MISSING_ARTIFACT"
    # orchestrator should also downgrade
    orch, _ = _make_orch(tmp_path)
    orch.handle_text(event_id="evt-no-art-auth", line_user_id="U-no-art", text="為自己整理")
    orig = orch.interpreter.interpret
    calls = {"c": 0}
    def spy(e):
        calls["c"] += 1
        return orig(e)
    orch.interpreter.interpret = spy  # type: ignore
    orch.handle_text(event_id="evt-no-art-edu", line_user_id="U-no-art", text="糖尿病一天可以吃幾份水果？")
    assert calls["c"] == 1  # not early exit, downgraded


def test_malformed_artifact_downgrades(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    p = tmp_path / "malformed.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason == "GUARDED_DOWNGRADED_MALFORMED_ARTIFACT"


def test_guarded_pass_false_downgrades(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    payload["guarded_pass"] = False
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason == "GUARDED_DOWNGRADED_NOT_PASSED"


def test_false_fast_nonzero_downgrades(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    payload["false_fast"] = 4
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason == "GUARDED_DOWNGRADED_FALSE_FAST_NONZERO"


def test_hash_mismatch_downgrades(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    payload["dataset_sha256"] = "0" * 64
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason == "GUARDED_DOWNGRADED_HASH_MISMATCH"


def test_expired_downgrades(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    past = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat().replace("+00:00", "Z")
    payload["calibration_timestamp"] = past
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    eff, reason, _ = get_effective_route_mode("guarded")
    assert eff == "shadow"
    assert reason == "GUARDED_DOWNGRADED_EXPIRED"


def test_synthetic_pass_allows_edu_early_exit(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    eff, _, _ = get_effective_route_mode("guarded")
    assert eff == "guarded"
    # now test orchestrator early exit with synthetic pass
    from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation
    from tfda_context_gate.workflow.runner import run_workflow
    from tfda_context_gate.rag.retriever import FixtureRetriever

    def fixture_runner(request, **kwargs):
        kwargs.pop("use_formal", None)
        return run_workflow(request, retriever=FixtureRetriever(), **kwargs)

    orch, _ = _make_orch(tmp_path)
    # inject high edu router
    class HighEdu:
        def route(self, t):
            return SemanticRouteObservation(route="PURE_EDUCATION", confidence=0.95, margin=0.3, latency_ms=1, mode="guarded", degraded=False)
        predict = route
    orch._semantic_router = HighEdu()  # type: ignore
    orch._semantic_router_init_attempted = True
    orch._semantic_router_config = type("Cfg", (), {"cosine_threshold": 0.62, "margin_threshold": 0.10, "mode": "guarded"})()
    orch.handle_text(event_id="evt-syn-auth", line_user_id="U-syn", text="為自己整理")
    orig = orch.interpreter.interpret
    calls = {"c": 0}
    def spy(e):
        calls["c"] += 1
        return orig(e)
    orch.interpreter.interpret = spy  # type: ignore
    res = orch.handle_text(event_id="evt-syn-edu", line_user_id="U-syn", text="糖尿病一天可以吃幾份水果？")
    assert calls["c"] == 0
    assert res.metadata is not None and res.metadata.get("semantic_fast_path")


def test_synthetic_pass_still_blocks_mixed(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    from tfda_context_gate.semantic_router.telemetry import SemanticRouteObservation
    orch, _ = _make_orch(tmp_path)
    class MixedHigh:
        def route(self, t):
            return SemanticRouteObservation(route="MIXED", confidence=0.95, margin=0.3, latency_ms=1, mode="guarded", degraded=False)
        predict = route
    orch._semantic_router = MixedHigh()  # type: ignore
    orch._semantic_router_init_attempted = True
    orch._semantic_router_config = type("Cfg", (), {"cosine_threshold": 0.62, "margin_threshold": 0.10, "mode": "guarded"})()
    orch.handle_text(event_id="evt-syn-mix-auth", line_user_id="U-syn-mix", text="為自己整理")
    orig = orch.interpreter.interpret
    calls = {"c": 0}
    def spy(e):
        calls["c"] += 1
        return orig(e)
    orch.interpreter.interpret = spy  # type: ignore
    orch.handle_text(event_id="evt-syn-mix", line_user_id="U-syn-mix", text="我最近常口渴，糖尿病一天可以吃幾份水果？")
    assert calls["c"] == 1


def test_downgrade_does_not_change_version_and_no_intake_write(tmp_path, monkeypatch):
    payload = _base_pass_payload()
    payload["false_fast"] = 4  # will cause downgrade
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    monkeypatch.setenv("SEMANTIC_ROUTER_MODE", "guarded")
    orch, _ = _make_orch(tmp_path)
    orch.handle_text(event_id="evt-dg-auth", line_user_id="U-dg", text="為自己整理")
    v_before = orch.session_for_user("U-dg").version
    res = orch.handle_text(event_id="evt-dg-edu", line_user_id="U-dg", text="糖尿病一天可以吃幾份水果？")
    v_after = orch.session_for_user("U-dg").version
    assert (v_after - v_before) == 1  # one turn, not double due to downgrade
    sess = orch.session_for_user("U-dg")
    assert sess.intake_snapshot.symptom_description in (None, "",)


def test_artifact_not_logged(monkeypatch, tmp_path, caplog):
    payload = _base_pass_payload()
    p = _write_artifact(payload, tmp_path)
    monkeypatch.setenv("SEMANTIC_ROUTER_APPROVAL_PATH", str(p))
    import logging
    caplog.set_level(logging.INFO)
    get_effective_route_mode("guarded")
    # logs should not contain sha or threshold values
    for rec in caplog.records:
        msg = rec.getMessage()
        assert payload["dataset_sha256"] not in msg
        assert str(payload["cosine_threshold"]) not in msg or "guarded_pass" in msg
