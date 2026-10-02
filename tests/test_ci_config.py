"""The CI workflow, CODEOWNERS and the accepted-findings list stay what the
branch-protection guidance (docs/BUILD_AND_RUN.md section 9) relies on:
three required checks named `tests`, `executor-tests` and `images`, run on
every push and pull request; the suite as a non-root user; the executor
suite as root and as the sandbox uid; pip-audit with both advisory services
and Trivy on both images; neither image containing pip (the installed sets
are read with importlib.metadata); code-owner review over the boundary
files."""
import re
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


def _trivyignore_entries() -> list:
    """The finding ids the file accepts: comments (whole-line and trailing)
    and blank lines dropped, first token of each remaining line."""
    out = []
    for raw in TRIVYIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line.split()[0])
    return out


def test_every_accepted_trivy_finding_is_documented_in_the_releases():
    """The file may hold NO entry; every entry that is present must be
    recorded in RELEASES.md."""
    assert TRIVYIGNORE.is_file(), "CI mounts .trivyignore into both Trivy steps"
    ids = [line.strip() for line in TRIVYIGNORE.read_text(encoding="utf-8").splitlines()
           if line.strip() and not line.strip().startswith("#")]
    releases = (ROOT / "RELEASES.md").read_text(encoding="utf-8")
    missing = [i for i in ids if i not in releases]
    assert missing == [], missing


@pytest.mark.parametrize("cve", ["CVE-2026-97687", "CVE-2026-97689"])
def test_the_urllib3_findings_are_fixed_not_ignored(cve):
    """The urllib3 findings are closed by the `urllib3==2.8.0` pin and by
    removing pip (whose vendored copy was the flagged one) — never by an
    ignore entry."""
    entries = [e.upper() for e in _trivyignore_entries()]
    assert cve not in entries, entries


def _image_steps() -> list:
    return [str(s.get("run", "")) for s in _workflow()["jobs"]["images"]["steps"]]


def test_the_images_job_never_runs_pip_inside_the_images():
    """Neither image contains pip, so the installed set is read with
    importlib.metadata; a `pip list` / `pip freeze` anywhere in the job would
    either fail or mean pip is back in an image."""
    text = "\n".join(_image_steps())
    listing = re.findall(r"pip3?\s+(?:list|freeze)\b", text)
    assert listing == [], listing
    assert "--entrypoint pip" not in text, text
    listers = [run for run in _image_steps() if "importlib.metadata" in run]
    assert len(listers) == 1, listers
    assert "powerdatachat-client:ci" in listers[0], listers[0]
    assert "powerdatachat-executor:ci" in listers[0], listers[0]


def test_a_step_proves_neither_image_contains_pip():
    steps = [run for run in _image_steps()
             if re.search(r"""!\s*python\s+-c\s+["']import pip["']""", run)]
    assert len(steps) == 1, _image_steps()
    step = steps[0]
    assert "powerdatachat-client:ci" in step and "powerdatachat-executor:ci" in step, step
    assert "docker run" in step, step
    # a launcher left on PATH counts as pip too
    assert "/usr/local/bin/pip" in step, step


def test_pip_audit_keeps_both_services_and_trivy_still_fails_the_job():
    runs = _image_steps()
    audit = [run for run in runs if "pip-audit -r" in run]
    assert len(audit) == 1, runs
    assert re.search(r"for\s+s\s+in\s+pypi\s+osv\b", audit[0]), audit[0]
    assert re.search(r"-s\s+\"?\$s\"?", audit[0]), audit[0]
    assert "web-freeze.txt" in audit[0] and "executor-freeze.txt" in audit[0], audit[0]
    trivy = [run for run in runs if "aquasec/trivy:" in run]
    assert len(trivy) == 2, runs
    for run in trivy:
        assert re.search(r"--exit-code\s+1\b", run), run
    images = sorted(img for run in trivy
                    for img in ("powerdatachat-client:ci", "powerdatachat-executor:ci") if img in run)
    assert images == ["powerdatachat-client:ci", "powerdatachat-executor:ci"], images
