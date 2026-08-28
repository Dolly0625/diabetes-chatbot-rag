from __future__ import annotations

from typing import Any

from tfda_context_gate.a_router.schemas import AResult

from .schemas import QueryExpansionInput


def from_a_result(a_result: AResult | Any) -> QueryExpansionInput:
    """將 A 層路由結果轉為查詢擴展輸入，不更動 A 的結果契約。

    轉接邏輯：
      - 若傳入非 AResult 型別，先透過 AResult.model_validate 驗證轉換
      - 逐欄位映射：request_id、user_raw_input→original_query、router_status、
        intent_tags、declared_role、language
      - Enum 欄位取 .value 轉為字串，確保下游僅處理純字串

    參數:
        a_result: A 層路由結果（AResult 實例或可驗證的 dict）
    回傳:
        對應的 QueryExpansionInput，可直接送入 QueryExpander.expand()
    """

    if not isinstance(a_result, AResult):
        a_result = AResult.model_validate(a_result)  # 非 AResult 則先驗證轉換
    return QueryExpansionInput(
        request_id=a_result.request_id,
        original_query=a_result.user_raw_input,  # A 層的原始輸入即為查詢擴展的原始查詢
        router_status=a_result.router_status.value,  # Enum → 字串
        intent_tags=[tag.value for tag in a_result.intent_tags],  # Enum 列表 → 字串列表
        declared_role=a_result.declared_role.value,
        language=a_result.language.value,
    )
