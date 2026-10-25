from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DOMAIN_ID = "sungkyul-computer-2024"

DEFAULT_PROFILE = {
    "school": "성결대학교", "department": "컴퓨터공학과", "entry_year": 2024,
    "track": "전공심화", "enrollment_type": "신입학", "current_term": "2026-2",
    "target_graduation_term": "2027-2", "completed_semesters": 5,
    "registered_semesters": 6, "chapel_pass_semesters": 5,
    "graduation_project_status": "미완료", "engineering_accreditation": False,
    "is_dummy": True, "scenario_id": "on_track", "academic_warning": False,
}
