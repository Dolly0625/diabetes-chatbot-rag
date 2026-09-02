"""看診前整理入口與 resume 控制的呈現 adapter。

嚴格 metadata 驅動，不臆測草稿。

- 入口：使用者點或說「我要準備看診」時說明將進入專用流程，而非悄悄沿用舊資料。
- Resume：僅當 orchestrator 回覆的 OrchestratorResult.metadata 同時滿足
  requires_resume_decision is True 且 has_existing_draft is True 時，
  才顯示「繼續上次整理 / 開始新的整理 / 取消整理」三按鈕。
- 若 metadata 缺失或任一為 False / 非 True，維持既有一般入口，不顯示 resume 控制，
  絕不以回覆中文 substring 猜測有草稿。

此 adapter 不依賴 orchestrator 文字，僅消費明確 boolean，讓 UI contract 可透過
合成 result metadata 直接測試，無需等待第一組完成。
"""

from __future__ import annotations

from typing import Any

# ── 契約字串（不可改） ──────────────────────────────────────────────
RESUME_CONTRACT_TEXT_RESUME = "繼續上次整理"
RESUME_CONTRACT_TEXT_RESTART = "開始新的整理"
RESUME_CONTRACT_TEXT_CANCEL = "取消整理"

ENTRY_TRIGGER_TEXTS: set[str] = {"我要準備看診"}

# 專用入口說明：強調專用流程 vs 悄悄沿用舊資料
INTAKE_ENTRY_EXPLANATION = (
    "將為你開啟「看診前整理」專用流程，會分段收集用藥、症狀與想問醫師的問題，"
    "完成後可產生摘要供看診使用。不會悄悄沿用舊資料，完成後可由你確認或重新開始。"
)

# 三按鈕（label 與 text 嚴格等於契約字串，不可映射到舊字串）
RESUME_CHOICE_ACTIONS: list[dict[str, str]] = [
    {"label": RESUME_CONTRACT_TEXT_RESUME, "text": RESUME_CONTRACT_TEXT_RESUME},
    {"label": RESUME_CONTRACT_TEXT_RESTART, "text": RESUME_CONTRACT_TEXT_RESTART},
    {"label": RESUME_CONTRACT_TEXT_CANCEL, "text": RESUME_CONTRACT_TEXT_CANCEL},
]


def is_entry_trigger(text: str) -> bool:
    """精準判斷是否為入口觸發句。只做 strip 精準匹配，不做 substring/模糊匹配。"""
    try:
        return (text or "").strip() == "我要準備看診"
    except Exception:
        return False


def build_intake_entry_message() -> str:
    """入口卡片/訊息文字：說明專用流程且不會悄悄沿用舊資料。"""
    return INTAKE_ENTRY_EXPLANATION


def build_resume_choice_actions() -> list[dict[str, str]]:
    """回傳三按鈕拷貝，label/text 嚴格等於契約字串。"""
    return [dict(item) for item in RESUME_CHOICE_ACTIONS]


def _extract_metadata(result: Any) -> dict[str, Any] | None:
    """從 OrchestratorResult / dict 提取 metadata dict，失敗回 None。"""
    if result is None:
        return None
    # Pydantic model: getattr(result, "metadata", None)
    try:
        md = getattr(result, "metadata", None)
        if isinstance(md, dict):
            return md
        if md is not None and isinstance(md, dict):
            return md
    except Exception:
        pass
    # dict-like result
    try:
        if isinstance(result, dict):
            md2 = result.get("metadata")
            if isinstance(md2, dict):
                return md2
            # also support top-level keys for synthetic tests
            # e.g. {"requires_resume_decision": True, "has_existing_draft": True}
            if isinstance(md2, dict) is False and ("requires_resume_decision" in result or "has_existing_draft" in result):
                return result
    except Exception:
        pass
    # fallback: if result itself is dict containing the booleans directly
    try:
        if isinstance(result, dict) and ("requires_resume_decision" in result or "has_existing_draft" in result):
            return result
    except Exception:
        pass
    return None


def should_show_resume_controls(result: Any) -> bool:
    """嚴格 metadata 判斷：僅當兩 boolean 皆為 True 才顯示 resume 三選一。

    - requires_resume_decision is True 且 has_existing_draft is True → True
    - 任一缺失、非 True、或 metadata 完全不存在 → False（維持既有一般入口）
    - 絕不讀 reply/status 中文，不臆測草稿。
    """
    md = _extract_metadata(result)
    if not isinstance(md, dict):
        return False
    # 支援巢狀：metadata 內可能再包 resume 欄位
    # 優先讀頂層 requires_resume_decision / has_existing_draft
    req = md.get("requires_resume_decision")
    has = md.get("has_existing_draft")
    # 也支援 metadata.resume.requires 形式（未來擴充），但不作為必要
    if req is None:
        try:
            resume_nested = md.get("resume")
            if isinstance(resume_nested, dict):
                req = resume_nested.get("requires_resume_decision", req)
                has = resume_nested.get("has_existing_draft", has)
        except Exception:
            pass
    # 明確布林 True 才算，字串 "true" / 1 皆不算，避免寬鬆誤觸
    return req is True and has is True


def get_resume_actions_for_result(result: Any) -> list[dict[str, str]] | None:
    """若 should_show_resume_controls 為 True 則回三按鈕，否則 None。"""
    if should_show_resume_controls(result):
        return build_resume_choice_actions()
    return None


def build_entry_enriched_reply(original_reply: str, *, is_entry: bool) -> str:
    """若為入口觸發，則在原 orchestrator 回覆前加上專用入口說明。

    - is_entry False → 原樣返回
    - original_reply 為空/空白 → 只回入口說明
    - 否則 → f"{入口說明}\\n\\n{original_reply}"
    """
    if not is_entry:
        return original_reply
    entry = build_intake_entry_message()
    if not original_reply or not original_reply.strip():
        return entry
    return f"{entry}\n\n{original_reply.strip()}"


__all__ = [
    "RESUME_CONTRACT_TEXT_RESUME",
    "RESUME_CONTRACT_TEXT_RESTART",
    "RESUME_CONTRACT_TEXT_CANCEL",
    "ENTRY_TRIGGER_TEXTS",
    "INTAKE_ENTRY_EXPLANATION",
    "RESUME_CHOICE_ACTIONS",
    "is_entry_trigger",
    "build_intake_entry_message",
    "build_resume_choice_actions",
    "should_show_resume_controls",
    "get_resume_actions_for_result",
    "build_entry_enriched_reply",
]
