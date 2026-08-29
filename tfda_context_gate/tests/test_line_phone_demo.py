from __future__ import annotations

import json
from pathlib import Path

from scripts.demo.check_line_phone_demo import (
    BLOCKED,
    PASS,
    compute_readiness,
    mask_url,
    run_checks,
)


def _fake_tunnel_api(_url: str, _timeout: float) -> dict[str, object]:
    return {"tunnels": [{"public_url": "https://sample-tunnel.example.test"}]}


def _fake_status(url: str, _timeout: float) -> int:
    return 200 if url.endswith("/health") else 405


def test_phone_checker_passes_with_active_tunnel_and_masks_public_host():
    callback = "https://sample-tunnel.example.test/callback"
    checks = run_checks(
        callback_url=callback,
        request_json=_fake_tunnel_api,
        http_status=_fake_status,
    )

    assert compute_readiness(checks) == "READY_FOR_LINE_PHONE_DEMO"
    assert all(item.status == PASS for item in checks)
    rendered = json.dumps([item.__dict__ for item in checks], ensure_ascii=False)
    assert callback not in rendered
    assert "sample-tunnel.example.test" not in rendered
    assert mask_url(callback) == "https://sam***est/callback"


def test_phone_checker_blocks_private_or_wrong_callback_without_leaking_url():
    checks = run_checks(
        callback_url="https://localhost/callback?token=do-not-print",
        request_json=_fake_tunnel_api,
        http_status=_fake_status,
    )
    callback_check = next(item for item in checks if item.id == "callback_url")
    assert callback_check.status == BLOCKED
    rendered = json.dumps([item.__dict__ for item in checks], ensure_ascii=False)
    assert "do-not-print" not in rendered
    assert "https://localhost/callback" not in rendered


def test_phone_checker_blocks_tunnel_host_mismatch():
    checks = run_checks(
        callback_url="https://another-tunnel.example.test/callback",
        request_json=_fake_tunnel_api,
        http_status=_fake_status,
    )
    mismatch = next(item for item in checks if item.id == "callback_tunnel_match")
    assert mismatch.status == BLOCKED
    assert "another-tunnel.example.test" not in mismatch.message


def test_phone_checker_accepts_expected_get_405_without_posting_event():
    calls: list[str] = []

    def status(url: str, timeout: float) -> int:
        calls.append(url)
        return _fake_status(url, timeout)

    checks = run_checks(
        callback_url="https://sample-tunnel.example.test/callback",
        request_json=_fake_tunnel_api,
        http_status=status,
    )
    route = next(item for item in checks if item.id == "public_callback_route")
    assert route.status == PASS
    assert calls == ["http://127.0.0.1:8000/health", "https://sample-tunnel.example.test/callback"]


def test_phone_checker_reads_only_callback_from_project_dotenv(tmp_path: Path, monkeypatch):
    import scripts.demo.check_line_phone_demo as checker

    (tmp_path / ".env").write_text(
        "LINE_CALLBACK_URL=https://sample-tunnel.example.test/callback\n"
        "LINE_CHANNEL_SECRET=must-not-be-returned\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LINE_CALLBACK_URL", raising=False)
    monkeypatch.setattr(checker, "PROJECT_ROOT", tmp_path)
    assert checker.configured_callback_url() == "https://sample-tunnel.example.test/callback"
    assert "must-not-be-returned" not in checker.configured_callback_url()
