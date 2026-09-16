"""Structural pins for the `pdc-executor` image and package
(devbox text parsing; no Docker, no import of the executor package).

`executor/requirements.txt` must be an identical-version SUBSET of the
audited root `requirements.txt`: exactly the required list, none of the
forbidden packages (DB drivers, cryptography, Authlib, httpx, kaleido,
google-*, ...). `executor/Dockerfile` must build the unprivileged sandbox
image: the same base image and pip line as the root Dockerfile, uid 10002 in
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
                   "read_inputs", "serialize_result", "safe_execute", "render_plot_safe"):
        assert marker in text, f"executor/runner.py lacks {marker!r}"
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
