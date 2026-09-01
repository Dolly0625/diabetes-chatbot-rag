"""Deterministic P2B response phrasing.

This module changes presentation only.  It accepts facts that were already
selected by the interpreter/intake merge and never classifies intent, invents
clinical content, or calls a model.  Keeping the composer pure makes the
natural-language layer cheap to test and guarantees that safety decisions
remain in the existing orchestration and output gates.
"""

from __future__ import annotations

import re

from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS


# One focused question per turn.  These intentionally retain the existing
# field cues used by quick replies and tests, while removing form-like framing.
_NATURAL_INTAKE_QUESTIONS: dict[str, str] = {
    "known_medications": "先從用藥開始：目前有固定吃藥或打胰島素嗎？知道藥名就直接說，不確定也沒關係。",
    "allergies": "接著想確認過敏：有沒有對特定藥物或食物過敏？沒有或不確定，直接說也可以。",
    "chronic_conditions": "再確認一下病史：除了糖尿病，還有高血壓、高血脂等慢性病嗎？沒有可以直接說沒有。",
    "family_history": "家人中（如父母、兄弟姊妹）有人有糖尿病或相關疾病嗎？不確定也可以先說。",
    "symptom_onset": "接著是症狀時間：這次想看診的狀況，大約從什麼時候開始的呢？",
    "symptom_description": "目前最主要的不舒服或症狀是什麼？照你平時的感覺描述就可以。",
    "symptom_severity": "最後想了解程度：如果用輕度、中度、重度，或 1–10 分，你會怎麼形容？",
    "questions_for_doctor": "這次看診最想問醫師什麼問題？還沒想到也沒關係，可以先跳過或說沒有。",
    "time_frame": "這些症狀是現在發生、以前發生過，還是假設性詢問？",
    "target_subject": "這些症狀是你本人、家人，還是其他對象的情況？",
    "medicine_name": "目前使用的藥物名稱或成分是什麼？不確定也可以直接說。",
    "medication_class": "目前使用的是哪一類糖尿病藥物？不確定可以先說不知道。",
    "drug_type": "目前使用的是哪一類糖尿病藥物？不確定可以先說不知道。",
    "symptom": "目前具體有哪些症狀或不舒服？照你的感覺描述就可以。",
}


def compose_intake_question(field: str | None) -> str | None:
    """Return a short, single-field prompt without changing field selection."""

    if field is None:
        return None
    return _NATURAL_INTAKE_QUESTIONS.get(field) or INTAKE_FIELD_QUESTIONS.get(field)


def compose_side_answer(answer: str | None, pending_question: str | None) -> str:
    """Answer the user's detour without immediately pulling them back to a form."""

    base = (answer or "").strip() or "這題我先回答到這裡。"
    if not pending_question:
        return base
    return f"{base}\n\n看診資料我先幫你留著；想繼續整理時，跟我說「繼續整理」就好。"


def compose_single_confirmation(raw: str, label: str) -> str:
    """Confirm an already-normalized field with respectful human phrasing."""

    raw_value = (raw or "剛才的內容").strip()[:30]
    normalized = (label or "這一項").strip()[:40]
    return f"好的，已幫你記下「{normalized}」；如果哪裡不對，直接告訴我就好。"


def compose_multi_confirmation(base: str, labels: str) -> str:
    """Make the multi-field confirmation natural and easy to act on."""

    if not base:
        return base
    label_text = (labels or "").strip()
    suffix = f"（已分別記在「{label_text}」）" if label_text else ""
    return f"{base}{suffix} 如果哪一項不對，直接告訴我就好。"


