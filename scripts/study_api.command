#!/bin/bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  echo "먼저 scripts/run.command로 프로젝트 의존성을 설치하세요."
  exit 1
fi
echo "Swagger: http://127.0.0.1:8001/docs"
echo "소스 변경 시 다시 불러옵니다. 종료: Control+C"
exec .venv/bin/python -m uvicorn jolup.study_api:app --host 127.0.0.1 --port 8001 --reload --reload-dir jolup
