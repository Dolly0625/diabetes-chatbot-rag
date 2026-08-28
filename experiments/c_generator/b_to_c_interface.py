"""
tfda_context_gate.c_generator.b_to_c_interface — B→C 介面夾具建構器

【本檔定位】
- 將 B 層實際檢索結果（langchain_documents.json + hybrid_narrow_top4.json）
  與 C 層實驗 case 規格（build_case_specs / build_hard_case_specs）組裝為
  B-to-C interface_cases，供 C 生成器實驗使用。

【build_interface 流程（5 步）】
1. 讀 B 層文件庫：b_run_dir/data/processed/langchain_documents.json → by_id 索引
2. 讀 B 層 Phase5 結果：b_run_dir/results/hybrid_narrow_top4.json → 取 runs[0].context_rows 得 S1 的 document_ids
3. 選 case 規格：依環境變數 C_CASE_SET 決定用 build_case_specs（baseline）或 build_hard_case_specs（hard）
4. 逐 spec 組裝：S1 用 S1_ids（真實 B narrow_top4），其他用 spec.context_ids；校驗文件存在性後組 contexts
5. 寫檔：輸出至 output_path（JSON，含 ground_truth 與 provenance）

【S1 特殊處理】
- S1 使用真實 B narrow_top4 的 context 與 PASS 結果；其他 cases 使用手動指定的 fixture（同語料庫，不宣稱新檢索結果）。

【環境變數】
- C_CASE_SET：baseline（預設）或 hard
- C_B_RUN_DIR / C_INTERFACE_PATH：CLI 模式時的輸入／輸出路徑
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from experiments.c_generator.experiment_cases import build_case_specs
    from experiments.c_generator.hard_experiment_cases import build_hard_case_specs
except ImportError:
    from tfda_context_gate.c_generator.experiment_cases import build_case_specs  # type: ignore[no-redef]
    from tfda_context_gate.c_generator.hard_experiment_cases import build_hard_case_specs  # type: ignore[no-redef]


def build_interface(b_run_dir: Path, output_path: Path) -> list[dict]:
    """建構 B→C 介面夾具（讀 B 層結果 → 組 C 層實驗 cases → 寫檔）。

    參數：
        b_run_dir：B 層 run 目錄（需含 data/processed/langchain_documents.json 與 results/hybrid_narrow_top4.json）
        output_path：輸出的 interface_cases.json 路徑

    回傳：
        interface_cases 陣列（每項含 case_id / query / contexts / ground_truth / provenance 等）
    """
    docs_path = b_run_dir / "data" / "processed" / "langchain_documents.json"  # 步驟1：B 文件庫路徑
    phase5_path = b_run_dir / "results" / "hybrid_narrow_top4.json"  # 步驟2：B Phase5 結果路徑
    documents = json.loads(docs_path.read_text(encoding="utf-8"))  # 讀取全部 B 文件
    by_id = {item["id"]: item for item in documents}  # 建 id→document 索引，供後續校驗與組裝

    phase5 = json.loads(phase5_path.read_text(encoding="utf-8"))  # 讀取 Phase5 結果
    s1_rows = phase5["runs"][0]["context_rows"]  # 取第一個 run 的 context_rows（S1 真實檢索結果）
    s1_ids = [row["document_id"] for row in s1_rows]  # 抽出 S1 的 document_id 清單

    case_builder = build_hard_case_specs if os.getenv("C_CASE_SET", "baseline") == "hard" else build_case_specs  # 步驟3：依環境變數選規格建構器
    interface_cases: list[dict] = []
    for spec in case_builder():  # 步驟4：逐 spec 組裝
        context_ids = s1_ids if spec.case_id == "S1" else list(spec.context_ids)  # S1 用真實 B 結果，其他用 spec 自帶 IDs
        for document_id in context_ids:  # 校驗：每個引用的 document_id 必須在 B 文件庫中存在
            if document_id not in by_id:
                raise RuntimeError(f"C fixture references missing B document: {document_id}")
        contexts = []
        for document_id in context_ids:  # 組 contexts 陣列（供 C prompt 使用）
            item = by_id[document_id]  # 取對應文件
            metadata = item.get("metadata", {})  # 取 metadata
            contexts.append(
                {
                    "document_id": document_id,
                    "row_index": metadata.get("row_index"),
                    "發布日期": metadata.get("發布日期", ""),
                    "藥品成分": metadata.get("藥品成分", ""),
                    "page_content": item["page_content"],
                    "source_dataset": metadata.get("source_dataset", ""),
                }
            )
        interface_cases.append(
            {
                "case_id": spec.case_id,
                "case_type": spec.case_type,
                "query": spec.query,
                "b_decision": spec.b_decision,
                "stress_test": spec.stress_test,
                "approved_document_ids": list(spec.approved_ids),  # B 核准的 evidence IDs（v2 引用唯一來源）
                "context_document_ids": context_ids,  # 實際使用的 context IDs
                "contexts": contexts,  # 完整文件內容（含 page_content）
                "ground_truth": {
                    "source": "manual",  # 人工標註
                    "expected_decision": getattr(
                        spec,
                        "expected_decision",
                        "INSUFFICIENT" if spec.case_type == "insufficient" else "ANSWER",  # 預設：insufficient 類型為 INSUFFICIENT，其餘為 ANSWER
                    ),
                    "expected_handling": spec.expected_handling,
                    "supported_facts": list(spec.supported_facts),  # 文件支持的重點
                    "unavailable_facts": list(spec.unavailable_facts),  # 文件未提供的重點
                    "expected_evidence_ids": list(spec.approved_ids),
                },
                "provenance": {
                    "b_run_dir": str(b_run_dir),
                    "b_phase5_reference": str(phase5_path) if spec.case_id == "S1" else None,  # 僅 S1 標註真實 B 來源
                    "fixture_note": (
                        "S1 uses the actual B narrow_top4 context and PASS result. "
                        "Other cases use manually specified B-to-C interface fixtures "
                        "over the same TFDA corpus; no new retrieval result is claimed."
                    ),
                },
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)  # 步驟5：確保輸出目錄存在
    output_path.write_text(json.dumps(interface_cases, ensure_ascii=False, indent=2), encoding="utf-8")  # 寫入 JSON 檔
    return interface_cases


if __name__ == "__main__":
    """CLI 入口：依環境變數讀 B 層結果並產生 interface_cases.json。"""
    import os
    from tfda_context_gate.run_config import RESULTS_DIR

    b_dir = Path(os.getenv("C_B_RUN_DIR", "runs/b_nemotron_20260818"))  # B 層 run 目錄（可由環境變數覆蓋）
    if not b_dir.is_absolute():  # 相對路徑 → 轉為專案根目錄下的絕對路徑
        b_dir = Path(__file__).resolve().parents[1] / b_dir
    out = Path(os.getenv("C_INTERFACE_PATH", str(RESULTS_DIR / "interface_cases.json")))  # 輸出路徑
    if not out.is_absolute():  # 相對路徑 → 轉為絕對路徑
        out = Path(__file__).resolve().parents[1] / out
    cases = build_interface(b_dir, out)  # 執行建構
    print(f"wrote {len(cases)} B-to-C interface cases to {out}")
