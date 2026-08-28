from __future__ import annotations

import re
import unicodedata

from .schemas import SystemRiskClassification


_LIMITATION = "僅檢查系統已定義且由使用者明確陳述的文字訊號，不代表已排除急症或其他併發症。"
_NEGATION_PREFIX = re.compile(r"(?:沒有|無|否認|並未|不是|未出現|not|no|without).{0,5}$", re.IGNORECASE)


class RiskSignalPolicy:
    """可由醫療 reviewer 逐條核定的 deterministic 文字訊號表。"""

    SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        # CHEST_PAIN: 保留既有「胸痛|胸悶」並補口語變體「胸口.{0,3}?痛」「胸口.{0,3}?悶」「悶悶」 (F4-R1,P4-FIX4有界; 痛在前避免 greedy 吞掉「但」後肯定); _has_affirmed_match 透過 _NEGATION_PREFIX 互斥排除否定句
        ("CHEST_PAIN", re.compile(r"胸痛|胸悶|胸口.{0,3}?痛|胸口.{0,3}?悶|悶悶|chest pain", re.IGNORECASE)),
        # BREATHING_DIFFICULTY: 保留既有並補口語「走.*喘」「走幾步.*喘」; 限定以「走」為前綴避免單字「喘/喘氣」誤報
        ("BREATHING_DIFFICULTY", re.compile(r"喘不過氣|呼吸困難|呼吸急促|走.*喘|走幾步.*喘|shortness of breath", re.IGNORECASE)),
        ("ALTERED_CONSCIOUSNESS", re.compile(r"意識不清|昏迷|昏厥|叫不醒|unconscious", re.IGNORECASE)),
        ("PERSISTENT_VOMITING", re.compile(r"持續嘔吐|一直吐|反覆嘔吐", re.IGNORECASE)),
        ("MAJOR_BLEEDING", re.compile(r"大量出血", re.IGNORECASE)),
        ("PERSISTENT_HIGH_FEVER", re.compile(r"高燒不退", re.IGNORECASE)),
        ("FOOT_ULCER_OR_WOUND", re.compile(r"(?:足部|腳|腳趾).{0,8}(?:潰瘍|流膿|傷口.{0,6}(?:感染|流膿|惡臭|不癒合))", re.IGNORECASE)),
        ("WOUND_INFECTION", re.compile(r"傷口.{0,8}(?:感染|紅腫|流膿|惡臭)", re.IGNORECASE)),
        ("TISSUE_NECROSIS", re.compile(r"皮膚變黑|組織壞死|壞疽|gangrene|necrosis", re.IGNORECASE)),
        ("SEVERE_HYPOGLYCEMIA", re.compile(r"低血糖.{0,12}(?:抽搐|昏迷|叫不醒|無法進食)|抽搐.{0,8}低血糖", re.IGNORECASE)),
        ("POSSIBLE_DKA", re.compile(r"(?:呼吸.*水果味|水果味.*呼吸|酮酸中毒|深快呼吸).{0,12}|(?:高血糖).{0,12}(?:持續嘔吐|腹痛)", re.IGNORECASE)),
        ("POSSIBLE_SEPSIS", re.compile(r"(?:傷口|感染).{0,12}(?:意識不清|呼吸急促|高燒不退|發冷發抖)", re.IGNORECASE)),
    )

    def classify(self, text: str) -> SystemRiskClassification:
        normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text))).strip()
        signals: list[str] = []
        for signal, pattern in self.SIGNAL_PATTERNS:
            if self._has_affirmed_match(normalized, pattern):
                signals.append(signal)
        signals = list(dict.fromkeys(signals))
        if signals:
            return SystemRiskClassification(
                level="RED_FLAG",
                signals=signals,
                action="URGENT_HUMAN",
                basis="explicit_user_report",
                limitations=_LIMITATION,
            )
        return SystemRiskClassification(
            level="NO_DEFINED_SIGNAL",
            signals=[],
            action="CONTINUE_BOUNDED_WORKFLOW",
            basis="no_defined_signal_detected",
            limitations=_LIMITATION,
        )

    @staticmethod
    def _has_affirmed_match(text: str, pattern: re.Pattern[str]) -> bool:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 14):match.start()]
            # 對比轉折後視為新子句，避免「沒有胸痛，但現在呼吸困難」整句被否定。
            contrast_end = 0
            for marker in ("但", "可是", "不過", "然而", "，", "；", ",", ";"):
                idx = prefix.rfind(marker)
                if idx != -1:
                    contrast_end = max(contrast_end, idx + len(marker))
            prefix = prefix[contrast_end:]
            if not _NEGATION_PREFIX.search(prefix):
                return True
        return False
