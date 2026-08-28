from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from experiments.c_generator.b_to_c_interface import build_interface
    from experiments.c_generator.evaluator import evaluate_outputs, summarize, write_summary
    from experiments.c_generator.experiment_cases import build_case_specs
    from experiments.c_generator.generator import build_llm, run_generators
except ImportError:
    from tfda_context_gate.c_generator.b_to_c_interface import build_interface  # type: ignore[no-redef]
    from tfda_context_gate.c_generator.evaluator import evaluate_outputs, summarize, write_summary  # type: ignore[no-redef]
    from tfda_context_gate.c_generator.experiment_cases import build_case_specs  # type: ignore[no-redef]
    from tfda_context_gate.c_generator.generator import build_llm, run_generators  # type: ignore[no-redef]
from tfda_context_gate.c_generator.schemas import AuxiliaryEvaluation
from tfda_context_gate.rate_limiter import RollingRequestRateLimiter
from tfda_context_gate.run_config import (
    PROJECT_ROOT,
    REPORT_DIR,
    RESULTS_DIR,
    RUN_DIR,
    env_value,
    ensure_run_dirs,
)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def resolve_b_run_dir() -> Path:
    raw = Path(os.getenv("C_B_RUN_DIR", "runs/b_nemotron_20260818")).expanduser()
    if raw.is_absolute():
        return raw
    return PROJECT_ROOT / "tfda_context_gate" / raw


