from __future__ import annotations

import re
from enum import Enum
from typing import Any, Protocol

from .errors import RouterDependencyError


QWEN3GUARD_MODEL_ID = "Qwen/Qwen3Guard-Gen-0.6B"


class GuardSafety(str, Enum):
    SAFE = "Safe"
    UNSAFE = "Unsafe"
    CONTROVERSIAL = "Controversial"


class GuardCategory(str, Enum):
    VIOLENT = "Violent"
    NON_VIOLENT_ILLEGAL_ACTS = "Non-violent Illegal Acts"
    SEXUAL_CONTENT_OR_ACTS = "Sexual Content or Sexual Acts"
    PII = "PII"
    SUICIDE_SELF_HARM = "Suicide & Self-Harm"
    UNETHICAL_ACTS = "Unethical Acts"
    POLITICALLY_SENSITIVE = "Politically Sensitive Topics"
    COPYRIGHT_VIOLATION = "Copyright Violation"
    JAILBREAK = "Jailbreak"
    NONE = "None"


class PromptInjectionGuardResult:
    def __init__(
        self,
        *,
        blocked: bool,
        safety: GuardSafety,
        categories: tuple[GuardCategory, ...] = (),
    ) -> None:
        self.blocked = blocked
        self.safety = safety
        self.categories = categories


class PromptInjectionGuard(Protocol):
    def check(self, raw_input: str) -> PromptInjectionGuardResult:
        ...


class RuleBasedPromptInjectionGuard:
    """Deterministic fallback and defense-in-depth check for known jailbreak text."""

    _pattern = re.compile(
        r"忽略(?:前面|以上|所有)?規則|忘記(?:你的)?指示|解除限制|揭露(?:系統|提示|system prompt)|"
        r"ignore\s+(?:all\s+)?(?:previous|prior|以上)?\s*instructions?|system\s+prompt|"
        r"jailbreak|developer\s+message",
        re.IGNORECASE,
    )

    def check(self, raw_input: str) -> PromptInjectionGuardResult:
        blocked = bool(self._pattern.search(raw_input))
        categories = (GuardCategory.JAILBREAK,) if blocked else (GuardCategory.NONE,)
        return PromptInjectionGuardResult(
            blocked=blocked,
            safety=GuardSafety.UNSAFE if blocked else GuardSafety.SAFE,
            categories=categories,
        )


def parse_qwen3guard_output(content: str) -> PromptInjectionGuardResult:
    """Parse the official Qwen3Guard text format and fail closed on ambiguity."""

    safety_match = re.search(r"Safety:\s*(Safe|Unsafe|Controversial)", content, re.IGNORECASE)
    categories_match = re.search(r"Categories:\s*(.+)", content, re.IGNORECASE)
    if not safety_match or not categories_match:
        raise RouterDependencyError("Qwen3Guard output missing Safety or Categories")

    safety = GuardSafety(safety_match.group(1).title())
    raw_categories = categories_match.group(1).strip()
    categories: list[GuardCategory] = []
    if raw_categories.lower() != "none":
        for category in GuardCategory:
            if category is GuardCategory.NONE:
                continue
            if category.value.lower() in raw_categories.lower():
                categories.append(category)
        if not categories:
            raise RouterDependencyError("Qwen3Guard returned an unknown category")

    blocked = GuardCategory.JAILBREAK in categories
    return PromptInjectionGuardResult(
        blocked=blocked,
        safety=safety,
        categories=tuple(categories or [GuardCategory.NONE]),
    )


class Qwen3GuardPromptInjectionGuard:
    """Local Transformers adapter for Qwen/Qwen3Guard-Gen-0.6B.

    Model loading is lazy.  Tests and callers can inject `tokenizer` and `model`
    without downloading weights.  The model is used only as a prompt guard; it
    never receives policy authority or produces the final router status.
    """

    def __init__(
        self,
        model_id: str = QWEN3GUARD_MODEL_ID,
        *,
        tokenizer: Any | None = None,
        model: Any | None = None,
        max_new_tokens: int = 128,
        device_map: str | None = None,
    ) -> None:
        self.model_id = model_id
        self._tokenizer = tokenizer
        self._model = model
        self.max_new_tokens = max_new_tokens
        self.device_map = device_map

    def _load(self) -> None:
        if self._tokenizer is not None and self._model is not None:
            return
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            model_kwargs = {"torch_dtype": "auto"}
            if self.device_map:
                model_kwargs["device_map"] = self.device_map
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, **model_kwargs)
        except Exception as exc:
            raise RouterDependencyError("unable to load Qwen3Guard model") from exc

    def check(self, raw_input: str) -> PromptInjectionGuardResult:
        self._load()
        try:
            messages = [{"role": "user", "content": raw_input}]
            rendered = self._tokenizer.apply_chat_template(messages, tokenize=False)
            model_inputs = self._tokenizer([rendered], return_tensors="pt")
            device = getattr(self._model, "device", None)
            if device is not None:
                model_inputs = model_inputs.to(device)
            generated_ids = self._model.generate(
                **model_inputs,
                max_new_tokens=self.max_new_tokens,
            )
            input_length = model_inputs["input_ids"].shape[-1]
            output_ids = generated_ids[0][input_length:].tolist()
            content = self._tokenizer.decode(output_ids, skip_special_tokens=True)
            return parse_qwen3guard_output(content)
        except RouterDependencyError:
            raise
        except Exception as exc:
            raise RouterDependencyError("Qwen3Guard inference or parsing failed") from exc
