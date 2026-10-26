from rag.text_splitter import (
    split_text,
    extract_section
)


def test_split_text_returns_chunks():

    text = (
        "첫 번째 문단입니다.\n\n"
        + ("두 번째 문단입니다. " * 100)
    )

    chunks = split_text(
        text,
        chunk_size=100,
        overlap=20
    )

    assert chunks

    assert all(
        chunk.strip()
        for chunk in chunks
    )


def test_split_text_respects_chunk_size():

    text = "\n".join(f"{i}번째 줄의 내용입니다." * (i % 5 + 1) for i in range(200))

    chunks = split_text(text, chunk_size=300, overlap=60)

    assert all(len(chunk) <= 300 for chunk in chunks)


def test_extract_section():

    result = extract_section(
        "제12조(졸업)\n졸업 요건"
    )

    assert result == "제12조(졸업)"


def test_extract_section_ignores_inline_reference_and_dates():

    assert extract_section("이 학칙 제10조에 따라 처리한다.") is None
    assert extract_section("2026. 3. 2.(월) 개강") is None
