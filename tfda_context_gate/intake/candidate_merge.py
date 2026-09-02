"""candidate_merge — P2A 自然語意候選合併與 fast-path 白名單.

原則：
- AI 只能提候選，不能直接寫 ProductSession；寫入仍由 orchestrator/PendingAction/validation 決定
- deterministic 可補充或安全降級，但不可因部分命中就阻止 AI 看全句
- 紅旗/授權/角色/產品命令仍由 deterministic 優先（在 orchestrator 前段已處理）
- 提供單一 merge/validate 流程：provenance/confidence/dedup/問句·否定·他人防污染

本模組不觸及 B/D/Share/EMR，僅處理 intake 候選的合併與驗證。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# ── Fast-path 白名單與多子句偵測 ─────────────────────────────────

# 可不呼叫 AI 的高精度封閉值：顯式產品命令已在 orchestrator 1160 前段處理，
# 此處白名單僅涵蓋「pending 單值回答」。
_SEVERITY_EXPLICIT_RE = re.compile(r"(輕度|中度|重度|\d+\s*分|\d+\s*/\s*\d+|\b(10|[1-9])\b)", re.IGNORECASE)
_SEVERITY_PURE_RE = re.compile(r"^\s*(大概|大約|約|差不多)?\s*(10|[1-9]|\d+\s*分|\d+\s*/\s*\d+|輕度|中度|重度)\s*(分|左右|吧)?\s*[。！!？?]*\s*$")

# allergies / meds 封閉否定（pending 對應欄位時才可 fast-path）
_ALLERGY_NEG_RE = re.compile(r"^\s*(沒有|無|沒有過敏|無過敏|不過敏|沒有[藥物]?過敏|目前沒有過敏)\s*[。！!？?]*\s*$")
_MEDS_NEG_RE = re.compile(r"^\s*(沒有|無|沒有用藥|沒有在吃藥|沒有吃藥|沒吃藥|沒有固定.*藥|沒有服用.*藥|目前沒有用藥|目前沒有吃藥)\s*[。！!？?]*\s*$")
_CHRONIC_NEG_RE = re.compile(r"^\s*(沒有|無|沒有慢性病|無慢性病)\s*[。！!？?]*\s*$")
_FAMILY_NEG_RE = re.compile(r"^\s*(沒有|無|沒有家族史|無家族史|家族沒有)\s*[。！!？?]*\s*$")
_UNCERTAIN_BARE_RE = re.compile(r"^\s*(不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚|不太知道)\s*[啊欸呢啦哦喔嗎]?[？?。!！]*\s*$")
# Fast-path positive closed vocab for intake (no LLM needed)
_ALLERGY_POS_RE = re.compile(r"^\s*(花生|花生過敏|海鮮|蝦|蟹|藥物過敏|無)\s*[。！!？?]*\s*$")
_CHRONIC_POS_RE = re.compile(r"^\s*(三高|高血壓|高血脂|高膽固醇|糖尿病|高血壓、高血脂|高血壓、高血脂、高膽固醇)\s*[。！!？?]*\s*$")

# 多子句標記（需進 AI，避免只抓半句）
_CLAUSE_MARKERS = ("而且", "另外", "還有", "但是", "順便", "又", "也", "加上", "以及", "然後", "再來", "同時", "不過", "可是")
_EDU_LIKE_RE = re.compile(r"(可以吃|飲食|水果|血糖|副作用|會傷腎|能吃|芭樂)")

# 問句指紋（用於防污染：問句不得寫入本人病史）
_QUESTION_RE = re.compile(r"[？?]|嗎\s*[。？?]?$|會是.*嗎|是不是|是否會")
_QUESTION_INTAKE_RE = re.compile(r"(會傷腎嗎|副作用是|可以吃多少|是不是糖尿病|會是糖尿病嗎)")

# 他人/假設/否定 指紋
_THIRD_PARTY_RE = re.compile(r"(我朋友|我同事|我媽媽|我媽|我爸爸|我爸|家人|我先生|我太太|代問)")
_HYPOTHETICAL_RE = re.compile(r"(如果|假設|以後|萬一|要是)")
_NEGATION_QUESTION_RE = re.compile(r"(沒有頭暈|不是.*頭暈|只是想問|只是好奇)")

# 時間/頻率/口語症狀（若出現且句長較長，應進 AI）
_TIME_FREQ_RE = re.compile(r"(上週|上周|這幾天|最近|晚上|半夜|一直|好幾次|常常|經常|每天|每晚)")
_SYMPTOM_COLLOQUIAL_RE = re.compile(r"(嘴巴乾|口乾|口渴|很渴|跑廁所|上廁所|夜尿|頻尿|尿多|頭暈|麻|視線模糊|很累|疲倦)")

# 用於 provenance 正規化對照（允許的 normalization）— 白名單僅此，不得 AI 捏造
# 規格口語對照必須明確覆蓋：嘴巴很乾→口乾、跑廁所→頻尿、喝水還是渴→口渴
_NORMALIZATION_MAP: dict[str, list[str]] = {
    "口乾": ["嘴巴很乾", "嘴巴乾", "口很乾", "很口渴", "嘴巴乾燥", "口乾舌燥"],
    "頻尿": ["跑廁所", "一直跑廁所", "晚上一直跑廁所", "半夜起來尿", "尿好幾次", "爬起來上廁所", "常跑廁所", "晚上常跑廁所"],
    "口渴": ["喝水還是渴", "一直喝水還是渴", "喝水還是很渴", "喝很多水還是渴", "很口渴", "一直很渴"],
}

# ── P2A.1-A 欄位限定 canonicalization（封閉高信心對照，不可跨欄混用） ──────────
# 僅處理 intake 高頻封閉概念：chronic_conditions 與 known_medications 各自獨立字典。
# 未知詞一律保持原值，不翻譯；canonical value 與 source_quote 分開保存於 MergedCandidate。

def _normalize_canonical_key(text: str) -> str:
    """封閉對照用 key：NFKC + 去空白/連接符 + 小寫."""
    try:
        t = unicodedata.normalize("NFKC", text or "").strip().lower()
    except Exception:
        t = (text or "").strip().lower()
    t = re.sub(r"[\s\-_·•]+", "", t)
    # 去除常見標點干擾（保留中英文核心）
    t = re.sub(r"[，。,。；;、！!？?·]", "", t)
    return t


_CHRONIC_VARIANTS: dict[str, list[str]] = {
    "高血壓": ["高血壓", "hypertension", "htn", "high blood pressure", "high-blood-pressure"],
    "糖尿病": ["糖尿病", "diabetes", "diabetes mellitus", "dm"],
    "高血脂": ["高血脂", "高脂血症", "hyperlipidemia", "hyperlipidaemia"],
    "高膽固醇": ["高膽固醇", "高胆固醇", "hypercholesterolemia", "hypercholesterolaemia", "high cholesterol", "高膽固醇血症"],
}

_SANHIGH_EXPANSION: list[str] = ["高血壓", "高血脂", "高膽固醇"]
_SANHIGH_KEYS: set[str] = {_normalize_canonical_key("三高"), _normalize_canonical_key("三高症")}

_ALLERGY_VARIANTS: dict[str, list[str]] = {
    "花生": ["花生", "peanut", "peanuts", "花生過敏", "peanut allergy", "花生过敏"],
}

_MED_VARIANTS: dict[str, list[str]] = {
    "metformin": ["metformin", "二甲雙胍", "二甲双胍", "metformin hcl", "metformin hydrochloride"],
    "insulin": ["insulin", "胰島素"],
}


def _build_canonical_map(variants: dict[str, list[str]]) -> dict[str, str]:
    m: dict[str, str] = {}
    for canon, var_list in variants.items():
        for v in var_list:
            k = _normalize_canonical_key(v)
            if k:
                m[k] = canon
    return m


_CHRONIC_CANONICAL_MAP: dict[str, str] = _build_canonical_map(_CHRONIC_VARIANTS)
_MED_CANONICAL_MAP: dict[str, str] = _build_canonical_map(_MED_VARIANTS)
_ALLERGY_CANONICAL_MAP: dict[str, str] = _build_canonical_map(_ALLERGY_VARIANTS)
_COLLOQUIAL_MED_RE = re.compile(r"白色.*藥丸|小藥丸|藥丸|膠囊|紅色.*藥|黃色.*藥|藍色.*藥|圓形.*藥|長條.*藥|大顆.*藥|小顆.*藥")
_UNCERTAIN_PHRASE_RE = re.compile(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚|不太知道")

_CHRONIC_NEGATION_RE = re.compile(r"(沒有|無|否認|未有|不曾|沒得|未曾|沒有得|沒有患|未患|不是.*高血壓|沒有.*高血壓)")
_CHRONIC_QUESTION_RE = re.compile(r"(是什麼|是甚麼|是什麼\?|是甚麼\?|什麼是|為何|為甚麼|怎麼|如何)")


def _canonicalize_value(field: str, value: str) -> str:
    if not value or not value.strip():
        return value
    v = value.strip()
    if v == "無":
        return v
    key = _normalize_canonical_key(v)
    if key in _SANHIGH_KEYS and field == "chronic_conditions":
        return "三高"
    if field == "chronic_conditions":
        return _CHRONIC_CANONICAL_MAP.get(key, v)
    if field == "allergies":
        low = v.lower()
        if "peanut" in low or "花生" in v:
            return "花生"
        base = v.replace("過敏", "").strip()
        base_key = _normalize_canonical_key(base) if base else key
        if base_key in _ALLERGY_CANONICAL_MAP:
            return _ALLERGY_CANONICAL_MAP[base_key]
        if key in _ALLERGY_CANONICAL_MAP:
            return _ALLERGY_CANONICAL_MAP[key]
        for k, canon in _ALLERGY_CANONICAL_MAP.items():
            if k and k in key:
                if key == k or key == k + _normalize_canonical_key("過敏") or key == _normalize_canonical_key(canon + "過敏"):
                    return canon
        return v
    if field == "known_medications":
        canon = _MED_CANONICAL_MAP.get(key)
        if canon:
            return canon
        return v
    return v


def _expand_sanhigh_candidates(cands: list[MergedCandidate]) -> list[MergedCandidate]:
    out: list[MergedCandidate] = []
    for c in cands:
        if c.target_field == "chronic_conditions" and _normalize_canonical_key(c.value) in _SANHIGH_KEYS:
            for exp in _SANHIGH_EXPANSION:
                out.append(
                    MergedCandidate(
                        target_field=c.target_field,
                        value=exp,
                        confidence=c.confidence,
                        source_quote=c.source_quote,
                        raw=c.raw,
                        source=c.source,
                        explicitly_stated=c.explicitly_stated,
                        requires_confirmation=c.requires_confirmation,
                    )
                )
        else:
            out.append(c)
    return out


def _split_combined_candidates(cands: list[MergedCandidate]) -> list[MergedCandidate]:
    out: list[MergedCandidate] = []
    for c in cands:
        if c.target_field in ("chronic_conditions", "allergies", "known_medications", "family_history"):
            if any(sep in c.value for sep in (",", "，", "、", "；", ";")):
                parts = re.split(r"[，,、；;]+", c.value)
                for p in parts:
                    p = p.strip()
                    if not p:
                        continue
                    out.append(
                        MergedCandidate(
                            target_field=c.target_field,
                            value=p,
                            confidence=c.confidence,
                            source_quote=c.source_quote,
                            raw=c.raw,
                            source=c.source,
                            explicitly_stated=c.explicitly_stated,
                            requires_confirmation=c.requires_confirmation,
                        )
                    )
                continue
        out.append(c)
    return out


def _normalize_text(text: str) -> str:
    try:
        return unicodedata.normalize("NFKC", text or "").strip()
    except Exception:
        return (text or "").strip()


def _split_into_clauses(text: str) -> list[str]:
    if not text:
        return []
    n = _normalize_text(text)
    if not n:
        return []
    parts = re.split(r"[，。,；;、.。！!？?]+", n)
    clauses: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        subs = re.split(r"(?:但是|不過|可是|然而|卻|但)", part)
        for sp in subs:
            sp = sp.strip()
            if not sp:
                continue
            if "也" in sp and ("沒有" in sp or "無" in sp or "不" in sp):
                also_parts = [s.strip() for s in sp.split("也") if s.strip()]
                if len(also_parts) > 1:
                    clauses.extend(also_parts)
                    continue
            clauses.append(sp)
    if not clauses and n.strip():
        return [n.strip()]
    return clauses


def _clause_contains_value(clause: str, value: str, field: str = "chronic_conditions") -> bool:
    if not clause or not value:
        return False
    try:
        clause_key = _normalize_canonical_key(clause)
        val_key = _normalize_canonical_key(value)
        if val_key and val_key in clause_key:
            return True
        canon = _canonicalize_value(field, value)
        canon_map = _CHRONIC_CANONICAL_MAP if field == "chronic_conditions" else _ALLERGY_CANONICAL_MAP if field == "allergies" else {}
        for var_key, c in canon_map.items():
            if c == canon and var_key in clause_key:
                return True
        canon_key = _normalize_canonical_key(canon) if canon else ""
        if canon_key and canon_key in clause_key:
            return True
        low_clause = clause.lower()
        low_val = value.strip().lower()
        if low_val and low_val in low_clause:
            return True
        if canon and canon in clause:
            return True
    except Exception:
        return False
    return False


def _clause_is_negated(clause: str) -> bool:
    n = _normalize_text(clause)
    if not n:
        return False
    if _CHRONIC_NEGATION_RE.search(n):
        return True
    if re.search(r"沒有|無|否認|未有|不曾|沒得|未曾|沒有得|沒有患|未患|不是", n):
        return True
    return False


def _clause_is_question(clause: str) -> bool:
    n = _normalize_text(clause)
    if not n:
        return False
    if is_question_like(n):
        return True
    if _CHRONIC_QUESTION_RE.search(n):
        if re.search(r"hypertension|htn|diabetes|hyperlipidemia|高血壓|糖尿病|高血脂", n.lower()):
            return True
        if "是什麼" in n or "是甚麼" in n or "什麼是" in n:
            return True
    if "是什麼" in n or "是甚麼" in n or "什麼是" in n:
        return True
    return False


def _is_chronic_negated(raw: str, value: str) -> bool:
    if not raw or not value or value.strip() == "無":
        return False
    clauses = _split_into_clauses(raw)
    if not clauses:
        return False
    for clause in clauses:
        if _clause_contains_value(clause, value, "chronic_conditions"):
            if _clause_is_negated(clause):
                return True
    return False


def _is_allergy_negated(raw: str, value: str) -> bool:
    if not raw or not value or value.strip() == "無":
        return False
    clauses = _split_into_clauses(raw)
    if not clauses:
        return False
    for clause in clauses:
        if _clause_contains_value(clause, value, "allergies"):
            if _clause_is_negated(clause):
                return True
    return False


def _is_chronic_question(raw: str, value: str | None = None) -> bool:
    if not raw:
        return False
    n = _normalize_text(raw)
    if value is not None and value.strip():
        clauses = _split_into_clauses(raw)
        for clause in clauses:
            if _clause_contains_value(clause, value, "chronic_conditions"):
                if _clause_is_question(clause):
                    return True
        return False
    if is_question_like(n):
        if "是什麼" in n or "是甚麼" in n or "什麼是" in n:
            return True
        if re.search(r"hypertension|htn|diabetes|hyperlipidemia", n.lower()) and ("？" in n or "?" in n or "嗎" in n):
            return True
        if re.search(r"hypertension|diabetes", n.lower()) and is_question_like(n):
            return True
    if _CHRONIC_QUESTION_RE.search(n) and re.search(r"hypertension|htn|diabetes|高血壓|糖尿病|高血脂", n.lower()):
        return True
    return False


def is_multi_clause(text: str) -> bool:
    """判斷是否為多子句/複合語意（需進 AI，不得 fast-path）。"""
    n = _normalize_text(text)
    if not n:
        return False
    if any(m in n for m in _CLAUSE_MARKERS):
        return True
    # 標點分隔子句
    parts = [p.strip() for p in re.split(r"[，,。；;、]", n) if p.strip()]
    if len(parts) >= 2:
        return True
    # 長句含時間+症狀
    if len(n) >= 14 and _TIME_FREQ_RE.search(n) and _SYMPTOM_COLLOQUIAL_RE.search(n):
        return True
    return False


def is_question_like(text: str) -> bool:
    n = _normalize_text(text)
    return bool(_QUESTION_RE.search(n) or _QUESTION_INTAKE_RE.search(n))


def is_third_party(text: str) -> bool:
    return bool(_THIRD_PARTY_RE.search(text))


def is_hypothetical(text: str) -> bool:
    return bool(_HYPOTHETICAL_RE.search(text))


def is_negation_or_curiosity(text: str) -> bool:
    return bool(_NEGATION_QUESTION_RE.search(text))


def is_fast_path_eligible(text: str, pending_field: str | None) -> bool:
    """判斷是否可走窄 fast-path（不呼叫 AI）。

    僅限：
    - 嚴重程度純數字/單詞（6 分 / 輕度 等）且 pending 為 symptom_severity
    - allergies / meds / chronic / family 的封閉否定
    - 單純不知道（bare uncertain）
    其它一律不 eligible（需進 AI 再合併）。
    """
    n = _normalize_text(text)
    if not n or not pending_field:
        return False
    # 多子句 / 同時含衛教問 / 含時間頻率口語症狀 → 不得 fast-path
    if is_multi_clause(n):
        return False
    if _EDU_LIKE_RE.search(n) and ("？" in n or "嗎" in n):
        return False
    if len(n) >= 14 and _TIME_FREQ_RE.search(n) and _SYMPTOM_COLLOQUIAL_RE.search(n):
        return False
    if is_question_like(n) and pending_field in ("known_medications", "symptom_description", "symptom_onset", "symptom_severity"):
        return False
    if is_third_party(n) or is_hypothetical(n) or is_negation_or_curiosity(n):
        return False

    if pending_field == "symptom_severity" and _SEVERITY_PURE_RE.match(n):
        return True
    if pending_field == "allergies" and (_ALLERGY_NEG_RE.match(n) or _ALLERGY_POS_RE.match(n)):
        return True
    if pending_field == "known_medications" and _MEDS_NEG_RE.match(n):
        return True
    if pending_field == "chronic_conditions" and (_CHRONIC_NEG_RE.match(n) or _CHRONIC_POS_RE.match(n)):
        return True
    if pending_field == "family_history" and _FAMILY_NEG_RE.match(n):
        return True
    if pending_field == "allergies" and _normalize_canonical_key(n) in _ALLERGY_CANONICAL_MAP:
        return True
    if pending_field == "chronic_conditions" and (_normalize_canonical_key(n) in _CHRONIC_CANONICAL_MAP or _normalize_canonical_key(n) in _SANHIGH_KEYS):
        return True
    if _UNCERTAIN_BARE_RE.match(n):
        if pending_field in ("symptom_onset", "symptom_description", "symptom_severity", "known_medications", "allergies", "chronic_conditions", "family_history"):
            return True
    return False


# ── Candidate 統一模型 ─────────────────────────────────────────────

@dataclass(frozen=True)
class MergedCandidate:
    """統一後的候選，供 validation/dedup/寫入判斷."""

    target_field: str
    value: str  # 單值；多症狀已在 deterministic 端以 "；" join，formal 端亦同
    confidence: float
    source_quote: str
    raw: str  # 原句
    source: str  # "deterministic" | "formal"
    explicitly_stated: bool = True
    requires_confirmation: bool = False


def _check_provenance(source_quote: str, raw: str) -> bool:
    """source_quote 必須能在 raw 中找到，或在 normalization 對照中."""
    if not source_quote or not raw:
        return False
    sq = _normalize_text(source_quote)
    r = _normalize_text(raw)
    if sq and sq in r:
        return True
    # 檢查 normalization 對照：如 sq=口乾，raw 含 嘴巴很乾 視為合法
    for norm, variants in _NORMALIZATION_MAP.items():
        if sq == norm and any(v in r for v in variants):
            return True
        if sq in variants and norm in r:
            return True
    # 允許大小寫/空白差異：去空白後包含
    sq_ns = re.sub(r"\s+", "", sq)
    r_ns = re.sub(r"\s+", "", r)
    if sq_ns and sq_ns in r_ns:
        return True
    return False


def _is_polluted(text: str, field: str) -> bool:
    """問句/否定/假設/他人資料不得寫入本人資料 — 嚴格白名單，不得 AI 捏造."""
    n = _normalize_text(text)
    if field == "known_medications" and is_question_like(n) and ("？" in n or "嗎" in n):
        if not re.search(r"我(有|正在)?吃|醫生有開.*給我", n):
            if _QUESTION_INTAKE_RE.search(n) or "會傷腎" in n or "副作用" in n:
                return True
        if _QUESTION_INTAKE_RE.search(n) or "會傷腎嗎" in n:
            if not re.search(r"我(有|正在)?吃", n):
                return True
    if field in ("symptom_description", "symptom_onset", "symptom_severity"):
        # 任何問句形式「會是糖尿病嗎？」不得當本人症狀；即使含「我最近」亦需高信心+確認，故此處一律攔截
        if is_question_like(n) and ("會是糖尿病嗎" in n or "是不是" in n or "糖尿病嗎" in n):
            return True
        if is_question_like(n) and _QUESTION_INTAKE_RE.search(n) and field == "symptom_description":
            if not re.search(r"我(有|出現|最近).*(口渴|頻尿|口乾|很渴|跑廁所)", n):
                return True
        if is_hypothetical(n) and field == "symptom_description":
            return True
        if is_negation_or_curiosity(n):
            return True
        if is_question_like(n) and is_hypothetical(n):
            return True
    if is_third_party(n):
        if field in ("known_medications", "allergies", "symptom_description", "symptom_onset", "symptom_severity", "chronic_conditions", "family_history"):
            return True
    if is_hypothetical(n) and field in ("known_medications", "allergies", "symptom_description"):
        return True
    if field == "chronic_conditions":
        clauses = _split_into_clauses(text)
        for cl in clauses:
            if _clause_is_question(cl) and re.search(r"高血壓|糖尿病|高血脂|hypertension|htn|diabetes|hyperlipidemia", cl.lower()):
                return True
            if _CHRONIC_QUESTION_RE.search(cl) and re.search(r"高血壓|糖尿病|高血脂|hypertension|htn|diabetes|hyperlipidemia", cl.lower()):
                return True
        return False
    return False


def _dedup_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = re.sub(r"\s+", "", v.strip().lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(v.strip())
    return out


def _split_symptom_clauses(value: str) -> list[str]:
    return [c.strip() for c in re.split(r"[；;]", value) if c.strip()]


def _merge_symptom_description(existing: str | None, incoming: list[str]) -> str:
    """多症狀完整保留：以 clause 為單位 dedup 再以 ； join."""
    clauses: list[str] = []
    if existing:
        clauses.extend(_split_symptom_clauses(existing))
    for v in incoming:
        clauses.extend(_split_symptom_clauses(v))
    deduped = _dedup_values(clauses)
    return "；".join(deduped)


# ── 對外 API ──────────────────────────────────────────────────────

def deterministic_to_candidates(
    extracted: dict[str, Any],
    raw: str,
    source: str = "deterministic",
) -> list[MergedCandidate]:
    """將 PreVisitIntakeTool.extract_fields_from_utterance 的 dict 轉為 MergedCandidate list."""
    out: list[MergedCandidate] = []
    for field, val in extracted.items():
        if field.startswith("_"):
            continue
        if not val:
            continue
        # list[str] 或 str
        if isinstance(val, list):
            # questions_for_doctor 等 list 欄：每項一個候選
            for item in val:
                s = str(item).strip()
                if not s:
                    continue
                # provenance：取 raw 前 80 字作為 quote（deterministic 無精確 quote）
                quote = raw.strip()[:80] if raw.strip() else s[:80]
                out.append(
                    MergedCandidate(
                        target_field=field,
                        value=s,
                        confidence=0.78,
                        source_quote=quote,
                        raw=raw,
                        source=source,
                        explicitly_stated=True,
                        requires_confirmation=False,
                    )
                )
        else:
            s = str(val).strip()
            if not s:
                continue
            quote = raw.strip()[:80] if raw.strip() else s[:80]
            out.append(
                MergedCandidate(
                    target_field=field,
                    value=s,
                    confidence=0.78,
                    source_quote=quote,
                    raw=raw,
                    source=source,
                    explicitly_stated=True,
                    requires_confirmation=False,
                )
            )
    return out


def formal_to_candidates(
    intake_candidates: list[Any],
    raw: str,
) -> list[MergedCandidate]:
    """將 Formal/Deterministic interpreter 的 IntakeCandidate 轉為 MergedCandidate."""
    out: list[MergedCandidate] = []
    for c in intake_candidates or []:
        try:
            field = getattr(c, "field_name", None) or getattr(c, "target_field", None)
            val = getattr(c, "candidate_value", None) or getattr(c, "value", None)
            quote = getattr(c, "source_quote", "") or raw[:80]
            conf = float(getattr(c, "confidence", 0.5))
            exp = bool(getattr(c, "explicitly_stated", True))
            req = bool(getattr(c, "requires_confirmation", False))
            if not field or not val:
                continue
            out.append(
                MergedCandidate(
                    target_field=str(field),
                    value=str(val).strip(),
                    confidence=conf,
                    source_quote=str(quote).strip()[:500],
                    raw=raw,
                    source="formal",
                    explicitly_stated=exp,
                    requires_confirmation=req,
                )
            )
        except Exception:
            continue
    return out


def validate_candidate(c: MergedCandidate) -> tuple[bool, str | None]:
    if not c.value or not c.value.strip():
        return False, "empty_value"
    if len(c.value) > 2000:
        return False, "too_long"
    if not _check_provenance(c.source_quote, c.raw):
        return False, "provenance_fail"
    if c.target_field == "known_medications":
        if c.value.strip() in ("不清楚（待看診確認）", "待確認", "待看診確認"):
            pass
        else:
            has_known = any(k.lower() in (c.value + " " + c.source_quote + " " + c.raw).lower() for k in ["metformin", "二甲雙胍", "二甲双胍", "胰島素", "insulin", "sglt2", "glp-1", "semaglutide", "阿卡波糖", "格列美脲"])
            if not has_known and (_COLLOQUIAL_MED_RE.search(c.value) or _COLLOQUIAL_MED_RE.search(c.source_quote) or _COLLOQUIAL_MED_RE.search(c.raw)):
                return False, "colloquial_medication_uncertain"
            if _UNCERTAIN_PHRASE_RE.search(c.value) or _UNCERTAIN_PHRASE_RE.search(c.source_quote):
                if c.value.strip() in ("不清楚", "不確定", "不知道", "不記得", "忘了", "忘記") or _UNCERTAIN_PHRASE_RE.search(c.raw):
                    if not has_known:
                        return False, "uncertain_medication"
    if c.target_field == "allergies" and c.value.strip() != "無":
        if _is_allergy_negated(c.raw, c.value) or _is_allergy_negated(c.source_quote, c.value):
            return False, "polluted_negated_allergy"
        for txt in (c.raw, c.source_quote):
            if not txt:
                continue
            clauses = _split_into_clauses(txt)
            for cl in clauses:
                if _clause_contains_value(cl, c.value, "allergies") and is_question_like(cl):
                    return False, "polluted_question_or_third_party"
    if c.target_field == "chronic_conditions":
        # 子句級污染：僅當候選所在子句為問句/他人/假設才攔截，避免整句掃描錯殺另一子句的正向候選
        _polluted_for_value = False
        for txt in (c.raw, c.source_quote, c.value):
            if not txt:
                continue
            clauses = _split_into_clauses(txt)
            for cl in clauses:
                if not _clause_contains_value(cl, c.value, "chronic_conditions"):
                    continue
                if _clause_is_question(cl) or is_third_party(cl) or is_hypothetical(cl):
                    _polluted_for_value = True
                if _CHRONIC_QUESTION_RE.search(cl) and re.search(r"高血壓|糖尿病|高血脂|hypertension|htn|diabetes|hyperlipidemia", cl.lower()):
                    _polluted_for_value = True
        if _polluted_for_value:
            return False, "polluted_question_or_third_party"
        # 家人/他人若整句為 third_party 且候選在該句，仍視為污染（單句情況）
        if is_third_party(c.raw) or is_third_party(c.source_quote):
            for txt in (c.raw, c.source_quote):
                if not txt:
                    continue
                if is_third_party(txt) and _clause_contains_value(txt, c.value, "chronic_conditions"):
                    return False, "polluted_question_or_third_party"
                clauses = _split_into_clauses(txt)
                for cl in clauses:
                    if is_third_party(cl) and _clause_contains_value(cl, c.value, "chronic_conditions"):
                        return False, "polluted_question_or_third_party"
    else:
        if _is_polluted(c.raw, c.target_field) or _is_polluted(c.value, c.target_field) or _is_polluted(c.source_quote, c.target_field):
            if is_question_like(c.raw) or is_third_party(c.raw) or is_hypothetical(c.raw) or is_question_like(c.source_quote):
                return False, "polluted_question_or_third_party"
            return False, "polluted_question_or_third_party"
    if c.target_field in ("known_medications", "symptom_description", "symptom_onset", "symptom_severity") and is_question_like(c.raw):
        if is_question_like(c.source_quote):
            return False, "question_pollution"
        if _QUESTION_INTAKE_RE.search(c.raw) and _QUESTION_INTAKE_RE.search(c.source_quote):
            return False, "question_pollution"
    if c.target_field in ("known_medications", "allergies", "symptom_description") and is_third_party(c.raw):
        return False, "polluted_question_or_third_party"
    if c.target_field == "symptom_description" and is_hypothetical(c.raw):
        return False, "polluted_question_or_third_party"
    if c.target_field == "chronic_conditions" and c.value.strip() != "無":
        if _is_chronic_negated(c.raw, c.value) or _is_chronic_negated(c.source_quote, c.value):
            return False, "polluted_question_or_third_party"
        if _is_chronic_question(c.raw, c.value) or _is_chronic_question(c.source_quote, c.value) or _is_chronic_question(c.value, c.value):
            return False, "polluted_question_or_third_party"
        # 問句以子句為單位：僅當候選所在子句為問句才攔截
        for txt in (c.raw, c.source_quote, c.value):
            if not txt:
                continue
            clauses = _split_into_clauses(txt)
            for cl in clauses:
                if _clause_contains_value(cl, c.value, "chronic_conditions") and is_question_like(cl):
                    if re.search(r"hypertension|htn|diabetes|高血壓|糖尿病|高血脂", cl.lower()):
                        if "？" in cl or "?" in cl or "嗎" in cl or "是什麼" in cl or "是甚麼" in cl:
                            return False, "polluted_question_or_third_party"
    return True, None


def merge_candidates(
    deterministic: list[MergedCandidate],
    formal: list[MergedCandidate],
    existing_intake: Any | None = None,
) -> tuple[list[MergedCandidate], list[dict[str, Any]]]:
    """合併 deterministic 與 formal 候選.

    規則：
    - 先做 provenance/confidence/污染校驗
    - 同欄同值去重
    - 多症狀 (symptom_description) 以 clause 為單位保留
    - 衝突時：高信心且有原文依據才進入寫入，否則標為需 clarification

    回 (valid_candidates, clarification_needed)
    clarification_needed 為 [{field, reason, raw}]
    """
    all_cands = list(deterministic) + list(formal)
    canonicalized: list[MergedCandidate] = []
    for c in all_cands:
        if c.target_field in ("chronic_conditions", "allergies"):
            try:
                canon_val = _canonicalize_value(c.target_field, c.value)
            except Exception:
                canon_val = c.value
            if canon_val != c.value:
                canonicalized.append(
                    MergedCandidate(
                        target_field=c.target_field,
                        value=canon_val,
                        confidence=c.confidence,
                        source_quote=c.source_quote,
                        raw=c.raw,
                        source=c.source,
                        explicitly_stated=c.explicitly_stated,
                        requires_confirmation=c.requires_confirmation,
                    )
                )
            else:
                canonicalized.append(c)
        else:
            canonicalized.append(c)
    all_cands = _split_combined_candidates(_expand_sanhigh_candidates(canonicalized))
    all_cands.sort(key=lambda c: c.confidence, reverse=True)

    seen: set[tuple[str, str]] = set()
    deduped: list[MergedCandidate] = []
    for c in all_cands:
        try:
            canon_for_key = _canonicalize_value(c.target_field, c.value)
        except Exception:
            canon_for_key = c.value
        key = (c.target_field, re.sub(r"\s+", "", canon_for_key.lower()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    valid: list[MergedCandidate] = []
    need_clarify: list[dict[str, Any]] = []

    # 已有 intake 快照，用於判斷覆蓋是否安全
    existing_map: dict[str, Any] = {}
    if existing_intake is not None:
        try:
            for f in ("known_medications", "allergies", "chronic_conditions", "family_history", "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor"):
                existing_map[f] = getattr(existing_intake, f, None)
        except Exception:
            pass

    for c in deduped:
        ok, reason = validate_candidate(c)
        if not ok:
            if reason in ("polluted_question_or_third_party", "question_pollution", "provenance_fail"):
                need_clarify.append({"field": c.target_field, "reason": reason, "raw": c.raw, "value": c.value})
            continue
        if c.source == "formal" and c.confidence < 0.6 and not c.explicitly_stated:
            need_clarify.append({"field": c.target_field, "reason": "low_confidence", "raw": c.raw, "value": c.value})
            continue
        if c.target_field == "symptom_description":
            distinct_sym = ("口渴", "頻尿", "口乾", "很渴", "跑廁所", "頭暈", "疼痛", "麻", "視力", "餓", "手抖", "疲倦", "夜尿", "喘", "血糖高", "血糖低", "發抖")
            chronic_only = ("高血壓", "高血脂", "高脂血", "腎臟病", "心臟病")
            # 同句同時命中 chronic 與 symptom 時，若 symptom 僅含慢性詞且無 distinct 症狀詞，視為誤判
            has_distinct = any(kw in c.value for kw in distinct_sym)
            has_chronic = any(kw in c.value for kw in chronic_only)
            if not has_distinct and has_chronic:
                chronic_vals = [x.value for x in deduped if x.target_field == "chronic_conditions"]
                # 若同句亦有 chronic 候選，或 symptom 值本質上是慢性描述，過濾
                if chronic_vals or has_chronic:
                    need_clarify.append({"field": c.target_field, "reason": "chronic_symptom_confusion", "raw": c.raw, "value": c.value})
                    continue
        existing_val = existing_map.get(c.target_field)
        if existing_val and c.target_field in ("symptom_description", "symptom_onset", "symptom_severity"):
            if c.confidence < 0.75:
                need_clarify.append({"field": c.target_field, "reason": "conflict_low_confidence", "raw": c.raw, "value": c.value})
                continue
        valid.append(c)

    # 多症狀特殊：symptom_description 多候選需以 clause 合併為單一 valid（保持 ； join 約定）
    sym_cands = [c for c in valid if c.target_field == "symptom_description"]
    if len(sym_cands) > 1:
        # 合併為單一候選，取最高信心與首個 raw/quote
        best = max(sym_cands, key=lambda c: c.confidence)
        merged_value = _merge_symptom_description(existing_map.get("symptom_description"), [c.value for c in sym_cands])
        # 替換
        valid = [c for c in valid if c.target_field != "symptom_description"]
        valid.append(
            MergedCandidate(
                target_field="symptom_description",
                value=merged_value,
                confidence=best.confidence,
                source_quote=best.source_quote,
                raw=best.raw,
                source=best.source,
                explicitly_stated=best.explicitly_stated,
                requires_confirmation=best.requires_confirmation,
            )
        )

    return valid, need_clarify


def candidates_to_intake_updates(
    valid: list[MergedCandidate],
    existing_intake: Any | None = None,
) -> dict[str, Any]:
    """將 valid MergedCandidate 轉為可寫入 intake_snapshot 的 updates dict."""
    updates: dict[str, Any] = {}
    by_field: dict[str, list[MergedCandidate]] = {}
    for c in valid:
        by_field.setdefault(c.target_field, []).append(c)

    for field, clist in by_field.items():
        if field in ("known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"):
            existing_list: list[str] = []
            if existing_intake is not None:
                try:
                    existing_list = list(getattr(existing_intake, field) or [])
                except Exception:
                    existing_list = []
            vals: list[str] = []
            for c in clist:
                raw_val = c.value.strip()
                if any(sep in raw_val for sep in ("；", ";", "、", ",", "，")):
                    parts = re.split(r"[；;、,，]", raw_val)
                    for p in parts:
                        p = p.strip()
                        if not p:
                            continue
                        if field == "chronic_conditions":
                            try:
                                vals.append(_canonicalize_value(field, p))
                            except Exception:
                                vals.append(p)
                        else:
                            vals.append(p)
                else:
                    if field == "chronic_conditions":
                        try:
                            vals.append(_canonicalize_value(field, raw_val))
                        except Exception:
                            vals.append(raw_val)
                    else:
                        vals.append(raw_val)
            merged = list(existing_list)
            try:
                seen_lower = {re.sub(r"\s+", "", _canonicalize_value(field, x).lower()) for x in merged}
            except Exception:
                seen_lower = {re.sub(r"\s+", "", x.lower()) for x in merged}
            for v in vals:
                try:
                    key = re.sub(r"\s+", "", _canonicalize_value(field, v).lower())
                except Exception:
                    key = re.sub(r"\s+", "", v.lower())
                if key not in seen_lower and len(merged) < 10:
                    if field == "chronic_conditions":
                        try:
                            v = _canonicalize_value(field, v)
                        except Exception:
                            pass
                    merged.append(v)
                    seen_lower.add(key)
            updates[field] = merged
        elif field in ("symptom_description",):
            best = max(clist, key=lambda c: c.confidence)
            updates[field] = best.value
        else:
            best = max(clist, key=lambda c: c.confidence)
            updates[field] = best.value
    return updates

