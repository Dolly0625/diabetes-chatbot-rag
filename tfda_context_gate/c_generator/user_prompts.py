"""
tfda_context_gate.c_generator.user_prompts — C 層 user prompt 組裝

【本檔定位】
- 僅含 user prompt 組裝邏輯與 context_block，不含系統提示詞
- 與 system_prompts.py 嚴格分離，避免循環依賴
- 4 動態 hint（矛盾/mg/kg/排他性/標示日期）與 no_evidence_hint 完整保留
"""

from __future__ import annotations


EVIDENCE_PAGE_CONTENT_MAX_CHARS = 300

def smart_truncate(content: str, max_chars: int = EVIDENCE_PAGE_CONTENT_MAX_CHARS) -> str:
    if not isinstance(content, str):
        return content
    if len(content) <= max_chars:
        return content
    head = content[: max_chars + 60]
    cut = max_chars
    for sep in ("。", "！", "？", "；", "\n"):
        idx = head.rfind(sep, 0, max_chars + 60)
        if idx != -1 and idx + 1 >= max_chars - 40 and idx + 1 <= max_chars + 60:
            if idx + 1 > cut:
                cut = idx + 1
    if cut != max_chars:
        return head[:cut]
    last = -1
    for sep in ("。", "！", "？", "；", "\n"):
        idx = head.rfind(sep, 0, max_chars)
        if idx > last:
            last = idx
    if last != -1 and last + 1 >= 80:
        return head[: last + 1]
    return content[:max_chars]

def context_block(case: dict) -> str:
    """將 case['contexts'] 轉為提示詞中的文件區塊（v1/v2 共用）。

    每份文件含 document_id / 發布日期 / 藥品成分 / page_content，以分隔線串接。
    P4: page_content 截斷至 300 字/evidence 以降低 C 輸入 tokens。
    """
    blocks = []
    for item in case["contexts"]:
        raw_content = str(item.get("page_content", "") or "")
        truncated = smart_truncate(raw_content, EVIDENCE_PAGE_CONTENT_MAX_CHARS)
        blocks.append(
            "\n".join(
                [
                    f"document_id: {item['document_id']}",
                    f"發布日期: {item.get('發布日期', '')}",
                    f"藥品成分: {item.get('藥品成分', '')}",
                    f"page_content:\n{truncated}",
                ]
            )
        )
    return "\n\n--- DOCUMENT SEPARATOR ---\n\n".join(blocks)  # 以分隔線串接多份文件


def generator_user_prompt(case: dict, method: str) -> str:
    """組 v1 三方法的 user prompt（含 B 決策、approved IDs、文件區塊）。"""
    approved = ", ".join(case["approved_document_ids"]) or "（沒有 approved evidence ID）"  # 無核准 ID 時給提示文字
    return f"""Case ID: {case['case_id']}
Query:
{case['query']}

B Context Gate decision: {case['b_decision']}
Approved evidence IDs for this interface: {approved}

Context documents:
{context_block(case)}

請完成 {method} Generator 的輸出。"""


