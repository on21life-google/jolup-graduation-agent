import json
from pathlib import Path

import pytest

from jolup.academic import parse_transcript, check_requirements
from jolup.samples import load_sample


def test_explicit_foreign_school_column_is_not_silently_dropped():
    csv = "학교,course_name,term,category,credits,grade,status\n다른대학교,기초글쓰기,2024-1,교필,2,A0,completed\n"
    parsed = parse_transcript(csv)
    assert not parsed["ok"] and not parsed["courses"]


def test_mixed_cohort_rows_rejected():
    csv = "입학년도,course_name,term,category,credits,grade,status\n2024,기초글쓰기,2024-1,교필,2,A0,completed\n2023,사회봉사,2024-1,교필,1,P,completed\n"
    assert not parse_transcript(csv)["ok"]


def test_changed_rules_are_not_treated_as_approved(monkeypatch):
    import jolup.evidence as evidence
    monkeypatch.setattr(evidence, "_file_hash", lambda *args: "changed")
    s = load_sample("fully_met")
    d = check_requirements(s["profile"], s["courses"])
    assert d["status"] == "확인 필요"


@pytest.mark.parametrize("mutation", ["empty", "missing_rule", "missing_source"])
def test_review_manifest_cannot_silently_omit_required_evidence(monkeypatch, mutation):
    import jolup.evidence as evidence
    path = evidence.DATA / "rules/reviewed_bundle.json"
    original_read = Path.read_text
    bundle = json.loads(original_read(path, encoding="utf-8"))
    if mutation == "empty":
        bundle["files"] = []
    elif mutation == "missing_rule":
        bundle["files"] = [entry for entry in bundle["files"] if not entry["path"].endswith("sungkyul_cs_2024.json")]
    else:
        bundle["files"] = [entry for entry in bundle["files"] if not entry["path"].endswith("2026-2학기 강의시간표 안내문.pdf")]
    def changed_manifest(self, *args, **kwargs):
        return json.dumps(bundle) if self == path else original_read(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", changed_manifest)
    sample = load_sample("fully_met")
    diagnosis = check_requirements(sample["profile"], sample["courses"])
    assert diagnosis["status"] == "확인 필요"
    assert any("검토 대상" in issue for issue in diagnosis["review_items"])
