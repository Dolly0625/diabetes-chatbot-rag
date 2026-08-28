from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    case_type: str
    query: str
    context_ids: tuple[str, ...]
    approved_ids: tuple[str, ...]
    b_decision: str
    expected_handling: str
    supported_facts: tuple[str, ...]
    unavailable_facts: tuple[str, ...]
    stress_test: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        result["context_ids"] = list(self.context_ids)
        result["approved_ids"] = list(self.approved_ids)
        result["supported_facts"] = list(self.supported_facts)
        result["unavailable_facts"] = list(self.unavailable_facts)
        return result


def build_case_specs() -> list[CaseSpec]:
    return [
        CaseSpec("S1", "sufficient", "TFDA 對 SGLT2 抑制劑類藥品的酮酸中毒風險有什麼安全說明？", ("tfda-risk-0019", "tfda-risk-0064", "tfda-risk-0042", "tfda-risk-0035"), ("tfda-risk-0019",), "PASS", "直接回答文件支持的安全說明", ("TFDA 收錄 SGLT2 抑制劑可能導致酮酸中毒的安全資訊", "文件描述不良事件通報與相關風險背景"), ()),
        CaseSpec("S2", "sufficient", "Codeine 用於兒童咳嗽或感冒時有哪些安全限制？", ("tfda-risk-0015",), ("tfda-risk-0015",), "PASS", "直接回答文件支持的安全限制", ("兒童使用含 codeine 藥品的年齡與咳嗽感冒用途限制"), ()),
        CaseSpec("S3", "sufficient", "Hydrochlorothiazide 與非黑色素瘤皮膚癌有什麼安全警訊？", ("tfda-risk-0065",), ("tfda-risk-0065",), "PASS", "直接回答文件支持的安全警訊", ("使用 hydrochlorothiazide 與非黑色素瘤皮膚癌風險的關聯說明"), ()),
        CaseSpec("S4", "sufficient", "重複使用含釓顯影劑時，文件對腦部釓累積有什麼說明？", ("tfda-risk-0023",), ("tfda-risk-0023",), "PASS", "直接回答文件支持的累積與未知風險說明", ("重複使用含釓顯影劑可能造成腦部釓累積",), ()),
        CaseSpec("S5", "sufficient", "DPP-4 抑制劑引起嚴重關節疼痛時，TFDA 文件有哪些安全資訊？", ("tfda-risk-0026",), ("tfda-risk-0026",), "PASS", "直接回答文件支持的安全資訊", ("DPP-4 抑制劑與嚴重關節疼痛的通報與停藥後改善或再發描述",), ()),
        CaseSpec("P1", "partial", "SGLT2 抑制劑酮酸中毒的發生率與死亡率是多少？", ("tfda-risk-0019",), ("tfda-risk-0019",), "PASS", "回答可支持部分並明確承認缺口", ("文件提供酮酸中毒安全警訊與不良事件通報背景",), ("精確發生率", "死亡率")),
        CaseSpec("P2", "partial", "Codeine 用於兒童咳嗽時，各年齡層的確切風險機率是多少？", ("tfda-risk-0015",), ("tfda-risk-0015",), "PASS", "回答限制內容並明確承認缺口", ("文件提供兒童使用限制與安全警訊",), ("各年齡層精確機率")),
        CaseSpec("P3", "partial", "Hydrochlorothiazide 導致皮膚癌的因果確定性與個人絕對風險是多少？", ("tfda-risk-0065",), ("tfda-risk-0065",), "PASS", "回答關聯證據並明確承認缺口", ("文件提供皮膚癌關聯與風險分析",), ("因果確定性", "個人絕對風險")),
        CaseSpec("P4", "partial", "腦部釓累積會造成哪些神經症狀？應如何監測？", ("tfda-risk-0023",), ("tfda-risk-0023",), "PASS", "回答已知累積現象並明確承認缺口", ("文件說明腦部累積及其臨床危害尚未明確",), ("具體神經症狀", "監測建議")),
        CaseSpec("P5", "partial", "DPP-4 抑制劑關節疼痛的發生率與死亡率是多少？", ("tfda-risk-0026",), ("tfda-risk-0026",), "PASS", "回答通報內容並明確承認缺口", ("文件提供通報件數、發生時間與再發資訊",), ("發生率", "死亡率")),
        CaseSpec("D1", "distractor", "SGLT2 抑制劑酮酸中毒有哪些安全說明？", ("tfda-risk-0019", "tfda-risk-0042", "tfda-risk-0064", "tfda-risk-0035"), ("tfda-risk-0019",), "PASS", "只使用直接證據，排除同藥不同風險文件", ("tfda-risk-0019 直接描述酮酸中毒",), ("其他文件談不同安全主題")),
        CaseSpec("D2", "distractor", "Codeine 用於兒童咳嗽或感冒時有哪些安全限制？", ("tfda-risk-0015", "tfda-risk-0019", "tfda-risk-0020"), ("tfda-risk-0015",), "PASS", "只使用 codeine 文件，排除不相關文件", ("tfda-risk-0015 提供兒童 codeine 安全限制",), ("其他文件不支持本問題")),
        CaseSpec("D3", "distractor", "Hydrochlorothiazide 與非黑色素瘤皮膚癌有什麼安全警訊？", ("tfda-risk-0065", "tfda-risk-0023", "tfda-risk-0019"), ("tfda-risk-0065",), "PASS", "只使用 hydrochlorothiazide 文件，排除不相關文件", ("tfda-risk-0065 提供皮膚癌安全警訊",), ("其他文件不支持本問題")),
        CaseSpec("D4", "distractor", "DPP-4 抑制劑引起嚴重關節疼痛時有哪些安全資訊？", ("tfda-risk-0026", "tfda-risk-0035", "tfda-risk-0015"), ("tfda-risk-0026",), "PASS", "只使用關節疼痛文件，排除不相關文件", ("tfda-risk-0026 提供 DPP-4 關節疼痛資訊",), ("其他文件不支持本問題")),
        CaseSpec("D5", "distractor", "重複使用含釓顯影劑時，文件對腦部釓累積有什麼說明？", ("tfda-risk-0023", "tfda-risk-0065", "tfda-risk-0019"), ("tfda-risk-0023",), "PASS", "只使用釓顯影劑文件，排除不相關文件", ("tfda-risk-0023 提供腦部累積資訊",), ("其他文件不支持本問題")),
        CaseSpec("I1", "insufficient", "SGLT2 抑制劑酮酸中毒有哪些安全說明？", ("tfda-risk-0042", "tfda-risk-0064", "tfda-risk-0035"), (), "FALLBACK", "明確表示目前文件不足，不猜測", (), ("直接酮酸中毒證據",), True),
        CaseSpec("I2", "insufficient", "Codeine 用於兒童咳嗽或感冒時有哪些安全限制？", ("tfda-risk-0019", "tfda-risk-0020"), (), "FALLBACK", "明確表示目前文件不足，不猜測", (), ("兒童 codeine 安全限制",), True),
        CaseSpec("I3", "insufficient", "Hydrochlorothiazide 與非黑色素瘤皮膚癌有什麼安全警訊？", ("tfda-risk-0023", "tfda-risk-0019"), (), "FALLBACK", "明確表示目前文件不足，不猜測", (), ("hydrochlorothiazide 皮膚癌證據",), True),
        CaseSpec("I4", "insufficient", "DPP-4 抑制劑引起嚴重關節疼痛時有哪些安全資訊？", ("tfda-risk-0065", "tfda-risk-0019"), (), "FALLBACK", "明確表示目前文件不足，不猜測", (), ("DPP-4 關節疼痛證據",), True),
        CaseSpec("I5", "insufficient", "重複使用含釓顯影劑時，文件對腦部釓累積有什麼說明？", ("tfda-risk-0019", "tfda-risk-0065"), (), "FALLBACK", "明確表示目前文件不足，不猜測", (), ("釓顯影劑腦部累積證據",), True),
    ]

