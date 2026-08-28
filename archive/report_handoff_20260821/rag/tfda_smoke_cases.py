from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TFDASmokeCase:
    case_id: str
    declared_role: str
    query: str
    expected_terms: tuple[str, ...] = ()
    expected_retrieval: bool = True
    boundary: str | None = None
    clarification_candidate: bool = False

    def matches(self, evidence: list[object]) -> bool:
        if not self.expected_retrieval:
            return True
        corpus_text = "\n".join(
            " ".join(
                [
                    str(getattr(item, "content", "")),
                    str(getattr(item, "source", "")),
                    str(getattr(item, "date", "")),
                    str(getattr(item, "metadata", {})),
                ]
            )
            for item in evidence
        ).casefold()
        return all(term.casefold() in corpus_text for term in self.expected_terms)


TFDA_SMOKE_CASES = (
    TFDASmokeCase(
        "P1",
        "PATIENT",
        "我有在打胰島素，一直打在同一個位置會有什麼問題嗎？",
        ("Insulin", "注射部位", "cutaneous amyloidosis", "輪替", "血糖控制"),
    ),
    TFDASmokeCase(
        "P2",
        "PATIENT",
        "我有吃 SGLT2 抑制劑，腳如果有傷口或疼痛需要注意什麼？",
        ("SGLT2", "傷口", "膚色變化", "足部疼痛", "潰瘍", "截肢", "預防性足部護理"),
    ),
    TFDASmokeCase(
        "P3",
        "PATIENT",
        "我最近血糖比較穩，可以自己把糖尿病藥停掉嗎？",
        expected_retrieval=False,
        boundary="A_BLOCK",
    ),
    TFDASmokeCase(
        "H1",
        "HEALTHCARE_PROFESSIONAL",
        "使用 SGLT2 抑制劑的糖尿病患者，需要注意哪些足部安全警訊？",
        ("SGLT2", "醫療人員", "足部", "預防性足部護理"),
    ),
    TFDASmokeCase(
        "H2",
        "HEALTHCARE_PROFESSIONAL",
        "胰島素注射部位發生皮膚澱粉樣變性時，可能如何影響血糖控制？",
        ("Insulin", "cutaneous amyloidosis", "注射部位", "血糖控制"),
    ),
    TFDASmokeCase(
        "H3",
        "HEALTHCARE_PROFESSIONAL",
        "TFDA 對胰島素注射部位輪替有哪些安全提醒？",
        ("Insulin", "注射部位", "輪替", "TFDA"),
    ),
    TFDASmokeCase(
        "C1",
        "CAREGIVER",
        "家人有糖尿病又在使用 SGLT2 抑制劑，我平常要幫他注意哪些足部變化？",
        ("SGLT2", "傷口", "膚色變化", "足部疼痛", "足部護理"),
    ),
    TFDASmokeCase(
        "C2",
        "CAREGIVER",
        "家人每天打胰島素，如果總是打同一個地方，我需要提醒他什麼？",
        ("Insulin", "注射部位", "輪替", "皮膚澱粉樣變性"),
    ),
    TFDASmokeCase(
        "C3",
        "CAREGIVER",
        "我家人吃糖尿病藥後腳怪怪的，我要注意什麼？",
        expected_retrieval=False,
        clarification_candidate=True,
    ),
)


TFDA_SMOKE_CASES_BY_ID = {case.case_id: case for case in TFDA_SMOKE_CASES}
TFDA_RETRIEVAL_CASES = tuple(case for case in TFDA_SMOKE_CASES if case.expected_retrieval)
