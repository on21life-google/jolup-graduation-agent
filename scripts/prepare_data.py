"""Public rules and teaching fixtures. Never reads a student's upload."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def save(path, obj):
    target = DATA / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


GENERAL_CORE_COURSES = [
    ("GE001", "기독교로의초대", 3, [1], ["현대인과기독교"]),
    ("GE002", "신앙으로의초대", 3, [2], ["현대인과성경"]),
    ("GE003", "영어커뮤니케이션(1)", 2, [1], []),
    ("GE004", "영어커뮤니케이션(2)", 2, [2], []),
    ("GE005", "기초글쓰기", 2, [1, 2], []),
    ("GE006", "대학생활과진로", 1, [1], []),
    ("GE007", "사회봉사", 1, [1, 2], []),
]

def _write_sources():
    sources = [
        {"source_id": "school_rules", "file_name": "(2-1-1)성결대학교 학칙20260506.pdf", "path": "data/sources/(2-1-1)성결대학교 학칙20260506.pdf", "url": "https://www.sungkyul.ac.kr/skukr/253/subview.do", "registered_date": None, "date_note": "파일명의 20260506은 등록일로 단정하지 않음. 사용자가 제공한 원본.", "priority": 80},
        {"source_id": "course_guide", "file_name": "2026-2학기 강의시간표 안내문.pdf", "path": "data/sources/2026-2학기 강의시간표 안내문.pdf", "url": "https://www.sungkyul.ac.kr/bbs/skukr/96/26953/download.do", "registered_date": "2026-08-07", "date_note": "학사 공지 게시글 등록일. PDF 내부 작성일 아님.", "priority": 90},
        {"source_id": "major_notice", "file_name": "컴퓨터공학과_졸업요건변경_20250120_사실정리.txt", "path": "data/sources/컴퓨터공학과_졸업요건변경_20250120_사실정리.txt", "url": "https://www.sungkyul.ac.kr/bbs/computer/1372/37225/artclView.do", "registered_date": "2025-01-20", "priority": 100, "kind": "official_facts", "date_note": "학과 공지의 수치와 적용 조건을 정리. 원문은 URL 참조."},
    ]
    notice = """공식 공지 사실 정리 — 원문 자체가 아닌 검증된 수치와 조건의 정리
