# PowerDataChat Client — enterprise (on-premise, customer's LAN)
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_ROOT=/data/client \
    BRAIN_URL=http://brain:8080

# Native libs for matplotlib / kaleido / pandas + unixodbc for pyodbc (MSSQL)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
        libxml2 libgomp1 fontconfig \
        unixodbc \
    && rm -rf /var/lib/apt/lists/*

# Microsoft ODBC Driver 18 for SQL Server (Debian 12 bookworm repo — the
# /debian/11/ path is the classic "driver won't load" mistake). Microsoft
# publishes bookworm packages for amd64/arm64 only, so the install is guarded:
# other architectures (and INSTALL_MSSQL_ODBC=0 builds) skip it and the mssql
# dialect reports {available: false} in the admin UI instead of breaking the
# image. ACCEPT_EULA is a scoped prefix, not a global ENV.
ARG INSTALL_MSSQL_ODBC=1
RUN if [ "$INSTALL_MSSQL_ODBC" = "1" ] \
       && { [ "$(dpkg --print-architecture)" = "amd64" ] || [ "$(dpkg --print-architecture)" = "arm64" ]; }; then \
         apt-get update \
         && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
              | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
         && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
              > /etc/apt/sources.list.d/mssql-release.list \
         && apt-get update \
         && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
         && rm -rf /var/lib/apt/lists/* ; \
       fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Offline plotly.js: bake the pip package's own plotly.min.js into static/vendor/
# so chart iframes never load from cdn.plot.ly (customer LANs may be air-gapped).
# The app lifespan runs the same idempotent copy as a self-heal for non-Docker runs.
RUN python -c "import plotly, pathlib, shutil; d = pathlib.Path('static/vendor/plotly'); d.mkdir(parents=True, exist_ok=True); shutil.copyfile(str(pathlib.Path(plotly.__file__).parent / 'package_data' / 'plotly.min.js'), str(d / 'plotly.min.js'))"

# Non-root runtime identity. The container runs on a read-only rootfs (see the
# compose files), so nothing may write inside the image: application state goes
# to DATA_ROOT on the mounted data volume, caches go to the tmpfs /tmp. Fixed
# uid/gid so a pre-existing data volume can be chowned to a known owner.
RUN groupadd -g 10001 pdc && useradd -u 10001 -g pdc -M -s /usr/sbin/nologin pdc

# Build identity, surfaced by GET /version and the admin sidebar. Build args
# (NOT install-time config): `.git` is dockerignored, so the commit can only
# arrive from the builder. Declared AFTER the pip layer so passing them never
# invalidates the dependency cache. Absent -> the app shows its start time.
ARG BUILD_COMMIT=""
ARG BUILD_TIME=""
ENV BUILD_COMMIT=${BUILD_COMMIT} \
    BUILD_TIME=${BUILD_TIME}

# Only the directories the app must write are created and handed to `pdc` —
# never all of /app. A FRESH named volume inherits this ownership of
# /data/client; a volume created by an earlier (root) image needs a one-time
# chown (see CUSTOMER_INSTALL.md). /tmp/mpl matters only for a plain
# `docker run` without --tmpfs: under compose the tmpfs hides it.
RUN mkdir -p /data/client /tmp/mpl \
    && chown -R pdc:pdc /data/client /tmp/mpl /app/static/vendor

# Caches and the home directory are redirected to the tmpfs: matplotlib's font
# cache, fontconfig, and kaleido's headless-Chromium profile all want a
# writable HOME, which a read-only rootfs cannot provide. HOME is /tmp itself
# (the tmpfs mounts over /tmp, so a subdirectory would never exist and a
# missing HOME is a known headless-Chromium failure mode).
ENV MPLCONFIGDIR=/tmp/mpl \
    XDG_CACHE_HOME=/tmp/cache \
    HOME=/tmp

USER pdc

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
