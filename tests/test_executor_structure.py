"""Structural pins for the `pdc-executor` image and package
(devbox text parsing; no Docker, no import of the executor package).

`executor/requirements.txt` must be an identical-version SUBSET of the
audited root `requirements.txt`: exactly the required list, none of the
forbidden packages (DB drivers, cryptography, Authlib, httpx, urllib3,
kaleido, google-*, ...). `executor/Dockerfile` must build the unprivileged
sandbox image: the same base image and pip line as the root Dockerfile, pip
REMOVED again after the requirements install (only the `test` stage restores
it, through ensurepip), uid 10002 in
gid 10001, `USER pdcexec` before `CMD`, a HEALTHCHECK on `/healthz`, the
thread/allocator ENV set the runner needs, the plotly bake line, and a COPY
set limited to the modules the runner imports (never `COPY . .`, never
`local_store.py` / `db_*` / `brain_client.py` / `routes` / `app.py` /
`.env`). The Python sources must import none of the denied modules, the app
must read only its own env names and never use `JSONResponse` (NaN), and the
runner/app must carry the resource-limit and stray-sweep markers.

THE DEFAULT-TARGET INVARIANT. Every Dockerfile pin above is asserted against
the EFFECTIVE DEFAULT BUILD TARGET, never against a stage that happens to be
called "runtime": `docker build` builds the LAST stage when no `--target` is
given, and a compose `build:` block gives none. So the default image is the
last stage plus the local stages it inherits through `FROM <stage>` —
`_default_chain` resolves that chain and `_runtime_instructions` flattens it.
The stage order is `app` (everything, ending `USER pdcexec`) -> `test` (root +
pytest) -> a trailing, instruction-free `FROM app AS runtime`, and that
trailing stage is the whole point: with `test` last, a plain `docker build`
produced an image with `Config.User=root` AND pytest installed while every
assertion keyed on the stage NAME "runtime" still passed.

The parsing helpers are PRIVATE copies of the idioms in
`tests/test_dependency_pins.py` (`_normalize`, the `name==version` regex)
and `tests/test_container_hardening_config.py` (`_instructions`,
`_raw_instruction_block`) — cross-test imports are not the suite's
convention. Every file is asserted to exist first with a clear message, so a
missing file names itself instead of surfacing as a parse error.
"""
import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ROOT_REQUIREMENTS = ROOT / "requirements.txt"
ROOT_DOCKERFILE = ROOT / "Dockerfile"
EXEC_REQUIREMENTS = ROOT / "executor" / "requirements.txt"
EXEC_DOCKERFILE = ROOT / "executor" / "Dockerfile"
EXEC_INIT = ROOT / "executor" / "__init__.py"
EXEC_APP = ROOT / "executor" / "app.py"
EXEC_RUNNER = ROOT / "executor" / "runner.py"
EXEC_TRANSPORT = ROOT / "exec_transport.py"

# executor/requirements.txt — EXACTLY these.
REQUIRED_EXECUTOR_PACKAGES = [
    "fastapi", "starlette", "uvicorn[standard]", "pydantic", "python-dotenv",
    "pandas", "numpy", "pyarrow", "matplotlib", "pillow", "seaborn", "plotly",
    "scipy", "scikit-learn", "matplotlib-venn", "wordcloud", "networkx",
    "squarify", "missingno", "calplot", "upsetplot", "adjustText",
    # jinja2 is REQUIRED here for one reason only: pandas implements
    # `DataFrame.style` with it, and a Styler RESULT is rendered to
    # `styled_html` inside this image. It is NOT web templating —
    # `Jinja2Templates` appears nowhere in the copied file set. A future
    # "jinja2 is web templating, main-app only" cleanup must FAIL this test
    # rather than silently regress every generated `df.style...` to an
    # ImportError at exec time.
    "jinja2",
]
# Named explicitly so a rename in the root file cannot make the check vacuous.
FORBIDDEN_EXECUTOR_PACKAGES = [
    "httpx", "itsdangerous", "python-multipart", "Authlib", "joserfc",
    "openpyxl", "python-calamine", "kaleido", "python-pptx", "reportlab",
    "SQLAlchemy", "cryptography", "psycopg2-binary", "psycopg2", "psycopg",
    "PyMySQL", "pyodbc", "oracledb", "clickhouse-sqlalchemy", "clickhouse-driver",
    "asynch", "ciso8601", "sqlglot", "google-cloud-storage", "google-auth",
    "google-api-core", "google-cloud-core", "google-resumable-media",
    "google-crc32c", "googleapis-common-protos", "proto-plus", "protobuf",
    "cachetools", "pyasn1", "pyasn1_modules", "rsa", "croniter",
    # the styled-table HTML sanitiser: main app only, never in the sandbox
    "nh3",
    # an HTTP client library: the sandbox holds no HTTP client by decision
    # (the root file pins it only because the web image's libraries pull it)
    "urllib3",
]

# Dockerfile COPY sources: the modules the runner imports plus the transport.
RUNTIME_COPY_SOURCES = {
    "code_exec.py", "plot_utils.py", "exec_sanitizer.py", "sandbox_guard.py",
    "outlier_utils.py", "logger_utils.py", "settings.py", "exec_transport.py",
    "static/fonts/DejaVuSans.ttf",
    "executor/__init__.py", "executor/app.py", "executor/runner.py",
    "executor/requirements.txt",
}
TEST_STAGE_COPY_SOURCES = {"executor/tests", "tools/fixtures/sample_sales.csv"}
FORBIDDEN_COPY_FRAGMENTS = [
    "local_store", "db_connector", "db_sources", "db_scheduler", "brain_client",
    "roles_store", "sso_store", "gcs_upload", "password_utils", "routes",
    "templates", ".env", "client.env", "run_chat_local", "auto_analytics",
]

EXPECTED_RUNTIME_ENV = {
    "DATA_ROOT": "/tmp/executor",
    "MPLBACKEND": "Agg",
    "MPLCONFIGDIR": "/tmp/mpl",
    "XDG_CACHE_HOME": "/tmp/cache",
    "HOME": "/tmp",
    "EXECUTOR_SHARED_DIR": "/jobs",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "ARROW_IO_THREADS": "1",
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "LOG_MAX_BYTES": "5242880",
    "LOG_BACKUP_COUNT": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    # stdout-only logging inside the sandbox (no log file a later job reads)
    "PDC_EXECUTOR": "1",
}
EXPECTED_CMD = 'CMD ["uvicorn", "executor.app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]'
EXPECTED_BASE_IMAGE = "python:3.12-slim"

