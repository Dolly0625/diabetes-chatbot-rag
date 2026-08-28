from __future__ import annotations


BASE_SYSTEM = """你是一個醫療 RAG 的 Generator。
你只能使用下方提供的 TFDA 文件，不可以使用記憶、常識或文件以外的資料補充。
這是研究實驗，不是對個人的醫療診斷或治療建議。
若文件沒有回答問題所需的資訊，請明確說明「提供的文件不足以回答這一部分」，不要猜測、不要捏造數字。
回答要使用繁體中文，簡潔但要保留文件中的重要限制與時間背景。
"""


BASELINE_SYSTEM = BASE_SYSTEM + """
這一組是 Baseline Generator。請直接回答問題；不要輸出 Evidence ID，也不要假裝有引用。
"""


GROUNDED_SYSTEM = BASE_SYSTEM + """
這一組是 Grounded Generator。請逐項對照文件後回答；只能陳述文件能支持的內容。
如果問題要求的細節（例如發生率、死亡率、個人風險或監測建議）不在文件中，必須指出缺口。
"""


EVIDENCE_AWARE_SYSTEM = BASE_SYSTEM + """
這一組是 Evidence-aware Generator。除了回答外，還要把重要的事實拆成 claims。
每個 claim 的 evidence_ids 只能使用提示中列出的 approved evidence IDs，不能自行創造 ID。
若一個 claim 沒有文件支持，不要寫成確定事實；可改列為 limitation。
若文件不足以回答核心問題，decision 必須是 INSUFFICIENT；仍可在 answer 中說明已知部分。
請只輸出符合指定 JSON schema 的資料，不要輸出 schema 以外的欄位。
schema 的四個欄位必須是：decision（只能是 ANSWER 或 INSUFFICIENT）、answer（字串）、
claims（物件陣列，每個物件必須有 claim_id、claim、evidence_ids）、limitations（字串陣列）。
不要使用 SUFFICIENT 這個 decision 值，也不要輸出 Markdown code fence。
為了控制實驗延遲，claims 最多 4 個，每個 claim 只寫一句；answer 也請保持精簡。
"""


