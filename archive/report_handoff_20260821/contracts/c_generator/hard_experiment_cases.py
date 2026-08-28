from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HardCaseSpec:
    case_id: str
    case_type: str
    query: str
    context_ids: tuple[str, ...]
    approved_ids: tuple[str, ...]
    b_decision: str
    expected_decision: str
    expected_handling: str
    supported_facts: tuple[str, ...]
    unavailable_facts: tuple[str, ...]
    stress_test: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("context_ids", "approved_ids", "supported_facts", "unavailable_facts"):
            result[key] = list(result[key])
        return result


def build_hard_case_specs() -> list[HardCaseSpec]:
    """Thirty deliberately difficult cases; Ground Truth is never sent to Generator."""
    return [
        # 1. Keyword-near but wrong-topic evidence.
        HardCaseSpec("N1", "near_match", "SGLT2 抑制劑是否會造成酮酸中毒？請只回答這個風險。", ("tfda-risk-0019", "tfda-risk-0035", "tfda-risk-0042", "tfda-risk-0064"), ("tfda-risk-0019",), "PASS", "ANSWER", "只使用酮酸中毒文件，排除同藥不同風險", ("tfda-risk-0019 直接描述酮酸中毒",), ("急性腎損傷、截肢、Fournier 壞疽不是本題證據")),
        HardCaseSpec("N2", "near_match", "Canagliflozin 或 dapagliflozin 的急性腎損傷風險有哪些說明？", ("tfda-risk-0035", "tfda-risk-0019", "tfda-risk-0042", "tfda-risk-0064"), ("tfda-risk-0035",), "PASS", "ANSWER", "只使用急性腎損傷文件", ("tfda-risk-0035 描述急性腎損傷通報與注意事項",), ("其他 SGLT2 文件談不同風險")),
        HardCaseSpec("N3", "near_match", "SGLT2 抑制劑造成 Fournier 氏壞疽的安全警訊是什麼？", ("tfda-risk-0064", "tfda-risk-0019", "tfda-risk-0035", "tfda-risk-0042"), ("tfda-risk-0064",), "PASS", "ANSWER", "只使用 Fournier 氏壞疽文件，不把酮酸中毒或截肢混進來", ("tfda-risk-0064 描述案例、症狀與緊急處置背景",), ("其他 SGLT2 風險不是本題答案")),
        HardCaseSpec("N4", "near_match", "2015 年 EMA 對 Codeine 用於兒童咳嗽與感冒的安全建議是什麼？", ("tfda-risk-0015", "tfda-risk-0046", "tfda-risk-0095"), ("tfda-risk-0015",), "PASS", "ANSWER", "辨識 2015 EMA 文件，不用後來文件取代當年內容", ("12 歲以下禁用、特定 12 至 18 歲族群不建議用於咳嗽感冒",), ("2020 年 18 歲限制是後續資料，不應冒充 2015 建議")),
        HardCaseSpec("N5", "near_match", "DPP-4 抑制劑與嚴重關節疼痛有什麼安全資訊？", ("tfda-risk-0026", "tfda-risk-0019", "tfda-risk-0035"), ("tfda-risk-0026",), "PASS", "ANSWER", "只使用 DPP-4 關節疼痛文件", ("文件描述通報、發生時間及停藥後改善或再發",), ("SGLT2 文件不能支持 DPP-4 關節疼痛")),

        # 2. Temporal reasoning: same ingredient and evolving warnings.
        HardCaseSpec("T1", "temporal", "請依 2015、2017、2020 文件整理 Codeine 兒童使用限制如何演變，並標示日期。", ("tfda-risk-0015", "tfda-risk-0046", "tfda-risk-0095"), ("tfda-risk-0015", "tfda-risk-0046", "tfda-risk-0095"), "PASS", "ANSWER", "按日期分開說明，不把不同年份限制混成一條規則", ("2015、2017、2020 三份文件各自的年齡與風險重點"), ("資料沒有提供一個跨年份統一的單一風險百分比",)),
        HardCaseSpec("T2", "temporal", "Gadolinium 腦部蓄積的 2015 FDA 與 2017 EMA 文件，對風險與管理措施有何不同？", ("tfda-risk-0023", "tfda-risk-0052"), ("tfda-risk-0023", "tfda-risk-0052"), "PASS", "ANSWER", "以時間順序比較 FDA 評估階段與 EMA 後續限制", ("2015 文件說仍在評估且未知臨床危害", "2017 文件描述線性結構顯影劑的限制措施"), ("沒有證據可把未知危害說成已確定造成神經症狀")),
        HardCaseSpec("T3", "temporal", "請按照日期整理 SGLT2 抑制劑 2015 到 2018 的三種不同安全警訊。", ("tfda-risk-0019", "tfda-risk-0035", "tfda-risk-0042", "tfda-risk-0064"), ("tfda-risk-0019", "tfda-risk-0035", "tfda-risk-0042", "tfda-risk-0064"), "PASS", "ANSWER", "依日期列出不同風險，不要把它們說成同一個不良反應", ("2015 酮酸中毒、2016 急性腎損傷、2017 截肢、2018 Fournier 氏壞疽"), ("沒有資料支持四者的共同發生率")),
        HardCaseSpec("T4", "temporal", "Tofacitinib 2019 與 2021 的血栓及死亡風險資訊有何進展？", ("tfda-risk-0070", "tfda-risk-0112"), ("tfda-risk-0070", "tfda-risk-0112"), "PASS", "ANSWER", "保留兩個年份的劑量與證據差異", ("2019 聚焦較高劑量試驗結果", "2021 文件指出較低劑量也可能有血栓與死亡風險"), ("不能把 2019 的高劑量結果單獨套用成所有劑量的結論")),
        HardCaseSpec("T5", "temporal", "Amiodarone 2015 與 2020 文件涉及的安全問題是否相同？請分別說明。", ("tfda-risk-0017", "tfda-risk-0093"), ("tfda-risk-0017", "tfda-risk-0093"), "PASS", "ANSWER", "比較兩份日期不同文件的主題，不要只因成分相同就合併", ("兩份文件的藥品成分相同但安全議題需依原文辨識"), ("文件沒有提供一個跨年份合併後的總風險數字")),

        # 3. Same risk / different wording or conclusion strength.
        HardCaseSpec("C1", "same_risk_conflict", "Codeine 兒童咳嗽限制到底是 12 歲以下，還是 18 歲以下？請解釋兩份文件。", ("tfda-risk-0015", "tfda-risk-0095"), ("tfda-risk-0015", "tfda-risk-0095"), "PASS", "ANSWER", "指出兩個年份與不同監管來源的限制差異，不強行選一個當作唯一規則", ("2015 文件的 12 歲限制與 2020 文件對 18 歲以下咳嗽感冒用藥的更嚴格建議"), ("不能在沒有指定國家、年份、處方或非處方情境時宣稱只有一個普遍答案")),
        HardCaseSpec("C2", "same_risk_conflict", "Gadolinium 腦部蓄積是否已證明會造成神經傷害？請比較 2015 與 2017 說法。", ("tfda-risk-0023", "tfda-risk-0052"), ("tfda-risk-0023", "tfda-risk-0052"), "PASS", "ANSWER", "保留『已觀察到蓄積』與『臨床後果未知』的限制", ("兩份文件都說有腦部蓄積證據且臨床後果尚未知", "2017 文件另有管理限制"), ("神經症狀、神經傷害因果關係")),
        HardCaseSpec("C3", "same_risk_conflict", "Tofacitinib 的血栓風險是否只發生在 10 mg 每日兩次？", ("tfda-risk-0070", "tfda-risk-0112"), ("tfda-risk-0070", "tfda-risk-0112"), "PASS", "ANSWER", "說明早期高劑量結果與後續較低劑量結果的差異", ("2019 文件聚焦 10 mg 每日兩次", "2021 文件指出較低劑量也可能增加血栓與死亡風險"), ("不同疾病適應症的個人風險大小")),
        HardCaseSpec("C4", "same_risk_conflict", "SGLT2 截肢風險是否只和 canagliflozin 有關？", ("tfda-risk-0042", "tfda-risk-0064", "tfda-risk-0019"), ("tfda-risk-0042",), "PASS", "ANSWER", "以 2017 文件的證據強度回答，不能把『尚未發現』說成『不可能』", ("canagliflozin 的截肢訊號", "其他 SGLT2 成分當時尚未發現但資料有限"), ("其他成分的確切風險機率")),
        HardCaseSpec("C5", "same_risk_conflict", "Codeine 文件一方面說台灣沒有 12 歲以下呼吸抑制通報，另一方面又限制兒童使用，這矛盾嗎？", ("tfda-risk-0015", "tfda-risk-0095"), ("tfda-risk-0015", "tfda-risk-0095"), "PASS", "ANSWER", "區分國內通報觀察、國外風險與預防性管制，不把它當邏輯矛盾", ("沒有通報不等於沒有風險", "後續文件根據國外評估與預防原則收緊限制"), ("台灣兒童實際發生率")),

        # 4. Numbers present in context but not the number asked by query.
        HardCaseSpec("X1", "numeric_trap", "SGLT2 酮酸中毒的發生率、死亡率和每十萬人風險是多少？", ("tfda-risk-0019", "tfda-risk-0035"), ("tfda-risk-0019",), "PASS", "ANSWER", "只能回答文件有的通報背景，明確指出缺少精確率", ("文件有安全警訊與通報背景"), ("發生率", "死亡率", "每十萬人風險")),
        HardCaseSpec("X2", "numeric_trap", "SGLT2 Fournier 氏壞疽的 12 個案例占所有使用者的百分比是多少？", ("tfda-risk-0064", "tfda-risk-0019"), ("tfda-risk-0064",), "PASS", "ANSWER", "可報告文件中的 12 例與 7 男 5 女，但不能換算使用者百分比", ("2013 至 2018 年間 12 例、7 男 5 女、1 人死亡"), ("所有使用者分母", "發生率或百分比")),
        HardCaseSpec("X3", "numeric_trap", "Codeine 對不同年齡兒童造成呼吸抑制的確切機率是多少？", ("tfda-risk-0015", "tfda-risk-0046", "tfda-risk-0095"), ("tfda-risk-0015", "tfda-risk-0046", "tfda-risk-0095"), "PASS", "ANSWER", "回答年齡限制與風險機制，但拒絕捏造機率", ("文件描述代謝差異、呼吸抑制風險與年齡限制"), ("各年齡層確切機率")),
        HardCaseSpec("X4", "numeric_trap", "Tofacitinib 10 mg 與 5 mg 的肺栓塞相對風險和死亡率各是多少？", ("tfda-risk-0070", "tfda-risk-0112"), ("tfda-risk-0070",), "PASS", "ANSWER", "只說文件支持的劑量比較與風險方向，不補數字", ("文件指出 10 mg 每日兩次相對較高風險背景"), ("相對風險數值", "死亡率")),
        HardCaseSpec("X5", "numeric_trap", "Hydrochlorothiazide 累積到多少 mg 會造成皮膚癌？個人絕對風險是多少？", ("tfda-risk-0065", "tfda-risk-0112"), ("tfda-risk-0065",), "PASS", "ANSWER", "說明研究中的暴露關聯，但不把門檻或個人風險編造出來", ("文件描述累積暴露量與非黑色素瘤皮膚癌的關聯"), ("固定致癌劑量門檻", "個人絕對風險", "確定因果關係")),

        # 5. Partial support: the model must answer known part and mark the gap.
        HardCaseSpec("P1", "partial_guess", "Gadolinium 腦部蓄積會造成哪些神經症狀？應該多久監測一次？", ("tfda-risk-0023", "tfda-risk-0052"), ("tfda-risk-0023", "tfda-risk-0052"), "PASS", "ANSWER", "回答蓄積與未知後果，明確承認症狀與監測頻率不在文件中", ("兩份文件描述腦部蓄積與管理限制"), ("具體神經症狀", "監測頻率")),
        HardCaseSpec("P2", "partial_guess", "SGLT2 酮酸中毒發生後應停藥多久、改用什麼劑量？", ("tfda-risk-0019", "tfda-risk-0035"), ("tfda-risk-0019",), "PASS", "ANSWER", "回答文件支持的停用與就醫安全提醒，不猜療程與劑量", ("文件支持確診後停用並處理酸中毒"), ("停藥多久", "替代藥物劑量")),
        HardCaseSpec("P3", "partial_guess", "Hydrochlorothiazide 使用者是否都應接受皮膚癌篩檢？多久一次？", ("tfda-risk-0065", "tfda-risk-0112"), ("tfda-risk-0065",), "PASS", "ANSWER", "回答皮膚癌風險關聯與防曬提醒，承認文件沒有篩檢頻率", ("文件提供累積暴露關聯與安全提醒"), ("所有使用者都應篩檢的規則", "篩檢間隔")),
        HardCaseSpec("P4", "partial_guess", "12 到 18 歲兒童使用 Codeine 的安全劑量是多少 mg/kg？", ("tfda-risk-0015", "tfda-risk-0095"), ("tfda-risk-0015", "tfda-risk-0095"), "PASS", "ANSWER", "回答年齡與呼吸功能風險限制，不能補 mg/kg 劑量", ("文件要求依年齡減量或由醫師評估",), ("mg/kg 精確劑量", "個人化處方")),
        HardCaseSpec("P5", "partial_guess", "DPP-4 抑制劑關節痛復發時，是否可以自行重新開始原藥？復發率是多少？", ("tfda-risk-0026", "tfda-risk-0019"), ("tfda-risk-0026",), "PASS", "ANSWER", "回答通報中的再發現象，不能提供自行重啟建議或復發率", ("文件記載停藥後改善與重新使用後再發的通報"), ("自行重啟是否安全", "復發率")),

        # 6. High-similarity fallback: same ingredient/class, no direct evidence.
        HardCaseSpec("I1", "hard_insufficient", "SGLT2 抑制劑酮酸中毒有哪些安全說明？", ("tfda-risk-0035", "tfda-risk-0042", "tfda-risk-0064"), (), "FALLBACK", "INSUFFICIENT", "明確表示目前 context 沒有酮酸中毒的直接證據", (), ("酮酸中毒直接證據",), True),
        HardCaseSpec("I2", "hard_insufficient", "Gadolinium 顯影劑造成急性腎損傷的發生率是多少？", ("tfda-risk-0023", "tfda-risk-0052"), (), "FALLBACK", "INSUFFICIENT", "不要把腦部蓄積文件當成急性腎損傷證據", (), ("急性腎損傷證據", "發生率"), True),
        HardCaseSpec("I3", "hard_insufficient", "DPP-4 抑制劑造成胰臟炎的確切風險是多少？", ("tfda-risk-0019", "tfda-risk-0035", "tfda-risk-0064"), (), "FALLBACK", "INSUFFICIENT", "不要因為都是糖尿病藥就推論 DPP-4 胰臟炎", (), ("DPP-4 胰臟炎直接證據", "確切風險"), True),
        HardCaseSpec("I4", "hard_insufficient", "Hydrochlorothiazide 使用後皮膚癌的風險是多少？", ("tfda-risk-0112", "tfda-risk-0070"), (), "FALLBACK", "INSUFFICIENT", "不要用 JAK 抑制劑文件代替 hydrochlorothiazide 文件", (), ("Hydrochlorothiazide 皮膚癌證據",), True),
        HardCaseSpec("I5", "hard_insufficient", "Codeine 與 tramadol 未滿 18 歲扁桃腺手術後止痛的具體禁忌是什麼？", ("tfda-risk-0015", "tfda-risk-0095"), (), "FALLBACK", "INSUFFICIENT", "目前 context 沒有包含 codeine/tramadol 2017 文件，不能引用記憶補上", (), ("tfda-risk-0046 的具體術後禁忌",), True),
    ]

