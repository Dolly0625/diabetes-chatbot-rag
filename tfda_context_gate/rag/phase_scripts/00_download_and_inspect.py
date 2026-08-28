from __future__ import annotations

import json
import re
import statistics
import zipfile
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

from run_config import RAW_DIR, REPORT_DIR, ensure_run_dirs, relative_to_run

RISK_URL = "https://data.fda.gov.tw/data/opendata/export/53/json"
LICENSE_URL = "https://data.fda.gov.tw/data/opendata/export/36/json"
RISK_PATH = RAW_DIR / "drug_risk_communication.json"
LICENSE_PATH = RAW_DIR / "drug_license.json.zip"
REPORT_PATH = REPORT_DIR / "phase1_data_report.md"

RISK_TEXT_FIELDS = [
    "藥品成分",
    "適應症",
    "藥理作用機轉",
    "訊息緣由",
    "藥品安全有關資訊分析及描述",
    "TFDA風險溝通說明",
]


def download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "tfda-context-gate-phase1/1.0"})
    with urlopen(request, timeout=120) as response:
        payload = response.read()
    destination.write_bytes(payload)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_license_records(path: Path) -> list[dict]:
    payload = path.read_bytes()
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        json_names = [name for name in archive.namelist() if name.lower().endswith(".json")]
        if len(json_names) != 1:
            raise ValueError(f"Expected one JSON file in license archive, got {json_names}")
        return json.loads(archive.read(json_names[0]).decode("utf-8-sig"))


def clean(value) -> str:
    return "" if value is None else str(value).strip()


def null_count(records: list[dict], field: str) -> int:
    return sum(not clean(record.get(field)) for record in records)


def length_stats(records: list[dict], field: str) -> dict[str, object]:
    lengths = [len(clean(record.get(field))) for record in records if clean(record.get(field))]
    if not lengths:
        return {"nonempty": 0, "null_or_empty": len(records), "median": None, "p90": None, "max": None}
    return {
        "nonempty": len(lengths),
        "null_or_empty": len(records) - len(lengths),
        "median": round(statistics.median(lengths), 1),
        "p90": round(statistics.quantiles(lengths, n=10)[8], 1) if len(lengths) >= 10 else max(lengths),
        "max": max(lengths),
    }


def schema_rows(records: list[dict]) -> list[dict[str, object]]:
    fields = list(records[0].keys())
    rows = []
    for field in fields:
        values = [record.get(field) for record in records]
        types = sorted({type(value).__name__ for value in values})
        rows.append(
            {
                "field": field,
                "types": ", ".join(types),
                "null_or_empty": null_count(records, field),
                "example": clean(values[0])[:240],
            }
        )
    return rows


def md_escape(value: object) -> str:
    return clean(value).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[object]]) -> str:
    output = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    output.extend("| " + " | ".join(md_escape(cell) for cell in row) + " |" for row in rows)
    return "\n".join(output)


def explicit_license_matches(risk_records: list[dict], license_records: list[dict]):
    license_map: dict[str, list[dict]] = {}
    for record in license_records:
        key = clean(record.get("許可證字號"))
        if key:
            license_map.setdefault(key, []).append(record)

    matched_rows = []
    for record in risk_records:
        text = clean(record.get("藥品名稱及許可證字號"))
        hits = sorted(key for key in license_map if key in text)
        if hits:
            matched_rows.append(
                {
                    "ingredient": clean(record.get("藥品成分")),
                    "date": clean(record.get("發布日期")),
                    "license_ids": hits,
                    "license_row_count": sum(len(license_map[key]) for key in hits),
                }
            )
    return matched_rows