def evidence_aware_v2_user_prompt(case: dict) -> str:
    """組 v2 的 user prompt（含動態 query_shape_hint 與 no_evidence_hint）。

    依 query 內容與 approved 狀態，動態附加提示：
    - 無 approved ID → 極短 INSUFFICIENT 提示
    - 含「矛盾」→ 比較兩文件是否矛盾的處理提示
    - 含「mg/kg」→ 精確劑量缺口應為 PARTIAL 的提示
    - 含「只和/只發生/僅與/只有」→ 排他性風險的 ANSWER 提示
    - 含「標示日期」→ 多文件日期整理的 ANSWER 提示
    """
    approved = ", ".join(case["approved_document_ids"]) or "（沒有 approved evidence ID）"  # 無核准 ID 時給提示文字
    no_evidence_hint = ""  # 預設無額外提示
    query_shape_hint = ""  # 預設無形狀提示
    if not case["approved_document_ids"]:  # 無 approved ID → 附加極短 INSUFFICIENT 指示
        no_evidence_hint = """
本題沒有 approved evidence ID：請保持極短，只輸出一個 INSUFFICIENT decision、
一個 unsupported_requests、一個 limitation，supported_claims 請留空；不要延伸討論。
"""
    if "矛盾" in case["query"]:  # 矛盾題 → 附加比較兩文件的處理提示
        query_shape_hint = """
本題是比較兩個文件說法是否矛盾的問題：先列出兩個文件各自直接支持的事實，再做有界結論。
若一邊是「沒有本地通報」、另一邊是「基於外部證據的預防性限制」，結論應是「不必然矛盾，
因為沒有通報不等於沒有風險」；不要把是否矛盾列成 unsupported_request。最多 2 個 claims、
1 個 limitation，保持簡短。
"""
    if "mg/kg" in case["query"]:  # 精確劑量題 → 附加 PARTIAL 提示
        query_shape_hint = """
本題要求精確 mg/kg，但若文件有年齡減量、由醫師評估或兒童使用限制，這些質性規則仍是
supported claim，請使用 PARTIAL；只把精確 mg/kg 列為 unsupported_request，不要整題 INSUFFICIENT。
最多輸出 1 個 supported claim、1 個 unsupported request、1 個 limitation。
"""
    if any(marker in case["query"] for marker in ("只和", "只發生", "僅與", "只有")):  # 排他性題 → 附加 ANSWER 提示
        query_shape_hint = """
本題在檢查風險是否只限於某一成分或劑量。若文件已支持目標成分／劑量有風險，並明確說
其他成分目前尚未發現但資料有限、也可能有相同風險，這已足以回答「不能斷言只有它」；
請使用 ANSWER，直接說明這個有界結論，不要把它列成 unsupported_request 或 PARTIAL。
"""
    if "標示日期" in case["query"]:  # 日期整理題 → 附加 ANSWER 提示
        query_shape_hint = """
本題要求整理多份文件並標示日期；如果每一份文件的日期與要求整理的內容都有 context 支持，
請使用 ANSWER，不要因為沒有跨年份統一數字就改成 PARTIAL。只有真的缺少其中一份要求時才列缺口。
"""
    return f"""Case ID: {case['case_id']}
Query:
{case['query']}

B Context Gate decision: {case['b_decision']}
Approved evidence IDs for this interface: {approved}

Context documents:
{context_block(case)}

請依 Evidence-aware v2 規則處理這個 Query：先拆解各項要求，逐項判斷文件支持程度，
再輸出 ANSWER、PARTIAL 或 INSUFFICIENT，以及對應的 supported_claims、
unsupported_requests 和 limitations。不要輸出人工 Ground Truth 或分析過程。
{query_shape_hint}
{no_evidence_hint}"""


def evaluation_user_prompt(case: dict, method: str, output: dict | str) -> str:
    """組 v1 輔助評估的 user prompt（含 Ground Truth 摘要與 Generator 輸出）。"""
    return f"""Case ID: {case['case_id']}
Generator method: {method}
Query:
{case['query']}

人工 Ground Truth 摘要：
- 預期處理方式：{case['ground_truth']['expected_handling']}
- 文件支持的重點：{'；'.join(case['ground_truth']['supported_facts'])}
- 文件沒有提供的重點：{'；'.join(case['ground_truth']['unavailable_facts']) or '無'}
- 預期 evidence IDs：{', '.join(case['approved_document_ids']) or '無'}

Context documents:
{context_block(case)}

Generator output:
{output if isinstance(output, str) else output}

請依 schema 評估這個輸出。"""


