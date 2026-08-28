from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Protocol, Sequence

from .schemas import AssistantTurn, CandidateEvidence, ToolCall
from .tools.base import ToolRegistry


SYSTEM_PROMPT = """You are an experimental TFDA diabetes medication information agent.
You may use only the registered read-only tools. Tool outputs are candidate evidence,
not approved evidence. Never prescribe, diagnose, or tell a user to start/stop/change
medication. Cite TFDA evidence IDs in square brackets. A mandatory evidence gate and
output gate will review your draft after you finish.
"""


class AgentModel(Protocol):
    name: str

    def next_turn(self, state: Dict[str, Any]) -> AssistantTurn:
        ...


def _new_call(name: str, arguments: Dict[str, Any]) -> ToolCall:
    return ToolCall(call_id="call_%s" % uuid.uuid4().hex[:12], name=name, arguments=arguments)


def _extract_ingredient(query: str) -> str:
    patterns = [
        r"SGLT-?2",
        r"DPP-?4",
        r"GLP-?1",
        r"Canagliflozin",
        r"Dapagliflozin",
        r"Saxagliptin",
        r"Alogliptin",
        r"Repaglinide",
        r"Pioglitazone",
        r"胰島素",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return "糖尿病"


class RuleBasedTFDAModel:
    """Offline deterministic model used to exercise the real orchestration.

    It deliberately behaves like a tool-calling model: first issue independent
    retrieval calls, then inspect the selected evidence, and only then draft.
    """

    name = "rule-based-tfda-tool-model"

    def next_turn(self, state: Dict[str, Any]) -> AssistantTurn:
        query = state["original_query"]
        results = state.get("tool_results", [])
        current_results = [item for item in results if item.call_id]
        result_names = {item.tool_name for item in current_results}

        if not current_results:
            ingredient = _extract_ingredient(query)
            return AssistantTurn(
                tool_calls=[
                    _new_call("search_tfda_risk_communications", {"query": query, "top_k": 5}),
                    _new_call("lookup_tfda_ingredient_risks", {"ingredient": ingredient, "top_k": 5}),
                ]
            )

        candidates = state.get("candidate_evidence", [])
        if "inspect_tfda_evidence_set" not in result_names and candidates:
            evidence_ids = [item.evidence_id for item in candidates[:8]]
            return AssistantTurn(
                tool_calls=[_new_call("inspect_tfda_evidence_set", {"evidence_ids": evidence_ids})]
            )

        if not candidates:
            return AssistantTurn(
                content="目前在指定的 TFDA 藥品安全資訊資料中找不到足以支持回答的候選證據。"
            )

        bullets = []
        for evidence in candidates[:3]:
            excerpt = self._safe_excerpt(evidence)
            label = evidence.ingredient or "未標示成分"
            date = "，發布日期 %s" % evidence.published_date if evidence.published_date else ""
            bullets.append("- %s%s：%s [%s]" % (label, date, excerpt, evidence.evidence_id))
        content = (
            "依據目前檢索到的 TFDA 藥品安全資訊，可整理出以下一般性風險溝通資料：\n"
            + "\n".join(bullets)
            + "\n這些內容只供一般藥品安全資訊查詢，不是個別診斷、處方或停換藥建議；"
            + "實際用藥請由醫師或藥師依個人情況評估。"
        )
        return AssistantTurn(content=content)

    @staticmethod
    def _safe_excerpt(evidence: CandidateEvidence) -> str:
        content = re.sub(r"\s+", " ", evidence.content).strip()
        for marker in ["訊息緣由：", "藥品安全有關資訊分析及描述："]:
            if marker in content:
                content = content.split(marker, 1)[1]
                break
        sentence = re.split(r"(?<=[。！？])", content, maxsplit=1)[0]
        return sentence[:220].strip()


class LangChainToolCallingModel:
    """Optional adapter for a real LangChain chat model.

    The caller owns provider credentials and model construction.  The adapter
    binds only this experiment's selected tool schemas.
    """

    name = "langchain-tool-calling-model"

    def __init__(self, llm: Any, registry: ToolRegistry, system_prompt: str = SYSTEM_PROMPT):
        self.system_prompt = system_prompt
        self.bound_model = llm.bind_tools(registry.llm_schemas())

    def next_turn(self, state: Dict[str, Any]) -> AssistantTurn:
        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        except ImportError as exc:
            raise RuntimeError("LangChain adapter requires langchain-core") from exc

        messages: List[Any] = [SystemMessage(content=self.system_prompt)]
        for message in state.get("messages", []):
            if message.role == "user":
                messages.append(HumanMessage(content=message.content))
            elif message.role == "assistant":
                messages.append(
                    AIMessage(
                        content=message.content,
                        tool_calls=[
                            {"id": call.call_id, "name": call.name, "args": call.arguments}
                            for call in message.tool_calls
                        ],
                    )
                )
            elif message.role == "tool" and message.tool_result is not None:
                messages.append(
                    ToolMessage(
                        content=json.dumps(
                            message.tool_result.model_dump(mode="json"),
                            ensure_ascii=False,
                        ),
                        tool_call_id=message.tool_result.call_id,
                    )
                )

        response = self.bound_model.invoke(messages)
        calls = []
        for item in getattr(response, "tool_calls", []) or []:
            calls.append(
                ToolCall(
                    call_id=str(item.get("id") or "call_%s" % uuid.uuid4().hex[:12]),
                    name=str(item["name"]),
                    arguments=dict(item.get("args") or {}),
                )
            )
        content = response.content if isinstance(response.content, str) else str(response.content or "")
        return AssistantTurn(content=content, tool_calls=calls)


class ScriptedModel:
    name = "scripted-model"

    def __init__(self, turns: Sequence[AssistantTurn]):
        self.turns = list(turns)
        self.index = 0

    def next_turn(self, state: Dict[str, Any]) -> AssistantTurn:
        if self.index >= len(self.turns):
            return AssistantTurn(content="script exhausted")
        turn = self.turns[self.index]
        self.index += 1
        return turn


class LoopingModel:
    name = "looping-model"

    def next_turn(self, state: Dict[str, Any]) -> AssistantTurn:
        return AssistantTurn(
            tool_calls=[
                _new_call("search_tfda_risk_communications", {"query": state["original_query"], "top_k": 1})
            ]
        )