# Modules the executor side must never import.
DENIED_IMPORTS = {
    "local_store", "db_connector", "db_sources", "db_scheduler", "brain_client",
    "roles_store", "sso_store", "gcs_upload", "password_utils", "sqlalchemy", "kaleido",
}

# Env names executor/app.py may read: its own config, the build stamp, the
# runner-env allowlist it forwards, and the secret names it REFUSES to start
# with (reading them in order to refuse is the check itself).
ALLOWED_APP_ENV_PREFIXES = ("EXECUTOR_", "BUILD_")
ALLOWED_APP_ENV_NAMES = {
    "PATH", "HOME", "LANG", "LC_ALL", "MPLBACKEND", "MPLCONFIGDIR", "XDG_CACHE_HOME",
    "DATA_ROOT", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT", "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ARROW_IO_THREADS",
    "ARROW_DEFAULT_MEMORY_POOL", "SECRET_KEY", "CLIENT_ENCRYPTION_KEY",
    "CLIENT_ENCRYPTION_KEY_OLD", "LOCAL_ADMIN_PASSWORD", "GCS_UPLOAD_BUCKET",
    "BRAIN_URL", "BRAIN_TENANT_TOKEN",
}


# ---------------------------------------------------------------------------
# file helpers
# ---------------------------------------------------------------------------
def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path.relative_to(ROOT).as_posix()} (not written yet)"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# requirements helpers (private copy of tests/test_dependency_pins.py's idiom)
# ---------------------------------------------------------------------------
_EXACT_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?)\s*==\s*([A-Za-z0-9.+!-]+)$")


def _normalize(name: str) -> str:
    """PEP 503 name normalization, extras stripped: `uvicorn[standard]` -> `uvicorn`."""
    base = name.split("[", 1)[0].strip()
    return re.sub(r"[-_.]+", "-", base).lower()


def _pins(text: str) -> dict:
    """{normalized name: version} for every `name==version` line."""
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _EXACT_PIN_RE.match(line)
        if m:
            out[_normalize(m.group(1))] = m.group(2)
    return out


def _raw_names(text: str) -> dict:
    """{normalized name: the name as written (extras kept)}."""
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = _EXACT_PIN_RE.match(line) if line else None
        if m:
            out[_normalize(m.group(1))] = m.group(1)
    return out


