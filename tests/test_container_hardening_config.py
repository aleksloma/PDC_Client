"""Structural pins for the hardened container (Dockerfile + compose).

The client image must run as a non-root user on a read-only rootfs with a
tmpfs `/tmp`, every capability dropped, `no-new-privileges`, and memory / pid
limits — and every runtime write moved under DATA_ROOT or /tmp. These tests
parse the build/compose files themselves (PyYAML for compose; a small
continuation-aware reader for the Dockerfile) so a regression in either file
fails the suite without Docker.

Pinned as UNCHANGED on purpose: ports, HEALTHCHECK, CMD,
`env_file: client.env` in the customer file, and the external-volume rule in
the local file.

Two things this file deliberately does more than once. (1) The hardening
cases parametrize over EVERY service in EVERY compose file, not just
`client` — a new service could otherwise land without the block and keep the
suite green. (2) The `pdc-executor` topology is pinned here too: the
internal `backend` network with its pinned subnet, the shared jobs volume as
the executor's ONLY mount, no ports, no env_file, no healthcheck override, the
`service_healthy` dependency, and `EXECUTOR_MAX_CONCURRENT=1` plus one agreed
`EXECUTOR_SHARED_DIR` on both sides. Without a compose executor service a
clean checkout answers every question `ExecutorUnavailable`, which is why the
wiring is a structural pin and not only an integration test.
"""
import fnmatch
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

# The OS patch layer is pinned in BOTH images: the sandbox ships its own base
# layer and would drift out of the web image's patch level unnoticed.
EXEC_DOCKERFILE = ROOT / "executor" / "Dockerfile"
DOCKERFILE_FILES = [
    pytest.param(DOCKERFILE, id="client"),
    pytest.param(EXEC_DOCKERFILE, id="executor"),
]
APT_CACHE_CLEANUP = "rm -rf /var/lib/apt/lists/*"


# ---------------------------------------------------------------------------
# Dockerfile helpers
# ---------------------------------------------------------------------------
def _dockerfile_text(path: Path = DOCKERFILE) -> str:
    return path.read_text(encoding="utf-8")


def _instructions(path: Path = DOCKERFILE):
    """[(KEYWORD, args)] with backslash-continuations joined and comment lines dropped."""
    logical = []
    buf = None
    for raw in _dockerfile_text(path).splitlines():
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


def _run_args(path: Path = DOCKERFILE):
    return [args for kw, args in _instructions(path) if kw == "RUN"]


# ---------------------------------------------------------------------------
# Dockerfile — non-root user
# ---------------------------------------------------------------------------
def test_dockerfile_creates_pdc_group_and_user_with_fixed_ids():
    runs = " ; ".join(_run_args())
    assert re.search(r"groupadd\s+-g\s+10001\s+pdc\b", runs), runs
    assert re.search(r"useradd\s+(?:-\S+\s+)*-u\s+10001\b", runs), runs
    assert re.search(r"useradd\s+[^;&|]*-g\s+pdc\b", runs), runs
    assert re.search(r"useradd\s+[^;&|]*\bpdc\s*(?:$|&&|;)", runs), runs


@pytest.mark.parametrize("path", DOCKERFILE_FILES)
def test_dockerfile_base_apt_layer_upgrades_the_os_in_the_same_run(path):
    """The base image's own OS packages are patched at build time.

    A base tag is rebuilt far less often than the security fixes for the
    packages inside it ship, so a build that only INSTALLS packages inherits
    every OS vulnerability that already has a fix upstream. The release image
    scan is the DETECTOR for what is left over; this line is the FIX, and
    nothing else in the repository would notice its removal — the suite would
    stay green while both images quietly went stale.

    Pinned in the SAME `RUN` as `apt-get update`: a separate layer would be
    resolved against a cached, possibly stale package index, which is the
    classic way an "upgrade" line ends up applying nothing. The cache cleanup
    must still close the layer so the index never ships inside the image.

    Only the FIRST apt layer — the base one — is required to upgrade. The
    client image has a SECOND apt chain (the optional MSSQL ODBC driver from
    Microsoft's repository, arch- and flag-guarded) which is deliberately left
    alone: it runs after the base upgrade and exists to add one vendor package.
    """
    layers = [args for args in _run_args(path) if "apt-get update" in args]
    assert layers, f"no apt layer in {path.name}: {_run_args(path)}"
    base = layers[0]
    # The base layer is the one that installs the native libs, never the
    # vendor-repo chain — asserted so a reordering cannot make this pin latch
    # onto the wrong layer and pass.
    assert "msodbcsql" not in base, base
    assert "apt-get install" in base, base
    assert "apt-get upgrade -y" in base, base
    assert base.rstrip().endswith(APT_CACHE_CLEANUP), base


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

