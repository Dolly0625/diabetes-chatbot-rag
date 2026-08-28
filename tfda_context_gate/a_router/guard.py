from __future__ import annotations

import re
from enum import Enum
from typing import Any, Protocol

from .errors import RouterDependencyError


# 本模組提供提示注入防護（Prompt Injection Guard）
# 雙軌設計：RuleBased（正則離線可用）vs Qwen3Guard（模型深度檢測），皆 fail-closed（模糊即阻擋/拋錯）
# 管線位置：第 3 步，位於語意萃取之前，先攔截惡意指令

QWEN3GUARD_MODEL_ID = "Qwen/Qwen3Guard-Gen-0.6B"  # Qwen3Guard 模型識別碼


class GuardSafety(str, Enum):
    """防護安全等級：對應 Qwen3Guard 輸出的 Safety 欄位。"""

    SAFE = "Safe"  # 安全
    UNSAFE = "Unsafe"  # 不安全（需阻擋）
    CONTROVERSIAL = "Controversial"  # 爭議性內容


class GuardCategory(str, Enum):
    """防護分類：對應 Qwen3Guard 輸出的 Categories 欄位。"""

    VIOLENT = "Violent"  # 暴力
    NON_VIOLENT_ILLEGAL_ACTS = "Non-violent Illegal Acts"  # 非暴力違法行為
    SEXUAL_CONTENT_OR_ACTS = "Sexual Content or Sexual Acts"  # 性相關內容
    PII = "PII"  # 個人識別資訊
    SUICIDE_SELF_HARM = "Suicide & Self-Harm"  # 自殺／自傷
    UNETHICAL_ACTS = "Unethical Acts"  # 不道德行為
    POLITICALLY_SENSITIVE = "Politically Sensitive Topics"  # 政治敏感
    COPYRIGHT_VIOLATION = "Copyright Violation"  # 版權侵害
    JAILBREAK = "Jailbreak"  # 越獄／提示注入（本專案核心關注）
    NONE = "None"  # 無分類／安全


class PromptInjectionGuardResult:
    """防護檢查結果：是否阻擋、安全等級與命中分類。"""

    def __init__(
        self,
        *,
        blocked: bool,  # 是否阻擋（True 即觸發安全否決）
        safety: GuardSafety,  # 安全等級
        categories: tuple[GuardCategory, ...] = (),  # 命中分類清單
    ) -> None:
        self.blocked = blocked
        self.safety = safety
        self.categories = categories


class PromptInjectionGuard(Protocol):
    """防護器協定：任何實作只需提供 check(raw_input) → GuardResult。"""

    def check(self, raw_input: str) -> PromptInjectionGuardResult:
        """檢查輸入是否含提示注入；輸入：原始文字，輸出：阻擋結果。"""
        ...


class RuleBasedPromptInjectionGuard:
    """基於正則的確定性防護：離線可用，作為 Qwen3Guard 的備援與縱深防禦。
    Deterministic fallback and defense-in-depth check for known jailbreak text."""

    # 正則目的：捕捉中英文常見越獄指令（忽略規則/忘記指示/揭露系統提示/jailbreak 等）
    # 涵蓋：中文「忽略規則」「解除限制」、英文「ignore instructions」「system prompt」「jailbreak」
    # B 方案擴充：離線兜底擋暴力/色情/髒話/違法，自殺相關詞刻意不列入，保留給 policy 轉 U_URGENT_HUMAN

    _pattern = re.compile(
        r"忽略(?:前面|以上|所有)?規則|忘記(?:你的)?指示|解除限制|揭露(?:系統|提示|system prompt)|"
        r"ignore\s+(?:all\s+)?(?:previous|prior|以上)?\s*instructions?|system\s+prompt|"
        r"jailbreak|developer\s+message|"
        r"殺人|砍人|打人|暴力|血腥|恐怖攻擊|"
        r"色情|裸露|做愛|性交|色圖|成人影片|黃片|"
        r"幹你|他媽|去死|白癡|智障|垃圾|"
        r"販毒|吸毒|製毒|賭博|詐騙",
        re.IGNORECASE,
    )

    def check(self, raw_input: str) -> PromptInjectionGuardResult:
        """檢查輸入是否命中越獄正則；輸入：原始文字，輸出：阻擋結果（命中即 UNSAFE+JAILBREAK）。"""
        blocked = bool(self._pattern.search(raw_input))  # 正則掃描是否含注入關鍵字
        categories = (GuardCategory.JAILBREAK,) if blocked else (GuardCategory.NONE,)
        return PromptInjectionGuardResult(
            blocked=blocked,
            safety=GuardSafety.UNSAFE if blocked else GuardSafety.SAFE,
            categories=categories,
        )