# ---------------------------------------------------------------------------
# Dockerfile helpers (private copy of tests/test_container_hardening_config.py's idiom)
# ---------------------------------------------------------------------------
def _instructions(text: str):
    """[(KEYWORD, args)] with backslash-continuations joined and comment lines dropped."""
    logical = []
    buf = None
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        if buf is None:
            if not raw.strip():
                continue
            buf = raw.rstrip()
        else:
            buf = buf[:-1].rstrip() + " " + raw.strip()
        if buf.endswith("\\"):
            continue
        logical.append(buf)
        buf = None
    if buf is not None:
        logical.append(buf)
    out = []
    for line in logical:
        m = re.match(r"^\s*([A-Za-z]+)\s*(.*)$", line)
        if m:
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def _raw_instruction_block(text: str, keyword: str) -> str:
    """The raw source lines of the (last) instruction starting with `keyword`,
    continuation lines included — for byte-identical pins."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(keyword + " ")]
    assert starts, f"no {keyword} instruction in Dockerfile"
    i = starts[-1]
    block = [lines[i]]
    while block[-1].rstrip().endswith("\\"):
        i += 1
        block.append(lines[i])
    return "\n".join(block)


def _stages(text: str) -> list:
    """[(stage_name_or_None, base, [(KEYWORD, args), ...])] split at each FROM."""
    stages = []
    for kw, args in _instructions(text):
        if kw == "FROM":
            m = re.match(r"^(\S+)(?:\s+AS\s+(\S+))?$", args, flags=re.IGNORECASE)
            assert m, args
            stages.append((m.group(2), m.group(1), []))
            continue
        assert stages, f"instruction before the first FROM: {kw} {args}"
        stages[-1][2].append((kw, args))
    return stages


def _default_chain(text: str) -> list:
    """The stages a plain `docker build` actually builds, in build order.

    Docker builds the LAST stage when no `--target` is given (and a compose
    `build:` block gives none), so the default image is that stage plus every
    LOCAL stage it inherits through `FROM <stage>`. Resolving the chain rather
    than looking up the stage NAMED "runtime" is what makes the pins below
    describe the shipped image: a trailing `test` stage once made the plain
    build root-with-pytest while a `runtime` stage earlier in the file still
    satisfied every name-keyed assertion.
    """
    stages = _stages(text)
    assert stages, "no FROM in executor/Dockerfile"
    by_name = {s[0]: s for s in stages if s[0]}
    chain = [stages[-1]]
    seen = {stages[-1][0]}
    while True:
        parent = by_name.get(chain[0][1])
        if parent is None:          # an external base image — chain complete
            break
        assert parent[0] not in seen, f"FROM cycle at stage {parent[0]!r}"
        seen.add(parent[0])
        chain.insert(0, parent)
    return chain


def _runtime_instructions(text: str) -> list:
    """[(KEYWORD, args)] of the effective default build target, in order."""
    out = []
    for _, _, instructions in _default_chain(text):
        out.extend(instructions)
    return out


def _test_stage(text: str):
    stages = _stages(text)
    names = [s[0] for s in stages]
    assert "test" in names, names
    return stages[names.index("test")]


def _copy_sources(instructions) -> list:
    """Every COPY source path (flags skipped, JSON form handled, trailing `/` dropped)."""
    out = []
    for kw, args in instructions:
        if kw != "COPY":
            continue
        if args.startswith("["):
            parts = json.loads(args)
        else:
            parts = [p for p in args.split() if not p.startswith("--")]
        assert len(parts) >= 2, args
        out.extend(p.rstrip("/") for p in parts[:-1])
    return out


def _env_pairs(instructions) -> dict:
    """{KEY: VALUE} over every ENV instruction (quotes stripped; `KEY value` form too)."""
    out = {}
    for kw, args in instructions:
        if kw != "ENV":
            continue
        if "=" not in args:
            key, _, value = args.partition(" ")
            out[key.strip()] = value.strip().strip('"')
            continue
        for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\S*)', args):
            out[m.group(1)] = m.group(2).strip('"')
    return out


def _import_roots(path: Path) -> set:
    """Top-level module names imported anywhere in the file (function-local too)."""
    tree = ast.parse(_read(path), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# ---------------------------------------------------------------------------
# (1) files exist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [EXEC_REQUIREMENTS, EXEC_DOCKERFILE, EXEC_INIT, EXEC_APP, EXEC_RUNNER, EXEC_TRANSPORT],
                         ids=lambda p: p.relative_to(ROOT).as_posix())
def test_executor_deliverable_exists(path):
    _read(path)


# ---------------------------------------------------------------------------
# (2) executor/requirements.txt
# ---------------------------------------------------------------------------
def test_executor_requirements_every_line_is_an_exact_pin():
    unpinned = []
    for idx, raw in enumerate(_read(EXEC_REQUIREMENTS).splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _EXACT_PIN_RE.match(line):
            unpinned.append((idx, line))
    assert unpinned == [], unpinned
    n_pins = len(_pins(_read(EXEC_REQUIREMENTS)))
    assert n_pins > 0, "no `==` pins parsed at all"


def test_executor_requirements_is_exactly_the_required_set():
    exec_pins = _pins(_read(EXEC_REQUIREMENTS))
    required = {_normalize(n) for n in REQUIRED_EXECUTOR_PACKAGES}
    missing = sorted(required - set(exec_pins))
    assert missing == [], f"required executor packages missing: {missing}"
    extra = sorted(set(exec_pins) - required)
    assert extra == [], f"packages the executor must not ship: {extra}"


def test_executor_requirements_pins_match_root_versions():
    exec_pins = _pins(_read(EXEC_REQUIREMENTS))
    root_pins = _pins(_read(ROOT_REQUIREMENTS))
    drift = {name: (v, root_pins.get(name)) for name, v in exec_pins.items() if root_pins.get(name) != v}
    assert drift == {}, f"executor pin != root pin (executor, root): {drift}"
    # the extras spelling matches too (uvicorn[standard] is what the root pins)
    exec_raw = _raw_names(_read(EXEC_REQUIREMENTS))
    root_raw = _raw_names(_read(ROOT_REQUIREMENTS))
    spelled = {n: (exec_raw[n], root_raw.get(n)) for n in exec_raw if exec_raw[n] != root_raw.get(n)}
    assert spelled == {}, spelled


def test_executor_requirements_contain_none_of_the_forbidden_packages():
    exec_pins = _pins(_read(EXEC_REQUIREMENTS))
    forbidden = {_normalize(n) for n in FORBIDDEN_EXECUTOR_PACKAGES}
    present = sorted(set(exec_pins) & forbidden)
    assert present == [], f"forbidden packages in executor/requirements.txt: {present}"
    text = _read(EXEC_REQUIREMENTS).lower()
    assert "kaleido" not in text, "kaleido is named in executor/requirements.txt"


def test_forbidden_list_covers_every_root_package_outside_the_required_set():
    """Guards the guard: a root package that is neither required nor named
    forbidden means the two lists drifted from the root file."""
    root_pins = set(_pins(_read(ROOT_REQUIREMENTS)))
    required = {_normalize(n) for n in REQUIRED_EXECUTOR_PACKAGES}
    forbidden = {_normalize(n) for n in FORBIDDEN_EXECUTOR_PACKAGES}
    unclassified = sorted(root_pins - required - forbidden)
    assert unclassified == [], f"root packages not classified required/forbidden: {unclassified}"


# ---------------------------------------------------------------------------
# (3) executor/Dockerfile
# ---------------------------------------------------------------------------
def test_dockerfile_base_image_matches_root_and_stages_are_named():
    text = _read(EXEC_DOCKERFILE)
    stages = _stages(text)
    names = [s[0] for s in stages]
    assert names == ["app", "test", "runtime"], names
    root_from = [args for kw, args in _instructions(_read(ROOT_DOCKERFILE)) if kw == "FROM"][0]
    assert root_from == EXPECTED_BASE_IMAGE, root_from
    assert stages[0][1] == EXPECTED_BASE_IMAGE, stages[0][1]
    # both later stages build ON `app` — neither restates the external base
    assert stages[1][1] == "app", stages[1][1]
    assert stages[2][1] == "app", stages[2][1]


def test_dockerfile_pip_upgrade_line_is_identical_to_root_and_precedes_install():
    root_runs = [args for kw, args in _instructions(_read(ROOT_DOCKERFILE)) if kw == "RUN"]
    root_upgrade = [r for r in root_runs if re.search(r"pip install .*--upgrade .*pip==", r)]
    assert root_upgrade, root_runs
    runtime = _runtime_instructions(_read(EXEC_DOCKERFILE))
    runs = [(i, args) for i, (kw, args) in enumerate(runtime) if kw == "RUN"]
    upgrade = [(i, r) for i, r in runs if r == root_upgrade[0]]
    assert upgrade, f"executor Dockerfile lacks the root's pip line {root_upgrade[0]!r}; RUN lines: {[r for _, r in runs]}"
    install = [(i, r) for i, r in runs if re.search(r"pip install .*-r\s+\S*requirements\.txt", r)]
    assert install, [r for _, r in runs]
    assert "executor/requirements.txt" in install[0][1] or "requirements.txt" in install[0][1], install
    assert upgrade[0][0] < install[0][0], (upgrade, install)
    assert "--no-cache-dir" in install[0][1], install


def test_dockerfile_creates_shared_group_and_executor_user():
    runtime = _runtime_instructions(_read(EXEC_DOCKERFILE))
    runs = " ; ".join(args for kw, args in runtime if kw == "RUN")
    assert re.search(r"groupadd\s+-g\s+10001\s+pdc\b", runs), runs
    assert re.search(r"useradd\s+(?:-\S+\s+)*-u\s+10002\b", runs), runs
    assert re.search(r"useradd\s+[^;&|]*-g\s+pdc\b", runs), runs
    assert re.search(r"useradd\s+[^;&|]*\bpdcexec\s*(?:$|&&|;)", runs), runs
    assert not re.search(r"useradd\s+[^;&|]*-u\s+10001\b", runs), runs
    assert re.search(r"mkdir\s+-p\s+/jobs", runs), runs
    assert re.search(r"chown\s+root:pdc\s+/jobs", runs), runs
    assert re.search(r"chmod\s+2770\s+/jobs", runs), runs


def test_dockerfile_runtime_switches_to_pdcexec_after_last_run_and_before_cmd():
    runtime = _runtime_instructions(_read(EXEC_DOCKERFILE))
    keywords = [kw for kw, _ in runtime]
    users = [(i, args) for i, (kw, args) in enumerate(runtime) if kw == "USER"]
    assert users, keywords
    user_idx, user_args = users[-1]
    assert user_args == "pdcexec", user_args
    last_run = max(i for i, kw in enumerate(keywords) if kw == "RUN")
    cmd_idx = max(i for i, kw in enumerate(keywords) if kw == "CMD")
    assert last_run < user_idx < cmd_idx, keywords


def test_dockerfile_expose_cmd_and_healthcheck():
    text = _read(EXEC_DOCKERFILE)
    runtime = _runtime_instructions(text)
    exposes = [args for kw, args in runtime if kw == "EXPOSE"]
    assert exposes == ["8090"], exposes
    cmds = [f"CMD {args}" for kw, args in runtime if kw == "CMD"]
    assert cmds == [EXPECTED_CMD], cmds
    checks = [args for kw, args in runtime if kw == "HEALTHCHECK"]
    assert len(checks) == 1, checks
    check = checks[0]
    assert "/healthz" in check, check
    assert "127.0.0.1:8090" in check or "localhost:8090" in check, check
    assert "python" in check and "urllib" in check, check
    assert "curl" not in check, check


def test_dockerfile_runtime_env_set():
    runtime = _runtime_instructions(_read(EXEC_DOCKERFILE))
    env = _env_pairs(runtime)
    wrong = {k: (env.get(k), v) for k, v in EXPECTED_RUNTIME_ENV.items() if env.get(k) != v}
    assert wrong == {}, f"ENV mismatches (got, expected): {wrong}"
    assert "BUILD_COMMIT" in env and "BUILD_TIME" in env, sorted(env)
    args = [a for kw, a in runtime if kw == "ARG"]
    assert any(a.startswith("BUILD_COMMIT") for a in args), args
    assert any(a.startswith("BUILD_TIME") for a in args), args
    for secret in ("BRAIN_URL", "BRAIN_TENANT_TOKEN", "SECRET_KEY", "CLIENT_ENCRYPTION_KEY", "LOCAL_ADMIN_PASSWORD"):
        assert secret not in env, (secret, env.get(secret))


def test_dockerfile_bakes_plotly_js_with_the_root_one_liner():
    root_bake = [args for kw, args in _instructions(_read(ROOT_DOCKERFILE))
                 if kw == "RUN" and "plotly.min.js" in args]
    assert len(root_bake) == 1, root_bake
    runtime = _runtime_instructions(_read(EXEC_DOCKERFILE))
    exec_bake = [args for kw, args in runtime if kw == "RUN" and "plotly.min.js" in args]
    assert exec_bake == root_bake, (exec_bake, root_bake)


def test_dockerfile_copies_exactly_the_allowed_module_set():
    text = _read(EXEC_DOCKERFILE)
    runtime = _runtime_instructions(text)
    sources = _copy_sources(runtime)
    assert sources, "no COPY in the default build target"
    assert "." not in sources and "./" not in sources, sources
    unexpected = sorted(set(sources) - RUNTIME_COPY_SOURCES)
    assert unexpected == [], f"default target copies files outside the allowed set: {unexpected}"
    missing = sorted(RUNTIME_COPY_SOURCES - set(sources))
    assert missing == [], f"default target does not copy: {missing}"
    _, _, test_stage = _test_stage(text)
    test_sources = _copy_sources(test_stage)
    test_unexpected = sorted(set(test_sources) - TEST_STAGE_COPY_SOURCES - RUNTIME_COPY_SOURCES)
    assert test_unexpected == [], test_unexpected
    everything = " ".join(sources + test_sources)
    for fragment in FORBIDDEN_COPY_FRAGMENTS:
        assert fragment not in everything, (fragment, everything)
    for kw, args in _instructions(text):
        assert kw != "ADD", args


def test_dockerfile_never_mentions_kaleido_and_pytest_only_in_test_stage():
    text = _read(EXEC_DOCKERFILE)
    assert "kaleido" not in text.lower(), "kaleido must not be in the executor image (PNGs are rasterized by the web service)"
    runtime = _runtime_instructions(text)
    runtime_text = "\n".join(f"{kw} {args}" for kw, args in runtime)
    assert "pytest" not in runtime_text, runtime_text
    assert "httpx" not in runtime_text, runtime_text
    _, _, test_stage = _test_stage(text)
    test_text = "\n".join(f"{kw} {args}" for kw, args in test_stage)
    assert "pytest" in test_text, test_text
    users = [args for kw, args in test_stage if kw == "USER"]
    assert users and users[-1] == "root", users
    m = re.search(r"httpx==([A-Za-z0-9.+!-]+)", test_text)
    assert m, test_text
    root_httpx = _pins(_read(ROOT_REQUIREMENTS)).get("httpx")
    assert m.group(1) == root_httpx, (m.group(1), root_httpx)
    # `USER root` appears EXACTLY once in the file, and only inside `test`.
    # What used to stand here was a pin on the file's LAST `USER` line being
    # `USER root` — the assertion that made the defect invisible: it PASSED
    # precisely because the privileged test stage came last, i.e. because the
    # default build target was the root image.
    raw_root_users = [ln for ln in text.splitlines() if ln.strip() == "USER root"]
    assert len(raw_root_users) == 1, raw_root_users
    per_stage = {name: [a for kw, a in instr if kw == "USER"] for name, _, instr in _stages(text)}
    assert "root" in per_stage.get("test", []), per_stage
    leaked = {n: u for n, u in per_stage.items() if n != "test" and "root" in u}
    assert leaked == {}, f"`USER root` outside the test stage: {leaked}"


def test_dockerfile_default_build_target_is_not_the_test_stage():
    """The LAST stage decides what a plain `docker build` (and a compose
    `build:` block, which passes no `--target`) produces. It must be the
    hardened image: a trailing, instruction-free `FROM app AS runtime` that
    inherits `app` whole, so the default build and `--target runtime` are the
    same image and `test` can never be reached by accident."""
    text = _read(EXEC_DOCKERFILE)
    stages = _stages(text)
    last_name, _, last_instructions = stages[-1]
    assert last_name != "test", last_name
    assert last_name == "runtime", last_name
    chain_names = [s[0] for s in _default_chain(text)]
    assert "test" not in chain_names, chain_names
    assert last_instructions == [], last_instructions
    lines = [ln.rstrip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines[-1] == "FROM app AS runtime", lines[-1]
    assert _raw_instruction_block(text, "FROM") == "FROM app AS runtime"


def test_dockerfile_default_target_runs_as_pdcexec_and_has_no_test_runner():
    """The shipped image runs as uid 10002 and carries no test runner at all."""
    text = _read(EXEC_DOCKERFILE)
    instructions = _runtime_instructions(text)
    users = [args for kw, args in instructions if kw == "USER"]
    assert users, [kw for kw, _ in instructions]
    assert users[-1] == "pdcexec", users
    assert "root" not in users, users
    flat = "\n".join(f"{kw} {args}" for kw, args in instructions)
    assert not re.search(r"pip install[^\n]*\bpytest\b", flat), flat
    assert "pytest" not in flat, flat


# ---------------------------------------------------------------------------
# (3b) pip leaves the hardened image; only the test stage puts it back
# ---------------------------------------------------------------------------
_PIP_REMOVAL_RE = re.compile(r"\bpip\s+uninstall\s+(?:-\S+\s+)*pip\b")
_REQ_INSTALL_RE = re.compile(r"pip\s+install\s+.*-r\s+\S*requirements\.txt")
_PIP_WORD_RE = re.compile(r"(?<![\w./-])pip3?(?:\.\d+)?(?![\w-])")


def test_executor_requirements_never_name_urllib3():
    """urllib3 is pinned in the ROOT file (a transitive of the web image's
    HTTP libraries). The sandbox installs no HTTP client, so the name must not
    appear in its requirements at all — not as a pin, not in any other form."""
    lines = [raw.split("#", 1)[0].strip() for raw in _read(EXEC_REQUIREMENTS).splitlines()]
    hits = [ln for ln in lines if ln and re.match(r"(?i)^urllib3\b", ln)]
    assert hits == [], hits
    assert "urllib3" not in _pins(_read(EXEC_REQUIREMENTS)), sorted(_pins(_read(EXEC_REQUIREMENTS)))
    root_pin = _pins(_read(ROOT_REQUIREMENTS)).get("urllib3")
    assert root_pin is not None, "urllib3 is not pinned in the root requirements.txt"


def test_dockerfile_default_target_removes_pip_after_its_requirements_install():
    """pip is a build tool the sandbox never calls, and its vendored libraries
    (pip/_vendor) are what an image scan reports as if the sandbox shipped
    them. The removal leaves no installed pip distribution, no launcher and no
    pip/_vendor tree (the base image's ensurepip wheel stays). The removal step must sit in the chain the DEFAULT
    target is built from, after the requirements install, before `USER
    pdcexec`, and nothing after it in that chain may call pip."""
    runtime = _runtime_instructions(_read(EXEC_DOCKERFILE))
    runs = [(i, args) for i, (kw, args) in enumerate(runtime) if kw == "RUN"]
    install = [i for i, r in runs if _REQ_INSTALL_RE.search(r)]
    assert install, [r for _, r in runs]
    removal = [i for i, r in runs if _PIP_REMOVAL_RE.search(r)]
    assert len(removal) == 1, f"expected one pip removal RUN in the default target: {[r for _, r in runs]}"
    removal_idx = removal[0]
    assert install[-1] < removal_idx, (install, removal_idx)
    user_idx = max(i for i, (kw, _) in enumerate(runtime) if kw == "USER")
    assert removal_idx < user_idx, (removal_idx, user_idx)
    later = [f"{kw} {args}" for kw, args in runtime[removal_idx + 1:] if _PIP_WORD_RE.search(args)]
    assert later == [], f"pip named after its removal in the default target: {later}"
    flat = "\n".join(f"{kw} {args}" for kw, args in runtime)
    assert "ensurepip" not in flat, "the default target must not restore pip"


def test_dockerfile_removal_is_in_the_stage_the_default_target_inherits():
    """The removal lives in `app` (the stage `runtime` inherits whole), never
    only in the trailing stage: `runtime` stays instruction-free, and `test`
    builds on an `app` that already has no pip."""
    stages = {name: instr for name, _, instr in _stages(_read(EXEC_DOCKERFILE))}
    app_runs = [args for kw, args in stages["app"] if kw == "RUN"]
    assert any(_PIP_REMOVAL_RE.search(r) for r in app_runs), app_runs
    assert stages["runtime"] == [], stages["runtime"]


def test_dockerfile_test_stage_restores_pip_before_installing_pytest():
    """`app` has no pip, so the test stage re-creates it (ensurepip) BEFORE
    the pytest install, and never removes it again — and the last stage is
    still the hardened one, so the restored pip cannot ship by default."""
    text = _read(EXEC_DOCKERFILE)
    _, base, test_stage = _test_stage(text)
    assert base == "app", base
    flat = "\n".join(f"{kw} {args}" for kw, args in test_stage)
    ensure = re.search(r"python\s+-m\s+ensurepip\b", flat)
    assert ensure, flat
    pytest_install = re.search(r"pip\s+install\b[^\n]*?\bpytest\b", flat)
    assert pytest_install, flat
    first_pip_install = re.search(r"pip\s+install\b", flat)
    assert ensure.start() < first_pip_install.start(), flat
    assert ensure.start() < pytest_install.start(), flat
    last_name = _stages(text)[-1][0]
    assert last_name == "runtime" and last_name != "test", last_name
    assert "test" not in [s[0] for s in _default_chain(text)]


# ---------------------------------------------------------------------------
# (4) Python sources: import hygiene, env names, markers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [EXEC_INIT, EXEC_APP, EXEC_RUNNER, EXEC_TRANSPORT],
                         ids=lambda p: p.relative_to(ROOT).as_posix())
def test_executor_sources_import_none_of_the_denied_modules(path):
    roots = _import_roots(path)
    denied = sorted(roots & DENIED_IMPORTS)
    assert denied == [], f"{path.name} imports denied modules: {denied}"


def test_exec_transport_imports_only_the_planned_set():
    roots = _import_roots(EXEC_TRANSPORT)
    forbidden_here = {"code_exec", "plot_utils", "routes", "app", "settings", "run_chat_local", "requests", "httpx"}
    hit = sorted(roots & forbidden_here)
    assert hit == [], hit


_ENV_READ_RE = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|environ(?:\.get)?\s*[\[(]\s*)["']([A-Za-z_][A-Za-z0-9_]*)["']"""
)