# The jobs volume's KEY differs per file, but the real volume NAME no longer
# does: both files pin `name: pdc_client_exec_jobs` (asserted below), so it
# never depends on compose's project prefix. The DATA volume still does — the
# customer file lets it be prefixed, the local file declares it `external:
# true` and names it by hand.
JOBS_VOLUME_KEY = {COMPOSE_CUSTOMER: "pdc_client_exec_jobs", COMPOSE_LOCAL: "exec_jobs"}
JOBS_TARGET = "/jobs"
# The literal default of `PDC_BACKEND_SUBNET`. A /28, so the web
# container's route table gives up 16 addresses instead of a /16, and
# deliberately OUTSIDE 172.31.0.0/16 — AWS's default-VPC range — because a
# customer host inside such a VPC would otherwise blackhole part of its own
# network the moment Docker created this subnet (Docker does not consult the
# host routes for a user-specified subnet).
BACKEND_SUBNET_DEFAULT = "192.168.255.240/28"
BACKEND_SUBNET_EXPR = "${PDC_BACKEND_SUBNET:-" + BACKEND_SUBNET_DEFAULT + "}"
# What must never be mounted into the executor, as path fragments.
FORBIDDEN_EXECUTOR_MOUNT_TOKENS = (
    "data/client", "users", "chatdata", "db_snapshots", "data_sources", "roles",
    "sso", "logs",
)


def _services(path: Path) -> dict:
    doc = _load(path)
    services = doc.get("services") or {}
    assert isinstance(services, dict) and services, f"no services in {path.name}"
    return services


def _service(path: Path, name: str) -> dict:
    services = _services(path)
    assert name in services, sorted(services)
    svc = services[name]
    assert isinstance(svc, dict), svc
    return svc


def _service_cases():
    """(compose file, service name) for EVERY service in EVERY file.

    An earlier pass asserted the hardening block on `client` only, which
    left the hole this closes: a second service — the `pdc-executor`
    sandbox, the one container that runs untrusted code — could land without
    a read-only rootfs or a pid cap and the suite would stay green. The
    parametrization is built by READING the services map, so any service added
    later is pinned the moment it appears.
    """
    cases = []
    for param in COMPOSE_FILES:
        path = param.values[0]
        for name in sorted(_services(path)):
            cases.append(pytest.param(path, name, id=f"{param.id}-{name}"))
    return cases


SERVICE_CASES = _service_cases()


# ---------------------------------------------------------------------------
# Compose — every service in both files
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,service", SERVICE_CASES)
def test_compose_read_only_rootfs_with_tmp_tmpfs(path, service):
    svc = _service(path, service)
    read_only = svc.get("read_only")
    assert read_only is True, sorted(svc.keys())
    tmpfs = _tmpfs_entries(svc)
    assert any(str(e).split(":")[0] == "/tmp" for e in tmpfs), tmpfs


@pytest.mark.parametrize("path,service", SERVICE_CASES)
def test_compose_drops_all_capabilities_and_forbids_privilege_gain(path, service):
    svc = _service(path, service)
    cap_drop = svc.get("cap_drop")
    assert cap_drop == ["ALL"], cap_drop
    security_opt = svc.get("security_opt") or []
    assert "no-new-privileges:true" in security_opt, security_opt


@pytest.mark.parametrize("path,service", SERVICE_CASES)
def test_compose_sets_memory_and_pid_limits(path, service):
    svc = _service(path, service)
    mem_limit = svc.get("mem_limit")
    assert mem_limit, sorted(svc.keys())
    pids_limit = svc.get("pids_limit")
    assert isinstance(pids_limit, int) and pids_limit > 0, pids_limit


