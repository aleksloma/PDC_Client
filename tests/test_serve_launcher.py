"""`serve.py`, the image's launcher (CMD `python serve.py`).

Without TLS settings it must BE the command the image always ran -- the same
argv, one worker, process replaced -- and with them the same command plus the
certificate, key and cipher options. uvicorn's default cipher string leaves
TLS 1.2 with CBC suites only, which current clients no longer offer, so the
launcher's string is pinned to forward-secret AEAD suites and proven with real
handshakes. `tls_is_served` is how the app recognises a start that bypassed
the launcher and would answer in plain HTTP. The proxy behaviour must not move in either mode:
uvicorn's own CLI and Config are driven with the old argv and the launcher's,
under every shape of FORWARDED_ALLOW_IPS, and the resulting
ProxyHeadersMiddleware is exercised from trusted and untrusted peers.
"""
import ast
import asyncio
import importlib
import os
import runpy
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn.config

import serve
from settings import settings
from tls_test_certs import write_pair

ROOT = Path(__file__).resolve().parent.parent
# `uvicorn.main` the attribute is the click command; this is the module.
UVICORN_MAIN = importlib.import_module("uvicorn.main")
# The command the image ran before the launcher existed.
OLD_ARGV = ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"


def _tls_argv(cert, key):
    return OLD_ARGV + ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key),
                       "--ssl-ciphers", CIPHERS]


class _Replaced(Exception):
    """Raised by the fake execvp: the real one never returns."""


@pytest.fixture
def execvp(monkeypatch):
    calls = []

    def fake(file, argv):
        calls.append((file, list(argv), dict(os.environ)))
        raise _Replaced

    monkeypatch.setattr(os, "execvp", fake)
    # main() replaces the whole environment; the test process needs its own back.
    environ = dict(os.environ)
    try:
        yield calls
    finally:
        os.environ.clear()
        os.environ.update(environ)


# The environment the launcher process was started with (Windows folds the
# names to upper case, so these are upper case already).
START_ENV = {"PATH": "/usr/local/bin:/usr/bin", "FORWARDED_ALLOW_IPS": "10.0.0.9",
             "DATA_ROOT": "/data/client"}


@pytest.fixture
def start_env(monkeypatch):
    """A known start environment, and a process environment that has since
    gained a variable and changed another (what a `.env` file does)."""
    monkeypatch.setattr(serve, "_START_ENV", dict(START_ENV))
    monkeypatch.setenv("UVICORN_ROOT_PATH", "/from-dotenv")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")


def _tls(monkeypatch, cert="", key=""):
    monkeypatch.setattr(settings, "TLS_CERT_FILE", str(cert))
    monkeypatch.setattr(settings, "TLS_KEY_FILE", str(key))


# ---------------------------------------------------------------------------
# argv and exec
# ---------------------------------------------------------------------------
def test_argv_without_tls_is_exactly_the_old_command():
    assert serve.build_argv(None) == OLD_ARGV


def test_argv_with_tls_is_the_old_command_plus_certificate_key_and_ciphers():
    argv = serve.build_argv(("/certs/server.crt", "/certs/server.key"))
    assert argv == ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000",
                    "--workers", "1",
                    "--ssl-certfile", "/certs/server.crt",
                    "--ssl-keyfile", "/certs/server.key",
                    "--ssl-ciphers", "ECDHE+AESGCM:ECDHE+CHACHA20"]


def test_build_argv_returns_a_fresh_list_each_time():
    serve.build_argv(None).append("--reload")
    assert serve.build_argv(None) == OLD_ARGV


def test_main_replaces_the_process_with_the_old_command(monkeypatch, start_env, execvp):
    _tls(monkeypatch)
    with pytest.raises(_Replaced):
        serve.main()
    assert len(execvp) == 1
    file, argv, env = execvp[0]
    assert (file, argv) == ("uvicorn", OLD_ARGV)
    assert env == START_ENV, "uvicorn gets exactly the environment of the start"


def test_main_with_tls_execs_the_tls_command(monkeypatch, tmp_path, start_env, execvp):
    cert, key = write_pair(tmp_path)
    _tls(monkeypatch, cert, key)
    with pytest.raises(_Replaced):
        serve.main()
    assert len(execvp) == 1
    file, argv, env = execvp[0]
    assert file == "uvicorn"
    assert argv == _tls_argv(cert, key)
    assert env == START_ENV


def test_a_bad_configuration_never_reaches_exec(monkeypatch, tmp_path, execvp):
    cert, _key = write_pair(tmp_path)
    _tls(monkeypatch, cert, "")
    with pytest.raises(SystemExit) as exc:
        serve.main()
    assert exc.value.code == 1
    assert execvp == []


