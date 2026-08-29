# Python 3.10 執行環境

本專案與 RAG 組的共同執行基準為 **CPython 3.10**，當前鎖定 `3.10.20`。

## 本機

```bash
uv python install 3.10.20
uv venv --python 3.10.20 .venv
source .venv/bin/activate
python -m pip install -r tfda_context_gate/requirements.txt
python -m pytest -q
```

`.python-version` 讓 `uv` / `pyenv` 在專案目錄選擇相同版本。Dockerfile 亦使用
`python:3.10.20-slim`，避免本機與容器產生 3.10/3.11 差異。

## 相容邊界

- 共用 schema 與 production code 不得使用 Python 3.11+ 專屬 API。
- Pydantic 使用 v2（`>=2.8,<3`）。
- 模型名稱與 provider 仍只能從 `.env` 讀取，Python 版本統一不改變此原則。
- 不得將 `.env`、`.env.bak`、SQLite session 或秘密帶入容器 image。

## 驗收

```bash
python --version                 # Python 3.10.x
python -m compileall -q tfda_context_gate line_bot
python -m pytest -q
docker build -t tfda-agent:py310 .
```
