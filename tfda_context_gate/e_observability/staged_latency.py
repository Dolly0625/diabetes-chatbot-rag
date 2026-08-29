"""Staged latency measurement helper for P2A.1 Phase 2 Task A.

Records per-turn stage timings into E trace without PII.
Fields: red_flag_and_auth_ms, conversation_interpreter_ms, candidate_validation_ms,
        rag_retrieval_ms, answer_generator_ms, b_gate_ms, d_gate_ms, persistence_ms, total_ms
plus process-first and session-first labels.  A first measurement is not a
claim about model/container coldness; only a real process restart can provide
that guarantee.

Usage:
    recorder = StagedLatencyRecorder()
    with recorder.stage("red_flag_and_auth_ms"):
        risk = policy.classify(text)
    ...
    timings = recorder.snapshot()  # dict of ms floats, no PII
    # merge into WorkflowResult.trace or OrchestratorResult trace:
    trace_dict["staged_latency"] = timings

Process labels: module-level _IS_FIRST_CALL initially True; first snapshot
flips to False.  Session labels are tracked independently when a session_id
is supplied.  Deprecated ``is_cold_start``/``is_warm_run`` aliases map to the
process labels for backwards compatibility and must not be called model-cold.

No PII, no raw medical text, no tokens/secrets — timings only.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

# Module-level process-order flag. Process-local. True until first staged measurement completes.
_IS_FIRST_CALL: bool = True
_COLD_START_LOCK = None

try:
    import threading as _threading
    _COLD_START_LOCK = _threading.Lock()
except Exception:
    _COLD_START_LOCK = None  # type: ignore[assignment]

# Ordered stage names per Task A spec
STAGE_KEYS = [
    "red_flag_and_auth_ms",
    "conversation_interpreter_ms",
    "candidate_validation_ms",
    "rag_retrieval_ms",
    "answer_generator_ms",
    "b_gate_ms",
    "d_gate_ms",
    "persistence_ms",
    "total_ms",
]

# Subset measured outside workflow (orchestrator) vs inside workflow (runner)
ORCHESTRATOR_STAGES = {"red_flag_and_auth_ms", "conversation_interpreter_ms", "candidate_validation_ms", "persistence_ms", "total_ms"}
WORKFLOW_STAGES = {"rag_retrieval_ms", "answer_generator_ms", "b_gate_ms", "d_gate_ms", "total_ms"}


def _consume_cold_flag() -> bool:
    """Return True if this is the first call (cold), then flip to False thread-safely."""
    global _IS_FIRST_CALL
    if _COLD_START_LOCK is not None:
        with _COLD_START_LOCK:
            is_cold = _IS_FIRST_CALL
            if _IS_FIRST_CALL:
                _IS_FIRST_CALL = False
            return is_cold
    else:
        is_cold = _IS_FIRST_CALL
        if _IS_FIRST_CALL:
            _IS_FIRST_CALL = False
        return is_cold


_SEEN_SESSIONS: set[str] = set()


def _consume_session_first_turn(session_id: str) -> bool:
    """Return whether this is the first measured turn for ``session_id``."""

    if _COLD_START_LOCK is not None:
        with _COLD_START_LOCK:
            if session_id in _SEEN_SESSIONS:
                return False
            _SEEN_SESSIONS.add(session_id)
            return True
    if session_id in _SEEN_SESSIONS:
        return False
    _SEEN_SESSIONS.add(session_id)
    return True


def _reset_cold_flag_for_tests() -> None:
    """Test helper: reset cold flag to True. Not for production."""
    global _IS_FIRST_CALL, _SEEN_SESSIONS
    if _COLD_START_LOCK is not None:
        with _COLD_START_LOCK:
            _IS_FIRST_CALL = True
            _SEEN_SESSIONS.clear()
    else:
        _IS_FIRST_CALL = True
        _SEEN_SESSIONS.clear()


class StagedLatencyRecorder:
    """Per-turn staged latency recorder. Thread-safe per instance, not shared."""

    def __init__(self, is_cold_start: bool | None = None, session_id: str | None = None) -> None:
        self._starts: dict[str, float] = {}
        self._elapsed: dict[str, float] = {}
        self._total_start = time.perf_counter()
        # Keep constructor compatibility; the canonical output label is
        # process-first-measurement, not cold-start.
        self._process_first: bool | None = is_cold_start
        self._session_first: bool | None = None
        self._session_id: str | None = session_id

    @contextmanager
    def stage(self, name: str):
        """Context manager to measure a stage. Usage: with recorder.stage('b_gate_ms'): ..."""
        start = time.perf_counter()
        self._starts[name] = start
        try:
            yield
        finally:
            end = time.perf_counter()
            elapsed_ms = max(0.0, (end - start) * 1000.0)
            # Accumulate if stage measured multiple times (sum)
            prev = self._elapsed.get(name, 0.0)
            self._elapsed[name] = round(prev + elapsed_ms, 3)

    def record(self, name: str, elapsed_ms: float) -> None:
        """Directly record a timing (if already measured elsewhere)."""
        prev = self._elapsed.get(name, 0.0)
        self._elapsed[name] = round(prev + max(0.0, elapsed_ms), 3)

    def snapshot(self) -> dict[str, Any]:
        """Return timings plus process-order and optional session-order flags."""
        # Ensure total_ms measured from construction to now
        total_elapsed = max(0.0, (time.perf_counter() - self._total_start) * 1000.0)
        if "total_ms" not in self._elapsed:
            self._elapsed["total_ms"] = round(total_elapsed, 3)
        else:
            # If total already partially recorded, ensure at least total_elapsed is max
            self._elapsed["total_ms"] = round(max(self._elapsed["total_ms"], total_elapsed), 3)

        if self._process_first is None:
            self._process_first = _consume_cold_flag()
        if self._session_id is not None and self._session_first is None:
            self._session_first = _consume_session_first_turn(self._session_id)
        is_process_first = bool(self._process_first)
        is_session_first = bool(self._session_first) if self._session_id is not None else None
        out: dict[str, Any] = {}
        for k in STAGE_KEYS:
            out[k] = round(self._elapsed.get(k, 0.0), 3)
        out["is_process_first_measurement"] = is_process_first
        out["is_warm_process_measurement"] = not is_process_first
        if is_session_first is not None:
            out["is_session_first_turn"] = is_session_first
            out["is_warm_session_turn"] = not is_session_first
        # Backwards-compatible aliases.  They deliberately refer to process
        # ordering, not a model/container cold start.
        out["is_cold_start"] = is_process_first
        out["is_warm_run"] = not is_process_first
        # Also provide total as top-level for convenience compatibility
        return out

    def merge_into_trace(self, trace_dict: dict[str, Any]) -> dict[str, Any]:
        """Merge snapshot into a trace dict under staged_latency key (mutates and returns)."""
        if not isinstance(trace_dict, dict):
            return trace_dict
        trace_dict["staged_latency"] = self.snapshot()
        return trace_dict
