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

# Build identity, surfaced by GET /version and the admin sidebar. Build args
# (NOT install-time config): `.git` is dockerignored, so the commit can only
# arrive from the builder. Declared AFTER the pip layer so passing them never
# invalidates the dependency cache. Absent -> the app shows its start time.
ARG BUILD_COMMIT=""
ARG BUILD_TIME=""
ENV BUILD_COMMIT=${BUILD_COMMIT} \
    BUILD_TIME=${BUILD_TIME}

RUN mkdir -p /data/client /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