def test_running_the_file_as_a_script_execs(monkeypatch, execvp):
    """The image runs `python serve.py`, so the file must call main()."""
    _tls(monkeypatch)
    with pytest.raises(_Replaced):
        runpy.run_path(str(ROOT / "serve.py"), run_name="__main__")
    assert [c[:2] for c in execvp] == [("uvicorn", OLD_ARGV)]


def test_exec_is_the_only_way_the_server_is_started():
    """No child process: PID 1 must BE uvicorn, so a stop signal reaches it."""
    tree = ast.parse((ROOT / "serve.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"subprocess", "multiprocessing", "uvicorn", "app", "routes"}, imported
    source = ast.unparse(tree)
    assert "os.spawn" not in source and "os.system" not in source


def test_importing_serve_has_no_side_effect(tmp_path):
    """Not even a refusal: the app imports it on every boot."""
    env = dict(os.environ)
    env.update({"DATA_ROOT": str(tmp_path), "TLS_CERT_FILE": "/nope/x.crt",
                "TLS_KEY_FILE": ""})
    code = ("import os, sys\n"
            "def boom(*a):\n"
            "    raise RuntimeError('EXEC')\n"
            "os.execvp = boom\n"
            "import settings, logger_utils\n"
            "before = dict(os.environ)\n"
            "import serve\n"
            "assert dict(os.environ) == before\n"
            "assert 'app' not in sys.modules and 'uvicorn' not in sys.modules\n"
            "print('IMPORTED')\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "IMPORTED" in proc.stdout
    assert "TLS_" not in proc.stdout + proc.stderr


def test_a_dotenv_file_does_not_reach_uvicorns_own_configuration(tmp_path):
    """Under the old command uvicorn read FORWARDED_ALLOW_IPS and its
    UVICORN_* options BEFORE the app -- and so settings.py's load_dotenv() --
    was imported; a `.env` file next to the app never influenced them. The
    launcher imports settings first, so what load_dotenv() adds to the
    environment must not be handed on to uvicorn."""
    for name in ("serve.py", "settings.py", "logger_utils.py"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / ".env").write_text("FORWARDED_ALLOW_IPS=*\nUVICORN_ROOT_PATH=/x\n",
                                   encoding="utf-8")
    (tmp_path / "drive.py").write_text(
        "import os\n"
        "def fake(file, argv, env=None):\n"
        "    env = os.environ if env is None else env\n"
        "    print('EXEC', env.get('FORWARDED_ALLOW_IPS'), env.get('UVICORN_ROOT_PATH'))\n"
        "    raise SystemExit(0)\n"
        "os.execvp = os.execvpe = fake\n"
        "import serve\n"
        "serve.main()\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORWARDED_ALLOW_IPS", "UVICORN_ROOT_PATH")}
    env.update({"DATA_ROOT": str(tmp_path / "data"), "TLS_CERT_FILE": "", "TLS_KEY_FILE": ""})
    proc = subprocess.run([sys.executable, "drive.py"], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("EXEC")]
    assert lines == ["EXEC None None"], (lines, proc.stderr[-1500:])


# ---------------------------------------------------------------------------
# Proxy behaviour: uvicorn's own CLI and Config, old argv against the launcher
# ---------------------------------------------------------------------------
async def _echo(scope, receive, send):
    scope["seen"].append((scope["client"], scope["scheme"]))


def _config(monkeypatch, argv):
    """The Config uvicorn's CLI builds for `argv` (nothing is served)."""
    built = []

    class FakeServer:
        started = True

        def __init__(self, config):
            built.append(config)

        def run(self):
            pass

    monkeypatch.setattr(UVICORN_MAIN, "Server", FakeServer)
    # Config() would otherwise reconfigure the process's logging.
    monkeypatch.setattr(uvicorn.config.Config, "configure_logging", lambda self: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert argv[0] == "uvicorn"
    UVICORN_MAIN.main.main(args=argv[1:], prog_name="uvicorn", standalone_mode=False)
    assert len(built) == 1
    return built[0]


def _loaded(monkeypatch, argv):
    config = _config(monkeypatch, argv)
    config.app = _echo              # never import the real application here
    config.interface = "asgi3"
    config.load()
    return config


def _forwarded(config, peer, headers):
    """(client host, scheme) the application would see for one request."""
    seen = []
    scope = {"type": "http", "client": (peer, 5555), "server": ("0.0.0.0", 8000),
             "scheme": "https" if config.is_ssl else "http", "method": "GET", "path": "/",
             "headers": headers, "seen": seen}
    asyncio.run(config.loaded_app(scope, None, None))
    (client, scheme), = seen
    return client[0], scheme


ALLOW = [None, "10.0.0.9", "10.0.0.0/24", "", "*"]
PEERS = ["127.0.0.1", "10.0.0.9", "203.0.113.7", "192.168.255.242"]
HEADERS = [
    [],
    [(b"x-forwarded-for", b"198.51.100.1")],
    [(b"x-forwarded-for", b"198.51.100.1"), (b"x-forwarded-proto", b"https")],
    [(b"x-forwarded-for", b"198.51.100.1, 10.0.0.9"), (b"x-forwarded-proto", b"http")],
]


@pytest.mark.parametrize("tls", [False, True], ids=["plain", "tls"])
@pytest.mark.parametrize("allow", ALLOW, ids=["unset", "address", "network", "empty", "star"])
def test_proxy_behaviour_is_identical_to_the_old_command(monkeypatch, tmp_path, allow, tls):
    if allow is None:
        monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    else:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", allow)
    pair = tuple(str(p) for p in write_pair(tmp_path)) if tls else None
    old = _loaded(monkeypatch, OLD_ARGV)
    new = _loaded(monkeypatch, serve.build_argv(pair))
    assert new.proxy_headers is old.proxy_headers is True
    assert new.forwarded_allow_ips == old.forwarded_allow_ips
    assert new.forwarded_allow_ips == ("127.0.0.1" if allow is None else allow)
    assert type(new.loaded_app) is type(old.loaded_app)
    for peer in PEERS:
        for headers in HEADERS:
            old_client, old_scheme = _forwarded(old, peer, headers)
            new_client, new_scheme = _forwarded(new, peer, headers)
            assert new_client == old_client, (allow, peer, headers)
            # X-Forwarded-Proto from a trusted peer decides the scheme in both
            # modes; otherwise it is the connection's own scheme.
            proto_applied = (b"x-forwarded-proto" in dict(headers)
                             and peer in old.loaded_app.trusted_hosts)
            if proto_applied or not tls:
                assert new_scheme == old_scheme, (allow, peer, headers)
            else:
                assert (old_scheme, new_scheme) == ("http", "https")


@pytest.mark.parametrize("tls", [False, True], ids=["plain", "tls"])
def test_a_trusted_peer_is_believed_and_an_untrusted_one_is_not(monkeypatch, tmp_path, tls):
    """The absolute half of the comparison above."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.9")
    pair = tuple(str(p) for p in write_pair(tmp_path)) if tls else None
    config = _loaded(monkeypatch, serve.build_argv(pair))
    headers = [(b"x-forwarded-for", b"198.51.100.1")]
    assert _forwarded(config, "10.0.0.9", headers)[0] == "198.51.100.1"
    for untrusted in ("127.0.0.1", "203.0.113.7", "192.168.255.242"):
        assert _forwarded(config, untrusted, headers)[0] == untrusted


def _settings_of(config):
    skip = {"app", "loaded_app", "ssl_certfile", "ssl_keyfile", "ssl_ciphers", "ssl",
            "lifespan_class", "http_protocol_class", "ws_protocol_class"}
    return {k: v for k, v in vars(config).items() if k not in skip}


@pytest.mark.parametrize("tls", [False, True], ids=["plain", "tls"])
def test_every_other_server_option_is_the_old_commands(monkeypatch, tmp_path, tls):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    pair = tuple(str(p) for p in write_pair(tmp_path)) if tls else None
    old = _config(monkeypatch, OLD_ARGV)
    new = _config(monkeypatch, serve.build_argv(pair))
    assert _settings_of(new) == _settings_of(old)
    assert (new.app, new.host, new.port, new.workers) == ("app:app", "0.0.0.0", 8000, 1)
    assert new.is_ssl is tls
    assert new.ssl_ciphers == (CIPHERS if tls else old.ssl_ciphers)


# ---------------------------------------------------------------------------
# The TLS context uvicorn builds from the launcher's argv
# ---------------------------------------------------------------------------
def _server_context(monkeypatch, tmp_path, kind="ec"):
    """(the SSLContext uvicorn's own Config.load() builds, certificate path)."""
    cert, key = write_pair(tmp_path, kind=kind)
    config = _loaded(monkeypatch, serve.build_argv((str(cert), str(key))))
    assert isinstance(config.ssl, ssl.SSLContext)
    return config.ssl, cert


def _handshake(server_ctx, client_ctx):
    """A real handshake over memory BIOs -> (version, cipher name)."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname="localhost")
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    done = set()
    for _ in range(10):
        for name, side in (("client", client), ("server", server)):
            try:
                side.do_handshake()
                done.add(name)
            except ssl.SSLWantReadError:
                pass
            s_in.write(c_out.read())
            c_in.write(s_out.read())
        if len(done) == 2:
            return client.version(), client.cipher()[0]
    raise AssertionError("the handshake did not finish")


def _client_context(cert, minimum=None, maximum=None):
    """A DEFAULT Python client that trusts nothing but this certificate."""
    ctx = ssl.create_default_context(cafile=str(cert))
    if minimum is not None:
        ctx.minimum_version = minimum
    if maximum is not None:
        ctx.maximum_version = maximum
    return ctx


def test_only_forward_secret_aead_suites_are_enabled(monkeypatch, tmp_path):
    ctx, _cert = _server_context(monkeypatch, tmp_path)
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    suites = ctx.get_ciphers()
    old = [c for c in suites if c["protocol"] != "TLSv1.3"]
    assert old, "no TLS 1.2 suite is enabled"
    for c in suites:
        assert c["aead"] is True, c["name"]                # no CBC, no NULL
        assert c["strength_bits"] >= 128, c["name"]
    for c in old:
        assert c["name"].startswith(("ECDHE-RSA-", "ECDHE-ECDSA-")), c["name"]
        assert c["kea"] == "kx-ecdhe", c["name"]           # no RSA key exchange
        assert c["auth"] in ("auth-rsa", "auth-ecdsa"), c["name"]   # no anonymous, no PSK
        assert "GCM" in c["name"] or "CHACHA20" in c["name"], c["name"]
    assert any(c["protocol"] == "TLSv1.3" for c in suites)


@pytest.mark.parametrize("kind", ["ec", "rsa"])
def test_a_default_tls_1_2_client_connects(monkeypatch, tmp_path, kind):
    """The regression: uvicorn's default cipher string shares no suite with a
    current client that only speaks TLS 1.2."""
    ctx, cert = _server_context(monkeypatch, tmp_path, kind)
    version, cipher = _handshake(
        ctx, _client_context(cert, maximum=ssl.TLSVersion.TLSv1_2))
    assert version == "TLSv1.2"
    assert cipher.startswith("ECDHE-") and ("GCM" in cipher or "CHACHA20" in cipher), cipher


@pytest.mark.parametrize("kind", ["ec", "rsa"])
def test_tls_1_3_works(monkeypatch, tmp_path, kind):
    ctx, cert = _server_context(monkeypatch, tmp_path, kind)
    version, _ = _handshake(ctx, _client_context(cert, minimum=ssl.TLSVersion.TLSv1_3))
    assert version == "TLSv1.3"
    assert _handshake(ctx, _client_context(cert))[0] == "TLSv1.3"


@pytest.mark.parametrize("suite", ["ECDHE-ECDSA-AES128-SHA", "ECDHE-ECDSA-AES256-SHA384",
                                   "AES128-SHA", "AES128-GCM-SHA256", "DHE-RSA-AES128-GCM-SHA256"])
def test_a_client_offering_only_a_weaker_suite_is_refused(monkeypatch, tmp_path, suite):
    ctx, cert = _server_context(monkeypatch, tmp_path,
                                "ec" if "ECDSA" in suite else "rsa")
    client = _client_context(cert, maximum=ssl.TLSVersion.TLSv1_2)
    try:
        client.set_ciphers(suite)
    except ssl.SSLError:
        pytest.skip(f"this OpenSSL does not offer {suite}")
    with pytest.raises(ssl.SSLError):
        _handshake(ctx, client)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize("version", ["TLSv1", "TLSv1_1"])
def test_a_tls_1_0_or_1_1_client_is_refused(monkeypatch, tmp_path, version):
    ctx, cert = _server_context(monkeypatch, tmp_path)
    old = getattr(ssl.TLSVersion, version)
    try:
        client = _client_context(cert)
        client.set_ciphers("DEFAULT:@SECLEVEL=0")
        client.minimum_version = client.maximum_version = old
        # Control: a deliberately weak server does speak it here, so a refusal
        # below is the server's doing and not this OpenSSL's.
        weak = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        weak.load_cert_chain(str(cert), str(cert.with_suffix(".key")))
        weak.set_ciphers("DEFAULT:@SECLEVEL=0")
        weak.minimum_version = weak.maximum_version = old
        _handshake(weak, client)
    except (ValueError, ssl.SSLError, AssertionError):
        pytest.skip(f"this OpenSSL cannot offer {version}")
    with pytest.raises(ssl.SSLError):
        _handshake(ctx, client)


# ---------------------------------------------------------------------------
# tls_is_served: was uvicorn told, on its command line, to serve TLS?
# ---------------------------------------------------------------------------
CRT, KEY = "/certs/server.crt", "/certs/server.key"
SERVED = [
    _tls_argv(CRT, KEY),
    ["uvicorn", "app:app", "--ssl-certfile", CRT, "--ssl-keyfile", KEY],
    ["uvicorn", f"--ssl-keyfile={KEY}", f"--ssl-certfile={CRT}", "app:app"],
    ["uvicorn", "app:app", f"--ssl-certfile={CRT}", "--ssl-keyfile", KEY],
    ["/usr/local/bin/uvicorn", "app:app", "--ssl-certfile", CRT, f"--ssl-keyfile={KEY}"],
]
NOT_SERVED = [
    OLD_ARGV,
    ["uvicorn", "app:app"],
    ["python", "-c"],
    [],
    ["uvicorn", "app:app", "--ssl-certfile", CRT],
    ["uvicorn", "app:app", "--ssl-keyfile", KEY],
    ["uvicorn", "app:app", f"--ssl-certfile={CRT}"],
    ["uvicorn", "app:app", "--ssl-certfile", CRT, "--ssl-keyfile"],
    ["uvicorn", "app:app", "--ssl-certfile", CRT, "--ssl-keyfile", ""],
    ["uvicorn", "app:app", "--ssl-certfile", "", "--ssl-keyfile", KEY],
    ["uvicorn", "app:app", "--ssl-certfile=", f"--ssl-keyfile={KEY}"],
    ["uvicorn", "app:app", f"--ssl-certfile={CRT}", "--ssl-keyfile="],
    ["uvicorn", "app:app", "--ssl-ciphers", CIPHERS],
    ["uvicorn", "app:app", "--ssl-keyfile-password", "x", "--ssl-certfile", CRT],
    ["uvicorn", "app:app", "--ssl-certfilex", CRT, "--ssl-keyfilex", KEY],
    ["uvicorn", "app:app", CRT, KEY],
]


@pytest.mark.parametrize("argv", SERVED)
def test_a_start_with_both_options_is_served(argv):
    assert serve.tls_is_served(argv) is True


@pytest.mark.parametrize("argv", NOT_SERVED)
def test_any_other_start_is_not_served(argv):
    assert serve.tls_is_served(argv) is False


@pytest.mark.parametrize("argv", [None, 5, [None, 7], "--ssl-certfile x --ssl-keyfile y"])
def test_tls_is_served_never_raises(argv):
    assert serve.tls_is_served(argv) is False


def test_uvicorn_environment_options_do_not_count(monkeypatch):
    """`.env` is loaded into os.environ AFTER uvicorn read its options, so a
    UVICORN_SSL_* variable says nothing about what is being served."""
    monkeypatch.setenv("UVICORN_SSL_CERTFILE", CRT)
    monkeypatch.setenv("UVICORN_SSL_KEYFILE", KEY)
    assert serve.tls_is_served(OLD_ARGV) is False
    assert serve.tls_is_served(["uvicorn", "app:app", "--ssl-certfile", CRT]) is False


def test_the_launchers_own_argv_decides(tmp_path):
    assert serve.tls_is_served(serve.build_argv((CRT, KEY))) is True
    assert serve.tls_is_served(serve.build_argv(None)) is False


@pytest.mark.parametrize("extra", [
    ["--ssl-certfile", "{crt}", "--ssl-keyfile", "{key}"],
    ["--ssl-certfile={crt}", "--ssl-keyfile={key}"],
    ["--ssl-certfile", "{crt}", "--ssl-keyfile", "{key}", "--ssl-ciphers", CIPHERS],
    ["--ssl-certfile", "{crt}"],
    [],
    # uvicorn takes the LAST value of a repeated option.
    ["--ssl-certfile", "{crt}", "--ssl-keyfile", "{key}", "--ssl-certfile=",
     "--ssl-keyfile="],
], ids=["spaced", "equals", "launcher", "cert-only", "none", "repeated-empty"])
def test_served_agrees_with_uvicorns_own_cli(monkeypatch, tmp_path, extra):
    """True only when uvicorn's own parser ends up with both files: True for a
    plain-HTTP server would be the silent fall back this check exists for."""
    cert, key = write_pair(tmp_path)
    argv = OLD_ARGV + [a.replace("{crt}", str(cert)).replace("{key}", str(key)) for a in extra]
    config = _config(monkeypatch, argv)
    both = bool(config.ssl_certfile) and bool(config.ssl_keyfile)
    assert serve.tls_is_served(argv) is both
