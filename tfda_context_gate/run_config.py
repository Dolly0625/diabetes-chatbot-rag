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
    # 保留 LINE 測試的 hermetic：load_dotenv 不得覆蓋測試設定的 LINE 相關 env
    _preserve_keys = (
        "LINE_CHANNEL_SECRET",
        "LINE_ALLOW_UNSIGNED_WEBHOOK",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_ACCESS_TOKEN",
        "LINE_CHANNEL_TOKEN",
        "LINE_IDENTITY_HASH_KEY",
        "LINE_SESSION_DB_PATH",
        "LINE_LOGIN_CHANNEL_ID",
        "LINE_LIFF_ID",
        "LINE_DEMO_MODE",
        "DEMO_CLINICIAN_IDS",
    )
    _saved = {k: os.getenv(k) for k in _preserve_keys}
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    except ImportError:
        pass
    finally:
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    values = load_dotenv_file()
    return os.getenv(name) or values.get(name) or default


def relative_to_run(path: Path) -> str:
    try:
        return str(path.relative_to(RUN_DIR))
    except ValueError:
        return str(path)
