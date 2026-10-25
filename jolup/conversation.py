"""Four local turns of verified results; never a model prompt or client-supplied cache."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

from jolup.settings import DATA


DOMAIN_INTENTS = frozenset({"diagnosis", "planning", "regulation", "calendar"})
TRANSIENT_FIELDS = frozenset({"study_trace", "timings", "llm", "ai_explanation", "routing",
                              "execution_mode", "service_mode", "memory", "session_id", "transport_mode"})


def school_revision() -> str:
    """Content, rather than modification time, invalidates cached school facts."""
    paths = {DATA / name for name in ("sources.json", "courses.json", "events.json")}
    for folder in ("sources", "rules"):
        paths.update(path for path in (DATA / folder).rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(DATA)).encode())
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()


def _fingerprint(profile: dict, courses: list[dict]) -> str:
    inputs = json.dumps({"profile": profile, "courses": courses, "school": school_revision()},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(inputs.encode()).hexdigest()


def _repeat_target(query: str) -> str | None:
    """Only an explicit replay request can bypass fresh calculations/search."""
    q = re.sub(r"[\s?!.]", "", query)
    match = re.fullmatch(r"(?:(?:아까|방금|이전)(?:그)?)?(규정|진단|계획|일정|결과|답변|내용|그거)?"
                         r"(?:을|를)?다시(?:보여줘|알려줘|말해줘|보여주세요|알려주세요)", q)
    if match is None:
        return None
    return {"규정": "regulation", "진단": "diagnosis", "계획": "planning", "일정": "calendar"}.get(match[1], "latest")


@dataclass
class Turn:
    query: str
    result: dict


class ConversationMemory:
    """Caller-owned, process-local session. Reset on any student or source change."""
    window_size = 4

    def __init__(self):
        self._turns: list[Turn] = []
        self._fingerprint = ""

    def __len__(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        self._turns.clear()
        self._fingerprint = ""

    def synchronize(self, profile: dict, courses: list[dict]) -> bool:
        current = _fingerprint(profile, courses)
        invalidated = bool(self._fingerprint and current != self._fingerprint)
        if invalidated:
            self._turns.clear()
        self._fingerprint = current
        return invalidated

    def previous_intent(self) -> str | None:
        return next((turn.result["intent"] for turn in reversed(self._turns)
                     if turn.result.get("intent") in DOMAIN_INTENTS), None)

    def replay(self, query: str) -> dict | None:
        target = _repeat_target(query)
        if target is None:
            return None
        for turn in reversed(self._turns):
            result = turn.result
            if result.get("intent") not in DOMAIN_INTENTS or result.get("error_code") or result.get("partial_failure"):
                continue
            if target == "latest" or target == result["intent"]:
                return deepcopy(result)
            for section in result.get("sections", []):
                if section["intent"] == target:
                    return deepcopy(section)
        return None

    def remember(self, query: str, result: dict) -> None:
        saved = {key: value for key, value in result.items() if key not in TRANSIENT_FIELDS}
        self._turns.append(Turn(query, deepcopy(saved)))
        self._turns = self._turns[-self.window_size:]
