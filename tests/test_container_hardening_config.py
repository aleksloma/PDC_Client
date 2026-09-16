"""Task 2 — structural pins for the hardened container (Dockerfile + compose).

The client image must run as a non-root user on a read-only rootfs with a
tmpfs `/tmp`, every capability dropped, `no-new-privileges`, and memory / pid
limits — and every runtime write moved under DATA_ROOT or /tmp. These tests
parse the build/compose files themselves (PyYAML for compose; a small
continuation-aware reader for the Dockerfile) so a regression in either file
fails the suite without Docker.

Pinned as UNCHANGED (the task's "Do NOT change" list): ports, HEALTHCHECK, CMD,
`env_file: client.env` in the customer file, and the external-volume rule in
the local file.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_CUSTOMER = ROOT / "docker-compose.yml"
COMPOSE_LOCAL = ROOT / "docker-compose.local.yml"

# Today's exact text — byte-for-byte pins (universal newlines via read_text).
EXPECTED_HEALTHCHECK = (
    "HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\\n"
    "    CMD curl -fs http://localhost:8000/health || exit 1"
)
EXPECTED_CMD = 'CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]'
EXPECTED_EXPOSE = "EXPOSE 8000"


# ---------------------------------------------------------------------------
# Dockerfile helpers
# ---------------------------------------------------------------------------
def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _instructions():
    """[(KEYWORD, args)] with backslash-continuations joined and comment lines dropped."""
    logical = []
    buf = None
    for raw in _dockerfile_text().splitlines():
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


def _raw_instruction_block(keyword: str) -> str:
    """The raw source lines of the (last) instruction starting with `keyword`,
    continuation lines included — for byte-identical pins."""
    lines = _dockerfile_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(keyword + " ")]
    assert starts, f"no {keyword} instruction in Dockerfile"
    i = starts[-1]
    block = [lines[i]]
    while block[-1].rstrip().endswith("\\"):
        i += 1
        block.append(lines[i])
    return "\n".join(block)


def _run_args():
    return [args for kw, args in _instructions() if kw == "RUN"]


# ---------------------------------------------------------------------------
# Dockerfile — non-root user
# ---------------------------------------------------------------------------
def test_dockerfile_creates_pdc_group_and_user_with_fixed_ids():
    runs = " ; ".join(_run_args())
    assert re.search(r"groupadd\s+-g\s+10001\s+pdc\b", runs), runs
    assert re.search(r"useradd\s+(?:-\S+\s+)*-u\s+10001\b", runs), runs
    assert re.search(r"useradd\s+[^;&|]*-g\s+pdc\b", runs), runs
    assert re.search(r"useradd\s+[^;&|]*\bpdc\s*(?:$|&&|;)", runs), runs


def test_dockerfile_switches_to_pdc_after_last_run_and_before_cmd():
    ins = _instructions()
    keywords = [kw for kw, _ in ins]
    users = [(i, args) for i, (kw, args) in enumerate(ins) if kw == "USER"]
    assert users, keywords
    user_idx, user_args = users[-1]
    assert user_args == "pdc", user_args
    last_run = max(i for i, kw in enumerate(keywords) if kw == "RUN")
    cmd_idx = max(i for i, kw in enumerate(keywords) if kw == "CMD")
    assert last_run < user_idx < cmd_idx, keywords


def test_dockerfile_env_redirects_caches_and_home_to_tmp():
    envs = " ".join(args for kw, args in _instructions() if kw == "ENV")
    assert re.search(r"(?<![A-Za-z_])MPLCONFIGDIR=\"?/tmp/mpl\"?(?=\s|$)", envs), envs
    assert re.search(r"(?<![A-Za-z_])XDG_CACHE_HOME=\"?/tmp/cache\"?(?=\s|$)", envs), envs
    assert re.search(r"(?<![A-Za-z_])HOME=\"?/tmp\"?(?=\s|$)", envs), envs


def test_dockerfile_no_longer_creates_app_logs():
    body = "\n".join(f"{kw} {args}" for kw, args in _instructions())
    assert "/app/logs" not in body, body
    assert not re.search(r"mkdir\s+-p[^&;|]*\blogs\b", body), body


def test_dockerfile_chowns_data_root_to_pdc():
    runs = _run_args()
    chowns = [r for r in runs if "chown" in r]
    assert chowns, runs
    joined = " ; ".join(chowns)
    assert re.search(
        r"chown\s+(?:-R\s+)?(?:pdc:pdc|10001:10001)\s+[^&;|]*/data/client(?=\s|$)", joined
    ), joined


def test_dockerfile_never_chowns_all_of_app():
    """`chown -R pdc:pdc /app` would hand the runtime user its own code.

    The read-only rootfs makes it inert under compose, but the image is also
    run plainly (the Cloud Run demo, `docker run` without `--read-only`), and
    a process that can rewrite the code it is about to execute is exactly what
    the non-root user is meant to prevent. Only directories the app must write
    may be chowned; /app/static/vendor is deliberately NOT one of them (the
    plotly bundle is baked at build time and is served to browsers).
    """
    for run in _run_args():
        if "chown" not in run:
            continue
        for target in re.findall(r"/app[\w./-]*", run):
            assert target not in ("/app", "/app/"), run
            assert not target.startswith("/app/static"), run


def test_dockerfile_expose_healthcheck_and_cmd_unchanged():
    expose = _raw_instruction_block("EXPOSE")
    assert expose == EXPECTED_EXPOSE, expose
    healthcheck = _raw_instruction_block("HEALTHCHECK")
    assert healthcheck == EXPECTED_HEALTHCHECK, healthcheck
    cmd = _raw_instruction_block("CMD")
    assert cmd == EXPECTED_CMD, cmd


# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------
def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _client_service(path: Path) -> dict:
    doc = _load(path)
    svc = doc["services"]["client"]
    assert isinstance(svc, dict)
    return svc


def _volume_pairs(svc: dict):
    """[(source, target)] for both the short 'src:dst[:mode]' and long forms."""
    pairs = []
    for entry in svc.get("volumes") or []:
        if isinstance(entry, str):
            parts = entry.split(":")
            pairs.append((parts[0], parts[1] if len(parts) > 1 else ""))
        elif isinstance(entry, dict):
            pairs.append((str(entry.get("source", "")), str(entry.get("target", ""))))
    return pairs


def _tmpfs_entries(svc: dict):
    t = svc.get("tmpfs")
    if t is None:
        return []
    return [t] if isinstance(t, str) else list(t)


def _ports(svc: dict):
    out = []
    for p in svc.get("ports") or []:
        if isinstance(p, dict):
            out.append(f"{p.get('published')}:{p.get('target')}")
        else:
            out.append(str(p))
    return out


def _environment(svc: dict) -> dict:
    env = svc.get("environment")
    if env is None:
        return {}
    if isinstance(env, dict):
        return {str(k): v for k, v in env.items()}
    out = {}
    for item in env:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def _walk_strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_strings(v)
    elif node is not None:
        yield str(node)


COMPOSE_FILES = [
    pytest.param(COMPOSE_CUSTOMER, id="customer"),
    pytest.param(COMPOSE_LOCAL, id="local"),
]


# ---------------------------------------------------------------------------
# Compose — both files
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_read_only_rootfs_with_tmp_tmpfs(path):
    svc = _client_service(path)
    read_only = svc.get("read_only")
    assert read_only is True, sorted(svc.keys())
    tmpfs = _tmpfs_entries(svc)
    assert any(str(e).split(":")[0] == "/tmp" for e in tmpfs), tmpfs


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_drops_all_capabilities_and_forbids_privilege_gain(path):
    svc = _client_service(path)
    cap_drop = svc.get("cap_drop")
    assert cap_drop == ["ALL"], cap_drop
    security_opt = svc.get("security_opt") or []
    assert "no-new-privileges:true" in security_opt, security_opt


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_sets_memory_and_pid_limits(path):
    svc = _client_service(path)
    mem_limit = svc.get("mem_limit")
    assert mem_limit, sorted(svc.keys())
    pids_limit = svc.get("pids_limit")
    assert isinstance(pids_limit, int) and pids_limit > 0, pids_limit


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_limit_values_match_what_the_docs_promise(path):
    """CUSTOMER_INSTALL.md states these numbers to the operator.

    A limit that drifts from the documented one is worse than no limit: the
    customer sizes their host from the doc. The /tmp size matters twice over —
    Starlette spools every upload part above 1 MiB there, so it caps an
    in-flight upload batch as well as the caches, and being RAM-backed it is
    charged against mem_limit.
    """
    svc = _client_service(path)
    mem_limit = svc.get("mem_limit")
    assert mem_limit == "4g", mem_limit
    pids_limit = svc.get("pids_limit")
    assert pids_limit == 512, pids_limit
    tmp = [str(e) for e in _tmpfs_entries(svc) if str(e).split(":")[0] == "/tmp"]
    assert len(tmp) == 1, _tmpfs_entries(svc)
    options = tmp[0].split(":", 1)[1] if ":" in tmp[0] else ""
    assert "size=512m" in options, tmp
    assert "mode=1777" in options, tmp


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_keeps_data_volume_and_drops_app_logs_mount(path):
    pairs = _volume_pairs(_client_service(path))
    assert ("pdc_client_data", "/data/client") in pairs, pairs
    targets = [t for _, t in pairs]
    assert "/app/logs" not in targets, pairs


# ---------------------------------------------------------------------------
# Compose — local file only
# ---------------------------------------------------------------------------
def test_local_compose_disables_secure_cookie_for_plain_http():
    env = _environment(_client_service(COMPOSE_LOCAL))
    assert "SESSION_HTTPS_ONLY" in env, env
    value = env["SESSION_HTTPS_ONLY"]
    assert value is False or str(value).strip().strip("\"'").lower() == "false", value


def test_local_compose_keeps_external_data_volume_and_drops_logs_volume():
    doc = _load(COMPOSE_LOCAL)
    external = doc["volumes"]["pdc_client_data"].get("external")
    assert external is True, doc["volumes"]
    assert "pdc_client_logs" not in set(_walk_strings(doc)), doc.get("volumes")


def test_local_compose_port_unchanged():
    ports = _ports(_client_service(COMPOSE_LOCAL))
    assert "8091:8000" in ports, ports


# ---------------------------------------------------------------------------
# Compose — customer file only
# ---------------------------------------------------------------------------
def test_customer_compose_never_mentions_session_https_only():
    text = COMPOSE_CUSTOMER.read_text(encoding="utf-8")
    assert "SESSION_HTTPS_ONLY" not in text


def test_customer_compose_port_restart_and_env_file_unchanged():
    svc = _client_service(COMPOSE_CUSTOMER)
    ports = _ports(svc)
    assert "8000:8000" in ports, ports
    restart = svc.get("restart")
    assert restart == "unless-stopped", restart
    env_file = svc.get("env_file")
    env_files = [env_file] if isinstance(env_file, str) else list(env_file or [])
    assert "client.env" in env_files, env_file