@pytest.mark.parametrize("path,service", SERVICE_CASES)
def test_compose_cpu_cap_stays_commented_out(path, service):
    """No `cpus` on either service, in either file.

    The sandbox runner already pins every numeric library to one thread and the
    web process no longer executes analysis, so a CPU cap here would only
    throttle the customer's own analysis on their own hardware. The commented
    `# cpus:` line stays as the documented opt-in.
    """
    svc = _service(path, service)
    assert "cpus" not in svc, sorted(svc.keys())


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


# ---------------------------------------------------------------------------
# Compose — the pdc-executor service (both files)
# ---------------------------------------------------------------------------
def _executor_service(path: Path) -> dict:
    return _service(path, "executor")


def _joined_networks(svc: dict):
    """The network names a service joins, for both the list and mapping forms."""
    nets = svc.get("networks")
    if nets is None:
        return []
    if isinstance(nets, dict):
        return [str(k) for k in nets]
    return [str(n) for n in nets]


def _env_text(value) -> str:
    """A compose value as the operator wrote it (quotes and whitespace stripped)."""
    return str(value).strip().strip("\"").strip("'")


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_declares_the_executor_service(path):
    """With no executor service a clean checkout answers every question
    `ExecutorUnavailable` — the dispatch hop has nowhere to go."""
    svc = _executor_service(path)
    container_name = svc.get("container_name")
    assert container_name == "pdc-executor", container_name
    image = svc.get("image")
    assert image == "powerdatachat-executor:enterprise", image
    restart = svc.get("restart")
    assert restart == "unless-stopped", restart


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_executor_publishes_no_ports_and_reads_no_env_file(path):
    """The sandbox is reachable ONLY from the client over the internal network.

    A published port would expose the unauthenticated `/execute` to the LAN;
    an `env_file` would hand it the client's secrets — and while the image's
    self-check refuses to start on any `BRAIN_*`/`SECRET_KEY`/... value, that
    refusal is the second line of defence, not the design. Every other
    settings field has a default, so a mis-set value would NOT fail loudly.
    """
    svc = _executor_service(path)
    assert "ports" not in svc, sorted(svc.keys())
    assert "env_file" not in svc, sorted(svc.keys())


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_executor_limit_values_match_what_the_docs_promise(path):
    """The sandbox's own numbers — the operator-facing pins.

    `mem_limit` 3g is the container wall around the runner's own
    `EXECUTOR_MEM_LIMIT_MB=2048` RLIMIT_AS, and /tmp is 1g because that tmpfs
    is the only place generated code may write — RAM-backed, so it is charged
    against the same 3g.
    """
    svc = _executor_service(path)
    mem_limit = svc.get("mem_limit")
    assert mem_limit == "3g", mem_limit
    pids_limit = svc.get("pids_limit")
    assert pids_limit == 256, pids_limit
    tmp = [str(e) for e in _tmpfs_entries(svc) if str(e).split(":")[0] == "/tmp"]
    assert len(tmp) == 1, _tmpfs_entries(svc)
    options = tmp[0].split(":", 1)[1] if ":" in tmp[0] else ""
    assert "size=1g" in options, tmp
    assert "mode=1777" in options, tmp
    env = _environment(svc)
    mem_mb = _env_text(env.get("EXECUTOR_MEM_LIMIT_MB"))
    assert mem_mb == "2048", env


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_executor_joins_only_the_backend_network(path):
    """No route to the internet or the customer LAN for generated code."""
    nets = _joined_networks(_executor_service(path))
    assert set(nets) == {"backend"}, nets


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_client_joins_both_the_default_and_backend_networks(path):
    """The client needs `default` (browser + brain over HTTPS) AND `backend`
    (the only way to reach the sandbox)."""
    nets = _joined_networks(_service(path, "client"))
    assert set(nets) == {"default", "backend"}, nets


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_backend_network_is_internal(path):
    doc = _load(path)
    networks = doc.get("networks") or {}
    assert "backend" in networks, sorted(networks)
    backend = networks["backend"] or {}
    internal = backend.get("internal")
    assert internal is True, backend


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_backend_subnet_and_client_cidr_are_one_expression(path):
    """The network's ipam subnet and the app guard's CIDR must be the
    SAME text, so the two can never disagree.

    The app refuses (403) any request whose socket peer is inside
    `EXECUTOR_NETWORK_CIDR`; if that value drifted from the subnet Docker
    actually allocated, generated code could reach the unauthenticated login
    and password-reset endpoints over the bidirectional backend network.
    PyYAML keeps `${PDC_BACKEND_SUBNET:-...}` as plain text, so the two are
    compared as strings — one compose variable, one default.
    """
    doc = _load(path)
    config = ((doc.get("networks") or {}).get("backend") or {}).get("ipam", {}).get("config")
    assert isinstance(config, list) and config, doc.get("networks")
    subnet = str(config[0].get("subnet"))
    cidr = _env_text(_environment(_service(path, "client")).get("EXECUTOR_NETWORK_CIDR"))
    assert subnet == BACKEND_SUBNET_EXPR, subnet
    assert cidr == BACKEND_SUBNET_EXPR, cidr
    assert BACKEND_SUBNET_DEFAULT in subnet, subnet


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_client_and_executor_share_one_jobs_volume_at_slash_jobs(path):
    """The jobs volume is the ONE shared surface between the two containers."""
    client_pairs = _volume_pairs(_service(path, "client"))
    executor_pairs = _volume_pairs(_executor_service(path))
    client_jobs = [src for src, target in client_pairs if target == JOBS_TARGET]
    executor_jobs = [src for src, target in executor_pairs if target == JOBS_TARGET]
    assert len(client_jobs) == 1, client_pairs
    assert len(executor_jobs) == 1, executor_pairs
    assert client_jobs[0] == executor_jobs[0], (client_jobs, executor_jobs)


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_executor_mounts_nothing_but_the_jobs_volume(path):
    targets = {target for _, target in _volume_pairs(_executor_service(path))}
    assert targets == {JOBS_TARGET}, targets


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_executor_mount_never_names_a_client_data_store(path):
    """The explicit "never mount this into the executor" list.

    The previous test pins the positive shape; this one names the forbidden
    stores so a future mount of users / chats / snapshots / credentials /
    roles / sso / logs fails with the offending token printed, instead of only
    as a set mismatch.
    """
    for source, target in _volume_pairs(_executor_service(path)):
        text = f"{source}:{target}".lower()
        for token in FORBIDDEN_EXECUTOR_MOUNT_TOKENS:
            assert token not in text, (token, source, target)


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_client_waits_for_a_healthy_executor(path):
    """`depends_on: service_healthy` is what makes the jobs volume root come up
    `root:pdc 2770`: the executor image seeds the still-empty volume first
    A client that started first would create the root as uid 10001
    with umask 022 — unwritable for the sandbox's own sweep.
    """
    depends_on = _service(path, "client").get("depends_on")
    assert isinstance(depends_on, dict), depends_on
    assert "executor" in depends_on, depends_on
    entry = depends_on["executor"]
    assert isinstance(entry, dict), entry
    condition = entry.get("condition")
    assert condition == "service_healthy", entry


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_pins_max_concurrent_to_one_on_both_services(path):
    """One job at a time on BOTH sides.

    Raising either forfeits the isolation the sandbox exists for: the
    executor's same-uid stray sweep is skipped above 1, and the app-side
    dispatch gate is what keeps the sandbox's own queue empty.
    """
    for service in ("client", "executor"):
        env = _environment(_service(path, service))
        value = _env_text(env.get("EXECUTOR_MAX_CONCURRENT"))
        assert value == "1", (service, env)


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_client_and_executor_agree_on_the_shared_dir(path):
    """The two sides must name the same mount point or every job is refused.

    The app creates `<EXECUTOR_SHARED_DIR>/<job_id>` and posts that path; the
    sandbox re-checks it against its OWN `EXECUTOR_SHARED_DIR` and answers
    400 `JOB_DIR_INVALID` when the containment check fails.
    """
    client_dir = _env_text(_environment(_service(path, "client")).get("EXECUTOR_SHARED_DIR"))
    executor_dir = _env_text(_environment(_executor_service(path)).get("EXECUTOR_SHARED_DIR"))
    assert client_dir == JOBS_TARGET, client_dir
    assert executor_dir == JOBS_TARGET, executor_dir


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_client_points_at_the_executor_by_service_name(path):
    url = _env_text(_environment(_service(path, "client")).get("EXECUTOR_URL"))
    assert url == "http://pdc-executor:8090", url


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_executor_inherits_the_images_healthcheck(path):
    """No compose override: the image owns the probe (a python one-liner
    against 127.0.0.1:8090/healthz — there is no curl in that image), so the
    healthy condition means the same thing however the stack is started."""
    svc = _executor_service(path)
    assert "healthcheck" not in svc, sorted(svc.keys())


