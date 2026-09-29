"""The CI workflow, CODEOWNERS and the accepted-findings list stay what the
branch-protection guidance (docs/BUILD_AND_RUN.md section 9) relies on:
three required checks named `tests`, `executor-tests` and `images`, run on
every push and pull request; the suite as a non-root user; the executor
suite as root and as the sandbox uid; pip-audit with both advisory services
and Trivy on both images; code-owner review over the boundary files."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
TRIVYIGNORE = ROOT / ".trivyignore"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps_text(job: str) -> str:
    steps = _workflow()["jobs"][job]["steps"]
    return "\n".join(str(s.get("run", "")) + " " + str(s.get("uses", "")) for s in steps)


def test_the_workflow_runs_on_every_push_and_pull_request():
    doc = _workflow()
    triggers = doc.get("on", doc.get(True))      # YAML 1.1 reads `on` as True
    assert "push" in triggers and "pull_request" in triggers, triggers
    assert triggers["push"] in (None, {}) or "branches" not in (triggers["push"] or {}), triggers


def test_the_three_required_checks_exist_by_name():
    jobs = _workflow()["jobs"]
    assert {jobs[j].get("name", j) for j in jobs} >= {"tests", "executor-tests", "images"}, jobs


def test_the_suite_runs_in_full_and_not_as_root():
    text = _steps_text("tests")
    assert 'test "$(id -u)" != "0"' in text, text
    assert "python -m pytest tests/ " in text, text
    assert "--ignore" not in text and " -k " not in text, "the suite must run in full"


def test_the_executor_suite_runs_as_root_and_as_the_sandbox_uid():
    text = _steps_text("executor-tests")
    assert "--target test" in text, text
    assert "--user 10002:10001" in text, text
    assert text.count("python -m pytest executor/tests") == 2, text


def test_both_images_are_audited_and_scanned():
    text = _steps_text("images")
    assert "docker build -t powerdatachat-client:ci ." in text, text
    assert "docker build -f executor/Dockerfile -t powerdatachat-executor:ci ." in text, text
    assert "pip-audit" in text and "--disable-pip" in text, text
    assert "for s in pypi osv" in text, text
    assert text.count("aquasec/trivy:") == 2, text
    assert text.count("--exit-code 1") == 2, text
    assert "--severity HIGH,CRITICAL" in text, text


@pytest.mark.parametrize("path", [
    "/sandbox_guard.py", "/executor/", "/exec_transport.py", "/executor_client.py",
    "/docker-compose*.yml", "/Dockerfile*", "/local_store.py", "/db_connector.py",
    "/html_sanitize.py", "/routes/charts.py", "/routes/report.py", "/logger_utils.py",
    "/.github/",
])
def test_codeowners_covers_the_boundary_files(path):
    rules = {}
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts and not parts[0].startswith("#"):
            rules[parts[0]] = parts[1:]
    assert rules.get(path) == ["@aleksloma"], (path, rules)


def test_every_accepted_trivy_finding_is_documented_in_the_releases():
    ids = [line.strip() for line in TRIVYIGNORE.read_text(encoding="utf-8").splitlines()
           if line.strip() and not line.strip().startswith("#")]
    assert ids, "empty .trivyignore"
    releases = (ROOT / "RELEASES.md").read_text(encoding="utf-8")
    missing = [i for i in ids if i not in releases]
    assert missing == [], missing
