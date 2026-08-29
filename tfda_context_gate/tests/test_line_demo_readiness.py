from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.demo.check_line_demo_readiness import (
    NOT_READY,
    READY_DEVICE,
    READY_LOCAL,
    compute_readiness,
    format_json,
    main,
    run_all_checks,
)


def test_no_secret_in_stdout(monkeypatch, capsys):
    secret = "my_super_secret_value_987654321"
    token = "my_token_abc123_super_long_token_value"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", token)
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "1234567890123456")
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test-secret-key")
    monkeypatch.setenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")
    main([])
    out = capsys.readouterr().out
    assert secret not in out
    assert token not in out
    assert "sk-test-secret-key" not in out


def test_no_secret_in_json(monkeypatch, capsys):
    secret = "another_secret_12345"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-json-secret")
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")
    main(["--json"])
    out = capsys.readouterr().out
    assert secret not in out
    assert "sk-json-secret" not in out
    payload = json.loads(out)
    dumped = json.dumps(payload)
    assert secret not in dumped
    assert "sk-json-secret" not in dumped
    assert "readiness" in payload
    assert "checks" in payload


def test_missing_required_exits_nonzero(monkeypatch):
    # 清空 LLM 相關，確保本地核心未就緒
    monkeypatch.delenv("CONVERSATION_LLM_MODEL", raising=False)
    monkeypatch.delenv("ROUTER_LLM_MODEL", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)
    code = main([])
    assert code != 0


def test_ready_local_when_line_missing_but_local_ok(monkeypatch):
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")
    monkeypatch.setenv("OPENCODE_API_KEY", "fake-key")
    monkeypatch.setenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)
    checks = run_all_checks()
    readiness = compute_readiness(checks)
    assert readiness == READY_LOCAL


def test_ready_device_when_all_set(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "a" * 32)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "b" * 60)
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "c" * 16)
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "opencode/mimo-v2.5")
    monkeypatch.setenv("OPENCODE_API_KEY", "fake-key")
    monkeypatch.setenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "123")
    monkeypatch.setenv("LINE_LIFF_ID", "liff-123")
    monkeypatch.setenv("PATIENT_PORTAL_URL", "https://example.com")
    monkeypatch.setenv("LINE_CALLBACK_URL", "https://example.com/callback")
    checks = run_all_checks()
    readiness = compute_readiness(checks)
    assert readiness == READY_DEVICE


def test_identity_hash_key_short_is_blocked(monkeypatch):
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "short")
    from scripts.demo.check_line_demo_readiness import check_identity_hash_key

    result = check_identity_hash_key()
    assert result.status == "BLOCKED"


def test_webhook_unsigned_is_blocked(monkeypatch):
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    from scripts.demo.check_line_demo_readiness import check_webhook_signature

    result = check_webhook_signature()
    assert result.status == "BLOCKED"


def test_json_contains_no_secrets_and_structure(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "secret1234567890")
    checks = run_all_checks()
    readiness = compute_readiness(checks)
    j = format_json(checks, readiness)
    assert "secret1234567890" not in j
    data = json.loads(j)
    assert "readiness" in data
    assert "summary" in data
    assert "checks" in data
    for c in data["checks"]:
        assert "status" in c
        assert c["status"] in {"PASS", "WARN", "BLOCKED"}


def test_cli_via_subprocess_no_secret_leak(tmp_path):
    # 透過 subprocess 測試 python -m 執行路徑
    env = {
        "LINE_CHANNEL_SECRET": "subprocess_secret_xyz",
        "CONVERSATION_LLM_MODEL": "opencode/mimo-v2.5",
        "OPENCODE_API_KEY": "subprocess_key",
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
    }
    result = subprocess.run(
        [sys.executable, "-m", "scripts.demo.check_line_demo_readiness", "--json"],
        capture_output=True,
        text=True,
        env={**env, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert "subprocess_secret_xyz" not in result.stdout
    assert "subprocess_key" not in result.stdout
