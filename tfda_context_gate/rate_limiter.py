from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Any


class RateLimitInvocationError(RuntimeError):
    def __init__(self, message: str, timing: dict[str, float | int], original: Exception):
        super().__init__(message)
        self.timing = timing
        self.original = original


class RollingRequestRateLimiter:
    """A small persistent rolling-window limiter for sequential API experiments."""

    def __init__(
        self,
        state_path: Path,
        event_log_path: Path,
        max_requests: int = 20,
        window_seconds: float = 60.0,
    ) -> None:
        self.state_path = state_path
        self.event_log_path = event_log_path
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_timestamps(self) -> list[float]:
        if not self.state_path.exists():
            return []
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            timestamps = payload.get("timestamps", [])
            return [float(value) for value in timestamps]
        except (OSError, ValueError, TypeError):
            return []

    def _write_timestamps(self, timestamps: list[float]) -> None:
        self.state_path.write_text(
            json.dumps({"timestamps": timestamps}, indent=2), encoding="utf-8"
        )

    def _log(self, event: str, **payload: Any) -> None:
        record = {
            "timestamp": time.time(),
            "event": event,
            "max_requests": self.max_requests,
            "window_seconds": self.window_seconds,
            **payload,
        }
        with self.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def acquire(self, label: str) -> float:
        """Reserve one request slot and return the wait spent before sending."""
        started = time.perf_counter()
        while True:
            with self._lock:
                now = time.time()
                timestamps = [
                    timestamp
                    for timestamp in self._read_timestamps()
                    if now - timestamp < self.window_seconds
                ]
                if len(timestamps) < self.max_requests:
                    timestamps.append(now)
                    self._write_timestamps(timestamps)
                    waited = time.perf_counter() - started
                    if waited > 0.001:
                        self._log(
                            "rate_limit_wait_complete",
                            label=label,
                            wait_seconds=waited,
                            active_requests=len(timestamps),
                        )
                    # `waited` already includes every sleep in this call.
                    # Do not add the individual sleeps a second time.
                    return waited
                wait_seconds = max(0.0, timestamps[0] + self.window_seconds - now)
                self._log(
                    "rate_limit_wait",
                    label=label,
                    wait_seconds=wait_seconds,
                    active_requests=len(timestamps),
                )
            if wait_seconds > 0:
                time.sleep(wait_seconds)

    def log_retry(self, label: str, retry_index: int, wait_seconds: float, source: str, error: str) -> None:
        self._log(
            "retry_wait",
            label=label,
            retry_index=retry_index,
            wait_seconds=wait_seconds,
            source=source,
            error=error[:500],
        )


def _headers_from_exception(error: Exception) -> dict[str, Any]:
    for candidate in (
        getattr(error, "response", None),
        getattr(error, "http_response", None),
    ):
        headers = getattr(candidate, "headers", None)
        if headers:
            return {str(key).lower(): value for key, value in dict(headers).items()}
    headers = getattr(error, "headers", None)
    if headers:
        return {str(key).lower(): value for key, value in dict(headers).items()}
    return {}


def is_rate_limit_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    text = str(error).lower()
    return status_code == 429 or "429" in text or "rate limit" in text or "too many requests" in text


def retry_after_seconds(error: Exception) -> float | None:
    headers = _headers_from_exception(error)
    value = headers.get("retry-after")
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    match = re.search(r"retry[-_ ]after\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", str(error), re.I)
    return float(match.group(1)) if match else None


def invoke_with_rate_limit(
    call: Callable[[], Any],
    limiter: RollingRequestRateLimiter,
    label: str,
    max_retries: int = 4,
    backoff_base_seconds: float = 2.0,
    backoff_cap_seconds: float = 60.0,
) -> tuple[Any, dict[str, float | int]]:
    wall_started = time.perf_counter()
    model_latency = 0.0
    rate_limit_wait = 0.0
    retry_wait = 0.0
    retry_count = 0
    while True:
        rate_limit_wait += limiter.acquire(label)
        model_started = time.perf_counter()
        try:
            response = call()
            model_latency += time.perf_counter() - model_started
            return response, {
                "model_latency": model_latency,
                "rate_limit_wait_time": rate_limit_wait,
                "retry_wait_time": retry_wait,
                "total_wall_time": time.perf_counter() - wall_started,
                "retry_count": retry_count,
            }
        except Exception as error:
            model_latency += time.perf_counter() - model_started
            if not is_rate_limit_error(error) or retry_count >= max_retries:
                timing = {
                    "model_latency": model_latency,
                    "rate_limit_wait_time": rate_limit_wait,
                    "retry_wait_time": retry_wait,
                    "total_wall_time": time.perf_counter() - wall_started,
                    "retry_count": retry_count,
                }
                limiter._log("request_failed", label=label, error=str(error)[:500], **timing)
                raise RateLimitInvocationError(
                    f"Request failed after {retry_count} retries: {error}", timing, error
                ) from error
            retry_count += 1
            retry_after = retry_after_seconds(error)
            wait_seconds = (
                retry_after
                if retry_after is not None
                else min(backoff_cap_seconds, backoff_base_seconds * (2 ** (retry_count - 1)))
            )
            retry_wait += wait_seconds
            limiter.log_retry(
                label,
                retry_count,
                wait_seconds,
                "retry-after" if retry_after is not None else "exponential_backoff",
                str(error),
            )
            time.sleep(wait_seconds)
