"""Dependency pins: audited versions, explicit transitives, framework signature.

The client image ships a fully pinned `requirements.txt`, and several of those
pins carried published advisories (cryptography, starlette via fastapi,
python-multipart, jinja2, pyarrow, python-dotenv) while three security-relevant
transitives (starlette, joserfc, pillow) were not pinned at all and floated at
build time. The remediation bumps every audited pin to at least its highest fix
version, pins the three transitives explicitly, replaces the Authlib comment
that justified the old `cryptography<45` constraint, upgrades pip inside the
Dockerfile before the requirements install (pip itself had advisories), and
moves the nine `TemplateResponse(name, context)` calls to the Starlette-1
`TemplateResponse(request, name, context)` signature that the bump requires.
These tests parse `requirements.txt`, the `Dockerfile`, `app.py` and
`routes/*.py` as TEXT, and check the installed metadata of the venv, so a
regression in any of them fails the suite without Docker.

Every assertion binds the value to a LOCAL first so a failure prints that
value, never a `Settings` repr — same rule as tests/test_container_hardening_config.py.
"""
import importlib.metadata
import re
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
DOCKERFILE = ROOT / "Dockerfile"
TEMPLATE_SOURCES = [ROOT / "app.py"] + sorted((ROOT / "routes").glob("*.py"))

# Highest fix version per audited package (pip-audit, PyPI + OSV advisories).
FIX_VERSIONS = {
    "cryptography": "49.0.0",
    "starlette": "1.3.1",
    "python-multipart": "0.0.31",
    "jinja2": "3.1.6",
    "pyarrow": "23.0.1",
    "python-dotenv": "1.2.2",
}

# Security-relevant transitives that fastapi / Authlib / matplotlib would
# otherwise leave to the resolver at build time.
PRIORITY_TRANSITIVES = ["starlette", "joserfc", "pillow"]

# Packages whose installed version must equal the pin (the priority set).
INSTALLED_MUST_MATCH = [
    "fastapi",
    "starlette",
    "cryptography",
    "Authlib",
    "joserfc",
    "python-multipart",
    "jinja2",
    "pyarrow",
    "pillow",
]

MIN_PIP_VERSION = "26.2.1"
MIN_AUTHLIB_VERSION = "1.8.0"
MIN_CRYPTOGRAPHY_FOR_AUTHLIB = "45.0.1"   # Authlib 1.8's own lower bound


# ---------------------------------------------------------------------------
# requirements.txt helpers
# ---------------------------------------------------------------------------
def _normalize(name: str) -> str:
    """PEP 503 name normalization, extras stripped: `uvicorn[standard]` -> `uvicorn`."""
    base = name.split("[", 1)[0].strip()
    return re.sub(r"[-_.]+", "-", base).lower()


def _requirements_text() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


def _pins() -> dict:
    """{normalized name: version string} for every `name==version` line.

    Comment and blank lines are ignored; a trailing ` # comment` is dropped.
    """
    out = {}
    for raw in _requirements_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?)\s*==\s*([A-Za-z0-9.+!-]+)$", line)
        if m:
            out[_normalize(m.group(1))] = m.group(2)
    return out


def _pinned_version(name: str):
    return _pins().get(_normalize(name))


# ---------------------------------------------------------------------------
# Dockerfile helpers
# ---------------------------------------------------------------------------
def _logical_lines():
    """[(first_line_index, joined_text)] with backslash-continuations joined
    and comment lines dropped — so a RUN split over several lines is one
    instruction whose position is the index of its first source line."""
    out = []
    start = None
    buf = None
    for idx, raw in enumerate(DOCKERFILE.read_text(encoding="utf-8").splitlines()):
        if raw.strip().startswith("#"):
            continue
        if buf is None:
            if not raw.strip():
                continue
            start = idx
            buf = raw.rstrip()
        else:
            buf = buf[:-1].rstrip() + " " + raw.strip()
        if buf.endswith("\\"):
            continue
        out.append((start, buf))
        buf = None
    if buf is not None:
        out.append((start, buf))
    return out