def test_app_reads_only_its_own_env_names():
    text = _read(EXEC_APP)
    names = sorted(set(_ENV_READ_RE.findall(text)))
    assert names, "executor/app.py reads no env names at all — EXECUTOR_SHARED_DIR must come from the env"
    offenders = [n for n in names
                 if not n.startswith(ALLOWED_APP_ENV_PREFIXES) and n not in ALLOWED_APP_ENV_NAMES]
    assert offenders == [], f"executor/app.py reads env names outside its allowlist: {offenders}"
    for required in ("EXECUTOR_SHARED_DIR", "EXECUTOR_MEM_LIMIT_MB", "EXECUTOR_MAX_CONCURRENT",
                     "EXECUTOR_MAX_TIMEOUT_S", "EXECUTOR_GRACE_S"):
        assert required in text, required
    assert "load_dotenv" not in text, "executor/app.py must not read a .env file"
    assert "client.env" not in text


def test_app_never_uses_jsonresponse():
    text = _read(EXEC_APP)
    assert not re.search(r"\bJSONResponse\b", text), "JSONResponse renders NaN with allow_nan=False"
    assert "media_type" in text, "the /execute body must be a plain Response(content=dumps(...))"


def test_app_carries_the_runner_spawn_and_kill_markers():
    text = _read(EXEC_APP)
    for marker in ("start_new_session=True", "killpg", '"-I"', '"-u"', "pass_fds", "SIGKILL",
                   "EXECUTOR_RESPONSE_FD", "os.getpid()", "os.getppid()", "/proc"):
        assert marker in text, f"executor/app.py lacks {marker!r}"
    assert "umask" in text, "the app must set umask 0o007 so the web uid keeps group write"
    assert "SystemExit" in text, "the secret self-check must refuse startup with SystemExit"
    for refused in ("BRAIN_", "SECRET_KEY", "CLIENT_ENCRYPTION_KEY", "CLIENT_ENCRYPTION_KEY_OLD",
                    "LOCAL_ADMIN_PASSWORD", "GCS_UPLOAD_BUCKET"):
        assert refused in text, f"secret self-check does not name {refused}"


