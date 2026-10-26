"""Public fixtures and release artifacts stay reproducible and self-verifying."""
import hashlib
import importlib
import json
import runpy
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import package_project as package
from scripts import profile_study

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr(package, "ROOT", tmp_path)
    monkeypatch.setattr(package, "REQUIRED", {"README.md", "data/sources.json"})
    (tmp_path / "data/sources").mkdir(parents=True)
    (tmp_path / "README.md").write_text("public readme")
    (tmp_path / "data/sources/source.txt").write_text("school rule")
    (tmp_path / "data/sources.json").write_text(json.dumps({"sources": [{"path": "data/sources/source.txt"}]}))
    return tmp_path


def test_release_allowlist_omits_private_and_unknown_files(release):
    for relative in [".env", "data/sources/.hidden.txt", "data/sources/unknown.bin", "data/sources/.cache/dummy.txt"]:
        p = release / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("DO_NOT_PACKAGE")
    selected = {p.relative_to(release).as_posix() for p in package.selected_files()}
    assert selected == {"README.md", "data/sources.json", "data/sources/source.txt"}
    report = package.package(check_only=True)
    assert report["status"] == "allowlist_checked" and report["file_count"] == 3
    assert not (release / "output").exists()


def test_release_zip_crc_manifest_and_hashes_agree(release):
    report = package.package()
    assert report["status"] == "packaged_and_hash_verified"
    with ZipFile(report["path"]) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("jolup_team/MANIFEST.json"))
        for row in manifest["files"]:
            body = archive.read("jolup_team/" + row["path"])
            assert len(body) == row["bytes"]
            assert hashlib.sha256(body).hexdigest() == row["sha256"]


def test_release_keeps_design_and_authored_contributions_without_runtime_archives(release):
    included = {
        "assets/sujin/rabbit-hero.png", "assets/sujin/rabbit-avatar.png", "assets/sujin/theme.css",
        "team_contributions/README.md", "team_contributions/sujin/prototype.html",
        "team_contributions/sujin/preview.py", "team_contributions/sujin/notes.txt",
        "team_contributions/jumi/main.py", "team_contributions/jumi/requirements.txt",
        "team_contributions/jumi/tests/test_text_splitter.py",
        "team_contributions/jumi/index_manifest.json",
    }
    excluded = {
        "assets/sujin/source.zip", "assets/sujin/.private.png", "assets/other/unused.png",
        "team_contributions/.env", "team_contributions/jumi/config.json",
        "team_contributions/jumi/chroma_db/chroma.sqlite3", "team_contributions/jumi/index.bin",
        "team_contributions/incoming.zip", "team_contributions/node_modules/library/index.py",
        "team_contributions/cache/previous.py", "team_contributions/caches/index_manifest.json",
        "team_contributions/logs/session.txt", "team_contributions/rawzips/notes.txt",
        "team_contributions/.venv/runtime.py", "team_contributions/jumi/__pycache__/compiled.py",
    }
    for relative in included | excluded:
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode("utf-8"))
    report = package.package()
    with ZipFile(report["path"]) as archive:
        manifest = json.loads(archive.read("jolup_team/MANIFEST.json"))
        entries = {row["path"]: row for row in manifest["files"]}
        assert included <= entries.keys()
        assert not excluded & entries.keys()
        for relative in included:
            content = archive.read("jolup_team/" + relative)
            assert content == (release / relative).read_bytes()
            assert hashlib.sha256(content).hexdigest() == entries[relative]["sha256"]


def test_release_missing_required_source_and_external_symlinks_are_rejected(release):
    (release / "README.md").unlink()
    with pytest.raises(ValueError, match="Required release"):
        package.selected_files()
    (release / "README.md").symlink_to(release / "data/sources/source.txt")
    with pytest.raises(ValueError, match="Symlinks"):
        package.selected_files()
    (release / "README.md").unlink()
    (release / "README.md").write_text("public")
    (release / "data/sources.json").write_text('[{"path":"data/sources/missing.txt"}]')
    with pytest.raises(ValueError, match="source.*missing"):
        package.selected_files()


def test_release_example_must_have_blank_credentials(release):
    example = release / ".env.example"
    example.write_text('# template\nblank line\nOPENAI_API_KEY=""\nMODEL=model\n')
    assert example in package.selected_files()
    example.write_text("OPENAI_API_KEY=fixture-secret")
    with pytest.raises(ValueError, match="nonempty API key"):
        package.selected_files()