JOBS_VOLUME_REAL_NAME = "pdc_client_exec_jobs"


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_jobs_volume_pins_its_real_name(path):
    """Without `name:`, compose prefixes the project, so the volume the stack
    actually uses would be called something different on every install —
    while the ownership-repair command in the install guide names ONE fixed
    volume. That command would then create and repair an empty volume nobody
    mounts and report success. Pinned in BOTH files so they resolve to the
    same name and every document that names it stays true."""
    doc = _load(path)
    volumes = doc.get("volumes") or {}
    key = JOBS_VOLUME_KEY[path]
    declaration = volumes.get(key) or {}
    assert isinstance(declaration, dict), declaration
    name = declaration.get("name")
    assert name == JOBS_VOLUME_REAL_NAME, declaration


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_compose_jobs_volume_is_declared_and_never_external(path):
    """The jobs volume holds nothing persistent — only in-flight job dirs — so
    compose must be free to create it, which is also what gives the volume its
    root ownership from the executor image."""
    doc = _load(path)
    volumes = doc.get("volumes") or {}
    key = JOBS_VOLUME_KEY[path]
    assert key in volumes, sorted(volumes)
    declaration = volumes[key] or {}
    external = declaration.get("external") if isinstance(declaration, dict) else None
    assert not external, declaration
    mounted = {src for src, target in _volume_pairs(_executor_service(path))
               if target == JOBS_TARGET}
    assert mounted == {key}, mounted


