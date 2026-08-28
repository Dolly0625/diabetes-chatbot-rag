# a_router 導讀 — 索引

> 本目錄文件為與你對話中講解的沉澱（muse-spark-1.2-contributor 產出），逐句對照 `labels.py` / `schemas.py` 原始碼與中文註解。

| 序 | 文件 | 內容 | 對應原始碼 |
|---|---|---|---|
| 00 | [00_閱讀順序.md](./00_閱讀順序.md) | 6 檔閱讀路徑、每檔關鍵問題、預估時間 | 全模組 |
| 01 | [01_labels.md](./01_labels.md) | 9 枚舉詞彙表、三層結構、一票否決邊界 | `labels.py` |
| 02 | [02_schemas與容器.md](./02_schemas與容器.md) | 容器是什麼、4 模型詳解、`rag_allowed` 硬邊界 | `schemas.py` |
| 03 | [03_StrictModel封口詳解.md](./03_StrictModel封口詳解.md) | `extra="forbid"` 封口、攻擊範例、fail-closed 鏈路 | `schemas.py: StrictModel` |
| 04 | [04_guard.md](./04_guard.md) | 守門員(Guard) B方案全擋保留自殺 | `guard.py` |

**建議**：`00 → 01 → 02 → 03` 順序閱讀，每篇 5-10 分鐘。

**關聯**：
- 上層總覽：`../../../docs/codebase/01_a_router.md`（含 Mermaid 7步管線圖）
- 下游管線：`guard.py → rules.py → policy.py → router.py`（已加中文註解，直接讀源碼即可）
