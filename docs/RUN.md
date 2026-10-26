# 팀원 실행 안내

공유 ZIP을 풀고 `jolup_team` 폴더에서 실행합니다. GitHub 계정이나 Git 설치는 필요하지 않습니다. 기본 대상은 **2024학번 성결대학교 컴퓨터공학과·전공심화·공학인증 제외**이며, 제공 성적표는 모두 가상 자료입니다.

2026-10-23 사용자 결정으로 **팀원 Windows 환경 검증은 제외**합니다. 아래 Windows 명령은 기존 참고 자료이며 이번 완료 기준이 아닙니다. 시연 영상은 만들지 않습니다. 교수 이론·실습 자료는 수령해 대조했습니다. 팀원 개별 변경 파일 병합과 최종 리허설은 자료를 받은 뒤 진행할 후속 작업입니다.

## 가장 쉬운 실행

처음 한 번은 **64비트 Python 3.12**와 인터넷 연결이 필요합니다. Python은 [공식 다운로드](https://www.python.org/downloads/)에서 설치합니다. Windows에서는 설치 시 `Add Python to PATH`를 선택합니다.

1. ZIP을 완전히 풀어 새 폴더에 둡니다. 압축 프로그램 안에서 실행하지 않습니다.
2. Mac: `scripts/run.command`를 실행합니다. Windows: `scripts/run.bat`를 실행합니다.
3. 설치가 끝나면 [http://127.0.0.1:8501](http://127.0.0.1:8501)을 엽니다.
4. 화면에서 가상 사례를 선택하고 **나 졸업 괜찮아?**를 입력합니다.
5. 종료하려면 실행 창에서 **Control+C / Ctrl+C**를 누릅니다. 브라우저 창만 닫으면 서버는 계속 실행됩니다.

실행 스크립트가 프로젝트 안에 `.venv`를 만들고, `requirements.lock`에 고정한 라이브러리를 설치합니다. 라이브러리 목록이 같으면 다음 실행에서 설치를 반복하지 않습니다. 학교 규정 검색을 처음 실행할 때는 공개 임베딩 모델을 내려받으므로 추가 시간이 걸릴 수 있습니다.

Mac에서 더블클릭 실행 권한 문제가 생기면, 터미널을 열어 프로젝트 폴더로 이동한 뒤 아래처럼 실행합니다. 경로에 한글이나 공백이 있으면 경로 전체를 따옴표로 감쌉니다.

```bash
cd "/공유 ZIP을 푼 경로/jolup_team"
bash scripts/run.command
```

Windows 명령 프롬프트에서 직접 실행하려면 다음 명령을 사용합니다.

```bat
cd /d "C:\공유 ZIP을 푼 경로\jolup_team"
scripts\run.bat
```

## API 키 연결

로컬 모드에서는 API 키가 없어도 이수 진단, 수강 계획, 로컬 규정 검색, 일정 미리보기·ICS 파일 만들기는 실행됩니다. 키를 연결하면 공통 AI 설명 인터페이스를 사용합니다. **계산·판정 표는 Python 계산 결과로 고정**됩니다.

화면의 가려진 API 키 입력란에 본인의 키를 넣습니다. 알려진 OpenAI/Anthropic 키 형식은 자동 판별하며, 형식이 모호하면 공급자를 직접 선택합니다. 키를 바꿀 때 계산 코드나 화면 코드를 수정할 필요가 없습니다. 모델명이 필요한 경우에만 고급 설정에서 선택합니다.

기본 공유 ZIP에는 키를 넣지 않습니다. 사용자가 요청한 **별도 `.env` 전달**은 지원합니다. 앱의 생성 API 설정에 키와 모델을 입력한 뒤 **키 포함 .env 다운로드 준비 → .env 파일 다운로드**를 선택하면 실제 값이 포함된 파일을 받습니다. 이 파일은 의도한 팀원에게 별도로 전달하고 프로젝트 루트에 둡니다. LangSmith 키는 내보내기에 포함하지 않습니다.

일반 로컬 실행은 `.env`를 자동으로 읽지 않습니다. 로컬 앱에서는 받은 파일의 생성 API 설정을 화면에 입력하고, Docker Compose에서는 루트의 `.env`를 읽어 컨테이너에 전달합니다. 파일 전달만으로 일반 Python 실행에 설정이 적용됐다고 간주하지 않습니다. `.env.example`은 변수 이름 참고용입니다. 실제 API 검증 여부는 [검증결과](검증결과.md)에서 별도로 확인합니다.

## 수동 설치와 검증

학습 기록은 키 없이 **대화로 확인 → 새 질문 → 실행 과정 살펴보기**에서 확인합니다. [학습 가이드](학습_가이드.md)와 [LangSmith 연결 안내](LANGSMITH.md)를 함께 읽으세요. LangSmith 외부 전송에는 OpenAI 키와 별도로 발급한 LangSmith 키가 필요합니다.

새 코드를 받아도 기존 서버에 이전 모듈이 남아 있다면 서버를 종료하고 다시 실행합니다. 종료하면 메모리의 대화와 키도 사라지므로 새 세션에서 다시 입력합니다. 기본 포트는 8501이며, 10/23 개발 검증 중에는 기존 세션을 보존하고 최신 코드를 8502에서 별도로 실행했습니다.

Mac/Linux 터미널:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pytest -q
.venv/bin/python scripts/build_index.py --query "전공심화 졸업학점은?"
.venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
```

Windows 명령 프롬프트:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\build_index.py --query "전공심화 졸업학점은?"
.venv\Scripts\python.exe -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
```

`build_index.py`는 공개 학교 자료만 처리합니다. 벡터 검색 구성이 준비되지 않으면 종료 코드 `2`로 실패를 표시합니다. `--lexical` 옵션은 별도의 키워드 검색 시험 모드이며 벡터 검색 성공으로 간주하지 않습니다.

## Docker로 앱·API·MCP·Chroma를 함께 실행

[Docker Desktop](https://docs.docker.com/desktop/)이 이미 설치된 팀원은 Python 대신 이 방법을 사용할 수 있습니다. Docker Desktop을 먼저 실행한 뒤 프로젝트 폴더의 터미널에서 실행합니다.

```bash
docker compose up --build -d
docker compose ps
```

화면은 [http://127.0.0.1:8501](http://127.0.0.1:8501), Swagger는 [http://127.0.0.1:8001/docs](http://127.0.0.1:8001/docs)에서 엽니다. Compose의 기본 4서비스는 `app → api → mcp → chroma`입니다. 채팅은 `JOLUP_AGENT_API_URL=http://api:8001`의 `/chat`으로 전달합니다. API와 일부 별도 화면의 도메인 호출은 내부 MCP를 사용하며 MCP는 Chroma를 사용합니다. UI/API 포트는 호스트의 127.0.0.1에만 열고 MCP/Chroma는 내부 전용입니다. 선택적 study profile을 켤 필요가 없습니다.

Compose의 HTTP 채팅은 외부 LLM과 LangSmith 전송을 사용하지 않습니다. 생성 키·LangSmith 키를 API로 보내지 않으며 실행 단계만 화면에서 확인합니다. 기존 공통 생성 AI와 더미 기록 전송을 실습하려면 아래 로컬 모드를 사용합니다. Compose가 앱 컨테이너에 전달하는 JOLUP 모델 설정이 HTTP Agent의 LLM을 켜는 것은 아닙니다. 빌드 이미지에는 `.env`를 복사하지 않습니다.

동작 확인과 종료:

```bash
docker compose exec mcp python scripts/build_index.py --query "채플은 몇 학기야?"
docker compose stop
```

공개 학교 자료 색인과 임베딩 모델은 이름 있는 Docker 볼륨에 남아 재사용됩니다. 업로드한 성적표와 API 키는 이 볼륨에 저장하지 않습니다. 컨테이너를 새로 만들어도 세션의 입력 성적표·대화·승인은 다시 입력해야 합니다. Chroma 저장 위치는 공식 Docker 지침의 `/data`를 사용합니다. [Chroma Docker 문서](https://docs.trychroma.com/deployment/docker)

Docker 실제 실행 검증 여부는 프로젝트 검증 보고서를 확인합니다. 설정 파일이 존재하는 것만으로 해당 팀원 PC의 실행 성공을 보장하지는 않습니다.

### 실행 준비 검증 상태 · 2026-10-22

아래 표는 **2026-10-22 당시 버전의 역사적 실행 기록**입니다. 현재 테스트 수·커버리지·최신 Docker 실행 여부는 [검증결과](검증결과.md)를 기준으로 확인합니다. 이 표의 성공을 변경 후 코드의 재검증으로 해석하지 않습니다.

| 확인 항목 | 상태 |
|---|---|
| 핵심 계산·수강 계획·자료 적용 범위 테스트 | 69개 통과 |
| 전체 자동 검증 | 237개 통과 · 실패/건너뛰기 없음 |
| Mac 실행 스크립트 | Bash 문법 검증 완료 · 스크립트 전체 실행은 별도 확인 필요 |
| Windows 실행 스크립트 | 작성 당시 미검증 · 현재 사용자 결정으로 검증 범위 제외 |
| Compose 설정 | `docker compose config --quiet` 통과 |
| Docker 엔진 | 읽기 전용 `/_ping` 요청에서 HTTP 200 응답 확인 |
| Docker 이미지 빌드·3서비스 통합 실행 | Linux arm64 이미지 빌드·3서비스 실행 및 실제 HTTP MCP 연동 통과 |
| MCP→Chroma 규정 검색 | 공개 자료 7개·208개 조각의 실제 벡터 색인/검색 통과 |
| Docker 최종 코드 동기화 | 최신 app.py·llm.py 포함 코드 해시 8개 일치, MCP 서버 해시도 일치 |
| Docker 화면 통합 시험 | 실제 MCP HTTP를 사용한 Streamlit 화면 시험 통과 · 현재 90/예상 105 분리·4개 답변 영역·모델 조회 버튼 확인 |
| 검증용 Docker 종료 | jolup-codex-verify 3서비스만 중지 완료 · 컨테이너/공개 자료 볼륨 유지 |
| 공유 ZIP 준비 | 필수 파일·허용 목록 검사 통과 · 최종 문서 반영 후 생성 |

통합 시험에서는 더미 학생 6종의 진단, 정상/부족 수강 계획, 승인 없는 ICS 생성 차단, 승인 후 ICS, 실제 Chroma 검색을 확인했습니다. 최종 API 화면을 반영한 앱과 MCP 서버의 코드 해시를 대조했으며, 벡터 DB에 공개 학교 자료 7종만 존재하는 것도 확인했습니다. Docker 시험은 Linux arm64 환경에서 수행했습니다.

## 로컬과 HTTP를 비교하는 실행 실습

기존 한 프로세스 실행은 `JOLUP_AGENT_API_URL`을 설정하지 않고 Streamlit을 시작하면 된다. 이 모드에서는 기존 AI 설명과 선택적 LangSmith 내보내기를 사용할 수 있다.

HTTP 경계를 공부하려면 서로 다른 터미널에서 다음을 실행한다. 일반 Python 실행은 `.env`를 자동으로 읽지 않으므로 주소를 명시한다.

```bash
# 터미널 1: 로컬 Agent API
.venv/bin/python -m jolup.study_api --port 8001

# 터미널 2: Streamlit이 위 API를 사용
JOLUP_AGENT_API_URL=http://127.0.0.1:8001 .venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
```

새 질문에서 서버 세션 ID를 받고 이후 질문에 같은 ID를 사용한다. 세션마다 최근 4턴만 보관하며 API 전체는 최대 64세션이다. 학생 자료나 학교 근거가 바뀌면 해당 기억을 무효화한다. 사례/프로필/성적표 변경과 세션 초기화 시 서버의 `DELETE /sessions/{session_id}`를 호출한다. 삭제 실패는 표시하고 이전 ID는 재사용하지 않는다. 서버 재시작·오래 사용하지 않은 세션의 제거로 기억이 사라질 수 있다. 영구 학생 DB는 추가하지 않았다.

[Postman 실측 보고서 · 공개 요약](검증결과.md)는 로컬 API의 21요청·60검증 통과 기록이다. 이 실측은 lexical 검색·AI off·일정 승인 false로 수행했으며 Docker 새 4서비스의 기동 성공을 의미하지 않는다. 최신 Docker 실행 상태는 [검증결과](검증결과.md)를 따른다. 공유 ZIP에서 reports가 제외되면 로컬 실행으로 다시 확인한다.

## GitHub 없이 팀원 작업 합치기

1. 각자 받은 폴더의 복사본에서 담당 파일만 수정합니다. 맡은 파일 경계는 `docs/INTERFACES.md`를 따릅니다.
2. 전달할 때 **수정 파일·변경 이유·실행 명령·결과·남은 문제**를 간단히 적습니다. 서로의 `.venv`, `.cache`, 실행 로그는 보내지 않습니다. `.env`는 기본 ZIP에서 제외하며, 사용자가 요청한 실제 값의 별도 전달만 예외로 구분합니다.
3. 취합 담당자가 기존 폴더를 복사해 백업하고, 팀원 변경 파일을 새 통합 폴더에 반영합니다. 같은 파일을 두 사람이 수정했다면 통째로 덮어쓰지 말고 변경 부분을 비교합니다.
4. 통합 폴더에서 `python -m pytest -q`를 실행합니다. 정상/부족 사례의 표와 근거가 기대 정답과 일치하는지도 화면에서 확인합니다.
5. 확인 후 다음 명령으로 공유 ZIP을 만듭니다. 아래 `python`은 프로젝트 `.venv`의 Python으로 바꿔 실행할 수 있습니다.

```bash
python scripts/package_project.py --check
python scripts/package_project.py
```

ZIP은 `output/졸업로드맵_팀공유_날짜시간.zip`에 생성됩니다. 패키징은 허용 목록에 있는 코드·공개 학교 자료·더미 자료·테스트·문서만 포함하고, 파일별 SHA-256과 ZIP 무결성을 확인합니다. `.env`, `.venv`, `.cache`, `.build`, 로그, 색인·모델, 기존 결과 ZIP, 검증 보고서 디렉터리는 포함하지 않습니다. `MANIFEST.json`에서 포함 파일과 해시를 확인할 수 있습니다.

교수 이론·PPT 양식·제공 실습 코드의 대조는 [수업자료 반영표](수업자료_반영대조표.md)에 있습니다. 아직 받지 않은 팀원 개별 파일의 병합은 완료로 표시하지 않습니다. 자료 수령 후 변경 목록을 대조하고 같은 계약 테스트로 통합 결과를 확인합니다. 현재 코드 개선 완료와 후속 팀 통합 완료는 별개입니다.

## 자주 만나는 문제

| 현상 | 확인할 점 |
|---|---|
| Python을 찾을 수 없음 | 64비트 Python 3.12 설치 후 실행 창을 새로 엽니다. Windows는 `py -3.12 --version`을 확인합니다. |
| 설치 중 다운로드 실패 | 인터넷/학교 네트워크를 확인하고 같은 실행 파일을 다시 실행합니다. 실패 내용을 팀에 전달합니다. |
| 8501 포트 사용 중 | 이전 실행 창에서 Ctrl+C를 누르거나 Docker 실행을 `docker compose stop`으로 종료합니다. |
| 규정 검색이 키워드 모드/준비 안 됨 | 화면 상태와 `build_index.py` 결과를 확인합니다. 모델 다운로드 및 Chroma 연결이 성공했는지 확인합니다. |
| AI 설명 없음 | HTTP 채팅은 AI 설명을 지원하지 않습니다. 로컬 모드라면 명시적 사용 선택·키·공급자·모델 권한을 확인합니다. |
| 다른 PC에서 `.venv` 실행 불가 | `.venv`는 복사하지 않습니다. 공유 ZIP을 새로 풀고 해당 PC에서 설치합니다. |
| 미래 시간표나 날짜를 확정할 수 없음 | 제공 자료에 없는 정보입니다. 후보·확인 필요 표시를 유지하고 학교의 해당 학기 공지를 보완합니다. |

## 전체 기능을 테스트부터 공부하기

설치가 끝난 프로젝트 폴더에서 실행합니다. 앱 서버나 API 키가 없어도 학습할 수 있습니다. [TDD 학습 가이드](TDD_학습가이드.md)가 기대 결과, 테스트 함수, 실제 코드의 읽는 순서를 안내합니다.

Mac/Linux:

```bash
.venv/bin/python scripts/tdd_study.py list
.venv/bin/python scripts/tdd_study.py show REQ-008
.venv/bin/python scripts/tdd_study.py run REQ-008
.venv/bin/python scripts/tdd_study.py replay
```

Windows 명령 프롬프트:

```bat
.venv\Scripts\python.exe scripts\tdd_study.py list
.venv\Scripts\python.exe scripts\tdd_study.py show REQ-008
.venv\Scripts\python.exe scripts\tdd_study.py run REQ-008
.venv\Scripts\python.exe scripts\tdd_study.py replay
```

`replay`는 동일한 새 계약 검사 21개로 미구현(Red)의 의도된 실패와 Green·Refactor의 통과를 확인합니다. 기존 앱의 과거 개발 이력을 재현한 기록은 아닙니다. `run --all`은 추적표에 연결된 기존 시험을 실행하며, 공유본 패키징인 REQ-024는 별도로 `python scripts/package_project.py --check`로 확인합니다. 전체 기존 자동 시험은 `python -m pytest -q`입니다.

기본 학습 검색은 어휘 검색입니다. 실제 임베딩·로컬 Chroma까지 시험하려면 `run --all --vector`를 사용합니다. 첫 벡터 실행은 모델 다운로드가 필요할 수 있습니다. 모델 응답과 LangSmith 전송은 모의 호출이며, 실제 키로 외부 서비스를 다시 호출하지 않습니다. 결과는 실행마다 `reports/tdd_study/날짜_시간/summary.json`과 XML·로그에 저장합니다. 전체 연결 시험 중 MCP HTTP 항목은 로컬 임시 포트를 사용하므로 포트를 금지한 실행 환경에서는 별도 오류가 날 수 있습니다.