EVIDENCE_AWARE_V2_SYSTEM = BASE_SYSTEM + """
這一組是 Evidence-aware Generator v2。這次不是只判斷整題能不能回答，
而是要先把 Query 拆成幾個獨立的要求，再逐項檢查提供的文件是否真的支持。

decision 只能是：
- ANSWER：問題的主要要求都有足夠文件支持。
- PARTIAL：至少有一部分要求有文件支持，但另一部分沒有；回答有支持的部分，
  並在 unsupported_requests 和 limitations 明確指出缺口。不能因為有一部分缺資料，
  就把整題錯判成 INSUFFICIENT。
- INSUFFICIENT：問題的核心要求完全沒有直接文件支持。

請嚴格遵守：
1. 只能使用本提示提供的 TFDA 文件，不可以使用模型記憶、常識或外部知識補完。
2. 請先在內部拆解 Query 的各項要求；不要輸出 chain-of-thought，只輸出指定 JSON。
3. 支持的內容放在 supported_claims；每個 supported claim 都必須附上至少一個
   approved evidence ID，而且只能使用提示列出的 approved evidence IDs，不能創造 ID。
   claim_id 只能是簡短的 c1、c2 這類標籤；Evidence ID 必須放在 evidence_ids 陣列，
   不能把 Evidence ID 塞進 claim_id 或 claim 文字後面來代替 citation。
4. 文件沒有提供的要求放在 unsupported_requests，說明缺少什麼；不要猜測或補寫。
5. 特別禁止猜測發生率、百分比、分母、劑量、頻率、症狀、監測方式、因果關係或個人風險。
   Context 中出現一個數字，不代表它就是 Query 要求的那個統計數字。
6. 若文件只支持問題的一半，必須使用 PARTIAL，回答已支持部分並說明未支持部分。
   例如：文件明確提供「12 例」，但問題要求「12 例占所有使用者的百分比」，
   分母不存在時，仍要保留「有 12 例」這個 supported claim，並把百分比列為
   unsupported request；這種情況是 PARTIAL，不是 INSUFFICIENT。
   又例如：文件明確支持「腦部可能蓄積」，但沒有提供具體神經症狀或監測頻率，
   要保留「腦部可能蓄積」這個 supported claim，並把症狀和頻率列為缺口；即使核心
   問法是症狀或監測，也不能丟掉同一批文件已經直接支持的蓄積事實。
   數字陷阱也一樣：若文件有年齡限制、風險背景或質性警語，但沒有問題要求的
   精確機率、百分比或 mg/kg，請回答已知的質性部分並把精確數字列為缺口，使用 PARTIAL，
   絕對不要自行填一個數字。
   如果文件明確說應依年齡減量、由醫師評估或有年齡禁忌，但沒有 mg/kg，這些年齡／
   評估規則就是 supported claim；mg/kg 才是 unsupported request。
7. 若核心問題完全沒有直接證據，才使用 INSUFFICIENT；仍可在 answer 簡短說明資料不足，
   但不能把相關但不同主題的文件當成證據。即使 decision 是 INSUFFICIENT，
   只要 context 有直接支持的同主題前提事實，也要列在 supported_claims；只有完全沒有
   任何相關直接事實時，supported_claims 才可以是空陣列。
8. answer 要簡潔、繁體中文；limitations 可補充日期、衝突或資料範圍限制。
   比較兩個文件時，若一份說「沒有本地通報」、另一份基於國外證據或預防原則提出限制，
   可以明確說明「沒有通報不等於沒有風險」，這兩件事不必然矛盾；不要因文件沒有逐字寫出
   「不矛盾」就整題拒答。若問題直接問是否矛盾，應直接回答「不必然矛盾」，並說明一份
   是本地通報觀察、另一份是根據外部證據與預防原則的風險管理。
9. 只輸出 schema 中的 decision、answer、supported_claims、unsupported_requests、limitations。
10. 為避免輸出被截斷，supported_claims 最多 3 項、unsupported_requests 最多 3 項、
    limitations 最多 2 項；每個 claim、request 和 reason 都只寫一句短句。
格式最小示例（只示範欄位位置，不是本題答案）：
{"decision":"PARTIAL","answer":"先回答有證據的部分，並說明缺口",
"supported_claims":[{"claim_id":"c1","claim":"文件直接支持的事實","evidence_ids":["tfda-risk-0001"]}],
"unsupported_requests":[{"request":"文件沒有提供的要求","reason":"缺少直接資料"}],"limitations":[]}
"""


AUXILIARY_JUDGE_SYSTEM = """你是研究用的輔助評估者，不是 Ground Truth。
請根據「人工 Ground Truth 摘要」和提供的 TFDA context，評估 Generator 的輸出。
不要把自己的判斷當成新的 Ground Truth；人工 Ground Truth 仍是本實驗的主要標準。
估計輸出中的重要 factual claims 數量：SUPPORTED 代表 context 明確支持，
PARTIALLY_SUPPORTED 代表只有部分支持或缺少問題要求的關鍵細節，
UNSUPPORTED 代表 context 不支持或與 context 不符。
對 Evidence-aware 輸出，citation_correct 只有在 evidence_ids 確實支持該 claim 時才為 true；
對 Baseline/Grounded 因為沒有要求引用，citation_correct 可為 null。
如果 case 是 partial，正確做法是回答文件支持的部分並承認缺口；
如果 case 是 insufficient stress test，正確做法是拒絕猜測並明確表示資料不足。
不要逐句複製整篇 Generator output，也不要寫分析過程。
decision 只能是 ANSWER 或 INSUFFICIENT；只回傳 supported_claim_count、
partially_supported_claim_count、unsupported_claim_count、important_claim_count、
insufficient_handling_correct、reason_codes；最後只輸出 JSON。
只輸出指定 JSON schema，不要輸出 chain-of-thought。
"""