@pytest.mark.parametrize("failure", ["crc", "hash"])
def test_release_detects_corrupted_archive(release, monkeypatch, failure):
    class CorruptArchive(ZipFile):
        def testzip(self):
            return "broken.txt" if failure == "crc" else None
        def read(self, *args, **kwargs):
            return b"corrupted" if failure == "hash" else super().read(*args, **kwargs)
    monkeypatch.setattr(package, "ZipFile", CorruptArchive)
    with pytest.raises(ValueError, match="integrity|hash mismatch"):
        package.package()


def test_package_cli_preflight_and_error_status(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["package_project.py", "--check"])
    runpy.run_path(str(ROOT / "scripts/package_project.py"), run_name="__main__")
    assert json.loads(capsys.readouterr().out)["status"] == "allowlist_checked"
    original = Path.read_text
    def fail_manifest(path, *args, **kwargs):
        if path == ROOT / "data/sources.json":
            raise OSError("fixture unreadable manifest")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", fail_manifest)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / "scripts/package_project.py"), run_name="__main__")
    assert error.value.code == 1 and "Packaging stopped" in capsys.readouterr().err


def test_public_fixture_generator_runs_in_isolated_destination(tmp_path, monkeypatch):
    # Import and CLI are both supported without modifying real school sources.
    importlib.import_module("scripts.prepare_data")
    monkeypatch.setattr(sys, "argv", ["prepare_data.py", "--output", str(tmp_path)])
    runpy.run_path(str(ROOT / "scripts/prepare_data.py"), run_name="__main__")
    rules = json.loads((tmp_path / "rules/sungkyul_cs_2024.json").read_text())
    assert (rules["total_credits"], rules["major_total"], rules["engineering_accreditation"]) == (123, 63, False)
    catalog = json.loads((tmp_path / "courses.json").read_text())["courses"]
    capstone = next(row for row in catalog if row["course_id"] == "CS_CAP2")
    assert capstone["prerequisites"] == ["CS_CAP1"]
    assert len({row["course_id"] for row in catalog}) == len(catalog)
    assert len(json.loads((tmp_path / "events.json").read_text())["events"]) == 38
    assert len(list((tmp_path / "sources").glob("*.txt"))) == 5


def test_sample_export_preserves_independent_expected_answers(tmp_path, monkeypatch, capsys):
    from jolup.samples import list_samples, load_sample
    importlib.import_module("scripts.export_samples")
    monkeypatch.setattr(sys, "argv", ["export_samples.py", "--output", str(tmp_path)])
    runpy.run_path(str(ROOT / "scripts/export_samples.py"), run_name="__main__")
    for item in list_samples():
        source = load_sample(item["id"])
        assert (tmp_path / f"{item['id']}.csv").read_bytes() == source["csv_bytes"]
        saved = json.loads((tmp_path / f"{item['id']}.json").read_text())
        assert saved["expected"] == source["expected"] and "csv_bytes" not in saved
    assert "No uploaded data" in capsys.readouterr().out


@pytest.mark.parametrize("args,backend,expected", [
    (["--lexical", "--force", "--query", "졸업"], "lexical", None),
    ([], "chroma_fastembed", None), ([], "lexical_fallback", 2),
])
def test_build_index_cli_never_reports_fallback_as_vector_success(monkeypatch, capsys, args, backend, expected):
    from jolup import rag
    importlib.import_module("scripts.build_index")
    calls = []
    class FakeKnowledgeBase:
        def __init__(self, mode):
            calls.append(mode)
        def build_index(self, force=False):
            calls.append(force)
            return {"backend": backend}
        def answer(self, query):
            calls.append(query)
            return {"answer": "근거"}
    monkeypatch.setattr(rag, "KnowledgeBase", FakeKnowledgeBase)
    monkeypatch.setattr(sys, "argv", ["build_index.py", *args])
    if expected is None:
        runpy.run_path(str(ROOT / "scripts/build_index.py"), run_name="__main__")
    else:
        with pytest.raises(SystemExit) as error:
            runpy.run_path(str(ROOT / "scripts/build_index.py"), run_name="__main__")
        assert error.value.code == expected
    assert calls[:2] == (["lexical", True] if "--lexical" in args else ["vector", False])
    assert backend in capsys.readouterr().out


def test_profile_report_handles_no_observed_edges(tmp_path):
    report = profile_study.run_profile("calculate", "on_track", 5)
    report["calls"] = []
    report["call_observation"]["calls"] = []
    report["measurement_warning"] = "cProfile 시간 통계가 일부 누락된 결과"
    paths = profile_study.write_reports(report, tmp_path)
    assert "시간 통계 주의" in paths[1].read_text()


def test_profile_cli_entrypoint(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["profile_study.py", "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / "scripts/profile_study.py"), run_name="__main__")
    assert error.value.code == 0 and "--sample" in capsys.readouterr().out
