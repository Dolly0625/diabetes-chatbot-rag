#!/usr/bin/env python3
"""Safe readiness check for the public HTTPS -> local LINE phone demo.

This checker is deliberately narrower than ``check_line_demo_readiness``.  It
only verifies the transport path that a phone webhook needs:

* an active HTTPS tunnel is visible through the local ngrok inspection API;
* the local app answers ``GET /health``;
* the configured callback is HTTPS and ends in ``/callback``;
* the public callback route exists (a GET is expected to return 405 from
  FastAPI; no LINE event is sent).

It never calls the LINE API, never sends a webhook event, and never prints a
complete public URL or any environment secret.  Values are read from the
process environment only; the checker intentionally does not parse ``.env``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


PASS = "PASS"
BLOCKED = "BLOCKED"
READY = "READY_FOR_LINE_PHONE_DEMO"
NOT_READY = "NOT_READY"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: str
    message: str
    hint: str


def configured_callback_url() -> str:
    """Read only LINE_CALLBACK_URL without exporting the rest of .env."""

    process_value = os.getenv("LINE_CALLBACK_URL", "").strip()
    if process_value:
        return process_value
    try:
        from dotenv import dotenv_values

        value = dotenv_values(PROJECT_ROOT / ".env").get("LINE_CALLBACK_URL")
        return str(value or "").strip()
    except Exception:
        return ""


def mask_url(raw: str, *, local: bool = False) -> str:
    """Return a display-safe URL, never exposing a complete public host."""

    try:
        parsed = urllib.parse.urlsplit(raw)
        scheme = parsed.scheme or "https"
        if local:
            host = "<local>"
        else:
            hostname = parsed.hostname or ""
            host = f"{hostname[:3]}***{hostname[-3:]}" if len(hostname) > 6 else "***"
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        # Deliberately omit port, userinfo, query, and fragment.  This keeps
        # accidental token-like URL components out of reports.
        return f"{scheme}://{host}{path}"
    except Exception:
        return "<invalid-url>"


def _is_local_hostname(hostname: str) -> bool:
    lowered = hostname.strip().lower().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback or ipaddress.ip_address(lowered).is_private
    except ValueError:
        return False


def _callback_url_error(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return "未提供 callback URL（只從 process environment 讀取）"
    try:
        parsed = urllib.parse.urlsplit(raw.strip())
    except ValueError:
        return "callback URL 格式無效"
    if parsed.scheme.lower() != "https":
        return "callback URL 必須使用 HTTPS"
    if not parsed.hostname or _is_local_hostname(parsed.hostname):
        return "callback URL 必須是公開 host，不可為 localhost／私有位址"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "callback URL 不可包含 userinfo、query 或 fragment"
    if parsed.path.rstrip("/") != "/callback":
        return "callback URL path 必須是 /callback"
    return None


def _request_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(512 * 1024)
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _http_status(url: str, timeout: float) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "tfda-line-demo-readiness/1"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        # 405 is the expected result for GET /callback: it proves the route
        # exists without posting a LINE-shaped event.
        return int(exc.code)


def discover_https_tunnel(
    tunnel_api_url: str,
    *,
    timeout: float = 1.5,
    request_json: Callable[[str, float], dict[str, Any]] = _request_json,
) -> tuple[str | None, CheckResult]:
    try:
        payload = request_json(tunnel_api_url, timeout)
        tunnels = payload.get("tunnels", [])
        if not isinstance(tunnels, list):
            tunnels = []
        for tunnel in tunnels:
            if not isinstance(tunnel, dict):
                continue
            candidate = str(tunnel.get("public_url") or "").strip()
            try:
                parsed = urllib.parse.urlsplit(candidate)
            except ValueError:
                continue
            if parsed.scheme.lower() == "https" and parsed.hostname:
                return candidate.rstrip("/"), CheckResult(
                    "active_tunnel",
                    PASS,
                    f"找到 active HTTPS tunnel：{mask_url(candidate)}",
                    "確認 tunnel 仍指向本機 app port 8000",
                )
        return None, CheckResult(
            "active_tunnel",
            BLOCKED,
            "找不到 active HTTPS tunnel",
            "啟動 ngrok（例如 ngrok http 8000），再重新執行 checker",
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None, CheckResult(
            "active_tunnel",
            BLOCKED,
            "無法讀取本機 tunnel inspection API",
            "確認 ngrok 正在執行，且本機 inspection API 可用",
        )
    except Exception:
        return None, CheckResult(
            "active_tunnel",
            BLOCKED,
            "tunnel inspection 失敗",
            "確認 ngrok 版本與本機 inspection API 設定",
        )


def run_checks(
    *,
    callback_url: str | None = None,
    app_url: str = "http://127.0.0.1:8000",
    tunnel_api_url: str = "http://127.0.0.1:4040/api/tunnels",
    timeout: float = 1.5,
    request_json: Callable[[str, float], dict[str, Any]] = _request_json,
    http_status: Callable[[str, float], int] = _http_status,
    require_public_probe: bool = True,
) -> list[CheckResult]:
    """Run safe transport checks; injected callables keep tests hermetic."""

    tunnel_url, tunnel_check = discover_https_tunnel(
        tunnel_api_url, timeout=timeout, request_json=request_json
    )
    checks = [tunnel_check]

    app_base = app_url.rstrip("/")
    try:
        health_status = http_status(app_base + "/health", timeout)
        if health_status == 200:
            checks.append(CheckResult(
                "local_app_health",
                PASS,
                f"local app /health 回應 HTTP {health_status}（{mask_url(app_base, local=True)}）",
                "保持 uvicorn 服務執行",
            ))
        else:
            checks.append(CheckResult(
                "local_app_health",
                BLOCKED,
                f"local app /health 回應 HTTP {health_status}（{mask_url(app_base, local=True)}）",
                "先修正 app 設定或啟動狀態；LINE 尚不能安全驗證",
            ))
    except (urllib.error.URLError, TimeoutError, OSError, socket.timeout):
        checks.append(CheckResult(
            "local_app_health",
            BLOCKED,
            f"local app /health 無法連線（{mask_url(app_base, local=True)}）",
            "啟動 uvicorn，預設監聽 localhost:8000",
        ))
    except Exception:
        checks.append(CheckResult(
            "local_app_health",
            BLOCKED,
            "local app /health 檢查失敗",
            "確認 app 的 HTTP health route 可用",
        ))

    configured_callback = (callback_url or configured_callback_url()).strip()
    callback_error = _callback_url_error(configured_callback)
    if callback_error:
        checks.append(CheckResult("callback_url", BLOCKED, callback_error, "設定公開 HTTPS host + /callback；不要把 secret 放進 URL"))
    else:
        parsed_callback = urllib.parse.urlsplit(configured_callback)
        if tunnel_url and parsed_callback.hostname != urllib.parse.urlsplit(tunnel_url).hostname:
            checks.append(CheckResult(
                "callback_tunnel_match",
                BLOCKED,
                "callback host 與 active tunnel host 不一致",
                f"將 LINE_CALLBACK_URL 指向目前 tunnel（目前設定：{mask_url(configured_callback)}）",
            ))
        else:
            checks.append(CheckResult(
                "callback_tunnel_match",
                PASS,
                f"callback host 與 active tunnel 一致（{mask_url(configured_callback)}）",
                "LINE Console 的 Webhook URL 必須使用同一個 /callback URL",
            ))

        if require_public_probe:
            try:
                status = http_status(configured_callback, timeout)
                if status == 405 or 200 <= status < 500:
                    checks.append(CheckResult(
                        "public_callback_route",
                        PASS,
                        f"公開 callback route 可達（GET 回應 HTTP {status}；未送出 webhook event）",
                        "LINE Console Verify 仍需人工完成",
                    ))
                else:
                    checks.append(CheckResult(
                        "public_callback_route",
                        BLOCKED,
                        f"公開 callback route 回應 HTTP {status}",
                        "確認 tunnel forwarding、app port 與 /callback path",
                    ))
            except (urllib.error.URLError, TimeoutError, OSError, socket.timeout):
                checks.append(CheckResult(
                    "public_callback_route",
                    BLOCKED,
                    "公開 callback route 無法連線",
                    "確認 tunnel online、DNS 與 firewall；checker 沒有送出 LINE event",
                ))
            except Exception:
                checks.append(CheckResult(
                    "public_callback_route",
                    BLOCKED,
                    "公開 callback route 檢查失敗",
                    "確認公開 URL 只包含 /callback 且 tunnel 指向 app",
                ))
    if not require_public_probe:
        checks.append(CheckResult(
            "public_callback_route",
            PASS,
            "略過公開 route probe（只做結構檢查）",
            "正式手機 demo 前請移除 --skip-public-probe",
        ))
    return checks


def compute_readiness(checks: list[CheckResult]) -> str:
    return READY if checks and all(item.status == PASS for item in checks) else NOT_READY


def _format_json(checks: list[CheckResult], readiness: str) -> str:
    return json.dumps(
        {
            "readiness": readiness,
            "summary": {
                PASS: sum(item.status == PASS for item in checks),
                BLOCKED: sum(item.status == BLOCKED for item in checks),
            },
            "checks": [asdict(item) for item in checks],
            "note": "no secrets or complete public URLs included",
        },
        ensure_ascii=False,
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全檢查 LINE 手機真機 Demo 的 HTTPS callback transport")
    parser.add_argument("--callback-url", default=None, help="公開 callback URL；只在 process argument 內使用，不會完整印出")
    parser.add_argument("--app-url", default=os.getenv("LINE_DEMO_APP_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--tunnel-api-url", default=os.getenv("LINE_DEMO_TUNNEL_API_URL", "http://127.0.0.1:4040/api/tunnels"))
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--skip-public-probe", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    checks = run_checks(
        callback_url=args.callback_url,
        app_url=args.app_url,
        tunnel_api_url=args.tunnel_api_url,
        timeout=max(0.1, min(args.timeout, 10.0)),
        require_public_probe=not args.skip_public_probe,
    )
    readiness = compute_readiness(checks)
    if args.json:
        print(_format_json(checks, readiness))
    elif args.quiet:
        print(readiness)
    else:
        print("LINE phone demo transport readiness")
        for item in checks:
            marker = "✓" if item.status == PASS else "✗"
            print(f"[{item.status:<7}] {marker} {item.id}: {item.message}")
            print(f"          → {item.hint}")
        print(f"結果：{readiness}")
        print("注意：本工具不呼叫 LINE API、不送出 LINE event；URL 只以遮罩形式顯示。")
    return 0 if readiness == READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