# ---------------------------------------------------------------------------
# (1) every audited pin is at or above its fix version
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package,fix", sorted(FIX_VERSIONS.items()), ids=sorted(FIX_VERSIONS))
def test_audited_pins_meet_fix_versions(package, fix):
    """Each package pip-audit flagged is pinned at >= the highest fix version.

    A MISSING pin fails too: an unpinned package floats at build time, so the
    image could ship any version — including a vulnerable one.
    """
    pinned = _pinned_version(package)
    assert pinned is not None, (
        f"{package} has no `==` pin in requirements.txt "
        f"(needs >= {fix}); pinned names: {sorted(_pins())}"
    )
    pinned_v = Version(pinned)
    fix_v = Version(fix)
    assert pinned_v >= fix_v, f"{package}=={pinned} is below the fix version {fix}"


# ---------------------------------------------------------------------------
# (2) the priority transitives are pinned explicitly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", PRIORITY_TRANSITIVES)
def test_priority_transitives_are_pinned_explicitly(package):
    """starlette (fastapi's), joserfc (Authlib's) and pillow (matplotlib's)
    are compiled or security-relevant transitives. Without an explicit `==`
    pin the resolver picks whatever satisfies the parent's open-ended bound
    at build time, so two builds of the same commit can ship different
    versions — and the audited version is not the shipped one."""
    pinned = _pinned_version(package)
    assert pinned is not None, (
        f"{package} is not pinned in requirements.txt — it would float at "
        f"build time; add an explicit `{package}==<version>` line"
    )


# ---------------------------------------------------------------------------
# (3) the Authlib block no longer justifies the old cryptography constraint
# ---------------------------------------------------------------------------
def test_authlib_comment_no_longer_claims_the_old_constraint():
    """The old comment kept Authlib on 1.6.x because 1.7+ (joserfc) forces
    cryptography>=45 and 'would break the cryptography==43.0.3 pin'. That
    constraint is the thing being removed: the comment must go with it, and
    the pins must reflect the new state (Authlib 1.8+, cryptography >= the
    lower bound Authlib 1.8 declares)."""
    text = _requirements_text()
    claims_old_branch = "1.6.x on purpose" in text
    assert not claims_old_branch, "requirements.txt still says Authlib is '1.6.x on purpose'"
    mentions_old_pin = "cryptography==43.0.3" in text
    assert not mentions_old_pin, "requirements.txt still mentions cryptography==43.0.3"

    authlib = _pinned_version("Authlib")
    assert authlib is not None, f"Authlib not pinned; pinned names: {sorted(_pins())}"
    authlib_v = Version(authlib)
    assert authlib_v >= Version(MIN_AUTHLIB_VERSION), f"Authlib=={authlib} < {MIN_AUTHLIB_VERSION}"

    crypto = _pinned_version("cryptography")
    assert crypto is not None, f"cryptography not pinned; pinned names: {sorted(_pins())}"
    crypto_v = Version(crypto)
    assert crypto_v >= Version(MIN_CRYPTOGRAPHY_FOR_AUTHLIB), (
        f"cryptography=={crypto} < {MIN_CRYPTOGRAPHY_FOR_AUTHLIB} (Authlib {MIN_AUTHLIB_VERSION}'s lower bound)"
    )