def test_app_sweep_excludes_the_parent_process():
    """The same-uid /proc sweep must spare BOTH the app and its parent: under a
    supervisor, `--reload` or a multi-worker uvicorn the process above shares
    uid 10002 and sweeping it takes the service down mid-request."""
    text = _read(EXEC_APP)
    assert "os.getppid()" in text, "the same-uid sweep must exclude os.getppid()"


def test_runner_carries_the_limit_and_exit_markers():
    text = _read(EXEC_RUNNER)
    for marker in ("RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_FSIZE", "RLIMIT_CORE", "setrlimit",
                   "os._exit", "flush()", "umask", "EXECUTOR_RESPONSE_FD", "sys.path.insert",
                   "read_inputs", "serialize_result",
                   "_execute_in_process", "_render_in_process"):
        assert marker in text, f"executor/runner.py lacks {marker!r}"
    # The PUBLIC names dispatch over HTTP; calling one from inside the sandbox
    # would make the sandbox ask itself to run the job.
    for public in (".safe_execute(", ".render_plot_safe("):
        assert public not in text, f"executor/runner.py calls {public!r}"
    assert "timeout=None" not in text, "the runner must pass timeout=timeout_s, never None"
    # limits are set before the heavy imports
    limit_pos = text.find("setrlimit")
    import_pos = min(p for p in (text.find("import code_exec"), text.find("import plot_utils")) if p >= 0)
    assert 0 <= limit_pos < import_pos, (limit_pos, import_pos)