# ---------------------------------------------------------------------------
# Compose — the executor service, per file
# ---------------------------------------------------------------------------
def test_local_compose_executor_builds_from_the_executor_dockerfile():
    svc = _executor_service(COMPOSE_LOCAL)
    build = svc.get("build")
    assert isinstance(build, dict), build
    context = build.get("context")
    assert context == ".", build
    dockerfile = build.get("dockerfile")
    assert dockerfile == "executor/Dockerfile", build
    args = build.get("args") or {}
    assert "BUILD_COMMIT" in args and "BUILD_TIME" in args, args


def test_customer_compose_executor_is_image_only():
    """Customers never build — they run the two images they were handed."""
    svc = _executor_service(COMPOSE_CUSTOMER)
    assert "build" not in svc, sorted(svc.keys())


# ---------------------------------------------------------------------------
# What the build context hands to a customer
#
# The web image is built with a WHOLE-TREE `COPY . .`, so every file sitting
# in the working tree at build time lands in `/app` and is shipped. Some of
# this repository's working material is local-only and must not ship;
# git-ignoring it keeps it out of the REPOSITORY, which is a different thing
# from keeping it out of the ARTIFACT, and only `.dockerignore` does the
# second. The two facts — a whole-tree copy and the exclusion list — are only
# safe TOGETHER, which is why both are pinned here: a reader who changes one
# has to see the other.
# ---------------------------------------------------------------------------
DOCKERIGNORE = ROOT / ".dockerignore"

# Entries that must be in the list. The first two are the local working
# material; the rest were already there and are restated so a rewrite of the
# file cannot quietly drop them.
REQUIRED_IGNORES = ["docs/security/", "*.docx", ".git", "*.env", ".claude/",
                    ".venv", "client_data/", "__pycache__"]

# Paths in the tree that must never reach the image, as glob patterns
# evaluated against the tree itself (so a NEW one of these is caught, not
# just a deleted line).
MUST_NOT_SHIP_GLOBS = ["**/*.docx", "**/*.env"]
MUST_NOT_SHIP_DIRS = ["docs/security", ".git", ".claude"]


