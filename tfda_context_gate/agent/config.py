from __future__ import annotations

# ── 有界 Agent 上限（繁中註解）──────────────────────────────────────────────
# 設計原則：上限由系統持有、不可被 Planner 覆蓋或透過提示詞注入改變。
# AGENT_LIMITS 預設：max_agent_steps=2（最多進 Planner 2 次）、max_rewrites=1、max_clarifications=1
# 超限時由圖強制改為 FALLBACK（LIMIT_EXCEEDED），確保有界終止。

from pydantic import BaseModel, ConfigDict, Field


class AgentLimits(BaseModel):
    """System-owned bounds; these values are never exposed to the Planner.

    【繁中註解】系統擁有的有界上限，Planner 無法讀取或修改，僅圖節點用於強制封閉。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_agent_steps: int = Field(default=2, ge=1)  # 最多 Agent 步數（預設 2）
    max_rewrites: int = Field(default=1, ge=0)  # 最多重寫次數（預設 1，唯一回環的上限）
    max_clarifications: int = Field(default=1, ge=0)  # 最多追問次數（預設 1）


AGENT_LIMITS = AgentLimits()  # 全域預設實例，供 workflow 預設注入