def test_runner_and_app_never_read_a_dotenv_or_touch_data_root_stores():
    for path in (EXEC_APP, EXEC_RUNNER):
        text = _read(path)
        assert "load_dotenv" not in text, path.name
        assert "db_snapshots" not in text, path.name
        assert "chatdata" not in text, path.name


# ---------------------------------------------------------------------------
# (13) the healthcheck flags and the two shared vocabularies
# ---------------------------------------------------------------------------
EXECUTOR_CLIENT = ROOT / "executor_client.py"

# Every flag the HEALTHCHECK must carry. `--start-period` + a 2 s
# `--start-interval` are what keep `depends_on: service_healthy` from holding
# the web container back a full 30 s interval on every `up`.
# `--start-interval` needs Engine 25+ AT BUILD TIME: an older builder REJECTS
# it as an unknown HEALTHCHECK flag and the build fails (an older engine merely
# RUNNING a pre-built image just ignores the field). So this flag also states
# the minimum engine for building the images from source.
EXPECTED_HEALTHCHECK_FLAGS = (
    "--interval=30s", "--timeout=5s", "--retries=3",
    "--start-period=15s", "--start-interval=2s",
)

# Every `reason` token `executor/app.py` can put on a response. The vocabulary
# is CLOSED because `reason` comes back through the hostile response body and
# reaches the planner's retry prompt via `crash_error_text`; an unlisted token
# must be dropped, not forwarded.
EXPECTED_CRASH_REASONS = {
    "spawn_failed", "hard_timeout", "sigkill", "response_invalid", "signal",
    "exit", "queued", "executor_error",
}


