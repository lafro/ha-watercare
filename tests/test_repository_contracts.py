"""Tests for repository, release and privacy contracts."""

from __future__ import annotations

import json
import os
import re
import tomllib
from importlib.metadata import requires, version
from pathlib import Path

import pytest
import yaml
from packaging.version import Version

from scripts import check_versions

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REPOSITORY = "https://github.com/lafro/ha-watercare"
TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".ambr", ".sh", ""}
SKIPPED_PARTS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "htmlcov",
}


def _text_files() -> list[Path]:
    files: list[Path] = []
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in SKIPPED_PARTS]
        files.extend(
            Path(folder, name)
            for name in names
            if name not in {"settings.local.json", ".coverage"}
            and Path(name).suffix in TEXT_SUFFIXES
        )
    return files


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_every_action_is_pinned_to_a_commit() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text().splitlines():
            match = re.search(r"uses:\s*([^\s#]+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            assert re.search(r"@[0-9a-f]{40}$", match.group(1)), (path.name, line)


def test_release_publishes_only_the_verified_main_commit() -> None:
    workflow = _workflow("release.yml")
    assert set(workflow[True]) == {"workflow_dispatch"}
    jobs = workflow["jobs"]
    assert jobs["release"]["needs"] == ["python", "hassfest", "hacs"]
    for name in ("python", "hassfest", "hacs"):
        assert jobs[name]["if"] == "github.ref == 'refs/heads/main'"
    # Only the renamed repository publishes (docs/release.md cut-over).
    assert jobs["release"]["if"] == (
        "github.ref == 'refs/heads/main' && github.repository == 'lafro/ha-watercare'"
    )
    script = "\n".join(str(step.get("run", "")) for step in jobs["release"]["steps"])
    assert "git/ref/tags/$RELEASE_TAG" in script
    assert '"$GITHUB_SHA"' in script
    assert "--verify-tag" in script
    assert "--target" not in script


def test_release_never_edits_the_manifest() -> None:
    jobs = _workflow("release.yml")["jobs"]
    scripts = "\n".join(
        str(step.get("run", "")) for job in jobs.values() for step in job["steps"]
    )
    for forbidden in ("yq", "manifest.json", "sed ", "zip", "git commit", "git push"):
        assert forbidden not in scripts, forbidden


def test_release_validators_use_isolated_workspaces() -> None:
    jobs = _workflow("release.yml")["jobs"]
    for job_name in ("hassfest", "hacs"):
        uses = [step.get("uses", "") for step in jobs[job_name]["steps"]]
        assert any("actions/checkout@" in action for action in uses)
        assert not any("setup-uv@" in action for action in uses)


def test_versions_agree_and_have_no_prefix() -> None:
    manifest = json.loads(
        (ROOT / "custom_components/watercare/manifest.json").read_text()
    )
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    assert manifest["version"] == project["project"]["version"]
    assert not manifest["version"].startswith("v")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## {manifest['version']}" in changelog


def test_hacs_requires_a_minimum_home_assistant() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["homeassistant"]
    assert "zip_release" not in hacs


def test_fixtures_use_only_synthetic_identifiers() -> None:
    # Watercare account numbers look like seven digits, a dash and two
    # digits. Only the synthetic ones may appear anywhere in the repository.
    allowed = {"1000001-01", "2000002-02"}
    pattern = re.compile(r"\b\d{7}-\d{2}\b")
    for path in _text_files():
        found = set(pattern.findall(path.read_text(errors="ignore"))) - allowed
        assert not found, path


def test_no_meter_id_shaped_values() -> None:
    # Meter ids are short runs of capital letters and digits. Synthetic ones
    # in this repository are spelled with dashes (TEST-METER-0001), so any
    # unbroken 8 to 14 character mix with at least two letters and five
    # digits is suspect.
    pattern = re.compile(
        r"\b(?=(?:[A-Z0-9]*[A-Z]){2})(?=(?:[A-Z0-9]*\d){5})[A-Z0-9]{8,14}\b"
    )
    assert pattern.search("X12" + "Y345678")  # split, so this file passes
    assert not pattern.search("TEST-METER-0001")
    for path in _text_files():
        found = pattern.findall(path.read_text(errors="ignore"))
        assert not found, path


def test_emails_are_example_addresses_only() -> None:
    pattern = re.compile(r"[\w.%+-]+@([\w-]+(?:\.[\w-]+)+)")
    allowed = {"example.com", "example.org", "example.invalid"}
    for path in _text_files():
        domains = set(pattern.findall(path.read_text(errors="ignore")))
        assert domains <= allowed, (path, domains - allowed)


def test_public_links_name_the_repository_hacs_installs() -> None:
    manifest = json.loads(
        (ROOT / "custom_components/watercare/manifest.json").read_text()
    )
    assert manifest["documentation"] == REPOSITORY
    assert manifest["issue_tracker"] == f"{REPOSITORY}/issues"
    const = (ROOT / "custom_components/watercare/const.py").read_text()
    assert f'DOCS_URL: Final = "{REPOSITORY}"' in const
    for path in _text_files():
        text = path.read_text(errors="ignore")
        for link in re.findall(r"github\.com/lafro/[\w.-]+", text):
            assert link == "github.com/lafro/ha-watercare", (path, link)


def test_every_checkout_drops_the_job_token() -> None:
    """No later step, including unpinned compat installs, can reuse the token."""
    for path in WORKFLOWS.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "actions/checkout@" in step.get("uses", ""):
                    assert step.get("with", {}).get("persist-credentials") is False, (
                        path.name
                    )


def test_release_gates_skip_the_cache_and_publish_changelog_notes() -> None:
    workflow = _workflow("release.yml")
    python_job = workflow["jobs"]["python"]
    setup_uv = next(
        step for step in python_job["steps"] if "setup-uv@" in step.get("uses", "")
    )
    assert setup_uv["with"]["enable-cache"] is False

    versions = next(
        step for step in python_job["steps"] if step.get("id") == "versions"
    )
    assert "check_versions.py" in versions["run"]
    assert "--release-notes" in versions["run"]
    assert '--repository "$GITHUB_REPOSITORY"' in versions["run"]
    assert python_job["outputs"]["notes"] == "${{ steps.versions.outputs.notes }}"

    publish = workflow["jobs"]["release"]["steps"][-1]
    assert publish["env"]["RELEASE_NOTES"] == "${{ needs.python.outputs.notes }}"
    assert "--notes-file" in publish["run"]


def test_compat_failures_open_a_tracking_issue() -> None:
    workflow = _workflow("compat.yml")
    report = workflow["jobs"]["report"]
    assert report["needs"] == "tests"
    assert report["if"] == "failure() && github.event_name == 'schedule'"
    assert report["permissions"] == {"issues": "write"}
    assert workflow["permissions"] == {"contents": "read"}
    script = report["steps"][0]["run"]
    # --force makes an existing label a success, so no error is swallowed.
    assert "gh label create compat --force --color e99695" in script
    assert "|| true" not in script
    assert 'gh issue create --title "$title" --body "$body" --label compat' in script


def _fake_root(root: Path, version_: str = "9.8.7") -> Path:
    (root / "custom_components/watercare").mkdir(parents=True)
    (root / "custom_components/watercare/manifest.json").write_text(
        json.dumps(
            {
                "version": version_,
                "documentation": "https://github.com/owner/repo",
                "issue_tracker": "https://github.com/owner/repo/issues",
            }
        )
    )
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{version_}"\n')
    return root / "CHANGELOG.md"


def test_check_versions_requires_a_changelog_section(tmp_path: Path) -> None:
    changelog = _fake_root(tmp_path)

    changelog.write_text("# Changelog\n\n## 9.8.6\n\n- Older.\n")
    with pytest.raises(SystemExit, match=r"no '## 9\.8\.7' section"):
        check_versions.main([], tmp_path)

    changelog.write_text("# Changelog\n\n## 9.8.7\n\n## 9.8.6\n\n- Older.\n")
    with pytest.raises(SystemExit, match="is empty"):
        check_versions.main([], tmp_path)

    changelog.write_text(
        "# Changelog\n\n## 9.8.7\n\n### Fixed\n\n- New.\n\n## 9.8.6\n\n- Older.\n"
    )
    notes = tmp_path / "notes.md"
    check_versions.main(["v9.8.7", "--release-notes", str(notes)], tmp_path)
    assert notes.read_text() == "### Fixed\n\n- New.\n"

    with pytest.raises(SystemExit, match="Tag mismatch"):
        check_versions.main(["v9.8.8"], tmp_path)
    with pytest.raises(SystemExit, match="needs a value"):
        check_versions.main(["--release-notes"], tmp_path)
    with pytest.raises(SystemExit, match="Unexpected argument"):
        check_versions.main(["v9.8.7", "extra"], tmp_path)


def test_check_versions_rejects_bad_versions(tmp_path: Path) -> None:
    _fake_root(tmp_path, "v9.8.7")
    with pytest.raises(SystemExit, match="must not start with v"):
        check_versions.main([], tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    manifest = tmp_path / "custom_components/watercare/manifest.json"
    manifest.write_text('{"version": "9.8.7"}')
    with pytest.raises(SystemExit, match="Version mismatch"):
        check_versions.main([], tmp_path)


def test_check_versions_requires_the_release_repository(tmp_path: Path) -> None:
    changelog = _fake_root(tmp_path)
    changelog.write_text("# Changelog\n\n## 9.8.7\n\n- New.\n")

    check_versions.main(["v9.8.7", "--repository", "owner/repo"], tmp_path)
    check_versions.main(["--repository=owner/repo"], tmp_path)
    with pytest.raises(SystemExit, match="cut-over"):
        check_versions.main(["v9.8.7", "--repository", "owner/repo-next"], tmp_path)


def test_repository_versions_and_changelog_agree(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    check_versions.main(["--release-notes=" + str(notes)])
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    section = check_versions.changelog_section(
        project["project"]["version"], (ROOT / "CHANGELOG.md").read_text()
    )
    assert notes.read_text() == section
    assert section.strip()
    # The release only goes out from the repository the manifest names.
    check_versions.main(["--repository", "lafro/ha-watercare"])
    with pytest.raises(SystemExit, match="cut-over"):
        check_versions.main(["--repository", "lafro/ha-watercare-next"])


def test_home_assistant_under_test_matches_the_harness() -> None:
    """The uv override may swap the harness's beta for that release only.

    pyproject.toml overrides homeassistant, so a harness bump that pins a
    newer release would otherwise still test the old one and pass.
    """
    harness_pin = next(
        requirement
        for requirement in requires("pytest-homeassistant-custom-component") or []
        if requirement.replace(" ", "").startswith("homeassistant==")
    )
    pinned = Version(harness_pin.split("==", 1)[1])
    installed = Version(version("homeassistant"))
    assert installed.base_version == pinned.base_version, (installed, pinned)
    assert not installed.is_prerelease
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    overrides = project["tool"]["uv"].get("override-dependencies", [])
    if not pinned.is_prerelease:
        assert not overrides, "The harness pins a release now: drop the override"


def test_dependabot_never_bumps_home_assistant_on_its_own() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    updates = {update["package-ecosystem"]: update for update in config["updates"]}
    assert set(updates) == {"uv", "github-actions"}
    ignored = [item["dependency-name"] for item in updates["uv"].get("ignore", [])]
    assert ignored == ["homeassistant"]


def test_dependabot_labels_are_explicit() -> None:
    """Without labels, Dependabot creates and applies github_actions."""
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    labels = {
        update["package-ecosystem"]: update["labels"] for update in config["updates"]
    }
    assert labels == {
        "uv": ["dependencies", "python"],
        "github-actions": ["dependencies", "github-actions"],
    }