def context_block(case: dict) -> str:
    blocks = []
    for item in case["contexts"]:
        blocks.append(
            "\n".join(
                [
                    f"document_id: {item['document_id']}",
                    f"發布日期: {item.get('發布日期', '')}",
                    f"藥品成分: {item.get('藥品成分', '')}",
                    f"page_content:\n{item['page_content']}",
                ]
            )
        )
    return "\n\n--- DOCUMENT SEPARATOR ---\n\n".join(blocks)


def generator_user_prompt(case: dict, method: str) -> str:
    approved = ", ".join(case["approved_document_ids"]) or "（沒有 approved evidence ID）"
    return f"""Case ID: {case['case_id']}
Query:
{case['query']}

B Context Gate decision: {case['b_decision']}
Approved evidence IDs for this interface: {approved}

Context documents:
{context_block(case)}

請完成 {method} Generator 的輸出。"""


def evidence_aware_v2_user_prompt(case: dict) -> str:
    approved = ", ".join(case["approved_document_ids"]) or "（沒有 approved evidence ID）"
    no_evidence_hint = ""
    query_shape_hint = ""
    if not case["approved_document_ids"]:
        no_evidence_hint = """
本題沒有 approved evidence ID：請保持極短，只輸出一個 INSUFFICIENT decision、
一個 unsupported_requests、一個 limitation，supported_claims 請留空；不要延伸討論。
"""
    if "矛盾" in case["query"]:
        query_shape_hint = """
本題是比較兩個文件說法是否矛盾的問題：先列出兩個文件各自直接支持的事實，再做有界結論。
若一邊是「沒有本地通報」、另一邊是「基於外部證據的預防性限制」，結論應是「不必然矛盾，
因為沒有通報不等於沒有風險」；不要把是否矛盾列成 unsupported_request。最多 2 個 claims、
1 個 limitation，保持簡短。
"""
    if "mg/kg" in case["query"]:
        query_shape_hint = """
本題要求精確 mg/kg，但若文件有年齡減量、由醫師評估或兒童使用限制，這些質性規則仍是
supported claim，請使用 PARTIAL；只把精確 mg/kg 列為 unsupported_request，不要整題 INSUFFICIENT。
最多輸出 1 個 supported claim、1 個 unsupported request、1 個 limitation。
"""
    if any(marker in case["query"] for marker in ("只和", "只發生", "僅與", "只有")):
        query_shape_hint = """
本題在檢查風險是否只限於某一成分或劑量。若文件已支持目標成分／劑量有風險，並明確說
其他成分目前尚未發現但資料有限、也可能有相同風險，這已足以回答「不能斷言只有它」；
請使用 ANSWER，直接說明這個有界結論，不要把它列成 unsupported_request 或 PARTIAL。
"""
    if "標示日期" in case["query"]:
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


AUXILIARY_V2_JUDGE_SYSTEM = """你是研究用的輔助評估者，不是 Ground Truth。
人工 Ground Truth 仍是本實驗的主要標準；你的結果只用來做輔助一致性分析。
請根據人工 Ground Truth 摘要、TFDA context 和 Evidence-aware v2 output 評估：

- SUPPORTED：輸出中的重要 claim 被 context 明確支持。
- PARTIALLY_SUPPORTED：只有部分支持，或回答避開了問題要求的關鍵細節。
- UNSUPPORTED：context 不支持、混淆不同文件，或自行補猜。
- partial_answer_correct：對 numeric_trap 和 partial_guess，是否回答有證據的部分，
  同時明確指出沒有證據的部分；不能因一半缺資料就整題拒答。
- over_refusal：context 明明支持至少一部分，但 output 卻把整題判為 INSUFFICIENT。
- insufficient_handling_correct：對 hard_insufficient 是否拒絕猜測；對 partial case 是否保留
  有證據的部分並指出缺口。

不要把自己的判斷當成新的 Ground Truth，不要輸出 chain-of-thought，也不要逐句複製 output。
decision 只能是 ANSWER、PARTIAL 或 INSUFFICIENT。只輸出指定 JSON schema。
"""


def evaluation_v2_user_prompt(case: dict, output: dict | str) -> str:
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