def _module_level_assignment(path: Path, name: str):
    """The value node of a module-level `name = ...` (or `name: T = ...`)."""
    tree = ast.parse(_read(path), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
    return None


def _string_constants(node) -> set:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _reason_tokens_in_executor_app():
    """(tokens, allows_none, unresolved) over every `"reason"` the app assigns.

    Collected from the response dicts (`{..., "reason": "sigkill"}`), from the
    `response.setdefault("reason", None)` default, and — for the crashed
    branch, whose value is the local `reason` of a conditional expression —
    from the string constants assigned to that name. `unresolved` carries any
    `"reason"` value this reader could not resolve to a string, so the pin can
    never pass by silently missing a token.
    """
    tree = ast.parse(_read(EXEC_APP), filename=str(EXEC_APP))
    assigned = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                strings = _string_constants(node.value)
                if strings:
                    assigned.setdefault(target.id, set()).update(strings)

    value_nodes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "reason":
                    value_nodes.append(value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "reason"):
            value_nodes.extend(node.args[1:2])

    tokens, unresolved, allows_none = set(), [], False
    for value in value_nodes:
        if isinstance(value, ast.Constant):
            if value.value is None:
                allows_none = True
            elif isinstance(value.value, str):
                tokens.add(value.value)
            else:
                unresolved.append(repr(value.value))
            continue
        strings = assigned.get(value.id, set()) if isinstance(value, ast.Name) else set()
        strings |= _string_constants(value)
        if not strings:
            unresolved.append(ast.dump(value))
        tokens |= strings
    return tokens, allows_none, unresolved


def test_dockerfile_healthcheck_carries_every_flag_including_the_start_probe():
    """A dropped flag must fail here, not surface as a slow `up`.

    `depends_on: {executor: {condition: service_healthy}}` gates the web
    container on this probe, so the flags are startup behaviour of the whole
    stack, not a detail of one image. The probe itself stays the python
    one-liner: there is no curl in this image (and adding one to pass a
    healthcheck would put an HTTP client in the sandbox).
    """
    block = _raw_instruction_block(_read(EXEC_DOCKERFILE), "HEALTHCHECK")
    missing = [flag for flag in EXPECTED_HEALTHCHECK_FLAGS if flag not in block]
    assert missing == [], f"HEALTHCHECK lacks {missing}: {block}"
    assert "/healthz" in block, block
    assert "127.0.0.1:8090" in block, block
    assert "python" in block and "urllib" in block, block
    assert "curl" not in block, block


def test_version_modules_tuples_agree_on_both_sides():
    """A DRIFT GUARD, green today.

    The web side warns per library on a version mismatch; a module the two
    tuples disagree about is simply never compared, so the handshake would
    silently stop covering it. Both are
    `("matplotlib", "numpy", "pandas", "plotly", "pyarrow")` today.
    """
    app_node = _module_level_assignment(EXEC_APP, "_VERSION_MODULES")
    client_node = _module_level_assignment(EXECUTOR_CLIENT, "_VERSION_MODULES")
    assert app_node is not None, "executor/app.py has no module-level _VERSION_MODULES"
    assert client_node is not None, "executor_client.py has no module-level _VERSION_MODULES"
    theirs = ast.literal_eval(app_node)
    ours = ast.literal_eval(client_node)
    assert tuple(ours) == tuple(theirs), (ours, theirs)


def test_exec_transport_crash_reason_allowlist_matches_the_executor_vocabulary():
    """`reason` is untrusted text that reaches the retry prompt.

    The sandbox's response body is hostile input, and `reason`/`status` are
    inlined into the error sentence the planner sees on retry. So the accepted
    vocabulary is CLOSED and lives in the SHARED transport (both images import
    it), pinned here against what `executor/app.py` actually assigns — a new
    token on the sandbox side must be added to the allowlist in the same
    change or this test names it.
    """
    tokens, allows_none, unresolved = _reason_tokens_in_executor_app()
    assert unresolved == [], f"unreadable `reason` values in executor/app.py: {unresolved}"
    assert tokens == EXPECTED_CRASH_REASONS, tokens
    assert allows_none is True, "executor/app.py no longer defaults `reason` to None"
    node = _module_level_assignment(EXEC_TRANSPORT, "CRASH_REASONS")
    assert node is not None, "exec_transport.py has no module-level CRASH_REASONS allowlist"
    allowed = _string_constants(node)
    assert allowed == EXPECTED_CRASH_REASONS, allowed


# ---------------------------------------------------------------------------
# no unpickling of a job-directory path on the web side
# ---------------------------------------------------------------------------
def _unpickle_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ("read_pickle", "load", "loads") and (
                    name == "read_pickle"
                    or (isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "pickle")):
                yield node


def test_the_web_side_never_unpickles_a_job_directory_path():
    """The job directory is writable by the sandbox uid, so the web process
    may unpickle only an in-memory buffer there. Inside exec_transport the one
    path read is `read_inputs`, which only the sandbox's runner calls;
    executor_client and app.py never unpickle at all. (local_store's
    parquet-cache pickle lives under the chat's own files directory, which the
    sandbox never mounts — outside this pin on purpose.)"""
    tree = ast.parse(_read(EXEC_TRANSPORT))
    offenders = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for call in _unpickle_calls(fn):
            if fn.name == "read_inputs":
                continue
            arg = call.args[0] if call.args else None
            is_buffer = (isinstance(arg, ast.Call)
                         and ast.unparse(arg.func) in ("io.BytesIO", "BytesIO"))
            if not is_buffer:
                offenders.append(f"exec_transport.{fn.name}:{call.lineno}")
    assert offenders == [], offenders
    for name in ("executor_client.py", "app.py"):
        calls = list(_unpickle_calls(ast.parse((ROOT / name).read_text(encoding="utf-8"))))
        assert calls == [], (name, [c.lineno for c in calls])
    callers = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "executor/tests/", ".venv/")) or rel == "exec_transport.py":
            continue
        if "read_inputs(" in path.read_text(encoding="utf-8", errors="replace"):
            callers.append(rel)
    assert callers == ["executor/runner.py"], callers