def _dockerignore_patterns() -> tuple:
    """The file's effective patterns, parsed — not grepped.

    Returns `(excludes, negations)`. Comments and blank lines are dropped, so
    a commented-out entry cannot satisfy a pin, and `!`-prefixed lines are
    kept apart because they RE-INCLUDE (the file uses one for
    `*.env.example`).
    """
    assert DOCKERIGNORE.is_file(), f"missing {DOCKERIGNORE}"
    excludes, negations = [], []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            negations.append(line[1:].strip())
        else:
            excludes.append(line)
    return tuple(excludes), tuple(negations)


def _matches(pattern: str, relative: str) -> bool:
    """Whether a `.dockerignore` pattern excludes `relative` (or its parent).

    A deliberate approximation of Docker's matcher, narrowed to the shapes
    this file uses: patterns are compared SEGMENT BY SEGMENT, because `*` does
    not cross a `/` — `*.docx` matches a top-level file only, NOT
    `docs/x.docx`. Stated explicitly so nobody "simplifies" that entry into
    something that reads broader than it behaves. A pattern that matches a
    leading run of segments excludes the whole subtree below it, which is how
    a directory entry works.
    """
    pattern_parts = [p for p in pattern.strip("/").split("/") if p]
    path_parts = [p for p in relative.strip("/").split("/") if p]
    if not pattern_parts or len(pattern_parts) > len(path_parts):
        return False
    return all(fnmatch.fnmatch(path_parts[i], pattern_parts[i])
               for i in range(len(pattern_parts)))


def _is_excluded(relative: str) -> bool:
    excludes, negations = _dockerignore_patterns()
    if any(_matches(pattern, relative) for pattern in negations):
        return False
    return any(_matches(pattern, relative) for pattern in excludes)


@pytest.mark.parametrize("entry", REQUIRED_IGNORES)
def test_dockerignore_lists_the_entry(entry):
    """Parsed, so a commented-out line cannot pass for a live one."""
    excludes, _ = _dockerignore_patterns()
    normalized = {value.rstrip("/") for value in excludes}
    assert entry.rstrip("/") in normalized, sorted(excludes)


@pytest.mark.parametrize("relative", MUST_NOT_SHIP_DIRS)
def test_a_local_only_directory_is_excluded_from_the_build_context(relative):
    """The requirement stated as itself: this path must not ship.

    Asserted against the matcher rather than against a line of text, and it
    holds whether or not the directory exists locally — a checkout without it
    must still be unable to ship it.
    """
    excluded = _is_excluded(relative)
    assert excluded is True, (relative, _dockerignore_patterns()[0])


@pytest.mark.parametrize("glob", MUST_NOT_SHIP_GLOBS)
def test_every_such_file_in_the_tree_is_excluded(glob):
    """The STRONGER shape: driven by the tree, so a new file of one of these
    kinds in a directory the list does not reach fails here instead of
    shipping. Vacuously true when the tree holds none, which is the honest
    answer in that case."""
    offenders = []
    for path in sorted(ROOT.glob(glob)):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        if ".venv" in relative.split("/") or ".git" in relative.split("/"):
            continue
        if not _is_excluded(relative):
            offenders.append(relative)
    assert offenders == [], offenders


def test_the_web_image_still_copies_the_whole_tree():
    """This is WHY the exclusion list is load-bearing.

    If this ever becomes a list of named files (the shape the sandbox image
    uses), the ignore file stops being the only thing between the working
    tree and the customer — and the reasoning above has to be revisited in
    the same change rather than left as a stale comment.
    """
    text = _dockerfile_text(DOCKERFILE)
    copies = [line.strip() for line in text.splitlines()
              if line.strip().upper().startswith("COPY ")]
    whole_tree = [line for line in copies if line.split()[1:] == [".", "."]]
    assert whole_tree, copies


def test_the_sandbox_image_copies_named_files_only():
    """The sandbox image was never exposed to this, and that is a property
    worth keeping: it copies named files, so the working tree cannot leak
    into it however the ignore file changes."""
    text = _dockerfile_text(EXEC_DOCKERFILE)
    copies = [line.strip() for line in text.splitlines()
              if line.strip().upper().startswith(("COPY ", "ADD "))]
    assert copies, text[:200]
    whole_tree = [line for line in copies if line.split()[1:] == [".", "."]]
    assert whole_tree == [], whole_tree
