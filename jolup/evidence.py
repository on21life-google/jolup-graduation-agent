"""Metadata is provenance, not a claim that an unknown publication date is known."""
import json
import hashlib
from functools import lru_cache
from .settings import DATA, ROOT


@lru_cache(maxsize=1)
def rules():
    return json.loads((DATA / "rules/sungkyul_cs_2024.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def sources():
    raw = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    return {s["source_id"]: s for s in raw["sources"]}


def evidence_for(*groups):
    result = []
    seen = set()
    for group in groups:
        for ref in rules()["citations"].get(group, []):
            key = (ref["source_id"], ref.get("page"))
            if key in seen:
                continue
            seen.add(key)
            s = sources()[ref["source_id"]]
            result.append({**ref, "file_name": s["file_name"], "url": s["url"], "registered_date": s["registered_date"], "date_note": s["date_note"], "kind": s.get("kind", "provided_pdf")})
    return result


def metadata(evidence):
    seen = set()
    result = []
    for item in evidence:
        if item["source_id"] in seen:
            continue
        seen.add(item["source_id"])
        result.append({k: item.get(k) for k in ("source_id", "file_name", "url", "registered_date", "date_note")})
    return result


@lru_cache(maxsize=128)
def _file_hash(path, modified_ns, size):
    del modified_ns, size
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_rule_bundle():
    """A changed source or rule needs human review before producing a verdict."""
    path = DATA / "rules/reviewed_bundle.json"
    if not path.exists():
        return ["규정 대조 검토 기록이 없습니다. 원문과 요건 JSON을 검토해야 합니다."]
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for entry in bundle["files"]:
            target = (ROOT / entry["path"]).resolve()
            if not target.is_relative_to(DATA.resolve()) or not target.is_file():
                return ["검토된 규정 원본 또는 계산 규칙 파일이 없습니다."]
            stat = target.stat()
            if _file_hash(target, stat.st_mtime_ns, stat.st_size) != entry["sha256"]:
                return ["검토 후 규정 원본 또는 계산 규칙이 변경되었습니다. 원문 교차검토 후 재등록해야 합니다."]
        required_paths = {"data/rules/sungkyul_cs_2024.json", "data/sources.json"}
        required_paths.update(source["path"] for source in sources().values())
        reviewed_paths = {entry["path"] for entry in bundle["files"]}
        if not required_paths.issubset(reviewed_paths):
            return ["규정 검토 대상 파일 목록이 불완전합니다. 계산 규칙·출처 목록·원문을 함께 확인해야 합니다."]
    except (OSError, ValueError, KeyError, TypeError):
        return ["규정 검토 기록을 확인할 수 없습니다."]
    return []