def clinician_draft_user_prompt(case: dict) -> str:
    approved = ", ".join(case["approved_document_ids"]) or "（沒有 approved evidence ID）"
    no_evidence_hint = ""
    if not case["approved_document_ids"]:
        no_evidence_hint = """
本題沒有 approved evidence ID：請輸出 INSUFFICIENT，evidence_summary 與 source_table 留空，conflicts 留空，limitations 說明資料不足，disclaimer 仍需包含待確認聲明。
"""
    intake_block = ""
    intake = case.get("intake") or case.get("previsit_intake") or case.get("intake_data")
    if intake:
        try:
            if isinstance(intake, dict):
                meds = ", ".join(intake.get("known_medications", [])) or "未提供"
                allergies = ", ".join(intake.get("allergies", [])) or "未提供"
                chronic = ", ".join(intake.get("chronic_conditions", [])) or "未提供"
                family = ", ".join(intake.get("family_history", [])) or "未提供"
                onset = intake.get("symptom_onset") or "未提供"
                desc = intake.get("symptom_description") or "未提供"
                severity = intake.get("symptom_severity") or "未提供"
                questions = "；".join(intake.get("questions_for_doctor", [])) or "未提供"
            else:
                meds = ", ".join(getattr(intake, "known_medications", []) or []) or "未提供"
                allergies = ", ".join(getattr(intake, "allergies", []) or []) or "未提供"
                chronic = ", ".join(getattr(intake, "chronic_conditions", []) or []) or "未提供"
                family = ", ".join(getattr(intake, "family_history", []) or []) or "未提供"
                onset = getattr(intake, "symptom_onset", None) or "未提供"
                desc = getattr(intake, "symptom_description", None) or "未提供"
                severity = getattr(intake, "symptom_severity", None) or "未提供"
                qs = getattr(intake, "questions_for_doctor", []) or []
                questions = "；".join(qs) or "未提供"
            intake_block = f"""
已提供的 Intake 事實（僅整理，不可捏造）：
- 已知用藥：{meds}
- 過敏史：{allergies}
- 慢性病史：{chronic}
- 家族史：{family}
- 症狀起始：{onset}
- 症狀描述：{desc}
- 症狀程度：{severity}
- 想問醫師的問題：{questions}
"""
        except Exception:
            intake_block = ""
    return f"""Case ID: {case['case_id']}
Query:
{case['query']}

B Context Gate decision: {case['b_decision']}
Approved evidence IDs for this interface: {approved}
{intake_block}
Context documents:
{context_block(case)}

請依醫護草稿詳細版規則產生 ClinicianEvidenceDraft：
- answer 為格式化文本（非 JSON only），含 4 段：一、基本資料（用藥/過敏/慢性/家族）、二、時間軸（起始/描述/程度）、三、安全訊號與限制（不得宣稱已排除急症）、四、待確認（藥袋提醒與待確認項目），末尾附來源對照表（2 列：evidence_id | source | date | version | score）
- 全文 300-400 字，專業但易懂，詳細但不超過 800 字，含免責聲明（需含「確認」）
- 禁止幻覺診斷與個人化劑量指示，僅整理事實與證據
- source_table 結構化欄位需與文本表格一致，且每列 evidence_id 來自 B-approved，最多 2 列
不要輸出 chain-of-thought。
{no_evidence_hint}"""


def evaluation_v2_user_prompt(case: dict, output: dict | str) -> str:
    """組 v2 輔助評估的 user prompt（含 Ground Truth 摘要與 v2 輸出）。"""
    return f"""Case ID: {case['case_id']}
Case type: {case['case_type']}
Query:
{case['query']}

人工 Ground Truth 摘要：
- 預期處理方式：{case['ground_truth']['expected_handling']}
- 文件支持的重點：{'；'.join(case['ground_truth']['supported_facts'])}
- 文件沒有提供的重點：{'；'.join(case['ground_truth']['unavailable_facts']) or '無'}
- 預期 evidence IDs：{', '.join(case['approved_document_ids']) or '無'}

Context documents:
{context_block(case)}

Evidence-aware v2 output:
{output if isinstance(output, str) else output}

請依輔助評估 schema 評估，不要輸出分析過程。"""