def parse_qwen3guard_output(content: str) -> PromptInjectionGuardResult:
    """解析 Qwen3Guard 官方文字格式，模糊時 fail-closed（拋 RouterDependencyError）。
    Parse the official Qwen3Guard text format and fail closed on ambiguity."""

    # 預期格式：Safety: Safe/Unsafe/Controversial 與 Categories: ... 兩行
    # 若缺任一欄位或分類無法識別，視為依賴失效（fail-closed）

    safety_match = re.search(r"Safety:\s*(Safe|Unsafe|Controversial)", content, re.IGNORECASE)  # 萃取 Safety 欄位
    categories_match = re.search(r"Categories:\s*(.+)", content, re.IGNORECASE)  # 萃取 Categories 欄位
    if not safety_match or not categories_match:
        raise RouterDependencyError("Qwen3Guard output missing Safety or Categories")  # 缺欄位即 fail-closed

    safety = GuardSafety(safety_match.group(1).title())  # 正規化大小寫後轉枚舉
    raw_categories = categories_match.group(1).strip()
    categories: list[GuardCategory] = []
    if raw_categories.lower() != "none":  # 非 None 才逐一比對已知分類
        for category in GuardCategory:
            if category is GuardCategory.NONE:
                continue
            if category.value.lower() in raw_categories.lower():
                categories.append(category)
        if not categories:
            raise RouterDependencyError("Qwen3Guard returned an unknown category")  # 未知分類亦 fail-closed

    # B 方案：全擋但保留自殺轉真人
    # 若含 JAILBREAK 直接擋；
    # 若 safety==UNSAFE 且不含 SUICIDE_SELF_HARM 則擋（暴力/色情/違法等）；
    # 若僅 SUICIDE_SELF_HARM（或 CONTROVERSIAL/SAFE）則放行交由 policy 轉 U_URGENT_HUMAN
    if GuardCategory.JAILBREAK in categories:
        blocked = True
    elif safety == GuardSafety.UNSAFE and GuardCategory.SUICIDE_SELF_HARM not in categories:
        blocked = True
    else:
        blocked = False
    return PromptInjectionGuardResult(
        blocked=blocked,
        safety=safety,
        categories=tuple(categories or [GuardCategory.NONE]),
    )


class Qwen3GuardPromptInjectionGuard:
    """Qwen3Guard 本地 Transformers 適配器：呼叫 Qwen/Qwen3Guard-Gen-0.6B 進行深度檢測。
    模型採懶加載（lazy），測試可注入假 tokenizer/model 免下載；僅作防護，不具政策決策權。
    Local Transformers adapter for Qwen/Qwen3Guard-Gen-0.6B.

    Model loading is lazy.  Tests and callers can inject `tokenizer` and `model`
    without downloading weights.  The model is used only as a prompt guard; it
    never receives policy authority or produces the final router status.
    """

    def __init__(
        self,
        model_id: str = QWEN3GUARD_MODEL_ID,  # 模型 ID，預設 Qwen3Guard-Gen-0.6B
        *,
        tokenizer: Any | None = None,  # 可注入假 tokenizer（測試用）
        model: Any | None = None,  # 可注入假 model（測試用）
        max_new_tokens: int = 128,  # 生成最大 token 數
        device_map: str | None = None,  # 裝置映射（如 auto）
    ) -> None:
        self.model_id = model_id
        self._tokenizer = tokenizer
        self._model = model
        self.max_new_tokens = max_new_tokens
        self.device_map = device_map

    def _load(self) -> None:
        """懶加載模型；若已注入則跳過，否則從 HuggingFace 下載。"""
        if self._tokenizer is not None and self._model is not None:
            return  # 已注入測試替身，無需加載
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            model_kwargs = {"torch_dtype": "auto"}
            if self.device_map:
                model_kwargs["device_map"] = self.device_map
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, **model_kwargs)
        except Exception as exc:
            raise RouterDependencyError("unable to load Qwen3Guard model") from exc  # 加載失敗即依賴失效

    def check(self, raw_input: str) -> PromptInjectionGuardResult:
        """執行模型推理並解析輸出；輸入：原始文字，輸出：阻擋結果；異常一律轉 RouterDependencyError（fail-closed）。"""
        self._load()  # 確保模型已就緒
        try:
            messages = [{"role": "user", "content": raw_input}]
            rendered = self._tokenizer.apply_chat_template(messages, tokenize=False)  # 套用對話模板
            model_inputs = self._tokenizer([rendered], return_tensors="pt")
            device = getattr(self._model, "device", None)
            if device is not None:
                model_inputs = model_inputs.to(device)  # 移至模型所在裝置
            generated_ids = self._model.generate(
                **model_inputs,
                max_new_tokens=self.max_new_tokens,
            )
            input_length = model_inputs["input_ids"].shape[-1]
            output_ids = generated_ids[0][input_length:].tolist()  # 僅取新生成部分
            content = self._tokenizer.decode(output_ids, skip_special_tokens=True)
            return parse_qwen3guard_output(content)  # 解析為結構化結果
        except RouterDependencyError:
            raise  # 已是依賴錯誤，直接透傳
        except Exception as exc:
            raise RouterDependencyError("Qwen3Guard inference or parsing failed") from exc  # 其餘異常皆視為依賴失效