def write_report(cases: list[dict], summary: dict, config: dict, report_path: Path, b_run_dir: Path) -> None:
    method_rows = []
    for row in summary.get("methods", []):
        method_rows.append(
            f"| {row['method']} | {row['n_outputs']} | {row.get('claim_support_rate')} | "
            f"{row.get('unsupported_claim_rate')} | {row.get('citation_accuracy')} | "
            f"{row.get('citation_coverage')} | {row.get('insufficient_handling_accuracy_partial_and_stress')} | "
            f"{row.get('model_latency_mean_seconds')} | {row.get('total_tokens_mean')} |"
        )
    evidence_summary = next(
        (row for row in summary.get("methods", []) if row["method"] == "evidence_aware"),
        {},
    )
    manual_case_lines = []
    for case_type in sorted((evidence_summary.get("case_type_metrics") or {}).keys()):
        metric = (evidence_summary.get("case_type_metrics") or {}).get(case_type, {})
        manual_case_lines.append(f"- {case_type}: {metric.get('manual_decision_accuracy')}")
    case_counts = summary.get("case_counts", {})
    case_count_text = ", ".join(f"{key} {value}" for key, value in case_counts.items())
    hard_run = "hard_insufficient" in case_counts
    if hard_run:
        data_provenance = "本 run 的 30 題都是同一份 129 筆 corpus 上人工設計的困難 interface fixture；沒有把 Ground Truth 摘要放進 Generator prompt。每題仍保留 B decision、context documents 與 approved evidence IDs，Ground Truth 只在評估階段使用。"
    else:
        data_provenance = "S1 使用 B Phase 5 `narrow_top4` 真正產生的 context；其餘案例是在同一份 129 筆 TFDA corpus 上，由人工指定 B→C interface 的 context、approved evidence IDs 與 Ground Truth，讓 C 可以固定比較三種 Generator。"
    report = f"""# C：Grounded / Evidence-aware Generator 實驗報告

## 先講結論

這次實驗是在 B 已經把 Context Gate 判斷完之後，觀察三種 Generator 怎麼使用同一批 context：Baseline、Grounded、Evidence-aware。B 負責「哪些文件可以進入生成」，C 負責「拿到這些文件後，回答是否只講文件支持的內容，以及能不能指出證據 ID」。

本實驗的主要標準是人工寫好的 Ground Truth；LLM Judge 只作為輔助評估，不把它當成正解來源。

## 實驗設定

- 模型：`{config['model']}`
- endpoint：從 `.env` 的 `base_url` 讀取；本次實際設定為 `{config['base_url']}`
- temperature：`{config['temperature']}`
- timeout：程式設定 `{config['request_timeout_seconds']}` 秒，傳給 `ChatOpenRouter` 前轉成 `{config['sdk_timeout_milliseconds']}` 毫秒。結果中的 latency 全部以秒記錄。
- 每分鐘限流：rolling 60 秒最多 20 次；429 先讀 Retry-After，沒有才 exponential backoff。
- 案例：{len(cases)} 個（{case_count_text}）。
- 每個案例都跑三種 Generator，因此主要生成請求共 {len(cases) * 3} 次；完整評估另有輔助 Judge 請求。
- B run：`{b_run_dir}`

## 資料怎麼來

文件不是臨時複製的摘要，而是 B Nemotron run 產生的 `data/processed/langchain_documents.json` 中的 TFDA 風險溝通文件。{data_provenance}這些 fixture 不宣稱是重新執行每個 query 的 retrieval 結果。

## 結果

| 方法 | 輸出數 | Claim support rate | Unsupported rate | Citation accuracy | Citation coverage | Partial/insufficient handling | 平均 model latency (秒) | 平均 total tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(method_rows)}

Evidence-aware 的人工 Ground Truth decision accuracy：`{next((r.get('manual_decision_accuracy') for r in summary.get('methods', []) if r['method'] == 'evidence_aware'), None)}`；在 partial + insufficient 案例上的人工 decision accuracy：`{next((r.get('manual_partial_and_insufficient_decision_accuracy') for r in summary.get('methods', []) if r['method'] == 'evidence_aware'), None)}`。Baseline/Grounded 的 claim support 與 insufficient handling 數字是輔助 Judge 估計，不能解讀成人工標註的金標準準確率。

Evidence-aware 依案例類型的人工 decision accuracy：
{chr(10).join(manual_case_lines)}

這裡的 `partial_guess` Ground Truth 定義是「回答文件支持的部分，同時承認缺口」，所以被輸出成 `INSUFFICIENT` 仍算沒有符合本實驗預設；`hard_insufficient` 則相反，沒有直接證據時本來就應該拒答。這個結果可以區分模型的保守性和真正的防止亂猜能力。

這一版特別移除了 Generator 可看到的 Ground Truth 摘要、supported facts 和 unavailable facts，避免模型拿到答案提示；這也是它和前一版 prototype 結果不能直接當成同一條件比較的原因。

`model_latency` 是 API 實際推論時間；`total_wall_time` 還包括 request rate limiter 和 retry 等等待。這次報告比較 Generator 時主要看 model latency，避免把免費 endpoint 的限流等待誤算成模型速度。

## 口語化解讀

Baseline 就像把文件丟給模型後直接問它：「你看完幫我回答。」它可能回答得流暢，但實驗重點是：它沒有被要求逐句對文件負責，也沒有 Evidence ID 可以回頭核對。

Grounded 多了一層規則：只能用 context，文件沒有的數字就要說沒有。它通常比 Baseline 更能守住資料邊界，但輸出仍然是自然語言，後續系統若要知道「這句話對應哪一篇文件」，還得另外解析。

Evidence-aware 除了回答，還要把重要 claim 和 `evidence_ids` 一起輸出。這讓 downstream workflow 可以直接檢查引用是不是來自 B approved IDs。不過結構化輸出不會自動保證內容正確；所以本實驗仍然用人工 Ground Truth 判斷，並把 citation accuracy、coverage 與 claim support 分開看。

## 限制

1. 20 個案例是小型研究設計，不足以宣稱三種 Generator 對所有醫療問題的普遍準確率。
2. partial 與 insufficient stress test 是刻意設計來測「資料不夠時會不會承認」，不是一般使用情境的自然分布。
3. 輔助 LLM Judge 不是 Ground Truth；若 Judge 與人工標準不一致，應保留差異並回到人工檢查。
4. 免費模型 endpoint 的延遲會波動；因此報告分開保存 model latency、rate-limit wait、retry wait 和 total wall time。
5. C 不重新宣稱 B 的 retrieval 成效；B 的實際 retrieval / reranker / Context Gate 結果保存在獨立 run directory。

## 可重現檔案

- `results/interface_cases.json`
- `results/generator_outputs.jsonl`
- `results/llm_judge_evaluations.jsonl`
- `results/evaluation_summary.json`
- `results/evaluation_results.csv`
- `results/c_request_events.jsonl`
- `data/processed/langchain_documents.json`（B run 內）
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-only", action="store_true", help="Run S1 once with all three generators")
    parser.add_argument("--evaluate-only", action="store_true", help="Reuse the completed generator_outputs.jsonl")
    parser.add_argument("--evaluate-limit", type=int, default=None, help="Only for debugging: evaluate the first N generator rows")
    parser.add_argument("--summarize-only", action="store_true", help="Reuse completed generator and auxiliary evaluation files")
    args = parser.parse_args()
    ensure_run_dirs()
    b_run_dir = resolve_b_run_dir()
    interface_path = RESULTS_DIR / "interface_cases.json"
    cases = build_interface(b_run_dir, interface_path)
    (RESULTS_DIR / "experiment_config.json").write_text(
        json.dumps({"b_run_dir": str(b_run_dir), "case_count": len(cases), "smoke_only": args.smoke_only}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    event_path = RESULTS_DIR / "c_request_events.jsonl"
    state_path = PROJECT_ROOT / ".openrouter_rate_limit_state.json"
    limiter = RollingRequestRateLimiter(state_path, event_path)
    output_path = RESULTS_DIR / ("smoke_outputs.jsonl" if args.smoke_only else "generator_outputs.jsonl")
    if args.evaluate_only and args.smoke_only:
        raise SystemExit("--evaluate-only and --smoke-only cannot be combined")
    if args.summarize_only and args.smoke_only:
        raise SystemExit("--summarize-only and --smoke-only cannot be combined")
    if args.summarize_only:
        output_rows = load_jsonl(RESULTS_DIR / "generator_outputs.jsonl")
        evaluations = load_jsonl(RESULTS_DIR / "llm_judge_evaluations.jsonl")
        expected_rows = len(cases) * 3
        if len(output_rows) != expected_rows or len(evaluations) != expected_rows:
            raise SystemExit(f"Expected {expected_rows} generator and auxiliary rows, found {len(output_rows)} and {len(evaluations)}")
        _, _, generator_config = build_llm()
        summary, summary_rows = summarize(cases, output_rows, evaluations)
        summary["generator_config"] = generator_config
        summary["auxiliary_judge_config"] = "reused from existing llm_judge_evaluations.jsonl"
        summary["input_interface"] = str(interface_path)
        summary["manual_ground_truth_summary"] = "Each case has manually specified expected_decision, expected_handling, supported_facts and unavailable_facts."
        write_summary(summary, summary_rows, RESULTS_DIR / "evaluation_summary.json", RESULTS_DIR / "evaluation_results.csv")
        write_report(cases, summary, generator_config, REPORT_DIR / "C_Grounded_Evidence_Aware_Generator.md", b_run_dir)
        print(f"summary: {RESULTS_DIR / 'evaluation_summary.json'}")
        return
    if args.evaluate_only:
        output_rows = load_jsonl(RESULTS_DIR / "generator_outputs.jsonl")
        expected_rows = len(cases) * 3
        if len(output_rows) != expected_rows:
            raise SystemExit(f"Expected {expected_rows} generator outputs before evaluation, found {len(output_rows)}")
        if args.evaluate_limit:
            output_rows = output_rows[:args.evaluate_limit]
    else:
        output_rows = None
    if output_path.exists() and not args.evaluate_only:
        output_path.unlink()
    if not args.evaluate_only:
        output_rows = run_generators(cases, output_path, limiter, smoke_only=args.smoke_only)
    print(f"generator outputs: {len(output_rows)} -> {output_path}")
    if args.smoke_only:
        return

    _, _, generator_config = build_llm()
    llm, _, auxiliary_config = build_llm(
        max_tokens_override=int(env_value("AUX_JUDGE_MAX_TOKENS", "1024")),
        reasoning_override=env_value("AUX_JUDGE_REASONING_EFFORT", "none"),
    )
    judge_chain = llm.with_structured_output(
        AuxiliaryEvaluation,
        method="json_schema",
        strict=True,
        include_raw=True,
    )
    aux_path = RESULTS_DIR / "llm_judge_evaluations.jsonl"
    evaluations = evaluate_outputs(cases, output_rows, judge_chain, limiter, aux_path)
    summary, summary_rows = summarize(cases, output_rows, evaluations)
    summary["generator_config"] = generator_config
    summary["auxiliary_judge_config"] = auxiliary_config
    summary["input_interface"] = str(interface_path)
    summary["manual_ground_truth_summary"] = "Each case has manually specified expected_handling, supported_facts and unavailable_facts."
    write_summary(summary, summary_rows, RESULTS_DIR / "evaluation_summary.json", RESULTS_DIR / "evaluation_results.csv")
    write_report(cases, summary, generator_config, REPORT_DIR / "C_Grounded_Evidence_Aware_Generator.md", b_run_dir)
    print(f"auxiliary evaluations: {len(evaluations)} -> {aux_path}")
    print(f"summary: {RESULTS_DIR / 'evaluation_summary.json'}")


if __name__ == "__main__":
    main()
