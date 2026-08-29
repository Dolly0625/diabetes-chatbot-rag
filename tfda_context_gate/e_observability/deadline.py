"""Bounded deadline execution for user-facing dependency calls.

``Future.result(timeout=...)`` only bounds the caller.  A worker that is
already inside an HTTP call may continue after the deadline, so this module
uses a small shared executor with an admission semaphore:

* at most ``MAX_DEADLINE_WORKERS`` calls can be queued/running;
* a timed-out running call keeps its slot until the callable actually exits;
* when all slots are occupied, a new call fails closed immediately instead of
  growing an unbounded queue or creating another executor;
* callers never call ``shutdown(wait=True)`` on the timeout path.

The worker is not forcibly killed (Python cannot safely kill a thread).  The
``DeadlineGuard`` is propagated through a context variable so downstream
side-effect code can reject late persistence or push operations.  Native
client timeouts should still be configured at the HTTP client boundary.
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, TypeVar

T = TypeVar("T")

MAX_DEADLINE_WORKERS = 5

# A single bounded pool is deliberately never shut down per request.  A
# timed-out network call may still occupy its worker; the semaphore therefore
# represents both executor capacity and the bounded admission queue.
_SHARED_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_DEADLINE_WORKERS,
    thread_name_prefix="deadline-shared",
)
_SHARED_CAPACITY = threading.BoundedSemaphore(MAX_DEADLINE_WORKERS)

_ACTIVE_GUARD: contextvars.ContextVar["DeadlineGuard | None"] = contextvars.ContextVar(
    "tfda_active_deadline_guard", default=None
)


class DeadlineGuard:
    """Monotonic deadline and abandoned flag for side-effect gating."""

    def __init__(self, timeout_s: float | None):
        self.timeout_s = timeout_s
        self.deadline_monotonic: float | None = None
        self._abandoned = False
        self._lock = threading.Lock()
        if timeout_s is not None and timeout_s > 0:
            self.deadline_monotonic = time.monotonic() + float(timeout_s)

    def mark_abandoned(self) -> None:
        with self._lock:
            self._abandoned = True

    def is_abandoned(self) -> bool:
        with self._lock:
            return self._abandoned

    def is_expired(self) -> bool:
        if self.deadline_monotonic is None:
            return False
        return time.monotonic() >= self.deadline_monotonic

    def should_abort(self) -> bool:
        """Whether a late worker must stop before persistence or push."""
        return self.is_abandoned() or self.is_expired()

    def remaining_s(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())


def current_deadline_guard() -> DeadlineGuard | None:
    """Return the guard active in the current deadline worker, if any."""

    return _ACTIVE_GUARD.get()


def deadline_scope_active() -> bool:
    """True while running inside a worker owned by ``run_with_deadline``."""

    return current_deadline_guard() is not None


def _call_in_scope(
    guard: DeadlineGuard,
    func: Callable[..., T],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> T:
    # Preserve the outer guard for nested inline calls.  Replacing it would
    # let a longer inner timeout outlive the caller's already-expired budget.
    active_guard = _ACTIVE_GUARD.get() or guard
    token = _ACTIVE_GUARD.set(active_guard)
    try:
        return func(*args, **kwargs)
    finally:
        _ACTIVE_GUARD.reset(token)


def _safe_fallback(fallback: Callable[[], T] | None) -> T | None:
    # Fallbacks are application-owned and should be deterministic/quick.  Do
    # not hide a fallback programming error behind the deadline machinery.
    return fallback() if fallback is not None else None


def run_with_deadline(
    func: Callable[..., T],
    *args: Any,
    timeout_s: float | None,
    fallback: Callable[[], T] | None = None,
    use_shared: bool = True,
    **kwargs: Any,
) -> tuple[T | None, bool, DeadlineGuard]:
    """Run ``func`` with bounded caller wait.

    Returns ``(result, timed_out, guard)``.  ``use_shared`` remains accepted
    for API compatibility; all timed calls use the one bounded pool.  This is
    intentional: creating a per-call executor would allow abandoned workers
    to accumulate without a hard upper bound.

    If called from inside another deadline worker, execute inline.  This avoids
    nested-pool deadlock (an outer worker cannot consume all pool slots while
    waiting for an inner submission).  The outer deadline remains the hard
    caller bound; native HTTP timeouts remain responsible for the dependency.
    """

    del use_shared  # compatibility flag; bounded shared execution is mandatory

    if timeout_s is None or timeout_s <= 0:
        guard = DeadlineGuard(None)
        return func(*args, **kwargs), False, guard

    guard = DeadlineGuard(timeout_s)

    # Formal runner and orchestrator can both enforce a boundary.  Do not
    # submit a nested future from a deadline worker: that can deadlock when all
    # five outer workers are waiting for five inner workers.
    parent_guard = current_deadline_guard()
    if parent_guard is not None:
        if parent_guard.should_abort():
            guard.mark_abandoned()
            return _safe_fallback(fallback), True, guard
        result = _call_in_scope(guard, func, args, kwargs)
        if guard.should_abort() or parent_guard.should_abort():
            guard.mark_abandoned()
            return _safe_fallback(fallback), True, guard
        return result, False, guard

    # Admission is non-blocking.  A full pool means all bounded workers are
    # either running or already handling a timed-out call; queueing another
    # request would only create unbounded stale work.
    if not _SHARED_CAPACITY.acquire(blocking=False):
        guard.mark_abandoned()
        return _safe_fallback(fallback), True, guard

    try:
        future: Future[T] = _SHARED_EXECUTOR.submit(_call_in_scope, guard, func, args, kwargs)
    except Exception:
        _SHARED_CAPACITY.release()
        raise
    released = False
    release_lock = threading.Lock()

    def _release_capacity(_future: Future[T]) -> None:
        nonlocal released
        with release_lock:
            if not released:
                released = True
                _SHARED_CAPACITY.release()

    # Capacity is released only when the callable has exited, not when the
    # caller gives up waiting.  This is what makes abandoned work bounded.
    future.add_done_callback(_release_capacity)
    try:
        result = future.result(timeout=float(timeout_s))
        if guard.should_abort():
            guard.mark_abandoned()
            return _safe_fallback(fallback), True, guard
        return result, False, guard
    except FuturesTimeoutError:
        guard.mark_abandoned()
        try:
            future.cancel()  # cancels only work that has not started
        except Exception:
            pass
        return _safe_fallback(fallback), True, guard
    finally:
        # Never wait for a running HTTP call here.  The shared executor owns
        # the worker until it exits and the done callback releases capacity.
        pass


def fire_and_forget_with_deadline(
    func: Callable[..., Any],
    *args: Any,
    timeout_s: float | None = None,
    daemon: bool = True,
    **kwargs: Any,
) -> tuple[threading.Thread, DeadlineGuard]:
    """Launch a small daemon thread with a guard for asynchronous side effects.

    This helper is reserved for non-blocking push orchestration.  The bounded
    deadline pool above is used for dependency calls; this thread only owns
    the surrounding callback and must check the returned guard before writes.
    """

    guard = DeadlineGuard(timeout_s)

    def _wrapped() -> None:
        token = _ACTIVE_GUARD.set(guard)
        try:
            func(*args, **kwargs)
        except Exception:
            pass
        finally:
            _ACTIVE_GUARD.reset(token)

    thread = threading.Thread(target=_wrapped, daemon=daemon)
    thread.start()
    return thread, guard


__all__ = [
    "DeadlineGuard",
    "MAX_DEADLINE_WORKERS",
    "current_deadline_guard",
    "deadline_scope_active",
    "fire_and_forget_with_deadline",
    "run_with_deadline",
]
