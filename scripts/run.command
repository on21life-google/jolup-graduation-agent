#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ ! -x .venv/bin/python ]]; then
  PYTHON_COMMAND=""
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info[:2] == (3, 12)' >/dev/null 2>&1; then
      PYTHON_COMMAND="$candidate"
      break
    fi
  done
  if [[ -z "$PYTHON_COMMAND" ]]; then
    echo "Python 3.12가 필요합니다. docs/RUN.md의 설치 안내를 확인해 주세요."
    exit 1
  fi
  "$PYTHON_COMMAND" -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12)' >/dev/null 2>&1; then
  echo "이 폴더의 .venv는 Python 3.12 환경이 아닙니다. 새 폴더에 공유 ZIP을 풀고 다시 실행해 주세요."
  exit 1
fi

REQUIREMENTS_FILE="requirements.lock"
[[ -f "$REQUIREMENTS_FILE" ]] || REQUIREMENTS_FILE="requirements.txt"
REQUIREMENTS_HASH="$(.venv/bin/python -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$REQUIREMENTS_FILE")"
INSTALLED_HASH=""
[[ ! -f .venv/.jolup_requirements_sha256 ]] || INSTALLED_HASH="$(cat .venv/.jolup_requirements_sha256)"
if [[ "$REQUIREMENTS_HASH" != "$INSTALLED_HASH" ]]; then
  echo "실행에 필요한 라이브러리를 설치합니다. 최초 실행에는 인터넷이 필요합니다."
  .venv/bin/python -m pip install --disable-pip-version-check --cache-dir .cache/pip -r "$REQUIREMENTS_FILE"
  printf '%s\n' "$REQUIREMENTS_HASH" > .venv/.jolup_requirements_sha256
fi

echo "브라우저 주소: http://127.0.0.1:8501"
echo "종료하려면 이 창에서 Control+C를 누르세요."
exec .venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.maxUploadSize=2 --browser.gatherUsageStats=false
