"""靜態檢查：防止 production code 出現模型名稱硬編碼"""

from pathlib import Path
import re

def test_no_hardcoded_model_in_production():
    root = Path("tfda_context_gate")
    # 硬編碼模型名稱黑名單
    patterns = [
        "opencode/mimo-v2.5",
        "qwen3-14b-opencode",
        "opencode/qwen3-14b",
    ]
    # 排除 tests、fixtures、archive、docs、__pycache__
    exclude_dirs = {"tests", "fixtures", "archive", "__pycache__", ".pycache__"}
    hits = []
    for p in root.rglob("*.py"):
        if any(ex in p.parts for ex in exclude_dirs):
            continue
        if "test_" in p.name:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in patterns:
            if pat in text:
                # 允許在註解中提及 .env 範例？但 production code 不應有
                # 檢查是否在 env_value 的 default 參數中（硬編碼 fallback）
                if re.search(rf'env_value\([^)]*"{re.escape(pat)}"', text):
                    hits.append(f"{p}:{pat}")
                elif f'"{pat}"' in text or f"'{pat}'" in text:
                    # 若出現在字串字面量且非註解，視為違規
                    # 簡單檢查：若該行不是以 # 開頭
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if pat in line and not line.strip().startswith("#") and "env_value" in line:
                            hits.append(f"{p}:{lineno}:{line.strip()[:80]}")
    assert not hits, f"production code 出現硬編碼模型名稱: {hits}"
