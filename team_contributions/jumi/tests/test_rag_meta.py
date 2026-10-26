from rag.answer_generator import collect_sources, format_source
from rag.config import classify_doc_type
from rag.document_loader import get_file_meta, normalize_text


def test_classify_doc_type():

    assert classify_doc_type("(2-1-1)성결대학교 학칙20260506.pdf") == "regulation"
    assert classify_doc_type("2026-2학기 강의시간표 안내문.pdf") == "course"
    assert classify_doc_type("2026학년도 학사일정.pdf") == "schedule"
    assert classify_doc_type("컴퓨터공학과 교육과정 요람.pdf") == "curriculum"


def test_file_meta_uses_date_in_file_name(tmp_path):

    pdf = tmp_path / "학칙20260506.pdf"
    pdf.write_bytes(b"")

    meta = get_file_meta(pdf)

    assert meta["source"] == "학칙20260506.pdf"
    assert meta["written_at"] == "2026-05-06"
    assert meta["doc_type"] == "regulation"


def test_written_date_is_none_without_file_date_or_pdf_metadata(tmp_path):

    pdf = tmp_path / "강의시간표 안내문.pdf"
    pdf.write_bytes(b"")

    # 업로드 시각(파일 수정일)으로 채우지 않는다.
    assert get_file_meta(pdf)["written_at"] is None


def test_format_source_shows_page_and_hides_missing_date():

    item = {"source": "안내문.pdf", "page": 4, "section": None, "written_at": None}
    assert format_source(item) == "안내문.pdf / 4페이지"

    item = {"source": "학칙.pdf", "page": 9, "section": "제50조", "written_at": "2026-05-06"}
    assert format_source(item) == "학칙.pdf / 9페이지 / 제50조 / 작성일 2026-05-06"


def test_normalize_text_joins_decomposed_hangul():

    decomposed = "학칙"  # NFD "학칙"
    assert normalize_text(decomposed) == "학칙"


def test_collect_sources_removes_duplicates():

    item = {
        "source": "학칙.pdf", "page": 3, "section": "제12조(졸업)",
        "location": "data/학칙.pdf", "written_at": "2026-05-06",
    }

    assert len(collect_sources([item, dict(item)])) == 1
