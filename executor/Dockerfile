# PowerDataChat Client — the pdc-executor sidecar (generated Python runs HERE,
# never in the web process). Build from the REPOSITORY ROOT:
#   docker build -f executor/Dockerfile -t powerdatachat-executor:enterprise .
#     the plain build = the LAST stage = `runtime` (uid 10002, no pytest)
#   docker build -f executor/Dockerfile --target test -t pdc-executor-test .
#     the privileged test image (root + pytest), asked for EXPLICITLY only
FROM python:3.12-slim AS app

WORKDIR /app

# DATA_ROOT is a tmpfs throwaway: the executor keeps NO state. It exists only
# so logger_utils has a writable log directory (5 MiB x 2, so a chatty job
# cannot compete with the 1 GiB /tmp). The thread pins and the system Arrow
# allocator keep a job inside its RLIMIT_AS (jemalloc reserves large virtual
# ranges that the limit counts).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_ROOT=/tmp/executor \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl \
    XDG_CACHE_HOME=/tmp/cache \
    HOME=/tmp \
    EXECUTOR_SHARED_DIR=/jobs \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    ARROW_IO_THREADS=1 \
    ARROW_DEFAULT_MEMORY_POOL=system \
    LOG_MAX_BYTES=5242880 \
    LOG_BACKUP_COUNT=1

# Native libs matplotlib / scikit-learn need. No curl (the HEALTHCHECK uses
# python), no unixodbc / MSSQL driver: this image has no database access at all.
# `apt-get upgrade` patches the base image's own OS packages in the same layer:
# the base tag is rebuilt less often than its security fixes ship, so without it
# a fresh build inherits OS vulnerabilities that already have a fix. The release
# image scan is the detector for whatever is left (docs/BUILD_AND_RUN.md §8).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        fontconfig libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY executor/requirements.txt executor/requirements.txt
# Same rule as the main image: pip is a build-time tool kept current here
# rather than in a requirements file, and the upgrade runs BEFORE the install
# so the dependencies are installed by the fixed pip.
RUN pip install --no-cache-dir --upgrade pip==26.2.1
RUN pip install --no-cache-dir -r executor/requirements.txt

# Only the modules the runner imports — never `COPY . .`. No local_store, no
# db_*, no brain_client, no routes, no templates: the sandbox container cannot
# reach state or credentials it does not contain.
COPY code_exec.py plot_utils.py exec_sanitizer.py sandbox_guard.py outlier_utils.py logger_utils.py settings.py exec_transport.py ./
COPY static/fonts/DejaVuSans.ttf static/fonts/
COPY executor/__init__.py executor/app.py executor/runner.py executor/

# Offline plotly.js: bake the pip package's own plotly.min.js into static/vendor/
# so chart iframes never load from cdn.plot.ly (customer LANs may be air-gapped).
# `fig.to_html` only checks that the asset EXISTS to emit the local URL, so
# without this every generated chart would silently fall back to the CDN.
RUN python -c "import plotly, pathlib, shutil; d = pathlib.Path('static/vendor/plotly'); d.mkdir(parents=True, exist_ok=True); shutil.copyfile(str(pathlib.Path(plotly.__file__).parent / 'package_data' / 'plotly.min.js'), str(d / 'plotly.min.js'))"

# Two uids, ONE shared group: the web service (uid 10001) creates the job
# directory and writes the inputs, this service (uid 10002) writes the result
# into it. Both are in gid 10001 (`pdc`) and both run with umask 007, so
# neither needs to be root and nothing is world-writable. A FRESH named volume
# first mounted by this image inherits `root:pdc 2770` for /jobs, which lets
# both uids create job directories.
RUN groupadd -g 10001 pdc && useradd -u 10002 -g pdc -M -s /usr/sbin/nologin pdcexec
RUN mkdir -p /jobs && chown root:pdc /jobs && chmod 2770 /jobs

# Build identity, reported by GET /healthz for the main app's handshake.
ARG BUILD_COMMIT=""
ARG BUILD_TIME=""
ENV BUILD_COMMIT=${BUILD_COMMIT} \
    BUILD_TIME=${BUILD_TIME}

USER pdcexec

EXPOSE 8090

# The web service waits for `service_healthy` before it starts, so the FIRST
# probe decides how long every `up` takes: without a start interval Docker asks
# again only after the full 30 s interval. --start-interval needs Engine 25+; an
# older engine ignores the flag and behaves exactly as it did before.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s --start-interval=2s \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "executor.app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]

# The container test stage — NEVER the runtime. Root is required by the
# two-uid test (it chowns the shared dir and spawns helpers as 10001/10002),
# and httpx is Starlette's TestClient dependency, pinned to the root
# requirements' version so the test stage cannot introduce a second one.
# Run: docker run --rm --tmpfs /jobs pdc-executor-test python -m pytest executor/tests -q
FROM app AS test

USER root

RUN pip install --no-cache-dir pytest==8.3.3 httpx==0.27.2

COPY executor/tests/ executor/tests/
COPY tools/fixtures/sample_sales.csv tools/fixtures/

# The DEFAULT build target: docker builds the LAST stage when no `--target` is
# given, so the last stage must be the hardened image, not the test one. This
# stage adds NOTHING — it inherits `app` whole, `USER pdcexec` and CMD
# included, which makes a plain build and `--target runtime` the same image.
FROM app AS runtime
