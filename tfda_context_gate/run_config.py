from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


def configured_run_dir() -> Path:
    """Return the isolated output directory for the current experiment run."""
    raw = os.getenv("TFDA_RUN_DIR")
    if not raw:
        return ROOT
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


RUN_DIR = configured_run_dir()
DATA_DIR = RUN_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
REPORT_DIR = RUN_DIR / "reports"
RESULTS_DIR = RUN_DIR / "results"


def ensure_run_dirs() -> None:
    for directory in (RAW_DIR, PROCESSED_DIR, REPORT_DIR, RESULTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def load_dotenv_file(path: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    dotenv_path = path or PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return values
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_value(name: str, default: str | None = None) -> str | None:
    # 優先讓 .env 覆蓋已存在的環境變數，確保本地 .env 修改即生效
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    except ImportError:
        pass
    values = load_dotenv_file()
    return os.getenv(name) or values.get(name) or default


def relative_to_run(path: Path) -> str:
    try:
        return str(path.relative_to(RUN_DIR))
    except ValueError:
        return str(path)
