"""Small retrieval chunks; source pages remain the unit for reading full rules."""
from __future__ import annotations

import hashlib
import re
from tokenizers import Tokenizer


class TokenChunker:
    """Count with the embedding model's tokenizer, without mutating that model.

    Character offsets preserve the original wording. Prefer paragraph/line and
    sentence boundaries, then verify the actual input including special tokens.
    No overlap is necessary: citations carry a link to their complete page.
    """

    def __init__(self, tokenizer: Tokenizer):
        if not tokenizer.truncation:
            raise ValueError("Embedding tokenizer must declare a token limit")
        self.limit = int(tokenizer.truncation["max_length"])
        self.tokenizer = Tokenizer.from_str(tokenizer.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.tokenizer_hash = hashlib.sha256(self.tokenizer.to_str().encode()).hexdigest()
        self.budget = self.limit - self.tokenizer.num_special_tokens_to_add(False)
        if self.budget < 1:
            raise ValueError("Embedding token limit cannot contain its special tokens")

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text).ids)

    def split(self, text: str) -> list[str]:
        remaining = re.sub(r"[ \t]+", " ", text).strip()
        result = []
        while remaining:
            encoded = self.tokenizer.encode(remaining, add_special_tokens=False)
            if len(encoded.ids) <= self.budget:
                result.append(remaining)
                break
            end = encoded.offsets[self.budget - 1][1]
            boundary = max(remaining.rfind("\n", end // 2, end),
                           remaining.rfind(". ", end // 2, end))
            if boundary >= 0:
                end = boundary + 1
            # A wordpiece cut can retokenize differently in isolation. Verify it.
            while end and self.count(remaining[:end].strip()) > self.limit:
                end -= 1
            if not end:
                raise ValueError("A source character exceeds the embedding token limit")
            result.append(remaining[:end].strip())
            remaining = remaining[end:].strip()
        return result
