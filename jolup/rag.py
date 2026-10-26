"""Evidence retrieval over the allow-listed public school documents only.

Embeddings rank evidence; they never calculate credits or decide eligibility.
Document text is data, not an instruction to this application or its LLM.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .settings import DATA, DOMAIN_ID
from .chunking import TokenChunker
from .source_metadata import (classify_document_type, filename_date_hint,
                              normalize_text, section_for_chunk)

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
INDEX_VERSION = "7-token-page-source-metadata"


def _compact(text: str) -> str:
    return re.sub(r"[^가-힣a-z0-9]", "", normalize_text(text).lower())


def _terms(text: str) -> Counter:
    # Korean character bigrams also match inflected words without a tokenizer service.
    terms = re.findall(r"[a-z0-9]+|[가-힣]+", normalize_text(text).lower())
    return Counter(terms + [word[i:i + 2] for word in terms if re.search("[가-힣]", word)
                            for i in range(len(word) - 1)])


def _chunks(text: str, size: int = 620, overlap: int = 100) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start + size // 2, end),
                           text.rfind(". ", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        value = text[start:end].strip()
        if value:
            result.append(value)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return result


TOPICS = {
    "total": {"patterns": ["졸업학점", "총학점", "총이수", "졸업몇학점", "졸업에필요한학점", "졸업하려면", "졸업요건", "졸업조건"],
              "query": "2019학년도 이후 졸업 이수학점 123학점 제35조", "sources": ["school_rules", "course_guide"], "pages": [6, 9, 24]},
    "major": {"patterns": ["전공심화", "전공학점", "주전공", "전공몇"],
              "query": "2024 컴퓨터공학과 주전공 42 전공심화 21 63", "sources": ["course_guide", "major_notice"], "pages": [24, 28, 29]},
    "required_major": {"patterns": ["전필", "전공필수", "종합설계", "권장필수", "데이터구조", "운영체제"],
                       "query": "2025.01 이후 컴퓨터공학과 졸업 전공필수 6 전공종합설계 권장필수", "sources": ["major_notice"], "pages": []},
    "general": {"patterns": ["교양", "교필", "교선", "글쓰기", "사회봉사", "영어커뮤니케이션", "코딩기초", "ai의이해"],
                "query": "2024 컴퓨터공학과 교양필수 14 교양선택 20 기독교 영어 기초글쓰기 대학생활 사회봉사", "sources": ["course_guide"], "pages": [17, 24, 28, 29]},
    "general_courses": {"patterns": ["교필과목", "교양필수과목", "교필목록", "글쓰기", "사회봉사", "영어커뮤니케이션"],
                "query": "교양필수 기독교로의초대 신앙으로의초대 영어커뮤니케이션 기초글쓰기 사회봉사 대학생활과진로", "sources": ["course_guide"], "pages": [17, 28]},
    "general_exemption": {"patterns": ["코딩기초", "ai의이해"],
                "query": "컴퓨터공학과 2021학번 이후 AI의이해와활용 제외 교양필수 14학점", "sources": ["course_guide"], "pages": [17, 20]},
    "chapel": {"patterns": ["채플", "chapel", "경건회"], "query": "경건회 채플 6학기 이수 졸업 제50조", "sources": ["school_rules", "course_guide"], "pages": [9, 24, 29]},
    "semester": {"patterns": ["등록학기", "8학기", "재학연한", "몇학기"], "query": "졸업 8학기 이상 등록 제50조", "sources": ["school_rules"], "pages": [9]},
    "project": {"patterns": ["졸업작품", "졸업논문", "졸업심사"], "query": "컴퓨터공학과 졸업작품 심사 통과 전공종합설계", "sources": ["major_notice", "school_rules", "curriculum_year4"], "pages": [9]},
    "load": {"patterns": ["학기당", "한학기", "학점한도", "수강신청학점", "최대학점", "18학점", "연간", "36학점", "초과학점", "초과수강", "4.2"],
             "query": "123학점 졸업 학기 18학점 연간 36 15학점 평균평점 4.2 3학점", "sources": ["school_rules", "course_guide"], "pages": [7, 8, 9]},
    "summer": {"patterns": ["계절학기", "여름학기", "겨울학기"], "query": "계절학기 최대 6학점 연간 36학점", "sources": ["school_rules", "course_guide"], "pages": [7, 8]},
    "retake": {"patterns": ["재수강", "재이수", "재수강상한", "대체과목"], "query": "재수강 30학점 A0 대체 교과목 승인", "sources": ["course_guide", "school_rules"], "pages": [9, 20, 29]},
    "duplicate": {"patterns": ["중복", "동일과목", "두번", "두 번"], "query": "동일 교과목 중복 이수 나중 취득 학점 인정하지", "sources": ["course_guide"], "pages": [29]},
    "grade": {"patterns": ["f학점", "f받", "f는", "d0", "p학점", "p는", "pf", "평점", "패스", "pass", "낙제"],
              "query": "성적 D0 이상 취득 학점 F P 평점 필수과목 낙제 재이수", "sources": ["school_rules", "course_guide"], "pages": [7, 8, 9]},
}


@dataclass(frozen=True)
class GroundedFact:
    """A curated statement and the exact source text required before it is usable.

    all_of/any_of match compacted page context; citation_terms select the excerpt
    to show. Follow-ups are checked only after the parent source guard succeeds.
    """

    statement: str
    sources: tuple[str, ...]
    pages: tuple[int, ...]
    citation_terms: tuple[str, ...]
    all_of: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    citation_sources: tuple[str, ...] = ()
    followups: tuple[GroundedFact, ...] = ()

    def matches(self, text: str) -> bool:
        return (all(term in text for term in self.all_of)
                and (not self.any_of or any(term in text for term in self.any_of)))


# These are existing verified wording/guards, not generated answers or new rules.
# Reading one entry shows: permitted sources -> required original text -> answer.
GROUNDED_FACTS = {
    'total': (GroundedFact(
        statement='총 졸업학점은 123학점이며, 총점만 채우는 것으로 충분하지 않고 영역별 요건과 비학점 요건도 충족해야 합니다.',
        sources=('school_rules',), pages=(6, 9),
        citation_terms=('123학점', '제50조'),
        any_of=('2019학번부터는123학점', '2019학번부터123학점'),
    ),),
    'major': (GroundedFact(
        statement='전공심화는 주전공 42학점과 심화 21학점을 합친 전공 총 63학점이 기준입니다. 같은 이수학점을 두 영역에 중복 합산하지 않습니다.',
        sources=('course_guide',), pages=(24,),
        citation_terms=('컴퓨터공학', '전공심화'),
        all_of=('컴퓨터공학과202620252024146203466364221',),
    ),),
    'required_major': (GroundedFact(
        statement='2025년 1월 변경 공지에 따라 전공필수는 전공종합설계(1)·(2) 각 3학점, 합계 6학점입니다. 졸업작품 심사 통과도 필요합니다. 이전 전공필수 이수학점은 전공 총학점에 포함하며, 권장필수 과목을 별도의 졸업 필수요건으로 강제하지 않습니다.',
        sources=('major_notice',), pages=(),
        citation_terms=('전공종합설계', '6학점', '권장필수'),
        all_of=('전공필수는6학점', '전공종합설계13학점', '전공종합설계23학점', '졸업작품심사통과'),
    ),),
    'general': (GroundedFact(
        statement='교양필수 14학점과 교양선택 20학점이 필요합니다. 교양필수는 합계뿐 아니라 해당 학번의 지정 과목 이수도 확인해야 합니다.',
        sources=('course_guide',), pages=(24,),
        citation_terms=('컴퓨터공학', '교양필수', '교양선택'),
        all_of=('컴퓨터공학과202620252024146203466364221',),
    ),),
    'general_courses': (GroundedFact(
        statement='교양필수 지정 과목은 기독교로의초대(3), 신앙으로의초대(3), 영어커뮤니케이션(1)·(2)(각 2), 기초글쓰기(2), 대학생활과진로(1), 사회봉사(1)입니다. 과거 과목명·재수강 대체 인정은 해당 승인 기록도 확인해야 합니다.',
        sources=('course_guide',), pages=(17,),
        citation_terms=('기독교로의초대', '신앙으로의초대', '사회봉사', '대학생활과진로'),
        all_of=('기독교로의초대', '신앙으로의초대', '영어커뮤니케이션', '기초글쓰기', '사회봉사', '대학생활과진로'),
    ),),
    'general_exemption': (GroundedFact(
        statement='컴퓨터공학과의 2021학번 이후는 AI의이해와활용을 제외한 교양필수 14학점이 기준입니다.',
        sources=('course_guide',), pages=(17,),
        citation_terms=('컴퓨터공학과', '제외', '14'),
        all_of=('컴퓨터공학과', '2021', '제외', '14'),
        followups=(GroundedFact(
            statement='대체과목표에는 컴퓨팅사고와코딩기초에서 AI의이해와활용으로의 변경 관계가 나옵니다. 이 범위의 진단에서는 해당 과목을 추가 졸업 필수로 요구하지 않습니다.',
            sources=('course_guide',), pages=(20,),
            citation_terms=('컴퓨팅사고와코딩기초', '의이해와활용 AI'),
            all_of=('컴퓨팅사고와코딩기초', '의이해와활용ai'),
        ),),
    ),),
    'chapel': (GroundedFact(
        statement='채플(경건회)은 6학기 이수가 필요합니다. 수강 중인 학기는 통과가 확인되기 전까지 완료로 세지 않습니다.',
        sources=('school_rules',), pages=(9,),
        citation_terms=('6학기', '경건회'),
        all_of=('채플6학기',),
    ),),
    'semester': (GroundedFact(
        statement='일반 신입학의 정규 졸업은 8학기 이상 등록이 기준입니다. 조기졸업·편입 등 예외는 이 진단 범위에서 별도 확인 대상으로 둡니다.',
        sources=('school_rules',), pages=(9,),
        citation_terms=('8학기',),
        all_of=('4년8학기이상을등록',),
    ),),
    'project': (GroundedFact(
        statement='컴퓨터공학과는 전공종합설계(1)·(2) 이수와 졸업작품 심사 통과를 확인해야 합니다. 학점 합계만으로 졸업작품 통과를 추정할 수 없습니다.',
        sources=('major_notice', 'school_rules'), pages=(),
        citation_terms=('졸업작품', '전공종합설계'),
        all_of=('졸업작품심사통과', '전공종합설계'),
        citation_sources=('major_notice',),
    ),),
    'load': (GroundedFact(
        statement='123학점 졸업 대상의 일반 수강 한도는 학기당 18학점, 계절학기를 포함한 연간 36학점입니다. 직전 학기 15학점 이상 이수하고 평점 4.2 이상인 경우 3학점 초과 이수 예외가 있으나, 기본 계획은 예외를 자동 적용하지 않습니다.',
        sources=('school_rules',), pages=(7, 8),
        citation_terms=('18학점', '36학점', '4.2'),
        all_of=('123졸업학점', '1518학점', '36학점', '15학점이상', '42이상'),
    ),),
    'summer': (GroundedFact(
        statement='계절학기는 최대 6학점이며 연간 이수 한도도 함께 확인해야 합니다. 향후 개설 과목은 현재 자료만으로 확정하지 않습니다.',
        sources=('school_rules',), pages=(7, 8),
        citation_terms=('계절학기', '6학점'),
        all_of=('최대학점은1회6학점', '계절'),
    ),),
    'retake': (GroundedFact(
        statement='재수강은 재학 중 총 30학점 이내이고 재수강 성적 상한은 A0입니다. 대체 교과목은 승인 여부를 확인해야 하며, 계산에서는 승인된 재수강을 이전 시도와 연결해 학점을 한 번만 인정합니다.',
        sources=('course_guide',), pages=(9,),
        citation_terms=('재수강', '30학점', 'A0'),
        all_of=('30', '재수강', 'a0'),
    ),),
    'duplicate': (GroundedFact(
        statement='동일 교과목 중복 이수는 학점을 두 번 인정하지 않습니다. 승인된 재수강과 단순 중복을 구분하고, 관계가 불명확하면 확인 필요로 표시합니다.',
        sources=('course_guide',), pages=(29,),
        citation_terms=('동일', '중복'),
        all_of=('동일',),
        any_of=('중복', '취득'),
    ),),
    'grade': (GroundedFact(
        statement='D0 이상 또는 P로 확인된 합격 학점은 이수학점에 포함하고 F는 제외합니다. P/F 과목의 평점 처리와 필수과목 재이수는 해당 규정을 함께 확인합니다. 수강 중 과목은 현재 이수학점에 넣지 않습니다.',
        sources=('school_rules',), pages=(7, 8),
        citation_terms=('D0', '성적', '재이수'),
        all_of=('d0', '성적'),
    ),),
}


class KnowledgeBase:
    def __init__(self, sources_path: str | Path | None = None,
                 index_dir: str | Path | None = None, mode: str | None = None):
        self.sources_path = Path(sources_path or DATA / "sources.json")
        self.index_dir = Path(index_dir or DATA / "index")
        self.mode = mode or os.getenv("JOLUP_RAG_MODE", "vector")
        if self.mode not in {"vector", "lexical"}:
            raise ValueError("JOLUP_RAG_MODE must be vector or lexical")
        self.model_name = os.getenv("JOLUP_EMBED_MODEL", MODEL)
        self.chunks: list[dict] = []
        self.sources: dict[str, dict] = {}
        self.warnings: list[str] = []
        self.backend = "not_built"
        self._model = self._collection = self._client = None
        self._built = False
        self._tokens: list[Counter] = []
        self._fingerprint = ""
        self._chunker = None
        self._page_context: dict[str, str] = {}
        self._source_stats: list[dict] = []

    def _source_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            # Accept both data/sources/... and sources/... manifest conventions.
            candidate = self.sources_path.parent / path
            path = candidate if candidate.exists() else self.sources_path.parent.parent / path
        return path.resolve()

    @staticmethod
    def _source_scope_matches(source: dict) -> bool:
        scope = source.get("applies_to", {})
        return source.get("school", "성결대학교") == "성결대학교" and all(
            key not in scope or str(scope[key]) == str(expected)
            for key, expected in {"department": "컴퓨터공학과", "entry_year": 2024}.items())

    def _checked_source(self, source: dict, allowed_root: Path) -> tuple[dict, Path] | None:
        """Validate one manifest entry before any document content is read."""
        sid = str(source.get("source_id") or source.get("id") or "")
        file_value = source.get("path") or source.get("file_path") or source.get("local_path")
        if not sid or not file_value:
            self.warnings.append("출처 ID 또는 파일 경로가 없는 자료는 제외했습니다.")
            return None
        path = self._source_path(file_value)
        if not path.is_relative_to(allowed_root) or path.suffix.lower() not in {".pdf", ".txt", ".md", ".json"}:
            self.warnings.append(f"{sid}: 공개 근거자료 폴더 밖의 파일 또는 허용하지 않는 형식은 색인하지 않습니다.")
            return None
        if not path.is_file():
            self.warnings.append(f"{sid}: 자료 파일이 없어 제외했습니다.")
            return None
        if source.get("is_student_data") or source.get("kind") in {"transcript", "student", "profile"}:
            self.warnings.append(f"{sid}: 학생 자료는 색인하지 않습니다.")
            return None
        if not self._source_scope_matches(source):
            self.warnings.append(f"{sid}: 학교·학과·입학년도 적용 범위가 달라 제외했습니다.")
            return None
        return dict(source, source_id=sid, file_name=source.get("file_name") or path.name), path

    def _append_page_chunks(self, record: dict, pages: list, chunker: TokenChunker | None):
        sid = record["source_id"]
        document_type = classify_document_type(record["file_name"])
        version_date_hint = filename_date_hint(record["file_name"])
        for page_no, text in pages:
            context_id = f"{sid}:p{page_no or 0}"
            self._page_context[context_id] = text
            for position, chunk in enumerate(chunker.split(text) if chunker else _chunks(text)):
                chunk_id = f"{sid}:p{page_no or 0}:{position}:{hashlib.sha256(chunk.encode()).hexdigest()[:12]}"
                self.chunks.append({
                    "source_id": sid, "chunk_id": chunk_id, "text": chunk,
                    "context_id": context_id,
                    "file_name": record["file_name"], "url": record.get("url"),
                    "registered_date": record.get("registered_date"), "page": page_no,
                    "priority": int(record.get("priority", 0)),
                    "source_kind": record.get("kind", "provided_original"),
                    "date_note": record.get("date_note"),
                    "retrieved_date": record.get("retrieved_date"),
                    "document_type": document_type,
                    "section": section_for_chunk(text, chunk),
                    "version_date_hint": version_date_hint,
                })

    def _load_sources(self, chunker: TokenChunker | None = None):
        self.chunks, self.sources, self.warnings = [], {}, []
        self._page_context = {}
        self._source_stats = []
        manifest = json.loads(self.sources_path.read_text(encoding="utf-8"))
        records = manifest.get("sources", []) if isinstance(manifest, dict) else manifest
        if not isinstance(records, list):
            raise ValueError("sources.json must contain a sources list")
        allowed_root = (self.sources_path.parent / "sources").resolve()
        for source in records:
            checked = self._checked_source(source, allowed_root)
            if checked is None:
                continue
            record, path = checked
            sid = record["source_id"]
            self.sources[sid] = record
            try:
                pages = self._read_pages(path, source)
                source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception as exc:
                self.warnings.append(f"{sid}: 자료 읽기 실패 ({type(exc).__name__}).")
                continue
            before = len(self.chunks)
            self._append_page_chunks(record, pages, chunker)
            self._source_stats.append({"source_id": sid, "file_name": record["file_name"],
                                       "sha256": source_hash, "chunk_count": len(self.chunks) - before})
        self._tokens = [_terms(c["text"]) for c in self.chunks]
        payload = json.dumps([INDEX_VERSION, self.model_name,
                              chunker.tokenizer_hash if chunker else None,
                              chunker.limit if chunker else None, self.chunks],
                             ensure_ascii=False, sort_keys=True)
        self._fingerprint = hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _read_pages(self, path: Path, source: dict) -> list[tuple[int | None, str]]:
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            result = []
            for page_no, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    result.append((page_no, text))
                else:
                    self.warnings.append(f"{source.get('source_id', source.get('id'))}: PDF {page_no}쪽 텍스트 없음. OCR 검토 필요.")
            return result
        content = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            obj = json.loads(content)
            if isinstance(obj, dict) and isinstance(obj.get("pages"), list):
                return [(p.get("page"), str(p.get("text", ""))) for p in obj["pages"]]
            raise ValueError("JSON sources require explicit pages containing text")
        return [(source.get("page"), content)]

    def build_index(self, force: bool = False) -> dict:
        """Build real embeddings; disclose and retain an explicit lexical fallback on failure."""
        if self._built and not force:
            return self._report()
        self._chunker = None
        self._load_sources()
        if not self.chunks:
            self.backend = "unavailable"
            self._built = True
            self.warnings.append("색인 가능한 근거자료가 없습니다.")
            return self._report()
        if self.mode == "lexical":
            self.backend = "lexical_explicit"
            self.warnings.append("명시적으로 선택한 어휘 검색입니다. 벡터 검색 검증 결과가 아닙니다.")
            self._built = True
            return self._report()
        try:
            import chromadb
            from chromadb.config import Settings
            from fastembed import TextEmbedding
            self.index_dir.mkdir(parents=True, exist_ok=True)
            model_dir = self.index_dir.parent / "models"
            model_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(model_dir), threads=2)
            self._chunker = TokenChunker(self._model.model.tokenizer)
            self._load_sources(self._chunker)
            chroma_host = os.getenv("CHROMA_HOST")
            settings = Settings(anonymized_telemetry=False)
            self._client = (chromadb.HttpClient(host=chroma_host, port=int(os.getenv("CHROMA_PORT", "8000")), settings=settings)
                            if chroma_host else chromadb.PersistentClient(path=str(self.index_dir / "chroma"), settings=settings))
            name = f"jolup-{self._fingerprint}"
            self._collection = self._client.get_or_create_collection(
                name=name, metadata={"hnsw:space": "cosine", "domain_id": DOMAIN_ID}, embedding_function=None)
            if force or self._collection.count() != len(self.chunks):
                if force:
                    self._client.delete_collection(name)
                    self._collection = self._client.create_collection(name=name,
                        metadata={"hnsw:space": "cosine", "domain_id": DOMAIN_ID}, embedding_function=None)
                for start in range(0, len(self.chunks), 32):
                    batch = self.chunks[start:start + 32]
                    vectors = [vector.tolist() for vector in self._model.embed([c["text"] for c in batch], batch_size=16)]
                    self._collection.upsert(ids=[c["chunk_id"] for c in batch], embeddings=vectors,
                        documents=[c["text"] for c in batch],
                        metadatas=[{k: v for k, v in c.items() if k != "text" and v is not None} for c in batch])
            self.backend = "chroma_fastembed"
        except Exception as exc:
            # Preserve diagnostics for local troubleshooting without exposing URLs/credentials.
            self.backend = "lexical_fallback"
            self._collection = self._model = None
            self.warnings.append(f"벡터 색인 실패 ({type(exc).__name__}): {self._safe_error(exc)}. 현재 어휘 검색 대체 모드이며 실제 벡터 검색은 미검증입니다.")
        self._built = True
        return self._report()

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        text = str(exc).partition("\n")[0][:250]
        text = re.sub(r"(?:sk-|hf_)[A-Za-z0-9_\-]+", "[redacted]", text)
        text = re.sub(r"https?://\S+", "[remote URL]", text)
        text = re.sub(r"(?i)(?:authorization|token|api[_-]?key)\s*[:=]\s*\S+", "[credential redacted]", text)
        return text

    def _report(self):
        return {"status": "ready" if self.backend == "chroma_fastembed" else "degraded" if self.chunks else "unavailable",
                "backend": self.backend, "model": self.model_name if self.backend == "chroma_fastembed" else None,
                "source_count": len(self.sources), "chunk_count": len(self.chunks),
                "sources": [dict(source) for source in self._source_stats],
                "fingerprint": self._fingerprint, "warnings": list(self.warnings),
                "chunking": {"policy": INDEX_VERSION,
                             "token_limit": self._chunker.limit if self._chunker else None,
                             "context": "source_page"}}

    @staticmethod
    def _profile_error(profile: dict | None) -> str | None:
        if profile is None:
            return None
        expected = {"school": "성결대학교", "department": "컴퓨터공학과", "entry_year": 2024, "track": "전공심화"}
        for key, value in expected.items():
            if key not in profile or str(profile[key]) != str(value):
                return "이 자료 묶음은 성결대학교 2024학번 컴퓨터공학과 전공심화에 한정됩니다. 적용 학과·입학년도·이수 방식을 확인해 주세요."
        if profile.get("enrollment_type", "신입학") != "신입학":
            return "편입학·재입학 등 별도 인정 규정은 이 자료 묶음으로 확정할 수 없습니다. 학과 확인이 필요합니다."
        if profile.get("engineering_accreditation", False):
            return "이 프로젝트의 판정 범위에서는 공학인증을 제외합니다. 공학인증 요건은 별도 확인이 필요합니다."
        return None

    @staticmethod
    def _topics(query: str) -> list[str]:
        compact = _compact(query)
        return [name for name, value in TOPICS.items() if any(_compact(pattern) in compact for pattern in value["patterns"])]

    def _lexical_scores(self, query: str) -> dict[str, float]:
        query_terms = _terms(query)
        if not query_terms or not self.chunks:
            return {}
        df = Counter(term for token_set in self._tokens for term in query_terms if term in token_set)
        scores = {}
        for chunk, token_set in zip(self.chunks, self._tokens):
            score = sum((1 + math.log(count)) * math.log(1 + len(self.chunks) / (1 + df[term]))
                        for term, count in token_set.items() if term in query_terms)
            scores[chunk["chunk_id"]] = score / math.sqrt(max(10, sum(token_set.values())))
        return scores

    def _vector_scores(self, expanded: str) -> dict[str, float]:
        if self.backend != "chroma_fastembed":
            return {}
        try:
            import numpy as np
            query_parts = self._chunker.split(expanded)
            query_vector = np.mean([next(iter(self._model.query_embed(part)))
                                    for part in query_parts], axis=0).tolist()
            result = self._collection.query(query_embeddings=[query_vector],
                n_results=min(30, len(self.chunks)), include=["distances"])
            return {cid: max(0.0, 1 - float(distance))
                    for cid, distance in zip(result["ids"][0], result["distances"][0])}
        except Exception as exc:
            self.backend = "lexical_fallback"
            self.warnings.append(f"벡터 질의 실패 ({type(exc).__name__}); 이 응답은 어휘 검색 결과입니다.")
            return {}

    @staticmethod
    def _source_bonus(chunk: dict, topics: list[str]) -> float:
        score = 0.0
        for topic in topics:
            definition = TOPICS[topic]
            if chunk["source_id"] in definition["sources"]:
                score += .13
                if chunk["page"] in definition["pages"]:
                    score += .20
        # The department's 2025 notice supersedes older mandatory-major counts.
        if "required_major" in topics and chunk["source_id"] == "major_notice":
            score += .55
        return score

    def _rank_chunks(self, lexical: dict, semantic: dict, topics: list[str]) -> list:
        max_lex = max(lexical.values(), default=1) or 1
        ranked = []
        for chunk in self.chunks:
            cid = chunk["chunk_id"]
            lex = lexical.get(cid, 0) / max_lex
            score = .55 * lex + .45 * semantic.get(cid, 0) if semantic else lex
            score += self._source_bonus(chunk, topics)
            if score > .03:
                ranked.append((score, chunk))
        return sorted(ranked, key=lambda item: (-item[0], -item[1]["priority"], item[1]["chunk_id"]))

    def _select_pages(self, ranked: list, k: int) -> list[dict]:
        output = []
        for score, chunk in ranked:
            # One strongest excerpt per page; full page context carries its other conditions.
            if any(c["source_id"] == chunk["source_id"] and c["page"] == chunk["page"] for c in output):
                continue
            output.append(dict(chunk, score=round(score, 6), retrieval_backend=self.backend))
            if len(output) == k:
                break
        return output

    def search(self, query: str, profile: dict | None = None, k: int = 5) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or self._profile_error(profile):
            return []
        query = normalize_text(query)
        if not self._built:
            self.build_index()
        if not self.chunks:
            return []
        topics = self._topics(query)
        expanded = query + " " + " ".join(TOPICS[t]["query"] for t in topics)
        lexical = self._lexical_scores(expanded)
        semantic = self._vector_scores(expanded)
        ranked = self._rank_chunks(lexical, semantic, topics)
        return self._select_pages(ranked, max(1, min(int(k), 5)))

    @staticmethod
    def _query_scope_error(query: str) -> str | None:
        """Explain unsupported question scope before retrieval can begin."""
        # Unsupported promises and injection attempts must not become retrieval instructions.
        query = normalize_text(query)
        compact = _compact(query)
        restricted = ["시스템프롬프트", "apikey", "api키", "비밀키", "다른학생", "타학생", "성적조작", "성적위조", "이전지시무시", "규칙무시", "무조건졸업", "졸업보장", "토익", "toeic", "토플", "장학금", "공학인증"]
        if any(token in compact for token in restricted):
            return "제공된 근거자료와 이 프로젝트의 적용 범위로 확인하거나 수행할 수 없는 요청입니다. 졸업 판정은 학교의 최종 심사가 필요합니다."
        if any(word in compact for word in ["편입", "재입학", "외국인", "유학생", "조기졸업", "복수전공", "부전공"]):
            return "현재 계산과 규정 설명은 일반 신입학·전공심화의 정규 졸업 기준입니다. 질문에 포함된 별도 이수 방식·신분·졸업 예외에는 추가 적용 규정이 필요합니다."
        cohorts = re.findall(r"(?<!\d)(\d{4}|\d{2})학번", compact)
        if any(year not in {"2024", "24"} for year in cohorts):
            return "현재 검증된 적용 대상은 2024학번 컴퓨터공학과 전공심화입니다. 다른 입학년도에는 별도 교육과정 확인이 필요합니다."
        mentioned_departments = re.findall(r"[가-힣]+(?:학과|학부)", query)
        if any(dept not in {"컴퓨터공학과", "우리학과", "해당학과", "소속학과"} for dept in mentioned_departments):
            return "컴퓨터공학과 이외 학과의 요건은 별도 자료가 필요합니다."
        universities = re.findall(r"[가-힣]+대학교", query)
        if any(name != "성결대학교" for name in universities):
            return "성결대학교 이외 학교의 요건은 별도 자료가 필요합니다."
        if (any(word in compact for word in ["개설확정", "확정개설", "시간표", "담당교수", "여석", "수강정원"])
                or (re.search(r"20(?:2[7-9]|[3-9]\d)", compact)
                    and any(word in compact for word in ["개설", "신청기간", "졸업식", "신청일", "날짜"]))):
            return "제공된 안내문과 교육과정은 실제 개설·여석·담당교수 또는 향후 학사일정의 확정 자료가 아닙니다. 해당 학기의 공식 시간표나 학사 공지가 필요합니다."
        if "졸업" in compact and "평점" in compact and not any(word in compact for word in ["초과", "수강", "4.2"]):
            return "이 자료에서 일반 졸업의 별도 최소 평점 기준을 확정하지 못했습니다. D0 이상 합격 기준을 전체 졸업 평점 기준으로 바꾸어 적용하지 않습니다."
        return None

    def answer(self, query: str, profile: dict | None = None) -> dict:
        if not isinstance(query, str) or not query.strip():
            return self._answer_result("확인 필요", "확인할 규정 질문을 입력해 주세요.", [], "")
        error = self._profile_error(profile)
        if error:
            return self._answer_result("확인 필요", error, [], query)
        error = self._query_scope_error(query)
        if error:
            return self._answer_result("확인 필요", error, [], query)
        topics = self._topics(query)
        evidence = self.search(query, profile)
        if not evidence or not topics:
            return self._answer_result("확인 필요", "제공된 자료에서 질문에 직접 답할 근거를 확정하지 못했습니다. 학과 공지나 해당 규정의 추가 확인이 필요합니다.", evidence[:2] if topics else [], query)
        statements, used, unverified = self._grounded_statements(topics, evidence)
        if not statements:
            return self._answer_result("확인 필요", "관련 원문은 검색했지만 요청한 조건에 적용되는 규정을 확정하지 못했습니다. 아래 원문과 적용 대상을 확인해 주세요.", evidence, query)
        if unverified:
            result = self._answer_result("확인 필요", "2024학번 컴퓨터공학과 전공심화 기준으로 확인된 부분입니다. " + " ".join(statements)
                                         + " 질문의 일부 규정은 근거가 부족하거나 한 번에 제시할 인용 범위를 넘었습니다. 나머지 조건은 별도로 확인해 주세요.", used, query)
            result["unverified_topics"] = unverified
            return result
        return self._answer_result("근거 확인", "2024학번 컴퓨터공학과 전공심화 기준입니다. " + " ".join(statements), used, query)

    def _fact_context(self, fact: GroundedFact) -> tuple[str, list[dict]]:
        # Retain the full page context even when a retrieved chunk ends at a table.
        selected = [chunk for chunk in self.chunks if chunk["source_id"] in fact.sources
                    and (not fact.pages or chunk["page"] in fact.pages)]
        return _compact(" ".join(chunk["text"] for chunk in selected)), selected

    def _verified_facts(self, facts: tuple[GroundedFact, ...]):
        """Yield source-backed facts; dependent facts cannot bypass their parent."""
        for fact in facts:
            text, selected = self._fact_context(fact)
            if fact.matches(text):
                yield fact, selected
                yield from self._verified_facts(fact.followups)

    @staticmethod
    def _select_citation(fact: GroundedFact, selected: list[dict]) -> dict | None:
        terms = [_compact(term) for term in fact.citation_terms]
        ranked = []
        for chunk in selected:
            if fact.citation_sources and chunk["source_id"] not in fact.citation_sources:
                continue
            text = _compact(chunk["text"])
            matches = sum(term in text for term in terms)
            if matches:
                ranked.append((-matches, -chunk["priority"], chunk))
        # min preserves original source order on a tie, just like the former sort.
        return min(ranked, key=lambda item: item[:2])[2] if ranked else None

    def _append_grounded_fact(self, fact: GroundedFact, selected: list[dict],
                              evidence: list[dict], statements: list[str], used: list[dict]):
        chosen = self._select_citation(fact, selected)
        if chosen is None:
            return
        already_cited = chosen["chunk_id"] in {item["chunk_id"] for item in used}
        if not already_cited and len(used) >= 5:
            return
        # One cited context per statement; never append an uncited tail then trim it.
        statements.append(fact.statement)
        if not already_cited:
            score = next((item["score"] for item in evidence
                          if item["chunk_id"] == chosen["chunk_id"]), None)
            used.append(dict(chosen, score=score, retrieval_backend=self.backend,
                             citation_origin="source_context"))

    def _grounded_statements(self, topics: list[str], evidence: list[dict]) -> tuple[list[str], list[dict], list[str]]:
        """Verify each topic's source guards, then add statements within the citation budget."""
        statements, used, unverified = [], [], []
        for topic in topics:
            statement_count = len(statements)
            for fact, selected in self._verified_facts(GROUNDED_FACTS.get(topic, ())):
                self._append_grounded_fact(fact, selected, evidence, statements, used)
            if len(statements) == statement_count:
                unverified.append(topic)
        return statements, used, unverified

    def _answer_result(self, status: str, answer: str, evidence: list[dict], query: str) -> dict:
        evidence = [dict(e, context_text=self._page_context.get(e.get("context_id"), "")) for e in evidence]
        meta = [{"source_id": e["source_id"], "file_name": e["file_name"], "url": e.get("url"),
                 "registered_date": e.get("registered_date"), "page": e.get("page"),
                 "chunk_id": e["chunk_id"], "retrieval_backend": self.backend,
                 "source_kind": e.get("source_kind"), "date_note": e.get("date_note"),
                 "retrieved_date": e.get("retrieved_date"),
                 "document_type": e.get("document_type"), "section": e.get("section"),
                 "version_date_hint": e.get("version_date_hint")} for e in evidence]
        return {"status": status, "answer": answer, "evidence": evidence, "meta": meta,
                "retrieval": self._report(), "scope": DOMAIN_ID,
                "warnings": list(self.warnings), "answer_mode": "로컬 근거 검색·규칙 설명"}