def _module_constant(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


def test_orphan_thresholds_are_five_minutes_and_one_minute_on_both_sides():
    web = ROOT / "executor_client.py"
    assert _module_constant(EXEC_APP, "_ORPHAN_MAX_AGE_S") == 300
    assert _module_constant(web, "_ORPHAN_MAX_AGE_S") == 300
    assert _module_constant(EXEC_APP, "_ORPHAN_SWEEP_INTERVAL_S") == 60
    assert _module_constant(web, "_SWEEP_INTERVAL_S") == 60


def test_both_sweeps_skip_jobs_in_flight_and_the_sandbox_locks_at_startup():
    app_text = _read(EXEC_APP)
    web_text = (ROOT / "executor_client.py").read_text(encoding="utf-8")
    assert "_ACTIVE_JOBS" in app_text and "child.name in _ACTIVE_JOBS" in app_text
    assert "_ACTIVE_JOBS" in web_text and "child.name in _ACTIVE_JOBS" in web_text
    assert "os.chmod(child, 0o000)" in app_text
    lifespan = app_text.index("_lock_preexisting_job_dirs(config.shared_dir)")
    assert lifespan < app_text.index("_sweep_orphans(config.shared_dir)", lifespan)


# ---------------------------------------------------------------------------
# the sandbox keeps no log FILE and logs no job-derived text
# ---------------------------------------------------------------------------
def _log_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "log_with_sid":
            yield node


def _without_lengths(call) -> str:
    """The call's source with every `len(...)` removed: a length is what these
    lines are allowed to carry."""
    src = ast.unparse(call)
    for node in ast.walk(call):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "len":
            src = src.replace(ast.unparse(node), "<len>")
    return src


def test_sandbox_side_log_lines_carry_no_error_text_code_or_stderr():
    """Every job runs as the uid that owns the sandbox's log stream: exception
    text (a library quotes the value it choked on), code snippets, frame
    names and stderr contents stay out of it. Lengths, hashes, types and
    statuses only; the full text travels in the response."""
    offenders = []
    code_exec = ROOT / "code_exec.py"
    for call in _log_calls(code_exec):
        src = _without_lengths(call)
        if "EXEC_ERROR" not in src and "EXEC_TIMEOUT" not in src:
            continue
        for bad in ("exec_result.get('error')", "{e}", "snippet", "code=", "dfs="):
            if bad in src:
                offenders.append(f"code_exec.py:{call.lineno} {bad}")
    app_text = _read(EXEC_APP)
    assert "EXEC_JOB_STDERR" not in app_text
    for call in _log_calls(EXEC_APP):
        src = _without_lengths(call)
        if "stderr_text" in src:
            offenders.append(f"executor/app.py:{call.lineno} stderr text")
    for call in _log_calls(ROOT / "executor" / "runner.py"):
        src = ast.unparse(call)
        if "EXEC_RUNNER_FAILED" in src and "{e}" in src:
            offenders.append(f"runner.py:{call.lineno} exception text")
    # plot_utils._render_in_process is the runner's PLOT entry: its root-logger
    # lines must not carry the exception text or a traceback either.
    # Exempt: two setup functions no job reaches — the font registration at
    # import, and the web-lifespan copy of the Plotly bundle. Their exceptions
    # are about files of the image, not about a job's data.
    tree = ast.parse((ROOT / "plot_utils.py").read_text(encoding="utf-8"))
    setup = {"_setup_unicode_font", "ensure_plotly_js_asset"}
    exempt = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in setup:
            exempt.update(id(n) for n in ast.walk(fn))
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", "") == "logging"
                and node.func.attr in ("error", "warning", "exception")):
            src = _without_lengths(node)
            for bad in ("{e}", "{tb}", "format_exc"):
                if bad in src:
                    offenders.append(f"plot_utils.py:{node.lineno} {bad}")
    assert offenders == [], offenders


def test_the_runner_env_marks_the_sandbox_for_the_logger():
    text = _read(EXEC_APP)
    fn = text[text.index("def _runner_env"):text.index("def _enter_job")]
    assert '"PDC_EXECUTOR": "1"' in fn


# ---------------------------------------------------------------------------
# the stray-process sweep loops to exhaustion; each job has private scratch
# ---------------------------------------------------------------------------
def test_the_process_sweep_loops_within_its_bound_and_always_runs():
    text = _read(EXEC_APP)
    assert _module_constant(EXEC_APP, "_SWEEP_MAX_PASSES") == 10
    fn = text[text.index("def _sweep_same_uid_processes"):text.index("# ----", text.index("def _sweep_same_uid_processes"))]
    assert "for _ in range(_SWEEP_MAX_PASSES):" in fn, fn[:400]
    assert "_same_uid_pids()" in fn and '_UNHEALTHY["reason"] = "stray_processes"' in fn
    # never skipped: no concurrency branch around the call, no SKIPPED line
    assert "EXEC_STRAY_SWEEP_SKIPPED" not in text
    call = text.index("    _sweep_same_uid_processes()\n")
    preceding = text[text.rfind("\n", 0, call - 1):call]
    assert "if " not in preceding and "else" not in preceding, preceding
    # zombies do not count as live processes
    assert '("Z", "X")' in text[text.index("def _same_uid_pids"):text.index("def _kill_pid")]
    # unhealthy is visible on /healthz and refuses /execute
    assert 'status_code=503' in text[text.index("async def healthz"):text.index("def _bad_request")]
    assert '"EXECUTOR_UNHEALTHY"' in text


def test_every_job_runs_with_private_temp_and_cache_directories():
    text = _read(EXEC_APP)
    fn = text[text.index("def _runner_env"):text.index("def _enter_job")]
    for name in ("TMPDIR", "MPLCONFIGDIR", "XDG_CACHE_HOME"):
        assert f'"{name}": str(scratch' in fn, name
    run = text[text.index("def _run_job"):text.index("def _mpl_template")]
    assert "_make_scratch(request.job_id)" in run and "_remove_scratch(scratch" in run
    assert 'scratch.mkdir(mode=0o700)' in text