def compose_implicit_confirmation(base: str | None) -> str:
    """Add warm, empathetic human phrasing to implicit confirmations."""

    if not base:
        return "好的，已為你記錄剛才的內容。"

    # Extract raw and normalized if base follows standard format
    m = re.search(r"你提到「(.*?)」，我記為「(.*?)」", base)
    if m:
        raw, norm = m.group(1), m.group(2)
        # 1. 否定/無 (Negative / None)
        if any(neg in norm or neg in raw for neg in ("目前無用藥", "沒有吃藥", "無用藥", "沒有用藥", "無過敏", "沒有過敏", "無慢性病", "沒有慢性病", "無家族史", "沒有家族史", "無")):
            if "用藥" in norm or "吃藥" in raw:
                return "好的，了解，目前沒有固定用藥；如果有需要修改隨時告訴我。"
            if "過敏" in norm or "過敏" in raw:
                return "好的，已幫你記下無特殊過敏史；如果有需要修改隨時告訴我。"
            if "慢性病" in norm or "慢性病" in raw:
                return "收到，已記錄目前無其他慢性病史；如果有需要修改隨時告訴我。"
            if "家族" in norm or "家族" in raw:
                return "好的，已記錄無相關家族病史；如果有需要修改隨時告訴我。"
            return f"好的，已為你記錄「{norm}」；如果有需要修改隨時告訴我。"

        # 2. 症狀不適 (Discomfort / Symptoms)
        symptom_cues = ("口渴", "口乾", "頻尿", "尿尿", "跑廁所", "頭暈", "視力", "疲倦", "很累", "睡不好", "手麻", "腳麻", "不舒服", "疼痛", "傷口", "模糊")
        if any(cue in raw or cue in norm for cue in symptom_cues):
            return f"辛苦了，這些不適一定很困擾。我已幫你記下狀況「{norm}」，讓醫師看診時評估；如果不對隨時告訴我。"

        # 3. 不確定 / 待確認 (Uncertain)
        if any(unc in raw or unc in norm for unc in ("不確定", "待確認", "不知道", "不記得", "忘了", "忘記", "待看診")):
            return f"沒關係的！我先幫你記下「{norm}」，看診時再由醫師或藥袋進一步確認；如果不對隨時告訴我。"

        # 4. 一般具體數值/用藥/病史
        return f"好的，已幫你記下「{norm}」；如果不對直接告訴我。"

    if "如果不對" in base or "若要修改" in base or "隨時告訴我" in base or "對嗎" in base:
        return base
    return f"{base} 如果不對，直接告訴我就好。"


def compose_correction(value: str, labels: str | None = None) -> str:
    """Acknowledge a correction while preserving the existing data boundary."""

    label_text = f"（{labels}）" if labels else ""
    return f"已更新成「{(value or '').strip()[:120]}」{label_text}；其他已填資料會保留。"


def compose_uncertain(*, symptom: bool) -> str:
    """Record uncertainty honestly; never turn it into a guessed value."""

    if symptom:
        return "沒關係，這項先記成「待確認」；看診時再和醫師確認，之後想補充也可以。"
    return "沒關係，我先把這項記成「待看診確認」，不替你猜；之後想補充再告訴我。"


def compose_none_answer() -> str:
    """Acknowledge an explicit negative answer without medical inference."""

    return "好，我先記成目前沒有；我們接著看下一項。"


def compose_question_added(raw: str, label: str = "想問醫師的問題") -> str:
    """Confirm a user-provided doctor question in plain language."""

    snippet = (raw or "這個問題").strip()[:30]
    return f"你說的「{snippet}」我已幫你加到「{label}」，其他資料會保留。"


def compose_unknown_candidate() -> str:
    """Explain that an unrecognized candidate is being left for follow-up."""

    return "這項我先不猜，請直接補充或告訴我不確定；資料會留給看診時和醫師確認。"


__all__ = [
    "compose_intake_question",
    "compose_side_answer",
    "compose_single_confirmation",
    "compose_multi_confirmation",
    "compose_implicit_confirmation",
    "compose_correction",
    "compose_uncertain",
    "compose_none_answer",
    "compose_question_added",
    "compose_unknown_candidate",
]
