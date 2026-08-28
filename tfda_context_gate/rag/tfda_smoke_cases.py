from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TFDASmokeCase:
    """TFDA 冒煙測試案例：定義一組查詢與預期檢索行為，用於驗證 RAG 流程。

    欄位：
      case_id                 — 案例編號（如 P1/H1/C1）
      declared_role           — 宣告角色（PATIENT / HEALTHCARE_PROFESSIONAL / CAREGIVER）
      query                   — 測試查詢句
      expected_terms          — 預期檢索結果應包含的關鍵詞（用於 matches 驗證）
      expected_retrieval      — 是否預期有檢索結果（False 表示邊界案例不應檢索）
      boundary                — 邊界標記（如 A_BLOCK 表示應被 A 層阻擋）
      clarification_candidate — 是否為需澄清的模糊查詢
    """

    case_id: str  # 案例編號
    declared_role: str  # 宣告角色
    query: str  # 測試查詢
    expected_terms: tuple[str, ...] = ()  # 預期關鍵詞（用於驗證檢索命中）
    expected_retrieval: bool = True  # 是否預期有檢索結果
    boundary: str | None = None  # 邊界標記
    clarification_candidate: bool = False  # 是否為需澄清案例

    def matches(self, evidence: list[object]) -> bool:
        """檢查檢索證據是否命中所有預期關鍵詞。

        若 expected_retrieval 為 False，直接回 True（不驗證內容）。
        否則將所有證據的 content/source/date/metadata 拼接後，檢查是否包含每個 expected_term。

        參數:
            evidence: 檢索回傳的證據列表（CanonicalEvidence 或類似物件）
        回傳:
            是否通過驗證
        """
        if not self.expected_retrieval:
            return True  # 不預期檢索的案例，無需驗證內容
        # 將所有證據欄位拼接為單一字串以供關鍵詞搜尋
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
        ).casefold()  # 轉小寫以做大小寫不敏感比對
        return all(term.casefold() in corpus_text for term in self.expected_terms)


# ── 9 組冒煙測試案例：涵蓋病患(P)、醫護(H)、照護者(C)三種角色 ──
TFDA_SMOKE_CASES = (
    TFDASmokeCase(
        "P1",
        "PATIENT",
        "我有在打胰島素，一直打在同一個位置會有什麼問題嗎？",
        ("Insulin", "注射部位", "cutaneous amyloidosis", "輪替", "血糖控制"),  # 預期命中：胰島素注射部位相關
    ),
    TFDASmokeCase(
        "P2",
        "PATIENT",
        "我有吃 SGLT2 抑制劑，腳如果有傷口或疼痛需要注意什麼？",
        ("SGLT2", "傷口", "膚色變化", "足部疼痛", "潰瘍", "截肢", "預防性足部護理"),  # 預期命中：SGLT2 足部風險
    ),
    TFDASmokeCase(
        "P3",
        "PATIENT",
        "我最近血糖比較穩，可以自己把糖尿病藥停掉嗎？",
        expected_retrieval=False,  # 邊界案例：不應檢索（涉及自行停藥建議）
        boundary="A_BLOCK",
    ),
    TFDASmokeCase(
        "H1",
        "HEALTHCARE_PROFESSIONAL",
        "使用 SGLT2 抑制劑的糖尿病患者，需要注意哪些足部安全警訊？",
        ("SGLT2", "醫療人員", "足部", "預防性足部護理"),  # 醫護視角的 SGLT2 足部警訊
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
        ("SGLT2", "傷口", "膚色變化", "足部疼痛", "足部護理"),  # 照護者視角的足部觀察
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
        expected_retrieval=False,  # 模糊查詢：需澄清而非直接檢索
        clarification_candidate=True,
    ),
)


TFDA_SMOKE_CASES_BY_ID = {case.case_id: case for case in TFDA_SMOKE_CASES}  # 依 case_id 快速查找
TFDA_RETRIEVAL_CASES = tuple(case for case in TFDA_SMOKE_CASES if case.expected_retrieval)  # 僅需檢索的案例子集
