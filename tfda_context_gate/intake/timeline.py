"""Timeline builder for pre-visit intake (p6.2) — 8 fields.

Never fabricates history: only organizes provided facts.
Sorted by onset; entries without onset are placed last in original order.
"""

from __future__ import annotations

import re
from datetime import datetime

from .schemas import PreVisitIntake, TimelineEntry


def _normalize_onset(onset: str | None) -> str | None:
    """Normalize onset for sorting without fabricating.

    - If onset is None or empty → None (no sort key)
    - Try to parse ISO date (YYYY-MM-DD) → use as sort key
    - Try to parse relative like "3天前", "昨天", "上週" → keep original but assign heuristic key
    - Otherwise → use original string as key (lexicographic fallback)
    Never invents a date when not provided.
    """
    if not onset or not onset.strip():
        return None
    s = onset.strip()
    # ISO date
    try:
        # Try YYYY-MM-DD or YYYY/MM/DD
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Try YYYY-MM
        try:
            dt = datetime.strptime(s, "%Y-%m")
            return dt.strftime("%Y-%m")
        except ValueError:
            pass
    except Exception:
        pass
    # Relative expressions: keep as-is but return original for stable sort
    # Do not convert to absolute date (would be fabrication)
    return s


def build_timeline(intake: PreVisitIntake) -> list[TimelineEntry]:
    """Build timeline sorted by onset, never fabricates history.

    Rules:
    - If symptom_description is None/empty and symptom_onset is None → empty timeline
    - If symptom_description provided → one entry with that description + onset + medications snapshot
    - If multiple medications with different onsets? v0.2 only has single onset field, so one entry.
    - Sorted: entries with onset first (by normalized key), then without onset.
    - Never invents missing onset/description/medications.
    - Includes symptom_severity in description if provided.

    Args:
        intake: validated PreVisitIntake (only provided facts)
    Returns:
        Sorted list[TimelineEntry]
    """
    entries: list[TimelineEntry] = []

    # Only create entry if there is at least one provided fact
    has_symptom = bool(intake.symptom_description and intake.symptom_description.strip())
    has_onset = bool(intake.symptom_onset and intake.symptom_onset.strip())
    has_meds = bool(intake.known_medications)
    has_severity = bool(intake.symptom_severity and intake.symptom_severity.strip())

    if not has_symptom and not has_onset and not has_meds and not has_severity:
        return []

    # Single entry for v0.2 (one symptom_description + one onset)
    # If symptom_description missing but onset/meds present, still create entry with placeholder
    # but never fabricate description: use onset or meds as description fallback only if provided
    if has_symptom:
        desc = intake.symptom_description.strip()  # type: ignore[union-attr]
        if has_severity:
            desc = f"{desc}（程度：{intake.symptom_severity.strip()}）"  # type: ignore[union-attr]
    elif has_onset:
        desc = f"症狀起始：{intake.symptom_onset.strip()}"  # type: ignore[union-attr]
        if has_severity:
            desc += f"（程度：{intake.symptom_severity.strip()}）"  # type: ignore[union-attr]
    elif has_meds:
        desc = f"已知用藥：{', '.join(intake.known_medications)}"
    elif has_severity:
        desc = f"症狀程度：{intake.symptom_severity.strip()}"  # type: ignore[union-attr]
    else:
        return []

    entry = TimelineEntry(
        onset=intake.symptom_onset.strip() if has_onset else None,  # type: ignore[union-attr]
        description=desc,
        medications=list(intake.known_medications),
        sort_key=_normalize_onset(intake.symptom_onset),
    )
    entries.append(entry)

    # Future: if intake had multiple symptom events, would create multiple entries here.
    # For now, single entry is sufficient and never fabricates.

    # Sort: entries with sort_key first (lexicographic), None last, stable
    def sort_key_fn(e: TimelineEntry):
        # (has_no_key, key) → None last
        return (e.sort_key is None, e.sort_key or "")

    entries.sort(key=sort_key_fn)
    return entries


def build_timeline_from_entries(entries: list[TimelineEntry]) -> list[TimelineEntry]:
    """Sort arbitrary entries by onset, never fabricates.

    Used when multiple entries are provided (e.g., future multi-event intake).
    """
    for e in entries:
        if e.sort_key is None and e.onset:
            e.sort_key = _normalize_onset(e.onset)
    return sorted(entries, key=lambda e: (e.sort_key is None, e.sort_key or ""))