컴퓨터공학과 졸업 최소 이수 요구학점 변경건 (2025.01 이후)
등록일 2025-01-20. 2025년 2월 졸업생부터 재학생에게 소급 적용.
2024학번: 졸업 총 123학점, 교양필수 14학점, 교양선택 20학점, 주전공 42학점, 전공심화 추가 21학점. 전공심화 선택자는 전공 총 63학점.
전공필수는 6학점: 전공종합설계(1) 3학점과 전공종합설계(2) 3학점. 두 과목 이수와 졸업작품 심사 통과를 확인.
전공필수 최소학점이 24에서 6으로 변경됨. 예전 전필로 표시된 추가 학점도 전공 총합에 포함.
예: 전필 39학점 + 전선 3학점 = 주전공 42학점. 표의 전선 36학점만 보고 추가 33학점을 요구하지 않음.
전공심화 21학점은 주전공 42학점 외에 추가. 동일 학점을 두 번 더하지 않음.
데이터구조, 데이터베이스, 소프트웨어공학, 컴퓨터알고리즘, 운영체제는 권장필수이며 두 설계 과목과 구별.
"""
    (DATA / sources[2]["path"].removeprefix("data/")).write_text(notice, encoding="utf-8")
    for i in range(1, 5):
        sources.append({"source_id": f"curriculum_year{i}", "file_name": f"컴퓨터공학과_{i}학년_교육과정_사실정리.txt", "path": f"data/sources/컴퓨터공학과_{i}학년_교육과정_사실정리.txt", "url": f"https://www.sungkyul.ac.kr/computer/{9779+i}/subview.do", "registered_date": None, "priority": 70, "kind": "official_facts", "date_note": "게시일 미표시. 2026-10-22 열람. 미래 학기 실제 개설표가 아님."})
    for source in sources:
        source.update({"school": "성결대학교", "retrieved_date": "2026-10-22", "applies_to": {"department": "컴퓨터공학과", "entry_year": 2024}})
    save("sources.json", {"sources": sources})
    return sources


def _write_rules(cores):
    rules = {
        "domain_id": "sungkyul-computer-2024", "version": "2026-10-22.1",
        "school": "성결대학교", "department": "컴퓨터공학과", "entry_year": 2024, "track": "전공심화", "enrollment_type": "신입학", "engineering_accreditation": False,
        "total_credits": 123, "general_required": 14, "general_elective": 20, "major_base": 42, "major_depth": 21, "major_total": 63, "major_required": 6,
        "registered_semesters": 8, "chapel_semesters": 6, "term_limit": 18, "year_limit": 36, "summer_limit": 6, "term_min_regular": 15, "term_min_senior": 9, "academic_warning_limit": 15,
        "overload": {"extra": 3, "previous_earned_min": 15, "previous_gpa_min": 4.2},
        "general_required_courses": [{"course_id": c[0], "name": c[1], "credits": c[2], "aliases": c[4]} for c in cores],
        "major_required_courses": [{"course_id": "CS_CAP1", "name": "전공종합설계(1)", "credits": 3, "aliases": []}, {"course_id": "CS_CAP2", "name": "전공종합설계(2)", "credits": 3, "aliases": []}],
        "graduation_project_required": True,
        "notes": ["공학인증은 사용자가 지정한 프로젝트 범위에서 제외.", "기타 26학점은 합계의 잔여분이며 일선 26학점 필수 규칙이 아님.", "전공필수 초과학점은 전공 총합에 인정. 전선 36학점 독립 부족 판정 금지.", "컴공 2021학번 이후 교필 14학점. 코딩기초/AI의이해와활용 제외.", "공식 졸업 확정은 학교 졸업사정회를 거쳐야 함.", "대체 과목의 인정은 승인 증빙이 있는 경우에 한함. 본 샘플은 현재 명칭 사용."],
        "citations": {"credits": [{"source_id": "course_guide", "page": 24}, {"source_id": "school_rules", "page": 6, "section": "제35조"}], "general": [{"source_id": "course_guide", "page": 17}, {"source_id": "course_guide", "page": 28}], "major": [{"source_id": "major_notice", "page": None}, {"source_id": "course_guide", "page": 29}], "noncredit": [{"source_id": "school_rules", "page": 9, "section": "제50조"}, {"source_id": "major_notice", "page": None}], "limits": [{"source_id": "school_rules", "page": 7, "section": "제40조"}, {"source_id": "course_guide", "page": 7}, {"source_id": "course_guide", "page": 8}], "grades": [{"source_id": "school_rules", "page": 8, "section": "제44조"}], "retakes": [{"source_id": "course_guide", "page": 9}, {"source_id": "course_guide", "page": 29}]}}
    save("rules/sungkyul_cs_2024.json", rules)


def _major_course_id(name, count):
    return {"전공종합설계(1)": "CS_CAP1", "전공종합설계(2)": "CS_CAP2"}.get(name, f"CS{count:03}")


def _add_major(course, catalog, cid, name, term, year):
    cat = "전필" if cid.startswith("CS_CAP") else "전선"
    course(cid, name, 3, cat, [term], year, [f"curriculum_year{year}"])
    if cid == "CS_CAP2":
        catalog[-1]["prerequisites"] = ["CS_CAP1"]
        catalog[-1]["prerequisite_note"] = "설계(1)→설계(2)의 이수 순서를 보수적으로 적용한 계획 정책. 공식 수강 제한 확정은 학과 확인 필요."
    return f"{term}학기 / {cat} / {name} / 3학점"


def _write_catalog(cores):
    catalog = []
    def course(cid, name, credits, cat, semesters, year, sources, **extra):
        catalog.append(dict(course_id=cid, course_name=name, credits=credits, category=cat, offered_semesters=semesters, recommended_year=year, source_ids=sources, offering_status="교육과정상 후보 · 실제 개설 미확인", course_id_note="프로젝트 내부 식별자이며 학교 학수번호가 아님", prerequisites=[], **extra))
    for cid, name, cr, sem, aliases in cores:
        course(cid, name, cr, "교필", sem, 1, ["course_guide"], aliases=aliases)
    majors = {
        1: {1: ["컴퓨터공학입문", "C프로그래밍"], 2: ["창의적공학설계", "C프로그래밍응용", "파이썬프로그래밍", "전산수학"]},
        2: {1: ["데이터구조", "유닉스와리눅스", "자바프로그래밍", "윈도우프로그래밍", "선형대수"], 2: ["논리회로", "컴퓨터네트워크", "데이터베이스", "웹 표준기술", "자바프로그래밍응용", "확률통계"]},
        3: {1: ["소프트웨어공학", "인공지능", "컴퓨터알고리즘", "모바일프로그래밍", "웹응용기술"], 2: ["컴퓨터구조", "운영체제", "임베디드시스템", "전공종합설계(1)", "머신러닝"]},
        4: {1: ["고급네트워킹", "전공종합설계(2)", "텍스트마이닝"], 2: ["정보보안", "엔터프라이즈애플리케이션", "IT창업실습"]},
    }
    count = 0
    for year, terms in majors.items():
        lines = [f"컴퓨터공학과 {year}학년 교육과정 공개 페이지의 과목명·학점 사실 정리.", "실제 개설 학기, 시간, 여석, 수강 자격은 학기별 시간표에서 확인해야 함."]
        for term, names in terms.items():
            for name in names:
                count += 1
                cid = _major_course_id(name, count)
                lines.append(_add_major(course, catalog, cid, name, term, year))
        if year == 4:
            lines.append("2학기 / 졸업작품 / 0학점. 심사 통과는 별도 확인.")
        (DATA / f"sources/컴퓨터공학과_{year}학년_교육과정_사실정리.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for i, (name, sem) in enumerate([("건강한자아찾기", 1), ("과학과종교의대화", 1), ("범죄심리와교정교화", 1), ("생성형AI이해와윤리", 1), ("성과인간심리", 2), ("생태환경과윤리", 2), ("현대사회와범죄", 2), ("영화로만나는기독교", 2)]):
        course(f"GE_E{i+1:02}", name, 3, "교선", [sem], 1, ["course_guide"])
    save("courses.json", {"note": "학수번호는 내부 ID. 실제 시간표 미제공이므로 후보만 제시. 현장실습 등 별도 승인 과목은 자동 추천에서 제외.", "courses": catalog})


def _write_events(sources):
    event_rows = [('수강신청', '2026-08-18', '2026-08-20', 4), ('수강신청 변경', '2026-08-31', '2026-09-04', 4), ('대체과목 재수강 신청', '2026-09-07', '2026-09-11', 9), ('수강과목 철회 신청', '2026-09-14', '2026-09-18', 4), ('졸업논문·작품 제목 제출 마감', '2026-09-30', '2026-09-30', 4), ('조기졸업 신청', '2026-10-21', '2026-10-23', 4), ('전부과·복수전공·부전공 신청', '2026-10-26', '2026-10-30', 4), ('동계계절학기 예비수강 신청', '2026-11-02', '2026-11-04', 4), ('졸업유예 신청', '2026-12-02', '2026-12-04', 4), ('졸업논문·작품 제출 마감', '2026-12-04', '2026-12-04', 4), ('동계계절학기', '2026-12-21', '2027-01-12', 4), ('재학생 성적열람', '2026-12-28', '2026-12-28', 4), ('성적정정 신청', '2026-12-29', '2026-12-31', 4), ('2026학년도 전기 졸업사정회', '2027-01-28', '2027-01-28', 5), ('2027-1학기 예비수강 신청', '2027-02-01', '2027-02-03', 5), ('2027-1학기 수강신청', '2027-02-15', '2027-02-17', 5), ('2026학년도 전기 학위수여식', '2027-02-18', '2027-02-18', 5), ('2026-1학기 개강', '2026-03-03', '2026-03-03', 3), ('2026-1 수강신청 변경', '2026-03-04', '2026-03-09', 3), ('2026-1 수강과목 철회', '2026-03-17', '2026-03-23', 3), ('2026-1 졸업논문·작품 제목 제출', '2026-03-31', '2026-03-31', 3), ('2026-1 전부과·복수전공·부전공 신청', '2026-05-11', '2026-05-15', 3), ('2026-1 조기졸업 신청', '2026-05-13', '2026-05-15', 3), ('하계계절학기 예비수강 신청', '2026-05-18', '2026-05-20', 3), ('후기졸업 논문·작품 제출 마감', '2026-05-29', '2026-05-29', 3), ('2026-1 졸업유예 신청', '2026-06-08', '2026-06-10', 3), ('2026-1학기 종강', '2026-06-19', '2026-06-19', 3), ('하계계절학기', '2026-06-22', '2026-07-10', 3), ('2026-1 성적열람', '2026-07-01', '2026-07-01', 4), ('2026-1 성적정정', '2026-07-02', '2026-07-03', 4), ('2026-2 휴학·복학 신청', '2026-07-20', '2026-07-24', 4), ('2025학년도 후기 졸업사정회', '2026-07-30', '2026-07-30', 4), ('2026-2 예비수강 신청', '2026-08-10', '2026-08-12', 4), ('2026-2 등록금 납부', '2026-08-21', '2026-08-28', 4), ('2026-2학기 개강', '2026-08-31', '2026-08-31', 4), ('2026-2학기 종강', '2026-12-18', '2026-12-18', 4), ('2027-1 휴학·복학 신청', '2027-01-25', '2027-01-29', 5), ('2027-1 등록금 납부', '2027-02-19', '2027-02-26', 5)]
    save("events.json", {"events": [dict(id=f"EVENT_{i+1:02}" if i < 17 else f"ACADEMIC_{i-16:02}", title=n, start_date=s, end_date=e, source_id="course_guide", url=sources[1]["url"], file_name=sources[1]["file_name"], registered_date=sources[1]["registered_date"], page=p, note="학교 공지 변경 여부 확인. 목표 졸업(2028년 2월) 날짜와 혼동하지 않음.") for i, (n, s, e, p) in enumerate(event_rows)]})


def main():
    """Reproduce public teaching fixtures, optionally in an isolated directory."""
    global DATA
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DATA, help="Generated public fixture directory")
    DATA = parser.parse_args().output
    (DATA / "sources").mkdir(parents=True, exist_ok=True)
    sources = _write_sources()
    _write_rules(GENERAL_CORE_COURSES)
    _write_catalog(GENERAL_CORE_COURSES)
    _write_events(sources)


if __name__ == "__main__":
    main()
