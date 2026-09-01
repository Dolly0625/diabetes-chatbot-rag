#!/usr/bin/env bash
# 初始化 rag_retrieval repo 的骨架。
# 可重複執行：絕不會覆蓋已存在的檔案。
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '  %s\n' "$*"; }
keep() { mkdir -p "$(dirname "$1")"; [ -f "$1" ] || { : > "$1"; say "created $1"; }; }

echo "== directories =="
mkdir -p src/rag_retrieval/{contract,retrievers,data} tests eval scripts
for f in \
  src/rag_retrieval/__init__.py \
  src/rag_retrieval/contract/__init__.py \
  src/rag_retrieval/contract/models.py \
  src/rag_retrieval/contract/enums.py \
  src/rag_retrieval/contract/errors.py \
  src/rag_retrieval/gate_in.py \
  src/rag_retrieval/routing.py \
  src/rag_retrieval/retrievers/__init__.py \
  src/rag_retrieval/retrievers/base.py \
  src/rag_retrieval/retrievers/vector.py \
  src/rag_retrieval/retrievers/graph.py \
  src/rag_retrieval/fusion.py \
  src/rag_retrieval/risk.py \
  src/rag_retrieval/gate_out.py \
  src/rag_retrieval/loaders.py \
  src/rag_retrieval/embedding.py \
  src/rag_retrieval/tool.py \
  tests/__init__.py \
  scripts/build_index.py \
  eval/run_eval.py
do keep "$f"; done

echo "== .gitignore =="
if [ ! -f .gitignore ]; then
cat > .gitignore <<'EOF'
# secrets — never commit
.env
.env.*
*.key

# python
__pycache__/
*.py[cod]
.venv/
venv/
*.egg-info/
build/
dist/
.pytest_cache/

# large source material (regenerate with pipelines/corpus_ingest)
*.pdf

# macOS
.DS_Store
EOF
say "created .gitignore"
fi

echo "== git =="
if [ ! -d .git ]; then
  git init -q
  git checkout -q -b main 2>/dev/null || true
  say "git initialized on main"
else
  say "git already initialized"
fi

echo
echo "Next:"
echo "  1. Read CLAUDE.md"
echo "  2. Build step 1 (contract/) and verify against ../02_MS2_demo/contract/examples/"
echo "  3. Confirm no secret is staged:  git log -p | grep -i 'api.*key'  (expect nothing)"
echo "  4. git remote add origin <lab GitLab URL>"
