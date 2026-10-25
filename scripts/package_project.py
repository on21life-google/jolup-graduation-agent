"""Create a shareable allowlisted ZIP; no keys, local caches, or uploads.

Run with Python 3.12: python scripts/package_project.py [--check]
Output always stays inside the project's output/ directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "README.md", "app.py", "requirements.txt", "requirements.lock", "Dockerfile",
    "compose.yaml", ".dockerignore", ".gitignore", ".env.example", ".streamlit/config.toml",
    "compose.quality.yaml", ".coveragerc", "requirements-quality.txt", "sonar-project.properties",
}
TREE_RULES = {
    "jolup": {".py"}, "tests": {".py"}, "docs": {".md", ".txt", ".json"},
    "scripts": {".py", ".command", ".bat"},
    "learning/tdd": {".py", ".md", ".json"},
    "data/rules": {".json"}, "data/samples": {".csv", ".json"},
    "data/sources": {".pdf", ".txt"},
}
DATA_FILES = {"data/courses.json", "data/events.json", "data/sources.json"}
EXCLUDED_PARTS = {".env", ".venv", ".cache", ".build", ".git", "__pycache__", ".pytest_cache", "node_modules"}
REQUIRED = {"README.md", "app.py", "requirements.txt", "requirements.lock", "Dockerfile", "compose.yaml",
            "docs/RUN.md", "scripts/run.command", "scripts/run.bat", "data/sources.json",
            "docs/TDD_학습가이드.md", "docs/tdd/requirements.json", "docs/tdd/추적표.md",
            "scripts/tdd_study.py", "tests/test_tdd_study.py", "learning/tdd/checks.py",
            "learning/tdd/replay_spec.json", "learning/tdd/README.md",
            "learning/tdd/stages/red.py", "learning/tdd/stages/green.py", "learning/tdd/stages/refactor.py"}
REQUIRED |= {"compose.quality.yaml", ".coveragerc", "requirements-quality.txt", "sonar-project.properties",
             "scripts/quality_check.py", "scripts/sonar_local.py", "docs/SONARQUBE.md", "docs/SONARQUBE_코드읽기.md"}


def _tree_files(tree, extensions):
    for path in (ROOT / tree).rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_PARTS or part.startswith(".") for part in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


def _check_selected_paths(selected):
    for path in selected:
        if path.is_symlink() or ROOT not in path.resolve().parents:
            raise ValueError(f"Symlinks and external paths are not packaged: {path.relative_to(ROOT)}")
    missing = REQUIRED - {path.relative_to(ROOT).as_posix() for path in selected}
    if missing:
        raise ValueError("Required release files are missing: " + ", ".join(sorted(missing)))


def _check_example_credentials():
    example = ROOT / ".env.example"
    if example.is_file():
        for line in example.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().endswith("API_KEY") and value.strip().strip("\"'"):
                raise ValueError(".env.example contains a nonempty API key. Keep example key values blank before sharing.")


def _check_public_sources(selected):
    source_manifest = json.loads((ROOT / "data/sources.json").read_text(encoding="utf-8"))
    source_records = source_manifest.get("sources", []) if isinstance(source_manifest, dict) else source_manifest
    selected_names = {path.relative_to(ROOT).as_posix() for path in selected}
    for source in source_records:
        name = str(source.get("path", ""))
        if not name.startswith("data/sources/") or name not in selected_names:
            raise ValueError(f"A source in sources.json is missing from the public-source package: {name}")


def selected_files() -> list[Path]:
    selected = {ROOT / name for name in ROOT_FILES | DATA_FILES if (ROOT / name).is_file()}
    for tree, extensions in TREE_RULES.items():
        selected.update(_tree_files(tree, extensions))
    _check_selected_paths(selected)
    _check_example_credentials()
    _check_public_sources(selected)
    return sorted(selected, key=lambda path: path.relative_to(ROOT).as_posix())


def package(check_only: bool = False) -> dict:
    files = selected_files()
    manifest = {
        "project": "졸업로드맵 에이전트", "created_at": datetime.now(timezone.utc).isoformat(),
        "student_data": "Only built-in fictional samples; uploaded records and API keys are excluded.",
        "files": [{"path": p.relative_to(ROOT).as_posix(), "bytes": p.stat().st_size,
                   "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files],
    }
    report = {"file_count": len(files), "source_bytes": sum(item["bytes"] for item in manifest["files"]),
              "excluded": ["API keys/.env", ".venv", ".cache", ".build", "logs", "indexes/models", "uploaded records", "output", "reports"]}
    if check_only:
        report["status"] = "allowlist_checked"
        return report
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = output / f"졸업로드맵_팀공유_{stamp}.zip"
    prefix = "jolup_team"
    with ZipFile(destination, "x", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, f"{prefix}/{path.relative_to(ROOT).as_posix()}")
        archive.writestr(f"{prefix}/MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    # Validate names, content hashes, and CRC without extracting anything.
    with ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        for item in manifest["files"]:
            if hashlib.sha256(archive.read(f"{prefix}/{item['path']}")).hexdigest() != item["sha256"]:
                raise ValueError(f"ZIP content hash mismatch: {item['path']}")
    report.update(status="packaged_and_hash_verified", path=str(destination), archive_bytes=destination.stat().st_size)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the allowlisted team ZIP without secrets or runtime data.")
    parser.add_argument("--check", action="store_true", help="Validate the release file list without creating a ZIP")
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.check), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Packaging stopped: {exc}\n")
