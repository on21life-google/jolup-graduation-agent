"""RAG ingestion, cache rebuild and retrieval degradation contracts without network."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from jolup.rag import GroundedFact, KnowledgeBase, _chunks
from test_chunking import tokenizer


def manifest(tmp_path, records=None, content="졸업 123학점 원문"):
    (tmp_path / "sources").mkdir(exist_ok=True)
    document = tmp_path / "sources/public.txt"
    document.write_text(content)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(records if records is not None else [{"source_id": "public", "path": "sources/public.txt"}]))
    return path


def test_chunk_boundaries_retain_nonempty_text_and_bounded_progress():
    assert _chunks("") == []
    assert _chunks("a" * 15, size=6, overlap=2) == ["aaaaaa", "aaaaaa", "aaaaaaa"[:6], "aaa"]
    assert all(part.strip() for part in _chunks("abc" + "\n" * 50 + "xyz", size=10, overlap=2))
    parts = _chunks("alpha beta\nnext sentence. final text", size=20, overlap=3)
    assert parts[0] == "alpha beta" and parts[-1].endswith("final text")


def test_invalid_mode_and_manifest_shape_raise_clear_configuration_errors(tmp_path):
    with pytest.raises(ValueError, match="vector or lexical"):
        KnowledgeBase(mode="other")
    path = manifest(tmp_path)
    path.write_text('{"sources": {}}')
    with pytest.raises(ValueError, match="sources list"):
        KnowledgeBase(path, mode="lexical").build_index()


def test_missing_identity_missing_file_and_unreadable_source_are_disclosed(tmp_path):
    path = manifest(tmp_path, [
        {"path": "sources/public.txt"},
        {"source_id": "missing", "path": str(tmp_path / "sources/missing.txt")},
        {"source_id": "broken", "path": str(tmp_path / "sources/broken.json")},
    ])
    (tmp_path / "sources/broken.json").write_text('{"wrong":"schema"}')
    result = KnowledgeBase(path, mode="lexical").build_index()
    assert result["status"] == "unavailable"
    assert any("출처 ID" in warning for warning in result["warnings"])
    assert any("파일이 없어" in warning for warning in result["warnings"])
    assert any("읽기 실패 (ValueError)" in warning for warning in result["warnings"])


def test_blank_pdf_page_requests_ocr_without_fabricating_text(monkeypatch, tmp_path):
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda path: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: None)]))
    kb = KnowledgeBase(mode="lexical")
    assert kb._read_pages(tmp_path / "blank.pdf", {"source_id": "blank"}) == []
    assert "OCR 검토 필요" in kb.warnings[0]


def install_vector_stubs(monkeypatch, *, count=0, query_error=False, initialization_error=None):
    calls = []
    class Model:
        def __init__(self, **kwargs):
            if initialization_error is not None:
                raise initialization_error
            self.model = SimpleNamespace(tokenizer=tokenizer(128))
        def embed(self, texts, batch_size):
            return [np.array([.25, .75]) for _ in texts]
        def query_embed(self, query):
            if query_error:
                raise RuntimeError("private token must not escape")
            return [np.array([.25, .75])]
    class Collection:
        def count(self):
            return count
        def upsert(self, **kwargs):
            calls.append(("upsert", kwargs))
        def query(self, **kwargs):
            return {"ids": [[]], "distances": [[]]}
    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))
        def get_or_create_collection(self, **kwargs):
            return Collection()
        def delete_collection(self, name):
            calls.append(("delete", name))
        def create_collection(self, **kwargs):
            calls.append(("create", kwargs))
            return Collection()
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=Model))
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(HttpClient=Client, PersistentClient=Client))
    monkeypatch.setitem(sys.modules, "chromadb.config", SimpleNamespace(Settings=lambda **kwargs: kwargs))
    return calls


@pytest.mark.parametrize("force,count,host", [(False, 0, None), (True, 1, "chroma"), (False, 1, None)])
def test_vector_cache_rebuilds_only_when_required(monkeypatch, tmp_path, force, count, host):
    path = manifest(tmp_path)
    if host:
        monkeypatch.setenv("CHROMA_HOST", host)
    else:
        monkeypatch.delenv("CHROMA_HOST", raising=False)
    calls = install_vector_stubs(monkeypatch, count=count)
    kb = KnowledgeBase(path, index_dir=tmp_path / "index", mode="vector")
    assert kb.build_index(force=force)["backend"] == "chroma_fastembed"
    assert any(name == "upsert" for name, _ in calls) is (force or count == 0)
    assert any(name == "delete" for name, _ in calls) is force
    assert any(name == "create" for name, _ in calls) is force
    if host:
        assert calls[0][1]["host"] == "chroma"
    for name, payload in calls:
        if name == "upsert":
            assert payload["documents"] == ["졸업 123학점 원문"]
            assert payload["embeddings"] == [[.25, .75]]
            assert "text" not in payload["metadatas"][0]
    before = list(calls)
    assert kb.build_index()["chunk_count"] == 1
    assert calls == before


def test_vector_construction_failure_preserves_lexical_service_and_redacts_detail(monkeypatch, tmp_path):
    path = manifest(tmp_path)
    install_vector_stubs(monkeypatch, initialization_error=RuntimeError("https://private.example sk-private-key"))
    kb = KnowledgeBase(path, index_dir=tmp_path / "index", mode="vector")
    result = kb.build_index()
    assert result["backend"] == "lexical_fallback"
    assert "private" not in " ".join(result["warnings"])
    assert kb.search("123학점")


def test_vector_query_failure_is_disclosed_and_empty_lexical_query_is_safe(monkeypatch, tmp_path):
    path = manifest(tmp_path)
    install_vector_stubs(monkeypatch, count=1, query_error=True)
    kb = KnowledgeBase(path, index_dir=tmp_path / "index", mode="vector")
    results = kb.search("123학점")
    assert results and results[0]["retrieval_backend"] == "lexical_fallback"
    assert any("벡터 질의 실패" in warning for warning in kb.warnings)
    assert "private" not in str(kb.warnings)
    assert kb._lexical_scores("!!!") == {}


def test_no_matching_citation_means_no_fact_is_presented():
    fact = GroundedFact("should not be invented", ("public",), (), ("required quote",))
    kb = KnowledgeBase(mode="lexical")
    statements, citations = [], []
    kb._append_grounded_fact(fact, [{"text": "unrelated", "source_id": "public", "priority": 0}], [], statements, citations)
    assert statements == citations == []


def test_empty_provider_exception_still_allows_lexical_fallback(monkeypatch, tmp_path):
    path = manifest(tmp_path)
    install_vector_stubs(monkeypatch, initialization_error=RuntimeError())
    kb = KnowledgeBase(path, index_dir=tmp_path / "index", mode="vector")
    assert kb.build_index()["backend"] == "lexical_fallback"
    assert kb.search("123학점")


def test_successful_vector_query_preserves_real_backend_label(monkeypatch, tmp_path):
    path = manifest(tmp_path)
    install_vector_stubs(monkeypatch, count=1)
    kb = KnowledgeBase(path, index_dir=tmp_path / "index", mode="vector")
    kb.build_index()
    requests = []
    def query(**arguments):
        requests.append(arguments)
        return {"ids": [[kb.chunks[0]["chunk_id"]]], "distances": [[.5]]}
    monkeypatch.setattr(kb._collection, "query", query)
    results = kb.search("123학점")
    assert results and results[0]["retrieval_backend"] == "chroma_fastembed"
    assert requests == [{"query_embeddings": [[.25, .75]], "n_results": 1, "include": ["distances"]}]
    # Normalized lexical score 1 plus cosine similarity .5, with documented weights.
    assert results[0]["score"] == .775


def test_token_chunks_and_page_context_replace_legacy_cache(monkeypatch, tmp_path):
    content = "제1조 기준\n" + "학생 이수 학점 조건 " * 100
    path = manifest(tmp_path, content=content)
    install_vector_stubs(monkeypatch)
    lexical = KnowledgeBase(path, mode="lexical")
    lexical.build_index()
    kb = KnowledgeBase(path, index_dir=tmp_path / "index", mode="vector")
    report = kb.build_index()
    assert report["chunking"]["token_limit"] == 128
    assert all(kb._chunker.count(c["text"]) <= 128 for c in kb.chunks)
    assert kb._fingerprint != lexical._fingerprint
    assert all(c["context_id"] == "public:p0" for c in kb.chunks)
    result = kb._answer_result("확인 필요", "검토", kb.search("학점"), "학점")
    assert all(e["context_text"] == content for e in result["evidence"])
    captured = []
    def query_embed(text):
        captured.append(text)
        return [np.array([.25, .75])]
    monkeypatch.setattr(kb._model, "query_embed", query_embed)
    kb.search("학점 " * 300)
    assert len(captured) > 1
    assert all(kb._chunker.count(text) <= 128 for text in captured)


def test_top_results_prefer_distinct_pages_when_full_page_context_is_available(monkeypatch):
    kb = KnowledgeBase(mode="lexical")
    kb._built = True
    kb.backend = "lexical_explicit"
    kb.chunks = [dict(source_id="public", page=page, chunk_id=str(i), text=f"different excerpt {i}", priority=0)
                 for i, page in enumerate([1, 1, 1, 2, 3])]
    monkeypatch.setattr(kb, "_lexical_scores", lambda query: {str(i): 5-i for i in range(5)})
    hits = kb.search("규정", k=3)
    assert [hit["page"] for hit in hits] == [1, 2, 3]
