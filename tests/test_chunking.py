"""Token budget and readable source context are independent of answer correctness."""
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from jolup.chunking import TokenChunker


def tokenizer(limit=8):
    result = Tokenizer(models.WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2}, unk_token="[UNK]"))
    result.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    result.post_processor = processors.TemplateProcessing(single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)])
    result.enable_truncation(limit)
    result.enable_padding(length=limit)
    return result


def test_split_respects_real_budget_including_special_tokens_without_changing_model():
    original = tokenizer()
    splitter = TokenChunker(original)
    source = " ".join(f"과목{i}" for i in range(31))
    chunks = splitter.split(source)
    assert len(chunks) > 1
    assert " ".join(chunks) == source
    assert all(splitter.count(chunk) <= 8 for chunk in chunks)
    assert original.truncation["max_length"] == 8
    assert original.padding["length"] == 8
    assert splitter.count(source) == 33  # Counting must not silently truncate or pad.


def test_prefer_line_or_sentence_boundary_and_keep_every_character():
    splitter = TokenChunker(tokenizer(10))
    for text in ["제1조 기준\n첫 조건 하나 둘\n예외 조건 셋 넷 다섯 여섯", "첫 문장 하나 둘. 다음 문장 셋 넷 다섯 여섯 일곱 여덟"]:
        chunks = splitter.split(text)
        assert len(chunks) >= 2
        assert all(splitter.count(chunk) <= 10 for chunk in chunks)
        assert "".join("".join(chunks).split()) == "".join(text.split())
    assert splitter.split(" \n\t ") == []


def test_missing_or_impossible_token_limit_fails_explicitly():
    original = tokenizer()
    original.no_truncation()
    with pytest.raises(ValueError, match="token limit"):
        TokenChunker(original)
    with pytest.raises(ValueError, match="special tokens"):
        TokenChunker(tokenizer(2))


def test_unicode_rows_and_long_text_are_preserved_without_a_download():
    vocab = {value: index for index, value in enumerate(pre_tokenizers.ByteLevel.alphabet())}
    original = Tokenizer(models.BPE(vocab=vocab, merges=[]))
    original.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    original.enable_truncation(128)
    splitter = TokenChunker(original)
    source = "제50조 졸업\n" + "컴퓨터공학과 2024학번 교양필수 14 전공심화 63. 예외는 원문 확인. " * 30
    chunks = splitter.split(source)
    assert all(splitter.count(chunk) <= 128 for chunk in chunks)
    assert "".join("".join(chunks).split()) == "".join(source.split())


def test_single_character_that_exceeds_budget_is_rejected_instead_of_silently_lost():
    # A byte-level tokenizer may need several tokens for one Unicode character.
    vocab = {value: index for index, value in enumerate(pre_tokenizers.ByteLevel.alphabet())}
    original = Tokenizer(models.BPE(vocab=vocab, merges=[]))
    original.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    original.enable_truncation(1)
    splitter = TokenChunker(original)
    with pytest.raises(ValueError, match="source character"):
        splitter.split("😀")