# ---------------------------------------------------------------------------
# (4) the Dockerfile upgrades pip BEFORE installing requirements
# ---------------------------------------------------------------------------
def test_dockerfile_upgrades_pip_before_requirements():
    """pip is an image tool, not an application dependency, so its advisories
    are closed in the Dockerfile: a `RUN pip install ... --upgrade ... pip==X`
    (X >= 26.2.1) must come BEFORE `RUN pip install --no-cache-dir -r
    requirements.txt` — the install itself runs with the fixed pip — and
    before `USER pdc` (root is still needed to write site-packages)."""
    lines = _logical_lines()
    pip_re = re.compile(r"pip install .*--upgrade .*pip==(\d+\.\d+(\.\d+)?)")

    upgrade_hits = [
        (idx, text, pip_re.search(text).group(1))
        for idx, text in lines
        if text.startswith("RUN") and pip_re.search(text)
    ]
    assert upgrade_hits, (
        "no `RUN pip install ... --upgrade ... pip==<version>` instruction in the Dockerfile; "
        f"RUN lines: {[t for _, t in lines if t.startswith('RUN')]}"
    )
    upgrade_idx, upgrade_text, upgrade_version = upgrade_hits[0]
    upgrade_v = Version(upgrade_version)
    assert upgrade_v >= Version(MIN_PIP_VERSION), f"pip=={upgrade_version} < {MIN_PIP_VERSION}: {upgrade_text}"

    req_hits = [(idx, text) for idx, text in lines
                if text == "RUN pip install --no-cache-dir -r requirements.txt"]
    assert req_hits, [t for _, t in lines if t.startswith("RUN")]
    req_idx = req_hits[0][0]
    assert upgrade_idx < req_idx, (
        f"pip upgrade (line {upgrade_idx + 1}) must precede the requirements install (line {req_idx + 1})"
    )

    user_hits = [idx for idx, text in lines if text == "USER pdc"]
    assert user_hits, [t for _, t in lines if t.startswith("USER")]
    user_idx = user_hits[-1]
    assert upgrade_idx < user_idx, (
        f"pip upgrade (line {upgrade_idx + 1}) must precede `USER pdc` (line {user_idx + 1})"
    )


# ---------------------------------------------------------------------------
# (5) Starlette-1 TemplateResponse signature everywhere
# ---------------------------------------------------------------------------
def _template_response_offenders():
    """[(relative path, 1-based line)] of every `TemplateResponse(` whose first
    non-whitespace argument starts with a quote, i.e. the removed
    `TemplateResponse(name, context)` form."""
    offenders = []
    for path in TEMPLATE_SOURCES:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"TemplateResponse\(", text):
            rest = text[m.end():].lstrip()
            first = rest[:1]
            if first in ('"', "'"):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append((path.relative_to(ROOT).as_posix(), line))
    return offenders


def test_template_response_uses_request_first():
    """Starlette 1.0 removed `TemplateResponse(name, context)`; the only form
    left is `TemplateResponse(request, name, context)`. Every call in app.py
    and routes/*.py must pass `request` first — a string literal there is
    the old signature and raises at request time under the bumped pin."""
    offenders = _template_response_offenders()
    n_offenders = len(offenders)
    assert n_offenders == 0, (
        "TemplateResponse called with a string literal first (old signature) at: "
        + ", ".join(f"{p}:{ln}" for p, ln in offenders)
    )


# ---------------------------------------------------------------------------
# (6) the venv runs exactly what the image ships
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("package", INSTALLED_MUST_MATCH)
def test_installed_versions_match_requirements(package):
    """The suite must run against exactly what the image ships: the installed
    version of each priority package equals its `requirements.txt` pin. A
    stale venv is a real finding (tests passing against an old framework say
    nothing about the shipped one) — the fix is
    `python -m pip install -r requirements.txt`, never a looser assertion."""
    pinned = _pinned_version(package)
    assert pinned is not None, f"{package} has no `==` pin in requirements.txt"
    try:
        installed = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pytest.fail(
            f"{package} is pinned at {pinned} but not installed in this environment; "
            f"reinstall requirements.txt"
        )
    installed_v = Version(installed)
    pinned_v = Version(pinned)
    assert installed_v == pinned_v, (
        f"{package}: installed {installed} != pinned {pinned}; reinstall requirements.txt"
    )