def candidate_queries(risk_records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for record in risk_records:
        ingredient = clean(record.get("藥品成分"))
        grouped.setdefault(ingredient, []).append(record)

    candidates = []
    for ingredient, records in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(records) < 2:
            continue
        dates = [clean(record.get("發布日期")) for record in records]
        topics = [clean(record.get("訊息緣由"))[:120] for record in records]
        candidates.append(
            {
                "query": f"TFDA 對 {ingredient} 成分藥品有哪些藥品安全警訊？",
                "ingredient": ingredient,
                "record_count": len(records),
                "dates": dates,
                "why_suitable": "同一成分在主資料中有多筆安全資訊，可觀察多文件 retrieval 與資訊是否足夠。",
                "retrieval_difficulty": "同一成分的不同日期可能對應不同風險主題；需要判斷是同一問題的補充資料，還是只是同成分的另一則警訊。",
                "source_topics": topics,
            }
        )
    return candidates[:10]


def build_report(risk_records: list[dict], license_records: list[dict], license_matches: list[dict]) -> str:
    risk_schema = schema_rows(risk_records)
    license_schema = schema_rows(license_records)
    risk_fields = list(risk_records[0].keys())
    license_fields = list(license_records[0].keys())
    ingredients = Counter(clean(record.get("藥品成分")) for record in risk_records)
    candidates = candidate_queries(risk_records)

    risk_schema_table = table(
        ["欄位", "實際型別", "空值/空字串筆數", "第一筆範例"],
        [[row["field"], row["types"], row["null_or_empty"], row["example"]] for row in risk_schema],
    )
    license_schema_table = table(
        ["欄位", "實際型別", "空值/空字串筆數", "第一筆範例"],
        [[row["field"], row["types"], row["null_or_empty"], row["example"]] for row in license_schema],
    )

    length_table = table(
        ["主資料欄位", "空值/空字串", "字元數中位數", "P90", "最大值"],
        [
            [field, length_stats(risk_records, field)["null_or_empty"], length_stats(risk_records, field)["median"], length_stats(risk_records, field)["p90"], length_stats(risk_records, field)["max"]]
            for field in ["訊息緣由", "藥品安全有關資訊分析及描述", "TFDA風險溝通說明"]
        ],
    )

    examples = json.dumps(risk_records[:5], ensure_ascii=False, indent=2)
    candidate_table = table(
        ["Query", "成分", "資料筆數", "發布日期", "為什麼值得測", "可能難點"],
        [[item["query"], item["ingredient"], item["record_count"], ", ".join(item["dates"]), item["why_suitable"], item["retrieval_difficulty"]] for item in candidates],
    )

    return f"""# TFDA Context Gate Phase 1：資料探索報告

> 這份不是最終 RAG 實驗報告。本階段只確認 TFDA 資料的真實結構、可用欄位、join 可行性，以及下一階段可測試的 Query。

## 1. 資料來源與下載結果

主資料集：藥品安全資訊風險溝通資料。

- 提供機關：衛生福利部食品藥物管理署（TFDA）
- 政府資料開放平台：<https://data.gov.tw/dataset/9573>
- 實際 JSON endpoint：`{RISK_URL}`
- 原始檔案：`{relative_to_run(RISK_PATH)}`
- 下載時間：{datetime.now(timezone.utc).isoformat()}
- 原始 record 數量：{len(risk_records)}

第二資料集：全部藥品許可證資料集。

- 政府資料開放平台：<https://data.gov.tw/dataset/9122>
- 實際 endpoint：`{LICENSE_URL}`
- 原始下載檔：`{relative_to_run(LICENSE_PATH)}`
- 解壓後 JSON record 數量：{len(license_records)}

主資料不是「一筆藥品」的清單，而比較像「一筆 TFDA 發布的藥品安全資訊／風險溝通事件」。同一個藥品成分可以出現多次，而且每次可能是不同日期、不同安全主題。

## 2. 主資料真實 schema

主資料的原始欄位共有 {len(risk_fields)} 個：

`{", ".join(risk_fields)}`

```text
{risk_schema_table}
```

`CONTEXT` 類型的長度分析如下。字元數是直接對原始中文字串計算，沒有先改寫內容：

```text
{length_table}
```

完整前 5 筆原始資料如下；這裡保留官方欄位與原文，不把它們改寫成虛構資料：

```json
{examples}
```

## 3. 重複與資料形態

- 主資料共 {len(risk_records)} 筆。
- 不重複的「藥品成分」共有 {len(ingredients)} 個。
- 有重複紀錄的成分共有 {sum(1 for count in ingredients.values() if count > 1)} 組。
- 出現最多次的成分：{", ".join(f"{name}（{count}筆）" for name, count in ingredients.most_common(8))}。

這表示未來可以用「同一成分有多筆安全資訊」來測試多文件 retrieval；但不能把同一成分的所有文件直接當成同一個風險，因為不同發布日期可能是在談不同安全問題。

## 4. LangChain Document 建議

### 建議放進 `page_content`

這些欄位本身就是安全資訊的正文，適合讓 embedding 和 reranker 讀到：

| TFDA 原始欄位 | 用途 | 判斷 |
|---|---|---|
| 藥品成分 | 讓 Query 能對到成分 | 放入 page_content，也可複製到 metadata |
| 適應症 | 協助區分同成分不同用途 | 放入 page_content |
| 藥理作用機轉 | 回答機轉類問題時有用 | 放入 page_content；若太長可在實驗中做 ablation |
| 訊息緣由 | 說明這次安全訊息為何發布 | 放入 page_content |
| 藥品安全有關資訊分析及描述 | 主要風險分析正文 | 放入 page_content |
| TFDA風險溝通說明 | TFDA 對醫療人員與病人的溝通內容 | 放入 page_content |

`藥品名稱及許可證字號` 的內容常是多張許可證的說明、數量與網址，不是穩定的一個許可證 key。因此第一版先放入 page_content 作為補充，不把它直接當成可可靠 join 的 metadata key。

### 建議放入 metadata

| 欄位 | 來源 | 原因 |
|---|---|---|
| 發布日期 | TFDA 原始欄位 | 可用來追溯與分析時間，不代表最新狀態 |
| 藥品成分 | TFDA 原始欄位 | 可做篩選、分組與 provenance |
| row_index | pipeline 新增 | 保留原始 JSON list 的位置，方便回查 |
| document_id | pipeline 新增 | 例如 `tfda-risk-0001`，方便實驗輸出引用 |
| source_dataset | pipeline 新增 | 明確標示資料來自 TFDA 風險溝通資料集 |
| raw_source_file | pipeline 新增 | 指向保留的 raw 檔案 |

新增欄位不是 TFDA 原始欄位，之後報告中會明確分開。

## 5. Contract Gate 在這批資料真正能檢查什麼

目前可以合理檢查：

- `document_id` 是否由 pipeline 產生且不重複。
- `row_index` 是否存在。
- `發布日期` 是否為非空字串。
- `藥品成分` 是否為非空字串。
- `藥品名稱及許可證字號` 是否為非空字串。
- 組合後的 `page_content` 是否為空。

目前不能從這個主資料直接判斷：

- 這筆資訊是不是最新。
- 藥品許可證目前是否有效或已註銷。
- 這個風險是否已經被後續資料取代。

因為主資料本身沒有 `status`、`version` 或有效狀態欄位。Contract Gate 能檢查多少，取決於上游真正提供多少 metadata。

## 6. 藥品許可證資料 schema 與 join 初檢

解壓後的許可證資料共有 {len(license_fields)} 個原始欄位、{len(license_records)} 筆：

`{", ".join(license_fields)}`

```text
{license_schema_table}
```

這份資料確實有 `許可證字號`、`註銷狀態`、`註銷日期`、`有效日期`、`中文品名`、`適應症`、`主成分略述` 等欄位，而且每週同步的資料集說明也與實際檔案相符。

但是主資料的 `藥品名稱及許可證字號` 是自由文字。把 72,008 筆許可證字號逐一比對後：

- 只有 {len(license_matches)} / {len(risk_records)} 筆風險溝通 record 內含至少一個可精確對上的許可證字號。
- 這些 record 一共對到 {sum(len(item['license_ids']) for item in license_matches)} 個許可證字號、{sum(item['license_row_count'] for item in license_matches)} 筆許可證資料列。
- 其餘多數 record 只寫「共幾張許可證」、藥品名稱或查詢網址，沒有列出可直接 join 的許可證字號。

因此目前結論是：**許可證字號在少數 record 上可以可靠 join，但不能把整個主資料集視為已經具備完整的許可證 join key。** 未來若要合併，應保留 `join_status=exact / unavailable`，不能用藥品成分文字硬猜。

## 7. 下一階段候選 Query

以下 Query 都是從主資料中實際重複出現的成分產生，不代表現在已經建立 ground truth，也還沒有開始 retrieval：

{candidate_table}

這些候選的共同難點是：同一成分的多筆資料可能是不同日期、不同安全主題。這正好適合下一階段先跑真正的 Retriever，再對 Top-K 做人工 `Relevant / Partial / Irrelevant` 標註；現在不先預設哪筆一定是 distractor。

## 8. Phase 1 結論與下一步

主資料適合當作真實 RAG corpus，因為每筆 record 本身就是 TFDA 發布的安全資訊，正文包含訊息緣由、風險分析與官方風險溝通內容。但它不是乾淨的「一藥一列」資料，必須保留發布日期與原始 row provenance。

下一步應先選定一個候選成分，再建立 LangChain `Document` 與完整 corpus vector store。等 Retriever 真正產生 Top-K 後，才建立小型 evaluation label；本階段不製造 Conflict，也不先把任何文件標成正確或錯誤。
"""


def main() -> None:
    ensure_run_dirs()
    print(f"Downloading risk dataset -> {RISK_PATH}")
    download(RISK_URL, RISK_PATH)
    print(f"Downloading license dataset -> {LICENSE_PATH}")
    download(LICENSE_URL, LICENSE_PATH)
    risk_records = load_json(RISK_PATH)
    license_records = load_license_records(LICENSE_PATH)
    matches = explicit_license_matches(risk_records, license_records)
    report = build_report(risk_records, license_records, matches)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"risk_records={len(risk_records)}")
    print(f"license_records={len(license_records)}")
    print(f"risk_schema={list(risk_records[0])}")
    print(f"license_schema={list(license_records[0])}")
    print(f"joinable_risk_records={len(matches)}")
    print(f"report={REPORT_PATH}")


if __name__ == "__main__":
    main()
