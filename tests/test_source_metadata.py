"""Team RAG contributions: normalization, honest dates and traceable source labels.

The submitted implementation package was missing; these tests independently
adapt its metadata expectations to the existing source and token contracts.
"""
import hashlib
import json
from unicodedata import normalize

import pytest

from jolup.rag import KnowledgeBase, _compact, _terms


def school_manifest(tmp_path, text=None):
    sources = tmp_path / "sources"
    sources.mkdir()
    text = text or "제50조(졸업)\n2019학번부터 123학점 졸업 요건."
    document = sources / "rules.json"
    document.write_text(json.dumps({"pages": [{"page": 9, "text": text}]}, ensure_ascii=False))
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": [{
        "source_id": "school_rules", "path": "sources/rules.json",
        "file_name": "학칙20260506.pdf", "registered_date": None,
        "date_note": "파일명 날짜는 등록일로 단정하지 않음.",
    }]}, ensure_ascii=False))
    return path, document, text


def test_decomposed_korean_has_the_same_topics_and_terms():
    query = "졸업학점은 몇 학점이야?"
    decomposed = normalize("NFD", query)
    assert KnowledgeBase._topics(decomposed) == ["total"]
    assert _compact(decomposed) == _compact(query)
    assert _terms(decomposed) == _terms(query)


def test_decomposed_question_retrieves_the_same_answer_and_original_text(tmp_path):
    path, _, original = school_manifest(tmp_path)
    kb = KnowledgeBase(path, mode="lexical")
    normal = kb.answer("졸업학점은 몇 학점이야?")
    decomposed = kb.answer(normalize("NFD", "졸업학점은 몇 학점이야?"))
    assert normal["status"] == decomposed["status"] == "근거 확인"
    assert normal["answer"] == decomposed["answer"]
    assert normal["evidence"] == decomposed["evidence"]
    assert decomposed["evidence"][0]["context_text"] == original


@pytest.mark.parametrize("query", ["경영학과 졸업학점", "서울대학교 졸업학점"])
def test_decomposed_scope_names_do_not_bypass_scope_checks(query):
    assert KnowledgeBase._query_scope_error(normalize("NFD", query)) == KnowledgeBase._query_scope_error(query)


@pytest.mark.parametrize("filename,expected", [
    ("(2-1-1)성결대학교 학칙20260506.pdf", "regulation"),
    ("2026-2학기 강의시간표 안내문.pdf", "course"),
    ("2026학년도 학사일정.pdf", "schedule"),
    ("컴퓨터공학과 교육과정 요람.pdf", "curriculum"),
    ("컴퓨터공학과_졸업요건변경_20250120_사실정리.txt", "regulation"),
    ("안내.txt", "other"),
])
def test_document_type_is_distinct_from_original_or_summary_kind(filename, expected):
    from jolup.source_metadata import classify_document_type
    assert classify_document_type(filename) == expected
    assert classify_document_type(normalize("NFD", filename)) == expected


@pytest.mark.parametrize("filename,expected", [
    ("학칙20260506.pdf", "2026-05-06"),
    ("안내_2026-10-26.pdf", "2026-10-26"),
    ("안내_2026.10.26.pdf", "2026-10-26"),
    ("학칙20260230.pdf", None),
    ("학칙20261301.pdf", None),
    ("2026-2학기 강의시간표 안내문.pdf", None),
    ("학칙1202605069.pdf", None),
])
def test_filename_date_is_only_a_valid_version_hint(filename, expected):
    from jolup.source_metadata import filename_date_hint
    assert filename_date_hint(filename) == expected


@pytest.mark.parametrize("text,expected", [
    ("제12조(졸업)\n졸업 요건", "제12조(졸업)"),
    ("  제50조(졸업) 졸업에 필요한 학점", "제50조(졸업)"),
    ("제12조의2(등록)\n신청", "제12조의2(등록)"),
    ("제50조\n졸업", "제50조"),
    ("이 학칙 제10조에 따라 처리한다.", None),
    ("제10조에 따라 처리한다.", None),
    ("2026. 3. 2.(월) 개강", None),
    ("제1조(총칙)\n본문\n제2조(학과)\n본문", None),
])
def test_section_label_is_an_unambiguous_line_heading(text, expected):
    from jolup.source_metadata import extract_section
    assert extract_section(text) == expected


def test_chunk_section_does_not_invent_a_heading_at_a_mid_line_split():
    from jolup.source_metadata import section_for_chunk
    page = "다른 조항은 제50조(졸업)에 따라 정한다.\n제51조(수료) 수료 기준"
    assert section_for_chunk(page, "제50조(졸업)에 따라 정한다.") is None
    assert section_for_chunk(page, "제51조(수료) 수료 기준") == "제51조(수료)"
    assert section_for_chunk(page, "일치하지 않는 텍스트") is None
    assert section_for_chunk("제1조(규정)\n본문", "본문") is None
    assert section_for_chunk("제1조(규정)\n제1조(규정)", "제1조(규정)") is None


def test_rag_exposes_metadata_and_actual_source_hash_without_promoting_dates(tmp_path):
    path, document, original = school_manifest(tmp_path)
    kb = KnowledgeBase(path, mode="lexical")
    result = kb.answer("졸업학점은 몇 학점인가요?")
    evidence = result["evidence"][0]
    meta = result["meta"][0]
    for item in (evidence, meta):
        assert item["document_type"] == "regulation"
        assert item["section"] == "제50조(졸업)"
        assert item["version_date_hint"] == "2026-05-06"
        assert item["registered_date"] is None
        assert item["date_note"] == "파일명 날짜는 등록일로 단정하지 않음."
    assert evidence["context_text"] == original
    stats = result["retrieval"]["sources"]
    assert len(stats) == 1
    assert stats[0]["source_id"] == "school_rules"
    assert stats[0]["sha256"] == hashlib.sha256(document.read_bytes()).hexdigest()
    assert stats[0]["chunk_count"] == len(kb.chunks)
    assert result["retrieval"]["model"] is None
    # Existing HTTP output contracts retain these optional metadata keys.
    from jolup.study_api import DomainResult
    assert DomainResult.model_validate(result).model_dump()["meta"] == result["meta"]


def test_raw_decomposed_source_text_stays_traceable(tmp_path):
    original = normalize("NFD", "제50조(졸업)\n2019학번부터 123학점 졸업 요건.")
    path, _, _ = school_manifest(tmp_path, original)
    kb = KnowledgeBase(path, mode="lexical")
    result = kb.answer("졸업학점은 몇 학점인가요?")
    assert result["status"] == "근거 확인"
    assert result["evidence"][0]["context_text"] == original
    assert result["evidence"][0]["text"] == original


def test_file_mtime_is_not_a_document_date(tmp_path):
    path, document, _ = school_manifest(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["sources"][0]["file_name"] = "학교 안내.json"
    path.write_text(json.dumps(manifest))
    kb = KnowledgeBase(path, mode="lexical")
    kb.build_index()
    assert document.stat().st_mtime > 0
    assert kb.chunks[0]["registered_date"] is None
    assert kb.chunks[0]["version_date_hint"] is None
