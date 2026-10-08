"""Tests for repository, release and privacy contracts."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


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
    for job in jobs.values():
        assert job["if"] == "github.ref == 'refs/heads/main'"
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
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or ".venv" in path.parts
            or path.suffix
            not in {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".ambr"}
        ):
            continue
        found = set(pattern.findall(path.read_text(errors="ignore"))) - allowed
        assert not found, path
